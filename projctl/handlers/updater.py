"""Ticket (issue/MR/epic/milestone) updater handler."""

import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from ..exceptions import PlatformError
from ..utils.validation import validate_labels
from .loader import TicketLoader

logger = logging.getLogger(__name__)


class TicketUpdater:
    """Updates GitLab issues, MRs, epics, and milestones using the glab CLI."""

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        """Initialize the updater.

        Args:
            config: Configuration object with defaults.
            dry_run: If True, print what would be sent without executing.
        """
        self.config = config
        self.dry_run = dry_run
        # Reuse loader for reference parsing, label fetching, and glab execution.
        self._loader = TicketLoader(config)

    def _validate_labels_add(self, labels_add: Optional[List[str]]) -> None:
        """Raise ValueError if any label in labels_add is not in the allowed list."""
        if labels_add:
            validate_labels(labels_add, self.config.get_allowed_labels())

    def _fetch_and_merge_labels(
        self,
        endpoint: str,
        labels_add: Optional[List[str]],
        labels_remove: Optional[List[str]],
        *,
        label_key: str = "labels",
    ) -> Optional[str]:
        """Fetch current labels from a resource and compute the merged set.

        GitLab's PUT endpoint replaces labels entirely, so we must fetch the
        current set and compute the desired result ourselves.

        Args:
            endpoint: GitLab API path for the resource.
            labels_add: Labels to add.
            labels_remove: Labels to remove.
            label_key: JSON key that holds labels (differs for epics vs issues).

        Returns:
            Comma-separated label string, or None if no label change requested.
        """
        if not labels_add and not labels_remove:
            return None

        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        current_data = json.loads(self._loader._run_glab_command(["api", endpoint]))
        raw_labels = current_data.get(label_key, [])
        # Epic labels may be dicts with a 'name' key; issue/MR labels are plain strings.
        current_labels: List[str] = [
            lbl["name"] if isinstance(lbl, dict) else lbl for lbl in raw_labels
        ]
        return ",".join(self._merge_labels(current_labels, labels_add, labels_remove))

    def _merge_labels(
        self,
        current: List[str],
        add: Optional[List[str]],
        remove: Optional[List[str]],
    ) -> List[str]:
        """Compute the final label list after additions and removals.

        Args:
            current: Labels currently on the resource.
            add: Labels to add.
            remove: Labels to remove.

        Returns:
            Final label list.
        """
        result = set(current)
        if add:
            result.update(add)
        if remove:
            result.difference_update(remove)
        return sorted(result)

    def _build_put_cmd(
        self,
        endpoint: str,
        fields: Dict[str, Any],
    ) -> List[str]:
        """Build a glab api -X PUT command from an endpoint and field map.

        Args:
            endpoint: GitLab API path (e.g. 'projects/:id/issues/1').
            fields: Mapping of field name to value. None values are skipped.

        Returns:
            Argument list for _loader._run_glab_command (without 'glab' prefix).
        """
        cmd = ["api", "-X", "PUT", endpoint]
        for key, value in fields.items():
            if value is not None:
                cmd.extend(["-f", f"{key}={value}"])
        return cmd

    def _resolve_user_id(self, username: str) -> str:
        """Resolve a GitLab username to its numeric user ID.

        GitLab's PUT API requires numeric IDs for assignee_ids and reviewer_ids,
        not usernames.

        Args:
            username: GitLab username to resolve.

        Returns:
            Numeric user ID as a string.

        Raises:
            ValueError: If no user is found for the given username.
            PlatformError: If the API call fails.
        """
        encoded = urllib.parse.quote(username, safe="")
        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        output = self._loader._run_glab_command(["api", f"users?username={encoded}"])
        users = json.loads(output)
        if not users:
            raise ValueError(f"No GitLab user found for username: {username!r}")
        return str(users[0]["id"])

    def _resolve_milestone_id(self, milestone_ref: str, project_path: Optional[str] = None) -> str:
        """Resolve a milestone title or iid string to its numeric ID.

        GitLab's PUT API requires the milestone's database ID (not iid) in the
        milestone_id field. This method searches by iid or title.

        Args:
            milestone_ref: Milestone iid (as string) or title (e.g. "v2.0").
            project_path: Project namespace path. Defaults to ':fullpath' sentinel.

        Returns:
            Numeric milestone ID as a string.

        Raises:
            ValueError: If no matching milestone is found.
            PlatformError: If the API call fails.
        """
        if project_path:
            encoded_path = urllib.parse.quote(project_path, safe="")
        else:
            encoded_path = ":fullpath"

        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        output = self._loader._run_glab_command(["api", f"projects/{encoded_path}/milestones"])
        milestones = json.loads(output)
        for m in milestones:
            if str(m.get("iid")) == milestone_ref or m.get("title") == milestone_ref:
                return str(m["id"])
        raise ValueError(f"Milestone not found: {milestone_ref!r}")

    def _resolve_group_milestone_id(self, milestone_ref: str, group_path: str) -> str:
        """Resolve a milestone title or iid string to its numeric ID via group milestones.

        GitLab's PUT /groups/:id/epics/:iid endpoint requires the milestone's
        global database ID (not the iid). Group-level milestones live at
        GET /groups/:id/milestones, not /projects/:id/milestones.

        Args:
            milestone_ref: Milestone iid (as string) or title (e.g. "v2.0").
            group_path: Group namespace path (e.g. "my-org/my-group").

        Returns:
            Numeric milestone ID as a string.

        Raises:
            ValueError: If no matching milestone is found.
            PlatformError: If the API call fails.
        """
        # Strip leading % so callers can pass either "14" or "%14"
        ref = milestone_ref.lstrip("%")
        encoded_group = urllib.parse.quote(group_path, safe="")
        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        # per_page=100 and state=all ensure all milestones are returned in one request.
        output = self._loader._run_glab_command(
            ["api", f"groups/{encoded_group}/milestones?per_page=100&state=all"]
        )
        milestones = json.loads(output)
        for m in milestones:
            if str(m.get("iid")) == ref or m.get("title") == ref:
                return str(m["id"])
        raise ValueError(f"Group milestone not found: {milestone_ref!r}")

    def _set_epic_milestone_via_graphql(self, work_item_id: int, milestone_db_id: str) -> None:
        """Set an epic's milestone using the GraphQL workItemUpdate mutation.

        GitLab 15.9+ backs epics with work items. The REST epics API silently
        ignores ``milestone_id`` on these epics; GraphQL ``milestoneWidget`` is
        the only supported path.

        Args:
            work_item_id: The numeric work_item_id from the epic REST response.
            milestone_db_id: The milestone's global database ID (not iid) as a string.
        """
        wi_gid = f"gid://gitlab/WorkItem/{work_item_id}"
        ms_gid = f"gid://gitlab/Milestone/{milestone_db_id}"
        query = (
            "mutation { workItemUpdate(input: { "
            f'id: "{wi_gid}", '
            f'milestoneWidget: {{ milestoneId: "{ms_gid}" }} '
            "}) { workItem { title } errors } }"
        )
        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        output = self._loader._run_glab_command(["api", "graphql", "-f", f"query={query}"])
        resp = json.loads(output)
        errors = resp.get("data", {}).get("workItemUpdate", {}).get("errors", [])
        if errors:
            raise PlatformError(f"GraphQL milestone assignment failed: {errors}")

    def _resolve_epic_global_id(self, epic_ref: str) -> tuple:
        """Resolve an epic reference to its global database ID and iid.

        GitLab's issue PUT endpoint accepts epic_id (the global database ID),
        not the group-scoped iid, so we must fetch the epic first.

        Args:
            epic_ref: Epic reference (&number, URL, or plain number format).

        Returns:
            Tuple of (global_epic_id, epic_iid) as strings.

        Raises:
            PlatformError: If the API call fails.
            ValueError: If the epic reference cannot be parsed or group is unavailable.
        """
        # pylint: disable=protected-access
        # TicketLoader's reference-parsing methods are internal helpers shared
        # between sibling handler classes; no public API exists for them.
        parsed_group, epic_iid = self._loader._parse_epic_reference(epic_ref)
        group_path = parsed_group or self.config.get_default_group()

        if not group_path:
            raise ValueError(
                "Group path is required to assign issue to epic.\n"
                "Either include the group in the epic URL or set 'default_group' in your config."
            )

        encoded_group = urllib.parse.quote(group_path, safe="")
        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        epic_data = json.loads(
            self._loader._run_glab_command(["api", f"groups/{encoded_group}/epics/{epic_iid}"])
        )
        return str(epic_data["id"]), epic_iid

    def _assign_issue_to_epic(self, issue_ref: str, epic_ref: str) -> None:
        """Assign an issue to a GitLab epic via the issue update API.

        GitLab's issue PUT endpoint accepts epic_id (global epic database ID).
        The POST /groups/:id/epics/:iid/issues endpoint is unavailable on some
        GitLab configurations; using PUT /projects/:id/issues/:iid with epic_id
        is the reliable alternative.

        Args:
            issue_ref: Issue reference (number, URL, or #number format).
            epic_ref: Epic reference (&number, URL, or plain number format).

        Raises:
            PlatformError: If the API call fails.
            ValueError: If either reference cannot be parsed or group is unavailable.
        """
        # pylint: disable=protected-access
        # TicketLoader's reference-parsing methods are internal helpers shared
        # between sibling handler classes; no public API exists for them.
        project_path, iid = self._loader._parse_issue_reference(issue_ref)

        if project_path:
            encoded_project = urllib.parse.quote(project_path, safe="")
        else:
            encoded_project = ":fullpath"

        global_epic_id, epic_iid = self._resolve_epic_global_id(epic_ref)

        endpoint = f"projects/{encoded_project}/issues/{iid}"
        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        self._loader._run_glab_command(self._build_put_cmd(endpoint, {"epic_id": global_epic_id}))
        print(f"✓ Assigned issue #{iid} to epic &{epic_iid}")

    def _resolve_project_id(self) -> str:
        """Resolve the current project's numeric ID via the API.

        The GitLab issue-links POST endpoint requires a numeric project ID in
        ``target_project_id``; ``:fullpath`` expansion only works in URL paths,
        not in ``-f`` form fields passed to ``glab api``.

        Returns:
            Numeric project ID as a string.

        Raises:
            PlatformError: If the API call fails.
        """
        # pylint: disable=protected-access
        output = self._loader._run_glab_command(["api", "projects/:fullpath"])
        return str(json.loads(output)["id"])

    def add_issue_link(self, issue_ref: str, target_ref: str, link_type: str = "is_blocked_by") -> None:
        """Add a blocking/blocked-by link between two issues.

        Args:
            issue_ref: Source issue reference (number, URL, or #number format).
            target_ref: Target issue reference (number, URL, or #number format).
            link_type: One of "is_blocked_by", "blocks", or "relates_to".

        Raises:
            PlatformError: If the API call fails.
            ValueError: If either reference cannot be parsed.
        """
        # pylint: disable=protected-access
        project_path, iid = self._loader._parse_issue_reference(issue_ref)
        _, target_iid = self._loader._parse_issue_reference(target_ref)

        encoded_project = urllib.parse.quote(project_path, safe="") if project_path else ":fullpath"
        endpoint = f"projects/{encoded_project}/issues/{iid}/links"

        if self.dry_run:
            print(f"[DRY RUN] Would POST {endpoint}: {link_type} link to #{target_iid}")
            return

        # target_project_id must be a numeric ID — :fullpath is not expanded in -f fields.
        project_id = self._resolve_project_id()
        cmd = [
            "api", "-X", "POST", endpoint,
            "-f", f"target_project_id={project_id}",
            "-f", f"target_issue_iid={target_iid}",
            "-f", f"link_type={link_type}",
        ]
        self._loader._run_glab_command(cmd)
        print(f"✓ Added {link_type} link: issue #{iid} ← #{target_iid}")

    def remove_issue_link(self, issue_ref: str, target_ref: str) -> None:
        """Remove a link between two issues.

        Fetches current links, finds the one matching target_ref by IID, then
        deletes it by link ID.

        Args:
            issue_ref: Source issue reference (number, URL, or #number format).
            target_ref: Target issue reference (number, URL, or #number format).

        Raises:
            PlatformError: If the API call fails.
            ValueError: If either reference cannot be parsed or no matching link exists.
        """
        # pylint: disable=protected-access
        project_path, iid = self._loader._parse_issue_reference(issue_ref)
        _, target_iid = self._loader._parse_issue_reference(target_ref)

        encoded_project = urllib.parse.quote(project_path, safe="") if project_path else ":fullpath"
        links_endpoint = f"projects/{encoded_project}/issues/{iid}/links"

        output = self._loader._run_glab_command(["api", links_endpoint])
        links = json.loads(output)

        link_id: Optional[int] = None
        for link in links:
            if str(link.get("iid")) == target_iid:
                # GitLab returns "issue_link_id" as the link record ID; "id" is
                # the linked issue's database ID and is not accepted by the DELETE endpoint.
                link_id = link.get("issue_link_id")
                break

        if link_id is None:
            raise ValueError(f"No link found between issue #{iid} and #{target_iid}")

        if self.dry_run:
            print(f"[DRY RUN] Would DELETE {links_endpoint}/{link_id} (link to #{target_iid})")
            return

        self._loader._run_glab_command(["api", "-X", "DELETE", f"{links_endpoint}/{link_id}"])
        print(f"✓ Removed link between issue #{iid} and #{target_iid}")

    def update_issue(  # pylint: disable=too-many-locals,too-many-branches,too-many-arguments
        # Weight adds one more argument and one more branch, pushing the counts
        # just above the default pylint thresholds. Extracting a helper would
        # obscure the single-method read-then-update flow.
        self,
        issue_ref: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        labels_add: Optional[List[str]] = None,
        labels_remove: Optional[List[str]] = None,
        assignee: Optional[str] = None,
        milestone: Optional[str] = None,
        state_event: Optional[str] = None,
        epic: Optional[str] = None,
        weight: Optional[int] = None,
        due_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing GitLab issue.

        Args:
            issue_ref: Issue reference (number, URL, or #number format).
            title: New title, or None to leave unchanged.
            description: New description, or None to leave unchanged.
            labels_add: Labels to add to the current set.
            labels_remove: Labels to remove from the current set.
            assignee: Assignee username to set, or None to leave unchanged.
            milestone: Milestone title or iid to set, or None to leave unchanged.
            state_event: 'close' or 'reopen', or None to leave unchanged.
            epic: Epic reference to assign the issue to (e.g. &47), or None to skip.
            weight: Story-point weight (non-negative integer), or None to leave unchanged.
            due_date: Due date in YYYY-MM-DD format, or None to leave unchanged.

        Returns:
            Updated issue data returned by the API.

        Raises:
            PlatformError: If the update fails.
            ValueError: If the issue reference cannot be parsed.
        """
        self._validate_labels_add(labels_add)

        # pylint: disable=protected-access
        # TicketLoader's reference-parsing methods are internal helpers shared
        # between sibling handler classes; no public API exists for them.
        project_path, iid = self._loader._parse_issue_reference(issue_ref)

        if project_path:
            encoded_project = urllib.parse.quote(project_path, safe="")
        else:
            encoded_project = ":fullpath"

        endpoint = f"projects/{encoded_project}/issues/{iid}"

        fields: Dict[str, Any] = {
            "title": title,
            "description": description,
            "state_event": state_event,
        }
        if weight is not None:
            fields["weight"] = weight
        if due_date is not None:
            fields["due_date"] = due_date

        # Determine whether there are fields to PUT (epic assignment is a
        # separate POST and does not go through the PUT endpoint).
        has_put_fields = any(
            [
                title,
                description,
                state_event,
                labels_add,
                labels_remove,
                assignee,
                milestone,
                weight,
                due_date,
            ]
        )

        if self.dry_run:
            if has_put_fields:
                # Show intent for labels without making a live API call.
                if labels_add or labels_remove:
                    fields["labels"] = f"<add: {labels_add or []}, remove: {labels_remove or []}>"
                if assignee is not None:
                    fields["assignee_ids"] = f"<resolve user: {assignee}>"
                if milestone is not None:
                    fields["milestone_id"] = f"<resolve milestone: {milestone}>"
                print(f"[DRY RUN] Would PUT {endpoint} with fields: {fields}")
            if epic is not None:
                print(f"[DRY RUN] Would assign issue #{iid} to epic &{epic.lstrip('&')}")
            return {}

        result: Dict[str, Any] = {}
        if has_put_fields:
            labels_value = self._fetch_and_merge_labels(endpoint, labels_add, labels_remove)
            if labels_value is not None:
                fields["labels"] = labels_value
            if assignee is not None:
                fields["assignee_ids"] = self._resolve_user_id(assignee)
            if milestone is not None:
                fields["milestone_id"] = self._resolve_milestone_id(milestone, project_path)

            # pylint: disable=protected-access
            # TicketLoader's _run_glab_command is an internal helper shared between
            # sibling handler classes; no public API exists for command execution.
            output = self._loader._run_glab_command(self._build_put_cmd(endpoint, fields))
            result = json.loads(output)

            ref_display = result.get("iid", iid)
            result_title = result.get("title", "")
            print(f"✓ Updated issue #{ref_display}: {result_title}")

        if epic is not None:
            self._assign_issue_to_epic(issue_ref, epic)

        return result

    def update_mr(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        mr_ref: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        labels_add: Optional[List[str]] = None,
        labels_remove: Optional[List[str]] = None,
        assignee: Optional[str] = None,
        reviewer: Optional[str] = None,
        milestone: Optional[str] = None,
        target_branch: Optional[str] = None,
        state_event: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing GitLab merge request.

        MRs have more updatable fields than other resources (reviewer, target_branch),
        so the argument count intentionally exceeds the default pylint limit.

        Args:
            mr_ref: MR reference (number, URL, or !number format).
            title: New title, or None to leave unchanged.
            description: New description, or None to leave unchanged.
            labels_add: Labels to add to the current set.
            labels_remove: Labels to remove from the current set.
            assignee: Assignee username to set, or None to leave unchanged.
            reviewer: Reviewer username to set, or None to leave unchanged.
            milestone: Milestone title or iid to set, or None to leave unchanged.
            target_branch: Target branch to set, or None to leave unchanged.
            state_event: 'close' or 'reopen', or None to leave unchanged.

        Returns:
            Updated MR data returned by the API.

        Raises:
            PlatformError: If the update fails.
            ValueError: If the MR reference cannot be parsed.
        """
        self._validate_labels_add(labels_add)

        project_path, iid = self._parse_mr_reference(mr_ref)

        if project_path:
            encoded_project = urllib.parse.quote(project_path, safe="")
            endpoint = f"projects/{encoded_project}/merge_requests/{iid}"
        else:
            endpoint = f"projects/:id/merge_requests/{iid}"

        fields: Dict[str, Any] = {
            "title": title,
            "description": description,
            "state_event": state_event,
            "target_branch": target_branch,
        }

        if self.dry_run:
            # Show intent for labels without making a live API call.
            if labels_add or labels_remove:
                fields["labels"] = f"<add: {labels_add or []}, remove: {labels_remove or []}>"
            if assignee is not None:
                fields["assignee_ids"] = f"<resolve user: {assignee}>"
            if reviewer is not None:
                fields["reviewer_ids"] = f"<resolve user: {reviewer}>"
            if milestone is not None:
                fields["milestone_id"] = f"<resolve milestone: {milestone}>"
            print(f"[DRY RUN] Would PUT {endpoint} with fields: {fields}")
            return {}

        labels_value = self._fetch_and_merge_labels(endpoint, labels_add, labels_remove)
        if labels_value is not None:
            fields["labels"] = labels_value
        if assignee is not None:
            fields["assignee_ids"] = self._resolve_user_id(assignee)
        if reviewer is not None:
            fields["reviewer_ids"] = self._resolve_user_id(reviewer)
        if milestone is not None:
            fields["milestone_id"] = self._resolve_milestone_id(milestone, project_path)

        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        output = self._loader._run_glab_command(self._build_put_cmd(endpoint, fields))
        result: Dict[str, Any] = json.loads(output)

        ref_display = result.get("iid", iid)
        result_title = result.get("title", "")
        print(f"✓ Updated mr #{ref_display}: {result_title}")
        return result

    def update_epic(
        self,
        epic_ref: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        labels_add: Optional[List[str]] = None,
        labels_remove: Optional[List[str]] = None,
        state_event: Optional[str] = None,
        milestone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing GitLab epic.

        Args:
            epic_ref: Epic reference (number, URL, or &number format).
            title: New title, or None to leave unchanged.
            description: New description, or None to leave unchanged.
            labels_add: Labels to add to the current set.
            labels_remove: Labels to remove from the current set.
            state_event: 'close' or 'reopen', or None to leave unchanged.
            milestone: Group milestone title or iid to set, or None to leave unchanged.

        Returns:
            Updated epic data returned by the API.

        Raises:
            PlatformError: If the update fails.
            ValueError: If the epic reference cannot be parsed or group is unavailable.
        """
        self._validate_labels_add(labels_add)

        # pylint: disable=protected-access
        # TicketLoader's reference-parsing methods are internal helpers shared
        # between sibling handler classes; no public API exists for them.
        parsed_group, iid = self._loader._parse_epic_reference(epic_ref)
        group_path = parsed_group or self.config.get_default_group()

        if not group_path:
            raise ValueError(
                "Group path is required to update epic.\n"
                "Either include the group in the URL or set 'default_group' in your config."
            )

        endpoint = f"groups/{urllib.parse.quote(group_path, safe='')}/epics/{iid}"

        fields: Dict[str, Any] = {
            "title": title,
            "description": description,
            "state_event": state_event,
        }

        if self.dry_run:
            # Show intent for labels without making a live API call.
            if labels_add or labels_remove:
                fields["labels"] = f"<add: {labels_add or []}, remove: {labels_remove or []}>"
            if milestone is not None:
                fields["milestone_id"] = f"<resolve group milestone: {milestone}>"
            print(f"[DRY RUN] Would PUT {endpoint} with fields: {fields}")
            return {}

        # Fetch current title if not provided — GitLab rejects PUT with only milestone_id.
        if title is None:
            # pylint: disable=protected-access
            # TicketLoader's _run_glab_command is an internal helper shared between
            # sibling handler classes; no public API exists for command execution.
            current = json.loads(self._loader._run_glab_command(["api", endpoint]))
            fields["title"] = current.get("title", "")
        else:
            fields["title"] = title

        # Resolve milestone to its numeric database ID before the PUT so the
        # ID is included in the initial request (required for older GitLab).
        if milestone is not None:
            fields["milestone_id"] = self._resolve_group_milestone_id(milestone, group_path)

        # Epic labels are returned as dicts with a 'name' key.
        labels_value = self._fetch_and_merge_labels(
            endpoint, labels_add, labels_remove, label_key="labels"
        )
        if labels_value is not None:
            fields["labels"] = labels_value

        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        output = self._loader._run_glab_command(self._build_put_cmd(endpoint, fields))
        result: Dict[str, Any] = json.loads(output)

        # GitLab 15.9+ epics are backed by work items; the REST epics API silently
        # ignores milestone_id on those. Use GraphQL workItemUpdate as an additional
        # step when the response exposes a work_item_id.
        if milestone is not None:
            work_item_id = result.get("work_item_id")
            if work_item_id:
                self._set_epic_milestone_via_graphql(work_item_id, fields["milestone_id"])

        print(f"✓ Updated epic #{result.get('iid', iid)}: {result.get('title', '')}")
        return result

    def update_milestone(
        self,
        milestone_ref: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        state_event: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing GitLab milestone.

        Args:
            milestone_ref: Milestone reference (number, URL, or %number format).
            title: New title, or None to leave unchanged.
            description: New description, or None to leave unchanged.
            due_date: New due date in YYYY-MM-DD format, or None to leave unchanged.
            state_event: 'close' or 'activate', or None to leave unchanged.

        Returns:
            Updated milestone data returned by the API.

        Raises:
            PlatformError: If the update fails.
            ValueError: If the milestone reference cannot be parsed.
        """
        # pylint: disable=protected-access
        # TicketLoader's reference-parsing methods are internal helpers shared
        # between sibling handler classes; no public API exists for them.
        parsed_path, iid, is_group_milestone = self._loader._parse_milestone_reference(
            milestone_ref
        )

        # Reuse loader's shared endpoint resolver to avoid duplication.
        api_endpoint, _ = self._loader._resolve_milestone_endpoints(
            parsed_path, iid, is_group_milestone
        )
        # Strip the trailing /issues query part to get the base milestone endpoint.
        endpoint = api_endpoint

        fields: Dict[str, Any] = {
            "title": title,
            "description": description,
            "due_date": due_date,
            "state_event": state_event,
        }
        # None values are skipped by _build_put_cmd; no need for an extra
        # dict comprehension here (consistent with update_issue/update_mr/update_epic).

        if self.dry_run:
            print(f"[DRY RUN] Would PUT {endpoint} with fields: {fields}")
            return {}

        # pylint: disable=protected-access
        # TicketLoader's _run_glab_command is an internal helper shared between
        # sibling handler classes; no public API exists for command execution.
        output = self._loader._run_glab_command(self._build_put_cmd(endpoint, fields))
        result: Dict[str, Any] = json.loads(output)

        ref_display = result.get("iid", iid)
        result_title = result.get("title", "")
        print(f"✓ Updated milestone #{ref_display}: {result_title}")
        return result

    @staticmethod
    def _parse_mr_reference(mr_ref: str) -> Tuple[Optional[str], str]:
        """Parse an MR reference to extract the optional project path and iid.

        Args:
            mr_ref: MR reference (!number, URL, or plain number).

        Returns:
            Tuple of (project_path, iid). project_path is None for non-URL refs.

        Raises:
            ValueError: If the reference cannot be parsed.
        """
        ref = mr_ref.lstrip("!")

        project_path: Optional[str] = None
        if "/-/merge_requests/" in ref:
            parts = ref.split("/-/merge_requests/")
            raw_path = parts[0]
            iid_part = parts[1].split("/")[0].split("?")[0]
            # Strip scheme and host from URL references (e.g. https://gitlab.com/group/project).
            if "://" in raw_path:
                raw_path = "/".join(raw_path.split("/")[3:])
            project_path = raw_path if raw_path else None
            ref = iid_part

        if not ref.isdigit():
            raise ValueError(f"Cannot parse MR reference: {mr_ref}")

        return project_path, ref
