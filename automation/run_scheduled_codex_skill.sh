#!/usr/bin/env bash
set -euo pipefail

readonly CODEX_BIN="${CODEX_BIN:-${HOME}/.bun/bin/codex}"
readonly CODEX_MODEL="$("${HOME}/dev/misc/gpt-model.sh")"
readonly CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
readonly HERDR_BIN="${HERDR_BIN:-${HOME}/.local/bin/herdr}"
readonly NOTES_DIR="${HOME}/notes"
readonly TOOLS_DIR="${HOME}/dev/notes-tools"
readonly LOG_DIR="${SCHEDULED_CODEX_LOG_DIR:-${TOOLS_DIR}/automation/scheduled-codex-logs}"
readonly INTERACTIVE_CODEX_SESSION_RUNNER="${TOOLS_DIR}/automation/run_interactive_codex_session.sh"
readonly HERDR_LAUNCHER="${HERDR_LAUNCHER:-${HOME}/dev/misc/desktop/herdr-launch.sh}"
readonly LOG_MAX_BYTES="${SCHEDULED_CODEX_LOG_MAX_BYTES:-200000}"
readonly STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/scheduled-codex"
readonly MESSAGE_REPLY_CHANGED_NOTES_FILE="${STATE_DIR}/message-reply-changed-notes.txt"
readonly DESKTOP_ERROR_LOG="${DESKTOP_ERROR_LOG_PATH:-${HOME}/dev/error_log.txt}"
readonly DESKTOP_ERROR_LOGGER="${DESKTOP_ERROR_LOGGER:-${HOME}/dev/misc/automation/log_desktop_error.sh}"
readonly NOTES_AUTO_COMMIT_LOCK="${SCHEDULED_CODEX_NOTES_AUTO_COMMIT_LOCK:-${NOTES_DIR}/.git/git_auto_commit.lock}"
readonly CATCHUP_GRACE_SECONDS=600
readonly HERDR_CODEX_INPUT_DELAY_MS="${HERDR_CODEX_INPUT_DELAY_MS:-3000}"

scheduled_codex_jobs() {
  scheduled_codex_job "scheduled-goal-advancement" "scheduled-goal-advancement" "exec" "07:00" "" "daily-goal-advancement"
  scheduled_codex_job_every_n_days "scheduled-tweet-ideas" "scheduled-tweet-ideas" "exec" "04:00" 3 2
  scheduled_codex_job_every_n_days "scheduled-idea-space-search" "scheduled-idea-space-search" "exec" "05:00" 5 1
  scheduled_codex_job_every_n_days "scheduled-note-critique" "scheduled-note-critique" "exec" "05:00" 5 2
  scheduled_codex_job_every_n_days "scheduled-hard-feedback" "scheduled-hard-feedback" "exec" "05:00" 5 3
  scheduled_codex_job_every_n_days "scheduled-answer-open-questions" "scheduled-answer-open-questions" "exec" "04:00" 6 4
  scheduled_codex_job_every_n_days "scheduled-security-audit" "scheduled-security-audit" "exec" "11:00" 21 3
  scheduled_codex_job_every_n_days "scheduled-distill-assistant-chats" "scheduled-distill-assistant-chats" "exec" "16:00" 2 0
  scheduled_codex_job_every_n_days "scheduled-infolio-relevance" "scheduled-infolio-relevance" "exec" "21:00" 3 1 "" prepare_infolio_relevance_prompt
  scheduled_error_log_job "scheduled-fix-logged-errors" "scheduled-fix-logged-errors" "exec" "06:00"
}

scheduled_c_bang_jobs() {
  scheduled_codex_job_count=$((scheduled_codex_job_count + 1))
  run_prepared_c_bang_job
}

scheduled_ci_bang_jobs() {
  local claimable_status
  local error_count_before

  scheduled_codex_job_count=$((scheduled_codex_job_count + 1))
  error_count_before="$(desktop_error_count scheduled-ci-bang-interactive)"
  if claimable_ci_bang_tasks; then
    claimable_status=0
  else
    claimable_status=$?
  fi

  if (( claimable_status == 1 )); then
    log_skipped_job "scheduled-ci-bang-interactive" "no claimable ci! tasks found"
    return 0
  fi
  if (( claimable_status != 0 )); then
    log_scheduled_job_failure \
      "scheduled-ci-bang-interactive" \
      "scheduled-ci-bang-interactive" \
      "$claimable_status" \
      "$error_count_before" || true
    if (( overall_status == 0 )); then
      overall_status="$claimable_status"
    fi
    return 0
  fi

  run_and_record_codex_job "scheduled-ci-bang-interactive" "scheduled-ci-bang-interactive" "interactive" ""
}

scheduled_message_reply_jobs() {
  local extra_prompt

  run_and_record_message_pull_scripts || :

  normalize_changed_message_notes

  if [[ -s "$MESSAGE_REPLY_CHANGED_NOTES_FILE" ]]; then
    extra_prompt="Use this changed message notes file when finding reply candidates: ${MESSAGE_REPLY_CHANGED_NOTES_FILE}"
    scheduled_codex_job_count=$((scheduled_codex_job_count + 1))
    run_and_record_codex_job "scheduled-draft-message-replies" "scheduled-draft-message-replies" "exec" "$extra_prompt"
  else
    scheduled_codex_job_count=$((scheduled_codex_job_count + 1))
    printf '[%s] skipped scheduled Codex job: scheduled-draft-message-replies; no new pulled message notes.\n' \
      "$(date --iso-8601=seconds)" | append_job_log "${LOG_DIR}/scheduled-draft-message-replies.log"
  fi

  return 0
}

usage() {
  echo "Usage: $0 [--override] [all|c-bang|ci-bang|message-replies|scheduled-jobs SLOT]" >&2
  echo "Edit scheduled_codex_jobs in this script to choose scheduled skills." >&2
}

valid_name() {
  [[ "$1" =~ ^[A-Za-z0-9_.-]+$ ]]
}

valid_nonnegative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

valid_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

valid_slot() {
  [[ "$1" =~ ^([01][0-9]|2[0-3])[0-5][0-9]$ ]]
}

valid_thread_id() {
  [[ "$1" =~ ^[0-9a-fA-F-]+$ ]]
}

valid_session_source() {
  [[ "$1" == "cli" || "$1" == "exec" || "$1" == "interactive" ]]
}

