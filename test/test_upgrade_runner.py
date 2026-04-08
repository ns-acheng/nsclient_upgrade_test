"""
Unit tests for upgrade_runner.py.
All I/O is mocked — no network, no local client, no tenant needed.
"""

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from upgrade_runner import (
    UpgradeRunner, UpgradeResult, PollResult, RebootVerifyResult,
    BASE_VERSION_DIR, INSTALLER_JSON, REBOOT_TIMING_PRESETS,
)
from util_client import ExeValidationResult, UninstallEntryResult
from util_config import (
    UpgradeConfig, RebootTestState,
    save_reboot_state, load_reboot_state, clear_reboot_state,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_webui() -> MagicMock:
    """Create a mock WebUIClient with default return values."""
    webui = MagicMock()
    webui.hostname = "test-tenant.goskope.com"
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
    webui.set_update_win64bit.return_value = {"status": "success"}
    return webui


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock LocalClient with default return values."""
    client = MagicMock()
    client.email = "test@gmail.com"
    client.platform = "windows"
    client.is_initialized = True
    client.is_service_running.return_value = False
    client.wait_for_service.return_value = True
    client.get_installer_filename.return_value = "STAgent.msi"
    client.download_build.return_value = {"location": "C:\\temp\\STAgent.msi"}
    client.get_version.return_value = "92.0.0.100"
    client.update_config.return_value = None
    client.install_msi.return_value = None
    client.create.return_value = None
    client.sync_config_from_tenant.return_value = None
    client.detect_tenant_from_nsconfig.return_value = None
    client.create_verify_task.return_value = None
    client.delete_verify_task.return_value = None
    client.verify_executables.return_value = ExeValidationResult(
        valid=True, install_dir="C:\\fake", present=["stAgentSvc.exe"], missing=[], version_mismatches=[],
    )
    client.check_uninstall_registry.return_value = UninstallEntryResult(
        found=True, display_name="Netskope Client", display_version="95.1.0.900", install_location="C:\\fake",
    )
    return client


@pytest.fixture
def fast_cfg() -> UpgradeConfig:
    """Upgrade config with minimal waits for fast tests."""
    return UpgradeConfig(
        poll_interval_seconds=0,  # No actual sleeping in mocked tests
        max_wait_seconds=1,
        config_update_wait_seconds=0,
    )


@pytest.fixture(autouse=True)
def no_local_installer(tmp_path: Path) -> Any:
    """Prevent tests from picking up real data/base_version/ or installer.json."""
    empty_dir = tmp_path / "empty_base_version"
    empty_dir.mkdir()
    fake_json = tmp_path / "no_installer.json"  # Does not exist
    with patch("upgrade_runner.BASE_VERSION_DIR", empty_dir), \
         patch("upgrade_runner.INSTALLER_JSON", fake_json):
        yield


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
        mock_client.is_service_running.side_effect = [False, True]

        # First call returns old version (version_before), then poll reads
        mock_client.get_version.side_effect = [
            "92.0.0.100",  # version_before (Phase 2 init)
            "92.0.0.100",  # Poll: initial read
            "95.1.0.900",  # Poll: upgraded!
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

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

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

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
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = ["92.0.0.100", "92.0.0.100", "95.1.0.900"]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        runner.run_upgrade_to_latest(from_version="92.0.0")

        # disable_auto_upgrade called at least twice: once in _prepare, once in _cleanup
        assert mock_webui.disable_auto_upgrade.call_count >= 2

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_cleanup_skips_rollback_when_upgrade_never_enabled(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Cleanup skips disable_auto_upgrade when install fails before upgrade is enabled."""
        mock_time.side_effect = [0, 0.5]
        mock_client.download_build.side_effect = Exception("Network error")

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

        assert result.success is False
        assert "Exception" in result.message
        # disable_auto_upgrade NOT called in cleanup — upgrade was never enabled
        mock_webui.disable_auto_upgrade.assert_not_called()


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
        mock_client.is_service_running.side_effect = [False, True]

        # Expected: base of golden 90.0.0 -> sorted[0] = "90.0.0.100"
        mock_client.get_version.side_effect = [
            "84.0.0.100",  # After install
            "84.0.0.100",  # Poll initial
            "90.0.0.100",  # Poll: upgraded
        ]
        mock_webui.get_device_version.return_value = "90.0.0.100"

        result = runner.run_upgrade_to_golden(dot=False)

        assert result.success is True
        assert result.version_after == "90.0.0.100"
        mock_webui.enable_upgrade_golden.assert_called_once_with(
            "90.0.0", dot=False, search_config="",
        )

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
        mock_client.is_service_running.side_effect = [False, True]

        # Expected: highest dot of 90.0.0 -> sorted[-1] = "90.1.0.300"
        mock_client.get_version.side_effect = [
            "84.0.0.100",
            "84.0.0.100",
            "90.1.0.300",
        ]
        mock_webui.get_device_version.return_value = "90.1.0.300"

        result = runner.run_upgrade_to_golden(dot=True)

        assert result.success is True
        assert result.version_after == "90.1.0.300"
        mock_webui.enable_upgrade_golden.assert_called_once_with(
            "90.0.0", dot=True, search_config="",
        )

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
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = ["87.0.0.100", "87.0.0.100", "90.0.0.100"]
        mock_webui.get_device_version.return_value = "90.0.0.100"

        result = runner.run_upgrade_to_golden(from_version=None, dot=False)

        assert result.success is True
        # Should have auto-picked release-87.0.0 (max version < 90)
        mock_client.download_build.assert_called_once()
        dl_args = mock_client.download_build.call_args
        assert "release-87.0.0" == dl_args.kwargs.get("build_version", dl_args[1].get("build_version", ""))


    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_golden_dot_picks_highest_version_numerically(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        fast_cfg: UpgradeConfig,
    ) -> None:
        """golden-dot picks 135.1.10.2611 over 135.1.7.2602 (numeric sort)."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        mock_client.is_service_running.side_effect = [False, True]

        # Versions that sort differently lexicographically vs numerically
        mock_webui.get_release_versions.return_value = {
            "latestversion": "136.0.4.2612",
            "goldenversions": ["135.0.0"],
            "135.0.0": [
                "135.0.0.2500",
                "135.1.7.2602",
                "135.1.10.2611",
            ],
        }
        mock_webui.get_sorted_version_list.return_value = ["132.0.0", "135.0.0"]

        # Expected: 135.1.10.2611 (highest numerically, NOT 135.1.7.2602)
        mock_client.get_version.side_effect = [
            "132.0.0.100",
            "132.0.0.100",
            "135.1.10.2611",
        ]
        mock_webui.get_device_version.return_value = "135.1.10.2611"

        runner = UpgradeRunner(
            webui=mock_webui, client=mock_client, upgrade_cfg=fast_cfg,
            host_name="test-host", email="test@gmail.com",
        )

        result = runner.run_upgrade_to_golden(dot=True)

        assert result.success is True
        assert result.expected_version == "135.1.10.2611"
        assert result.version_after == "135.1.10.2611"


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
        mock_client.is_service_running.side_effect = [False, True]

        # Version never changes
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        result = runner.run_upgrade_disabled(from_version="92.0.0")

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

        result = runner.run_upgrade_disabled(from_version="92.0.0")

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
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = ["92.0.0.100", "92.0.0.100", "95.1.0.900"]
        mock_webui.get_device_version.return_value = "92.0.0.100"  # Stale!

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

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
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = ["92.0.0.100", "92.0.0.100", "95.1.0.900"]
        mock_webui.get_device_version.side_effect = Exception("Connection lost")

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

        assert result.success is True
        assert result.webui_version == "error"


# ── Ensure Client Installed ──────────────────────────────────────────


class TestEnsureClientInstalled:
    """Tests for the _ensure_client_installed helper flow."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_service_running_skips_install(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """When service is already running, skip install and return version."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.return_value = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.install_msi.assert_not_called()
        mock_client.download_build.assert_not_called()
        mock_client.wait_for_service.assert_not_called()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_service_not_running_installs_via_msiexec(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """When service is not running, install via msiexec and wait for service."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.return_value = False
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.install_msi.assert_called_once()
        mock_client.wait_for_service.assert_called_once()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_email_invite_sent_during_install(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Email invite is sent when client is not installed."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.return_value = False
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        runner.run_upgrade_disabled(
            from_version="92.0.0", invite_email="user@example.com",
        )

        mock_webui.send_email_invite.assert_called_once_with("user@example.com")

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_email_invite_skipped_when_running(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Email invite is NOT sent when service is already running."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.return_value = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        runner.run_upgrade_disabled(
            from_version="92.0.0", invite_email="user@example.com",
        )

        mock_webui.send_email_invite.assert_not_called()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_service_wait_failure(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
    ) -> None:
        """Fails when service does not start after installation."""
        mock_time.side_effect = [0, 0.5]
        mock_client.is_service_running.return_value = False
        mock_client.wait_for_service.return_value = False

        result = runner.run_upgrade_disabled(from_version="92.0.0")

        assert result.success is False
        assert "stAgentSvc" in result.message

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_local_installer_exact_match(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When exact installer filename exists in base_version/, download is skipped."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        # Place exact match file
        (tmp_path / "STAgent.msi").touch()

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.download_build.assert_not_called()
        mock_client.install_msi.assert_called_once_with(
            str(tmp_path / "STAgent.msi"),
        )

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_local_installer_64bit(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        fast_cfg: UpgradeConfig,
        tmp_path: Path,
    ) -> None:
        """With is_64_bit=True, picks STAgent64.msi from base_version/."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"
        mock_client.get_installer_filename.return_value = "STAgent64.msi"

        # Place both 32-bit and 64-bit files
        (tmp_path / "STAgent.msi").touch()
        (tmp_path / "STAgent64.msi").touch()

        runner_64 = UpgradeRunner(
            webui=mock_webui,
            client=mock_client,
            upgrade_cfg=fast_cfg,
            host_name="test-host",
            email="test@gmail.com",
            source_64_bit=True,
        )

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner_64.run_upgrade_disabled(from_version="92.0.0")

        mock_client.download_build.assert_not_called()
        mock_client.install_msi.assert_called_once_with(
            str(tmp_path / "STAgent64.msi"),
        )

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_local_installer_single_file_copied(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Single file in base_version/ is copied to expected filename (original preserved)."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        # Place a single file with a different name
        (tmp_path / "NSClient_old.msi").touch()

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        # Copy created the expected file
        assert (tmp_path / "STAgent.msi").exists()
        # Original file is preserved (copy, not move)
        assert (tmp_path / "NSClient_old.msi").exists()
        mock_client.download_build.assert_not_called()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_no_local_installer_falls_back_to_download(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When data/base_version/ is empty, falls back to downloading."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.download_build.assert_called_once()
        mock_client.install_msi.assert_called_once()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_multiple_files_no_match_falls_back_to_download(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Multiple files with no exact match and >1 file falls back to download."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        # Place two non-matching files — ambiguous, can't auto-copy
        (tmp_path / "installer_a.msi").touch()
        (tmp_path / "installer_b.msi").touch()

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.download_build.assert_called_once()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_installer_json_renames_base_to_tenant_name(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Base installer is copied to the tenant-specific name from installer.json."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        # Place base installer
        (tmp_path / "STAgent.msi").write_bytes(b"fake-msi")

        # Create installer.json with tenant-specific prefix
        installer_json = tmp_path / "installer.json"
        prefix = "NSClient_addon-test-tenant_12345"
        installer_json.write_text(
            f'{{"test-tenant.goskope.com": {{"installer_name": "{prefix}"}}}}',
            encoding="utf-8",
        )

        token = "abc123token"
        download_link = f"https://download-test.example.com/dlr/win/{token}"
        expected_name = f"{prefix}_{token}_.msi"

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path), \
             patch("upgrade_runner.INSTALLER_JSON", installer_json), \
             patch("builtins.input", return_value=download_link), \
             patch("builtins.print"):
            runner.run_upgrade_disabled(
                from_version="92.0.0", invite_email="test@example.com",
            )

        # Base installer still exists
        assert (tmp_path / "STAgent.msi").exists()
        # Tenant-specific copy was cleaned up after upgrade
        assert not (tmp_path / expected_name).exists()
        # msiexec was called with the tenant-specific name
        mock_client.install_msi.assert_called_once_with(
            str(tmp_path / expected_name),
        )

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_no_installer_no_from_version_errors(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
    ) -> None:
        """Errors when no local installer and no from_version provided."""
        mock_time.side_effect = [0, 0.5]

        result = runner.run_upgrade_disabled()

        assert result.success is False
        assert "No installer found" in result.message

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_auto_email_extracts_link(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """GmailBrowser auto-extracts download link; no manual input."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.return_value = False
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        mock_browser = MagicMock()
        mock_browser.get_download_link.return_value = (
            "https://download.example.com/dlr/win/TOKEN"
        )
        mock_browser.__enter__ = MagicMock(return_value=mock_browser)
        mock_browser.__exit__ = MagicMock(return_value=False)

        with patch(
            "util_email.GmailBrowser", return_value=mock_browser,
        ), patch("builtins.input") as mock_input:
            runner.run_upgrade_disabled(
                from_version="92.0.0",
                invite_email="user@example.com",
            )
            mock_browser.connect.assert_called_once()
            mock_browser.get_download_link.assert_called_once()
            mock_input.assert_not_called()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_auto_email_fallback_to_manual(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Falls back to manual input when auto-email fails."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.return_value = False
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        mock_browser = MagicMock()
        mock_browser.connect.side_effect = RuntimeError("no Chrome")
        mock_browser.__enter__ = MagicMock(return_value=mock_browser)
        mock_browser.__exit__ = MagicMock(return_value=False)

        with patch(
            "util_email.GmailBrowser", return_value=mock_browser,
        ), patch("builtins.input", return_value="") as mock_input:
            runner.run_upgrade_disabled(
                from_version="92.0.0",
                invite_email="user@example.com",
            )
            # Falls back to manual input prompt
            mock_input.assert_called_once()


# ── MSI Version Check ───────────────────────────────────────────────


class TestMsiVersionCheck:
    """Tests for MSI version comparison in _ensure_client_installed."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_same_version_running_skips_install(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Case 1: Same version installed and running — skip install."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.return_value = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"
        mock_client.get_msi_subject.return_value = "92.0.0.100"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=True, display_name="Netskope Client",
            display_version="92.0.0.100", install_location="C:\\fake",
            product_code="{GUID-123}",
        )

        (tmp_path / "STAgent.msi").write_bytes(b"fake")

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.install_msi.assert_not_called()
        mock_client.uninstall_msi.assert_not_called()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_different_version_uninstalls_first(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Case 0: Different version installed — uninstall then install."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.side_effect = [True, True]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"
        mock_client.get_msi_subject.return_value = "90.0.0.100"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=True, display_name="Netskope Client",
            display_version="92.0.0.100", install_location="C:\\fake",
            product_code="{GUID-123}",
        )

        (tmp_path / "STAgent.msi").write_bytes(b"fake")

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.uninstall_msi.assert_called_once_with("{GUID-123}")
        mock_client.install_msi.assert_called_once()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_same_version_not_running_reinstalls(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Case 2: Same version but not running — uninstall then install."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"
        mock_client.get_msi_subject.return_value = "92.0.0.100"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=True, display_name="Netskope Client",
            display_version="92.0.0.100", install_location="C:\\fake",
            product_code="{GUID-123}",
        )

        (tmp_path / "STAgent.msi").write_bytes(b"fake")

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.uninstall_msi.assert_called_once_with("{GUID-123}")
        mock_client.install_msi.assert_called_once()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_not_installed_does_fresh_install(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Case 3: Not installed — install without uninstall."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"
        mock_client.get_msi_subject.return_value = "92.0.0.100"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=False, display_name="", display_version="",
            install_location="",
        )

        (tmp_path / "STAgent.msi").write_bytes(b"fake")

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.uninstall_msi.assert_not_called()
        mock_client.install_msi.assert_called_once()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_no_msi_subject_falls_back_to_service_check(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When MSI subject is empty, fall back to service running check."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.return_value = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"
        mock_client.get_msi_subject.return_value = ""
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=True, display_name="Netskope Client",
            display_version="92.0.0.100", install_location="C:\\fake",
            product_code="{GUID-123}",
        )

        (tmp_path / "STAgent.msi").write_bytes(b"fake")

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path):
            runner.run_upgrade_disabled(from_version="92.0.0")

        # Falls back to service running → skips install
        mock_client.install_msi.assert_not_called()
        mock_client.uninstall_msi.assert_not_called()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_different_version_sends_email_after_uninstall(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Email invite is sent after uninstall when version differs."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.side_effect = [True, True]
        mock_client.get_version.return_value = "90.0.0.100"
        mock_webui.get_device_version.return_value = "90.0.0.100"
        mock_client.get_msi_subject.return_value = "90.0.0.100"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=True, display_name="Netskope Client",
            display_version="92.0.0.100", install_location="C:\\fake",
            product_code="{GUID-123}",
        )

        (tmp_path / "STAgent.msi").write_bytes(b"fake")

        with patch("upgrade_runner.BASE_VERSION_DIR", tmp_path), \
             patch("builtins.input", return_value=""), \
             patch("builtins.print"):
            runner.run_upgrade_disabled(
                from_version="90.0.0", invite_email="user@example.com",
            )

        mock_client.uninstall_msi.assert_called_once()
        mock_webui.send_email_invite.assert_called_once_with("user@example.com")
        mock_client.install_msi.assert_called_once()


# ── Init Nsclient ───────────────────────────────────────────────────


class TestInitNsclient:
    """Tests for the _init_nsclient lazy initialization."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_creates_client_when_not_initialized(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """_init_nsclient calls create() when client is not yet initialized."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_initialized = False
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.create.assert_called()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_skips_create_when_already_initialized(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """_init_nsclient skips create() when client is already initialized."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_initialized = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"

        runner.run_upgrade_disabled(from_version="92.0.0")

        mock_client.create.assert_not_called()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_nsclient_missing_falls_back_to_webui(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """When nsclient is missing, falls back to WebUI for version monitoring."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_initialized = False
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.create.side_effect = ModuleNotFoundError("No module named 'nsclient'")
        mock_webui.get_device_version.return_value = "92.0.0.100"

        result = runner.run_upgrade_disabled(from_version="92.0.0")

        # Scenario completes via WebUI fallback instead of crashing
        assert result.success is True
        mock_webui.get_device_version.assert_called()


# ── Post-Upgrade Service Check ──────────────────────────────────────


class TestPostUpgradeServiceCheck:
    """Tests for post-upgrade service verification."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_service_checked_after_upgrade(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Service running check happens after upgrade completes."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = [
            "92.0.0.100", "92.0.0.100", "95.1.0.900",
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        runner.run_upgrade_to_latest(from_version="92.0.0")

        # is_service_running called at least twice:
        # once in _ensure_client_installed, once in _verify_service_running
        assert mock_client.is_service_running.call_count >= 2

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_service_down_after_upgrade_fails_validation(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Service down after upgrade fails pre-report validation."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.is_service_running.return_value = False
        mock_client.get_version.side_effect = [
            "92.0.0.100", "92.0.0.100", "95.1.0.900",
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

        assert result.success is False
        assert result.service_running is False
        assert result.version_after == "95.1.0.900"
        assert "service not running" in result.message


# ── Pre-Report Validation ──────────────────────────────────────────


class TestPreReportValidation:
    """Tests for exe/registry/service validation in upgrade scenarios."""

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_exe_missing_fails_upgrade(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Upgrade fails when a required executable is missing."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = [
            "92.0.0.100", "92.0.0.100", "95.1.0.900",
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"
        mock_client.verify_executables.return_value = ExeValidationResult(
            valid=False,
            install_dir=r"C:\Program Files (x86)\Netskope\STAgent",
            present=["stAgentSvc.exe"],
            missing=["stAgentUI.exe"],
            version_mismatches=[],
        )

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

        assert result.success is False
        assert result.exe_validation is not None
        assert "stAgentUI.exe" in result.exe_validation.missing
        assert "missing exe" in result.message

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_exe_version_mismatch_fails_upgrade(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Upgrade fails when executable has wrong version."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = [
            "92.0.0.100", "92.0.0.100", "95.1.0.900",
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"
        mock_client.verify_executables.return_value = ExeValidationResult(
            valid=False,
            install_dir=r"C:\Program Files (x86)\Netskope\STAgent",
            present=["stAgentSvc.exe", "stAgentUI.exe"],
            missing=[],
            version_mismatches=["stAgentSvc.exe: 92.0.0.100 (expected 95.1.0.900)"],
        )

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

        assert result.success is False
        assert "exe version mismatch" in result.message

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_uninstall_entry_missing_fails_upgrade(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Upgrade fails when uninstall registry entry is missing."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = [
            "92.0.0.100", "92.0.0.100", "95.1.0.900",
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=False, display_name="", display_version="", install_location="",
        )

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

        assert result.success is False
        assert result.uninstall_entry is not None
        assert result.uninstall_entry.found is False
        assert "uninstall registry entry missing" in result.message

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_all_validation_passes(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Upgrade succeeds when all validation items pass."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = [
            "92.0.0.100", "92.0.0.100", "95.1.0.900",
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

        assert result.success is True
        assert result.service_running is True
        assert result.exe_validation is not None
        assert result.exe_validation.valid is True
        assert result.uninstall_entry is not None
        assert result.uninstall_entry.found is True

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_golden_validation_fails_on_missing_exe(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Golden upgrade also fails when exe validation fails."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.side_effect = [
            "84.0.0.100", "84.0.0.100", "90.0.0.100",
        ]
        mock_webui.get_device_version.return_value = "90.0.0.100"
        mock_client.verify_executables.return_value = ExeValidationResult(
            valid=False,
            install_dir=r"C:\Program Files (x86)\Netskope\STAgent",
            present=["stAgentSvc.exe"],
            missing=["stAgentUI.exe"],
            version_mismatches=[],
        )

        result = runner.run_upgrade_to_golden(dot=False)

        assert result.success is False
        assert "missing exe" in result.message

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_disabled_validation_fails_on_missing_uninstall(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Disabled upgrade also fails when uninstall entry is missing."""
        mock_time.side_effect = [0, 0.1, 100, 100, 100]
        mock_client.is_service_running.side_effect = [False, True]
        mock_client.get_version.return_value = "92.0.0.100"
        mock_webui.get_device_version.return_value = "92.0.0.100"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=False, display_name="", display_version="", install_location="",
        )

        result = runner.run_upgrade_disabled(from_version="92.0.0")

        assert result.success is False
        assert "uninstall registry entry missing" in result.message


# ── Reboot State Persistence ───────────────────────────────────────


class TestRebootStatePersistence:
    """Tests for save/load/clear of RebootTestState."""

    def test_save_and_load(self, tmp_path: Path) -> None:
        """State round-trips through JSON correctly."""
        state = RebootTestState(
            scenario="reboot_interrupt",
            version_before="135.0.0.2631",
            target_type="latest",
            expected_version="136.0.4.2612",
            reboot_timing="mid",
            source_64_bit=False,
            target_64_bit=True,
            config_name="acheng config",
            stabilize_wait=300,
            timestamp="2026-04-08T10:00:00",
        )
        path = tmp_path / "reboot_state.json"
        save_reboot_state(state, path=path)

        loaded = load_reboot_state(path=path)
        assert loaded is not None
        assert loaded.version_before == "135.0.0.2631"
        assert loaded.target_64_bit is True
        assert loaded.stabilize_wait == 300

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """Returns None when state file does not exist."""
        result = load_reboot_state(path=tmp_path / "missing.json")
        assert result is None

    def test_clear(self, tmp_path: Path) -> None:
        """Deletes the state file."""
        path = tmp_path / "reboot_state.json"
        path.write_text("{}", encoding="utf-8")
        assert path.is_file()

        clear_reboot_state(path=path)
        assert not path.is_file()

    def test_clear_missing_file(self, tmp_path: Path) -> None:
        """No error when clearing a non-existent file."""
        clear_reboot_state(path=tmp_path / "missing.json")


# ── Reboot-Interrupt Setup ─────────────────────────────────────────


class TestRebootInterruptSetup:
    """Tests for run_reboot_interrupt_setup (Phase 1)."""

    @patch("upgrade_runner.subprocess.run")
    @patch("upgrade_runner.save_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_setup_latest_saves_state_and_triggers_reboot(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_save_state: MagicMock,
        mock_subprocess: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Phase 1 saves state and schedules reboot."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3]
        mock_client.is_service_running.return_value = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_subprocess.return_value = MagicMock(returncode=0)

        result = runner.run_reboot_interrupt_setup(
            target_type="latest",
            reboot_timing="mid",
        )

        assert result.success is True
        assert "reboot" in result.message.lower()
        mock_save_state.assert_called_once()
        saved = mock_save_state.call_args[0][0]
        assert saved.target_type == "latest"
        assert saved.reboot_timing == "mid"
        assert saved.expected_version == "95.1.0.900"

        # Verify shutdown command was called with correct delay
        mock_subprocess.assert_called()
        shutdown_call = [
            c for c in mock_subprocess.call_args_list
            if "shutdown" in str(c)
        ]
        assert len(shutdown_call) == 1
        assert str(REBOOT_TIMING_PRESETS["mid"]) in str(shutdown_call[0])

    @patch("upgrade_runner.subprocess.run")
    @patch("upgrade_runner.save_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_setup_golden_with_dot(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_save_state: MagicMock,
        mock_subprocess: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Phase 1 with golden+dot resolves highest dot release."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3]
        mock_client.is_service_running.return_value = True
        mock_client.get_version.return_value = "84.0.0.100"
        mock_subprocess.return_value = MagicMock(returncode=0)

        result = runner.run_reboot_interrupt_setup(
            target_type="golden",
            reboot_timing="early",
            dot=True,
        )

        assert result.success is True
        mock_save_state.assert_called_once()
        saved = mock_save_state.call_args[0][0]
        # Highest dot of golden 90.0.0 is "90.1.0.300"
        assert saved.expected_version == "90.1.0.300"

    @patch("upgrade_runner.subprocess.run")
    @patch("upgrade_runner.save_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_setup_custom_timing(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_save_state: MagicMock,
        mock_subprocess: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Custom numeric timing is passed through to shutdown command."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3]
        mock_client.is_service_running.return_value = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_subprocess.return_value = MagicMock(returncode=0)

        result = runner.run_reboot_interrupt_setup(
            target_type="latest",
            reboot_timing="45",
        )

        assert result.success is True
        shutdown_call = [
            c for c in mock_subprocess.call_args_list
            if "shutdown" in str(c)
        ]
        assert "45" in str(shutdown_call[0])

    @patch("upgrade_runner.subprocess.run")
    @patch("upgrade_runner.save_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_setup_64bit_flags(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_save_state: MagicMock,
        mock_subprocess: MagicMock,
        mock_client: MagicMock,
        mock_webui: MagicMock,
        fast_cfg: UpgradeConfig,
    ) -> None:
        """source_64_bit and target_64_bit are saved correctly."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3]
        mock_client.is_service_running.return_value = True
        mock_client.get_version.return_value = "92.0.0.100"
        mock_subprocess.return_value = MagicMock(returncode=0)

        runner_64 = UpgradeRunner(
            webui=mock_webui,
            client=mock_client,
            upgrade_cfg=fast_cfg,
            host_name="test-host",
            email="test@gmail.com",
            source_64_bit=True,
            target_64_bit=False,
        )

        result = runner_64.run_reboot_interrupt_setup(
            target_type="latest",
            reboot_timing="mid",
        )

        assert result.success is True
        saved = mock_save_state.call_args[0][0]
        assert saved.source_64_bit is True
        assert saved.target_64_bit is False


# ── Reboot-Interrupt Verify ────────────────────────────────────────


class TestRebootVerify:
    """Tests for run_reboot_verify (Phase 2)."""

    @staticmethod
    def _write_state(tmp_path: Path, **overrides: Any) -> Path:
        """Helper to write a reboot state file."""
        defaults = {
            "scenario": "reboot_interrupt",
            "version_before": "92.0.0.100",
            "target_type": "latest",
            "expected_version": "95.1.0.900",
            "reboot_timing": "mid",
            "source_64_bit": False,
            "target_64_bit": False,
            "config_name": "test config",
            "stabilize_wait": 0,
            "timestamp": "2026-04-08T10:00:00",
        }
        defaults.update(overrides)
        path = tmp_path / "reboot_state.json"
        path.write_text(json.dumps(defaults), encoding="utf-8")
        return path

    @patch("upgrade_runner.clear_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_upgrade_completed(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_clear: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Verify succeeds when upgrade completed (version matches expected)."""
        mock_time.side_effect = [0, 0.1, 0.2]
        state_path = self._write_state(tmp_path)
        mock_client.get_version.return_value = "95.1.0.900"
        mock_client.query_service.return_value = MagicMock(
            name="stAgentSvc", exists=True, state="RUNNING",
        )
        mock_client.query_service_binpath.return_value = (
            r'"C:\Program Files (x86)\Netskope\STAgent\stwatchdog.exe"'
        )
        mock_client.verify_install_dir.return_value = True
        mock_client.get_install_dir.return_value = Path(
            r"C:\Program Files (x86)\Netskope\STAgent"
        )

        result = runner.run_reboot_verify(state_path=state_path)

        assert result.success is True
        assert result.upgrade_completed is True
        assert result.rolled_back is False
        assert result.version_after == "95.1.0.900"
        mock_clear.assert_called_once()

    @patch("upgrade_runner.clear_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_rolled_back(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_clear: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Verify succeeds when client rolled back to original version."""
        mock_time.side_effect = [0, 0.1, 0.2]
        state_path = self._write_state(tmp_path)
        mock_client.get_version.return_value = "92.0.0.100"
        mock_client.query_service.return_value = MagicMock(
            name="stAgentSvc", exists=True, state="RUNNING",
        )
        mock_client.query_service_binpath.return_value = (
            r'"C:\Program Files (x86)\Netskope\STAgent\stwatchdog.exe"'
        )
        mock_client.verify_install_dir.return_value = True
        mock_client.get_install_dir.return_value = Path(
            r"C:\Program Files (x86)\Netskope\STAgent"
        )

        result = runner.run_reboot_verify(state_path=state_path)

        assert result.success is True
        assert result.upgrade_completed is False
        assert result.rolled_back is True

    @patch("upgrade_runner.clear_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_service_down_fails(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_clear: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Verify fails when a service is not running."""
        mock_time.side_effect = [0, 0.1, 0.2]
        state_path = self._write_state(tmp_path)
        mock_client.get_version.return_value = "95.1.0.900"
        mock_client.query_service.return_value = MagicMock(
            name="stAgentSvc", exists=True, state="STOPPED",
        )
        mock_client.query_service_binpath.return_value = (
            r'"C:\Program Files (x86)\Netskope\STAgent\stwatchdog.exe"'
        )
        mock_client.verify_install_dir.return_value = True
        mock_client.get_install_dir.return_value = Path(
            r"C:\Program Files (x86)\Netskope\STAgent"
        )

        result = runner.run_reboot_verify(state_path=state_path)

        assert result.success is False
        assert "not all services running" in result.message

    @patch("upgrade_runner.clear_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_unexpected_version_fails(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_clear: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Verify fails when version is neither expected nor original."""
        mock_time.side_effect = [0, 0.1, 0.2]
        state_path = self._write_state(tmp_path)
        mock_client.get_version.return_value = "99.0.0.999"
        mock_client.query_service.return_value = MagicMock(
            name="stAgentSvc", exists=True, state="RUNNING",
        )
        mock_client.query_service_binpath.return_value = (
            r'"C:\Program Files (x86)\Netskope\STAgent\stwatchdog.exe"'
        )
        mock_client.verify_install_dir.return_value = True
        mock_client.get_install_dir.return_value = Path(
            r"C:\Program Files (x86)\Netskope\STAgent"
        )

        result = runner.run_reboot_verify(state_path=state_path)

        assert result.success is False
        assert result.upgrade_completed is False
        assert result.rolled_back is False
        assert "unexpected version" in result.message.lower()

    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_no_state_file(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        tmp_path: Path,
    ) -> None:
        """Verify fails gracefully when no state file exists."""
        mock_time.side_effect = [0, 0.1]
        result = runner.run_reboot_verify(
            state_path=tmp_path / "missing.json",
        )

        assert result.success is False
        assert "reboot-setup" in result.message.lower()

    @patch("upgrade_runner.clear_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_64bit_upgrade_checks_correct_dir(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_clear: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When upgrade completes to 64-bit target, verifies 64-bit dir."""
        mock_time.side_effect = [0, 0.1, 0.2]
        state_path = self._write_state(
            tmp_path,
            source_64_bit=False,
            target_64_bit=True,
        )
        mock_client.get_version.return_value = "95.1.0.900"
        mock_client.query_service.return_value = MagicMock(
            name="stAgentSvc", exists=True, state="RUNNING",
        )
        mock_client.query_service_binpath.return_value = (
            r'"C:\Program Files\Netskope\STAgent\stwatchdog.exe"'
        )
        mock_client.verify_install_dir.return_value = True
        mock_client.get_install_dir.return_value = Path(
            r"C:\Program Files\Netskope\STAgent"
        )

        result = runner.run_reboot_verify(state_path=state_path)

        assert result.success is True
        # verify_install_dir called with target_64_bit=True since upgrade completed
        mock_client.verify_install_dir.assert_called_with(True)

    @patch("upgrade_runner.clear_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_exe_missing_fails(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_clear: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Verify fails when an executable is missing from install dir."""
        mock_time.side_effect = [0, 0.1, 0.2]
        state_path = self._write_state(tmp_path)
        mock_client.get_version.return_value = "95.1.0.900"
        mock_client.query_service.return_value = MagicMock(
            name="stAgentSvc", exists=True, state="RUNNING",
        )
        mock_client.query_service_binpath.return_value = (
            r'"C:\Program Files (x86)\Netskope\STAgent\stwatchdog.exe"'
        )
        mock_client.verify_install_dir.return_value = True
        mock_client.get_install_dir.return_value = Path(
            r"C:\Program Files (x86)\Netskope\STAgent"
        )
        mock_client.verify_executables.return_value = ExeValidationResult(
            valid=False,
            install_dir=r"C:\Program Files (x86)\Netskope\STAgent",
            present=["stAgentSvc.exe"],
            missing=["stAgentUI.exe"],
            version_mismatches=[],
        )

        result = runner.run_reboot_verify(state_path=state_path)

        assert result.success is False
        assert "missing exe" in result.message.lower()
        assert result.exe_validation is not None
        assert "stAgentUI.exe" in result.exe_validation.missing

    @patch("upgrade_runner.clear_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_exe_version_mismatch_fails(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_clear: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Verify fails when an executable has wrong version."""
        mock_time.side_effect = [0, 0.1, 0.2]
        state_path = self._write_state(tmp_path)
        mock_client.get_version.return_value = "95.1.0.900"
        mock_client.query_service.return_value = MagicMock(
            name="stAgentSvc", exists=True, state="RUNNING",
        )
        mock_client.query_service_binpath.return_value = (
            r'"C:\Program Files (x86)\Netskope\STAgent\stwatchdog.exe"'
        )
        mock_client.verify_install_dir.return_value = True
        mock_client.get_install_dir.return_value = Path(
            r"C:\Program Files (x86)\Netskope\STAgent"
        )
        mock_client.verify_executables.return_value = ExeValidationResult(
            valid=False,
            install_dir=r"C:\Program Files (x86)\Netskope\STAgent",
            present=["stAgentSvc.exe", "stAgentUI.exe"],
            missing=[],
            version_mismatches=["stAgentSvc.exe: 92.0.0.100 (expected 95.1.0.900)"],
        )

        result = runner.run_reboot_verify(state_path=state_path)

        assert result.success is False
        assert "exe version mismatch" in result.message.lower()

    @patch("upgrade_runner.clear_reboot_state")
    @patch("upgrade_runner.time.sleep", return_value=None)
    @patch("upgrade_runner.time.time")
    def test_verify_uninstall_entry_missing_fails(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        mock_clear: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Verify fails when uninstall registry entry is missing."""
        mock_time.side_effect = [0, 0.1, 0.2]
        state_path = self._write_state(tmp_path)
        mock_client.get_version.return_value = "95.1.0.900"
        mock_client.query_service.return_value = MagicMock(
            name="stAgentSvc", exists=True, state="RUNNING",
        )
        mock_client.query_service_binpath.return_value = (
            r'"C:\Program Files (x86)\Netskope\STAgent\stwatchdog.exe"'
        )
        mock_client.verify_install_dir.return_value = True
        mock_client.get_install_dir.return_value = Path(
            r"C:\Program Files (x86)\Netskope\STAgent"
        )
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=False, display_name="", display_version="", install_location="",
        )

        result = runner.run_reboot_verify(state_path=state_path)

        assert result.success is False
        assert "uninstall registry entry missing" in result.message.lower()


# ── Timing Monitor Integration ──────────────────────────────────────


class TestTimingMonitorIntegration:
    """Tests for timing monitor start/stop in upgrade scenarios."""

    @patch("util_monitor.TimingMonitor")
    def test_monitor_started_when_reboot_time_set(
        self,
        MockMonitor: MagicMock,
        mock_webui: MagicMock,
        mock_client: MagicMock,
        fast_cfg: UpgradeConfig,
    ) -> None:
        """Monitor is created and started when reboot_time is set."""
        mock_monitor_instance = MagicMock()
        MockMonitor.return_value = mock_monitor_instance

        mock_client.get_version.return_value = "95.1.0.900"
        mock_client.is_service_running.return_value = True
        mock_client.get_msi_subject.return_value = "92.0.0.100"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=True, display_name="Netskope Client",
            display_version="92.0.0.100", install_location="C:\\fake",
        )

        runner = UpgradeRunner(
            webui=mock_webui,
            client=mock_client,
            upgrade_cfg=fast_cfg,
            host_name="test-host",
            email="test@gmail.com",
            reboot_time=5,
            reboot_delay=10,
        )
        runner.run_upgrade_to_latest()

        MockMonitor.assert_called_once_with(
            target_64_bit=False,
            reboot_time=5,
            reboot_delay=10,
        )
        mock_monitor_instance.start.assert_called_once()
        mock_monitor_instance.stop.assert_called_once()
        mock_monitor_instance.print_report.assert_called_once()

    def test_monitor_not_started_when_reboot_time_none(
        self,
        mock_webui: MagicMock,
        mock_client: MagicMock,
        fast_cfg: UpgradeConfig,
    ) -> None:
        """No monitor when reboot_time is None (default)."""
        mock_client.get_version.return_value = "95.1.0.900"
        mock_client.is_service_running.return_value = True
        mock_client.get_msi_subject.return_value = "92.0.0.100"
        mock_client.check_uninstall_registry.return_value = UninstallEntryResult(
            found=True, display_name="Netskope Client",
            display_version="92.0.0.100", install_location="C:\\fake",
        )

        runner = UpgradeRunner(
            webui=mock_webui,
            client=mock_client,
            upgrade_cfg=fast_cfg,
            host_name="test-host",
            email="test@gmail.com",
        )

        with patch("util_monitor.TimingMonitor") as MockMonitor:
            runner.run_upgrade_to_latest()
            MockMonitor.assert_not_called()
