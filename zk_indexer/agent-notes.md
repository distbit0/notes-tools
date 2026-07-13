# Inbox context

- New links appended to `~/notes/inbox-index.md` are prefixed with `note not in any index note:` so agents can tell they came from the ZK index scan.
- Generated message notes whose stem starts with `msg - ` are intentionally ignored by the unindexed-note scan. They are transient conversation artifacts and should not create inbox warnings merely because no index note links to them.
