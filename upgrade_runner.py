"""
Core upgrade orchestration logic for the Netskope Client Upgrade Tool.
Each public method implements a complete upgrade scenario end-to-end.
"""

import json
import logging
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from util_client import (
    LocalClient, SERVICES,
    ExeValidationResult, UninstallEntryResult,
)
from util_config import UpgradeConfig
from util_log import LOG_DIR, build_log_dir_name, rename_log_dir, setup_folder_logging
from util_webui import WebUIClient

BASE_VERSION_DIR = Path(__file__).parent / "data" / "base_version"
INSTALLER_JSON = Path(__file__).parent / "data" / "installer.json"


def _version_key(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints for sorting."""
    return tuple(int(x) for x in version.split("."))

log = logging.getLogger(__name__)


@dataclass
class UpgradeResult:
    """Result of an upgrade scenario run."""
    success: bool
    scenario: str
    version_before: str
    version_after: str
    expected_version: str
    webui_version: str
    elapsed_seconds: float
    message: str
    service_running: bool = True
    exe_validation: Optional[ExeValidationResult] = None
    uninstall_entry: Optional[UninstallEntryResult] = None


@dataclass
class PollResult:
    """Result of version polling."""
    changed: bool
    final_version: str
    elapsed_seconds: float
    crash_detected: bool = False


class UpgradeRunner:
    """
    Orchestrates client auto-upgrade scenarios.

    Coordinates between WebUI API (tenant-side config) and
    local client operations (install, version check, config pull).
    """

    def __init__(
        self,
        webui: WebUIClient,
        client: LocalClient,
        upgrade_cfg: UpgradeConfig,
        config_name: str = "",
        host_name: Optional[str] = None,
        email: Optional[str] = None,
        source_64_bit: bool = False,
        target_64_bit: bool = False,
        reboot_time: Optional[int] = None,
        reboot_delay: int = 5,
        stop_event: Optional[threading.Event] = None,
        log_dir: Optional[Path] = None,
    ) -> None:
        """
        Initialize the upgrade runner.

        :param webui: Authenticated WebUI API client.
        :param client: Initialized local client wrapper.
        :param upgrade_cfg: Polling and timing configuration.
        :param config_name: Client configuration name on the tenant
                            (e.g. 'Default tenant config'). Passed as
                            search_config to WebUI API calls.
        :param host_name: Device hostname for WebUI verification.
        :param email: User email for WebUI verification.
        :param source_64_bit: Whether the base (source) install is 64-bit.
        :param target_64_bit: Whether the upgrade target is 64-bit.
        :param reboot_time: Timing number (1-11) that triggers a reboot.
        :param reboot_delay: Seconds before reboot after timing fires.
        :param stop_event: Threading event for graceful shutdown (ESC key).
        :param log_dir: Pre-created log folder (from main.py).
        """
        self.webui = webui
        self.client = client
        self.cfg = upgrade_cfg
        self.config_name = config_name
        self.host_name = host_name or socket.gethostname()
        self.email = email or client.email
        self.source_64_bit = source_64_bit
        self.target_64_bit = target_64_bit
        self.reboot_time = reboot_time
        self.reboot_delay = reboot_delay
        self.stop_event = stop_event or threading.Event()
        self._cloned_installer: Optional[Path] = None
        self._gmail_browser: Any = None
        self._upgrade_enabled = False
        self._log_dir: Optional[Path] = log_dir

    # ── Upgrade Scenarios ────────────────────────────────────────────

    def run_upgrade_to_latest(
        self,
        from_version: Optional[str] = None,
        invite_email: Optional[str] = None,
    ) -> UpgradeResult:
        """
        Scenario: Ensure client is installed, enable auto-upgrade to
        latest, wait and verify upgrade completes.

        :param from_version: Build version for download fallback
                             (e.g. '123.0.0'). Not needed when a
                             local installer exists in data/base_version/.
        :param invite_email: Email to send enrollment invite before install.
        :return: UpgradeResult with outcome details.
        """
        scenario = "upgrade_to_latest"
        start_time = time.time()
        log.info("=" * 70)
        log.info("SCENARIO: Upgrade to Latest Release")
        log.info("  config_name: %s", self.config_name or "(default)")
        log.info("  from_version: %s", from_version)
        log.info("=" * 70)
        try:
            # Phase 1: Ensure base client is installed (no nsclient needed)
            self._ensure_client_installed(from_version, invite_email)
            self._sync_and_detect_config()

            # Phase 2: Init nsclient + read version
            nsclient_ok = self._init_nsclient()
            version_before = self._get_current_version()
            log.info("Version before upgrade: %s", version_before)

            # Get expected target version
            all_versions = self.webui.get_release_versions()
            expected = self._apply_64bit_suffix(
                all_versions["latestversion"],
            )
            log.info("Target latest version: %s", expected)

            # Create scenario log folder now that versions are known
            self._create_log_dir(version_before, expected)

            # Trigger upgrade via WebUI
            self.webui.disable_auto_upgrade(search_config=self.config_name)
            self.webui.set_update_win64bit(
                enable=self.target_64_bit, search_config=self.config_name,
            )
            self.webui.enable_upgrade_latest(search_config=self.config_name)
            self._upgrade_enabled = True
            self.client.sync_config_from_tenant(
                is_64_bit=self.source_64_bit, wait_seconds=10,
            )

            # Start timing monitor and wait for upgrade to complete
            monitor = self._start_monitor()
            completed = monitor.wait_for_upgrade_complete(
                timeout=self.cfg.max_wait_seconds,
            )

            # Check for crash dumps
            crash_found, zero_count = LocalClient.check_crash_dumps()
            if zero_count > 0:
                log.info("Cleaned %d zero-byte dump files", zero_count)
            if crash_found:
                log.error("Crash dump detected during upgrade!")
                effective_64 = self.target_64_bit or self.source_64_bit
                log_dir = self._log_dir or LOG_DIR
                LocalClient.handle_crash(effective_64, log_dir)

            version_after = self._get_current_version()

            # Stop monitor and print timing report
            self._stop_monitor(monitor)

            # Post-upgrade checks
            service_running = self._verify_service_running()
            validation_ok, exe_validation, uninstall_entry = (
                self._validate_pre_report(version_after)
            )
            webui_version = self._verify_webui_version(version_after)

            elapsed = time.time() - start_time
            version_ok = version_after == expected
            success = (
                version_ok and service_running
                and validation_ok and not crash_found
            )
            message = (
                f"Upgrade successful: {version_before} -> {version_after}"
                if version_ok
                else f"Upgrade FAILED: expected {expected}, got {version_after}"
            )
            if crash_found:
                message += " — CRASH DUMP DETECTED"
            message += self._format_validation_issues(
                service_running, exe_validation, uninstall_entry,
            )
            log.info(message)

            result = UpgradeResult(
                success=success,
                scenario=scenario,
                version_before=version_before,
                version_after=version_after,
                expected_version=expected,
                webui_version=webui_version,
                elapsed_seconds=elapsed,
                message=message,
                service_running=service_running,
                exe_validation=exe_validation,
                uninstall_entry=uninstall_entry,
            )
            if not result.success:
                self._collect_failure_logs()
            return result

        except Exception as exc:
            elapsed = time.time() - start_time
            log.exception("Scenario %s failed with exception", scenario)
            self._collect_failure_logs()
            return UpgradeResult(
                success=False,
                scenario=scenario,
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                webui_version="unknown",
                elapsed_seconds=elapsed,
                message=f"Exception: {exc}",
                service_running=False,
            )
        finally:
            self._cleanup()

    def run_upgrade_to_golden(
        self,
        from_version: Optional[str] = None,
        dot: bool = False,
        invite_email: Optional[str] = None,
    ) -> UpgradeResult:
        """
        Scenario: Ensure client is installed, enable auto-upgrade to the
        latest golden release, wait and verify.

        :param from_version: Build version for download fallback. If None,
                             auto-picks a version older than the target golden.
        :param dot: If True, enable dot release within the golden version.
        :param invite_email: Email to send enrollment invite before install.
        :return: UpgradeResult with outcome details.
        """
        scenario = f"upgrade_to_golden(dot={dot})"
        start_time = time.time()
        log.info("=" * 70)
        log.info("SCENARIO: Upgrade to Golden Release")
        log.info("  config_name: %s", self.config_name or "(default)")
        log.info("  dot: %s, from_version: %s", dot, from_version)
        log.info("=" * 70)
        try:
            # Resolve latest golden version for auto-pick before install
            all_versions = self.webui.get_release_versions()
            golden_versions_sorted = sorted(all_versions["goldenversions"], key=_version_key)
            golden_version = golden_versions_sorted[-1]
            log.info("Selected golden version: %s", golden_version)

            # Auto-pick from_version for download fallback if not provided
            if from_version is None:
                version_list = self.webui.get_sorted_version_list()
                older_candidates = [
                    v for v in version_list
                    if int(v.split(".")[0]) < int(golden_version.split(".")[0])
                ]
                if older_candidates:
                    from_version = max(older_candidates)
                    log.info("Auto-picked from_version: %s", from_version)

            # Phase 1: Ensure base client is installed (no nsclient needed)
            self._ensure_client_installed(from_version, invite_email)
            self._sync_and_detect_config()

            # Phase 2: Init nsclient + read version
            nsclient_ok = self._init_nsclient()
            version_before = self._get_current_version()
            log.info("Version before upgrade: %s", version_before)

            # Determine expected version after upgrade
            if dot:
                expected = sorted(all_versions[golden_version], key=_version_key)[-1]
            else:
                expected = sorted(all_versions[golden_version], key=_version_key)[0]
            expected = self._apply_64bit_suffix(expected)
            log.info("Expected version after upgrade: %s", expected)

            # Create scenario log folder now that versions are known
            self._create_log_dir(version_before, expected)

            # Trigger golden upgrade via WebUI
            self.webui.disable_auto_upgrade(search_config=self.config_name)
            self.webui.set_update_win64bit(
                enable=self.target_64_bit, search_config=self.config_name,
            )
            self.webui.enable_upgrade_golden(
                golden_version, dot=dot, search_config=self.config_name,
            )
            self._upgrade_enabled = True
            self.client.sync_config_from_tenant(
                is_64_bit=self.source_64_bit, wait_seconds=10,
            )

            # Start timing monitor and wait for upgrade to complete
            monitor = self._start_monitor()
            completed = monitor.wait_for_upgrade_complete(
                timeout=self.cfg.max_wait_seconds,
            )

            # Check for crash dumps
            crash_found, zero_count = LocalClient.check_crash_dumps()
            if zero_count > 0:
                log.info("Cleaned %d zero-byte dump files", zero_count)
            if crash_found:
                log.error("Crash dump detected during upgrade!")
                effective_64 = self.target_64_bit or self.source_64_bit
                log_dir = self._log_dir or LOG_DIR
                LocalClient.handle_crash(effective_64, log_dir)

            version_after = self._get_current_version()

            # Stop monitor and print timing report
            self._stop_monitor(monitor)

            # Post-upgrade checks
            service_running = self._verify_service_running()
            validation_ok, exe_validation, uninstall_entry = (
                self._validate_pre_report(version_after)
            )
            webui_version = self._verify_webui_version(version_after)

            elapsed = time.time() - start_time
            version_ok = version_after == expected
            success = (
                version_ok and service_running
                and validation_ok and not crash_found
            )
            message = (
                f"Golden upgrade successful: {version_before} -> {version_after}"
                if version_ok
                else f"Golden upgrade FAILED: expected {expected}, got {version_after}"
            )
            if crash_found:
                message += " — CRASH DUMP DETECTED"
            message += self._format_validation_issues(
                service_running, exe_validation, uninstall_entry,
            )
            log.info(message)

            result = UpgradeResult(
                success=success,
                scenario=scenario,
                version_before=version_before,
                version_after=version_after,
                expected_version=expected,
                webui_version=webui_version,
                elapsed_seconds=elapsed,
                message=message,
                service_running=service_running,
                exe_validation=exe_validation,
                uninstall_entry=uninstall_entry,
            )
            if not result.success:
                self._collect_failure_logs()
            return result

        except Exception as exc:
            elapsed = time.time() - start_time
            log.exception("Scenario %s failed with exception", scenario)
            self._collect_failure_logs()
            return UpgradeResult(
                success=False,
                scenario=scenario,
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                webui_version="unknown",
                elapsed_seconds=elapsed,
                message=f"Exception: {exc}",
                service_running=False,
            )
        finally:
            self._cleanup()

    def run_upgrade_disabled(
        self,
        from_version: Optional[str] = None,
        invite_email: Optional[str] = None,
    ) -> UpgradeResult:
        """
        Scenario: Ensure client is installed with auto-upgrade disabled,
        verify the client does NOT upgrade.

        :param from_version: Build version for download fallback
                             (e.g. '123.0.0'). Not needed when a
                             local installer exists in data/base_version/.
        :param invite_email: Email to send enrollment invite before install.
        :return: UpgradeResult with outcome details.
        """
        scenario = "upgrade_disabled"
        start_time = time.time()
        log.info("=" * 70)
        log.info("SCENARIO: Auto-Upgrade Disabled Verification")
        log.info("  config_name: %s", self.config_name or "(default)")
        log.info("  from_version: %s", from_version)
        log.info("=" * 70)
        try:
            # Phase 1: Ensure base client is installed (no nsclient needed)
            self._ensure_client_installed(from_version, invite_email)
            self._sync_and_detect_config()

            # Phase 2: Init nsclient + read version
            nsclient_ok = self._init_nsclient()
            version_before = self._get_current_version()
            log.info("Version before: %s", version_before)

            # Create scenario log folder
            self._create_log_dir(version_before, "disabled")

            # Disable auto-upgrade and verify it stays
            self.webui.disable_auto_upgrade(search_config=self.config_name)
            if nsclient_ok:
                self.client.update_config(wait_seconds=self.cfg.config_update_wait_seconds)
            else:
                log.info("Skipping local config pull (nsclient not available)")

            # Wait the full polling period — version should NOT change
            log.info(
                "Waiting %d seconds to confirm no upgrade occurs...",
                self.cfg.max_wait_seconds,
            )
            poll = self._wait_for_upgrade(
                expected_version=None,
                timeout_override=self.cfg.max_wait_seconds,
            )
            version_after = poll.final_version

            # Post checks
            service_running = self._verify_service_running()
            validation_ok, exe_validation, uninstall_entry = (
                self._validate_pre_report(
                    version_after, is_64_bit=self.source_64_bit,
                )
            )
            webui_version = self._verify_webui_version(version_after)

            elapsed = time.time() - start_time
            version_ok = version_before == version_after
            success = (
                version_ok and service_running
                and validation_ok and not poll.crash_detected
            )
            message = (
                f"Correctly stayed at {version_before} — auto-upgrade disabled works"
                if version_ok
                else (
                    f"UNEXPECTED upgrade occurred: {version_before} -> {version_after}"
                )
            )
            if poll.crash_detected:
                message += " — CRASH DUMP DETECTED"
            message += self._format_validation_issues(
                service_running, exe_validation, uninstall_entry,
            )
            log.info(message)

            result = UpgradeResult(
                success=success,
                scenario=scenario,
                version_before=version_before,
                version_after=version_after,
                expected_version=version_before,  # Expected to stay same
                webui_version=webui_version,
                elapsed_seconds=elapsed,
                message=message,
                service_running=service_running,
                exe_validation=exe_validation,
                uninstall_entry=uninstall_entry,
            )
            if not result.success:
                self._collect_failure_logs()
            return result

        except Exception as exc:
            elapsed = time.time() - start_time
            log.exception("Scenario %s failed with exception", scenario)
            self._collect_failure_logs()
            return UpgradeResult(
                success=False,
                scenario=scenario,
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                webui_version="unknown",
                elapsed_seconds=elapsed,
                message=f"Exception: {exc}",
                service_running=False,
            )
        finally:
            self._cleanup()

    # ── Timing Monitor ───────────────────────────────────────────────

    def _start_monitor(self) -> Any:
        """Start timing monitor for upgrade lifecycle detection."""
        from util_monitor import TimingMonitor

        monitor = TimingMonitor(
            target_64_bit=self.target_64_bit,
            reboot_time=self.reboot_time,
            reboot_delay=self.reboot_delay,
            log_dir=str(self._log_dir) if self._log_dir else "",
        )
        monitor.start()

        # Bridge the runner's ESC stop_event to the monitor so
        # pressing ESC stops the monitor promptly.
        def _esc_bridge() -> None:
            self.stop_event.wait()
            monitor.stop()

        bridge = threading.Thread(target=_esc_bridge, daemon=True)
        bridge.start()

        return monitor

    def _stop_monitor(self, monitor: Optional[Any]) -> None:
        """Stop timing monitor and print report."""
        if monitor is None:
            return
        monitor.stop()
        monitor.print_report()

    # ── Shared Helpers ───────────────────────────────────────────────

    def _ensure_client_installed(
        self,
        from_version: Optional[str] = None,
        invite_email: Optional[str] = None,
    ) -> None:
        """
        Ensure the Netskope Client is installed at the correct base
        version and running (Phase 1).

        Compares the installed version (from registry) against the base
        MSI's Subject field:

        - **(0) Different version installed** — uninstall via msiexec /x,
          then do full install flow.
        - **(1) Same version and service running** — skip install, go
          straight to upgrade.
        - **(2) Same version but service not running** — uninstall via
          msiexec /x, then do full install flow.
        - **(3) Not installed** — do full install flow.

        The install flow: send email invite (if requested), resolve
        installer (with optional tenant-specific rename), install via
        msiexec, wait for service.

        :param from_version: Build version for download fallback
                             (e.g. '123.0.0').
        :param invite_email: Email to send enrollment invite before install.
        """
        # Step 1: Find base installer for version comparison
        base_filename = self.client.get_installer_filename(is_64_bit=self.source_64_bit)
        base_installer = self._find_base_installer(base_filename)

        # Step 2: Read MSI subject to get base version
        # The Subject field may include a product name prefix
        # (e.g. "Netskope Client 135.0.0.2631") — strip it to get
        # the bare version for comparison with registry DisplayVersion.
        msi_version = ""
        if base_installer:
            raw_subject = self.client.get_msi_subject(base_installer)
            if raw_subject:
                msi_version = raw_subject.rsplit(" ", 1)[-1] if " " in raw_subject else raw_subject
                log.info("Base MSI version (subject): %s (raw: %s)", msi_version, raw_subject)

        # Step 3: Check current installation state
        uninstall_info = self.client.check_uninstall_registry()
        service_running = self.client.is_service_running()

        if uninstall_info.found and msi_version:
            installed_version = uninstall_info.display_version
            log.info(
                "Installed: %s (running=%s), base MSI: %s",
                installed_version, service_running, msi_version,
            )
            if installed_version == msi_version and service_running:
                # Case 1: Same version and running — skip install
                log.info(
                    "Installed version matches base MSI and running "
                    "— skipping install"
                )
                return
            elif installed_version == msi_version:
                # Case 2: Same version but not running — uninstall first
                log.info(
                    "Same version but not running — uninstalling "
                    "before reinstall"
                )
                self.client.uninstall_msi(uninstall_info.product_code)
                time.sleep(10)
            else:
                # Case 0: Different version — uninstall first
                log.info(
                    "Installed version %s differs from base MSI %s "
                    "— uninstalling first",
                    installed_version, msi_version,
                )
                self.client.uninstall_msi(uninstall_info.product_code)
                time.sleep(10)
        elif uninstall_info.found:
            # Installed but no MSI version to compare — fall back to
            # service check
            if service_running:
                log.info(
                    "Client running (no MSI version to compare) "
                    "— skipping install"
                )
                return
            log.info(
                "Client installed but not running (no MSI version) "
                "— uninstalling"
            )
            self.client.uninstall_msi(uninstall_info.product_code)
            time.sleep(10)
        else:
            # Case 3: Not installed
            log.info("No existing installation found")

        # Step 4: Full install flow
        log.info("Installing base client")

        # Get download link from email invite (also sends the invite)
        installer_name = None
        if invite_email:
            download_link = self._fetch_download_link_from_gmail(
                invite_email
            )
            if not download_link:
                print("\n" + "=" * 60)
                print(
                    "Auto-email extraction failed. "
                    "Open the email and copy the download link."
                )
                print(
                    "Example: "
                    "https://download-tenant.example.com/dlr/win/TOKEN"
                )
                print("=" * 60)
                download_link = input(
                    "Paste the download link here: "
                ).strip()
            if download_link:
                installer_name = self._get_installer_name(download_link)
                if installer_name:
                    print(f"Installer name: {installer_name}")
            else:
                log.info(
                    "No download link provided — using base installer name"
                )

        # Resolve base installer and copy to tenant-specific name
        installer = self._resolve_installer(base_filename, installer_name)
        if installer_name and installer:
            self._cloned_installer = installer

        if not installer and from_version:
            log.info("No local installer — downloading build (requires nsclient)")
            self._init_nsclient()
            build_version = (
                f"release-{from_version}"
                if not from_version.startswith("release-")
                else from_version
            )
            info = self.client.download_build(
                build_version=build_version,
                installer_filename=base_filename,
            )
            installer = Path(info["location"])

        if not installer:
            raise FileNotFoundError(
                f"No installer found in {BASE_VERSION_DIR} and "
                "--from-version not provided"
            )

        # Wait for the tenant to register the token before installing
        if installer_name:
            log.info("Waiting 15s for tenant to accept the token")
            time.sleep(15)

        # Install with msiexec
        try:
            self.client.install_msi(str(installer), log_dir=self._log_dir)
        finally:
            self._close_gmail_browser()

        # Wait for service to start
        if not self.client.wait_for_service():
            raise RuntimeError(
                "Client service (stAgentSvc) did not start after installation"
            )

    def _fetch_download_link_from_gmail(
        self, invite_email: str,
    ) -> str:
        """
        Send an email invite and auto-extract the download link from Gmail.

        Flow: connect Chrome → count existing emails → send invite →
        wait for delivery → search only new emails.

        Returns the URL on success, or an empty string on any failure
        (caller falls back to the manual input prompt).
        """
        invite_sent = False
        try:
            from util_email import GmailBrowser

            self._gmail_browser = GmailBrowser(
                email_address=invite_email,
                is_64_bit=self.source_64_bit,
                tenant_hostname=self.webui.hostname,
                stop_event=self.stop_event,
            )
            self._gmail_browser.connect()
            old_count = self._gmail_browser.count_matching_emails()
            log.info(
                "Found %d existing email(s) — will skip these",
                old_count,
            )

            log.info("Sending email invite to %s", invite_email)
            self.webui.send_email_invite(invite_email)
            invite_sent = True

            log.info("Waiting 10s for invite email to arrive")
            time.sleep(10)

            url = self._gmail_browser.get_download_link(
                skip_count=old_count,
            )
            log.info("Auto-extracted download link: %s", url)
            return url
        except Exception:
            log.warning(
                "Auto-email extraction failed — falling back to "
                "manual input",
                exc_info=True,
            )
            if not invite_sent:
                log.info("Sending email invite to %s", invite_email)
                self.webui.send_email_invite(invite_email)
            return ""

    def _close_gmail_browser(self) -> None:
        """Close the Gmail browser session if one is open."""
        if self._gmail_browser is not None:
            try:
                self._gmail_browser.close()
            except Exception:
                pass
            self._gmail_browser = None

    @staticmethod
    def _find_base_installer(base_filename: str) -> Optional[Path]:
        """
        Find the base installer file in data/base_version/ without
        renaming or copying anything.

        :param base_filename: Expected installer name (e.g. 'STAgent.msi').
        :return: Path to the installer, or None if not found.
        """
        if not BASE_VERSION_DIR.is_dir():
            return None
        base = BASE_VERSION_DIR / base_filename
        if base.is_file():
            return base
        # Single-file fallback
        files = [f for f in BASE_VERSION_DIR.iterdir() if f.is_file()]
        if len(files) == 1:
            return files[0]
        return None

    @staticmethod
    def _extract_token_from_url(download_link: str) -> str:
        """
        Extract the download token from an email invite download link.

        Example URL:
          https://download-exploratory2.stg.boomskope.com/dlr/win/QO848Vt80sc...
        Returns:
          'QO848Vt80sc...'

        :param download_link: Full download URL from the email invite.
        :return: Token string (last path segment).
        :raises ValueError: If the URL has no extractable token.
        """
        from urllib.parse import urlparse

        path = urlparse(download_link.strip()).path.rstrip("/")
        token = path.rsplit("/", 1)[-1] if "/" in path else ""
        if not token:
            raise ValueError(
                f"Could not extract token from download link: {download_link}"
            )
        return token

    def _get_installer_name(self, download_link: str) -> Optional[str]:
        """
        Compose the tenant-specific installer name from data/installer.json
        and the download token extracted from the email invite link.

        The JSON maps tenant hostnames to an installer name prefix.
        The full filename is: {prefix}_{token}_.msi

        :param download_link: Full download URL from the email invite.
        :return: Full installer filename, or None if no config for tenant.
        """
        if not INSTALLER_JSON.is_file():
            log.info("No installer.json found — using base installer name")
            return None

        try:
            token = self._extract_token_from_url(download_link)
            data = json.loads(INSTALLER_JSON.read_text(encoding="utf-8"))
            tenant = self.webui.hostname
            entry = data.get(tenant)
            if entry and "installer_name" in entry:
                prefix = entry["installer_name"]
                name = f"{prefix}_{token}_.msi"
                log.info("Composed installer name: %s", name)
                return name
            log.info(
                "No installer config for tenant %s in installer.json",
                tenant,
            )
            return None
        except Exception as exc:
            log.warning("Failed to read installer.json: %s", exc)
            return None

    @staticmethod
    def _resolve_installer(
        base_filename: str,
        installer_name: Optional[str] = None,
    ) -> Optional[Path]:
        """
        Resolve the installer file from data/base_version/.

        If installer_name is provided (from installer.json), copies the
        base installer to that name. Otherwise falls back to finding the
        base installer directly.

        :param base_filename: Base installer name (e.g. 'STAgent.msi').
        :param installer_name: Tenant-specific installer name, or None.
        :return: Path to the installer ready for msiexec, or None.
        """
        if not BASE_VERSION_DIR.is_dir():
            return None

        # Find the base installer file
        base = BASE_VERSION_DIR / base_filename
        if not base.is_file():
            # Single-file fallback: copy it to the base name first
            files = [f for f in BASE_VERSION_DIR.iterdir() if f.is_file()]
            if len(files) == 1:
                source = files[0]
                log.info("Copying %s -> %s", source.name, base_filename)
                shutil.copy2(str(source), str(base))
            else:
                return None

        # If tenant-specific name given, copy base to that name
        if installer_name:
            target = BASE_VERSION_DIR / installer_name
            log.info(
                "Copying base installer %s -> %s",
                base_filename, installer_name,
            )
            shutil.copy2(str(base), str(target))
            return target

        # No tenant-specific name — use the base installer directly
        return base

    def _wait_for_upgrade(
        self,
        expected_version: Optional[str] = None,
        timeout_override: Optional[int] = None,
    ) -> PollResult:
        """
        Poll the local client version until it changes (or matches expected).

        Also monitors for crash dumps and the stop_event (ESC key) each
        polling cycle.

        :param expected_version: Specific version to wait for. If None,
                                 detects any version change.
        :param timeout_override: Override max wait time in seconds.
        :return: PollResult with final state.
        """
        timeout = timeout_override or self.cfg.max_wait_seconds
        interval = self.cfg.poll_interval_seconds
        initial_version = self._get_current_version()

        log.info(
            "Polling for upgrade — current: %s, expected: %s, "
            "timeout: %ds, interval: %ds",
            initial_version,
            expected_version or "(any change)",
            timeout,
            interval,
        )

        elapsed = 0.0
        start = time.time()

        while elapsed < timeout:
            # Use stop_event.wait() instead of time.sleep() for
            # responsive ESC-key cancellation
            if self.stop_event.wait(timeout=interval):
                log.warning("Stop event detected — aborting upgrade wait")
                break

            elapsed = time.time() - start

            # Check for crash dumps each cycle
            crash_found, zero_count = LocalClient.check_crash_dumps()
            if zero_count > 0:
                log.info("Cleaned %d zero-byte dump files", zero_count)
            if crash_found:
                log.error("Crash dump detected during upgrade polling!")
                effective_64 = self.target_64_bit or self.source_64_bit
                log_dir = self._log_dir or LOG_DIR
                LocalClient.handle_crash(effective_64, log_dir)
                final = self._get_current_version()
                return PollResult(
                    changed=(final != initial_version),
                    final_version=final,
                    elapsed_seconds=elapsed,
                    crash_detected=True,
                )

            current = self._get_current_version()

            if expected_version and current == expected_version:
                log.info(
                    "Version matched expected %s after %.0fs",
                    expected_version, elapsed,
                )
                return PollResult(
                    changed=True, final_version=current,
                    elapsed_seconds=elapsed,
                )

            if expected_version is None and current != initial_version:
                log.info(
                    "Version changed from %s to %s after %.0fs",
                    initial_version, current, elapsed,
                )
                return PollResult(
                    changed=True, final_version=current,
                    elapsed_seconds=elapsed,
                )

            log.debug(
                "Poll: version=%s, elapsed=%.0fs/%ds",
                current, elapsed, timeout,
            )

        final = self._get_current_version()
        log.warning(
            "Polling timed out after %ds — final version: %s",
            timeout, final,
        )
        return PollResult(
            changed=(final != initial_version),
            final_version=final, elapsed_seconds=elapsed,
        )

    def _init_nsclient(self) -> bool:
        """
        Lazily initialize the nsclient library instance.

        Called at the Phase 2 boundary — after the client service is
        confirmed running. Skips if already initialized.

        :return: True if nsclient is available, False otherwise.
        """
        if self.client.is_initialized:
            return True
        try:
            self.client.create(
                platform=self.client.platform,
                email=self.email,
                password="",
                stack=None,
                tenant_name="",
            )
            return True
        except ModuleNotFoundError:
            log.warning(
                "nsclient package not installed — version monitoring "
                "will use WebUI only"
            )
            return False

    def _get_current_version(self) -> str:
        """
        Get the current client version via nsclient, local exe, or WebUI.

        Tries in order:
        1. nsclient library (if initialized)
        2. Local stAgentSvc.exe ProductVersion (always available)
        3. WebUI device version API (may be stale)

        :return: Version string, or 'unknown' if all methods fail.
        """
        if self.client.is_initialized:
            return self.client.get_version()
        # Try local exe version — more reliable than WebUI
        local_ver = self._get_local_exe_version()
        if local_ver:
            return local_ver
        try:
            return self.webui.get_device_version(
                host_name=self.host_name, email=self.email,
            )
        except Exception as exc:
            log.warning("Failed to get version from WebUI: %s", exc)
            return "unknown"

    def _get_local_exe_version(self) -> str:
        """
        Read the installed client version from the local stAgentSvc.exe.

        Checks both 64-bit and 32-bit install directories.

        :return: Version string (with ' (64-bit)' suffix if from 64-bit
                 path), or empty string if exe not found.
        """
        for is_64, suffix in [(True, " (64-bit)"), (False, "")]:
            install_dir = LocalClient.get_install_dir(is_64)
            exe = install_dir / "stAgentSvc.exe"
            if exe.is_file():
                ver = LocalClient.get_file_version(exe)
                if ver:
                    return f"{ver}{suffix}" if suffix else ver
        return ""

    def _verify_service_running(self) -> bool:
        """
        Confirm the client service is still running after upgrade.

        :return: True if service is running.
        """
        running = self.client.is_service_running()
        if not running:
            log.warning("Service stAgentSvc not running after upgrade")
        else:
            log.info("Service stAgentSvc confirmed running after upgrade")
        return running

    def _verify_webui_version(self, expected_local: str) -> str:
        """
        Check that the WebUI device page reports the same version.

        :param expected_local: The version we expect WebUI to show.
        :return: WebUI-reported version string.
        """
        try:
            webui_version = self.webui.get_device_version(
                host_name=self.host_name, email=self.email,
            )
            if webui_version != expected_local:
                log.warning(
                    "WebUI version mismatch: local=%s, webui=%s",
                    expected_local, webui_version,
                )
            else:
                log.info("WebUI version matches local: %s", webui_version)
            return webui_version
        except Exception as exc:
            log.warning("Failed to verify WebUI version: %s", exc)
            return "error"

    def _apply_64bit_suffix(
        self,
        version: str,
        is_64_bit: Optional[bool] = None,
    ) -> str:
        """
        Append ' (64-bit)' to a version string when targeting 64-bit.

        The WebUI API returns bare version numbers (e.g. '136.0.4.2612'),
        but the installed 64-bit client reports '136.0.4.2612 (64-bit)'.

        :param version: Base version string from the API.
        :param is_64_bit: Override bitness flag (defaults to self.target_64_bit).
        :return: Version with suffix if 64-bit, unchanged otherwise.
        """
        use_64 = is_64_bit if is_64_bit is not None else self.target_64_bit
        if use_64 and not version.endswith("(64-bit)"):
            return f"{version} (64-bit)"
        return version

    def _validate_pre_report(
        self,
        version_after: str,
        is_64_bit: Optional[bool] = None,
    ) -> tuple[bool, ExeValidationResult, UninstallEntryResult]:
        """
        Run pre-report validation: executables and uninstall registry.

        Called after upgrade polling completes but before the final
        result is assembled. Checks:
        1. Required executables exist in the correct architecture
           path with the expected version.
        2. Windows uninstall registry entry exists.

        :param version_after: The version to validate against.
        :param is_64_bit: Bitness for install dir lookup (defaults
                          to target_64_bit).
        :return: (all_valid, exe_result, uninstall_result).
        """
        use_64 = is_64_bit if is_64_bit is not None else self.target_64_bit
        exe_validation = self.client.verify_executables(
            is_64_bit=use_64,
            expected_version=version_after,
        )
        if not exe_validation.valid:
            log.warning(
                "Pre-report exe validation failed: missing=%s, mismatches=%s",
                exe_validation.missing, exe_validation.version_mismatches,
            )

        uninstall_entry = self.client.check_uninstall_registry()
        if not uninstall_entry.found:
            log.warning("Pre-report validation: uninstall registry entry not found")

        valid = exe_validation.valid and uninstall_entry.found
        return valid, exe_validation, uninstall_entry

    @staticmethod
    def _format_validation_issues(
        service_running: bool,
        exe_validation: Optional[ExeValidationResult],
        uninstall_entry: Optional[UninstallEntryResult],
    ) -> str:
        """Build a suffix string listing validation issues, if any."""
        issues: list[str] = []
        if not service_running:
            issues.append("service not running")
        if exe_validation and not exe_validation.valid:
            if exe_validation.missing:
                issues.append(f"missing exe: {', '.join(exe_validation.missing)}")
            if exe_validation.version_mismatches:
                issues.append(
                    f"exe version mismatch: {', '.join(exe_validation.version_mismatches)}"
                )
        if uninstall_entry and not uninstall_entry.found:
            issues.append("uninstall registry entry missing")
        if issues:
            return f" — ISSUES: {', '.join(issues)}"
        return ""

    def _sync_and_detect_config(self) -> None:
        """
        Sync config from tenant and re-detect config_name.

        After a fresh install, nsconfig.json may not yet have the
        ``configurationName``.  Running ``nsdiag -u`` forces a pull,
        then we re-read nsconfig.json to pick up the correct name.
        Skips entirely when config_name is already set.
        """
        if self.config_name:
            return
        log.info("config_name is empty — syncing config from tenant")
        self.client.sync_config_from_tenant(is_64_bit=self.source_64_bit)
        ns_info = self.client.detect_tenant_from_nsconfig()
        if ns_info and ns_info.config_name:
            self.config_name = ns_info.config_name
            log.info("Detected config_name after sync: %s", self.config_name)
        else:
            log.warning(
                "Could not detect config_name after sync — "
                "API calls will target the default config"
            )

    # ── Log folder & failure collection ────────────────────────────────

    def _create_log_dir(
        self, from_version: str, to_version: str,
    ) -> Path:
        """
        Rename the pre-created log folder to include version info.

        If no pre-created folder exists (e.g. tests), falls back to
        creating a new folder.

        :param from_version: Installed (source) version string.
        :param to_version: Target upgrade version string.
        :return: Path to the log folder.
        """
        dir_name = build_log_dir_name(
            from_version=from_version,
            to_version=to_version,
            target_64_bit=self.target_64_bit,
            reboot_time=self.reboot_time,
        )
        new_dir = LOG_DIR / dir_name

        if self._log_dir and self._log_dir.exists():
            self._log_dir = rename_log_dir(self._log_dir, new_dir)
        else:
            self._log_dir = new_dir
            setup_folder_logging(self._log_dir)

        log.info("Log folder: %s", self._log_dir)
        return self._log_dir

    def _collect_failure_logs(self) -> None:
        """Collect nsdiag log bundle when the final result is failure."""
        log_dir = self._log_dir or LOG_DIR
        effective_64 = self.target_64_bit or self.source_64_bit
        log.info("Collecting log bundle for failure analysis...")
        LocalClient.collect_log_bundle(effective_64, log_dir)

    @property
    def log_dir(self) -> Optional[Path]:
        """Return the scenario log directory, if created."""
        return self._log_dir

    def _cleanup(self) -> None:
        """Reset tenant config and remove cloned installer."""
        if self._upgrade_enabled:
            try:
                self.webui.disable_auto_upgrade(search_config=self.config_name)
                log.info("Cleanup: auto-upgrade disabled on tenant")
            except Exception as exc:
                log.warning("Cleanup failed: %s", exc)
            self._upgrade_enabled = False
        else:
            log.info("Cleanup: skipping tenant rollback (upgrade was never enabled)")

        if self._cloned_installer and self._cloned_installer.is_file():
            try:
                self._cloned_installer.unlink()
                log.info(
                    "Cleanup: deleted cloned installer %s",
                    self._cloned_installer.name,
                )
            except Exception as exc:
                log.warning(
                    "Failed to delete cloned installer: %s", exc,
                )
            self._cloned_installer = None
