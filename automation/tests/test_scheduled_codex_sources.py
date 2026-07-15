from __future__ import annotations

import fcntl
from math import lcm
import os
import re
from pathlib import Path
import subprocess
import time


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER = REPO_ROOT / "automation/run_scheduled_codex_skill.sh"


def scheduled_every_n_day_jobs() -> dict[str, tuple[str, int, int]]:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    return {
        job_name: (schedule_time, int(period_days), int(phase))
        for job_name, _skill_name, _session_source, schedule_time, period_days, phase in re.findall(
            r'scheduled_codex_job_every_n_days\s+"([^"]+)"\s+"([^"]+)"\s+"([^"]+)"\s+"([^"]+)"\s+(\d+)\s+(\d+)',
            scheduler_text,
        )
    }


def scheduled_daily_jobs() -> dict[str, tuple[str, str]]:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    return {
        job_name: (schedule_time, profile_name)
        for job_name, _skill_name, _session_source, schedule_time, profile_name in re.findall(
            r'scheduled_codex_job\s+"([^"]+)"\s+"([^"]+)"\s+"([^"]+)"\s+"([^"]+)"\s+""\s+"([^"]+)"',
            scheduler_text,
        )
    }


def test_scheduled_codex_job_cadences_are_spread_out() -> None:
    expected_schedule = {
        "scheduled-tweet-ideas": ("04:00", 3, 2),
        "scheduled-resolve-contradictions": ("04:00", 6, 1),
        "scheduled-idea-space-search": ("05:00", 5, 1),
        "scheduled-hard-feedback": ("05:00", 5, 3),
        "scheduled-answer-open-questions": ("04:00", 6, 4),
        "scheduled-security-audit": ("11:00", 21, 3),
        "scheduled-distill-assistant-chats": ("16:00", 2, 0),
        "scheduled-infolio-relevance": ("21:00", 3, 1),
    }

    actual_schedule = scheduled_every_n_day_jobs()
    daily_schedule = scheduled_daily_jobs()

    assert actual_schedule == expected_schedule
    assert daily_schedule == {
        "scheduled-goal-advancement": ("07:00", "daily-goal-advancement"),
    }

    schedule_period = lcm(*(period_days for _schedule_time, period_days, _phase in actual_schedule.values()))
    for epoch_day in range(schedule_period):
        running_jobs_by_time = {
            schedule_time: [job_name]
            for job_name, (schedule_time, _profile_name) in daily_schedule.items()
        }
        for job_name, (schedule_time, period_days, phase) in actual_schedule.items():
            if epoch_day % period_days == phase:
                running_jobs_by_time.setdefault(schedule_time, []).append(job_name)
        assert all(len(job_names) <= 1 for job_names in running_jobs_by_time.values())


def test_unattended_scheduled_jobs_do_not_relabel_as_cli() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    expected_exec_jobs = {
        "scheduled-tweet-ideas",
        "scheduled-resolve-contradictions",
        "scheduled-idea-space-search",
        "scheduled-hard-feedback",
        "scheduled-answer-open-questions",
        "scheduled-security-audit",
        "scheduled-distill-assistant-chats",
        "scheduled-goal-advancement",
        "scheduled-infolio-relevance",
        "scheduled-draft-message-replies",
    }

    job_sources = dict(
        re.findall(
            r'run_and_record_codex_job\s+"([^"]+)"\s+"[^"]+"\s+"([^"]+)"',
            scheduler_text,
        )
    )
    job_sources.update(
        re.findall(
            r'scheduled_codex_job_every_n_days\s+"([^"]+)"\s+"[^"]+"\s+"([^"]+)"',
            scheduler_text,
        )
    )
    job_sources.update(
        re.findall(
            r'scheduled_codex_job\s+"([^"]+)"\s+"[^"]+"\s+"([^"]+)"',
            scheduler_text,
        )
    )

    for job_name in expected_exec_jobs:
        assert job_sources[job_name] == "exec"


def test_cadence_phase_uses_the_scheduled_slot_for_catchups() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    cadence_function = scheduler_text[
        scheduler_text.index("cadence_phase_for_slot()"):
        scheduler_text.index("\nnormalize_slot()")
    ]

    assert 'cadence_epoch="$(scheduled_epoch_for_slot "$slot")"' in cadence_function
    assert 'current_phase="$(cadence_phase_for_slot "$period_days" "$run_slot")"' in scheduler_text
    assert "today_phase" not in scheduler_text


