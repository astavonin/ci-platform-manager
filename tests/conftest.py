"""Shared pytest fixtures for projctl tests."""

import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, Mock

import pytest
import yaml

from projctl.config import Config


@pytest.fixture(autouse=True, scope="session")
def _cleanup_pytest_claude_stubs():
    """Remove any pytest-created stub directories from ~/.claude/projects/ after the session."""
    yield
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return
    for entry in projects_dir.iterdir():
        if entry.is_dir() and "-tmp-pytest-" in entry.name:
            shutil.rmtree(entry, ignore_errors=True)


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for test files.

    Args:
        tmp_path: pytest built-in fixture providing temporary directory.

    Returns:
        Path to temporary directory.
    """
    return tmp_path


@pytest.fixture
def legacy_config_data() -> Dict[str, Any]:
    """Legacy config format data."""
    return {
        "gitlab": {"default_group": "test/group"},
        "labels": {
            "default": ["type::feature", "development-status::backlog"],
            "default_epic": ["epic"],
            "allowed_labels": [
                "type::feature",
                "type::bug",
                "development-status::backlog",
                "development-status::in-progress",
                "epic",
            ],
        },
        "issue_template": {
            "sections": [
                {"name": "Description", "required": True},
                {"name": "Acceptance Criteria", "required": True},
                {"name": "Notes", "required": False},
            ]
        },
    }


@pytest.fixture
def new_config_data() -> Dict[str, Any]:
    """New config format data."""
    return {
        "platform": "gitlab",
        "gitlab": {
            "default_group": "test/group",
            "labels": {
                "default": ["type::feature", "development-status::backlog"],
                "default_epic": ["epic"],
                "allowed": [
                    "type::feature",
                    "type::bug",
                    "development-status::backlog",
                    "development-status::in-progress",
                    "epic",
                ],
            },
        },
        "common": {"issue_template": {"required_sections": ["Description", "Acceptance Criteria"]}},
    }


@pytest.fixture
def legacy_config_path(temp_dir: Path, legacy_config_data: Dict[str, Any]) -> Path:
    """Create a legacy config file.

    Args:
        temp_dir: Temporary directory path.
        legacy_config_data: Legacy config data.

    Returns:
        Path to the created config file.
    """
    config_path = temp_dir / "glab_config.yaml"
    with open(config_path, "w", encoding="utf-8") as file:
        yaml.dump(legacy_config_data, file)
    return config_path


@pytest.fixture
def new_config_path(temp_dir: Path, new_config_data: Dict[str, Any]) -> Path:
    """Create a new format config file.

    Args:
        temp_dir: Temporary directory path.
        new_config_data: New config data.

    Returns:
        Path to the created config file.
    """
    config_path = temp_dir / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as file:
        yaml.dump(new_config_data, file)
    return config_path


@pytest.fixture
def mock_subprocess_run() -> Mock:
    """Create a mock for subprocess.run.

    Returns:
        Mock object configured for subprocess.run.
    """
    mock = Mock()
    mock.return_value = Mock(stdout="", stderr="", returncode=0)
    return mock


@pytest.fixture
def mock_glab_success() -> Mock:
    """Mock successful glab command execution.

    Returns:
        Mock object for successful glab command.
    """
    mock = Mock()
    mock.return_value = Mock(
        stdout='{"id": 123, "iid": 1, "title": "Test Issue"}', stderr="", returncode=0
    )
    return mock


@pytest.fixture
def mock_glab_failure() -> Mock:
    """Mock failed glab command execution.

    Returns:
        Mock object for failed glab command.
    """
    mock = Mock()
    mock.return_value = Mock(stdout="", stderr="Error: Command failed", returncode=1)
    return mock


@pytest.fixture
def sample_issue_yaml_data() -> Dict[str, Any]:
    """Sample issue YAML data for testing.

    Returns:
        Dictionary containing sample issue data.
    """
    return {
        "epic": {
            "title": "Test Epic",
            "description": "# Description\n\nTest epic description.",
        },
        "issues": [
            {
                "id": "task-1",
                "title": "[Impl] First task",
                "description": "# Description\n\nTask description\n\n# Acceptance Criteria\n\n- AC1",
                "labels": ["type::feature"],
                "assignee": "testuser",
                "weight": 3,
            },
            {
                "id": "task-2",
                "title": "[Impl] Second task",
                "description": "# Description\n\nSecond task\n\n# Acceptance Criteria\n\n- AC2",
                "labels": ["type::bug"],
                "dependencies": ["task-1"],
                "weight": 5,
            },
        ],
    }


@pytest.fixture
def sample_issue_yaml_path(temp_dir: Path, sample_issue_yaml_data: Dict[str, Any]) -> Path:
    """Create a sample issue YAML file.

    Args:
        temp_dir: Temporary directory path.
        sample_issue_yaml_data: Sample issue data.

    Returns:
        Path to the created YAML file.
    """
    yaml_path = temp_dir / "test_issues.yaml"
    with open(yaml_path, "w", encoding="utf-8") as file:
        yaml.dump(sample_issue_yaml_data, file)
    return yaml_path


@pytest.fixture
def sample_review_yaml_data() -> Dict[str, Any]:
    """Sample review YAML data for testing.

    Returns:
        Dictionary containing sample review data.
    """
    return {
        "mr_number": 123,
        "title": "Draft: Test MR",
        "review_date": "2026-02-05",
        "findings": [
            {
                "severity": "Critical",
                "title": "Memory leak detected",
                "description": "Memory is not freed in error path",
                "location": "src/main.cc:45",
                "fix": "Add delete statement",
                "guideline": "C++ Core Guidelines R.3",
            },
            {
                "severity": "Medium",
                "title": "Missing null check",
                "description": "Pointer not validated before use",
                "locations": ["src/util.cc:123", "src/util.cc:456"],
                "fix": "Add null pointer check",
                "guideline": None,
            },
        ],
    }


@pytest.fixture
def sample_review_yaml_path(temp_dir: Path, sample_review_yaml_data: Dict[str, Any]) -> Path:
    """Create a sample review YAML file.

    Args:
        temp_dir: Temporary directory path.
        sample_review_yaml_data: Sample review data.

    Returns:
        Path to the created YAML file.
    """
    yaml_path = temp_dir / "test_review.yaml"
    with open(yaml_path, "w", encoding="utf-8") as file:
        yaml.dump(sample_review_yaml_data, file)
    return yaml_path


@pytest.fixture
def mock_glab_issue_view() -> str:
    """Mock glab issue view JSON output.

    Returns:
        JSON string with issue data.
    """
    return """{
        "id": 123,
        "iid": 1,
        "title": "Test Issue",
        "state": "opened",
        "labels": ["type::feature"],
        "author": {"username": "testuser"},
        "assignees": [{"username": "dev1"}],
        "milestone": {"title": "v1.0"},
        "description": "# Description\\n\\nIssue description",
        "created_at": "2026-02-01T10:00:00Z",
        "updated_at": "2026-02-05T15:30:00Z",
        "closed_at": null,
        "web_url": "https://gitlab.example.com/test/project/-/issues/1"
    }"""


@pytest.fixture
def mock_glab_epic_view() -> str:
    """Mock glab api epic view JSON output.

    Returns:
        JSON string with epic data.
    """
    return """{
        "id": 456,
        "iid": 21,
        "title": "Test Epic",
        "state": "opened",
        "labels": ["epic"],
        "author": {"username": "testuser"},
        "description": "Epic description",
        "created_at": "2026-01-01T10:00:00Z",
        "updated_at": "2026-02-05T15:30:00Z",
        "web_url": "https://gitlab.example.com/groups/test/-/epics/21"
    }"""


@pytest.fixture
def mock_glab_mr_view() -> str:
    """Mock glab mr view JSON output.

    Returns:
        JSON string with MR data.
    """
    return """{
        "id": 789,
        "iid": 134,
        "title": "Draft: Test MR",
        "state": "opened",
        "draft": true,
        "source_branch": "feature-branch",
        "target_branch": "main",
        "labels": ["type::feature"],
        "author": {"username": "testuser"},
        "assignees": [{"username": "dev1"}],
        "reviewers": [{"username": "reviewer1"}],
        "milestone": {"title": "v2.0"},
        "description": "MR description",
        "created_at": "2026-02-03T10:00:00Z",
        "updated_at": "2026-02-05T15:30:00Z",
        "merged_at": null,
        "pipeline": {"status": "success"},
        "web_url": "https://gitlab.example.com/test/project/-/merge_requests/134"
    }"""


@pytest.fixture
def make_mr_config():
    """Factory fixture for a MagicMock Config with MR-relevant methods stubbed."""

    def _factory(
        default_reviewers: Optional[List[str]] = None,
        required_fields: Optional[List[str]] = None,
        required_sections: Optional[List[str]] = None,
        allowed_labels: Optional[List[str]] = None,
    ) -> MagicMock:
        mock = MagicMock(spec=Config)
        mock.get_default_mr_reviewers.return_value = default_reviewers or []
        mock.get_required_mr_fields.return_value = required_fields or []
        mock.get_required_mr_sections.return_value = (
            required_sections
            or ["Summary", "Implementation Details", "How It Was Tested"]
        )
        mock.get_allowed_labels.return_value = allowed_labels
        return mock

    return _factory
