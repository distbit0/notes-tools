#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


NOTES_DIR = Path("/home/pimania/notes")
DEFAULT_INBOX = NOTES_DIR / "inbox-index.md"
DEFAULT_HERDR_BIN = "/home/pimania/.local/bin/herdr"
DEFAULT_HERDR_LAUNCHER = "/home/pimania/dev/misc/desktop/herdr-launch.sh"
DEFAULT_LOCK = Path.home() / ".local/state/scheduled-codex/start-random-todo.lock"
WORKSPACE_LABEL = "notes"
TODO_SECTION_MARKER = "<!-- scheduled-todo-kickoff-start -->"
SESSION_SUFFIX = re.compile(
    r"\s+codex resume "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Todo:
    line_index: int
    line: str
    task: str
    session_id: str | None


class HerdrError(RuntimeError):
    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def parse_inbox_todos(text: str) -> list[Todo]:
    lines = text.splitlines()
    try:
        inbox_start = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == TODO_SECTION_MARKER
        )
    except StopIteration as exc:
        raise RuntimeError(
            f"inbox-index.md has no {TODO_SECTION_MARKER} marker"
        ) from exc

    todos: list[Todo] = []
    for line_index, line in enumerate(lines[inbox_start + 1 :], inbox_start + 1):
        if not line.startswith("- "):
            continue
        item_text = line[2:].strip()
        session_match = SESSION_SUFFIX.search(item_text)
        session_id = session_match.group(1) if session_match else None
        task = SESSION_SUFFIX.sub("", item_text).strip()
        if task:
            todos.append(Todo(line_index, line, task, session_id))
    if not todos:
        raise RuntimeError("the inbox section contains no top-level todo bullets")
    return todos


def load_todos(inbox_path: Path) -> list[Todo]:
    return parse_inbox_todos(inbox_path.read_text(encoding="utf-8"))


def write_lines_atomically(path: Path, lines: list[str]) -> None:
    file_mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary_file:
        temporary_file.writelines(lines)
        temporary_path = Path(temporary_file.name)
    os.chmod(temporary_path, file_mode)
    os.replace(temporary_path, path)


def add_session_annotation(inbox_path: Path, todo: Todo, session_id: str) -> None:
    lines = inbox_path.read_text(encoding="utf-8").splitlines(keepends=True)
    current_line = lines[todo.line_index].rstrip("\r\n")
    if current_line != todo.line:
        raise RuntimeError("the selected inbox todo changed before it could be annotated")
    if SESSION_SUFFIX.search(current_line):
        raise RuntimeError("the selected inbox todo already has a Codex session")

    newline = "\n" if lines[todo.line_index].endswith("\n") else ""
    lines[todo.line_index] = f"{current_line} codex resume {session_id}{newline}"
    write_lines_atomically(inbox_path, lines)


