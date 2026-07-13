from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import github_notifs_to_notes  # noqa: E402


def test_run_gh_api_ignores_token_environment(monkeypatch) -> None:
    captured_env = {}
    monkeypatch.setenv("GITHUB_TOKEN", "stale-token")
    monkeypatch.setenv("GH_TOKEN", "other-stale-token")

    def fake_run(_command, *, capture_output, text, check, env):
        nonlocal captured_env
        captured_env = env
        return type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "[]",
                "stderr": "",
            },
        )()

    monkeypatch.setattr(github_notifs_to_notes.subprocess, "run", fake_run)

    assert github_notifs_to_notes.run_gh_api("/notifications") == "[]"
    assert "GITHUB_TOKEN" not in captured_env
    assert "GH_TOKEN" not in captured_env
