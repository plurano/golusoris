#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Fail when a SKILL.md frontmatter name or description breaks the Agent Skills specification.

The Agent Skills specification (agentskills.io/specification) requires `name` to be 1-64
characters of lowercase a-z, 0-9 and hyphens, with no leading or trailing hyphen, no `--`, and
equal to its parent directory name, and `description` to be 1-1024 characters. The check fails on
missing or unterminated `---` frontmatter, missing or duplicate `name` or `description`, any YAML
scalar form outside the allow-list, or a `name` or `description` breaking those rules.

The frontmatter reader accepts only single-line plain or double-quoted scalars (without quotes or
backslashes inside double quotes) for `name` and `description` and reports every other form.
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
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

_KEY_LINE = re.compile(r"^(name|description):(?: (.*))?$")
_PLAIN_FORBIDDEN_START = frozenset("-?:,[]{}#&*!|>%@'\"`")


def _double_quoted(value: str) -> str | None:
    """Return the inner text of an escape-free double-quoted scalar, or None."""
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return None
    inner = value[1:-1]
    return None if ('"' in inner or "\\" in inner) else inner


def _parse_scalar(raw: str | None, *, indented: bool) -> str | None:
    """Parse an allowed single-line plain or double-quoted scalar, or return None."""
    if indented:
        return None
    value = "" if raw is None else raw.rstrip(" \t")
    if value == "":
        return ""
    if value[0] == '"':
        return _double_quoted(value)
    if value[0] in _PLAIN_FORBIDDEN_START or value.endswith(":"):
        return None
    return None if (": " in value or " #" in value) else value


def _frontmatter_lines(path: str, text: str) -> tuple[list[str] | None, list[str]]:
    """Return the frontmatter lines inside `---` fences, or None with the fence finding."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0] != "---":
        return None, [f"{path}: no frontmatter"]
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return lines[1:index], []
    return None, [f"{path}: unterminated frontmatter"]


def _top_level_fields(frontmatter: list[str]) -> list[tuple[str, str | None, bool]]:
    """Return (key, raw_value, has_indented_line) for top-level name and description lines."""
    entries: list[tuple[str, str | None, bool]] = []
    for index, line in enumerate(frontmatter):
        if line.startswith((" ", "\t")):
            continue
        match = _KEY_LINE.match(line)
        if match is not None:
            indented = index + 1 < len(frontmatter) and frontmatter[index + 1].startswith((" ", "\t"))
            entries.append((match.group(1), match.group(2), indented))
    return entries


def _check_name(path: str, value: str) -> list[str]:
    """Validate name length, charset and parent directory match."""
    if not (1 <= len(value) <= NAME_MAX and NAME_PATTERN.fullmatch(value)):
        rule = f"must be 1-{NAME_MAX} chars of a-z, 0-9 and single hyphens, no leading or trailing hyphen"
        return [f"{path}: name {value!r} {rule}"]
    parent = PurePosixPath(path).parent.name
    if value != parent:
        return [f"{path}: name {value!r} must equal its directory {parent!r}"]
    return []


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
        desc_len = len(values["description"])
        if not (1 <= desc_len <= DESCRIPTION_MAX):
            findings.append(f"{path}: description must be 1-{DESCRIPTION_MAX} chars, has {desc_len}")
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
    for key, raw, indented in _top_level_fields(frontmatter):
        if key in seen:
            if key not in duplicates:
                duplicates.add(key)
                findings.append(f"{path}: duplicate {key}")
            continue
        seen.add(key)
        scalar = _parse_scalar(raw, indented=indented)
        if scalar is None:
            findings.append(f"{path}: unsupported {key} form")
        else:
            values[key] = scalar
    return findings + _check_fields(path, seen, values)


def _check_root(root: Path) -> list[str]:
    """Check every non-ignored SKILL.md under root and return all findings."""
    findings: list[str] = []
    for file_path in repo_git.worktree_files(root, NAME):
        rel = file_path.relative_to(root).as_posix()
        text = file_path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        findings.extend(problems(rel, text))
    return findings


def main(argv: list[str]) -> int:
    """Check [root] (default: current directory): 0 valid, 1 findings, 3 error."""
    args = argv[1:]
    if len(args) > 1:
        sys.stderr.write("skill-frontmatter: error: usage: skill_frontmatter.py [root]\n")
        return 3
    root = Path(args[0] if args else ".").resolve()
    try:
        findings = _check_root(root)
    except (repo_git.GitError, OSError, UnicodeDecodeError) as err:
        sys.stderr.write(f"skill-frontmatter: error: {err}\n")
        return 3
    for finding in findings:
        sys.stderr.write(f"skill-frontmatter: {finding}\n")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
