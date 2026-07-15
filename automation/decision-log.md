# Decision Log

## Scheduled contradiction processing

- Date: 2026-07-16
- Decision: run `scheduled-resolve-contradictions` every six days at 04:00 on phase 1 as an unattended `exec` session, and disable the separate `scheduled-note-critique` detector. The resolver's phase is disjoint from the three-day tweet job and the other six-day 04:00 job.
- Purpose: use contradictions to find consequential errors in prior thinking, derive novel or useful implications, and propose improvements rather than merely reconcile incompatible statements.

## Daily goal advancement scheduling

- Date: 2026-07-14
- Decision: run `scheduled-goal-advancement` every day at 07:00 through the existing scheduled-job wrapper, using an `exec` session and a dedicated `daily-goal-advancement` Codex profile.
- Rationale: isolate its maximum reasoning and broad local implementation work from the defaults used by other scheduled skills, while keeping the run out of interactive session history.
- Safety: the profile uses a named permission policy that writes only under `/home/pimania` and temporary directories, denies credential stores, and enables unattended approvals without the wrapper's full-access bypass. It permits SSH clients and Git to authenticate with existing identities under `~/.ssh`, while forbidding agents from inspecting, copying, modifying, or exposing private-key material. The skill forbids messaging, spending, high-consequence actions, and obvious public association with the user.

## Behavior-aware hard feedback scheduling

- Date: 2026-07-13
- Decision: run `scheduled-hard-feedback` every five days at 05:00 on phase 3, using the enabled 05:00 user timer.
- Rationale: the redesigned skill needs multiple days of completed time-allocation evidence, while its phase keeps it separate from the other five-day jobs at 05:00.
- Constraints: the skill must remain an unattended `exec` session, emit at most one decision-changing item, and no-op clearly when its ICS evidence is missing, stale, or insufficient.

## Scheduled skill cadence revision

- Date: 2026-07-13
- Decision: run tweet ideas every three days at 04:00; idea-space search every five days at 05:00; the security audit every three weeks on Sunday at 11:00; and assistant-chat distillation every two days at 16:00.
- Rationale: separating the three-day and five-day jobs preserves the one-Codex-job-per-slot invariant without changing their requested frequencies.
- Reliability: cadence phase is derived from the scheduled slot, not the wall-clock replay time, so a persistent timer catch-up after the slot cannot incorrectly skip a due job. This fixes the missed tweet-ideas catch-up observed on 2026-07-08.

## Scheduled Infolio relevance preparation

- Date: 2026-07-12
- Decision: run the Infolio relevance job every three days at 21:00 and prepare its article selection before starting Codex, so the five selected Lineate URLs appear in the initial prompt.
- Rationale: all 04:00 cadence phases overlap an existing six-day job, while the scheduler intentionally limits each time slot to one Codex job. Preselection makes duplicate exclusion deterministic and prevents Codex from changing the sampled batch.
- Constraints: keep the 21:00 timer disabled until Infolio's `documents.lineate_url` migration is live and the notes-tools environment has its service-role credential.

## Weekly security audit scheduling

- Date: 2026-07-12
- Decision: run `scheduled-security-audit` every Sunday at 11:00 through the existing scheduled Codex wrapper and user timer.
- Rationale: keep the security audit on the established unattended-job path while separating its system inspection from the 04:00 note-processing slot.
- Constraints: the audit runs as an ordinary user, records inaccessible privileged checks as coverage gaps, and proposes rather than applies fixes.

## Scheduled Codex execution boundary

- The shared wrapper always runs scheduled skills from `~/notes`; each skill owns its own file selection and mutation policy, while systemd owns wakeups and the wrapper owns cadence.
- Non-interactive jobs use `exec` sessions so conversation-processing jobs cannot ingest their own automation threads. Interactive jobs keep the Codex TUI as the foreground terminal process.
- Importers record changed message-note paths before reply drafting. Drafting runs only when the unified message pull actually changed message notes.
- Codex state-schema and Herdr lifecycle failures remain explicit; the scheduler must not silently switch execution paths.