cadence_phase_for_slot() {
  local period_days="$1"
  local slot="$2"
  local cadence_epoch

  if [[ -n "$slot" ]]; then
    cadence_epoch="$(scheduled_epoch_for_slot "$slot")"
  else
    cadence_epoch="$(date +%s)"
  fi

  echo $(((cadence_epoch / 86400) % period_days))
}

normalize_slot() {
  local raw_slot="$1"
  local normalized_slot

  normalized_slot="${raw_slot//:/}"
  if ! valid_slot "$normalized_slot"; then
    echo "Invalid scheduled slot: $raw_slot. Expected HHMM or HH:MM." >&2
    return 2
  fi

  printf '%s\n' "$normalized_slot"
}

normalize_schedule_time() {
  local raw_time="$1"
  local normalized_time

  normalized_time="${raw_time//:/}"
  if ! valid_slot "$normalized_time"; then
    echo "Invalid scheduled job time: $raw_time. Expected HH:MM." >&2
    return 2
  fi

  printf '%s\n' "$normalized_time"
}

slot_matches_schedule() {
  local schedule_times="$1"
  local slot="$2"
  local schedule_time
  local normalized_time

  for schedule_time in $schedule_times; do
    normalized_time="$(normalize_schedule_time "$schedule_time")"
    if [[ -n "$slot" && "$normalized_time" == "$slot" ]]; then
      return 0
    fi
  done

  if [[ -z "$slot" ]]; then
    return 0
  fi

  return 1
}

scheduled_epoch_for_slot() {
  local slot="$1"
  local hour="${slot:0:2}"
  local minute="${slot:2:2}"
  local today
  local slot_epoch
  local now_epoch

  today="$(date +%F)"
  slot_epoch="$(date -d "${today} ${hour}:${minute}:00" +%s)"
  now_epoch="$(date +%s)"

  if (( slot_epoch > now_epoch + CATCHUP_GRACE_SECONDS )); then
    slot_epoch="$(date -d "yesterday ${hour}:${minute}:00" +%s)"
  fi

  printf '%s\n' "$slot_epoch"
}

catchup_date_for_slot() {
  local slot="$1"
  local scheduled_epoch
  local now_epoch

  if [[ -z "$slot" ]]; then
    return 0
  fi

  scheduled_epoch="$(scheduled_epoch_for_slot "$slot")"
  now_epoch="$(date +%s)"
  if (( now_epoch - scheduled_epoch <= CATCHUP_GRACE_SECONDS )); then
    return 0
  fi

  date -d "@${scheduled_epoch}" +%F
}

claim_catchup_run() {
  local job_name="$1"
  local slot="$2"
  local override_existing_run="$3"
  local catchup_date
  local marker_file

  catchup_date="$(catchup_date_for_slot "$slot")"
  if [[ -z "$catchup_date" ]]; then
    return 0
  fi

  marker_file="${STATE_DIR}/catchup-${job_name}-${catchup_date}"
  if [[ -e "$marker_file" && "$override_existing_run" != "1" ]]; then
    printf '[%s] skipped scheduled Codex job: %s; catch-up already ran for %s.\n' \
      "$(date --iso-8601=seconds)" "$job_name" "$catchup_date" \
      | append_job_log "${LOG_DIR}/${job_name}.log"
    return 1
  fi

  printf '%s slot=%s\n' "$(date --iso-8601=seconds)" "$slot" > "$marker_file"
  return 0
}

cap_log_file() {
  local log_file="$1"
  local temp_log_file

  if [[ ! -f "$log_file" ]]; then
    return 0
  fi

  if ! valid_positive_integer "$LOG_MAX_BYTES"; then
    echo "Invalid SCHEDULED_CODEX_LOG_MAX_BYTES: $LOG_MAX_BYTES" >&2
    return 2
  fi

  if (( $(wc -c < "$log_file") <= LOG_MAX_BYTES )); then
    return 0
  fi

  temp_log_file="$(mktemp "${log_file}.tmp.XXXXXX")"
  tail -c "$LOG_MAX_BYTES" "$log_file" > "$temp_log_file"
  mv "$temp_log_file" "$log_file"
}

append_job_log() {
  local log_file="$1"

  tee -a "$log_file"
  cap_log_file "$log_file"
}

log_skipped_job() {
  local job_name="$1"
  local reason="$2"

  printf '[%s] skipped scheduled Codex job: %s; %s.\n' \
    "$(date --iso-8601=seconds)" "$job_name" "$reason" \
    | append_job_log "${LOG_DIR}/${job_name}.log"
}

acquire_notes_auto_commit_lock() {
  if ! exec 8>"$NOTES_AUTO_COMMIT_LOCK"; then
    echo "Could not open notes Git auto-commit lock: $NOTES_AUTO_COMMIT_LOCK" >&2
    return 1
  fi

  if flock -n 8; then
    return 0
  fi

  echo "Notes Git auto-commit is active; waiting for its repository lock." >&2
  flock 8
}

desktop_error_count() {
  local source_name="$1"
  local count

  if [[ ! -f "$DESKTOP_ERROR_LOG" ]]; then
    echo 0
    return 0
  fi

  count="$(grep -Fxc "source: ${source_name}" "$DESKTOP_ERROR_LOG" || true)"
  printf '%s\n' "${count:-0}"
}

log_scheduled_job_failure() {
  local job_name="$1"
  local skill_name="$2"
  local status="$3"
  local error_count_before="$4"
  local thread_id="${5:-}"
  local job_log="${LOG_DIR}/${job_name}.log"
  local error_count_after
  local details

  error_count_after="$(desktop_error_count "$skill_name")"
  if (( error_count_after > error_count_before )); then
    return 0
  fi

  details="operation=scheduled execution
impact=the scheduled job did not complete successfully
recovered=no
exit_status=${status}
thread_id=${thread_id:-unavailable}
job_log=${job_log}"

  if [[ ! -x "$DESKTOP_ERROR_LOGGER" ]]; then
    printf 'Could not log scheduled failure for %s: logger is not executable: %s\n' \
      "$job_name" "$DESKTOP_ERROR_LOGGER" >&2
    return 1
  fi

  "$DESKTOP_ERROR_LOGGER" \
    "$skill_name" \
    "Scheduled job failed" \
    "${job_name} exited with status ${status} before it could reliably report the failure." \
    "$details"
}

error_log_has_new_records() {
  [[ -s "$DESKTOP_ERROR_LOG" ]]
}

