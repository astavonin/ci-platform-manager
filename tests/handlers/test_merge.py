"""Tests for the merge handler's pre-merge gates and stacked-chain sequencing."""

from unittest.mock import MagicMock, patch

import pytest

from projctl.exceptions import PlatformError
from projctl.handlers.merge import MergeBlocked, MergeHandler

_PATCH_JSON = "projctl.handlers.merge.run_glab_json"
_PATCH_PAGES = "projctl.handlers.merge.run_glab_json_pages"
_PATCH_SLEEP = "projctl.handlers.merge.time.sleep"

# Captured before the autouse fixture below replaces the method, so the tests
# that exercise the real lookup can put it back.
_REAL_MERGE_METHOD = MergeHandler.project_merge_method


def _mr(**over) -> dict:
    """Build a mergeable MR dict, overridable per test."""
    base = {
        "iid": 264,
        "state": "opened",
        "draft": False,
        "work_in_progress": False,
        "merge_status": "can_be_merged",
        "source_branch": "feature/a",
        "target_branch": "master",
        "head_pipeline": {"status": "success"},
    }
    base.update(over)
    return base


def _handler(dry_run: bool = False) -> MergeHandler:
    return MergeHandler(MagicMock(), dry_run=dry_run)


@pytest.fixture(autouse=True)
def _default_merge_method():
    """Most tests are not about fast-forward projects.

    The ff gate costs an extra API call, which would otherwise consume the
    mocked call sequences every other test depends on. Tests that care about
    fast-forward behaviour override this.
    """
    with patch(
        "projctl.handlers.merge.MergeHandler.project_merge_method", return_value="merge"
    ):
        yield


class TestReferenceParsing:
    """A malformed reference must fail before it becomes a request path."""

    @pytest.mark.parametrize("ref", ["!x", "abc", "", "!"])
    def test_non_numeric_reference_raises(self, ref: str) -> None:
        with pytest.raises(ValueError, match="Invalid MR reference"):
            MergeHandler._endpoint(ref)

    @pytest.mark.parametrize("ref", ["264", "!264"])
    def test_numeric_reference_builds_endpoint(self, ref: str) -> None:
        endpoint, iid = MergeHandler._endpoint(ref)

        assert iid == "264"
        assert endpoint.endswith("/merge_requests/264")


class TestGates:
    """Each gate must block, and name why."""

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_closed_mr_is_blocked(self, mock_json: MagicMock, _pages: MagicMock) -> None:
        mock_json.return_value = _mr(state="merged")

        with pytest.raises(MergeBlocked, match="is merged, not opened"):
            _handler().check("264")

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_draft_is_blocked(self, mock_json: MagicMock, _pages: MagicMock) -> None:
        mock_json.return_value = _mr(draft=True)

        with pytest.raises(MergeBlocked, match="draft"):
            _handler().check("264")

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_unmergeable_is_blocked(self, mock_json: MagicMock, _pages: MagicMock) -> None:
        mock_json.return_value = _mr(merge_status="cannot_be_merged")

        with pytest.raises(MergeBlocked, match="cannot_be_merged"):
            _handler().check("264")

    @patch(_PATCH_JSON)
    def test_unresolved_threads_block(self, mock_json: MagicMock) -> None:
        mock_json.return_value = _mr()
        discussions = [{"notes": [{"resolvable": True, "resolved": False}]}]

        with patch(_PATCH_PAGES, return_value=discussions):
            with pytest.raises(MergeBlocked, match="1 unresolved thread"):
                _handler().check("264")

    @patch(_PATCH_JSON)
    def test_allow_unresolved_waives_the_thread_gate(self, mock_json: MagicMock) -> None:
        mock_json.return_value = _mr()
        discussions = [{"notes": [{"resolvable": True, "resolved": False}]}]

        with patch(_PATCH_PAGES, return_value=discussions):
            assert _handler().check("264", allow_unresolved=True)["iid"] == 264

    @patch(_PATCH_JSON)
    def test_individual_notes_are_not_counted_as_unresolved(self, mock_json: MagicMock) -> None:
        """A standalone comment is not resolvable and must not block a merge."""
        mock_json.return_value = _mr()
        discussions = [{"individual_note": True, "notes": [{"resolvable": False}]}]

        with patch(_PATCH_PAGES, return_value=discussions):
            assert _handler().check("264")["iid"] == 264

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_failed_pipeline_is_blocked(self, mock_json: MagicMock, _pages: MagicMock) -> None:
        mock_json.return_value = _mr(head_pipeline={"status": "failed"})

        with pytest.raises(MergeBlocked, match="head pipeline is 'failed'"):
            _handler().check("264")

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_allow_failed_pipeline_waives_it(self, mock_json: MagicMock, _pages: MagicMock) -> None:
        mock_json.return_value = _mr(head_pipeline={"status": "failed"})

        assert _handler().check("264", allow_failed_pipeline=True)["iid"] == 264

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_absent_pipeline_does_not_block(self, mock_json: MagicMock, _pages: MagicMock) -> None:
        """A docs-only branch legitimately has no pipeline; that is not a failure."""
        mock_json.return_value = _mr(head_pipeline=None, pipeline=None)

        assert _handler().check("264")["iid"] == 264

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_checking_status_is_polled_then_passes(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock
    ) -> None:
        """merge_status settles asynchronously; the gate polls rather than failing."""
        mock_json.side_effect = [_mr(merge_status="checking"), _mr()]

        assert _handler().check("264")["iid"] == 264

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_permanently_checking_is_blocked(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock
    ) -> None:
        mock_json.return_value = _mr(merge_status="checking")

        with pytest.raises(MergeBlocked, match="still 'checking'"):
            _handler().check("264")


