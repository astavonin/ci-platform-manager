"""Note (comment) handler for posting notes to GitLab issues and MRs."""

import json
import logging
import shlex
import urllib.parse
from typing import List, Optional, Tuple

from ..config import Config
from ..exceptions import PlatformError
from ..utils.git_helpers import parse_issue_url
from ..utils.glab_runner import run_glab_command

logger = logging.getLogger(__name__)


class NoteHandler:
    """Posts notes to GitLab issues or MRs via the glab CLI."""

    def __init__(self, config: Config, dry_run: bool = False) -> None:  # pylint: disable=unused-argument
        """Initialize the handler.

        Args:
            config: Accepted for handler-protocol uniformity; platform dispatch
                is handled by the CLI before this handler is constructed.
            dry_run: When True, print the command instead of executing it.
        """
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
                raw_path = parts[0].split("//", 1)[-1].split("/", 1)[-1] if "//" in parts[0] else None
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

        if self.dry_run:
            print(f"[dry-run] {shlex.join(['glab', *cmd])}")
            return

        result = run_glab_command(cmd)
        try:
            data = json.loads(result)
        except json.JSONDecodeError as exc:
            raise PlatformError(f"Unexpected glab response: {result[:200]!r}") from exc
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
