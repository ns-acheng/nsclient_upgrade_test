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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

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
    NSDIAG_PATH_32 = Path(r"C:\Program Files (x86)\Netskope\STAgent\nsdiag.exe")
    NSDIAG_PATH_64 = Path(r"C:\Program Files\Netskope\STAgent\nsdiag.exe")

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

        :param is_64_bit: Use 64-bit nsdiag path.
        :param wait_seconds: Seconds to wait after sync for config
                             to be written to nsconfig.json.
        """
        nsdiag = (
            LocalClient.NSDIAG_PATH_64 if is_64_bit
            else LocalClient.NSDIAG_PATH_32
        )
        if not nsdiag.is_file():
            log.warning("nsdiag not found at %s — skipping config sync", nsdiag)
            return

        log.info("Syncing config from tenant: %s -u", nsdiag)
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

        log.info(
            "Waiting %ds for config sync to complete...",
            int(wait_seconds),
        )
        time.sleep(wait_seconds)
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
    def uninstall_msi(product_code: str) -> None:
        """
        Uninstall the client using ``msiexec /x`` with a product code.

        :param product_code: Registry subkey name (e.g. '{GUID}' or 'NetskopeClient').
        """
        log.info("Uninstalling via msiexec /x %s", product_code)
        result = subprocess.run(
            ["msiexec", "/x", product_code, "/qn"],
            capture_output=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            log.warning(
                "msiexec /x exit code %d: %s",
                result.returncode, result.stderr,
            )
        else:
            log.info("msiexec /x completed")

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

        :param nsconfig_path: Override path (for testing).
        :return: True if watchdog monitor is enabled.
        """
        path = nsconfig_path or LocalClient.NSCONFIG_PATH
        if not path.is_file():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return bool(config.get("nsclient_watchdog_monitor", False))
        except Exception as exc:
            log.warning("Failed to read watchdog mode from nsconfig: %s", exc)
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

        exe_list = list(REQUIRED_EXECUTABLES)
        if watchdog:
            exe_list.append(WATCHDOG_EXECUTABLE)

        present: list[str] = []
        missing: list[str] = []
        version_mismatches: list[str] = []

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

        valid = len(missing) == 0 and len(version_mismatches) == 0
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
        )

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
                    log.error("CRASH DUMP DETECTED: %s (Size: %d)", f, size)
                    found = True
                except Exception as exc:
                    log.error("Error checking dump file %s: %s", f, exc)
        return found, zero_count

    @staticmethod
    def collect_log_bundle(
        is_64_bit: bool,
        output_dir: Path,
    ) -> Optional[Path]:
        """
        Collect a client log bundle using ``nsdiag.exe -o``.

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

