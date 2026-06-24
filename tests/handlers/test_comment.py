"""Tests for diff pre-validation helpers and modified functions in comment.py."""

import json
import subprocess
from typing import Set, Tuple
from unittest.mock import MagicMock, patch

from projctl.handlers.comment import (
    CommentPosition,
    _fetch_mr_diff_lines,
    _parse_diff_lines,
    _parse_hunk_lines,
    _post_inline_findings,
    post_inline_comment,
)

# ---------------------------------------------------------------------------
# Patch path
# ---------------------------------------------------------------------------

_PATCH_SUBPROCESS = "projctl.handlers.comment.subprocess.run"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position() -> CommentPosition:
    return CommentPosition(base_sha="base000", head_sha="head111")


def _make_finding(title: str = "Test finding") -> dict:
    return {
        "severity": "High",
        "title": title,
        "description": "A test description.",
        "fix": "",
    }


# ---------------------------------------------------------------------------
# TestParseHunkLines
# ---------------------------------------------------------------------------


class TestParseHunkLines:
    """Tests for _parse_hunk_lines."""

    def test_added_lines_are_included(self) -> None:
        """Lines beginning with '+' increment new_line and are added to valid."""
        valid: Set[Tuple[str, int]] = set()
        diff = "@@ -0,0 +1,2 @@\n+first\n+second\n"
        _parse_hunk_lines("foo.py", diff, valid)
        assert ("foo.py", 1) in valid
        assert ("foo.py", 2) in valid

    def test_context_lines_are_included(self) -> None:
        """Context lines (space-prefixed) increment new_line and are added."""
        valid: Set[Tuple[str, int]] = set()
        diff = "@@ -1,2 +1,2 @@\n context\n+added\n"
        _parse_hunk_lines("bar.py", diff, valid)
        assert ("bar.py", 1) in valid  # context line
        assert ("bar.py", 2) in valid  # added line

    def test_removed_lines_do_not_increment(self) -> None:
        """Lines beginning with '-' do not appear in valid and do not shift new_line."""
        valid: Set[Tuple[str, int]] = set()
        diff = "@@ -1,1 +1,1 @@\n-old\n+new\n"
        _parse_hunk_lines("baz.py", diff, valid)
        # Only line 1 (the "+new") should be present; removed line is not addressable
        assert ("baz.py", 1) in valid
        assert len(valid) == 1

    def test_no_newline_marker_is_skipped(self) -> None:
        r"""'\\ No newline at end of file' lines are ignored."""
        valid: Set[Tuple[str, int]] = set()
        diff = "@@ -1,1 +1,1 @@\n+last\n\\ No newline at end of file\n"
        _parse_hunk_lines("x.py", diff, valid)
        assert ("x.py", 1) in valid
        assert len(valid) == 1

    def test_hunk_header_resets_counter(self) -> None:
        """@@ header sets new_line to the start of the hunk correctly."""
        valid: Set[Tuple[str, int]] = set()
        diff = "@@ -10,1 +10,1 @@\n context\n"
        _parse_hunk_lines("a.py", diff, valid)
        assert ("a.py", 10) in valid

    def test_multi_hunk_diff_produces_correct_line_numbers(self) -> None:
        """Two hunks produce distinct, correct line numbers from each hunk."""
        valid: Set[Tuple[str, int]] = set()
        diff = "@@ -1,1 +1,1 @@\n+hunk1\n@@ -10,1 +10,1 @@\n+hunk2\n"
        _parse_hunk_lines("m.py", diff, valid)
        assert ("m.py", 1) in valid
        assert ("m.py", 10) in valid


# ---------------------------------------------------------------------------
# TestParseDiffLines
# ---------------------------------------------------------------------------


