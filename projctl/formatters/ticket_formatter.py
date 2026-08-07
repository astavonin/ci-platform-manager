"""Markdown formatters for GitLab tickets (issues, epics, milestones, MRs)."""

from typing import Any, Dict, List, Optional

from .utils import format_user, format_users


def _issue_assignee_str(issue: Dict[str, Any]) -> str:
    """Return a formatted owner string for an issue in an epic list."""
    assignees = issue.get("assignees") or []
    if assignees:
        return format_users(assignees)
    single = issue.get("assignee")
    if single:
        return format_user(single)
    return "unassigned"


def _print_timing(timing: Dict[str, Any]) -> None:
    """Print status and timing block for an issue."""
    current_status = timing.get("current_status")
    if current_status:
        print(f"**Status:** {current_status}  ")
    if timing.get("is_rejected"):
        print("**Time counted:** No (rejected)  ")
    else:
        start = timing.get("start_date")
        end = timing.get("end_date")
        if start:
            print(f"**Started:** {start}  ")
        if end:
            print(f"**Completed:** {end}  ")


def _print_link_list(heading: str, links: List[Dict[str, Any]]) -> None:
    """Print a section of issue dependency links."""
    print(f"\n{heading}\n")
    for link in links:
        link_iid = link.get("iid")
        link_title = link.get("title", "Untitled")
        link_state = link.get("state", "unknown")
        link_url = link.get("web_url", "")
        print(f"- [#{link_iid} {link_title}]({link_url}) `[{link_state}]`")


def _print_issue_links(links: Dict[str, List[Dict[str, Any]]]) -> None:
    """Print blocking/blocked-by dependency sections."""
    blocked_by = links.get("blocked_by", [])
    blocking = links.get("blocking", [])
    if blocked_by:
        _print_link_list("### ⛔ Blocked By", blocked_by)
    if blocking:
        _print_link_list("### 🚧 Blocking", blocking)


def _print_epic_issue_group(heading: str, issues: List[Dict[str, Any]], show_labels: bool) -> None:
    """Print one group (opened or closed) of issues in an epic."""
    print(f"{heading}\n")
    for issue in issues:
        iid = issue.get("iid")
        title = issue.get("title", "Untitled")
        url = issue.get("web_url", "")
        print(f"- [#{iid} {title}]({url})")
        print(f"  - Owner: {_issue_assignee_str(issue)}")
        if show_labels:
            issue_labels = issue.get("labels", [])
            label_str = (
                ", ".join([f"`{label}`" for label in issue_labels[:3]])
                if issue_labels
                else "*none*"
            )
            if len(issue_labels) > 3:
                label_str += f" *+{len(issue_labels) - 3} more*"
            print(f"  - Labels: {label_str}")
    print()


def print_issue(
    issue: Dict[str, Any],
    epic: Optional[Dict[str, Any]] = None,
    links: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    timing: Optional[Dict[str, Any]] = None,
) -> None:
    """Print issue information in markdown format."""
    print(f"# Issue #{issue.get('iid')}: {issue.get('title')}\n")
    print(f"**URL:** {issue.get('web_url')}  ")
    print(f"**State:** {issue.get('state')}  ")

    if timing:
        _print_timing(timing)

    print(f"**Author:** {issue.get('author', {}).get('name', 'Unknown')}  ")

    labels = issue.get("labels", [])
    if labels:
        print(f"**Labels:** {', '.join([f'`{label}`' for label in labels])}  ")

    assignees = issue.get("assignees", [])
    if assignees:
        print(f"**Assignees:** {format_users(assignees)}  ")

    if issue.get("milestone"):
        print(f"**Milestone:** {issue['milestone'].get('title')}  ")

    if issue.get("due_date"):
        print(f"**Due Date:** {issue['due_date']}  ")

    print(f"\n**Created:** {issue.get('created_at')}  ")
    print(f"**Updated:** {issue.get('updated_at')}  ")

    if links:
        _print_issue_links(links)

    if epic:
        print(f"\n## Epic &{epic.get('iid')}: {epic.get('title')}\n")
        print(f"**URL:** {epic.get('web_url')}  ")
        print(f"**State:** {epic.get('state')}  ")

    print("\n## Description\n")
    description = issue.get("description", "No description")
    print(description if description else "*No description*")
    print()


