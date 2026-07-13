import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from private_test_data import PRIVATE_TEST_DATA


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import route_friend_discussion_ideas as router  # noqa: E402


FRIEND_ROUTER_TEST_DATA = PRIVATE_TEST_DATA["friendRouter"]
FRIENDS_INDEX_EXCERPT = FRIEND_ROUTER_TEST_DATA["friendsIndexExcerpt"]
TAGGED_AGENT_MEMORY_FRONTMATTER = FRIEND_ROUTER_TEST_DATA[
    "taggedAgentMemoryFrontmatter"
]
TAGGED_PRICE_MANIPULATION_FRONTMATTER = FRIEND_ROUTER_TEST_DATA[
    "taggedPriceManipulationFrontmatter"
]
SAM_BREW_EXCERPT = FRIEND_ROUTER_TEST_DATA["friendNoteExcerpt"]


def test_extract_friend_links_and_scratchpad_items_from_real_index_shape() -> None:
    assert router.extract_friend_links(FRIENDS_INDEX_EXCERPT) == [
        "max-k-index",
        "sam-brew-index",
    ]
    assert router.extract_scratchpad_items(FRIENDS_INDEX_EXCERPT) == [
        "pivotal public goods",
        "functional decision theory. cooperate in prisoners dilemma, blackmail",
        "blackmail insurance bond",
    ]


def test_read_frontmatter_tags_accepts_existing_tag_shapes() -> None:
    assert router.read_frontmatter_tags(TAGGED_AGENT_MEMORY_FRONTMATTER) == frozenset(
        {"memory", "ai", "agents"}
    )
    assert router.read_frontmatter_tags(
        TAGGED_PRICE_MANIPULATION_FRONTMATTER
    ) == frozenset({"defi", "oracles", "collateral"})


def test_validate_classification_requires_each_candidate_once() -> None:
    classification = router.validate_classification(
        {"matches": ["ai"], "non_matches": ["agents", "memory"]},
        ["agents", "ai", "memory"],
    )

    assert classification.matches == frozenset({"ai"})
    assert classification.non_matches == frozenset({"agents", "memory"})

    with pytest.raises(RuntimeError, match="omitted"):
        router.validate_classification(
            {"matches": ["ai"], "non_matches": ["agents"]},
            ["agents", "ai", "memory"],
        )

    with pytest.raises(RuntimeError, match="duplicated"):
        router.validate_classification(
            {"matches": ["ai"], "non_matches": ["ai", "memory"]},
            ["ai", "memory"],
        )


def test_classification_prompt_uses_inclusive_approved_tag_descriptions() -> None:
    pending_items = [
        router.PendingClassification(
            item_index=1,
            item_text=FRIEND_ROUTER_TEST_DATA["conditionalMarketIdea"],
            missing_tags=("prediction-markets", "crypto"),
        )
    ]
    prompt = router.classification_prompt(
        pending_items,
    )

    assert "Use the tags inclusively" in prompt
    assert "- prediction-markets:" in prompt
    assert "conditional or decision/impact instruments" in prompt
    assert "futarchy" not in prompt.lower()
    assert "Index: 1" in prompt

    with pytest.raises(RuntimeError, match="Unsupported"):
        router.classification_prompt(
            [
                router.PendingClassification(
                    item_index=1,
                    item_text="idea",
                    missing_tags=("agents",),
                )
            ]
        )


def test_openrouter_classifier_requests_strict_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_request: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "output_text": (
                    '{"items":[{"index":1,"matches":["ai"],'
                    '"non_matches":["crypto"]}]}'
                ),
            }

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: int,
    ) -> FakeResponse:
        captured_request.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-token")
    monkeypatch.setattr(router.requests, "post", fake_post)

    pending_items = [
        router.PendingClassification(
            item_index=1,
            item_text=FRIEND_ROUTER_TEST_DATA["classifierIdea"],
            missing_tags=("ai", "crypto"),
        )
    ]
    classifications = router.call_openrouter_classifier(
        responses_url="https://openrouter.example/responses",
        model="test/model",
        reasoning_effort="low",
        pending_items=pending_items,
    )

    request_json = captured_request["json"]
    assert classifications[1].matches == frozenset({"ai"})
    assert request_json["text"]["format"]["type"] == "json_schema"
    assert request_json["text"]["format"]["strict"] is True
    assert request_json["text"]["format"]["schema"]["additionalProperties"] is False
    item_schema = request_json["text"]["format"]["schema"]["properties"]["items"][
        "items"
    ]
    assert item_schema["properties"]["index"]["enum"] == [1]
    assert item_schema["properties"]["matches"]["items"]["enum"] == ["ai", "crypto"]


def test_classify_missing_items_runs_parallel_prompt_batches() -> None:
    cache = router.empty_cache()
    items = router.extract_scratchpad_items(FRIENDS_INDEX_EXCERPT)
    active_calls = 0
    max_active_calls = 0
    call_lock = threading.Lock()
    started_batches: list[tuple[str, ...]] = []

    def classifier(
        pending_items: list[router.PendingClassification],
    ) -> dict[int, router.Classification]:
        nonlocal active_calls, max_active_calls
        with call_lock:
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            started_batches.append(
                tuple(pending.item_text for pending in pending_items)
            )
        time.sleep(0.02)
        with call_lock:
            active_calls -= 1
        return {
            pending.item_index: router.Classification(
                matches=frozenset({"ai"}),
                non_matches=frozenset(set(pending.missing_tags) - {"ai"}),
            )
            for pending in pending_items
        }

    result = router.classify_missing_items(
        items,
        {"ai", "crypto"},
        cache,
        classifier,
        batch_size=2,
        parallel_prompts=2,
    )

    assert result.classified_items == len(items)
    assert result.failed_items == 0
    assert max_active_calls == 2
    assert sorted(item for batch in started_batches for item in batch) == sorted(items)
    assert sorted(len(batch) for batch in started_batches) == [1, 2]
    assert len(cache["items"]) == len(items)
    for cache_entry in cache["items"].values():
        assert cache_entry["matched_tags"] == ["ai"]
        assert cache_entry["non_matched_tags"] == ["crypto"]