def test_infolio_selection_is_passed_to_codex_after_cadence_check() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    scheduled_job = scheduler_text[
        scheduler_text.index("scheduled_codex_job_every_n_days()"):
        scheduler_text.index("\nrun_mode=")
    ]

    assert "prepare_infolio_relevance_prompt" in scheduler_text
    assert 'extra_prompt="$("$extra_prompt_builder")"' in scheduled_job
    assert scheduled_job.index("current_phase=") < scheduled_job.index('extra_prompt="$("$extra_prompt_builder")"')
    assert scheduled_job.index("claim_catchup_run") < scheduled_job.index('extra_prompt="$("$extra_prompt_builder")"')
    assert "if (( prompt_builder_status == 3 )); then" in scheduled_job
    assert 'log_skipped_job "$job_name" "$extra_prompt"' in scheduled_job
    assert "Selection JSON:" in scheduler_text


def test_interactive_ci_prompt_uses_herdr_terminal_input() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    interactive_job = scheduler_text[
        scheduler_text.index("run_interactive_codex_job()"):
        scheduler_text.index("\nrun_and_record_codex_job()")
    ]

    assert (
        'readonly HERDR_CODEX_INPUT_DELAY_MS="${HERDR_CODEX_INPUT_DELAY_MS:-3000}"'
        in scheduler_text
    )
    assert (
        'readonly HERDR_BIN="${HERDR_BIN:-${HOME}/.local/bin/herdr}"'
        in scheduler_text
    )
    assert (
        'start_herdr_codex_agent "$agent_name" "$job_name" "$run_id" "$session_id"'
        in interactive_job
    )
    assert (
        'send_herdr_codex_input "$agent_name" "$pane_id" "$prompt" '
        '"$HERDR_CODEX_INPUT_DELAY_MS"'
    ) in interactive_job
    assert '"$HERDR_BIN" agent send "$agent_name" "$prompt"' in scheduler_text
    assert '"$HERDR_BIN" pane send-keys "$pane_id" Enter' in scheduler_text
    assert '"$INTERACTIVE_CODEX_SESSION_RUNNER" \\' in scheduler_text
    assert '"${session_id:-"-"}"' in scheduler_text
    assert '"$prompt_file"' not in interactive_job
    assert "write_terminal_launch_request" not in scheduler_text
    assert "VSCODE_BIN" not in scheduler_text


def test_goal_advancement_uses_a_profile_instead_of_full_access() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    run_job = scheduler_text[
        scheduler_text.index("run_and_record_codex_job()"):
        scheduler_text.index("\nvalidate_job_config()")
    ]

    assert (
        'scheduled_codex_job "scheduled-goal-advancement" '
        '"scheduled-goal-advancement" "exec" "07:00" "" "daily-goal-advancement"'
        in scheduler_text
    )
    assert 'codex_command+=(--profile "$profile_name")' in run_job
    assert 'codex_command+=(--dangerously-bypass-approvals-and-sandbox)' in run_job
    assert "Codex stderr:" in run_job
    assert 'cat "$run_output_file"' in run_job
    assert run_job.index('if [[ -n "$profile_name" ]]') < run_job.index(
        'codex_command+=(--dangerously-bypass-approvals-and-sandbox)'
    )


def test_scheduler_holds_notes_auto_commit_lock_for_entire_run() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    run_job = scheduler_text[
        scheduler_text.index("run_and_record_codex_job()"):
        scheduler_text.index("\nvalidate_job_config()")
    ]
    top_level = scheduler_text[scheduler_text.index("scheduled_codex_job_count=0") :]

    assert "acquire_notes_auto_commit_lock" not in run_job
    assert '"${codex_command[@]}" 7>&- 8>&-' in run_job
    assert top_level.index('exec 8>"${STATE_DIR}/run_scheduled_codex_skill.lock"') < (
        top_level.index("flock 8")
    )
    assert top_level.index("flock 8") < top_level.index(
        "acquire_notes_auto_commit_lock"
    )
    assert top_level.index("acquire_notes_auto_commit_lock") < top_level.index(
        'case "$run_mode" in'
    )
    assert "exec 7>&-" not in top_level


