import random
import subprocess
from pathlib import Path

import pytest

from src.dynamic_habit_text import (
    count_words,
    materialize_trigger_habit_text,
    select_habit_source_text,
    transform_habit_text_with_codex,
    validate_dynamic_habit_text,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
README_TEXT = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")


def test_source_sampling_uses_contiguous_source_window_below_strict_word_cap():
    selected_text = select_habit_source_text(
        README_TEXT,
        100,
        random_generator=random.Random(20260816),
    )

    assert count_words(selected_text) < 100
    assert selected_text in README_TEXT


def test_source_sampling_can_reach_content_inside_oversized_paragraph():
    oversized_paragraph = max(
        README_TEXT.split("\n\n"),
        key=count_words,
    ).strip()
    target_line = max(oversized_paragraph.splitlines(), key=count_words).strip()

    selected_windows = [
        select_habit_source_text(
            README_TEXT,
            100,
            random_generator=random.Random(random_seed),
        )
        for random_seed in range(500)
    ]

    assert count_words(oversized_paragraph) >= 100
    assert any(target_line in selected_window for selected_window in selected_windows)


def test_source_sampling_returns_complete_file_when_within_cap():
    source_text = README_TEXT.split("\n\n", 1)[0]

    assert select_habit_source_text(source_text, count_words(source_text)) == source_text


def test_source_sampling_rejects_line_at_or_above_word_cap():
    longest_source_line = max(README_TEXT.splitlines(), key=count_words)
    strict_word_cap = count_words(longest_source_line)

    with pytest.raises(ValueError, match="oversized lines"):
        select_habit_source_text(README_TEXT, strict_word_cap)


def test_codex_transform_uses_noninteractive_terra_high(tmp_path, monkeypatch):
    source_text = README_TEXT.split("\n\n", 2)[1]
    observed_call = {}

    def fake_run(command, **kwargs):
        observed_call.update({"command": command, **kwargs})
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(source_text, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("src.dynamic_habit_text.subprocess.run", fake_run)

    transformed_text = transform_habit_text_with_codex(
        "Rewrite the source as prose.", source_text
    )

    assert transformed_text == source_text
    assert observed_call["command"][:2] == ["codex", "exec"]
    assert ["--model", "gpt-5.6-terra"] == observed_call["command"][
        observed_call["command"].index("--model") :
        observed_call["command"].index("--model") + 2
    ]
    assert 'model_reasoning_effort="high"' in observed_call["command"]
    assert observed_call["command"][-1] == "-"
    assert source_text in observed_call["input"]
    assert observed_call["capture_output"] is True
    assert observed_call["cwd"] != str(PROJECT_ROOT)


def test_materialized_text_is_reused_for_trigger(tmp_path, monkeypatch):
    source_path = tmp_path / "source.md"
    source_path.write_text(README_TEXT, encoding="utf-8")
    item = {
        "habit": {
            "name": "Habit documentation",
            "textSourceFile": str(source_path),
            "maxSourceWordCount": 100,
            "textTransformPrompt": "Rewrite the source as prose.",
        },
        "trigger": {},
    }
    transform_calls = []

    def fake_transform(transform_prompt, source_text):
        transform_calls.append((transform_prompt, source_text))
        return source_text

    monkeypatch.setattr(
        "src.dynamic_habit_text.transform_habit_text_with_codex", fake_transform
    )

    materialize_trigger_habit_text(item)
    first_habit_text = item["trigger"]["habitText"]
    materialize_trigger_habit_text(item)

    assert count_words(first_habit_text) < 100
    assert len(transform_calls) == 1


def test_dynamic_text_fields_require_source_file():
    with pytest.raises(ValueError, match="without textSourceFile"):
        validate_dynamic_habit_text(
            {"name": "Habit documentation", "maxSourceWordCount": 100}
        )
