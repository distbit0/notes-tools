from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STATE_PATH = Path.home() / ".local/state/assistant-convos-to-notes.json"
CODEX_STATE_DB_PATH = Path.home() / ".codex/state_5.sqlite"
STATE_SCHEMA = 1
PREFIX_LENGTH = 128
CODEX_PENDING_SCAN_LIMIT = 200
CODEX_STALLED_TIMEOUT_MS = 15 * 60 * 1000
CODEX_WATCHER_STATE_DIR = Path("/tmp/codex_notify_watchers")
INTERACTIVE_CODEX_SOURCES = {"cli", "vscode"}


@dataclass(frozen=True)
class CodexSessionMessage:
    role: str
    offset: int
    phase: str
    text: str


@dataclass(frozen=True)
class CodexSessionMessages:
    latest_message: CodexSessionMessage | None
    latest_meaningful_user_message: CodexSessionMessage | None
    latest_meaningful_activity_offset: int


def current_epoch_ms() -> int:
    return int(time.time() * 1000)


def collapse_prefix(text: str, *, max_length: int = PREFIX_LENGTH) -> str:
    collapsed = " ".join(text.split())
    if not collapsed:
        raise RuntimeError("conversation first message is empty")
    if len(collapsed) <= max_length:
        return collapsed
    return f"{collapsed[: max_length - 3].rstrip()}..."


def empty_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "pending": {},
        "appended": {},
        "codex_scan_started_at_ms": current_epoch_ms(),
    }


