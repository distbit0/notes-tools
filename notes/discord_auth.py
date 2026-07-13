from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import requests


DISCORD_API_BASE_URL = "https://discord.com/api/v9"
BRAVE_DISCORD_LOCAL_STORAGE_DIR = (
    Path.home()
    / ".config/BraveSoftware/Brave-Browser/Default/Local Storage/leveldb"
)

DISCORD_TOKEN_PATTERNS = (
    re.compile(rb"mfa\.[A-Za-z0-9_-]{20,}"),
    re.compile(rb"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}"),
)
LEVELDB_STORAGE_SUFFIXES = {".ldb", ".log", ".sst"}


@dataclass(frozen=True)
class DiscordTokenCandidate:
    token: str
    source_path: Path
    source_mtime_ns: int


def iter_storage_files(storage_dir: Path) -> list[Path]:
    if not storage_dir.exists():
        raise RuntimeError(
            f"Brave Discord local storage directory does not exist: {storage_dir}"
        )
    if not storage_dir.is_dir():
        raise RuntimeError(
            f"Brave Discord local storage path is not a directory: {storage_dir}"
        )

    return sorted(
        (
            path
            for path in storage_dir.iterdir()
            if path.is_file() and path.suffix in LEVELDB_STORAGE_SUFFIXES
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )


def extract_discord_token_candidates(
    storage_dir: Path = BRAVE_DISCORD_LOCAL_STORAGE_DIR,
) -> list[DiscordTokenCandidate]:
    candidates: list[DiscordTokenCandidate] = []
    seen_tokens: set[str] = set()

    for path in iter_storage_files(storage_dir):
        data = path.read_bytes()
        source_mtime_ns = path.stat().st_mtime_ns
        for pattern in DISCORD_TOKEN_PATTERNS:
            for match in pattern.finditer(data):
                token = match.group(0).decode("ascii")
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                candidates.append(
                    DiscordTokenCandidate(
                        token=token,
                        source_path=path,
                        source_mtime_ns=source_mtime_ns,
                    )
                )

    return sorted(
        candidates,
        key=lambda candidate: candidate.source_mtime_ns,
        reverse=True,
    )


def discord_token_is_valid(token: str) -> bool:
    response = requests.get(
        f"{DISCORD_API_BASE_URL}/users/@me",
        headers={"authorization": token},
        timeout=30,
    )
    if response.status_code == 200:
        return True
    if response.status_code == 401:
        return False

    raise RuntimeError(
        f"Discord API error {response.status_code} while validating Brave token: "
        f"{response.text}"
    )


def load_discord_auth_token(
    storage_dir: Path = BRAVE_DISCORD_LOCAL_STORAGE_DIR,
) -> str:
    candidates = extract_discord_token_candidates(storage_dir)
    if not candidates:
        raise RuntimeError(
            f"No Discord auth token candidates found in Brave storage: {storage_dir}"
        )

    for candidate in candidates:
        if discord_token_is_valid(candidate.token):
            return candidate.token

    raise RuntimeError(
        f"Found {len(candidates)} Discord auth token candidate(s) in Brave storage, "
        "but none authenticated with /users/@me"
    )
