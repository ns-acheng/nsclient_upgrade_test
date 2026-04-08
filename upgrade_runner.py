"""
Core upgrade orchestration logic for the Netskope Client Upgrade Tool.
Each public method implements a complete upgrade scenario end-to-end.
"""

import json
import logging
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from util_client import LocalClient, SERVICES
from util_config import (
    UpgradeConfig, RebootTestState,
    save_reboot_state, load_reboot_state, clear_reboot_state,
)
from util_webui import WebUIClient

BASE_VERSION_DIR = Path(__file__).parent / "data" / "base_version"
INSTALLER_JSON = Path(__file__).parent / "data" / "installer.json"

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


@dataclass
class PollResult:
    """Result of version polling."""
    changed: bool
    final_version: str
    elapsed_seconds: float


# Timing presets for reboot during upgrade (seconds)
REBOOT_TIMING_PRESETS: dict[str, int] = {
    "early": 30,    # During download/prep
    "mid":   60,    # During old service removal
    "late":  90,    # During new service/driver install
}


@dataclass
class RebootVerifyResult:
    """Result of the reboot-verify phase."""
    success: bool
    scenario: str
    version_before: str
    version_after: str
    expected_version: str
    upgrade_completed: bool
    rolled_back: bool
    services: dict[str, dict[str, Any]]
    watchdog_binpath: str
    watchdog_binpath_valid: bool
    install_dir_valid: bool
    elapsed_seconds: float
    message: str


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
        is_64_bit: bool = False,
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
        :param is_64_bit: Whether to use 64-bit client installer (Windows).
        """
        self.webui = webui
        self.client = client
        self.cfg = upgrade_cfg
        self.config_name = config_name
        self.host_name = host_name or socket.gethostname()
        self.email = email or client.email
        self.is_64_bit = is_64_bit

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
        if not self.config_name:
            log.warning(
                "config_name is empty — API calls will target the default "
                "tenant config. Set config_name via nsconfig.json or --config."
            )

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
            expected = all_versions["latestversion"]
            log.info("Target latest version: %s", expected)

            # Trigger upgrade via WebUI
            self.webui.disable_auto_upgrade(search_config=self.config_name)
            self.webui.set_update_win64bit(
                enable=self.is_64_bit, search_config=self.config_name,
            )
            self.webui.enable_upgrade_latest(search_config=self.config_name)
            self.webui.set_upgrade_schedule(
                minutes_from_now=2, search_config=self.config_name,
            )
            if nsclient_ok:
                self.client.update_config(wait_seconds=self.cfg.config_update_wait_seconds)
            else:
                log.info("Skipping local config pull (nsclient not available)")

            # Poll for upgrade
            poll = self._wait_for_upgrade(expected_version=expected)
            version_after = poll.final_version

            # Post-upgrade checks
            self._verify_service_running()
            webui_version = self._verify_webui_version(version_after)

            elapsed = time.time() - start_time
            success = version_after == expected
            message = (
                f"Upgrade successful: {version_before} -> {version_after}"
                if success
                else f"Upgrade FAILED: expected {expected}, got {version_after}"
            )
            log.info(message)

            return UpgradeResult(
                success=success,
                scenario=scenario,
                version_before=version_before,
                version_after=version_after,
                expected_version=expected,
                webui_version=webui_version,
                elapsed_seconds=elapsed,
                message=message,
            )

        except Exception as exc:
            elapsed = time.time() - start_time
            log.exception("Scenario %s failed with exception", scenario)
            return UpgradeResult(
                success=False,
                scenario=scenario,
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                webui_version="unknown",
                elapsed_seconds=elapsed,
                message=f"Exception: {exc}",
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
        if not self.config_name:
            log.warning(
                "config_name is empty — API calls will target the default "
                "tenant config. Set config_name via nsconfig.json or --config."
            )

        try:
            # Resolve latest golden version for auto-pick before install
            all_versions = self.webui.get_release_versions()
            golden_versions_sorted = sorted(all_versions["goldenversions"])
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
                expected = sorted(all_versions[golden_version])[-1]
            else:
                expected = sorted(all_versions[golden_version])[0]
            log.info("Expected version after upgrade: %s", expected)

            # Trigger golden upgrade via WebUI
            self.webui.disable_auto_upgrade(search_config=self.config_name)
            self.webui.set_update_win64bit(
                enable=self.is_64_bit, search_config=self.config_name,
            )
            self.webui.enable_upgrade_golden(
                golden_version, dot=dot, search_config=self.config_name,
            )
            self.webui.set_upgrade_schedule(
                minutes_from_now=2, search_config=self.config_name,
            )
            if nsclient_ok:
                self.client.update_config(wait_seconds=self.cfg.config_update_wait_seconds)
            else:
                log.info("Skipping local config pull (nsclient not available)")

            # Poll for upgrade
            poll = self._wait_for_upgrade(expected_version=expected)
            version_after = poll.final_version

            # Post-upgrade checks
            self._verify_service_running()
            webui_version = self._verify_webui_version(version_after)

            elapsed = time.time() - start_time
            success = version_after == expected
            message = (
                f"Golden upgrade successful: {version_before} -> {version_after}"
                if success
                else f"Golden upgrade FAILED: expected {expected}, got {version_after}"
            )
            log.info(message)

            return UpgradeResult(
                success=success,
                scenario=scenario,
                version_before=version_before,
                version_after=version_after,
                expected_version=expected,
                webui_version=webui_version,
                elapsed_seconds=elapsed,
                message=message,
            )

        except Exception as exc:
            elapsed = time.time() - start_time
            log.exception("Scenario %s failed with exception", scenario)
            return UpgradeResult(
                success=False,
                scenario=scenario,
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                webui_version="unknown",
                elapsed_seconds=elapsed,
                message=f"Exception: {exc}",
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
        if not self.config_name:
            log.warning(
                "config_name is empty — API calls will target the default "
                "tenant config. Set config_name via nsconfig.json or --config."
            )

        try:
            # Phase 1: Ensure base client is installed (no nsclient needed)
            self._ensure_client_installed(from_version, invite_email)
            self._sync_and_detect_config()

            # Phase 2: Init nsclient + read version
            nsclient_ok = self._init_nsclient()
            version_before = self._get_current_version()
            log.info("Version before: %s", version_before)

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

            # Post-upgrade checks
            self._verify_service_running()
            webui_version = self._verify_webui_version(version_after)

            elapsed = time.time() - start_time
            success = version_before == version_after
            message = (
                f"Correctly stayed at {version_before} — auto-upgrade disabled works"
                if success
                else (
                    f"UNEXPECTED upgrade occurred: {version_before} -> {version_after}"
                )
            )
            log.info(message)

            return UpgradeResult(
                success=success,
                scenario=scenario,
                version_before=version_before,
                version_after=version_after,
                expected_version=version_before,  # Expected to stay same
                webui_version=webui_version,
                elapsed_seconds=elapsed,
                message=message,
            )

        except Exception as exc:
            elapsed = time.time() - start_time
            log.exception("Scenario %s failed with exception", scenario)
            return UpgradeResult(
                success=False,
                scenario=scenario,
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                webui_version="unknown",
                elapsed_seconds=elapsed,
                message=f"Exception: {exc}",
            )
        finally:
            self._cleanup()

    # ── Shared Helpers ───────────────────────────────────────────────

    def _ensure_client_installed(
        self,
        from_version: Optional[str] = None,
        invite_email: Optional[str] = None,
    ) -> None:
        """
        Ensure the Netskope Client is installed and running (Phase 1).

        Uses only subprocess calls (msiexec, sc query) — no nsclient needed.
        If the service is already running, returns immediately.
        Otherwise: sends email invite, reads the tenant-specific installer
        name from data/installer.json, copies the base installer to that
        name, installs via msiexec, and waits for the service to start.

        :param from_version: Build version for download fallback
                             (e.g. '123.0.0').
        :param invite_email: Email to send enrollment invite before install.
        """
        if self.client.is_service_running():
            log.info("Client service already running — skipping install")
            return

        log.info("Client service not running — installing base version")

        # Send email invite before installation
        if invite_email:
            log.info("Sending email invite to %s", invite_email)
            self.webui.send_email_invite(invite_email)

        # Prompt user for the download link from the email invite
        installer_name = None
        if invite_email:
            print("\n" + "=" * 60)
            print("Email invite sent. Open the email and copy the download link.")
            print("Example: https://download-tenant.example.com/dlr/win/TOKEN")
            print("=" * 60)
            download_link = input("Paste the download link here: ").strip()
            if download_link:
                installer_name = self._get_installer_name(download_link)
                if installer_name:
                    print(f"Installer name: {installer_name}")
            else:
                log.info("No download link provided — using base installer name")

        # Resolve base installer and copy to tenant-specific name
        base_filename = self.client.get_installer_filename(is_64_bit=self.is_64_bit)
        installer = self._resolve_installer(base_filename, installer_name)

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

        # Install with msiexec
        self.client.install_msi(str(installer))

        # Wait for service to start
        if not self.client.wait_for_service():
            raise RuntimeError(
                "Client service (stAgentSvc) did not start after installation"
            )

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

        :param expected_version: Specific version to wait for. If None,
                                 detects any version change.
        :param timeout_override: Override max wait time in seconds.
        :return: PollResult with final state.
        """
        timeout = timeout_override or self.cfg.max_wait_seconds
        interval = self.cfg.poll_interval_seconds
        initial_version = self._get_current_version()

        log.info(
            "Polling for upgrade — current: %s, expected: %s, timeout: %ds, interval: %ds",
            initial_version,
            expected_version or "(any change)",
            timeout,
            interval,
        )

        elapsed = 0.0
        start = time.time()

        while elapsed < timeout:
            time.sleep(interval)
            elapsed = time.time() - start
            current = self._get_current_version()

            if expected_version and current == expected_version:
                log.info(
                    "Version matched expected %s after %.0fs",
                    expected_version, elapsed,
                )
                return PollResult(changed=True, final_version=current, elapsed_seconds=elapsed)

            if expected_version is None and current != initial_version:
                log.info(
                    "Version changed from %s to %s after %.0fs",
                    initial_version, current, elapsed,
                )
                return PollResult(changed=True, final_version=current, elapsed_seconds=elapsed)

            log.debug(
                "Poll: version=%s, elapsed=%.0fs/%ds",
                current, elapsed, timeout,
            )

        final = self._get_current_version()
        log.warning(
            "Polling timed out after %ds — final version: %s",
            timeout, final,
        )
        return PollResult(changed=(final != initial_version), final_version=final, elapsed_seconds=elapsed)

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
        Get the current client version via nsclient or WebUI fallback.

        :return: Version string, or 'unknown' if both methods fail.
        """
        if self.client.is_initialized:
            return self.client.get_version()
        try:
            return self.webui.get_device_version(
                host_name=self.host_name, email=self.email,
            )
        except Exception as exc:
            log.warning("Failed to get version from WebUI: %s", exc)
            return "unknown"

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
        self.client.sync_config_from_tenant(is_64_bit=self.is_64_bit)
        ns_info = self.client.detect_tenant_from_nsconfig()
        if ns_info and ns_info.config_name:
            self.config_name = ns_info.config_name
            log.info("Detected config_name after sync: %s", self.config_name)
        else:
            log.warning(
                "Could not detect config_name after sync — "
                "API calls will target the default config"
            )

    # ── Reboot-Interrupt Scenarios ──────────────────────────────────

    def run_reboot_interrupt_setup(
        self,
        target_type: str,
        reboot_timing: str,
        from_version: Optional[str] = None,
        dot: bool = False,
        invite_email: Optional[str] = None,
        target_64_bit: Optional[bool] = None,
        stabilize_wait: int = 300,
    ) -> UpgradeResult:
        """
        Phase 1: Prepare upgrade and schedule a reboot to interrupt it.

        Installs base client, enables auto-upgrade, saves state to
        ``data/reboot_state.json``, then triggers ``shutdown /r /f /t <delay>``.
        The tool process will be killed by the reboot.

        :param target_type: 'latest' or 'golden'.
        :param reboot_timing: 'early', 'mid', 'late', or seconds as string.
        :param from_version: Build version for download fallback.
        :param dot: Enable dot release (golden only).
        :param invite_email: Email to send enrollment invite.
        :param target_64_bit: Target bitness for upgrade (defaults to self.is_64_bit).
        :param stabilize_wait: Seconds to wait in verify phase after reboot.
        :return: UpgradeResult (setup outcome, not upgrade outcome).
        """
        scenario = f"reboot_interrupt_setup({target_type}, timing={reboot_timing})"
        start_time = time.time()
        t64 = target_64_bit if target_64_bit is not None else self.is_64_bit
        log.info("=" * 70)
        log.info("SCENARIO: Reboot-Interrupt Setup (Phase 1)")
        log.info("  target: %s, reboot_timing: %s", target_type, reboot_timing)
        log.info("  source_64_bit: %s, target_64_bit: %s", self.is_64_bit, t64)
        log.info("  config_name: %s", self.config_name or "(default)")
        log.info("=" * 70)

        try:
            # Phase 1: Ensure base client is installed
            self._ensure_client_installed(from_version, invite_email)
            self._sync_and_detect_config()

            # Phase 2: Init nsclient + read version
            nsclient_ok = self._init_nsclient()
            version_before = self._get_current_version()
            log.info("Version before upgrade: %s", version_before)

            # Determine expected version
            all_versions = self.webui.get_release_versions()
            if target_type == "latest":
                expected = all_versions["latestversion"]
            else:
                golden_versions_sorted = sorted(all_versions["goldenversions"])
                golden_version = golden_versions_sorted[-1]
                if dot:
                    expected = sorted(all_versions[golden_version])[-1]
                else:
                    expected = sorted(all_versions[golden_version])[0]
            log.info("Expected version after upgrade: %s", expected)

            # Enable upgrade on tenant
            self.webui.disable_auto_upgrade(search_config=self.config_name)
            self.webui.set_update_win64bit(
                enable=t64, search_config=self.config_name,
            )
            if target_type == "latest":
                self.webui.enable_upgrade_latest(search_config=self.config_name)
            else:
                self.webui.enable_upgrade_golden(
                    golden_version, dot=dot, search_config=self.config_name,
                )
            self.webui.set_upgrade_schedule(
                minutes_from_now=2, search_config=self.config_name,
            )
            if nsclient_ok:
                self.client.update_config(
                    wait_seconds=self.cfg.config_update_wait_seconds,
                )
            else:
                log.info("Skipping local config pull (nsclient not available)")

            # Save state for verify phase
            from datetime import datetime
            state = RebootTestState(
                scenario="reboot_interrupt",
                version_before=version_before,
                target_type=target_type,
                expected_version=expected,
                reboot_timing=reboot_timing,
                source_64_bit=self.is_64_bit,
                target_64_bit=t64,
                config_name=self.config_name,
                stabilize_wait=stabilize_wait,
                timestamp=datetime.now().isoformat(),
            )
            save_reboot_state(state)
            log.info("Reboot state saved — ready for reboot")

            # Resolve reboot delay
            if reboot_timing in REBOOT_TIMING_PRESETS:
                delay = REBOOT_TIMING_PRESETS[reboot_timing]
            else:
                delay = int(reboot_timing)
            log.info("Scheduling reboot in %d seconds", delay)

            # Trigger reboot
            subprocess.run(
                ["shutdown", "/r", "/f", "/t", str(delay)],
                capture_output=True, text=True, timeout=10,
            )
            log.info("Reboot scheduled — process will terminate when reboot occurs")

            elapsed = time.time() - start_time
            return UpgradeResult(
                success=True,
                scenario=scenario,
                version_before=version_before,
                version_after="pending_reboot",
                expected_version=expected,
                webui_version="N/A",
                elapsed_seconds=elapsed,
                message=(
                    f"Setup complete — reboot in {delay}s. "
                    f"Run 'reboot-verify' after reboot."
                ),
            )

        except Exception as exc:
            elapsed = time.time() - start_time
            log.exception("Reboot setup failed")
            return UpgradeResult(
                success=False,
                scenario=scenario,
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                webui_version="unknown",
                elapsed_seconds=elapsed,
                message=f"Exception: {exc}",
            )

    def run_reboot_verify(
        self,
        stabilize_wait: Optional[int] = None,
        state_path: Optional[Path] = None,
    ) -> RebootVerifyResult:
        """
        Phase 2: After reboot, verify client recovered correctly.

        Loads state from ``data/reboot_state.json``, waits for
        stabilization, then checks services, watchdog binpath,
        installed version, and install directory.

        :param stabilize_wait: Override seconds to wait (default from state).
        :param state_path: Override path to reboot state file (for testing).
        :return: RebootVerifyResult with all check outcomes.
        """
        start_time = time.time()

        # Load saved state
        state = load_reboot_state(path=state_path)
        if state is None:
            return RebootVerifyResult(
                success=False,
                scenario="reboot_verify",
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                upgrade_completed=False,
                rolled_back=False,
                services={},
                watchdog_binpath="",
                watchdog_binpath_valid=False,
                install_dir_valid=False,
                elapsed_seconds=time.time() - start_time,
                message="No reboot state file found — run 'reboot-setup' first",
            )

        log.info("=" * 70)
        log.info("SCENARIO: Reboot-Interrupt Verify (Phase 2)")
        log.info("  version_before: %s", state.version_before)
        log.info("  expected_version: %s", state.expected_version)
        log.info("  reboot_timing: %s", state.reboot_timing)
        log.info("  source_64_bit: %s, target_64_bit: %s",
                 state.source_64_bit, state.target_64_bit)
        log.info("=" * 70)

        # Wait for system stabilization
        wait = stabilize_wait if stabilize_wait is not None else state.stabilize_wait
        log.info("Waiting %d seconds for system stabilization...", wait)
        time.sleep(wait)
        log.info("Stabilization wait completed")

        # ── Service checks ──────────────────────────────────────────
        services: dict[str, dict[str, Any]] = {}
        all_services_ok = True
        for role, svc_name in SERVICES.items():
            info = self.client.query_service(svc_name)
            services[role] = {
                "name": info.name,
                "exists": info.exists,
                "state": info.state,
            }
            log.info(
                "Service %s (%s): exists=%s, state=%s",
                role, svc_name, info.exists, info.state,
            )
            if not info.exists or info.state != "RUNNING":
                all_services_ok = False

        # ── Watchdog binary path ────────────────────────────────────
        watchdog_binpath = self.client.query_service_binpath("stwatchdog")
        log.info("Watchdog binpath: %s", watchdog_binpath or "(empty)")

        # ── Version check ───────────────────────────────────────────
        version_after = self._get_current_version()
        log.info("Version after reboot: %s", version_after)

        upgrade_completed = (version_after == state.expected_version)
        rolled_back = (version_after == state.version_before)

        # ── Determine active bitness and verify install dir ─────────
        if upgrade_completed:
            active_64_bit = state.target_64_bit
        else:
            active_64_bit = state.source_64_bit
        install_dir_valid = self.client.verify_install_dir(active_64_bit)

        # ── Watchdog binpath validation ─────────────────────────────
        expected_dir = str(self.client.get_install_dir(active_64_bit)).lower()
        watchdog_binpath_valid = (
            bool(watchdog_binpath)
            and expected_dir in watchdog_binpath.lower()
        )
        if not watchdog_binpath_valid:
            log.warning(
                "Watchdog binpath mismatch: expected dir=%s, actual=%s",
                expected_dir, watchdog_binpath,
            )

        # ── Determine overall success ───────────────────────────────
        has_valid_version = upgrade_completed or rolled_back
        success = (
            has_valid_version
            and all_services_ok
            and watchdog_binpath_valid
            and install_dir_valid
        )

        # Build message
        if upgrade_completed:
            version_msg = f"Upgrade completed: {state.version_before} -> {version_after}"
        elif rolled_back:
            version_msg = f"Rolled back to original: {version_after}"
        else:
            version_msg = (
                f"Unexpected version: {version_after} "
                f"(expected {state.expected_version} or {state.version_before})"
            )

        issues: list[str] = []
        if not all_services_ok:
            issues.append("not all services running")
        if not watchdog_binpath_valid:
            issues.append("watchdog binpath invalid")
        if not install_dir_valid:
            issues.append("install dir invalid")
        if not has_valid_version:
            issues.append("unexpected version")

        message = version_msg
        if issues:
            message += f" — ISSUES: {', '.join(issues)}"

        elapsed = time.time() - start_time
        log.info("Verify result: success=%s — %s", success, message)

        # Clean up state file on completion
        clear_reboot_state(path=state_path)

        return RebootVerifyResult(
            success=success,
            scenario=f"reboot_verify(timing={state.reboot_timing})",
            version_before=state.version_before,
            version_after=version_after,
            expected_version=state.expected_version,
            upgrade_completed=upgrade_completed,
            rolled_back=rolled_back,
            services=services,
            watchdog_binpath=watchdog_binpath,
            watchdog_binpath_valid=watchdog_binpath_valid,
            install_dir_valid=install_dir_valid,
            elapsed_seconds=elapsed,
            message=message,
        )

    def _cleanup(self) -> None:
        """Reset tenant config to disable auto-upgrade."""
        try:
            self.webui.disable_auto_upgrade(search_config=self.config_name)
            log.info("Cleanup: auto-upgrade disabled on tenant")
        except Exception as exc:
            log.warning("Cleanup failed: %s", exc)
