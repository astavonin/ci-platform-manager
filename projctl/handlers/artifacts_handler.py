"""GitLab CI job-artifact download handler.

PipelineHandler exposes job *logs* only (``glab api .../trace``, a text
response). Job *artifacts* — files a CI job archives, e.g.
``.build-<job>/server.stdout`` — are served as binary payloads (a single
file, or the full zip archive) that must never go through the ``text=True``
subprocess path the rest of projctl's glab callers share, since decoding
non-UTF-8 bytes as text corrupts or raises. This module uses the binary-safe
transport in ``utils.glab_runner`` instead.
"""

import logging
import urllib.parse
import zipfile
from pathlib import Path
from typing import List

from ..config import Config
from ..exceptions import PlatformError
from ..utils.glab_runner import run_glab_command_binary, stream_glab_command_to_file
from .pipeline_handler import PipelineHandler

logger = logging.getLogger(__name__)


class ArtifactsHandler:
    """Handler for downloading GitLab CI job artifacts (files and archives)."""

    def __init__(self, config: Config) -> None:
        """Initialize the artifacts handler.

        Args:
            config: Configuration object with GitLab settings.
        """
        self.config = config
        # Reuse PipelineHandler's project-id resolution (git remote / config
        # fallback) instead of duplicating it here.
        self._pipeline = PipelineHandler(config)

    def _encoded_project(self) -> str:
        """Return the URL-encoded project ID for API endpoint construction."""
        return urllib.parse.quote(self._pipeline.get_project_id(), safe="")

    def fetch_artifact_file(self, job_id: int, artifact_path: str) -> bytes:
        """Download a single file from a job's artifacts archive.

        Args:
            job_id: GitLab CI job ID.
            artifact_path: Path to the file inside the archive.

        Returns:
            Raw file bytes.

        Raises:
            PlatformError: If the API request fails (e.g. job, artifacts, or
                path not found — GitLab's error text is preserved as-is).
        """
        encoded_path = urllib.parse.quote(artifact_path, safe="/")
        endpoint = f"projects/{self._encoded_project()}/jobs/{job_id}/artifacts/{encoded_path}"
        logger.debug("Fetching artifact file for job #%s: %s", job_id, artifact_path)
        return run_glab_command_binary(["api", endpoint])

    def download_archive(self, job_id: int, dest_dir: Path) -> Path:
        """Stream a job's full artifacts archive into dest_dir/artifacts.zip.

        Streams directly to disk rather than buffering in memory, since
        artifact archives can be large.

        Args:
            job_id: GitLab CI job ID.
            dest_dir: Directory to write the archive into (created if missing).

        Returns:
            Path to the written archive file.

        Raises:
            PlatformError: If the API request fails.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / "artifacts.zip"
        endpoint = f"projects/{self._encoded_project()}/jobs/{job_id}/artifacts"
        logger.debug("Downloading artifacts archive for job #%s to %s", job_id, dest_file)
        stream_glab_command_to_file(["api", endpoint], dest_file)
        return dest_file

    @staticmethod
    def extract_archive(archive_path: Path, dest_dir: Path) -> List[str]:
        """Extract a downloaded artifacts archive into dest_dir.

        Uses ZipFile.extractall(), which sanitizes member names (strips '..'
        components and absolute prefixes) before writing anything — this is
        what makes extraction zip-slip safe. Do not replace this with a
        hand-rolled read+write loop over infolist()/namelist(); that
        reintroduces the exact path-traversal bug this relies on stdlib to
        avoid.

        Args:
            archive_path: Path to the downloaded .zip archive.
            dest_dir: Directory to extract into.

        Returns:
            List of member names extracted from the archive.

        Raises:
            PlatformError: If the archive is not a valid zip file.
        """
        try:
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(dest_dir)
                return archive.namelist()
        except zipfile.BadZipFile as err:
            raise PlatformError(f"{archive_path} is not a valid zip file.") from err
