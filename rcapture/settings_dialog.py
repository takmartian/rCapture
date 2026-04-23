from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QKeySequenceEdit, QLabel, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from .config import ACTIONS, ACTION_LABELS, Config, default_hotkeys


# ------------------------------------------------------------
# QKeySequence ⇄ pynput hotkey-string conversion
# ------------------------------------------------------------
#
# Qt's QKeySequence "PortableText" format uses cross-platform tokens like
# "Ctrl+Shift+1". On macOS, Qt by default swaps the **physical** Ctrl key and
# the Cmd key so that QKeySequence's "Ctrl" actually means Cmd, and "Meta"
# means physical Ctrl. We follow that swap when converting to pynput's
# `<cmd>` / `<ctrl>` tokens; on Windows/Linux there's no swap.

def _qt_mod_to_pynput(token: str) -> str:
    if sys.platform == "darwin":
        if token == "Ctrl":
            return "<cmd>"
        if token == "Meta":
            return "<ctrl>"
    else:
        if token == "Ctrl":
            return "<ctrl>"
        if token == "Meta":
            return "<cmd>"
    if token == "Shift":
        return "<shift>"
    if token == "Alt":
        return "<alt>"
    return ""


def _pynput_to_qt_mod(tok: str) -> str:
    if sys.platform == "darwin":
        if tok == "<cmd>":
            return "Ctrl"
        if tok == "<ctrl>":
            return "Meta"
    else:
        if tok == "<cmd>":
            return "Meta"
        if tok == "<ctrl>":
            return "Ctrl"
    if tok == "<shift>":
        return "Shift"
    if tok == "<alt>":
        return "Alt"
    return ""


_KEY_NAMED = {
    "<space>":     "Space",
    "<enter>":     "Return",
    "<tab>":       "Tab",
    "<backspace>": "Backspace",
}
_KEY_NAMED_REV = {v.lower(): k for k, v in _KEY_NAMED.items()}


def qkeyseq_to_pynput(seq: QKeySequence) -> str:
    if seq.isEmpty():
        return ""
    # Use only the first chord and a portable spelling so we can split it.
    text = seq.toString(QKeySequence.PortableText).split(",")[0].strip()
    if not text:
        return ""
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        return ""
    *mods, key = parts
    out_tokens: list[str] = []
    for m in mods:
        t = _qt_mod_to_pynput(m)
        if t:
            out_tokens.append(t)
    # convert the trailing key
    k_lower = key.lower()
    if k_lower in _KEY_NAMED_REV:
        out_tokens.append(_KEY_NAMED_REV[k_lower])
    elif key.startswith("F") and key[1:].isdigit():
        out_tokens.append(f"<{key.lower()}>")
    elif len(key) == 1:
        out_tokens.append(key.lower())
    else:
        out_tokens.append(key.lower())
    return "+".join(out_tokens)


def pynput_to_qkeyseq(s: str) -> QKeySequence:
    if not s:
        return QKeySequence()
    parts = [p.strip() for p in s.split("+") if p.strip()]
    qt_parts: list[str] = []
    for p in parts:
        if p in _KEY_NAMED:
            qt_parts.append(_KEY_NAMED[p])
        elif p.startswith("<") and p.endswith(">"):
            mod = _pynput_to_qt_mod(p)
            if mod:
                qt_parts.append(mod)
            elif p[1] == "f" and p[2:-1].isdigit():
                qt_parts.append(f"F{p[2:-1]}")
        elif len(p) == 1:
            qt_parts.append(p.upper())
        else:
            qt_parts.append(p)
    if not qt_parts:
        return QKeySequence()
    return QKeySequence("+".join(qt_parts), QKeySequence.PortableText)


# ------------------------------------------------------------
# Hotkey edit widget — wraps QKeySequenceEdit
# ------------------------------------------------------------

