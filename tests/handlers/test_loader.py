"""Tests for projctl.handlers.loader module."""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock, patch

import pytest

from projctl.config import Config
from projctl.exceptions import PlatformError
from projctl.formatters import format_user as _format_user, format_users as _format_users
from projctl.handlers.loader import TicketLoader


class TestTicketLoaderInit:
    """Test TicketLoader initialization."""

    def test_init(self, new_config_path: Path) -> None:
        """Loader initializes correctly."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        assert loader.config == config
        assert loader.group == "test/group"


class TestParseReference:
    """Test reference parsing."""

    def test_parse_issue_number(self, new_config_path: Path) -> None:
        """Parse issue number reference."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        ref_type, ref_id, project = loader.parse_reference("#123")

        assert ref_type == "issue"
        assert ref_id == "123"

    def test_parse_epic_reference(self, new_config_path: Path) -> None:
        """Parse epic reference with & prefix."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        ref_type, ref_id, project = loader.parse_reference("&21")

        assert ref_type == "epic"
        assert ref_id == "21"

    def test_parse_milestone_reference(self, new_config_path: Path) -> None:
        """Parse milestone reference with % prefix."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        ref_type, ref_id, project = loader.parse_reference("%123")

        assert ref_type == "milestone"
        assert ref_id == "123"

    def test_parse_mr_reference(self, new_config_path: Path) -> None:
        """Parse MR reference with ! prefix."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        ref_type, ref_id, project = loader.parse_reference("!134")

        assert ref_type == "mr"
        assert ref_id == "134"

    def test_parse_issue_url(self, new_config_path: Path) -> None:
        """Parse issue URL."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        url = "https://gitlab.example.com/group/project/-/issues/123"
        ref_type, ref_id, project = loader.parse_reference(url)

        assert ref_type == "issue"
        assert ref_id == "123"
        assert project == "group/project"

    def test_parse_epic_url(self, new_config_path: Path) -> None:
        """Parse epic URL."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        url = "https://gitlab.example.com/groups/test/-/epics/21"
        ref_type, ref_id, project = loader.parse_reference(url)

        assert ref_type == "epic"
        assert ref_id == "21"

    def test_parse_mr_url(self, new_config_path: Path) -> None:
        """Parse MR URL."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        url = "https://gitlab.example.com/group/project/-/merge_requests/134"
        ref_type, ref_id, project = loader.parse_reference(url)

        assert ref_type == "mr"
        assert ref_id == "134"

    def test_parse_plain_number(self, new_config_path: Path) -> None:
        """Parse plain number as issue."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        ref_type, ref_id, project = loader.parse_reference("123")

        assert ref_type == "issue"
        assert ref_id == "123"

    def test_parse_invalid_reference(self, new_config_path: Path) -> None:
        """Invalid reference raises ValueError."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        with pytest.raises(ValueError, match="Invalid reference"):
            loader.parse_reference("invalid")


class TestLoadIssue:
    """Test issue loading."""

    @patch("subprocess.run")
    def test_load_issue_success(
        self, mock_run: Mock, new_config_path: Path, mock_glab_issue_view: str
    ) -> None:
        """Load issue successfully."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        mock_run.return_value = Mock(stdout=mock_glab_issue_view, stderr="", returncode=0)

        result = loader.load_issue("#1")

        assert result is not None
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_load_issue_with_project(
        self, mock_run: Mock, new_config_path: Path, mock_glab_issue_view: str
    ) -> None:
        """Load issue with specific project."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        mock_run.return_value = Mock(stdout=mock_glab_issue_view, returncode=0)

        result = loader.load_issue("#1", project="group/project")

        # Verify project was encoded and passed in the API endpoint
        call_args = mock_run.call_args[0][0]
        joined = " ".join(call_args)
        # The project path is URL-encoded (/ → %2F) in the API endpoint
        assert "group%2Fproject" in joined or "group/project" in joined

    @patch("subprocess.run")
    def test_load_issue_command_failure(self, mock_run: Mock, new_config_path: Path) -> None:
        """Issue loading failure raises PlatformError."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["glab", "issue", "view"], stderr="Error loading issue"
        )

        with pytest.raises(PlatformError, match="Command failed"):
            loader.load_issue("#1")


class TestLoadEpic:
    """Test epic loading."""

    @patch("subprocess.run")
    def test_load_epic_success(
        self, mock_run: Mock, new_config_path: Path, mock_glab_epic_view: str
    ) -> None:
        """Load epic successfully."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        graphql_no_assignees = json.dumps(
            {
                "data": {
                    "group": {
                        "workItem": {"widgets": [{"type": "ASSIGNEES", "assignees": {"nodes": []}}]}
                    }
                }
            }
        )
        # Call order: REST epic data, GraphQL assignees
        mock_run.side_effect = [
            Mock(stdout=mock_glab_epic_view, returncode=0),
            Mock(stdout=graphql_no_assignees, returncode=0),
        ]

        result = loader.load_epic("&21")

        assert result is not None


