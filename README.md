# notes-tools

Monorepo for scripts that operate on `~/notes` or run its agent skills.

Each standalone tool remains an independent `uv` project in its own directory. The root project supplies dependencies for `notes/` and `automation/`.

## Tools

- `notes/`: notification capture, conversation export, note maintenance, and friend-idea routing.
- `automation/`: scheduled and interactive Codex skill runners and context-frontmatter cleanup.
- `autoBlogPost/`: publish marked notes to the Jekyll blog.
- `gitWordCountHistory/`: analyse the notes repository's word-count history.
- `integrate_notes/`: organise and integrate notes.
- `prioritise_habits/`: prioritise habits and append ready triggers to the notes inbox.
- `pull_memos/`: import Google Keep notes and voice memos.
- `syncToGist/`: publish notes and bookmark collections to Gists.
- `zk_indexer/`: identify notes missing from indexes.

Run a root tool from this directory with `uv run --env-file .env python notes/<script>.py`. Run a standalone tool from its own directory with `uv run --env-file .env ...`.
