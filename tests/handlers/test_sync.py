"""Tests for projctl.handlers.sync module — sync status command."""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import io
import os
import subprocess
import textwrap
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml

from projctl.cli import cmd_sync, main
from projctl.config import Config
from projctl.exceptions import PlatformError
from projctl.handlers.sync import (
    RSYNC_EXCLUDES,
    DriftClassification,
    ItemizeEntry,
    PlanningSyncHandler,
    _MEMORY_RSYNC_EXCLUDES,
    _SyncPaths,
    _assert_exclude_shape,
    _claude_memory_path,
    _claude_projects_root,
    _encode_repo_path,
    _is_timestamp_or_perms_only,
    _parse_itemize_line,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, gdrive_base: Path) -> Config:
    """Create a minimal projctl config that points gdrive_base at tmp_path."""
    config_data: Dict[str, Any] = {
        "platform": "gitlab",
        "gitlab": {
            "default_group": "test/group",
            "labels": {
                "default": ["type::feature"],
            },
        },
        "planning_sync": {
            "gdrive_base": str(gdrive_base),
        },
    }
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.dump(config_data, fh)
    return Config(config_path)


def _file_snapshot(directory: Path) -> dict[str, tuple[int, float, str]]:
    """Capture {relative_path: (size, mtime, sha256)} for all regular files."""
    snap: dict[str, tuple[int, float, str]] = {}
    if not directory.exists():
        return snap
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(directory))
            data = p.read_bytes()
            snap[rel] = (
                p.stat().st_size,
                p.stat().st_mtime,
                hashlib.sha256(data).hexdigest(),
            )
    return snap


