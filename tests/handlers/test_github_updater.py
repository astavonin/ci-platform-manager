"""Tests for projctl.handlers.github_updater module."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from projctl.config import Config
from projctl.handlers.github_updater import GithubUpdater


def _config_with_allowed(tmp_path: Path) -> Config:
    """Write a GitHub config with a labels.allowed list and return a Config."""
    cfg_path = tmp_path / "config_allowed.yaml"
    cfg_path.write_text(
        "platform: github\n"
        "github:\n"
        "  repo: owner/repo\n"
        "  labels:\n"
        "    allowed:\n"
        "      - type::feature\n"
        "      - type::bug\n"
    )
    return Config(cfg_path)


def _config_without_allowed(tmp_path: Path) -> Config:
    """Write a GitHub config that omits labels.allowed → validation is a no-op."""
    cfg_path = tmp_path / "config_no_allowed.yaml"
    cfg_path.write_text("platform: github\n" "github:\n" "  repo: owner/repo\n")
    return Config(cfg_path)


class TestUpdateIssueLabelValidation:
    """GithubUpdater.update_issue rejects labels not in the configured allowed list."""

    @patch("subprocess.run")
    def test_unknown_add_label_raises_before_gh(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """--add-label with a value not in allowed → ValueError before any gh call."""
        config = _config_with_allowed(tmp_path)
        updater = GithubUpdater(config, dry_run=False)

        with pytest.raises(ValueError, match="Infra & DevOps"):
            updater.update_issue("42", labels_add=["Infra & DevOps"])

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_known_add_label_passes_validation(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """--add-label with a value in allowed → no exception (dry-run so no subprocess)."""
        config = _config_with_allowed(tmp_path)
        updater = GithubUpdater(config, dry_run=True)

        # Act / Assert — validation must not raise for an allowed label
        updater.update_issue("42", labels_add=["type::feature"])

        # dry_run=True prints commands rather than invoking gh
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_no_allowed_list_configured_any_label_passes(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """No labels.allowed key → validation is a no-op regardless of label content."""
        config = _config_without_allowed(tmp_path)
        updater = GithubUpdater(config, dry_run=True)

        # Act / Assert — no exception even with arbitrary label
        updater.update_issue("42", labels_add=["arbitrary::whatever"])
        mock_run.assert_not_called()


class TestUpdatePrLabelValidation:
    """GithubUpdater.update_pr rejects labels not in the configured allowed list."""

    @patch("subprocess.run")
    def test_unknown_add_label_raises_before_gh(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """--add-label on a PR with a value not in allowed → ValueError before any gh call."""
        config = _config_with_allowed(tmp_path)
        updater = GithubUpdater(config, dry_run=False)

        with pytest.raises(ValueError, match="Infra & DevOps"):
            updater.update_pr("7", labels_add=["Infra & DevOps"])

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_known_add_label_passes_validation(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """--add-label on a PR with a value in allowed → no exception."""
        config = _config_with_allowed(tmp_path)
        updater = GithubUpdater(config, dry_run=True)

        # Act / Assert — validation must not raise for an allowed label
        updater.update_pr("7", labels_add=["type::bug"])
        mock_run.assert_not_called()
