# Decision Log

## Runtime and key attribution

- Stay on Python 3.13 until the locked Pydantic/PyO3 dependency line supports Python 3.14.
- The project `.env` OpenRouter key overrides inherited shell values so usage remains attributable to Integrate Notes.

## Continuous note organization

- Continuous processing is opt-in through `organise: continuous` frontmatter and uses `grouping: |` as its instruction boundary.
- Scheduled mode must remain non-interactive. A missing grouping value is written explicitly with a warning rather than held as hidden state.
- Model patches use locally validated structured JSON. Malformed patch structure is rejected before note writes; a markdown fence may be unwrapped only with a warning before normal schema validation.
- Every non-empty scratchpad chunk must produce at least one locally validated patch or exact duplication proof. An empty evidence response is invalid and must retry.
- Verification is a blocking safety gate before scratchpad removal. A structured missing assessment gets exactly one fresh integration attempt with the identified omissions; if the retry is still incomplete, fail with the scratchpad unchanged.

## Report notes

- A report declares its own sources and description in frontmatter, so a report is self-contained and source notes are never edited for report bookkeeping.
- The per-report baseline is a notes-repo commit stored visibly in the report's frontmatter (`report_sources_commit`), not hidden local state. Sources are checkpoint-committed (only their paths) before diffing, so the baseline always names exactly what the model saw.
- A missing baseline means git's empty tree. Bootstrapping a new report and adding a source to an existing one then go through the same diff→patch path, and an empty-search patch appends.
- The gist sync appends `# -- SCRATCHPAD` to every `#share` note that lacks it, and teleported backlinks are appended to the end of a note. A report therefore hides everything from that heading onward from the model and keeps it verbatim; otherwise appended patches would land after it and be left out of the gist.
- There is no completeness-verification gate, unlike scratchpad integration. Relevance to a description is a judgment call, and sources are never modified, so a bad report edit loses nothing. A real run over a pure line-reorganisation diff produced zero patches, which is the behaviour that matters most.
- Reports run every half hour (user request), independently of the daily 09:07 scratchpad integration. A report can therefore pick up raw scratchpad lines before they are integrated; the later reorganisation diff is expected to produce zero patches.
