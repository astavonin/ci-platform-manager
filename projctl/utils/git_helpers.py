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


def get_gitlab_base_url() -> str:
    """Derive the GitLab base URL from the git remote origin.

    Returns:
        Base URL such as ``https://gitlab.cartrack.com``, or empty string
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
            # https://gitlab.cartrack.com/group/project.git
            parts = remote.split("/")
            return f"{parts[0]}//{parts[2]}"
        if "@" in remote:
            # git@gitlab.cartrack.com:group/project.git
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
