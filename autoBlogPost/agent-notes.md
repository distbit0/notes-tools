## Notes filename slug migration

- Blog link conversion resolves readable wikilinks against hyphen-slug note filenames. Display titles should replace slug separators with spaces, while generated URLs stay slugged.

## Git auto-commit invocation

- `gitAutoPath` should call `~/dev/misc/automation/uvrun.sh ~/dev/git-auto/gitAutoCommit.py -p`, not `python3 ~/dev/git-auto/gitAutoCommit.py -p`, so cron runs inherit the wrapper PATH that includes Homebrew tools such as `git-lfs`.
- `main.py` should fail when the git auto-commit subprocess fails. A failed push must not be hidden behind a successful blog-copy run.

## MathJax delimiters

- The configured blog should not enable bare `$...$` as inline MathJax because generated posts contain many literal currency amounts and token symbols. The blog generator converts unambiguous TeX spans from `$...$` to Kramdown-safe `\\(...\\)` delimiters and display `$$...$$` spans to `\\[...\\]`; ambiguous inline math such as `$X$` should be written with explicit delimiters in the source note.

## Notes vault boundaries

- Blog post discovery must only scan top-level Markdown files directly under `~/notes`. Repo-local agent files under subdirectories can mention markers such as `#ghp` as instruction text, and must not be rewritten as blog notes.
