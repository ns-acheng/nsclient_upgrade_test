"""
Unit tests for util_webui.py.
All external webapi calls are mocked — no network or tenant needed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from util_webui import WebUIClient


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_webapi_module() -> MagicMock:
    """Create a mock webapi module with all sub-modules wired up."""
    mock_webapi_cls = MagicMock(name="WebAPI")
    mock_auth_cls = MagicMock(name="Authentication")
    mock_client_config_cls = MagicMock(name="ClientConfiguration")
    mock_devices_cls = MagicMock(name="Devices")
    mock_users_cls = MagicMock(name="Users")

    with patch.dict(sys.modules, {
        "webapi": MagicMock(WebAPI=mock_webapi_cls),
        "webapi.auth": MagicMock(),
        "webapi.auth.authentication": MagicMock(Authentication=mock_auth_cls),
        "webapi.settings": MagicMock(),
        "webapi.settings.security_cloud_platform": MagicMock(),
        "webapi.settings.security_cloud_platform.netskope_client": MagicMock(),
        "webapi.settings.security_cloud_platform.netskope_client.client_configuration": MagicMock(
            ClientConfiguration=mock_client_config_cls,
        ),
        "webapi.settings.security_cloud_platform.netskope_client.devices": MagicMock(
            Devices=mock_devices_cls,
        ),
        "webapi.settings.security_cloud_platform.netskope_client.users": MagicMock(
            Users=mock_users_cls,
        ),
    }):
        yield {
            "WebAPI": mock_webapi_cls,
            "Authentication": mock_auth_cls,
            "ClientConfiguration": mock_client_config_cls,
            "Devices": mock_devices_cls,
            "Users": mock_users_cls,
        }


@pytest.fixture
def connected_client(mock_webapi_module: dict) -> tuple[WebUIClient, dict]:
    """Return a WebUIClient that has already called connect()."""
    client = WebUIClient()
    client.connect("tenant.example.com", "admin@example.com", "secret")
    return client, mock_webapi_module


# ── Connection ───────────────────────────────────────────────────────


class TestConnect:
    """Tests for connect() and is_connected."""

    def test_not_connected_initially(self) -> None:
        """New client is not connected."""
        client = WebUIClient()
        assert client.is_connected is False

    def test_connect_raises_when_webapi_missing(self) -> None:
        """connect() raises RuntimeError with install instructions if webapi is missing."""
        client = WebUIClient()
        with patch.dict(sys.modules, {"webapi": None}):
            with pytest.raises(RuntimeError, match="pylark-webapi-lib is not installed"):
                client.connect("host", "user", "pass")

    def test_connect_authenticates(self, mock_webapi_module: dict) -> None:
        """connect() creates a WebAPI instance and calls login()."""
        client = WebUIClient()
        client.connect("tenant.example.com", "admin@example.com", "secret")

        mock_webapi_module["WebAPI"].assert_called_once_with(
            hostname="tenant.example.com",
            username="admin@example.com",
            password="secret",
        )
        mock_webapi_module["Authentication"].return_value.login.assert_called_once()
        assert client.is_connected is True

    def test_connect_raises_on_auth_failure(self, mock_webapi_module: dict) -> None:
        """connect() propagates auth exceptions and resets state."""
        mock_webapi_module["Authentication"].return_value.login.side_effect = (
            Exception("Tenant authentication failed, invalid username or password")
        )
        client = WebUIClient()
        with pytest.raises(Exception, match="invalid username or password"):
            client.connect("host", "user", "bad_pass")
        assert client.is_connected is False

    @patch("util_webui.LOGIN_TIMEOUT_SECONDS", 1)
    def test_connect_raises_on_timeout(self, mock_webapi_module: dict) -> None:
        """connect() raises TimeoutError if login hangs."""
        import time

        def hang_login():
            time.sleep(5)

        mock_webapi_module["Authentication"].return_value.login.side_effect = hang_login
        client = WebUIClient()
        with pytest.raises(TimeoutError, match="Login timed out"):
            client.connect("host", "user", "pass")
        assert client.is_connected is False

    def test_connect_initializes_page_objects(self, mock_webapi_module: dict) -> None:
        """connect() initializes ClientConfiguration and Devices."""
        client = WebUIClient()
        client.connect("tenant.example.com", "admin@example.com", "secret")

        webapi_instance = mock_webapi_module["WebAPI"].return_value
        mock_webapi_module["ClientConfiguration"].assert_called_once_with(
            webapi=webapi_instance,
        )
        mock_webapi_module["Devices"].assert_called_once_with(
            webapi=webapi_instance,
        )


# ── Ensure Connected Guard ───────────────────────────────────────────


class TestEnsureConnected:
    """Tests for the _ensure_connected guard."""

    def test_raises_when_not_connected(self) -> None:
        """Methods that need a connection raise RuntimeError."""
        client = WebUIClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            client.get_release_versions()

    def test_raises_for_get_client_config(self) -> None:
        """get_client_config raises when not connected."""
        client = WebUIClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            client.get_client_config()

    def test_raises_for_get_device_version(self) -> None:
        """get_device_version raises when not connected."""
        client = WebUIClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            client.get_device_version(host_name="host", email="a@b.com")


# ── Release Versions ─────────────────────────────────────────────────


class TestReleaseVersions:
    """Tests for get_release_versions and get_sorted_version_list."""

    def test_get_release_versions(self, connected_client: tuple) -> None:
        """Returns the 'data' key from the API response."""
        client, mocks = connected_client
        config_instance = mocks["ClientConfiguration"].return_value
        config_instance.get_client_release_versions.return_value = {
            "data": {
                "latestversion": "95.1.0.900",
                "goldenversions": ["90.0.0"],
                "90.0.0": ["90.0.0.100"],
            }
        }

        result = client.get_release_versions()

        assert result["latestversion"] == "95.1.0.900"
        assert "90.0.0" in result["goldenversions"]
        config_instance.get_client_release_versions.assert_called_once()

    def test_get_sorted_version_list(self, connected_client: tuple) -> None:
        """Returns sorted major versions, excluding metadata keys."""
        client, mocks = connected_client
        config_instance = mocks["ClientConfiguration"].return_value
        config_instance.get_client_release_versions.return_value = {
            "data": {
                "latestversion": "95.1.0.900",
                "goldenversions": ["90.0.0"],
                "90.0.0": ["90.0.0.100"],
                "84.0.0": ["84.0.0.100"],
                "92.0.0": ["92.0.0.100"],
            }
        }

        result = client.get_sorted_version_list()

        assert result == ["84.0.0", "90.0.0", "92.0.0"]


# ── Client Configuration Updates ─────────────────────────────────────


class TestClientConfig:
    """Tests for config read/update methods."""

    def test_disable_auto_upgrade(self, connected_client: tuple) -> None:
        """disable_auto_upgrade sends correct parameters."""
        client, mocks = connected_client
        config_instance = mocks["ClientConfiguration"].return_value
        config_instance.update_client_config.return_value = {"status": "success"}

        client.disable_auto_upgrade()

        config_instance.update_client_config.assert_called_once_with(
            search_config="",
            clientAllowAutoUpdate=0,
            allowAutoGoldenUpdate=0,
            goldenReleaseVersion="",
            goldenDotReleaseUpdate=0,
        )

    def test_enable_upgrade_latest(self, connected_client: tuple) -> None:
        """enable_upgrade_latest enables clientAllowAutoUpdate."""
        client, mocks = connected_client
        config_instance = mocks["ClientConfiguration"].return_value
        config_instance.update_client_config.return_value = {"status": "success"}

        client.enable_upgrade_latest()

        config_instance.update_client_config.assert_called_once_with(
            search_config="",
            clientAllowAutoUpdate=1,
        )

    def test_enable_upgrade_golden_without_dot(self, connected_client: tuple) -> None:
        """enable_upgrade_golden sets golden version without dot release."""
        client, mocks = connected_client
        config_instance = mocks["ClientConfiguration"].return_value
        config_instance.update_client_config.return_value = {"status": "success"}

        client.enable_upgrade_golden("90.0.0", dot=False)

        config_instance.update_client_config.assert_called_once_with(
            search_config="",
            clientAllowAutoUpdate=1,
            allowAutoGoldenUpdate=1,
            goldenReleaseVersion="90.0.0",
            goldenDotReleaseUpdate=0,
        )

    def test_enable_upgrade_golden_with_dot(self, connected_client: tuple) -> None:
        """enable_upgrade_golden with dot=True enables dot release updates."""
        client, mocks = connected_client
        config_instance = mocks["ClientConfiguration"].return_value
        config_instance.update_client_config.return_value = {"status": "success"}

        client.enable_upgrade_golden("90.0.0", dot=True)

        config_instance.update_client_config.assert_called_once_with(
            search_config="",
            clientAllowAutoUpdate=1,
            allowAutoGoldenUpdate=1,
            goldenReleaseVersion="90.0.0",
            goldenDotReleaseUpdate=1,
        )

    def test_get_client_config(self, connected_client: tuple) -> None:
        """get_client_config passes search_config to the API."""
        client, mocks = connected_client
        config_instance = mocks["ClientConfiguration"].return_value
        config_instance.get_client_config.return_value = {"data": {"key": "val"}}

        result = client.get_client_config(search_config="my_config")

        config_instance.get_client_config.assert_called_once_with(
            search_config="my_config",
        )
        assert result == {"data": {"key": "val"}}

    def test_update_client_config_with_kwargs(self, connected_client: tuple) -> None:
        """update_client_config forwards arbitrary kwargs."""
        client, mocks = connected_client
        config_instance = mocks["ClientConfiguration"].return_value
        config_instance.update_client_config.return_value = {"status": "success"}

        client.update_client_config(search_config="", customKey=42)

        config_instance.update_client_config.assert_called_once_with(
            search_config="",
            customKey=42,
        )


# ── Device Queries ───────────────────────────────────────────────────


class TestDeviceQueries:
    """Tests for device version queries."""

    def test_get_device_version(self, connected_client: tuple) -> None:
        """get_device_version returns the version from the API."""
        client, mocks = connected_client
        devices_instance = mocks["Devices"].return_value
        devices_instance.get_device_client_version.return_value = "95.1.0.900"

        result = client.get_device_version(host_name="test-host", email="a@b.com")

        assert result == "95.1.0.900"
        devices_instance.get_device_client_version.assert_called_once_with(
            host_name="test-host",
            email="a@b.com",
        )


# ── Email Invite ─────────────────────────────────────────────────────


class TestEmailInvite:
    """Tests for send_email_invite."""

    def test_send_email_invite(self, connected_client: tuple) -> None:
        """send_email_invite calls create_user with send_invite=True."""
        client, mocks = connected_client
        users_instance = mocks["Users"].return_value
        users_instance.create_user.return_value = {"status": "success"}

        client.send_email_invite("user@example.com")

        users_instance.create_user.assert_called_once_with(
            email="user@example.com",
            send_invite=True,
            warn_duplicate=False,
        )

    def test_send_email_invite_raises_when_not_connected(self) -> None:
        """send_email_invite raises when not connected."""
        client = WebUIClient()
        with pytest.raises(RuntimeError, match="Not connected"):
            client.send_email_invite("user@example.com")
