#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
from loguru import logger
from tqdm import tqdm
import plotly.graph_objects as go


# ────────────────────────────────────────────────────────────────────────────
# Data-collection helpers
# ────────────────────────────────────────────────────────────────────────────
def get_sampled_commits(repo_path: str, sample_size: int = 200) -> list[str]:
    """
    Return `sample_size` commits in chronological order (oldest → newest).
    The very first and the HEAD commit are always included; the rest are
    randomly sampled from the remainder.

    The ordering is made deterministic by an explicit sort *after* sampling.
    """
    cmd = ["git", "log", "--pretty=format:%h %ad", "--date=short", "--reverse"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, cwd=repo_path, check=True)
    all_commits = result.stdout.strip().split("\n")

    if not all_commits or not all_commits[0]:
        return []

    if len(all_commits) <= sample_size:
        return all_commits  # already sorted by --reverse

    # Always include first & last, sample the middle, then sort.
    middle = random.sample(all_commits[1:-1], sample_size - 2)
    commits = [all_commits[0], *middle, all_commits[-1]]
    commits.sort(key=lambda line: line.split()[1])  # sort by YYYY-MM-DD string
    return commits


def get_word_count(repo_path: str, sample_size: int = 200) -> dict[str, dict[str, int]]:
    """
    For each sampled commit, collect cumulative word-count and .md file count.

    If multiple sampled commits fall on the same calendar day, only the **latest**
    commit (by commit timestamp) is kept for that day.
    """
    commits = get_sampled_commits(repo_path, sample_size)
    if not commits:
        logger.info(f"No commits found in {repo_path}. Exiting.")
        return {}

    commit_data: dict[str, dict[str, int]] = {}

    for commit in tqdm(commits, desc="Processing commits", unit="commit"):
        sha, date_str = commit.split(" ", 1)

        # Retrieve list of Markdown files for this commit
        try:
            ls_out = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", sha],
                cwd=repo_path,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
        except subprocess.CalledProcessError as e:
            logger.warning(f"git ls-tree failed for {sha}: {e}")
            continue

        md_files = [f for f in ls_out.strip().split("\n") if f.endswith(".md")]

        # Batch-retrieve file blobs (faster than one-by-one)
        words = 0
        if md_files:
            batch_spec = "\n".join(f"{sha}:{path}" for path in md_files).encode()
            proc = subprocess.Popen(
                ["git", "cat-file", "--batch"],
                cwd=repo_path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = proc.communicate(input=batch_spec)
            if proc.returncode:
                logger.warning(f"git cat-file failed for {sha}: {stderr.decode(errors='replace')}")
                continue

            pos = 0
            for _ in md_files:
                nl = stdout.find(b"\n", pos)
                if nl == -1:
                    break
                header = stdout[pos:nl].decode(errors="replace").split()
                pos = nl + 1
                if len(header) == 3 and header[1] == "blob":
                    size = int(header[2])
                    content = stdout[pos : pos + size]
                    pos += size + 1  # skip trailing newline
                    words += len(content.decode(errors="replace").split())

        # Determine commit’s epoch timestamp to pick the latest of the day
        ts = int(
            subprocess.run(
                ["git", "show", "-s", "--format=%ct", sha],
                cwd=repo_path,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
        )

        # Keep only the newest commit for that calendar date
        existing = commit_data.get(date_str)
        if (existing is None) or (ts > existing["timestamp"]):
            commit_data[date_str] = {
                "word_count": words,
                "md_file_count": len(md_files),
                "timestamp": ts,
            }

    return commit_data


# ────────────────────────────────────────────────────────────────────────────
# Plotting
# ────────────────────────────────────────────────────────────────────────────
def plot_word_count(data_map: dict[str, dict[str, int]]):
    if not data_map:
        logger.info("No data to plot.")
        return None

    # Convert to chronological order
    dates_str = sorted(data_map)
    dates = [datetime.strptime(d, "%Y-%m-%d") for d in dates_str]

    words_tot = np.array([data_map[d]["word_count"] for d in dates_str])
    md_tot = np.array([data_map[d]["md_file_count"] for d in dates_str])

    # Per-day rates (first-difference normalised by day gaps)
    delta_words = np.diff(np.insert(words_tot, 0, words_tot[0]))
    delta_md = np.diff(np.insert(md_tot, 0, md_tot[0]))
    days_between = np.diff(np.insert(np.array(dates, "datetime64[D]"), 0, dates[0])).astype(int)
    days_between = np.maximum(days_between, 1)
    rate_words = np.maximum(0, delta_words / days_between)
    rate_md = np.maximum(0, delta_md / days_between)

    # 10-day moving average for smoother trend lines (unchanged)
    win = 10
    smooth_words = pd.Series(rate_words).rolling(win, min_periods=1).mean().to_numpy()
    smooth_md = pd.Series(rate_md).rolling(win, min_periods=1).mean().to_numpy()
    smooth_words[-1] = rate_words[-1]
    smooth_md[-1] = rate_md[-1]

    # Build interactive Plotly figure
    fig = go.Figure()

    fig.add_trace(
        go.Scatter(
            x=dates,
            y=smooth_words,
            name=f"Net Word Δ / day ({win}-day avg)",
            mode="lines",
            line=dict(shape="spline"),
            yaxis="y1",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=smooth_md,
            name=f"Net .md Δ / day ({win}-day avg)",
            mode="lines",
            line=dict(shape="spline"),
            yaxis="y2",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=words_tot,
            name="Cumulative Word Count",
            mode="lines",
            line=dict(shape="spline", dash="dot"),
            yaxis="y3",
        )
    )

    fig.update_layout(
        template="plotly_white",
        title="Git Repository Analysis: Word / File Rate & Total Words (latest point = latest commit)",
        xaxis_title="Date",
        yaxis=dict(title="Net Word Δ / day"),
        yaxis2=dict(title="Net .md Δ / day", overlaying="y", side="right"),
        yaxis3=dict(title="Cumulative Word Count", overlaying="y", side="right", position=1),
        legend=dict(x=0.5, y=-0.25, orientation="h", xanchor="center"),
        margin=dict(l=60, r=80, t=80, b=100),
    )

    output_path = os.path.join(os.path.expanduser("~/Downloads"), "git_repository_analysis_sampled.html")
    fig.write_html(output_path)
    logger.info(f"Interactive plot saved to {output_path}")
    return output_path


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────
def main():
    logger.remove()
    logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss!UTC} | {level} | {message}")

    parser = argparse.ArgumentParser(description="Analyse git repository word & .md file counts.")
    parser.add_argument("--repo-path", type=str, default=".", help="Path to the git repository")
    parser.add_argument("--sample-size", type=int, default=200, help="Number of commits to sample")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed for random sampling (use the same number to reproduce a run)",
    )
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        logger.info(f"Random seed set to {args.seed}")

    logger.info(f"Analysing {args.repo_path} with sample size {args.sample_size}")
    data = get_word_count(args.repo_path, args.sample_size)

    if not any(v["word_count"] or v["md_file_count"] for v in data.values()):
        logger.info("No meaningful data collected – skipping plot.")
        return

    out = plot_word_count(data)
    if out:
        try:
            import webbrowser

            webbrowser.open(out)
        except Exception:
            logger.warning("Couldn't open browser automatically; open the HTML file manually.")

    logger.info("Done.")


if __name__ == "__main__":
    main()