"""Tests for the 'activity' subcommand (projctl.handlers.activity, projctl.cli.cmd_activity).

Purely local git, so every test here mocks subprocess.run directly with a
fixed transcript — never a real repository — matching the module's own
"no network, no GitLab API" contract.
"""

import json
import os
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, List, Optional
from unittest.mock import patch

import pytest

from projctl.cli import cmd_activity, main
from projctl.exceptions import PlatformError
from projctl.handlers.activity import ActivityHandler, ActivityReport, IssueActivity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# UTC+6, matching the offset the design doc's own local-day reasoning is
# pinned to ("this host runs at UTC+6").
_FIXED_TZ = timezone(timedelta(hours=6))
_TARGET_DAY = date(2026, 8, 13)


def _completed(
    cmd: List[str], returncode: int, stdout: str, stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
    )


def _git_stub(
    *,
    toplevel: str = "/repo",
    toplevel_returncode: int = 0,
    reflog: str = "",
    status: str = "",
    branch: str = "",
    calls: Optional[List[List[str]]] = None,
) -> Callable[..., subprocess.CompletedProcess]:
    """Build a subprocess.run replacement keyed by git subcommand.

    Dispatching by subcommand name (rather than an ordered side_effect
    list) means each test only has to supply the transcripts relevant to
    the evidence path it exercises, and stays correct even if report()'s
    internal call order changes. `calls`, when given, records every
    invoked argv so a test can assert a subcommand was (or was not)
    consulted — e.g. 'branch' must never be called when no file was
    modified on the date.
    """

    def _run(cmd: List[str], **_kwargs) -> subprocess.CompletedProcess:
        assert cmd[0] == "git"
        if calls is not None:
            calls.append(cmd)
        sub = cmd[1]
        if sub == "rev-parse":
            return _completed(cmd, toplevel_returncode, toplevel)
        if sub == "reflog":
            return _completed(cmd, 0, reflog)
        if sub == "status":
            return _completed(cmd, 0, status)
        if sub == "branch":
            return _completed(cmd, 0, branch)
        raise AssertionError(f"unexpected git subcommand invoked: {cmd}")

    return _run


def _reflog_line(sha: str, iso_dt: str, subject: str) -> str:
    return f"{sha} HEAD@{{{iso_dt}}}: {subject}"


def _set_mtime(path: Path, day: date, tz=_FIXED_TZ) -> None:
    """Set a file's mtime to local noon on `day`, in `tz`."""
    ts = datetime.combine(day, time(12, 0), tzinfo=tz).timestamp()
    os.utime(path, (ts, ts))


# ---------------------------------------------------------------------------
# _parse_status_entries — pure parsing, no subprocess involved
# ---------------------------------------------------------------------------


class TestParseStatusEntries:
    """Parsing of `git status --porcelain -z` output."""

    def test_untracked_file(self) -> None:
        output = "?? file.txt\0"
        assert ActivityHandler._parse_status_entries(output) == ["file.txt"]

    def test_path_with_space(self) -> None:
        output = "?? un tracked.txt\0"
        assert ActivityHandler._parse_status_entries(output) == ["un tracked.txt"]

    def test_rename_entry_yields_only_the_new_path(self) -> None:
        """A rename carries an extra NUL field (the pre-rename path) that must be
        consumed and discarded, not treated as a second entry."""
        output = "RM new name.txt\0old name.txt\0"
        assert ActivityHandler._parse_status_entries(output) == ["new name.txt"]

    def test_copy_entry_also_consumes_the_original_path_field(self) -> None:
        output = "C  copied.txt\0source.txt\0"
        assert ActivityHandler._parse_status_entries(output) == ["copied.txt"]

    def test_modified_and_rename_and_untracked_together(self) -> None:
        output = "RM new name.txt\0old name.txt\0M  changed.txt\0?? untracked.txt\0"
        assert ActivityHandler._parse_status_entries(output) == [
            "new name.txt",
            "changed.txt",
            "untracked.txt",
        ]

    def test_empty_output_yields_no_entries(self) -> None:
        assert ActivityHandler._parse_status_entries("") == []


