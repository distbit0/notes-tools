from __future__ import annotations

from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import github_notifs_to_notes  # noqa: E402
import notes_utils  # noqa: E402


def real_repository_commit() -> tuple[str, str]:
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    repository_path = remote.removeprefix("git@github.com:").removesuffix(".git")
    if repository_path == remote:
        raise RuntimeError(f"Unsupported real GitHub remote: {remote}")
    return (
        f"https://github.com/{repository_path}/commit/{commit_sha}",
        commit_sha,
    )


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


def test_keyed_writer_replaces_legacy_entry_for_real_github_thread(
    tmp_path: Path,
) -> None:
    commit_url, commit_sha = real_repository_commit()
    legacy_line = notes_utils.format_notification_note_line(
        source="github",
        label=commit_sha,
        url=commit_url,
    )
    notes_file = tmp_path / "inbox-index.md"
    source_heading = (
        (Path.home() / "notes/inbox-index.md")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    notes_file.write_text(
        f"{source_heading}\n\n- {legacy_line}\n",
        encoding="utf-8",
    )
    identity = github_notifs_to_notes.github_notification_identity(commit_url)

    notes_utils.replace_keyed_notification_lines(
        notes_file,
        {identity: legacy_line},
        legacy_identity_for_line=(
            github_notifs_to_notes.legacy_github_notification_identity
        ),
    )

    inbox_text = notes_file.read_text(encoding="utf-8")
    assert inbox_text.count("new notif: github:") == 1
    assert notes_utils.NOTIFICATION_IDENTITY_RE.search(inbox_text)
