"""
Local client wrapper for the Netskope Client Upgrade Tool.
Thin facade over the nsclient library for local client operations.

External imports are deferred to create() so the module can be
imported without nsclient installed (e.g. during testing or --help).
"""

import ctypes
import glob
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_SEEN_DUMP_SIGNATURES: set[tuple[str, int, int]] = set()
_SEEN_DUMP_LOCK = threading.Lock()


class UninstallCriticalError(RuntimeError):
    """Raised when msiexec /x fails with a critical error (e.g. 1603)."""

# Install directories
INSTALL_DIR_32 = Path(r"C:\Program Files (x86)\Netskope\STAgent")
INSTALL_DIR_64 = Path(r"C:\Program Files\Netskope\STAgent")

# Services to verify after upgrade
SERVICES: dict[str, str] = {
    "client":   "stAgentSvc",
    "watchdog": "stwatchdog",
    "driver":   "stadrv",
}

# Key executables that must exist in the install directory
REQUIRED_EXECUTABLES: list[str] = [
    "stAgentSvc.exe",
    "stAgentUI.exe",
]

# Additional executable required only in watchdog mode
WATCHDOG_EXECUTABLE = "stAgentSvcMon.exe"

# Registry paths for Netskope uninstall entry
UNINSTALL_REG_PATHS: list[str] = [
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
]
UNINSTALL_DISPLAY_NAME = "Netskope Client"


@dataclass
class ServiceInfo:
    """Parsed result of an ``sc query`` call."""
    name: str
    exists: bool
    state: str


@dataclass
class ExeValidationResult:
    """Result of executable validation in the install directory."""
    valid: bool
    install_dir: str
    present: list[str]
    missing: list[str]
    version_mismatches: list[str]
    stale_arch_files: list[str] = field(default_factory=list)
    watchdog_mode: bool = False
    processes_running: list[str] = field(default_factory=list)
    processes_not_running: list[str] = field(default_factory=list)
    stwatchdog_running: Optional[bool] = None   # None = not in watchdog mode
    watchdog_duplicate: Optional[str] = None   # Non-None = validation issue


@dataclass
class UninstallEntryResult:
    """Result of checking the Windows uninstall registry entry."""
    found: bool
    display_name: str
    display_version: str
    install_location: str
    product_code: str = ""


