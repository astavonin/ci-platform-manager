"""Pipeline debugging handler for GitLab CI/CD."""

import json
import logging
import subprocess
import urllib.parse
from typing import Any, Dict, List, Optional

from ..config import Config
from ..exceptions import PlatformError
from ..utils.git_helpers import get_current_repo_path
from ..utils.glab_runner import run_glab_command

logger = logging.getLogger(__name__)


class PipelineHandler:
    """Handler for pipeline debugging operations."""

    def __init__(self, config: Config) -> None:
        """Initialize the pipeline handler.

        Args:
            config: Configuration object with GitLab settings.
        """
        self.config = config
        self._project_id_cache: Optional[str] = None

    def _run_glab_command(self, cmd: List[str]) -> str:
        """Delegate to shared glab runner."""
        return run_glab_command(cmd)

    def get_current_branch(self) -> str:
        """Get current git branch name.

        Returns:
            Current branch name.

        Raises:
            PlatformError: If git command fails.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            branch = result.stdout.strip()
            logger.debug("Current branch: %s", branch)
            return branch
        except subprocess.CalledProcessError as err:
            error_msg = f"Failed to get current branch: {err.stderr}"
            logger.error(error_msg)
            raise PlatformError(error_msg) from err

    def get_project_id(self) -> str:
        """Get project ID (cached).

        Tries git remote first, falls back to config.

        Returns:
            Project ID (e.g., 'group/project').

        Raises:
            PlatformError: If project cannot be determined.
        """
        if self._project_id_cache:
            return self._project_id_cache

        # Try git remote first
        project_id = self.get_project_from_remote()
        if not project_id:
            # Fall back to config
            project_id = self.config.get_default_group()
            if not project_id:
                raise PlatformError(
                    "Cannot determine project. Either run from a git repository with GitLab remote, "
                    "or set 'gitlab.default_group' in config."
                )
            logger.debug("Using project from config: %s", project_id)
        else:
            logger.debug("Using project from git remote: %s", project_id)

        # Cache the result
        self._project_id_cache = project_id
        return project_id

    def get_project_from_remote(self) -> Optional[str]:
        """Detect GitLab project path from git remote URL.

        Returns:
            Project path (e.g., 'group/project') or None if not detectable.
        """
        project_path = get_current_repo_path()
        if project_path:
            logger.debug("Detected project: %s", project_path)
        else:
            logger.debug("Remote URL does not appear to be GitLab")
        return project_path

    def get_current_pipeline(self, branch: str) -> Dict[str, Any]:
        """Find the latest pipeline for a branch.

        Workflow:
        1. Find MR associated with branch
        2. Get latest pipeline from MR
        3. Return pipeline info

        Args:
            branch: Branch name.

        Returns:
            dict with pipeline info (id, status, web_url, etc.)

        Raises:
            PlatformError: If no pipeline found or API error.
        """
        logger.debug("Finding pipeline for branch: %s", branch)

        # Get project ID (cached, auto-detected from git or config)
        project_id = self.get_project_id()
        encoded_project = urllib.parse.quote(project_id, safe="")
        encoded_branch = urllib.parse.quote(branch, safe="")

        # Find MR for this branch
        api_endpoint = f"projects/{encoded_project}/merge_requests?source_branch={encoded_branch}"

        try:
            output = self._run_glab_command(["api", api_endpoint])
            mrs = json.loads(output) if output else []

            if not mrs:
                raise PlatformError(
                    f"No merge request found for branch '{branch}'. "
                    f"Create an MR first or check branch name."
                )

            # Get the first (most recent) MR
            mr = mrs[0]
            mr_iid = mr.get("iid")
            logger.debug("Found MR !%s for branch %s", mr_iid, branch)

            # Get pipeline from MR (preferred method)
            pipeline = mr.get("head_pipeline")
            if pipeline:
                logger.info(
                    "Found pipeline #%s with status: %s",
                    pipeline.get("id"),
                    pipeline.get("status"),
                )
                return pipeline  # type: ignore[no-any-return]

            # MR has no pipeline - try to find pipelines directly for this branch
            logger.debug(
                "MR !%s has no pipeline, searching project pipelines for branch %s", mr_iid, branch
            )
            pipelines_endpoint = (
                f"projects/{encoded_project}/pipelines?ref={encoded_branch}&order_by=id&sort=desc"
            )

            try:
                pipelines_output = self._run_glab_command(["api", pipelines_endpoint])
                pipelines = json.loads(pipelines_output) if pipelines_output else []

                if pipelines:
                    # Return the most recent pipeline
                    pipeline = pipelines[0]
                    logger.info(
                        "Found pipeline #%s with status: %s (not attached to MR)",
                        pipeline.get("id"),
                        pipeline.get("status"),
                    )
                    return pipeline  # type: ignore[no-any-return]
            except (PlatformError, json.JSONDecodeError) as err:
                logger.debug("Failed to fetch project pipelines: %s", err)

            # No pipeline found anywhere
            raise PlatformError(
                f"No pipeline found for branch '{branch}'. "
                f"MR !{mr_iid} exists but has no pipeline. "
                f"Push commits to trigger CI or run pipeline manually in GitLab."
            )

        except json.JSONDecodeError as err:
            error_msg = f"Failed to parse API response: {err}"
            logger.error(error_msg)
            raise PlatformError(error_msg) from err

    def get_failed_jobs(self, pipeline_id: int) -> List[Dict[str, Any]]:
        """Get all failed jobs from a pipeline.

        Args:
            pipeline_id: Pipeline ID.

        Returns:
            list of dicts with job info (id, name, stage, status, etc.)

        Raises:
            PlatformError: If API error.
        """
        logger.debug("Fetching failed jobs for pipeline #%s", pipeline_id)

        # Get project ID (cached, auto-detected from git or config)
        project_id = self.get_project_id()
        encoded_project = urllib.parse.quote(project_id, safe="")
        api_endpoint = f"projects/{encoded_project}/pipelines/{pipeline_id}/jobs"

        try:
            output = self._run_glab_command(["api", api_endpoint])
            jobs = json.loads(output) if output else []

            # Filter for failed jobs
            failed_jobs = [job for job in jobs if job.get("status") == "failed"]

            logger.info("Found %d failed jobs in pipeline #%s", len(failed_jobs), pipeline_id)
            return failed_jobs  # type: ignore[no-any-return]

        except json.JSONDecodeError as err:
            error_msg = f"Failed to parse API response: {err}"
            logger.error(error_msg)
            raise PlatformError(error_msg) from err

    def get_job_logs(self, job_id: int) -> str:
        """Fetch complete logs for a job.

        Args:
            job_id: Job ID.

        Returns:
            str with complete job logs

        Raises:
            PlatformError: If API error or job not found.
        """
        logger.debug("Fetching logs for job #%s", job_id)

        # Get project ID (cached, auto-detected from git or config)
        project_id = self.get_project_id()
        encoded_project = urllib.parse.quote(project_id, safe="")
        api_endpoint = f"projects/{encoded_project}/jobs/{job_id}/trace"

        try:
            output = self._run_glab_command(["api", api_endpoint])
            logger.info("Fetched %d bytes of logs for job #%s", len(output), job_id)
            return output
        except PlatformError as err:
            # Re-raise with more context
            raise PlatformError(f"Failed to fetch logs for job #{job_id}: {err}") from err

    def get_job_info(self, job_id: int) -> Dict[str, Any]:
        """Fetch metadata for a single job.

        Args:
            job_id: Job ID.

        Returns:
            dict with job metadata (name, stage, status, duration, etc.)

        Raises:
            PlatformError: If API error or job not found.
        """
        logger.debug("Fetching info for job #%s", job_id)

        project_id = self.get_project_id()
        encoded_project = urllib.parse.quote(project_id, safe="")
        api_endpoint = f"projects/{encoded_project}/jobs/{job_id}"

        output = self._run_glab_command(["api", api_endpoint])
        try:
            return json.loads(output)  # type: ignore[no-any-return]
        except json.JSONDecodeError as err:
            raise PlatformError(f"Failed to parse job info response: {err}") from err
