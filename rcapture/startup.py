from __future__ import annotations

import os
import sys
from pathlib import Path

_LABEL = "com.rcapture.app"
_REG_NAME = "rCapture"
_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def is_launch_at_login() -> bool:
    if sys.platform == "darwin":
        return _launch_agent_path().exists()
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, _REG_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False
    return False


def set_launch_at_login(enabled: bool) -> None:
    if sys.platform == "darwin":
        _set_darwin(enabled)
    elif sys.platform == "win32":
        _set_windows(enabled)


def _set_darwin(enabled: bool) -> None:
    import plistlib
    path = _launch_agent_path()
    if enabled:
        exe = sys.executable
        script = os.path.abspath(sys.argv[0])
        plist = {
            "Label": _LABEL,
            "ProgramArguments": [exe, script],
            "RunAtLoad": True,
            "KeepAlive": False,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
    else:
        if path.exists():
            path.unlink()


def _set_windows(enabled: bool) -> None:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY, 0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        )
        if enabled:
            exe = sys.executable
            script = os.path.abspath(sys.argv[0])
            winreg.SetValueEx(key, _REG_NAME, 0, winreg.REG_SZ, f'"{exe}" "{script}"')
        else:
            try:
                winreg.DeleteValue(key, _REG_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception:
        pass
