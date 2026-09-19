#!/usr/bin/env python3
"""
reflow_scan.py -- restore reading order for two-column OCR scans.

Problem
-------
`Mankiw.pdf` (Journal of Economic Literature, Dec 1990) is a scanned article.
Each page is a full-page raster plus a hidden OCR text layer (the extraction
bug that discarded that layer entirely was fixed separately in extract.py).

The OCR text layer emits ONE span per visual line, and in a two-column layout a
visual line runs straight across the gutter:

    "At the more applied level, this consen-  nomists have not su"

Both columns are therefore concatenated inside a single element. Sorting those
elements by y interleaves the two columns and the prose becomes unreadable.

Fix
-----
1. Detect the gutter: the x band where wide inter-word gaps cluster. In this
   document it is a very sharp peak near the page centre (~x=250 of 504).
2. Split every visual line at that gutter. A gap only counts as a column break
   when the right-hand word genuinely starts to the right of it; otherwise the
   gap sits inside a hyphenated word and must be left alone.
3. Emit the left column top-to-bottom, then the right column (column-major
   reading order).
4. Rejoin words broken by end-of-line hyphenation.

Output keeps the standard model schema so the rest of the pipeline is
unchanged, and elements are re-indexed afterwards.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys

MIN_GAP = 14.0          # an inter-word gap this wide may be a gutter
Y_TOL = 3.0


def _line_key(y: float) -> int:
    return round(y / Y_TOL)


def _mk_element(words: list[dict]) -> dict:
    """Build one text element from a contiguous run of words."""
    return {
        "kind": "text",
        "bbox": [
            min(w["bbox"][0] for w in words),
            min(w["bbox"][1] for w in words),
            max(w["bbox"][2] for w in words),
            max(w["bbox"][3] for w in words),
        ],
        "text": " ".join(w["text"] for w in words),
        "font": "",
        "size": 10.0,
        "style": {},
        "sentences": [],
    }


def detect_gutter(page_w: float, lines: dict[int, list[dict]]) -> float | None:
    """Return the x centre of the column gutter, or None for a single column."""
    centres: list[float] = []
    for ws in lines.values():
        ws.sort(key=lambda w: w["bbox"][0])
        for i in range(len(ws) - 1):
            gap = ws[i + 1]["bbox"][0] - ws[i]["bbox"][2]
            if gap >= MIN_GAP:
                centres.append((ws[i]["bbox"][2] + ws[i + 1]["bbox"][0]) / 2.0)

    if len(centres) < 4:
        return None

    # The gutter is the tightest cluster of wide gaps: bucket to 8pt, take the
    # winning bucket, then average its members for sub-point precision.
    buckets = collections.Counter(round(c / 8) * 8 for c in centres)
    best, votes = buckets.most_common(1)[0]
    if votes < 0.35 * len(centres):
        return None
    gut = sum(c for c in centres if round(c / 8) * 8 == best) / votes
    if not (0.30 * page_w <= gut <= 0.70 * page_w):
        return None
    return gut


def split_line(ws: list[dict], gutter: float) -> tuple[list[dict], list[dict]]:
    """Partition one visual line into (left, right) column fragments.

    A word is assigned by its own extent. Words that straddle the gutter are
    rare and are left with whichever side holds most of them, so a hyphenated
    word is never torn in half.
    """
    left, right = [], []
    for w in ws:
        x0, x1 = w["bbox"][0], w["bbox"][2]
        if x1 <= gutter + 2:
            left.append(w)
        elif x0 >= gutter - 2:
            right.append(w)
        else:
            # Straddles: give it to the side covering more of the word.
            (left if (gutter - x0) > (x1 - gutter) else right).append(w)
    return left, right


def reflow_page(page: dict, all_words: list[dict]) -> tuple[list[dict], dict]:
    others = [dict(e) for e in page["elements"] if e["kind"] != "text"]
    lines: dict[int, list[dict]] = collections.defaultdict(list)
    for w in all_words:
        lines[_line_key(w["bbox"][1])].append(w)

    gutter = detect_gutter(page["width"], lines)
    stats = {"gutter": None, "after": 0}

    if gutter is None:
        els = others + [_mk_element(ws) for ws in lines.values()]
        els.sort(key=lambda e: (round(e["bbox"][1], 1), e["bbox"][0]))
        stats["after"] = len(els)
        return _reindex(els), stats

    stats["gutter"] = round(gutter, 1)

    # Collect per-line fragments, then emit column-major: every left fragment
    # in top-to-bottom order, followed by every right fragment.
    left_frags: list[list[dict]] = []
    right_frags: list[list[dict]] = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda w: w["bbox"][0])
        left, right = split_line(ws, gutter)
        if left:
            left_frags.append(left)
        if right:
            right_frags.append(right)

    out: list[dict] = []
    for frags in (left_frags, right_frags):
        for ws in frags:
            el = _mk_element(ws)
            _append_or_merge(out, el)

    els = others + out
    stats["after"] = len(els)
    return _reindex(els), stats


def _append_or_merge(out: list[dict], el: dict) -> None:
    """Append `el`, or merge it into the previous element on the same line."""
    if out:
        prev = out[-1]
        same_line = abs(prev["bbox"][3] - el["bbox"][3]) <= Y_TOL
        near = el["bbox"][0] - prev["bbox"][2] <= 6.0
        if same_line and near:
            a, b = prev["text"], el["text"]
            if a.endswith("-") and re.match(r"^[a-z]", b):
                prev["text"] = a[:-1] + b          # rejoined across a break
            else:
                prev["text"] = (a + " " + b).strip()
            prev["bbox"][2] = max(prev["bbox"][2], el["bbox"][2])
            prev["bbox"][3] = max(prev["bbox"][3], el["bbox"][3])
            prev["sentences"] = []
            return
    out.append(el)


def _reindex(els: list[dict]) -> list[dict]:
    for i, e in enumerate(els):
        e["_idx"] = i
    return els


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    import pymupdf

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    doc = pymupdf.open(args.pdf)
    split_pages = 0
    for page in model["pages"]:
        raw = doc[page["page"] - 1].get_text("words")
        words = [{"text": w[4], "bbox": [w[0], w[1], w[2], w[3]]} for w in raw]
        if not words:
            print(f"  p{page['page']:>3}: no words")
            continue
        els, st = reflow_page(page, words)
        page["elements"] = els
        if st["gutter"] is not None:
            split_pages += 1
        print(f"  p{page['page']:>3}: gutter={st['gutter']} -> {st['after']} elements")
    doc.close()

    print(f"[scan-reflow] pages with a detected gutter: {split_pages}/{len(model['pages'])}")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
