"""
Unit tests for util_client.py — detect_tenant_from_nsconfig and service queries.
All I/O is mocked — no real nsconfig.json or NSClient needed.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from util_client import (
    LocalClient, NsConfigInfo, ServiceInfo, ExeValidationResult, UninstallEntryResult,
    INSTALL_DIR_32, INSTALL_DIR_64, REQUIRED_EXECUTABLES, WATCHDOG_EXECUTABLE,
)


# ── detect_tenant_from_nsconfig ────────────────────────────────────


class TestDetectTenantFromNsconfig:
    """Tests for reading tenant hostname and config name from nsconfig.json."""

    def test_strips_gateway_prefix(self, tmp_path: Path) -> None:
        """Strips 'gateway-' to derive tenant hostname."""
        cfg = {"nsgw": {"host": "gateway-acheng2.qa.boomskope.com"}}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        result = LocalClient.detect_tenant_from_nsconfig(nsconfig_path=config_file)
        assert result is not None
        assert result.tenant_hostname == "acheng2.qa.boomskope.com"

    def test_no_gateway_prefix(self, tmp_path: Path) -> None:
        """Returns hostname as-is when there is no gateway- prefix."""
        cfg = {"nsgw": {"host": "tenant.goskope.com"}}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        result = LocalClient.detect_tenant_from_nsconfig(nsconfig_path=config_file)
        assert result is not None
        assert result.tenant_hostname == "tenant.goskope.com"

    def test_extracts_config_name(self, tmp_path: Path) -> None:
        """Extracts clientConfig.configurationName from nsconfig."""
        cfg = {
            "nsgw": {"host": "gateway-acheng2.qa.boomskope.com"},
            "clientConfig": {
                "priority": "-1",
                "configurationName": "Default tenant config",
            },
        }
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        result = LocalClient.detect_tenant_from_nsconfig(nsconfig_path=config_file)
        assert result is not None
        assert result.tenant_hostname == "acheng2.qa.boomskope.com"
        assert result.config_name == "Default tenant config"

    def test_missing_config_name_defaults_empty(self, tmp_path: Path) -> None:
        """config_name is empty when clientConfig section is absent."""
        cfg = {"nsgw": {"host": "gateway-tenant.example.com"}}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        result = LocalClient.detect_tenant_from_nsconfig(nsconfig_path=config_file)
        assert result is not None
        assert result.config_name == ""

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """Returns None when nsconfig.json does not exist."""
        result = LocalClient.detect_tenant_from_nsconfig(
            nsconfig_path=tmp_path / "missing.json",
        )
        assert result is None

    def test_empty_host_returns_none(self, tmp_path: Path) -> None:
        """Returns None when nsgw.host is empty."""
        cfg = {"nsgw": {"host": ""}}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        result = LocalClient.detect_tenant_from_nsconfig(nsconfig_path=config_file)
        assert result is None

    def test_missing_nsgw_key_returns_none(self, tmp_path: Path) -> None:
        """Returns None when nsgw section is missing."""
        cfg = {"other": {"key": "value"}}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        result = LocalClient.detect_tenant_from_nsconfig(nsconfig_path=config_file)
        assert result is None

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        """Returns None when file contains invalid JSON."""
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text("not valid json {{{", encoding="utf-8")

        result = LocalClient.detect_tenant_from_nsconfig(nsconfig_path=config_file)
        assert result is None

    def test_missing_host_key_returns_none(self, tmp_path: Path) -> None:
        """Returns None when nsgw exists but host key is missing."""
        cfg = {"nsgw": {"port": 443}}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        result = LocalClient.detect_tenant_from_nsconfig(nsconfig_path=config_file)
        assert result is None


# ── query_service ───────────────────────────────────────────────────


SC_QUERY_RUNNING = (
    "SERVICE_NAME: stAgentSvc\n"
    "        TYPE               : 10  WIN32_OWN_PROCESS\n"
    "        STATE              : 4  RUNNING\n"
    "        WIN32_EXIT_CODE    : 0  (0x0)\n"
)

SC_QUERY_STOPPED = (
    "SERVICE_NAME: stAgentSvc\n"
    "        TYPE               : 10  WIN32_OWN_PROCESS\n"
    "        STATE              : 1  STOPPED\n"
    "        WIN32_EXIT_CODE    : 0  (0x0)\n"
)


class TestQueryService:
    """Tests for LocalClient.query_service static method."""

    @patch("util_client.subprocess.run")
    def test_running_service(self, mock_run: MagicMock) -> None:
        """Parses RUNNING state from sc query output."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SC_QUERY_RUNNING)

        info = LocalClient.query_service("stAgentSvc")

        assert info.exists is True
        assert info.state == "RUNNING"
        assert info.name == "stAgentSvc"

    @patch("util_client.subprocess.run")
    def test_stopped_service(self, mock_run: MagicMock) -> None:
        """Parses STOPPED state from sc query output."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SC_QUERY_STOPPED)

        info = LocalClient.query_service("stAgentSvc")

        assert info.exists is True
        assert info.state == "STOPPED"

    @patch("util_client.subprocess.run")
    def test_nonexistent_service(self, mock_run: MagicMock) -> None:
        """Non-zero return code means service does not exist."""
        mock_run.return_value = MagicMock(returncode=1060, stdout="")

        info = LocalClient.query_service("fakeservice")

        assert info.exists is False
        assert info.state == ""

    @patch("util_client.subprocess.run")
    def test_exception_returns_not_exists(self, mock_run: MagicMock) -> None:
        """Exception during sc query returns exists=False."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sc", timeout=10)

        info = LocalClient.query_service("stAgentSvc")

        assert info.exists is False


