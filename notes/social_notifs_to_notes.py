#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from loguru import logger

from ethresearch_social_notifs import collect_ethresearch_all
from lesswrong_social_notifs import collect_lesswrong_all
from notes_utils import (
    configure_logger,
    format_notification_note_line,
    replace_keyed_notification_lines,
    send_persistent_desktop_notification,
)
from social_notif_common import (
    ItemCursor,
    SocialNotification,
    cursor_sort_key,
    load_state,
    save_state,
)
from x_social_notifs import collect_x_notifications


NOTES_FILE = Path.home() / "notes/inbox-index.md"
LOG_PATH = Path(__file__).with_name("social-notifs.log")
STATE_PATH = Path.home() / ".local/state/social-notifs-state.json"
SOCIAL_NOTIFICATION_LINE_RE = re.compile(
    r"\s*(?:[-*+]\s+)?new notif: "
    r"(?P<source>x|lesswrong|ethresearch): ",
    re.IGNORECASE,
)
MARKDOWN_URL_RE = re.compile(r"\]\((?P<url>https?://[^)\s]+)\)")


def social_conversation_identity(source: str, conversation_id: str) -> str:
    return f"social-conversation:{source.lower()}:{conversation_id}"


def conversation_id_from_url(source: str, url: str) -> str | None:
    parsed_url = urlparse(url)
    path_parts = parsed_url.path.strip("/").split("/")
    if source == "x" and parsed_url.netloc == "x.com":
        if len(path_parts) == 2 and path_parts[0] == "messages":
            return path_parts[1]
    elif source == "lesswrong" and parsed_url.netloc == "www.lesswrong.com":
        conversation_ids = parse_qs(parsed_url.query).get("conversation")
        if parsed_url.path == "/inbox" and conversation_ids:
            return conversation_ids[0]
    elif source == "ethresearch" and parsed_url.netloc == "ethresear.ch":
        if len(path_parts) >= 3 and path_parts[0] == "t":
            return path_parts[2]
    return None


def legacy_social_notification_identity(line: str) -> str | None:
    line_match = SOCIAL_NOTIFICATION_LINE_RE.match(line)
    url_match = MARKDOWN_URL_RE.search(line)
    if line_match is None or url_match is None:
        return None

    source = line_match.group("source").lower()
    conversation_id = conversation_id_from_url(source, url_match.group("url"))
    if conversation_id is None:
        return None
    return social_conversation_identity(source, conversation_id)


def social_notification_identity(notification: SocialNotification) -> str:
    if notification.conversation_id is not None:
        return social_conversation_identity(
            notification.source, notification.conversation_id
        )
    return (
        f"social-event:{notification.source.lower()}:{notification.kind}:"
        f"{notification.cursor.record_key}:{notification.cursor.item_id}"
    )


def save_social_notifications(
    notes_file: Path, notifications: list[SocialNotification]
) -> list[str]:
    lines_by_identity: dict[str, str] = {}
    for notification in notifications:
        identity = social_notification_identity(notification)
        lines_by_identity.pop(identity, None)
        lines_by_identity[identity] = format_notification_note_line(
            source=notification.source.lower(),
            label=notification.label,
            url=notification.url,
        )
    return replace_keyed_notification_lines(
        notes_file,
        lines_by_identity,
        legacy_identity_for_line=legacy_social_notification_identity,
    )


def collect_source_notifications(
    state: dict[str, dict[str, ItemCursor]],
) -> tuple[list[SocialNotification], list[str]]:
    notifications: list[SocialNotification] = []
    errors: list[str] = []

    collectors = (
        ("X", lambda: collect_x_notifications(state)),
        ("LessWrong", lambda: collect_lesswrong_all(state)),
        ("EthResearch", lambda: collect_ethresearch_all(state)),
    )

    for source_name, collect in collectors:
        try:
            source_notifications = collect()
        except Exception as exc:
            logger.exception(f"{source_name} collection failed")
            errors.append(f"{source_name}: {exc}")
            continue
        notifications.extend(source_notifications)

    notifications.sort(key=lambda notification: cursor_sort_key(notification.cursor))
    return notifications, errors


def run() -> int:
    configure_logger(LOG_PATH)
    state = load_state(STATE_PATH)
    notifications, errors = collect_source_notifications(state)

    if notifications:
        for notification in notifications:
            logger.info(
                f"Added {notification.source} {notification.kind}: {notification.label}"
            )
            send_persistent_desktop_notification(
                app_name=notification.source,
                summary=notification.label,
                category=notification.source.lower(),
                on_click_url=notification.url,
            )
        save_social_notifications(NOTES_FILE, notifications)
        logger.info(f"Processed {len(notifications)} social notifications")
    else:
        logger.info("No new X, LessWrong, or EthResearch notifications")

    save_state(STATE_PATH, state)

    if errors:
        raise RuntimeError("; ".join(errors))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
