"""Merge handler for merging GitLab merge requests, including stacked chains."""

import logging
import time
import urllib.parse
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import Config
from ..exceptions import PlatformError
from ..utils.git_helpers import parse_mr_url
from ..utils.glab_runner import run_glab_json, run_glab_json_pages

logger = logging.getLogger(__name__)

# GitLab caps per_page at 100. A heavily reviewed MR exceeds one page, and a
# truncated list would let the unresolved-thread gate pass an MR that has open
# threads beyond the first 100.
_PER_PAGE = 100

# GitLab recomputes merge_status asynchronously after a push or a retarget, and
# reports "checking" until it settles. Merging is refused in that state, so the
# gate polls rather than treating it as a hard failure.
_MERGE_STATUS_SETTLE_TRIES = 10
_MERGE_STATUS_SETTLE_DELAY_S = 3.0

# detailed_merge_status values that mean "ask again shortly" rather than "no".
# A retarget puts an MR through several of these before it settles, and treating
# any of them as a refusal would abort a chain that was about to become
# mergeable. ci_still_running is transient; ci_must_pass is not — it means the
# pipeline finished without success, or none ran against the current target.
_TRANSIENT_MERGE_STATUSES = frozenset(
    {"checking", "unchecked", "preparing", "ci_still_running"}
)

# GitLab sets state "locked" while a merge is in flight. Observed on !266: a
# merge already underway made the state gate report "not opened" and the run
# fail, while the merge itself completed. Treat it as in-progress, not as a
# refusal.
_TRANSIENT_MR_STATES = frozenset({"locked"})

# After a stacked MR's target merges, GitLab retargets its children onto the
# grandparent. That write is asynchronous, so a chain merge must wait for it
# rather than merging a child that still names a branch which no longer exists.
_RETARGET_TRIES = 20
_RETARGET_DELAY_S = 3.0

# A server-side rebase is asynchronous; GitLab sets rebase_in_progress and
# clears it when done, recording any failure in merge_error.
_REBASE_TRIES = 40
_REBASE_DELAY_S = 3.0

# After a rebase the branch has a new SHA, so a project that requires a green
# pipeline needs a fresh one before the merge is allowed.
_PIPELINE_WAIT_TRIES = 120
_PIPELINE_WAIT_DELAY_S = 15.0

# Immediately after a retarget GitLab reports detailed_merge_status "mergeable"
# while still refusing the merge itself with 405/422. Observed on !263 and !265:
# the gate passed, the PUT was rejected, and the same MR merged cleanly seconds
# later untouched. Re-gate and retry rather than failing a merge that is only
# not-yet-ready.
_MERGE_RETRY_TRIES = 6
_MERGE_RETRY_DELAY_S = 10.0
_TRANSIENT_MERGE_REJECTIONS = ("405", "422")


class MergeBlocked(Exception):
    """Raised when an MR fails a pre-merge gate.

    Distinct from PlatformError: the API call succeeded and told us the MR is
    not mergeable. Callers report this without a stack trace.
    """


