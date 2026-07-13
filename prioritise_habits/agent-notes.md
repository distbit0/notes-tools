# Local JSON Habit Store Migration

- `active_habits.json` and `habits_store.json` are local runtime data and must remain ignored. `config.json` keeps their existing paths, while the phone audio control capability is loaded from `PHONE_AUDIO_CONTROL_TRIGGER_URL` in the ignored project `.env`. Privacy-sensitive regression values live in the ignored `tests/private_test_data.json`.
- Goal: remove the hard dependency on TickTick API and keep habit prioritization functional with local state only.
- Design choice: archived habit definitions and completion history stay in `habits_store.json`; editable active/non-archived habit definitions live in `active_habits.json`.
- Active habits are written as full JSON habit records with commonly edited fields ordered first. This preserves metadata while making normal habit edits less buried in TickTick-derived fields.
- Trade-off: this removes remote sync behavior entirely; all updates are now local file mutations by design.
- Validation behavior: the script now raises explicit errors for malformed store/config schema instead of silently fabricating missing fields.
- Priority updates: habit name and `sortOrder` updates are now merged back into the local `habits` list by id, preserving non-prioritized habits unchanged.
- Migration finding: one active habit was duplicated with identical JSON records, which made id-based updates ambiguous. The redundant copy was removed.

# Due Habit Outputs

- `dueOutputs` controls delivery channels independently from recurrence: `writeToMd` appends to `~/notes/inbox-index.md`, while `desktopNotification` sends a persistent `notify-send` notification.
- `textToSpeech` speaks habit prompts through cached ElevenLabs MP3s. It intentionally waits for a Bluetooth default audio sink before generating or playing audio, so prompts are not spoken through the laptop speakers.
- A habit-level `audioFile` path reuses the `textToSpeech` delivery channel but plays the specified project-relative or absolute `.mp3` instead of calling ElevenLabs. Missing custom audio files are explicit playback failures and leave the trigger pending; there is no generated-speech fallback for a habit with `audioFile`.
- The cron entry runs this repo every minute. A repo-local run lock is required because cached MP3 playback is sequential and can last longer than one minute.
- TTS needs the habit cron entry to source `~/dev/guiFromCron/crongui.sh` before running, otherwise cron lacks `XDG_RUNTIME_DIR`/DBus and `wpctl inspect @DEFAULT_AUDIO_SINK@` cannot reach PipeWire. The cron command also needs `uv run --env-file .env` so ElevenLabs cache misses can use the project-specific API key.
- `.habit_trigger_schedule` tracks delivery per output channel in `deliveredOutputs`, not only whole-trigger completion. This prevents Markdown/notification repeats while TTS is still pending for Bluetooth audio; for legacy ready schedule entries, non-TTS outputs are treated as already delivered.
- One 20-item TTS prompt sequence uses a 20-day interval, with exactly one habit target-starting per day and two prompt items combined into each habit. The local due logic treats unchecked habits as overdue after `targetStartDate`, so the no-overlap cadence assumes the normal scheduled run records each delivered habit as checked.
- Habit audio playback uses an `ffplay` `adelay=750:all=1` filter to prepend silence inside the same playback stream. This is meant to wake Bluetooth audio before speech starts; a separate warm-up playback process is less reliable because it can open and close a different stream before the real MP3 starts.
- Before starting a TTS batch, the script treats BlueZ media transport states `pending`, `broadcasting`, and `active` as busy and leaves TTS pending. This only detects streaming visible to the laptop's BlueZ/PipeWire stack; phone-to-headphones multipoint playback may still be invisible.

## Notes filename slug migration

- The notes vault now uses hyphen-slug filenames while keeping readable wikilinks. Direct append targets must use the slugged path, e.g. `~/notes/inbox-index.md`.
- Markdown output targets now reject filenames with whitespace before writing, so stale config cannot recreate old spaced note files.
- The daily trigger schedule is shared across output channels and stored in `.habit_trigger_schedule`; completion/check-in is still based on all daily triggers firing, not on a specific output channel.
