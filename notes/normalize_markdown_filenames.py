from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


DEFAULT_NOTES_ROOT = Path.home() / "notes"
DEFAULT_LOG_PATH = Path(__file__).with_name("normalize-markdown-filenames.log")
PROTECTED_FILENAMES = {"AGENTS.md", "README.md", "decision-log.md"}
PROTECTED_FILENAME_PREFIXES = ("msg - ",)
WIKILINK_PATTERN = re.compile(r"(?P<embed>!)?(?<!\\)\[\[(?P<body>[^\]\n]+)\]\]")
FENCE_PATTERN = re.compile(r"\s*(```|~~~)")


@dataclass(frozen=True)
class RenamePlan:
    source: Path
    target: Path


@dataclass(frozen=True)
class NormalizationReport:
    renamed_files: int
    updated_links: int
    updated_files: int


def configure_logger(log_path: Path) -> None:
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.add(
        log_path,
        level="INFO",
        rotation="100 KB",
        retention=5,
        encoding="utf-8",
    )


def slugify_stem(stem: str) -> str:
    ascii_stem = (
        unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    )
    ascii_stem = ascii_stem.lower().replace("&", " and ")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_stem).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive a slug from Markdown filename stem: {stem!r}")
    return slug


def compliant_markdown_name(path: Path) -> str:
    return f"{slugify_stem(path.stem)}{path.suffix.lower()}"


def is_hidden_or_git_path(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    return any(part.startswith(".") for part in relative_parts)


def markdown_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and not is_hidden_or_git_path(path, root)
    )


def build_rename_plan(root: Path) -> list[RenamePlan]:
    plans = []
    for path in markdown_files(root):
        if path.name in PROTECTED_FILENAMES or path.name.startswith(
            PROTECTED_FILENAME_PREFIXES
        ):
            continue

        compliant_name = compliant_markdown_name(path)
        if path.name == compliant_name:
            continue
        plans.append(RenamePlan(source=path, target=path.with_name(compliant_name)))

    validate_rename_plan(plans)
    return plans


def validate_rename_plan(plans: list[RenamePlan]) -> None:
    targets: dict[Path, Path] = {}
    for plan in plans:
        existing_source = targets.get(plan.target)
        if existing_source is not None:
            raise RuntimeError(
                f"Filename normalization collision: {existing_source} and "
                f"{plan.source} both map to {plan.target}"
            )
        targets[plan.target] = plan.source

    sources = {plan.source for plan in plans}
    for plan in plans:
        if plan.target.exists() and plan.target not in sources:
            raise RuntimeError(
                f"Filename normalization would overwrite existing file: "
                f"{plan.source} -> {plan.target}"
            )


def link_target_map(root: Path, plans: list[RenamePlan]) -> dict[str, str]:
    target_map: dict[str, str] = {}
    for plan in plans:
        old_relative_stem = plan.source.relative_to(root).with_suffix("").as_posix()
        new_relative_stem = plan.target.relative_to(root).with_suffix("").as_posix()
        target_map[old_relative_stem] = new_relative_stem
        target_map[f"{old_relative_stem}.md"] = new_relative_stem

        target_map[plan.source.stem] = plan.target.stem
        target_map[f"{plan.source.stem}.md"] = plan.target.stem
    return target_map


def split_wikilink_body(body: str) -> tuple[str, str, str]:
    target_and_fragment, pipe, alias = body.partition("|")
    target, hash_mark, fragment = target_and_fragment.partition("#")
    return target.strip(), hash_mark + fragment if hash_mark else "", pipe + alias if pipe else ""


def is_inline_code(line: str, index: int) -> bool:
    return line[:index].count("`") % 2 == 1


def rewrite_wikilinks_in_text(text: str, replacements: dict[str, str]) -> tuple[str, int]:
    updated_links = 0
    rewritten_lines = []
    in_fence = False

    for line in text.splitlines(keepends=True):
        if FENCE_PATTERN.match(line):
            in_fence = not in_fence
            rewritten_lines.append(line)
            continue
        if in_fence:
            rewritten_lines.append(line)
            continue

        def replace(match: re.Match[str]) -> str:
            nonlocal updated_links
            if is_inline_code(line, match.start()):
                return match.group(0)

            body = match.group("body")
            target, fragment, alias = split_wikilink_body(body)
            replacement = replacements.get(target)
            if replacement is None:
                return match.group(0)

            updated_links += 1
            return f"{match.group('embed') or ''}[[{replacement}{fragment}{alias}]]"

        rewritten_lines.append(WIKILINK_PATTERN.sub(replace, line))

    return "".join(rewritten_lines), updated_links


def apply_renames(plans: list[RenamePlan], *, dry_run: bool) -> None:
    for plan in plans:
        logger.info(f"Rename Markdown note: {plan.source} -> {plan.target}")
        if not dry_run:
            plan.source.rename(plan.target)


def update_wikilinks(root: Path, replacements: dict[str, str], *, dry_run: bool) -> tuple[int, int]:
    updated_files = 0
    updated_links = 0
    for path in markdown_files(root):
        text = path.read_text(encoding="utf-8")
        rewritten, link_count = rewrite_wikilinks_in_text(text, replacements)
        if rewritten == text:
            continue

        logger.info(f"Update {link_count} wikilink target(s) in {path}")
        updated_files += 1
        updated_links += link_count
        if not dry_run:
            path.write_text(rewritten, encoding="utf-8")
    return updated_files, updated_links


def normalize_notes(root: Path, *, dry_run: bool) -> NormalizationReport:
    if not root.exists():
        raise FileNotFoundError(f"Notes root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Notes root is not a directory: {root}")

    plans = build_rename_plan(root)
    replacements = link_target_map(root, plans)

    apply_renames(plans, dry_run=dry_run)
    updated_files, updated_links = update_wikilinks(root, replacements, dry_run=dry_run)

    return NormalizationReport(
        renamed_files=len(plans),
        updated_links=updated_links,
        updated_files=updated_files,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename Markdown files in the notes vault to slug filenames and update wikilinks."
    )
    parser.add_argument("--notes-root", type=Path, default=DEFAULT_NOTES_ROOT)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logger(args.log_path)
    report = normalize_notes(args.notes_root.expanduser(), dry_run=args.dry_run)
    logger.info(
        "Markdown filename normalization complete: "
        f"renamed_files={report.renamed_files}, "
        f"updated_links={report.updated_links}, "
        f"updated_files={report.updated_files}, "
        f"dry_run={args.dry_run}"
    )


if __name__ == "__main__":
    main()
