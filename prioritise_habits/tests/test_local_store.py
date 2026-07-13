import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest


PRIVATE_TEST_DATA = json.loads(
    Path(__file__).with_name("private_test_data.json").read_text(encoding="utf-8")
)
NOTIFICATION_HABIT_NAME = PRIVATE_TEST_DATA["notificationHabitName"]
NOTIFICATION_HABIT_TEXT = PRIVATE_TEST_DATA["notificationHabitText"]
REPLY_HABIT_NAME = PRIVATE_TEST_DATA["replyHabitName"]
REPLY_HABIT_TEXT = PRIVATE_TEST_DATA["replyHabitText"]
FOLLOW_UP_HABIT_NAME = PRIVATE_TEST_DATA["followUpHabitName"]
CUSTOM_AUDIO_FILE = PRIVATE_TEST_DATA["customAudioFile"]
CUSTOM_AUDIO_HABIT_ID = PRIVATE_TEST_DATA["customAudioHabitId"]
CUSTOM_AUDIO_HABIT_NAME = PRIVATE_TEST_DATA["customAudioHabitName"]

from src.main import (
    DUE_OUTPUT_TEXT_TO_SPEECH,
    DUE_OUTPUT_WRITE_TO_MD,
    PROJECT_ROOT,
    apply_checkin_payload,
    acquire_run_lock,
    append_ready_habit_triggers,
    build_phone_audio_control_url,
    create_persistent_desktop_notifications,
    get_completed_habits_after_ready_triggers,
    get_bluetooth_address_from_audio_sink_metadata,
    get_bluez_media_transport_paths,
    get_habit_audio_file_path,
    get_habit_due_outputs,
    get_phone_audio_control_trigger_url,
    get_or_create_text_to_speech_audio,
    get_ready_habit_triggers,
    get_ready_triggers_for_due_output,
    is_bluetooth_audio_sink_metadata,
    load_habit_store,
    mark_triggers_output_delivered,
    merge_habit_updates,
    play_audio_file,
    save_habit_store,
    sample_habit_trigger_time,
    speak_ready_habit_triggers,
)


def write_store(tmp_path, store_payload, active_habits=None):
    store_path = tmp_path / "store.json"
    active_habits_path = tmp_path / "active_habits.json"
    store_path.write_text(json.dumps(store_payload))
    active_habits_path.write_text(json.dumps(active_habits or []))
    return store_path, active_habits_path


def test_load_habit_store_accepts_valid_schema(tmp_path):
    store_path, active_habits_path = write_store(
        tmp_path,
        {
            "habits": [{"id": "habit-2", "name": "Archived", "archivedTime": "now"}],
            "checkins": {"habit-1": []},
        },
        [{"id": "habit-1", "name": "Read", "goal": 1}],
    )

    store = load_habit_store(store_path, active_habits_path)

    assert len(store["habits"]) == 2
    assert store["habits"][0]["id"] == "habit-1"
    assert store["habits"][1]["id"] == "habit-2"
    assert store["checkins"]["habit-1"] == []


def test_load_habit_store_rejects_habit_without_id(tmp_path):
    store_path, active_habits_path = write_store(
        tmp_path,
        {
            "habits": [],
            "checkins": {},
        },
        [{"name": "Read"}],
    )

    with pytest.raises(ValueError, match="must include an 'id'"):
        load_habit_store(store_path, active_habits_path)


def test_load_habit_store_rejects_invalid_due_output(tmp_path):
    store_path, active_habits_path = write_store(
        tmp_path,
        {
            "habits": [],
            "checkins": {},
        },
        [
            {
                "id": "habit-1",
                "name": "Read",
                "dueOutputs": {"writeToMd": "yes"},
            }
        ],
    )

    with pytest.raises(ValueError, match="dueOutputs.writeToMd must be a boolean"):
        load_habit_store(store_path, active_habits_path)


def test_load_habit_store_rejects_non_archived_habits_in_store(tmp_path):
    store_path, active_habits_path = write_store(
        tmp_path,
        {
            "habits": [{"id": "habit-1", "name": "Read", "archivedTime": None}],
            "checkins": {},
        },
    )

    with pytest.raises(ValueError, match="move them to active habits file"):
        load_habit_store(store_path, active_habits_path)


