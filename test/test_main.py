"""
Unit tests for main.py — cmd_versions output and connect_with_retry.
All I/O is mocked — no network or tenant needed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import cmd_versions, cmd_status, cmd_upgrade, connect_with_retry
from util_config import ToolConfig, TenantConfig
from util_webui import WebUIClient


# ── Fixtures ─────────────────────────────────────────────────────────


AUTH_ERROR = Exception("Tenant authentication failed, invalid username or password")


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

    @patch("main.connect_with_retry", return_value=True)
    @patch("main.WebUIClient")
    def test_shows_latest_golden_build(
        self, mock_webui_cls: MagicMock, mock_retry: MagicMock,
        cfg: ToolConfig, versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """Displays the latest build of the latest golden version."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "126.1.10.2475" in output

    @patch("main.connect_with_retry", return_value=True)
    @patch("main.WebUIClient")
    def test_hides_versions_upload_timestamp(
        self, mock_webui_cls: MagicMock, mock_retry: MagicMock,
        cfg: ToolConfig, versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """versions_upload_timestamp key is not printed."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "versions_upload_timestamp" not in output
        assert "1746067121" not in output

    @patch("main.connect_with_retry", return_value=True)
    @patch("main.WebUIClient")
    def test_shows_latest_version(
        self, mock_webui_cls: MagicMock, mock_retry: MagicMock,
        cfg: ToolConfig, versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """Displays the latest version from the tenant."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "135.1.0.2500" in output

    @patch("main.connect_with_retry", return_value=True)
    @patch("main.WebUIClient")
    def test_shows_golden_versions(
        self, mock_webui_cls: MagicMock, mock_retry: MagicMock,
        cfg: ToolConfig, versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """Displays all golden version names."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "114.0.0" in output
        assert "120.0.0" in output
        assert "126.0.0" in output

    @patch("main.connect_with_retry", return_value=True)
    @patch("main.WebUIClient")
    def test_shows_dot_releases(
        self, mock_webui_cls: MagicMock, mock_retry: MagicMock,
        cfg: ToolConfig, versions_data: dict, capsys: pytest.CaptureFixture,
    ) -> None:
        """Displays dot releases for each major version."""
        mock_webui_cls.return_value.get_release_versions.return_value = versions_data

        cmd_versions(cfg)

        output = capsys.readouterr().out
        assert "132.0.0.2450" in output
        assert "120.0.1.2150" in output

    @patch("main.connect_with_retry", return_value=True)
    @patch("main.WebUIClient")
    def test_no_golden_versions(
        self, mock_webui_cls: MagicMock, mock_retry: MagicMock,
        cfg: ToolConfig, capsys: pytest.CaptureFixture,
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

    @patch("main.connect_with_retry", return_value=False)
    @patch("main.WebUIClient")
    def test_returns_1_on_connect_failure(
        self, mock_webui_cls: MagicMock, mock_retry: MagicMock,
        cfg: ToolConfig,
    ) -> None:
        """Returns exit code 1 when connection fails."""
        assert cmd_versions(cfg) == 1


# ── connect_with_retry ───────────────────────────────────────────────


class TestConnectWithRetry:
    """Tests for connect_with_retry login retry logic."""

    @patch("main.save_password")
    def test_success_on_first_attempt(
        self, mock_save: MagicMock, cfg: ToolConfig,
    ) -> None:
        """Returns True and saves password on first successful connect."""
        webui = MagicMock()
        assert connect_with_retry(webui, cfg) is True
        webui.connect.assert_called_once()
        mock_save.assert_called_once_with("secret")

    @patch("main.save_password")
    @patch("main.clear_password")
    @patch("main.getpass.getpass", return_value="correct_pass")
    def test_success_on_second_attempt(
        self, mock_getpass: MagicMock, mock_clear: MagicMock,
        mock_save: MagicMock, cfg: ToolConfig,
    ) -> None:
        """Retries after auth failure, succeeds on second attempt."""
        webui = MagicMock()
        webui.connect.side_effect = [AUTH_ERROR, None]

        assert connect_with_retry(webui, cfg) is True
        assert webui.connect.call_count == 2
        mock_clear.assert_called_once()
        mock_save.assert_called_once_with("correct_pass")

    @patch("main.save_password")
    @patch("main.clear_password")
    @patch("main.getpass.getpass", return_value="still_wrong")
    def test_fails_after_three_attempts(
        self, mock_getpass: MagicMock, mock_clear: MagicMock,
        mock_save: MagicMock, cfg: ToolConfig, capsys: pytest.CaptureFixture,
    ) -> None:
        """Returns False after exhausting all 3 attempts."""
        webui = MagicMock()
        webui.connect.side_effect = AUTH_ERROR

        assert connect_with_retry(webui, cfg) is False
        assert webui.connect.call_count == 3
        mock_save.assert_not_called()
        output = capsys.readouterr().out
        assert "failed after 3 attempts" in output

    @patch("main.save_password")
    @patch("main.clear_password")
    @patch("main.getpass.getpass", side_effect=["wrong", "correct"])
    def test_success_on_third_attempt(
        self, mock_getpass: MagicMock, mock_clear: MagicMock,
        mock_save: MagicMock, cfg: ToolConfig,
    ) -> None:
        """Retries twice, succeeds on third attempt."""
        webui = MagicMock()
        webui.connect.side_effect = [AUTH_ERROR, AUTH_ERROR, None]

        assert connect_with_retry(webui, cfg) is True
        assert webui.connect.call_count == 3
        mock_save.assert_called_once_with("correct")

    @patch("main.save_password")
    def test_non_auth_error_propagates(
        self, mock_save: MagicMock, cfg: ToolConfig,
    ) -> None:
        """Non-auth exceptions are raised, not retried."""
        webui = MagicMock()
        webui.connect.side_effect = ConnectionError("network down")

        with pytest.raises(ConnectionError, match="network down"):
            connect_with_retry(webui, cfg)
        webui.connect.assert_called_once()
        mock_save.assert_not_called()

    @patch("main.save_password")
    @patch("main.clear_password")
    @patch("main.getpass.getpass", return_value="retry_pass")
    def test_clears_saved_password_on_auth_failure(
        self, mock_getpass: MagicMock, mock_clear: MagicMock,
        mock_save: MagicMock, cfg: ToolConfig,
    ) -> None:
        """Clears saved password file each time auth fails."""
        webui = MagicMock()
        webui.connect.side_effect = [AUTH_ERROR, None]

        connect_with_retry(webui, cfg)
        mock_clear.assert_called_once()

    @patch("main.save_password")
    @patch("main.clear_password")
    @patch("main.getpass.getpass", return_value="retry_pass")
    def test_timeout_treated_as_auth_failure(
        self, mock_getpass: MagicMock, mock_clear: MagicMock,
        mock_save: MagicMock, cfg: ToolConfig,
    ) -> None:
        """TimeoutError from hung login is retried like an auth failure."""
        timeout_err = TimeoutError(
            "Login timed out after 60s — invalid username or password"
        )
        webui = MagicMock()
        webui.connect.side_effect = [timeout_err, None]

        assert connect_with_retry(webui, cfg) is True
        assert webui.connect.call_count == 2
        mock_clear.assert_called_once()


# ── nsclient availability checks ────────────────────────────────────


class TestNsclientCheck:
    """Tests for nsclient availability guard in status and upgrade."""

    @patch("main._check_nsclient_available", return_value=False)
    def test_status_aborts_without_nsclient(
        self, mock_check: MagicMock, cfg: ToolConfig,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """cmd_status returns 1 with clear message when nsclient is missing."""
        assert cmd_status(cfg) == 1
        output = capsys.readouterr().out
        assert "nsclient package is not installed" in output

    @patch("main._check_nsclient_available", return_value=False)
    def test_upgrade_aborts_without_nsclient(
        self, mock_check: MagicMock, cfg: ToolConfig,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """cmd_upgrade returns 1 before connecting when nsclient is missing."""
        args = MagicMock()
        args.target = "latest"
        args.from_version = "release-92.0.0"
        assert cmd_upgrade(cfg, args) == 1
        output = capsys.readouterr().out
        assert "nsclient package is not installed" in output