claimable_ci_bang_tasks() {
  local count
  local status

  if count="$(
    uv run --env-file .env python \
      .agents/skills/scheduled-ci-bang-interactive/scripts/prepare_interactive_session.py \
      --count \
      --notes-dir "$NOTES_DIR"
  )"; then
    status=0
  else
    status=$?
  fi

  if (( status != 0 )); then
    return "$status"
  fi
  if ! valid_nonnegative_integer "$count"; then
    echo "Invalid ci! claimable task count: $count" >&2
    return 2
  fi

  (( count > 0 ))
}

build_c_bang_prompt() {
  local prepare_file="$1"
  local action
  local run_id

  action="$(jq -r '.action' "$prepare_file")"
  run_id="$(jq -r '.run_id' "$prepare_file")"

  jq -c 'del(.prompt)' "$prepare_file" >/dev/null

  cat <<PROMPT
Use \$scheduled-c-bang-executor for this unattended scheduled Codex job.

Scheduled job: scheduled-c-bang-executor
Working directory: $NOTES_DIR
Action: $action
Run ID: $run_id

The scheduler has already selected the task run below. Do not run the claim script again. Use the supplied run_id and task_id values when writing the completion report.

Claim data:
$(cat "$prepare_file")

Rules:
- Do not ask follow-up questions.
- If blocked, fail clearly instead of using a silent fallback.
- Log each distinct material failure, blocker, incomplete required verification, or correctness-threatening warning with /home/pimania/dev/misc/automation/log_desktop_error.sh. Use scheduled-c-bang-executor as SOURCE and include the affected task, concise redacted impact, recovery status, thread id, and scheduler log path. Do not log expected no-ops or optional warnings that do not affect correctness.
- Keep edits scoped to what the claimed tasks require.
- After every claimed task is complete, blocked, or intentionally reduced to a drafted next step, run complete_c_bang_tasks.py with one report per task_id.
- Summarize any files changed and anything surprising in the final response.
PROMPT
}

record_c_bang_session_id() {
  local run_id="$1"
  local session_id="$2"

  (
    cd "$NOTES_DIR"
    uv run --env-file .env python \
      .agents/skills/scheduled-c-bang-executor/scripts/claim_c_bang_tasks.py \
      --notes-dir "$NOTES_DIR" \
      --record-session \
      --run-id "$run_id" \
      --session-id "$session_id"
  )
}

release_c_bang_run() {
  local run_id="$1"

  (
    cd "$NOTES_DIR"
    uv run --env-file .env python \
      .agents/skills/scheduled-c-bang-executor/scripts/claim_c_bang_tasks.py \
      --notes-dir "$NOTES_DIR" \
      --release-run \
      --run-id "$run_id"
  )
}

run_prepared_c_bang_job() {
  local job_name="scheduled-c-bang-executor"
  local log_file="${LOG_DIR}/${job_name}.log"
  local lock_file="${STATE_DIR}/${job_name}.lock"
  local prepare_file
  local prepare_error_file
  local prompt_file
  local run_event_file
  local run_output_file
  local final_message_file
  local source_update_file
  local action
  local run_id
  local session_id
  local prompt
  local codex_pid
  local status
  local thread_id=""
  local session_recorded=0
  local source_update_status=0
  local error_count_before

  mkdir -p "$LOG_DIR" "$STATE_DIR"

  exec 9>"$lock_file"
  if ! flock -n 9; then
    log_skipped_job "$job_name" "already running"
    return 0
  fi

  error_count_before="$(desktop_error_count "$job_name")"

  prepare_file="$(mktemp "${STATE_DIR}/${job_name}.prepare.XXXXXX")"
  prepare_error_file="$(mktemp "${STATE_DIR}/${job_name}.prepare-stderr.XXXXXX")"
  prompt_file="$(mktemp "${STATE_DIR}/${job_name}.prompt.XXXXXX")"
  run_event_file="$(mktemp "${STATE_DIR}/${job_name}.events.XXXXXX")"
  run_output_file="$(mktemp "${STATE_DIR}/${job_name}.stderr.XXXXXX")"
  final_message_file="$(mktemp "${STATE_DIR}/${job_name}.final.XXXXXX")"
  source_update_file="$(mktemp "${STATE_DIR}/${job_name}.source.XXXXXX")"

  set +e
  (
    cd "$NOTES_DIR"
    uv run --env-file .env python \
      .agents/skills/scheduled-c-bang-executor/scripts/claim_c_bang_tasks.py \
      --notes-dir "$NOTES_DIR" \
      --prepare
  ) > "$prepare_file" 2> "$prepare_error_file"
  status=$?
  set -e

  if (( status == 0 )); then
    action="$(jq -r '.action // empty' "$prepare_file" 2> "$source_update_file")" || status=1
  fi

  if (( status == 0 )) && [[ "$action" == "skip" ]]; then
    {
      printf '\n[%s] skipped scheduled Codex job: %s; no claimable or resumable c! tasks.\n' \
        "$(date --iso-8601=seconds)" "$job_name"
      jq -c '.' "$prepare_file"
      printf '\n'
    } | append_job_log "$log_file"
    rm -f "$prepare_file" "$prepare_error_file" "$prompt_file" "$run_event_file" "$run_output_file" "$final_message_file" "$source_update_file"
    return 0
  fi

  if (( status == 0 )); then
    run_id="$(jq -r '.run_id // empty' "$prepare_file")"
    session_id="$(jq -r '.session_id // empty' "$prepare_file")"
    prompt="$(build_c_bang_prompt "$prepare_file")"
    printf '%s\n' "$prompt" > "$prompt_file"

    record_started_session() {
      local discovered_thread_id
      discovered_thread_id="$(read_started_thread_id "$run_event_file" 2>/dev/null || true)"
      if [[ -n "$run_id" && -n "$discovered_thread_id" ]]; then
        record_c_bang_session_id "$run_id" "$discovered_thread_id" >/dev/null 2>> "$source_update_file" || true
      fi
    }
    trap 'record_started_session; exit 143' TERM INT HUP

    if [[ "$action" == "resume" ]]; then
      "$CODEX_BIN" --model "$CODEX_MODEL" --dangerously-bypass-approvals-and-sandbox -C "$NOTES_DIR" exec resume \
        --json \
        --output-last-message "$final_message_file" \
        "$session_id" \
        - < "$prompt_file" > "$run_event_file" 2> "$run_output_file" &
    else
      "$CODEX_BIN" --model "$CODEX_MODEL" --dangerously-bypass-approvals-and-sandbox exec \
        -C "$NOTES_DIR" \
        --color never \
        --json \
        --output-last-message "$final_message_file" \
        - < "$prompt_file" > "$run_event_file" 2> "$run_output_file" &
    fi
    codex_pid=$!

    set +e
    while kill -0 "$codex_pid" 2>/dev/null; do
      if [[ "$action" == "start" && -z "$thread_id" ]]; then
        thread_id="$(read_started_thread_id "$run_event_file" 2>/dev/null || true)"
        if [[ -n "$thread_id" ]]; then
          if record_c_bang_session_id "$run_id" "$thread_id" >> "$source_update_file" 2>&1; then
            session_recorded=1
          else
            source_update_status=1
          fi
        fi
      fi
      sleep 1
    done
    wait "$codex_pid"
    status=$?
    set -e
    trap - TERM INT HUP

    if [[ "$action" == "start" ]]; then
      if ! thread_id="$(read_started_thread_id "$run_event_file" 2> "$source_update_file")"; then
        source_update_status=$?
      elif [[ -z "$thread_id" ]]; then
        printf 'Could not record c! Codex session for run %s: codex exec did not emit a session id.\n' \
          "$run_id" > "$source_update_file"
        if (( status != 0 )); then
          release_c_bang_run "$run_id" >> "$source_update_file" 2>&1 || true
        fi
        source_update_status=1
      elif (( session_recorded == 1 )); then
        :
      else
        record_c_bang_session_id "$run_id" "$thread_id" >> "$source_update_file" 2>&1 || source_update_status=$?
      fi
    else
      thread_id="$session_id"
    fi

    if [[ -n "$thread_id" ]]; then
      mark_thread_as_cli "$thread_id" >> "$source_update_file" 2>&1 || source_update_status=$?
    fi

    if (( source_update_status != 0 && status == 0 )); then
      status="$source_update_status"
    fi
  fi

  {
    printf '\n[%s] scheduled Codex job: %s status=%s\n' \
      "$(date --iso-8601=seconds)" "$job_name" "$status"
    printf 'requested session source: cli\n'
    if [[ -n "${action:-}" ]]; then
      printf 'action: %s\n' "$action"
    fi
    if [[ -n "${run_id:-}" ]]; then
      printf 'run id: %s\n' "$run_id"
    fi
    if [[ -n "${thread_id:-}" ]]; then
      printf 'thread id: %s\n' "$thread_id"
    fi
    if [[ -s "$prepare_error_file" ]]; then
      printf 'prepare stderr:\n'
      cat "$prepare_error_file"
      printf '\n'
    fi
    if [[ -s "$source_update_file" ]]; then
      printf 'session source update:\n'
      cat "$source_update_file"
      printf '\n'
    fi
    if [[ -s "$run_output_file" ]]; then
      printf 'Codex stderr:\n'
      cat "$run_output_file"
      printf '\n'
    fi
    printf 'final response:\n'
    if [[ -s "$final_message_file" ]]; then
      cat "$final_message_file"
      printf '\n'
    else
      printf '(no final response captured)\n'
    fi
  } | append_job_log "$log_file"

  if (( status != 0 )); then
    log_scheduled_job_failure \
      "$job_name" "$job_name" "$status" "$error_count_before" "${thread_id:-}" || true
  fi

  rm -f "$prepare_file" "$prepare_error_file" "$prompt_file" "$run_event_file" "$run_output_file" "$final_message_file" "$source_update_file"

  return "$status"
}