class MergeHandler:
    """Merges merge requests via the glab CLI, with pre-merge gates."""

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        """Initialize the handler.

        Args:
            config: Retained for interface parity with other handlers; MR
                operations resolve project scope from the git remote.
            dry_run: When True, run every gate and report the decision without
                issuing the merge call.
        """
        self.config = config
        self.dry_run = dry_run
        self._merge_method: Optional[str] = None
        # Separate from the value: None is a real answer ("could not read it"),
        # and re-asking on every gate would cost an API call per MR.
        self._merge_method_known = False
        self.rebase_between = False
        self.wait_pipeline = False

    # -- reference and endpoint helpers ------------------------------------

    @staticmethod
    def _endpoint(mr_ref: str) -> Tuple[str, str]:
        """Build the merge_requests endpoint base for an MR reference.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            Tuple of (endpoint base path, numeric iid string).

        Raises:
            ValueError: If the reference does not resolve to a numeric iid.
        """
        project, iid = parse_mr_url(mr_ref)
        # parse_mr_url is lenient by contract and returns "x" for "!x". Merging
        # is irreversible, so a malformed reference must fail here rather than
        # become a request path.
        if not iid or not iid.isdigit():
            raise ValueError(f"Invalid MR reference: {mr_ref!r}")
        path = urllib.parse.quote(project, safe="") if project else ":fullpath"
        return f"projects/{path}/merge_requests/{iid}", iid

    def fetch(self, mr_ref: str) -> Dict:
        """Fetch an MR's current state.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            MR dict as returned by the GitLab API.

        Raises:
            PlatformError: If the API call fails or returns non-JSON output.
        """
        endpoint, _ = self._endpoint(mr_ref)
        data = run_glab_json(["api", endpoint])
        if not isinstance(data, dict):
            raise PlatformError(f"Unexpected API response for {mr_ref}: {data!r}")
        return data

    def _unresolved_threads(self, mr_ref: str) -> int:
        """Count resolvable discussion threads that are not yet resolved.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            Number of unresolved resolvable threads.
        """
        endpoint, _ = self._endpoint(mr_ref)
        discussions = run_glab_json_pages(
            ["api", "--paginate", f"{endpoint}/discussions?per_page={_PER_PAGE}"]
        )
        unresolved = 0
        for discussion in discussions:
            if discussion.get("individual_note"):
                continue
            notes = [n for n in discussion.get("notes") or [] if n.get("resolvable")]
            if notes and not all(n.get("resolved") for n in notes):
                unresolved += 1
        return unresolved

    # -- gates --------------------------------------------------------------

    def _await_merge_status(self, mr_ref: str, mr: Dict) -> Dict:
        """Poll until GitLab finishes recomputing the merge status.

        Args:
            mr_ref: MR reference, used to refetch.
            mr: The MR dict already fetched.

        Returns:
            The most recent MR dict; still transient if it never settled.
        """
        for _ in range(_MERGE_STATUS_SETTLE_TRIES):
            status = mr.get("detailed_merge_status") or mr.get("merge_status")
            if status not in _TRANSIENT_MERGE_STATUSES:
                return mr
            time.sleep(_MERGE_STATUS_SETTLE_DELAY_S)
            mr = self.fetch(mr_ref)
        return mr

    def project_merge_method(self) -> Optional[str]:
        """Return the project's merge method ("merge", "rebase_merge", or "ff").

        Returns:
            The configured method, or None when it cannot be read.
        """
        if not self._merge_method_known:
            try:
                project = run_glab_json(["api", "projects/:fullpath"])
                method = project.get("merge_method") if isinstance(project, dict) else None
            except PlatformError:
                method = None
            self._merge_method = str(method) if method is not None else None
            self._merge_method_known = True
        return self._merge_method

    def diverged_commits(self, mr_ref: str) -> int:
        """Count commits the target has that this MR's branch does not.

        GitLab only populates this when explicitly asked, and it is the field
        that decides whether a fast-forward is possible.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            Number of diverged commits; 0 when unknown.
        """
        endpoint, _ = self._endpoint(mr_ref)
        try:
            data = run_glab_json(
                ["api", f"{endpoint}?include_diverged_commits_count=true"]
            )
        except PlatformError:
            return 0
        if not isinstance(data, dict):
            return 0
        return int(data.get("diverged_commits_count") or 0)

    def failed_allowed_jobs(self, mr: Dict) -> List[str]:
        """Name the head pipeline's failed jobs that carry allow_failure.

        A pipeline whose only failures are `allow_failure: true` reports
        `success`, so the pipeline gate passes it. That is usually intended, but
        it means a green rollup can hide a red job — worth showing rather than
        silently trusting.

        Args:
            mr: MR dict from the API.

        Returns:
            Job names that failed but were allowed to. Empty when the pipeline
            is absent or nothing failed.
        """
        pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
        pipeline_id = pipeline.get("id")
        if not pipeline_id:
            return []
        try:
            jobs = run_glab_json_pages(
                ["api", "--paginate", f"projects/:fullpath/pipelines/{pipeline_id}/jobs"]
            )
        except PlatformError:
            # Diagnostic only — never let it break a gate that already passed.
            return []
        return [
            j.get("name", "?")
            for j in jobs
            if j.get("status") == "failed" and j.get("allow_failure")
        ]

    def evaluate(
        self,
        mr_ref: str,
        allow_unresolved: bool = False,
        allow_failed_pipeline: bool = False,
    ) -> Tuple[Dict, List[Tuple[str, bool, str]]]:
        """Run every pre-merge gate without raising.

        Args:
            mr_ref: MR reference (!N, N, or URL).
            allow_unresolved: Treat the unresolved-thread gate as waived.
            allow_failed_pipeline: Treat the pipeline gate as waived.

        Returns:
            Tuple of (MR dict, list of (gate name, passed, detail)). Detail
            carries the blocking message when a gate fails, or a short state
            summary when it passes.

        Raises:
            PlatformError: If an API call fails.
        """
        mr = self._await_merge_status(mr_ref, self.fetch(mr_ref))
        iid = mr.get("iid", "?")
        gates: List[Tuple[str, bool, str]] = []

        state = mr.get("state")
        if state in _TRANSIENT_MR_STATES:
            detail = f"MR !{iid} is {state} — a merge is already in flight; re-check shortly"
        elif state != "opened":
            detail = f"MR !{iid} is {state}, not opened"
        else:
            detail = "opened"
        gates.append(("state", state == "opened", detail))

        is_draft = bool(mr.get("draft") or mr.get("work_in_progress"))
        gates.append(("draft", not is_draft,
                      f"MR !{iid} is a draft; mark it ready first" if is_draft else "ready"))

        # detailed_merge_status is authoritative; the legacy merge_status field
        # reports can_be_merged for an MR GitLab will refuse with HTTP 405 —
        # ci_must_pass after a retarget is the case that bites, because the old
        # pipeline ran against the old target.
        detailed = mr.get("detailed_merge_status")
        status = detailed or mr.get("merge_status")
        ok_values = {"mergeable", "can_be_merged"}
        if status in _TRANSIENT_MERGE_STATUSES:
            detail = (
                f"MR !{iid} merge status is still '{status}' after "
                f"{_MERGE_STATUS_SETTLE_TRIES} polls; retry shortly"
            )
        elif status not in ok_values:
            detail = f"MR !{iid} is not mergeable ({status})"
        else:
            detail = str(status)
        gates.append(("mergeable", status in ok_values, detail))

        open_threads = self._unresolved_threads(mr_ref)
        thread_ok = allow_unresolved or open_threads == 0
        gates.append((
            "threads",
            thread_ok,
            f"MR !{iid} has {open_threads} unresolved thread(s); "
            "resolve them or pass --allow-unresolved"
            if not thread_ok else f"{open_threads} unresolved",
        ))

        # Under fast-forward-only merging the branch must be a direct descendant
        # of its target. A squashing project rewrites each merged commit, so a
        # stacked MR stops being a descendant the moment its parent lands — and
        # GitLab still reports detailed_merge_status "mergeable", then refuses
        # the PUT with 422. Observed on !265 after !263 merged.
        if self.project_merge_method() == "ff":
            behind = self.diverged_commits(mr_ref)
            gates.append((
                "ff-ready",
                behind == 0,
                f"MR !{iid} is {behind} commit(s) behind {mr.get('target_branch')} and the "
                "project is fast-forward only; rebase it (merge --rebase)"
                if behind else "descendant of target",
            ))

        pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
        p_status = pipeline.get("status")
        # A missing pipeline is not a failure: docs-only branches and projects
        # without CI legitimately have none.
        pipeline_ok = allow_failed_pipeline or p_status is None or p_status == "success"
        gates.append((
            "pipeline",
            pipeline_ok,
            f"MR !{iid} head pipeline is '{p_status}'; "
            "pass --allow-failed-pipeline to merge anyway"
            if not pipeline_ok else str(p_status or "none"),
        ))

        return mr, gates

    def check(
        self,
        mr_ref: str,
        allow_unresolved: bool = False,
        allow_failed_pipeline: bool = False,
    ) -> Dict:
        """Run every pre-merge gate and return the MR state.

        Args:
            mr_ref: MR reference (!N, N, or URL).
            allow_unresolved: Permit merging with unresolved discussion threads.
            allow_failed_pipeline: Permit merging when the head pipeline is not
                successful.

        Returns:
            The MR dict, once every gate passes.

        Raises:
            MergeBlocked: If any gate fails.
            PlatformError: If an API call fails.
        """
        mr, gates = self.evaluate(mr_ref, allow_unresolved, allow_failed_pipeline)
        for _name, ok, detail in gates:
            if not ok:
                raise MergeBlocked(detail)
        return mr

    def report(
        self,
        mr_refs: Sequence[str],
        allow_unresolved: bool = False,
        allow_failed_pipeline: bool = False,
    ) -> int:
        """Print each MR's mergeability without merging anything.

        Unlike a chain merge, this does not stop at the first blockage — the
        point is to see every reason at once.

        Args:
            mr_refs: MR references to inspect.
            allow_unresolved: Treat the unresolved-thread gate as waived.
            allow_failed_pipeline: Treat the pipeline gate as waived.

        Returns:
            Process exit code: 0 when every MR can merge, 1 otherwise.
        """
        blocked = 0
        for ref in mr_refs:
            try:
                mr, gates = self.evaluate(ref, allow_unresolved, allow_failed_pipeline)
            except (PlatformError, ValueError) as err:
                print(f"✗ {ref}: {err}\n")
                blocked += 1
                continue

            iid = mr.get("iid", "?")
            failures = [(n, d) for n, ok, d in gates if not ok]
            mark = "✓" if not failures else "✗"
            print(f"{mark} MR !{iid}: {mr.get('source_branch')} -> {mr.get('target_branch')}")
            for name, ok, detail in gates:
                print(f"    {'ok  ' if ok else 'BLOCK'} {name:<10} {detail}")

            masked = self.failed_allowed_jobs(mr)
            if masked:
                # Not a gate: these jobs are configured to fail without failing
                # the pipeline. Surfaced so a green rollup is not mistaken for a
                # green run.
                print(f"    note  masked    failed but allow_failure: {', '.join(masked)}")
            print()
            if failures:
                blocked += 1

        total = len(mr_refs)
        print(f"{total - blocked} of {total} MR(s) can merge")
        return 1 if blocked else 0

    # -- merge --------------------------------------------------------------

    def merge_one(
        self,
        mr_ref: str,
        *,
        allow_unresolved: bool = False,
        allow_failed_pipeline: bool = False,
        remove_branch: bool = True,
        squash: bool = False,
    ) -> Dict:
        """Merge a single MR after its gates pass.

        Args:
            mr_ref: MR reference (!N, N, or URL).
            allow_unresolved: Permit unresolved discussion threads.
            allow_failed_pipeline: Permit a non-successful head pipeline.
            remove_branch: Delete the source branch after merging.
            squash: Squash commits on merge.

        Returns:
            The merged MR dict, or the pre-merge dict under dry_run.

        Raises:
            MergeBlocked: If a gate fails.
            PlatformError: If the merge call fails.
        """
        if self.rebase_between and not self.dry_run:
            # Gate after any rebase: rebasing changes the SHA, so the pre-rebase
            # pipeline and divergence readings are both stale.
            rebased = self.rebase_if_behind(mr_ref)
        else:
            rebased = False

        if self.wait_pipeline and not self.dry_run and not rebased:
            # rebase_if_behind already waited; do not pay for it twice.
            status = self.await_pipeline(mr_ref)
            print(f"  {mr_ref} pipeline: {status}")

        mr = self.check(mr_ref, allow_unresolved, allow_failed_pipeline)
        endpoint, iid = self._endpoint(mr_ref)
        source, target = mr.get("source_branch"), mr.get("target_branch")

        if self.dry_run:
            print(f"[dry-run] Would merge MR !{iid}: {source} -> {target}")
            return mr

        cmd = [
            "api",
            "-X",
            "PUT",
            f"{endpoint}/merge",
            "-f",
            f"should_remove_source_branch={str(remove_branch).lower()}",
            "-f",
            f"squash={str(squash).lower()}",
        ]
        last_error: Optional[PlatformError] = None
        for attempt in range(_MERGE_RETRY_TRIES):
            try:
                merged = run_glab_json(cmd)
            except PlatformError as err:
                if not any(code in str(err) for code in _TRANSIENT_MERGE_REJECTIONS):
                    raise
                last_error = err
                if attempt + 1 == _MERGE_RETRY_TRIES:
                    break
                time.sleep(_MERGE_RETRY_DELAY_S)
                # Re-gate before retrying: if the refusal was real rather than a
                # race, check() names the actual reason instead of looping.
                self.check(mr_ref, allow_unresolved, allow_failed_pipeline)
                continue
            if not isinstance(merged, dict):
                raise PlatformError(f"Unexpected merge response for !{iid}: {merged!r}")
            print(f"✓ Merged MR !{iid}: {source} -> {target}")
            return merged

        raise PlatformError(
            f"MR !{iid} refused the merge {_MERGE_RETRY_TRIES} times while reporting "
            f"itself mergeable; last error: {last_error}"
        )

    def rebase(self, mr_ref: str) -> None:
        """Rebase an MR's branch onto its target, server-side.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Raises:
            PlatformError: If the rebase call fails, times out, or GitLab
                records a merge_error afterwards.
        """
        endpoint, iid = self._endpoint(mr_ref)
        if self.dry_run:
            print(f"[dry-run] Would rebase MR !{iid}")
            return

        run_glab_json(["api", "-X", "PUT", f"{endpoint}/rebase"])
        for _ in range(_REBASE_TRIES):
            mr = run_glab_json(["api", f"{endpoint}?include_rebase_in_progress=true"])
            if not isinstance(mr, dict):
                break
            if not mr.get("rebase_in_progress"):
                error = mr.get("merge_error")
                if error:
                    raise PlatformError(f"Rebase of !{iid} failed: {error}")
                print(f"  rebased !{iid} onto {mr.get('target_branch')}")
                return
            time.sleep(_REBASE_DELAY_S)
        raise PlatformError(f"Rebase of !{iid} did not finish in time")

    def rebase_if_behind(self, mr_ref: str) -> bool:
        """Rebase an MR only when a fast-forward merge would be refused.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            True when a rebase was performed.

        Raises:
            PlatformError: If the rebase fails.
        """
        if self.project_merge_method() != "ff":
            return False
        if self.diverged_commits(mr_ref) == 0:
            return False
        self.rebase(mr_ref)
        status = self.await_pipeline(mr_ref)
        print(f"  {mr_ref} pipeline after rebase: {status}")
        return True

    def _fetch_tolerantly(self, mr_ref: str) -> Optional[Dict]:
        """Fetch an MR, returning None instead of raising on a transport error.

        Long poll loops run for tens of minutes over a VPN. A single timeout
        mid-wait is not a reason to abandon a merge that is otherwise on track;
        the loop's own budget is what bounds the wait.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            The MR dict, or None when this attempt could not reach GitLab.
        """
        try:
            return self.fetch(mr_ref)
        except PlatformError as err:
            logger.warning("Transient error polling %s: %s", mr_ref, err)
            return None

    def await_pipeline(self, mr_ref: str) -> str:
        """Wait for the MR's head pipeline to reach a terminal status.

        A rebase gives the branch a new SHA, so a project that gates merging on
        a green pipeline needs the new one to finish first.

        Args:
            mr_ref: MR reference (!N, N, or URL).

        Returns:
            The terminal pipeline status, "none" when no pipeline appeared, or
            the last status seen if the budget ran out.
        """
        terminal = {"success", "failed", "canceled", "cancelled", "skipped", "manual"}
        status = "none"
        for _ in range(_PIPELINE_WAIT_TRIES):
            mr = self._fetch_tolerantly(mr_ref)
            if mr is None:
                time.sleep(_PIPELINE_WAIT_DELAY_S)
                continue
            pipeline = mr.get("head_pipeline") or {}
            status = pipeline.get("status") or "none"
            # A rebase leaves the previous pipeline attached for a moment. Judging
            # its status would report the pre-rebase result as if it were the new
            # one — which is how a merge then hit 'ci_still_running' immediately
            # after this returned "success".
            head_sha = mr.get("sha")
            stale = bool(head_sha) and pipeline.get("sha") not in (None, head_sha)
            if status in terminal and not stale:
                return status
            time.sleep(_PIPELINE_WAIT_DELAY_S)
        return status

    def _await_retarget(self, mr_ref: str, gone_branch: str) -> Optional[str]:
        """Wait for GitLab to retarget a child MR off a merged branch.

        When a stacked MR's target merges, GitLab moves its children onto the
        grandparent. That write is asynchronous, so a chain merge that does not
        wait would gate the child against a branch that no longer exists.

        Args:
            mr_ref: The child MR reference.
            gone_branch: The branch that was just merged and deleted.

        Returns:
            The child's new target branch, or None if it never moved.
        """
        for _ in range(_RETARGET_TRIES):
            mr = self._fetch_tolerantly(mr_ref)
            target = mr.get("target_branch") if mr else gone_branch
            if target != gone_branch:
                return target
            time.sleep(_RETARGET_DELAY_S)
        return None

    def merge_chain(
        self,
        mr_refs: Sequence[str],
        *,
        allow_unresolved: bool = False,
        allow_failed_pipeline: bool = False,
        remove_branch: bool = True,
        squash: bool = False,
    ) -> int:
        """Merge a stack of MRs in the given order, bottom-up.

        After each merge, waits for GitLab to retarget the next MR before
        gating it, so a child is never evaluated against a deleted branch.

        Args:
            mr_refs: MR references in merge order, base first.
            allow_unresolved: Permit unresolved discussion threads.
            allow_failed_pipeline: Permit a non-successful head pipeline.
            remove_branch: Delete each source branch after merging.
            squash: Squash commits on merge.

        Returns:
            Process exit code: 0 if every MR merged, 1 on the first blockage.
        """
        if self.dry_run:
            # A dry run has nothing to protect from a wrong base, so it reports
            # every MR instead of stopping at the first blockage.
            return self.report(mr_refs, allow_unresolved, allow_failed_pipeline)

        merged_count = 0
        for index, ref in enumerate(mr_refs):
            try:
                mr = self.merge_one(
                    ref,
                    allow_unresolved=allow_unresolved,
                    allow_failed_pipeline=allow_failed_pipeline,
                    remove_branch=remove_branch,
                    squash=squash,
                )
            except (MergeBlocked, PlatformError, ValueError) as err:
                # Stop rather than continue: every later MR in a stack targets a
                # branch this one was supposed to move, so merging on would
                # either fail the same way or merge into the wrong base.
                print(f"✗ Stopped at {ref}: {err}")
                print(f"  {merged_count} of {len(mr_refs)} merged.")
                return 1

            merged_count += 1
            remaining = mr_refs[index + 1:]
            if not remaining:
                continue

            source = str(mr.get("source_branch") or "")
            child_target = self.fetch(remaining[0]).get("target_branch")
            # Only a child stacked on the branch just merged needs retargeting.
            # Independent MRs merged in one run never move, and waiting for a
            # move that will not happen would report a retarget that never
            # occurred — or stall the run for the full retarget budget.
            if source and child_target == source:
                new_target = self._await_retarget(remaining[0], source)
                if new_target is None:
                    print(
                        f"✗ Stopped: {remaining[0]} still targets {source!r}, which was just "
                        f"merged and removed. Retarget it, then merge the rest."
                    )
                    print(f"  {merged_count} of {len(mr_refs)} merged.")
                    return 1
                print(f"  {remaining[0]} retargeted onto {new_target}")

            if self.rebase_between:
                # Under fast-forward merging the child stopped being a
                # descendant the moment this parent landed.
                self.rebase_if_behind(remaining[0])

        print(f"\n✓ Merged {merged_count} of {len(mr_refs)} MR(s)")
        return 0


