# Habit Prioritization Script (Local JSON Store)

This script prioritizes your due habits from a local JSON file. It does not call TickTick.

## What it does

- Reads active habits, archived habits, and check-in history from local JSON files.
- Finds habits due today from each habit's `repeatRule`.
- Calculates completion rate over `lookBackDays`.
- Reorders due habits so lower completion rates are prioritized first.
- Updates habit names with numeric prefixes (for ordered display).
- Records today's completion in the local `checkins` store.
- Samples each due habit trigger time between 06:00 and 12:00 local time.
- Writes ready habit names to enabled due outputs once their sampled trigger time has passed.
- Speaks ready habit names from cached ElevenLabs MP3s, or plays a configured habit MP3 file, when the default audio output is Bluetooth.

## Configuration

Edit [`config.json`](config.json):

```json
{
  "lookBackDays": 21,
  "habitsStoreFile": "./habits_store.json",
  "activeHabitsFile": "./active_habits.json",
  "textToSpeech": {
    "provider": "elevenlabs",
    "voiceId": "JBFqnCBsd6RMkjVDRZzb",
    "modelId": "eleven_multilingual_v2",
    "outputFormat": "mp3_44100_128",
    "cacheDir": "./.tts_cache"
  }
}
```

- `lookBackDays`: number of days used when calculating completion rate.
- `habitsStoreFile`: path to the archived habit and check-in store.
- `activeHabitsFile`: path to the editable non-archived habits file.
- `textToSpeech`: ElevenLabs voice/model/output settings and MP3 cache directory.

Relative paths are resolved from the repo root.
Set `ELEVENLABS_API_KEY` in `.env`. The key is not logged or committed.

## Active Habit Format

Active/non-archived habits live in [`active_habits.json`](active_habits.json). The
file is a JSON list, and each object keeps the full habit record so edits do not
discard TickTick-derived metadata:

```json
[
  {
    "id": "habit-1",
    "name": "Read",
    "repeatRule": "RRULE:FREQ=DAILY;INTERVAL=1",
    "reminders": ["06:00"],
    "targetStartDate": "2025-01-01",
    "goal": 1,
    "dailyTriggerCount": 1,
    "dueOutputs": {
      "writeToMd": true,
      "desktopNotification": false,
      "textToSpeech": true
    },
    "audioFile": "audio/read.mp3",
    "archivedTime": null,
    "sortOrder": 1
  }
]
```

The most commonly edited fields are ordered near the top. Unknown fields are
preserved when the app saves the file.

## Habit Store Format

Archived habits and completion history live in [`habits_store.json`](habits_store.json):

```json
{
  "habits": [
    {
      "id": "habit-2",
      "name": "Archived habit",
      "archivedTime": "2025-01-01T00:00:00.000+0000"
    }
  ],
  "checkins": {
    "habit-1": [
      {
        "id": "habit-1-20260224",
        "habitId": "habit-1",
        "checkinStamp": 20260224,
        "goal": 1,
        "value": 1,
        "status": 2,
        "checkinTime": "2026-02-24T12:00:00.000+0000",
        "opTime": "2026-02-24T12:00:00.000+0000"
      }
    ]
  }
}
```

Notes:
- `active_habits.json` must contain only habits where `archivedTime` is missing or null.
- `habits_store.json` `habits` must contain only archived habits.
- Every habit object must include at least `id` and `name`.
- `checkins` must be an object keyed by habit id.
- Habits with `archivedTime` are treated as inactive.
- `dailyTriggerCount` is optional and defaults to `1`. Use `2` for a habit that should trigger twice on each due day.
- `dueOutputs` is optional and defaults to `writeToMd` and `textToSpeech` enabled.
- Set `writeToMd`, `desktopNotification`, and `textToSpeech` independently.
- `audioFile` is optional. When present with `textToSpeech` enabled, it must point to an `.mp3` file and is played instead of calling ElevenLabs. Relative paths are resolved from the repo root.

## Trigger Scheduling

Each run creates or reuses a daily trigger schedule at `.habit_trigger_schedule`.
For each due habit trigger, the script samples a local time from 06:00 through 12:00.
Only triggers whose sampled time has passed are written to their enabled outputs.
Desktop notifications are created with `notify-send` using critical urgency and no expiry.
Text-to-speech uses cached ElevenLabs MP3 files and plays them sequentially with `ffplay`, adding a short silent lead-in so Bluetooth outputs do not clip the first word.
Habits can set `audioFile` to play a custom MP3 through the same gated audio channel instead of generating speech.
Audio only runs when `wpctl inspect @DEFAULT_AUDIO_SINK@` shows a Bluetooth sink.
TTS is also deferred while the default Bluetooth sink's BlueZ media transport is already streaming from this laptop.
If the default output is not Bluetooth, the trigger is left pending for a later run.

Run the script repeatedly during that window, for example from cron or another scheduler, if you want habits to appear throughout the morning instead of all at once.

## Usage

```bash
uv run --env-file .env python src/main.py
```

Run in test mode:

```bash
uv run --env-file .env python src/main.py --test
```
