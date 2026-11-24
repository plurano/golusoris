# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Unit tests for branch-touched docstring coverage policy."""

from __future__ import annotations

import io
import os
import subprocess  # Runs only isolated fixture git commands. # nosec B404
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docstring_coverage_policy as policy


class GitFixture:
    """Isolated git repository for docstring coverage fixtures."""

    def __init__(self) -> None:
        """Create a temporary directory and initialise a main branch."""
        self._tmpdir = tempfile.TemporaryDirectory(prefix="golusoris-docstring-test-")
        self.root = Path(self._tmpdir.name)
        self._run(["init", "-q", "-b", "main"])

    def close(self) -> None:
        """Remove the temporary directory."""
        self._tmpdir.cleanup()

    def _env(self) -> dict[str, str]:
        """Build subprocess env without inherited GIT_* variables."""
        env = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        return env

    def _run(self, args: list[str]) -> None:
        """Run one git command inside the fixture repository."""
        proc = subprocess.run(  # noqa: S603 - git argv only. # nosec B603
            ["git", *args],  # noqa: S607 - git on PATH. # nosec B607
            cwd=self.root,
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr.strip() or proc.stdout.strip())

    def write(self, rel: str, text: str) -> None:
        """Write a tracked-path fixture file under the repository root."""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_bytes(self, rel: str, data: bytes) -> None:
        """Write raw bytes for newline fixtures under the repository root."""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def commit(self, message: str = "commit") -> None:
        """Stage all changes and create one signed-off commit."""
        self._run(["add", "-A"])
        self._run(
            [
                "-c",
                "user.name=policy",
                "-c",
                "user.email=policy@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                message,
            ]
        )

    def mark_origin(self) -> None:
        """Point refs/remotes/origin/main at the current HEAD."""
        self._run(["update-ref", "refs/remotes/origin/main", "HEAD"])

    def branch(self, name: str = "topic") -> None:
        """Create and switch to a topic branch."""
        self._run(["switch", "-q", "-c", name])


