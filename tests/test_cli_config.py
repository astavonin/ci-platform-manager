"""Tests for the `projctl config` subcommand and --help config resolution epilog."""

import io
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from projctl.cli import cmd_config, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(config_path: Path | None = None):
    """Build a minimal args namespace for cmd_config."""
    from types import SimpleNamespace

    return SimpleNamespace(config=str(config_path) if config_path else None)


def _write_config(path: Path) -> Path:
    cfg = path / "projctl.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "platform": "gitlab",
                "gitlab": {
                    "default_group": "test/group",
                    "labels": {"default": ["type::feature"], "allowed": []},
                },
                "common": {
                    "issue_template": {"required_sections": ["Description", "Acceptance Criteria"]}
                },
            }
        )
    )
    return cfg


# ---------------------------------------------------------------------------
# TestCmdConfig
# ---------------------------------------------------------------------------


class TestCmdConfig:
    """Tests for cmd_config()."""

    def test_prints_config_file_path(self, tmp_path: Path, capsys) -> None:
        """Output includes the resolved config file path."""
        cfg = _write_config(tmp_path)
        result = cmd_config(_make_args(cfg))
        assert result == 0
        out = capsys.readouterr().out
        assert str(cfg) in out

    def test_prints_platform(self, tmp_path: Path, capsys) -> None:
        """Output includes the formatted platform line."""
        cfg = _write_config(tmp_path)
        cmd_config(_make_args(cfg))
        out = capsys.readouterr().out
        assert "Platform:    gitlab" in out

    def test_prints_assembled_yaml(self, tmp_path: Path, capsys) -> None:
        """Output includes assembled YAML content (a key from the config)."""
        cfg = _write_config(tmp_path)
        cmd_config(_make_args(cfg))
        out = capsys.readouterr().out
        assert "default_group" in out
        assert "test/group" in out

    def test_missing_config_returns_1(self, tmp_path: Path, capsys) -> None:
        """Returns exit code 1 when the specified config file does not exist."""
        result = cmd_config(_make_args(tmp_path / "nonexistent.yaml"))
        assert result == 1

    def test_missing_config_no_config_found_returns_1(self, tmp_path: Path, monkeypatch) -> None:
        """Returns 1 when no config is found in the auto-search paths."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = cmd_config(_make_args())
        assert result == 1

    def test_malformed_yaml_returns_1(self, tmp_path: Path) -> None:
        """cmd_config returns 1 and does not raise when the config file contains invalid YAML."""
        cfg = tmp_path / "projctl.yaml"
        cfg.write_text("key: [unclosed bracket\n")
        result = cmd_config(_make_args(cfg))
        assert result == 1


# ---------------------------------------------------------------------------
# TestHelpConfigResolution
# ---------------------------------------------------------------------------


class TestHelpConfigResolution:
    """Tests for config resolution info printed in --help epilog."""

    def _capture_help(self, argv=None) -> str:
        """Capture --help output as a string."""
        buf = io.StringIO()
        with pytest.raises(SystemExit):
            with patch("sys.stdout", buf):
                main(argv or ["--help"])
        return buf.getvalue()

    def test_help_contains_search_order_header(self) -> None:
        """--help epilog includes the config search order section."""
        out = self._capture_help()
        assert "Config search order" in out

    def test_help_shows_not_found_when_no_config(self, tmp_path: Path, monkeypatch) -> None:
        """'not found' appears for each of the four candidates when no config exists."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        out = self._capture_help()
        assert out.count("not found") == 4
        assert "none found" in out

    def test_help_shows_active_when_config_exists(self, tmp_path: Path, monkeypatch) -> None:
        """'← active' and the resolved path appear when a config file exists."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg = _write_config(tmp_path)
        out = self._capture_help()
        assert "← active" in out
        assert str(cfg.resolve()) in out

    def test_help_lists_all_four_candidates(self, tmp_path: Path, monkeypatch) -> None:
        """All four auto-search candidates appear in --help."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        out = self._capture_help()
        assert "glab_config.yaml" in out
        assert "projctl.yaml" in out
        assert ".config/projctl/config.yaml" in out
        assert "user config, legacy" in out