class TestParseDiffLines:
    """Tests for _parse_diff_lines."""

    def test_empty_list_returns_empty_sets(self) -> None:
        """Empty diffs list produces empty valid and unchecked sets."""
        valid, unchecked = _parse_diff_lines([])
        assert valid == set()
        assert unchecked == set()

    def test_single_diff_object_yields_correct_pairs(self) -> None:
        """A single diff object with an added line produces the expected pair."""
        diffs = [{"new_path": "src/foo.py", "diff": "@@ -0,0 +1 @@\n+hello\n"}]
        valid, unchecked = _parse_diff_lines(diffs)
        assert ("src/foo.py", 1) in valid
        assert "src/foo.py" not in unchecked

    def test_multiple_diff_objects_produce_union(self) -> None:
        """Multiple diff objects contribute their pairs to the valid set."""
        diffs = [
            {"new_path": "a.py", "diff": "@@ -0,0 +1 @@\n+x\n"},
            {"new_path": "b.py", "diff": "@@ -0,0 +1 @@\n+y\n"},
        ]
        valid, unchecked = _parse_diff_lines(diffs)
        assert ("a.py", 1) in valid
        assert ("b.py", 1) in valid
        assert unchecked == set()

    def test_missing_new_path_is_skipped(self) -> None:
        """Diff object without new_path is silently skipped."""
        diffs = [{"diff": "@@ -0,0 +1 @@\n+x\n"}]
        valid, unchecked = _parse_diff_lines(diffs)
        assert valid == set()
        assert unchecked == set()

    def test_empty_diff_text_goes_to_unchecked(self) -> None:
        """Diff object with empty diff string is added to unchecked_files, not valid."""
        diffs = [{"new_path": "large_new_file.py", "diff": ""}]
        valid, unchecked = _parse_diff_lines(diffs)
        assert valid == set()
        assert "large_new_file.py" in unchecked

    def test_mixed_empty_and_nonempty_diffs(self) -> None:
        """Files with content go to valid; files with empty diffs go to unchecked."""
        diffs = [
            {"new_path": "small.py", "diff": "@@ -0,0 +1 @@\n+x\n"},
            {"new_path": "large.py", "diff": ""},
        ]
        valid, unchecked = _parse_diff_lines(diffs)
        assert ("small.py", 1) in valid
        assert "large.py" in unchecked
        assert "small.py" not in unchecked


# ---------------------------------------------------------------------------
# TestFetchMrDiffLines
# ---------------------------------------------------------------------------


class TestFetchMrDiffLines:
    """Tests for _fetch_mr_diff_lines."""

    @patch(_PATCH_SUBPROCESS)
    def test_happy_path_returns_correct_set(self, mock_run: MagicMock) -> None:
        """Valid JSON response from glab is parsed and returns (valid, unchecked) tuple."""
        diffs = [{"new_path": "src/main.py", "diff": "@@ -0,0 +1 @@\n+hello\n"}]
        mock_run.return_value = MagicMock(stdout=json.dumps(diffs))
        result = _fetch_mr_diff_lines(42)
        assert result is not None
        valid, unchecked = result
        assert ("src/main.py", 1) in valid
        assert unchecked == set()

    @patch(_PATCH_SUBPROCESS)
    def test_empty_diff_file_goes_to_unchecked(self, mock_run: MagicMock) -> None:
        """Files with empty diff text appear in the unchecked set, not in valid."""
        diffs = [
            {"new_path": "src/main.py", "diff": "@@ -0,0 +1 @@\n+hello\n"},
            {"new_path": "src/large_new.py", "diff": ""},
        ]
        mock_run.return_value = MagicMock(stdout=json.dumps(diffs))
        result = _fetch_mr_diff_lines(42)
        assert result is not None
        valid, unchecked = result
        assert ("src/main.py", 1) in valid
        assert "src/large_new.py" in unchecked

    @patch(_PATCH_SUBPROCESS)
    def test_called_process_error_returns_none(self, mock_run: MagicMock) -> None:
        """CalledProcessError from glab causes the function to return None."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "glab")
        result = _fetch_mr_diff_lines(42)
        assert result is None

    @patch(_PATCH_SUBPROCESS)
    def test_invalid_json_returns_none(self, mock_run: MagicMock) -> None:
        """Malformed JSON response causes the function to return None."""
        mock_run.return_value = MagicMock(stdout="not-valid-json")
        result = _fetch_mr_diff_lines(42)
        assert result is None

    @patch(_PATCH_SUBPROCESS)
    def test_non_list_json_returns_none(self, mock_run: MagicMock) -> None:
        """Valid JSON that is not a list (e.g. GitLab error object) returns None without raising."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"error": "Not Found"}))
        result = _fetch_mr_diff_lines(42)
        assert result is None


