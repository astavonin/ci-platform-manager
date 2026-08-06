# pylint: disable=too-many-lines
# The module is intentionally large — it is the single CLI wiring point for
# all subcommands. Splitting it would reduce locality without a clear benefit.
"""Command-line interface for CI Platform Manager."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install PyYAML")
    sys.exit(1)

from .config import Config, config_search_paths
from .exceptions import PlatformError
from .handlers.artifacts_handler import ArtifactsHandler
from .handlers.comment import cmd_comment
from .handlers.labels import LabelsHandler
from .handlers.note import NoteHandler
from .handlers.creator import EpicIssueCreator
from .handlers.github_creator import GithubIssueCreator
from .handlers.github_loader import GithubLoader
from .handlers.github_mr_handler import cmd_create_pr
from .handlers.github_search import GithubSearchHandler
from .handlers.github_updater import GithubUpdater
from .handlers.loader import TicketLoader
from .handlers.mr_handler import cmd_create_mr
from .handlers.pipeline_handler import PipelineHandler
from .handlers.search import SearchHandler
from .handlers.sync import PlanningSyncHandler
from .handlers.updater import TicketUpdater
from .handlers.wiki import WikiHandler

logger = logging.getLogger(__name__)


def cmd_create(args) -> int:
    """Handle the 'create' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    if not args.yaml_file.exists():
        logger.error("YAML file not found: %s", args.yaml_file)
        return 1

    try:
        # Load configuration
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)

        if config.platform == "github":
            github_creator = GithubIssueCreator(config=config, dry_run=args.dry_run)
            github_creator.process_yaml_file(args.yaml_file)
        else:
            creator = EpicIssueCreator(config=config, dry_run=args.dry_run)
            creator.process_yaml_file(args.yaml_file)
            creator.print_summary()
        return 0
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError, yaml.YAMLError) as err:
        logger.error("Error: %s", err)
        return 1


def _dispatch_github_load(loader: GithubLoader, resource_type: str, reference: str) -> None:
    """Dispatch a GitHub load call based on resource type.

    Args:
        loader: The GithubLoader instance.
        resource_type: One of "mr", "milestone", "issue".
        reference: The resource reference string.
    """
    if resource_type == "mr":
        loader.load_pr(reference)
    elif resource_type == "milestone":
        loader.load_milestone(reference)
    else:
        loader.load_issue(reference)


def _dispatch_load(
    loader: TicketLoader, resource_type: str, reference: str, comments: bool = False
) -> None:
    """Dispatch a load + print call based on resource type.

    Args:
        loader: The TicketLoader instance.
        resource_type: One of "mr", "epic", "milestone", "issue".
        reference: The resource reference string.
        comments: When True and resource_type is "mr", also fetch and print review comments.
    """
    if resource_type == "mr":
        if comments:
            data = loader.load_mr_comments(reference)
        else:
            data = loader.load_mr(reference)
        loader.print_mr_info(data, with_comments=comments)
    elif resource_type == "epic":
        loader.print_epic_info(loader.load_epic_with_issues(reference))
    elif resource_type == "milestone":
        loader.print_milestone_info(loader.load_milestone_with_issues(reference))
    else:
        loader.print_ticket_info(loader.load_ticket_with_epic(reference))


def cmd_load(args) -> int:
    """Handle the 'load' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)

        if config.platform == "github":
            gh_loader = GithubLoader(config=config)
            _dispatch_github_load(gh_loader, args.resource_type, args.reference)
        else:
            loader = TicketLoader(config=config)
            _dispatch_load(
                loader,
                args.resource_type,
                args.reference,
                comments=getattr(args, "comments", False),
            )
        return 0
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError, json.JSONDecodeError) as err:
        logger.error("Error: %s", err)
        return 1


def _validate_label_for_gitlab_search(search_type: str, labels: list | None) -> str | None:
    """Return an error message if the label filter cannot be applied, or None when valid.

    Args:
        search_type: The search resource type (issues, epics, milestones).
        labels: The list of label names, or None when not provided.

    Returns:
        An error message string if validation fails, otherwise None.
    """
    if not labels:
        return None
    if search_type == "milestones":
        return "--label filter is not supported for milestones"
    return None


def _dispatch_gitlab_search(searcher: SearchHandler, args) -> int:
    """Dispatch a GitLab search call based on resource type.

    Args:
        searcher: The SearchHandler instance.
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    labels = None
    if getattr(args, "label", None):
        labels = [lbl for lbl in args.label if lbl.strip()]
        if not labels:
            print("Error: --label requires a non-empty label name", file=sys.stderr)
            return 1

    label_error = _validate_label_for_gitlab_search(args.type, labels)
    if label_error:
        print(f"Error: {label_error}", file=sys.stderr)
        return 1

    if args.type == "issues":
        searcher.search_issues(query=args.query, state=args.state, limit=args.limit, labels=labels)
    elif args.type == "epics":
        searcher.search_epics(query=args.query, state=args.state, limit=args.limit, labels=labels)
    elif args.type == "milestones":
        searcher.search_milestones(query=args.query, state=args.state, limit=args.limit)
    else:
        logger.error("Unknown search type: %s", args.type)
        return 1

    return 0


