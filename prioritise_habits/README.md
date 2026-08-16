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
- Can sample paragraph-like segments from a text file and transform them into fresh habit text with Codex.
- Speaks ready habit names from cached Google Cloud or ElevenLabs MP3s, or plays a configured habit MP3 file, when the default audio output is Bluetooth.

## Configuration

Edit [`config.json`](config.json):

```json
{
  "lookBackDays": 21,
  "habitsStoreFile": "./habits_store.json",
  "activeHabitsFile": "./active_habits.json",
  "textToSpeech": {
    "provider": "google",
    "quotaProject": "your-google-cloud-project",
    "gcloudCommand": "/absolute/path/to/gcloud",
    "languageCode": "en-US",
    "voiceName": "en-US-Neural2-D",
    "voiceNamePrefix": "en-US-Neural2-",
    "audioEncoding": "MP3",
    "cacheDir": "./.tts_cache"
  }
}
```

- `lookBackDays`: number of days used when calculating completion rate.
- `habitsStoreFile`: path to the archived habit and check-in store.
- `activeHabitsFile`: path to the editable non-archived habits file.
- `textToSpeech`: Google Cloud or ElevenLabs voice settings and the MP3 cache directory. With Google Cloud, `voiceName` is the fixed default and `voiceNamePrefix` selects the catalog pool used by habits with random voice enabled. The configured account must have access to `quotaProject`, the Cloud Text-to-Speech API must be enabled there, and `gcloud auth print-access-token` must work non-interactively. The script sends `quotaProject` as the quota/billing project and never stores the access token.

For ElevenLabs, set `provider` to `elevenlabs` and configure `voiceId`, optional `voiceIds`, `modelId`, and `outputFormat`. Set `ELEVENLABS_API_KEY` in `.env`; the key is not logged or committed.

Relative paths are resolved from the repo root.

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
    "textSourceFile": "/home/user/notes/advice.md",
    "maxSourceWordCount": 800,
    "textTransformPrompt": "Rewrite the selected source as concise flowing prose.",
    "randomTtsVoice": true,
    "ttsPlaybackSpeed": 2.0,
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
- `textSourceFile` optionally replaces the hardcoded `name` as the delivered habit text. `name` remains the habit's short identity and prioritization label. Relative source paths are resolved from the project root.
- A habit with `textSourceFile` must also set a positive integer `maxSourceWordCount` and a non-empty `textTransformPrompt`.
- If the source exceeds `maxSourceWordCount`, complete segments separated by a blank line are shuffled with fresh operating-system randomness and greedily selected while keeping the selected source strictly below the cap. If no complete segment fits, delivery fails explicitly.
- Selected source text is transformed by non-interactive `codex exec` using `gpt-5.6-terra` with high reasoning. The generated text is stored in the ignored daily schedule and reused across output channels and retries for that trigger.
- `randomTtsVoice` is optional and defaults to `false`. When true with Google Cloud, the script lists voices for `languageCode`, filters them by `voiceNamePrefix`, and randomly chooses one. With ElevenLabs, it chooses from `textToSpeech.voiceIds`, or from the account's voices when no pool is configured; account discovery requires the API key's `voices_read` permission. The choice is persisted for retries and the flag cannot be combined with `audioFile`.
- `ttsPlaybackSpeed` is optional and defaults to `1.0`. Set it to `2.0` to play that habit's generated or custom audio at twice its original speed while retaining the Bluetooth lead-in.
- `audioFile` is optional. When present with `textToSpeech` enabled, it must point to an `.mp3` file and is played instead of calling the configured TTS provider. Relative paths are resolved from the repo root.

## Trigger Scheduling

Each run creates or reuses a daily trigger schedule at `.habit_trigger_schedule`.
For each due habit trigger, the script samples a local time from 06:00 through 12:00.
Only triggers whose sampled time has passed are written to their enabled outputs.
Desktop notifications are created with `notify-send` using critical urgency and no expiry.
Text-to-speech uses cached Google Cloud or ElevenLabs MP3 files and plays them sequentially with `ffplay`, adding a short silent lead-in so Bluetooth outputs do not clip the first word.
Google Cloud random selection uses its `GET /v1/voices` endpoint and the configured voice-name prefix. Without a configured ElevenLabs voice pool, random selection uses ElevenLabs' paginated `GET /v2/voices` endpoint.
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
