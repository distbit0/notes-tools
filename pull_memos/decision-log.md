# Decision Log

## Commit effects at their real dependency boundary

- Plain Keep text and processed audio commit early so a slow URL conversion cannot block ordinary capture. Processed audio is trashed in the same commit boundary as its written note.
- URL-only Keep notes are trashed and synced only after Lineate conversion and any required opened-URL logging succeed. Infolio-bound URLs use Lineate's launcher so its destination environment and ingestion contract apply.
- Overlapping runs are prevented by a non-blocking file lock rather than an early Keep sync, which would commit source state before dependent effects finish.

## Keep notes can change while being captured

- Keep uploads a note while it is typed, so a note only commits after `keepNoteQuietPeriodSeconds` without an update. This includes blank notes, which may be new notes about to be typed, and URL-only notes, so a shared URL reaches Lineate later too.
- The gate reads `timestamps.updated`: it is never older than `edited` and never missing. The text-child `ListItem` timestamps are placeholders and cannot date an edit.
- `authenticate_keep` is the only pull, so it runs after audio. Pulling first left a 102s median and 268s maximum gap between snapshot and commit across a 15-day log, and edits in that gap were trashed unsaved.
- Edits to a note already saved and trashed are still lost, including edits during a Lineate run. Recovering them needs a persisted note-id to saved-text ledger compared against Keep's trash, which holds notes for 7 days. It was left out because 0 of 155 audited trashed notes showed `edited` after `trashed`; build it if that recurs.

## Capture routing

- Leading `qq `, `ff `, and URL-only `ii ` markers route to writing ideas, friend ideas, and Infolio respectively before ordinary inbox handling.
- Browser text-fragment URLs are discarded. URL-only Lineate work has a persisted finite retry count; exhaustion writes the real raw Keep text to notes and trashes the source instead of looping forever.