class TestMergeOne:
    """The merge call itself."""

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_dry_run_issues_no_merge_call(
        self, mock_json: MagicMock, _pages: MagicMock, capsys
    ) -> None:
        mock_json.return_value = _mr()

        _handler(dry_run=True).merge_one("264")

        assert "[dry-run] Would merge MR !264" in capsys.readouterr().out
        # One fetch for the gate, and nothing else.
        assert all("merge" not in str(c).split("',")[-1] for c in mock_json.call_args_list[1:])

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_merge_sends_put_with_branch_removal(
        self, mock_json: MagicMock, _pages: MagicMock, capsys
    ) -> None:
        mock_json.side_effect = [_mr(), _mr(state="merged")]

        _handler().merge_one("264")

        cmd = mock_json.call_args_list[-1][0][0]
        assert "-X" in cmd and "PUT" in cmd
        assert any(c.endswith("/merge") for c in cmd)
        assert "should_remove_source_branch=true" in cmd
        assert "✓ Merged MR !264" in capsys.readouterr().out

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_keep_branch_and_squash_are_forwarded(
        self, mock_json: MagicMock, _pages: MagicMock
    ) -> None:
        mock_json.side_effect = [_mr(), _mr(state="merged")]

        _handler().merge_one("264", remove_branch=False, squash=True)

        cmd = mock_json.call_args_list[-1][0][0]
        assert "should_remove_source_branch=false" in cmd
        assert "squash=true" in cmd

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_blocked_mr_is_never_merged(self, mock_json: MagicMock, _pages: MagicMock) -> None:
        """A failed gate must stop before the PUT, not after."""
        mock_json.return_value = _mr(state="closed")

        with pytest.raises(MergeBlocked):
            _handler().merge_one("264")

        assert all("PUT" not in str(c) for c in mock_json.call_args_list)


