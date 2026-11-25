#!/usr/bin/env python3
# Ported from plurano/amussis tools/regenerate.py (commit 2c0e2aa).
# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Regenerate generated repository files and write an artifact manifest.

When dependencies are bumped on renovate branches, modules that replace a bumped
local module must be tidied so go.mod and go.sum match the updated graph, and
embedded assets such as the Scalar API reference browser bundle are rebuilt.
This tool executes go mod tidy across all modules, rebuilds the Scalar bundle,
regenerates module-backed upstream documentation pins,
recomputes the Go module sums pinned in tools/tool-versions.env,
recomputes release asset and image digests annotated in tools/tool-versions.env,
re-locks the tiny trainer Python requirements whose pins a Renovate branch moved,
verifies that no unexpected files were modified or deleted, and writes a directory
containing the regenerated files and a manifest describing them.

    write       run update commands, verify changes, and write artifact to --out
    check       hold the table below to the declared list .github/generated-files
    selftest    verify plant refusals and accepted cases on a fixture repository

The merge train of plurano/horistes commits the artifact to the dependency
update; it reads .github/generated-files from the default branch and commits
no path missing there, so that file and GENERATED must name the same paths.

Exit status: 0 success or nothing to regenerate, 1 unexpected changes or
generated file deleted, 2 the tool could not run or command failed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

SCHEMA = "horistes.generated-files.v1"
DECLARED = ".github/generated-files"
MODULE_TIMEOUT_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 60
SCALAR_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 60
MODULES_DISCOVERY_TIMEOUT_SECONDS = 60
MAX_STATUS_ENTRIES = 2048
MAX_MODULES = 64
MAX_CHANGED_FILES = 256
MAX_DECLARED_LINES = 1024
MAX_UPSTREAM_ROWS = 64
MAX_GRAPH_LINES = 8192
UPSTREAM_CATALOGUE = "docs/upstream/README.md"
MODULE_AUTHORITIES = {
    "root": ("root",),
    "core": ("core",),
    "root + core": ("root", "core"),
}
SKIPPED_AUTHORITIES = {
    "repository tool pin": "repository tool version, not a module graph pin",
    "embedded asset": "embedded asset pin handled by scalar-bundle-update.sh",
    "root + core + toolchain": "requires agreement with the installed Go toolchain",
}
UPSTREAM_VERSION = r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
UPSTREAM_ROW = (
    r"\| `(?P<package>[^`|\s]+)` \| `(?P<version>[^`|\s]+)` \| "
    r"(?P<authority>[^|]+) \| \[[^\]\n]+\]\((?P<directory>[a-z0-9][a-z0-9-]*)/README\.md\) \|"
)
# A replace chain settles one link per pass; the deepest chain here is four modules.
MAX_TIDY_PASSES = 6
SCALAR_PATHS = [
    "apidocs/embed/SCALAR_RELEASED",
    "apidocs/embed/scalar.js",
]
TOOL_VERSIONS = "tools/tool-versions.env"
MAX_TOOL_VERSION_LINES = 256
UNSAFE_PIN_CHAR = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
MODULE_SUM_LINE = re.compile(
    r"(?P<name>[A-Z][A-Z0-9_]*)_MODULE_SUM=(?P<sum>h1:[A-Za-z0-9+/]{43}=)"
)
MODULE_SUM_VALUE = re.compile(r"h1:[A-Za-z0-9+/]{43}=")
GO_VERSION_VALUE = re.compile(r"v[0-9A-Za-z.+-]+")
GO_PIN_COMMENT = re.compile(
    r"# renovate: datasource=go depName=(?P<module>\S+)(?: .*)?"
)
DIGEST_TIMEOUT_SECONDS = 300
MAX_DIGEST_BYTES = 256 << 20
DIGEST_CHUNK_BYTES = 65536
MAX_RELEASE_DIGEST_CASES = 32
RELEASE_DIGEST_COMMENT = re.compile(
    r"# (?P<kind>sha256-of|digest-of): (?P<template>\S+)"
)
DIGEST_DIRECTIVE_PREFIX = re.compile(r"\s*#\s*(?:sha256-of|digest-of)(?:\s|:|$)")
PIN_ASSIGNMENT = re.compile(r"(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>[^\s\"'`#;]*)")
RAW_PIN_ASSIGNMENT = re.compile(r"(?P<name>[A-Z][A-Z0-9_]*)=.*")
DIGEST_PLACEHOLDER = re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)(?P<strip>#v)?\}")
SHA256_VALUE = re.compile(r"[0-9a-f]{64}")
IMAGE_DIGEST_VALUE = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_REF_VALUE = re.compile(r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+")
SHA256_PIN_LINE = re.compile(
    r"(?P<var>[A-Z][A-Z0-9_]*_SHA256)=(?P<value>[0-9a-f]{64})"
)
IMAGE_DIGEST_PIN_LINE = re.compile(
    r"(?P<var>[A-Z][A-Z0-9_]*_IMAGE_DIGEST)=(?P<value>sha256:[0-9a-f]{64})"
)
TRAINER_LOCK_SCRIPT = "scripts/ci/tiny-trainer-locks.sh"
TRAINER_LOCK_PATHS = [
    "ai/tiny/serve/tflite/sidecar/requirements.lock",
    "ai/tiny/trainers/gemma/requirements.lock",
    "ai/tiny/trainers/litert/requirements.lock",
    "scripts/ci/tiny-trainer-pip-audit.lock",
]
RENOVATE_CONFIG = "renovate.json"
PRESET_URL = "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/default.json"
LOCK_TIMEOUT_SECONDS = 900
PRESET_TIMEOUT_SECONDS = 20
MAX_PRESET_BYTES = 1048576
MAX_LOCK_CONTEXTS = 4
MAX_LOCK_LINES = 16384
MAX_REQUIREMENT_LINES = 256
MAX_EXTENDS = 32
# check_pypi_overrides inspects renovate.json plus every extended preset.
MAX_RENOVATE_CONFIGS = MAX_EXTENDS + 1
MAX_PACKAGE_RULES = 256
MAX_DATASOURCES = 64
PRESET_REF = re.compile(r"github>(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+)")
RELEASE_AGE = re.compile(
    r"(?P<count>[0-9]{1,4}) (?P<unit>minutes?|hours?|days?|weeks?)"
)
RELEASE_AGE_SECONDS = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}
CONTEXT_ROW_PATTERN = (
    r"(?P<var>[A-Z][A-Z0-9_]*_LOCK_EXCLUDE_NEWER)\t"
    r"(?P<lock>[^\t]+)\t(?P<kind>file|pin):(?P<source>[^\t]+)"
)
CONTEXT_ROW = re.compile(CONTEXT_ROW_PATTERN)
REQUIREMENT_PIN_PATTERN = (
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9._,-]+\])?==(?P<version>[A-Za-z0-9.+!_-]+)"
)
REQUIREMENT_PIN = re.compile(REQUIREMENT_PIN_PATTERN)
LOCK_PIN = re.compile(r"(?P<name>[a-z0-9][a-z0-9._-]*)==(?P<version>\S+) \\")
DATASOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
LOCK_DIRECT_VIA = frozenset({"# via -r requirements.in", "#   -r requirements.in"})
CUTOFF_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CUTOFF_VALUE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class LockHooks(NamedTuple):
    """Injectable clock, preset fetcher, and subprocess environment for lock regen."""

    now: datetime
    fetch: Callable[[str], bytes]
    env: dict[str, str] | None


class DigestHooks(NamedTuple):
    """Injectable URL hasher and OCI image digest resolver for release pins."""

    fetch_sha256: Callable[[str], str]
    image_digest: Callable[[str], str]


class ReleaseDigestPin(NamedTuple):
    """Validated sha256-of or digest-of directive and target pin in tool-versions."""

    kind: str
    source: str
    var: str
    current: str
    pin_idx: int


class CheckError(Exception):
    """Raised when regeneration or verification cannot proceed."""


def declared_paths(text: str) -> list[str]:
    """The paths of a .github/generated-files text: no comments, no blank lines."""
    lines = text.splitlines()
    bound = min(len(lines), MAX_DECLARED_LINES)
    out: list[str] = []
    for i in range(bound):
        line = lines[i].strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def discover_modules(
    root: Path, timeout: int = MODULES_DISCOVERY_TIMEOUT_SECONDS
) -> list[str]:
    script = root / "scripts" / "ci" / "go-modules.sh"
    if not script.is_file():
        raise CheckError(f"module discovery script not found: {script}")
    try:
        proc = subprocess.run(  # noqa: S603 - fixed bash command on a repository script.
            ["bash", str(script), "list"],  # noqa: S607 - bash executable on runner PATH.
            cwd=root,
            env={
                **os.environ,
                "CI_SCOPE_BASE": "",
            },  # regeneration covers every module, never a diff scope
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"go-modules.sh list failed to run: {error}") from error
    if proc.returncode != 0:
        err = proc.stderr.strip()
        raise CheckError(f"go-modules.sh list exited {proc.returncode}: {err}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines or len(lines) > MAX_MODULES:
        raise CheckError(f"unexpected module count: {len(lines)}")
    return lines


def upstream_paths(root: Path) -> list[str]:
    paths = sorted(root.glob("docs/upstream/*/README.md"))
    if not paths or len(paths) > MAX_UPSTREAM_ROWS:
        raise CheckError(f"upstream pins: unexpected snapshot count: {len(paths)}")
    return [UPSTREAM_CATALOGUE, *[p.relative_to(root).as_posix() for p in paths]]


def get_generated_files(root: Path) -> list[str]:
    """Return the sorted repository-relative paths of all generated files."""
    modules = discover_modules(root)
    paths: list[str] = []
    bound = min(len(modules), MAX_MODULES)
    for i in range(bound):
        m = modules[i]
        if m == ".":
            paths.extend(["go.mod", "go.sum"])
        else:
            paths.extend([f"{m}/go.mod", f"{m}/go.sum"])
    paths.extend(SCALAR_PATHS)
    paths.extend(upstream_paths(root))
    paths.append(TOOL_VERSIONS)
    paths.append(TRAINER_LOCK_SCRIPT)
    paths.extend(TRAINER_LOCK_PATHS)
    return sorted(paths)


def check_declared(root: Path, expected: list[str] | None = None) -> list[str]:
    try:
        text = (root / DECLARED).read_text(encoding="utf-8")
    except OSError as error:
        raise CheckError(f"{DECLARED} could not be read: {error}") from error
    listed = declared_paths(text)
    if expected is None:
        expected = get_generated_files(root)
    if sorted(listed) != sorted(expected) or len(listed) != len(set(listed)):
        diffs: list[str] = []
        missing = sorted(set(expected) - set(listed))
        extra = sorted(set(listed) - set(expected))
        if missing:
            diffs.append(f"missing: {missing}")
        if extra:
            diffs.append(f"unexpected: {extra}")
        if len(listed) != len(set(listed)):
            dups = sorted({x for x in listed if listed.count(x) > 1})
            diffs.append(f"duplicate entries: {dups}")
        return [f"{DECLARED} differs from generated table ({'; '.join(diffs)})"]
    return []


def run_git(args: list[str], root: Path, timeout: int = GIT_TIMEOUT_SECONDS) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 - git with arguments built in this file.
            ["git", *args],  # noqa: S607 - git executable on runner PATH.
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"git {' '.join(args)} could not run: {error}") from error
    if proc.returncode != 0:
        err = proc.stderr.strip()
        raise CheckError(f"git {' '.join(args)} exited {proc.returncode}: {err}")
    return proc.stdout


def git_status_bytes(root: Path, timeout: int = GIT_TIMEOUT_SECONDS) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],  # noqa: S607 - git executable on runner PATH.
            cwd=root,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckError(f"git status could not run: {error}") from error
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        raise CheckError(f"git status exited {proc.returncode}: {err}")
    return proc.stdout


def parse_status_entries(raw: bytes) -> list[tuple[str, str, str | None]]:
    if not raw:
        return []
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    entries: list[tuple[str, str, str | None]] = []
    idx = 0
    bound = min(len(tokens), MAX_STATUS_ENTRIES)
    for _ in range(bound):
        if idx >= len(tokens):
            break
        item = tokens[idx]
        idx += 1
        if len(item) < 3:
            continue
        code = item[:2].decode("ascii", errors="replace")
        path = item[3:].decode("utf-8", errors="replace")
        orig_path = None
        if ("R" in code or "C" in code) and idx < len(tokens):
            orig_path = tokens[idx].decode("utf-8", errors="replace")
            idx += 1
        entries.append((code, path, orig_path))
    return entries


