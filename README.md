# notes-tools

Personal automation for capturing messages and ideas into a Markdown notes vault, maintaining that vault, and publishing selected material. Each standalone tool keeps its own `uv` environment; the root project supplies dependencies for `notes/` and `automation/`.

## Scripts

The tables cover runnable entry points. The remaining Python and JavaScript files are supporting modules.

### `notes/`

| Script | Purpose |
| --- | --- |
| `assistant_convos_to_notes.py` | Adds reminders for stalled Codex threads and unread or interrupted ChatGPT conversations to the notes inbox. |
| `auth_telegram_notifs.py` | Interactively creates or refreshes the Telegram session used by the notification importer. |
| `chatgpt_backend_fetch.mjs` | Fetches unread or interrupted ChatGPT conversation metadata for `assistant_convos_to_notes.py`. |
| `chatgpt_convos_to_notes.mjs` | Incrementally exports active ChatGPT conversations; its independent `--browser-actions` mode opens final-response interactive HTML once and drains the configured browser-handoff project. |
| `discord_notifs_to_notes.py` | Saves unread Discord DMs and mentions as note entries and desktop notifications. |
| `github_notifs_to_notes.py` | Saves unread GitHub notifications to the notes inbox, then marks them read. |
| `linear_notifs_to_notes.py` | Contains a Linear notification importer, but is currently disabled by an immediate exit. |
| `migrate_note_frontmatter_directives.py` | Moves legacy note directives into YAML frontmatter and removes obsolete todo blocks. |
| `normalize_markdown_filenames.py` | Renames Markdown files to stable slugs and updates affected wikilinks. |
| `route_friend_discussion_ideas.py` | Classifies discussion ideas from a scratchpad and routes them into tagged friend notes. |
| `select_infolio_relevance_articles.py` | Selects an unreviewed sample from a ranked Infolio article queue for scheduled analysis. |
| `social_notifs_to_notes.py` | Collects X, LessWrong, and EthResearch notifications into the notes inbox. |
| `telegram_notifs_to_notes.py` | Saves unread Telegram DMs, mentions, and selected small-group messages as notes and desktop notifications. |

### `automation/`

| Script | Purpose |
| --- | --- |
| `run_interactive_codex_session.sh` | Starts or resumes an interactive scheduled Codex session and records its session ID. |
| `run_scheduled_codex_skill.sh` | Runs due Codex skills, message importers, and reply-drafting jobs with locking and logs. |
| `strip_context_frontmatter_fields.py` | Removes publishing-only `gist_url` and `live` fields from context-note frontmatter. |

### Standalone tools

| Script | Purpose |
| --- | --- |
| `autoBlogPost/main.py` | Converts marked notes into Jekyll posts, maintains post frontmatter and links, and commits the blog update. |
| `autoBlogPost/invertBlockquotes.py` | Normalizes nested blockquote conversations for blog publication. |
| `autoBlogPost/run.sh` | Runs the blog publisher from its project directory. |
| `gitWordCountHistory/src/main.py` | Samples Git history and plots Markdown word and file counts over time. |
| `integrate_notes/src/integrate_notes.py` | Uses an LLM to integrate scratchpad material into one note or all notes marked for continuous organization. |
| `integrate_notes/src/integrate_notes_spec.py` | Runs the alternative repository-exploration and edit-based scratchpad integration flow. |
| `prioritise_habits/src/main.py` | Prioritizes due habits from local history and delivers scheduled prompts to notes, desktop notifications, or audio. |
| `pull_memos/pullTempNotes.py` | Imports Google Keep notes and transcribed voice captures, routing text and URLs to their configured destinations. |
| `pull_memos/reauth_keep.py` | Refreshes the local Google Keep master token used by the importer. |
| `syncToGist/bookmarksSync.py` | Publishes browser bookmark folders marked `SHARE` as Gists and builds a Gist index. |
| `syncToGist/notesSync.py` | Publishes notes marked `#share` and their linked notes as interconnected Gists. |
| `syncToGist/teleportWikilinks.py` | Replaces specially marked wikilinks with backlinks in the linked notes. |
| `zk_indexer/src/main.py` | Finds notes absent from index notes, updates `unindexed.md`, and tags index files. |

## Running

Run root tools from this directory with `uv run --env-file .env python notes/<script>.py`. Run a standalone tool from its own directory so its configuration, `.env`, and lockfile remain authoritative. Credentials, personal data, logs, caches, and runtime state stay local and are ignored by Git.
