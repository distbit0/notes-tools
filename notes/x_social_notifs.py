from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import requests

from notes_utils import format_notification_label
from social_notif_common import (
    ItemCursor,
    SocialNotification,
    USER_AGENT,
    cursor_sort_key,
    is_newer_than_cursor,
    load_brave_cookies,
    parse_int,
    request_json,
    require_non_empty_string,
    update_state_cursor,
)


X_BOOTSTRAP_URL = "https://x.com/notifications"
X_API_BASE_URL = "https://x.com/i/api"
X_MENTION_ELEMENTS = {
    "user_replied_to_your_tweet": "reply",
    "user_mentioned_you": "mention",
}
X_COMMON_PARAMS = {
    "include_profile_interstitial_type": "1",
    "include_blocking": "1",
    "include_blocked_by": "1",
    "include_followed_by": "1",
    "include_want_retweets": "1",
    "include_mute_edge": "1",
    "include_can_dm": "1",
    "include_can_media_tag": "1",
    "include_ext_has_nft_avatar": "1",
    "include_ext_is_blue_verified": "1",
    "include_ext_verified_type": "1",
    "include_ext_profile_image_shape": "1",
    "skip_status": "1",
    "cards_platform": "Web-12",
    "include_cards": "1",
    "include_ext_alt_text": "true",
    "include_ext_limited_action_results": "false",
    "include_quote_count": "true",
    "include_reply_count": "1",
    "tweet_mode": "extended",
    "include_ext_views": "true",
    "include_entities": "true",
    "include_user_entities": "true",
    "include_ext_media_color": "true",
    "include_ext_media_availability": "true",
    "include_ext_sensitive_media_warning": "true",
    "send_error_codes": "true",
    "simple_quoted_tweet": "true",
}


def parse_x_self_user_id(twid_cookie: str) -> str:
    decoded = unquote(twid_cookie)
    match = re.search(r"u=(\d+)", decoded)
    if not match:
        raise RuntimeError("X twid cookie does not contain a user id")
    return match.group(1)


