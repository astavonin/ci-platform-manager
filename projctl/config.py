"""Configuration management for CI Platform Manager."""

import logging
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    import yaml
except ImportError as exc:
    raise ImportError("PyYAML is required. Install with: pip install PyYAML") from exc

from .utils.config_migration import transform_issue_template
from .utils.git_helpers import get_current_repo_path

_LEGACY_CONFIG_NAMES: frozenset[str] = frozenset({"glab_config.yaml"})


def config_search_paths() -> list[tuple[Path, str]]:
    """Return the default config search paths with human-readable labels.

    Returns:
        List of (path, label) pairs in priority order (first found wins).
        Does not include the --config explicit-path option.
    """
    return [
        (Path.cwd() / "glab_config.yaml", "./glab_config.yaml (project-local, legacy)"),
        (Path.cwd() / "projctl.yaml", "./projctl.yaml (project-local, preferred)"),
        (
            Path.home() / ".config" / "projctl" / "config.yaml",
            "~/.config/projctl/config.yaml (user config)",
        ),
        (
            Path.home() / ".config" / "glab_config.yaml",
            "~/.config/glab_config.yaml (user config, legacy)",
        ),
    ]


# Known field names per template; unknown entries emit a UserWarning via _warn_unknown_fields.
_KNOWN_ISSUE_FIELDS: frozenset[str] = frozenset({"weight"})
# Empty: no epic field names are supported yet (reserved for future extension).
_KNOWN_EPIC_FIELDS: frozenset[str] = frozenset()
_KNOWN_MR_FIELDS: frozenset[str] = frozenset({"reviewers", "labels"})

logger = logging.getLogger(__name__)


def _warn_unknown_fields(fields: List[str], known: frozenset, template_name: str) -> None:
    """Emit a warning for unrecognised field names in required_fields."""
    unknown = [f for f in fields if f not in known]
    if unknown:
        # stacklevel=3: caller → getter (get_required_*_fields) → _warn_unknown_fields → warnings.warn
        warnings.warn(
            f"Unknown required_fields entries for {template_name}: {unknown!r}. "
            f"Known names: {sorted(known) if known else '(none)'}. "
            "They will be ignored.",
            stacklevel=3,
        )


class ConfigurationError(Exception):
    """Configuration-related errors."""


