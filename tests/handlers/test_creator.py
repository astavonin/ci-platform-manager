"""Tests for projctl.handlers.creator module."""

import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock, patch

import pytest
import yaml

from projctl.config import Config
from projctl.exceptions import PlatformError
from projctl.handlers.creator import EpicIssueCreator


class TestEpicIssueCreatorInit:
    """Test EpicIssueCreator initialization."""

    def test_init_normal_mode(self, new_config_path: Path) -> None:
        """Creator initializes correctly in normal mode."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=False)

        assert creator.config == config
        assert creator.dry_run is False
        assert creator.group == "test/group"
        assert creator.created_issues == []
        assert creator.issue_id_mapping == {}

    def test_init_dry_run_mode(self, new_config_path: Path) -> None:
        """Creator initializes correctly in dry-run mode."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        assert creator.dry_run is True


class TestCreateEpic:
    """Test epic creation."""

    @patch("subprocess.run")
    def test_create_new_epic(self, mock_run: Mock, new_config_path: Path) -> None:
        """Create new epic via API call."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        # Mock successful epic creation
        mock_run.return_value = Mock(stdout='{"id": 456, "iid": 21}', stderr="", returncode=0)

        epic_config = {
            "title": "Test Epic",
            "description": "# Description\n\nEpic description.",
        }

        result = creator.create_epic(epic_config)

        # Verify glab api was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "glab"
        assert "api" in call_args

    def test_use_existing_epic(self, new_config_path: Path) -> None:
        """Use existing epic ID without API call."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        epic_config = {"id": 21}

        result = creator.create_epic(epic_config)

        assert result == "21"

    def test_epic_missing_required_fields(self, new_config_path: Path) -> None:
        """Epic without id or title raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        epic_config = {"description": "Missing title"}

        with pytest.raises(ValueError, match="must have either 'id' or 'title'"):
            creator.create_epic(epic_config)

    @patch("subprocess.run")
    def test_create_epic_dry_run(self, mock_run: Mock, new_config_path: Path) -> None:
        """Dry run mode doesn't execute commands."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        epic_config = {
            "title": "Test Epic",
            "description": "# Description\n\nEpic description.",
        }

        result = creator.create_epic(epic_config)

        # No actual command execution in dry run
        mock_run.assert_not_called()


