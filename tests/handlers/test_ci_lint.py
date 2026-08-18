"""Tests for projctl.handlers.ci_lint module."""

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from projctl.exceptions import PlatformError
from projctl.handlers.ci_lint import CiLintHandler


@pytest.fixture(name="ci_file")
def ci_file_fixture(tmp_path: Path, monkeypatch) -> Path:
    """Create a .gitlab-ci.yml in a temp cwd and chdir into it."""
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("job:\n  script:\n    - echo hi\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return target


def _stub_glab(monkeypatch, result: Tuple[int, str, str]) -> List[List[str]]:
    """Stub run_glab_command_status with a canned result.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        result: The (exit code, stdout, stderr) triple to return.

    Returns:
        A list that accumulates every command the handler passed to glab.
    """
    captured: List[List[str]] = []

    def _fake_run(cmd: List[str]) -> Tuple[int, str, str]:
        captured.append(cmd)
        return result

    monkeypatch.setattr("projctl.handlers.ci_lint.run_glab_command_status", _fake_run)
    return captured


class TestResolvePath:
    """Path resolution and its failure modes."""

    def test_defaults_to_repo_ci_file_when_path_omitted(self, ci_file: Path, monkeypatch) -> None:
        """No path argument resolves to .gitlab-ci.yml in the working directory."""
        captured = _stub_glab(monkeypatch, (0, "Configuration is valid", ""))

        CiLintHandler().lint()

        assert captured[0] == ["ci", "lint", "--", ".gitlab-ci.yml"]

    def test_missing_file_raises_platform_error_before_any_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A path that does not exist fails without shelling out to glab."""
        captured = _stub_glab(monkeypatch, (0, "is valid", ""))

        with pytest.raises(PlatformError, match="nope.yml"):
            CiLintHandler().lint(path=str(tmp_path / "nope.yml"))

        assert captured == []

    def test_directory_is_reported_as_not_a_regular_file(self, tmp_path: Path, monkeypatch) -> None:
        """An existing path that is not a file says so rather than 'not found'.

        'Not found' would send the user looking for a missing file when the
        path is right there — it is just a directory.
        """
        captured = _stub_glab(monkeypatch, (0, "is valid", ""))

        with pytest.raises(PlatformError, match="not a regular file"):
            CiLintHandler().lint(path=str(tmp_path))

        assert captured == []


class TestLintOutcome:
    """The valid/invalid decision, which is the handler's whole contract."""

    def test_returns_true_and_prints_report_when_valid(
        self, ci_file: Path, monkeypatch, capsys
    ) -> None:
        """A configuration glab accepts returns True."""
        _stub_glab(monkeypatch, (0, ".gitlab-ci.yml is valid", ""))

        assert CiLintHandler().lint(path=str(ci_file)) is True
        assert "is valid" in capsys.readouterr().out

    def test_returns_false_and_prints_report_when_glab_rejects(
        self, ci_file: Path, monkeypatch, capsys
    ) -> None:
        """glab exits non-zero on an invalid config; the report is the useful part.

        This is the real-world case: `script` holding a mapping rather than a
        string. The lint report must reach the caller instead of a stack trace.
        """
        message = (
            "jobs:yocto-build:script config should be a string "
            "or a nested array of strings up to 10 levels deep"
        )
        _stub_glab(monkeypatch, (1, f"{ci_file} is invalid.\n1 {message}", ""))

        assert CiLintHandler().lint(path=str(ci_file)) is False
        out = capsys.readouterr().out
        assert "is invalid" in out
        # The GitLab report itself must reach the caller — it names the job and
        # the offending key, which is the whole reason to run the linter.
        assert "nested array of strings" in out
        assert "yocto-build" in out


class TestToolFailureIsNotAVerdict:
    """glab exits 1 for 'invalid config' AND for 'could not check'.

    Only the former carries a marker on stdout. Collapsing the two would blame
    a user's CI file for an expired token, so everything without a marker has
    to raise rather than return False.
    """

    def test_missing_gitlab_remote_raises_instead_of_reporting_invalid(
        self, ci_file: Path, monkeypatch
    ) -> None:
        """The observed failure: no GitLab remote exits 1 with empty stdout."""
        stderr = (
            "ERROR: You must be in a GitLab project repository for this action: "
            "none of the git remotes configured for this repository point to a "
            "known GitLab host."
        )
        _stub_glab(monkeypatch, (1, "", stderr))

        with pytest.raises(PlatformError, match="no lint verdict") as err:
            CiLintHandler().lint(path=str(ci_file))

        # The diagnosis lives in stderr; dropping it leaves the user with an
        # exit code and no way to tell credentials from a broken config.
        assert "known GitLab host" in str(err.value)

    def test_expired_token_raises_even_though_glab_started_validating(
        self, ci_file: Path, monkeypatch
    ) -> None:
        """A non-empty stdout is not itself a verdict.

        glab prints 'Validating...' before calling the API, so 'stdout has
        content' cannot stand in for the marker check.
        """
        _stub_glab(monkeypatch, (1, "Validating...", "ERROR: 401 Unauthorized"))

        with pytest.raises(PlatformError, match="401 Unauthorized"):
            CiLintHandler().lint(path=str(ci_file))

    def test_exit_zero_without_validity_marker_raises(self, ci_file: Path, monkeypatch) -> None:
        """Unrecognised success output is never treated as a pass.

        If glab changes its wording, raising surfaces the drift; returning
        True would silently approve every configuration.
        """
        _stub_glab(monkeypatch, (0, "some unexpected output", ""))

        with pytest.raises(PlatformError, match="no lint verdict"):
            CiLintHandler().lint(path=str(ci_file))

    def test_reports_no_output_when_both_streams_are_empty(
        self, ci_file: Path, monkeypatch
    ) -> None:
        """A silent failure still produces an actionable message."""
        _stub_glab(monkeypatch, (1, "", ""))

        with pytest.raises(PlatformError, match="<no output>"):
            CiLintHandler().lint(path=str(ci_file))

    def test_propagates_when_glab_itself_is_unavailable(self, ci_file: Path, monkeypatch) -> None:
        """A tool failure is not a lint verdict and must not read as one."""

        def _raise(cmd):
            raise PlatformError("glab command not found. Please install glab CLI.")

        monkeypatch.setattr("projctl.handlers.ci_lint.run_glab_command_status", _raise)

        with pytest.raises(PlatformError, match="glab command not found"):
            CiLintHandler().lint(path=str(ci_file))


class TestFlagPassthrough:
    """Flags reach glab in the form it expects."""

    def test_simulate_and_ref_precede_the_path_separator(self, ci_file: Path, monkeypatch) -> None:
        """--dry-run and --ref are sent, and the path stays positional.

        Flags must come before '--'; one placed after it would parse as a
        second positional argument and glab would reject the invocation.
        """
        captured = _stub_glab(monkeypatch, (0, "is valid", ""))

        CiLintHandler(simulate=True).lint(path=str(ci_file), ref="master")

        assert captured[0] == [
            "ci",
            "lint",
            "--dry-run",
            "--ref",
            "master",
            "--",
            str(ci_file),
        ]

    def test_explicit_path_is_the_one_sent_to_glab(self, ci_file: Path, monkeypatch) -> None:
        """The named file is linted, not the working-directory default.

        Without this the handler could validate .gitlab-ci.yml while reporting
        on the path the user actually asked about.
        """
        other = ci_file.parent / "other-ci.yml"
        other.write_text("job:\n  script:\n    - echo hi\n", encoding="utf-8")
        captured = _stub_glab(monkeypatch, (0, "is valid", ""))

        CiLintHandler().lint(path=str(other))

        assert captured[0] == ["ci", "lint", "--", str(other)]

    def test_flags_absent_when_not_requested(self, ci_file: Path, monkeypatch) -> None:
        """Default invocation carries neither flag."""
        captured = _stub_glab(monkeypatch, (0, "is valid", ""))

        CiLintHandler().lint(path=str(ci_file))

        assert "--dry-run" not in captured[0]
        assert "--ref" not in captured[0]

    def test_ref_without_simulate_is_rejected_before_any_call(
        self, ci_file: Path, monkeypatch
    ) -> None:
        """--ref outside a simulation is an error, not a silently ignored flag.

        glab only honours --ref when --dry-run is set. Forwarding it anyway
        would report a plain static check as if it had been validated against
        that branch.
        """
        captured = _stub_glab(monkeypatch, (0, "is valid", ""))

        with pytest.raises(ValueError, match="--dry-run"):
            CiLintHandler().lint(path=str(ci_file), ref="master")

        assert captured == []


@pytest.mark.integration
class TestToolFailureAgainstRealGlab:
    """Real `glab`, real failure shape — no stubbed boundary.

    The tests above pin the classification against triples we believe glab
    returns. That belief is exactly what was wrong: glab exits 1 for a tool
    failure just as it does for an invalid configuration, so a stubbed test
    would have re-encoded the bug's assumption and still passed. These run the
    binary and let it produce the failure itself.

    Both cases fail inside glab before any HTTP request, so they need no
    network and no GitLab credentials.
    """

    @staticmethod
    def _make_repo(tmp_path: Path, remote: Optional[str]) -> Path:
        """Create a git repo holding a valid CI file, optionally with a remote.

        Args:
            tmp_path: Directory to initialise the repository in.
            remote: Origin URL to configure, or None to leave it remote-less.

        Returns:
            Path to the CI file inside the new repository.
        """
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        if remote:
            subprocess.run(
                ["git", "-C", str(tmp_path), "remote", "add", "origin", remote],
                check=True,
            )
        target = tmp_path / ".gitlab-ci.yml"
        target.write_text("job:\n  script:\n    - echo hi\n", encoding="utf-8")
        return target

    @pytest.mark.skipif(shutil.which("glab") is None, reason="glab CLI not installed")
    def test_repo_with_no_remote_raises_rather_than_reporting_invalid(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """glab cannot resolve a project, so there is no verdict to report.

        The CI file is valid. Returning False here would tell the user their
        configuration is broken when glab never checked it.
        """
        target = self._make_repo(tmp_path, remote=None)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(PlatformError, match="no lint verdict"):
            CiLintHandler().lint(path=str(target))

    @pytest.mark.skipif(shutil.which("glab") is None, reason="glab CLI not installed")
    def test_non_gitlab_remote_raises_rather_than_reporting_invalid(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The originally observed failure: a GitHub remote made glab exit 1."""
        target = self._make_repo(tmp_path, remote="https://github.com/example/example.git")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(PlatformError, match="no lint verdict"):
            CiLintHandler().lint(path=str(target))
