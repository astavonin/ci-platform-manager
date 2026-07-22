"""Tests for projctl.handlers.github_creator module."""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock, call, patch

import pytest
import yaml

from projctl.config import Config
from projctl.exceptions import PlatformError
from projctl.handlers.github_creator import GithubIssueCreator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_DESCRIPTION = "# Description\n\nSome details.\n\n# Acceptance Criteria\n\n- AC1"


def _make_config(tmp_path: Path) -> Config:
    """Write a GitHub config file and return a Config object."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "platform: github\n"
        "github:\n"
        "  repo: owner/test-repo\n"
        "common:\n"
        "  issue_template:\n"
        "    required_sections:\n"
        "      - Description\n"
        "      - Acceptance Criteria\n"
    )
    return Config(cfg_path)


def _write_yaml(path: Path, data: Dict[str, Any]) -> Path:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    return path


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    """Dry-run mode makes zero subprocess calls."""

    @patch("subprocess.run")
    def test_create_issue_dry_run(self, mock_run: Mock, tmp_path: Path) -> None:
        """No subprocess calls in dry-run mode."""
        config = _make_config(tmp_path)
        creator = GithubIssueCreator(config, dry_run=True)

        yaml_path = _write_yaml(
            tmp_path / "issues.yaml",
            {
                "issues": [
                    {
                        "title": "Dry Issue",
                        "description": _VALID_DESCRIPTION,
                    }
                ]
            },
        )

        creator.process_yaml_file(yaml_path)

        mock_run.assert_not_called()
        assert len(creator.created_issues) == 1


# ---------------------------------------------------------------------------
# Minimal creation
# ---------------------------------------------------------------------------


class TestCreateIssueMinimal:
    """Correct gh issue create command constructed for a minimal issue."""

    @patch("subprocess.run")
    def test_create_issue_minimal(self, mock_run: Mock, tmp_path: Path) -> None:
        """gh issue create called with title and body flags."""
        mock_run.return_value = Mock(
            stdout="https://github.com/owner/test-repo/issues/1",
            stderr="",
            returncode=0,
        )

        config = _make_config(tmp_path)
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues.yaml",
            {
                "issues": [
                    {
                        "title": "Minimal Issue",
                        "description": _VALID_DESCRIPTION,
                    }
                ]
            },
        )

        creator.process_yaml_file(yaml_path)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"
        assert "issue" in cmd
        assert "create" in cmd
        assert "--title" in cmd
        assert "Minimal Issue" in cmd
        assert "--body" in cmd


# ---------------------------------------------------------------------------
# All fields
# ---------------------------------------------------------------------------


class TestCreateIssueAllFields:
    """All supported YAML fields appear as correct CLI flags."""

    @patch("subprocess.run")
    def test_create_issue_with_all_fields(self, mock_run: Mock, tmp_path: Path) -> None:
        """title, body, labels, assignee, milestone number all present in command."""
        milestones_response = json.dumps([{"title": "v1.0", "number": 7}])

        def side_effect(cmd, **kwargs):
            # First call: list milestones; second call: create issue
            joined = " ".join(cmd)
            if "milestones" in joined and "POST" not in joined:
                return Mock(stdout=milestones_response, stderr="", returncode=0)
            return Mock(
                stdout="https://github.com/owner/test-repo/issues/2",
                stderr="",
                returncode=0,
            )

        mock_run.side_effect = side_effect

        config = _make_config(tmp_path)
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues.yaml",
            {
                "issues": [
                    {
                        "title": "Full Issue",
                        "description": _VALID_DESCRIPTION,
                        "labels": ["enhancement", "good first issue"],
                        "assignee": "octocat",
                        "milestone": "v1.0",
                    }
                ]
            },
        )

        creator.process_yaml_file(yaml_path)

        # Find the issue create call
        create_call = None
        for c in mock_run.call_args_list:
            if "issue" in c[0][0] and "create" in c[0][0]:
                create_call = c
                break

        assert create_call is not None
        cmd = create_call[0][0]
        assert "--label" in cmd
        assert "enhancement" in cmd
        assert "--assignee" in cmd
        assert "octocat" in cmd
        assert "--milestone" in cmd
        # gh issue create --milestone accepts a title, not a number
        assert "v1.0" in cmd


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    """Issues with dependencies are created after their dependencies."""

    @patch("subprocess.run")
    def test_topological_sort_respects_dependencies(self, mock_run: Mock, tmp_path: Path) -> None:
        """Issues with 'dependencies' field are created after their prerequisites."""
        call_order = []

        def side_effect(cmd, **kwargs):
            if "create" in cmd:
                # Extract the title from the --title flag
                try:
                    title_idx = cmd.index("--title")
                    call_order.append(cmd[title_idx + 1])
                except (ValueError, IndexError):
                    pass
            return Mock(
                stdout="https://github.com/owner/test-repo/issues/1",
                stderr="",
                returncode=0,
            )

        mock_run.side_effect = side_effect

        config = _make_config(tmp_path)
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues.yaml",
            {
                "issues": [
                    {
                        "id": "second",
                        "title": "Second Issue",
                        "description": _VALID_DESCRIPTION,
                        "dependencies": ["first"],
                    },
                    {
                        "id": "first",
                        "title": "First Issue",
                        "description": _VALID_DESCRIPTION,
                    },
                ]
            },
        )

        creator.process_yaml_file(yaml_path)

        assert call_order.index("First Issue") < call_order.index("Second Issue")


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    """Dependency cycles are detected before any API calls."""

    @patch("subprocess.run")
    def test_cycle_detection_raises_before_api_calls(self, mock_run: Mock, tmp_path: Path) -> None:
        """ValueError raised for cycles; no subprocess calls made."""
        config = _make_config(tmp_path)
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues.yaml",
            {
                "issues": [
                    {
                        "id": "a",
                        "title": "Issue A",
                        "description": _VALID_DESCRIPTION,
                        "dependencies": ["b"],
                    },
                    {
                        "id": "b",
                        "title": "Issue B",
                        "description": _VALID_DESCRIPTION,
                        "dependencies": ["a"],
                    },
                ]
            },
        )

        with pytest.raises(ValueError, match="Dependency cycle detected"):
            creator.process_yaml_file(yaml_path)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Required section validation
# ---------------------------------------------------------------------------


class TestRequiredSectionValidation:
    """Description missing required sections raises ValueError."""

    @patch("subprocess.run")
    def test_required_section_validation_fails(self, mock_run: Mock, tmp_path: Path) -> None:
        """ValueError raised when Acceptance Criteria section is absent."""
        config = _make_config(tmp_path)
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues.yaml",
            {
                "issues": [
                    {
                        "title": "Bad Issue",
                        "description": "# Description\n\nOnly description, no AC.",
                    }
                ]
            },
        )

        with pytest.raises(ValueError, match="missing required sections"):
            creator.process_yaml_file(yaml_path)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Milestone caching
# ---------------------------------------------------------------------------


class TestMilestoneCaching:
    """Milestone API called only once for multiple issues sharing a milestone."""

    @patch("subprocess.run")
    def test_milestone_resolved_once_for_multiple_issues(
        self, mock_run: Mock, tmp_path: Path
    ) -> None:
        """API milestone list fetched exactly once even with two issues on same milestone."""
        milestones_response = json.dumps([{"title": "v2.0", "number": 3}])

        def side_effect(cmd, **kwargs):
            joined = " ".join(cmd)
            if "milestones" in joined:
                return Mock(stdout=milestones_response, stderr="", returncode=0)
            return Mock(
                stdout="https://github.com/owner/test-repo/issues/1",
                stderr="",
                returncode=0,
            )

        mock_run.side_effect = side_effect

        config = _make_config(tmp_path)
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues.yaml",
            {
                "issues": [
                    {
                        "title": "Issue Alpha",
                        "description": _VALID_DESCRIPTION,
                        "milestone": "v2.0",
                    },
                    {
                        "title": "Issue Beta",
                        "description": _VALID_DESCRIPTION,
                        "milestone": "v2.0",
                    },
                ]
            },
        )

        creator.process_yaml_file(yaml_path)

        milestone_calls = [c for c in mock_run.call_args_list if "milestones" in " ".join(c[0][0])]
        assert (
            len(milestone_calls) == 1
        ), f"Expected 1 milestone API call, got {len(milestone_calls)}"


# ---------------------------------------------------------------------------
# M3: OR group validation fires before any subprocess call
# ---------------------------------------------------------------------------


class TestOrGroupValidationBeforeSubprocess:
    """Ensure OR group validation fires before any subprocess call (M3)."""

    def _config_with_or_groups(self, tmp_path: Path) -> Config:
        cfg_path = tmp_path / "config_org.yaml"
        cfg_path.write_text(
            "platform: github\n"
            "github:\n"
            "  repo: owner/repo\n"
            "  labels:\n"
            "    default:\n"
            "      - - type::feature\n"
            "        - type::bug\n"
            "common:\n"
            "  issue_template:\n"
            "    required_sections:\n"
            "      - Description\n"
            "      - Acceptance Criteria\n"
        )
        return Config(cfg_path)

    @patch("subprocess.run")
    def test_missing_or_group_raises_before_gh(self, mock_run: Mock, tmp_path: Path) -> None:
        """ValueError from OR group validation fires before any gh subprocess call."""
        config = self._config_with_or_groups(tmp_path)
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues_no_group.yaml",
            {
                "issues": [
                    {
                        "title": "Missing group label",
                        "description": _VALID_DESCRIPTION,
                        # no type::feature / type::bug — OR group not satisfied
                        "labels": ["development-status::backlog"],
                    }
                ]
            },
        )

        with pytest.raises(ValueError, match="Missing required label"):
            creator.process_yaml_file(yaml_path)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# M4: allowed-label validation fires before any subprocess call
# ---------------------------------------------------------------------------


class TestAllowedLabelValidationBeforeSubprocess:
    """Ensure allowed-label validation fires before any subprocess call."""

    def _config_with_allowed(self, tmp_path: Path) -> Config:
        cfg_path = tmp_path / "config_allowed.yaml"
        cfg_path.write_text(
            "platform: github\n"
            "github:\n"
            "  repo: owner/repo\n"
            "  labels:\n"
            "    allowed:\n"
            "      - type::feature\n"
            "      - type::bug\n"
            "common:\n"
            "  issue_template:\n"
            "    required_sections:\n"
            "      - Description\n"
            "      - Acceptance Criteria\n"
        )
        return Config(cfg_path)

    @patch("subprocess.run")
    def test_unknown_label_raises_before_gh(self, mock_run: Mock, tmp_path: Path) -> None:
        """ValueError from allowlist validation fires before any gh subprocess call."""
        config = self._config_with_allowed(tmp_path)
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues_bad_label.yaml",
            {
                "issues": [
                    {
                        "title": "Bad label",
                        "description": _VALID_DESCRIPTION,
                        "labels": ["Infra & DevOps"],  # not in allowed list
                    }
                ]
            },
        )

        with pytest.raises(ValueError, match="Infra & DevOps"):
            creator.process_yaml_file(yaml_path)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# GitHub required-field validation for issues
# ---------------------------------------------------------------------------


class TestGithubRequiredIssueFields:
    """GitHub platform: required-field validation is platform-aware."""

    def _make_config_with_required_fields(self, tmp_path: Path, required_fields: list) -> Config:
        """Write a GitHub config with explicit required_fields and return Config."""
        cfg_path = tmp_path / "config_rf.yaml"
        cfg_path.write_text(
            "platform: github\n"
            "github:\n"
            "  repo: owner/test-repo\n"
            "common:\n"
            "  issue_template:\n"
            "    required_sections:\n"
            "      - Description\n"
            "      - Acceptance Criteria\n"
            f"    required_fields: {required_fields!r}\n"
        )
        return Config(cfg_path)

    @patch("subprocess.run")
    def test_github_default_no_weight_requirement(self, mock_run: Mock, tmp_path: Path) -> None:
        """GitHub default config (no required_fields key) → no weight check → no exception."""
        # Arrange — default _make_config has no required_fields at all
        config = _make_config(tmp_path)
        creator = GithubIssueCreator(config, dry_run=True)

        yaml_path = _write_yaml(
            tmp_path / "issues_no_weight.yaml",
            {
                "issues": [
                    {
                        "title": "No Weight GitHub Issue",
                        "description": _VALID_DESCRIPTION,
                        # weight intentionally absent
                    }
                ]
            },
        )

        # Act — should not raise (GitHub default: required_fields=[])
        creator.process_yaml_file(yaml_path)

        assert len(creator.created_issues) == 1

    @patch("subprocess.run")
    def test_github_explicit_weight_required_raises(self, mock_run: Mock, tmp_path: Path) -> None:
        """GitHub config with explicit required_fields=['weight'] → ValueError before subprocess."""
        # Arrange
        config = self._make_config_with_required_fields(tmp_path, ["weight"])
        creator = GithubIssueCreator(config, dry_run=False)

        yaml_path = _write_yaml(
            tmp_path / "issues_gh_weight.yaml",
            {
                "issues": [
                    {
                        "title": "No Weight GitHub Issue",
                        "description": _VALID_DESCRIPTION,
                        # weight absent — but now explicitly required
                    }
                ]
            },
        )

        # Act / Assert
        with pytest.raises(ValueError, match="missing required 'weight' field"):
            creator.process_yaml_file(yaml_path)

        mock_run.assert_not_called()
