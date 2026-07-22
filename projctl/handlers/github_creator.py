"""GitHub issue creation handler."""

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError as exc:
    raise ImportError("PyYAML is required. Install with: pip install PyYAML") from exc

from ..config import Config
from ..exceptions import PlatformError
from ..utils.gh_runner import run_gh_command
from ..utils.validation import (
    apply_required_issue_fields,
    validate_issue_description,
    validate_labels,
    validate_required_label_groups,
)

logger = logging.getLogger(__name__)


class GithubIssueCreator:
    """Creates GitHub issues from YAML using the gh CLI."""

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        """Initialize the creator.

        Args:
            config: Configuration object with defaults.
            dry_run: If True, only print intent without executing commands.
        """
        self.config = config
        self.repo = config.get_github_repo()
        self.dry_run = dry_run
        self.created_issues: List[Dict[str, str]] = []

    def _run_gh_command(self, cmd: List[str]) -> str:
        """Run a gh command, skipping execution in dry-run mode.

        Args:
            cmd: List of command arguments to pass to gh.

        Returns:
            Command output as a string, or empty string in dry-run mode.

        Raises:
            PlatformError: If the command fails.
        """
        if self.dry_run:
            logger.info("[DRY RUN] Would execute: gh %s", " ".join(cmd))
            return ""
        return run_gh_command(cmd)

    def _resolve_milestone_number(
        self, title: str, cache: Dict[str, Optional[int]]
    ) -> Optional[int]:
        """Resolve a milestone title to its number via the GitHub API.

        Results are cached in the provided dict to avoid redundant API calls
        when multiple issues share the same milestone.

        Args:
            title: Milestone title to resolve.
            cache: Mutable dict used as an in-call cache (title -> number or None).

        Returns:
            Milestone number, or None if not found.

        Raises:
            PlatformError: If the API call fails.
        """
        if title in cache:
            return cache[title]

        # Use per_page=100 instead of --paginate: gh api --paginate concatenates
        # JSON arrays across pages producing invalid JSON for json.loads().
        output = run_gh_command(["api", f"repos/{self.repo}/milestones?per_page=100"])
        milestones: List[Dict[str, Any]] = json.loads(output) if output else []
        if len(milestones) >= 100:
            logger.warning("[github] Milestone list may be truncated; only first 100 returned")
        for ms in milestones:
            ms_title = ms.get("title", "")
            ms_number = ms.get("number")
            if isinstance(ms_title, str) and isinstance(ms_number, int):
                cache[ms_title] = ms_number

        if title not in cache:
            # Milestone not found — create it so issues can be linked immediately.
            logger.info("[github] Milestone '%s' not found, creating it", title)
            create_output = run_gh_command(
                ["api", f"repos/{self.repo}/milestones", "-X", "POST", "-f", f"title={title}"]
            )
            created = json.loads(create_output)
            new_number = created.get("number")
            if isinstance(new_number, int):
                cache[title] = new_number
                logger.info("[github] Created milestone '%s' as #%d", title, new_number)
            else:
                cache[title] = None
                logger.warning(
                    "[github] Created milestone '%s' but could not read its number", title
                )

        return cache[title]

    def _validate_issue_description(self, issue_config: Dict[str, Any]) -> None:
        """Validate that issue description contains required sections.

        Args:
            issue_config: Dictionary containing issue configuration.

        Raises:
            ValueError: If required sections are missing.
        """
        validate_issue_description(
            description=str(issue_config.get("description", "")),
            required_sections=self.config.get_required_sections(),
            issue_title=str(issue_config.get("title", "unknown")),
        )

    def _topological_sort(self, issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return issues sorted in dependency order (dependencies before dependents).

        Uses Kahn's algorithm. Cycle detection raises ValueError before any API
        calls are made.

        Args:
            issues: List of issue configuration dicts from YAML.

        Returns:
            Issues in creation order.

        Raises:
            ValueError: If a dependency cycle is detected, including the cycle path.
        """
        # Build index: yaml_id -> issue_config
        id_map: Dict[str, Dict[str, Any]] = {}
        for issue in issues:
            yaml_id = issue.get("id")
            if yaml_id:
                id_map[str(yaml_id)] = issue

        # Build adjacency: node -> list of nodes that depend on it (reverse edges for Kahn)
        # in_degree: number of unresolved dependencies for each node
        in_degree: Dict[str, int] = {
            str(i.get("id", f"__anon_{idx}__")): 0 for idx, i in enumerate(issues)
        }
        dependents: Dict[str, List[str]] = {k: [] for k in in_degree}

        # Assign stable keys to anonymous issues
        keyed: List[str] = []
        for idx, issue in enumerate(issues):
            yaml_id = issue.get("id")
            keyed.append(str(yaml_id) if yaml_id else f"__anon_{idx}__")

        for idx, issue in enumerate(issues):
            node = keyed[idx]
            for dep in issue.get("dependencies", []):
                dep_str = str(dep)
                if dep_str not in id_map:
                    # Unknown dependency — skip ordering (will still create the issue)
                    continue
                in_degree[node] += 1
                dependents[dep_str].append(node)

        queue: deque[str] = deque(k for k, v in in_degree.items() if v == 0)
        sorted_keys: List[str] = []

        while queue:
            node = queue.popleft()
            sorted_keys.append(node)
            for dependent in dependents[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(sorted_keys) != len(issues):
            # Cycle exists — find it for the error message
            cycle_path = self._find_cycle(in_degree, dependents)
            raise ValueError(f"Dependency cycle detected: {cycle_path}")

        # Reconstruct ordered issue list
        key_to_issue = {keyed[idx]: issue for idx, issue in enumerate(issues)}
        return [key_to_issue[k] for k in sorted_keys]

    @staticmethod
    def _find_cycle(in_degree: Dict[str, int], dependents: Dict[str, List[str]]) -> str:
        """Find and describe a cycle among nodes with non-zero in-degree.

        Args:
            in_degree: Remaining in-degree counts after Kahn's algorithm.
            dependents: Adjacency list (node -> dependents).

        Returns:
            String describing the cycle, e.g. "a -> b -> a".
        """
        remaining = {k for k, v in in_degree.items() if v > 0}
        start = next(iter(remaining))
        path = [start]
        visited = {start}
        current = start

        while True:
            # Walk along any edge from the current node that stays within remaining
            found_next = False
            for dep in dependents.get(current, []):
                if dep in remaining:
                    if dep in visited:
                        # Found the cycle start
                        cycle_start_idx = path.index(dep)
                        cycle = path[cycle_start_idx:] + [dep]
                        return " -> ".join(cycle)
                    path.append(dep)
                    visited.add(dep)
                    current = dep
                    found_next = True
                    break
            if not found_next:
                break

        return " -> ".join(path)

    def _build_issue_cmd(
        self,
        issue_config: Dict[str, Any],
        milestone_title: Optional[str],
    ) -> List[str]:
        """Build the gh issue create command.

        Args:
            issue_config: Issue configuration dictionary.
            milestone_title: Milestone title string, or None.
                `gh issue create --milestone` accepts a title directly,
                so we pass the title rather than a resolved number.

        Returns:
            Command list ready to pass to _run_gh_command.
        """
        cmd = ["issue", "create", "--title", str(issue_config["title"])]

        if "description" in issue_config:
            cmd.extend(["--body", str(issue_config["description"])])

        labels = issue_config.get("labels", [])
        if not isinstance(labels, list):
            labels = []
        for label in labels:
            cmd.extend(["--label", str(label)])

        if "assignee" in issue_config:
            cmd.extend(["--assignee", str(issue_config["assignee"])])

        if milestone_title is not None:
            cmd.extend(["--milestone", milestone_title])

        return cmd

    def create_issue(
        self,
        issue_config: Dict[str, Any],
        milestone_cache: Dict[str, Optional[int]],
    ) -> str:
        """Create a single GitHub issue.

        Args:
            issue_config: Dictionary containing issue configuration.
            milestone_cache: Shared cache for milestone title -> number lookups.

        Returns:
            Created issue URL, or a placeholder in dry-run mode.

        Raises:
            PlatformError: If issue creation fails.
            ValueError: If issue_config is invalid.
        """
        if "title" not in issue_config:
            raise ValueError("Issue must have a 'title' field")

        self._validate_issue_description(issue_config)

        title = str(issue_config["title"])

        required_fields = self.config.get_required_issue_fields()
        logger.debug("[github] Issue '%s': required_fields=%s", title, required_fields)
        apply_required_issue_fields(issue_config, title, required_fields)
        if required_fields:
            logger.debug(
                "[github] Issue '%s': required fields validated: %s", title, required_fields
            )
        yaml_id = issue_config.get("id")
        id_suffix = f" (id: {yaml_id})" if yaml_id else ""
        logger.info("[github] Creating issue: %s%s", title, id_suffix)

        # Merge and validate labels before any I/O so a bad config cannot
        # create a persistent side effect (e.g. an orphaned milestone).
        default_labels = self.config.get_default_labels()
        issue_labels = issue_config.get("labels", [])
        if not isinstance(issue_labels, list):
            issue_labels = []
        all_labels = list(dict.fromkeys(default_labels + issue_labels))

        # Validate labels against the allowed list (no-op if not configured).
        validate_labels(all_labels, self.config.get_allowed_labels())

        # Validate required OR groups — exactly one label per group must be present
        validate_required_label_groups(all_labels, self.config.get_required_label_groups())

        milestone_title: Optional[str] = None
        raw_milestone = issue_config.get("milestone")
        if raw_milestone:
            milestone_title = str(raw_milestone)
            if not self.dry_run:
                # Ensure the milestone exists; creates it if absent.
                self._resolve_milestone_number(milestone_title, milestone_cache)

        merged_issue_config = {**issue_config, "labels": all_labels}
        cmd = self._build_issue_cmd(merged_issue_config, milestone_title)

        if self.dry_run:
            print(f"[DRY RUN] Would create issue: {title}")
            if milestone_title:
                print(f"  milestone: {milestone_title}")
            if all_labels:
                print(f"  labels: {', '.join(str(lbl) for lbl in all_labels)}")
            issue_url = "DRY_RUN_ISSUE_URL"
        else:
            output = self._run_gh_command(cmd)
            # gh issue create returns the issue URL on stdout
            issue_url = output.split()[0] if output else "unknown"
            logger.info("[github] Created issue: %s", issue_url)

        self.created_issues.append({"title": title, "url": issue_url})
        return issue_url

    def process_yaml_file(self, yaml_path: Path) -> None:
        """Process a YAML file and create GitHub issues.

        Args:
            yaml_path: Path to the YAML configuration file.

        Raises:
            PlatformError: If creation fails.
            ValueError: If YAML structure is invalid or a dependency cycle exists.
        """
        logger.info("[github] Loading configuration from: %s", yaml_path)
        with open(yaml_path, "r", encoding="utf-8") as yaml_file:
            config = yaml.safe_load(yaml_file)

        if not config:
            raise ValueError("YAML file is empty")

        if "issues" not in config or not config["issues"]:
            raise ValueError("YAML must contain a non-empty 'issues' section")

        issues: List[Dict[str, Any]] = config["issues"]

        # Validate all required sections before any API calls
        for issue_config in issues:
            self._validate_issue_description(issue_config)

        # Topological sort raises ValueError on cycles before any API calls
        ordered_issues = self._topological_sort(issues)

        milestone_cache: Dict[str, Optional[int]] = {}
        for issue_config in ordered_issues:
            try:
                self.create_issue(issue_config, milestone_cache)
            except (PlatformError, ValueError) as err:
                logger.error("[github] Failed to create issue: %s", err)
                raise

        self.print_summary()

    def print_summary(self) -> None:
        """Print a summary of created issues."""
        if not self.created_issues:
            logger.info("[github] No issues were created")
            return

        print("\n" + "=" * 60)
        print("SUMMARY: Created GitHub Issues")
        print("=" * 60)
        for issue in self.created_issues:
            print(f"  - {issue['title']}")
            print(f"    URL: {issue['url']}")
        print("=" * 60)
        print(
            f"Total: {len(self.created_issues)} issue{'s' if len(self.created_issues) != 1 else ''} created"
        )
        print()