# ── query_service_binpath ───────────────────────────────────────────


SC_QC_OUTPUT = (
    "[SC] QueryServiceConfig SUCCESS\n"
    "SERVICE_NAME: stwatchdog\n"
    "        TYPE               : 10  WIN32_OWN_PROCESS\n"
    "        START_TYPE         : 2   AUTO_START\n"
    '        BINARY_PATH_NAME   : "C:\\Program Files (x86)\\Netskope\\STAgent\\stwatchdog.exe"\n'
    "        LOAD_ORDER_GROUP   :\n"
)


class TestQueryServiceBinpath:
    """Tests for LocalClient.query_service_binpath static method."""

    @patch("util_client.subprocess.run")
    def test_parses_binpath(self, mock_run: MagicMock) -> None:
        """Extracts BINARY_PATH_NAME value from sc qc output."""
        mock_run.return_value = MagicMock(returncode=0, stdout=SC_QC_OUTPUT)

        result = LocalClient.query_service_binpath("stwatchdog")

        assert "Netskope" in result
        assert "stwatchdog.exe" in result

    @patch("util_client.subprocess.run")
    def test_nonexistent_service_returns_empty(self, mock_run: MagicMock) -> None:
        """Non-zero return code returns empty string."""
        mock_run.return_value = MagicMock(returncode=1060, stdout="")

        result = LocalClient.query_service_binpath("fakeservice")

        assert result == ""

    @patch("util_client.subprocess.run")
    def test_exception_returns_empty(self, mock_run: MagicMock) -> None:
        """Exception during sc qc returns empty string."""
        mock_run.side_effect = OSError("Access denied")

        result = LocalClient.query_service_binpath("stwatchdog")

        assert result == ""


# ── Install dir helpers ─────────────────────────────────────────────


class TestInstallDirHelpers:
    """Tests for get_install_dir and verify_install_dir."""

    def test_get_install_dir_32bit(self) -> None:
        """32-bit returns Program Files (x86) path."""
        assert LocalClient.get_install_dir(False) == INSTALL_DIR_32

    def test_get_install_dir_64bit(self) -> None:
        """64-bit returns Program Files path."""
        assert LocalClient.get_install_dir(True) == INSTALL_DIR_64

    def test_verify_install_dir_exists(self, tmp_path: Path) -> None:
        """Returns True when install dir contains stAgentSvc.exe."""
        exe = tmp_path / "stAgentSvc.exe"
        exe.touch()

        with patch("util_client.INSTALL_DIR_64", tmp_path):
            assert LocalClient.verify_install_dir(True) is True

    def test_verify_install_dir_missing_exe(self, tmp_path: Path) -> None:
        """Returns False when stAgentSvc.exe is missing."""
        with patch("util_client.INSTALL_DIR_64", tmp_path):
            assert LocalClient.verify_install_dir(True) is False


# ── Watchdog mode detection ─────────────────────────────────────────


