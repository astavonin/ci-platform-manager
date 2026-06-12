"""Tests for projctl.handlers.note module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from projctl.exceptions import PlatformError
from projctl.handlers.note import NoteHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_PATH = "projctl.handlers.note.run_glab_command"


def _make_handler(dry_run: bool = False) -> NoteHandler:
    """Create a NoteHandler with a MagicMock config."""
    return NoteHandler(config=MagicMock(), dry_run=dry_run)


def _endpoint_from_call(mock_run: MagicMock) -> str:
    """Return the API endpoint argument from the last run_glab_command call."""
    args = mock_run.call_args[0][0]
    # The endpoint is the argument immediately after "-X POST"
    for i, arg in enumerate(args):
        if arg == "POST" and i + 1 < len(args):
            return args[i + 1]
    return ""


# ---------------------------------------------------------------------------
# add_issue_note
# ---------------------------------------------------------------------------


class TestAddIssueNote:
    """Tests for NoteHandler.add_issue_note."""

    @patch(_PATCH_PATH)
    def test_add_issue_note_happy_path(self, mock_run: MagicMock) -> None:
        """Successful note post uses :fullpath endpoint for a plain numeric ref."""
        mock_run.return_value = json.dumps({"id": 42, "noteable_iid": 5})
        handler = _make_handler()

        handler.add_issue_note("123", "hello")

        assert _endpoint_from_call(mock_run) == "projects/:fullpath/issues/123/notes"

    @patch(_PATCH_PATH)
    def test_add_issue_note_hash_prefix(self, mock_run: MagicMock) -> None:
        """Hash-prefixed reference '#123' is accepted and stripped."""
        mock_run.return_value = json.dumps({"id": 42, "noteable_iid": 5})
        handler = _make_handler()

        handler.add_issue_note("#123", "hello")

        assert _endpoint_from_call(mock_run) == "projects/:fullpath/issues/123/notes"

    @patch(_PATCH_PATH)
    def test_add_issue_note_url_uses_project_path(self, mock_run: MagicMock) -> None:
        """Full issue URL encodes the project path into the endpoint."""
        mock_run.return_value = json.dumps({"id": 42, "noteable_iid": 5})
        handler = _make_handler()

        handler.add_issue_note(
            "https://gitlab.com/group/project/-/issues/123", "hello"
        )

        assert (
            _endpoint_from_call(mock_run)
            == "projects/group%2Fproject/issues/123/notes"
        )

    @patch(_PATCH_PATH)
    def test_add_issue_note_url_with_fragment(self, mock_run: MagicMock) -> None:
        """Issue URL with a '#note_NNN' fragment strips the fragment."""
        mock_run.return_value = json.dumps({"id": 42, "noteable_iid": 5})
        handler = _make_handler()

        handler.add_issue_note(
            "https://gitlab.com/group/project/-/issues/123#note_456", "hello"
        )

        assert (
            _endpoint_from_call(mock_run)
            == "projects/group%2Fproject/issues/123/notes"
        )

    @patch(_PATCH_PATH)
    def test_add_issue_note_body_is_sent_to_api(self, mock_run: MagicMock) -> None:
        """The note body is included in the glab command arguments."""
        mock_run.return_value = json.dumps({"id": 42, "noteable_iid": 5})
        handler = _make_handler()

        handler.add_issue_note("123", "my note body")

        call_args = mock_run.call_args[0][0]
        assert any("my note body" in arg for arg in call_args)

    def test_add_issue_note_invalid_ref(self) -> None:
        """Non-numeric, non-URL reference raises ValueError with the ref in the message."""
        handler = _make_handler()

        with pytest.raises(ValueError, match="garbage-not-a-number"):
            handler.add_issue_note("garbage-not-a-number", "hello")

    def test_add_issue_note_hash_non_numeric_raises(self) -> None:
        """'#abc' (non-numeric after stripping #) raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_issue_note("#abc", "hello")

    def test_add_issue_note_empty_body(self) -> None:
        """Empty body raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_issue_note("123", "")

    def test_add_issue_note_whitespace_body(self) -> None:
        """Whitespace-only body raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_issue_note("123", "   ")


# ---------------------------------------------------------------------------
# add_mr_note
# ---------------------------------------------------------------------------


class TestAddMrNote:
    """Tests for NoteHandler.add_mr_note."""

    @patch(_PATCH_PATH)
    def test_add_mr_note_happy_path(self, mock_run: MagicMock) -> None:
        """Successful note post uses :fullpath endpoint for a plain numeric ref."""
        mock_run.return_value = json.dumps({"id": 99, "noteable_iid": 3})
        handler = _make_handler()

        handler.add_mr_note("456", "lgtm")

        assert _endpoint_from_call(mock_run) == "projects/:fullpath/merge_requests/456/notes"

    @patch(_PATCH_PATH)
    def test_add_mr_note_bang_prefix(self, mock_run: MagicMock) -> None:
        """Bang-prefixed reference '!456' is accepted and stripped."""
        mock_run.return_value = json.dumps({"id": 99, "noteable_iid": 3})
        handler = _make_handler()

        handler.add_mr_note("!456", "lgtm")

        assert _endpoint_from_call(mock_run) == "projects/:fullpath/merge_requests/456/notes"

    @patch(_PATCH_PATH)
    def test_add_mr_note_url_uses_project_path(self, mock_run: MagicMock) -> None:
        """Full MR URL encodes the project path into the endpoint."""
        mock_run.return_value = json.dumps({"id": 99, "noteable_iid": 3})
        handler = _make_handler()

        handler.add_mr_note(
            "https://gitlab.com/group/project/-/merge_requests/789", "lgtm"
        )

        assert (
            _endpoint_from_call(mock_run)
            == "projects/group%2Fproject/merge_requests/789/notes"
        )

    @patch(_PATCH_PATH)
    def test_add_mr_note_url_with_fragment(self, mock_run: MagicMock) -> None:
        """URL with a '#note_NNN' fragment has the fragment stripped."""
        mock_run.return_value = json.dumps({"id": 99, "noteable_iid": 3})
        handler = _make_handler()

        handler.add_mr_note(
            "https://gitlab.com/group/project/-/merge_requests/789#note_123", "lgtm"
        )

        assert (
            _endpoint_from_call(mock_run)
            == "projects/group%2Fproject/merge_requests/789/notes"
        )

    @patch(_PATCH_PATH)
    def test_add_mr_note_url_with_query(self, mock_run: MagicMock) -> None:
        """URL with query parameters has the query string stripped."""
        mock_run.return_value = json.dumps({"id": 99, "noteable_iid": 3})
        handler = _make_handler()

        handler.add_mr_note(
            "https://gitlab.com/g/p/-/merge_requests/789?diff_id=5", "lgtm"
        )

        assert (
            _endpoint_from_call(mock_run)
            == "projects/g%2Fp/merge_requests/789/notes"
        )

    @patch(_PATCH_PATH)
    def test_add_mr_note_body_is_sent_to_api(self, mock_run: MagicMock) -> None:
        """The note body is included in the glab command arguments."""
        mock_run.return_value = json.dumps({"id": 99, "noteable_iid": 3})
        handler = _make_handler()

        handler.add_mr_note("456", "my mr note")

        call_args = mock_run.call_args[0][0]
        assert any("my mr note" in arg for arg in call_args)

    def test_add_mr_note_invalid_ref(self) -> None:
        """Non-numeric, non-URL reference raises ValueError with the ref in the message."""
        handler = _make_handler()

        with pytest.raises(ValueError, match="garbage"):
            handler.add_mr_note("garbage", "hello")

    def test_add_mr_note_url_non_numeric_iid_raises(self) -> None:
        """URL with non-numeric MR iid raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_mr_note(
                "https://gitlab.com/g/p/-/merge_requests/abc", "hello"
            )

    def test_add_mr_note_url_empty_iid_raises(self) -> None:
        """URL with trailing slash and no iid raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_mr_note(
                "https://gitlab.com/g/p/-/merge_requests/", "hello"
            )

    def test_add_mr_note_url_no_project_segment_raises(self) -> None:
        """URL with no group/project path (bare host) raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_mr_note(
                "https://gitlab.com/-/merge_requests/123", "hello"
            )

    def test_add_mr_note_invalid_url_format(self) -> None:
        """URL with no '/-/merge_requests/' segment raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_mr_note(
                "https://gitlab.com/group/project/-/issues/5", "hello"
            )

    def test_add_mr_note_empty_body(self) -> None:
        """Empty body raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_mr_note("456", "")

    def test_add_mr_note_whitespace_body(self) -> None:
        """Whitespace-only body raises ValueError."""
        handler = _make_handler()

        with pytest.raises(ValueError):
            handler.add_mr_note("456", "   ")


# ---------------------------------------------------------------------------
# dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    """Tests for NoteHandler dry-run behaviour."""

    @patch(_PATCH_PATH)
    def test_dry_run_issue_note_does_not_call_api(self, mock_run: MagicMock) -> None:
        """Dry-run for issue note skips the API call."""
        handler = _make_handler(dry_run=True)

        handler.add_issue_note("123", "hello")

        mock_run.assert_not_called()

    @patch("builtins.print")
    @patch(_PATCH_PATH)
    def test_dry_run_issue_note_prints_expected_content(
        self, mock_run: MagicMock, mock_print: MagicMock
    ) -> None:
        """Dry-run for issue note prints the endpoint and body."""
        handler = _make_handler(dry_run=True)

        handler.add_issue_note("123", "hello")

        printed = " ".join(str(call) for call in mock_print.call_args_list)
        assert "dry-run" in printed
        assert "issues/123/notes" in printed
        assert "hello" in printed

    @patch(_PATCH_PATH)
    def test_dry_run_mr_note_does_not_call_api(self, mock_run: MagicMock) -> None:
        """Dry-run for MR note skips the API call."""
        handler = _make_handler(dry_run=True)

        handler.add_mr_note("!456", "lgtm")

        mock_run.assert_not_called()

    @patch("builtins.print")
    @patch(_PATCH_PATH)
    def test_dry_run_mr_note_prints_expected_content(
        self, mock_run: MagicMock, mock_print: MagicMock
    ) -> None:
        """Dry-run for MR note prints the endpoint and body."""
        handler = _make_handler(dry_run=True)

        handler.add_mr_note("!456", "lgtm")

        printed = " ".join(str(call) for call in mock_print.call_args_list)
        assert "dry-run" in printed
        assert "merge_requests/456/notes" in printed
        assert "lgtm" in printed


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for NoteHandler error paths."""

    @patch(_PATCH_PATH)
    def test_post_note_non_json_response_raises_platform_error(
        self, mock_run: MagicMock
    ) -> None:
        """Non-JSON glab output raises PlatformError."""
        mock_run.return_value = "<html>error</html>"
        handler = _make_handler()

        with pytest.raises(PlatformError):
            handler.add_issue_note("123", "hello")

    @patch(_PATCH_PATH)
    def test_post_note_empty_response_raises_platform_error(
        self, mock_run: MagicMock
    ) -> None:
        """Empty glab response raises PlatformError."""
        mock_run.return_value = ""
        handler = _make_handler()

        with pytest.raises(PlatformError):
            handler.add_issue_note("123", "hello")

    @patch(_PATCH_PATH)
    def test_post_note_truncated_json_raises_platform_error(
        self, mock_run: MagicMock
    ) -> None:
        """Truncated JSON response raises PlatformError."""
        mock_run.return_value = '{"id":'
        handler = _make_handler()

        with pytest.raises(PlatformError):
            handler.add_issue_note("123", "hello")


# ---------------------------------------------------------------------------
# CLI dispatch (cmd_note)
# ---------------------------------------------------------------------------


class TestCmdNote:
    """Tests for cmd_note CLI dispatch."""

    @patch("projctl.cli.Config")
    @patch("projctl.cli.NoteHandler")
    def test_cmd_note_returns_0_on_success(
        self, mock_handler_cls: MagicMock, mock_config_cls: MagicMock
    ) -> None:
        """cmd_note returns 0 when the handler succeeds."""
        import argparse

        from projctl.cli import cmd_note

        mock_config_cls.return_value.platform = "gitlab"
        mock_handler_cls.return_value.add_issue_note.return_value = None
        args = argparse.Namespace(
            resource_type="issue",
            reference="123",
            body="hello",
            dry_run=False,
            config=None,
        )

        result = cmd_note(args)

        assert result == 0
        mock_handler_cls.return_value.add_issue_note.assert_called_once_with("123", "hello")
        mock_handler_cls.return_value.add_mr_note.assert_not_called()

    @patch("projctl.cli.Config")
    @patch("projctl.cli.NoteHandler")
    def test_cmd_note_returns_1_on_value_error(
        self, mock_handler_cls: MagicMock, mock_config_cls: MagicMock
    ) -> None:
        """cmd_note returns 1 when the handler raises ValueError."""
        import argparse

        from projctl.cli import cmd_note

        mock_config_cls.return_value.platform = "gitlab"
        mock_handler_cls.return_value.add_issue_note.side_effect = ValueError("bad ref")
        args = argparse.Namespace(
            resource_type="issue",
            reference="garbage",
            body="hello",
            dry_run=False,
            config=None,
        )

        result = cmd_note(args)

        assert result == 1

    @patch("projctl.cli.Config")
    @patch("projctl.cli.NoteHandler")
    def test_cmd_note_returns_1_on_platform_error(
        self, mock_handler_cls: MagicMock, mock_config_cls: MagicMock
    ) -> None:
        """cmd_note returns 1 when the handler raises PlatformError."""
        import argparse

        from projctl.cli import cmd_note

        mock_config_cls.return_value.platform = "gitlab"
        mock_handler_cls.return_value.add_issue_note.side_effect = PlatformError("api failure")
        args = argparse.Namespace(
            resource_type="issue",
            reference="123",
            body="hello",
            dry_run=False,
            config=None,
        )

        result = cmd_note(args)

        assert result == 1

    @patch("projctl.cli.Config")
    @patch("projctl.cli.NoteHandler")
    def test_cmd_note_mr_dispatch(
        self, mock_handler_cls: MagicMock, mock_config_cls: MagicMock
    ) -> None:
        """cmd_note dispatches to add_mr_note when resource_type is 'mr'."""
        import argparse

        from projctl.cli import cmd_note

        mock_config_cls.return_value.platform = "gitlab"
        mock_handler_cls.return_value.add_mr_note.return_value = None
        args = argparse.Namespace(
            resource_type="mr",
            reference="!456",
            body="lgtm",
            dry_run=False,
            config=None,
        )

        result = cmd_note(args)

        assert result == 0
        mock_handler_cls.return_value.add_mr_note.assert_called_once_with("!456", "lgtm")
        mock_handler_cls.return_value.add_issue_note.assert_not_called()

    @patch("projctl.cli.Config")
    @patch("projctl.cli.NoteHandler")
    def test_cmd_note_returns_1_for_github_platform(
        self, mock_handler_cls: MagicMock, mock_config_cls: MagicMock
    ) -> None:
        """cmd_note returns 1 with an error for non-GitLab platforms."""
        import argparse

        from projctl.cli import cmd_note

        mock_config_cls.return_value.platform = "github"
        args = argparse.Namespace(
            resource_type="issue",
            reference="123",
            body="hello",
            dry_run=False,
            config=None,
        )

        result = cmd_note(args)

        assert result == 1
        mock_handler_cls.assert_not_called()

    @patch("projctl.cli.Config")
    @patch("projctl.cli.NoteHandler")
    def test_cmd_note_propagates_dry_run(
        self, mock_handler_cls: MagicMock, mock_config_cls: MagicMock
    ) -> None:
        """cmd_note passes dry_run=True to the handler constructor."""
        import argparse

        from projctl.cli import cmd_note

        mock_config_cls.return_value.platform = "gitlab"
        mock_handler_cls.return_value.add_issue_note.return_value = None
        args = argparse.Namespace(
            resource_type="issue",
            reference="123",
            body="hello",
            dry_run=True,
            config=None,
        )

        cmd_note(args)

        _, kwargs = mock_handler_cls.call_args
        assert kwargs.get("dry_run") is True or mock_handler_cls.call_args[0][1] is True