class TestMergeChain:
    """Stacked merges must respect order and wait for retargeting."""

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_chain_merges_in_order_and_waits_for_retarget(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock, capsys
    ) -> None:
        mock_json.side_effect = [
            _mr(iid=264, source_branch="feature/a", target_branch="master"),  # gate 264
            _mr(iid=264, state="merged", source_branch="feature/a"),          # merge 264
            _mr(iid=263, target_branch="feature/a"),                          # still stacked
            _mr(iid=263, target_branch="master"),                             # retarget poll
            _mr(iid=263, source_branch="feature/b", target_branch="master"),  # gate 263
            _mr(iid=263, state="merged", source_branch="feature/b"),          # merge 263
        ]

        assert _handler().merge_chain(["264", "263"]) == 0

        out = capsys.readouterr().out
        assert "✓ Merged MR !264" in out
        assert "263 retargeted onto master" in out
        assert "Merged 2 of 2" in out

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_independent_mrs_are_not_reported_as_retargeted(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock, capsys
    ) -> None:
        """An MR that never targeted the merged branch did not move.

        Several MRs off master can be merged in one run. Claiming a retarget
        that GitLab never performed misreports what happened to the stack.
        """
        mock_json.side_effect = [
            _mr(iid=264, source_branch="feature/a", target_branch="master"),  # gate 264
            _mr(iid=264, state="merged", source_branch="feature/a"),          # merge 264
            _mr(iid=263, target_branch="master"),                             # never stacked
            _mr(iid=263, source_branch="feature/b", target_branch="master"),  # gate 263
            _mr(iid=263, state="merged", source_branch="feature/b"),          # merge 263
        ]

        assert _handler().merge_chain(["264", "263"]) == 0

        out = capsys.readouterr().out
        assert "retargeted" not in out
        assert "Merged 2 of 2" in out

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_chain_stops_on_first_blocked_mr(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock, capsys
    ) -> None:
        """Merging on would put the rest of the stack into the wrong base."""
        mock_json.return_value = _mr(iid=264, merge_status="cannot_be_merged")

        assert _handler().merge_chain(["264", "263"]) == 1

        out = capsys.readouterr().out
        assert "Stopped at 264" in out
        assert "0 of 2 merged" in out

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_chain_stops_when_retarget_never_happens(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock, capsys
    ) -> None:
        """A child still pointing at a deleted branch must not be merged."""
        mock_json.side_effect = [
            _mr(iid=264, source_branch="feature/a"),
            _mr(iid=264, state="merged", source_branch="feature/a"),
        ] + [_mr(iid=263, target_branch="feature/a")] * 40

        assert _handler().merge_chain(["264", "263"]) == 1

        out = capsys.readouterr().out
        assert "still targets 'feature/a'" in out
        assert "1 of 2 merged" in out

    @patch("projctl.handlers.merge.MergeHandler.failed_allowed_jobs", return_value=[])
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_dry_run_chain_reports_every_mr_and_merges_none(
        self, mock_json: MagicMock, _pages: MagicMock, _masked: MagicMock, capsys
    ) -> None:
        """A dry run has no wrong base to protect, so it evaluates all of them."""
        mock_json.side_effect = [_mr(iid=264, state="closed"), _mr(iid=263)]

        assert _handler(dry_run=True).merge_chain(["264", "263"]) == 1

        out = capsys.readouterr().out
        assert "✗ MR !264" in out
        assert "✓ MR !263" in out
        assert all("PUT" not in str(c) for c in mock_json.call_args_list)

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_platform_error_stops_the_chain(
        self, mock_json: MagicMock, _pages: MagicMock, capsys
    ) -> None:
        mock_json.side_effect = PlatformError("glab exploded")

        assert _handler().merge_chain(["264", "263"]) == 1
        assert "Stopped at 264" in capsys.readouterr().out


