#!/usr/bin/env python
"""Audit a extracted model for the scan-specific defects this skill knows about.

Why this file exists
--------------------
SKILL.md documents fifteen ways a scanned two-column paper goes wrong. Written
as prose, each one relies on the next reader noticing it and choosing to check.
That is not a guarantee -- it is a hope. The Mankiw job shipped three layout
defects (a folio rendering untranslated, a page collapsed from two columns into
one, blocks alternating between full width and half width) precisely because
nothing in the pipeline was watching for them. The translation gate was the
only hard gate, and it only ever looked at coverage.

So every prose pitfall that can be decided by a rule lives here as a rule, and
the pipeline runs this as a gate. A pitfall becomes a guarantee only when a
machine refuses to let it through.

Severity
--------
  P0  wrong or lost content. Blocks delivery.
  P1  almost certainly a defect; verify before shipping.
  P2  a hint worth a human glance. Not necessarily wrong.

Exit code is 1 when any P0 is found, 0 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from build_html import _detect_gutter
except Exception:                                    # pragma: no cover
    _detect_gutter = None

# --- thresholds ------------------------------------------------------------
PAGE_EDGE_BAND = 0.12        # top/bottom margin band, as a share of page height
FOLIO_MAX_W = 0.12           # a folio is narrow; a data row is not
COLUMN_OVER_TOL = 6.0        # pt a left-column box may exceed the gutter
OVERLAP_MIN = 0.75           # share of a side's span that must sit beside the other
WIDE_BLOCK = 0.60            # a text box wider than this share of the page
CROSS_COLUMN_MAX = 0.15      # share of text elements allowed to span both columns
HEADER_MIN_PAGES = 3         # a phrase repeating on this many pages is furniture
HEADER_PREFIX = 48           # chars of an element used as its header fingerprint
CAPS_RUN = re.compile(r"\b[A-Z][A-Z'\-]{1,}(?:\s+[A-Z][A-Z'\-]{1,}){1,}\b")
DOUBLE_STEM = re.compile(r"\b(?:mac|mic)(?:mac|mic)(?=ro)", re.IGNORECASE)
NUMERAL = re.compile(r"\d{1,4}")


def stem(p: dict) -> tuple[float, float]:
    return p.get("width", 0.0), p.get("height", 0.0)


def texts(p: dict) -> list[dict]:
    return [e for e in p["elements"] if e.get("kind") in ("text", "formula")]


def live(p: dict) -> list[dict]:
    """Text elements that still carry content."""
    return [e for e in texts(p)
            if (e.get("text") or "").strip()
            or any((s or "").strip() for s in (e.get("sentences") or []))]


# --------------------------------------------------------------------------
# P0 -- wrong or lost content
# --------------------------------------------------------------------------
def check_folio(model: dict) -> list[str]:
    """A running page number still sitting in a margin band.

    Universal signal, no journal-specific literals: a bare numeral, narrow,
    parked in the top or bottom margin. It is the only text on the page with
    no Chinese under it, and if it sits right of the centre line it also drags
    the gutter off the real column start.
    """
    out = []
    for p in model["pages"]:
        pw, ph = stem(p)
        if pw <= 0 or ph <= 0:
            continue
        for i, e in enumerate(p["elements"]):
            if e.get("kind") not in ("text", "formula"):
                continue
            t = (e.get("text") or "").strip()
            if not t or not NUMERAL.fullmatch(t):
                continue
            x0, y0, x1, y1 = e["bbox"]
            if (x1 - x0) > pw * FOLIO_MAX_W:
                continue
            if y1 < ph * PAGE_EDGE_BAND or y0 > ph * (1 - PAGE_EDGE_BAND):
                out.append(f"P0 folio    {p['page']}#{i} x={x0:.0f} y={y0:.0f} {t!r}"
                           f" -- untranslated page number; run fix_pagenums.py")
    return out


def check_gutter_missed(model: dict) -> list[str]:
    """Two columns that `_detect_gutter` failed to see.

    The detector's vertical test rejects any page whose right column spans
    under 60% of the height -- which is exactly an article's first page, where
    the masthead pushes the columns into the lower half. Miss it and the whole
    page runs on one cursor, every left-column block is widened to the full
    text measure, and the page collapses into a single tall run.

    Independent rule: a real column shares a vertical band with the other one.
    A single-column page's right-of-centre lines sit above or below the body,
    never alongside it.
    """
    if _detect_gutter is None:
        return []
    out = []
    for p in model["pages"]:
        pw, ph = stem(p)
        els = live(p)
        if _detect_gutter(pw, ph, p["elements"]) is not None:
            continue
        left = [e for e in els if e["bbox"][0] <= pw * 0.45]
        right = [e for e in els if e["bbox"][0] > pw * 0.45]
        if len(left) < 2 or len(right) < 2:
            continue
        r_top = min(e["bbox"][1] for e in right)
        r_bot = max(e["bbox"][3] for e in right)
        l_top = min(e["bbox"][1] for e in left)
        l_bot = max(e["bbox"][3] for e in left)
        span = r_bot - r_top
        if span <= 0:
            continue
        ov = min(r_bot, l_bot) - max(r_top, l_top)
        if ov >= span * OVERLAP_MIN:
            out.append(f"P0 gutter   page {p['page']}: right column spans only"
                       f" {span / ph:.0%} of the page but overlaps the left"
                       f" column {ov / span:.0%} -- two columns, read as one")
    return out


def check_column_collapse(model: dict) -> list[str]:
    """Left-column boxes stretching past the gutter, or full-width blocks.

    With the gutter known, a left-column body has no business reaching into the
    right column. Two or more such boxes on one page means the widen pass ran
    without the gutter cap.
    """
    if _detect_gutter is None:
        return []
    out = []
    for p in model["pages"]:
        pw, ph = stem(p)
        g = _detect_gutter(pw, ph, p["elements"])
        if g is None:
            continue
        over, wide = [], []
        for i, e in enumerate(live(p)):
            if e.get("kind") != "text":
                continue
            x0, _, x1, _ = e["bbox"]
            if x0 < g and x1 > g + COLUMN_OVER_TOL:
                over.append(f"{p['page']}#{i}({x1 - x0:.0f}pt)")
            if (x1 - x0) > pw * WIDE_BLOCK:
                wide.append(f"{p['page']}#{i}({(x1 - x0) / pw:.0%})")
        if len(over) >= 2:
            out.append(f"P0 collapse page {p['page']}: {len(over)} left-column"
                       f" blocks cross the gutter at {g:.0f}pt -- {', '.join(over[:4])}")
        if len(wide) > max(1, len(live(p)) * CROSS_COLUMN_MAX):
            out.append(f"P0 collapse page {p['page']}: {len(wide)} text blocks wider"
                       f" than {WIDE_BLOCK:.0%} of the page -- {', '.join(wide[:4])}")
    return out


def check_uncropped_scan(model: dict) -> list[str]:
    """A full-page raster the crop step did not shrink.

    Pages 2+ are described completely by the text layer, so their raster is
    duplicate weight and paints an untranslated original above every page.
    """
    out = []
    for p in model["pages"]:
        pw, ph = stem(p)
        if pw <= 0 or ph <= 0:
            continue
        for i, e in enumerate(p["elements"]):
            if e.get("kind") != "image" or e.get("_skip"):
                continue
            x0, y0, x1, y1 = e["bbox"]
            h = y1 - y0
            if h >= ph * 0.80:
                out.append(f"P0 scan     page {p['page']}#{i}: raster kept at"
                           f" {h:.0f}pt of {ph:.0f}pt -- run crop_scan_cover.py")
    return out


def check_translations(model: dict, zh: dict | None) -> list[str]:
    if zh is None:
        return []
    out = []
    for p in model["pages"]:
        for i, e in enumerate(p["elements"]):
            if e.get("kind") != "text":
                continue
            sents = [s for s in (e.get("sentences") or []) if (s or "").strip()]
            if not sents:
                continue
            sid = f"{p['page']}#{i}"
            entry = zh.get(sid)
            if entry is None:
                if NUMERAL.fullmatch((e.get("text") or "").strip()):
                    continue
                out.append(f"P0 zh       {sid}: no translation for {len(sents)} sentence(s)")
            elif len(entry) != len(sents):
                out.append(f"P0 zh       {sid}: {len(entry)} translations for"
                           f" {len(sents)} sentences")
    return out


# --------------------------------------------------------------------------
# P1 -- almost certainly a defect
# --------------------------------------------------------------------------
def check_repeated_header(model: dict) -> list[str]:
    """A phrase repeating at the start of elements across many pages.

    Running heads are journal furniture, not prose, and the OCR fuses them into
    whichever body element shares their line. Finding them by repetition rather
    than by matching a known journal name is what makes this work on any scan:
    a body sentence does not begin identically on three different pages.
    """
    seen: dict[str, list[str]] = defaultdict(list)
    for p in model["pages"]:
        for i, e in enumerate(live(p)):
            if e.get("kind") != "text":
                continue
            head = re.sub(r"\d+", "#", (e.get("text") or "")[:HEADER_PREFIX]).strip()
            head = re.sub(r"\s+", " ", head)
            if len(head) < 12:
                continue
            seen[head].append(f"{p['page']}#{i}")
    out = []
    for head, where in seen.items():
        if len(where) >= HEADER_MIN_PAGES:
            out.append(f"P1 header   {len(where)}x {head[:44]!r} -- {', '.join(where[:5])}")
    return out


def check_double_stem(model: dict) -> list[str]:
    out = []
    for p in model["pages"]:
        for i, e in enumerate(live(p)):
            for m in DOUBLE_STEM.finditer(e.get("text") or ""):
                out.append(f"P1 fusion   {p['page']}#{i} {m.group(0)!r} --"
                           f" run fix_ocr_fusions.py")
    return out


def check_orphan_tail(model: dict) -> list[str]:
    """An element whose first sentence starts lower-case.

    Either the gutter or a page break cut a sentence, leaving the tail alone at
    the top of the next column. Not always wrong (an abbreviation can end a
    sentence), which is why it is P1 and wants a human look.
    """
    out = []
    for p in model["pages"]:
        for i, e in enumerate(live(p)):
            if e.get("kind") != "text":
                continue
            sents = [s for s in (e.get("sentences") or []) if (s or "").strip()]
            if not sents:
                continue
            first = sents[0].lstrip()
            if first[:1].islower():
                out.append(f"P1 orphan   {p['page']}#{i} {first[:52]!r}")
    return out


# --------------------------------------------------------------------------
# P2 -- hints
# --------------------------------------------------------------------------
def check_retained_scan(model: dict) -> list[str]:
    out = []
    for p in model["pages"]:
        for i, e in enumerate(p["elements"]):
            if e.get("kind") != "image" or e.get("_skip"):
                continue
            x0, y0, x1, y1 = e["bbox"]
            out.append(f"P2 cover    page {p['page']}#{i}: raster kept"
                       f" ({x1 - x0:.0f}x{y1 - y0:.0f}pt) -- this area has no"
                       f" translation; confirm with the user that is intended")
    return out


def check_scrambled_caps(model: dict) -> list[str]:
    """A run of capitals mid-sentence: a drop cap displaced by reflow.

    The small-caps remnant of a drop cap sits slightly lower than its line, so
    the paragraph reflow can place it after the words that followed it:
    "it was easier being TWENTY YEARS AGO, a student of macroeconomics."
    """
    out = []
    for p in model["pages"]:
        for i, e in enumerate(live(p)):
            if e.get("kind") != "text":
                continue
            for sent in (e.get("sentences") or []):
                s = (sent or "").strip()
                if len(s) < 20:
                    continue
                for m in CAPS_RUN.finditer(s):
                    pos = m.start() / len(s)
                    if 0.15 < pos < 0.85:
                        out.append(f"P2 caps     {p['page']}#{i} {m.group(0)!r}"
                                   f" at {pos:.0%} of the sentence")
    return out


def check_slots(model: dict) -> list[str]:
    """Two elements painted on the same spot.

    The OCR layer occasionally reads one line twice and classifies the copies
    differently -- the page-1 folio arrives as both a "formula" and a "text",
    and the reference entry "1-23." does the same on the last page. Nothing
    visually betrays it, because the copies coincide, but it means duplicated
    content in the model and a doubled element in the output.

    Emptied elements are NOT reported here. The model keeps their original
    bbox after a repair clears their text (so span ids stay stable), and
    `build_html.relayout_page` is what collapses them to zero height. Judging
    that from the model would flag every stripped folio; the render-time check
    in verify_html.js is the one that can tell.
    """
    out = []
    for p in model["pages"]:
        spots: dict[tuple[float, float], list[str]] = defaultdict(list)
        for i, e in enumerate(p["elements"]):
            if e.get("kind") not in ("text", "formula"):
                continue
            if not ((e.get("text") or "").strip()
                    or any((s or "").strip() for s in (e.get("sentences") or []))):
                continue
            spots[(round(e["bbox"][0], 0), round(e["bbox"][1], 0))].append(
                f"{i}({e['kind']}:{(e.get('text') or '').strip()[:18]!r})")
        for (x, y), who in spots.items():
            if len(who) > 1:
                out.append(f"P2 slot     page {p['page']} at ({x:.0f},{y:.0f}):"
                           f" {len(who)} coincident elements -- {' '.join(who)}")
    return out


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model")
    ap.add_argument("-z", "--zh", help="translation file (span id -> [text])")
    ap.add_argument("--only", help="comma-separated severities, default P0,P1,P2")
    ap.add_argument("--quiet", action="store_true", help="print counts only")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)
    zh = None
    if args.zh:
        with open(args.zh, encoding="utf-8") as fh:
            zh = json.load(fh)

    findings = (
        check_folio(model)
        + check_gutter_missed(model)
        + check_column_collapse(model)
        + check_uncropped_scan(model)
        + check_translations(model, zh)
        + check_repeated_header(model)
        + check_double_stem(model)
        + check_orphan_tail(model)
        + check_retained_scan(model)
        + check_scrambled_caps(model)
        + check_slots(model)
    )

    want = {s.strip().upper() for s in (args.only or "P0,P1,P2").split(",")}
    findings = [f for f in findings if f[:2] in want]

    by = Counter(f[:2] for f in findings)
    if not args.quiet:
        for line in findings:
            print(line)
        print()
    print(f"audit: {len(model['pages'])} pages, "
          f"P0={by.get('P0', 0)} P1={by.get('P1', 0)} P2={by.get('P2', 0)}")
    if by.get("P0", 0):
        print("P0 findings block delivery.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