def test_save_habit_store_splits_active_and_archived_habits(tmp_path):
    store_path = tmp_path / "store.json"
    active_habits_path = tmp_path / "active_habits.json"

    save_habit_store(
        store_path,
        active_habits_path,
        [
            {"id": "habit-1", "name": "Read", "archivedTime": None, "goal": 1},
            {"id": "habit-2", "name": "Archived", "archivedTime": "now"},
        ],
        {"habit-1": []},
    )

    active_habits = json.loads(active_habits_path.read_text())
    store = json.loads(store_path.read_text())

    assert [habit["id"] for habit in active_habits] == ["habit-1"]
    assert list(active_habits[0])[:4] == ["id", "name", "goal", "archivedTime"]
    assert [habit["id"] for habit in store["habits"]] == ["habit-2"]
    assert store["checkins"] == {"habit-1": []}


def test_habit_due_outputs_default_to_md_and_tts():
    habit = {"id": "habit-1", "name": "Read"}

    assert get_habit_due_outputs(habit) == {
        "writeToMd": True,
        "desktopNotification": False,
        "textToSpeech": True,
    }


def test_ready_triggers_are_filtered_by_due_output():
    ready_triggers = [
        {
            "habit": {
                "id": "habit-1",
                "name": "Read",
                "dueOutputs": {"writeToMd": True, "desktopNotification": False},
            },
            "trigger": {},
        },
        {
            "habit": {
                "id": "habit-2",
                "name": "Reply",
                "dueOutputs": {"writeToMd": False, "desktopNotification": True},
            },
            "trigger": {},
        },
    ]

    md_triggers = get_ready_triggers_for_due_output(ready_triggers, "writeToMd")
    notification_triggers = get_ready_triggers_for_due_output(
        ready_triggers, "desktopNotification"
    )

    assert [item["habit"]["id"] for item in md_triggers] == ["habit-1"]
    assert [item["habit"]["id"] for item in notification_triggers] == ["habit-2"]


def test_apply_checkin_payload_add_and_update():
    checkins = {}
    add_payload = {
        "add": [
            {
                "habitId": "habit-1",
                "checkinStamp": 20260224,
                "goal": 1.0,
                "value": 1.0,
                "status": 2,
                "checkinTime": "2026-02-24T10:00:00.000+0000",
                "opTime": "2026-02-24T10:00:00.000+0000",
            }
        ],
        "update": [],
        "delete": [],
    }

    applied_add_count = apply_checkin_payload(checkins, add_payload)

    assert applied_add_count == 1
    assert "habit-1" in checkins
    assert len(checkins["habit-1"]) == 1
    assert checkins["habit-1"][0]["id"] == "habit-1-20260224"
    assert checkins["habit-1"][0]["status"] == 2

    update_payload = {
        "add": [],
        "update": [
            {
                "habitId": "habit-1",
                "checkinStamp": 20260224,
                "id": "habit-1-20260224",
                "goal": 1.0,
                "value": 1.0,
                "status": 1,
                "checkinTime": "2026-02-24T12:00:00.000+0000",
                "opTime": "2026-02-24T12:00:00.000+0000",
            }
        ],
        "delete": [],
    }

    applied_update_count = apply_checkin_payload(checkins, update_payload)

    assert applied_update_count == 1
    assert len(checkins["habit-1"]) == 1
    assert checkins["habit-1"][0]["status"] == 1


def test_merge_habit_updates_replaces_matching_habit():
    all_habits = [
        {"id": "habit-1", "name": "Read", "sortOrder": 10},
        {"id": "habit-2", "name": "Walk", "sortOrder": 20},
    ]
    updated_habits = [{"id": "habit-2", "name": "1. Walk", "sortOrder": 1}]

    merged_habits = merge_habit_updates(all_habits, updated_habits)

    assert merged_habits[0]["name"] == "Read"
    assert merged_habits[1]["name"] == "1. Walk"
    assert merged_habits[1]["sortOrder"] == 1


def test_merge_habit_updates_rejects_unknown_habit_id():
    all_habits = [{"id": "habit-1", "name": "Read"}]
    updated_habits = [{"id": "habit-x", "name": "Invalid"}]

    with pytest.raises(ValueError, match="Cannot update missing habits"):
        merge_habit_updates(all_habits, updated_habits)


