"""Tests for projctl.handlers.pipeline_handler module."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from projctl.config import Config
from projctl.exceptions import PlatformError
from projctl.handlers.pipeline_handler import PipelineHandler


class TestPipelineHandlerInit:
    """Test PipelineHandler initialization."""

    def test_init(self, new_config_path: Path) -> None:
        """Handler initializes correctly."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        assert handler.config == config


class TestGetCurrentBranch:
    """Test getting current git branch."""

    @patch("subprocess.run")
    def test_get_current_branch_success(self, mock_run: Mock, new_config_path: Path) -> None:
        """Get current branch successfully."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run.return_value = Mock(stdout="feature/my-branch\n", stderr="", returncode=0)

        branch = handler.get_current_branch()

        assert branch == "feature/my-branch"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]

    @patch("subprocess.run")
    def test_get_current_branch_failure(self, mock_run: Mock, new_config_path: Path) -> None:
        """Failure to get branch raises PlatformError."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["git"], stderr="Not a git repository"
        )

        with pytest.raises(PlatformError, match="Failed to get current branch"):
            handler.get_current_branch()


class TestGetCurrentPipeline:
    """Test getting current pipeline for a branch."""

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_current_pipeline_success(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """Get pipeline successfully from MR."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        # Mock MR response with pipeline
        mr_data = [
            {
                "iid": 123,
                "source_branch": "feature/test",
                "head_pipeline": {
                    "id": 456,
                    "status": "failed",
                    "web_url": "https://gitlab.example.com/pipeline/456",
                },
            }
        ]

        mock_run_glab.return_value = json.dumps(mr_data)

        pipeline = handler.get_current_pipeline("feature/test")

        assert pipeline["id"] == 456
        assert pipeline["status"] == "failed"
        assert "web_url" in pipeline

        # Verify API call
        mock_run_glab.assert_called_once()
        call_args = mock_run_glab.call_args[0][0]
        assert "api" in call_args
        assert "merge_requests" in " ".join(call_args)

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_current_pipeline_no_mr(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """No MR found for branch raises PlatformError."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        # Mock empty MR response
        mock_run_glab.return_value = "[]"

        with pytest.raises(PlatformError, match="No merge request found"):
            handler.get_current_pipeline("feature/test")

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_current_pipeline_no_pipeline(
        self, mock_run_glab: Mock, new_config_path: Path
    ) -> None:
        """MR without pipeline raises PlatformError."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        # Mock MR response without pipeline, then empty pipelines list
        mr_data = [{"iid": 123, "source_branch": "feature/test", "head_pipeline": None}]

        # First call: MR data (no pipeline), Second call: empty pipelines list
        mock_run_glab.side_effect = [json.dumps(mr_data), json.dumps([])]

        with pytest.raises(PlatformError, match="No pipeline found"):
            handler.get_current_pipeline("feature/test")

    @patch(
        "projctl.handlers.pipeline_handler.PipelineHandler.get_project_from_remote",
        return_value=None,
    )
    def test_get_current_pipeline_no_config(self, mock_remote: Mock, tmp_path: Path) -> None:
        """Missing config and no git remote raises PlatformError."""
        # Create minimal config without default_group
        config_file = tmp_path / "config.yaml"
        config_file.write_text("platform: gitlab\n")

        config = Config(config_file)
        handler = PipelineHandler(config)

        with pytest.raises(PlatformError, match="Cannot determine project"):
            handler.get_current_pipeline("feature/test")


class TestGetFailedJobs:
    """Test getting failed jobs from pipeline."""

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_failed_jobs_multiple(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """Get multiple failed jobs successfully."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        # Mock jobs response
        jobs_data = [
            {
                "id": 1,
                "name": "test:unit",
                "stage": "test",
                "status": "failed",
                "duration": 120.5,
            },
            {
                "id": 2,
                "name": "build",
                "stage": "build",
                "status": "success",
                "duration": 60.0,
            },
            {
                "id": 3,
                "name": "lint",
                "stage": "test",
                "status": "failed",
                "duration": 30.2,
            },
        ]

        mock_run_glab.return_value = json.dumps(jobs_data)

        failed_jobs = handler.get_failed_jobs(456)

        assert len(failed_jobs) == 2
        assert failed_jobs[0]["name"] == "test:unit"
        assert failed_jobs[1]["name"] == "lint"

        # Verify API call
        mock_run_glab.assert_called_once()
        call_args = mock_run_glab.call_args[0][0]
        assert "api" in call_args
        assert "pipelines/456/jobs" in " ".join(call_args)

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_failed_jobs_none(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """No failed jobs returns empty list."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        # Mock jobs response with all successful
        jobs_data = [
            {
                "id": 1,
                "name": "test:unit",
                "stage": "test",
                "status": "success",
                "duration": 120.5,
            },
            {
                "id": 2,
                "name": "build",
                "stage": "build",
                "status": "success",
                "duration": 60.0,
            },
        ]

        mock_run_glab.return_value = json.dumps(jobs_data)

        failed_jobs = handler.get_failed_jobs(456)

        assert len(failed_jobs) == 0

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_failed_jobs_empty_pipeline(
        self, mock_run_glab: Mock, new_config_path: Path
    ) -> None:
        """Empty pipeline returns empty list."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run_glab.return_value = "[]"

        failed_jobs = handler.get_failed_jobs(456)

        assert len(failed_jobs) == 0


class TestGetJobLogs:
    """Test getting job logs."""

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_job_logs_success(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """Get job logs successfully."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        log_content = "Error: Test failed\nExpected 2, got 1\nFailed tests: 5"
        mock_run_glab.return_value = log_content

        logs = handler.get_job_logs(789)

        assert logs == log_content

        # Verify API call
        mock_run_glab.assert_called_once()
        call_args = mock_run_glab.call_args[0][0]
        assert "api" in call_args
        assert "jobs/789/trace" in " ".join(call_args)

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_job_logs_not_found(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """Job not found raises PlatformError."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run_glab.side_effect = PlatformError("Job not found")

        with pytest.raises(PlatformError, match="Failed to fetch logs"):
            handler.get_job_logs(789)

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_job_logs_empty(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """Empty logs returns empty string."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run_glab.return_value = ""

        logs = handler.get_job_logs(789)

        assert logs == ""


class TestRunGlabCommand:
    """Test internal glab command execution."""

    @patch("subprocess.run")
    def test_run_glab_command_success(self, mock_run: Mock, new_config_path: Path) -> None:
        """Successful command execution."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run.return_value = Mock(stdout='{"success": true}', stderr="", returncode=0)

        # NOLINTNEXTLINE(protected-access): Testing private method
        result = handler._run_glab_command(["api", "projects"])

        assert result == '{"success": true}'

    @patch("subprocess.run")
    def test_run_glab_command_failure(self, mock_run: Mock, new_config_path: Path) -> None:
        """Failed command raises PlatformError."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run.side_effect = subprocess.CalledProcessError(1, ["glab"], stderr="API error")

        with pytest.raises(PlatformError, match="Command failed"):
            # NOLINTNEXTLINE(protected-access): Testing private method
            handler._run_glab_command(["api", "projects"])

    @patch("subprocess.run")
    def test_run_glab_command_not_found(self, mock_run: Mock, new_config_path: Path) -> None:
        """Missing glab command raises PlatformError."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run.side_effect = FileNotFoundError("glab not found")

        with pytest.raises(PlatformError, match="glab command not found"):
            # NOLINTNEXTLINE(protected-access): Testing private method
            handler._run_glab_command(["api", "projects"])


class TestGetJobInfo:
    """Test get_job_info method."""

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_job_info_success(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """Fetch job metadata successfully."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        job_data = {
            "id": 789,
            "name": "test:unit",
            "stage": "test",
            "status": "failed",
            "duration": 42.5,
        }
        mock_run_glab.return_value = json.dumps(job_data)

        result = handler.get_job_info(789)

        assert result["id"] == 789
        assert result["name"] == "test:unit"
        call_args = mock_run_glab.call_args[0][0]
        assert "api" in call_args
        assert "jobs/789" in " ".join(call_args)

    @patch("projctl.handlers.pipeline_handler.PipelineHandler._run_glab_command")
    def test_get_job_info_invalid_json(self, mock_run_glab: Mock, new_config_path: Path) -> None:
        """Invalid JSON response raises PlatformError."""
        config = Config(new_config_path)
        handler = PipelineHandler(config)

        mock_run_glab.return_value = "not-json"

        with pytest.raises(PlatformError, match="Failed to parse job info response"):
            handler.get_job_info(789)
