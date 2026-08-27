"""Note (comment) handler for posting notes to GitLab issues, MRs, and epics."""

import json
import logging
import urllib.parse
from typing import List, Optional, Tuple

from ..config import Config
from ..exceptions import PlatformError
from ..utils.git_helpers import parse_epic_url, parse_issue_url
from ..utils.glab_runner import DRY_RUN, run_glab_json

logger = logging.getLogger(__name__)


class NoteHandler:
    """Posts notes to GitLab issues, MRs, or epics via the glab CLI."""

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        """Initialize the handler.

        Args:
            config: Used to resolve the default group for epic references
                lacking an explicit group path.
            dry_run: When True, print the command instead of executing it.
        """
        self.config = config
        self.dry_run = dry_run

    def _normalize_issue_ref(self, ref: str) -> Tuple[Optional[str], str]:
        """Parse an issue reference into (encoded_project_path, iid).

        Args:
            ref: Issue reference (#N, N, or a full URL).

        Returns:
            Tuple of (URL-encoded project path or None, numeric iid string).
            project path is None for local refs (#N, plain N).

        Raises:
            ValueError: If the reference cannot be parsed to a numeric iid.
        """
        project_path, iid = parse_issue_url(ref)
        if not iid or not iid.isdigit():
            raise ValueError(f"Cannot parse issue reference: {ref!r}")
        encoded = urllib.parse.quote(project_path, safe="") if project_path else None
        return encoded, iid

    def _normalize_mr_ref(self, ref: str) -> Tuple[Optional[str], str]:
        """Parse an MR reference into (encoded_project_path, iid).

        Args:
            ref: MR reference (!N, N, or a full URL).

        Returns:
            Tuple of (URL-encoded project path or None, numeric iid string).
            project path is None for local refs (!N, plain N).

        Raises:
            ValueError: If the reference cannot be parsed to a numeric iid.
        """
        if ref.startswith("!"):
            ref = ref[1:]
        if "://" in ref:
            if "/-/merge_requests/" in ref:
                parts = ref.split("/-/merge_requests/")
                iid = parts[-1].split("/")[0].split("?")[0].split("#")[0]
                if not iid or not iid.isdigit():
                    raise ValueError(f"Invalid MR reference in URL: {ref!r}")
                raw_path = (
                    parts[0].split("//", 1)[-1].split("/", 1)[-1] if "//" in parts[0] else None
                )
                if raw_path and "/" not in raw_path:
                    raise ValueError(f"Invalid MR URL format (missing project path): {ref!r}")
                encoded = urllib.parse.quote(raw_path, safe="") if raw_path else None
                return encoded, iid
            raise ValueError(f"Invalid MR URL format: {ref!r}")
        if not ref.isdigit():
            raise ValueError(f"Invalid MR reference: {ref!r}")
        return None, ref

    def _post_note(self, endpoint: str, body: str) -> None:
        """Execute the POST API call to create a note.

        Args:
            endpoint: GitLab API path (e.g. 'projects/:fullpath/issues/3/notes').
            body: Note body text.

        Raises:
            ValueError: If the body is empty or whitespace-only.
            PlatformError: If the API call fails or returns non-JSON output.
        """
        if not body.strip():
            raise ValueError("Note body cannot be empty")

        cmd: List[str] = ["api", "-X", "POST", endpoint, "-f", f"body={body}"]

        data = run_glab_json(cmd, dry_run=self.dry_run)
        if data is DRY_RUN:
            return
        if not isinstance(data, dict):
            raise PlatformError(f"Expected a JSON object from the notes API, got: {data!r}")
        note_id = data.get("id", "?")
        noteable_iid = data.get("noteable_iid") or ""
        logger.debug("Note %s created: noteable_iid=%s", note_id, noteable_iid)

    def add_issue_note(self, issue_ref: str, body: str) -> None:
        """Post a note to a GitLab issue.

        Args:
            issue_ref: Issue reference (#N, N, or URL).
            body: Note body text.

        Raises:
            ValueError: If the reference cannot be parsed or body is empty.
            PlatformError: If the API call fails.
        """
        project, iid = self._normalize_issue_ref(issue_ref)
        path = project if project else ":fullpath"
        endpoint = f"projects/{path}/issues/{iid}/notes"
        self._post_note(endpoint, body)
        if not self.dry_run:
            print(f"✓ Note added to issue #{iid}")

    def add_mr_note(self, mr_ref: str, body: str) -> None:
        """Post a note to a GitLab MR.

        Args:
            mr_ref: MR reference (!N, N, or URL).
            body: Note body text.

        Raises:
            ValueError: If the reference cannot be parsed or body is empty.
            PlatformError: If the API call fails.
        """
        project, iid = self._normalize_mr_ref(mr_ref)
        path = project if project else ":fullpath"
        endpoint = f"projects/{path}/merge_requests/{iid}/notes"
        self._post_note(endpoint, body)
        if not self.dry_run:
            print(f"✓ Note added to MR !{iid}")

    def _normalize_epic_ref(self, ref: str) -> Tuple[Optional[str], str]:
        """Parse an epic reference into (group_path, iid).

        Args:
            ref: Epic reference (&N, N, or a full URL).

        Returns:
            Tuple of (group path or None, numeric iid string).
            group path is None for local refs (&N, plain N).

        Raises:
            ValueError: If the reference cannot be parsed to a numeric iid,
                or a URL reference lacks a group path.
        """
        group_path, iid = parse_epic_url(ref)
        if not iid or not iid.isdigit():
            raise ValueError(f"Cannot parse epic reference: {ref!r}")
        if "/-/epics/" in ref and not group_path:
            raise ValueError(f"Invalid epic URL format (missing group path): {ref!r}")
        return group_path, iid

    def _resolve_epic_work_item_id(self, group_path: str, iid: str) -> int:
        """Fetch the work_item_id backing a group epic.

        Args:
            group_path: Un-encoded group path (e.g. 'my/group').
            iid: Epic iid as string.

        Returns:
            Numeric work_item_id from the epic REST response.

        Raises:
            PlatformError: If the API call fails, response is non-JSON, or
                the epic lacks a work_item_id field.
        """
        encoded_group = urllib.parse.quote(group_path, safe="")
        endpoint = f"groups/{encoded_group}/epics/{iid}"
        data = run_glab_json(["api", endpoint])
        if not isinstance(data, dict):
            raise PlatformError(f"Expected a JSON object for epic {iid}, got: {data!r}")
        work_item_id = data.get("work_item_id")
        if not work_item_id:
            raise PlatformError(
                f"Epic &{iid} has no work_item_id; group-epic notes require GitLab 15.9+ "
                "with epics migrated to work items"
            )
        return int(work_item_id)

    def _post_epic_note_graphql(self, work_item_id: int, body: str) -> None:
        """Post a note to an epic via the GraphQL createNote mutation.

        GitLab's REST group-epic notes endpoint returns 404 on instances where
        epics have been unified under work items (15.9+); createNote against a
        WorkItem GID is the reliable path.

        Args:
            work_item_id: Numeric work_item_id backing the epic.
            body: Note body text (already validated non-empty).

        Raises:
            PlatformError: If the API call fails, response is non-JSON, or
                the mutation reports errors.
        """
        wi_gid = f"gid://gitlab/WorkItem/{work_item_id}"
        # GraphQL string literals forbid raw LineTerminators and other control
        # chars; json.dumps produces the required escapes (its output is a
        # strict superset of GraphQL's string-literal grammar).
        body_literal = json.dumps(body)
        query = (
            "mutation { createNote(input: { "
            f'noteableId: "{wi_gid}", body: {body_literal} '
            "}) { note { id } errors } }"
        )
        cmd = ["api", "graphql", "-f", f"query={query}"]

        resp = run_glab_json(cmd, dry_run=self.dry_run)
        if resp is DRY_RUN:
            return
        if not isinstance(resp, dict):
            raise PlatformError(f"Expected a JSON object from GraphQL, got: {resp!r}")
        payload = resp.get("data", {}).get("createNote", {}) or {}
        errors = payload.get("errors") or []
        if errors:
            raise PlatformError(f"GraphQL createNote failed: {errors}")
        note = payload.get("note") or {}
        logger.debug("Epic note created: id=%s", note.get("id", "?"))

    def add_epic_note(self, epic_ref: str, body: str) -> None:
        """Post a note to a GitLab group epic.

        Args:
            epic_ref: Epic reference (&N, N, or URL).
            body: Note body text.

        Raises:
            ValueError: If the reference cannot be parsed, body is empty, or
                no default group is configured for a local (&N/plain) ref.
            PlatformError: If the API call fails.
        """
        if not body.strip():
            raise ValueError("Note body cannot be empty")
        group_path, iid = self._normalize_epic_ref(epic_ref)
        if not group_path:
            group_path = self.config.get_default_group()
            if not group_path:
                raise ValueError(
                    "Group path is required to post a note to an epic.\n"
                    "Either include the group in the epic URL or set 'default_group' in your config."
                )
        work_item_id = self._resolve_epic_work_item_id(group_path, iid)
        self._post_epic_note_graphql(work_item_id, body)
        if not self.dry_run:
            print(f"✓ Note added to epic &{iid}")
