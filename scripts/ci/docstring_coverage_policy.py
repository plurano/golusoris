# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Fail closed when branch-touched Python or shell functions lack docstrings at complexity >= 2."""

from __future__ import annotations

import ast
import itertools
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
MAX_SHELL_NESTING = 64
GIT_TIMEOUT_SECONDS = 60
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SHELL_DEF_LINE_RE = re.compile(
    r"^\s*(?:function\s+([A-Za-z_][A-Za-z0-9_:-]*)\s*(?:\(\s*\))?|"
    r"([A-Za-z_][A-Za-z0-9_:-]*)\s*\(\s*\))\s*(.*)$"
)
SH_WORD_RE = re.compile(r"\b(if|elif|for|while|until)\b")
_SH_COMMENT_BEFORE = frozenset("&;|()<>")
PY_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


class Finding(NamedTuple):
    """Represent one docstring-coverage finding."""

    path: str
    line: int
    rule: str
    message: str


class DiffTouch(NamedTuple):
    """New-side touched lines plus old-side deletion hunk metadata."""

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
        ["git", "-C", str(repo_root), *args],  # noqa: S607 - git on PATH. # nosec B607
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
        match = HUNK_RE.match(line)
        if not match:
            return None
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        hunks_list.append((old_start, old_count, new_start, new_count))
        if new_count > 0:
            end_line = new_start + new_count - 1
            for num in range(new_start, min(end_line, MAX_SOURCE_LINES) + 1):
                lines.add(num)
        else:
            old_end = old_start + old_count - 1
            deleted.append((old_start, old_end))
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
        ["diff", "-U0", "--no-color", "--no-ext-diff", base_sha, "HEAD", "--", path],
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
    line_map = python_git_lines(base)
    try:
        tree = ast.parse(base, filename=path)
    except (SyntaxError, ValueError):
        return None
    if len(lf_lines(base)) > MAX_SOURCE_LINES:
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


def _shell_function_span(def_line: int, def_idx: int, body_end: int, line_count: int) -> tuple[int, int]:
    """Return inclusive shell function span; an unbounded body runs to end of file."""
    if body_end == -1:
        return (def_line, line_count)
    return (def_line, max(body_end, def_idx) + 1)


def _shell_deleted_owner_lines(
    base: str,
    ranges: list[tuple[int, int]],
    hunks: list[tuple[int, int, int, int]],
) -> set[int] | None:
    """Collect new-side owner lines for shell functions intersecting deletion ranges."""
    lines = lf_lines(base)
    if len(lines) > MAX_SOURCE_LINES:
        return None
    code = _shell_code_lines(lines)
    if code is None:
        return None
    owners: set[int] = set()
    for def_line, _name, def_idx, _bs, body_end, _tail in _shell_function_defs(lines, code):
        span = _shell_function_span(def_line, def_idx, body_end, len(lines))
        if not _ranges_hit_span(span, ranges):
            continue
        surv = _surviving_new_line(span, hunks)
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


def _line_indent(line: str) -> int:
    """Return leading whitespace length for a shell line."""
    return len(line) - len(line.lstrip(" \t"))


def _sh_heredoc_delim(word: str) -> str:
    """Strip one quoting level from a heredoc delimiter token."""
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        return word[1:-1]
    if word.startswith("\\"):
        return word[1:]
    return word


def _sh_comment_hash(line: str, pos: int) -> bool:
    """Return whether # at pos begins a comment in shell code context."""
    if pos >= len(line) or line[pos] != "#":
        return False
    if pos == 0:
        return True
    prev = line[pos - 1]
    return prev.isspace() or prev in _SH_COMMENT_BEFORE


_SH_QUOTE_OPEN = {"'": "sq", '"': "dq", "`": "bkt"}
_SH_CLOSER = {"sq": "'", "ansic": "'", "dq": '"', "bkt": "`", "cmd": ")", "paren": ")", "param": "}"}
_SH_LITERAL = frozenset({"sq", "ansic"})
_SH_QUOTED = frozenset({"dq", "param"})


