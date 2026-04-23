from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, QTimer, Signal
from PIL import Image as _PILImage

from PySide6.QtGui import (
    QColor, QFontMetrics, QGuiApplication, QImage, QPainter,
    QPainterPath, QPen, QFont, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QWidget, QFrame, QHBoxLayout, QLabel, QPushButton, QApplication,
    QPlainTextEdit, QVBoxLayout,
)


# ======================================================================
# Color presets and tool-popup helpers
# ======================================================================

_PRESET_COLORS: list[tuple[int, int, int]] = [
    (220,  50,  50),   # red
    (255, 150,  30),   # orange
    (255, 220,  50),   # yellow
    ( 60, 200,  80),   # green
    ( 50, 120, 255),   # blue
]


class _HoverButton(QPushButton):
    """QPushButton that emits hovered(True/False) on enter/leave."""
    hovered = Signal(bool)

    def enterEvent(self, e) -> None:
        super().enterEvent(e)
        self.hovered.emit(True)

    def leaveEvent(self, e) -> None:
        super().leaveEvent(e)
        self.hovered.emit(False)


class _ColorSwatch(QWidget):
    """Clickable color circle; color may be None (= transparent / no-stroke)."""
    color_picked = Signal(object)   # tuple(r,g,b) | None

    def __init__(self, color, parent=None) -> None:
        super().__init__(parent)
        self._color = color
        self._selected = False
        self.setFixedSize(24, 24)
        self.setCursor(Qt.PointingHandCursor)

    def set_selected(self, v: bool) -> None:
        self._selected = v
        self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.color_picked.emit(self._color)

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy, r = 12, 12, 8
        if self._color is None:
            p.setPen(QPen(QColor(150, 150, 150), 1))
            p.setBrush(QColor(240, 240, 240))
            p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
            p.setPen(QPen(QColor(200, 60, 60), 1.5))
            p.drawLine(cx - r + 2, cy - r + 2, cx + r - 2, cy + r - 2)
        else:
            border = QColor("white") if self._selected else QColor(100, 100, 100)
            p.setPen(QPen(border, 1.5))
            p.setBrush(QColor(*self._color))
            p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        if self._selected:
            p.setPen(QPen(QColor("white"), 2.0))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(cx - r - 2, cy - r - 2, (r + 2) * 2, (r + 2) * 2)


