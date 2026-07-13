from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from notes_utils import configure_logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
DEFAULT_LOG_PATH = PROJECT_DIR / "friend-idea-router.log"
OPENROUTER_TOKEN_ENV = "OPENROUTER_API_KEY"
FRIENDS_HEADING = "# friends"
SCRATCHPAD_HEADING = "# -- SCRATCHPAD"
ROUTE_CACHE_VERSION = 1
CLASSIFICATION_SCHEMA_NAME = "friend_idea_route_classification"
CLASSIFICATION_PROMPT_ITEM_COUNT = 15
CLASSIFICATION_PARALLEL_PROMPTS = 3
CLASSIFICATION_MAX_ATTEMPTS = 3
CLASSIFICATION_RETRY_BACKOFF_SECONDS = 2.0
WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]\n]+)\]\]")
TAG_TOKEN_RE = re.compile(r"#?([A-Za-z0-9][A-Za-z0-9_/-]*)")
ROUTE_TAG_DESCRIPTIONS = {
    "ai": "artificial intelligence, machine learning, LLMs, AI tooling, evaluation, automation, and software systems shaped by AI",
    "prediction-markets": "market-based forecasting, information discovery, conditional or decision/impact instruments, market scoring, and mechanisms where prices or payoffs aggregate beliefs or direct attention/resources",
    "defi": "decentralized finance protocols, stablecoins, lending, perps, AMMs, liquidity, collateral, yield, and onchain financial mechanism design",
    "crypto": "general blockchain, web3, tokens, wallets, Ethereum, Bitcoin, onchain apps, and protocols not better captured by a narrower route tag",
    "privacy-cryptography": "privacy tech, zero-knowledge proofs, cryptographic protocols, identity, secrets, anonymity, and security primitives",
    "web-of-trust": "reputation, attestations, trust networks, social graph trust, identity credibility, and relationship-mediated verification",
    "governance": "decision-making institutions, DAOs, public-goods or funding mechanisms, policy/process design, and organizational mechanisms",
    "epistemology": "reasoning, truth-seeking, critical rationalism, arguments, refutations, belief formation, and intellectual method",
    "zettelkasten": "notes, knowledge management, memory systems, retrieval, idea organization, and personal knowledge-base workflows",
    "productivity": "workflows, tools, focus, habits, coding or automation for personal output, and practical execution systems",
    "nootropics": "stimulants, supplements, cognitive enhancers, psychiatric meds, and self-experimentation substances aimed at cognition, energy, or focus",
    "psychedelics-consciousness": "psychedelics, meditation, valence, consciousness, subjective experience, and unusual mental-state exploration",
    "health-longevity": "biology, medicine, aging, longevity, health interventions, exercise, nutrition, and bodily wellbeing",
    "dating-social": "dating, relationships, friendship, socializing, community life, and interpersonal strategy",
    "startups": "companies, products, go-to-market, hiring, entrepreneurship, roles, moats, and business strategy",
    "investing": "asset selection, trading, valuation, portfolios, personal investments, and investment opportunities",
    "economics-politics": "macroeconomics, microeconomics, culture, ideology, law, policy, jurisdictions, and social or political trends",
    "travel-local": "places, nomad logistics, local communities/events, accommodation, and city-specific life",
}
APPROVED_ROUTE_TAGS = frozenset(ROUTE_TAG_DESCRIPTIONS)


@dataclass(frozen=True)
class FriendNote:
    link_target: str
    path: Path
    tags: frozenset[str]


@dataclass(frozen=True)
class Classification:
    matches: frozenset[str]
    non_matches: frozenset[str]


@dataclass(frozen=True)
class PendingClassification:
    item_index: int
    item_text: str
    missing_tags: tuple[str, ...]


@dataclass(frozen=True)
class ClassificationBatchOutcome:
    pending_items: tuple[PendingClassification, ...]
    classifications: dict[int, Classification] | None
    error: Exception | None


@dataclass(frozen=True)
class ClassificationRunResult:
    classified_items: int
    failed_items: int