def test_ready_habit_triggers_uses_persisted_daily_schedule(tmp_path):
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "date": "20260410",
                "triggers": {
                    "habit-1": [
                        {
                            "time": "2026-04-10T06:30:00+00:00",
                            "triggered": False,
                        },
                        {
                            "time": "2026-04-10T11:30:00+00:00",
                            "triggered": False,
                        },
                    ]
                },
            }
        )
    )
    due_habits = [
        {
            "id": "habit-1",
            "name": "Sequence",
            "dailyTriggerCount": 2,
        }
    ]

    ready_triggers, schedule = get_ready_habit_triggers(
        due_habits,
        schedule_path,
        datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
    )

    assert len(ready_triggers) == 1
    assert ready_triggers[0]["habit"]["id"] == "habit-1"
    assert schedule["triggers"]["habit-1"][1]["triggered"] is False


def test_legacy_ready_triggers_keep_non_tts_outputs_delivered(tmp_path):
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "date": "20260410",
                "triggers": {
                    "habit-1": [
                        {
                            "time": "2026-04-10T06:30:00+00:00",
                            "triggered": False,
                        },
                        {
                            "time": "2026-04-10T11:30:00+00:00",
                            "triggered": False,
                        },
                    ]
                },
            }
        )
    )
    due_habits = [
        {
            "id": "habit-1",
            "name": "Sequence",
            "dailyTriggerCount": 2,
            "dueOutputs": {
                "writeToMd": True,
                "desktopNotification": True,
                "textToSpeech": True,
            },
        }
    ]

    ready_triggers, schedule = get_ready_habit_triggers(
        due_habits,
        schedule_path,
        datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
    )

    assert len(ready_triggers) == 1
    assert ready_triggers[0]["trigger"]["deliveredOutputs"] == {
        "writeToMd": True,
        "desktopNotification": True,
        "textToSpeech": False,
    }
    assert schedule["triggers"]["habit-1"][1]["deliveredOutputs"] == {
        "writeToMd": False,
        "desktopNotification": False,
        "textToSpeech": False,
    }


def test_delivered_outputs_complete_stale_untriggered_schedule_entry(tmp_path):
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "date": "20260410",
                "triggers": {
                    "habit-1": [
                        {
                            "time": "2026-04-10T06:30:00+00:00",
                            "triggered": False,
                            "deliveredOutputs": {
                                "writeToMd": True,
                                "desktopNotification": False,
                                "textToSpeech": True,
                            },
                        }
                    ]
                },
            }
        )
    )
    due_habits = [
        {
            "id": "habit-1",
            "name": "Read",
            "dueOutputs": {"writeToMd": True, "textToSpeech": True},
        }
    ]

    ready_triggers, schedule = get_ready_habit_triggers(
        due_habits,
        schedule_path,
        datetime(2026, 4, 10, 9, 0, tzinfo=timezone.utc),
    )

    assert ready_triggers == []
    assert schedule["triggers"]["habit-1"][0]["triggered"] is True


def test_sample_habit_trigger_time_stays_in_morning_window():
    trigger_time = sample_habit_trigger_time(
        datetime(2026, 4, 10, tzinfo=timezone.utc).date(),
        timezone.utc,
    )

    assert trigger_time.hour >= 6
    assert trigger_time.hour <= 12


def test_persistent_desktop_notifications_use_notify_send(monkeypatch):
    calls = []

    def fake_run(command, check):
        calls.append({"command": command, "check": check})

    monkeypatch.setattr("src.main.subprocess.run", fake_run)

    notification_count = create_persistent_desktop_notifications(
        [
            {
                "habit": {"id": "habit-1", "name": NOTIFICATION_HABIT_NAME},
                "trigger": {},
            }
        ]
    )

    assert notification_count == 1
    assert calls == [
        {
            "command": [
                "notify-send",
                "--app-name=prioritise_habits",
                "--urgency=critical",
                "--expire-time=0",
                "Habit due",
                NOTIFICATION_HABIT_TEXT,
            ],
            "check": True,
        }
    ]