message_pull_scripts() {
  printf '%s\t%s\n' "github" "notes/github_notifs_to_notes.py"
  printf '%s\t%s\n' "linear" "notes/linear_notifs_to_notes.py"
  printf '%s\t%s\n' "telegram" "notes/telegram_notifs_to_notes.py"
  printf '%s\t%s\n' "discord" "notes/discord_notifs_to_notes.py"
  printf '%s\t%s\n' "social" "notes/social_notifs_to_notes.py"
}

run_message_pull_script() {
  local label="$1"
  local script_path="$2"
  local status

  printf 'running %s message pull script: %s\n' "$label" "$script_path"
  (
    cd "$TOOLS_DIR"
    MESSAGE_NOTIF_CHANGED_NOTES_FILE="$MESSAGE_REPLY_CHANGED_NOTES_FILE" \
      uv run --env-file .env python "$script_path" < /dev/null
  ) || status=$?
  status="${status:-0}"
  printf '%s message pull script status=%s\n' "$label" "$status"
  return "$status"
}

run_message_pull_scripts() {
  local status=0
  local script_status
  local label
  local script_path

  while IFS=$'\t' read -r label script_path; do
    if run_message_pull_script "$label" "$script_path"; then
      :
    else
      script_status=$?
      if (( status == 0 )); then
        status="$script_status"
      fi
    fi
  done < <(message_pull_scripts)

  return "$status"
}

normalize_changed_message_notes() {
  local temp_file

  if [[ ! -s "$MESSAGE_REPLY_CHANGED_NOTES_FILE" ]]; then
    return 0
  fi

  temp_file="$(mktemp "${STATE_DIR}/changed-message-notes.XXXXXX")"
  sort -u "$MESSAGE_REPLY_CHANGED_NOTES_FILE" > "$temp_file"
  mv "$temp_file" "$MESSAGE_REPLY_CHANGED_NOTES_FILE"
}

run_and_record_message_pull_scripts() {
  local log_file="${LOG_DIR}/message-pull-scripts.log"
  local output_file
  local status
  local error_count_before

  mkdir -p "$LOG_DIR" "$STATE_DIR"
  error_count_before="$(desktop_error_count scheduled-draft-message-replies)"
  : > "$MESSAGE_REPLY_CHANGED_NOTES_FILE"
  output_file="$(mktemp "${STATE_DIR}/message-pull-scripts.XXXXXX")"

  set +e
  run_message_pull_scripts > "$output_file" 2>&1
  status=$?
  set -e

  {
    printf '\n[%s] message pull scripts status=%s\n' \
      "$(date --iso-8601=seconds)" "$status"
    cat "$output_file"
  } | append_job_log "$log_file"

  rm -f "$output_file"

  if (( status != 0 )); then
    log_scheduled_job_failure \
      "message-pull-scripts" \
      "scheduled-draft-message-replies" \
      "$status" \
      "$error_count_before" || true
  fi

  if (( status != 0 && overall_status == 0 )); then
    overall_status="$status"
  fi

  return "$status"
}

read_started_thread_id() {
  local event_file="$1"

  if ! command -v jq >/dev/null; then
    echo "jq is required to read codex exec JSON events." >&2
    return 2
  fi

  jq -r '
    if .type == "thread.started" then
      .thread_id // empty
    elif .type == "session_meta" then
      .payload.session_id // .payload.id // empty
    else
      empty
    end
  ' "$event_file" | head -n 1
}

