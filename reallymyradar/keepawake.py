"""Keep the computer from idle-sleeping while the radar server runs, like a media server does.

Only system sleep is blocked; the display may still turn off. Every mechanism is tied to this
process, so quitting or crashing the app releases it automatically:
  macOS    caffeinate -i -w <our pid>
  Windows  SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) on a dedicated thread
  Linux    systemd-inhibit --what=idle (if available)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time


class KeepAwake:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._win_stop: threading.Event | None = None

    @property
    def active(self) -> bool:
        if self._proc is not None:
            return self._proc.poll() is None
        return self._win_stop is not None

    def set(self, on: bool) -> None:
        if on and not self.active:
            self._start()
        elif not on and self.active:
            self._stop()

    def _start(self) -> None:
        if sys.platform == "darwin":
            self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif sys.platform == "win32":
            # The execution state belongs to the calling thread, so hold it on one that lives until stopped.
            stop = self._win_stop = threading.Event()

            def hold():
                import ctypes
                es_continuous, es_system_required = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
                stop.wait()
                ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)

            threading.Thread(target=hold, daemon=True, name="keep-awake").start()
        elif shutil.which("systemd-inhibit"):
            self._proc = subprocess.Popen(
                ["systemd-inhibit", "--what=idle", "--who=ReallymyRadar", "--why=Serving live radar",
                 "--mode=block", "sh", "-c", f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 30; done"])

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc = None
        if self._win_stop is not None:
            self._win_stop.set()
            self._win_stop = None


def follow_setting(get_enabled) -> KeepAwake:
    """Apply the `server.keep_awake` setting now and whenever it changes (viewer, API, file or tray)."""
    ka = KeepAwake()

    def loop():
        while True:
            try:
                ka.set(bool(get_enabled()))
            except Exception:
                pass
            time.sleep(5)

    threading.Thread(target=loop, daemon=True, name="keep-awake-sync").start()
    return ka
