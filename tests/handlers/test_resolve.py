"""Tests for projctl.handlers.resolve module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from projctl.exceptions import PlatformError
from projctl.handlers.resolve import ResolveHandler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_PATH = "projctl.utils.glab_runner.run_glab_command"

_UNSET = object()


def _make_handler(dry_run: bool = False) -> ResolveHandler:
    """Create a ResolveHandler with a MagicMock config."""
    return ResolveHandler(config=MagicMock(), dry_run=dry_run)


def _discussion(
    did: str,
    body: str,
    *,
    resolvable: bool = True,
    resolved: bool = False,
    individual: bool = False,
    path: str = "docs/a.md",
    line: int = 10,
    notes: list | None = None,
    position: dict | None = _UNSET,
) -> dict:
    """Build a discussion dict shaped like the GitLab API response."""
    if notes is None:
        notes = [{"resolvable": resolvable, "resolved": resolved}]
    pos = {"new_path": path, "new_line": line} if position is _UNSET else position
    return {
        "id": did,
        "individual_note": individual,
        "notes": [
            {
                "id": i,
                "body": n.get("body", body),
                "resolvable": n.get("resolvable", True),
                "resolved": n.get("resolved", False),
                "author": {"name": "Reviewer"},
                "position": pos,
            }
            for i, n in enumerate(notes)
        ],
    }


def _put_calls(mock_run: MagicMock) -> list:
    """Return the argument lists of every PUT call made."""
    return [c[0][0] for c in mock_run.call_args_list if "PUT" in c[0][0]]


# ---------------------------------------------------------------------------
# fetch_discussions
# ---------------------------------------------------------------------------


class TestFetchDiscussions:
    """Tests for ResolveHandler.fetch_discussions."""

    @patch(_PATCH_PATH)
    def test_single_page_parses(self, mock_run: MagicMock) -> None:
        """A single JSON array is returned as-is."""
        mock_run.return_value = json.dumps([_discussion("a1", "first")])
        result = _make_handler().fetch_discussions("2")
        assert [d["id"] for d in result] == ["a1"]

    @patch(_PATCH_PATH)
    def test_concatenated_pages_are_merged(self, mock_run: MagicMock) -> None:
        """glab --paginate concatenates arrays; all pages must survive."""
        mock_run.return_value = (
            json.dumps([_discussion("a1", "one")])
            + "\n"
            + json.dumps([_discussion("b2", "two"), _discussion("c3", "three")])
        )
        result = _make_handler().fetch_discussions("2")
        assert [d["id"] for d in result] == ["a1", "b2", "c3"]

    @patch(_PATCH_PATH)
    def test_non_json_raises_platform_error(self, mock_run: MagicMock) -> None:
        """Garbage output surfaces as PlatformError, not a bare JSON error."""
        mock_run.return_value = "<html>gateway timeout</html>"
        with pytest.raises(PlatformError):
            _make_handler().fetch_discussions("2")

    @patch(_PATCH_PATH)
    def test_uses_paginate_flag(self, mock_run: MagicMock) -> None:
        """Pagination must be requested or later threads are silently dropped."""
        mock_run.return_value = json.dumps([])
        _make_handler().fetch_discussions("2")
        assert "--paginate" in mock_run.call_args[0][0]


# ---------------------------------------------------------------------------
# State predicates
# ---------------------------------------------------------------------------


class TestStatePredicates:
    """Tests for is_resolvable and is_resolved."""

    def test_individual_note_is_not_resolvable(self) -> None:
        """Standalone comments cannot be resolved; the API rejects them."""
        assert ResolveHandler.is_resolvable(_discussion("a", "x", individual=True)) is False

    def test_non_resolvable_note_is_not_resolvable(self) -> None:
        """System notes carry resolvable=False."""
        assert ResolveHandler.is_resolvable(_discussion("a", "x", resolvable=False)) is False

    def test_resolvable_thread_is_resolvable(self) -> None:
        """A normal review thread is resolvable."""
        assert ResolveHandler.is_resolvable(_discussion("a", "x")) is True

    def test_resolved_thread_reports_resolved(self) -> None:
        """All resolvable notes resolved means the thread is resolved."""
        assert ResolveHandler.is_resolved(_discussion("a", "x", resolved=True)) is True

    def test_open_thread_reports_unresolved(self) -> None:
        """An open thread is not reported as resolved."""
        assert ResolveHandler.is_resolved(_discussion("a", "x")) is False

    def test_thread_without_resolvable_notes_is_not_resolved(self) -> None:
        """No resolvable notes must not vacuously report resolved."""
        assert ResolveHandler.is_resolved(_discussion("a", "x", resolvable=False)) is False


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class TestSelect:
    """Tests for ResolveHandler._select selector semantics."""

    def test_match_selects_single_thread(self) -> None:
        """A unique substring selects exactly its thread."""
        ds = [_discussion("a1", "6 inputs, not 7"), _discussion("b2", "README skips submodules")]
        got = _make_handler()._select(ds, [], ["submodules"])
        assert [d["id"] for d in got] == ["b2"]

    def test_match_with_no_hit_raises(self) -> None:
        """A missed selector is an error, never a silent no-op."""
        ds = [_discussion("a1", "6 inputs, not 7")]
        with pytest.raises(ValueError, match="No resolvable thread"):
            _make_handler()._select(ds, [], ["nonexistent"])

    def test_ambiguous_match_raises_and_names_candidates(self) -> None:
        """An ambiguous selector must not resolve an arbitrary thread."""
        ds = [_discussion("a1", "warp output differs"), _discussion("b2", "warp output identical")]
        with pytest.raises(ValueError, match="matches 2 threads"):
            _make_handler()._select(ds, [], ["warp output"])

    def test_match_ignores_non_resolvable_threads(self) -> None:
        """A substring only present in an unresolvable thread is a miss."""
        ds = [_discussion("a1", "plain comment", individual=True)]
        with pytest.raises(ValueError, match="No resolvable thread"):
            _make_handler()._select(ds, [], ["plain comment"])

    def test_discussion_id_prefix_selects(self) -> None:
        """A unique id prefix resolves to its full discussion."""
        ds = [_discussion("abc123def", "x"), _discussion("zzz999", "y")]
        got = _make_handler()._select(ds, ["abc"], [])
        assert [d["id"] for d in got] == ["abc123def"]

    def test_ambiguous_id_prefix_raises(self) -> None:
        """An ambiguous prefix is rejected rather than guessed."""
        ds = [_discussion("abc1", "x"), _discussion("abc2", "y")]
        with pytest.raises(ValueError, match="ambiguous"):
            _make_handler()._select(ds, ["abc"], [])

    def test_unknown_id_raises(self) -> None:
        """An id that is not on the MR is an error."""
        with pytest.raises(ValueError, match="No discussion with id"):
            _make_handler()._select([_discussion("a1", "x")], ["nope"], [])

    def test_duplicate_selectors_deduplicate(self) -> None:
        """The same thread named twice is acted on once."""
        ds = [_discussion("a1", "6 inputs")]
        got = _make_handler()._select(ds, ["a1"], ["6 inputs"])
        assert [d["id"] for d in got] == ["a1"]


# ---------------------------------------------------------------------------
# set_resolution
# ---------------------------------------------------------------------------


class TestSetResolution:
    """Tests for ResolveHandler.set_resolution."""

    @patch(_PATCH_PATH)
    def test_resolves_selected_thread(self, mock_run: MagicMock) -> None:
        """A matched thread is resolved via PUT with resolved=true."""
        mock_run.side_effect = [json.dumps([_discussion("a1", "6 inputs, not 7")]), json.dumps({})]
        rc = _make_handler().set_resolution("2", [], ["6 inputs"], resolved=True)
        assert rc == 0
        puts = _put_calls(mock_run)
        assert len(puts) == 1
        # Full endpoint: a wrong iid, project scope, or resource type would
        # otherwise resolve a thread on some other MR entirely.
        assert puts[0][3] == "projects/:fullpath/merge_requests/2/discussions/a1?resolved=true"
        # `resolved` must never travel as a request body — GitLab 403s on
        # DiscussionNote threads when it does.
        assert not any(str(a).startswith("resolved=") for a in puts[0])
        assert "-f" not in puts[0] and "-F" not in puts[0]

    @patch(_PATCH_PATH)
    def test_unresolve_sends_false(self, mock_run: MagicMock) -> None:
        """--unresolve flips the target state rather than skipping."""
        mock_run.side_effect = [
            json.dumps([_discussion("a1", "x", resolved=True)]),
            json.dumps({}),
        ]
        _make_handler().set_resolution("2", ["a1"], [], resolved=False)
        put = _put_calls(mock_run)[0]
        assert put[3] == "projects/:fullpath/merge_requests/2/discussions/a1?resolved=false"
        assert not any(str(a).startswith("resolved=") for a in put)

    @patch(_PATCH_PATH)
    def test_already_resolved_thread_is_skipped(self, mock_run: MagicMock) -> None:
        """Re-resolving an already-resolved thread makes no API call."""
        mock_run.side_effect = [json.dumps([_discussion("a1", "x", resolved=True)])]
        _make_handler().set_resolution("2", ["a1"], [], resolved=True)
        assert _put_calls(mock_run) == []

    @patch(_PATCH_PATH)
    def test_non_resolvable_thread_is_skipped(self, mock_run: MagicMock) -> None:
        """An explicitly named unresolvable thread is skipped, not sent."""
        mock_run.side_effect = [json.dumps([_discussion("a1", "x", individual=True)])]
        rc = _make_handler().set_resolution("2", ["a1"], [], resolved=True)
        assert _put_calls(mock_run) == []
        # Exit 0 here would tell the caller a thread it named was handled.
        assert rc == 1

    @patch(_PATCH_PATH)
    def test_dry_run_makes_no_put_call(self, mock_run: MagicMock) -> None:
        """Dry run previews without mutating the MR."""
        mock_run.side_effect = [json.dumps([_discussion("a1", "6 inputs")])]
        _make_handler(dry_run=True).set_resolution("2", [], ["6 inputs"], resolved=True)
        assert _put_calls(mock_run) == []

    @patch(_PATCH_PATH)
    def test_no_selector_raises(self, mock_run: MagicMock) -> None:
        """Calling with no selector must not resolve everything."""
        with pytest.raises(ValueError, match="Nothing selected"):
            _make_handler().set_resolution("2", [], [], resolved=True)
        mock_run.assert_not_called()

    @patch(_PATCH_PATH)
    def test_ambiguous_match_makes_no_put_call(self, mock_run: MagicMock) -> None:
        """An ambiguous selector aborts before any thread is touched."""
        mock_run.side_effect = [
            json.dumps([_discussion("a1", "warp differs"), _discussion("b2", "warp same")])
        ]
        with pytest.raises(ValueError):
            _make_handler().set_resolution("2", [], ["warp"], resolved=True)
        assert _put_calls(mock_run) == []

    @patch(_PATCH_PATH)
    def test_multiple_matches_resolve_each(self, mock_run: MagicMock) -> None:
        """Repeated --match selectors each resolve their own thread."""
        mock_run.side_effect = [
            json.dumps([_discussion("a1", "6 inputs"), _discussion("b2", "submodules")]),
            json.dumps({}),
            json.dumps({}),
        ]
        _make_handler().set_resolution("2", [], ["6 inputs", "submodules"], resolved=True)
        puts = _put_calls(mock_run)
        assert [p[3].split("/")[-1] for p in puts] == ["a1?resolved=true", "b2?resolved=true"]


# ---------------------------------------------------------------------------
# Reference parsing
# ---------------------------------------------------------------------------


class TestEndpointBase:
    """Tests for ResolveHandler._endpoint_base reference handling."""

    @pytest.mark.parametrize("ref,expected", [("2", "2"), ("!2", "2"), ("134", "134")])
    def test_local_refs_use_fullpath(self, ref: str, expected: str) -> None:
        """Plain and !-prefixed refs resolve against the current repository."""
        endpoint, iid = _make_handler()._endpoint_base(ref)
        assert endpoint == f"projects/:fullpath/merge_requests/{expected}/discussions"
        assert iid == expected

    def test_url_ref_targets_that_project(self) -> None:
        """A URL ref must reach the request, or the wrong project gets edited."""
        endpoint, iid = _make_handler()._endpoint_base(
            "https://gitlab.example.com/grp/proj/-/merge_requests/7"
        )
        assert endpoint == "projects/grp%2Fproj/merge_requests/7/discussions"
        assert iid == "7"

    @pytest.mark.parametrize("ref", ["abc", "!x", "https://gitlab.example.com/grp/proj/-/issues/7"])
    def test_invalid_refs_raise(self, ref: str) -> None:
        """Unparseable references are rejected."""
        with pytest.raises(ValueError):
            _make_handler()._endpoint_base(ref)


class TestFetchEndpoint:
    """The GET must target the same MR the PUT will."""

    @patch(_PATCH_PATH)
    def test_fetch_uses_full_endpoint_with_page_size(self, mock_run: MagicMock) -> None:
        """per_page guards --match against a truncated first page."""
        mock_run.return_value = json.dumps([])
        _make_handler().fetch_discussions("https://gitlab.example.com/grp/proj/-/merge_requests/7")
        arg = mock_run.call_args[0][0][-1]
        assert arg == "projects/grp%2Fproj/merge_requests/7/discussions?per_page=100"


class TestMultiNoteThreads:
    """Real threads have replies; the any/all split must survive them."""

    def test_thread_with_unresolved_reply_is_not_resolved(self) -> None:
        """One resolved note plus an open reply is still an open thread."""
        d = _discussion("a1", "x", notes=[{"resolved": True}, {"resolved": False}])
        assert ResolveHandler.is_resolved(d) is False

    def test_thread_with_all_notes_resolved_is_resolved(self) -> None:
        """Every resolvable note resolved means the thread is done."""
        d = _discussion("a1", "x", notes=[{"resolved": True}, {"resolved": True}])
        assert ResolveHandler.is_resolved(d) is True

    def test_system_note_alongside_resolvable_note_is_resolvable(self) -> None:
        """A non-resolvable note must not veto the thread."""
        d = _discussion("a1", "x", notes=[{"resolvable": False}, {"resolvable": True}])
        assert ResolveHandler.is_resolvable(d) is True


class TestDescribe:
    """describe() drives --list, which is how ids are chosen for --discussion."""

    def test_open_thread_renders_open(self) -> None:
        """An unresolved thread is marked open."""
        assert ResolveHandler.describe(_discussion("a1", "hello")).startswith("[open]")

    def test_resolved_thread_renders_done(self) -> None:
        """A resolved thread is marked done; a wrong marker routes a wrong resolve."""
        assert ResolveHandler.describe(_discussion("a1", "x", resolved=True)).startswith("[done]")

    def test_non_resolvable_thread_renders_na(self) -> None:
        """System notes are shown as not applicable."""
        assert ResolveHandler.describe(_discussion("a1", "x", individual=True)).startswith("[n/a ]")

    def test_position_is_rendered_as_file_and_line(self) -> None:
        """The location is what a reader matches against their review."""
        out = ResolveHandler.describe(_discussion("a1", "x", path="docs/b.md", line=42))
        assert "docs/b.md:42" in out

    def test_missing_position_falls_back(self) -> None:
        """A thread with no diff anchor still renders."""
        out = ResolveHandler.describe(_discussion("a1", "x", position=None))
        assert "(no position)" in out

    def test_long_body_is_truncated(self) -> None:
        """Bodies are clipped so one thread stays one line."""
        out = ResolveHandler.describe(_discussion("a1", "y" * 200))
        assert "..." in out and len(out) < 200


class TestListDiscussions:
    """list_discussions reports the counts a user acts on."""

    @patch(_PATCH_PATH)
    def test_counts_resolvable_and_unresolved(self, mock_run: MagicMock, capsys) -> None:
        """Mixed list: one open, one done, one system note."""
        mock_run.return_value = json.dumps(
            [
                _discussion("a1", "open one"),
                _discussion("b2", "done one", resolved=True),
                _discussion("c3", "system", individual=True),
            ]
        )
        rc = _make_handler().list_discussions("2")
        out = capsys.readouterr().out
        assert rc == 0
        assert "3 discussion(s), 2 resolvable, 1 unresolved" in out


class TestPartialFailure:
    """A mid-run API error must not hide what already changed."""

    @patch(_PATCH_PATH)
    def test_failure_reports_prior_success_and_exits_nonzero(
        self, mock_run: MagicMock, capsys
    ) -> None:
        """Thread 1 resolved, thread 2 403s: the run says so and exits 1."""
        mock_run.side_effect = [
            json.dumps([_discussion("a1", "first"), _discussion("b2", "second")]),
            json.dumps({}),
            PlatformError("403 Forbidden"),
        ]
        rc = _make_handler().set_resolution("2", [], ["first", "second"], resolved=True)
        out = capsys.readouterr().out
        assert rc == 1
        assert "a1" in out and "FAILED b2" in out
        assert "1 thread(s) on MR !2" in out and "1 failed" in out
