"""
Unit tests for util_monitor.py — upgrade timing monitor.
All subprocess calls (tasklist, PowerShell, sc, schtasks, shutdown) are mocked.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from util_monitor import (
    TimingEvent,
    MonitorState,
    TimingMonitor,
    save_monitor_state,
    load_monitor_state,
    clear_monitor_state,
    create_continue_task,
    delete_continue_task,
    _get_process_pid,
    _is_process_running,
    _get_process_commandline,
    MONITOR_STATE_PATH,
    MONITOR_TASK_NAME,
    MONITOR_BAT_PATH,
    NS_INSTALLATION_LOG,
)


# ── TimingEvent ──────────────────────────────────────────────────────


class TestTimingEvent:
    """Tests for the TimingEvent enum."""

    def test_all_11_events_defined(self) -> None:
        assert len(TimingEvent) == 11

    def test_values_1_through_11(self) -> None:
        for i in range(1, 12):
            assert TimingEvent(i).value == i

    def test_descriptions_defined(self) -> None:
        for event in TimingEvent:
            assert isinstance(event.description, str)
            assert len(event.description) > 0


# ── MonitorState persistence ─────────────────────────────────────────


def _sample_state(**overrides: object) -> MonitorState:
    """Create a MonitorState with sensible defaults."""
    defaults = dict(
        monitor_start_time="2026-04-08T10:00:00",
        target_64_bit=True,
        reboot_time=5,
        reboot_delay=5,
        initial_svc_pid=1234,
        initial_mon_pid=5678,
        initial_mon_version="135.0.0.2631",
        initial_log_mtime=1700000000.0,
        initial_install_dir=r"C:\Program Files\Netskope\STAgent",
        timings={},
        reboot_triggered=False,
        pre_reboot_elapsed=0.0,
    )
    defaults.update(overrides)
    return MonitorState(**defaults)


class TestMonitorStatePersistence:
    """Tests for save/load/clear monitor state."""

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        state = _sample_state(timings={"3": 12.5, "5": 30.1})
        path = tmp_path / "state.json"
        save_monitor_state(state, path=path)

        loaded = load_monitor_state(path=path)
        assert loaded is not None
        assert loaded.monitor_start_time == state.monitor_start_time
        assert loaded.timings == {"3": 12.5, "5": 30.1}
        assert loaded.initial_svc_pid == 1234

    def test_load_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.json"
        assert load_monitor_state(path=path) is None

    def test_load_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("not valid json", encoding="utf-8")
        assert load_monitor_state(path=path) is None

    def test_clear_deletes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{}", encoding="utf-8")
        clear_monitor_state(path=path)
        assert not path.exists()

    def test_clear_missing_file_no_error(self, tmp_path: Path) -> None:
        path = tmp_path / "nope.json"
        clear_monitor_state(path=path)  # Should not raise


# ── Process helpers ──────────────────────────────────────────────────


class TestProcessHelpers:
    """Tests for _get_process_pid, _is_process_running, _get_process_commandline."""

    @patch("util_monitor.subprocess.run")
    def test_get_process_pid_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='"stAgentSvc.exe","4321","Services","0","12,345 K"\r\n',
        )
        assert _get_process_pid("stAgentSvc.exe") == 4321

    @patch("util_monitor.subprocess.run")
    def test_get_process_pid_not_found(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="INFO: No tasks are running which match the specified criteria.\r\n",
        )
        assert _get_process_pid("stAgentSvc.exe") is None

    @patch("util_monitor.subprocess.run")
    def test_is_process_running_true(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='"stAgentUI.exe","9999","Console","1","5,000 K"\r\n',
        )
        assert _is_process_running("stAgentUI.exe") is True

    @patch("util_monitor.subprocess.run")
    def test_is_process_running_false(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="INFO: No tasks are running which match the specified criteria.\r\n",
        )
        assert _is_process_running("stAgentUI.exe") is False

    @patch("util_monitor.subprocess.run")
    def test_get_process_commandline(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                '"ProcessId","CommandLine"\r\n'
                '"5678","stAgentSvcMon.exe -monitor"\r\n'
            ),
        )
        entries = _get_process_commandline("stAgentSvcMon.exe")
        assert len(entries) == 1
        assert entries[0] == (5678, "stAgentSvcMon.exe -monitor")

    @patch("util_monitor.subprocess.run")
    def test_get_process_commandline_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='"ProcessId","CommandLine"\r\n',
        )
        assert _get_process_commandline("stAgentSvcMon.exe") == []


# ── Scheduled task helpers ───────────────────────────────────────────


class TestScheduledTask:
    """Tests for create_continue_task and delete_continue_task."""

    @patch("util_monitor.subprocess.run")
    def test_create_continue_task(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        bat = tmp_path / "continue.bat"
        create_continue_task(bat_path=bat, task_name="TestTask")
        assert bat.exists()
        assert "main.py continue" in bat.read_text(encoding="utf-8")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "schtasks" in args
        assert "TestTask" in args

    @patch("util_monitor.subprocess.run")
    def test_create_continue_task_failure_raises(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        mock_run.return_value = MagicMock(
            returncode=1, stderr="Access denied",
        )
        bat = tmp_path / "continue.bat"
        with pytest.raises(RuntimeError, match="Access denied"):
            create_continue_task(bat_path=bat, task_name="TestTask")

    @patch("util_monitor.subprocess.run")
    def test_delete_continue_task(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        bat = tmp_path / "continue.bat"
        bat.write_text("dummy", encoding="utf-8")
        delete_continue_task(bat_path=bat, task_name="TestTask")
        assert not bat.exists()
        mock_run.assert_called_once()


# ── Individual detectors ─────────────────────────────────────────────


class TestDetectors:
    """Tests for each of the 11 timing detector methods."""

    def _make_monitor(self, **state_overrides: object) -> TimingMonitor:
        """Create a TimingMonitor with a pre-built state (no baselines)."""
        state = _sample_state(**state_overrides)
        monitor = TimingMonitor(
            target_64_bit=state.target_64_bit,
            reboot_time=state.reboot_time,
            reboot_delay=state.reboot_delay,
            state=state,
        )
        return monitor

    # -- Timing 1: stAgentSvcMon.exe -monitor starts --

    @patch("util_monitor._get_process_commandline")
    def test_detect_monitor_process_new(
        self, mock_cmd: MagicMock,
    ) -> None:
        mock_cmd.return_value = [
            (9999, "stAgentSvcMon.exe -monitor"),
        ]
        m = self._make_monitor(initial_mon_pid=5678)
        assert m._detect_monitor_process() is True

    @patch("util_monitor._get_process_commandline")
    def test_detect_monitor_process_same_pid(
        self, mock_cmd: MagicMock,
    ) -> None:
        """Same PID as baseline — not a new event."""
        mock_cmd.return_value = [
            (5678, "stAgentSvcMon.exe -monitor"),
        ]
        m = self._make_monitor(initial_mon_pid=5678)
        assert m._detect_monitor_process() is False

    @patch("util_monitor._get_process_commandline")
    def test_detect_monitor_process_no_flag(
        self, mock_cmd: MagicMock,
    ) -> None:
        mock_cmd.return_value = [
            (9999, "stAgentSvcMon.exe"),
        ]
        m = self._make_monitor()
        assert m._detect_monitor_process() is False

    # -- Timing 2: nsInstallation.log created/updated --

    @patch("util_monitor.NS_INSTALLATION_LOG")
    def test_detect_installation_log_updated(
        self, mock_log_path: MagicMock,
    ) -> None:
        mock_log_path.is_file.return_value = True
        mock_log_path.stat.return_value = MagicMock(
            st_mtime=1700000100.0,
        )
        m = self._make_monitor(initial_log_mtime=1700000000.0)
        assert m._detect_installation_log() is True

    @patch("util_monitor.NS_INSTALLATION_LOG")
    def test_detect_installation_log_not_updated(
        self, mock_log_path: MagicMock,
    ) -> None:
        mock_log_path.is_file.return_value = True
        mock_log_path.stat.return_value = MagicMock(
            st_mtime=1700000000.0,
        )
        m = self._make_monitor(initial_log_mtime=1700000000.0)
        assert m._detect_installation_log() is False

    @patch("util_monitor.NS_INSTALLATION_LOG")
    def test_detect_installation_log_created(
        self, mock_log_path: MagicMock,
    ) -> None:
        """File created after monitoring started (no initial mtime)."""
        mock_log_path.is_file.return_value = True
        mock_log_path.stat.return_value = MagicMock(
            st_mtime=1700000100.0,
        )
        m = self._make_monitor(initial_log_mtime=None)
        assert m._detect_installation_log() is True

    @patch("util_monitor.NS_INSTALLATION_LOG")
    def test_detect_installation_log_missing(
        self, mock_log_path: MagicMock,
    ) -> None:
        mock_log_path.is_file.return_value = False
        m = self._make_monitor()
        assert m._detect_installation_log() is False

    # -- Timing 3: stAgentUI.exe is gone --

    @patch("util_monitor._is_process_running")
    def test_detect_ui_gone_after_seen(
        self, mock_running: MagicMock,
    ) -> None:
        mock_running.return_value = False
        m = self._make_monitor()
        m._ui_was_seen = True
        assert m._detect_ui_gone() is True

    @patch("util_monitor._is_process_running")
    def test_detect_ui_gone_never_seen(
        self, mock_running: MagicMock,
    ) -> None:
        """UI was never observed — don't report as gone."""
        mock_running.return_value = False
        m = self._make_monitor()
        m._ui_was_seen = False
        assert m._detect_ui_gone() is False

    @patch("util_monitor._is_process_running")
    def test_detect_ui_still_running(
        self, mock_running: MagicMock,
    ) -> None:
        mock_running.return_value = True
        m = self._make_monitor()
        assert m._detect_ui_gone() is False
        assert m._ui_was_seen is True  # Side effect: marks as seen

    # -- Timing 4: stAgentSvc service stopped/stop_pending --

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_service_stop_pending(
        self, mock_query: MagicMock,
    ) -> None:
        mock_query.return_value = MagicMock(
            exists=True, state="STOP_PENDING",
        )
        m = self._make_monitor()
        assert m._detect_service_stop_pending() is True

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_service_running(
        self, mock_query: MagicMock,
    ) -> None:
        mock_query.return_value = MagicMock(
            exists=True, state="RUNNING",
        )
        m = self._make_monitor()
        assert m._detect_service_stop_pending() is False

    # -- Timing 5: stAgentSvc.exe process gone --

    @patch("util_monitor._get_process_pid")
    def test_detect_svc_process_gone(
        self, mock_pid: MagicMock,
    ) -> None:
        mock_pid.return_value = None
        m = self._make_monitor(initial_svc_pid=1234)
        assert m._detect_svc_process_gone() is True

    @patch("util_monitor._get_process_pid")
    def test_detect_svc_process_still_running(
        self, mock_pid: MagicMock,
    ) -> None:
        mock_pid.return_value = 1234
        m = self._make_monitor(initial_svc_pid=1234)
        assert m._detect_svc_process_gone() is False

    @patch("util_monitor._get_process_pid")
    def test_detect_svc_process_never_existed(
        self, mock_pid: MagicMock,
    ) -> None:
        """No initial PID — can't detect disappearance."""
        mock_pid.return_value = None
        m = self._make_monitor(initial_svc_pid=None)
        assert m._detect_svc_process_gone() is False

    # -- Timing 6: stadrv service stopped/gone --

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_driver_stopped(
        self, mock_query: MagicMock,
    ) -> None:
        mock_query.return_value = MagicMock(
            exists=True, state="STOPPED",
        )
        m = self._make_monitor()
        assert m._detect_driver_stopped() is True

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_driver_gone(
        self, mock_query: MagicMock,
    ) -> None:
        mock_query.return_value = MagicMock(exists=False, state="")
        m = self._make_monitor()
        assert m._detect_driver_stopped() is True

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_driver_running(
        self, mock_query: MagicMock,
    ) -> None:
        mock_query.return_value = MagicMock(
            exists=True, state="RUNNING",
        )
        m = self._make_monitor()
        assert m._detect_driver_stopped() is False

    # -- Timing 7: stAgentSvc service stopped after process exit --

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_service_stopped_after_exit(
        self, mock_query: MagicMock,
    ) -> None:
        mock_query.return_value = MagicMock(
            exists=True, state="STOPPED",
        )
        m = self._make_monitor(timings={"5": 30.0})
        assert m._detect_service_stopped_after_exit() is True

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_service_stopped_before_exit(
        self, mock_query: MagicMock,
    ) -> None:
        """Timing 5 not yet detected — should not fire."""
        mock_query.return_value = MagicMock(
            exists=True, state="STOPPED",
        )
        m = self._make_monitor(timings={})
        assert m._detect_service_stopped_after_exit() is False

    # -- Timing 8: stAgentSvc service removed from SCM --

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_service_gone(
        self, mock_query: MagicMock,
    ) -> None:
        mock_query.return_value = MagicMock(exists=False, state="")
        m = self._make_monitor()
        assert m._detect_service_gone() is True

    @patch("util_monitor.LocalClient.query_service")
    def test_detect_service_still_exists(
        self, mock_query: MagicMock,
    ) -> None:
        mock_query.return_value = MagicMock(
            exists=True, state="STOPPED",
        )
        m = self._make_monitor()
        assert m._detect_service_gone() is False

    # -- Timing 9: New stAgentSvc.exe in target dir --

    @patch("util_monitor.LocalClient.get_install_dir")
    def test_detect_new_exe_after_service_gone(
        self, mock_dir: MagicMock, tmp_path: Path,
    ) -> None:
        mock_dir.return_value = tmp_path
        (tmp_path / "stAgentSvc.exe").write_text("dummy")
        m = self._make_monitor(timings={"8": 50.0})
        assert m._detect_new_exe_in_dir() is True

    @patch("util_monitor.LocalClient.get_install_dir")
    def test_detect_new_exe_different_dir(
        self, mock_dir: MagicMock, tmp_path: Path,
    ) -> None:
        """Cross-arch: target dir differs from initial."""
        mock_dir.return_value = tmp_path
        (tmp_path / "stAgentSvc.exe").write_text("dummy")
        m = self._make_monitor(
            initial_install_dir=r"C:\other\path",
            timings={},
        )
        assert m._detect_new_exe_in_dir() is True

    @patch("util_monitor.LocalClient.get_install_dir")
    def test_detect_new_exe_not_yet(
        self, mock_dir: MagicMock, tmp_path: Path,
    ) -> None:
        mock_dir.return_value = tmp_path
        # No exe file
        m = self._make_monitor(timings={"8": 50.0})
        assert m._detect_new_exe_in_dir() is False

    # -- Timing 10: stAgentSvc.exe running with new PID --

    @patch("util_monitor._get_process_pid")
    def test_detect_svc_new_pid(
        self, mock_pid: MagicMock,
    ) -> None:
        mock_pid.return_value = 9999
        m = self._make_monitor(initial_svc_pid=1234)
        assert m._detect_svc_new_pid() is True

    @patch("util_monitor._get_process_pid")
    def test_detect_svc_same_pid(
        self, mock_pid: MagicMock,
    ) -> None:
        mock_pid.return_value = 1234
        m = self._make_monitor(initial_svc_pid=1234)
        assert m._detect_svc_new_pid() is False

    @patch("util_monitor._get_process_pid")
    def test_detect_svc_not_running(
        self, mock_pid: MagicMock,
    ) -> None:
        mock_pid.return_value = None
        m = self._make_monitor(initial_svc_pid=1234)
        assert m._detect_svc_new_pid() is False

    # -- Timing 11: stAgentSvcMon.exe stopped & upgraded --

    @patch("util_monitor._is_process_running")
    @patch("util_monitor.LocalClient.get_file_version")
    @patch("util_monitor.LocalClient.get_install_dir")
    def test_detect_monitor_upgraded(
        self,
        mock_dir: MagicMock,
        mock_ver: MagicMock,
        mock_running: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_dir.return_value = tmp_path
        (tmp_path / "stAgentSvcMon.exe").write_text("dummy")
        mock_ver.return_value = "136.0.0.2700"
        mock_running.return_value = True
        m = self._make_monitor(
            initial_mon_version="135.0.0.2631",
        )
        assert m._detect_monitor_upgraded() is True

    @patch("util_monitor._is_process_running")
    @patch("util_monitor.LocalClient.get_file_version")
    @patch("util_monitor.LocalClient.get_install_dir")
    def test_detect_monitor_same_version(
        self,
        mock_dir: MagicMock,
        mock_ver: MagicMock,
        mock_running: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_dir.return_value = tmp_path
        (tmp_path / "stAgentSvcMon.exe").write_text("dummy")
        mock_ver.return_value = "135.0.0.2631"
        mock_running.return_value = True
        m = self._make_monitor(
            initial_mon_version="135.0.0.2631",
        )
        assert m._detect_monitor_upgraded() is False

    @patch("util_monitor.LocalClient.get_install_dir")
    def test_detect_monitor_no_initial_version(
        self, mock_dir: MagicMock, tmp_path: Path,
    ) -> None:
        """No initial version — can't detect upgrade."""
        mock_dir.return_value = tmp_path
        m = self._make_monitor(initial_mon_version="")
        assert m._detect_monitor_upgraded() is False


# ── Polling loop ─────────────────────────────────────────────────────


class TestPollingLoop:
    """Tests for the monitor's main _run loop."""

    def test_stop_event_exits_loop(self) -> None:
        """Monitor exits promptly when stop event is set."""
        state = _sample_state()
        monitor = TimingMonitor(
            target_64_bit=True,
            state=state,
            poll_interval=0.05,
            timeout=60,
        )
        # Don't call start() — call _run directly in foreground
        # but set stop_event after a short delay
        monitor._stop_event.set()
        monitor._run()  # Should return immediately

    @patch("util_monitor._get_process_pid", return_value=None)
    @patch("util_monitor._is_process_running", return_value=False)
    @patch("util_monitor._get_process_commandline", return_value=[])
    @patch("util_monitor.LocalClient.query_service")
    @patch("util_monitor.LocalClient.get_install_dir")
    @patch("util_monitor.LocalClient.get_file_version", return_value="")
    @patch("util_monitor.NS_INSTALLATION_LOG")
    def test_timeout_exits_loop(
        self,
        mock_log: MagicMock,
        mock_ver: MagicMock,
        mock_dir: MagicMock,
        mock_query: MagicMock,
        mock_cmd: MagicMock,
        mock_running: MagicMock,
        mock_pid: MagicMock,
    ) -> None:
        """Monitor exits when timeout is reached."""
        mock_log.is_file.return_value = False
        mock_query.return_value = MagicMock(
            exists=True, state="RUNNING",
        )
        mock_dir.return_value = Path(r"C:\fake")

        state = _sample_state(initial_svc_pid=None)
        monitor = TimingMonitor(
            target_64_bit=True,
            state=state,
            poll_interval=0.01,
            timeout=0.05,
        )
        monitor._run()
        # Should exit without error due to timeout


# ── Reboot trigger ───────────────────────────────────────────────────


class TestRebootTrigger:
    """Tests for the reboot trigger flow."""

    @patch("util_monitor.subprocess.run")
    @patch("util_monitor.create_continue_task")
    @patch("util_monitor.save_monitor_state")
    def test_trigger_reboot_saves_state(
        self,
        mock_save: MagicMock,
        mock_task: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        state = _sample_state(reboot_time=5, reboot_delay=10)
        monitor = TimingMonitor(
            target_64_bit=True,
            state=state,
        )
        monitor._trigger_reboot(elapsed=45.0)

        assert state.reboot_triggered is True
        assert state.pre_reboot_elapsed == 45.0
        mock_save.assert_called_once_with(state)
        mock_task.assert_called_once()
        mock_run.assert_called_once()
        shutdown_args = mock_run.call_args[0][0]
        assert "shutdown" in shutdown_args
        assert "10" in shutdown_args


# ── Report printing ──────────────────────────────────────────────────


class TestReport:
    """Tests for print_report output."""

    def test_report_with_some_timings(self, capsys: pytest.CaptureFixture) -> None:
        state = _sample_state(
            timings={"1": 5.0, "4": 20.3, "10": 55.0},
        )
        monitor = TimingMonitor(
            target_64_bit=True, state=state,
        )
        monitor.print_report()
        output = capsys.readouterr().out
        assert "Upgrade Timing Report" in output
        assert "5.0s" in output
        assert "20.3s" in output
        assert "55.0s" in output
        assert "N/A" in output
        assert "Detected: 3/11" in output

    def test_report_with_reboot(self, capsys: pytest.CaptureFixture) -> None:
        state = _sample_state(
            timings={"5": 30.0},
            reboot_triggered=True,
            reboot_time=5,
        )
        monitor = TimingMonitor(
            target_64_bit=True, state=state,
        )
        monitor.print_report()
        output = capsys.readouterr().out
        assert "Reboot triggered at timing 5" in output

    def test_report_empty_timings(self, capsys: pytest.CaptureFixture) -> None:
        state = _sample_state(timings={})
        monitor = TimingMonitor(
            target_64_bit=True, state=state,
        )
        monitor.print_report()
        output = capsys.readouterr().out
        assert "Detected: 0/11" in output


# ── Resume after reboot ──────────────────────────────────────────────


class TestResumeAfterReboot:
    """Tests for monitor resumption with pre-reboot state."""

    def test_pre_reboot_elapsed_offset(self) -> None:
        """Timings after reboot include pre-reboot elapsed offset."""
        state = _sample_state(
            timings={"5": 30.0},
            pre_reboot_elapsed=45.0,
            reboot_triggered=True,
            reboot_time=5,
        )
        monitor = TimingMonitor(
            target_64_bit=True,
            reboot_time=None,  # Don't re-reboot
            state=state,
        )
        # Pre-existing timings preserved
        assert monitor.get_timings() == {5: 30.0}
        # New timings will be offset by pre_reboot_elapsed
        assert monitor._state.pre_reboot_elapsed == 45.0