# ---------------------------------------------------------------------------
# Reflog evidence
# ---------------------------------------------------------------------------


class TestScanReflog:
    """ActivityHandler._scan_reflog / report()'s reflog-sourced issue attribution."""

    def test_entry_with_ref_is_attributed(self) -> None:
        reflog = _reflog_line("abc1234", "2026-08-13T09:00:00+06:00", "commit: Fix thing. Ref #10")
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog)):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(10, "reflog", 1)]
        assert report.unattributed_events == 0

    def test_entry_without_ref_is_counted_as_unattributed_not_dropped(self) -> None:
        reflog = _reflog_line("abc1234", "2026-08-13T09:00:00+06:00", "commit: Fix thing")
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog, status="")):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_events == 1

    def test_commit_and_amend_subjects_both_count(self) -> None:
        reflog = "\n".join(
            [
                _reflog_line("aaa1111", "2026-08-13T09:00:00+06:00", "commit: work. Ref #10"),
                _reflog_line(
                    "bbb2222", "2026-08-13T10:00:00+06:00", "commit (amend): more work. Ref #10"
                ),
            ]
        )
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog)):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(10, "reflog", 2)]

    def test_initial_commit_subject_is_not_evidence(self) -> None:
        """'commit (initial):' is a real reflog subject for a repo's first commit,
        but the spec enumerates only 'commit:' and 'commit (amend):' as evidence."""
        reflog = _reflog_line(
            "aaa1111", "2026-08-13T09:00:00+06:00", "commit (initial): init. Ref #10"
        )
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog, status="")):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_events == 0

    def test_non_commit_subjects_are_ignored_entirely(self) -> None:
        """checkout/merge/etc. reflog entries are neither attributed nor counted
        as unattributed — they carry no commit evidence at all."""
        reflog = "\n".join(
            [
                _reflog_line(
                    "aaa1111", "2026-08-13T09:00:00+06:00", "checkout: moving from a to b"
                ),
                _reflog_line("bbb2222", "2026-08-13T09:05:00+06:00", "pull: Fast-forward"),
            ]
        )
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog, status="")):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_events == 0

    def test_entry_at_end_of_local_day_boundary_is_included(self) -> None:
        reflog = _reflog_line("aaa1111", "2026-08-13T23:59:59+06:00", "commit: late work. Ref #10")
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog)):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(10, "reflog", 1)]

    def test_entry_just_past_local_midnight_falls_to_the_next_day(self) -> None:
        reflog = _reflog_line(
            "aaa1111", "2026-08-14T00:00:00+06:00", "commit: past midnight. Ref #10"
        )
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog, status="")):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_events == 0

    def test_entry_recorded_in_a_different_offset_is_converted_to_local_day(self) -> None:
        """23:30 UTC on 2026-08-13 is 05:30 local (+06:00) on 2026-08-14 — a naive
        string comparison against the recorded date would misclassify this."""
        reflog = _reflog_line("aaa1111", "2026-08-13T23:30:00+00:00", "commit: work. Ref #10")
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))

        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog, status="")):
            report_13 = handler.report("2026-08-13")
        assert report_13.issues == []

        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog)):
            report_14 = handler.report("2026-08-14")
        assert report_14.issues == [IssueActivity(10, "reflog", 1)]

    def test_empty_reflog_yields_no_issues_and_falls_through(self) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog="", status="")):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_events == 0

    def test_blank_reflog_line_is_skipped(self) -> None:
        reflog = "\n\n" + _reflog_line(
            "aaa1111", "2026-08-13T09:00:00+06:00", "commit: work. Ref #10"
        )
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog)):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(10, "reflog", 1)]

    def test_reflog_entry_with_unparsable_date_is_skipped_not_fatal(self) -> None:
        reflog = _reflog_line("aaa1111", "not-a-timestamp", "commit: work. Ref #10")
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog, status="")):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_events == 0

    def test_unparsable_reflog_line_is_skipped_not_fatal(self) -> None:
        reflog = "\n".join(
            [
                "this is not a reflog line",
                _reflog_line("aaa1111", "2026-08-13T09:00:00+06:00", "commit: work. Ref #10"),
            ]
        )
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog)):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(10, "reflog", 1)]


