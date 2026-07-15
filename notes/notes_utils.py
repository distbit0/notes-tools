from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger


def terminal_safe_markdown_path(path: Path) -> Path:
    has_whitespace = any(character.isspace() for character in path.name)
    if path.suffix.lower() != ".md" or not has_whitespace:
        return path

    collapsed_stem = "-".join(path.stem.split())
    return path.with_name(f"{collapsed_stem}{path.suffix.lower()}")


def ensure_terminal_safe_markdown_path(path: Path) -> None:
    safe_path = terminal_safe_markdown_path(path)
    if safe_path != path:
        raise ValueError(
            f"Markdown note path contains whitespace: {path}. Use {safe_path} instead."
        )


def configure_logger(log_path: Path) -> None:
    logger.remove()
    logger.add(sys.stdout, level="INFO", diagnose=False)
    logger.add(
        log_path,
        level="DEBUG",
        rotation="100 KB",
        retention=5,
        encoding="utf-8",
        diagnose=False,
    )


def trim_trailing_blank_lines(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    path.write_text("\n".join(lines), encoding="utf-8")


def append_markdown_lines(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    ensure_terminal_safe_markdown_path(path)
    if not path.exists():
        raise FileNotFoundError(f"Notes file not found: {path}")

    trim_trailing_blank_lines(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n")
        handle.write("\n".join(lines))
        handle.write("\n\n")


def normalize_notification_name(name: str, *, field_name: str) -> str:
    normalized_name = " ".join(name.split()).strip()
    if not normalized_name:
        raise RuntimeError(f"Notification {field_name} is empty")
    return normalized_name


def escape_markdown_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def markdown_link(label: str, url: str) -> str:
    return f"[{escape_markdown_label(label)}]({url})"


def format_notification_note_line(
    *,
    source: str,
    label: str,
    url: str | None = None,
) -> str:
    normalized_source = normalize_notification_name(source, field_name="source")
    normalized_label = normalize_notification_name(label, field_name="label")
    body = markdown_link(normalized_label, url) if url else normalized_label
    return f"new notif: {normalized_source}: {body}"


def collapse_notification_text(raw_text: str | None, *, max_length: int = 120) -> str:
    if not raw_text or not raw_text.strip():
        return "<media>"

    collapsed = " ".join(raw_text.split())
    if len(collapsed) <= max_length:
        return collapsed
    return f"{collapsed[: max_length - 3].rstrip()}..."


def format_notification_label(
    *,
    sender_name: str,
    raw_text: str | None,
    conversation_name: str | None = None,
    is_group_mention: bool = False,
) -> str:
    normalized_sender_name = normalize_notification_name(
        sender_name, field_name="sender name"
    )
    preview = collapse_notification_text(raw_text)
    if is_group_mention:
        preview = f"@{preview}"

    if conversation_name is None:
        return f"{normalized_sender_name}: {preview}"

    normalized_conversation_name = normalize_notification_name(
        conversation_name,
        field_name="conversation name",
    )
    return f"{normalized_conversation_name} | {normalized_sender_name}: {preview}"


def send_persistent_desktop_notification(
    *,
    app_name: str,
    summary: str,
    body: str = "",
    category: str | None = None,
    on_click_url: str | None = None,
) -> None:
    if not shutil.which("notify-send"):
        raise RuntimeError("notify-send not found in PATH")

    notification_body = body.strip()
    if on_click_url:
        # Dunst's `open_url` click action uses the notification's parsed URLs.
        # The local rules only render the summary, so keep the click target hidden
        # in the body instead of showing a raw URL in the visible notification text.
        notification_body = on_click_url

    args = [
        "notify-send",
        "--app-name",
        app_name,
        "--urgency",
        "critical",
        "--expire-time",
        "0",
    ]
    if category:
        args.extend(["--hint", f"string:category:{category}"])
    args.append(summary)
    if notification_body:
        args.append(notification_body)

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"notify-send failed for {app_name}: {error or 'unknown error'}"
        )
