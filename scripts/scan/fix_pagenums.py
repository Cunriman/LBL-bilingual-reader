#!/usr/bin/env python
"""Strip the running page numbers off every page edge.

A JSTOR scan carries the journal's folio number twice per page: once in the
text layer, where OCR reads it as a lone numeral. On the Mankiw scan that is
"1645".."1660", the article's page range in JEL 28(4).

Two things make these worth removing rather than leaving as harmless residue:

  * They are the only untranslated text on the page. `build_html.py` already
    exempts a bare numeral from the coverage rule, so it renders as a naked
    number with no Chinese under it -- exactly the "why is this bit not
    translated" a reader notices.

  * They corrupt the column split. `_detect_gutter` locates the right-hand
    column by the leftmost x of the right side, and the page-1 folio sits at
    x=241, *inside* the left column's x-range but right of the 45% centre
    line. Left in place it drags the gutter from 261 to 241, every left-column
    block then appears to straddle the gutter, and the entire page collapses
    from two columns into one tall run. See _detect_gutter in build_html.py.

Element slots are kept (text and sentences emptied) so that every span id
stays stable and the translation file needs no renumbering.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# A page-edge numeral: short, digit-only, and parked in the top or bottom
# margin band. Both bounds matter -- the 45% centre test alone would catch
# table cells and "1990" in a running head's "December 1990".
NUMERAL = re.compile(r"\d{1,4}")
EDGE_TOP = 0.12          # bbox bottom above this share of page height
EDGE_BOTTOM = 0.90       # bbox top below this share of page height
MAX_WIDTH_SHARE = 0.12   # a folio is narrow; a data row is not


def is_page_number(el: dict, page_w: float, page_h: float) -> bool:
    if el.get("kind") not in ("text", "formula"):
        return False
    text = (el.get("text") or "").strip()
    if not text or not NUMERAL.fullmatch(text):
        return False
    x0, y0, x1, y1 = el["bbox"]
    if (x1 - x0) > page_w * MAX_WIDTH_SHARE:
        return False
    if y1 < page_h * EDGE_TOP:
        return True
    if y0 > page_h * EDGE_BOTTOM:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("-z", "--zh")
    ap.add_argument("-zout", "--zh-out")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    zh = None
    if args.zh:
        with open(args.zh, encoding="utf-8") as fh:
            zh = json.load(fh)

    hit: list[str] = []
    for page in model["pages"]:
        pno, pw, ph = page["page"], page["width"], page["height"]
        for idx, el in enumerate(page["elements"]):
            if not is_page_number(el, pw, ph):
                continue
            sid = f"{pno}#{idx}"
            hit.append(f"{sid} {el['bbox'][0]:.0f},{el['bbox'][1]:.0f}"
                       f" {el.get('kind')} {el.get('text','').strip()!r}")
            if zh is not None:
                zh.pop(sid, None)
            el["text"] = ""
            el["sentences"] = []

    if args.report:
        for line in hit:
            print("  " + line)
        print(f"stripped {len(hit)} page-edge numerals")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    if zh is not None and args.zh_out:
        with open(args.zh_out, "w", encoding="utf-8") as fh:
            json.dump(zh, fh, ensure_ascii=False)

    # Self-check: none may survive.
    left = [
        f"{p['page']}#{i}"
        for p in model["pages"]
        for i, e in enumerate(p["elements"])
        if is_page_number(e, p["width"], p["height"])
    ]
    if left:
        print(f"ERROR: {len(left)} page numbers survived: {left[:5]}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