# ---------------------------------------------------------------------------
# Working-tree evidence (only reached when the reflog attributes nothing)
# ---------------------------------------------------------------------------


class TestWorkingTreeFallback:
    """report()'s working-tree evidence path, and the branch-consultation gate."""

    def test_file_modified_on_date_vs_weeks_ago(self, tmp_path: Path) -> None:
        recent = tmp_path / "recent.txt"
        old = tmp_path / "old.txt"
        recent.write_text("x")
        old.write_text("y")
        _set_mtime(recent, _TARGET_DAY)
        _set_mtime(old, _TARGET_DAY - timedelta(days=21))

        status = "?? recent.txt\0?? old.txt\0"
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=status, branch="feature/4-vision"),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(4, "branch", 1)]

    def test_path_with_space_is_matched(self, tmp_path: Path) -> None:
        target = tmp_path / "un tracked.txt"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        status = "?? un tracked.txt\0"
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=status, branch="feature/4-vision"),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(4, "branch", 1)]

    def test_rename_checks_the_new_paths_mtime_only(self, tmp_path: Path) -> None:
        new_path = tmp_path / "new name.txt"
        new_path.write_text("x")
        _set_mtime(new_path, _TARGET_DAY)
        # old name.txt deliberately does not exist on disk (it was renamed away).

        status = "RM new name.txt\0old name.txt\0"
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=status, branch="feature/4-vision"),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(4, "branch", 1)]

    def test_untracked_file_counts_as_evidence(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        status = "?? file.txt\0"
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=status, branch="fix/9-bug"),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(9, "branch", 1)]

    def test_deleted_path_with_no_mtime_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        status = "?? gone.txt\0"  # never created on disk
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=status),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_files == 0

    def test_branch_without_issue_number_is_unattributed(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        status = "?? file.txt\0"
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=status, branch="main"),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_files == 1
        assert report.unattributed_branch == "main"

    def test_branch_with_no_slash_yields_no_issue(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status="?? file.txt\0", branch="develop"),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_branch == "develop"

    def test_stale_branch_with_no_modified_files_contributes_nothing(self, tmp_path: Path) -> None:
        """A branch left checked out from unrelated old work must not be attributed
        when nothing on disk was actually touched on the date — and the branch
        must never even be queried in that case (see calls assertion below)."""
        calls: List[List[str]] = []
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status="", calls=calls),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == []
        assert report.unattributed_files == 0
        assert report.unattributed_branch is None
        assert all(cmd[1] != "branch" for cmd in calls)

    def test_reflog_issue_found_means_branch_is_never_consulted(self, tmp_path: Path) -> None:
        calls: List[List[str]] = []
        reflog = _reflog_line("aaa1111", "2026-08-13T09:00:00+06:00", "commit: work. Ref #10")
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog=reflog, calls=calls),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == [IssueActivity(10, "reflog", 1)]
        assert all(cmd[1] not in ("status", "branch") for cmd in calls)


# ---------------------------------------------------------------------------
# Planning-tree evidence (issues/<N>-<slug>/ and reviews/MR<N>-review.yaml) —
# no subprocess involved, so these hit the real filesystem under tmp_path.
# ---------------------------------------------------------------------------


