"""Tests for projctl.config module."""

import warnings
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
import yaml

from projctl.config import Config, ConfigurationError, config_search_paths


class TestConfigLoading:
    """Test configuration file loading."""

    def test_load_new_format_config(self, new_config_path: Path) -> None:
        """New format config loads without warnings."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # Turn warnings into errors
            config = Config(new_config_path)

        assert config.platform == "gitlab"
        assert config.loaded_config_path == new_config_path

    def test_load_legacy_format_config(self, legacy_config_path: Path) -> None:
        """Legacy format config loads with deprecation warning."""
        with pytest.warns(DeprecationWarning, match="deprecated format"):
            config = Config(legacy_config_path)

        assert config.platform == "gitlab"

    def test_explicit_config_path_not_found(self, temp_dir: Path) -> None:
        """Raises FileNotFoundError when explicit config doesn't exist."""
        nonexistent = temp_dir / "nonexistent.yaml"

        with pytest.raises(FileNotFoundError, match="Config file not found"):
            Config(nonexistent)

    def test_config_search_order(
        self, temp_dir: Path, new_config_data: Dict[str, Any], monkeypatch
    ) -> None:
        """Config search follows correct priority order."""
        # Change to temp dir
        monkeypatch.chdir(temp_dir)

        # Create config in current directory
        local_config = temp_dir / "glab_config.yaml"
        with open(local_config, "w", encoding="utf-8") as file:
            yaml.dump(new_config_data, file)

        with pytest.warns(DeprecationWarning, match="legacy config name"):
            config = Config()

        assert config.loaded_config_path == local_config

    def test_no_config_found_error(self, temp_dir: Path, monkeypatch) -> None:
        """Raises FileNotFoundError when no config found."""
        monkeypatch.chdir(temp_dir)
        # Redirect Path.home() to the temp dir so the user-level config is not found.
        monkeypatch.setattr(Path, "home", lambda: temp_dir)

        with pytest.raises(FileNotFoundError, match="No config file found"):
            Config()


class TestConfigTransformation:
    """Test legacy config transformation."""

    def test_legacy_to_new_transformation(self, legacy_config_path: Path) -> None:
        """Legacy config transforms to new format correctly."""
        with pytest.warns(DeprecationWarning):
            config = Config(legacy_config_path)

        # Check platform is set
        assert config.platform == "gitlab"

        # Check GitLab config preserved
        gitlab_config = config.get_platform_config("gitlab")
        assert gitlab_config["default_group"] == "test/group"
        assert "type::feature" in gitlab_config["labels"]["default"]

    def test_legacy_issue_template_transform(self, legacy_config_path: Path) -> None:
        """Legacy issue_template.sections maps to required_sections."""
        with pytest.warns(DeprecationWarning):
            config = Config(legacy_config_path)

        required = config.get_required_sections()
        assert "Description" in required
        assert "Acceptance Criteria" in required
        # Notes is not required in legacy fixture
        assert "Notes" not in required

    def test_legacy_labels_preserved(self, legacy_config_path: Path) -> None:
        """Label configuration preserved in transformation."""
        with pytest.warns(DeprecationWarning):
            config = Config(legacy_config_path)

        gitlab_config = config.get_platform_config("gitlab")
        assert "type::feature" in gitlab_config["labels"]["default"]
        assert "epic" in gitlab_config["labels"]["default_epic"]
        assert "type::bug" in gitlab_config["labels"]["allowed"]