def _write_file(path: Path, content: str = "content") -> None:
    """Write a file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Integration fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_env(tmp_path: Path):
    """Create a fake git repo + planning dir + gdrive dir.

    Returns a (handler, planning_path, gdrive_repo_path) tuple so tests can
    manipulate both trees directly.
    """
    # Fake git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )

    planning = repo / "planning"
    planning.mkdir()

    gdrive_base = tmp_path / "gdrive"
    gdrive_base.mkdir()

    config = _make_config(tmp_path, gdrive_base)

    # Build handler while cwd is inside the fake repo so git detection works.
    orig_cwd = os.getcwd()
    os.chdir(str(repo))
    try:
        handler = PlanningSyncHandler(config)
    finally:
        os.chdir(orig_cwd)

    gdrive_repo = gdrive_base / "backup" / "planning" / "repo"
    return handler, planning, gdrive_repo


# ---------------------------------------------------------------------------
# Unit tests — _parse_itemize_line
# ---------------------------------------------------------------------------


class TestParseItemizeLine:
    """Unit tests for _parse_itemize_line — pure function, no subprocess."""

    def test_new_file(self) -> None:
        """New file transfer line is parsed correctly."""
        entry = _parse_itemize_line(">f+++++++++ new.md")
        assert entry == ItemizeEntry(op=">", kind="f", attrs="+++++++++", path="new.md")

    def test_modified_size_and_time(self) -> None:
        """File with size and mtime change is parsed correctly."""
        entry = _parse_itemize_line(">f.st...... mod.md")
        assert entry == ItemizeEntry(op=">", kind="f", attrs=".st......", path="mod.md")

    def test_mtime_only(self) -> None:
        """File with mtime-only change is parsed correctly."""
        entry = _parse_itemize_line(">f..t...... mtime.md")
        assert entry == ItemizeEntry(op=">", kind="f", attrs="..t......", path="mtime.md")

    def test_deleting_file(self) -> None:
        """Deletion line for a file is parsed correctly.

        rsync emits exactly three spaces between '*deleting' and the path.
        """
        entry = _parse_itemize_line("*deleting   old.md")
        assert entry == ItemizeEntry(op="*deleting", kind="", attrs="", path="old.md")

    def test_deleting_directory(self) -> None:
        """Deletion line for a directory is parsed correctly.

        rsync emits exactly three spaces between '*deleting' and the path.
        """
        entry = _parse_itemize_line("*deleting   old/")
        assert entry == ItemizeEntry(op="*deleting", kind="", attrs="", path="old/")

    def test_new_directory(self) -> None:
        """New directory entry is parsed correctly."""
        entry = _parse_itemize_line("cd+++++++++ newdir/")
        assert entry == ItemizeEntry(op="c", kind="d", attrs="+++++++++", path="newdir/")

    def test_unchanged_file(self) -> None:
        """Unchanged file entry is parsed correctly."""
        # rsync -i format: YX<9-char-attrs> <path>, so total 11 chars before the space.
        entry = _parse_itemize_line(".f......... stable.md")
        assert entry == ItemizeEntry(op=".", kind="f", attrs=".........", path="stable.md")

    def test_hard_link(self) -> None:
        """Hard link entry is parsed correctly."""
        entry = _parse_itemize_line("hf+++++++++ link.md")
        assert entry == ItemizeEntry(op="h", kind="f", attrs="+++++++++", path="link.md")

    def test_path_with_space(self) -> None:
        """Path with embedded space is captured fully."""
        entry = _parse_itemize_line(">f+++++++++ a b.md")
        assert entry is not None
        assert entry.path == "a b.md"

    def test_hidden_file(self) -> None:
        """Path starting with a dot is parsed correctly."""
        entry = _parse_itemize_line(">f+++++++++ .hidden")
        assert entry is not None
        assert entry.path == ".hidden"

    def test_banner_sending(self) -> None:
        """Banner 'sending incremental file list' returns None."""
        assert _parse_itemize_line("sending incremental file list") is None

    def test_banner_receiving(self) -> None:
        """Banner 'receiving incremental file list' returns None."""
        assert _parse_itemize_line("receiving incremental file list") is None

    def test_banner_total_size(self) -> None:
        """Banner 'total size is ...' returns None."""
        assert _parse_itemize_line("total size is 1234") is None

    def test_banner_dry_run(self) -> None:
        """Banner '(DRY RUN)' returns None."""
        assert _parse_itemize_line("(DRY RUN)") is None

    def test_banner_sent(self) -> None:
        """Banner 'sent ...' returns None."""
        assert _parse_itemize_line("sent 100 bytes  received 50 bytes  300.00 bytes/sec") is None

    def test_blank_line(self) -> None:
        """Blank line returns None."""
        assert _parse_itemize_line("") is None

    def test_blank_line_with_newline(self) -> None:
        """Line with only a newline returns None."""
        assert _parse_itemize_line("\n") is None

    def test_garbage_raises(self) -> None:
        """Unrecognized line raises PlatformError."""
        with pytest.raises(PlatformError, match="xyzzy blah"):
            _parse_itemize_line("xyzzy blah")


# ---------------------------------------------------------------------------
# Lightweight fixture for unit tests (H6)
# ---------------------------------------------------------------------------


@pytest.fixture()
def bare_handler(tmp_path: Path) -> PlanningSyncHandler:
    """Build a PlanningSyncHandler without running __init__ or any subprocess.

    Bypasses git detection, rsync availability checks, and all filesystem
    access so that unit tests for pure methods (_classify_drift,
    _format_status_report, _is_timestamp_or_perms_only) remain fully isolated
    from external processes.
    """
    planning = tmp_path / "planning"
    planning.mkdir()
    gdrive_base = tmp_path / "gdrive"
    gdrive_base.mkdir()
    gdrive_repo = gdrive_base / "backup" / "planning" / "repo"

    handler = PlanningSyncHandler.__new__(PlanningSyncHandler)
    handler.config = None  # type: ignore[assignment]
    handler.dry_run = False
    handler.repo_root = tmp_path / "repo"
    handler.repo_name = "repo"
    handler.paths = _SyncPaths(
        planning_path=planning,
        gdrive_base=gdrive_base,
        gdrive_planning_base=gdrive_base / "backup" / "planning",
        gdrive_repo_path=gdrive_repo,
        memory_path=None,
        gdrive_memory_path=tmp_path / "no-such-mem",  # non-existent sentinel
    )
    return handler


# ---------------------------------------------------------------------------
# Unit tests — _classify_drift
# ---------------------------------------------------------------------------


def _make_entries(lines: list[str]) -> list[ItemizeEntry]:
    """Parse a list of rsync output lines into ItemizeEntry objects."""
    result: list[ItemizeEntry] = []
    for line in lines:
        entry = _parse_itemize_line(line)
        if entry is not None:
            result.append(entry)
    return result


class TestClassifyDrift:
    """Unit tests for _classify_drift — pure, no subprocess.

    Uses bare_handler (no git, no subprocess) to confirm full isolation.
    """

    def test_empty_empty_is_in_sync(self, bare_handler: PlanningSyncHandler) -> None:
        """Two empty entry lists → in-sync."""
        dc = bare_handler._classify_drift([], [])
        assert dc.state == "in-sync"
        assert dc.push_transfers == []
        assert dc.push_deletes == []
        assert dc.pull_transfers == []
        assert dc.pull_deletes == []
        assert dc.mtime_only_count == 0

    def test_push_only_transfers_local_ahead(self, bare_handler: PlanningSyncHandler) -> None:
        """Push has transfers, pull is empty → local-ahead."""
        push = _make_entries([">f+++++++++ a.md"])
        pull = _make_entries(["*deleting   a.md"])
        dc = bare_handler._classify_drift(push, pull)
        assert dc.state == "local-ahead"
        assert dc.push_transfers == ["a.md"]
        assert dc.pull_deletes == ["a.md"]
        assert dc.pull_transfers == []
        assert dc.push_deletes == []

    def test_pull_only_transfers_remote_ahead(self, bare_handler: PlanningSyncHandler) -> None:
        """Pull has transfers, push has only deletes → remote-ahead."""
        push = _make_entries(["*deleting   b.md"])
        pull = _make_entries([">f+++++++++ b.md"])
        dc = bare_handler._classify_drift(push, pull)
        assert dc.state == "remote-ahead"
        assert dc.pull_transfers == ["b.md"]
        assert dc.push_deletes == ["b.md"]
        assert dc.push_transfers == []
        assert dc.pull_deletes == []

    def test_both_transfers_diverged(self, bare_handler: PlanningSyncHandler) -> None:
        """Both push and pull have transfers → diverged."""
        push = _make_entries([">f.st...... c.md"])
        pull = _make_entries([">f.st...... c.md"])
        dc = bare_handler._classify_drift(push, pull)
        assert dc.state == "diverged"
        assert "c.md" in dc.push_transfers
        assert "c.md" in dc.pull_transfers
        # Size-changed entries must not be counted as mtime-only.
        assert dc.mtime_only_count == 0

    def test_paths_sorted(self, bare_handler: PlanningSyncHandler) -> None:
        """Returned path lists are sorted lexicographically."""
        push = _make_entries(
            [
                ">f+++++++++ z.md",
                ">f+++++++++ a.md",
                ">f+++++++++ m.md",
            ]
        )
        dc = bare_handler._classify_drift(push, [])
        assert dc.push_transfers == ["a.md", "m.md", "z.md"]

    def test_directory_entries_not_counted(self, bare_handler: PlanningSyncHandler) -> None:
        """Directory entries do not contribute to transfer counts."""
        push = _make_entries(["cd+++++++++ newdir/"])
        dc = bare_handler._classify_drift(push, [])
        assert dc.state == "in-sync"
        assert dc.push_transfers == []

    def test_mtime_only_counted(self, bare_handler: PlanningSyncHandler) -> None:
        """mtime-only transfer entries increment mtime_only_count."""
        # attrs '..t......': checksum unchanged (pos0='.'), size unchanged (pos1='.'),
        # mtime changed (pos2='t'). Full rsync output: YX + 9-char-attrs + space + path.
        push = _make_entries([">f..t...... mtime.md"])
        dc = bare_handler._classify_drift(push, [])
        assert dc.mtime_only_count == 1

    def test_new_file_not_mtime_only(self, bare_handler: PlanningSyncHandler) -> None:
        """New file transfer ('+++++++++) is not counted as mtime-only."""
        push = _make_entries([">f+++++++++ new.md"])
        dc = bare_handler._classify_drift(push, [])
        assert dc.mtime_only_count == 0

    def test_size_changed_not_mtime_only(self, bare_handler: PlanningSyncHandler) -> None:
        """File with size change is not counted as mtime-only."""
        push = _make_entries([">f.st...... mod.md"])
        dc = bare_handler._classify_drift(push, [])
        assert dc.mtime_only_count == 0

    def test_mtime_only_deduplicated_across_directions(
        self, bare_handler: PlanningSyncHandler
    ) -> None:
        """Same path appearing as mtime-only in both push and pull counts as 1, not 2 (M2)."""
        # Same file with mtime-only diff appears in both directions (clock-skew scenario).
        push = _make_entries([">f..t...... shared.md"])
        pull = _make_entries([">f..t...... shared.md"])
        dc = bare_handler._classify_drift(push, pull)
        assert dc.mtime_only_count == 1


# ---------------------------------------------------------------------------
# Unit tests — _format_status_report
# ---------------------------------------------------------------------------


def _dc(
    state: str,
    push_transfers: list[str] | None = None,
    push_deletes: list[str] | None = None,
    pull_transfers: list[str] | None = None,
    pull_deletes: list[str] | None = None,
    mtime_only_count: int = 0,
) -> DriftClassification:
    """Convenience constructor for DriftClassification in tests."""
    return DriftClassification(
        state=state,
        push_transfers=push_transfers or [],
        push_deletes=push_deletes or [],
        pull_transfers=pull_transfers or [],
        pull_deletes=pull_deletes or [],
        mtime_only_count=mtime_only_count,
    )


class TestFormatStatusReport:
    """Unit tests for _format_status_report — pure, no subprocess.

    Uses bare_handler (no git, no subprocess) to confirm full isolation.
    """

    def test_in_sync_first_line(self, bare_handler: PlanningSyncHandler) -> None:
        """in-sync report starts with 'STATUS: in-sync'."""
        report = bare_handler._format_status_report(_dc("in-sync"))
        assert report.splitlines()[0] == "STATUS: in-sync"

    def test_local_ahead_first_line(self, bare_handler: PlanningSyncHandler) -> None:
        """local-ahead report starts with 'STATUS: local-ahead'."""
        report = bare_handler._format_status_report(_dc("local-ahead", push_transfers=["a.md"]))
        assert report.splitlines()[0] == "STATUS: local-ahead"

    def test_remote_ahead_first_line(self, bare_handler: PlanningSyncHandler) -> None:
        """remote-ahead report starts with 'STATUS: remote-ahead'."""
        report = bare_handler._format_status_report(_dc("remote-ahead", pull_transfers=["b.md"]))
        assert report.splitlines()[0] == "STATUS: remote-ahead"

    def test_diverged_first_line(self, bare_handler: PlanningSyncHandler) -> None:
        """diverged report starts with 'STATUS: diverged'."""
        report = bare_handler._format_status_report(
            _dc("diverged", push_transfers=["c.md"], pull_transfers=["c.md"])
        )
        assert report.splitlines()[0] == "STATUS: diverged"

    def test_subsections_omitted_when_empty(self, bare_handler: PlanningSyncHandler) -> None:
        """Subsection headers are not printed when their lists are empty."""
        report = bare_handler._format_status_report(_dc("in-sync"))
        assert "Local changes to push" not in report
        assert "Remote files a push would DELETE" not in report
        assert "Remote changes to pull" not in report
        assert "Local files a pull would DELETE" not in report

    def test_push_section_present(self, bare_handler: PlanningSyncHandler) -> None:
        """Push-transfer section appears when push_transfers is non-empty."""
        report = bare_handler._format_status_report(_dc("local-ahead", push_transfers=["x.md"]))
        assert "Local changes to push" in report
        assert "x.md" in report

    def test_pull_section_present(self, bare_handler: PlanningSyncHandler) -> None:
        """Pull-transfer section appears when pull_transfers is non-empty."""
        report = bare_handler._format_status_report(_dc("remote-ahead", pull_transfers=["y.md"]))
        assert "Remote changes to pull" in report
        assert "y.md" in report

    def test_push_delete_section_present(self, bare_handler: PlanningSyncHandler) -> None:
        """Push-delete section appears when push_deletes is non-empty."""
        report = bare_handler._format_status_report(
            _dc("local-ahead", push_transfers=["a.md"], push_deletes=["old.md"])
        )
        assert "Remote files a push would DELETE" in report
        assert "old.md" in report

    def test_pull_delete_section_present(self, bare_handler: PlanningSyncHandler) -> None:
        """Pull-delete section appears when pull_deletes is non-empty."""
        report = bare_handler._format_status_report(
            _dc("remote-ahead", pull_transfers=["b.md"], pull_deletes=["local_old.md"])
        )
        assert "Local files a pull would DELETE" in report
        assert "local_old.md" in report

    def test_annotation_on_overlapping_path(self, bare_handler: PlanningSyncHandler) -> None:
        """Paths in both push and pull lists get the 'also differs' annotation."""
        report = bare_handler._format_status_report(
            _dc("diverged", push_transfers=["c.md"], pull_transfers=["c.md"])
        )
        assert "[also differs on the other side]" in report

    def test_no_annotation_when_not_overlapping(self, bare_handler: PlanningSyncHandler) -> None:
        """Paths that appear in only one direction do not get annotated."""
        report = bare_handler._format_status_report(
            _dc("diverged", push_transfers=["a.md"], pull_transfers=["b.md"])
        )
        assert "[also differs on the other side]" not in report

    def test_mtime_note_appears_when_count_positive(
        self, bare_handler: PlanningSyncHandler
    ) -> None:
        """Mtime-only note appears when mtime_only_count > 0."""
        report = bare_handler._format_status_report(
            _dc("local-ahead", push_transfers=["m.md"], mtime_only_count=1)
        )
        assert "differ only in timestamp or permissions" in report

    def test_mtime_note_absent_when_zero(self, bare_handler: PlanningSyncHandler) -> None:
        """Mtime-only note is omitted when mtime_only_count == 0."""
        report = bare_handler._format_status_report(_dc("local-ahead", push_transfers=["m.md"]))
        assert "differ only in timestamp or permissions" not in report

    def test_safe_next_step_in_sync(self, bare_handler: PlanningSyncHandler) -> None:
        """in-sync safe-next-step says no sync needed."""
        report = bare_handler._format_status_report(_dc("in-sync"))
        assert "No sync needed." in report

    def test_safe_next_step_local_ahead(self, bare_handler: PlanningSyncHandler) -> None:
        """local-ahead safe-next-step recommends push and warns about DELETE."""
        report = bare_handler._format_status_report(_dc("local-ahead", push_transfers=["a.md"]))
        assert "projctl sync push" in report
        assert "DELETE" in report

    def test_safe_next_step_remote_ahead(self, bare_handler: PlanningSyncHandler) -> None:
        """remote-ahead safe-next-step recommends pull and warns about DELETE."""
        report = bare_handler._format_status_report(_dc("remote-ahead", pull_transfers=["b.md"]))
        assert "projctl sync pull" in report
        assert "DELETE" in report

    def test_safe_next_step_diverged(self, bare_handler: PlanningSyncHandler) -> None:
        """diverged safe-next-step warns about manual reconciliation."""
        report = bare_handler._format_status_report(
            _dc("diverged", push_transfers=["a.md"], pull_transfers=["b.md"])
        )
        assert "Manual reconciliation required" in report

    def test_summary_line_counts(self, bare_handler: PlanningSyncHandler) -> None:
        """Summary line reflects correct transfer counts."""
        report = bare_handler._format_status_report(
            _dc("diverged", push_transfers=["a.md", "b.md"], pull_transfers=["c.md"])
        )
        assert "2 file(s) would be pushed" in report
        assert "1 file(s) would be pulled" in report

    def test_delete_summary_present_when_non_empty(self, bare_handler: PlanningSyncHandler) -> None:
        """Delete summary line appears when there are deletions."""
        report = bare_handler._format_status_report(
            _dc("local-ahead", push_transfers=["a.md"], push_deletes=["old.md"])
        )
        assert "remote file(s) would be deleted" in report

    def test_delete_summary_absent_when_empty(self, bare_handler: PlanningSyncHandler) -> None:
        """Delete summary line is omitted when there are no deletions."""
        report = bare_handler._format_status_report(_dc("local-ahead", push_transfers=["a.md"]))
        assert "remote file(s) would be deleted" not in report


# ---------------------------------------------------------------------------
# Unit tests — RSYNC_EXCLUDES assertion shape
# ---------------------------------------------------------------------------


class TestRsyncExcludes:
    """Tests for the RSYNC_EXCLUDES module-level constant and shape assertion."""

    def test_current_patterns_pass_assertion(self) -> None:
        """Current RSYNC_EXCLUDES patterns satisfy the basename-style constraint."""
        # This calls the same helper used at module import time.
        _assert_exclude_shape(RSYNC_EXCLUDES)

    def test_slash_pattern_raises(self) -> None:
        """A pattern with '/' triggers AssertionError."""
        with pytest.raises(AssertionError, match="basename-style"):
            _assert_exclude_shape(("*.swp", "subdir/*.tmp"))

    def test_double_star_pattern_raises(self) -> None:
        """A pattern with '**' triggers AssertionError."""
        with pytest.raises(AssertionError, match="basename-style"):
            _assert_exclude_shape(("*.swp", "**/*.tmp"))

    def test_tuple_is_non_empty(self) -> None:
        """RSYNC_EXCLUDES is non-empty (guards against accidental emptying)."""
        assert len(RSYNC_EXCLUDES) > 0


# ---------------------------------------------------------------------------
# Unit tests — status() AST-based read-only invariant
# ---------------------------------------------------------------------------


class TestStatusReadOnlyInvariant:
    """Verify by AST inspection that the status() transitive closure has no forbidden calls.

    The check covers status() and every helper reachable from it.  The only
    legitimate subprocess.run() calls live inside _rsync_itemize and
    _verify_rsync_available; those two methods are whitelisted by name.
    """

    # Methods reachable from status() that legitimately call subprocess.run.
    ALLOWED_SUBPROCESS_SITES = {"_rsync_itemize", "_verify_rsync_available"}

    # Attribute/function names that indicate a mutating or subprocess call.
    # Any of these appearing OUTSIDE the whitelisted sites fails the test.
    FORBIDDEN_NAMES = {
        "subprocess.run",
        "os.system",
        "mkdir",
        "makedirs",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "rmtree",
        "remove",
        "touch",
        "write_bytes",
        "write_text",
        "mkdtemp",
    }

    # All PlanningSyncHandler methods that are part of the status() call graph.
    STATUS_REACHABLE_METHODS = (
        "status",
        "_verify_rsync_available",
        "_rsync_itemize",
        "_parse_itemize_line",
        "_classify_drift",
        "_format_status_report",
        "_is_timestamp_or_perms_only",
        "_print_memory_status",
    )

    def _collect_call_names(self, tree: ast.AST) -> set[str]:
        """Walk an AST tree and collect string representations of Call nodes."""
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
                if isinstance(func.value, ast.Name):
                    names.add(f"{func.value.id}.{func.attr}")
            elif isinstance(func, ast.Name):
                names.add(func.id)
        return names

    def _get_method_source(self, method_name: str) -> str:
        """Return dedented source for a method on PlanningSyncHandler or module level."""
        import projctl.handlers.sync as sync_module

        obj = getattr(PlanningSyncHandler, method_name, None) or getattr(
            sync_module, method_name, None
        )
        assert obj is not None, f"Cannot find method/function: {method_name}"
        return textwrap.dedent(inspect.getsource(obj))

    def test_status_transitive_closure_has_no_forbidden_calls(self) -> None:
        """status() and all helpers it calls must not contain forbidden mutating calls.

        subprocess.run is allowed only inside _rsync_itemize and
        _verify_rsync_available (whitelisted by ALLOWED_SUBPROCESS_SITES).
        All other methods must not call it.
        """
        for method_name in self.STATUS_REACHABLE_METHODS:
            source = self._get_method_source(method_name)
            tree = ast.parse(source)
            found_names = self._collect_call_names(tree)
            if method_name in self.ALLOWED_SUBPROCESS_SITES:
                # Only disallow mutations other than subprocess.run in these two.
                forbidden_here = self.FORBIDDEN_NAMES - {"subprocess.run"}
            else:
                forbidden_here = self.FORBIDDEN_NAMES
            violations = forbidden_here & found_names
            assert not violations, (
                f"{method_name}() contains forbidden call(s): {violations}. "
                f"All filesystem mutations must go through _rsync_itemize (-n dry-run)."
            )

    def test_status_has_no_open_write_modes(self) -> None:
        """status() and helpers must not open files in write or append mode."""
        for method_name in self.STATUS_REACHABLE_METHODS:
            source = self._get_method_source(method_name)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_open = (isinstance(func, ast.Name) and func.id == "open") or (
                    isinstance(func, ast.Attribute) and func.attr == "open"
                )
                if not is_open:
                    continue
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        assert kw.value.value not in ("w", "a", "wb", "ab"), (
                            f"{method_name}() opens a file in write/append mode: "
                            f"mode={kw.value.value!r}"
                        )
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    assert node.args[1].value not in ("w", "a", "wb", "ab"), (
                        f"{method_name}() opens a file in write/append mode: "
                        f"{node.args[1].value!r}"
                    )


# ---------------------------------------------------------------------------
# Integration tests — real rsync, tmp_path trees
# ---------------------------------------------------------------------------


@pytest.mark.integration
# pylint: disable=too-many-public-methods
# Integration test class covers many independent rsync scenarios; splitting
# into multiple classes would reduce cohesion without reducing complexity.
class TestStatusIntegration:
    """Integration tests using real rsync invocations on tmp_path fixtures."""

    def _run_status(self, handler: PlanningSyncHandler) -> str:
        """Run handler.status() and return captured stdout."""

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        return buf.getvalue()

    def _first_line(self, handler: PlanningSyncHandler) -> str:
        """Return the first line of status() stdout."""
        return self._run_status(handler).splitlines()[0]

    # -- Basic drift states --------------------------------------------------

    def test_identical_trees_in_sync(self, sync_env) -> None:
        """Identical local and remote → STATUS: in-sync."""
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        _write_file(planning / "a.md")
        _write_file(gdrive_repo / "a.md")
        assert self._first_line(handler) == "STATUS: in-sync"

    def test_local_only_file_local_ahead(self, sync_env) -> None:
        """Local has a file not on remote → STATUS: local-ahead."""
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        _write_file(planning / "local_only.md")
        output = self._run_status(handler)
        assert output.splitlines()[0] == "STATUS: local-ahead"
        assert "local_only.md" in output

    def test_remote_only_file_remote_ahead(self, sync_env) -> None:
        """Remote has a file not on local → STATUS: remote-ahead."""
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        _write_file(gdrive_repo / "remote_only.md")
        output = self._run_status(handler)
        assert output.splitlines()[0] == "STATUS: remote-ahead"
        assert "remote_only.md" in output

    def test_same_file_edited_both_sides_diverged(self, sync_env) -> None:
        """Same file edited differently on both sides → STATUS: diverged."""
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        _write_file(planning / "shared.md", "local version")
        _write_file(gdrive_repo / "shared.md", "remote version")
        output = self._run_status(handler)
        assert output.splitlines()[0] == "STATUS: diverged"
        assert "shared.md" in output

    def test_locally_edited_remote_stale_diverged(self, sync_env) -> None:
        """File edited locally but remote has older content → STATUS: diverged.

        This is a documented oracle limitation (design §1.1 point 2): rsync uses
        size+mtime, so a locally-edited file is diverged even though the remote
        was previously canonical. The mtime-only note should NOT appear since
        this is a real content change.
        """
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        _write_file(gdrive_repo / "doc.md", "original")
        _write_file(planning / "doc.md", "locally modified with longer content")
        output = self._run_status(handler)
        assert output.splitlines()[0] == "STATUS: diverged"

    def test_same_content_different_mtime(self, sync_env) -> None:
        """Same content but forced different mtime → diverged with mtime-only note.

        rsync's size+mtime comparison sees a difference in BOTH directions when
        each side has a different mtime: push would send local→remote, and pull
        would also send remote→local (each side's "view" of the file differs).
        Therefore the state is diverged, not local-ahead.

        The mtime-only note must appear because size is identical and only
        mtime differs, confirming content may be identical.
        """
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        content = "identical content\n"
        local_file = planning / "same.md"
        remote_file = gdrive_repo / "same.md"
        local_file.write_text(content, encoding="utf-8")
        remote_file.write_text(content, encoding="utf-8")
        # Pin remote to a deterministic past mtime (year 2001); local stays
        # at current time (write_text above already set it to "now").
        # Both push and pull directions see a size+mtime difference → diverged.
        os.utime(str(remote_file), (1000000000, 1000000000))
        output = self._run_status(handler)
        # rsync size+mtime sees a difference in both directions → diverged.
        first = output.splitlines()[0]
        assert first == "STATUS: diverged"
        # The mtime-only note must appear because size is identical.
        assert "differ only in timestamp or permissions" in output

    def test_nested_ds_store_excluded(self, sync_env) -> None:
        """Nested .DS_Store is excluded; identical useful content → in-sync."""
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        _write_file(planning / "sub" / ".DS_Store", "mac metadata")
        _write_file(gdrive_repo / "sub" / ".DS_Store", "different mac metadata")
        _write_file(planning / "notes.md")
        _write_file(gdrive_repo / "notes.md")
        assert self._first_line(handler) == "STATUS: in-sync"

    def test_file_with_space_and_utf8(self, sync_env) -> None:
        """File path with space and UTF-8 character is classified correctly.

        rsync may encode non-ASCII bytes as octal escapes when LC_ALL=C is
        set, so we verify state classification rather than exact path strings
        in the output.
        """
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        fname = "my file — notes.md"  # em-dash via Unicode
        _write_file(planning / fname, "local content")
        output = self._run_status(handler)
        # State classification must be correct regardless of path encoding.
        assert output.splitlines()[0] == "STATUS: local-ahead"

    def test_empty_dir_only_local_in_sync(self, sync_env) -> None:
        """Empty directory only on local side → in-sync (documented §1.1 point 3)."""
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        (planning / "emptydir").mkdir()
        assert self._first_line(handler) == "STATUS: in-sync"

    # -- Missing remote path scenarios ---------------------------------------

    def test_missing_gdrive_repo_empty_local_in_sync(self, sync_env) -> None:
        """Missing gdrive_repo_path + empty local → in-sync; no mkdir on gdrive."""
        handler, planning, gdrive_repo = sync_env
        # gdrive_repo does NOT exist; local planning is empty.
        assert not gdrive_repo.exists()
        assert self._first_line(handler) == "STATUS: in-sync"
        # status() must not have created gdrive_repo.
        assert not gdrive_repo.exists()

    def test_missing_gdrive_repo_nonempty_local_local_ahead(self, sync_env) -> None:
        """Missing gdrive_repo_path + non-empty local → local-ahead; no mkdir."""
        handler, planning, gdrive_repo = sync_env
        _write_file(planning / "new.md")
        assert not gdrive_repo.exists()
        output = self._run_status(handler)
        assert output.splitlines()[0] == "STATUS: local-ahead"
        assert "new.md" in output
        assert not gdrive_repo.exists()

    # -- Error paths ---------------------------------------------------------

    def test_missing_gdrive_base_raises(self, sync_env) -> None:
        """Missing gdrive_base raises PlatformError before any rsync call."""
        handler, _, _ = sync_env
        # Replace gdrive_base with a path that doesn't exist.
        handler.paths.gdrive_base = handler.paths.gdrive_base / "nonexistent_mount"
        with pytest.raises(PlatformError, match="Google Drive not found"):
            handler.status()

    @pytest.mark.skipif(
        os.getuid() == 0,
        reason="root bypasses file permission checks; chmod 000 does not trigger rsync rc=23 as root",
    )
    def test_chmod_000_file_raises(self, sync_env) -> None:
        """Unreadable file under planning/ causes rsync rc=23 → PlatformError.

        Skipped when running as root (root bypasses chmod 000 restrictions so
        rsync exits 0 instead of 23) or when the rsync version on this system
        exits with a code other than 23 for permission errors (implementation-
        defined for some BSD/macOS rsync builds).

        The mock-based H4 tests cover rc=23 behaviour unconditionally; this test
        is the end-to-end probe that exercises the real rsync binary.
        """
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        secret = planning / "secret.md"
        _write_file(secret, "private")
        os.chmod(str(secret), 0o000)
        try:
            # Probe whether this rsync build actually returns rc=23 on this OS.
            # Only rc=23 is expected here; mock-based tests cover rc=1 generic.
            probe = subprocess.run(
                ["rsync", "-avn", "--itemize-changes", f"{planning}/", f"{gdrive_repo}/"],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode != 23:
                pytest.skip(
                    f"rsync returned rc={probe.returncode} (not 23) for chmod 000 on this system; "
                    "behaviour is implementation-defined. Mock-based rc=23 test covers this branch."
                )
            with pytest.raises(PlatformError, match="partial transfer"):
                handler.status()
        finally:
            os.chmod(str(secret), 0o644)

    def test_unrecognized_rsync_line_raises(self, sync_env) -> None:
        """An unrecognized line from rsync's itemize output raises PlatformError."""
        handler, _, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = ""
        fake_result.stdout = "xyzzy corrupted line\n"

        # Narrow patch: only intercept _rsync_itemize's subprocess.run call.
        # _verify_rsync_available is monkeypatched to a no-op so its subprocess
        # call is never reached, keeping the patch scope tight.
        with patch.object(handler, "_verify_rsync_available"):
            with patch("projctl.handlers.sync.subprocess.run", return_value=fake_result):
                with pytest.raises(PlatformError, match="xyzzy corrupted line"):
                    handler.status()

    def test_rsync_rc0_nonempty_stderr_raises(self, sync_env) -> None:
        """rsync exits 0 but writes to stderr → PlatformError."""
        handler, _, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stderr = "some unexpected warning from rsync"
        fake_result.stdout = ""

        # Narrow patch: bypass rsync availability check, patch only _rsync_itemize call.
        with patch.object(handler, "_verify_rsync_available"):
            with patch("projctl.handlers.sync.subprocess.run", return_value=fake_result):
                with pytest.raises(PlatformError, match="stderr"):
                    handler.status()

    # -- Read-only parametric tests ------------------------------------------

    @pytest.mark.parametrize(
        "tree_shape",
        ["flat", "nested", "only-excludes", "mixed"],
        ids=["flat", "nested", "only-excludes", "mixed"],
    )
    @pytest.mark.parametrize(
        "drift_state",
        ["in-sync", "local-ahead", "remote-ahead", "diverged"],
        ids=["in-sync", "local-ahead", "remote-ahead", "diverged"],
    )
    def test_readonly_parametric(
        self,
        tmp_path: Path,
        tree_shape: str,
        drift_state: str,
    ) -> None:
        """status() must not mutate any file on either side for any drift state."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        planning = repo / "planning"
        planning.mkdir()
        gdrive_base = tmp_path / "gdrive"
        gdrive_base.mkdir()
        gdrive_repo = gdrive_base / "backup" / "planning" / "repo"

        # Build tree according to shape.
        if tree_shape == "flat":
            _write_file(planning / "a.md", "hello")
            _write_file(planning / "b.md", "world")
        elif tree_shape == "nested":
            _write_file(planning / "sub" / "c.md", "nested")
            _write_file(planning / "d.md", "top")
        elif tree_shape == "only-excludes":
            _write_file(planning / ".DS_Store", "mac junk")
            _write_file(planning / "real.md", "keep")
        elif tree_shape == "mixed":
            _write_file(planning / "e.md", "mixed")
            _write_file(planning / "sub" / "f.md", "deep")
            _write_file(planning / ".DS_Store", "skip")

        # Mirror to remote and then apply drift.
        if drift_state != "local-ahead":
            # Copy planning → gdrive_repo so remote is not empty.
            gdrive_repo.mkdir(parents=True)
            for src in planning.rglob("*"):
                if src.is_file():
                    dst = gdrive_repo / src.relative_to(planning)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())

        if drift_state == "local-ahead":
            _write_file(planning / "local_extra.md", "extra")
        elif drift_state == "remote-ahead":
            _write_file(gdrive_repo / "remote_extra.md", "extra")
        elif drift_state == "diverged":
            _write_file(planning / "conflict.md", "local side")
            _write_file(gdrive_repo / "conflict.md", "remote side")
        # in-sync: no extra changes needed.

        config = _make_config(tmp_path, gdrive_base)
        orig_cwd = os.getcwd()
        os.chdir(str(repo))
        try:
            handler = PlanningSyncHandler(config)
        finally:
            os.chdir(orig_cwd)

        # Snapshot before.
        before_planning = _file_snapshot(planning)
        before_gdrive = _file_snapshot(gdrive_repo) if gdrive_repo.exists() else {}
        gdrive_planning_base = gdrive_base / "backup" / "planning"
        gdrive_base_existed = gdrive_base.exists()
        gdrive_planning_base_existed = gdrive_planning_base.exists()
        gdrive_repo_existed = gdrive_repo.exists()

        # Run status.

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        output = buf.getvalue()

        # Verify first line matches the exact expected state (TC-2).
        assert output.splitlines()[0] == f"STATUS: {drift_state}"

        # Snapshot after.
        after_planning = _file_snapshot(planning)
        after_gdrive = _file_snapshot(gdrive_repo) if gdrive_repo.exists() else {}

        assert before_planning == after_planning, "status() mutated local planning files"
        assert before_gdrive == after_gdrive, "status() mutated remote gdrive files"

        # gdrive directories must not be newly created.
        if not gdrive_base_existed:
            assert not gdrive_base.exists(), "status() created gdrive_base"
        if not gdrive_planning_base_existed:
            assert not gdrive_planning_base.exists(), "status() created gdrive_planning_base"
        if not gdrive_repo_existed:
            assert not gdrive_repo.exists(), "status() created gdrive_repo_path"

    # -- Oracle parity tests -------------------------------------------------

    def test_oracle_parity_push_direction(self, sync_env) -> None:
        """status() push-transfer paths match what real push(dry_run=True) would transfer."""
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)

        # Build 6 files with various states.
        _write_file(planning / "local_new.md", "local only")
        _write_file(planning / "shared.md", "common content")
        _write_file(gdrive_repo / "shared.md", "common content")
        _write_file(planning / "modified.md", "local version")
        _write_file(gdrive_repo / "modified.md", "old version")
        _write_file(gdrive_repo / "remote_new.md", "remote only")
        _write_file(planning / "sub" / "deep.md", "nested local")

        status_buf = io.StringIO()
        with redirect_stdout(status_buf):
            handler.status()
        status_output = status_buf.getvalue()

        # Run real dry-run push to capture rsync's itemize output.
        cmd = [
            "rsync",
            "-avn",
            "--delete",
            "--itemize-changes",
            "--exclude=*.swp",
            "--exclude=*~",
            "--exclude=.DS_Store",
            "--exclude=.workflow-safety.log",
            f"{planning}/",
            f"{gdrive_repo}/",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}, check=False
        )
        assert result.returncode == 0

        push_transfers_real: set[str] = set()
        push_deletes_real: set[str] = set()
        for line in result.stdout.splitlines():
            entry = _parse_itemize_line(line)
            if entry is None:
                continue
            if entry.op in ("<", ">") and entry.kind == "f":
                push_transfers_real.add(entry.path)
            elif entry.op == "*deleting":
                push_deletes_real.add(entry.path)

        # Extract status-reported push transfers from the output text.
        # The status report lists them under the "Local changes to push" header.
        status_push_transfers: set[str] = set()
        in_push_section = False
        for line in status_output.splitlines():
            if "Local changes to push" in line:
                in_push_section = True
                continue
            if in_push_section:
                if line.startswith("  ") and line.strip():
                    path = line.strip().replace(" [also differs on the other side]", "")
                    status_push_transfers.add(path)
                elif line.strip() == "" or (not line.startswith("  ") and line.strip()):
                    # Next section or blank separator
                    if line.strip() and not line.startswith("  "):
                        in_push_section = False

        assert push_transfers_real == status_push_transfers, (
            f"Oracle parity failure:\n"
            f"  rsync real push transfers: {push_transfers_real}\n"
            f"  status push transfers:     {status_push_transfers}"
        )

    # -- Idempotence tests ---------------------------------------------------

    def test_idempotence_push_cycle(self, sync_env) -> None:
        """status → local-ahead → push → status → in-sync → push → status → in-sync."""

        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        _write_file(planning / "new.md", "local content")

        # Initial status should be local-ahead.
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        assert buf.getvalue().splitlines()[0] == "STATUS: local-ahead"

        # Push (real, not dry-run) — create a non-dry-run handler.
        orig_cwd = os.getcwd()
        os.chdir(str(handler.repo_root))
        try:
            push_handler = PlanningSyncHandler(handler.config, dry_run=False)
        finally:
            os.chdir(orig_cwd)
        push_handler.push()

        # Status after push should be in-sync.
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        assert buf.getvalue().splitlines()[0] == "STATUS: in-sync"

        # Push again → still in-sync.
        push_handler.push()
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        assert buf.getvalue().splitlines()[0] == "STATUS: in-sync"

    # -- Stdout line-1 contract for all states -------------------------------

    @pytest.mark.parametrize(
        "state_setup",
        [
            ("in-sync", lambda p, r: None),
            (
                "local-ahead",
                lambda p, r: _write_file(p / "local.md", "new"),
            ),
            (
                "remote-ahead",
                lambda p, r: _write_file(r / "remote.md", "new"),
            ),
            (
                "diverged",
                lambda p, r: (
                    _write_file(p / "x.md", "local"),
                    _write_file(r / "x.md", "remote"),
                ),
            ),
        ],
        ids=["in-sync", "local-ahead", "remote-ahead", "diverged"],
    )
    def test_stdout_line1_contract(self, sync_env, state_setup) -> None:
        """First line of stdout is exactly 'STATUS: <state>' for each drift state."""

        expected_state, setup_fn = state_setup
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        setup_fn(planning, gdrive_repo)

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        lines = buf.getvalue().splitlines()
        assert len(lines) >= 1
        assert lines[0] == f"STATUS: {expected_state}"

    # -- H2: Oracle parity — pull direction ------------------------------------

    def test_oracle_parity_pull_direction(self, sync_env) -> None:
        """status() pull-transfer paths match what real pull(dry_run=True) would transfer (H2)."""
        handler, planning, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)

        # Seed: remote has files local does not (plus shared and modified files).
        _write_file(gdrive_repo / "remote_new.md", "remote only")
        _write_file(planning / "shared.md", "common content")
        _write_file(gdrive_repo / "shared.md", "common content")
        _write_file(gdrive_repo / "modified.md", "remote version")
        _write_file(planning / "modified.md", "old local version")
        _write_file(planning / "local_new.md", "local only")

        status_buf = io.StringIO()
        with redirect_stdout(status_buf):
            handler.status()
        status_output = status_buf.getvalue()

        # Run real dry-run pull rsync to capture itemize output.
        cmd = [
            "rsync",
            "-avn",
            "--delete",
            "--itemize-changes",
            "--exclude=*.swp",
            "--exclude=*~",
            "--exclude=.DS_Store",
            "--exclude=.workflow-safety.log",
            f"{gdrive_repo}/",
            f"{planning}/",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}, check=False
        )
        assert result.returncode == 0

        pull_transfers_real: set[str] = set()
        pull_deletes_real: set[str] = set()
        for line in result.stdout.splitlines():
            entry = _parse_itemize_line(line)
            if entry is None:
                continue
            if entry.op in ("<", ">") and entry.kind == "f":
                pull_transfers_real.add(entry.path)
            elif entry.op == "*deleting":
                pull_deletes_real.add(entry.path)

        # Extract pull transfers from status output (under "Remote changes to pull").
        status_pull_transfers: set[str] = set()
        in_pull_section = False
        for line in status_output.splitlines():
            if "Remote changes to pull" in line:
                in_pull_section = True
                continue
            if in_pull_section:
                if line.startswith("  ") and line.strip():
                    path = line.strip().replace(" [also differs on the other side]", "")
                    status_pull_transfers.add(path)
                elif line.strip() and not line.startswith("  "):
                    in_pull_section = False

        assert pull_transfers_real == status_pull_transfers, (
            f"Oracle parity failure (pull direction):\n"
            f"  rsync real pull transfers: {pull_transfers_real}\n"
            f"  status pull transfers:     {status_pull_transfers}"
        )

    # -- H3: Idempotence — pull cycle ------------------------------------------

    def test_idempotence_pull_cycle(self, sync_env) -> None:
        """remote-ahead → pull → in-sync → pull → in-sync (H3)."""

        handler, _, gdrive_repo = sync_env
        gdrive_repo.mkdir(parents=True)
        _write_file(gdrive_repo / "new.md", "remote content")

        # Initial status should be remote-ahead.
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        assert buf.getvalue().splitlines()[0] == "STATUS: remote-ahead"

        # Pull (real, not dry-run) — create a non-dry-run handler.
        orig_cwd = os.getcwd()
        os.chdir(str(handler.repo_root))
        try:
            pull_handler = PlanningSyncHandler(handler.config, dry_run=False)
        finally:
            os.chdir(orig_cwd)
        pull_handler.pull()

        # Status after pull should be in-sync.
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        assert buf.getvalue().splitlines()[0] == "STATUS: in-sync"

        # Pull again → still in-sync, zero transfers.
        pull_handler.pull()
        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()
        assert buf.getvalue().splitlines()[0] == "STATUS: in-sync"


# ---------------------------------------------------------------------------
# Unit tests — _is_timestamp_or_perms_only (M4)
# ---------------------------------------------------------------------------


class TestIsTimestampOrPermsOnly:
    """Unit tests for _is_timestamp_or_perms_only — pure function, no subprocess.

    One test per branch of the helper.  All inputs are 9-character strings
    matching rsync's cstpoguax layout.
    """

    def test_wrong_length_false(self) -> None:
        """Attribute string of wrong length → False."""
        assert _is_timestamp_or_perms_only("..t.....") is False  # 8 chars
        assert _is_timestamp_or_perms_only("..t..........") is False  # 13 chars

    def test_new_file_prefix_false(self) -> None:
        """'+++++++++ attrs (new file) → False."""
        assert _is_timestamp_or_perms_only("+++++++++") is False

    def test_checksum_changed_c_false(self) -> None:
        """Checksum flag 'c' at index 0 → False."""
        assert _is_timestamp_or_perms_only("cs.......") is False

    def test_checksum_changed_uppercase_c_false(self) -> None:
        """Checksum flag 'C' at index 0 → False."""
        assert _is_timestamp_or_perms_only("Cs.......") is False

    def test_size_changed_s_false(self) -> None:
        """Size flag 's' at index 1 → False."""
        assert _is_timestamp_or_perms_only("..s......") is False

    def test_size_changed_uppercase_s_false(self) -> None:
        """Size flag 'S' at index 1 → False."""
        assert _is_timestamp_or_perms_only(".St......") is False

    def test_mtime_t_true(self) -> None:
        """mtime flag 't' at index 2 with no other changes → True."""
        assert _is_timestamp_or_perms_only("..t......") is True

    def test_mtime_uppercase_t_true(self) -> None:
        """mtime flag 'T' at index 2 with no other changes → True."""
        assert _is_timestamp_or_perms_only("..T......") is True

    def test_perms_p_true(self) -> None:
        """Permissions flag 'p' at index 3, mtime unchanged → True (M1 broadening)."""
        assert _is_timestamp_or_perms_only("...p.....") is True

    def test_perms_uppercase_p_true(self) -> None:
        """Permissions flag 'P' at index 3, mtime unchanged → True."""
        assert _is_timestamp_or_perms_only("...P.....") is True

    def test_all_dots_no_mtime_no_perms_false(self) -> None:
        """All dots — no mtime or perms flag → False."""
        assert _is_timestamp_or_perms_only(".........") is False

    def test_owner_changed_false(self) -> None:
        """Owner flag at index 4 → False (metadata beyond mtime/perms changed)."""
        assert _is_timestamp_or_perms_only("..t.o....") is False

    def test_xattr_changed_false(self) -> None:
        """xattr flag at index 8 → False."""
        assert _is_timestamp_or_perms_only("..t.....x") is False

    def test_group_changed_false(self) -> None:
        """Group flag at index 5 → False."""
        assert _is_timestamp_or_perms_only("..t..g...") is False


# ---------------------------------------------------------------------------
# Unit tests — H1: leading-whitespace filename parser
# ---------------------------------------------------------------------------


class TestParseItemizeLineLeadingWhitespace:
    """Parser correctness for filenames that begin with whitespace (H1)."""

    def test_leading_space_filename_preserved(self) -> None:
        """Flag-block + exactly one separator space + one leading-space filename is correct.

        rsync line: '>f+++++++++ <space>leading.md'
        Expected: path == ' leading.md' (with the leading space).
        The old \\s+ regex would strip the leading space; the new literal-space
        regex preserves it.
        """
        entry = _parse_itemize_line(">f+++++++++  leading.md")
        assert entry is not None
        assert entry.path == " leading.md"

    def test_sent_garbage_raises(self) -> None:
        """'sent garbage' line must raise PlatformError (L1 anchor check)."""
        with pytest.raises(PlatformError):
            _parse_itemize_line("sent garbage")

    def test_sent_digits_banner_returns_none(self) -> None:
        """Anchored 'sent N bytes' banner returns None (real rsync format)."""
        assert _parse_itemize_line("sent 100 bytes  received 50 bytes") is None


# ---------------------------------------------------------------------------
# Unit tests — H4: rsync error-code branches (mock-based)
# ---------------------------------------------------------------------------


class TestRsyncErrorCodeBranches:
    """Mock-based tests for rsync rc=23 / rc=24 / rc=1 / FileNotFoundError (H4)."""

    def _make_bare_handler(self, tmp_path: Path) -> PlanningSyncHandler:
        """Return a bare PlanningSyncHandler with _verify_rsync_available no-oped."""
        planning = tmp_path / "planning"
        planning.mkdir()
        gdrive_base = tmp_path / "gdrive"
        gdrive_base.mkdir()
        gdrive_repo = gdrive_base / "backup" / "planning" / "repo"
        gdrive_repo.mkdir(parents=True)

        handler = PlanningSyncHandler.__new__(PlanningSyncHandler)
        handler.config = None  # type: ignore[assignment]
        handler.dry_run = False
        handler.repo_root = tmp_path / "repo"
        handler.repo_name = "repo"
        handler.paths = _SyncPaths(
            planning_path=planning,
            gdrive_base=gdrive_base,
            gdrive_planning_base=gdrive_base / "backup" / "planning",
            gdrive_repo_path=gdrive_repo,
            memory_path=None,
            gdrive_memory_path=tmp_path / "no-such-mem",  # non-existent sentinel
        )
        return handler

    def test_rc23_raises_partial_transfer(self, tmp_path: Path) -> None:
        """rsync rc=23 → PlatformError mentioning 'partial transfer'."""
        handler = self._make_bare_handler(tmp_path)
        fake = MagicMock()
        fake.returncode = 23
        fake.stderr = "some file vanished"
        fake.stdout = ""
        with patch.object(handler, "_verify_rsync_available"):
            with patch("projctl.handlers.sync.subprocess.run", return_value=fake):
                with pytest.raises(PlatformError, match="partial transfer"):
                    handler.status()

    def test_rc24_raises_vanished(self, tmp_path: Path) -> None:
        """rsync rc=24 → PlatformError mentioning 'vanished'."""
        handler = self._make_bare_handler(tmp_path)
        fake = MagicMock()
        fake.returncode = 24
        fake.stderr = "files vanished during scan"
        fake.stdout = ""
        with patch.object(handler, "_verify_rsync_available"):
            with patch("projctl.handlers.sync.subprocess.run", return_value=fake):
                with pytest.raises(PlatformError, match="vanished"):
                    handler.status()

    def test_rc1_raises_exit_code(self, tmp_path: Path) -> None:
        """Generic rsync rc=1 → PlatformError mentioning 'exit code 1'."""
        handler = self._make_bare_handler(tmp_path)
        fake = MagicMock()
        fake.returncode = 1
        fake.stderr = "something went wrong"
        fake.stdout = ""
        with patch.object(handler, "_verify_rsync_available"):
            with patch("projctl.handlers.sync.subprocess.run", return_value=fake):
                with pytest.raises(PlatformError, match="exit code 1"):
                    handler.status()

    def test_file_not_found_raises_not_installed(self, tmp_path: Path) -> None:
        """FileNotFoundError from subprocess.run → PlatformError mentioning 'not installed'."""
        handler = self._make_bare_handler(tmp_path)
        with patch.object(handler, "_verify_rsync_available"):
            with patch(
                "projctl.handlers.sync.subprocess.run",
                side_effect=FileNotFoundError("rsync: No such file or directory"),
            ):
                with pytest.raises(PlatformError, match="not installed"):
                    handler.status()


# ---------------------------------------------------------------------------
# Unit tests — H5: CLI-level dispatch for 'sync status'
# ---------------------------------------------------------------------------


class TestCliDispatchSyncStatus:
    """CLI-level dispatch tests for 'projctl sync status' (H5)."""

    def _make_config_file(self, tmp_path: Path) -> Path:
        """Write a minimal config file and return its path."""
        config_data: Dict[str, Any] = {
            "platform": "gitlab",
            "gitlab": {
                "default_group": "test/group",
                "labels": {"default": ["type::feature"]},
            },
            "planning_sync": {"gdrive_base": str(tmp_path / "gdrive")},
        }
        cfg = tmp_path / "config.yaml"
        with open(cfg, "w", encoding="utf-8") as fh:
            yaml.dump(config_data, fh)
        return cfg

    def test_dispatch_success_returns_0(self, tmp_path: Path) -> None:
        """main(['sync', 'status']) returns 0 and calls status() once."""
        cfg = self._make_config_file(tmp_path)
        calls: list[int] = []

        def fake_status(_handler: PlanningSyncHandler) -> None:
            calls.append(1)

        with patch.object(PlanningSyncHandler, "__init__", lambda s, c, dry_run=False: None):
            with patch.object(PlanningSyncHandler, "status", fake_status):
                rc = main(["--config", str(cfg), "sync", "status"])
        assert rc == 0
        assert len(calls) == 1

    def test_dispatch_platform_error_returns_1(self, tmp_path: Path) -> None:
        """main(['sync', 'status']) returns 1 when status() raises PlatformError."""
        cfg = self._make_config_file(tmp_path)

        def raise_error(_handler: PlanningSyncHandler) -> None:
            raise PlatformError("simulated error")

        with patch.object(PlanningSyncHandler, "__init__", lambda s, c, dry_run=False: None):
            with patch.object(PlanningSyncHandler, "status", raise_error):
                rc = main(["--config", str(cfg), "sync", "status"])
        assert rc == 1

    def test_cmd_sync_dry_run_fallback_no_attribute_error(self, tmp_path: Path) -> None:
        """cmd_sync does not raise AttributeError when args has no dry_run attribute.

        The status subparser intentionally omits --dry-run; cmd_sync uses
        getattr(args, 'dry_run', False) to handle this gracefully.
        """
        cfg = self._make_config_file(tmp_path)
        args = argparse.Namespace(config=str(cfg), sync_command="status")
        # args has no 'dry_run' attribute — must not raise.

        with patch.object(PlanningSyncHandler, "__init__", lambda s, c, dry_run=False: None):
            with patch.object(PlanningSyncHandler, "status", lambda s: None):
                rc = cmd_sync(args)
        assert rc == 0


# ---------------------------------------------------------------------------
# Unit tests — TestMemorySync: path helpers and push/pull/status memory legs
# ---------------------------------------------------------------------------


class TestMemorySync:
    """Unit tests for Claude memory sync helpers and the push/pull/status memory legs."""

    # -- Path helper tests ---------------------------------------------------

    def test_encode_repo_path_replaces_all_slashes(self) -> None:
        """_encode_repo_path replaces every '/' with '-', including the leading one."""
        assert _encode_repo_path(Path("/home/alice/proj")) == "-home-alice-proj"

    def test_encode_repo_path_preserves_leading_slash(self) -> None:
        """_encode_repo_path encodes the leading '/' as '-'."""
        assert _encode_repo_path(Path("/foo")) == "-foo"

    def test_claude_memory_path_structure(self) -> None:
        """_claude_memory_path returns ~/.claude/projects/<encoded>/memory."""
        result = _claude_memory_path(Path("/home/alice/proj"))
        # Verify the suffix is correct regardless of the actual home directory.
        assert result.parts[-1] == "memory"
        assert result.parts[-2] == "-home-alice-proj"
        assert result.parts[-3] == "projects"
        assert result.parts[-4] == ".claude"

    # -- Exclude constant tests -----------------------------------------------

    def test_memory_rsync_excludes_omits_memory_pattern(self) -> None:
        """_MEMORY_RSYNC_EXCLUDES does not contain the 'memory' pattern."""
        assert "memory" not in _MEMORY_RSYNC_EXCLUDES

    def test_memory_rsync_excludes_is_subset_of_rsync_excludes(self) -> None:
        """Every item in _MEMORY_RSYNC_EXCLUDES is also in RSYNC_EXCLUDES."""
        for pat in _MEMORY_RSYNC_EXCLUDES:
            assert pat in RSYNC_EXCLUDES

    # -- Push memory leg tests ------------------------------------------------

    def _make_bare_handler_with_memory(
        self, tmp_path: Path, memory_present: bool
    ) -> PlanningSyncHandler:
        """Build a bare handler with optional memory_path set."""
        planning = tmp_path / "planning"
        planning.mkdir()
        gdrive_base = tmp_path / "gdrive"
        gdrive_base.mkdir()
        gdrive_repo = gdrive_base / "backup" / "planning" / "repo"

        memory_dir: Path | None = None
        if memory_present:
            memory_dir = tmp_path / "memory"
            memory_dir.mkdir()

        handler = PlanningSyncHandler.__new__(PlanningSyncHandler)
        handler.config = None  # type: ignore[assignment]
        handler.dry_run = False
        handler.repo_root = tmp_path / "repo"
        handler.repo_name = "repo"
        handler.paths = _SyncPaths(
            planning_path=planning,
            gdrive_base=gdrive_base,
            gdrive_planning_base=gdrive_base / "backup" / "planning",
            gdrive_repo_path=gdrive_repo,
            memory_path=memory_dir,
            gdrive_memory_path=gdrive_base / "backup" / "claude-memory" / "-tmp-repo",
        )
        return handler

    def test_push_calls_rsync_twice_when_memory_present(self, tmp_path: Path) -> None:
        """push() invokes _run_rsync twice when a local memory directory exists."""
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=True)
        # gdrive_base already exists (created by _make_bare_handler_with_memory).

        calls: list[dict] = []

        def fake_run_rsync(source: Path, target: Path, description: str, **kwargs) -> None:
            calls.append({"source": source, "target": target, "kwargs": kwargs})

        with patch.object(handler, "_verify_rsync_available"):
            with patch.object(handler, "_run_rsync", side_effect=fake_run_rsync):
                handler.push()

        assert len(calls) == 2
        # Planning leg is first, memory leg is second.
        assert calls[0]["source"] == handler.planning_path
        assert calls[0]["target"] == handler.gdrive_repo_path
        assert calls[1]["source"] == handler.memory_path
        assert calls[1]["target"] == handler.gdrive_memory_path
        # Second call must use _MEMORY_RSYNC_EXCLUDES.
        second_call_excludes = calls[1]["kwargs"].get("excludes")
        assert second_call_excludes == _MEMORY_RSYNC_EXCLUDES

    def test_push_calls_rsync_once_when_memory_absent(self, tmp_path: Path) -> None:
        """push() invokes _run_rsync only once when no local memory directory exists."""
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=False)

        calls: list[dict] = []

        def fake_run_rsync(source: Path, target: Path, description: str, **kwargs) -> None:
            calls.append({"source": source, "target": target})

        with patch.object(handler, "_verify_rsync_available"):
            with patch.object(handler, "_run_rsync", side_effect=fake_run_rsync):
                handler.push()

        assert len(calls) == 1
        # The single call is the planning leg.
        assert calls[0]["source"] == handler.planning_path
        assert calls[0]["target"] == handler.gdrive_repo_path

    # -- Pull memory leg tests ------------------------------------------------

    def test_pull_calls_rsync_twice_when_gdrive_memory_exists(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """pull() invokes _run_rsync twice when the GDrive memory path exists."""
        monkeypatch.setattr(
            "projctl.handlers.sync._claude_projects_root",
            lambda: tmp_path / ".claude" / "projects",
        )
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=False)
        # Make the gdrive_repo_path exist so pull() doesn't raise.
        handler.paths.gdrive_repo_path.mkdir(parents=True, exist_ok=True)

        calls: list[dict] = []

        def fake_run_rsync(source: Path, target: Path, description: str, **kwargs) -> None:
            calls.append({"source": source, "target": target, "kwargs": kwargs})

        gdrive_mem = tmp_path / "gdrive-mem-exists"
        gdrive_mem.mkdir(parents=True, exist_ok=True)

        with patch.object(handler, "_verify_rsync_available"):
            with patch.object(handler, "_run_rsync", side_effect=fake_run_rsync):
                # Simulate GDrive memory directory existing.
                with patch.object(
                    type(handler), "gdrive_memory_path", new_callable=lambda: property(
                        lambda self: gdrive_mem
                    )
                ):
                    handler.pull()

        assert len(calls) == 2
        # Planning leg is first, memory leg is second.
        assert calls[0]["source"] == handler.gdrive_repo_path
        assert calls[0]["target"] == handler.planning_path
        assert calls[1]["source"] == gdrive_mem
        second_call_excludes = calls[1]["kwargs"].get("excludes")
        assert second_call_excludes == _MEMORY_RSYNC_EXCLUDES

    def test_pull_calls_rsync_once_when_gdrive_memory_absent(self, tmp_path: Path) -> None:
        """pull() invokes _run_rsync only once when GDrive memory path is absent."""
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=False)
        handler.paths.gdrive_repo_path.mkdir(parents=True, exist_ok=True)

        calls: list[dict] = []

        def fake_run_rsync(source: Path, target: Path, description: str, **kwargs) -> None:
            calls.append({"source": source, "target": target})

        with patch.object(handler, "_verify_rsync_available"):
            with patch.object(handler, "_run_rsync", side_effect=fake_run_rsync):
                # gdrive_memory_path points to a path that does not exist.
                handler.pull()

        assert len(calls) == 1
        # The single call is the planning leg.
        assert calls[0]["source"] == handler.gdrive_repo_path
        assert calls[0]["target"] == handler.planning_path

    # -- Dry-run push includes memory leg ------------------------------------

    def test_push_dry_run_includes_memory_leg(self, tmp_path: Path) -> None:
        """In dry-run mode, push() still calls _run_rsync twice when memory is present."""
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=True)
        handler.dry_run = True

        calls: list[dict] = []

        def fake_run_rsync(source: Path, target: Path, description: str, **kwargs) -> None:
            calls.append({"source": source, "target": target, "kwargs": kwargs})

        with patch.object(handler, "_verify_rsync_available"):
            with patch.object(handler, "_run_rsync", side_effect=fake_run_rsync):
                handler.push()

        assert len(calls) == 2
        # Planning leg is first, memory leg is second.
        assert calls[0]["source"] == handler.planning_path
        assert calls[0]["target"] == handler.gdrive_repo_path
        assert calls[1]["source"] == handler.memory_path
        assert calls[1]["target"] == handler.gdrive_memory_path

    # -- _print_memory_status output tests ------------------------------------

    def test_status_memory_section_both_absent(self, tmp_path: Path) -> None:
        """When neither local nor GDrive memory exists, output contains 'nothing to sync'."""
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=False)
        # gdrive_memory_path does not exist (default in bare handler).

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler._print_memory_status()

        assert "nothing to sync" in buf.getvalue()

    def test_status_memory_section_local_only(self, tmp_path: Path) -> None:
        """When only local memory exists, output contains 'local-ahead'."""
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=True)
        # gdrive_memory_path does not exist.

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler._print_memory_status()

        assert "local-ahead" in buf.getvalue()

    def test_status_memory_section_remote_only(self, tmp_path: Path) -> None:
        """When only GDrive memory exists, output contains 'remote-ahead'."""
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=False)

        # Create the gdrive_memory_path so it appears to exist.
        gdrive_mem = tmp_path / "gdrive" / "backup" / "claude-memory" / "-tmp-repo"
        gdrive_mem.mkdir(parents=True, exist_ok=True)
        # Update the path in _SyncPaths to point to the created directory.
        handler.paths = _SyncPaths(
            planning_path=handler.paths.planning_path,
            gdrive_base=handler.paths.gdrive_base,
            gdrive_planning_base=handler.paths.gdrive_planning_base,
            gdrive_repo_path=handler.paths.gdrive_repo_path,
            memory_path=None,
            gdrive_memory_path=gdrive_mem,
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler._print_memory_status()

        assert "remote-ahead" in buf.getvalue()

    # -- [T5] "both present" branch — uses real rsync -------------------------

    def _make_handler_with_both_memory_dirs(
        self, tmp_path: Path, repo: Path
    ) -> tuple["PlanningSyncHandler", Path, Path]:
        """Create a real handler (via PlanningSyncHandler.__init__) with both memory dirs.

        Returns (handler, local_memory_dir, gdrive_memory_dir).
        """
        planning = repo / "planning"
        planning.mkdir(exist_ok=True)
        gdrive_base = tmp_path / "gdrive"
        gdrive_base.mkdir(exist_ok=True)

        config = _make_config(tmp_path, gdrive_base)

        orig_cwd = os.getcwd()
        os.chdir(str(repo))
        try:
            handler = PlanningSyncHandler(config)
        finally:
            os.chdir(orig_cwd)

        return handler, handler.paths.gdrive_base, handler.paths.gdrive_memory_path

    def _make_memory_env(self, tmp_path: Path) -> tuple["PlanningSyncHandler", Path, Path]:
        """Bootstrap a fake git repo and handler with memory dirs accessible."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        planning = repo / "planning"
        planning.mkdir()
        gdrive_base = tmp_path / "gdrive"
        gdrive_base.mkdir()

        config = _make_config(tmp_path, gdrive_base)

        orig_cwd = os.getcwd()
        os.chdir(str(repo))
        try:
            handler = PlanningSyncHandler(config)
        finally:
            os.chdir(orig_cwd)

        # Derive the memory paths from what the handler computed.
        local_mem = handler.paths.gdrive_base.parent  # not the memory — use repo root
        local_mem = handler.gdrive_memory_path  # gdrive side
        # The local memory path is ~/.claude/projects/<encoded-repo>/memory — we need to
        # create it and register it on the handler directly for test purposes.
        local_memory = tmp_path / "local-memory"
        local_memory.mkdir()
        gdrive_memory = tmp_path / "gdrive" / "backup" / "claude-memory" / "test-repo"
        gdrive_memory.mkdir(parents=True, exist_ok=True)

        # Override the paths dataclass fields with test-local directories.
        handler.paths = _SyncPaths(
            planning_path=handler.paths.planning_path,
            gdrive_base=handler.paths.gdrive_base,
            gdrive_planning_base=handler.paths.gdrive_planning_base,
            gdrive_repo_path=handler.paths.gdrive_repo_path,
            memory_path=local_memory,
            gdrive_memory_path=gdrive_memory,
        )
        return handler, local_memory, gdrive_memory

    @pytest.mark.integration
    def test_status_memory_section_both_in_sync(self, tmp_path: Path) -> None:
        """Both memory dirs exist with identical content → 'Memory STATUS: in-sync'."""
        handler, local_memory, gdrive_memory = self._make_memory_env(tmp_path)

        # Write identical file in both dirs.
        _write_file(local_memory / "MEMORY.md", "# memory\nsome content\n")
        _write_file(gdrive_memory / "MEMORY.md", "# memory\nsome content\n")

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler._print_memory_status()

        assert "Memory STATUS: in-sync" in buf.getvalue()

    @pytest.mark.integration
    def test_status_memory_section_both_local_ahead(self, tmp_path: Path) -> None:
        """Local memory has a file GDrive does not → 'Memory STATUS: local-ahead' + filename."""
        handler, local_memory, gdrive_memory = self._make_memory_env(tmp_path)

        # Only local has a file.
        _write_file(local_memory / "extra.md", "local-only note\n")

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler._print_memory_status()

        output = buf.getvalue()
        assert "Memory STATUS: local-ahead" in output
        assert "extra.md" in output

    # -- [T6] PlatformError fallback in _print_memory_status ------------------

    def test_status_memory_section_rsync_error_is_nonfatal(self, tmp_path: Path) -> None:
        """_print_memory_status does not raise when _rsync_itemize raises PlatformError."""
        handler, local_memory, gdrive_memory = self._make_memory_env(tmp_path)
        # Both dirs exist so the rsync itemize branch is entered.
        _write_file(local_memory / "a.md", "x")
        _write_file(gdrive_memory / "a.md", "x")

        def exploding_itemize(source: Path, target: Path, **kwargs) -> list:
            raise PlatformError("boom")

        buf = io.StringIO()
        with patch.object(handler, "_rsync_itemize", side_effect=exploding_itemize):
            with redirect_stdout(buf):
                # Must not raise.
                handler._print_memory_status()

        assert "Memory: status check failed" in buf.getvalue()
        assert "boom" in buf.getvalue()

    # -- [T3] Planning succeeds, memory leg fails ------------------------------

    def test_push_memory_leg_failure_propagates(self, tmp_path: Path) -> None:
        """push() propagates PlatformError from the memory leg after planning succeeds."""
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=True)
        printed: list[str] = []
        call_count = [0]

        def fake_run_rsync(source: Path, target: Path, description: str, **kwargs) -> None:
            call_count[0] += 1
            if call_count[0] == 2:
                raise PlatformError("memory rsync failed")

        buf = io.StringIO()
        with patch.object(handler, "_verify_rsync_available"):
            with patch.object(handler, "_run_rsync", side_effect=fake_run_rsync):
                with redirect_stdout(buf):
                    with pytest.raises(PlatformError, match="memory rsync failed"):
                        handler.push()

        # Planning success print must have appeared before the exception.
        assert "✓ Pushed" in buf.getvalue() and "planning" in buf.getvalue()

    def test_pull_memory_leg_failure_propagates(self, tmp_path: Path, monkeypatch) -> None:
        """pull() propagates PlatformError from the memory leg after planning succeeds."""
        monkeypatch.setattr(
            "projctl.handlers.sync._claude_projects_root",
            lambda: tmp_path / ".claude" / "projects",
        )
        handler = self._make_bare_handler_with_memory(tmp_path, memory_present=False)
        handler.paths.gdrive_repo_path.mkdir(parents=True, exist_ok=True)
        call_count = [0]

        def fake_run_rsync(source: Path, target: Path, description: str, **kwargs) -> None:
            call_count[0] += 1
            if call_count[0] == 2:
                raise PlatformError("memory rsync failed")

        gdrive_mem = tmp_path / "gdrive-mem-exists"
        gdrive_mem.mkdir(parents=True, exist_ok=True)

        buf = io.StringIO()
        with patch.object(handler, "_verify_rsync_available"):
            with patch.object(handler, "_run_rsync", side_effect=fake_run_rsync):
                with patch.object(
                    type(handler), "gdrive_memory_path", new_callable=lambda: property(
                        lambda self: gdrive_mem
                    )
                ):
                    with redirect_stdout(buf):
                        with pytest.raises(PlatformError, match="memory rsync failed"):
                            handler.pull()

        # Planning success print must have appeared before the exception.
        assert "✓ Pulled" in buf.getvalue() and "planning" in buf.getvalue()

    # -- CR-4: oracle correctness — pull direction uses delete=False ----------

    @pytest.mark.integration
    def test_status_memory_pull_direction_is_additive_oracle(self, tmp_path: Path) -> None:
        """_print_memory_status() uses delete=False for the pull-direction _rsync_itemize call.

        Local memory has a.md; GDrive memory has b.md.  Because pull() is
        additive (delete=False), a pull would never delete a.md — and the
        oracle must not report it as a pull-delete candidate either.
        """
        handler, local_memory, gdrive_memory = self._make_memory_env(tmp_path)
        _write_file(local_memory / "a.md", "local content")
        _write_file(gdrive_memory / "b.md", "remote content")

        itemize_calls: list[dict] = []
        original_itemize = handler._rsync_itemize

        def spy_itemize(source: Path, target: Path, **kwargs) -> list:
            itemize_calls.append({"source": source, "target": target, "kwargs": kwargs})
            return original_itemize(source, target, **kwargs)

        buf = io.StringIO()
        with patch.object(handler, "_rsync_itemize", side_effect=spy_itemize):
            with redirect_stdout(buf):
                handler._print_memory_status()

        output = buf.getvalue()
        # Memory section must be present (both dirs exist and differ).
        assert "Memory STATUS:" in output

        # Identify pull-direction call: source is gdrive_memory, target is local_memory.
        pull_calls = [
            c for c in itemize_calls if c["source"] == gdrive_memory
        ]
        assert pull_calls, "No pull-direction _rsync_itemize call was recorded"
        for call in pull_calls:
            assert call["kwargs"].get("delete") is False, (
                "Pull-direction _rsync_itemize must use delete=False to match pull() behavior"
            )

        # a.md is local-only; because delete=False the oracle must not list it
        # under any "would be deleted" section for the pull direction.
        assert "Local files a pull would DELETE" not in output

    # -- [C2] CLI exit code when memory leg fails ------------------------------

    def test_cmd_sync_push_returns_1_when_memory_fails(self, tmp_path: Path) -> None:
        """cmd_sync returns 1 when push() raises PlatformError (e.g. memory leg failure)."""
        config_data: Dict[str, Any] = {
            "platform": "gitlab",
            "gitlab": {
                "default_group": "test/group",
                "labels": {"default": ["type::feature"]},
            },
            "planning_sync": {"gdrive_base": str(tmp_path / "gdrive")},
        }
        cfg = tmp_path / "config.yaml"
        with open(cfg, "w", encoding="utf-8") as fh:
            yaml.dump(config_data, fh)

        args = argparse.Namespace(config=str(cfg), sync_command="push", dry_run=False)

        with patch.object(PlanningSyncHandler, "__init__", lambda s, c, dry_run=False: None):
            with patch.object(
                PlanningSyncHandler,
                "push",
                side_effect=PlatformError("memory rsync failed"),
            ):
                rc = cmd_sync(args)

        assert rc == 1

    # -- [C3] status() line 1 remains STATUS: <state> after memory block ------

    @pytest.mark.integration
    def test_status_line1_is_planning_state_with_memory_drift(self, tmp_path: Path) -> None:
        """Line 1 of status() output is 'STATUS: <state>', not a Memory line."""
        handler, local_memory, gdrive_memory = self._make_memory_env(tmp_path)
        # Set up planning as in-sync (no remote path yet → status will use tempdir).
        # Local memory has a file GDrive does not, so memory section shows local-ahead.
        _write_file(local_memory / "drift.md", "local-only memory content")

        buf = io.StringIO()
        with redirect_stdout(buf):
            handler.status()

        first_line = buf.getvalue().splitlines()[0]
        assert first_line.startswith("STATUS: ")
        assert "Memory" not in first_line
