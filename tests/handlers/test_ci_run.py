"""Tests for the CI pipeline trigger."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from projctl.exceptions import PlatformError
from projctl.handlers.ci_run import CiRunHandler, cmd_ci_run

_PATCH_JSON = "projctl.handlers.ci_run.run_glab_json"
_PATCH_RUN = "projctl.utils.git_helpers.subprocess.run"
_PATCH_SLEEP = "projctl.handlers.ci_run.time.sleep"


def _pipeline(**over) -> dict:
    base = {"id": 7, "status": "created", "web_url": "https://gitlab/x/-/pipelines/7"}
    base.update(over)
    return base


def _args(**over):
    ns = MagicMock()
    ns.branch = over.get("branch")
    ns.variable = over.get("variable", [])
    ns.wait = over.get("wait", False)
    ns.dry_run = over.get("dry_run", False)
    return ns


class TestCurrentBranch:
    """The default ref comes from git, and must fail loudly when it cannot."""

    @patch(_PATCH_RUN)
    def test_branch_is_read_from_git(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="feature/x\n")

        assert CiRunHandler.current_branch() == "feature/x"

    @patch(_PATCH_RUN)
    def test_detached_head_raises(self, mock_run: MagicMock) -> None:
        """A detached HEAD has no branch to run a pipeline for."""
        mock_run.return_value = MagicMock(stdout="HEAD\n")

        with pytest.raises(PlatformError, match="detached"):
            CiRunHandler.current_branch()

    @patch(_PATCH_RUN, side_effect=subprocess.CalledProcessError(1, "git"))
    def test_git_failure_raises(self, _run: MagicMock) -> None:
        with pytest.raises(PlatformError, match="Failed to get current branch"):
            CiRunHandler.current_branch()


class TestVariables:
    """A dropped variable produces a pipeline that looks right and behaves differently."""

    def test_pairs_are_parsed(self) -> None:
        assert CiRunHandler._parse_variables(["A=1", "B=x=y"]) == [("A", "1"), ("B", "x=y")]

    def test_empty_value_is_allowed(self) -> None:
        assert CiRunHandler._parse_variables(["A="]) == [("A", "")]

    @pytest.mark.parametrize("bad", ["NOEQUALS", "=novalue"])
    def test_malformed_variable_raises(self, bad: str) -> None:
        with pytest.raises(ValueError, match="expected KEY=VALUE"):
            CiRunHandler._parse_variables([bad])


class TestTrigger:
    """Pipeline creation."""

    @patch(_PATCH_JSON)
    def test_dry_run_creates_nothing(self, mock_json: MagicMock, capsys) -> None:
        assert CiRunHandler(dry_run=True).trigger("master") is None

        assert "[dry-run] Would create a pipeline on master" in capsys.readouterr().out
        mock_json.assert_not_called()

    @patch(_PATCH_JSON)
    def test_post_targets_the_ref(self, mock_json: MagicMock, capsys) -> None:
        mock_json.return_value = _pipeline()

        CiRunHandler().trigger("feature/x")

        cmd = mock_json.call_args[0][0]
        assert "-X" in cmd and "POST" in cmd
        assert any(c.endswith("projects/:fullpath/pipeline?ref=feature/x") for c in cmd)
        out = capsys.readouterr().out
        assert "Created pipeline #7 on feature/x" in out
        assert "https://gitlab/x/-/pipelines/7" in out

    @patch(_PATCH_JSON)
    def test_variables_are_sent_as_an_indexed_array(self, mock_json: MagicMock) -> None:
        mock_json.return_value = _pipeline()

        CiRunHandler().trigger("master", ["HIL_ONLY=true", "DEBUG=1"])

        cmd = mock_json.call_args[0][0]
        assert "variables[0][key]=HIL_ONLY" in cmd
        assert "variables[0][value]=true" in cmd
        assert "variables[1][key]=DEBUG" in cmd
        assert "variables[1][value]=1" in cmd

    @patch(_PATCH_JSON, return_value={"message": "403 Forbidden"})
    def test_response_without_an_id_raises(self, _json: MagicMock) -> None:
        """A body that is not a pipeline must not be reported as a created one."""
        with pytest.raises(PlatformError, match="Unexpected pipeline response"):
            CiRunHandler().trigger("master")


class TestWait:
    """--wait polls to a terminal status."""

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON)
    def test_polls_until_terminal(self, mock_json: MagicMock, _sleep: MagicMock) -> None:
        mock_json.side_effect = [
            _pipeline(status="running"),
            _pipeline(status="running"),
            _pipeline(status="success"),
        ]

        assert CiRunHandler().wait(7) == "success"

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON, return_value=_pipeline(status="failed"))
    def test_failed_is_terminal(self, _json: MagicMock, _sleep: MagicMock) -> None:
        assert CiRunHandler().wait(7) == "failed"


class TestExitCodes:
    """'could not run' must never read as 'ran and failed'."""

    @patch(_PATCH_JSON)
    def test_created_without_wait_exits_zero(self, mock_json: MagicMock) -> None:
        mock_json.return_value = _pipeline()

        assert cmd_ci_run(_args(branch="master")) == 0

    @patch("projctl.handlers.ci_run.CiRunHandler.wait", return_value="success")
    @patch(_PATCH_JSON)
    def test_wait_success_exits_zero(self, mock_json: MagicMock, _wait: MagicMock) -> None:
        mock_json.return_value = _pipeline()

        assert cmd_ci_run(_args(branch="master", wait=True)) == 0

    @patch("projctl.handlers.ci_run.CiRunHandler.wait", return_value="failed")
    @patch(_PATCH_JSON)
    def test_wait_failure_exits_one(self, mock_json: MagicMock, _wait: MagicMock) -> None:
        mock_json.return_value = _pipeline()

        assert cmd_ci_run(_args(branch="master", wait=True)) == 1

    @patch(_PATCH_JSON, side_effect=PlatformError("token expired"))
    def test_creation_failure_exits_two(self, _json: MagicMock) -> None:
        assert cmd_ci_run(_args(branch="master")) == 2

    def test_malformed_variable_exits_two(self) -> None:
        assert cmd_ci_run(_args(branch="master", variable=["OOPS"])) == 2

    @patch(_PATCH_JSON)
    def test_dry_run_exits_zero_without_waiting(self, mock_json: MagicMock) -> None:
        assert cmd_ci_run(_args(branch="master", dry_run=True, wait=True)) == 0
        mock_json.assert_not_called()

    @patch("projctl.handlers.ci_run.CiRunHandler.current_branch", return_value="feature/y")
    @patch(_PATCH_JSON)
    def test_branch_defaults_to_the_checkout(
        self, mock_json: MagicMock, _branch: MagicMock
    ) -> None:
        mock_json.return_value = _pipeline()

        assert cmd_ci_run(_args()) == 0
        assert any("ref=feature/y" in c for c in mock_json.call_args[0][0])
