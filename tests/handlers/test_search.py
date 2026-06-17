"""Tests for projctl.handlers.search module."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from projctl.cli import main as cli_main
from projctl.config import Config
from projctl.exceptions import PlatformError
from projctl.handlers.search import SearchHandler


class TestSearchHandlerInit:
    """Test SearchHandler initialization."""

    def test_init(self, new_config_path: Path) -> None:
        """SearchHandler initializes correctly."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        assert handler.config == config


class TestSearchIssues:
    """Test issue search."""

    @patch("subprocess.run")
    def test_search_issues_success(self, mock_run: Mock, new_config_path: Path, capsys) -> None:
        """Search issues successfully."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        # Mock search results
        search_results = [
            {
                "iid": 1,
                "title": "First issue",
                "state": "opened",
                "labels": ["type::feature"],
                "web_url": "https://gitlab.example.com/test/project/-/issues/1",
            },
            {
                "iid": 2,
                "title": "Second issue",
                "state": "closed",
                "labels": ["type::bug"],
                "web_url": "https://gitlab.example.com/test/project/-/issues/2",
            },
        ]

        mock_run.return_value = Mock(stdout=json.dumps(search_results), returncode=0)

        handler.search_issues("test query")

        captured = capsys.readouterr()
        assert "First issue" in captured.out
        assert "Second issue" in captured.out

    @patch("subprocess.run")
    def test_search_issues_with_state(self, mock_run: Mock, new_config_path: Path) -> None:
        """Search issues with state filter."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues("query", state="opened")

        # Verify state parameter was passed
        call_args = mock_run.call_args[0][0]
        assert "state=opened" in " ".join(call_args)

    @patch("subprocess.run")
    def test_search_issues_with_limit(self, mock_run: Mock, new_config_path: Path) -> None:
        """Search issues with result limit."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues("query", limit=10)

        # Verify limit parameter was passed
        call_args = mock_run.call_args[0][0]
        assert "per_page=10" in " ".join(call_args)

    @patch("subprocess.run")
    def test_search_issues_no_results(self, mock_run: Mock, new_config_path: Path, capsys) -> None:
        """Search with no results displays appropriate message."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues("nonexistent")

        captured = capsys.readouterr()
        assert "No issues found" in captured.out or "found" in captured.out.lower()

    @patch("subprocess.run")
    def test_search_issues_command_failure(self, mock_run: Mock, new_config_path: Path) -> None:
        """Search failure raises PlatformError."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.side_effect = subprocess.CalledProcessError(1, ["glab", "api"], stderr="API error")

        with pytest.raises(PlatformError, match="Command failed"):
            handler.search_issues("query")


class TestSearchEpics:
    """Test epic search."""

    @patch("subprocess.run")
    def test_search_epics_success(self, mock_run: Mock, new_config_path: Path, capsys) -> None:
        """Search epics successfully."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        search_results = [
            {
                "iid": 21,
                "title": "Test Epic",
                "state": "opened",
                "web_url": "https://gitlab.example.com/groups/test/-/epics/21",
            }
        ]

        mock_run.return_value = Mock(stdout=json.dumps(search_results), returncode=0)

        handler.search_epics("epic query")

        captured = capsys.readouterr()
        assert "Test Epic" in captured.out

    @patch("subprocess.run")
    def test_search_epics_requires_group(self, mock_run: Mock, temp_dir: Path) -> None:
        """Epic search requires group in config."""
        # Config without default group
        minimal_config = {"platform": "gitlab", "gitlab": {}}
        import yaml

        config_path = temp_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as file:
            yaml.dump(minimal_config, file)

        config = Config(config_path)
        handler = SearchHandler(config)

        with pytest.raises(ValueError, match="group"):
            handler.search_epics("query")

    @patch("subprocess.run")
    def test_search_epics_command_failure(self, mock_run: Mock, new_config_path: Path) -> None:
        """Epic search failure raises PlatformError."""
        config = Config(new_config_path)
        handler = SearchHandler(config)
        mock_run.side_effect = subprocess.CalledProcessError(1, ["glab", "api"], stderr="API error")
        with pytest.raises(PlatformError, match="Command failed"):
            handler.search_epics("query")


