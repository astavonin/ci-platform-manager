"""Tests for projctl.handlers.artifacts_handler module."""

import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from projctl.config import Config
from projctl.exceptions import PlatformError
from projctl.handlers.artifacts_handler import ArtifactsHandler

# Pins PipelineHandler.get_project_id() so tests don't depend on this
# checkout's own ambient git remote (see get_project_from_remote()).
_PIN_PROJECT_ID = patch(
    "projctl.handlers.artifacts_handler.PipelineHandler.get_project_id",
    return_value="test/group",
)


@pytest.fixture
def handler(new_config_path: Path) -> ArtifactsHandler:
    """ArtifactsHandler built from the standard test config."""
    return ArtifactsHandler(Config(new_config_path))


class TestFetchArtifactFile:
    """Test downloading a single artifact file."""

    @_PIN_PROJECT_ID
    @patch("projctl.handlers.artifacts_handler.run_glab_command_binary")
    def test_fetch_artifact_file_success(
        self, mock_run_binary: Mock, _mock_project_id: Mock, handler: ArtifactsHandler
    ) -> None:
        """Fetches raw bytes and hits the expected artifacts endpoint."""
        payload = b"log line one\nlog line two\n"
        mock_run_binary.return_value = payload

        result = handler.fetch_artifact_file(789, ".build-789/server.stdout")

        assert result == payload
        mock_run_binary.assert_called_once()
        call_args = mock_run_binary.call_args[0][0]
        assert "api" in call_args
        endpoint = call_args[1]
        assert "jobs/789/artifacts/" in endpoint
        # Slashes stay literal (safe="/") since the API's *artifact_path is a
        # wildcard path segment, not a single opaque, percent-encoded token.
        assert endpoint.endswith(".build-789/server.stdout")

    @_PIN_PROJECT_ID
    @patch("projctl.handlers.artifacts_handler.run_glab_command_binary")
    def test_fetch_artifact_file_error_propagates(
        self, mock_run_binary: Mock, _mock_project_id: Mock, handler: ArtifactsHandler
    ) -> None:
        """A failed fetch surfaces as PlatformError with the underlying detail intact."""
        mock_run_binary.side_effect = PlatformError("Command failed: 404 Not Found")

        with pytest.raises(PlatformError, match="404 Not Found"):
            handler.fetch_artifact_file(789, "missing.txt")


class TestDownloadArchive:
    """Test downloading the full artifacts archive."""

    @_PIN_PROJECT_ID
    @patch("projctl.handlers.artifacts_handler.stream_glab_command_to_file")
    def test_download_archive_streams_to_expected_path(
        self, mock_stream: Mock, _mock_project_id: Mock, handler: ArtifactsHandler, tmp_path: Path
    ) -> None:
        """Streams to <dest_dir>/artifacts.zip via the archive endpoint."""
        dest_dir = tmp_path / "out"

        result = handler.download_archive(789, dest_dir)

        assert result == dest_dir / "artifacts.zip"
        assert dest_dir.is_dir()
        mock_stream.assert_called_once()
        cmd_arg, dest_arg = mock_stream.call_args[0]
        assert "jobs/789/artifacts" in cmd_arg[1]
        assert dest_arg == dest_dir / "artifacts.zip"


class TestExtractArchive:
    """Test extracting a downloaded artifacts archive."""

    def test_extract_archive_writes_members(self, tmp_path: Path) -> None:
        """A normal archive extracts its members into dest_dir."""
        archive_path = tmp_path / "artifacts.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr(".build-789/server.stdout", "server log contents")
        dest_dir = tmp_path / "dest"

        names = ArtifactsHandler.extract_archive(archive_path, dest_dir)

        assert ".build-789/server.stdout" in names
        extracted = dest_dir / ".build-789" / "server.stdout"
        assert extracted.read_text() == "server log contents"

    def test_extract_archive_rejects_zip_slip(self, tmp_path: Path) -> None:
        """A '../escape.txt' member must not be written outside dest_dir.

        Regression guard for the extraction path: it must keep using
        ZipFile.extractall() (which sanitizes member names) rather than a
        hand-rolled read+write loop that reintroduces path traversal.
        """
        archive_path = tmp_path / "artifacts.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("../escape.txt", "should not escape")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()

        ArtifactsHandler.extract_archive(archive_path, dest_dir)

        assert not (tmp_path / "escape.txt").exists()

    def test_extract_archive_bad_zip_raises_platform_error(self, tmp_path: Path) -> None:
        """A non-zip file reports a clear PlatformError instead of a raw traceback."""
        archive_path = tmp_path / "not-a-zip.zip"
        archive_path.write_bytes(b"not a zip file")

        with pytest.raises(PlatformError, match="not a valid zip file"):
            ArtifactsHandler.extract_archive(archive_path, tmp_path / "dest")
