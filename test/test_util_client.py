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

from util_client import LocalClient, NsConfigInfo, ServiceInfo, INSTALL_DIR_32, INSTALL_DIR_64


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