@dataclass(frozen=True)
class AppendResult:
    appended_items: tuple[str, ...]
    already_present_items: tuple[str, ...]

    @property
    def routed_items(self) -> tuple[str, ...]:
        return self.appended_items + self.already_present_items


@dataclass(frozen=True)
class RouterReport:
    scratchpad_items: int
    friend_files: int
    available_tags: int
    classified_items: int
    failed_classifications: int
    appended_items: int
    touched_files: int
    dry_run: bool


def load_project_env(env_path: Path = PROJECT_ROOT / ".env") -> None:
    if os.environ.get(OPENROUTER_TOKEN_ENV) or not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.removeprefix("export ").strip()
        if name != OPENROUTER_TOKEN_ENV:
            continue
        value = value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ[name] = value
        return


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    required_fields = {
        "notesRoot",
        "friendsIndex",
        "cachePath",
        "routeSectionHeading",
        "openRouter",
    }
    missing_fields = sorted(required_fields - set(config))
    if missing_fields:
        raise RuntimeError(f"Router config missing field(s): {', '.join(missing_fields)}")
    return config


def resolve_notes_path(notes_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = notes_root / path
    resolved_root = notes_root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise RuntimeError(f"Configured notes path escapes notes root: {path}")
    return resolved_path


def canonical_item_text(line: str) -> str:
    return " ".join(line.split())


def item_key(item_text: str) -> str:
    return hashlib.sha256(item_text.encode("utf-8")).hexdigest()


def normalize_tag(raw_tag: str) -> str:
    stripped = raw_tag.strip().strip("\"'")
    if stripped.startswith("#"):
        stripped = stripped[1:]
    normalized = stripped.strip().lower()
    if not normalized:
        raise ValueError("Tag cannot be empty")
    if TAG_TOKEN_RE.fullmatch(normalized) is None:
        raise ValueError(f"Unsupported tag format: {raw_tag!r}")
    return normalized


def parse_tag_value(raw_value: str) -> list[str]:
    value = raw_value.strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        return [normalize_tag(part) for part in value[1:-1].split(",") if part.strip()]
    if "," in value:
        return [normalize_tag(part) for part in value.split(",") if part.strip()]
    if "#" in value:
        return [normalize_tag(match.group(0)) for match in TAG_TOKEN_RE.finditer(value)]
    return [normalize_tag(value)]


def split_frontmatter(content: str) -> tuple[list[str], str] | None:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            body = "\n".join(lines[index + 1 :])
            if content.endswith("\n"):
                body += "\n"
            return lines[1:index], body
    raise ValueError("Frontmatter starts with '---' but has no closing delimiter")


def is_top_level_frontmatter_field(line: str) -> bool:
    return bool(line.strip()) and not line.startswith((" ", "\t", "-")) and ":" in line


def read_frontmatter_tags(content: str) -> frozenset[str]:
    frontmatter_parts = split_frontmatter(content)
    if frontmatter_parts is None:
        return frozenset()

    metadata_lines, _ = frontmatter_parts
    tags: set[str] = set()
    index = 0
    while index < len(metadata_lines):
        line = metadata_lines[index]
        field_match = re.match(r"^(tag|tags):\s*(.*)$", line)
        if field_match is None:
            index += 1
            continue

        inline_value = field_match.group(2)
        if inline_value.strip():
            tags.update(parse_tag_value(inline_value))
            index += 1
            continue

        index += 1
        while index < len(metadata_lines) and not is_top_level_frontmatter_field(
            metadata_lines[index]
        ):
            stripped = metadata_lines[index].strip()
            if stripped.startswith("-"):
                tags.update(parse_tag_value(stripped[1:].strip()))
            elif stripped:
                raise RuntimeError(f"Unsupported tag frontmatter line: {metadata_lines[index]}")
            index += 1

    return frozenset(tags)


def wikilink_target(raw_body: str) -> str:
    target_and_fragment = raw_body.split("|", 1)[0].split("#", 1)[0].strip()
    return target_and_fragment[:-3] if target_and_fragment.endswith(".md") else target_and_fragment


def extract_friend_links(index_text: str) -> list[str]:
    links: list[str] = []
    in_friends_section = False
    for line in index_text.splitlines():
        if line.strip() == FRIENDS_HEADING:
            in_friends_section = True
            continue
        if in_friends_section and line.strip() == SCRATCHPAD_HEADING:
            break
        if not in_friends_section:
            continue

        for match in WIKILINK_RE.finditer(line):
            target = wikilink_target(match.group(1))
            if target:
                links.append(target)
    if not links:
        raise RuntimeError(f"No friend wikilinks found under {FRIENDS_HEADING}")
    return links


def extract_scratchpad_items(index_text: str) -> list[str]:
    items: list[str] = []
    in_scratchpad = False
    for line in index_text.splitlines():
        if line.strip() == SCRATCHPAD_HEADING:
            in_scratchpad = True
            continue
        if not in_scratchpad:
            continue

        item = canonical_item_text(line)
        if item:
            items.append(item)
    return items


def friend_note_path(notes_root: Path, link_target: str) -> Path:
    relative_target = Path(link_target)
    if relative_target.is_absolute():
        raise RuntimeError(f"Friend wikilink must be relative: {link_target}")
    if relative_target.suffix != ".md":
        relative_target = relative_target.with_suffix(".md")
    path = (notes_root / relative_target).resolve()
    if not path.is_relative_to(notes_root.resolve()):
        raise RuntimeError(f"Friend wikilink escapes notes root: {link_target}")
    return path


def collect_friend_notes(notes_root: Path, index_path: Path) -> list[FriendNote]:
    index_text = index_path.read_text(encoding="utf-8")
    friend_notes: list[FriendNote] = []
    for link_target in extract_friend_links(index_text):
        path = friend_note_path(notes_root, link_target)
        if not path.exists():
            raise FileNotFoundError(f"Linked friend note not found: {path}")
        tags = read_frontmatter_tags(path.read_text(encoding="utf-8"))
        friend_notes.append(FriendNote(link_target=link_target, path=path, tags=tags))
    return friend_notes


def empty_cache() -> dict[str, Any]:
    return {"version": ROUTE_CACHE_VERSION, "items": {}}


def load_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return empty_cache()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    if cache.get("version") != ROUTE_CACHE_VERSION or not isinstance(
        cache.get("items"), dict
    ):
        raise RuntimeError(f"Unsupported friend idea router cache format: {cache_path}")
    return cache


def save_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f"{cache_path.name}.tmp")
    temp_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(cache_path)


