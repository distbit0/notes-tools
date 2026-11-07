from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
import openai
import pytest

import keep_auth
from processed_hashes import ProcessedHashes
import pullTempNotes

KEEP_QUIET_PERIOD_SECONDS = 300
# Timing of the November 7 note that was saved mid-sentence.
TRUNCATED_NOTE_CREATED_AT = datetime.fromisoformat("2026-11-07T09:08:00.903+00:00")
TRUNCATED_NOTE_SNAPSHOT_AT = datetime.fromisoformat("2026-11-07T09:09:46.394+00:00")


@pytest.fixture(autouse=True)
def isolated_script_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pullTempNotes,
        "load_config",
        lambda: {"keepNoteQuietPeriodSeconds": KEEP_QUIET_PERIOD_SECONDS},
    )
    monkeypatch.setattr(
        pullTempNotes,
        "KEEP_CAPTURE_LEDGER_FILE",
        str(tmp_path / "keep_capture_ledger.json"),
    )
    monkeypatch.setattr(
        pullTempNotes, "AUDIO_TRANSCRIPTS_DIR", str(tmp_path / "audio_transcripts")
    )
    monkeypatch.setattr(
        pullTempNotes,
        "processed_hashes",
        ProcessedHashes(str(tmp_path / "audio_hashes.json")),
    )


@pytest.fixture
def notes_dir(tmp_path: Path) -> Path:
    for file_name in ("temp.md", "writing.md", "friends.md"):
        (tmp_path / file_name).write_text("# Notes\n")
    return tmp_path


def run_keep_sync(keep: "FakeKeep", notes_dir: Path) -> None:
    pullTempNotes.sync_keep_notes(
        keep,
        str(notes_dir / "temp.md"),
        str(notes_dir / "writing.md"),
        str(notes_dir / "friends.md"),
        str(notes_dir / "urls.md"),
    )


def test_opened_url_history_excludes_statement_credentials(tmp_path: Path) -> None:
    # Captured from the September 27 commit failure; only tokens are redacted.
    urls = (Path(__file__).parent / "fixtures/opened_statement_urls.txt").read_text().splitlines()
    history_path = tmp_path / "opened-urls.md"
    pullTempNotes.append_opened_urls(urls, history_path)
    logged_urls = [line.split(" ", 2)[2] for line in history_path.read_text().splitlines()]

    assert len(logged_urls) == len(urls)
    for original, logged in zip(urls, logged_urls):
        original_parts = urlsplit(original)
        logged_parts = urlsplit(logged)
        assert logged_parts._replace(query="") == original_parts._replace(query="")
        assert parse_qsl(logged_parts.query) == [
            (name, value) for name, value in parse_qsl(original_parts.query)
            if name != "token"
        ]
        assert pullTempNotes.url_for_history(logged) == logged


def test_history_preserves_urls_without_credentials() -> None:
    urls = (Path(__file__).parent / "fixtures/opened_statement_urls.txt").read_text().splitlines()
    for url in urls:
        resource_url = urlunsplit(urlsplit(url)._replace(query=""))
        assert pullTempNotes.url_for_history(resource_url) == resource_url


class FakeKeep:
    def __init__(
        self, events: list[str], notes: list["FakeKeepNote"] | None = None
    ) -> None:
        self.events = events
        self.notes = notes or []

    def sync(self) -> None:
        self.events.append("sync")

    def find(self, **kwargs) -> list["FakeKeepNote"]:
        return [note for note in self.notes if not note.trashed]

    def get(self, note_id: str) -> "FakeKeepNote | None":
        return next((note for note in self.notes if note.id == note_id), None)


class FakeTimestamp:
    def __init__(self, seconds: float = 0) -> None:
        self.seconds = seconds

    def timestamp(self) -> float:
        return self.seconds


class FakeTimestamps:
    edited = FakeTimestamp()
    updated = FakeTimestamp()


