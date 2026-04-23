from __future__ import annotations

import sys
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from .config import ACTIONS


# ======================================================================
# Backend abstraction
# ======================================================================
#
# We ship two implementations and pick at runtime:
#
#   * ``_NSEventBackend`` — macOS only. Uses AppKit's
#     ``NSEvent.addGlobalMonitorForEventsMatchingMask_handler_``, which runs
#     the handler on the main thread, integrates natively with the app's run
#     loop, and plays nicely with ``NSApplicationActivationPolicyAccessory``.
#     This replaces a previous pynput-based setup that reliably crashed on
#     macOS whenever its Quartz event tap was torn down and recreated (e.g.
#     after closing the hotkey-settings dialog).
#
#   * ``_PynputBackend`` — cross-platform fallback for Windows/Linux (and a
#     safety net on macOS if AppKit is missing). Uses pynput's
#     ``GlobalHotKeys``.
#
# Both backends consume the same pynput-style hotkey strings ("<cmd>+<shift>+r")
# and map action names → Qt signals declared on ``HotkeyBridge``.
# ======================================================================


class HotkeyBridge(QObject):
    """Public front-end. Owns the Qt signals; delegates I/O to a backend."""

    full_screenshot = Signal()
    region_screenshot = Signal()
    long_screenshot = Signal()
    toggle_full_record = Signal()
    toggle_region_record = Signal()

    def __init__(self, bindings: Optional[dict[str, str]] = None) -> None:
        super().__init__()
        self._bindings: dict[str, str] = dict(bindings or {})
        self._backend: Optional[_Backend] = None
        self._last_error: Optional[str] = None

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def bindings(self) -> dict[str, str]:
        return dict(self._bindings)

    # --- public API ---
    def start(self, bindings: Optional[dict[str, str]] = None) -> bool:
        if bindings is not None:
            self._bindings = dict(bindings)
        self._ensure_backend()
        if self._backend is None:
            return False
        return self._backend.start(self._bindings)

    def stop(self) -> None:
        if self._backend is not None:
            self._backend.stop()

    def reload(self, bindings: dict[str, str]) -> bool:
        self.stop()
        return self.start(bindings)

    # --- internal ---
    def _ensure_backend(self) -> None:
        if self._backend is not None:
            return
        if sys.platform == "darwin":
            try:
                self._backend = _NSEventBackend(self)
                return
            except _BackendUnavailable as e:
                self._last_error = f"NSEvent 监听不可用,回退到 pynput: {e}"
                # fall through to pynput
        try:
            self._backend = _PynputBackend(self)
        except _BackendUnavailable as e:
            self._last_error = str(e)
            self._backend = None

    def _signal_for(self, action: str):
        return getattr(self, action, None) if action in ACTIONS else None


# ----------------------------------------------------------------------
# Backend protocol + shared helpers
# ----------------------------------------------------------------------

class _BackendUnavailable(RuntimeError):
    pass


class _Backend:
    def __init__(self, bridge: HotkeyBridge) -> None:
        self.bridge = bridge

    def start(self, bindings: dict[str, str]) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


# ----------------------------------------------------------------------
# macOS NSEvent backend
# ----------------------------------------------------------------------