class TestReport:
    """--dry-run reports every gate for every MR without stopping."""

    @patch("projctl.handlers.merge.MergeHandler.failed_allowed_jobs", return_value=[])
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_mergeable_mr_reports_all_gates_ok(
        self, mock_json: MagicMock, _pages: MagicMock, _masked: MagicMock, capsys
    ) -> None:
        mock_json.return_value = _mr()

        assert _handler().report(["264"]) == 0

        out = capsys.readouterr().out
        assert "✓ MR !264" in out
        assert "BLOCK" not in out
        assert "1 of 1 MR(s) can merge" in out

    @patch("projctl.handlers.merge.MergeHandler.failed_allowed_jobs", return_value=[])
    @patch(_PATCH_JSON)
    def test_every_failing_gate_is_listed_not_just_the_first(
        self, mock_json: MagicMock, _masked: MagicMock, capsys
    ) -> None:
        """The point of a dry run is seeing all the reasons at once."""
        mock_json.return_value = _mr(draft=True, head_pipeline={"status": "failed"})
        discussions = [{"notes": [{"resolvable": True, "resolved": False}]}]

        with patch(_PATCH_PAGES, return_value=discussions):
            assert _handler().report(["264"]) == 1

        out = capsys.readouterr().out
        assert out.count("BLOCK") == 3  # draft, threads, pipeline
        assert "0 of 1 MR(s) can merge" in out

    @patch("projctl.handlers.merge.MergeHandler.failed_allowed_jobs", return_value=[])
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_report_continues_past_a_blocked_mr(
        self, mock_json: MagicMock, _pages: MagicMock, _masked: MagicMock, capsys
    ) -> None:
        mock_json.side_effect = [_mr(iid=264, state="closed"), _mr(iid=263)]

        assert _handler().report(["264", "263"]) == 1

        out = capsys.readouterr().out
        assert "✗ MR !264" in out
        assert "✓ MR !263" in out
        assert "1 of 2 MR(s) can merge" in out

    @patch("projctl.handlers.merge.MergeHandler.failed_allowed_jobs", return_value=["ota-e2e"])
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_allow_failure_jobs_are_surfaced_without_blocking(
        self, mock_json: MagicMock, _pages: MagicMock, _masked: MagicMock, capsys
    ) -> None:
        """A green rollup hiding a red allow_failure job must still be visible."""
        mock_json.return_value = _mr()

        assert _handler().report(["264"]) == 0

        out = capsys.readouterr().out
        assert "failed but allow_failure: ota-e2e" in out
        assert "✓ MR !264" in out

    @patch("projctl.handlers.merge.MergeHandler.failed_allowed_jobs", return_value=[])
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_platform_error_on_one_mr_does_not_abort_the_rest(
        self, mock_json: MagicMock, _pages: MagicMock, _masked: MagicMock, capsys
    ) -> None:
        mock_json.side_effect = [PlatformError("boom"), _mr(iid=263)]

        assert _handler().report(["264", "263"]) == 1

        out = capsys.readouterr().out
        assert "✗ 264: boom" in out
        assert "✓ MR !263" in out


class TestFailedAllowedJobs:
    """Detection of failed-but-allowed jobs is diagnostic and must never raise."""

    def test_absent_pipeline_yields_no_jobs(self) -> None:
        assert _handler().failed_allowed_jobs({"head_pipeline": None}) == []

    @patch(_PATCH_PAGES)
    def test_only_failed_and_allowed_jobs_are_named(self, mock_pages: MagicMock) -> None:
        mock_pages.return_value = [
            {"name": "ota-e2e", "status": "failed", "allow_failure": True},
            {"name": "build", "status": "success", "allow_failure": False},
            {"name": "lint", "status": "failed", "allow_failure": False},
        ]

        assert _handler().failed_allowed_jobs({"head_pipeline": {"id": 7}}) == ["ota-e2e"]

    @patch(_PATCH_PAGES, side_effect=PlatformError("no access"))
    def test_api_failure_degrades_to_empty(self, _pages: MagicMock) -> None:
        """A diagnostic must not break a gate that already passed."""
        assert _handler().failed_allowed_jobs({"head_pipeline": {"id": 7}}) == []


class TestDetailedMergeStatus:
    """detailed_merge_status is authoritative; merge_status alone lies.

    Observed on !263: after a retarget onto master the legacy field still read
    can_be_merged while GitLab refused the merge with HTTP 405, because the old
    pipeline had run against the old target. Gating on the coarse field let the
    dry run report 'ok' for an MR that could not merge.
    """

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_ci_must_pass_blocks_despite_can_be_merged(
        self, mock_json: MagicMock, _pages: MagicMock
    ) -> None:
        mock_json.return_value = _mr(
            merge_status="can_be_merged", detailed_merge_status="ci_must_pass"
        )

        with pytest.raises(MergeBlocked, match="ci_must_pass"):
            _handler().check("264")

    @patch("projctl.handlers.merge.MergeHandler.failed_allowed_jobs", return_value=[])
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_dry_run_names_the_real_blocker(
        self, mock_json: MagicMock, _pages: MagicMock, _masked: MagicMock, capsys
    ) -> None:
        """The dry run must say why, not report a stale 'ok'."""
        mock_json.return_value = _mr(
            merge_status="can_be_merged", detailed_merge_status="ci_must_pass"
        )

        assert _handler(dry_run=True).report(["263"]) == 1
        assert "ci_must_pass" in capsys.readouterr().out

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_mergeable_detailed_status_passes(
        self, mock_json: MagicMock, _pages: MagicMock
    ) -> None:
        mock_json.return_value = _mr(detailed_merge_status="mergeable")

        assert _handler().check("264")["iid"] == 264

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_ci_still_running_is_polled_not_refused(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock
    ) -> None:
        """A retarget puts an MR through transient states before it settles."""
        mock_json.side_effect = [
            _mr(detailed_merge_status="ci_still_running"),
            _mr(detailed_merge_status="mergeable"),
        ]

        assert _handler().check("264")["iid"] == 264

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_legacy_field_used_when_detailed_absent(
        self, mock_json: MagicMock, _pages: MagicMock
    ) -> None:
        """Older GitLab does not return detailed_merge_status."""
        mock_json.return_value = _mr(detailed_merge_status=None)

        assert _handler().check("264")["iid"] == 264


