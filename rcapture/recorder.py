from __future__ import annotations

import re
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


class RecorderError(RuntimeError):
    pass


def _find_ffmpeg() -> str:
    """Return path to ffmpeg: bundled copy inside .app first, then system PATH."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent  # Contents/MacOS/
        for candidate in [
            exe_dir / "ffmpeg",
            exe_dir.parent / "Frameworks" / "ffmpeg",
            exe_dir.parent / "Resources" / "ffmpeg",
            Path(sys._MEIPASS) / "ffmpeg",
        ]:
            if candidate.exists():
                return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RecorderError("系统未找到 ffmpeg，请先安装 (brew install ffmpeg)。")


def _timestamp_name(ext: str = "mp4") -> str:
    return f"rCapture_{datetime.now():%Y%m%d_%H%M%S}.{ext}"


def list_avfoundation_devices() -> dict:
    """Parse `ffmpeg -f avfoundation -list_devices true -i ""` output.

    Returns {"video": [(index, name), ...], "audio": [(index, name), ...]}.
    """
    ffmpeg = _find_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    out = proc.stderr  # ffmpeg prints device list to stderr
    video: list[tuple[str, str]] = []
    audio: list[tuple[str, str]] = []
    section: Optional[str] = None
    for line in out.splitlines():
        if "AVFoundation video devices" in line:
            section = "video"; continue
        if "AVFoundation audio devices" in line:
            section = "audio"; continue
        m = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if not m or section is None:
            continue
        idx, name = m.group(1), m.group(2).strip()
        (video if section == "video" else audio).append((idx, name))
    return {"video": video, "audio": audio}


def pick_screen_index(devices: dict) -> str:
    """Pick the first 'Capture screen' index from video devices, falling back to "1"."""
    for idx, name in devices.get("video", []):
        if "Capture screen" in name or "screen" in name.lower():
            return idx
    # fall back — on most macs, Capture screen 0 is index 1 (after built-in cameras)
    return "1"


class ScreenRecorder:
    """Wraps an ffmpeg subprocess recording the macOS screen via AVFoundation."""

    def __init__(
        self,
        save_dir: Path,
        screen_index: str = "auto",
        audio_index: Optional[str] = None,
        fps: int = 30,
        capture_cursor: bool = True,
    ):
        self.save_dir = save_dir
        self.screen_index = screen_index
        self.audio_index = audio_index
        self.fps = fps
        self.capture_cursor = capture_cursor

        self._proc: Optional[subprocess.Popen] = None
        self._out_path: Optional[Path] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_tail: list[str] = []

    @property
    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def output_path(self) -> Optional[Path]:
        return self._out_path

    def start(self, crop: Optional[tuple[int, int, int, int]] = None) -> Path:
        """Start recording. `crop` is ``(w, h, x, y)`` in physical pixels, or None."""
        if self.is_recording:
            raise RecorderError("录屏已在进行中。")

        ffmpeg = _find_ffmpeg()
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._out_path = self.save_dir / _timestamp_name("mp4")

        screen_idx = self.screen_index
        if screen_idx == "auto":
            screen_idx = pick_screen_index(list_avfoundation_devices())

        audio_part = self.audio_index if self.audio_index not in (None, "", "none") else "none"
        input_spec = f"{screen_idx}:{audio_part}"

        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation",
            "-framerate", str(self.fps),
            "-capture_cursor", "1" if self.capture_cursor else "0",
            "-pixel_format", "nv12",   # AVFoundation native; ffmpeg converts to yuv420p
            "-i", input_spec,
        ]
        if crop is not None:
            w, h, x, y = crop
            cmd.extend(["-vf", f"crop={w}:{h}:{x}:{y}"])
        cmd.extend([
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-y", str(self._out_path),
        ])

        self._stderr_tail = []
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()
        return self._out_path

    def _drain_stderr(self) -> None:
        assert self._proc is not None
        stderr = self._proc.stderr
        if stderr is None:
            return
        for line in stderr:
            self._stderr_tail.append(line.rstrip())
            if len(self._stderr_tail) > 50:
                self._stderr_tail.pop(0)

    def stop(self, timeout: float = 8.0) -> Optional[Path]:
        if self._proc is None:
            return None
        proc = self._proc
        try:
            if proc.poll() is None:
                # SIGINT is the most reliable way to ask ffmpeg to flush and exit.
                try:
                    proc.send_signal(signal.SIGINT)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        finally:
            self._proc = None
            if self._stderr_thread:
                self._stderr_thread.join(timeout=2)

        if self._out_path and self._out_path.exists() and self._out_path.stat().st_size > 0:
            return self._out_path

        # Build a useful error message, filtering out objc runtime noise.
        real_lines = [
            l for l in self._stderr_tail
            if not l.startswith("objc[") and l.strip()
        ]
        if real_lines:
            tail = "\n".join(real_lines[-10:])
            raise RecorderError(f"录屏未生成输出文件。ffmpeg 日志:\n{tail}")
        # No real ffmpeg errors — almost certainly a macOS permission issue.
        raise RecorderError(
            "录屏未生成输出文件。\n\n"
            "最可能的原因：Python / PyCharm 未获得『屏幕录制』权限。\n"
            "请前往『系统设置 → 隐私与安全性 → 屏幕录制』，\n"
            "勾选 Terminal / PyCharm / Python，然后重启应用。"
        )
