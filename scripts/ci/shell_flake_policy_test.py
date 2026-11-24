# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Unit tests for shell flake policy covering early-exit readers and bare downloads."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess  # Fixture git commands and SubprocessError in mocked git failures. # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import shell_flake_policy

PLANTED_EXCLUSION = json.dumps(
    {
        "packageRules": [
            {
                "description": "praetor-managed files",
                "matchFileNames": ["scripts/ci/planted.sh"],
            }
        ]
    }
)


class ShellFlakePolicyTest(unittest.TestCase):
    """Exercise positive, negative, and boundary policy cases on file fixtures."""

    def setUp(self) -> None:
        """Create an isolated temporary directory for test fixtures."""
        self._temp_dir = tempfile.TemporaryDirectory(prefix="golusoris-flake-test-")
        self.temp_root = Path(self._temp_dir.name)
        self.addCleanup(self._temp_dir.cleanup)

    def write_fixture(self, filename: str, content: str) -> Path:
        """Write content into a temporary fixture file."""
        file_path = self.temp_root / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return file_path

    def assert_has_rule(
        self, findings: list[shell_flake_policy.Finding], rule: str
    ) -> None:
        """Assert that at least one finding has the expected rule name."""
        rules = [f.rule for f in findings]
        self.assertIn(rule, rules, f"expected rule {rule!r} in {rules!r}")

    def test_positive_reader_fix_forms(self) -> None:
        """Verify that all approved Part A fix forms pass with zero reader findings."""
        positive_script = (
            'out="$(producer)"; grep -q PAT <<<"$out"\n'
            'if out="$(producer)" && grep -q PAT <<<"$out"; then echo ok; fi\n'
            'if ! { out="$(producer)" && grep -q PAT <<<"$out"; }; then echo ok; fi\n'
            '{ out="$(producer)" && grep -Fq PAT <<<"$out"; } || { echo fail; }\n'
            'grep -q PAT <<<"$var"\n'
            "producer | sed -n '1,4p'\n"
            "producer | sed -n '1p'\n"
            "grep -m 40 -E PAT file.txt\n"
        )
        path = self.write_fixture("scripts/ci/positive.sh", positive_script)
        findings = shell_flake_policy.scan_file(str(path), check_download=False)
        self.assertEqual(findings, [])

    def test_positive_retry_wrapped_downloads(self) -> None:
        """Verify that retry-wrapped go module download forms pass cleanly."""
        positive_downloads = (
            'bash scripts/ci/retry.sh go install mvdan.cc/gofumpt@v1.0.0\n'
            'GOFLAGS=-mod=mod bash "$root/scripts/ci/retry.sh" go get foo bar\n'
            'bash scripts/ci/retry.sh go mod download foo@v1.0.0\n'
            'bash scripts/ci/retry.sh $(GO) install mvdan.cc/gofumpt@v1.0.0\n'
            'GOBIN="$tmp" bash "$root/scripts/ci/retry.sh" go install foo\n'
            'bash "$repo_root/scripts/ci/retry.sh" go mod download "$module@$ver"\n'
            'download="$(GOFLAGS=-mod=mod bash "$repo_root/scripts/ci/retry.sh" go mod download -json "$module@$ver")"\n'
        )
        path = self.write_fixture("scripts/ci/positive_dl.sh", positive_downloads)
        findings = shell_flake_policy.scan_file(str(path), check_download=True)
        self.assertEqual(findings, [])

    def test_negative_reader_flag_variants(self) -> None:
        """Verify negative early-exit reader flag variants are flagged."""
        flag_cases = [
            "-q",
            "-Fq",
            "-qE",
            "-Eq",
            "--quiet",
            "--silent",
            "-m 1",
            "--max-count=1",
            "-l",
        ]
        for flag in flag_cases:
            content = f"echo hello | grep {flag} hello\n"
            path = self.write_fixture(f"scripts/ci/reader_{flag.replace(' ', '_')}.sh", content)
            findings = shell_flake_policy.scan_file(str(path), check_download=False)
            self.assert_has_rule(findings, "early-exit-reader")

    def test_negative_reader_dev_null_and_head(self) -> None:
        """Verify dev-null redirection, head forms, and pipeline variants are flagged."""
        reader_cases = [
            "echo hello | grep x >/dev/null\n",
            "echo hello | head -n 5\n",
            "echo hello | head -1\n",
            "echo hello |& grep -q hello\n",
        ]
        for idx, content in enumerate(reader_cases):
            path = self.write_fixture(f"scripts/ci/reader_variant_{idx}.sh", content)
            findings = shell_flake_policy.scan_file(str(path), check_download=False)
            self.assert_has_rule(findings, "early-exit-reader")

    def test_negative_reader_continuation_splits(self) -> None:
        """Verify continuation lines across backslash or pipe are joined and flagged."""
        split_cases = [
            "echo hello | \\\n  grep -q hello\n",
            "echo hello |\n  grep -q hello\n",
        ]
        for idx, content in enumerate(split_cases):
            path = self.write_fixture(f"scripts/ci/continuation_{idx}.sh", content)
            findings = shell_flake_policy.scan_file(str(path), check_download=False)
            self.assert_has_rule(findings, "early-exit-reader")

    def test_negative_bare_downloads(self) -> None:
        """Verify bare go module download forms are flagged."""
        bare_cases = [
            ("bare_install", "go install mvdan.cc/gofumpt@v1.0.0\n"),
            ("gobin_install", "GOBIN=/tmp/bin go install mvdan.cc/gofumpt@v1.0.0\n"),
            ("goflags_get", "GOFLAGS=-mod=mod go get foo@v1.0.0\n"),
            ("mod_download_json", 'go mod download -json "$module@$ver"\n'),
            ("make_go_install", "$(GO) install mvdan.cc/gofumpt@v1.0.0\n"),
            ("subshell_download", 'res="$(go install mvdan.cc/gofumpt@v1.0.0)"\n'),
            ("retry_not_as_command", "echo retry.sh && go install mvdan.cc/gofumpt@v1.0.0\n"),
            (
                "warmed_cache_json_bare",
                'bash scripts/ci/retry.sh go mod download y@v\ngo mod download -json x@v\n',
            ),
        ]
        for name, content in bare_cases:
            path = self.write_fixture(f"scripts/ci/{name}.sh", content)
            findings = shell_flake_policy.scan_file(str(path), check_download=True)
            self.assert_has_rule(findings, "bare-module-download")

    def test_boundary_cases(self) -> None:
        """Verify non-pipe, comment, non-download, and excluded boundaries pass cleanly."""
        boundary_script = (
            "false || grep -q hello file.txt\n"
            "# echo hello | grep -q hello\n"
            "# go install mvdan.cc/gofumpt@v1.0.0\n"
            "echo hello | grep -c hello\n"
            "echo hello | grep -o hello\n"
            "grep -q hello /tmp/file.txt\n"
            "go mod verify\n"
            "go mod tidy\n"
            "go build ./...\n"
            "go test ./...\n"
        )
        path = self.write_fixture("scripts/ci/boundary.sh", boundary_script)
        findings = shell_flake_policy.scan_file(str(path), check_download=True)
        self.assertEqual(findings, [])

    def test_boundary_test_scripts_excluded_from_downloads(self) -> None:
        """Verify that _test.sh files are excluded from download scanning."""
        test_script = "go install mvdan.cc/gofumpt@v1.0.0\n"
        path = self.write_fixture("scripts/ci/sample_test.sh", test_script)
        in_dl_scope = shell_flake_policy.is_in_download_scope(str(path), set())
        self.assertFalse(in_dl_scope)

    def test_boundary_praetor_files_skipped(self) -> None:
        """Verify praetor-managed files are skipped from repository scanning."""
        praetor_files = {".github/workflows/praetor-docs.yml"}
        in_scope = shell_flake_policy.is_in_repo_scope(
            ".github/workflows/praetor-docs.yml", praetor_files
        )
        self.assertFalse(in_scope)

    def test_negative_escaped_quote_and_pipe_variants(self) -> None:
        """Verify quote and escape variants correctly detect or ignore pipes."""
        cases = [
            ("escaped_single_quote", r"echo \' | grep -q x" + "\n", True),
            ("escaped_backslash_double_quote", 'echo "a\\\\" | grep -q x\n', True),
            ("literal_backslash_single_quote", r"echo 'a\' | grep -q x" + "\n", True),
            ("quoted_pipe", 'echo "a | grep -q x"\n', False),
            ("cmd_sub_in_double_quotes", 'x="$(printf a | grep -q a)"\n', True),
            ("single_quoted_pipe_in_cmd_sub", "x=\"$(echo 'a|b')\"\n", False),
            ("ansi_quote_escaped_quote", "echo $'\\'' | grep -q x\n", True),
            ("double_quoted_pipe_in_cmd_sub", 'echo "$(printf \'%s\' "a | b")"\n', False),
            ("escaped_space_before_hash", "echo a\\ #x | grep -q y\n", True),
            ("unescaped_space_before_hash", "echo hi # x | grep -q y\n", False),
        ]
        for name, content, expect_finding in cases:
            path = self.write_fixture(f"scripts/ci/{name}.sh", content)
            findings = shell_flake_policy.scan_file(str(path), check_download=False)
            if expect_finding:
                self.assert_has_rule(findings, "early-exit-reader")
            else:
                self.assertEqual(findings, [])

    @patch.object(shell_flake_policy, "MAX_SCAN_FILES", 2)
    @patch("subprocess.check_output")
    def test_scan_limit_tracked_files_exceeded(self, mock_ls: MagicMock) -> None:
        """Verify exceeding MAX_SCAN_FILES triggers scan-limit finding."""
        mock_ls.return_value = b"file1\0file2\0file3\0"
        findings = shell_flake_policy.scan_repository(self.temp_root, set())
        self.assert_has_rule(findings, "scan-limit")

    @patch.object(shell_flake_policy, "MAX_FILE_LINES", 2)
    def test_scan_limit_file_lines_exceeded(self) -> None:
        """Verify exceeding MAX_FILE_LINES triggers scan-limit finding."""
        content = "echo 1\necho 2\necho 3\n"
        path = self.write_fixture("scripts/ci/long.sh", content)
        findings = shell_flake_policy.scan_file(str(path), check_download=False)
        self.assert_has_rule(findings, "scan-limit")

    @patch("subprocess.check_output")
    def test_git_ls_files_failure_emits_finding(self, mock_ls: MagicMock) -> None:
        """Verify git ls-files failure emits git-error finding."""
        mock_ls.side_effect = subprocess.SubprocessError("mock git error")
        findings = shell_flake_policy.scan_repository(self.temp_root, set())
        self.assert_has_rule(findings, "git-error")

    @patch("sys.stdout")
    @patch("subprocess.check_output")
    @patch("sys.argv", ["shell_flake_policy.py"])
    def test_main_exits_nonzero_on_ls_files_failure(
        self, mock_ls: MagicMock, mock_stdout: MagicMock
    ) -> None:
        """Verify main() exits with code 3 when git ls-files fails."""
        mock_ls.side_effect = subprocess.SubprocessError("mock git error")
        rc = shell_flake_policy.main()
        self.assertEqual(rc, 3)

    @patch.object(Path, "read_text", side_effect=OSError("mock read error"))
    def test_read_error_emits_finding(self, mock_read: MagicMock) -> None:
        """Verify OSError during read_text emits a read-error finding."""
        path = self.write_fixture("scripts/ci/unreadable.sh", "echo ok\n")
        findings = shell_flake_policy.scan_file(str(path), check_download=False)
        self.assert_has_rule(findings, "read-error")

    @patch("sys.stdout")
    @patch.object(Path, "read_text", side_effect=OSError("mock read error"))
    def test_main_exits_nonzero_on_read_error(
        self, mock_read: MagicMock, mock_stdout: MagicMock
    ) -> None:
        """Verify main() exits with code 3 when a scanned file raises OSError."""
        path = self.write_fixture("scripts/ci/unreadable.sh", "echo ok\n")
        with patch("sys.argv", ["shell_flake_policy.py", str(path)]):
            rc = shell_flake_policy.main()
        self.assertEqual(rc, 3)

    def test_boundary_yaml_list_item_block_scalar(self) -> None:
        """Verify list-item YAML block scalar headers are not joined with script lines."""
        content = "jobs:\n  a:\n    steps:\n      - run: |\n          grep -q PAT file.txt\n"
        path = self.write_fixture(".github/workflows/list_item.yml", content)
        findings = shell_flake_policy.scan_file(str(path), check_download=False)
        self.assertEqual(findings, [])

    def test_boundary_grep_stderr_dev_null(self) -> None:
        """Verify 2>/dev/null stderr redirection is not flagged as early exit."""
        content = "echo hello | grep x 2>/dev/null\n"
        path = self.write_fixture("scripts/ci/grep_stderr.sh", content)
        findings = shell_flake_policy.scan_file(str(path), check_download=False)
        self.assertEqual(findings, [])

    def test_negative_grep_stdout_dev_null_forms(self) -> None:
        """Verify stdout dev-null redirection forms are flagged as early exit."""
        cases = [
            ("stdout_1", "echo hello | grep x 1>/dev/null\n"),
            ("stdout_amp", "echo hello | grep x &>/dev/null\n"),
            ("stdout_append", "echo hello | grep x >>/dev/null\n"),
        ]
        for name, content in cases:
            path = self.write_fixture(f"scripts/ci/{name}.sh", content)
            findings = shell_flake_policy.scan_file(str(path), check_download=False)
            self.assert_has_rule(findings, "early-exit-reader")

    @patch("subprocess.check_output")
    def test_scan_repository_resolves_paths_against_root(
        self, mock_ls: MagicMock
    ) -> None:
        """Verify scan_repository resolves relative paths against repo_root."""
        self.write_fixture("scripts/ci/root_probe.sh", "echo hello | head -1\n")
        mock_ls.return_value = b"scripts/ci/root_probe.sh\0"
        findings = shell_flake_policy.scan_repository(self.temp_root, set())
        self.assert_has_rule(findings, "early-exit-reader")
        self.assertEqual(findings[0].path, "scripts/ci/root_probe.sh")


