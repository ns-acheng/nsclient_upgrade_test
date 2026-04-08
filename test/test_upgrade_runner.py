"""
Unit tests for upgrade_runner.py.
All I/O is mocked — no network, no local client, no tenant needed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from upgrade_runner import UpgradeRunner, UpgradeResult, PollResult
from util_config import UpgradeConfig


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_webui() -> MagicMock:
    """Create a mock WebUIClient with default return values."""
    webui = MagicMock()
    webui.get_release_versions.return_value = {
        "latestversion": "95.1.0.900",
        "goldenversions": ["90.0.0", "87.0.0", "84.0.0"],
        "90.0.0": ["90.0.0.100", "90.0.1.200", "90.1.0.300"],
        "87.0.0": ["87.0.0.100", "87.0.1.200"],
        "84.0.0": ["84.0.0.100"],
        "92.0.0": ["92.0.0.100"],
        "80.0.0": ["80.0.0.100"],
    }
    webui.get_sorted_version_list.return_value = [
        "80.0.0", "84.0.0", "87.0.0", "90.0.0", "92.0.0",
    ]
    webui.get_device_version.return_value = "95.1.0.900"
    webui.disable_auto_upgrade.return_value = {"status": "success"}
    webui.enable_upgrade_latest.return_value = {"status": "success"}
    webui.enable_upgrade_golden.return_value = {"status": "success"}
    return webui


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock LocalClient with default return values."""
    client = MagicMock()
    client.email = "test@gmail.com"
    client.platform = "windows"
    client.is_installed.return_value = True
    client.get_installer_filename.return_value = "STAgent.msi"
    client.download_build.return_value = {"location": "C:\\temp\\STAgent.msi"}
    client.get_version.return_value = "92.0.0.100"
    client.update_config.return_value = None
    client.install.return_value = None
    client.uninstall.return_value = None
    return client


@pytest.fixture
def fast_cfg() -> UpgradeConfig:
    """Upgrade config with minimal waits for fast tests."""
    return UpgradeConfig(
        poll_interval_seconds=0,  # No actual sleeping in mocked tests
        max_wait_seconds=1,
        config_update_wait_seconds=0,
    )


@pytest.fixture
def runner(mock_webui: MagicMock, mock_client: MagicMock, fast_cfg: UpgradeConfig) -> UpgradeRunner:
    """Create an UpgradeRunner with mocked dependencies."""
    return UpgradeRunner(
        webui=mock_webui,
        client=mock_client,
        upgrade_cfg=fast_cfg,
        host_name="test-host",
        email="test@gmail.com",
    )


# ── Upgrade to Latest ────────────────────────────────────────────────


