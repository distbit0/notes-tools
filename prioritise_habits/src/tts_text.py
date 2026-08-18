import re


TTS_PAUSE_MARKER = "[[PAUSE]]"


def remove_tts_pause_markers(text):
    return re.sub(
        rf"\s*{re.escape(TTS_PAUSE_MARKER)}\s*",
        " ",
        text,
    ).strip()


def split_tts_text(text):
    raw_segments = text.split(TTS_PAUSE_MARKER)
    if len(raw_segments) > 1 and any(not segment.strip() for segment in raw_segments):
        raise ValueError(
            f"{TTS_PAUSE_MARKER} must appear only between non-empty text segments"
        )
    return [segment.strip() for segment in raw_segments if segment.strip()]
