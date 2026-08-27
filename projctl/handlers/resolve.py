"""Resolve handler for closing MR review discussion threads on GitLab."""

import logging
import urllib.parse
from typing import Dict, List, Sequence, Tuple

from ..config import Config
from ..exceptions import PlatformError
from ..utils.git_helpers import parse_mr_url
from ..utils.glab_runner import (
    discussion_resolve_endpoint,
    run_glab_json,
    run_glab_json_pages,
)

logger = logging.getLogger(__name__)

# GitLab caps per_page at 100; discussions on a heavily reviewed MR exceed one
# page, and a truncated list would silently make --match miss its target.
_PER_PAGE = 100


class ResolveHandler:
    """Lists and resolves MR discussion threads via the glab CLI."""

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        """Initialize the handler.

        Args:
            config: Retained for interface parity with other handlers; thread
                operations resolve project scope from the git remote.
            dry_run: When True, print the command instead of executing it.
        """
        self.config = config
        self.dry_run = dry_run

    def _endpoint_base(self, mr_ref: str) -> Tuple[str, str]:
        """Build the discussions endpoint base for an MR reference.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            Tuple of (endpoint base path, numeric iid string).

        Raises:
            ValueError: If the reference does not resolve to a numeric iid.
        """
        project, iid = parse_mr_url(mr_ref)
        # parse_mr_url is lenient by contract: it returns (None, None) for an
        # unrecognised reference and does not check that the iid is numeric, so
        # "!x" comes back as "x". Resolving mutates review state, so a malformed
        # reference must fail here rather than become a request path.
        if not iid or not iid.isdigit():
            raise ValueError(f"Invalid MR reference: {mr_ref!r}")
        # It also returns the project path unencoded; the API needs it as a
        # single path segment.
        path = urllib.parse.quote(project, safe="") if project else ":fullpath"
        return f"projects/{path}/merge_requests/{iid}/discussions", iid

    def fetch_discussions(self, mr_ref: str) -> List[Dict]:
        """Fetch every discussion on an MR, following pagination.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            List of discussion dicts as returned by the GitLab API.

        Raises:
            PlatformError: If the API call fails or returns non-JSON output.
        """
        endpoint, _ = self._endpoint_base(mr_ref)
        return run_glab_json_pages(["api", "--paginate", f"{endpoint}?per_page={_PER_PAGE}"])

    @staticmethod
    def _first_note(discussion: Dict) -> Dict:
        """Return the first note of a discussion, or an empty dict."""
        notes = discussion.get("notes") or []
        return notes[0] if notes else {}

    @staticmethod
    def is_resolvable(discussion: Dict) -> bool:
        """Report whether a discussion supports resolution.

        Standalone comments (`individual_note`) and system notes are not
        resolvable; attempting to resolve one returns HTTP 400.

        Args:
            discussion: Discussion dict from the GitLab API.

        Returns:
            True when at least one note is resolvable.
        """
        if discussion.get("individual_note"):
            return False
        return any(n.get("resolvable") for n in discussion.get("notes") or [])

    @staticmethod
    def is_resolved(discussion: Dict) -> bool:
        """Report whether every resolvable note in a discussion is resolved.

        Args:
            discussion: Discussion dict from the GitLab API.

        Returns:
            True when all resolvable notes are already resolved.
        """
        resolvable = [n for n in discussion.get("notes") or [] if n.get("resolvable")]
        return bool(resolvable) and all(n.get("resolved") for n in resolvable)

    @staticmethod
    def describe(discussion: Dict) -> str:
        """Render a one-line summary of a discussion for listing output.

        Args:
            discussion: Discussion dict from the GitLab API.

        Returns:
            Single-line summary: state, location, author, and body excerpt.
        """
        note = ResolveHandler._first_note(discussion)
        pos = note.get("position") or {}
        loc = pos.get("new_path") or pos.get("old_path") or ""
        line = pos.get("new_line") or pos.get("old_line")
        where = f"{loc}:{line}" if loc and line else (loc or "(no position)")
        author = (note.get("author") or {}).get("name", "?")
        body = " ".join((note.get("body") or "").split())
        if len(body) > 70:
            body = body[:67] + "..."
        if not ResolveHandler.is_resolvable(discussion):
            state = "n/a "
        else:
            state = "done" if ResolveHandler.is_resolved(discussion) else "open"
        return f"[{state}] {discussion.get('id', '?')[:12]}  {where:<44}  {author:<16}  {body}"

    def list_discussions(self, mr_ref: str) -> int:
        """Print every discussion on an MR with its id and resolution state.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            Process exit code (0).
        """
        discussions = self.fetch_discussions(mr_ref)
        _, iid = self._endpoint_base(mr_ref)
        resolvable = [d for d in discussions if self.is_resolvable(d)]
        open_count = sum(1 for d in resolvable if not self.is_resolved(d))
        print(f"=== Discussions on MR !{iid} ===\n")
        for d in discussions:
            print(self.describe(d))
        print(
            f"\n{len(discussions)} discussion(s), {len(resolvable)} resolvable, "
            f"{open_count} unresolved"
        )
        return 0

    def _select(
        self, discussions: Sequence[Dict], discussion_ids: Sequence[str], matches: Sequence[str]
    ) -> List[Dict]:
        """Resolve selector arguments to the discussions they name.

        A `--match` selector that hits zero or several threads is an error
        rather than a no-op or a batch: resolving the wrong thread silently
        marks a finding as handled, which is the failure this guard exists for.

        Args:
            discussions: All discussions on the MR.
            discussion_ids: Explicit discussion ids (full or unique prefix).
            matches: Substrings to match against each thread's first note.

        Returns:
            Selected discussion dicts, in selector order, de-duplicated.

        Raises:
            ValueError: If a selector matches no thread or is ambiguous.
        """
        by_id = {d["id"]: d for d in discussions if d.get("id")}
        selected: List[Dict] = []
        seen: set = set()

        for did in discussion_ids:
            hits = [d for i, d in by_id.items() if i == did or i.startswith(did)]
            if not hits:
                raise ValueError(f"No discussion with id {did!r} on this MR")
            if len(hits) > 1:
                raise ValueError(
                    f"Discussion id prefix {did!r} is ambiguous ({len(hits)} matches); "
                    "use the full id"
                )
            if hits[0]["id"] not in seen:
                seen.add(hits[0]["id"])
                selected.append(hits[0])

        for text in matches:
            hits = [
                d
                for d in discussions
                if d.get("id")
                and self.is_resolvable(d)
                and text in (self._first_note(d).get("body") or "")
            ]
            if not hits:
                raise ValueError(f"No resolvable thread whose first note contains {text!r}")
            if len(hits) > 1:
                where = ", ".join(d.get("id", "?")[:12] for d in hits)
                raise ValueError(
                    f"{text!r} matches {len(hits)} threads ({where}); use a longer substring "
                    "or --discussion"
                )
            if hits[0]["id"] not in seen:
                seen.add(hits[0]["id"])
                selected.append(hits[0])

        return selected

    def _set_resolved(self, endpoint: str, discussion_id: str, resolved: bool) -> None:
        """Execute the PUT API call that sets a discussion's resolved state.

        Args:
            endpoint: Discussions endpoint base for the MR.
            discussion_id: Full discussion id.
            resolved: Target state.

        Raises:
            PlatformError: If the API call fails or returns non-JSON output.
        """
        cmd: List[str] = [
            "api",
            "-X",
            "PUT",
            discussion_resolve_endpoint(endpoint, discussion_id, resolved),
        ]
        run_glab_json(cmd, dry_run=self.dry_run)
        logger.info("Set resolved=%s on discussion %s", resolved, discussion_id[:12])

    def set_resolution(
        self,
        mr_ref: str,
        discussion_ids: Sequence[str],
        matches: Sequence[str],
        resolved: bool = True,
    ) -> int:
        """Resolve (or unresolve) the selected discussion threads.

        Args:
            mr_ref: MR reference (!N, N, or URL).
            discussion_ids: Explicit discussion ids (full or unique prefix).
            matches: Substrings matched against each thread's first note.
            resolved: Target state; False unresolves.

        Returns:
            Process exit code: 0 when every selected thread reached the target
            state or was already there, 1 when any explicitly named thread was
            unresolvable or its API call failed.

        Raises:
            ValueError: If no selector is given, or a selector misses or is
                ambiguous.
            PlatformError: If fetching the discussion list fails.
        """
        if not discussion_ids and not matches:
            raise ValueError("Nothing selected: pass --discussion and/or --match")

        endpoint, iid = self._endpoint_base(mr_ref)
        discussions = self.fetch_discussions(mr_ref)
        selected = self._select(discussions, discussion_ids, matches)

        verb = "resolved" if resolved else "unresolved"
        changed = skipped = failed = unresolvable = 0
        for discussion in selected:
            did = discussion["id"]
            summary = " ".join((self._first_note(discussion).get("body") or "").split())[:60]
            if not self.is_resolvable(discussion):
                print(f"- skipped {did[:12]} (not resolvable): {summary}")
                skipped += 1
                unresolvable += 1
                continue
            if self.is_resolved(discussion) == resolved:
                print(f"- skipped {did[:12]} (already {'resolved' if resolved else 'open'})")
                skipped += 1
                continue
            try:
                self._set_resolved(endpoint, did, resolved)
            except PlatformError as err:
                # Keep going and report at the end: aborting mid-loop would hide
                # which threads the run had already changed on the server.
                print(f"- FAILED {did[:12]}: {err}")
                failed += 1
                continue
            if not self.dry_run:
                print(f"✓ {verb} {did[:12]}: {summary}")
            changed += 1

        prefix = "[dry-run] Would have " if self.dry_run else ""
        tail = f", {failed} failed" if failed else ""
        print(f"\n{prefix}{verb} {changed} thread(s) on MR !{iid}, {skipped} skipped{tail}")

        # An explicitly named thread that cannot be resolved is not a no-op the
        # caller should read as success — it asked for something that did not
        # happen. An already-resolved skip is a genuine no-op and stays 0.
        return 1 if failed or unresolvable else 0
