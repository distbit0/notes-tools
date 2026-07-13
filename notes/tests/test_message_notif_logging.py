from pathlib import Path
import sys

from private_test_data import PRIVATE_TEST_DATA


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

from message_notif_logging import (  # noqa: E402
    CHANGED_MESSAGE_NOTES_FILE_ENV,
    MessageLogEntry,
    ReplyContext,
    delete_unlinked_message_notes,
    save_message_notifications,
)


CONTACT_NAME = PRIVATE_TEST_DATA["discord"]["contactName"]
SELF_NAME = PRIVATE_TEST_DATA["discord"]["selfName"]


def make_entry(message_id: str, raw_text: str, reply: ReplyContext | None = None) -> MessageLogEntry:
    return MessageLogEntry(
        source="discord",
        kind="dm",
        label=f"{CONTACT_NAME}: truncated preview",
        url=f"https://discord.com/channels/@me/10/{message_id}",
        conversation_id="10",
        conversation_name=CONTACT_NAME,
        sender_name=CONTACT_NAME,
        message_id=message_id,
        timestamp_ms=1_800_000_000_000 + int(message_id),
        raw_text=raw_text,
        reply=reply,
    )


def test_save_message_notifications_keeps_preview_and_reuses_conversation_note(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "inbox-index.md"
    inbox_path.write_text("# Inbox index\n", encoding="utf-8")
    reply = ReplyContext(
        sender_name=SELF_NAME,
        raw_text="full earlier message being replied to",
        timestamp_ms=1_799_999_999_999,
        message_id="99",
        url="https://discord.com/channels/@me/10/99",
    )

    first_lines = save_message_notifications(
        inbox_path,
        tmp_path,
        [make_entry("100", "full first message, not just the preview", reply)],
    )
    second_lines = save_message_notifications(
        inbox_path,
        tmp_path,
        [make_entry("101", "full second message in the same conversation")],
    )

    message_notes = sorted(tmp_path.glob(f"msg - Discord - {CONTACT_NAME} - *.md"))
    assert len(message_notes) == 1
    assert first_lines[0].startswith(
        f"new notif: discord: [{CONTACT_NAME}: truncated preview]"
        f"(https://discord.com/channels/@me/10/100) [[msg - Discord - {CONTACT_NAME} - "
    )
    assert second_lines[0].startswith(
        f"new notif: discord: [{CONTACT_NAME}: truncated preview]"
        f"(https://discord.com/channels/@me/10/101) [[msg - Discord - {CONTACT_NAME} - "
    )

    inbox_text = inbox_path.read_text(encoding="utf-8")
    assert inbox_text.count("new notif: discord:") == 2
    assert inbox_text.count(f"[[msg - Discord - {CONTACT_NAME} - ") == 2

    note_text = message_notes[0].read_text(encoding="utf-8")
    assert note_text.count("<!-- msg-message") == 2
    assert "full first message, not just the preview" in note_text
    assert "full second message in the same conversation" in note_text
    assert "Replying to:" in note_text
    assert "full earlier message being replied to" in note_text


def test_save_message_notifications_records_notes_with_new_message_blocks(
    tmp_path: Path, monkeypatch
) -> None:
    inbox_path = tmp_path / "inbox-index.md"
    changed_notes_path = tmp_path / "changed-message-notes.txt"
    inbox_path.write_text("# Inbox index\n", encoding="utf-8")
    monkeypatch.setenv(CHANGED_MESSAGE_NOTES_FILE_ENV, str(changed_notes_path))

    save_message_notifications(
        inbox_path,
        tmp_path,
        [make_entry("100", "first message")],
    )
    save_message_notifications(
        inbox_path,
        tmp_path,
        [make_entry("100", "first message")],
    )

    changed_paths = changed_notes_path.read_text(encoding="utf-8").splitlines()
    assert len(changed_paths) == 1
    assert Path(changed_paths[0]).name.startswith(
        f"msg - Discord - {CONTACT_NAME} - "
    )


def test_save_message_notifications_deletes_unlinked_message_notes(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "inbox-index.md"
    inbox_path.write_text("# Inbox index\n", encoding="utf-8")

    save_message_notifications(
        inbox_path,
        tmp_path,
        [make_entry("100", "message that will be unlinked")],
    )
    stale_note = next(tmp_path.glob(f"msg - Discord - {CONTACT_NAME} - *.md"))
    inbox_path.write_text("# Inbox index\n", encoding="utf-8")

    save_message_notifications(
        inbox_path,
        tmp_path,
        [
            MessageLogEntry(
                source="telegram",
                kind="dm",
                label="Michael B: newer message",
                url=None,
                conversation_id="20",
                conversation_name="Michael B",
                sender_name="Michael B",
                message_id="200",
                timestamp_ms=1_800_000_000_200,
                raw_text="newer linked message",
            )
        ],
    )

    assert not stale_note.exists()
    assert len(sorted(tmp_path.glob("msg - Telegram - Michael B - *.md"))) == 1


def test_delete_unlinked_message_notes_ignores_links_from_message_notes(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "inbox-index.md"
    inbox_path.write_text("# Inbox index\n", encoding="utf-8")

    save_message_notifications(
        inbox_path,
        tmp_path,
        [make_entry("100", "message that will be unlinked")],
    )
    stale_note = next(tmp_path.glob(f"msg - Discord - {CONTACT_NAME} - *.md"))
    inbox_path.write_text("# Inbox index\n", encoding="utf-8")

    referring_note = tmp_path / "msg - Manual - local reminder - abc12345.md"
    referring_note.write_text(f"[[{stale_note.stem}]]\n", encoding="utf-8")

    deleted_notes = delete_unlinked_message_notes(tmp_path)

    assert deleted_notes == [stale_note, referring_note]
    assert not stale_note.exists()
    assert not referring_note.exists()
