"""Tests for projctl.handlers.timelog module."""

import json
import os
import re
import time as time_module
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Set
from unittest.mock import MagicMock, patch

import pytest

from projctl.exceptions import PlatformError
from projctl.handlers.timelog import (
    _CURRENT_USER_QUERY,
    _ISSUE_GLOBAL_ID_QUERY,
    _MAX_PAGES,
    _MR_GLOBAL_ID_QUERY,
    _TIMELOG_CREATE_MUTATION,
    _TIMELOGS_QUERY,
    TimelogHandler,
    _format_duration,
    _format_utc_offset,
    _parse_spent_at,
    _project_identity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_PATH = "projctl.handlers.timelog.run_glab_command"

# UTC+6, matching the offset used throughout the research corpus this
# feature was designed against.
_FIXED_TZ = timezone(timedelta(hours=6))

# The frozen reference instant _make_handler() sets as the write path's "now"
# by default. Every fixed --date fixture used with add() elsewhere in this
# module ("2026-08-05") is a date strictly before this one, so under the
# default it is always in the past relative to "now" — the clamp introduced
# for the future-date/today-clamp fix never fires for those tests, and their
# spentAt assertions (bare _local_noon(day), unclamped) stay valid regardless
# of the real wall-clock date the suite happens to run under. Tests that
# specifically exercise "today" or "the future" override handler._now
# themselves rather than relying on this default.
_FIXED_NOW = datetime(2026, 8, 20, 15, 0, 0, tzinfo=_FIXED_TZ)

# Tests that inject a named zone (e.g. "Asia/Dhaka") via the system_tz
# fixture depend on the host having the IANA tzdata to resolve it — glibc
# silently falls back to UTC for an unresolvable zone rather than raising,
# which would make the test's own assumption (the requested offset actually
# took effect) fail unhelpfully deep inside a date-bucketing assertion
# instead of a clear "tzdata missing" skip.
_HAS_TZDATA = os.path.isdir("/usr/share/zoneinfo")
_REQUIRES_TZDATA = pytest.mark.skipif(not _HAS_TZDATA, reason="host has no IANA tzdata")


def _adjacent_pair_present(cmd: list, pair: list) -> bool:
    """Return True if `pair` appears as a contiguous subsequence of `cmd`.

    A bare `"x=y" in cmd` substring check passes even when the flag ahead of
    it (-f vs -F) is wrong — the safety property under test is which flag
    the value travels on, not merely that the value is present somewhere.
    """
    n = len(pair)
    return any(cmd[i : i + n] == pair for i in range(len(cmd) - n + 1))


@pytest.fixture(name="system_tz")
def system_tz_fixture(monkeypatch: pytest.MonkeyPatch):
    """Yield a setter that overrides the process TZ, restoring the exact prior state on teardown.

    monkeypatch alone is not enough here: it restores os.environ["TZ"] at
    its own teardown, but never calls time.tzset() again afterward, so the
    C library's cached zone stays wherever the test last set it — diverging
    from $TZ for the rest of the process and making any later system-tz-
    dependent test order-dependent. Snapshotting the pre-test value and
    calling tzset() again after restoring it (bypassing monkeypatch's own
    restore, which would just leave the same gap) closes that.
    """
    original = os.environ.get("TZ")

    def _set(zone: str) -> None:
        monkeypatch.setenv("TZ", zone)
        time_module.tzset()

    yield _set

    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time_module.tzset()


def _current_user_response(username: str = "astavonin") -> str:
    return json.dumps({"data": {"currentUser": {"username": username}}})


def _timelogs_response(nodes, has_next_page=False, end_cursor=None, total_spent_time=None) -> str:
    if total_spent_time is None:
        total_spent_time = str(sum(n.get("timeSpent", 0) for n in nodes))
    return json.dumps(
        {
            "data": {
                "timelogs": {
                    "totalSpentTime": total_spent_time,
                    "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                    "nodes": nodes,
                }
            }
        }
    )


def _project(name="proj", full_path="group/proj"):
    return {"name": name, "fullPath": full_path}


def _issue_node(
    iid=1, title="Fix bug", time_spent=3600, spent_at="2026-08-05T09:00:00Z", project=None
):
    return {
        "spentAt": spent_at,
        "timeSpent": time_spent,
        "issue": {"iid": iid, "title": title},
        "mergeRequest": None,
        "project": project or _project(),
    }


def _mr_node(
    iid=7, title="Add feature", time_spent=1800, spent_at="2026-08-05T10:00:00Z", project=None
):
    return {
        "spentAt": spent_at,
        "timeSpent": time_spent,
        "issue": None,
        "mergeRequest": {"iid": iid, "title": title},
        "project": project or _project(),
    }


def _bare_project_node(time_spent=900, spent_at="2026-08-05T11:00:00Z", project=None):
    return {
        "spentAt": spent_at,
        "timeSpent": time_spent,
        "issue": None,
        "mergeRequest": None,
        "project": project or _project(),
    }


def _make_handler() -> TimelogHandler:
    handler = TimelogHandler(tz=_FIXED_TZ)
    # Freeze "now" so add()'s future-date rejection and today-clamp are
    # deterministic regardless of the real wall clock the suite runs under
    # — see _FIXED_NOW's own comment for why this default is safe for every
    # existing fixed --date value used with add() in this module.
    handler._now = lambda: _FIXED_NOW  # pylint: disable=protected-access
    return handler


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    """Tests for the seconds-to-human duration formatter."""

    def test_whole_hours(self) -> None:
        """A whole-hour value renders without a minutes component."""
        assert _format_duration(28800) == "8h"

    def test_hours_and_minutes(self) -> None:
        """A sub-hour remainder is appended after the hours component."""
        assert _format_duration(5400) == "1h 30m"

    def test_sub_hour_only(self) -> None:
        """A duration under one hour renders as minutes only."""
        assert _format_duration(2700) == "45m"

    def test_zero_seconds(self) -> None:
        """Zero seconds renders as '0m' rather than an empty string."""
        assert _format_duration(0) == "0m"

    def test_sub_minute_nonzero_renders_as_seconds(self) -> None:
        """59 seconds renders as '59s' rather than flooring to '0m' — a row's displayed value
        must not disagree with the day total it sums into."""
        assert _format_duration(59) == "59s"

    def test_negative_sub_minute_duration_renders_as_seconds(self) -> None:
        """A negative sub-minute correction renders with a leading minus and exact seconds."""
        assert _format_duration(-45) == "-45s"

    def test_sub_hour_with_seconds_remainder(self) -> None:
        """A duration with both a minutes and a seconds component shows both, not just minutes."""
        assert _format_duration(61) == "1m 1s"

    def test_negative_sub_hour_duration(self) -> None:
        """A negative correction under one hour renders with a leading minus, no borrowed hour."""
        assert _format_duration(-1800) == "-30m"

    def test_negative_duration_with_hours_and_minutes(self) -> None:
        """A negative correction spanning hours and minutes keeps both components positive."""
        assert _format_duration(-5400) == "-1h 30m"

    def test_negative_one_minute_duration(self) -> None:
        """-60 seconds renders as '-1m', not '-1h 59m' (naive divmod on a negative would borrow)."""
        assert _format_duration(-60) == "-1m"


class TestFormatUtcOffset:
    """Tests for the UTC-offset formatter used on the 'Queried window' header line."""

    def test_positive_offset(self) -> None:
        """A positive offset renders with a leading '+'."""
        moment = datetime(2026, 8, 5, tzinfo=timezone(timedelta(hours=6)))
        assert _format_utc_offset(moment) == "+06:00"

    def test_negative_offset(self) -> None:
        """A negative offset renders with a leading '-', not '+' with a negated magnitude."""
        moment = datetime(2026, 8, 5, tzinfo=timezone(timedelta(hours=-5)))
        assert _format_utc_offset(moment) == "-05:00"

    def test_zero_offset(self) -> None:
        """UTC itself renders as '+00:00'."""
        moment = datetime(2026, 8, 5, tzinfo=timezone.utc)
        assert _format_utc_offset(moment) == "+00:00"

    def test_positive_offset_with_minutes(self) -> None:
        """A non-hour-aligned positive offset (e.g. India) renders its minutes component."""
        moment = datetime(2026, 8, 5, tzinfo=timezone(timedelta(hours=5, minutes=45)))
        assert _format_utc_offset(moment) == "+05:45"

    def test_negative_offset_with_minutes(self) -> None:
        """A non-hour-aligned negative offset (e.g. Newfoundland) renders its minutes component."""
        moment = datetime(2026, 8, 5, tzinfo=timezone(timedelta(hours=-3, minutes=-30)))
        assert _format_utc_offset(moment) == "-03:30"


class TestParseSpentAt:
    """Direct tests for the Z-suffix shim spentAt parsing relies on.

    datetime.fromisoformat() does not accept a trailing 'Z' until Python
    3.11; this project's declared floor is 3.7 (setup.py python_requires)
    and 3.8 (pyproject.toml [project] requires-python) — [tool.mypy]
    python_version = "3.9" is only mypy's type-checking target, not a
    minimum-supported-version declaration — so the shim is load-bearing
    under all three, not defensive padding.
    """

    def test_z_suffix_is_converted_to_a_utc_offset(self) -> None:
        """A GitLab 'Z'-suffixed timestamp parses to an aware UTC datetime."""
        result = _parse_spent_at("2026-08-05T09:00:00Z")
        assert result == datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc)

    def test_z_suffix_is_rewritten_to_an_explicit_offset_before_parsing(self) -> None:
        """The shim rewrites a trailing 'Z' to '+00:00' before calling fromisoformat().

        Asserted on the transformed string that actually reaches
        fromisoformat(), not just on the parsed result: Python's own
        fromisoformat() has accepted a bare 'Z' natively since 3.11, so on
        that interpreter a deleted shim would still produce an identical
        *output*, and an output-only assertion could not tell "shim ran"
        apart from "shim absent, native parser handled it anyway".
        """
        with patch("projctl.handlers.timelog.datetime") as mock_datetime:
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat

            _parse_spent_at("2026-08-05T09:00:00Z")

        mock_datetime.fromisoformat.assert_called_once_with("2026-08-05T09:00:00+00:00")

    def test_explicit_offset_is_parsed_without_modification(self) -> None:
        """A timestamp already carrying an explicit offset is parsed as-is."""
        result = _parse_spent_at("2026-08-05T09:00:00+06:00")
        assert result == datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone(timedelta(hours=6)))


class TestProjectIdentity:
    """Direct tests for the fullPath/name precedence and its 'unknown' fallback."""

    def test_full_path_takes_precedence_over_name(self) -> None:
        """fullPath wins when both fields are present."""
        assert _project_identity({"fullPath": "group/proj", "name": "proj"}) == "group/proj"

    def test_falls_back_to_name_when_full_path_is_absent(self) -> None:
        """name is used when fullPath is missing."""
        assert _project_identity({"name": "proj"}) == "proj"

    def test_falls_back_to_unknown_when_neither_field_is_present(self) -> None:
        """Neither field present renders as the literal 'unknown' identity."""
        assert _project_identity({}) == "unknown"


class TestLocalBound:
    """Tests for the injected-tz test seam in _local_bound()."""

    def test_no_injected_tz_produces_an_aware_system_local_datetime(self) -> None:
        """With no injected tz (the production path), the naive value gains the system's offset."""
        handler = TimelogHandler(tz=None)

        # pylint: disable=protected-access
        # Exercising the timezone seam directly is the only way to test it
        # without also depending on query-construction wiring.
        start = handler._local_bound(date(2026, 8, 5), end_of_day=False)
        # pylint: enable=protected-access

        assert start.tzinfo is not None
        assert start.replace(tzinfo=None) == datetime(2026, 8, 5, 0, 0, 0)

    @_REQUIRES_TZDATA
    def test_injected_tz_localizes_rather_than_converts_from_system_tz(self, system_tz) -> None:
        """The query window must reflect the injected tz, not whatever TZ the process runs under.

        Regression for a bug where an injected tz was passed through
        astimezone(), which treats a naive value as system-local first and
        then converts — shifting the window whenever the system tz and the
        injected tz disagree. Forcing TZ to a zone far from _FIXED_TZ makes
        that shift observable regardless of the machine running the suite.
        """
        system_tz("America/New_York")
        handler = TimelogHandler(tz=_FIXED_TZ)
        # pylint: disable=protected-access
        # Exercising the timezone seam directly is the only way to test
        # it without also depending on query-construction wiring.
        start = handler._local_bound(date(2026, 8, 5), end_of_day=False)
        end = handler._local_bound(date(2026, 8, 5), end_of_day=True)
        # pylint: enable=protected-access

        assert start.isoformat() == "2026-08-05T00:00:00+06:00"
        assert end.isoformat() == "2026-08-05T23:59:59.999999+06:00"


# ---------------------------------------------------------------------------
# current-user resolution / host-safety
# ---------------------------------------------------------------------------


class TestResolveCurrentUser:
    """Tests for the mandatory currentUser hard-error guard."""

    @patch(_PATCH_PATH)
    def test_null_current_user_raises_platform_error(self, mock_run: MagicMock) -> None:
        """A null currentUser (wrong-host symptom) is a hard error, not an empty report."""
        mock_run.return_value = json.dumps({"data": {"currentUser": None}})
        handler = _make_handler()

        with pytest.raises(PlatformError, match="currentUser returned null"):
            handler.report("2026-08-05")

        # The timelogs query must never run once currentUser fails to resolve.
        assert mock_run.call_count == 1

    @patch(_PATCH_PATH)
    def test_missing_username_key_raises_platform_error(self, mock_run: MagicMock) -> None:
        """currentUser present but without a username is also a hard error."""
        mock_run.return_value = json.dumps({"data": {"currentUser": {}}})
        handler = _make_handler()

        with pytest.raises(PlatformError, match="currentUser returned null"):
            handler.report("2026-08-05")

    @patch(_PATCH_PATH)
    def test_empty_string_username_raises_platform_error(self, mock_run: MagicMock) -> None:
        """An empty-string username is treated the same as a missing/null one — the guard is
        `if not username:`, which catches both, not `is None`, which would let "" through to
        be forwarded as a real GraphQL variable."""
        mock_run.return_value = _current_user_response(username="")
        handler = _make_handler()

        with pytest.raises(PlatformError, match="currentUser returned null"):
            handler.report("2026-08-05")

    @patch(_PATCH_PATH)
    def test_top_level_graphql_errors_raise(self, mock_run: MagicMock) -> None:
        """A top-level 'errors' array in a 200 response raises PlatformError."""
        mock_run.return_value = json.dumps({"errors": [{"message": "denied"}]})
        handler = _make_handler()

        with pytest.raises(PlatformError, match="denied"):
            handler.report("2026-08-05")

    @patch(_PATCH_PATH)
    def test_non_json_response_raises_platform_error(self, mock_run: MagicMock) -> None:
        """Non-JSON glab output raises PlatformError whose message names the actual response.

        The truncated snippet in the message is the entire supportability
        payload of this error — it is what tells an operator glab returned
        an HTML login page rather than JSON.
        """
        mock_run.return_value = "<html>error</html>"
        handler = _make_handler()

        with pytest.raises(PlatformError, match="Unexpected glab response") as exc_info:
            handler.report("2026-08-05")

        assert "<html>error</html>" in str(exc_info.value)

    @patch(_PATCH_PATH)
    def test_glab_failure_during_current_user_resolution_propagates(
        self, mock_run: MagicMock
    ) -> None:
        """A PlatformError from run_glab_command (e.g. glab missing) is not swallowed."""
        mock_run.side_effect = PlatformError("glab command not found. Please install glab CLI.")
        handler = _make_handler()

        with pytest.raises(PlatformError, match="glab command not found"):
            handler.report("2026-08-05")

    @patch(_PATCH_PATH)
    def test_json_array_response_raises_platform_error_not_a_traceback(
        self, mock_run: MagicMock
    ) -> None:
        """Valid JSON whose top level is not an object (e.g. a bare array) is rejected as a
        malformed response instead of crashing with an uncaught AttributeError from .get()."""
        mock_run.return_value = "[]"
        handler = _make_handler()

        with pytest.raises(PlatformError, match="Unexpected glab response"):
            handler.report("2026-08-05")


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


