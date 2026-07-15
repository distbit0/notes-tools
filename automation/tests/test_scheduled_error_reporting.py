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

SPECIALIST_SKILLS = {
    "scheduled-fix-logged-errors",
    "scheduled-resolve-contradictions",
}
ROUTING_REFERENCES = {
    "contradict",
    "desktop error",
    "error reporting",
    "error_log",
    "log_desktop_error",
}


def test_agents_md_is_the_shared_log_routing_authority() -> None:
    agents_text = (NOTES_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "[[contradictions]]" in agents_text
    assert "/home/pimania/dev/error_log.txt" in agents_text
    assert "/home/pimania/dev/misc/automation/log_desktop_error.sh" in agents_text
    assert "technical or operational" in agents_text
    assert "recovery status" in agents_text
    assert "current thread ID" in agents_text
    assert "scheduler log path" in agents_text
    assert "relevant writable skill or task feedback" in agents_text
    assert "final response when no writable feedback destination exists" in agents_text


def test_only_specialist_skill_packages_reference_shared_log_routing() -> None:
    skill_files = [
        *SKILLS_DIR.glob("*/SKILL.md"),
        *SKILLS_DIR.glob("*/agents/openai.yaml"),
    ]

    for skill_file in skill_files:
        skill_name = (
            skill_file.parents[1].name
            if skill_file.name == "openai.yaml"
            else skill_file.parent.name
        )
        if skill_name in SPECIALIST_SKILLS:
            continue

        skill_text = skill_file.read_text(encoding="utf-8").lower()
        for routing_reference in ROUTING_REFERENCES:
            assert routing_reference not in skill_text, (
                f"{skill_name} duplicates AGENTS.md routing for "
                f"{routing_reference}"
            )


def test_scheduler_prompts_defer_shared_log_routing_to_agents_md() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    prompt_sections = (
        scheduler_text[
            scheduler_text.index("build_c_bang_prompt()"):
            scheduler_text.index("\nrecord_c_bang_session_id()")
        ],
        scheduler_text[
            scheduler_text.index("run_and_record_codex_job()"):
            scheduler_text.index("\nvalidate_job_config()")
        ],
    )

    for prompt_section in prompt_sections:
        prompt_section = prompt_section.lower()
        for routing_reference in ROUTING_REFERENCES:
            assert routing_reference not in prompt_section


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
