"""Tests for projctl.utils.cli_runner module."""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from projctl.exceptions import PlatformError
from projctl.utils.cli_runner import (
    run_cli_command,
    run_cli_command_binary,
    stream_cli_command_to_file,
)


class TestRunCliCommandSuccess:
    """run_cli_command returns stripped stdout on success."""

    @patch("subprocess.run")
    def test_returns_stripped_stdout(self, mock_run: Mock) -> None:
        """Successful command returns stripped stdout."""
        mock_run.return_value = Mock(stdout="  hello world  ", stderr="", returncode=0)

        result = run_cli_command("gh", ["pr", "list"], "gh not found")

        assert result == "hello world"

    @patch("subprocess.run")
    def test_debug_log_emitted(self, mock_run: Mock) -> None:
        """logger.debug is called with the full command."""
        mock_run.return_value = Mock(stdout="output", stderr="", returncode=0)

        with patch("projctl.utils.cli_runner.logger") as mock_logger:
            run_cli_command("gh", ["issue", "list"], "gh not found")
            mock_logger.debug.assert_called_once()
            call_args = mock_logger.debug.call_args[0]
            # The debug message should reference the full command including the binary name
            assert "gh" in str(call_args)


class TestRunCliCommandCalledProcessError:
    """run_cli_command raises PlatformError when the command exits non-zero."""

    @patch("subprocess.run")
    def test_raises_platform_error_with_stderr(self, mock_run: Mock) -> None:
        """CalledProcessError raises PlatformError containing the stderr message."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["gh", "pr", "create"], stderr="authentication required"
        )

        with pytest.raises(PlatformError, match="authentication required"):
            run_cli_command("gh", ["pr", "create"], "gh not found")


class TestRunCliCommandFileNotFound:
    """run_cli_command raises PlatformError when the binary is missing."""

    @patch("subprocess.run")
    def test_raises_platform_error_with_not_found_msg(self, mock_run: Mock) -> None:
        """FileNotFoundError raises PlatformError with the provided not_found_msg."""
        not_found_msg = "gh is not installed. Install from https://cli.github.com"
        mock_run.side_effect = FileNotFoundError("No such file or directory: 'gh'")

        with pytest.raises(PlatformError, match="gh is not installed"):
            run_cli_command("gh", ["pr", "list"], not_found_msg)


class TestRunCliCommandBinary:
    """run_cli_command_binary returns raw stdout bytes without text decoding."""

    @patch("subprocess.run")
    def test_returns_raw_bytes_unmodified(self, mock_run: Mock) -> None:
        """Non-UTF-8 bytes come back byte-for-byte (would raise/corrupt under text=True)."""
        payload = bytes(range(256))
        mock_run.return_value = Mock(stdout=payload, stderr=b"", returncode=0)

        result = run_cli_command_binary("glab", ["api", "endpoint"], "glab not found")

        assert result == payload

    @patch("subprocess.run")
    def test_raises_platform_error_on_failure(self, mock_run: Mock) -> None:
        """CalledProcessError raises PlatformError with decoded stderr."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["glab", "api", "x"], stderr=b"404 Not Found"
        )

        with pytest.raises(PlatformError, match="404 Not Found"):
            run_cli_command_binary("glab", ["api", "x"], "glab not found")

    @patch("subprocess.run")
    def test_raises_platform_error_when_binary_missing(self, mock_run: Mock) -> None:
        """Missing binary raises PlatformError with the provided not_found_msg."""
        mock_run.side_effect = FileNotFoundError("No such file or directory: 'glab'")

        with pytest.raises(PlatformError, match="glab not found"):
            run_cli_command_binary("glab", ["api", "x"], "glab not found")


class TestStreamCliCommandToFile:
    """stream_cli_command_to_file writes raw stdout bytes directly to a file."""

    @patch("subprocess.run")
    def test_binary_content_survives_round_trip(self, mock_run: Mock, tmp_path: Path) -> None:
        """A zip-like byte payload reaches the destination file unmodified.

        Regression guard: if this path were ever rerouted through the
        text=True runner, decoding this payload as UTF-8 would raise (or, with
        errors ignored, silently corrupt it) — so this assertion fails the
        moment someone makes that change.
        """
        payload = b"PK\x03\x04" + bytes(range(256))

        def fake_subprocess_run(cmd, stdout=None, stderr=None, check=None):
            stdout.write(payload)
            return Mock(returncode=0)

        mock_run.side_effect = fake_subprocess_run
        dest = tmp_path / "artifacts.zip"

        stream_cli_command_to_file("glab", ["api", "endpoint"], dest, "glab not found")

        assert dest.read_bytes() == payload

    @patch("subprocess.run")
    def test_raises_platform_error_and_removes_partial_file(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """A failed download reports PlatformError and cleans up the partial file."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["glab", "api", "x"], stderr=b"404 Not Found"
        )
        dest = tmp_path / "artifacts.zip"

        with pytest.raises(PlatformError, match="404 Not Found"):
            stream_cli_command_to_file("glab", ["api", "x"], dest, "glab not found")

        assert not dest.exists()

    @patch("subprocess.run")
    def test_raises_platform_error_when_binary_missing(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """Missing binary raises PlatformError with the provided not_found_msg."""
        mock_run.side_effect = FileNotFoundError("No such file or directory: 'glab'")
        dest = tmp_path / "artifacts.zip"

        with pytest.raises(PlatformError, match="glab not found"):
            stream_cli_command_to_file("glab", ["api", "x"], dest, "glab not found")

        assert not dest.exists()
