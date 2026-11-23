# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Unit tests for shell flake policy covering early-exit readers and bare downloads."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess  # Fixture git commands and SubprocessError in mocked git failures. # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import shell_flake_policy


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
        mock_ls.return_value = "file1\nfile2\nfile3\n"
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
        """Verify git ls-files failure emits scan-limit finding and non-zero exit."""
        mock_ls.side_effect = subprocess.SubprocessError("mock git error")
        findings = shell_flake_policy.scan_repository(self.temp_root, set())
        self.assert_has_rule(findings, "scan-limit")

    @patch("sys.stdout")
    @patch("subprocess.check_output")
    @patch("sys.argv", ["shell_flake_policy.py"])
    def test_main_exits_nonzero_on_ls_files_failure(
        self, mock_ls: MagicMock, mock_stdout: MagicMock
    ) -> None:
        """Verify main() exits nonzero when git ls-files fails."""
        mock_ls.side_effect = subprocess.SubprocessError("mock git error")
        rc = shell_flake_policy.main()
        self.assertEqual(rc, 1)

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
        """Verify main() exits nonzero when a scanned file raises OSError."""
        path = self.write_fixture("scripts/ci/unreadable.sh", "echo ok\n")
        with patch("sys.argv", ["shell_flake_policy.py", str(path)]):
            rc = shell_flake_policy.main()
        self.assertEqual(rc, 1)

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
        mock_ls.return_value = "scripts/ci/root_probe.sh\n"
        findings = shell_flake_policy.scan_repository(self.temp_root, set())
        self.assert_has_rule(findings, "early-exit-reader")
        self.assertEqual(findings[0].path, "scripts/ci/root_probe.sh")