def evaluate_changes(
    entries: list[tuple[str, str, str | None]],
    root: Path,
    allowed: set[str],
    pre_existing: set[str],
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    changed: list[str] = []
    bound = min(len(entries), MAX_STATUS_ENTRIES)
    for i in range(bound):
        code, path, orig = entries[i]
        if path not in allowed:
            failures.append(f"unexpected changed path: {path}")
        if orig is not None and orig not in allowed:
            failures.append(f"unexpected changed path: {orig}")
        is_deleted = "D" in code or not (root / path).is_file()
        if is_deleted:
            if path in allowed and path in pre_existing:
                failures.append(f"generated path was deleted: {path}")
        elif path in allowed:
            changed.append(path)
    return failures, sorted(set(changed))


def run_update_command(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 - go command lines built in this file.
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CheckError(f"{' '.join(cmd)} timed out after {timeout} s") from error
    except OSError as error:
        raise CheckError(f"{' '.join(cmd)} could not run: {error}") from error
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        raise CheckError(f"{' '.join(cmd)} exited {proc.returncode}:\n{err}")
    return proc.stdout


def upstream_rows(text: str) -> list[re.Match[str]]:
    lines = re.findall(r"^\| `.*$", text, re.MULTILINE)
    if not lines or len(lines) > MAX_UPSTREAM_ROWS:
        raise CheckError(f"upstream pins: unexpected catalogue row count: {len(lines)}")
    rows: list[re.Match[str]] = []
    packages: set[str] = set()
    directories: set[str] = set()
    for i in range(min(len(lines), MAX_UPSTREAM_ROWS)):
        row = re.fullmatch(UPSTREAM_ROW, lines[i])
        if row is None:
            raise CheckError(f"upstream pins: malformed catalogue row: {lines[i]}")
        package, directory = row["package"], row["directory"]
        if package in packages or directory in directories:
            raise CheckError(f"upstream pins: duplicate catalogue mapping: {package}")
        packages.add(package)
        directories.add(directory)
        rows.append(row)
    return rows


def upstream_graphs(root: Path) -> tuple[dict[str, dict[str, str]], list[list[str]]]:
    graphs: dict[str, dict[str, str]] = {}
    commands: list[list[str]] = []
    for authority, directory in (("root", "."), ("core", "core")):
        cmd = [
            "go",
            "-C",
            directory,
            "list",
            "-mod=readonly",
            "-m",
            "-f",
            "{{.Path}}\t{{.Version}}",
            "all",
        ]
        lines = run_update_command(cmd, root, DEFAULT_TIMEOUT_SECONDS).splitlines()
        if len(lines) > MAX_GRAPH_LINES:
            raise CheckError(
                f"upstream pins: {authority} graph exceeds {MAX_GRAPH_LINES} lines"
            )
        graph: dict[str, str] = {}
        for i in range(min(len(lines), MAX_GRAPH_LINES)):
            fields = lines[i].split("\t")
            if len(fields) != 2 or fields[0] in graph:
                raise CheckError(
                    f"upstream pins: malformed {authority} graph line: {lines[i]}"
                )
            graph[fields[0]] = fields[1]
        graphs[authority] = graph
        commands.append(cmd)
    return graphs, commands


def upstream_version(
    package: str, authorities: tuple[str, ...], graphs: dict[str, dict[str, str]]
) -> str:
    versions = {graphs[a].get(package, "") for a in authorities}
    if len(versions) != 1 or "" in versions:
        raise CheckError(
            f"upstream pins: {package}: missing or disagreeing module authorities {authorities}"
        )
    version = versions.pop()
    if re.fullmatch(UPSTREAM_VERSION, version) is None:
        raise CheckError(
            f"upstream pins: {package}: unsupported module version {version!r}"
        )
    return version


def replace_upstream_token(
    text: str, prefix: str, pattern: str, version: str, label: str
) -> str:
    matches = list(re.finditer(rf"^{prefix}[^\r\n]*$", text, re.MULTILINE))
    if prefix == "# ":
        matches = matches[:1]
    if len(matches) != 1:
        raise CheckError(f"upstream pins: {label} shape (expected exactly one pin)")
    line = matches[0]
    match = re.fullmatch(pattern, line[0])
    if match is None:
        raise CheckError(f"upstream pins: {label} shape (unsupported pin line)")
    start, end = match.span("version")
    return text[: line.start() + start] + version + text[line.start() + end :]


def upstream_snapshot(text: str, version: str, path: str) -> str:
    token = rf"(?P<version>{UPSTREAM_VERSION})"
    patterns = (
        ("heading", "# ", rf"# [^\r\n]+ — {token} snapshot"),
        ("Pinned", "Pinned: ", rf"Pinned: \*\*{token}\*\*[^\r\n]*"),
        (
            "Source",
            "Source: ",
            rf"Source: \[tagged source\]\(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/tree/{token}\)",
        ),
    )
    for label, prefix, pattern in patterns:
        text = replace_upstream_token(
            text, prefix, pattern, version, f"{path}: unexpected {label}"
        )
    return text


def read_upstream_file(root: Path, path: str) -> str:
    try:
        return (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CheckError(f"upstream pins: {path}: could not read: {error}") from error


def regenerate_upstream_pins(root: Path) -> list[list[str]]:
    catalogue = root / UPSTREAM_CATALOGUE
    original = read_upstream_file(root, UPSTREAM_CATALOGUE)
    text = original
    rows = upstream_rows(text)
    graphs, commands = upstream_graphs(root)
    pending: dict[Path, str] = {}
    for i in range(min(len(rows), MAX_UPSTREAM_ROWS)):
        row = rows[i]
        package, authority = row["package"], row["authority"]
        if authority in SKIPPED_AUTHORITIES:
            print(f"upstream pins: skip {package}: {SKIPPED_AUTHORITIES[authority]}")
            continue
        if authority not in MODULE_AUTHORITIES:
            raise CheckError(
                f"upstream pins: {package}: unknown authority {authority!r}"
            )
        version = upstream_version(package, MODULE_AUTHORITIES[authority], graphs)
        path = f"docs/upstream/{row['directory']}/README.md"
        snapshot_file = root / path
        snapshot_text = read_upstream_file(root, path)
        updated = upstream_snapshot(snapshot_text, version, path)
        if updated != snapshot_text:
            pending[snapshot_file] = updated
        start, end = row.span("version")
        updated_row = row[0][:start] + version + row[0][end:]
        text = text.replace(row[0], updated_row, 1)
    if text != original:
        pending[catalogue] = text
    for path, updated in list(pending.items())[: MAX_UPSTREAM_ROWS + 1]:
        path.write_text(updated, encoding="utf-8")
    return commands


def tool_version_lines(text: str) -> list[str]:
    # Split on LF only so Python agrees with bash source and Make include.
    lines = text.split("\n")
    if len(lines) > MAX_TOOL_VERSION_LINES + 1:
        raise CheckError(f"{TOOL_VERSIONS} exceeds {MAX_TOOL_VERSION_LINES} lines")
    for i in range(min(len(lines), MAX_TOOL_VERSION_LINES + 1)):
        if UNSAFE_PIN_CHAR.search(lines[i]) is not None:
            msg = (
                f"{TOOL_VERSIONS}:{i + 1}: "
                "line holds a control or line-separator character"
            )
            raise CheckError(msg)
        if "\\" in lines[i]:
            msg = (
                f"{TOOL_VERSIONS}:{i + 1}: line holds a backslash; "
                "bash and Make read line continuations differently"
            )
            raise CheckError(msg)
    return lines


def _pin_version(lines: list[str], index: int, name: str) -> tuple[str, str]:
    prefix = f"{name}_VERSION="
    comment = GO_PIN_COMMENT.fullmatch(lines[index - 2]) if index >= 2 else None
    version = (
        lines[index - 1].removeprefix(prefix)
        if index >= 2 and lines[index - 1].startswith(prefix)
        else ""
    )
    if comment is None or GO_VERSION_VALUE.fullmatch(version) is None:
        msg = (
            f"{TOOL_VERSIONS}:{index + 1}: {name}_MODULE_SUM needs {name}_VERSION "
            "and a datasource=go renovate comment above it"
        )
        raise CheckError(msg)
    return comment["module"], version


def module_sum_pins(text: str) -> list[tuple[str, str, int]]:
    lines = tool_version_lines(text)
    pins: list[tuple[str, str, int]] = []
    for i in range(min(len(lines), MAX_TOOL_VERSION_LINES + 1)):
        line = lines[i]
        sum_match = MODULE_SUM_LINE.fullmatch(line)
        if sum_match is None:
            if "_MODULE_SUM" in line.upper() and not line.lstrip().startswith("#"):
                msg = (
                    f"{TOOL_VERSIONS}:{i + 1}: module sum pin must be "
                    "NAME_MODULE_SUM=h1:<43 base64 chars>= at column 0 "
                    "with nothing after it"
                )
                raise CheckError(msg)
            continue
        module, version = _pin_version(lines, i, sum_match["name"])
        pins.append((module, version, i))
    return pins


def go_module_sum(
    root: Path,
    module: str,
    version: str,
    env: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    target = f"{module}@{version}"
    cmd = [
        "bash",
        str(root / "scripts" / "ci" / "retry.sh"),
        "go",
        "mod",
        "download",
        "-json",
        target,
    ]
    base_env = env if env is not None else os.environ
    run_env = {
        **base_env,
        "GO111MODULE": "on",
        "GOFLAGS": "-mod=mod -modcacherw",
    }
    with tempfile.TemporaryDirectory(prefix="golusoris-modsum-") as work_dir:
        stdout = run_update_command(
            cmd, Path(work_dir), MODULE_TIMEOUT_SECONDS, run_env
        )
    try:
        data = json.loads(stdout)
    except ValueError as error:
        raise CheckError(f"go mod download {target}: invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise CheckError(f"go mod download {target}: expected JSON object")
    if data.get("Error") not in (None, ""):
        raise CheckError(f"go mod download {target}: {data['Error']}")
    if data.get("Path") != module or data.get("Version") != version:
        msg = (
            f"go mod download {target}: "
            f"resolved {data.get('Path')!r}@{data.get('Version')!r}"
        )
        raise CheckError(msg)
    module_sum = data.get("Sum")
    if (
        not isinstance(module_sum, str)
        or MODULE_SUM_VALUE.fullmatch(module_sum) is None
    ):
        raise CheckError(
            f"go mod download {target}: invalid module sum {module_sum!r}"
        )
    return module_sum, cmd


def regenerate_module_sums(
    root: Path, env: dict[str, str] | None = None
) -> list[list[str]]:
    path = root / TOOL_VERSIONS
    if not path.is_file():
        return []
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise CheckError(f"{TOOL_VERSIONS}: could not read: {error}") from error
    pins = module_sum_pins(text)
    if not pins:
        return []
    lines = tool_version_lines(text)
    commands: list[list[str]] = []
    for i in range(min(len(pins), MAX_TOOL_VERSION_LINES)):
        module, version, line_idx = pins[i]
        new_sum, cmd = go_module_sum(root, module, version, env)
        commands.append(cmd)
        match = MODULE_SUM_LINE.fullmatch(lines[line_idx])
        if match is not None and match["sum"] != new_sum:
            name = match["name"]
            lines[line_idx] = f"{name}_MODULE_SUM={new_sum}"
    updated = "\n".join(lines)
    if updated != text:
        path.write_bytes(updated.encode("utf-8"))
    return commands


def _https_opener(
    *, follow_redirects: bool = False
) -> urllib.request.OpenerDirector:
    """Return a urllib opener restricted to HTTPS URLs."""
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.UnknownHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    if follow_redirects:
        opener.add_handler(urllib.request.HTTPRedirectHandler())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


def sha256_stream(reader: io.BufferedIOBase, limit: int) -> str:
    """Hash reader in fixed chunks up to limit bytes or refuse larger bodies."""
    if limit < 0 or limit > MAX_DIGEST_BYTES:
        raise CheckError(f"invalid digest byte limit: {limit}")
    digest = hashlib.sha256()
    total = 0
    for _ in range(MAX_DIGEST_BYTES // DIGEST_CHUNK_BYTES + 1):
        chunk = reader.read(DIGEST_CHUNK_BYTES)
        if not chunk:
            return digest.hexdigest()
        total += len(chunk)
        if total > limit:
            raise CheckError(f"release asset exceeds {limit} bytes")
        digest.update(chunk)
    raise CheckError(f"release asset exceeds {limit} bytes")


def fetch_sha256(url: str) -> str:
    """Fetch an HTTPS URL and return its lowercase SHA-256 hex digest."""
    if not url.startswith("https://"):
        raise CheckError(f"release digest URL must start with https://: {url}")
    try:
        with _https_opener(follow_redirects=True).open(
            url, timeout=DIGEST_TIMEOUT_SECONDS
        ) as response:
            return sha256_stream(response, MAX_DIGEST_BYTES)
    except (OSError, ValueError, http.client.HTTPException) as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        raise CheckError(f"could not fetch release asset {url}: {error}") from error


def _imagetools_command(ref: str) -> list[str]:
    """Return the retry-wrapped buildx imagetools inspect command for ref."""
    return [
        "bash",
        "scripts/ci/retry.sh",
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        ref,
        "--format",
        "{{json .Manifest}}",
    ]


def image_digest(ref: str, root: Path | None = None) -> str:
    """Return ref's OCI index or manifest digest via buildx imagetools inspect."""
    if "@" in ref or IMAGE_REF_VALUE.fullmatch(ref) is None:
        raise CheckError(f"invalid OCI image reference: {ref!r}")
    actual_root = root if root is not None else Path(__file__).resolve().parents[2]
    stdout = run_update_command(
        _imagetools_command(ref), actual_root, DIGEST_TIMEOUT_SECONDS
    )
    manifest = load_json_object(stdout, f"imagetools inspect {ref}")
    digest = manifest.get("digest")
    if not isinstance(digest, str) or IMAGE_DIGEST_VALUE.fullmatch(digest) is None:
        raise CheckError(f"imagetools inspect {ref}: invalid digest {digest!r}")
    return digest


def _expand_token(
    match: re.Match[str], defs: dict[str, str], line_no: int
) -> str:
    """Resolve one ${NAME} or ${NAME#v} placeholder against prior assignments."""
    name = match["name"]
    if name not in defs:
        raise CheckError(f"{TOOL_VERSIONS}:{line_no}: undefined variable {name}")
    value = defs[name]
    return value.removeprefix("v") if match["strip"] else value


def _expand_digest_source(
    template: str, kind: str, defs: dict[str, str], line_no: int
) -> str:
    """Expand allow-listed placeholders and validate the resulting URL or ref."""
    expanded = DIGEST_PLACEHOLDER.sub(
        lambda m: _expand_token(m, defs, line_no), template
    )
    if "$" in expanded:
        msg = f"{TOOL_VERSIONS}:{line_no}: unsupported expansion in {template!r}"
        raise CheckError(msg)
    if kind == "sha256-of":
        if not expanded.startswith("https://") or len(expanded) <= len("https://"):
            msg = (
                f"{TOOL_VERSIONS}:{line_no}: "
                f"sha256-of URL must start with https://: {expanded!r}"
            )
            raise CheckError(msg)
        return expanded
    if "@" in expanded or IMAGE_REF_VALUE.fullmatch(expanded) is None:
        msg = f"{TOOL_VERSIONS}:{line_no}: invalid digest-of ref {expanded!r}"
        raise CheckError(msg)
    return expanded


def _parse_digest_pin_line(
    kind: str, next_line: str, line_no: int
) -> tuple[str, str]:
    """Validate the pin line directly below a sha256-of or digest-of comment."""
    pattern = SHA256_PIN_LINE if kind == "sha256-of" else IMAGE_DIGEST_PIN_LINE
    match = pattern.fullmatch(next_line)
    if match is None:
        expected = (
            "NAME_SHA256=<64 hex>"
            if kind == "sha256-of"
            else "NAME_IMAGE_DIGEST=sha256:<64 hex>"
        )
        msg = f"{TOOL_VERSIONS}:{line_no}: {kind} comment must precede {expected}"
        raise CheckError(msg)
    return match["var"], match["value"]


def _record_pin_assignment(line: str, defs: dict[str, str]) -> bool:
    """Record a column-0 NAME=value line into defs and report whether line matched."""
    assign = PIN_ASSIGNMENT.fullmatch(line)
    if assign is not None:
        defs[assign["name"]] = assign["value"]
        return True
    raw_assign = RAW_PIN_ASSIGNMENT.fullmatch(line)
    if raw_assign is not None:
        defs.pop(raw_assign["name"], None)
        return True
    return False


def release_digest_pins(text: str) -> list[ReleaseDigestPin]:
    """Parse and validate all sha256-of and digest-of pin pairs in text."""
    lines = tool_version_lines(text)
    defs: dict[str, str] = {}
    pins: list[ReleaseDigestPin] = []
    for i in range(min(len(lines), MAX_TOOL_VERSION_LINES + 1)):
        line = lines[i]
        if _record_pin_assignment(line, defs):
            continue
        comment = RELEASE_DIGEST_COMMENT.fullmatch(line)
        if comment is None:
            if DIGEST_DIRECTIVE_PREFIX.match(line) is not None:
                msg = f"{TOOL_VERSIONS}:{i + 1}: malformed release digest comment"
                raise CheckError(msg)
            continue
        kind = comment["kind"]
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        var, current = _parse_digest_pin_line(kind, next_line, i + 1)
        source = _expand_digest_source(comment["template"], kind, defs, i + 1)
        pins.append(ReleaseDigestPin(kind, source, var, current, i + 1))
    return pins


def _resolve_release_digest(
    pin: ReleaseDigestPin, hooks: DigestHooks
) -> tuple[str, list[str]]:
    """Compute one verified sha256 or OCI digest and its manifest command."""
    if pin.kind == "sha256-of":
        got = hooks.fetch_sha256(pin.source)
        if not isinstance(got, str) or SHA256_VALUE.fullmatch(got) is None:
            msg = f"{TOOL_VERSIONS}:{pin.pin_idx + 1}: invalid sha256 {got!r}"
            raise CheckError(msg)
        return got, ["sha256-of", pin.source]
    got = hooks.image_digest(pin.source)
    if not isinstance(got, str) or IMAGE_DIGEST_VALUE.fullmatch(got) is None:
        msg = f"{TOOL_VERSIONS}:{pin.pin_idx + 1}: invalid image digest {got!r}"
        raise CheckError(msg)
    return got, _imagetools_command(pin.source)


def regenerate_release_digests(
    root: Path, hooks: DigestHooks | None = None
) -> list[list[str]]:
    """Recompute annotated release asset and OCI image digests in tool-versions."""
    path = root / TOOL_VERSIONS
    if not path.is_file():
        return []
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise CheckError(f"{TOOL_VERSIONS}: could not read: {error}") from error
    pins = release_digest_pins(text)
    if not pins:
        return []
    active = (
        hooks
        if hooks is not None
        else DigestHooks(fetch_sha256, lambda ref: image_digest(ref, root))
    )
    lines = tool_version_lines(text)
    commands: list[list[str]] = []
    for i in range(min(len(pins), MAX_TOOL_VERSION_LINES)):
        pin = pins[i]
        new_value, cmd = _resolve_release_digest(pin, active)
        commands.append(cmd)
        if new_value != pin.current:
            lines[pin.pin_idx] = f"{pin.var}={new_value}"
    updated = "\n".join(lines)
    if updated != text:
        path.write_bytes(updated.encode("utf-8"))
    return commands


def fetch_preset(url: str) -> bytes:
    """Return raw Renovate preset bytes from GitHub or refuse invalid URLs."""
    if not url.startswith("https://raw.githubusercontent.com/"):
        raise CheckError(f"unexpected Renovate preset URL: {url}")
    try:
        with _https_opener().open(url, timeout=PRESET_TIMEOUT_SECONDS) as response:
            body: bytes = response.read(MAX_PRESET_BYTES + 1)
    except (OSError, ValueError) as error:
        raise CheckError(f"could not fetch Renovate preset {url}: {error}") from error
    if len(body) > MAX_PRESET_BYTES:
        raise CheckError(f"Renovate preset {url} exceeds {MAX_PRESET_BYTES} bytes")
    return body


def load_json_object(data: bytes | str, source: str) -> dict[str, Any]:
    """Return a parsed JSON object or refuse malformed or non-dict payloads."""
    try:
        parsed = json.loads(data)
    except ValueError as error:
        raise CheckError(f"{source}: invalid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise CheckError(f"{source}: expected JSON object")
    return parsed


def parse_release_age(value: object, source: str) -> timedelta:
    """Return the minimumReleaseAge duration or refuse unsupported strings."""
    msg = (
        f"{source}: minimumReleaseAge {value!r} is not "
        "'<n> minutes|hours|days|weeks'"
    )
    if not isinstance(value, str):
        raise CheckError(msg)
    match = RELEASE_AGE.fullmatch(value)
    if match is None:
        raise CheckError(msg)
    count = int(match["count"])
    if count == 0:
        raise CheckError(msg)
    unit = match["unit"].removesuffix("s")
    return timedelta(seconds=count * RELEASE_AGE_SECONDS[unit])


def github_presets(
    config: dict[str, Any], fetch: Callable[[str], bytes]
) -> list[tuple[dict[str, Any], str]]:
    """Return fetched GitHub Renovate presets in extends order."""
    extends = config.get("extends", [])
    if not isinstance(extends, list) or len(extends) > MAX_EXTENDS:
        raise CheckError(f"{RENOVATE_CONFIG}: invalid or oversized extends list")
    presets: list[tuple[dict[str, Any], str]] = []
    for i in range(min(len(extends), MAX_EXTENDS)):
        entry = extends[i]
        if not isinstance(entry, str) or not entry.startswith("github>"):
            continue
        ref = PRESET_REF.fullmatch(entry)
        if ref is None:
            msg = f"{RENOVATE_CONFIG}: unsupported github preset reference {entry!r}"
            raise CheckError(msg)
        url = PRESET_URL.format(owner=ref["owner"], repo=ref["repo"])
        preset = load_json_object(fetch(url), entry)
        presets.append((preset, entry))
    return presets


def release_age_source(
    config: dict[str, Any], presets: list[tuple[dict[str, Any], str]]
) -> tuple[dict[str, Any], str]:
    """Return the config or last preset defining minimumReleaseAge."""
    if "minimumReleaseAge" in config:
        return config, RENOVATE_CONFIG
    reversed_presets = list(reversed(presets))
    for i in range(min(len(reversed_presets), MAX_EXTENDS)):
        preset, entry = reversed_presets[i]
        if "minimumReleaseAge" in preset:
            return preset, entry
    msg = f"{RENOVATE_CONFIG}: no minimumReleaseAge in the file or its github presets"
    raise CheckError(msg)


def _applies_to_pypi(datasources: object, source: str) -> bool:
    """Validate matchDatasources plain names and report whether pypi is listed."""
    if datasources is None:
        return True
    if not isinstance(datasources, list) or len(datasources) > MAX_DATASOURCES:
        raise CheckError(f"{source}: invalid packageRule matchDatasources")
    for i in range(min(len(datasources), MAX_DATASOURCES)):
        entry = datasources[i]
        if not isinstance(entry, str) or DATASOURCE_NAME.fullmatch(entry) is None:
            msg = (
                f"{source}: packageRule matchDatasources entry {entry!r} "
                "is not a plain datasource name"
            )
            raise CheckError(msg)
    return "pypi" in datasources


def check_pypi_package_rule(rule: object, source: str, base: timedelta) -> None:
    """Refuse a non-object packageRule or conflicting PyPI minimumReleaseAge."""
    if not isinstance(rule, dict):
        raise CheckError(f"{source}: packageRule is not a JSON object")
    if "minimumReleaseAge" not in rule:
        return
    if not _applies_to_pypi(rule.get("matchDatasources"), source):
        return
    if parse_release_age(rule["minimumReleaseAge"], source) != base:
        raise CheckError(f"{source}: packageRule minimumReleaseAge differs for pypi")


def check_pypi_overrides(
    configs: list[tuple[dict[str, Any], str]], base: timedelta
) -> None:
    """Refuse oversized packageRules lists or conflicting PyPI release ages."""
    if len(configs) > MAX_RENOVATE_CONFIGS:
        raise CheckError(f"{RENOVATE_CONFIG}: too many configs to check")
    for i in range(min(len(configs), MAX_RENOVATE_CONFIGS)):
        cfg, source = configs[i]
        rules = cfg.get("packageRules", [])
        if not isinstance(rules, list) or len(rules) > MAX_PACKAGE_RULES:
            raise CheckError(f"{source}: invalid or oversized packageRules")
        for j in range(min(len(rules), MAX_PACKAGE_RULES)):
            check_pypi_package_rule(rules[j], source, base)


def renovate_min_release_age(
    root: Path, fetch: Callable[[str], bytes]
) -> timedelta:
    """Return the validated PyPI minimumReleaseAge from Renovate config."""
    try:
        raw = (root / RENOVATE_CONFIG).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CheckError(f"{RENOVATE_CONFIG}: could not read: {error}") from error
    config = load_json_object(raw, RENOVATE_CONFIG)
    presets = github_presets(config, fetch)
    base_cfg, base_src = release_age_source(config, presets)
    age = parse_release_age(base_cfg["minimumReleaseAge"], base_src)
    check_pypi_overrides([(config, RENOVATE_CONFIG), *presets], age)
    return age


def normalize_name(name: str) -> str:
    """Return the PEP 503 canonical lowercase hyphenated package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def direct_pins(text: str, source: str) -> dict[str, str]:
    """Return normalized exact requirement pins or refuse unpinned entries."""
    lines = text.splitlines()
    if len(lines) > MAX_REQUIREMENT_LINES:
        raise CheckError(f"{source} exceeds {MAX_REQUIREMENT_LINES} lines")
    pins: dict[str, str] = {}
    for i in range(min(len(lines), MAX_REQUIREMENT_LINES)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = REQUIREMENT_PIN.fullmatch(stripped)
        if match is None:
            msg = f"{source}:{i + 1}: only name==version pins can be re-locked"
            raise CheckError(msg)
        pins[normalize_name(match["name"])] = match["version"]
    return pins


def lock_pins(text: str, source: str) -> dict[str, str]:
    """Return locked package versions by name or refuse oversized lockfiles."""
    lines = text.splitlines()
    if len(lines) > MAX_LOCK_LINES:
        raise CheckError(f"{source} exceeds {MAX_LOCK_LINES} lines")
    pins: dict[str, str] = {}
    for i in range(min(len(lines), MAX_LOCK_LINES)):
        match = LOCK_PIN.fullmatch(lines[i])
        if match is not None:
            pins[normalize_name(match["name"])] = match["version"]
    return pins


def lock_direct_names(text: str, source: str) -> set[str]:
    """Return normalized package names annotated as direct requirements in text."""
    lines = text.splitlines()
    if len(lines) > MAX_LOCK_LINES:
        raise CheckError(f"{source} exceeds {MAX_LOCK_LINES} lines")
    direct: set[str] = set()
    current = ""
    for i in range(min(len(lines), MAX_LOCK_LINES)):
        match = LOCK_PIN.fullmatch(lines[i])
        if match is not None:
            current = normalize_name(match["name"])
        elif current and lines[i].strip() in LOCK_DIRECT_VIA:
            direct.add(current)
    return direct


def parse_context_row(line: str) -> tuple[str, str, str, str]:
    """Return a validated lock-context tuple or refuse malformed rows."""
    match = CONTEXT_ROW.fullmatch(line)
    if match is None:
        raise CheckError(f"invalid lock context row: {line!r}")
    var, lock, kind, source = (
        match["var"],
        match["lock"],
        match["kind"],
        match["source"],
    )
    if lock not in TRAINER_LOCK_PATHS:
        raise CheckError(f"unexpected lock path in context row: {line!r}")
    if kind == "file":
        src_path = Path(source)
        if src_path.is_absolute() or src_path.anchor or ".." in src_path.parts:
            raise CheckError(f"unsafe file source in context row: {line!r}")
    return var, lock, kind, source


def lock_contexts(
    root: Path, env: dict[str, str] | None
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Return parsed trainer lock contexts and the discovery command."""
    cmd = ["bash", TRAINER_LOCK_SCRIPT, "--contexts"]
    stdout = run_update_command(cmd, root, DEFAULT_TIMEOUT_SECONDS, env)
    lines = [line for line in stdout.splitlines() if line]
    if not lines or len(lines) > MAX_LOCK_CONTEXTS:
        raise CheckError(f"unexpected trainer lock context count: {len(lines)}")
    rows = [
        parse_context_row(lines[i])
        for i in range(min(len(lines), MAX_LOCK_CONTEXTS))
    ]
    return rows, cmd


def read_repo_text(root: Path, rel_path: str) -> str:
    """Return UTF-8 file contents under root or refuse unreadable paths."""
    try:
        return (root / rel_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CheckError(f"{rel_path}: could not read: {error}") from error


def context_stale(root: Path, row: tuple[str, str, str, str]) -> bool:
    """Return True when direct pins or direct lock entries disagree."""
    var, lock, kind, source = row
    if kind == "file":
        pins = direct_pins(read_repo_text(root, source), source)
    else:
        pins = direct_pins(source, var)
    lock_text = read_repo_text(root, lock)
    locked = lock_pins(lock_text, lock)
    if lock_direct_names(lock_text, lock) - pins.keys():
        return True
    items = list(pins.items())
    for i in range(min(len(items), MAX_REQUIREMENT_LINES)):
        name, version = items[i]
        if locked.get(name) != version:
            return True
    return False


def parse_cutoff(value: str, label: str) -> datetime:
    """Return a parsed UTC cutoff timestamp or refuse malformed values."""
    if CUTOFF_VALUE.fullmatch(value) is None:
        raise CheckError(f"{label}: invalid cutoff {value!r}")
    try:
        return datetime.strptime(value, CUTOFF_FORMAT)
    except ValueError as error:
        raise CheckError(f"{label}: invalid cutoff {value!r}: {error}") from error


def move_cutoffs(text: str, names: list[str], cutoff: str) -> str:
    """Return script text with named lock cutoffs set to the candidate cutoff."""
    parse_cutoff(cutoff, "cutoff")
    for i in range(min(len(names), MAX_LOCK_CONTEXTS)):
        name = names[i]
        pattern = re.compile(rf"^{re.escape(name)}=(?P<value>\S*)$", re.MULTILINE)
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            msg = (
                f"{TRAINER_LOCK_SCRIPT}: expected one {name}= line, "
                f"found {len(matches)}"
            )
            raise CheckError(msg)
        match = matches[0]
        parse_cutoff(match["value"], f"{TRAINER_LOCK_SCRIPT}:{name}")
        start, end = match.span("value")
        text = text[:start] + cutoff + text[end:]
    return text


def regenerate_trainer_locks(
    root: Path, hooks: LockHooks | None = None
) -> list[list[str]]:
    """Re-lock stale trainer contexts and return the executed commands."""
    script_path = root / TRAINER_LOCK_SCRIPT
    if not script_path.is_file():
        return []
    active = (
        hooks
        if hooks is not None
        else LockHooks(datetime.now(UTC), fetch_preset, None)
    )
    rows, ctx_cmd = lock_contexts(root, active.env)
    stale = [
        rows[i][0]
        for i in range(min(len(rows), MAX_LOCK_CONTEXTS))
        if context_stale(root, rows[i])
    ]
    if not stale:
        return [ctx_cmd]
    age = renovate_min_release_age(root, active.fetch)
    cutoff = (active.now.astimezone(UTC) - age).strftime(CUTOFF_FORMAT)
    try:
        script_text = script_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise CheckError(f"{TRAINER_LOCK_SCRIPT}: could not read: {error}") from error
    updated = move_cutoffs(script_text, stale, cutoff)
    if updated != script_text:
        script_path.write_bytes(updated.encode("utf-8"))
    write_cmd = ["bash", TRAINER_LOCK_SCRIPT, "--write"]
    run_update_command(write_cmd, root, LOCK_TIMEOUT_SECONDS, active.env)
    return [ctx_cmd, write_cmd]


def format_command(cmd: list[str]) -> list[str]:
    return ["python3" if arg == sys.executable else arg for arg in cmd]


def build_manifest(
    head: str, files: list[dict[str, Any]], commands: list[list[str]]
) -> str:
    manifest = {
        "commands": [format_command(cmd) for cmd in commands],
        "files": sorted(files, key=lambda item: item["path"]),
        "head": head,
        "schema": SCHEMA,
    }
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def snapshot(root: Path, paths: set[str]) -> dict[str, bytes]:
    return {p: (root / p).read_bytes() for p in sorted(paths) if (root / p).is_file()}


def tidy_until_stable(
    root: Path,
    modules: list[str],
    allowed: set[str],
    after_pass: Callable[[Path], None] | None = None,
) -> list[list[str]]:
    """Tidy every module, pass after pass, until a pass changes no generated file."""
    env = os.environ.copy()
    env["GOFLAGS"] = "-mod=mod"
    cmds = [["go", "-C", m, "mod", "tidy"] for m in modules[:MAX_MODULES]]
    before = snapshot(root, allowed)
    for _ in range(MAX_TIDY_PASSES):
        for cmd in cmds:
            run_update_command(cmd, root, MODULE_TIMEOUT_SECONDS, env)
        if after_pass is not None:
            after_pass(root)
        after = snapshot(root, allowed)
        if after == before:
            return cmds
        before = after
    raise CheckError(f"go mod tidy did not converge after {MAX_TIDY_PASSES} passes")


def copy_changed_files(
    root: Path, out: Path, changed: list[str]
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    bound = min(len(changed), MAX_CHANGED_FILES)
    for i in range(bound):
        path_str = changed[i]
        src = root / path_str
        dst = out / "files" / path_str
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dst.write_bytes(data)
        records.append(
            {
                "bytes": len(data),
                "path": path_str,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return records


def write_regenerated(
    out: Path,
    root: Path,
    commands: dict[str, list[str]] | None = None,
    after_pass: Callable[[Path], None] | None = None,
    module_env: dict[str, str] | None = None,
    lock_hooks: LockHooks | None = None,
    digest_hooks: DigestHooks | None = None,
) -> tuple[list[str], str | None]:
    """Run regeneration steps, stage artifacts, and return failures or status."""
    head = run_git(["rev-parse", "HEAD"], root).strip()
    if len(head) != 40 or not all(c in "0123456789abcdefABCDEF" for c in head):
        raise CheckError(f"git rev-parse HEAD returned invalid commit: {head!r}")
    if git_status_bytes(root):
        raise CheckError("working tree is dirty before regeneration")

    executed: list[list[str]] = []
    if commands is not None:
        allowed = set(commands.keys())
        pre_existing = {p for p in allowed if (root / p).is_file()}
        for cmd in commands.values():
            run_update_command(cmd, root, DEFAULT_TIMEOUT_SECONDS)
            executed.append(cmd)
    else:
        allowed = set(get_generated_files(root))
        pre_existing = {p for p in allowed if (root / p).is_file()}
        executed = tidy_until_stable(root, discover_modules(root), allowed, after_pass)
        scalar_script = root / "scripts" / "ci" / "scalar-bundle-update.sh"
        if scalar_script.is_file():
            scalar_cmd = ["bash", "scripts/ci/scalar-bundle-update.sh", "regenerate"]
            run_update_command(scalar_cmd, root, SCALAR_TIMEOUT_SECONDS)
            executed.append(scalar_cmd)
        executed.extend(regenerate_upstream_pins(root))
        executed.extend(regenerate_module_sums(root, module_env))
        executed.extend(regenerate_release_digests(root, digest_hooks))
        executed.extend(regenerate_trainer_locks(root, lock_hooks))

    raw_status = git_status_bytes(root)
    if not raw_status:
        return [], "nothing to regenerate"
    entries = parse_status_entries(raw_status)
    failures, changed = evaluate_changes(entries, root, allowed, pre_existing)
    if failures:
        return failures, None
    if not changed:
        return [], "nothing to regenerate"

    file_records = copy_changed_files(root, out, changed)
    manifest_text = build_manifest(head, file_records, executed)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(manifest_text, encoding="utf-8")
    return [], f"{len(file_records)} files at {head}"


FIXTURE_GENERATED = sorted(
    [
        "a/go.mod",
        "a/go.sum",
        "b/go.mod",
        "b/go.sum",
        "c/go.mod",
        "c/go.sum",
        "core/go.mod",
        "core/go.sum",
        "docs/upstream/c/README.md",
        UPSTREAM_CATALOGUE,
        "go.mod",
        "go.sum",
        TOOL_VERSIONS,
        TRAINER_LOCK_SCRIPT,
        *SCALAR_PATHS,
        *TRAINER_LOCK_PATHS,
    ]
)

# c has no dependencies; a needs c and b needs a, each joined by a local replace.
FIXTURE_FILES = {
    "go.mod": (
        "module example.com/fixture\n\ngo 1.24\n\n"
        "require example.com/c v0.0.0\nreplace example.com/c => ./c\n"
    ),
    "fixture.go": 'package fixture\n\nimport "example.com/c"\n\nconst V = c.V\n',
    "core/go.mod": (
        "module example.com/core\n\ngo 1.24\n\n"
        "require example.com/c v0.0.0\nreplace example.com/c => ../c\n"
    ),
    "core/core.go": 'package core\n\nimport "example.com/c"\n\nconst V = c.V\n',
    "docs/upstream/README.md": (
        "| Package | Pinned version | Authority | Snapshot |\n"
        "| --- | --- | --- | --- |\n"
        "| `example.com/c` | `v0.0.0` | root + core | [c/](c/README.md) |\n"
    ),
    "docs/upstream/c/README.md": (
        "# example.com/c — v0.0.0 snapshot\n\n"
        "Pinned: **v0.0.0** (semconv API: **v0.0.1**)\n"
        "Source: [tagged source](https://github.com/example/c/tree/v0.0.0)\n\n"
        "## Changes since v0.0.1\n\nCurated v0.0.1 prose.\n"
    ),
    "c/go.mod": "module example.com/c\n\ngo 1.24\n",
    "c/c.go": "package c\n\nconst V = 1\n",
    "a/go.mod": (
        "module example.com/a\n\ngo 1.24\n\n"
        "require example.com/c v0.0.0\nreplace example.com/c => ../c\n"
    ),
    "a/a.go": 'package a\n\nimport "example.com/c"\n\nconst A = c.V\n',
    "b/go.mod": (
        "module example.com/b\n\ngo 1.24\n\n"
        "require (\n\texample.com/a v0.0.0\n\texample.com/c v0.0.0\n)\n"
        "replace (\n\texample.com/a => ../a\n\texample.com/c => ../c\n)\n"
    ),
    "b/b.go": 'package b\n\nimport "example.com/a"\n\nconst B = a.A\n',
}

# Renovate's shape: a's requirement moves and a is tidied, b is left as it was.
STALE_A_GO_MOD = (
    "module example.com/a\n\ngo 1.24\n\n"
    "require example.com/c v0.1.0\nreplace example.com/c => ../c\n"
)

STALE_REFUSAL = "/b: go mod download rewrote go.mod or go.sum"


def offline_env() -> dict[str, str]:
    return {**os.environ, "GOPROXY": "off", "GOFLAGS": "-mod=mod", "CI_SCOPE_BASE": ""}


def fixture_tidy(module_dir: Path) -> None:
    subprocess.run(
        ["go", "mod", "tidy"],  # noqa: S607 - go executable on runner PATH.
        cwd=module_dir,
        env=offline_env(),
        check=True,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )


def fixture_verify(repo: Path) -> subprocess.CompletedProcess[str]:
    """The Build job's verify step, on the fixture's own copy of go-modules.sh."""
    return subprocess.run(
        ["bash", "scripts/ci/go-modules.sh", "verify"],  # noqa: S607 - bash executable on runner PATH.
        cwd=repo,
        env=offline_env(),
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        check=False,
    )


def setup_fixture_repo(repo: Path, source_root: Path) -> str:
    """Build and commit the multi-module fixture repository; return its HEAD commit sha."""
    run_git(["init"], repo)
    run_git(["config", "user.name", "Golusoris Tester"], repo)
    run_git(["config", "user.email", "test@example.com"], repo)
    (repo / ".gitignore").write_text("target/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("tracked fixture\n", encoding="utf-8")
    script = repo / "scripts" / "ci" / "go-modules.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(source_root / "scripts" / "ci" / "go-modules.sh", script)
    shutil.copy2(
        source_root / "scripts" / "ci" / "retry.sh",
        repo / "scripts" / "ci" / "retry.sh",
    )
    for rel, text in FIXTURE_FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    for rel in (".", "c", "a", "b", "core"):
        fixture_tidy(repo / rel)
    (repo / ".github").mkdir()
    (repo / DECLARED).write_text("\n".join(FIXTURE_GENERATED) + "\n", encoding="utf-8")
    run_git(["add", "."], repo)
    run_git(["commit", "-m", "initial fixture"], repo)
    return run_git(["rev-parse", "HEAD"], repo).strip()


def reset_fixture_repo(repo: Path) -> None:
    run_git(["reset", "--hard", "HEAD"], repo)
    run_git(["clean", "-fdx"], repo)


def plant_stale_consumer(repo: Path) -> str:
    reset_fixture_repo(repo)
    (repo / "a" / "go.mod").write_text(STALE_A_GO_MOD, encoding="utf-8")
    fixture_tidy(repo / "a")
    run_git(["commit", "-am", "bump a require c v0.1.0"], repo)
    return run_git(["rev-parse", "HEAD"], repo).strip()


def manifest_failures(out: Path, head: str) -> list[str]:
    manifest_file = out / "manifest.json"
    if not manifest_file.is_file():
        return ["case write-repairs manifest.json missing"]
    text = manifest_file.read_text(encoding="utf-8")
    data = json.loads(text)
    failures: list[str] = []
    if data.get("schema") != SCHEMA:
        failures.append(f"case write-repairs bad schema: {data.get('schema')}")
    if data.get("head") != head:
        failures.append(f"case write-repairs bad head: {data.get('head')} != {head}")
    entries = data.get("files", [])
    paths = [entry["path"] for entry in entries]
    if "b/go.mod" not in paths:
        failures.append(f"case write-repairs expected b/go.mod in files, got: {paths}")
    for entry in entries:
        copied = out / "files" / entry["path"]
        data_bytes = copied.read_bytes() if copied.is_file() else None
        if data_bytes is None:
            failures.append(f"case write-repairs copied file missing: {copied}")
        elif hashlib.sha256(data_bytes).hexdigest() != entry["sha256"]:
            failures.append(f"case write-repairs sha256 mismatch for {entry['path']}")
        elif len(data_bytes) != entry["bytes"]:
            failures.append(f"case write-repairs bytes mismatch for {entry['path']}")
    if text != json.dumps(data, indent=2, sort_keys=True) + "\n":
        failures.append(
            "case write-repairs manifest JSON not formatted deterministically"
        )
    return failures


def run_repair_cases(repo: Path, temp_dir: Path) -> tuple[list[str], int, int]:
    """A stale consumer fails verify with the workflow off, and passes after write."""
    failures: list[str] = []
    refused = accepted = 0
    head = plant_stale_consumer(repo)
    proc = fixture_verify(repo)
    if proc.returncode == 0:
        failures.append(
            "case stale-refused: go-modules.sh verify unexpectedly exited 0"
        )
    elif STALE_REFUSAL not in proc.stderr:
        failures.append(f"case stale-refused: wrong refusal: {proc.stderr.strip()}")
    else:
        refused += 1
    run_git(["reset", "--hard", "HEAD"], repo)
    out = temp_dir / "out-repairs"
    fails, _msg = write_regenerated(out, repo)
    failures.extend(f"case write-repairs unexpectedly failed: {f}" for f in fails)
    failures.extend(manifest_failures(out, head))
    proc = fixture_verify(repo)
    if proc.returncode != 0:
        failures.append(
            f"case write-repairs: verify failed after tidy: {proc.stderr.strip()}"
        )
    else:
        accepted += 1
    return failures, refused, accepted


def run_plant_cases_ab(repo: Path, temp_dir: Path) -> tuple[list[str], int]:
    failures: list[str] = []
    refused = 0

    # (a) command edits a non-generated tracked file
    reset_fixture_repo(repo)
    out_a = temp_dir / "out-a"
    cmd_a = {
        "b/go.mod": [
            sys.executable,
            "-c",
            "import pathlib; pathlib.Path('tracked.txt').write_text('dirty\\n')",
        ]
    }
    fails, _ = write_regenerated(out_a, repo, cmd_a)
    if not fails or not any("tracked.txt" in f for f in fails):
        failures.append(f"plant (a) not refused with tracked.txt: {fails}")
    else:
        refused += 1
    if (out_a / "manifest.json").exists():
        failures.append("plant (a) wrote manifest.json")

    # (b) command creates an untracked non-generated file
    reset_fixture_repo(repo)
    out_b = temp_dir / "out-b"
    cmd_b = {
        "b/go.mod": [
            sys.executable,
            "-c",
            "import pathlib; pathlib.Path('untracked.txt').write_text('new\\n')",
        ]
    }
    fails, _ = write_regenerated(out_b, repo, cmd_b)
    if not fails or not any("untracked.txt" in f for f in fails):
        failures.append(f"plant (b) not refused with untracked.txt: {fails}")
    else:
        refused += 1
    if (out_b / "manifest.json").exists():
        failures.append("plant (b) wrote manifest.json")

    return failures, refused


def run_plant_case_scalar_unlisted(repo: Path, temp_dir: Path) -> tuple[list[str], int]:
    """Proves an unlisted path is refused when the scalar step writes outside the table."""
    failures: list[str] = []
    refused = 0

    reset_fixture_repo(repo)
    out_scalar = temp_dir / "out-scalar-unlisted"
    script = repo / "scripts" / "ci" / "scalar-bundle-update.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/usr/bin/env bash\nprintf 'leak\\n' > outside_scalar.txt\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    run_git(["add", "scripts/ci/scalar-bundle-update.sh"], repo)
    run_git(["commit", "-m", "plant scalar script"], repo)

    fails, _ = write_regenerated(out_scalar, repo)
    if not fails or not any("outside_scalar.txt" in f for f in fails):
        failures.append(
            f"scalar unlisted plant not refused with outside_scalar.txt: {fails}"
        )
    else:
        refused += 1
    if (out_scalar / "manifest.json").exists():
        failures.append("scalar unlisted plant wrote manifest.json")

    run_git(["reset", "--hard", "HEAD~1"], repo)
    reset_fixture_repo(repo)
    return failures, refused


def run_upstream_repair_case(repo: Path, temp_dir: Path) -> list[str]:
    reset_fixture_repo(repo)
    original_head = run_git(["rev-parse", "HEAD"], repo).strip()
    paths = ("docs/upstream/README.md", "docs/upstream/c/README.md")
    for path in paths:
        original = FIXTURE_FILES[path]
        # Pin tokens alone move; the semconv pin and curated prose must survive.
        stale = original.replace("v0.0.0", "v0.0.1")
        (repo / path).write_text(stale, encoding="utf-8")
    run_git(["commit", "-am", "plant stale upstream pins"], repo)
    out = temp_dir / "out-upstream"
    fails, _ = write_regenerated(out, repo)
    failures = [f"upstream repair unexpectedly failed: {f}" for f in fails]
    for path in paths:
        copied = out / "files" / path
        if (
            not copied.is_file()
            or copied.read_text(encoding="utf-8") != FIXTURE_FILES[path]
        ):
            failures.append(f"upstream repair did not restore exact contents: {path}")
    proc = subprocess.run(
        ["bash", "scripts/verify-upstream-pins.sh"],  # noqa: S607 - bash executable on runner PATH.
        cwd=repo,
        env=offline_env(),
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        failures.append(f"upstream repair verify failed: {proc.stderr.strip()}")
    if not failures:
        run_git(["commit", "-am", "accept regenerated upstream fixture"], repo)
        fails, message = write_regenerated(temp_dir / "out-upstream-current", repo)
        if fails or message != "nothing to regenerate":
            failures.append(f"upstream repair not idempotent: {fails}, {message}")
    run_git(["reset", "--hard", original_head], repo)
    reset_fixture_repo(repo)
    return failures


def run_upstream_source_plant(repo: Path, temp_dir: Path) -> list[str]:
    reset_fixture_repo(repo)
    path = "docs/upstream/c/README.md"
    text = FIXTURE_FILES[path].replace("[tagged source]", "[unexpected source]")
    (repo / path).write_text(text, encoding="utf-8")
    run_git(["commit", "-am", "plant unexpected upstream Source"], repo)
    out = temp_dir / "out-upstream-source"
    failures: list[str] = []
    try:
        write_regenerated(out, repo)
    except CheckError as error:
        if f"{path}: unexpected Source shape" not in str(error):
            failures.append(f"upstream Source plant wrong refusal: {error}")
    else:
        failures.append("upstream Source plant was not refused")
    if (out / "manifest.json").exists():
        failures.append("upstream Source plant wrote manifest.json")
    run_git(["reset", "--hard", "HEAD~1"], repo)
    reset_fixture_repo(repo)
    return failures


def run_upstream_validation_cases() -> tuple[list[str], int]:
    source = FIXTURE_FILES["docs/upstream/c/README.md"]
    row = FIXTURE_FILES[UPSTREAM_CATALOGUE].splitlines()[-1] + "\n"
    graphs = {"root": {"example.com/c": "v0.0.0"}, "core": {"example.com/c": "v0.0.1"}}
    cases: tuple[tuple[Callable[[], Any], str], ...] = (
        (lambda: upstream_rows(""), "catalogue row count"),
        (lambda: upstream_rows(row * (MAX_UPSTREAM_ROWS + 1)), "catalogue row count"),
        (lambda: upstream_rows(row + row), "duplicate catalogue mapping"),
        (
            lambda: upstream_rows(row.replace("c/README.md", "../README.md")),
            "malformed catalogue row",
        ),
        (
            lambda: upstream_snapshot("# unexpected\n" + source, "v0.0.0", "c"),
            "unexpected heading shape",
        ),
        (
            lambda: upstream_snapshot(source.replace("Pinned:", "Pin:"), "v0.0.0", "c"),
            "unexpected Pinned shape",
        ),
        (
            lambda: upstream_snapshot(source + "\nSource: unexpected\n", "v0.0.0", "c"),
            "unexpected Source shape",
        ),
        (
            lambda: upstream_version("example.com/c", ("root", "core"), graphs),
            "disagreeing module authorities",
        ),
        (
            lambda: upstream_version("example.com/missing", ("root",), graphs),
            "missing or disagreeing",
        ),
        (
            lambda: upstream_version("c", ("root",), {"root": {"c": "branch/main"}}),
            "unsupported module version",
        ),
    )
    failures: list[str] = []
    refused = 0
    for i in range(min(len(cases), MAX_UPSTREAM_ROWS)):
        action, expected = cases[i]
        try:
            action()
        except CheckError as error:
            if expected in str(error):
                refused += 1
            else:
                failures.append(f"upstream validation wrong refusal: {error}")
        else:
            failures.append(f"upstream validation not refused: {expected}")
    for authority, version in (("root", "v0.0.0"), ("core", "v0.0.1")):
        if upstream_version("example.com/c", (authority,), graphs) != version:
            failures.append(f"upstream selected wrong {authority} version")
    return failures, refused


def strip_module_sum_lines(text: str) -> str:
    """Drop module-sum pins and release-digest comments for offline fixtures."""
    plain = text.splitlines()
    raw = text.splitlines(keepends=True)
    return "".join(
        raw[i]
        for i in range(min(len(plain), MAX_TOOL_VERSION_LINES))
        if MODULE_SUM_LINE.fullmatch(plain[i]) is None
        and RELEASE_DIGEST_COMMENT.fullmatch(plain[i]) is None
    )


def fixture_h1(prefix: str, files: dict[str, str]) -> str:
    by_path = {prefix + name: content for name, content in files.items()}
    lines = [
        f"{hashlib.sha256(by_path[name].encode('utf-8')).hexdigest()}  {name}\n"
        for name in sorted(by_path)[:MAX_CHANGED_FILES]
    ]
    summary = hashlib.sha256("".join(lines).encode("utf-8")).digest()
    return "h1:" + base64.b64encode(summary).decode("ascii")


def write_fixture_goproxy(proxy_dir: Path) -> dict[str, str]:
    at_v = proxy_dir / "example.com" / "tool" / "@v"
    at_v.mkdir(parents=True, exist_ok=True)
    versions = ("v0.1.0", "v0.2.0")
    (at_v / "list").write_text("".join(f"{v}\n" for v in versions), encoding="utf-8")
    mod_text = "module example.com/tool\n\ngo 1.24\n"
    sums: dict[str, str] = {}
    for i in range(min(len(versions), MAX_TOOL_VERSION_LINES)):
        v = versions[i]
        info = json.dumps({"Version": v, "Time": "2026-01-01T00:00:00Z"}) + "\n"
        (at_v / f"{v}.info").write_text(info, encoding="utf-8")
        (at_v / f"{v}.mod").write_text(mod_text, encoding="utf-8")
        files = {
            "go.mod": mod_text,
            "tool.go": f'package tool\n\nconst Version = "{v}"\n',
        }
        prefix = f"example.com/tool@{v}/"
        items = list(files.items())
        with zipfile.ZipFile(at_v / f"{v}.zip", "w") as archive:
            for j in range(min(len(items), MAX_CHANGED_FILES)):
                name, content = items[j]
                archive.writestr(prefix + name, content)
        sums[v] = fixture_h1(prefix, files)
    return sums


def _module_sum_pin_cases(sample_sum: str) -> tuple[tuple[str, str], ...]:
    go_comment = "# renovate: datasource=go depName=example.com/tool"
    header = f"{go_comment}\nTOOL_VERSION=v0.1.0\n"
    return (
        ("line-1", f"TOOL_MODULE_SUM={sample_sum}\n"),
        (
            "github-releases-comment",
            (
                "# renovate: datasource=github-releases depName=x/y\n"
                f"TOOL_VERSION=v0.1.0\nTOOL_MODULE_SUM={sample_sum}\n"
            ),
        ),
        (
            "missing-version-line",
            f"{go_comment}\n# missing version\nTOOL_MODULE_SUM={sample_sum}\n",
        ),
        ("trailing-comment", f"{header}TOOL_MODULE_SUM={sample_sum} # checksum\n"),
        ("trailing-space", f"{header}TOOL_MODULE_SUM={sample_sum} \n"),
        ("crlf-sum", header + f"TOOL_MODULE_SUM={sample_sum}\r\n"),
        (
            "line-separator-after-sum",
            header + f"TOOL_MODULE_SUM={sample_sum}{chr(0x2028)}EXTRA=1\n",
        ),
        (
            "cr-separated-header",
            (
                f"# c\r{go_comment}\r"
                f"TOOL_VERSION=v0.1.0\rTOOL_MODULE_SUM={sample_sum}\n"
            ),
        ),
        ("indented-pin", header + f"  TOOL_MODULE_SUM={sample_sum}\n"),
        ("export-pin", header + f"export TOOL_MODULE_SUM={sample_sum}\n"),
        ("hash-after-sum", header + f"TOOL_MODULE_SUM={sample_sum}#c\n"),
        ("quoted-sum", header + f'TOOL_MODULE_SUM="{sample_sum}"\n'),
        ("short-sum", header + "TOOL_MODULE_SUM=h1:abc=\n"),
        (
            "version-hash",
            f"{go_comment}\nTOOL_VERSION=v0.1.0#c\nTOOL_MODULE_SUM={sample_sum}\n",
        ),
        (
            "comment-continuation",
            (
                f"{go_comment} {chr(92)}\n"
                f"TOOL_VERSION=v0.1.0\nTOOL_MODULE_SUM={sample_sum}\n"
            ),
        ),
        (
            "name-continuation",
            header + f"TOOL_MODULE_{chr(92)}\nSUM={sample_sum}\n",
        ),
        (
            "hidden-after-continuation",
            header + f": a{chr(92)}\n#; TOOL_MODULE_SUM={sample_sum}\n",
        ),
        ("lower-case-sum", header + f"tool_module_sum={sample_sum}\n"),
    )


def run_module_sum_refusal_cases(
    repo: Path, temp_dir: Path, env: dict[str, str], sample_sum: str
) -> tuple[list[str], int]:
    pin_cases = _module_sum_pin_cases(sample_sum)
    failures: list[str] = []
    refused = 0
    for i in range(min(len(pin_cases), MAX_TOOL_VERSION_LINES)):
        label, bad_text = pin_cases[i]
        try:
            module_sum_pins(bad_text)
        except CheckError:
            refused += 1
        else:
            failures.append(f"module_sum_pins {label} was not refused")

    write_cases = (
        (
            "v0.3.0",
            "plant missing module version v0.3.0",
            "out-modsum-missing",
            ("example.com/tool@v0.3.0",),
        ),
        (
            "v0.2",
            "plant module version query v0.2",
            "out-modsum-query",
            ("resolved", "v0.2.0"),
        ),
    )
    for i in range(min(len(write_cases), MAX_TOOL_VERSION_LINES)):
        version, commit_msg, out_name, expected_parts = write_cases[i]
        reset_fixture_repo(repo)
        original_head = run_git(["rev-parse", "HEAD"], repo).strip()
        pin = (
            "# renovate: datasource=go depName=example.com/tool versioning=semver\n"
            f"TOOL_VERSION={version}\nTOOL_MODULE_SUM={sample_sum}\n"
        )
        (repo / TOOL_VERSIONS).write_text(pin, encoding="utf-8")
        run_git(["commit", "-am", commit_msg], repo)
        try:
            write_regenerated(temp_dir / out_name, repo, module_env=env)
        except CheckError as error:
            error_text = str(error)
            if all(part in error_text for part in expected_parts):
                refused += 1
            else:
                failures.append(f"module sum {version} wrong refusal: {error}")
        else:
            failures.append(f"module sum {version} was not refused")
        run_git(["reset", "--hard", original_head], repo)
        reset_fixture_repo(repo)
    return failures, refused


def run_module_sum_cases(repo: Path, temp_dir: Path) -> tuple[list[str], int, int]:
    sums = write_fixture_goproxy(temp_dir / "goproxy")
    env = {
        **offline_env(),
        "GOPROXY": (temp_dir / "goproxy").as_uri(),
        "GONOSUMDB": "example.com",
        "GOMODCACHE": str(temp_dir / "modcache"),
        "GOTOOLCHAIN": "local",
        "RETRY_ATTEMPTS": "1",
        "RETRY_DELAY_SECONDS": "0",
    }
    reset_fixture_repo(repo)
    original_head = run_git(["rev-parse", "HEAD"], repo).strip()
    header = (
        "# renovate: datasource=go depName=example.com/tool versioning=semver\n"
        "TOOL_VERSION=v0.2.0\n"
    )
    (repo / TOOL_VERSIONS).write_text(
        f"{header}TOOL_MODULE_SUM={sums['v0.1.0']}\n", encoding="utf-8"
    )
    run_git(["commit", "-am", "plant stale module sum"], repo)
    out = temp_dir / "out-modsum"
    fails, _ = write_regenerated(out, repo, module_env=env)
    failures = [f"module sum repair unexpectedly failed: {f}" for f in fails]
    copied = out / "files" / TOOL_VERSIONS
    expected = f"{header}TOOL_MODULE_SUM={sums['v0.2.0']}\n"
    if not copied.is_file() or copied.read_text(encoding="utf-8") != expected:
        failures.append("module sum repair did not write expected tool-versions.env")
    accepted = 0
    if not failures:
        run_git(["commit", "-am", "accept regenerated module sum"], repo)
        fails2, message = write_regenerated(
            temp_dir / "out-modsum-current", repo, module_env=env
        )
        if fails2 or message != "nothing to regenerate":
            failures.append(f"module sum repair not idempotent: {fails2}, {message}")
        else:
            accepted = 1
    run_git(["reset", "--hard", original_head], repo)
    reset_fixture_repo(repo)
    ref_failures, refused = run_module_sum_refusal_cases(
        repo, temp_dir, env, sums["v0.1.0"]
    )
    failures.extend(ref_failures)
    return failures, refused, accepted


def _stub_digest_hooks() -> DigestHooks:
    """Return deterministic offline DigestHooks hashing URL and ref text."""
    return DigestHooks(
        fetch_sha256=lambda url: hashlib.sha256(url.encode("utf-8")).hexdigest(),
        image_digest=lambda ref: (
            "sha256:" + hashlib.sha256(ref.encode("utf-8")).hexdigest()
        ),
    )


def _fail_digest_hook(target: str) -> str:
    """Refuse any unexpected digest lookup in unannotated boundary fixtures."""
    raise CheckError(f"unexpected digest lookup: {target}")


def _verify_release_digest_manifest(
    out: Path, expected_url: str, expected_ref: str
) -> list[str]:
    """Verify manifest.json records both release digest update commands."""
    manifest_file = out / "manifest.json"
    if not manifest_file.is_file():
        return ["release digest repair did not write manifest.json"]
    manifest = load_json_object(
        manifest_file.read_text(encoding="utf-8"), "manifest.json"
    )
    cmds = manifest.get("commands", [])
    failures: list[str] = []
    if ["sha256-of", expected_url] not in cmds:
        failures.append("release digest manifest missing sha256-of command")
    if _imagetools_command(expected_ref) not in cmds:
        failures.append("release digest manifest missing imagetools command")
    return failures


def _run_release_digest_positive(
    repo: Path, temp_dir: Path, hooks: DigestHooks
) -> tuple[list[str], int]:
    """Verify write_regenerated repairs stale sha256-of and digest-of pins."""
    reset_fixture_repo(repo)
    original_head = run_git(["rev-parse", "HEAD"], repo).strip()
    url_comment = (
        "# sha256-of: "
        "https://example.com/${TOOL_VERSION}/tool-${TOOL_VERSION#v}.tar.gz\n"
    )
    ref_comment = "# digest-of: example.invalid/tool:${TOOL_VERSION#v}\n"
    stale_text = (
        "TOOL_VERSION=v1.2.0\n"
        f"{url_comment}TOOL_SHA256={'0' * 64}\n"
        f"{ref_comment}TOOL_IMAGE_DIGEST=sha256:{'1' * 64}\n"
    )
    expected_url = "https://example.com/v1.2.0/tool-1.2.0.tar.gz"
    expected_ref = "example.invalid/tool:1.2.0"
    expected_text = (
        "TOOL_VERSION=v1.2.0\n"
        f"{url_comment}TOOL_SHA256={hooks.fetch_sha256(expected_url)}\n"
        f"{ref_comment}TOOL_IMAGE_DIGEST={hooks.image_digest(expected_ref)}\n"
    )
    try:
        (repo / TOOL_VERSIONS).write_text(stale_text, encoding="utf-8")
        run_git(["commit", "-am", "plant stale release digests"], repo)
        out = temp_dir / "out-release-digests"
        fails, _ = write_regenerated(out, repo, digest_hooks=hooks)
        failures = [f"release digest repair failed: {f}" for f in fails]
        copied = out / "files" / TOOL_VERSIONS
        if not copied.is_file() or copied.read_text(encoding="utf-8") != expected_text:
            failures.append(
                "release digest repair did not write expected tool-versions.env"
            )
        failures.extend(
            _verify_release_digest_manifest(out, expected_url, expected_ref)
        )
        if failures:
            return failures, 0
        run_git(["commit", "-am", "accept regenerated release digests"], repo)
        fails2, msg = write_regenerated(
            temp_dir / "out-release-digests-current", repo, digest_hooks=hooks
        )
        if fails2 or msg != "nothing to regenerate":
            return [f"release digest repair not idempotent: {fails2}, {msg}"], 0
        return [], 1
    finally:
        run_git(["reset", "--hard", original_head], repo)
        reset_fixture_repo(repo)


def _refuse_release_digest_case(
    temp_dir: Path,
    index: int,
    case: tuple[str, str, DigestHooks, str],
    failures: list[str],
) -> int:
    """Write one invalid tool-versions fixture and return 1 on CheckError."""
    label, content, case_hooks, expected_substr = case
    scratch = temp_dir / f"release-digest-refuse-{index}"
    (scratch / TOOL_VERSIONS).parent.mkdir(parents=True, exist_ok=True)
    (scratch / TOOL_VERSIONS).write_bytes(content.encode("utf-8"))
    try:
        regenerate_release_digests(scratch, case_hooks)
    except CheckError as error:
        if expected_substr in str(error):
            return 1
        failures.append(f"case {label}: wrong CheckError: {error}")
        return 0
    failures.append(f"case {label}: expected CheckError")
    return 0


def _release_digest_expansion_refusals(
    hooks: DigestHooks,
) -> tuple[tuple[str, str, DigestHooks, str], ...]:
    """Return template-expansion fixtures that release digest parsing refuses."""
    zero = "0" * 64
    prefix = f"{TOOL_VERSIONS}:"
    return (
        (
            "undefined-name",
            f"# sha256-of: https://example.com/${{MISSING_NAME}}/a.tgz\nTOOL_SHA256={zero}\n",
            hooks,
            f"{prefix}1:",
        ),
        (
            "percent-strip",
            f"TOOL_VERSION=v1.2.0\n# sha256-of: https://example.com/${{TOOL_VERSION%v}}/a.tgz\nTOOL_SHA256={zero}\n",
            hooks,
            f"{prefix}2:",
        ),
        (
            "bare-dollar",
            f"TOOL_VERSION=v1.2.0\n# sha256-of: https://example.com/$TOOL_VERSION/a.tgz\nTOOL_SHA256={zero}\n",
            hooks,
            f"{prefix}2:",
        ),
        (
            "http-url",
            f"TOOL_VERSION=v1.2.0\n# sha256-of: http://example.com/${{TOOL_VERSION}}/a.tgz\nTOOL_SHA256={zero}\n",
            hooks,
            f"{prefix}2:",
        ),
        (
            "digest-ref-at",
            f"TOOL_VERSION=v1.2.0\n# digest-of: example.invalid/tool:${{TOOL_VERSION#v}}@sha256:{zero}\nTOOL_IMAGE_DIGEST=sha256:{zero}\n",
            hooks,
            f"{prefix}2:",
        ),
    )


def _release_digest_pin_refusals(
    hooks: DigestHooks,
) -> tuple[tuple[str, str, DigestHooks, str], ...]:
    """Return pin-shape, stub-value and trailing-comment refusal fixtures."""
    zero = "0" * 64
    bad_hooks = DigestHooks(lambda _url: "not-a-hex-digest", hooks.image_digest)
    prefix = f"{TOOL_VERSIONS}:"
    return (
        (
            "blank-after-comment",
            f"TOOL_VERSION=v1.2.0\n# sha256-of: https://example.com/${{TOOL_VERSION}}/a.tgz\n\nTOOL_SHA256={zero}\n",
            hooks,
            f"{prefix}2:",
        ),
        (
            "sha256-over-image-digest",
            f"TOOL_VERSION=v1.2.0\n# sha256-of: https://example.com/${{TOOL_VERSION}}/a.tgz\nTOOL_IMAGE_DIGEST=sha256:{zero}\n",
            hooks,
            f"{prefix}2:",
        ),
        (
            "non-hex-stub",
            f"TOOL_VERSION=v1.2.0\n# sha256-of: https://example.com/${{TOOL_VERSION}}/a.tgz\nTOOL_SHA256={zero}\n",
            bad_hooks,
            f"{prefix}3:",
        ),
        (
            "comment-last-line",
            "TOOL_VERSION=v1.2.0\n# sha256-of: https://example.com/${TOOL_VERSION}/a.tgz",
            hooks,
            f"{prefix}2:",
        ),
    )


def _release_digest_refusal_cases(
    hooks: DigestHooks,
) -> tuple[tuple[str, str, DigestHooks, str], ...]:
    """Return negative and trailing-comment boundary fixtures for release digests."""
    return (
        *_release_digest_expansion_refusals(hooks),
        *_release_digest_pin_refusals(hooks),
    )


def _run_release_digest_boundary_cases(
    temp_dir: Path,
) -> tuple[list[str], int, int]:
    """Verify unannotated pins, overridden variables, and sha256_stream bounds."""
    failures: list[str] = []
    refused = 0
    accepted = 0
    uncommented_dir = temp_dir / "release-digest-uncommented"
    (uncommented_dir / TOOL_VERSIONS).parent.mkdir(parents=True, exist_ok=True)
    raw_pin = f"# Renovate-paired digest.\nUNMANAGED_SHA256={'a' * 64}\n".encode()
    pin_path = uncommented_dir / TOOL_VERSIONS
    pin_path.write_bytes(raw_pin)
    fail_hooks = DigestHooks(_fail_digest_hook, _fail_digest_hook)
    cmds = regenerate_release_digests(uncommented_dir, fail_hooks)
    if cmds or pin_path.read_bytes() != raw_pin:
        failures.append("case uncommented-sha256: file or command list changed")
    else:
        accepted += 1

    redef_text = (
        "TOOL_VERSION=v1.0.0\nTOOL_VERSION=v2.0.0\n"
        "# sha256-of: https://example.com/${TOOL_VERSION}/a.tgz\n"
        f"TOOL_SHA256={'0' * 64}\n"
    )
    if [p.source for p in release_digest_pins(redef_text)] != [
        "https://example.com/v2.0.0/a.tgz"
    ]:
        failures.append("case last-definition-wins: wrong expanded URL")
    else:
        accepted += 1

    limit = DIGEST_CHUNK_BYTES
    exact_payload = b"a" * limit
    if (
        sha256_stream(io.BytesIO(exact_payload), limit)
        != hashlib.sha256(exact_payload).hexdigest()
    ):
        failures.append("case sha256-stream-exact: unexpected digest at limit")
    else:
        accepted += 1
    try:
        sha256_stream(io.BytesIO(b"a" * (limit + 1)), limit)
    except CheckError:
        refused += 1
    else:
        failures.append("case sha256-stream-over-limit: expected CheckError")
    return failures, refused, accepted


def run_release_digest_cases(
    repo: Path, temp_dir: Path
) -> tuple[list[str], int, int]:
    """Return failures, refusals, and acceptances for release digest regeneration."""
    hooks = _stub_digest_hooks()
    pos_failures, pos_accepted = _run_release_digest_positive(repo, temp_dir, hooks)
    ref_cases = _release_digest_refusal_cases(hooks)
    ref_failures: list[str] = []
    refused = sum(
        _refuse_release_digest_case(temp_dir, i, ref_cases[i], ref_failures)
        for i in range(min(len(ref_cases), MAX_RELEASE_DIGEST_CASES))
    )
    bnd_failures, bnd_refused, bnd_accepted = _run_release_digest_boundary_cases(
        temp_dir
    )
    failures = [*pos_failures, *ref_failures, *bnd_failures]
    return failures, refused + bnd_refused, pos_accepted + bnd_accepted


def write_trainer_target_fixtures(repo: Path) -> None:
    """Write minimal trainer Dockerfiles, requirements, and Renovate config."""
    dockerfile_text = (
        f'ARG PYTHON_IMAGE="python:3.11.16-slim-bookworm@sha256:{"0" * 64}"\n'
        "FROM ${PYTHON_IMAGE}\n"
    )
    targets = (
        ("ai/tiny/trainers/gemma", "pkg-a==1.0\npkg-c[extra]==1.0\n"),
        ("ai/tiny/trainers/litert", "pkg-c==1.0\n"),
        ("ai/tiny/serve/tflite/sidecar", "pkg-c==1.0\n"),
    )
    for target_dir, req_text in targets:
        target_path = repo / target_dir
        target_path.mkdir(parents=True, exist_ok=True)
        (target_path / "Dockerfile").write_text(dockerfile_text, encoding="utf-8")
        (target_path / "requirements.in").write_text(req_text, encoding="utf-8")
    renovate_data = {
        "extends": ["github>example/preset", ":semanticCommits"],
        "packageRules": [{"matchDatasources": ["docker"], "minimumReleaseAge": None}],
    }
    (repo / RENOVATE_CONFIG).write_text(
        json.dumps(renovate_data, indent=2) + "\n", encoding="utf-8"
    )


def build_trainer_lock_hooks(
    temp_dir: Path, source_root: Path, pip_audit_version: str
) -> LockHooks:
    """Return offline LockHooks wired to the local Docker compile stub."""
    index_path = temp_dir / "stub-index.json"
    index_data = {
        "pkg-a": [
            ["1.0", "2026-01-01T00:00:00Z", ["pkg-b"]],
            ["2.0", "2026-06-05T00:00:00Z", ["pkg-b"]],
        ],
        "pkg-b": [
            ["1.0", "2026-01-01T00:00:00Z", []],
            ["1.0.5", "2026-06-01T00:00:00Z", []],
            ["1.1", "2026-06-09T00:00:00Z", []],
        ],
        "pkg-c": [["1.0", "2026-01-01T00:00:00Z", []]],
        "pip-audit": [[pip_audit_version, "2026-01-01T00:00:00Z", []]],
    }
    index_path.write_text(json.dumps(index_data), encoding="utf-8")
    stub_bin = temp_dir / "stub-bin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    docker_stub = stub_bin / "docker"
    shutil.copy2(
        source_root / "scripts" / "ci" / "testdata" / "regen_docker_stub.py",
        docker_stub,
    )
    docker_stub.chmod(0o755)
    env = {
        **offline_env(),
        "PATH": str(stub_bin) + os.pathsep + os.environ["PATH"],
        "REGEN_STUB_INDEX": str(index_path),
    }
    preset_url = "https://raw.githubusercontent.com/example/preset/HEAD/default.json"

    def fake_fetch(url: str) -> bytes:
        """Return the canned Renovate preset JSON or refuse unknown URLs."""
        if url == preset_url:
            return b'{"minimumReleaseAge": "3 days"}'
        raise CheckError(f"unexpected preset URL: {url}")

    return LockHooks(datetime(2026, 6, 10, 12, 0, tzinfo=UTC), fake_fetch, env)


def setup_trainer_lock_fixture(
    repo: Path, temp_dir: Path, source_root: Path
) -> tuple[list[str], LockHooks | None]:
    """Commit initial trainer locks and return any setup failures with hooks."""
    repo_script = repo / TRAINER_LOCK_SCRIPT
    repo_script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_root / TRAINER_LOCK_SCRIPT, repo_script)
    script_text = repo_script.read_text(encoding="utf-8")
    cutoff_re = re.compile(
        r"^(?P<name>[A-Z][A-Z0-9_]*_LOCK_EXCLUDE_NEWER)=\S+$", re.MULTILINE
    )
    replaced, count = cutoff_re.subn(r"\g<name>=2026-01-15T00:00:00Z", script_text)
    if count != MAX_LOCK_CONTEXTS:
        msg = f"case renovate-shape: expected 4 cutoff replacements, got {count}"
        return [msg], None
    repo_script.write_bytes(replaced.encode("utf-8"))
    audit_match = re.search(
        r"^PIP_AUDIT_VERSION=(?P<version>\S+)$", replaced, re.MULTILINE
    )
    if audit_match is None:
        return ["case renovate-shape: missing PIP_AUDIT_VERSION"], None
    write_trainer_target_fixtures(repo)
    hooks = build_trainer_lock_hooks(temp_dir, source_root, audit_match["version"])
    run_update_command(
        ["bash", TRAINER_LOCK_SCRIPT, "--write"], repo, LOCK_TIMEOUT_SECONDS, hooks.env
    )
    run_git(["add", "-A"], repo)
    run_git(["commit", "-m", "add trainer lock fixture"], repo)
    return [], hooks


def _verify_gemma_regen_scope(out: Path, case_name: str) -> list[str]:
    """Verify gemma cutoff moved and litert, sidecar, and audit locks stayed clean."""
    failures: list[str] = []
    copied_script = out / "files" / TRAINER_LOCK_SCRIPT
    expected_cutoffs = (
        "GEMMA_LOCK_EXCLUDE_NEWER=2026-06-07T12:00:00Z",
        "LITERT_LOCK_EXCLUDE_NEWER=2026-01-15T00:00:00Z",
        "SIDECAR_LOCK_EXCLUDE_NEWER=2026-01-15T00:00:00Z",
        "AUDIT_LOCK_EXCLUDE_NEWER=2026-01-15T00:00:00Z",
    )
    script_lines = (
        set(copied_script.read_text(encoding="utf-8").splitlines())
        if copied_script.is_file()
        else set()
    )
    if not all(
        expected_cutoffs[i] in script_lines
        for i in range(min(len(expected_cutoffs), MAX_LOCK_CONTEXTS))
    ):
        failures.append(f"case {case_name}: unexpected cutoff lines in script")
    unchanged = (
        "ai/tiny/trainers/litert/requirements.lock",
        "ai/tiny/serve/tflite/sidecar/requirements.lock",
        "scripts/ci/tiny-trainer-pip-audit.lock",
    )
    failures.extend(
        f"case {case_name}: unchanged lock written: {unchanged[i]}"
        for i in range(min(len(unchanged), MAX_LOCK_CONTEXTS))
        if (out / "files" / unchanged[i]).exists()
    )
    return failures


def verify_renovate_shape_files(out: Path) -> list[str]:
    """Return verification failures for regenerated Renovate-bump artifacts."""
    failures = _verify_gemma_regen_scope(out, "renovate-shape")
    gemma_lock = out / "files" / "ai/tiny/trainers/gemma/requirements.lock"
    gemma_lines = (
        set(gemma_lock.read_text(encoding="utf-8").splitlines())
        if gemma_lock.is_file()
        else set()
    )
    if "pkg-a==2.0 \\" not in gemma_lines:
        failures.append("case renovate-shape: gemma lock missing pkg-a==2.0")
    return failures


def verify_young_transitive(out: Path) -> tuple[list[str], int]:
    """Return failures and accepted count for transitive release-age filtering."""
    gemma_lock = out / "files" / "ai/tiny/trainers/gemma/requirements.lock"
    lines = (
        set(gemma_lock.read_text(encoding="utf-8").splitlines())
        if gemma_lock.is_file()
        else set()
    )
    if "pkg-b==1.0.5 \\" in lines and "pkg-b==1.1 \\" not in lines:
        return [], 1
    msg = "case young-transitive: expected pkg-b==1.0.5 and no pkg-b==1.1"
    return [msg], 0


def run_trainer_lock_repo_cases(
    repo: Path, temp_dir: Path, source_root: Path
) -> tuple[list[str], int]:
    """Return failures and accepted count for trainer lock regeneration cases."""
    reset_fixture_repo(repo)
    original_head = run_git(["rev-parse", "HEAD"], repo).strip()
    accepted = 0
    try:
        setup_failures, hooks = setup_trainer_lock_fixture(repo, temp_dir, source_root)
        if setup_failures or hooks is None:
            return setup_failures, 0
        (repo / "ai/tiny/trainers/gemma/requirements.in").write_text(
            "pkg-a==2.0\npkg-c[extra]==1.0\n", encoding="utf-8"
        )
        run_git(["commit", "-am", "bump gemma pkg-a to 2.0"], repo)
        out = temp_dir / "out-trainer"
        fails, _ = write_regenerated(out, repo, lock_hooks=hooks)
        shape_failures = [f"case renovate-shape failed: {f}" for f in fails]
        shape_failures.extend(verify_renovate_shape_files(out))
        young_failures, young_accepted = verify_young_transitive(out)
        accepted += young_accepted
        if not shape_failures:
            run_git(["commit", "-am", "accept regenerated trainer lock"], repo)
            fails2, message = write_regenerated(
                temp_dir / "out-trainer-current", repo, lock_hooks=hooks
            )
            if fails2 or message != "nothing to regenerate":
                msg = f"case renovate-shape not idempotent: {fails2}, {message}"
                shape_failures.append(msg)
            else:
                accepted += 1
        return [*shape_failures, *young_failures], accepted
    finally:
        run_git(["reset", "--hard", original_head], repo)
        reset_fixture_repo(repo)


def refuse_release_age_case(
    temp_dir: Path,
    index: int,
    case: tuple[str, dict[str, Any] | None, Callable[[str], bytes]],
    failures: list[str],
) -> int:
    """Write one Renovate fixture and return 1 when release-age validation fails."""
    label, payload, fetch_fn = case
    scratch = temp_dir / f"renovate-refuse-{index}"
    scratch.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (scratch / RENOVATE_CONFIG).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    try:
        renovate_min_release_age(scratch, fetch_fn)
    except CheckError:
        return 1
    failures.append(f"case {label}: expected CheckError")
    return 0


def run_release_age_refusal_cases(temp_dir: Path) -> tuple[list[str], int]:
    """Return failures and refused count for invalid release-age inputs."""

    def fail_fetch(url: str) -> bytes:
        """Refuse every preset fetch attempt with CheckError."""
        raise CheckError(f"fetch failed: {url}")

    def empty_fetch(_url: str) -> bytes:
        """Return an empty JSON object preset body."""
        return b"{}"

    def pypi_override_fetch(_url: str) -> bytes:
        """Return a preset body overriding PyPI minimumReleaseAge."""
        rule = b'{"matchDatasources": ["pypi"], "minimumReleaseAge": "7 days"}'
        return b'{"packageRules": [' + rule + b"]}"

    def earlier_preset_fetch(url: str) -> bytes:
        """Return a PyPI override for preset a and three days otherwise."""
        if "/example/a/" in url:
            return pypi_override_fetch(url)
        return b'{"minimumReleaseAge": "3 days"}'

    def last_preset_fetch(url: str) -> bytes:
        """Return a PyPI override for the last preset and three days otherwise."""
        hit = f"/example/p{MAX_EXTENDS - 1}/" in url
        return pypi_override_fetch(url) if hit else b'{"minimumReleaseAge": "3 days"}'

    diff_rule = {"matchDatasources": ["pypi"], "minimumReleaseAge": "1 day"}
    diff_cfg = {"minimumReleaseAge": "3 days", "packageRules": [diff_rule]}
    bad_ref_cfg = {"extends": ["github>example/preset:other"]}
    top_cfg = {"minimumReleaseAge": "2 days", "extends": ["github>example/preset"]}
    ab_cfg = {"extends": ["github>example/a", "github>example/b"]}
    non_obj_cfg = {"minimumReleaseAge": "3 days", "packageRules": ["not-a-dict"]}
    max_cfg = {"extends": [f"github>example/p{i}" for i in range(MAX_EXTENDS)]}
    cases: tuple[tuple[str, dict[str, Any] | None, Callable[[str], bytes]], ...] = (
        ("missing-renovate-json", None, fail_fetch),
        ("null-release-age", {"minimumReleaseAge": None}, fail_fetch),
        ("bad-release-age-unit", {"minimumReleaseAge": "3 dayz"}, fail_fetch),
        ("preset-fetch-error", {"extends": ["github>example/preset"]}, fail_fetch),
        ("preset-missing-key", {"extends": ["github>example/preset"]}, empty_fetch),
        ("pypi-override-differs", diff_cfg, fail_fetch),
        ("unreadable-preset-ref", bad_ref_cfg, empty_fetch),
        ("top-level-age-preset-pypi-override", top_cfg, pypi_override_fetch),
        ("earlier-preset-pypi-override", ab_cfg, earlier_preset_fetch),
        ("non-object-package-rule", non_obj_cfg, fail_fetch),
        ("last-of-max-presets-pypi-override", max_cfg, last_preset_fetch),
    )
    failures: list[str] = []
    refused = sum(
        refuse_release_age_case(temp_dir, i, cases[i], failures)
        for i in range(min(len(cases), MAX_EXTENDS))
    )
    try:
        direct_pins("pkg-a>=1.0\n", "x")
    except CheckError:
        refused += 1
    else:
        failures.append("case direct-pins-non-exact: expected CheckError")
    return failures, refused


def run_release_age_accepted_cases(temp_dir: Path) -> tuple[list[str], int]:
    """Return failures and accepted count for valid release-age configs."""
    failures: list[str] = []
    accepted = 0
    fetched = False

    def preset_three_days(_url: str) -> bytes:
        """Record a preset fetch and return a three-day minimumReleaseAge."""
        nonlocal fetched
        fetched = True
        return b'{"minimumReleaseAge": "3 days"}'

    top_dir = temp_dir / "renovate-accept-top"
    top_dir.mkdir(parents=True, exist_ok=True)
    top_cfg = {"extends": ["github>example/preset"], "minimumReleaseAge": "2 days"}
    (top_dir / RENOVATE_CONFIG).write_text(json.dumps(top_cfg), encoding="utf-8")
    if renovate_min_release_age(top_dir, preset_three_days) != timedelta(days=2):
        failures.append("case top-level-release-age: expected 2 days")
    elif not fetched:
        failures.append(
            "case top-level-release-age: preset not fetched for packageRules check"
        )
    else:
        accepted += 1

    docker_dir = temp_dir / "renovate-accept-docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    docker_cfg = {
        "minimumReleaseAge": "3 days",
        "packageRules": [{"matchDatasources": ["docker"], "minimumReleaseAge": None}],
    }
    (docker_dir / RENOVATE_CONFIG).write_text(json.dumps(docker_cfg), encoding="utf-8")
    if renovate_min_release_age(docker_dir, preset_three_days) != timedelta(days=3):
        failures.append("case docker-null-package-rule: expected 3 days")
    else:
        accepted += 1
    return failures, accepted


def _refuse_opener_scheme(url: str, failures: list[str]) -> int:
    """Return 1 when _https_opener refuses url before any scheme handler runs."""
    try:
        response = _https_opener().open(url, timeout=1)
    except OSError as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        if "unknown url type" in str(error):
            return 1
        failures.append(f"preset scheme {url} wrong OSError: {error}")
        return 0
    except AttributeError as error:
        failures.append(f"preset scheme {url} returned None response: {error}")
        return 0
    if response is not None:
        response.close()
    failures.append(f"preset scheme {url} was not refused")
    return 0


def run_preset_scheme_cases(temp_dir: Path) -> tuple[list[str], int]:
    """Return failures and refused count for non-HTTPS preset URL schemes."""
    scheme_dir = temp_dir / "preset-scheme"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    planted = scheme_dir / "default.json"
    planted.write_text('{"minimumReleaseAge": "3 days"}\n', encoding="utf-8")
    http_url = "http://raw.githubusercontent.com/example/preset/HEAD/default.json"
    urls = (
        planted.as_uri(),
        http_url,
        "ftp://example.invalid/default.json",
        "data:,{}",
    )
    failures: list[str] = []
    refused = sum(_refuse_opener_scheme(url, failures) for url in urls)
    try:
        fetch_preset(http_url)
    except CheckError:
        refused += 1
    else:
        failures.append(f"fetch_preset did not refuse {http_url}")
    return failures, refused


def _verify_removed_requirement_files(out: Path) -> list[str]:
    """Return verification failures when a removed direct requirement stays locked."""
    failures = _verify_gemma_regen_scope(out, "removed-requirement")
    gemma_lock = out / "files" / "ai/tiny/trainers/gemma/requirements.lock"
    lines = (
        gemma_lock.read_text(encoding="utf-8").splitlines()
        if gemma_lock.is_file()
        else []
    )
    if "pkg-c==1.0 \\" not in lines:
        failures.append("case removed-requirement: gemma lock missing pkg-c==1.0")
    if any(
        lines[i].startswith("pkg-a==")
        for i in range(min(len(lines), MAX_LOCK_LINES))
    ):
        failures.append("case removed-requirement: gemma lock kept removed pkg-a")
    return failures


def _check_lock_direct_names() -> tuple[list[str], int]:
    """Verify lock_direct_names extracts single-line and multi-line direct stanzas."""
    sample = (
        "keras-hub==0.32.0 \\\n"
        "    --hash=sha256:aaa\n"
        "    # via -r requirements.in\n"
        "tensorflow==2.21.0 \\\n"
        "    --hash=sha256:bbb\n"
        "    # via\n"
        "    #   -r requirements.in\n"
        "    #   tensorflow-text\n"
        "keras==3.15.1 \\\n"
        "    --hash=sha256:ccc\n"
        "    # via\n"
        "    #   keras-hub\n"
        "    #   tensorflow\n"
    )
    if lock_direct_names(sample, "sample.lock") != {"keras-hub", "tensorflow"}:
        return ["case lock-direct-names: expected keras-hub and tensorflow"], 0
    return [], 1


def run_removed_requirement_case(
    repo: Path, temp_dir: Path, source_root: Path
) -> tuple[list[str], int]:
    """Return failures and accepted count when a direct requirement is removed."""
    unit_failures, unit_accepted = _check_lock_direct_names()
    case_dir = temp_dir / "removed-requirement"
    case_dir.mkdir(parents=True, exist_ok=True)
    reset_fixture_repo(repo)
    original_head = run_git(["rev-parse", "HEAD"], repo).strip()
    try:
        setup_failures, hooks = setup_trainer_lock_fixture(repo, case_dir, source_root)
        if setup_failures or hooks is None:
            return [*unit_failures, *setup_failures], unit_accepted
        (repo / "ai/tiny/trainers/gemma/requirements.in").write_text(
            "pkg-c[extra]==1.0\n", encoding="utf-8"
        )
        run_git(["commit", "-am", "remove gemma pkg-a"], repo)
        out = case_dir / "out-removed"
        fails, _ = write_regenerated(out, repo, lock_hooks=hooks)
        repo_failures = [
            f"case removed-requirement failed: {fails[i]}"
            for i in range(min(len(fails), MAX_CHANGED_FILES))
        ]
        repo_failures.extend(_verify_removed_requirement_files(out))
        repo_accepted = 0 if repo_failures else 1
        return [*unit_failures, *repo_failures], unit_accepted + repo_accepted
    finally:
        run_git(["reset", "--hard", original_head], repo)
        reset_fixture_repo(repo)


def _rule_age_config(datasources: list[Any]) -> dict[str, Any]:
    """Return a Renovate config with a 7-day packageRule on datasources."""
    rule = {"matchDatasources": datasources, "minimumReleaseAge": "7 days"}
    return {"minimumReleaseAge": "3 days", "packageRules": [rule]}


def run_datasource_pattern_cases(
    temp_dir: Path,
) -> tuple[list[str], int, int]:
    """Return failures, refusals, and acceptances for matchDatasources patterns."""
    pattern_dir = temp_dir / "datasource-patterns"

    def preset_fetch(_url: str) -> bytes:
        """Return a preset with a negated matchDatasources minimumReleaseAge."""
        return json.dumps(_rule_age_config(["!npm"])).encode("utf-8")

    bad: tuple[list[Any], ...] = (["*"], ["/^pypi$/"], ["!docker"], ["py*"], [1])
    cases: list[tuple[str, dict[str, Any] | None, Callable[[str], bytes]]] = [
        (f"datasource-pattern-{i}", _rule_age_config(bad[i]), preset_fetch)
        for i in range(min(len(bad), MAX_DATASOURCES))
    ]
    preset_cfg: dict[str, Any] = {"extends": ["github>example/preset"]}
    cases.append(("preset-negated-datasource", preset_cfg, preset_fetch))
    failures: list[str] = []
    refused = sum(
        refuse_release_age_case(pattern_dir, i, cases[i], failures)
        for i in range(min(len(cases), MAX_EXTENDS))
    )
    accept_dir = pattern_dir / "accept-github-releases"
    accept_dir.mkdir(parents=True, exist_ok=True)
    (accept_dir / RENOVATE_CONFIG).write_text(
        json.dumps(_rule_age_config(["github-releases"])), encoding="utf-8"
    )
    accepted = 0
    if renovate_min_release_age(accept_dir, preset_fetch) != timedelta(days=3):
        failures.append("case github-releases-package-rule: expected 3 days")
    else:
        accepted = 1
    return failures, refused, accepted


def run_move_cutoff_cases() -> tuple[list[str], int]:
    """Return failures and accepted count for move_cutoffs replacement."""
    script_text = (
        "GEMMA_LOCK_EXCLUDE_NEWER=2026-11-23T12:00:00Z\n"
        "LITERT_LOCK_EXCLUDE_NEWER=2026-01-01T00:00:00Z\n"
        "SIDECAR_LOCK_EXCLUDE_NEWER=2026-11-23T12:00:00Z\n"
    )
    expected = (
        "GEMMA_LOCK_EXCLUDE_NEWER=2026-11-17T12:00:00Z\n"
        "LITERT_LOCK_EXCLUDE_NEWER=2026-11-17T12:00:00Z\n"
        "SIDECAR_LOCK_EXCLUDE_NEWER=2026-11-23T12:00:00Z\n"
    )
    updated = move_cutoffs(
        script_text,
        ["GEMMA_LOCK_EXCLUDE_NEWER", "LITERT_LOCK_EXCLUDE_NEWER"],
        "2026-11-17T12:00:00Z",
    )
    if updated != expected:
        return ["case move-cutoffs: unexpected script text after cutoff update"], 0
    return [], 1


def run_trainer_lock_cases(
    repo: Path, temp_dir: Path, source_root: Path | None = None
) -> tuple[list[str], int, int]:
    """Return combined failures, refusals, and acceptances for trainer locks."""
    actual_root = (
        source_root
        if source_root is not None
        else Path(__file__).resolve().parents[2]
    )
    repo_failures, repo_accepted = run_trainer_lock_repo_cases(
        repo, temp_dir, actual_root
    )
    rem_failures, rem_accepted = run_removed_requirement_case(
        repo, temp_dir, actual_root
    )
    ref_failures, refused = run_release_age_refusal_cases(temp_dir)
    ds_failures, ds_refused, ds_accepted = run_datasource_pattern_cases(temp_dir)
    scheme_failures, scheme_refused = run_preset_scheme_cases(temp_dir)
    acc_failures, unit_accepted = run_release_age_accepted_cases(temp_dir)
    cut_failures, cut_accepted = run_move_cutoff_cases()
    failures = [
        *repo_failures,
        *rem_failures,
        *ref_failures,
        *ds_failures,
        *scheme_failures,
        *acc_failures,
        *cut_failures,
    ]
    total_refused = refused + ds_refused + scheme_refused
    total_accepted = (
        repo_accepted + rem_accepted + ds_accepted + unit_accepted + cut_accepted
    )
    return failures, total_refused, total_accepted


def run_plant_case_c(repo: Path, temp_dir: Path) -> tuple[list[str], int]:
    failures: list[str] = []
    refused = 0

    # (c) command deletes a generated file
    reset_fixture_repo(repo)
    out_c = temp_dir / "out-c"
    cmd_c = {
        "b/go.mod": [
            sys.executable,
            "-c",
            "import pathlib; pathlib.Path('b/go.mod').unlink()",
        ]
    }
    fails, _ = write_regenerated(out_c, repo, cmd_c)
    if not fails or not any("b/go.mod" in f for f in fails):
        failures.append(f"plant (c) not refused with deleted generated file: {fails}")
    else:
        refused += 1
    if (out_c / "manifest.json").exists():
        failures.append("plant (c) wrote manifest.json")

    return failures, refused


def run_plant_cases_de(repo: Path, temp_dir: Path) -> tuple[list[str], int]:
    failures: list[str] = []
    refused = 0

    # (d) command exits 1
    reset_fixture_repo(repo)
    out_d = temp_dir / "out-d"
    cmd_d = {"b/go.mod": [sys.executable, "-c", "import sys; sys.exit(1)"]}
    refused_d = False
    try:
        write_regenerated(out_d, repo, cmd_d)
    except CheckError as error:
        if "sys.exit(1)" in str(error):
            refused_d = True
        else:
            failures.append(f"plant (d) error did not name command: {error}")
    if not refused_d:
        failures.append("plant (d) command exit 1 was not refused")
    else:
        refused += 1
    if (out_d / "manifest.json").exists():
        failures.append("plant (d) wrote manifest.json")

    # (e) tree dirty before run
    reset_fixture_repo(repo)
    (repo / "dirty_pre.txt").write_text("pre-existing dirty file\n", encoding="utf-8")
    out_e = temp_dir / "out-e"
    cmd_e = {"b/go.mod": [sys.executable, "-c", ""]}
    refused_e = False
    try:
        write_regenerated(out_e, repo, cmd_e)
    except CheckError as error:
        if "dirty" in str(error):
            refused_e = True
        else:
            failures.append(f"plant (e) error did not mention dirty: {error}")
    if not refused_e:
        failures.append("plant (e) dirty tree was not refused")
    else:
        refused += 1
    if (out_e / "manifest.json").exists():
        failures.append("plant (e) wrote manifest.json")

    return failures, refused


def append_comment(root: Path) -> None:
    """A planted step that changes b/go.mod on every pass, so tidy never settles."""
    with (root / "b" / "go.mod").open("a", encoding="utf-8") as handle:
        handle.write("// pass\n")


def run_plant_case_f_convergence(repo: Path, temp_dir: Path) -> tuple[list[str], int]:
    failures: list[str] = []
    refused = 0

    # (f) non-converging update command
    reset_fixture_repo(repo)
    out_f = temp_dir / "out-f"
    refused_f = False
    try:
        write_regenerated(out_f, repo, after_pass=append_comment)
    except CheckError as error:
        if "converge" in str(error):
            refused_f = True
        else:
            failures.append(f"plant (f) error did not mention converge: {error}")
    if not refused_f:
        failures.append("plant (f) non-convergence was not refused")
    else:
        refused += 1
    if (out_f / "manifest.json").exists():
        failures.append("plant (f) wrote manifest.json")

    return failures, refused


def run_accepted_cases_gh(repo: Path, temp_dir: Path) -> tuple[list[str], int]:
    failures: list[str] = []
    accepted = 0

    # (g) command writes nothing
    reset_fixture_repo(repo)
    out_g = temp_dir / "out-g"
    cmd_g = {"b/go.mod": [sys.executable, "-c", ""]}
    fails, msg = write_regenerated(out_g, repo, cmd_g)
    if fails:
        failures.append(f"case (g) unexpectedly failed: {fails}")
    elif msg != "nothing to regenerate":
        failures.append(f"case (g) expected 'nothing to regenerate', got {msg!r}")
    else:
        accepted += 1
    if (out_g / "manifest.json").exists() or (out_g / "files").exists():
        failures.append("case (g) wrote output to DIR")

    # (h) command writes under ignored target/
    reset_fixture_repo(repo)
    out_h = temp_dir / "out-h"
    cmd_h = {
        "b/go.mod": [
            sys.executable,
            "-c",
            (
                "import pathlib; p = pathlib.Path('target/t.txt'); "
                "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('build\\n')"
            ),
        ]
    }
    fails, msg = write_regenerated(out_h, repo, cmd_h)
    if fails:
        failures.append(f"case (h) unexpectedly failed: {fails}")
    elif msg != "nothing to regenerate":
        failures.append(f"case (h) expected 'nothing to regenerate', got {msg!r}")
    else:
        accepted += 1
    if (out_h / "manifest.json").exists() or (out_h / "files").exists():
        failures.append("case (h) wrote output to DIR")

    return failures, accepted


def run_declared_cases(repo: Path, generated: list[str]) -> tuple[list[str], int, int]:
    failures: list[str] = []
    refused = 0
    accepted = 0

    cases = (
        ("# comment\n\n" + "\n".join(generated) + "\n", False),
        ("\n".join(generated[:-1]) + "\n", True),
        ("\n".join(generated) + "\ntracked.txt\n", True),
        ("\n".join([*generated, generated[0]]) + "\n", True),
        ("\n".join(p for p in generated if p not in SCALAR_PATHS) + "\n", True),
    )
    for text, should_refuse in cases:
        (repo / ".github").mkdir(parents=True, exist_ok=True)
        (repo / DECLARED).write_text(text, encoding="utf-8")
        found = check_declared(repo, generated)
        if bool(found) != should_refuse:
            failures.append(
                f"declared list {text!r}: refused={bool(found)}, expected {should_refuse}"
            )
        elif should_refuse:
            refused += 1
        else:
            accepted += 1

    return failures, refused, accepted


def _stage_upstream_fixture(repo: Path, root: Path) -> None:
    """Copy the upstream pin verifier and stripped tool-versions into repo."""
    verifier = repo / "scripts/verify-upstream-pins.sh"
    shutil.copy2(root / "scripts/verify-upstream-pins.sh", verifier)
    (repo / "tools").mkdir()
    source_tools = (root / TOOL_VERSIONS).read_text(encoding="utf-8")
    release_digest_pins(source_tools)
    (repo / TOOL_VERSIONS).write_text(
        strip_module_sum_lines(source_tools), encoding="utf-8"
    )
    run_git(["add", "scripts/verify-upstream-pins.sh", TOOL_VERSIONS], repo)
    run_git(["commit", "-m", "add upstream fixture verifier"], repo)


def selftest(root: Path | None = None) -> tuple[list[str], int, int]:
    """Run all fixture selftests and return failures, refusals, and acceptances."""
    if root is None:
        root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(
        prefix="golusoris-regen-selftest-"
    ) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        repo = temp_dir / "repo"
        repo.mkdir()
        setup_fixture_repo(repo, root)
        generated = list(FIXTURE_GENERATED)

        failures, refused_rep, accepted_rep = run_repair_cases(repo, temp_dir)
        _stage_upstream_fixture(repo, root)
        upstream_repair = run_upstream_repair_case(repo, temp_dir)
        upstream_source = run_upstream_source_plant(repo, temp_dir)
        failures.extend([*upstream_repair, *upstream_source])
        upstream_validation, refused_upstream = run_upstream_validation_cases()
        failures.extend(upstream_validation)
        fails_ms, refused_ms, accepted_ms = run_module_sum_cases(repo, temp_dir)
        failures.extend(fails_ms)
        fails_rd, refused_rd, accepted_rd = run_release_digest_cases(repo, temp_dir)
        failures.extend(fails_rd)
        fails_tl, refused_tl, accepted_tl = run_trainer_lock_cases(
            repo, temp_dir, root
        )
        failures.extend(fails_tl)
        fails_ab, refused_ab = run_plant_cases_ab(repo, temp_dir)
        failures.extend(fails_ab)
        fails_sc, refused_sc = run_plant_case_scalar_unlisted(repo, temp_dir)
        failures.extend(fails_sc)
        fails_c, refused_c = run_plant_case_c(repo, temp_dir)
        failures.extend(fails_c)
        fails_de, refused_de = run_plant_cases_de(repo, temp_dir)
        failures.extend(fails_de)
        fails_f, refused_f = run_plant_case_f_convergence(repo, temp_dir)
        failures.extend(fails_f)
        fails_gh, accepted_gh = run_accepted_cases_gh(repo, temp_dir)
        failures.extend(fails_gh)
        fails_decl, refused_decl, accepted_decl = run_declared_cases(repo, generated)
        failures.extend(fails_decl)

        total_refused = refused_rep + refused_ab + refused_sc + refused_c + refused_de
        total_refused += refused_f + refused_decl + int(not upstream_source)
        total_refused += refused_upstream + refused_ms + refused_rd + refused_tl
        total_accepted = accepted_rep + accepted_gh + accepted_decl + accepted_ms
        total_accepted += int(not upstream_repair) + accepted_rd + accepted_tl
        return failures, total_refused, total_accepted


def report(failures: list[str]) -> bool:
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return bool(failures)


def run_selftest(root: Path) -> int:
    failures, refused, accepted = selftest(root)
    if report(failures):
        return 1
    print(f"PASS: regenerate selftest ({refused} plants refused, {accepted} accepted)")
    return 0


def run_check(root: Path) -> int:
    if report(check_declared(root)):
        return 1
    count = len(get_generated_files(root))
    print(f"PASS: regenerate check ({count} generated files, {DECLARED} equal)")
    return 0


def run_write(root: Path, out_arg: Path | None) -> int:
    if not out_arg:
        raise CheckError("--out DIR is required for write")
    if report(check_declared(root)):
        return 1
    failures, message = write_regenerated(out_arg.resolve(), root)
    if report(failures):
        return 1
    if message == "nothing to regenerate":
        print("PASS: nothing to regenerate")
    else:
        print(f"PASS: regenerate write ({message})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=("write", "check", "selftest"))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "selftest":
            return run_selftest(root)
        if args.command == "check":
            return run_check(root)
        return run_write(root, args.out)
    except CheckError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
