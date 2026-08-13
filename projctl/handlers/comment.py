"""Comment/review posting handler."""

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
except ImportError as exc:
    raise ImportError("PyYAML is required. Install with: pip install PyYAML") from exc


logger = logging.getLogger(__name__)

VALID_APPROVAL_STATUSES = frozenset({"approved", "changes_requested", "none"})
DEFAULT_APPROVAL_STATUS = "approved"


@dataclass
class CommentPosition:
    """Position data for posting inline MR comments."""

    base_sha: str
    head_sha: str


def _normalize_location(loc: str) -> str:
    """Normalize a location string by adding default line number if missing.

    Args:
        loc: Location string (e.g., "file.py" or "file.py:123")

    Returns:
        Normalized location with line number (e.g., "file.py:1" or "file.py:123")
    """
    if ":" not in loc:
        return f"{loc}:1"
    return loc


def _process_finding_locations(finding: Dict[str, Any]) -> list:
    """Process a finding's locations into a single (finding, location) pair.

    One comment is posted per finding regardless of how many locations are
    listed. The comment is anchored at the first location; remaining locations
    are stored in ``_extra_locations`` and rendered in the comment body.

    Args:
        finding: Finding dictionary from review YAML

    Returns:
        Single-element list containing a (modified_finding, [primary_location])
        tuple, or an empty list if the finding has no location.
    """
    locations = finding.get("locations", [])
    if "location" in finding:
        locations = [finding["location"]]

    if not locations:
        return []

    normalized = [_normalize_location(loc) for loc in locations]
    primary = normalized[0]

    modified_finding = finding.copy()
    if len(normalized) > 1:
        modified_finding["_extra_locations"] = normalized[1:]

    return [(modified_finding, [primary])]


def _load_review_data(review_file: str) -> tuple:
    """Load and validate review YAML, returning (review_data, mr_number, approval) or raising on error.

    Args:
        review_file: Path to the review YAML file.

    Returns:
        Tuple of (review_data dict, mr_number or None, approval string).

    Raises:
        FileNotFoundError: If the review file does not exist.
        ValueError: If required fields are missing or approval value is invalid.
        yaml.YAMLError: If the YAML is invalid.
    """
    if not Path(review_file).exists():
        raise FileNotFoundError(f"Review file not found: {review_file}")

    with open(review_file, "r", encoding="utf-8") as yaml_file:
        review_data = yaml.safe_load(yaml_file)

    if not any(k in review_data for k in ("findings", "replies", "resolve")):
        raise ValueError("Review YAML must contain 'findings', 'replies', or 'resolve' field")

    approval = review_data.get("approval", DEFAULT_APPROVAL_STATUS)
    if approval not in VALID_APPROVAL_STATUSES:
        raise ValueError(
            f"Invalid approval status '{approval}'. "
            f"Must be one of: {', '.join(sorted(VALID_APPROVAL_STATUSES))}"
        )

    return review_data, review_data.get("mr_number"), approval


def _fetch_mr_position(mr_number: int) -> Optional[CommentPosition]:
    """Fetch base and head SHAs for an MR to build inline comment positions.

    Args:
        mr_number: The MR iid.

    Returns:
        CommentPosition if both SHAs are available, None otherwise.
    """
    logger.debug("Fetching MR !%s details", mr_number)
    mr_info_cmd = ["glab", "mr", "view", str(mr_number), "--output", "json"]
    result = subprocess.run(mr_info_cmd, capture_output=True, text=True, check=True)
    mr_info = json.loads(result.stdout)

    head_sha = mr_info.get("sha") or mr_info.get("diff_refs", {}).get("head_sha")
    base_sha = mr_info.get("diff_refs", {}).get("base_sha")

    if not head_sha or not base_sha:
        return None
    return CommentPosition(base_sha=base_sha, head_sha=head_sha)


