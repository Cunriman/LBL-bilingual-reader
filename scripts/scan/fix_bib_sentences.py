#!/usr/bin/env python3
"""
fix_bib_sentences.py -- keep bibliography entries as single translation units.

The reference list at the end of the article is full of journal abbreviations
("Quart. J. Econ.", "Amer. Econ. Rev.", "J. Polit. Econ."). sentence splitting
on ".<space>" therefore tears one entry into 3-6 bogus "sentences", e.g.

    "1660 Journal of Economic Literature, icy,\" Amer. Econ. Rev., Mar. 1968,
     58, pp. 1-17."
  -> ['1660 Journal of Economic Literature, icy,\" Amer.',
      'Econ.',
      'Rev., Mar.',
      '1968, 58, pp. 1-17.']

Those fragments are not sentences in any language, and no translator can
render them -- which is why the translation audit reported "译文为空".

Meanwhile the *Chinese* for these entries is correct and complete: each entry
was translated as one unit. The fault is purely on the English side.

Detecting "is this a bibliography entry" by abbreviation density turned out to
be unreliable, because the same abbreviatons appear in ordinary footnotes in
the article body. The reliable signal is structural: in a two-column academic
journal, the reference list is a run of full-width entries filling the whole
page, whereas every other text element in this document is either a single
body line or a narrow footnote fragment.

So this stage requires BOTH:
  * width >= 60% of the page's text column, and
  * an explicit --page argument naming the reference page(s),
and then rewrites each element's `sentences` to a single whole-entry unit.

The page number is passed in rather than inferred, so the transformation can
never silently reflow prose elsewhere in the document.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument(
        "--page",
        type=int,
        action="append",
        required=True,
        dest="pages",
        help="page number(s) holding the reference list (1-based, repeatable)",
    )
    ap.add_argument(
        "--min-width-frac",
        type=float,
        default=0.60,
        help="min element width as a fraction of its column (default 0.60)",
    )
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    targets = set(args.pages)
    merged = 0

    for page in model["pages"]:
        pno = page["page"]
        if pno not in targets:
            continue

        pw = float(page.get("width") or 0.0)
        # In a two-column layout the usable column is roughly half the page,
        # minus the outer margins. A full entry spans (almost) all of it.
        col_w = pw / 2.0
        min_w = col_w * args.min_width_frac

        for el in page.get("elements", []):
            if el.get("kind") != "text":
                continue
            sents = el.get("sentences") or []
            if len(sents) < 2:
                continue
            bbox = el.get("bbox") or [0, 0, 0, 0]
            width = float(bbox[2]) - float(bbox[0])
            if width < min_w:
                continue

            text = (el.get("text") or "").strip()
            if not text:
                continue

            if args.report:
                print(f'  {pno}#{el["_idx"]}: {len(sents)} -> 1  (w={width:.0f})')
                print(f'      {text[:100]}')

            el["sentences"] = [text]
            merged += 1

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False, indent=1)

    print(f"[fix_bib_sentences] entries re-unified: {merged}")
    print(f"[fix_bib_sentences] wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