class TestPagination:
    """Tests for cursor-based pagination of the timelogs connection."""

    @patch(_PATCH_PATH)
    def test_multi_page_response_includes_every_entry(self, mock_run: MagicMock, capsys) -> None:
        """Nodes from every page are combined; the loop terminates on hasNextPage=False."""
        page1_nodes = [_issue_node(iid=i, time_spent=3600) for i in range(1, 4)]
        page2_nodes = [_issue_node(iid=i, time_spent=3600) for i in range(4, 6)]
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(page1_nodes, has_next_page=True, end_cursor="cursor-1"),
            _timelogs_response(page2_nodes, has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        # 1 currentUser call + 2 timelogs page calls.
        assert mock_run.call_count == 3
        second_page_cmd = mock_run.call_args_list[2][0][0]
        assert _adjacent_pair_present(second_page_cmd, ["-f", "after=cursor-1"])
        out = capsys.readouterr().out
        # All 5 nodes from both pages must reach the report, not just page 1's 3 —
        # replacing the accumulating extend() with a page-local assignment survives
        # the shape assertions above but would drop this count to "2 entries".
        assert "5 entries" in out

    @patch(_PATCH_PATH)
    def test_cursor_advances_past_the_second_page(self, mock_run: MagicMock) -> None:
        """A third page must request after=<the second page's cursor>, not the first page's
        cursor stuck in place. Every other multi-page fixture in this module is exactly two
        pages, so 'cursor = cursor or next_cursor' (freezing after the first advance) would
        survive them all — against a real server that shape re-serves page 2 forever."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_issue_node(iid=1)], has_next_page=True, end_cursor="c1"),
            _timelogs_response([_issue_node(iid=2)], has_next_page=True, end_cursor="c2"),
            _timelogs_response([_issue_node(iid=3)], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        assert mock_run.call_count == 4
        third_call_cmd = mock_run.call_args_list[2][0][0]
        fourth_call_cmd = mock_run.call_args_list[3][0][0]
        assert _adjacent_pair_present(third_call_cmd, ["-f", "after=c1"])
        assert _adjacent_pair_present(fourth_call_cmd, ["-f", "after=c2"])

    @patch(_PATCH_PATH)
    def test_single_page_stops_after_one_call(self, mock_run: MagicMock) -> None:
        """hasNextPage=False on the first page means no second request is made."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_issue_node()], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        assert mock_run.call_count == 2

    @patch(_PATCH_PATH)
    def test_never_terminates_on_count(self, mock_run: MagicMock, capsys) -> None:
        """A saturated 'count' field is ignored; only hasNextPage/endCursor drive the loop."""
        page1 = json.dumps(
            {
                "data": {
                    "timelogs": {
                        "count": 101,  # schema-documented "limit + 1" saturation
                        "totalSpentTime": "7200",
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                        "nodes": [_issue_node(iid=1, time_spent=3600)],
                    }
                }
            }
        )
        page2 = _timelogs_response([_issue_node(iid=2, time_spent=3600)], has_next_page=False)
        mock_run.side_effect = [_current_user_response(), page1, page2]
        handler = _make_handler()

        handler.report("2026-08-05")

        assert mock_run.call_count == 3
        out = capsys.readouterr().out
        assert "2 entries" in out

    @patch(_PATCH_PATH)
    def test_count_field_equal_to_one_does_not_stop_the_loop(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A saturated-looking count of 1 does not stop the loop; hasNextPage is authoritative.

        A count-bounded loop would behave identically to the correct one
        against the 'count: 101' fixture above (both already exceed any
        plausible bound); count: 1 is the value a count-bounded
        implementation would actually stop on.
        """
        page1 = json.dumps(
            {
                "data": {
                    "timelogs": {
                        "count": 1,
                        "totalSpentTime": "7200",
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                        "nodes": [_issue_node(iid=1, time_spent=3600)],
                    }
                }
            }
        )
        page2 = _timelogs_response([_issue_node(iid=2, time_spent=3600)], has_next_page=False)
        mock_run.side_effect = [_current_user_response(), page1, page2]
        handler = _make_handler()

        handler.report("2026-08-05")

        assert mock_run.call_count == 3
        out = capsys.readouterr().out
        assert "2 entries" in out

    @patch(_PATCH_PATH)
    def test_missing_cursor_with_has_next_page_raises_instead_of_truncating(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """hasNextPage=True without an endCursor fails closed rather than printing a partial report.

        Printing the truncated report at exit code 0 would be indistinguishable
        from a complete one — the loop must abort loudly instead.
        """
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_issue_node()], has_next_page=True, end_cursor=None),
        ]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="truncated node set"):
            handler.report("2026-08-05")

        # The loop must not retry indefinitely trying to recover a cursor.
        assert mock_run.call_count == 2
        # Nothing from _print_report must reach the terminal before the raise —
        # only the fetch step ran, so no partial report can have been printed.
        assert capsys.readouterr().out == ""

    @patch(_PATCH_PATH)
    def test_repeated_cursor_with_has_next_page_raises_instead_of_looping(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A server bug that repeats the same endCursor fails closed rather than looping forever."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_issue_node(iid=1)], has_next_page=True, end_cursor="c1"),
            _timelogs_response([_issue_node(iid=2)], has_next_page=True, end_cursor="c1"),
        ]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="repeated the same endCursor"):
            handler.report("2026-08-05")

        # Bounded: the second occurrence of "c1" raises rather than issuing a third request.
        assert mock_run.call_count == 3
        assert capsys.readouterr().out == ""

    @patch(_PATCH_PATH)
    def test_pagination_gives_up_after_the_page_cap_instead_of_looping_forever(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A server minting a fresh, never-repeated cursor on every page still terminates —
        seen_cursors alone only catches a *repeated* cursor, not this shape."""
        responses = [_current_user_response()]
        for i in range(_MAX_PAGES + 1):
            responses.append(
                _timelogs_response(
                    [_issue_node(iid=i)], has_next_page=True, end_cursor=f"cursor-{i}"
                )
            )
        mock_run.side_effect = responses
        handler = _make_handler()

        with pytest.raises(PlatformError, match="did not terminate"):
            handler.report("2026-08-05")

        # Matches its two hard-error siblings above: nothing from _print_report may
        # reach the terminal before the raise, since the fetch step never returns.
        assert capsys.readouterr().out == ""

    def test_max_pages_is_200(self) -> None:
        """Pins the literal cap so a change to it is a deliberate, reviewed edit — every test
        that paginates up to _MAX_PAGES imports the symbol, so a silent '200 -> 3' would leave
        them all green while quietly shrinking a real, reachable window (3 pages is 300
        timelogs, well within a legitimate multi-week query) down to a hard error."""
        assert _MAX_PAGES == 200

    @patch(_PATCH_PATH)
    def test_pagination_completes_at_exactly_the_page_cap(self, mock_run: MagicMock) -> None:
        """Exactly _MAX_PAGES pages must succeed — the guard is 'page_count > _MAX_PAGES', not
        '>=', so the last legitimate page must not itself trip the malfunctioning-server error."""
        responses = [_current_user_response()]
        for i in range(_MAX_PAGES - 1):
            responses.append(
                _timelogs_response(
                    [_issue_node(iid=i)], has_next_page=True, end_cursor=f"cursor-{i}"
                )
            )
        responses.append(_timelogs_response([_issue_node(iid=_MAX_PAGES)], has_next_page=False))
        mock_run.side_effect = responses
        handler = _make_handler()

        handler.report("2026-08-05")

        # 1 currentUser call + _MAX_PAGES timelogs page calls.
        assert mock_run.call_count == _MAX_PAGES + 1

    @patch(_PATCH_PATH)
    def test_graphql_errors_on_second_page_abort_rather_than_truncate(
        self, mock_run: MagicMock
    ) -> None:
        """A top-level 'errors' array on page 2 raises rather than silently stopping at page 1."""
        page1 = _timelogs_response([_issue_node(iid=1)], has_next_page=True, end_cursor="c1")
        page2_errors = json.dumps({"errors": [{"message": "internal server error"}]})
        mock_run.side_effect = [_current_user_response(), page1, page2_errors]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="internal server error"):
            handler.report("2026-08-05")

    @patch(_PATCH_PATH)
    def test_glab_failure_during_timelogs_fetch_propagates(self, mock_run: MagicMock) -> None:
        """A PlatformError raised while fetching timelogs (post currentUser) is not swallowed."""
        mock_run.side_effect = [_current_user_response(), PlatformError("glab auth failed")]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="glab auth failed"):
            handler.report("2026-08-05")


# ---------------------------------------------------------------------------
# anti-dedup guarantee
# ---------------------------------------------------------------------------


