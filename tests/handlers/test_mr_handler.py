"""Tests for projctl.handlers.mr_handler module."""

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch

import pytest

from projctl.handlers.mr_handler import cmd_create_mr


def _args(**kwargs) -> SimpleNamespace:
    """Build a minimal args namespace with sensible defaults."""
    defaults = {
        "title": None,
        "description": None,
        "fill": False,
        "draft": False,
        "web": False,
        "assignee": [],
        "reviewer": [],
        "label": [],
        "milestone": None,
        "target_branch": None,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


_VALID_DESCRIPTION = (
    "# Summary\n\nWhat changed.\n\n"
    "# Implementation Details\n\nHow it was done.\n\n"
    "# How It Was Tested\n\nTest approach.\n"
)


class TestCmdCreateMrSuccess:
    """cmd_create_mr returns 0 and calls subprocess for valid input."""

    @patch("subprocess.run")
    def test_valid_title_and_description_returns_0(
        self, mock_run: Mock, make_mr_config
    ) -> None:
        """Valid title and description returns 0 and subprocess is called."""
        # Arrange
        mock_run.return_value = Mock(
            stdout="https://gitlab.com/group/project/-/merge_requests/42",
            stderr="",
            returncode=0,
        )
        args = _args(title="Add feature X", description=_VALID_DESCRIPTION)
        config = make_mr_config()

        # Act
        result = cmd_create_mr(args, config)

        # Assert
        assert result == 0
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_fill_flag_returns_0_subprocess_called(
        self, mock_run: Mock, make_mr_config
    ) -> None:
        """fill=True skips validation and calls subprocess successfully."""
        # Arrange
        mock_run.return_value = Mock(
            stdout="https://gitlab.com/group/project/-/merge_requests/43",
            stderr="",
            returncode=0,
        )
        args = _args(fill=True)
        config = make_mr_config()

        # Act
        result = cmd_create_mr(args, config)

        # Assert
        assert result == 0
        mock_run.assert_called_once()


class TestCmdCreateMrValidationFailure:
    """cmd_create_mr returns 1 and never calls subprocess when validation fails."""

    @patch("subprocess.run")
    def test_missing_title_returns_1(self, mock_run: Mock, make_mr_config) -> None:
        """Missing title without --fill returns 1; subprocess never called."""
        # Arrange
        args = _args(title=None, description=_VALID_DESCRIPTION, fill=False)
        config = make_mr_config()

        # Act
        result = cmd_create_mr(args, config)

        # Assert
        assert result == 1
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_missing_description_returns_1(self, mock_run: Mock, make_mr_config) -> None:
        """Missing description without --fill returns 1; subprocess never called."""
        # Arrange
        args = _args(title="Add feature X", description=None, fill=False)
        config = make_mr_config()

        # Act
        result = cmd_create_mr(args, config)

        # Assert
        assert result == 1
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_description_missing_required_section_returns_1(
        self, mock_run: Mock, make_mr_config
    ) -> None:
        """Description missing required section returns 1; subprocess never called."""
        # Arrange
        incomplete = "# Summary\n\nWhat changed.\n\n# Implementation Details\n\nHow.\n"
        args = _args(title="Add feature X", description=incomplete, fill=False)
        config = make_mr_config()

        # Act
        result = cmd_create_mr(args, config)

        # Assert
        assert result == 1
        mock_run.assert_not_called()


class TestCmdCreateMrDryRun:
    """cmd_create_mr dry-run behaviour."""

    @patch("subprocess.run")
    def test_dry_run_with_invalid_description_returns_1(
        self, mock_run: Mock, make_mr_config
    ) -> None:
        """Dry-run with invalid description returns 1; subprocess never called."""
        # Arrange — validation runs before dry-run branch
        args = _args(title="My MR", description="No sections here", dry_run=True, fill=False)
        config = make_mr_config()

        # Act
        result = cmd_create_mr(args, config)

        # Assert
        assert result == 1
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_dry_run_with_valid_input_returns_0_no_subprocess(
        self, mock_run: Mock, make_mr_config
    ) -> None:
        """Dry-run with valid input returns 0; subprocess never called."""
        # Arrange
        args = _args(title="My MR", description=_VALID_DESCRIPTION, dry_run=True)
        config = make_mr_config()

        # Act
        result = cmd_create_mr(args, config)

        # Assert
        assert result == 0
        mock_run.assert_not_called()


class TestCmdCreateMrDefaultReviewers:
    """Default reviewers from config reach the glab mr create subprocess command."""

    @patch("projctl.handlers.mr_handler.subprocess.run")
    def test_default_reviewer_from_config_appears_in_glab_argv(
        self, mock_run: Mock, make_mr_config
    ) -> None:
        """Default reviewer from config reaches the final glab mr create command."""
        # Arrange
        config = make_mr_config(default_reviewers=["alice"])
        args = _args(title="T", description=_VALID_DESCRIPTION)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Act
        cmd_create_mr(args, config)

        # Assert
        called_cmd = mock_run.call_args[0][0]
        assert "--reviewer" in called_cmd
        idx = called_cmd.index("--reviewer")
        assert called_cmd[idx + 1] == "alice"
