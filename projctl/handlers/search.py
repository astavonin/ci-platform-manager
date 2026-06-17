"""Search handler for issues, epics, and milestones."""

import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.glab_runner import run_glab_command

logger = logging.getLogger(__name__)


class SearchHandler:
    """Handles search operations for GitLab issues and epics."""

    def __init__(self, config: Config) -> None:
        """Initialize the search handler.

        Args:
            config: Configuration object with defaults.
        """
        self.config = config

    def _run_glab_command(self, cmd: List[str]) -> str:
        """Delegate to shared glab runner."""
        return run_glab_command(cmd)

    def search_issues(
        self,
        query: str = "",
        state: str = "all",
        limit: int = 20,
        labels: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for issues matching a query and print the results.

        Args:
            query: Search text for title and description. Empty string matches all.
            state: Filter by state ('opened', 'closed', 'all').
            limit: Maximum number of results to return.
            labels: List of label names to filter by. Each is URL-encoded; multiple labels are AND-filtered.

        Returns:
            List of issue dictionaries.

        Raises:
            PlatformError: If search fails.
        """
        params = []
        if query:
            params.append(f"search={urllib.parse.quote(query)}")
        if state != "all":
            params.append(f"state={state}")
        if labels:
            params.append(f"labels={','.join(urllib.parse.quote(lbl, safe='') for lbl in labels)}")
        params.append(f"per_page={limit}")
        api_endpoint = f"projects/:fullpath/issues?{'&'.join(params)}"

        logger.debug("GET %s", api_endpoint)
        output = self._run_glab_command(["api", api_endpoint])

        results: List[Dict[str, Any]] = json.loads(output) if output else []
        self.print_issues(results, query, labels)
        return results

    def search_epics(
        self,
        query: str = "",
        state: str = "all",
        limit: int = 20,
        labels: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for epics matching a query and print the results.

        Args:
            query: Search text for title and description. Empty string matches all.
            state: Filter by state ('opened', 'closed', 'all').
            limit: Maximum number of results to return.
            labels: List of label names to filter by. Each is URL-encoded; multiple labels are AND-filtered.

        Returns:
            List of epic dictionaries.

        Raises:
            PlatformError: If search fails.
            ValueError: If group is not specified.
        """
        group_path = self.config.get_default_group()
        if not group_path:
            raise ValueError(
                "Group path is required for epic search.\n"
                "Please set 'default_group' in your glab_config.yaml file."
            )

        encoded_group = urllib.parse.quote(group_path, safe="")
        params = []
        if query:
            params.append(f"search={urllib.parse.quote(query)}")
        if state != "all":
            params.append(f"state={state}")
        if labels:
            params.append(f"labels={','.join(urllib.parse.quote(lbl, safe='') for lbl in labels)}")
        params.append(f"per_page={limit}")
        api_endpoint = f"groups/{encoded_group}/epics?{'&'.join(params)}"

        logger.debug("GET %s", api_endpoint)
        output = self._run_glab_command(["api", api_endpoint])

        results: List[Dict[str, Any]] = json.loads(output) if output else []
        self.print_epics(results, query, labels)
        return results

    def search_milestones(
        self, query: str = "", state: str = "all", limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search for milestones matching a query and print the results.

        Args:
            query: Search text for title. Empty string matches all milestones.
            state: Filter by state ('active', 'closed', 'all').
            limit: Maximum number of results to return.

        Returns:
            List of milestone dictionaries.

        Raises:
            PlatformError: If search fails.
        """
        group_path = self.config.get_default_group()
        params = []
        if query:
            params.append(f"search={urllib.parse.quote(query)}")
        if state != "all":
            params.append(f"state={state}")
        params.append(f"per_page={limit}")

        if group_path:
            encoded_group = urllib.parse.quote(group_path, safe="")
            api_endpoint = f"groups/{encoded_group}/milestones?{'&'.join(params)}"
        else:
            api_endpoint = f"projects/:fullpath/milestones?{'&'.join(params)}"

        logger.debug("GET %s", api_endpoint)
        output = self._run_glab_command(["api", api_endpoint])

        results: List[Dict[str, Any]] = json.loads(output) if output else []
        self.print_milestones(results, query)
        return results

    def print_issues(
        self, issues: List[Dict[str, Any]], query: str, labels: Optional[List[str]] = None
    ) -> None:
        """Print search results for issues in text format.

        Args:
            issues: List of issue dictionaries.
            query: The search query used.
            labels: Label filter applied, if any.
        """
        parts = []
        if query:
            parts.append(f'"{query}"')
        if labels:
            parts.append(f"label:{', '.join(labels)}")
        filter_str = " + ".join(parts) if parts else "(all)"
        print(f"\n=== ISSUES matching {filter_str} ===\n")

        if not issues:
            print("No issues found")
            return

        for issue in issues:
            iid = issue.get("iid")
            title = issue.get("title", "Untitled")
            state = issue.get("state", "unknown")
            issue_labels = issue.get("labels", [])
            url = issue.get("web_url", "")

            print(f"#{iid} {title}")
            label_str = ", ".join(issue_labels) if issue_labels else "none"
            print(f"    State: {state} | Labels: {label_str}")
            print(f"    URL: {url}\n")

        print(f"Found {len(issues)} issue{'s' if len(issues) != 1 else ''}")

    def print_epics(
        self, epics: List[Dict[str, Any]], query: str, labels: Optional[List[str]] = None
    ) -> None:
        """Print search results for epics in text format.

        Args:
            epics: List of epic dictionaries.
            query: The search query used.
            labels: Label filter applied, if any.
        """
        parts = []
        if query:
            parts.append(f'"{query}"')
        if labels:
            parts.append(f"label:{', '.join(labels)}")
        filter_str = " + ".join(parts) if parts else "(all)"
        print(f"\n=== EPICS matching {filter_str} ===\n")

        if not epics:
            print("No epics found")
            return

        for epic in epics:
            iid = epic.get("iid")
            title = epic.get("title", "Untitled")
            state = epic.get("state", "unknown")
            epic_labels = epic.get("labels", [])
            url = epic.get("web_url", "")

            print(f"&{iid} {title}")
            label_str = ", ".join(epic_labels) if epic_labels else "none"
            print(f"    State: {state} | Labels: {label_str}")
            print(f"    URL: {url}\n")

        print(f"Found {len(epics)} epic{'s' if len(epics) != 1 else ''}")

    def print_milestones(self, milestones: List[Dict[str, Any]], query: str = "") -> None:
        """Print search results for milestones in text format.

        Args:
            milestones: List of milestone dictionaries.
            query: The search query used. Empty string shows (all).
        """
        filter_str = f'"{query}"' if query else "(all)"
        print(f"\n=== MILESTONES matching {filter_str} ===\n")

        if not milestones:
            print("No milestones found")
            return

        for milestone in milestones:
            iid = milestone.get("iid")
            title = milestone.get("title", "Untitled")
            state = milestone.get("state", "unknown")
            url = milestone.get("web_url", "")
            due_date = milestone.get("due_date", "N/A")

            print(f"%{iid} {title}")
            print(f"    State: {state} | Due: {due_date}")
            print(f"    URL: {url}\n")

        print(f"Found {len(milestones)} milestone{'s' if len(milestones) != 1 else ''}")
