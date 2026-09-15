import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from src import audio_output, main


PRIVATE_DATA = json.loads(Path(__file__).with_name("private_test_data.json").read_text())
XM6_METADATA = PRIVATE_DATA["audioOutputFixtures"]["xm6Sink"]
SPEAKER_METADATA = PRIVATE_DATA["audioOutputFixtures"]["speakerSink"]
CONFIG = json.loads(main.CONFIG_FILE.read_text())
HEADPHONES_MAC = CONFIG["textToSpeech"]["headphonesMac"]
BLUETOOTH_ADDRESSES = [
    line.split()[1]
    for line in PRIVATE_DATA["audioOutputFixtures"]["bluetoothDevices"].splitlines()
]
AUDIO_PATH = main.PROJECT_ROOT / PRIVATE_DATA["customAudioFile"]


@pytest.mark.parametrize("metadata", [XM6_METADATA, SPEAKER_METADATA])
@pytest.mark.parametrize("address", BLUETOOTH_ADDRESSES)
def test_only_exact_bluetooth_device_is_accepted(monkeypatch, metadata, address):
    monkeypatch.setattr(
        audio_output.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=metadata),
    )
    expected = "211" if metadata == XM6_METADATA and address == HEADPHONES_MAC else None
    assert audio_output.get_headphones_sink(address) == expected


def test_inspection_failure_prevents_any_playback(monkeypatch):
    def fail_inspection(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    start_process = Mock()
    monkeypatch.setattr(audio_output.subprocess, "run", fail_inspection)
    monkeypatch.setattr(audio_output.subprocess, "Popen", start_process)
    with pytest.raises(RuntimeError):
        audio_output.play_audio_file(AUDIO_PATH, headphones_mac=HEADPHONES_MAC)
    start_process.assert_not_called()


@pytest.mark.parametrize("playback_speed", [1.0, 2.0])
@pytest.mark.parametrize("disconnect", [False, True])
def test_playback_is_pinned_and_kills_both_processes_on_output_change(
    monkeypatch, playback_speed, disconnect
):
    # Replay the recorded XM6 -> actual laptop-speaker transition after startup.
    metadata = iter([XM6_METADATA, XM6_METADATA, SPEAKER_METADATA if disconnect else XM6_METADATA])
    monkeypatch.setattr(
        audio_output.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=next(metadata)),
    )
    decoder = Mock()
    player = Mock()
    decoder.wait.return_value = 0
    player.wait.side_effect = (
        [subprocess.TimeoutExpired("pw-play", 0.05), 0]
        if disconnect else [0, 0]
    )
    player.poll.return_value = None if disconnect else 0
    decoder.poll.return_value = None if disconnect else 0
    start_process = Mock(side_effect=[decoder, player])
    monkeypatch.setattr(audio_output.subprocess, "Popen", start_process)
    if disconnect:
        with pytest.raises(RuntimeError):
            audio_output.play_audio_file(AUDIO_PATH, playback_speed, headphones_mac=HEADPHONES_MAC)
        player.kill.assert_called_once()
        decoder.kill.assert_called_once()
    else:
        audio_output.play_audio_file(AUDIO_PATH, playback_speed, headphones_mac=HEADPHONES_MAC)
        player.kill.assert_not_called()
        decoder.kill.assert_not_called()
    decoder_command = start_process.call_args_list[0].args[0]
    filters = decoder_command[decoder_command.index("-af") + 1]
    assert filters == ("atempo=2," if playback_speed == 2.0 else "") + "adelay=750:all=1"
    player_command = start_process.call_args_list[1].args[0]
    assert player_command[player_command.index("--target") + 1] == "211"
    properties = player_command[player_command.index("--properties") + 1]
    assert dict(item.split("=") for item in properties.split()) == {
        "node.dont-fallback": "true",
        "node.dont-reconnect": "true",
        "node.dont-move": "true",
    }
    assert start_process.call_args_list[1].kwargs["stdin"] is decoder.stdout
    assert player.wait.called and decoder.wait.called


def test_pause_is_cancelled_as_soon_as_output_check_fails(monkeypatch):
    sinks = iter(["211", "211", None])
    monkeypatch.setattr(audio_output, "get_headphones_sink", lambda mac: next(sinks))
    sleep = Mock()
    monkeypatch.setattr(audio_output.time, "sleep", sleep)
    with pytest.raises(RuntimeError):
        audio_output.wait_on_headphones(CONFIG["textToSpeech"]["pauseSeconds"], HEADPHONES_MAC)
    sleep.assert_called_once_with(audio_output.SINK_POLL_SECONDS)


def test_interrupted_trigger_remains_pending_and_stops_batch(monkeypatch):
    habits = json.loads((main.PROJECT_ROOT / "active_habits.json").read_text())[:2]
    ready = [{"habit": habit, "trigger": {}} for habit in habits]
    monkeypatch.setattr(main, "get_headphones_sink", lambda mac: "211")
    monkeypatch.setattr(main, "is_headphones_audio_transport_busy", lambda mac: False)
    monkeypatch.setattr(main, "get_habit_audio_paths", lambda config, item: [AUDIO_PATH])
    play = Mock(side_effect=RuntimeError("Output changed"))
    monkeypatch.setattr(main, "play_audio_file", play)
    assert main.speak_ready_habit_triggers(CONFIG["textToSpeech"], ready) == []
    play.assert_called_once()
    assert all(item["trigger"] == {} for item in ready)


def test_missing_headphones_configuration_fails_closed():
    with pytest.raises(ValueError, match="headphonesMac"):
        main.speak_ready_habit_triggers({}, [])


def test_player_start_failure_cleans_up_decoder(monkeypatch):
    monkeypatch.setattr(audio_output, "get_headphones_sink", lambda mac: "211")
    decoder = Mock()
    decoder.poll.return_value = None
    monkeypatch.setattr(
        audio_output.subprocess, "Popen",
        Mock(side_effect=[decoder, FileNotFoundError("pw-play")]),
    )
    with pytest.raises(FileNotFoundError):
        audio_output.play_audio_file(AUDIO_PATH, headphones_mac=HEADPHONES_MAC)
    decoder.kill.assert_called_once()
    decoder.wait.assert_called_once()
    decoder.stdout.close.assert_called_once()


@pytest.mark.parametrize("failed_process", ["decoder", "player"])
def test_process_failure_is_not_reported_as_delivery(monkeypatch, failed_process):
    monkeypatch.setattr(audio_output, "get_headphones_sink", lambda mac: "211")
    decoder = Mock()
    player = Mock()
    decoder.wait.return_value = 1 if failed_process == "decoder" else 0
    player.wait.return_value = 1 if failed_process == "player" else 0
    decoder.poll.return_value = decoder.wait.return_value
    player.poll.return_value = player.wait.return_value
    monkeypatch.setattr(audio_output.subprocess, "Popen", Mock(side_effect=[decoder, player]))
    with pytest.raises(subprocess.CalledProcessError):
        audio_output.play_audio_file(AUDIO_PATH, headphones_mac=HEADPHONES_MAC)