class TestUpgradeToLatest:
    """Tests for run_upgrade_to_latest scenario."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_upgrade_success(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Client upgrades to latest version successfully."""
        # Simulate time progression: start=0, then each time() call increments
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]

        # First call returns old version, second returns new (after poll)
        mock_client.get_version.side_effect = [
            "92.0.0.100",  # After install (_prepare_client)
            "92.0.0.100",  # Poll: initial read
            "95.1.0.900",  # Poll: upgraded!
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        result = runner.run_upgrade_to_latest(from_version="release-92.0.0")

        assert result.success is True
        assert result.version_after == "95.1.0.900"
        assert result.scenario == "upgrade_to_latest"
        mock_webui.disable_auto_upgrade.assert_called()  # Cleanup
        mock_webui.enable_upgrade_latest.assert_called_once()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_upgrade_timeout(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
    ) -> None:
        """Client fails to upgrade within timeout."""
        # Time always exceeds timeout
        mock_time.side_effect = [0, 0.1, 100, 100, 100]

        # Version never changes
        mock_client.get_version.return_value = "92.0.0.100"

        result = runner.run_upgrade_to_latest(from_version="release-92.0.0")

        assert result.success is False
        assert "FAILED" in result.message

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_cleanup_on_success(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Cleanup (disable_auto_upgrade) runs after successful upgrade."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.get_version.side_effect = ["92.0.0.100", "92.0.0.100", "95.1.0.900"]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        runner.run_upgrade_to_latest(from_version="release-92.0.0")

        # disable_auto_upgrade called at least twice: once in _prepare, once in _cleanup
        assert mock_webui.disable_auto_upgrade.call_count >= 2

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_cleanup_on_exception(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Cleanup runs even when scenario raises an exception."""
        mock_time.side_effect = [0, 0.5]
        mock_client.download_build.side_effect = Exception("Network error")

        result = runner.run_upgrade_to_latest(from_version="release-92.0.0")

        assert result.success is False
        assert "Exception" in result.message
        # Cleanup still called
        mock_webui.disable_auto_upgrade.assert_called()


# ── Upgrade to Golden ────────────────────────────────────────────────


class TestUpgradeToGolden:
    """Tests for run_upgrade_to_golden scenario."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_golden_latest_no_dot(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Upgrade to latest golden (index=-1) without dot release."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]

        # Expected: base of golden 90.0.0 -> sorted[0] = "90.0.0.100"
        mock_client.get_version.side_effect = [
            "84.0.0.100",  # After install
            "84.0.0.100",  # Poll initial
            "90.0.0.100",  # Poll: upgraded
        ]
        mock_webui.get_device_version.return_value = "90.0.0.100"

        result = runner.run_upgrade_to_golden(golden_index=-1, dot=False)

        assert result.success is True
        assert result.version_after == "90.0.0.100"
        mock_webui.enable_upgrade_golden.assert_called_once_with("90.0.0", dot=False)

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_golden_latest_with_dot(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Upgrade to latest golden with dot release enabled."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]

        # Expected: highest dot of 90.0.0 -> sorted[-1] = "90.1.0.300"
        mock_client.get_version.side_effect = [
            "84.0.0.100",
            "84.0.0.100",
            "90.1.0.300",
        ]
        mock_webui.get_device_version.return_value = "90.1.0.300"

        result = runner.run_upgrade_to_golden(golden_index=-1, dot=True)

        assert result.success is True
        assert result.version_after == "90.1.0.300"
        mock_webui.enable_upgrade_golden.assert_called_once_with("90.0.0", dot=True)

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_golden_n_minus_1(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Upgrade to N-1 golden (index=-2 = 87.0.0)."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]

        mock_client.get_version.side_effect = [
            "80.0.0.100",
            "80.0.0.100",
            "87.0.0.100",
        ]
        mock_webui.get_device_version.return_value = "87.0.0.100"

        result = runner.run_upgrade_to_golden(golden_index=-2, dot=False)

        assert result.success is True
        assert result.version_after == "87.0.0.100"
        mock_webui.enable_upgrade_golden.assert_called_once_with("87.0.0", dot=False)

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_auto_picks_from_version(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """When from_version is None, auto-picks an older version."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        mock_client.get_version.side_effect = ["87.0.0.100", "87.0.0.100", "90.0.0.100"]
        mock_webui.get_device_version.return_value = "90.0.0.100"

        result = runner.run_upgrade_to_golden(from_version=None, golden_index=-1, dot=False)

        assert result.success is True
        # Should have auto-picked release-87.0.0 (max version < 90)
        mock_client.download_build.assert_called_once()
        dl_args = mock_client.download_build.call_args
        assert "release-87.0.0" == dl_args.kwargs.get("build_version", dl_args[1].get("build_version", ""))


# ── Upgrade Disabled ─────────────────────────────────────────────────


class TestUpgradeDisabled:
    """Tests for run_upgrade_disabled scenario."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_no_upgrade_occurs(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Version stays the same when auto-upgrade is disabled."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]

        # Version never changes
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        result = runner.run_upgrade_disabled(from_version="release-92.0.0")

        assert result.success is True
        assert result.version_before == "92.0.0.100"
        assert result.version_after == "92.0.0.100"
        assert "auto-upgrade disabled works" in result.message

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_unexpected_upgrade_detected(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Detects if an unexpected upgrade happened despite being disabled."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.get_version.side_effect = [
            "92.0.0.100",  # After install
            "92.0.0.100",  # Poll initial
            "95.0.0.100",  # Unexpected change!
        ]
        mock_webui.get_device_version.return_value = "95.0.0.100"

        result = runner.run_upgrade_disabled(from_version="release-92.0.0")

        assert result.success is False
        assert "UNEXPECTED" in result.message


# ── WebUI Version Verification ───────────────────────────────────────


class TestWebUIVerification:
    """Tests for WebUI version verification behavior."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_webui_mismatch_logged(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """WebUI version mismatch is captured in result but doesn't fail the test."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.get_version.side_effect = ["92.0.0.100", "92.0.0.100", "95.1.0.900"]
        mock_webui.get_device_version.return_value = "92.0.0.100"  # Stale!

        result = runner.run_upgrade_to_latest(from_version="release-92.0.0")

        # Upgrade itself succeeded (version matched latest)
        assert result.success is True
        # But WebUI reported stale version
        assert result.webui_version == "92.0.0.100"

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_webui_error_handled(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """WebUI query failure doesn't crash the scenario."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.get_version.side_effect = ["92.0.0.100", "92.0.0.100", "95.1.0.900"]
        mock_webui.get_device_version.side_effect = Exception("Connection lost")

        result = runner.run_upgrade_to_latest(from_version="release-92.0.0")

        assert result.success is True
        assert result.webui_version == "error"


# ── Prepare Client ───────────────────────────────────────────────────


class TestPrepareClient:
    """Tests for the _prepare_client helper flow."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_uninstalls_existing_before_install(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Existing client is uninstalled before installing target version."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_installed.return_value = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        runner.run_upgrade_disabled(from_version="release-92.0.0")

        mock_client.uninstall.assert_called_once()
        mock_client.install.assert_called_once()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_skips_uninstall_when_not_installed(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Skips uninstall if client is not currently installed."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_installed.return_value = False
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        runner.run_upgrade_disabled(from_version="release-92.0.0")

        mock_client.uninstall.assert_not_called()
        mock_client.install.assert_called_once()
