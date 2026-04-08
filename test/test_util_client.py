"""
Unit tests for util_client.py — detect_tenant_from_nsconfig.
All I/O is mocked — no real nsconfig.json or NSClient needed.
"""

import json
import sys
from pathlib import Path

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from util_client import LocalClient, NsConfigInfo


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
