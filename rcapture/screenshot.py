from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

import mss
from PIL import Image, ImageDraw, ImageFilter, ImageFont

Mode = Literal["full", "region"]


def _grab_quartz_region(x: int, y: int, w: int, h: int) -> Optional[Image.Image]:
    """Capture a screen region at physical (Retina) resolution via Quartz (macOS only).

    CGWindowListCreateImage uses Quartz logical-point coordinates but returns an
    image at the display's native (backing-store) pixel density — on a 2× Retina
    screen a 100×100-pt selection yields a 200×200-pixel image.
    """
    try:
        from Quartz import (  # type: ignore
            CGWindowListCreateImage, CGRectMake,
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID, kCGWindowImageDefault,
            CGImageGetWidth, CGImageGetHeight,
            CGImageGetDataProvider, CGDataProviderCopyData, CGImageGetBytesPerRow,
        )
        image = CGWindowListCreateImage(
            CGRectMake(x, y, w, h),
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowImageDefault,
        )
        if image is None:
            return None
        pw   = CGImageGetWidth(image)
        ph   = CGImageGetHeight(image)
        bpr  = CGImageGetBytesPerRow(image)
        raw  = bytes(CGDataProviderCopyData(CGImageGetDataProvider(image)))
        return Image.frombytes("RGBA", (pw, ph), raw, "raw", "BGRA", bpr).convert("RGB")
    except Exception:
        return None


class ScreenshotError(RuntimeError):
    pass


def _timestamp_name(ext: str = "png") -> str:
    return f"rCapture_{datetime.now():%Y%m%d_%H%M%S}.{ext}"


def _save_shot(shot, path: Path) -> None:
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    img.save(path, "PNG")


