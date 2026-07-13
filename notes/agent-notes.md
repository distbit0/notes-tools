# Telegram notifications to notes (March 2026)

- Privacy-sensitive regression values shared by notification and friend-routing tests live in the ignored `tests/private_test_data.json`; the tracked loader fails explicitly when that local fixture is absent.

## Friend discussion idea router

- `notes/route_friend_discussion_ideas.py` treats `~/notes/friends-index.md` as the source of discussion ideas, but it never edits that scratchpad. Per-item classification and per-file routing state live in `~/notes/.friend-idea-router-cache.json`, so deleting a routed line from a friend note does not cause it to be re-added later.
- Friend-note routing only considers files linked from the `# friends` section of `friends-index.md`, and only frontmatter `tag`/`tags` values in those linked files are route tags. Body hashtags and markdown headings are intentionally ignored.
- User crontab runs the friend discussion idea router hourly at minute 17 from `~/dev/misc`, with cron stdout/stderr written to `~/dev/notes-tools/notes/friend-idea-router.cron.log`. The script's own log remains `~/dev/notes-tools/notes/friend-idea-router.log`.
- OpenRouter classifications run in prompt groups of up to 15 scratchpad items, with up to 3 prompt groups in flight at once. Cache mutation and markdown writes still happen on the main thread after classification results return.
- Each OpenRouter prompt group is retried up to three times with explicit warning logs. If the prompt group still fails validation, every item in that prompt group is left unclassified in the cache so a later run retries it, while successful prompt groups continue to route normally.
- Cache locks contain the owning PID. If the lock file exists but that PID no longer exists, the friend discussion idea router logs a warning, removes the stale lock, and retries lock creation; it still fails fast for malformed lock files or live owner PIDs.
- Scratchpad classification is deduplicated by cache key before parallel OpenRouter calls. The live `friends-index.md` has repeated item text, and classifying duplicates concurrently can produce contradictory tag decisions for the same cache entry.

## Assistant conversations to notes

- Codex final-answer note state is keyed by exact final assistant message (`codex:<thread_id>:assistant:<jsonl_offset>`), not by thread. A tracked final answer is handled once the user replies after it or its desktop notification is activated/clicked; final answers are not appended to `inbox-index.md` on a timer.
- `notes/assistant_convos_to_notes.py` tracks Codex final-answer records created by `record_codex_pending(...)` only so activation-ledger clicks and later user replies can mark them handled. It does not append completed final-answer reminders to `inbox-index.md` on a timer, because that produced too many false positives.
- Separately, Codex threads whose latest meaningful activity is a real user prompt can become `possibly stalled codex convo:` entries after 15 minutes, but only when the watcher PID file and `/proc` rollout-file scan do not show an active thread.
- Codex unread capture is only for interactive Codex thread sources (`cli` and `vscode`). Non-interactive `codex exec` sessions, including scheduled skill runs from `automation/run_scheduled_codex_skill.sh`, are ignored and any existing pending records for those threads are marked `non_interactive_source` instead of appended.
- Codex note entries should put a plain `cd <session cwd> && codex resume <thread-id>` command on its own line immediately below the label. This lets Vim copy the command with `yy` from that line, or `jyy` from the label line. Do not emit custom `codex-resume://` links; VS Code Markdown tries to resolve them as editor resources instead of reliably delegating to the OS URL handler.
- Assistant conversation reminders use short `possibly stalled codex convo:` / `maybe pending chatgpt convo:` prefixes in `inbox-index.md` so agents can identify them without treating the false-positive-prone unread signal as certain. Their stored prompt/title excerpt is capped at 128 collapsed characters.
- Codex reminder labels use the latest meaningful user prompt in the rollout log rather than the thread's first message. User messages that are only Codex `<turn_aborted>` interruption wrappers are skipped entirely for stalled detection. The persisted JSON field is still named `first_message_prefix` for the existing state shape, but its Codex semantics are now this latest-user label.
- ChatGPT unread capture uses Brave Default cookies decrypted by Python and passes only a cookie header to the Node fetcher over stdin. Node owns the ChatGPT network calls because local testing found Node fetch succeeds where Python requests hit Cloudflare 403. In the backend conversation list, `async_status == 4` is the unread signal; null/other async status values are known non-unread. ChatGPT note labels use the conversation title from ChatGPT, not the first user message.
- ChatGPT cut-off detection scans the latest 100 backend conversation-list items and checks list/status metadata plus embedded message mappings. If the list no longer exposes a recognized unread boolean or `async_status`, the script logs a warning and cannot emit ChatGPT-unread links from backend state rather than guessing read state.

