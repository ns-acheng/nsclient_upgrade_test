"""
Unit tests for upgrade_runner.py.
All I/O is mocked — no network, no local client, no tenant needed.
"""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from upgrade_runner import (
    UpgradeRunner, UpgradeResult, PollResult, BASE_VERSION_DIR, INSTALLER_JSON,
)
from util_config import UpgradeConfig


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
    webui.set_upgrade_schedule.return_value = {"status": "success"}
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
        mock_client.get_version.side_effect = ["92.0.0.100", "92.0.0.100", "95.1.0.900"]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        runner.run_upgrade_to_latest(from_version="92.0.0")

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

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

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
        mock_client.get_version.side_effect = ["87.0.0.100", "87.0.0.100", "90.0.0.100"]
        mock_webui.get_device_version.return_value = "90.0.0.100"

        result = runner.run_upgrade_to_golden(from_version=None, dot=False)

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
            is_64_bit=True,
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
        # Tenant-specific copy was created
        assert (tmp_path / expected_name).exists()
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
    def test_service_down_after_upgrade_still_reports_result(
        self,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
        runner: UpgradeRunner,
        mock_client: MagicMock,
        mock_webui: MagicMock,
    ) -> None:
        """Service down after upgrade logs warning but does not fail the result."""
        mock_time.side_effect = [0, 0.1, 0.2, 0.3, 0.4, 0.5]
        mock_client.is_service_running.return_value = False
        mock_client.get_version.side_effect = [
            "92.0.0.100", "92.0.0.100", "95.1.0.900",
        ]
        mock_webui.get_device_version.return_value = "95.1.0.900"

        result = runner.run_upgrade_to_latest(from_version="92.0.0")

        # Upgrade itself succeeded — service check is informational
        assert result.success is True
        assert result.version_after == "95.1.0.900"
