from __future__ import annotations

from PIL import Image

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                f"OCR 需要安装 rapidocr-onnxruntime：\n"
                f"pip install rapidocr-onnxruntime\n{e}"
            ) from e
        _engine = RapidOCR()
    return _engine


def ocr_image(img: Image.Image) -> str:
    """Run offline OCR on a PIL image; return the recognised text."""
    try:
        import numpy as np  # type: ignore
    except ImportError as e:
        raise RuntimeError(f"OCR 需要 numpy：pip install numpy\n{e}") from e

    engine = _get_engine()
    arr = np.array(img.convert("RGB"))
    result, _ = engine(arr)
    if not result:
        return ""
    return "\n".join(item[1] for item in result)
