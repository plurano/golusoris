# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Positive, negative, and boundary tests for the shared policy-check git helpers."""

from __future__ import annotations

import os
import shutil
import subprocess  # Runs only this repository's own policy script. # nosec B404
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import repo_git


class RepoGitCase(unittest.TestCase):
    """Give each test a scratch directory with the caller's git variables removed."""

    def setUp(self) -> None:
        """Create the scratch directory inside an isolated git environment."""
        isolation = repo_git.scratch_environ()
        isolation.__enter__()
        self.addCleanup(isolation.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory(prefix="golusoris-repo-git-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, rel: str, text: str = "fixture\n") -> Path:
        """Write text to rel under the scratch directory, creating parents."""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def listed(self, name: str = "go.mod") -> list[str]:
        """Return worktree_files results relative to the scratch directory."""
        return [p.relative_to(self.root).as_posix() for p in repo_git.worktree_files(self.root, name)]


class WorktreeFilesTest(RepoGitCase):
    """Exercise the git-backed listing that replaced the filesystem walks."""

    def setUp(self) -> None:
        """Initialise a scratch repository that ignores ignored/."""
        super().setUp()
        repo_git.run_git(["init", "-q"], self.root)
        self.write(".gitignore", "/ignored/\n")

    def test_tracked_and_untracked_files_are_listed(self) -> None:
        """Positive: root, tracked nested, and untracked nested files are all listed."""
        self.write("go.mod")
        self.write("a/b/go.mod")
        repo_git.run_git(["add", "."], self.root)
        self.write("untracked/go.mod")
        self.assertEqual(self.listed(), ["a/b/go.mod", "go.mod", "untracked/go.mod"])

    def test_ignored_deleted_and_similar_names_are_not_listed(self) -> None:
        """Negative: ignored, deleted tracked, and differently named files stay out."""
        self.write("ignored/sub/go.mod")
        deleted = self.write("deleted/go.mod")
        self.write("x/xgo.mod")
        self.write("x/go.mod.bak")
        repo_git.run_git(["add", "."], self.root)
        deleted.unlink()
        self.assertEqual(self.listed(), [])

    def test_dangling_symlink_is_listed(self) -> None:
        """Boundary: a dangling symlink stays in the list so callers report it."""
        try:
            (self.root / "go.mod").symlink_to(self.root / "missing" / "go.mod")
        except OSError as error:
            self.skipTest(f"symlinks unsupported: {error}")
        self.assertEqual(self.listed(), ["go.mod"])

    def test_inherited_repository_overrides_are_ignored(self) -> None:
        """Boundary: inherited GIT_DIR and GIT_INDEX_FILE overrides do not redirect the listing."""
        self.write("go.mod")
        self.write("ignored/sub/go.mod")
        repo_git.run_git(["add", "."], self.root)
        decoy_dir = tempfile.TemporaryDirectory(prefix="golusoris-repo-git-decoy-")
        self.addCleanup(decoy_dir.cleanup)
        decoy = Path(decoy_dir.name)
        repo_git.run_git(["init", "-q"], decoy)
        decoy_sub = decoy / "ignored" / "sub" / "go.mod"
        decoy_sub.parent.mkdir(parents=True, exist_ok=True)
        decoy_sub.write_text("fixture\n", encoding="utf-8")
        repo_git.run_git(["add", "."], decoy)
        overrides = {
            "GIT_DIR": str(decoy / ".git"),
            "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
        }
        for var, value in overrides.items():
            with self.subTest(var=var), unittest.mock.patch.dict(os.environ, {var: value}):
                self.assertEqual(self.listed(), ["go.mod"])

    def test_non_repository_fails_closed(self) -> None:
        """Negative: a directory outside any repository raises GitError."""
        outside = tempfile.TemporaryDirectory(prefix="golusoris-repo-git-outside-")
        self.addCleanup(outside.cleanup)
        ceiling = Path(outside.name).resolve()
        non_repo = ceiling / "outside"
        non_repo.mkdir()
        # GIT_CEILING_DIRECTORIES bounds strict subdirectories of the ceiling, not cwd itself.
        with unittest.mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(ceiling)}):
            with self.assertRaises(repo_git.GitError):
                repo_git.worktree_files(non_repo, "go.mod")