def test_bluetooth_sink_detection_uses_wpctl_metadata():
    bluetooth_sink_output = """
id 223, type PipeWire:Interface:Node
    api.bluez5.address = "90:BF:D9:5D:41:D0"
    device.api = "bluez5"
  * media.class = "Audio/Sink"
  * node.description = "soundcore Space Q45"
  * node.name = "bluez_output.90_BF_D9_5D_41_D0.1"
"""
    internal_speaker_output = """
id 90, type PipeWire:Interface:Node
    api.alsa.path = "hw:sofsoundwire,2"
    device.api = "alsa"
    device.icon_name = "audio-speakers"
  * media.class = "Audio/Sink"
  * node.description = "Lunar Lake-M HD Audio Controller Speaker"
  * node.name = "alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink"
"""

    assert is_bluetooth_audio_sink_metadata(bluetooth_sink_output)
    assert not is_bluetooth_audio_sink_metadata(internal_speaker_output)


def test_bluetooth_sink_address_is_read_from_wpctl_metadata():
    bluetooth_sink_output = """
id 196, type PipeWire:Interface:Node
    api.bluez5.address = "90:BF:D9:5D:41:D0"
    api.bluez5.codec = "sbc_xq"
    api.bluez5.profile = "a2dp-sink"
    api.bluez5.transport = ""
    card.profile.device = "1"
  * client.id = "48"
    clock.quantum-limit = "8192"
    device.api = "bluez5"
  * device.id = "189"
    device.routes = "1"
  * factory.id = "12"
    factory.name = "api.bluez5.a2dp.sink"
    library.name = "audioconvert/libspa-audioconvert"
  * media.class = "Audio/Sink"
    media.name = "soundcore Space Q45"
  * node.description = "soundcore Space Q45"
    node.driver = "true"
    node.loop.name = "data-loop.0"
  * node.name = "bluez_output.90_BF_D9_5D_41_D0.1"
    node.pause-on-idle = "false"
  * object.serial = "211"
    port.group = "stream.0"
  * priority.driver = "1010"
  * priority.session = "1010"
    spa.object.id = "1"
"""
    bluetooth_sink_output_without_address = """
id 196, type PipeWire:Interface:Node
  * node.name = "bluez_output.90_BF_D9_5D_41_D0.1"
"""

    assert (
        get_bluetooth_address_from_audio_sink_metadata(bluetooth_sink_output)
        == "90:BF:D9:5D:41:D0"
    )
    assert (
        get_bluetooth_address_from_audio_sink_metadata(
            bluetooth_sink_output_without_address
        )
        == "90:BF:D9:5D:41:D0"
    )


def test_bluez_media_transport_paths_are_matched_by_device_address(monkeypatch):
    busctl_tree_output = """
/
/org
/org/bluez
/org/bluez/hci0
/org/bluez/hci0/dev_07_B9_70_4F_16_66
/org/bluez/hci0/dev_49_CF_81_B9_FD_9F
/org/bluez/hci0/dev_50_B2_B8_06_BE_F8
/org/bluez/hci0/dev_54_F2_9F_9F_AD_A7
/org/bluez/hci0/dev_54_F2_9F_9F_E1_C2
/org/bluez/hci0/dev_5D_D6_D9_09_AF_AE
/org/bluez/hci0/dev_90_BF_D9_5D_41_D0
/org/bluez/hci0/dev_90_BF_D9_5D_41_D0/sep1
/org/bluez/hci0/dev_90_BF_D9_5D_41_D0/sep1/fd0
/org/bluez/hci0/dev_90_BF_D9_5D_41_D0/sep2
/org/bluez/hci0/dev_90_F2_60_BD_B7_3D
/org/bluez/hci0/dev_A0_D3_65_04_20_A3
"""

    def fake_run(command, check, capture_output, text):
        assert command == ["busctl", "tree", "--list", "org.bluez"]
        return subprocess.CompletedProcess(command, 0, stdout=busctl_tree_output)

    monkeypatch.setattr("src.main.subprocess.run", fake_run)

    assert get_bluez_media_transport_paths("90:BF:D9:5D:41:D0") == [
        "/org/bluez/hci0/dev_90_BF_D9_5D_41_D0/sep1/fd0"
    ]