class TestScanPlanningTree:
    """ActivityHandler._scan_planning_tree — sources 3 (planning) and 4 (review)."""

    def test_padded_issue_folder_strips_leading_zeros(self, tmp_path: Path) -> None:
        issue_dir = tmp_path / "planning" / "epic" / "milestone-01" / "issues" / "054-yocto-setup"
        issue_dir.mkdir(parents=True)
        target = issue_dir / "analysis.md"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        issue_counts, mr_counts = handler._scan_planning_tree(_TARGET_DAY)  # noqa: SLF001

        assert issue_counts == {54: 1}
        assert mr_counts == {}

    def test_file_nested_several_levels_below_issue_folder_is_counted(self, tmp_path: Path) -> None:
        nested_dir = tmp_path / "planning" / "epic" / "issues" / "10-foo" / "sub" / "deeper"
        nested_dir.mkdir(parents=True)
        target = nested_dir / "notes.md"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        issue_counts, _mr_counts = handler._scan_planning_tree(_TARGET_DAY)  # noqa: SLF001

        assert issue_counts == {10: 1}

    def test_file_under_planning_outside_any_issue_folder_yields_nothing(
        self, tmp_path: Path
    ) -> None:
        misc_dir = tmp_path / "planning" / "epic"
        misc_dir.mkdir(parents=True)
        target = misc_dir / "overview.md"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        issue_counts, mr_counts = handler._scan_planning_tree(_TARGET_DAY)  # noqa: SLF001

        assert issue_counts == {}
        assert mr_counts == {}

    def test_reviews_orphan_directory_does_not_match_the_mr_pattern(self, tmp_path: Path) -> None:
        """'reviews-orphan/' (hand-managed reviews for unlinked work, per the
        planning-structure convention) must not be mistaken for 'reviews/' —
        the MR pattern requires the exact directory name, not a prefix of it."""
        orphan_dir = tmp_path / "planning" / "reviews-orphan" / "51-ab-fallback-fix"
        orphan_dir.mkdir(parents=True)
        target = orphan_dir / "observed-failures.md"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        issue_counts, mr_counts = handler._scan_planning_tree(_TARGET_DAY)  # noqa: SLF001

        assert issue_counts == {}
        assert mr_counts == {}

    def test_mr_review_yaml_is_detected(self, tmp_path: Path) -> None:
        reviews_dir = tmp_path / "planning" / "epic" / "reviews"
        reviews_dir.mkdir(parents=True)
        target = reviews_dir / "MR121-review.yaml"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY)

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        issue_counts, mr_counts = handler._scan_planning_tree(_TARGET_DAY)  # noqa: SLF001

        assert issue_counts == {}
        assert mr_counts == {121: 1}

    def test_multiple_files_in_the_same_issue_folder_sum_into_one_count(
        self, tmp_path: Path
    ) -> None:
        issue_dir = tmp_path / "planning" / "epic" / "issues" / "7-foo"
        issue_dir.mkdir(parents=True)
        for name in ("design.md", "code-review.md"):
            target = issue_dir / name
            target.write_text("x")
            _set_mtime(target, _TARGET_DAY)

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        issue_counts, _mr_counts = handler._scan_planning_tree(_TARGET_DAY)  # noqa: SLF001

        assert issue_counts == {7: 2}

    def test_file_mtimed_on_a_different_date_is_excluded(self, tmp_path: Path) -> None:
        issue_dir = tmp_path / "planning" / "epic" / "issues" / "7-foo"
        issue_dir.mkdir(parents=True)
        target = issue_dir / "design.md"
        target.write_text("x")
        _set_mtime(target, _TARGET_DAY - timedelta(days=5))

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        issue_counts, mr_counts = handler._scan_planning_tree(_TARGET_DAY)  # noqa: SLF001

        assert issue_counts == {}
        assert mr_counts == {}

    def test_repository_with_no_planning_directory_yields_nothing(self, tmp_path: Path) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        issue_counts, mr_counts = handler._scan_planning_tree(_TARGET_DAY)  # noqa: SLF001

        assert issue_counts == {}
        assert mr_counts == {}