class FakeKeepNote:
    def __init__(
        self,
        text: str,
        title: str = "",
        *,
        note_id: str | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.text = text
        self.title = title
        self.id = note_id
        self.events = events
        self.timestamps = FakeTimestamps()
        self.trashed = False

    def trash(self) -> None:
        self.trashed = True
        if self.events is not None:
            self.events.append("trash")


def keep_sync_plan(
    *,
    temp_text: str = "",
    writing_text: str = "",
    friends_text: str = "",
    url_actions: list[pullTempNotes.KeepUrlAction] | None = None,
    saved_texts: dict[str, str] | None = None,
) -> pullTempNotes.KeepSyncPlan:
    return pullTempNotes.KeepSyncPlan(
        temp_text=temp_text,
        writing_text=writing_text,
        friends_text=friends_text,
        url_actions=url_actions or [],
        saved_texts=saved_texts or {},
    )


def test_sync_keep_notes_commits_plain_keep_text_before_url_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    plain_keep_note = FakeKeepNote("keep text", note_id="plain", events=events)
    url_note = FakeKeepNote("https://example.com", note_id="url", events=events)
    keep = FakeKeep(events, [plain_keep_note, url_note])
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg, capture_ledger: keep_sync_plan(
            temp_text="\nkeep text",
            url_actions=[
                pullTempNotes.KeepUrlAction(
                    note=url_note,
                    note_title="",
                    raw_text="https://example.com",
                    lineate_urls=["https://example.com"],
                )
            ],
            saved_texts={"plain": "keep text"},
        ),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "run_lineate_for_urls",
        lambda urls, output_dest="browser": events.append("lineate"),
    )

    def fail_append(urls, file_path):
        events.append("append")
        raise RuntimeError("append failed")

    monkeypatch.setattr(pullTempNotes, "append_opened_urls", fail_append)
    original_write_to_file = pullTempNotes.writeToFile

    def track_write(file_path, text):
        if text:
            events.append("write")
        original_write_to_file(file_path, text)

    monkeypatch.setattr(pullTempNotes, "writeToFile", track_write)

    with pytest.raises(RuntimeError, match="append failed"):
        pullTempNotes.sync_keep_notes(
            keep,
            str(temp_notes_path),
            str(writing_notes_path),
            str(tmp_path / "friends.md"),
            str(tmp_path / "urls.md"),
        )

    assert events == ["write", "lineate", "append"]
    assert temp_notes_path.read_text() == "existing\nkeep text\n"
    assert pullTempNotes.load_keep_capture_ledger() == {"plain": "keep text"}