def _dispatch_github_search(gh_searcher: GithubSearchHandler, args) -> int:
    """Dispatch a GitHub search call based on resource type.

    Args:
        gh_searcher: The GithubSearchHandler instance.
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    if getattr(args, "label", None):
        print("Error: --label filter is not supported for GitHub (GitLab only)", file=sys.stderr)
        return 1
    if args.type == "issues":
        if args.state == "active":
            print(
                "Error: --state active is not supported for GitHub (milestone-only state)",
                file=sys.stderr,
            )
            return 1
        # GitLab uses "opened"; the gh CLI expects "open"
        gh_state = "open" if args.state == "opened" else args.state
        gh_searcher.search_issues(query=args.query, state=gh_state)
    elif args.type == "milestones":
        gh_searcher.search_milestones(query=args.query)
    else:
        logger.error("Search type '%s' is not supported on GitHub", args.type)
        return 1
    return 0


def cmd_search(args) -> int:
    """Handle the 'search' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)

        if config.platform == "github":
            return _dispatch_github_search(GithubSearchHandler(config=config), args)

        return _dispatch_gitlab_search(SearchHandler(config=config), args)

    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError, json.JSONDecodeError) as err:
        logger.error("Error: %s", err)
        return 1


def cmd_sync(args) -> int:
    """Handle the 'sync' subcommand - sync planning folder with Google Drive.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        # Load configuration
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)

        # Use getattr so the status sub-subparser (which has no --dry-run flag)
        # does not cause an AttributeError here.
        handler = PlanningSyncHandler(config, dry_run=getattr(args, "dry_run", False))

        if args.sync_command == "push":
            handler.push()
        elif args.sync_command == "pull":
            handler.pull()
        elif args.sync_command == "status":
            handler.status()
        else:
            logger.error("Unknown sync command: %s", args.sync_command)
            return 1

        return 0
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


def _print_job_with_logs(job: dict, logs: str) -> None:
    """Print header and pre-fetched logs for a single pipeline job.

    Args:
        job: Job data dictionary from the pipeline API.
        logs: Pre-fetched job log content.
    """
    job_name = job.get("name")
    job_stage = job.get("stage")
    job_status = job.get("status")
    job_duration = job.get("duration") or 0
    print(f"### Job: {job_name}\n")
    print(f"- **Stage:** {job_stage}")
    print(f"- **Status:** {job_status}")
    print(f"- **Duration:** {job_duration:.1f}s\n")
    print("**Logs:**\n```")
    print(logs)
    print("```\n")


def _print_job_logs(handler: PipelineHandler, job: dict) -> None:
    """Print header and logs for a single failed pipeline job.

    Args:
        handler: PipelineHandler used to fetch job logs.
        job: Job data dictionary from the pipeline API.
    """
    job_id = job.get("id")
    job_name = job.get("name")
    job_stage = job.get("stage")
    job_status = job.get("status")
    job_duration = job.get("duration") or 0

    print(f"### Job: {job_name}\n")
    print(f"- **Stage:** {job_stage}")
    print(f"- **Status:** {job_status}")
    print(f"- **Duration:** {job_duration:.1f}s\n")
    print("**Logs:**\n```")

    if not isinstance(job_id, int):
        logger.warning("Invalid job ID for job %s: %s", job_name, job_id)
        print("(Job ID unavailable)")
    else:
        try:
            print(handler.get_job_logs(job_id))
        except PlatformError as err:
            logger.warning("Failed to fetch logs for job %s: %s", job_name, err)
            print(f"Error fetching logs: {err}")

    print("```\n")


def _cmd_pipeline_debug_by_job_id(args) -> int:
    """Fetch and print logs for a single job given by --job-id.

    Args:
        args: Parsed command-line arguments (requires args.config, args.job_id).

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)
        handler = PipelineHandler(config)
        job = handler.get_job_info(args.job_id)
        logs = handler.get_job_logs(args.job_id)
        print("\n# Job Debug Results\n")
        print(f"**Job ID:** {args.job_id}\n")
        _print_job_with_logs(job, logs)
        return 0
    except (FileNotFoundError, PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


def cmd_pipeline_debug(args) -> int:
    """Handle the 'pipeline-debug' subcommand - debug failed pipeline jobs.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    if args.job_id:
        return _cmd_pipeline_debug_by_job_id(args)

    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)
        handler = PipelineHandler(config)

        branch = args.branch if args.branch else handler.get_current_branch()
        logger.info("Debugging pipeline for branch: %s", branch)

        pipeline = handler.get_current_pipeline(branch)
        pipeline_id = pipeline.get("id")
        pipeline_status = pipeline.get("status")
        pipeline_url = pipeline.get("web_url")

        if not isinstance(pipeline_id, int):
            logger.error("Invalid pipeline ID: %s", pipeline_id)
            return 1

        failed_jobs = handler.get_failed_jobs(pipeline_id)

        if not failed_jobs:
            print(f"\n✓ No failed jobs in pipeline #{pipeline_id}")
            print(f"Pipeline status: {pipeline_status}")
            print(f"URL: {pipeline_url}\n")
            return 0

        print("\n# Pipeline Debug Results\n")
        print(f"**Branch:** {branch}")
        print(f"**Pipeline:** #{pipeline_id} - {pipeline_status}")
        print(f"**URL:** {pipeline_url}\n")
        print(f"## Failed Jobs ({len(failed_jobs)})\n")

        for job in failed_jobs:
            _print_job_logs(handler, job)

        return 0

    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


# pylint: disable=too-many-return-statements,too-many-branches
# cmd_update validates resource-type-specific flags and at least one update
# field before delegating, making multiple early returns and branches necessary.
def _cmd_update_github(args, config) -> int:
    """Handle the 'update' subcommand for GitHub platform.

    Supports issue and pr resource types with state, title, label, assignee,
    reviewer, and milestone fields.  Epic and milestone resource types are not
    supported on GitHub.

    Args:
        args: Parsed command-line arguments.
        config: Loaded Config object with platform == 'github'.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    resource_type = args.update_type
    ref = args.reference

    if resource_type in ("epic", "milestone"):
        logger.error("'%s' resource type is not supported on GitHub", resource_type)
        return 1

    if args.state == "activate":
        logger.error("--state activate is only valid for GitLab milestones")
        return 1

    updater = GithubUpdater(config=config, dry_run=args.dry_run)

    try:
        if resource_type == "issue":
            updater.update_issue(
                ref,
                state=args.state,
                title=args.title,
                labels_add=args.add_label,
                labels_remove=args.remove_label,
                assignee=args.assignee,
                milestone=args.milestone,
            )
        elif resource_type in ("mr", "pr"):
            updater.update_pr(
                ref,
                state=args.state,
                title=args.title,
                labels_add=args.add_label,
                labels_remove=args.remove_label,
                assignee=args.assignee,
                reviewer=args.reviewer,
                milestone=args.milestone,
            )
        else:
            logger.error("Unknown resource type: %s", resource_type)
            return 1

        logger.info("✓ Updated %s %s", resource_type, ref)
        return 0

    except (PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


# cmd_update is a linear dispatcher: per-flag validation, per-resource-type
# field checks, and per-resource-type handler dispatch. Splitting it would
# fragment the argparse contract without simplifying anything.
def cmd_update(args) -> int:  # pylint: disable=too-many-statements
    """Handle the 'update' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)

        if config.platform == "github":
            return _cmd_update_github(args, config)

        updater = TicketUpdater(config=config, dry_run=args.dry_run)

        resource_type = args.update_type
        ref = args.reference

        # --- L1: Validate state values per resource type ---
        if args.state == "activate" and resource_type != "milestone":
            logger.error("--state activate is only valid for milestone resources")
            return 1
        if args.state == "reopen" and resource_type == "milestone":
            logger.error("--state reopen is not valid for milestones; use 'activate' instead")
            return 1

        # --- L2: Reject type-specific flags used on the wrong resource type ---
        if resource_type != "mr" and (args.reviewer or args.target_branch):
            logger.error("--reviewer and --target-branch are only valid for MR resources")
            return 1
        if resource_type not in ("milestone", "issue") and args.due_date:
            logger.error("--due-date is only valid for issue and milestone resources")
            return 1
        if resource_type in ("epic", "milestone") and args.assignee:
            logger.error("--assignee is not valid for %s resources", resource_type)
            return 1
        if resource_type == "milestone" and args.milestone:
            logger.error("--milestone is not valid for milestone resources")
            return 1
        if resource_type != "issue" and args.epic:
            logger.error("--epic is only valid for issue resources")
            return 1
        if resource_type != "issue" and getattr(args, "weight", None) is not None:
            logger.error("--weight is only valid for issue resources")
            return 1
        if resource_type != "issue" and (
            getattr(args, "add_blocker", None) or getattr(args, "remove_blocker", None)
        ):
            logger.error("--add-blocker and --remove-blocker are only valid for issue resources")
            return 1

        # --- M3: Require at least one field to update ---
        if resource_type == "issue":
            has_update = any(
                [
                    args.title,
                    args.description,
                    args.add_label,
                    args.remove_label,
                    args.assignee,
                    args.milestone,
                    args.state,
                    args.epic,
                    args.due_date,
                    getattr(args, "weight", None) is not None,
                    getattr(args, "add_blocker", None),
                    getattr(args, "remove_blocker", None),
                ]
            )
        elif resource_type == "mr":
            has_update = any(
                [
                    args.title,
                    args.description,
                    args.add_label,
                    args.remove_label,
                    args.assignee,
                    args.reviewer,
                    args.milestone,
                    args.target_branch,
                    args.state,
                ]
            )
        elif resource_type == "epic":
            has_update = any(
                [
                    args.title,
                    args.description,
                    args.add_label,
                    args.remove_label,
                    args.state,
                    args.milestone,
                ]
            )
        else:  # milestone
            has_update = any(
                [
                    args.title,
                    args.description,
                    args.due_date,
                    args.state,
                ]
            )

        if not has_update:
            logger.error("No fields to update — specify at least one option.")
            return 1

        if resource_type == "issue":
            add_blocker = getattr(args, "add_blocker", None)
            remove_blocker = getattr(args, "remove_blocker", None)
            # Only call update_issue when there are non-link fields to update.
            non_link_update = any(
                [
                    args.title,
                    args.description,
                    args.add_label,
                    args.remove_label,
                    args.assignee,
                    args.milestone,
                    args.state,
                    args.epic,
                    args.due_date,
                    getattr(args, "weight", None) is not None,
                ]
            )
            if non_link_update:
                updater.update_issue(
                    issue_ref=ref,
                    title=args.title,
                    description=args.description,
                    # L4: pass None directly when no labels given, not an empty list
                    labels_add=args.add_label,
                    labels_remove=args.remove_label,
                    assignee=args.assignee,
                    milestone=args.milestone,
                    state_event=args.state,
                    epic=args.epic,
                    weight=getattr(args, "weight", None),
                    due_date=args.due_date,
                )
            if remove_blocker:
                updater.remove_issue_link(ref, remove_blocker)
            if add_blocker:
                updater.add_issue_link(ref, add_blocker, link_type="is_blocked_by")
        elif resource_type == "mr":
            updater.update_mr(
                mr_ref=ref,
                title=args.title,
                description=args.description,
                # L4: pass None directly when no labels given, not an empty list
                labels_add=args.add_label,
                labels_remove=args.remove_label,
                assignee=args.assignee,
                reviewer=args.reviewer,
                milestone=args.milestone,
                target_branch=args.target_branch,
                state_event=args.state,
            )
        elif resource_type == "epic":
            updater.update_epic(
                epic_ref=ref,
                title=args.title,
                description=args.description,
                # L4: pass None directly when no labels given, not an empty list
                labels_add=args.add_label,
                labels_remove=args.remove_label,
                state_event=args.state,
                milestone=args.milestone,
            )
        elif resource_type == "milestone":
            updater.update_milestone(
                milestone_ref=ref,
                title=args.title,
                description=args.description,
                due_date=args.due_date,
                state_event=args.state,
            )
        else:
            logger.error("Unknown update type: %s", resource_type)
            return 1

        return 0

    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


def cmd_create_milestone(args) -> int:
    """Handle the 'create-milestone' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)

        creator = EpicIssueCreator(config=config, dry_run=args.dry_run)
        result = creator.create_milestone(
            title=args.title,
            description=args.description or "",
            due_date=args.due_date or "",
        )

        print(f"Created milestone %{result['iid']}: {args.title}")
        print(f"URL: {result['web_url']}")
        return 0
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


def _add_create_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'create' subcommand."""
    p = subparsers.add_parser(
        "create",
        help="Create milestone, epic, and/or issues from YAML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
YAML format:
  milestone:  # optional
    title: "My Milestone"
    description: "..."
    due_date: "2026-12-31"

  epic:
    title: "My Epic Title"
    description: "Epic description"
    # OR use existing epic:
    # id: 123

  issues:
    - title: "Issue 1"
      description: "Description"
      labels:
        - "bug"
        - "priority::high"
      assignee: "username"
      milestone: "v1.0"
      due_date: "2025-01-15"
        """,
    )
    p.add_argument(
        "yaml_file", type=Path, help="Path to YAML file containing epic and issue definitions"
    )
    p.add_argument("--dry-run", action="store_true", help="Preview commands without executing them")


