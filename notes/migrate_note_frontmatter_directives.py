from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


NOTES_ROOT = Path.home() / "notes"
GROUPING_FIELD = "grouping"
GROUPING_PREFIX = "Grouping approach:"
GROUPING_BLOCK_START = "<!-- GROUPING APPROACH START -->"
GROUPING_BLOCK_END = "<!-- GROUPING APPROACH END -->"
TODO_DELIMITER = "+++++"
TODO_BLOCK_HEADING = "Top 2 todos:"
MARKER_PATTERNS = {
    "#share": re.compile(r"(?<![\w-])#share(?![\w-])"),
    "#ghp": re.compile(r"(?<![\w-])#ghp(?![\w-])"),
    "#hbp": re.compile(r"(?<![\w-])#hbp(?![\w-])"),
}


@dataclass(frozen=True)
class NoteMigration:
    path: Path
    original_content: str
    updated_content: str
    grouping: str | None
    deleted_todo_blocks: int
    expected_body: str

    @property
    def changed(self) -> bool:
        return self.original_content != self.updated_content


def file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def markdown_paths(notes_root: Path) -> list[Path]:
    return sorted(
        path
        for path in notes_root.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(notes_root).parts)
    )


def split_frontmatter(content: str) -> tuple[list[str], str] | None:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            body = "\n".join(content.splitlines()[index + 1 :])
            if content.endswith("\n"):
                body += "\n"
            return lines[1:index], body
    raise ValueError("Frontmatter starts with '---' but has no closing delimiter.")


def frontmatter_body(content: str) -> str:
    frontmatter_parts = split_frontmatter(content)
    if frontmatter_parts is None:
        return content
    _, body = frontmatter_parts
    return body


def _is_top_level_field(line: str) -> bool:
    return bool(line.strip()) and not line.startswith((" ", "\t", "-")) and ":" in line


def _skip_frontmatter_field(lines: list[str], start_index: int) -> int:
    value = lines[start_index].split(":", 1)[1].strip()
    next_index = start_index + 1
    if value in {"|", "|-", "|+", ">", ">-", ">+"}:
        while next_index < len(lines) and not _is_top_level_field(lines[next_index]):
            next_index += 1
    return next_index


def read_frontmatter_field(content: str, field_name: str) -> str | None:
    frontmatter_parts = split_frontmatter(content)
    if frontmatter_parts is None:
        return None

    metadata_lines, _ = frontmatter_parts
    field_prefix = f"{field_name}:"
    for index, line in enumerate(metadata_lines):
        if not line.startswith(field_prefix):
            continue
        value = line.split(":", 1)[1].strip()
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block_lines = []
            for block_line in metadata_lines[index + 1 :]:
                if _is_top_level_field(block_line):
                    break
                block_lines.append(
                    block_line[2:] if block_line.startswith("  ") else block_line
                )
            return "\n".join(block_lines).strip("\n")
        return value.strip("\"'")
    return None


def _without_frontmatter_field(lines: list[str], field_name: str) -> list[str]:
    field_prefix = f"{field_name}:"
    filtered = []
    index = 0
    while index < len(lines):
        if lines[index].startswith(field_prefix):
            index = _skip_frontmatter_field(lines, index)
            continue
        filtered.append(lines[index])
        index += 1
    return filtered


def render_block_field(field_name: str, value: str) -> list[str]:
    stripped_value = value.strip()
    if not stripped_value:
        raise ValueError(f"{field_name} cannot be empty.")
    return [f"{field_name}: |"] + [
        f"  {line}" if line else "" for line in stripped_value.splitlines()
    ]


