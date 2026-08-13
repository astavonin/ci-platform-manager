"""Report which issues (and MRs) show local evidence of work on a given date.

Purely local — git plus the on-disk planning/ tree, no GitLab API, no
network, no platform gate. That is what makes it testable with fixed
transcripts and usable offline, and is why it lives outside
`timelog.py` (which is GitLab-only by design).
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..exceptions import PlatformError

logger = logging.getLogger(__name__)

# Reflog subjects treated as evidence of work, per the convention this
# command replaces (~/.claude/commands/log-time.md): a day's work is
# usually one commit amended repeatedly, so only these two subjects count.
# "commit (initial):" (a repository's very first commit) and everything
# else (checkout, merge, rebase, pull, ...) is deliberately excluded.
_COMMIT_SUBJECT_RE = re.compile(r"^commit(?: \(amend\))?:")

# Reflog line shape under --date=iso-strict: "<sha> <ref>@{<iso8601>}: <message>".
_REFLOG_LINE_RE = re.compile(r"^[0-9a-fA-F]+\s+\S+@\{(?P<date>[^}]+)\}:\s?(?P<message>.*)$")

_ISSUE_REF_RE = re.compile(r"Ref #(\d+)")

# <type>/<N>-<slug>, e.g. "feature/4-alpha-svc-update" -> issue 4. A branch
# with no numeric segment after the first '/' (or no '/' at all, e.g.
# "main") yields no issue.
_BRANCH_ISSUE_RE = re.compile(r"^[^/]+/(\d+)-")

# git status --porcelain -z status codes that carry an extra NUL-separated
# "original path" field (see _parse_status_entries).
_RENAME_OR_COPY_CODES = frozenset("RC")

# planning/<epic>/milestone-XX-<name>/issues/<N>-<slug>/... — the issue id
# is the digits right after "issues/", however many path segments follow
# (a file directly in the folder, or nested several levels below it).
# int() on the captured group strips any zero-padding ("054" -> 54).
_ISSUE_FOLDER_RE = re.compile(r"(?:^|/)issues/(\d+)-[^/]+/")

# planning/<epic>/reviews/MR<N>-review.yaml exactly. The literal "reviews/"
# (not a prefix match) is what keeps "reviews-orphan/" — hand-managed
# reviews for unlinked work — from matching.
_MR_REVIEW_FILE_RE = re.compile(r"(?:^|/)reviews/MR(\d+)-review\.yaml$")


@dataclass(frozen=True)
class IssueActivity:
    """One issue's — or, when `via` is "review", one MR's — evidence of
    work on the reported date.

    `id` is a merge-request number for via="review" and an issue number
    for every other `via`. GitLab issue and MR numbers are independent
    sequences (id collisions across the two are expected, not a bug), so
    a caller must already switch on `via` to know which tracker endpoint
    a given id belongs to; a separate `mr_id` field would only give the
    caller a second place to make that same check.
    """

    issue_id: int
    via: str  # "reflog" | "branch" | "planning" | "review"
    count: int  # matching entries: reflog events, or modified files

    def to_dict(self) -> Dict[str, Any]:
        """Render as the caller-facing JSON shape.

        `count` is uniform across all four sources. A per-source key name
        (as before this handler had only two sources: `events` for reflog
        vs `files` for branch) forces every caller to branch on `via` just
        to find the number — a cost that only grows as sources are added.
        """
        return {"id": self.issue_id, "via": self.via, "count": self.count}


@dataclass(frozen=True)
class ActivityReport:
    """The full result of one `projctl activity [DATE]` run."""

    activity_date: date
    repo: str
    issues: List[IssueActivity]
    unattributed_events: int
    unattributed_files: int
    unattributed_branch: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        """Render the report in the stable JSON shape documented for `--json`."""
        return {
            "date": self.activity_date.isoformat(),
            "repo": self.repo,
            "issues": [issue.to_dict() for issue in self.issues],
            "unattributed": {
                "events": self.unattributed_events,
                "files": self.unattributed_files,
                "branch": self.unattributed_branch,
            },
        }

    def print_table(self) -> None:
        """Print a human-readable summary to stdout."""
        print(f"Activity for {self.repo} on {self.activity_date.isoformat()}")
        print()
        if not self.issues:
            print("No attributed issues.")
        else:
            for issue in self.issues:
                unit = "event" if issue.via == "reflog" else "file"
                plural = "" if issue.count == 1 else "s"
                # A "review" id is a merge-request number, not an issue
                # number — the '!' prefix (this project's own MR-sigil
                # convention) keeps the two from being read as the same kind
                # of reference in the printed table.
                prefix = "!" if issue.via == "review" else "#"
                print(
                    f"  {prefix}{issue.issue_id}  via {issue.via}  "
                    f"({issue.count} {unit}{plural})"
                )

        if self.unattributed_events or self.unattributed_files:
            print()
            print("Unattributed:")
            if self.unattributed_events:
                print(f"  {self.unattributed_events} reflog event(s) with no Ref #N")
            if self.unattributed_files:
                branch_label = self.unattributed_branch or "(detached HEAD)"
                print(
                    f"  {self.unattributed_files} modified file(s) on branch "
                    f"{branch_label!r} (no issue number in branch name)"
                )


# pylint: disable=too-few-public-methods
# ActivityHandler is intentionally a single-responsibility class with one
# public entry point (report()), matching LabelsHandler's precedent — adding
# an artificial second public method to satisfy the checker would violate
# single-responsibility for no benefit.
class ActivityHandler:
    """Reports which issues (and MRs) show local evidence of work on a date.

    Four evidence sources:

    1. reflog (_scan_reflog) — 'Ref #<N>' in a commit/amend subject.
    2. branch (_scan_working_tree + _current_branch), consulted only when
       the reflog attributes no issue at all — a day's work with no commit
       yet still shows up as modified files, but a stale branch checkout
       with no file changes must contribute nothing.
    3. planning (_scan_planning_tree) — a file under
       planning/**/issues/<N>-<slug>/ modified on the date.
    4. review (_scan_planning_tree) — a
       planning/**/reviews/MR<N>-review.yaml modified on the date.

    Sources 3 and 4 walk the planning/ tree directly by mtime rather than
    going through `git status` like source 2 does: a planning doc is
    often committed the same day it is written, so by the time this runs
    it may no longer be "dirty", but its mtime still marks the day the
    work happened. They run unconditionally, independent of whether the
    reflog or branch already attributed something — see report() for how
    all four combine, and why an issue can carry more than one entry.
    """

    def __init__(self, tz: Optional[tzinfo] = None, cwd: Optional[Path] = None) -> None:
        """Initialize the handler.

        Args:
            tz: Overrides the system local timezone used for date
                bucketing. Test seam only, mirroring
                TimelogHandler.__init__(tz=...) — production callers
                always pass None (system local time).
            cwd: Overrides the directory git commands run in. Test seam
                only; production callers always pass None (Path.cwd()).
        """
        self._tz = tz
        self._cwd = cwd or Path.cwd()

    # ------------------------------------------------------------------
    # Date / clock handling
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date_arg(value: str) -> date:
        """Parse a YYYY-MM-DD date argument.

        Raises:
            ValueError: If the value is not a valid calendar date in that format.
        """
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc

    def _now(self) -> datetime:
        """Return the current instant in this handler's local tz, always aware.

        Routed through timezone.utc first rather than the shorter
        `datetime.now(self._tz)`: with no injected tz (self._tz is None,
        the production path), `datetime.now(None)` returns a *naive*
        datetime, while every other datetime this handler produces is
        aware (reflog timestamps carry their own offset; mtimes are
        converted via `.astimezone(self._tz)`) — see
        TimelogHandler._now() for the same reasoning against the same
        naive/aware hazard.
        """
        return datetime.now(timezone.utc).astimezone(self._tz)

    # ------------------------------------------------------------------
    # Git transport
    # ------------------------------------------------------------------

    def _run_git(self, args: List[str]) -> str:
        """Run a git subcommand in self._cwd and return its stdout.

        Raises:
            PlatformError: If the git executable is not on PATH, or the
                command exits non-zero (e.g. self._cwd is not inside a
                git work tree).
        """
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=self._cwd,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PlatformError(
                "git executable not found on PATH — 'projctl activity' requires git."
            ) from exc
        if result.returncode != 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            )
            raise PlatformError(f"'git {' '.join(args)}' failed: {detail}")
        return result.stdout

    def _repo_name(self) -> str:
        """Return the repository's directory name, and validate this is a git repo.

        Raises:
            PlatformError: If self._cwd is not inside a git work tree (via
                _run_git), or the resolved toplevel path is empty.
        """
        toplevel = self._run_git(["rev-parse", "--show-toplevel"]).strip()
        if not toplevel:
            raise PlatformError(f"'git rev-parse --show-toplevel' returned no path in {self._cwd}")
        return Path(toplevel).name

    # ------------------------------------------------------------------
    # Reflog evidence
    # ------------------------------------------------------------------

    def _scan_reflog(self, target: date) -> Tuple[Dict[int, int], int]:
        """Scan the reflog for commit/amend entries whose local date is `target`.

        Returns:
            Tuple of (per-issue event counts keyed by issue id, count of
            matching entries that carried no `Ref #<N>` — reported, never
            dropped, since a day's work that produced no attributable
            entry is exactly the case a silent zero would misrepresent).
        """
        output = self._run_git(["reflog", "--date=iso-strict"])
        per_issue: Dict[int, int] = {}
        unattributed = 0
        for line in output.splitlines():
            if not line.strip():
                continue
            match = _REFLOG_LINE_RE.match(line)
            if not match:
                logger.debug("Skipping unparsable reflog line: %r", line)
                continue
            message = match.group("message")
            if not _COMMIT_SUBJECT_RE.match(message):
                continue
            try:
                entry_dt = datetime.fromisoformat(match.group("date"))
            except ValueError:
                logger.debug("Skipping reflog entry with unparsable date: %r", line)
                continue
            # entry_dt already carries the offset git recorded for it, so
            # .astimezone(self._tz) alone (no naive-localize step, unlike
            # TimelogHandler._localize) converts it into this handler's
            # local day.
            if entry_dt.astimezone(self._tz).date() != target:
                continue
            ref_match = _ISSUE_REF_RE.search(message)
            if ref_match:
                issue_id = int(ref_match.group(1))
                per_issue[issue_id] = per_issue.get(issue_id, 0) + 1
            else:
                unattributed += 1
        return per_issue, unattributed

    # ------------------------------------------------------------------
    # Working-tree evidence
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_status_entries(output: str) -> List[str]:
        """Parse `git status --porcelain -z` output into a list of current paths.

        NUL-separated per the -z contract, so a path containing a space
        needs no special handling (no shell quoting to undo). A
        rename/copy entry ('R'/'C' in either status column) carries an
        extra NUL-terminated field holding the pre-rename path
        immediately after the current path; that field is consumed and
        discarded here since the *current* path is what has a real mtime
        to check against.
        """
        tokens = [token for token in output.split("\0") if token]
        paths: List[str] = []
        i = 0
        while i < len(tokens):
            status, path = tokens[i][:2], tokens[i][3:]
            paths.append(path)
            i += 2 if _RENAME_OR_COPY_CODES.intersection(status) else 1
        return paths

    def _mtime_on_date(self, path: Path, target: date) -> bool:
        """Return whether `path`'s on-disk mtime falls on `target`, local time.

        Shared by _scan_working_tree and _scan_planning_tree. A path that
        no longer exists on disk (e.g. staged for deletion) has no mtime
        left to check and is treated as not matching — its absence is not
        evidence of work on any particular date.

        st_mtime is a naive instant in absolute (UTC-equivalent) time;
        attaching timezone.utc before converting to local time is what
        keeps a file touched before 06:00 local (this host's UTC+6) from
        being misclassified into the previous local day.
        """
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone(self._tz)
        return mtime_dt.date() == target

    def _scan_working_tree(self, target: date) -> List[str]:
        """Return paths (relative to self._cwd) whose mtime falls on `target`, local time.

        Evidence is `git status --porcelain -z`'s *current* working-tree
        state — staged, unstaged, and untracked changes — matched against
        each file's on-disk mtime.
        """
        output = self._run_git(["status", "--porcelain", "-z"])
        return [
            rel_path
            for rel_path in self._parse_status_entries(output)
            if self._mtime_on_date(self._cwd / rel_path, target)
        ]

    def _current_branch(self) -> Optional[str]:
        """Return the checked-out branch name, or None in detached HEAD."""
        branch = self._run_git(["branch", "--show-current"]).strip()
        return branch or None

    # ------------------------------------------------------------------
    # Planning-tree evidence
    # ------------------------------------------------------------------

    def _scan_planning_tree(self, target: date) -> Tuple[Dict[int, int], Dict[int, int]]:
        """Scan planning/ for issue-folder and MR-review-yaml files mtimed on `target`.

        Unlike _scan_working_tree, this walks the filesystem directly
        instead of going through `git status`: a planning doc is often
        committed the same day it is written, so by the time this runs it
        may no longer be "dirty" in git's eyes, even though its mtime
        still marks the day the work happened. A repo with no planning/
        directory (e.g. one that hasn't adopted this planning layout)
        yields nothing rather than an error.

        Returns:
            Tuple of (issue id -> matching file count, MR id -> matching
            file count).
        """
        planning_dir = self._cwd / "planning"
        if not planning_dir.is_dir():
            return {}, {}

        issue_counts: Dict[int, int] = {}
        mr_counts: Dict[int, int] = {}
        for path in planning_dir.rglob("*"):
            if not path.is_file() or not self._mtime_on_date(path, target):
                continue
            rel_posix = path.relative_to(self._cwd).as_posix()
            issue_match = _ISSUE_FOLDER_RE.search(rel_posix)
            if issue_match:
                issue_id = int(issue_match.group(1))
                issue_counts[issue_id] = issue_counts.get(issue_id, 0) + 1
                continue
            mr_match = _MR_REVIEW_FILE_RE.search(rel_posix)
            if mr_match:
                mr_id = int(mr_match.group(1))
                mr_counts[mr_id] = mr_counts.get(mr_id, 0) + 1
        return issue_counts, mr_counts

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def report(self, date_str: Optional[str] = None) -> ActivityReport:
        """Build an activity report for `date_str` (default: today, local date).

        The reflog, and the planning-tree sources (issue folders, MR
        review yaml), are each consulted unconditionally and independently
        — an issue can end up with more than one entry when several
        sources corroborate it. The working tree is the one source that
        stays gated: it is consulted only when the reflog attributes no
        issue at all, and within it the branch name is consulted only once
        a file modified on the date is actually found — a branch left
        checked out from unrelated older work must never contribute on its
        own. See ActivityHandler's class docstring for why sources 3 and 4
        don't share that gate.

        Args:
            date_str: Date to report, YYYY-MM-DD; defaults to today (local date).

        Raises:
            ValueError: If date_str is not a valid YYYY-MM-DD date.
            PlatformError: If self._cwd is not inside a git work tree, or
                git is not installed.
        """
        target = self._parse_date_arg(date_str) if date_str else self._now().date()
        repo = self._repo_name()

        per_issue, unattributed_events = self._scan_reflog(target)
        issues = [
            IssueActivity(issue_id, "reflog", count)
            for issue_id, count in sorted(per_issue.items())
        ]

        unattributed_files = 0
        unattributed_branch: Optional[str] = None
        if not per_issue:
            modified = self._scan_working_tree(target)
            if modified:
                branch = self._current_branch()
                branch_match = _BRANCH_ISSUE_RE.match(branch) if branch else None
                if branch_match:
                    issues.append(
                        IssueActivity(int(branch_match.group(1)), "branch", len(modified))
                    )
                else:
                    unattributed_files = len(modified)
                    unattributed_branch = branch

        planning_counts, review_counts = self._scan_planning_tree(target)
        issues.extend(
            IssueActivity(issue_id, "planning", count)
            for issue_id, count in sorted(planning_counts.items())
        )
        issues.extend(
            IssueActivity(mr_id, "review", count) for mr_id, count in sorted(review_counts.items())
        )

        return ActivityReport(
            target, repo, issues, unattributed_events, unattributed_files, unattributed_branch
        )
