from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from notes_utils import append_markdown_lines, format_notification_note_line


THREAD_MARKER_RE = re.compile(
    r'<!-- msg-thread source="(?P<source>[^"]+)" '
    r'conversation_id="(?P<conversation_id>[^"]+)" -->'
)
WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
CHANGED_MESSAGE_NOTES_FILE_ENV = "MESSAGE_NOTIF_CHANGED_NOTES_FILE"


@dataclass(frozen=True)
class ReplyContext:
    sender_name: str | None
    raw_text: str | None
    timestamp_ms: int | None = None
    message_id: str | None = None
    url: str | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class MessageLogEntry:
    source: str
    kind: str
    label: str
    url: str | None
    conversation_id: str
    conversation_name: str
    sender_name: str
    message_id: str
    timestamp_ms: int
    raw_text: str | None
    reply: ReplyContext | None = None


def xml_attr(value: str) -> str:
    return html.escape(value, quote=True)


def normalize_title_part(value: str, *, fallback: str) -> str:
    title = " ".join(value.split())
    title = re.sub(r'[\[\]#|<>:"/\\?*\x00-\x1f]+', " ", title)
    title = " ".join(title.split()).strip(" .")
    return title or fallback


def source_title(source: str) -> str:
    return normalize_title_part(source, fallback="source").title()


def short_thread_hash(source: str, conversation_id: str) -> str:
    return hashlib.sha256(f"{source}\0{conversation_id}".encode("utf-8")).hexdigest()[
        :8
    ]


def message_note_title(entry: MessageLogEntry) -> str:
    conversation_name = normalize_title_part(
        entry.conversation_name, fallback=f"{entry.source} conversation"
    )
    return (
        f"msg - {source_title(entry.source)} - {conversation_name} - "
        f"{short_thread_hash(entry.source, entry.conversation_id)}"
    )


def message_note_path(notes_root: Path, entry: MessageLogEntry) -> Path:
    existing_path = find_existing_message_note(
        notes_root,
        source=entry.source,
        conversation_id=entry.conversation_id,
    )
    if existing_path is not None:
        return existing_path
    return notes_root / f"{message_note_title(entry)}.md"


def find_existing_message_note(
    notes_root: Path, *, source: str, conversation_id: str
) -> Path | None:
    for path in sorted(notes_root.glob("msg - *.md")):
        marker = THREAD_MARKER_RE.search(path.read_text(encoding="utf-8"))
        if marker is None:
            continue
        if (
            marker.group("source") == source
            and marker.group("conversation_id") == conversation_id
        ):
            return path
    return None