class HotkeyEdit(QWidget):
    """Captures a single chord via Qt's built-in QKeySequenceEdit.

    Using QKeySequenceEdit (rather than ``grabKeyboard`` on a button) keeps
    keyboard input flowing through Qt's standard event pipeline. That avoids
    the macOS Input Method Kit warning ``IMKCFRunLoopWakeUpReliable`` and the
    instability that comes with hijacking AppKit's input tap inside an
    accessory-policy app.
    """

    changed = Signal(str)

    def __init__(self, current: str = "") -> None:
        super().__init__()
        self._edit = QKeySequenceEdit()
        try:
            # Qt 6 exposes setMaximumSequenceLength; cap at one chord.
            self._edit.setMaximumSequenceLength(1)
        except Exception:
            pass
        # Only accept focus on explicit click — prevents the first edit from
        # stealing focus when the settings dialog opens and swallowing keystrokes.
        self._edit.setFocusPolicy(Qt.ClickFocus)
        self._edit.setStyleSheet(
            "QKeySequenceEdit { padding: 2px; }"
            "QKeySequenceEdit:focus { border: 2px solid #2c7be5; border-radius: 3px; }"
        )
        if current:
            self._edit.setKeySequence(pynput_to_qkeyseq(current))

        self._clear = QPushButton("清除")
        self._clear.clicked.connect(self._do_clear)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._edit, 1)
        lay.addWidget(self._clear)

        self._edit.keySequenceChanged.connect(self._on_changed)

    def value(self) -> str:
        return qkeyseq_to_pynput(self._edit.keySequence())

    def set_value(self, s: str) -> None:
        self._edit.setKeySequence(pynput_to_qkeyseq(s) if s else QKeySequence())

    def cancel_recording(self) -> None:
        # Nothing to release — QKeySequenceEdit owns its capture state and
        # cleans itself up when the dialog closes.
        return

    def _do_clear(self) -> None:
        self._edit.clear()

    def _on_changed(self) -> None:
        self.changed.emit(self.value())


# ------------------------------------------------------------
# Dialog
# ------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.resize(520, 480)

        root = QVBoxLayout(self)

        # ---- general ----
        gen_group = QGroupBox("通用")
        gen_lay = QVBoxLayout(gen_group)
        self._start_minimized = QCheckBox("启动时不显示主窗口（仅保留菜单栏图标）")
        self._start_minimized.setChecked(cfg.start_minimized)
        gen_lay.addWidget(self._start_minimized)
        self._launch_at_login = QCheckBox("开机自动启动")
        self._launch_at_login.setChecked(cfg.launch_at_login)
        gen_lay.addWidget(self._launch_at_login)
        root.addWidget(gen_group)

        # ---- hotkeys ----
        hk_group = QGroupBox("快捷键")
        hk_lay = QVBoxLayout(hk_group)
        hk_lay.addWidget(QLabel(
            "在右侧输入框中按下快捷键。必须包含至少一个修饰键(⌘ / ⌃ / ⇧ / ⌥)。"
        ))
        form = QFormLayout()
        self._edits: dict[str, HotkeyEdit] = {}
        for action in ACTIONS:
            edit = HotkeyEdit(cfg.hotkeys.get(action, ""))
            self._edits[action] = edit
            form.addRow(ACTION_LABELS.get(action, action), edit)
        hk_lay.addLayout(form)
        root.addWidget(hk_group)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
            | QDialogButtonBox.RestoreDefaults
        )
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._restore_defaults)
        root.addWidget(btns)

        # Focus the OK button so no hotkey input field captures keystrokes on open.
        btns.button(QDialogButtonBox.Ok).setFocus()

    def _restore_defaults(self) -> None:
        d = default_hotkeys()
        for action, edit in self._edits.items():
            edit.set_value(d.get(action, ""))

    def _on_accept(self) -> None:
        for action, edit in self._edits.items():
            v = edit.value()
            if not v:
                continue
            tokens = v.split("+")
            if not any(t in ("<cmd>", "<ctrl>", "<shift>", "<alt>") for t in tokens):
                QMessageBox.warning(
                    self, "无效快捷键",
                    f"「{ACTION_LABELS[action]}」的快捷键 {v} 缺少修饰键。"
                )
                return
        seen: dict[str, str] = {}
        for action, edit in self._edits.items():
            v = edit.value()
            if not v:
                continue
            if v in seen:
                QMessageBox.warning(
                    self, "快捷键冲突",
                    f"「{ACTION_LABELS[seen[v]]}」和「{ACTION_LABELS[action]}」都绑定了 {v}。"
                )
                return
            seen[v] = action
        self.accept()

    def result_bindings(self) -> dict[str, str]:
        return {a: e.value() for a, e in self._edits.items()}

    def result_start_minimized(self) -> bool:
        return self._start_minimized.isChecked()

    def result_launch_at_login(self) -> bool:
        return self._launch_at_login.isChecked()