## ChatGPT conversations sync

- ChatGPT conversation sync should use the live Brave `Default` profile cookies as the default auth path. Local inspection showed ChatGPT's split `__Secure-next-auth.session-token.*` cookies are encrypted in Brave's SQLite cookie DB, and `browser-cookie3` can decrypt them on this Fedora/XFCE machine.
- Direct Python `requests` to `https://chatgpt.com/api/auth/session` returned Cloudflare 403 even with decrypted cookies, while Node 22 native `fetch` succeeded. Keep ChatGPT network requests in Node and use Python only for cookie extraction.
- The sync is intentionally incremental and conservative: it should export only conversations updated on or after `2026-05-27T00:00:00+07:00`, exclude archived conversations, enforce at most two sync starts per Ho Chi Minh calendar day, and require at least six hours since the previous start before making any ChatGPT network request.
- `chatgpt-conversations-sync.timer` runs the sync at 03:00 and 15:00 as a persistent systemd user timer. The unit files live in `~/.config/systemd/user/`; the service gives the puller up to one hour because a catch-up export can legitimately exceed systemd's default start timeout. Runtime sync state lives outside Git at `~/.local/state/chatgpt-convos-to-notes/state.json`.
- Conversation sync treats the local state file as a message ledger. If a local conversation markdown file is deleted after being saved, the sync must not reconstruct old messages; existing pre-ledger state is migrated by recording currently visible message ids as seen without writing content, so future runs append only newly unseen message ids.

## Note directive frontmatter migration

- `~/dev/notes-tools/notes/migrate_note_frontmatter_directives.py` intentionally migrates only top-ish grouping directives into `grouping: |` frontmatter and deletes obsolete `+++++` todo blocks. It does not migrate `#share`, `#ghp`, or `#hbp`; those are body markers owned by sharing/blog publishing scripts.

- `notes/telegram_notifs_to_notes.py` now keeps Telegram unread status untouched and uses a local cursor state file at `~/.local/state/telegram-notifs-state.json` as the source of truth for what was already processed.
- The state tracks latest processed message metadata per chat (`chat_id`, `sender_id`, `latest_message_timestamp_ms`, `latest_message_id`, `read=true`) separately for DMs and mentions, so newly arrived messages are detected even if they are already read inside Telegram.
- For chats that already have local cursors, only Telegram-unread DMs/mentions are appended to notes; Telegram-read messages still advance local cursors so already-read items do not keep getting reconsidered.
- Telegram chat-level `unread_mark` is treated as an explicit override: when a chat is manually marked unread, all new DMs/mentions in that chat are considered notify-worthy even if each message is individually marked read.
- Telegram Desktop does not support `tg://openmessage?...` and only jumps to specific message IDs for channels/supergroups; private user chats cannot be deep-linked to an exact message via desktop URL handlers.
- `notes/telegram_notifs_to_notes.py` now emits only Telegram Desktop-supported links (`message.link`, `https://t.me/c/...` for channels, public user profile/phone resolve links when available) and falls back to plain text notifications when no direct link exists.
- Basic private groups (`Chat`) still have no normal `t.me` message URL; prefer the chat's exported invite link when Telethon exposes one via `GetFullChat`, and otherwise fall back to Telegram Web K at `https://web.telegram.org/k/#<peer_id>` using signed `get_peer_id(...)`.
- Dialog URL resolution must tolerate non-notifiable Telethon dialog entity variants by returning `None` instead of raising. The invite-link lookup should only fail fast once a real notification URL is being built for an unsupported entity; otherwise cron scans can die before reaching relevant chats.
- The synced user-crontab copy had pre-existing GitHub/Linear notification entries pointing at removed top-level script paths; they were corrected to `notes/` paths while adding the Telegram cron entry.
- Telethon can yield `MessageService` objects from `iter_messages` in DM/mention scans; these objects may have `out` but no `unread`, so unread filtering must explicitly skip service messages before touching unread-related fields.
- Telethon `Message` objects from history scans may also omit per-message flags/fields like `unread` and `link`; notifier logic should treat dialog-level unread counters as authoritative and use `getattr` for optional message URL fields.
- For chats with existing local cursors, notify selection now uses a newest-tail window sized by dialog unread counters (`unread_count` / `unread_mentions_count`) while still advancing cursors for all newly seen incoming messages.
- Telegram unread message scanning now covers both direct-message dialogs and small group chats with fewer than 15 members, but only when the group dialog is not muted. Mute state is read from `dialog.dialog.notify_settings.mute_until`; missing participant counts intentionally fail closed so the notifier does not guess which groups qualify.

