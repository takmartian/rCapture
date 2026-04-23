from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np
from PIL import Image
from PySide6.QtCore import QThread, Signal

from .screenshot import grab_region_image


def _find_new_content_offset(prev_arr: np.ndarray, curr_arr: np.ndarray) -> int:
    """Return y-pixel in curr where new (not-yet-captured) content starts.

    Direct offset search: if the page scrolled up by Y pixels then
    prev[Y : Y+check_h] == curr[0 : check_h].  We try every candidate Y in
    [min_y, max_y] and pick the one with the lowest mean absolute difference.
    This is immune to the direction-of-search false-positive problem of the
    old bottom-strip approach.
    Returns -1 when capture should stop.
    """
    h = min(prev_arr.shape[0], curr_arr.shape[0])
    min_y = max(5, h // 40)
    max_y = (h * 3) // 4
    check_h = min(120, h // 4)
    if max_y <= min_y or check_h < 10:
        return -1

    ref = curr_arr[0:check_h].astype(np.int16)
    best_y, best_diff = -1, float("inf")
    step = max(1, (max_y - min_y) // 200)
    for y in range(min_y, max_y + 1, step):
        if y + check_h > h:
            break
        diff = float(np.mean(np.abs(prev_arr[y: y + check_h].astype(np.int16) - ref)))
        if diff < best_diff:
            best_diff, best_y = diff, y

    if best_diff > 8 or best_y < 0:
        return -1
    new_start = h - best_y
    if new_start >= h - 5:
        return -1  # negligible scroll — end of content
    return new_start


def stitch_images(images: list[Image.Image]) -> Image.Image:
    """Stack images vertically into one tall PNG-ready image."""
    if not images:
        raise ValueError("没有可拼接的图片")
    w = images[0].width
    total_h = sum(img.height for img in images)
    result = Image.new("RGB", (w, total_h))
    y = 0
    for img in images:
        result.paste(img.convert("RGB"), (0, y))
        y += img.height
    return result


class LongScreenshotThread(QThread):
    """Captures a scrolling region and emits a stitched PIL Image when done."""

    frame_captured = Signal(int)   # total pieces collected so far
    finished = Signal(object)      # PIL Image — stitched result (even on cancel)
    failed = Signal(str)           # error message; no image
    cancelled = Signal()           # user cancelled before any frame was collected

    def __init__(
        self,
        region: tuple[int, int, int, int],
        scroll_clicks: int = 5,
        scroll_delay: float = 0.35,
        max_frames: int = 50,
    ) -> None:
        super().__init__()
        self._region = region
        self._scroll_clicks = scroll_clicks
        self._scroll_delay = scroll_delay
        self._max_frames = max_frames
        self._stop_flag = False
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_flag = True
        self._stop_event.set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            import traceback as _tb
            self.failed.emit(f"{exc}\n{_tb.format_exc()}")

    def _run(self) -> None:
        from pynput.mouse import Controller as Mouse  # type: ignore
        from pynput.mouse import Listener as MouseListener, Button  # type: ignore

        x, y, w, h = self._region
        cx, cy = x + w // 2, y + h // 2

        # Stop on right-click anywhere
        def _on_click(mx, my, button, pressed):
            if pressed and button == Button.right:
                self._stop_event.set()
                return False

        ml: Optional[MouseListener] = None
        try:
            ml = MouseListener(on_click=_on_click)
            ml.daemon = True
            ml.start()
        except Exception:
            ml = None

        mouse = Mouse()
        mouse.position = (cx, cy)
        time.sleep(0.15)

        frames: list[Image.Image] = []
        prev_arr: Optional[np.ndarray] = None

        try:
            for _ in range(self._max_frames):
                if self._stop_flag or self._stop_event.is_set():
                    break

                img = grab_region_image(self._region)
                curr_arr = np.array(img.convert("RGB"))

                if prev_arr is not None:
                    new_start = _find_new_content_offset(prev_arr, curr_arr)
                    if new_start < 0:
                        break  # end of content
                    frames.append(img.crop((0, new_start, w, h)))
                else:
                    frames.append(img)

                prev_arr = curr_arr
                self.frame_captured.emit(len(frames))

                if self._stop_flag or self._stop_event.is_set():
                    break

                mouse.position = (cx, cy)
                mouse.scroll(0, -self._scroll_clicks)

                # Interruptible sleep so stop() takes effect promptly
                deadline = time.monotonic() + self._scroll_delay
                while time.monotonic() < deadline:
                    if self._stop_flag or self._stop_event.is_set():
                        break
                    time.sleep(0.05)
        finally:
            if ml is not None:
                try:
                    ml.stop()
                except Exception:
                    pass

        if not frames:
            self.cancelled.emit()
            return
        try:
            self.finished.emit(stitch_images(frames))
        except Exception as exc:
            self.failed.emit(str(exc))