def test_goal_advancement_waits_for_notes_auto_commit_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "git_auto_commit.lock"
    stderr_path = tmp_path / "scheduler.stderr"
    environment = os.environ | {
        "CODEX_BIN": "/usr/bin/true",
        "SCHEDULED_CODEX_LOG_DIR": str(tmp_path / "logs"),
        "SCHEDULED_CODEX_NOTES_AUTO_COMMIT_LOCK": str(lock_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    with lock_path.open("w", encoding="utf-8") as lock_file, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        scheduler = subprocess.Popen(
            [str(SCHEDULER), "--override", "scheduled-jobs", "0700"],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stderr_file.flush()
            if "waiting for its repository lock" in stderr_path.read_text(
                encoding="utf-8"
            ):
                break
            time.sleep(0.01)
        else:
            scheduler.terminate()
            scheduler.wait(timeout=5)
            raise AssertionError("scheduler did not wait on the held auto-commit lock")

        assert scheduler.poll() is None
        global_lock_path = (
            tmp_path / "state/scheduled-codex/run_scheduled_codex_skill.lock"
        )
        with global_lock_path.open("w", encoding="utf-8") as global_lock_file:
            try:
                fcntl.flock(global_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(global_lock_file, fcntl.LOCK_UN)
                raise AssertionError(
                    "goal advancement released the global scheduler lock"
                )
        fcntl.flock(lock_file, fcntl.LOCK_UN)

    stdout, _stderr = scheduler.communicate(timeout=10)
    assert scheduler.returncode == 0
    assert "scheduled Codex job: scheduled-goal-advancement status=0" in stdout


def test_global_scheduler_lock_remains_held_after_goal_job(tmp_path: Path) -> None:
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    tee_started_path = tmp_path / "tee-started"
    tee_release_path = tmp_path / "tee-release"
    tee_script = executable_directory / "tee"
    tee_script.write_text(
        r"""#!/usr/bin/env bash
printf '%s\n' "$$" >> "$TEST_TEE_STARTED"
while [[ ! -e "$TEST_TEE_RELEASE" ]]; do
  /usr/bin/sleep 0.01
done
exec /usr/bin/tee "$@"
""",
        encoding="utf-8",
    )
    tee_script.chmod(0o700)

    environment = os.environ | {
        "CODEX_BIN": "/usr/bin/true",
        "PATH": f"{executable_directory}:{os.environ['PATH']}",
        "SCHEDULED_CODEX_LOG_DIR": str(tmp_path / "logs"),
        "SCHEDULED_CODEX_NOTES_AUTO_COMMIT_LOCK": str(
            tmp_path / "git_auto_commit.lock"
        ),
        "TEST_TEE_RELEASE": str(tee_release_path),
        "TEST_TEE_STARTED": str(tee_started_path),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    def start_scheduler() -> subprocess.Popen[str]:
        return subprocess.Popen(
            [str(SCHEDULER), "--override", "scheduled-jobs", "0700"],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    first_scheduler = start_scheduler()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if tee_started_path.exists():
            break
        time.sleep(0.01)
    else:
        first_scheduler.terminate()
        first_scheduler.wait(timeout=5)
        raise AssertionError("first scheduler did not finish the goal job")

    second_scheduler = start_scheduler()
    time.sleep(0.1)
    assert tee_started_path.read_text(encoding="utf-8").count("\n") == 1

    tee_release_path.touch()
    first_stdout, first_stderr = first_scheduler.communicate(timeout=10)
    second_stdout, second_stderr = second_scheduler.communicate(timeout=10)

    assert first_scheduler.returncode == 0, first_stderr
    assert second_scheduler.returncode == 0, second_stderr
    assert "scheduled Codex job: scheduled-goal-advancement status=0" in first_stdout
    assert "scheduled Codex job: scheduled-goal-advancement status=0" in second_stdout
    assert tee_started_path.read_text(encoding="utf-8").count("\n") == 2


def test_no_claimable_ci_tasks_are_handled_without_errexit() -> None:
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    ci_scheduler = scheduler_text[
        scheduler_text.index("scheduled_ci_bang_jobs()"):
        scheduler_text.index("\nscheduled_message_reply_jobs()")
    ]

    assert "if claimable_ci_bang_tasks; then" in ci_scheduler
    assert "claimable_status=0\n  else\n    claimable_status=$?" in ci_scheduler
    assert "if (( claimable_status == 1 )); then" in ci_scheduler
    assert "return 0" in ci_scheduler


def test_override_runs_a_scheduled_job_again_after_its_catchup_was_claimed(
    tmp_path: Path,
) -> None:
    environment = os.environ | {
        "CODEX_BIN": "/usr/bin/true",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    def run_scheduler(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCHEDULER), *arguments],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    first_run = run_scheduler("scheduled-jobs", "0700")
    repeated_run = run_scheduler("scheduled-jobs", "0700")
    override_run = run_scheduler("--override", "scheduled-jobs", "0700")

    assert first_run.returncode == 0
    assert "scheduled Codex job: scheduled-goal-advancement status=0" in first_run.stdout
    assert repeated_run.returncode == 0
    assert "catch-up already ran" in repeated_run.stdout
    assert override_run.returncode == 0
    assert "scheduled Codex job: scheduled-goal-advancement status=0" in override_run.stdout
    assert "catch-up already ran" not in override_run.stdout
