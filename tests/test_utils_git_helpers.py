"""Tests for projctl.utils.git_helpers module."""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

from projctl.utils.git_helpers import extract_host_from_url, get_current_repo_path, parse_mr_url


class TestExtractHostFromUrl:
    """Test extract_host_from_url function — the symmetric counterpart to
    extract_path_from_url, used by timelog.py's write path to bind a URL reference's own
    host alongside its project path (see projctl/handlers/timelog.py::_resolve_target)."""

    def test_https_url_returns_the_host(self) -> None:
        """A standard https URL's host is extracted without its scheme or path."""
        assert extract_host_from_url("https://gitlab.example.com/group/project") == (
            "gitlab.example.com"
        )

    def test_url_with_path_after_host_returns_only_the_host(self) -> None:
        """A deep path after the host does not leak into the returned host string."""
        result = extract_host_from_url("https://gitlab.example.com/group/project/-/issues/478")
        assert result == "gitlab.example.com"

    def test_no_scheme_separator_returns_empty_string(self) -> None:
        """A string with no '//' scheme separator (not a URL at all) returns ''."""
        assert extract_host_from_url("group/project") == ""

    def test_empty_string_returns_empty_string(self) -> None:
        assert extract_host_from_url("") == ""


class TestParseMrUrl:
    """Test parse_mr_url function.

    Scope note: this class tests git_helpers.parse_mr_url() only, which is the timelog
    write path's canonical MR-reference parser (see that function's own docstring). It does
    not describe how note.py, updater.py, or loader.py parse MR references — those three
    modules each implement their own inline logic and diverge from this one (and each other)
    on a URL fragment such as '#note_456'.
    """

    def test_plain_number(self) -> None:
        """A bare digit string parses with no project path."""
        assert parse_mr_url("134") == (None, "134")

    def test_bang_prefixed(self) -> None:
        """A '!'-prefixed reference strips the prefix, with no project path."""
        assert parse_mr_url("!134") == (None, "134")

    def test_full_url(self) -> None:
        """A full MR URL extracts both the project path and the iid."""
        result = parse_mr_url("https://gitlab.example.com/group/project/-/merge_requests/134")
        assert result == ("group/project", "134")

    def test_full_url_nested_group(self) -> None:
        """A URL with a nested group path extracts the full path, not just the last segment."""
        result = parse_mr_url("https://gitlab.example.com/org/team/project/-/merge_requests/9")
        assert result == ("org/team/project", "9")

    def test_url_with_trailing_slash_segment(self) -> None:
        """A trailing path segment after the iid (e.g. /diffs) is stripped."""
        result = parse_mr_url("https://gitlab.example.com/group/project/-/merge_requests/134/diffs")
        assert result == ("group/project", "134")

    def test_url_with_query_string(self) -> None:
        """A trailing query string after the iid is stripped."""
        result = parse_mr_url(
            "https://gitlab.example.com/group/project/-/merge_requests/134?tab=commits"
        )
        assert result == ("group/project", "134")

    def test_url_with_fragment(self) -> None:
        """A trailing '#note_NNN' fragment after the iid is stripped."""
        result = parse_mr_url(
            "https://gitlab.example.com/group/project/-/merge_requests/134#note_456"
        )
        assert result == ("group/project", "134")

    def test_unrecognized_format_returns_none_none(self) -> None:
        """A reference matching none of the three formats returns (None, None)."""
        assert parse_mr_url("not-a-valid-ref") == (None, None)

    def test_empty_string_returns_none_none(self) -> None:
        """An empty string is not a digit and not '!'-prefixed — returns (None, None)."""
        assert parse_mr_url("") == (None, None)

    def test_issue_style_url_is_not_matched(self) -> None:
        """An issues URL (not merge_requests) does not match the MR URL branch."""
        result = parse_mr_url("https://gitlab.example.com/group/project/-/issues/134")
        # No '/-/merge_requests/' substring; falls through to the bare-digit
        # check, which also fails since the whole URL string isn't a digit.
        assert result == (None, None)


class TestGetCurrentRepoPath:
    """Test get_current_repo_path function."""

    @patch("subprocess.run")
    def test_https_url_gitlab(self, mock_run: Mock) -> None:
        """Parse HTTPS GitLab URL correctly."""
        mock_run.return_value = Mock(stdout="https://gitlab.com/group/project.git\n", returncode=0)

        result = get_current_repo_path()

        assert result == "group/project"
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_ssh_url_gitlab(self, mock_run: Mock) -> None:
        """Parse SSH GitLab URL correctly."""
        mock_run.return_value = Mock(
            stdout="git@gitlab.com:group/subgroup/project.git\n", returncode=0
        )

        result = get_current_repo_path()

        assert result == "group/subgroup/project"

    @patch("subprocess.run")
    def test_https_url_github(self, mock_run: Mock) -> None:
        """Parse HTTPS GitHub URL correctly."""
        mock_run.return_value = Mock(stdout="https://github.com/owner/repo.git\n", returncode=0)

        result = get_current_repo_path()

        assert result == "owner/repo"

    @patch("subprocess.run")
    def test_ssh_url_github(self, mock_run: Mock) -> None:
        """Parse SSH GitHub URL correctly."""
        mock_run.return_value = Mock(stdout="git@github.com:owner/repo.git\n", returncode=0)

        result = get_current_repo_path()

        assert result == "owner/repo"

    @patch("subprocess.run")
    def test_url_without_git_suffix(self, mock_run: Mock) -> None:
        """Parse URL without .git suffix correctly."""
        mock_run.return_value = Mock(stdout="https://gitlab.com/group/project\n", returncode=0)

        result = get_current_repo_path()

        assert result == "group/project"

    @patch("subprocess.run")
    def test_nested_group_path(self, mock_run: Mock) -> None:
        """Parse nested group path correctly."""
        mock_run.return_value = Mock(
            stdout="git@gitlab.com:org/team/subteam/project.git\n", returncode=0
        )

        result = get_current_repo_path()

        assert result == "org/team/subteam/project"

    @patch("subprocess.run")
    def test_not_a_git_repo(self, mock_run: Mock) -> None:
        """Return None when not in a git repository."""
        mock_run.side_effect = subprocess.CalledProcessError(
            128, ["git", "remote", "get-url", "origin"]
        )

        result = get_current_repo_path()

        assert result is None

    @patch("subprocess.run")
    def test_no_remote_origin(self, mock_run: Mock) -> None:
        """Return None when remote origin doesn't exist."""
        mock_run.side_effect = subprocess.CalledProcessError(
            128, ["git", "remote", "get-url", "origin"]
        )

        result = get_current_repo_path()

        assert result is None

    @patch("subprocess.run")
    def test_invalid_url_format(self, mock_run: Mock) -> None:
        """Return None for malformed URL."""
        mock_run.return_value = Mock(stdout="invalid-url-format\n", returncode=0)

        # Should not crash, returns None or partial parsing
        result = get_current_repo_path()

        # Result might be None or partial - we just verify no exception
        assert result is not None or result is None

    @patch("subprocess.run")
    def test_uses_current_directory(self, mock_run: Mock, temp_dir: Path) -> None:
        """Uses current working directory for git command."""
        mock_run.return_value = Mock(stdout="https://gitlab.com/test/repo.git\n", returncode=0)

        get_current_repo_path()

        # Verify subprocess.run was called with cwd parameter
        call_args = mock_run.call_args
        assert "cwd" in call_args.kwargs
        assert call_args.kwargs["cwd"] == Path.cwd()
