# Decision Log

## Public repository data boundary

- Date: 2026-07-14
- Decision: keep user-specific tool configuration, habit definitions and history, and privacy-sensitive test fixtures as ignored local files; load the phone automation capability from the project-local environment file.
- Rationale: the repository can expose reusable code without publishing personal runtime data or identity-specific machine paths.
- Constraints: preserve existing local file locations and behavior, resolve home-relative paths dynamically, and use a fresh public Git history so removed data is not retained in earlier commits.

## Notes tooling consolidation

- Date: 2026-07-12
- Decision: consolidate notes and notes-skill tooling under `~/dev/notes-tools`, while retaining independent subprojects and dependency environments.
- Rationale: keep related automation discoverable in one private repository without coupling unrelated runtimes.
- Constraints: migrate every active external caller, preserve current working-tree data, do not add old-path compatibility wrappers, and keep secrets and runtime state untracked.
