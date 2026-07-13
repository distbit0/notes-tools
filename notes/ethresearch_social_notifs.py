from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from notes_utils import format_notification_label
from social_notif_common import (
    ItemCursor,
    SocialNotification,
    USER_AGENT,
    cursor_sort_key,
    html_to_text,
    is_newer_than_cursor,
    load_brave_cookies,
    parse_int,
    parse_iso_timestamp_ms,
    request_json,
    require_non_empty_string,
    update_state_cursor,
)


ETHRESEARCH_BASE_URL = "https://ethresear.ch"
ETHRESEARCH_NOTIFICATION_PATH = "/notifications?limit=60&offset=0"

TOPIC_NOTIFICATION_ACTIONS = {
    1: "mentioned you",
    2: "replied",
    3: "quoted you",
    4: "edited",
    5: "liked",
    6: "sent a private message",
    7: "invited you to a private message",
    9: "posted",
    10: "moved a post",
    11: "linked",
    13: "invited you",
    15: "mentioned your group",
    16: "messaged your group",
    19: "liked",
    20: "approved a post",
    21: "approved a commit",
    24: "set a bookmark reminder",
    25: "reacted",
    35: "commented",
    39: "linked",
    800: "followed",
    801: "created a followed topic",
    802: "replied to a followed topic",
    900: "added activity",
}

PRIVATE_MESSAGE_NOTIFICATION_TYPES = {6, 7, 16}


