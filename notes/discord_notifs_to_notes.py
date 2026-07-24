from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests
import websockets
from loguru import logger

from discord_auth import load_discord_auth_token
from message_notif_logging import (
    MessageLogEntry,
    ReplyContext,
    delete_unlinked_message_notes,
    save_message_notifications,
)
from notes_utils import (
    configure_logger,
    format_notification_label,
    send_persistent_desktop_notification,
)


DISCORD_API_BASE_URL = "https://discord.com/api/v9"
DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json"
DISCORD_POLLING_ENABLED = False

NOTES_FILE = Path.home() / "notes/inbox-index.md"
LOG_PATH = Path(__file__).with_name("discord-notifs.log")
STATE_PATH = Path.home() / ".local/state/discord-notifs-state.json"

DISCORD_EPOCH_MS = 1420070400000


@dataclass(frozen=True)
class MessageCursor:
    chat_id: int
    sender_id: int
    timestamp_ms: int
    message_id: int


@dataclass(frozen=True)
class DiscordNotification:
    kind: str
    label: str
    url: str
    cursor: MessageCursor
    message: MessageLogEntry


@dataclass(frozen=True)
class DiscordMessage:
    chat_id: int
    sender_id: int
    sender_name: str
    message_id: int
    timestamp_ms: int
    content: str | None
    guild_id: int | None
    attachments: tuple[str, ...] = ()
    reply: ReplyContext | None = None


@dataclass(frozen=True)
class DiscordDMChannel:
    channel_id: int
    last_message_id: int | None = None


class DiscordApi(Protocol):
    def current_user_id(self) -> int: ...

    def read_state_last_message_ids(self) -> dict[int, int]: ...

    def dm_channels(self) -> list[DiscordDMChannel]: ...

    def recent_channel_messages(
        self, channel_id: int, *, limit: int
    ) -> list[DiscordMessage]: ...

    def channel_messages_after(
        self, channel_id: int, after_message_id: int
    ) -> list[DiscordMessage]: ...

    def recent_mentions(
        self, *, before_message_id: int | None, limit: int
    ) -> list[DiscordMessage]: ...

    def channel_display_name(self, channel_id: int) -> str: ...


def parse_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        raise RuntimeError(f"Invalid {field_name}: {value}")

    if parsed <= 0:
        raise RuntimeError(f"{field_name} must be positive: {value}")
    return parsed


def parse_non_negative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        raise RuntimeError(f"Invalid {field_name}: {value}")
    if parsed < 0:
        raise RuntimeError(f"{field_name} must be non-negative: {value}")
    return parsed


def snowflake_timestamp_ms(message_id: int) -> int:
    return (message_id >> 22) + DISCORD_EPOCH_MS


def message_sort_key(message: DiscordMessage) -> tuple[int, int]:
    return message.timestamp_ms, message.message_id


def cursor_sort_key(cursor: MessageCursor) -> tuple[int, int]:
    return cursor.timestamp_ms, cursor.message_id


def build_message_url(message: DiscordMessage) -> str:
    if message.guild_id is None:
        return (
            f"https://discord.com/channels/@me/{message.chat_id}/{message.message_id}"
        )
    return f"https://discord.com/channels/{message.guild_id}/{message.chat_id}/{message.message_id}"


def build_message_cursor(message: DiscordMessage) -> MessageCursor:
    return MessageCursor(
        chat_id=message.chat_id,
        sender_id=message.sender_id,
        timestamp_ms=message.timestamp_ms,
        message_id=message.message_id,
    )


def discord_message_text(message: DiscordMessage) -> str | None:
    parts: list[str] = []
    if message.content:
        parts.append(message.content)
    parts.extend(f"[attachment] {url}" for url in message.attachments)
    if not parts:
        return None
    return "\n".join(parts)


def message_is_unread(*, message_id: int, last_read_message_id: int) -> bool:
    return message_id > last_read_message_id


def required_last_read_message_id(
    *,
    read_state_last_message_ids: dict[int, int],
    channel_id: int,
) -> int:
    last_read_message_id = read_state_last_message_ids.get(channel_id)
    if last_read_message_id is None:
        raise RuntimeError(f"Missing Discord read state for channel {channel_id}")
    return last_read_message_id