def cmd_merge(args, config: Config) -> int:
    """Handle the 'merge' subcommand.

    Args:
        args: Parsed command-line arguments.
        config: Loaded project configuration.

    Returns:
        Process exit code.
    """
    handler = MergeHandler(config, dry_run=args.dry_run)
    handler.rebase_between = getattr(args, "rebase", False)
    handler.wait_pipeline = getattr(args, "wait", False)
    refs: List[str] = list(args.mr)
    try:
        if args.dry_run:
            return handler.report(
                refs,
                allow_unresolved=args.allow_unresolved,
                allow_failed_pipeline=args.allow_failed_pipeline,
            )
        if len(refs) == 1:
            handler.merge_one(
                refs[0],
                allow_unresolved=args.allow_unresolved,
                allow_failed_pipeline=args.allow_failed_pipeline,
                remove_branch=not args.keep_branch,
                squash=args.squash,
            )
            return 0
        return handler.merge_chain(
            refs,
            allow_unresolved=args.allow_unresolved,
            allow_failed_pipeline=args.allow_failed_pipeline,
            remove_branch=not args.keep_branch,
            squash=args.squash,
        )
    except (MergeBlocked, ValueError) as err:
        print(f"✗ {err}")
        return 1
    except PlatformError as err:
        logger.error("Merge failed: %s", err)
        return 1