def print_epic(
    epic: Dict[str, Any],
    issues: List[Dict[str, Any]],
    derived_dates: Optional[Dict[str, Optional[str]]] = None,
) -> None:
    """Print epic information in markdown format."""
    print(f"# Epic &{epic.get('iid')}: {epic.get('title')}\n")
    print(f"**URL:** {epic.get('web_url')}  ")
    print(f"**State:** {epic.get('state')}  ")

    assignees = epic.get("assignees") or []
    owner_str = format_users(assignees) if assignees else format_user(epic.get("author") or {})
    print(f"**Owner:** {owner_str}  ")

    labels = epic.get("labels", [])
    if labels:
        label_names = [
            label.get("name", label) if isinstance(label, dict) else label for label in labels
        ]
        print(f"**Labels:** {', '.join([f'`{label}`' for label in label_names])}  ")

    if derived_dates:
        if derived_dates.get("start_date"):
            print(f"**Started:** {derived_dates['start_date']}  ")
        if derived_dates.get("end_date"):
            print(f"**Completed:** {derived_dates['end_date']}  ")

    print(f"\n**Created:** {epic.get('created_at')}  ")
    print(f"**Updated:** {epic.get('updated_at')}  ")

    print(f"\n## Issues in Epic ({len(issues)})\n")
    if not issues:
        print("*No issues in this epic*\n")
    else:
        opened = [i for i in issues if i.get("state") == "opened"]
        closed = [i for i in issues if i.get("state") == "closed"]
        if opened:
            _print_epic_issue_group(f"### Opened ({len(opened)})", opened, show_labels=True)
        if closed:
            _print_epic_issue_group(f"### Closed ({len(closed)})", closed, show_labels=False)

    print("## Description\n")
    description = epic.get("description", "No description")
    print(description if description else "*No description*")
    print()


def _print_issue_line(issue: Dict[str, Any]) -> None:
    """Print a single issue line in milestone breakdown format."""
    iid = issue.get("iid")
    title = issue.get("title", "Untitled")
    state = issue.get("state", "unknown")
    print(f"- #{iid} {title} `[{state}]`")


def _print_epic_breakdown(
    issues: List[Dict[str, Any]],
    epic_map: Dict[int, Dict[str, Any]],
) -> None:
    """Print the Epic Breakdown section of a milestone report."""
    print("\n## Epic Breakdown\n")
    if not issues:
        print("*No issues in this milestone*\n")
        return

    issues_by_epic: Dict[Any, List[Dict[str, Any]]] = {}
    issues_without_epic: List[Dict[str, Any]] = []
    for issue in issues:
        epic_iid = issue.get("epic_iid")
        if epic_iid:
            issues_by_epic.setdefault(epic_iid, []).append(issue)
        else:
            issues_without_epic.append(issue)

    for epic_iid, epic_issues in sorted(issues_by_epic.items()):
        epic_data = epic_map.get(epic_iid)
        if epic_data:
            print(f"### Epic &{epic_iid}: {epic_data.get('title', 'Unknown')}\n")
        else:
            print(f"### Epic &{epic_iid}\n")
        for issue in epic_issues:
            _print_issue_line(issue)
        print()

    if issues_without_epic:
        print("### No Epic\n")
        for issue in issues_without_epic:
            _print_issue_line(issue)
        print()