class _ToolPopup(QFrame):
    """Base floating popup that auto-hides when the cursor leaves."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("""
            QFrame {
                background: rgba(25, 25, 25, 245);
                border: 1px solid rgba(255,255,255,50);
                border-radius: 8px;
            }
            QLabel {
                color: rgba(180,180,180,220); font-size: 11px;
                background: transparent; border: none;
            }
            QPushButton {
                color: white; background: transparent;
                border: 1px solid rgba(255,255,255,30);
                padding: 3px 8px; font-size: 11px; border-radius: 4px;
            }
            QPushButton:hover  { background: rgba(255,255,255,25); }
            QPushButton:checked { background: #2c7be5; border-color: #2c7be5; }
        """)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.hide()

    def schedule_hide(self) -> None:
        # Called when the trigger button loses hover: give cursor time to reach popup.
        self._hide_timer.setInterval(100)
        self._hide_timer.start()

    def cancel_hide(self) -> None:
        self._hide_timer.stop()

    def enterEvent(self, e) -> None:
        self.cancel_hide()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        # Mouse left the popup itself — hide immediately.
        self._hide_timer.setInterval(0)
        self._hide_timer.start()
        super().leaveEvent(e)

    def show_above(self, btn: QWidget) -> None:
        self.adjustSize()
        par = self.parent()
        btn_pos = btn.mapTo(par, QPoint(0, 0))
        x = btn_pos.x()
        y = btn_pos.y() - self.height() - 2   # 2px gap (small enough to cross in < 100ms)
        if x + self.width() > par.width():
            x = max(0, par.width() - self.width())
        if x < 0:
            x = 0
        if y < 0:
            y = btn_pos.y() + btn.height() + 2
        self.move(x, y)
        self.cancel_hide()
        self.show()
        self.raise_()


class _PenPopup(_ToolPopup):
    color_changed = Signal(tuple)   # (r, g, b)
    mode_changed  = Signal(str)     # "line" | "arrow_end" | "arrow_both"
    activated     = Signal()        # any pick → auto-select the tool

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        root.addWidget(QLabel("颜色"))
        color_row = QHBoxLayout()
        color_row.setSpacing(4)
        color_row.setContentsMargins(0, 0, 0, 0)
        self._swatches: list[_ColorSwatch] = []
        for c in _PRESET_COLORS:
            sw = _ColorSwatch(c, self)
            sw.color_picked.connect(self._on_color)
            color_row.addWidget(sw)
            self._swatches.append(sw)
        color_row.addStretch()
        root.addLayout(color_row)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        mode_row.setContentsMargins(0, 0, 0, 0)
        self._mode_btns: dict[str, QPushButton] = {}
        for key, lbl in [("line", "纯线条"), ("arrow_end", "终点箭头"), ("arrow_both", "双向箭头")]:
            btn = QPushButton(lbl)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self._on_mode(k))
            self._mode_btns[key] = btn
            mode_row.addWidget(btn)
        mode_row.addStretch()
        root.addLayout(mode_row)

        self.set_color(_PRESET_COLORS[0])
        self.set_mode("line")

    def set_color(self, rgb: tuple) -> None:
        for sw in self._swatches:
            sw.set_selected(sw._color == rgb)

    def set_mode(self, mode: str) -> None:
        for k, btn in self._mode_btns.items():
            btn.setChecked(k == mode)

    def _on_color(self, color: tuple) -> None:
        self.set_color(color)
        self.color_changed.emit(color)
        self.activated.emit()

    def _on_mode(self, mode: str) -> None:
        self.set_mode(mode)
        self.mode_changed.emit(mode)
        self.activated.emit()


class _TextPopup(_ToolPopup):
    text_color_changed   = Signal(tuple)   # (r, g, b)
    stroke_color_changed = Signal(object)  # None | (r, g, b)
    activated            = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        root.addWidget(QLabel("文字颜色"))
        tc_row = QHBoxLayout()
        tc_row.setSpacing(4)
        tc_row.setContentsMargins(0, 0, 0, 0)
        self._text_swatches: list[_ColorSwatch] = []
        for c in _PRESET_COLORS:
            sw = _ColorSwatch(c, self)
            sw.color_picked.connect(self._on_text_color)
            tc_row.addWidget(sw)
            self._text_swatches.append(sw)
        tc_row.addStretch()
        root.addLayout(tc_row)

        root.addWidget(QLabel("描边颜色"))
        sc_row = QHBoxLayout()
        sc_row.setSpacing(4)
        sc_row.setContentsMargins(0, 0, 0, 0)
        self._stroke_swatches: list[_ColorSwatch] = []
        sw_none = _ColorSwatch(None, self)
        sw_none.color_picked.connect(self._on_stroke_color)
        sc_row.addWidget(sw_none)
        self._stroke_swatches.append(sw_none)
        for c in _PRESET_COLORS:
            sw = _ColorSwatch(c, self)
            sw.color_picked.connect(self._on_stroke_color)
            sc_row.addWidget(sw)
            self._stroke_swatches.append(sw)
        sc_row.addStretch()
        root.addLayout(sc_row)

        self.set_text_color(_PRESET_COLORS[0])
        self.set_stroke_color(None)

    def set_text_color(self, rgb: tuple) -> None:
        for sw in self._text_swatches:
            sw.set_selected(sw._color == rgb)

    def set_stroke_color(self, rgb) -> None:
        for sw in self._stroke_swatches:
            sw.set_selected(sw._color == rgb)

    def _on_text_color(self, color: tuple) -> None:
        self.set_text_color(color)
        self.text_color_changed.emit(color)
        self.activated.emit()

    def _on_stroke_color(self, color) -> None:
        self.set_stroke_color(color)
        self.stroke_color_changed.emit(color)
        self.activated.emit()


class _RectPopup(_ToolPopup):
    color_changed  = Signal(tuple)   # (r, g, b)
    filled_changed = Signal(bool)    # True = filled
    activated      = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        root.addWidget(QLabel("颜色"))
        color_row = QHBoxLayout()
        color_row.setSpacing(4)
        color_row.setContentsMargins(0, 0, 0, 0)
        self._swatches: list[_ColorSwatch] = []
        for c in _PRESET_COLORS:
            sw = _ColorSwatch(c, self)
            sw.color_picked.connect(self._on_color)
            color_row.addWidget(sw)
            self._swatches.append(sw)
        color_row.addStretch()
        root.addLayout(color_row)

        fill_row = QHBoxLayout()
        fill_row.setSpacing(4)
        fill_row.setContentsMargins(0, 0, 0, 0)
        self._fill_btns: dict[bool, QPushButton] = {}
        for filled, lbl in [(False, "空心"), (True, "实心")]:
            btn = QPushButton(lbl)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, f=filled: self._on_fill(f))
            self._fill_btns[filled] = btn
            fill_row.addWidget(btn)
        fill_row.addStretch()
        root.addLayout(fill_row)

        self.set_color(_PRESET_COLORS[0])
        self.set_filled(False)

    def set_color(self, rgb: tuple) -> None:
        for sw in self._swatches:
            sw.set_selected(sw._color == rgb)

    def set_filled(self, filled: bool) -> None:
        for k, btn in self._fill_btns.items():
            btn.setChecked(k == filled)

    def _on_color(self, color: tuple) -> None:
        self.set_color(color)
        self.color_changed.emit(color)
        self.activated.emit()

    def _on_fill(self, filled: bool) -> None:
        self.set_filled(filled)
        self.filled_changed.emit(filled)
        self.activated.emit()


# ======================================================================
# Data
# ======================================================================

@dataclass
class Annotation:
    """A single annotation drawn on top of the selection.

    Coordinates are stored in the selector widget's logical points (same
    space as the rect); conversion to image pixels happens at capture time.
    """
    kind: str   # "pen" | "rect" | "mosaic" | "text"
    points: list[tuple[int, int]] = field(default_factory=list)
    color_rgb: tuple[int, int, int] = (220, 50, 50)
    width_pt: int = 3
    text: str = ""
    font_size_pt: int = 16
    stroke_color_rgb: Optional[tuple[int, int, int]] = None
    pen_mode: str = "line"   # "line" | "arrow_end" | "arrow_both"
    filled: bool = False     # rect: True = solid fill, False = outline only


@dataclass
class RegionSelection:
    """A region picked on screen, expressed in Qt widget/screen-geometry points."""

    x_pt: int
    y_pt: int
    w_pt: int
    h_pt: int
    screen_w_pt: int
    screen_h_pt: int
    annotations: list[Annotation] = field(default_factory=list)
    corner_radius_pt: int = 0
    shadow_size_pt: int = 0
    action: str = "clipboard"   # "clipboard" | "save"

    def to_mss_region(self) -> tuple[int, int, int, int]:
        import mss as _mss
        with _mss.mss() as sct:
            mon = sct.monitors[1]
        sx = mon["width"] / max(1, self.screen_w_pt)
        sy = mon["height"] / max(1, self.screen_h_pt)
        x = int(round(self.x_pt * sx)) + mon["left"]
        y = int(round(self.y_pt * sy)) + mon["top"]
        w = int(round(self.w_pt * sx))
        h = int(round(self.h_pt * sy))
        if w % 2:
            w -= 1
        if h % 2:
            h -= 1
        return x, y, w, h

    def to_ffmpeg_crop(self, dpr: float = 1.0) -> tuple[int, int, int, int]:
        """(w, h, x, y) in physical pixels for ffmpeg crop filter.

        ffmpeg AVFoundation on macOS captures at native/physical resolution,
        so coordinates must be scaled by the display's devicePixelRatio.
        """
        x = int(round(self.x_pt * dpr))
        y = int(round(self.y_pt * dpr))
        w = int(round(self.w_pt * dpr))
        h = int(round(self.h_pt * dpr))
        if w % 2:
            w -= 1
        if h % 2:
            h -= 1
        return w, h, x, y

    def widget_point_to_image_pixel(
        self, wx: int, wy: int, image_w: int, image_h: int
    ) -> tuple[int, int]:
        rel_x = wx - self.x_pt
        rel_y = wy - self.y_pt
        sx = image_w / max(1, self.w_pt)
        sy = image_h / max(1, self.h_pt)
        return int(round(rel_x * sx)), int(round(rel_y * sy))

    def annotations_in_image_pixels(
        self, image_w: int, image_h: int
    ) -> list[dict]:
        out: list[dict] = []
        for a in self.annotations:
            pts = [
                self.widget_point_to_image_pixel(px, py, image_w, image_h)
                for (px, py) in a.points
            ]
            scale = max(image_w / max(1, self.w_pt), image_h / max(1, self.h_pt))
            sw = max(1, int(round(2 * scale))) if a.stroke_color_rgb else 0
            out.append({
                "kind": a.kind,
                "points": pts,
                "color": a.color_rgb,
                "width": max(1, int(round(a.width_pt * scale))),
                "text": a.text,
                "font_size": max(8, int(round(a.font_size_pt * scale))),
                "pen_mode": a.pen_mode,
                "stroke_color": a.stroke_color_rgb,
                "stroke_width": sw,
                "filled": a.filled,
            })
        return out

    def corner_radius_in_image_pixels(self, image_w: int, image_h: int) -> int:
        """Scale the widget-space corner radius into image-pixel space."""
        if self.corner_radius_pt <= 0:
            return 0
        scale = max(image_w / max(1, self.w_pt), image_h / max(1, self.h_pt))
        return max(0, int(round(self.corner_radius_pt * scale)))

    def shadow_size_in_image_pixels(self, image_w: int, image_h: int) -> int:
        if self.shadow_size_pt <= 0:
            return 0
        scale = max(image_w / max(1, self.w_pt), image_h / max(1, self.h_pt))
        return max(0, int(round(self.shadow_size_pt * scale)))


# ======================================================================
# Hit testing for resize handles + corner-radius handle
# ======================================================================

_HANDLE_R = 8

_CURSOR_FOR_MODE = {
    "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
    "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
    "n":  Qt.SizeVerCursor,   "s":  Qt.SizeVerCursor,
    "e":  Qt.SizeHorCursor,   "w":  Qt.SizeHorCursor,
    "move":    Qt.SizeAllCursor,
    "radius":  Qt.PointingHandCursor,
    "shadow_top":    Qt.SizeVerCursor,
    "shadow_bottom": Qt.SizeVerCursor,
    "shadow_left":   Qt.SizeHorCursor,
    "shadow_right":  Qt.SizeHorCursor,
}

_RADIUS_ICON_SIZE = 22      # bounding box of the corner glyph
_RADIUS_ICON_INSET = 8      # distance from rect top-left corner

_SHADOW_TICK_BASE = 10      # baseline visible length of each shadow tick (pt)
_SHADOW_TICK_HIT  = 8       # hit-test half-size around each tick's outer end
MAX_SHADOW_SIZE   = 60      # widget-points

_MAG_CELLS   = 15   # odd → center cell = cursor pixel
_MAG_CELL_PX = 9    # logical px per cell in the magnifier grid


def _radius_icon_rect(rect: QRect) -> QRect:
    """Position the radius-handle inside the rect, offset from top-left.

    Offsetting keeps it from colliding with the NW resize handle at the very
    corner; it stays attached to the rect so moving the selection carries it
    along.
    """
    return QRect(
        rect.left() + _RADIUS_ICON_INSET,
        rect.top() + _RADIUS_ICON_INSET,
        _RADIUS_ICON_SIZE, _RADIUS_ICON_SIZE,
    )


def _shadow_tick_endpoint(rect: QRect, side: str, shadow_size: int) -> QPoint:
    ext = _SHADOW_TICK_BASE + max(0, shadow_size)
    if side == "top":    return QPoint(rect.center().x(), rect.top()    - ext)
    if side == "bottom": return QPoint(rect.center().x(), rect.bottom() + ext)
    if side == "left":   return QPoint(rect.left()  - ext, rect.center().y())
    return                      QPoint(rect.right() + ext, rect.center().y())   # "right"


def _shadow_tick_hit(rect: QRect, pos: QPoint, shadow_size: int) -> Optional[str]:
    hs = _SHADOW_TICK_HIT
    for side in ("top", "bottom", "left", "right"):
        p = _shadow_tick_endpoint(rect, side, shadow_size)
        if abs(pos.x() - p.x()) <= hs and abs(pos.y() - p.y()) <= hs:
            return f"shadow_{side}"
    return None


def _hit_test(
    rect: QRect,
    pos: QPoint,
    r: int = _HANDLE_R,
    shadow_size: int = 0,
) -> Optional[str]:
    if rect.isEmpty():
        return None
    # The radius handle wins inside its own small circle.
    if _radius_icon_rect(rect).contains(pos):
        return "radius"
    # Shadow ticks live outside the rect — check them before the corner /
    # edge handles so the user can grab ticks close to a corner.
    st = _shadow_tick_hit(rect, pos, shadow_size)
    if st is not None:
        return st
    corners = {
        "nw": QPoint(rect.left(),  rect.top()),
        "ne": QPoint(rect.right(), rect.top()),
        "sw": QPoint(rect.left(),  rect.bottom()),
        "se": QPoint(rect.right(), rect.bottom()),
    }
    for k, cp in corners.items():
        if abs(pos.x() - cp.x()) <= r and abs(pos.y() - cp.y()) <= r:
            return k
    if rect.top() - r <= pos.y() <= rect.top() + r and rect.left() <= pos.x() <= rect.right():
        return "n"
    if rect.bottom() - r <= pos.y() <= rect.bottom() + r and rect.left() <= pos.x() <= rect.right():
        return "s"
    if rect.left() - r <= pos.x() <= rect.left() + r and rect.top() <= pos.y() <= rect.bottom():
        return "w"
    if rect.right() - r <= pos.x() <= rect.right() + r and rect.top() <= pos.y() <= rect.bottom():
        return "e"
    if rect.contains(pos):
        return "move"
    return None


def _max_corner_radius(rect: QRect) -> int:
    return max(0, min(rect.width(), rect.height()) // 2)


# ======================================================================
# macOS: lift our overlay above the menu bar
# ======================================================================

def _lift_above_menubar(widget: QWidget) -> None:
    """On macOS: raise above the menu bar and force-take key-window focus.

    Historically this only set the NSWindow level. After a hide/show cycle
    (e.g. right-click cancel → reopen), the window would come back as a
    non-key window and macOS would stop routing mouseMove events to it,
    leaving left-drags stuck at a single pixel. Explicitly calling
    ``makeKeyAndOrderFront_`` on every pick() fixes that by promoting the
    overlay to key status again.
    """
    if sys.platform != "darwin":
        return
    try:
        from PySide6.QtGui import QGuiApplication as _G
        if _G.platformName() != "cocoa":
            return
    except Exception:
        return
    try:
        from ctypes import c_void_p  # type: ignore
        import objc  # type: ignore
        from AppKit import NSPopUpMenuWindowLevel, NSApp, NSScreen  # type: ignore
        view_ptr = int(widget.winId())
        if view_ptr == 0:
            return
        view = objc.objc_object(c_void_p=c_void_p(view_ptr))
        window = view.window()
        if window is None:
            return
        window.setLevel_(NSPopUpMenuWindowLevel)
        # Prevent auto-hide when the panel loses key status (default behaviour
        # for Tool/NSPanel windows is to hide on deactivation, which makes the
        # overlay disappear when the user clicks the menu bar strip).
        window.setHidesOnDeactivate_(False)
        # macOS constrains new windows below the menu bar. Reposition the frame
        # to the full screen extent now that we have a high enough window level.
        main_screen = NSScreen.mainScreen()
        if main_screen is not None:
            window.setFrame_display_(main_screen.frame(), True)
        # Hide the system menu bar so clicks at the very top reach our overlay,
        # not the menu bar process. HideMenuBar requires HideDock per Apple docs.
        _NSPresentationHideDock    = 1 << 1   # 2
        _NSPresentationHideMenuBar = 1 << 3   # 8
        try:
            NSApp.setPresentationOptions_(_NSPresentationHideDock | _NSPresentationHideMenuBar)
        except Exception:
            pass
        window.makeKeyAndOrderFront_(None)
        try:
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass
    except Exception:
        pass


def _restore_presentation() -> None:
    """Restore default macOS presentation options (show menu bar + dock)."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApp  # type: ignore
        NSApp.setPresentationOptions_(0)
    except Exception:
        pass