class TestLoadMR:
    """Test MR loading."""

    @patch("subprocess.run")
    def test_load_mr_success(
        self, mock_run: Mock, new_config_path: Path, mock_glab_mr_view: str
    ) -> None:
        """Load MR successfully."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        mock_run.return_value = Mock(stdout=mock_glab_mr_view, returncode=0)

        result = loader.load_mr("!134")

        assert result is not None
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_load_mr_with_project(
        self, mock_run: Mock, new_config_path: Path, mock_glab_mr_view: str
    ) -> None:
        """Load MR with specific project (project kwarg accepted without error)."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        mock_run.return_value = Mock(stdout=mock_glab_mr_view, returncode=0)

        result = loader.load_mr("!134", project="group/project")

        # Verify the command was executed (project kwarg does not raise)
        mock_run.assert_called_once()


class TestLoadMilestone:
    """Test milestone loading."""

    @patch("subprocess.run")
    def test_load_milestone_success(self, mock_run: Mock, new_config_path: Path) -> None:
        """Load milestone successfully."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        milestone_data = {
            "id": 123,
            "iid": 1,
            "title": "v1.0",
            "state": "active",
            "description": "Milestone description",
        }

        # Call order when default_group is set:
        # 1. GET groups/{group}/milestones?per_page=100  (iid→id lookup)
        # 2. GET groups/{group}/milestones/{id}          (milestone data)
        # 3. GET groups/{group}/milestones/{id}/issues   (issues list)
        milestones_list = [milestone_data]
        mock_run.side_effect = [
            Mock(stdout=json.dumps(milestones_list), returncode=0),
            Mock(stdout=json.dumps(milestone_data), returncode=0),
            Mock(stdout="[]", returncode=0),
        ]

        result = loader.load_milestone("%1")

        assert result is not None


class TestFormatting:
    """Test output formatting."""

    @patch("subprocess.run")
    def test_markdown_output_format(
        self, mock_run: Mock, new_config_path: Path, mock_glab_issue_view: str, capsys
    ) -> None:
        """Issue output is formatted as markdown."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        mock_run.return_value = Mock(stdout=mock_glab_issue_view, returncode=0)

        loader.load_issue("#1")

        captured = capsys.readouterr()
        # Verify actual heading with the IID from mock data
        assert "# Issue #1: Test Issue" in captured.out

    @patch("subprocess.run")
    def test_includes_metadata(
        self, mock_run: Mock, new_config_path: Path, mock_glab_issue_view: str, capsys
    ) -> None:
        """Output includes issue metadata with correct values from mock data."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        mock_run.return_value = Mock(stdout=mock_glab_issue_view, returncode=0)

        loader.load_issue("#1")

        captured = capsys.readouterr()
        assert "**State:** opened" in captured.out
        assert "**Labels:**" in captured.out
        assert "`type::feature`" in captured.out


class TestFormatUser:
    """Test _format_user and _format_users helpers."""

    def test_format_user_with_name_and_username(self) -> None:
        """Full user dict renders as 'Name (@username)'."""
        user = {"name": "Alex Stavonin", "username": "alex.stavonin"}
        assert _format_user(user) == "Alex Stavonin (@alex.stavonin)"

    def test_format_user_username_only(self) -> None:
        """User with no name falls back to username."""
        user = {"username": "alex.stavonin"}
        assert _format_user(user) == "alex.stavonin (@alex.stavonin)"

    def test_format_user_name_only(self) -> None:
        """User with name but no username renders without (@...) suffix."""
        user = {"name": "Alice"}
        assert _format_user(user) == "Alice"

    def test_format_user_empty_dict(self) -> None:
        """Empty dict returns '?'."""
        assert _format_user({}) == "?"

    def test_format_users_multiple(self) -> None:
        """Multiple users are comma-separated."""
        users = [
            {"name": "Alice", "username": "alice"},
            {"name": "Bob", "username": "bob"},
        ]
        assert _format_users(users) == "Alice (@alice), Bob (@bob)"

    def test_format_users_empty(self) -> None:
        """Empty list returns empty string."""
        assert _format_users([]) == ""


class TestGetStatusHistory:
    """Test _get_status_history."""

    def test_returns_chronological_order(self, new_config_path: Path) -> None:
        """Notes are reversed from newest-first to oldest-first."""
        notes = [
            {
                "system": True,
                "body": "set status to **Done**",
                "created_at": "2026-03-25T10:00:00Z",
            },
            {
                "system": True,
                "body": "set status to **In progress**",
                "created_at": "2026-03-10T08:00:00Z",
            },
            {
                "system": True,
                "body": "set status to **To do**",
                "created_at": "2026-03-01T09:00:00Z",
            },
        ]
        config = Config(new_config_path)
        loader = TicketLoader(config)

        with patch.object(loader, "_run_glab_command", return_value=json.dumps(notes)):
            history = loader._get_status_history(1403, 22)

        assert [h["status"] for h in history] == ["To do", "In progress", "Done"]
        assert history[0]["timestamp"] == "2026-03-01T09:00:00Z"

    def test_ignores_non_system_notes(self, new_config_path: Path) -> None:
        """Non-system notes are skipped."""
        notes = [
            {
                "system": False,
                "body": "set status to **Done**",
                "created_at": "2026-03-25T10:00:00Z",
            },
            {
                "system": True,
                "body": "set status to **In progress**",
                "created_at": "2026-03-10T08:00:00Z",
            },
        ]
        config = Config(new_config_path)
        loader = TicketLoader(config)

        with patch.object(loader, "_run_glab_command", return_value=json.dumps(notes)):
            history = loader._get_status_history(1403, 22)

        assert len(history) == 1
        assert history[0]["status"] == "In progress"

    def test_returns_empty_on_error(self, new_config_path: Path) -> None:
        """PlatformError returns empty list without raising."""
        config = Config(new_config_path)
        loader = TicketLoader(config)

        with patch.object(loader, "_run_glab_command", side_effect=PlatformError("fail")):
            history = loader._get_status_history(1403, 22)

        assert history == []


class TestComputeTiming:
    """Test _compute_timing."""

    def test_in_progress_then_done(self, new_config_path: Path) -> None:
        """Normal flow: To do → In progress → Done."""
        history = [
            {"status": "To do", "timestamp": "2026-03-01T09:00:00Z"},
            {"status": "In progress", "timestamp": "2026-03-10T08:00:00Z"},
            {"status": "Done", "timestamp": "2026-03-25T10:00:00Z"},
        ]
        loader = TicketLoader(Config(new_config_path))
        result = loader._compute_timing(history)

        assert result["current_status"] == "Done"
        assert result["start_date"] == "2026-03-10T08:00:00Z"
        assert result["end_date"] == "2026-03-25T10:00:00Z"
        assert result["is_rejected"] is False

    def test_todo_to_done_no_in_progress(self, new_config_path: Path) -> None:
        """To do → Done directly: start_date is None, end_date is set."""
        history = [
            {"status": "To do", "timestamp": "2026-03-01T09:00:00Z"},
            {"status": "Done", "timestamp": "2026-03-25T10:00:00Z"},
        ]
        loader = TicketLoader(Config(new_config_path))
        result = loader._compute_timing(history)

        assert result["current_status"] == "Done"
        assert result["start_date"] is None
        assert result["end_date"] == "2026-03-25T10:00:00Z"
        assert result["is_rejected"] is False

    def test_duplicate_is_rejected(self, new_config_path: Path) -> None:
        """Duplicate status: no dates, is_rejected True."""
        history = [
            {"status": "To do", "timestamp": "2026-03-01T09:00:00Z"},
            {"status": "Duplicate", "timestamp": "2026-03-30T08:00:00Z"},
        ]
        loader = TicketLoader(Config(new_config_path))
        result = loader._compute_timing(history)

        assert result["current_status"] == "Duplicate"
        assert result["start_date"] is None
        assert result["end_date"] is None
        assert result["is_rejected"] is True

    def test_wont_do_is_rejected(self, new_config_path: Path) -> None:
        """Won't do status: no dates, is_rejected True."""
        history = [
            {"status": "To do", "timestamp": "2026-03-01T09:00:00Z"},
            {"status": "Won't do", "timestamp": "2026-04-01T08:00:00Z"},
        ]
        loader = TicketLoader(Config(new_config_path))
        result = loader._compute_timing(history)

        assert result["is_rejected"] is True
        assert result["start_date"] is None
        assert result["end_date"] is None

    def test_cycled_back_uses_first_in_progress_and_last_done(self, new_config_path: Path) -> None:
        """To do → In progress → To do → In progress → Done: first start, last end."""
        history = [
            {"status": "To do", "timestamp": "2026-03-01T09:00:00Z"},
            {"status": "In progress", "timestamp": "2026-03-10T08:00:00Z"},
            {"status": "To do", "timestamp": "2026-03-15T09:00:00Z"},
            {"status": "In progress", "timestamp": "2026-03-17T08:00:00Z"},
            {"status": "Done", "timestamp": "2026-03-25T10:00:00Z"},
        ]
        loader = TicketLoader(Config(new_config_path))
        result = loader._compute_timing(history)

        assert result["start_date"] == "2026-03-10T08:00:00Z"
        assert result["end_date"] == "2026-03-25T10:00:00Z"

    def test_empty_history(self, new_config_path: Path) -> None:
        """Empty history returns all None fields."""
        loader = TicketLoader(Config(new_config_path))
        result = loader._compute_timing([])

        assert result["current_status"] is None
        assert result["start_date"] is None
        assert result["end_date"] is None
        assert result["is_rejected"] is False