def last_read_message_id_for_dm_channel(
    *,
    read_state_last_message_ids: dict[int, int],
    channel: DiscordDMChannel,
) -> int:
    last_read_message_id = read_state_last_message_ids.get(channel.channel_id)
    if last_read_message_id is not None:
        return last_read_message_id

    # Discord omits read_state rows for empty DMs that have never had a message.
    if channel.last_message_id is None:
        return 0

    raise RuntimeError(
        f"Missing Discord read state for non-empty channel {channel.channel_id}"
    )


def empty_state() -> dict[str, dict[int, MessageCursor]]:
    return {"dm": {}, "mention": {}}


def parse_cursor(chat_id_key: str, payload: Any) -> MessageCursor:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid Discord state record for chat {chat_id_key}")

    try:
        chat_id = int(chat_id_key)
    except ValueError as exc:
        raise RuntimeError(f"Invalid Discord state chat id: {chat_id_key}") from exc

    sender_id = payload.get("sender_id")
    timestamp_ms = payload.get("latest_message_timestamp_ms")
    message_id = payload.get("latest_message_id")
    read_flag = payload.get("read")

    if not isinstance(sender_id, int):
        raise RuntimeError(f"Invalid sender_id for chat {chat_id_key}")
    if not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
        raise RuntimeError(
            f"Invalid latest_message_timestamp_ms for chat {chat_id_key}"
        )
    if not isinstance(message_id, int) or message_id <= 0:
        raise RuntimeError(f"Invalid latest_message_id for chat {chat_id_key}")
    if read_flag is not True:
        raise RuntimeError(f"Invalid read flag for chat {chat_id_key}")

    return MessageCursor(
        chat_id=chat_id,
        sender_id=sender_id,
        timestamp_ms=timestamp_ms,
        message_id=message_id,
    )


def load_state(path: Path) -> dict[str, dict[int, MessageCursor]]:
    if not path.exists():
        return empty_state()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON state file: {path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"State file must contain an object: {path}")

    state = empty_state()
    for kind in ("dm", "mention"):
        raw_kind_state = payload.get(kind, {})
        if not isinstance(raw_kind_state, dict):
            raise RuntimeError(f"Invalid {kind} state in {path}")

        parsed_kind_state: dict[int, MessageCursor] = {}
        for chat_id_key, record in raw_kind_state.items():
            cursor = parse_cursor(str(chat_id_key), record)
            parsed_kind_state[cursor.chat_id] = cursor

        state[kind] = parsed_kind_state

    return state


def serialize_state(
    state: dict[str, dict[int, MessageCursor]],
) -> dict[str, dict[str, dict[str, int | bool]]]:
    serialized: dict[str, dict[str, dict[str, int | bool]]] = {}

    for kind in ("dm", "mention"):
        serialized_kind: dict[str, dict[str, int | bool]] = {}
        for chat_id, cursor in state[kind].items():
            serialized_kind[str(chat_id)] = {
                "chat_id": cursor.chat_id,
                "sender_id": cursor.sender_id,
                "latest_message_timestamp_ms": cursor.timestamp_ms,
                "latest_message_id": cursor.message_id,
                "read": True,
            }
        serialized[kind] = serialized_kind

    return serialized