def read_lock_pid(lock_path: Path) -> int:
    lock_text = lock_path.read_text(encoding="utf-8").strip()
    if not lock_text.isdecimal():
        raise RuntimeError(f"Friend idea router cache lock has invalid PID: {lock_path}")
    return int(lock_text)


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def cache_lock(cache_path: Path) -> Iterable[None]:
    lock_path = cache_path.with_name(f"{cache_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            lock_pid = read_lock_pid(lock_path)
            if process_is_running(lock_pid):
                raise RuntimeError(
                    f"Friend idea router cache lock already exists for running PID "
                    f"{lock_pid}: {lock_path}"
                ) from exc
            logger.warning(
                "Removing stale friend idea router cache lock for missing PID {pid}: {path}",
                pid=lock_pid,
                path=lock_path,
            )
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"{os.getpid()}\n")
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def ensure_cache_item(cache: dict[str, Any], item_text: str) -> dict[str, Any]:
    key = item_key(item_text)
    items = cache.setdefault("items", {})
    if key not in items:
        items[key] = {
            "text": item_text,
            "matched_tags": [],
            "non_matched_tags": [],
            "routed_files": [],
            "created_at": datetime.now(UTC).isoformat(),
        }
    entry = items[key]
    if entry.get("text") != item_text:
        raise RuntimeError(f"Cache key collision for scratchpad item: {item_text}")
    return entry


def missing_classification_tags(entry: dict[str, Any], available_tags: set[str]) -> list[str]:
    known_tags = set(entry.get("matched_tags", [])) | set(entry.get("non_matched_tags", []))
    return sorted(available_tags - known_tags)


