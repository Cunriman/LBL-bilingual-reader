#!/usr/bin/env python3
"""
ocr_hotspots.py -- Extract text inside figures/images so it can become a
clickable lookup layer over the rendered image.

Figure text ("Input Layer", "Encoder", table headers) is baked into pixels in
the source PDF, so it cannot be selected or clicked. We OCR each image region
and record, for every recognised word, its bounding box *relative to the
image*, scaled to the size the image will be displayed at. The HTML renderer
then overlays transparent <span> hotspots at those coordinates.

Backends, tried in order:
  1. tesseract via pytesseract       (best accuracy, needs tesseract binary)
  2. rapidocr-onnxruntime            (pip-only, bundled models, no system deps)
  3. PyMuPDF's own OCR               (needs Tesseract installed system-wide)

If none is available we emit an empty layer and the build simply proceeds
without figure hotspots -- the document is still usable.

Usage:
  python ocr_hotspots.py model.json -o hotspots.json [--lang eng] [--min-conf 55]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys


WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*[A-Za-z]|[A-Za-z]")


def _try_pytesseract(img_bytes: bytes, min_conf: float):
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    try:
        data = pytesseract.image_to_data(
            im, lang="eng", output_type=pytesseract.Output.DICT
        )
    except Exception:
        return None
    return im.size, data


def _try_rapidocr(img_bytes: bytes, min_conf: float):
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    try:
        engine = _try_rapidocr.engine  # type: ignore[attr-defined]
    except AttributeError:
        engine = RapidOCR()
        _try_rapidocr.engine = engine  # type: ignore[attr-defined]
    try:
        res, _ = engine(np.array(im))
    except Exception:
        return None
    return im.size, res


def ocr_image(img_bytes: bytes, min_conf: float, lang: str) -> tuple[list[dict], str]:
    """Return ([{text, box:[x,y,w,h], conf}], backend_name)."""
    out: list[dict] = []

    r = _try_pytesseract(img_bytes, min_conf)
    if r is not None:
        size, data = r
        n = len(data.get("text", []))
        for i in range(n):
            txt = (data["text"][i] or "").strip()
            if not txt:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = 0.0
            if conf < min_conf:
                continue
            x, y = int(data["left"][i]), int(data["top"][i])
            w, h = int(data["width"][i]), int(data["height"][i])
            if w <= 0 or h <= 0:
                continue
            out.append({"text": txt, "box": [x, y, w, h], "conf": round(conf, 1)})
        if out:
            return out, "pytesseract"

    r = _try_rapidocr(img_bytes, min_conf)
    if r is not None:
        size, res = r
        for item in (res or []):
            try:
                poly, txt, conf = item[0], item[1], float(item[2])
            except Exception:
                continue
            if not txt or conf * 100 < min_conf:
                continue
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            x, y = int(min(xs)), int(min(ys))
            w, h = int(max(xs) - x), int(max(ys) - y)
            if w <= 0 or h <= 0:
                continue
            out.append({"text": txt.strip(), "box": [x, y, w, h], "conf": round(conf * 100, 1)})
        return out, "rapidocr"

    return out, "none"


def split_words(item: dict) -> list[dict]:
    """Split an OCR line into per-word hotspots by proportional widths."""
    txt = item["text"]
    x, y, w, h = item["box"]
    words = [m.group(0) for m in WORD_RE.finditer(txt)]
    if not words:
        return []
    # Proportional allocation by character count is adequate for overlay spam
    # anchors; exact glyph metrics are not available from OCR.
    total_chars = max(len(txt), 1)
    cursor = 0.0
    spans: list[dict] = []
    for wd in words:
        # find this word's position in the original string
        idx = txt.find(wd, int(cursor))
        if idx < 0:
            idx = int(cursor)
        start_ratio = idx / total_chars
        width_ratio = len(wd) / total_chars
        spans.append({
            "w": wd.lower(),
            "x": round(x + start_ratio * w, 1),
            "y": round(float(y), 1),
            "w_px": round(max(width_ratio * w, len(wd) * h * 0.42), 1),
            "h_px": round(float(h), 1),
        })
        cursor = idx + len(wd)
    return spans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--lang", default="eng")
    ap.add_argument("--min-conf", type=float, default=55.0)
    args = ap.parse_args()

    model = json.load(open(args.model, encoding="utf-8"))
    layer: dict[str, list[dict]] = {}
    total_images = 0
    total_words = 0
    backend = "none"

    for page in model["pages"]:
        pno = page["page"]
        for idx, el in enumerate(page["elements"]):
            if el["kind"] != "image" or not el.get("image_b64"):
                continue
            total_images += 1
            span_id = f"{pno}#{idx}"
            try:
                raw = base64.b64decode(el["image_b64"])
            except Exception:
                continue

            items, used = ocr_image(raw, args.min_conf, args.lang)
            if used != "none":
                backend = used

            words: list[dict] = []
            for it in items:
                words.extend(split_words(it))

            # Store in image-pixel coordinates; the renderer scales by
            # (displayed_width / natural_width).
            if words:
                layer[span_id] = {
                    "nat_w": _natural_width(raw),
                    "nat_h": _natural_height(raw),
                    "words": words,
                }
                total_words += len(words)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(layer, f, ensure_ascii=False, separators=(",", ":"))

    sys.stderr.write(
        f"[ocr_hotspots] backend={backend} images={total_images} "
        f"hotspot_words={total_words} -> {args.output}\n"
    )
    print(json.dumps({
        "backend": backend,
        "images_scanned": total_images,
        "hotspot_words": total_words,
        "images_with_text": len(layer),
    }, ensure_ascii=False))
    return 0


def _natural_size(raw: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            return im.size
    except Exception:
        return (0, 0)


def _natural_width(raw: bytes) -> int:
    return _natural_size(raw)[0]


def _natural_height(raw: bytes) -> int:
    return _natural_size(raw)[1]


if __name__ == "__main__":
    raise SystemExit(main())
