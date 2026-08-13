"""Read-only report of the current user's own GitLab timelogs."""

import json
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Dict, List, Optional, Set, Tuple

from ..exceptions import PlatformError
from ..utils.glab_runner import run_glab_command

logger = logging.getLogger(__name__)

# GitLab clamps `first` to 100 regardless of what is requested above it.
_PAGE_SIZE = 100

# Defensive bound on pagination: seen_cursors only catches a server that
# repeats a cursor, not one that keeps minting fresh ones forever. 200 pages
# is 20,000 timelogs — far beyond any real interval query — so hitting this
# means the server is malfunctioning, not that the window is legitimately
# large.
_MAX_PAGES = 200

_CURRENT_USER_QUERY = "query { currentUser { username } }"

_TIMELOGS_QUERY = (
    "query("
    "$username: String!, $startTime: Time!, $endTime: Time!, "
    "$first: Int!, $after: String"
    ") { timelogs("
    "username: $username, startTime: $startTime, endTime: $endTime, "
    "first: $first, after: $after"
    ") { "
    "totalSpentTime "
    "pageInfo { hasNextPage endCursor } "
    "nodes { "
    "spentAt timeSpent "
    "issue { iid title } "
    "mergeRequest { iid title } "
    "project { name fullPath } "
    "} } }"
)


