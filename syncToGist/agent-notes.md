## Notes filename slug migration

- Gist sync and teleport backlinks resolve readable wikilinks by slug-normalized note title, because vault filenames are hyphen slugs but Markdown links intentionally remain easy to type.

## Share marker semantics

- `#share` is a body token, not a top-of-document directive. Gist sync scans top-level note bodies for the token anywhere, removes only the token from exported gist text, and leaves `#ghp` / `#hbp` blog markers to their separate pipeline.
- Top-level note selection screens raw body text before frontmatter parsing, so malformed frontmatter in unshared notes cannot abort the whole sync. Malformed frontmatter in selected or linked notes should still fail loudly because those notes need valid metadata for gist updates.

## Notes vault boundaries

- Gist sync and teleport backlink processing must only read, create, or update top-level Markdown files directly under `~/notes`. Repo-local agent files under subdirectories can contain marker strings as literal instructions, so recursive scans can corrupt workflow files.
