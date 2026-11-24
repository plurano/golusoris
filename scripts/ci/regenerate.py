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
MODULE_SUM_LINE = re.compile(r"(?P<name>[A-Z][A-Z0-9_]*)_MODULE_SUM=(?P<sum>\S*)")
MODULE_SUM_PREFIX = re.compile(r"[A-Z][A-Z0-9_]*_MODULE_SUM=")
MODULE_SUM_VALUE = re.compile(r"h1:[A-Za-z0-9+/]{43}=")
GO_PIN_COMMENT = re.compile(
    r"# renovate: datasource=go depName=(?P<module>\S+)(?: .*)?"
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
CUTOFF_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CUTOFF_VALUE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class LockHooks(NamedTuple):
    """Injectable clock, preset fetcher, and subprocess environment for lock regen."""

    now: datetime
    fetch: Callable[[str], bytes]
    env: dict[str, str] | None


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


def module_sum_pins(text: str) -> list[tuple[str, str, int]]:
    lines = text.splitlines()
    if len(lines) > MAX_TOOL_VERSION_LINES:
        raise CheckError(f"{TOOL_VERSIONS} exceeds {MAX_TOOL_VERSION_LINES} lines")
    pins: list[tuple[str, str, int]] = []
    for i in range(min(len(lines), MAX_TOOL_VERSION_LINES)):
        sum_match = MODULE_SUM_LINE.fullmatch(lines[i])
        if sum_match is None:
            if MODULE_SUM_PREFIX.match(lines[i]) is not None:
                msg = (
                    f"{TOOL_VERSIONS}:{i + 1}: module sum pin must be "
                    "NAME_MODULE_SUM=<sum> with no comment or trailing space"
                )
                raise CheckError(msg)
            continue
        name = sum_match["name"]
        prefix = f"{name}_VERSION="
        comment = GO_PIN_COMMENT.fullmatch(lines[i - 2]) if i >= 2 else None
        version = (
            lines[i - 1].removeprefix(prefix)
            if i >= 2 and lines[i - 1].startswith(prefix)
            else ""
        )
        if comment is None or not version or any(ch.isspace() for ch in version):
            msg = (
                f"{TOOL_VERSIONS}:{i + 1}: {name}_MODULE_SUM needs {name}_VERSION "
                "and a datasource=go renovate comment above it"
            )
            raise CheckError(msg)
        pins.append((comment["module"], version, i))
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
    lines = text.splitlines()
    raw_lines = text.splitlines(keepends=True)
    commands: list[list[str]] = []
    for i in range(min(len(pins), MAX_TOOL_VERSION_LINES)):
        module, version, line_idx = pins[i]
        new_sum, cmd = go_module_sum(root, module, version, env)
        commands.append(cmd)
        match = MODULE_SUM_LINE.fullmatch(lines[line_idx])
        if match is not None and match["sum"] != new_sum:
            ending = raw_lines[line_idx][len(lines[line_idx]) :]
            raw_lines[line_idx] = f"{match['name']}_MODULE_SUM={new_sum}{ending}"
    updated = "".join(raw_lines)
    if updated != text:
        path.write_bytes(updated.encode("utf-8"))
    return commands


def _https_opener() -> urllib.request.OpenerDirector:
    """Return a redirect-free urllib opener restricted to HTTPS URLs."""
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.UnknownHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


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


def check_pypi_package_rule(rule: object, source: str, base: timedelta) -> None:
    """Refuse a non-object packageRule or conflicting PyPI minimumReleaseAge."""
    if not isinstance(rule, dict):
        raise CheckError(f"{source}: packageRule is not a JSON object")
    if "minimumReleaseAge" not in rule:
        return
    datasources = rule.get("matchDatasources")
    if datasources is not None:
        if not isinstance(datasources, list):
            raise CheckError(f"{source}: invalid packageRule matchDatasources")
        if "pypi" not in datasources:
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
    """Return True when any direct pin disagrees with its compiled lockfile."""
    var, lock, kind, source = row
    if kind == "file":
        pins = direct_pins(read_repo_text(root, source), source)
    else:
        pins = direct_pins(source, var)
    locked = lock_pins(read_repo_text(root, lock), lock)
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
    """Return script text with named lock cutoffs advanced when newer."""
    cutoff_dt = parse_cutoff(cutoff, "cutoff")
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
        old_value = match["value"]
        old_dt = parse_cutoff(old_value, f"{TRAINER_LOCK_SCRIPT}:{name}")
        new_value = cutoff if cutoff_dt > old_dt else old_value
        start, end = match.span("value")
        text = text[:start] + new_value + text[end:]
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
    plain = text.splitlines()
    raw = text.splitlines(keepends=True)
    return "".join(
        raw[i]
        for i in range(min(len(plain), MAX_TOOL_VERSION_LINES))
        if MODULE_SUM_LINE.fullmatch(plain[i]) is None
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


def run_module_sum_refusal_cases(
    repo: Path, temp_dir: Path, env: dict[str, str], sample_sum: str
) -> tuple[list[str], int]:
    pin_cases = (
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
            (
                "# renovate: datasource=go depName=example.com/tool\n"
                f"# missing version\nTOOL_MODULE_SUM={sample_sum}\n"
            ),
        ),
        ("trailing-comment", (
            "# renovate: datasource=go depName=example.com/tool\n"
            f"TOOL_VERSION=v0.1.0\nTOOL_MODULE_SUM={sample_sum} # checksum\n"
        )),
        ("trailing-space", (
            "# renovate: datasource=go depName=example.com/tool\n"
            f"TOOL_VERSION=v0.1.0\nTOOL_MODULE_SUM={sample_sum} \n"
        )),
    )
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

    reset_fixture_repo(repo)
    original_head = run_git(["rev-parse", "HEAD"], repo).strip()
    absent_pin = (
        "# renovate: datasource=go depName=example.com/tool versioning=semver\n"
        f"TOOL_VERSION=v0.3.0\nTOOL_MODULE_SUM={sample_sum}\n"
    )
    (repo / TOOL_VERSIONS).write_text(absent_pin, encoding="utf-8")
    run_git(["commit", "-am", "plant missing module version v0.3.0"], repo)
    try:
        write_regenerated(temp_dir / "out-modsum-missing", repo, module_env=env)
    except CheckError as error:
        if "example.com/tool@v0.3.0" in str(error):
            refused += 1
        else:
            failures.append(f"module sum v0.3.0 wrong refusal: {error}")
    else:
        failures.append("module sum v0.3.0 absent version was not refused")
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


def verify_renovate_shape_files(out: Path) -> list[str]:
    """Return verification failures for regenerated Renovate-bump artifacts."""
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
    if not all(line in script_lines for line in expected_cutoffs):
        failures.append("case renovate-shape: unexpected cutoff lines in script")
    gemma_lock = out / "files" / "ai/tiny/trainers/gemma/requirements.lock"
    gemma_lines = (
        set(gemma_lock.read_text(encoding="utf-8").splitlines())
        if gemma_lock.is_file()
        else set()
    )
    if "pkg-a==2.0 \\" not in gemma_lines:
        failures.append("case renovate-shape: gemma lock missing pkg-a==2.0")
    unchanged = (
        "ai/tiny/trainers/litert/requirements.lock",
        "ai/tiny/serve/tflite/sidecar/requirements.lock",
        "scripts/ci/tiny-trainer-pip-audit.lock",
    )
    failures.extend(
        f"case renovate-shape: unchanged lock written: {rel}"
        for rel in unchanged
        if (out / "files" / rel).exists()
    )
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
    ref_failures, refused = run_release_age_refusal_cases(temp_dir)
    scheme_failures, scheme_refused = run_preset_scheme_cases(temp_dir)
    acc_failures, unit_accepted = run_release_age_accepted_cases(temp_dir)
    return (
        [*repo_failures, *ref_failures, *scheme_failures, *acc_failures],
        refused + scheme_refused,
        repo_accepted + unit_accepted,
    )


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
        verifier = repo / "scripts/verify-upstream-pins.sh"
        shutil.copy2(root / "scripts/verify-upstream-pins.sh", verifier)
        (repo / "tools").mkdir()
        source_tools = (root / TOOL_VERSIONS).read_text(encoding="utf-8")
        (repo / TOOL_VERSIONS).write_text(
            strip_module_sum_lines(source_tools), encoding="utf-8"
        )
        run_git(["add", "scripts/verify-upstream-pins.sh", TOOL_VERSIONS], repo)
        run_git(["commit", "-m", "add upstream fixture verifier"], repo)
        upstream_repair = run_upstream_repair_case(repo, temp_dir)
        upstream_source = run_upstream_source_plant(repo, temp_dir)
        failures.extend(upstream_repair + upstream_source)
        upstream_validation, refused_upstream = run_upstream_validation_cases()
        failures.extend(upstream_validation)
        fails_ms, refused_ms, accepted_ms = run_module_sum_cases(repo, temp_dir)
        failures.extend(fails_ms)
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
        total_refused += refused_upstream + refused_ms + refused_tl
        total_accepted = accepted_rep + accepted_gh + accepted_decl + accepted_ms
        total_accepted += int(not upstream_repair) + accepted_tl
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
