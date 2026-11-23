"""Local OCR for scanned invoices.

Uses RapidOCR (PaddleOCR models exported to ONNX) running on the CPU. The two
small models live in models/ocr/, so nothing is downloaded and no API key or
network is needed.

The output is the same shape the parser gets from a digital PDF: words with
positions. That way scanned and digital invoices go through one parser.
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
import time
from dataclasses import dataclass

import fitz  # PyMuPDF

from .config import ROOT, RUNTIME_DIR

MODELS = ROOT / "models" / "ocr"
DPI = 150
CACHE_DIR = RUNTIME_DIR / "ocr_cache"

_engine = None
_engine_lock = threading.Lock()
_import_error: str | None = None


class OCRError(RuntimeError):
    pass


@dataclass
class OCRWord:
    x0: float
    y: float        # vertical centre after deskew, in PDF points
    height: float   # line height in points (used to find the biggest text, i.e. the vendor name)
    text: str
    score: float
    line: int = 0   # which OCR text line the word came from


def available() -> tuple[bool, str | None]:
    try:
        import numpy  # noqa: F401
        import rapidocr  # noqa: F401
    except Exception as e:  # pragma: no cover - depends on install
        return False, f"OCR engine not installed ({e})"
    if not (MODELS / "PP-OCRv6_det_tiny.onnx").exists():
        return False, "OCR model files missing from models/ocr/"
    return True, None


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            ok, why = available()
            if not ok:
                raise OCRError(why)
            from rapidocr import RapidOCR
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            _engine = RapidOCR(params={
                "Global.use_cls": False,          # pages are upright (small skew is handled below)
                "Global.log_level": "warning",
                "Det.model_path": str(MODELS / "PP-OCRv6_det_tiny.onnx"),
                "Rec.model_path": str(MODELS / "PP-OCRv6_rec_tiny.onnx"),
            })
        return _engine


def warm_up() -> None:
    """Load the models and run one tiny image so the first real invoice isn't slow."""
    try:
        import numpy as np
        _get_engine()(np.full((64, 256, 3), 255, dtype=np.uint8))
    except Exception:
        pass


def _page_lines(engine, page) -> list[tuple[list, str, float]]:
    import numpy as np
    pix = page.get_pixmap(dpi=DPI)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
    r = engine(img)
    if r.boxes is None:
        return []
    return [(b.tolist(), t, float(s)) for b, t, s in zip(r.boxes, r.txts, r.scores)]


def _to_words(lines: list[tuple[list, str, float]]) -> list[OCRWord]:
    """Turn OCR text lines into words, correcting for the page being slightly rotated."""
    scale = 72.0 / DPI
    slopes = []
    for box, _, _ in lines:
        (x0, y0), (x1, y1) = box[0], box[1]
        if x1 - x0 > 150:
            slopes.append((y1 - y0) / (x1 - x0))
    slope = statistics.median(slopes) if slopes else 0.0

    words: list[OCRWord] = []
    for n, (box, text, score) in enumerate(lines):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        left, right = min(xs), max(xs)
        cx, cy = sum(xs) / 4, sum(ys) / 4
        height = (max(ys) - min(ys)) - abs(slope) * (right - left)
        y = cy - slope * cx                     # undo the rotation
        tokens = text.split()
        total = max(sum(len(t) for t in tokens), 1)
        pos = left
        for t in tokens:                        # spread the words across the line box
            words.append(OCRWord(pos * scale, y * scale, max(height, 1) * scale, t, score, n))
            pos += (right - left) * (len(t) + 1) / (total + len(tokens))
    return words


def ocr_pdf(data: bytes, sha256: str) -> tuple[list[list[OCRWord]], dict]:
    """OCR every page. Results are cached by file hash, so re-running a file is instant."""
    cache = CACHE_DIR / f"{sha256}.json"
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
        pages = [[OCRWord(**w) for w in p] for p in raw["pages"]]
        return pages, {**raw["meta"], "cached": True}

    engine = _get_engine()
    t0 = time.perf_counter()
    doc = fitz.open(stream=data, filetype="pdf")
    pages, scores = [], []
    for page in doc:
        lines = _page_lines(engine, page)
        scores += [s for _, _, s in lines]
        pages.append(_to_words(lines))
    meta = {
        "engine": "RapidOCR (PP-OCRv6 tiny, ONNX, CPU)",
        "ms": int((time.perf_counter() - t0) * 1000),
        "lines": len(scores),
        "mean_confidence": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "min_confidence": round(min(scores), 3) if scores else 0.0,
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"pages": [[w.__dict__ for w in p] for p in pages], "meta": meta}), encoding="utf-8")
    except OSError:
        pass
    return pages, {**meta, "cached": False}
