# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Fail closed when branch-touched Python or shell functions lack docstrings at complexity >= 2."""

from __future__ import annotations

import ast
import itertools
import json
import os
import re
import subprocess  # Runs only fixed git and shfmt invocations below. # nosec B404
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

MAX_FILES = 2000
MAX_RAW_PATHS = 200_000
MAX_SOURCE_LINES = 200_000
MAX_HUNKS = 5000
MAX_SHELL_BODY_LINES = 10000
MAX_SHELL_NESTING = 64
MAX_SHELL_AST_NODES = 5_000_000
GIT_TIMEOUT_SECONDS = 60
SHFMT_TIMEOUT_SECONDS = 60
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SHFMT_ERROR_RE = re.compile(r"^(?:<standard input>:)?(\d+):(\d+): (.*)$")
_SH_NEST_TYPES = frozenset(
    {
        "Block",
        "Subshell",
        "IfClause",
        "WhileClause",
        "ForClause",
        "CaseClause",
        "CmdSubst",
        "ProcSubst",
        "FuncDecl",
    }
)
_SH_SHORT_CIRCUIT = frozenset({"&&", "||"})
_SH_BINARY_TYPES = frozenset({"BinaryCmd", "BinaryTest", "BinaryArithm"})
PY_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


class Finding(NamedTuple):
    """Represent one docstring-coverage finding."""

    path: str
    line: int
    rule: str
    message: str


class DiffTouch(NamedTuple):
    """New-side touched lines plus old-side replaced or deleted hunk metadata."""

    lines: set[int]
    deleted: list[tuple[int, int]]
    anchors: list[int]
    hunks: list[tuple[int, int, int, int]]


def usage() -> None:
    """Print CLI usage to stderr."""
    prog = Path(sys.argv[0]).name
    print(f"usage: {prog} [--base REF] --pushed [PATH...]", file=sys.stderr)


def normalize_paths(raw: list[str]) -> tuple[list[str], int]:
    """Keep deduped .py and .sh paths, returning overflow count beyond MAX_FILES."""
    kept: list[str] = []
    seen: set[str] = set()
    for item in raw[:MAX_RAW_PATHS]:
        norm = item.replace("\\", "/")
        if not (norm.endswith(".py") or norm.endswith(".sh")):
            continue
        if norm in seen:
            continue
        seen.add(norm)
        kept.append(norm)
    overflow = max(0, len(kept) - MAX_FILES) + max(0, len(raw) - MAX_RAW_PATHS)
    return kept[:MAX_FILES], overflow


def _run_git_text(repo_root: Path, args: list[str]) -> str:
    """Run git in repo_root and return decoded stdout."""
    proc = subprocess.run(  # noqa: S603 - fixed git argv, no shell. # nosec B603
        ["git", "-C", str(repo_root), "--literal-pathspecs", *args],  # noqa: S607 - git on PATH. # nosec B607
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, stdout, stderr)
    return stdout


def merge_base_sha(repo_root: Path, base_ref: str) -> str:
    """Resolve merge-base between base_ref and HEAD."""
    return _run_git_text(repo_root, ["merge-base", base_ref, "HEAD"]).strip()


def blob_text(repo_root: Path, rev: str, path: str) -> str | None:
    """Return blob text at rev for path, or None when the path is absent."""
    ls_out = _run_git_text(repo_root, ["ls-tree", "-z", rev, "--", path])
    if not ls_out:
        return None
    entry = ls_out.split("\0", 1)[0]
    if not entry.strip():
        return None
    meta, found, _path = entry.partition("\t")
    if not found:
        return None
    fields = meta.split()
    if len(fields) < 2 or fields[1] != "blob":
        return None
    return _run_git_text(repo_root, ["show", f"{rev}:{path}"])


def head_blob_text(repo_root: Path, path: str) -> str | None:
    """Return committed blob at HEAD for path, or None when the path is absent."""
    return blob_text(repo_root, "HEAD", path)


def _parse_hunk_header(line: str) -> tuple[int, int, int, int] | None:
    """Parse one unified diff @@ header into (old_start, old_count, new_start, new_count)."""
    match = HUNK_RE.match(line)
    if not match:
        return None
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    return (old_start, old_count, new_start, new_count)


