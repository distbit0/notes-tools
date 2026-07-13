#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

import assistant_convo_state
from notes_utils import append_markdown_lines, configure_logger


NOTES_FILE = Path.home() / "notes/inbox-index.md"
LOG_PATH = Path(__file__).with_name("assistant-convos-to-notes.log")
CHATGPT_FETCHER_PATH = Path(__file__).with_name("chatgpt_backend_fetch.mjs")
ASSISTANT_NOTIFICATION_ACTIVATIONS_PATH = (
    Path.home() / ".local/state/assistant-convo-notification-activations.jsonl"
)
BRAVE_COOKIES_PATH = (
    Path.home() / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
)


def escape_markdown_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def message_label_for_record(record: dict[str, Any]) -> str:
    return escape_markdown_label(str(record["first_message_prefix"]))


def codex_resume_command(record: dict[str, Any]) -> str:
    thread_id = str(record.get("thread_id") or "").strip()
    cwd = str(record.get("cwd") or "").strip()
    if not thread_id:
        raise RuntimeError(f"Codex pending record lacks thread_id: {record.get('key')}")
    if not cwd:
        raise RuntimeError(f"Codex pending record lacks cwd: {record.get('key')}")
    return f"cd {shlex.quote(cwd)} && codex resume {shlex.quote(thread_id)}"


def markdown_line_for_record(record: dict[str, Any]) -> str:
    source = record.get("source")
    label = message_label_for_record(record)
    if source == "codex":
        if record.get("reason") == "stalled":
            return f"possibly stalled codex convo: {label}\n{codex_resume_command(record)}"
        raise RuntimeError(
            f"non-appendable Codex record reached formatter: {record.get('key')}"
        )
    if source == "chatgpt":
        return f"maybe pending chatgpt convo: [{label}]({record['link']})"
    raise RuntimeError(f"unknown assistant conversation source: {source}")