class TestCreateIssue:
    """Test issue creation."""

    @patch("subprocess.run")
    def test_create_issue_minimal(self, mock_run: Mock, new_config_path: Path) -> None:
        """Create issue with minimal required fields."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        # Mock successful issue creation
        mock_run.return_value = Mock(
            stdout="https://gitlab.example.com/test/project/-/issues/1", stderr="", returncode=0
        )

        issue_config = {
            "title": "Test Issue",
            "description": "# Description\n\nTest description\n\n# Acceptance Criteria\n\n- AC1",
            "weight": 3,
        }

        creator.create_issue(issue_config, epic_id=None)

        # Calls: (0) glab issue create, (1) glab api PUT labels, (2) glab api PUT weight
        assert mock_run.call_count == 3
        create_args = mock_run.call_args_list[0][0][0]
        assert create_args[0] == "glab"
        assert "issue" in create_args
        assert "create" in create_args

        label_args = mock_run.call_args_list[1][0][0]
        assert "api" in label_args
        assert "-X" in label_args
        assert "PUT" in label_args
        assert any("labels=" in a for a in label_args)

        weight_args = mock_run.call_args_list[2][0][0]
        assert "api" in weight_args
        assert "-X" in weight_args
        assert "PUT" in weight_args
        assert any("weight=3" in a for a in weight_args)

    @patch("subprocess.run")
    def test_create_issue_with_metadata(self, mock_run: Mock, new_config_path: Path) -> None:
        """Create issue with all metadata fields."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        mock_run.return_value = Mock(
            stdout="https://gitlab.example.com/test/project/-/issues/1", returncode=0
        )

        issue_config = {
            "title": "Test Issue",
            "description": "# Description\n\nTest\n\n# Acceptance Criteria\n\n- AC1",
            "labels": ["type::bug"],
            "assignee": "testuser",
            "milestone": "v1.0",
            "due_date": "2026-03-01",
            "weight": 2,
        }

        creator.create_issue(issue_config, epic_id=None)

        # Verify metadata was passed in the create command
        create_args = mock_run.call_args_list[0][0][0]
        assert "--assignee" in create_args
        assert "testuser" in create_args
        assert "--milestone" in create_args
        assert "v1.0" in create_args

        # Labels applied via PUT (call 1), weight via PUT (call 2)
        weight_args = mock_run.call_args_list[2][0][0]
        assert any("weight=2" in a for a in weight_args)

    @patch("subprocess.run")
    def test_create_issue_command_failure(self, mock_run: Mock, new_config_path: Path) -> None:
        """Issue creation failure raises PlatformError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["glab", "issue", "create"], stderr="Error creating issue"
        )

        issue_config = {
            "title": "Test Issue",
            "description": "# Description\n\nTest\n\n# Acceptance Criteria\n\n- AC1",
            "weight": 1,
        }

        with pytest.raises(PlatformError, match="Command failed"):
            creator.create_issue(issue_config, epic_id=None)


class TestLoadYAML:
    """Test YAML file loading."""

    def test_load_valid_yaml(self, new_config_path: Path, sample_issue_yaml_path: Path) -> None:
        """Load valid YAML file successfully in dry-run mode."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        # Test in dry-run mode to avoid complex mocking
        creator.process_yaml_file(sample_issue_yaml_path)

        # In dry-run mode, should track created issues
        assert len(creator.created_issues) == 2

    def test_load_nonexistent_yaml(self, new_config_path: Path, temp_dir: Path) -> None:
        """Loading nonexistent file raises FileNotFoundError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        nonexistent = temp_dir / "nonexistent.yaml"

        with pytest.raises(FileNotFoundError):
            creator.process_yaml_file(nonexistent)

    def test_load_invalid_yaml(self, new_config_path: Path, temp_dir: Path) -> None:
        """Loading invalid YAML raises appropriate error."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        invalid_yaml = temp_dir / "invalid.yaml"
        invalid_yaml.write_text("invalid: yaml: content: [[[")

        with pytest.raises(yaml.YAMLError):
            creator.process_yaml_file(invalid_yaml)


class TestValidation:
    """Test validation methods."""

    def test_validate_labels_success(self, new_config_path: Path) -> None:
        """Valid labels pass validation."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        labels = ["type::feature", "type::bug"]

        # Should not raise
        creator._validate_issue_labels(labels)

    def test_validate_labels_unknown(self, new_config_path: Path) -> None:
        """Unknown labels raise ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        labels = ["unknown::label"]

        with pytest.raises(ValueError, match="Unknown labels"):
            creator._validate_issue_labels(labels)

    def test_validate_description_success(self, new_config_path: Path) -> None:
        """Valid description passes validation."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        issue_config = {
            "title": "Test Issue",
            "description": "# Description\n\nContent\n\n# Acceptance Criteria\n\n- AC1",
        }

        # Should not raise
        creator._validate_issue_description(issue_config)

    def test_validate_description_missing_sections(self, new_config_path: Path) -> None:
        """Description missing required sections raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        issue_config = {
            "title": "Test Issue",
            "description": "# Description\n\nOnly description",
        }

        with pytest.raises(ValueError, match="missing required sections"):
            creator._validate_issue_description(issue_config)

    def test_create_issue_missing_weight_raises(self, new_config_path: Path) -> None:
        """Issue without weight field raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        issue_config = {
            "title": "No Weight Issue",
            "description": "# Description\n\nContent\n\n# Acceptance Criteria\n\n- AC1",
        }

        with pytest.raises(ValueError, match="missing required 'weight' field"):
            creator.create_issue(issue_config, epic_id=None)

    def test_create_issue_negative_weight_raises(self, new_config_path: Path) -> None:
        """Issue with negative weight raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        issue_config = {
            "title": "Bad Weight Issue",
            "description": "# Description\n\nContent\n\n# Acceptance Criteria\n\n- AC1",
            "weight": -1,
        }

        with pytest.raises(ValueError, match="weight must be a non-negative integer"):
            creator.create_issue(issue_config, epic_id=None)

    def test_create_issue_non_integer_weight_raises(self, new_config_path: Path) -> None:
        """Issue with non-integer weight raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        issue_config = {
            "title": "Bad Weight Issue",
            "description": "# Description\n\nContent\n\n# Acceptance Criteria\n\n- AC1",
            "weight": "high",
        }

        with pytest.raises(ValueError, match="weight must be a non-negative integer"):
            creator.create_issue(issue_config, epic_id=None)


class TestDryRun:
    """Test dry-run mode."""

    @patch("subprocess.run")
    def test_dry_run_no_execution(self, mock_run: Mock, new_config_path: Path) -> None:
        """Dry run mode prevents command execution."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        issue_config = {
            "title": "Test Issue",
            "description": "# Description\n\nTest\n\n# Acceptance Criteria\n\n- AC1",
            "weight": 2,
        }

        # Should not raise and should not call subprocess
        creator.create_issue(issue_config, epic_id=None)

        mock_run.assert_not_called()


