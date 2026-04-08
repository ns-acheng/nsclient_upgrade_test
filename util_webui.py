"""
WebUI API wrapper for the Netskope Client Upgrade Tool.
Thin facade over pylark-webapi-lib for upgrade-related operations.

External imports are deferred to connect() so the module can be
imported without webapi installed (e.g. during testing or --help).
"""

import logging
import threading
from typing import Any, Optional

LOGIN_TIMEOUT_SECONDS = 60

log = logging.getLogger(__name__)


class WebUIClient:
    """
    Manages WebUI API operations for client upgrade configuration.

    Wraps authentication, client configuration, device queries,
    and release version lookups.
    """

    def __init__(self) -> None:
        self._webapi: Optional[Any] = None
        self._client_config: Optional[Any] = None
        self._devices: Optional[Any] = None
        self._users: Optional[Any] = None

    @property
    def is_connected(self) -> bool:
        """Check if authenticated session exists."""
        return self._webapi is not None

    def connect(self, hostname: str, username: str, password: str) -> None:
        """
        Authenticate to the tenant WebUI.

        :param hostname: Tenant hostname (e.g. 'tenant.goskope.com').
        :param username: Admin username.
        :param password: Admin password.
        """
        try:
            from webapi import WebAPI
            from webapi.auth.authentication import Authentication
            from webapi.settings.security_cloud_platform.netskope_client.client_configuration import (
                ClientConfiguration,
            )
            from webapi.settings.security_cloud_platform.netskope_client.devices import Devices
            from webapi.settings.security_cloud_platform.netskope_client.users import Users
        except ModuleNotFoundError:
            raise RuntimeError(
                "pylark-webapi-lib is not installed. "
                "Install it with: pip install -e /path/to/pylark-webapi-lib"
            )

        log.info("Connecting to tenant: %s as %s", hostname, username)
        self._webapi = WebAPI(hostname=hostname, username=username, password=password)
        auth = Authentication(self._webapi)

        # Run login in a thread with a timeout so a bad password can't hang
        login_error: list[BaseException] = []

        def _do_login() -> None:
            try:
                auth.login()
            except Exception as exc:
                login_error.append(exc)

        thread = threading.Thread(target=_do_login, daemon=True)
        thread.start()
        thread.join(timeout=LOGIN_TIMEOUT_SECONDS)

        if thread.is_alive():
            self._webapi = None
            raise TimeoutError(
                f"Login timed out after {LOGIN_TIMEOUT_SECONDS}s — "
                "invalid username or password"
            )
        if login_error:
            self._webapi = None
            raise login_error[0]

        log.info("Successfully authenticated to %s", hostname)

        # Initialize page objects
        self._client_config = ClientConfiguration(webapi=self._webapi)
        self._devices = Devices(webapi=self._webapi)
        self._users = Users(webapi=self._webapi)

    def _ensure_connected(self) -> None:
        """Raise if not connected."""
        if not self.is_connected:
            raise RuntimeError("Not connected to WebUI. Call connect() first.")

    # ── Release Versions ─────────────────────────────────────────────

    def get_release_versions(self) -> dict[str, Any]:
        """
        Fetch all available client release versions from the tenant.

        :return: Dict with keys like 'latestversion', 'goldenversions',
                 and major version keys (e.g. '92.0.0') mapping to dot releases.
        """
        self._ensure_connected()
        response = self._client_config.get_client_release_versions()
        versions = response["data"]
        log.info(
            "Fetched release versions — latest: %s, golden: %s",
            versions.get("latestversion", "N/A"),
            versions.get("goldenversions", []),
        )
        return versions

    def get_sorted_version_list(self) -> list[str]:
        """
        Get sorted list of all major release version strings.

        :return: Sorted list like ['72.0.0', '80.0.0', '84.0.0', ...].
        """
        versions = self.get_release_versions()
        version_list = [
            key for key in sorted(versions)
            if key not in ("goldenversions", "latestversion")
        ]
        return version_list

    # ── Client Configuration ─────────────────────────────────────────

    def get_client_config(self, search_config: str = "") -> dict[str, Any]:
        """
        Read current client configuration from WebUI.

        :param search_config: Optional search filter.
        :return: Configuration response dict.
        """
        self._ensure_connected()
        return self._client_config.get_client_config(search_config=search_config)

    def update_client_config(self, search_config: str = "", **kwargs: Any) -> dict[str, Any]:
        """
        Update client configuration with arbitrary settings.

        :param search_config: Config name to update (empty for default).
        :param kwargs: Key-value pairs to set.
        :return: API response dict.
        """
        self._ensure_connected()
        log.info("Updating client config: %s", kwargs)
        return self._client_config.update_client_config(search_config=search_config, **kwargs)

    def disable_auto_upgrade(self, search_config: str = "") -> dict[str, Any]:
        """
        Disable all auto-upgrade settings on the tenant.

        :param search_config: Config name (empty for default).
        :return: API response dict.
        """
        log.info("Disabling auto-upgrade on tenant")
        return self.update_client_config(
            search_config=search_config,
            clientAllowAutoUpdate=0,
            allowAutoGoldenUpdate=0,
            goldenReleaseVersion="",
            goldenDotReleaseUpdate=0,
        )

    def enable_upgrade_latest(self, search_config: str = "") -> dict[str, Any]:
        """
        Enable auto-upgrade to the latest release.

        :param search_config: Config name (empty for default).
        :return: API response dict.
        """
        log.info("Enabling auto-upgrade to LATEST release")
        return self.update_client_config(
            search_config=search_config,
            clientAllowAutoUpdate=1,
        )

    def enable_upgrade_golden(
        self,
        golden_version: str,
        dot: bool = False,
        search_config: str = "",
    ) -> dict[str, Any]:
        """
        Enable auto-upgrade to a specific golden release.

        :param golden_version: Target golden version string (e.g. '90.0.0').
        :param dot: If True, enable dot release updates within the golden.
        :param search_config: Config name (empty for default).
        :return: API response dict.
        """
        log.info(
            "Enabling auto-upgrade to GOLDEN release %s (dot=%s)",
            golden_version, dot,
        )
        return self.update_client_config(
            search_config=search_config,
            clientAllowAutoUpdate=1,
            allowAutoGoldenUpdate=1,
            goldenReleaseVersion=golden_version,
            goldenDotReleaseUpdate=1 if dot else 0,
        )

    # ── Email Invite ─────────────────────────────────────────────────

    def send_email_invite(self, email: str) -> dict[str, Any]:
        """
        Create the user (if needed) and send an email invite with
        client download links.

        :param email: User email address.
        :return: API response dict.
        """
        self._ensure_connected()
        log.info("Sending email invite to %s", email)
        response = self._users.create_user(
            email=email, send_invite=True, warn_duplicate=False,
        )
        log.info("Email invite sent to %s", email)
        return response

    # ── Device Queries ───────────────────────────────────────────────

    def get_device_version(self, host_name: str, email: str) -> str:
        """
        Get the client version reported by WebUI for a specific device.

        :param host_name: Device hostname.
        :param email: User email.
        :return: Version string as shown in WebUI.
        """
        self._ensure_connected()
        version = self._devices.get_device_client_version(
            host_name=host_name, email=email,
        )
        log.info("WebUI reports device version: %s (host=%s)", version, host_name)
        return version
