# Decision Log

## Share only top-level note content

- `#share` is a body marker that can appear anywhere in a top-level note. Export removes only that marker; blog markers remain owned by their separate pipeline.
- Gist and teleport processing is limited to top-level Markdown files in `~/notes`. Nested repository and workflow files are outside the publication boundary.
- Screen raw body markers before parsing frontmatter so malformed unshared notes cannot block the run. Selected or linked notes still require valid metadata and fail visibly when malformed.
- Resolve readable wikilinks against slugged filenames rather than requiring link text to match the on-disk stem exactly.
