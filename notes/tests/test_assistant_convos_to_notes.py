import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import assistant_convo_state
import assistant_convos_to_notes
from notes_utils import append_markdown_lines


def write_codex_session(path: Path, roles: list[tuple[str, str | None]]) -> None:
    lines = []
    for role, phase in roles:
        payload = {
            "type": "message",
            "role": role,
            "content": [{"type": "output_text", "text": f"{role} text"}],
        }
        if phase is not None:
            payload["phase"] = phase
        lines.append(json.dumps({"type": "response_item", "payload": payload}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_codex_session_texts(
    path: Path,
    messages: list[tuple[str, str | None, str]],
) -> None:
    lines = []
    for role, phase, text in messages:
        content_type = "input_text" if role == "user" else "output_text"
        payload = {
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": text}],
        }
        if phase is not None:
            payload["phase"] = phase
        lines.append(json.dumps({"type": "response_item", "payload": payload}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_codex_threads_db(
    path: Path,
    *,
    thread_id: str,
    cwd: Path,
    rollout_path: Path,
    source: str = "cli",
    updated_at_ms: int = 1000,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                cwd text not null,
                first_user_message text not null,
                title text not null default '',
                preview text not null default '',
                source text not null default 'cli',
                archived integer not null default 0,
                updated_at integer not null default 0,
                updated_at_ms integer not null default 0
            )
            """
        )
        connection.execute(
            """
            insert into threads (
                id,
                rollout_path,
                cwd,
                first_user_message,
                title,
                preview,
                source,
                updated_at,
                updated_at_ms
            )
            values (?, ?, ?, ?, '', '', ?, 1, ?)
            """,
            (
                thread_id,
                str(rollout_path),
                str(cwd),
                "  First Codex message\nwith spacing  ",
                source,
                updated_at_ms,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_codex_pending_record_is_removed_when_thread_is_read(tmp_path):
    thread_id = "019e79c8-5594-7761-ba4d-53c2d51ccba1"
    state_path = tmp_path / "assistant-convos.json"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None), ("assistant", "final_answer")])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
    )

    key = assistant_convo_state.record_codex_pending(
        thread_id,
        notification_id=123,
        state_path=state_path,
        database_path=database_path,
    )
    state = assistant_convo_state.load_state(state_path)

    assert key in state["pending"]
    assert state["pending"][key]["notification_id"] == 123
    assert state["pending"][key]["first_message_prefix"] == "user text"

    removed_count = assistant_convo_state.mark_codex_thread_read(
        thread_id,
        state_path=state_path,
    )

    assert removed_count == 1
    state = assistant_convo_state.load_state(state_path)
    assert state["pending"] == {}
    assert state["appended"][key]["reason"] == "user_reply"


def test_markdown_collect_is_idempotent_after_append_marker(tmp_path):
    state = assistant_convo_state.empty_state()
    state["pending"]["chatgpt:conversation:unread:message"] = {
        "key": "chatgpt:conversation:unread:message",
        "source": "chatgpt",
        "reason": "unread",
        "link": "https://chatgpt.com/c/conversation",
        "first_message_prefix": "First [message]",
        "created_at_ms": 1,
    }

    lines, keys = assistant_convos_to_notes.collect_markdown_lines(state)
    note_path = tmp_path / "inbox-index.md"
    note_path.write_text("# Temp\n", encoding="utf-8")
    append_markdown_lines(note_path, lines)
    assistant_convo_state.mark_appended(state, keys, state_path=tmp_path / "state.json")

    assert lines == [
        "maybe pending chatgpt convo: "
        "[First \\[message\\]](https://chatgpt.com/c/conversation)"
    ]
    updated_state = assistant_convo_state.load_state(tmp_path / "state.json")
    assert assistant_convos_to_notes.collect_markdown_lines(updated_state) == ([], [])


def test_codex_final_answer_pending_is_not_appended():
    state = assistant_convo_state.empty_state()
    state["pending"]["codex:thread:unread"] = {
        "key": "codex:thread:unread",
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "notification_id": 123,
        "thread_id": "thread",
        "cwd": "/repo",
        "created_at_ms": 1000,
    }

    assert assistant_convos_to_notes.collect_markdown_lines(
        state,
        activated_ids=frozenset(),
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
    ) == ([], [])


def test_codex_pending_is_marked_read_after_notification_activation():
    state = assistant_convo_state.empty_state()
    state["pending"]["codex:thread:unread"] = {
        "key": "codex:thread:unread",
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "notification_id": 123,
        "thread_id": "thread",
        "cwd": "/repo",
        "created_at_ms": 1,
    }
    assert assistant_convos_to_notes.collect_markdown_lines(
        state,
        activated_ids=frozenset({123}),
        now_ms=1 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
    ) == ([], [])
    assert state["pending"] == {}
    assert state["appended"]["codex:thread:unread"]["reason"] == "notification_activated"


def test_codex_final_answer_pending_is_not_appended_without_activation():
    state = assistant_convo_state.empty_state()
    state["pending"]["codex:thread:unread"] = {
        "key": "codex:thread:unread",
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "notification_id": 123,
        "thread_id": "thread",
        "cwd": "/repo",
        "created_at_ms": 1000,
    }

    assert assistant_convos_to_notes.collect_markdown_lines(
        state,
        activated_ids=frozenset(),
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
    ) == ([], [])


def test_codex_pending_without_notification_is_not_appended():
    state = assistant_convo_state.empty_state()
    state["pending"]["codex:thread:assistant:42"] = {
        "key": "codex:thread:assistant:42",
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "thread_id": "thread",
        "cwd": "/repo",
        "created_at_ms": 1,
    }

    assert assistant_convos_to_notes.collect_markdown_lines(
        state,
        now_ms=1 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
    ) == ([], [])


def test_activation_ledger_extracts_codex_notification_ids(tmp_path):
    ledger_path = tmp_path / "activations.jsonl"
    ledger_path.write_text(
        "\n".join(
            [
                json.dumps({"notification_id": 11, "source": "codex"}),
                json.dumps({"notification_id": 22, "source": "chatgpt"}),
                json.dumps({"notification_id": "bad", "source": "codex"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert assistant_convos_to_notes.fetch_assistant_notification_activation_ids(
        ledger_path
    ) == frozenset({11})


def test_sync_codex_pending_from_sessions_does_not_backfill_final_answers(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None), ("assistant", "final_answer")])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000,
    )

    assert state["pending"] == {}


def test_sync_codex_pending_updates_notification_backed_final_answer(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None), ("assistant", "final_answer")])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0
    latest_message = assistant_convo_state.latest_codex_session_message(rollout_path)
    assert latest_message is not None
    key = assistant_convo_state.codex_message_key(thread_id, latest_message)
    state["pending"][key] = {
        "key": key,
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "notification_id": 123,
        "thread_id": thread_id,
        "cwd": str(tmp_path),
        "created_at_ms": 1,
    }

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
    )

    records = list(state["pending"].values())
    assert len(records) == 1
    assert records[0]["source"] == "codex"
    assert records[0]["notification_id"] == 123
    assert records[0]["thread_id"] == thread_id
    assert records[0]["cwd"] == str(tmp_path)


def test_codex_final_answer_label_uses_latest_user_message(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session_texts(
        rollout_path,
        [
            ("user", None, "first setup prompt"),
            ("assistant", "final_answer", "first answer"),
            ("user", None, "more recent user context"),
            ("assistant", "final_answer", "latest answer"),
        ],
    )
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
    )

    key = assistant_convo_state.record_codex_pending(
        thread_id,
        notification_id=123,
        state_path=tmp_path / "state.json",
        database_path=database_path,
    )
    state = assistant_convo_state.load_state(tmp_path / "state.json")

    assert (
        state["pending"][key]["first_message_prefix"]
        == "more recent user context"
    )


def test_sync_codex_pending_ignores_non_interactive_exec_thread(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None), ("assistant", "final_answer")])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
        source="exec",
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000,
    )

    assert state["pending"] == {}


def test_sync_codex_pending_clears_existing_non_interactive_record(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None), ("assistant", "final_answer")])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
        source="exec",
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0
    state["pending"]["codex:thread:assistant:42"] = {
        "key": "codex:thread:assistant:42",
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "notification_id": 123,
        "thread_id": thread_id,
        "cwd": str(tmp_path),
        "created_at_ms": 1,
    }

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
    )

    assert state["pending"] == {}
    assert state["appended"]["codex:thread:assistant:42"]["reason"] == (
        "non_interactive_source"
    )


def test_sync_codex_pending_drops_old_non_notification_record(tmp_path):
    state = assistant_convo_state.empty_state()
    state["pending"]["codex:thread:assistant:42"] = {
        "key": "codex:thread:assistant:42",
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "thread_id": "thread",
        "cwd": str(tmp_path),
        "created_at_ms": 1,
    }

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=tmp_path / "missing.sqlite",
        limit=0,
    )

    assert state["pending"] == {}
    assert state["appended"] == {}


def test_sync_codex_pending_rejects_malformed_notification_record(tmp_path):
    state = assistant_convo_state.empty_state()
    state["pending"]["codex:thread:assistant:42"] = {
        "key": "codex:thread:assistant:42",
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "notification_id": "bad",
        "thread_id": "thread",
        "cwd": str(tmp_path),
        "created_at_ms": 1,
    }

    with pytest.raises(RuntimeError, match="invalid Codex notification id"):
        assistant_convo_state.sync_codex_pending_from_sessions(
            state,
            database_path=tmp_path / "missing.sqlite",
            limit=0,
        )


def test_sync_codex_pending_respects_legacy_thread_acknowledgement(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None), ("assistant", "final_answer")])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0
    state["appended"]["codex:thread:unread"] = 1

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
    )

    assert state["pending"] == {}


def test_sync_codex_pending_does_not_backfill_before_scan_start(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None), ("assistant", "final_answer")])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 2000

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
    )

    assert state["pending"] == {}


def test_sync_codex_pending_marks_thread_read_after_user_reply(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(
        rollout_path,
        [("user", None), ("assistant", "final_answer"), ("user", None)],
    )
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0
    state["pending"]["codex:thread:assistant:42"] = {
        "key": "codex:thread:assistant:42",
        "source": "codex",
        "reason": "unread",
        "first_message_prefix": "Prompt",
        "notification_id": 123,
        "thread_id": thread_id,
        "cwd": str(tmp_path),
        "created_at_ms": 1,
    }

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000,
    )

    assert state["pending"] == {}
    assert state["appended"]["codex:thread:assistant:42"]["reason"] == "user_reply"


def test_sync_codex_pending_skips_young_user_last_thread(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None)])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
        updated_at_ms=1000,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS - 1,
        watcher_dir=tmp_path / "watchers",
        proc_root=tmp_path / "proc",
    )

    assert state["pending"] == {}


def test_sync_codex_pending_adds_old_inactive_user_last_thread(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session(rollout_path, [("user", None)])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
        updated_at_ms=1000,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
        watcher_dir=tmp_path / "watchers",
        proc_root=tmp_path / "proc",
    )

    records = list(state["pending"].values())
    assert len(records) == 1
    assert records[0]["reason"] == "stalled"
    assert records[0]["first_message_prefix"] == "user text"
    assert assistant_convos_to_notes.collect_markdown_lines(
        state,
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
    ) == (
        ["possibly stalled codex convo: user text\ncd "
         f"{tmp_path} && codex resume thread"],
        [records[0]["key"]],
    )


def test_sync_codex_pending_ignores_turn_aborted_wrapper_for_stalled(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    write_codex_session_texts(
        rollout_path,
        [
            ("user", None, "first setup prompt"),
            ("assistant", "final_answer", "done"),
            ("user", None, "resume this specific task"),
            (
                "user",
                None,
                (
                    "<turn_aborted>\n"
                    "The user interrupted the previous turn on purpose. Any running "
                    "unified exec processes may still be running in the background. "
                    "If any tools/commands were aborted, they may have partially "
                    "executed.\n"
                    "</turn_aborted>"
                ),
            ),
        ],
    )
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
        updated_at_ms=1000,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
        watcher_dir=tmp_path / "watchers",
        proc_root=tmp_path / "proc",
    )

    assert state["pending"] == {}


def test_sync_codex_pending_suppresses_stalled_after_tool_activity(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    lines = []
    for payload in [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "do the task"}],
        },
        {
            "type": "function_call",
            "name": "exec_command",
            "call_id": "call_1",
            "arguments": "{}",
        },
    ]:
        lines.append(json.dumps({"type": "response_item", "payload": payload}))
    rollout_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
        updated_at_ms=1000,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
        watcher_dir=tmp_path / "watchers",
        proc_root=tmp_path / "proc",
    )

    assert state["pending"] == {}


def test_sync_codex_pending_suppresses_stalled_thread_with_active_watcher(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    watcher_dir = tmp_path / "watchers"
    watcher_dir.mkdir()
    (watcher_dir / f"{thread_id}.pid").write_text(
        f"{os.getpid()}\n",
        encoding="utf-8",
    )
    write_codex_session(rollout_path, [("user", None)])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
        updated_at_ms=1000,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
        watcher_dir=watcher_dir,
        proc_root=tmp_path / "proc",
    )

    assert state["pending"] == {}


def test_sync_codex_pending_suppresses_stalled_thread_with_open_rollout(tmp_path):
    thread_id = "thread"
    database_path = tmp_path / "state.sqlite"
    rollout_path = tmp_path / "rollout.jsonl"
    proc_root = tmp_path / "proc"
    fd_dir = proc_root / "123" / "fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "4").symlink_to(rollout_path)
    write_codex_session(rollout_path, [("user", None)])
    create_codex_threads_db(
        database_path,
        thread_id=thread_id,
        cwd=tmp_path,
        rollout_path=rollout_path,
        updated_at_ms=1000,
    )
    state = assistant_convo_state.empty_state()
    state["codex_scan_started_at_ms"] = 0

    assistant_convo_state.sync_codex_pending_from_sessions(
        state,
        database_path=database_path,
        now_ms=1000 + assistant_convo_state.CODEX_STALLED_TIMEOUT_MS,
        watcher_dir=tmp_path / "watchers",
        proc_root=proc_root,
    )

    assert state["pending"] == {}


def test_chatgpt_fixture_parser_extracts_unread_and_cut_off_conversations():
    fetcher_path = Path(__file__).resolve().parents[2] / "notes/chatgpt_backend_fetch.mjs"
    fixture = {
        "listPayload": {
            "items": [
                {"id": "conv-unread", "async_status": 4, "title": "Unread title"},
                {
                    "id": "conv-cutoff",
                    "async_status": None,
                    "status": "aborted",
                    "title": "Cut off title",
                },
                {"id": "conv-read", "async_status": None, "title": "Read title"},
            ]
        },
        "conversationsById": {
            "conv-unread": {
                "conversation_id": "conv-unread",
                "mapping": {
                    "user": {
                        "message": {
                            "id": "user-1",
                            "author": {"role": "user"},
                            "content": {"parts": ["Unread prompt"]},
                            "create_time": 1,
                        }
                    },
                    "assistant": {
                        "message": {
                            "id": "assistant-1",
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Done"]},
                            "create_time": 2,
                            "status": "finished_successfully",
                        }
                    },
                },
            },
            "conv-cutoff": {
                "conversation_id": "conv-cutoff",
                "mapping": {
                    "user": {
                        "message": {
                            "id": "user-2",
                            "author": {"role": "user"},
                            "content": {"parts": ["Cut off prompt"]},
                            "create_time": 1,
                        }
                    },
                    "assistant": {
                        "message": {
                            "id": "assistant-2",
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Partial"]},
                            "create_time": 2,
                            "status": "aborted",
                        }
                    },
                },
            },
        },
    }
    script = f"""
      import {{ recordsFromFixture }} from {json.dumps(fetcher_path.as_uri())};
      const chunks = [];
      for await (const chunk of process.stdin) chunks.push(chunk);
      const fixture = JSON.parse(chunks.join(""));
      process.stdout.write(JSON.stringify(recordsFromFixture(fixture)));
    """

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(fixture),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {
            "conversationId": "conv-unread",
            "reason": "unread",
            "title": "Unread title",
            "latestMessageId": "assistant-1",
        },
        {
            "conversationId": "conv-cutoff",
            "reason": "cut_off",
            "title": "Cut off title",
            "latestMessageId": "assistant-2",
        },
    ]


def test_chatgpt_fixture_parser_treats_async_status_as_known_read_state():
    fetcher_path = Path(__file__).resolve().parents[2] / "notes/chatgpt_backend_fetch.mjs"
    script = f"""
      import {{ recordsFromFixture }} from {json.dumps(fetcher_path.as_uri())};
      const records = recordsFromFixture({{
        listPayload: {{ items: [{{ id: "conv-read", async_status: null }}] }},
        conversationsById: {{}}
      }});
      process.stdout.write(JSON.stringify(records));
    """

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_validate_chatgpt_record_rejects_missing_title():
    with pytest.raises(RuntimeError, match="empty title"):
        assistant_convos_to_notes.validate_chatgpt_record(
            {
                "conversationId": "conv",
                "reason": "unread",
                "title": "",
                "latestMessageId": "message",
            }
        )
