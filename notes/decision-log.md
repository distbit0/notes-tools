# Decision Log

## 2026-07-16: Open interactive HTML conversations once

- Open a conversation in Brave when its latest visible assistant message contains generated sandbox HTML or direct `text/html` content.
- Persist both the last checked update time and the successful open time in the conversation ledger. Updated non-matches are reconsidered; a conversation that has opened once is never opened again by this rule.
- Existing current exports without any HTML hint are ruled out locally; possible matches and missing exports are verified from the live conversation response.

## 2026-07-16: ChatGPT project as a browser handoff queue

- After a successful archive pass, the conversation sync opens every chat in the configured `open_in_browser` project as a new Brave tab, then removes its project association through the ChatGPT backend API.
- The project itself is the retry ledger: a chat remains queued if Brave cannot accept the tab or the API removal fails. The sync records no second queue state.

## 2026-07-14: ChatGPT conversation sync schedule

- Sync active ChatGPT conversations into `~/notes/chatgpt-conversations` at 03:00 and 15:00 with a persistent systemd user timer.
- The 15:00 run refreshes the archive before the 16:00 assistant-chat distillation job. The sync's existing twice-daily and six-hour run gate remains authoritative.

## 2026-06-22: EthResearch social notification scope

- Capture EthResearch through the existing social-notification runner using Brave-authenticated Discourse notifications and private-message topics.
- Exclude regular `/unread.json` topics so followed forum activity does not become notification noise.

## Assistant conversation reminders

- Codex final answers are tracked per exact assistant-message offset only so a later user reply or notification activation can mark them handled. They are not appended to the inbox on a timer.
- A Codex reminder is emitted only for an interactive thread whose latest meaningful activity is a user prompt and whose watcher/session evidence shows no active process. Scheduled `exec` sessions are excluded.
- ChatGPT unread state comes from recognized backend status fields fetched with Brave cookies through Node. Unknown status shape produces a warning and no reminder rather than a guessed unread state.

## Message capture preserves upstream state

- Telegram, Discord, and social importers use local cursors for deduplication and do not mutate Telegram unread state. Upstream read markers remain evidence for whether a newly seen message deserves a notification.
- Desktop notification delivery and Markdown persistence complete before cursors advance or GitHub threads are marked read, so a local failure remains retryable rather than losing the alert.
- Literal top-level `msg - *.md` filenames are a deliberate reply-workflow interface. Filename normalization preserves them, and cleanup considers them live only when linked by a non-message note.

## ChatGPT archive is an append-only message ledger

- Sync only active conversations updated since the configured cutoff. The local state records seen message IDs, so deleting an exported Markdown file does not cause old messages to be reconstructed.
- Network fetching stays in Node because equivalent authenticated Python requests were rejected by Cloudflare; Python owns local Brave-cookie extraction only.

## Private regression data stays local

- Identity-sensitive notification and routing fixtures live in ignored `tests/private_test_data.json`. Tests fail explicitly when that real local fixture is absent rather than substituting dummy data.