class ShellFlakePolicyPushedTest(unittest.TestCase):
    """Exercise pre-push --pushed path selection, divergence, and hook wiring."""

    def setUp(self) -> None:
        """Create a temporary git repository with isolated Git configuration."""
        self._temp_dir = tempfile.TemporaryDirectory(prefix="golusoris-flake-pushed-")
        self.repo_root = Path(self._temp_dir.name)
        self.addCleanup(self._temp_dir.cleanup)
        self._git("init", "-q")

    def _git(self, *args: str) -> None:
        """Run one fixture git command without inherited Git variables."""
        env = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        subprocess.run(  # noqa: S603 - fixed git argv in fixture, no shell. # nosec B603
            ["git", *args],  # noqa: S607 - git executable on runner PATH. # nosec B607
            cwd=self.repo_root,
            env=env,
            check=True,
            timeout=60,
        )

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

    def test_p1_planted_early_exit_reader(self) -> None:
        """P1: in-scope pushed shell script with early-exit reader is flagged."""
        self._commit_paths({"scripts/ci/planted.sh": "echo x | head -1\n"})
        findings = shell_flake_policy.scan_pushed(
            self.repo_root, ["scripts/ci/planted.sh"]
        )
        self.assertTrue(any(f.rule == "early-exit-reader" for f in findings))

    def test_p2_planted_bare_module_download(self) -> None:
        """P2: pushed Makefile with bare go install is flagged."""
        self._commit_paths({"Makefile": "install:\n\tgo install example.com/x@v1\n"})
        findings = shell_flake_policy.scan_pushed(self.repo_root, ["Makefile"])
        self.assertTrue(any(f.rule == "bare-module-download" for f in findings))

    def test_c1_clean_pattern_passes(self) -> None:
        """C1: approved reader pattern on pushed path yields no findings."""
        body = 'out="$(producer)"; grep -q PAT <<<"$out"\n'
        self._commit_paths({"scripts/ci/clean.sh": body})
        findings = shell_flake_policy.scan_pushed(
            self.repo_root, ["scripts/ci/clean.sh"]
        )
        self.assertEqual(findings, [])

    def test_b1_out_of_scope_filtered(self) -> None:
        """B1: out-of-scope paths are not scanned."""
        self._commit_paths({"internal/x.txt": "echo x | head -1\n"})
        findings = shell_flake_policy.scan_pushed(
            self.repo_root, ["internal/x.txt"]
        )
        self.assertEqual(findings, [])

    @patch.object(shell_flake_policy, "scan_repository")
    def test_b2_empty_path_list_skips_full_scan(self, mock_scan: MagicMock) -> None:
        """B2: empty pushed path list does not fall back to full repository scan."""
        findings = shell_flake_policy.scan_pushed(self.repo_root, [])
        self.assertEqual(findings, [])
        mock_scan.assert_not_called()

    def test_b3_windows_path_normalized(self) -> None:
        """B3: backslash path separators are normalized before scan."""
        self._commit_paths({"scripts/ci/planted.sh": "echo x | head -1\n"})
        findings = shell_flake_policy.scan_pushed(
            self.repo_root, [r"scripts\ci\planted.sh"]
        )
        self.assertTrue(any(f.rule == "early-exit-reader" for f in findings))

    def test_n1_uncommitted_change_blocks_scan(self) -> None:
        """N1: worktree drift against HEAD returns uncommitted-change without scan."""
        self._commit_paths({"scripts/ci/clean.sh": "# clean\n"})
        (self.repo_root / "scripts/ci/clean.sh").write_text(
            "echo x | head -1\n", encoding="utf-8"
        )
        findings = shell_flake_policy.scan_pushed(
            self.repo_root, ["scripts/ci/clean.sh"]
        )
        self.assertTrue(any(f.rule == "uncommitted-change" for f in findings))
        self.assertFalse(any(f.rule == "early-exit-reader" for f in findings))

    def test_n2_deleted_committed_path_skips(self) -> None:
        """N2: deleted committed path produces no findings."""
        self._commit_paths({"scripts/ci/planted.sh": "echo x | head -1\n"})
        self._git("rm", "-q", "scripts/ci/planted.sh")
        self._commit("remove")
        findings = shell_flake_policy.scan_pushed(
            self.repo_root, ["scripts/ci/planted.sh"]
        )
        self.assertEqual(findings, [])

    @patch.object(shell_flake_policy, "MAX_SCAN_FILES", 2)
    def test_n3_path_bound_falls_back_to_full_scan(self) -> None:
        """N3: exceeding MAX_SCAN_FILES triggers bounded full-repository scan."""
        self._commit_paths(
            {
                "scripts/ci/a.sh": "echo ok\n",
                "scripts/ci/b.sh": "echo x | head -1\n",
            }
        )
        findings = shell_flake_policy.scan_pushed(
            self.repo_root,
            ["scripts/ci/a.sh", "scripts/ci/b.sh", "scripts/ci/gone.sh"],
        )
        self.assertTrue(
            any(
                f.rule == "early-exit-reader" and f.path == "scripts/ci/b.sh"
                for f in findings
            )
        )

    @patch.object(shell_flake_policy, "MAX_SCAN_FILES", 2)
    def test_n4_overbound_uncommitted_blocks_full_scan(self) -> None:
        """N4: over-bound fallback checks worktree divergence before full scan."""
        self._commit_paths(
            {
                "scripts/ci/a.sh": "echo ok\n",
                "scripts/ci/b.sh": "echo x | head -1\n",
            }
        )
        (self.repo_root / "scripts/ci/b.sh").write_text("echo ok\n", encoding="utf-8")
        findings = shell_flake_policy.scan_pushed(
            self.repo_root,
            ["scripts/ci/a.sh", "scripts/ci/b.sh", "scripts/ci/gone.sh"],
        )
        self.assertEqual([f.rule for f in findings], ["uncommitted-change"])

    def test_b4_out_of_scope_dirt_ignored_for_pushed_paths(self) -> None:
        """B4: uncommitted out-of-scope files do not block in-bound pushed scan."""
        self._commit_paths(
            {
                "scripts/ci/a.sh": "echo ok\n",
                "internal/x.txt": "x\n",
            }
        )
        (self.repo_root / "internal/x.txt").write_text("dirty\n", encoding="utf-8")
        findings = shell_flake_policy.scan_pushed(
            self.repo_root, ["scripts/ci/a.sh"]
        )
        self.assertEqual(findings, [])

    def test_b5_staged_rename_source_blocks_scan(self) -> None:
        """B5: staged rename leaves source path dirty and blocks scan."""
        self._commit_paths({"scripts/ci/planted.sh": "echo x | head -1\n"})
        self._git("mv", "scripts/ci/planted.sh", "scripts/ci/moved.sh")
        findings = shell_flake_policy.scan_pushed(
            self.repo_root, ["scripts/ci/planted.sh"]
        )
        self.assertEqual([f.rule for f in findings], ["uncommitted-change"])

    @patch.object(shell_flake_policy, "MAX_SCAN_FILES", 2)
    def test_n5_overbound_staged_deletion_blocks_full_scan(self) -> None:
        """N5: over-bound fallback sees HEAD path after staged rm and blocks scan."""
        self._commit_paths(
            {
                "scripts/ci/a.sh": "echo ok\n",
                "scripts/ci/b.sh": "echo x | head -1\n",
            }
        )
        self._git("rm", "-q", "scripts/ci/b.sh")
        findings = shell_flake_policy.scan_pushed(
            self.repo_root,
            ["scripts/ci/a.sh", "scripts/ci/b.sh", "scripts/ci/gone.sh"],
        )
        self.assertEqual([f.rule for f in findings], ["uncommitted-change"])

    @patch.object(shell_flake_policy, "MAX_SCAN_FILES", 2)
    def test_n6_overbound_committed_deletion_skips(self) -> None:
        """N6: over-bound fallback skips a committed deletion of a pushed path."""
        self._commit_paths(
            {
                "scripts/ci/a.sh": "echo ok\n",
                "scripts/ci/gone.sh": "echo x | head -1\n",
            }
        )
        self._git("rm", "-q", "scripts/ci/gone.sh")
        self._commit("remove")
        findings = shell_flake_policy.scan_pushed(
            self.repo_root,
            ["scripts/ci/a.sh", "scripts/ci/b.sh", "scripts/ci/gone.sh"],
        )
        self.assertEqual(findings, [])

    @patch.object(shell_flake_policy, "MAX_SCAN_FILES", 2)
    def test_b6_overbound_candidate_union_bound(self) -> None:
        """B6: union of index and HEAD divergence candidates enforces scan limit."""
        self._commit_paths(
            {
                "scripts/ci/a.sh": "echo ok\n",
                "scripts/ci/b.sh": "echo ok\n",
            }
        )
        self._git("rm", "-q", "scripts/ci/b.sh")
        target = self.repo_root / "scripts/ci/c.sh"
        target.write_text("echo ok\n", encoding="utf-8")
        self._git("add", "scripts/ci/c.sh")
        findings = shell_flake_policy.scan_pushed(
            self.repo_root,
            ["scripts/ci/a.sh", "scripts/ci/b.sh", "scripts/ci/gone.sh"],
        )
        self.assertTrue(
            any(
                f.rule == "scan-limit" and "divergence candidate count" in f.message
                for f in findings
            )
        )

    def test_w1_lefthook_prepush_wiring(self) -> None:
        """W1: lefthook pre-push runs shell_flake_policy.py --pushed on push_files."""
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
            "--pushed {push_files}"
        )
        self.assertIn(expected, section)

    @patch.object(shell_flake_policy, "scan_repository")
    def test_m1_main_dispatches_pushed_mode(self, mock_scan: MagicMock) -> None:
        """M1: main() routes --pushed through scan_pushed and maps findings to exit code."""
        finding = shell_flake_policy.Finding("a.sh", 1, "early-exit-reader", "x")
        repo = Path(__file__).resolve().parents[2]
        argv = ["shell_flake_policy.py", "--pushed", "a.sh"]
        out = io.StringIO()
        with (
            patch.object(shell_flake_policy, "scan_pushed") as mock_pushed,
            patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(out),
        ):
            mock_pushed.return_value = [finding]
            self.assertEqual(shell_flake_policy.main(), 1)
            mock_pushed.assert_called_with(repo, ["a.sh"])
            mock_pushed.return_value = []
            self.assertEqual(shell_flake_policy.main(), 0)
        mock_scan.assert_not_called()
        self.assertEqual(out.getvalue(), "a.sh:1: early-exit-reader: x\n")


if __name__ == "__main__":
    unittest.main()
