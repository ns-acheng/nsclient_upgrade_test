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


# ── Timing events enum ──────────────────────────────────────────────

class TimingEvent(IntEnum):
    """The 11 upgrade lifecycle events to monitor."""

    MONITOR_PROCESS_STARTS = 1
    INSTALLATION_LOG_UPDATED = 2
    UI_PROCESS_GONE = 3
    SERVICE_STOP_PENDING = 4
    SVC_PROCESS_GONE = 5
    DRIVER_STOPPED = 6
    SERVICE_STOPPED = 7
    SERVICE_GONE = 8
    NEW_EXE_IN_DIR = 9
    SVC_RUNNING_NEW_PID = 10
    MONITOR_STOPPED_UPGRADED = 11

    @property
    def description(self) -> str:
        """Human-readable description for the timing report."""
        return _TIMING_DESCRIPTIONS[self.value]


_TIMING_DESCRIPTIONS: dict[int, str] = {
    1: "stAgentSvcMon.exe -monitor starts",
    2: "nsInstallation.log created/updated",
    3: "stAgentUI.exe is gone",
    4: "stAgentSvc service stopped/stop_pending",
    5: "stAgentSvc.exe process gone",
    6: "stadrv service stopped/gone",
    7: "stAgentSvc service stopped (after process exit)",
    8: "stAgentSvc service removed from SCM",
    9: "New stAgentSvc.exe in target install dir",
    10: "stAgentSvc.exe running with new PID",
    11: "stAgentSvcMon.exe stopped & upgraded",
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

    Uses ``wmic process`` for command-line argument inspection.
    """
    try:
        result = subprocess.run(
            [
                "wmic", "process", "where",
                f"name='{image_name}'",
                "get", "ProcessId,CommandLine",
                "/FORMAT:CSV",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        entries: list[tuple[int, str]] = []
        for row in csv.reader(io.StringIO(result.stdout)):
            # CSV format: Node, CommandLine, ProcessId
            if len(row) >= 3 and row[2].strip().isdigit():
                pid = int(row[2].strip())
                cmdline = row[1].strip()
                entries.append((pid, cmdline))
        return entries
    except Exception as exc:
        log.debug("wmic for %s failed: %s", image_name, exc)
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
        timeout: int = MONITOR_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
        state: Optional[MonitorState] = None,
    ) -> None:
        self._target_64_bit = target_64_bit
        self._timeout = timeout
        self._poll_interval = poll_interval

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
                initial_svc_pid=None,
                initial_mon_pid=None,
                initial_mon_version="",
                initial_log_mtime=None,
                initial_install_dir="",
            )

        # Build detector map: timing number -> detector method
        self._detectors: dict[int, callable] = {
            1: self._detect_monitor_process,
            2: self._detect_installation_log,
            3: self._detect_ui_gone,
            4: self._detect_service_stop_pending,
            5: self._detect_svc_process_gone,
            6: self._detect_driver_stopped,
            7: self._detect_service_stopped_after_exit,
            8: self._detect_service_gone,
            9: self._detect_new_exe_in_dir,
            10: self._detect_svc_new_pid,
            11: self._detect_monitor_upgraded,
        }

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

    def print_report(self) -> None:
        """Print formatted timing report to stdout."""
        print(f"\n{'=' * 60}")
        print("  Upgrade Timing Report")
        print(f"{'=' * 60}")
        for num in range(1, 12):
            event = TimingEvent(num)
            key = str(num)
            with self._lock:
                offset = self._state.timings.get(key)
            if offset is not None:
                print(
                    f"  [{offset:7.1f}s] {num:2d}. {event.description}"
                )
            else:
                print(
                    f"  [    N/A] {num:2d}. {event.description}"
                )
        with self._lock:
            detected = len(self._state.timings)
            reboot_triggered = self._state.reboot_triggered
            reboot_time = self._state.reboot_time
        print(f"\n  Detected: {detected}/11 timings")
        if reboot_triggered:
            print(f"  Reboot triggered at timing {reboot_time}")
        print()

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

        # Check if UI is currently running for timing 3 tracking
        if _is_process_running("stAgentUI.exe"):
            self._ui_was_seen = True

        log.info(
            "Baselines: svc_pid=%s, mon_pid=%s, mon_ver=%s, "
            "log_mtime=%s, install_dir=%s",
            self._state.initial_svc_pid,
            self._state.initial_mon_pid,
            self._state.initial_mon_version,
            self._state.initial_log_mtime,
            self._state.initial_install_dir,
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
            if count == 11:
                self._all_detected.set()
                log.info("All 11 timings detected")
                return

            if elapsed >= self._timeout:
                log.info(
                    "Monitor timed out after %.0fs", elapsed,
                )
                return

            self._stop_event.wait(timeout=self._poll_interval)

    # ── Reboot trigger ───────────────────────────────────────────

    def _trigger_reboot(self, elapsed: float) -> None:
        """Save state, create scheduled task, and trigger reboot."""
        self._state.reboot_triggered = True
        self._state.pre_reboot_elapsed = elapsed
        save_monitor_state(self._state)

        create_continue_task()

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

    # ── Detector methods ─────────────────────────────────────────

    def _detect_monitor_process(self) -> bool:
        """Timing 1: stAgentSvcMon.exe -monitor starts."""
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
        """Timing 2: nsInstallation.log created/updated."""
        if not NS_INSTALLATION_LOG.is_file():
            return False
        current_mtime = NS_INSTALLATION_LOG.stat().st_mtime
        if self._state.initial_log_mtime is None:
            return True
        return current_mtime > self._state.initial_log_mtime

    def _detect_ui_gone(self) -> bool:
        """Timing 3: stAgentUI.exe is gone."""
        running = _is_process_running("stAgentUI.exe")
        if running:
            self._ui_was_seen = True
            return False
        return self._ui_was_seen

    def _detect_service_stop_pending(self) -> bool:
        """Timing 4: stAgentSvc service stopped/stop_pending."""
        info = LocalClient.query_service("stAgentSvc")
        return info.exists and info.state in (
            "STOP_PENDING", "STOPPED",
        )

    def _detect_svc_process_gone(self) -> bool:
        """Timing 5: stAgentSvc.exe process gone (keep PID)."""
        pid = _get_process_pid("stAgentSvc.exe")
        if pid is not None:
            # Still running — update last-known PID
            self._state.initial_svc_pid = pid
            return False
        # Process gone — only fire if we ever had a PID
        return self._state.initial_svc_pid is not None

    def _detect_driver_stopped(self) -> bool:
        """Timing 6: stadrv service stopped/stop_pending/gone."""
        info = LocalClient.query_service("stadrv")
        if not info.exists:
            return True
        return info.state in ("STOP_PENDING", "STOPPED")

    def _detect_service_stopped_after_exit(self) -> bool:
        """Timing 7: stAgentSvc service stopped (after process exit)."""
        with self._lock:
            if "5" not in self._state.timings:
                return False
        info = LocalClient.query_service("stAgentSvc")
        return info.exists and info.state == "STOPPED"

    def _detect_service_gone(self) -> bool:
        """Timing 8: stAgentSvc service removed from SCM."""
        info = LocalClient.query_service("stAgentSvc")
        return not info.exists

    def _detect_new_exe_in_dir(self) -> bool:
        """Timing 9: New stAgentSvc.exe in target install dir."""
        install_dir = LocalClient.get_install_dir(
            self._target_64_bit
        )
        exe = install_dir / "stAgentSvc.exe"
        if not exe.is_file():
            return False
        # Fire if service was previously removed (timing 8) or if
        # the target dir differs from the initial install dir
        with self._lock:
            service_was_gone = "8" in self._state.timings
        if service_was_gone:
            return True
        return (
            str(install_dir) != self._state.initial_install_dir
        )

    def _detect_svc_new_pid(self) -> bool:
        """Timing 10: stAgentSvc.exe running with new PID."""
        pid = _get_process_pid("stAgentSvc.exe")
        if pid is None:
            return False
        return (
            self._state.initial_svc_pid is not None
            and pid != self._state.initial_svc_pid
        )

    def _detect_monitor_upgraded(self) -> bool:
        """Timing 11: stAgentSvcMon.exe stopped & upgraded."""
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