def apply_classification(entry: dict[str, Any], classification: Classification) -> None:
    matched_tags = set(entry.get("matched_tags", []))
    non_matched_tags = set(entry.get("non_matched_tags", []))
    matched_tags.update(classification.matches)
    non_matched_tags.update(classification.non_matches)
    overlap = matched_tags & non_matched_tags
    if overlap:
        raise RuntimeError(f"Cache classification overlap for tags: {', '.join(sorted(overlap))}")
    entry["matched_tags"] = sorted(matched_tags)
    entry["non_matched_tags"] = sorted(non_matched_tags)
    entry["classified_at"] = datetime.now(UTC).isoformat()


def response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    pieces: list[str] = []
    for output in payload.get("output") or []:
        if output.get("type") != "message":
            continue
        for content in output.get("content") or []:
            if content.get("type") != "output_text":
                continue
            text = content.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "\n".join(pieces)


def parse_llm_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenRouter response JSON must be an object")
    return parsed


def validate_supported_route_tags(tags: Iterable[str]) -> None:
    unsupported_tags = sorted(set(tags) - APPROVED_ROUTE_TAGS)
    if unsupported_tags:
        raise RuntimeError(
            "Unsupported friend idea route tag(s): "
            f"{', '.join(unsupported_tags)}. Approved tags are: "
            f"{', '.join(sorted(APPROVED_ROUTE_TAGS))}"
        )


def render_candidate_tag_guide(candidate_tags: list[str]) -> str:
    validate_supported_route_tags(candidate_tags)
    return "\n".join(
        f"- {tag}: {ROUTE_TAG_DESCRIPTIONS[tag]}" for tag in candidate_tags
    )


def batch_candidate_tags(pending_items: list[PendingClassification]) -> list[str]:
    return sorted({tag for pending in pending_items for tag in pending.missing_tags})


def classification_prompt(pending_items: list[PendingClassification]) -> str:
    candidate_tags = batch_candidate_tags(pending_items)
    item_blocks = "\n\n".join(
        "\n".join(
            [
                f"Index: {pending.item_index}",
                f"Discussion idea: {pending.item_text}",
                "Candidate tags: "
                f"{json.dumps(list(pending.missing_tags), ensure_ascii=True)}",
            ]
        )
        for pending in pending_items
    )
    return (
        "Classify discussion ideas against route tags for personal friend notes.\n"
        "Use the tags inclusively: mark a tag as a match when the idea is "
        "substantially connected, adjacent, or likely to be a useful discussion "
        "thread for a friend note carrying that tag. Avoid only extremely remote "
        "or generic associations.\n\n"
        "Tag meanings:\n"
        f"{render_candidate_tag_guide(candidate_tags)}\n\n"
        "Return only JSON with exactly one top-level key: items.\n"
        "For each input item, return an object with exactly these keys: "
        "index, matches, non_matches. Every candidate tag listed for that item "
        "must appear in exactly one of matches or non_matches. Use only candidate "
        "tags exactly as written. Do not invent tags.\n\n"
        "Discussion ideas:\n"
        f"{item_blocks}"
    )


def classification_text_format(pending_items: list[PendingClassification]) -> dict[str, Any]:
    candidate_tags = batch_candidate_tags(pending_items)
    validate_supported_route_tags(candidate_tags)
    tag_schema = {
        "type": "array",
        "items": {
            "type": "string",
            "enum": candidate_tags,
        },
    }
    item_schema = {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "enum": [pending.item_index for pending in pending_items],
            },
            "matches": tag_schema,
            "non_matches": tag_schema,
        },
        "required": ["index", "matches", "non_matches"],
        "additionalProperties": False,
    }
    return {
        "format": {
            "type": "json_schema",
            "name": CLASSIFICATION_SCHEMA_NAME,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": item_schema,
                    },
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
    }