class StagedAndBlobHelpersTest(RepoGitCase):
    """Exercise staged index entries, blob size/content reads, and untracked listing."""

    def setUp(self) -> None:
        """Initialise a scratch repository that ignores ignored/."""
        super().setUp()
        repo_git.run_git(["init", "-q"], self.root)
        self.write(".gitignore", "/ignored/\n")

    def test_staged_entries_lists_exact_basename_and_mode(self) -> None:
        """Positive: staged_entries returns mode 100644 and skips similarly named XSKILL.md."""
        self.write("a/SKILL.md", "skill\n")
        self.write("a/XSKILL.md", "other\n")
        repo_git.run_git(["add", "."], self.root)
        entries = repo_git.staged_entries(self.root, "SKILL.md")
        self.assertEqual(list(entries), ["a/SKILL.md"])
        mode, oid = entries["a/SKILL.md"]
        self.assertEqual(mode, "100644")
        self.assertEqual(len(oid), 40)

    def test_staged_entries_lists_symlink_mode(self) -> None:
        """Boundary: a staged symlink entry reports git mode 120000."""
        link = self.root / "a" / "SKILL.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink("/dev/zero", link)
        except OSError as error:
            self.skipTest(f"symlinks unsupported: {error}")
        repo_git.run_git(["add", "--", "a/SKILL.md"], self.root)
        entries = repo_git.staged_entries(self.root, "SKILL.md")
        self.assertEqual(entries["a/SKILL.md"][0], "120000")

    def test_read_blobs_and_blob_sizes_return_staged_bytes(self) -> None:
        """Positive: read_blobs returns staged bytes and blob_sizes returns their lengths."""
        self.write("SKILL.md", "hello\n")
        self.write("sub/SKILL.md", "longer content\n")
        repo_git.run_git(["add", "."], self.root)
        (self.root / "SKILL.md").write_text("unstaged overwrite\n", encoding="utf-8")
        entries = repo_git.staged_entries(self.root, "SKILL.md")
        oids = [entries["SKILL.md"][1], entries["sub/SKILL.md"][1]]
        self.assertEqual(repo_git.blob_sizes(self.root, oids), [6, 15])
        self.assertEqual(repo_git.read_blobs(self.root, oids), [b"hello\n", b"longer content\n"])

    def test_unknown_object_id_raises_git_error(self) -> None:
        """Negative: an unknown object ID raises GitError in both blob_sizes and read_blobs."""
        missing = "0" * 40
        with self.assertRaises(repo_git.GitError):
            repo_git.blob_sizes(self.root, [missing])
        with self.assertRaises(repo_git.GitError):
            repo_git.read_blobs(self.root, [missing])

    def test_untracked_files_lists_untracked_and_skips_ignored(self) -> None:
        """Positive: untracked_files returns non-ignored exact matches and skips ignored ones."""
        self.write("tracked/SKILL.md", "tracked\n")
        repo_git.run_git(["add", "."], self.root)
        self.write("new/SKILL.md", "untracked\n")
        self.write("new/XSKILL.md", "other\n")
        self.write("ignored/SKILL.md", "ignored\n")
        self.assertEqual(repo_git.untracked_files(self.root, "SKILL.md"), ["new/SKILL.md"])


class ScratchEnvironTest(unittest.TestCase):
    """Exercise variable removal, restoration, and fail-closed lookup."""

    def setUp(self) -> None:
        """Give each test a scratch directory, a decoy index path, and no inherited hook git variables."""
        self.enterContext(unittest.mock.patch.dict(os.environ, repo_git.fixture_environ(), clear=True))
        directory = tempfile.TemporaryDirectory(prefix="golusoris-repo-git-env-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.decoy = self.root / "decoy-index"

    def fixture(self) -> None:
        """Initialise a repository and stage one file, as the policy fixtures do."""
        repo_git.run_git(["init", "-q"], self.root)
        (self.root / "file.txt").write_text("fixture\n", encoding="utf-8")
        repo_git.run_git(["add", "."], self.root)

    def test_hook_index_does_not_receive_fixture_writes(self) -> None:
        """Positive: an inherited GIT_INDEX_FILE stays untouched by git add in the fixture."""
        with unittest.mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(self.decoy)}):
            with repo_git.scratch_environ():
                self.assertNotIn("GIT_INDEX_FILE", os.environ)
                self.fixture()
            self.assertEqual(os.environ["GIT_INDEX_FILE"], str(self.decoy))
        self.assertFalse(self.decoy.exists())
        self.assertTrue((self.root / ".git" / "index").is_file())

    def test_without_isolation_the_decoy_index_is_written(self) -> None:
        """Negative control: the same fixture without isolation writes the inherited index."""
        with unittest.mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(self.decoy)}):
            self.fixture()
        self.assertTrue(self.decoy.is_file())

    def test_unset_and_unrelated_variables_are_left_alone(self) -> None:
        """Boundary: unset local variables stay unset; non-local GIT_ variables are kept."""
        with unittest.mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "fixture"}):
            os.environ.pop("GIT_DIR", None)
            with repo_git.scratch_environ():
                self.assertEqual(os.environ["GIT_AUTHOR_NAME"], "fixture")
            self.assertNotIn("GIT_DIR", os.environ)
            self.assertEqual(os.environ["GIT_AUTHOR_NAME"], "fixture")

    def test_missing_git_fails_closed(self) -> None:
        """Negative: without git on PATH the lookup raises and the environment is unchanged."""
        with unittest.mock.patch.dict(os.environ, {"PATH": "", "GIT_INDEX_FILE": str(self.decoy)}):
            with self.assertRaises(repo_git.GitError), repo_git.scratch_environ():
                self.fail("scratch_environ entered without git")
            self.assertEqual(os.environ["GIT_INDEX_FILE"], str(self.decoy))


