# Decision Log

## Commit effects at their real dependency boundary

- Plain Keep text and processed audio commit early so a slow URL conversion cannot block ordinary capture. Processed audio is trashed in the same commit boundary as its written note.
- URL-only Keep notes enter the capture ledger, which is what makes them eligible for trashing, only after Lineate conversion and any required opened-URL logging succeed. Infolio-bound URLs use Lineate's launcher so its destination environment and ingestion contract apply.
- Overlapping runs are prevented by a non-blocking file lock rather than an early Keep sync, which would commit source state before dependent effects finish.

## Keep notes can change after capture

- A note is saved by the first run that sees it, with no wait; the user chose this over delaying capture. `logs/keep_capture_ledger.json` maps note id to saved text until Keep purges the note, and every run swaps the saved text for any newer version in place, whether the note is active or trashed.
- Only trashing waits for `keepNoteQuietPeriodSeconds` without an update. The wait adds no inbox latency and avoids trashing a note under an open phone editor, whose behaviour is untested; 0 trashes on first sight. Blank notes wait too, since a new note is blank before it is typed.
- Trashed does not mean final. On real notes, Keep accepted an edit to a trashed note, and an edit from a client that had not seen the trash untrashed the note, so an active note in the ledger is not new. An edit that races a trash leaves `edited == updated == trashed`.
- If the saved text has left the file, the edit is appended rather than dropped, so an edited note can reappear after triage. An edit goes to the file its current text routes to, so adding a prefix after capture leaves the old text in the inbox. Edits never re-run Lineate.
- The quiet check reads `timestamps.updated`: it is never older than `edited` and never missing. The text-child `ListItem` timestamps are placeholders and cannot date an edit.
- `authenticate_keep` is the only pull, so it runs after audio. Pulling first left a 102s median and 268s maximum gap between snapshot and commit across a 15-day log, and edits in that gap were trashed unsaved.

## Capture routing

- Leading `qq `, `ff `, and URL-only `ii ` markers route to writing ideas, friend ideas, and Infolio respectively before ordinary inbox handling.
- Browser text-fragment URLs are discarded. URL-only Lineate work has a persisted finite retry count; exhaustion writes the real raw Keep text to notes and trashes the source instead of looping forever.