class TestTransientMergeRejection:
    """GitLab reports 'mergeable' before it will accept the merge after a retarget.

    Observed on !263 and !265: the gate passed, the PUT came back 405/422, and
    the same MR merged cleanly seconds later with nothing changed.
    """

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_422_is_retried_and_succeeds(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock, capsys
    ) -> None:
        mock_json.side_effect = [
            _mr(),                                  # gate
            PlatformError("Branch cannot be merged (HTTP 422)"),
            _mr(),                                  # re-gate
            _mr(state="merged"),                    # retry succeeds
        ]

        _handler().merge_one("265")

        # iid comes from the reference, not the payload.
        assert "✓ Merged MR !265" in capsys.readouterr().out

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_405_is_retried(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock
    ) -> None:
        mock_json.side_effect = [
            _mr(),
            PlatformError("405 Method Not Allowed"),
            _mr(),
            _mr(state="merged"),
        ]

        assert _handler().merge_one("263")["state"] == "merged"

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_a_real_block_surfaces_on_re_gate_rather_than_looping(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock
    ) -> None:
        """If the refusal was real, the re-gate names the actual reason."""
        mock_json.side_effect = [
            _mr(),
            PlatformError("Branch cannot be merged (HTTP 422)"),
            _mr(detailed_merge_status="conflict"),  # re-gate finds the real cause
        ]

        with pytest.raises(MergeBlocked, match="conflict"):
            _handler().merge_one("265")

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_non_transient_error_is_not_retried(
        self, mock_json: MagicMock, _pages: MagicMock
    ) -> None:
        """A 403 is not a race; retrying it just delays the report."""
        mock_json.side_effect = [_mr(), PlatformError("403 Forbidden")]

        with pytest.raises(PlatformError, match="403"):
            _handler().merge_one("264")

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_persistent_transient_rejection_eventually_fails(
        self, mock_json: MagicMock, _pages: MagicMock, _sleep: MagicMock
    ) -> None:
        mock_json.side_effect = [_mr()] + [
            PlatformError("HTTP 422"), _mr()
        ] * 10

        with pytest.raises(PlatformError, match="refused the merge"):
            _handler().merge_one("265")


