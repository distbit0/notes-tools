from pathlib import Path
import sys

import pytest

from private_test_data import PRIVATE_TEST_DATA


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import normalize_markdown_filenames as normalizer  # noqa: E402


CONTACT_NAME = PRIVATE_TEST_DATA["discord"]["contactName"]


def test_normalize_notes_renames_files_and_updates_wikilinks(tmp_path: Path) -> None:
    notes_root = tmp_path / "notes"
    notes_root.mkdir()
    (notes_root / "source note.md").write_text(
        "[[target note]] [[target note#Heading]] [[target note|label]]\n"
        "`[[target note]]`\n"
        "```\n[[target note]]\n```\n",
        encoding="utf-8",
    )
    (notes_root / "target note.md").write_text("target\n", encoding="utf-8")

    report = normalizer.normalize_notes(notes_root, dry_run=False)

    assert report.renamed_files == 2
    assert report.updated_files == 1
    assert report.updated_links == 3
    assert not (notes_root / "source note.md").exists()
    assert not (notes_root / "target note.md").exists()
    assert (notes_root / "source-note.md").read_text(encoding="utf-8") == (
        "[[target-note]] [[target-note#Heading]] [[target-note|label]]\n"
        "`[[target note]]`\n"
        "```\n[[target note]]\n```\n"
    )


def test_normalize_notes_rejects_filename_collisions(tmp_path: Path) -> None:
    notes_root = tmp_path / "notes"
    notes_root.mkdir()
    (notes_root / "target note.md").write_text("one\n", encoding="utf-8")
    (notes_root / "target-note.md").write_text("two\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="would overwrite existing file"):
        normalizer.normalize_notes(notes_root, dry_run=False)


def test_normalize_notes_preserves_literal_msg_files_and_links(tmp_path: Path) -> None:
    notes_root = tmp_path / "notes"
    notes_root.mkdir()
    message_note_name = f"msg - Discord - {CONTACT_NAME} - abc12345"
    (notes_root / "source note.md").write_text(
        f"[[{message_note_name}]]\n",
        encoding="utf-8",
    )
    (notes_root / f"{message_note_name}.md").write_text(
        "message note\n",
        encoding="utf-8",
    )

    report = normalizer.normalize_notes(notes_root, dry_run=False)

    assert report.renamed_files == 1
    assert (notes_root / f"{message_note_name}.md").exists()
    assert (notes_root / "source-note.md").read_text(encoding="utf-8") == (
        f"[[{message_note_name}]]\n"
    )
