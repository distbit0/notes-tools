#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

from notes_utils import append_markdown_lines, configure_logger


NOTES_FILE = Path.home() / "notes/inbox-index.md"
LOG_PATH = Path(__file__).with_name("chatgpt-pending-convos-to-notes.log")
STATE_PATH = Path.home() / ".local/state/chatgpt-pending-convos-to-notes.json"
STATE_SCHEMA = 1
PREFIX_LENGTH = 128


def current_epoch_ms() -> int:
    return int(time.time() * 1000)


def collapse_prefix(text: str) -> str:
    collapsed = " ".join(text.split())
    if not collapsed:
        raise RuntimeError("conversation title is empty")
    if len(collapsed) <= PREFIX_LENGTH:
        return collapsed
    return f"{collapsed[: PREFIX_LENGTH - 3].rstrip()}..."


def empty_state() -> dict[str, Any]:
    return {"schema": STATE_SCHEMA, "pending": {}, "appended": {}}


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return empty_state()
    try:
        stored_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ChatGPT conversation state is invalid: {STATE_PATH}") from exc
    if not isinstance(stored_state, dict):
        raise RuntimeError(f"ChatGPT conversation state is not an object: {STATE_PATH}")
    if stored_state.get("schema") != STATE_SCHEMA:
        raise RuntimeError(
            f"Unsupported ChatGPT conversation state schema: {stored_state.get('schema')}"
        )
    if not isinstance(stored_state.get("pending"), dict) or not isinstance(
        stored_state.get("appended"), dict
    ):
        raise RuntimeError(f"ChatGPT conversation state has invalid maps: {STATE_PATH}")
    return {
        "schema": STATE_SCHEMA,
        "pending": {
            key: record
            for key, record in stored_state["pending"].items()
            if key.startswith("chatgpt:") and isinstance(record, dict)
        },
        "appended": {
            key: record
            for key, record in stored_state["appended"].items()
            if key.startswith("chatgpt:")
        },
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=STATE_PATH.parent, delete=False
    ) as temp_file:
        json.dump(state, temp_file, indent=2, sort_keys=True)
        temp_file.write("\n")
        temp_path = Path(temp_file.name)
    temp_path.replace(STATE_PATH)


def escape_markdown_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


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
        "reason": reason,
        "conversation_id": conversation_id,
        "latest_message_id": latest_message_id,
        "link": f"https://chatgpt.com/c/{conversation_id}",
        "title": collapse_prefix(title),
    }


def read_pending_records() -> list[dict[str, Any]]:
    try:
        raw_records = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Pending ChatGPT records are invalid JSON") from exc
    if not isinstance(raw_records, list):
        raise RuntimeError("Pending ChatGPT records JSON root is not a list")
    if not all(isinstance(record, dict) for record in raw_records):
        raise RuntimeError("Pending ChatGPT records contain a non-object")
    return [validate_chatgpt_record(record) for record in raw_records]


def update_pending_records(
    state: dict[str, Any], records: list[dict[str, Any]]
) -> None:
    now_ms = current_epoch_ms()
    for record in records:
        previous_record = state["pending"].get(record["key"], {})
        state["pending"][record["key"]] = {
            **record,
            "created_at_ms": previous_record.get("created_at_ms", now_ms),
            "updated_at_ms": now_ms,
        }


def unappended_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (
            record
            for key, record in state["pending"].items()
            if key not in state["appended"]
        ),
        key=lambda record: record["created_at_ms"],
    )


def markdown_line(record: dict[str, Any]) -> str:
    label = escape_markdown_label(record["title"])
    return f"maybe pending chatgpt convo: [{label}]({record['link']})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Append unread or interrupted ChatGPT conversations to notes."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    configure_logger(LOG_PATH)

    state = load_state()
    update_pending_records(state, read_pending_records())
    records = unappended_records(state)
    if args.dry_run:
        for record in records:
            print(markdown_line(record))
        return 0

    save_state(state)
    append_markdown_lines(NOTES_FILE, [markdown_line(record) for record in records])
    acknowledged_at_ms = current_epoch_ms()
    for record in records:
        state["appended"][record["key"]] = acknowledged_at_ms
        del state["pending"][record["key"]]
    save_state(state)
    logger.info(f"Appended {len(records)} ChatGPT conversation link(s)")
    print(json.dumps({"appended": len(records)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
