"""Tests for projctl.utils.glab_runner module."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from projctl.exceptions import PlatformError
from projctl.utils.glab_runner import (
    run_glab_command,
    run_glab_command_binary,
    run_glab_command_status,
    DRY_RUN,
    discussion_resolve_endpoint,
    run_glab_json,
    run_glab_json_pages,
    stream_glab_command_to_file,
)

_NOT_FOUND_MSG = "glab command not found. Please install glab CLI."


class TestRunGlabCommand:
    """run_glab_command delegates to the text runner."""

    @patch("projctl.utils.glab_runner.run_cli_command")
    def test_delegates_with_glab_binary(self, mock_run: Mock) -> None:
        """Passes 'glab' as the binary and returns the runner's string output."""
        mock_run.return_value = "output"

        result = run_glab_command(["api", "endpoint"])

        assert result == "output"
        mock_run.assert_called_once_with("glab", ["api", "endpoint"], _NOT_FOUND_MSG)


class TestRunGlabCommandBinary:
    """run_glab_command_binary delegates to the binary-safe runner."""

    @patch("projctl.utils.glab_runner.run_cli_command_binary")
    def test_delegates_to_binary_runner(self, mock_run_binary: Mock) -> None:
        """Routes to run_cli_command_binary, not run_cli_command.

        Which runner this wraps is the whole point of the function: the text
        runner hardcodes text=True and would corrupt or raise on a non-UTF-8
        artifact payload, so a delegation slip here is silent data corruption.
        """
        payload = bytes(range(256))
        mock_run_binary.return_value = payload

        result = run_glab_command_binary(["api", "endpoint"])

        assert result == payload
        mock_run_binary.assert_called_once_with("glab", ["api", "endpoint"], _NOT_FOUND_MSG)


class TestRunGlabCommandStatus:
    """run_glab_command_status delegates to the status-returning runner."""

    @patch("projctl.utils.glab_runner.run_cli_command_status")
    def test_delegates_to_status_runner(self, mock_run_status: Mock) -> None:
        """Routes to run_cli_command_status, not run_cli_command.

        Which runner this wraps is the point: the plain runner raises on a
        non-zero exit and keeps only stderr, so a delegation slip here turns an
        invalid-config report into a stack trace with the report discarded.
        """
        mock_run_status.return_value = (1, "config is invalid", "")

        result = run_glab_command_status(["ci", "lint"])

        assert result == (1, "config is invalid", "")
        mock_run_status.assert_called_once_with("glab", ["ci", "lint"], _NOT_FOUND_MSG)


class TestStreamGlabCommandToFile:
    """stream_glab_command_to_file delegates to the streaming runner."""

    @patch("projctl.utils.glab_runner.stream_cli_command_to_file")
    def test_delegates_with_destination(self, mock_stream: Mock, tmp_path: Path) -> None:
        """Forwards the destination path through to the streaming runner."""
        dest = tmp_path / "artifacts.zip"

        stream_glab_command_to_file(["api", "endpoint"], dest)

        mock_stream.assert_called_once_with("glab", ["api", "endpoint"], dest, _NOT_FOUND_MSG)


class TestRunGlabJson:
    """run_glab_json centralises the preview / execute / parse sequence."""

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_parses_json_response(self, mock_run: Mock) -> None:
        """A JSON body is returned already decoded."""
        mock_run.return_value = '{"id": 7, "name": "x"}'

        assert run_glab_json(["api", "endpoint"]) == {"id": 7, "name": "x"}

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_dry_run_skips_the_call(self, mock_run: Mock) -> None:
        """Dry run must make no API call; DRY_RUN signals 'nothing executed'."""
        assert run_glab_json(["api", "endpoint"], dry_run=True) is DRY_RUN
        mock_run.assert_not_called()

    @patch("builtins.print")
    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_dry_run_previews_the_command(self, mock_run: Mock, mock_print: Mock) -> None:
        """The preview names the binary and the endpoint so it can be pasted."""
        run_glab_json(["api", "-X", "PUT", "endpoint"], dry_run=True)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        assert "glab api -X PUT endpoint" in printed

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_non_json_raises_platform_error(self, mock_run: Mock) -> None:
        """A gateway error page must surface as PlatformError, not a JSON error."""
        mock_run.return_value = "<html>502</html>"

        with pytest.raises(PlatformError, match="Unexpected glab response"):
            run_glab_json(["api", "endpoint"])


