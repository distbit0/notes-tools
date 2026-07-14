# Decision Log

## Runtime and key attribution

- Stay on Python 3.13 until the locked Pydantic/PyO3 dependency line supports Python 3.14.
- The project `.env` OpenRouter key overrides inherited shell values so usage remains attributable to Integrate Notes.

## Continuous note organization

- Continuous processing is opt-in through `organise: continuous` frontmatter and uses `grouping: |` as its instruction boundary.
- Scheduled mode must remain non-interactive. A missing grouping value is written explicitly with a warning rather than held as hidden state.
- Model patches use locally validated structured JSON. Malformed patch structure is rejected before note writes; a markdown fence may be unwrapped only with a warning before normal schema validation.