rewrite_rollout_session_meta_source() {
  local rollout_path="$1"
  local thread_id="$2"
  local temp_file
  local updated_first_line

  if [[ ! -f "$rollout_path" ]]; then
    echo "Codex rollout file not found for thread ${thread_id}: ${rollout_path}" >&2
    return 1
  fi

  updated_first_line="$(
    head -n 1 "$rollout_path" | jq -c --arg thread_id "$thread_id" '
      if .type != "session_meta" then
        error("first rollout line is not session_meta")
      elif (.payload.id != $thread_id and .payload.session_id != $thread_id) then
        error("session_meta thread id does not match")
      else
        .payload.source = "cli" | .payload.originator = "codex-tui"
      end
    '
  )"

  temp_file="$(mktemp "${rollout_path}.tmp.XXXXXX")"
  {
    printf '%s\n' "$updated_first_line"
    tail -n +2 "$rollout_path"
  } > "$temp_file"
  mv "$temp_file" "$rollout_path"
}

mark_thread_as_cli() {
  local thread_id="$1"
  local state_db
  local current_source
  local new_source
  local rollout_path

  if ! valid_thread_id "$thread_id"; then
    echo "Invalid Codex thread id emitted by codex exec: ${thread_id}" >&2
    return 2
  fi

  if ! command -v jq >/dev/null; then
    echo "jq is required to mark scheduled Codex sessions as cli." >&2
    return 2
  fi

  if ! command -v sqlite3 >/dev/null; then
    echo "sqlite3 is required to mark scheduled Codex sessions as cli." >&2
    return 2
  fi

  while IFS= read -r state_db; do
    current_source="$(
      sqlite3 -cmd '.timeout 5000' -noheader "$state_db" \
        "SELECT source FROM threads WHERE id = '${thread_id}' LIMIT 1;"
    )"

    if [[ -z "$current_source" ]]; then
      continue
    fi

    rollout_path="$(
      sqlite3 -cmd '.timeout 5000' -noheader "$state_db" \
        "SELECT rollout_path FROM threads WHERE id = '${thread_id}' LIMIT 1;"
    )"

    sqlite3 -cmd '.timeout 5000' "$state_db" \
      "UPDATE threads SET source = 'cli' WHERE id = '${thread_id}';"

    new_source="$(
      sqlite3 -cmd '.timeout 5000' -noheader "$state_db" \
        "SELECT source FROM threads WHERE id = '${thread_id}' LIMIT 1;"
    )"

    if [[ "$new_source" != "cli" ]]; then
      echo "Failed to verify cli source for Codex thread ${thread_id} in ${state_db}." >&2
      return 1
    fi

    rewrite_rollout_session_meta_source "$rollout_path" "$thread_id"
    echo "Marked Codex thread ${thread_id} as cli in ${state_db} and ${rollout_path}."
    return 0
  done < <(find "$CODEX_HOME_DIR" -maxdepth 1 -type f -name 'state_*.sqlite' -print | sort)

  echo "No Codex state DB in ${CODEX_HOME_DIR} contained thread ${thread_id}." >&2
  return 1
}

codex_terminal_input() {
  local prompt="$1"
  printf '%s\n' "${prompt//$'\n'/ }"
}

milliseconds_to_sleep_seconds() {
  local delay_ms="$1"

  if ! valid_nonnegative_integer "$delay_ms"; then
    echo "Invalid Herdr Codex input delay: $delay_ms" >&2
    return 2
  fi

  printf '%s.%03d\n' "$((delay_ms / 1000))" "$((delay_ms % 1000))"
}

herdr_codex_agent_name() {
  local run_id="$1"
  printf 'codex-ci-%s\n' "${run_id:0:8}"
}

start_herdr_codex_agent() {
  local agent_name="$1"
  local job_name="$2"
  local run_id="$3"
  local session_id="$4"

  "$HERDR_BIN" agent start "$agent_name" --cwd "$NOTES_DIR" --focus -- \
    "$INTERACTIVE_CODEX_SESSION_RUNNER" \
    "$job_name" \
    "$run_id" \
    "${session_id:-"-"}"
}

herdr_agent_pane_id() {
  local agent_name="$1"

  "$HERDR_BIN" agent get "$agent_name" | jq -r '.result.agent.pane_id // empty'
}

send_herdr_codex_input() {
  local agent_name="$1"
  local pane_id="$2"
  local prompt="$3"
  local delay_ms="$4"
  local sleep_seconds

  sleep_seconds="$(milliseconds_to_sleep_seconds "$delay_ms")"
  sleep "$sleep_seconds"
  "$HERDR_BIN" agent send "$agent_name" "$prompt"
  "$HERDR_BIN" pane send-keys "$pane_id" Enter
}

