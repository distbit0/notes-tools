import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import integrate_notes  # noqa: E402
import update_reports  # noqa: E402

# Real notes, report states and captured model responses; private, so gitignored.
PRIVATE_DATA_DIR = Path(__file__).with_name("private_test_data")
SOURCE_NAME = "distilled-social-index.md"
SOURCE_BEFORE_REORG = PRIVATE_DATA_DIR / "distilled-social-index.before-reorg.md"
SOURCE_AFTER_REORG = PRIVATE_DATA_DIR / "distilled-social-index.after-reorg.md"
REPORT_WITH_BASELINE = PRIVATE_DATA_DIR / "pre-date-read.md"
SHARED_REPORT = PRIVATE_DATA_DIR / "pre-cold-approach-read.md"
CAPTURED_UPDATES = sorted(PRIVATE_DATA_DIR.glob("captured-update-*.json"))
CAPTURED_INTEGRATION_UPDATE = PRIVATE_DATA_DIR / "captured-update-cold-approach-integration.json"


def init_notes_repo(repo_root: Path) -> None:
    for arguments in (
        ["init", "-q"],
        ["config", "user.name", "update-reports-test"],
        ["config", "user.email", "update-reports-test@localhost"],
        ["config", "core.hooksPath", "/dev/null"],
    ):
        subprocess.run(["git", "-C", str(repo_root), *arguments], check=True)


