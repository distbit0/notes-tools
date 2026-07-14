# Decision Log

## Commit effects at their real dependency boundary

- Plain Keep text and processed audio commit early so a slow URL conversion cannot block ordinary capture. Processed audio is trashed in the same commit boundary as its written note.
- URL-only Keep notes are trashed and synced only after Lineate conversion and any required opened-URL logging succeed. Infolio-bound URLs use Lineate's launcher so its destination environment and ingestion contract apply.
- Overlapping runs are prevented by a non-blocking file lock rather than an early Keep sync, which would commit source state before dependent effects finish.

## Capture routing

- Leading `qq `, `ff `, and URL-only `ii ` markers route to writing ideas, friend ideas, and Infolio respectively before ordinary inbox handling.
- Browser text-fragment URLs are discarded. URL-only Lineate work has a persisted finite retry count; exhaustion writes the real raw Keep text to notes and trashes the source instead of looping forever.
