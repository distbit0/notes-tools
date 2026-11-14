"""Keep report notes current by patching them from diffs of their source notes.

A report note declares, in frontmatter, a description of what it should contain,
the source notes it is derived from, and the notes-repo commit it last reflected.
Each run diffs every source from that commit to HEAD and asks an LLM for
find/replace patches; irrelevant changes may produce no patches at all.
"""

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger
from openai import OpenAI

from integrate_notes import (
    DEFAULT_MODEL,
    DEFAULT_NOTES_ROOT,
    DEFAULT_REASONING,
    MAX_PATCH_ATTEMPTS,
    SCRATCHPAD_HEADING,
    PatchFailure,
    PatchInstruction,
    configure_logging,
    create_openrouter_client,
    execute_with_retry,
    set_frontmatter_scalar_field,
    split_frontmatter,
    strip_json_code_fence,
    try_apply_patch,
)

DESCRIPTION_FIELD = "report_description"
SOURCES_FIELD = "report_sources"
BASELINE_FIELD = "report_sources_commit"
# Git's empty tree: diffing from it presents a source as entirely added, which
# bootstraps a new report (or a newly added source) through the normal path.
GIT_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
CHECKPOINT_COMMIT_MESSAGE = "chore: checkpoint report sources"
DIFF_CONTEXT_LINES = 10
REPORT_REQUEST_TIMEOUT_SECONDS = 600.0
MAX_PARALLEL_REPORTS = 20
WHOLE_NOTE_WIKILINK_PATTERN = re.compile(r"^\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]$")
LOG_FILE_PATH = Path(__file__).resolve().parent / "logs" / "update_reports.log"

REPORT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "replace": {"type": "string"},
                },
                "required": ["search", "replace"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "patches"],
    "additionalProperties": False,
}
REPORT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "report_update_response",
    "strict": True,
    "schema": REPORT_RESPONSE_SCHEMA,
}
REPORT_RULES = """- Judge every source change against the report description. Ignore changes that are irrelevant to the report's purpose. When nothing relevant changed, return an empty patches array.
- When relevant source content is added, add it to the report where it fits. When relevant source content is removed, remove what the report derived from it. When relevant source content is edited, update the report to match.
- Text deleted in one place and re-added unchanged elsewhere has only moved; that is not a content change. Do not add, remove, or duplicate report content because of a move, unless the description makes the source location meaningful.
- Use only information present in the sources. Do not invent points, examples, or explanations.
- Do not change report content that is unrelated to the source changes, except for minimal edits needed to keep the report coherent.
- Follow the description for scope, ordering, and writing style.
- Preserve wikilinks and URLs exactly, and leave tag lines such as #share unchanged.
- Use markdown headings for sections, "- " as the bullet prefix, and four spaces per bullet nesting level, unless the report already uses a different convention."""


@dataclass(frozen=True)
class Report:
    path: Path
    description: str
    source_paths: tuple[Path, ...]
    baseline_commit: str | None


class ReportPayloadError(ValueError):
    pass


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update report notes from diffs of their source notes."
    )
    parser.add_argument(
        "--notes-root",
        default=str(DEFAULT_NOTES_ROOT),
        help="Notes vault root (a git repository) to scan for report notes.",
    )
    parser.add_argument(
        "--report",
        help="Update only this report note instead of every report under the notes root.",
    )
    return parser.parse_args()