class _ShCodeScan:
    """Quote-aware shell code view built one source line at a time."""

    def __init__(self) -> None:
        """Initialize the context stack and heredoc queues."""
        self._ctx: list[str] = ["code"]
        self._overflow = False
        self._next_hd: list[tuple[str, bool]] = []
        self._active_hd: tuple[str, bool] | None = None

    def _push(self, label: str) -> None:
        """Enter a nested context; flag overflow past MAX_SHELL_NESTING."""
        self._ctx.append(label)
        if len(self._ctx) > MAX_SHELL_NESTING:
            self._overflow = True

    def _queue_heredoc(self, line: str, pos: int) -> int:
        """Parse <<[-]WORD and queue a pending heredoc; return new index."""
        pos += 2
        strip_tabs = False
        if pos < len(line) and line[pos] == "-":
            strip_tabs = True
            pos += 1
        while pos < len(line) and line[pos].isspace():
            pos += 1
        start = pos
        while pos < len(line) and not line[pos].isspace():
            pos += 1
        word = _sh_heredoc_delim(line[start:pos])
        if word:
            self._next_hd.append((word, strip_tabs))
        return pos

    def _close(self, line: str, pos: int) -> tuple[str, int] | None:
        """Leave the active context when pos holds its closer."""
        top = self._ctx[-1]
        if top == "arith" and line.startswith("))", pos):
            self._ctx.pop()
            return "", pos + 2
        if _SH_CLOSER.get(top) != line[pos]:
            return None
        self._ctx.pop()
        return (")" if top == "paren" else ""), pos + 1

    def _open_dollar(self, line: str, pos: int, ansic: bool) -> tuple[str, int] | None:
        """Enter $'...', $((...)), $(...) or ${...} when pos opens one."""
        if line[pos] != "$":
            return None
        nxt = line[pos + 1 : pos + 3]
        if ansic and nxt.startswith("'"):
            self._push("ansic")
            return "", pos + 2
        if nxt == "((":
            self._push("arith")
            return "", pos + 3
        if nxt.startswith("("):
            self._push("cmd")
            return "", pos + 2
        if nxt.startswith("{"):
            self._push("param")
            return "__", pos + 2
        return None

    def _step_quoted(self, line: str, pos: int, top: str) -> tuple[str, int]:
        """Step inside double quotes or ${...}: expansions nest, other text masks."""
        opened = self._open_dollar(line, pos, ansic=False)
        if opened is not None:
            return opened
        ch = line[pos]
        if ch == "`" or (top == "param" and ch in "'\""):
            self._push(_SH_QUOTE_OPEN[ch])
            return "", pos + 1
        return ("_" if not ch.isspace() else ch), pos + 1

    def _step_code(self, line: str, pos: int, top: str) -> tuple[str, int]:
        """Step inside code, $(...), (...), backticks or $((...))."""
        opened = self._open_dollar(line, pos, ansic=True)
        if opened is not None:
            return opened
        ch = line[pos]
        if ch in _SH_QUOTE_OPEN:
            self._push(_SH_QUOTE_OPEN[ch])
            return "", pos + 1
        if ch == "(":
            self._push("paren")
            return ch, pos + 1
        if top == "arith":
            return ch, pos + 1
        if line.startswith("<<", pos) and not line.startswith("<<<", pos):
            return "", self._queue_heredoc(line, pos)
        if _sh_comment_hash(line, pos):
            return "", len(line)
        return ch, pos + 1

    def _step(self, line: str, pos: int) -> tuple[str, int]:
        """Consume one lexer step for the active context."""
        top = self._ctx[-1]
        if top != "sq" and line[pos] == "\\" and pos + 1 < len(line):
            return "__", pos + 2
        closed = self._close(line, pos)
        if closed is not None:
            return closed
        if top in _SH_LITERAL:
            return ("_" if not line[pos].isspace() else line[pos]), pos + 1
        if top in _SH_QUOTED:
            return self._step_quoted(line, pos, top)
        return self._step_code(line, pos, top)

    def _heredoc_line(self, raw: str) -> None:
        """Consume one heredoc body line; advance the queue when the delimiter closes."""
        if self._active_hd is None:
            return
        delim, strip_tabs = self._active_hd
        line = raw.lstrip("\t") if strip_tabs else raw
        if line == delim:
            self._active_hd = self._next_hd.pop(0) if self._next_hd else None

    def line(self, raw: str) -> str | None:
        """Return the code view for one physical line, or None when nesting overflows."""
        if self._active_hd is not None:
            self._heredoc_line(raw)
            return ""
        out: list[str] = []
        pos = 0
        while pos < len(raw):
            chunk, pos = self._step(raw, pos)
            if self._overflow:
                return None
            out.append(chunk)
        if self._next_hd:
            self._active_hd = self._next_hd.pop(0)
        return "".join(out)


def _shell_code_lines(lines: list[str]) -> list[str] | None:
    """Return per-line shell code views or None when nesting overflows."""
    scanner = _ShCodeScan()
    out: list[str] = []
    for raw in lines:
        view = scanner.line(raw)
        if view is None:
            return None
        out.append(view)
    return out


def _shell_complexity_chunk(chunk: str) -> int:
    """Count decision points in one code-view chunk."""
    score = len(SH_WORD_RE.findall(chunk))
    score += chunk.count("&&")
    score += chunk.count("||")
    score += chunk.count(";;")
    return score