def validate_classification(payload: dict[str, Any], candidate_tags: list[str]) -> Classification:
    candidate_set = set(candidate_tags)
    matches = payload.get("matches")
    non_matches = payload.get("non_matches")
    if not isinstance(matches, list) or not isinstance(non_matches, list):
        raise RuntimeError("OpenRouter response must contain matches and non_matches arrays")

    match_tags = [normalize_tag(str(tag)) for tag in matches]
    non_match_tags = [normalize_tag(str(tag)) for tag in non_matches]
    returned_tags = match_tags + non_match_tags
    duplicate_tags = {
        tag
        for tag in returned_tags
        if returned_tags.count(tag) > 1
    }
    if duplicate_tags:
        raise RuntimeError(f"OpenRouter response duplicated tag(s): {sorted(duplicate_tags)}")

    match_set = set(match_tags)
    non_match_set = set(non_match_tags)
    overlap = match_set & non_match_set
    if overlap:
        raise RuntimeError(f"OpenRouter response placed tag(s) in both arrays: {sorted(overlap)}")
    unknown_tags = (match_set | non_match_set) - candidate_set
    if unknown_tags:
        raise RuntimeError(f"OpenRouter response used unknown tag(s): {sorted(unknown_tags)}")
    missing_tags = candidate_set - match_set - non_match_set
    if missing_tags:
        raise RuntimeError(f"OpenRouter response omitted tag(s): {sorted(missing_tags)}")
    return Classification(matches=frozenset(match_set), non_matches=frozenset(non_match_set))


def validate_classification_batch(
    payload: dict[str, Any],
    pending_items: list[PendingClassification],
) -> dict[int, Classification]:
    response_items = payload.get("items")
    if not isinstance(response_items, list):
        raise RuntimeError("OpenRouter response must contain an items array")

    expected_by_index = {pending.item_index: pending for pending in pending_items}
    response_by_index: dict[int, dict[str, Any]] = {}
    for response_item in response_items:
        if not isinstance(response_item, dict):
            raise RuntimeError("OpenRouter response items must be objects")
        response_index = response_item.get("index")
        if not isinstance(response_index, int) or isinstance(response_index, bool):
            raise RuntimeError("OpenRouter response item index must be an integer")
        if response_index in response_by_index:
            raise RuntimeError(
                f"OpenRouter response duplicated item index: {response_index}"
            )
        response_by_index[response_index] = response_item

    unknown_indexes = sorted(set(response_by_index) - set(expected_by_index))
    if unknown_indexes:
        raise RuntimeError(
            f"OpenRouter response used unknown item index(es): {unknown_indexes}"
        )
    missing_indexes = sorted(set(expected_by_index) - set(response_by_index))
    if missing_indexes:
        raise RuntimeError(
            f"OpenRouter response omitted item index(es): {missing_indexes}"
        )

    return {
        item_index: validate_classification(
            response_by_index[item_index],
            list(expected_by_index[item_index].missing_tags),
        )
        for item_index in expected_by_index
    }


def call_openrouter_classifier(
    *,
    responses_url: str,
    model: str,
    reasoning_effort: str,
    pending_items: list[PendingClassification],
) -> dict[int, Classification]:
    token = require_env(OPENROUTER_TOKEN_ENV)
    response = requests.post(
        responses_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "reasoning": {"effort": reasoning_effort},
            "input": classification_prompt(pending_items),
            "text": classification_text_format(pending_items),
        },
        timeout=180,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            "OpenRouter request failed with HTTP "
            f"{response.status_code}: {response.text[:500]}"
        )
    return validate_classification_batch(
        parse_llm_json(response_text(response.json())),
        pending_items,
    )


def route_line(item_text: str) -> str:
    return f"- {item_text}"