def test_text_to_speech_audio_is_cached(tmp_path, monkeypatch):
    text_to_speech_config = {
        "provider": "elevenlabs",
        "voiceId": "JBFqnCBsd6RMkjVDRZzb",
        "modelId": "eleven_multilingual_v2",
        "outputFormat": "mp3_44100_128",
        "cacheDir": str(tmp_path),
    }
    fetch_calls = []

    def fake_fetch(config, habit_text, api_key):
        fetch_calls.append(
            {
                "voiceId": config["voiceId"],
                "habitText": habit_text,
                "hasApiKey": bool(api_key),
            }
        )
        return b"cached mp3 bytes"

    monkeypatch.setenv("ELEVENLABS_API_KEY", "redacted-test-key")
    monkeypatch.setattr("src.main.fetch_elevenlabs_text_to_speech_audio", fake_fetch)

    first_audio_path = get_or_create_text_to_speech_audio(
        text_to_speech_config, REPLY_HABIT_TEXT
    )
    second_audio_path = get_or_create_text_to_speech_audio(
        text_to_speech_config, REPLY_HABIT_TEXT
    )

    assert first_audio_path == second_audio_path
    assert first_audio_path.read_bytes() == b"cached mp3 bytes"
    assert fetch_calls == [
        {
            "voiceId": "JBFqnCBsd6RMkjVDRZzb",
            "habitText": REPLY_HABIT_TEXT,
            "hasApiKey": True,
        }
    ]


def test_audio_playback_adds_silence_lead_in(tmp_path, monkeypatch):
    audio_path = tmp_path / "habit.mp3"
    audio_path.write_bytes(b"cached mp3 bytes")
    subprocess_calls = []

    def fake_run(command, check):
        subprocess_calls.append({"command": command, "check": check})

    monkeypatch.setattr("src.main.subprocess.run", fake_run)

    play_audio_file(audio_path)

    assert subprocess_calls == [
        {
            "command": [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-hide_banner",
                "-loglevel",
                "error",
                "-af",
                "adelay=750:all=1",
                str(audio_path),
            ],
            "check": True,
        }
    ]


def test_custom_habit_audio_file_plays_without_generating_tts(monkeypatch):
    audio_file = CUSTOM_AUDIO_FILE
    custom_audio_path = PROJECT_ROOT / audio_file
    assert custom_audio_path.is_file()

    ready_triggers = [
        {
            "habit": {
                "id": CUSTOM_AUDIO_HABIT_ID,
                "name": CUSTOM_AUDIO_HABIT_NAME,
                "audioFile": audio_file,
                "dueOutputs": {"textToSpeech": True},
            },
            "trigger": {"time": "2026-06-13T06:30:00+07:00"},
        }
    ]
    played_paths = []

    def fail_if_tts_is_requested(config, habit_text):
        raise AssertionError("custom audio habit should not request generated TTS")

    monkeypatch.setattr("src.main.is_default_audio_output_bluetooth", lambda: True)
    monkeypatch.setattr(
        "src.main.is_default_bluetooth_audio_transport_busy", lambda: False
    )
    monkeypatch.setattr(
        "src.main.get_or_create_text_to_speech_audio", fail_if_tts_is_requested
    )
    monkeypatch.setattr(
        "src.main.play_audio_file", lambda audio_path: played_paths.append(audio_path)
    )

    spoken_triggers = speak_ready_habit_triggers({}, ready_triggers)

    assert [item["habit"]["id"] for item in spoken_triggers] == [
        CUSTOM_AUDIO_HABIT_ID
    ]
    assert played_paths == [custom_audio_path.resolve()]


def test_habit_audio_file_rejects_non_mp3_path():
    with pytest.raises(ValueError, match="must point to an .mp3 file"):
        get_habit_audio_file_path(
            {
                "id": CUSTOM_AUDIO_HABIT_ID,
                "name": CUSTOM_AUDIO_HABIT_NAME,
                "audioFile": str(Path(CUSTOM_AUDIO_FILE).with_suffix(".wav")),
            }
        )


