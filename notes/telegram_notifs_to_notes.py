from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from telethon import TelegramClient
from telethon.tl import functions
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    Channel,
    Chat,
    InputMessagesFilterMyMentions,
    MessageService,
    User,
)
from telethon.utils import get_peer_id

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


TELEGRAM_API_ID_ENV = "TELEGRAM_API_ID"
TELEGRAM_API_HASH_ENV = "TELEGRAM_API_HASH"
TELEGRAM_SESSION_NAME_ENV = "TELEGRAM_SESSION_NAME"
TELEGRAM_AUTH_COMMAND = (
    f"cd {Path(__file__).resolve().parents[1]} && "
    "uv run --env-file .env python notes/auth_telegram_notifs.py"
)

NOTES_FILE = Path.home() / "notes/inbox-index.md"
LOG_PATH = Path(__file__).with_name("telegram-notifs.log")
STATE_PATH = Path.home() / ".local/state/telegram-notifs-state.json"
SMALL_GROUP_MAX_PARTICIPANTS = 15


@dataclass(frozen=True)
class MessageCursor:
    chat_id: int
    sender_id: int
    timestamp_ms: int
    message_id: int


@dataclass(frozen=True)
class TelegramNotification:
    kind: str
    label: str
    url: str | None
    cursor: MessageCursor
    message: MessageLogEntry


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def telegram_api_id() -> int:
    value = require_env(TELEGRAM_API_ID_ENV)
    try:
        api_id = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{TELEGRAM_API_ID_ENV} must be an integer") from exc
    if api_id <= 0:
        raise RuntimeError(f"{TELEGRAM_API_ID_ENV} must be positive")
    return api_id


def notify_telegram_reauth_needed(session_name: str) -> None:
    send_persistent_desktop_notification(
        app_name="Telegram",
        summary="Telegram notification sync needs re-auth",
        body=(
            f"Session `{session_name}` is not authorized. Run:\n"
            f"{TELEGRAM_AUTH_COMMAND}"
        ),
        category="telegram",
    )


async def ensure_telegram_authorized(
    client: TelegramClient, session_name: str
) -> None:
    if await client.is_user_authorized():
        return

    try:
        notify_telegram_reauth_needed(session_name)
    except Exception as exc:
        logger.warning(f"Failed to send Telegram re-auth notification: {exc}")

    raise RuntimeError(
        "Telegram notification session is not authorized. Run: "
        f"{TELEGRAM_AUTH_COMMAND}"
    )


def dialog_entity_id(entity: Any) -> int:
    entity_id = getattr(entity, "id", None)
    if not isinstance(entity_id, int):
        raise RuntimeError("Telegram entity missing integer id")
    return entity_id


def message_sort_key(message: Message) -> tuple[int, int]:
    if not isinstance(message.id, int):
        raise RuntimeError("Telegram message missing id")
    if not isinstance(message.date, datetime):
        raise RuntimeError("Telegram message missing date")

    message_date = message.date
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=timezone.utc)

    timestamp_ms = int(message_date.timestamp() * 1000)
    return timestamp_ms, message.id


def cursor_sort_key(cursor: MessageCursor) -> tuple[int, int]:
    return cursor.timestamp_ms, cursor.message_id


def is_message_newer_than_cursor(message: Message, cursor: MessageCursor) -> bool:
    return message_sort_key(message) > cursor_sort_key(cursor)


