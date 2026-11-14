# integrate_notes

## Scratchpad integration

`src/integrate_notes.py --continuous --notes-root <vault>` integrates the `# -- SCRATCHPAD` section of every note marked `organise: continuous` into that note's body, following its `grouping` frontmatter.

## Report notes

`src/update_reports.py --notes-root <vault> [--report <path>]` keeps report notes current. A report is any note whose frontmatter has `report_sources`:

```yaml
---
report_description: |
  What the report should contain, its ordering, and its writing style.
report_sources:
  - "[[source-note]]"
report_sources_commit: <commit the report last reflected; written by the script>
---
```

Each run commits any uncommitted changes to the sources (only those paths), then diffs every source from the report's `report_sources_commit` to `HEAD`. The model receives the description, the current report body and those diffs, and returns find/replace patches. A report with no source changes is skipped without a model call. Changes that are irrelevant to the description can produce zero patches, and the baseline still advances.

To create a report, write the frontmatter without `report_sources_commit`. The first run diffs from git's empty tree, so the whole source counts as new and the model builds the report from it. Adding a source to an existing report works the same way. The notes vault must be a git repository, and sources must be whole-note `[[wikilinks]]` that resolve to exactly one note.

Run with `uv run --env-file .env python src/update_reports.py --notes-root ~/notes`. A systemd user timer, `continuous-report-updates.timer`, runs it daily after the scratchpad integration timer.
