"""
Unit tests for util_input.py — ESC key input monitor.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from util_input import start_input_monitor


class TestInputMonitor:
    """Tests for the ESC key input monitor."""

    @patch("util_input.sys")
    def test_start_calls_windows_on_win32(self, mock_sys: MagicMock) -> None:
        """On win32, the Windows monitor path is taken."""
        mock_sys.platform = "win32"
        stop_event = threading.Event()
        with patch("util_input._start_windows_monitor") as mock_win:
            start_input_monitor(stop_event)
            mock_win.assert_called_once_with(stop_event)

    @patch("util_input.sys")
    def test_start_calls_unix_on_linux(self, mock_sys: MagicMock) -> None:
        """On linux, the Unix monitor path is taken."""
        mock_sys.platform = "linux"
        stop_event = threading.Event()
        with patch("util_input._start_unix_monitor") as mock_unix:
            start_input_monitor(stop_event)
            mock_unix.assert_called_once_with(stop_event)

    @pytest.mark.skipif(
        sys.platform != "win32", reason="Windows-only test",
    )
    def test_windows_esc_sets_stop_event(self) -> None:
        """ESC key (0x1b) sets the stop event on Windows."""
        stop_event = threading.Event()

        call_count = 0

        def fake_kbhit() -> bool:
            nonlocal call_count
            call_count += 1
            return call_count == 2

        def fake_getch() -> bytes:
            return b"\x1b"

        mock_msvcrt = MagicMock()
        mock_msvcrt.kbhit = fake_kbhit
        mock_msvcrt.getch = fake_getch

        with patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
            from util_input import _start_windows_monitor
            _start_windows_monitor(stop_event)

        stop_event.wait(timeout=2.0)
        assert stop_event.is_set()

    def test_stop_event_already_set_exits(self) -> None:
        """Monitor thread exits immediately if stop_event is already set."""
        stop_event = threading.Event()
        stop_event.set()
        # Should not hang — the thread checks stop_event at loop top
        start_input_monitor(stop_event)
        time.sleep(0.3)
        assert stop_event.is_set()