def parse_diff(diff_text: str) -> DiffTouch | None:
    """Parse unified diff hunks into touch and deletion metadata; None means every line."""
    lines: set[int] = set()
    deleted: list[tuple[int, int]] = []
    anchors: list[int] = []
    hunks_list: list[tuple[int, int, int, int]] = []
    hunks = 0
    for line in diff_text.split("\n"):
        if not line.startswith("@@"):
            continue
        hunks += 1
        if hunks > MAX_HUNKS:
            return None
        parsed = _parse_hunk_header(line)
        if parsed is None:
            return None
        old_start, old_count, new_start, new_count = parsed
        hunks_list.append(parsed)
        if new_count > 0:
            end_line = new_start + new_count - 1
            lines.update(range(new_start, min(end_line, MAX_SOURCE_LINES) + 1))
        if old_count > 0:
            deleted.append((old_start, old_start + old_count - 1))
            anchors.append(max(new_start, 1))
    return DiffTouch(lines, deleted, anchors, hunks_list)


def parse_touched_lines(diff_text: str) -> set[int] | None:
    """Parse unified diff hunks into touched 1-based lines; None means every line."""
    parsed = parse_diff(diff_text)
    if parsed is None:
        return None
    return parsed.lines


def diff_touched_lines(repo_root: Path, base_sha: str, path: str) -> DiffTouch | None:
    """Return diff touch metadata between base_sha and HEAD; None means every line."""
    diff = _run_git_text(
        repo_root,
        [
            "diff",
            "-U0",
            "--inter-hunk-context=0",
            "--text",
            "--no-color",
            "--no-ext-diff",
            base_sha,
            "HEAD",
            "--",
            path,
        ],
    )
    return parse_diff(diff)


def lf_lines(text: str) -> list[str]:
    """Split text into git-numbered lines on LF only."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def python_git_lines(source: str) -> list[int]:
    """Map each Python line number to its git 1-based line number."""
    mapping: list[int] = [0, 1]
    git_line = 1
    for match in itertools.islice(PY_NEWLINE_RE.finditer(source), MAX_SOURCE_LINES):
        if match.group().endswith("\n"):
            git_line += 1
        mapping.append(git_line)
    return mapping


def _python_git_line(line: int, mapping: list[int]) -> int:
    """Convert one Python line number to a git line number."""
    return mapping[min(line, len(mapping) - 1)]


def _python_git_span(span: tuple[int, int], mapping: list[int]) -> tuple[int, int]:
    """Convert an inclusive Python line span to git lines; the map never decreases."""
    return (_python_git_line(span[0], mapping), _python_git_line(span[1], mapping))


def function_span(node: ast.AST) -> tuple[int, int]:
    """Return inclusive line span for a function node including decorators."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (1, 1)
    start = node.lineno
    for dec in node.decorator_list[:32]:
        start = min(start, dec.lineno)
    end = node.end_lineno if node.end_lineno is not None else node.lineno
    return (start, end)


def span_touched(span: tuple[int, int], touched: set[int]) -> bool:
    """Return whether any line in span intersects touched."""
    lo, hi = span
    end = min(hi, MAX_SOURCE_LINES)
    for line in range(lo, end + 1):
        if line in touched:
            return True
    return False


def _node_complexity_cost(node: ast.AST) -> int:
    """Return cyclomatic increment for one AST node."""
    if isinstance(node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)):
        return 1
    if isinstance(node, ast.BoolOp):
        return max(0, len(node.values) - 1)
    if isinstance(node, ast.comprehension):
        return 1 + len(node.ifs[:16])
    if hasattr(ast, "match_case") and isinstance(node, ast.match_case):
        return 1
    return 0


def _owned_descendants(roots: Iterable[ast.AST]) -> Iterable[ast.AST]:
    """Yield roots and their descendants without entering nested callables or classes."""
    stack = list(roots)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def function_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count cyclomatic complexity for one function body."""
    score = 1
    for child in _owned_descendants(node.body):
        score += _node_complexity_cost(child)
    return score


def iter_functions(tree: ast.Module) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every function including nested and methods."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def iter_named_functions(
    tree: ast.Module,
) -> Iterable[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Yield qualified names and function nodes from a parsed module."""
    stack: list[tuple[ast.AST, str]] = [(tree, "")]
    while stack:
        parent, prefix = stack.pop()
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, ast.ClassDef):
                qual = f"{prefix}.{node.name}" if prefix else node.name
                stack.append((node, qual))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}.{node.name}" if prefix else node.name
                yield qual, node
                stack.append((node, qual))
            else:
                stack.append((node, prefix))