def append_route_lines_to_content(
    content: str,
    item_texts: list[str],
    route_section_heading: str,
) -> tuple[str, AppendResult]:
    lines = content.splitlines()
    existing_lines = set(lines)
    new_item_texts = [
        item_text for item_text in item_texts if route_line(item_text) not in existing_lines
    ]
    new_route_lines = [route_line(item_text) for item_text in new_item_texts]
    already_present_items = tuple(
        item_text for item_text in item_texts if route_line(item_text) in existing_lines
    )
    if not new_route_lines:
        return content, AppendResult(appended_items=(), already_present_items=already_present_items)

    route_heading = f"# {route_section_heading}"
    try:
        heading_index = next(
            index for index, line in enumerate(lines) if line.strip() == route_heading
        )
    except StopIteration:
        insertion_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip() == SCRATCHPAD_HEADING
            ),
            len(lines),
        )
        block = []
        if insertion_index > 0 and lines[insertion_index - 1].strip():
            block.append("")
        block.append(route_heading)
        block.extend(new_route_lines)
        if insertion_index < len(lines) and lines[insertion_index].strip():
            block.append("")
        lines[insertion_index:insertion_index] = block
    else:
        insertion_index = heading_index + 1
        while insertion_index < len(lines) and not lines[insertion_index].startswith("# "):
            insertion_index += 1
        while insertion_index > heading_index + 1 and not lines[insertion_index - 1].strip():
            insertion_index -= 1
        block = list(new_route_lines)
        if insertion_index < len(lines) and lines[insertion_index].strip():
            block.append("")
        lines[insertion_index:insertion_index] = block

    return "\n".join(lines).rstrip("\n") + "\n", AppendResult(
        appended_items=tuple(new_item_texts),
        already_present_items=already_present_items,
    )


def append_items_to_note(
    path: Path,
    item_texts: list[str],
    route_section_heading: str,
    *,
    dry_run: bool,
) -> AppendResult:
    content = path.read_text(encoding="utf-8")
    updated_content, result = append_route_lines_to_content(
        content,
        item_texts,
        route_section_heading,
    )
    if not dry_run and updated_content != content:
        path.write_text(updated_content, encoding="utf-8")
    return result


def build_file_routes(
    items: list[str],
    friend_notes: list[FriendNote],
    cache: dict[str, Any],
) -> dict[Path, list[str]]:
    file_routes: dict[Path, list[str]] = {}
    for item_text in items:
        entry = ensure_cache_item(cache, item_text)
        matched_tags = set(entry.get("matched_tags", []))
        if not matched_tags:
            continue
        routed_files = set(entry.get("routed_files", []))
        for friend_note in friend_notes:
            if not matched_tags.intersection(friend_note.tags):
                continue
            if str(friend_note.path) in routed_files:
                continue
            file_routes.setdefault(friend_note.path, []).append(item_text)
    return file_routes


def mark_routed(cache: dict[str, Any], item_text: str, path: Path) -> None:
    entry = ensure_cache_item(cache, item_text)
    routed_files = set(entry.get("routed_files", []))
    routed_files.add(str(path))
    entry["routed_files"] = sorted(routed_files)
    entry["routed_at"] = datetime.now(UTC).isoformat()


def pending_classifications(
    items: list[str],
    cache: dict[str, Any],
    available_tags: set[str],
) -> list[PendingClassification]:
    pending: list[PendingClassification] = []
    pending_keys: set[str] = set()
    for item_index, item_text in enumerate(items, start=1):
        key = item_key(item_text)
        if key in pending_keys:
            continue
        pending_keys.add(key)
        entry = ensure_cache_item(cache, item_text)
        missing_tags = tuple(missing_classification_tags(entry, available_tags))
        if missing_tags:
            pending.append(
                PendingClassification(
                    item_index=item_index,
                    item_text=item_text,
                    missing_tags=missing_tags,
                )
            )
    return pending


def classification_batches(
    pending_items: list[PendingClassification],
    batch_size: int,
) -> Iterable[list[PendingClassification]]:
    if batch_size < 1:
        raise ValueError("Classification batch size must be at least 1")
    for start_index in range(0, len(pending_items), batch_size):
        yield pending_items[start_index : start_index + batch_size]


