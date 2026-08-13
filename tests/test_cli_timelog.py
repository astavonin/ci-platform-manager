"""Tests for the CLI-level 'timelog' command (projctl.cli.cmd_timelog)."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from projctl.cli import cmd_timelog, main
from projctl.exceptions import PlatformError


def _args(**overrides) -> SimpleNamespace:
    """Build an args namespace with every field cmd_timelog reads."""
    defaults = {"date": None, "to": None, "config": None}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture(name="handler_cls")
def handler_cls_fixture():
    """Patch Config (defaulting to gitlab) and TimelogHandler; yield the handler class mock."""
    with patch("projctl.cli.Config") as mock_config_cls, patch(
        "projctl.cli.TimelogHandler"
    ) as mock_handler_cls:
        mock_config_cls.return_value.platform = "gitlab"
        yield mock_handler_cls


class TestPlatformGate:
    """cmd_timelog rejects non-GitLab platforms before touching the handler."""

    def test_github_platform_returns_one(self) -> None:
        """A github platform config exits 1 and names the config setting, not a glab transport error."""
        with patch("projctl.cli.Config") as mock_config_cls, patch(
            "projctl.cli.TimelogHandler"
        ) as mock_handler_cls:
            mock_config_cls.return_value.platform = "github"

            rc = cmd_timelog(_args())

            assert rc == 1
            mock_handler_cls.assert_not_called()

    def test_github_platform_error_names_command_and_gitlab(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The rejection message names the 'timelog' command, matching the cmd_note precedent."""
        with patch("projctl.cli.Config") as mock_config_cls, patch("projctl.cli.TimelogHandler"):
            mock_config_cls.return_value.platform = "github"

            with caplog.at_level("ERROR"):
                cmd_timelog(_args())

            assert "timelog" in caplog.text
            assert "GitLab" in caplog.text

    def test_gitlab_platform_reaches_handler(self, handler_cls: Mock) -> None:
        """A gitlab platform config constructs and calls the handler."""
        rc = cmd_timelog(_args())

        assert rc == 0
        handler_cls.assert_called_once_with()
        handler_cls.return_value.report.assert_called_once_with(None, None)


class TestArgumentPassthrough:
    """cmd_timelog forwards the parsed date/--to fields to handler.report unchanged."""

    def test_single_date_forwarded(self, handler_cls: Mock) -> None:
        """A bare date positional is forwarded as (date, None)."""
        rc = cmd_timelog(_args(date="2026-08-05"))

        assert rc == 0
        handler_cls.return_value.report.assert_called_once_with("2026-08-05", None)

    def test_date_and_to_forwarded(self, handler_cls: Mock) -> None:
        """date plus --to are forwarded as (date, to)."""
        rc = cmd_timelog(_args(date="2026-08-05", to="2026-08-12"))

        assert rc == 0
        handler_cls.return_value.report.assert_called_once_with("2026-08-05", "2026-08-12")


