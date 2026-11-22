#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Fail when a SKILL.md frontmatter name or description breaks the Agent Skills rules.

Sources:
- Agent Skills specification (agentskills.io/specification): `name` is 1-64 characters of
  lowercase a-z, 0-9 and single hyphens, with no leading or trailing hyphen and equal to its
  parent directory name; `description` is 1-1024 characters.
- Claude Agent Skills field requirements
  (platform.claude.com/docs/en/agents-and-tools/agent-skills/overview): `name` must not contain
  the reserved words `anthropic` or `claude`, and `description` must not contain XML tags.

Frontmatter must be enclosed in `---` lines and hold only blank lines, `#` comments, list items
and top-level mapping keys at column 0. `name` and `description` accept only single-line plain
scalars, single-quoted scalars without inner `'`, or double-quoted scalars without `"` or `\\`;
multi-line values and plain scalars that YAML 1.1 or 1.2 resolves as non-strings (null, bool,
int, float, timestamp, merge, value) are rejected.

Symlinks and non-regular index entries are rejected without following or reading them, and files
over SIZE_MAX_BYTES (128 KiB) fail before reading past the cap. Default (pre-commit) mode reads
tracked files from the git index and untracked non-ignored files from disk; `--worktree` reads
non-ignored files from the working tree.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

import repo_git

NAME = "SKILL.md"
NAME_MAX = 64
DESCRIPTION_MAX = 1024
SIZE_MAX_BYTES = 128 * 1024
RESERVED_WORDS = ("anthropic", "claude")
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
YAML_NON_STRING = re.compile(
    r"~|null|Null|NULL|y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF"
    r"|[-+]?(?:0b[01_]+|0o[0-7_]+|0x[0-9a-fA-F_]+|[0-9][0-9_]*|[1-9][0-9_]*(?::[0-5]?[0-9])+)"
    r"|[-+]?(?:\.[0-9_]+|[0-9][0-9_]*(?:\.[0-9_]*)?)(?:[eE][-+]?[0-9_]+)?"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)?"
    r"|<<|="
)

_KEY_LINE = re.compile(r"^(name|description):(?:[ \t]+(.*))?$")
_ANY_KEY_LINE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*:(?:[ \t]|$)")
_XML_TAG = re.compile(r"<\s*/?\s*[A-Za-z][^<>]*>")
_PLAIN_FORBIDDEN_START = frozenset("-?:,[]{}#&*!|>%@'\"`")


def _parse_quoted(value: str) -> str | None:
    """Return the inner text of an escape-free single- or double-quoted scalar, or None."""
    quote = value[0]
    if len(value) < 2 or not value.endswith(quote):
        return None
    inner = value[1:-1]
    if quote == '"':
        return None if ('"' in inner or "\\" in inner) else inner
    return None if "'" in inner else inner


def _parse_plain(value: str) -> str | None:
    """Return value when it is an allowed single-line plain scalar, or None."""
    if value[0] in _PLAIN_FORBIDDEN_START or value.endswith(":"):
        return None
    return None if (": " in value or " #" in value) else value


def _parse_scalar(raw: str | None, *, indented: bool) -> tuple[str, bool] | None:
    """Parse a single-line plain, single-quoted or double-quoted scalar as (text, quoted)."""
    if indented:
        return None
    value = "" if raw is None else raw.rstrip(" \t")
    if value == "":
        return "", False
    if value[0] in ('"', "'"):
        inner = _parse_quoted(value)
        return None if inner is None else (inner, True)
    plain = _parse_plain(value)
    return None if plain is None else (plain, False)