# ======================================================================
# Inline text input for the text annotation tool
# ======================================================================

class _TextInput(QPlainTextEdit):
    """Transparent inline multi-line text editor that floats over the selection.

    Return commits; Shift+Return inserts a newline.
    """

    committed = Signal(str)   # text content (empty string = cancelled)

    def __init__(self, parent: QWidget, font_size_pt: int) -> None:
        super().__init__(parent)
        self._done = False
        font = QFont()
        font.setPixelSize(max(10, font_size_pt))
        self.setFont(font)
        self.setStyleSheet(
            "QPlainTextEdit {"
            " background: rgba(0,0,0,160); color: white;"
            " border: 1px solid #2c7be5; border-radius: 3px; padding: 1px 3px;"
            "}"
        )
        self.setMinimumWidth(140)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._update_size()
        self.document().contentsChanged.connect(self._update_size)

    def _update_size(self) -> None:
        fm = self.fontMetrics()
        text = self.toPlainText()
        lines = text.split('\n') if text else ['']
        w = max(140, max(fm.horizontalAdvance(ln) for ln in lines) + 24)
        h = fm.height() * max(1, len(lines)) + 12
        self.resize(w, h)

    def _emit_once(self, text: str) -> None:
        if not self._done:
            self._done = True
            self.committed.emit(text)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if e.modifiers() & Qt.ShiftModifier:
                self.insertPlainText('\n')
            else:
                self._emit_once(self.toPlainText())
        elif e.key() == Qt.Key_Escape:
            self._emit_once("")
        else:
            super().keyPressEvent(e)

    def focusOutEvent(self, e) -> None:
        self._emit_once(self.toPlainText())
        super().focusOutEvent(e)

    def wheelEvent(self, e) -> None:
        if self.parent():
            self.parent().wheelEvent(e)
        else:
            super().wheelEvent(e)


# ======================================================================
# Edit toolbar
# ======================================================================

class EditToolbar(QFrame):
    """Floating toolbar that appears near the selection rect in adjust mode."""

    PEN, RECT, MOSAIC, TEXT = "pen", "rect", "mosaic", "text"

    tool_changed = Signal(object)     # str | None
    undo_requested = Signal()
    save_requested = Signal()         # → file dialog
    done_requested = Signal()         # → clipboard
    ocr_requested = Signal()          # → OCR text → clipboard

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("EditToolbar")
        self.setAttribute(Qt.WA_NoMousePropagation, True)
        self.setStyleSheet(
            """
            QFrame#EditToolbar {
                background: rgba(30, 30, 30, 230);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 6px;
            }
            QPushButton {
                color: white; background: transparent; border: none;
                padding: 6px 10px; font-size: 12px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 25); border-radius: 4px; }
            QPushButton:checked { background: #2c7be5; border-radius: 4px; }
            QPushButton#doneBtn { background: #2e7d32; border-radius: 4px; }
            QPushButton#doneBtn:hover { background: #1b5e20; }
            QPushButton#ocrBtn { background: #5c35a0; border-radius: 4px; }
            QPushButton#ocrBtn:hover { background: #4a2080; }
            """
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)

        self._btn_pen    = _HoverButton("画笔")
        self._btn_rect   = _HoverButton("矩形")
        self._btn_mosaic = QPushButton("马赛克")
        self._btn_text   = _HoverButton("文字")
        for b in (self._btn_pen, self._btn_rect, self._btn_mosaic, self._btn_text):
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            lay.addWidget(b)

        self._btn_undo = QPushButton("撤销")
        self._btn_save = QPushButton("保存")
        self._btn_ocr  = QPushButton("识文字")
        self._btn_done = QPushButton("完成")
        self._btn_done.setObjectName("doneBtn")
        self._btn_ocr.setObjectName("ocrBtn")
        self._btn_done.setToolTip("复制到剪贴板")
        self._btn_save.setToolTip("保存到文件…")
        self._btn_ocr.setToolTip("OCR识别文字并复制到剪贴板")
        for b in (self._btn_undo, self._btn_save, self._btn_ocr, self._btn_done):
            b.setCursor(Qt.PointingHandCursor)
            lay.addWidget(b)

        self._btn_pen.clicked.connect(lambda: self._pick(self.PEN))
        self._btn_rect.clicked.connect(lambda: self._pick(self.RECT))
        self._btn_mosaic.clicked.connect(lambda: self._pick(self.MOSAIC))
        self._btn_text.clicked.connect(lambda: self._pick(self.TEXT))
        self._btn_undo.clicked.connect(self.undo_requested.emit)
        self._btn_save.clicked.connect(self.save_requested.emit)
        self._btn_ocr.clicked.connect(self.ocr_requested.emit)
        self._btn_done.clicked.connect(self.done_requested.emit)

        self._current: Optional[str] = None
        self.adjustSize()

    def current_tool(self) -> Optional[str]:
        return self._current

    def reset(self) -> None:
        self._current = None
        self._sync_buttons()

    def _pick(self, tool: str) -> None:
        self._current = None if self._current == tool else tool
        self._sync_buttons()
        self.tool_changed.emit(self._current)

    def _sync_buttons(self) -> None:
        self._btn_pen.setChecked(self._current == self.PEN)
        self._btn_rect.setChecked(self._current == self.RECT)
        self._btn_mosaic.setChecked(self._current == self.MOSAIC)
        self._btn_text.setChecked(self._current == self.TEXT)

    def pen_button(self) -> _HoverButton:
        return self._btn_pen

    def rect_button(self) -> _HoverButton:
        return self._btn_rect

    def text_button(self) -> _HoverButton:
        return self._btn_text

    def select_tool(self, tool: str) -> None:
        """Activate a tool without toggling off (called from popup picks)."""
        if self._current != tool:
            self._current = tool
            self._sync_buttons()
            self.tool_changed.emit(self._current)


# ======================================================================
# Arrow rendering helper
# ======================================================================

def _draw_arrowhead(
    p: QPainter, tip: QPointF, base: QPointF, color: QColor, size: float
) -> None:
    dx = tip.x() - base.x()
    dy = tip.y() - base.y()
    length = math.hypot(dx, dy)
    if length < 1:
        return
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    p1 = QPointF(tip.x() - ux * size + px * size * 0.4,
                 tip.y() - uy * size + py * size * 0.4)
    p2 = QPointF(tip.x() - ux * size - px * size * 0.4,
                 tip.y() - uy * size - py * size * 0.4)
    poly = QPolygonF([tip, p1, p2])
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPolygon(poly)


def _arrow_base(
    tip: tuple[float, float], prev: tuple[float, float], size: float
) -> tuple[float, float]:
    """Return the point where the shaft should end (arrowhead base)."""
    dx = tip[0] - prev[0]
    dy = tip[1] - prev[1]
    length = math.hypot(dx, dy)
    if length < 1:
        return tip
    return (tip[0] - dx / length * size, tip[1] - dy / length * size)


def _stable_dir_ref(pts: list, from_end: bool, min_dist: float) -> tuple[float, float]:
    """Return a point at least min_dist from the tip for stable arrow direction.

    Using the immediately adjacent point (pts[-2]) for direction causes jitter
    because consecutive freehand samples can be 1-2 px apart. This walks back
    along the polyline until a point far enough away is found.
    """
    if from_end:
        tx, ty = float(pts[-1][0]), float(pts[-1][1])
        for i in range(len(pts) - 2, -1, -1):
            if math.hypot(pts[i][0] - tx, pts[i][1] - ty) >= min_dist:
                return (float(pts[i][0]), float(pts[i][1]))
        return (float(pts[0][0]), float(pts[0][1]))
    else:
        tx, ty = float(pts[0][0]), float(pts[0][1])
        for i in range(1, len(pts)):
            if math.hypot(pts[i][0] - tx, pts[i][1] - ty) >= min_dist:
                return (float(pts[i][0]), float(pts[i][1]))
        return (float(pts[-1][0]), float(pts[-1][1]))


# ======================================================================
# Region selector widget
# ======================================================================