def _shell_one_line_brace(rest: str) -> str | None:
    """Return inner code after { when rest is a one-line brace body."""
    text = rest.lstrip()
    if not text.startswith("{"):
        return None
    body = text[1:].rstrip()
    if not body.endswith("}"):
        return None
    inner = body[:-1].rstrip()
    if not inner:
        return ""
    trimmed = inner.rstrip()
    if not (trimmed.endswith(";") or trimmed.endswith("&")):
        return None
    return inner


def _shell_one_line_paren(rest: str) -> str | None:
    """Return inner code after ( when rest is a balanced one-line subshell body."""
    text = rest.lstrip()
    if not text.startswith("("):
        return None
    depth = 0
    for pos, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[1:pos]
    return None


def _shell_body_end(code: list[str], start: int, def_indent: int, closer: str) -> int:
    """Return the closer line index, -1 when no closer lies within the body bound, or the EOF index."""
    limit = min(len(code), start + 2 + MAX_SHELL_BODY_LINES)
    for idx in range(start + 1, limit):
        if code[idx].strip() != closer:
            continue
        if _line_indent(code[idx]) <= def_indent:
            return idx
    if limit >= len(code):
        return len(code) - 1
    return -1


def _shell_def_accept(rest: str) -> bool:
    """Return whether captured definition tail opens a brace or subshell body."""
    stripped = rest.strip()
    return not stripped or stripped.startswith("{") or stripped.startswith("(")


def _shell_body_layout(
    code: list[str], idx: int, def_indent: int, rest: str
) -> tuple[int, int, str]:
    """Return body_start index, body_end index, and opener-tail complexity text."""
    one_br = _shell_one_line_brace(rest)
    if one_br is not None:
        return idx + 1, idx, one_br
    one_par = _shell_one_line_paren(rest)
    if one_par is not None:
        return idx + 1, idx, one_par
    stripped = rest.lstrip()
    if stripped.startswith("("):
        end = _shell_body_end(code, idx, def_indent, ")")
        par = rest.find("(")
        opener = rest[par + 1 :].strip() if par >= 0 else ""
        return idx + 1, end, opener
    end = _shell_body_end(code, idx, def_indent, "}")
    brace = rest.find("{")
    opener = rest[brace + 1 :].strip() if brace >= 0 else ""
    return idx + 1, end, opener


def _shell_function_defs(
    lines: list[str], code: list[str]
) -> list[tuple[int, str, int, int, int, str]]:
    """Return shell def rows: line, name, index, body start/end, opener tail."""
    defs: list[tuple[int, str, int, int, int, str]] = []
    for idx in range(len(lines)):
        match = SHELL_DEF_LINE_RE.match(code[idx])
        if not match:
            continue
        name = match.group(1) or match.group(2)
        tail = match.group(3) or ""
        if not _shell_def_accept(tail):
            continue
        indent = _line_indent(code[idx])
        b_start, b_end, opener = _shell_body_layout(code, idx, indent, tail)
        defs.append((idx + 1, name, idx, b_start, b_end, opener))
    return defs


def _shell_has_docstring(lines: list[str], def_idx: int) -> bool:
    """Return whether contiguous # comment lines above the def qualify as a docstring."""
    idx = def_idx - 1
    while idx >= 0 and lines[idx].lstrip().startswith("#"):
        text = lines[idx].lstrip()
        if not text.startswith("#!") and not text.lower().startswith("# shellcheck"):
            return True
        idx -= 1
    return False


def _shell_body_complexity(
    code: list[str], def_idx: int, body_start: int, body_end: int, opener_tail: str
) -> int:
    """Count shell cyclomatic complexity inside a function body."""
    score = 1 + _shell_complexity_chunk(opener_tail)
    if body_end < body_start:
        return score
    for idx in range(body_start, body_end + 1):
        score += _shell_complexity_chunk(code[idx])
    return score


def check_shell_source(path: str, source: str, touched: set[int]) -> list[Finding]:
    """Analyze shell source text for missing docstrings on touched functions."""
    lines = lf_lines(source)
    code = _shell_code_lines(lines)
    if code is None:
        return [
            Finding(
                path,
                1,
                "analysis-limit",
                f"shell nesting deeper than {MAX_SHELL_NESTING}",
            )
        ]
    findings: list[Finding] = []
    for def_line, name, def_idx, body_start, body_end, opener in _shell_function_defs(
        lines, code
    ):
        span = _shell_function_span(def_line, def_idx, body_end, len(lines))
        if not span_touched(span, touched):
            continue
        if _shell_has_docstring(lines, def_idx):
            continue
        if body_end == -1:
            findings.append(
                Finding(
                    path,
                    def_line,
                    "analysis-limit",
                    f"function {name} body has no closer within {MAX_SHELL_BODY_LINES} lines; split it",
                )
            )
            continue
        complexity = _shell_body_complexity(code, def_idx, body_start, body_end, opener)
        if complexity < 2:
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