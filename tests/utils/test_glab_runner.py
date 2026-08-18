"""Tests for projctl.utils.glab_runner module."""

from pathlib import Path
from unittest.mock import Mock, patch

from projctl.utils.glab_runner import (
    run_glab_command,
    run_glab_command_binary,
    run_glab_command_status,
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
