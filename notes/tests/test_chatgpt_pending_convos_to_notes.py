import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chatgpt_pending_convos_to_notes


SOURCE_STATE_PATH = (
    Path.home() / ".local/state/chatgpt-pending-convos-to-notes.json"
)
SOURCE_ARCHIVE_STATE_PATH = (
    Path.home() / ".local/state/chatgpt-convos-to-notes/state.json"
)
SOURCE_INBOX_PATH = Path.home() / "notes/inbox-index.md"


def real_pending_input_record() -> dict[str, str]:
    source_state = json.loads(SOURCE_STATE_PATH.read_text(encoding="utf-8"))
    archive_state = json.loads(SOURCE_ARCHIVE_STATE_PATH.read_text(encoding="utf-8"))
    for key, appended_record in source_state["appended"].items():
        if not isinstance(appended_record, dict):
            continue
        _, conversation_id, reason, key_suffix = key.split(":", 3)
        archived_conversation = archive_state["conversations"].get(conversation_id)
        if archived_conversation:
            return {
                "conversationId": conversation_id,
                "reason": reason,
                "title": archived_conversation["title"],
                "latestMessageId": "" if key_suffix == reason else key_suffix,
            }
    raise RuntimeError("No real pending ChatGPT reminder is available for testing")


def test_local_pending_writer_appends_a_real_record_once(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    notes_file = tmp_path / "inbox-index.md"
    source_inbox_first_line = SOURCE_INBOX_PATH.read_text(encoding="utf-8").splitlines()[0]
    notes_file.write_text(f"{source_inbox_first_line}\n", encoding="utf-8")
    state_path = tmp_path / "pending-state.json"
    log_path = tmp_path / "pending.log"
    monkeypatch.setattr(chatgpt_pending_convos_to_notes, "NOTES_FILE", notes_file)
    monkeypatch.setattr(chatgpt_pending_convos_to_notes, "STATE_PATH", state_path)
    monkeypatch.setattr(chatgpt_pending_convos_to_notes, "LOG_PATH", log_path)
    record = real_pending_input_record()

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([record])))
    assert chatgpt_pending_convos_to_notes.main([]) == 0
    first_result = json.loads(capsys.readouterr().out.splitlines()[-1])
    first_contents = notes_file.read_text(encoding="utf-8")
    assert first_result == {"appended": 1}
    assert record["conversationId"] in first_contents

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([record])))
    assert chatgpt_pending_convos_to_notes.main([]) == 0
    second_result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert second_result == {"appended": 0}
    assert notes_file.read_text(encoding="utf-8") == first_contents
