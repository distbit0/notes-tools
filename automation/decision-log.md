# Decision Log

## Behavior-aware hard feedback scheduling

- Date: 2026-07-13
- Decision: run `scheduled-hard-feedback` every five days at 05:00 on phase 3, using the enabled 05:00 user timer.
- Rationale: the redesigned skill needs multiple days of completed time-allocation evidence, while its phase keeps it separate from the other five-day jobs at 05:00.
- Constraints: the skill must remain an unattended `exec` session, emit at most one decision-changing item, and no-op clearly when its ICS evidence is missing, stale, or insufficient.

## Scheduled skill cadence revision

- Date: 2026-07-13
- Decision: run tweet ideas every three days at 04:00; idea-space search and note critique every five days at 05:00 on separate phases; the security audit every three weeks on Sunday at 11:00; and assistant-chat distillation every two days at 16:00.
- Rationale: the requested three-day tweet cadence and five-day idea cadences must eventually coincide if they share 04:00. Moving the two five-day jobs to a dedicated 05:00 slot preserves the one-Codex-job-per-slot invariant without changing their requested frequencies.
- Reliability: cadence phase is derived from the scheduled slot, not the wall-clock replay time, so a persistent timer catch-up after the slot cannot incorrectly skip a due job. This fixes the missed tweet-ideas catch-up observed on 2026-07-08.

## Scheduled Infolio relevance preparation

- Date: 2026-07-12
- Decision: run the Infolio relevance job every three days at 21:00 and prepare its article selection before starting Codex, so the five selected Lineate URLs appear in the initial prompt.
- Rationale: all 04:00 cadence phases overlap an existing six-day job, while the scheduler intentionally limits each time slot to one Codex job. Preselection makes duplicate exclusion deterministic and prevents Codex from changing the sampled batch.
- Constraints: keep the 21:00 timer disabled until Infolio's `documents.lineate_url` migration is live and the notes-tools environment has its service-role credential.

## Weekly assistant-chat distillation scheduling

- Date: 2026-07-12
- Decision: run `scheduled-distill-assistant-chats` every Sunday at 16:00 through the existing scheduled Codex wrapper and user timer.
- Rationale: keep assistant-chat processing on the established unattended path while separating it from the 04:00 note-generation jobs and the 11:00 security audit.
- Constraints: run as a non-interactive `exec` session so the distillation job does not ingest its own automation thread.

## Weekly security audit scheduling

- Date: 2026-07-12
- Decision: run `scheduled-security-audit` every Sunday at 11:00 through the existing scheduled Codex wrapper and user timer.
- Rationale: keep the security audit on the established unattended-job path while separating its system inspection from the 04:00 note-processing slot.
- Constraints: the audit runs as an ordinary user, records inaccessible privileged checks as coverage gaps, and proposes rather than applies fixes.
