# Decision Log

## Publishing boundary

- Discover blog posts only from top-level Markdown files in `~/notes`; nested repository and workflow files are outside the content boundary.
- Resolve readable wikilinks against slugged filenames, keeping generated URLs slugged and display titles readable.
- Do not enable bare-dollar inline MathJax because ordinary currency and token text is common. Convert only unambiguous TeX spans and require explicit delimiters for ambiguous inline math.
- A blog-copy run fails when its Git auto-commit/push step fails; publishing success must not hide repository delivery failure.