class TestIsWatchdogMode:
    """Tests for is_watchdog_mode reading nsconfig.json."""

    def test_watchdog_enabled(self, tmp_path: Path) -> None:
        """Returns True when nsclient_watchdog_monitor is true."""
        cfg = {"nsclient_watchdog_monitor": True}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        assert LocalClient.is_watchdog_mode(nsconfig_path=config_file) is True

    def test_watchdog_disabled(self, tmp_path: Path) -> None:
        """Returns False when nsclient_watchdog_monitor is false."""
        cfg = {"nsclient_watchdog_monitor": False}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        assert LocalClient.is_watchdog_mode(nsconfig_path=config_file) is False

    def test_watchdog_key_missing(self, tmp_path: Path) -> None:
        """Returns False when key is absent."""
        cfg = {"other": "value"}
        config_file = tmp_path / "nsconfig.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        assert LocalClient.is_watchdog_mode(nsconfig_path=config_file) is False

    def test_file_missing(self, tmp_path: Path) -> None:
        """Returns False when nsconfig.json does not exist."""
        assert LocalClient.is_watchdog_mode(nsconfig_path=tmp_path / "missing.json") is False


# ── Executable validation ───────────────────────────────────────────


class TestVerifyExecutables:
    """Tests for verify_executables — checks exe presence and version."""

    def test_all_present_correct_version(self, tmp_path: Path) -> None:
        """All required executables present with correct version."""
        for exe in REQUIRED_EXECUTABLES:
            (tmp_path / exe).touch()

        nsconfig = tmp_path / "nsconfig.json"
        nsconfig.write_text('{"nsclient_watchdog_monitor": false}', encoding="utf-8")

        with patch("util_client.INSTALL_DIR_32", tmp_path), \
             patch.object(LocalClient, "get_file_version", return_value="95.1.0.900"):
            result = LocalClient.verify_executables(
                is_64_bit=False, expected_version="95.1.0.900",
                nsconfig_path=nsconfig,
            )

        assert result.valid is True
        assert len(result.missing) == 0
        assert len(result.version_mismatches) == 0

    def test_missing_executable(self, tmp_path: Path) -> None:
        """Reports missing when stAgentUI.exe is absent."""
        (tmp_path / "stAgentSvc.exe").touch()
        # stAgentUI.exe not created

        nsconfig = tmp_path / "nsconfig.json"
        nsconfig.write_text('{"nsclient_watchdog_monitor": false}', encoding="utf-8")

        with patch("util_client.INSTALL_DIR_64", tmp_path):
            result = LocalClient.verify_executables(
                is_64_bit=True, expected_version="95.1.0.900",
                nsconfig_path=nsconfig,
            )

        assert result.valid is False
        assert "stAgentUI.exe" in result.missing

    def test_watchdog_mode_checks_svcmon(self, tmp_path: Path) -> None:
        """In watchdog mode, stAgentSvcMon.exe is also required."""
        for exe in REQUIRED_EXECUTABLES:
            (tmp_path / exe).touch()
        # stAgentSvcMon.exe not created

        nsconfig = tmp_path / "nsconfig.json"
        nsconfig.write_text('{"nsclient_watchdog_monitor": true}', encoding="utf-8")

        with patch("util_client.INSTALL_DIR_32", tmp_path), \
             patch.object(LocalClient, "get_file_version", return_value="95.1.0.900"):
            result = LocalClient.verify_executables(
                is_64_bit=False, expected_version="95.1.0.900",
                nsconfig_path=nsconfig,
            )

        assert result.valid is False
        assert WATCHDOG_EXECUTABLE in result.missing

    def test_watchdog_mode_all_present(self, tmp_path: Path) -> None:
        """In watchdog mode, passes when all 3 executables present."""
        for exe in REQUIRED_EXECUTABLES:
            (tmp_path / exe).touch()
        (tmp_path / WATCHDOG_EXECUTABLE).touch()

        nsconfig = tmp_path / "nsconfig.json"
        nsconfig.write_text('{"nsclient_watchdog_monitor": true}', encoding="utf-8")

        with patch("util_client.INSTALL_DIR_32", tmp_path), \
             patch.object(LocalClient, "get_file_version", return_value="95.1.0.900"):
            result = LocalClient.verify_executables(
                is_64_bit=False, expected_version="95.1.0.900",
                nsconfig_path=nsconfig,
            )

        assert result.valid is True
        assert WATCHDOG_EXECUTABLE in result.present

    def test_version_mismatch(self, tmp_path: Path) -> None:
        """Reports version mismatch when exe has wrong version."""
        for exe in REQUIRED_EXECUTABLES:
            (tmp_path / exe).touch()

        nsconfig = tmp_path / "nsconfig.json"
        nsconfig.write_text('{"nsclient_watchdog_monitor": false}', encoding="utf-8")

        with patch("util_client.INSTALL_DIR_32", tmp_path), \
             patch.object(LocalClient, "get_file_version", return_value="92.0.0.100"):
            result = LocalClient.verify_executables(
                is_64_bit=False, expected_version="95.1.0.900",
                nsconfig_path=nsconfig,
            )

        assert result.valid is False
        assert len(result.version_mismatches) > 0


