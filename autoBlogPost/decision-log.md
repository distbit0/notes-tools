# Decision Log

## Publishing boundary

- Discover blog posts only from top-level Markdown files in `~/notes`; nested repository and workflow files are outside the content boundary.
- Treat the configured `_posts` directory as a destructive mirror of the currently selected source notes: every run deletes files there that it did not regenerate. Manually maintained website pages must stay outside `_posts`, and a generated post disappears when its top-level source is removed or loses its publication marker.
- Resolve readable wikilinks against slugged filenames, keeping generated URLs slugged and display titles readable.
- Never infer or convert TeX from dollar signs because ordinary currency and token text is common. Canonical sources use Markdown-escaped `\\(...\\)` for inline math and `\\[...\\]` for display math; Kramdown emits the single-backslash delimiters consumed by MathJax.
- A blog-copy run fails when its Git auto-commit/push step fails; publishing success must not hide repository delivery failure.