def validate_state(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("schema") != STATE_SCHEMA:
        raise RuntimeError(
            f"unsupported assistant convo state schema: {state.get('schema')}"
        )
    if not isinstance(state.get("pending"), dict):
        raise RuntimeError("assistant convo state has invalid pending map")
    if not isinstance(state.get("appended"), dict):
        raise RuntimeError("assistant convo state has invalid appended map")
    return state


def codex_scan_started_at_ms(state: dict[str, Any]) -> int:
    raw_value = state.get("codex_scan_started_at_ms")
    if isinstance(raw_value, int) and raw_value >= 0:
        return raw_value
    started_at_ms = current_epoch_ms()
    state["codex_scan_started_at_ms"] = started_at_ms
    return started_at_ms


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"assistant convo state is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"assistant convo state root is not an object: {path}")
    return validate_state(data)


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    validate_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def codex_thread_info(
    thread_id: str,
    *,
    database_path: Path = CODEX_STATE_DB_PATH,
) -> dict[str, str]:
    thread_id = thread_id.strip()
    if not thread_id:
        raise RuntimeError("codex thread id is empty")
    if not database_path.exists():
        raise RuntimeError(f"Codex state database not found: {database_path}")

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            """
            select cwd, first_user_message, title, preview, rollout_path, source
            from threads
            where id = ?
            """,
            (thread_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise RuntimeError(f"Codex thread not found in state database: {thread_id}")

    cwd, first_user_message, title, preview, rollout_path, source = row
    cwd = str(cwd or "").strip()
    first_user_message = str(first_user_message or "").strip()
    if not cwd:
        raise RuntimeError(f"Codex thread has no cwd: {thread_id}")
    if not first_user_message:
        raise RuntimeError(f"Codex thread has no first user message: {thread_id}")
    return {
        "cwd": cwd,
        "first_user_message": first_user_message,
        "title": str(title or "").strip(),
        "preview": str(preview or "").strip(),
        "rollout_path": str(rollout_path or "").strip(),
        "source": str(source or "").strip(),
    }


def codex_recent_threads(
    *,
    database_path: Path = CODEX_STATE_DB_PATH,
    limit: int = CODEX_PENDING_SCAN_LIMIT,
) -> list[dict[str, str]]:
    if limit <= 0:
        return []
    if not database_path.exists():
        raise RuntimeError(f"Codex state database not found: {database_path}")

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            select
                id,
                cwd,
                first_user_message,
                title,
                preview,
                rollout_path,
                source,
                updated_at_ms
            from threads
            where archived = 0
            order by updated_at_ms desc, updated_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    return [
        {
            "thread_id": str(thread_id or "").strip(),
            "cwd": str(cwd or "").strip(),
            "first_user_message": str(first_user_message or "").strip(),
            "title": str(title or "").strip(),
            "preview": str(preview or "").strip(),
            "rollout_path": str(rollout_path or "").strip(),
            "source": str(source or "").strip(),
            "updated_at_ms": str(int(updated_at_ms or 0)),
        }
        for (
            thread_id,
            cwd,
            first_user_message,
            title,
            preview,
            rollout_path,
            source,
            updated_at_ms,
        ) in rows
        if str(thread_id or "").strip()
    ]


def is_interactive_codex_thread(thread_info: dict[str, str]) -> bool:
    return thread_info.get("source", "").strip() in INTERACTIVE_CODEX_SOURCES


def message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        item.get("text")
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "".join(parts).strip()


def meaningful_codex_user_text(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("<turn_aborted>"):
        return stripped
    if "</turn_aborted>" not in stripped:
        return ""
    return stripped.split("</turn_aborted>", 1)[1].strip()


def latest_codex_session_messages(rollout_path: str | Path) -> CodexSessionMessages:
    path = Path(rollout_path)
    if not path.exists():
        return CodexSessionMessages(None, None, 0)

    latest_message: CodexSessionMessage | None = None
    latest_meaningful_user_message: CodexSessionMessage | None = None
    latest_meaningful_activity_offset = 0
    offset = 0
    with path.open("rb") as handle:
        for raw_line in handle:
            offset += len(raw_line)
            try:
                record = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")
            if payload_type in {"function_call", "function_call_output"}:
                latest_meaningful_activity_offset = offset
                continue
            if payload_type != "message":
                continue
            role = str(payload.get("role") or "").strip()
            if role not in {"assistant", "user"}:
                continue
            phase = str(payload.get("phase") or "").strip()
            latest_message = CodexSessionMessage(
                role=role,
                offset=offset,
                phase=phase,
                text=message_text(payload),
            )
            if role == "user":
                user_text = meaningful_codex_user_text(latest_message.text)
                if user_text:
                    latest_meaningful_activity_offset = offset
                    latest_meaningful_user_message = CodexSessionMessage(
                        role=role,
                        offset=offset,
                        phase=phase,
                        text=user_text,
                    )
            else:
                latest_meaningful_activity_offset = offset
    return CodexSessionMessages(
        latest_message,
        latest_meaningful_user_message,
        latest_meaningful_activity_offset,
    )


def latest_codex_session_message(rollout_path: str | Path) -> CodexSessionMessage | None:
    return latest_codex_session_messages(rollout_path).latest_message


def codex_message_key(thread_id: str, message: CodexSessionMessage) -> str:
    return f"codex:{thread_id}:assistant:{message.offset}"


def codex_stalled_key(thread_id: str, message: CodexSessionMessage) -> str:
    return f"codex:{thread_id}:stalled:{message.offset}"


def legacy_codex_thread_key(thread_id: str) -> str:
    return f"codex:{thread_id}:unread"


def is_codex_message_acknowledged(
    state: dict[str, Any],
    *,
    thread_id: str,
    message_key: str,
) -> bool:
    appended = state["appended"]
    return message_key in appended or legacy_codex_thread_key(thread_id) in appended


def codex_pending_record_for_thread(
    thread_info: dict[str, str],
    latest_message: CodexSessionMessage | None = None,
    latest_user_message: CodexSessionMessage | None = None,
) -> dict[str, Any] | None:
    thread_id = thread_info.get("thread_id") or thread_info.get("id") or ""
    thread_id = thread_id.strip()
    rollout_path = thread_info.get("rollout_path", "").strip()
    first_user_message = thread_info.get("first_user_message", "").strip()
    cwd = thread_info.get("cwd", "").strip()
    if not thread_id or not rollout_path or not first_user_message or not cwd:
        return None

    if latest_message is None or latest_user_message is None:
        session_messages = latest_codex_session_messages(rollout_path)
        latest_message = latest_message or session_messages.latest_message
        latest_user_message = (
            latest_user_message or session_messages.latest_meaningful_user_message
        )
    if (
        latest_message is None
        or latest_message.role != "assistant"
        or latest_message.phase != "final_answer"
    ):
        return None

    key = codex_message_key(thread_id, latest_message)
    label_source = first_user_message
    if latest_user_message is not None:
        label_source = latest_user_message.text
    return {
        "key": key,
        "source": "codex",
        "codex_source": thread_info.get("source", "").strip(),
        "reason": "unread",
        "first_message_prefix": collapse_prefix(label_source),
        "thread_id": thread_id,
        "agent_message_offset": latest_message.offset,
        "agent_message_phase": latest_message.phase,
        "cwd": cwd,
    }


def codex_stalled_record_for_thread(
    thread_info: dict[str, str],
    latest_message: CodexSessionMessage | None = None,
    latest_user_message: CodexSessionMessage | None = None,
    latest_meaningful_activity_offset: int | None = None,
) -> dict[str, Any] | None:
    thread_id = thread_info.get("thread_id") or thread_info.get("id") or ""
    thread_id = thread_id.strip()
    rollout_path = thread_info.get("rollout_path", "").strip()
    first_user_message = thread_info.get("first_user_message", "").strip()
    cwd = thread_info.get("cwd", "").strip()
    if not thread_id or not rollout_path or not first_user_message or not cwd:
        return None

    if latest_message is None or latest_user_message is None:
        session_messages = latest_codex_session_messages(rollout_path)
        latest_message = latest_message or session_messages.latest_message
        latest_user_message = (
            latest_user_message or session_messages.latest_meaningful_user_message
        )
        latest_meaningful_activity_offset = (
            latest_meaningful_activity_offset
            or session_messages.latest_meaningful_activity_offset
        )
    if latest_message is None or latest_message.role != "user":
        return None
    if latest_user_message is None:
        return None
    if latest_message.offset != latest_user_message.offset:
        return None
    if latest_meaningful_activity_offset != latest_user_message.offset:
        return None

    return {
        "key": codex_stalled_key(thread_id, latest_user_message),
        "source": "codex",
        "codex_source": thread_info.get("source", "").strip(),
        "reason": "stalled",
        "first_message_prefix": collapse_prefix(latest_user_message.text),
        "thread_id": thread_id,
        "user_message_offset": latest_user_message.offset,
        "cwd": cwd,
    }


def acknowledge_pending_keys(
    state: dict[str, Any],
    keys: list[str],
    *,
    reason: str,
) -> None:
    now_ms = current_epoch_ms()
    for key in keys:
        state["appended"][key] = {"acknowledged_at_ms": now_ms, "reason": reason}


def acknowledge_codex_thread_pending(
    state: dict[str, Any],
    thread_id: str,
    *,
    reason: str,
    pending_reasons: set[str] | None = None,
) -> int:
    pending = state["pending"]
    keys_to_acknowledge = [
        key
        for key, record in pending.items()
        if isinstance(record, dict)
        and record.get("source") == "codex"
        and record.get("thread_id") == thread_id
        and (pending_reasons is None or record.get("reason") in pending_reasons)
    ]
    acknowledge_pending_keys(state, keys_to_acknowledge, reason=reason)
    for key in keys_to_acknowledge:
        del pending[key]
    return len(keys_to_acknowledge)


def remove_codex_records_without_notification(state: dict[str, Any]) -> int:
    pending = state["pending"]
    keys_to_remove: list[str] = []
    for key, record in pending.items():
        if (
            not isinstance(record, dict)
            or record.get("source") != "codex"
            or record.get("reason") != "unread"
        ):
            continue
        notification_id = record.get("notification_id")
        if notification_id is None:
            keys_to_remove.append(key)
            continue
        if not isinstance(notification_id, int) or notification_id <= 0:
            raise RuntimeError(f"invalid Codex notification id in pending record: {key}")
    for key in keys_to_remove:
        del pending[key]
    return len(keys_to_remove)


def has_codex_pending_for_thread(state: dict[str, Any], thread_id: str) -> bool:
    return any(
        isinstance(record, dict)
        and record.get("source") == "codex"
        and record.get("thread_id") == thread_id
        for record in state["pending"].values()
    )


def _read_pid_file(path: Path) -> int | None:
    try:
        raw_pid = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw_pid)
    except ValueError:
        return None


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watcher_pid_path(thread_id: str, watcher_dir: Path) -> Path:
    return watcher_dir / f"{thread_id}.pid"


def codex_watcher_is_active(thread_id: str, watcher_dir: Path) -> bool:
    if not thread_id:
        return False
    pid_path = _watcher_pid_path(thread_id, watcher_dir)
    if not pid_path.exists():
        return False

    pid = _read_pid_file(pid_path)
    if pid is not None and _process_is_alive(pid):
        return True

    try:
        pid_path.unlink()
    except OSError:
        pass
    return False


def process_has_file_open(file_path: Path, proc_root: Path = Path("/proc")) -> bool:
    target = str(file_path)
    if not target:
        return False
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return False

    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd_path in fds:
            try:
                link_target = os.readlink(fd_path)
            except OSError:
                continue
            if link_target == target:
                return True
    return False


def codex_thread_is_active(
    thread_id: str,
    rollout_path: str | Path,
    *,
    watcher_dir: Path = CODEX_WATCHER_STATE_DIR,
    proc_root: Path = Path("/proc"),
) -> bool:
    if codex_watcher_is_active(thread_id, watcher_dir):
        return True
    return process_has_file_open(Path(rollout_path), proc_root=proc_root)


def thread_is_old_enough_for_stalled(
    thread_updated_at_ms: int,
    now_ms: int,
    *,
    timeout_ms: int = CODEX_STALLED_TIMEOUT_MS,
) -> bool:
    return thread_updated_at_ms > 0 and now_ms - thread_updated_at_ms >= timeout_ms


def sync_codex_pending_from_sessions(
    state: dict[str, Any],
    *,
    database_path: Path = CODEX_STATE_DB_PATH,
    limit: int = CODEX_PENDING_SCAN_LIMIT,
    now_ms: int | None = None,
    watcher_dir: Path = CODEX_WATCHER_STATE_DIR,
    proc_root: Path = Path("/proc"),
) -> int:
    validate_state(state)
    now_ms = current_epoch_ms() if now_ms is None else now_ms
    pending = state["pending"]
    scan_started_at_ms = codex_scan_started_at_ms(state)
    scanned_thread_ids: set[str] = set()
    current_codex_keys: set[str] = set()

    remove_codex_records_without_notification(state)

    for thread_info in codex_recent_threads(database_path=database_path, limit=limit):
        thread_id = thread_info["thread_id"]
        thread_updated_at_ms = int(thread_info.get("updated_at_ms") or 0)
        if (
            thread_updated_at_ms < scan_started_at_ms
            and not has_codex_pending_for_thread(state, thread_id)
        ):
            continue
        scanned_thread_ids.add(thread_id)
        if not is_interactive_codex_thread(thread_info):
            acknowledge_codex_thread_pending(
                state,
                thread_id,
                reason="non_interactive_source",
            )
            continue
        session_messages = latest_codex_session_messages(thread_info["rollout_path"])
        latest_message = session_messages.latest_message
        latest_user_message = session_messages.latest_meaningful_user_message
        latest_meaningful_activity_offset = (
            session_messages.latest_meaningful_activity_offset
        )
        if latest_message and latest_message.role == "user":
            acknowledge_codex_thread_pending(
                state,
                thread_id,
                reason="user_reply",
                pending_reasons={"unread"},
            )
            if not thread_is_old_enough_for_stalled(thread_updated_at_ms, now_ms):
                continue
            if codex_thread_is_active(
                thread_id,
                thread_info["rollout_path"],
                watcher_dir=watcher_dir,
                proc_root=proc_root,
            ):
                continue
            record = codex_stalled_record_for_thread(
                thread_info,
                latest_message,
                latest_user_message,
                latest_meaningful_activity_offset,
            )
            if record is None:
                continue
            key = record["key"]
            current_codex_keys.add(key)
            if key in state["appended"]:
                continue
            existing_record = pending.get(key)
            record["created_at_ms"] = (
                existing_record.get("created_at_ms", now_ms)
                if isinstance(existing_record, dict)
                else now_ms
            )
            record["updated_at_ms"] = now_ms
            pending[key] = record
            continue
        if (
            latest_message is None
            or latest_message.role != "assistant"
            or latest_message.phase != "final_answer"
        ):
            continue

        record = codex_pending_record_for_thread(
            thread_info,
            latest_message,
            latest_user_message,
        )
        if record is None:
            continue
        key = record["key"]
        current_codex_keys.add(key)
        acknowledge_codex_thread_pending(
            state,
            thread_id,
            reason="agent_reply",
            pending_reasons={"stalled"},
        )
        if is_codex_message_acknowledged(
            state,
            thread_id=thread_id,
            message_key=key,
        ):
            continue

        existing_record = pending.get(key)
        if not isinstance(existing_record, dict):
            existing_record = pending.get(legacy_codex_thread_key(thread_id))
        if not isinstance(existing_record, dict):
            continue
        existing_key = str(existing_record.get("key") or key)
        notification_id = existing_record.get("notification_id")
        if notification_id is None:
            pending.pop(existing_key, None)
            continue
        if not isinstance(notification_id, int) or notification_id <= 0:
            raise RuntimeError(
                f"invalid Codex notification id in pending record: {existing_key}"
            )
        record = {**existing_record, **record}
        record["notification_id"] = notification_id
        record["created_at_ms"] = existing_record.get("created_at_ms", now_ms)
        record["updated_at_ms"] = now_ms
        pending[key] = record

    for key, record in list(pending.items()):
        if not isinstance(record, dict) or record.get("source") != "codex":
            continue
        if record.get("thread_id") in scanned_thread_ids and key not in current_codex_keys:
            del pending[key]

    return len(current_codex_keys)


def record_codex_pending(
    thread_id: str,
    *,
    notification_id: int,
    reason: str = "unread",
    state_path: Path = STATE_PATH,
    database_path: Path = CODEX_STATE_DB_PATH,
) -> str:
    if notification_id <= 0:
        raise RuntimeError(f"invalid Codex notification id: {notification_id}")
    if reason != "unread":
        raise RuntimeError(f"unsupported Codex pending reason: {reason}")

    thread_info = {
        **codex_thread_info(thread_id, database_path=database_path),
        "thread_id": thread_id,
    }
    if not is_interactive_codex_thread(thread_info):
        return ""
    record = codex_pending_record_for_thread(thread_info)
    if record is None:
        raise RuntimeError(f"Codex thread has no final assistant message: {thread_id}")

    state = load_state(state_path)
    if is_codex_message_acknowledged(
        state,
        thread_id=thread_id,
        message_key=record["key"],
    ):
        return record["key"]

    now_ms = current_epoch_ms()
    existing_record = state["pending"].get(record["key"])
    created_at_ms = (
        existing_record.get("created_at_ms", now_ms)
        if isinstance(existing_record, dict)
        else now_ms
    )
    state["pending"][record["key"]] = {
        **record,
        "notification_id": notification_id,
        "created_at_ms": created_at_ms,
        "updated_at_ms": now_ms,
    }
    save_state(state, state_path)
    return record["key"]


def mark_codex_thread_read(
    thread_id: str,
    *,
    state_path: Path = STATE_PATH,
) -> int:
    state = load_state(state_path)
    removed_count = acknowledge_codex_thread_pending(
        state,
        thread_id,
        reason="user_reply",
    )
    if removed_count:
        save_state(state, state_path)
    return removed_count


def replace_chatgpt_pending(
    records: list[dict[str, Any]],
    *,
    state_path: Path = STATE_PATH,
) -> list[str]:
    state = load_state(state_path)
    pending = state["pending"]
    for key, record in list(pending.items()):
        if isinstance(record, dict) and record.get("source") == "chatgpt":
            del pending[key]

    now_ms = current_epoch_ms()
    keys: list[str] = []
    for record in records:
        key = str(record["key"])
        existing_record = pending.get(key)
        created_at_ms = (
            existing_record.get("created_at_ms", now_ms)
            if isinstance(existing_record, dict)
            else now_ms
        )
        pending[key] = {
            **record,
            "key": key,
            "source": "chatgpt",
            "created_at_ms": created_at_ms,
            "updated_at_ms": now_ms,
        }
        keys.append(key)

    save_state(state, state_path)
    return keys


def unappended_pending_records(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    validate_state(state)
    appended = state["appended"]
    records = [
        record
        for key, record in state["pending"].items()
        if key not in appended and isinstance(record, dict)
    ]
    return sorted(records, key=lambda record: record.get("created_at_ms", 0))


def mark_appended(
    state: dict[str, Any],
    keys: list[str],
    *,
    state_path: Path = STATE_PATH,
) -> None:
    if not keys:
        return
    acknowledge_pending_keys(state, keys, reason="appended")
    save_state(state, state_path)
