# Scheduled Codex skills

- Scheduled Codex jobs use a thin wrapper plus systemd user timers. The wrapper always runs Codex with `~/notes` as its working directory; each skill owns its file selection and mutation policy.
- Job schedules live in `scheduled_codex_jobs` in `run_scheduled_codex_skill.sh`. Systemd owns slot wakeups, while the wrapper decides which jobs are due.
- Interactive jobs use `run_interactive_codex_session.sh` and keep the Codex TUI as the foreground terminal process. Only session-id recording runs in the background.
- The unified message-pull path runs GitHub, Linear, Telegram, Discord, and social notification importers before reply drafting. `assistant_convos_to_notes.py` remains outside this path.
- Message-producing importers record changed message-note paths through `MESSAGE_NOTIF_CHANGED_NOTES_FILE`; reply drafting runs only when the pull cycle changed message notes.
- Failures in Codex state schema integration or Herdr agent/pane operations must remain explicit. Do not add silent execution fallbacks.

