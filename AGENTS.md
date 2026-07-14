# Repository instructions

- Keep every tool in its existing top-level subdirectory. Do not merge independent projects into one package or dependency environment.
- The root `uv` project owns only `notes/` and `automation/`.
- Run standalone projects from their own directories so their `.env`, configuration, and lockfile remain authoritative.
- Keep runtime state, credentials, logs, caches, generated reports, and session databases out of Git.
- Update every active external caller when a script path changes. Do not leave compatibility symlinks at old paths.
