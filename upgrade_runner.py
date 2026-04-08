"""
Core upgrade orchestration logic for the Netskope Client Upgrade Tool.
Each public method implements a complete upgrade scenario end-to-end.
"""

import logging
import socket
import time
from dataclasses import dataclass
from typing import Any, Optional

from util_client import LocalClient
from util_config import UpgradeConfig
from util_webui import WebUIClient

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
        host_name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        """
        Initialize the upgrade runner.

        :param webui: Authenticated WebUI API client.
        :param client: Initialized local client wrapper.
        :param upgrade_cfg: Polling and timing configuration.
        :param host_name: Device hostname for WebUI verification.
        :param email: User email for WebUI verification.
        """
        self.webui = webui
        self.client = client
        self.cfg = upgrade_cfg
        self.host_name = host_name or socket.gethostname()
        self.email = email or client.email

    # ── Upgrade Scenarios ────────────────────────────────────────────

    def run_upgrade_to_latest(self, from_version: str) -> UpgradeResult:
        """
        Scenario: Install older version, enable auto-upgrade to latest,
        wait and verify upgrade completes.

        :param from_version: Build version to install first (e.g. 'release-92.0.0').
        :return: UpgradeResult with outcome details.
        """
        scenario = "upgrade_to_latest"
        start_time = time.time()
        log.info("=" * 70)
        log.info("SCENARIO: Upgrade to Latest Release")
        log.info("  from_version: %s", from_version)
        log.info("=" * 70)

        try:
            # Get expected target version
            all_versions = self.webui.get_release_versions()
            expected = all_versions["latestversion"]
            log.info("Target latest version: %s", expected)

            # Prepare: disable upgrade, install older build
            version_before = self._prepare_client(from_version)

            # Enable upgrade to latest
            self.webui.enable_upgrade_latest()
            self.client.update_config(wait_seconds=self.cfg.config_update_wait_seconds)

            # Poll for upgrade
            poll = self._wait_for_upgrade(expected_version=expected)
            version_after = poll.final_version

            # Verify WebUI
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
        golden_index: int = -1,
        dot: bool = False,
    ) -> UpgradeResult:
        """
        Scenario: Install older version, enable auto-upgrade to a golden
        release, wait and verify.

        :param from_version: Build to install first. If None, auto-picks
                             a version older than the target golden.
        :param golden_index: Index into sorted golden versions list.
                             -1 = latest golden, -2 = N-1, -3 = N-2.
        :param dot: If True, enable dot release within the golden version.
        :return: UpgradeResult with outcome details.
        """
        scenario = f"upgrade_to_golden(index={golden_index}, dot={dot})"
        start_time = time.time()
        log.info("=" * 70)
        log.info("SCENARIO: Upgrade to Golden Release")
        log.info("  golden_index: %d, dot: %s, from_version: %s", golden_index, dot, from_version)
        log.info("=" * 70)

        try:
            # Resolve golden version and expected target
            all_versions = self.webui.get_release_versions()
            golden_versions_sorted = sorted(all_versions["goldenversions"])
            golden_version = golden_versions_sorted[golden_index]
            log.info("Selected golden version: %s", golden_version)

            # Determine expected version after upgrade
            if dot:
                # Highest dot release within the golden
                expected = sorted(all_versions[golden_version])[-1]
            else:
                # Base golden version (lowest in the group)
                expected = sorted(all_versions[golden_version])[0]
            log.info("Expected version after upgrade: %s", expected)

            # Auto-pick from_version if not provided
            if from_version is None:
                version_list = self.webui.get_sorted_version_list()
                older_candidates = [
                    v for v in version_list
                    if int(v.split(".")[0]) < int(golden_version.split(".")[0])
                ]
                if not older_candidates:
                    raise ValueError(
                        f"No version older than golden {golden_version} found"
                    )
                older_version = max(older_candidates)
                from_version = f"release-{older_version}"
                log.info("Auto-picked from_version: %s", from_version)

            # Prepare: disable upgrade, install older build
            version_before = self._prepare_client(from_version)

            # Enable golden upgrade
            self.webui.enable_upgrade_golden(golden_version, dot=dot)
            self.client.update_config(wait_seconds=self.cfg.config_update_wait_seconds)

            # Poll for upgrade
            poll = self._wait_for_upgrade(expected_version=expected)
            version_after = poll.final_version

            # Verify WebUI
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

    def run_upgrade_disabled(self, from_version: str) -> UpgradeResult:
        """
        Scenario: Install a version with auto-upgrade disabled, verify
        the client does NOT upgrade.

        :param from_version: Build to install (e.g. 'release-92.0.0').
        :return: UpgradeResult with outcome details.
        """
        scenario = "upgrade_disabled"
        start_time = time.time()
        log.info("=" * 70)
        log.info("SCENARIO: Auto-Upgrade Disabled Verification")
        log.info("  from_version: %s", from_version)
        log.info("=" * 70)

        try:
            # Prepare: disable upgrade, install build
            version_before = self._prepare_client(from_version)

            # Update config with upgrade still disabled
            self.client.update_config(wait_seconds=self.cfg.config_update_wait_seconds)

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

            # Verify WebUI
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

    def _prepare_client(self, from_version: str) -> str:
        """
        Common setup: disable auto-upgrade, download and install a build.

        :param from_version: Build version to install (e.g. 'release-92.0.0').
        :return: Installed version string.
        """
        # Disable auto-upgrade first
        self.webui.disable_auto_upgrade()

        # Download the target build
        filename = self.client.get_installer_filename()
        info = self.client.download_build(
            build_version=from_version,
            installer_filename=filename,
        )

        # Uninstall existing client if present
        if self.client.is_installed():
            log.info("Existing client found — uninstalling first")
            self.client.uninstall()

        # Install the specified version
        self.client.install(setup_file_path=info["location"])
        version = self.client.get_version()
        log.info("Installed client version: %s", version)
        return version

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
        initial_version = self.client.get_version()

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
            current = self.client.get_version()

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

        final = self.client.get_version()
        log.warning(
            "Polling timed out after %ds — final version: %s",
            timeout, final,
        )
        return PollResult(changed=(final != initial_version), final_version=final, elapsed_seconds=elapsed)

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

    def _cleanup(self) -> None:
        """Reset tenant config to disable auto-upgrade."""
        try:
            self.webui.disable_auto_upgrade()
            log.info("Cleanup: auto-upgrade disabled on tenant")
        except Exception as exc:
            log.warning("Cleanup failed: %s", exc)
