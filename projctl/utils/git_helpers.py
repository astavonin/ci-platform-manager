"""Git repository utilities."""

import subprocess
from pathlib import Path
from typing import Optional, Tuple


def extract_path_from_url(url: str) -> str:
    """Extract the repository/group path from a GitLab URL.

    Strips the scheme and host, returning the path component.

    Args:
        url: A GitLab URL such as ``https://gitlab.com/group/project``.

    Returns:
        The path portion, e.g. ``group/project``.
    """
    if "//" in url:
        return "/".join(url.split("//")[1].split("/")[1:])
    return url


def extract_host_from_url(url: str) -> str:
    """Extract the host component from a GitLab URL.

    Symmetric counterpart to extract_path_from_url(): that function strips
    scheme+host and keeps the path; this one keeps only the host. Needed
    wherever a URL-derived project path is used to build an API request —
    the host the URL names must travel alongside the path (e.g. as glab's
    --hostname), not be silently discarded in favor of whatever host glab
    would otherwise pick as its ambient default.

    Args:
        url: A GitLab URL such as ``https://gitlab.example.com/group/project``.

    Returns:
        The host portion, e.g. ``gitlab.example.com``, or an empty string if
        the URL has no ``//`` scheme separator to locate the host after.
    """
    if "//" in url:
        return url.split("//")[1].split("/")[0]
    return ""


def parse_issue_url(issue_ref: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a GitLab issue URL or reference to extract project path and iid.

    Supports three formats:
    - Full URL:  https://gitlab.../group/project/-/issues/123
    - Prefixed:  #123
    - Plain:     123

    Args:
        issue_ref: Issue reference string.

    Returns:
        Tuple of (project_path, iid). project_path is None when not in a URL.
        Both values are None when the reference format is not recognised.
    """
    if "/-/issues/" in issue_ref:
        parts = issue_ref.split("/-/issues/")
        if len(parts) == 2:
            project_url = parts[0]
            iid = parts[1].split("/")[0].split("?")[0].split("#")[0]

            # Extract project path from URL
            # Format: https://gitlab.example.com/group/subgroup/project
            project_path = extract_path_from_url(project_url)

            return (project_path, iid)

    # GitLab work_items URL format: https://gitlab.../group/project/-/work_items/123
    if "/-/work_items/" in issue_ref:
        parts = issue_ref.split("/-/work_items/")
        if len(parts) == 2:
            project_url = parts[0]
            iid = parts[1].split("/")[0].split("?")[0].split("#")[0]
            project_path = extract_path_from_url(project_url)
            return (project_path, iid)

    if issue_ref.startswith("#"):
        return (None, issue_ref[1:])

    if issue_ref.isdigit():
        return (None, issue_ref)

    return (None, None)


def parse_epic_url(epic_ref: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a GitLab epic URL or reference to extract group path and iid.

    Supports three formats:
    - Full URL:  https://gitlab.../groups/mygroup/-/epics/21
    - Prefixed:  &21
    - Plain:     21

    Args:
        epic_ref: Epic reference string.

    Returns:
        Tuple of (group_path, iid). group_path is None when not in a URL.
        Both values are None when the reference format is not recognised.
    """
    if "/-/epics/" in epic_ref:
        parts = epic_ref.split("/-/epics/")
        if len(parts) == 2:
            group_url = parts[0]
            iid = parts[1].split("/")[0].split("?")[0].split("#")[0]
            if "/groups/" in group_url:
                group_path = group_url.split("/groups/")[-1]
            else:
                group_path = extract_path_from_url(group_url)
            return (group_path, iid)

    if epic_ref.startswith("&"):
        return (None, epic_ref[1:])

    if epic_ref.isdigit():
        return (None, epic_ref)

    return (None, None)


def parse_mr_url(mr_ref: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a GitLab MR URL or reference to extract project path and iid.

    Supports three formats:
    - Full URL:  https://gitlab.../group/project/-/merge_requests/123
    - Prefixed:  !123
    - Plain:     123

    Scope: this is the canonical parser for timelog.py's write path
    (TimelogHandler._resolve_target()) only. note.py, updater.py, and
    loader.py each parse MR references with their own inline logic and
    diverge from this one and each other on a URL fragment such as
    "#note_456" appended after the iid — e.g. note.py's _normalize_mr_ref()
    URL-encodes the project path where this function does not, and
    loader.py's two inline sites do not strip the fragment at all. Converging
    all four onto this helper would touch three already-hardened,
    unrelated modules outside the timelog write path's review scope;
    TestParseMrUrl below is scoped to this function alone and does not
    imply the other three sites share its behavior.

    Args:
        mr_ref: MR reference string.

    Returns:
        Tuple of (project_path, iid). project_path is None when not in a URL.
        Both values are None when the reference format is not recognised.
    """
    if "/-/merge_requests/" in mr_ref:
        parts = mr_ref.split("/-/merge_requests/")
        if len(parts) == 2:
            project_url = parts[0]
            iid = parts[1].split("/")[0].split("?")[0].split("#")[0]
            project_path = extract_path_from_url(project_url)
            return (project_path, iid)

    if mr_ref.startswith("!"):
        return (None, mr_ref[1:])

    if mr_ref.isdigit():
        return (None, mr_ref)

    return (None, None)


def get_gitlab_base_url() -> str:
    """Derive the GitLab base URL from the git remote origin.

    Returns:
        Base URL such as ``https://gitlab.example.com``, or empty string
        if it cannot be determined.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path.cwd(),
        )
        remote = result.stdout.strip()
        if remote.startswith("http"):
            # https://gitlab.example.com/group/project.git
            parts = remote.split("/")
            return f"{parts[0]}//{parts[2]}"
        if "@" in remote:
            # git@gitlab.example.com:group/project.git
            host = remote.split("@")[1].split(":")[0]
            return f"https://{host}"
    except (subprocess.CalledProcessError, IndexError):
        pass
    return ""


def get_current_repo_path() -> Optional[str]:
    """Get current repository full path from git remote.

    Returns:
        Repository path (e.g., 'group/project') or None if not in a repo.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path.cwd(),
        )

        remote_url = result.stdout.strip()

        # Parse GitLab/GitHub URL
        # Examples:
        #   https://gitlab.com/group/project.git
        #   git@gitlab.com:group/project.git
        #   https://github.com/owner/repo.git

        if "@" in remote_url:
            # SSH format: git@host:path.git
            path = remote_url.split(":", 1)[1]
        else:
            # HTTPS format: https://host/path.git
            path = remote_url.split("/", 3)[-1]

        # Remove .git suffix
        if path.endswith(".git"):
            path = path[:-4]

        return path

    except (subprocess.CalledProcessError, IndexError):
        return None
