"""Epic and issue creation handler."""

import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import yaml
except ImportError as exc:
    raise ImportError("PyYAML is required. Install with: pip install PyYAML") from exc

from ..config import Config
from ..exceptions import PlatformError
from ..utils.git_helpers import parse_issue_url
from ..utils.glab_runner import run_glab_command
from ..utils.validation import (
    apply_required_issue_fields,
    validate_issue_description,
    validate_labels,
    validate_required_label_groups,
)

logger = logging.getLogger(__name__)


class EpicIssueCreator:
    """Creates GitLab epics and issues using the glab CLI."""

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        """Initialize the creator.

        Args:
            config: Configuration object with defaults.
            dry_run: If True, only print commands without executing them.
        """
        self.config = config
        self.group = config.get_default_group()
        self.dry_run = dry_run
        self.created_issues: List[Dict[str, str]] = []
        self.issue_id_mapping: Dict[str, Dict[str, str]] = {}  # yaml_id -> {'iid': ..., 'url': ...}
        self._group_label_cache: Optional[Dict[str, int]] = None

    def _run_glab_command(self, cmd: List[str]) -> str:
        """Run a glab command, skipping execution in dry-run mode.

        Args:
            cmd: List of command arguments to pass to glab.

        Returns:
            Command output as a string, or empty string in dry-run mode.

        Raises:
            PlatformError: If the command fails.
        """
        if self.dry_run:
            logger.info("[DRY RUN] Would execute: glab %s", " ".join(cmd))
            return ""
        return run_glab_command(cmd)

    def create_epic(self, epic_config: Dict[str, Any]) -> str:
        """Create a new epic or return existing epic ID.

        Args:
            epic_config: Dictionary containing epic configuration with either:
                         - 'id': existing epic ID
                         - 'title' and optionally 'description': for new epic
                         - 'labels': List of label names (merged with defaults)

        Returns:
            Epic ID as a string.

        Raises:
            PlatformError: If epic creation fails.
            ValueError: If epic_config is invalid.
        """
        if "id" in epic_config:
            epic_id = str(epic_config["id"])
            logger.info("Using existing epic ID: %s", epic_id)
            return epic_id

        if "title" not in epic_config:
            raise ValueError("Epic must have either 'id' or 'title' field")

        title = epic_config["title"]
        description = epic_config.get("description", "")

        logger.info("Creating epic: %s", title)
        # Use GitLab API to create epic (glab epic create doesn't exist)
        # Endpoint: POST /groups/:id/epics
        if not self.group:
            raise ValueError(
                "Group path is required to create epics.\n"
                "Please set 'default_group' in your glab_config.yaml file."
            )

        default_labels = self.config.get_default_epic_labels()
        epic_labels = epic_config.get("labels", [])
        all_labels = list(dict.fromkeys(default_labels + epic_labels))
        self._validate_issue_labels(all_labels)
        validate_issue_description(
            description=description,
            required_sections=self.config.get_required_epic_sections(),
            issue_title=title,
            entity_type="Epic",
        )

        required_fields = self.config.get_required_epic_fields()
        if required_fields:
            logger.debug(
                "Epic '%s': required_fields=%s (no field checks currently implemented)",
                title,
                required_fields,
            )

        # URL encode the group path
        encoded_group = urllib.parse.quote(self.group, safe="")

        cmd = ["api", "-X", "POST", f"groups/{encoded_group}/epics", "-f", f"title={title}"]

        if description:
            cmd.extend(["-f", f"description={description}"])

        if all_labels:
            # GitLab API expects labels as comma-separated string
            labels_str = ",".join(all_labels)
            cmd.extend(["-f", f"labels={labels_str}"])

        output = self._run_glab_command(cmd)

        if self.dry_run:
            return "DRY_RUN_EPIC_ID"

        # Parse JSON response to get epic IID (not ID!)
        # The linking API requires the IID, not the global ID
        try:
            response = json.loads(output)
            epic_iid = str(response["iid"])
            epic_id = str(response["id"])
            logger.info("Created epic with ID: %s, IID: %s", epic_id, epic_iid)
            return epic_iid  # Return IID for linking
        except (json.JSONDecodeError, KeyError) as err:
            raise PlatformError(
                f"Failed to parse epic creation response: {err}\nOutput: {output}"
            ) from err

    def _extract_epic_id(self, output: str) -> str:
        """Extract epic ID from glab output.

        Args:
            output: Output from glab epic create command.

        Returns:
            Epic ID as a string.

        Raises:
            PlatformError: If epic ID cannot be extracted.
        """
        # Try to extract from URL format first
        if "epics/" in output:
            parts = output.split("epics/")
            if len(parts) > 1:
                epic_id = parts[1].split()[0].strip()
                return epic_id

        # Try to extract from #ID format
        if "#" in output:
            parts = output.split("#")
            if len(parts) > 1:
                epic_id = parts[1].split()[0].strip()
                return epic_id

        raise PlatformError(f"Could not extract epic ID from output: {output}")

    def _validate_issue_description(self, issue_config: Dict[str, Any]) -> None:
        """Validate that issue description contains required sections.

        Args:
            issue_config: Dictionary containing issue configuration.

        Raises:
            ValueError: If required sections are missing from description.
        """
        validate_issue_description(
            description=issue_config.get("description", ""),
            required_sections=self.config.get_required_sections(),
            issue_title=issue_config.get("title", "unknown"),
        )

    def _validate_issue_labels(self, labels: List[str]) -> None:
        """Validate that issue labels are in the allowed list.

        Args:
            labels: List of label names to validate.

        Raises:
            ValueError: If any label is not in the allowed list.
        """
        validate_labels(labels, self.config.get_allowed_labels())

    def _fetch_group_labels(self) -> Dict[str, int]:
        """Fetch all group-level labels and return a name→ID map (cached per instance).

        Group labels are yellow in the GitLab UI; project labels are blue. Passing
        label IDs instead of names ensures GitLab uses the group-scoped label rather
        than auto-creating a new project-scoped one when the name is not found locally.

        Returns:
            Dict mapping lowercase label name to numeric label ID. Empty dict if the
            group is not configured or the API call fails.
        """
        if self._group_label_cache is not None:
            return self._group_label_cache

        cache: Dict[str, int] = {}
        if not self.group:
            self._group_label_cache = cache
            return cache

        encoded_group = urllib.parse.quote(self.group, safe="")
        page = 1
        while True:
            try:
                output = self._run_glab_command(
                    ["api", f"groups/{encoded_group}/labels?per_page=100&page={page}"]
                )
                if not output:
                    break
                labels = json.loads(output)
                if not labels:
                    break
                for label in labels:
                    cache[label["name"].lower()] = label["id"]
                if len(labels) < 100:
                    break
                page += 1
            except (PlatformError, json.JSONDecodeError) as err:
                logger.warning("Failed to fetch group labels (page %s): %s", page, err)
                break

        logger.debug("Fetched %s group labels", len(cache))
        self._group_label_cache = cache
        return cache

    def _apply_labels(self, issue_url: str, issue_iid: str, label_names: List[str]) -> None:
        """Apply labels to a newly created issue.

        Uses the REST API ``labels`` field (comma-separated names) rather than
        ``label_ids[]``. The GitLab REST API resolves label names against group-level
        labels before project-level ones, so group-scoped labels are used without
        creating project-level duplicates — the same behavior as the updater path.

        Passing multiple ``-f labels=name`` arguments is wrong: each overrides the
        previous and only the last label survives. All names must be joined into a
        single comma-separated value.

        Args:
            issue_url: Issue URL used to derive the project path.
            issue_iid: Project-scoped issue IID.
            label_names: List of label names to apply.
        """
        if not label_names:
            return

        project_path, _ = parse_issue_url(issue_url)
        if not project_path:
            logger.warning("Cannot apply labels: unable to parse project path from %s", issue_url)
            return

        encoded_project = urllib.parse.quote(project_path, safe="")
        api_endpoint = f"projects/{encoded_project}/issues/{issue_iid}"
        cmd = ["api", "-X", "PUT", api_endpoint, "-f", f"labels={','.join(label_names)}"]

        try:
            self._run_glab_command(cmd)
            logger.debug("Applied %s label(s) to issue #%s", len(label_names), issue_iid)
        except PlatformError as err:
            logger.warning("Failed to apply labels to issue #%s: %s", issue_iid, err)

    def _build_issue_cmd(self, issue_config: Dict[str, Any]) -> List[str]:
        """Build the glab command list for creating an issue (without labels).

        Labels are intentionally omitted here and applied separately via
        _apply_labels so that group-scoped labels are resolved correctly,
        preventing GitLab from auto-creating project-level duplicates.

        Args:
            issue_config: Issue configuration dictionary.

        Returns:
            Command list ready to pass to _run_glab_command.
        """
        cmd = ["issue", "create", "--title", issue_config["title"]]
        if "description" in issue_config:
            cmd.extend(["--description", issue_config["description"]])
        if "assignee" in issue_config:
            cmd.extend(["--assignee", issue_config["assignee"]])
        if "milestone" in issue_config:
            cmd.extend(["--milestone", issue_config["milestone"]])
        if "due_date" in issue_config:
            # glab uses --due flag for due date
            cmd.extend(["--due", issue_config["due_date"]])
        return cmd

    def create_issue(self, issue_config: Dict[str, Any], epic_id: Optional[str] = None) -> str:
        """Create a GitLab issue.

        Args:
            issue_config: Dictionary containing issue configuration:
                          - id: Optional YAML-local identifier for dependencies
                          - title: Issue title (required)
                          - description: Issue description
                          - labels: List of label names (merged with defaults)
                          - assignee: Assignee username
                          - milestone: Milestone title
                          - due_date: Due date in YYYY-MM-DD format
                          - dependencies: List of YAML IDs this issue depends on
            epic_id: Epic ID to add the issue to (optional).

        Returns:
            Issue ID/URL as a string.

        Raises:
            PlatformError: If issue creation fails.
            ValueError: If issue_config is invalid.
        """
        if "title" not in issue_config:
            raise ValueError("Issue must have a 'title' field")

        title = issue_config["title"]

        required_fields = self.config.get_required_issue_fields()
        logger.debug("Issue '%s': required_fields=%s", issue_config.get("title"), required_fields)
        apply_required_issue_fields(issue_config, title, required_fields)
        if required_fields:
            logger.debug("Issue '%s': required fields validated: %s", title, required_fields)

        self._validate_issue_description(issue_config)

        yaml_id = issue_config.get("id")
        id_suffix = f" (id: {yaml_id})" if yaml_id else ""
        logger.info("Creating issue: %s%s", title, id_suffix)

        all_labels = list(
            dict.fromkeys(self.config.get_default_labels() + issue_config.get("labels", []))
        )
        self._validate_issue_labels(all_labels)
        validate_required_label_groups(all_labels, self.config.get_required_label_groups())

        # Create the issue without labels first; labels are applied separately via
        # label IDs so that group-scoped labels are used (not project-level duplicates).
        cmd = self._build_issue_cmd(issue_config)
        output = self._run_glab_command(cmd)

        if self.dry_run:
            issue_url = "DRY_RUN_ISSUE_URL"
            issue_iid = "DRY_RUN_IID"
            # Track the yaml_id mapping for dependency linking
            if yaml_id:
                self.issue_id_mapping[yaml_id] = {"url": issue_url, "iid": issue_iid}
            # Still show the linking command in dry-run mode
            if epic_id:
                self._link_issue_to_epic(issue_url, epic_id)
            issue_info = {"title": title, "id": issue_url}
            self.created_issues.append(issue_info)
            return issue_url

        issue_url = self._extract_issue_id(output)
        logger.info("Created issue: %s", issue_url)
        issue_iid = self._extract_issue_iid_from_url(issue_url)

        if all_labels:
            self._apply_labels(issue_url, issue_iid, all_labels)

        if "weight" in issue_config:
            self._set_issue_weight(issue_url, issue_iid, issue_config["weight"])

        if yaml_id:
            self.issue_id_mapping[yaml_id] = {"url": issue_url, "iid": issue_iid}
            logger.debug("Mapped YAML ID '%s' to issue #%s", yaml_id, issue_iid)
        if epic_id:
            self._link_issue_to_epic(issue_url, epic_id)

        issue_info = {"title": title, "id": issue_url}
        self.created_issues.append(issue_info)
        return issue_url

    def _extract_issue_id(self, output: str) -> str:
        """Extract issue ID/URL from glab output.

        Args:
            output: Output from glab issue create command.

        Returns:
            Issue ID or URL as a string.

        Raises:
            PlatformError: If issue ID cannot be extracted.
        """
        # glab issue create typically returns the issue URL or #ID
        if output:
            # Return the full output which usually contains the issue reference
            return output.split()[0] if output else "unknown"

        raise PlatformError("Could not extract issue ID from output")

    def _extract_issue_iid_from_url(self, issue_url: str) -> str:
        """Extract issue iid from issue URL.

        Args:
            issue_url: Issue URL (e.g., https://gitlab.com/group/project/-/issues/123).

        Returns:
            Issue iid as a string.

        Raises:
            PlatformError: If iid cannot be extracted.
        """
        # URL format: https://gitlab.../group/project/-/issues/123
        if "/-/issues/" in issue_url:
            parts = issue_url.split("/-/issues/")
            if len(parts) == 2:
                iid = parts[1].split("/")[0].split("?")[0]
                return iid

        # GitLab work_items URL format: https://gitlab.../group/project/-/work_items/123
        if "/-/work_items/" in issue_url:
            parts = issue_url.split("/-/work_items/")
            if len(parts) == 2:
                iid = parts[1].split("/")[0].split("?")[0]
                return iid

        # Fallback: try to extract from #ID format
        if "#" in issue_url:
            return issue_url.split("#")[-1].split()[0]

        # If already a number, return it
        if issue_url.isdigit():
            return issue_url

        raise PlatformError(f"Could not extract iid from issue URL: {issue_url}")

    def _get_group_path(self) -> Optional[str]:
        """Get the group path for epic operations.

        Returns:
            Group path string, or None if not available.
        """
        return self.group

    def _get_global_issue_id(self, issue_id: str) -> Optional[str]:
        """Get the global issue ID from an issue URL or iid.

        The GitLab API for epic-issue linking requires the global issue ID,
        not the project-scoped iid.

        Args:
            issue_id: Issue URL or iid.

        Returns:
            Global issue ID as string, or None if not found.
        """
        # Extract project path and iid from URL using shared utility
        project_path, iid = parse_issue_url(issue_id)
        if project_path is not None and iid is not None:
            encoded_project = urllib.parse.quote(project_path, safe="")
            api_endpoint = f"projects/{encoded_project}/issues/{iid}"

            try:
                output = self._run_glab_command(["api", api_endpoint])
                if output:
                    data = json.loads(output)
                    return str(data.get("id"))
            except (PlatformError, json.JSONDecodeError) as err:
                logger.warning("Failed to get global issue ID: %s", err)
                return None

        return None

    def _link_issue_to_epic(self, issue_id: str, epic_id: str) -> None:
        """Link an issue to an epic using GitLab API.

        Uses the GitLab API endpoint:
        POST /groups/:id/epics/:epic_iid/issues/:issue_id

        Note: The API requires the global issue ID, not the project-scoped iid.

        Args:
            issue_id: Issue URL (preferred) or iid.
            epic_id: Epic ID (iid within the group).

        Raises:
            PlatformError: If linking fails.
        """
        logger.info("Linking issue %s to epic %s", issue_id, epic_id)
        group_path = self._get_group_path()
        if not group_path:
            logger.warning(
                "Cannot link issue to epic: group path not specified.\n"
                "Please set 'default_group' in your glab_config.yaml file."
            )
            return

        # Get the global issue ID (required by the API)
        global_issue_id = self._get_global_issue_id(issue_id)
        if not global_issue_id:
            # Fallback: try using the ID directly (might work if already global)
            if "#" in issue_id:
                global_issue_id = issue_id.split("#")[-1].split()[0]
            elif "/" in issue_id:
                global_issue_id = issue_id.rstrip("/").split("/")[-1]
            else:
                global_issue_id = issue_id
            logger.debug("Using issue ID directly: %s", global_issue_id)
        # URL-encode the group path for the API endpoint
        encoded_group = urllib.parse.quote(group_path, safe="")

        # Build the API endpoint
        api_endpoint = f"groups/{encoded_group}/epics/{epic_id}/issues/{global_issue_id}"

        cmd = ["api", "-X", "POST", api_endpoint]

        try:
            self._run_glab_command(cmd)
            logger.info("Successfully linked issue to epic")
        except PlatformError as err:
            logger.warning("Failed to link issue to epic: %s", err)
            # Don't fail the whole operation if linking fails

    def _set_issue_weight(self, issue_url: str, issue_iid: str, weight: int) -> None:
        """Set the weight of an issue via the GitLab API.

        Args:
            issue_url: Issue URL used to derive the project path.
            issue_iid: Project-scoped issue IID.
            weight: Non-negative integer weight value.

        Raises:
            PlatformError: If the API call fails.
        """
        project_path, iid = parse_issue_url(issue_url)
        if not project_path or not iid:
            logger.warning("Cannot set weight: unable to parse project path from %s", issue_url)
            return

        encoded_project = urllib.parse.quote(project_path, safe="")
        api_endpoint = f"projects/{encoded_project}/issues/{iid}"
        cmd = ["api", "-X", "PUT", api_endpoint, "-f", f"weight={weight}"]

        try:
            self._run_glab_command(cmd)
            logger.info("Set weight=%s on issue #%s", weight, issue_iid)
        except PlatformError as err:
            logger.warning("Failed to set weight on issue #%s: %s", issue_iid, err)

    def _create_issue_dependency_link(
        self, blocking_issue_iid: str, blocked_issue_iid: str, project_id: str
    ) -> None:
        """Create a dependency link between two issues.

        Uses the GitLab API endpoint:
        POST /projects/:id/issues/:issue_iid/links
        with link_type=blocks

        Args:
            blocking_issue_iid: The iid of the issue that blocks.
            blocked_issue_iid: The iid of the issue that is blocked.
            project_id: The project ID (can be namespace/project or numeric ID).

        Raises:
            PlatformError: If linking fails.
        """
        logger.info(
            "Creating dependency: issue #%s blocks #%s", blocking_issue_iid, blocked_issue_iid
        )

        # URL-encode the project path
        encoded_project = urllib.parse.quote(project_id, safe="")

        # Build the API endpoint for the blocking issue
        api_endpoint = f"projects/{encoded_project}/issues/{blocking_issue_iid}/links"

        # The link_type=blocks means: blocking_issue blocks blocked_issue
        cmd = [
            "api",
            "-X",
            "POST",
            api_endpoint,
            "-f",
            f"target_project_id={project_id}",
            "-f",
            f"target_issue_iid={blocked_issue_iid}",
            "-f",
            "link_type=blocks",
        ]

        try:
            self._run_glab_command(cmd)
            logger.info("Successfully created dependency link")
        except PlatformError as err:
            logger.warning("Failed to create dependency link: %s", err)
            # Don't fail the whole operation if linking fails

    def _parse_dependency_reference(self, dep_ref: Union[str, int]) -> Tuple[str, bool]:
        """Parse dependency reference into (identifier, is_external).

        Args:
            dep_ref: Dependency reference (YAML ID, IID, or #IID)

        Returns:
            Tuple of (identifier, is_external_flag)
            - For YAML ID: ("yaml-id", False)
            - For GitLab IID: ("13", True)

        Raises:
            ValueError: If external IID is not a valid positive integer

        Examples:
            "design-task" -> ("design-task", False)
            13 -> ("13", True)
            "#42" -> ("42", True)
            "123" -> ("123", False)  # Treated as YAML-local ID
        """
        # Integer: external IID
        if isinstance(dep_ref, int):
            if dep_ref <= 0:
                raise ValueError(f"External IID must be positive integer, got: {dep_ref}")
            identifier = str(dep_ref)
            logger.debug("Parsed dependency reference %s as external IID #%s", dep_ref, identifier)
            return (identifier, True)

        # String starting with #: external IID
        if isinstance(dep_ref, str) and dep_ref.startswith("#"):
            identifier = dep_ref[1:]
            if not identifier or not identifier.isdigit() or int(identifier) <= 0:
                raise ValueError(f"Invalid external IID format: {dep_ref}")
            logger.debug(
                "Parsed dependency reference '%s' as external IID #%s", dep_ref, identifier
            )
            return (identifier, True)

        # All other strings: YAML-local ID (including numeric strings like "123")
        logger.debug("Parsed dependency reference '%s' as YAML-local ID", dep_ref)
        return (dep_ref, False)

    def _validate_external_issue_exists(self, issue_iid: str, project_id: str) -> bool:
        """Verify that external GitLab issue exists.

        Args:
            issue_iid: GitLab issue IID (validated positive integer as string)
            project_id: Project path (namespace/project)

        Returns:
            True if issue exists, False otherwise
        """
        encoded_project = urllib.parse.quote(project_id, safe="")
        api_endpoint = f"projects/{encoded_project}/issues/{issue_iid}"

        try:
            self._run_glab_command(["api", api_endpoint])
            logger.debug("Validated external issue #%s exists", issue_iid)
            return True
        except PlatformError:
            logger.warning("External issue #%s not found in project %s", issue_iid, project_id)
            return False

    def _validate_external_dependencies(
        self, issues: List[Dict[str, Any]], project_id: str
    ) -> List[str]:
        """Validate all external dependencies upfront before creating issues.

        Args:
            issues: List of issue configurations from YAML
            project_id: Project path (namespace/project)

        Returns:
            List of validation error messages (empty if all valid)
        """
        validation_errors = []
        checked_iids = set()  # Cache to avoid redundant API calls (both success and failure)

        for issue_config in issues:
            yaml_id = issue_config.get("id")
            dependencies = issue_config.get("dependencies", [])

            for dep_ref in dependencies:
                try:
                    identifier, is_external = self._parse_dependency_reference(dep_ref)

                    if is_external and identifier not in checked_iids:
                        # Validate this external IID
                        if not self._validate_external_issue_exists(identifier, project_id):
                            error_msg = f"External issue #{identifier} (dependency of '{yaml_id}') not found"
                            validation_errors.append(error_msg)
                        checked_iids.add(identifier)  # Cache regardless of outcome

                except ValueError as err:
                    error_msg = f"Invalid dependency reference in '{yaml_id}': {err}"
                    validation_errors.append(error_msg)

        return validation_errors

    def process_yaml_file(  # pylint: disable=too-many-branches
        # milestone + epic + issues support adds multiple conditional paths that
        # are inherently flat and readable; extracting helpers would obscure flow.
        self,
        yaml_path: Path,
    ) -> None:
        """Process a YAML file and create milestone, epic, and/or issues.

        Supported combinations:
        - milestone only
        - milestone + epic + issues
        - epic + issues (existing behaviour, unchanged)

        Args:
            yaml_path: Path to the YAML configuration file.

        Raises:
            PlatformError: If creation fails.
            ValueError: If YAML structure is invalid or contains none of the
                        supported top-level keys.
        """
        logger.info("Loading configuration from: %s", yaml_path)
        with open(yaml_path, "r", encoding="utf-8") as yaml_file:
            config = yaml.safe_load(yaml_file)

        if not config:
            raise ValueError("YAML file is empty")

        has_milestone = "milestone" in config
        has_epic = "epic" in config
        has_issues = "issues" in config and config["issues"]

        if not has_milestone and not has_epic and not has_issues:
            raise ValueError(
                "YAML must contain at least one of: 'milestone', 'epic', or 'issues' sections"
            )

        if has_milestone:
            milestone_config = config["milestone"]
            if "title" not in milestone_config:
                raise ValueError("Milestone section must contain a 'title' field")
            result = self.create_milestone(
                title=milestone_config["title"],
                description=milestone_config.get("description", ""),
                due_date=milestone_config.get("due_date", ""),
            )
            print(f"Created milestone %{result['iid']}: {milestone_config['title']}")
            print(f"URL: {result['web_url']}")

        if has_epic and has_issues:
            # Create or get epic
            epic_config = config["epic"]
            epic_id = self.create_epic(epic_config)

            issues = config["issues"]

            # Extract project ID for validation (need a placeholder for dry-run)
            if not self.dry_run:
                # Validate external dependencies upfront before creating issues
                project_id = self._get_project_id_for_validation()
                if project_id:
                    validation_errors = self._validate_external_dependencies(issues, project_id)
                    if validation_errors:
                        logger.error("External dependency validation failed:")
                        for error in validation_errors:
                            logger.error("  - %s", error)
                        raise PlatformError("Cannot proceed with invalid external dependencies")

            # Create issues
            logger.info("Creating %s issues...", len(issues))
            for idx, issue_config in enumerate(issues, 1):
                try:
                    self.create_issue(issue_config, epic_id)
                except (PlatformError, ValueError) as err:
                    logger.error("Failed to create issue %s: %s", idx, err)
                    raise

            # Create dependency links after all issues are created
            if self.issue_id_mapping:
                self._create_dependency_links(issues)
        elif has_epic and not has_issues:
            # Epic-only creation (no issues needed yet)
            epic_config = config["epic"]
            if self.dry_run:
                print(f"[dry-run] Would create epic: {epic_config.get('title', '')}")
            else:
                epic_iid = self.create_epic(epic_config)
                print(f"Created epic &{epic_iid}: {epic_config.get('title', '')}")
        elif has_issues and not has_epic:
            raise ValueError("YAML contains 'issues' but no 'epic' section")

        logger.info("Successfully completed all operations")

    def _create_dependency_links(self, issues: List[Dict[str, Any]]) -> None:
        """Create dependency links with support for external GitLab issues.

        Args:
            issues: List of issue configurations from YAML.
        """
        logger.info("Creating dependency links...")

        project_id = self._extract_project_id_from_url()
        if not project_id:
            logger.warning("Cannot create dependency links: unable to determine project ID")
            return

        dependency_count = 0
        link_errors = []

        for issue_config in issues:
            yaml_id = issue_config.get("id")
            dependencies = issue_config.get("dependencies", [])

            if not yaml_id or not dependencies:
                continue

            blocked_issue = self.issue_id_mapping.get(yaml_id)
            if not blocked_issue:
                logger.warning("Issue with YAML ID '%s' not found in mapping", yaml_id)
                continue

            blocked_iid = blocked_issue["iid"]

            # Process each dependency
            for dep_ref in dependencies:
                try:
                    # Parse dependency reference
                    identifier, is_external = self._parse_dependency_reference(dep_ref)

                    if is_external:
                        # External GitLab issue reference (already validated upfront)
                        blocking_iid = identifier
                    else:
                        # YAML-local ID (existing behavior)
                        blocking_issue = self.issue_id_mapping.get(identifier)
                        if not blocking_issue:
                            logger.warning(
                                "Dependency '%s' for issue '%s' not found in mapping",
                                identifier,
                                yaml_id,
                            )
                            continue

                        blocking_iid = blocking_issue["iid"]

                    # Create the dependency link
                    self._create_issue_dependency_link(blocking_iid, blocked_iid, project_id)
                    dependency_count += 1
                    logger.info("Linked issue #%s blocks #%s", blocking_iid, blocked_iid)

                except ValueError as err:
                    error_msg = f"Invalid dependency in '{yaml_id}': {err}"
                    link_errors.append(error_msg)
                    logger.error(error_msg)
                except PlatformError as err:
                    error_msg = f"Failed to create dependency link for '{yaml_id}': {err}"
                    link_errors.append(error_msg)
                    logger.warning(error_msg)

        logger.info("Created %s dependency links", dependency_count)

        if link_errors:
            logger.warning("Dependency creation encountered %s errors:", len(link_errors))
            for error in link_errors:
                logger.warning("  - %s", error)

    def _get_project_id_for_validation(self) -> Optional[str]:
        """Get project ID for external dependency validation.

        This attempts to determine the current project path before any issues are created.
        Uses git remote URL to extract the project namespace/name.

        Returns:
            Project path (namespace/project) or None if unable to determine.
        """
        # Import here to avoid circular dependency with git_helpers module
        from ..utils.git_helpers import get_current_repo_path  # pylint: disable=import-outside-toplevel  # fmt: skip

        project_id = get_current_repo_path()
        if project_id:
            logger.debug("Extracted project ID for validation: %s", project_id)
        else:
            logger.debug("Could not determine project ID from git remote")

        return project_id

    def _extract_project_id_from_url(self) -> Optional[str]:
        """Extract project ID from the first created issue URL.

        Returns:
            Project path (namespace/project) or None if unable to extract.
        """
        if not self.created_issues:
            return None

        first_issue_url = self.created_issues[0]["id"]

        # Handle dry-run mode
        if first_issue_url == "DRY_RUN_ISSUE_URL":
            # Use the group path if available as project path for dry-run
            if self.group:
                # Use generic project name for dry-run demonstration
                return f"{self.group}/project"
            return "DRY_RUN_PROJECT_ID"

        # URL format: https://gitlab.com/group/subgroup/project/-/issues/123
        # or:         https://gitlab.com/group/subgroup/project/-/work_items/123
        for separator in ("/-/issues/", "/-/work_items/"):
            if separator in first_issue_url:
                parts = first_issue_url.split(separator)
                if len(parts) == 2:
                    project_url = parts[0]
                    # Extract project path from URL
                    if "//" in project_url:
                        project_path = "/".join(project_url.split("//")[1].split("/")[1:])
                        return project_path

        return None

    def create_milestone(self, title: str, description: str = "", due_date: str = "") -> dict:
        """Create a group-level GitLab milestone.

        Args:
            title: Milestone title (required).
            description: Optional description.
            due_date: Optional due date in YYYY-MM-DD format.

        Returns:
            Dict with 'iid', 'id', and 'web_url' keys from the API response.

        Raises:
            PlatformError: If group is not configured or the API call fails.
            ValueError: If title is empty.
        """
        if not title:
            raise ValueError("Milestone title must not be empty")

        if not self.group:
            raise PlatformError(
                "Group path is required to create milestones.\n"
                "Please set 'default_group' in your glab_config.yaml file."
            )

        logger.info("Creating milestone: %s", title)

        encoded_group = urllib.parse.quote(self.group, safe="")
        cmd = ["api", "-X", "POST", f"groups/{encoded_group}/milestones", "-f", f"title={title}"]

        if description:
            cmd.extend(["-f", f"description={description}"])
        if due_date:
            cmd.extend(["-f", f"due_date={due_date}"])

        if self.dry_run:
            logger.info("[DRY RUN] Would execute: glab %s", " ".join(cmd))
            return {"iid": "DRY_RUN", "id": "DRY_RUN", "web_url": "DRY_RUN_URL"}

        output = self._run_glab_command(cmd)

        try:
            response = json.loads(output)
            iid = str(response["iid"])
            gid = str(response["id"])
            web_url = str(response["web_url"])
            logger.info("Created milestone with ID: %s, IID: %s", gid, iid)
            return {"iid": iid, "id": gid, "web_url": web_url}
        except (json.JSONDecodeError, KeyError) as err:
            raise PlatformError(
                f"Failed to parse milestone creation response: {err}\nOutput: {output}"
            ) from err

    def print_summary(self) -> None:
        """Print a summary of created issues."""
        if not self.created_issues:
            logger.info("No issues were created")
            return

        print("\n" + "=" * 60)
        print("SUMMARY: Created Issues")
        print("=" * 60)
        for issue in self.created_issues:
            print(f"  - {issue['title']}")
            print(f"    ID: {issue['id']}")
        print("=" * 60)
        print(f"Total: {len(self.created_issues)} issues created")

        if self.issue_id_mapping:
            print(f"Total: {len(self.issue_id_mapping)} issues with IDs tracked for dependencies")

        print()
