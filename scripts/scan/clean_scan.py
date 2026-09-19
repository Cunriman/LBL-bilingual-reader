#!/usr/bin/env python3
"""
clean_scan.py -- repair OCR artefacts and drop non-article pages.

Two jobs, both specific to the Mankiw scan:

1. **Drop-cap repair.** A drop cap is extracted as its own tiny element whose
   text is a whole word minus its first letter, with the initial committed to a
   separate big-character element. After line/paragraph joining the fragment
   surfaces as `WENTY YEARS AGO` inside the running text. Common English
   sentence-openers with a swallowed initial are repaired by lookup.

2. **Non-article pages.** The JSTOR PDF ends with "LINKED CITATIONS" stub pages
   that carry no article prose (just URLs). They are detected and removed so
   they are not translated.

3. **OCR slips** that would otherwise reach the dictionary: `jinancial` ->
   `financial`, and similar ligature misreads (`fi` -> `ji`, `fl` -> `jl`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Drop-cap survivors: the fragment as OCR leaves it -> the real word.
DROPCAP_FIXES = {
    "WENTY": "TWENTY",
    "HIS": "THIS",
    "HAT": "THAT",
    "HE": "THE",
    "HESE": "THESE",
    "HOSE": "THOSE",
    "HERE": "THERE",
}

# A drop-cap paragraph opens with a short all-caps phrase a few points lower in
# the line box than the rest of the line, so a naive y-sort files it after the
# text it actually precedes. Move that phrase back to the front.
DROPCAP_REORDER = re.compile(
    r'^(?P<lead>[^"]{0,90}?)\s*'
    r'(?P<cap>"?[A-Z]{2,}(?:\s+[A-Z]{2,}){1,4}[,:.]?)'   # SHORT all-caps run
    r'\s+(?P<rest>.*)$',
    re.DOTALL,
)


def fix_dropcaps(text: str) -> str:
    """Repair the swallowed drop-cap initial.

    Position restoration for the one affected paragraph lives in
    `fix_dropcap.py`, which keys on the exact broken substring. A general
    reading-order heuristic here made things worse, so this stays narrow.
    """
    pattern = re.compile(
        r'\b(' + "|".join(sorted(DROPCAP_FIXES, key=len, reverse=True)) + r')\b'
        r'(?=\s+[A-Z]{2,})'
    )
    return pattern.sub(lambda mm: DROPCAP_FIXES[mm.group(1)], text)


# Whole-word OCR slips seen in this document.
WORD_FIXES = {
    "jinancial": "financial",
    "jinance": "finance",
    "jirm": "firm",
    "jirms": "firms",
    "jirst": "first",
    "jind": "find",
    "jixed": "fixed",
    "jlow": "flow",
    "jluctuations": "fluctuations",
    "Keyndsians": "Keynesians",
    "Keyndsian": "Keynesian",
}

# Page markers that mean "not article content".
DROP_PAGE_PATTERNS = [
    re.compile(r"LINKED CITATIONS", re.IGNORECASE),
    re.compile(r"^\s*http://www\.jstor\.org\s*$", re.IGNORECASE),
]


def fix_words(text: str) -> str:
    for bad, good in WORD_FIXES.items():
        text = re.sub(rf"\b{re.escape(bad)}\b", good, text, flags=re.IGNORECASE
                      if bad[0].islower() else 0)
    return text


def is_stub_page(page: dict) -> bool:
    texts = [e.get("text", "") for e in page["elements"] if e["kind"] == "text"]
    joined = "\n".join(texts)
    for pat in DROP_PAGE_PATTERNS:
        if pat.search(joined):
            return True
    # A page that is almost entirely URLs is a citation stub.
    if len(joined) > 200:
        urls = len(re.findall(r"https?://", joined))
        if urls >= 3 and urls * 60 > len(joined):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--drop-stub-pages", action="store_true")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    kept = []
    dropped = []
    for page in model["pages"]:
        if args.drop_stub_pages and is_stub_page(page):
            dropped.append(page["page"])
            continue
        for el in page["elements"]:
            if el["kind"] == "text":
                el["text"] = fix_words(fix_dropcaps(el["text"]))
                el["sentences"] = []
        kept.append(page)

    for i, page in enumerate(kept, start=1):
        page["page"] = i
        for j, el in enumerate(page["elements"]):
            el["_idx"] = j
    model["pages"] = kept
    model["page_count"] = len(kept)

    print(f"[clean] dropped stub pages: {dropped if dropped else 'none'}")
    print(f"[clean] pages kept: {len(kept)}")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
