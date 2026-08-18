import pathlib
import re
import secrets
import subprocess
import tempfile


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
HABIT_TEXT_SOURCE_FILE_FIELD = "textSourceFile"
HABIT_TEXT_MAX_WORD_COUNT_FIELD = "maxSourceWordCount"
HABIT_TEXT_TRANSFORM_PROMPT_FIELD = "textTransformPrompt"
HABIT_RENDERED_TEXT_FIELD = "habitText"
CODEX_MODEL = "gpt-5.6-terra"
CODEX_REASONING_EFFORT = "high"
CODEX_TIMEOUT_SECONDS = 300


def resolve_habit_text_source_path(habit):
    source_file = habit.get(HABIT_TEXT_SOURCE_FILE_FIELD)
    if source_file is None:
        return None
    if not isinstance(source_file, str) or not source_file.strip():
        raise ValueError(
            f"Habit '{habit.get('name')}' {HABIT_TEXT_SOURCE_FILE_FIELD} "
            "must be a non-empty string"
        )

    source_path = pathlib.Path(source_file).expanduser()
    if not source_path.is_absolute():
        source_path = (PROJECT_ROOT / source_path).resolve()
    return source_path


def validate_dynamic_habit_text(habit):
    source_path = resolve_habit_text_source_path(habit)
    dynamic_fields = {
        HABIT_TEXT_MAX_WORD_COUNT_FIELD,
        HABIT_TEXT_TRANSFORM_PROMPT_FIELD,
    }
    configured_dynamic_fields = dynamic_fields.intersection(habit)
    if source_path is None:
        if configured_dynamic_fields:
            raise ValueError(
                f"Habit '{habit.get('name')}' sets {sorted(configured_dynamic_fields)} "
                f"without {HABIT_TEXT_SOURCE_FILE_FIELD}"
            )
        return

    missing_fields = dynamic_fields.difference(habit)
    if missing_fields:
        raise ValueError(
            f"Habit '{habit.get('name')}' is missing dynamic text fields: "
            f"{sorted(missing_fields)}"
        )

    max_word_count = habit[HABIT_TEXT_MAX_WORD_COUNT_FIELD]
    if (
        isinstance(max_word_count, bool)
        or not isinstance(max_word_count, int)
        or max_word_count < 1
    ):
        raise ValueError(
            f"Habit '{habit.get('name')}' {HABIT_TEXT_MAX_WORD_COUNT_FIELD} "
            "must be a positive integer"
        )

    transform_prompt = habit[HABIT_TEXT_TRANSFORM_PROMPT_FIELD]
    if not isinstance(transform_prompt, str) or not transform_prompt.strip():
        raise ValueError(
            f"Habit '{habit.get('name')}' {HABIT_TEXT_TRANSFORM_PROMPT_FIELD} "
            "must be a non-empty string"
        )


def count_words(text):
    return len(re.findall(r"\S+", text))


def select_habit_source_text(source_text, max_word_count, random_generator=None):
    stripped_source_text = source_text.strip()
    if not stripped_source_text:
        raise ValueError("Habit text source file is empty")
    if count_words(stripped_source_text) <= max_word_count:
        return stripped_source_text

    source_lines = stripped_source_text.splitlines()
    line_word_counts = [count_words(line) for line in source_lines]
    oversized_line_numbers = [
        line_number
        for line_number, line_word_count in enumerate(line_word_counts, start=1)
        if line_word_count >= max_word_count
    ]
    if oversized_line_numbers:
        raise ValueError(
            "Habit text source lines must each remain below maxSourceWordCount; "
            f"oversized lines: {oversized_line_numbers}"
        )

    content_line_indexes = [
        line_index
        for line_index, line_word_count in enumerate(line_word_counts)
        if line_word_count
    ]
    if not content_line_indexes:
        raise ValueError("Habit text source file is empty")

    random_source = random_generator or secrets.SystemRandom()
    selected_start = random_source.choice(content_line_indexes)
    selected_end = selected_start
    selected_word_count = line_word_counts[selected_start]

    while True:
        expandable_sides = []
        if (
            selected_start > 0
            and selected_word_count + line_word_counts[selected_start - 1]
            < max_word_count
        ):
            expandable_sides.append("before")
        if (
            selected_end + 1 < len(source_lines)
            and selected_word_count + line_word_counts[selected_end + 1]
            < max_word_count
        ):
            expandable_sides.append("after")
        if not expandable_sides:
            break

        selected_side = random_source.choice(expandable_sides)
        if selected_side == "before":
            selected_start -= 1
            selected_word_count += line_word_counts[selected_start]
        else:
            selected_end += 1
            selected_word_count += line_word_counts[selected_end]

    return "\n".join(source_lines[selected_start : selected_end + 1]).strip()


def build_codex_transform_input(transform_prompt, source_text):
    return (
        "Transform the source text according to the instruction below. Return only the "
        "transformed habit text, with no preface, commentary, or Markdown fence.\n\n"
        f"Instruction:\n{transform_prompt.strip()}\n\n"
        f"Source text:\n{source_text.strip()}\n"
    )


def transform_habit_text_with_codex(transform_prompt, source_text):
    codex_input = build_codex_transform_input(transform_prompt, source_text)
    with tempfile.TemporaryDirectory(prefix="habit-text-codex-") as temporary_dir:
        output_path = pathlib.Path(temporary_dir) / "last-message.txt"
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            CODEX_MODEL,
            "--config",
            f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
            "--color",
            "never",
            "--output-last-message",
            str(output_path),
            "-",
        ]
        try:
            result = subprocess.run(
                command,
                input=codex_input,
                text=True,
                capture_output=True,
                cwd=temporary_dir,
                timeout=CODEX_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError("Codex CLI is not installed or not on PATH") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"Codex habit text transformation timed out after "
                f"{CODEX_TIMEOUT_SECONDS} seconds"
            ) from error

        if result.returncode != 0:
            error_detail = result.stderr.strip()[-1000:] or "no error output"
            raise RuntimeError(
                f"Codex habit text transformation failed with exit code "
                f"{result.returncode}: {error_detail}"
            )
        if not output_path.is_file():
            raise RuntimeError("Codex did not write a final habit text response")

        transformed_text = output_path.read_text(encoding="utf-8").strip()
        if not transformed_text:
            raise RuntimeError("Codex returned empty habit text")
        return transformed_text


def materialize_trigger_habit_text(item):
    habit = item["habit"]
    trigger = item["trigger"]
    source_path = resolve_habit_text_source_path(habit)
    if source_path is None or trigger.get(HABIT_RENDERED_TEXT_FIELD):
        return
    if not source_path.is_file():
        raise RuntimeError(f"Habit text source file does not exist: {source_path}")

    source_text = source_path.read_text(encoding="utf-8")
    selected_text = select_habit_source_text(
        source_text,
        habit[HABIT_TEXT_MAX_WORD_COUNT_FIELD],
    )
    trigger[HABIT_RENDERED_TEXT_FIELD] = transform_habit_text_with_codex(
        habit[HABIT_TEXT_TRANSFORM_PROMPT_FIELD],
        selected_text,
    )


def get_trigger_habit_text(item):
    rendered_text = item["trigger"].get(HABIT_RENDERED_TEXT_FIELD)
    if rendered_text is not None:
        return rendered_text.strip()
    return re.sub(r"^\d+\.\s*", "", item["habit"].get("name", "")).strip()