def _add_load_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'load' subcommand."""
    p = subparsers.add_parser(
        "load",
        help="Load ticket (issue), epic, or milestone information from GitLab",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  load issue 113
  load issue #113
  load epic &21
  load milestone %%123
  load mr !134
  load issue https://gitlab.com/group/project/-/issues/113
        """,
    )
    p.add_argument(
        "resource_type",
        choices=["issue", "epic", "milestone", "mr"],
        help="Type of resource to load",
    )
    p.add_argument(
        "reference",
        type=str,
        help=(
            "Resource reference: number, URL, #number (issue), "
            "&number (epic), or %%number (milestone)"
        ),
    )
    p.add_argument(
        "--comments",
        action="store_true",
        default=False,
        help="Also load and display review comments (MR only)",
    )


def _add_search_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'search' subcommand."""
    p = subparsers.add_parser(
        "search",
        help="Search for issues, epics, or milestones by text",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "type", choices=["issues", "epics", "milestones"], help="Type of resource to search"
    )
    p.add_argument(
        "query",
        type=str,
        nargs="?",
        default="",
        help="Search query text (searches title and description). Optional when --label is used.",
    )
    p.add_argument(
        "--state",
        choices=["opened", "closed", "active", "all"],
        default="all",
        help='Filter by state (default: all). Use "active" for milestones.',
    )
    p.add_argument("--limit", type=int, default=20, help="Maximum number of results (default: 20)")
    p.add_argument(
        "--label",
        action="append",
        dest="label",
        metavar="LABEL",
        help="Filter by label (can be repeated for multiple labels; issues and epics only)",
    )


def _add_comment_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'comment' subcommand."""
    p = subparsers.add_parser(
        "comment",
        help="Post review comment from YAML file to merge request",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "review_file",
        type=str,
        help="Path to review YAML file (e.g., planning/reviews/MR134-review.yaml)",
    )
    p.add_argument("--mr", dest="mr_number", type=int, help="MR number (overrides value from YAML)")
    p.add_argument("--dry-run", action="store_true", help="Preview comment without posting")


