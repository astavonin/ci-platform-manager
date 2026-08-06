"""Shared subprocess execution for CLI tools (gh, glab, etc.)."""

import logging
import subprocess
from pathlib import Path
from typing import List

from ..exceptions import PlatformError

logger = logging.getLogger(__name__)


def run_cli_command(cli_name: str, cmd: List[str], not_found_msg: str) -> str:
    """Run a CLI command and return its stdout.

    Args:
        cli_name: Name of the CLI binary (e.g. "gh", "glab").
        cmd: Command arguments to pass after the binary name.
        not_found_msg: Error message to raise when the binary is not installed.

    Returns:
        Command stdout as a stripped string.

    Raises:
        PlatformError: If the command fails or the binary is not installed.
    """
    full_cmd = [cli_name] + cmd

    try:
        logger.debug("Executing: %s", " ".join(full_cmd))
        result = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as err:
        error_msg = f"Command failed: {' '.join(full_cmd)}\n{err.stderr}"
        logger.error(error_msg)
        raise PlatformError(error_msg) from err
    except FileNotFoundError as err:
        logger.error(not_found_msg)
        raise PlatformError(not_found_msg) from err


def run_cli_command_binary(cli_name: str, cmd: List[str], not_found_msg: str) -> bytes:
    """Run a CLI command and return its raw stdout bytes.

    Binary-safe counterpart to run_cli_command(): that function hardcodes
    text=True, which corrupts (or raises UnicodeDecodeError on) non-UTF-8
    stdout such as a downloaded file. Use this for small binary payloads;
    for large ones, stream_cli_command_to_file() avoids buffering in memory.

    Args:
        cli_name: Name of the CLI binary (e.g. "glab").
        cmd: Command arguments to pass after the binary name.
        not_found_msg: Error message to raise when the binary is not installed.

    Returns:
        Command stdout as raw bytes (not decoded, not stripped).

    Raises:
        PlatformError: If the command fails or the binary is not installed.
    """
    full_cmd = [cli_name] + cmd

    try:
        logger.debug("Executing (binary): %s", " ".join(full_cmd))
        result = subprocess.run(full_cmd, capture_output=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError as err:
        stderr = err.stderr.decode("utf-8", errors="replace") if err.stderr else ""
        error_msg = f"Command failed: {' '.join(full_cmd)}\n{stderr}"
        logger.error(error_msg)
        raise PlatformError(error_msg) from err
    except FileNotFoundError as err:
        logger.error(not_found_msg)
        raise PlatformError(not_found_msg) from err


def stream_cli_command_to_file(
    cli_name: str, cmd: List[str], dest: Path, not_found_msg: str
) -> None:
    """Run a CLI command and stream its stdout directly to a file.

    Writes bytes straight from the subprocess pipe to `dest` without ever
    holding the payload in memory, so downloading a large archive doesn't
    inflate process RSS. `dest`'s parent directory must already exist.

    Args:
        cli_name: Name of the CLI binary (e.g. "glab").
        cmd: Command arguments to pass after the binary name.
        dest: Destination file path; created (or truncated) and written in
            binary mode.
        not_found_msg: Error message to raise when the binary is not installed.

    Raises:
        PlatformError: If the command fails or the binary is not installed.
            Any partially-written destination file is removed on failure.
    """
    full_cmd = [cli_name] + cmd
    logger.debug("Executing (streaming to %s): %s", dest, " ".join(full_cmd))

    with open(dest, "wb") as out_file:
        try:
            subprocess.run(full_cmd, stdout=out_file, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as err:
            dest.unlink(missing_ok=True)
            stderr = err.stderr.decode("utf-8", errors="replace") if err.stderr else ""
            error_msg = f"Command failed: {' '.join(full_cmd)}\n{stderr}"
            logger.error(error_msg)
            raise PlatformError(error_msg) from err
        except FileNotFoundError as err:
            dest.unlink(missing_ok=True)
            logger.error(not_found_msg)
            raise PlatformError(not_found_msg) from err