class RegionSelector(QWidget):
    """Translucent overlay for picking + adjusting + annotating a region."""

    selected = Signal(object)    # RegionSelection
    cancelled = Signal()
    colour_picked = Signal(str)  # "#RRGGBB" or "R, G, B" — whichever was copied
    ocr_cancelled = Signal()     # user aborted OCR via Esc / right-click

    _STATE_IDLE    = 0
    _STATE_DRAWING = 1
    _STATE_ADJUST  = 2
    _STATE_OCR_BUSY = 3

    # Persisted across re-creation (new instance per screenshot)
    _cls_pen_color         = _PRESET_COLORS[0]
    _cls_pen_mode: str     = "line"
    _cls_rect_color        = _PRESET_COLORS[0]
    _cls_rect_filled: bool = False
    _cls_text_color        = _PRESET_COLORS[0]
    _cls_text_stroke_color = None
    _cls_stroke_width: int = 3
    _cls_text_font_size: int = 16

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        # macOS delivers a contextMenuEvent + steals the right-button release
        # on right-click by default, which leaves our widget thinking the
        # right button is still pressed — subsequent left-drags then fail to
        # track and you get a 1×1 rect. Disabling the context-menu hook keeps
        # right-click a plain mouse-button event from Qt's POV.
        self.setContextMenuPolicy(Qt.PreventContextMenu)

        self._state = self._STATE_IDLE
        self._rect: QRect = QRect()
        self._screen_geom: Optional[QRect] = None

        self._drag_mode: Optional[str] = None
        self._drag_start_pos: QPoint = QPoint()
        self._drag_start_rect: QRect = QRect()
        self._drag_start_radius: int = 0
        self._drag_start_shadow: int = 0

        self._corner_radius: int = 0
        self._shadow_size: int = 0
        # defaults applied whenever a fresh rect enters ADJUST — carries last
        # session's values so the user doesn't re-adjust every screenshot.
        self._default_corner_radius: int = 0
        self._default_shadow_size: int = 0

        self._annotations: list[Annotation] = []
        self._current_ann: Optional[Annotation] = None
        self._tool: Optional[str] = None
        self._stroke_width: int = RegionSelector._cls_stroke_width
        self._text_font_size: int = RegionSelector._cls_text_font_size
        self._mouse_pos: QPoint = QPoint()
        self._text_input: Optional[_TextInput] = None
        self._text_anchor: Optional[tuple[int, int]] = None
        self._auto_confirm: bool = False
        self._current_modifiers: Qt.KeyboardModifiers = Qt.NoModifier
        self._screen_pil: Optional[_PILImage.Image] = None
        self._screen_pil_dpr: float = 1.0
        self._screen_pixmap: Optional[QPixmap] = None

        self._ocr_angle: int = 0
        self._ocr_spinner_timer: Optional[QTimer] = None

        # Per-tool color / mode state — restored from class vars across re-creation
        self._pen_color: tuple[int, int, int] = RegionSelector._cls_pen_color
        self._pen_mode: str = RegionSelector._cls_pen_mode
        self._rect_color: tuple[int, int, int] = RegionSelector._cls_rect_color
        self._rect_filled: bool = RegionSelector._cls_rect_filled
        self._text_color: tuple[int, int, int] = RegionSelector._cls_text_color
        self._text_stroke_color: Optional[tuple[int, int, int]] = RegionSelector._cls_text_stroke_color

        self._toolbar = EditToolbar(self)
        self._toolbar.hide()
        self._toolbar.tool_changed.connect(self._on_tool_changed)
        self._toolbar.undo_requested.connect(self._on_undo)
        self._toolbar.done_requested.connect(lambda: self._commit("clipboard"))
        self._toolbar.save_requested.connect(lambda: self._commit("save"))
        self._toolbar.ocr_requested.connect(lambda: self._commit("ocr"))

        # Hover popups (children of this widget so they float freely)
        self._pen_popup  = _PenPopup(self)
        self._rect_popup = _RectPopup(self)
        self._text_popup = _TextPopup(self)
        self._pen_popup.color_changed.connect(self._on_pen_color_changed)
        self._pen_popup.mode_changed.connect(self._on_pen_mode_changed)
        self._pen_popup.activated.connect(lambda: self._activate_tool("pen"))
        self._rect_popup.color_changed.connect(self._on_rect_color_changed)
        self._rect_popup.filled_changed.connect(self._on_rect_filled_changed)
        self._rect_popup.activated.connect(lambda: self._activate_tool("rect"))
        self._text_popup.text_color_changed.connect(self._on_text_color_changed)
        self._text_popup.stroke_color_changed.connect(self._on_text_stroke_changed)
        self._text_popup.activated.connect(lambda: self._activate_tool("text"))
        self._toolbar.pen_button().hovered.connect(self._on_pen_btn_hover)
        self._toolbar.rect_button().hovered.connect(self._on_rect_btn_hover)
        self._toolbar.text_button().hovered.connect(self._on_text_btn_hover)

        # Restore popup visual state from persisted class vars
        self._pen_popup.set_color(self._pen_color)
        self._pen_popup.set_mode(self._pen_mode)
        self._rect_popup.set_color(self._rect_color)
        self._rect_popup.set_filled(self._rect_filled)
        self._text_popup.set_text_color(self._text_color)
        self._text_popup.set_stroke_color(self._text_stroke_color)

    # ---- lifecycle ----
    def pick(
        self,
        initial_corner_radius: int = 0,
        initial_shadow_size: int = 0,
        auto_confirm: bool = False,
    ) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.cancelled.emit()
            return
        self._auto_confirm = auto_confirm
        self._screen_geom = screen.geometry()
        self.setGeometry(self._screen_geom)
        self.setFixedSize(self._screen_geom.size())
        self._state = self._STATE_IDLE
        self._rect = QRect()
        self._drag_mode = None
        self._default_corner_radius = max(0, int(initial_corner_radius))
        self._default_shadow_size   = max(0, min(int(initial_shadow_size), MAX_SHADOW_SIZE))
        self._corner_radius = 0
        self._shadow_size = 0
        self._annotations = []
        self._current_ann = None
        self._tool = None
        self._mouse_pos = QPoint()
        self._cancel_text_input()
        self._pen_popup.hide()
        self._rect_popup.hide()
        self._text_popup.hide()
        self._toolbar.reset()
        self._toolbar.hide()
        self._drag_start_pos = QPoint()
        self._drag_start_rect = QRect()
        # Re-assert tracking + cursor in case a previous session left them
        # out of sync (shouldn't, but cheap insurance).
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        # Capture the screen before show() so the overlay isn't in the image.
        # On macOS: CGDisplayCreateImage captures at the display's true physical
        # pixel resolution. mss uses CGDisplayBounds (logical points) and may
        # return a logical-resolution image, giving _screen_pil_dpr=1.0 on
        # scaled displays — which would force Qt to upscale the frozen background
        # pixmap 2× and make it appear blurry vs the live desktop.
        self._screen_pil = None
        self._screen_pil_dpr = 1.0
        self._screen_pixmap = None
        if sys.platform == "darwin":
            try:
                from Quartz import (  # type: ignore
                    CGDisplayCreateImage, CGMainDisplayID,
                    CGImageGetWidth, CGImageGetHeight, CGImageGetBytesPerRow,
                    CGImageGetDataProvider, CGDataProviderCopyData,
                )
                _cg = CGDisplayCreateImage(CGMainDisplayID())
                if _cg is not None:
                    _pw = CGImageGetWidth(_cg)
                    _ph = CGImageGetHeight(_cg)
                    _bpr = CGImageGetBytesPerRow(_cg)
                    _raw = bytes(CGDataProviderCopyData(CGImageGetDataProvider(_cg)))
                    self._screen_pil = _PILImage.frombytes(
                        "RGBA", (_pw, _ph), _raw, "raw", "BGRA", _bpr
                    ).convert("RGB")
                    self._screen_pil_dpr = _pw / max(1, self._screen_geom.width())
            except Exception:
                pass
        if self._screen_pil is None:
            try:
                import mss as _mss
                with _mss.mss() as sct:
                    mon = sct.monitors[1]
                    shot = sct.grab(mon)
                self._screen_pil = _PILImage.frombytes("RGB", shot.size, shot.rgb)
                self._screen_pil_dpr = shot.width / max(1, self._screen_geom.width())
            except Exception:
                pass
        # Build frozen QPixmap so paintEvent composites over a static image
        # rather than the live (animated) desktop.
        if self._screen_pil is not None:
            try:
                _pil = self._screen_pil
                _rb = _pil.tobytes("raw", "RGB")
                _qi = QImage(_rb, _pil.width, _pil.height,
                             _pil.width * 3, QImage.Format_RGB888)
                _pm = QPixmap.fromImage(_qi)
                _pm.setDevicePixelRatio(self._screen_pil_dpr)
                self._screen_pixmap = _pm
            except Exception:
                pass
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)
        _lift_above_menubar(self)

    # ---- mouse ----
    def mousePressEvent(self, e) -> None:
        pos = e.position().toPoint()

        # Middle-click in ADJUST → pin the screenshot to the screen as a floating overlay.
        if e.button() == Qt.MiddleButton:
            if self._state == self._STATE_ADJUST and not self._rect.isEmpty():
                self._commit("pin")
            e.accept()
            return

        # Right-click: two-state behaviour
        #   - ADJUST:  clear the selection and go back to IDLE so the user
        #              can re-draw without exiting the overlay.
        #   - IDLE / DRAWING: cancel the whole capture.
        if e.button() == Qt.RightButton:
            if self._state == self._STATE_OCR_BUSY:
                self.ocr_cancelled.emit()
                self.finish_ocr()
                e.accept()
                return
            if self._state == self._STATE_ADJUST:
                self._reset_to_idle()
                self.update()
            else:
                self._finish_cancel()
            e.accept()
            return

        # Annotation tool routing wins in adjust mode, *except* over the
        # radius handle or any shadow tick — those always take priority so the
        # user can tweak those values even while a drawing tool is active.
        if self._state == self._STATE_ADJUST:
            mode = _hit_test(self._rect, pos, shadow_size=self._shadow_size)
            if mode == "radius":
                self._drag_mode = "radius"
                self._drag_start_pos = pos
                self._drag_start_radius = self._corner_radius
                return
            if mode and mode.startswith("shadow_"):
                self._drag_mode = mode
                self._drag_start_pos = pos
                self._drag_start_shadow = self._shadow_size
                return
            if self._tool:
                if not self._rect.contains(pos):
                    return
                if self._tool == "text":
                    self._apply_text_input()
                    # Re-edit an existing text annotation when clicked
                    for i in range(len(self._annotations) - 1, -1, -1):
                        ann = self._annotations[i]
                        if ann.kind == "text" and ann.points and ann.text:
                            ax, ay = ann.points[0]
                            font = QFont()
                            font.setPixelSize(max(10, ann.font_size_pt))
                            fm = QFontMetrics(font)
                            lines = ann.text.split('\n')
                            w = max(fm.horizontalAdvance(ln) for ln in lines) + 14
                            h = fm.height() * len(lines) + 8
                            if QRect(ax, ay, w, h).contains(pos):
                                self._annotations.pop(i)
                                self._text_font_size = ann.font_size_pt
                                inp = _TextInput(self, ann.font_size_pt)
                                inp.committed.connect(self._on_text_committed)
                                inp_y = ay - 2
                                inp.setPlainText(ann.text)
                                inp.move(ax, inp_y)
                                inp._update_size()
                                self._text_input = inp
                                self._text_anchor = (ax, inp_y + 2)
                                inp.show()
                                inp.setFocus()
                                cur = inp.textCursor()
                                cur.movePosition(cur.MoveOperation.End)
                                inp.setTextCursor(cur)
                                self.update()
                                return
                    inp = _TextInput(self, self._text_font_size)
                    inp.committed.connect(self._on_text_committed)
                    inp_y = pos.y() - inp.height() // 2
                    inp.move(pos.x(), inp_y)
                    self._text_input = inp
                    self._text_anchor = (pos.x(), inp_y + 2)
                    inp.show()
                    inp.setFocus()
                    return
                if self._tool == "pen":
                    ann_color  = self._pen_color
                    ann_mode   = self._pen_mode
                    ann_filled = False
                elif self._tool == "rect":
                    ann_color  = self._rect_color
                    ann_mode   = "line"
                    ann_filled = self._rect_filled
                else:
                    ann_color  = (220, 50, 50)
                    ann_mode   = "line"
                    ann_filled = False
                self._current_ann = Annotation(
                    kind=self._tool,
                    points=[(pos.x(), pos.y())],
                    color_rgb=ann_color,
                    width_pt=self._stroke_width,
                    pen_mode=ann_mode,
                    filled=ann_filled,
                )
                self.update()
                return
            if mode is not None:
                self._drag_mode = mode
                self._drag_start_pos = pos
                self._drag_start_rect = QRect(self._rect)
                return
            # clicked outside rect → start fresh selection
            self._state = self._STATE_DRAWING
            self._rect = QRect(pos, pos)
            self._drag_start_pos = pos
            self._corner_radius = 0
            self._annotations.clear()
            self._toolbar.hide()
            self.setCursor(Qt.CrossCursor)
            self.update()
            return

        # IDLE → start drawing
        self._state = self._STATE_DRAWING
        self._rect = QRect(pos, pos)
        self._drag_start_pos = pos
        self.setCursor(Qt.CrossCursor)
        self.update()

    def mouseMoveEvent(self, e) -> None:
        pos = e.position().toPoint()
        self._mouse_pos = pos
        self._current_modifiers = e.modifiers()

        if self._current_ann is not None:
            self._extend_current_annotation(pos)
            self.update()
            return

        if self._state == self._STATE_IDLE:
            self.update()
            return

        if self._state == self._STATE_DRAWING:
            shift = bool(self._current_modifiers & Qt.ShiftModifier)
            alt   = bool(self._current_modifiers & Qt.AltModifier)
            anchor = self._drag_start_pos
            dx = pos.x() - anchor.x()
            dy = pos.y() - anchor.y()
            if shift:
                side = max(abs(dx), abs(dy))
                dx = side if dx >= 0 else -side
                dy = side if dy >= 0 else -side
            if alt:
                self._rect = QRect(
                    QPoint(anchor.x() - dx, anchor.y() - dy),
                    QPoint(anchor.x() + dx, anchor.y() + dy),
                ).normalized()
            else:
                self._rect = QRect(
                    anchor, QPoint(anchor.x() + dx, anchor.y() + dy),
                ).normalized()
            self.update()
            return

        if self._state == self._STATE_ADJUST and self._drag_mode:
            self._apply_drag(pos)
            self._position_toolbar()
            return

        if self._state == self._STATE_ADJUST:
            if self._tool:
                # tool active — still switch to appropriate cursor for the
                # radius handle and shadow ticks (and cross/ibeam elsewhere)
                if _radius_icon_rect(self._rect).contains(pos):
                    self.setCursor(Qt.PointingHandCursor)
                else:
                    st = _shadow_tick_hit(self._rect, pos, self._shadow_size)
                    if st:
                        self.setCursor(_CURSOR_FOR_MODE.get(st, Qt.CrossCursor))
                    elif self._tool == "text":
                        self.setCursor(Qt.IBeamCursor)
                    else:
                        self.setCursor(Qt.CrossCursor)
                self.update()  # redraw size badge near cursor
            else:
                mode = _hit_test(self._rect, pos, shadow_size=self._shadow_size)
                self.setCursor(_CURSOR_FOR_MODE.get(mode or "", Qt.CrossCursor))

    def mouseReleaseEvent(self, e) -> None:
        if self._current_ann is not None:
            self._finalize_annotation()
            self.update()
            return

        if self._state == self._STATE_DRAWING:
            if self._rect.width() >= 5 and self._rect.height() >= 5:
                self._state = self._STATE_ADJUST
                # seed radius/shadow from the stored defaults so the new
                # selection already has the user's previously-used values
                self._corner_radius = min(
                    self._default_corner_radius, _max_corner_radius(self._rect),
                )
                self._shadow_size = min(
                    self._default_shadow_size, MAX_SHADOW_SIZE,
                )
                self._position_toolbar()
            else:
                self._state = self._STATE_IDLE
                self._rect = QRect()
            self.update()
            return

        if self._state == self._STATE_ADJUST and self._drag_mode:
            self._drag_mode = None
            self._rect = self._rect.normalized()
            self._corner_radius = min(self._corner_radius, _max_corner_radius(self._rect))
            self._position_toolbar()
            self.update()

    def mouseDoubleClickEvent(self, e) -> None:
        if self._tool:
            return
        if self._state != self._STATE_ADJUST:
            return
        if _radius_icon_rect(self._rect).contains(e.position().toPoint()):
            return
        if self._rect.contains(e.position().toPoint()):
            # double-click is the "done" shortcut → clipboard
            self._commit("clipboard")

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Escape:
            if self._state == self._STATE_OCR_BUSY:
                self.ocr_cancelled.emit()
                self.finish_ocr()
                return
            self._finish_cancel()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self._state == self._STATE_ADJUST and not self._rect.isEmpty():
                self._commit("clipboard")
            return
        if e.key() == Qt.Key_C and self._state == self._STATE_IDLE:
            rgb = self._sample_color_at_cursor()
            if rgb is not None:
                r, g, b = rgb
                if e.modifiers() & Qt.ShiftModifier:
                    text = f"#{r:02X}{g:02X}{b:02X}"
                else:
                    text = f"{r}, {g}, {b}"
                QApplication.clipboard().setText(text)
                self.colour_picked.emit(text)
                self._finish_cancel()
            return
        if e.key() == Qt.Key_Z and (e.modifiers() & Qt.ControlModifier):
            self._on_undo()

    def wheelEvent(self, e) -> None:
        if self._state == self._STATE_ADJUST and not self._tool:
            # Proportional resize from center — each tick scales by ~5 %.
            step = 1 if e.angleDelta().y() > 0 else -1
            r = self._rect.normalized()
            cx = r.center().x()
            cy = r.center().y()
            scale = 1.0 + step * 0.05
            new_w = max(20, int(round(r.width()  * scale)))
            new_h = max(20, int(round(r.height() * scale)))
            if new_w % 2:
                new_w += 1
            if new_h % 2:
                new_h += 1
            new_rect = QRect(
                int(cx - new_w / 2), int(cy - new_h / 2),
                new_w, new_h,
            )
            if self._screen_geom is not None:
                new_rect = new_rect.intersected(self._screen_geom)
            self._rect = new_rect
            self._corner_radius = min(self._corner_radius, _max_corner_radius(self._rect))
            self._position_toolbar()
            self.update()
            e.accept()
            return
        if self._state != self._STATE_ADJUST or not self._tool:
            e.ignore()
            return
        step = 1 if e.angleDelta().y() > 0 else -1
        if self._tool in ("pen", "rect"):
            self._stroke_width = max(1, min(20, self._stroke_width + step))
            RegionSelector._cls_stroke_width = self._stroke_width
        elif self._tool == "text":
            self._text_font_size = max(8, min(72, self._text_font_size + step))
            RegionSelector._cls_text_font_size = self._text_font_size
            if self._text_input is not None:
                font = QFont()
                font.setPixelSize(self._text_font_size)
                self._text_input.setFont(font)
                self._text_input._update_size()
        self.update()
        e.accept()

    # ---- annotation helpers ----
    def _extend_current_annotation(self, pos: QPoint) -> None:
        ann = self._current_ann
        if ann is None:
            return
        x = max(self._rect.left(), min(pos.x(), self._rect.right()))
        y = max(self._rect.top(),  min(pos.y(), self._rect.bottom()))
        shift = bool(self._current_modifiers & Qt.ShiftModifier)
        if ann.kind == "pen":
            if shift and ann.points:
                # Shift: straight line from anchor to current position
                ann.points = [ann.points[0], (x, y)]
            else:
                ann.points.append((x, y))
        elif ann.kind == "rect":
            if shift and ann.points:
                x0, y0 = ann.points[0]
                side = max(abs(x - x0), abs(y - y0))
                x = x0 + (side if x >= x0 else -side)
                y = y0 + (side if y >= y0 else -side)
                x = max(self._rect.left(), min(x, self._rect.right()))
                y = max(self._rect.top(),  min(y, self._rect.bottom()))
            if len(ann.points) < 2:
                ann.points.append((x, y))
            else:
                ann.points[1] = (x, y)
        else:
            if len(ann.points) < 2:
                ann.points.append((x, y))
            else:
                ann.points[1] = (x, y)

    def _finalize_annotation(self) -> None:
        ann = self._current_ann
        self._current_ann = None
        if ann is None:
            return
        if ann.kind == "pen":
            if len(ann.points) >= 2:
                self._annotations.append(ann)
            return
        if len(ann.points) >= 2:
            (x0, y0), (x1, y1) = ann.points[0], ann.points[1]
            if abs(x1 - x0) >= 3 and abs(y1 - y0) >= 3:
                self._annotations.append(ann)

    def _on_undo(self) -> None:
        if self._annotations:
            self._annotations.pop()
            self.update()

    def _cancel_text_input(self) -> None:
        """Destroy any live text input without applying its content."""
        if self._text_input is not None:
            try:
                self._text_input.committed.disconnect()
            except Exception:
                pass
            self._text_input.hide()
            self._text_input.deleteLater()
            self._text_input = None
            self._text_anchor = None

    def _apply_text_input(self) -> None:
        """Apply pending text input to annotations, then clean up."""
        if self._text_input is not None and not self._text_input._done:
            text = self._text_input.toPlainText()
            anchor = self._text_anchor
            self._text_input._done = True   # prevent double-emit on focus-out
            self._cancel_text_input()
            if text.strip() and anchor:
                self._annotations.append(Annotation(
                    kind="text",
                    points=[anchor],
                    color_rgb=self._text_color,
                    width_pt=self._stroke_width,
                    text=text,
                    font_size_pt=self._text_font_size,
                    stroke_color_rgb=self._text_stroke_color,
                ))
                self.update()
        else:
            self._cancel_text_input()

    def _on_text_committed(self, text: str) -> None:
        # Called via Signal from _TextInput (focus-out or Enter).
        # _cancel_text_input disconnects the signal and frees the widget.
        anchor = self._text_anchor
        self._cancel_text_input()
        if text.strip() and anchor:
            self._annotations.append(Annotation(
                kind="text",
                points=[anchor],
                color_rgb=self._text_color,
                width_pt=self._stroke_width,
                text=text,
                font_size_pt=self._text_font_size,
                stroke_color_rgb=self._text_stroke_color,
            ))
            self.update()

    def _on_tool_changed(self, tool) -> None:
        self._apply_text_input()
        self._tool = tool
        if tool == "text":
            self.setCursor(Qt.IBeamCursor)
        elif tool:
            self.setCursor(Qt.CrossCursor)
        else:
            self.setCursor(Qt.ArrowCursor)
        self.update()

    # ---- popup hover handlers ----
    def _on_pen_btn_hover(self, entered: bool) -> None:
        if entered:
            self._pen_popup.show_above(self._toolbar.pen_button())
        else:
            self._pen_popup.schedule_hide()

    def _on_rect_btn_hover(self, entered: bool) -> None:
        if entered:
            self._rect_popup.show_above(self._toolbar.rect_button())
        else:
            self._rect_popup.schedule_hide()

    def _on_text_btn_hover(self, entered: bool) -> None:
        if entered:
            self._text_popup.show_above(self._toolbar.text_button())
        else:
            self._text_popup.schedule_hide()

    def _activate_tool(self, tool: str) -> None:
        self._toolbar.select_tool(tool)

    def _on_pen_color_changed(self, color: tuple) -> None:
        self._pen_color = color
        RegionSelector._cls_pen_color = color

    def _on_pen_mode_changed(self, mode: str) -> None:
        self._pen_mode = mode
        RegionSelector._cls_pen_mode = mode

    def _on_rect_color_changed(self, color: tuple) -> None:
        self._rect_color = color
        RegionSelector._cls_rect_color = color

    def _on_rect_filled_changed(self, filled: bool) -> None:
        self._rect_filled = filled
        RegionSelector._cls_rect_filled = filled

    def _on_text_color_changed(self, color: tuple) -> None:
        self._text_color = color
        RegionSelector._cls_text_color = color

    def _on_text_stroke_changed(self, color) -> None:
        self._text_stroke_color = color
        RegionSelector._cls_text_stroke_color = color

    # ---- drag handling ----
    def _apply_drag(self, pos: QPoint) -> None:
        mode = self._drag_mode or ""
        if mode == "radius":
            dx = pos.x() - self._drag_start_pos.x()
            dy = pos.y() - self._drag_start_pos.y()
            delta = dx if abs(dx) >= abs(dy) else dy
            new_r = self._drag_start_radius + delta
            self._corner_radius = max(0, min(new_r, _max_corner_radius(self._rect)))
            self.update()
            return

        if mode.startswith("shadow_"):
            # Drag outward grows the shadow; inward shrinks it. Use the axis
            # that matches the tick's side.
            side = mode.split("_", 1)[1]
            dx = pos.x() - self._drag_start_pos.x()
            dy = pos.y() - self._drag_start_pos.y()
            outward = {"top": -dy, "bottom": dy, "left": -dx, "right": dx}[side]
            new_s = self._drag_start_shadow + outward
            self._shadow_size = max(0, min(int(new_s), MAX_SHADOW_SIZE))
            self._position_toolbar()
            self.update()
            return

        dx = pos.x() - self._drag_start_pos.x()
        dy = pos.y() - self._drag_start_pos.y()
        orig = self._drag_start_rect
        if mode == "move":
            r = QRect(orig)
            r.translate(dx, dy)
            bounds = self.rect()
            if r.left() < bounds.left():
                r.translate(bounds.left() - r.left(), 0)
            if r.right() > bounds.right():
                r.translate(bounds.right() - r.right(), 0)
            if r.top() < bounds.top():
                r.translate(0, bounds.top() - r.top())
            if r.bottom() > bounds.bottom():
                r.translate(0, bounds.bottom() - r.bottom())
            self._rect = r
        else:
            mods = QApplication.queryKeyboardModifiers()
            shift = bool(mods & Qt.ShiftModifier)
            alt   = bool(mods & Qt.AltModifier)
            self._rect = self._compute_resize(mode, orig, dx, dy, shift, alt)
            self._corner_radius = min(self._corner_radius, _max_corner_radius(self._rect))
        self.update()

    def _compute_resize(
        self, mode: str, orig: QRect,
        dx: int, dy: int, shift: bool, alt: bool,
    ) -> QRect:
        cx = orig.center().x()
        cy = orig.center().y()
        r = QRect(orig)
        if alt:
            if "n" in mode:
                nt = orig.top() + dy;    r.setTop(nt);    r.setBottom(2 * cy - nt)
            if "s" in mode:
                nb = orig.bottom() + dy; r.setBottom(nb); r.setTop(2 * cy - nb)
            if "w" in mode:
                nl = orig.left() + dx;   r.setLeft(nl);   r.setRight(2 * cx - nl)
            if "e" in mode:
                nr = orig.right() + dx;  r.setRight(nr);  r.setLeft(2 * cx - nr)
        else:
            if "n" in mode: r.setTop(orig.top() + dy)
            if "s" in mode: r.setBottom(orig.bottom() + dy)
            if "w" in mode: r.setLeft(orig.left() + dx)
            if "e" in mode: r.setRight(orig.right() + dx)
        r = r.normalized()
        if shift and r.width() > 0 and r.height() > 0:
            ratio = orig.width() / max(1, orig.height())
            w, h = r.width(), r.height()
            if mode in ("n", "s"):
                w = max(1, int(round(h * ratio)))
            elif mode in ("e", "w"):
                h = max(1, int(round(w / ratio)))
            else:
                scale = max(w / max(1, orig.width()), h / max(1, orig.height()))
                w = max(1, int(round(orig.width() * scale)))
                h = max(1, int(round(orig.height() * scale)))
            if alt:
                return QRect(cx - w // 2, cy - h // 2, w, h)
            return self._rect_with_anchor(mode, orig, w, h)
        return r

    def _rect_with_anchor(self, mode: str, orig: QRect, w: int, h: int) -> QRect:
        """Return a w×h rect keeping the anchor (opposite the dragged handle) fixed."""
        l, t, r, b = orig.left(), orig.top(), orig.right(), orig.bottom()
        cx, cy = orig.center().x(), orig.center().y()
        if   mode == "se": return QRect(l,     t,     w, h)
        elif mode == "sw": return QRect(r - w, t,     w, h)
        elif mode == "ne": return QRect(l,     b - h, w, h)
        elif mode == "nw": return QRect(r - w, b - h, w, h)
        elif mode == "s":  return QRect(cx - w // 2, t,     w, h)
        elif mode == "n":  return QRect(cx - w // 2, b - h, w, h)
        elif mode == "e":  return QRect(l,     cy - h // 2, w, h)
        elif mode == "w":  return QRect(r - w, cy - h // 2, w, h)
        return QRect(l, t, w, h)

    # ---- toolbar placement ----
    def _position_toolbar(self) -> None:
        if self._rect.isEmpty() or self._state != self._STATE_ADJUST:
            self._toolbar.hide()
            self._pen_popup.hide()
            self._rect_popup.hide()
            self._text_popup.hide()
            return
        if self._auto_confirm:
            QTimer.singleShot(0, lambda: self._commit("confirm"))
            return
        self._toolbar.adjustSize()
        tw = self._toolbar.width()
        th = self._toolbar.height()
        W = self.width()
        H = self.height()
        # push the toolbar further from the rect when a bottom shadow tick is
        # extended, so the two don't visually collide
        gap = 8 + _SHADOW_TICK_BASE + self._shadow_size + 4
        x = self._rect.right() - tw + 1
        y = self._rect.bottom() + gap
        if x < 0: x = 0
        if x + tw > W: x = max(0, W - tw)
        if y + th > H:
            y_alt = self._rect.top() - gap - th
            if y_alt >= 0:
                y = y_alt
            else:
                y = max(0, self._rect.bottom() - th - gap)
                x = max(0, self._rect.right() - tw - gap)
        self._toolbar.move(x, y)
        self._toolbar.show()
        self._toolbar.raise_()

    # ---- completion ----
    def _commit(self, action: str) -> None:
        self._apply_text_input()
        if self._screen_geom is None or self._rect.isEmpty():
            return
        rect = self._rect.normalized()
        if rect.width() < 5 or rect.height() < 5:
            return
        sel = RegionSelection(
            x_pt=rect.x(), y_pt=rect.y(),
            w_pt=rect.width(), h_pt=rect.height(),
            screen_w_pt=self._screen_geom.width(),
            screen_h_pt=self._screen_geom.height(),
            annotations=list(self._annotations),
            corner_radius_pt=int(self._corner_radius),
            shadow_size_pt=int(self._shadow_size),
            action=action,
        )
        if action == "ocr":
            # Stay visible and show a spinner while the caller runs OCR.
            # The caller must call finish_ocr() when done (or on cancel).
            for w in (self._toolbar, self._pen_popup, self._rect_popup, self._text_popup):
                w.hide()
            self._state = self._STATE_OCR_BUSY
            self._ocr_angle = 0
            if self._ocr_spinner_timer is None:
                self._ocr_spinner_timer = QTimer(self)
                self._ocr_spinner_timer.timeout.connect(self._tick_ocr_spinner)
            self._ocr_spinner_timer.start(50)
            self.selected.emit(sel)
            return
        _restore_presentation()
        self.hide()
        self.selected.emit(sel)

    def _tick_ocr_spinner(self) -> None:
        self._ocr_angle = (self._ocr_angle - 18) % 360
        self.update()

    def finish_ocr(self) -> None:
        """Called by the app once OCR completes or is cancelled."""
        if self._ocr_spinner_timer is not None:
            self._ocr_spinner_timer.stop()
        _restore_presentation()
        self.hide()

    def _finish_cancel(self) -> None:
        self._cancel_text_input()
        try:
            if QApplication.mouseGrabber() is self:
                self.releaseMouse()
        except Exception:
            pass
        _restore_presentation()
        self.hide()
        self.cancelled.emit()

    def _reset_to_idle(self) -> None:
        """Throw away the current rect + any adjustments, re-enter IDLE.

        Used by right-click to let the user restart framing without closing
        the overlay.
        """
        self._drag_mode = None
        self._current_ann = None
        self._tool = None
        self._cancel_text_input()
        self._pen_popup.hide()
        self._rect_popup.hide()
        self._text_popup.hide()
        self._toolbar.reset()
        self._toolbar.hide()
        self._rect = QRect()
        self._corner_radius = 0
        self._shadow_size = 0
        self._annotations.clear()
        self._state = self._STATE_IDLE
        self.setCursor(Qt.CrossCursor)

    # ---- painting ----
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # Always paint the frozen screenshot first so the live desktop never
        # shows through (animated windows / videos would otherwise be visible).
        if self._screen_pixmap is not None:
            p.drawPixmap(0, 0, self._screen_pixmap)
        if self._state != self._STATE_IDLE:
            p.fillRect(self.rect(), QColor(0, 0, 0, 110))

        if not self._rect.isEmpty():
            r = self._rect.normalized()
            cr = self._corner_radius
            # Re-draw the frozen screenshot inside the selection so the overlay
            # does not punch a transparent hole through to the live desktop
            # (which would make the selection content animate).
            if self._screen_pixmap is not None:
                p.save()
                if cr > 0:
                    _sp = QPainterPath()
                    _sp.addRoundedRect(QRectF(r), cr, cr)
                    p.setClipPath(_sp)
                else:
                    p.setClipRect(r)
                p.drawPixmap(0, 0, self._screen_pixmap)
                p.restore()
            # border
            pen = QPen(QColor("#2c7be5"))
            pen.setWidth(2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            if cr > 0:
                p.drawRoundedRect(r, cr, cr)
            else:
                p.drawRect(r)
            # size + radius label
            label = f"{r.width()} × {r.height()}"
            if cr > 0:
                label += f"   ⌒ {cr}"
            p.setPen(QColor("white"))
            font = QFont(); font.setPixelSize(14); font.setBold(True)
            p.setFont(font)
            tx = r.x()
            ty = r.y() - 6
            if ty < 14:
                ty = r.y() + r.height() + 18
            p.drawText(tx, ty, label)
            # annotations inside the rect
            self._paint_annotations(p, r, cr)
            # resize handles only when no tool is active (keeps the UI clean)
            if self._state == self._STATE_ADJUST and not self._tool:
                self._paint_handles(p, r)
            # radius handle (always shown in adjust mode)
            if self._state == self._STATE_ADJUST:
                self._paint_radius_handle(p, r)
                self._paint_shadow_ticks(p, r)

        # size badge near cursor when a drawing tool is active
        if (self._state == self._STATE_ADJUST and self._tool in ("pen", "rect", "text")
                and not self._mouse_pos.isNull()):
            if self._tool == "text":
                badge = f"字号 {self._text_font_size}"
            else:
                badge = f"粗细 {self._stroke_width}"
            p.save()
            badge_font = QFont(); badge_font.setPixelSize(11); badge_font.setBold(True)
            p.setFont(badge_font)
            fm = p.fontMetrics()
            bw = fm.horizontalAdvance(badge) + 12
            bh = 18
            bx = self._mouse_pos.x() + 16
            by = self._mouse_pos.y() + 14
            # keep badge inside the widget
            if bx + bw > self.width():
                bx = self._mouse_pos.x() - bw - 4
            if by + bh > self.height():
                by = self._mouse_pos.y() - bh - 4
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 20, 20, 210))
            p.drawRoundedRect(bx, by, bw, bh, 4, 4)
            p.setPen(QColor("white"))
            p.drawText(bx + 6, by + bh - 4, badge)
            p.restore()

        # magnifier in IDLE mode (before any region is drawn)
        if (self._state == self._STATE_IDLE
                and self._screen_pil is not None
                and not self._mouse_pos.isNull()):
            self._paint_magnifier(p, self._mouse_pos)

        # OCR busy overlay — spinner + "识别中" label inside the selection
        if self._state == self._STATE_OCR_BUSY and not self._rect.isEmpty():
            r = self._rect.normalized()
            cx = float(r.center().x())
            cy = float(r.center().y())
            p.save()
            p.fillRect(r, QColor(0, 0, 0, 100))
            sr = max(16, min(min(r.width(), r.height()) // 5, 40))
            # background circle track (faint)
            p.setPen(QPen(QColor(255, 255, 255, 50), 3))
            p.setBrush(Qt.NoBrush)
            arc_rect = QRectF(cx - sr, cy - sr - 14, sr * 2, sr * 2)
            p.drawEllipse(arc_rect)
            # spinning arc
            arc_pen = QPen(QColor(255, 255, 255, 220), 3)
            arc_pen.setCapStyle(Qt.RoundCap)
            p.setPen(arc_pen)
            p.drawArc(arc_rect, self._ocr_angle * 16, 270 * 16)
            # label
            p.setPen(QColor(255, 255, 255, 220))
            lf = QFont(); lf.setPixelSize(13); lf.setBold(True)
            p.setFont(lf)
            p.drawText(QRectF(float(r.x()), cy + sr - 6, float(r.width()), 22.0),
                       Qt.AlignHCenter | Qt.AlignVCenter, "识别中…")
            p.restore()

        # hint banner
        if self._state == self._STATE_OCR_BUSY:
            return  # no hint during OCR; spinner carries the message
        if self._state == self._STATE_ADJUST:
            if self._tool == "text":
                hint = "点击输入文字 · 滚轮调整字号 · ⌘Z 撤销 · 回车或『完成』复制到剪贴板 · ESC 取消"
            elif self._tool in ("pen", "rect"):
                hint = "拖拽绘制 · Shift 直线/正方形 · 滚轮调整粗细 · ⌘Z 撤销 · 回车或『完成』复制到剪贴板 · ESC 取消"
            elif self._tool:
                hint = "拖拽绘制批注 · 滚轮调整粗细 · ⌘Z 撤销 · 回车或『完成』复制到剪贴板 · ESC 取消"
            else:
                hint = "拖边角调整 · Shift 等比 · Alt(⌥) 中心缩放 · 左上角半圆拖动改圆角 · 中键 Pin · 回车/『完成』复制 · ESC 取消"
        elif self._state == self._STATE_IDLE:
            hint = "拖拽选择区域 · C 复制RGB · Shift+C 复制HEX · ESC 取消"
        else:
            hint = "拖拽选择区域 · Shift 正方形 · Alt(⌥) 中心框选 · ESC 取消"
        # In IDLE there is no dark overlay, so draw a pill behind the hint for legibility.
        p.save()
        font = QFont(); font.setPixelSize(13); p.setFont(font)
        fm = p.fontMetrics()
        hint_w = fm.horizontalAdvance(hint) + 24
        hint_h = 26
        hint_x = (self.width() - hint_w) // 2
        hint_y = 10
        if self._state == self._STATE_IDLE:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 140))
            p.drawRoundedRect(hint_x, hint_y, hint_w, hint_h, 6, 6)
        p.setPen(QColor(255, 255, 255, 220))
        p.drawText(hint_x, hint_y, hint_w, hint_h,
                   Qt.AlignHCenter | Qt.AlignVCenter, hint)
        p.restore()

    def _paint_handles(self, p: QPainter, r: QRect) -> None:
        hs = 8
        half = hs // 2
        points = [
            (r.left(),  r.top()),    (r.center().x(), r.top()),    (r.right(), r.top()),
            (r.left(),  r.center().y()),                             (r.right(), r.center().y()),
            (r.left(),  r.bottom()), (r.center().x(), r.bottom()), (r.right(), r.bottom()),
        ]
        p.setPen(QPen(QColor("white"), 1))
        for (x, y) in points:
            p.fillRect(QRect(x - half, y - half, hs, hs), QColor("#2c7be5"))
            p.drawRect(QRect(x - half, y - half, hs, hs))

    def _paint_radius_handle(self, p: QPainter, r: QRect) -> None:
        """Draw just a rounded-corner glyph — no disc backing.

        It's a single quadratic-bezier path with two short tails, stroked
        first in semi-transparent black (outline) then in white. The curve
        fills most of the icon box so the "rounded" shape is obvious.
        """
        icon = _radius_icon_rect(r)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)

        pad = 3
        inner = icon.adjusted(pad, pad, -pad, -pad)
        cx = inner.center().x()
        cy = inner.center().y()

        # Path:  ──╮
        #          │
        # Short tails (cx→right on top, left→bottom on left) + a quadratic
        # bezier through the NW "corner" control point. The control-point
        # placement at (left, top) gives a pronounced rounded bend.
        path = QPainterPath()
        path.moveTo(inner.right(), inner.top())
        path.lineTo(cx, inner.top())
        path.quadTo(
            float(inner.left()), float(inner.top()),
            float(inner.left()), float(cy),
        )
        path.lineTo(inner.left(), inner.bottom())

        # outer black outline first
        p.setPen(QPen(
            QColor(0, 0, 0, 235), 3.6,
            Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
        ))
        p.drawPath(path)
        # white body on top
        p.setPen(QPen(
            QColor("white"), 1.9,
            Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
        ))
        p.drawPath(path)
        p.restore()

    def _paint_shadow_ticks(self, p: QPainter, r: QRect) -> None:
        """Short stubs on each side that grow with the shadow size."""
        ext = _SHADOW_TICK_BASE + self._shadow_size
        ends = {
            "top":    (QPoint(r.center().x(), r.top()),
                       QPoint(r.center().x(), r.top() - ext)),
            "bottom": (QPoint(r.center().x(), r.bottom()),
                       QPoint(r.center().x(), r.bottom() + ext)),
            "left":   (QPoint(r.left(),  r.center().y()),
                       QPoint(r.left() - ext, r.center().y())),
            "right":  (QPoint(r.right(), r.center().y()),
                       QPoint(r.right() + ext, r.center().y())),
        }
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        for (inner, outer) in ends.values():
            # 2px stroke with a thin dark outline for visibility
            p.setPen(QPen(QColor(0, 0, 0, 200), 4.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(inner, outer)
            p.setPen(QPen(QColor("white"), 2.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(inner, outer)
            # knob at outer end for a clear grab target
            p.setPen(QPen(QColor(0, 0, 0, 220), 1.2))
            p.setBrush(QColor("white"))
            p.drawEllipse(outer, 3, 3)
        # Faint outline showing the shadow's extent — pure feedback.
        if self._shadow_size > 0:
            ext_rect = r.adjusted(-self._shadow_size, -self._shadow_size,
                                   self._shadow_size, self._shadow_size)
            extra_cr = self._corner_radius + self._shadow_size
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.DashLine))
            if extra_cr > 0:
                p.drawRoundedRect(ext_rect, extra_cr, extra_cr)
            else:
                p.drawRect(ext_rect)
        p.restore()

    def _sample_color_at_cursor(self) -> Optional[tuple[int, int, int]]:
        if self._screen_pil is None or self._mouse_pos.isNull():
            return None
        dpr = self._screen_pil_dpr
        pw, ph = self._screen_pil.size
        px = max(0, min(int(self._mouse_pos.x() * dpr), pw - 1))
        py = max(0, min(int(self._mouse_pos.y() * dpr), ph - 1))
        raw = self._screen_pil.getpixel((px, py))
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            v = int(raw)
            return v, v, v
        return int(raw[0]), int(raw[1]), int(raw[2])

    def _paint_magnifier(self, p: QPainter, cur: QPoint) -> None:
        if self._screen_pil is None:
            return
        dpr = self._screen_pil_dpr
        pw, ph = self._screen_pil.size
        half = _MAG_CELLS // 2
        cell = _MAG_CELL_PX
        mag_px = _MAG_CELLS * cell   # pixel-grid area side length
        info_h = 46
        pad = 6
        box_w = mag_px + pad * 2
        box_h = mag_px + info_h + pad * 2

        # Offset magnifier box from cursor; flip if too close to an edge.
        bx = cur.x() + 22
        by = cur.y() + 22
        if bx + box_w > self.width() - 4:
            bx = cur.x() - box_w - 14
        if by + box_h > self.height() - 4:
            by = cur.y() - box_h - 14

        center_r, center_g, center_b = 128, 128, 128

        p.save()

        # Background pill
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(18, 18, 18, 235))
        p.drawRoundedRect(bx, by, box_w, box_h, 8, 8)

        # Pixel grid (no antialiasing for crisp cells)
        p.setRenderHint(QPainter.Antialiasing, False)
        grid_x = bx + pad
        grid_y = by + pad
        cx_px = int(cur.x() * dpr)
        cy_px = int(cur.y() * dpr)
        for row in range(_MAG_CELLS):
            for col in range(_MAG_CELLS):
                spx = max(0, min(cx_px + col - half, pw - 1))
                spy = max(0, min(cy_px + row - half, ph - 1))
                _raw = self._screen_pil.getpixel((spx, spy))
                if isinstance(_raw, (int, float)):
                    rv = gv = bv = int(_raw)
                elif _raw is None:
                    rv = gv = bv = 0
                else:
                    rv, gv, bv = int(_raw[0]), int(_raw[1]), int(_raw[2])
                if row == half and col == half:
                    center_r, center_g, center_b = rv, gv, bv
                p.fillRect(
                    QRect(grid_x + col * cell, grid_y + row * cell, cell, cell),
                    QColor(rv, gv, bv),
                )

        # Outer border around the grid
        p.setPen(QPen(QColor(60, 60, 60), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRect(grid_x, grid_y, mag_px, mag_px))

        # Crosshair highlight on the center cell
        p.setRenderHint(QPainter.Antialiasing, False)
        cx_rect = QRect(grid_x + half * cell, grid_y + half * cell, cell, cell)
        p.setPen(QPen(QColor(255, 255, 255, 210), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(cx_rect)

        # Color info area
        p.setRenderHint(QPainter.Antialiasing, True)
        swatch_size = 18
        info_y = grid_y + mag_px + 8
        # Color swatch
        p.setPen(QPen(QColor(80, 80, 80), 1))
        p.setBrush(QColor(center_r, center_g, center_b))
        p.drawRoundedRect(grid_x, info_y, swatch_size, swatch_size, 3, 3)

        hex_str = f"#{center_r:02X}{center_g:02X}{center_b:02X}"
        rgb_str = f"{center_r}, {center_g}, {center_b}"
        tx = grid_x + swatch_size + 6

        p.setPen(QColor("white"))
        font = QFont(); font.setPixelSize(12); font.setBold(True); p.setFont(font)
        p.drawText(tx, info_y + 12, hex_str)

        p.setPen(QColor(180, 180, 180))
        font.setBold(False); p.setFont(font)
        p.drawText(tx, info_y + 26, rgb_str)

        p.restore()

    def _paint_annotations(self, p: QPainter, rect: QRect, cr: int) -> None:
        p.save()
        # clip to the (possibly rounded) selection so strokes don't bleed past it
        if cr > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect), cr, cr)
            p.setClipPath(path)
        else:
            p.setClipRect(rect)
        p.setRenderHint(QPainter.Antialiasing, True)

        def draw_one(ann: Annotation):
            color = QColor(*ann.color_rgb)
            if ann.kind == "pen":
                pen = QPen(color, ann.width_pt, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                p.setPen(pen)
                pts = ann.points
                n = len(pts)
                mode = ann.pen_mode
                if mode in ("arrow_end", "arrow_both") and n >= 2:
                    size = max(10.0, ann.width_pt * 4.0)
                    end_ref   = _stable_dir_ref(pts, True,  size)
                    start_ref = _stable_dir_ref(pts, False, size) if mode == "arrow_both" else None
                    end_base  = _arrow_base(pts[-1], end_ref, size)
                    start_base = _arrow_base(pts[0], start_ref, size) if start_ref else None
                    tip_x, tip_y = float(pts[-1][0]), float(pts[-1][1])
                    s0_x,  s0_y  = float(pts[0][0]),  float(pts[0][1])
                    # Build shaft: exclude every point within the arrowhead zone(s)
                    # to prevent the shaft from visually protruding past the arrowhead.
                    shaft: list[tuple[float, float]] = []
                    for pt in pts:
                        px, py = float(pt[0]), float(pt[1])
                        if math.hypot(px - tip_x, py - tip_y) < size:
                            continue
                        if start_ref and math.hypot(px - s0_x, py - s0_y) < size:
                            continue
                        shaft.append((px, py))
                    if not shaft:
                        shaft = [start_base or (float(pts[0][0]), float(pts[0][1])), end_base]
                    else:
                        if start_base:
                            shaft[0] = start_base
                        shaft.append(end_base)
                    for i in range(1, len(shaft)):
                        p.drawLine(QPointF(*shaft[i-1]), QPointF(*shaft[i]))
                    _draw_arrowhead(p, QPointF(*pts[-1]), QPointF(*end_ref), color, size)
                    if mode == "arrow_both":
                        _draw_arrowhead(p, QPointF(*pts[0]), QPointF(*start_ref), color, size)
                else:
                    for i in range(1, n):
                        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
                        p.drawLine(x0, y0, x1, y1)
            elif ann.kind == "rect":
                if len(ann.points) >= 2:
                    (x0, y0), (x1, y1) = ann.points[0], ann.points[1]
                    r = QRect(QPoint(x0, y0), QPoint(x1, y1)).normalized()
                    if ann.filled:
                        p.setPen(Qt.NoPen)
                        p.setBrush(color)
                    else:
                        pen = QPen(color, ann.width_pt)
                        p.setPen(pen)
                        p.setBrush(Qt.NoBrush)
                    p.drawRect(r)
            elif ann.kind == "mosaic":
                if len(ann.points) >= 2:
                    (x0, y0), (x1, y1) = ann.points[0], ann.points[1]
                    br = QRect(QPoint(x0, y0), QPoint(x1, y1)).normalized()
                    br = br.intersected(rect)
                    if not br.isEmpty() and self._screen_pil is not None:
                        dpr = self._screen_pil_dpr
                        px = int(br.x() * dpr)
                        py = int(br.y() * dpr)
                        pw = max(1, int(br.width() * dpr))
                        ph = max(1, int(br.height() * dpr))
                        region = self._screen_pil.crop((px, py, px + pw, py + ph))
                        block = max(4, pw // 15)
                        small = region.resize(
                            (max(1, pw // block), max(1, ph // block)),
                            _PILImage.NEAREST,
                        )
                        pixelated = small.resize((pw, ph), _PILImage.NEAREST)
                        raw = bytes(pixelated.tobytes())
                        _qimg = QImage(raw, pw, ph, pw * 3, QImage.Format_RGB888).copy()
                        mosaic_pix = QPixmap.fromImage(_qimg)
                        mosaic_pix.setDevicePixelRatio(dpr)
                        p.drawPixmap(br, mosaic_pix, QRect(0, 0, pw, ph))
                    elif not br.isEmpty():
                        p.fillRect(br, QColor(0, 0, 0, 180))
            elif ann.kind == "text":
                if ann.points and ann.text:
                    x, y = ann.points[0]   # y is the visual text top
                    font = QFont()
                    font.setPixelSize(max(10, ann.font_size_pt))
                    p.setFont(font)
                    fm = p.fontMetrics()
                    line_h = fm.height()
                    for i, line in enumerate(ann.text.split('\n')):
                        ly = y + fm.ascent() + i * line_h
                        if ann.stroke_color_rgb:
                            path = QPainterPath()
                            path.addText(QPointF(x, ly), font, line)
                            stroke_pen = QPen(QColor(*ann.stroke_color_rgb), 3.0)
                            stroke_pen.setJoinStyle(Qt.RoundJoin)
                            p.strokePath(path, stroke_pen)
                            p.fillPath(path, color)
                        else:
                            p.setPen(color)
                            p.drawText(x, ly, line)

        for a in self._annotations:
            draw_one(a)
        if self._current_ann is not None:
            draw_one(self._current_ann)
        p.restore()