class TestFastForwardGate:
    """A squashing ff project breaks stacked chains in a way the API hides.

    Observed on !265: after !263 merged as a squashed commit, !265 was no longer
    a descendant of master. detailed_merge_status still read 'mergeable' and the
    PUT returned 422 with no usable reason. The UI's Merge button hides this by
    rebasing first; the REST endpoint does not.
    """

    @patch("projctl.handlers.merge.MergeHandler.diverged_commits", return_value=1)
    @patch("projctl.handlers.merge.MergeHandler.project_merge_method", return_value="ff")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_diverged_branch_blocks_on_ff_project(
        self, mock_json: MagicMock, _pages: MagicMock, _mm: MagicMock, _dv: MagicMock
    ) -> None:
        mock_json.return_value = _mr(detailed_merge_status="mergeable")

        with pytest.raises(MergeBlocked, match="behind master.*fast-forward"):
            _handler().check("265")

    @patch("projctl.handlers.merge.MergeHandler.failed_allowed_jobs", return_value=[])
    @patch("projctl.handlers.merge.MergeHandler.diverged_commits", return_value=1)
    @patch("projctl.handlers.merge.MergeHandler.project_merge_method", return_value="ff")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_dry_run_names_the_rebase_requirement(
        self, mock_json: MagicMock, _p: MagicMock, _mm: MagicMock, _dv: MagicMock,
        _mask: MagicMock, capsys
    ) -> None:
        """The dry run must explain the 422 rather than reporting a stale ok."""
        mock_json.return_value = _mr(detailed_merge_status="mergeable")

        assert _handler(dry_run=True).report(["265"]) == 1

        out = capsys.readouterr().out
        assert "ff-ready" in out
        assert "rebase" in out

    @patch("projctl.handlers.merge.MergeHandler.diverged_commits", return_value=0)
    @patch("projctl.handlers.merge.MergeHandler.project_merge_method", return_value="ff")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_descendant_branch_passes_on_ff_project(
        self, mock_json: MagicMock, _pages: MagicMock, _mm: MagicMock, _dv: MagicMock
    ) -> None:
        mock_json.return_value = _mr()

        assert _handler().check("265")["iid"] == 264

    @patch("projctl.handlers.merge.MergeHandler.diverged_commits")
    @patch("projctl.handlers.merge.MergeHandler.project_merge_method", return_value="merge")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_non_ff_project_skips_the_check_entirely(
        self, mock_json: MagicMock, _pages: MagicMock, _mm: MagicMock, mock_dv: MagicMock
    ) -> None:
        """A merge-commit project tolerates divergence; do not spend the call."""
        mock_json.return_value = _mr()

        _handler().check("264")

        mock_dv.assert_not_called()


class TestRebaseIsAppliedWhereNeeded:
    """--rebase must reach a single MR, not only the gaps between chain steps."""

    @patch("projctl.handlers.merge.MergeHandler.await_pipeline", return_value="success")
    @patch("projctl.handlers.merge.MergeHandler.rebase")
    @patch("projctl.handlers.merge.MergeHandler.diverged_commits", return_value=1)
    @patch("projctl.handlers.merge.MergeHandler.project_merge_method", return_value="ff")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_single_mr_is_rebased_before_gating(
        self, mock_json: MagicMock, _p: MagicMock, _mm: MagicMock,
        mock_div: MagicMock, mock_rebase: MagicMock, _await: MagicMock
    ) -> None:
        """Without this, --rebase on one MR was silently a no-op and the gate blocked."""
        mock_json.side_effect = [_mr(), _mr(state="merged")]
        handler = _handler()
        handler.rebase_between = True
        # Behind before the rebase, a descendant after it.
        mock_div.side_effect = [1, 0]

        handler.merge_one("265")

        mock_rebase.assert_called_once_with("265")

    @patch("projctl.handlers.merge.MergeHandler.rebase")
    @patch("projctl.handlers.merge.MergeHandler.diverged_commits", return_value=0)
    @patch("projctl.handlers.merge.MergeHandler.project_merge_method", return_value="ff")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_descendant_branch_is_not_rebased(
        self, mock_json: MagicMock, _p: MagicMock, _mm: MagicMock,
        _div: MagicMock, mock_rebase: MagicMock
    ) -> None:
        """A rebase costs a pipeline; do not spend one that changes nothing."""
        mock_json.side_effect = [_mr(), _mr(state="merged")]
        handler = _handler()
        handler.rebase_between = True

        handler.merge_one("265")

        mock_rebase.assert_not_called()

    @patch("projctl.handlers.merge.MergeHandler.rebase")
    @patch("projctl.handlers.merge.MergeHandler.diverged_commits", return_value=5)
    @patch("projctl.handlers.merge.MergeHandler.project_merge_method", return_value="merge")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_non_ff_project_is_never_rebased(
        self, mock_json: MagicMock, _p: MagicMock, _mm: MagicMock,
        _div: MagicMock, mock_rebase: MagicMock
    ) -> None:
        mock_json.side_effect = [_mr(), _mr(state="merged")]
        handler = _handler()
        handler.rebase_between = True

        handler.merge_one("264")

        mock_rebase.assert_not_called()


