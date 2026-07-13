#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import random
import re
from typing import Any, Sequence
from urllib.parse import urlparse

import requests


REVIEWED_ARTICLE_PATTERN = re.compile(
    r"^(?:-\s*)?(?:Analysed|Reviewed) article ID:\s*`?([0-9a-fA-F-]{36})`?\s*$",
    re.MULTILINE,
)
BATCH_DATE_PATTERN = re.compile(r"^## Infolio relevance (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


@dataclass(frozen=True)
class InfolioSettings:
    supabase_url: str
    owner_user_id: str
    view_slug: str
    queue_filter: str
    pool_size: int
    sample_size: int


@dataclass(frozen=True)
class RankedArticle:
    article_id: str
    title: str
    lineate_url: str
    weighted_score: float
    any_sort_score: float
    all_sort_score: float
    none_sort_score: float


def required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def positive_integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def finite_score(value: Any, field_name: str) -> float:
    score = float(0 if value is None else value)
    if not math.isfinite(score):
        raise ValueError(f"Candidate {field_name} must be finite")
    return score


def load_settings(config_path: Path) -> InfolioSettings:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_settings = config.get("infolioArticleRelevance")
    if not isinstance(raw_settings, dict):
        raise ValueError("config.json must contain infolioArticleRelevance settings")
    return InfolioSettings(
        supabase_url=required_string(raw_settings.get("supabaseUrl"), "supabaseUrl").rstrip("/"),
        owner_user_id=required_string(raw_settings.get("ownerUserId"), "ownerUserId"),
        view_slug=required_string(raw_settings.get("viewSlug"), "viewSlug"),
        queue_filter=required_string(raw_settings.get("queueFilter"), "queueFilter"),
        pool_size=positive_integer(raw_settings.get("poolSize"), "poolSize"),
        sample_size=positive_integer(raw_settings.get("sampleSize"), "sampleSize"),
    )


def response_json(response: requests.Response, operation: str) -> Any:
    try:
        payload = response.json()
    except requests.JSONDecodeError as error:
        raise RuntimeError(f"Infolio {operation} returned invalid JSON") from error
    if not response.ok:
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("error")
        else:
            detail = None
        raise RuntimeError(f"Infolio {operation} failed with HTTP {response.status_code}: {detail or 'unknown error'}")
    return payload


def fetch_ranked_articles(
    settings: InfolioSettings,
    service_role_key: str,
    session: requests.Session | None = None,
) -> list[RankedArticle]:
    http_session = session or requests.Session()
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
    }
    views_response = http_session.get(
        f"{settings.supabase_url}/rest/v1/views",
        headers=headers,
        params={
            "select": "id",
            "owner_id": f"eq.{settings.owner_user_id}",
            "slug": f"eq.{settings.view_slug}",
        },
        timeout=30,
    )
    views = response_json(views_response, "view lookup")
    if not isinstance(views, list) or len(views) != 1 or not isinstance(views[0], dict):
        raise RuntimeError(f"Expected exactly one Infolio view with slug {settings.view_slug!r}")
    view_id = required_string(views[0].get("id"), "view id")

    candidates_response = http_session.post(
        f"{settings.supabase_url}/rest/v1/rpc/get_view_candidates",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "target_view_id": view_id,
            "target_owner_id": settings.owner_user_id,
            "queue_filter": settings.queue_filter,
        },
        timeout=60,
    )
    candidates = response_json(candidates_response, "candidate lookup")
    if not isinstance(candidates, list):
        raise RuntimeError("Infolio candidate lookup must return a JSON array")

    ranked_articles = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise RuntimeError("Every Infolio candidate must be a JSON object")
        lineate_url = required_string(candidate.get("lineate_url"), "lineate_url")
        parsed_url = urlparse(lineate_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(f"Invalid Lineate URL for article {candidate.get('article_id')}: {lineate_url}")
        ranked_articles.append(
            RankedArticle(
                article_id=required_string(candidate.get("article_id"), "article_id"),
                title=required_string(candidate.get("title"), "title"),
                lineate_url=lineate_url,
                weighted_score=finite_score(candidate.get("weighted_score"), "weighted_score"),
                any_sort_score=finite_score(candidate.get("any_sort_score"), "any_sort_score"),
                all_sort_score=finite_score(candidate.get("all_sort_score"), "all_sort_score"),
                none_sort_score=finite_score(candidate.get("none_sort_score"), "none_sort_score"),
            )
        )

    return sorted(
        ranked_articles,
        key=lambda article: (
            article.weighted_score,
            article.any_sort_score,
            article.all_sort_score,
            article.none_sort_score,
        ),
        reverse=True,
    )


def reviewed_article_ids(feedback_path: Path) -> set[str]:
    feedback = feedback_path.read_text(encoding="utf-8")
    return {match.group(1).lower() for match in REVIEWED_ARTICLE_PATTERN.finditer(feedback)}


def completed_batch_dates(feedback_path: Path) -> set[str]:
    feedback = feedback_path.read_text(encoding="utf-8")
    return {match.group(1) for match in BATCH_DATE_PATTERN.finditer(feedback)}


def select_articles(
    ranked_articles: Sequence[RankedArticle],
    excluded_article_ids: set[str],
    pool_size: int,
    sample_size: int,
    random_source: random.Random | random.SystemRandom | None = None,
) -> tuple[list[RankedArticle], int]:
    eligible_articles = [
        article
        for article in ranked_articles
        if article.article_id.lower() not in excluded_article_ids
    ]
    candidate_pool = eligible_articles[:pool_size]
    selection_size = min(sample_size, len(candidate_pool))
    selected_articles = (random_source or random.SystemRandom()).sample(candidate_pool, selection_size)
    return selected_articles, len(candidate_pool)


def selection_payload(
    settings: InfolioSettings,
    selected_articles: Sequence[RankedArticle],
    candidate_pool_size: int,
    ranked_articles: Sequence[RankedArticle],
) -> dict[str, Any]:
    rank_by_article_id = {
        article.article_id: rank
        for rank, article in enumerate(ranked_articles, start=1)
    }
    return {
        "selected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "view": settings.view_slug,
        "queue_filter": settings.queue_filter,
        "candidate_pool_size": candidate_pool_size,
        "articles": [
            {
                **asdict(article),
                "infolio_rank": rank_by_article_id[article.article_id],
            }
            for article in selected_articles
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select unreviewed articles from the ranked Infolio queue.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("notes/config.json"),
        help="Notes-tools config file.",
    )
    parser.add_argument(
        "--feedback-file",
        type=Path,
        required=True,
        help="Scheduled skill feedback file used to exclude reviewed article IDs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings(args.config)
    today = datetime.now().astimezone().date().isoformat()
    if today in completed_batch_dates(args.feedback_file):
        print(json.dumps({
            "selected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "view": settings.view_slug,
            "queue_filter": settings.queue_filter,
            "candidate_pool_size": 0,
            "articles": [],
            "skip_reason": f"An Infolio relevance batch already completed on {today}",
        }, indent=2))
        return
    service_role_key = os.environ.get("INFOLIO_SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not service_role_key:
        raise RuntimeError("INFOLIO_SUPABASE_SERVICE_ROLE_KEY is not configured")
    ranked_articles = fetch_ranked_articles(settings, service_role_key)
    selected_articles, candidate_pool_size = select_articles(
        ranked_articles,
        reviewed_article_ids(args.feedback_file),
        settings.pool_size,
        settings.sample_size,
    )
    print(json.dumps(
        selection_payload(settings, selected_articles, candidate_pool_size, ranked_articles),
        indent=2,
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