def test_sync_keep_notes_orders_commit_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    note = FakeKeepNote("https://example.com", note_id="keep-note-1", events=events)
    keep = FakeKeep(events, [note])
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg, capture_ledger: keep_sync_plan(
            url_actions=[
                pullTempNotes.KeepUrlAction(
                    note=note,
                    note_title="",
                    raw_text="https://example.com",
                    lineate_urls=["https://example.com"],
                    success_text="\nkeep text",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "run_lineate_for_urls",
        lambda urls, output_dest="browser": events.append("lineate"),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "append_opened_urls",
        lambda urls, file_path: events.append("append"),
    )
    original_write_to_file = pullTempNotes.writeToFile

    def track_write(file_path, text):
        if text:
            events.append("write")
        original_write_to_file(file_path, text)

    monkeypatch.setattr(pullTempNotes, "writeToFile", track_write)

    pullTempNotes.sync_keep_notes(
        keep,
        str(temp_notes_path),
        str(writing_notes_path),
        str(tmp_path / "friends.md"),
        str(tmp_path / "urls.md"),
    )

    assert events == ["lineate", "append", "write", "trash", "sync"]
    assert temp_notes_path.read_text() == "existing\nkeep text\n"


def test_sync_keep_notes_does_not_apply_keep_timeout_to_lineate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    note = FakeKeepNote("https://example.com", note_id="keep-note-1", events=events)
    keep = FakeKeep(events, [note])
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")

    monkeypatch.setattr(keep_auth, "KEEP_NETWORK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg, capture_ledger: keep_sync_plan(
            url_actions=[
                pullTempNotes.KeepUrlAction(
                    note=note,
                    note_title="",
                    raw_text="https://example.com",
                    lineate_urls=["https://example.com"],
                )
            ],
        ),
    )

    def slow_lineate(urls, output_dest="browser"):
        time.sleep(0.1)
        events.append("lineate")

    monkeypatch.setattr(pullTempNotes, "run_lineate_for_urls", slow_lineate)
    monkeypatch.setattr(
        pullTempNotes,
        "append_opened_urls",
        lambda urls, file_path: events.append("append"),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "sync_keep",
        lambda keep_arg: keep_auth.run_with_keep_timeout(
            "Google Keep sync", lambda: events.append("sync")
        ),
    )

    pullTempNotes.sync_keep_notes(
        keep,
        str(temp_notes_path),
        str(writing_notes_path),
        str(tmp_path / "friends.md"),
        str(tmp_path / "urls.md"),
    )

    assert events == ["lineate", "append", "trash", "sync"]


def test_main_commits_processed_mp3s_before_keep_url_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class DummyLock:
        def close(self) -> None:
            events.append("lock_close")

    monkeypatch.setattr(pullTempNotes, "acquire_script_lock", lambda: DummyLock())
    monkeypatch.setattr(
        pullTempNotes, "authenticate_keep", lambda: events.append("auth") or object()
    )
    monkeypatch.setattr(
        pullTempNotes, "delete_duplicate_files", lambda path: events.append("dedupe")
    )
    monkeypatch.setattr(
        pullTempNotes,
        "saveNotesFromMp3s",
        lambda: ("\n\ntranscribed note", {"note.mp3": {"transcription_successful": True}}),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "commit_processed_mp3_batch",
        lambda temp_path, text, processed, mp3_path: events.append("commit_mp3"),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "sync_keep_notes",
        lambda keep, temp_path, writing_path, friends_path, opened_urls_path: (
            events.append("sync_keep_notes")
        ),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "load_config",
        lambda: {
            "tempNotesPath": "/tmp/inbox-index.md",
            "writingNotesPath": "/tmp/writing-ideas-index.md",
            "friendsNotesPath": "/tmp/friends-index.md",
            "mp3CaptureFolder": "/tmp/mp3s",
        },
    )

    pullTempNotes.main()

    assert events == [
        "dedupe",
        "commit_mp3",
        "auth",
        "sync_keep_notes",
        "lock_close",
    ]


def test_sync_keep_notes_writes_raw_text_after_third_lineate_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    note = FakeKeepNote(
        "https://example.com",
        note_id="keep-note-1",
        events=events,
    )
    keep = FakeKeep(events, [note])
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")
    retry_counts_path = tmp_path / "keep_url_retry_counts.json"

    monkeypatch.setattr(
        pullTempNotes,
        "KEEP_URL_RETRY_COUNTS_FILE",
        str(retry_counts_path),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg, capture_ledger: keep_sync_plan(
            url_actions=[
                pullTempNotes.KeepUrlAction(
                    note=note,
                    note_title="",
                    raw_text="https://example.com",
                    lineate_urls=["https://example.com"],
                )
            ],
        ),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "run_lineate_for_urls",
        lambda urls, output_dest="browser": (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["lineate"])
        ),
    )

    pullTempNotes.sync_keep_notes(
        keep,
        str(temp_notes_path),
        str(writing_notes_path),
        str(tmp_path / "friends.md"),
        str(tmp_path / "urls.md"),
    )
    assert temp_notes_path.read_text() == "existing\n"
    assert retry_counts_path.read_text().strip() == '{\n  "keep-note-1": 1\n}'
    assert events == []

    pullTempNotes.sync_keep_notes(
        keep,
        str(temp_notes_path),
        str(writing_notes_path),
        str(tmp_path / "friends.md"),
        str(tmp_path / "urls.md"),
    )
    assert temp_notes_path.read_text() == "existing\n"
    assert retry_counts_path.read_text().strip() == '{\n  "keep-note-1": 2\n}'
    assert events == []

    pullTempNotes.sync_keep_notes(
        keep,
        str(temp_notes_path),
        str(writing_notes_path),
        str(tmp_path / "friends.md"),
        str(tmp_path / "urls.md"),
    )
    assert temp_notes_path.read_text() == "existing\n\nhttps://example.com\n"
    assert retry_counts_path.read_text().strip() == "{}"
    assert events == ["trash", "sync"]


def test_sync_keep_notes_saves_new_note_at_once_and_only_once(
    monkeypatch: pytest.MonkeyPatch, notes_dir: Path
) -> None:
    events: list[str] = []
    note = FakeKeepNote("draft one", note_id="keep-note-1", events=events)
    note.timestamps.updated = FakeTimestamp(TRUNCATED_NOTE_CREATED_AT.timestamp())
    keep = FakeKeep(events, [note])
    monkeypatch.setattr(
        pullTempNotes.time, "time", TRUNCATED_NOTE_SNAPSHOT_AT.timestamp
    )

    temp_notes_path = notes_dir / "temp.md"
    run_keep_sync(keep, notes_dir)
    assert temp_notes_path.read_text() == "# Notes\n\ndraft one\n"

    # Triaging the note out of the file must not bring it back on the next run.
    temp_notes_path.write_text("# Notes\n")
    run_keep_sync(keep, notes_dir)

    assert temp_notes_path.read_text() == "# Notes\n"
    assert events == []