class TestConfigGetters:
    """Test configuration getter methods."""

    def test_get_platform_config(self, new_config_path: Path) -> None:
        """get_platform_config returns correct platform data."""
        config = Config(new_config_path)

        gitlab_config = config.get_platform_config("gitlab")
        assert gitlab_config["default_group"] == "test/group"

        # Non-existent platform returns empty dict
        unknown_config = config.get_platform_config("unknown")
        assert unknown_config == {}

    def test_get_common_config(self, new_config_path: Path) -> None:
        """get_common_config returns common configuration."""
        config = Config(new_config_path)

        common_config = config.get_common_config()
        assert "issue_template" in common_config
        assert "required_sections" in common_config["issue_template"]

    def test_get_required_sections_new_format(self, new_config_path: Path) -> None:
        """get_required_sections returns sections from new format."""
        config = Config(new_config_path)

        sections = config.get_required_sections()
        assert "Description" in sections
        assert "Acceptance Criteria" in sections

    def test_get_required_sections_legacy_format(self, legacy_config_path: Path) -> None:
        """get_required_sections works with legacy format."""
        with pytest.warns(DeprecationWarning):
            config = Config(legacy_config_path)

        sections = config.get_required_sections()
        assert "Description" in sections
        assert "Acceptance Criteria" in sections

    def test_get_default_group(self, new_config_path: Path) -> None:
        """get_default_group returns GitLab default group."""
        config = Config(new_config_path)

        group = config.get_default_group()
        assert group == "test/group"

    def test_get_default_labels(self, new_config_path: Path) -> None:
        """get_default_labels returns default labels for platform."""
        config = Config(new_config_path)

        labels = config.get_default_labels()
        assert "type::feature" in labels
        assert "development-status::backlog" in labels

    def test_get_default_epic_labels(self, new_config_path: Path) -> None:
        """get_default_epic_labels returns default epic labels."""
        config = Config(new_config_path)

        labels = config.get_default_epic_labels()
        assert "epic" in labels

    def test_get_allowed_labels(self, new_config_path: Path) -> None:
        """get_allowed_labels returns allowed labels list."""
        config = Config(new_config_path)

        allowed = config.get_allowed_labels()
        assert allowed is not None
        assert "type::feature" in allowed
        assert "type::bug" in allowed

    def test_get_allowed_labels_none(self, temp_dir: Path) -> None:
        """get_allowed_labels returns None when not configured."""
        # Create minimal config without allowed labels
        minimal_config = {"platform": "gitlab", "gitlab": {"default_group": "test"}}
        config_path = temp_dir / "minimal.yaml"
        with open(config_path, "w", encoding="utf-8") as file:
            yaml.dump(minimal_config, file)

        config = Config(config_path)
        allowed = config.get_allowed_labels()
        assert allowed is None

    def test_get_default_labels_flat_only(self, temp_dir: Path) -> None:
        """get_default_labels returns only flat strings, excluding OR groups."""
        config_data = {
            "platform": "gitlab",
            "gitlab": {
                "default_group": "test/group",
                "labels": {
                    "default": [
                        ["type::feature", "type::bug"],
                        "development-status::backlog",
                    ],
                },
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = Config(config_path)
        labels = config.get_default_labels()

        assert "development-status::backlog" in labels
        assert "type::feature" not in labels
        assert "type::bug" not in labels

    def test_get_required_label_groups_returns_or_groups(self, temp_dir: Path) -> None:
        """get_required_label_groups returns inner lists, excluding flat strings."""
        config_data = {
            "platform": "gitlab",
            "gitlab": {
                "default_group": "test/group",
                "labels": {
                    "default": [
                        ["type::feature", "type::bug"],
                        "development-status::backlog",
                        ["priority::high", "priority::low"],
                    ],
                },
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = Config(config_path)
        groups = config.get_required_label_groups()

        assert len(groups) == 2
        assert ["type::feature", "type::bug"] in groups
        assert ["priority::high", "priority::low"] in groups

    def test_get_required_label_groups_empty_when_all_flat(self, new_config_path: Path) -> None:
        """get_required_label_groups returns empty list when default has no OR groups."""
        config = Config(new_config_path)
        assert config.get_required_label_groups() == []

    def test_malformed_dict_entry_raises_configuration_error(self, temp_dir: Path) -> None:
        """A dict item in labels.default raises ConfigurationError at access time."""
        config_data = {
            "platform": "gitlab",
            "gitlab": {
                "default_group": "test/group",
                "labels": {"default": [{"type::feature": None}]},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = Config(config_path)
        with pytest.raises(ConfigurationError, match="unexpected type"):
            config.get_default_labels()

    def test_empty_or_group_raises_configuration_error(self, temp_dir: Path) -> None:
        """An empty list item in labels.default raises ConfigurationError."""
        config_data = {
            "platform": "gitlab",
            "gitlab": {
                "default_group": "test/group",
                "labels": {"default": [[], "development-status::backlog"]},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = Config(config_path)
        with pytest.raises(ConfigurationError, match="empty OR group"):
            config.get_default_labels()

    def test_non_string_inner_item_raises_configuration_error(self, temp_dir: Path) -> None:
        """A list whose members include a non-string raises ConfigurationError."""
        config_data = {
            "platform": "gitlab",
            "gitlab": {
                "default_group": "test/group",
                "labels": {"default": [["type::feature", 42]]},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        config = Config(config_path)
        with pytest.raises(ConfigurationError, match="non-string members"):
            config.get_default_labels()


class TestPlatformOverride:
    """Test platform override functionality."""

    def test_platform_override(self, new_config_path: Path) -> None:
        """Platform can be overridden via constructor."""
        config = Config(new_config_path, platform="github")

        assert config.platform == "github"

    def test_platform_from_config(self, new_config_path: Path) -> None:
        """Platform defaults to config value."""
        config = Config(new_config_path)

        assert config.platform == "gitlab"

    def test_platform_default(self, temp_dir: Path) -> None:
        """Platform defaults to 'gitlab' when not in config."""
        minimal_config = {"gitlab": {"default_group": "test"}}
        config_path = temp_dir / "minimal.yaml"
        with open(config_path, "w", encoding="utf-8") as file:
            yaml.dump(minimal_config, file)

        config = Config(config_path)
        assert config.platform == "gitlab"


class TestPlanningSyncConfig:
    """Test planning_sync configuration."""

    def test_planning_sync_in_new_format(self, temp_dir: Path) -> None:
        """planning_sync config is loaded in new format."""
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/project"},
            "planning_sync": {"gdrive_base": "~/GoogleDrive"},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as file:
            yaml.dump(config_data, file)

        config = Config(config_path)
        assert config.planning_sync == {"gdrive_base": "~/GoogleDrive"}

    def test_planning_sync_in_legacy_format(self, temp_dir: Path) -> None:
        """planning_sync config is preserved during legacy transformation."""
        legacy_config = {
            "gitlab": {"default_group": "test/project"},
            "labels": {"default": ["type::feature"], "allowed_labels": ["type::feature"]},
            "planning_sync": {"gdrive_base": "~/GoogleDrive"},
        }
        config_path = temp_dir / "legacy.yaml"
        with open(config_path, "w", encoding="utf-8") as file:
            yaml.dump(legacy_config, file)

        with pytest.warns(DeprecationWarning):
            config = Config(config_path)

        # Verify planning_sync was preserved during transformation
        assert config.planning_sync == {"gdrive_base": "~/GoogleDrive"}

    def test_planning_sync_missing(self, temp_dir: Path) -> None:
        """planning_sync is empty dict when not configured."""
        config_data = {"platform": "gitlab", "gitlab": {"default_group": "test/project"}}
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as file:
            yaml.dump(config_data, file)

        config = Config(config_path)
        assert config.planning_sync == {}


class TestGetGithubRepo:
    """Test get_github_repo() method."""

    def test_get_github_repo_from_config(self, temp_dir: Path) -> None:
        """Returns explicit repo value from config when present."""
        cfg_path = temp_dir / "config.yaml"
        cfg_path.write_text("platform: github\n" "github:\n" "  repo: myorg/myrepo\n")
        config = Config(cfg_path)
        assert config.get_github_repo() == "myorg/myrepo"

    def test_get_github_repo_auto_detected(self, temp_dir: Path) -> None:
        """Falls back to git remote when repo not in config."""
        cfg_path = temp_dir / "config.yaml"
        cfg_path.write_text("platform: github\ngithub: {}\n")
        config = Config(cfg_path)

        with patch("projctl.config.get_current_repo_path", return_value="detected/repo"):
            assert config.get_github_repo() == "detected/repo"

    def test_get_github_repo_raises_when_unresolvable(self, temp_dir: Path) -> None:
        """ConfigurationError raised when both config and git remote are absent."""
        cfg_path = temp_dir / "config.yaml"
        cfg_path.write_text("platform: github\ngithub: {}\n")
        config = Config(cfg_path)

        with patch("projctl.config.get_current_repo_path", return_value=None):
            with pytest.raises(ConfigurationError, match="Cannot determine GitHub repository"):
                config.get_github_repo()


class TestGetRequiredEpicSections:
    """Test get_required_epic_sections() method."""

    def test_key_absent_returns_default(self, temp_dir: Path) -> None:
        """Returns ['Description'] when epic_template key is absent from config."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"issue_template": {"required_sections": ["Description"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        sections = config.get_required_epic_sections()

        # Assert
        assert sections == ["Description"]

    def test_key_present_with_values_returns_configured_list(self, temp_dir: Path) -> None:
        """Returns configured list when epic_template.required_sections is set."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {
                "epic_template": {"required_sections": ["Overview", "Goals"]},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        sections = config.get_required_epic_sections()

        # Assert
        assert sections == ["Overview", "Goals"]

    def test_key_present_but_empty_returns_empty_list(self, temp_dir: Path) -> None:
        """Returns [] when epic_template.required_sections is explicitly empty."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {
                "epic_template": {"required_sections": []},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        sections = config.get_required_epic_sections()

        # Assert
        assert sections == []


class TestGetRequiredMrSections:
    """Test get_required_mr_sections() method."""

    def test_key_absent_returns_default(self, temp_dir: Path) -> None:
        """Returns default MR sections when mr_template key is absent from config."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"issue_template": {"required_sections": ["Description"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        sections = config.get_required_mr_sections()

        # Assert
        assert sections == ["Summary", "Implementation Details", "How It Was Tested"]

    def test_key_present_with_values_returns_configured_list(self, temp_dir: Path) -> None:
        """Returns configured list when mr_template.required_sections is set."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {
                "mr_template": {"required_sections": ["Summary", "Testing"]},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        sections = config.get_required_mr_sections()

        # Assert
        assert sections == ["Summary", "Testing"]

    def test_key_present_but_empty_returns_empty_list(self, temp_dir: Path) -> None:
        """Returns [] when mr_template.required_sections is explicitly empty."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {
                "mr_template": {"required_sections": []},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        sections = config.get_required_mr_sections()

        # Assert
        assert sections == []


class TestGetRequiredFields:
    """Tests for get_required_issue_fields, get_required_epic_fields, get_required_mr_fields."""

    # -------------------------------------------------------------------
    # H1 regression: sibling key must not disturb the other sub-key default
    # -------------------------------------------------------------------

    def test_mr_template_with_only_required_fields_sections_returns_default(
        self, temp_dir: Path
    ) -> None:
        """mr_template present with only required_fields → get_required_mr_sections returns default."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {
                "mr_template": {"required_fields": ["reviewers"]},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        sections = config.get_required_mr_sections()

        # Assert — required_sections absent under mr_template → default returned
        assert sections == ["Summary", "Implementation Details", "How It Was Tested"]

    def test_mr_template_with_only_required_sections_fields_returns_empty(
        self, temp_dir: Path
    ) -> None:
        """mr_template present with only required_sections → get_required_mr_fields returns []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {
                "mr_template": {"required_sections": ["Summary", "Implementation Details"]},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        fields = config.get_required_mr_fields()

        # Assert — required_fields absent under mr_template → []
        assert fields == []

    def test_epic_template_with_only_required_fields_sections_returns_default(
        self, temp_dir: Path
    ) -> None:
        """epic_template present with only required_fields → get_required_epic_sections returns default."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {
                "epic_template": {"required_fields": ["some_field"]},
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act — suppress the unknown-field warning for the assertion
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            config = Config(config_path)
            sections = config.get_required_epic_sections()

        # Assert
        assert sections == ["Description"]

    # -------------------------------------------------------------------
    # get_required_issue_fields
    # -------------------------------------------------------------------

    def test_issue_fields_common_absent_gitlab_returns_weight(self, temp_dir: Path) -> None:
        """common: absent, platform=gitlab → returns ["weight"]."""
        # Arrange
        config_data = {"platform": "gitlab", "gitlab": {"default_group": "test/group"}}
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_issue_fields() == ["weight"]

    def test_issue_fields_issue_template_absent_gitlab_returns_weight(self, temp_dir: Path) -> None:
        """issue_template absent under common, platform=gitlab → returns ["weight"]."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_issue_fields() == ["weight"]

    def test_issue_fields_only_required_sections_gitlab_returns_weight(
        self, temp_dir: Path
    ) -> None:
        """issue_template has required_sections only (no required_fields), gitlab → ["weight"]."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {
                "issue_template": {"required_sections": ["Description", "Acceptance Criteria"]}
            },
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_issue_fields() == ["weight"]

    def test_issue_fields_empty_required_fields_returns_empty(self, temp_dir: Path) -> None:
        """required_fields: [] → returns []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"issue_template": {"required_fields": []}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_issue_fields() == []

    def test_issue_fields_required_fields_weight_returns_weight(self, temp_dir: Path) -> None:
        """required_fields: ["weight"] → returns ["weight"]."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"issue_template": {"required_fields": ["weight"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_issue_fields() == ["weight"]

    def test_issue_fields_github_no_required_fields_returns_empty(self, temp_dir: Path) -> None:
        """platform=github, required_fields absent → returns []."""
        # Arrange
        config_data = {
            "platform": "github",
            "github": {"repo": "owner/repo"},
            "common": {"issue_template": {"required_sections": ["Description"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_issue_fields() == []

    def test_issue_fields_unknown_name_emits_warning(self, temp_dir: Path) -> None:
        """Unknown field name in required_fields → warning emitted, field included in return."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"issue_template": {"required_fields": ["weight", "unknown_field"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act / Assert
        config = Config(config_path)
        with pytest.warns(UserWarning, match="Unknown required_fields"):
            fields = config.get_required_issue_fields()

        assert "unknown_field" in fields

    # -------------------------------------------------------------------
    # get_required_epic_fields
    # -------------------------------------------------------------------

    def test_epic_fields_key_absent_returns_empty(self, temp_dir: Path) -> None:
        """required_fields key absent from epic_template → []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"epic_template": {"required_sections": ["Description"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_epic_fields() == []

    def test_epic_fields_key_present_with_values_returns_list(self, temp_dir: Path) -> None:
        """required_fields present with values → returns configured list."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"epic_template": {"required_fields": ["future_field"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act / Assert — suppress unknown-field warning
        config = Config(config_path)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            fields = config.get_required_epic_fields()

        assert fields == ["future_field"]

    def test_epic_fields_key_present_but_empty_returns_empty(self, temp_dir: Path) -> None:
        """required_fields: [] → []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"epic_template": {"required_fields": []}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_epic_fields() == []

    # -------------------------------------------------------------------
    # get_required_mr_fields
    # -------------------------------------------------------------------

    def test_mr_fields_key_absent_returns_empty(self, temp_dir: Path) -> None:
        """required_fields key absent from mr_template → []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"required_sections": ["Summary"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_mr_fields() == []

    def test_mr_fields_key_present_with_values_returns_list(self, temp_dir: Path) -> None:
        """required_fields: ["reviewers", "labels"] → returns configured list."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"required_fields": ["reviewers", "labels"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        fields = config.get_required_mr_fields()

        # Assert
        assert fields == ["reviewers", "labels"]

    def test_mr_fields_key_present_but_empty_returns_empty(self, temp_dir: Path) -> None:
        """required_fields: [] → []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"required_fields": []}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_required_mr_fields() == []

    def test_mr_fields_unknown_name_emits_warning(self, temp_dir: Path) -> None:
        """Unknown field name "reviewer" in mr required_fields → warning emitted, included in return."""
        # Arrange — "reviewer" (singular) is not a known name; "reviewers" (plural) is
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"required_fields": ["reviewer"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act / Assert
        config = Config(config_path)
        with pytest.warns(UserWarning, match="Unknown required_fields"):
            fields = config.get_required_mr_fields()

        assert "reviewer" in fields

    # -------------------------------------------------------------------
    # M1: type validation for required_fields (scalar → ConfigurationError, null → [])
    # -------------------------------------------------------------------

    def test_issue_fields_scalar_string_raises_configuration_error(self, temp_dir: Path) -> None:
        """required_fields: 'weight' (scalar string) → raises ConfigurationError."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"issue_template": {"required_fields": "weight"}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act / Assert
        config = Config(config_path)
        with pytest.raises(
            ConfigurationError, match="issue_template.required_fields must be a list"
        ):
            config.get_required_issue_fields()

    def test_issue_fields_null_returns_empty(self, temp_dir: Path) -> None:
        """required_fields: null (YAML null → Python None) → returns []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"issue_template": {"required_fields": None}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)
        fields = config.get_required_issue_fields()

        # Assert — None is handled by `or []`; no error
        assert fields == []

    def test_epic_fields_scalar_string_raises_configuration_error(self, temp_dir: Path) -> None:
        """epic required_fields: 'some_field' (scalar string) → raises ConfigurationError."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"epic_template": {"required_fields": "some_field"}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act / Assert
        config = Config(config_path)
        with pytest.raises(
            ConfigurationError, match="epic_template.required_fields must be a list"
        ):
            config.get_required_epic_fields()

    def test_mr_fields_scalar_string_raises_configuration_error(self, temp_dir: Path) -> None:
        """mr required_fields: 'reviewers' (scalar string) → raises ConfigurationError."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"required_fields": "reviewers"}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act / Assert
        config = Config(config_path)
        with pytest.raises(ConfigurationError, match="mr_template.required_fields must be a list"):
            config.get_required_mr_fields()


# ---------------------------------------------------------------------------
# TestGetDefaultMrReviewers
# ---------------------------------------------------------------------------


class TestGetDefaultMrReviewers:
    """Tests for Config.get_default_mr_reviewers()."""

    def test_absent_reviewers_returns_empty(self, temp_dir: Path) -> None:
        """No mr_template.reviewers key → []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"required_sections": ["Summary"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_default_mr_reviewers() == []

    def test_empty_reviewers_returns_empty(self, temp_dir: Path) -> None:
        """mr_template.reviewers: [] → []."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"reviewers": []}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_default_mr_reviewers() == []

    def test_configured_reviewers_returned(self, temp_dir: Path) -> None:
        """mr_template.reviewers: [alice, bob] → ['alice', 'bob']."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"reviewers": ["alice", "bob"]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act
        config = Config(config_path)

        # Assert
        assert config.get_default_mr_reviewers() == ["alice", "bob"]

    def test_non_list_raises_configuration_error(self, temp_dir: Path) -> None:
        """mr_template.reviewers: 'alice' (scalar) → ConfigurationError."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"reviewers": "alice"}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act / Assert
        config = Config(config_path)
        with pytest.raises(ConfigurationError, match="mr_template.reviewers must be a list"):
            config.get_default_mr_reviewers()

    def test_non_string_element_raises_configuration_error(self, temp_dir: Path) -> None:
        """mr_template.reviewers: [123] (non-string element) → ConfigurationError."""
        # Arrange
        config_data = {
            "platform": "gitlab",
            "gitlab": {"default_group": "test/group"},
            "common": {"mr_template": {"reviewers": [123]}},
        }
        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f)

        # Act / Assert
        config = Config(config_path)
        with pytest.raises(ConfigurationError, match="mr_template.reviewers must be a list of strings"):
            config.get_default_mr_reviewers()


# ---------------------------------------------------------------------------
# TestConfigSearchPaths
# ---------------------------------------------------------------------------


class TestConfigSearchPaths:
    """Tests for the config_search_paths() module-level helper."""

    def test_returns_four_entries(self) -> None:
        """config_search_paths() returns exactly 4 (path, label) tuples."""
        paths = config_search_paths()
        assert len(paths) == 4

    def test_all_entries_are_tuples_of_path_and_str(self) -> None:
        """Every entry is a (Path, str) tuple."""
        for path, label in config_search_paths():
            assert isinstance(path, Path)
            assert isinstance(label, str) and label

    def test_first_two_entries_are_cwd_relative(self, monkeypatch, tmp_path: Path) -> None:
        """The first two entries are under the current working directory."""
        monkeypatch.chdir(tmp_path)
        paths = config_search_paths()
        assert paths[0][0].parent == tmp_path
        assert paths[1][0].parent == tmp_path

    def test_last_two_entries_are_home_relative(self) -> None:
        """The last two entries are under the user home directory."""
        home = Path.home()
        paths = config_search_paths()
        assert str(paths[2][0]).startswith(str(home))
        assert str(paths[3][0]).startswith(str(home))

    def test_legacy_name_appears_before_preferred_name(self) -> None:
        """glab_config.yaml (legacy) appears before projctl.yaml (preferred) in order."""
        paths = config_search_paths()
        names = [p.name for p, _ in paths]
        assert names.index("glab_config.yaml") < names.index("projctl.yaml")

    def test_labels_are_human_readable(self) -> None:
        """Every label contains the file name so it is self-describing."""
        for path, label in config_search_paths():
            assert path.name in label

    def test_user_config_appears_before_legacy_user_config(self) -> None:
        """~/.config/projctl/config.yaml appears before ~/.config/glab_config.yaml."""
        paths = config_search_paths()
        user_idx = next(i for i, (p, _) in enumerate(paths) if ".config/projctl" in str(p))
        legacy_idx = next(
            i
            for i, (p, _) in enumerate(paths)
            if p.name == "glab_config.yaml" and ".config" in str(p)
        )
        assert user_idx < legacy_idx