def _format_duration(seconds: int) -> str:
    """Format a duration in seconds as '8h', '1h 30m', '45m', '59s', '0m', or a negative form.

    timeSpent is not guaranteed to be positive or a whole number of hours —
    GitLab accepts sub-hour quick actions such as '/spend 15m', and a
    negative '/spend' creates a negative-duration correction entry rather
    than mutating the original (GitLab's "cannot go below 0" clamp bounds an
    issuable's cumulative total, not a single timelog entry). The sign is
    extracted before decomposing the magnitude so a negative value never
    borrows an hour the way floor-division on a negative int would.

    Every non-zero component (hours, minutes, seconds) that is present is
    shown — flooring a sub-minute remainder to nothing would make a row's
    displayed value disagree with the day total it is part of, since the
    day total is the exact sum of the same underlying integers.
    """
    if seconds == 0:
        return "0m"
    sign = "-" if seconds < 0 else ""
    magnitude = abs(seconds)
    hours, remainder = divmod(magnitude, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs:
        parts.append(f"{secs}s")
    return sign + " ".join(parts)


def _format_utc_offset(moment: datetime) -> str:
    """Format an aware datetime's UTC offset as '+06:00' / '-05:00'."""
    offset = moment.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _parse_spent_at(value: str) -> datetime:
    """Parse a GitLab spentAt UTC timestamp ('...Z' suffixed) into an aware datetime."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _project_identity(project: Dict[str, Any]) -> str:
    """Return a project's identity string: full path, falling back to name.

    Used to project-qualify the per-issue/per-MR grouping key — GitLab IIDs
    are per-project, not global (see _target_label) — and as the key into
    the disambiguated display-label map (see _disambiguated_project_labels).
    """
    return project.get("fullPath") or project.get("name") or "unknown"


def _project_short_name(identity: str) -> str:
    """Return a project's short display name: the last path segment of its identity."""
    return identity.rsplit("/", 1)[-1]


# pylint: disable=too-few-public-methods
# TimelogHandler is intentionally a single-responsibility class with one public
# entry point (report); the rest are private steps of that one operation.
class TimelogHandler:
    """Reports the current user's own GitLab timelogs over a local-calendar-day window."""

    def __init__(self, tz: Optional[tzinfo] = None) -> None:
        """Initialize the handler.

        Args:
            tz: Overrides the system local timezone used to build the query
                window and to bucket entries by day. There is no CLI flag or
                config key for this — production callers always pass None,
                which resolves to the system's local timezone at call time.
                This exists purely as a test seam so day-boundary tests do
                not depend on the timezone of the machine running them.
        """
        self._tz = tz

    # ------------------------------------------------------------------
    # Date handling
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date_arg(value: str) -> date:
        """Parse a YYYY-MM-DD date argument.

        Raises:
            ValueError: If the value is not a valid calendar date in that format.
        """
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc

    def _local_bound(self, day: date, end_of_day: bool) -> datetime:
        """Return the local start- or end-of-day instant for `day`, offset-aware.

        With no injected tz (the production path), `astimezone(None)` treats
        the naive value as already being in system-local time and simply
        attaches that offset — the correct reading, resolved per-date so a
        date on the far side of a DST transition still gets it right. With an
        injected tz (the test seam), the naive value must instead be
        *localized* into that zone via `replace(tzinfo=...)`: `astimezone()`
        would first assume the naive value is system-local and then convert
        it, shifting the window by (tz offset − system offset) whenever the
        two differ — invisible only when they happen to coincide.

        `endTime` is inclusive on the GitLab side ("equal to or before"), so
        the end bound is the last microsecond of the day rather than the
        next day's midnight — the latter would double-count an entry logged
        at exactly local midnight across two adjacent day queries. GitLab
        filters spentAt at microsecond precision, so stopping at :59 (no
        microseconds) would itself exclude any entry in the last second of
        the day; :59.999999 closes that gap at the same cost.
        """
        naive = datetime.combine(day, time(23, 59, 59, 999999) if end_of_day else time.min)
        if self._tz is None:
            return naive.astimezone(self._tz)
        return naive.replace(tzinfo=self._tz)

    # ------------------------------------------------------------------
    # GraphQL transport
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_graphql_response(output: str) -> Dict[str, Any]:
        """Decode a glab GraphQL response and return its 'data' object.

        Raises:
            PlatformError: If the output is not valid JSON, decodes to a
                non-object top level (e.g. a bare JSON array), or the
                top-level 'errors' array is present. GitLab's GraphQL
                endpoint can report query errors in an HTTP 200 response, so
                this is checked independently of glab's own subprocess exit
                code.
        """
        try:
            resp = json.loads(output)
        except json.JSONDecodeError as exc:
            raise PlatformError(f"Unexpected glab response: {output[:200]!r}") from exc
        if not isinstance(resp, dict):
            # Valid JSON with the wrong shape (e.g. "[]") would otherwise reach
            # resp.get() below and crash with an uncaught AttributeError instead
            # of the normal PlatformError error path.
            raise PlatformError(f"Unexpected glab response: {output[:200]!r}")
        errors = resp.get("errors")
        if errors:
            raise PlatformError(f"GraphQL query failed: {errors}")
        return resp.get("data") or {}  # type: ignore[no-any-return]

    def _resolve_current_user(self) -> str:
        """Resolve the authenticated GitLab username via GraphQL.

        This is the load-bearing safety check for the whole command: `glab`
        resolves its target host from the current directory's git remote and
        silently falls back to a default host outside a GitLab repo, at exit
        code 0 with `currentUser: null`. For a command whose entire purpose
        is answering "did I log time", treating that null as "zero results"
        would be indistinguishable from a true zero — so it is a hard error
        instead, naming the likely cause.

        Raises:
            PlatformError: If currentUser resolves to null, or the request fails.
        """
        cmd = ["api", "graphql", "-f", f"query={_CURRENT_USER_QUERY}"]
        data = self._parse_graphql_response(run_glab_command(cmd))
        username = (data.get("currentUser") or {}).get("username")
        if not username:
            raise PlatformError(
                "GitLab GraphQL currentUser returned null — glab likely resolved the "
                "wrong host (e.g. this directory has no GitLab remote, so glab fell "
                "back to a default host with no authenticated session). Run from "
                "inside a GitLab-remote repository, or check 'glab auth status'."
            )
        return str(username)

    def _fetch_timelogs(
        self, username: str, start_time: datetime, end_time: datetime
    ) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        """Fetch every timelog node in [start_time, end_time], following cursors.

        Never terminates on TimelogConnection.count — GitLab's own schema
        documents it as saturating at "limit + 1" once the filtered set
        exceeds the page size, so pageInfo.hasNextPage is the only reliable
        termination signal.

        Pagination fails closed on three conditions: a page reporting
        hasNextPage=true with no endCursor to continue from, a page
        reporting hasNextPage=true with a cursor already seen this call
        (a server bug that would otherwise loop forever), and the page
        count exceeding _MAX_PAGES (seen_cursors only catches a *repeated*
        cursor, not a server minting a fresh one every page). Each is a
        hard error rather than a silent stop — stopping quietly would print
        a report built from a truncated node set at exit code 0,
        indistinguishable from a complete one.

        Returns:
            Tuple of (every node across every page, server-computed
            totalSpentTime in seconds, or None if the key was absent from
            the response or unparsable). The value is taken from the first
            page where the key is present and never revisited afterward —
            see the total_spent_time_seen comment below for why.

        Raises:
            PlatformError: If any page request fails, a page reports
                hasNextPage=true without a usable cursor to continue from,
                or pagination exceeds _MAX_PAGES.
        """
        nodes: List[Dict[str, Any]] = []
        total_spent_time: Optional[int] = None
        # A well-behaved server computes totalSpentTime once over the whole
        # filtered set, so every page repeats the same value — but taking
        # the *last* page's value instead of the first would let a genuine
        # page-1-vs-final-page divergence go unwarned, since only the final
        # value would ever reach the cross-check in _print_report. Latching
        # on the first page where the key is present (regardless of whether
        # it parses) means a later page can never paper over an earlier
        # disagreement.
        total_spent_time_seen = False
        cursor: Optional[str] = None
        seen_cursors: Set[str] = set()
        page_count = 0

        while True:
            page_count += 1
            if page_count > _MAX_PAGES:
                raise PlatformError(
                    f"Timelog pagination did not terminate within {_MAX_PAGES} "
                    "pages — refusing to keep requesting more from what looks "
                    "like a malfunctioning server. Try narrowing the date "
                    "window (a shorter range stays under one page and "
                    "sidesteps pagination)."
                )

            cmd = [
                "api",
                "graphql",
                "-f",
                f"query={_TIMELOGS_QUERY}",
                "-f",
                f"username={username}",
                "-f",
                f"startTime={start_time.isoformat()}",
                "-f",
                f"endTime={end_time.isoformat()}",
                "-F",
                f"first={_PAGE_SIZE}",
            ]
            if cursor:
                cmd += ["-f", f"after={cursor}"]

            data = self._parse_graphql_response(run_glab_command(cmd))
            connection = data.get("timelogs") or {}
            nodes.extend(connection.get("nodes") or [])
            if not total_spent_time_seen:
                # BigInt serializes as a JSON string. A missing key (malformed
                # response) stays None rather than collapsing to 0, so
                # _print_report's cross-check can tell "server said nothing"
                # apart from "server said zero" — and a present-but-unparsable
                # value (e.g. a non-numeric string) degrades to that same
                # None rather than raising, since it is advisory input, not
                # data the report depends on to produce a correct total.
                raw_total = connection.get("totalSpentTime")
                if raw_total is not None:
                    total_spent_time_seen = True
                    try:
                        total_spent_time = int(raw_total)
                    except (TypeError, ValueError):
                        total_spent_time = None

            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break

            next_cursor = page_info.get("endCursor")
            if not next_cursor:
                raise PlatformError(
                    "GitLab reported another page of timelogs (hasNextPage=true) "
                    "but returned no endCursor to continue from — refusing to "
                    "print a report built from a truncated node set. Try "
                    "narrowing the date window (a shorter range stays under "
                    "one page and sidesteps pagination)."
                )
            if next_cursor in seen_cursors:
                raise PlatformError(
                    f"GitLab repeated the same endCursor ({next_cursor!r}) "
                    "across pages instead of advancing — refusing to loop "
                    "forever against what looks like a server bug. Try "
                    "narrowing the date window (a shorter range stays under "
                    "one page and sidesteps pagination)."
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return nodes, total_spent_time

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _bucket_by_local_day(self, nodes: List[Dict[str, Any]]) -> Dict[date, List[Dict[str, Any]]]:
        """Group timelog nodes by the local calendar date derived from spentAt.

        Never deduplicates: every node is appended to its bucket even when
        another node in the same bucket has an identical spentAt and
        timeSpent on the same issue — GitLab's own data contains exactly
        that shape, and collapsing them would silently understate real
        logged time.
        """
        buckets: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
        for node in nodes:
            spent_at = node.get("spentAt")
            if not spent_at:
                continue
            local_dt = _parse_spent_at(spent_at).astimezone(self._tz)
            buckets[local_dt.date()].append(node)
        return buckets

    @staticmethod
    def _disambiguated_project_labels(nodes: List[Dict[str, Any]]) -> Dict[str, str]:
        """Map each project identity in the window to a display label.

        The label is the project's short name (its identity's last path
        segment) when that short name belongs to exactly one identity in the
        window. Two projects can share a short name — team-a/docs and
        team-b/docs both end in "docs" — in which case the short name no
        longer identifies a project; those identities fall back to their
        full identity string instead. The grouping key (see _target_label)
        always uses the full identity regardless, so rows never merge —
        only the label was ambiguous.
        """
        identities = {_project_identity(n.get("project") or {}) for n in nodes}
        owners_by_short_name: Dict[str, List[str]] = defaultdict(list)
        for identity in identities:
            owners_by_short_name[_project_short_name(identity)].append(identity)

        labels: Dict[str, str] = {}
        for short_name, owners in owners_by_short_name.items():
            for identity in owners:
                labels[identity] = short_name if len(owners) == 1 else identity
        return labels

    @staticmethod
    def _target_label(node: Dict[str, Any], project_labels: Dict[str, str]) -> Tuple[str, str]:
        """Return (display label, grouping key) for what a timelog entry is attached to.

        The grouping key always includes the project's identity, regardless
        of whether the label is prefixed: GitLab issue/MR IIDs are
        per-project, not global, and the timelogs query is unscoped across
        every project the user has logged against, so two different issues
        sharing an IID in different projects must never collapse into one
        row. The label is prefixed with the disambiguated project name (see
        _disambiguated_project_labels) only when the window spans more than
        one project — len(project_labels) > 1 is exactly that condition,
        since every distinct project identity gets one entry in the map —
        so a single-project window renders exactly as before.

        issue and mergeRequest were mutually exclusive in every observed
        record, but the schema does not guarantee it, and `project` is
        non-null on every timelog — so project is the fallback identity for
        "neither set" rather than an assumed-unreachable case. That fallback
        also uses the disambiguated label, not the bare project name, so two
        differently-named-but-same-short-name projects don't render two
        identical-looking bare rows with different durations.
        """
        project = node.get("project") or {}
        identity = _project_identity(project)
        label_for_identity = project_labels.get(identity, identity)
        prefix = f"{label_for_identity} " if len(project_labels) > 1 else ""

        issue = node.get("issue")
        if issue:
            title = issue.get("title", "")
            label = f"{prefix}#{issue.get('iid')} {title}".strip()
            return label, f"issue:{identity}:{issue.get('iid')}"
        merge_request = node.get("mergeRequest")
        if merge_request:
            title = merge_request.get("title", "")
            label = f"{prefix}!{merge_request.get('iid')} {title}".strip()
            return label, f"mr:{identity}:{merge_request.get('iid')}"
        return f"(no issue/MR) {label_for_identity}", f"project:{identity}"

    def _day_rows(
        self, nodes: List[Dict[str, Any]], project_labels: Dict[str, str]
    ) -> List[Tuple[str, int]]:
        """Sum timeSpent per target within one day, in first-seen order.

        Summing into an accumulator (rather than overwriting a dict entry)
        is what satisfies the anti-dedup guarantee for repeat entries
        against the same target on the same day. Iterating `totals` directly
        (rather than a separately tracked insertion-order list) is enough to
        preserve first-seen order — dicts have guaranteed insertion order
        since Python 3.7.
        """
        totals: Dict[str, int] = {}
        labels: Dict[str, str] = {}
        for node in nodes:
            label, key = self._target_label(node, project_labels)
            if key not in totals:
                labels[key] = label
                totals[key] = 0
            totals[key] += int(node.get("timeSpent") or 0)
        return [(labels[key], total) for key, total in totals.items()]

    def _print_days(
        self,
        buckets: Dict[date, List[Dict[str, Any]]],
        project_labels: Dict[str, str],
    ) -> int:
        """Print one section per day, oldest first, and return the sum of every row printed.

        day_total is derived from the same rows this method prints (rather
        than a second, independent sum over day_nodes), so the day header
        reconciles with its rows by construction, not by coincidence — a
        future filter inside _day_rows() could no longer silently break
        that invariant while this docstring still claimed it held.
        """
        local_total = 0
        for day in sorted(buckets):
            rows = self._day_rows(buckets[day], project_labels)
            day_total = sum(seconds for _, seconds in rows)
            local_total += day_total
            print(f"{day.isoformat()} — {_format_duration(day_total)}")
            for label, seconds in rows:
                print(f"  {label} — {_format_duration(seconds)}")
            print()
        return local_total

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _print_report(  # pylint: disable=too-many-positional-arguments
        # 5 distinct data items plus self exceeds pylint's default cap of 5;
        # start/end were dropped from this signature (they're derivable from
        # start_time/end_time — see the docstring) rather than papering over
        # the count with a bag-of-fields object for what's still one call site.
        self,
        username: str,
        start_time: datetime,
        end_time: datetime,
        nodes: List[Dict[str, Any]],
        server_total_spent_time: Optional[int],
    ) -> None:
        """Print the window/identity header, then a per-day breakdown or a zero notice.

        The header prints unconditionally — including on a zero result — so
        an operator can tell a true "nothing logged" apart from a query
        misdirected at the wrong host (see _resolve_current_user).

        The calendar dates in the header are read off start_time/end_time
        rather than taken as separate parameters: _local_bound() builds each
        from exactly one date with only the time-of-day changed, so
        start_time.date() and end_time.date() always equal the dates report()
        parsed them from — carrying those dates as two more parameters would
        just be the same information twice.

        The printed grand total is the sum of the same nodes that produce
        the rows above it, so it reconciles with them by construction —
        `server_total_spent_time` (GitLab's `totalSpentTime`, computed
        server-side; None when the response omitted the key entirely) is
        used only as a cross-check, logged as a warning when it disagrees
        and skipped outright when None — a malformed response is not the
        same claim as "the server computed zero". GitLab documents
        `totalSpentTime` as computed *before* authorization filtering
        (gitlab-org/gitlab#425747), so the two can legitimately diverge; a
        report that silently printed the server value instead would be able
        to disagree with its own rows.
        """
        start, end = start_time.date(), end_time.date()
        window = start.isoformat() if end == start else f"{start.isoformat()} to {end.isoformat()}"
        offset = _format_utc_offset(start_time)
        print(f"Timelogs for {username} — {window} (local time, UTC{offset})")
        print(f"Queried window: {start_time.isoformat()} .. {end_time.isoformat()}")
        print()

        if not nodes:
            print("No timelogs returned for this window.")
            return

        project_labels = self._disambiguated_project_labels(nodes)
        buckets = self._bucket_by_local_day(nodes)

        bucketed_count = sum(len(day_nodes) for day_nodes in buckets.values())
        if bucketed_count != len(nodes):
            logger.warning(
                "%d of %d timelog node(s) had no spentAt and were excluded from "
                "the day breakdown; totals reflect only the %d node(s) shown.",
                len(nodes) - bucketed_count,
                len(nodes),
                bucketed_count,
            )

        local_total = self._print_days(buckets, project_labels)

        if server_total_spent_time is not None and server_total_spent_time != local_total:
            logger.warning(
                "GitLab's server-computed total (%s) differs from the sum of "
                "this report's rows (%s); the printed 'Total:' line is the "
                "row sum, not the server value.",
                _format_duration(server_total_spent_time),
                _format_duration(local_total),
            )

        entry_word = "entry" if bucketed_count == 1 else "entries"
        print(
            f"Total: {_format_duration(local_total)} across "
            f"{len(buckets)} day(s), {bucketed_count} {entry_word}"
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def report(self, date_str: Optional[str] = None, to_str: Optional[str] = None) -> None:
        """Print a per-day, per-issue timelog report for [date_str, to_str], inclusive.

        Args:
            date_str: Start date as YYYY-MM-DD; defaults to today (local date).
            to_str: End date as YYYY-MM-DD; defaults to date_str (a single day).

        Raises:
            ValueError: If a date argument is malformed, or to_str precedes date_str.
            PlatformError: If the authenticated user cannot be resolved, or the
                GraphQL request fails.
        """
        start = self._parse_date_arg(date_str) if date_str else datetime.now(self._tz).date()
        end = self._parse_date_arg(to_str) if to_str else start
        if end < start:
            raise ValueError(
                f"--to ({end.isoformat()}) cannot be earlier than the report date "
                f"({start.isoformat()})"
            )

        start_time = self._local_bound(start, end_of_day=False)
        end_time = self._local_bound(end, end_of_day=True)

        username = self._resolve_current_user()
        nodes, total_spent_time = self._fetch_timelogs(username, start_time, end_time)
        self._print_report(username, start_time, end_time, nodes, total_spent_time)
