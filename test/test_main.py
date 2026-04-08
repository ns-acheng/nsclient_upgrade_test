"""
Unit tests for main.py cmd_versions output.
All I/O is mocked — no network or tenant needed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import cmd_versions
from util_config import ToolConfig, TenantConfig


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def cfg() -> ToolConfig:
    """Config with tenant credentials filled in."""
    return ToolConfig(
        tenant=TenantConfig(
            hostname="tenant.example.com",
            username="admin@example.com",
            password="secret",
        ),
    )


@pytest.fixture
def versions_data() -> dict:
    """Realistic version data including timestamps."""
    return {
        "latestversion": "135.1.0.2500",
        "goldenversions": ["126.0.0", "120.0.0", "114.0.0"],
        "126.0.0": [
            "126.0.0.2387", "126.0.3.2408", "126.0.5.2428",
            "126.0.9.2460", "126.1.10.2475",
        ],
        "120.0.0": ["120.0.0.2100", "120.0.1.2150"],
        "114.0.0": ["114.0.0.1900"],
        "132.0.0": ["132.0.0.2450"],
        "versions_upload_timestamp": {
            "126.0.0.2387": 1746067121,
            "126.0.3.2408": 1747976953,
            "126.0.5.2428": 1749184656,
            "132.0.0.2450": 1752000000,
        },
    }


# ── cmd_versions Output ─────────────────────────────────────────────


class TestCmdVersions:
    """Tests for cmd_versions display output."""

    @patch("main.WebUIClient")
    def test_shows_latest_golden_build(
        self, mock_webui_cls: MagicMock, cfg: ToolConfig,
        versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """Displays the latest build of the latest golden version."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        # Latest golden is 126.0.0, its highest build is 126.1.10.2475
        assert "126.1.10.2475" in output

    @patch("main.WebUIClient")
    def test_hides_versions_upload_timestamp(
        self, mock_webui_cls: MagicMock, cfg: ToolConfig,
        versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """versions_upload_timestamp key is not printed."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "versions_upload_timestamp" not in output
        assert "1746067121" not in output

    @patch("main.WebUIClient")
    def test_shows_latest_version(
        self, mock_webui_cls: MagicMock, cfg: ToolConfig,
        versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """Displays the latest version from the tenant."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "135.1.0.2500" in output

    @patch("main.WebUIClient")
    def test_shows_golden_versions(
        self, mock_webui_cls: MagicMock, cfg: ToolConfig,
        versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """Displays all golden version names."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "114.0.0" in output
        assert "120.0.0" in output
        assert "126.0.0" in output

    @patch("main.WebUIClient")
    def test_shows_dot_releases(
        self, mock_webui_cls: MagicMock, cfg: ToolConfig,
        versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """Displays dot releases for each major version."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "132.0.0.2450" in output
        assert "120.0.1.2150" in output

    @patch("main.WebUIClient")
    def test_no_golden_versions(
        self, mock_webui_cls: MagicMock, cfg: ToolConfig,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Handles missing golden versions gracefully."""
        mock_webui_cls.return_value.get_release_versions.return_value = {
            "latestversion": "95.0.0.100",
            "goldenversions": [],
            "95.0.0": ["95.0.0.100"],
        }

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "N/A" in output
