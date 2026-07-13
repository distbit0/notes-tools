from __future__ import annotations

import os
from pathlib import Path
import random

import pytest

from select_infolio_relevance_articles import (
    fetch_ranked_articles,
    load_settings,
    reviewed_article_ids,
    select_articles,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "notes/config.json"


@pytest.fixture(scope="module")
def live_ranked_articles():
    service_role_key = os.environ.get("INFOLIO_SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not service_role_key:
        pytest.skip("INFOLIO_SUPABASE_SERVICE_ROLE_KEY is required for live Infolio tests")
    try:
        articles = fetch_ranked_articles(load_settings(CONFIG_PATH), service_role_key)
    except RuntimeError as error:
        if "column views.owner_id does not exist" in str(error):
            pytest.skip("The live Infolio ownership and Lineate-document migration is not deployed")
        raise
    if not articles:
        pytest.skip("The live Infolio unread queue has no candidates")
    return articles


def test_selection_uses_unique_articles_from_top_eligible_pool(live_ranked_articles) -> None:
    settings = load_settings(CONFIG_PATH)
    selected, candidate_pool_size = select_articles(
        live_ranked_articles,
        set(),
        settings.pool_size,
        settings.sample_size,
        random.Random(20260712),
    )
    top_candidate_ids = {
        article.article_id
        for article in live_ranked_articles[: settings.pool_size]
    }

    assert candidate_pool_size == min(settings.pool_size, len(live_ranked_articles))
    assert len(selected) == min(settings.sample_size, candidate_pool_size)
    assert len({article.article_id for article in selected}) == len(selected)
    assert {article.article_id for article in selected} <= top_candidate_ids
    assert all(article.lineate_url.startswith(("http://", "https://")) for article in selected)


@pytest.mark.parametrize(
    "marker_prefix",
    ["Analysed article ID:", "- Reviewed article ID:"],
)
def test_feedback_marker_excludes_an_actual_article(
    live_ranked_articles,
    tmp_path: Path,
    marker_prefix: str,
) -> None:
    excluded_article = live_ranked_articles[0]
    feedback_path = tmp_path / "feedback.md"
    feedback_path.write_text(
        f"# Feedback\n\n{marker_prefix} `{excluded_article.article_id}`\n",
        encoding="utf-8",
    )

    selected, _candidate_pool_size = select_articles(
        live_ranked_articles,
        reviewed_article_ids(feedback_path),
        len(live_ranked_articles),
        len(live_ranked_articles),
        random.Random(20260712),
    )

    assert excluded_article.article_id not in {article.article_id for article in selected}


def test_selection_returns_every_available_actual_candidate_when_under_limit(
    live_ranked_articles,
) -> None:
    actual_candidate_slice = live_ranked_articles[: min(4, len(live_ranked_articles))]
    selected, candidate_pool_size = select_articles(
        actual_candidate_slice,
        set(),
        30,
        5,
        random.Random(20260712),
    )

    assert candidate_pool_size == len(actual_candidate_slice)
    assert {article.article_id for article in selected} == {
        article.article_id for article in actual_candidate_slice
    }
