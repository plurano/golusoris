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
        return policy.check(self.fx.root, base_ref, list(paths))

    def start_topic(self) -> None:
        """Record origin/main at the current commit and open the topic branch."""
        self.fx.write(".gitkeep", "")
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()

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
        text = '"""doc."""\ndef f(a):\n    if a:\n        return 1\n    return 2\n'
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
        base = "def f():\n    if True:\n        return 1\n    return 2\n"
        self.fx.write("scripts/ci/x.py", base)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        topic = "def f():\n    if True:\n        return 2\n    return 2\n"
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
        self.fx.write("scripts/ci/x.py", text)
        self.fx.commit("base")
        self.fx.mark_origin()
        self.fx.branch()
        self.fx.write("scripts/ci/x.py", text + "# touch\n")
        self.fx.commit("topic")
        worktree = (
            '"""added."""\n'
            "def f():\n    if True:\n        return 1\n    return 2\n"
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
        """N2: missing origin/main merge base fails closed with exit code two."""
        self.fx.write("scripts/ci/x.py", "def f():\n    return 1\n")
        self.fx.commit("base")
        self.fx.branch()
        self.fx.write("scripts/ci/x.py", "def f():\n    return 2\n")
        self.fx.commit("topic")
        code, _, err = self.run_main(["--pushed", "scripts/ci/x.py"])
        self.assertEqual(code, 2)
        self.assertIn("origin/main", err)

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


if __name__ == "__main__":
    unittest.main()