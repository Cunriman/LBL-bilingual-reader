#!/usr/bin/env python3
"""
fix_dropcap.py -- one targeted repair for the Mankiw scan's opening paragraph.

The article opens with a drop cap:

        T wenty years ago, it was easier being a student of macroeconomics.

The OCR text layer lost the large `T` glyph entirely and emitted the remainder
as `WENTY YEARS AGO,` sitting a few points BELOW the line it belongs to. Two
consequences:

  * the phrase is spelled `WENTY`, and
  * because element order is by y, it landed AFTER the line it introduces,
    giving "Introduction it was easier being WENTY YEARS AGO, a student ...".

Rather than guess at reading order in general, this script applies the single
exact repair, keyed on the precise broken substring. It is deliberately narrow:
general-purpose heuristics proved to make this worse, not better.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

BROKEN = re.compile(
    r'Introduction\s+it\s+was\s+easier\s+being\s+TWENTY\s+YEARS\s+AGO,\s+'
    r'a\s+student\s+of\s+macroeconomics\.'
)
FIXED = (
    'Introduction TWENTY YEARS AGO, it was easier being a student of '
    'macroeconomics.'
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    hits = 0
    for page in model["pages"]:
        for el in page["elements"]:
            if el["kind"] != "text":
                continue
            new, n = BROKEN.subn(FIXED, el["text"])
            if n:
                el["text"] = new
                el["sentences"] = []
                hits += n

    # Any remaining `WENTY` is the same drop-cap loss elsewhere.
    for page in model["pages"]:
        for el in page["elements"]:
            if el["kind"] == "text" and re.search(r"\bWENTY\b", el["text"]):
                el["text"] = re.sub(r"\bWENTY\b", "TWENTY", el["text"])
                el["sentences"] = []
                hits += 1

    print(f"[dropcap] repairs applied: {hits}")
    if hits == 0:
        print("[dropcap] WARNING: expected pattern not found")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