def display_name(entity: Any, dialog_name: str | None) -> str:
    if dialog_name:
        return dialog_name
    if isinstance(entity, User):
        name_parts = [part for part in (entity.first_name, entity.last_name) if part]
        if name_parts:
            return " ".join(name_parts)
        if entity.username:
            return entity.username
        return f"user {entity.id}"
    title = getattr(entity, "title", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    username = getattr(entity, "username", None)
    if isinstance(username, str) and username.strip():
        return username.strip()
    return f"chat {dialog_entity_id(entity)}"


def build_dialog_url(entity: Any) -> str | None:
    if isinstance(entity, User):
        username = (entity.username or "").strip()
        if username:
            return f"https://t.me/{username}"

        phone = (getattr(entity, "phone", "") or "").strip()
        if phone:
            digits = "".join(character for character in phone if character.isdigit())
            if digits:
                return f"tg://resolve?phone={digits}"

        return None
    if isinstance(entity, Chat):
        return f"https://web.telegram.org/k/#{get_peer_id(entity)}"
    if isinstance(entity, Channel):
        return None

    return None


async def resolve_dialog_url(client: TelegramClient, entity: Any) -> str | None:
    dialog_url = build_dialog_url(entity)
    if not isinstance(entity, Chat):
        return dialog_url

    full_chat = await client(functions.messages.GetFullChatRequest(chat_id=entity.id))
    exported_invite = getattr(
        getattr(full_chat, "full_chat", None), "exported_invite", None
    )
    invite_link = getattr(exported_invite, "link", None)
    if isinstance(invite_link, str) and invite_link:
        return invite_link

    return dialog_url


def build_message_url(
    entity: Any, message: Message, dialog_url: str | None = None
) -> str | None:
    if not isinstance(message.id, int):
        raise RuntimeError("Telegram message missing id")

    message_link = getattr(message, "link", None)
    if isinstance(message_link, str) and message_link:
        return message_link

    # Telegram Desktop only deep-links to specific messages for channels/supergroups.
    if isinstance(entity, Channel):
        return f"https://t.me/c/{entity.id}/{message.id}"
    if isinstance(entity, (User, Chat)):
        return build_dialog_url(entity) if dialog_url is None else dialog_url

    raise RuntimeError("Unsupported Telegram entity type for URL")


def build_message_cursor(entity: Any, message: Message) -> MessageCursor:
    timestamp_ms, message_id = message_sort_key(message)
    chat_id = dialog_entity_id(entity)

    sender_id = message.sender_id
    if isinstance(sender_id, int):
        resolved_sender_id = sender_id
    else:
        resolved_sender_id = chat_id

    return MessageCursor(
        chat_id=chat_id,
        sender_id=resolved_sender_id,
        timestamp_ms=timestamp_ms,
        message_id=message_id,
    )


def empty_state() -> dict[str, dict[int, MessageCursor]]:
    return {"dm": {}, "group": {}, "mention": {}}


def parse_cursor(chat_id_key: str, payload: Any) -> MessageCursor:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid Telegram state record for chat {chat_id_key}")

    try:
        chat_id = int(chat_id_key)
    except ValueError as exc:
        raise RuntimeError(f"Invalid Telegram state chat id: {chat_id_key}") from exc

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
    for kind in ("dm", "group", "mention"):
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

    for kind in ("dm", "group", "mention"):
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


async def fetch_initial_incoming_messages(
    client: TelegramClient, entity: Any, unread_count: int
) -> list[Message]:
    if unread_count <= 0:
        return []

    unread_messages: list[Message] = []
    async for message in client.iter_messages(entity):
        if isinstance(message, MessageService):
            continue
        if message.out:
            continue
        unread_messages.append(message)
        if len(unread_messages) >= unread_count:
            break

    unread_messages.reverse()
    return unread_messages


async def fetch_new_incoming_messages_since_cursor(
    client: TelegramClient,
    entity: Any,
    cursor: MessageCursor,
) -> list[Message]:
    new_messages: list[Message] = []
    async for message in client.iter_messages(entity):
        if isinstance(message, MessageService):
            continue
        if message.out:
            continue
        if not is_message_newer_than_cursor(message, cursor):
            break
        new_messages.append(message)

    new_messages.reverse()
    return new_messages


async def fetch_initial_mentions(
    client: TelegramClient, entity: Any, unread_mentions_count: int
) -> list[Message]:
    if unread_mentions_count <= 0:
        return []

    unread_mentions: list[Message] = []
    mention_filter = InputMessagesFilterMyMentions()
    async for message in client.iter_messages(entity, filter=mention_filter):
        if isinstance(message, MessageService):
            continue
        if not message.mentioned:
            continue
        unread_mentions.append(message)
        if len(unread_mentions) >= unread_mentions_count:
            break

    unread_mentions.reverse()
    return unread_mentions


async def fetch_new_mentions_since_cursor(
    client: TelegramClient,
    entity: Any,
    cursor: MessageCursor,
) -> list[Message]:
    new_mentions: list[Message] = []
    mention_filter = InputMessagesFilterMyMentions()
    async for message in client.iter_messages(entity, filter=mention_filter):
        if isinstance(message, MessageService):
            continue
        if not message.mentioned:
            continue
        if not is_message_newer_than_cursor(message, cursor):
            break
        new_mentions.append(message)

    new_mentions.reverse()
    return new_mentions


def update_state_cursor(
    state: dict[str, dict[int, MessageCursor]],
    kind: str,
    cursor: MessageCursor,
) -> None:
    existing = state[kind].get(cursor.chat_id)
    if existing is None or cursor_sort_key(cursor) > cursor_sort_key(existing):
        state[kind][cursor.chat_id] = cursor


def group_participant_count(entity: Any) -> int | None:
    participants_count = getattr(entity, "participants_count", None)
    if isinstance(participants_count, int):
        return participants_count

    participants = getattr(entity, "participants", None)
    if isinstance(participants, list):
        return len(participants)

    return None


def normalize_mute_until(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, int) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def dialog_is_muted(dialog: Any) -> bool:
    notify_settings = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    mute_until = normalize_mute_until(getattr(notify_settings, "mute_until", None))
    return mute_until is not None and mute_until > datetime.now(timezone.utc)


def is_small_unmuted_group(dialog: Any, entity: Any) -> bool:
    if not bool(getattr(dialog, "is_group", False)):
        return False
    if dialog_is_muted(dialog):
        return False

    participant_count = group_participant_count(entity)
    if participant_count is None:
        return False

    return participant_count < SMALL_GROUP_MAX_PARTICIPANTS


def first_notifiable_index(
    total_messages: int, unread_count: int, unread_mark: bool
) -> int:
    if unread_mark:
        return 0
    if unread_count <= 0:
        return total_messages
    if unread_count >= total_messages:
        return 0
    return total_messages - unread_count


async def resolve_sender_name(
    client: TelegramClient,
    entity: Any,
    message: Message,
    sender_name_cache: dict[int, str],
) -> str:
    sender = getattr(message, "sender", None)
    if sender is None and isinstance(entity, User):
        sender = entity

    sender_id = getattr(message, "sender_id", None)
    if sender is None and isinstance(sender_id, int):
        cached_sender_name = sender_name_cache.get(sender_id)
        if cached_sender_name is not None:
            return cached_sender_name

        sender = await client.get_entity(sender_id)

    if sender is None:
        sender_name = display_name(entity, None)
    else:
        sender_name = display_name(sender, None)

    if isinstance(sender_id, int):
        sender_name_cache[sender_id] = sender_name
    return sender_name


def raw_message_text(message: Message) -> str | None:
    raw_text = getattr(message, "raw_text", None)
    if isinstance(raw_text, str) and raw_text:
        return raw_text
    return None


def maybe_message_timestamp_ms(message: Message) -> int | None:
    try:
        timestamp_ms, _message_id = message_sort_key(message)
    except RuntimeError:
        return None
    return timestamp_ms


async def resolve_reply_context(
    client: TelegramClient,
    entity: Any,
    message: Message,
    dialog_url: str | None,
    sender_name_cache: dict[int, str],
) -> ReplyContext | None:
    reply_message_id = getattr(message, "reply_to_msg_id", None)
    if not isinstance(reply_message_id, int):
        return None

    get_reply_message = getattr(message, "get_reply_message", None)
    if not callable(get_reply_message):
        return ReplyContext(
            sender_name=None,
            raw_text=None,
            message_id=str(reply_message_id),
            unavailable_reason=(
                "Telegram reported a reply target, but the message object could not fetch it."
            ),
        )

    reply_message = await get_reply_message()
    if reply_message is None:
        return ReplyContext(
            sender_name=None,
            raw_text=None,
            message_id=str(reply_message_id),
            unavailable_reason="Telegram reply target is unavailable, deleted, or inaccessible.",
        )

    reply_sender_name = await resolve_sender_name(
        client,
        entity,
        reply_message,
        sender_name_cache,
    )
    reply_url = build_message_url(entity, reply_message, dialog_url)
    return ReplyContext(
        sender_name=reply_sender_name,
        raw_text=raw_message_text(reply_message),
        timestamp_ms=maybe_message_timestamp_ms(reply_message),
        message_id=str(getattr(reply_message, "id", reply_message_id)),
        url=reply_url,
    )


def build_notification(
    kind: str,
    entity: Any,
    message: Message,
    dialog_url: str | None,
    sender_name: str,
    conversation_name: str | None,
    reply_context: ReplyContext | None,
) -> TelegramNotification:
    cursor = build_message_cursor(entity, message)
    label = format_notification_label(
        sender_name=sender_name,
        raw_text=raw_message_text(message),
        conversation_name=conversation_name,
        is_group_mention=kind == "mention" and conversation_name is not None,
    )
    url = build_message_url(entity, message, dialog_url)
    log_entry = MessageLogEntry(
        source="telegram",
        kind=kind,
        label=label,
        url=url,
        conversation_id=str(cursor.chat_id),
        conversation_name=conversation_name or sender_name,
        sender_name=sender_name,
        message_id=str(cursor.message_id),
        timestamp_ms=cursor.timestamp_ms,
        raw_text=raw_message_text(message),
        reply=reply_context,
    )

    return TelegramNotification(
        kind=kind,
        label=label,
        url=url,
        cursor=cursor,
        message=log_entry,
    )


async def collect_notifications(
    client: TelegramClient,
    state: dict[str, dict[int, MessageCursor]],
) -> tuple[list[TelegramNotification], dict[str, dict[int, MessageCursor]]]:
    notifications: list[TelegramNotification] = []
    sender_name_cache: dict[int, str] = {}

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if entity is None:
            continue

        chat_id = dialog_entity_id(entity)
        conversation_name = None if dialog.is_user else display_name(entity, dialog.name)
        dialog_url = await resolve_dialog_url(client, entity)
        unread_count = int(dialog.unread_count or 0)
        unread_mentions_count = int(getattr(dialog, "unread_mentions_count", 0) or 0)
        dialog_unread_mark = bool(getattr(dialog, "unread_mark", False))

        message_kind = None
        if dialog.is_user:
            message_kind = "dm"
        elif is_small_unmuted_group(dialog, entity):
            message_kind = "group"

        if message_kind is not None:
            message_cursor = state[message_kind].get(chat_id)
            if message_cursor is None:
                messages = await fetch_initial_incoming_messages(
                    client, entity, unread_count
                )
            else:
                messages = await fetch_new_incoming_messages_since_cursor(
                    client, entity, message_cursor
                )

            message_notification_start = first_notifiable_index(
                total_messages=len(messages),
                unread_count=unread_count,
                unread_mark=dialog_unread_mark,
            )
            for index, message in enumerate(messages):
                unread_cursor = build_message_cursor(entity, message)
                update_state_cursor(state, message_kind, unread_cursor)
                if index < message_notification_start:
                    continue
                sender_name = await resolve_sender_name(
                    client,
                    entity,
                    message,
                    sender_name_cache,
                )
                reply_context = await resolve_reply_context(
                    client,
                    entity,
                    message,
                    dialog_url,
                    sender_name_cache,
                )
                notifications.append(
                    build_notification(
                        message_kind,
                        entity,
                        message,
                        dialog_url,
                        sender_name,
                        conversation_name if message_kind == "group" else None,
                        reply_context,
                    )
                )

        mention_cursor = state["mention"].get(chat_id)

        if mention_cursor is None and unread_mentions_count <= 0:
            continue

        if mention_cursor is None:
            mention_messages = await fetch_initial_mentions(
                client, entity, unread_mentions_count
            )
        else:
            mention_messages = await fetch_new_mentions_since_cursor(
                client, entity, mention_cursor
            )

        mention_notification_start = first_notifiable_index(
            total_messages=len(mention_messages),
            unread_count=unread_mentions_count,
            unread_mark=dialog_unread_mark,
        )
        for index, message in enumerate(mention_messages):
            message_cursor = build_message_cursor(entity, message)
            update_state_cursor(state, "mention", message_cursor)
            if index < mention_notification_start:
                continue
            sender_name = await resolve_sender_name(
                client,
                entity,
                message,
                sender_name_cache,
            )
            reply_context = await resolve_reply_context(
                client,
                entity,
                message,
                dialog_url,
                sender_name_cache,
            )
            notifications.append(
                build_notification(
                    "mention",
                    entity,
                    message,
                    dialog_url,
                    sender_name,
                    conversation_name,
                    reply_context,
                )
            )

    return notifications, state


async def run() -> int:
    configure_logger(LOG_PATH)

    api_id = telegram_api_id()
    api_hash = require_env(TELEGRAM_API_HASH_ENV)
    session_name = require_env(TELEGRAM_SESSION_NAME_ENV)

    state = load_state(STATE_PATH)

    client = TelegramClient(session_name, api_id, api_hash)
    await client.connect()
    try:
        await ensure_telegram_authorized(client, session_name)
        notifications, next_state = await collect_notifications(client, state)
    finally:
        await client.disconnect()

    if not notifications:
        logger.info("No new Telegram DMs or mentions")
        deleted_message_notes = delete_unlinked_message_notes(NOTES_FILE.parent)
        if deleted_message_notes:
            logger.info(f"Deleted {len(deleted_message_notes)} unlinked message notes")
        save_state(STATE_PATH, next_state)
        return 0

    for notification in notifications:
        logger.info(f"Added: {notification.label}")
        send_persistent_desktop_notification(
            app_name="Telegram",
            summary=notification.label,
            category="telegram",
            on_click_url=notification.url,
        )

    save_message_notifications(
        NOTES_FILE,
        NOTES_FILE.parent,
        [notification.message for notification in notifications],
    )
    save_state(STATE_PATH, next_state)

    logger.info("Processed Telegram notifications")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
