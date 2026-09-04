"""CI pipeline trigger — create a pipeline for a branch and optionally await it."""

import logging
import time
from typing import Dict, List, Optional, Sequence, Tuple

from ..exceptions import PlatformError
from ..utils.git_helpers import get_current_branch
from ..utils.glab_runner import run_glab_json

logger = logging.getLogger(__name__)

# GitLab pipeline statuses that will not change without another event. Anything
# else ("created", "pending", "running", "waiting_for_resource", …) is still in
# flight and --wait keeps polling.
_TERMINAL_STATUSES = frozenset(
    {"success", "failed", "canceled", "cancelled", "skipped", "manual"}
)

# A pipeline that finished only because every job was allowed to fail still
# reports success; --wait reports the status GitLab gives and leaves the
# judgement to the caller.
_WAIT_POLL_DELAY_S = 15.0
_WAIT_MAX_POLLS = 240  # 240 x 15s = 1 hour


class CiRunHandler:
    """Creates GitLab pipelines for a branch via the glab CLI."""

    def __init__(self, dry_run: bool = False) -> None:
        """Initialize the handler.

        Args:
            dry_run: When True, report the pipeline that would be created
                without creating it.
        """
        self.dry_run = dry_run

    @staticmethod
    def current_branch() -> str:
        """Return the checked-out branch name.

        Returns:
            Branch name.

        Raises:
            PlatformError: If the branch cannot be determined (detached HEAD,
                or not a git repository).
        """
        branch = get_current_branch()
        if not branch or branch == "HEAD":
            raise PlatformError(
                "HEAD is detached; pass --branch to say which ref to run a pipeline for"
            )
        return branch

    @staticmethod
    def _parse_variables(pairs: Sequence[str]) -> List[Tuple[str, str]]:
        """Parse KEY=VALUE strings into pairs.

        Args:
            pairs: Raw ``KEY=VALUE`` strings.

        Returns:
            List of (key, value) tuples.

        Raises:
            ValueError: If an entry has no ``=`` or an empty key. A silently
                dropped variable would produce a pipeline that looks right and
                behaves differently.
        """
        parsed: List[Tuple[str, str]] = []
        for raw in pairs:
            key, sep, value = raw.partition("=")
            if not sep or not key:
                raise ValueError(f"Invalid --variable {raw!r}; expected KEY=VALUE")
            parsed.append((key, value))
        return parsed

    def trigger(self, ref: str, variables: Sequence[str] = ()) -> Optional[Dict]:
        """Create a pipeline for a ref.

        Args:
            ref: Branch or tag to run the pipeline against.
            variables: ``KEY=VALUE`` pipeline variables.

        Returns:
            The created pipeline dict, or None under dry_run.

        Raises:
            PlatformError: If the API call fails or returns an unexpected shape.
            ValueError: If a variable is malformed.
        """
        parsed = self._parse_variables(variables)

        cmd: List[str] = ["api", "-X", "POST", f"projects/:fullpath/pipeline?ref={ref}"]
        for index, (key, value) in enumerate(parsed):
            # GitLab takes pipeline variables as an indexed array of objects.
            cmd += ["-f", f"variables[{index}][key]={key}"]
            cmd += ["-f", f"variables[{index}][value]={value}"]

        if self.dry_run:
            shown = ", ".join(f"{k}={v}" for k, v in parsed) or "none"
            print(f"[dry-run] Would create a pipeline on {ref} (variables: {shown})")
            return None

        pipeline = run_glab_json(cmd)
        if not isinstance(pipeline, dict) or not pipeline.get("id"):
            raise PlatformError(f"Unexpected pipeline response for {ref}: {pipeline!r}")

        print(
            f"✓ Created pipeline #{pipeline['id']} on {ref} "
            f"({pipeline.get('status', '?')})"
        )
        if pipeline.get("web_url"):
            print(f"  {pipeline['web_url']}")
        return pipeline

    def _fetch(self, pipeline_id: int) -> Dict:
        """Fetch a pipeline's current state.

        Args:
            pipeline_id: Pipeline id.

        Returns:
            Pipeline dict.

        Raises:
            PlatformError: If the API call fails or returns an unexpected shape.
        """
        data = run_glab_json(["api", f"projects/:fullpath/pipelines/{pipeline_id}"])
        if not isinstance(data, dict):
            raise PlatformError(f"Unexpected response for pipeline {pipeline_id}: {data!r}")
        return data

    def wait(self, pipeline_id: int) -> str:
        """Poll a pipeline until it reaches a terminal status.

        Args:
            pipeline_id: Pipeline id.

        Returns:
            The terminal status, or the last status seen if the poll budget ran
            out.

        Raises:
            PlatformError: If a poll fails.
        """
        status = "unknown"
        for _ in range(_WAIT_MAX_POLLS):
            pipeline = self._fetch(pipeline_id)
            status = str(pipeline.get("status", "unknown"))
            if status in _TERMINAL_STATUSES:
                return status
            time.sleep(_WAIT_POLL_DELAY_S)
        return status


def cmd_ci_run(args) -> int:
    """Handle the 'ci run' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code: 0 when the pipeline was created (and succeeded, with
        --wait), 1 when it was created but did not succeed, 2 when it could
        not be created. Scripts branch on this, so "could not run" must never
        read as "ran and failed".
    """
    handler = CiRunHandler(dry_run=args.dry_run)
    try:
        ref = args.branch or handler.current_branch()
        pipeline = handler.trigger(ref, args.variable)
    except ValueError as err:
        logger.error("Error: %s", err)
        return 2
    except PlatformError as err:
        logger.error("Could not create the pipeline: %s", err)
        return 2

    if pipeline is None:  # dry run
        return 0
    if not args.wait:
        return 0

    try:
        status = handler.wait(pipeline["id"])
    except PlatformError as err:
        logger.error("Lost track of pipeline #%s: %s", pipeline["id"], err)
        return 2

    print(f"Pipeline #{pipeline['id']} finished: {status}")
    return 0 if status == "success" else 1
