import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import integrate_notes  # noqa: E402
import spec_llm  # noqa: E402
from spec_config import (  # noqa: E402
    OPENROUTER_REQUEST_TIMEOUT_SECONDS,
    OPENROUTER_SDK_MAX_RETRIES,
)


def test_integrate_notes_openrouter_client_uses_bounded_timeout(monkeypatch) -> None:
    captured_kwargs = {}
    client = object()

    def openai_client(**kwargs):
        captured_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(integrate_notes, "OpenAI", openai_client)
    monkeypatch.setattr(integrate_notes, "load_dotenv", lambda *_, **__: None)
    monkeypatch.setenv(integrate_notes.ENV_API_KEY, "test-key")

    assert integrate_notes.create_openrouter_client() is client
    assert (
        captured_kwargs["timeout"]
        == integrate_notes.OPENROUTER_REQUEST_TIMEOUT_SECONDS
    )
    assert captured_kwargs["max_retries"] == integrate_notes.OPENROUTER_SDK_MAX_RETRIES


def test_spec_openrouter_client_uses_bounded_timeout(monkeypatch) -> None:
    captured_kwargs = {}
    client = object()

    def openai_client(**kwargs):
        captured_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(spec_llm, "OpenAI", openai_client)
    monkeypatch.setattr(spec_llm, "load_dotenv", lambda *_, **__: None)
    monkeypatch.setenv(spec_llm.ENV_API_KEY, "test-key")

    assert spec_llm.create_openrouter_client() is client
    assert captured_kwargs["timeout"] == OPENROUTER_REQUEST_TIMEOUT_SECONDS
    assert captured_kwargs["max_retries"] == OPENROUTER_SDK_MAX_RETRIES


def test_integration_request_sets_per_call_timeout() -> None:
    captured_kwargs = {}
    tool_arguments = json.dumps(
        {
            "action": "integrate",
            "patches": [
                {
                    "search": "- Create space for the other person to talk.\n- Ask open-ended follow-up questions.",
                    "replace": (
                        "- Create space for the other person to talk.\n"
                        "- Ask people to tell you more rather than immediately giving advice or your opinion.\n"
                        '- Use verbal acknowledgments while they are speaking, e.g., "yeah that makes sense," "uh huh."\n'
                        "- Ask open-ended follow-up questions."
                    ),
                }
            ],
            "duplications": [],
        }
    )

    class Responses:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            return SimpleNamespace(error=None, output_text=f"```json\n{tool_arguments}\n```")

    client = SimpleNamespace(responses=Responses())

    response_text = integrate_notes.request_integration(client, "prompt", "unit")
    instructions, duplications = integrate_notes.parse_integration_payload(
        response_text
    )

    assert instructions == [
        integrate_notes.PatchInstruction(
            search_text="- Create space for the other person to talk.\n- Ask open-ended follow-up questions.",
            replace_text=(
                "- Create space for the other person to talk.\n"
                "- Ask people to tell you more rather than immediately giving advice or your opinion.\n"
                '- Use verbal acknowledgments while they are speaking, e.g., "yeah that makes sense," "uh huh."\n'
                "- Ask open-ended follow-up questions."
            ),
        )
    ]
    assert duplications == []
    assert captured_kwargs["reasoning"] == integrate_notes.DEFAULT_REASONING
    assert captured_kwargs["model"] == "openai/gpt-5.6-terra"
    assert captured_kwargs["text"] == {
        "format": integrate_notes.INTEGRATION_RESPONSE_FORMAT
    }
    assert (
        captured_kwargs["timeout"]
        == integrate_notes.OPENROUTER_REQUEST_TIMEOUT_SECONDS
    )


def test_integration_rejects_captured_empty_evidence_response() -> None:
    captured_response = json.dumps(
        {
            "action": "integrate",
            "patches": [],
            "duplications": [],
        }
    )

    with pytest.raises(
        integrate_notes.IntegrationParseError,
        match="at least one patch or duplication proof",
    ):
        integrate_notes.parse_integration_payload(captured_response)


def test_integration_prompt_requires_evidence_for_non_empty_chunk() -> None:
    prompt = integrate_notes.build_integration_prompt(
        "group by topic",
        "# Existing body",
        "A new scratchpad point.",
    )

    assert "patches and duplications must not both be empty" in prompt