def test_text_to_speech_speaks_sequentially_and_stops_without_bluetooth(
    tmp_path, monkeypatch
):
    text_to_speech_config = {"cacheDir": str(tmp_path)}
    ready_triggers = [
        {
            "habit": {
                "id": "habit-1",
                "name": REPLY_HABIT_NAME,
                "dueOutputs": {"textToSpeech": True},
            },
            "trigger": {"time": "2026-06-12T06:30:00+07:00"},
        },
        {
            "habit": {
                "id": "habit-2",
                "name": FOLLOW_UP_HABIT_NAME,
                "dueOutputs": {"textToSpeech": True},
            },
            "trigger": {"time": "2026-06-12T06:31:00+07:00"},
        },
    ]
    bluetooth_states = iter([True, True, True, False])
    generated_text = []
    played_paths = []

    def fake_get_audio(config, habit_text):
        audio_path = tmp_path / f"{len(generated_text)}.mp3"
        audio_path.write_bytes(b"cached mp3 bytes")
        generated_text.append(habit_text)
        return audio_path

    monkeypatch.setattr(
        "src.main.is_default_audio_output_bluetooth",
        lambda: next(bluetooth_states),
    )
    monkeypatch.setattr(
        "src.main.is_default_bluetooth_audio_transport_busy", lambda: False
    )
    monkeypatch.setattr("src.main.get_or_create_text_to_speech_audio", fake_get_audio)
    monkeypatch.setattr(
        "src.main.play_audio_file", lambda path: played_paths.append(path)
    )

    spoken_triggers = speak_ready_habit_triggers(text_to_speech_config, ready_triggers)

    assert [item["habit"]["id"] for item in spoken_triggers] == ["habit-1"]
    assert generated_text == [REPLY_HABIT_TEXT]
    assert [path.name for path in played_paths] == ["0.mp3"]


def test_phone_audio_control_url_replaces_state_from_environment():
    trigger_url = get_phone_audio_control_trigger_url()

    pause_url = build_phone_audio_control_url(trigger_url, "pause")
    play_url = build_phone_audio_control_url(pause_url, "play")

    assert pause_url.endswith("/audio_toggle?state=pause")
    assert play_url.endswith("/audio_toggle?state=play")


def test_phone_audio_is_paused_around_queued_tts_batch(tmp_path, monkeypatch):
    trigger_url = get_phone_audio_control_trigger_url()
    active_habits = json.loads((PROJECT_ROOT / "active_habits.json").read_text())
    queued_habits = [
        habit
        for habit in active_habits
        if get_habit_due_outputs(habit)[DUE_OUTPUT_TEXT_TO_SPEECH]
    ][:2]
    assert len(queued_habits) == 2

    ready_triggers = [
        {"habit": habit, "trigger": {"time": habit["targetStartDate"]}}
        for habit in queued_habits
    ]
    event_log = []

    def fake_get_habit_audio_path(config, habit):
        return tmp_path / f"{habit['id']}.mp3"

    monkeypatch.setattr("src.main.is_default_audio_output_bluetooth", lambda: True)
    monkeypatch.setattr(
        "src.main.is_default_bluetooth_audio_transport_busy", lambda: False
    )
    monkeypatch.setattr("src.main.get_habit_audio_path", fake_get_habit_audio_path)
    monkeypatch.setattr(
        "src.main.send_phone_audio_control",
        lambda trigger_url, state: event_log.append(f"phone:{state}"),
    )
    monkeypatch.setattr(
        "src.main.time_module.sleep",
        lambda seconds: event_log.append(f"sleep:{seconds}"),
    )
    monkeypatch.setattr(
        "src.main.play_audio_file",
        lambda audio_path: event_log.append(f"play:{audio_path.name}"),
    )

    spoken_triggers = speak_ready_habit_triggers(
        {},
        ready_triggers,
        trigger_url,
    )

    assert [item["habit"]["id"] for item in spoken_triggers] == [
        habit["id"] for habit in queued_habits
    ]
    assert event_log == [
        "phone:pause",
        "sleep:10",
        f"play:{queued_habits[0]['id']}.mp3",
        f"play:{queued_habits[1]['id']}.mp3",
        "sleep:10",
        "phone:play",
    ]


def test_text_to_speech_waits_when_bluetooth_transport_is_busy(monkeypatch):
    ready_triggers = [
        {
            "habit": {
                "id": "habit-1",
                "name": REPLY_HABIT_NAME,
                "dueOutputs": {"textToSpeech": True},
            },
            "trigger": {"time": "2026-06-12T06:30:00+07:00"},
        }
    ]

    def fail_if_audio_is_requested(config, habit_text):
        raise AssertionError("busy Bluetooth transport should keep TTS pending")

    monkeypatch.setattr("src.main.is_default_audio_output_bluetooth", lambda: True)
    monkeypatch.setattr(
        "src.main.is_default_bluetooth_audio_transport_busy", lambda: True
    )
    monkeypatch.setattr(
        "src.main.get_or_create_text_to_speech_audio", fail_if_audio_is_requested
    )

    assert speak_ready_habit_triggers({}, ready_triggers) == []


