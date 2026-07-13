# Zettelkasten Indexer

This script helps manage a Zettelkasten note collection by performing several key operations.

## Features

1.  **Load Configuration**: Reads the path to your Zettelkasten note collection from a `config.json` file.
2.  **List Markdown Files**: Identifies all `.md` files within the specified Zettelkasten directory.
3.  **Wikilink Extraction**: Parses markdown files to find and extract `[[wikilinks]]`.
4.  **Note Normalization**: Normalizes note names, typically by removing the `.md` extension, to ensure consistent referencing.
5.  **Identify Unindexed Notes**: Compares the set of all notes with those linked from any `*index.md` files. Notes that are not linked from an index file are listed in `unindexed.md` (itself excluded from this list).
6.  **Tag Index Files**: Ensures that all files ending with `index.md` are tagged with `#index`. The tag is added after any YAML frontmatter if present, or at the beginning of the file otherwise.

## How it Works

The script (`src/main.py`) orchestrates these tasks:

-   **`load_zettelkasten_path()`**: Retrieves the Zettelkasten directory path from `config.json`.
-   **`list_md_files_in_zettelkasten()`**: Scans the Zettelkasten directory for markdown files.
-   **`normalize_note_name()`**: Standardizes note filenames (e.g., `My Note.md` becomes `My Note`).
-   **`extract_wikilinks_from_content()`**: Uses regular expressions to find all wikilinks within a given markdown string.
-   **`main()`**: The main function that:
    -   Initializes logging.
    -   Loads the Zettelkasten path.
    -   Lists all markdown notes and derives a set of all unique note names.
    -   Identifies `*index.md` files.
    -   Extracts all wikilinks from these index files.
    -   Determines which notes are not referenced in any index file and writes them as wikilinks to `unindexed.md`.
    -   Checks each `*index.md` file and adds an `#index` tag if it's not already present, placing it appropriately after frontmatter or at the start of the file.

## Dependencies

-   `loguru`: For enhanced logging.
-   `pathlib`: For object-oriented filesystem paths.

## Configuration

Ensure you have a `config.json` file in the root of the project with the following structure:

```json
{
  "zettelkasten_folder_path": "/path/to/your/zettelkasten_notes"
}
```

Replace `/path/to/your/zettelkasten_notes` with the actual path to your notes directory.

## Usage

To run the script:

```bash
# Ensure you have Python installed and are in the project's root directory
# Install dependencies (if you have a requirements.txt):
# uv pip install -r requirements.txt

# Run the script:
uv run src/main.py