class TestDeriveEpicDates:
    """Test _derive_epic_dates."""

    def test_all_done_with_in_progress(self) -> None:
        """All non-rejected issues done: returns earliest start, latest end."""
        issues = [
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": "2026-03-10T08:00:00Z",
                    "end_date": "2026-03-20T10:00:00Z",
                }
            },
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": "2026-03-05T09:00:00Z",
                    "end_date": "2026-03-25T10:00:00Z",
                }
            },
        ]
        result = TicketLoader._derive_epic_dates(issues)

        assert result["start_date"] == "2026-03-05T09:00:00Z"
        assert result["end_date"] == "2026-03-25T10:00:00Z"

    def test_any_unfinished_clears_end_date(self) -> None:
        """Any non-rejected issue without end_date → epic end is None."""
        issues = [
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": "2026-03-10T08:00:00Z",
                    "end_date": "2026-03-20T10:00:00Z",
                }
            },
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": "2026-03-12T08:00:00Z",
                    "end_date": None,
                }
            },
        ]
        result = TicketLoader._derive_epic_dates(issues)

        assert result["start_date"] == "2026-03-10T08:00:00Z"
        assert result["end_date"] is None

    def test_rejected_issues_excluded(self) -> None:
        """Rejected issues do not affect dates."""
        issues = [
            {"timing": {"is_rejected": True, "start_date": None, "end_date": None}},
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": "2026-03-10T08:00:00Z",
                    "end_date": "2026-03-20T10:00:00Z",
                }
            },
        ]
        result = TicketLoader._derive_epic_dates(issues)

        assert result["start_date"] == "2026-03-10T08:00:00Z"
        assert result["end_date"] == "2026-03-20T10:00:00Z"

    def test_todo_to_done_no_start(self) -> None:
        """Issues without start_date (To do → Done) don't contribute to epic start."""
        issues = [
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": None,
                    "end_date": "2026-03-20T10:00:00Z",
                }
            },
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": "2026-03-10T08:00:00Z",
                    "end_date": "2026-03-25T10:00:00Z",
                }
            },
        ]
        result = TicketLoader._derive_epic_dates(issues)

        assert result["start_date"] == "2026-03-10T08:00:00Z"
        assert result["end_date"] == "2026-03-25T10:00:00Z"

    def test_all_issues_no_start_dates(self) -> None:
        """All issues went To do → Done: epic start is None."""
        issues = [
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": None,
                    "end_date": "2026-03-20T10:00:00Z",
                }
            },
            {
                "timing": {
                    "is_rejected": False,
                    "start_date": None,
                    "end_date": "2026-03-25T10:00:00Z",
                }
            },
        ]
        result = TicketLoader._derive_epic_dates(issues)

        assert result["start_date"] is None
        assert result["end_date"] == "2026-03-25T10:00:00Z"

    def test_empty_issues(self) -> None:
        """No issues → both dates None."""
        result = TicketLoader._derive_epic_dates([])

        assert result["start_date"] is None
        assert result["end_date"] is None

    def test_all_rejected(self) -> None:
        """All issues rejected → both dates None."""
        issues = [
            {"timing": {"is_rejected": True, "start_date": None, "end_date": None}},
            {"timing": {"is_rejected": True, "start_date": None, "end_date": None}},
        ]
        result = TicketLoader._derive_epic_dates(issues)

        assert result["start_date"] is None
        assert result["end_date"] is None