def parse_herdr_payload(output: str, operation: str) -> dict:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HerdrError(f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HerdrError(f"{operation} returned a non-object response")
    error = payload.get("error")
    if isinstance(error, dict):
        raise HerdrError(
            f"{operation} failed: {error.get('message') or 'unknown error'}",
            code=str(error.get("code") or ""),
        )
    return payload


class Herdr:
    def __init__(self, binary: str, launcher: str) -> None:
        self.binary = binary
        self.launcher = launcher

    def run(self, arguments: list[str]) -> str:
        operation = f"herdr {' '.join(arguments[:2])}"
        try:
            result = subprocess.run(
                [self.binary, *arguments],
                capture_output=True,
                text=True,
                check=False,
                timeout=70,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HerdrError(f"{operation} could not run: {exc}") from exc
        output = result.stdout or result.stderr
        if result.returncode != 0:
            if output:
                parse_herdr_payload(output, operation)
            raise HerdrError(f"{operation} exited with status {result.returncode}")
        return output

    def run_json(self, arguments: list[str]) -> dict:
        operation = f"herdr {' '.join(arguments[:2])}"
        return parse_herdr_payload(self.run(arguments), operation)

    def snapshot(self) -> dict:
        payload = self.run_json(["api", "snapshot"])
        snapshot = payload.get("result", {}).get("snapshot")
        if not isinstance(snapshot, dict):
            raise HerdrError("herdr api snapshot omitted result.snapshot")
        return snapshot

    def window_exists(self) -> bool:
        return subprocess.run(
            ["xdotool", "search", "--name", "^Herdr$"],
            capture_output=True,
            check=False,
        ).returncode == 0

    def show_window(self) -> None:
        if self.window_exists():
            command = [self.launcher, "--focus-only"]
        else:
            command = [
                "systemd-run",
                "--user",
                "--unit=herdr-todo-kickoff-window",
                "--collect",
                "--no-block",
                self.launcher,
            ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            raise HerdrError(f"could not show the Herdr window: {error or result.returncode}")

    def ensure_snapshot(self) -> dict:
        try:
            return self.snapshot()
        except HerdrError as initial_error:
            self.show_window()
            deadline = time.monotonic() + 15
            latest_error: HerdrError = initial_error
            while time.monotonic() < deadline:
                time.sleep(0.25)
                try:
                    return self.snapshot()
                except HerdrError as exc:
                    latest_error = exc
            raise HerdrError(
                f"Herdr did not become ready after launch: {latest_error}"
            ) from initial_error


def live_session_panes(snapshot: dict, session_id: str) -> list[dict]:
    panes = snapshot.get("panes")
    if not isinstance(panes, list):
        raise HerdrError("Herdr snapshot omitted panes")
    return [
        pane
        for pane in panes
        if isinstance(pane, dict)
        and pane.get("agent") == "codex"
        and pane.get("agent_session", {}).get("kind") == "id"
        and pane.get("agent_session", {}).get("value") == session_id
    ]


def process_resumes_session(process_info: dict, session_id: str) -> bool:
    foreground_processes = process_info.get("foreground_processes")
    if not isinstance(foreground_processes, list):
        raise HerdrError("Herdr process info omitted foreground_processes")
    for process in foreground_processes:
        arguments = process.get("argv") if isinstance(process, dict) else None
        if not isinstance(arguments, list):
            continue
        if any(
            argument == "resume" and arguments[index + 1] == session_id
            for index, argument in enumerate(arguments[:-1])
        ):
            return True
    return False


def pane_resumes_session(herdr: Herdr, pane_id: str, session_id: str) -> bool:
    result = result_object(
        herdr.run_json(["pane", "process-info", "--pane", pane_id])
    )
    process_info = result.get("process_info")
    if not isinstance(process_info, dict):
        raise HerdrError("Herdr did not return pane process info")
    return process_resumes_session(process_info, session_id)


def find_live_session_panes(
    herdr: Herdr,
    snapshot: dict,
    session_id: str,
) -> list[dict]:
    matches = live_session_panes(snapshot, session_id)
    panes = snapshot.get("panes")
    if not isinstance(panes, list):
        raise HerdrError("Herdr snapshot omitted panes")
    matched_pane_ids = {str(pane["pane_id"]) for pane in matches}
    for pane in panes:
        if (
            not isinstance(pane, dict)
            or pane.get("agent") != "codex"
            or str(pane.get("pane_id")) in matched_pane_ids
        ):
            continue
        pane_id = str(pane["pane_id"])
        if pane_resumes_session(herdr, pane_id, session_id):
            matches.append(pane)
    return matches


def notes_workspace(snapshot: dict) -> dict | None:
    workspaces = snapshot.get("workspaces")
    if not isinstance(workspaces, list):
        raise HerdrError("Herdr snapshot omitted workspaces")
    matches = [
        workspace
        for workspace in workspaces
        if isinstance(workspace, dict) and workspace.get("label") == WORKSPACE_LABEL
    ]
    if len(matches) > 1:
        raise HerdrError("multiple Herdr workspaces are labelled 'notes'")
    return matches[0] if matches else None


def result_object(payload: dict) -> dict:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HerdrError("Herdr response omitted its result object")
    return result


def create_todo_tab(herdr: Herdr, snapshot: dict, task: str) -> tuple[str, str]:
    workspace = notes_workspace(snapshot)
    label = f"todo: {' '.join(task.split())[:60]}"
    if workspace:
        result = result_object(
            herdr.run_json(
                [
                    "tab",
                    "create",
                    "--workspace",
                    str(workspace["workspace_id"]),
                    "--cwd",
                    str(NOTES_DIR),
                    "--label",
                    label,
                    "--no-focus",
                ]
            )
        )
    else:
        result = result_object(
            herdr.run_json(
                [
                    "workspace",
                    "create",
                    "--cwd",
                    str(NOTES_DIR),
                    "--label",
                    WORKSPACE_LABEL,
                    "--no-focus",
                ]
            )
        )

    tab = result.get("tab")
    root_pane = result.get("root_pane")
    if not isinstance(tab, dict) or not isinstance(root_pane, dict):
        raise HerdrError("Herdr did not return the new tab and root pane")
    return str(tab["tab_id"]), str(root_pane["pane_id"])


def start_codex(
    herdr: Herdr,
    pane_id: str,
    session_id: str | None,
    initial_prompt: str | None = None,
) -> str:
    agent_name = f"todo-{uuid.uuid4().hex[:10]}"
    arguments = [
        "agent",
        "start",
        agent_name,
        "--kind",
        "codex",
        "--pane",
        pane_id,
        "--timeout",
        "60000",
    ]
    if session_id:
        arguments.extend(["--", "resume", session_id])

    shell_ready_deadline = time.monotonic() + 10
    while True:
        try:
            herdr.run_json(arguments)
            break
        except HerdrError as exc:
            if exc.code != "agent_pane_busy" or time.monotonic() >= shell_ready_deadline:
                raise
            time.sleep(0.1)

    if initial_prompt:
        herdr.run_json(["agent", "prompt", pane_id, initial_prompt])

    if session_id:
        if not pane_resumes_session(herdr, pane_id, session_id):
            raise HerdrError(
                f"the started Codex process did not resume session {session_id}"
            )
        return session_id

    session_ready_deadline = time.monotonic() + 30
    while True:
        agent = result_object(herdr.run_json(["agent", "get", pane_id])).get("agent")
        if not isinstance(agent, dict):
            raise HerdrError("Herdr did not return the started Codex agent")
        agent_session = agent.get("agent_session")
        if isinstance(agent_session, dict) and agent_session.get("kind") == "id":
            break
        if time.monotonic() >= session_ready_deadline:
            raise HerdrError("the started Codex agent did not report a session id")
        time.sleep(0.1)

    started_session_id = str(agent_session.get("value") or "")
    if session_id and started_session_id != session_id:
        raise HerdrError(
            f"Codex resumed session {started_session_id}, expected {session_id}"
        )
    if not SESSION_SUFFIX.fullmatch(f" codex resume {started_session_id}"):
        raise HerdrError("the started Codex agent returned an invalid session id")
    return started_session_id


def focus_tab_and_window(herdr: Herdr, tab_id: str) -> None:
    herdr.run_json(["tab", "focus", tab_id])
    herdr.show_window()


def close_new_tab(herdr: Herdr, tab_id: str) -> str | None:
    try:
        herdr.run_json(["tab", "close", tab_id])
    except HerdrError as exc:
        return str(exc)
    return None


def kickoff(todo: Todo, inbox_path: Path, herdr: Herdr) -> dict:
    snapshot = herdr.ensure_snapshot()
    if todo.session_id:
        matching_panes = find_live_session_panes(herdr, snapshot, todo.session_id)
        if len(matching_panes) > 1:
            raise HerdrError(
                f"multiple Herdr tabs own Codex session {todo.session_id}"
            )
        if matching_panes:
            tab_id = str(matching_panes[0]["tab_id"])
            focus_tab_and_window(herdr, tab_id)
            return {
                "action": "focused-existing",
                "session_id": todo.session_id,
                "tab_id": tab_id,
                "task": todo.task,
            }

    tab_id, pane_id = create_todo_tab(herdr, snapshot, todo.task)
    initial_prompt = (
        None
        if todo.session_id
        else f"Use $execute-todo for this inbox item:\n\n{todo.task}"
    )
    try:
        session_id = start_codex(
            herdr,
            pane_id,
            todo.session_id,
            initial_prompt,
        )
    except Exception:
        cleanup_error = close_new_tab(herdr, tab_id)
        if cleanup_error:
            raise HerdrError(f"Codex failed to start; tab cleanup also failed: {cleanup_error}")
        raise

    if todo.session_id:
        focus_tab_and_window(herdr, tab_id)
        return {
            "action": "resumed",
            "session_id": session_id,
            "tab_id": tab_id,
            "task": todo.task,
        }

    try:
        add_session_annotation(inbox_path, todo, session_id)
    except Exception:
        cleanup_error = close_new_tab(herdr, tab_id)
        if cleanup_error:
            raise RuntimeError(
                f"the inbox annotation failed; tab cleanup also failed: {cleanup_error}"
            )
        raise

    focus_tab_and_window(herdr, tab_id)
    return {
        "action": "created-and-prompted",
        "session_id": session_id,
        "tab_id": tab_id,
        "task": todo.task,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open or resume a random inbox todo in an interactive Herdr Codex tab."
    )
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX)
    parser.add_argument("--herdr-bin", default=DEFAULT_HERDR_BIN)
    parser.add_argument("--herdr-launcher", default=DEFAULT_HERDR_LAUNCHER)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    args.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with args.lock_file.open("w", encoding="utf-8") as lock_file:
        if not args.dry_run:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        todo = random.SystemRandom().choice(load_todos(args.inbox))
        if args.dry_run:
            print(json.dumps({"action": "dry-run", **asdict(todo)}, sort_keys=True))
            return 0
        result = kickoff(
            todo,
            args.inbox,
            Herdr(args.herdr_bin, args.herdr_launcher),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