def validate_chatgpt_record(record: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(record.get("conversationId") or "").strip()
    if not conversation_id:
        raise RuntimeError("ChatGPT record is missing conversationId")
    reason = str(record.get("reason") or "").strip()
    if reason not in {"unread", "cut_off"}:
        raise RuntimeError(f"ChatGPT record has invalid reason: {reason}")
    title = str(record.get("title") or "").strip()
    if not title:
        raise RuntimeError(f"ChatGPT record has empty title: {conversation_id}")
    latest_message_id = str(record.get("latestMessageId") or "").strip()
    key_suffix = latest_message_id or reason
    return {
        "key": f"chatgpt:{conversation_id}:{reason}:{key_suffix}",
        "source": "chatgpt",
        "reason": reason,
        "conversation_id": conversation_id,
        "latest_message_id": latest_message_id,
        "link": f"https://chatgpt.com/c/{conversation_id}",
        "first_message_prefix": assistant_convo_state.collapse_prefix(title),
    }


def load_chatgpt_cookie_header() -> str:
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError(
            "browser-cookie3 is not installed; run `uv sync` in "
            f"{Path.home() / 'dev/notes-tools'}"
        ) from exc

    if not BRAVE_COOKIES_PATH.exists():
        raise RuntimeError(f"Brave cookies database not found: {BRAVE_COOKIES_PATH}")

    cookie_jar = browser_cookie3.brave(
        cookie_file=str(BRAVE_COOKIES_PATH),
        domain_name="chatgpt.com",
    )
    cookie_parts = [
        f"{cookie.name}={cookie.value}"
        for cookie in cookie_jar
        if "chatgpt.com" in cookie.domain
    ]
    if not cookie_parts:
        raise RuntimeError("No ChatGPT cookies found in Brave Default profile")
    return "; ".join(cookie_parts)


def fetch_chatgpt_pending_records() -> list[dict[str, Any]]:
    if not shutil.which("node"):
        raise RuntimeError("node not found in PATH")
    if not CHATGPT_FETCHER_PATH.exists():
        raise RuntimeError(f"ChatGPT fetcher not found: {CHATGPT_FETCHER_PATH}")

    cookie_header = load_chatgpt_cookie_header()
    result = subprocess.run(
        ["node", str(CHATGPT_FETCHER_PATH)],
        input=json.dumps({"cookieHeader": cookie_header}),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ChatGPT fetch failed: {error or 'unknown error'}")
    if result.stderr.strip():
        logger.warning(result.stderr.strip())

    try:
        raw_records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ChatGPT fetcher returned invalid JSON") from exc
    if not isinstance(raw_records, list):
        raise RuntimeError("ChatGPT fetcher JSON root is not a list")

    validated_records: list[dict[str, Any]] = []
    for record in raw_records:
        if not isinstance(record, dict):
            raise RuntimeError("ChatGPT fetcher returned a non-object record")
        validated_records.append(validate_chatgpt_record(record))
    return validated_records


def fetch_assistant_notification_activation_ids(
    path: Path = ASSISTANT_NOTIFICATION_ACTIVATIONS_PATH,
) -> frozenset[int]:
    if not path.exists():
        return frozenset()

    activated_ids: set[int] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            logger.warning(f"Skipping invalid activation ledger line in {path}")
            continue
        if not isinstance(record, dict) or record.get("source") != "codex":
            continue
        notification_id = record.get("notification_id")
        if isinstance(notification_id, int) and notification_id > 0:
            activated_ids.add(notification_id)
    return frozenset(activated_ids)


def maybe_codex_notification_id(record: dict[str, Any]) -> int | None:
    raw_notification_id = record.get("notification_id")
    if raw_notification_id is None:
        return None
    if not isinstance(raw_notification_id, int) or raw_notification_id <= 0:
        raise RuntimeError(
            f"Codex pending record has invalid notification_id: {record.get('key')}"
        )
    return raw_notification_id


def acknowledge_activated_codex_records(
    state: dict[str, Any],
    activated_ids: frozenset[int],
) -> int:
    keys_to_acknowledge: list[str] = []
    for key, record in state["pending"].items():
        if not isinstance(record, dict) or record.get("source") != "codex":
            continue
        notification_id = maybe_codex_notification_id(record)
        if notification_id is not None and notification_id in activated_ids:
            keys_to_acknowledge.append(key)

    assistant_convo_state.acknowledge_pending_keys(
        state,
        keys_to_acknowledge,
        reason="notification_activated",
    )
    for key in keys_to_acknowledge:
        del state["pending"][key]
    return len(keys_to_acknowledge)


def should_append_record(
    record: dict[str, Any],
    *,
    now_ms: int,
) -> bool:
    if record.get("source") != "codex":
        return True
    return record.get("reason") == "stalled"


def collect_markdown_lines(
    state: dict[str, Any],
    *,
    activated_ids: frozenset[int] | None = None,
    now_ms: int | None = None,
) -> tuple[list[str], list[str]]:
    now_ms = assistant_convo_state.current_epoch_ms() if now_ms is None else now_ms
    records = assistant_convo_state.unappended_pending_records(state)
    if activated_ids is None and any(
        record.get("source") == "codex"
        and maybe_codex_notification_id(record) is not None
        for record in records
    ):
        activated_ids = fetch_assistant_notification_activation_ids()
    if activated_ids is not None:
        acknowledge_activated_codex_records(state, activated_ids)
        records = assistant_convo_state.unappended_pending_records(state)
    records = [
        record
        for record in records
        if should_append_record(record, now_ms=now_ms)
    ]
    keys = [str(record["key"]) for record in records]
    lines = [markdown_line_for_record(record) for record in records]
    return lines, keys


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append unread/cut-off Codex and ChatGPT conversations to notes."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-chatgpt",
        action="store_true",
        help="Only process locally tracked Codex pending conversations.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    configure_logger(LOG_PATH)

    state_path = assistant_convo_state.STATE_PATH
    state = assistant_convo_state.load_state(state_path)

    if args.dry_run:
        working_state = copy.deepcopy(state)
    else:
        working_state = state

    if not args.skip_chatgpt:
        chatgpt_records = fetch_chatgpt_pending_records()
        if args.dry_run:
            working_state["pending"] = {
                key: record
                for key, record in working_state["pending"].items()
                if not (
                    isinstance(record, dict) and record.get("source") == "chatgpt"
                )
            }
            for record in chatgpt_records:
                working_state["pending"][record["key"]] = record
        else:
            assistant_convo_state.replace_chatgpt_pending(
                chatgpt_records,
                state_path=state_path,
            )
            working_state = assistant_convo_state.load_state(state_path)

    assistant_convo_state.sync_codex_pending_from_sessions(working_state)
    if not args.dry_run:
        assistant_convo_state.save_state(working_state, state_path)

    lines, keys = collect_markdown_lines(working_state)
    if args.dry_run:
        for line in lines:
            print(line)
        return 0

    assistant_convo_state.save_state(working_state, state_path)
    append_markdown_lines(NOTES_FILE, lines)
    assistant_convo_state.mark_appended(working_state, keys, state_path=state_path)
    logger.info(f"Appended {len(lines)} assistant conversation link(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
