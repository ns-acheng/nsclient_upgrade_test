"""
Input monitor for the Netskope Client Upgrade Tool.

Monitors for ESC key press in a background daemon thread to allow
graceful stop of long-running operations (upgrade polling, timing
monitor, etc.).  Sets a ``threading.Event`` when ESC is detected.
"""

import logging
import sys
import threading
import time

log = logging.getLogger(__name__)


def drain_input() -> None:
    """
    Discard any buffered keystrokes so a previously-pressed ESC does not
    immediately re-trigger a freshly started monitor.
    """
    if sys.platform == "win32":
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()


def start_input_monitor(stop_event: threading.Event) -> None:
    """
    Start a daemon thread that monitors for ESC key press.

    :param stop_event: Event to set when ESC is detected.
    """
    if sys.platform == "win32":
        _start_windows_monitor(stop_event)
    else:
        _start_unix_monitor(stop_event)


def _start_windows_monitor(stop_event: threading.Event) -> None:
    """Windows ESC key monitor using msvcrt."""
    import msvcrt

    def _monitor() -> None:
        log.info("Input monitor started. Press ESC to stop.")
        while not stop_event.is_set():
            if msvcrt.kbhit():
                try:
                    key = msvcrt.getch()
                    if key == b"\x1b":
                        log.warning("ESC pressed. Stopping...")
                        stop_event.set()
                        break
                except Exception:
                    pass
            time.sleep(0.1)

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()


def _start_unix_monitor(stop_event: threading.Event) -> None:
    """Unix input monitor using select."""
    import select

    def _monitor() -> None:
        log.info("Input monitor started. Press Enter to stop.")
        while not stop_event.is_set():
            dr, _, _ = select.select([sys.stdin], [], [], 0.5)
            if dr:
                sys.stdin.readline()
                log.warning("Key detected. Stopping...")
                stop_event.set()
            time.sleep(0.1)

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()