MANAGED_EXCLUSION = json.dumps(
    {
        "packageRules": [
            {
                "description": "praetor-managed files",
                "matchFileNames": ["scripts/ci/managed.sh"],
            }
        ]
    }
)


class ShellFlakePolicyPushedTest(unittest.TestCase):
    """Exercise pre-push stdin ref scanning, blob reads, and hook wiring."""

    def setUp(self) -> None:
        """Create a temporary git repository with isolated Git configuration."""
        self._temp_dir = tempfile.TemporaryDirectory(prefix="golusoris-flake-pushed-")
        self.repo_root = Path(self._temp_dir.name)
        self.addCleanup(self._temp_dir.cleanup)
        env = patch.dict(os.environ)
        self.addCleanup(env.stop)
        env.start()
        for key in list(os.environ):
            if key.startswith("GIT_"):
                del os.environ[key]
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self._git("init", "-q")

    def _git(self, *args: str) -> None:
        """Run one fixture git command without inherited Git variables."""
        subprocess.run(  # noqa: S603 - fixed git argv in fixture, no shell. # nosec B603
            ["git", *args],  # noqa: S607 - git executable on runner PATH. # nosec B607
            cwd=self.repo_root,
            env=os.environ.copy(),
            check=True,
            timeout=60,
        )

    def _out(self, *args: str) -> str:
        """Run one fixture git command and return stripped stdout."""
        proc = subprocess.run(  # noqa: S603 - fixed git argv in fixture, no shell. # nosec B603
            ["git", *args],  # noqa: S607 - git executable on runner PATH. # nosec B607
            cwd=self.repo_root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return proc.stdout.strip()

    def _commit(self, message: str) -> None:
        """Create one unsigned fixture commit with a fixed identity."""
        identity = ("-c", "user.name=policy", "-c", "user.email=policy@example.invalid")
        self._git(*identity, "-c", "commit.gpgsign=false", "commit", "-qm", message)

    def _commit_paths(self, mapping: dict[str, str]) -> None:
        """Write relative paths and create one commit."""
        for rel, content in mapping.items():
            target = self.repo_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self._git("add", rel)
        self._commit("fixture")

    def _commit_symlink(self, rel: str, target: str) -> None:
        """Stage and commit one git symlink entry without creating a filesystem link."""
        proc = subprocess.run(  # noqa: S603 - fixed git argv in fixture, no shell. # nosec B603
            ["git", "-C", str(self.repo_root), "hash-object", "-w", "--stdin"],  # noqa: S607 - git executable on runner PATH. # nosec B607
            cwd=self.repo_root,
            env=os.environ.copy(),
            input=target.encode(),
            capture_output=True,
            check=True,
            timeout=60,
        )
        blob = proc.stdout.decode("ascii").strip()
        self._git("update-index", "--add", "--cacheinfo", f"120000,{blob},{rel}")
        self._commit("symlink")

    def _base(self, mapping: dict[str, str]) -> None:
        """Seed origin/main at HEAD after one commit (empty mapping allowed)."""
        if mapping:
            self._commit_paths(mapping)
        else:
            identity = ("-c", "user.name=policy", "-c", "user.email=policy@example.invalid")
            self._git(
                *identity,
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-qm",
                "empty",
            )
        self._git("update-ref", "refs/remotes/origin/main", "HEAD")

    def _push(self, oid: str | None = None) -> list[shell_flake_policy.Finding]:
        """Run scan_pre_push with one synthetic pre-push stdin line."""
        if oid is None:
            oid = self._out("rev-parse", "HEAD")
        zeros = "0" * 40
        line = f"refs/heads/topic {oid} refs/heads/topic {zeros}\n"
        return shell_flake_policy.scan_pre_push(self.repo_root, io.StringIO(line))

    def test_p1_planted_early_exit_reader(self) -> None:
        """P1: in-scope shell script with early-exit reader is flagged on push."""
        self._base({"README.md": "x\n"})
        self._commit_paths({"scripts/ci/planted.sh": "echo x | head -1\n"})
        self.assertTrue(
            any(f.rule == "early-exit-reader" for f in self._push())
        )

    def test_p2_planted_bare_module_download(self) -> None:
        """P2: Makefile with bare go install is flagged on push."""
        self._base({"README.md": "x\n"})
        self._commit_paths({"Makefile": "install:\n\tgo install example.com/x@v1\n"})
        self.assertTrue(any(f.rule == "bare-module-download" for f in self._push()))

    def test_c1_clean_pattern_passes(self) -> None:
        """C1: approved reader pattern yields no findings on push."""
        self._base({"README.md": "x\n"})
        body = 'out="$(producer)"; grep -q PAT <<<"$out"\n'
        self._commit_paths({"scripts/ci/clean.sh": body})
        self.assertEqual(self._push(), [])

    def test_b1_out_of_scope_filtered(self) -> None:
        """B1: out-of-scope paths are not scanned on push."""
        self._base({"README.md": "x\n"})
        self._commit_paths({"internal/x.txt": "echo x | head -1\n"})
        self.assertEqual(self._push(), [])

    def test_n1_worktree_deletion_cannot_hide_violation(self) -> None:
        """N1: local worktree deletion does not hide a committed violation."""
        self._base({"README.md": "x\n"})
        self._commit_paths({"scripts/ci/gone.sh": "echo x | head -1\n"})
        (self.repo_root / "scripts/ci/gone.sh").unlink()
        self.assertTrue(any(f.rule == "early-exit-reader" for f in self._push()))

    def test_n2_pushed_ref_differs_from_head(self) -> None:
        """N2: scan uses the pushed oid, not HEAD when they differ."""
        self._base({"README.md": "x\n"})
        self._git("checkout", "-q", "-b", "other")
        self._commit_paths({"scripts/ci/other.sh": "echo x | head -1\n"})
        oid = self._out("rev-parse", "HEAD")
        self._git("checkout", "-q", "-")
        self.assertTrue(
            any(f.rule == "early-exit-reader" for f in self._push(oid))
        )

    def test_n3_rename_into_scope(self) -> None:
        """N3: rename into scripts/ci/ is scanned at the new path."""
        self._base({"docs/old.txt": "echo x | head -1\n"})
        self._git("config", "diff.renames", "true")
        (self.repo_root / "scripts/ci").mkdir(parents=True, exist_ok=True)
        self._git("mv", "docs/old.txt", "scripts/ci/new.sh")
        self._commit("mv")
        findings = self._push()
        self.assertTrue(
            any(
                f.rule == "early-exit-reader" and f.path == "scripts/ci/new.sh"
                for f in findings
            )
        )

    def test_b2_committed_deletion_ignores_untracked_recreate(self) -> None:
        """B2: committed deletion is not rescanned from an untracked recreate."""
        self._base({"scripts/ci/gone.sh": "echo ok\n"})
        self._git("rm", "-q", "scripts/ci/gone.sh")
        self._commit("remove")
        (self.repo_root / "scripts/ci").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "scripts/ci/gone.sh").write_text(
            "echo x | head -1\n", encoding="utf-8"
        )
        self.assertEqual(self._push(), [])

    def test_b3_uncommitted_dirt_ignored(self) -> None:
        """B3: uncommitted worktree edits do not affect blob scan."""
        self._base({"README.md": "x\n"})
        self._commit_paths({"scripts/ci/x.sh": "echo ok\n"})
        (self.repo_root / "scripts/ci/x.sh").write_text(
            "echo x | head -1\n", encoding="utf-8"
        )
        self.assertEqual(self._push(), [])

    def test_b4_ref_deletion_skipped(self) -> None:
        """B4: all-zero local oid (ref deletion) is skipped."""
        zeros = "0" * 40
        line = f"refs/heads/topic {zeros} refs/heads/topic {zeros}\n"
        self._base({"README.md": "x\n"})
        self.assertEqual(
            shell_flake_policy.scan_pre_push(self.repo_root, io.StringIO(line)),
            [],
        )

    def test_b5_two_refs_one_dirty_each(self) -> None:
        """B5: two pushed refs yield one finding per violating path."""
        self._base({"README.md": "x\n"})
        self._git("checkout", "-q", "-b", "a")
        self._commit_paths({"scripts/ci/a.sh": "echo x | head -1\n"})
        oid_a = self._out("rev-parse", "HEAD")
        self._git("checkout", "-q", "origin/main")
        self._git("checkout", "-q", "-b", "b")
        self._commit_paths({"scripts/ci/b.sh": "echo x | head -1\n"})
        oid_b = self._out("rev-parse", "HEAD")
        zeros = "0" * 40
        stream = io.StringIO(
            f"refs/heads/a {oid_a} refs/heads/a {zeros}\n"
            f"refs/heads/b {oid_b} refs/heads/b {zeros}\n"
        )
        findings = shell_flake_policy.scan_pre_push(self.repo_root, stream)
        paths = {f.path for f in findings if f.rule == "early-exit-reader"}
        self.assertEqual(paths, {"scripts/ci/a.sh", "scripts/ci/b.sh"})

    def test_n4_malformed_pre_push_line(self) -> None:
        """N4: malformed pre-push stdin returns pre-push-input."""
        self._base({"README.md": "x\n"})
        findings = shell_flake_policy.scan_pre_push(
            self.repo_root, io.StringIO("a b c\n")
        )
        self.assertTrue(
            any(
                f.rule == "pre-push-input" and "expected 4 fields" in f.message
                for f in findings
            )
        )

    def test_n5_invalid_local_oid(self) -> None:
        """N5: invalid local oid returns pre-push-input."""
        self._base({"README.md": "x\n"})
        zeros = "0" * 40
        line = f"refs/heads/x nothex refs/heads/x {zeros}\n"
        findings = shell_flake_policy.scan_pre_push(self.repo_root, io.StringIO(line))
        self.assertTrue(
            any(
                f.rule == "pre-push-input" and "invalid local oid" in f.message
                for f in findings
            )
        )

    @patch.object(shell_flake_policy, "MAX_PUSH_REFS", 2)
    def test_n6_ref_count_bound(self) -> None:
        """N6: more than MAX_PUSH_REFS lines returns pre-push-input."""
        self._base({"README.md": "x\n"})
        oid = self._out("rev-parse", "HEAD")
        zeros = "0" * 40
        lines = "".join(
            f"refs/heads/r{i} {oid} refs/heads/r{i} {zeros}\n" for i in range(3)
        )
        findings = shell_flake_policy.scan_pre_push(
            self.repo_root, io.StringIO(lines)
        )
        self.assertTrue(
            any(
                f.rule == "pre-push-input" and "ref count exceeds 2" in f.message
                for f in findings
            )
        )

    def test_n7_unknown_commit_oid(self) -> None:
        """N7: unknown pushed oid returns git-error."""
        self._base({"README.md": "x\n"})
        bogus = "1" * 40
        findings = self._push(bogus)
        self.assertTrue(any(f.rule == "git-error" for f in findings))

    def test_b6_missing_origin_main_skips(self) -> None:
        """B6: missing origin/main prints skip and returns no findings."""
        self._commit_paths({"scripts/ci/planted.sh": "echo x | head -1\n"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            findings = self._push()
        self.assertEqual(findings, [])
        self.assertIn("origin/main not found", err.getvalue())

    def test_b7_committed_exclusion_at_pushed_ref(self) -> None:
        """B7: committed renovate.json exclusion is honoured at pushed oid."""
        self._base({"README.md": "x\n"})
        self._commit_paths(
            {
                "renovate.json": MANAGED_EXCLUSION + "\n",
                "scripts/ci/managed.sh": "echo x | head -1\n",
            }
        )
        self.assertEqual(self._push(), [])

    def test_n8_uncommitted_exclusion_ignored(self) -> None:
        """N8: uncommitted renovate.json exclusion does not affect push scan."""
        self._base({"README.md": "x\n"})
        self._commit_paths({"scripts/ci/managed.sh": "echo x | head -1\n"})
        (self.repo_root / "renovate.json").write_text(
            MANAGED_EXCLUSION, encoding="utf-8"
        )
        self.assertTrue(any(f.rule == "early-exit-reader" for f in self._push()))

    def test_b8_non_ascii_path(self) -> None:
        """B8: non-ASCII path names are reported with the committed path."""
        self._base({"README.md": "x\n"})
        path = "scripts/ci/caf\u00e9.sh"
        self._commit_paths({path: "echo x | head -1\n"})
        findings = self._push()
        self.assertTrue(
            any(f.rule == "early-exit-reader" and f.path == path for f in findings)
        )

    @unittest.skipIf(sys.platform == "win32", "Windows file names cannot contain a carriage return")
    def test_b9_carriage_return_path(self) -> None:
        """B9: paths containing a carriage return are scanned and reported."""
        self._base({"README.md": "x\n"})
        cr_path = "scripts/ci/a\rb.sh"
        self._commit_paths({cr_path: "echo x | head -1\n"})
        self.assertTrue(
            any(f.rule == "early-exit-reader" and f.path == cr_path for f in self._push())
        )

    @unittest.skipIf(sys.platform == "win32", "Windows file names cannot contain a backslash")
    def test_b10_posix_backslash_name(self) -> None:
        """B10: POSIX backslash in a file name is kept in findings."""
        self._base({"README.md": "x\n"})
        bs_path = r"scripts/ci/a\b.sh"
        self._commit_paths({bs_path: "echo x | head -1\n"})
        self.assertTrue(
            any(
                f.rule == "early-exit-reader" and f.path == bs_path for f in self._push()
            )
        )

    def test_n9_download_scope_backslash_fold(self) -> None:
        """N9: policy_path fold affects download exclusion on POSIX vs Windows mode."""
        praetor = {"scripts/ci/a/b.sh"}
        with patch.object(shell_flake_policy, "WINDOWS_PATHS", False):
            self.assertTrue(
                shell_flake_policy.is_in_download_scope(r"scripts/ci/a\b.sh", praetor)
            )
        with patch.object(shell_flake_policy, "WINDOWS_PATHS", True):
            self.assertFalse(
                shell_flake_policy.is_in_download_scope(r"scripts/ci/a\b.sh", praetor)
            )

    @patch.object(shell_flake_policy, "MAX_SCAN_FILES", 1)
    def test_b11_scan_bound(self) -> None:
        """B11: in-scope file count over MAX_SCAN_FILES returns scan-limit."""
        self._base({"README.md": "x\n"})
        self._commit_paths(
            {
                "scripts/ci/a.sh": "echo ok\n",
                "scripts/ci/b.sh": "echo ok\n",
            }
        )
        findings = self._push()
        self.assertTrue(
            any(
                f.rule == "scan-limit"
                and "pushed in-scope file count exceeds scan limit 1" in f.message
                for f in findings
            )
        )

    @patch.object(shell_flake_policy, "MAX_LISTED_PATHS", 1)
    def test_n10_listing_bound(self) -> None:
        """N10: raw diff listing bound returns scan-limit before scan."""
        self._base({"README.md": "x\n"})
        self._commit_paths(
            {
                "scripts/ci/a.sh": "echo ok\n",
                "scripts/ci/b.sh": "echo ok\n",
            }
        )
        findings = self._push()
        self.assertTrue(
            any(
                f.rule == "scan-limit" and "listing exceeds scan limit" in f.message
                for f in findings
            )
        )

    def test_w1_lefthook_prepush_wiring(self) -> None:
        """W1: lefthook pre-push runs shell_flake_policy.py --pre-push on stdin."""
        lefthook = Path(__file__).resolve().parents[2] / "lefthook.yml"
        text = lefthook.read_text(encoding="utf-8")
        lines = text.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("pre-push:"))
        block: list[str] = []
        for line in lines[start + 1 :]:
            if line and not line[0].isspace():
                break
            block.append(line)
        section = "\n".join(block)
        self.assertIn("    shell-flake-policy:", section)
        expected = (
            "      run: sh .config/lefthook/python.sh -B scripts/ci/shell_flake_policy.py "
            "--pre-push"
        )
        self.assertIn(expected, section)
        job_lines = block[block.index("    shell-flake-policy:") + 1 :]
        job_end = next(
            (i for i, line in enumerate(job_lines) if line[4:5].strip() and not line[:4].strip()),
            len(job_lines),
        )
        self.assertIn("      use_stdin: true", job_lines[:job_end])

    @patch.object(shell_flake_policy, "scan_repository")
    def test_m1_main_dispatches_pre_push_mode(self, mock_scan: MagicMock) -> None:
        """M1: main() routes --pre-push through scan_pre_push and maps exit codes."""
        finding = shell_flake_policy.Finding("a.sh", 1, "early-exit-reader", "x")
        repo = Path(__file__).resolve().parents[2]
        argv = ["shell_flake_policy.py", "--pre-push"]
        out = io.StringIO()
        with (
            patch.object(shell_flake_policy, "scan_pre_push") as mock_pre,
            patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(out),
        ):
            mock_pre.return_value = [finding]
            self.assertEqual(shell_flake_policy.main(), 1)
            mock_pre.assert_called_with(repo, sys.stdin)
            mock_pre.return_value = []
            self.assertEqual(shell_flake_policy.main(), 0)
        mock_scan.assert_not_called()
        self.assertEqual(out.getvalue(), "a.sh:1: early-exit-reader: x\n")

    def test_m2_usage_errors(self) -> None:
        """M2: removed --pushed and extra --pre-push args return exit code 3."""
        cases = [
            (["shell_flake_policy.py", "--pushed", "a.sh"], "unknown option"),
            (["shell_flake_policy.py", "--pre-push", "x"], "takes no arguments"),
        ]
        for argv, needle in cases:
            err = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stderr(err):
                self.assertEqual(shell_flake_policy.main(), 3)
            self.assertIn(needle, err.getvalue())

    def test_p3_in_tree_symlink_follows_target(self) -> None:
        """P3: in-scope symlink to an in-tree violation is flagged under the link path."""
        self._base({"README.md": "x\n"})
        self._commit_paths({"docs/payload.txt": "echo x | head -1\n"})
        self._commit_symlink("scripts/ci/link.sh", "../../docs/payload.txt")
        findings = self._push()
        self.assertTrue(
            any(
                f.rule == "early-exit-reader" and f.path == "scripts/ci/link.sh"
                for f in findings
            )
        )

    def test_b12_in_tree_symlink_clean_target(self) -> None:
        """B12: in-scope symlink to a clean in-tree target yields no findings."""
        self._base({"README.md": "x\n"})
        self._commit_paths({"docs/clean.txt": "echo ok\n"})
        self._commit_symlink("scripts/ci/ok.sh", "../../docs/clean.txt")
        self.assertEqual(self._push(), [])

    def test_n11_out_of_tree_symlink_rejected(self) -> None:
        """N11: symlink pointing outside the repository tree emits read-error."""
        self._base({"README.md": "x\n"})
        self._commit_symlink("scripts/ci/out.sh", "/etc/passwd")
        findings = self._push()
        self.assertTrue(
            any(f.rule == "read-error" and f.path == "scripts/ci/out.sh" for f in findings)
        )

    def test_n12_dangling_symlink_rejected(self) -> None:
        """N12: dangling in-tree symlink emits read-error for the link path."""
        self._base({"README.md": "x\n"})
        self._commit_symlink("scripts/ci/dang.sh", "missing.txt")
        findings = self._push()
        self.assertTrue(
            any(f.rule == "read-error" and f.path == "scripts/ci/dang.sh" for f in findings)
        )

    def test_n13_origin_main_present_reraises_git_error(self) -> None:
        """N13: non-ref git failure in origin_main_present emits git-error without skip notice."""
        self._base({"README.md": "x\n"})
        err = io.StringIO()
        failure = subprocess.CalledProcessError(128, ["git"])
        with (
            patch.object(shell_flake_policy, "git_output", side_effect=failure),
            contextlib.redirect_stderr(err),
        ):
            findings = self._push()
        self.assertEqual([f.rule for f in findings], ["git-error"])
        self.assertNotIn("origin/main not found", err.getvalue())

    def test_n14_main_exit_3_on_malformed_stdin(self) -> None:
        """N14: main() exits with code 3 and prints pre-push-input on malformed stdin."""
        out = io.StringIO()
        with (
            patch.object(sys, "argv", ["shell_flake_policy.py", "--pre-push"]),
            patch.object(sys, "stdin", io.StringIO("a b c\n")),
            patch.object(sys, "stdout", out),
            patch.object(shell_flake_policy, "origin_main_present", return_value=True),
        ):
            rc = shell_flake_policy.main()
        self.assertEqual(rc, 3)
        self.assertIn("pre-push-input", out.getvalue())

    def test_p4_main_exit_1_violation_and_0_clean_push(self) -> None:
        """P4: main() returns 1 on a pushed policy violation and 0 on a clean push."""
        planted = [
            shell_flake_policy.Finding("scripts/ci/p4.sh", 2, "early-exit-reader", "head -1")
        ]
        out = io.StringIO()
        with (
            patch.object(sys, "argv", ["shell_flake_policy.py", "--pre-push"]),
            patch.object(sys, "stdin", io.StringIO("")),
            patch.object(sys, "stdout", out),
            patch.object(shell_flake_policy, "scan_pre_push", side_effect=[planted, []]),
        ):
            first_rc = shell_flake_policy.main()
            second_rc = shell_flake_policy.main()
        self.assertEqual((first_rc, second_rc), (1, 0))
        self.assertIn("scripts/ci/p4.sh:2: early-exit-reader:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