class Config:
    """Multi-platform configuration manager with legacy format support.

    Loads configuration from YAML file with automatic legacy format detection.
    Configuration can be overridden by command-line arguments.

    Auto-search order is defined by config_search_paths() — first found wins.
    An explicit --config path takes priority and fails immediately if not found.
    """

    def __init__(self, config_path: Optional[Path] = None, platform: Optional[str] = None) -> None:
        """Initialize configuration.

        Args:
            config_path: Path to config file. If None, uses default search order.
            platform: Platform override (gitlab, github). If None, uses config or defaults to gitlab.

        Raises:
            ConfigurationError: If config file doesn't exist or is invalid.
            FileNotFoundError: If config file not found.
        """
        self.config_data: Dict[str, Any] = {}
        self.loaded_config_path: Optional[Path] = None
        self.planning_sync: Dict[str, Any] = {}

        self.config_data = self._load_config_with_legacy_support(config_path)
        self.platform = platform or self.config_data.get("platform", "gitlab")
        self._load_planning_sync()

    def _load_config_with_legacy_support(self, config_path: Optional[Path]) -> Dict[str, Any]:
        """Load config with automatic legacy format detection.

        Auto-search order is defined by config_search_paths() — first found wins.
        An explicit path is used as-is and fails immediately if not found.

        Args:
            config_path: Explicit config path or None for auto-search.

        Returns:
            Dictionary containing configuration data.

        Raises:
            FileNotFoundError: If no config file found in search order.
        """
        if config_path is not None:
            # Explicit config path specified via --config
            if not config_path.exists():
                raise FileNotFoundError(
                    f"Config file not found: {config_path}\n\n"
                    f"To fix this:\n"
                    f"  - Verify the path specified with --config is correct\n"
                    f"  - OR copy the example config to your desired location"
                )
            return self._load_config_file(config_path)

        # Search order for backward compatibility
        candidates = config_search_paths()

        for candidate, _ in candidates:
            if candidate.exists():
                if candidate.name in _LEGACY_CONFIG_NAMES:
                    warnings.warn(
                        f"Using legacy config name '{candidate.name}'. "
                        f"Consider renaming to projctl.yaml",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                return self._load_config_file(candidate)

        # No config found
        raise FileNotFoundError(
            "No config file found. Searched:\n"
            + "\n".join(f"  - {p}" for p, _ in candidates)
        )

    def _load_config_file(self, config_path: Path) -> Dict[str, Any]:
        """Load configuration from a file.

        Args:
            config_path: Path to the configuration file.

        Returns:
            Dictionary containing configuration data.

        Raises:
            yaml.YAMLError: If YAML parsing fails.
            IOError: If file cannot be read.
        """
        with open(config_path, "r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}

        self.loaded_config_path = config_path
        logger.info("Loaded configuration from: %s", config_path)
        # Detect and transform old format
        is_old_format = "labels" in config and "platform" not in config

        if is_old_format:
            warnings.warn(
                "Config uses deprecated format. Run migration script to update.",
                DeprecationWarning,
                stacklevel=2,
            )
            config = self._transform_legacy_config(config)

        return config

    def _transform_legacy_config(self, old_config: Dict[str, Any]) -> Dict[str, Any]:
        """Transform old config format to new format in-memory.

        Args:
            old_config: Configuration in legacy format.

        Returns:
            Configuration in new format.
        """
        old_template = old_config.get("issue_template", {})
        new_template = transform_issue_template(old_template)

        # Extract labels section
        labels_config = old_config.get("labels", {})

        # Build labels dictionary, making default_epic optional
        labels = {
            "default": labels_config.get("default", []),
            "default_epic": labels_config.get("default_epic", []),  # Optional
        }

        # Handle both 'allowed' and 'allowed_labels' for backward compatibility.
        # Use sentinel to distinguish "key absent" from "key present with empty list".
        _sentinel = object()
        allowed = labels_config.get("allowed", labels_config.get("allowed_labels", _sentinel))
        if allowed is not _sentinel:
            labels["allowed"] = allowed

        new_config = {
            "platform": "gitlab",
            "gitlab": {
                "default_group": old_config.get("gitlab", {}).get("default_group", ""),
                "labels": labels,
            },
            "common": {"issue_template": new_template},
        }

        # Preserve planning_sync section if present
        if "planning_sync" in old_config:
            new_config["planning_sync"] = old_config["planning_sync"]

        return new_config

    def get_platform_config(self, platform: str) -> Dict[str, Any]:
        """Get platform-specific configuration.

        Args:
            platform: Platform name (gitlab, github).

        Returns:
            Platform-specific configuration dictionary.
        """
        return self.config_data.get(platform, {})  # type: ignore[no-any-return]

    def get_common_config(self) -> Dict[str, Any]:
        """Get common (platform-agnostic) configuration.

        Returns:
            Common configuration dictionary.
        """
        return self.config_data.get("common", {})  # type: ignore[no-any-return]

    def get_required_sections(self) -> List[str]:
        """Get required issue template sections.

        Returns:
            List of required section names.
        """
        # New format
        new_sections = (
            self.get_common_config().get("issue_template", {}).get("required_sections", [])
        )
        if new_sections:
            return new_sections  # type: ignore[no-any-return]

        # Legacy format support (if config wasn't transformed)
        legacy_sections = self.config_data.get("issue_template", {}).get("sections", [])
        return [s["name"] for s in legacy_sections if s.get("required", False)]

    def get_required_epic_sections(self) -> List[str]:
        """Get required description sections for new epics.

        Reads common.epic_template.required_sections.
        Returns ["Description"] when the key is absent.
        Returns [] when the key is present but empty (no validation).

        Returns:
            List of required section names for epic descriptions.
        """
        epic_template = self.get_common_config().get("epic_template", {})
        if "required_sections" not in epic_template:
            return ["Description"]
        return list(epic_template["required_sections"] or [])

    def get_required_mr_sections(self) -> List[str]:
        """Get required description sections for MRs/PRs.

        Reads common.mr_template.required_sections.
        Returns ["Summary", "Implementation Details", "How It Was Tested"] when the key is absent.
        Returns [] when the key is present but empty (no validation).

        Returns:
            List of required section names for MR/PR descriptions.
        """
        mr_template = self.get_common_config().get("mr_template", {})
        if "required_sections" not in mr_template:
            return ["Summary", "Implementation Details", "How It Was Tested"]
        return list(mr_template["required_sections"] or [])

    def get_required_issue_fields(self) -> List[str]:
        """Return required fields for new issues.

        Platform-aware defaults:
        - GitLab: ["weight"] when key absent — preserves existing behaviour.
        - GitHub: [] when key absent — GitHub issues have no weight concept.

        Returns [] when required_fields is present but empty (explicit opt-out).
        Emits warnings.warn for unrecognised field names.
        """
        issue_template = self.get_common_config().get("issue_template", {})
        if "required_fields" not in issue_template:
            return ["weight"] if self.platform == "gitlab" else []
        fields = issue_template["required_fields"] or []
        if not isinstance(fields, list):
            raise ConfigurationError(
                f"issue_template.required_fields must be a list, " f"got {type(fields).__name__!r}"
            )
        _warn_unknown_fields(fields, _KNOWN_ISSUE_FIELDS, "issue_template")
        return list(fields)

    def get_required_epic_fields(self) -> List[str]:
        """Return required fields for new epics.

        Returns [] when the key is absent or explicitly empty.
        Emits warnings.warn for unrecognised field names.
        """
        epic_template = self.get_common_config().get("epic_template", {})
        if "required_fields" not in epic_template:
            return []
        fields = epic_template["required_fields"] or []
        if not isinstance(fields, list):
            raise ConfigurationError(
                f"epic_template.required_fields must be a list, " f"got {type(fields).__name__!r}"
            )
        _warn_unknown_fields(fields, _KNOWN_EPIC_FIELDS, "epic_template")
        return list(fields)

    def get_required_mr_fields(self) -> List[str]:
        """Return required fields for MR/PR creation.

        Returns [] when the key is absent or explicitly empty.
        Supported names: "reviewers", "labels".
        Emits warnings.warn for unrecognised field names.
        """
        mr_template = self.get_common_config().get("mr_template", {})
        if "required_fields" not in mr_template:
            return []
        fields = mr_template["required_fields"] or []
        if not isinstance(fields, list):
            raise ConfigurationError(
                f"mr_template.required_fields must be a list, " f"got {type(fields).__name__!r}"
            )
        _warn_unknown_fields(fields, _KNOWN_MR_FIELDS, "mr_template")
        return list(fields)

    def get_default_group(self) -> Optional[str]:
        """Get the default GitLab group path for epic operations.

        Returns:
            Default group path or None.
        """
        return self.get_platform_config("gitlab").get("default_group")

    @staticmethod
    def _validate_raw_default_labels(raw: object) -> List[Union[str, List[str]]]:
        """Validate and return the raw default-labels list.

        Each item must be either a plain string (flat label) or a non-empty list
        whose every element is a string (OR group).  Any other shape raises
        ConfigurationError so silent policy bypass is impossible.

        Args:
            raw: The value at ``labels.default`` from the YAML config.

        Returns:
            Validated list of flat labels and OR groups.

        Raises:
            ConfigurationError: If any item has an unexpected type, if an OR group
                is empty, or if any member inside an OR group is not a string.
        """
        if not isinstance(raw, list):
            return []
        validated: List[Union[str, List[str]]] = []
        for idx, item in enumerate(raw):
            if isinstance(item, str):
                validated.append(item)
            elif isinstance(item, list):
                if not item:
                    raise ConfigurationError(
                        f"labels.default[{idx}] is an empty OR group — "
                        "OR groups must contain at least one label"
                    )
                bad = [m for m in item if not isinstance(m, str)]
                if bad:
                    raise ConfigurationError(
                        f"labels.default[{idx}] OR group contains non-string members: "
                        + ", ".join(repr(m) for m in bad)
                    )
                validated.append(item)
            else:
                raise ConfigurationError(
                    f"labels.default[{idx}] has unexpected type {type(item).__name__!r} "
                    "(expected a label string or a list of strings for an OR group)"
                )
        return validated

    def _get_raw_default_labels(self) -> List[Union[str, List[str]]]:
        """Get the raw default labels list, which may contain flat strings and OR groups.

        Returns:
            Raw list where each item is either a label string or a list of strings
            representing an OR group (exactly one must be chosen).

        Raises:
            ConfigurationError: If any entry in ``labels.default`` has an unexpected
                type, contains an empty OR group, or has non-string OR group members.
        """
        platform_labels = (
            self.get_platform_config(self.platform).get("labels", {}).get("default", [])
        )
        if platform_labels:
            return self._validate_raw_default_labels(platform_labels)
        return self._validate_raw_default_labels(
            self.config_data.get("labels", {}).get("default", [])
        )

    def get_default_labels(self) -> List[str]:
        """Get flat default labels that are always applied to every issue.

        OR groups (inner lists) are excluded — use get_required_label_groups() for those.

        Returns:
            List of label names that are unconditionally applied.
        """
        return [item for item in self._get_raw_default_labels() if isinstance(item, str)]

    def get_required_label_groups(self) -> List[List[str]]:
        """Get OR groups from the default labels config.

        Each group requires exactly one of its members to be present on the issue.
        Defined as inner lists in the config, e.g.::

            default:
              - ["type::feature", "type::bug"]   # OR group
              - "development-status::backlog"     # flat label

        Returns:
            List of OR groups; each group is a list of mutually-exclusive label choices.
        """
        return [item for item in self._get_raw_default_labels() if isinstance(item, list)]

    def get_default_epic_labels(self) -> List[str]:
        """Get the default labels to apply to epics.

        Returns:
            List of default epic label names.
        """
        # Try new format first
        platform_labels = (
            self.get_platform_config(self.platform).get("labels", {}).get("default_epic", [])
        )
        if platform_labels:
            return platform_labels  # type: ignore[no-any-return]

        # Fallback to legacy format
        return self.config_data.get("labels", {}).get("default_epic", [])  # type: ignore[no-any-return]

    def get_allowed_labels(self) -> Optional[List[str]]:
        """Get the allowed labels list for validation.

        Returns:
            List of allowed label names, or None if not configured (key absent).
            An explicitly configured empty list is returned as-is.
        """
        platform_labels_config = self.get_platform_config(self.platform).get("labels", {})

        # New format: key present (even if empty list) means validation is configured
        if "allowed" in platform_labels_config:
            return platform_labels_config["allowed"]  # type: ignore[no-any-return]

        # Legacy format: check for allowed_labels key
        legacy_labels = self.config_data.get("labels", {})
        if "allowed_labels" in legacy_labels:
            return legacy_labels["allowed_labels"]  # type: ignore[no-any-return]

        return None

    def get_github_repo(self) -> str:
        """Return 'owner/repo' from config or auto-detected from git remote.

        Raises:
            ConfigurationError: If repo cannot be determined from config or git remote.
        """
        explicit = self.get_platform_config("github").get("repo")
        if explicit:
            return str(explicit)
        detected = get_current_repo_path()
        if not detected:
            raise ConfigurationError(
                "Cannot determine GitHub repository. "
                "Set 'github.repo' in config.yaml or run from inside a git repository "
                "with a GitHub remote."
            )
        return detected

    def _load_planning_sync(self) -> None:
        """Load planning sync configuration from config file.

        Loads the 'planning_sync' section if present, which contains:
        - gdrive_base: Base path to Google Drive mount point
        """
        if "planning_sync" in self.config_data:
            self.planning_sync = self.config_data["planning_sync"]
            logger.debug("Loaded planning_sync config: %s", self.planning_sync)
        else:
            self.planning_sync = {}
            logger.debug("No planning_sync section in config")
