# Opened URL history

`append_opened_urls` removes credential query parameters before recording a URL
in the notes vault. Lineate still receives the original URL for processing.
Other URL components and query parameters are preserved. Signed statement links
in the history require a fresh authenticated link to reopen the document.

The statement URL regression fixture was captured from the September 27, 2026
auto-commit failure, with token values redacted.