# Keep kept the edit and the trash in the first case, and a client that had not
# seen the trash untrashed the note in the second. Both were observed on real notes.
@pytest.mark.parametrize(
    "note_is_trashed", [True, False], ids=["still_trashed", "untrashed_by_edit"]
)
def test_sync_keep_notes_replaces_saved_text_with_later_edit_in_place(
    notes_dir: Path, note_is_trashed: bool
) -> None:
    note = FakeKeepNote("draft one", note_id="keep-note-1")
    keep = FakeKeep([], [note])
    run_keep_sync(keep, notes_dir)
    temp_notes_path = notes_dir / "temp.md"
    temp_notes_path.write_text(temp_notes_path.read_text() + "\nlater note\n")

    note.trashed = note_is_trashed
    note.text = "edit from client that knows it is trashed"
    run_keep_sync(keep, notes_dir)

    assert temp_notes_path.read_text() == (
        "# Notes\n\nedit from client that knows it is trashed\n\nlater note\n"
    )
    assert pullTempNotes.load_keep_capture_ledger() == {
        "keep-note-1": "edit from client that knows it is trashed"
    }


def test_sync_keep_notes_appends_edit_when_saved_text_has_left_the_file(
    notes_dir: Path,
) -> None:
    note = FakeKeepNote("draft one", note_id="keep-note-1")
    keep = FakeKeep([], [note])
    run_keep_sync(keep, notes_dir)
    temp_notes_path = notes_dir / "temp.md"
    temp_notes_path.write_text("# Notes\n\ntriaged\n")

    note.text = "edit landed before trash"
    run_keep_sync(keep, notes_dir)

    assert temp_notes_path.read_text() == (
        "# Notes\n\ntriaged\n\nedit landed before trash\n"
    )


def test_replace_saved_keep_text_only_matches_whole_lines(tmp_path: Path) -> None:
    notes_path = tmp_path / "temp.md"
    notes_path.write_text("a draft one follows\ndraft one\nsecond line\n")

    was_replaced = pullTempNotes.replace_saved_keep_text(
        notes_path, "draft one\nsecond line", "edit landed before trash"
    )

    assert was_replaced
    assert notes_path.read_text() == "a draft one follows\nedit landed before trash\n"
    assert not pullTempNotes.replace_saved_keep_text(notes_path, "draft", "unused")


def test_sync_keep_notes_keeps_saved_text_when_note_is_blanked(notes_dir: Path) -> None:
    note = FakeKeepNote("draft one", note_id="keep-note-1")
    keep = FakeKeep([], [note])
    run_keep_sync(keep, notes_dir)

    note.text = ""
    run_keep_sync(keep, notes_dir)

    assert (notes_dir / "temp.md").read_text() == "# Notes\n\ndraft one\n"


def test_sync_keep_notes_forgets_note_keep_has_purged(notes_dir: Path) -> None:
    pullTempNotes.save_keep_capture_ledger({"purged-note": "draft one"})

    run_keep_sync(FakeKeep([]), notes_dir)

    assert pullTempNotes.load_keep_capture_ledger() == {}
    assert (notes_dir / "temp.md").read_text() == "# Notes\n"


def test_sync_keep_notes_appends_url_note_edit_without_rerunning_lineate(
    monkeypatch: pytest.MonkeyPatch, notes_dir: Path
) -> None:
    lineate_calls: list[list[str]] = []
    note = FakeKeepNote("https://example.com", note_id="keep-note-1")
    keep = FakeKeep([], [note])
    monkeypatch.setattr(
        pullTempNotes,
        "run_lineate_for_urls",
        lambda urls, output_dest="browser": lineate_calls.append(urls),
    )
    monkeypatch.setattr(pullTempNotes, "append_opened_urls", lambda urls, path: None)

    run_keep_sync(keep, notes_dir)
    note.text = "https://example.com worth reading"
    run_keep_sync(keep, notes_dir)

    assert lineate_calls == [["https://example.com"]]
    assert (notes_dir / "temp.md").read_text() == (
        "# Notes\n\nhttps://example.com worth reading\n"
    )