def _add_create_mr_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'create-mr' subcommand."""
    p = subparsers.add_parser(
        "create-mr",
        help="Create merge request from current branch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--title", type=str, help="MR title")
    p.add_argument("--description", type=str, help="MR description")
    p.add_argument("--draft", action="store_true", help="Mark MR as draft")
    p.add_argument("--assignee", action="append", help="Assignee username (can be repeated)")
    p.add_argument("--reviewer", action="append", help="Reviewer username (can be repeated)")
    p.add_argument("--label", action="append", help="Label to add (can be repeated)")
    p.add_argument("--milestone", type=str, help="Milestone title")
    p.add_argument("--target-branch", type=str, help="Target branch (default: default branch)")
    p.add_argument("--fill", action="store_true", help="Fill in title and description from commits")
    p.add_argument("--web", action="store_true", help="Open MR in web browser after creation")
    p.add_argument("--dry-run", action="store_true", help="Preview command without creating MR")


def _add_sync_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'sync' subcommand with push/pull/status sub-subcommands."""
    p = subparsers.add_parser(
        "sync",
        help="Sync planning folder for current repository with Google Drive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="sync_command", required=True)
    push_p = sub.add_parser("push", help="Push local planning folder to Google Drive")
    push_p.add_argument("--dry-run", action="store_true", help="Preview sync without executing")
    pull_p = sub.add_parser("pull", help="Pull planning folder from Google Drive to local")
    pull_p.add_argument("--dry-run", action="store_true", help="Preview sync without executing")
    sub.add_parser(
        "status",
        help="Report drift (in-sync | local-ahead | remote-ahead | diverged) — read-only",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Classify drift between ./planning/ and the Google Drive backup without "
            "modifying either side."
        ),
        epilog=(
            "States:\n"
            "  in-sync       local and remote contain identical content\n"
            "  local-ahead   local has changes; safe to run 'projctl sync push'\n"
            "  remote-ahead  remote has changes; safe to run 'projctl sync pull'\n"
            "  diverged      both sides changed; manual reconciliation required\n"
            "\n"
            "Exit codes:\n"
            "  0    classification succeeded (any drift state)\n"
            "  1    genuine error (not in a git repo, rsync missing, Google Drive\n"
            "       unmounted, partial rsync failure, etc.)\n"
            "\n"
            "Drift oracle: what 'sync push'/'sync pull' would transfer or delete,\n"
            "using rsync's default size+mtime comparison (no content checksum).\n"
            "Files with identical content but different timestamps are reported\n"
            "as drift; see the note emitted in the detail section when that occurs."
        ),
    )


