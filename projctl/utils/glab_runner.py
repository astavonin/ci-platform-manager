"""Shared utility for running glab CLI commands."""

import json
import logging
import shlex
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ..exceptions import PlatformError
from .cli_runner import (
    run_cli_command,
    run_cli_command_binary,
    run_cli_command_status,
    stream_cli_command_to_file,
)

logger = logging.getLogger(__name__)

_NOT_FOUND_MSG = "glab command not found. Please install glab CLI."


# A sentinel type has no behaviour to expose by design: its only job is to be a
# unique identity distinct from None.
class _DryRun:  # pylint: disable=too-few-public-methods
    """Sentinel returned when a call was previewed rather than executed.

    Distinct from None so a caller cannot confuse "nothing ran" with a genuine
    JSON `null` response, which would turn a no-op write into a reported success.
    """

    def __repr__(self) -> str:
        return "<dry-run>"


DRY_RUN = _DryRun()


def run_glab_command(cmd: List[str]) -> str:
    """Run a glab command and return its output.

    Args:
        cmd: List of command arguments to pass to glab.

    Returns:
        Command output as a string.

    Raises:
        PlatformError: If the command fails or glab is not installed.
    """
    return run_cli_command("glab", cmd, _NOT_FOUND_MSG)


def run_glab_command_binary(cmd: List[str]) -> bytes:
    """Run a glab command and return its raw stdout bytes.

    Binary-safe counterpart to run_glab_command(), for endpoints that return
    non-text payloads (e.g. a single downloaded artifact file).

    Args:
        cmd: List of command arguments to pass to glab.

    Returns:
        Command stdout as raw bytes.

    Raises:
        PlatformError: If the command fails or glab is not installed.
    """
    return run_cli_command_binary("glab", cmd, _NOT_FOUND_MSG)


def stream_glab_command_to_file(cmd: List[str], dest: Path) -> None:
    """Run a glab command and stream its stdout directly to a file.

    Args:
        cmd: List of command arguments to pass to glab.
        dest: Destination file path.

    Raises:
        PlatformError: If the command fails or glab is not installed.
    """
    stream_cli_command_to_file("glab", cmd, dest, _NOT_FOUND_MSG)


def run_glab_json(cmd: List[str], dry_run: bool = False) -> Optional[Any]:
    """Run a glab command and return its parsed JSON response.

    Centralises the preview / execute / parse sequence shared by every handler
    that writes through the glab API, so the dry-run contract is defined once.

    Args:
        cmd: List of command arguments to pass to glab.
        dry_run: When True, print the command and make no API call.

    Returns:
        Parsed JSON response, or the DRY_RUN sentinel when dry_run is True.

    Raises:
        PlatformError: If the command fails or its output is not JSON.
    """
    if dry_run:
        print(f"[dry-run] {shlex.join(['glab', *cmd])}")
        return DRY_RUN

    result = run_glab_command(cmd)
    try:
        return json.loads(result)
    except json.JSONDecodeError as exc:
        raise PlatformError(f"Unexpected glab response: {result[:200]!r}") from exc


def run_glab_json_pages(cmd: List[str]) -> List[Any]:
    """Run a paginated glab API command and return every page's items merged.

    Older glab versions concatenate one JSON array per page rather than merging
    them, so a multi-page response is not parseable as a single document; newer
    ones emit a single array. Both forms are handled. Silently parsing only the
    first page would drop later items, which reads as "the resource has fewer
    entries" rather than as an error.

    Args:
        cmd: List of command arguments to pass to glab, including --paginate.

    Returns:
        Flat list of items across all pages.

    Raises:
        PlatformError: If the command fails, output is not JSON, or a decoded
            page is not a JSON array.
    """
    result = run_glab_command(cmd)
    if not result.strip():
        raise PlatformError("Empty response from glab; expected a JSON array")
    try:
        single = json.loads(result)
    except json.JSONDecodeError:
        pass
    else:
        if not isinstance(single, list):
            raise PlatformError(f"Expected a JSON array, got: {str(single)[:200]!r}")
        return single

    merged: List[Any] = []
    decoder = json.JSONDecoder()
    idx, end = 0, len(result)
    while idx < end:
        while idx < end and result[idx].isspace():
            idx += 1
        if idx >= end:
            break
        try:
            page, idx = decoder.raw_decode(result, idx)
        except json.JSONDecodeError as exc:
            raise PlatformError(f"Unexpected glab response: {result[:200]!r}") from exc
        if not isinstance(page, list):
            raise PlatformError(f"Expected a JSON array page, got: {str(page)[:200]!r}")
        merged.extend(page)
    return merged


def run_glab_command_status(cmd: List[str]) -> Tuple[int, str, str]:
    """Run a glab command and return (exit code, stdout, stderr).

    For commands whose non-zero exit is a verdict rather than a failure.

    Args:
        cmd: List of command arguments to pass to glab.

    Returns:
        Tuple of (exit code, stdout, stderr).

    Raises:
        PlatformError: Only if glab is not installed.
    """
    return run_cli_command_status("glab", cmd, _NOT_FOUND_MSG)


def discussion_resolve_endpoint(
    discussions_base: str, discussion_id: str, resolved: bool = True
) -> str:
    """Build the endpoint that sets an MR discussion thread's resolved state.

    `resolved` belongs in the query string, never in a JSON body. GitLab accepts
    the body form for DiffNote threads but answers 403 for DiscussionNote ones (a
    plain comment on the MR rather than one anchored to a diff line), so a body
    form appears to work until the first non-diff thread hits it. Recorded in
    planning/reviews-orphan/main-0e68987/observed-failures.md (2026-08-13); this
    helper exists so the two call sites cannot drift back apart.

    Args:
        discussions_base: Endpoint up to and including `/discussions`.
        discussion_id: Full GitLab discussion SHA.
        resolved: Target state.

    Returns:
        Endpoint string with the `resolved` query parameter applied.
    """
    return f"{discussions_base}/{discussion_id}?resolved={'true' if resolved else 'false'}"
