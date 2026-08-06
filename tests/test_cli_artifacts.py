"""Tests for the CLI-level 'artifacts' command (projctl.cli.cmd_artifacts)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from projctl.cli import cmd_artifacts
from projctl.exceptions import PlatformError


def _args(**overrides) -> SimpleNamespace:
    """Build an args namespace with every field cmd_artifacts reads."""
    defaults = {"job_id": 789, "path": None, "dest": None, "config": None}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture(name="handler_cls")
def handler_cls_fixture():
    """Patch Config and ArtifactsHandler in the cli namespace; yield the class mock."""
    with patch("projctl.cli.Config", MagicMock()), patch(
        "projctl.cli.ArtifactsHandler"
    ) as mock_cls:
        yield mock_cls


class TestSingleFileDownload:
    """--path fetches one file out of the job's archive."""

    def test_writes_file_mirroring_archive_layout(self, handler_cls: Mock, tmp_path: Path) -> None:
        """Nested archive paths are recreated under dest rather than flattened.

        Flattening to a basename would silently overwrite when two artifacts
        share a name across directories, so the nested write is the contract —
        not an implementation detail.
        """
        payload = b"server log contents"
        handler_cls.return_value.fetch_artifact_file.return_value = payload

        rc = cmd_artifacts(_args(path=".build-789/server.stdout", dest=str(tmp_path)))

        assert rc == 0
        assert (tmp_path / ".build-789" / "server.stdout").read_bytes() == payload

    def test_passes_job_id_and_path_to_handler(self, handler_cls: Mock, tmp_path: Path) -> None:
        """The job ID and archive-relative path reach fetch_artifact_file unchanged."""
        handler_cls.return_value.fetch_artifact_file.return_value = b""

        cmd_artifacts(_args(job_id=12345, path="build/out.bin", dest=str(tmp_path)))

        handler_cls.return_value.fetch_artifact_file.assert_called_once_with(12345, "build/out.bin")

    def test_writes_non_utf8_payload_unmodified(self, handler_cls: Mock, tmp_path: Path) -> None:
        """A binary payload lands on disk byte-for-byte."""
        payload = bytes(range(256))
        handler_cls.return_value.fetch_artifact_file.return_value = payload

        rc = cmd_artifacts(_args(path="core.dump", dest=str(tmp_path)))

        assert rc == 0
        assert (tmp_path / "core.dump").read_bytes() == payload

    def test_defaults_to_cwd_when_dest_omitted(
        self, handler_cls: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --dest the file is written under the current directory."""
        handler_cls.return_value.fetch_artifact_file.return_value = b"x"
        monkeypatch.chdir(tmp_path)

        rc = cmd_artifacts(_args(path="out.txt"))

        assert rc == 0
        assert (tmp_path / "out.txt").read_bytes() == b"x"

    def test_does_not_download_archive(self, handler_cls: Mock, tmp_path: Path) -> None:
        """The single-file path never pulls the whole archive."""
        handler_cls.return_value.fetch_artifact_file.return_value = b""

        cmd_artifacts(_args(path="out.txt", dest=str(tmp_path)))

        handler_cls.return_value.download_archive.assert_not_called()


class TestFullArchiveDownload:
    """Without --path the whole archive is downloaded and extracted."""

    def test_downloads_then_extracts_into_dest(self, handler_cls: Mock, tmp_path: Path) -> None:
        """download_archive feeds its result straight into extract_archive."""
        archive = tmp_path / "artifacts.zip"
        handler_cls.return_value.download_archive.return_value = archive
        handler_cls.extract_archive.return_value = ["a.txt", "b.txt"]

        rc = cmd_artifacts(_args(dest=str(tmp_path)))

        assert rc == 0
        handler_cls.return_value.download_archive.assert_called_once_with(789, tmp_path)
        handler_cls.extract_archive.assert_called_once_with(archive, tmp_path)

    def test_reports_extracted_member_count(
        self, handler_cls: Mock, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The success line reports how many files were extracted."""
        handler_cls.return_value.download_archive.return_value = tmp_path / "artifacts.zip"
        handler_cls.extract_archive.return_value = ["a.txt", "b.txt", "c.txt"]

        cmd_artifacts(_args(dest=str(tmp_path)))

        assert "3 file(s)" in capsys.readouterr().out


class TestErrorHandling:
    """Handler failures become a non-zero exit code, not a traceback."""

    def test_fetch_failure_returns_one(self, handler_cls: Mock, tmp_path: Path) -> None:
        """A failed single-file fetch exits 1."""
        handler_cls.return_value.fetch_artifact_file.side_effect = PlatformError("404 Not Found")

        assert cmd_artifacts(_args(path="missing.txt", dest=str(tmp_path))) == 1

    def test_download_failure_returns_one(self, handler_cls: Mock, tmp_path: Path) -> None:
        """A failed archive download exits 1."""
        handler_cls.return_value.download_archive.side_effect = PlatformError("job not found")

        assert cmd_artifacts(_args(dest=str(tmp_path))) == 1

    def test_bad_archive_returns_one(self, handler_cls: Mock, tmp_path: Path) -> None:
        """An invalid zip surfaces as exit 1 rather than a BadZipFile traceback."""
        handler_cls.return_value.download_archive.return_value = tmp_path / "artifacts.zip"
        handler_cls.extract_archive.side_effect = PlatformError("is not a valid zip file")

        assert cmd_artifacts(_args(dest=str(tmp_path))) == 1

    def test_missing_config_returns_one(self) -> None:
        """A missing config file exits 1 instead of raising FileNotFoundError."""
        with patch("projctl.cli.Config", side_effect=FileNotFoundError("no config found")):
            assert cmd_artifacts(_args(config="/nonexistent/projctl.yaml")) == 1

    def test_unwritable_dest_returns_one(self, handler_cls: Mock, tmp_path: Path) -> None:
        """An OSError while writing the payload exits 1.

        dest points at an existing *file*, so creating a directory of the same
        name fails — the realistic way this path raises OSError.
        """
        blocker = tmp_path / "blocker"
        blocker.write_text("occupied")
        handler_cls.return_value.fetch_artifact_file.return_value = b"x"

        assert cmd_artifacts(_args(path="nested/out.txt", dest=str(blocker))) == 1