class TestProcessYamlFileMilestone:
    """Test process_yaml_file with milestone section combinations."""

    def _write_yaml(self, path: Path, data: Dict[str, Any]) -> Path:
        """Write a YAML file to the given path and return it."""
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
        return path

    def test_milestone_only_dry_run(self, new_config_path: Path, temp_dir: Path, capsys) -> None:
        """Milestone-only YAML creates milestone and prints summary in dry-run mode."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        yaml_path = self._write_yaml(
            temp_dir / "milestone_only.yaml",
            {
                "milestone": {
                    "title": "ADAS Model Integration",
                    "description": "Integration of supercombo model",
                    "due_date": "2026-12-31",
                }
            },
        )

        creator.process_yaml_file(yaml_path)

        captured = capsys.readouterr()
        assert "ADAS Model Integration" in captured.out
        assert "DRY_RUN" in captured.out
        # No issues should have been created
        assert creator.created_issues == []

    def test_milestone_with_epic_and_issues_dry_run(
        self, new_config_path: Path, temp_dir: Path, capsys
    ) -> None:
        """Milestone + epic + issues YAML processes all three sections in dry-run mode."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        yaml_path = self._write_yaml(
            temp_dir / "full.yaml",
            {
                "milestone": {
                    "title": "Sprint 1",
                    "due_date": "2026-06-30",
                },
                "epic": {
                    "title": "My Epic",
                    "description": "# Description\n\nEpic overview.",
                },
                "issues": [
                    {
                        "title": "First Issue",
                        "description": ("# Description\n\nDesc\n\n# Acceptance Criteria\n\n- AC1"),
                        "weight": 3,
                    }
                ],
            },
        )

        creator.process_yaml_file(yaml_path)

        captured = capsys.readouterr()
        assert "Sprint 1" in captured.out
        # One issue created
        assert len(creator.created_issues) == 1

    def test_epic_and_issues_without_milestone_unchanged(
        self, new_config_path: Path, sample_issue_yaml_path: Path
    ) -> None:
        """Existing epic+issues YAML without milestone continues to work."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        creator.process_yaml_file(sample_issue_yaml_path)

        assert len(creator.created_issues) == 2

    def test_empty_yaml_raises_value_error(self, new_config_path: Path, temp_dir: Path) -> None:
        """Empty YAML file raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        empty_yaml = temp_dir / "empty.yaml"
        empty_yaml.write_text("")

        with pytest.raises(ValueError, match="empty"):
            creator.process_yaml_file(empty_yaml)

    def test_yaml_with_no_known_sections_raises_value_error(
        self, new_config_path: Path, temp_dir: Path
    ) -> None:
        """YAML with none of the supported keys raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        yaml_path = self._write_yaml(
            temp_dir / "unknown.yaml",
            {"unknown_key": "value"},
        )

        with pytest.raises(ValueError, match="at least one of"):
            creator.process_yaml_file(yaml_path)

    def test_milestone_missing_title_raises_value_error(
        self, new_config_path: Path, temp_dir: Path
    ) -> None:
        """Milestone section without title raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        yaml_path = self._write_yaml(
            temp_dir / "no_title.yaml",
            {"milestone": {"description": "No title here"}},
        )

        with pytest.raises(ValueError, match="title"):
            creator.process_yaml_file(yaml_path)

    def test_epic_only_dry_run_prints_summary(
        self, new_config_path: Path, temp_dir: Path, capsys
    ) -> None:
        """Epic-only YAML creates epic and prints summary in dry-run mode."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        yaml_path = self._write_yaml(
            temp_dir / "epic_only.yaml",
            {"epic": {"title": "Standalone Epic"}},
        )

        creator.process_yaml_file(yaml_path)

        # dry-run suppresses the print (dry_run guard), no issues created
        assert creator.created_issues == []

    @patch("subprocess.run")
    def test_epic_only_new_epic_creates_via_api(
        self, mock_run: Mock, new_config_path: Path, temp_dir: Path, capsys
    ) -> None:
        """Epic-only YAML with a new epic title calls the API and prints result."""
        mock_run.return_value = Mock(stdout='{"id": 100, "iid": 7}', stderr="", returncode=0)
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=False)

        yaml_path = self._write_yaml(
            temp_dir / "epic_new.yaml",
            {"epic": {"title": "Brand New Epic", "description": "# Description\n\nNew epic."}},
        )

        creator.process_yaml_file(yaml_path)

        mock_run.assert_called_once()
        captured = capsys.readouterr()
        assert "7" in captured.out
        assert "Brand New Epic" in captured.out

    def test_epic_only_existing_id_dry_run(self, new_config_path: Path, temp_dir: Path) -> None:
        """Epic-only YAML with an existing epic id succeeds without any API call."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)

        yaml_path = self._write_yaml(
            temp_dir / "epic_existing.yaml",
            {"epic": {"id": 42}},
        )

        creator.process_yaml_file(yaml_path)

        assert creator.created_issues == []

    def test_issues_without_epic_raises_value_error(
        self, new_config_path: Path, temp_dir: Path
    ) -> None:
        """YAML with issues but no epic raises ValueError."""
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)

        yaml_path = self._write_yaml(
            temp_dir / "issues_no_epic.yaml",
            {
                "issues": [
                    {
                        "title": "Orphan Issue",
                        "description": ("# Description\n\nDesc\n\n# Acceptance Criteria\n\n- AC1"),
                    }
                ]
            },
        )

        with pytest.raises(ValueError, match="'issues' but no 'epic'"):
            creator.process_yaml_file(yaml_path)