class TestLoadMrComments:
    """Test MR comment loading, including thread identity."""

    _RAW_DISCUSSIONS = [
        {
            "id": "thread-abc123",
            "notes": [
                {
                    "id": 9001,
                    "system": False,
                    "body": "Major: this assertion is not scoped to the build job",
                    "resolvable": True,
                    "resolved": False,
                    "author": {"name": "Reviewer One"},
                    "position": {"new_path": "ci/audit.py", "new_line": 42},
                    "created_at": "2026-08-07T09:00:00Z",
                },
                # An author reply in the same thread — the shape that distinguishes a
                # per-thread id from a per-note one.
                {
                    "id": 9004,
                    "system": False,
                    "body": "Scoped it to the build job",
                    "resolvable": True,
                    "resolved": False,
                    "author": {"name": "Author"},
                    "position": {"new_path": "ci/audit.py", "new_line": 42},
                    "created_at": "2026-08-07T09:10:00Z",
                },
            ],
        },
        {
            "id": "thread-def456",
            "notes": [
                {
                    "id": 9002,
                    "system": True,
                    "body": "assigned to @someone",
                    "author": {"name": "GitLab"},
                },
                {
                    "id": 9003,
                    "system": False,
                    "body": "General remark, not on a diff line",
                    "resolvable": False,
                    "resolved": False,
                    "author": {"name": "Reviewer Two"},
                    "created_at": "2026-08-07T09:05:00Z",
                },
            ],
        },
    ]

    def _load_full(self, new_config_path: Path, discussions: Any = None) -> Dict[str, Any]:
        """Run load_mr_comments() against the subprocess boundary and return the envelope."""
        loader = TicketLoader(Config(new_config_path))
        payload = self._RAW_DISCUSSIONS if discussions is None else discussions
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(stdout=json.dumps({"iid": 235}), returncode=0),
                Mock(stdout=json.dumps(payload), returncode=0),
            ]
            return loader.load_mr_comments("235")

    def _load(self, new_config_path: Path) -> list:
        return self._load_full(new_config_path)["comments"]

    def test_each_comment_carries_its_enclosing_thread_id(self, new_config_path: Path) -> None:
        """`resolve:`/`replies:` in a review YAML take the discussion id, not the note
        id, so dropping it makes the loaded output unusable for resolving a thread."""
        comments = self._load(new_config_path)

        assert [c["discussion_id"] for c in comments] == [
            "thread-abc123",
            "thread-abc123",
            "thread-def456",
        ]

    def test_all_notes_in_one_thread_share_that_thread_id(self, new_config_path: Path) -> None:
        """Every note in a discussion carries the discussion's id, not its own.

        A per-note id would pass any single-note fixture yet make a reply to the
        second note POST to /discussions/<note_id>, which GitLab 404s.
        """
        comments = self._load(new_config_path)
        first, reply = comments[0], comments[1]

        assert first["discussion_id"] == reply["discussion_id"] == "thread-abc123"
        assert first["id"] != reply["id"]

    def test_note_id_and_thread_id_stay_distinct(self, new_config_path: Path) -> None:
        """The two ids address different resources; one must never stand in for the
        other, since the discussions endpoint rejects a note id."""
        comments = self._load(new_config_path)

        assert comments[0]["id"] == 9001
        assert comments[0]["discussion_id"] == "thread-abc123"

    def test_system_notes_are_still_filtered_out(self, new_config_path: Path) -> None:
        """Threading data must not resurrect the system notes the loader excludes."""
        comments = self._load(new_config_path)

        assert len(comments) == 3
        assert all("assigned to" not in c["body"] for c in comments)

    def test_thread_id_defaults_to_empty_when_absent(self, new_config_path: Path) -> None:
        """A discussion without an id must not raise — the note is still worth showing."""
        comments = self._load_full(
            new_config_path, [{"notes": [{"id": 1, "system": False, "body": "hi"}]}]
        )["comments"]

        assert comments[0]["discussion_id"] == ""

    def test_explicit_null_thread_id_becomes_empty_string(self, new_config_path: Path) -> None:
        """A JSON null must normalize to "" like a missing key does.

        dict.get(key, "") returns None for an explicit null, which would violate the
        str type load_mr_comments() documents for discussion_id.
        """
        comments = self._load_full(
            new_config_path,
            [{"id": None, "notes": [{"id": 1, "system": False, "body": "hi"}]}],
        )["comments"]

        assert comments[0]["discussion_id"] == ""

    def test_comment_dict_shape(self, new_config_path: Path) -> None:
        """Full contract of one comment dict.

        TestLoadMrComments is the only coverage load_mr_comments() has anywhere in
        the suite, so any key not asserted here can drift or disappear silently.
        """
        comments = self._load(new_config_path)

        assert comments[0] == {
            "id": 9001,
            "discussion_id": "thread-abc123",
            "author": "Reviewer One",
            "body": "Major: this assertion is not scoped to the build job",
            "resolvable": True,
            "resolved": False,
            "file_path": "ci/audit.py",
            "line": 42,
            "created_at": "2026-08-07T09:00:00Z",
        }

    def test_positionless_note_emits_empty_file_path_and_line(self, new_config_path: Path) -> None:
        """A note with no `position` yields empty strings, not placeholders.

        The formatter's regression fixture for the thread-id defect hard-codes
        file_path="" / line="" as the shape a top-level note takes, so that guard
        is only as good as this producer-side assertion.
        """
        comments = self._load(new_config_path)

        assert comments[2] == {
            "id": 9003,
            "discussion_id": "thread-def456",
            "author": "Reviewer Two",
            "body": "General remark, not on a diff line",
            "resolvable": False,
            "resolved": False,
            "file_path": "",
            "line": "",
            "created_at": "2026-08-07T09:05:00Z",
        }

    def test_discussions_are_fetched_with_the_note_list_argv(self, new_config_path: Path) -> None:
        """`mr note list` is what returns discussion objects; `mr view` returns none.

        The mock replays responses by call order, so every other test here passes
        whatever subcommand is issued — this is the only assertion pinning it.
        """
        loader = TicketLoader(Config(new_config_path))
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(stdout=json.dumps({"iid": 235}), returncode=0),
                Mock(stdout=json.dumps(self._RAW_DISCUSSIONS), returncode=0),
            ]
            loader.load_mr_comments("235")

        assert mock_run.call_args_list[1].args[0] == [
            "glab",
            "mr",
            "note",
            "list",
            "235",
            "--output",
            "json",
        ]

    def test_envelope_carries_mr_metadata(self, new_config_path: Path) -> None:
        """print_mr_info() does `print_mr(data["mr"])`, so an empty envelope is not
        enough — the metadata itself has to survive."""
        result = self._load_full(new_config_path)

        assert result["mr"] == {"iid": 235}

    def test_print_mr_info_includes_comment_thread_id(self, new_config_path: Path, capsys) -> None:
        """Composition test for the path the CLI actually runs: load_mr_comments()
        feeding print_mr_info(..., with_comments=True). The two halves are tested in
        isolation elsewhere, which cannot catch the producer and consumer drifting
        apart — e.g. a key rename on one side with only that side's tests updated.

        Covers the unresolvable thread too: that is the shape the recorded observed
        failure was about, and it is otherwise guarded only by a hand-built fixture.
        """
        data = self._load_full(new_config_path)
        TicketLoader(Config(new_config_path)).print_mr_info(data, with_comments=True)

        out = capsys.readouterr().out
        assert "thread: thread-abc123" in out
        assert "thread: thread-def456" in out

    def test_print_mr_info_omits_comments_when_not_requested(
        self, new_config_path: Path, capsys
    ) -> None:
        """Without --comments the listing must not appear.

        `cli.py` passes the flag straight through, so a gate that ignores it would
        print review comments on every plain `projctl load mr N`.
        """
        data = self._load_full(new_config_path)
        TicketLoader(Config(new_config_path)).print_mr_info(data, with_comments=False)

        out = capsys.readouterr().out
        assert "thread: thread-abc123" not in out
        assert "Review Comments" not in out

    def test_load_mr_comments_propagates_platform_error(self, new_config_path: Path) -> None:
        """Pins the method's error contract against its declared `Raises`: unlike
        _get_status_history, which swallows PlatformError and returns [], this method
        must propagate it rather than silently returning partial data."""
        loader = TicketLoader(Config(new_config_path))
        with patch.object(loader, "_run_glab_command", side_effect=PlatformError("fail")):
            with pytest.raises(PlatformError):
                loader.load_mr_comments("235")
