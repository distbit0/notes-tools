#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from loguru import logger

from ethresearch_social_notifs import collect_ethresearch_all
from lesswrong_social_notifs import collect_lesswrong_all
from notes_utils import (
    append_markdown_lines,
    configure_logger,
    format_notification_note_line,
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
        append_markdown_lines(
            NOTES_FILE,
            [
                format_notification_note_line(
                    source=notification.source.lower(),
                    label=notification.label,
                    url=notification.url,
                )
                for notification in notifications
            ],
        )
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