def test_empty_evidence_is_retried_before_a_patch_is_accepted(monkeypatch) -> None:
    responses = iter(
        [
            json.dumps(
                {"action": "integrate", "patches": [], "duplications": []}
            ),
            json.dumps(
                {
                    "action": "integrate",
                    "patches": [
                        {
                            "search": "- Focusing:",
                            "replace": (
                                "- When I am feeling tired, take melatonin, put the laptop away, "
                                "read a germane article, then go to sleep instead of watching YouTube.\n"
                                "- Focusing:"
                            ),
                        }
                    ],
                    "duplications": [],
                }
            ),
        ]
    )
    request_count = 0

    def request_integration(*_args, **_kwargs):
        nonlocal request_count
        request_count += 1
        return next(responses)

    monkeypatch.setattr(integrate_notes, "request_integration", request_integration)

    updated_body, patches, duplications = integrate_notes.integrate_chunk_with_patches(
        object(),
        "group by topic",
        "- Focusing:",
        (
            "Chris Lakin's method of noticing where a feeling sits in one's body. "
            "v valuble explanation wrt procrastination: "
            "https://chatgpt.com/share/6a78e598-cd7c-83ea-ace4-dccf338d4960\n"
            "when i am feeling tired, take melatonin, put laptop away, read germane "
            "article then go to sleep instead of watching yt"
        ),
        "historical chunk",
    )

    assert request_count == 2
    assert len(patches) == 1
    assert duplications == []
    assert updated_body == patches[0].replace_text


def test_verification_retry_uses_structured_omissions_once(monkeypatch) -> None:
    first_candidate = ("body after first candidate", [], [])
    second_candidate = ("body after corrected candidate", [], [])
    integration_results = iter([first_candidate, second_candidate])
    integration_feedback = []
    omission = integrate_notes.VerificationOmission(
        notes_text=(
            "when i am feeling tired, take melatonin, put laptop away, read germane "
            "article then go to sleep instead of watching yt"
        ),
        body_text="<not present>",
        explanation="The independent bedtime routine was omitted.",
        proposed_fix="Add the complete bedtime routine.",
    )
    assessments = iter(
        [
            integrate_notes.VerificationAssessment(
                status="missing",
                summary="One independent instruction is missing.",
                omissions=(omission,),
            ),
            integrate_notes.VerificationAssessment(
                status="complete",
                summary="Every source instruction is represented.",
                omissions=(),
            ),
        ]
    )

    def integrate_chunk(*_args, verification_omissions=None, **_kwargs):
        integration_feedback.append(verification_omissions)
        return next(integration_results)

    monkeypatch.setattr(
        integrate_notes,
        "integrate_chunk_with_patches",
        integrate_chunk,
    )
    monkeypatch.setattr(
        integrate_notes,
        "verify_candidate_body",
        lambda *_args, **_kwargs: next(assessments),
    )
    monkeypatch.setattr(
        integrate_notes,
        "log_verification_assessment",
        lambda *_args, **_kwargs: None,
    )

    result = integrate_notes.integrate_verified_chunk(
        object(),
        "group by topic",
        "body before integration",
        omission.notes_text,
        "historical chunk",
        "productivity-strat-index.md",
        0,
        1,
        False,
    )

    assert result == second_candidate
    assert integration_feedback == [None, (omission,)]


def test_verification_payload_enforces_status_and_omission_consistency() -> None:
    inconsistent_response = json.dumps(
        {
            "status": "missing",
            "summary": "Content is missing.",
            "omissions": [],
        }
    )

    with pytest.raises(RuntimeError, match="at least one omission"):
        integrate_notes.parse_verification_payload(inconsistent_response)


def test_verification_request_uses_structured_output() -> None:
    captured_kwargs = {}
    response_payload = json.dumps(
        {
            "status": "complete",
            "summary": "Every source detail is represented.",
            "omissions": [],
        }
    )

    class Responses:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            return SimpleNamespace(error=None, output_text=response_payload)

    assessment = integrate_notes.request_verification(
        SimpleNamespace(responses=Responses()),
        "verification prompt",
        "historical chunk",
    )

    assert assessment.is_complete
    assert assessment.omissions == ()
    assert captured_kwargs["model"] == "openai/gpt-5.6-terra"
    assert captured_kwargs["text"] == {
        "format": integrate_notes.VERIFICATION_RESPONSE_FORMAT
    }
