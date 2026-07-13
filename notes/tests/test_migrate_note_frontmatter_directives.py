from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_TOOLS_DIR = REPO_ROOT / "notes"

if str(NOTES_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_TOOLS_DIR))

import migrate_note_frontmatter_directives as migrate_notes  # noqa: E402


def test_run_migration_moves_grouping_line_and_deletes_todo_block(
    tmp_path: Path,
) -> None:
    note = tmp_path / "index.md"
    original = (
        "---\n"
        "title: Index\n"
        "---\n"
        "#share\n"
        "Grouping approach: group by topic\n"
        "\n"
        "+++++\n"
        "Top 2 todos:\n"
        "- stale task\n"
        "+++++\n"
        "Body text\n"
    )
    note.write_text(original, encoding="utf-8")

    dry_run_report = migrate_notes.run_migration(tmp_path, apply=False)

    assert note.read_text(encoding="utf-8") == original
    assert dry_run_report["files_changed"] == 1
    assert dry_run_report["grouping_migrations"] == 1
    assert dry_run_report["todo_blocks_deleted"] == 1

    apply_report = migrate_notes.run_migration(tmp_path, apply=True)
    updated = note.read_text(encoding="utf-8")

    assert apply_report["marker_counts"] == {"#ghp": 0, "#hbp": 0, "#share": 1}
    assert migrate_notes.read_frontmatter_field(updated, "title") == "Index"
    assert migrate_notes.read_frontmatter_field(updated, "grouping") == "group by topic"
    assert "#share\nBody text\n" == migrate_notes.frontmatter_body(updated)
    assert "Grouping approach:" not in updated
    assert "+++++" not in updated


def test_migrate_note_moves_grouping_block_into_new_frontmatter(
    tmp_path: Path,
) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        "<!-- GROUPING APPROACH START -->\n"
        "Group by problem\n"
        "---\n"
        "Then by solution\n"
        "<!-- GROUPING APPROACH END -->\n"
        "\n"
        "# Body\n",
        encoding="utf-8",
    )

    migration = migrate_notes.migrate_note(note)

    assert migrate_notes.read_frontmatter_field(
        migration.updated_content,
        "grouping",
    ) == "Group by problem\n---\nThen by solution"
    assert migrate_notes.frontmatter_body(migration.updated_content) == "# Body\n"
