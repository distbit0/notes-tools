from __future__ import annotations

exit(0)

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from notes_utils import (
    append_markdown_lines,
    configure_logger,
    format_notification_note_line,
)


LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_TOKEN_ENV = "LINEAR_API_KEY"
NOTES_FILE = Path.home() / "notes/inbox-index.md"
LOG_PATH = Path(__file__).with_name("linear-notifs.log")

NOTIFICATIONS_QUERY = """
query Notifications($after: String) {
  notifications(first: 50, after: $after) {
    nodes {
      id
      readAt
      ... on IssueNotification {
        title
        subtitle
        url
        comment { url }
        parentComment { url }
      }
      ... on ProjectNotification {
        title
        subtitle
        url
        comment { url }
        parentComment { url }
      }
      ... on InitiativeNotification {
        title
        subtitle
        url
        comment { url }
        parentComment { url }
      }
      ... on DocumentNotification {
        title
        subtitle
        url
      }
      ... on PostNotification {
        title
        subtitle
        url
      }
      ... on PullRequestNotification {
        title
        subtitle
        url
      }
      ... on CustomerNotification {
        title
        subtitle
        url
      }
      ... on CustomerNeedNotification {
        title
        subtitle
        url
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
""".strip()

MARK_READ_MUTATION = """
mutation UpdateNotification($id: String!, $input: NotificationUpdateInput!) {
  notificationUpdate(id: $id, input: $input) {
    success
  }
}
""".strip()


def get_linear_token() -> str:
    token = os.environ.get(LINEAR_TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(f"{LINEAR_TOKEN_ENV} is not set")
    return token


def linear_graphql(query: str, variables: dict | None, token: str) -> dict:
    payload = {"query": query, "variables": variables or {}}
    request = urllib.request.Request(
        LINEAR_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Linear API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Linear API request failed: {exc}") from exc

    if "errors" in data:
        raise RuntimeError(f"Linear API returned errors: {data['errors']}")
    if "data" not in data:
        raise RuntimeError("Linear API response missing data field")
    return data["data"]


def fetch_unread_notifications(token: str) -> list[dict]:
    notifications: list[dict] = []
    cursor: str | None = None

    while True:
        variables = {"after": cursor}
        payload = linear_graphql(NOTIFICATIONS_QUERY, variables, token)
        connection = payload.get("notifications")
        if not isinstance(connection, dict):
            raise RuntimeError("Unexpected Linear notifications response")

        nodes = connection.get("nodes", [])
        if not isinstance(nodes, list):
            raise RuntimeError("Unexpected Linear notifications nodes")
        for node in nodes:
            if isinstance(node, dict) and node.get("readAt") is None:
                notifications.append(node)

        page_info = connection.get("pageInfo", {})
        if not isinstance(page_info, dict):
            raise RuntimeError("Unexpected Linear pageInfo response")
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    return notifications


def notification_label(notification: dict) -> str:
    title = (notification.get("title") or "").strip()
    if title:
        return title
    subtitle = (notification.get("subtitle") or "").strip()
    if subtitle:
        return subtitle
    raise RuntimeError("Notification missing title/subtitle")


def notification_url(notification: dict) -> str:
    comment = notification.get("comment") or {}
    comment_url = comment.get("url")
    if comment_url:
        return str(comment_url)

    parent_comment = notification.get("parentComment") or {}
    parent_url = parent_comment.get("url")
    if parent_url:
        return str(parent_url)

    url = notification.get("url")
    if url:
        return str(url)
    raise RuntimeError("Notification missing URL")


def mark_notification_read(notification_id: str, *, token: str, read_at: str) -> None:
    variables = {"id": notification_id, "input": {"readAt": read_at}}
    payload = linear_graphql(MARK_READ_MUTATION, variables, token)
    result = payload.get("notificationUpdate")
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError(f"Failed to mark notification {notification_id} as read")


def main() -> int:
    configure_logger(LOG_PATH)
    token = get_linear_token()

    notifications = fetch_unread_notifications(token)
    if not notifications:
        logger.info("No unread notifications")
        return 0

    read_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lines: list[str] = []
    for notification in notifications:
        label = notification_label(notification)
        url = notification_url(notification)
        lines.append(
            format_notification_note_line(source="linear", label=label, url=url)
        )
        logger.info(f"Added: {label}")
        notification_id = notification.get("id")
        if not notification_id:
            raise RuntimeError("Notification missing id")
        mark_notification_read(str(notification_id), token=token, read_at=read_at)

    append_markdown_lines(NOTES_FILE, lines)
    logger.info("Processed notifications")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
