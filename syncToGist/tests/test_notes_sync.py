from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import notesSync  # noqa: E402
import teleportWikilinks  # noqa: E402


NOTES_FOLDER = Path(notesSync.getConfig()["notesFolder"])


def test_strip_share_token_removes_only_marker_token() -> None:
    content = "\n".join(
        [
            "#share",
            "keep this #share line",
            "leave #shared alone",
            "- #share keep bullet text",
        ]
    )

    assert notesSync.strip_share_token(content) == "\n".join(
        [
            "keep this line",
            "leave #shared alone",
            "- keep bullet text",
        ]
    )


def test_get_all_index_notes_finds_share_token_anywhere_in_body(
    tmp_path: Path,
) -> None:
    shared_note = tmp_path / "shared.md"
    shared_note.write_text(
        "---\ngist_url: https://gist.github.com/old\n---\n"
        "Intro #share details\n[[Linked Note]]\n",
        encoding="utf-8",
    )
    unshared_note = tmp_path / "unshared.md"
    unshared_note.write_text("#shared is not the marker\n", encoding="utf-8")

    index_notes = notesSync.getAllIndexNotes(str(tmp_path))

    assert index_notes == {
        "shared.md": {
            "file_path": str(shared_note),
            "gist_link": "https://gist.github.com/old",
            "text": "Intro details\n[[Linked Note]]",
        }
    }


def test_get_all_index_notes_skips_unshared_note_with_invalid_frontmatter(
    tmp_path: Path,
) -> None:
    malformed_unshared_note = tmp_path / "inbox-index.md"
    malformed_unshared_note.write_text(
        "---\n"
        "gist_url: https://gist.github.com/585bcb3220e47d9d3456c4fdb8070c0b live: true\n"
        "---\n\n"
        "#index\n",
        encoding="utf-8",
    )
    shared_note = tmp_path / "shared.md"
    shared_note.write_text("#share\n", encoding="utf-8")

    assert "shared.md" in notesSync.getAllIndexNotes(str(tmp_path))


def test_get_all_notes_linked_from_index_notes_strips_linked_note_share_token(
    tmp_path: Path,
) -> None:
    linked_note = tmp_path / "linked-note.md"
    linked_note.write_text("Linked #share content\n", encoding="utf-8")
    index_notes = {
        "index.md": {
            "file_path": str(tmp_path / "index.md"),
            "gist_link": None,
            "text": "[[Linked Note]]",
        }
    }

    linked_notes = notesSync.getAllNotesLinkedFromIndexNotes(
        index_notes,
        str(tmp_path),
    )

    assert linked_notes["linked-note.md"]["text"] == "Linked content"


def test_real_notes_gist_scan_ignores_subdirectories() -> None:
    index_notes = notesSync.getAllIndexNotes(str(NOTES_FOLDER))

    assert index_notes
    assert all(
        Path(info["file_path"]).parent == NOTES_FOLDER
        for info in index_notes.values()
    )


def test_real_notes_teleport_lookup_ignores_subdirectories() -> None:
    markdown_paths = teleportWikilinks.get_all_markdown_files(str(NOTES_FOLDER))

    assert markdown_paths
    assert all(Path(path).parent == NOTES_FOLDER for path in markdown_paths)
    assert teleportWikilinks.find_file_by_name(str(NOTES_FOLDER), "SKILL.md") is None