class TestAwaitPipelineMatchesTheHeadSha:
    """After a rebase the old pipeline stays attached briefly.

    Observed on !265: await_pipeline returned "success" for the pre-rebase
    pipeline, and the merge gate then found 'ci_still_running' for the real one.
    """

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON)
    def test_stale_pipeline_is_not_reported_as_the_result(
        self, mock_json: MagicMock, _sleep: MagicMock
    ) -> None:
        mock_json.side_effect = [
            {"sha": "new", "head_pipeline": {"sha": "old", "status": "success"}},
            {"sha": "new", "head_pipeline": {"sha": "new", "status": "running"}},
            {"sha": "new", "head_pipeline": {"sha": "new", "status": "success"}},
        ]

        assert _handler().await_pipeline("265") == "success"
        assert mock_json.call_count == 3

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON)
    def test_pipeline_without_a_sha_is_still_accepted(
        self, mock_json: MagicMock, _sleep: MagicMock
    ) -> None:
        """Not every payload carries a pipeline sha; absence must not hang the wait."""
        mock_json.return_value = {"sha": "new", "head_pipeline": {"status": "success"}}

        assert _handler().await_pipeline("265") == "success"


class TestWaitForPipeline:
    """--wait turns a running pipeline into a delay rather than a refusal."""

    @patch("projctl.handlers.merge.MergeHandler.await_pipeline", return_value="success")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_wait_polls_before_gating(
        self, mock_json: MagicMock, _pages: MagicMock, mock_await: MagicMock
    ) -> None:
        mock_json.side_effect = [_mr(), _mr(state="merged")]
        handler = _handler()
        handler.wait_pipeline = True

        handler.merge_one("265")

        mock_await.assert_called_once_with("265")

    @patch("projctl.handlers.merge.MergeHandler.await_pipeline")
    @patch("projctl.handlers.merge.MergeHandler.rebase_if_behind", return_value=True)
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_rebase_path_does_not_wait_twice(
        self, mock_json: MagicMock, _pages: MagicMock, _rb: MagicMock, mock_await: MagicMock
    ) -> None:
        """rebase_if_behind already waited for the post-rebase pipeline."""
        mock_json.side_effect = [_mr(), _mr(state="merged")]
        handler = _handler()
        handler.wait_pipeline = True
        handler.rebase_between = True

        handler.merge_one("265")

        mock_await.assert_not_called()

    @patch("projctl.handlers.merge.MergeHandler.await_pipeline")
    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_without_wait_a_running_pipeline_is_not_polled(
        self, mock_json: MagicMock, _pages: MagicMock, mock_await: MagicMock
    ) -> None:
        mock_json.side_effect = [_mr(), _mr(state="merged")]

        _handler().merge_one("265")

        mock_await.assert_not_called()


class TestPollingSurvivesTransientNetworkErrors:
    """A 20-minute wait over a VPN must not die on one timeout.

    Observed three times on this stack: a merge that was otherwise on track
    aborted because a single poll could not reach GitLab.
    """

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON)
    def test_pipeline_wait_survives_a_failed_poll(
        self, mock_json: MagicMock, _sleep: MagicMock
    ) -> None:
        mock_json.side_effect = [
            PlatformError("dial tcp: i/o timeout"),
            {"sha": "s", "head_pipeline": {"sha": "s", "status": "success"}},
        ]

        assert _handler().await_pipeline("265") == "success"

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON)
    def test_retarget_wait_survives_a_failed_poll(
        self, mock_json: MagicMock, _sleep: MagicMock
    ) -> None:
        mock_json.side_effect = [
            PlatformError("lookup gitlab: i/o timeout"),
            _mr(target_branch="master"),
        ]

        assert _handler()._await_retarget("265", "feature/old") == "master"

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON, side_effect=PlatformError("down"))
    def test_persistent_failure_still_ends_within_budget(
        self, _json: MagicMock, _sleep: MagicMock
    ) -> None:
        """Tolerance is not patience without limit."""
        assert _handler().await_pipeline("265") == "none"