class TestOrGroupValidationBeforeSubprocess:
    """Ensure OR group validation fires before any subprocess call (M3)."""

    def _config_with_or_groups(self, tmp_path: Path) -> Config:
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            "platform: gitlab\n"
            "gitlab:\n"
            "  default_group: g/p\n"
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

    @patch("projctl.handlers.creator.run_glab_command")
    def test_missing_or_group_raises_before_glab(self, mock_glab: Mock, tmp_path: Path) -> None:
        """ValueError from OR group validation fires before any glab subprocess call."""
        config = self._config_with_or_groups(tmp_path)
        creator = EpicIssueCreator(config, dry_run=False)

        # Issue labels contain no member of the required [type::feature | type::bug] group
        issue_config = {
            "title": "Missing group label",
            "description": "# Description\n\nX\n\n# Acceptance Criteria\n\n- AC1",
            "labels": ["development-status::backlog"],
            "weight": 1,
        }

        with pytest.raises(ValueError, match="Missing required label"):
            creator.create_issue(issue_config, epic_id=None)

        mock_glab.assert_not_called()


class TestCreateEpicDescriptionValidation:
    """Epic description is validated against required sections before any API call."""

    @patch("subprocess.run")
    def test_new_epic_valid_description_subprocess_called(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """New epic with valid description proceeds to API call."""
        # Arrange
        mock_run.return_value = Mock(stdout='{"id": 456, "iid": 21}', stderr="", returncode=0)
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)
        epic_config = {
            "title": "Auth Service Refactor",
            "description": "# Description\n\nRefactor the auth service.",
        }

        # Act
        result = creator.create_epic(epic_config)

        # Assert — subprocess was called (validation passed)
        mock_run.assert_called_once()
        assert result == "21"

    @patch("subprocess.run")
    def test_new_epic_description_missing_section_raises_before_subprocess(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """New epic with description missing required section raises ValueError before subprocess."""
        # Arrange
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)
        epic_config = {
            "title": "Auth Service Refactor",
            "description": "Some text without a section header.",
        }

        # Act / Assert
        with pytest.raises(ValueError, match="Epic"):
            creator.create_epic(epic_config)

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_new_epic_empty_description_raises_before_subprocess(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """New epic with empty description raises ValueError before subprocess."""
        # Arrange
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)
        epic_config = {"title": "Auth Service Refactor", "description": ""}

        # Act / Assert
        with pytest.raises(ValueError, match="Epic"):
            creator.create_epic(epic_config)

        mock_run.assert_not_called()

    def test_existing_epic_id_skips_validation(self, new_config_path: Path) -> None:
        """Existing epic (id: present) returns immediately — no validation, no subprocess."""
        # Arrange
        config = Config(new_config_path)
        creator = EpicIssueCreator(config)
        epic_config = {"id": 42}

        # Act
        result = creator.create_epic(epic_config)

        # Assert — returned the ID directly without touching description
        assert result == "42"

    @patch("subprocess.run")
    def test_dry_run_invalid_description_raises_before_dry_run(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """Dry-run with invalid epic description raises ValueError (validation is pre-dry-run)."""
        # Arrange
        config = Config(new_config_path)
        creator = EpicIssueCreator(config, dry_run=True)
        epic_config = {
            "title": "Auth Service Refactor",
            "description": "No section headers here.",
        }

        # Act / Assert
        with pytest.raises(ValueError, match="Epic"):
            creator.create_epic(epic_config)

        mock_run.assert_not_called()


class TestRequiredIssueFieldsValidation:
    """Config-driven required-field validation fires before any glab subprocess call."""

    def _config_with_required_fields(self, tmp_path: Path, required_fields: list) -> Config:
        """Write a GitLab config with given issue required_fields and return Config."""
        import yaml as _yaml  # local import to avoid shadowing module-level yaml

        cfg_path = tmp_path / "config.yaml"
        with open(cfg_path, "w", encoding="utf-8") as f:
            _yaml.dump(
                {
                    "platform": "gitlab",
                    "gitlab": {
                        "default_group": "g/p",
                        "labels": {
                            "default": ["type::feature", "development-status::backlog"],
                            "allowed": [
                                "type::feature",
                                "type::bug",
                                "development-status::backlog",
                            ],
                        },
                    },
                    "common": {
                        "issue_template": {
                            "required_sections": ["Description", "Acceptance Criteria"],
                            "required_fields": required_fields,
                        }
                    },
                },
                f,
            )
        return Config(cfg_path)

    _VALID_DESCRIPTION = "# Description\n\nContent\n\n# Acceptance Criteria\n\n- AC1"

    @patch("projctl.handlers.creator.run_glab_command")
    def test_missing_weight_required_raises_before_glab(
        self, mock_glab: Mock, tmp_path: Path
    ) -> None:
        """Issue missing weight, required_fields=['weight'] → ValueError before glab called."""
        # Arrange
        config = self._config_with_required_fields(tmp_path, ["weight"])
        creator = EpicIssueCreator(config, dry_run=False)
        issue_config = {
            "title": "No Weight Issue",
            "description": self._VALID_DESCRIPTION,
            # weight intentionally absent
        }

        # Act / Assert
        with pytest.raises(ValueError, match="missing required 'weight' field"):
            creator.create_issue(issue_config, epic_id=None)

        mock_glab.assert_not_called()

    @patch("projctl.handlers.creator.run_glab_command")
    def test_missing_weight_opted_out_no_exception(self, mock_glab: Mock, tmp_path: Path) -> None:
        """Issue missing weight, required_fields=[] (opt-out) → no exception; glab called."""
        # Arrange
        config = self._config_with_required_fields(tmp_path, [])
        creator = EpicIssueCreator(config, dry_run=False)
        issue_config = {
            "title": "No Weight Issue",
            "description": self._VALID_DESCRIPTION,
            # weight absent — but not required
        }
        mock_glab.return_value = "https://gitlab.example.com/g/p/-/issues/1"

        # Act — should not raise
        creator.create_issue(issue_config, epic_id=None)

        # Assert — glab was called (validation passed)
        mock_glab.assert_called()

    @patch("projctl.handlers.creator.run_glab_command")
    def test_weight_zero_is_valid(self, mock_glab: Mock, tmp_path: Path) -> None:
        """Issue with weight=0 → no exception (0 is a valid non-negative integer)."""
        # Arrange
        config = self._config_with_required_fields(tmp_path, ["weight"])
        creator = EpicIssueCreator(config, dry_run=False)
        issue_config = {
            "title": "Zero Weight Issue",
            "description": self._VALID_DESCRIPTION,
            "weight": 0,
        }
        mock_glab.return_value = "https://gitlab.example.com/g/p/-/issues/2"

        # Act — should not raise
        creator.create_issue(issue_config, epic_id=None)

    @patch("projctl.handlers.creator.run_glab_command")
    def test_weight_true_is_rejected(self, mock_glab: Mock, tmp_path: Path) -> None:
        """Issue with weight=True (bool) → ValueError before glab called."""
        # Arrange
        config = self._config_with_required_fields(tmp_path, ["weight"])
        creator = EpicIssueCreator(config, dry_run=False)
        issue_config = {
            "title": "Bool Weight Issue",
            "description": self._VALID_DESCRIPTION,
            "weight": True,
        }

        # Act / Assert
        with pytest.raises(ValueError, match="weight must be a non-negative integer"):
            creator.create_issue(issue_config, epic_id=None)

        mock_glab.assert_not_called()

    @patch("projctl.handlers.creator.run_glab_command")
    def test_valid_weight_eight_no_exception(self, mock_glab: Mock, tmp_path: Path) -> None:
        """Issue with weight=8 → no exception."""
        # Arrange
        config = self._config_with_required_fields(tmp_path, ["weight"])
        creator = EpicIssueCreator(config, dry_run=False)
        issue_config = {
            "title": "Valid Weight Issue",
            "description": self._VALID_DESCRIPTION,
            "weight": 8,
        }
        mock_glab.return_value = "https://gitlab.example.com/g/p/-/issues/3"

        # Act — should not raise
        creator.create_issue(issue_config, epic_id=None)

    def test_create_epic_empty_required_fields_no_exception(self, tmp_path: Path) -> None:
        """create_epic with required_fields=[] in config → no exception (no-op loop smoke test)."""
        # Arrange
        import yaml as _yaml

        cfg_path = tmp_path / "epic_config.yaml"
        with open(cfg_path, "w", encoding="utf-8") as f:
            _yaml.dump(
                {
                    "platform": "gitlab",
                    "gitlab": {"default_group": "g/p"},
                    "common": {
                        "epic_template": {
                            "required_sections": ["Description"],
                            "required_fields": [],
                        }
                    },
                },
                f,
            )
        config = Config(cfg_path)
        creator = EpicIssueCreator(config, dry_run=True)
        epic_config = {
            "title": "Smoke Epic",
            "description": "# Description\n\nEpic description.",
        }

        # Act — should not raise
        creator.create_epic(epic_config)