def write_text_atomic(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temp_file:
        temp_file.write(text)
        temp_path = Path(temp_file.name)
    temp_path.replace(path)


def ensure_message_note(path: Path, entry: MessageLogEntry) -> None:
    if path.exists():
        marker = THREAD_MARKER_RE.search(path.read_text(encoding="utf-8"))
        if marker is None:
            raise RuntimeError(f"Existing message note lacks thread marker: {path}")
        if (
            marker.group("source") != entry.source
            or marker.group("conversation_id") != entry.conversation_id
        ):
            raise RuntimeError(
                f"Existing message note has different thread marker: {path}"
            )
        return

    write_text_atomic(
        path,
        "\n".join(
            [
                f"# {path.stem}",
                "",
                (
                    f'<!-- msg-thread source="{xml_attr(entry.source)}" '
                    f'conversation_id="{xml_attr(entry.conversation_id)}" -->'
                ),
                "",
            ]
        ),
    )


def timestamp_label(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return "unknown time"
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().isoformat(
        timespec="seconds"
    )


def longest_backtick_run(text: str) -> int:
    return max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)


def fenced_block(text: str | None) -> str:
    body = text if text is not None and text != "" else "[No text content captured.]"
    fence = "`" * max(3, longest_backtick_run(body) + 1)
    return f"{fence}text\n{body}\n{fence}"


def reply_context_lines(reply: ReplyContext | None) -> list[str]:
    if reply is None:
        return []

    lines = ["", "Replying to:"]
    if reply.unavailable_reason:
        lines.append(f"- Status: {reply.unavailable_reason}")
    if reply.sender_name:
        lines.append(f"- From: {reply.sender_name}")
    if reply.timestamp_ms is not None:
        lines.append(f"- Time: {timestamp_label(reply.timestamp_ms)}")
    if reply.message_id:
        lines.append(f"- Message ID: `{reply.message_id}`")
    if reply.url:
        lines.append(f"- Source: {reply.url}")
    if reply.raw_text is not None:
        lines.extend(["", fenced_block(reply.raw_text)])
    return lines


def message_marker(entry: MessageLogEntry) -> str:
    return (
        f'<!-- msg-message source="{xml_attr(entry.source)}" '
        f'conversation_id="{xml_attr(entry.conversation_id)}" '
        f'message_id="{xml_attr(entry.message_id)}" '
        f'timestamp_ms="{entry.timestamp_ms}" -->'
    )


def message_block(entry: MessageLogEntry) -> str:
    lines = [
        f"## Message {timestamp_label(entry.timestamp_ms)}",
        "",
        message_marker(entry),
        "",
        f"From: {entry.sender_name}",
        f"Kind: {entry.kind}",
        f"Message ID: `{entry.message_id}`",
    ]
    if entry.url:
        lines.append(f"Source: {entry.url}")

    lines.extend(reply_context_lines(entry.reply))
    lines.extend(["", "Message:", "", fenced_block(entry.raw_text)])
    return "\n".join(lines)


def append_message_block(path: Path, entry: MessageLogEntry) -> bool:
    text = path.read_text(encoding="utf-8")
    marker = message_marker(entry)
    if marker in text:
        return False

    updated_text = text.rstrip() + "\n\n" + message_block(entry) + "\n"
    write_text_atomic(path, updated_text)
    return True


def wikilink(title: str) -> str:
    return f"[[{title}]]"


def wikilink_target(link_body: str) -> str:
    target = link_body.split("|", 1)[0].split("#", 1)[0].strip()
    if target.endswith(".md"):
        target = target[:-3]
    return target


def linked_note_titles(path: Path) -> set[str]:
    return {
        wikilink_target(match.group(1))
        for match in WIKILINK_RE.finditer(path.read_text(encoding="utf-8"))
    }


def linked_message_note_titles(notes_root: Path) -> set[str]:
    return {
        title
        for path in notes_root.rglob("*.md")
        if not path.name.startswith("msg - ")
        for title in linked_note_titles(path)
        if title.startswith("msg - ")
    }


def delete_unlinked_message_notes(notes_root: Path) -> list[Path]:
    linked_titles = linked_message_note_titles(notes_root)
    deleted_paths: list[Path] = []
    for path in sorted(notes_root.glob("msg - *.md")):
        if path.stem in linked_titles:
            continue
        path.unlink()
        deleted_paths.append(path)
    return deleted_paths


def notification_line(entry: MessageLogEntry, note_title: str) -> str:
    preview_line = format_notification_note_line(
        source=entry.source,
        label=entry.label,
        url=entry.url,
    )
    return f"{preview_line} {wikilink(note_title)}"


def record_changed_message_notes(paths: Iterable[Path]) -> None:
    changed_notes_file = os.environ.get(CHANGED_MESSAGE_NOTES_FILE_ENV)
    if not changed_notes_file:
        return

    unique_paths = sorted({str(path.resolve()) for path in paths})
    if not unique_paths:
        return

    with Path(changed_notes_file).open("a", encoding="utf-8") as handle:
        for path in unique_paths:
            handle.write(f"{path}\n")


def save_message_notifications(
    notes_file: Path,
    notes_root: Path,
    entries: Iterable[MessageLogEntry],
) -> list[str]:
    inbox_lines: list[str] = []
    changed_message_notes: list[Path] = []
    for entry in entries:
        note_path = message_note_path(notes_root, entry)
        ensure_message_note(note_path, entry)
        if append_message_block(note_path, entry):
            changed_message_notes.append(note_path)
        inbox_lines.append(notification_line(entry, note_path.stem))

    append_markdown_lines(notes_file, inbox_lines)
    record_changed_message_notes(changed_message_notes)
    delete_unlinked_message_notes(notes_root)
    return inbox_lines
