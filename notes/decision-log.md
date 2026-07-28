# Decision Log

## 2026-07-17: Bound ChatGPT backend request volume

- The separate 15-minute pending-conversation scan was removed after it repeatedly scanned roughly 1,600 conversations and triggered sustained HTTP 429 responses.
- The pending-reminder writer is now retained but disabled. The Brave opener runs every four hours. Archive export remains at 03:00 and 15:00, and uses 100-conversation list pages so it retains complete cutoff coverage with fewer requests.
- All direct ChatGPT backend work shares one client with 10–15 second pacing. A 429 is never retried: it records a shared 24-hour cooldown, terminates the triggering run visibly, and makes later invocations explicit zero-request no-ops until expiry.
- The user chose to retain low-rate access to the unofficial consumer backend. Lower request volume reduces the observed failure mode but does not remove account or terms risk.

## 2026-07-28: Brave history is the conversation-view ledger

- The opener performs one complete active-conversation scan when its dedicated ledger is absent. Each later four-hour run stops normal and per-project pagination at the last fully successful scan watermarks; failed or artificially limited runs never advance them.
- A conversation absent from Brave history is queued unconditionally; a visited conversation is queued only when its latest visible assistant message is more than ten minutes newer than its latest Brave visit.
- The grace interval treats a response completed shortly after navigation as viewed. Brave history is the sole open ledger, so clearing or expiring history intentionally makes conversations eligible again.
- Qualifying URLs are appended uniquely to a mode-0600 text file rather than opened directly because launching the initial backlog crashed Brave. The user removes processed URLs from that file; a later update can then queue the same conversation again.
- Until the first full scan succeeds, an exact-title or conversation-URL recovery manifest in the opener state directory preserves URLs from the interrupted browser-opening run without trusting Brave's lossy history. Titles must resolve uniquely against the complete active-conversation list; exact URLs remain valid if a recovered conversation has since become inactive. The manifest is ignored once a successful scan ledger exists.
- The former `open_in_browser` project handoff, interactive-HTML download/open behavior, browser-action state, and pending-inbox integration were removed.

## 2026-07-14: ChatGPT conversation sync schedule

- Sync active ChatGPT conversations into `~/notes/chatgpt-conversations` at 03:00 and 15:00 with a persistent systemd user timer.
- The 15:00 run refreshes the archive before the 16:00 assistant-chat distillation job. The sync's existing twice-daily and six-hour run gate remains authoritative.

## 2026-06-22: EthResearch social notification scope

- Capture EthResearch through the existing social-notification runner using Brave-authenticated Discourse notifications and private-message topics.
- Exclude regular `/unread.json` topics so followed forum activity does not become notification noise.

## Assistant conversation reminders

- Codex final answers are tracked per exact assistant-message offset only so a later user reply or notification activation can mark them handled. They are not appended to the inbox on a timer.
- A Codex reminder is emitted only for an interactive thread whose latest meaningful activity is a user prompt and whose watcher/session evidence shows no active process. Scheduled `exec` sessions are excluded.
- ChatGPT unread state comes from recognized backend status fields on incrementally discovered conversations. Unknown status shape produces a warning and no reminder rather than a guessed unread state.

## Message capture preserves upstream state

- Telegram, Discord, and social importers use local cursors for deduplication and do not mutate Telegram unread state. Upstream read markers remain evidence for whether a newly seen message deserves a notification.
- Desktop notification delivery and Markdown persistence complete before cursors advance or GitHub threads are marked read, so a local failure remains retryable rather than losing the alert.
- Literal top-level `msg - *.md` filenames are a deliberate reply-workflow interface. Filename normalization preserves them, and cleanup considers them live only when linked by a non-message note.
- Discord pulling is temporarily disabled by `DISCORD_POLLING_ENABLED = False`; its implementation, scheduled entry, and state remain intact for later re-enablement.

## ChatGPT archive is an append-only message ledger

- Sync only active conversations updated since the configured cutoff. The local state records seen message IDs, so deleting an exported Markdown file does not cause old messages to be reconstructed.
- ChatGPT HTTP requests use `curl` because Cloudflare stalls Node TLS handshakes while accepting the same Brave session through `curl`; secrets are supplied through mode-0600 temporary config files. Python remains limited to local Brave-cookie extraction because equivalent authenticated Python requests were rejected.

## Private regression data stays local

- Identity-sensitive notification and routing fixtures live in ignored `tests/private_test_data.json`. Tests fail explicitly when that real local fixture is absent rather than substituting dummy data.