@dataclass(frozen=True)
class EthResearchClient:
    session: requests.Session
    cookie_header: str

    @classmethod
    def from_brave(cls) -> "EthResearchClient":
        cookies = load_brave_cookies(("ethresear.ch",))
        return cls(session=requests.Session(), cookie_header=cookies.header)

    def headers(self, referer: str = ETHRESEARCH_BASE_URL) -> dict[str, str]:
        return {
            "Cookie": self.cookie_header,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": referer,
        }

    def get_json(self, path: str) -> dict[str, Any]:
        payload = request_json(
            self.session,
            "GET",
            urljoin(ETHRESEARCH_BASE_URL, path),
            headers=self.headers(),
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected EthResearch response for {path}")
        return payload

    def current_username(self) -> str:
        payload = self.get_json("/session/current.json")
        current_user = payload.get("current_user")
        if not isinstance(current_user, dict):
            raise RuntimeError("EthResearch current user is missing")
        return require_non_empty_string(
            current_user.get("username"),
            field_name="EthResearch username",
        )

    def notifications(self) -> list[dict[str, Any]]:
        path = ETHRESEARCH_NOTIFICATION_PATH
        seen_paths: set[str] = set()
        records: list[dict[str, Any]] = []

        while path:
            if path in seen_paths:
                raise RuntimeError(f"EthResearch notification pagination loop: {path}")
            seen_paths.add(path)

            payload = self.get_json(path)
            page_notifications = payload.get("notifications")
            if not isinstance(page_notifications, list):
                raise RuntimeError("EthResearch notifications response is invalid")
            records.extend(
                notification
                for notification in page_notifications
                if isinstance(notification, dict)
            )

            total_rows = payload.get("total_rows_notifications")
            if isinstance(total_rows, int) and len(records) >= total_rows:
                break
            if not page_notifications:
                break

            load_more_path = payload.get("load_more_notifications")
            if not isinstance(load_more_path, str) or not load_more_path.strip():
                break
            path = load_more_path

        return records

    def private_message_topics(self, username: str) -> list[dict[str, Any]]:
        payload = self.get_json(f"/topics/private-messages/{username}.json")
        topic_list = payload.get("topic_list")
        if not isinstance(topic_list, dict) or not isinstance(
            topic_list.get("topics"),
            list,
        ):
            raise RuntimeError("EthResearch private messages response is invalid")
        return [topic for topic in topic_list["topics"] if isinstance(topic, dict)]


def topic_title_from_payload(payload: dict[str, Any], data: dict[str, Any]) -> str:
    for value in (
        payload.get("fancy_title"),
        data.get("topic_title"),
        payload.get("title"),
    ):
        if isinstance(value, str) and value.strip():
            return html_to_text(value) or value.strip()
    raise RuntimeError("EthResearch topic title is missing")


def notification_actor_name(data: dict[str, Any]) -> str | None:
    actor_name = (
        data.get("display_username")
        or data.get("display_name")
        or data.get("original_username")
        or data.get("username")
    )
    if not isinstance(actor_name, str) or not actor_name.strip():
        return None
    return actor_name.strip()


def topic_url_from_parts(topic_id: int, slug: str, post_number: int) -> str:
    return f"{ETHRESEARCH_BASE_URL}/t/{slug}/{topic_id}/{post_number}"


def notification_metadata(
    notification: dict[str, Any],
) -> tuple[ItemCursor, bool, int, str]:
    notification_id = str(
        parse_int(notification.get("id"), field_name="EthResearch notification id")
    )
    notification_type = parse_int(
        notification.get("notification_type"),
        field_name=f"EthResearch notification {notification_id} type",
    )
    read_flag = notification.get("read")
    if not isinstance(read_flag, bool):
        raise RuntimeError(f"EthResearch notification {notification_id} read is invalid")
    cursor = ItemCursor(
        "notifications",
        parse_iso_timestamp_ms(
            notification.get("created_at"),
            field_name=f"EthResearch notification {notification_id} created_at",
        ),
        notification_id,
    )
    return cursor, read_flag, notification_type, notification_id


def topic_notification_label_url_topic_id(
    notification: dict[str, Any],
    data: dict[str, Any],
    *,
    notification_id: str,
    notification_type: int,
) -> tuple[str, str, str | None]:
    action = TOPIC_NOTIFICATION_ACTIONS.get(notification_type)
    if action is None:
        raise unsupported_unread_notification(notification_id, notification_type, data)

    topic_id = parse_int(
        notification.get("topic_id"),
        field_name=f"EthResearch notification {notification_id} topic_id",
    )
    slug = require_non_empty_string(
        notification.get("slug"),
        field_name=f"EthResearch notification {notification_id} slug",
    )
    post_number = parse_int(
        notification.get("post_number"),
        field_name=f"EthResearch notification {notification_id} post_number",
    )
    title = topic_title_from_payload(notification, data)
    actor_name = notification_actor_name(data)
    label = f"{actor_name} {action}: {title}" if actor_name else f"{action}: {title}"
    pm_topic_id = (
        str(topic_id)
        if notification_type in PRIVATE_MESSAGE_NOTIFICATION_TYPES
        else None
    )
    return label, topic_url_from_parts(topic_id, slug, post_number), pm_topic_id


def badge_notification_label_url(
    data: dict[str, Any],
    notification_id: str,
) -> tuple[str, str]:
    badge_id = parse_int(
        data.get("badge_id"),
        field_name=f"EthResearch notification {notification_id} badge_id",
    )
    badge_slug = require_non_empty_string(
        data.get("badge_slug"),
        field_name=f"EthResearch notification {notification_id} badge_slug",
    )
    badge_name = require_non_empty_string(
        data.get("badge_name") or data.get("badge_title"),
        field_name=f"EthResearch notification {notification_id} badge_name",
    )
    return (
        f"Earned badge: {badge_name}",
        f"{ETHRESEARCH_BASE_URL}/badges/{badge_id}/{badge_slug}",
    )


def unsupported_unread_notification(
    notification_id: str,
    notification_type: int,
    data: dict[str, Any],
) -> RuntimeError:
    return RuntimeError(
        "Unsupported EthResearch unread notification "
        f"{notification_id} type {notification_type} with data keys {sorted(data)}"
    )


def unread_notification_label_url_topic_id(
    notification: dict[str, Any],
    *,
    notification_id: str,
    notification_type: int,
) -> tuple[str, str, str | None]:
    data = notification.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"EthResearch notification {notification_id} data is invalid")
    if notification_type == 12:
        label, url = badge_notification_label_url(data, notification_id)
        return label, url, None
    if not all(
        notification.get(field) is not None
        for field in ("topic_id", "slug", "post_number")
    ):
        raise unsupported_unread_notification(notification_id, notification_type, data)
    return topic_notification_label_url_topic_id(
        notification,
        data,
        notification_id=notification_id,
        notification_type=notification_type,
    )


