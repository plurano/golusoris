#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Fail when an AGENTS.md reaches 95% of a coding client's context cap.

Windsurf reads each rule file up to 12,000 characters; Codex reads the AGENTS.md files on the
path from the repository root to the working directory up to 32 KiB together. A file past
either cap is silently truncated for that client. The check fails from 95% of a cap on, as the
fleet context survey does, so growth is caught before truncation.

Tracked files are read from the index, so the check sees what the next commit holds, with
LF line endings, whatever the working tree or a Windows checkout shows.
"""

from __future__ import annotations

import subprocess  # Caught in main for any subprocess error. # nosec B404
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

import repo_git

NAME = "AGENTS.md"
WINDSURF_CHARS_PER_FILE = 12_000
CODEX_BYTES_PER_CHAIN = 32 * 1024
NEAR_CAP_PERCENT = 95


def agents_contents(root: Path, *, worktree: bool = False) -> dict[str, bytes]:
    """Return every AGENTS.md as the next commit holds it, or with worktree=True as on disk.

    Commit view: tracked files from the index, untracked ones with the LF endings that
    eol=lf gives them on add. Disk view: raw bytes, since clients read those.
    """
    staged = repo_git.staged_entries(root, NAME)
    paths = sorted(staged)
    if worktree:
        contents = {p: (root / p).read_bytes() for p in paths if (root / p).is_file()}
    else:
        blobs = repo_git.read_blobs(root, [staged[p][1] for p in paths])
        contents = dict(zip(paths, blobs, strict=True))
    for path in repo_git.untracked_files(root, NAME):
        raw = (root / path).read_bytes()
        contents[path] = raw if worktree else raw.replace(b"\r\n", b"\n")
    return contents


def _chain(path: str) -> list[str]:
    """Return the AGENTS.md candidates from the root down to the directory holding path."""
    parts = PurePosixPath(path).parent.parts
    return [PurePosixPath(*parts[:depth], NAME).as_posix() for depth in range(len(parts) + 1)]


def _near_cap(size: int, cap: int) -> bool:
    """Return whether size reaches 95% of cap, compared in integers like the fleet survey."""
    return size * 100 >= cap * NEAR_CAP_PERCENT


def oversized(contents: dict[str, bytes]) -> list[str]:
    """Return one message per AGENTS.md or AGENTS.md chain at or past 95% of a client cap."""
    problems = []
    for name in sorted(contents):
        chars = len(contents[name].decode("utf-8"))
        if _near_cap(chars, WINDSURF_CHARS_PER_FILE):
            problems.append(f"{name}: {chars} chars >= 95% of Windsurf cap {WINDSURF_CHARS_PER_FILE}")
        chain_bytes = sum(len(contents[c]) for c in _chain(name) if c in contents)
        if _near_cap(chain_bytes, CODEX_BYTES_PER_CHAIN):
            problems.append(f"{name}: chain {chain_bytes} B >= 95% of Codex cap {CODEX_BYTES_PER_CHAIN}")
    return problems


def main(argv: list[str]) -> int:
    """Check [--worktree] [root] (default: current directory): 0 fits, 1 oversized, 3 error.

    Without --worktree tracked files are read as staged (pre-commit); with it, as on disk.
    """
    args = argv[1:]
    worktree = "--worktree" in args
    rest = [a for a in args if a != "--worktree"]
    if len(rest) > 1:
        sys.stderr.write("context-size: error: usage: context_size.py [--worktree] [root]\n")
        return 3
    root = Path(rest[0] if rest else ".").resolve()
    try:
        problems = oversized(agents_contents(root, worktree=worktree))
    except (repo_git.GitError, OSError, UnicodeDecodeError, subprocess.SubprocessError) as err:
        sys.stderr.write(f"context-size: error: {err}\n")
        return 3
    for line in problems:
        sys.stderr.write(f"context-size: {line}; move bulk to linked on-demand docs\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
