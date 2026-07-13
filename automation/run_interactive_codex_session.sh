#!/usr/bin/env bash
set -euo pipefail

readonly CODEX_BIN="${CODEX_BIN:-${HOME}/.bun/bin/codex}"
readonly CODEX_MODEL="$("${HOME}/dev/misc/gpt-model.sh")"
readonly CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
readonly NOTES_DIR="${HOME}/notes"
readonly STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/scheduled-codex"

usage() {
  echo "Usage: $0 JOB_NAME RUN_ID SESSION_ID_OR_- [PROMPT_FILE]" >&2
}

discover_session_id() {
  local run_id="$1"
  local rollout_path

  while IFS= read -r rollout_path; do
    head -n 1 "$rollout_path" | jq -r '
      select(.type == "session_meta")
      | .payload.session_id // .payload.id // empty
    '
    return 0
  done < <(rg -l --fixed-strings "$run_id" "${CODEX_HOME_DIR}/sessions" -g '*.jsonl' 2>/dev/null | sort -r)
}

record_session_id() {
  local skill_script="$1"
  local run_id="$2"
  local session_id="$3"

  (
    cd "$NOTES_DIR"
    uv run --env-file .env python "$skill_script" \
      --notes-dir "$NOTES_DIR" \
      --record-session \
      --run-id "$run_id" \
      --session-id "$session_id"
  )
}

release_run() {
  local skill_script="$1"
  local run_id="$2"

  (
    cd "$NOTES_DIR"
    uv run --env-file .env python "$skill_script" \
      --notes-dir "$NOTES_DIR" \
      --release-run \
      --run-id "$run_id"
  )
}

start_session_recorder() {
  local skill_script="$1"
  local run_id="$2"
  local status_file="$3"
  local log_file="$4"

  (
    local discovered_session_id=""

    for _ in {1..60}; do
      discovered_session_id="$(discover_session_id "$run_id")"
      if [[ -n "$discovered_session_id" ]]; then
        if record_session_id "$skill_script" "$run_id" "$discovered_session_id"; then
          printf 'recorded %s\n' "$discovered_session_id" > "$status_file"
          exit 0
        fi
        printf 'record-failed %s\n' "$discovered_session_id" > "$status_file"
        exit 1
      fi
      sleep 1
    done

    printf 'not-found\n' > "$status_file"
    exit 1
  ) > "$log_file" 2>&1 &

  printf '%s\n' "$!"
}

finish_session_recorder() {
  local recorder_pid="$1"
  local status_file="$2"
  local log_file="$3"
  local skill_script="$4"
  local run_id="$5"
  local outcome=""
  local recorder_status=0

  for _ in {1..5}; do
    if [[ -s "$status_file" ]]; then
      break
    fi
    if ! kill -0 "$recorder_pid" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  if kill -0 "$recorder_pid" 2>/dev/null; then
    kill "$recorder_pid" 2>/dev/null || true
  fi

  if wait "$recorder_pid"; then
    recorder_status=0
  else
    recorder_status=$?
  fi

  if [[ -s "$status_file" ]]; then
    outcome="$(head -n 1 "$status_file")"
  fi

  if [[ "$outcome" == recorded\ * ]]; then
    rm -f "$status_file" "$log_file"
    return 0
  fi

  echo "Codex session id was not recorded for ${run_id}; releasing the ci! claim for retry." >&2
  if [[ -s "$log_file" ]]; then
    cat "$log_file" >&2
  fi
  release_run "$skill_script" "$run_id" || true
  rm -f "$status_file" "$log_file"
  return "$recorder_status"
}

if (( $# != 3 && $# != 4 )); then
  usage
  exit 2
fi

job_name="$1"
run_id="$2"
session_id="$3"
prompt_file="${4:-}"

if [[ "$session_id" == "-" ]]; then
  session_id=""
fi

case "$job_name" in
  scheduled-ci-bang-interactive)
    skill_script=".agents/skills/scheduled-ci-bang-interactive/scripts/prepare_interactive_session.py"
    ;;
  *)
    echo "Unsupported interactive scheduled job: $job_name" >&2
    exit 2
    ;;
esac

if [[ -n "$prompt_file" ]]; then
  if [[ ! -s "$prompt_file" ]]; then
    echo "Prompt file missing or empty: $prompt_file" >&2
    exit 2
  fi
  echo "Passing a prompt file directly to Codex can submit /plan through a default-mode startup turn; send the prompt as terminal input after Codex starts instead." >&2
fi

mkdir -p "$STATE_DIR"
lock_file="${STATE_DIR}/${job_name}.${run_id}.lock"

exec 9>"$lock_file"
if ! flock -n 9; then
  echo "Interactive scheduled Codex run already active: ${job_name} run=${run_id}"
  exit 0
fi

if [[ -n "$prompt_file" ]]; then
  prompt="$(cat "$prompt_file")"
  rm -f "$prompt_file"
else
  prompt=""
fi

if [[ -n "$session_id" ]]; then
  if [[ -n "$prompt" ]]; then
    exec "$CODEX_BIN" \
      -C "$NOTES_DIR" \
      --model "$CODEX_MODEL" \
      --dangerously-bypass-approvals-and-sandbox \
      resume "$session_id" "$prompt"
  fi
  exec "$CODEX_BIN" \
    -C "$NOTES_DIR" \
    --model "$CODEX_MODEL" \
    --dangerously-bypass-approvals-and-sandbox \
    resume "$session_id"
fi

recorder_status_file="$(mktemp "${STATE_DIR}/${job_name}.${run_id}.session.XXXXXX")"
recorder_log_file="$(mktemp "${STATE_DIR}/${job_name}.${run_id}.session-log.XXXXXX")"
recorder_pid="$(start_session_recorder "$skill_script" "$run_id" "$recorder_status_file" "$recorder_log_file")"

codex_command=(
  "$CODEX_BIN"
  -C "$NOTES_DIR"
  --model "$CODEX_MODEL"
  --dangerously-bypass-approvals-and-sandbox
)
if [[ -n "$prompt" ]]; then
  codex_command+=("$prompt")
fi

if "${codex_command[@]}"; then
  codex_status=0
else
  codex_status=$?
fi

if finish_session_recorder "$recorder_pid" "$recorder_status_file" "$recorder_log_file" "$skill_script" "$run_id"; then
  recorder_status=0
else
  recorder_status=$?
fi

if (( codex_status == 0 && recorder_status != 0 )); then
  exit "$recorder_status"
fi
exit "$codex_status"