# ---------------------------------------------------------------------------
# TestPostInlineCommentDiffValidation
# ---------------------------------------------------------------------------


class TestPostInlineCommentDiffValidation:
    """Tests for the diff pre-validation path in post_inline_comment."""

    def test_location_in_diff_attempts_inline_comment(self) -> None:
        """When the location is in valid_diff_lines, the inline API is called."""
        valid_diff_lines = {("src/foo.py", 5)}
        finding = _make_finding()
        position = _make_position()

        with patch(_PATCH_SUBPROCESS) as mock_run:
            mock_run.return_value = MagicMock(stdout="{}", returncode=0)
            result = post_inline_comment(
                mr_number=1,
                finding=finding,
                location="src/foo.py:5",
                position=position,
                dry_run=False,
                valid_diff_lines=valid_diff_lines,
            )

        assert result is True
        # Inline discussions endpoint must have been called
        called_cmd = mock_run.call_args[0][0]
        assert "discussions" in " ".join(called_cmd)

    def test_location_not_in_diff_calls_note_fallback_not_inline(self) -> None:
        """When location is absent from valid_diff_lines, note fallback is used, not inline."""
        valid_diff_lines = {("src/other.py", 10)}  # different file
        finding = _make_finding()
        position = _make_position()

        with patch(_PATCH_SUBPROCESS) as mock_run:
            mock_run.return_value = MagicMock(stdout="{}", returncode=0)
            result = post_inline_comment(
                mr_number=1,
                finding=finding,
                location="src/foo.py:5",
                position=position,
                dry_run=False,
                valid_diff_lines=valid_diff_lines,
            )

        # Must have returned truthy (note posted)
        assert result is not False
        # The notes endpoint (not discussions) must have been called
        called_cmd = mock_run.call_args[0][0]
        assert "notes" in " ".join(called_cmd)
        assert "discussions" not in " ".join(called_cmd)

    def test_unchecked_file_attempts_inline_not_note(self) -> None:
        """Lines in unchecked_files bypass pre-validation and attempt inline posting."""
        valid_diff_lines: Set[Tuple[str, int]] = set()  # empty — would normally block all
        unchecked_files = {"src/large_new.py"}
        finding = _make_finding()
        position = _make_position()

        with patch(_PATCH_SUBPROCESS) as mock_run:
            mock_run.return_value = MagicMock(stdout="{}", returncode=0)
            result = post_inline_comment(
                mr_number=1,
                finding=finding,
                location="src/large_new.py:301",
                position=position,
                dry_run=False,
                valid_diff_lines=valid_diff_lines,
                unchecked_files=unchecked_files,
            )

        assert result is True
        # Inline discussions endpoint must have been called, not notes
        called_cmd = mock_run.call_args[0][0]
        assert "discussions" in " ".join(called_cmd)

    def test_unchecked_file_does_not_affect_other_files(self) -> None:
        """unchecked_files exemption is file-specific; other files still use pre-validation."""
        valid_diff_lines: Set[Tuple[str, int]] = set()
        unchecked_files = {"src/large_new.py"}
        finding = _make_finding()
        position = _make_position()

        with patch(_PATCH_SUBPROCESS) as mock_run:
            mock_run.return_value = MagicMock(stdout="{}", returncode=0)
            result = post_inline_comment(
                mr_number=1,
                finding=finding,
                location="src/other.py:10",  # not in unchecked_files
                position=position,
                dry_run=False,
                valid_diff_lines=valid_diff_lines,
                unchecked_files=unchecked_files,
            )

        # Should have fallen back to note (line not in diff, file not unchecked)
        called_cmd = mock_run.call_args[0][0]
        assert "notes" in " ".join(called_cmd)
        assert "discussions" not in " ".join(called_cmd)

    def test_none_valid_diff_lines_attempts_inline(self) -> None:
        """When valid_diff_lines is None, inline comment is attempted (no regression)."""
        finding = _make_finding()
        position = _make_position()

        with patch(_PATCH_SUBPROCESS) as mock_run:
            mock_run.return_value = MagicMock(stdout="{}", returncode=0)
            result = post_inline_comment(
                mr_number=1,
                finding=finding,
                location="src/foo.py:5",
                position=position,
                dry_run=False,
                valid_diff_lines=None,
            )

        assert result is True
        called_cmd = mock_run.call_args[0][0]
        assert "discussions" in " ".join(called_cmd)

    @patch("builtins.print")
    def test_dry_run_location_not_in_diff_prints_note_message_and_returns_true(
        self, mock_print: MagicMock
    ) -> None:
        """dry_run + location not in diff prints a note message, returns True, no subprocess."""
        valid_diff_lines: Set[Tuple[str, int]] = set()
        finding = _make_finding()
        position = _make_position()

        with patch(_PATCH_SUBPROCESS) as mock_run:
            result = post_inline_comment(
                mr_number=1,
                finding=finding,
                location="src/foo.py:99",
                position=position,
                dry_run=True,
                valid_diff_lines=valid_diff_lines,
            )
            mock_run.assert_not_called()

        assert result is True
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        assert "note" in printed.lower()


