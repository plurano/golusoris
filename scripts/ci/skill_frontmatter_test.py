#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Tests for skill_frontmatter.py: the repository passes, invalid frontmatter fails."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess  # Runs only fixed git commands in a scratch repository. # nosec B404
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import repo_git
import skill_frontmatter

ROOT = Path(__file__).resolve().parents[2]


class SkillFrontmatterTest(unittest.TestCase):
    """Positive, negative, boundary, discovery and exit-code cases for SKILL.md frontmatter."""

    def setUp(self) -> None:
        """Create a scratch repository with the hook's git variables removed."""
        self.enterContext(unittest.mock.patch.dict(os.environ, repo_git.fixture_environ(), clear=True))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.git("init", "-q")

    def git(self, *args: str) -> None:
        """Run one git command in the scratch repository."""
        git = shutil.which("git")
        self.assertIsNotNone(git)
        subprocess.run(  # noqa: S603 - fixed git command on runner PATH. # nosec B603
            [str(git), *args], cwd=self.root, check=True, capture_output=True, timeout=60
        )

    def write(self, name: str, text: str, *, stage: bool = True) -> None:
        """Write a UTF-8 file into the scratch repository and optionally stage it."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        if stage:
            self.git("add", "--", name)

    def check(self) -> list[str]:
        """Return frontmatter findings across non-ignored SKILL.md files in self.root."""
        findings: list[str] = []
        for path in repo_git.worktree_files(self.root, skill_frontmatter.NAME):
            rel = path.relative_to(self.root).as_posix()
            text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
            findings.extend(skill_frontmatter.problems(rel, text))
        return findings

    def run_main(self, *args: str) -> tuple[int, str]:
        """Run skill_frontmatter.main capturing stderr and return (code, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = skill_frontmatter.main(["skill_frontmatter", *args])
        return code, err.getvalue()

    def test_repository_skills_pass(self) -> None:
        """Every SKILL.md in this repository satisfies the Agent Skills specification."""
        code, stderr = self.run_main(str(ROOT))
        self.assertEqual(code, 0, stderr)

    def test_plain_name_and_description_passes(self) -> None:
        """Plain single-line name and description scalars pass."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: Plain skill description.\n---\n")
        self.assertEqual(self.check(), [])

    def test_double_quoted_description_passes(self) -> None:
        """A double-quoted description without quotes or backslashes passes."""
        self.write("alpha/SKILL.md", '---\nname: alpha\ndescription: "Quoted: description #1:"\n---\n')
        self.assertEqual(self.check(), [])

    def test_crlf_file_passes(self) -> None:
        """A SKILL.md with CRLF line endings is normalised to LF and passes."""
        self.write("alpha/SKILL.md", "---\r\nname: alpha\r\ndescription: CRLF skill.\r\n---\r\n")
        self.assertEqual(self.check(), [])

    def test_name_of_64_chars_equal_to_directory_passes(self) -> None:
        """A 64-character lowercase name equal to its parent directory passes."""
        name = "a" * 64
        self.write(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), [])

    def test_description_of_1_char_passes(self) -> None:
        """A 1-character description passes the lower bound."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: x\n---\n")
        self.assertEqual(self.check(), [])

    def test_description_of_1024_chars_passes(self) -> None:
        """A 1024-character description passes the upper bound."""
        desc = "x" * 1024
        self.write("alpha/SKILL.md", f"---\nname: alpha\ndescription: {desc}\n---\n")
        self.assertEqual(self.check(), [])

    def test_other_keys_and_indented_metadata_block_pass(self) -> None:
        """Other top-level keys and an indented metadata block are ignored."""
        doc = (
            "---\n"
            "name: alpha\n"
            "license: MIT\n"
            "metadata:\n"
            "  author: example\n"
            "  name: nested-ignored\n"
            "description: Valid with metadata.\n"
            "---\n"
        )
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), [])

    def test_double_hyphen_name_fails(self) -> None:
        """A name containing consecutive hyphens fails even when matching its directory."""
        self.write("alpha--beta/SKILL.md", "---\nname: alpha--beta\ndescription: Invalid.\n---\n")
        self.assertEqual(
            self.check(),
            [
                "alpha--beta/SKILL.md: name 'alpha--beta' must be 1-64 chars of a-z, 0-9 and "
                + "single hyphens, no leading or trailing hyphen"
            ],
        )

    def test_uppercase_name_fails(self) -> None:
        """An uppercase name fails even when matching its directory."""
        self.write("Alpha/SKILL.md", "---\nname: Alpha\ndescription: Invalid.\n---\n")
        self.assertEqual(
            self.check(),
            [
                "Alpha/SKILL.md: name 'Alpha' must be 1-64 chars of a-z, 0-9 and "
                + "single hyphens, no leading or trailing hyphen"
            ],
        )

    def test_leading_hyphen_name_fails(self) -> None:
        """A name starting with a hyphen fails even when matching its directory."""
        self.write("-alpha/SKILL.md", '---\nname: "-alpha"\ndescription: Invalid.\n---\n')
        self.assertEqual(
            self.check(),
            [
                "-alpha/SKILL.md: name '-alpha' must be 1-64 chars of a-z, 0-9 and "
                + "single hyphens, no leading or trailing hyphen"
            ],
        )

    def test_trailing_hyphen_name_fails(self) -> None:
        """A name ending with a hyphen fails even when matching its directory."""
        self.write("alpha-/SKILL.md", "---\nname: alpha-\ndescription: Invalid.\n---\n")
        self.assertEqual(
            self.check(),
            [
                "alpha-/SKILL.md: name 'alpha-' must be 1-64 chars of a-z, 0-9 and "
                + "single hyphens, no leading or trailing hyphen"
            ],
        )

    def test_name_of_65_chars_equal_to_directory_fails(self) -> None:
        """A 65-character name fails the 64-character upper bound."""
        name = "a" * 65
        self.write(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: Invalid.\n---\n")
        self.assertEqual(
            self.check(),
            [
                f"{name}/SKILL.md: name {name!r} must be 1-64 chars of a-z, 0-9 and "
                + "single hyphens, no leading or trailing hyphen"
            ],
        )

    def test_name_mismatching_directory_fails(self) -> None:
        """A valid lowercase name that differs from its parent directory fails."""
        self.write("alpha/SKILL.md", "---\nname: beta\ndescription: Mismatched directory.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: name 'beta' must equal its directory 'alpha'"])

    def test_empty_description_fails(self) -> None:
        """An empty description fails the 1-1024 character rule."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription:\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: description must be 1-1024 chars, has 0"])

    def test_description_of_1025_chars_fails(self) -> None:
        """A 1025-character description fails the 1024-character upper bound."""
        desc = "x" * 1025
        self.write("alpha/SKILL.md", f"---\nname: alpha\ndescription: {desc}\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: description must be 1-1024 chars, has 1025"])

    def test_missing_name_fails(self) -> None:
        """Frontmatter without a name field reports missing name."""
        self.write("alpha/SKILL.md", "---\ndescription: Missing name.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: missing name"])

    def test_missing_description_fails(self) -> None:
        """Frontmatter without a description field reports missing description."""
        self.write("alpha/SKILL.md", "---\nname: alpha\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: missing description"])

    def test_duplicate_name_fails(self) -> None:
        """Repeated name keys in frontmatter report duplicate name."""
        self.write("alpha/SKILL.md", "---\nname: alpha\nname: alpha\ndescription: Duplicate.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: duplicate name"])

    def test_no_frontmatter_fails(self) -> None:
        """A SKILL.md whose first line is not --- reports no frontmatter."""
        self.write("alpha/SKILL.md", "# Skill without frontmatter\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: no frontmatter"])

    def test_unterminated_frontmatter_fails(self) -> None:
        """A SKILL.md without a closing --- line reports unterminated frontmatter."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: Open.\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unterminated frontmatter"])

    def test_single_quoted_name_fails(self) -> None:
        """A single-quoted scalar is rejected as an unsupported name form."""
        self.write("alpha/SKILL.md", "---\nname: 'alpha'\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported name form"])

    def test_double_quoted_backslash_fails(self) -> None:
        """A double-quoted scalar containing a backslash is rejected as unsupported."""
        self.write("alpha/SKILL.md", '---\nname: alpha\ndescription: "line\\ntwo"\n---\n')
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported description form"])

    def test_block_scalar_description_fails(self) -> None:
        """A block scalar followed by an indented line is rejected as unsupported."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: >\n  folded text\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported description form"])

    def test_plain_description_containing_comment_marker_fails(self) -> None:
        """A plain description containing space-hash is rejected as unsupported."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: plain # comment\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported description form"])

    def test_untracked_non_ignored_skill_is_checked(self) -> None:
        """An untracked SKILL.md that is not ignored by git is checked."""
        self.write("alpha/SKILL.md", "---\nname: beta\ndescription: Untracked.\n---\n", stage=False)
        self.assertEqual(self.check(), ["alpha/SKILL.md: name 'beta' must equal its directory 'alpha'"])

    def test_gitignored_skill_is_skipped(self) -> None:
        """A SKILL.md under a directory listed in .gitignore is skipped."""
        self.write(".gitignore", "ignored/\n")
        self.write("ignored/alpha/SKILL.md", "# Invalid but ignored\n", stage=False)
        self.assertEqual(self.check(), [])

    def test_findings_exit_with_status_1(self) -> None:
        """Findings produce stderr diagnostics and exit status 1."""
        self.write("alpha/SKILL.md", "---\nname: beta\ndescription: Invalid.\n---\n")
        code, stderr = self.run_main(str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "skill-frontmatter: alpha/SKILL.md: name 'beta' must equal its directory 'alpha'\n")

    def test_extra_argument_exits_with_status_3(self) -> None:
        """More than one positional argument prints usage and exits 3."""
        code, stderr = self.run_main(str(self.root), str(self.root))
        self.assertEqual(code, 3)
        self.assertEqual(stderr, "skill-frontmatter: error: usage: skill_frontmatter.py [root]\n")

    def test_missing_root_exits_with_status_3(self) -> None:
        """A non-existent root directory prints an error and exits 3."""
        code, stderr = self.run_main(str(self.root / "absent"))
        self.assertEqual(code, 3)
        self.assertIn("skill-frontmatter: error:", stderr)

    def test_removed_git_directory_exits_with_status_3(self) -> None:
        """A root whose .git directory was removed prints an error and exits 3."""
        shutil.rmtree(self.root / ".git")
        code, stderr = self.run_main(str(self.root))
        self.assertEqual(code, 3)
        self.assertIn("skill-frontmatter: error:", stderr)


if __name__ == "__main__":
    unittest.main()
