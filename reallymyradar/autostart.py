"""Start ReallymyRadar when the user logs in (macOS LaunchAgent, Windows Run key, Linux XDG autostart)."""
from __future__ import annotations

import os
import plistlib
import shlex
import sys
from pathlib import Path

LABEL = "com.reallymyradar.app"


def launch_command() -> list[str]:
    """How to start this same install again: the frozen app binary, or this Python running the package."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "reallymyradar"]


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _linux_desktop() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "reallymyradar.desktop"


def is_enabled() -> bool:
    if sys.platform == "darwin":
        return _mac_plist().exists()
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, "ReallymyRadar")
                return True
        except OSError:
            return False
    return _linux_desktop().exists()


def enable() -> None:
    cmd = launch_command()
    if sys.platform == "darwin":
        p = _mac_plist()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True,
                                      "ProcessType": "Interactive"}))
    elif sys.platform == "win32":
        import subprocess
        import winreg
        line = subprocess.list2cmdline(cmd)
        if not getattr(sys, "frozen", False) and cmd[0].lower().endswith("python.exe"):
            line = subprocess.list2cmdline([cmd[0][:-10] + "pythonw.exe", *cmd[1:]])  # no console window
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ReallymyRadar", 0, winreg.REG_SZ, line)
    else:
        p = _linux_desktop()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[Desktop Entry]\nType=Application\nName=ReallymyRadar\n"
                     f"Exec={shlex.join(cmd)}\nX-GNOME-Autostart-enabled=true\nTerminal=false\n")


def disable() -> None:
    if sys.platform == "darwin":
        _mac_plist().unlink(missing_ok=True)
    elif sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "ReallymyRadar")
        except OSError:
            pass
    else:
        _linux_desktop().unlink(missing_ok=True)