class TestProjectMergeMethod:
    """The ff gate depends on this answer, and it must be paid for once."""

    def test_method_is_read_from_the_project(self) -> None:
        with patch.object(MergeHandler, "project_merge_method", _REAL_MERGE_METHOD):
            with patch(_PATCH_JSON, return_value={"merge_method": "ff"}) as mock_json:
                assert _handler().project_merge_method() == "ff"
                assert mock_json.call_count == 1

    def test_answer_is_cached_across_calls(self) -> None:
        """Every gated MR asks; re-reading would cost an API call per MR."""
        with patch.object(MergeHandler, "project_merge_method", _REAL_MERGE_METHOD):
            with patch(_PATCH_JSON, return_value={"merge_method": "ff"}) as mock_json:
                handler = _handler()
                handler.project_merge_method()
                handler.project_merge_method()

                assert mock_json.call_count == 1

    def test_unreadable_project_is_cached_as_unknown(self) -> None:
        """An unknown method means "do not apply the ff gate", not "retry forever"."""
        with patch.object(MergeHandler, "project_merge_method", _REAL_MERGE_METHOD):
            with patch(_PATCH_JSON, side_effect=PlatformError("403")) as mock_json:
                handler = _handler()

                assert handler.project_merge_method() is None
                assert handler.project_merge_method() is None
                assert mock_json.call_count == 1


class TestDivergedCommits:
    """The ff gate reads this count; an unreadable one must not block a merge."""

    @patch(_PATCH_JSON, return_value={"diverged_commits_count": 3})
    def test_count_is_reported(self, _json: MagicMock) -> None:
        assert _handler().diverged_commits("264") == 3

    @patch(_PATCH_JSON, return_value={})
    def test_absent_count_reads_as_zero(self, _json: MagicMock) -> None:
        """GitLab omits the field unless asked; absent is not "behind"."""
        assert _handler().diverged_commits("264") == 0

    @patch(_PATCH_JSON, side_effect=PlatformError("boom"))
    def test_failed_lookup_reads_as_zero(self, _json: MagicMock) -> None:
        assert _handler().diverged_commits("264") == 0


class TestRebase:
    """A server-side rebase is asynchronous, so its outcome must be polled."""

    @patch(_PATCH_JSON)
    def test_dry_run_issues_no_call(self, mock_json: MagicMock, capsys) -> None:
        _handler(dry_run=True).rebase("265")

        assert "[dry-run] Would rebase MR !265" in capsys.readouterr().out
        mock_json.assert_not_called()

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON)
    def test_rebase_waits_for_completion(
        self, mock_json: MagicMock, _sleep: MagicMock, capsys
    ) -> None:
        mock_json.side_effect = [
            {},                                                    # PUT /rebase
            {"rebase_in_progress": True},                          # still running
            {"rebase_in_progress": False, "target_branch": "master"},
        ]

        _handler().rebase("265")

        assert "rebased !265 onto master" in capsys.readouterr().out

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON)
    def test_merge_error_after_rebase_raises(
        self, mock_json: MagicMock, _sleep: MagicMock
    ) -> None:
        """A conflict is recorded in merge_error, not in the rebase response."""
        mock_json.side_effect = [
            {},
            {"rebase_in_progress": False, "merge_error": "conflict in src/a.py"},
        ]

        with pytest.raises(PlatformError, match="conflict in src/a.py"):
            _handler().rebase("265")

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JSON)
    def test_rebase_that_never_finishes_raises(
        self, mock_json: MagicMock, _sleep: MagicMock
    ) -> None:
        mock_json.side_effect = [{}] + [{"rebase_in_progress": True}] * 40

        with pytest.raises(PlatformError, match="did not finish in time"):
            _handler().rebase("265")


class TestLockedState:
    """GitLab sets state 'locked' while a merge is in flight.

    Observed on !266: the merge completed while the run reported failure,
    because the state gate read 'locked' as a terminal refusal.
    """

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_locked_reports_a_merge_in_flight_not_a_wrong_state(
        self, mock_json: MagicMock, _pages: MagicMock
    ) -> None:
        mock_json.return_value = _mr(state="locked")

        with pytest.raises(MergeBlocked, match="merge is already in flight"):
            _handler().check("266")

    @patch(_PATCH_PAGES, return_value=[])
    @patch(_PATCH_JSON)
    def test_genuinely_closed_still_reads_as_not_opened(
        self, mock_json: MagicMock, _pages: MagicMock
    ) -> None:
        mock_json.return_value = _mr(state="closed")

        with pytest.raises(MergeBlocked, match="is closed, not opened"):
            _handler().check("266")