class HookVariableSuiteTest(unittest.TestCase):
    """Run the fixture suites the way a pre-push hook in a linked worktree starts them."""

    def test_negative_suites_under_hook_git_dir_leave_the_repository_alone(self) -> None:
        """Negative: an exported linked-worktree GIT_DIR reaches no fixture init, add, or config write."""
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite(loader.loadTestsFromTestCase(case) for case in (RepoGitCase, ScratchEnvironTest))
        result = unittest.TestResult()
        with repo_git.decoy_environ():
            suite.run(result)
        self.assertEqual(result.errors + result.failures, [])
        self.assertGreater(result.testsRun, 0)

    def test_boundary_decoy_detects_a_shared_config_write(self) -> None:
        """Boundary: the decoy itself fails closed when hook-directed git init rewrites core.bare."""
        outside = tempfile.TemporaryDirectory(prefix="golusoris-repo-git-init-")
        self.addCleanup(outside.cleanup)
        with self.assertRaisesRegex(repo_git.GitError, "shared config changed"), repo_git.decoy_environ():
            repo_git.run_git(["init", "-q"], Path(outside.name))


class CallerExitCodeTest(unittest.TestCase):
    """Pin how a policy script reports a git failure to its caller."""

    def test_renovate_policy_exits_3_without_git(self) -> None:
        """Negative: missing git makes renovate-policy-test.py exit 3, not 1 with a traceback."""
        script = Path(__file__).with_name("renovate-policy-test.py")
        proc = subprocess.run(  # noqa: S603 - runs this repository's own script. # nosec B603
            [sys.executable, "-B", str(script)],
            env={**os.environ, "PATH": ""},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("ERROR: git", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class FixtureRepositoryTest(unittest.TestCase):
    """Exercise fixture_repository ceiling isolation, environment restore, and cleanup."""

    def test_yields_fixture_inside_enclosing_repository(self) -> None:
        """Positive: rev-parse --show-toplevel resolves the fixture and its enclosing parent."""
        with repo_git.fixture_repository() as root:
            top = Path(repo_git.run_git(["rev-parse", "--show-toplevel"], root).strip())
            parent = Path(repo_git.run_git(["rev-parse", "--show-toplevel"], root.parent).strip())
            self.assertEqual(top, root)
            self.assertEqual(parent, root.parent)

    def test_removed_fixture_git_dir_does_not_escape_to_enclosing_repository(self) -> None:
        """Negative: removing the fixture .git makes rev-parse fail instead of finding the parent."""
        with repo_git.fixture_repository() as root:
            shutil.rmtree(root / ".git")
            with self.assertRaises(repo_git.GitError):
                repo_git.run_git(["rev-parse", "--show-toplevel"], root)

    def test_restores_environment_and_removes_tempdir_on_exit_and_error(self) -> None:
        """Boundary: sentinel variables are restored and the tempdir is removed on exit and error."""
        sentinels = {
            "GIT_CEILING_DIRECTORIES": "/sentinel/ceiling",
            "GOLUSORIS_FIXTURE_SENTINEL": "sentinel-value",
        }
        with unittest.mock.patch.dict(os.environ, sentinels):
            with repo_git.fixture_repository() as root:
                enclosing = root.parent
                self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], str(enclosing))
                self.assertEqual(os.environ["GOLUSORIS_FIXTURE_SENTINEL"], "sentinel-value")
                os.environ["GOLUSORIS_FIXTURE_SENTINEL"] = "changed-inside"
            self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], "/sentinel/ceiling")
            self.assertEqual(os.environ["GOLUSORIS_FIXTURE_SENTINEL"], "sentinel-value")
            self.assertFalse(enclosing.exists())
            entered: list[Path] = []

            def raise_inside_fixture() -> None:
                """Enter fixture_repository, mutate the sentinel variable, and raise."""
                with repo_git.fixture_repository() as err_root:
                    entered.append(err_root.parent)
                    os.environ["GOLUSORIS_FIXTURE_SENTINEL"] = "changed-on-error"
                    raise RuntimeError("boom")

            self.assertRaisesRegex(RuntimeError, "boom", raise_inside_fixture)
            self.assertEqual(len(entered), 1)
            self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], "/sentinel/ceiling")
            self.assertEqual(os.environ["GOLUSORIS_FIXTURE_SENTINEL"], "sentinel-value")
            self.assertFalse(entered[0].exists())


if __name__ == "__main__":
    unittest.main()