@dataclass
class NsConfigInfo:
    """Information extracted from a local NSClient nsconfig.json."""
    tenant_hostname: str
    config_name: str
    allow_auto_update: bool = False


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
    NSCONFIG_ENC_PATH = Path(r"C:\ProgramData\netskope\stagent\nsconfig.enc")
    NSDIAG_PATH_32 = Path(r"C:\Program Files (x86)\Netskope\STAgent\nsdiag.exe")
    NSDIAG_PATH_64 = Path(r"C:\Program Files\Netskope\STAgent\nsdiag.exe")

    @staticmethod
    def _strip_trailing_dot(value: str) -> str:
        """Normalize nsdiag field values by removing trailing dot/space."""
        return value.strip().rstrip(".").strip()

    @staticmethod
    def _extract_tenant_from_gateway(gateway: str) -> str:
        """Convert gateway host to tenant host (strip leading gateway-)."""
        host = LocalClient._strip_trailing_dot(gateway)
        return host[len("gateway-"):] if host.startswith("gateway-") else host

    @staticmethod
    def detect_tenant_from_nsdiag(
        is_64_bit: Optional[bool] = None,
    ) -> NsConfigInfo | None:
        """
        Detect tenant/config from ``nsdiag.exe -f`` output.

        Primary field is ``Tenant URL``. If missing, falls back to
        ``Gateway`` (with ``gateway-`` prefix stripped).

        :param is_64_bit: Prefer 64-bit nsdiag when True, 32-bit when False,
                          or auto-detect when None.
        :return: NsConfigInfo when parsed, otherwise None.
        """
        paths_to_try: list[Path]
        if is_64_bit is True:
            paths_to_try = [LocalClient.NSDIAG_PATH_64, LocalClient.NSDIAG_PATH_32]
        elif is_64_bit is False:
            paths_to_try = [LocalClient.NSDIAG_PATH_32, LocalClient.NSDIAG_PATH_64]
        else:
            paths_to_try = [LocalClient.NSDIAG_PATH_64, LocalClient.NSDIAG_PATH_32]

        nsdiag: Optional[Path] = None
        for candidate in paths_to_try:
            if candidate.is_file():
                nsdiag = candidate
                break
        if nsdiag is None:
            log.warning("nsdiag.exe not found for tenant fallback")
            return None

        try:
            result = subprocess.run(
                [str(nsdiag), "-f"],
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                log.warning(
                    "nsdiag -f failed (exit %d): %s",
                    result.returncode,
                    (result.stderr or "").strip(),
                )
                return None

            tenant_url = ""
            gateway = ""
            config_name = ""
            for raw_line in result.stdout.splitlines():
                line = raw_line.strip()
                if "::" not in line:
                    continue
                key, value = line.split("::", 1)
                key = key.strip().lower()
                value = LocalClient._strip_trailing_dot(value)
                if key == "tenant url":
                    tenant_url = value
                elif key == "gateway":
                    gateway = value
                elif key == "config":
                    config_name = value

            hostname = tenant_url or LocalClient._extract_tenant_from_gateway(gateway)
            if not hostname:
                log.warning("nsdiag -f did not provide tenant hostname")
                return None

            log.info("Detected tenant from nsdiag -f: %s", hostname)
            return NsConfigInfo(
                tenant_hostname=hostname,
                config_name=config_name,
                allow_auto_update=False,
            )
        except Exception as exc:
            log.warning("Failed to parse tenant from nsdiag -f: %s", exc)
            return None

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
                 or None if detection fails.
        """
        path = nsconfig_path or LocalClient.NSCONFIG_PATH

        # For default path, try nsdiag fallback when nsconfig is missing,
        # encrypted, or not readable due to permissions.
        allow_diag_fallback = nsconfig_path is None

        try:
            if not path.is_file():
                if allow_diag_fallback:
                    if LocalClient.NSCONFIG_ENC_PATH.is_file():
                        log.warning(
                            "nsconfig.json unavailable but nsconfig.enc exists "
                            "— falling back to nsdiag -f"
                        )
                    else:
                        log.warning(
                            "nsconfig.json not found at %s — falling back to "
                            "nsdiag -f",
                            path,
                        )
                    return LocalClient.detect_tenant_from_nsdiag()
                return None
        except PermissionError as exc:
            if allow_diag_fallback:
                log.warning(
                    "No permission to access nsconfig folder (%s) — "
                    "falling back to nsdiag -f",
                    exc,
                )
                return LocalClient.detect_tenant_from_nsdiag()
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
            client_config = config.get("clientConfig", {})
            config_name: str = client_config.get("configurationName", "")
            allow_auto_update = (
                str(
                    client_config
                    .get("clientUpdate", {})
                    .get("allowAutoUpdate", "")
                ).lower() == "true"
            )
            return NsConfigInfo(
                tenant_hostname=hostname,
                config_name=config_name,
                allow_auto_update=allow_auto_update,
            )
        except PermissionError as exc:
            if allow_diag_fallback:
                log.warning(
                    "Cannot read nsconfig.json (permission denied: %s) — "
                    "falling back to nsdiag -f",
                    exc,
                )
                return LocalClient.detect_tenant_from_nsdiag()
            log.warning("Failed to read nsconfig.json: %s", exc)
            return None
        except Exception as exc:
            if allow_diag_fallback:
                log.warning(
                    "Failed to read nsconfig.json: %s — "
                    "falling back to nsdiag -f",
                    exc,
                )
                return LocalClient.detect_tenant_from_nsdiag()
            log.warning("Failed to read nsconfig.json: %s", exc)
            return None

    @staticmethod
    def sync_config_from_tenant(
        is_64_bit: bool = False,
        wait_seconds: float = 30,
    ) -> None:
        """
        Trigger a config sync from the tenant using nsdiag -u.

        After a fresh install, nsconfig.json may not yet contain
        the full client configuration (e.g. configurationName).
        Running ``nsdiag.exe -u`` forces the client to pull the
        latest config from the tenant.

        Wait logic adapts to how long nsdiag itself took:
        - If nsdiag took > 10s the sync likely completed during
          the command — no extra wait.
        - If nsdiag took <= 10s, wait an additional 5s for
          nsconfig.json to be written.

        :param is_64_bit: Use 64-bit nsdiag path.
        :param wait_seconds: Legacy parameter (kept for
                             signature compatibility, no longer used).
        """
        nsdiag = (
            LocalClient.NSDIAG_PATH_64 if is_64_bit
            else LocalClient.NSDIAG_PATH_32
        )
        if not nsdiag.is_file():
            log.warning("nsdiag not found at %s — skipping config sync", nsdiag)
            return

        log.info("Syncing config from tenant: %s -u", nsdiag)
        cmd_start = time.time()
        try:
            result = subprocess.run(
                [str(nsdiag), "-u"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                log.warning(
                    "nsdiag -u exit code %d: %s",
                    result.returncode, result.stderr,
                )
        except Exception as exc:
            log.warning("nsdiag -u failed: %s", exc)
            return

        cmd_elapsed = time.time() - cmd_start
        log.info("nsdiag -u completed in %.1fs", cmd_elapsed)

        if cmd_elapsed > 10:
            log.info(
                "nsdiag took >10s — skipping additional wait"
            )
        else:
            extra_wait = 5
            log.info(
                "nsdiag took <=10s — waiting %ds for config write...",
                extra_wait,
            )
            time.sleep(extra_wait)

        log.info("Config sync wait completed")

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

    def install_msi(self, setup_file_path: str, log_dir: Optional[Path] = None) -> None:
        """
        Install the client using msiexec silent install (Windows).

        :param setup_file_path: Full path to the MSI installer.
        :param log_dir: Directory for the msiexec verbose log. Falls back to the MSI's parent.
        :raises RuntimeError: If not running as admin or msiexec fails.
        """
        if not self._is_admin():
            raise RuntimeError(
                "msiexec /qn requires administrator privileges. "
                "Re-run the script as Administrator."
            )

        log_parent = log_dir if log_dir else Path(setup_file_path).parent
        msi_log = Path(log_parent) / "msiexec.log"
        log.info("Installing via msiexec: %s", setup_file_path)
        result = subprocess.run(
            ["msiexec", "/i", setup_file_path, "/qn", "/l*v", str(msi_log)],
            capture_output=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            detail = ""
            if msi_log.is_file():
                log.error("msiexec verbose log: %s", msi_log)
                detail = f" (see {msi_log})"
            raise RuntimeError(
                f"msiexec failed (exit code {result.returncode}){detail}"
            )
        # Clean up msi log on success
        if msi_log.is_file():
            msi_log.unlink(missing_ok=True)
        log.info("msiexec install completed")

    @staticmethod
    def _is_admin() -> bool:
        """Check if the current process has administrator privileges."""
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def uninstall(self) -> None:
        """Uninstall the currently installed client."""
        self._ensure_initialized()
        log.info("Uninstalling client")
        self._client.uninstall()
        log.info("Client uninstalled")

    @staticmethod
    def uninstall_msi(
        product_code: str,
        log_dir: Optional[Path] = None,
    ) -> None:
        """
        Uninstall the client using ``msiexec /x`` with a product code.

        Retries once after 10 seconds on failure. Raises
        :class:`UninstallCriticalError` if the last exit code is 1603,
        or :class:`RuntimeError` for other failures.

        :param product_code: Registry subkey name (e.g. '{GUID}' or 'NetskopeClient').
        :param log_dir: Directory for the msiexec uninstall log.
        :raises UninstallCriticalError: If uninstall fails with exit code 1603.
        :raises RuntimeError: If uninstall fails on both attempts (non-1603).
        """
        msi_log = Path(log_dir) / "msiexec_uninstall.log" if log_dir else None
        for attempt in range(1, 3):
            log.info("Uninstalling via msiexec /x %s", product_code)
            cmd = ["msiexec", "/x", product_code, "/qn"]
            if msi_log:
                cmd.extend(["/l*v", str(msi_log)])
            result = subprocess.run(
                cmd,
                capture_output=True, timeout=300,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                log.info("msiexec /x completed")
                return
            log.warning(
                "msiexec /x exit code %d: %s",
                result.returncode, result.stderr,
            )
            if attempt == 1:
                if result.returncode == 1603:
                    log.warning("Exit code 1603 — killing msiexec.exe before retry")
                    subprocess.run(
                        ["taskkill", "/f", "/im", "msiexec.exe"],
                        capture_output=True,
                    )
                log.info("Retrying uninstall in 10 seconds...")
                time.sleep(10)
        if msi_log and msi_log.is_file():
            log.error("msiexec uninstall log: %s", msi_log)
        if result.returncode == 1603:
            raise UninstallCriticalError(
                f"Uninstall failed with critical error 1603 "
                f"(product_code={product_code})"
            )
        raise RuntimeError(
            f"Uninstall failed after 2 attempts "
            f"(product_code={product_code}, "
            f"last exit code={result.returncode})"
        )

    @staticmethod
    def get_msi_subject(msi_path: Path) -> str:
        """
        Read the Subject field from an MSI file's summary information.

        Uses the Windows Installer COM object via PowerShell.
        The Subject typically contains the product version string.

        :param msi_path: Path to the .msi file.
        :return: Subject string, or empty on failure.
        """
        ps_script = (
            "$installer = New-Object -ComObject WindowsInstaller.Installer; "
            "$db = $installer.GetType().InvokeMember('OpenDatabase', "
            "'InvokeMethod', $null, $installer, "
            f"@('{msi_path}', 0)); "
            "$si = $db.GetType().InvokeMember('SummaryInformation', "
            "'GetProperty', $null, $db, $null); "
            "$si.GetType().InvokeMember('Property', "
            "'GetProperty', $null, $si, @(3))"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=15,
            )
            subject = result.stdout.strip()
            if result.returncode == 0 and subject:
                log.info("MSI subject for %s: %s", msi_path.name, subject)
                return subject
            log.warning(
                "Failed to read MSI subject (exit %d): %s",
                result.returncode, result.stderr.strip(),
            )
            return ""
        except Exception as exc:
            log.warning("Failed to read MSI subject: %s", exc)
            return ""

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
    def is_process_running(image_name: str) -> bool:
        """
        Check if a process is currently running by image name.

        :param image_name: Executable name (e.g. 'stAgentSvc.exe').
        :return: True if at least one instance is running.
        """
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return image_name.lower() in result.stdout.lower()
        except Exception as exc:
            log.debug("Process check for %s failed: %s", image_name, exc)
            return False

    @staticmethod
    def get_process_instances(
        image_name: str,
    ) -> list[tuple[int, str]]:
        """
        Return ``(PID, CommandLine)`` for every running instance of
        *image_name* using PowerShell ``Get-CimInstance``.

        :param image_name: Executable name (e.g. ``stAgentSvcMon.exe``).
        :return: List of (pid, cmdline) tuples, empty on failure.
        """
        import csv
        import io

        try:
            ps_cmd = (
                f"Get-CimInstance Win32_Process "
                f"-Filter \"Name='{image_name}'\" "
                f"| Select-Object ProcessId,CommandLine "
                f"| ConvertTo-Csv -NoTypeInformation"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return []
            entries: list[tuple[int, str]] = []
            lines = result.stdout.strip().splitlines()
            for row in csv.reader(lines[1:]):
                if len(row) >= 2 and row[0].strip().isdigit():
                    pid = int(row[0].strip())
                    cmdline = row[1].strip() if row[1] else ""
                    entries.append((pid, cmdline))
            return entries
        except Exception as exc:
            log.debug(
                "get_process_instances(%s) failed: %s",
                image_name, exc,
            )
            return []

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

    # ── Service Queries ─────────────────────────────────────────────

    @staticmethod
    def query_service(service_name: str) -> ServiceInfo:
        """
        Query a Windows service via ``sc query``.

        :param service_name: Windows service name (e.g. 'stAgentSvc').
        :return: ServiceInfo with exists flag and current state.
        """
        try:
            result = subprocess.run(
                ["sc", "query", service_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return ServiceInfo(name=service_name, exists=False, state="")
            # Parse STATE line, e.g. "        STATE              : 4  RUNNING "
            state = ""
            for line in result.stdout.splitlines():
                if "STATE" in line:
                    parts = line.strip().split()
                    # Last token is the state name
                    state = parts[-1] if parts else ""
                    break
            return ServiceInfo(name=service_name, exists=True, state=state)
        except Exception as exc:
            log.warning("sc query %s failed: %s", service_name, exc)
            return ServiceInfo(name=service_name, exists=False, state="")

    @staticmethod
    def query_service_binpath(service_name: str) -> str:
        """
        Get the binary path of a Windows service via ``sc qc``.

        :param service_name: Windows service name.
        :return: BINARY_PATH_NAME value, or empty string on failure.
        """
        try:
            result = subprocess.run(
                ["sc", "qc", service_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return ""
            for line in result.stdout.splitlines():
                if "BINARY_PATH_NAME" in line:
                    # Format: "        BINARY_PATH_NAME   : C:\path\to\exe"
                    _, _, value = line.partition(":")
                    return value.strip()
            return ""
        except Exception as exc:
            log.warning("sc qc %s failed: %s", service_name, exc)
            return ""

    # ── Install Path Helpers ────────────────────────────────────────

    @staticmethod
    def get_install_dir(is_64_bit: bool) -> Path:
        """
        Return the expected install directory for the given bitness.

        :param is_64_bit: True for 64-bit, False for 32-bit.
        :return: Path to the install directory.
        """
        return INSTALL_DIR_64 if is_64_bit else INSTALL_DIR_32

    @staticmethod
    def verify_install_dir(is_64_bit: bool) -> bool:
        """
        Verify that the expected install directory exists and contains
        the main service executable (``stAgentSvc.exe``).

        :param is_64_bit: True for 64-bit, False for 32-bit.
        :return: True if directory and key executable exist.
        """
        install_dir = LocalClient.get_install_dir(is_64_bit)
        exe = install_dir / "stAgentSvc.exe"
        exists = exe.is_file()
        if exists:
            log.info("Install dir verified: %s", install_dir)
        else:
            log.warning("Install dir missing or incomplete: %s", install_dir)
        return exists

    # ── Pre-Report Validation ──────────────────────────────────────

    @staticmethod
    def is_watchdog_mode(nsconfig_path: Path | None = None) -> bool:
        """
        Check if the client is in watchdog mode by reading
        ``nsclient_watchdog_monitor`` from nsconfig.json.

        If nsconfig.json is unavailable, check for the stAgentSvcMon.exe
        watchdog monitor executable (indicates watchdog mode).

        :param nsconfig_path: Override path (for testing).
        :return: True if watchdog monitor is enabled.
        """
        path = nsconfig_path or LocalClient.NSCONFIG_PATH
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                raw = config.get("clientConfig", {}).get("nsclient_watchdog_monitor")
                result = str(raw).lower() == "true" if raw is not None else False
                log.info(
                    "is_watchdog_mode: read %s — clientConfig.nsclient_watchdog_monitor=%r → %s",
                    path, raw, result,
                )
                return result
            except Exception as exc:
                log.warning("Failed to read watchdog mode from nsconfig: %s", exc)
                return False

        # nsconfig.json not found; check for stAgentSvcMon.exe watchdog executable
        log.info("is_watchdog_mode: nsconfig.json not found — checking for watchdog executable")
        for install_dir in [INSTALL_DIR_64, INSTALL_DIR_32]:
            watchdog_exe = install_dir / WATCHDOG_EXECUTABLE
            if watchdog_exe.is_file():
                log.info(
                    "is_watchdog_mode: watchdog executable found at %s → True",
                    watchdog_exe,
                )
                return True
        log.info("is_watchdog_mode: no watchdog executable found → False")
        return False

    @staticmethod
    def get_file_version(file_path: Path) -> str:
        """
        Get the product version of a Windows executable via PowerShell.

        :param file_path: Path to the .exe file.
        :return: Version string (e.g. '95.1.0.900'), or empty on failure.
        """
        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"(Get-Item '{file_path}').VersionInfo.ProductVersion",
                ],
                capture_output=True, text=True, timeout=15,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception as exc:
            log.warning("Failed to get version of %s: %s", file_path, exc)
            return ""

    @staticmethod
    def verify_executables(
        is_64_bit: bool,
        expected_version: str,
        nsconfig_path: Path | None = None,
    ) -> ExeValidationResult:
        """
        Verify that required executables exist in the correct install
        directory, are the expected version, and include the watchdog
        monitor executable if watchdog mode is enabled.

        :param is_64_bit: Expected bitness (determines install directory).
        :param expected_version: Expected product version string.
        :param nsconfig_path: Override nsconfig path (for testing).
        :return: ExeValidationResult with details.
        """
        install_dir = LocalClient.get_install_dir(is_64_bit)
        watchdog = LocalClient.is_watchdog_mode(nsconfig_path)

        # Strip "(64-bit)" suffix — the nsclient library appends it for
        # display, but the actual executable ProductVersion is bare.
        clean_expected = expected_version.replace(" (64-bit)", "")

        # Fall back to physical presence if nsconfig key is missing
        if not watchdog and (install_dir / WATCHDOG_EXECUTABLE).is_file():
            log.info(
                "nsclient_watchdog_monitor not set in nsconfig but %s exists "
                "— treating as watchdog mode",
                WATCHDOG_EXECUTABLE,
            )
            watchdog = True

        exe_list = list(REQUIRED_EXECUTABLES)
        if watchdog:
            log.info(
                "Watchdog mode enabled — verifying %s in %s",
                WATCHDOG_EXECUTABLE, install_dir,
            )
            exe_list.append(WATCHDOG_EXECUTABLE)

        present: list[str] = []
        missing: list[str] = []
        version_mismatches: list[str] = []
        processes_running: list[str] = []
        processes_not_running: list[str] = []

        for exe_name in exe_list:
            exe_path = install_dir / exe_name
            if not exe_path.is_file():
                missing.append(exe_name)
                log.warning("Missing executable: %s", exe_path)
                continue
            present.append(exe_name)
            file_ver = LocalClient.get_file_version(exe_path)
            if file_ver and file_ver != clean_expected:
                version_mismatches.append(
                    f"{exe_name}: {file_ver} (expected {clean_expected})"
                )
                log.warning(
                    "Version mismatch: %s is %s, expected %s",
                    exe_name, file_ver, clean_expected,
                )
            elif file_ver:
                log.info("Verified %s version: %s", exe_name, file_ver)

            if LocalClient.is_process_running(exe_name):
                processes_running.append(exe_name)
                log.info("Process running: %s", exe_name)
            else:
                processes_not_running.append(exe_name)
                log.warning("Process not running: %s", exe_name)

        # Check stwatchdog service when in watchdog mode
        stwatchdog_running: Optional[bool] = None
        watchdog_duplicate: Optional[str] = None
        if watchdog:
            stwatchdog_running = LocalClient.is_service_running("stwatchdog")
            if stwatchdog_running:
                log.info("stwatchdog service is running")
            else:
                log.warning("stwatchdog service is NOT running")

            # In watchdog mode there must be exactly one
            # stAgentSvcMon.exe running with the "-watchdog" arg.
            mon_instances = LocalClient.get_process_instances(
                WATCHDOG_EXECUTABLE,
            )
            if len(mon_instances) == 0:
                watchdog_duplicate = (
                    f"no {WATCHDOG_EXECUTABLE} process found"
                )
                log.warning(watchdog_duplicate)
            elif len(mon_instances) > 1:
                pids = [str(pid) for pid, _ in mon_instances]
                watchdog_duplicate = (
                    f"multiple {WATCHDOG_EXECUTABLE} instances "
                    f"running (PIDs: {', '.join(pids)})"
                )
                log.warning(watchdog_duplicate)
            else:
                pid, cmdline = mon_instances[0]
                if "-watchdog" not in cmdline.lower():
                    watchdog_duplicate = (
                        f"{WATCHDOG_EXECUTABLE} (PID {pid}) not "
                        f"running as -watchdog: {cmdline}"
                    )
                    log.warning(watchdog_duplicate)
                else:
                    log.info(
                        "%s running as -watchdog (PID %d)",
                        WATCHDOG_EXECUTABLE, pid,
                    )

        valid = (
            len(missing) == 0
            and len(version_mismatches) == 0
            and watchdog_duplicate is None
            and (not watchdog or stwatchdog_running is True)
        )
        if valid:
            log.info(
                "All executables verified in %s (watchdog_mode=%s)",
                install_dir, watchdog,
            )

        return ExeValidationResult(
            valid=valid,
            install_dir=str(install_dir),
            present=present,
            missing=missing,
            version_mismatches=version_mismatches,
            watchdog_mode=watchdog,
            processes_running=processes_running,
            processes_not_running=processes_not_running,
            stwatchdog_running=stwatchdog_running,
            watchdog_duplicate=watchdog_duplicate,
        )

    @staticmethod
    def check_old_arch_cleanup(source_64_bit: bool, target_64_bit: bool) -> list[str]:
        """
        Check for leftover NSClient executables in the old arch install dir
        after an arch-changing upgrade (32→64 or 64→32).

        :param source_64_bit: Arch of the original install.
        :param target_64_bit: Arch of the upgrade target.
        :return: List of leftover exe names still present in the old dir.
                 Empty if no arch change or the old dir is already clean.
        """
        if source_64_bit == target_64_bit:
            return []
        old_dir = LocalClient.get_install_dir(source_64_bit)
        old_arch = "64" if source_64_bit else "32"
        all_exes = list(REQUIRED_EXECUTABLES) + [WATCHDOG_EXECUTABLE]
        leftover = [exe for exe in all_exes if (old_dir / exe).is_file()]
        if leftover:
            log.warning(
                "Arch change cleanup: old %s-bit dir %s still contains: %s",
                old_arch, old_dir, leftover,
            )
        else:
            log.info(
                "Arch change cleanup: old %s-bit dir is clean: %s",
                old_arch, old_dir,
            )
        return leftover

    @staticmethod
    def check_uninstall_registry() -> UninstallEntryResult:
        """
        Check if the Netskope Client uninstall entry exists in the
        Windows registry (Add/Remove Programs).

        Searches both native and WOW6432Node uninstall paths.

        :return: UninstallEntryResult with entry details.
        """
        import winreg

        for reg_path in UNINSTALL_REG_PATHS:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, reg_path,
                ) as parent_key:
                    i = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(parent_key, i)
                            with winreg.OpenKey(
                                parent_key, subkey_name,
                            ) as subkey:
                                try:
                                    display_name, _ = winreg.QueryValueEx(
                                        subkey, "DisplayName",
                                    )
                                except FileNotFoundError:
                                    i += 1
                                    continue
                                if UNINSTALL_DISPLAY_NAME.lower() in str(display_name).lower():
                                    display_ver = ""
                                    install_loc = ""
                                    try:
                                        display_ver, _ = winreg.QueryValueEx(
                                            subkey, "DisplayVersion",
                                        )
                                    except FileNotFoundError:
                                        pass
                                    try:
                                        install_loc, _ = winreg.QueryValueEx(
                                            subkey, "InstallLocation",
                                        )
                                    except FileNotFoundError:
                                        pass
                                    log.info(
                                        "Found uninstall entry: %s v%s at %s (key=%s)",
                                        display_name, display_ver, install_loc,
                                        subkey_name,
                                    )
                                    return UninstallEntryResult(
                                        found=True,
                                        display_name=str(display_name),
                                        display_version=str(display_ver),
                                        install_location=str(install_loc),
                                        product_code=subkey_name,
                                    )
                            i += 1
                        except OSError:
                            break
            except OSError:
                continue

        log.warning("No Netskope Client uninstall entry found in registry")
        return UninstallEntryResult(
            found=False,
            display_name="",
            display_version="",
            install_location="",
        )

    # ── Upgrade-In-Progress Registry Check ─────────────────────────────

    UPGRADE_REG_KEY = r"SOFTWARE\Netskope"
    UPGRADE_IN_PROGRESS_VALUE = "UpgradeInProgress"

    @staticmethod
    def check_upgrade_in_progress() -> bool:
        """
        Check whether DWORD value
        ``HKLM\\SOFTWARE\\Netskope\\UpgradeInProgress`` exists and is non-zero.

        The value is created/updated by the installer at the start of an
        upgrade and cleared/removed when the upgrade finishes. Immediately
        after reboot, the value may still be present while upgrade work is
        continuing.

        :return: True if value exists and int(value) != 0, False otherwise.
        """
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                LocalClient.UPGRADE_REG_KEY,
            ) as key:
                raw_value, reg_type = winreg.QueryValueEx(
                    key,
                    LocalClient.UPGRADE_IN_PROGRESS_VALUE,
                )
                value = int(raw_value)
                log.info(
                    "Registry value found: HKLM\\%s\\%s=%d (type=%s)",
                    LocalClient.UPGRADE_REG_KEY,
                    LocalClient.UPGRADE_IN_PROGRESS_VALUE,
                    value,
                    reg_type,
                )
                return value != 0
        except FileNotFoundError:
            log.info(
                "Registry value NOT found: HKLM\\%s\\%s",
                LocalClient.UPGRADE_REG_KEY,
                LocalClient.UPGRADE_IN_PROGRESS_VALUE,
            )
            return False
        except (TypeError, ValueError) as exc:
            log.warning(
                "Registry value invalid: HKLM\\%s\\%s (%s)",
                LocalClient.UPGRADE_REG_KEY,
                LocalClient.UPGRADE_IN_PROGRESS_VALUE,
                exc,
            )
            return False
        except OSError as exc:
            log.warning(
                "Error reading registry HKLM\\%s\\%s: %s",
                LocalClient.UPGRADE_REG_KEY,
                LocalClient.UPGRADE_IN_PROGRESS_VALUE,
                exc,
            )
            return False

    @staticmethod
    def set_upgrade_in_progress(value: int = 1) -> None:
        """
        Set DWORD value ``HKLM\\SOFTWARE\\Netskope\\UpgradeInProgress``.

        :param value: DWORD value to write (default: 1).
        :raises RuntimeError: If registry write fails.
        """
        import winreg

        try:
            with winreg.CreateKey(
                winreg.HKEY_LOCAL_MACHINE,
                LocalClient.UPGRADE_REG_KEY,
            ) as key:
                winreg.SetValueEx(
                    key,
                    LocalClient.UPGRADE_IN_PROGRESS_VALUE,
                    0,
                    winreg.REG_DWORD,
                    int(value),
                )
            log.info(
                "Set registry value HKLM\\%s\\%s=%d",
                LocalClient.UPGRADE_REG_KEY,
                LocalClient.UPGRADE_IN_PROGRESS_VALUE,
                value,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to set UpgradeInProgress registry value: {exc}"
            ) from exc

    @staticmethod
    def set_upgrade_nsconfig_cache(
        last_client_updated: str = "1",
        new_client_ver: str = "137.0.0.2222",
        nsconfig_path: Optional[Path] = None,
    ) -> None:
        """
        Update simulation cache fields in nsconfig.json under the root ``cache`` node.

        Fields written:
        - ``cache.lastClientUpdated``
        - ``cache.newClientVer``

        :param last_client_updated: Value for ``cache.lastClientUpdated``.
        :param new_client_ver: Value for ``cache.newClientVer``.
        :param nsconfig_path: Optional override path for tests.
        :raises RuntimeError: If file read/write fails.
        """
        path = nsconfig_path or LocalClient.NSCONFIG_PATH
        if nsconfig_path is None and LocalClient.NSCONFIG_ENC_PATH.is_file() and not path.is_file():
            raise RuntimeError(
                "nsconfig.json is unavailable (nsconfig.enc exists); "
                "cannot update simulation cache"
            )
        if not path.is_file():
            raise RuntimeError(f"nsconfig.json not found: {path}")

        try:
            with open(path, "r", encoding="utf-8") as file_obj:
                config: dict[str, Any] = json.load(file_obj)

            cache_obj = config.get("cache")
            if not isinstance(cache_obj, dict):
                cache_obj = {}
                config["cache"] = cache_obj

            cache_obj["lastClientUpdated"] = str(last_client_updated)
            cache_obj["newClientVer"] = str(new_client_ver)

            with open(path, "w", encoding="utf-8") as file_obj:
                json.dump(config, file_obj, indent=4)
                file_obj.write("\n")

            log.info(
                "Updated nsconfig cache simulation fields: "
                "cache.lastClientUpdated=%s, cache.newClientVer=%s",
                cache_obj["lastClientUpdated"],
                cache_obj["newClientVer"],
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to update nsconfig cache simulation fields: {exc}"
            ) from exc

    @staticmethod
    def try_set_upgrade_nsconfig_cache(
        last_client_updated: str = "1",
        new_client_ver: str = "137.0.0.2222",
        nsconfig_path: Optional[Path] = None,
    ) -> bool:
        """
        Best-effort wrapper for simulation cache updates.

        Returns False (without raising) when nsconfig cannot be read/written,
        including encrypted or permission-restricted environments.

        :return: True if updated, False if skipped.
        """
        try:
            LocalClient.set_upgrade_nsconfig_cache(
                last_client_updated=last_client_updated,
                new_client_ver=new_client_ver,
                nsconfig_path=nsconfig_path,
            )
            return True
        except Exception as exc:
            log.warning(
                "Skipping nsconfig simulation cache update: %s",
                exc,
            )
            return False

    @staticmethod
    def ensure_non_watchdog_monitor_service(
        is_64_bit: bool,
    ) -> None:
        """
        For non-watchdog simulation runs, ensure monitor executable/service exist.

        Actions (idempotent):
        1. Clone ``stAgentSvc.exe`` to ``stAgentSvcMon.exe`` if missing.
        2. Ensure Windows service ``stagentsvcmon`` exists with
           ``stAgentSvcMon.exe -monitor`` binpath.
        3. Start the service if not already running.

        :param is_64_bit: Install arch used to resolve STAgent directory.
        :raises RuntimeError: If required executable is missing or service ops fail.
        """
        install_dir = LocalClient.get_install_dir(is_64_bit)
        src_exe = install_dir / "stAgentSvc.exe"
        mon_exe = install_dir / WATCHDOG_EXECUTABLE
        service_name = "stagentsvcmon"

        if not src_exe.is_file():
            raise RuntimeError(f"Missing source executable for clone: {src_exe}")

        if mon_exe.is_file():
            log.info("Monitor executable already exists: %s", mon_exe)
        else:
            shutil.copy2(src_exe, mon_exe)
            log.info("Cloned monitor executable: %s -> %s", src_exe, mon_exe)

        cmdline = f'"{mon_exe}" -monitor'
        svc_info = LocalClient.query_service(service_name)

        if svc_info.exists:
            binpath = LocalClient.query_service_binpath(service_name)
            if "stagentsvcmon.exe" not in binpath.lower():
                log.warning(
                    "Service %s already exists with unexpected binpath: %s",
                    service_name,
                    binpath,
                )
            else:
                log.info("Service %s already exists", service_name)
        else:
            result = subprocess.run(
                [
                    "sc",
                    "create",
                    service_name,
                    f"binPath= {cmdline}",
                    "start= demand",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "Failed to create service "
                    f"{service_name}: {result.stderr.strip() or result.stdout.strip()}"
                )
            log.info("Created service %s with binpath: %s", service_name, cmdline)

        svc_info = LocalClient.query_service(service_name)
        if svc_info.state.upper() == "RUNNING":
            log.info("Service %s already running", service_name)
            return

        start_result = subprocess.run(
            ["sc", "start", service_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if start_result.returncode != 0:
            stderr = (start_result.stderr or "").lower()
            stdout = (start_result.stdout or "").lower()
            if "1056" in stderr or "1056" in stdout:
                log.info("Service %s already running (1056)", service_name)
                return
            raise RuntimeError(
                "Failed to start service "
                f"{service_name}: "
                f"{start_result.stderr.strip() or start_result.stdout.strip()}"
            )

        log.info("Started service %s", service_name)

    @staticmethod
    def install_local_upgrade_msi(
        setup_file_path: str,
        sta_update_log_path: Path,
    ) -> None:
        """
        Install a local upgrade MSI with STAUpdate logging format.

        Command shape:
            msiexec /l*v+ "<STAUpdate.txt>" /i "<msi>" /qn

        :param setup_file_path: Full path to local upgrade MSI.
        :param sta_update_log_path: Full path to STAUpdate.txt.
        :raises RuntimeError: If not admin or msiexec fails.
        """
        if not LocalClient._is_admin():
            log.error(
                "Local upgrade msiexec not started: administrator privileges "
                "are required"
            )
            raise RuntimeError(
                "msiexec /qn requires administrator privileges. "
                "Re-run the script as Administrator."
            )

        msi_path = Path(setup_file_path)
        if not msi_path.is_file():
            log.error("Local upgrade MSI not found: %s", msi_path)
            raise FileNotFoundError(f"Local upgrade MSI not found: {msi_path}")

        sta_update_log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "msiexec",
            "/l*v+", str(sta_update_log_path),
            "/i", setup_file_path,
            "/qn",
        ]
        trigger_time = datetime.now().isoformat(timespec="seconds")
        log.info("Local upgrade msiexec trigger time: %s", trigger_time)
        log.info("Local upgrade msiexec args: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        log.info("Local upgrade msiexec started (pid=%s)", proc.pid)

        try:
            stdout, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise RuntimeError(
                "Local upgrade msiexec timed out after 300s "
                f"(pid={proc.pid}, log={sta_update_log_path})"
            )

        if proc.returncode != 0:
            raise RuntimeError(
                "Local upgrade msiexec failed "
                f"(exit code {proc.returncode}, pid={proc.pid}, "
                f"log={sta_update_log_path})"
            )
        if stdout:
            log.debug("Local upgrade msiexec stdout: %s", stdout.strip())
        if stderr:
            log.debug("Local upgrade msiexec stderr: %s", stderr.strip())
        log.info("Local upgrade msiexec completed")

    # ── Crash Dump Detection & Log Bundle ──────────────────────────────

    # Paths where Netskope Client may write crash dump files
    DUMP_GLOB_PATTERNS: list[str] = [
        r"C:\dump\stAgentSvc.exe\*.dmp",
        r"C:\ProgramData\netskope\stagent\logs\*.dmp",
    ]

    @staticmethod
    def _get_dump_patterns() -> list[str]:
        """Return all glob patterns for crash dump locations."""
        import os

        patterns = list(LocalClient.DUMP_GLOB_PATTERNS)
        appdata = os.getenv("APPDATA")
        if appdata:
            patterns.append(
                str(Path(appdata) / r"Netskope\stagent\Logs\*.dmp")
            )
        return patterns

    @staticmethod
    def check_crash_dumps() -> tuple[bool, int]:
        """
        Check well-known paths for crash dump files.

        Zero-byte dumps are cleaned up automatically.

        :return: (crash_found, zero_byte_count) — ``crash_found`` is
                 True if at least one non-empty ``.dmp`` file exists.
        """
        import os

        found = False
        zero_count = 0
        for pattern in LocalClient._get_dump_patterns():
            for f in glob.glob(pattern):
                try:
                    size = os.path.getsize(f)
                    if size == 0:
                        try:
                            os.remove(f)
                            zero_count += 1
                        except OSError:
                            pass
                        continue

                    mtime = int(os.path.getmtime(f))
                    signature = (str(f).lower(), int(size), mtime)
                    first_seen = False
                    with _SEEN_DUMP_LOCK:
                        if signature not in _SEEN_DUMP_SIGNATURES:
                            _SEEN_DUMP_SIGNATURES.add(signature)
                            first_seen = True

                    if first_seen:
                        log.error(
                            "CRASH DUMP DETECTED: %s (Size: %d)",
                            f,
                            size,
                        )
                    found = True
                except Exception as exc:
                    log.error("Error checking dump file %s: %s", f, exc)
        return found, zero_count

    @staticmethod
    def _collect_event_logs(output_dir: Path, timestamp: str) -> None:
        """
        Export Windows Event Log System and Application channels to .evtx files.

        :param output_dir: Directory to write the exported log files into.
        :param timestamp: Timestamp string used as a filename prefix.
        """
        for channel in ("System", "Application"):
            output_file = output_dir / f"{timestamp}_event_log_{channel.lower()}.evtx"
            log.info("Collecting Windows Event Log (%s) -> %s", channel, output_file)
            try:
                result = subprocess.run(
                    ["wevtutil.exe", "epl", channel, str(output_file)],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode != 0:
                    log.warning(
                        "Event log export failed for %s (exit %d): %s",
                        channel, result.returncode, result.stderr,
                    )
                else:
                    log.info("Event log exported: %s", output_file)
            except Exception as exc:
                log.warning("Event log export failed for %s: %s", channel, exc)

    @staticmethod
    def collect_log_bundle(
        is_64_bit: bool,
        output_dir: Path,
    ) -> Optional[Path]:
        """
        Collect a client log bundle using ``nsdiag.exe -o``.

        Also exports the Windows Event Log System and Application channels
        as .evtx files into the same directory.

        Tries the 64-bit path first, falls back to 32-bit.

        :param is_64_bit: Preferred bitness for nsdiag.
        :param output_dir: Directory to write the zip bundle into.
        :return: Path to the created zip, or None on failure.
        """
        # Try preferred path first, then fallback
        paths_to_try = (
            [LocalClient.NSDIAG_PATH_64, LocalClient.NSDIAG_PATH_32]
            if is_64_bit
            else [LocalClient.NSDIAG_PATH_32, LocalClient.NSDIAG_PATH_64]
        )
        nsdiag: Optional[Path] = None
        for p in paths_to_try:
            if p.is_file():
                nsdiag = p
                break
        if nsdiag is None:
            log.warning("nsdiag.exe not found — cannot collect log bundle")
            return None

        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"{timestamp}_log_bundle.zip"

        LocalClient._collect_event_logs(output_dir, timestamp)

        log.info("Collecting log bundle: %s -o %s", nsdiag, output_file)
        try:
            result = subprocess.run(
                [str(nsdiag), "-o", str(output_file)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                log.warning(
                    "nsdiag log collection failed (exit %d): %s",
                    result.returncode, result.stderr,
                )
                return None
            log.info("Log bundle created: %s", output_file)
            return output_file
        except Exception as exc:
            log.warning("nsdiag log collection failed: %s", exc)
            return None

    @staticmethod
    def handle_crash(is_64_bit: bool, log_dir: Path) -> None:
        """
        Handle a detected crash: collect log bundle and copy dump
        files into the log directory.

        :param is_64_bit: Preferred bitness for nsdiag.
        :param log_dir: Directory to store collected artifacts.
        """
        try:
            log.info("Handling crash: collecting logs and dumps...")
            LocalClient.collect_log_bundle(is_64_bit, log_dir)

            for pattern in LocalClient._get_dump_patterns():
                for f in glob.glob(pattern):
                    import os

                    if os.path.exists(f) and os.path.getsize(f) > 0:
                        try:
                            shutil.copy2(f, str(log_dir))
                            log.info("Copied dump file %s to %s", f, log_dir)
                        except Exception as exc:
                            log.error("Failed to copy dump %s: %s", f, exc)
        except Exception as exc:
            log.error("Error during crash handling: %s", exc)


# ── Crash dump monitor ─────────────────────────────────────────────────

CRASH_MONITOR_INTERVAL = 2.0  # seconds between crash dump polls


class CrashMonitor:
    """
    Background daemon thread that polls for crash dump files every
    ``CRASH_MONITOR_INTERVAL`` seconds throughout the upgrade lifecycle.

    Start it before the upgrade begins so any dump written during install,
    post-reboot continuation, or idle wait is caught immediately.  The
    first time a non-empty dump is found :meth:`LocalClient.handle_crash`
    is called to collect the log bundle, Windows Event Logs, and copy the
    dump into *log_dir*.  After collection the optional *on_crash* callback
    is invoked so the caller can abort the active timing monitor or
    version-poll loop.

    The thread exits when:
    - :meth:`stop` is called (internal stop event), OR
    - the *stop_event* passed by the caller is set (ESC key interrupt)

    Usage::

        cm = CrashMonitor(
            is_64_bit=True,
            log_dir=Path("log/run1"),
            stop_event=esc_event,
            on_crash=monitor.stop,
        )
        cm.start()
        # ... upgrade runs ...
        cm.stop()
        if cm.crash_detected:
            ...
    """

    def __init__(
        self,
        is_64_bit: bool,
        log_dir: Path,
        stop_event: Optional[threading.Event] = None,
        on_crash: Optional[callable] = None,
    ) -> None:
        self._is_64_bit = is_64_bit
        self._log_dir = log_dir
        self._ext_stop_event = stop_event
        self._on_crash = on_crash
        self._crash_detected = False
        self._zero_count = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the background polling thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="crash-monitor",
        )
        self._thread.start()
        log.info(
            "Crash monitor started (interval=%.0fs)",
            CRASH_MONITOR_INTERVAL,
        )

    def stop(self) -> None:
        """Signal the thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("Crash monitor stopped")

    @property
    def crash_detected(self) -> bool:
        """True if at least one non-empty crash dump was found."""
        with self._lock:
            return self._crash_detected

    def _run(self) -> None:
        """Polling loop — runs as daemon thread."""
        while not self._stop_event.wait(timeout=CRASH_MONITOR_INTERVAL):
            # Also stop when the external stop event (ESC key) fires
            if self._ext_stop_event is not None and self._ext_stop_event.is_set():
                log.info("Crash monitor: external stop event set — exiting")
                break
            try:
                found, zero_count = LocalClient.check_crash_dumps()
                if zero_count > 0:
                    with self._lock:
                        self._zero_count += zero_count
                    log.info(
                        "Crash monitor: cleaned %d zero-byte dump file(s)",
                        zero_count,
                    )
                if found:
                    with self._lock:
                        already = self._crash_detected
                        self._crash_detected = True
                    if not already:
                        log.error("Crash monitor: crash dump detected — collecting logs")
                        try:
                            LocalClient.handle_crash(
                                self._is_64_bit, self._log_dir,
                            )
                        except Exception as exc:
                            log.error(
                                "Crash monitor: handle_crash failed: %s", exc,
                            )
                        if self._on_crash is not None:
                            try:
                                log.info("Crash monitor: invoking abort callback")
                                self._on_crash()
                            except Exception as exc:
                                log.debug(
                                    "Crash monitor: on_crash callback failed: %s", exc,
                                )

                        # Stop polling after first confirmed crash to avoid
                        # duplicate detections while the main flow is
                        # collecting failure logs.
                        self._stop_event.set()
            except Exception as exc:
                log.debug("Crash monitor: check failed: %s", exc)


# ── Installation log helpers ────────────────────────────────────────────

_NS_INSTALLATION_LOG = Path(
    r"C:\ProgramData\netskope\stagent\logs\nsInstallation.log"
)


def scan_installation_log(pattern: str) -> list[str]:
    """
    Scan nsInstallation.log for lines matching *pattern* (case-insensitive).

    :return: List of matching lines (stripped), or [] if file is missing.
    """
    if not _NS_INSTALLATION_LOG.is_file():
        return []
    try:
        text = _NS_INSTALLATION_LOG.read_text(encoding="utf-8", errors="replace")
        pat = pattern.lower()
        return [line.strip() for line in text.splitlines() if pat in line.lower()]
    except Exception as exc:
        log.warning("Failed to scan nsInstallation.log: %s", exc)
        return []


def check_driver_install_log(
    exe_validation: ExeValidationResult,
    service_running: bool,
) -> str:
    """
    When exe version mismatches exist but services are still running,
    scan nsInstallation.log for "driverinstall failed" entries.

    :return: Message fragment like ' — driverinstall failure: ...',
             or empty string if the condition is not met or no match found.
    """
    if not service_running:
        return ""
    if not exe_validation or not exe_validation.version_mismatches:
        return ""
    matches = scan_installation_log("driverinstall failed")
    if not matches:
        return ""
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for line in matches:
        if line and line not in seen:
            seen.add(line)
            unique.append(line)
    note = "; ".join(unique)
    if len(note) > 150:
        note = note[:147] + "..."
    log.warning("Driver install failure found in nsInstallation.log: %s", note)
    return f" — driverinstall failure: {note}"