def _apply_mosaic(img: Image.Image, box: tuple[int, int, int, int], block: int = 12) -> None:
    """Pixelate the pixels inside ``box`` in-place."""
    l, t, r, b = box
    l, t = max(0, l), max(0, t)
    r, b = min(img.width, r), min(img.height, b)
    if r <= l or b <= t:
        return
    region = img.crop((l, t, r, b))
    sw = max(1, (r - l) // block)
    sh = max(1, (b - t) // block)
    small = region.resize((sw, sh), Image.NEAREST)
    pixelated = small.resize((r - l, b - t), Image.NEAREST)
    img.paste(pixelated, (l, t))


def _get_pil_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_arrowhead_pil(
    draw: ImageDraw.ImageDraw,
    tip: tuple[int, int],
    base: tuple[int, int],
    color: tuple,
    size: float,
) -> None:
    dx = tip[0] - base[0]
    dy = tip[1] - base[1]
    length = math.hypot(dx, dy)
    if length < 1:
        return
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    p1 = (tip[0] - ux * size + px * size * 0.4,
          tip[1] - uy * size + py * size * 0.4)
    p2 = (tip[0] - ux * size - px * size * 0.4,
          tip[1] - uy * size - py * size * 0.4)
    draw.polygon([tip, p1, p2], fill=color)


def _stable_dir_ref_pil(pts: list, from_end: bool, min_dist: float) -> tuple:
    """Return a point at least min_dist from the tip for stable arrow direction."""
    if from_end:
        tx, ty = pts[-1][0], pts[-1][1]
        for i in range(len(pts) - 2, -1, -1):
            if math.hypot(pts[i][0] - tx, pts[i][1] - ty) >= min_dist:
                return pts[i]
        return pts[0]
    else:
        tx, ty = pts[0][0], pts[0][1]
        for i in range(1, len(pts)):
            if math.hypot(pts[i][0] - tx, pts[i][1] - ty) >= min_dist:
                return pts[i]
        return pts[-1]


def _apply_annotations(img: Image.Image, annotations: list[dict[str, Any]]) -> None:
    """Replay the annotation list onto ``img`` (PIL RGB) in-place.

    Each annotation dict has keys ``kind`` ("pen"|"rect"|"mosaic"),
    ``points`` (list of ``(x, y)`` in image-pixel coords), ``color`` (RGB
    tuple) and ``width`` (stroke width in pixels).
    """
    if not annotations:
        return
    draw = ImageDraw.Draw(img)
    for a in annotations:
        kind = a.get("kind", "")
        pts = a.get("points", [])
        color = tuple(a.get("color", (220, 50, 50)))
        width = int(a.get("width", 3))
        if kind == "pen" and len(pts) >= 2:
            mode = a.get("pen_mode", "line")
            if mode in ("arrow_end", "arrow_both"):
                size = max(10.0, width * 4.0)
                end_ref   = _stable_dir_ref_pil(pts, True,  size)
                start_ref = _stable_dir_ref_pil(pts, False, size) if mode == "arrow_both" else None
                end_base_dx = pts[-1][0] - end_ref[0]; end_base_dy = pts[-1][1] - end_ref[1]
                end_base_lng = math.hypot(end_base_dx, end_base_dy)
                if end_base_lng >= 1:
                    end_base = (pts[-1][0] - end_base_dx/end_base_lng*size,
                                pts[-1][1] - end_base_dy/end_base_lng*size)
                else:
                    end_base = end_ref
                if start_ref is not None:
                    sb_dx = pts[0][0] - start_ref[0]; sb_dy = pts[0][1] - start_ref[1]
                    sb_lng = math.hypot(sb_dx, sb_dy)
                    start_base = (pts[0][0] - sb_dx/sb_lng*size,
                                  pts[0][1] - sb_dy/sb_lng*size) if sb_lng >= 1 else start_ref
                else:
                    start_base = None
                # Filter: exclude points inside either arrowhead zone so shaft never protrudes
                tip_x, tip_y = pts[-1][0], pts[-1][1]
                s0_x,  s0_y  = pts[0][0],  pts[0][1]
                draw_pts = []
                for pt in pts:
                    px, py = pt[0], pt[1]
                    if math.hypot(px - tip_x, py - tip_y) < size:
                        continue
                    if start_ref and math.hypot(px - s0_x, py - s0_y) < size:
                        continue
                    draw_pts.append((px, py))
                if not draw_pts:
                    draw_pts = [start_base or pts[0], end_base]
                else:
                    if start_base:
                        draw_pts[0] = start_base
                    draw_pts.append(end_base)
                draw.line(draw_pts, fill=color, width=width, joint="curve")
                _draw_arrowhead_pil(draw, pts[-1], end_ref, color, size)
                if mode == "arrow_both" and start_ref is not None:
                    _draw_arrowhead_pil(draw, pts[0], start_ref, color, size)
            else:
                draw.line(pts, fill=color, width=width, joint="curve")
        elif kind == "rect" and len(pts) >= 2:
            (x0, y0), (x1, y1) = pts[0], pts[1]
            box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
            if a.get("filled"):
                draw.rectangle(box, fill=color, outline=None)
            else:
                draw.rectangle(box, outline=color, width=width)
        elif kind == "mosaic" and len(pts) >= 2:
            (x0, y0), (x1, y1) = pts[0], pts[1]
            box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            _apply_mosaic(img, box)
        elif kind == "text" and pts:
            text = a.get("text", "")
            font_size = int(a.get("font_size", 16))
            stroke_color = a.get("stroke_color")
            stroke_width = int(a.get("stroke_width", 0))
            if text:
                font = _get_pil_font(font_size)
                kw: dict[str, Any] = {"fill": color, "font": font, "spacing": 4}
                if stroke_color and stroke_width > 0:
                    kw["stroke_width"] = stroke_width
                    kw["stroke_fill"] = tuple(stroke_color)
                draw.multiline_text(pts[0], text, **kw)


def take_full_screenshot(save_dir: Path, monitor_index: int = 1) -> Path:
    """Capture a whole monitor at physical (Retina) resolution where possible."""
    save_dir.mkdir(parents=True, exist_ok=True)
    out = save_dir / _timestamp_name("png")
    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            idx = monitor_index if 0 <= monitor_index < len(monitors) else 1
            if idx == 0 and len(monitors) > 1:
                idx = 1
            mon = monitors[idx]
    except Exception as e:
        raise ScreenshotError(f"截图失败:{e}") from e

    img: Optional[Image.Image] = None
    if sys.platform == "darwin":
        img = _grab_quartz_region(mon["left"], mon["top"], mon["width"], mon["height"])
    if img is None:
        try:
            with mss.mss() as sct:
                shot = sct.grab(mon)
        except Exception as e:
            raise ScreenshotError(f"截图失败:{e}") from e
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    img.save(out, "PNG")
    return out


def _apply_rounded_corners(img: Image.Image, radius: int) -> Image.Image:
    """Return a new RGBA image with transparent corners of the given radius."""
    w, h = img.size
    radius = max(0, min(radius, min(w, h) // 2))
    if radius == 0:
        return img
    # Supersample 4× then downscale with LANCZOS for smooth antialiased edges.
    scale = 4
    mask = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * scale - 1, h * scale - 1), radius=radius * scale, fill=255
    )
    mask = mask.resize((w, h), Image.LANCZOS)
    rgba = img.convert("RGBA")
    rgba.putalpha(mask)
    return rgba


def _apply_drop_shadow(img: Image.Image, shadow_size: int) -> Image.Image:
    """Add a soft dark drop shadow around the image's alpha silhouette.

    Produces a canvas ``img.size + 2*padding`` where the shadow is centered
    on the image with a small downward offset for a natural look. Respects
    the image's alpha channel so rounded corners cast rounded shadows.
    """
    if shadow_size <= 0:
        return img
    rgba = img.convert("RGBA")
    w, h = rgba.size
    pad = max(4, int(shadow_size * 1.5))
    cw, ch = w + 2 * pad, h + 2 * pad
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))

    # A black silhouette painted from the image's alpha, slightly offset
    # downward, then Gaussian-blurred. Using putalpha on a solid-black layer
    # makes the shadow shape match the source exactly (incl. rounded corners).
    alpha = rgba.getchannel("A")
    silhouette = Image.new("RGBA", (w, h), (0, 0, 0, 170))
    silhouette.putalpha(alpha)

    offset = (pad, pad + max(1, shadow_size // 3))
    shadow_layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    shadow_layer.paste(silhouette, offset, silhouette)
    shadow_layer = shadow_layer.filter(
        ImageFilter.GaussianBlur(radius=max(1, shadow_size / 2))
    )

    canvas.alpha_composite(shadow_layer)
    canvas.alpha_composite(rgba, (pad, pad))
    return canvas


def grab_region_image(
    region: tuple[int, int, int, int],
    annotations: Optional[list[dict[str, Any]]] = None,
    corner_radius: int = 0,
    shadow_size: int = 0,
) -> Image.Image:
    """Capture a screen region at physical (Retina) resolution where possible."""
    x, y, w, h = region
    if w <= 0 or h <= 0:
        raise ScreenshotError("区域无效(宽高必须大于 0)。")

    img: Optional[Image.Image] = None
    if sys.platform == "darwin":
        img = _grab_quartz_region(x, y, w, h)
    if img is None:
        try:
            with mss.mss() as sct:
                shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
        except Exception as e:
            raise ScreenshotError(f"截图失败:{e}") from e
        img = Image.frombytes("RGB", shot.size, shot.rgb)

    if annotations:
        _apply_annotations(img, annotations)
    if corner_radius > 0:
        img = _apply_rounded_corners(img, corner_radius)
    if shadow_size > 0:
        img = _apply_drop_shadow(img, shadow_size)
    return img


def take_region_screenshot(
    save_dir: Path,
    region: tuple[int, int, int, int],
    annotations: Optional[list[dict[str, Any]]] = None,
    corner_radius: int = 0,
    shadow_size: int = 0,
) -> Path:
    """Capture a region and save to PNG in ``save_dir``."""
    save_dir.mkdir(parents=True, exist_ok=True)
    out = save_dir / _timestamp_name("png")
    img = grab_region_image(
        region,
        annotations=annotations,
        corner_radius=corner_radius,
        shadow_size=shadow_size,
    )
    img.save(out, "PNG")
    return out


def take_screenshot(
    save_dir: Path,
    mode: Mode = "full",
    region: Optional[tuple[int, int, int, int]] = None,
    monitor_index: int = 1,
    annotations: Optional[list[dict[str, Any]]] = None,
    corner_radius: int = 0,
    shadow_size: int = 0,
) -> Path:
    """Unified entry point — dispatches to full / region capture."""
    if mode == "full":
        return take_full_screenshot(save_dir, monitor_index=monitor_index)
    if mode == "region":
        if region is None:
            raise ScreenshotError("区域截图需要提供 region 参数。")
        return take_region_screenshot(
            save_dir, region,
            annotations=annotations,
            corner_radius=corner_radius,
            shadow_size=shadow_size,
        )
    raise ScreenshotError(f"未知截图模式:{mode}")