run_interactive_codex_job() {
  local job_name="$1"
  local skill_name="$2"
  local extra_prompt="$3"
  local log_file="${LOG_DIR}/${job_name}.log"
  local lock_file="${STATE_DIR}/${job_name}.lock"
  local prepare_script="${NOTES_DIR}/.agents/skills/${skill_name}/scripts/prepare_interactive_session.py"
  local prepare_output_file
  local prepare_error_file
  local source_update_file
  local launch
  local prompt
  local task_count
  local run_id
  local action
  local session_id
  local run_lock_file
  local agent_name=""
  local pane_id=""
  local status=0

  mkdir -p "$LOG_DIR" "$STATE_DIR"

  exec 9>"$lock_file"
  if ! flock -n 9; then
    printf '[%s] skipped scheduled Codex job: %s; already running.\n' \
      "$(date --iso-8601=seconds)" "$job_name" | append_job_log "$log_file"
    return 0
  fi

  if [[ ! -f "$prepare_script" ]]; then
    echo "Interactive scheduled Codex job is missing prepare script: $prepare_script" >&2
    return 1
  fi

  prepare_output_file="$(mktemp "${STATE_DIR}/${job_name}.prepare.XXXXXX")"
  prepare_error_file="$(mktemp "${STATE_DIR}/${job_name}.prepare-stderr.XXXXXX")"
  source_update_file="$(mktemp "${STATE_DIR}/${job_name}.source.XXXXXX")"

  set +e
  (
    cd "$NOTES_DIR"
    uv run --env-file .env python "$prepare_script" --notes-dir "$NOTES_DIR"
  ) > "$prepare_output_file" 2> "$prepare_error_file"
  status=$?
  set -e

  if (( status == 0 )); then
    if ! launch="$(jq -r '.launch // false' "$prepare_output_file" 2> "$source_update_file")"; then
      status=1
    elif [[ "$launch" == "true" ]]; then
      action="$(jq -r '.action // "start"' "$prepare_output_file")"
      prompt="$(jq -r '.prompt // empty' "$prepare_output_file")"
      task_count="$(jq -r '.task_count // 0' "$prepare_output_file")"
      run_id="$(jq -r '.run_id // empty' "$prepare_output_file")"
      session_id="$(jq -r '.session_id // empty' "$prepare_output_file")"
      run_lock_file="${STATE_DIR}/${job_name}.${run_id}.lock"

      if [[ -z "$prompt" ]]; then
        echo "Interactive scheduled Codex job ${job_name} did not produce a prompt." > "$source_update_file"
        status=1
      elif [[ -z "$run_id" ]]; then
        echo "Interactive scheduled Codex job ${job_name} did not produce a run_id." > "$source_update_file"
        status=1
      elif ! flock -n "$run_lock_file" true; then
        printf 'Interactive scheduled Codex run already active: %s run=%s.\n' \
          "$job_name" "$run_id" > "$source_update_file"
        status=0
        launch="false"
      elif [[ ! -x "$HERDR_BIN" ]]; then
        echo "Herdr executable not found for interactive scheduled job: $HERDR_BIN" > "$source_update_file"
        status=1
      elif [[ ! -x "$HERDR_LAUNCHER" ]]; then
        echo "Herdr launcher not executable: $HERDR_LAUNCHER" > "$source_update_file"
        status=1
      elif [[ ! -x "$INTERACTIVE_CODEX_SESSION_RUNNER" ]]; then
        echo "Interactive Codex session runner not executable: $INTERACTIVE_CODEX_SESSION_RUNNER" > "$source_update_file"
        status=1
      elif ! valid_nonnegative_integer "$HERDR_CODEX_INPUT_DELAY_MS"; then
        echo "HERDR_CODEX_INPUT_DELAY_MS must be a non-negative integer." > "$source_update_file"
        status=1
      else
        prompt="$(codex_terminal_input "$prompt")"
        agent_name="$(herdr_codex_agent_name "$run_id")"
        if ! start_herdr_codex_agent "$agent_name" "$job_name" "$run_id" "$session_id" >> "$source_update_file" 2>&1; then
          status=1
        elif ! pane_id="$(herdr_agent_pane_id "$agent_name" 2>> "$source_update_file")" || [[ -z "$pane_id" ]]; then
          echo "Herdr did not report a pane id for agent: $agent_name" >> "$source_update_file"
          status=1
        elif ! "$HERDR_LAUNCHER" >> "$source_update_file" 2>&1; then
          status=1
        elif ! send_herdr_codex_input "$agent_name" "$pane_id" "$prompt" "$HERDR_CODEX_INPUT_DELAY_MS" >> "$source_update_file" 2>&1; then
          status=1
        else
          status=0
        fi
      fi
    else
      task_count="$(jq -r '.task_count // 0' "$prepare_output_file")"
      run_id="$(jq -r '.run_id // empty' "$prepare_output_file")"
    fi
  fi

  {
    printf '\n[%s] scheduled Codex job: %s status=%s\n' \
      "$(date --iso-8601=seconds)" "$job_name" "$status"
    printf 'requested session source: interactive\n'
    if [[ -n "${action:-}" ]]; then
      printf 'action: %s\n' "$action"
    fi
    if [[ -n "${run_id:-}" ]]; then
      printf 'run id: %s\n' "$run_id"
    fi
    if [[ -n "${session_id:-}" ]]; then
      printf 'session id: %s\n' "$session_id"
    fi
    if [[ -n "${task_count:-}" ]]; then
      printf 'claimed task count: %s\n' "$task_count"
    fi
    if [[ -n "$agent_name" ]]; then
      printf 'herdr agent: %s\n' "$agent_name"
    fi
    if [[ -n "$pane_id" ]]; then
      printf 'herdr pane id: %s\n' "$pane_id"
    fi
    if [[ -s "$prepare_error_file" ]]; then
      printf 'prepare stderr:\n'
      cat "$prepare_error_file"
      printf '\n'
    fi
    if [[ -s "$source_update_file" ]]; then
      printf 'interactive launch details:\n'
      cat "$source_update_file"
      printf '\n'
    fi
    if [[ "$launch" == "true" && "$status" == "0" ]]; then
      printf 'final response:\n(interactive Codex session opened in Herdr)\n'
    elif [[ -s "$prepare_output_file" ]]; then
      printf 'prepare response:\n'
      jq -c 'del(.prompt)' "$prepare_output_file" || cat "$prepare_output_file"
      printf '\n'
    fi
  } | append_job_log "$log_file"

  rm -f "$prepare_output_file" "$prepare_error_file" "$source_update_file"

  return "$status"
}

run_and_record_codex_job() {
  local job_name="$1"
  local skill_name="$2"
  local session_source="$3"
  local extra_prompt="$4"
  local profile_name="${5:-}"
  local log_file="${LOG_DIR}/${job_name}.log"
  local lock_file="${STATE_DIR}/${job_name}.lock"
  local run_event_file
  local run_output_file
  local final_message_file
  local source_update_file
  local prompt
  local status
  local source_update_status=0
  local thread_id=""
  local error_count_before
  local -a codex_command

  if [[ "$session_source" == "interactive" ]]; then
    error_count_before="$(desktop_error_count "$skill_name")"
    if run_interactive_codex_job "$job_name" "$skill_name" "$extra_prompt"; then
      status=0
    else
      status=$?
    fi
    if (( status != 0 )); then
      log_scheduled_job_failure \
        "$job_name" "$skill_name" "$status" "$error_count_before" || true
    fi
    if (( status != 0 && overall_status == 0 )); then
      overall_status="$status"
    fi
    return 0
  fi

  mkdir -p "$LOG_DIR" "$STATE_DIR"

  exec 9>"$lock_file"
  if ! flock -n 9; then
    printf '[%s] skipped scheduled Codex job: %s; already running.\n' \
      "$(date --iso-8601=seconds)" "$job_name" | append_job_log "$log_file"
    return 0
  fi

  error_count_before="$(desktop_error_count "$skill_name")"

  prompt="$(
    cat <<PROMPT
Use \$$skill_name for this unattended scheduled Codex job.

Scheduled job: $job_name
Working directory: $NOTES_DIR

Rules:
- Do not ask follow-up questions.
- If blocked, fail clearly instead of using a silent fallback.
- Log each distinct material failure, blocker, incomplete required verification, or correctness-threatening warning with /home/pimania/dev/misc/automation/log_desktop_error.sh. Use $skill_name as SOURCE and include concise redacted impact, recovery status, thread id, and scheduler log path. Do not log expected no-ops or optional warnings that do not affect correctness.
- Keep edits scoped to what the skill requires.
- Summarize any files changed and anything surprising in the final response.
PROMPT
  )"

  if [[ -n "$extra_prompt" ]]; then
    prompt="${prompt}