# ── Uninstall registry entry ───────────────────────────────────────


class TestCheckUninstallRegistry:
    """Tests for check_uninstall_registry — mocked winreg."""

    def test_entry_found(self) -> None:
        """Returns found=True when Netskope Client entry exists."""
        import winreg

        mock_subkey = MagicMock()
        mock_subkey.__enter__ = MagicMock(return_value=mock_subkey)
        mock_subkey.__exit__ = MagicMock(return_value=False)

        def query_value(key: MagicMock, name: str) -> tuple:
            values = {
                "DisplayName": ("Netskope Client", 1),
                "DisplayVersion": ("95.1.0.900", 1),
                "InstallLocation": (r"C:\Program Files (x86)\Netskope\STAgent", 1),
            }
            if name in values:
                return values[name]
            raise FileNotFoundError

        mock_parent = MagicMock()
        mock_parent.__enter__ = MagicMock(return_value=mock_parent)
        mock_parent.__exit__ = MagicMock(return_value=False)

        def open_key(hkey: int, path: str) -> MagicMock:
            return mock_parent

        with patch("winreg.OpenKey", side_effect=[mock_parent, mock_subkey]), \
             patch("winreg.EnumKey", side_effect=["NetskopeClient", OSError]), \
             patch("winreg.QueryValueEx", side_effect=query_value):
            result = LocalClient.check_uninstall_registry()

        assert result.found is True
        assert "Netskope" in result.display_name
        assert result.display_version == "95.1.0.900"

    def test_entry_not_found(self) -> None:
        """Returns found=False when no matching entry exists."""
        mock_parent = MagicMock()
        mock_parent.__enter__ = MagicMock(return_value=mock_parent)
        mock_parent.__exit__ = MagicMock(return_value=False)

        with patch("winreg.OpenKey", return_value=mock_parent), \
             patch("winreg.EnumKey", side_effect=OSError):
            result = LocalClient.check_uninstall_registry()

        assert result.found is False


# ── Task Scheduler for Reboot-Verify ───────────────────────────────


class TestCreateVerifyTask:
    """Tests for create_verify_task / delete_verify_task."""

    @patch("util_client.subprocess.run")
    def test_creates_bat_and_scheduled_task(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        """Writes .bat file and calls schtasks /create."""
        mock_run.return_value = MagicMock(returncode=0, stdout="SUCCESS")
        bat = tmp_path / "reboot_verify.bat"

        LocalClient.create_verify_task(bat_path=bat, task_name="TestTask")

        assert bat.is_file()
        content = bat.read_text(encoding="utf-8")
        assert "main.py reboot-verify" in content
        assert "pause" in content

        # schtasks was called with ONLOGON trigger and 30s delay
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "schtasks" in cmd
        assert "ONLOGON" in cmd
        assert "0000:30" in cmd
        assert "TestTask" in cmd

    @patch("util_client.subprocess.run")
    def test_create_raises_on_schtasks_failure(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        """Raises RuntimeError when schtasks fails."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr="Access denied",
        )
        bat = tmp_path / "reboot_verify.bat"

        with pytest.raises(RuntimeError, match="Access denied"):
            LocalClient.create_verify_task(bat_path=bat, task_name="TestTask")

    @patch("util_client.subprocess.run")
    def test_delete_removes_task_and_bat(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        """Deletes scheduled task and batch file."""
        mock_run.return_value = MagicMock(returncode=0)
        bat = tmp_path / "reboot_verify.bat"
        bat.write_text("@echo test", encoding="utf-8")

        LocalClient.delete_verify_task(bat_path=bat, task_name="TestTask")

        assert not bat.is_file()
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "schtasks" in cmd
        assert "/delete" in cmd
        assert "TestTask" in cmd

    @patch("util_client.subprocess.run")
    def test_delete_no_error_when_missing(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        """No error when task and bat don't exist."""
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")

        LocalClient.delete_verify_task(
            bat_path=tmp_path / "missing.bat",
            task_name="TestTask",
        )
