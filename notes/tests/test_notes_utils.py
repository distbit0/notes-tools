from pathlib import Path
from types import SimpleNamespace
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import notes_utils  # noqa: E402


def test_terminal_safe_markdown_path_rejects_spaced_note_targets() -> None:
    stale_path = Path.home() / "notes/home note.md"

    assert notes_utils.terminal_safe_markdown_path(stale_path) == (
        Path.home() / "notes/home-note.md"
    )

    try:
        notes_utils.ensure_terminal_safe_markdown_path(stale_path)
    except ValueError as error:
        assert "home-note.md" in str(error)
    else:
        raise AssertionError("spaced Markdown note path should be rejected")


def test_format_notification_label_defaults_to_sender_prefixed_preview() -> None:
    preview = notes_utils.format_notification_label(
        sender_name="Alex",
        raw_text="hello there",
    )
    assert preview == "Alex: hello there"

    truncated_preview = notes_utils.format_notification_label(
        sender_name="Alex",
        raw_text="word " * 40,
    )
    assert truncated_preview.startswith("Alex: ")
    assert truncated_preview.endswith("...")


def test_format_notification_label_includes_group_and_mention_marker() -> None:
    group_preview = notes_utils.format_notification_label(
        sender_name="Alex",
        raw_text="hello there",
        conversation_name="Study Group",
    )
    assert group_preview == "Study Group | Alex: hello there"

    mentioned_group_preview = notes_utils.format_notification_label(
        sender_name="Alex",
        raw_text="hello there",
        conversation_name="Study Group",
        is_group_mention=True,
    )
    assert mentioned_group_preview == "Study Group | Alex: @hello there"


def test_format_notification_note_line_prefixes_linked_and_plain_entries() -> None:
    label = notes_utils.format_notification_label(
        sender_name="Alex",
        raw_text="hello [there]",
    )

    assert (
        notes_utils.format_notification_note_line(
            source="telegram",
            label=label,
            url="https://example.com/chat",
        )
        == "new notif: telegram: [Alex: hello \\[there\\]](https://example.com/chat)"
    )
    assert (
        notes_utils.format_notification_note_line(source="telegram", label=label)
        == "new notif: telegram: Alex: hello [there]"
    )


def test_send_persistent_desktop_notification_uses_hidden_url_body(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(notes_utils.shutil, "which", lambda command: "/usr/bin/notify-send")

    def fake_run(args, capture_output, text, check):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(notes_utils.subprocess, "run", fake_run)

    notes_utils.send_persistent_desktop_notification(
        app_name="Telegram",
        summary="Alex: hi",
        body="ignored body",
        category="telegram",
        on_click_url="https://example.com/chat",
    )

    assert calls == [
        [
            "notify-send",
            "--app-name",
            "Telegram",
            "--urgency",
            "critical",
            "--expire-time",
            "0",
            "--hint",
            "string:category:telegram",
            "Alex: hi",
            "https://example.com/chat",
        ]
    ]