def save_state(path: Path, state: dict[str, dict[int, MessageCursor]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(serialize_state(state), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def update_state_cursor(
    state: dict[str, dict[int, MessageCursor]],
    kind: str,
    cursor: MessageCursor,
) -> None:
    existing = state[kind].get(cursor.chat_id)
    if existing is None or cursor_sort_key(cursor) > cursor_sort_key(existing):
        state[kind][cursor.chat_id] = cursor


def build_notification(
    kind: str,
    message: DiscordMessage,
    *,
    conversation_name: str | None = None,
) -> DiscordNotification:
    label = format_notification_label(
        sender_name=message.sender_name,
        raw_text=discord_message_text(message),
        conversation_name=conversation_name,
        is_group_mention=kind == "mention" and conversation_name is not None,
    )
    url = build_message_url(message)
    log_entry = MessageLogEntry(
        source="discord",
        kind=kind,
        label=label,
        url=url,
        conversation_id=str(message.chat_id),
        conversation_name=conversation_name or message.sender_name,
        sender_name=message.sender_name,
        message_id=str(message.message_id),
        timestamp_ms=message.timestamp_ms,
        raw_text=discord_message_text(message),
        reply=message.reply,
    )
    return DiscordNotification(
        kind=kind,
        label=label,
        url=url,
        cursor=build_message_cursor(message),
        message=log_entry,
    )


def parse_attachment_urls(
    raw_message: dict[str, Any], *, message_id: int
) -> tuple[str, ...]:
    raw_attachments = raw_message.get("attachments", [])
    if raw_attachments is None:
        return ()
    if not isinstance(raw_attachments, list):
        raise RuntimeError(f"Discord message {message_id} has invalid attachments")

    urls: list[str] = []
    for attachment in raw_attachments:
        if not isinstance(attachment, dict):
            raise RuntimeError(f"Discord message {message_id} has invalid attachment")
        url = attachment.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url.strip())
    return tuple(urls)


def parse_reply_context(
    raw_message: dict[str, Any],
    *,
    default_chat_id: int,
) -> ReplyContext | None:
    raw_referenced_message = raw_message.get("referenced_message")
    if isinstance(raw_referenced_message, dict):
        referenced_message = parse_message_payload(
            raw_referenced_message,
            default_chat_id=default_chat_id,
            parse_referenced=False,
        )
        return ReplyContext(
            sender_name=referenced_message.sender_name,
            raw_text=discord_message_text(referenced_message),
            timestamp_ms=referenced_message.timestamp_ms,
            message_id=str(referenced_message.message_id),
            url=build_message_url(referenced_message),
        )

    if raw_message.get("message_reference") is not None:
        return ReplyContext(
            sender_name=None,
            raw_text=None,
            unavailable_reason=(
                "Discord returned a reply reference without the referenced message payload."
            ),
        )

    return None


def parse_message_payload(
    raw_message: dict[str, Any],
    *,
    default_chat_id: int | None = None,
    parse_referenced: bool = True,
) -> DiscordMessage:
    message_id = parse_positive_int(raw_message.get("id"), field_name="message id")

    raw_chat_id = raw_message.get("channel_id")
    if raw_chat_id is None:
        if default_chat_id is None:
            raise RuntimeError("Discord message missing channel_id")
        chat_id = default_chat_id
    else:
        chat_id = parse_positive_int(raw_chat_id, field_name="channel id")

    author = raw_message.get("author")
    if not isinstance(author, dict):
        raise RuntimeError(f"Discord message {message_id} missing author")
    sender_id = parse_positive_int(author.get("id"), field_name="sender id")
    global_name = author.get("global_name")
    username = author.get("username")
    if isinstance(global_name, str) and global_name.strip():
        sender_name = global_name.strip()
    elif isinstance(username, str) and username.strip():
        sender_name = username.strip()
    else:
        sender_name = f"user {sender_id}"

    content = raw_message.get("content")
    if content is not None and not isinstance(content, str):
        raise RuntimeError(f"Discord message {message_id} has invalid content")

    raw_guild_id = raw_message.get("guild_id")
    guild_id: int | None
    if raw_guild_id is None:
        guild_id = None
    else:
        guild_id = parse_positive_int(raw_guild_id, field_name="guild id")

    return DiscordMessage(
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        message_id=message_id,
        timestamp_ms=snowflake_timestamp_ms(message_id),
        content=content,
        guild_id=guild_id,
        attachments=parse_attachment_urls(raw_message, message_id=message_id),
        reply=(
            parse_reply_context(raw_message, default_chat_id=chat_id)
            if parse_referenced
            else None
        ),
    )


def parse_dm_channel_payload(
    raw_channel: dict[str, Any], *, self_user_id: int
) -> DiscordDMChannel:
    channel_type = raw_channel.get("type")
    if channel_type != 1:
        raise RuntimeError(f"Unsupported Discord DM channel type: {channel_type}")

    channel_id = parse_positive_int(raw_channel.get("id"), field_name="channel id")
    raw_last_message_id = raw_channel.get("last_message_id")
    last_message_id: int | None
    if raw_last_message_id is None:
        last_message_id = None
    else:
        last_message_id = parse_positive_int(
            raw_last_message_id,
            field_name=f"last_message_id for channel {channel_id}",
        )

    recipients = raw_channel.get("recipients")
    if not isinstance(recipients, list):
        raise RuntimeError(f"Discord DM channel {channel_id} missing recipients")

    recipient_records = [
        recipient for recipient in recipients if isinstance(recipient, dict)
    ]
    if not recipient_records:
        raise RuntimeError(f"Discord DM channel {channel_id} has no recipients")

    recipient = recipient_records[0]
    for candidate in recipient_records:
        candidate_id = parse_positive_int(
            candidate.get("id"), field_name="recipient id"
        )
        if candidate_id != self_user_id:
            recipient = candidate
            break

    return DiscordDMChannel(
        channel_id=channel_id,
        last_message_id=last_message_id,
    )


class DiscordHttpApi:
    def __init__(self, token: str) -> None:
        self._token = token
        self._current_user_id: int | None = None
        self._read_state_last_message_ids: dict[int, int] | None = None
        self._channel_display_names: dict[int, str] = {}

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        url = f"{DISCORD_API_BASE_URL}{path}"
        headers = {"authorization": self._token}

        retry_count = 3
        for attempt in range(retry_count + 1):
            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code != 429:
                break

            if attempt >= retry_count:
                raise RuntimeError(
                    f"Discord API rate-limited after {retry_count + 1} attempts: {path}"
                )

            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid Discord 429 response for {path}") from exc

            retry_after = payload.get("retry_after")
            if not isinstance(retry_after, (int, float)) or retry_after < 0:
                raise RuntimeError(
                    f"Discord 429 response missing retry_after for {path}"
                )
            time.sleep(retry_after)

        if response.status_code >= 400:
            body = response.text.strip()
            raise RuntimeError(
                f"Discord API error {response.status_code} for {path}: {body}"
            )

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid JSON response from Discord API for {path}"
            ) from exc

    def current_user_id(self) -> int:
        if self._current_user_id is not None:
            return self._current_user_id
        payload = self._get("/users/@me")
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected /users/@me response")
        self._current_user_id = parse_positive_int(
            payload.get("id"), field_name="current user id"
        )
        return self._current_user_id

    async def _gateway_ready_payload(self) -> dict[str, Any]:
        async with websockets.connect(DISCORD_GATEWAY_URL, max_size=2**23) as websocket:
            hello = json.loads(await websocket.recv())
            if not isinstance(hello, dict) or hello.get("op") != 10:
                raise RuntimeError("Unexpected Discord gateway hello payload")
            hello_data = hello.get("d")
            if not isinstance(hello_data, dict):
                raise RuntimeError("Discord gateway hello payload missing data")

            heartbeat_interval_ms = hello_data.get("heartbeat_interval")
            if not isinstance(heartbeat_interval_ms, int) or heartbeat_interval_ms <= 0:
                raise RuntimeError("Discord gateway heartbeat interval is invalid")

            sequence: int | None = None

            async def heartbeat_loop() -> None:
                while True:
                    await asyncio.sleep(heartbeat_interval_ms / 1000)
                    await websocket.send(json.dumps({"op": 1, "d": sequence}))

            heartbeat_task = asyncio.create_task(heartbeat_loop())
            await websocket.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {
                            "token": self._token,
                            "properties": {
                                "os": "linux",
                                "browser": "chrome",
                                "device": "desktop",
                            },
                            "compress": False,
                            "large_threshold": 50,
                            "v": 9,
                        },
                    }
                )
            )

            try:
                while True:
                    raw_event = await asyncio.wait_for(websocket.recv(), timeout=45)
                    event = json.loads(raw_event)
                    if not isinstance(event, dict):
                        raise RuntimeError("Discord gateway event payload is invalid")

                    sequence_value = event.get("s")
                    if isinstance(sequence_value, int):
                        sequence = sequence_value

                    op = event.get("op")
                    if op == 9:
                        raise RuntimeError("Discord gateway rejected session")
                    if op != 0:
                        continue

                    event_type = event.get("t")
                    if event_type != "READY":
                        continue

                    ready_payload = event.get("d")
                    if not isinstance(ready_payload, dict):
                        raise RuntimeError("Discord READY payload is invalid")
                    return ready_payload
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    def read_state_last_message_ids(self) -> dict[int, int]:
        if self._read_state_last_message_ids is not None:
            return self._read_state_last_message_ids

        ready_payload = asyncio.run(self._gateway_ready_payload())
        read_state_entries = ready_payload.get("read_state")
        if not isinstance(read_state_entries, list):
            raise RuntimeError("Discord READY payload missing read_state entries")

        read_state_last_message_ids: dict[int, int] = {}
        for entry in read_state_entries:
            if not isinstance(entry, dict):
                raise RuntimeError("Discord read_state entry payload is invalid")

            channel_id = parse_positive_int(
                entry.get("id"), field_name="read state channel id"
            )
            last_read_raw = entry.get("last_message_id")
            if last_read_raw is None:
                last_read_message_id = 0
            else:
                last_read_message_id = parse_non_negative_int(
                    last_read_raw,
                    field_name=f"read state last_message_id for channel {channel_id}",
                )
            read_state_last_message_ids[channel_id] = last_read_message_id

        self._read_state_last_message_ids = read_state_last_message_ids
        return read_state_last_message_ids

    def dm_channels(self) -> list[DiscordDMChannel]:
        self_user_id = self.current_user_id()
        payload = self._get("/users/@me/channels")
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected /users/@me/channels response")

        dm_channels: list[DiscordDMChannel] = []
        for channel in payload:
            if not isinstance(channel, dict):
                raise RuntimeError("Discord channel payload is not an object")
            if channel.get("type") != 1:
                continue
            dm_channels.append(
                parse_dm_channel_payload(channel, self_user_id=self_user_id)
            )
        return dm_channels

    def recent_channel_messages(
        self, channel_id: int, *, limit: int
    ) -> list[DiscordMessage]:
        if limit <= 0:
            return []
        payload = self._get(
            f"/channels/{channel_id}/messages",
            params={"limit": str(limit)},
        )
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected messages response for channel {channel_id}")
        messages = [
            parse_message_payload(message, default_chat_id=channel_id)
            for message in payload
        ]
        messages.sort(key=message_sort_key)
        return messages

    def channel_messages_after(
        self, channel_id: int, after_message_id: int
    ) -> list[DiscordMessage]:
        all_messages: list[DiscordMessage] = []
        last_after = after_message_id

        while True:
            payload = self._get(
                f"/channels/{channel_id}/messages",
                params={"limit": "100", "after": str(last_after)},
            )
            if not isinstance(payload, list):
                raise RuntimeError(
                    f"Unexpected messages response for channel {channel_id}"
                )
            if not payload:
                break

            page_messages = [
                parse_message_payload(message, default_chat_id=channel_id)
                for message in payload
            ]
            page_messages.sort(key=message_sort_key)
            all_messages.extend(page_messages)

            page_latest_message_id = page_messages[-1].message_id
            if page_latest_message_id <= last_after:
                raise RuntimeError(
                    f"Discord pagination did not advance for channel {channel_id}"
                )
            last_after = page_latest_message_id

            if len(page_messages) < 100:
                break

        return all_messages

    def recent_mentions(
        self, *, before_message_id: int | None, limit: int
    ) -> list[DiscordMessage]:
        params = {"limit": str(limit)}
        if before_message_id is not None:
            params["before"] = str(before_message_id)

        payload = self._get("/users/@me/mentions", params=params)
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected /users/@me/mentions response")

        mentions: list[DiscordMessage] = []
        for item in payload:
            if not isinstance(item, dict):
                raise RuntimeError("Discord mention payload is not an object")
            if isinstance(item.get("message"), dict):
                message_payload = item["message"]
            else:
                message_payload = item
            mentions.append(parse_message_payload(message_payload))

        mentions.sort(key=message_sort_key)
        return mentions

    def channel_display_name(self, channel_id: int) -> str:
        cached_display_name = self._channel_display_names.get(channel_id)
        if cached_display_name is not None:
            return cached_display_name

        payload = self._get(f"/channels/{channel_id}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected Discord channel response for {channel_id}")

        channel_name = payload.get("name")
        if not isinstance(channel_name, str) or not channel_name.strip():
            raise RuntimeError(f"Discord channel {channel_id} missing name")

        display_name = channel_name.strip()
        self._channel_display_names[channel_id] = display_name
        return display_name