def _range_intersects(span: tuple[int, int], lo: int, hi: int) -> bool:
    """Return whether inclusive git span intersects an old-side deletion range."""
    a, b = span
    return lo <= b and a <= hi


def _ranges_hit_span(span: tuple[int, int], ranges: list[tuple[int, int]]) -> bool:
    """Return whether any deletion range intersects span."""
    for lo, hi in ranges:
        if _range_intersects(span, lo, hi):
            return True
    return False


def _surviving_new_line(
    span: tuple[int, int], hunks: list[tuple[int, int, int, int]]
) -> int | None:
    """Return new-side line for first base line in span unchanged by hunks, else None."""
    line = span[0]
    delta = 0
    for old_start, old_count, _new_start, new_count in hunks[:MAX_HUNKS]:
        if old_count == 0:
            if old_start < line:
                delta += new_count
                continue
            break
        old_end = old_start + old_count - 1
        if old_end < line:
            delta += new_count - old_count
            continue
        if old_start <= line:
            line = old_end + 1
            delta += new_count - old_count
            continue
        break
    return line + delta if line <= span[1] else None


def deleted_owner_lines(
    path: str,
    base: str,
    ranges: list[tuple[int, int]],
    hunks: list[tuple[int, int, int, int]],
) -> set[int] | None:
    """Return new-side owner lines for base functions hit by old-side deletion ranges."""
    if not ranges:
        return set()
    norm = path.replace("\\", "/")
    if norm.endswith(".py"):
        return _python_deleted_owner_lines(path, base, ranges, hunks)
    if norm.endswith(".sh"):
        return _shell_deleted_owner_lines(base, ranges, hunks)
    return set()


def _python_deleted_owner_lines(
    path: str,
    base: str,
    ranges: list[tuple[int, int]],
    hunks: list[tuple[int, int, int, int]],
) -> set[int] | None:
    """Collect new-side owner lines for Python functions intersecting deletion ranges."""
    if len(lf_lines(base)) + base.count("\r") - base.count("\r\n") > MAX_SOURCE_LINES:
        return None
    line_map = python_git_lines(base)
    try:
        tree = ast.parse(base, filename=path)
    except (SyntaxError, ValueError):
        return None
    owners: set[int] = set()
    for _qual, fn in iter_named_functions(tree):
        span = _python_git_span(function_span(fn), line_map)
        if not _ranges_hit_span(span, ranges):
            continue
        surv = _surviving_new_line(span, hunks)
        if surv is not None:
            owners.add(surv)
    return owners


def _shell_deleted_owner_lines(
    base: str,
    ranges: list[tuple[int, int]],
    hunks: list[tuple[int, int, int, int]],
) -> set[int] | None:
    """Collect new-side owner lines for shell functions intersecting deletion ranges."""
    lines = lf_lines(base)
    if len(lines) > MAX_SOURCE_LINES:
        return None
    parsed = parse_shell(base)
    if parsed.tree is None:
        return None
    found = shell_functions(parsed.tree)
    if found is None:
        return None
    funcs, comments = found
    owners: set[int] = set()
    for fn in funcs:
        span = (_shell_doc_block_start(lines, comments, fn.start), fn.end)
        if not _ranges_hit_span(span, ranges):
            continue
        surv = _surviving_new_line((fn.start, fn.end), hunks)
        if surv is not None:
            owners.add(surv)
    return owners


def deleted_owner_touch(
    repo_root: Path, base_sha: str, path: str, diff: DiffTouch
) -> set[int]:
    """Map deletion-only hunks to surviving owner lines or anchor fallback lines."""
    if not diff.deleted:
        return set()
    base = blob_text(repo_root, base_sha, path)
    if base is None:
        return set()
    owners = deleted_owner_lines(path, base, diff.deleted, diff.hunks)
    if owners is None:
        return set(diff.anchors)
    return owners