Extra instructions:
${extra_prompt}"
  fi

  run_event_file="$(mktemp "${STATE_DIR}/${job_name}.events.XXXXXX")"
  run_output_file="$(mktemp "${STATE_DIR}/${job_name}.stderr.XXXXXX")"
  final_message_file="$(mktemp "${STATE_DIR}/${job_name}.final.XXXXXX")"
  source_update_file="$(mktemp "${STATE_DIR}/${job_name}.source.XXXXXX")"

  codex_command=("$CODEX_BIN" --model "$CODEX_MODEL")
  if [[ -n "$profile_name" ]]; then
    if [[ ! -f "${CODEX_HOME_DIR}/${profile_name}.config.toml" ]]; then
      echo "Codex profile not found for scheduled job ${job_name}: ${CODEX_HOME_DIR}/${profile_name}.config.toml" >&2
      log_scheduled_job_failure \
        "$job_name" "$skill_name" 1 "$error_count_before" || true
      rm -f "$run_event_file" "$run_output_file" "$final_message_file" "$source_update_file"
      if (( overall_status == 0 )); then
        overall_status=1
      fi
      return 0
    fi
    codex_command+=(--profile "$profile_name")
  else
    codex_command+=(--dangerously-bypass-approvals-and-sandbox)
  fi
  codex_command+=(exec -C "$NOTES_DIR" --color never --json --output-last-message "$final_message_file" -)

  set +e
  if [[ "$job_name" == "scheduled-goal-advancement" ]]; then
    acquire_notes_auto_commit_lock
    status=$?
  else
    status=0
  fi

  if (( status == 0 )); then
    printf '%s\n' "$prompt" | "${codex_command[@]}" 8>&- > "$run_event_file" 2> "$run_output_file"
    status=$?
  fi

  if [[ "$job_name" == "scheduled-goal-advancement" ]]; then
    exec 8>&-
  fi
  set -e

  if [[ "$session_source" == "cli" ]]; then
    if ! thread_id="$(read_started_thread_id "$run_event_file" 2> "$source_update_file")"; then
      source_update_status=$?
    elif [[ -z "$thread_id" ]]; then
      printf 'Could not mark scheduled Codex job %s as cli: codex exec did not emit a session id.\n' \
        "$job_name" > "$source_update_file"
      source_update_status=1
    elif mark_thread_as_cli "$thread_id" > "$source_update_file" 2>&1; then
      source_update_status=0
    else
      source_update_status=$?
    fi

    if (( source_update_status != 0 && status == 0 )); then
      status="$source_update_status"
    fi
  fi

  {
    printf '\n[%s] scheduled Codex job: %s status=%s\n' \
      "$(date --iso-8601=seconds)" "$job_name" "$status"
    printf 'requested session source: %s\n' "$session_source"
    if [[ -n "$profile_name" ]]; then
      printf 'Codex profile: %s\n' "$profile_name"
    fi
    if [[ -n "$thread_id" ]]; then
      printf 'thread id: %s\n' "$thread_id"
    fi
    if [[ -s "$source_update_file" ]]; then
      printf 'session source update:\n'
      cat "$source_update_file"
      printf '\n'
    fi
    if [[ -s "$run_output_file" ]]; then
      printf 'Codex stderr:\n'
      cat "$run_output_file"
      printf '\n'
    fi
    printf 'final response:\n'
    if [[ -s "$final_message_file" ]]; then
      cat "$final_message_file"
      printf '\n'
    else
      printf '(no final response captured)\n'
    fi
  } | append_job_log "$log_file"

  if (( status != 0 )); then
    log_scheduled_job_failure \
      "$job_name" "$skill_name" "$status" "$error_count_before" "$thread_id" || true
  fi

  rm -f "$run_event_file" "$run_output_file" "$final_message_file" "$source_update_file"

  if (( status != 0 && overall_status == 0 )); then
    overall_status="$status"
  fi
  return 0
}

validate_job_config() {
  local job_name="$1"
  local skill_name="$2"
  local session_source="$3"
  local profile_name="${4:-}"
  if [[ -z "${job_name:-}" ]]; then
    echo "Invalid scheduled job config; job name is required." >&2
    return 2
  fi

  if ! valid_name "$job_name"; then
    echo "Invalid job name: $job_name" >&2
    return 2
  fi

  if [[ -z "${skill_name:-}" ]]; then
    echo "Invalid scheduled job config; skill name is required for job: $job_name" >&2
    return 2
  fi

  if ! valid_name "$skill_name"; then
    echo "Invalid skill name: $skill_name" >&2
    return 2
  fi

  if [[ -z "${session_source:-}" ]]; then
    echo "Invalid scheduled job config; session source is required for job: $job_name." >&2
    return 2
  fi

  if ! valid_session_source "$session_source"; then
    echo "Invalid session source for job ${job_name}: ${session_source}. Expected cli, exec, or interactive." >&2
    return 2
  fi

  if [[ -n "$profile_name" ]] && ! valid_name "$profile_name"; then
    echo "Invalid Codex profile name for job ${job_name}: ${profile_name}" >&2
    return 2
  fi
  if [[ -n "$profile_name" && "$session_source" == "interactive" ]]; then
    echo "Interactive scheduled jobs cannot use a non-interactive Codex profile: ${job_name}" >&2
    return 2
  fi
}

scheduled_codex_job() {
  local job_name="${1:-}"
  local skill_name="${2:-${1:-}}"
  local session_source="${3:-}"
  local schedule_times="${4:-}"
  local extra_prompt="${5:-}"
  local profile_name="${6:-}"

  validate_job_config "$job_name" "$skill_name" "$session_source" "$profile_name"
  if ! slot_matches_schedule "$schedule_times" "$run_slot"; then
    return 0
  fi

  scheduled_codex_job_count=$((scheduled_codex_job_count + 1))
  if ! claim_catchup_run "$job_name" "$run_slot" "$override_existing_run"; then
    return 0
  fi

  run_and_record_codex_job "$job_name" "$skill_name" "$session_source" "$extra_prompt" "$profile_name"
}

