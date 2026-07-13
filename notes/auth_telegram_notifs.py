#!/usr/bin/env python3
from __future__ import annotations

from telegram_notifs_to_notes import (
    TELEGRAM_API_HASH_ENV,
    TELEGRAM_SESSION_NAME_ENV,
    require_env,
    telegram_api_id,
)
from telethon.sync import TelegramClient


def main() -> int:
    api_id = telegram_api_id()
    api_hash = require_env(TELEGRAM_API_HASH_ENV)
    session_name = require_env(TELEGRAM_SESSION_NAME_ENV)

    with TelegramClient(session_name, api_id, api_hash) as client:
        client.start()
        if not client.is_user_authorized():
            raise RuntimeError("Telegram login did not authorize the session")

    print(f"Telegram notification session is authorized: {session_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