# Discord notifications to notes (March 2026)

- `notes/discord_notifs_to_notes.py` now derives unread status from Discord Gateway `READY` `read_state` markers (`last_message_id`) instead of using bootstrap heuristics, so per-message unread is computed as `message_id > last_message_id` for both DMs and mentions.
- Discord entries written to `inbox-index.md` use a short `discord:` source prefix. The message label itself still keeps DMs as `<chat>: <preview>` and mentions as `"Discord mention - ..."`.
- Discord web auth is loaded from Brave Default local storage LevelDB and validated against `/users/@me` each run.

## Notes filename slug migration

- Notification capture scripts write to the slugged inbox filename `inbox-index.md`, while note content can keep readable wikilinks.
- Notification capture scripts prefix inbox entries with their source (`github:`, `linear:`, `telegram:`, `discord:`, `x:`, `lesswrong:`) while leaving desktop notification summaries unchanged.
- Telegram/Discord message capture intentionally creates literal `msg - ...md` files and `[[msg - ...]]` wikilinks despite the vault's normal slug convention, because the scheduled reply-drafting workflow scans for that prefix. `normalize_markdown_filenames.py` must keep those files protected.
- Message-note cleanup treats root-level `msg - *.md` files as live only when linked from non-message markdown files under the notes directory. Links from other `msg - *.md` files do not keep a message note alive, so stale message-note clusters are removed after inbox/todo links are deleted.
- REST polling remains on `/users/@me/channels`, `/channels/{id}/messages`, and `/users/@me/mentions`, with local cursors still used only for de-duplication of already-processed messages.
- Discord can omit `read_state` entries for DM channels that have never had any message (`/users/@me/channels` reports `last_message_id = null` for those channels). Treat those as read marker `0`; still fail fast if a non-empty DM lacks read-state so unread semantics are not silently guessed.

## Social notifications to notes

- `notes/social_notifs_to_notes.py` uses Brave Default cookies for X and LessWrong. X fetches the current public web bearer token from the loaded X web bundle each run, because hard-coded X bearer tokens have changed.
- X reply/mention capture uses `notifications/mentions.json` and the server-provided `markEntriesUnreadGreaterThanSortIndex` boundary; X DMs use `dm/inbox_initial_state.json`, filter to trusted, non-muted conversations, and compare incoming message ids to each conversation's `last_read_event_id`.
- LessWrong capture uses authenticated `/graphql`: unread notifications come from `notifications(selector: unreadUserNotifications)`, and private-message reminders come from conversations whose `hasUnreadMessages` is true. DM links use `/inbox?conversation=<id>`, matching the current web route query name.
- EthResearch capture uses Brave-authenticated Discourse endpoints: paginated `/notifications` plus `/topics/private-messages/<username>.json`. It intentionally ignores regular `/unread.json` topics to avoid turning followed forum activity into notification noise.
- On 2026-06-22, the active user crontab already ran `notes/social_notifs_to_notes.py` every 15 minutes even though the synced copy inspected earlier was stale.

# Desktop notifications for note sync scripts (April 2026)

- `notes/notes_utils.py` now owns the desktop notification path via `notify-send` so Telegram, Discord, and GitHub note sync scripts all use the same persistence/error handling behavior instead of each shelling out differently.
- Notification and DM capture scripts should write inbox entries through `format_notification_note_line(...)`, producing `new notif: <source>: ...` note lines while leaving desktop notification summaries unprefixed.
- The desktop notifications are sent with `--expire-time 0` and `--urgency critical` to keep them resident until dismissed on the local Linux desktop.
- The shared helper now also sends a Dunst `category` hint so desktop color rules can distinguish Telegram, Discord, and GitHub notifications without relying on appname matching.
- Telegram and Discord now emit desktop notifications before appending to markdown/saving cursor state, so a notification-delivery failure retries later rather than silently advancing the local cursors and losing the alert.
- GitHub thread notifications are marked read only after the markdown append succeeds, so a local write failure does not immediately clear the unread thread upstream.
- The local Dunst config renders only the notification summary (`format = "%s"`) for the Telegram/Discord/GitHub categories, so the scripts must put the human-readable preview in the summary; the body can be reserved for a hidden click target URL.
- Left click in the local Dunst config intentionally stays `do_action` because other notification sources rely on Dunst activation handlers. Do not combine `open_url` with `do_action` for note notifications; that can trigger URL-backed notifications twice.
- Group-originated Telegram and Discord notifications now include the conversation name in the summary (`<group> | <sender>: ...`) so they do not read like DMs, and group mentions additionally prefix the preview text with `@`.
