"""
Core upgrade orchestration logic for the Netskope Client Upgrade Tool.
Each public method implements a complete upgrade scenario end-to-end.
"""

import logging
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from util_client import (
    LocalClient, SERVICES,
    ExeValidationResult, UninstallEntryResult,
)
from util_config import UpgradeConfig
from util_installer import (
    InstallerManager,
    BASE_VERSION_DIR, INSTALLER_JSON,
)
from util_log import LOG_DIR, build_log_dir_name, rename_log_dir, setup_folder_logging
from util_verify import (
    UpgradeVerifier, PollResult,
    format_validation_issues,
)
from util_webui import WebUIClient


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
    critical_failure: bool = False


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
        reboot_action: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
        log_dir: Optional[Path] = None,
        email_profiles: Optional[dict[str, str]] = None,
        save_config_fn: Optional[Callable[[], None]] = None,
        batch_mode: bool = False,
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
        :param reboot_time: Timing number (1-13) that triggers a reboot.
        :param reboot_delay: Seconds before reboot after timing fires.
        :param reboot_action: Action at reboot timing (2=kill monitor+reboot,
                              3=kill monitor+msiexec+reboot). None=default reboot.
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
        self.reboot_action = reboot_action
        self.stop_event = stop_event or threading.Event()
        self._upgrade_enabled = False
        self._log_dir: Optional[Path] = log_dir
        self._batch_mode = batch_mode

        # Composed helpers
        self._installer = InstallerManager(
            client=client,
            webui=webui,
            source_64_bit=source_64_bit,
            stop_event=self.stop_event,
            log_dir=self._log_dir,
            init_nsclient_fn=self._init_nsclient,
            email_profiles=email_profiles,
            save_config_fn=save_config_fn,
        )
        self._verifier = UpgradeVerifier(
            client=client,
            webui=webui,
            cfg=upgrade_cfg,
            host_name=self.host_name,
            email=self.email,
            target_64_bit=target_64_bit,
            source_64_bit=source_64_bit,
            stop_event=self.stop_event,
            log_dir=self._log_dir,
        )

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
            # Phase 1: Ensure base client is installed
            self._check_stopped()
            self._installer.ensure_client_installed(
                from_version, invite_email,
            )
            self._check_stopped()
            self._sync_and_detect_config()

            # Phase 2: Init nsclient + read version
            self._check_stopped()
            nsclient_ok = self._init_nsclient()
            version_before = self._verifier.get_current_version()
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
            self._check_stopped()
            self.webui.enable_upgrade_latest(
                search_config=self.config_name,
                target_64_bit=self.target_64_bit,
            )
            self._upgrade_enabled = True

            # Start monitor before sync loop so it can detect
            # timing 1 (config sync) and timing 2 (MSI download)
            monitor = self._start_monitor()
            self._start_sync_thread(monitor)
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

            version_after = self._verifier.get_current_version()

            # Stop monitor and print timing report
            self._stop_monitor(monitor)

            # Post-upgrade checks
            service_running = self._verifier.verify_service_running()
            validation_ok, exe_validation, uninstall_entry = (
                self._verifier.validate_pre_report(version_after)
            )
            webui_version = self._verifier.verify_webui_version(
                version_after,
            )

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
            message += format_validation_issues(
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
                critical_failure=not validation_ok,
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
            golden_versions_sorted = sorted(
                all_versions["goldenversions"], key=_version_key,
            )
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
                    log.info(
                        "Auto-picked from_version: %s", from_version
                    )

            # Phase 1: Ensure base client is installed
            self._check_stopped()
            self._installer.ensure_client_installed(
                from_version, invite_email,
            )
            self._check_stopped()
            self._sync_and_detect_config()

            # Phase 2: Init nsclient + read version
            self._check_stopped()
            nsclient_ok = self._init_nsclient()
            version_before = self._verifier.get_current_version()
            log.info("Version before upgrade: %s", version_before)

            # Determine expected version after upgrade
            if dot:
                expected = sorted(
                    all_versions[golden_version], key=_version_key,
                )[-1]
            else:
                expected = sorted(
                    all_versions[golden_version], key=_version_key,
                )[0]
            expected = self._apply_64bit_suffix(expected)
            log.info("Expected version after upgrade: %s", expected)

            # Create scenario log folder now that versions are known
            self._create_log_dir(version_before, expected)

            # Trigger golden upgrade via WebUI
            self._check_stopped()
            self.webui.enable_upgrade_golden(
                golden_version, dot=dot,
                search_config=self.config_name,
                target_64_bit=self.target_64_bit,
            )
            self._upgrade_enabled = True

            # Start monitor before sync loop so it can detect
            # timing 1 (config sync) and timing 2 (MSI download)
            monitor = self._start_monitor()
            self._start_sync_thread(monitor)
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

            version_after = self._verifier.get_current_version()

            # Stop monitor and print timing report
            self._stop_monitor(monitor)

            # Post-upgrade checks
            service_running = self._verifier.verify_service_running()
            validation_ok, exe_validation, uninstall_entry = (
                self._verifier.validate_pre_report(version_after)
            )
            webui_version = self._verifier.verify_webui_version(
                version_after,
            )

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
            message += format_validation_issues(
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
                critical_failure=not validation_ok,
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
            # Phase 1: Ensure base client is installed
            self._check_stopped()
            self._installer.ensure_client_installed(
                from_version, invite_email,
            )
            self._check_stopped()
            self._sync_and_detect_config()

            # Phase 2: Init nsclient + read version
            self._check_stopped()
            nsclient_ok = self._init_nsclient()
            version_before = self._verifier.get_current_version()
            log.info("Version before: %s", version_before)

            # Create scenario log folder
            self._create_log_dir(version_before, "disabled")

            # Disable auto-upgrade and verify it stays
            self._check_stopped()
            self.webui.disable_auto_upgrade(
                search_config=self.config_name,
            )
            if nsclient_ok:
                self.client.update_config(
                    wait_seconds=self.cfg.config_update_wait_seconds,
                )
            else:
                log.info(
                    "Skipping local config pull "
                    "(nsclient not available)"
                )

            # Wait the full polling period — version should NOT change
            log.info(
                "Waiting %d seconds to confirm no upgrade occurs...",
                self.cfg.max_wait_seconds,
            )
            poll = self._verifier.wait_for_upgrade(
                expected_version=None,
                timeout_override=self.cfg.max_wait_seconds,
            )
            version_after = poll.final_version

            # Post checks
            service_running = self._verifier.verify_service_running()
            validation_ok, exe_validation, uninstall_entry = (
                self._verifier.validate_pre_report(
                    version_after, is_64_bit=self.source_64_bit,
                )
            )
            webui_version = self._verifier.verify_webui_version(
                version_after,
            )

            elapsed = time.time() - start_time
            version_ok = version_before == version_after
            success = (
                version_ok and service_running
                and validation_ok and not poll.crash_detected
            )
            message = (
                f"Correctly stayed at {version_before} "
                "— auto-upgrade disabled works"
                if version_ok
                else (
                    f"UNEXPECTED upgrade occurred: "
                    f"{version_before} -> {version_after}"
                )
            )
            if poll.crash_detected:
                message += " — CRASH DUMP DETECTED"
            message += format_validation_issues(
                service_running, exe_validation, uninstall_entry,
            )
            log.info(message)

            result = UpgradeResult(
                success=success,
                scenario=scenario,
                version_before=version_before,
                version_after=version_after,
                expected_version=version_before,
                webui_version=webui_version,
                elapsed_seconds=elapsed,
                message=message,
                service_running=service_running,
                exe_validation=exe_validation,
                uninstall_entry=uninstall_entry,
                critical_failure=not validation_ok,
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
            reboot_action=self.reboot_action,
            log_dir=str(self._log_dir) if self._log_dir else "",
            skip_continue_task=self._batch_mode,
        )
        monitor.start()

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

    def _check_stopped(self) -> None:
        """Raise if the stop event (ESC key) has been set."""
        if self.stop_event.is_set():
            raise KeyboardInterrupt("Stopped by user (ESC)")

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

    def _apply_64bit_suffix(
        self,
        version: str,
        is_64_bit: Optional[bool] = None,
    ) -> str:
        """
        Append ' (64-bit)' to a version string when targeting 64-bit.

        :param version: Base version string from the API.
        :param is_64_bit: Override bitness flag (defaults to target_64_bit).
        :return: Version with suffix if 64-bit, unchanged otherwise.
        """
        use_64 = (
            is_64_bit if is_64_bit is not None
            else self.target_64_bit
        )
        if use_64 and not version.endswith("(64-bit)"):
            return f"{version} (64-bit)"
        return version

    def _start_sync_thread(self, monitor: Any) -> None:
        """
        Launch a background thread that runs ``nsdiag -u`` up to 3 times,
        with a 30-second gap between attempts.

        The thread stops early as soon as any timing event fires so the
        syncs don't continue once the upgrade is already in progress.
        The main thread returns immediately and proceeds to
        ``wait_for_upgrade_complete``.

        :param monitor: Running TimingMonitor instance.
        """
        MAX_SYNC_ATTEMPTS = 3
        SYNC_INTERVAL = 30   # seconds between attempts

        def _worker() -> None:
            for attempt in range(1, MAX_SYNC_ATTEMPTS + 1):
                if self.stop_event.is_set() or monitor.get_timings():
                    log.info(
                        "Sync thread stopping early before attempt %d "
                        "— timings already firing",
                        attempt,
                    )
                    return
                self.client.sync_config_from_tenant(
                    is_64_bit=self.source_64_bit, wait_seconds=0,
                )
                log.info(
                    "Config sync attempt %d/%d complete",
                    attempt, MAX_SYNC_ATTEMPTS,
                )
                if attempt < MAX_SYNC_ATTEMPTS:
                    # Wait between attempts; abort early if any timing fires
                    for _ in range(SYNC_INTERVAL):
                        if self.stop_event.is_set() or monitor.get_timings():
                            log.info(
                                "Sync thread stopping — timings firing "
                                "after attempt %d",
                                attempt,
                            )
                            return
                        time.sleep(1)
            log.info(
                "Config sync thread finished (%d attempts)",
                MAX_SYNC_ATTEMPTS,
            )

        thread = threading.Thread(
            target=_worker, daemon=True, name="sync-config",
        )
        thread.start()
        log.info(
            "Config sync thread started (up to %d attempts)",
            MAX_SYNC_ATTEMPTS,
        )

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
        self.client.sync_config_from_tenant(
            is_64_bit=self.source_64_bit,
        )
        ns_info = self.client.detect_tenant_from_nsconfig()
        if ns_info and ns_info.config_name:
            self.config_name = ns_info.config_name
            log.info(
                "Detected config_name after sync: %s",
                self.config_name,
            )
        else:
            log.warning(
                "Could not detect config_name after sync — "
                "API calls will target the default config"
            )
        watchdog = LocalClient.is_watchdog_mode()
        log.info("Watchdog mode: %s", watchdog)

    # ── Log folder & failure collection ──────────────────────────────

    def _create_log_dir(
        self, from_version: str, to_version: str,
    ) -> Path:
        """
        Rename the pre-created log folder to include version info.

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
        """Collect nsdiag log bundle when the final result is failure.

        Skips collection if upgrade was never triggered (e.g. install
        or email extraction failure) — there is nothing useful to
        collect in that case.
        """
        if not self._upgrade_enabled:
            log.info(
                "Skipping log bundle — upgrade was not started"
            )
            return
        log_dir = self._log_dir or LOG_DIR
        effective_64 = self.target_64_bit or self.source_64_bit
        log.info("Collecting log bundle for failure analysis...")
        LocalClient.collect_log_bundle(effective_64, log_dir)

    @property
    def log_dir(self) -> Optional[Path]:
        """Return the scenario log directory, if created."""
        return self._log_dir

    def _cleanup(self) -> None:
        """Reset tenant config, close browser, remove cloned installer."""
        self._installer.cleanup()
        if self._upgrade_enabled:
            try:
                self.webui.disable_auto_upgrade(
                    search_config=self.config_name,
                )
                log.info("Cleanup: auto-upgrade disabled on tenant")
            except Exception as exc:
                log.warning("Cleanup failed: %s", exc)
            self._upgrade_enabled = False
        else:
            log.info(
                "Cleanup: skipping tenant rollback "
                "(upgrade was never enabled)"
            )