scheduled_error_log_job() {
  local job_name="${1:-}"
  local skill_name="${2:-${1:-}}"
  local session_source="${3:-}"
  local schedule_times="${4:-}"
  local preflight_status
  local error_count_before

  validate_job_config "$job_name" "$skill_name" "$session_source" ""
  if ! slot_matches_schedule "$schedule_times" "$run_slot"; then
    return 0
  fi

  scheduled_codex_job_count=$((scheduled_codex_job_count + 1))
  error_count_before="$(desktop_error_count "$skill_name")"

  set +e
  error_log_has_new_records
  preflight_status=$?
  set -e

  if (( preflight_status == 1 )); then
    log_skipped_job "$job_name" "no new desktop error log records"
    return 0
  fi
  if (( preflight_status != 0 )); then
    log_scheduled_job_failure \
      "$job_name" "$skill_name" "$preflight_status" "$error_count_before" || true
    if (( overall_status == 0 )); then
      overall_status="$preflight_status"
    fi
    return 0
  fi

  if ! claim_catchup_run "$job_name" "$run_slot" "$override_existing_run"; then
    return 0
  fi

  run_and_record_codex_job "$job_name" "$skill_name" "$session_source" "" ""
}

prepare_infolio_relevance_prompt() {
  local selection_json
  local skip_reason

  selection_json="$(
    cd "$TOOLS_DIR"
    uv run --env-file .env python notes/select_infolio_relevance_articles.py \
      --feedback-file "$NOTES_DIR/.agents/skills/scheduled-infolio-relevance/feedback.md"
  )"
  jq -e '
    .articles | type == "array"
    and all(.[]; (.article_id | type == "string") and (.lineate_url | type == "string"))
  ' <<< "$selection_json" >/dev/null
  skip_reason="$(jq -r '.skip_reason // empty' <<< "$selection_json")"
  if [[ -n "$skip_reason" ]]; then
    printf '%s\n' "$skip_reason"
    return 3
  fi

  cat <<PROMPT
Analyse exactly the Infolio articles selected below. The selector has already excluded articles marked as analysed in the skill feedback file. Attempt every supplied Lineate URL and do not replace the selection.

Selection JSON:
$selection_json
PROMPT
}

scheduled_codex_job_every_n_days() {
  local job_name="${1:-}"
  local skill_name="${2:-${1:-}}"
  local session_source="${3:-}"
  local schedule_times="${4:-}"
  local period_days="${5:-}"
  local phase="${6:-}"
  local extra_prompt="${7:-}"
  local extra_prompt_builder="${8:-}"
  local current_phase
  local prompt_builder_status
  local error_count_before

  validate_job_config "$job_name" "$skill_name" "$session_source" ""
  if ! slot_matches_schedule "$schedule_times" "$run_slot"; then
    return 0
  fi

  scheduled_codex_job_count=$((scheduled_codex_job_count + 1))

  if ! valid_positive_integer "$period_days"; then
    echo "Invalid cadence for scheduled job ${job_name}: period_days must be a positive integer." >&2
    return 2
  fi

  if ! valid_nonnegative_integer "$phase" || (( phase >= period_days )); then
    echo "Invalid cadence for scheduled job ${job_name}: phase must be an integer in [0, period_days)." >&2
    return 2
  fi

  current_phase="$(cadence_phase_for_slot "$period_days" "$run_slot")"
  if (( current_phase != phase )); then
    echo "Skipping scheduled Codex job: ${job_name} (every ${period_days} days, phase ${phase}; today phase ${current_phase})."
    return 0
  fi

  if ! claim_catchup_run "$job_name" "$run_slot" "$override_existing_run"; then
    return 0
  fi

  if [[ -n "$extra_prompt_builder" ]]; then
    error_count_before="$(desktop_error_count "$skill_name")"
    if ! declare -F "$extra_prompt_builder" >/dev/null; then
      echo "Unknown extra-prompt builder for ${job_name}: ${extra_prompt_builder}" >&2
      return 2
    fi
    if extra_prompt="$("$extra_prompt_builder")"; then
      prompt_builder_status=0
    else
      prompt_builder_status=$?
    fi
    if (( prompt_builder_status == 3 )); then
      log_skipped_job "$job_name" "$extra_prompt"
      return 0
    fi
    if (( prompt_builder_status != 0 )); then
      log_scheduled_job_failure \
        "$job_name" "$skill_name" "$prompt_builder_status" "$error_count_before" || true
      if (( overall_status == 0 )); then
        overall_status="$prompt_builder_status"
      fi
      return 0
    fi
  fi

  run_and_record_codex_job "$job_name" "$skill_name" "$session_source" "$extra_prompt" ""
}

override_existing_run=0
if [[ "${1:-}" == "--override" ]]; then
  override_existing_run=1
  shift
fi

run_mode="${1:-all}"
run_slot=""

case "$run_mode" in
  all|c-bang|ci-bang|message-replies)
    if (( $# != 0 && $# != 1 )); then
      usage
      exit 2
    fi
    ;;
  scheduled-jobs)
    if (( $# != 2 )); then
      usage
      exit 2
    fi
    run_slot="$(normalize_slot "$2")"
    ;;
  *)
    usage
    exit 2
    ;;
esac

if [[ ! -x "$CODEX_BIN" ]]; then
  echo "Codex executable not found or not executable: $CODEX_BIN" >&2
  exit 1
fi

if [[ ! -d "$NOTES_DIR" ]]; then
  echo "Notes directory not found: $NOTES_DIR" >&2
  exit 1
fi

scheduled_codex_job_count=0
overall_status=0
mkdir -p "$LOG_DIR" "$STATE_DIR"

exec 8>"${STATE_DIR}/run_scheduled_codex_skill.lock"
flock 8

case "$run_mode" in
  all)
    scheduled_c_bang_jobs
    scheduled_ci_bang_jobs
    scheduled_codex_jobs
    scheduled_message_reply_jobs
    ;;
  c-bang)
    scheduled_c_bang_jobs
    ;;
  ci-bang)
    scheduled_ci_bang_jobs
    ;;
  message-replies)
    scheduled_message_reply_jobs
    ;;
  scheduled-jobs)
    scheduled_codex_jobs
    ;;
esac

if (( scheduled_codex_job_count == 0 )); then
  echo "No scheduled Codex jobs configured in scheduled_codex_jobs." >&2
  exit 2
fi

exit "$overall_status"
