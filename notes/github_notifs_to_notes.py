from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urldefrag

from loguru import logger

from notes_utils import (
    collapse_notification_text,
    configure_logger,
    format_notification_note_line,
    replace_keyed_notification_lines,
    send_persistent_desktop_notification,
)


GITHUB_API_PREFIX = "https://api.github.com"
NOTES_FILE = Path.home() / "notes/inbox-index.md"
LOG_PATH = Path(__file__).with_name("github-notifs.log")
GITHUB_NOTIFICATION_LINE_RE = re.compile(
    r"\s*(?:[-*+]\s+)?new notif: github: ", re.IGNORECASE
)
MARKDOWN_URL_RE = re.compile(r"\]\((?P<url>https?://[^)\s]+)\)")
GITHUB_CONVERSATION_URL_RE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/"
    r"(?:issues|pull|discussions|commit)/[^/]+"
)


@dataclass(frozen=True)
class GithubNotification:
    thread_id: str
    title: str
    subject_url: str
    latest_comment_url: str | None


def ensure_gh_available() -> None:
    if not shutil.which("gh"):
        raise RuntimeError("gh CLI not found in PATH")


def normalize_api_endpoint(api_url: str) -> str:
    if api_url.startswith(GITHUB_API_PREFIX):
        return api_url[len(GITHUB_API_PREFIX) :]
    return api_url


def looks_like_comment_api_url(api_url: str) -> bool:
    if not api_url:
        return False
    comment_markers = (
        "/comments/",
        "/pulls/comments/",
        "/issues/comments/",
        "/reviews/",
    )
    return any(marker in api_url for marker in comment_markers)


def choose_target_api_url(subject_url: str, latest_comment_url: str | None) -> str:
    if latest_comment_url:
        return latest_comment_url
    if looks_like_comment_api_url(subject_url):
        return subject_url
    return subject_url


def run_gh_api(endpoint: str, *, method: str = "GET") -> str:
    command = ["gh", "api", endpoint]
    if method != "GET":
        command.extend(["-X", method])

    env = os.environ.copy()
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"gh api failed for {endpoint}: {error or 'unknown error'}")
    return result.stdout


def gh_api_json(endpoint: str) -> list[dict] | dict:
    raw = run_gh_api(endpoint)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from gh api {endpoint}: {exc}") from exc


def fetch_unread_notifications() -> list[GithubNotification]:
    payload = gh_api_json("/notifications")
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected GitHub notifications response")

    notifications: list[GithubNotification] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("unread"):
            continue
        thread_id = item.get("id")
        subject = item.get("subject") or {}
        title = subject.get("title")
        subject_url = subject.get("url")
        latest_comment_url = subject.get("latest_comment_url")

        if not thread_id or not title or not subject_url:
            raise RuntimeError("Missing notification fields from GitHub API")

        notifications.append(
            GithubNotification(
                thread_id=str(thread_id),
                title=str(title),
                subject_url=str(subject_url),
                latest_comment_url=str(latest_comment_url)
                if latest_comment_url
                else None,
            )
        )

    return notifications


def fetch_html_url(api_url: str) -> str:
    endpoint = normalize_api_endpoint(api_url)
    payload = gh_api_json(endpoint)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected GitHub API response for {api_url}")
    html_url = payload.get("html_url")
    if not html_url:
        raise RuntimeError(f"Missing html_url for {api_url}")
    return str(html_url)


def mark_thread_read(thread_id: str) -> None:
    run_gh_api(f"/notifications/threads/{thread_id}", method="PATCH")


def canonical_github_thread_url(url: str) -> str:
    url_without_fragment = urldefrag(url).url.rstrip("/")
    conversation_match = GITHUB_CONVERSATION_URL_RE.match(url_without_fragment)
    if conversation_match:
        return conversation_match.group(0)
    return url_without_fragment


def github_notification_identity(url: str) -> str:
    return f"github-thread:{canonical_github_thread_url(url)}"


def legacy_github_notification_identity(line: str) -> str | None:
    if GITHUB_NOTIFICATION_LINE_RE.match(line) is None:
        return None
    url_match = MARKDOWN_URL_RE.search(line)
    if url_match is None:
        return None
    return github_notification_identity(url_match.group("url"))


def main() -> int:
    configure_logger(LOG_PATH)
    ensure_gh_available()

    notifications = fetch_unread_notifications()
    if not notifications:
        logger.info("No unread notifications")
        return 0

    lines_by_identity: dict[str, str] = {}
    for notification in notifications:
        target_api_url = choose_target_api_url(
            notification.subject_url,
            notification.latest_comment_url,
        )
        html_url = fetch_html_url(target_api_url)
        identity = github_notification_identity(html_url)
        lines_by_identity.pop(identity, None)
        lines_by_identity[identity] = format_notification_note_line(
            source="github",
            label=notification.title,
            url=html_url,
        )
        logger.info(f"Added: {notification.title}")
        send_persistent_desktop_notification(
            app_name="GitHub",
            summary=collapse_notification_text(notification.title),
            category="github",
            on_click_url=html_url,
        )

    replace_keyed_notification_lines(
        NOTES_FILE,
        lines_by_identity,
        legacy_identity_for_line=legacy_github_notification_identity,
    )
    for notification in notifications:
        mark_thread_read(notification.thread_id)
    logger.info("Processed notifications")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