@pytest.mark.parametrize("note_text", ["draft one", ""], ids=["saved", "blank"])
@pytest.mark.parametrize(
    ("seconds_since_update", "should_trash"),
    [(KEEP_QUIET_PERIOD_SECONDS - 1, False), (KEEP_QUIET_PERIOD_SECONDS, True)],
    ids=["active", "quiet"],
)
def test_sync_keep_notes_trashes_notes_only_after_quiet_period(
    monkeypatch: pytest.MonkeyPatch,
    notes_dir: Path,
    note_text: str,
    seconds_since_update: int,
    should_trash: bool,
) -> None:
    note = FakeKeepNote(note_text, note_id="keep-note-1")
    last_update = TRUNCATED_NOTE_CREATED_AT.timestamp()
    note.timestamps.updated = FakeTimestamp(last_update)
    monkeypatch.setattr(
        pullTempNotes.time, "time", lambda: last_update + seconds_since_update
    )

    run_keep_sync(FakeKeep([], [note]), notes_dir)

    assert note.trashed is should_trash


def test_build_keep_sync_plan_routes_ii_url_only_notes_to_infolio() -> None:
    note = FakeKeepNote("ii https://example.com\nhttps://slack.com/example")
    keep = type("Keep", (), {"find": lambda self, **kwargs: [note]})()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep, {})

    assert keep_plan.temp_text == ""
    assert keep_plan.writing_text == ""
    assert keep_plan.friends_text == ""
    assert len(keep_plan.url_actions) == 1
    assert keep_plan.url_actions[0].lineate_urls == [
        "https://example.com",
        "https://slack.com/example",
    ]
    assert keep_plan.url_actions[0].output_dest == "infolio"
    assert keep_plan.saved_texts == {}


def test_build_keep_sync_plan_keeps_mixed_ii_notes_in_markdown() -> None:
    note = FakeKeepNote("ii remember https://example.com", note_id="keep-note-1")
    keep = type("Keep", (), {"find": lambda self, **kwargs: [note]})()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep, {})

    assert "ii remember https://example.com" in keep_plan.temp_text
    assert keep_plan.writing_text == ""
    assert keep_plan.friends_text == ""
    assert keep_plan.url_actions == []
    assert keep_plan.saved_texts == {"keep-note-1": "ii remember https://example.com"}


def test_build_keep_sync_plan_routes_ff_prefix_to_friends_index() -> None:
    title_note = FakeKeepNote("body detail", "ff friend note", note_id="title")
    body_note = FakeKeepNote(
        "ff remember this\nwith detail", "Context", note_id="body"
    )
    keep = type(
        "Keep",
        (),
        {"find": lambda self, **kwargs: [title_note, body_note]},
    )()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep, {})

    assert keep_plan.temp_text == ""
    assert keep_plan.writing_text == ""
    assert keep_plan.friends_text == (
        "\n\nfriend note\nbody detail\n\nContext\nremember this\nwith detail"
    )
    assert keep_plan.url_actions == []
    assert keep_plan.saved_texts == {
        "title": "friend note\nbody detail",
        "body": "Context\nremember this\nwith detail",
    }


def test_build_keep_sync_plan_skips_text_fragment_url_lines() -> None:
    note = FakeKeepNote(
        "https://example.com/page#:~:text=skip%20me\nkeep this line\nhttp://ok.example",
        note_id="keep-note-1",
    )
    keep = type("Keep", (), {"find": lambda self, **kwargs: [note]})()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep, {})

    assert "#:~:text=" not in keep_plan.temp_text
    assert "keep this line" in keep_plan.temp_text
    assert "http://ok.example" in keep_plan.temp_text
    assert keep_plan.writing_text == ""
    assert keep_plan.url_actions == []
    assert keep_plan.saved_texts == {"keep-note-1": "keep this line\nhttp://ok.example"}


