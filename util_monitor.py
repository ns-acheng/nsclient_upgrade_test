"""
Upgrade timing monitor for the Netskope Client Upgrade Tool.

Runs as a background daemon thread, polling at ~1.5s intervals to detect
and timestamp 11 specific events during the client upgrade lifecycle.
Optionally triggers a reboot at a specified timing and persists state
to disk so monitoring can resume after reboot via ``python main.py continue``.
"""

import csv
import io
import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Optional

from util_client import LocalClient

log = logging.getLogger(__name__)

# ── Paths and constants ─────────────────────────────────────────────

MONITOR_STATE_PATH = Path(__file__).parent / "data" / "monitor_state.json"
MONITOR_TASK_NAME = "NsClientMonitorContinue"
MONITOR_BAT_PATH = Path(__file__).parent / "data" / "monitor_continue.bat"
NS_INSTALLATION_LOG = Path(
    r"C:\ProgramData\netskope\stagent\logs\nsInstallation.log"
)
POLL_INTERVAL = 1.5   # seconds between detector sweeps
MONITOR_TIMEOUT = 600  # 10 minutes total monitoring time
NS_MSI_DOWNLOAD_PATH = Path(
    r"C:\ProgramData\netskope\stagent\download\STAgent.msi"
)
MSI_MIN_SIZE = 25 * 1024 * 1024  # 25 MB threshold

TOTAL_TIMINGS = 13


# ── Timing events enum ──────────────────────────────────────────────

class TimingEvent(IntEnum):
    """The 13 upgrade lifecycle events to monitor."""

    CONFIG_SYNC_ALLOW_UPDATE = 1
    MSI_DOWNLOADED = 2
    MONITOR_PROCESS_STARTS = 3
    INSTALLATION_LOG_UPDATED = 4
    UI_PROCESS_GONE = 5
    SERVICE_STOP_PENDING = 6
    SVC_PROCESS_GONE = 7
    DRIVER_STOPPED = 8
    SERVICE_STOPPED = 9
    SERVICE_GONE = 10
    NEW_EXE_IN_DIR = 11
    SVC_RUNNING_NEW_PID = 12
    MONITOR_STOPPED_UPGRADED = 13

    @property
    def description(self) -> str:
        """Human-readable description for the timing report."""
        return _TIMING_DESCRIPTIONS[self.value]


_TIMING_DESCRIPTIONS: dict[int, str] = {
    1: "nsconfig.json clientUpdate.allowAutoUpdate = true",
    2: "STAgent.msi downloaded (>25 MB)",
    3: "stAgentSvcMon.exe -monitor starts",
    4: "nsInstallation.log created/updated",
    5: "stAgentUI.exe is gone",
    6: "stAgentSvc service stopped/stop_pending",
    7: "stAgentSvc.exe process gone",
    8: "stadrv service stopped/gone",
    9: "stAgentSvc service stopped (after process exit)",
    10: "stAgentSvc service removed from SCM",
    11: "New stAgentSvc.exe in target install dir",
    12: "stAgentSvc.exe running with new PID",
    13: "stAgentSvcMon.exe stopped & upgraded",
}


# ── Monitor state dataclass ─────────────────────────────────────────

@dataclass
class MonitorState:
    """Persisted state for cross-reboot timing monitor continuity."""

    monitor_start_time: str
    target_64_bit: bool
    reboot_time: Optional[int]
    reboot_delay: int

    initial_svc_pid: Optional[int]
    initial_mon_pid: Optional[int]
    initial_mon_version: str
    initial_log_mtime: Optional[float]
    initial_install_dir: str

    timings: dict[str, float] = field(default_factory=dict)
    reboot_triggered: bool = False
    pre_reboot_elapsed: float = 0.0
    log_dir: str = ""
    reboot_action: Optional[int] = None
    initial_allow_auto_update: bool = False
    initial_msi_size: int = 0

    # Context for post-reboot validation (set by upgrade runner)
    version_before: str = ""
    expected_version: str = ""
    scenario: str = ""
    source_64_bit: bool = False

    # Original sys.argv[1:] from the manual main.py invocation — used by
    # cmd_continue to map the post-reboot result back into the batch record.
    original_argv: list[str] = field(default_factory=list)