def print_milestone(
    milestone: Dict[str, Any],
    issues: List[Dict[str, Any]],
    epic_map: Dict[int, Dict[str, Any]],
) -> None:
    """Print milestone information in markdown format."""
    print(f"# Milestone %{milestone.get('iid')}: {milestone.get('title')}\n")
    print(f"**URL:** {milestone.get('web_url')}  ")
    print(f"**State:** {milestone.get('state')}  ")

    if milestone.get("start_date"):
        print(f"**Start Date:** {milestone['start_date']}  ")

    if milestone.get("due_date"):
        print(f"**Due Date:** {milestone['due_date']}  ")

    total_issues = len(issues)
    closed_count = len([i for i in issues if i.get("state") == "closed"])
    print(f"**Progress:** {closed_count}/{total_issues} issues closed  ")

    print(f"\n**Created:** {milestone.get('created_at')}  ")
    print(f"**Updated:** {milestone.get('updated_at')}  ")

    _print_epic_breakdown(issues, epic_map)

    print("## Description\n")
    description = milestone.get("description", "No description")
    print(description if description else "*No description*")
    print()


def print_mr(mr: Dict[str, Any]) -> None:
    """Print merge request information in markdown format."""
    print(f"# MR !{mr.get('iid')}: {mr.get('title')}\n")
    print(f"**URL:** {mr.get('web_url')}  ")
    print(f"**State:** {mr.get('state')}  ")
    print(f"**Author:** {mr.get('author', {}).get('name', 'Unknown')}  ")

    if mr.get("draft"):
        print("**Draft:** Yes  ")

    if mr.get("source_branch"):
        print(f"**Source Branch:** `{mr['source_branch']}`  ")

    if mr.get("target_branch"):
        print(f"**Target Branch:** `{mr['target_branch']}`  ")

    labels = mr.get("labels", [])
    if labels:
        print(f"**Labels:** {', '.join([f'`{label}`' for label in labels])}  ")

    assignees = mr.get("assignees", [])
    if assignees:
        print(f"**Assignees:** {format_users(assignees)}  ")

    reviewers = mr.get("reviewers", [])
    if reviewers:
        print(f"**Reviewers:** {format_users(reviewers)}  ")

    if mr.get("milestone"):
        print(f"**Milestone:** {mr['milestone'].get('title')}  ")

    print(f"\n**Created:** {mr.get('created_at')}  ")
    print(f"**Updated:** {mr.get('updated_at')}  ")

    if mr.get("merged_at"):
        print(f"**Merged:** {mr['merged_at']}  ")

    if mr.get("pipeline"):
        print(f"**Pipeline Status:** {mr['pipeline'].get('status', 'unknown')}  ")

    print("\n## Description\n")
    description = mr.get("description", "No description")
    print(description if description else "*No description*")
    print()


def print_mr_comments(comments: List[Dict[str, Any]]) -> None:
    """Print MR review comments grouped by open/resolved in markdown format."""
    if not comments:
        print("## Review Comments\n\n*No review comments.*\n")
        return

    open_comments = [c for c in comments if not c.get("resolved")]
    resolved_comments = [c for c in comments if c.get("resolved")]

    def _print_comment(comment: Dict[str, Any]) -> None:
        author = comment.get("author", "Unknown")
        file_path = comment.get("file_path", "")
        line = comment.get("line", "")
        location = f" — `{file_path}:{line}`" if file_path else ""
        resolvable = comment.get("resolvable", False)
        resolved = comment.get("resolved", False)
        status = " ✅" if resolved else (" 🔴" if resolvable else "")
        print(f"**{author}**{location}{status}\n")
        # Not gated on `resolvable`: an unresolvable top-level note is still a valid
        # `replies:` target, so withholding its id here would strand it.
        discussion_id = comment.get("discussion_id", "")
        if discussion_id:
            print(f"`thread: {discussion_id}`\n")
        print(comment.get("body", ""))
        print()

    if open_comments:
        print(f"## Review Comments — Open ({len(open_comments)})\n")
        for comment in open_comments:
            print("---\n")
            _print_comment(comment)

    if resolved_comments:
        print(f"## Review Comments — Resolved ({len(resolved_comments)})\n")
        for comment in resolved_comments:
            print("---\n")
            _print_comment(comment)
