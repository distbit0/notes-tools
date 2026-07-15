from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup
from loguru import logger


BRAVE_COOKIES_PATH = (
    Path.home() / ".config/BraveSoftware/Brave-Browser/Default/Cookies"
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
STATE_BUCKETS = (
    "x_reply",
    "x_dm",
    "lesswrong_notification",
    "lesswrong_dm",
    "ethresearch_notification",
    "ethresearch_pm",
)
TRANSIENT_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
GET_ATTEMPTS = 3
GET_RETRY_DELAY_SECONDS = 2


@dataclass(frozen=True)
class BrowserCookies:
    header: str
    values_by_name: dict[str, str]


@dataclass(frozen=True)
class ItemCursor:
    record_key: str
    timestamp_ms: int
    item_id: str


@dataclass(frozen=True)
class SocialNotification:
    source: str
    kind: str
    label: str
    url: str
    cursor: ItemCursor


def require_non_empty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{field_name} is missing")
    return value.strip()


def parse_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        raise RuntimeError(f"Invalid {field_name}: {value}")
    if parsed < 0:
        raise RuntimeError(f"{field_name} must be non-negative: {value}")
    return parsed


def parse_iso_timestamp_ms(value: Any, *, field_name: str) -> int:
    timestamp = require_non_empty_string(value, field_name=field_name)
    if timestamp.endswith("Z"):
        timestamp = f"{timestamp[:-1]}+00:00"
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def cursor_sort_key(cursor: ItemCursor) -> tuple[int, str]:
    return cursor.timestamp_ms, cursor.item_id


def is_newer_than_cursor(cursor: ItemCursor, previous: ItemCursor | None) -> bool:
    return previous is None or cursor_sort_key(cursor) > cursor_sort_key(previous)


def empty_state() -> dict[str, dict[str, ItemCursor]]:
    return {bucket: {} for bucket in STATE_BUCKETS}


def parse_cursor(record_key: str, payload: Any) -> ItemCursor:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid state record for {record_key}")

    stored_record_key = payload.get("record_key")
    timestamp_ms = payload.get("latest_item_timestamp_ms")
    item_id = payload.get("latest_item_id")
    read_flag = payload.get("read")

    if stored_record_key != record_key:
        raise RuntimeError(f"State record key mismatch for {record_key}")
    if not isinstance(timestamp_ms, int) or timestamp_ms < 0:
        raise RuntimeError(f"Invalid latest_item_timestamp_ms for {record_key}")
    if not isinstance(item_id, str) or not item_id.strip():
        raise RuntimeError(f"Invalid latest_item_id for {record_key}")
    if read_flag is not True:
        raise RuntimeError(f"Invalid read flag for {record_key}")

    return ItemCursor(
        record_key=record_key,
        timestamp_ms=timestamp_ms,
        item_id=item_id,
    )


def load_state(path: Path) -> dict[str, dict[str, ItemCursor]]:
    if not path.exists():
        return empty_state()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON state file: {path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"State file must contain an object: {path}")

    state = empty_state()
    for bucket in STATE_BUCKETS:
        raw_bucket = payload.get(bucket, {})
        if not isinstance(raw_bucket, dict):
            raise RuntimeError(f"Invalid {bucket} state in {path}")
        state[bucket] = {
            str(record_key): parse_cursor(str(record_key), record)
            for record_key, record in raw_bucket.items()
        }

    return state


def serialize_state(
    state: dict[str, dict[str, ItemCursor]],
) -> dict[str, dict[str, dict[str, int | str | bool]]]:
    serialized: dict[str, dict[str, dict[str, int | str | bool]]] = {}
    for bucket in STATE_BUCKETS:
        serialized[bucket] = {
            record_key: {
                "record_key": cursor.record_key,
                "latest_item_timestamp_ms": cursor.timestamp_ms,
                "latest_item_id": cursor.item_id,
                "read": True,
            }
            for record_key, cursor in sorted(state[bucket].items())
        }
    return serialized


def save_state(path: Path, state: dict[str, dict[str, ItemCursor]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(serialize_state(state), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def update_state_cursor(
    state: dict[str, dict[str, ItemCursor]],
    bucket: str,
    cursor: ItemCursor,
) -> None:
    existing = state[bucket].get(cursor.record_key)
    if is_newer_than_cursor(cursor, existing):
        state[bucket][cursor.record_key] = cursor


def cookie_domain_matches(cookie_domain: str, hosts: Iterable[str]) -> bool:
    normalized_domain = cookie_domain.lstrip(".")
    return any(
        normalized_domain == host or normalized_domain.endswith(f".{host}")
        for host in hosts
    )


def load_brave_cookies(hosts: tuple[str, ...]) -> BrowserCookies:
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError(
            "browser-cookie3 is not installed; run `uv sync` in "
            f"{Path.home() / 'dev/misc'}"
        ) from exc

    if not BRAVE_COOKIES_PATH.exists():
        raise RuntimeError(f"Brave cookies database not found: {BRAVE_COOKIES_PATH}")

    cookie_records = {}
    for host in hosts:
        jar = browser_cookie3.brave(
            cookie_file=str(BRAVE_COOKIES_PATH),
            domain_name=host,
        )
        for cookie in jar:
            if not cookie.value or not cookie_domain_matches(cookie.domain, hosts):
                continue
            key = (cookie.domain, cookie.path, cookie.name)
            cookie_records[key] = cookie

    cookies = sorted(
        cookie_records.values(),
        key=lambda cookie: (cookie.domain, cookie.path, cookie.name),
    )
    if not cookies:
        raise RuntimeError(f"No Brave cookies found for {', '.join(hosts)}")

    values_by_name = {cookie.name: cookie.value for cookie in cookies}
    return BrowserCookies(
        header="; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies),
        values_by_name=values_by_name,
    )


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
) -> Any:
    attempts = GET_ATTEMPTS if method.upper() == "GET" else 1
    for attempt in range(1, attempts + 1):
        response = session.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_payload,
            timeout=30,
        )
        should_retry = (
            response.status_code in TRANSIENT_HTTP_STATUS_CODES
            and attempt < attempts
        )
        if not should_retry:
            break
        logger.warning(
            f"{url} returned HTTP {response.status_code}; retrying in "
            f"{GET_RETRY_DELAY_SECONDS} seconds "
            f"(attempt {attempt + 1}/{attempts})"
        )
        time.sleep(GET_RETRY_DELAY_SECONDS)

    if response.status_code >= 400:
        body = response.text.strip()
        raise RuntimeError(
            f"{url} returned HTTP {response.status_code}: {body[:500] or 'empty body'}"
        )
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{url} returned invalid JSON") from exc
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError(f"{url} returned errors: {payload['errors']}")
    return payload


def html_to_text(raw_html: str | None) -> str | None:
    if not raw_html:
        return None
    text = BeautifulSoup(raw_html, "html.parser").get_text(" ")
    text = text.replace("\ufeff", "")
    return " ".join(text.split())
