from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "rcapture"
CONFIG_FILE = CONFIG_DIR / "config.json"
# Match macOS's native screenshot destination when the user hasn't picked one.
DEFAULT_SAVE_DIR = Path.home() / "Desktop"


# ------------------- hotkeys -------------------

ACTIONS: tuple[str, ...] = (
    "full_screenshot",
    "region_screenshot",
    "long_screenshot",
    "toggle_full_record",
    "toggle_region_record",
)

ACTION_LABELS: dict[str, str] = {
    "full_screenshot": "全屏截图",
    "region_screenshot": "区域截图",
    "long_screenshot": "截长图",
    "toggle_full_record": "开始/停止全屏录屏",
    "toggle_region_record": "开始/停止区域录屏",
}


def default_hotkeys() -> dict[str, str]:
    """Platform-appropriate defaults. Uses Cmd on macOS, Ctrl elsewhere."""
    mod = "<cmd>+<shift>" if sys.platform == "darwin" else "<ctrl>+<shift>"
    return {
        "full_screenshot":       f"{mod}+1",
        "region_screenshot":     f"{mod}+2",
        "long_screenshot":       f"{mod}+3",
        "toggle_full_record":    f"{mod}+r",
        "toggle_region_record":  f"{mod}+e",
    }


# ------------------- config -------------------

@dataclass
class Config:
    save_dir: str = str(DEFAULT_SAVE_DIR)
    capture_cursor: bool = True
    record_audio: bool = False
    record_fps: int = 30
    avfoundation_screen_index: str = "auto"
    avfoundation_audio_index: str = "0"
    monitor_index: int = 1
    hotkeys: dict[str, str] = field(default_factory=default_hotkeys)
    start_minimized: bool = False
    launch_at_login: bool = False
    # last-used region-screenshot aesthetics — reused as defaults next time
    last_corner_radius_pt: int = 0
    last_shadow_size_pt: int = 0

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text("utf-8"))
                known = {f for f in cls.__dataclass_fields__}
                filtered = {k: v for k, v in data.items() if k in known}
                inst = cls(**filtered)
                # fill missing hotkey entries from defaults
                defaults = default_hotkeys()
                merged = {**defaults, **(inst.hotkeys or {})}
                # drop entries that are no longer known actions
                inst.hotkeys = {k: v for k, v in merged.items() if k in ACTIONS}
                return inst
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), "utf-8")
        Path(self.save_dir).mkdir(parents=True, exist_ok=True)

    def ensure_save_dir(self) -> Path:
        p = Path(self.save_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
