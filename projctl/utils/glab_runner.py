"""Shared utility for running glab CLI commands."""

import logging
from pathlib import Path
from typing import List

from .cli_runner import run_cli_command, run_cli_command_binary, stream_cli_command_to_file

logger = logging.getLogger(__name__)

_NOT_FOUND_MSG = "glab command not found. Please install glab CLI."


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