def _add_update_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'update' subcommand."""
    p = subparsers.add_parser(
        "update",
        help="Update an existing issue, MR, epic, or milestone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  update issue 231 --title "New title"
  update issue 376 --add-blocker 385
  update issue 376 --remove-blocker 252
  update mr 144 --state close
  update epic 37 --add-label "epic::active"
  update milestone 10 --due-date 2026-04-01
        """,
    )
    p.add_argument(
        "update_type",
        choices=["issue", "mr", "epic", "milestone"],
        help="Type of resource to update",
    )
    p.add_argument(
        "reference", type=str, help="Resource reference (number, URL, or prefixed format)"
    )
    p.add_argument("--title", type=str, help="New title")
    p.add_argument("--description", type=str, help="New description")
    p.add_argument(
        "--add-label", action="append", metavar="LABEL", help="Label to add (can be repeated)"
    )
    p.add_argument(
        "--remove-label", action="append", metavar="LABEL", help="Label to remove (can be repeated)"
    )
    p.add_argument("--assignee", type=str, help="Assignee username (issue and MR only)")
    p.add_argument("--reviewer", type=str, help="Reviewer username (MR only)")
    p.add_argument("--milestone", type=str, help="Milestone title or iid (issue, MR, and epic)")
    p.add_argument("--target-branch", type=str, help="Target branch (MR only)")
    p.add_argument(
        "--due-date", type=str, metavar="YYYY-MM-DD", help="Due date (issue and milestone only)"
    )
    p.add_argument(
        "--state",
        choices=["close", "reopen", "activate"],
        help="State event: close or reopen (issue/MR/epic); activate (milestone)",
    )
    p.add_argument("--epic", type=str, help="Assign issue to epic (e.g. &47) — issue only")
    p.add_argument(
        "--weight", type=int, metavar="N", help="Story-point weight in hours (issue only)"
    )
    p.add_argument(
        "--add-blocker",
        type=str,
        metavar="ISSUE",
        help="Add 'blocked by' link to ISSUE (issue only, e.g. 252 or #252)",
    )
    p.add_argument(
        "--remove-blocker",
        type=str,
        metavar="ISSUE",
        help="Remove 'blocked by' link to ISSUE (issue only, e.g. 252 or #252)",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview changes without executing")


def _add_wiki_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'wiki' subcommand with list/load/create/update sub-subcommands."""
    p = subparsers.add_parser(
        "wiki",
        help="Manage GitLab project wiki pages",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  wiki list
  wiki load my-page-slug
  wiki create "My Page" --content page.md
  wiki update my-page-slug --content updated.md --dry-run
        """,
    )
    sub = p.add_subparsers(dest="wiki_command", required=True)

    # list: no positional args
    sub.add_parser("list", help="List all wiki pages (slug + title)")

    # load: positional slug
    load_p = sub.add_parser("load", help="Load and print a wiki page by slug")
    load_p.add_argument("slug", type=str, help="Wiki page slug")

    # create: positional title, required --content, optional --dry-run
    create_p = sub.add_parser("create", help="Create a new wiki page")
    create_p.add_argument("title", type=str, help="Page title")
    create_p.add_argument(
        "--content",
        metavar="FILE",
        required=True,
        help="Path to Markdown file with page content",
    )
    create_p.add_argument("--dry-run", action="store_true", help="Preview without making API calls")

    # update: positional slug, required --content, optional --dry-run
    update_p = sub.add_parser("update", help="Update an existing wiki page")
    update_p.add_argument("slug", type=str, help="Wiki page slug to update")
    update_p.add_argument(
        "--content",
        metavar="FILE",
        required=True,
        help="Path to Markdown file with new page content",
    )
    update_p.add_argument("--dry-run", action="store_true", help="Preview without making API calls")