def run_git(repo_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def list_note_paths(notes_root: Path) -> list[Path]:
    return [
        path
        for path in sorted(notes_root.rglob("*.md"))
        if not any(part.startswith(".") for part in path.relative_to(notes_root).parts)
    ]


def resolve_wikilink(link: str, notes_by_stem: dict[str, list[Path]]) -> Path:
    match = WHOLE_NOTE_WIKILINK_PATTERN.match(link.strip())
    if match is None:
        raise ValueError(f"{SOURCES_FIELD} entry {link!r} must be a [[wikilink]] to a whole note.")
    candidates = notes_by_stem.get(match.group(1).strip(), [])
    if len(candidates) != 1:
        raise ValueError(
            f"{SOURCES_FIELD} entry {link!r} matched {len(candidates)} notes; expected exactly one."
        )
    return candidates[0]


def parse_report(path: Path, notes_by_stem: dict[str, list[Path]]) -> Report | None:
    content = path.read_text(encoding="utf-8")
    if f"\n{SOURCES_FIELD}:" not in content:
        return None
    frontmatter_parts = split_frontmatter(content)
    if frontmatter_parts is None:
        return None
    metadata = yaml.safe_load("\n".join(frontmatter_parts[0])) or {}
    if SOURCES_FIELD not in metadata:
        return None

    description = metadata.get(DESCRIPTION_FIELD)
    sources = metadata[SOURCES_FIELD]
    baseline_commit = metadata.get(BASELINE_FIELD)
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{path} must define a non-empty {DESCRIPTION_FIELD}.")
    if not isinstance(sources, list) or not sources or not all(isinstance(link, str) for link in sources):
        raise ValueError(f"{path} must list {SOURCES_FIELD} as a non-empty list of [[wikilinks]].")
    if baseline_commit is not None and not isinstance(baseline_commit, str):
        raise ValueError(f"{path} has a non-string {BASELINE_FIELD}.")
    source_paths = tuple(resolve_wikilink(link, notes_by_stem) for link in sources)
    if path in source_paths:
        raise ValueError(f"{path} lists itself as a source.")
    return Report(path, description.strip(), source_paths, baseline_commit)


def load_reports(notes_root: Path, only_report: Path | None) -> list[Report]:
    note_paths = list_note_paths(notes_root)
    notes_by_stem: dict[str, list[Path]] = {}
    for note_path in note_paths:
        notes_by_stem.setdefault(note_path.stem, []).append(note_path)

    if only_report is not None:
        report = parse_report(only_report, notes_by_stem)
        if report is None:
            raise ValueError(f"{only_report} has no {SOURCES_FIELD} frontmatter.")
        return [report]
    reports = [parse_report(path, notes_by_stem) for path in note_paths]
    return [report for report in reports if report is not None]


def checkpoint_sources(repo_root: Path, source_paths: list[Path]) -> str:
    relative_paths = sorted({str(path.relative_to(repo_root)) for path in source_paths})
    if run_git(repo_root, "status", "--porcelain", "--", *relative_paths).strip():
        run_git(repo_root, "add", "--", *relative_paths)
        run_git(repo_root, "commit", "-m", CHECKPOINT_COMMIT_MESSAGE, "--", *relative_paths)
        logger.info(f"Committed report sources: {', '.join(relative_paths)}.")
    return run_git(repo_root, "rev-parse", "HEAD").strip()


def source_diff(repo_root: Path, baseline: str, head_commit: str, source_path: Path) -> str:
    return run_git(
        repo_root,
        "diff",
        "--no-ext-diff",
        "--no-color",
        f"--unified={DIFF_CONTEXT_LINES}",
        baseline,
        head_commit,
        "--",
        str(source_path.relative_to(repo_root)),
    )


def build_report_prompt(
    description: str,
    report_body: str,
    source_diffs: dict[str, str],
    full_sources: bool,
    previous_response: str | None = None,
    previous_error: str | None = None,
) -> str:
    full_sources_note = (
        "\nThe report has no recorded baseline, so the diffs show the full sources as added lines. Build or rebuild the "
        "report so that it fully satisfies its description, keeping existing report content that already does."
        if full_sources
        else ""
    )
    diff_sections = "\n".join(
        f'<source_diff note="[[{note_name}]]">\n{diff_text}\n</source_diff>'
        for note_name, diff_text in source_diffs.items()
    )
    response_directive = (
        "Return exactly one JSON object matching the schema below. "
        "summary briefly states which source changes you judged relevant and what you changed, or why nothing changed. "
        "Each patch's search must be an exact, contiguous span copied from the current report body that occurs exactly once; "
        "replace is the complete replacement text. For insertions, include the anchor text in both search and replace. "
        "Use an empty search only to append text at the end of the report. "
        "Patches are applied in order, so each search must match the report as modified by the preceding patches. "
        "Do not add commentary or markdown fences outside the JSON object.\n"
        f"<json_schema>\n{json.dumps(REPORT_RESPONSE_SCHEMA, indent=2)}\n</json_schema>"
    )
    sections = [
        "<task>\nYou maintain a report note derived from source notes. Below are the report's description, its current body, "
        "and diffs of its source notes since the report was last updated. Decide which source changes, if any, matter for this "
        f"report and emit patches that update the report accordingly.{full_sources_note}\n</task>",
        f"<rules>\n{REPORT_RULES}\n</rules>",
        f"<report_description>\n{description}\n</report_description>",
        f"<current_report_body>\n{report_body}\n</current_report_body>",
        f"<source_diffs>\n{diff_sections}\n</source_diffs>",
        f"<response_directive>\n{response_directive}\n</response_directive>",
    ]
    if previous_error is not None:
        sections.append(
            "<previous_attempt_feedback>\nYour previous response could not be applied. Fix the problem below and "
            f"re-emit the complete JSON response.\n{previous_error}\n</previous_attempt_feedback>\n"
            f"<previous_json_response>\n{previous_response}\n</previous_json_response>"
        )
    return "\n\n\n".join(sections)


def request_report_patches(client: OpenAI, prompt: str, context_label: str) -> str:
    def perform_request() -> str:
        response = client.responses.create(
            model=DEFAULT_MODEL,
            reasoning=DEFAULT_REASONING,
            input=prompt,
            text={"format": REPORT_RESPONSE_FORMAT},
            timeout=REPORT_REQUEST_TIMEOUT_SECONDS,
        )
        if getattr(response, "error", None):
            raise RuntimeError(f"OpenRouter error for {context_label}: {response.error}")
        if not response.output_text.strip():
            raise RuntimeError(f"Received empty response for {context_label}.")
        return response.output_text.strip()

    return execute_with_retry(perform_request, f"report update {context_label}")


def parse_report_payload(response_text: str) -> tuple[str, list[PatchInstruction]]:
    try:
        payload = json.loads(strip_json_code_fence(response_text))
    except json.JSONDecodeError as error:
        raise ReportPayloadError(f"Response is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ReportPayloadError("Response must be a JSON object.")
    summary = payload.get("summary")
    patches = payload.get("patches")
    if not isinstance(summary, str) or not summary.strip():
        raise ReportPayloadError("summary must be a non-empty string.")
    if not isinstance(patches, list):
        raise ReportPayloadError("patches must be an array.")

    instructions: list[PatchInstruction] = []
    for index, patch in enumerate(patches, start=1):
        if not isinstance(patch, dict):
            raise ReportPayloadError(f"Patch {index} must be an object.")
        search_text = patch.get("search")
        replace_text = patch.get("replace")
        if not isinstance(search_text, str) or not isinstance(replace_text, str):
            raise ReportPayloadError(f"Patch {index} must have string search and replace fields.")
        if not search_text.strip() and not replace_text.strip():
            raise ReportPayloadError(f"Patch {index} appends nothing: search and replace are both empty.")
        instructions.append(PatchInstruction(search_text=search_text, replace_text=replace_text))
    return summary.strip(), instructions


def apply_report_patches(
    report_body: str, patches: list[PatchInstruction]
) -> tuple[str, PatchFailure | None]:
    candidate_body = report_body
    for index, patch in enumerate(patches, start=1):
        if not patch.search_text.strip():
            appended_text = patch.replace_text.strip("\n")
            candidate_body = (
                f"{candidate_body.rstrip()}\n\n{appended_text}" if candidate_body.strip() else appended_text
            )
            continue
        applied, updated_body, reason = try_apply_patch(candidate_body, patch)
        if not applied:
            return report_body, PatchFailure(index=index, search_text=patch.search_text, reason=reason)
        candidate_body = updated_body
    return candidate_body, None


def patch_report_body(
    client: OpenAI,
    description: str,
    report_body: str,
    source_diffs: dict[str, str],
    full_sources: bool,
    context_label: str,
) -> tuple[str, str, int]:
    previous_response: str | None = None
    previous_error: str | None = None
    for attempt in range(1, MAX_PATCH_ATTEMPTS + 1):
        attempt_label = f"{context_label} attempt {attempt}"
        prompt = build_report_prompt(
            description, report_body, source_diffs, full_sources, previous_response, previous_error
        )
        response_text = request_report_patches(client, prompt, attempt_label)
        logger.debug(f"Raw report response for {attempt_label}:\n{response_text}")
        previous_response = response_text
        try:
            summary, patches = parse_report_payload(response_text)
        except ReportPayloadError as error:
            previous_error = str(error)
            logger.warning(f"Invalid report response for {attempt_label}: {error}")
            continue
        updated_body, failure = apply_report_patches(report_body, patches)
        if failure is None:
            return updated_body, summary, len(patches)
        previous_error = (
            f"Patch {failure.index} failed: {failure.reason}\nSEARCH text:\n{failure.search_text}"
        )
        logger.warning(f"Patch {failure.index} failed for {attempt_label}: {failure.reason}")
    raise RuntimeError(
        f"Unable to produce applicable report patches for {context_label} after {MAX_PATCH_ATTEMPTS} attempt(s)."
    )


def update_report(client: OpenAI, repo_root: Path, head_commit: str, report: Report) -> None:
    baseline = report.baseline_commit or GIT_EMPTY_TREE
    source_diffs = {
        source_path.stem: source_diff(repo_root, baseline, head_commit, source_path)
        for source_path in report.source_paths
    }
    changed_source_diffs = {name: diff for name, diff in source_diffs.items() if diff.strip()}
    if not changed_source_diffs:
        logger.info(f"{report.path.name}: no source changes since {baseline[:12]}; skipped.")
        return

    original_content = report.path.read_text(encoding="utf-8")
    metadata_lines, body = split_frontmatter(original_content)
    # Shared notes get a scratchpad heading appended by the gist sync; everything
    # from it onward stays outside the model's view and is preserved verbatim.
    report_body, scratchpad_heading, scratchpad_text = body.partition(SCRATCHPAD_HEADING)
    logger.info(
        f"{report.path.name}: requesting patches for changes in {', '.join(changed_source_diffs)} "
        f"since {baseline[:12]}."
    )
    updated_body, summary, patch_count = patch_report_body(
        client,
        report.description,
        report_body.strip("\n"),
        changed_source_diffs,
        report.baseline_commit is None,
        report.path.name,
    )
    if report.path.read_text(encoding="utf-8") != original_content:
        raise RuntimeError(
            f"{report.path} changed while its update was running; left untouched so the next run retries."
        )
    updated_content = "---\n" + "\n".join(metadata_lines) + "\n---\n\n" + updated_body.strip("\n") + "\n"
    if scratchpad_heading:
        updated_content += f"\n{scratchpad_heading}{scratchpad_text.rstrip()}\n"
    report.path.write_text(
        set_frontmatter_scalar_field(updated_content, BASELINE_FIELD, head_commit),
        encoding="utf-8",
    )
    logger.info(f"{report.path.name}: applied {patch_count} patch(es) at {head_commit[:12]}. {summary}")


def update_reports(notes_root: Path, only_report: Path | None) -> list[Path]:
    reports = load_reports(notes_root, only_report)
    if not reports:
        logger.info(f"No report notes found under {notes_root}.")
        return []
    repo_root = Path(run_git(notes_root, "rev-parse", "--show-toplevel").strip())
    head_commit = checkpoint_sources(
        repo_root, [source for report in reports for source in report.source_paths]
    )
    client = create_openrouter_client()

    failed_reports: list[Path] = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REPORTS) as executor:
        futures = {
            executor.submit(update_report, client, repo_root, head_commit, report): report
            for report in reports
        }
        for future in as_completed(futures):
            report = futures[future]
            try:
                future.result()
            except Exception as error:
                logger.exception(f"Updating {report.path} failed: {error}")
                failed_reports.append(report.path)
    return failed_reports


def main() -> None:
    configure_logging(LOG_FILE_PATH)
    try:
        args = parse_arguments()
        notes_root = Path(args.notes_root).expanduser().resolve()
        only_report = Path(args.report).expanduser().resolve() if args.report else None
        failed_reports = update_reports(notes_root, only_report)
    except Exception as error:
        logger.exception(f"Report update failed: {error}")
        sys.exit(1)
    if failed_reports:
        logger.error(f"{len(failed_reports)} report(s) failed: {', '.join(map(str, failed_reports))}")
        sys.exit(1)
    logger.info("Report update completed.")


if __name__ == "__main__":
    main()