def check_python_source(path: str, source: str, touched: set[int]) -> list[Finding]:
    """Analyze Python source text for missing docstrings on touched functions."""
    line_map = python_git_lines(source)
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as err:
        line = _python_git_line(err.lineno or 1, line_map)
        msg = err.msg or "syntax error"
        return [Finding(path, line, "parse-error", msg)]
    findings: list[Finding] = []
    for qual, fn in iter_named_functions(tree):
        span = _python_git_span(function_span(fn), line_map)
        if not span_touched(span, touched):
            continue
        complexity = function_complexity(fn)
        if complexity < 2:
            continue
        if ast.get_docstring(fn):
            continue
        label = qual if qual != fn.name else fn.name
        findings.append(
            Finding(
                path,
                _python_git_line(fn.lineno, line_map),
                "missing-docstring",
                f"function {label} (complexity {complexity}) touched by this branch has no docstring",
            )
        )
    return findings


class ShellParserUnavailable(RuntimeError):
    """shfmt could not run, so shell files cannot be checked."""


class ShellAst(NamedTuple):
    """Parsed shell AST, or the finding that replaces it."""

    tree: dict[str, object] | None
    line: int
    rule: str
    message: str


class ShellFunction(NamedTuple):
    """One shell function with its git line span and complexity."""

    name: str
    start: int
    end: int
    complexity: int


def shfmt_bin() -> str:
    """Return the configured shfmt executable path or name."""
    return os.environ.get("SHFMT_BIN") or "shfmt"


def _shfmt_parse_error(stderr: bytes) -> ShellAst:
    """Build a parse-error ShellAst from shfmt stderr bytes."""
    first = stderr.decode("utf-8", "replace").partition("\n")[0].rstrip("\r")
    match = SHFMT_ERROR_RE.match(first)
    if match:
        return ShellAst(None, int(match.group(1)), "parse-error", match.group(3))
    return ShellAst(None, 1, "parse-error", first or "shfmt failed")


