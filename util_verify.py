"""
Post-upgrade verification and version polling for the
Netskope Client Upgrade Tool.
"""

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from util_client import (
    LocalClient, ExeValidationResult, UninstallEntryResult,
)
from util_config import UpgradeConfig
from util_webui import WebUIClient

log = logging.getLogger(__name__)


@dataclass
class PollResult:
    """Result of version polling."""
    changed: bool
    final_version: str
    elapsed_seconds: float
    crash_detected: bool = False


class UpgradeVerifier:
    """
    Handles version polling and post-upgrade verification.

    Provides methods to poll the local client version, verify service
    state, compare WebUI version, and validate executables/registry.
    """

    def __init__(
        self,
        client: LocalClient,
        webui: WebUIClient,
        cfg: UpgradeConfig,
        host_name: str,
        email: str,
        target_64_bit: bool,
        source_64_bit: bool,
        stop_event: threading.Event,
        log_dir: Optional[Path] = None,
    ) -> None:
        self.client = client
        self.webui = webui
        self.cfg = cfg
        self.host_name = host_name
        self.email = email
        self.target_64_bit = target_64_bit
        self.source_64_bit = source_64_bit
        self.stop_event = stop_event
        self.log_dir = log_dir

    # ── Version Reading ──────────────────────────────────────────────

    def get_current_version(self) -> str:
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

    # ── Version Polling ──────────────────────────────────────────────

    def wait_for_upgrade(
        self,
        expected_version: Optional[str] = None,
        timeout_override: Optional[int] = None,
    ) -> PollResult:
        """
        Poll the local client version until it changes or matches expected.

        Also monitors for crash dumps and the stop_event (ESC key) each
        polling cycle.

        :param expected_version: Specific version to wait for. If None,
                                 detects any version change.
        :param timeout_override: Override max wait time in seconds.
        :return: PollResult with final state.
        """
        timeout = timeout_override or self.cfg.max_wait_seconds
        interval = self.cfg.poll_interval_seconds
        initial_version = self.get_current_version()

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
            if self.stop_event.wait(timeout=interval):
                log.warning(
                    "Stop event detected — aborting upgrade wait"
                )
                break

            elapsed = time.time() - start

            current = self.get_current_version()

            if expected_version and current == expected_version:
                log.info(
                    "Version matched expected %s after %.0fs",
                    expected_version, elapsed,
                )
                return PollResult(
                    changed=True, final_version=current,
                    elapsed_seconds=elapsed,
                )

            if (
                expected_version is None
                and current != initial_version
            ):
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

        final = self.get_current_version()
        log.warning(
            "Polling timed out after %ds — final version: %s",
            timeout, final,
        )
        return PollResult(
            changed=(final != initial_version),
            final_version=final, elapsed_seconds=elapsed,
        )

    # ── Post-Upgrade Verification ────────────────────────────────────

    def verify_service_running(self) -> bool:
        """
        Confirm the client service is still running after upgrade.

        :return: True if service is running.
        """
        running = self.client.is_service_running()
        if not running:
            log.warning(
                "Service stAgentSvc not running after upgrade"
            )
        else:
            log.info(
                "Service stAgentSvc confirmed running after upgrade"
            )
        return running

    def verify_webui_version(self, expected_local: str) -> str:
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
                log.info(
                    "WebUI version matches local: %s", webui_version
                )
            return webui_version
        except Exception as exc:
            log.warning("Failed to verify WebUI version: %s", exc)
            return "error"

    def validate_pre_report(
        self,
        version_after: str,
        is_64_bit: Optional[bool] = None,
    ) -> tuple[bool, ExeValidationResult, UninstallEntryResult]:
        """
        Run pre-report validation: executables and uninstall registry.

        :param version_after: The version to validate against.
        :param is_64_bit: Bitness for install dir lookup (defaults
                          to target_64_bit).
        :return: (all_valid, exe_result, uninstall_result).
        """
        use_64 = (
            is_64_bit if is_64_bit is not None
            else self.target_64_bit
        )
        exe_validation = self.client.verify_executables(
            is_64_bit=use_64,
            expected_version=version_after,
        )
        if not exe_validation.valid:
            log.warning(
                "Pre-report exe validation failed: "
                "missing=%s, mismatches=%s",
                exe_validation.missing,
                exe_validation.version_mismatches,
            )

        # Arch change cleanup check (32→64 or 64→32)
        if self.source_64_bit != use_64:
            stale = LocalClient.check_old_arch_cleanup(self.source_64_bit, use_64)
            exe_validation.stale_arch_files = stale
            if stale:
                exe_validation.valid = False

        uninstall_entry = self.client.check_uninstall_registry()
        if not uninstall_entry.found:
            log.warning(
                "Pre-report validation: "
                "uninstall registry entry not found"
            )

        valid = exe_validation.valid and uninstall_entry.found
        return valid, exe_validation, uninstall_entry


def is_mismatch_only_failure(
    exe_validation: Optional[ExeValidationResult],
    service_running: bool,
    uninstall_entry: Optional[UninstallEntryResult],
) -> bool:
    """
    Return True when the only validation failure is an exe version mismatch.

    All structural checks must pass: service running, uninstall entry found,
    no missing executables, no stale arch files.  The mismatch itself must
    exist (otherwise there is no failure to downgrade).

    :return: True if downgrading critical_failure to a normal failure is safe.
    """
    if not service_running:
        return False
    if uninstall_entry is not None and not uninstall_entry.found:
        return False
    if exe_validation is None or not exe_validation.version_mismatches:
        return False
    if exe_validation.watchdog_duplicate:
        return False
    return not exe_validation.missing and not exe_validation.stale_arch_files


def format_validation_issues(
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
            issues.append(
                f"missing exe: {', '.join(exe_validation.missing)}"
            )
        if exe_validation.version_mismatches:
            issues.append(
                "exe version mismatch: "
                f"{', '.join(exe_validation.version_mismatches)}"
            )
        if exe_validation.stale_arch_files:
            issues.append(
                "old arch files not cleaned: "
                f"{', '.join(exe_validation.stale_arch_files)}"
            )
    if exe_validation and exe_validation.processes_not_running:
        issues.append(
            f"process not running: {', '.join(exe_validation.processes_not_running)}"
        )
    if exe_validation and exe_validation.stwatchdog_running is False:
        issues.append("stwatchdog service not running")
    if exe_validation and exe_validation.watchdog_duplicate:
        issues.append(exe_validation.watchdog_duplicate)
    if uninstall_entry and not uninstall_entry.found:
        issues.append("uninstall registry entry missing")
    if issues:
        return f" — ISSUES: {', '.join(issues)}"
    return ""
