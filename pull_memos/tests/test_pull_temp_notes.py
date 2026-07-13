from pathlib import Path
import subprocess
import time

import pytest

import keep_auth
import pullTempNotes


class FakeNote:
    def __init__(self, events: list[str], trash_event: str = "trash") -> None:
        self.events = events
        self.trash_event = trash_event

    def trash(self) -> None:
        self.events.append(self.trash_event)


class FakeKeep:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def sync(self) -> None:
        self.events.append("sync")


class FakeTimestamp:
    def timestamp(self) -> int:
        return 0


class FakeTimestamps:
    edited = FakeTimestamp()


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

    def trash(self) -> None:
        if self.events is not None:
            self.events.append("trash")


def keep_sync_plan(
    *,
    temp_text: str = "",
    writing_text: str = "",
    friends_text: str = "",
    url_actions: list[pullTempNotes.KeepUrlAction] | None = None,
    notes_to_trash: list[object] | None = None,
) -> pullTempNotes.KeepSyncPlan:
    return pullTempNotes.KeepSyncPlan(
        temp_text=temp_text,
        writing_text=writing_text,
        friends_text=friends_text,
        url_actions=url_actions or [],
        notes_to_trash=notes_to_trash or [],
    )


def test_sync_keep_notes_commits_plain_keep_text_before_url_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    plain_keep_note = FakeNote(events, "plain_trash")
    url_note = FakeNote(events, "url_trash")
    keep = FakeKeep(events)
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg: keep_sync_plan(
            temp_text="\nkeep text",
            url_actions=[
                pullTempNotes.KeepUrlAction(
                    note=url_note,
                    note_title="",
                    raw_text="https://example.com",
                    lineate_urls=["https://example.com"],
                )
            ],
            notes_to_trash=[plain_keep_note],
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

    assert events == ["write", "plain_trash", "sync", "lineate", "append"]
    assert temp_notes_path.read_text() == "existing\nkeep text\n"


def test_sync_keep_notes_orders_commit_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    note = FakeNote(events)
    keep = FakeKeep(events)
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg: keep_sync_plan(
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
    note = FakeNote(events)
    keep = FakeKeep(events)
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")

    monkeypatch.setattr(keep_auth, "KEEP_NETWORK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg: keep_sync_plan(
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
        "auth",
        "dedupe",
        "commit_mp3",
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
    keep = FakeKeep(events)
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
        lambda keep_arg: keep_sync_plan(
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


def test_build_keep_sync_plan_routes_ii_url_only_notes_to_infolio() -> None:
    note = FakeKeepNote("ii https://example.com\nhttps://slack.com/example")
    keep = type("Keep", (), {"find": lambda self, **kwargs: [note]})()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep)

    assert keep_plan.temp_text == ""
    assert keep_plan.writing_text == ""
    assert keep_plan.friends_text == ""
    assert len(keep_plan.url_actions) == 1
    assert keep_plan.url_actions[0].lineate_urls == [
        "https://example.com",
        "https://slack.com/example",
    ]
    assert keep_plan.url_actions[0].output_dest == "infolio"
    assert keep_plan.notes_to_trash == []


def test_build_keep_sync_plan_keeps_mixed_ii_notes_in_markdown() -> None:
    note = FakeKeepNote("ii remember https://example.com")
    keep = type("Keep", (), {"find": lambda self, **kwargs: [note]})()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep)

    assert "ii remember https://example.com" in keep_plan.temp_text
    assert keep_plan.writing_text == ""
    assert keep_plan.friends_text == ""
    assert keep_plan.url_actions == []
    assert keep_plan.notes_to_trash == [note]


def test_build_keep_sync_plan_routes_ff_prefix_to_friends_index() -> None:
    title_note = FakeKeepNote("body detail", "ff friend note")
    body_note = FakeKeepNote("ff remember this\nwith detail", "Context")
    keep = type(
        "Keep",
        (),
        {"find": lambda self, **kwargs: [title_note, body_note]},
    )()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep)

    assert keep_plan.temp_text == ""
    assert keep_plan.writing_text == ""
    assert keep_plan.friends_text == (
        "\n\nfriend note\nbody detail\n\nContext\nremember this\nwith detail"
    )
    assert keep_plan.url_actions == []
    assert keep_plan.notes_to_trash == [title_note, body_note]


def test_build_keep_sync_plan_skips_text_fragment_url_lines() -> None:
    note = FakeKeepNote(
        "https://example.com/page#:~:text=skip%20me\nkeep this line\nhttp://ok.example"
    )
    keep = type("Keep", (), {"find": lambda self, **kwargs: [note]})()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep)

    assert "#:~:text=" not in keep_plan.temp_text
    assert "keep this line" in keep_plan.temp_text
    assert "http://ok.example" in keep_plan.temp_text
    assert keep_plan.writing_text == ""
    assert keep_plan.url_actions == []
    assert keep_plan.notes_to_trash == [note]


def test_sync_keep_notes_sends_infolio_actions_to_lineate_without_opened_url_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    note = FakeNote([])
    keep = FakeKeep([])
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    temp_notes_path.write_text("existing\n")
    writing_notes_path.write_text("# Writing\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg: keep_sync_plan(
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
    title_note = FakeKeepNote("body detail", "qq title question")
    body_note = FakeKeepNote("qq should this be drafted?\nwith detail", "Context")
    url_note = FakeKeepNote("qq https://example.com")
    keep = type(
        "Keep",
        (),
        {"find": lambda self, **kwargs: [title_note, body_note, url_note]},
    )()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep)

    assert keep_plan.temp_text == ""
    assert keep_plan.writing_text == (
        "\n\ntitle question\nbody detail\n\n"
        "Context\nshould this be drafted?\nwith detail\n\n"
        "https://example.com"
    )
    assert keep_plan.friends_text == ""
    assert keep_plan.url_actions == []
    assert keep_plan.notes_to_trash == [title_note, body_note, url_note]


def test_build_keep_sync_plan_does_not_route_old_q_prefix_to_writing_index() -> None:
    note = FakeKeepNote("q: keep this in temp")
    keep = type("Keep", (), {"find": lambda self, **kwargs: [note]})()

    keep_plan = pullTempNotes.build_keep_sync_plan(keep)

    assert "q: keep this in temp" in keep_plan.temp_text
    assert keep_plan.writing_text == ""
    assert keep_plan.friends_text == ""
    assert keep_plan.url_actions == []
    assert keep_plan.notes_to_trash == [note]


def test_sync_keep_notes_commits_question_notes_to_writing_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    note = FakeNote(events)
    keep = FakeKeep(events)
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    friends_notes_path = tmp_path / "friends.md"
    temp_notes_path.write_text("existing temp\n")
    writing_notes_path.write_text("# Writing\n")
    friends_notes_path.write_text("# Friends\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg: keep_sync_plan(
            writing_text="\n\nquestion note",
            notes_to_trash=[note],
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
    note = FakeNote(events)
    keep = FakeKeep(events)
    temp_notes_path = tmp_path / "temp.md"
    writing_notes_path = tmp_path / "writing.md"
    friends_notes_path = tmp_path / "friends.md"
    temp_notes_path.write_text("existing temp\n")
    writing_notes_path.write_text("# Writing\n")
    friends_notes_path.write_text("# Friends\n")

    monkeypatch.setattr(
        pullTempNotes,
        "build_keep_sync_plan",
        lambda keep_arg: keep_sync_plan(
            friends_text="\n\nfriend note",
            notes_to_trash=[note],
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