def fetch_x_bearer_token(session: requests.Session, cookie_header: str) -> str:
    response = session.get(
        X_BOOTSTRAP_URL,
        headers={"Cookie": cookie_header, "User-Agent": USER_AGENT},
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"X bootstrap returned HTTP {response.status_code}: {response.text[:500]}"
        )

    scripts = sorted(
        set(
            re.findall(
                r"https://abs\.twimg\.com/responsive-web/client-web/[^\"<>]+?\.js",
                response.text,
            )
        ),
        key=lambda url: ("main." not in url, url),
    )
    if not scripts:
        raise RuntimeError("X bootstrap page did not include web app scripts")

    for script_url in scripts:
        script_response = session.get(
            script_url,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if script_response.status_code >= 400:
            raise RuntimeError(
                f"X script returned HTTP {script_response.status_code}: {script_url}"
            )
        match = re.search(r"Bearer ([A-Za-z0-9%_-]+)", script_response.text)
        if match:
            return match.group(1)

    raise RuntimeError("Could not find X bearer token in web app scripts")


@dataclass(frozen=True)
class XClient:
    session: requests.Session
    cookie_header: str
    csrf_token: str
    bearer_token: str
    self_user_id: str

    @classmethod
    def from_brave(cls) -> "XClient":
        cookies = load_brave_cookies(("x.com",))
        csrf_token = require_non_empty_string(
            cookies.values_by_name.get("ct0"),
            field_name="X ct0 cookie",
        )
        self_user_id = parse_x_self_user_id(
            require_non_empty_string(
                cookies.values_by_name.get("twid"),
                field_name="X twid cookie",
            )
        )
        session = requests.Session()
        bearer_token = fetch_x_bearer_token(session, cookies.header)
        return cls(
            session=session,
            cookie_header=cookies.header,
            csrf_token=csrf_token,
            bearer_token=bearer_token,
            self_user_id=self_user_id,
        )

    def api_headers(self, referer: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "Cookie": self.cookie_header,
            "X-CSRF-Token": self.csrf_token,
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Auth-Type": "OAuth2Session",
            "X-Twitter-Client-Language": "en",
            "Accept": "application/json",
            "Referer": referer,
            "User-Agent": USER_AGENT,
        }

    def get_json(
        self, path: str, *, params: dict[str, str], referer: str
    ) -> dict[str, Any]:
        payload = request_json(
            self.session,
            "GET",
            f"{X_API_BASE_URL}{path}",
            headers=self.api_headers(referer),
            params=params,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected X response for {path}")
        return payload

    def fetch_mentions(self) -> dict[str, Any]:
        return self.get_json(
            "/2/notifications/mentions.json",
            params={**X_COMMON_PARAMS, "count": "20"},
            referer="https://x.com/notifications/mentions",
        )

    def fetch_dm_inbox(self) -> dict[str, Any]:
        return self.get_json(
            "/1.1/dm/inbox_initial_state.json",
            params={
                **X_COMMON_PARAMS,
                "nsfw_filtering_enabled": "false",
                "filter_low_quality": "true",
                "include_quality": "all",
                "dm_secret_conversations_enabled": "false",
                "krs_registration_enabled": "true",
            },
            referer="https://x.com/messages",
        )


def x_mentions_unread_sort_index(payload: dict[str, Any]) -> int:
    instructions = payload.get("timeline", {}).get("instructions")
    if not isinstance(instructions, list):
        raise RuntimeError("X mentions response missing timeline instructions")

    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise RuntimeError("Invalid X timeline instruction")
        marker = instruction.get("markEntriesUnreadGreaterThanSortIndex")
        if marker is None:
            continue
        if not isinstance(marker, dict):
            raise RuntimeError("Invalid X unread sort marker")
        return parse_int(marker.get("sortIndex"), field_name="X unread sort index")

    raise RuntimeError("X mentions response missing unread sort marker")


def x_timeline_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    instructions = payload.get("timeline", {}).get("instructions")
    if not isinstance(instructions, list):
        raise RuntimeError("X response missing timeline instructions")

    entries: list[dict[str, Any]] = []
    for instruction in instructions:
        if not isinstance(instruction, dict):
            raise RuntimeError("Invalid X timeline instruction")
        raw_entries = instruction.get("addEntries", {}).get("entries", [])
        if not isinstance(raw_entries, list):
            raise RuntimeError("Invalid X addEntries payload")
        entries.extend(entry for entry in raw_entries if isinstance(entry, dict))
    return entries


def collect_x_mentions(
    client: XClient,
    state: dict[str, dict[str, ItemCursor]],
) -> list[SocialNotification]:
    payload = client.fetch_mentions()
    global_objects = payload.get("globalObjects")
    if not isinstance(global_objects, dict):
        raise RuntimeError("X mentions response missing globalObjects")
    tweets = global_objects.get("tweets")
    users = global_objects.get("users")
    if not isinstance(tweets, dict) or not isinstance(users, dict):
        raise RuntimeError("X mentions response missing tweets/users")

    unread_sort_index = x_mentions_unread_sort_index(payload)
    previous_cursor = state["x_reply"].get("mentions")
    records: list[tuple[ItemCursor, str, str, str]] = []

    for entry in x_timeline_entries(payload):
        item = (entry.get("content") or {}).get("item") or {}
        item_content = item.get("content") or {}
        tweet_ref = item_content.get("tweet") or {}
        if not isinstance(tweet_ref, dict) or not tweet_ref.get("id"):
            continue

        element = (item.get("clientEventInfo") or {}).get("element")
        kind = X_MENTION_ELEMENTS.get(element)
        if kind is None:
            continue

        tweet_id = require_non_empty_string(tweet_ref.get("id"), field_name="tweet id")
        tweet = tweets.get(tweet_id)
        if not isinstance(tweet, dict):
            raise RuntimeError(f"X tweet {tweet_id} missing from globalObjects")

        sender_id = require_non_empty_string(
            tweet.get("user_id_str"),
            field_name=f"sender id for tweet {tweet_id}",
        )
        sender = users.get(sender_id)
        if not isinstance(sender, dict):
            raise RuntimeError(f"X user {sender_id} missing from globalObjects")

        sender_name = require_non_empty_string(
            sender.get("name") or sender.get("screen_name"),
            field_name=f"sender name for tweet {tweet_id}",
        )
        screen_name = require_non_empty_string(
            sender.get("screen_name"),
            field_name=f"screen name for tweet {tweet_id}",
        )
        sort_index = parse_int(entry.get("sortIndex"), field_name="X sortIndex")
        cursor = ItemCursor("mentions", sort_index, tweet_id)
        url = f"https://x.com/{screen_name}/status/{tweet_id}"
        records.append((cursor, kind, sender_name, url))

    notifications: list[SocialNotification] = []
    for cursor, kind, sender_name, url in sorted(
        records, key=lambda record: cursor_sort_key(record[0])
    ):
        tweet = tweets[cursor.item_id]
        update_state_cursor(state, "x_reply", cursor)
        if not is_newer_than_cursor(cursor, previous_cursor):
            continue
        if cursor.timestamp_ms <= unread_sort_index:
            continue

        notifications.append(
            SocialNotification(
                source="X",
                kind=kind,
                label=format_notification_label(
                    sender_name=sender_name,
                    raw_text=tweet.get("full_text"),
                ),
                url=url,
                cursor=cursor,
            )
        )

    return notifications


def x_user_name(users: dict[str, Any], user_id: str) -> str:
    user = users.get(user_id)
    if not isinstance(user, dict):
        return f"user {user_id}"
    return require_non_empty_string(
        user.get("name") or user.get("screen_name") or f"user {user_id}",
        field_name=f"X user name for {user_id}",
    )


def x_conversation_name(
    conversation: dict[str, Any],
    users: dict[str, Any],
    *,
    self_user_id: str,
) -> str | None:
    if conversation.get("type") == "ONE_TO_ONE":
        return None

    title = conversation.get("name")
    if isinstance(title, str) and title.strip():
        return title.strip()

    participants = conversation.get("participants")
    if not isinstance(participants, list):
        return None
    names = [
        x_user_name(users, str(participant.get("user_id")))
        for participant in participants
        if isinstance(participant, dict)
        and str(participant.get("user_id")) != self_user_id
    ]
    return ", ".join(names) if names else None


def collect_x_dms(
    client: XClient,
    state: dict[str, dict[str, ItemCursor]],
) -> list[SocialNotification]:
    payload = client.fetch_dm_inbox()
    inbox_state = payload.get("inbox_initial_state")
    if not isinstance(inbox_state, dict):
        raise RuntimeError("X DM response missing inbox_initial_state")

    conversations = inbox_state.get("conversations")
    users = inbox_state.get("users")
    entries = inbox_state.get("entries")
    if not isinstance(conversations, dict) or not isinstance(users, dict):
        raise RuntimeError("X DM response missing conversations/users")
    if not isinstance(entries, list):
        raise RuntimeError("X DM response missing entries")

    records: list[tuple[ItemCursor, str, str | None, str, str, int]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Invalid X DM entry")
        message = entry.get("message")
        if not isinstance(message, dict):
            continue

        conversation_id = require_non_empty_string(
            message.get("conversation_id"),
            field_name="X DM conversation id",
        )
        conversation = conversations.get(conversation_id)
        if not isinstance(conversation, dict):
            raise RuntimeError(f"X conversation {conversation_id} missing")

        if conversation.get("trusted") is not True:
            continue
        if (
            conversation.get("muted") is True
            or conversation.get("notifications_disabled") is True
        ):
            continue

        message_data = message.get("message_data")
        if not isinstance(message_data, dict):
            raise RuntimeError(f"X DM {message.get('id')} missing message_data")
        sender_id = require_non_empty_string(
            message_data.get("sender_id"),
            field_name=f"sender id for X DM {message.get('id')}",
        )
        if sender_id == client.self_user_id:
            continue

        message_id = require_non_empty_string(message.get("id"), field_name="X DM id")
        message_timestamp_ms = parse_int(message.get("time"), field_name="X DM time")
        last_read_event_id = parse_int(
            conversation.get("last_read_event_id") or 0,
            field_name=f"last_read_event_id for X conversation {conversation_id}",
        )
        sender_name = x_user_name(users, sender_id)
        conversation_name = x_conversation_name(
            conversation,
            users,
            self_user_id=client.self_user_id,
        )
        cursor = ItemCursor(conversation_id, message_timestamp_ms, message_id)
        url = f"https://x.com/messages/{conversation_id}"
        records.append(
            (
                cursor,
                sender_name,
                conversation_name,
                str(message_data.get("text") or ""),
                url,
                last_read_event_id,
            )
        )

    notifications: list[SocialNotification] = []
    for (
        cursor,
        sender_name,
        conversation_name,
        raw_text,
        url,
        last_read_event_id,
    ) in sorted(records, key=lambda record: cursor_sort_key(record[0])):
        previous_cursor = state["x_dm"].get(cursor.record_key)
        update_state_cursor(state, "x_dm", cursor)
        if not is_newer_than_cursor(cursor, previous_cursor):
            continue
        if parse_int(cursor.item_id, field_name="X DM cursor id") <= last_read_event_id:
            continue

        notifications.append(
            SocialNotification(
                source="X",
                kind="dm",
                label=format_notification_label(
                    sender_name=sender_name,
                    raw_text=raw_text,
                    conversation_name=conversation_name,
                ),
                url=url,
                cursor=cursor,
            )
        )

    return notifications


def collect_x_notifications(
    state: dict[str, dict[str, ItemCursor]],
) -> list[SocialNotification]:
    client = XClient.from_brave()
    return collect_x_mentions(client, state) + collect_x_dms(client, state)
