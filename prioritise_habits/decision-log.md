# Decision Log

## Local habit store

- `active_habits.json` owns editable active definitions and `habits_store.json` owns archived definitions and completion history. Both are ignored local runtime data; there is no TickTick synchronization fallback.
- Schema or identity ambiguity is an explicit error. Priority updates merge by habit ID while preserving unrelated records.

## Persisted daily delivery schedule

- Each due habit receives one or more randomly timed triggers from 06:00 through 12:00. `.habit_trigger_schedule` persists each output channel's delivery separately so a pending channel cannot repeat already delivered ones.
- `writeToMd`, desktop notification, and text-to-speech are independent outputs. Completion remains based on all configured daily triggers, not one particular channel.

## Audio delivery is gated, not degraded

- Text-to-speech and custom audio remain pending until the default sink is Bluetooth. A missing custom audio file is an error and never falls back to generated speech or laptop speakers.
- Playback is sequential under a process lock because the every-minute scheduler can otherwise overlap long batches. Phone audio is paused once around the whole batch and resumed once afterward.
- Project audio-control configuration comes from the ignored `.env`, and generated speech is cached locally to avoid repeated API spend.
