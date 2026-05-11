"""Ticket (issue/epic/milestone/MR) loader handler."""

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from ..config import Config
from ..exceptions import PlatformError
from ..formatters import (
    print_epic,
    print_issue,
    print_milestone,
    print_mr,
    print_mr_comments,
)
from ..utils.git_helpers import extract_path_from_url, parse_issue_url
from ..utils.glab_runner import run_glab_command

logger = logging.getLogger(__name__)


class TicketLoader:
    """Loads GitLab issue and epic information using the glab CLI."""

    def __init__(self, config: Config) -> None:
        """Initialize the loader.

        Args:
            config: Configuration object with defaults.
        """
        self.config = config
        self.group = config.get_default_group()

    def _run_glab_command(self, cmd: List[str]) -> str:
        """Delegate to shared glab runner."""
        return run_glab_command(cmd)

    def _parse_issue_reference(self, issue_ref: str) -> tuple:
        """Parse issue reference to extract project path and iid.

        Args:
            issue_ref: Issue reference (number, URL, or #number format).

        Returns:
            Tuple of (project_path, iid). project_path may be None if not in URL.
        """
        project_path, iid = parse_issue_url(issue_ref)
        if iid is not None:
            return (project_path, iid)
        raise ValueError(f"Cannot parse issue reference: {issue_ref}")

    def _parse_epic_reference(self, epic_ref: str) -> tuple:
        """Parse epic reference to extract group path and iid.

        Args:
            epic_ref: Epic reference (number, URL, or &number format).

        Returns:
            Tuple of (group_path, iid). group_path may be None if not in URL.

        Raises:
            ValueError: If epic reference cannot be parsed.
        """
        # URL format: https://gitlab.../groups/mygroup/-/epics/21
        if "/-/epics/" in epic_ref:
            parts = epic_ref.split("/-/epics/")
            if len(parts) == 2:
                group_url = parts[0]
                iid = parts[1].split("/")[0].split("?")[0]

                # Extract group path from URL
                # Format: https://gitlab.example.com/groups/mygroup
                if "/groups/" in group_url:
                    group_path = group_url.split("/groups/")[-1]
                elif "//" in group_url:
                    # Fallback: take everything after the domain
                    group_path = "/".join(group_url.split("//")[1].split("/")[1:])
                else:
                    group_path = group_url

                return (group_path, iid)

        # &21 format (GitLab epic reference)
        if epic_ref.startswith("&"):
            return (None, epic_ref[1:])

        # Plain number
        if epic_ref.isdigit():
            return (None, epic_ref)

        raise ValueError(f"Cannot parse epic reference: {epic_ref}")

    def _parse_milestone_reference(self, milestone_ref: str) -> tuple:
        """Parse milestone reference to extract project/group path, iid, and milestone type.

        Args:
            milestone_ref: Milestone reference (number, URL, or %number format).

        Returns:
            Tuple of (project_or_group_path, iid, is_group_milestone).
            project_or_group_path may be None if not in URL.

        Raises:
            ValueError: If milestone reference cannot be parsed.
        """
        # URL format for group milestone: https://gitlab.../groups/mygroup/-/milestones/123
        if "/groups/" in milestone_ref and "/-/milestones/" in milestone_ref:
            parts = milestone_ref.split("/-/milestones/")
            if len(parts) == 2:
                group_url = parts[0]
                iid = parts[1].split("/")[0].split("?")[0]

                # Extract group path from URL
                # Format: https://gitlab.example.com/groups/mygroup/subgroup
                if "/groups/" in group_url:
                    group_path = group_url.split("/groups/")[-1]
                elif "//" in group_url:
                    # Fallback: take everything after the domain
                    group_path = "/".join(group_url.split("//")[1].split("/")[1:])
                else:
                    group_path = group_url

                return (group_path, iid, True)

        # URL format for project milestone: https://gitlab.../group/project/-/milestones/123
        if "/-/milestones/" in milestone_ref:
            parts = milestone_ref.split("/-/milestones/")
            if len(parts) == 2:
                project_url = parts[0]
                iid = parts[1].split("/")[0].split("?")[0]
                return (extract_path_from_url(project_url), iid, False)

        # %123 format (GitLab milestone reference)
        if milestone_ref.startswith("%"):
            return (None, milestone_ref[1:], None)

        # Plain number
        if milestone_ref.isdigit():
            return (None, milestone_ref, None)

        raise ValueError(f"Cannot parse milestone reference: {milestone_ref}")

    def parse_reference(self, reference: str) -> tuple:
        """Parse a resource reference string into its type, id, and optional project.

        Supported prefixes: # (issue), & (epic), % (milestone), ! (MR).
        Plain numbers are treated as issues.
        Full GitLab URLs are also accepted.

        Args:
            reference: Reference string such as "#123", "&21", "!5", "%10", or a URL.

        Returns:
            Tuple of (resource_type, resource_id, project_path).
            resource_type is one of "issue", "epic", "milestone", "mr".
            project_path is the extracted project/group path or None.

        Raises:
            ValueError: If the reference format is not recognised.
        """
        # URL-based detection
        if "://" in reference:
            if "/-/issues/" in reference:
                project_path, iid = parse_issue_url(reference)
                return ("issue", iid, project_path)
            if "/-/epics/" in reference:
                group_path, iid = self._parse_epic_reference(reference)
                return ("epic", iid, group_path)
            if "/-/milestones/" in reference:
                path, iid, _ = self._parse_milestone_reference(reference)
                return ("milestone", iid, path)
            if "/-/merge_requests/" in reference:
                mr_iid = reference.split("/-/merge_requests/")[-1].split("/")[0].split("?")[0]
                project_path_val = extract_path_from_url(reference.split("/-/merge_requests/")[0])
                return ("mr", mr_iid, project_path_val)

        prefix_map = {"#": "issue", "&": "epic", "%": "milestone", "!": "mr"}
        if reference[0] in prefix_map:
            return (prefix_map[reference[0]], reference[1:], None)

        if reference.isdigit():
            return ("issue", reference, None)

        raise ValueError(f"Invalid reference: {reference!r}")

    def _fetch_issue(self, issue_ref: str, project: Optional[str] = None) -> Dict[str, Any]:
        """Fetch raw issue data from GitLab without printing.

        Args:
            issue_ref: Issue reference (number, URL, or #number format).
            project: Optional project path override (e.g. "group/project").

        Returns:
            Dictionary containing issue data.

        Raises:
            PlatformError: If loading fails.
        """
        project_path, iid = self._parse_issue_reference(issue_ref)

        # The explicit project kwarg takes priority over the URL-parsed path.
        effective_project = project or project_path

        if effective_project:
            encoded_project = urllib.parse.quote(effective_project, safe="")
        else:
            # Use current repo via glab's :fullpath shorthand
            encoded_project = ":fullpath"

        api_endpoint = f"projects/{encoded_project}/issues/{iid}"

        output = self._run_glab_command(["api", api_endpoint])
        return json.loads(output)  # type: ignore[no-any-return]

    def load_issue(self, issue_ref: str, project: Optional[str] = None) -> Dict[str, Any]:
        """Load issue information from GitLab and print it.

        Args:
            issue_ref: Issue reference (number, URL, or #number format).
            project: Optional project path override (e.g. "group/project").

        Returns:
            Dictionary containing issue data.

        Raises:
            PlatformError: If loading fails.
        """
        issue_data = self._fetch_issue(issue_ref, project)

        timing: Dict[str, Any] = {}
        project_id = issue_data.get("project_id")
        issue_iid = issue_data.get("iid")
        if project_id and issue_iid:
            history = self._get_status_history(project_id, issue_iid)
            timing = self._compute_timing(history)

        # Print formatted issue info immediately for interactive use.
        self.print_ticket_info({"issue": issue_data, "epic": None, "links": None, "timing": timing})

        return issue_data  # type: ignore[no-any-return]

    def _fetch_epic(self, group_path: str, epic_iid: int) -> Dict[str, Any]:
        """Fetch epic data from GitLab API by group path and iid.

        Args:
            group_path: GitLab group path.
            epic_iid: Epic iid within the group.

        Returns:
            Dictionary containing epic data.

        Raises:
            PlatformError: If loading fails.
        """
        encoded_group = urllib.parse.quote(group_path, safe="")
        api_endpoint = f"groups/{encoded_group}/epics/{epic_iid}"

        output = self._run_glab_command(["api", api_endpoint])
        epic_data = json.loads(output)

        # Epic assignees are not exposed by the REST API; fetch via GraphQL WorkItem widgets.
        epic_data["assignees"] = self._fetch_epic_assignees(group_path, epic_iid)
        return epic_data  # type: ignore[no-any-return]

    def _fetch_epic_assignees(self, group_path: str, epic_iid: int) -> List[Dict[str, str]]:
        """Fetch epic assignees via GraphQL, which the REST API does not expose.

        Args:
            group_path: GitLab group full path.
            epic_iid: Epic IID within the group.

        Returns:
            List of assignee dicts with 'name' and 'username' keys. Empty if none or on error.
        """
        query = (
            "query($groupPath: ID!, $iid: String!) { "
            "group(fullPath: $groupPath) { workItem(iid: $iid) { widgets { type "
            "... on WorkItemWidgetAssignees { assignees { nodes { name username } } } } } } }"
        )
        try:
            output = self._run_glab_command(
                [
                    "api",
                    "graphql",
                    "-f",
                    f"query={query}",
                    "-f",
                    f"groupPath={group_path}",
                    "-f",
                    f"iid={epic_iid}",
                ]
            )
            data = json.loads(output)
            widgets = data.get("data", {}).get("group", {}).get("workItem", {}).get("widgets", [])
            for widget in widgets:
                if widget.get("type") == "ASSIGNEES":
                    nodes: List[Dict[str, str]] = widget.get("assignees", {}).get("nodes", [])
                    return nodes
        except (PlatformError, json.JSONDecodeError, KeyError, AttributeError) as err:
            logger.warning("Failed to fetch epic assignees for iid %s: %s", epic_iid, err)
        return []

    def load_epic(self, epic_ref: str) -> Dict[str, Any]:
        """Load epic information from GitLab.

        Args:
            epic_ref: Epic reference (e.g. "&21", plain number, or full URL).

        Returns:
            Dictionary containing epic data.

        Raises:
            PlatformError: If loading fails.
            ValueError: If the group path cannot be determined.
        """
        parsed_group, epic_iid = self._parse_epic_reference(epic_ref)
        final_group_path = parsed_group or self.config.get_default_group()

        if not final_group_path:
            raise ValueError(
                "Group path is required to load epic.\n"
                "Either include the group in the URL or set 'default_group' in config."
            )

        return self._fetch_epic(final_group_path, int(epic_iid))

    def _get_group_milestone_id(self, group_path: str, milestone_iid: str) -> Optional[str]:
        """Convert group milestone iid to id.

        Group milestone API requires id, not iid.
        List all milestones and find the one matching the iid.

        Args:
            group_path: GitLab group path.
            milestone_iid: Milestone iid within the group.

        Returns:
            Milestone id as string, or None if not found.

        Raises:
            PlatformError: If API call fails.
        """
        encoded_group = urllib.parse.quote(group_path, safe="")
        api_endpoint = f"groups/{encoded_group}/milestones?per_page=100"

        try:
            output = self._run_glab_command(["api", api_endpoint])
            milestones = json.loads(output) if output else []

            for ms in milestones:
                if str(ms.get("iid")) == str(milestone_iid):
                    return str(ms.get("id"))

            return None
        except (PlatformError, json.JSONDecodeError) as err:
            logger.warning("Failed to resolve milestone iid to id: %s", err)
            return None

    def load_epic_issues(self, group_path: str, epic_iid: int) -> List[Dict[str, Any]]:
        """Load all issues associated with an epic.

        Args:
            group_path: GitLab group path.
            epic_iid: Epic iid within the group.

        Returns:
            List of issue dictionaries.

        Raises:
            PlatformError: If loading fails.
        """
        encoded_group = urllib.parse.quote(group_path, safe="")
        api_endpoint = f"groups/{encoded_group}/epics/{epic_iid}/issues?per_page=100"

        try:
            output = self._run_glab_command(["api", api_endpoint])
            if not output:
                return []
            return json.loads(output)  # type: ignore[no-any-return]
        except PlatformError as err:
            logger.warning("Failed to load epic issues: %s", err)
            return []

    def load_epic_with_issues(self, epic_ref: str) -> Dict[str, Any]:
        """Load epic and all its associated issues.

        Args:
            epic_ref: Epic reference (number, URL, or &number format).

        Returns:
            Dictionary containing epic and issues data with structure:
            {
                'epic': {epic data},
                'issues': [list of issues]
            }

        Raises:
            PlatformError: If loading fails.
            ValueError: If group path cannot be determined.
        """
        # Parse epic reference
        parsed_group, epic_iid = self._parse_epic_reference(epic_ref)

        # Determine group path
        final_group_path = parsed_group or self.config.get_default_group()

        if not final_group_path:
            raise ValueError(
                "Group path is required to load epic.\n"
                "Either include the group in the URL or set 'default_group' in your glab_config.yaml file."
            )

        # Load epic data
        epic_data = self._fetch_epic(final_group_path, int(epic_iid))

        # Load epic issues and enrich each with its status timing.
        raw_issues = self.load_epic_issues(final_group_path, int(epic_iid))
        issues = []
        for issue in raw_issues:
            project_id = issue.get("project_id")
            issue_iid = issue.get("iid")
            timing: Dict[str, Any] = {}
            if project_id and issue_iid:
                history = self._get_status_history(project_id, issue_iid)
                timing = self._compute_timing(history)
            issues.append({**issue, "timing": timing})

        return {"epic": epic_data, "issues": issues}

    def _resolve_milestone_endpoints(
        self, parsed_path: Optional[str], milestone_iid: str, is_group_milestone: Optional[bool]
    ) -> tuple:
        """Resolve API endpoints for a milestone based on its type.

        Args:
            parsed_path: Group or project path, or None to use current project.
            milestone_iid: Milestone iid string.
            is_group_milestone: True for group milestone, False for project, None to auto-detect.

        Returns:
            Tuple of (api_endpoint, issues_endpoint).

        Raises:
            ValueError: If group path is required but missing.
            PlatformError: If milestone iid cannot be resolved to an id.
        """
        if is_group_milestone is None:
            is_group_milestone = bool(self.config.get_default_group())
            if is_group_milestone:
                parsed_path = self.config.get_default_group()

        if is_group_milestone:
            if not parsed_path:
                raise ValueError(
                    "Group path is required for group milestone.\n"
                    "Either include the group in the URL or set 'default_group' in your glab_config.yaml file."
                )
            encoded_path = urllib.parse.quote(parsed_path, safe="")
            milestone_id = self._get_group_milestone_id(parsed_path, milestone_iid)
            if not milestone_id:
                raise PlatformError(
                    f"Milestone iid {milestone_iid} not found in group {parsed_path}"
                )
            api_endpoint = f"groups/{encoded_path}/milestones/{milestone_id}"
            issues_endpoint = f"groups/{encoded_path}/milestones/{milestone_id}/issues?per_page=100"
        else:
            encoded_path = urllib.parse.quote(parsed_path, safe="") if parsed_path else ":fullpath"
            api_endpoint = f"projects/{encoded_path}/milestones/{milestone_iid}"
            issues_endpoint = (
                f"projects/{encoded_path}/milestones/{milestone_iid}/issues?per_page=100"
            )

        return api_endpoint, issues_endpoint

    def _load_epic_map(self, issues: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        """Build a mapping from epic_iid to epic data for all issues.

        Args:
            issues: List of issue dictionaries that may have epic_iid fields.

        Returns:
            Mapping from epic iid to epic data dict.
        """
        epic_map: Dict[int, Dict[str, Any]] = {}
        for issue in issues:
            epic_iid = issue.get("epic_iid")
            if not epic_iid or epic_iid in epic_map:
                continue
            project_path = issue.get("references", {}).get("full", "").split("#")[0]
            if not project_path:
                continue
            group_path = "/".join(project_path.split("/")[:-1])
            if group_path:
                try:
                    epic_map[epic_iid] = self._fetch_epic(group_path, epic_iid)
                except PlatformError as err:
                    logger.warning("Failed to load epic %s: %s", epic_iid, err)
        return epic_map

    def load_milestone(self, milestone_ref: str) -> Dict[str, Any]:
        """Load milestone information from GitLab.

        Convenience wrapper around :meth:`load_milestone_with_issues` that
        returns the full milestone data dictionary (including issues and epic map).

        Args:
            milestone_ref: Milestone reference (number, URL, or %number format).

        Returns:
            Dictionary containing milestone, issues, and epic mapping.

        Raises:
            PlatformError: If loading fails.
        """
        return self.load_milestone_with_issues(milestone_ref)

    def load_milestone_with_issues(self, milestone_ref: str) -> Dict[str, Any]:
        """Load milestone and all its associated issues.

        Args:
            milestone_ref: Milestone reference (number, URL, or %number format).

        Returns:
            Dictionary containing milestone, issues, and epic mapping with structure:
            {
                'milestone': {milestone data},
                'issues': [list of issues],
                'epic_map': {epic_iid: epic_data}
            }

        Raises:
            PlatformError: If loading fails.
        """
        parsed_path, milestone_iid, is_group_milestone = self._parse_milestone_reference(
            milestone_ref
        )
        api_endpoint, issues_endpoint = self._resolve_milestone_endpoints(
            parsed_path, milestone_iid, is_group_milestone
        )

        output = self._run_glab_command(["api", api_endpoint])
        milestone_data = json.loads(output)

        try:
            issues_output = self._run_glab_command(["api", issues_endpoint])
            issues = json.loads(issues_output) if issues_output else []
        except PlatformError as err:
            logger.warning("Failed to load milestone issues: %s", err)
            issues = []

        return {
            "milestone": milestone_data,
            "issues": issues,
            "epic_map": self._load_epic_map(issues),
        }

    def load_issue_links(self, project_path: str, issue_iid: str) -> Dict[str, List[Dict]]:
        """Load issue dependency links (blocking and blocked relationships).

        Args:
            project_path: GitLab project path.
            issue_iid: Issue iid within the project.

        Returns:
            Dictionary with 'blocking' and 'blocked' lists.

        Raises:
            PlatformError: If loading fails.
        """
        encoded_project = urllib.parse.quote(project_path, safe="")
        api_endpoint = f"projects/{encoded_project}/issues/{issue_iid}/links"

        try:
            output = self._run_glab_command(["api", api_endpoint])
            if not output:
                return {"blocking": [], "blocked": []}

            links = json.loads(output)

            # Separate into blocking (this issue blocks others) and blocked (this issue is blocked by others)
            blocking = []
            blocked_by = []

            for link in links:
                link_type = link.get("link_type")
                if link_type == "blocks":
                    # This issue blocks the linked issue
                    blocking.append(link)
                elif link_type == "is_blocked_by":
                    # This issue is blocked by the linked issue
                    blocked_by.append(link)

            return {"blocking": blocking, "blocked_by": blocked_by}
        except PlatformError as err:
            logger.warning("Failed to load issue links: %s", err)
            return {"blocking": [], "blocked_by": []}

    def load_ticket_with_epic(self, issue_ref: str) -> Dict[str, Any]:
        """Load issue and its related epic information.

        Args:
            issue_ref: Issue reference (number, URL, or #number format).

        Returns:
            Dictionary containing issue and epic data.

        Raises:
            PlatformError: If loading fails.
        """
        # Use _fetch_issue to avoid printing here; print_ticket_info is called by the caller.
        issue_data = self._fetch_issue(issue_ref)

        timing: Dict[str, Any] = {}
        project_id = issue_data.get("project_id")
        issue_iid_val = issue_data.get("iid")
        if project_id and issue_iid_val:
            history = self._get_status_history(project_id, issue_iid_val)
            timing = self._compute_timing(history)

        result: Dict[str, Any] = {
            "issue": issue_data,
            "epic": None,
            "links": {"blocking": [], "blocked_by": []},
            "timing": timing,
        }

        # Check if issue has an associated epic
        epic_iid = issue_data.get("epic_iid")
        if epic_iid:
            # Extract group path from issue's project
            # The epic belongs to the parent group of the project
            project_path = issue_data.get("references", {}).get("full", "")
            if project_path:
                # Remove issue reference part and project name to get group
                # Format: group/subgroup/project#123
                project_full = project_path.split("#")[0]
                group_path = "/".join(project_full.split("/")[:-1])

                if group_path:
                    try:
                        epic_data = self._fetch_epic(group_path, epic_iid)
                        result["epic"] = epic_data
                    except PlatformError as err:
                        logger.warning("Failed to load epic %s: %s", epic_iid, err)
        # Load issue dependency links
        project_path = issue_data.get("references", {}).get("full", "").split("#")[0]
        if project_path:
            issue_iid = issue_data.get("iid")
            result["links"] = self.load_issue_links(project_path, str(issue_iid))

        return result

    def print_ticket_info(self, data: Dict[str, Any]) -> None:
        """Print ticket information in markdown format.

        Args:
            data: Dictionary containing issue and epic data.
        """
        print_issue(
            data["issue"],
            epic=data.get("epic"),
            links=data.get("links"),
            timing=data.get("timing", {}),
        )

    # Status values that mean the issue was rejected and no work should be counted.
    _REJECTED_STATUSES: frozenset = frozenset({"duplicate", "won't do", "wouldn't do"})
    _IN_PROGRESS_STATUS = "in progress"
    _DONE_STATUS = "done"

    def _get_status_history(self, project_id: int, issue_iid: int) -> List[Dict[str, str]]:
        """Return chronological list of status transitions from issue system notes.

        Each entry has 'status' (raw value from GitLab) and 'timestamp' (ISO 8601 string).
        GitLab returns notes newest-first; this method reverses them so callers get
        oldest-first ordering, which makes timeline reasoning straightforward.

        Args:
            project_id: GitLab project ID (numeric).
            issue_iid: Issue IID within the project.

        Returns:
            List of {'status': str, 'timestamp': str} dicts, oldest first.
            Empty list if notes cannot be fetched or no status transitions exist.
        """
        api_endpoint = f"projects/{project_id}/issues/{issue_iid}/notes?per_page=100"
        pattern = re.compile(r"set status to \*\*(.+?)\*\*", re.IGNORECASE)

        try:
            output = self._run_glab_command(["api", api_endpoint])
            notes = json.loads(output) if output else []
        except (PlatformError, json.JSONDecodeError) as err:
            logger.warning("Failed to fetch status history for issue %s: %s", issue_iid, err)
            return []

        history = []
        for note in notes:
            if not note.get("system"):
                continue
            match = pattern.search(note.get("body", ""))
            if match:
                history.append({"status": match.group(1), "timestamp": note.get("created_at", "")})

        # Notes arrive newest-first; reverse so callers see oldest-first.
        history.reverse()
        return history

    def _compute_timing(self, history: List[Dict[str, str]]) -> Dict[str, Any]:
        """Derive issue timing from a chronological status history.

        Rules:
        - start_date: timestamp of the first "In progress" transition, or None if
          the issue moved directly from "To do" to "Done" without an in-progress step.
        - end_date: timestamp of the last "Done" transition, or None.
        - is_rejected: True when the final status is "Duplicate" or "Won't do".
          Rejected issues have neither start_date nor end_date (no work counted).
        - current_status: the most recent status string, or None if no history.

        Args:
            history: Chronological list from _get_status_history (oldest first).

        Returns:
            Dict with keys: current_status, start_date, end_date, is_rejected.
        """
        if not history:
            return {
                "current_status": None,
                "start_date": None,
                "end_date": None,
                "is_rejected": False,
            }

        current_status = history[-1]["status"]
        is_rejected = current_status.lower() in self._REJECTED_STATUSES

        if is_rejected:
            return {
                "current_status": current_status,
                "start_date": None,
                "end_date": None,
                "is_rejected": True,
            }

        start_date: Optional[str] = None
        end_date: Optional[str] = None

        for entry in history:
            status_lower = entry["status"].lower()
            if start_date is None and status_lower == self._IN_PROGRESS_STATUS:
                start_date = entry["timestamp"]
            if status_lower == self._DONE_STATUS:
                # Keep updating so we capture the *last* Done transition.
                end_date = entry["timestamp"]

        return {
            "current_status": current_status,
            "start_date": start_date,
            "end_date": end_date,
            "is_rejected": False,
        }

    def print_epic_info(self, data: Dict[str, Any]) -> None:
        """Print epic information in markdown format.

        Args:
            data: Dictionary containing epic and issues data.
        """
        print_epic(
            data["epic"],
            data.get("issues", []),
            derived_dates=self._derive_epic_dates(data.get("issues", [])),
        )

    @staticmethod
    def _derive_epic_dates(
        issues: List[Dict[str, Any]],
    ) -> Dict[str, Optional[str]]:
        """Derive epic start and end dates purely from issue status flows.

        Only non-rejected issues contribute. Start is the earliest first-"In progress"
        timestamp; end is the latest last-"Done" timestamp, or None if any contributing
        issue has not yet reached "Done".

        Args:
            issues: Issues enriched with a 'timing' dict from _compute_timing.

        Returns:
            Dict with 'start_date' and 'end_date', either an ISO timestamp or None.
        """
        start_timestamps: List[str] = []
        end_timestamps: List[str] = []
        any_unfinished = False

        for issue in issues:
            timing = issue.get("timing", {})
            if timing.get("is_rejected"):
                continue
            start = timing.get("start_date")
            end = timing.get("end_date")
            if start:
                start_timestamps.append(start)
            if end:
                end_timestamps.append(end)
            else:
                any_unfinished = True

        return {
            "start_date": min(start_timestamps) if start_timestamps else None,
            "end_date": (
                None if any_unfinished else (max(end_timestamps) if end_timestamps else None)
            ),
        }

    def print_milestone_info(self, data: Dict[str, Any]) -> None:
        """Print milestone information in markdown format.

        Args:
            data: Dictionary containing milestone, issues, and epic mapping.
        """
        print_milestone(
            data["milestone"],
            data.get("issues", []),
            data.get("epic_map", {}),
        )

    def _normalize_mr_ref(self, mr_ref: str) -> str:
        """Strip ! prefix and URL-decode an MR reference to a bare integer string."""
        if mr_ref.startswith("!"):
            mr_ref = mr_ref[1:]
        if "://" in mr_ref:
            if "/-/merge_requests/" in mr_ref:
                mr_ref = mr_ref.split("/-/merge_requests/")[-1].split("/")[0].split("?")[0]
            else:
                raise ValueError(f"Invalid MR URL format: {mr_ref}")
        if not mr_ref.isdigit():
            raise ValueError(f"Invalid MR reference: {mr_ref}")
        return mr_ref

    def load_mr(self, mr_ref: str, project: Optional[str] = None) -> Dict[str, Any]:
        """Load merge request information from GitLab.

        Args:
            mr_ref: MR reference (number, URL, or !number format).
            project: Optional project path override (e.g. "group/project").
                     Currently informational; glab infers the project from git context.

        Returns:
            Dictionary containing MR data.

        Raises:
            PlatformError: If loading fails.
        """
        mr_ref = self._normalize_mr_ref(mr_ref)
        logger.debug("Loading MR !%s (project=%s)", mr_ref, project)
        cmd = ["mr", "view", mr_ref, "--output", "json"]
        output = self._run_glab_command(cmd)
        mr_data = json.loads(output)
        return {"mr": mr_data}

    def load_mr_comments(self, mr_ref: str) -> Dict[str, Any]:
        """Load MR metadata and non-system review comments (notes/discussions).

        Filters out system notes (auto-merge, reviewer assignments, etc.) and returns
        only human-authored comments, preserving discussion threading and inline position.

        Args:
            mr_ref: MR reference (number, URL, or !number format).

        Returns:
            Dictionary with keys ``mr`` (metadata) and ``comments`` (list of dicts with
            keys: id, author, body, resolvable, resolved, file_path, line).

        Raises:
            PlatformError: If loading fails.
        """
        mr_ref = self._normalize_mr_ref(mr_ref)
        logger.debug("Loading MR !%s comments", mr_ref)

        mr_data = json.loads(self._run_glab_command(["mr", "view", mr_ref, "--output", "json"]))
        raw_discussions = json.loads(
            self._run_glab_command(["mr", "note", "list", mr_ref, "--output", "json"])
        )

        comments: List[Dict[str, Any]] = []
        for discussion in raw_discussions:
            for note in discussion.get("notes", []):
                if note.get("system"):
                    continue
                body = note.get("body", "").strip()
                if not body:
                    continue
                position = note.get("position") or {}
                comments.append(
                    {
                        "id": note["id"],
                        "author": note.get("author", {}).get("name", "Unknown"),
                        "body": body,
                        "resolvable": note.get("resolvable", False),
                        "resolved": note.get("resolved", False),
                        "file_path": position.get("new_path", ""),
                        "line": position.get("new_line", ""),
                        "created_at": note.get("created_at", ""),
                    }
                )

        return {"mr": mr_data, "comments": comments}

    def print_mr_info(self, data: Dict[str, Any], with_comments: bool = False) -> None:
        """Print merge request information in markdown format.

        Args:
            data: Dictionary containing MR data (and optionally comments).
            with_comments: When True, also prints review comments.
        """
        print_mr(data["mr"])
        if with_comments and "comments" in data:
            print_mr_comments(data["comments"])