class TestSearchMilestones:
    """Test milestone search."""

    @patch("subprocess.run")
    def test_search_milestones_success(self, mock_run: Mock, new_config_path: Path, capsys) -> None:
        """Search milestones successfully."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        search_results = [
            {
                "id": 123,
                "iid": 1,
                "title": "v1.0",
                "state": "active",
                "web_url": "https://gitlab.example.com/test/project/-/milestones/1",
            },
            {
                "id": 124,
                "iid": 2,
                "title": "v2.0",
                "state": "closed",
                "web_url": "https://gitlab.example.com/test/project/-/milestones/2",
            },
        ]

        mock_run.return_value = Mock(stdout=json.dumps(search_results), returncode=0)

        handler.search_milestones("v")

        captured = capsys.readouterr()
        assert "v1.0" in captured.out
        assert "v2.0" in captured.out

    @patch("subprocess.run")
    def test_search_milestones_state_filter(self, mock_run: Mock, new_config_path: Path) -> None:
        """Search milestones with state filter."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_milestones("query", state="active")

        # Verify state filter was applied
        call_args = mock_run.call_args[0][0]
        assert "state=active" in " ".join(call_args)

    @patch("subprocess.run")
    def test_search_milestones_all_states(self, mock_run: Mock, new_config_path: Path) -> None:
        """Search milestones with state=all omits state= from the API URL."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_milestones("query", state="all")

        url = " ".join(mock_run.call_args[0][0])
        assert "state=" not in url

    @patch("subprocess.run")
    def test_search_milestones_command_failure(self, mock_run: Mock, new_config_path: Path) -> None:
        """Milestone search failure raises PlatformError."""
        config = Config(new_config_path)
        handler = SearchHandler(config)
        mock_run.side_effect = subprocess.CalledProcessError(1, ["glab", "api"], stderr="API error")
        with pytest.raises(PlatformError, match="Command failed"):
            handler.search_milestones("query")


class TestOutputFormatting:
    """Test search output formatting."""

    @patch("subprocess.run")
    def test_text_output_format(self, mock_run: Mock, new_config_path: Path, capsys) -> None:
        """Search output is plain text format."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        search_results = [
            {
                "iid": 1,
                "title": "Test Issue",
                "state": "opened",
                "labels": ["type::feature"],
                "web_url": "https://gitlab.example.com/test/project/-/issues/1",
            }
        ]

        mock_run.return_value = Mock(stdout=json.dumps(search_results), returncode=0)

        handler.search_issues("test")

        captured = capsys.readouterr()
        # Verify text format (not markdown)
        assert "#1" in captured.out
        assert "Test Issue" in captured.out

    @patch("subprocess.run")
    def test_includes_labels(self, mock_run: Mock, new_config_path: Path, capsys) -> None:
        """Search output includes labels."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        search_results = [
            {
                "iid": 1,
                "title": "Test Issue",
                "state": "opened",
                "labels": ["type::feature", "priority::high"],
                "web_url": "https://gitlab.example.com/test/project/-/issues/1",
            }
        ]

        mock_run.return_value = Mock(stdout=json.dumps(search_results), returncode=0)

        handler.search_issues("test")

        captured = capsys.readouterr()
        assert "type::feature" in captured.out
        assert "priority::high" in captured.out


class TestLabelFilter:
    """Test label filtering for issues and epics."""

    @patch("subprocess.run")
    def test_search_issues_with_label(self, mock_run: Mock, new_config_path: Path) -> None:
        """Handler URL-encodes a label with special characters in the issues API URL."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues(labels=["type::feature"])

        call_args = mock_run.call_args[0][0]
        assert "labels=type%3A%3Afeature" in " ".join(call_args)

    @patch("subprocess.run")
    def test_search_issues_with_label_and_query(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """Both search= and labels=<encoded-value> appear in URL when query and label are given."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues(query="bug", labels=["type::feature"])

        url = " ".join(mock_run.call_args[0][0])
        assert "search=bug" in url
        assert "labels=type%3A%3Afeature" in url

    @patch("subprocess.run")
    def test_search_issues_label_only_no_query_in_url(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """When no query is given, search= must not appear in the issues API URL."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues(labels=["type::feature"])

        url = " ".join(mock_run.call_args[0][0])
        assert "search=" not in url
        assert "labels=type%3A%3Afeature" in url

    @patch("subprocess.run")
    def test_search_issues_label_header(
        self, mock_run: Mock, new_config_path: Path, capsys
    ) -> None:
        """Output header shows label:my-label when only a label filter is set."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues(labels=["my-label"])

        captured = capsys.readouterr()
        assert "label:my-label" in captured.out

    @patch("subprocess.run")
    def test_search_issues_label_and_query_header(
        self, mock_run: Mock, new_config_path: Path, capsys
    ) -> None:
        """Output header contains both the quoted query and label:my-label."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues(query="my-query", labels=["my-label"])

        captured = capsys.readouterr()
        assert '"my-query"' in captured.out
        assert "label:my-label" in captured.out

    @patch("subprocess.run")
    def test_search_issues_no_filter_header(
        self, mock_run: Mock, new_config_path: Path, capsys
    ) -> None:
        """Output header shows (all) when neither query nor label is provided."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues()

        captured = capsys.readouterr()
        assert "(all)" in captured.out
        assert "=== ISSUES matching (all) ===" in captured.out
        assert "label:" not in captured.out

    @patch("subprocess.run")
    def test_search_epics_with_label(self, mock_run: Mock, new_config_path: Path) -> None:
        """Handler URL-encodes a label with special characters in the epics API URL."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_epics(labels=["type::feature"])

        url = " ".join(mock_run.call_args[0][0])
        assert "labels=type%3A%3Afeature" in url

    @patch("subprocess.run")
    def test_search_epics_label_only_no_query_in_url(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """When no query is given, search= must not appear in the epics API URL."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_epics(labels=["type::epic"])

        url = " ".join(mock_run.call_args[0][0])
        assert "search=" not in url
        assert "labels=type%3A%3Aepic" in url

    @patch("subprocess.run")
    def test_search_epics_no_per_page_duplicate(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """per_page appears exactly once in the epics API URL (regression for duplicate bug)."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_epics()

        url = " ".join(mock_run.call_args[0][0])
        assert url.count("per_page=") == 1
        assert "per_page=20" in url

    @patch("subprocess.run")
    def test_search_epics_label_header(self, mock_run: Mock, new_config_path: Path, capsys) -> None:
        """Output header shows label:my-label when an epic label filter is set."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_epics(labels=["my-label"])

        captured = capsys.readouterr()
        assert "label:my-label" in captured.out


class TestCLISearchLabel:
    """Test that the CLI correctly joins multiple --label values before passing them on."""

    @patch("subprocess.run")
    def test_multiple_labels_joined_in_api_url(self, mock_run: Mock, new_config_path: Path) -> None:
        """Two --label flags are joined with a comma and appear together in the API URL."""
        mock_run.return_value = Mock(stdout="[]", returncode=0)

        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(new_config_path),
                "search",
                "issues",
                "--label",
                "type::feature",
                "--label",
                "priority::high",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 0
        url = " ".join(mock_run.call_args[0][0])
        # Each label is individually percent-encoded; the separator is a literal comma
        assert "labels=type%3A%3Afeature,priority%3A%3Ahigh" in url

    @patch("subprocess.run")
    def test_search_issues_single_label_url_encoding(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """Handler URL-encodes a label containing special characters."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues(labels=["area,backend"])

        url = " ".join(mock_run.call_args[0][0])
        assert "labels=area%2Cbackend" in url
        assert url.count("labels=") == 1

    @patch("subprocess.run")
    def test_label_with_comma_in_name_via_cli(self, mock_run: Mock, new_config_path: Path) -> None:
        """A label with a comma in its name is encoded as a single label at the CLI level."""
        mock_run.return_value = Mock(stdout="[]", returncode=0)

        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(new_config_path),
                "search",
                "issues",
                "--label",
                "area,backend",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 0
        url = " ".join(mock_run.call_args[0][0])
        assert "labels=area%2Cbackend" in url

    @patch("subprocess.run")
    def test_search_issues_state_and_label_combined(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """Both state= and labels= appear in URL when state and label are both set."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_issues(state="opened", labels=["type::feature"])

        url = " ".join(mock_run.call_args[0][0])
        assert "state=opened" in url
        assert "labels=type%3A%3Afeature" in url

    @patch("subprocess.run")
    def test_single_label_cli(self, mock_run: Mock, new_config_path: Path) -> None:
        """A single --label flag is correctly URL-encoded in the API call."""
        mock_run.return_value = Mock(stdout="[]", returncode=0)

        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(new_config_path),
                "search",
                "issues",
                "--label",
                "type::feature",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 0
        url = " ".join(mock_run.call_args[0][0])
        assert "labels=type%3A%3Afeature" in url

    @patch("subprocess.run")
    def test_search_epics_label_cli(self, mock_run: Mock, new_config_path: Path) -> None:
        """--label on search epics passes the encoded label to the epics API endpoint."""
        mock_run.return_value = Mock(stdout="[]", returncode=0)

        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(new_config_path),
                "search",
                "epics",
                "--label",
                "type::epic",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 0
        url = " ".join(mock_run.call_args[0][0])
        assert "labels=type%3A%3Aepic" in url
        assert "epics" in url

    @patch("projctl.handlers.search.SearchHandler.search_milestones")
    def test_label_rejected_for_milestones(
        self, mock_search_milestones: Mock, new_config_path: Path, capsys
    ) -> None:
        """--label on milestones returns exit code 1 and does not call search_milestones."""
        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(new_config_path),
                "search",
                "milestones",
                "--label",
                "foo",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 1
        mock_search_milestones.assert_not_called()
        captured = capsys.readouterr()
        assert "not supported" in captured.err

    def test_empty_label_rejected(self, new_config_path: Path, capsys) -> None:
        """--label with an empty string is rejected with exit code 1."""
        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(new_config_path),
                "search",
                "issues",
                "--label",
                "",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 1
        captured = capsys.readouterr()
        assert "non-empty" in captured.err or "empty" in captured.err

    def test_whitespace_only_label_rejected(self, new_config_path: Path, capsys) -> None:
        """--label with only whitespace is rejected."""
        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(new_config_path),
                "search",
                "issues",
                "--label",
                "   ",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 1


class TestSearchMilestonesEmptyQuery:
    """Test milestone search behaviour when no query is provided."""

    @patch("subprocess.run")
    def test_search_milestones_empty_query_no_search_param(
        self, mock_run: Mock, new_config_path: Path
    ) -> None:
        """search= must not appear in the milestones URL when query is empty."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_milestones()

        url = " ".join(mock_run.call_args[0][0])
        assert "search=" not in url
        assert "per_page=20" in url

    @patch("subprocess.run")
    def test_search_milestones_empty_query_all_header(
        self, mock_run: Mock, new_config_path: Path, capsys
    ) -> None:
        """Header shows (all) when no query is given."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_milestones()

        captured = capsys.readouterr()
        assert "(all)" in captured.out
        assert 'matching ""' not in captured.out

    @patch("subprocess.run")
    def test_search_milestones_with_query_shows_query_in_header(
        self, mock_run: Mock, new_config_path: Path, capsys
    ) -> None:
        """Header shows the quoted query string and search= param when a query is provided."""
        config = Config(new_config_path)
        handler = SearchHandler(config)

        mock_run.return_value = Mock(stdout="[]", returncode=0)

        handler.search_milestones(query="v1.0")

        url = " ".join(mock_run.call_args[0][0])
        assert "search=v1.0" in url
        captured = capsys.readouterr()
        assert '"v1.0"' in captured.out


class TestCLISearchGithub:
    """Tests for GitHub search dispatch."""

    def _make_github_config(self, tmp_path: Path) -> Path:
        """Create a minimal GitHub platform config."""
        import yaml

        cfg = {
            "platform": "github",
            "github": {"repo": "org/repo"},
            "common": {
                "issue_template": {"required_sections": ["Description", "Acceptance Criteria"]}
            },
        }
        config_path = tmp_path / "github_config.yaml"
        config_path.write_text(yaml.dump(cfg), encoding="utf-8")
        return config_path

    @patch("subprocess.run")
    def test_github_state_opened_translated_to_open(self, mock_run: Mock, tmp_path: Path) -> None:
        """--state opened is translated to 'open' for the gh CLI."""
        mock_run.return_value = Mock(stdout="[]", returncode=0)
        config_path = self._make_github_config(tmp_path)

        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(config_path),
                "search",
                "issues",
                "query",
                "--state",
                "opened",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 0
        parts = mock_run.call_args[0][0]
        call_args = " ".join(parts)
        assert "--state" in call_args
        assert "open" in parts
        assert "opened" not in parts

    def test_github_state_active_rejected(self, tmp_path: Path, capsys) -> None:
        """--state active is rejected for GitHub issues."""
        config_path = self._make_github_config(tmp_path)

        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(config_path),
                "search",
                "issues",
                "query",
                "--state",
                "active",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 1
        captured = capsys.readouterr()
        assert "active" in captured.err

    @patch("subprocess.run")
    def test_github_empty_query_shows_all_header(
        self, mock_run: Mock, tmp_path: Path, capsys
    ) -> None:
        """Empty query on GitHub shows (all) in the output header."""
        mock_run.return_value = Mock(stdout="[]", returncode=0)
        config_path = self._make_github_config(tmp_path)

        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(config_path),
                "search",
                "issues",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 0
        captured = capsys.readouterr()
        assert "(all)" in captured.out
        assert 'matching ""' not in captured.out

    def test_label_rejected_for_github(self, tmp_path: Path, capsys) -> None:
        """--label on GitHub returns exit code 1 with an error on stderr."""
        config_path = self._make_github_config(tmp_path)

        old_argv = sys.argv
        try:
            sys.argv = [
                "projctl",
                "--config",
                str(config_path),
                "search",
                "issues",
                "--label",
                "foo",
            ]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 1
        captured = capsys.readouterr()
        assert "GitHub" in captured.err
        assert "not supported" in captured.err


class TestGithubSearchMilestones:
    """Tests for GithubSearchHandler milestone path."""

    def _make_github_config(self, tmp_path: Path) -> Path:
        """Create a minimal GitHub platform config."""
        import yaml

        cfg = {
            "platform": "github",
            "github": {"repo": "org/repo"},
            "common": {
                "issue_template": {"required_sections": ["Description", "Acceptance Criteria"]}
            },
        }
        config_path = tmp_path / "github_config.yaml"
        config_path.write_text(yaml.dump(cfg), encoding="utf-8")
        return config_path

    @patch("subprocess.run")
    def test_github_milestones_empty_query_shows_all_header(
        self, mock_run: Mock, tmp_path: Path, capsys
    ) -> None:
        """Empty query on GitHub milestones shows (all) in the output header."""
        mock_run.return_value = Mock(stdout="[]", returncode=0)
        config_path = self._make_github_config(tmp_path)

        old_argv = sys.argv
        try:
            sys.argv = ["projctl", "--config", str(config_path), "search", "milestones"]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 0
        captured = capsys.readouterr()
        assert "(all)" in captured.out
        assert 'matching ""' not in captured.out

    @patch("subprocess.run")
    def test_github_milestones_with_query_shows_query_in_header(
        self, mock_run: Mock, tmp_path: Path, capsys
    ) -> None:
        """Non-empty query on GitHub milestones shows quoted query in output header."""
        search_results = [
            {
                "number": 1,
                "title": "v1.0",
                "state": "open",
                "html_url": "https://github.com/org/repo/milestone/1",
                "due_on": None,
            }
        ]
        mock_run.return_value = Mock(stdout=json.dumps(search_results), returncode=0)
        config_path = self._make_github_config(tmp_path)

        old_argv = sys.argv
        try:
            sys.argv = ["projctl", "--config", str(config_path), "search", "milestones", "v1.0"]
            result = cli_main()
        finally:
            sys.argv = old_argv

        assert result == 0
        captured = capsys.readouterr()
        assert '"v1.0"' in captured.out
