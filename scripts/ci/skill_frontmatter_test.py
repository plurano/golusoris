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

    def git(self, *args: str, env: dict[str, str] | None = None) -> None:
        """Run one git command in the scratch repository."""
        git = shutil.which("git")
        self.assertIsNotNone(git)
        subprocess.run(  # noqa: S603 - fixed git command on runner PATH. # nosec B603
            [str(git), *args], cwd=self.root, env=env, check=True, capture_output=True, timeout=60
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
        return skill_frontmatter.skill_findings(self.root)

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

    def test_single_quoted_name_passes(self) -> None:
        """A single-quoted scalar without inner single quotes passes."""
        self.write("alpha/SKILL.md", "---\nname: 'alpha'\ndescription: 'Valid.'\n---\n")
        self.assertEqual(self.check(), [])

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

    def test_tracked_symlink_to_dev_zero_fails_in_both_modes(self) -> None:
        """A tracked SKILL.md symlink to /dev/zero fails without reading in both modes."""
        link = self.root / "skills" / "alpha" / "SKILL.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink("/dev/zero", link)
        except OSError as err:
            self.skipTest(f"symlinks unsupported: {err}")
        self.git("add", "--", "skills/alpha/SKILL.md")
        expected = ["skills/alpha/SKILL.md: SKILL.md must be a regular file, not a symlink"]
        self.assertEqual(skill_frontmatter.skill_findings(self.root), expected)
        self.assertEqual(skill_frontmatter.skill_findings(self.root, worktree=True), expected)

    def test_untracked_symlink_fails_in_both_modes(self) -> None:
        """An untracked SKILL.md symlink fails without being followed in both modes."""
        link = self.root / "skills" / "alpha" / "SKILL.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink("/dev/zero", link)
        except OSError as err:
            self.skipTest(f"symlinks unsupported: {err}")
        expected = ["skills/alpha/SKILL.md: SKILL.md must be a regular file, not a symlink"]
        self.assertEqual(skill_frontmatter.skill_findings(self.root), expected)
        self.assertEqual(skill_frontmatter.skill_findings(self.root, worktree=True), expected)

    def test_file_over_size_cap_fails_in_both_modes(self) -> None:
        """A SKILL.md of SIZE_MAX_BYTES + 1 bytes fails in both default and worktree modes."""
        prefix = "---\nname: alpha\ndescription: Valid.\n---\n"
        body = "x" * (skill_frontmatter.SIZE_MAX_BYTES + 1 - len(prefix))
        self.write("alpha/SKILL.md", prefix + body)
        expected = [f"alpha/SKILL.md: SKILL.md is over {skill_frontmatter.SIZE_MAX_BYTES} bytes"]
        self.assertEqual(skill_frontmatter.skill_findings(self.root), expected)
        self.assertEqual(skill_frontmatter.skill_findings(self.root, worktree=True), expected)

    def test_file_at_size_cap_passes_in_both_modes(self) -> None:
        """A SKILL.md of exactly SIZE_MAX_BYTES bytes passes in both modes."""
        prefix = "---\nname: alpha\ndescription: Valid.\n---\n"
        body = "x" * (skill_frontmatter.SIZE_MAX_BYTES - len(prefix))
        self.write("alpha/SKILL.md", prefix + body)
        self.assertEqual(skill_frontmatter.skill_findings(self.root), [])
        self.assertEqual(skill_frontmatter.skill_findings(self.root, worktree=True), [])

    def test_three_space_key_separator_passes(self) -> None:
        """Three spaces after the colon separate key and value without keeping leading spaces."""
        self.write("alpha/SKILL.md", "---\nname:   alpha\ndescription:   Valid.\n---\n")
        self.assertEqual(self.check(), [])

    def test_tab_key_separator_passes(self) -> None:
        """A tab after the colon separates key and value."""
        self.write("alpha/SKILL.md", "---\nname:\talpha\ndescription:\tValid.\n---\n")
        self.assertEqual(self.check(), [])

    def test_plain_non_string_yaml_names_fail(self) -> None:
        """Plain scalars that YAML resolves as non-strings fail with a quote-it finding."""
        for token in ("true", "null", "123", "0x1f", "1e5", "2026-11-22"):
            with self.subTest(token=token):
                self.write(f"{token}/SKILL.md", f"---\nname: {token}\ndescription: Valid.\n---\n")
                self.assertEqual(
                    self.check(),
                    [f"{token}/SKILL.md: name {token!r} is not a YAML string; quote it"],
                )
                self.git("rm", "-qf", "--", f"{token}/SKILL.md")

    def test_plain_boolean_description_fails(self) -> None:
        """A plain YAML 1.1 boolean description fails with a quote-it finding."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: yes\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: description 'yes' is not a YAML string; quote it"])

    def test_quoted_yaml_keyword_name_passes(self) -> None:
        """A double-quoted YAML keyword is a string and passes when matching its directory."""
        self.write("true/SKILL.md", '---\nname: "true"\ndescription: Valid.\n---\n')
        self.assertEqual(self.check(), [])

    def test_single_quoted_escaped_apostrophe_fails(self) -> None:
        """A single-quoted name containing doubled single quotes fails as unsupported."""
        self.write("alpha/SKILL.md", "---\nname: 'al''pha'\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported name form"])

    def test_description_continued_after_blank_line_fails(self) -> None:
        """A description continued by an indented line after a blank line fails as unsupported."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: first line\n\n  second line\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported description form"])

    def test_column_zero_continuation_line_fails_with_line_number(self) -> None:
        """A column-0 continuation line fails the structure rule with its 1-based line number."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: text\nmore text\n---\n")
        self.assertEqual(
            self.check(),
            ["alpha/SKILL.md: frontmatter line 4 is not a key, comment or list item"],
        )

    def test_staged_invalid_with_fixed_worktree_fails_by_default_only(self) -> None:
        """Default mode fails on a staged invalid name even when the working copy was fixed."""
        self.write("alpha/SKILL.md", "---\nname: beta\ndescription: Staged invalid.\n---\n")
        (self.root / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Unstaged fix.\n---\n", encoding="utf-8"
        )
        code, stderr = self.run_main(str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "skill-frontmatter: alpha/SKILL.md: name 'beta' must equal its directory 'alpha'\n")
        wt_code, wt_stderr = self.run_main("--worktree", str(self.root))
        self.assertEqual(wt_code, 0, wt_stderr)

    def test_staged_valid_with_invalid_worktree_fails_in_worktree_mode(self) -> None:
        """Default mode passes a valid staged file while --worktree catches an unstaged error."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: Staged valid.\n---\n")
        (self.root / "alpha" / "SKILL.md").write_text(
            "---\nname: beta\ndescription: Unstaged invalid.\n---\n", encoding="utf-8"
        )
        code, stderr = self.run_main(str(self.root))
        self.assertEqual(code, 0, stderr)
        wt_code, wt_stderr = self.run_main("--worktree", str(self.root))
        self.assertEqual(wt_code, 1)
        self.assertEqual(wt_stderr, "skill-frontmatter: alpha/SKILL.md: name 'beta' must equal its directory 'alpha'\n")

    def test_alternate_git_index_file_is_checked(self) -> None:
        """Default mode reads the index named by GIT_INDEX_FILE when set in os.environ."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: Valid.\n---\n")
        alt_index = (self.root / ".git" / "alt-index").resolve()
        (self.root / "alpha" / "SKILL.md").write_text(
            "---\nname: beta\ndescription: Invalid alternate index.\n---\n", encoding="utf-8"
        )
        self.git("add", "--", "alpha/SKILL.md", env={**os.environ, "GIT_INDEX_FILE": str(alt_index)})
        (self.root / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Valid.\n---\n", encoding="utf-8"
        )
        code, stderr = self.run_main(str(self.root))
        self.assertEqual(code, 0, stderr)
        with unittest.mock.patch.dict(os.environ, {"GIT_INDEX_FILE": str(alt_index)}):
            alt_code, alt_stderr = self.run_main(str(self.root))
        self.assertEqual(alt_code, 1)
        self.assertEqual(alt_stderr, "skill-frontmatter: alpha/SKILL.md: name 'beta' must equal its directory 'alpha'\n")

    def test_untracked_planted_invalid_skill_fails_in_both_modes(self) -> None:
        """An untracked planted invalid SKILL.md fails in both default and worktree modes."""
        self.write("planted--name/SKILL.md", "---\nname: planted--name\ndescription: Planted.\n---\n", stage=False)
        expected = (
            "skill-frontmatter: planted--name/SKILL.md: name 'planted--name' must be "
            + "1-64 chars of a-z, 0-9 and single hyphens, no leading or trailing hyphen\n"
        )
        code, stderr = self.run_main(str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, expected)
        wt_code, wt_stderr = self.run_main("--worktree", str(self.root))
        self.assertEqual(wt_code, 1)
        self.assertEqual(wt_stderr, expected)

    def test_reserved_word_claude_in_name_fails(self) -> None:
        """A name containing the reserved word claude fails even when matching its directory."""
        self.write("claude-helper/SKILL.md", "---\nname: claude-helper\ndescription: Valid.\n---\n")
        self.assertEqual(
            self.check(),
            ["claude-helper/SKILL.md: name 'claude-helper' must not contain the reserved word 'claude'"],
        )

    def test_reserved_word_anthropic_in_name_fails(self) -> None:
        """A name containing the reserved word anthropic fails even when matching its directory."""
        self.write("my-anthropic-tool/SKILL.md", "---\nname: my-anthropic-tool\ndescription: Valid.\n---\n")
        self.assertEqual(
            self.check(),
            ["my-anthropic-tool/SKILL.md: name 'my-anthropic-tool' must not contain the reserved word 'anthropic'"],
        )

    def test_description_with_xml_tags_fails(self) -> None:
        """A description containing XML tags fails."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: use <b>bold</b>\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: description must not contain XML tags"])

    def test_extra_argument_exits_with_status_3(self) -> None:
        """More than one positional argument prints usage and exits 3."""
        code, stderr = self.run_main(str(self.root), str(self.root))
        self.assertEqual(code, 3)
        self.assertEqual(stderr, "skill-frontmatter: error: usage: skill_frontmatter.py [--worktree] [root]\n")

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

    def test_plain_description_with_tab_comment_fails(self) -> None:
        """A plain description with tab-hash comment separation fails as unsupported."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: null\t# comment\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported description form"])

    def test_plain_description_with_colon_tab_fails(self) -> None:
        """A plain description containing a colon-tab mapping indicator fails as unsupported."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: foo:\tbar\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported description form"])

    def test_plain_name_with_tab_comment_fails(self) -> None:
        """A plain name with tab-hash comment separation fails as unsupported."""
        self.write("alpha/SKILL.md", "---\nname: alpha\t#x\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported name form"])

    def test_plain_description_with_adjacent_hash_and_colon_passes(self) -> None:
        """A plain description holding C# and a:b without separator whitespace passes."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: Uses C# and a:b syntax.\n---\n")
        self.assertEqual(self.check(), [])

    def test_description_followed_by_column_zero_list_item_fails(self) -> None:
        """A plain description followed by a column-0 list item fails as unsupported."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: first\n- second\n---\n")
        self.assertEqual(
            self.check(),
            [
                "alpha/SKILL.md: unsupported description form",
                "alpha/SKILL.md: frontmatter line 4 is a list item outside a sequence value",
            ],
        )

    def test_empty_name_followed_by_column_zero_list_item_fails(self) -> None:
        """An empty name key followed by a column-0 list item fails as unsupported."""
        self.write("alpha/SKILL.md", "---\nname:\n- alpha\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported name form"])

    def test_sequence_key_with_column_zero_list_items_passes(self) -> None:
        """An empty top-level key followed by column-0 list items passes as a sequence."""
        doc = "---\nname: alpha\nallowed-tools:\n- Read\n- Bash\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), [])

    def test_scalar_key_followed_by_column_zero_list_item_fails(self) -> None:
        """A column-0 list item after a key with an inline value fails with its line number."""
        doc = "---\nname: alpha\nlicense: MIT\n- extra\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(
            self.check(),
            ["alpha/SKILL.md: frontmatter line 4 is a list item outside a sequence value"],
        )

    def test_list_item_right_after_opening_fence_fails(self) -> None:
        """A column-0 list item right after the opening fence fails with line number 2."""
        doc = "---\n- orphan\nname: alpha\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(
            self.check(),
            ["alpha/SKILL.md: frontmatter line 2 is a list item outside a sequence value"],
        )

    def test_staged_gitlink_fails_in_both_modes(self) -> None:
        """A staged gitlink (mode 160000) at SKILL.md fails as non-regular in both modes."""
        self.git(
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        )
        head = repo_git.run_git(["rev-parse", "HEAD"], self.root).strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{head},skills/alpha/SKILL.md")
        expected = ["skills/alpha/SKILL.md: SKILL.md must be a regular file"]
        self.assertEqual(skill_frontmatter.skill_findings(self.root), expected)
        self.assertEqual(skill_frontmatter.skill_findings(self.root, worktree=True), expected)

    def test_fifo_at_staged_skill_fails_in_worktree_mode_only(self) -> None:
        """A FIFO replacing a staged valid SKILL.md fails in worktree mode and passes from the index."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo unavailable on this platform")
        self.write("skills/alpha/SKILL.md", "---\nname: alpha\ndescription: Valid.\n---\n")
        fifo = self.root / "skills" / "alpha" / "SKILL.md"
        fifo.unlink()
        try:
            os.mkfifo(fifo)
        except OSError as err:
            self.skipTest(f"mkfifo unsupported: {err}")
        self.assertEqual(
            skill_frontmatter.skill_findings(self.root, worktree=True),
            ["skills/alpha/SKILL.md: SKILL.md must be a regular file"],
        )
        self.assertEqual(skill_frontmatter.skill_findings(self.root), [])

    def test_flow_sequence_metadata_fails(self) -> None:
        """An unclosed flow sequence on metadata fails as an unsupported metadata form."""
        self.write("alpha/SKILL.md", "---\nname: alpha\ndescription: Valid.\nmetadata: [\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported metadata form"])

    def test_flow_sequence_allowed_tools_fails(self) -> None:
        """A flow sequence on allowed-tools fails as an unsupported allowed-tools form."""
        self.write("alpha/SKILL.md", "---\nname: alpha\nallowed-tools: [Read, Bash]\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported allowed-tools form"])

    def test_block_scalar_other_key_fails_once(self) -> None:
        """A block scalar on license followed by an indented line produces one finding."""
        self.write("alpha/SKILL.md", "---\nname: alpha\nlicense: |\n  MIT\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported license form"])

    def test_continued_plain_other_key_fails_once(self) -> None:
        """A plain value on license followed by an indented continuation produces one finding."""
        self.write("alpha/SKILL.md", "---\nname: alpha\nlicense: MIT\n  more\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), ["alpha/SKILL.md: unsupported license form"])

    def test_single_quoted_and_plain_other_keys_pass(self) -> None:
        """Single-quoted and plain scalars on other top-level keys pass."""
        doc = "---\nname: alpha\nlicense: 'MIT'\ncompatibility: claude-code\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), [])

    def test_non_string_scalar_on_other_key_passes(self) -> None:
        """A non-string plain scalar on a top-level key other than name or description passes."""
        self.write("alpha/SKILL.md", "---\nname: alpha\nversion: 3.17.0\ndescription: Valid.\n---\n")
        self.assertEqual(self.check(), [])

    def test_nested_metadata_plain_and_quoted_scalars_pass(self) -> None:
        """Same-indented plain and double-quoted key-value lines under metadata pass."""
        doc = (
            "---\n"
            "name: alpha\n"
            "metadata:\n"
            "  author: olgasafonova\n"
            '  derived_from: "https://x.y (MIT)"\n'
            "description: Valid.\n"
            "---\n"
        )
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), [])

    def test_nested_metadata_flow_scalar_fails_with_line_number(self) -> None:
        """A flow indicator inside a nested metadata value fails naming its 1-based line."""
        doc = "---\nname: alpha\nmetadata:\n  author: [\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), ["alpha/SKILL.md: frontmatter line 4 is an unsupported nested value"])

    def test_nested_metadata_deeper_indentation_fails(self) -> None:
        """A deeper-indented line inside metadata fails on the deeper line."""
        doc = "---\nname: alpha\nmetadata:\n  a: x\n    b: y\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), ["alpha/SKILL.md: frontmatter line 5 is an unsupported nested value"])

    def test_nested_metadata_tab_indentation_fails(self) -> None:
        """A tab-indented line inside metadata fails naming its 1-based line."""
        doc = "---\nname: alpha\nmetadata:\n\tauthor: x\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), ["alpha/SKILL.md: frontmatter line 4 is an unsupported nested value"])

    def test_nested_metadata_mixed_mapping_and_list_fails(self) -> None:
        """Mixing a list item into a nested mapping block fails on the list item line."""
        doc = "---\nname: alpha\nmetadata:\n  a: x\n  - y\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), ["alpha/SKILL.md: frontmatter line 5 is an unsupported nested value"])

    def test_nested_indented_scalar_list_passes(self) -> None:
        """Same-indented scalar list items under metadata pass."""
        doc = "---\nname: alpha\nmetadata:\n  - Read\n  - Bash\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), [])

    def test_nested_metadata_indented_comment_passes(self) -> None:
        """An indented comment before nested mapping entries under metadata passes."""
        doc = "---\nname: alpha\nmetadata:\n  # comment\n  a: x\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), [])

    def test_column_zero_flow_list_item_fails_with_line_number(self) -> None:
        """A column-0 list item with a flow value fails as an unsupported list item."""
        doc = "---\nname: alpha\nallowed-tools:\n- [\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), ["alpha/SKILL.md: frontmatter line 4 is an unsupported list item"])

    def test_indented_line_after_column_zero_list_item_fails(self) -> None:
        """An indented continuation after a column-0 list item fails on the indented line."""
        doc = "---\nname: alpha\nallowed-tools:\n- Read\n  more\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), ["alpha/SKILL.md: frontmatter line 5 is an unsupported nested value"])

    def test_indented_line_after_opening_fence_fails(self) -> None:
        """An indented line right after the opening fence fails on line 2."""
        doc = "---\n  a: x\nname: alpha\ndescription: Valid.\n---\n"
        self.write("alpha/SKILL.md", doc)
        self.assertEqual(self.check(), ["alpha/SKILL.md: frontmatter line 2 is an unsupported nested value"])


if __name__ == "__main__":
    unittest.main()