# ---------------------------------------------------------------------------
# Whole-report assembly
# ---------------------------------------------------------------------------


class TestReportAssembly:
    def test_clean_repo_reports_zero_activity_at_exit_zero_shape(self, tmp_path: Path) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=""),
        ):
            report = handler.report("2026-08-13")

        assert report == ActivityReport(_TARGET_DAY, "repo", [], 0, 0, None)

    def test_repo_name_is_the_toplevel_directory_name(self, tmp_path: Path) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(toplevel="/home/user/projects/alpha-svc\n", reflog="", status=""),
        ):
            report = handler.report("2026-08-13")

        assert report.repo == "alpha-svc"

    def test_default_date_is_todays_local_date(self, tmp_path: Path) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        handler._now = lambda: datetime(2026, 8, 13, 15, 0, 0, tzinfo=_FIXED_TZ)  # noqa: SLF001

        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=""),
        ):
            report = handler.report(None)

        assert report.activity_date == _TARGET_DAY

    def test_multiple_reflog_issues_are_sorted_by_id(self) -> None:
        reflog = "\n".join(
            [
                _reflog_line("a", "2026-08-13T09:00:00+06:00", "commit: x. Ref #20"),
                _reflog_line("b", "2026-08-13T10:00:00+06:00", "commit: y. Ref #5"),
            ]
        )
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/repo"))
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog)):
            report = handler.report("2026-08-13")

        assert report.issues == [
            IssueActivity(5, "reflog", 1),
            IssueActivity(20, "reflog", 1),
        ]

    def test_issue_corroborated_by_reflog_and_planning_reports_both(self, tmp_path: Path) -> None:
        """An issue attributed by two independent sources on the same date must
        surface both entries — not just the first one found, unlike the
        reflog/branch pair where branch is deliberately suppressed once the
        reflog already fired."""
        issue_dir = tmp_path / "planning" / "epic" / "issues" / "10-foo"
        issue_dir.mkdir(parents=True)
        design = issue_dir / "design.md"
        design.write_text("x")
        _set_mtime(design, _TARGET_DAY)

        reflog = _reflog_line("aaa1111", "2026-08-13T09:00:00+06:00", "commit: work. Ref #10")
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch("projctl.handlers.activity.subprocess.run", _git_stub(reflog=reflog)):
            report = handler.report("2026-08-13")

        assert report.issues == [
            IssueActivity(10, "reflog", 1),
            IssueActivity(10, "planning", 1),
        ]

    def test_planning_and_review_sources_fire_with_no_reflog_or_branch_evidence(
        self, tmp_path: Path
    ) -> None:
        """Sources 3 and 4 must run even on a day with no commit and no branch
        match — the whole point of adding them is to catch design/review work
        that source 1 and 2 are structurally blind to."""
        issue_dir = tmp_path / "planning" / "epic" / "issues" / "3-foo"
        issue_dir.mkdir(parents=True)
        design = issue_dir / "design.md"
        design.write_text("x")
        _set_mtime(design, _TARGET_DAY)

        reviews_dir = tmp_path / "planning" / "epic" / "reviews"
        reviews_dir.mkdir(parents=True)
        review = reviews_dir / "MR55-review.yaml"
        review.write_text("x")
        _set_mtime(review, _TARGET_DAY)

        handler = ActivityHandler(tz=_FIXED_TZ, cwd=tmp_path)
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(reflog="", status=""),
        ):
            report = handler.report("2026-08-13")

        assert report.issues == [
            IssueActivity(3, "planning", 1),
            IssueActivity(55, "review", 1),
        ]


# ---------------------------------------------------------------------------
# Errors: not a git repo, git missing, bad date
# ---------------------------------------------------------------------------