def parse_shell(source: str) -> ShellAst:
    """Parse bash source into a shfmt JSON AST or a fail-closed finding."""
    binary = shfmt_bin()
    try:
        proc = subprocess.run(  # noqa: S603 - fixed shfmt argv, no shell. # nosec B603
            [binary, "-ln", "bash", "--to-json"],  # nosec B607
            input=source.encode("utf-8"),
            capture_output=True,
            timeout=SHFMT_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError as err:
        raise ShellParserUnavailable(
            f"docstring-coverage: cannot run {binary}: run make tools-bootstrap or set SHFMT_BIN"
        ) from err
    except subprocess.TimeoutExpired as err:
        raise ShellParserUnavailable(
            f"docstring-coverage: shfmt timed out after {SHFMT_TIMEOUT_SECONDS}s"
        ) from err
    if proc.returncode != 0:
        return _shfmt_parse_error(proc.stderr)
    try:
        tree = json.loads(proc.stdout)
    except RecursionError:
        return ShellAst(None, 1, "analysis-limit", f"shell nesting deeper than {MAX_SHELL_NESTING}")
    except ValueError:
        return ShellAst(None, 1, "parse-error", "shfmt output is not a JSON object")
    if not isinstance(tree, dict):
        return ShellAst(None, 1, "parse-error", "shfmt output is not a JSON object")
    return ShellAst(tree, 0, "", "")


def _shell_node_cost(node: dict[str, object], kind: object, key: str) -> int:
    """Return the cyclomatic complexity increment for one shell AST dict."""
    if kind in {"IfClause", "WhileClause"}:
        return 1
    if kind is None:
        return int(key == "Else" and bool(node.get("Cond")))
    if kind == "ForClause":
        return int(not node.get("Select"))
    if kind == "CaseClause":
        items = node.get("Items")
        return len(items) if isinstance(items, list) else 0
    if kind in _SH_BINARY_TYPES:
        return int(node.get("Op") in _SH_SHORT_CIRCUIT)
    return 0


def _shell_pos_line(pos: object, default: int = 1) -> int:
    """Extract a 1-based Line integer from a shfmt Pos or End dict."""
    if isinstance(pos, dict):
        line = pos.get("Line")
        if isinstance(line, int):
            return line
    return default


def _shell_func_header(node: dict[str, object]) -> tuple[str, int, int]:
    """Extract (name, start_line, end_line) from a FuncDecl AST node."""
    name_node = node.get("Name")
    raw_name = name_node.get("Value") if isinstance(name_node, dict) else None
    name = raw_name if isinstance(raw_name, str) else "<anonymous>"
    start = _shell_pos_line(node.get("Pos"), 1)
    end = max(start, _shell_pos_line(node.get("End"), start))
    return name, start, end


def _visit_shell_node(
    node: dict[str, object],
    key: str,
    owner: int,
    funcs: list[tuple[str, int, int]],
    scores: list[int],
    comments: set[int],
) -> int:
    """Record comments, FuncDecl headers, or decision costs; return child owner."""
    if "Hash" in node and "Text" in node:
        line = _shell_pos_line(node.get("Hash"), 0)
        if line > 0:
            comments.add(line)
    kind = node.get("Type")
    if kind == "FuncDecl":
        funcs.append(_shell_func_header(node))
        scores.append(1)
        return len(funcs) - 1
    if owner >= 0:
        scores[owner] += _shell_node_cost(node, kind, key)
    return owner


def _push_shell_children(
    stack: list[tuple[dict[str, object], str, int, int]],
    node: dict[str, object],
    depth: int,
    owner: int,
) -> None:
    """Push child AST dicts and list-contained AST dicts onto the traversal stack."""
    for child_key, val in itertools.islice(node.items(), 64):
        if isinstance(val, dict):
            stack.append((val, child_key, depth, owner))
        elif isinstance(val, list):
            stack.extend(
                (item, child_key, depth, owner)
                for item in reversed(val[: MAX_SHELL_AST_NODES + 1])
                if isinstance(item, dict)
            )


def shell_functions(
    tree: dict[str, object],
) -> tuple[list[ShellFunction], set[int]] | None:
    """Walk a shfmt AST iteratively to collect shell functions and comment lines."""
    stack: list[tuple[dict[str, object], str, int, int]] = [(tree, "", 0, -1)]
    funcs: list[tuple[str, int, int]] = []
    scores: list[int] = []
    comments: set[int] = set()
    for step in range(MAX_SHELL_AST_NODES + 1):
        if not stack:
            break
        if step == MAX_SHELL_AST_NODES:
            return None
        node, key, depth, owner = stack.pop()
        if node.get("Type") in _SH_NEST_TYPES:
            depth += 1
            if depth > MAX_SHELL_NESTING:
                return None
        owner = _visit_shell_node(node, key, owner, funcs, scores, comments)
        _push_shell_children(stack, node, depth, owner)
    rows = [
        ShellFunction(name, start, end, score)
        for (name, start, end), score in zip(funcs, scores, strict=True)
    ]
    rows.sort(key=lambda fn: fn.start)
    return rows, comments


def _shell_doc_lines(lines: list[str], comments: set[int], def_line: int) -> list[int]:
    """Return 1-based contiguous comment lines directly above def_line, nearest first."""
    doc: list[int] = []
    top = min(def_line - 1, len(lines), MAX_SOURCE_LINES)
    for idx in range(top, 0, -1):
        if idx not in comments:
            break
        text = lines[idx - 1].lstrip()
        if not text.startswith("#") or text.startswith("#!"):
            break
        doc.append(idx)
    return doc


def _shell_doc_block_start(lines: list[str], comments: set[int], def_line: int) -> int:
    """Return the 1-based start line of the doc block above def_line, or def_line."""
    doc = _shell_doc_lines(lines, comments, def_line)
    return doc[-1] if doc else def_line


def _shell_has_docstring(lines: list[str], comments: set[int], def_line: int) -> bool:
    """Return whether any doc comment line above def_line is not a shellcheck directive."""
    return any(
        not lines[idx - 1].lstrip().lower().startswith("# shellcheck")
        for idx in _shell_doc_lines(lines, comments, def_line)
    )


def check_shell_source(path: str, source: str, touched: set[int]) -> list[Finding]:
    """Analyze shell source text for missing docstrings on touched functions."""
    lines = lf_lines(source)
    parsed = parse_shell(source)
    if parsed.tree is None:
        return [Finding(path, parsed.line, parsed.rule, parsed.message)]
    found = shell_functions(parsed.tree)
    if found is None:
        msg = f"shell nesting deeper than {MAX_SHELL_NESTING} or more than {MAX_SHELL_AST_NODES} syntax nodes"
        return [Finding(path, 1, "analysis-limit", msg)]
    funcs, comments = found
    findings: list[Finding] = []
    for fn in funcs:
        if not span_touched((fn.start, fn.end), touched):
            continue
        if _shell_has_docstring(lines, comments, fn.start):
            continue
        if fn.end - fn.start > MAX_SHELL_BODY_LINES + 1:
            findings.append(
                Finding(
                    path,
                    fn.start,
                    "analysis-limit",
                    f"function {fn.name} body spans more than {MAX_SHELL_BODY_LINES} lines; split it",
                )
            )
            continue
        if fn.complexity < 2:
            continue
        findings.append(
            Finding(
                path,
                fn.start,
                "missing-docstring",
                f"function {fn.name} (complexity {fn.complexity}) touched by this branch has no docstring",
            )
        )
    return findings


def check_source(path: str, text: str, touched: set[int] | None = None) -> list[Finding]:
    """Check one in-memory file; default touched set covers every line."""
    lines = lf_lines(text)
    lone_cr = text.count("\r") - text.count("\r\n")
    line_count = len(lines) + lone_cr
    if line_count > MAX_SOURCE_LINES:
        return [
            Finding(
                path,
                1,
                "file-too-large",
                f"source has {line_count} lines, bound {MAX_SOURCE_LINES}; split it",
            )
        ]
    if touched is None:
        touched = set(range(1, min(len(lines), 1_000_000) + 1))
    norm = path.replace("\\", "/")
    if norm.endswith(".py"):
        return check_python_source(path, text, touched)
    if norm.endswith(".sh"):
        return check_shell_source(path, text, touched)
    return []


def check_path(repo_root: Path, base_sha: str, path: str) -> list[Finding]:
    """Check one repository path using committed HEAD content and branch diff."""
    blob = head_blob_text(repo_root, path)
    if blob is None:
        return []
    diff = diff_touched_lines(repo_root, base_sha, path)
    if diff is None:
        return check_source(path, blob, None)
    extra = deleted_owner_touch(repo_root, base_sha, path, diff)
    touched = diff.lines | extra
    if not touched:
        return []
    return check_source(path, blob, touched)


def check(repo_root: Path, base_sha: str, paths: list[str]) -> list[Finding]:
    """Check pushed paths against merge-base diff using committed HEAD blobs."""
    norm_paths, _overflow = normalize_paths(paths)
    findings: list[Finding] = []
    for path in norm_paths:
        findings.extend(check_path(repo_root, base_sha, path))
    findings.sort(key=lambda item: (item.path, item.line))
    return findings


def emit(findings: list[Finding]) -> None:
    """Print findings in tool-friendly form."""
    for item in findings:
        print(f"{item.path}:{item.line}: {item.rule}: {item.message}")


def policy_repo_root() -> Path:
    """Return the repository root that hosts this policy script."""
    return Path(__file__).resolve().parent.parent.parent


def parse_args(argv: list[str]) -> tuple[str, list[str]] | None:
    """Parse --base and --pushed; None means usage error."""
    if argv and argv[0] not in ("--base", "--pushed"):
        return None
    base_ref = "origin/main"
    rest = argv
    if rest and rest[0] == "--base":
        if len(rest) < 2:
            return None
        base_ref = rest[1]
        rest = rest[2:]
    if not rest or rest[0] != "--pushed":
        return None
    return base_ref, rest[1:]


def _git_failure_stderr(exc: subprocess.CalledProcessError) -> str:
    """Format a short stderr tail for a failed git invocation."""
    cmd = exc.cmd[0] if exc.cmd else "git"
    stderr = exc.stderr or ""
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    return f"docstring-coverage: git {cmd} failed: {tail}"


def main() -> int:
    """Parse argv, resolve merge base, and run docstring coverage checks."""
    parsed = parse_args(sys.argv[1:])
    if parsed is None:
        usage()
        return 3
    base_ref, raw_paths = parsed
    paths, overflow = normalize_paths(raw_paths)
    if overflow:
        print(
            f"docstring-coverage: path list truncated; {overflow} paths not checked",
            file=sys.stderr,
        )
    if not paths:
        print("docstring-coverage: no pushed Python or shell files, skipping")
        return 0
    repo = policy_repo_root()
    try:
        base_sha = merge_base_sha(repo, base_ref)
    except subprocess.CalledProcessError:
        print(
            f"docstring-coverage: cannot resolve merge base with {base_ref}; fetch it",
            file=sys.stderr,
        )
        return 3
    try:
        findings = check(repo, base_sha, paths)
    except subprocess.CalledProcessError as exc:
        print(_git_failure_stderr(exc), file=sys.stderr)
        return 3
    except ShellParserUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3
    emit(findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
