from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from automation.start_random_todo import (
    DEFAULT_HERDR_BIN,
    DEFAULT_HERDR_LAUNCHER,
    DEFAULT_INBOX,
    TODO_SECTION_MARKER,
    Herdr,
    add_session_annotation,
    find_live_session_panes,
    live_session_panes,
    load_todos,
    notes_workspace,
    parse_inbox_todos,
    process_resumes_session,
)


def live_snapshot() -> dict:
    try:
        return Herdr(DEFAULT_HERDR_BIN, DEFAULT_HERDR_LAUNCHER).snapshot()
    except RuntimeError as exc:
        pytest.skip(f"Herdr is unavailable: {exc}")


def test_live_inbox_parser_only_returns_top_level_inbox_items() -> None:
    inbox_text = DEFAULT_INBOX.read_text(encoding="utf-8")
    inbox_lines = inbox_text.splitlines()
    inbox_heading = next(
        index
        for index, line in enumerate(inbox_lines)
        if line.strip() == TODO_SECTION_MARKER
    )

    todos = load_todos(DEFAULT_INBOX)

    assert todos
    assert all(todo.line_index > inbox_heading for todo in todos)
    assert all(inbox_lines[todo.line_index] == todo.line for todo in todos)
    assert all(todo.line.startswith("- ") and todo.task for todo in todos)


def test_inbox_parser_uses_stable_marker_instead_of_heading_text() -> None:
    inbox_text = """# A heading the user can rename
- not a scheduled todo

<!-- scheduled-todo-kickoff-start -->
- first scheduled todo
- second scheduled todo
"""

    todos = parse_inbox_todos(inbox_text)

    assert [todo.task for todo in todos] == [
        "first scheduled todo",
        "second scheduled todo",
    ]


def test_annotation_round_trip_uses_a_live_codex_session(
    tmp_path: Path,
) -> None:
    snapshot = live_snapshot()
    active_session_ids = [
        str(pane["agent_session"]["value"])
        for pane in snapshot["panes"]
        if pane.get("agent") == "codex"
        and pane.get("agent_session", {}).get("kind") == "id"
    ]
    if not active_session_ids:
        pytest.skip("Herdr has no live Codex sessions")

    copied_inbox = tmp_path / "inbox-index.md"
    shutil.copyfile(DEFAULT_INBOX, copied_inbox)
    selected_todo = next(
        (todo for todo in load_todos(copied_inbox) if todo.session_id is None),
        None,
    )
    if selected_todo is None:
        pytest.skip("the live inbox has no unannotated todo")

    add_session_annotation(copied_inbox, selected_todo, active_session_ids[0])
    annotated_todo = next(
        todo
        for todo in load_todos(copied_inbox)
        if todo.line_index == selected_todo.line_index
    )

    assert annotated_todo.task == selected_todo.task
    assert annotated_todo.session_id == active_session_ids[0]


def test_live_session_and_notes_workspace_detection() -> None:
    snapshot = live_snapshot()
    workspace = notes_workspace(snapshot)
    assert workspace is None or workspace["label"] == "notes"

    codex_panes = [
        pane
        for pane in snapshot["panes"]
        if pane.get("agent") == "codex"
        and pane.get("agent_session", {}).get("kind") == "id"
    ]
    if not codex_panes:
        pytest.skip("Herdr has no live Codex sessions")

    session_id = str(codex_panes[0]["agent_session"]["value"])
    assert codex_panes[0] in live_session_panes(snapshot, session_id)


def test_live_resumed_codex_process_is_detected() -> None:
    herdr = Herdr(DEFAULT_HERDR_BIN, DEFAULT_HERDR_LAUNCHER)
    snapshot = live_snapshot()
    for pane in snapshot["panes"]:
        if pane.get("agent") != "codex":
            continue
        process_info = herdr.run_json(
            ["pane", "process-info", "--pane", str(pane["pane_id"])]
        )["result"]["process_info"]
        for process in process_info["foreground_processes"]:
            arguments = process.get("argv", [])
            for index, argument in enumerate(arguments[:-1]):
                if argument != "resume":
                    continue
                session_id = str(arguments[index + 1])
                assert process_resumes_session(process_info, session_id)
                assert pane in find_live_session_panes(herdr, snapshot, session_id)
                return
    pytest.skip("Herdr has no live resumed Codex process")