def commit_all(repo_root: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", message], check=True)
    return update_reports.run_git(repo_root, "rev-parse", "HEAD").strip()


def test_load_reports_resolves_real_report_config(tmp_path: Path) -> None:
    shutil.copy(SOURCE_AFTER_REORG, tmp_path / SOURCE_NAME)
    shutil.copy(REPORT_WITH_BASELINE, tmp_path / REPORT_WITH_BASELINE.name)
    frontmatter_lines, _ = integrate_notes.split_frontmatter(REPORT_WITH_BASELINE.read_text())
    expected_metadata = yaml.safe_load("\n".join(frontmatter_lines))

    reports = update_reports.load_reports(tmp_path, None)

    assert reports == [
        update_reports.Report(
            path=tmp_path / REPORT_WITH_BASELINE.name,
            description=expected_metadata["report_description"].strip(),
            source_paths=(tmp_path / SOURCE_NAME,),
            baseline_commit=expected_metadata["report_sources_commit"],
        )
    ]


def test_checkpoint_commits_only_sources_and_diffs_follow_baseline(tmp_path: Path) -> None:
    init_notes_repo(tmp_path)
    source_path = tmp_path / SOURCE_NAME
    source_path.write_text(SOURCE_BEFORE_REORG.read_text())
    before_commit = commit_all(tmp_path, "source before reorganisation")
    source_path.write_text(SOURCE_AFTER_REORG.read_text())
    unrelated_path = tmp_path / REPORT_WITH_BASELINE.name
    unrelated_path.write_text(REPORT_WITH_BASELINE.read_text())

    after_commit = update_reports.checkpoint_sources(tmp_path, [source_path])

    assert after_commit != before_commit
    committed_paths = update_reports.run_git(
        tmp_path, "show", "--name-only", "--format=", after_commit
    ).split()
    assert committed_paths == [SOURCE_NAME]
    assert update_reports.run_git(tmp_path, "status", "--porcelain").strip() == (
        f"?? {REPORT_WITH_BASELINE.name}"
    )
    assert update_reports.checkpoint_sources(tmp_path, [source_path]) == after_commit

    bootstrap_diff = update_reports.source_diff(
        tmp_path, update_reports.GIT_EMPTY_TREE, after_commit, source_path
    )
    added_lines = [
        line[1:] for line in bootstrap_diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    ]
    assert added_lines == SOURCE_AFTER_REORG.read_text().splitlines()

    reorganisation_diff = update_reports.source_diff(tmp_path, before_commit, after_commit, source_path)
    assert reorganisation_diff.strip()
    assert update_reports.source_diff(tmp_path, after_commit, after_commit, source_path) == ""


def test_update_report_skips_model_when_sources_are_unchanged(tmp_path: Path) -> None:
    init_notes_repo(tmp_path)
    source_path = tmp_path / SOURCE_NAME
    source_path.write_text(SOURCE_AFTER_REORG.read_text())
    head_commit = commit_all(tmp_path, "source")
    report_path = tmp_path / REPORT_WITH_BASELINE.name
    report_content = integrate_notes.set_frontmatter_scalar_field(
        REPORT_WITH_BASELINE.read_text(), update_reports.BASELINE_FIELD, head_commit
    )
    report_path.write_text(report_content)
    [report] = update_reports.load_reports(tmp_path, report_path)

    update_reports.update_report(None, tmp_path, head_commit, report)

    assert report_path.read_text() == report_content


def test_update_report_patches_only_the_shared_body_before_the_scratchpad(
    tmp_path: Path, monkeypatch
) -> None:
    captured = json.loads(CAPTURED_INTEGRATION_UPDATE.read_text())
    init_notes_repo(tmp_path)
    source_path = tmp_path / SOURCE_NAME
    source_path.write_text(SOURCE_BEFORE_REORG.read_text())
    baseline_commit = commit_all(tmp_path, "source before reorganisation")
    source_path.write_text(SOURCE_AFTER_REORG.read_text())
    head_commit = commit_all(tmp_path, "source after reorganisation")
    metadata_lines, _ = integrate_notes.split_frontmatter(SHARED_REPORT.read_text())

    def shared_report_content(body: str, commit: str) -> str:
        content = (
            "---\n" + "\n".join(metadata_lines) + "\n---\n\n#share\n\n" + body
            + f"\n\n{integrate_notes.SCRATCHPAD_HEADING}\n"
        )
        return integrate_notes.set_frontmatter_scalar_field(content, update_reports.BASELINE_FIELD, commit)

    report_path = tmp_path / SHARED_REPORT.name
    report_path.write_text(shared_report_content(captured["body_before"], baseline_commit))
    [report] = update_reports.load_reports(tmp_path, report_path)
    prompts = []

    def request_report_patches(_client, prompt, _label):
        prompts.append(prompt)
        return captured["response"]

    monkeypatch.setattr(update_reports, "request_report_patches", request_report_patches)

    update_reports.update_report(None, tmp_path, head_commit, report)

    assert report_path.read_text() == shared_report_content(captured["body_after"], head_commit)
    [prompt] = prompts
    report_body_in_prompt = prompt.split("<current_report_body>")[1].split("</current_report_body>")[0]
    assert integrate_notes.SCRATCHPAD_HEADING not in report_body_in_prompt
    assert report_body_in_prompt.strip().startswith("#share")


def test_set_frontmatter_scalar_field_preserves_real_report_body() -> None:
    report_content = REPORT_WITH_BASELINE.read_text()
    _, original_body = integrate_notes.split_frontmatter(report_content)

    updated_content = integrate_notes.set_frontmatter_scalar_field(
        report_content, update_reports.BASELINE_FIELD, "0" * 40
    )

    assert integrate_notes.read_frontmatter_field(updated_content, update_reports.BASELINE_FIELD) == "0" * 40
    assert updated_content.count(f"{update_reports.BASELINE_FIELD}:") == 1
    assert integrate_notes.split_frontmatter(updated_content)[1] == original_body


def test_captured_model_updates_reproduce_written_reports() -> None:
    assert CAPTURED_UPDATES, f"No captured updates in {PRIVATE_DATA_DIR}"
    for captured_path in CAPTURED_UPDATES:
        captured = json.loads(captured_path.read_text())

        _, patches = update_reports.parse_report_payload(captured["response"])
        updated_body, failure = update_reports.apply_report_patches(captured["body_before"], patches)

        assert failure is None, captured_path.name
        assert updated_body == captured["body_after"], captured_path.name