class DocstringCoveragePolicyTest(unittest.TestCase):
    """Exercise planted positive, boundary, negative, wiring, and self-check cases."""

    def setUp(self) -> None:
        """Allocate one isolated git fixture per test."""
        self.fx = GitFixture()
        self.addCleanup(self.fx.close)

    def findings(self, *paths: str, base: str = "origin/main") -> list[policy.Finding]:
        """Run the policy check API against the fixture repository."""
        base_ref = base
        if base == "origin/main":
            base_ref = "refs/remotes/origin/main"
        base_sha = policy.merge_base_sha(self.fx.root, base_ref)
        return policy.check(self.fx.root, base_sha, list(paths))

    def start_topic(self) -> None:
        """Record origin/main at the current commit and open the topic branch."""
        self.fx.write(".gitkeep", "")
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()

    def branch_fixture(self, rel: str, base: str, topic: str) -> list[policy.Finding]:
        """Check rel in a fresh fixture whose topic branch turns base into topic."""
        fx = GitFixture()
        self.addCleanup(fx.close)
        fx.write(rel, base)
        fx.commit("base")
        fx.mark_origin()
        fx.branch()
        fx.write(rel, topic)
        fx.commit("topic")
        base_sha = policy.merge_base_sha(fx.root, "refs/remotes/origin/main")
        return policy.check(fx.root, base_sha, [rel])

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        """Invoke main() with patched repository root and captured stdio."""
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.object(sys, "argv", ["docstring_coverage_policy.py", *argv]),
            patch.object(policy, "policy_repo_root", return_value=self.fx.root),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = policy.main()
        return code, out.getvalue(), err.getvalue()

    def test_p1_python_missing_docstring(self) -> None:
        """P1: touched complex Python function without a docstring is reported."""
        self.start_topic()
        self.fx.write("scripts/ci/x.py", "def f(a):\n    if a:\n        return 1\n    return 2\n")
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.py")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rule, "missing-docstring")
        self.assertEqual(found[0].line, 1)

    def test_c1_python_with_docstring(self) -> None:
        """C1: documented complex Python function yields no findings."""
        text = (
            "def f(a):\n"
            '    """Return 1 when a is set."""\n'
            "    if a:\n"
            "        return 1\n"
            "    return 2\n"
        )
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_b1_python_simple_undocumented(self) -> None:
        """B1: complexity-one functions are out of scope."""
        text = "def f():\n    return 1\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_b2_python_untouched_complex(self) -> None:
        """B2: untouched complex functions are not reported."""
        base = (
            "def f():\n    if True:\n        return 1\n    return 2\n\n"
            'def g():\n    """g."""\n    return 1\n'
        )
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = (
            "def f():\n    if True:\n        return 1\n    return 2\n\n"
            'def g():\n    """g."""\n    return 2\n'
        )
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_b3_python_legacy_touched(self) -> None:
        """B3: editing inside a legacy undocumented complex function is reported."""
        base = "def f():\n    if True:\n        return 1\n    return 2\n"
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = "def f():\n    if True:\n        return 9\n    return 2\n"
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.py")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rule, "missing-docstring")

    def test_b4_python_deletion_inside_function(self) -> None:
        """B4: partial deletions inside a surviving function still count as touched."""
        base = (
            "def f():\n"
            "    if True:\n"
            "        x = 1\n"
            "        return 1\n"
            "    return 2\n"
        )
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = "def f():\n    if True:\n        return 1\n    return 2\n"
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.py")), 1)

    def test_b5_python_whole_function_deleted(self) -> None:
        """B5: deleting the entire complex function clears findings."""
        base = "def f():\n    if True:\n        return 1\n    return 2\n\nx = 1\n"
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        self.fx.write("scripts/ci/x.py", "x = 2\n")
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_b6_python_boolop(self) -> None:
        """B6: BoolOp raises complexity to two without a docstring."""
        text = "def f(a, b):\n    return a and b\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.py")), 1)

    def test_b7_python_nested_inner_only(self) -> None:
        """B7: only the undocumented inner function is reported."""
        text = (
            'def outer():\n    """outer."""\n'
            "    def inner():\n        if True:\n            return 1\n        return 2\n"
        )
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.py")
        self.assertEqual(len(found), 1)
        self.assertIn("inner", found[0].message)

    def test_b8_python_method(self) -> None:
        """B8: undocumented complex methods are reported."""
        text = "class C:\n    def m(self):\n        if True:\n            return 1\n        return 2\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.py")), 1)

    def test_b9_committed_blob_not_worktree(self) -> None:
        """B9: HEAD blob is checked even when the worktree has a new docstring."""
        text = "def f():\n    if True:\n        return 1\n    return 2\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        worktree = (
            "def f():\n"
            '    """added."""\n'
            "    if True:\n"
            "        return 1\n"
            "    return 2\n"
        )
        self.fx.write("scripts/ci/x.py", worktree)
        self.assertEqual(len(self.findings("scripts/ci/x.py")), 1)

    def test_b10_scope_and_windows_path(self) -> None:
        """B10: README is skipped and Windows-style paths normalize."""
        text = "def f(a):\n    if a:\n        return 1\n    return 2\n"
        self.fx.write("README.md", "# noop\n")
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        found = self.findings("README.md", "scripts\\ci\\x.py")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path.replace("\\", "/"), "scripts/ci/x.py")

    def test_b11_empty_paths_main_skip(self) -> None:
        """B11: empty --pushed path list prints skip note and exits zero."""
        code, out, _ = self.run_main(["--pushed"])
        self.assertEqual(code, 0)
        self.assertIn("skipping", out)

    def test_p2_shell_missing_docstring(self) -> None:
        """P2: touched complex shell function without a docstring is reported."""
        text = "f() {\n\tif [ -n \"$1\" ]; then echo a; fi\n}\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.sh")), 1)

    def test_c2_shell_with_docstring(self) -> None:
        """C2: shell doc comment above the function clears findings."""
        text = "# Print a when set.\nf() {\n\tif [ -n \"$1\" ]; then echo a; fi\n}\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.sh"), [])

    def test_b12_shell_shellcheck_only(self) -> None:
        """B12: shellcheck-only comments do not satisfy the docstring rule."""
        text = "# shellcheck disable=SC2034\nf() {\n\tif [ -n \"$1\" ]; then echo a; fi\n}\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.sh")), 1)

    def test_b13_shell_function_keyword(self) -> None:
        """B13: function-keyword form with logical AND is complex and undocumented."""
        text = "function g {\n    a && b\n}\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.sh")), 1)

    def test_b14_shell_dollar_hash_not_comment(self) -> None:
        """B14: $# inside a test must not be treated as starting a comment."""
        text = 'f() {\n\t[ "$#" -gt 1 ] && echo a\n}\n'
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.sh")), 1)

    def test_b15_path_overflow_note(self) -> None:
        """B15: overflow paths beyond MAX_FILES emit a truncation note on stderr."""
        self.start_topic()
        self.fx.write("scripts/ci/a.py", "def f():\n    return 1\n")
        self.fx.write("scripts/ci/b.py", "def g():\n    return 1\n")
        self.fx.commit("topic")
        with patch.object(policy, "MAX_FILES", 1):
            code, _, err = self.run_main(["--pushed", "scripts/ci/a.py", "scripts/ci/b.py"])
        self.assertEqual(code, 0)
        self.assertIn("1 paths not checked", err)

    def test_b16_long_hunk_reaches_late_function(self) -> None:
        """B16: a function past line 16385 of one added hunk still counts as touched."""
        text = "x = 0\n" * 16_400 + "def f():\n    if True:\n        return 1\n    return 2\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.py")
        self.assertEqual([item.line for item in found], [16_401])

    def test_b17_hunk_overflow_checks_every_function(self) -> None:
        """B17: past MAX_HUNKS every function counts as touched, never fewer."""
        body = "def {0}():\n    if True:\n        return 1\n    return 2\n"
        base = body.format("f") + "x = 0\n" * 5 + body.format("g")
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = base.replace("return 1", "return 3")
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        with patch.object(policy, "MAX_HUNKS", 1):
            found = self.findings("scripts/ci/x.py")
        self.assertEqual(sorted(item.message.split()[1] for item in found), ["f", "g"])

    def test_b18_long_span_late_touch(self) -> None:
        """B18: a touch past line 16385 of a long function counts as touched."""
        self.assertTrue(policy.span_touched((1, 20_000), {19_999}))
        self.assertFalse(policy.span_touched((1, 20_000), {20_001}))

    def test_b19_raw_path_bound_counts_dropped(self) -> None:
        """B19: raw paths past MAX_RAW_PATHS count as not checked, never silently."""
        with patch.object(policy, "MAX_RAW_PATHS", 1):
            kept, overflow = policy.normalize_paths(["scripts/ci/a.py", "scripts/ci/b.py"])
        self.assertEqual((kept, overflow), (["scripts/ci/a.py"], 1))

    def test_n1_python_parse_error(self) -> None:
        """N1: syntax errors surface as parse-error findings."""
        text = "def f(:"
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        code, _, _ = self.run_main(["--pushed", "scripts/ci/x.py"])
        self.assertEqual(code, 1)
        found = self.findings("scripts/ci/x.py")
        self.assertEqual(found[0].rule, "parse-error")

    def test_n2_missing_base_main(self) -> None:
        """N2: missing origin/main merge base fails closed with exit code three."""
        self.fx.write("scripts/ci/x.py", "def f():\n    return 1\n")
        self.fx.commit("base")
        self.fx.branch()
        self.fx.write("scripts/ci/x.py", "def f():\n    return 2\n")
        self.fx.commit("topic")
        code, _, err = self.run_main(["--pushed", "scripts/ci/x.py"])
        self.assertEqual(code, 3)
        self.assertIn("origin/main", err)

    def test_n3_file_too_large(self) -> None:
        """N3: sources above MAX_SOURCE_LINES yield a file-too-large finding."""
        text = "\n".join(f"x{i} = {i}" for i in range(1, 6))
        with patch.object(policy, "MAX_SOURCE_LINES", 3):
            found = policy.check_source("x.py", text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rule, "file-too-large")

    def test_n4_git_diff_failure_after_merge_base(self) -> None:
        """N4: git failures after merge-base resolution report git diff, not merge base."""
        self.start_topic()
        self.fx.write("scripts/ci/x.py", "def f():\n    return 1\n")
        self.fx.commit("topic")
        boom = subprocess.CalledProcessError(128, ["diff"], "", "boom")
        with patch.object(policy, "diff_touched_lines", side_effect=boom):
            code, _, err = self.run_main(["--pushed", "scripts/ci/x.py"])
        self.assertEqual(code, 3)
        self.assertIn("git diff failed", err)
        self.assertNotIn("merge base", err)

    def test_w1_wiring(self) -> None:
        """W1: lefthook and python-tests register docstring coverage."""
        root = Path(__file__).resolve().parents[2]
        hook = (root / "lefthook.yml").read_text(encoding="utf-8")
        block = hook.split("pre-push:")[1].split("\n\n", 1)[0]
        self.assertIn("    docstring-coverage:", block)
        run_line = (
            "      run: sh .config/lefthook/python.sh -B scripts/ci/docstring_coverage_policy.py --pushed {push_files}"
        )
        self.assertIn(run_line, block)
        suite = (root / "scripts/ci/python-tests.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/ci/docstring_coverage_policy_test.py", suite)

    def test_s1_self_check(self) -> None:
        """S1: policy sources satisfy their own docstring coverage rule."""
        root = Path(__file__).resolve().parent
        for name in ("docstring_coverage_policy.py", "docstring_coverage_policy_test.py"):
            path = root / name
            text = path.read_text(encoding="utf-8")
            self.assertEqual(policy.check_source(str(path), text), [])

    def test_b20_python_lone_cr_line_numbers(self) -> None:
        """B20: lone CR in blob must not hide a touched complex Python function."""
        data = (
            b"# a\rb\rc\rd\re\rf\rg\n"
            b"def f(a):\n"
            b"    if a:\n"
            b"        return 1\n"
            b"    return 2\n"
        )
        self.start_topic()
        self.fx.write_bytes("scripts/ci/x.py", data)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.py")
        self.assertEqual([item.line for item in found], [2])
        self.assertEqual(found[0].rule, "missing-docstring")

    def test_b21_shell_lone_cr_line_numbers(self) -> None:
        """B21: lone CR in blob must not hide a touched complex shell function."""
        data = (
            b"x=a\rb\rc\rd\re\rf\rg\n"
            b"f() {\n"
            b'  if [ -n "$1" ]; then echo a; fi\n'
            b"}\n"
        )
        self.start_topic()
        self.fx.write_bytes("scripts/ci/x.sh", data)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.sh")
        self.assertEqual([item.line for item in found], [2])

    def test_b22_python_crlf_line_numbers(self) -> None:
        """B22: CRLF Python sources still report findings on the correct git line."""
        data = b"x = 1\r\ndef f(a):\r\n    if a:\r\n        return 1\r\n    return 2\r\n"
        self.start_topic()
        self.fx.write_bytes("scripts/ci/x.py", data)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.py")
        self.assertEqual([item.line for item in found], [2])

    def test_b23_shell_crlf_line_numbers(self) -> None:
        """B23: CRLF shell sources still report findings on the correct git line."""
        data = b"f() {\r\n  if [ -n \"$1\" ]; then echo a; fi\r\n}\r\n"
        self.start_topic()
        self.fx.write_bytes("scripts/ci/x.sh", data)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.sh")
        self.assertEqual([item.line for item in found], [1])

    def test_n10_diff_content_cr_no_fake_hunk(self) -> None:
        """N10: CR inside a diff content line must not start a fake hunk header."""
        touched = policy.parse_touched_lines("@@ -0,0 +1 @@\n+x\r@@ -1 +900 @@\n")
        self.assertEqual(touched, {1})

    def test_b24_python_lone_cr_untouched_function(self) -> None:
        """B24: lone CR must not widen spans so an untouched function is reported."""
        base = (
            b"# a\rb\n"
            b"def f(a):\n"
            b"    if a:\n"
            b"        return 1\n"
            b"    return 2\n"
            b"x = 1\n"
        )
        self.fx.write_bytes("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        self.fx.write_bytes("scripts/ci/x.py", base.replace(b"x = 1\n", b"x = 2\n"))
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_n11_lone_cr_lines_count_toward_bound(self) -> None:
        """N11: lone CR lines count toward MAX_SOURCE_LINES; exactly the bound passes."""
        with patch.object(policy, "MAX_SOURCE_LINES", 3):
            over = policy.check_source("scripts/ci/x.py", "a = 1\rb = 2\rc = 3\rd = 4\n")
            at = policy.check_source("scripts/ci/x.py", "a = 1\rb = 2\rc = 3\n")
        self.assertEqual([item.rule for item in over], ["file-too-large"])
        self.assertEqual(at, [])

    def test_n12_head_blob_read_failure_fails_closed(self) -> None:
        """N12: git show failure after ls-tree must exit 3, not skip the path."""
        real = policy._run_git_text

        def boom(repo_root: Path, args: list[str]) -> str:
            """Fail git show; pass every other git call through."""
            if args and args[0] == "show":
                raise subprocess.CalledProcessError(128, ["show"], "", "fatal: bad object")
            return real(repo_root, args)

        self.start_topic()
        self.fx.write("scripts/ci/x.py", "def f():\n    return 1\n")
        self.fx.commit("topic")
        with patch.object(policy, "_run_git_text", side_effect=boom):
            code, _, err = self.run_main(["--pushed", "scripts/ci/x.py"])
        self.assertEqual(code, 3)
        self.assertIn("git show failed", err)

    def test_b49_non_blob_entry_is_absent(self) -> None:
        """B49: a scoped path that names a tree at the revision reads as absent."""
        self.fx.write("scripts/ci/d.sh/inner.txt", "x\n")
        self.fx.commit("base")
        self.assertIsNone(policy.blob_text(self.fx.root, "HEAD", "scripts/ci/d.sh"))
        self.assertEqual(policy.blob_text(self.fx.root, "HEAD", "scripts/ci/d.sh/inner.txt"), "x\n")

    def test_b25_path_absent_at_head_skipped(self) -> None:
        """B25: deleting a path before push skips it instead of failing open."""
        self.fx.write(
            "scripts/ci/x.py",
            "def f(a):\n    if a:\n        return 1\n    return 2\n",
        )
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        (self.fx.root / "scripts/ci/x.py").unlink()
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_b26_shell_def_trailing_comment(self) -> None:
        """B26: brace opener with trailing comment still parses the function body."""
        text = "f() { # implementation\n  if [ -n \"$1\" ]; then echo a; fi\n}\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.sh")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 1)

    def test_b27_shell_one_line_body(self) -> None:
        """B27: one-line shell function bodies are checked."""
        text = "f() { if true; then :; fi; }\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.sh")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 1)

    def test_b28_shell_one_line_simple(self) -> None:
        """B28: trivial one-line shell functions stay out of scope."""
        text = "f() { echo a; }\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.sh"), [])

    def test_b29_shell_one_line_then_function(self) -> None:
        """B29: only the multi-line neighbor is reported when the prior def is simple."""
        text = "f() { echo a; }\ng() {\n  if a; then b; fi\n}\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.sh")
        self.assertEqual([item.line for item in found], [2])

    def test_b30_shell_subshell_body(self) -> None:
        """B30: parenthesized subshell bodies count complexity."""
        text = "f() (\n  if [ -n \"$1\" ]; then echo a; fi\n)\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.sh")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 1)

    def test_b31_shell_keyword_prefixed_name(self) -> None:
        """B31: function names containing keywords do not inflate complexity."""
        text = "for-each() { echo a; }\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.sh"), [])

    def test_b32_deleting_neighbor_keeps_function_clean(self) -> None:
        """B32: deletion-only hunks on a neighbor do not touch surviving functions."""
        base = (
            "def f(a):\n    if a:\n        return 1\n    return 2\n"
            "def g():\n    return 3\n"
        )
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = "def f(a):\n    if a:\n        return 1\n    return 2\n"
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_b33_tail_deletion_touches_function(self) -> None:
        """B33: deleting tail lines inside a function reports it."""
        base = "def f(a):\n    if a:\n        return 1\n    x = 2\n    return x\n"
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = "def f(a):\n    if a:\n        return 1\n    x = 2\n"
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.py")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 1)

    def test_b34_method_deletion_qualname(self) -> None:
        """B34: deletion hunks attribute to qualified Python method names."""
        base = (
            "class C:\n"
            "    def m(self, a):\n"
            "        if a:\n"
            "            x = 1\n"
            "            return 1\n"
            "        return 2\n"
            "    def n(self):\n"
            "        return 3\n"
        )
        cases = (
            (base.replace("            x = 1\n", ""), 1, "C.m"),
            (base.replace("    def n(self):\n        return 3\n", ""), 0, ""),
        )
        for topic, expect_len, qual in cases:
            with self.subTest(qual=qual or "none"):
                found = self.branch_fixture("scripts/ci/x.py", base, topic)
                self.assertEqual(len(found), expect_len)
                if qual:
                    self.assertIn(qual, found[0].message)

    def test_b35_shell_deleting_neighbor(self) -> None:
        """B35: shell deletion attribution leaves surviving neighbors clean."""
        cases = (
            (
                "f() {\n  if a; then b; fi\n}\ng() {\n  echo y\n}\n",
                "f() {\n  if a; then b; fi\n}\n",
                0,
                0,
            ),
            (
                "f() {\n  if a; then b; fi\n  echo x\n}\n",
                "f() {\n  if a; then b; fi\n}\n",
                1,
                1,
            ),
        )
        for base, topic, expect_len, line in cases:
            with self.subTest(line=line):
                found = self.branch_fixture("scripts/ci/x.sh", base, topic)
                self.assertEqual(len(found), expect_len)
                if line:
                    self.assertEqual(found[0].line, line)

    def test_b36_unparseable_base_falls_back(self) -> None:
        """B36: unparseable base blobs fall back to deletion anchors."""
        base = "def f(a):\n    if a:\n        return 1\n    return 2\n)\n"
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = "def f(a):\n    if a:\n        return 1\n    return 2\n"
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        found = self.findings("scripts/ci/x.py")
        self.assertEqual(len(found), 1)

    def test_b37_python_definition_time_expressions(self) -> None:
        """B37: defaults and annotations do not inflate Python complexity."""
        text = (
            "def f(value=a or b):\n    return value\n"
            "@d(1 if x else 2)\n"
            "def g():\n    return 1\n"
            "def h(a: int if X else str) -> None:\n    return None\n"
        )
        self.start_topic()
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_b38_shell_body_over_bound(self) -> None:
        """B38: oversized shell bodies yield analysis-limit, not silent passes."""
        over = "f() {\n  :\n  :\n  :\n  :\n  if a; then b; fi\n}\n"
        with patch.object(policy, "MAX_SHELL_BODY_LINES", 3):
            found = policy.check_source("scripts/ci/x.sh", over)
        self.assertEqual([item.rule for item in found], ["analysis-limit"])

        at_bound = "f() {\n  :\n  :\n  if a; then b; fi\n}\n"
        with patch.object(policy, "MAX_SHELL_BODY_LINES", 3):
            found = policy.check_source("scripts/ci/x.sh", at_bound)
        self.assertEqual([item.rule for item in found], ["missing-docstring"])

        documented = "# doc\n" + over
        with patch.object(policy, "MAX_SHELL_BODY_LINES", 3):
            self.assertEqual(policy.check_source("scripts/ci/x.sh", documented), [])

    def test_b39_shell_quoted_keyword(self) -> None:
        """B39: quoted shell keywords do not count as decisions."""
        text = "f() {\n  printf '%s\\n' 'if'\n}\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.sh"), [])

    def test_b40_shell_quoted_hash_keeps_decision(self) -> None:
        """B40: decisions after quoted segments still count."""
        text = "f() {\n  printf '%s\\n' ' # value'; false || true\n}\n"
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.sh")), 1)

    def test_b41_shell_dquote_subst_counts(self) -> None:
        """B41: command substitutions inside double quotes count decisions."""
        text = 'f() {\n  x="$(git rev-parse HEAD || true)"\n}\n'
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(len(self.findings("scripts/ci/x.sh")), 1)

    def test_b42_shell_heredoc_skipped(self) -> None:
        """B42: heredoc bodies do not count shell keywords."""
        for opener in ("<<EOF", "<<'EOF'", "<<-EOF"):
            with self.subTest(opener=opener):
                closer = "\tEOF" if opener == "<<-EOF" else "EOF"
                body = (
                    "f() {\n"
                    f"  cat {opener}\n"
                    "if this\n"
                    "}\n"
                    f"{closer}\n"
                    "}\n"
                )
                self.assertEqual(self.branch_fixture("scripts/ci/x.sh", "", body), [])

    def test_b43_shell_multiline_dquote(self) -> None:
        """B43: multiline double quotes mask embedded keywords."""
        text = 'f() {\n  echo "a\nif b\n"\n}\n'
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.sh"), [])

    def test_b44_shell_param_expansion_text(self) -> None:
        """B44: parameter expansions mask keyword text."""
        text = 'f() {\n  echo "${v:-if}" ${#v}\n}\n'
        self.start_topic()
        self.fx.write("scripts/ci/x.sh", text)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.sh"), [])

    def test_b45_shell_contexts_close_cleanly(self) -> None:
        """B45: nested expansions, arithmetic and subshells leave later functions scanned."""
        tail = "g() {\n  if a; then b; fi\n}\n"
        openers = (
            '"$(a)"',
            '"`a`"',
            '"${v:-$(a)}"',
            '$(echo "$(a)")',
            "$(( 1 << 2 ))",
            "$( (a) )",
            "$'a\\'b'",
        )
        for opener in openers:
            with self.subTest(opener=opener):
                text = f"f() {{\n  x={opener}\n}}\n{tail}"
                found = policy.check_source("scripts/ci/x.sh", text)
                self.assertEqual([(item.line, item.rule) for item in found], [(4, "missing-docstring")])
                self.assertIn("function g", found[0].message)
        masked = 'f() {\n  x="$( (a) "if" )"\n}\n'
        self.assertEqual(policy.check_source("scripts/ci/x.sh", masked), [])

    def test_b46_shell_heredoc_in_substitution(self) -> None:
        """B46: a heredoc opened inside $(...) masks its body and restores code after it."""
        text = "f() {\n  x=$(cat <<'EOF'\nif this\nEOF\n)\n}\ng() {\n  if a; then b; fi\n}\n"
        found = policy.check_source("scripts/ci/x.sh", text)
        self.assertEqual([(item.line, item.rule) for item in found], [(7, "missing-docstring")])

    def test_b47_shell_heredoc_delimiter_exact(self) -> None:
        """B47: a heredoc line that only resembles the delimiter does not close it."""
        text = "f() {\n  cat <<EOF\nEOF \nif x\nEOF\n}\n"
        self.assertEqual(policy.check_source("scripts/ci/x.sh", text), [])
        closed = "f() {\n  cat <<EOF\nEOF\nif x; then y; fi\n}\n"
        self.assertEqual(len(policy.check_source("scripts/ci/x.sh", closed)), 1)

    def test_b48_malformed_hunk_header_fails_closed(self) -> None:
        """B48: an unparseable hunk header treats every line as touched."""
        self.assertIsNone(policy.parse_diff("@@ -1 +1 @@\n@@ bogus @@\n"))
        self.assertEqual(policy.parse_touched_lines("@@ -1 +1 @@\n"), {1})

    def test_n13_shell_nesting_overflow(self) -> None:
        """N13: shell nesting beyond the bound yields analysis-limit."""
        text = 'f() {\n  echo "$(echo "$(echo x)")"\n}\n'
        with patch.object(policy, "MAX_SHELL_NESTING", 2):
            found = policy.check_source("scripts/ci/x.sh", text)
        self.assertEqual([item.rule for item in found], ["analysis-limit"])

    def test_n14_parse_diff_shapes(self) -> None:
        """N14: deletion-only hunks populate deleted ranges, not touched lines."""
        parsed = policy.parse_diff("@@ -5,2 +4,0 @@\n")
        self.assertEqual(parsed, policy.DiffTouch(set(), [(5, 6)], [4], [(5, 2, 4, 0)]))
        self.assertEqual(policy.parse_diff("@@ -1 +1 @@\n").lines, {1})
        self.assertEqual(policy.parse_touched_lines("@@ -5,2 +4,0 @@\n"), set())
        start = policy.parse_diff("@@ -1,2 +0,0 @@\n")
        self.assertEqual(start.anchors, [1])

    def test_b50_python_duplicate_name_deletion(self) -> None:
        """B50: duplicate Python names attribute deletions by owner line, not by name."""
        base = (
            "def f(a):\n"
            "    if a:\n"
            "        return 1\n"
            "    return 2\n"
            "\n"
            "\n"
            "def f(b):\n"
            "    x = 1\n"
            "    return b\n"
        )
        base_y = (
            "def f(a):\n"
            "    if a:\n"
            "        return 1\n"
            "    y = 2\n"
            "    return 2\n"
            "\n"
            "\n"
            "def f(b):\n"
            "    return b\n"
        )
        cases = (
            (base, base.replace("    x = 1\n", ""), 0, 0),
            (base_y, base_y.replace("    y = 2\n", ""), 1, 1),
        )
        for b, topic, expect_len, line in cases:
            with self.subTest(expect_len=expect_len):
                found = self.branch_fixture("scripts/ci/x.py", b, topic)
                self.assertEqual(len(found), expect_len)
                if line:
                    self.assertEqual(found[0].line, line)

    def test_b51_shell_duplicate_name_deletion(self) -> None:
        """B51: duplicate shell names attribute deletions by owner line, not by name."""
        base = "f() {\n  if a; then b; fi\n}\nf() {\n  echo x\n  echo y\n}\n"
        cases = (
            (base, base.replace("  echo y\n", ""), 0, 0),
            (
                "f() {\n  if a; then b; fi\n  echo x\n}\nf() {\n  echo y\n}\n",
                "f() {\n  if a; then b; fi\n}\nf() {\n  echo y\n}\n",
                1,
                1,
            ),
        )
        for b, topic, expect_len, line in cases:
            with self.subTest(line=line):
                found = self.branch_fixture("scripts/ci/x.sh", b, topic)
                self.assertEqual(len(found), expect_len)
                if line:
                    self.assertEqual(found[0].line, line)

    def test_b52_python_conditional_defs(self) -> None:
        """B52: Python defs under control flow are scanned and attributed on touch."""
        body = (
            "    def f(a):\n"
            "        if a:\n"
            "            return 1\n"
            "        x = 1\n"
            "        return 2\n"
        )
        wrappers = (
            ("if True:\n", "", 2),
            ("try:\n", "except OSError:\n    pass\n", 2),
            ("with ctx:\n", "", 2),
            ("if False:\n    pass\nelse:\n", "", 4),
            ("for _ in x:\n", "", 2),
        )
        for prefix, suffix, def_line in wrappers:
            with self.subTest(prefix=prefix[:12]):
                base = prefix + body + suffix
                topic = prefix + body.replace("        x = 1\n", "") + suffix
                found = self.branch_fixture("scripts/ci/x.py", base, topic)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].rule, "missing-docstring")
                self.assertEqual(found[0].line, def_line)
        lone = (
            "if True:\n"
            "    def f(a):\n"
            "        if a:\n"
            "            return 1\n"
            "        return 2\n"
        )
        rules = [item.rule for item in policy.check_source("scripts/ci/x.py", lone)]
        self.assertEqual(rules, ["missing-docstring"])
        neighbor_base = (
            "if True:\n"
            "    def f(a):\n"
            "        if a:\n"
            "            return 1\n"
            "        return 2\n"
            "    def g():\n"
            "        x = 1\n"
            "        return 0\n"
        )
        neighbor_topic = neighbor_base.replace("        x = 1\n", "")
        self.assertEqual(self.branch_fixture("scripts/ci/x.py", neighbor_base, neighbor_topic), [])

    def test_b53_shell_over_bound_body_touch(self) -> None:
        """B53: over-bound shell bodies still see body-line touches and deletions."""
        with patch.object(policy, "MAX_SHELL_BODY_LINES", 3):
            base = "f() {\n  :\n  :\n  :\n  :\n  :\n}\n"
            topic = "f() {\n  :\n  :\n  if a; then b; fi\n  :\n  :\n  :\n}\n"
            rules = [item.rule for item in self.branch_fixture("scripts/ci/x.sh", base, topic)]
            self.assertEqual(rules, ["analysis-limit"])
            base2 = "f() {\n  :\n  :\n  :\n  :\n  if a; then b; fi\n  :\n}\n"
            topic2 = "f() {\n  :\n  :\n  :\n  :\n  if a; then b; fi\n}\n"
            rules2 = [item.rule for item in self.branch_fixture("scripts/ci/x.sh", base2, topic2)]
            self.assertEqual(rules2, ["analysis-limit"])
            doc = "# doc\n" + base
            doc_topic = "# doc\n" + topic
            self.assertEqual(self.branch_fixture("scripts/ci/x.sh", doc, doc_topic), [])

    def test_b54_deletion_owner_line_shifts(self) -> None:
        """B54: deletion owner lines follow inter-function insertions in the new file."""
        base = (
            "def g():\n"
            "    return 0\n"
            "\n"
            "\n"
            "def f(a):\n"
            "    if a:\n"
            "        return 1\n"
            "    x = 1\n"
            "    return 2\n"
        )
        topic = (
            'def h():\n    """Doc."""\n    return 0\n\n\n'
            + base.replace("    return 0", "    return 1", 1).replace("    x = 1\n", "")
        )
        found = self.branch_fixture("scripts/ci/x.py", base, topic)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, "scripts/ci/x.py")
        self.assertEqual(found[0].line, 10)

    @unittest.skipIf(sys.platform == "win32", "literal pathspec colon names are unix-specific")
    def test_b55_literal_pathspec_name(self) -> None:
        """B55: git magic pathspec filenames are read via literal pathspecs."""
        topic = "x = 1\n\n\ndef f(a):\n    if a:\n        return 1\n    return 2\n"
        found = self.branch_fixture(":(literal)x.py", "x = 1\n", topic)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rule, "missing-docstring")
        self.assertEqual(found[0].path, ":(literal)x.py")
        self.assertEqual(found[0].line, 4)

    def test_b56_diff_attribute_text_patch(self) -> None:
        """B56: -diff attributes still yield textual hunks with --text."""
        self.fx.write(".gitattributes", "*.py -diff\n")
        self.fx.write("scripts/ci/x.py", "x = 1\n")
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = "x = 1\n\n\ndef f(a):\n    if a:\n        return 1\n    return 2\n"
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        base_sha = policy.merge_base_sha(self.fx.root, "refs/remotes/origin/main")
        found = policy.check(self.fx.root, base_sha, ["scripts/ci/x.py"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 4)

    def test_b57_inter_hunk_context_config(self) -> None:
        """B57: diff.interHunkContext must not merge separate edits into one hunk."""
        self.fx._run(["config", "diff.interHunkContext", "10"])
        base = (
            "def g():\n"
            '    """Doc."""\n'
            "    return 0\n"
            "\n"
            "\n"
            "def f(a):\n"
            "    if a:\n"
            "        return 1\n"
            "    return 2\n"
            "\n"
            "\n"
            "def h():\n"
            '    """Doc."""\n'
            "    return 0\n"
        )
        topic = (
            "def g():\n"
            '    """Doc."""\n'
            "    return 3\n"
            "\n"
            "\n"
            "def f(a):\n"
            "    if a:\n"
            "        return 1\n"
            "    return 2\n"
            "\n"
            "\n"
            "def h():\n"
            '    """Doc."""\n'
            "    return 4\n"
        )
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        self.fx.write("scripts/ci/x.py", topic)
        self.fx.commit("topic")
        self.assertEqual(self.findings("scripts/ci/x.py"), [])

    def test_b58_here_string_not_heredoc(self) -> None:
        """B58: <<< here-strings must not start a heredoc scan."""
        cases = (
            'f() {\n  grep -q x <<<"$v"\n  if a; then b; fi\n}\n',
            "f() {\n  cat <<< value\n  if a; then b; fi\n}\n",
        )
        for text in cases:
            with self.subTest(text=text[:20]):
                found = policy.check_source("scripts/ci/x.sh", text)
                self.assertEqual([item.rule for item in found], ["missing-docstring"])
                self.assertEqual(found[0].line, 1)

    def test_b59_heredoc_delimiter_operator(self) -> None:
        """B59: heredoc delimiters stop at shell operators, not mid-word."""
        tail = "g() {\n  if a; then b; fi\n}\n"
        for opener in ("<<EOF;", '<<"EOF";', '<<E"O"F;', "<<\\EOF;", "<<EOF|tr a b"):
            with self.subTest(opener=opener):
                text = "g() {\n  cat " + opener + "\ntext\nEOF\n}\n" + tail
                found = policy.check_source("scripts/ci/x.sh", text)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].rule, "missing-docstring")
                self.assertEqual(found[0].line, 6)

    def test_b60_over_bound_span_ends_at_closer(self) -> None:
        """B60: over-bound bodies end at the real closer, not at the line bound."""
        text = (
            "f() {\n"
            "  :\n"
            "  :\n"
            "  :\n"
            "  :\n"
            "}\n"
            "# doc\n"
            "g() {\n"
            "  if a; then b; fi\n"
            "}\n"
        )
        with patch.object(policy, "MAX_SHELL_BODY_LINES", 3):
            self.assertEqual(policy.check_source("scripts/ci/x.sh", text, {9}), [])
            found = policy.check_source("scripts/ci/x.sh", text, {3})
        self.assertEqual([item.rule for item in found], ["analysis-limit"])
        self.assertEqual(found[0].line, 1)

    def test_b61_shell_doc_comment_deletion(self) -> None:
        """B61: deleting shell doc comments above a def counts as touching the function."""
        cases = (
            ("# Explain f\nf() {\n  if a; then b; fi\n}\n", "f() {\n  if a; then b; fi\n}\n", 1),
            ("# Note\n\nf() {\n  if a; then b; fi\n}\n", "\nf() {\n  if a; then b; fi\n}\n", 0),
            ("#!/bin/sh\nf() {\n  if a; then b; fi\n}\n", "f() {\n  if a; then b; fi\n}\n", 0),
        )
        for base, topic, expect_len in cases:
            with self.subTest(expect_len=expect_len):
                found = self.branch_fixture("scripts/ci/x.sh", base, topic)
                self.assertEqual(len(found), expect_len)
                if expect_len:
                    self.assertEqual(found[0].rule, "missing-docstring")
                    self.assertEqual(found[0].line, 1)

    def test_b62_keyword_arguments_not_decisions(self) -> None:
        """B62: echoed keywords and subshell decisions are tokenized, not word-bound."""
        bt = "`"
        cases = (
            ("f() {\n  echo if\n  echo for while until elif\n}\n", []),
            ("f() {\n  x=$(if a; then echo b; fi)\n}\n", ["missing-docstring"]),
            ("f() {\n  x=" + bt + "if a; then echo b; fi" + bt + "\n}\n", ["missing-docstring"]),
            ("f() {\n  ! if a; then b; fi\n}\n", ["missing-docstring"]),
            ("f() {\n  time while a; do b; done\n}\n", ["missing-docstring"]),
        )
        for text, rules in cases:
            with self.subTest(rules=rules):
                found = policy.check_source("scripts/ci/x.sh", text)
                self.assertEqual([item.rule for item in found], rules)

    def test_b63_brace_redirect_closer(self) -> None:
        """B63: a redirect after } still closes the function body on that line."""
        text = "f() {\n  echo x\n} >out\n# doc\ng() {\n  if a; then b; fi\n}\n"
        self.assertEqual(policy.check_source("scripts/ci/x.sh", text, {6}), [])
        self.assertEqual(policy.check_source("scripts/ci/x.sh", text, {2}), [])

    def test_b64_nested_group_same_indent(self) -> None:
        """B64: an inner brace group must not end the outer function early."""
        text = "f() {\n  {\n    echo y\n}\n  if a; then b; fi\n}\n"
        found = policy.check_source("scripts/ci/x.sh", text, {5})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rule, "missing-docstring")
        self.assertEqual(found[0].line, 1)

    def test_b65_compound_function_bodies(self) -> None:
        """B65: compound keyword bodies are accepted and bounded like braced bodies."""
        cases = (
            ("f() if a; then b; fi\n", ["missing-docstring"], 1),
            ("f() for x in a b; do\n  if a; then b; fi\ndone\n", ["missing-docstring"], 1),
            ("f() while a; do b; done\n", ["missing-docstring"], 1),
            (
                "f() case $1 in\n  a) b;;\n  c) d;;\nesac\n",
                ["missing-docstring"],
                1,
            ),
            ("# doc\nf() if a; then b; fi\n", [], 0),
            ("f() { echo; }\n", [], 0),
        )
        for text, rules, line in cases:
            with self.subTest(line=line):
                found = policy.check_source("scripts/ci/x.sh", text)
                self.assertEqual([item.rule for item in found], rules)
                if line:
                    self.assertEqual(found[0].line, line)
        span_text = "f() if a; then\n  b\nfi\n# doc\ng() {\n  if c; then d; fi\n}\n"
        self.assertEqual(policy.check_source("scripts/ci/x.sh", span_text, {6}), [])

    def test_n15_shell_block_nesting_overflow(self) -> None:
        """N15: structural block nesting beyond the bound yields analysis-limit."""
        text = "f() {\n" + "  {\n" * 5 + "  }\n" * 5 + "}\n"
        with patch.object(policy, "MAX_SHELL_NESTING", 4):
            found = policy.check_source("scripts/ci/x.sh", text)
        self.assertEqual([item.rule for item in found], ["analysis-limit"])
        self.assertEqual(found[0].line, 1)


    def test_n16_usage_error_exits_three(self) -> None:
        """N16: malformed argv is a usage error with exit code three."""
        for argv in ([], ["--bogus"], ["--base"], ["--base", "origin/main"]):
            with self.subTest(argv=argv):
                code, out, _ = self.run_main(argv)
                self.assertEqual(code, 3)
                self.assertEqual(out, "")

    def test_b66_case_arm_paren_keeps_body_open(self) -> None:
        """B66: a case arm paren does not pop the case or the function block."""
        text = "f() {\n  case $1 in\n    a) b ;;\n  esac\n  if c; then d; fi\n}\n"
        found = policy.check_source("scripts/ci/x.sh", text, {5})
        self.assertEqual([item.rule for item in found], ["missing-docstring"])
        self.assertEqual(found[0].line, 1)

    def test_b67_brace_on_next_line(self) -> None:
        """B67: a body brace on the line after the header bounds the function."""
        text = "f()\n{\n  if a; then b; fi\n}\n# doc\ng() {\n  :\n}\n"
        found = policy.check_source("scripts/ci/x.sh", text, {3})
        self.assertEqual([item.rule for item in found], ["missing-docstring"])
        self.assertEqual(found[0].line, 1)
        self.assertEqual(policy.check_source("scripts/ci/x.sh", text, {7}), [])

    def test_b68_shell_doc_deletion_under_directive(self) -> None:
        """B68: deleting a doc comment below a kept shellcheck directive still touches the function."""
        cases = (
            (
                "directive-only-kept",
                "# shellcheck disable=SC2034\n# Explain f\nf() {\n  if a; then b; fi\n}\n",
                "# shellcheck disable=SC2034\nf() {\n  if a; then b; fi\n}\n",
                1,
            ),
            (
                "second-doc-kept",
                "# shellcheck disable=SC2034\n# Explain f\n# more\nf() {\n  if a; then b; fi\n}\n",
                "# shellcheck disable=SC2034\n# more\nf() {\n  if a; then b; fi\n}\n",
                0,
            ),
            (
                "unrelated-tail-append",
                "# Explain f\nf() {\n  if a; then b; fi\n}\n",
                "# Explain f\nf() {\n  if a; then b; fi\n}\n# tail\n",
                0,
            ),
        )
        for label, base, topic, expect_len in cases:
            with self.subTest(case=label):
                found = self.branch_fixture("scripts/ci/x.sh", base, topic)
                self.assertEqual(len(found), expect_len)
                if expect_len:
                    self.assertEqual(found[0].rule, "missing-docstring")
                    self.assertEqual(found[0].line, 2)


if __name__ == "__main__":
    unittest.main()
