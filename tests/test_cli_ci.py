"""Tests for the CLI-level 'ci' command (projctl.cli.cmd_ci)."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from projctl.cli import cmd_ci, main
from projctl.exceptions import PlatformError


def _args(**overrides) -> SimpleNamespace:
    """Build an args namespace with every field cmd_ci reads."""
    defaults = {"ci_command": "lint", "path": None, "dry_run": False, "ref": None}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture(name="handler_cls")
def handler_cls_fixture():
    """Patch CiLintHandler in the cli namespace; yield the class mock."""
    with patch("projctl.cli.CiLintHandler") as mock_cls:
        yield mock_cls


class TestExitCode:
    """The exit code is the contract — CI scripts branch on it.

    Three values, deliberately distinct: 0 valid, 1 rejected by GitLab, 2 could
    not be checked. Collapsing 1 and 2 would report a broken configuration
    every time a token expired.
    """

    def test_valid_configuration_exits_zero(self, handler_cls: Mock) -> None:
        """A configuration the linter accepts exits 0."""
        handler_cls.return_value.lint.return_value = True

        assert cmd_ci(_args()) == 0

    def test_invalid_configuration_exits_one(self, handler_cls: Mock) -> None:
        """A rejected configuration exits 1 rather than passing silently."""
        handler_cls.return_value.lint.return_value = False

        assert cmd_ci(_args()) == 1

    def test_tool_failure_exits_two_not_one(self, handler_cls: Mock) -> None:
        """A glab failure must not share an exit code with 'config invalid'."""
        handler_cls.return_value.lint.side_effect = PlatformError(
            "glab reported no lint verdict (exit 1): ERROR: 401 Unauthorized"
        )

        assert cmd_ci(_args()) == 2

    def test_missing_glab_exits_two(self, handler_cls: Mock) -> None:
        """An uninstalled glab exits 2 instead of raising PlatformError."""
        handler_cls.return_value.lint.side_effect = PlatformError("glab command not found")

        assert cmd_ci(_args()) == 2

    def test_rejected_flag_combination_exits_two(self, handler_cls: Mock) -> None:
        """The handler's ValueError for --ref without --dry-run exits 2."""
        handler_cls.return_value.lint.side_effect = ValueError("--ref only applies with --dry-run")

        assert cmd_ci(_args(ref="master")) == 2

    def test_unknown_subcommand_exits_two(self, handler_cls: Mock) -> None:
        """An unrecognised ci subcommand exits 2 without invoking the handler."""
        assert cmd_ci(_args(ci_command="bogus")) == 2
        handler_cls.assert_not_called()


class TestArgumentForwarding:
    """Parsed flags reach the handler unchanged."""

    def test_path_and_ref_reach_the_handler(self, handler_cls: Mock) -> None:
        """--dry-run maps to the handler's simulate flag; path and --ref go to lint()."""
        handler_cls.return_value.lint.return_value = True

        cmd_ci(_args(path="ci/.gitlab-ci.yml", dry_run=True, ref="master"))

        handler_cls.assert_called_once_with(simulate=True)
        handler_cls.return_value.lint.assert_called_once_with(
            path="ci/.gitlab-ci.yml", ref="master"
        )


class TestParserWiring:
    """The 'ci' subparser is reachable and dispatches to cmd_ci."""

    def test_lint_subcommand_dispatches_to_handler(self, handler_cls: Mock) -> None:
        """`projctl ci lint <path>` reaches the handler with the parsed path."""
        handler_cls.return_value.lint.return_value = True

        assert main(["ci", "lint", "ci/.gitlab-ci.yml"]) == 0
        handler_cls.return_value.lint.assert_called_once_with(path="ci/.gitlab-ci.yml", ref=None)

    def test_run_subcommand_dispatches_to_the_pipeline_trigger(
        self, handler_cls: Mock
    ) -> None:
        """`ci run` must reach cmd_ci_run, not the linter."""
        with patch("projctl.cli.cmd_ci_run", return_value=0) as mock_run:
            assert main(["ci", "run", "--branch", "master"]) == 0

        assert mock_run.call_args[0][0].branch == "master"
        handler_cls.assert_not_called()

    def test_run_exit_code_is_propagated(self, handler_cls: Mock) -> None:
        """A pipeline that ran and failed exits 1, not 0."""
        with patch("projctl.cli.cmd_ci_run", return_value=1):
            assert main(["ci", "run", "--wait"]) == 1

        handler_cls.assert_not_called()

    def test_bare_ci_is_rejected_by_the_parser(self, handler_cls: Mock) -> None:
        """`projctl ci` with no subcommand is an argparse usage error.

        Without a required subcommand argparse would accept it and dispatch
        with ci_command=None, turning a typo into a confusing runtime message.
        """
        with pytest.raises(SystemExit) as exit_info:
            main(["ci"])

        assert exit_info.value.code == 2
        handler_cls.assert_not_called()
