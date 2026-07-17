import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chatgpt_pending_convos_to_notes


SOURCE_STATE_PATH = (
    Path.home() / ".local/state/chatgpt-pending-convos-to-notes.json"
)
SOURCE_INBOX_PATH = Path.home() / "notes/inbox-index.md"


def real_pending_input_record() -> dict[str, str]:
    source_state = json.loads(SOURCE_STATE_PATH.read_text(encoding="utf-8"))
    source_record = next(iter(source_state["pending"].values()))
    return {
        "conversationId": source_record["conversation_id"],
        "reason": source_record["reason"],
        "title": source_record["title"],
        "latestMessageId": source_record["latest_message_id"],
    }


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