# ---------------------------------------------------------------------------
# TestPostInlineFindingsDiffFetch
# ---------------------------------------------------------------------------


class TestPostInlineFindingsDiffFetch:
    """Tests for _post_inline_findings diff pre-fetch behaviour."""

    @patch("projctl.handlers.comment._fetch_mr_diff_lines")
    @patch("projctl.handlers.comment._fetch_existing_note_bodies")
    @patch("projctl.handlers.comment.post_inline_comment")
    def test_fetch_diff_lines_called_once_per_invocation(
        self,
        mock_post: MagicMock,
        mock_existing: MagicMock,
        mock_fetch_diff: MagicMock,
    ) -> None:
        """_fetch_mr_diff_lines is called exactly once per _post_inline_findings call."""
        mock_existing.return_value = set()
        mock_fetch_diff.return_value = ({("a.py", 1)}, set())
        mock_post.return_value = True

        findings = [
            {"severity": "High", "title": "F1", "description": "D1", "location": "a.py:1"},
            {"severity": "Low", "title": "F2", "description": "D2", "location": "a.py:2"},
        ]
        _post_inline_findings(10, findings, _make_position(), dry_run=False)

        mock_fetch_diff.assert_called_once_with(10)

    @patch("projctl.handlers.comment._fetch_mr_diff_lines")
    @patch("projctl.handlers.comment._fetch_existing_note_bodies")
    @patch("projctl.handlers.comment.post_inline_comment")
    def test_none_diff_lines_passes_none_to_post_inline(
        self,
        mock_post: MagicMock,
        mock_existing: MagicMock,
        mock_fetch_diff: MagicMock,
    ) -> None:
        """When _fetch_mr_diff_lines returns None, None is forwarded to post_inline_comment."""
        mock_existing.return_value = set()
        mock_fetch_diff.return_value = None
        mock_post.return_value = True

        findings = [
            {"severity": "High", "title": "F1", "description": "D1", "location": "a.py:1"},
        ]
        _post_inline_findings(10, findings, _make_position(), dry_run=False)

        _, kwargs = mock_post.call_args
        assert kwargs.get("valid_diff_lines") is None
        assert kwargs.get("unchecked_files") is None

    @patch("projctl.handlers.comment._fetch_mr_diff_lines")
    @patch("projctl.handlers.comment._fetch_existing_note_bodies")
    @patch("projctl.handlers.comment.post_inline_comment")
    def test_unchecked_files_passed_to_post_inline(
        self,
        mock_post: MagicMock,
        mock_existing: MagicMock,
        mock_fetch_diff: MagicMock,
    ) -> None:
        """unchecked_files from _fetch_mr_diff_lines is forwarded to post_inline_comment."""
        mock_existing.return_value = set()
        unchecked = {"src/large.py"}
        mock_fetch_diff.return_value = (set(), unchecked)
        mock_post.return_value = True

        findings = [
            {"severity": "High", "title": "F1", "description": "D1", "location": "src/large.py:301"},
        ]
        _post_inline_findings(10, findings, _make_position(), dry_run=False)

        _, kwargs = mock_post.call_args
        assert kwargs.get("unchecked_files") == unchecked