def test_sync_keep_notes_sends_infolio_actions_to_lineate_without_opened_url_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    note = FakeKeepNote("ii https://example.com", note_id="keep-note-1")
    keep = FakeKeep([], [note])
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg, capture_ledger: keep_sync_plan(
            url_actions=[
                pullTempNotes.KeepUrlAction(
                    note=note,
                    note_title="",
                    raw_text="ii https://example.com",
                    lineate_urls=["https://example.com"],
                    output_dest="infolio",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "run_lineate_for_urls",
        lambda urls, output_dest="browser": events.append(
            ("lineate", (urls, output_dest))
        ),
    )
    monkeypatch.setattr(
        pullTempNotes,
        "append_opened_urls",
        lambda urls, file_path: events.append(("append", urls)),
    )

    pullTempNotes.sync_keep_notes(
        keep,
        str(temp_notes_path),
        str(writing_notes_path),
        str(tmp_path / "friends.md"),
        str(tmp_path / "urls.md"),
    )

    assert events == [("lineate", (["https://example.com"], "infolio"))]


def test_run_lineate_for_urls_passes_output_dest_to_lineate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bool, str]] = []

    def track_subprocess_run(command, *, check, env):
        calls.append((command, check, env["DISPLAY"]))

    monkeypatch.setattr(pullTempNotes.subprocess, "run", track_subprocess_run)

    pullTempNotes.run_lineate_for_urls(["https://example.com"], "infolio")

    assert calls == [
        (
            [
                str(Path.home() / "dev/lineate/run.sh"),
                "--force-convert-all",
                "--summarise",
                "--output-dest",
                "infolio",
                "https://example.com",
            ],
            True,
            ":0",
        )
    ]


def test_build_keep_sync_plan_routes_qq_prefix_to_writing_index() -> None:
    title_note = FakeKeepNote("body detail", "qq title question", note_id="title")
    body_note = FakeKeepNote(
        "qq should this be drafted?\nwith detail", "Context", note_id="body"
    )
    url_note = FakeKeepNote("qq https://example.com", note_id="url")
    keep = type(
        "Keep",
        (),
        {"find": lambda self, **kwargs: [title_note, body_note, url_note]},
    )()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep, {})

    assert keep_plan.temp_text == ""
    assert keep_plan.writing_text == (
        "\n\ntitle question\nbody detail\n\n"
        "Context\nshould this be drafted?\nwith detail\n\n"
        "https://example.com"
    )
    assert keep_plan.friends_text == ""
    assert keep_plan.url_actions == []
    assert keep_plan.saved_texts == {
        "title": "title question\nbody detail",
        "body": "Context\nshould this be drafted?\nwith detail",
        "url": "https://example.com",
    }


def test_build_keep_sync_plan_does_not_route_old_q_prefix_to_writing_index() -> None:
    note = FakeKeepNote("q: keep this in temp", note_id="keep-note-1")
    keep = type("Keep", (), {"find": lambda self, **kwargs: [note]})()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep, {})

    assert "q: keep this in temp" in keep_plan.temp_text
    assert keep_plan.writing_text == ""
    assert keep_plan.friends_text == ""
    assert keep_plan.url_actions == []
    assert keep_plan.saved_texts == {"keep-note-1": "q: keep this in temp"}


def test_sync_keep_notes_commits_question_notes_to_writing_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    note = FakeKeepNote("https://example.com", note_id="keep-note-1", events=events)
    keep = FakeKeep(events, [note])
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    friends_notes_path = tmp_path / "friends.md"
    temp_notes_path.write_text("existing temp\n")
    writing_notes_path.write_text("# Writing\n")
    friends_notes_path.write_text("# Friends\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg, capture_ledger: keep_sync_plan(
            writing_text="\n\nquestion note",
            saved_texts={"keep-note-1": "question note"},
        ),
    )

    pullTempNotes.sync_keep_notes(
        keep,
        str(temp_notes_path),
        str(writing_notes_path),
        str(friends_notes_path),
        str(tmp_path / "urls.md"),
    )

    assert temp_notes_path.read_text() == "existing temp\n"
    assert writing_notes_path.read_text() == "# Writing\n\nquestion note\n"
    assert friends_notes_path.read_text() == "# Friends\n"
    assert events == ["trash", "sync"]


