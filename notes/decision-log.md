# Decision Log

## 2026-07-14: ChatGPT conversation sync schedule

- Sync active ChatGPT conversations into `~/notes/chatgpt-conversations` at 03:00 and 15:00 with a persistent systemd user timer.
- The 15:00 run refreshes the archive before the 16:00 assistant-chat distillation job. The sync's existing twice-daily and six-hour run gate remains authoritative.

## 2026-06-22: EthResearch social notification scope

- Capture EthResearch through the existing social-notification runner using Brave-authenticated Discourse notifications and private-message topics.
- Exclude regular `/unread.json` topics so followed forum activity does not become notification noise.