class TestAntiDedup:
    """Two identical same-issue, same-day entries must both be counted."""

    @patch(_PATCH_PATH)
    def test_identical_entries_are_both_counted(self, mock_run: MagicMock, capsys) -> None:
        """Duplicate-looking entries (same spentAt, same timeSpent, same issue) sum, not collapse."""
        duplicate = _issue_node(iid=42, time_spent=14400, spent_at="2026-08-05T09:39:52Z")
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([duplicate, dict(duplicate)], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        # 14400s * 2 = 28800s = 8h; a naive dict-keyed-by-issue collapse (or
        # node-level dedup in bucketing) would show 4h on both lines below.
        assert "2026-08-05 — 8h" in out
        assert "  #42 Fix bug — 8h" in out


# ---------------------------------------------------------------------------
# mergeRequest-only rendering
# ---------------------------------------------------------------------------


class TestTargetRendering:
    """Tests for how issue / MR / bare-project entries render."""

    @patch(_PATCH_PATH)
    def test_merge_request_only_entry_renders_identifiably(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A null-issue, mergeRequest-populated entry renders with an '!' MR marker, not blank."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_mr_node(iid=99, title="Add feature X")], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "!99 Add feature X" in out

    @patch(_PATCH_PATH)
    def test_node_missing_spent_at_is_skipped_not_crashed(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A node with no spentAt is defensively dropped from the day buckets, not a crash."""
        malformed = _issue_node(iid=5, time_spent=1800)
        del malformed["spentAt"]
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([malformed], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "No timelogs returned for this window." not in out
        # The entry count matches the node(s) actually shown, not the raw
        # fetch count — a "1 entry" here would contradict the warning's own
        # claim that 0 nodes were included in the breakdown.
        assert "0 day(s), 0 entries" in out
        # A fabricated-date fallback in place of the skip would render this row
        # inside a day section instead of dropping it.
        assert "#5" not in out

    @patch(_PATCH_PATH)
    def test_bare_project_entry_renders_via_the_disambiguated_label(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """An entry with neither issue nor mergeRequest set still renders, via the project's
        disambiguated label — here equal to the short project name, since a single-project
        window's only identity gets that label unconditionally (see
        TestCrossProjectDisambiguation.test_bare_project_rows_use_the_disambiguated_label for
        the multi-project case, where two identities can share a short name)."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_bare_project_node()], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "(no issue/MR) proj" in out

    @patch(_PATCH_PATH)
    def test_node_with_missing_time_spent_contributes_zero(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A node with no 'timeSpent' key at all defaults to 0 seconds rather than crashing on
        int(None) — the `or 0` half of that guard is otherwise never exercised."""
        malformed = _issue_node(iid=5)
        del malformed["timeSpent"]
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([malformed], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "#5 Fix bug — 0m" in out
        assert "Total: 0m across 1 day(s), 1 entry" in out

    @patch(_PATCH_PATH)
    def test_zero_time_spent_row_renders_as_zero_minutes(self, mock_run: MagicMock, capsys) -> None:
        """A node whose timeSpent is legitimately 0 (not absent) renders the same '0m' row as
        the missing-key case above, and does not disappear from the report."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_issue_node(iid=5, time_spent=0)], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "#5 Fix bug — 0m" in out
        assert "Total: 0m across 1 day(s), 1 entry" in out

    @patch(_PATCH_PATH)
    def test_empty_issue_title_renders_without_a_trailing_space(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """An empty issue title must not leave a trailing space after the IID from the
        unconditional f-string + .strip() in _target_label()."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(iid=5, title="", time_spent=1800)], has_next_page=False
            ),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "#5 — 30m" in out
        assert "#5  — 30m" not in out


# ---------------------------------------------------------------------------
# cross-project IID disambiguation
# ---------------------------------------------------------------------------


class TestCrossProjectDisambiguation:
    """GitLab IIDs are per-project, not global; the unscoped timelogs query spans every
    project the user has logged against, so the grouping key must include the project."""

    @patch(_PATCH_PATH)
    def test_same_iid_different_projects_produce_separate_rows(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Two different issues sharing an IID in different projects must not collapse into one row.

        This is the regression test for the defect: a key built from the IID
        alone would merge these into a single row with a 6h total and only
        one of the two titles surviving.
        """
        alpha_entry = _issue_node(
            iid=11,
            title="Alpha issue",
            time_spent=7200,
            project=_project("alpha", "group/alpha"),
        )
        beta_entry = _issue_node(
            iid=11,
            title="Beta issue",
            time_spent=14400,
            project=_project("beta", "group/beta"),
        )
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([alpha_entry, beta_entry], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        # Full-line anchors, not bare substrings: "group/alpha #11 ..." is a
        # superstring of "alpha #11 ...", so a broken _project_short_name()
        # that returns the unshortened identity would still pass a plain
        # `"alpha #11 ..." in out` check.
        assert "\n  alpha #11 Alpha issue — 2h\n" in out
        assert "\n  beta #11 Beta issue — 4h\n" in out
        assert "group/alpha" not in out
        assert "group/beta" not in out
        # A dropped-project key would collapse both into one 6h row under a single title.
        assert "Alpha issue — 6h" not in out
        assert "Beta issue — 6h" not in out

    @patch(_PATCH_PATH)
    def test_same_iid_same_project_still_merges_with_anti_dedup(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Same IID in the same project still merges into one row — the fix must not break this."""
        first = _issue_node(
            iid=11, title="Alpha issue", time_spent=7200, spent_at="2026-08-05T09:00:00Z"
        )
        second = _issue_node(
            iid=11, title="Alpha issue", time_spent=3600, spent_at="2026-08-05T10:00:00Z"
        )
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([first, second], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        # One merged row at 3h (2h + 1h), not two separate #11 rows.
        assert out.count("#11") == 1
        assert "#11 Alpha issue — 3h" in out

    @patch(_PATCH_PATH)
    def test_same_iid_merge_requests_across_projects_stay_distinct(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Two MRs sharing an IID in different projects must not collapse into one row — the
        mergeRequest branch of the grouping key must include the project identity exactly like
        the issue branch above does. Dropping the identity from that branch alone (leaving the
        issue branch correct) would still pass every issue-only cross-project test in this class.
        """
        alpha_mr = _mr_node(
            iid=5, title="Alpha MR", time_spent=10800, project=_project("alpha", "group/alpha")
        )
        beta_mr = _mr_node(
            iid=5, title="Beta MR", time_spent=7200, project=_project("beta", "group/beta")
        )
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([alpha_mr, beta_mr], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "\n  alpha !5 Alpha MR — 3h\n" in out
        assert "\n  beta !5 Beta MR — 2h\n" in out
        # A dropped-project key would collapse both into one row under a single title.
        assert out.count("!5") == 2

    @patch(_PATCH_PATH)
    def test_single_project_window_label_unchanged(self, mock_run: MagicMock, capsys) -> None:
        """A single-project window renders exactly as before — no project prefix."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(iid=42, title="Fix bug", time_spent=3600)], has_next_page=False
            ),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "  #42 Fix bug — 1h\n" in out
        assert "proj #42" not in out

    @patch(_PATCH_PATH)
    def test_multi_project_window_prefixes_every_row(self, mock_run: MagicMock, capsys) -> None:
        """Every row is prefixed once the window spans multiple projects.

        Includes a day whose own entries all belong to what would, taken
        alone, look like a single-project day — the determination is made
        over the whole window, not per day, so rows stay comparable across
        days.
        """
        alpha_entry = _issue_node(
            iid=1,
            title="Alpha work",
            time_spent=3600,
            spent_at="2026-08-05T09:00:00Z",
            project=_project("alpha", "group/alpha"),
        )
        beta_entry = _issue_node(
            iid=2,
            title="Beta work",
            time_spent=3600,
            spent_at="2026-08-06T09:00:00Z",
            project=_project("beta", "group/beta"),
        )
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([alpha_entry, beta_entry], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05", "2026-08-06")

        out = capsys.readouterr().out
        # Full-line anchors so a not-actually-shortened label can't slip
        # through as a substring of the unshortened one (see the sibling
        # cross-project test's comment for the exact mutation this guards).
        assert "\n  alpha #1 Alpha work — 1h\n" in out
        assert "\n  beta #2 Beta work — 1h\n" in out
        assert "group/alpha" not in out
        assert "group/beta" not in out

    @patch(_PATCH_PATH)
    def test_same_iid_issue_vs_merge_request_across_projects_stay_distinct(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Same IID, different projects, different target types (issue vs MR) never collide."""
        issue_entry = _issue_node(
            iid=5, title="Issue five", time_spent=3600, project=_project("alpha", "group/alpha")
        )
        mr_entry = _mr_node(
            iid=5, title="MR five", time_spent=1800, project=_project("beta", "group/beta")
        )
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([issue_entry, mr_entry], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "\n  alpha #5 Issue five — 1h\n" in out
        assert "\n  beta !5 MR five — 30m\n" in out
        assert "group/alpha" not in out
        assert "group/beta" not in out

    @patch(_PATCH_PATH)
    def test_same_iid_issue_vs_merge_request_same_project_stay_distinct(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Same IID, same project, different target types (issue vs MR) never collide.

        Regression for a grouping-key bug where the mergeRequest branch used
        the same key prefix as the issue branch — the cross-project sibling
        test above cannot catch this alone, since the project qualifier
        there carries the assertion even if the issue/MR prefix collapsed.
        """
        issue_entry = _issue_node(iid=5, title="Issue five", time_spent=3600)
        mr_entry = _mr_node(iid=5, title="MR five", time_spent=1800)
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([issue_entry, mr_entry], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "#5 Issue five — 1h" in out
        assert "!5 MR five — 30m" in out

    @patch(_PATCH_PATH)
    def test_projects_sharing_a_short_name_fall_back_to_full_identity(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Two projects whose fullPath ends in the same segment must not render identical prefixes.

        team-a/docs and team-b/docs both end in "docs"; the grouping key
        already keeps their rows separate, but a bare last-segment prefix
        would render them as visually identical text with different
        durations, defeating the prefix's entire purpose.
        """
        team_a_entry = _issue_node(
            iid=11, title="Docs task", time_spent=3600, project=_project("docs", "team-a/docs")
        )
        team_b_entry = _issue_node(
            iid=11, title="Docs task", time_spent=1800, project=_project("docs", "team-b/docs")
        )
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([team_a_entry, team_b_entry], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "team-a/docs #11 Docs task — 1h" in out
        assert "team-b/docs #11 Docs task — 30m" in out
        # The bare last-segment prefix is ambiguous between the two projects
        # and must not appear on its own.
        assert "\n  docs #11" not in out

    @patch(_PATCH_PATH)
    def test_day_total_reflects_every_row_not_just_the_first(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A day header's total is the sum of every row within it, not only the first entry."""
        alpha_entry = _issue_node(
            iid=1,
            title="Alpha work",
            time_spent=3600,
            spent_at="2026-08-05T09:00:00Z",
            project=_project("alpha", "group/alpha"),
        )
        beta_entry = _issue_node(
            iid=2,
            title="Beta work",
            time_spent=1800,
            spent_at="2026-08-05T10:00:00Z",
            project=_project("beta", "group/beta"),
        )
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([alpha_entry, beta_entry], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        # 3600 + 1800 = 5400s = 1h 30m; a first-node-only bug would show 1h.
        assert "2026-08-05 — 1h 30m" in out

    @patch(_PATCH_PATH)
    def test_bare_project_rows_use_the_disambiguated_label(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Two projects sharing a short name still render distinct bare-project rows.

        Regression for a grouping-key bug where the bare (no issue/MR)
        branch ignored project_labels entirely and rendered both rows as
        "(no issue/MR) docs" — identical text, different durations. This
        also exercises the bare-project grouping key: a broken key that
        collapsed to a constant would merge the two entries into a single
        summed row under only the first-seen label, which the per-row
        duration assertions below would catch.
        """
        team_a_entry = _bare_project_node(time_spent=3600, project=_project("docs", "team-a/docs"))
        team_b_entry = _bare_project_node(time_spent=1800, project=_project("docs", "team-b/docs"))
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([team_a_entry, team_b_entry], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "(no issue/MR) team-a/docs — 1h" in out
        assert "(no issue/MR) team-b/docs — 30m" in out
        assert out.count("(no issue/MR) docs") == 0

    @patch(_PATCH_PATH)
    def test_rows_within_a_day_render_in_first_seen_order(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Multiple targets in one day render in the order their first entry appeared, not sorted.

        The two grouping keys here sort in the opposite order from
        insertion ("issue:...:2" precedes "issue:...:9" alphabetically), so
        a mutation that iterated the totals in sorted-key order instead of
        first-seen order would still pass a same-day-total assertion but
        reorder these two lines.
        """
        first_seen = _issue_node(
            iid=9, title="Nine", time_spent=3600, spent_at="2026-08-05T09:00:00Z"
        )
        second_seen = _issue_node(
            iid=2, title="Two", time_spent=1800, spent_at="2026-08-05T10:00:00Z"
        )
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([first_seen, second_seen], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert out.index("#9 Nine") < out.index("#2 Two")


# ---------------------------------------------------------------------------
# zero-result reporting
# ---------------------------------------------------------------------------


class TestZeroResult:
    """A zero-result window must still print the window and identity queried."""

    @patch(_PATCH_PATH)
    def test_zero_result_day_prints_window_and_identity(self, mock_run: MagicMock, capsys) -> None:
        """Empty nodes print the window/identity header plus an explicit zero notice."""
        mock_run.side_effect = [
            _current_user_response("astavonin"),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "astavonin" in out
        assert "2026-08-05" in out
        assert "No timelogs returned for this window." in out
        # Must not phrase a misdirected-query-shaped zero as a confident "you logged nothing".
        assert "you logged nothing" not in out.lower()
        # The early return must be the only path out of a zero result — falling
        # through would additionally print a contradictory "Total: 0m across
        # 0 day(s), 0 entries" line right under the "no timelogs" notice.
        assert "Total:" not in out


# ---------------------------------------------------------------------------
# malformed-response fallbacks
# ---------------------------------------------------------------------------


class TestMalformedResponseFallbacks:
    """Every `X.get(k) or {}` guard in the parse path must degrade gracefully, not crash.

    Each case here is a distinct malformed shape a real (or buggy) GitLab
    response could produce, reached end to end through report() rather than
    by calling a private method directly — deleting any one of these guards
    survives the rest of the suite green and turns the malformed shape into
    an uncaught AttributeError/KeyError instead of the designed fallback.
    """

    @pytest.mark.parametrize(
        "shape",
        [
            pytest.param({"data": None}, id="data_key_is_null"),
            pytest.param({}, id="data_key_absent"),
            pytest.param({"data": {}}, id="timelogs_key_absent"),
            pytest.param(
                {"data": {"timelogs": {"totalSpentTime": "0", "nodes": []}}},
                id="pageInfo_key_absent",
            ),
        ],
    )
    @patch(_PATCH_PATH)
    def test_connection_level_malformation_reports_zero_results_not_a_crash(
        self, mock_run: MagicMock, capsys, shape
    ) -> None:
        """A malformed timelogs response degrades to the same zero-result report as a
        legitimately empty window, rather than raising an uncaught AttributeError/KeyError."""
        mock_run.side_effect = [_current_user_response(), json.dumps(shape)]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "No timelogs returned for this window." in out

    @patch(_PATCH_PATH)
    def test_node_without_project_key_falls_back_to_unknown_identity(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A node missing the 'project' key entirely (not merely project: null) still renders
        under the 'unknown' identity fallback, instead of crashing in _target_label()."""
        malformed = _issue_node(iid=5, time_spent=1800)
        del malformed["project"]
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([malformed], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "#5" in out
        assert "Total: 30m across 1 day(s), 1 entry" in out

    @patch(_PATCH_PATH)
    def test_non_numeric_total_spent_time_degrades_to_none_like_an_absent_key(
        self, mock_run: MagicMock, capsys, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A totalSpentTime that fails int() (e.g. a non-numeric string) must not raise — it
        degrades to the same 'nothing to cross-check against' behavior as an absent key,
        not a ValueError that kills the whole report."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(time_spent=3600)],
                has_next_page=False,
                total_spent_time="not-a-number",
            ),
        ]
        handler = _make_handler()

        with caplog.at_level("WARNING"):
            handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "Total: 1h across 1 day(s), 1 entry" in out
        assert "differs" not in caplog.text


# ---------------------------------------------------------------------------
# totalSpentTime as a JSON string
# ---------------------------------------------------------------------------


class TestTotalSpentTimeString:
    """BigInt serializes as a JSON string; the handler must cast it without raising."""

    @patch(_PATCH_PATH)
    def test_total_spent_time_string_cast_does_not_leak_into_the_printed_total(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A string totalSpentTime that disagrees with the local sum does not raise or get printed.

        The server value is used only as a cross-check (see
        TestGrandTotalReconciliation): a fixture where it always equals the
        node sum would not distinguish "cast, then used for the printed
        total" from "cast, then compared and discarded" — this one sets a
        deliberately mismatched string so only the latter can pass.
        """
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(time_spent=3600)], has_next_page=False, total_spent_time="999"
            ),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "Total: 1h" in out


# ---------------------------------------------------------------------------
# grand total reconciliation
# ---------------------------------------------------------------------------


class TestGrandTotalReconciliation:
    """The printed grand total is derived from the same nodes that produce the rows.

    The server's totalSpentTime is used only as a cross-check that warns on
    disagreement — never printed directly — since three independent paths
    (a filtered node, an absent key, or a stale per-page value) can make it
    diverge from what the rows actually show.
    """

    @patch(_PATCH_PATH)
    def test_total_reflects_local_sum_not_a_mismatched_server_value(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Server total 18000s / node total 7200s: the printed total follows the node sum."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(time_spent=7200)], has_next_page=False, total_spent_time="18000"
            ),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "Total: 2h across 1 day(s), 1 entry" in out

    @patch(_PATCH_PATH)
    def test_missing_total_spent_time_key_does_not_zero_the_total(
        self, mock_run: MagicMock, capsys, caplog: pytest.LogCaptureFixture
    ) -> None:
        """totalSpentTime absent from the response: the total is still the node sum, not 0m,
        and the absent key is not reported as a server-computed zero that disagrees — that
        would send an operator looking for missing timelogs rather than a malformed response."""
        nodes = [
            _issue_node(iid=1, time_spent=7200, spent_at="2026-08-05T09:00:00Z"),
            _issue_node(iid=2, time_spent=3600, spent_at="2026-08-05T10:00:00Z"),
        ]
        response = json.dumps(
            {
                "data": {
                    "timelogs": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": nodes,
                    }
                }
            }
        )
        mock_run.side_effect = [_current_user_response(), response]
        handler = _make_handler()

        with caplog.at_level("WARNING"):
            handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "Total: 3h across 1 day(s), 2 entries" in out
        assert "differs" not in caplog.text

    @patch(_PATCH_PATH)
    def test_total_sums_nodes_across_pages_regardless_of_per_page_server_total(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Two pages report different per-page totalSpentTime; the printed total sums all nodes."""
        page1_nodes = [_issue_node(iid=1, time_spent=10800, spent_at="2026-08-05T09:00:00Z")]
        page2_nodes = [_issue_node(iid=2, time_spent=3600, spent_at="2026-08-05T10:00:00Z")]
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                page1_nodes, has_next_page=True, end_cursor="c1", total_spent_time="10800"
            ),
            _timelogs_response(page2_nodes, has_next_page=False, total_spent_time="3600"),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        # 10800 + 3600 = 14400s = 4h, the sum of every node across both pages.
        assert "Total: 4h across 1 day(s), 2 entries" in out

    @patch(_PATCH_PATH)
    def test_first_page_total_wins_even_when_a_later_page_matches_the_local_sum(
        self, mock_run: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A page-1 divergence must still be reported even when a later page's totalSpentTime
        happens to agree with the local sum — taking the *last* page's value instead of the
        first would let that later agreement silently mask the earlier disagreement."""
        page1_nodes = [_issue_node(iid=1, time_spent=7200, spent_at="2026-08-05T09:00:00Z")]
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                page1_nodes, has_next_page=True, end_cursor="c1", total_spent_time="999999"
            ),
            # Second page's totalSpentTime (7200) matches the local sum exactly — a
            # last-page-wins implementation would compare 7200 to 7200 and stay silent.
            _timelogs_response([], has_next_page=False, total_spent_time="7200"),
        ]
        handler = _make_handler()

        with caplog.at_level("WARNING"):
            handler.report("2026-08-05")

        assert "server-computed total (277h 46m 39s) differs" in caplog.text

    @patch(_PATCH_PATH)
    def test_mismatched_server_total_logs_a_warning_naming_each_value(
        self, mock_run: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning names which number is the server's and which is the local row sum, in
        that order — a swap would send an operator investigating the wrong direction."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(time_spent=7200)], has_next_page=False, total_spent_time="18000"
            ),
        ]
        handler = _make_handler()

        with caplog.at_level("WARNING"):
            handler.report("2026-08-05")

        # 18000s = 5h is the server value, 7200s = 2h is the local row sum;
        # a swapped _format_duration() argument order would put "2h" first.
        assert (
            "server-computed total (5h) differs from the sum of this report's rows (2h)"
            in caplog.text
        )

    @patch(_PATCH_PATH)
    def test_matching_server_total_does_not_warn(
        self, mock_run: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A server total that agrees with the local sum logs no reconciliation warning."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_issue_node(time_spent=3600)], has_next_page=False),
        ]
        handler = _make_handler()

        with caplog.at_level("WARNING"):
            handler.report("2026-08-05")

        assert "differs from the sum of this report's rows" not in caplog.text

    @patch(_PATCH_PATH)
    def test_dropped_node_without_spent_at_logs_a_reconciliation_notice(
        self, mock_run: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A node excluded from bucketing for missing spentAt is named in a warning, with the
        actual counts — not a proxy value that would still read "0 of 0" under a mutation that
        replaced every interpolated count with the post-exclusion bucketed count."""
        malformed = _issue_node(iid=5, time_spent=1800)
        del malformed["spentAt"]
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([malformed], has_next_page=False),
        ]
        handler = _make_handler()

        with caplog.at_level("WARNING"):
            handler.report("2026-08-05")

        assert "excluded from the day breakdown" in caplog.text
        assert "1 of 1 timelog node(s) had no spentAt" in caplog.text
        assert "totals reflect only the 0 node(s) shown" in caplog.text

    @patch(_PATCH_PATH)
    def test_multi_day_grand_total_sums_every_day(self, mock_run: MagicMock, capsys) -> None:
        """A three-day window's grand total is the sum of every day, not just the last one."""
        day1 = _issue_node(iid=1, time_spent=3600, spent_at="2026-08-05T09:00:00Z")
        day2 = _issue_node(iid=2, time_spent=7200, spent_at="2026-08-06T09:00:00Z")
        day3 = _issue_node(iid=3, time_spent=1800, spent_at="2026-08-07T09:00:00Z")
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([day1, day2, day3], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05", "2026-08-07")

        out = capsys.readouterr().out
        # An accumulator collapsed to plain assignment would leave only the
        # last day's 30m as the grand total instead of 1h + 2h + 30m.
        assert "2026-08-05 — 1h" in out
        assert "2026-08-06 — 2h" in out
        assert "2026-08-07 — 30m" in out
        assert "Total: 3h 30m across 3 day(s), 3 entries" in out


class TestSubMinuteDurationReconciliation:
    """A sub-minute row's displayed value must sum exactly to its day header's displayed value —
    flooring either independently is what let a row disagree with the total it belongs to."""

    @patch(_PATCH_PATH)
    def test_two_sub_minute_rows_reconcile_with_the_day_header(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Two 59s entries on different issues both render in seconds and sum exactly to the
        day header — a flooring bug would show both rows as '0m' under a '1m' header, hiding
        where the missing 58 seconds went."""
        first = _issue_node(iid=1, title="First", time_spent=59, spent_at="2026-08-05T09:00:00Z")
        second = _issue_node(iid=2, title="Second", time_spent=59, spent_at="2026-08-05T10:00:00Z")
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([first, second], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "  #1 First — 59s" in out
        assert "  #2 Second — 59s" in out
        # 59 + 59 = 118s = 1m 58s.
        assert "2026-08-05 — 1m 58s" in out

    @patch(_PATCH_PATH)
    def test_negative_sub_minute_row_renders_as_seconds(self, mock_run: MagicMock, capsys) -> None:
        """A negative sub-minute correction renders with a leading minus and exact seconds,
        not '0m'."""
        correction = _issue_node(iid=1, time_spent=-45, spent_at="2026-08-05T09:00:00Z")
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([correction], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "2026-08-05 — -45s" in out


# ---------------------------------------------------------------------------
# local-day boundary
# ---------------------------------------------------------------------------


class TestLocalDayBoundary:
    """An entry whose UTC date differs from its local date must land in the local day."""

    @patch(_PATCH_PATH)
    def test_entry_near_utc_midnight_buckets_under_local_date(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """20:00 UTC on Aug 5 is 02:00 local (UTC+6) on Aug 6 — it must appear under Aug 6."""
        late_utc_entry = _issue_node(iid=1, time_spent=3600, spent_at="2026-08-05T20:00:00Z")
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([late_utc_entry], has_next_page=False),
        ]
        handler = _make_handler()

        # Query an interval spanning both the UTC and local dates so the
        # bucketing decision (not the query window) is what's under test.
        handler.report("2026-08-05", "2026-08-06")

        out = capsys.readouterr().out
        assert "2026-08-06" in out
        # A day header line, not just the window/identity banner mentioning the date.
        assert "2026-08-06 — 1h" in out
        assert "2026-08-05 — " not in out

    @patch(_PATCH_PATH)
    def test_entry_early_utc_stays_on_same_local_date(self, mock_run: MagicMock, capsys) -> None:
        """09:00 UTC on Aug 5 is 15:00 local (UTC+6) on Aug 5 — no boundary crossing."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(spent_at="2026-08-05T09:00:00Z", time_spent=3600)],
                has_next_page=False,
            ),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "2026-08-05 — 1h" in out

    @_REQUIRES_TZDATA
    def test_production_tz_none_path_buckets_by_the_system_local_day(
        self, system_tz, capsys
    ) -> None:
        """With tz=None — the only shape TimelogHandler() runs in production — bucketing goes
        through the real astimezone(None) system-local-time path, not an injected fixed tz.

        Every other test in this class (and every handler test in this
        module) injects _FIXED_TZ; a regression that skipped the
        astimezone(None) conversion entirely — bucketing by the UTC date
        instead of the local one, the exact defect the local-day
        requirement exists to prevent — would still leave all of them
        green.
        """
        system_tz("Asia/Dhaka")  # UTC+6, matching this suite's _FIXED_TZ offset.
        late_utc_entry = _issue_node(iid=1, time_spent=3600, spent_at="2026-08-05T20:00:00Z")
        with patch(_PATCH_PATH) as mock_run:
            mock_run.side_effect = [
                _current_user_response(),
                _timelogs_response([late_utc_entry], has_next_page=False),
            ]
            handler = TimelogHandler(tz=None)

            handler.report("2026-08-05", "2026-08-06")

        out = capsys.readouterr().out
        assert "2026-08-06 — 1h" in out
        assert "2026-08-05 — " not in out


class TestDayOrdering:
    """Multiple days must render oldest first."""

    @patch(_PATCH_PATH)
    def test_days_render_in_ascending_order(self, mock_run: MagicMock, capsys) -> None:
        """Days render sorted ascending, not reverse chronological, regardless of node order."""
        first_day = _issue_node(iid=1, time_spent=3600, spent_at="2026-08-05T09:00:00Z")
        second_day = _issue_node(iid=2, time_spent=3600, spent_at="2026-08-06T09:00:00Z")
        third_day = _issue_node(iid=3, time_spent=3600, spent_at="2026-08-07T09:00:00Z")
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([third_day, first_day, second_day], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05", "2026-08-07")

        out = capsys.readouterr().out
        assert out.index("2026-08-05 —") < out.index("2026-08-06 —") < out.index("2026-08-07 —")


# ---------------------------------------------------------------------------
# date argument handling
# ---------------------------------------------------------------------------


class TestDateArguments:
    """Tests for date parsing, defaults, and range validation."""

    @patch(_PATCH_PATH)
    def test_invalid_date_raises_value_error(self, mock_run: MagicMock) -> None:
        """A malformed date string raises ValueError naming the expected format.

        Patches the transport boundary (unused here) so a reordering
        regression that moved validation after the network round trip would
        spawn a request instead of quietly passing.
        """
        handler = _make_handler()

        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            handler.report("not-a-date")

        mock_run.assert_not_called()

    @patch(_PATCH_PATH)
    def test_to_before_date_raises_value_error(self, mock_run: MagicMock) -> None:
        """--to earlier than the report date is rejected before any API call."""
        handler = _make_handler()

        with pytest.raises(ValueError, match="cannot be earlier"):
            handler.report("2026-08-12", "2026-08-05")

        mock_run.assert_not_called()

    @patch(_PATCH_PATH)
    def test_invalid_to_date_raises_value_error(self, mock_run: MagicMock) -> None:
        """A malformed --to value is rejected the same way as a malformed positional date —
        the second _parse_date_arg() call site is otherwise never exercised."""
        handler = _make_handler()

        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            handler.report("2026-08-05", "not-a-date")

        mock_run.assert_not_called()

    @patch(_PATCH_PATH)
    def test_no_date_defaults_to_a_single_day_window(self, mock_run: MagicMock, capsys) -> None:
        """Omitting both date and --to reports on 'today' as production resolves it, pinned to
        a known instant rather than recomputed by re-calling the same production expression —
        which would trivially agree with itself and could also race local midnight.

        Constructs TimelogHandler directly rather than via _make_handler(): that helper
        overrides handler._now with a fixed lambda that ignores the datetime.now() mock
        below entirely, which would make this test pass regardless of what report() actually
        does with its default-date expression — report() now goes through the same _now()
        seam add() uses (see the Low-severity clock-source finding), so patching
        datetime.now() directly here exercises the real, unoverridden implementation.
        """
        fixed_now = datetime(2026, 8, 5, 12, 0, 0, tzinfo=_FIXED_TZ)
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = TimelogHandler(tz=_FIXED_TZ)

        with patch("projctl.handlers.timelog.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.combine.side_effect = datetime.combine

            handler.report()

        out = capsys.readouterr().out
        first_line = out.split("\n", maxsplit=1)[0]
        # Single-day window renders one date, not a "D to D" range.
        assert " to " not in first_line
        assert "2026-08-05" in first_line


# ---------------------------------------------------------------------------
# GraphQL query construction
# ---------------------------------------------------------------------------


class TestQueryConstruction:
    """Tests asserting the shape of the glab invocations."""

    @patch(_PATCH_PATH)
    def test_timelogs_query_uses_start_time_end_time_not_start_date_end_date(
        self, mock_run: MagicMock
    ) -> None:
        """The query must use startTime/endTime — startDate/endDate is mutually exclusive with it."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        timelogs_cmd = mock_run.call_args_list[1][0][0]
        joined = " ".join(timelogs_cmd)
        assert "startTime=" in joined
        assert "endTime=" in joined
        assert "startDate=" not in joined
        assert "endDate=" not in joined

    @patch(_PATCH_PATH)
    def test_username_is_passed_as_graphql_variable(self, mock_run: MagicMock) -> None:
        """The resolved username is forwarded as the username GraphQL variable."""
        mock_run.side_effect = [
            _current_user_response("alice"),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        timelogs_cmd = mock_run.call_args_list[1][0][0]
        assert "username=alice" in timelogs_cmd

    @patch(_PATCH_PATH)
    def test_query_window_values_match_the_local_day_boundaries(self, mock_run: MagicMock) -> None:
        """startTime/endTime carry the exact local-day bounds, not merely present as keys."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        timelogs_cmd = mock_run.call_args_list[1][0][0]
        assert "startTime=2026-08-05T00:00:00+06:00" in timelogs_cmd
        assert "endTime=2026-08-05T23:59:59.999999+06:00" in timelogs_cmd

    @patch(_PATCH_PATH)
    def test_after_cursor_is_sent_via_raw_field_not_templated_field(
        self, mock_run: MagicMock
    ) -> None:
        """after=<cursor> travels on -f (--raw-field), never -F (--field), which glab treats a
        leading '@' in the value as a filename to read from disk rather than a literal string —
        a server-controlled cursor beginning with '@' on -F could exfiltrate a local file."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_issue_node(iid=1)], has_next_page=True, end_cursor="cursor-1"),
            _timelogs_response([_issue_node(iid=2)], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        second_page_cmd = mock_run.call_args_list[2][0][0]
        assert _adjacent_pair_present(second_page_cmd, ["-f", "after=cursor-1"])

    @patch(_PATCH_PATH)
    def test_username_is_sent_via_raw_field_not_templated_field(self, mock_run: MagicMock) -> None:
        """username=<value> travels on -f, never -F — the same '@'-as-filename hazard as the
        cursor, and username also originates from a server response (currentUser)."""
        mock_run.side_effect = [
            _current_user_response("alice"),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        timelogs_cmd = mock_run.call_args_list[1][0][0]
        assert _adjacent_pair_present(timelogs_cmd, ["-f", "username=alice"])

    @patch(_PATCH_PATH)
    def test_page_size_is_sent_via_templated_field_not_raw_field(self, mock_run: MagicMock) -> None:
        """first=<page size> is a local constant, not server-controlled — safe on -F/--field,
        and pinning the literal value here also catches a silent drop in _PAGE_SIZE."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        timelogs_cmd = mock_run.call_args_list[1][0][0]
        assert _adjacent_pair_present(timelogs_cmd, ["-F", "first=100"])

    @patch(_PATCH_PATH)
    def test_start_time_is_sent_via_raw_field_not_templated_field(
        self, mock_run: MagicMock
    ) -> None:
        """startTime=<value> travels on -f, matching endTime and every other request-shaped
        field below — pinning only 3 of the 7 argv pairs let the -f/-F invariant drift on the
        other 4 with nothing to catch it."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        timelogs_cmd = mock_run.call_args_list[1][0][0]
        assert _adjacent_pair_present(timelogs_cmd, ["-f", "startTime=2026-08-05T00:00:00+06:00"])

    @patch(_PATCH_PATH)
    def test_end_time_is_sent_via_raw_field_not_templated_field(self, mock_run: MagicMock) -> None:
        """endTime=<value> travels on -f, the same invariant as startTime above."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        timelogs_cmd = mock_run.call_args_list[1][0][0]
        assert _adjacent_pair_present(
            timelogs_cmd, ["-f", "endTime=2026-08-05T23:59:59.999999+06:00"]
        )

    @patch(_PATCH_PATH)
    def test_timelogs_query_field_is_sent_via_raw_field_not_templated_field(
        self, mock_run: MagicMock
    ) -> None:
        """query=<the timelogs query> travels on -f, the same invariant as every other
        request-shaped field in this command."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        timelogs_cmd = mock_run.call_args_list[1][0][0]
        assert _adjacent_pair_present(timelogs_cmd, ["-f", f"query={_TIMELOGS_QUERY}"])

    @patch(_PATCH_PATH)
    def test_current_user_query_field_is_sent_via_raw_field_not_templated_field(
        self, mock_run: MagicMock
    ) -> None:
        """The currentUser query's query=<value> also travels on -f, not just the timelogs
        query — the same invariant applies to both GraphQL requests this handler issues."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        current_user_cmd = mock_run.call_args_list[0][0][0]
        assert _adjacent_pair_present(current_user_cmd, ["-f", f"query={_CURRENT_USER_QUERY}"])

    @patch(_PATCH_PATH)
    def test_first_page_request_carries_no_after_field(self, mock_run: MagicMock) -> None:
        """The first page's request has no after= field at all — cursor is None, and the code
        must skip adding it rather than sending after=None or an empty value."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([_issue_node(iid=1)], has_next_page=True, end_cursor="c1"),
            _timelogs_response([_issue_node(iid=2)], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        first_page_cmd = mock_run.call_args_list[1][0][0]
        assert "after=" not in " ".join(first_page_cmd)

    # Every field name a parser read-site pulls out of the timelogs response,
    # named at its read site so a new .get("field") added to the parser
    # without a matching addition to _TIMELOGS_QUERY fails this test instead
    # of silently truncating the report (see the pageInfo/project cases
    # named in the docstring below).
    _FIELDS_THE_PARSER_READS = {
        "totalSpentTime",  # _fetch_timelogs(): connection.get("totalSpentTime")
        "pageInfo",  # _fetch_timelogs(): connection.get("pageInfo")
        "hasNextPage",  # _fetch_timelogs(): page_info.get("hasNextPage")
        "endCursor",  # _fetch_timelogs(): page_info.get("endCursor")
        "nodes",  # _fetch_timelogs(): connection.get("nodes")
        "spentAt",  # _bucket_by_local_day(): node.get("spentAt")
        "timeSpent",  # _day_rows(): node.get("timeSpent")
        "issue",  # _target_label(): node.get("issue")
        "mergeRequest",  # _target_label(): node.get("mergeRequest")
        "project",  # _target_label()/_disambiguated_project_labels(): node.get("project")
        "iid",  # _target_label(): issue.get("iid") / merge_request.get("iid")
        "title",  # _target_label(): issue.get("title") / merge_request.get("title")
        "name",  # _project_identity(): project.get("name")
        "fullPath",  # _project_identity(): project.get("fullPath")
    }

    def test_timelogs_query_requests_every_field_the_parser_reads(self) -> None:
        """Deleting any field the parser reads from _TIMELOGS_QUERY leaves every mocked test
        green (fixtures supply fields regardless of what was requested) — dropping pageInfo is
        the worst case, since the report then truncates silently at exit code 0 rather than
        failing loudly. This asserts the query and the parser agree, independent of fixtures."""
        query_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _TIMELOGS_QUERY))
        missing = self._FIELDS_THE_PARSER_READS - query_tokens
        assert not missing, f"parser reads field(s) not requested by _TIMELOGS_QUERY: {missing}"


# ---------------------------------------------------------------------------
# report header / window line
# ---------------------------------------------------------------------------


class TestReportHeader:
    """Tests for the window/identity header line's single-day vs range form."""

    @patch(_PATCH_PATH)
    def test_range_form_shows_both_dates_separated_by_to(self, mock_run: MagicMock, capsys) -> None:
        """An interval query renders 'D to D' in the header, not a single date."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05", "2026-08-12")

        out = capsys.readouterr().out
        first_line = out.split("\n", maxsplit=1)[0]
        assert "2026-08-05 to 2026-08-12" in first_line

    @patch(_PATCH_PATH)
    def test_queried_window_line_prints_exact_start_and_end_instants(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """The 'Queried window:' line names the exact instants queried, not just a placeholder."""
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert (
            "Queried window: 2026-08-05T00:00:00+06:00 .. 2026-08-05T23:59:59.999999+06:00" in out
        )


# ---------------------------------------------------------------------------
# negative-duration correction entries
# ---------------------------------------------------------------------------


class TestNegativeDurationReport:
    """A GitLab '/spend -Nd' correction produces a timelog entry with negative timeSpent."""

    @patch(_PATCH_PATH)
    def test_day_with_negative_net_time_renders_with_minus_sign(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """A day whose entries net negative (e.g. a correction) prints with a leading minus."""
        correction = _issue_node(iid=1, time_spent=-1800, spent_at="2026-08-05T09:00:00Z")
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response([correction], has_next_page=False),
        ]
        handler = _make_handler()

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "2026-08-05 — -30m" in out
        assert "Total: -30m across 1 day(s), 1 entry" in out


# ---------------------------------------------------------------------------
# write path — helpers
# ---------------------------------------------------------------------------

_GIT_HELPERS_PATCH_PATH = "projctl.handlers.timelog.get_current_repo_path"
_GITLAB_BASE_URL_PATCH_PATH = "projctl.handlers.timelog.get_gitlab_base_url"

# A GitLab-shaped host, distinct from the git remotes used throughout this
# module for project-path resolution, so a test asserting on the resolved
# host can't be satisfied by a lucky path/host mixup.
_FIXED_GITLAB_HOST = "gitlab.example.com"


@pytest.fixture(autouse=True)
def _default_gitlab_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default get_gitlab_base_url() to a GitLab-shaped host for every test in this module.

    add() calls get_gitlab_base_url() whenever a bare/prefixed target reference falls back
    to the git remote for project scope (see TestAddHostBinding below). Without this
    fixture, every such test would instead shell out to the *real* git remote of whatever
    repository the suite happens to run inside, making outcomes depend on ambient
    repository state rather than the fixed inputs the test declares — precisely the
    ambient-state trap this project's own git_helpers tests guard against elsewhere.
    Tests that specifically exercise host resolution (a GitHub remote, an unresolvable
    remote, a URL's own host) override this within their own body via
    _GITLAB_BASE_URL_PATCH_PATH.
    """
    monkeypatch.setattr(_GITLAB_BASE_URL_PATCH_PATH, lambda: f"https://{_FIXED_GITLAB_HOST}")


def _global_id_response(kind: str, gid: str = "gid://gitlab/Issue/999") -> str:
    field = "issue" if kind == "issue" else "mergeRequest"
    return json.dumps({"data": {"project": {field: {"id": gid}}}})


def _timelog_create_response(errors=None, timelog_id="1") -> str:
    errors = errors or []
    payload: dict = {"errors": errors}
    payload["timelog"] = None if errors else {"id": timelog_id}
    return json.dumps({"data": {"timelogCreate": payload}})


def _query_arg(cmd: list) -> str:
    """Return the 'query=...' argument from a glab api graphql command list."""
    return next(a for a in cmd if a.startswith("query="))


def _variable_arg(cmd: list, name: str) -> str:
    """Return the value of a '-f name=value' GraphQL variable field from a glab command list.

    Every user- or server-controlled value the write path sends (issuableId,
    timeSpent, spentAt, summary) travels as a typed variable, never
    interpolated into the query text — this helper asserts against the
    variable field itself, not a substring of the query string, so a
    regression back to string interpolation would show up as a missing
    field rather than an unaffected substring match.

    Raises:
        AssertionError: If no '-f name=...' pair is present in cmd.
    """
    prefix = f"{name}="
    for i, arg in enumerate(cmd):
        if arg == "-f" and i + 1 < len(cmd) and cmd[i + 1].startswith(prefix):
            return cmd[i + 1][len(prefix) :]
    raise AssertionError(f"no -f {name}=... field found in {cmd!r}")


def _sent_variable_names(cmd: list) -> Set[str]:
    """Return the set of GraphQL variable names sent via '-f name=value' in a glab command
    list, excluding the 'query' field itself (which carries the document, not a variable)."""
    names = set()
    for i, arg in enumerate(cmd):
        if arg == "-f" and i + 1 < len(cmd):
            name = cmd[i + 1].split("=", 1)[0]
            if name != "query":
                names.add(name)
    return names


def _normalized(document: str) -> str:
    """Collapse a GraphQL document's whitespace to single spaces for substring checks."""
    return re.sub(r"\s+", " ", document)


def _declared_variables(document: str) -> Dict[str, str]:
    """Return {name: type} for every '$name: Type' declared in the operation's signature.

    Matches only the header segment between 'mutation('/'query(' and its closing ')' —
    neither document under test nests parentheses inside that segment (no default values),
    so a non-greedy match to the first ')' is exact, not merely adequate.
    """
    header = re.match(r"^(?:mutation|query)\(([^)]*)\)", document)
    assert header, f"document has no operation-variable signature: {document!r}"
    declared: Dict[str, str] = {}
    for piece in header.group(1).split(","):
        piece = piece.strip()
        if not piece:
            continue
        name, _sep, type_ = piece.partition(":")
        declared[name.strip().lstrip("$")] = type_.strip()
    return declared


def _used_variable_names(document: str) -> Set[str]:
    """Return every '$name' referenced anywhere in the document body, excluding the
    operation's own variable-declaration header."""
    header = re.match(r"^(?:mutation|query)\([^)]*\)", document)
    assert header, f"document has no operation-variable signature: {document!r}"
    body = document[header.end() :]
    return set(re.findall(r"\$(\w+)", body))


# ---------------------------------------------------------------------------
# write path — GraphQL document structure (the -f side of a request only
# proves what projctl sent; these tests assert directly on the document text
# each -f field is paired with)
# ---------------------------------------------------------------------------


class TestGraphQLDocumentStructure:
    """Structural assertions on the write path's three GraphQL documents.

    Every other write-path test inspects the outgoing '-f name=value'
    fields, never the 'query=...' document those fields are meant to bind
    into — so a document that silently drops a binding, changes a variable's
    type, or is replaced outright survives unless something here reads the
    document text itself. The bijection checks make GraphQL's own "All
    Variables Used" validation rule (a declared-but-unused variable is
    itself a document error) catch a broken document during review as
    aggressively as GitLab's real validator does at request time.
    """

    # -- _TIMELOG_CREATE_MUTATION --------------------------------------

    def test_timelog_create_mutation_operation_keyword_is_mutation(self) -> None:
        """The document opens with 'mutation(', not 'query(' — timelogCreate does not exist
        on the Query root, so a query-shaped document would be rejected outright."""
        assert _TIMELOG_CREATE_MUTATION.startswith("mutation(")

    def test_timelog_create_mutation_declares_the_expected_variable_types(self) -> None:
        """Each declared variable's type is exact — a widened or narrowed type (e.g.
        $timeSpent: Int!) is itself a schema-invalid document, independent of any value
        ever sent for it."""
        assert _declared_variables(_TIMELOG_CREATE_MUTATION) == {
            "issuableId": "IssuableID!",
            "timeSpent": "String!",
            "spentAt": "Time",
            "summary": "String!",
        }

    def test_timelog_create_mutation_declared_and_used_variables_are_a_bijection(self) -> None:
        """Every declared variable is used, and every used variable is declared — GraphQL's
        'All Variables Used' rule makes a declared-but-unused variable a document error on
        its own, so dropping any single 'name: $name' binding while leaving its declaration
        in place (the exact shape of the escaped 'summary' defect) must turn this red."""
        assert _declared_variables(_TIMELOG_CREATE_MUTATION).keys() == _used_variable_names(
            _TIMELOG_CREATE_MUTATION
        )

    def test_timelog_create_mutation_input_bindings_are_present(self) -> None:
        """Each 'name: $name' binding inside 'input: {' is present verbatim — the document
        must actually forward every declared variable into TimelogCreateInput, not merely
        declare it."""
        normalized = _normalized(_TIMELOG_CREATE_MUTATION)
        for binding in (
            "issuableId: $issuableId",
            "timeSpent: $timeSpent",
            "spentAt: $spentAt",
            "summary: $summary",
        ):
            assert binding in normalized, f"missing binding: {binding}"

    def test_timelog_create_mutation_selects_timelog_id(self) -> None:
        """The payload selection includes 'timelog { id }' — the one field that positively
        proves a write occurred, which the success check in _create_timelog() depends on
        being requested at all."""
        assert "timelog { id }" in _normalized(_TIMELOG_CREATE_MUTATION)

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_timelog_create_mutation_variables_sent_equal_variables_used_in_the_document(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """The set of '-f name=value' fields a real call sends equals the set of '$name'
        variables the document actually uses — the reverse direction from the bijection test
        above, tying the document to what production code transmits rather than to itself."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler = _make_handler()

        handler.add("478", "2h", date_str="2026-08-05")

        create_cmd = mock_run.call_args_list[1][0][0]
        assert _sent_variable_names(create_cmd) == _used_variable_names(_TIMELOG_CREATE_MUTATION)

    # -- _ISSUE_GLOBAL_ID_QUERY -----------------------------------------

    def test_issue_global_id_query_operation_keyword_is_query(self) -> None:
        """The document opens with 'query(', not 'mutation(' — a read-only lookup must never
        be sent as a mutation."""
        assert _ISSUE_GLOBAL_ID_QUERY.startswith("query(")

    def test_issue_global_id_query_declares_the_expected_variable_types(self) -> None:
        """fullPath is ID! and iid is String! — GitLab's schema requires exactly these types
        for Project.issue(iid:)."""
        assert _declared_variables(_ISSUE_GLOBAL_ID_QUERY) == {
            "fullPath": "ID!",
            "iid": "String!",
        }

    def test_issue_global_id_query_declared_and_used_variables_are_a_bijection(self) -> None:
        """Same 'All Variables Used' property as the mutation — dropping 'fullPath:
        $fullPath' from project(...) while leaving the declaration in place must turn red."""
        assert _declared_variables(_ISSUE_GLOBAL_ID_QUERY).keys() == _used_variable_names(
            _ISSUE_GLOBAL_ID_QUERY
        )

    def test_issue_global_id_query_bindings_are_present(self) -> None:
        """fullPath: $fullPath scopes the project lookup and iid: $iid selects the issue —
        dropping either binding (leaving the declaration) breaks resolution silently."""
        normalized = _normalized(_ISSUE_GLOBAL_ID_QUERY)
        assert "project(fullPath: $fullPath)" in normalized
        assert "issue(iid: $iid)" in normalized

    def test_issue_global_id_query_selects_the_global_id_field_not_the_local_iid(self) -> None:
        """The issue selection is exactly '{ id }' — the GraphQL global ID timelogCreate's
        issuableId argument requires. Selecting '{ iid }' instead would return the local
        integer iid, which the write path would then forward as issuableId — a value the
        mutation would reject as a malformed global ID."""
        assert "issue(iid: $iid) { id }" in _normalized(_ISSUE_GLOBAL_ID_QUERY)

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_issue_global_id_query_variables_sent_equal_variables_used_in_the_document(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """The '-f' fields the resolve call actually sends equal the '$' variables the
        issue-branch document uses."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler = _make_handler()

        handler.add("478", "2h", date_str="2026-08-05")

        resolve_cmd = mock_run.call_args_list[0][0][0]
        assert _sent_variable_names(resolve_cmd) == _used_variable_names(_ISSUE_GLOBAL_ID_QUERY)

    # -- _MR_GLOBAL_ID_QUERY ----------------------------------------------

    def test_mr_global_id_query_operation_keyword_is_query(self) -> None:
        """The document opens with 'query(', not 'mutation(' — a read-only lookup must never
        be sent as a mutation."""
        assert _MR_GLOBAL_ID_QUERY.startswith("query(")

    def test_mr_global_id_query_declares_the_expected_variable_types(self) -> None:
        """fullPath is ID! and iid is String! — the same variable contract as the issue
        query, since both scope a project(fullPath:) lookup."""
        assert _declared_variables(_MR_GLOBAL_ID_QUERY) == {
            "fullPath": "ID!",
            "iid": "String!",
        }

    def test_mr_global_id_query_declared_and_used_variables_are_a_bijection(self) -> None:
        """Same 'All Variables Used' property as the issue query — dropping 'fullPath:
        $fullPath' from project(...) here must turn red independently of the issue branch."""
        assert _declared_variables(_MR_GLOBAL_ID_QUERY).keys() == _used_variable_names(
            _MR_GLOBAL_ID_QUERY
        )

    def test_mr_global_id_query_bindings_are_present(self) -> None:
        """fullPath: $fullPath scopes the project lookup and iid: $iid selects the merge
        request — dropping either binding breaks resolution silently."""
        normalized = _normalized(_MR_GLOBAL_ID_QUERY)
        assert "project(fullPath: $fullPath)" in normalized
        assert "mergeRequest(iid: $iid)" in normalized

    def test_mr_global_id_query_selects_the_global_id_field_not_the_local_iid(self) -> None:
        """The mergeRequest selection is exactly '{ id }', mirroring the issue branch's own
        selection-set assertion — the mergeRequest branch must independently guard against
        the same '{ iid }' regression."""
        assert "mergeRequest(iid: $iid) { id }" in _normalized(_MR_GLOBAL_ID_QUERY)

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_mr_global_id_query_variables_sent_equal_variables_used_in_the_document(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """The '-f' fields the resolve call actually sends equal the '$' variables the
        MR-branch document uses."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("mr"), _timelog_create_response()]
        handler = _make_handler()

        handler.add("!235", "30m", date_str="2026-08-05")

        resolve_cmd = mock_run.call_args_list[0][0][0]
        assert _sent_variable_names(resolve_cmd) == _used_variable_names(_MR_GLOBAL_ID_QUERY)


# ---------------------------------------------------------------------------
# write path — target reference classification
# ---------------------------------------------------------------------------


class TestResolveTarget:
    """Direct tests for _resolve_target's issue/MR classification, ref parsing, and the
    host it carries alongside project_path for a full URL reference."""

    def test_bare_digit_is_an_issue(self) -> None:
        """A plain digit string with no prefix is treated as an issue, with no host of its
        own — the caller (add()) resolves one from the git remote for this shape."""
        kind, host, project, iid = TimelogHandler._resolve_target(
            "478"
        )  # pylint: disable=protected-access
        assert (kind, host, project, iid) == ("issue", None, None, "478")

    def test_hash_prefixed_is_an_issue(self) -> None:
        """'#478' is treated as an issue with the prefix stripped."""
        kind, host, project, iid = TimelogHandler._resolve_target(
            "#478"
        )  # pylint: disable=protected-access
        assert (kind, host, project, iid) == ("issue", None, None, "478")

    def test_bang_prefixed_is_an_mr(self) -> None:
        """'!235' is treated as an MR with the prefix stripped."""
        kind, host, project, iid = TimelogHandler._resolve_target(
            "!235"
        )  # pylint: disable=protected-access
        assert (kind, host, project, iid) == ("mr", None, None, "235")

    def test_issue_url_carries_its_own_project_path_and_host(self) -> None:
        """A full issue URL resolves its own host alongside the project path and iid,
        kind 'issue' — the host must never be silently discarded, since add() binds it
        into --hostname for both GraphQL calls."""
        kind, host, project, iid = (
            TimelogHandler._resolve_target(  # pylint: disable=protected-access
                "https://gitlab.example.com/group/project/-/issues/478"
            )
        )
        assert (kind, host, project, iid) == (
            "issue",
            "gitlab.example.com",
            "group/project",
            "478",
        )

    def test_work_items_url_is_an_issue(self) -> None:
        """A work_items URL (GitLab's newer issue URL form) is also classified as an issue,
        with its own host resolved too."""
        kind, host, project, iid = (
            TimelogHandler._resolve_target(  # pylint: disable=protected-access
                "https://gitlab.example.com/group/project/-/work_items/478"
            )
        )
        assert (kind, host, project, iid) == (
            "issue",
            "gitlab.example.com",
            "group/project",
            "478",
        )

    def test_mr_url_carries_its_own_project_path_and_host(self) -> None:
        """A full MR URL resolves its own host alongside the project path and iid, kind
        'mr'."""
        kind, host, project, iid = (
            TimelogHandler._resolve_target(  # pylint: disable=protected-access
                "https://gitlab.example.com/group/project/-/merge_requests/235"
            )
        )
        assert (kind, host, project, iid) == (
            "mr",
            "gitlab.example.com",
            "group/project",
            "235",
        )

    def test_bare_reference_carries_no_host(self) -> None:
        """A bare/prefixed reference (no URL) resolves host as None — it has no host of its
        own to report, distinct from a URL whose host happens to be unparsable."""
        _kind, host, _project, _iid = TimelogHandler._resolve_target(
            "478"
        )  # pylint: disable=protected-access
        assert host is None

    def test_unparseable_reference_raises_value_error(self) -> None:
        """A reference matching neither issue nor MR forms raises ValueError."""
        with pytest.raises(ValueError, match="Cannot parse target reference"):
            TimelogHandler._resolve_target("garbage")  # pylint: disable=protected-access

    def test_bang_prefixed_non_numeric_raises_value_error(self) -> None:
        """'!abc' (non-numeric after stripping the prefix) raises ValueError."""
        with pytest.raises(ValueError, match="Cannot parse target reference"):
            TimelogHandler._resolve_target("!abc")  # pylint: disable=protected-access

    def test_hash_prefixed_non_numeric_raises_value_error(self) -> None:
        """'#abc' (non-numeric after stripping the prefix) raises ValueError — the
        parse_issue_url half of the isdigit() guard, mirroring the MR-side coverage above."""
        with pytest.raises(ValueError, match="Cannot parse target reference"):
            TimelogHandler._resolve_target("#abc")  # pylint: disable=protected-access

    def test_non_ascii_digit_reference_raises_value_error(self) -> None:
        """A non-ASCII digit string (e.g. Arabic-indic '٤٧٨') is not a valid iid — str.isdigit()
        alone admits it, but it can never be a real GitLab iid, and forwarding it would send
        a value GitLab's integer-typed argument cannot parse."""
        with pytest.raises(ValueError, match="Cannot parse target reference"):
            TimelogHandler._resolve_target("٤٧٨")  # pylint: disable=protected-access

    def test_unparseable_reference_error_names_accepted_forms(self) -> None:
        """The parse-failure message lists the accepted forms rather than only the rejected
        reference — they are finite and known, so the operator should not have to guess."""
        with pytest.raises(ValueError, match="accepted forms are N, #N"):
            TimelogHandler._resolve_target("garbage")  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# write path — local noon (spentAt basis)
# ---------------------------------------------------------------------------


class TestLocalNoon:
    """Tests for the injected-tz test seam in _local_noon()."""

    def test_no_injected_tz_produces_an_aware_system_local_datetime(self) -> None:
        """With no injected tz (the production path), the naive value gains the system's offset."""
        handler = TimelogHandler(tz=None)

        # pylint: disable=protected-access
        noon = handler._local_noon(date(2026, 8, 5))
        # pylint: enable=protected-access

        assert noon.tzinfo is not None
        assert noon.replace(tzinfo=None) == datetime(2026, 8, 5, 12, 0, 0)

    def test_injected_tz_localizes_the_given_day_at_noon(self) -> None:
        """The spentAt instant reflects the injected tz's offset, at noon on the given day."""
        handler = _make_handler()  # tz=+06:00

        # pylint: disable=protected-access
        noon = handler._local_noon(date(2026, 8, 5))
        # pylint: enable=protected-access

        assert noon.isoformat() == "2026-08-05T12:00:00+06:00"

    @_REQUIRES_TZDATA
    def test_injected_tz_localizes_rather_than_converts_from_system_tz(self, system_tz) -> None:
        """Mirrors TestLocalBound's regression: astimezone() would shift noon by (tz offset -
        system offset) instead of localizing it directly into the injected tz."""
        system_tz("America/New_York")
        handler = TimelogHandler(tz=_FIXED_TZ)

        # pylint: disable=protected-access
        noon = handler._local_noon(date(2026, 8, 5))
        # pylint: enable=protected-access

        assert noon.isoformat() == "2026-08-05T12:00:00+06:00"


class TestNow:
    """Tests for _now() — every other test in this module monkeypatches the bound method
    directly, so nothing else exercises the real datetime-based implementation."""

    def test_no_injected_tz_returns_an_instant_comparable_with_local_noon(self) -> None:
        """With no injected tz (the production path — TimelogHandler() takes no args in
        cmd_timelog()), _now() must return an aware datetime directly comparable with
        _local_noon()'s output, since add() computes min(_local_noon(day), self._now()).

        Regression for an observed crash: datetime.now(None) returns a *naive* datetime
        (tzinfo=None), while _local_noon()'s _localize() attaches the system tzinfo via
        naive.astimezone(None) even when self._tz is None — comparing the two with min()
        raised 'TypeError: can't compare offset-naive and offset-aware datetimes' on the
        very first real invocation of 'projctl timelog add' outside a --dry-run.
        """
        handler = TimelogHandler(tz=None)

        # pylint: disable=protected-access
        now = handler._now()
        noon = handler._local_noon(now.date())
        # pylint: enable=protected-access

        assert now.tzinfo is not None
        min(now, noon)  # must not raise TypeError: offset-naive vs offset-aware

    def test_returns_an_aware_instant_close_to_the_real_wall_clock(self) -> None:
        """The unmonkeypatched _now() reflects the real system clock, not a fixed value —
        bounded against a fresh datetime.now() call taken immediately before and after to
        avoid asserting an exact, inherently racy timestamp."""
        handler = TimelogHandler(tz=_FIXED_TZ)

        before = datetime.now(_FIXED_TZ)
        # pylint: disable=protected-access
        observed = handler._now()
        # pylint: enable=protected-access
        after = datetime.now(_FIXED_TZ)

        assert observed.tzinfo is not None
        assert before <= observed <= after

    @_REQUIRES_TZDATA
    def test_no_injected_tz_localizes_to_the_system_offset_not_utc(self, system_tz) -> None:
        """A regression that returned the raw UTC instant without astimezone(self._tz) would
        still pass test_returns_an_aware_instant_close_to_the_real_wall_clock above — comparing
        aware datetimes normalizes across tzinfo, so ordering alone is offset-independent and
        cannot tell 'converted to local' apart from 'still UTC'. Asia/Dhaka has no DST, so its
        offset is a fixed, easily-distinguished-from-UTC +06:00 regardless of what real time
        the suite happens to run at: at a positive offset, an unconverted _now() would
        silently misfile an early-morning entry onto the wrong local day in add()."""
        system_tz("Asia/Dhaka")  # UTC+6, no DST
        handler = TimelogHandler(tz=None)

        # pylint: disable=protected-access
        now = handler._now()
        # pylint: enable=protected-access

        assert now.utcoffset() == timedelta(hours=6)


# ---------------------------------------------------------------------------
# write path — add()
# ---------------------------------------------------------------------------


class TestAddIssue:
    """Happy-path tests for logging time against an issue."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_bare_digit_ref_resolves_project_from_git_remote(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys
    ) -> None:
        """A bare '478' has no project path of its own, so the git remote supplies it."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue", gid="gid://gitlab/Issue/999"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add("478", "2h", date_str="2026-08-05")

        mock_repo_path.assert_called_once()
        assert mock_run.call_count == 2
        resolve_cmd = mock_run.call_args_list[0][0][0]
        assert _adjacent_pair_present(resolve_cmd, ["-f", "fullPath=group/project"])
        assert _adjacent_pair_present(resolve_cmd, ["-f", "iid=478"])
        assert "issue(iid" in _query_arg(resolve_cmd)
        create_cmd = mock_run.call_args_list[1][0][0]
        assert _variable_arg(create_cmd, "issuableId") == "gid://gitlab/Issue/999"
        out = capsys.readouterr().out
        assert "Logged 2h to #478 in group/project" in out

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_hash_prefixed_ref_behaves_identically_to_bare_digit(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """'#478' and '478' resolve to the same target."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add("#478", "2h", date_str="2026-08-05")

        resolve_cmd = mock_run.call_args_list[0][0][0]
        assert _adjacent_pair_present(resolve_cmd, ["-f", "iid=478"])

    @patch(_PATCH_PATH)
    def test_full_url_ref_skips_the_git_remote_lookup(self, mock_run: MagicMock) -> None:
        """A full issue URL already carries its project path — no git remote call is made."""
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        with patch(_GIT_HELPERS_PATCH_PATH) as mock_repo_path:
            handler.add("https://gitlab.com/alpha/beta/-/issues/478", "2h", date_str="2026-08-05")
            mock_repo_path.assert_not_called()

        resolve_cmd = mock_run.call_args_list[0][0][0]
        assert _adjacent_pair_present(resolve_cmd, ["-f", "fullPath=alpha/beta"])


class TestAddMergeRequest:
    """Tests for logging time against a merge request — the second half of the 'one code
    path once the global ID is resolved' design: the create-timelog call must be identical
    in shape to the issue case, and only the resolution query differs."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_bang_prefixed_ref_resolves_as_a_merge_request(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys
    ) -> None:
        """'!235' resolves via the mergeRequest(iid:) query, not issue(iid:)."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("mr", gid="gid://gitlab/MergeRequest/42"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add("!235", "30m", date_str="2026-08-05")

        resolve_cmd = mock_run.call_args_list[0][0][0]
        assert "mergeRequest(iid" in _query_arg(resolve_cmd)
        assert "issue(iid" not in _query_arg(resolve_cmd)
        assert _adjacent_pair_present(resolve_cmd, ["-f", "iid=235"])
        create_cmd = mock_run.call_args_list[1][0][0]
        assert _variable_arg(create_cmd, "issuableId") == "gid://gitlab/MergeRequest/42"
        out = capsys.readouterr().out
        # '!235', not '#235' or 'mr #235' — MRs must carry their own sigil, matching the
        # read path's own _target_label() convention.
        assert "Logged 30m to !235 in group/project" in out

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_full_mr_url_skips_the_git_remote_lookup(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A full MR URL already carries its project path — no git remote call is made."""
        mock_run.side_effect = [
            _global_id_response("mr"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add(
            "https://gitlab.com/alpha/beta/-/merge_requests/235", "30m", date_str="2026-08-05"
        )

        mock_repo_path.assert_not_called()
        resolve_cmd = mock_run.call_args_list[0][0][0]
        assert _adjacent_pair_present(resolve_cmd, ["-f", "fullPath=alpha/beta"])


class TestAddHostBinding:
    """Target identity is host *and* project path together, never path alone. A
    bare/prefixed reference takes both from the git remote; a full URL carries its own of
    each; a remote resolving to github.com is rejected before any API call; the resolved
    host travels explicitly on both the resolve query and the mutation via --hostname, so
    the two calls cannot silently disagree on which GitLab instance they target."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_bare_reference_passes_the_git_remote_host_to_both_calls(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A bare reference's host (from get_gitlab_base_url(), not the ambient default
        glab would otherwise pick) reaches both the resolve query and the create mutation —
        a disagreement between the two would resolve a global ID on one instance and write
        against another."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler = _make_handler()

        handler.add("478", "2h", date_str="2026-08-05")

        resolve_cmd = mock_run.call_args_list[0][0][0]
        create_cmd = mock_run.call_args_list[1][0][0]
        assert _adjacent_pair_present(resolve_cmd, ["--hostname", _FIXED_GITLAB_HOST])
        assert _adjacent_pair_present(create_cmd, ["--hostname", _FIXED_GITLAB_HOST])

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_full_url_reference_passes_the_urls_own_host_to_both_calls(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A full URL's hostname must not be discarded — _resolve_target() previously
        returned only the path from a URL like 'https://EVIL.example.com/...', so glab would
        fall back to its own ambient host resolution instead of the URL's own instance. It
        must reach --hostname on both calls, and the git-remote fallback must never run."""
        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler = _make_handler()

        handler.add(
            "https://gitlab.other-instance.example/alpha/beta/-/issues/478",
            "2h",
            date_str="2026-08-05",
        )

        mock_repo_path.assert_not_called()
        resolve_cmd = mock_run.call_args_list[0][0][0]
        create_cmd = mock_run.call_args_list[1][0][0]
        assert _adjacent_pair_present(resolve_cmd, ["--hostname", "gitlab.other-instance.example"])
        assert _adjacent_pair_present(create_cmd, ["--hostname", "gitlab.other-instance.example"])

    @patch(_PATCH_PATH)
    def test_full_url_reference_shows_its_host_in_the_dry_run_intent(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """The dry-run preview names the host a real run would target — a preview that hides
        a mismatched host is worse than none, the same property required of the previewed
        spentAt value elsewhere in this module."""
        handler = _make_handler()

        with patch(_GIT_HELPERS_PATCH_PATH) as mock_repo_path:
            handler.add(
                "https://gitlab.other-instance.example/alpha/beta/-/issues/478",
                "2h",
                date_str="2026-08-05",
                dry_run=True,
            )
            mock_repo_path.assert_not_called()

        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "gitlab.other-instance.example" in out

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_github_remote_is_rejected_before_any_api_call(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A bare reference whose git remote resolves to github.com must fail before any glab
        call — get_current_repo_path() accepts any origin regardless of platform, so without
        this check 'timelog add' run from a GitHub-hosted repository would silently resolve
        a path there and attempt to log time against it as if it were a GitLab project."""
        mock_repo_path.return_value = "astavonin/projctl"
        handler = _make_handler()

        with patch(_GITLAB_BASE_URL_PATCH_PATH, return_value="https://github.com"):
            with pytest.raises(PlatformError, match="GitHub, not GitLab"):
                handler.add("1", "2h", date_str="2026-08-05")

        mock_run.assert_not_called()

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_github_remote_is_rejected_even_under_dry_run(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys
    ) -> None:
        """--dry-run makes no glab API calls at all, so glab's own host-auth check (which
        would otherwise catch an unrecognized host at call time) never runs for it — the
        github.com rejection must therefore be a client-side check that fires unconditionally,
        not something --dry-run can silently bypass."""
        mock_repo_path.return_value = "astavonin/projctl"
        handler = _make_handler()

        with patch(_GITLAB_BASE_URL_PATCH_PATH, return_value="https://github.com"):
            with pytest.raises(PlatformError, match="GitHub, not GitLab"):
                handler.add("1", "2h", date_str="2026-08-05", dry_run=True)

        mock_run.assert_not_called()
        assert "[dry-run]" not in capsys.readouterr().out

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_unresolvable_git_remote_host_omits_the_hostname_flag(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """When the git remote's host cannot be determined at all (get_gitlab_base_url()
        returns ''), --hostname is omitted rather than sent as an empty string — glab then
        falls back to its own ambient resolution, matching this handler's pre-existing
        behavior for a remote it cannot parse, rather than introducing a new failure mode
        for an edge case with no known concrete repro."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler = _make_handler()

        with patch(_GITLAB_BASE_URL_PATCH_PATH, return_value=""):
            handler.add("478", "2h", date_str="2026-08-05")

        resolve_cmd = mock_run.call_args_list[0][0][0]
        create_cmd = mock_run.call_args_list[1][0][0]
        assert "--hostname" not in resolve_cmd
        assert "--hostname" not in create_cmd


class TestAddNoGitRemote:
    """The write path fails loudly, not silently, when project scope cannot be resolved."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_bare_ref_with_no_git_remote_raises_before_any_api_call(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A bare ref with no git remote to fall back on raises PlatformError, and no glab
        command is ever run — the failure must be loud, not an empty no-op."""
        mock_repo_path.return_value = None
        handler = _make_handler()

        with pytest.raises(PlatformError, match="git repository with a GitLab remote"):
            handler.add("478", "2h", date_str="2026-08-05")

        mock_run.assert_not_called()


class TestAddInputValidation:
    """add()-level rejection for malformed input, exercised through the public method rather
    than the private helpers those failures ultimately come from — 'fails before any API
    call' is otherwise only guarded at the private-helper level, not at the entry point a
    real caller actually uses."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_malformed_reference_raises_before_any_api_call(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A reference _resolve_target() cannot parse raises ValueError from add() itself,
        before the git-remote fallback or any glab call runs."""
        handler = _make_handler()

        with pytest.raises(ValueError, match="Cannot parse target reference"):
            handler.add("garbage", "2h", date_str="2026-08-05")

        mock_repo_path.assert_not_called()
        mock_run.assert_not_called()

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_malformed_date_raises_before_target_resolution(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A malformed --date value raises the real _parse_date_arg() ValueError — every
        existing CLI-level test for this instead injects a mocked ValueError into a mocked
        handler, so the real message and its ordering ahead of target resolution were
        unverified."""
        handler = _make_handler()

        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            handler.add("478", "2h", date_str="not-a-date")

        mock_repo_path.assert_not_called()
        mock_run.assert_not_called()


class TestAddDryRun:
    """--dry-run must make zero mutating (or any) glab API calls."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_dry_run_issues_zero_api_calls_and_prints_intent(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys
    ) -> None:
        """dry_run=True must not call run_glab_command at all — not even the read-only
        global-ID lookup — while still printing the resolved target and date."""
        mock_repo_path.return_value = "group/project"
        handler = _make_handler()

        handler.add("478", "2h", date_str="2026-08-05", dry_run=True)

        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "2h" in out
        assert "issue #478" in out
        assert "group/project" in out
        assert "2026-08-05" in out
        assert _FIXED_GITLAB_HOST in out

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_dry_run_still_resolves_project_path_since_that_is_a_local_only_call(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """Resolving the project path via the git remote is a local subprocess call, not a
        glab API call — dry-run may still do it so the preview shows the real project."""
        mock_repo_path.return_value = "group/project"
        handler = _make_handler()

        handler.add("478", "2h", date_str="2026-08-05", dry_run=True)

        mock_repo_path.assert_called_once()
        mock_run.assert_not_called()


class TestAddDryRunMatchesRealSpentAt:
    """A preview that disagrees with the real request is worse than none.
    _FIXED_NOW is always after local noon, and every fixed --date fixture elsewhere in this
    module is safely in the past — both shapes make the today-clamp a structural no-op, so
    neither can tell a correct preview apart from a stale, unclamped one. This class freezes
    'now' before local noon on today so the clamp is actually active."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_dry_run_spent_at_equals_the_real_paths_spent_at(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys
    ) -> None:
        mock_repo_path.return_value = "group/project"
        handler = _make_handler()
        frozen_now = datetime(2026, 8, 13, 9, 41, 0, tzinfo=_FIXED_TZ)
        handler._now = lambda: frozen_now  # pylint: disable=protected-access

        handler.add("478", "2h", dry_run=True)
        dry_run_out = capsys.readouterr().out
        dry_run_match = re.search(r"spentAt=(\S+?),", dry_run_out)
        assert dry_run_match, f"no spentAt= found in dry-run output: {dry_run_out!r}"

        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler.add("478", "2h", dry_run=False)
        create_cmd = mock_run.call_args_list[1][0][0]
        real_spent_at = _variable_arg(create_cmd, "spentAt")

        assert dry_run_match.group(1) == real_spent_at
        # The clamp must actually have fired for this assertion to be meaningful —
        # otherwise a dropped clamp on both sides would still agree with itself.
        assert datetime.fromisoformat(real_spent_at) == frozen_now


class TestAddMutationErrors:
    """TimelogCreatePayload.errors is checked explicitly, beyond the top-level GraphQL
    'errors' array — a populated errors array must never look like silent success."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_populated_payload_errors_raise_platform_error(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A 200 response whose payload carries a populated errors array (and a null timelog)
        must raise, not return silently — the worst outcome for a command that records work
        is a no-op that looks like success."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(errors=["Time spent is invalid"]),
        ]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="Time spent is invalid"):
            handler.add("478", "not-a-real-duration", date_str="2026-08-05")

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_gitlab_rejected_duration_message_reaches_the_caller(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """GitLab's own duration-grammar rejection message is surfaced verbatim, not replaced
        with a generic error — this handler never parses the duration grammar itself."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(errors=['"garbage-duration" is not a valid duration']),
        ]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="not a valid duration"):
            handler.add("478", "garbage-duration", date_str="2026-08-05")

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_arbitrary_duration_string_is_passed_through_unparsed(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """The handler never validates or reformats the duration — whatever string is given
        reaches the mutation call verbatim, letting GitLab be the sole authority on its
        grammar (per the standing 'do not write a client-side duration parser' instruction)."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add("478", "1h 30m", date_str="2026-08-05")

        create_cmd = mock_run.call_args_list[1][0][0]
        assert _variable_arg(create_cmd, "timeSpent") == "1h 30m"

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_duration_with_surrounding_whitespace_is_passed_through_verbatim(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A duration padded with whitespace reaches the mutation call byte-for-byte —
        '1h 30m' alone (used above) cannot discriminate a hypothetical normalizing .strip()
        from true pass-through, since stripping a value with no leading/trailing whitespace
        is a no-op either way."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add("478", "  1h 30m  ", date_str="2026-08-05")

        create_cmd = mock_run.call_args_list[1][0][0]
        assert _variable_arg(create_cmd, "timeSpent") == "  1h 30m  "

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_multiple_payload_errors_are_semicolon_joined_not_a_python_list_repr(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """Several payload errors render as a readable '; '-joined sentence, not a Python
        list repr (['a', 'b']) — the latter is what the operator actually sees when GitLab
        returns more than one validation message at once."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(errors=["Time spent is invalid", "Spent at is required"]),
        ]
        handler = _make_handler()

        with pytest.raises(
            PlatformError, match=r"Time spent is invalid; Spent at is required"
        ) as exc_info:
            handler.add("478", "garbage", date_str="2026-08-05")

        assert "[" not in str(exc_info.value)

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_top_level_graphql_errors_during_resolution_propagate(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A top-level 'errors' array on the global-ID lookup raises, and the mutation is
        never attempted."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [json.dumps({"errors": [{"message": "not found"}]})]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="not found"):
            handler.add("478", "2h", date_str="2026-08-05")

        assert mock_run.call_count == 1

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_top_level_graphql_errors_during_creation_propagate(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A top-level 'errors' array on the mutation itself (distinct from a populated
        TimelogCreatePayload.errors) also raises."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            json.dumps({"errors": [{"message": "internal server error"}]}),
        ]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="internal server error"):
            handler.add("478", "2h", date_str="2026-08-05")


class TestAddTransportFailure:
    """A PlatformError raised by run_glab_command itself (e.g. glab missing, auth failure) is
    not swallowed at either write-path call site — the read path covers both of its own call
    sites for this (TestResolveCurrentUser, TestPagination); the write path had neither."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_glab_failure_during_global_id_resolution_propagates(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = PlatformError("glab command not found. Please install glab CLI.")
        handler = _make_handler()

        with pytest.raises(PlatformError, match="glab command not found"):
            handler.add("478", "2h", date_str="2026-08-05")

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_glab_failure_during_create_timelog_propagates(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            PlatformError("glab auth failed"),
        ]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="glab auth failed"):
            handler.add("478", "2h", date_str="2026-08-05")


class TestAddSuccessRequiresTimelogId:
    """A null or missing created timelog is reported as failure, not success — the
    mutation's own 'timelog { id }' selection exists precisely to let this be checked.
    Moving the '✓ Logged' print to run before _create_timelog() must fail every test in this
    class, since none of them would then assert on the payload's timelog field at all."""

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(
                {"data": {"timelogCreate": {"errors": [], "timelog": None}}},
                id="errors_empty_timelog_null",
            ),
            pytest.param(
                {"data": {"timelogCreate": None}},
                id="timelog_create_null",
            ),
            pytest.param({"data": {}}, id="data_has_no_timelog_create_key"),
            pytest.param({"data": None}, id="data_key_is_null"),
        ],
    )
    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_null_or_missing_timelog_payload_raises_instead_of_printing_success(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys, response
    ) -> None:
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("issue"), json.dumps(response)]
        handler = _make_handler()

        with pytest.raises(PlatformError, match="indeterminate"):
            handler.add("478", "2h", date_str="2026-08-05")

        assert "✓ Logged" not in capsys.readouterr().out

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_indeterminate_outcome_message_advises_verifying_before_retrying(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A blind retry risks a double-log (timelogCreate has no idempotency key and the
        read path never dedups) — the message must steer the operator to check first, not
        merely report a generic failure."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            json.dumps({"data": {"timelogCreate": {"errors": [], "timelog": None}}}),
        ]
        handler = _make_handler()

        with pytest.raises(PlatformError, match=r"projctl timelog.*before retrying"):
            handler.add("478", "2h", date_str="2026-08-05")

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_populated_timelog_id_logs_it_and_prints_success(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, caplog, capsys
    ) -> None:
        """A response carrying a real timelog id is logged at debug level and still prints
        the success line — exercises _timelog_create_response()'s timelog_id parameter,
        which no caller previously passed a non-default value for."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(timelog_id="42"),
        ]
        handler = _make_handler()

        with caplog.at_level("DEBUG"):
            handler.add("478", "2h", date_str="2026-08-05")

        assert "42" in caplog.text
        assert "✓ Logged" in capsys.readouterr().out


class TestAddSummaryField:
    """TimelogCreateInput.summary is `String!` with no schema default, which makes it a
    structurally required GraphQL input field — GitLab's document validator rejects the
    mutation before any resolver runs if it is omitted, independent of whether
    issuableId/timeSpent are otherwise valid. Verified against the live schema:

        $ glab api graphql -f query='mutation { timelogCreate(input: {
              issuableId: "gid://gitlab/Issue/0", timeSpent: "1m"
          }) { errors timelog { id } } }'
        {"errors":[{"message":"Argument 'summary' on InputObject 'TimelogCreateInput' is
         required. Expected type String!", ...}]}

    A mock that merely returns success proves nothing about whether GitLab's real
    validator would have accepted the document — every assertion here inspects the
    actual outgoing '-f' fields, not the mocked response.
    """

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_create_timelog_call_always_sends_a_summary_field(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """The outgoing mutation must carry a 'summary' GraphQL variable field — an omitted
        field is exactly the shape the live probe above shows GitLab rejecting outright."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add("478", "2h", date_str="2026-08-05")

        create_cmd = mock_run.call_args_list[1][0][0]
        assert _adjacent_pair_present(create_cmd, ["-f", "summary="])

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_summary_value_is_always_the_empty_string(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """summary carries no text — sending "" satisfies the schema without adding a
        feature the user explicitly declined (no --summary flag). This also guards
        against a future change deriving it from the duration or target ref."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add("478", "2h", date_str="2026-08-05")

        create_cmd = mock_run.call_args_list[1][0][0]
        assert _variable_arg(create_cmd, "summary") == ""


class TestAddMalformedGlobalIdResponse:
    """A malformed global-ID lookup response degrades to a clear, distinguishing
    PlatformError, not a crash — the response already distinguishes a missing project
    (data.project is None) from a missing issuable (data.project.<field> is None), so the
    two get separate messages rather than one generic 'could not resolve'."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_missing_project_key_raises_platform_error_naming_the_project(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A response with no 'project' key at all raises a clear, project-scoped
        PlatformError rather than an uncaught AttributeError from unwrapping the missing
        nesting, and rather than the issuable-scoped message below — the response gives
        enough information to tell the two apart."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [json.dumps({"data": {}})]
        handler = _make_handler()

        with pytest.raises(PlatformError, match=r"Project 'group/project' was not found"):
            handler.add("478", "2h", date_str="2026-08-05")

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_null_issue_within_project_raises_platform_error_naming_the_issue(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A project that exists but has no matching issue (null 'issue' field) raises an
        issue-scoped PlatformError rather than forwarding a None global ID into the
        mutation, and rather than the project-not-found message above."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [json.dumps({"data": {"project": {"issue": None}}})]
        handler = _make_handler()

        with pytest.raises(PlatformError, match=r"Issue #478 was not found"):
            handler.add("478", "2h", date_str="2026-08-05")

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_missing_project_key_raises_platform_error_for_a_merge_request(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """The project-not-found branch is exercised on the MR path too, not only the issue
        path — the two share one implementation, but only the issue path had a test."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [json.dumps({"data": {}})]
        handler = _make_handler()

        with pytest.raises(PlatformError, match=r"Project 'group/project' was not found"):
            handler.add("!235", "30m", date_str="2026-08-05")

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_null_merge_request_within_project_raises_platform_error_naming_the_mr(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A project that exists but has no matching merge request (null 'mergeRequest'
        field) raises an MR-scoped PlatformError, mirroring the issue-branch coverage above
        — the mergeRequest selection and message were previously exercised only via the
        happy path, never on this failure branch."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [json.dumps({"data": {"project": {"mergeRequest": None}}})]
        handler = _make_handler()

        with pytest.raises(PlatformError, match=r"Merge request #235 was not found"):
            handler.add("!235", "30m", date_str="2026-08-05")


class TestAddDateDefault:
    """--date omitted defaults to today, local time — and is always sent explicitly."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_omitted_date_defaults_to_today_local(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """With no date_str, spentAt is built from today's local date, not a fixed default."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(),
        ]
        handler = _make_handler()
        expected_today = _FIXED_NOW.date().isoformat()

        handler.add("478", "2h")

        create_cmd = mock_run.call_args_list[1][0][0]
        assert _variable_arg(create_cmd, "spentAt").startswith(f"{expected_today}T12:00:00")

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_omitted_date_still_sends_an_explicit_spent_at_field(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """spentAt's own schema description says GitLab defaults it to the current time when
        omitted — but that default is UTC-based server time, which would reintroduce exactly
        the local-vs-UTC day-boundary bug _local_bound() exists to prevent on the read side.
        The write path must never rely on that default: a 'spentAt' field must always be
        present on the call, with no date_str given, not just spentAt='' (an empty value
        would silently defer to the server) and not the field being absent altogether."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue"),
            _timelog_create_response(),
        ]
        handler = _make_handler()

        handler.add("478", "2h")

        create_cmd = mock_run.call_args_list[1][0][0]
        spent_at_value = _variable_arg(create_cmd, "spentAt")
        assert spent_at_value != ""


# ---------------------------------------------------------------------------
# future-spentAt clamp (GitLab rejects "Spent at can't be a future date and
# time.") — observed failure, reproduced live: local noon on today is itself
# in the future for any entry logged before local noon, which is this
# operator's normal 09:00-17:00 morning-to-afternoon logging pattern.
# ---------------------------------------------------------------------------


class TestAddFutureSpentAtClamp:
    """spentAt for *today* must never exceed the present; a *future* --date must be
    rejected client-side rather than silently clamped to today (which would log the
    entry to the wrong day) or sent to GitLab to be rejected there (a wasted round
    trip after target resolution). A day strictly before today is never affected."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_omitted_date_before_local_noon_clamps_spent_at_to_now(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A call before local noon on today must not send a future spentAt — GitLab
        rejects any spentAt later than the present, and an unclamped noon would fail
        on every such call given this operator's actual logging hours."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler = _make_handler()
        frozen_now = datetime(2026, 8, 13, 9, 41, 0, tzinfo=_FIXED_TZ)
        handler._now = lambda: frozen_now  # pylint: disable=protected-access

        handler.add("478", "2h")

        create_cmd = mock_run.call_args_list[1][0][0]
        sent = datetime.fromisoformat(_variable_arg(create_cmd, "spentAt"))
        assert sent <= frozen_now
        # The clamp must not have crossed a day boundary while pulling the
        # instant backward from noon to "now" — both are within today.
        assert sent.date() == frozen_now.date()

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_omitted_date_after_local_noon_uses_unclamped_noon(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A call after local noon on today is unaffected by the clamp — spentAt is
        exactly local noon, proving min(noon, now) does not fire when it need not."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler = _make_handler()
        frozen_now = datetime(2026, 8, 13, 15, 0, 0, tzinfo=_FIXED_TZ)
        handler._now = lambda: frozen_now  # pylint: disable=protected-access

        handler.add("478", "2h")

        create_cmd = mock_run.call_args_list[1][0][0]
        assert _variable_arg(create_cmd, "spentAt") == "2026-08-13T12:00:00+06:00"

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_past_date_uses_noon_regardless_of_current_time_of_day(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A --date strictly before today always sends local noon, even when 'now' is
        itself in the early morning — min(noon, now) is a no-op for a day before today
        since a past day's noon is chronologically earlier than any instant on a later
        day, regardless of that instant's time-of-day."""
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [_global_id_response("issue"), _timelog_create_response()]
        handler = _make_handler()
        # 06:00 local on a later day — before noon in time-of-day terms, but the day
        # itself (2026-08-13) is after the requested --date (2026-08-05), so a mutation
        # that swapped min() for max() (picking whichever instant is later) would still
        # incorrectly pick this "now" over noon on 2026-08-05, which this assertion
        # catches.
        handler._now = lambda: datetime(2026, 8, 13, 6, 0, 0, tzinfo=_FIXED_TZ)

        handler.add("478", "2h", date_str="2026-08-05")

        create_cmd = mock_run.call_args_list[1][0][0]
        assert _variable_arg(create_cmd, "spentAt") == "2026-08-05T12:00:00+06:00"

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_future_date_is_rejected_before_any_api_call(
        self, mock_run: MagicMock, mock_repo_path: MagicMock
    ) -> None:
        """A --date later than today raises ValueError naming the requested date, and
        neither the git-remote lookup nor any glab command runs — clamping a future
        date to today would silently log to the wrong day, which is worse than a
        loud, client-side failure."""
        handler = _make_handler()
        handler._now = lambda: datetime(  # pylint: disable=protected-access
            2026, 8, 13, 12, 0, 0, tzinfo=_FIXED_TZ
        )

        with pytest.raises(ValueError, match="2026-08-14"):
            handler.add("478", "2h", date_str="2026-08-14")

        mock_repo_path.assert_not_called()
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# write path -> read path round trip (the property that matters most)
# ---------------------------------------------------------------------------


class TestWriteReadRoundTrip:
    """A write with `--date D` must be found by a subsequent read for local day D — the
    single most important property of this change. spentAt is built from local noon
    specifically so this holds even at UTC offsets where a naive UTC-midnight choice
    would misfile the entry into the adjacent day."""

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_negative_offset_write_is_found_under_the_same_local_day_on_read(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys
    ) -> None:
        """At UTC-11:00, a naive UTC-midnight spentAt for local day D would read back as
        D-1 — local noon must round-trip to exactly D."""
        negative_tz = timezone(timedelta(hours=-11))
        handler = TimelogHandler(tz=negative_tz)
        # "2026-08-05" must read as a past day here, not today, so the
        # today-clamp added for the future-date fix does not interfere with
        # this test's own concern (the noon-vs-midnight offset hazard).
        handler._now = lambda: datetime(2026, 8, 20, 15, 0, 0, tzinfo=negative_tz)
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue", gid="gid://gitlab/Issue/999"),
            _timelog_create_response(),
        ]

        handler.add("478", "2h", date_str="2026-08-05")

        written_spent_at = self._captured_spent_at(mock_run)
        gitlab_read_back = self._as_gitlab_utc_string(written_spent_at)

        mock_run.reset_mock()
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(iid=478, spent_at=gitlab_read_back)], has_next_page=False
            ),
        ]

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "2026-08-05 —" in out
        assert "2026-08-04" not in out
        assert "2026-08-06" not in out

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_positive_extreme_offset_write_is_found_under_the_same_local_day_on_read(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys
    ) -> None:
        """At UTC+14:00 (the practical maximum, e.g. Line Islands), a naive local-end-of-day
        spentAt for local day D would read back as D+1 — local noon must round-trip to D."""
        positive_tz = timezone(timedelta(hours=14))
        handler = TimelogHandler(tz=positive_tz)
        # "2026-08-05" must read as a past day here, not today, so the
        # today-clamp added for the future-date fix does not interfere with
        # this test's own concern (the noon-vs-midnight offset hazard).
        handler._now = lambda: datetime(2026, 8, 20, 15, 0, 0, tzinfo=positive_tz)
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue", gid="gid://gitlab/Issue/999"),
            _timelog_create_response(),
        ]

        handler.add("478", "2h", date_str="2026-08-05")

        written_spent_at = self._captured_spent_at(mock_run)
        gitlab_read_back = self._as_gitlab_utc_string(written_spent_at)

        mock_run.reset_mock()
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(iid=478, spent_at=gitlab_read_back)], has_next_page=False
            ),
        ]

        handler.report("2026-08-05")

        out = capsys.readouterr().out
        assert "2026-08-05 —" in out
        assert "2026-08-04" not in out
        assert "2026-08-06" not in out

    @patch(_GIT_HELPERS_PATCH_PATH)
    @patch(_PATCH_PATH)
    def test_clamped_write_before_local_noon_is_found_under_the_same_local_day_on_read(
        self, mock_run: MagicMock, mock_repo_path: MagicMock, capsys
    ) -> None:
        """A write clamped to 'now' (because it happens before local noon on today, per
        TestAddFutureSpentAtClamp) must still round-trip to the correct local day on read.
        Every other case in this class uses a --date safely in the past, where noon is always
        unclamped — none of them can exercise the clamped branch's own round-trip at all."""
        tz = timezone(timedelta(hours=-11))
        handler = TimelogHandler(tz=tz)
        frozen_now = datetime(2026, 8, 20, 3, 0, 0, tzinfo=tz)  # before local noon on "today"
        handler._now = lambda: frozen_now  # pylint: disable=protected-access
        mock_repo_path.return_value = "group/project"
        mock_run.side_effect = [
            _global_id_response("issue", gid="gid://gitlab/Issue/999"),
            _timelog_create_response(),
        ]

        handler.add("478", "2h")  # --date omitted -> today = 2026-08-20

        written_spent_at = self._captured_spent_at(mock_run)
        # The clamp must actually have fired for this test to exercise anything
        # different from the sibling, unclamped cases above.
        assert datetime.fromisoformat(written_spent_at) == frozen_now
        gitlab_read_back = self._as_gitlab_utc_string(written_spent_at)

        mock_run.reset_mock()
        mock_run.side_effect = [
            _current_user_response(),
            _timelogs_response(
                [_issue_node(iid=478, spent_at=gitlab_read_back)], has_next_page=False
            ),
        ]

        handler.report("2026-08-20")

        out = capsys.readouterr().out
        assert "2026-08-20 —" in out
        assert "2026-08-19" not in out
        assert "2026-08-21" not in out

    @staticmethod
    def _captured_spent_at(mock_run: MagicMock) -> str:
        """Extract the spentAt value sent as a GraphQL variable in the create-timelog call."""
        create_cmd = mock_run.call_args_list[1][0][0]
        return _variable_arg(create_cmd, "spentAt")

    @staticmethod
    def _as_gitlab_utc_string(spent_at: str) -> str:
        """Simulate GitLab normalizing a stored instant to UTC and returning it 'Z'-suffixed —
        the exact shape every other fixture's spentAt uses in this module."""
        as_utc = datetime.fromisoformat(spent_at).astimezone(timezone.utc)
        return as_utc.isoformat().replace("+00:00", "Z")