def _frontmatter_lines(path: str, text: str) -> tuple[list[str] | None, list[str]]:
    """Return the frontmatter lines inside `---` fences, or None with the fence finding."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0] != "---":
        return None, [f"{path}: no frontmatter"]
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return lines[1:index], []
    return None, [f"{path}: unterminated frontmatter"]


def _has_continuation(frontmatter: list[str], start: int) -> bool:
    """Return whether the next non-blank, non-column-0-comment line starts with space or tab."""
    for index in range(start, len(frontmatter)):
        line = frontmatter[index]
        if line.strip(" \t") == "" or line.startswith("#"):
            continue
        return line.startswith((" ", "\t"))
    return False


def _is_non_key_line(line: str) -> bool:
    """Return whether a frontmatter line is indented, blank, a comment or a list item."""
    return line == "" or line == "-" or line.startswith((" ", "\t", "#", "- "))


def _record_field(
    path: str,
    key: str,
    raw: str | None,
    *,
    indented: bool,
    seen: set[str],
    duplicates: set[str],
    values: dict[str, str],
) -> str | None:
    """Parse one top-level name or description line and return any immediate finding."""
    if key in seen:
        if key in duplicates:
            return None
        duplicates.add(key)
        return f"{path}: duplicate {key}"
    seen.add(key)
    parsed = _parse_scalar(raw, indented=indented)
    if parsed is None:
        return f"{path}: unsupported {key} form"
    scalar, quoted = parsed
    if not quoted and YAML_NON_STRING.fullmatch(scalar) is not None:
        return f"{path}: {key} {scalar!r} is not a YAML string; quote it"
    values[key] = scalar
    return None


def _check_name(path: str, value: str) -> list[str]:
    """Validate name length, charset, reserved words and parent directory match."""
    if not (1 <= len(value) <= NAME_MAX and NAME_PATTERN.fullmatch(value)):
        rule = f"must be 1-{NAME_MAX} chars of a-z, 0-9 and single hyphens, no leading or trailing hyphen"
        return [f"{path}: name {value!r} {rule}"]
    for word in RESERVED_WORDS:
        if word in value:
            return [f"{path}: name {value!r} must not contain the reserved word {word!r}"]
    parent = PurePosixPath(path).parent.name
    if value != parent:
        return [f"{path}: name {value!r} must equal its directory {parent!r}"]
    return []


def _check_description(path: str, value: str) -> list[str]:
    """Validate description length and reject XML tags."""
    findings: list[str] = []
    desc_len = len(value)
    if not (1 <= desc_len <= DESCRIPTION_MAX):
        findings.append(f"{path}: description must be 1-{DESCRIPTION_MAX} chars, has {desc_len}")
    if _XML_TAG.search(value) is not None:
        findings.append(f"{path}: description must not contain XML tags")
    return findings


def _check_fields(path: str, seen: set[str], values: dict[str, str]) -> list[str]:
    """Validate presence and parsed values of name and description."""
    findings: list[str] = []
    if "name" not in seen:
        findings.append(f"{path}: missing name")
    elif "name" in values:
        findings.extend(_check_name(path, values["name"]))
    if "description" not in seen:
        findings.append(f"{path}: missing description")
    elif "description" in values:
        findings.extend(_check_description(path, values["description"]))
    return findings


def problems(path: str, text: str) -> list[str]:
    """Return one message per Agent Skills frontmatter finding in text."""
    frontmatter, errors = _frontmatter_lines(path, text)
    if frontmatter is None:
        return errors
    seen: set[str] = set()
    duplicates: set[str] = set()
    values: dict[str, str] = {}
    findings: list[str] = []
    for index, line in enumerate(frontmatter):
        if _is_non_key_line(line):
            continue
        if _ANY_KEY_LINE.match(line) is None:
            findings.append(f"{path}: frontmatter line {index + 2} is not a key, comment or list item")
            continue
        match = _KEY_LINE.match(line)
        if match is None:
            continue
        finding = _record_field(
            path,
            match.group(1),
            match.group(2),
            indented=_has_continuation(frontmatter, index + 1),
            seen=seen,
            duplicates=duplicates,
            values=values,
        )
        if finding is not None:
            findings.append(finding)
    return findings + _check_fields(path, seen, values)


def _check_disk_file(root: Path, rel: str) -> list[str]:
    """Check one SKILL.md on disk without following symlinks or reading past the cap."""
    file_path = root / rel
    if file_path.is_symlink():
        return [f"{rel}: {NAME} must be a regular file, not a symlink"]
    if not file_path.is_file():
        return [f"{rel}: {NAME} must be a regular file"]
    with file_path.open("rb") as handle:
        raw = handle.read(SIZE_MAX_BYTES + 1)
    if len(raw) > SIZE_MAX_BYTES:
        return [f"{rel}: {NAME} is over {SIZE_MAX_BYTES} bytes"]
    return problems(rel, raw.decode("utf-8").replace("\r\n", "\n"))


def _staged_skill_findings(root: Path) -> dict[str, list[str]]:
    """Check staged SKILL.md index entries using mode and blob size before blob reads."""
    by_path: dict[str, list[str]] = {}
    regular: list[tuple[str, str]] = []
    for path, (mode, oid) in sorted(repo_git.staged_entries(root, NAME).items()):
        if mode == "120000":
            by_path[path] = [f"{path}: {NAME} must be a regular file, not a symlink"]
        elif mode not in ("100644", "100755"):
            by_path[path] = [f"{path}: {NAME} must be a regular file"]
        else:
            regular.append((path, oid))
    sizes = repo_git.blob_sizes(root, [oid for _, oid in regular])
    readable: list[tuple[str, str]] = []
    for (path, oid), size in zip(regular, sizes, strict=True):
        if size > SIZE_MAX_BYTES:
            by_path[path] = [f"{path}: {NAME} is over {SIZE_MAX_BYTES} bytes"]
        else:
            readable.append((path, oid))
    blobs = repo_git.read_blobs(root, [oid for _, oid in readable])
    for (path, _), raw in zip(readable, blobs, strict=True):
        by_path[path] = problems(path, raw.decode("utf-8").replace("\r\n", "\n"))
    return by_path


def skill_findings(root: Path, *, worktree: bool = False) -> list[str]:
    """Return findings for SKILL.md files under root from index+untracked or worktree."""
    if worktree:
        findings: list[str] = []
        for file_path in repo_git.worktree_files(root, NAME):
            rel = file_path.relative_to(root).as_posix()
            findings.extend(_check_disk_file(root, rel))
        return findings
    by_path = _staged_skill_findings(root)
    for rel in repo_git.untracked_files(root, NAME):
        by_path[rel] = _check_disk_file(root, rel)
    return [finding for rel in sorted(by_path) for finding in by_path[rel]]


def main(argv: list[str]) -> int:
    """Check [--worktree] [root] (default: current directory): 0 valid, 1 findings, 3 error."""
    args = argv[1:]
    worktree = "--worktree" in args
    rest = [a for a in args if a != "--worktree"]
    if len(rest) > 1:
        sys.stderr.write("skill-frontmatter: error: usage: skill_frontmatter.py [--worktree] [root]\n")
        return 3
    root = Path(rest[0] if rest else ".").resolve()
    try:
        findings = skill_findings(root, worktree=worktree)
    except (repo_git.GitError, OSError, UnicodeDecodeError) as err:
        sys.stderr.write(f"skill-frontmatter: error: {err}\n")
        return 3
    for finding in findings:
        sys.stderr.write(f"skill-frontmatter: {finding}\n")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
