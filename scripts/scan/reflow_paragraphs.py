#!/usr/bin/env python3
"""
reflow_paragraphs.py -- join per-line fragments into sentences/paragraphs.

`reflow_scan.py` fixes the *column order* of a scanned two-column page, but the
result is still one element per visual line:

    'a student of macroeconomics. Mac-'
    'roeconomists felt more sure of the an-'
    'swers they gave to questions such as,'

Translating line-by-line produces broken Chinese and a bilingual layout that is
painful to read. This pass glues the lines back into whole sentences, undoing
end-of-line hyphenation and protecting common abbreviations.

A line boundary is only a real sentence boundary when the line already ends in
terminal punctuation AND the next line starts like a new sentence (capital and
a plausible sentence opener). Otherwise the lines are one sentence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Abbreviations whose trailing period is not a sentence end.
ABBREV = {
    "vs", "e.g", "i.e", "cf", "al", "etc", "fig", "no", "vol", "pp", "ed",
    "eds", "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "ca", "approx",
    "resp", "sec", "ch", "chap", "art", "ref", "refs", "esp", "ibid",
}

TERMINAL = re.compile(r'[.!?]["\'\u201d\u2019)]*\s*$')


def _ends_sentence(text: str) -> bool:
    t = text.rstrip()
    if not TERMINAL.search(t):
        return False
    # Reject a trailing abbreviation such as "Vol." or "e.g."
    core = t.rstrip('"\'\u201d\u2019)').rstrip(".")
    words = core.split()
    if not words:
        return True
    return words[-1].lower() not in ABBREV


def _starts_sentence(text: str) -> bool:
    t = text.lstrip()
    if not t:
        return False
    if t[0] in '"\'(\u201c\u2018':
        t = t[1:].lstrip()
    return bool(t) and (t[0].isupper() or t[0].isdigit())


def _join(a: str, b: str) -> str:
    """Join two line fragments, undoing hyphenation when present."""
    a = a.rstrip()
    b = b.lstrip()
    if a.endswith("-") and re.search(r"[A-Za-z]-$", a) and re.match(r"[a-z]", b):
        return a[:-1] + b
    return (a + " " + b).strip()


def reflow(elements: list[dict]) -> list[dict]:
    out: list[dict] = []
    for el in elements:
        if el["kind"] != "text":
            out.append(el)
            continue
        if out:
            prev = out[-1]
            # Only continue a paragraph when the next line really follows on:
            #   * small vertical step -- a large one means a new block
            #     (title, footnote area, column break);
            #   * the two lines overlap horizontally -- lines in different
            #     columns must never be glued together.
            gap = el["bbox"][1] - prev["bbox"][3]
            same_block = gap <= 14.0
            x_overlap = min(prev["bbox"][2], el["bbox"][2]) - max(
                prev["bbox"][0], el["bbox"][0]
            )
            same_column = x_overlap > 20.0
            can_join = (
                prev["kind"] == "text"
                and not prev.get("_sealed")
                and not _ends_sentence(prev["text"])
                and same_block
                and same_column
            )
            if can_join:
                prev["text"] = _join(prev["text"], el["text"])
                pb, eb = prev["bbox"], el["bbox"]
                prev["bbox"] = [
                    min(pb[0], eb[0]), min(pb[1], eb[1]),
                    max(pb[2], eb[2]), max(pb[3], eb[3]),
                ]
                prev["sentences"] = []
                continue
        out.append(dict(el))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    before = sum(len(p["elements"]) for p in model["pages"])
    for page in model["pages"]:
        page["elements"] = reflow(page["elements"])
        for i, e in enumerate(page["elements"]):
            e["_idx"] = i
    after = sum(len(p["elements"]) for p in model["pages"])

    print(f"[paragraphs] elements {before} -> {after}")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