def test_delivered_output_is_not_routed_again_while_tts_waits():
    ready_trigger = {
        "habit": {
            "id": "habit-1",
            "name": REPLY_HABIT_NAME,
            "dueOutputs": {"writeToMd": True, "textToSpeech": True},
        },
        "trigger": {
            "time": "2026-06-12T06:30:00+07:00",
            "triggered": False,
            "deliveredOutputs": {
                "writeToMd": True,
                "desktopNotification": False,
                "textToSpeech": False,
            },
        },
    }

    assert (
        get_ready_triggers_for_due_output([ready_trigger], DUE_OUTPUT_WRITE_TO_MD)
        == []
    )
    assert get_ready_triggers_for_due_output(
        [ready_trigger], DUE_OUTPUT_TEXT_TO_SPEECH
    ) == [ready_trigger]


def test_deleted_markdown_line_is_not_reappended_after_delivery(tmp_path):
    notes_path = tmp_path / "inbox-index.md"
    ready_trigger = {
        "habit": {
            "id": "habit-1",
            "name": REPLY_HABIT_NAME,
            "dueOutputs": {"writeToMd": True, "textToSpeech": True},
        },
        "trigger": {
            "time": "2026-06-12T06:30:00+07:00",
            "triggered": False,
            "deliveredOutputs": {
                "writeToMd": False,
                "desktopNotification": False,
                "textToSpeech": False,
            },
        },
    }

    markdown_triggers = get_ready_triggers_for_due_output(
        [ready_trigger], DUE_OUTPUT_WRITE_TO_MD
    )
    append_ready_habit_triggers(notes_path, markdown_triggers)
    mark_triggers_output_delivered(markdown_triggers, DUE_OUTPUT_WRITE_TO_MD)
    notes_path.unlink()

    second_markdown_triggers = get_ready_triggers_for_due_output(
        [ready_trigger], DUE_OUTPUT_WRITE_TO_MD
    )
    append_ready_habit_triggers(notes_path, second_markdown_triggers)

    assert second_markdown_triggers == []
    assert not notes_path.exists()
    assert ready_trigger["trigger"]["triggered"] is False


def test_run_lock_skips_second_process(tmp_path):
    lock_path = tmp_path / "habit.lock"

    first_lock = acquire_run_lock(lock_path)
    second_lock = acquire_run_lock(lock_path)

    try:
        assert first_lock is not None
        assert second_lock is None
    finally:
        first_lock.close()


def test_completed_habit_waits_for_all_daily_triggers():
    habit = {
        "id": "habit-1",
        "name": "Sequence",
        "dueOutputs": {"writeToMd": True, "textToSpeech": False},
    }
    schedule = {
        "date": "20260410",
        "triggers": {
            "habit-1": [
                {
                    "time": "2026-04-10T06:30:00+00:00",
                    "triggered": False,
                    "deliveredOutputs": {
                        "writeToMd": False,
                        "desktopNotification": False,
                        "textToSpeech": False,
                    },
                },
                {
                    "time": "2026-04-10T11:30:00+00:00",
                    "triggered": False,
                    "deliveredOutputs": {
                        "writeToMd": False,
                        "desktopNotification": False,
                        "textToSpeech": False,
                    },
                },
            ]
        },
    }
    first_trigger = {"habit": habit, "trigger": schedule["triggers"]["habit-1"][0]}
    second_trigger = {"habit": habit, "trigger": schedule["triggers"]["habit-1"][1]}

    mark_triggers_output_delivered([first_trigger], DUE_OUTPUT_WRITE_TO_MD)

    first_completed_habits, first_checkin_times = get_completed_habits_after_ready_triggers(
        [first_trigger],
        schedule,
    )

    assert first_completed_habits == []
    assert first_checkin_times == {}

    mark_triggers_output_delivered([second_trigger], DUE_OUTPUT_WRITE_TO_MD)

    second_completed_habits, second_checkin_times = (
        get_completed_habits_after_ready_triggers(
            [second_trigger],
            schedule,
        )
    )

    assert second_completed_habits == [habit]
    assert second_checkin_times["habit-1"] == "2026-04-10T11:30:00.000+0000"