class _NSEventBackend(_Backend):
    """macOS global key-down monitor via ``NSEvent``."""

    def __init__(self, bridge: HotkeyBridge) -> None:
        super().__init__(bridge)
        try:
            import AppKit  # type: ignore
        except Exception as e:
            raise _BackendUnavailable(str(e))
        self._AppKit = AppKit
        self._monitor = None        # global (other-app) monitor
        self._local_monitor = None  # local (own-app) monitor
        # (mods_int, char_lower_str_or_None, keycode_or_None, qt_signal)
        self._parsed: list[tuple[int, Optional[str], Optional[int], object]] = []
        self._handler_ref = None
        self._local_handler_ref = None

    _NSF1_UNICODE = 0xF704  # NSF1FunctionKey; NSF{n} = 0xF704 + (n-1)

    def _parse(self, s: str) -> Optional[tuple[int, Optional[str], Optional[int]]]:
        AppKit = self._AppKit
        M_CMD = int(AppKit.NSEventModifierFlagCommand)
        M_SHIFT = int(AppKit.NSEventModifierFlagShift)
        M_CTRL = int(AppKit.NSEventModifierFlagControl)
        M_ALT = int(AppKit.NSEventModifierFlagOption)
        mods = 0
        char: Optional[str] = None
        keycode: Optional[int] = None
        for p in (t for t in s.split("+") if t):
            if p == "<cmd>":
                mods |= M_CMD
            elif p == "<shift>":
                mods |= M_SHIFT
            elif p == "<ctrl>":
                mods |= M_CTRL
            elif p == "<alt>":
                mods |= M_ALT
            elif p == "<space>":
                char = " "
            elif p == "<enter>":
                char = "\r"
            elif p == "<tab>":
                char = "\t"
            elif p.startswith("<f") and p.endswith(">") and p[2:-1].isdigit():
                fn = int(p[2:-1])
                # F-keys appear as private-use-area codepoints in
                # charactersIgnoringModifiers.
                char = chr(self._NSF1_UNICODE + (fn - 1))
            elif len(p) == 1:
                char = p.lower()
            else:
                return None
        if char is None:
            return None
        return mods, char, keycode

    def _on_keydown(self, event) -> None:
        """Shared matching logic for both global and local monitors."""
        AppKit = self._AppKit
        mod_mask = int(AppKit.NSEventModifierFlagDeviceIndependentFlagsMask)
        evt_mods = int(event.modifierFlags()) & mod_mask
        # Narrow to the four meaningful modifier bits only.
        evt_mods &= (
            int(AppKit.NSEventModifierFlagCommand)
            | int(AppKit.NSEventModifierFlagShift)
            | int(AppKit.NSEventModifierFlagControl)
            | int(AppKit.NSEventModifierFlagOption)
        )
        chars = event.charactersIgnoringModifiers()
        ch = str(chars).lower() if chars is not None else ""
        for need_mods, need_char, _keycode, sig in self._parsed:
            if evt_mods == need_mods and need_char is not None and ch == need_char:
                sig.emit()
                return

    def start(self, bindings: dict[str, str]) -> bool:
        AppKit = self._AppKit
        self.stop()  # idempotent
        self._parsed = []
        for action, s in bindings.items():
            sig = self.bridge._signal_for(action)
            if sig is None or not s:
                continue
            parsed = self._parse(s)
            if parsed is None:
                continue
            mods, char, keycode = parsed
            self._parsed.append((mods, char, keycode, sig))

        if not self._parsed:
            self.bridge._last_error = "未配置任何快捷键。"
            return False

        self_ = self  # explicit capture to avoid late-binding issues

        def global_handler(event):
            try:
                self_._on_keydown(event)
            except Exception:
                pass  # never let a handler exception kill the run loop

        def local_handler(event):
            try:
                self_._on_keydown(event)
            except Exception:
                pass
            return event  # local monitors must return the event

        self._handler_ref = global_handler
        self._local_handler_ref = local_handler

        # Global monitor: fires for key events sent to OTHER applications.
        self._monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            int(AppKit.NSEventMaskKeyDown), global_handler
        )
        if self._monitor is None:
            self.bridge._last_error = (
                "NSEvent 全局监听被系统拒绝。请到『系统设置 → 隐私与安全性 → 输入监控』"
                "授权当前进程(Terminal / PyCharm / Python),然后重启应用。"
            )
            self._handler_ref = None
            self._local_handler_ref = None
            return False

        # Local monitor: fires for key events in THIS application's own windows.
        # Does not require Input Monitoring permission.
        self._local_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            int(AppKit.NSEventMaskKeyDown), local_handler
        )

        self.bridge._last_error = None
        return True

    def stop(self) -> None:
        if self._monitor is not None:
            try:
                self._AppKit.NSEvent.removeMonitor_(self._monitor)
            except Exception:
                pass
            self._monitor = None
        if self._local_monitor is not None:
            try:
                self._AppKit.NSEvent.removeMonitor_(self._local_monitor)
            except Exception:
                pass
            self._local_monitor = None
        self._handler_ref = None
        self._local_handler_ref = None


# ----------------------------------------------------------------------
# pynput fallback (non-macOS)
# ----------------------------------------------------------------------

class _PynputBackend(_Backend):
    def __init__(self, bridge: HotkeyBridge) -> None:
        super().__init__(bridge)
        try:
            from pynput import keyboard  # noqa: F401
        except Exception as e:
            raise _BackendUnavailable(f"未安装 pynput: {e}")
        self._listener = None

    def start(self, bindings: dict[str, str]) -> bool:
        from pynput import keyboard
        self.stop()
        mapping: dict[str, Callable[[], None]] = {}
        for action, hotkey in bindings.items():
            if not hotkey:
                continue
            sig = self.bridge._signal_for(action)
            if sig is None:
                continue
            mapping[hotkey] = (lambda s=sig: s.emit())
        if not mapping:
            self.bridge._last_error = "未配置任何快捷键。"
            return False
        try:
            self._listener = keyboard.GlobalHotKeys(mapping)
            self._listener.daemon = True
            self._listener.start()
        except Exception as e:
            self.bridge._last_error = str(e)
            self._listener = None
            return False
        self.bridge._last_error = None
        return True

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