def _fetch_existing_note_bodies(mr_number: int) -> set:
    """Fetch bodies of all existing notes on an MR for deduplication.

    Returns an empty set on any error so posting proceeds normally.

    Args:
        mr_number: The MR iid.

    Returns:
        Set of note body strings already posted to the MR.
    """
    cmd = [
        "glab",
        "api",
        f"projects/:id/merge_requests/{mr_number}/notes",
        "--method",
        "GET",
        "-F",
        "per_page=100",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        notes = json.loads(result.stdout)
        return {note["body"] for note in notes if isinstance(note, dict) and "body" in note}
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as err:
        logger.warning("Could not fetch existing notes for deduplication: %s", err)
        return set()


def _fetch_discussion_note_bodies(mr_number: int, discussion_id: str) -> set:
    """Fetch note bodies from a specific discussion thread for deduplication."""
    cmd = [
        "glab", "api",
        f"projects/:id/merge_requests/{mr_number}/discussions/{discussion_id}",
        "--method", "GET",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        disc = json.loads(result.stdout)
        return {note["body"] for note in disc.get("notes", []) if isinstance(note, dict)}
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as err:
        logger.warning("Could not fetch discussion %s notes: %s", discussion_id[:12], err)
        return set()


def _parse_hunk_lines(
    file_path: str, diff_text: str, valid: Set[Tuple[str, int]]
) -> None:
    """Add (file_path, new_line_number) pairs from a unified diff to valid."""
    new_line = 0
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            if m:
                new_line = int(m.group(1)) - 1
        elif line.startswith("-"):
            pass  # removed line — not addressable by new_line
        elif line.startswith("\\ "):
            pass  # "No newline at end of file" marker
        else:
            # added lines ("+") and context lines both increment new_line
            new_line += 1
            valid.add((file_path, new_line))


def _parse_diff_lines(diffs: list) -> Tuple[Set[Tuple[str, int]], Set[str]]:
    """Extract valid (file_path, new_line) pairs from GitLab diff objects.

    Returns:
        (valid_lines, unchecked_files) where unchecked_files contains paths whose
        diff content was empty (GitLab silently omits content for large new files even
        when too_large is False). Lines in unchecked files must not be pre-validated
        — the inline attempt should proceed and fall back to a note only on API error.
    """
    valid: Set[Tuple[str, int]] = set()
    unchecked: Set[str] = set()
    for diff in diffs:
        new_path = diff.get("new_path", "")
        diff_text = diff.get("diff", "")
        if not new_path:
            continue
        if diff_text:
            _parse_hunk_lines(new_path, diff_text, valid)
        else:
            unchecked.add(new_path)
    return valid, unchecked


def _fetch_mr_diff_lines(mr_number: int) -> Optional[Tuple[Set[Tuple[str, int]], Set[str]]]:
    """Fetch valid (file_path, new_line) pairs from the MR diff.

    Returns:
        (valid_lines, unchecked_files) on success, or None on error.
        None causes the caller to skip pre-validation entirely (old behaviour).
        unchecked_files holds paths whose diff text was empty in the API response;
        lines in those files are not blocked from inline posting.
    """
    cmd = [
        "glab",
        "api",
        f"projects/:id/merge_requests/{mr_number}/diffs",
        "--method",
        "GET",
        "-F",
        "per_page=100",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        diffs = json.loads(result.stdout)
        return _parse_diff_lines(diffs)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, AttributeError, TypeError) as err:
        logger.debug("Could not fetch MR diff lines for pre-validation: %s", err)
        return None


def _post_discussion_reply(
    mr_number: int,
    discussion_id: str,
    body: str,
    dry_run: bool,
) -> Optional[bool]:
    """Post a reply note to an existing MR discussion thread.

    Args:
        mr_number: The MR iid.
        discussion_id: Full GitLab discussion SHA.
        body: Reply text.
        dry_run: If True, only print what would be done.

    Returns:
        True if posted, None if skipped (already exists), False on failure.
    """
    existing = _fetch_discussion_note_bodies(mr_number, discussion_id)
    if body.strip() in {b.strip() for b in existing}:
        logger.info("Skipping already-posted reply in discussion %s", discussion_id[:12])
        return None

    if dry_run:
        print(f"\n[DRY RUN] Would reply to discussion {discussion_id[:12]}:")
        print(f"  {body[:120]}")
        return True

    payload = {"body": body}
    cmd = [
        "glab", "api",
        f"projects/:id/merge_requests/{mr_number}/discussions/{discussion_id}/notes",
        "--method", "POST",
        "--header", "Content-Type: application/json",
        "--input", "-",
    ]
    try:
        subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True, check=True)
        logger.info("✓ Replied to discussion %s", discussion_id[:12])
        return True
    except subprocess.CalledProcessError as err:
        logger.error("Failed to reply to discussion %s: %s", discussion_id[:12], err.stderr)
        return False


def _post_replies(mr_number: int, replies: list, dry_run: bool) -> tuple:
    """Post replies to existing MR discussion threads.

    Args:
        mr_number: The MR iid.
        replies: List of reply dicts (each with discussion_id and body).
        dry_run: If True, only print what would be done.

    Returns:
        Tuple of (posted_count, skipped_count, failed_count).
    """
    posted, skipped, failed = 0, 0, 0
    for reply in replies:
        discussion_id = reply.get("discussion_id", "")
        body = reply.get("body", "").strip()
        if not discussion_id or not body:
            logger.warning("Reply entry missing discussion_id or body, skipping")
            continue
        result = _post_discussion_reply(mr_number, discussion_id, body, dry_run)
        if result is None:
            skipped += 1
        elif result:
            posted += 1
        else:
            failed += 1
    return posted, skipped, failed


def _resolve_discussion(mr_number: int, discussion_id: str, dry_run: bool) -> Optional[bool]:
    """Resolve (close) an MR discussion thread.

    Args:
        mr_number: The MR iid.
        discussion_id: Full GitLab discussion SHA.
        dry_run: If True, only print what would be done.

    Returns:
        True if resolved, None if already resolved, False on failure.
    """
    # Check current state first.
    cmd_get = [
        "glab", "api",
        f"projects/:id/merge_requests/{mr_number}/discussions/{discussion_id}",
        "--method", "GET",
    ]
    try:
        result = subprocess.run(cmd_get, capture_output=True, text=True, check=True)
        disc = json.loads(result.stdout)
        if disc.get("resolved"):
            logger.info("Discussion %s is already resolved, skipping", discussion_id[:12])
            return None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass  # proceed and attempt resolve anyway

    if dry_run:
        print(f"\n[DRY RUN] Would resolve discussion {discussion_id[:12]}")
        return True

    # `resolved` goes in the query string, not a JSON body. GitLab accepts the
    # body form for DiffNote threads but answers 403 for DiscussionNote ones
    # (a plain comment on the MR rather than one anchored to a diff line), so
    # the body form silently worked until the first non-diff thread hit it.
    cmd_put = [
        "glab", "api",
        f"projects/:id/merge_requests/{mr_number}/discussions/{discussion_id}?resolved=true",
        "--method", "PUT",
    ]
    try:
        subprocess.run(cmd_put, capture_output=True, text=True, check=True)
        logger.info("✓ Resolved discussion %s", discussion_id[:12])
        return True
    except subprocess.CalledProcessError as err:
        logger.error("Failed to resolve discussion %s: %s", discussion_id[:12], err.stderr)
        return False


def _resolve_discussions(mr_number: int, resolve_list: list, dry_run: bool) -> tuple:
    """Resolve a list of MR discussion threads.

    Args:
        mr_number: The MR iid.
        resolve_list: List of dicts with ``discussion_id`` keys.
        dry_run: If True, only print what would be done.

    Returns:
        Tuple of (resolved_count, skipped_count, failed_count).
    """
    resolved, skipped, failed = 0, 0, 0
    for entry in resolve_list:
        discussion_id = entry.get("discussion_id", "")
        if not discussion_id:
            logger.warning("Resolve entry missing discussion_id, skipping")
            continue
        result = _resolve_discussion(mr_number, discussion_id, dry_run)
        if result is None:
            skipped += 1
        elif result:
            resolved += 1
        else:
            failed += 1
    return resolved, skipped, failed


def _post_inline_findings(
    mr_number: int,
    findings: list,
    position: CommentPosition,
    dry_run: bool,
) -> tuple:
    """Process findings and post them as inline comments.

    Args:
        mr_number: The MR iid.
        findings: List of finding dicts from review YAML.
        position: CommentPosition with base and head SHAs.
        dry_run: If True, only print what would be done.

    Returns:
        Tuple of (posted_count, skipped_count, failed_count).
    """
    existing_bodies = _fetch_existing_note_bodies(mr_number)
    diff_result = _fetch_mr_diff_lines(mr_number) if not dry_run else None
    valid_diff_lines: Optional[Set[Tuple[str, int]]] = diff_result[0] if diff_result else None
    unchecked_files: Optional[Set[str]] = diff_result[1] if diff_result else None
    posted_count, skipped_count, failed_count = 0, 0, 0
    for finding in findings:
        finding_results = _process_finding_locations(finding)
        if not finding_results:
            logger.warning("Finding '%s' has no location, skipping", finding.get("title"))
            continue
        for located_finding, locations in finding_results:
            for location in locations:
                result = post_inline_comment(
                    mr_number=mr_number,
                    finding=located_finding,
                    location=location,
                    position=position,
                    dry_run=dry_run,
                    existing_bodies=existing_bodies,
                    valid_diff_lines=valid_diff_lines,
                    unchecked_files=unchecked_files,
                )
                if result is None:
                    skipped_count += 1
                elif result:
                    posted_count += 1
                else:
                    failed_count += 1
    return posted_count, skipped_count, failed_count


def _is_already_approved_error(stderr: str) -> bool:
    """Return True when the glab stderr indicates the MR is already approved."""
    lowered = stderr.lower()
    return "already approved" in lowered or "user has already approved" in lowered


def _is_not_approved_error(stderr: str) -> bool:
    """Return True when the glab stderr indicates there is no approval to revoke."""
    lowered = stderr.lower()
    return (
        "not approved" in lowered
        or "user has not approved" in lowered
        or "cannot unapprove" in lowered
        or "404" in lowered
    )


def _run_approve(mr_number: int) -> bool:
    """Call 'glab mr approve' and return True on success or already-approved errors."""
    cmd = ["glab", "mr", "approve", str(mr_number)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("✓ Approved MR !%s", mr_number)
        return True
    except subprocess.CalledProcessError as err:
        if _is_already_approved_error(err.stderr):
            logger.info("MR !%s is already approved, treating as success", mr_number)
            return True
        logger.error("Failed to approve MR !%s: %s", mr_number, err.stderr)
        return False


def _run_unapprove(mr_number: int) -> bool:
    """Call 'glab mr unapprove' and return True on success or not-approved errors."""
    cmd = ["glab", "mr", "unapprove", str(mr_number)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("✓ Revoked approval on MR !%s", mr_number)
        return True
    except subprocess.CalledProcessError as err:
        if _is_not_approved_error(err.stderr):
            logger.info("MR !%s was not approved, nothing to revoke", mr_number)
            return True
        logger.error("Failed to revoke approval on MR !%s: %s", mr_number, err.stderr)
        return False


def _apply_approval_status(mr_number: int, approval: str, dry_run: bool) -> bool:
    """Apply the approval decision to the MR.

    Args:
        mr_number: The MR iid.
        approval: One of "approved", "changes_requested", or "none".
        dry_run: If True, only print what would be done.

    Returns:
        True on success. Also True when the approval is skipped ("none") or reverted
        ("changes_requested" with no prior approval to revoke). False on subprocess failure.
    """
    if approval == "changes_requested":
        if dry_run:
            print(f"\n[DRY RUN] Would revoke approval on MR !{mr_number}")
            return True
        return _run_unapprove(mr_number)

    if approval == "none":
        if dry_run:
            print(f"\n[DRY RUN] Approval status: 'none' — no approval action on MR !{mr_number}")
        else:
            logger.info("Approval status: 'none' — no approval action for MR !%s", mr_number)
        return True

    # approval == "approved"
    if dry_run:
        print(f"\n[DRY RUN] Would approve MR !{mr_number}")
        return True
    return _run_approve(mr_number)


def _handle_resolve(mr_number: int, resolve_list: list, dry_run: bool) -> int:
    """Resolve listed discussion threads and return 0/1 exit code."""
    if not resolve_list:
        return 0
    res_resolved, res_skipped, res_failed = _resolve_discussions(mr_number, resolve_list, dry_run)
    if dry_run:
        print(
            f"\n[DRY RUN] Resolve: {res_resolved} would resolve, "
            f"{res_skipped} already resolved, {res_failed} failed — MR !{mr_number}"
        )
    else:
        logger.info(
            "✓ Resolve: %d resolved, %d skipped, %d failed on MR !%s",
            res_resolved, res_skipped, res_failed, mr_number,
        )
    return 1 if res_failed else 0


def _post_findings(mr_number: int, review_data: Dict[str, Any], dry_run: bool) -> int:
    """Post inline findings (or fall back to a general comment) and return 0/1 exit code."""
    findings = review_data.get("findings", [])
    if not findings:
        return 0

    position = _fetch_mr_position(mr_number)
    if position is None:
        logger.error("Could not get commit SHAs from MR. Falling back to general comment.")
        return post_general_comment(mr_number, review_data, dry_run)

    posted_count, skipped_count, failed_count = _post_inline_findings(
        mr_number, findings, position, dry_run
    )
    if dry_run:
        print(
            f"\n[DRY RUN] Would post {posted_count} inline comments "
            f"({skipped_count} already posted, {failed_count} failed) to MR !{mr_number}"
        )
    else:
        logger.info(
            "✓ Posted %d inline comments to MR !%s (%d skipped, %d failed)",
            posted_count,
            mr_number,
            skipped_count,
            failed_count,
        )
    return 1 if failed_count else 0


def cmd_comment(args) -> int:
    """Handle the 'comment' subcommand - post review from YAML file to MR.

    Posts individual comments on specific lines in the MR diff for each finding.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code: 0 if all comments posted and approval applied; 1 if any step failed.
        Approval failure (e.g. glab connectivity) counts as failure even if comments posted.
    """
    try:
        review_data, yaml_mr_number, approval = _load_review_data(args.review_file)

        mr_number = args.mr_number or yaml_mr_number
        if mr_number is None:
            logger.error("MR number must be specified via --mr or in review YAML")
            return 1

        exit_code = 0

        if _handle_resolve(mr_number, review_data.get("resolve", []), args.dry_run):
            exit_code = 1

        replies = review_data.get("replies", [])
        if replies:
            r_posted, r_skipped, r_failed = _post_replies(mr_number, replies, args.dry_run)
            if args.dry_run:
                print(
                    f"\n[DRY RUN] Replies: {r_posted} would post, "
                    f"{r_skipped} already posted, {r_failed} failed — MR !{mr_number}"
                )
            else:
                logger.info(
                    "✓ Replies: %d posted, %d skipped, %d failed to MR !%s",
                    r_posted, r_skipped, r_failed, mr_number,
                )
            if r_failed:
                exit_code = 1

        if _post_findings(mr_number, review_data, args.dry_run):
            exit_code = 1

        if not _apply_approval_status(mr_number, approval, args.dry_run):
            exit_code = 1

        return exit_code

    except subprocess.CalledProcessError as err:
        logger.error("Command failed: %s", err.stderr)
    except (FileNotFoundError, ValueError, yaml.YAMLError, json.JSONDecodeError) as err:
        logger.error("Error: %s", err)

    return 1


def _post_note_fallback(
    mr_number: int, location: str, comment_body: str, existing_bodies: set
) -> Optional[bool]:
    """Post a general MR note when inline comment posting fails.

    Used when GitLab rejects an inline comment (e.g. the line is not in the
    diff). Prepends the original location so context is preserved.

    Args:
        mr_number: The MR iid.
        location: Original file:line location string.
        comment_body: Formatted comment body to post as a note.
        existing_bodies: Set of note bodies already on the MR; used to skip
            duplicates when the comment was previously posted as a fallback note.

    Returns:
        True if the note was posted successfully (or already existed), False otherwise.
    """
    # A previously posted fallback note embeds comment_body after the location
    # prefix, so checking for comment_body as a substring catches it.
    if any(comment_body in existing for existing in existing_bodies):
        logger.info("Skipping already-posted note for %s", location)
        return None  # None signals "skipped", distinct from True (posted) / False (failed)

    note_body = f"**Location:** `{location}`\n\n{comment_body}"
    cmd = [
        "glab",
        "api",
        f"projects/:id/merge_requests/{mr_number}/notes",
        "--method",
        "POST",
        "-f",
        f"body={note_body}",
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("✓ Posted note (fallback) for %s", location)
        return True
    except subprocess.CalledProcessError as err:
        logger.error("Failed to post note fallback on %s: %s", location, err.stderr)
        return False


def _format_comment_body(finding: Dict[str, Any]) -> str:
    """Build the markdown comment body from a finding dict."""
    severity = finding.get("severity", "Unknown")
    title = finding.get("title", "Untitled")
    description = finding.get("description", "").strip()
    fix = finding.get("fix", "").strip()
    extra_locations = finding.get("_extra_locations", [])

    body = f"**{severity}: {title}**\n\n{description}"

    if extra_locations:
        locs_str = ", ".join(f"`{loc}`" for loc in extra_locations)
        body += f"\n\n**Also affects:** {locs_str}"

    if fix:
        body += f"\n\n**Fix:**\n```\n{fix}\n```"

    return body


def _post_inline_via_api(  # pylint: disable=too-many-positional-arguments
    # All 7 arguments are distinct data needed by the API call; no sensible grouping exists.
    mr_number: int,
    file_path: str,
    line_num: int,
    location: str,
    comment_body: str,
    position: CommentPosition,
    existing_bodies: set,
) -> Optional[bool]:
    """Send the inline discussion API call and fall back to a note on failure."""
    payload = {
        "body": comment_body,
        "position": {
            "position_type": "text",
            "old_path": file_path,
            "new_path": file_path,
            "old_line": None,  # null for new files
            "new_line": line_num,
            "base_sha": position.base_sha,
            "start_sha": position.base_sha,
            "head_sha": position.head_sha,
        },
    }
    cmd = [
        "glab",
        "api",
        f"projects/:id/merge_requests/{mr_number}/discussions",
        "--method",
        "POST",
        "--header",
        "Content-Type: application/json",
        "--input",
        "-",
    ]
    logger.debug("Posting comment to %s:%d", file_path, line_num)
    try:
        subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True, check=True)
        logger.info("✓ Posted inline comment on %s:%d", file_path, line_num)
        return True
    except subprocess.CalledProcessError as err:
        # GitLab returns 500 when the line is not part of the diff.
        # Fall back to a general note so the finding is never silently dropped.
        logger.warning(
            "Inline comment failed for %s:%d (%s) — falling back to note",
            file_path,
            line_num,
            err.stderr.strip(),
        )
        return _post_note_fallback(mr_number, location, comment_body, existing_bodies)


def post_inline_comment(  # pylint: disable=too-many-return-statements
    # Each return guards a distinct failure/skip path; collapsing them would obscure intent.
    mr_number: int,
    finding: Dict[str, Any],
    location: str,
    position: CommentPosition,
    dry_run: bool = False,
    *,
    existing_bodies: Optional[set] = None,
    valid_diff_lines: Optional[Set[Tuple[str, int]]] = None,
    unchecked_files: Optional[Set[str]] = None,
) -> Optional[bool]:
    """Post an inline comment on a specific line in the MR diff.

    Args:
        mr_number: MR number
        finding: Finding dictionary from review YAML
        location: File location string (e.g., "path/to/file.cc:123")
        position: Comment position data (base and head SHAs)
        dry_run: If True, only print what would be done
        existing_bodies: Set of note bodies already on the MR; used to skip
            duplicates when the command is re-run after a partial failure.
        valid_diff_lines: Set of (file_path, line_num) pairs present in the MR
            diff; when provided, lines absent from it skip directly to note fallback.
        unchecked_files: Files whose diff content was empty in the GitLab API response
            (large new files). Lines in these files are not pre-validated — the inline
            API call is attempted and falls back to a note only on failure.

    Returns:
        True if comment posted successfully (or already existed), False otherwise
    """
    try:
        # Parse location (format: "path/to/file.cc:123" or "path/to/file.cc:123-145")
        if ":" not in location:
            logger.warning("Invalid location format: %s (expected 'file:line')", location)
            return False

        file_path, line_part = location.rsplit(":", 1)

        # Handle line ranges (use start line)
        if "-" in line_part:
            line_num = int(line_part.split("-")[0])
        else:
            line_num = int(line_part)

        comment_body = _format_comment_body(finding)
        bodies = existing_bodies or set()

        # Skip if this comment body was already posted (as inline or fallback note).
        # Fallback notes embed comment_body after a location prefix, so substring
        # matching catches both forms.
        already_posted = existing_bodies and any(comment_body in existing for existing in bodies)

        if already_posted:
            if dry_run:
                print(f"\n[DRY RUN] Already posted, skipping {file_path}:{line_num}")
            else:
                logger.info("Skipping already-posted comment on %s:%d", file_path, line_num)
            return None  # None signals "skipped", distinct from True (posted) / False (failed)

        # Skip inline attempt for lines confirmed absent from the diff.
        # Files in unchecked_files had empty diff content in the API response (GitLab
        # silently omits diff text for large new files); treat them as unvalidated and
        # let the inline API call decide.
        file_is_unchecked = unchecked_files is not None and file_path in unchecked_files
        if (
            not file_is_unchecked
            and valid_diff_lines is not None
            and (file_path, line_num) not in valid_diff_lines
        ):
            logger.debug(
                "Line %s:%d not in MR diff — posting as note directly",
                file_path,
                line_num,
            )
            if dry_run:
                print(f"\n[DRY RUN] Line not in diff, would post as note on {file_path}:{line_num}")
                return True
            return _post_note_fallback(mr_number, location, comment_body, bodies)

        if dry_run:
            print(f"\n[DRY RUN] Would post comment on {file_path}:{line_num}")
            print(f"  Severity: {finding.get('severity', 'Unknown')}")
            print(f"  Title: {finding.get('title', 'Untitled')}")
            return True

        return _post_inline_via_api(
            mr_number, file_path, line_num, location, comment_body, position, bodies
        )

    except (json.JSONDecodeError, KeyError, TypeError) as err:
        logger.error("Error posting comment on %s: %s", location, err)
        return False
    except ValueError as err:
        logger.error("Invalid line number in location: %s: %s", location, err)
        return False


def post_general_comment(mr_number: int, review_data: Dict[str, Any], dry_run: bool = False) -> int:
    """Post a general comment with all findings (fallback when inline comments fail).

    Args:
        mr_number: MR number
        review_data: Review data from YAML
        dry_run: If True, only print what would be done

    Returns:
        Exit code (0 for success, 1 for error)
    """
    try:
        comment = format_review_comment(review_data)

        if dry_run:
            print(f"[DRY RUN] Would post general comment to MR !{mr_number}:")
            print("=" * 80)
            print(comment)
            print("=" * 80)
            return 0

        cmd = ["glab", "mr", "comment", str(mr_number), "--message", comment]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        logger.info("✓ Posted general comment to MR !%s", mr_number)
        print(f"Comment posted: {result.stdout.strip()}")
        return 0

    except subprocess.CalledProcessError as err:
        logger.error("Failed to post general comment: %s", err.stderr)
        return 1


def format_review_comment(review_data: Dict[str, Any]) -> str:
    """Format review YAML data into a markdown comment.

    Args:
        review_data: Review data from YAML file.

    Returns:
        Formatted markdown comment.
    """
    lines = []

    # Header
    title = review_data.get("title", "Code Review")
    review_date = review_data.get("review_date", "")
    lines.append(f"# Code Review: {title}")
    if review_date:
        lines.append(f"**Review Date:** {review_date}")
    lines.append("")

    # Group findings by severity
    findings = review_data.get("findings", [])
    severity_groups: Dict[str, List[Dict[str, Any]]] = {}
    for finding in findings:
        severity = finding.get("severity", "Unknown")
        if severity not in severity_groups:
            severity_groups[severity] = []
        severity_groups[severity].append(finding)

    # Output findings by severity (Critical, High, Medium, Low)
    severity_order = ["Critical", "High", "Medium", "Low"]
    finding_num = 1

    for severity in severity_order:
        if severity not in severity_groups:
            continue

        lines.append(f"## {severity} Priority Issues")
        lines.append("")

        for finding in severity_groups[severity]:
            title = finding.get("title", "Untitled")
            description = finding.get("description", "").strip()
            location = finding.get("location")
            locations = finding.get("locations", [])
            fix = finding.get("fix", "").strip()

            lines.append(f"### {finding_num}. {title}")
            finding_num += 1

            if description:
                lines.append(f"{description}")
                lines.append("")

            if location:
                lines.append(f"**Location:** `{location}`")
            elif locations:
                lines.append("**Locations:**")
                for loc in locations:
                    lines.append(f"- `{loc}`")

            if fix:
                lines.append("\n**Fix:**")
                lines.append("```")
                lines.append(fix)
                lines.append("```")

            lines.append("")

    return "\n".join(lines)
