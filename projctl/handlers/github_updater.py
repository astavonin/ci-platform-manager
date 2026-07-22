"""GitHub issue/PR updater — bare-minimum implementation."""

import json
import logging
from typing import List, Optional

from ..config import Config
from ..utils.gh_runner import run_gh_command
from ..utils.validation import validate_labels

logger = logging.getLogger(__name__)


class GithubUpdater:
    """Updates GitHub issues and pull requests using the gh CLI.

    Bare-minimum scope: state transitions (close/reopen), title, labels,
    assignee, milestone.  Reviewer and target-branch are PR-only fields.
    Epic and due-date are not supported on GitHub.
    """

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        """Initialize the updater.

        Args:
            config: Configuration object with defaults.
            dry_run: If True, print intent without executing any gh calls.
        """
        self.config = config
        self.repo = config.get_github_repo()
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def update_issue(
        self,
        ref: str,
        *,
        state: Optional[str] = None,
        title: Optional[str] = None,
        labels_add: Optional[List[str]] = None,
        labels_remove: Optional[List[str]] = None,
        assignee: Optional[str] = None,
        milestone: Optional[str] = None,
    ) -> None:
        """Update a GitHub issue.

        Args:
            ref: Issue reference — plain number, or with '#' prefix.
            state: 'close' or 'reopen'.
            title: New title.
            labels_add: Labels to add.
            labels_remove: Labels to remove.
            assignee: GitHub username to assign.
            milestone: Milestone title to set.

        Raises:
            PlatformError: If any gh command fails.
            ValueError: If ref cannot be parsed.
        """
        number = self._parse_number(ref, prefix="#")
        self._validate_labels_add(labels_add)
        self._apply_update(
            "issue",
            number,
            state=state,
            title=title,
            labels_add=labels_add,
            labels_remove=labels_remove,
            assignee=assignee,
            milestone=milestone,
        )

    def update_pr(
        self,
        ref: str,
        *,
        state: Optional[str] = None,
        title: Optional[str] = None,
        labels_add: Optional[List[str]] = None,
        labels_remove: Optional[List[str]] = None,
        assignee: Optional[str] = None,
        reviewer: Optional[str] = None,
        milestone: Optional[str] = None,
    ) -> None:
        """Update a GitHub pull request.

        Args:
            ref: PR reference — plain number, or with '!' or '#' prefix.
            state: 'close' or 'reopen'.
            title: New title.
            labels_add: Labels to add.
            labels_remove: Labels to remove.
            assignee: GitHub username to assign.
            reviewer: GitHub username to request review from.
            milestone: Milestone title to set.

        Raises:
            PlatformError: If any gh command fails.
            ValueError: If ref cannot be parsed.
        """
        number = self._parse_number(ref, prefix="!")
        self._validate_labels_add(labels_add)
        self._apply_update(
            "pr",
            number,
            state=state,
            title=title,
            labels_add=labels_add,
            labels_remove=labels_remove,
            assignee=assignee,
            reviewer=reviewer,
            milestone=milestone,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_labels_add(self, labels_add: Optional[List[str]]) -> None:
        """Raise ValueError if any label in labels_add is not in the allowed list."""
        if labels_add:
            validate_labels(list(labels_add), self.config.get_allowed_labels())

    @staticmethod
    def _parse_number(ref: str, prefix: str) -> str:
        """Strip a leading prefix character and return the bare number string.

        Args:
            ref: Reference such as '#42', '!42', or '42'.
            prefix: Expected prefix character ('#' or '!').

        Returns:
            Bare number string, e.g. '42'.

        Raises:
            ValueError: If the result is not numeric.
        """
        stripped = ref.lstrip(prefix).lstrip("#")
        if not stripped.isdigit():
            raise ValueError(f"Cannot parse reference as a number: {ref!r}")
        return stripped

    def _current_labels(self, resource: str, number: str) -> List[str]:
        """Fetch the current labels for an issue or PR.

        Args:
            resource: 'issue' or 'pr'.
            number: Bare issue/PR number string.

        Returns:
            List of label name strings.
        """
        output = run_gh_command([resource, "view", number, "--json", "labels"])
        data = json.loads(output) if output else {}
        raw = data.get("labels", [])
        return [lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in raw]

    def _apply_update(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        resource: str,
        number: str,
        *,
        state: Optional[str],
        title: Optional[str],
        labels_add: Optional[List[str]],
        labels_remove: Optional[List[str]],
        assignee: Optional[str] = None,
        reviewer: Optional[str] = None,
        milestone: Optional[str] = None,
    ) -> None:
        """Build and execute gh <resource> edit / close / reopen commands.

        Args:
            resource: 'issue' or 'pr'.
            number: Bare number string.
            state: 'close' or 'reopen', or None.
            title: New title, or None.
            labels_add: Labels to add.
            labels_remove: Labels to remove.
            assignee: Username to assign, or None.
            reviewer: Username to request review from (PR only), or None.
            milestone: Milestone title to set, or None.
        """
        # ---- state transition ----
        if state == "close":
            self._run(resource, ["close", number])
        elif state == "reopen":
            self._run(resource, ["reopen", number])

        # ---- edit fields ----
        edit_cmd: List[str] = ["edit", number]

        if title:
            edit_cmd.extend(["--title", title])

        if labels_add or labels_remove:
            current = self._current_labels(resource, number) if not self.dry_run else []
            merged = list(
                dict.fromkeys(
                    [lbl for lbl in current if lbl not in (labels_remove or [])]
                    + (labels_add or [])
                )
            )
            for lbl in merged:
                edit_cmd.extend(["--add-label", lbl])
            for lbl in labels_remove or []:
                edit_cmd.extend(["--remove-label", lbl])

        if assignee:
            edit_cmd.extend(["--assignee", assignee])

        if reviewer and resource == "pr":
            edit_cmd.extend(["--reviewer", reviewer])

        if milestone:
            edit_cmd.extend(["--milestone", milestone])

        if len(edit_cmd) > 2:  # more than just ["edit", number]
            self._run(resource, edit_cmd)

    def _run(self, resource: str, cmd: List[str]) -> None:
        """Execute a gh command or print it in dry-run mode.

        Args:
            resource: 'issue' or 'pr'.
            cmd: Subcommand tokens (without the leading 'gh <resource>').
        """
        full = [resource] + cmd
        if self.dry_run:
            print(f"[DRY RUN] Would execute: gh {' '.join(full)}")
            return
        run_gh_command(full)
        logger.info("[github] Updated %s #%s", resource, cmd[1] if len(cmd) > 1 else "?")
