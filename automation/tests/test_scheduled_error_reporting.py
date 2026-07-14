from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER = REPO_ROOT / "automation/run_scheduled_codex_skill.sh"
NOTES_DIR = Path.home() / "notes"
SKILLS_DIR = NOTES_DIR / ".agents/skills"
ERROR_LOGGER = Path.home() / "dev/misc/automation/log_desktop_error.sh"

RECURRING_SKILLS = {
    "scheduled-answer-open-questions",
    "scheduled-c-bang-executor",
    "scheduled-ci-bang-interactive",
    "scheduled-distill-assistant-chats",
    "scheduled-draft-message-replies",
    "scheduled-fix-logged-errors",
    "scheduled-goal-advancement",
    "scheduled-hard-feedback",
    "scheduled-idea-space-search",
    "scheduled-infolio-relevance",
    "scheduled-note-critique",
    "scheduled-security-audit",
    "scheduled-tweet-ideas",
}


def test_every_recurring_skill_requires_material_error_reporting() -> None:
    actual_skills = {
        skill_file.parent.name
        for skill_file in SKILLS_DIR.glob("scheduled-*/SKILL.md")
    }
    assert actual_skills == RECURRING_SKILLS

    for skill_name in actual_skills:
        skill_text = (SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "## Error Reporting" in skill_text
        assert f"log_desktop_error.sh {skill_name} TITLE MESSAGE DETAILS" in skill_text
        assert "Do not log expected" in skill_text or "Do not relog records" in skill_text

    creator_text = (SKILLS_DIR / "schedule-codex-skill/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Require every recurring skill to report each distinct material failure" in creator_text


def test_runner_fallback_uses_only_redacted_status_metadata() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    fallback = scheduler_text[
        scheduler_text.index("log_scheduled_job_failure()"):
        scheduler_text.index("\nerror_log_has_new_records()")
    ]

    assert "exit_status=${status}" in fallback
    assert "job_log=${job_log}" in fallback
    assert "run_output_file" not in fallback
    assert "stderr" not in fallback.lower()


def test_runner_logs_an_actual_codex_process_failure(tmp_path: Path) -> None:
    error_log = tmp_path / "error_log.txt"
    state_home = tmp_path / "state"
    environment = os.environ | {
        "CODEX_BIN": "/usr/bin/false",
        "DESKTOP_ERROR_LOGGER": str(ERROR_LOGGER),
        "DESKTOP_ERROR_LOG_PATH": str(error_log),
        "XDG_STATE_HOME": str(state_home),
    }

    result = subprocess.run(
        [str(SCHEDULER), "scheduled-jobs", "0700"],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    log_text = error_log.read_text(encoding="utf-8")
    assert log_text.count("--- desktop-error ") == 1
    assert "source: scheduled-goal-advancement" in log_text
    assert "exit_status=1" in log_text
    assert "job_log=" in log_text
    assert stat.S_IMODE(error_log.stat().st_mode) == 0o600