class TestGitTransportErrors:
    def test_not_a_git_repository_raises_platform_error(self) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/tmp"))
        with patch(
            "projctl.handlers.activity.subprocess.run",
            side_effect=lambda cmd, **kw: _completed(cmd, 128, "", "fatal: not a git repository"),
        ):
            with pytest.raises(PlatformError, match="not a git repository"):
                handler.report("2026-08-13")

    def test_empty_toplevel_path_raises_platform_error(self) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/tmp"))
        with patch(
            "projctl.handlers.activity.subprocess.run",
            _git_stub(toplevel=""),
        ):
            with pytest.raises(PlatformError, match="returned no path"):
                handler.report("2026-08-13")

    def test_git_not_installed_raises_platform_error(self) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/tmp"))
        with patch(
            "projctl.handlers.activity.subprocess.run",
            side_effect=FileNotFoundError("git"),
        ):
            with pytest.raises(PlatformError, match="git executable not found"):
                handler.report("2026-08-13")


class TestNow:
    def test_production_now_with_no_injected_tz_is_aware(self) -> None:
        """With tz=None (the production path), _now() must still return an aware
        datetime — see the docstring on the naive/aware hazard this avoids."""
        handler = ActivityHandler()
        assert handler._now().tzinfo is not None  # noqa: SLF001


class TestDateParsing:
    def test_invalid_date_raises_value_error(self) -> None:
        handler = ActivityHandler(tz=_FIXED_TZ, cwd=Path("/tmp"))
        with pytest.raises(ValueError, match="not-a-date"):
            handler.report("not-a-date")


# ---------------------------------------------------------------------------
# JSON / table rendering shape
# ---------------------------------------------------------------------------


class TestActivityReportToDict:
    def test_json_shape_matches_documented_contract(self) -> None:
        report = ActivityReport(
            _TARGET_DAY,
            "alpha-svc",
            [IssueActivity(4, "branch", 3)],
            unattributed_events=0,
            unattributed_files=0,
            unattributed_branch=None,
        )

        payload = report.to_dict()

        assert payload == {
            "date": "2026-08-13",
            "repo": "alpha-svc",
            "issues": [{"id": 4, "via": "branch", "count": 3}],
            "unattributed": {"events": 0, "files": 0, "branch": None},
        }

    def test_count_key_is_uniform_across_every_via(self) -> None:
        """The count key name used to depend on `via` ('events' for reflog,
        'files' for branch); now that there are four sources, it must be the
        single uniform key 'count' for all of them."""
        for via in ("reflog", "branch", "planning", "review"):
            report = ActivityReport(_TARGET_DAY, "repo", [IssueActivity(1, via, 3)], 0, 0, None)
            payload = report.to_dict()
            assert payload["issues"] == [{"id": 1, "via": via, "count": 3}]

    def test_json_is_serializable(self) -> None:
        report = ActivityReport(
            _TARGET_DAY,
            "repo",
            [IssueActivity(4, "branch", 1)],
            unattributed_events=2,
            unattributed_files=0,
            unattributed_branch="main",
        )

        json.dumps(report.to_dict())  # must not raise