def cmd_wiki(args) -> int:
    """Handle the 'wiki' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        handler = WikiHandler()

        if args.wiki_command == "list":
            handler.list_pages()
        elif args.wiki_command == "load":
            handler.load_page(args.slug)
        elif args.wiki_command == "create":
            content_path = Path(args.content)
            if not content_path.exists():
                logger.error("Content file not found: %s", content_path)
                return 1
            content = content_path.read_text(encoding="utf-8")
            handler.create_page(title=args.title, content=content, dry_run=args.dry_run)
        elif args.wiki_command == "update":
            content_path = Path(args.content)
            if not content_path.exists():
                logger.error("Content file not found: %s", content_path)
                return 1
            content = content_path.read_text(encoding="utf-8")
            handler.update_page(
                slug=args.slug,
                content=content,
                dry_run=args.dry_run,
            )
        else:
            logger.error("Unknown wiki command: %s", args.wiki_command)
            return 1

        return 0
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


def _add_note_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'note' subcommand."""
    p = subparsers.add_parser(
        "note",
        help="Post a note (comment) to a GitLab issue, MR, or epic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  note issue 340 --body "Closing as false-positive; passes after clean rebuild."
  note mr !194 --body "LGTM"
  note epic &64 --body "Superseded by new approach."
  note issue #340 --body "See also #341" --dry-run
        """,
    )
    p.add_argument(
        "resource_type",
        choices=["issue", "mr", "epic"],
        help="Type of resource to comment on",
    )
    p.add_argument(
        "reference",
        type=str,
        help="Resource reference (number, #number/!number/&number, or URL)",
    )
    p.add_argument("--body", type=str, required=True, help="Note body text")
    p.add_argument("--dry-run", action="store_true", help="Preview without posting")


def cmd_note(args) -> int:
    """Handle the 'note' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)

        if config.platform != "gitlab":
            logger.error("Error: 'note' command is only supported for GitLab")
            return 1

        handler = NoteHandler(config, dry_run=args.dry_run)

        if args.resource_type == "issue":
            handler.add_issue_note(args.reference, args.body)
        elif args.resource_type == "mr":
            handler.add_mr_note(args.reference, args.body)
        else:
            handler.add_epic_note(args.reference, args.body)

        return 0
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


