from __future__ import annotations

from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "discord_notifs_to_notes.py"


def test_discord_notification_puller_is_explicitly_disabled() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == "Discord notification polling is disabled."
