#!/usr/bin/env python3
"""Keep only the part of a scanned page that the text layer does not cover.

A scanned article is stored twice: as a full-page raster, and as a text layer
describing the same page. Where the text layer is complete the raster is
redundant, and build_html.py's --scan-images drops it -- which also removes a
tall block of untranslated original from the top of every page.

But the raster is not always redundant. A title page's masthead, title, byline
and acknowledgment footnote are set in large or italic type that the OCR pass
often misses: on page 1 of this article the text layer starts 350pt down a 696pt
page, so the whole cover exists only in the image.

So the decision is per page rather than global:

  1. find the vertical bands of the page that no text element covers;
  2. measure the ink in each band on the raster itself, because a page margin
     is also uncovered but is blank;
  3. crop the raster to the ink-bearing bands and move the element to match.

Nothing the text layer already carries survives, and nothing the raster alone
carries is lost. Bands thinner than MIN_BAND_PT are ignored -- they cannot hold
a line of type.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys

try:
    from PIL import Image
except ImportError:                                     # pragma: no cover
    sys.stderr.write("ERROR: Pillow is required (pip install pillow)\n")
    raise SystemExit(2)

MIN_BAND_PT = 24.0        # a band thinner than this cannot hold a line of type
MIN_INK = 0.002           # fraction of dark pixels that counts as "has content"
DARK = 200                # grayscale value below which a pixel counts as ink


def _uncovered_bands(y_covered: list[tuple[float, float]],
                     page_h: float) -> list[tuple[float, float]]:
    """Vertical bands of the page that no text element covers."""
    merged: list[list[float]] = []
    for a, b in sorted(y_covered):
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    bands: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in merged:
        if a - cursor >= MIN_BAND_PT:
            bands.append((cursor, a))
        cursor = max(cursor, b)
    if page_h - cursor >= MIN_BAND_PT:
        bands.append((cursor, page_h))
    return bands


def _ink_fraction(im: "Image.Image", y0_pt: float, y1_pt: float,
                  bbox: list[float]) -> float:
    """Share of dark pixels in a horizontal band of the raster."""
    _, by0, _, by1 = bbox
    span = max(by1 - by0, 1e-6)
    h = im.height
    py0 = max(0, int((y0_pt - by0) / span * h))
    py1 = min(h, int((y1_pt - by0) / span * h))
    if py1 <= py0:
        return 0.0
    band = im.crop((0, py0, im.width, py1)).convert("L")
    small = band.resize((min(band.width, 700), max(1, int(band.height * 700 / max(band.width, 1)))))
    px = small.tobytes()
    dark = sum(1 for v in px if v < DARK)
    return dark / max(len(px), 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    kept_pages, dropped_pages = [], []

    for page in model["pages"]:
        pw = page.get("width", 595.0)
        ph = page.get("height", 842.0)
        page_area = max(pw * ph, 1e-6)

        for el in page["elements"]:
            if el.get("kind") != "image":
                continue
            bbox = el["bbox"]
            if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) < 0.8 * page_area:
                continue                                # a figure, not the page

            covered = [
                (e["bbox"][1], e["bbox"][3])
                for e in page["elements"]
                if e.get("kind") in ("text", "formula") and (e.get("text") or "").strip()
            ]
            raw = base64.b64decode(el["image_b64"])
            im = Image.open(io.BytesIO(raw))
            if im.mode not in ("L", "RGB"):
                im = im.convert("L")

            bands = _uncovered_bands(covered, ph)
            if args.report:
                print(f'  page {page["page"]}: {len(bands)} uncovered band(s)')

            kept: list[tuple[float, float]] = []
            for a, b in bands:
                ink = _ink_fraction(im, a, b, bbox)
                has_content = ink >= MIN_INK
                if args.report:
                    print(f'      band {a:>6.0f}-{b:>6.0f}pt  ink={ink*100:5.2f}%  '
                          f'{"KEEP" if has_content else "blank"}')
                if has_content:
                    kept.append((a, b))

            if not kept:
                el["_skip"] = True
                dropped_pages.append(page["page"])
                continue

            top = min(a for a, _ in kept)
            bottom = max(b for _, b in kept)
            by0, by1 = bbox[1], bbox[3]
            span = max(by1 - by0, 1e-6)
            py0 = max(0, int((top - by0) / span * im.height))
            py1 = min(im.height, int((bottom - by0) / span * im.height))
            crop = im.crop((0, py0, im.width, py1))

            buf = io.BytesIO()
            crop.save(buf, format="PNG", optimize=True)
            new_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            # Height is derived from the crop so the element keeps the raster's
            # exact aspect ratio -- otherwise object-fit:contain would letterbox.
            new_h = (py1 - py0) / im.height * span
            el["image_b64"] = new_b64
            el["bbox"] = [bbox[0], top, bbox[2], top + new_h]
            kept_pages.append((page["page"], round(top), round(bottom),
                               len(new_b64) // 1024))
            if args.report:
                print(f'      -> cropped to {top:.0f}-{bottom:.0f}pt '
                      f'({len(new_b64)//1024}KB)')

    print(f"[cover] pages kept (cropped to uncovered content): {len(kept_pages)}")
    for pno, a, b, kb in kept_pages:
        print(f"   page {pno}: y {a}-{b}pt, {kb}KB")
    print(f"[cover] pages dropped (text layer is complete): {len(dropped_pages)}")
    if dropped_pages:
        print("   " + ", ".join(str(p) for p in dropped_pages))

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