def _add_labels_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'labels' subcommand."""
    subparsers.add_parser(
        "labels",
        help="Display configured labels from the project config",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def cmd_labels(args) -> int:
    """Handle the 'labels' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)
        LabelsHandler(config).print_labels()
        return 0
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError) as err:
        logger.error("Error: %s", err)
        return 1


def cmd_artifacts(args) -> int:
    """Handle the 'artifacts' subcommand — download GitLab CI job artifacts.

    With --path, fetches that single file out of the job's archive. Without it,
    downloads the whole archive and extracts it, since a caller who does not
    know the archive's layout cannot name a path inside it.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)
        handler = ArtifactsHandler(config)
        dest_dir = Path(args.dest) if args.dest else Path.cwd()

        if args.path:
            payload = handler.fetch_artifact_file(args.job_id, args.path)
            # Mirror the archive's own layout under dest_dir rather than
            # flattening to a basename: two artifacts can share a basename
            # across directories, and flattening would silently overwrite.
            out_file = dest_dir / args.path
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(payload)
            print(f"✓ Wrote {len(payload)} bytes to {out_file}")
            return 0

        archive = handler.download_archive(args.job_id, dest_dir)
        members = ArtifactsHandler.extract_archive(archive, dest_dir)
        print(f"✓ Extracted {len(members)} file(s) from job #{args.job_id} into {dest_dir}")
        return 0
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except (PlatformError, ValueError, OSError) as err:
        logger.error("Error: %s", err)
        return 1


def _add_artifacts_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'artifacts' subcommand."""
    p = subparsers.add_parser(
        "artifacts",
        help="Download GitLab CI job artifacts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--job-id", type=int, required=True, help="GitLab CI job ID")
    p.add_argument(
        "--path",
        type=str,
        help="Path of a single file inside the archive; omit to download and extract everything",
    )
    p.add_argument("--dest", type=str, help="Directory to write into (default: current directory)")


def _add_pipeline_debug_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'pipeline-debug' subcommand."""
    p = subparsers.add_parser(
        "pipeline-debug",
        help="Debug failed pipeline jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--branch", type=str, help="Branch name (default: current git branch)")
    p.add_argument(
        "--job-id",
        type=int,
        help="Job ID to fetch logs from directly, bypassing branch/pipeline discovery.",
    )


def cmd_create_mr_dispatch(args) -> int:
    """Handle the 'create-mr' subcommand, dispatching to the correct platform handler.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1

    if config.platform == "github":
        return cmd_create_pr(args, config)
    return cmd_create_mr(args, config)


