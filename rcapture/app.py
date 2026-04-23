from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QTime, QSize, QPoint, QRect, Signal, QThread
from PySide6.QtGui import (
    QAction, QGuiApplication, QIcon, QPixmap, QPainter, QColor, QFont, QPainterPath, QPen,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QMessageBox, QCheckBox,
    QSpinBox, QGroupBox, QLineEdit, QStatusBar, QSystemTrayIcon, QMenu,
)

from .config import Config, ACTION_LABELS
from .screenshot import take_screenshot, grab_region_image, ScreenshotError
from .recorder import ScreenRecorder, RecorderError
from .region_selector import RegionSelector, RegionSelection
from .hotkeys import HotkeyBridge
from .settings_dialog import SettingsDialog
from .startup import set_launch_at_login


APP_NAME = "rCapture"
# Per-user key so multiple OS users on the same machine don't collide.
_INSTANCE_KEY = f"rCapture-{Path.home().name}"


def _make_app_icon() -> QIcon:
    pix = QPixmap(128, 128)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#2c7be5"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(8, 8, 112, 112, 22, 22)
    p.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(64)
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignCenter, "rC")
    p.end()
    return QIcon(pix)


def _make_tray_icon() -> QIcon:
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor("white"))
    p.setBrush(QColor("#2c7be5"))
    p.drawRoundedRect(2, 2, 28, 28, 6, 6)
    p.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(18)
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignCenter, "rC")
    p.end()
    return QIcon(pix)


def _humanize_hotkey(raw: str) -> str:
    """Display-only conversion, e.g. `<cmd>+<shift>+r` → `⌘⇧R`."""
    if not raw:
        return "(未设置)"
    tokens = [t for t in raw.split("+") if t]
    subs = {
        "<cmd>": "⌘", "<ctrl>": "⌃" if sys.platform == "darwin" else "Ctrl",
        "<shift>": "⇧", "<alt>": "⌥" if sys.platform == "darwin" else "Alt",
        "<enter>": "↵", "<space>": "Space", "<tab>": "⇥", "<backspace>": "⌫",
    }
    out = []
    for t in tokens:
        if t in subs:
            out.append(subs[t])
        elif t.startswith("<") and t.endswith(">"):
            out.append(t[1:-1].upper())
        else:
            out.append(t.upper())
    # no separator — matches macOS shortcut style ; use "+" on Win for clarity
    sep = "" if sys.platform == "darwin" else "+"
    return sep.join(out)


class _LongShotProgress(QWidget):
    """Small floating overlay shown during long-screenshot capture."""

    stop_requested = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 9, 14, 9)
        lay.setSpacing(12)

        self._label = QLabel("准备截长图…")
        self._label.setStyleSheet("color: white; font-size: 13px; background: transparent;")
        lay.addWidget(self._label)

        btn = QPushButton("停止")
        btn.setStyleSheet(
            "QPushButton{color:white;background:#c0392b;border:none;"
            "border-radius:4px;padding:3px 12px;font-size:12px;}"
            "QPushButton:hover{background:#e74c3c;}"
        )
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.stop_requested.emit)
        lay.addWidget(btn)

        self.adjustSize()
        self._reposition()

    def _reposition(self) -> None:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen:
            sg = screen.geometry()
            self.move(sg.center().x() - self.width() // 2, sg.top() + 56)

    def set_countdown(self, secs: int) -> None:
        self._label.setText(
            f"截长图将在 {secs} 秒后开始，请将鼠标焦点置于目标区域内"
        )
        self.adjustSize(); self._reposition()

    def set_capturing(self, n: int) -> None:
        self._label.setText(
            f"截长图进行中  ●  已截 {n} 帧     右键 / ESC / 停止 可终止"
        )
        self.adjustSize(); self._reposition()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Escape:
            self.stop_requested.emit()
        super().keyPressEvent(e)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 10, 10)
        p.fillPath(path, QColor(20, 20, 20, 215))


def _fix_pin_overlay_macos(widget: QWidget) -> None:
    """Set NSFloatingWindowLevel and disable hide-on-deactivate for the pinned overlay."""
    if sys.platform != "darwin":
        return
    try:
        from ctypes import c_void_p
        import objc  # type: ignore
        from AppKit import NSFloatingWindowLevel  # type: ignore
        view_ptr = int(widget.winId())
        if view_ptr == 0:
            return
        view = objc.objc_object(c_void_p=c_void_p(view_ptr))
        window = view.window()
        if window is None:
            return
        window.setLevel_(NSFloatingWindowLevel)
        try:
            window.setHidesOnDeactivate_(False)
        except Exception:
            pass
    except Exception:
        pass


def _setup_recording_overlay(widget: QWidget) -> None:
    """macOS: keep widget on top, visible when deactivated, and hidden from screen recording."""
    if sys.platform != "darwin":
        return
    try:
        from ctypes import c_void_p
        import objc  # type: ignore
        from AppKit import NSFloatingWindowLevel, NSWindowSharingNone  # type: ignore
        view_ptr = int(widget.winId())
        if view_ptr == 0:
            return
        view = objc.objc_object(c_void_p=c_void_p(view_ptr))
        window = view.window()
        if window is None:
            return
        window.setLevel_(NSFloatingWindowLevel)
        window.setHidesOnDeactivate_(False)
        window.setSharingType_(NSWindowSharingNone)
    except Exception:
        pass


