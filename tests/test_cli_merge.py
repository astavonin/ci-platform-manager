"""Tests for the CLI-level 'merge' command (projctl.cli.cmd_merge_dispatch)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from projctl.cli import cmd_merge_dispatch, main
from projctl.exceptions import PlatformError
from projctl.handlers.merge import MergeBlocked

_PATCH_CONFIG = "projctl.cli.Config"
_PATCH_HANDLER = "projctl.handlers.merge.MergeHandler"


def _args(**overrides) -> SimpleNamespace:
    """Build an args namespace with every field cmd_merge reads."""
    defaults = {
        "config": None,
        "mr": ["264"],
        "dry_run": False,
        "allow_unresolved": False,
        "allow_failed_pipeline": False,
        "keep_branch": False,
        "squash": False,
        "wait": False,
        "rebase": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture(name="handler_cls")
def handler_cls_fixture():
    """Patch MergeHandler where cmd_merge looks it up; yield the class mock."""
    with patch(_PATCH_CONFIG) as cfg:
        cfg.return_value.platform = "gitlab"
        with patch(_PATCH_HANDLER) as mock_cls:
            yield mock_cls


class TestPlatformGate:
    """Merging is GitLab-only; GitHub must be refused before any API call."""

    def test_github_is_rejected(self) -> None:
        with patch(_PATCH_CONFIG) as cfg:
            cfg.return_value.platform = "github"
            with patch(_PATCH_HANDLER) as handler_cls:
                assert cmd_merge_dispatch(_args()) == 1
                handler_cls.assert_not_called()


class TestRouting:
    """One MR merges directly; several go through the chain sequencer."""

    def test_single_ref_merges_one(self, handler_cls: MagicMock) -> None:
        assert cmd_merge_dispatch(_args(mr=["264"])) == 0

        handler_cls.return_value.merge_one.assert_called_once()
        handler_cls.return_value.merge_chain.assert_not_called()

    def test_several_refs_merge_as_a_chain(self, handler_cls: MagicMock) -> None:
        handler_cls.return_value.merge_chain.return_value = 0

        assert cmd_merge_dispatch(_args(mr=["264", "263"])) == 0

        handler_cls.return_value.merge_chain.assert_called_once()
        handler_cls.return_value.merge_one.assert_not_called()

    def test_chain_exit_code_is_propagated(self, handler_cls: MagicMock) -> None:
        """A partially merged chain must not report success."""
        handler_cls.return_value.merge_chain.return_value = 1

        assert cmd_merge_dispatch(_args(mr=["264", "263"])) == 1

    def test_dry_run_reports_and_merges_nothing(self, handler_cls: MagicMock) -> None:
        handler_cls.return_value.report.return_value = 0

        assert cmd_merge_dispatch(_args(mr=["264", "263"], dry_run=True)) == 0

        handler_cls.return_value.report.assert_called_once()
        handler_cls.return_value.merge_one.assert_not_called()
        handler_cls.return_value.merge_chain.assert_not_called()


class TestFlagForwarding:
    """A flag that does not reach the handler silently does nothing."""

    def test_keep_branch_inverts_into_remove_branch(self, handler_cls: MagicMock) -> None:
        cmd_merge_dispatch(_args(keep_branch=True, squash=True))

        kwargs = handler_cls.return_value.merge_one.call_args.kwargs
        assert kwargs["remove_branch"] is False
        assert kwargs["squash"] is True

    def test_branch_is_removed_by_default(self, handler_cls: MagicMock) -> None:
        cmd_merge_dispatch(_args())

        assert handler_cls.return_value.merge_one.call_args.kwargs["remove_branch"] is True

    def test_waivers_reach_the_handler(self, handler_cls: MagicMock) -> None:
        cmd_merge_dispatch(_args(allow_unresolved=True, allow_failed_pipeline=True))

        kwargs = handler_cls.return_value.merge_one.call_args.kwargs
        assert kwargs["allow_unresolved"] is True
        assert kwargs["allow_failed_pipeline"] is True

    def test_rebase_and_wait_are_set_on_the_handler(self, handler_cls: MagicMock) -> None:
        """These two are handler state, not merge_one arguments."""
        cmd_merge_dispatch(_args(rebase=True, wait=True))

        handler = handler_cls.return_value
        assert handler.rebase_between is True
        assert handler.wait_pipeline is True


class TestFailureExitCodes:
    """A blocked or failed merge must exit non-zero."""

    def test_blocked_merge_exits_one(self, handler_cls: MagicMock, capsys) -> None:
        handler_cls.return_value.merge_one.side_effect = MergeBlocked("MR !264 is a draft")

        assert cmd_merge_dispatch(_args()) == 1
        assert "is a draft" in capsys.readouterr().out

    def test_bad_reference_exits_one(self, handler_cls: MagicMock) -> None:
        handler_cls.return_value.merge_one.side_effect = ValueError("Invalid MR reference")

        assert cmd_merge_dispatch(_args(mr=["nonsense"])) == 1

    def test_api_failure_exits_one(self, handler_cls: MagicMock) -> None:
        handler_cls.return_value.merge_one.side_effect = PlatformError("glab exploded")

        assert cmd_merge_dispatch(_args()) == 1


class TestParserWiring:
    """The subcommand must be registered and require at least one MR."""

    def test_merge_subcommand_dispatches_to_handler(self, handler_cls: MagicMock) -> None:
        handler_cls.return_value.report.return_value = 0

        assert main(["merge", "264", "--dry-run"]) == 0
        handler_cls.return_value.report.assert_called_once()

    def test_bare_merge_is_rejected_by_the_parser(self) -> None:
        with pytest.raises(SystemExit):
            main(["merge"])
