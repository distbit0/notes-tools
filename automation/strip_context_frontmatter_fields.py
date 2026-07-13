#!/usr/bin/env python3

import argparse
import os
import re
import sys
from pathlib import Path

from loguru import logger


_DEFAULT_ROOT = Path.home() / "notes/context"
_FIELDS_TO_STRIP = ("gist_url", "live")


def strip_yaml_front_matter_fields(
    text: str, fields_to_strip: tuple[str, ...]
) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text, False

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return text, False

    fields_pat = "|".join(re.escape(f) for f in fields_to_strip)
    field_line_re = re.compile(rf"^\s*(?:{fields_pat})\s*:")

    original_fm = lines[1:end_idx]
    new_fm = [line for line in original_fm if not field_line_re.match(line)]
    if new_fm == original_fm:
        return text, False

    new_text = "".join([lines[0], *new_fm, *lines[end_idx:]])
    return new_text, True


def iter_markdown_files(root: Path):
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if filename.endswith(".md"):
                yield Path(dirpath) / filename


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Strip selected YAML front matter fields from markdown files under ~/notes/context."
    )
    parser.add_argument(
        "--root", type=Path, default=_DEFAULT_ROOT, help="Root folder to scan."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change, without editing files.",
    )
    args = parser.parse_args()

    log_dir = Path("~/.local/state/notes-scripts").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.add(
        log_dir / "strip_context_frontmatter_fields.log",
        level="DEBUG",
        rotation="1 MB",
        retention=10,
    )

    root = args.root.expanduser().resolve()
    if not root.exists():
        logger.error(f"Root does not exist: {root}")
        return 2

    changed = 0
    scanned = 0
    for path in iter_markdown_files(root):
        scanned += 1
        original = path.read_text(encoding="utf-8", errors="ignore")
        updated, did_change = strip_yaml_front_matter_fields(original, _FIELDS_TO_STRIP)
        if not did_change:
            continue

        changed += 1
        logger.info(f"Stripping {', '.join(_FIELDS_TO_STRIP)} from: {path}")
        if not args.dry_run:
            path.write_text(updated, encoding="utf-8")

    logger.info(f"Scanned {scanned} markdown files under {root}; updated {changed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
