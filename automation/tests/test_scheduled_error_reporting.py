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
    "scheduled-resolve-contradictions",
    "scheduled-security-audit",
    "scheduled-tweet-ideas",
}


def test_every_recurring_skill_separates_technical_errors_from_note_conflicts() -> None:
    actual_skills = {
        skill_file.parent.name
        for skill_file in SKILLS_DIR.glob("scheduled-*/SKILL.md")
    }
    assert actual_skills == RECURRING_SKILLS

    for skill_name in actual_skills:
        skill_text = (SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "## Error Reporting" in skill_text
        assert f"log_desktop_error.sh {skill_name} TITLE MESSAGE DETAILS" in skill_text
        assert "technical or operational" in skill_text
        assert "required technical verification" in skill_text
        assert "contradictions.md" in skill_text
        assert "substantive nontechnical" in skill_text
        assert "feedback" in skill_text
        assert "Do not log expected" in skill_text or "Do not relog records" in skill_text

    creator_text = (SKILLS_DIR / "schedule-codex-skill/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "technical or operational" in creator_text
    assert "required technical verification" in creator_text
    assert "contradictions.md" in creator_text
    assert "substantive nontechnical" in creator_text


def test_scheduler_prompts_enforce_the_same_reporting_boundary() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")

    assert scheduler_text.count("only for distinct material technical or operational") == 2
    assert scheduler_text.count("incomplete required technical verification") == 2
    assert scheduler_text.count(
        "Record note-content contradictions in /home/pimania/notes/contradictions.md instead."
    ) == 2
    assert scheduler_text.count(
        "Record other substantive nontechnical limitations in the skill or task feedback."
    ) == 2


def test_logged_error_fixer_requires_context_complete_judgment_based_reports() -> None:
    skill_text = (SKILLS_DIR / "scheduled-fix-logged-errors/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "standalone" in skill_text
    assert "without" in skill_text and "original error" in skill_text
    assert "quick fix" in skill_text
    assert "user intent" in skill_text
    assert "unresolved" in skill_text and "why" in skill_text
    assert "reroute" in skill_text and "contradictions.md" in skill_text


def test_goal_advancement_preserves_notes_and_tracks_action_origins() -> None:
    skill_text = (SKILLS_DIR / "scheduled-goal-advancement/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Do not delete" in skill_text
    assert "Goal-advancement update" in skill_text
    assert "additive" in skill_text
    assert "same line" in skill_text
    assert "completed" in skill_text and "partially advanced" in skill_text
    assert "todo-sourced" in skill_text
    assert "independently selected" in skill_text
    assert "at least half" in skill_text
    assert "rounded up" in skill_text
    assert "Drafts and decision aids" in skill_text


def test_contradiction_resolver_prioritizes_insight_and_preserves_sources() -> None:
    skill_text = (SKILLS_DIR / "scheduled-resolve-contradictions/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "novel" in skill_text
    assert "useful insights" in skill_text
    assert "implications" in skill_text
    assert "not merely" in skill_text
    assert "every active contradiction" in skill_text
    assert "resolved or superseded entry" in skill_text
    assert "no prior resolver-feedback entry" in skill_text
    assert "Resolution candidate" in skill_text
    assert "Do not delete" in skill_text
    assert "preserve" in skill_text
    assert "atomic" in skill_text
    assert "additive" in skill_text
    assert "feedback.md" in skill_text


def test_security_audit_read_only_boundary_allows_contradiction_routing() -> None:
    skill_text = (SKILLS_DIR / "scheduled-security-audit/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "read-only except" in skill_text
    assert "/home/pimania/notes/contradictions.md" in skill_text


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
