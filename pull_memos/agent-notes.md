# Pull Temp Notes

## Repo split

- This repo was split out of `~/dev/manageTodoList` so memo ingestion can run independently of the broader todo tooling. `pullTempNotes.py` now reads `config.json` directly instead of importing `utils.general`, because that old utility module also contained unrelated todo-list behavior.

## Keep note commit boundary

- Lineate-backed Keep URL notes keep an explicit commit boundary: `run_lineate_for_urls` must succeed before those source notes are trashed/synced or their deferred temp-note text is written locally. Browser-routed URL notes additionally require `append_opened_urls` to succeed before commit.
- Overlapping cron runs are prevented with a non-blocking file lock instead of early `keep.sync()`. That preserves the old "do not re-fetch while another run is still active" property without committing URL-backed Keep state before its side effects finish.
- Infolio-routed Keep URL notes stay inside the Lineate commit boundary: `pullTempNotes.py` only trashes/syncs the source note after Lineate accepts the URL action with `--output-dest infolio`.
- Keep URL routing invokes `~/dev/lineate/run.sh` rather than generic `uvrun.sh` so Infolio-bound runs get Lineate's Infolio-specific environment loading and reachable ingest endpoint.

## Keep staged temp-note writes

- Plain Keep text now commits before any Lineate-backed URL conversion runs. The intent is to stop long or stuck Lineate jobs from blocking ordinary Keep notes from landing in `inbox-index.md`.
- This intentionally narrows the old single commit boundary: non-Lineate Keep notes are written, trashed, and synced in an early batch, while URL-only Keep notes that depend on Lineate still keep their own later boundary around conversion plus opened-URL logging.
- The trade-off is explicit: a later Lineate failure can no longer block earlier plain-note ingestion, but plain-note and URL-note commits from the same Keep sweep are no longer all-or-nothing together.
- Keep notes whose title or body starts with `qq ` route to `writing-ideas-index.md` before URL-only routing. This keeps question/drafting captures out of the temp scratchpad even when the note content is just a URL.

## MP3 temp-note cleanup boundary

- Processed audio files now commit on the same boundary as their temp-note text: once a transcription has been written to `inbox-index.md`, the source file is immediately renamed into the trash before any Keep URL/Lineate work starts.
- The goal is operational, not cosmetic: the capture folder should reflect only audio that has not been written into temp notes yet, so it stays easy to map trashed audio files back to the notes that already landed.

## Cosimo Substack failure debugging

- The April 18 Cosimo failures attributed to `pullTempNotes.py` were actually downstream Lineate extraction issues. The generated `lineate/data/summary_inputs/*cosimoresearch*` artifacts for the failed `open.substack.com` URLs contained only a markdown heading with the canonical URL, which means article extraction produced an empty shell before any summary/highlights call.
- Because Lineate still passed that shell through title/highlights/summary generation, some pages failed later on malformed summary output while others "succeeded" and cached hallucinated missing-content boilerplate. The root bug is therefore in Lineate's extraction/validation path, not in `pullTempNotes.py`'s URL routing.

## Keep URL-only Infolio routing

- Infolio routing now uses a leading `ii ` prefix instead of the old trailing-period suffix. It remains URL-only scoped: `ii https://...` routes to Lineate with `--output-dest infolio`, while mixed `ii ...` notes still land in `inbox-index.md`.

## Keep friends routing

- Keep notes whose title or body starts with `ff ` route to `friends-index.md` before URL-only routing. Writing, friends, and Infolio markers all use the same two-letter-prefix parser.

## Notes filename slug migration

- The notes vault now uses hyphen-slug filenames while keeping readable wikilinks. Config paths that reference concrete note files must use filenames such as `inbox-index.md` and `opened-urls.md`.
- Writes to Markdown note targets now fail fast if the filename contains whitespace. The goal is to catch stale config/process state instead of recreating old spaced files after the migration.
- URL extraction strips trailing sentence punctuation before routing so casual sentence punctuation is not sent as part of the URL payload.
- The old trailing-period Infolio marker used to send URL-only notes through `clipboardToPhone/send.py`; that path was removed from `pullTempNotes.py` once the marker became an Infolio ingestion signal, and the marker itself has since been replaced by the leading `ii ` prefix.

## Keep text-fragment URLs

- Keep body lines that begin with `http` and contain `#:~:text=` are dropped before any Keep-note URL routing or markdown formatting. These are browser text-fragment URLs and should not be written into temp notes or treated as URL payloads.

## Keep URL conversion retry limit

- URL-only Keep notes that depend on lineate now persist a per-note failure count in `logs/keep_url_retry_counts.json`. After three conversion failures, `pullTempNotes.py` stops retrying, writes the raw Keep note text into the temp notes file, and trashes the source note so it cannot loop forever.

## Keep network hangs

- Google Keep auth/sync is now bounded by a hard 120s alarm in `keep_auth.py`. The concrete failure this addresses was a `pullTempNotes.py` process that stayed stuck overnight in an SSL socket read during Keep auth, which kept the file lock open and blocked every later cron/manual run from importing new notes.