def test_classify_missing_items_retries_and_keeps_going_after_failure() -> None:
    cache = router.empty_cache()
    items = router.extract_scratchpad_items(FRIENDS_INDEX_EXCERPT)
    attempts_by_item: dict[str, int] = {}

    def classifier(
        pending_items: list[router.PendingClassification],
    ) -> dict[int, router.Classification]:
        pending = pending_items[0]
        item_text = pending.item_text
        attempts_by_item[item_text] = attempts_by_item.get(item_text, 0) + 1
        if item_text == items[0]:
            raise RuntimeError("empty OpenRouter response")
        if item_text == items[1] and attempts_by_item[item_text] == 1:
            raise RuntimeError("temporary OpenRouter response parse error")
        return {
            pending.item_index: router.Classification(
                matches=frozenset({"ai"}),
                non_matches=frozenset(set(pending.missing_tags) - {"ai"}),
            )
        }

    result = router.classify_missing_items(
        items,
        {"ai", "crypto"},
        cache,
        classifier,
        batch_size=1,
        parallel_prompts=1,
        max_attempts=2,
        retry_backoff_seconds=0,
    )

    assert result.classified_items == 2
    assert result.failed_items == 1
    assert attempts_by_item[items[0]] == 2
    assert attempts_by_item[items[1]] == 2
    assert attempts_by_item[items[2]] == 1

    failed_entry = router.ensure_cache_item(cache, items[0])
    assert failed_entry["matched_tags"] == []
    assert failed_entry["non_matched_tags"] == []
    for item_text in items[1:]:
        cache_entry = router.ensure_cache_item(cache, item_text)
        assert cache_entry["matched_tags"] == ["ai"]
        assert cache_entry["non_matched_tags"] == ["crypto"]


def test_classify_missing_items_deduplicates_same_cache_key() -> None:
    cache = router.empty_cache()
    source_items = router.extract_scratchpad_items(FRIENDS_INDEX_EXCERPT)
    items = [source_items[0], source_items[1], source_items[0]]
    classified_items: list[str] = []

    def classifier(
        pending_items: list[router.PendingClassification],
    ) -> dict[int, router.Classification]:
        classified_items.extend(pending.item_text for pending in pending_items)
        return {
            pending.item_index: router.Classification(
                matches=frozenset({"ai"}),
                non_matches=frozenset(set(pending.missing_tags) - {"ai"}),
            )
            for pending in pending_items
        }

    result = router.classify_missing_items(
        items,
        {"ai", "crypto"},
        cache,
        classifier,
        batch_size=3,
        parallel_prompts=1,
    )

    assert result.classified_items == 2
    assert classified_items == source_items[:2]
    assert len(cache["items"]) == 2


def test_cache_lock_removes_stale_pid_lock(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    lock_path = tmp_path / "cache.json.lock"
    exited_process = subprocess.Popen(["true"])
    exited_process.wait(timeout=5)
    lock_path.write_text(f"{exited_process.pid}\n", encoding="utf-8")

    with router.cache_lock(cache_path):
        assert lock_path.read_text(encoding="utf-8") == f"{os.getpid()}\n"

    assert not lock_path.exists()


def test_cache_lock_keeps_live_pid_lock(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    lock_path = tmp_path / "cache.json.lock"
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=f"running PID {os.getpid()}"):
        with router.cache_lock(cache_path):
            pass

    assert lock_path.read_text(encoding="utf-8") == f"{os.getpid()}\n"


def test_build_file_routes_skips_previously_routed_file_for_same_item(
    tmp_path: Path,
) -> None:
    item_text = FRIEND_ROUTER_TEST_DATA["zettelkastenIdea"]
    first_note = tmp_path / "agent-memory-optimisation.md"
    second_note = tmp_path / "llm-idea-workbench.md"
    cache = router.empty_cache()
    entry = router.ensure_cache_item(cache, item_text)
    entry["matched_tags"] = ["ai"]
    entry["non_matched_tags"] = ["agents", "memory"]
    entry["routed_files"] = [str(first_note)]

    routes = router.build_file_routes(
        [item_text],
        [
            router.FriendNote(
                link_target="agent-memory-optimisation",
                path=first_note,
                tags=frozenset({"ai", "memory"}),
            ),
            router.FriendNote(
                link_target="llm-idea-workbench",
                path=second_note,
                tags=frozenset({"ai", "epistemology"}),
            ),
        ],
        cache,
    )

    assert routes == {second_note: [item_text]}


def test_append_route_lines_before_scratchpad_and_avoid_duplicate() -> None:
    item_text = FRIEND_ROUTER_TEST_DATA["routedDiscussionIdea"]
    updated_content, first_result = router.append_route_lines_to_content(
        SAM_BREW_EXCERPT,
        [item_text],
        "Routed discussion ideas",
    )

    assert first_result.appended_items == (item_text,)
    assert updated_content.index("# Routed discussion ideas") < updated_content.index(
        "# -- SCRATCHPAD"
    )
    assert f"- {item_text}\n\n# -- SCRATCHPAD" in updated_content

    second_content, second_result = router.append_route_lines_to_content(
        updated_content,
        [item_text],
        "Routed discussion ideas",
    )

    assert second_content == updated_content
    assert second_result.appended_items == ()
    assert second_result.already_present_items == (item_text,)
