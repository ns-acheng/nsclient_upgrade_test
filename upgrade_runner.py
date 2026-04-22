"""
Core upgrade orchestration logic for the Netskope Client Upgrade Tool.
Each public method implements a complete upgrade scenario end-to-end.
"""

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from util_client import (
    LocalClient, SERVICES,
    ExeValidationResult, UninstallEntryResult, UninstallCriticalError,
    check_driver_install_log, CrashMonitor,
)
from util_config import UpgradeConfig
from util_installer import (
    InstallerManager,
    BASE_VERSION_DIR, INSTALLER_JSON,
    find_upgrade_installer,
)
from util_log import LOG_DIR, build_log_dir_name, rename_log_dir, setup_folder_logging
from util_verify import (
    UpgradeVerifier, PollResult,
    format_validation_issues,
    is_mismatch_only_failure,
)
from util_webui import WebUIClient


def _version_key(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints for sorting."""
    return tuple(int(x) for x in version.split("."))


def _normalize_golden_version(version: str) -> str:
    """Normalize a golden version shorthand to full ``X.0.0`` format.

    ``'132'`` → ``'132.0.0'``, ``'132.0'`` → ``'132.0.0'``,
    ``'132.0.0'`` unchanged.
    """
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


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
        standby: Optional[str] = None,
        stop_event: Optional[threading.Event] = None,
        log_dir: Optional[Path] = None,
        email_profiles: Optional[dict[str, str]] = None,
        save_config_fn: Optional[Callable[[], None]] = None,
        batch_mode: bool = False,
        original_argv: Optional[list[str]] = None,
        simulate_upgrade: bool = False,
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
        self.standby = standby
        self.stop_event = stop_event or threading.Event()
        self._upgrade_enabled = False
        self._log_dir: Optional[Path] = log_dir
        self._batch_mode = batch_mode
        self._original_argv: list[str] = original_argv or []
        self._watchdog_mode: bool = False
        self._auto_update_already_enabled: bool = False
        self._simulate_upgrade: bool = simulate_upgrade
        self._crash_monitor: Optional[CrashMonitor] = None

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
            skip = self._skip_if_timing_not_applicable(scenario, start_time)
            if skip:
                return skip

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

            # Trigger upgrade via WebUI — skip if tenant already
            # had auto-update enabled (race: upgrade may already
            # be in progress)
            if self._auto_update_already_enabled:
                log.info(
                    "allowAutoUpdate already enabled on tenant — "
                    "skipping WebUI config, going straight to monitor"
                )
            else:
                self._check_stopped()
                self.webui.enable_upgrade_latest(
                    search_config=self.config_name,
                    target_64_bit=self.target_64_bit,
                )
            self._upgrade_enabled = True

            # Start monitor before sync loop so it can detect
            # timing 1 (config sync) and timing 2 (MSI download)
            monitor = self._start_monitor(
                version_before=version_before,
                expected_version=expected,
                scenario=scenario,
            )
            if not self._auto_update_already_enabled:
                self._start_sync_thread(monitor)
            completed = monitor.wait_for_upgrade_complete(
                timeout=self.cfg.max_wait_seconds,
            )

            crash_found = (
                self._crash_monitor.crash_detected
                if self._crash_monitor is not None else False
            )

            version_after = self._verifier.get_current_version()

            # Stop monitor and print timing report
            self._stop_monitor(monitor)

            # Wait for posture to settle after timing 12
            self._wait_posture_settle(monitor)

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
            driver_note = check_driver_install_log(exe_validation, service_running)
            if driver_note:
                message += driver_note
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
                critical_failure=(
                    False
                    if driver_note or is_mismatch_only_failure(
                        exe_validation, service_running, uninstall_entry,
                    )
                    else not validation_ok
                ),
            )
            if not result.success:
                self._collect_failure_logs()
            return result

        except Exception as exc:
            elapsed = time.time() - start_time
            log.error("Scenario %s failed with exception: %s", scenario, exc)
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
                critical_failure=isinstance(exc, UninstallCriticalError),
            )
        finally:
            self._cleanup()

    def run_upgrade_to_golden(
        self,
        from_version: Optional[str] = None,
        golden_version: Optional[str] = None,
        dot: bool = False,
        invite_email: Optional[str] = None,
    ) -> UpgradeResult:
        """
        Scenario: Ensure client is installed, enable auto-upgrade to a
        golden release, wait and verify.

        :param from_version: Build version for download fallback. If None,
                             auto-picks a version older than the target golden.
        :param golden_version: Specific golden version to target (e.g.
                               ``'132'`` or ``'132.0.0'``). If None, the
                               latest golden version from the tenant is used.
        :param dot: If True, enable dot release within the golden version.
        :param invite_email: Email to send enrollment invite before install.
        :return: UpgradeResult with outcome details.
        """
        scenario_label = golden_version or "latest"
        scenario = f"upgrade_to_golden({scenario_label}, dot={dot})"
        start_time = time.time()
        log.info("=" * 70)
        log.info("SCENARIO: Upgrade to Golden Release")
        log.info("  config_name: %s", self.config_name or "(default)")
        log.info(
            "  golden_version: %s, dot: %s, from_version: %s",
            golden_version or "(latest)", dot, from_version,
        )
        log.info("=" * 70)
        try:
            # Resolve golden version
            all_versions = self.webui.get_release_versions()
            available_golden = sorted(
                all_versions.get("goldenversions", []),
                key=_version_key,
            )
            if not available_golden:
                return UpgradeResult(
                    success=False,
                    scenario=scenario,
                    version_before="unknown",
                    version_after="unknown",
                    expected_version="unknown",
                    webui_version="",
                    elapsed_seconds=time.time() - start_time,
                    message="No golden versions available on the tenant",
                    service_running=False,
                    critical_failure=True,
                )

            if golden_version is not None:
                golden_version = _normalize_golden_version(golden_version)
                if golden_version not in available_golden:
                    avail_str = ", ".join(available_golden)
                    return UpgradeResult(
                        success=False,
                        scenario=scenario,
                        version_before="unknown",
                        version_after="unknown",
                        expected_version="unknown",
                        webui_version="",
                        elapsed_seconds=time.time() - start_time,
                        message=(
                            f"Golden version {golden_version} not found "
                            f"on tenant. Available: {avail_str}"
                        ),
                        service_running=False,
                        critical_failure=True,
                    )
            else:
                golden_version = available_golden[-1]

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
            skip = self._skip_if_timing_not_applicable(scenario, start_time)
            if skip:
                return skip

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

            # Trigger golden upgrade via WebUI — skip if tenant already
            # had auto-update enabled (race: upgrade may already
            # be in progress)
            if self._auto_update_already_enabled:
                log.info(
                    "allowAutoUpdate already enabled on tenant — "
                    "skipping WebUI config, going straight to monitor"
                )
            else:
                self._check_stopped()
                self.webui.enable_upgrade_golden(
                    golden_version, dot=dot,
                    search_config=self.config_name,
                    target_64_bit=self.target_64_bit,
                )
            self._upgrade_enabled = True

            # Start monitor before sync loop so it can detect
            # timing 1 (config sync) and timing 2 (MSI download)
            monitor = self._start_monitor(
                version_before=version_before,
                expected_version=expected,
                scenario=scenario,
            )
            if not self._auto_update_already_enabled:
                self._start_sync_thread(monitor)
            completed = monitor.wait_for_upgrade_complete(
                timeout=self.cfg.max_wait_seconds,
            )

            crash_found = (
                self._crash_monitor.crash_detected
                if self._crash_monitor is not None else False
            )

            version_after = self._verifier.get_current_version()

            # Stop monitor and print timing report
            self._stop_monitor(monitor)

            # Wait for posture to settle after timing 12
            self._wait_posture_settle(monitor)

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
            driver_note = check_driver_install_log(exe_validation, service_running)
            if driver_note:
                message += driver_note
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
                critical_failure=(
                    False
                    if driver_note or is_mismatch_only_failure(
                        exe_validation, service_running, uninstall_entry,
                    )
                    else not validation_ok
                ),
            )
            if not result.success:
                self._collect_failure_logs()
            return result

        except Exception as exc:
            elapsed = time.time() - start_time
            log.error("Scenario %s failed with exception: %s", scenario, exc)
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
                critical_failure=isinstance(exc, UninstallCriticalError),
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
            skip = self._skip_if_timing_not_applicable(scenario, start_time)
            if skip:
                return skip

            # Phase 2: Init nsclient + read version
            self._check_stopped()
            nsclient_ok = self._init_nsclient()
            version_before = self._verifier.get_current_version()
            log.info("Version before: %s", version_before)

            # Create scenario log folder
            self._create_log_dir(version_before, "disabled")
            self._start_crash_monitor()

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

            crash_found = (
                self._crash_monitor.crash_detected
                if self._crash_monitor is not None else False
            )
            elapsed = time.time() - start_time
            version_ok = version_before == version_after
            success = (
                version_ok and service_running
                and validation_ok and not crash_found
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
            log.error("Scenario %s failed with exception: %s", scenario, exc)
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
                critical_failure=isinstance(exc, UninstallCriticalError),
            )
        finally:
            self._cleanup()

    def run_upgrade_from_local(
        self,
        invite_email: Optional[str] = None,
    ) -> UpgradeResult:
        """
        Scenario: Install base client, then install upgrade MSI from
        data/upgrade_version/ and monitor the upgrade lifecycle.

        Flow:
        1. Uninstall existing client and install the base MSI
           (email invite + rename if *invite_email* is provided).
        2. Sync tenant config and detect watchdog mode.
        3. Start timing monitor and upgrade MSI install concurrently.
        4. Wait for the new stAgentSvc version to be running.
        5. Settle 10 s, then run posture validation.

        The upgrade MSI is chosen by bitness:
        - 32-bit target: data/upgrade_version/stagent.msi
        - 64-bit target: data/upgrade_version/stagent64.msi

        :param invite_email: Email to send enrollment invite before base install.
        :return: UpgradeResult with outcome details.
        """
        scenario = "upgrade_from_local"
        start_time = time.time()
        log.info("=" * 70)
        log.info("SCENARIO: Upgrade from Local MSI")
        log.info(
            "  source_64_bit: %s, target_64_bit: %s",
            self.source_64_bit, self.target_64_bit,
        )
        log.info("=" * 70)
        try:
            # Verify upgrade MSI exists before doing anything else
            upgrade_installer = find_upgrade_installer(self.target_64_bit)
            if upgrade_installer is None:
                msi_name = "stagent64.msi" if self.target_64_bit else "stagent.msi"
                return UpgradeResult(
                    success=False,
                    scenario=scenario,
                    version_before="unknown",
                    version_after="unknown",
                    expected_version="unknown",
                    webui_version="",
                    elapsed_seconds=0.0,
                    message=(
                        f"Upgrade MSI not found: "
                        f"data/upgrade_version/{msi_name}"
                    ),
                    service_running=False,
                    critical_failure=True,
                )

            # Phase 1: Ensure base client is installed
            self._check_stopped()
            self._installer.ensure_client_installed(
                from_version=None, invite_email=invite_email,
            )
            self._check_stopped()
            self._sync_and_detect_config()
            skip = self._skip_if_timing_not_applicable(scenario, start_time)
            if skip:
                return skip

            # Phase 2: Read version before upgrade
            self._check_stopped()
            self._init_nsclient()
            version_before = self._verifier.get_current_version()
            log.info("Version before upgrade: %s", version_before)

            # Determine expected version from the upgrade MSI subject
            raw_subject = LocalClient.get_msi_subject(upgrade_installer)
            if raw_subject:
                expected_raw = (
                    raw_subject.rsplit(" ", 1)[-1]
                    if " " in raw_subject else raw_subject
                )
            else:
                expected_raw = "unknown"
            expected = self._apply_64bit_suffix(expected_raw)
            log.info(
                "Upgrade MSI: %s — expected version: %s",
                upgrade_installer.name, expected,
            )

            # Create scenario log folder now that versions are known
            self._create_log_dir(version_before, expected)

            # Phase 3: Start timing monitor and install upgrade MSI
            # concurrently.  The monitor daemon detects timing events
            # while msiexec performs the upgrade in a background thread.
            monitor = self._start_monitor(
                version_before=version_before,
                expected_version=expected,
                scenario=scenario,
            )

            install_error: list[Optional[Exception]] = [None]
            install_done = threading.Event()

            def _install_worker() -> None:
                log.info("Local upgrade install worker entered")

                def _detect_protection_mode() -> tuple[bool, str]:
                    """
                    Best-effort detection for protection-mode fallback.

                    This maps to the environment where local install files are
                    inaccessible (rollback/protected stage), which is the only
                    case where --simulate registry pre-write may be skipped.
                    """
                    nsconfig_path = LocalClient.NSCONFIG_PATH
                    nsconfig_enc_path = LocalClient.NSCONFIG_ENC_PATH

                    # When nsconfig.json is unavailable we already fall back
                    # to ``nsdiag -f``. Treat this as protection mode to avoid
                    # false "non-protection mode" classification.
                    try:
                        if not nsconfig_path.is_file():
                            if nsconfig_enc_path.is_file():
                                return (
                                    True,
                                    "nsconfig.json unavailable (nsconfig.enc "
                                    "exists; using -f fallback)",
                                )
                            return (
                                True,
                                "nsconfig.json unavailable (using -f fallback)",
                            )
                    except PermissionError as exc:
                        return (
                            True,
                            f"nsconfig.json access denied (using -f fallback): {exc}",
                        )

                    install_dir = LocalClient.get_install_dir(self.source_64_bit)
                    try:
                        if not install_dir.exists():
                            return True, f"{install_dir} not found"
                        if not os.access(install_dir, os.R_OK | os.X_OK):
                            return True, f"no read/execute access to {install_dir}"
                        # Force a directory read to catch runtime permission errors.
                        list(install_dir.iterdir())
                        return False, ""
                    except Exception as access_exc:
                        return True, str(access_exc)

                try:
                    if self._simulate_upgrade:
                        try:
                            LocalClient.set_upgrade_in_progress(1)
                            log.info(
                                "--simulate: wrote UpgradeInProgress registry "
                                "value (DWORD=1)"
                            )
                        except Exception as exc:
                            protection_mode, protection_note = (
                                _detect_protection_mode()
                            )
                            if protection_mode:
                                log.warning(
                                    "--simulate: skipped UpgradeInProgress "
                                    "registry write in -f fallback/protection "
                                    "mode (%s): %s",
                                    protection_note,
                                    exc,
                                )
                            else:
                                log.warning(
                                    "--simulate: failed to set "
                                    "UpgradeInProgress registry key "
                                    "(non-protection mode) — "
                                    "ignoring and continuing: %s",
                                    exc,
                                )

                        cache_updated = LocalClient.try_set_upgrade_nsconfig_cache(
                            last_client_updated="1",
                            new_client_ver="137.0.0.2222",
                        )
                        if not cache_updated:
                            log.info(
                                "--simulate: nsconfig cache update skipped "
                                "(encrypted or no read/write permission)"
                            )

                        if not self._watchdog_mode:
                            try:
                                LocalClient.ensure_non_watchdog_monitor_service(
                                    is_64_bit=self.source_64_bit,
                                )
                            except Exception as exc:
                                # Only skip when the Netskope install folder
                                # itself is inaccessible (e.g. rollback/protected
                                # stage). Otherwise treat as a real failure.
                                inaccessible, access_note = _detect_protection_mode()
                                if inaccessible:
                                    install_dir = LocalClient.get_install_dir(
                                        self.source_64_bit
                                    )
                                    log.warning(
                                        "--simulate: monitor-service prep skipped "
                                        "because Netskope install folder is "
                                        "inaccessible (%s): %s",
                                        install_dir,
                                        access_note,
                                    )
                                else:
                                    raise
                        else:
                            log.info(
                                "--simulate monitor-service clone skipped in "
                                "watchdog mode"
                            )

                    # Always write /l*v+ output to the scenario test folder.
                    # If for any reason _log_dir is missing, create a
                    # workspace-local fallback folder under log/.
                    install_log_dir = self._log_dir
                    if install_log_dir is None:
                        fallback_name = datetime.now().strftime(
                            "upgrade_%Y%m%d_%H%M%S_msi"
                        )
                        install_log_dir = LOG_DIR / fallback_name
                        install_log_dir.mkdir(parents=True, exist_ok=True)
                        log.warning(
                            "Scenario log folder missing; using fallback "
                            "msiexec log dir: %s",
                            install_log_dir,
                        )

                    sta_update_log = install_log_dir / "STAUpdate.txt"
                    log.info("Upgrade MSI verbose log path: %s", sta_update_log)
                    log.info("Calling install_local_upgrade_msi now")
                    self.client.install_local_upgrade_msi(
                        str(upgrade_installer),
                        sta_update_log,
                    )
                except Exception as exc:
                    install_error[0] = exc
                    log.exception("Local upgrade install worker failed: %s", exc)
                finally:
                    install_done.set()

            install_thread = threading.Thread(
                target=_install_worker,
                daemon=True,
                name="local-upgrade-install",
            )
            log.info(
                "Triggering local upgrade MSI install now (no pre-wait)"
            )
            install_thread.start()
            log.info(
                "Upgrade MSI install started — monitor and install "
                "running concurrently"
            )

            # Fail fast if install thread exits immediately with an error
            # (e.g. non-admin, missing MSI, msiexec launch failure).
            if install_done.wait(timeout=2):
                early_exc = install_error[0]
                if early_exc is not None and not monitor.state.reboot_triggered:
                    raise RuntimeError(
                        f"Upgrade MSI install failed before monitor wait: "
                        f"{early_exc}"
                    ) from early_exc

            # Wait for new stAgentSvc version running; settle for 10 s
            completed = monitor.wait_for_upgrade_complete(
                timeout=self.cfg.max_wait_seconds,
                settle_time=10.0,
            )

            # Give install thread up to 10 s to finish after upgrade is done
            install_done.wait(timeout=10)
            exc = install_error[0]
            if exc is not None and not monitor.state.reboot_triggered:
                log.warning("Upgrade MSI install raised: %s", exc)
                if isinstance(exc, RuntimeError):
                    raise exc
                raise RuntimeError(
                    f"Upgrade MSI install failed: {exc}"
                ) from exc

            crash_found = (
                self._crash_monitor.crash_detected
                if self._crash_monitor is not None else False
            )

            version_after = self._verifier.get_current_version()

            # Stop monitor and print timing report
            self._stop_monitor(monitor)

            # Wait for posture to settle after timing 12
            self._wait_posture_settle(monitor)

            # Post-upgrade checks (no WebUI version check for local scenario)
            service_running = self._verifier.verify_service_running()
            validation_ok, exe_validation, uninstall_entry = (
                self._verifier.validate_pre_report(version_after)
            )

            elapsed = time.time() - start_time
            version_ok = (expected == "unknown" or version_after == expected)
            success = (
                completed and version_ok and service_running
                and validation_ok and not crash_found
            )
            if not completed:
                message = (
                    f"Local upgrade timed out — "
                    f"{version_before} -> {version_after}"
                )
            elif version_ok:
                message = (
                    f"Local upgrade successful: "
                    f"{version_before} -> {version_after}"
                )
            else:
                message = (
                    f"Local upgrade FAILED: expected {expected}, "
                    f"got {version_after}"
                )
            if crash_found:
                message += " — CRASH DUMP DETECTED"
            message += format_validation_issues(
                service_running, exe_validation, uninstall_entry,
            )
            driver_note = check_driver_install_log(
                exe_validation, service_running,
            )
            if driver_note:
                message += driver_note
            log.info(message)

            result = UpgradeResult(
                success=success,
                scenario=scenario,
                version_before=version_before,
                version_after=version_after,
                expected_version=expected,
                webui_version="",
                elapsed_seconds=elapsed,
                message=message,
                service_running=service_running,
                exe_validation=exe_validation,
                uninstall_entry=uninstall_entry,
                critical_failure=(
                    False
                    if driver_note or is_mismatch_only_failure(
                        exe_validation, service_running, uninstall_entry,
                    )
                    else not validation_ok
                ),
            )
            if not result.success:
                self._collect_failure_logs_local()
            return result

        except Exception as exc:
            elapsed = time.time() - start_time
            log.error("Scenario %s failed with exception: %s", scenario, exc)
            self._collect_failure_logs_local()
            return UpgradeResult(
                success=False,
                scenario=scenario,
                version_before="unknown",
                version_after="unknown",
                expected_version="unknown",
                webui_version="",
                elapsed_seconds=elapsed,
                message=f"Exception: {exc}",
                service_running=False,
                critical_failure=isinstance(exc, UninstallCriticalError),
            )
        finally:
            self._cleanup()

    # ── Timing Monitor ───────────────────────────────────────────────

    def _start_monitor(
        self,
        version_before: str = "",
        expected_version: str = "",
        scenario: str = "",
    ) -> Any:
        """Start timing monitor for upgrade lifecycle detection."""
        from util_monitor import TimingMonitor

        monitor = TimingMonitor(
            target_64_bit=self.target_64_bit,
            reboot_time=self.reboot_time,
            reboot_delay=self.reboot_delay,
            reboot_action=self.reboot_action,
            log_dir=str(self._log_dir) if self._log_dir else "",
            skip_continue_task=self._batch_mode,
            version_before=version_before,
            expected_version=expected_version,
            scenario=scenario,
            source_64_bit=self.source_64_bit,
            original_argv=self._original_argv,
            watchdog_mode=self._watchdog_mode,
            standby=self.standby,
        )
        monitor.start()
        self._start_crash_monitor(monitor=monitor)

        def _esc_bridge() -> None:
            self.stop_event.wait()
            monitor.stop()

        bridge = threading.Thread(target=_esc_bridge, daemon=True)
        bridge.start()

        return monitor

    def _start_crash_monitor(self, monitor: Optional[Any] = None) -> None:
        """Create and start the background crash dump monitor.

        :param monitor: Running TimingMonitor, if any.  Its :meth:`stop`
                        is used as the abort callback so a detected crash
                        immediately unblocks ``wait_for_upgrade_complete``.
        """
        if self._crash_monitor is not None:
            return  # already running
        effective_64 = self.target_64_bit or self.source_64_bit
        on_crash = monitor.stop if monitor is not None else None
        self._crash_monitor = CrashMonitor(
            is_64_bit=effective_64,
            log_dir=self._log_dir or LOG_DIR,
            stop_event=self.stop_event,
            on_crash=on_crash,
        )
        self._crash_monitor.start()

    def _stop_monitor(self, monitor: Optional[Any]) -> None:
        """Stop timing monitor and print report."""
        if monitor is None:
            return
        monitor.stop()
        monitor.print_report()

    # ── Shared Helpers ───────────────────────────────────────────────

    # Minimum seconds after timing 12 (service running with new PID)
    # before running post-upgrade posture validation. Gives the client
    # time to finish driver installation, register in the uninstall
    # registry, and let stAgentSvcMon settle.
    POSTURE_SETTLE_SECONDS = 30

    def _wait_posture_settle(self, monitor: Any) -> None:
        """
        Wait until at least :attr:`POSTURE_SETTLE_SECONDS` have elapsed
        since timing 12 was detected, then proceed with posture validation.

        If timing 12 was not detected (e.g. timeout), this is a no-op.
        """
        t12_offset = monitor.state.timings.get("12")
        if t12_offset is None:
            return
        monitor_start = datetime.fromisoformat(
            monitor.state.monitor_start_time
        ).timestamp()
        t12_abs = monitor_start + t12_offset
        remaining = self.POSTURE_SETTLE_SECONDS - (time.time() - t12_abs)
        if remaining > 0:
            log.info(
                "Waiting %.0fs for posture to settle after timing 12",
                remaining,
            )
            time.sleep(remaining)

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

        After each sync, re-reads nsconfig.json for config_name,
        watchdog_mode, and allowAutoUpdate.  If allowAutoUpdate is
        already true the tenant config is live — stop syncing and
        let the monitor capture the upgrade in progress.

        The thread also stops early when any timing event fires so
        the syncs don't continue once the upgrade is already in
        progress.  The main thread returns immediately and proceeds
        to ``wait_for_upgrade_complete``.

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

                # Re-read nsconfig after each sync for fresh values
                ns_info = self.client.detect_tenant_from_nsconfig()
                if ns_info:
                    if ns_info.config_name and not self.config_name:
                        self.config_name = ns_info.config_name
                        log.info(
                            "Sync thread detected config_name: %s",
                            self.config_name,
                        )
                    wdog = LocalClient.is_watchdog_mode()
                    if wdog != self._watchdog_mode:
                        self._watchdog_mode = wdog
                        log.info(
                            "Sync thread updated watchdog_mode: %s",
                            self._watchdog_mode,
                        )
                    if ns_info.allow_auto_update:
                        log.info(
                            "Sync thread: allowAutoUpdate=true after "
                            "attempt %d — stop syncing, monitor will "
                            "capture upgrade",
                            attempt,
                        )
                        return

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

        Reads three values together after sync:
        - ``config_name`` (clientConfig.configurationName)
        - ``watchdog_mode`` (clientConfig.nsclient_watchdog_monitor)
        - ``allow_auto_update`` (clientConfig.clientUpdate.allowAutoUpdate)

        If the tenant already has auto-update enabled, sets
        ``_auto_update_already_enabled`` so callers can skip
        redundant WebUI config calls.

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
        self._watchdog_mode = LocalClient.is_watchdog_mode()
        log.info("Watchdog mode: %s", self._watchdog_mode)

        self._auto_update_already_enabled = (
            ns_info.allow_auto_update if ns_info else False
        )
        log.info(
            "allowAutoUpdate already enabled: %s",
            self._auto_update_already_enabled,
        )

    # ── Timing applicability ─────────────────────────────────────────

    def _skip_if_timing_not_applicable(
        self, scenario: str, start_time: float,
    ) -> Optional["UpgradeResult"]:
        """
        Return a PASS UpgradeResult if the configured reboot timing will
        never fire under the current conditions, so the test is skipped.

        Timing 3 (stAgentSvcMon.exe -monitor starts) never fires in watchdog
        mode — the monitor process lifecycle is managed differently.
        """
        if self.reboot_time in (3, 13) and self._watchdog_mode:
            label = (
                "stAgentSvcMon.exe -monitor"
                if self.reboot_time == 3
                else "stAgentSvcMon.exe stopped & upgraded"
            )
            msg = (
                f"Skipped: reboottime {self.reboot_time} ({label}) "
                "never fires in watchdog mode"
            )
            log.info(msg)
            return UpgradeResult(
                success=True,
                scenario=scenario,
                version_before="",
                version_after="",
                expected_version="",
                webui_version="",
                elapsed_seconds=time.time() - start_time,
                message=msg,
            )
        return None

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

    def _collect_failure_logs_local(self) -> None:
        """
        Collect nsdiag log bundle for a failed local upgrade scenario.

        Unlike :meth:`_collect_failure_logs`, this method always collects
        because the upgrade was triggered by msiexec (not a WebUI call),
        so ``_upgrade_enabled`` is never set in the local scenario.
        """
        log_dir = self._log_dir or LOG_DIR
        effective_64 = self.target_64_bit or self.source_64_bit
        log.info("Collecting log bundle for local upgrade failure...")
        LocalClient.collect_log_bundle(effective_64, log_dir)

    @property
    def log_dir(self) -> Optional[Path]:
        """Return the scenario log directory, if created."""
        return self._log_dir

    def _cleanup(self) -> None:
        """Reset tenant config, close browser, remove cloned installer."""
        if self._crash_monitor is not None:
            self._crash_monitor.stop()
            self._crash_monitor = None
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
