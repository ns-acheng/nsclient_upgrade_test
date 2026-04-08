"""
Local client wrapper for the Netskope Client Upgrade Tool.
Thin facade over the nsclient library for local client operations.

External imports are deferred to create() so the module can be
imported without nsclient installed (e.g. during testing or --help).
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass
class NsConfigInfo:
    """Information extracted from a local NSClient nsconfig.json."""
    tenant_hostname: str
    config_name: str


class LocalClient:
    """
    Manages local Netskope Client operations via the nsclient library.

    Wraps install, uninstall, version checks, config updates,
    and build downloads.
    """

    def __init__(self, platform: str = "windows") -> None:
        self._client: Optional[Any] = None
        self._platform: str = platform
        self._email: str = ""

    NSCONFIG_PATH = Path(r"C:\ProgramData\netskope\stagent\nsconfig.json")

    @staticmethod
    def detect_tenant_from_nsconfig(
        nsconfig_path: Path | None = None,
    ) -> NsConfigInfo | None:
        """
        Detect tenant hostname and client config name from a local
        NSClient installation.

        Reads nsconfig.json, extracts ``nsgw.host`` (strips ``gateway-``
        prefix) and ``clientConfig.configurationName``.

        :param nsconfig_path: Override path to nsconfig.json (for testing).
        :return: NsConfigInfo with tenant_hostname and config_name,
                 or None if nsconfig.json is missing / unreadable.
        """
        path = nsconfig_path or LocalClient.NSCONFIG_PATH
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
            gateway_host: str = config.get("nsgw", {}).get("host", "")
            if not gateway_host:
                return None
            hostname = (
                gateway_host[len("gateway-"):]
                if gateway_host.startswith("gateway-")
                else gateway_host
            )
            config_name: str = (
                config.get("clientConfig", {}).get("configurationName", "")
            )
            return NsConfigInfo(
                tenant_hostname=hostname,
                config_name=config_name,
            )
        except Exception as exc:
            log.warning("Failed to read nsconfig.json: %s", exc)
            return None

    @property
    def is_initialized(self) -> bool:
        """Check if client instance has been created."""
        return self._client is not None

    @property
    def email(self) -> str:
        """Return the configured email address."""
        return self._email

    @property
    def platform(self) -> str:
        """Return the configured platform."""
        return self._platform

    def create(
        self,
        platform: str,
        email: str,
        password: str,
        stack: Any,
        tenant_name: str,
        is_64_bit: bool = False,
    ) -> None:
        """
        Create the nsclient instance.

        :param platform: Platform string ('windows', 'mac', 'linux').
        :param email: User email for enrollment.
        :param password: Gmail password for email downloads.
        :param stack: Stack configuration object.
        :param tenant_name: Tenant name string.
        :param is_64_bit: Whether to use 64-bit client installer.
        """
        from nsclient.nsclient import get_nsclient_instance

        self._platform = platform
        self._email = email
        log.info("Creating nsclient instance — platform=%s, email=%s", platform, email)
        self._client = get_nsclient_instance(
            is_64_bit=is_64_bit,
            platform=platform,
            email=email,
            password=password,
            stack=stack,
            tenant_name=tenant_name,
        )
        log.info("nsclient instance created successfully")

    def _ensure_initialized(self) -> None:
        """Raise if client not initialized."""
        if not self.is_initialized:
            raise RuntimeError("Client not initialized. Call create() first.")

    # ── Version ──────────────────────────────────────────────────────

    def get_version(self) -> str:
        """
        Get the currently installed client version.

        :return: Version string (e.g. '92.1.0.805').
        """
        self._ensure_initialized()
        version = self._client.get_installed_version()
        log.debug("Local client version: %s", version)
        return version

    def is_installed(self) -> bool:
        """
        Check if Netskope Client is currently installed.

        :return: True if installed.
        """
        self._ensure_initialized()
        return self._client.assert_installation()

    # ── Install / Uninstall ──────────────────────────────────────────

    def install(self, setup_file_path: str) -> None:
        """
        Install the client from a local installer file.

        :param setup_file_path: Full path to the installer (MSI/PKG/RUN).
        """
        self._ensure_initialized()
        log.info("Installing client from: %s", setup_file_path)
        self._client.install(setup_file_path=setup_file_path)
        log.info("Client installation completed")

    def install_msi(self, setup_file_path: str) -> None:
        """
        Install the client using msiexec silent install (Windows).

        :param setup_file_path: Full path to the MSI installer.
        """
        log.info("Installing via msiexec: %s", setup_file_path)
        result = subprocess.run(
            ["msiexec", "/i", setup_file_path, "/q"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"msiexec failed (exit code {result.returncode}): {result.stderr}"
            )
        log.info("msiexec install completed")

    def uninstall(self) -> None:
        """Uninstall the currently installed client."""
        self._ensure_initialized()
        log.info("Uninstalling client")
        self._client.uninstall()
        log.info("Client uninstalled")

    # ── Service ─────────────────────────────────────────────────────

    @staticmethod
    def is_service_running(service_name: str = "stAgentSvc") -> bool:
        """
        Check if a Windows service is running via sc query.

        :param service_name: Windows service name.
        :return: True if service state is RUNNING.
        """
        try:
            result = subprocess.run(
                ["sc", "query", service_name],
                capture_output=True, text=True, timeout=10,
            )
            return "RUNNING" in result.stdout
        except Exception:
            return False

    @staticmethod
    def wait_for_service(
        service_name: str = "stAgentSvc",
        timeout: int = 60,
        interval: int = 5,
    ) -> bool:
        """
        Poll until a Windows service is running.

        :param service_name: Windows service name.
        :param timeout: Max seconds to wait.
        :param interval: Seconds between checks.
        :return: True if service started, False if timed out.
        """
        start = time.time()
        while time.time() - start < timeout:
            if LocalClient.is_service_running(service_name):
                log.info("Service %s is running", service_name)
                return True
            log.debug("Waiting for service %s...", service_name)
            time.sleep(interval)
        log.warning("Service %s not running after %ds", service_name, timeout)
        return False

    # ── Config / Restart ─────────────────────────────────────────────

    def update_config(self, wait_seconds: float = 15, retries: int = 3) -> None:
        """
        Pull new configuration from the cloud, with retry logic.

        :param wait_seconds: Seconds to wait after config update.
        :param retries: Number of retry attempts.
        """
        self._ensure_initialized()
        log.info("Updating client config (wait=%ss, retries=%d)", wait_seconds, retries)
        for attempt in range(1, retries + 1):
            try:
                self._client.update_config()
                time.sleep(wait_seconds)
                log.info("Client config updated successfully (attempt %d)", attempt)
                return
            except Exception as exc:
                log.warning(
                    "Config update attempt %d/%d failed: %s",
                    attempt, retries, exc,
                )
                if attempt < retries:
                    time.sleep(5)
                else:
                    raise

    def restart(self, service_only: bool = False) -> None:
        """
        Restart the Netskope Client.

        :param service_only: If True, restart only the service (not the UI).
        """
        self._ensure_initialized()
        log.info("Restarting client (service_only=%s)", service_only)
        self._client.restart_client(service_only=service_only)
        log.info("Client restarted")

    # ── Build Downloads ──────────────────────────────────────────────

    def get_installer_filename(self, is_64_bit: bool = False) -> str:
        """
        Get the platform-appropriate installer filename.

        :param is_64_bit: Whether to use 64-bit installer on Windows.
        :return: Installer filename string.
        """
        if self._platform == "mac":
            return "STAgent.pkg"
        elif self._platform == "linux":
            return "STAgent.run"
        elif is_64_bit:
            return "STAgent64.msi"
        else:
            return "STAgent.msi"

    def download_build(
        self,
        build_version: str,
        installer_filename: str,
        client_installer_file: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Download a specific client build from the build server.

        :param build_version: Full build version (e.g. 'release-123.0.0').
        :param installer_filename: Installer filename (e.g. 'STAgent.msi').
        :param client_installer_file: Target local filename for the installer.
        :return: Dict with 'location' key pointing to downloaded file path.
        """
        self._ensure_initialized()
        target_file = client_installer_file or installer_filename
        log.info(
            "Downloading build: %s (filename=%s, target=%s)",
            build_version, installer_filename, target_file,
        )
        info = self._client.download_client_from_build_server(
            full_build_version=build_version,
            filename=installer_filename,
            client_installer_file=target_file,
        )
        log.info("Build downloaded to: %s", info.get("location", "unknown"))
        return info

    # ── Status ───────────────────────────────────────────────────────

    def get_status(self) -> str:
        """
        Get the current client status.

        :return: Status string (e.g. 'enabled', 'disabled').
        """
        self._ensure_initialized()
        return self._client.get_status()