class TestErrorHandling:
    """Handler failures become a non-zero exit code, not a traceback."""

    def test_platform_error_returns_one(
        self, handler_cls: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A PlatformError from the handler (e.g. null currentUser) exits 1 and is logged."""
        handler_cls.return_value.report.side_effect = PlatformError("currentUser returned null")

        with caplog.at_level("ERROR"):
            rc = cmd_timelog(_args(date="2026-08-05"))

        assert rc == 1
        # The diagnostic must reach the operator, not just the exit code — a
        # silent exit 1 is the exact ambiguity this command exists to remove.
        assert "currentUser returned null" in caplog.text

    def test_value_error_returns_one(
        self, handler_cls: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ValueError from the handler (e.g. bad date) exits 1 and is logged."""
        handler_cls.return_value.report.side_effect = ValueError("Invalid date")

        with caplog.at_level("ERROR"):
            rc = cmd_timelog(_args(date="not-a-date"))

        assert rc == 1
        assert "Invalid date" in caplog.text


class TestAbsentConfigFallback:
    """No config file anywhere must not make 'timelog' unusable.

    Regression: timelog needs no config at all (TimelogHandler takes none —
    no default_group or project scope to resolve), and it is GitLab-only by
    nature; the host guard inside TimelogHandler.report() already fails
    loudly on a non-GitLab host. But cmd_timelog constructed a Config()
    purely to read config.platform, and Config(None) raises FileNotFoundError
    when the auto-search order finds nothing anywhere on the machine — so the
    command died with "No config file found" in every directory with no
    projctl.yaml or user-wide config, including an authenticated, GitLab-remote
    repository where the query would otherwise have succeeded.
    """

    def test_no_config_anywhere_without_explicit_flag_still_runs(self) -> None:
        """No config file found and no --config given: the command still runs."""
        with patch(
            "projctl.cli.Config",
            side_effect=FileNotFoundError(
                "No config file found. Searched:\n  - ./projctl.yaml\n  - ~/.config/projctl/config.yaml"
            ),
        ), patch("projctl.cli.TimelogHandler") as mock_handler_cls:
            rc = cmd_timelog(_args(date="2026-08-05"))

        assert rc == 0
        mock_handler_cls.assert_called_once_with()
        mock_handler_cls.return_value.report.assert_called_once_with("2026-08-05", None)

    def test_explicit_missing_config_path_is_still_a_hard_error(self) -> None:
        """An explicitly-named --config path that does not exist is never swallowed."""
        with patch(
            "projctl.cli.Config",
            side_effect=FileNotFoundError("Config file not found: /nonexistent/projctl.yaml"),
        ), patch("projctl.cli.TimelogHandler") as mock_handler_cls:
            rc = cmd_timelog(_args(config="/nonexistent/projctl.yaml"))

        assert rc == 1
        mock_handler_cls.assert_not_called()

    def test_real_config_search_finding_nothing_on_disk_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integration-level: the real Config auto-search, not a mock, against an empty environment.

        The unit tests above mock Config's FileNotFoundError directly; this
        exercises the actual boundary the bug crossed — Config's real
        config_search_paths() resolution against a filesystem that genuinely
        has no projctl.yaml, glab_config.yaml, or user-wide config anywhere.
        """
        empty_home = tmp_path / "home"
        empty_cwd = tmp_path / "cwd"
        empty_home.mkdir()
        empty_cwd.mkdir()
        monkeypatch.setattr(Path, "home", lambda: empty_home)
        monkeypatch.chdir(empty_cwd)

        with patch("projctl.cli.TimelogHandler") as mock_handler_cls:
            rc = cmd_timelog(_args(date="2026-08-05"))

        assert rc == 0
        mock_handler_cls.assert_called_once_with()
        mock_handler_cls.return_value.report.assert_called_once_with("2026-08-05", None)


class TestArgparseWiring:
    """Exercises the real parser built by _add_timelog_subparser(), not a hand-rolled args object.

    Every other test in this module builds a SimpleNamespace by hand, so an
    argparse dest mismatch (e.g. the 'date' positional or '--to' option
    landing under a different attribute name) would be invisible to them.
    """

    def test_real_parser_wires_date_and_to_to_handler_report(self) -> None:
        """An argparse dest mismatch would leave handler.report() called with the wrong values."""
        with patch("projctl.cli.Config") as mock_config_cls, patch(
            "projctl.cli.TimelogHandler"
        ) as mock_handler_cls:
            mock_config_cls.return_value.platform = "gitlab"

            rc = main(["timelog", "2026-08-05", "--to", "2026-08-12"])

        assert rc == 0
        mock_handler_cls.return_value.report.assert_called_once_with("2026-08-05", "2026-08-12")

    def test_real_parser_defaults_date_and_to_to_none(self) -> None:
        """The bare 'timelog' subcommand with no positional or --to defaults both to None."""
        with patch("projctl.cli.Config") as mock_config_cls, patch(
            "projctl.cli.TimelogHandler"
        ) as mock_handler_cls:
            mock_config_cls.return_value.platform = "gitlab"

            rc = main(["timelog"])

        assert rc == 0
        mock_handler_cls.return_value.report.assert_called_once_with(None, None)

    def test_real_handler_end_to_end_reports_and_warns_at_the_default_log_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """argv -> main() -> a real TimelogHandler -> a mocked transport, with neither Config
        nor TimelogHandler mocked out.

        Every other test in this module mocks TimelogHandler wholesale, so
        none of them exercise the real handler's tz=None production path or
        main()'s own logging.basicConfig(level=...) call — every handler
        test that checks a logged warning forces the level via
        caplog.at_level instead. A server/local total mismatch warning
        reaching stderr here, under main()'s own default setup with no
        caplog override, is the cheapest single test that closes both gaps
        plus the config-auto-search path already covered by
        TestAbsentConfigFallback.
        """
        empty_home = tmp_path / "home"
        empty_cwd = tmp_path / "cwd"
        empty_home.mkdir()
        empty_cwd.mkdir()
        monkeypatch.setattr(Path, "home", lambda: empty_home)
        monkeypatch.chdir(empty_cwd)

        current_user = json.dumps({"data": {"currentUser": {"username": "astavonin"}}})
        timelogs = json.dumps(
            {
                "data": {
                    "timelogs": {
                        "totalSpentTime": "18000",
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "spentAt": "2026-08-05T09:00:00Z",
                                "timeSpent": 3600,
                                "issue": {"iid": 1, "title": "Fix bug"},
                                "mergeRequest": None,
                                "project": {"name": "proj", "fullPath": "group/proj"},
                            }
                        ],
                    }
                }
            }
        )

        # logging.basicConfig() only configures the root logger on its first
        # effective call in the process; a handler left by some earlier test
        # in the suite would make main()'s own call here a no-op. Clearing
        # handlers first forces main()'s call to actually apply, regardless
        # of what ran before it.
        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_level = root_logger.level
        # The handler-clearing mutation below must be inside the try: if it
        # raised before the try started, the finally block would never run
        # and this test would permanently mutate the root logger for the
        # rest of the suite.
        try:
            for handler in original_handlers:
                root_logger.removeHandler(handler)
            with patch("projctl.handlers.timelog.run_glab_command") as mock_run:
                mock_run.side_effect = [current_user, timelogs]
                rc = main(["timelog", "2026-08-05"])
        finally:
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            for handler in original_handlers:
                root_logger.addHandler(handler)
            root_logger.setLevel(original_level)

        assert rc == 0
        captured = capsys.readouterr()
        assert "Total: 1h across 1 day(s), 1 entry" in captured.out
        # 18000s = 5h is the server total, 3600s = 1h is the local row sum —
        # main()'s own basicConfig(level=WARNING) must surface this, not a
        # caplog override; a regression to level=ERROR would drop it.
        assert "server-computed total (5h) differs from the sum of this report's rows (1h)" in (
            captured.err
        )