def collect_notifications(
    api: DiscordApi,
    state: dict[str, dict[int, MessageCursor]],
) -> tuple[list[DiscordNotification], dict[str, dict[int, MessageCursor]]]:
    notifications: list[DiscordNotification] = []
    self_user_id = api.current_user_id()
    read_state_last_message_ids = api.read_state_last_message_ids()

    for channel in api.dm_channels():
        dm_cursor = state["dm"].get(channel.channel_id)
        last_read_message_id = last_read_message_id_for_dm_channel(
            read_state_last_message_ids=read_state_last_message_ids,
            channel=channel,
        )
        fetch_after_message_id = last_read_message_id
        if dm_cursor is not None and dm_cursor.message_id > fetch_after_message_id:
            fetch_after_message_id = dm_cursor.message_id

        new_messages = api.channel_messages_after(
            channel.channel_id, fetch_after_message_id
        )
        if not new_messages and dm_cursor is None:
            latest_messages = api.recent_channel_messages(channel.channel_id, limit=1)
            if latest_messages:
                update_state_cursor(
                    state, "dm", build_message_cursor(latest_messages[-1])
                )
            continue

        for message in new_messages:
            update_state_cursor(state, "dm", build_message_cursor(message))
            if message.sender_id == self_user_id:
                continue
            if not message_is_unread(
                message_id=message.message_id,
                last_read_message_id=last_read_message_id,
            ):
                continue
            notifications.append(build_notification("dm", message))

    candidate_mentions: list[DiscordMessage] = []
    before_message_id: int | None = None
    while True:
        page = api.recent_mentions(before_message_id=before_message_id, limit=100)
        if not page:
            break

        page_has_candidate = False
        for message in page:
            mention_cursor = state["mention"].get(message.chat_id)
            last_read_message_id = required_last_read_message_id(
                read_state_last_message_ids=read_state_last_message_ids,
                channel_id=message.chat_id,
            )

            threshold_message_id = last_read_message_id
            if (
                mention_cursor is not None
                and mention_cursor.message_id > threshold_message_id
            ):
                threshold_message_id = mention_cursor.message_id

            if message.message_id <= threshold_message_id:
                continue

            page_has_candidate = True
            candidate_mentions.append(message)

        if not page_has_candidate:
            break

        if len(page) < 100:
            break

        oldest_page_message_id = page[0].message_id
        if before_message_id == oldest_page_message_id:
            raise RuntimeError("Discord mentions pagination did not advance")
        before_message_id = oldest_page_message_id

    unique_mentions = {message.message_id: message for message in candidate_mentions}
    ordered_mentions = sorted(unique_mentions.values(), key=message_sort_key)

    for message in ordered_mentions:
        message_cursor = build_message_cursor(message)
        update_state_cursor(state, "mention", message_cursor)
        conversation_name = None
        if message.guild_id is not None:
            conversation_name = api.channel_display_name(message.chat_id)
        notifications.append(
            build_notification(
                "mention",
                message,
                conversation_name=conversation_name,
            )
        )

    return notifications, state


def run() -> int:
    configure_logger(LOG_PATH)
    if not DISCORD_POLLING_ENABLED:
        logger.warning("Discord notification polling is disabled")
        return 0

    token = load_discord_auth_token()

    api = DiscordHttpApi(token)
    state = load_state(STATE_PATH)
    notifications, next_state = collect_notifications(api, state)

    if not notifications:
        logger.info("No new Discord DMs or mentions")
        deleted_message_notes = delete_unlinked_message_notes(NOTES_FILE.parent)
        if deleted_message_notes:
            logger.info(f"Deleted {len(deleted_message_notes)} unlinked message notes")
        save_state(STATE_PATH, next_state)
        return 0

    for notification in notifications:
        logger.info(f"Added: {notification.label}")
        send_persistent_desktop_notification(
            app_name="Discord",
            summary=notification.label,
            category="discord",
            on_click_url=notification.url,
        )

    save_message_notifications(
        NOTES_FILE,
        NOTES_FILE.parent,
        [notification.message for notification in notifications],
    )
    save_state(STATE_PATH, next_state)

    logger.info("Processed Discord notifications")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