# ── State persistence ────────────────────────────────────────────────

def save_monitor_state(
    state: MonitorState,
    path: Optional[Path] = None,
) -> Path:
    """Write monitor state to JSON for post-reboot resume."""
    target = path or MONITOR_STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=4)
        f.write("\n")
    log.info("Monitor state saved to %s", target)
    return target


def load_monitor_state(
    path: Optional[Path] = None,
) -> Optional[MonitorState]:
    """Load monitor state from JSON, or None if missing/invalid."""
    target = path or MONITOR_STATE_PATH
    if not target.is_file():
        log.warning("No monitor state file at %s", target)
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        return MonitorState(**data)
    except Exception as exc:
        log.warning("Failed to load monitor state: %s", exc)
        return None


def clear_monitor_state(path: Optional[Path] = None) -> None:
    """Delete the monitor state file after continue completes."""
    target = path or MONITOR_STATE_PATH
    if target.is_file():
        target.unlink()
        log.info("Monitor state cleared: %s", target)


# ── Scheduled task helpers ───────────────────────────────────────────

def create_continue_task(
    bat_path: Optional[Path] = None,
    task_name: Optional[str] = None,
) -> None:
    """
    Create a Windows Task Scheduler task that runs ``python main.py continue``
    after user logon with a 30-second delay.
    """
    bat = bat_path or MONITOR_BAT_PATH
    name = task_name or MONITOR_TASK_NAME
    project_dir = Path(__file__).parent
    python_exe = sys.executable

    bat.parent.mkdir(parents=True, exist_ok=True)
    bat_content = (
        "@echo off\r\n"
        f'cd /d "{project_dir}"\r\n'
        "echo ============================================\r\n"
        "echo  Netskope Client Monitor Continue\r\n"
        "echo ============================================\r\n"
        "echo.\r\n"
        f'"{python_exe}" main.py continue\r\n'
        "echo.\r\n"
        "echo Monitor completed. Press any key to close...\r\n"
        "pause >nul\r\n"
    )
    bat.write_text(bat_content, encoding="utf-8")
    log.info("Wrote monitor continue batch file: %s", bat)

    result = subprocess.run(
        [
            "schtasks", "/create",
            "/tn", name,
            "/tr", str(bat),
            "/sc", "ONLOGON",
            "/delay", "0000:30",
            "/rl", "HIGHEST",
            "/it",
            "/f",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        log.warning(
            "schtasks /create failed (exit %d): %s",
            result.returncode, result.stderr,
        )
        raise RuntimeError(
            f"Failed to create scheduled task: {result.stderr.strip()}"
        )
    log.info(
        "Scheduled task '%s' created — will run on next logon", name,
    )


def delete_continue_task(
    bat_path: Optional[Path] = None,
    task_name: Optional[str] = None,
) -> None:
    """Delete the monitor-continue scheduled task and batch file."""
    name = task_name or MONITOR_TASK_NAME
    bat = bat_path or MONITOR_BAT_PATH

    try:
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", name, "/f"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            log.info("Scheduled task '%s' deleted", name)
        else:
            log.info(
                "Scheduled task '%s' not found or already deleted", name,
            )
    except Exception as exc:
        log.warning("Failed to delete scheduled task: %s", exc)

    if bat.is_file():
        bat.unlink()
        log.info("Deleted monitor continue batch file: %s", bat)


# ── Process helpers ──────────────────────────────────────────────────

def _get_process_pid(image_name: str) -> Optional[int]:
    """
    Get the PID of a process by image name, or None if not running.

    Uses ``tasklist /FI`` with CSV output for reliable parsing.
    """
    try:
        result = subprocess.run(
            [
                "tasklist",
                "/FI", f"IMAGENAME eq {image_name}",
                "/FO", "CSV",
                "/NH",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        for row in csv.reader(io.StringIO(result.stdout)):
            if len(row) >= 2 and row[0].lower() == image_name.lower():
                return int(row[1])
        return None
    except Exception as exc:
        log.debug("tasklist for %s failed: %s", image_name, exc)
        return None


def _is_process_running(image_name: str) -> bool:
    """Check if a process is currently running."""
    return _get_process_pid(image_name) is not None


def _get_process_commandline(
    image_name: str,
) -> list[tuple[int, str]]:
    """
    Get (PID, CommandLine) tuples for all instances of a process.

    Uses PowerShell ``Get-CimInstance`` for command-line inspection.
    """
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
        # Skip header row ("ProcessId","CommandLine"), parse data
        for row in csv.reader(lines[1:]):
            if len(row) >= 2 and row[0].strip().isdigit():
                pid = int(row[0].strip())
                cmdline = row[1].strip() if row[1] else ""
                entries.append((pid, cmdline))
        return entries
    except Exception as exc:
        log.debug(
            "PowerShell for %s failed: %s", image_name, exc,
        )
        return []


# ── Timing monitor class ────────────────────────────────────────────

class TimingMonitor:
    """
    Background daemon thread that detects and timestamps 11 upgrade
    lifecycle events.

    Usage::

        monitor = TimingMonitor(target_64_bit=True, reboot_time=5, reboot_delay=5)
        monitor.start()
        # ... main upgrade flow runs concurrently ...
        monitor.stop()
        monitor.print_report()
    """

    def __init__(
        self,
        target_64_bit: bool,
        reboot_time: Optional[int] = None,
        reboot_delay: int = 5,
        reboot_action: Optional[int] = None,
        timeout: int = MONITOR_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
        state: Optional[MonitorState] = None,
        log_dir: str = "",
        skip_continue_task: bool = False,
        version_before: str = "",
        expected_version: str = "",
        scenario: str = "",
        source_64_bit: bool = False,
        original_argv: list[str] | None = None,
        watchdog_mode: bool = False,
    ) -> None:
        self._target_64_bit = target_64_bit
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._skip_continue_task = skip_continue_task
        self._watchdog_mode = watchdog_mode

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._all_detected = threading.Event()
        self._lock = threading.Lock()

        # Track whether UI was ever seen running (avoid false positive
        # on timing 3 if UI was never observed)
        self._ui_was_seen = False

        if state is not None:
            self._state = state
        else:
            self._state = MonitorState(
                monitor_start_time=datetime.now().isoformat(),
                target_64_bit=target_64_bit,
                reboot_time=reboot_time,
                reboot_delay=reboot_delay,
                reboot_action=reboot_action,
                initial_svc_pid=None,
                initial_mon_pid=None,
                initial_mon_version="",
                initial_log_mtime=None,
                initial_install_dir="",
                log_dir=log_dir,
                version_before=version_before,
                expected_version=expected_version,
                scenario=scenario,
                source_64_bit=source_64_bit,
                original_argv=original_argv or [],
            )

        # Build detector map: timing number -> detector method
        self._detectors: dict[int, callable] = {
            1: self._detect_config_sync_allow_update,
            2: self._detect_msi_downloaded,
            3: self._detect_monitor_process,
            4: self._detect_installation_log,
            5: self._detect_ui_gone,
            6: self._detect_service_stop_pending,
            7: self._detect_svc_process_gone,
            8: self._detect_driver_stopped,
            9: self._detect_service_stopped_after_exit,
            10: self._detect_service_gone,
            11: self._detect_new_exe_in_dir,
            12: self._detect_svc_new_pid,
            13: self._detect_monitor_upgraded,
        }

        # In watchdog mode, stAgentSvcMon.exe lifecycle is managed
        # differently — timing 13 (monitor stopped & upgraded) will
        # never fire, so remove it from the detector map.
        if self._watchdog_mode:
            self._detectors.pop(13, None)
            log.info(
                "Watchdog mode: skipping timing 13 "
                "(stAgentSvcMon.exe not managed by monitor)"
            )

    # ── Public API ───────────────────────────────────────────────

    def start(self) -> None:
        """Take baseline snapshots and launch the monitor thread."""
        if self._state.initial_svc_pid is None and not self._state.timings:
            self._take_baselines()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="timing-monitor",
        )
        self._thread.start()
        log.info("Timing monitor started")

    def stop(self) -> None:
        """Signal the monitor thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("Timing monitor stopped")

    def get_timings(self) -> dict[int, float]:
        """Thread-safe read of detected timings."""
        with self._lock:
            return {
                int(k): v for k, v in self._state.timings.items()
            }

    def wait_for_completion(
        self, timeout: Optional[float] = None,
    ) -> bool:
        """Block until all timings detected or timeout."""
        wait_time = timeout if timeout is not None else self._timeout
        return self._all_detected.wait(timeout=wait_time)

    def wait_for_upgrade_complete(
        self,
        timeout: Optional[float] = None,
        settle_time: float = 15.0,
        extend_timeout: float = 900.0,
    ) -> bool:
        """
        Block until the upgrade is functionally complete, then give
        the monitor extra time to capture remaining timing events.

        The upgrade lifecycle is: old service stops → files replaced →
        new service starts with a different PID.  This method waits
        until the service has **cycled** (gone down or changed PID)
        and come back up, rather than accepting the still-running old
        service as "complete."

        When timing 1 (monitor process starts) or timing 2
        (nsInstallation.log updated) is detected, the deadline is
        extended by *extend_timeout* seconds so the upgrade has enough
        time to finish once it has actually started.

        :param timeout: Max seconds to wait for upgrade completion.
        :param settle_time: Extra seconds after upgrade is confirmed
                            to let the monitor capture late timings.
        :param extend_timeout: Seconds to extend the deadline when
                               timing 1 or 2 is first detected.
        :return: True if upgrade completed, False on timeout.
        """
        wait_time = timeout if timeout is not None else self._timeout
        deadline = time.time() + wait_time
        initial_pid = self._state.initial_svc_pid
        svc_went_down = False
        deadline_extended = False

        while time.time() < deadline and not self._stop_event.is_set():
            if self._all_detected.is_set():
                log.info("All timings already detected")
                return True

            # Extend deadline once when upgrade activity is detected
            if not deadline_extended:
                with self._lock:
                    upgrade_started = (
                        "1" in self._state.timings
                        or "2" in self._state.timings
                    )
                if upgrade_started:
                    deadline = time.time() + extend_timeout
                    deadline_extended = True
                    log.info(
                        "Upgrade activity detected — extending "
                        "deadline by %.0fs", extend_timeout,
                    )

            svc_info = LocalClient.query_service("stAgentSvc")
            svc_running = (
                svc_info.exists and svc_info.state == "RUNNING"
            )
            current_pid = _get_process_pid("stAgentSvc.exe")
            process_running = current_pid is not None
            install_dir = LocalClient.get_install_dir(
                self._target_64_bit
            )
            exe_exists = (install_dir / "stAgentSvc.exe").is_file()

            # Track whether the service has gone down during the wait
            if not svc_running or not process_running:
                if not svc_went_down:
                    log.info(
                        "Service went down (svc_running=%s, "
                        "process=%s) — waiting for restart",
                        svc_running, process_running,
                    )
                    svc_went_down = True

            # Upgrade is complete when:
            # - Service is running with a new PID (different from
            #   baseline), OR
            # - Service went down and came back up
            pid_changed = (
                initial_pid is not None
                and current_pid is not None
                and current_pid != initial_pid
            )
            restarted = svc_went_down and svc_running and process_running

            if (pid_changed or restarted) and exe_exists:
                log.info(
                    "Upgrade complete — service running (pid %s→%s), "
                    "exe present. Settling for %.0fs...",
                    initial_pid, current_pid, settle_time,
                )
                self._all_detected.wait(timeout=settle_time)
                return True

            log.debug(
                "Waiting: svc_running=%s, pid=%s (initial=%s), "
                "svc_went_down=%s, exe=%s",
                svc_running, current_pid, initial_pid,
                svc_went_down, exe_exists,
            )
            time.sleep(self._poll_interval)

        log.warning("Timed out waiting for upgrade to complete")
        return False

    def print_report(self) -> None:
        """Print formatted timing report to stdout and log file."""
        lines: list[str] = []
        lines.append(f"{'=' * 60}")
        lines.append("  Upgrade Timing Report")
        if self._watchdog_mode:
            lines.append("  (watchdog mode — timing 13 skipped)")
        lines.append(f"{'=' * 60}")
        for num in range(1, TOTAL_TIMINGS + 1):
            event = TimingEvent(num)
            key = str(num)
            with self._lock:
                offset = self._state.timings.get(key)
            if offset is not None:
                lines.append(
                    f"  [{offset:7.1f}s] {num:2d}. {event.description}"
                )
            elif self._watchdog_mode and num == 13:
                lines.append(
                    f"  [   SKIP] {num:2d}. {event.description}"
                )
            else:
                lines.append(
                    f"  [    N/A] {num:2d}. {event.description}"
                )
        with self._lock:
            detected = len(self._state.timings)
            reboot_triggered = self._state.reboot_triggered
            reboot_time = self._state.reboot_time
        expected_count = len(self._detectors)
        lines.append(f"\n  Detected: {detected}/{expected_count} timings")
        if reboot_triggered:
            lines.append(f"  Reboot triggered at timing {reboot_time}")
        report = "\n".join(lines)
        print(f"\n{report}\n")
        log.info("Timing report:\n%s", report)

    @property
    def state(self) -> MonitorState:
        """Return current state (for saving before reboot)."""
        return self._state

    # ── Baseline snapshots ───────────────────────────────────────

    def _take_baselines(self) -> None:
        """Capture initial state for comparison by detectors."""
        self._state.initial_svc_pid = _get_process_pid(
            "stAgentSvc.exe"
        )
        self._state.initial_mon_pid = _get_process_pid(
            "stAgentSvcMon.exe"
        )
        install_dir = LocalClient.get_install_dir(
            self._target_64_bit
        )
        self._state.initial_install_dir = str(install_dir)

        mon_exe = install_dir / "stAgentSvcMon.exe"
        if mon_exe.is_file():
            self._state.initial_mon_version = (
                LocalClient.get_file_version(mon_exe)
            )

        if NS_INSTALLATION_LOG.is_file():
            self._state.initial_log_mtime = (
                NS_INSTALLATION_LOG.stat().st_mtime
            )

        # Check if UI is currently running for timing 5 tracking
        if _is_process_running("stAgentUI.exe"):
            self._ui_was_seen = True

        # Baseline for timing 1: clientUpdate.allowAutoUpdate
        self._state.initial_allow_auto_update = (
            self._read_allow_auto_update()
        )

        # Baseline for timing 2: STAgent.msi download size
        if NS_MSI_DOWNLOAD_PATH.is_file():
            try:
                self._state.initial_msi_size = (
                    NS_MSI_DOWNLOAD_PATH.stat().st_size
                )
            except OSError:
                self._state.initial_msi_size = 0
        else:
            self._state.initial_msi_size = 0

        log.info(
            "Baselines: svc_pid=%s, mon_pid=%s, mon_ver=%s, "
            "log_mtime=%s, install_dir=%s, allow_auto_update=%s, "
            "msi_size=%s",
            self._state.initial_svc_pid,
            self._state.initial_mon_pid,
            self._state.initial_mon_version,
            self._state.initial_log_mtime,
            self._state.initial_install_dir,
            self._state.initial_allow_auto_update,
            self._state.initial_msi_size,
        )

    # ── Polling loop ─────────────────────────────────────────────

    def _run(self) -> None:
        """Main polling loop (runs as daemon thread)."""
        start = time.time()

        while not self._stop_event.is_set():
            elapsed = (
                time.time() - start + self._state.pre_reboot_elapsed
            )

            for timing_num, detector in self._detectors.items():
                key = str(timing_num)
                with self._lock:
                    already = key in self._state.timings
                if already:
                    continue
                try:
                    if detector():
                        with self._lock:
                            self._state.timings[key] = round(
                                elapsed, 1
                            )
                        log.info(
                            "Timing %d detected at %.1fs: %s",
                            timing_num,
                            elapsed,
                            TimingEvent(timing_num).description,
                        )

                        if self._state.reboot_time == timing_num:
                            self._trigger_reboot(elapsed)
                            return
                except Exception:
                    log.debug(
                        "Detector %d raised exception",
                        timing_num,
                        exc_info=True,
                    )

            with self._lock:
                count = len(self._state.timings)
            expected_count = len(self._detectors)
            if count >= expected_count:
                self._all_detected.set()
                log.info("All %d timings detected", expected_count)
                return

            if elapsed >= self._timeout:
                log.info(
                    "Monitor timed out after %.0fs", elapsed,
                )
                return

            self._stop_event.wait(timeout=self._poll_interval)

    # ── Reboot trigger ───────────────────────────────────────────

    def _trigger_reboot(self, elapsed: float) -> None:
        """Save state, create scheduled task, and trigger reboot.

        When ``reboot_action`` is set the reboot sequence changes:

        * **Action 2** — kill ``stAgentSvcMon.exe`` then reboot immediately.
        * **Action 3** — kill ``stAgentSvcMon.exe`` **and** ``msiexec.exe``
          then reboot immediately.
        * **None / default** — reboot after ``reboot_delay`` seconds.
        """
        self._state.reboot_triggered = True
        self._state.pre_reboot_elapsed = elapsed
        save_monitor_state(self._state)

        if self._skip_continue_task:
            log.info(
                "Skipping NsClientMonitorContinue task (batch mode — "
                "NsClientBatchContinue handles post-reboot continuation)"
            )
        else:
            create_continue_task()

        action = self._state.reboot_action

        if action in (2, 3):
            # Kill stAgentSvcMon.exe
            log.info("Action %d: killing stAgentSvcMon.exe", action)
            subprocess.run(
                ["taskkill", "/F", "/IM", "stAgentSvcMon.exe"],
                capture_output=True, text=True, timeout=10,
            )
            if action == 3:
                log.info("Action 3: killing msiexec.exe")
                subprocess.run(
                    ["taskkill", "/F", "/IM", "msiexec.exe"],
                    capture_output=True, text=True, timeout=10,
                )
            log.info(
                "Timing %d fired (action %d) — rebooting immediately",
                self._state.reboot_time, action,
            )
            subprocess.run(
                ["shutdown", "/r", "/f", "/t", "0"],
                capture_output=True, text=True, timeout=10,
            )
        else:
            log.info(
                "Timing %d fired — rebooting in %ds",
                self._state.reboot_time,
                self._state.reboot_delay,
            )
            subprocess.run(
                [
                    "shutdown", "/r", "/f", "/t",
                    str(self._state.reboot_delay),
                ],
                capture_output=True, text=True, timeout=10,
            )

    # ── Detector helpers ─────────────────────────────────────────

    @staticmethod
    def _read_allow_auto_update() -> bool:
        """Read clientUpdate.allowAutoUpdate from nsconfig.json."""
        path = LocalClient.NSCONFIG_PATH
        if not path.is_file():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
            value = (
                config.get("clientUpdate", {})
                .get("allowAutoUpdate", "")
            )
            return str(value).lower() == "true"
        except Exception:
            return False

    # ── Detector methods ─────────────────────────────────────────

    def _detect_config_sync_allow_update(self) -> bool:
        """Timing 1: nsconfig.json clientUpdate.allowAutoUpdate = true."""
        if self._state.initial_allow_auto_update:
            return False
        return self._read_allow_auto_update()

    def _detect_msi_downloaded(self) -> bool:
        """Timing 2: STAgent.msi downloaded (>25 MB)."""
        if not NS_MSI_DOWNLOAD_PATH.is_file():
            return False
        try:
            return NS_MSI_DOWNLOAD_PATH.stat().st_size >= MSI_MIN_SIZE
        except OSError:
            return False

    def _detect_monitor_process(self) -> bool:
        """Timing 3: stAgentSvcMon.exe -monitor starts."""
        entries = _get_process_commandline("stAgentSvcMon.exe")
        for pid, cmdline in entries:
            if "-monitor" in cmdline:
                # Skip if this was already running at baseline
                if (
                    self._state.initial_mon_pid is not None
                    and pid == self._state.initial_mon_pid
                ):
                    continue
                return True
        return False

    def _detect_installation_log(self) -> bool:
        """Timing 4: nsInstallation.log created/updated."""
        if not NS_INSTALLATION_LOG.is_file():
            return False
        current_mtime = NS_INSTALLATION_LOG.stat().st_mtime
        if self._state.initial_log_mtime is None:
            return True
        return current_mtime > self._state.initial_log_mtime

    def _detect_ui_gone(self) -> bool:
        """Timing 5: stAgentUI.exe is gone."""
        running = _is_process_running("stAgentUI.exe")
        if running:
            self._ui_was_seen = True
            return False
        return self._ui_was_seen

    def _detect_service_stop_pending(self) -> bool:
        """Timing 6: stAgentSvc service stopped/stop_pending/gone."""
        info = LocalClient.query_service("stAgentSvc")
        if not info.exists:
            # Service was removed entirely between polls — the
            # STOP_PENDING/STOPPED states were too brief to catch.
            return True
        return info.state in ("STOP_PENDING", "STOPPED")

    def _detect_svc_process_gone(self) -> bool:
        """Timing 7: stAgentSvc.exe process gone (keep PID)."""
        pid = _get_process_pid("stAgentSvc.exe")
        if pid is not None:
            # Still running — update last-known PID
            self._state.initial_svc_pid = pid
            return False
        # Process gone — only fire if we ever had a PID
        return self._state.initial_svc_pid is not None

    def _detect_driver_stopped(self) -> bool:
        """Timing 8: stadrv service stopped/stop_pending/gone."""
        info = LocalClient.query_service("stadrv")
        if not info.exists:
            return True
        return info.state in ("STOP_PENDING", "STOPPED")

    def _detect_service_stopped_after_exit(self) -> bool:
        """Timing 9: stAgentSvc service stopped/gone (after process exit)."""
        with self._lock:
            if "7" not in self._state.timings:
                return False
        info = LocalClient.query_service("stAgentSvc")
        if not info.exists:
            return True
        return info.state == "STOPPED"

    def _detect_service_gone(self) -> bool:
        """Timing 10: stAgentSvc service removed from SCM."""
        info = LocalClient.query_service("stAgentSvc")
        return not info.exists

    def _detect_new_exe_in_dir(self) -> bool:
        """Timing 11: New stAgentSvc.exe in target install dir."""
        install_dir = LocalClient.get_install_dir(
            self._target_64_bit
        )
        exe = install_dir / "stAgentSvc.exe"
        if not exe.is_file():
            return False
        # Fire if service was previously removed (timing 10) or if
        # the target dir differs from the initial install dir
        with self._lock:
            service_was_gone = "10" in self._state.timings
        if service_was_gone:
            return True
        return (
            str(install_dir) != self._state.initial_install_dir
        )

    def _detect_svc_new_pid(self) -> bool:
        """Timing 12: stAgentSvc.exe running with new PID."""
        pid = _get_process_pid("stAgentSvc.exe")
        if pid is None:
            return False
        return (
            self._state.initial_svc_pid is not None
            and pid != self._state.initial_svc_pid
        )

    def _detect_monitor_upgraded(self) -> bool:
        """Timing 13: stAgentSvcMon.exe stopped & upgraded."""
        if not self._state.initial_mon_version:
            return False
        install_dir = LocalClient.get_install_dir(
            self._target_64_bit
        )
        mon_exe = install_dir / "stAgentSvcMon.exe"
        if not mon_exe.is_file():
            return False
        current_version = LocalClient.get_file_version(mon_exe)
        if (
            not current_version
            or current_version == self._state.initial_mon_version
        ):
            return False
        return _is_process_running("stAgentSvcMon.exe")