class TestActivityReportPrintTable:
    def test_prints_issues_and_unattributed_sections(self, capsys: pytest.CaptureFixture) -> None:
        report = ActivityReport(
            _TARGET_DAY,
            "repo",
            [IssueActivity(4, "branch", 3)],
            unattributed_events=1,
            unattributed_files=2,
            unattributed_branch="main",
        )

        report.print_table()

        out = capsys.readouterr().out
        assert "#4" in out
        assert "3 file" in out
        assert "1 reflog event" in out
        assert "2 modified file" in out
        assert "'main'" in out

    def test_review_source_is_prefixed_with_bang_not_hash(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """via="review" holds a merge-request number, not an issue number — the
        table must not print it as '#55', which would read as an issue."""
        report = ActivityReport(_TARGET_DAY, "repo", [IssueActivity(55, "review", 1)], 0, 0, None)

        report.print_table()

        out = capsys.readouterr().out
        assert "!55" in out
        assert "#55" not in out

    def test_prints_no_attributed_issues_notice_when_empty(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        report = ActivityReport(_TARGET_DAY, "repo", [], 0, 0, None)

        report.print_table()

        assert "No attributed issues." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _args(**overrides) -> SimpleNamespace:
    defaults = {"date": None, "json": False}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestArgparseWiring:
    """Exercises the real parser built by _add_activity_subparser(), not a hand-rolled args object."""

    def test_real_parser_wires_bare_command_to_handler_report(self) -> None:
        with patch("projctl.cli.ActivityHandler") as mock_handler_cls:
            mock_handler_cls.return_value.report.return_value = ActivityReport(
                _TARGET_DAY, "repo", [], 0, 0, None
            )

            rc = main(["activity"])

        assert rc == 0
        mock_handler_cls.return_value.report.assert_called_once_with(None)

    def test_real_parser_wires_date_positional(self) -> None:
        with patch("projctl.cli.ActivityHandler") as mock_handler_cls:
            mock_handler_cls.return_value.report.return_value = ActivityReport(
                _TARGET_DAY, "repo", [], 0, 0, None
            )

            rc = main(["activity", "2026-08-13"])

        assert rc == 0
        mock_handler_cls.return_value.report.assert_called_once_with("2026-08-13")

    def test_real_parser_wires_json_flag(self, capsys: pytest.CaptureFixture) -> None:
        with patch("projctl.cli.ActivityHandler") as mock_handler_cls:
            mock_handler_cls.return_value.report.return_value = ActivityReport(
                _TARGET_DAY, "beta-svc", [IssueActivity(464, "reflog", 3)], 0, 0, None
            )

            rc = main(["activity", "2026-08-13", "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "date": "2026-08-13",
            "repo": "beta-svc",
            "issues": [{"id": 464, "via": "reflog", "count": 3}],
            "unattributed": {"events": 0, "files": 0, "branch": None},
        }


class TestCmdActivityOutputSelection:
    def test_default_output_is_the_human_table_not_json(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        with patch("projctl.cli.ActivityHandler") as mock_handler_cls:
            mock_handler_cls.return_value.report.return_value = ActivityReport(
                _TARGET_DAY, "repo", [IssueActivity(4, "branch", 1)], 0, 0, None
            )

            rc = cmd_activity(_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert "#4" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_json_flag_emits_valid_json_and_no_table_text(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        with patch("projctl.cli.ActivityHandler") as mock_handler_cls:
            mock_handler_cls.return_value.report.return_value = ActivityReport(
                _TARGET_DAY, "repo", [IssueActivity(4, "branch", 1)], 0, 0, None
            )

            rc = cmd_activity(_args(json=True))

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["issues"] == [{"id": 4, "via": "branch", "count": 1}]


class TestErrorHandling:
    """Handler failures become a non-zero exit code, not a traceback."""

    def test_platform_error_returns_one(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("projctl.cli.ActivityHandler") as mock_handler_cls:
            mock_handler_cls.return_value.report.side_effect = PlatformError("not a git repository")

            with caplog.at_level("ERROR"):
                rc = cmd_activity(_args())

        assert rc == 1
        assert "not a git repository" in caplog.text

    def test_value_error_returns_one(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("projctl.cli.ActivityHandler") as mock_handler_cls:
            mock_handler_cls.return_value.report.side_effect = ValueError("Invalid date")

            with caplog.at_level("ERROR"):
                rc = cmd_activity(_args(date="not-a-date"))

        assert rc == 1
        assert "Invalid date" in caplog.text


class TestNoActivityIsSuccessNotError:
    def test_no_activity_found_still_exits_zero(self) -> None:
        with patch("projctl.cli.ActivityHandler") as mock_handler_cls:
            mock_handler_cls.return_value.report.return_value = ActivityReport(
                _TARGET_DAY, "repo", [], 0, 0, None
            )

            rc = cmd_activity(_args())

        assert rc == 0
