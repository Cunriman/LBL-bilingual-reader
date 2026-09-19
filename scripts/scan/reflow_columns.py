#!/usr/bin/env python3
"""
reflow_columns.py -- restore reading order for OCR'd / two-column scans.

Problem
-------
`Mankiw.pdf` is a scanned Journal of Economic Literature article. Every page is
a full-page raster image plus a hidden OCR text layer. The extraction of that
text layer emits elements in PDF content-stream order, which for a two-column
layout interleaves the left and right columns line by line:

    Council (MPS) model. The job of refining
    Introduction
    these models generated many disserta-
    tions. Private and public decision makers
    ...

Reading it as-is produces nonsense, and translating it produces worse nonsense.

Fix
---
Two passes:

1. `split_columns` -- when a page has substantial content on both halves and a
   clear gutter between them, partition elements into columns by x-centre.
2. `order_columns` -- emit the left column top-to-bottom, then the right column.
   Full-width elements (titles, rules, footnotes below the columns) act as
   separators: everything above a full-width element is emitted first, then the
   element itself, then whatever follows.

A short merge pass then glues fragments that share a baseline, so hyphenated
OCR line breaks and stray spans rejoin into readable sentences.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

Y_TOL = 3.0
MIN_COL_ELEMENTS = 4          # fewer than this on a side -> not a real column
GUTTER_FRACTION = 0.06        # required empty band, as a fraction of page width


def _cx(el: dict) -> float:
    b = el["bbox"]
    return (b[0] + b[2]) / 2.0


def _full_width(el: dict, page_w: float) -> bool:
    """An element spanning most of the page (title, rule, wide formula)."""
    b = el["bbox"]
    return (b[2] - b[0]) >= 0.72 * page_w


def _find_gutter(els: list[dict], page_w: float) -> float | None:
    """Return the x position of the column gutter, or None for one column.

    We only accept a gutter that is a genuinely empty vertical band near the
    middle of the page, so a single column of justified text is never split.
    """
    body = [e for e in els if not _full_width(e, page_w)]
    if len(body) < 2 * MIN_COL_ELEMENTS:
        return None

    mid = page_w / 2.0
    # Occupied x-intervals of body text, restricted to the middle region.
    spans = sorted((e["bbox"][0], e["bbox"][2]) for e in body)
    lo, hi = 0.25 * page_w, 0.75 * page_w

    # Candidate gutters: gaps between the right edge of one element and the
    # left edge of the next, inside the middle region.
    best = None
    best_gap = 0.0
    reach = lo
    for x0, x1 in spans:
        if x1 <= reach:
            continue
        if x0 > reach:
            gap = x0 - reach
            centre = (x0 + reach) / 2.0
            if lo <= centre <= hi and gap > best_gap:
                best_gap, best = gap, centre
        reach = max(reach, x1)

    if best is None or best_gap < GUTTER_FRACTION * page_w:
        return None

    # Both sides must actually carry content.
    left = sum(1 for e in body if _cx(e) < best)
    right = sum(1 for e in body if _cx(e) >= best)
    if left < MIN_COL_ELEMENTS or right < MIN_COL_ELEMENTS:
        return None
    return best


def _merge_baselines(els: list[dict]) -> list[dict]:
    """Glue same-baseline fragments, left to right, rejoining hyphen breaks."""
    if not els:
        return els
    ordered = sorted(els, key=lambda e: (round(e["bbox"][1], 1), e["bbox"][0]))
    out: list[dict] = []
    for el in ordered:
        if out:
            prev = out[-1]
            same_line = abs(prev["bbox"][1] - el["bbox"][1]) <= Y_TOL
            contiguous = el["bbox"][0] - prev["bbox"][2] <= 4.0
            if same_line and contiguous:
                a = prev.get("text", "")
                b = el.get("text", "")
                if a.endswith("-"):
                    # A hyphen at a line break is a split word, not real
                    # punctuation: "disserta-" + "tions" -> "dissertations".
                    prev["text"] = a[:-1].rstrip() + b.lstrip()
                else:
                    prev["text"] = (a.rstrip() + " " + b.lstrip()).strip()
                prev["bbox"][2] = max(prev["bbox"][2], el["bbox"][2])
                prev["bbox"][3] = max(prev["bbox"][3], el["bbox"][3])
                continue
        out.append(dict(el))
    return out


def _rejoin_hyphens(els: list[dict]) -> list[dict]:
    """Join a trailing hyphen at end of one line with the next line's start."""
    out: list[dict] = []
    for el in els:
        if out:
            prev = out[-1]
            a = prev.get("text", "").rstrip()
            b = el.get("text", "").lstrip()
            if a.endswith("-") and re.search(r"[A-Za-z]$", a[:-1]) and re.match(r"[a-z]", b):
                prev["text"] = a[:-1] + b
                out.append(el)
                prev["text"] = prev["text"]
                out.pop()
                continue
        out.append(dict(el))
    return out


def reflow_page(page: dict) -> tuple[list[dict], dict]:
    els = [dict(e) for e in page["elements"]]
    page_w = page["width"]
    stats = {"gutter": None, "columns": False, "before": len(els)}

    text_idx = [e for e in els if e["kind"] == "text"]
    gutter = _find_gutter(text_idx, page_w)

    if gutter is None:
        ordered = sorted(els, key=lambda e: (round(e["bbox"][1], 1), e["bbox"][0]))
    else:
        stats["columns"] = True
        stats["gutter"] = round(gutter, 1)
        # Walk full-width elements as separators.
        bands: list[list[dict]] = [[]]
        for e in sorted(els, key=lambda e: e["bbox"][1]):
            if _full_width(e, page_w):
                bands.append([e])
                bands.append([])
            else:
                bands[-1].append(e)
        ordered = []
        for band in bands:
            if len(band) == 1:
                ordered.extend(band)
                continue
            left = [e for e in band if _cx(e) < gutter]
            right = [e for e in band if _cx(e) >= gutter]
            left.sort(key=lambda e: (round(e["bbox"][1], 1), e["bbox"][0]))
            right.sort(key=lambda e: (round(e["bbox"][1], 1), e["bbox"][0]))
            ordered.extend(left + right)

    ordered = _merge_baselines(ordered)
    ordered = _rejoin_hyphens(ordered)
    for i, e in enumerate(ordered):
        e["_idx"] = i
    stats["after"] = len(ordered)
    return ordered, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    col_pages = 0
    b4 = a4 = 0
    for page in model["pages"]:
        b4 += len(page["elements"])
        els, st = reflow_page(page)
        page["elements"] = els
        a4 += len(els)
        if st["columns"]:
            col_pages += 1
            print(f"  p{page['page']:>3}: gutter x={st['gutter']:<6} "
                  f"{st['before']} -> {st['after']} elements")

    print(f"[reflow] two-column pages: {col_pages}/{len(model['pages'])}")
    print(f"[reflow] elements {b4} -> {a4}")
    sent = sum(len(e.get("sentences", [])) for p in model["pages"] for e in p["elements"])
    print(f"[reflow] sentences: {sent}")

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