def set_frontmatter_block_field(content: str, field_name: str, value: str) -> str:
    frontmatter_parts = split_frontmatter(content)
    if frontmatter_parts is None:
        metadata_lines = []
        body = content
    else:
        metadata_lines, body = frontmatter_parts

    existing_value = read_frontmatter_field(content, field_name)
    if existing_value is not None and existing_value.strip() != value.strip():
        raise RuntimeError(
            f"Existing {field_name} frontmatter conflicts with migrated value."
        )

    metadata_lines = _without_frontmatter_field(metadata_lines, field_name)
    if metadata_lines and metadata_lines[-1].strip():
        metadata_lines.append("")
    metadata_lines.extend(render_block_field(field_name, value))
    return f"---\n{'\n'.join(metadata_lines).rstrip()}\n---\n{body}"


def frontmatter_end_line_index(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            return index + 1
    raise ValueError("Frontmatter starts with '---' but has no closing delimiter.")


def remove_obsolete_todo_blocks(content: str, path: Path) -> tuple[str, int]:
    lines = content.splitlines(keepends=True)
    updated_lines = []
    deleted_blocks = 0
    index = 0
    while index < len(lines):
        if lines[index].strip() != TODO_DELIMITER:
            updated_lines.append(lines[index])
            index += 1
            continue

        end_index = index + 1
        while end_index < len(lines) and lines[end_index].strip() != TODO_DELIMITER:
            end_index += 1
        if end_index >= len(lines):
            raise RuntimeError(f"{path} has an unterminated {TODO_DELIMITER} block.")

        inner_lines = lines[index + 1 : end_index]
        first_content_line = next(
            (line.strip() for line in inner_lines if line.strip()),
            "",
        )
        if first_content_line != TODO_BLOCK_HEADING:
            raise RuntimeError(
                f"{path} has a {TODO_DELIMITER} block that is not an obsolete todo block."
            )

        deleted_blocks += 1
        index = end_index + 1

    return "".join(updated_lines), deleted_blocks


def remove_legacy_grouping(content: str, path: Path) -> tuple[str, str | None]:
    lines = content.splitlines(keepends=True)
    scan_start = frontmatter_end_line_index([line.rstrip("\n\r") for line in lines])
    grouping_values: list[str] = []
    updated_lines = lines[:scan_start]
    index = scan_start

    while index < len(lines):
        stripped_line = lines[index].strip()
        if stripped_line == GROUPING_BLOCK_START:
            end_index = index + 1
            while (
                end_index < len(lines)
                and lines[end_index].strip() != GROUPING_BLOCK_END
            ):
                end_index += 1
            if end_index >= len(lines):
                raise RuntimeError(f"{path} has an unterminated grouping block.")
            grouping = "".join(lines[index + 1 : end_index]).strip("\n")
            if not grouping.strip():
                raise RuntimeError(f"{path} has an empty grouping block.")
            grouping_values.append(grouping)
            index = end_index + 1
            if index < len(lines) and not lines[index].strip():
                index += 1
            continue

        if stripped_line.lower().startswith(GROUPING_PREFIX.lower()):
            grouping = lines[index].lstrip()[len(GROUPING_PREFIX) :].strip()
            if not grouping:
                raise RuntimeError(f"{path} has an empty grouping line.")
            grouping_values.append(grouping)
            index += 1
            if index < len(lines) and not lines[index].strip():
                index += 1
            continue

        updated_lines.append(lines[index])
        index += 1

    if len(grouping_values) > 1:
        raise RuntimeError(f"{path} has multiple legacy grouping directives.")
    return "".join(updated_lines), grouping_values[0] if grouping_values else None


def migrate_note(path: Path) -> NoteMigration:
    original_content = path.read_text(encoding="utf-8")
    without_todos, deleted_todo_blocks = remove_obsolete_todo_blocks(
        original_content,
        path,
    )
    without_legacy_grouping, grouping = remove_legacy_grouping(without_todos, path)
    updated_content = without_legacy_grouping
    if grouping is not None:
        updated_content = set_frontmatter_block_field(
            without_legacy_grouping,
            GROUPING_FIELD,
            grouping,
        )

    migration = NoteMigration(
        path=path,
        original_content=original_content,
        updated_content=updated_content,
        grouping=grouping,
        deleted_todo_blocks=deleted_todo_blocks,
        expected_body=frontmatter_body(without_legacy_grouping),
    )
    verify_body_preserved(migration)
    return migration


def marker_counts(contents: list[str]) -> dict[str, int]:
    return {
        marker: sum(len(pattern.findall(content)) for content in contents)
        for marker, pattern in MARKER_PATTERNS.items()
    }


def verify_body_preserved(migration: NoteMigration) -> None:
    actual_body = frontmatter_body(migration.updated_content)
    if actual_body != migration.expected_body:
        raise RuntimeError(
            f"{migration.path} body changed outside intended grouping/todo removals."
        )


def verify_migrations(
    migrations: list[NoteMigration],
    before_marker_counts: dict[str, int],
    after_contents: list[str],
) -> None:
    after_marker_counts = marker_counts(after_contents)
    if after_marker_counts != before_marker_counts:
        raise RuntimeError(
            f"Hash marker counts changed: before={before_marker_counts}, after={after_marker_counts}"
        )

    for migration in migrations:
        if not migration.changed:
            continue
        verify_body_preserved(migration)
        if migration.grouping is not None:
            frontmatter_grouping = read_frontmatter_field(
                migration.updated_content,
                GROUPING_FIELD,
            )
            if frontmatter_grouping != migration.grouping:
                raise RuntimeError(f"{migration.path} grouping frontmatter mismatch.")

    legacy_patterns = [
        GROUPING_PREFIX,
        GROUPING_BLOCK_START,
        GROUPING_BLOCK_END,
        TODO_DELIMITER,
    ]
    for migration in migrations:
        for pattern in legacy_patterns:
            if pattern in migration.updated_content:
                raise RuntimeError(
                    f"{migration.path} still contains legacy marker {pattern!r}."
                )


def migration_report(notes_root: Path, migrations: list[NoteMigration]) -> dict:
    changed_migrations = [migration for migration in migrations if migration.changed]
    return {
        "notes_root": str(notes_root),
        "files_changed": len(changed_migrations),
        "grouping_migrations": sum(
            1 for migration in changed_migrations if migration.grouping is not None
        ),
        "todo_blocks_deleted": sum(
            migration.deleted_todo_blocks for migration in changed_migrations
        ),
        "changed_files": [
            {
                "path": str(migration.path.relative_to(notes_root)),
                "old_hash": file_hash(migration.original_content),
                "new_hash": file_hash(migration.updated_content),
                "migrated_grouping": migration.grouping is not None,
                "deleted_todo_blocks": migration.deleted_todo_blocks,
            }
            for migration in changed_migrations
        ],
    }


def run_migration(notes_root: Path, apply: bool) -> dict:
    if not notes_root.is_dir():
        raise NotADirectoryError(f"Notes root is not a directory: {notes_root}")

    paths = markdown_paths(notes_root)
    original_contents = [
        path.read_text(encoding="utf-8")
        for path in paths
    ]
    before_marker_counts = marker_counts(original_contents)
    migrations = [migrate_note(path) for path in paths]
    after_contents = [
        migration.updated_content for migration in migrations
    ]
    verify_migrations(migrations, before_marker_counts, after_contents)

    if apply:
        for migration in migrations:
            if migration.changed:
                migration.path.write_text(migration.updated_content, encoding="utf-8")

        written_contents = [
            path.read_text(encoding="utf-8")
            for path in paths
        ]
        verify_migrations(migrations, before_marker_counts, written_contents)

    report = migration_report(notes_root, migrations)
    report["applied"] = apply
    report["marker_counts"] = before_marker_counts
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate legacy note directives to frontmatter."
    )
    parser.add_argument("--notes-root", default=str(NOTES_ROOT))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run_migration(Path(args.notes_root).expanduser().resolve(), args.apply)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
