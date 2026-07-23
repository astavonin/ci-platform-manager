"""Planning folder sync handler."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..exceptions import PlatformError

logger = logging.getLogger(__name__)


def _assert_exclude_shape(patterns: tuple[str, ...]) -> None:
    """Assert that all rsync exclude patterns are basename-style.

    Basename-style means no '/' (which would make rsync treat it as an
    anchored path pattern) and no '**' (which is not valid rsync syntax).
    Both would cause _rsync_itemize to produce different results than what
    callers expect from the oracle contract.

    Args:
        patterns: Tuple of rsync exclude patterns to validate.

    Raises:
        AssertionError: If any pattern contains '/' or '**'.
    """
    assert all("/" not in p and "**" not in p for p in patterns), (
        "RSYNC_EXCLUDES patterns must be rsync basename-style (no '/', no '**'). "
        "Adding anchored patterns requires upgrading the status command."
    )


RSYNC_EXCLUDES: tuple[str, ...] = ("*.swp", "*~", ".DS_Store", ".workflow-safety.log", "memory")
_assert_exclude_shape(RSYNC_EXCLUDES)

# Memory sync uses all the same excludes except "memory" itself — when syncing
# the memory directory we want its contents, not an exclusion of the directory.
_MEMORY_RSYNC_EXCLUDES: tuple[str, ...] = tuple(p for p in RSYNC_EXCLUDES if p != "memory")
_assert_exclude_shape(_MEMORY_RSYNC_EXCLUDES)


def _encode_repo_path(repo_root: Path) -> str:
    """Encode a repo root path to the Claude project directory name.

    Claude Code names its per-project directory by replacing every '/' in the
    absolute path with '-', including the leading '/'.

    Example: /home/alice/projects/projctl → -home-alice-projects-projctl
    """
    return str(repo_root).replace("/", "-")


def _claude_projects_root() -> Path:
    """Return the Claude projects root directory.

    Isolated as a function so tests can monkeypatch it.
    """
    return Path.home() / ".claude" / "projects"


def _claude_memory_path(repo_root: Path) -> Path:
    """Return the Claude project memory directory for the given repo root.

    The directory may not exist if no memory has been written yet.
    """
    return _claude_projects_root() / _encode_repo_path(repo_root) / "memory"


@dataclass(frozen=True)
class ItemizeEntry:
    """A single parsed line from rsync --itemize-changes output.

    Attributes:
        op: Operation code: '<', '>', '.', 'c', 'h', or '*deleting'.
        kind: File kind: 'f', 'd', 'L', 'D', 'S', 'X', or '' for *deleting.
        attrs: 9-character attribute block, or '' for *deleting lines.
        path: File path relative to the rsync source root.
    """

    op: str
    kind: str
    attrs: str
    path: str


# Regex for the *deleting itemize shape.
# rsync emits exactly three spaces between "*deleting" and the path.
_DELETING_RE = re.compile(r"^\*deleting\s{3}(?P<path>.+)$")

# Regex for the standard flag-block itemize shape.
# rsync emits exactly one space between the 11-character flag block and the
# filename. Using a literal single space (not \s+) preserves leading-space
# filenames: ">f+++++++++ leading.md" has flag-block + ONE space + " leading.md".
_FLAGBLOCK_RE = re.compile(r"^(?P<op>[<>.ch])(?P<kind>[fdLDSX])(?P<attrs>\S{9}) (?P<path>.+)$")

# Compiled pattern for the "sent N bytes" summary line (anchored to avoid
# swallowing arbitrary lines that start with "sent ").
_SENT_BYTES_RE = re.compile(r"^sent \d+")

# Banner prefixes/strings that should be discarded without error.
_BANNER_PREFIXES = (
    "sending incremental file list",
    "receiving incremental file list",
    "total size is ",
    "(DRY RUN)",
)


def _parse_itemize_line(line: str) -> ItemizeEntry | None:
    """Parse a single line from rsync --itemize-changes stdout.

    Args:
        line: A raw line from rsync output (trailing newline is stripped).

    Returns:
        An ItemizeEntry if the line contains transfer/deletion info,
        or None for blank lines and known banner lines.

    Raises:
        PlatformError: If the line is non-blank, not a banner, and does not
            match either expected rsync output shape. This fail-loud policy
            surfaces rsync format changes before they corrupt classification.
    """
    line = line.rstrip("\r\n")
    if not line:
        return None

    # Check anchored "sent N bytes" banner before the generic prefix list.
    if _SENT_BYTES_RE.match(line):
        return None

    for prefix in _BANNER_PREFIXES:
        if line.startswith(prefix):
            return None

    m = _DELETING_RE.match(line)
    if m:
        return ItemizeEntry(op="*deleting", kind="", attrs="", path=m.group("path"))

    m = _FLAGBLOCK_RE.match(line)
    if m:
        return ItemizeEntry(
            op=m.group("op"),
            kind=m.group("kind"),
            attrs=m.group("attrs"),
            path=m.group("path"),
        )

    raise PlatformError(f"Unrecognized rsync itemize line (format may have changed): {line!r}")


@dataclass
class _SyncPaths:
    """Grouping of all path attributes used by PlanningSyncHandler."""

    planning_path: Path
    gdrive_base: Path
    gdrive_planning_base: Path
    gdrive_repo_path: Path
    memory_path: Path | None = None  # None when the local memory dir does not exist
    # Sentinel ensures bare _SyncPaths() construction never accidentally matches an existing path.
    gdrive_memory_path: Path = field(
        default_factory=lambda: Path("/nonexistent/__unset_gdrive_memory__")
    )


class PlanningSyncHandler:
    """Handles synchronization of planning folders to/from Google Drive.

    Auto-detects current repository name and planning folder location.
    Uses rsync for efficient file synchronization.
    """

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        """Initialize the sync handler.

        Args:
            config: Configuration object with planning_sync settings.
            dry_run: If True, only preview operations without executing them.

        Raises:
            PlatformError: If not in a git repository or planning folder doesn't exist.
        """
        self.config = config
        self.dry_run = dry_run

        # Auto-detect repository and planning folder
        self.repo_name = self._detect_repo_name()
        self.repo_root = self._get_repo_root()

        # Get Google Drive base from config
        planning_sync = getattr(config, "planning_sync", {})
        gdrive_base = planning_sync.get("gdrive_base")

        if not gdrive_base:
            raise PlatformError(
                "Google Drive path not configured.\n\n"
                "Add to your config file:\n"
                "planning_sync:\n"
                "  gdrive_base: ~/GoogleDrive"
            )

        gdrive_base_path = Path(os.path.expanduser(gdrive_base))
        gdrive_planning_base = gdrive_base_path / "backup" / "planning"
        memory_src = _claude_memory_path(self.repo_root)
        self.paths = _SyncPaths(
            planning_path=self._get_planning_path(),
            gdrive_base=gdrive_base_path,
            gdrive_planning_base=gdrive_planning_base,
            gdrive_repo_path=gdrive_planning_base / self.repo_name,
            memory_path=memory_src if memory_src.exists() else None,
            gdrive_memory_path=gdrive_base_path / "backup" / "claude-memory" / _encode_repo_path(self.repo_root),
        )

    @property
    def planning_path(self) -> Path:
        """Local planning folder path."""
        return self.paths.planning_path

    @property
    def gdrive_base(self) -> Path:
        """Google Drive base path."""
        return self.paths.gdrive_base

    @property
    def gdrive_planning_base(self) -> Path:
        """Google Drive planning backup base path."""
        return self.paths.gdrive_planning_base

    @property
    def gdrive_repo_path(self) -> Path:
        """Google Drive path for this repository's planning folder."""
        return self.paths.gdrive_repo_path

    @property
    def memory_path(self) -> Path | None:
        """Local Claude memory directory path, or None if it doesn't exist."""
        return self.paths.memory_path

    @property
    def gdrive_memory_path(self) -> Path:
        """GDrive path for this repository's Claude memory backup."""
        return self.paths.gdrive_memory_path

    def _get_repo_root(self) -> Path:
        """Get the git repository root directory.

        Returns:
            Path to repository root.

        Raises:
            PlatformError: If not in a git repository.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
            )
            return Path(result.stdout.strip())
        except subprocess.CalledProcessError as err:
            raise PlatformError("Not in a git repository. Planning sync requires git.") from err

    def _detect_repo_name(self) -> str:
        """Detect current git repository name.

        Uses the repository directory name as the identifier.

        Returns:
            Repository name.

        Raises:
            PlatformError: If not in a git repository.
        """
        repo_root = self._get_repo_root()
        return repo_root.name

    def _get_planning_path(self) -> Path:
        """Get planning folder path (./planning/ from repo root).

        Does not require the folder to exist: `pull()` bootstraps a fresh
        checkout by creating it, so existence is enforced by the operations
        that actually need local content (`push()`, `status()`), not here.

        Returns:
            Path to planning folder.

        Raises:
            PlatformError: If the path exists but is not a directory.
        """
        planning_path = self.repo_root / "planning"

        if planning_path.exists() and not planning_path.is_dir():
            raise PlatformError(f"Planning path exists but is not a directory: {planning_path}")

        return planning_path

    def _verify_rsync_available(self) -> None:
        """Verify rsync command is available.

        Raises:
            PlatformError: If rsync is not installed.
        """
        try:
            subprocess.run(["rsync", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as err:
            raise PlatformError(
                "rsync is not installed or not available in PATH.\n\n"
                "Install rsync:\n"
                "  Ubuntu/Debian: sudo apt install rsync\n"
                "  macOS: brew install rsync (or use built-in version)"
            ) from err

    # pylint: disable=too-many-positional-arguments
    # Each parameter maps to a distinct rsync option; a config object would
    # obscure the call sites and make the safety distinction (delete=False on
    # memory pull) harder to review.
    def _run_rsync(
        self,
        source: Path,
        target: Path,
        description: str,
        excludes: tuple[str, ...] = RSYNC_EXCLUDES,
        delete: bool = True,
    ) -> None:
        """Run rsync to synchronize directories.

        Args:
            source: Source directory path.
            target: Target directory path.
            description: Human-readable description of the operation.
            excludes: Rsync exclude patterns to apply. Defaults to RSYNC_EXCLUDES.
            delete: If True, pass --delete to rsync (last-write-wins). Set to
                False for memory pull so that local-only memory files are not
                destroyed — memory has no git backup and --delete would be
                unrecoverable data loss.

        Raises:
            PlatformError: If rsync fails.
        """
        # Ensure source and target end with trailing slash for rsync
        source_str = f"{source}/"
        target_str = f"{target}/"

        # Build rsync command using the provided exclude set for oracle parity
        rsync_cmd = ["rsync", "-av"]
        if delete:
            rsync_cmd.append("--delete")
        for pat in excludes:
            rsync_cmd.append(f"--exclude={pat}")

        if self.dry_run:
            rsync_cmd.append("--dry-run")

        rsync_cmd.extend([source_str, target_str])

        logger.debug("Executing rsync: %s", " ".join(rsync_cmd))

        if self.dry_run:
            print(f"\n[DRY RUN] {description}")
            print(f"  Source: {source}")
            print(f"  Target: {target}")
            print(f"  Command: {' '.join(rsync_cmd)}\n")

        try:
            result = subprocess.run(rsync_cmd, capture_output=True, text=True, check=True)

            if self.dry_run:
                print("Preview of changes:")
                print(result.stdout)
            else:
                logger.debug("rsync output: %s", result.stdout)
                logger.info("Synced: %s", description)

        except subprocess.CalledProcessError as err:
            raise PlatformError(
                f"rsync failed: {err.stderr}\n\n" f"Command: {' '.join(rsync_cmd)}"
            ) from err

    def push(self) -> None:
        """Push local planning folder to Google Drive.

        Syncs ./planning/ to the Google Drive backup location.

        Raises:
            PlatformError: If sync fails or the local planning folder doesn't exist.
        """
        self._verify_rsync_available()

        if not self.planning_path.exists():
            raise PlatformError(
                f"Planning folder not found: {self.planning_path}\n\n"
                f"Expected planning folder in repository root."
            )

        # Verify Google Drive base exists
        if not self.gdrive_base.exists():
            raise PlatformError(
                f"Google Drive not found: {self.gdrive_base}\n\n"
                f"Verify Google Drive is mounted and path is correct in config."
            )

        # Create target directory if it doesn't exist
        if not self.dry_run:
            self.gdrive_planning_base.mkdir(parents=True, exist_ok=True)

        description = f"Push {self.repo_name} planning to Google Drive"
        self._run_rsync(
            source=self.planning_path, target=self.gdrive_repo_path, description=description
        )

        if not self.dry_run:
            print(f"✓ Pushed {self.repo_name} planning to Google Drive")
            print(f"  Local:  {self.planning_path}")
            print(f"  Remote: {self.gdrive_repo_path}")

        # Sync Claude project memory (private, excluded from git)
        if self.memory_path is not None:
            if not self.dry_run:
                self.gdrive_memory_path.parent.mkdir(parents=True, exist_ok=True)
            self._run_rsync(
                source=self.memory_path,
                target=self.gdrive_memory_path,
                description=f"Push {self.repo_name} memory to Google Drive",
                excludes=_MEMORY_RSYNC_EXCLUDES,
            )
            if not self.dry_run:
                print(f"✓ Pushed {self.repo_name} memory to Google Drive")
                print(f"  Local:  {self.memory_path}")
                print(f"  Remote: {self.gdrive_memory_path}")
        else:
            logger.debug("No local memory directory found, skipping memory sync")

    def pull(self) -> None:
        """Pull planning folder from Google Drive to local.

        Syncs the Google Drive backup to ./planning/

        Raises:
            PlatformError: If sync fails or Google Drive folder doesn't exist.
        """
        self._verify_rsync_available()

        # Verify Google Drive folder exists
        if not self.gdrive_repo_path.exists():
            if self.gdrive_planning_base.exists():
                available = "\n".join(
                    f"  - {p.name}" for p in self.gdrive_planning_base.iterdir() if p.is_dir()
                )
                error_msg = (
                    f"Planning folder not found in Google Drive: {self.gdrive_repo_path}\n\n"
                    f"Available repositories in Google Drive:\n{available}"
                )
            else:
                error_msg = (
                    f"Planning folder not found in Google Drive: {self.gdrive_repo_path}\n\n"
                    f"Google Drive planning backup folder doesn't exist: "
                    f"{self.gdrive_planning_base}"
                )
            raise PlatformError(error_msg)

        # Create local planning folder if it doesn't exist
        if not self.dry_run and not self.planning_path.exists():
            self.planning_path.mkdir(parents=True, exist_ok=True)

        description = f"Pull {self.repo_name} planning from Google Drive"
        self._run_rsync(
            source=self.gdrive_repo_path, target=self.planning_path, description=description
        )

        if not self.dry_run:
            print(f"✓ Pulled {self.repo_name} planning from Google Drive")
            print(f"  Remote: {self.gdrive_repo_path}")
            print(f"  Local:  {self.planning_path}")

        # Pull Claude project memory (if present on GDrive)
        if self.gdrive_memory_path.exists():
            # Compute the local memory path fresh — self.memory_path may be None
            # if the directory didn't exist when the handler was initialised.
            local_memory = _claude_memory_path(self.repo_root)
            if not self.dry_run:
                local_memory.mkdir(parents=True, exist_ok=True)
            self._run_rsync(
                source=self.gdrive_memory_path,
                target=local_memory,
                description=f"Pull {self.repo_name} memory from Google Drive",
                excludes=_MEMORY_RSYNC_EXCLUDES,
                # Preserve local-only memory files — memory has no git backup and
                # --delete would silently destroy files that exist only on this machine.
                delete=False,
            )
            if not self.dry_run:
                print(f"✓ Pulled {self.repo_name} memory from Google Drive")
                print(f"  Remote: {self.gdrive_memory_path}")
                print(f"  Local:  {local_memory}")
        else:
            logger.debug("No memory backup found on Google Drive, skipping memory pull")

    def _rsync_itemize(
        self,
        source: Path,
        target: Path,
        excludes: tuple[str, ...] = RSYNC_EXCLUDES,
        delete: bool = True,
    ) -> list[ItemizeEntry]:
        """Run rsync in dry-run itemize mode and return parsed entries.

        This is a read-only helper. It always passes -n to rsync so no files
        are ever written. It is the sole subprocess entry point on the status()
        code path.

        Args:
            source: Source directory.
            target: Target directory.
            excludes: Rsync exclude patterns to apply. Defaults to RSYNC_EXCLUDES.
            delete: If True, pass --delete to rsync so the oracle reflects what
                the corresponding mutating operation would delete. Set to False
                for the memory pull direction, which mirrors pull()'s additive
                behavior (delete=False) so the oracle does not report pull_deletes
                that pull() would never actually perform.

        Returns:
            List of parsed ItemizeEntry objects from rsync output.

        Raises:
            PlatformError: On any rsync error or unrecognised output line.
        """
        source_str = f"{source}/"
        target_str = f"{target}/"

        cmd = ["rsync", "-avn", "--itemize-changes"]
        if delete:
            cmd.append("--delete")
        for pat in excludes:
            cmd.append(f"--exclude={pat}")
        cmd.extend([source_str, target_str])

        logger.debug("Executing itemize rsync: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C"},
                check=False,
            )
        except (FileNotFoundError, OSError) as err:
            # rsync binary not found or OS-level failure (fd exhaustion, etc.)
            raise PlatformError(
                f"rsync is not installed or not available in PATH.\n\n"
                f"Install rsync:\n"
                f"  Ubuntu/Debian: sudo apt install rsync\n"
                f"  macOS: brew install rsync (or use built-in version)\n"
                f"Error: {err}"
            ) from err

        rc = result.returncode
        stderr = result.stderr.strip()

        if rc == 0:
            if stderr:
                raise PlatformError(
                    f"rsync exited 0 but wrote to stderr (itemize listing may be incomplete):\n"
                    f"{stderr}"
                )
            entries: list[ItemizeEntry] = []
            for line in result.stdout.splitlines():
                entry = _parse_itemize_line(line)
                if entry is not None:
                    entries.append(entry)
            return entries

        if rc == 23:
            raise PlatformError(
                f"rsync reported partial transfer due to error (exit 23).\n"
                f"The itemize listing is incomplete and cannot be trusted.\n"
                f"stderr: {stderr}"
            )

        if rc == 24:
            raise PlatformError(
                f"rsync reported that some source files vanished during the scan (exit 24).\n"
                f"Retry once your filesystem edits have settled.\n"
                f"stderr: {stderr}"
            )

        raise PlatformError(f"rsync failed with exit code {rc}.\n" f"stderr: {stderr}")

    def _print_memory_status(self) -> None:
        """Print memory sync status below the planning report (non-fatal)."""
        local_exists = self.memory_path is not None
        remote_exists = self.gdrive_memory_path.exists()

        if not local_exists and not remote_exists:
            print("\nMemory: not present locally or on Google Drive — nothing to sync")
            return

        if local_exists and not remote_exists:
            print("\nMemory STATUS: local-ahead")
            print(f"  Local:  {self.memory_path}")
            print("  Run 'projctl sync push' to create the GDrive backup.")
            return

        if not local_exists and remote_exists:
            print("\nMemory STATUS: remote-ahead")
            print(f"  Remote: {self.gdrive_memory_path}")
            print("  Run 'projctl sync pull' to restore locally.")
            return

        # Both sides exist — compute real diff
        try:
            local_mem = self.memory_path
            assert local_mem is not None  # guaranteed by local_exists guard above
            push_entries = self._rsync_itemize(
                local_mem, self.gdrive_memory_path,
                excludes=_MEMORY_RSYNC_EXCLUDES,
                # delete=True (default): correctly shows what push would delete on remote
            )
            pull_entries = self._rsync_itemize(
                self.gdrive_memory_path, local_mem,
                excludes=_MEMORY_RSYNC_EXCLUDES,
                delete=False,  # mirrors pull() behavior: additive, never deletes local files
            )
            mem_class = self._classify_drift(push_entries, pull_entries)
        except PlatformError as exc:
            print(f"\nMemory: status check failed — {exc}")
            return

        print(f"\nMemory STATUS: {mem_class.state}")
        if mem_class.state != "in-sync":
            if mem_class.push_transfers:
                print("  Local changes to push:")
                for p in mem_class.push_transfers:
                    print(f"    {p}")
            if mem_class.pull_transfers:
                print("  Remote changes to pull:")
                for p in mem_class.pull_transfers:
                    print(f"    {p}")

    def _classify_drift(
        self,
        push_entries: list[ItemizeEntry],
        pull_entries: list[ItemizeEntry],
    ) -> "DriftClassification":
        """Classify drift state from two rsync itemize entry lists.

        Args:
            push_entries: Entries from the local-to-remote dry-run invocation.
            pull_entries: Entries from the remote-to-local dry-run invocation.

        Returns:
            A DriftClassification with state, transfer/delete path lists, and
            mtime_only_count (the number of *unique* paths across both directions
            that differ only in timestamp or permissions).
        """
        push_transfers: list[str] = []
        push_deletes: list[str] = []
        pull_transfers: list[str] = []
        pull_deletes: list[str] = []
        # Collect unique paths flagged as mtime/perms-only across both directions.
        mtime_only_paths: set[str] = set()

        for entry in push_entries:
            if entry.op in ("<", ">") and entry.kind == "f":
                push_transfers.append(entry.path)
                if _is_timestamp_or_perms_only(entry.attrs):
                    mtime_only_paths.add(entry.path)
            elif entry.op == "*deleting":
                push_deletes.append(entry.path)

        for entry in pull_entries:
            if entry.op in ("<", ">") and entry.kind == "f":
                pull_transfers.append(entry.path)
                if _is_timestamp_or_perms_only(entry.attrs):
                    mtime_only_paths.add(entry.path)
            elif entry.op == "*deleting":
                pull_deletes.append(entry.path)

        push_transfers = sorted(push_transfers)
        push_deletes = sorted(push_deletes)
        pull_transfers = sorted(pull_transfers)
        pull_deletes = sorted(pull_deletes)

        if push_transfers and pull_transfers:
            state = "diverged"
        elif push_transfers:
            state = "local-ahead"
        elif pull_transfers:
            state = "remote-ahead"
        else:
            state = "in-sync"

        return DriftClassification(
            state=state,
            push_transfers=push_transfers,
            push_deletes=push_deletes,
            pull_transfers=pull_transfers,
            pull_deletes=pull_deletes,
            mtime_only_count=len(mtime_only_paths),
        )

    def _format_status_report(self, classification: "DriftClassification") -> str:
        """Format the full human-readable status report string.

        This is a pure function: given a DriftClassification it produces the
        exact string that status() will print. It never writes to any file or
        stream itself.

        Args:
            classification: Drift classification produced by _classify_drift.

        Returns:
            The complete multi-line report string to be printed.
        """
        state = classification.state
        push_transfers = classification.push_transfers
        push_deletes = classification.push_deletes
        pull_transfers = classification.pull_transfers
        pull_deletes = classification.pull_deletes
        mtime_only_count = classification.mtime_only_count

        lines: list[str] = []
        lines.append(f"STATUS: {state}")
        lines.append("")
        lines.append(f"Local:  {self.planning_path}")
        lines.append(f"Remote: {self.gdrive_repo_path}")
        lines.append("")

        n_push = len(push_transfers)
        n_pull = len(pull_transfers)
        n_push_del = len(push_deletes)
        n_pull_del = len(pull_deletes)

        lines.append(
            f"Summary: {n_push} file(s) would be pushed, {n_pull} file(s) would be pulled."
        )
        if push_deletes or pull_deletes:
            lines.append(
                f"         {n_push_del} remote file(s) would be deleted,"
                f" {n_pull_del} local file(s) would be deleted."
            )

        # Build a set of paths that appear in both directions for annotation.
        both_sides = set(push_transfers) & set(pull_transfers)

        if push_transfers:
            lines.append("")
            lines.append("Local changes to push (new/modified content):")
            for path in push_transfers:
                annotation = " [also differs on the other side]" if path in both_sides else ""
                lines.append(f"  {path}{annotation}")

        if push_deletes:
            lines.append("")
            lines.append("Remote files a push would DELETE (present only on remote):")
            for path in push_deletes:
                lines.append(f"  {path}")

        if pull_transfers:
            lines.append("")
            lines.append("Remote changes to pull (new/modified content):")
            for path in pull_transfers:
                annotation = " [also differs on the other side]" if path in both_sides else ""
                lines.append(f"  {path}{annotation}")

        if pull_deletes:
            lines.append("")
            lines.append("Local files a pull would DELETE (present only on local):")
            for path in pull_deletes:
                lines.append(f"  {path}")

        if mtime_only_count > 0:
            lines.append("")
            lines.append(
                f"Note: {mtime_only_count} file(s) differ only in timestamp or permissions"
                f" — content may be identical."
            )
            lines.append(
                "Run 'sync push' or 'sync pull' if you are confident one side is canonical."
            )

        lines.append("")
        lines.append("Safe next step:")
        lines.append(f"  {_safe_next_step(state, n_push, n_push_del, n_pull, n_pull_del)}")

        return "\n".join(lines)

    def status(self) -> None:
        """Classify drift and print STATUS + human report to stdout.

        Contract: read-only. Makes no mutating filesystem call on
        planning_path or gdrive_repo_path. Raises PlatformError on
        genuine errors; returns None on any drift state so cmd_sync
        exits 0.

        Raises:
            PlatformError: On rsync errors, missing gdrive_base, or
                unrecognised rsync output.
        """
        self._verify_rsync_available()

        if not self.gdrive_base.exists():
            raise PlatformError(
                f"Google Drive not found: {self.gdrive_base}\n\n"
                f"Verify Google Drive is mounted and path is correct in config."
            )

        with contextlib.ExitStack() as stack:
            if self.planning_path.exists():
                local = self.planning_path
            else:
                # Fresh checkout with no local planning/ yet: treat it as empty
                # so a missing folder reads as "remote-ahead" rather than an
                # rsync error — mirrors the missing-remote branch below.
                local = Path(stack.enter_context(tempfile.TemporaryDirectory()))

            if self.gdrive_repo_path.exists():
                remote = self.gdrive_repo_path
            else:
                remote = Path(stack.enter_context(tempfile.TemporaryDirectory()))

            push_entries = self._rsync_itemize(local, remote)
            pull_entries = self._rsync_itemize(remote, local)

        classification = self._classify_drift(push_entries, pull_entries)
        print(self._format_status_report(classification))
        sys.stdout.flush()
        self._print_memory_status()


@dataclass(frozen=True)
class DriftClassification:
    """Result of classifying the drift between local and remote planning folders.

    Attributes:
        state: One of 'in-sync', 'local-ahead', 'remote-ahead', 'diverged'.
        push_transfers: Sorted list of file paths rsync would transfer on push.
        push_deletes: Sorted list of file paths rsync would delete on remote.
        pull_transfers: Sorted list of file paths rsync would transfer on pull.
        pull_deletes: Sorted list of file paths rsync would delete locally.
        mtime_only_count: Count of *unique* file paths across both directions
            that differ only in timestamp or permissions (not content).
    """

    state: str
    push_transfers: list[str] = field(default_factory=list)
    push_deletes: list[str] = field(default_factory=list)
    pull_transfers: list[str] = field(default_factory=list)
    pull_deletes: list[str] = field(default_factory=list)
    mtime_only_count: int = 0


def _is_timestamp_or_perms_only(attrs: str) -> bool:
    """Return True when attrs indicate a timestamp- or permissions-only change.

    The 9-character attribute block maps to rsync's cstpoguax layout:
      index 0: c — checksum
      index 1: s — size
      index 2: t — mtime (timestamp)
      index 3: p — permissions
      index 4: o — owner
      index 5: g — group
      index 6: u — acl (reserved slot in older rsync)
      index 7: a — acl
      index 8: x — extended attributes

    A file qualifies when ALL of the following hold:
    - attrs[0] is not 'c'/'C': checksum unchanged (no content change detected).
    - attrs[1] is not 's'/'S': size unchanged (content almost certainly identical).
    - attrs[2] is 't'/'T' OR attrs[3] is 'p'/'P': mtime or permissions changed.
    - attrs[4:9] are all in '.' or '+': owner, group, acl, xattr unchanged.
      Any metadata change beyond mtime/perms means we cannot say "content may be
      identical" with confidence.
    - attrs does not start with '+': new files are never mtime/perms-only.

    This is a conservative heuristic: prefer false negatives to false positives.

    Args:
        attrs: The 9-character attribute block from an ItemizeEntry.

    Returns:
        True only when the entry is safely identified as timestamp/perms-only.
    """
    if len(attrs) != 9:
        return False
    # New files ('+'), checksum change ('c'/'C'), or size change ('s'/'S') all
    # indicate content changed — none qualify as timestamp/perms-only.
    if attrs[0] in ("+", "c", "C") or attrs[1] in ("s", "S"):
        return False
    # At least one of mtime (index 2) or permissions (index 3) must differ.
    if attrs[2] not in ("t", "T") and attrs[3] not in ("p", "P"):
        return False
    # Owner, group, acl, xattr must all be unchanged (positions 4–8).
    # Any change there means more than just mtime/perms differ.
    for ch in attrs[4:]:
        if ch not in (".", "+"):
            return False
    return True


def _safe_next_step(
    state: str,
    n_push: int,
    n_push_del: int,
    n_pull: int,
    n_pull_del: int,
) -> str:
    """Return a one-line guidance string for the given drift state.

    Args:
        state: Drift state string.
        n_push: Number of files that would be transferred on push.
        n_push_del: Number of remote files that would be deleted on push.
        n_pull: Number of files that would be transferred on pull.
        n_pull_del: Number of local files that would be deleted on pull.

    Returns:
        A single-line guidance sentence.
    """
    if state == "in-sync":
        return "No sync needed."
    if state == "local-ahead":
        return (
            f"Run 'projctl sync push' (would transfer {n_push} file(s),"
            f" delete {n_push_del} file(s) on remote)."
            f" A pull would DELETE {n_push} local file(s)"
            f" and re-create {n_push_del} file(s) locally."
        )
    if state == "remote-ahead":
        return (
            f"Run 'projctl sync pull' (would transfer {n_pull} file(s),"
            f" delete {n_pull_del} file(s) locally)."
            f" A push would DELETE {n_pull} remote file(s)"
            f" and re-create {n_pull_del} file(s) on remote."
        )
    # diverged
    return (
        "Manual reconciliation required."
        " Neither push nor pull is safe; one side's work would be lost."
    )