class TestRunGlabJsonPages:
    """run_glab_json_pages merges the concatenated arrays --paginate emits."""

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_single_page_returned_as_list(self, mock_run: Mock) -> None:
        """One array parses directly."""
        mock_run.return_value = '[{"id": 1}, {"id": 2}]'

        assert run_glab_json_pages(["api", "--paginate", "e"]) == [{"id": 1}, {"id": 2}]

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_concatenated_pages_are_merged_in_order(self, mock_run: Mock) -> None:
        """Items from later pages must survive; dropping them looks like a short list."""
        mock_run.return_value = '[{"id": 1}]\n[{"id": 2}, {"id": 3}]'

        result = run_glab_json_pages(["api", "--paginate", "e"])

        assert [d["id"] for d in result] == [1, 2, 3]

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_empty_array_returns_empty_list(self, mock_run: Mock) -> None:
        """No results is a valid response, not an error."""
        mock_run.return_value = "[]"

        assert run_glab_json_pages(["api", "--paginate", "e"]) == []

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_non_array_json_raises(self, mock_run: Mock) -> None:
        """An object where an array was expected is an API contract break."""
        mock_run.return_value = '{"message": "403 Forbidden"}'

        with pytest.raises(PlatformError, match="Expected a JSON array"):
            run_glab_json_pages(["api", "--paginate", "e"])

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_garbage_raises_platform_error(self, mock_run: Mock) -> None:
        """Unparseable output surfaces as PlatformError."""
        mock_run.return_value = "not json at all"

        with pytest.raises(PlatformError, match="Unexpected glab response"):
            run_glab_json_pages(["api", "--paginate", "e"])


class TestRunGlabJsonPagesGuards:
    """Failure modes that would otherwise read as an empty resource."""

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_empty_output_raises(self, mock_run: Mock) -> None:
        """Empty stdout must not render as a confident '0 discussion(s)'."""
        mock_run.return_value = "   \n"

        with pytest.raises(PlatformError, match="Empty response"):
            run_glab_json_pages(["api", "--paginate", "e"])

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_error_object_on_a_later_page_raises(self, mock_run: Mock) -> None:
        """Page 2 coming back as an error object is not a short list."""
        mock_run.return_value = '[{"id": 1}]\n{"message": "500"}'

        with pytest.raises(PlatformError, match="Expected a JSON array page"):
            run_glab_json_pages(["api", "--paginate", "e"])


class TestDiscussionResolveEndpoint:
    """The query-string form is the fix for a recorded 403 defect."""

    def test_resolved_true_goes_in_the_query_string(self) -> None:
        """A JSON body 403s on DiscussionNote threads; the query string does not."""
        assert (
            discussion_resolve_endpoint("projects/:id/merge_requests/2/discussions", "abc")
            == "projects/:id/merge_requests/2/discussions/abc?resolved=true"
        )

    def test_resolved_false_for_unresolve(self) -> None:
        """Reopening uses the same shape with the opposite value."""
        assert discussion_resolve_endpoint(
            "projects/:id/merge_requests/2/discussions", "abc", resolved=False
        ).endswith("/abc?resolved=false")


class TestDryRunSentinel:
    """DRY_RUN must be distinguishable from a genuine JSON null."""

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_dry_run_returns_sentinel_not_none(self, mock_run: Mock) -> None:
        """A caller keying on None would treat a null response as a preview."""
        assert run_glab_json(["api", "e"], dry_run=True) is DRY_RUN

    @patch("projctl.utils.glab_runner.run_glab_command")
    def test_json_null_returns_none_not_sentinel(self, mock_run: Mock) -> None:
        """A real null response stays None so callers can reject it."""
        mock_run.return_value = "null"

        result = run_glab_json(["api", "e"])

        assert result is None
        assert result is not DRY_RUN
