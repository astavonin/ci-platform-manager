"""GitHub search handler for issues and milestones."""

# pylint: disable=duplicate-code
# Why: print_issues/print_milestones intentionally mirror the GitLab SearchHandler.
# The two handlers use different API field names (number/iid, url/web_url) that
# are platform-specific and cannot be shared without coupling independent adapters.

import json
import logging
from typing import Any, Dict, List

from ..config import Config
from ..exceptions import PlatformError
from ..utils.gh_runner import run_gh_command

logger = logging.getLogger(__name__)


class GithubSearchHandler:
    """Handles search operations for GitHub issues and milestones."""

    def __init__(self, config: Config) -> None:
        """Initialize the search handler.

        Args:
            config: Configuration object with defaults.
        """
        self.config = config
        self.repo = config.get_github_repo()

    def search_issues(
        self, query: str, state: str = "open", limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Search for GitHub issues matching a query.

        Args:
            query: Full-text search string.
            state: Filter by state ("open", "closed", or "all").
            limit: Maximum number of results to return.

        Returns:
            List of issue dictionaries.

        Raises:
            PlatformError: If the gh command fails.
        """
        cmd = [
            "issue",
            "list",
            "--search",
            query,
            "--state",
            state,
            "--json",
            "number,title,state,labels,url",
            "--limit",
            str(limit),
        ]

        try:
            output = run_gh_command(cmd)
            results: List[Dict[str, Any]] = json.loads(output) if output else []
        except PlatformError as err:
            logger.warning("[github] Issue search failed: %s", err)
            results = []

        self.print_issues(results, query)
        return results

    def search_milestones(self, query: str) -> List[Dict[str, Any]]:
        """Search for GitHub milestones whose titles contain the query string.

        GitHub has no server-side milestone search, so all milestones are fetched
        and filtered client-side.

        Args:
            query: Case-insensitive substring to match against milestone titles.

        Returns:
            List of milestone dictionaries matching the query.

        Raises:
            PlatformError: If the API call fails.
        """
        try:
            # Use per_page=100 instead of --paginate: gh api --paginate concatenates
            # JSON arrays across pages producing invalid JSON for json.loads().
            output = run_gh_command(["api", f"repos/{self.repo}/milestones?per_page=100"])
            all_milestones: List[Dict[str, Any]] = json.loads(output) if output else []
        except PlatformError as err:
            logger.warning("[github] Milestone fetch failed: %s", err)
            all_milestones = []

        if len(all_milestones) >= 100:
            logger.warning("[github] Milestone list may be truncated; only first 100 returned")

        query_lower = query.lower()
        results = [ms for ms in all_milestones if query_lower in str(ms.get("title", "")).lower()]

        self.print_milestones(results, query)
        return results

    def print_issues(self, issues: List[Dict[str, Any]], query: str) -> None:
        """Print issue search results in text format.

        Args:
            issues: List of issue dictionaries.
            query: The search query used (for display).
        """
        filter_str = f'"{query}"' if query else "(all)"
        print(f"\n=== ISSUES matching {filter_str} ===\n")

        if not issues:
            print("No issues found")
            return

        for issue in issues:
            number = issue.get("number", "?")
            title = issue.get("title", "Untitled")
            state = issue.get("state", "unknown")
            labels = [
                lb.get("name", lb) if isinstance(lb, dict) else str(lb)
                for lb in issue.get("labels", [])
            ]
            url = issue.get("url", "")

            print(f"#{number} {title}")
            label_str = ", ".join(labels) if labels else "none"
            print(f"    State: {state} | Labels: {label_str}")
            print(f"    URL: {url}\n")

        count = len(issues)
        print(f"Found {count} issue{'s' if count != 1 else ''}")

    def print_milestones(self, milestones: List[Dict[str, Any]], query: str) -> None:
        """Print milestone search results in text format.

        Args:
            milestones: List of milestone dictionaries.
            query: The search query used (for display).
        """
        filter_str = f'"{query}"' if query else "(all)"
        print(f"\n=== MILESTONES matching {filter_str} ===\n")

        if not milestones:
            print("No milestones found")
            return

        for ms in milestones:
            number = ms.get("number", "?")
            title = ms.get("title", "Untitled")
            state = ms.get("state", "unknown")
            url = ms.get("html_url", "")
            due_on = ms.get("due_on", "N/A") or "N/A"

            print(f"%{number} {title}")
            print(f"    State: {state} | Due: {due_on}")
            print(f"    URL: {url}\n")

        count = len(milestones)
        print(f"Found {count} milestone{'s' if count != 1 else ''}")
