"""Tests for projctl.formatters.ticket_formatter module."""

from typing import Any, Dict, List

import pytest

from projctl.formatters.ticket_formatter import (
    print_epic,
    print_issue,
    print_milestone,
    print_mr,
    print_mr_comments,
)


def _make_issue(**overrides: Any) -> Dict[str, Any]:
    """Return a minimal valid issue dict."""
    base: Dict[str, Any] = {
        "iid": 42,
        "title": "Fix the bug",
        "web_url": "https://gitlab.example.com/proj/-/issues/42",
        "state": "opened",
        "author": {"name": "Alice", "username": "alice"},
        "labels": ["type::bug"],
        "assignees": [],
        "milestone": None,
        "due_date": None,
        "description": "# Description\n\nSome description.",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _make_epic(**overrides: Any) -> Dict[str, Any]:
    """Return a minimal valid epic dict."""
    base: Dict[str, Any] = {
        "iid": 7,
        "title": "Big Feature",
        "web_url": "https://gitlab.example.com/groups/org/-/epics/7",
        "state": "opened",
        "author": {"name": "Bob", "username": "bob"},
        "assignees": [],
        "labels": [],
        "description": "Epic description text.",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _make_milestone(**overrides: Any) -> Dict[str, Any]:
    """Return a minimal valid milestone dict."""
    base: Dict[str, Any] = {
        "iid": 3,
        "title": "v1.0",
        "web_url": "https://gitlab.example.com/proj/-/milestones/3",
        "state": "active",
        "start_date": None,
        "due_date": None,
        "description": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _make_mr(**overrides: Any) -> Dict[str, Any]:
    """Return a minimal valid MR dict."""
    base: Dict[str, Any] = {
        "iid": 99,
        "title": "Add feature",
        "web_url": "https://gitlab.example.com/proj/-/merge_requests/99",
        "state": "opened",
        "author": {"name": "Alice", "username": "alice"},
        "draft": False,
        "source_branch": "feature",
        "target_branch": "main",
        "labels": [],
        "assignees": [],
        "reviewers": [],
        "milestone": None,
        "description": "MR description.",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "merged_at": None,
        "pipeline": None,
    }
    base.update(overrides)
    return base


class TestPrintIssue:
    """Tests for print_issue."""

    def test_print_issue_heading(self, capsys) -> None:
        """Output contains the '# Issue #' heading with correct iid and title."""
        print_issue(_make_issue())
        out = capsys.readouterr().out
        assert "# Issue #42: Fix the bug" in out

    def test_print_issue_state_and_author(self, capsys) -> None:
        """Output contains State and Author fields."""
        print_issue(_make_issue())
        out = capsys.readouterr().out
        assert "**State:** opened" in out
        assert "**Author:** Alice" in out

    def test_print_issue_with_timing_shows_status(self, capsys) -> None:
        """When timing has current_status, Status line appears."""
        timing = {"current_status": "In progress"}
        print_issue(_make_issue(), timing=timing)
        out = capsys.readouterr().out
        assert "**Status:** In progress" in out

    def test_print_issue_with_links_shows_blocked_by(self, capsys) -> None:
        """When links has blocked_by entries, Blocked By section appears."""
        links = {
            "blocked_by": [
                {"iid": 10, "title": "Blocker", "state": "opened", "web_url": "http://x/10"}
            ],
            "blocking": [],
        }
        print_issue(_make_issue(), links=links)
        out = capsys.readouterr().out
        assert "Blocked By" in out


class TestPrintIssueTimingBranches:
    """Tests for _print_timing branches via print_issue."""

    def test_timing_rejected(self, capsys) -> None:
        """Rejected timing shows 'Time counted: No (rejected)'."""
        timing = {"is_rejected": True}
        print_issue(_make_issue(), timing=timing)
        out = capsys.readouterr().out
        assert "**Time counted:** No (rejected)" in out

    def test_timing_start_date_only(self, capsys) -> None:
        """Start date present shows Started line but no Completed line."""
        timing = {"start_date": "2026-01-10", "end_date": None}
        print_issue(_make_issue(), timing=timing)
        out = capsys.readouterr().out
        assert "**Started:** 2026-01-10" in out
        assert "**Completed:**" not in out

    def test_timing_end_date_only(self, capsys) -> None:
        """End date present shows Completed line but no Started line."""
        timing = {"start_date": None, "end_date": "2026-02-01"}
        print_issue(_make_issue(), timing=timing)
        out = capsys.readouterr().out
        assert "**Completed:** 2026-02-01" in out
        assert "**Started:**" not in out

    def test_timing_both_dates(self, capsys) -> None:
        """Both start and end dates present shows both lines."""
        timing = {"start_date": "2026-01-10", "end_date": "2026-02-01"}
        print_issue(_make_issue(), timing=timing)
        out = capsys.readouterr().out
        assert "**Started:** 2026-01-10" in out
        assert "**Completed:** 2026-02-01" in out


class TestPrintEpic:
    """Tests for print_epic."""

    def test_print_epic_heading(self, capsys) -> None:
        """Output contains '# Epic &' heading with correct iid and title."""
        print_epic(_make_epic(), issues=[])
        out = capsys.readouterr().out
        assert "# Epic &7: Big Feature" in out

    def test_print_epic_issue_count_line(self, capsys) -> None:
        """Issues section heading shows correct count."""
        issues = [
            {
                "iid": 1,
                "title": "Issue A",
                "state": "opened",
                "web_url": "http://x/1",
                "assignees": [],
                "labels": [],
            }
        ]
        print_epic(_make_epic(), issues=issues)
        out = capsys.readouterr().out
        assert "Issues in Epic (1)" in out

    def test_print_epic_description_section(self, capsys) -> None:
        """Output contains Description section."""
        print_epic(_make_epic(), issues=[])
        out = capsys.readouterr().out
        assert "Description" in out


class TestPrintMilestone:
    """Tests for print_milestone."""

    def test_print_milestone_heading(self, capsys) -> None:
        """Output contains '# Milestone %' heading with correct iid and title."""
        print_milestone(_make_milestone(), issues=[], epic_map={})
        out = capsys.readouterr().out
        assert "# Milestone %3: v1.0" in out

    def test_print_milestone_progress_line(self, capsys) -> None:
        """Output contains Progress line showing closed/total."""
        issues = [
            {"iid": 1, "title": "A", "state": "closed"},
            {"iid": 2, "title": "B", "state": "opened"},
        ]
        print_milestone(_make_milestone(), issues=issues, epic_map={})
        out = capsys.readouterr().out
        assert "**Progress:** 1/2 issues closed" in out


class TestPrintMR:
    """Tests for print_mr."""

    def test_print_mr_heading(self, capsys) -> None:
        """Output contains '# MR !' heading with correct iid and title."""
        print_mr(_make_mr())
        out = capsys.readouterr().out
        assert "# MR !99: Add feature" in out

    def test_print_mr_state_and_author(self, capsys) -> None:
        """Output contains State and Author fields."""
        print_mr(_make_mr())
        out = capsys.readouterr().out
        assert "**State:** opened" in out
        assert "**Author:** Alice" in out


class TestPrintMrComments:
    """Test MR comment rendering, including the thread id needed to act on a comment."""

    @staticmethod
    def _comment(**overrides: Any) -> Dict[str, Any]:
        base = {
            "id": 9001,
            "discussion_id": "thread-abc123",
            "author": "Reviewer One",
            "body": "This assertion is not scoped to the build job",
            "resolvable": True,
            "resolved": False,
            "file_path": "ci/audit.py",
            "line": 42,
        }
        base.update(overrides)
        return base

    def test_resolvable_comment_shows_its_thread_id(self, capsys: Any) -> None:
        """Without it, a reader has the finding but no way to resolve or reply to it —
        the id is not derivable from anything else printed."""
        print_mr_comments([self._comment()])

        # Pins the full rendered token, backticks included: `projctl/CLAUDE.md`
        # documents that exact form.
        assert "`thread: thread-abc123`" in capsys.readouterr().out

    def test_thread_id_line_appears_between_author_and_body(self, capsys: Any) -> None:
        """Position is the only thing binding a printed id to its comment, so the
        thread line must render after the author heading and before the body."""
        print_mr_comments([self._comment()])

        out = capsys.readouterr().out
        assert out.index("Reviewer One") < out.index("thread: thread-abc123")
        assert out.index("thread: thread-abc123") < out.index("This assertion is not scoped")

    def test_unresolvable_comment_still_shows_its_thread_id(self, capsys: Any) -> None:
        """An individual top-level note is unresolvable but is still a valid `replies:`
        target, so withholding its id would strand the one comment kind that has no
        other way to be answered in-thread."""
        # A real unresolvable top-level note has no diff position, so file_path/line
        # must be empty here too, matching the shape load_mr_comments() actually
        # emits for an unresolvable note — this guards the resolvable-gating defect
        # this test was written to catch.
        print_mr_comments(
            [self._comment(resolvable=False, discussion_id="thread-def456", file_path="", line="")]
        )

        assert "thread: thread-def456" in capsys.readouterr().out

    def test_resolved_comment_shows_its_thread_id(self, capsys: Any) -> None:
        """Resolved threads render through a separate branch of print_mr_comments, so
        the id must survive the open/resolved split."""
        print_mr_comments([self._comment(resolved=True, discussion_id="thread-ghi789")])

        out = capsys.readouterr().out
        assert "Resolved" in out
        assert "thread: thread-ghi789" in out

    def test_empty_thread_id_prints_no_placeholder(self, capsys: Any) -> None:
        """An empty id renders nothing rather than an empty `thread:` line that would
        read as a real, copyable value."""
        print_mr_comments([self._comment(discussion_id="")])

        assert "thread:" not in capsys.readouterr().out

    def test_missing_thread_id_key_prints_no_placeholder(self, capsys: Any) -> None:
        """A comment dict that omits discussion_id entirely — a legitimate shape for
        any caller of the exported print_mr_comments, not just this module's loader —
        must fall back to the same default as an empty string, not raise KeyError."""
        comment = self._comment()
        del comment["discussion_id"]

        print_mr_comments([comment])

        assert "thread:" not in capsys.readouterr().out

    def test_each_comments_thread_id_matches_its_own_body(self, capsys: Any) -> None:
        """Guards against attributing one comment's thread id to a different comment
        when two or more are printed together — a misattributed id is worse than a
        missing one, since a reader would paste it into `resolve:` and close the
        wrong thread."""
        comment_a = self._comment(discussion_id="thread-aaa", body="Body from comment A")
        comment_b = self._comment(discussion_id="thread-bbb", body="Body from comment B")

        print_mr_comments([comment_a, comment_b])

        out = capsys.readouterr().out
        assert out.index("thread-aaa") < out.index("Body from comment B")
        assert out.index("thread-bbb") > out.index("Body from comment A")

    def test_body_and_author_still_render(self, capsys: Any) -> None:
        """The added line must not displace what the report already carried."""
        print_mr_comments([self._comment()])

        out = capsys.readouterr().out
        assert "Reviewer One" in out
        assert "This assertion is not scoped to the build job" in out