def collect_ethresearch_notifications(
    client: EthResearchClient,
    state: dict[str, dict[str, ItemCursor]],
) -> tuple[list[SocialNotification], set[str]]:
    records: list[tuple[ItemCursor, bool, int, str, dict[str, Any]]] = []
    for notification in client.notifications():
        cursor, read_flag, notification_type, notification_id = notification_metadata(
            notification
        )
        records.append(
            (cursor, read_flag, notification_type, notification_id, notification)
        )

    notifications: list[SocialNotification] = []
    private_message_topic_ids: set[str] = set()
    previous_cursor = state["ethresearch_notification"].get("notifications")
    for cursor, read_flag, notification_type, notification_id, notification in sorted(
        records,
        key=lambda record: cursor_sort_key(record[0]),
    ):
        update_state_cursor(state, "ethresearch_notification", cursor)
        if read_flag or not is_newer_than_cursor(cursor, previous_cursor):
            continue

        label, url, pm_topic_id = unread_notification_label_url_topic_id(
            notification,
            notification_id=notification_id,
            notification_type=notification_type,
        )
        if pm_topic_id is not None:
            private_message_topic_ids.add(pm_topic_id)
        notifications.append(
            SocialNotification(
                source="EthResearch",
                kind="private_message"
                if notification_type in PRIVATE_MESSAGE_NOTIFICATION_TYPES
                else "notification",
                label=label,
                url=url,
                cursor=cursor,
            )
        )

    return notifications, private_message_topic_ids


def has_unread_private_message(topic: dict[str, Any]) -> bool:
    for field_name in ("new_posts", "unread", "unread_posts"):
        value = topic.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True
    return topic.get("unseen") is True


def collect_ethresearch_private_messages(
    client: EthResearchClient,
    state: dict[str, dict[str, ItemCursor]],
    *,
    skip_topic_ids: set[str],
) -> list[SocialNotification]:
    username = client.current_username()
    records: list[tuple[ItemCursor, bool, str, str, str]] = []

    for topic in client.private_message_topics(username):
        topic_id = parse_int(topic.get("id"), field_name="EthResearch PM topic id")
        topic_key = str(topic_id)
        post_number = parse_int(
            topic.get("highest_post_number"),
            field_name=f"EthResearch private message {topic_key} highest_post_number",
        )
        cursor = ItemCursor(
            topic_key,
            parse_iso_timestamp_ms(
                topic.get("last_posted_at"),
                field_name=f"EthResearch private message {topic_key} last_posted_at",
            ),
            str(post_number),
        )
        title = topic_title_from_payload(topic, {})
        sender_name = require_non_empty_string(
            topic.get("last_poster_username"),
            field_name=f"EthResearch private message {topic_key} last_poster_username",
        )
        slug = require_non_empty_string(
            topic.get("slug"),
            field_name=f"EthResearch private message {topic_key} slug",
        )
        records.append(
            (
                cursor,
                has_unread_private_message(topic),
                sender_name,
                title,
                topic_url_from_parts(topic_id, slug, post_number),
            )
        )

    notifications: list[SocialNotification] = []
    for cursor, is_unread, sender_name, title, url in sorted(
        records,
        key=lambda record: cursor_sort_key(record[0]),
    ):
        previous_cursor = state["ethresearch_pm"].get(cursor.record_key)
        update_state_cursor(state, "ethresearch_pm", cursor)
        if not is_newer_than_cursor(cursor, previous_cursor):
            continue
        if not is_unread or cursor.record_key in skip_topic_ids:
            continue

        notifications.append(
            SocialNotification(
                source="EthResearch",
                kind="private_message",
                label=format_notification_label(
                    sender_name=sender_name,
                    raw_text=title,
                    conversation_name="Private message",
                ),
                url=url,
                cursor=cursor,
            )
        )

    return notifications


def collect_ethresearch_all(
    state: dict[str, dict[str, ItemCursor]],
) -> list[SocialNotification]:
    client = EthResearchClient.from_brave()
    notifications, private_message_topic_ids = collect_ethresearch_notifications(
        client,
        state,
    )
    return notifications + collect_ethresearch_private_messages(
        client,
        state,
        skip_topic_ids=private_message_topic_ids,
    )