def run_classification_batch(
    batches: list[list[PendingClassification]],
    classifier: Callable[[list[PendingClassification]], dict[int, Classification]],
    *,
    parallel_prompts: int = CLASSIFICATION_PARALLEL_PROMPTS,
    max_attempts: int = CLASSIFICATION_MAX_ATTEMPTS,
    retry_backoff_seconds: float = CLASSIFICATION_RETRY_BACKOFF_SECONDS,
) -> Iterable[ClassificationBatchOutcome]:
    if parallel_prompts < 1:
        raise ValueError("Classification parallel prompt count must be at least 1")

    with ThreadPoolExecutor(max_workers=min(parallel_prompts, len(batches))) as executor:
        future_to_batch: dict[
            Future[dict[int, Classification]],
            list[PendingClassification],
        ] = {
            executor.submit(
                classify_with_retries,
                classifier,
                batch,
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
            ): batch
            for batch in batches
        }
        for future in as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                yield ClassificationBatchOutcome(
                    pending_items=tuple(batch),
                    classifications=future.result(),
                    error=None,
                )
            except Exception as exc:
                yield ClassificationBatchOutcome(
                    pending_items=tuple(batch),
                    classifications=None,
                    error=exc,
                )


def classify_with_retries(
    classifier: Callable[[list[PendingClassification]], dict[int, Classification]],
    pending_items: list[PendingClassification],
    *,
    max_attempts: int,
    retry_backoff_seconds: float,
) -> dict[int, Classification]:
    if max_attempts < 1:
        raise ValueError("Classification max attempts must be at least 1")

    item_numbers = ", ".join(str(pending.item_index) for pending in pending_items)
    next_delay = retry_backoff_seconds
    for attempt in range(1, max_attempts + 1):
        try:
            return classifier(pending_items)
        except Exception as exc:
            if attempt == max_attempts:
                raise
            logger.warning(
                "Classification attempt {attempt}/{max_attempts} failed for scratchpad item(s) {item_numbers}; retrying in {delay:.1f}s: {error}",
                attempt=attempt,
                max_attempts=max_attempts,
                item_numbers=item_numbers,
                delay=next_delay,
                error=exc,
            )
            time.sleep(next_delay)
            next_delay *= 2

    raise RuntimeError("Classification retry loop exited unexpectedly")


def classify_missing_items(
    items: list[str],
    available_tags: set[str],
    cache: dict[str, Any],
    classifier: Callable[[list[PendingClassification]], dict[int, Classification]],
    *,
    batch_size: int = CLASSIFICATION_PROMPT_ITEM_COUNT,
    parallel_prompts: int = CLASSIFICATION_PARALLEL_PROMPTS,
    max_attempts: int = CLASSIFICATION_MAX_ATTEMPTS,
    retry_backoff_seconds: float = CLASSIFICATION_RETRY_BACKOFF_SECONDS,
) -> ClassificationRunResult:
    pending_items = pending_classifications(items, cache, available_tags)
    classified_items = 0
    failed_items = 0
    if not pending_items:
        return ClassificationRunResult(
            classified_items=classified_items,
            failed_items=failed_items,
        )

    batches = list(classification_batches(pending_items, batch_size))
    for batch_number, batch in enumerate(batches, start=1):
        logger.info(
            "Queued classification prompt {batch_number}/{batch_count} with {batch_size} scratchpad item(s)",
            batch_number=batch_number,
            batch_count=len(batches),
            batch_size=len(batch),
        )

    for outcome in run_classification_batch(
        batches,
        classifier,
        parallel_prompts=parallel_prompts,
        max_attempts=max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
    ):
        if outcome.error is not None:
            failed_items += len(outcome.pending_items)
            item_numbers = ", ".join(
                str(pending.item_index) for pending in outcome.pending_items
            )
            logger.error(
                "Classification failed after retries for scratchpad item(s) {item_numbers}: {error}",
                item_numbers=item_numbers,
                error=outcome.error,
            )
            continue
        if outcome.classifications is None:
            raise RuntimeError("Classification outcome missing result and error")

        for pending in outcome.pending_items:
            classification = outcome.classifications.get(pending.item_index)
            if classification is None:
                raise RuntimeError(
                    f"Classification outcome omitted item index {pending.item_index}"
                )
            try:
                apply_classification(
                    ensure_cache_item(cache, pending.item_text),
                    classification,
                )
            except Exception as exc:
                failed_items += 1
                logger.error(
                    "Classification failed for scratchpad item {item_number}/{item_count}: {item} | {error}",
                    item_number=pending.item_index,
                    item_count=len(items),
                    item=pending.item_text[:120],
                    error=exc,
                )
                continue

            classified_items += 1
            logger.info(
                "Classified scratchpad item {item_number}/{item_count}: {item}",
                item_number=pending.item_index,
                item_count=len(items),
                item=pending.item_text[:120],
            )
    return ClassificationRunResult(
        classified_items=classified_items,
        failed_items=failed_items,
    )


