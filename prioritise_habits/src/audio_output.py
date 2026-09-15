"""Playback pinned to one Bluetooth device, with no output fallback."""

import re
import subprocess
import time

from loguru import logger


SINK_POLL_SECONDS = 0.05
SINK_INSPECT_TIMEOUT_SECONDS = 0.5
AUDIO_PLAYBACK_LEAD_IN_MILLISECONDS = 750


def get_headphones_sink(headphones_mac):
    """Return the current node's serial only when its Bluetooth address matches."""
    try:
        result = subprocess.run(
            ["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"],
            check=True, capture_output=True, text=True,
            timeout=SINK_INSPECT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning(f"Cannot verify headphones output: {error}")
        return None
    properties = dict(re.findall(r'([\w.-]+) = "([^"]*)"', result.stdout))
    if (
        properties.get("device.api") != "bluez5"
        or properties.get("media.class") != "Audio/Sink"
        or properties.get("api.bluez5.address", "").upper() != headphones_mac.upper()
    ):
        return None
    serial = properties.get("object.serial", "")
    return serial if serial.isdecimal() and int(serial) > 0 else None


def require_headphones_sink(headphones_mac, expected_serial=None):
    serial = get_headphones_sink(headphones_mac)
    if serial is None or (expected_serial is not None and serial != expected_serial):
        raise RuntimeError("Stopping habit audio: default output is no longer the XM6 sink")
    return serial


def wait_on_headphones(seconds, headphones_mac):
    serial = require_headphones_sink(headphones_mac)
    deadline = time.monotonic() + seconds
    while True:
        require_headphones_sink(headphones_mac, serial)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(SINK_POLL_SECONDS, remaining))


def play_audio_file(audio_path, playback_speed=1.0, *, headphones_mac):
    serial = require_headphones_sink(headphones_mac)
    audio_filters = []
    if playback_speed != 1.0:
        audio_filters.append(f"atempo={playback_speed:g}")
    audio_filters.append(f"adelay={AUDIO_PLAYBACK_LEAD_IN_MILLISECONDS}:all=1")
    decoder_command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(audio_path), "-vn", "-af", ",".join(audio_filters),
        "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1",
    ]
    player_command = [
        "pw-play", "--target", serial,
        "--properties",
        "node.dont-fallback=true node.dont-reconnect=true node.dont-move=true",
        "--latency", "20ms", "--raw", "--format", "s16",
        "--rate", "48000", "--channels", "2", "-",
    ]
    processes = []
    try:
        decoder = subprocess.Popen(decoder_command, stdout=subprocess.PIPE)
        processes.append(decoder)
        player = subprocess.Popen(player_command, stdin=decoder.stdout)
        processes.append(player)
        decoder.stdout.close()
        while True:
            require_headphones_sink(headphones_mac, serial)
            try:
                player_status = player.wait(timeout=SINK_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
        require_headphones_sink(headphones_mac, serial)
        if player_status:
            raise subprocess.CalledProcessError(player_status, player_command)
        decoder_status = decoder.wait(timeout=1)
        if decoder_status:
            raise subprocess.CalledProcessError(decoder_status, decoder_command)
    finally:
        # Stop output before the decoder, including on inspection failure or Ctrl-C.
        for process in reversed(processes):
            if process.poll() is None:
                process.kill()
            process.wait()
        if processes and decoder.stdout:
            decoder.stdout.close()
