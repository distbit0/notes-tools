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
    parse_iso_timestamp_ms,
    request_json,
    require_non_empty_string,
    update_state_cursor,
)


LESSWRONG_BASE_URL = "https://www.lesswrong.com"
LESSWRONG_GRAPHQL_URL = f"{LESSWRONG_BASE_URL}/graphql"


@dataclass(frozen=True)
class LessWrongClient:
    session: requests.Session
    cookie_header: str

    @classmethod
    def from_brave(cls) -> "LessWrongClient":
        cookies = load_brave_cookies(("lesswrong.com",))
        return cls(session=requests.Session(), cookie_header=cookies.header)

    def graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = request_json(
            self.session,
            "POST",
            LESSWRONG_GRAPHQL_URL,
            headers={
                "Cookie": self.cookie_header,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": LESSWRONG_BASE_URL,
            },
            json_payload={"query": query, "variables": variables or {}},
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected LessWrong GraphQL response")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("LessWrong GraphQL response missing data")
        return data

    def current_user_id(self) -> str:
        data = self.graphql("query { currentUser { _id } }")
        user = data.get("currentUser")
        if not isinstance(user, dict):
            raise RuntimeError("LessWrong currentUser is missing")
        return require_non_empty_string(user.get("_id"), field_name="LessWrong user id")

    def unread_notifications(self, user_id: str) -> list[dict[str, Any]]:
        query = """
        query($userId: String!) {
          notifications(
            selector: { unreadUserNotifications: { userId: $userId } }
            limit: 50
          ) {
            results {
              _id
              createdAt
              link
              title
              message
              type
              viewed
              deleted
            }
          }
        }
        """.strip()
        data = self.graphql(query, {"userId": user_id})
        notifications = data.get("notifications")
        if not isinstance(notifications, dict) or not isinstance(
            notifications.get("results"), list
        ):
            raise RuntimeError("LessWrong notifications response is invalid")
        return [
            notification
            for notification in notifications["results"]
            if isinstance(notification, dict)
        ]

    def conversations(self, user_id: str) -> list[dict[str, Any]]:
        query = """
        query($userId: String!) {
          conversations(
            selector: { userConversations: { userId: $userId, showArchive: false } }
            limit: 50
          ) {
            results {
              _id
              title
              participantIds
              participants { _id displayName username slug }
              latestActivity
              messageCount
              hasUnreadMessages
              latestMessage {
                _id
                createdAt
                userId
                contents { html }
                contents_latest
                user { _id displayName username slug }
              }
            }
          }
        }
        """.strip()
        data = self.graphql(query, {"userId": user_id})
        conversations = data.get("conversations")
        if not isinstance(conversations, dict) or not isinstance(
            conversations.get("results"), list
        ):
            raise RuntimeError("LessWrong conversations response is invalid")
        return [
            conversation
            for conversation in conversations["results"]
            if isinstance(conversation, dict)
        ]


def lesswrong_absolute_url(link: Any) -> str:
    raw_link = require_non_empty_string(link, field_name="LessWrong link")
    return urljoin(LESSWRONG_BASE_URL, raw_link)


def collect_lesswrong_notifications(
    client: LessWrongClient,
    state: dict[str, dict[str, ItemCursor]],
) -> list[SocialNotification]:
    user_id = client.current_user_id()
    records: list[tuple[ItemCursor, str, str]] = []
    for notification in client.unread_notifications(user_id):
        if notification.get("deleted") is True:
            continue
        notification_id = require_non_empty_string(
            notification.get("_id"),
            field_name="LessWrong notification id",
        )
        label = require_non_empty_string(
            notification.get("message") or notification.get("title"),
            field_name=f"LessWrong notification label {notification_id}",
        )
        cursor = ItemCursor(
            "notifications",
            parse_iso_timestamp_ms(
                notification.get("createdAt"),
                field_name=f"LessWrong notification createdAt {notification_id}",
            ),
            notification_id,
        )
        records.append((cursor, label, lesswrong_absolute_url(notification.get("link"))))

    notifications: list[SocialNotification] = []
    for cursor, label, url in sorted(
        records, key=lambda record: cursor_sort_key(record[0])
    ):
        previous_cursor = state["lesswrong_notification"].get(cursor.record_key)
        update_state_cursor(state, "lesswrong_notification", cursor)
        if not is_newer_than_cursor(cursor, previous_cursor):
            continue
        notifications.append(
            SocialNotification(
                source="LessWrong",
                kind="notification",
                label=label,
                url=url,
                cursor=cursor,
            )
        )

    return notifications


def lesswrong_user_name(user: Any, fallback_user_id: str) -> str:
    if not isinstance(user, dict):
        return f"user {fallback_user_id}"
    return require_non_empty_string(
        user.get("displayName") or user.get("username") or f"user {fallback_user_id}",
        field_name=f"LessWrong user name {fallback_user_id}",
    )


def lesswrong_conversation_name(
    conversation: dict[str, Any],
    *,
    self_user_id: str,
) -> str | None:
    title = conversation.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()

    participants = conversation.get("participants")
    if not isinstance(participants, list):
        return None
    names = [
        lesswrong_user_name(participant, str(participant.get("_id")))
        for participant in participants
        if isinstance(participant, dict) and participant.get("_id") != self_user_id
    ]
    if len(names) <= 1:
        return None
    return ", ".join(names)


def collect_lesswrong_dms(
    client: LessWrongClient,
    state: dict[str, dict[str, ItemCursor]],
) -> list[SocialNotification]:
    user_id = client.current_user_id()
    records: list[tuple[ItemCursor, bool, str, str | None, str | None, str]] = []

    for conversation in client.conversations(user_id):
        conversation_id = require_non_empty_string(
            conversation.get("_id"),
            field_name="LessWrong conversation id",
        )
        latest_message = conversation.get("latestMessage")
        if not isinstance(latest_message, dict):
            continue
        message_id = require_non_empty_string(
            latest_message.get("_id"),
            field_name=f"LessWrong latest message id {conversation_id}",
        )
        message_user_id = require_non_empty_string(
            latest_message.get("userId"),
            field_name=f"LessWrong latest message userId {message_id}",
        )
        cursor = ItemCursor(
            conversation_id,
            parse_iso_timestamp_ms(
                latest_message.get("createdAt"),
                field_name=f"LessWrong latest message createdAt {message_id}",
            ),
            message_id,
        )
        contents = latest_message.get("contents")
        html = contents.get("html") if isinstance(contents, dict) else None
        records.append(
            (
                cursor,
                bool(conversation.get("hasUnreadMessages")),
                lesswrong_user_name(latest_message.get("user"), message_user_id),
                lesswrong_conversation_name(conversation, self_user_id=user_id),
                html_to_text(html) or latest_message.get("contents_latest"),
                f"{LESSWRONG_BASE_URL}/inbox?conversation={conversation_id}",
            )
        )

    notifications: list[SocialNotification] = []
    for (
        cursor,
        has_unread_messages,
        sender_name,
        conversation_name,
        raw_text,
        url,
    ) in sorted(records, key=lambda record: cursor_sort_key(record[0])):
        previous_cursor = state["lesswrong_dm"].get(cursor.record_key)
        update_state_cursor(state, "lesswrong_dm", cursor)
        if not is_newer_than_cursor(cursor, previous_cursor):
            continue
        if not has_unread_messages:
            continue

        notifications.append(
            SocialNotification(
                source="LessWrong",
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


def collect_lesswrong_all(
    state: dict[str, dict[str, ItemCursor]],
) -> list[SocialNotification]:
    client = LessWrongClient.from_brave()
    return collect_lesswrong_notifications(client, state) + collect_lesswrong_dms(
        client,
        state,
    )
