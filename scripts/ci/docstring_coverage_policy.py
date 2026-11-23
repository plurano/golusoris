# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Fail closed when branch-touched Python or shell functions lack docstrings at complexity >= 2."""

from __future__ import annotations

import ast
import re
import subprocess  # Runs only fixed git invocations below. # nosec B404
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

MAX_FILES = 2000
MAX_RAW_PATHS = 200_000
MAX_SOURCE_LINES = 200_000
MAX_HUNKS = 5000
MAX_SHELL_BODY_LINES = 10000
GIT_TIMEOUT_SECONDS = 60
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SHELL_DEF_RE = re.compile(
    r"^\s*(function\s+)?([A-Za-z_][A-Za-z0-9_:-]*)\s*(\(\s*\))?\s*\{?\s*$"
)
SH_WORD_RE = re.compile(r"\b(if|elif|for|while|until)\b")


class Finding(NamedTuple):
    """Represent one docstring-coverage finding."""

    path: str
    line: int
    rule: str
    message: str


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
        ["git", "-C", str(repo_root), *args],  # noqa: S607 - git on PATH. # nosec B607
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, proc.stdout, proc.stderr)
    return proc.stdout


def merge_base_sha(repo_root: Path, base_ref: str) -> str:
    """Resolve merge-base between base_ref and HEAD."""
    return _run_git_text(repo_root, ["merge-base", base_ref, "HEAD"]).strip()


def head_blob_text(repo_root: Path, path: str) -> str | None:
    """Return committed blob at HEAD for path, or None when the path is absent."""
    try:
        return _run_git_text(repo_root, ["show", f"HEAD:{path}"])
    except subprocess.CalledProcessError:
        return None


def parse_touched_lines(diff_text: str) -> set[int] | None:
    """Parse unified diff hunks into touched 1-based lines; None means every line."""
    touched: set[int] = set()
    hunks = 0
    for line in diff_text.splitlines():
        if not line.startswith("@@"):
            continue
        hunks += 1
        if hunks > MAX_HUNKS:
            return None
        match = HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count > 0:
            end_line = start + count - 1
            for num in range(start, min(end_line, MAX_SOURCE_LINES) + 1):
                touched.add(num)
        else:
            touched.add(max(start, 1))
    return touched


def diff_touched_lines(repo_root: Path, base_sha: str, path: str) -> set[int] | None:
    """Return lines touched between base_sha and HEAD for one path; None means every line."""
    diff = _run_git_text(
        repo_root,
        ["diff", "-U0", "--no-color", "--no-ext-diff", base_sha, "HEAD", "--", path],
    )
    return parse_touched_lines(diff)


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


def _owned_descendants(root: ast.AST) -> Iterable[ast.AST]:
    """Yield descendants of root without entering nested callables or classes."""
    stack = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def function_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count cyclomatic complexity for one function body."""
    score = 1
    for child in _owned_descendants(node):
        score += _node_complexity_cost(child)
    return score


def iter_functions(tree: ast.Module) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Yield every function including nested and methods."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def check_python_source(path: str, source: str, touched: set[int]) -> list[Finding]:
    """Analyze Python source text for missing docstrings on touched functions."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as err:
        line = err.lineno or 1
        msg = err.msg or "syntax error"
        return [Finding(path, line, "parse-error", msg)]
    findings: list[Finding] = []
    for fn in iter_functions(tree):
        if not span_touched(function_span(fn), touched):
            continue
        complexity = function_complexity(fn)
        if complexity < 2:
            continue
        if ast.get_docstring(fn):
            continue
        findings.append(
            Finding(
                path,
                fn.lineno,
                "missing-docstring",
                f"function {fn.name} (complexity {complexity}) touched by this branch has no docstring",
            )
        )
    return findings


def _line_indent(line: str) -> int:
    """Return leading whitespace length for a shell line."""
    return len(line) - len(line.lstrip(" \t"))


def _strip_sh_comment_tail(line: str) -> str:
    """Drop a shell # comment tail only when # starts the line or follows whitespace."""
    match = re.search(r"(?:^|\s)#", line)
    if not match:
        return line
    return line[: match.start()]


def _shell_body_end(lines: list[str], start: int, def_indent: int) -> int:
    """Locate the closing brace line index for a shell function body."""
    limit = min(len(lines), start + 1 + MAX_SHELL_BODY_LINES)
    for idx in range(start + 1, limit):
        if lines[idx].strip() != "}":
            continue
        if _line_indent(lines[idx]) <= def_indent:
            return idx
    return len(lines) - 1


def _shell_has_docstring(lines: list[str], def_idx: int) -> bool:
    """Return whether contiguous # comment lines above the def qualify as a docstring."""
    idx = def_idx - 1
    while idx >= 0 and lines[idx].lstrip().startswith("#"):
        text = lines[idx].lstrip()
        if not text.startswith("#!") and not text.lower().startswith("# shellcheck"):
            return True
        idx -= 1
    return False


def _shell_body_complexity(lines: list[str], body_start: int, body_end: int) -> int:
    """Count shell cyclomatic complexity inside a function body."""
    score = 1
    for idx in range(body_start, min(body_end, body_start + MAX_SHELL_BODY_LINES)):
        raw = lines[idx]
        if _is_full_line_comment(raw):
            continue
        chunk = _strip_sh_comment_tail(raw)
        score += len(SH_WORD_RE.findall(chunk))
        score += chunk.count("&&")
        score += chunk.count("||")
        score += chunk.count(";;")
    return score


def _is_full_line_comment(line: str) -> bool:
    """Return whether a shell line is only a comment."""
    return line.lstrip().startswith("#")


def _shell_function_defs(lines: list[str]) -> list[tuple[int, str, int, int, int]]:
    """Return shell def metadata as (def_line, name, def_idx, body_start, body_end)."""
    defs: list[tuple[int, str, int, int, int]] = []
    for idx in range(len(lines)):
        match = SHELL_DEF_RE.match(lines[idx])
        if not match or (not match.group(1) and not match.group(3)):
            continue
        name = match.group(2)
        indent = _line_indent(lines[idx])
        end = _shell_body_end(lines, idx, indent)
        defs.append((idx + 1, name, idx, idx + 1, end))
    return defs


def check_shell_source(path: str, source: str, touched: set[int]) -> list[Finding]:
    """Analyze shell source text for missing docstrings on touched functions."""
    lines = source.splitlines()
    findings: list[Finding] = []
    for def_line, name, def_idx, body_start, body_end in _shell_function_defs(lines):
        span = (def_line, body_end + 1)
        if not span_touched(span, touched):
            continue
        complexity = _shell_body_complexity(lines, body_start, body_end)
        if complexity < 2:
            continue
        if _shell_has_docstring(lines, def_idx):
            continue
        findings.append(
            Finding(
                path,
                def_line,
                "missing-docstring",
                f"function {name} (complexity {complexity}) touched by this branch has no docstring",
            )
        )
    return findings


def check_source(path: str, text: str, touched: set[int] | None = None) -> list[Finding]:
    """Check one in-memory file; default touched set covers every line."""
    lines = text.splitlines()
    if len(lines) > MAX_SOURCE_LINES:
        return [
            Finding(
                path,
                1,
                "file-too-large",
                f"source has {len(lines)} lines, bound {MAX_SOURCE_LINES}; split it",
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
    touched = diff_touched_lines(repo_root, base_sha, path)
    if touched is not None and not touched:
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
        return 2
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
        return 2
    try:
        findings = check(repo, base_sha, paths)
    except subprocess.CalledProcessError as exc:
        print(_git_failure_stderr(exc), file=sys.stderr)
        return 2
    emit(findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())