def test_sync_keep_notes_commits_friend_notes_to_friends_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    note = FakeKeepNote("https://example.com", note_id="keep-note-1", events=events)
    keep = FakeKeep(events, [note])
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    friends_notes_path = tmp_path / "friends.md"
    temp_notes_path.write_text("existing temp\n")
    writing_notes_path.write_text("# Writing\n")
    friends_notes_path.write_text("# Friends\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg, capture_ledger: keep_sync_plan(
            friends_text="\n\nfriend note",
            saved_texts={"keep-note-1": "friend note"},
        ),
    )

    pullTempNotes.sync_keep_notes(
        keep,
        str(temp_notes_path),
        str(writing_notes_path),
        str(friends_notes_path),
        str(tmp_path / "urls.md"),
    )

    assert temp_notes_path.read_text() == "existing temp\n"
    assert writing_notes_path.read_text() == "# Writing\n"
    assert friends_notes_path.read_text() == "# Friends\n\nfriend note\n"
    assert events == ["trash", "sync"]


def test_acquire_script_lock_exits_when_another_run_is_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pullTempNotes, "LOCK_FILE", str(tmp_path / "pullTempNotes.lock"))

    first_lock = pullTempNotes.acquire_script_lock()
    try:
        with pytest.raises(SystemExit) as exit_info:
            pullTempNotes.acquire_script_lock()
    finally:
        first_lock.close()

    assert exit_info.value.code == 0


class FakeTranscriptions:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.failing_chunks: set[str] = set()
        self.requested_chunks: list[str] = []

    def create(self, *, model: str, file, response_format: str) -> str:
        chunk_name = Path(file.name).stem
        self.requested_chunks.append(chunk_name)
        if self.failure:
            raise self.failure
        if chunk_name in self.failing_chunks:
            raise RuntimeError("transcription failed")
        return f"text of {chunk_name}"


@pytest.fixture
def transcriptions(monkeypatch: pytest.MonkeyPatch) -> FakeTranscriptions:
    fake_transcriptions = FakeTranscriptions()
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=fake_transcriptions))
    monkeypatch.setenv("OPENAI_API_KEY", "unused")
    monkeypatch.setattr(pullTempNotes, "OpenAI", lambda api_key: client)
    return fake_transcriptions


def record_audio(path: Path, seconds: int) -> str:
    # Real ffmpeg output, since the recordings themselves are private.
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:duration={seconds}",
            str(path),
        ],
        check=True,
    )
    return str(path)


def audio_seconds(path: Path) -> float:
    return float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )


def test_split_audio_into_chunks_covers_the_whole_recording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pullTempNotes, "TRANSCRIPTION_CHUNK_SECONDS", 2)
    recording = record_audio(tmp_path / "recording.mp3", seconds=5)

    chunk_paths = pullTempNotes.split_audio_into_chunks(recording, tmp_path)

    assert [path.name for path in chunk_paths] == [
        "chunk-00000.mp3",
        "chunk-00001.mp3",
        "chunk-00002.mp3",
    ]
    assert sum(audio_seconds(path) for path in chunk_paths) == pytest.approx(5, abs=0.2)


def test_process_mp3_file_joins_every_chunk_in_recording_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transcriptions: FakeTranscriptions,
) -> None:
    monkeypatch.setattr(pullTempNotes, "TRANSCRIPTION_CHUNK_SECONDS", 2)
    recording = record_audio(tmp_path / "recording.mp3", seconds=5)

    transcript, was_transcribed = pullTempNotes.processMp3File(
        recording, tmp_path / "transcripts"
    )

    assert was_transcribed
    assert transcript == (
        "text of chunk-00000\n\ntext of chunk-00001\n\ntext of chunk-00002"
    )


def test_process_mp3_file_retry_only_requests_unfinished_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transcriptions: FakeTranscriptions,
) -> None:
    monkeypatch.setattr(pullTempNotes, "TRANSCRIPTION_CHUNK_SECONDS", 2)
    recording = record_audio(tmp_path / "recording.mp3", seconds=5)
    transcriptions.failing_chunks = {"chunk-00001"}

    assert pullTempNotes.processMp3File(recording, tmp_path / "transcripts") == (
        "",
        False,
    )

    transcriptions.failing_chunks = set()
    transcriptions.requested_chunks = []
    transcript, was_transcribed = pullTempNotes.processMp3File(
        recording, tmp_path / "transcripts"
    )

    assert was_transcribed
    assert transcriptions.requested_chunks == ["chunk-00001"]
    assert transcript.count("text of chunk-") == 3