class _RecordingStatusBar(QWidget):
    """Floating bar: 3-s countdown → elapsed time + stop button. Not captured by screen recording."""

    stop_requested = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 9, 14, 9)
        lay.setSpacing(10)

        self._dot = QLabel("●")
        self._dot.setStyleSheet("color:#ff4444;font-size:13px;background:transparent;")
        lay.addWidget(self._dot)

        self._label = QLabel("准备录屏…")
        self._label.setStyleSheet("color:white;font-size:13px;background:transparent;")
        lay.addWidget(self._label)

        btn = QPushButton("停止录屏")
        btn.setStyleSheet(
            "QPushButton{color:white;background:#c0392b;border:none;"
            "border-radius:4px;padding:3px 12px;font-size:12px;}"
            "QPushButton:hover{background:#e74c3c;}"
        )
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.stop_requested.emit)
        lay.addWidget(btn)

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(600)
        self._blink_timer.timeout.connect(self._blink)
        self._blink_on = True

        self.adjustSize()
        self._reposition()

    def _reposition(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen:
            sg = screen.geometry()
            self.move(sg.center().x() - self.width() // 2, sg.top() + 56)

    def set_countdown(self, secs: int) -> None:
        self._blink_timer.stop()
        self._dot.setVisible(False)
        self._label.setText(f"录屏将在 {secs} 秒后开始，请切换到目标窗口")
        self.adjustSize()
        self._reposition()

    def set_recording(self, elapsed_secs: int) -> None:
        self._dot.setVisible(True)
        if not self._blink_timer.isActive():
            self._blink_timer.start()
        h = elapsed_secs // 3600
        m = (elapsed_secs % 3600) // 60
        s = elapsed_secs % 60
        self._label.setText(f"录屏中   {h:02d}:{m:02d}:{s:02d}")
        self.adjustSize()
        self._reposition()

    def _blink(self) -> None:
        self._blink_on = not self._blink_on
        self._dot.setVisible(self._blink_on)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        QTimer.singleShot(0, lambda: _setup_recording_overlay(self))

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 10, 10)
        p.fillPath(path, QColor(20, 20, 20, 215))


class _RecordingCornerOverlay(QWidget):
    """Blinking quarter-circle arcs at the corners of the recording region.
    Transparent to mouse events and excluded from screen recording."""

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._rw = w
        self._rh = h
        self._bright = True
        self.setGeometry(x, y, w, h)

        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(700)
        self._blink_timer.timeout.connect(self._blink)

    def start(self) -> None:
        self.show()
        self._blink_timer.start()
        QTimer.singleShot(0, lambda: _setup_recording_overlay(self))

    def stop(self) -> None:
        self._blink_timer.stop()
        self.hide()
        self.deleteLater()

    def _blink(self) -> None:
        self._bright = not self._bright
        self.update()

    def paintEvent(self, _e) -> None:
        from PySide6.QtCore import QRectF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        alpha = 230 if self._bright else 80
        pen = QPen(QColor(255, 60, 60, alpha), 3, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        w, h = self._rw, self._rh
        r = min(22, w // 4, h // 4)
        # Each arc is a quarter-circle bounding rect positioned at the corner,
        # extending inward. The arc spans the quadrant that faces inside the region.
        p.drawArc(QRectF(0,       0,       2*r, 2*r), 90*16,  90*16)   # top-left
        p.drawArc(QRectF(w-2*r,  0,       2*r, 2*r), 0*16,   90*16)   # top-right
        p.drawArc(QRectF(0,       h-2*r,  2*r, 2*r), 180*16, 90*16)   # bottom-left
        p.drawArc(QRectF(w-2*r,  h-2*r,  2*r, 2*r), 270*16, 90*16)   # bottom-right


class PinnedOverlay(QWidget):
    """Floating screenshot pinned always on top. Drag to move, right-click or Escape to close."""

    def __init__(self, pixmap: QPixmap, logical_w: int, logical_h: int) -> None:
        # Qt.Tool hides on app deactivate on macOS — omit it so the overlay
        # stays visible when the user switches to another application.
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._pixmap = pixmap
        self._base_w = logical_w
        self._base_h = logical_h
        self._scale = 1.0
        self._w = logical_w
        self._h = logical_h
        self._drag_start: Optional[QPoint] = None
        self._hovered = False
        self.setMouseTracking(True)
        self.resize(self._w, self._h)
        self.setContextMenuPolicy(Qt.PreventContextMenu)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        _fix_pin_overlay_macos(self)

    def _close_btn_center(self) -> tuple[int, int]:
        return self._w - 16, 16

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(QRect(0, 0, self._w, self._h), self._pixmap)
        if self._hovered:
            cx, cy = self._close_btn_center()
            r = 12
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(30, 30, 30, 210))
            p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
            m = 5
            p.setPen(QPen(QColor("white"), 2.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(cx - m, cy - m, cx + m, cy + m)
            p.drawLine(cx + m, cy - m, cx - m, cy + m)

    def _over_close_btn(self, pos: QPoint) -> bool:
        cx, cy = self._close_btn_center()
        return (pos.x() - cx) ** 2 + (pos.y() - cy) ** 2 <= 14 ** 2

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            if self._hovered and self._over_close_btn(e.position().toPoint()):
                self.close()
                return
            self._drag_start = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif e.button() == Qt.RightButton:
            self.close()
        e.accept()

    def mouseMoveEvent(self, e) -> None:
        pos = e.position().toPoint()
        if self._drag_start is not None and (e.buttons() & Qt.LeftButton):
            if not self._over_close_btn(pos):
                self.move(e.globalPosition().toPoint() - self._drag_start)
        e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._drag_start = None
        e.accept()

    def enterEvent(self, e) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, e) -> None:
        self._hovered = False
        self.update()

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.close()
        e.accept()

    def wheelEvent(self, e) -> None:
        delta = e.angleDelta().y()
        factor = 1.12 if delta > 0 else 1.0 / 1.12
        new_scale = max(0.2, min(5.0, self._scale * factor))
        if abs(new_scale - self._scale) < 0.001:
            e.ignore()
            return
        cursor_pos = e.position().toPoint()
        old_w, old_h = self._w, self._h
        self._scale = new_scale
        self._w = max(20, int(self._base_w * self._scale))
        self._h = max(20, int(self._base_h * self._scale))
        # Keep the point under the cursor stationary during zoom
        new_wx = self.x() - int(cursor_pos.x() * (self._w - old_w) / old_w)
        new_wy = self.y() - int(cursor_pos.y() * (self._h - old_h) / old_h)
        self.resize(self._w, self._h)
        self.move(new_wx, new_wy)
        self.update()
        e.accept()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Escape:
            self.close()


class _OcrWorker(QThread):
    finished = Signal(str)
    failed   = Signal(str)

    def __init__(self, img, parent=None):
        super().__init__(parent)
        self._img = img
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from .ocr import ocr_image
        try:
            text = ocr_image(self._img)
        except Exception as e:
            if not self._cancelled:
                self.failed.emit(str(e))
            return
        if not self._cancelled:
            self.finished.emit(text or "")


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.recorder: Optional[ScreenRecorder] = None
        self._user_requested_quit = False
        self._pending_region_action: Optional[str] = None  # "record" | "screenshot"

        self.rec_timer = QTimer(self)
        self.rec_timer.setInterval(1000)
        self.rec_timer.timeout.connect(self._tick_recording)
        self.rec_elapsed = QTime(0, 0, 0)

        self.region_selector: Optional[RegionSelector] = None  # created lazily per-capture
        self._ocr_worker: Optional[_OcrWorker] = None

        self.setWindowTitle(f"{APP_NAME} — 截图 & 录屏")
        self.setWindowIcon(_make_app_icon())
        self.setMinimumSize(QSize(520, 460))
        self.tray: Optional[QSystemTrayIcon] = None
        self._build_ui()
        self._refresh_save_dir_field()
        self._build_tray()

        self._long_shot_thread = None
        self._long_shot_overlay: Optional[_LongShotProgress] = None
        self._countdown_timer: Optional[QTimer] = None
        self._countdown_secs: int = 0
        self._long_shot_region: Optional[tuple] = None
        self._pinned_overlays: list[PinnedOverlay] = []

        self._recording_status_bar: Optional[_RecordingStatusBar] = None
        self._recording_corner_overlay: Optional[_RecordingCornerOverlay] = None
        self._pre_record_timer: Optional[QTimer] = None
        self._pre_record_countdown_secs: int = 0
        self._pre_record_crop: Optional[tuple] = None
        self._pre_record_region_sel: Optional[RegionSelection] = None

        self.hotkeys = HotkeyBridge()
        self.hotkeys.full_screenshot.connect(lambda: self._take("full"))
        self.hotkeys.region_screenshot.connect(lambda: self._take("region"))
        self.hotkeys.long_screenshot.connect(self._launch_long_screenshot)
        self.hotkeys.toggle_full_record.connect(self._toggle_record)
        self.hotkeys.toggle_region_record.connect(self._toggle_region_record)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)
        btn_settings = QPushButton("设置…")
        btn_settings.clicked.connect(self._open_settings)
        header.addWidget(btn_settings)
        root.addLayout(header)

        # ------ Screenshot group ------
        shot_box = QGroupBox("截图")
        shot_layout = QHBoxLayout(shot_box)
        self.btn_full = QPushButton("全屏截图")
        self.btn_region = QPushButton("区域截图")
        self.btn_full.clicked.connect(lambda: self._take("full"))
        self.btn_region.clicked.connect(lambda: self._take("region"))
        for b in (self.btn_full, self.btn_region):
            shot_layout.addWidget(b)
        root.addWidget(shot_box)

        # ------ Record group ------
        rec_box = QGroupBox("录屏")
        rec_layout = QVBoxLayout(rec_box)
        btn_row = QHBoxLayout()
        self.btn_record = QPushButton("开始录屏")
        self.btn_record.clicked.connect(self._toggle_record)
        self.btn_region_record = QPushButton("区域录屏")
        self.btn_region_record.clicked.connect(self._toggle_region_record)
        self.lbl_elapsed = QLabel("00:00:00")
        self.lbl_elapsed.setStyleSheet(
            "font-family: Menlo, Consolas, 'Courier New', monospace; font-size: 16px;"
        )
        btn_row.addWidget(self.btn_record)
        btn_row.addWidget(self.btn_region_record)
        btn_row.addStretch(1)
        btn_row.addWidget(self.lbl_elapsed)
        rec_layout.addLayout(btn_row)

        opts_row = QHBoxLayout()
        self.chk_cursor = QCheckBox("包含鼠标")
        self.chk_cursor.setChecked(self.cfg.capture_cursor)
        self.chk_audio = QCheckBox("录制系统麦克风")
        self.chk_audio.setChecked(self.cfg.record_audio)
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(5, 60)
        self.spin_fps.setValue(self.cfg.record_fps)
        self.spin_fps.setSuffix(" fps")
        opts_row.addWidget(self.chk_cursor)
        opts_row.addWidget(self.chk_audio)
        opts_row.addStretch(1)
        opts_row.addWidget(QLabel("帧率:"))
        opts_row.addWidget(self.spin_fps)
        rec_layout.addLayout(opts_row)
        root.addWidget(rec_box)

        # ------ Save dir group ------
        dir_box = QGroupBox("保存位置")
        dir_layout = QHBoxLayout(dir_box)
        self.ed_dir = QLineEdit()
        self.ed_dir.setReadOnly(True)
        btn_pick = QPushButton("更改…")
        btn_pick.clicked.connect(self._choose_dir)
        btn_open = QPushButton("打开")
        btn_open.clicked.connect(self._open_dir)
        dir_layout.addWidget(self.ed_dir, 1)
        dir_layout.addWidget(btn_pick)
        dir_layout.addWidget(btn_open)
        root.addWidget(dir_box)

        # ------ Hotkey hint ------
        self.lbl_hotkeys = QLabel("")
        self.lbl_hotkeys.setStyleSheet("color: #6b7280;")
        self.lbl_hotkeys.setWordWrap(True)
        root.addWidget(self.lbl_hotkeys)
        self._refresh_hotkey_hint()

        root.addStretch(1)
        self.setStatusBar(QStatusBar())
        self._status("就绪")

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(_make_tray_icon(), self)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu()
        act_prefs = QAction("偏好设置", self)
        act_prefs.triggered.connect(self._show_and_raise)
        menu.addAction(act_prefs)
        menu.addSeparator()

        act_full = QAction("全屏截图", self)
        act_full.triggered.connect(lambda: self._take("full"))
        menu.addAction(act_full)
        act_region = QAction("区域截图", self)
        act_region.triggered.connect(lambda: self._take("region"))
        menu.addAction(act_region)
        act_long = QAction("截长图", self)
        act_long.triggered.connect(self._launch_long_screenshot)
        menu.addAction(act_long)
        menu.addSeparator()

        self.act_tray_record = QAction("开始录屏", self)
        self.act_tray_record.triggered.connect(self._toggle_record)
        menu.addAction(self.act_tray_record)
        self.act_tray_region_record = QAction("区域录屏", self)
        self.act_tray_region_record.triggered.connect(self._toggle_region_record)
        menu.addAction(self.act_tray_region_record)
        menu.addSeparator()

        act_dir = QAction("打开保存目录", self)
        act_dir.triggered.connect(self._open_dir)
        menu.addAction(act_dir)
        menu.addSeparator()

        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._request_quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        # Left-click intentionally does NOT pop up the main window — on macOS
        # the menu bar icon always shows the context menu on any click.
        self.tray.show()

    def _show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _request_quit(self) -> None:
        self._user_requested_quit = True
        # Because we set setQuitOnLastWindowClosed(False) for the tray workflow,
        # closing the main window alone will not quit the app. Do a full teardown
        # and call QApplication.quit() explicitly.
        if self.recorder and self.recorder.is_recording:
            try:
                self.recorder.stop()
            except Exception:
                pass
            self.recorder = None
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        try:
            self._persist_ui_state()
        except Exception:
            pass
        if self.tray is not None:
            self.tray.hide()
            self.tray = None
        srv = getattr(self, "_instance_server", None)
        if srv is not None:
            srv.close()
            QLocalServer.removeServer(_INSTANCE_KEY)
        self.hide()
        QApplication.quit()

    # ---------- helpers ----------
    def _status(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 6000)
        if self.tray is not None:
            self.tray.setToolTip(f"{APP_NAME} — {msg}")

    def _notify(self, title: str, msg: str) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, msg, _make_tray_icon(), 3000)

    def _refresh_save_dir_field(self) -> None:
        self.ed_dir.setText(self.cfg.save_dir)

    def _refresh_hotkey_hint(self) -> None:
        parts: list[str] = []
        for action in ("full_screenshot", "region_screenshot",
                       "toggle_full_record", "toggle_region_record"):
            hk = self.cfg.hotkeys.get(action, "")
            parts.append(f"{_humanize_hotkey(hk)} {ACTION_LABELS[action]}")
        self.lbl_hotkeys.setText("快捷键:" + "   ·   ".join(parts))

    def _choose_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.cfg.save_dir)
        if d:
            self.cfg.save_dir = d
            self.cfg.save()
            self._refresh_save_dir_field()

    def _open_dir(self) -> None:
        p = self.cfg.ensure_save_dir()
        if sys.platform == "darwin":
            subprocess.run(["open", str(p)])
        elif sys.platform == "win32":
            subprocess.run(["explorer", str(p)])
        else:
            subprocess.run(["xdg-open", str(p)])

    def _persist_ui_state(self) -> None:
        self.cfg.capture_cursor = self.chk_cursor.isChecked()
        self.cfg.record_audio = self.chk_audio.isChecked()
        self.cfg.record_fps = self.spin_fps.value()
        self.cfg.save()

    # ---------- settings dialog ----------
    def _open_settings(self) -> None:
        # Stop the pynput listener BEFORE the dialog opens. While the dialog
        # is up, the user is editing key combinations — we don't want the
        # current bindings to fire on top of QKeySequenceEdit, and we want
        # the macOS Quartz event tap fully torn down so the post-dialog
        # restart starts from a clean state.
        try:
            self.hotkeys.stop()
        except Exception:
            pass

        dlg = SettingsDialog(self.cfg, parent=self)
        accepted = bool(dlg.exec())

        if accepted:
            new_bindings = dlg.result_bindings()
            old_launch = self.cfg.launch_at_login
            self.cfg.hotkeys = new_bindings
            self.cfg.start_minimized = dlg.result_start_minimized()
            self.cfg.launch_at_login = dlg.result_launch_at_login()
            self.cfg.save()
            if self.cfg.launch_at_login != old_launch:
                try:
                    set_launch_at_login(self.cfg.launch_at_login)
                except Exception as exc:
                    QMessageBox.warning(self, "开机启动", f"设置开机启动失败:\n{exc}")
            self._refresh_hotkey_hint()
            self._status("设置已更新")
        else:
            new_bindings = self.cfg.hotkeys

        # Restart the listener with a small delay so AppKit can finish
        # dispatching the dialog-close events before pynput installs its
        # new event tap.
        QTimer.singleShot(250, lambda: self._apply_hotkeys(new_bindings))

    def _apply_hotkeys(self, bindings: dict[str, str]) -> None:
        ok = self.hotkeys.start(bindings)
        if not ok and self.hotkeys.last_error:
            QMessageBox.warning(
                self, "全局快捷键",
                f"全局快捷键未启用:\n{self.hotkeys.last_error}\n\n"
                f"在 macOS 上可能需要在『系统设置 → 输入监控』中授权。"
            )

    # ---------- screenshot ----------
    def _take(self, mode: str) -> None:
        if self.recorder and self.recorder.is_recording:
            self._status("录屏中,已忽略截图请求")
            return
        self._persist_ui_state()
        save_dir = self.cfg.ensure_save_dir()

        if mode == "full":
            # hide main window so it doesn't appear in the capture
            if self.isVisible():
                self.hide()
                QTimer.singleShot(250, lambda: self._do_full_screenshot(save_dir, reshow=True))
            else:
                self._do_full_screenshot(save_dir, reshow=False)
            return

        if mode == "region":
            self._pending_region_action = "screenshot"
            self._launch_region_selector()
            return

    def _launch_region_selector(self) -> None:
        """Recreate the region selector on every invocation.

        Reusing a single RegionSelector across hide/show cycles turned out to
        leak macOS Tool-window state (mouse grab, event routing), which on
        the second open caused mouseMove to stop firing and the rect to stay
        stuck at 1×1. Allocating a fresh widget each time is cheap and
        sidesteps all that.
        """
        old = self.region_selector
        if old is not None:
            try:
                old.selected.disconnect(self._on_region_picked)
            except (TypeError, RuntimeError):
                pass
            try:
                old.cancelled.disconnect(self._on_region_cancelled)
            except (TypeError, RuntimeError):
                pass
            try:
                old.colour_picked.disconnect(self._on_colour_picked)
            except (TypeError, RuntimeError):
                pass
            try:
                old.ocr_cancelled.disconnect(self._on_ocr_cancelled)
            except (TypeError, RuntimeError):
                pass
            old.hide()
            old.deleteLater()

        self.region_selector = RegionSelector()
        self.region_selector.selected.connect(self._on_region_picked)
        self.region_selector.cancelled.connect(self._on_region_cancelled)
        self.region_selector.colour_picked.connect(self._on_colour_picked)
        self.region_selector.ocr_cancelled.connect(self._on_ocr_cancelled)

        kwargs = dict(
            initial_corner_radius=self.cfg.last_corner_radius_pt,
            initial_shadow_size=self.cfg.last_shadow_size_pt,
        )
        if self._pending_region_action in ("long_screenshot", "record"):
            kwargs["auto_confirm"] = True
        if self.isVisible():
            self.hide()
            QTimer.singleShot(150, lambda: self.region_selector.pick(**kwargs))
        else:
            self.region_selector.pick(**kwargs)

    def _do_full_screenshot(self, save_dir: Path, reshow: bool) -> None:
        try:
            path = take_screenshot(save_dir, mode="full", monitor_index=self.cfg.monitor_index)
        except ScreenshotError as e:
            if reshow:
                self.show()
            QMessageBox.critical(self, "截图失败", str(e))
            return
        except Exception:
            if reshow:
                self.show()
            QMessageBox.critical(self, "截图失败", traceback.format_exc())
            return
        if reshow:
            self.show()
        self._status(f"已保存:{path.name}")
        self._notify("截图完成", path.name)

    def _do_region_screenshot(self, sel: RegionSelection) -> None:
        """Dispatch a region selection to the clipboard or file-save flow."""
        # Give macOS one frame to drop the overlay before we grab pixels, so
        # neither the translucent mask nor the annotation preview shows up in
        # the captured image.
        action = "save" if sel.action == "save" else "clipboard"
        QTimer.singleShot(
            200,
            lambda: (
                self._region_save_to_file(sel)
                if action == "save"
                else self._region_copy_to_clipboard(sel)
            ),
        )

    def _grab_region_pil(self, sel: RegionSelection):
        x, y, w, h = sel.to_mss_region()
        # Scale annotation coords/sizes for physical pixels (Quartz captures at DPR×).
        screen = QGuiApplication.primaryScreen()
        dpr = int(screen.devicePixelRatio()) if screen else 1
        pw, ph = w * dpr, h * dpr
        annotations = sel.annotations_in_image_pixels(pw, ph) if sel.annotations else None
        radius = sel.corner_radius_in_image_pixels(pw, ph) if sel.corner_radius_pt else 0
        shadow = sel.shadow_size_in_image_pixels(pw, ph) if sel.shadow_size_pt else 0
        return grab_region_image(
            (x, y, w, h),
            annotations=annotations,
            corner_radius=radius,
            shadow_size=shadow,
        )

    def _region_copy_to_clipboard(self, sel: RegionSelection) -> None:
        try:
            img = self._grab_region_pil(sel)
        except ScreenshotError as e:
            QMessageBox.critical(self, "截图失败", str(e)); return
        except Exception:
            QMessageBox.critical(self, "截图失败", traceback.format_exc()); return
        import io
        from PySide6.QtGui import QImage
        buf = io.BytesIO()
        img.save(buf, "PNG")
        qimg = QImage()
        qimg.loadFromData(buf.getvalue(), "PNG")
        # Inform Qt of the real DPR so the clipboard image pastes at the correct
        # logical size in Retina-aware apps (e.g. Keynote, Pages, Preview).
        dpr = img.width / max(1, sel.w_pt)
        if dpr > 1.0:
            qimg.setDevicePixelRatio(dpr)
        QApplication.clipboard().setImage(qimg)
        self._status("截图已复制到剪贴板")
        self._notify("截图完成", "已复制到剪贴板")

    def _region_save_to_file(self, sel: RegionSelection) -> None:
        from datetime import datetime
        default_name = f"rCapture_{datetime.now():%Y%m%d_%H%M%S}.png"
        default_path = str(self.cfg.ensure_save_dir() / default_name)
        path_str, _ = QFileDialog.getSaveFileName(
            self, "保存截图", default_path,
            "PNG 图片 (*.png);;所有文件 (*)",
        )
        if not path_str:
            self._status("已取消保存")
            return
        try:
            img = self._grab_region_pil(sel)
        except ScreenshotError as e:
            QMessageBox.critical(self, "截图失败", str(e)); return
        except Exception:
            QMessageBox.critical(self, "截图失败", traceback.format_exc()); return
        out = Path(path_str)
        if out.suffix.lower() != ".png":
            out = out.with_suffix(".png")
        try:
            img.save(str(out), "PNG")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e)); return
        # Remember the chosen directory as the next default.
        self.cfg.save_dir = str(out.parent)
        self.cfg.save()
        self._refresh_save_dir_field()
        self._status(f"已保存:{out.name}")
        self._notify("截图已保存", out.name)

    def _do_region_ocr(self, sel: RegionSelection) -> None:
        from .ocr import ocr_image
        x, y, w, h = sel.to_mss_region()
        try:
            # Raw capture — no corner radius, shadow, or annotations so the
            # OCR engine sees clean pixel data.
            img = grab_region_image((x, y, w, h))
        except ScreenshotError as e:
            QMessageBox.critical(self, "截图失败", str(e)); return
        except Exception:
            QMessageBox.critical(self, "截图失败", traceback.format_exc()); return
        self._status("正在识别文字…")
        try:
            text = ocr_image(img)
        except RuntimeError as e:
            QMessageBox.critical(self, "OCR 失败", str(e)); return
        except Exception:
            QMessageBox.critical(self, "OCR 失败", traceback.format_exc()); return
        if not text.strip():
            self._status("OCR：未识别到文字")
            self._notify("OCR 结果", "未识别到文字")
            return
        QApplication.clipboard().setText(text)
        preview = text[:60].replace("\n", " ") + ("…" if len(text) > 60 else "")
        self._status(f"OCR 已复制：{preview}")
        self._notify("OCR 识别完成", preview)

    def _start_ocr_worker(self, sel: RegionSelection) -> None:
        """Start OCR in a background thread using the frozen screenshot."""
        rs = self.region_selector
        if rs is not None and rs._screen_pil is not None:
            dpr = rs._screen_pil_dpr
            img = rs._screen_pil.crop((
                int(sel.x_pt * dpr), int(sel.y_pt * dpr),
                int((sel.x_pt + sel.w_pt) * dpr), int((sel.y_pt + sel.h_pt) * dpr),
            ))
        else:
            # Fallback: grab from live screen (selector already hidden in this path)
            try:
                x, y, w, h = sel.to_mss_region()
                img = grab_region_image((x, y, w, h))
            except Exception as e:
                QMessageBox.critical(self, "截图失败", str(e))
                if rs:
                    rs.finish_ocr()
                return
        worker = _OcrWorker(img, self)
        worker.finished.connect(self._on_ocr_finished)
        worker.failed.connect(self._on_ocr_failed)
        self._ocr_worker = worker
        worker.start()

    def _on_ocr_finished(self, text: str) -> None:
        self._ocr_worker = None
        if self.region_selector:
            self.region_selector.finish_ocr()
        if not text.strip():
            self._status("OCR：未识别到文字")
            self._notify("OCR 结果", "未识别到文字")
            return
        QApplication.clipboard().setText(text)
        preview = text[:60].replace("\n", " ") + ("…" if len(text) > 60 else "")
        self._status(f"OCR 已复制：{preview}")
        self._notify("OCR 识别完成", preview)

    def _on_ocr_failed(self, err: str) -> None:
        self._ocr_worker = None
        if self.region_selector:
            self.region_selector.finish_ocr()
        QMessageBox.critical(self, "OCR 失败", err)

    def _on_ocr_cancelled(self) -> None:
        if self._ocr_worker is not None:
            self._ocr_worker.cancel()
            self._ocr_worker = None

    def _pin_region_screenshot(self, sel: RegionSelection) -> None:
        x, y, w, h = sel.to_mss_region()
        screen = QGuiApplication.primaryScreen()
        dpr = int(screen.devicePixelRatio()) if screen else 1
        pw, ph = w * dpr, h * dpr
        annotations = sel.annotations_in_image_pixels(pw, ph) if sel.annotations else None
        radius = sel.corner_radius_in_image_pixels(pw, ph) if sel.corner_radius_pt else 0
        try:
            img = grab_region_image((x, y, w, h), annotations=annotations,
                                    corner_radius=radius, shadow_size=0)
        except ScreenshotError as e:
            QMessageBox.critical(self, "截图失败", str(e)); return
        except Exception:
            QMessageBox.critical(self, "截图失败", traceback.format_exc()); return
        import io
        from PySide6.QtGui import QImage
        buf = io.BytesIO()
        img.save(buf, "PNG")
        qimg = QImage()
        qimg.loadFromData(buf.getvalue(), "PNG")
        pix = QPixmap.fromImage(qimg)
        # Derive capture DPR from actual image dimensions so the overlay renders
        # pixel-perfect regardless of whether Quartz or mss did the capture.
        capture_dpr = img.width / max(1, sel.w_pt)
        pix.setDevicePixelRatio(capture_dpr)
        overlay = PinnedOverlay(pix, sel.w_pt, sel.h_pt)
        screen = QGuiApplication.primaryScreen()
        if screen:
            sg = screen.geometry()
            overlay.move(sg.x() + sel.x_pt, sg.y() + sel.y_pt)
        overlay.show()
        overlay.raise_()
        self._pinned_overlays.append(overlay)
        def _remove(dead=overlay):
            try:
                self._pinned_overlays.remove(dead)
            except ValueError:
                pass
        overlay.destroyed.connect(_remove)
        self._status("截图已 Pin 到屏幕，右键或 ESC 关闭")

    # ---------- recording ----------
    def _toggle_record(self) -> None:
        if self.recorder and self.recorder.is_recording:
            self._stop_record()
        elif self._pre_record_timer is not None:
            self._cancel_pre_record()
        else:
            self._pre_record_start(crop=None, region_sel=None)

    def _toggle_region_record(self) -> None:
        if self.recorder and self.recorder.is_recording:
            self._stop_record()
            return
        if self._pre_record_timer is not None:
            self._cancel_pre_record()
            return
        self._pending_region_action = "record"
        self._launch_region_selector()

    def _on_region_picked(self, sel: RegionSelection) -> None:
        action = self._pending_region_action
        self._pending_region_action = None
        if action == "record":
            screen = QGuiApplication.primaryScreen()
            dpr = screen.devicePixelRatio() if screen else 1.0
            self._pre_record_start(crop=sel.to_ffmpeg_crop(dpr), region_sel=sel)
        elif action == "long_screenshot":
            QTimer.singleShot(200, lambda: self._start_long_screenshot(sel))
        elif action == "screenshot":
            self.cfg.last_corner_radius_pt = int(sel.corner_radius_pt)
            self.cfg.last_shadow_size_pt = int(sel.shadow_size_pt)
            self.cfg.save()
            if sel.action == "ocr":
                self._start_ocr_worker(sel)
            elif sel.action == "pin":
                QTimer.singleShot(200, lambda: self._pin_region_screenshot(sel))
            else:
                self._do_region_screenshot(sel)

    def _launch_long_screenshot(self) -> None:
        if self._long_shot_thread is not None and self._long_shot_thread.isRunning():
            return  # already in progress
        self._pending_region_action = "long_screenshot"
        self._launch_region_selector()

    def _start_long_screenshot(self, sel: RegionSelection) -> None:
        self._long_shot_region = sel.to_mss_region()
        x, y, w, h = self._long_shot_region
        if w < 10 or h < 10:
            return
        overlay = _LongShotProgress()
        overlay.stop_requested.connect(self._cancel_long_screenshot)
        self._long_shot_overlay = overlay
        self._countdown_secs = 3
        overlay.set_countdown(3)
        overlay.show()
        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._countdown_tick)
        self._countdown_timer.start(1000)

    def _countdown_tick(self) -> None:
        self._countdown_secs -= 1
        if self._countdown_secs > 0:
            if self._long_shot_overlay:
                self._long_shot_overlay.set_countdown(self._countdown_secs)
        else:
            self._countdown_timer.stop()
            self._countdown_timer = None
            self._begin_long_screenshot_capture()

    def _begin_long_screenshot_capture(self) -> None:
        from .long_screenshot import LongScreenshotThread
        if self._long_shot_overlay:
            self._long_shot_overlay.set_capturing(0)
        _x, _y, _w, region_h = self._long_shot_region
        scroll_clicks = max(3, region_h // 120)
        thread = LongScreenshotThread(self._long_shot_region, scroll_clicks=scroll_clicks)
        thread.frame_captured.connect(self._on_long_shot_frame)
        thread.finished.connect(self._on_long_shot_finished)
        thread.failed.connect(self._on_long_shot_failed)
        thread.cancelled.connect(self._on_long_shot_cancelled)
        self._long_shot_thread = thread
        thread.start()

    def _cancel_long_screenshot(self) -> None:
        if self._countdown_timer is not None:
            self._countdown_timer.stop()
            self._countdown_timer = None
            self._close_long_shot_overlay()
            self._status("截长图已取消")
            return
        if self._long_shot_thread is not None:
            self._long_shot_thread.stop()

    def _on_long_shot_frame(self, n: int) -> None:
        if self._long_shot_overlay:
            self._long_shot_overlay.set_capturing(n)

    def _on_long_shot_finished(self, img) -> None:
        self._close_long_shot_overlay()
        self._long_shot_thread = None
        try:
            import io
            from PySide6.QtGui import QImage
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qimg = QImage.fromData(buf.getvalue())
            QApplication.clipboard().setImage(qimg)
        except Exception as e:
            QMessageBox.critical(self, "截长图失败", str(e))
            return
        self._status("长截图已复制到剪贴板")
        self._notify("截长图完成", "已复制到剪贴板")

    def _on_long_shot_failed(self, msg: str) -> None:
        self._close_long_shot_overlay()
        self._long_shot_thread = None
        QMessageBox.critical(self, "截长图失败", msg)

    def _on_long_shot_cancelled(self) -> None:
        self._close_long_shot_overlay()
        self._long_shot_thread = None
        self._status("截长图已取消")

    def _close_long_shot_overlay(self) -> None:
        if self._long_shot_overlay is not None:
            self._long_shot_overlay.hide()
            self._long_shot_overlay.deleteLater()
            self._long_shot_overlay = None

    def _on_region_cancelled(self) -> None:
        self._pending_region_action = None
        self._status("区域选择已取消")
        if self.tray is None or not self.tray.isVisible():
            self._show_and_raise()

    def _on_colour_picked(self, text: str) -> None:
        self._pending_region_action = None
        self._status(f"已复制色值：{text}")
        if self.tray is None or not self.tray.isVisible():
            self._show_and_raise()

    def _start_record(self, crop: Optional[tuple[int, int, int, int]]) -> None:
        self._persist_ui_state()
        save_dir = self.cfg.ensure_save_dir()
        audio_idx = self.cfg.avfoundation_audio_index if self.chk_audio.isChecked() else None
        self.recorder = ScreenRecorder(
            save_dir=save_dir,
            screen_index=self.cfg.avfoundation_screen_index,
            audio_index=audio_idx,
            fps=self.spin_fps.value(),
            capture_cursor=self.chk_cursor.isChecked(),
        )
        try:
            out = self.recorder.start(crop=crop)
        except RecorderError as e:
            QMessageBox.critical(self, "录屏无法启动", str(e))
            self.recorder = None
            self._cleanup_recording_overlays()
            return
        except Exception:
            QMessageBox.critical(self, "录屏无法启动", traceback.format_exc())
            self.recorder = None
            self._cleanup_recording_overlays()
            return

        self.rec_elapsed = QTime(0, 0, 0)
        self.lbl_elapsed.setText(self.rec_elapsed.toString("HH:mm:ss"))
        self.rec_timer.start()
        self._set_recording_ui(True)
        if self._recording_status_bar is not None:
            self._recording_status_bar.set_recording(0)
        mode_text = "区域录屏" if crop is not None else "录屏"
        self._status(f"正在{mode_text}:{out.name}")
        self._notify(f"{mode_text}已开始", out.name)

    def _stop_record(self) -> None:
        if self._pre_record_timer is not None:
            self._cancel_pre_record()
            return
        if not self.recorder:
            return
        self.rec_timer.stop()
        try:
            out = self.recorder.stop()
        except RecorderError as e:
            QMessageBox.critical(self, "录屏结束时出错", str(e))
            out = None
        except Exception:
            QMessageBox.critical(self, "录屏结束时出错", traceback.format_exc())
            out = None
        self.recorder = None
        self._set_recording_ui(False)
        self._cleanup_recording_overlays()
        if out is not None:
            self._status(f"已保存录屏:{out.name}")
            self._notify("录屏已保存", out.name)
        else:
            self._status("录屏已停止")

    def _set_recording_ui(self, recording: bool) -> None:
        self.btn_record.setText("停止录屏" if recording else "开始录屏")
        self.btn_region_record.setText("停止录屏" if recording else "区域录屏")
        for b in (self.btn_full, self.btn_region):
            b.setEnabled(not recording)
        self.chk_audio.setEnabled(not recording)
        self.chk_cursor.setEnabled(not recording)
        self.spin_fps.setEnabled(not recording)

        if hasattr(self, "act_tray_record"):
            self.act_tray_record.setText("停止录屏" if recording else "开始录屏")
        if hasattr(self, "act_tray_region_record"):
            self.act_tray_region_record.setText("停止录屏" if recording else "区域录屏")

    def _tick_recording(self) -> None:
        self.rec_elapsed = self.rec_elapsed.addSecs(1)
        self.lbl_elapsed.setText(self.rec_elapsed.toString("HH:mm:ss"))
        if self._recording_status_bar is not None:
            total = (self.rec_elapsed.hour() * 3600
                     + self.rec_elapsed.minute() * 60
                     + self.rec_elapsed.second())
            self._recording_status_bar.set_recording(total)

    # ---------- pre-record countdown ----------

    def _pre_record_start(
        self, crop: Optional[tuple], region_sel: Optional[RegionSelection]
    ) -> None:
        self._pre_record_crop = crop
        self._pre_record_region_sel = region_sel

        bar = _RecordingStatusBar()
        bar.stop_requested.connect(self._stop_record)
        self._recording_status_bar = bar
        bar.set_countdown(3)
        bar.show()

        if region_sel is not None:
            overlay = _RecordingCornerOverlay(
                region_sel.x_pt, region_sel.y_pt,
                region_sel.w_pt, region_sel.h_pt,
            )
            overlay.start()
            self._recording_corner_overlay = overlay

        self._pre_record_countdown_secs = 3
        self._pre_record_timer = QTimer(self)
        self._pre_record_timer.timeout.connect(self._pre_record_tick)
        self._pre_record_timer.start(1000)

    def _pre_record_tick(self) -> None:
        self._pre_record_countdown_secs -= 1
        if self._pre_record_countdown_secs > 0:
            if self._recording_status_bar is not None:
                self._recording_status_bar.set_countdown(self._pre_record_countdown_secs)
        else:
            self._pre_record_timer.stop()
            self._pre_record_timer = None
            self._start_record(self._pre_record_crop)

    def _cancel_pre_record(self) -> None:
        if self._pre_record_timer is not None:
            self._pre_record_timer.stop()
            self._pre_record_timer = None
        self._cleanup_recording_overlays()
        self._pre_record_crop = None
        self._pre_record_region_sel = None
        self._status("录屏已取消")

    def _cleanup_recording_overlays(self) -> None:
        if self._recording_status_bar is not None:
            self._recording_status_bar.close()
            self._recording_status_bar = None
        if self._recording_corner_overlay is not None:
            self._recording_corner_overlay.stop()
            self._recording_corner_overlay = None

    # ---------- lifecycle ----------
    def closeEvent(self, event) -> None:
        if self.recorder and self.recorder.is_recording:
            resp = QMessageBox.question(
                self, "正在录屏",
                "录屏仍在进行,是否先停止并退出?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                event.ignore()
                return
            try:
                self.recorder.stop()
            except Exception:
                pass

        if self.tray is not None and self.tray.isVisible() and not self._user_requested_quit:
            event.ignore()
            self.hide()
            self._notify(APP_NAME, "已隐藏到菜单栏,点击图标再次打开。")
            return

        self._persist_ui_state()
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        super().closeEvent(event)


def _set_macos_accessory_policy() -> None:
    """Switch the app to NSApplicationActivationPolicyAccessory on macOS.

    This does two things we care about:
      1. No Dock icon, no entry in Cmd+Tab switcher.
      2. Our windows can take focus but do **not** own the menu bar — the
         menu bar stays on whatever app was active before, which lets full-
         screen captures actually include that app's menu bar instead of a
         hijacked "Python" menu.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit  # type: ignore
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass


def main() -> int:
    cfg = Config.load()
    cfg.ensure_save_dir()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(_make_app_icon())
    app.setQuitOnLastWindowClosed(False)

    # Single-instance guard: if another rCapture process is already running,
    # notify it to show its main window and exit immediately.
    sock = QLocalSocket()
    sock.connectToServer(_INSTANCE_KEY)
    if sock.waitForConnected(300):
        sock.write(b"show\n")
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return 0

    # This is the primary instance — start a local server so future launches
    # can signal us instead of starting a second copy.
    srv = QLocalServer()
    QLocalServer.removeServer(_INSTANCE_KEY)   # remove any stale socket file
    srv.listen(_INSTANCE_KEY)

    _set_macos_accessory_policy()

    win = MainWindow(cfg)
    win._instance_server = srv   # keep the server alive with the window

    def _on_new_instance() -> None:
        conn = srv.nextPendingConnection()
        if conn:
            conn.close()
        win._show_and_raise()

    srv.newConnection.connect(_on_new_instance)
    if not cfg.start_minimized:
        win.show()

    ok = win.hotkeys.start(cfg.hotkeys)
    if not ok:
        err = win.hotkeys.last_error or ""
        win._status(f"全局快捷键未启用 — {err}")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