def run_router(
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    max_items: int | None = None,
    classifier: Callable[[list[PendingClassification]], dict[int, Classification]]
    | None = None,
) -> RouterReport:
    notes_root = Path(config["notesRoot"]).expanduser().resolve()
    index_path = resolve_notes_path(notes_root, config["friendsIndex"])
    cache_path = resolve_notes_path(notes_root, config["cachePath"])

    index_text = index_path.read_text(encoding="utf-8")
    items = extract_scratchpad_items(index_text)
    if max_items is not None:
        items = items[:max_items]

    friend_notes = collect_friend_notes(notes_root, index_path)
    tagged_friend_notes = [friend_note for friend_note in friend_notes if friend_note.tags]
    available_tags = sorted({tag for friend_note in tagged_friend_notes for tag in friend_note.tags})
    if not available_tags:
        logger.warning("No frontmatter tags found in linked friend notes; nothing to route")
        return RouterReport(
            scratchpad_items=len(items),
            friend_files=len(friend_notes),
            available_tags=0,
            classified_items=0,
            failed_classifications=0,
            appended_items=0,
            touched_files=0,
            dry_run=dry_run,
        )
    validate_supported_route_tags(available_tags)

    openrouter_config = config["openRouter"]
    if classifier is None:
        load_project_env()
        classifier = lambda pending_items: call_openrouter_classifier(
            responses_url=openrouter_config["responsesUrl"],
            model=openrouter_config["model"],
            reasoning_effort=openrouter_config["reasoningEffort"],
            pending_items=pending_items,
        )

    def process_with_cache(cache: dict[str, Any]) -> RouterReport:
        available_tag_set = set(available_tags)
        classification_result = classify_missing_items(
            items,
            available_tag_set,
            cache,
            classifier,
        )

        file_routes = build_file_routes(items, tagged_friend_notes, cache)
        appended_items = 0
        touched_files = 0
        for path, route_items in file_routes.items():
            result = append_items_to_note(
                path,
                route_items,
                config["routeSectionHeading"],
                dry_run=dry_run,
            )
            if result.routed_items:
                touched_files += 1
            appended_items += len(result.appended_items)
            if not dry_run:
                for item_text in result.routed_items:
                    mark_routed(cache, item_text, path)

        return RouterReport(
            scratchpad_items=len(items),
            friend_files=len(friend_notes),
            available_tags=len(available_tags),
            classified_items=classification_result.classified_items,
            failed_classifications=classification_result.failed_items,
            appended_items=appended_items,
            touched_files=touched_files,
            dry_run=dry_run,
        )

    if dry_run:
        return process_with_cache(load_cache(cache_path))

    with cache_lock(cache_path):
        cache = load_cache(cache_path)
        report = process_with_cache(cache)
        save_cache(cache_path, cache)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Route friends-index.md scratchpad discussion ideas into tagged friend notes."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-items", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logger(args.log_path)
    config = load_config(args.config.expanduser())
    report = run_router(config, dry_run=args.dry_run, max_items=args.max_items)
    logger.info(
        "Processed {items} scratchpad item(s), {tags} available tag(s), "
        "{classified} classified item(s), {failed} failed classification(s), "
        "{appended} appended item(s) across {files} file(s){mode}",
        items=report.scratchpad_items,
        tags=report.available_tags,
        classified=report.classified_items,
        failed=report.failed_classifications,
        appended=report.appended_items,
        files=report.touched_files,
        mode=" [dry-run]" if report.dry_run else "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