def test_process_mp3_file_reports_unreadable_audio_as_failed(
    tmp_path: Path, transcriptions: FakeTranscriptions
) -> None:
    unreadable_recording = tmp_path / "recording.mp3"
    unreadable_recording.write_text("not audio")

    result = pullTempNotes.processMp3File(
        str(unreadable_recording), tmp_path / "transcripts"
    )

    assert result == ("", False)
    assert transcriptions.requested_chunks == []


def test_save_notes_from_mp3s_stops_at_a_rejected_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transcriptions: FakeTranscriptions,
) -> None:
    record_audio(tmp_path / "My recording 1.mp3", seconds=1)
    record_audio(tmp_path / "My recording 2.mp3", seconds=2)
    transcriptions.failure = openai.AuthenticationError(
        "Incorrect API key provided",
        response=httpx.Response(
            401, request=httpx.Request("POST", "https://api.openai.com/v1/")
        ),
        body=None,
    )
    monkeypatch.setattr(
        pullTempNotes,
        "load_config",
        lambda: {"mp3CaptureFolder": str(tmp_path)},
    )

    text_to_add, processed_mp3s = pullTempNotes.saveNotesFromMp3s()

    assert text_to_add == ""
    assert processed_mp3s == {}
    assert len(transcriptions.requested_chunks) == 1


@pytest.mark.parametrize(
    ("max_inbox_chars", "expected_inbox_text"),
    [
        (100, "\n\ntext of chunk-00000"),
        (
            5,
            "\n\nvoice transcript: "
            "[[voice-transcript-2026-09-14-my-recording-224]]",
        ),
    ],
    ids=["short_stays_in_inbox", "long_gets_its_own_note"],
)
def test_save_notes_from_mp3s_keeps_long_transcripts_out_of_the_inbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transcriptions: FakeTranscriptions,
    max_inbox_chars: int,
    expected_inbox_text: str,
) -> None:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    recording = record_audio(capture_dir / "My recording 224.mp3", seconds=1)
    recorded_at = datetime(2026, 9, 14, 12).timestamp()
    os.utime(recording, (recorded_at, recorded_at))
    monkeypatch.setattr(
        pullTempNotes,
        "load_config",
        lambda: {
            "mp3CaptureFolder": str(capture_dir),
            "tempNotesPath": str(tmp_path / "inbox-index.md"),
            "maxInboxTranscriptChars": max_inbox_chars,
        },
    )

    text_to_add, _ = pullTempNotes.saveNotesFromMp3s()

    assert text_to_add == expected_inbox_text
    note_paths = list(tmp_path.glob("voice-transcript-*.md"))
    if max_inbox_chars == 5:
        assert [path.read_text() for path in note_paths] == ["text of chunk-00000\n"]
    else:
        assert note_paths == []


def test_commit_processed_mp3_batch_records_audio_only_after_text_is_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trashed_paths: list[str] = []
    monkeypatch.setattr(pullTempNotes, "send2trash", trashed_paths.append)
    recording = tmp_path / "recording.mp3"
    recording.write_text("audio")
    transcript_dir = Path(pullTempNotes.AUDIO_TRANSCRIPTS_DIR) / "recording-hash"
    transcript_dir.mkdir(parents=True)
    processed_mp3s = {
        "recording.mp3": {
            "raw_text": "text of chunk-00000",
            "formatted_text": "text of chunk-00000",
            "transcription_successful": True,
            "new_hash": "recording-hash",
        }
    }

    def fail_write(file_path, text):
        raise OSError("disk full")

    original_write_to_file = pullTempNotes.writeToFile
    monkeypatch.setattr(pullTempNotes, "writeToFile", fail_write)
    with pytest.raises(OSError):
        pullTempNotes.commit_processed_mp3_batch(
            "inbox.md", "text", processed_mp3s, str(tmp_path)
        )

    assert not pullTempNotes.processed_hashes.is_hash_processed("recording-hash")
    assert transcript_dir.exists()
    assert recording.exists()
    assert trashed_paths == []

    monkeypatch.setattr(pullTempNotes, "writeToFile", original_write_to_file)
    inbox_path = tmp_path / "inbox.md"
    inbox_path.write_text("# Inbox\n")
    pullTempNotes.commit_processed_mp3_batch(
        str(inbox_path), "\n\ntext of chunk-00000", processed_mp3s, str(tmp_path)
    )

    assert pullTempNotes.processed_hashes.is_hash_processed("recording-hash")
    assert not transcript_dir.exists()
    assert len(trashed_paths) == 1
