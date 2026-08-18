"""CI lint handler — validate a GitLab CI configuration before pushing it."""

import logging
from pathlib import Path
from typing import List, Optional

from ..exceptions import PlatformError
from ..utils.glab_runner import run_glab_command_status

logger = logging.getLogger(__name__)

DEFAULT_CI_FILE = ".gitlab-ci.yml"

# glab writes exactly one of these to stdout when it reaches a verdict, and
# nothing else it prints contains either phrase. Every other way glab can fail
# — no GitLab remote, expired token, API error, unreadable file — also exits
# non-zero but leaves stdout without a marker, which is what separates "your
# configuration is broken" from "I could not check it". Matched
# case-insensitively; note "is invalid" does not contain "is valid".
_VALID_MARKER = "is valid"
_INVALID_MARKER = "is invalid"


# pylint: disable=too-few-public-methods
# Single-responsibility handler with one public entry point, matching
# LabelsHandler. Extra methods would exist only to satisfy the checker.
class CiLintHandler:
    """Validates a GitLab CI configuration against the server-side linter.

    The server linter is the only authority on CI schema: a file can be valid
    YAML and still be rejected, which is the class of error this catches. A
    local YAML parse cannot substitute for it.
    """

    def __init__(self, simulate: bool = False) -> None:
        """Initialize the CI lint handler.

        Args:
            simulate: When True, ask GitLab to simulate pipeline creation as
                well as validating the schema. This is glab's ``--dry-run``,
                which means the opposite of ``--dry-run`` everywhere else in
                projctl: it makes a *heavier* server call rather than skipping
                the API entirely. Hence the different name here.
        """
        self.simulate = simulate

    @staticmethod
    def _resolve_path(path: Optional[str]) -> Path:
        """Resolve the CI file to lint.

        Args:
            path: Explicit path, or None to use the repository default.

        Returns:
            Path to an existing CI configuration file.

        Raises:
            PlatformError: If the path does not exist or is not a regular file.
        """
        target = Path(path) if path else Path(DEFAULT_CI_FILE)
        if target.is_file():
            return target
        if target.exists():
            raise PlatformError(f"CI configuration is not a regular file: {target}")
        raise PlatformError(f"CI configuration not found: {target}")

    @staticmethod
    def _report(stdout: str, stderr: str) -> str:
        """Join both streams into the text the user should see.

        Args:
            stdout: Captured standard output.
            stderr: Captured standard error.

        Returns:
            Both streams joined, omitting whichever is empty.
        """
        return "\n".join(part for part in (stdout, stderr) if part)

    @staticmethod
    def _classify(exit_code: int, stdout: str) -> Optional[bool]:
        """Map glab's exit code and stdout onto a verdict.

        Args:
            exit_code: glab's exit status.
            stdout: Captured standard output.

        Returns:
            True when the configuration is valid, False when GitLab rejected
            it, or None when glab reported no verdict at all.
        """
        lowered = stdout.lower()
        if exit_code == 0 and _VALID_MARKER in lowered:
            return True
        if exit_code != 0 and _INVALID_MARKER in lowered:
            return False
        return None

    def lint(self, path: Optional[str] = None, ref: Optional[str] = None) -> bool:
        """Lint a CI configuration and print the result.

        Args:
            path: CI file to validate. Defaults to ``.gitlab-ci.yml``.
            ref: Branch or tag to use as the simulation context.

        Returns:
            True when the configuration is valid, False when GitLab rejected it.

        Raises:
            ValueError: If ref is given without simulate.
            PlatformError: If the file is missing, glab is unavailable, or glab
                reported no verdict.
        """
        # glab only applies --ref during a pipeline simulation; without
        # --dry-run it is silently ignored, so accepting the combination would
        # report a plain static check as if it had been validated against ref.
        if ref and not self.simulate:
            raise ValueError("--ref only applies with --dry-run; glab ignores it otherwise")

        target = self._resolve_path(path)

        cmd: List[str] = ["ci", "lint"]
        if self.simulate:
            cmd.append("--dry-run")
        if ref:
            cmd.extend(["--ref", ref])
        # Everything after "--" is positional, so a CI file whose name starts
        # with a dash cannot be swallowed as a glab flag. Flags must come
        # first: a flag placed after "--" would parse as a second positional.
        cmd.extend(["--", str(target)])

        logger.debug("Linting %s (simulate=%s, ref=%s)", target, self.simulate, ref)

        # glab writes the lint report to stdout and signals the verdict through
        # its exit code, so a runner that raises on non-zero and keeps only
        # stderr would discard the one thing the caller needs.
        exit_code, stdout, stderr = run_glab_command_status(cmd)

        verdict = self._classify(exit_code, stdout)
        report = self._report(stdout, stderr)

        # glab exits 1 both for "this configuration is invalid" and for "I
        # could not check it" — an expired token, no GitLab remote, or an API
        # error all land here with an empty stdout. Only a real verdict carries
        # a marker, so anything else has to surface as a tool failure: calling
        # it invalid would blame the user's CI file for their credentials.
        if verdict is None:
            raise PlatformError(
                f"glab reported no lint verdict (exit {exit_code}): {report or '<no output>'}"
            )

        print(report)
        return verdict