def _add_config_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the 'config' subcommand."""
    subparsers.add_parser(
        "config",
        help="Show the active config file path and its assembled contents",
        description=(
            "Resolve and display the active projctl configuration.\n\n"
            "WARNING: Prints the full config file contents. "
            "Do not share this output if your config contains secrets."
        ),
    )


def cmd_config(args: argparse.Namespace) -> int:
    """Print the resolved config file path and its assembled contents."""
    try:
        config_path = Path(args.config) if args.config else None
        config = Config(config_path)
    except FileNotFoundError as err:
        logger.error(str(err))
        return 1
    except yaml.YAMLError as err:
        logger.error("Config file contains invalid YAML: %s", err)
        return 1

    print(f"Config file: {config.loaded_config_path}")
    print(f"Platform:    {config.platform}")
    print()
    print("Assembled config:")
    print(yaml.dump(config.config_data, default_flow_style=False, sort_keys=False), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the script.

    Args:
        argv: Argument list for testing. When None, sys.argv is used.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    claude_md_path = (Path(__file__).parent / "CLAUDE.md").resolve()

    _search_summary_lines = ["  1. --config <path>  (explicit override)"]
    _active_config_path: Path | None = None
    for _i, (_path, _label) in enumerate(config_search_paths(), 2):
        if _active_config_path is None and _path.exists():
            _status = "← active"
            _active_config_path = _path.resolve()
        elif _path.exists():
            _status = "(found, shadowed)"
        else:
            _status = "not found"
        _search_summary_lines.append(f"  {_i}. {_label}  {_status}")
    _search_summary = "\n".join(_search_summary_lines)
    _active_line = (
        f"  Active: {_active_config_path}"
        if _active_config_path
        else "  Active: none found (run from a directory with projctl.yaml, or use --config)"
    )

    parser = argparse.ArgumentParser(
        description="GitLab Epic and Issue management tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Config search order (first found wins):
{_search_summary}
{_active_line}

Examples:
  %(prog)s create epic_definition.yaml
  %(prog)s load 113
  %(prog)s load &21
  %(prog)s search issues "streaming"
  %(prog)s search epics --label "Iteration::1"
  %(prog)s comment planning/reviews/MR134-review.yaml
  %(prog)s create-mr --title "Add feature X" --draft
  %(prog)s sync push
  %(prog)s sync status
  %(prog)s update issue 231 --title "New title"
  %(prog)s note issue 340 --body "Closing as false-positive."
  %(prog)s note epic &70 --body "Closed: different approach."
  %(prog)s pipeline-debug
  %(prog)s artifacts --job-id 12345 --path .build-12345/server.stdout
  %(prog)s config

Documentation:
  {claude_md_path}
        """,
    )

    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--config",
        type=str,
        help="Path to config file (overrides automatic search order)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    _add_create_subparser(subparsers)
    _add_load_subparser(subparsers)
    _add_search_subparser(subparsers)
    _add_comment_subparser(subparsers)
    _add_create_mr_subparser(subparsers)
    _add_sync_subparser(subparsers)
    _add_update_subparser(subparsers)
    _add_pipeline_debug_subparser(subparsers)
    _add_artifacts_subparser(subparsers)
    _add_wiki_subparser(subparsers)
    _add_note_subparser(subparsers)
    _add_labels_subparser(subparsers)
    _add_config_subparser(subparsers)

    args = parser.parse_args(argv)

    if args.verbose:
        # Configure root logger so handler-level loggers (e.g. projctl.handlers.sync)
        # also surface debug/info output under --verbose, not just this module's logger.
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "create": cmd_create,
        "load": cmd_load,
        "search": cmd_search,
        "comment": cmd_comment,
        "create-mr": cmd_create_mr_dispatch,
        "sync": cmd_sync,
        "update": cmd_update,
        "pipeline-debug": cmd_pipeline_debug,
        "artifacts": cmd_artifacts,
        "wiki": cmd_wiki,
        "note": cmd_note,
        "labels": cmd_labels,
        "config": cmd_config,
    }

    try:
        cmd_handler = commands.get(args.command)
        if cmd_handler:
            return cmd_handler(args)
        parser.print_help()
        return 1
    except KeyboardInterrupt:
        logger.info("\nOperation cancelled by user")
        return 130
