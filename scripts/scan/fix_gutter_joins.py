#!/usr/bin/env python3
"""
fix_gutter_joins.py -- split words that a column-gutter split fused together.

On the reference-list page of the Mankiw scan, the two columns are so close
that a right-column word beginning a line got glued to the tail of the
left-column line before it:

    "... recent developments" + "monetary policy ..."  ->  "developtimal Policy"
    "... Rein" + "about and discuss ..."               ->  "Reinabout and discuss"

These fusions are visible as a lowercase-to-uppercase seam inside what should
be a single word, or as a nonsense token. Each one is repaired explicitly from
the known list -- guessing generally would corrupt legitimate words.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Exact fused token -> the correct rendering of both halves.
FUSIONS = {
    "developtimal": "developments Monetary",
    "reinabout": "Rein about",
    "agapplied": "aggregate applied",
    "macrpeconomists": "macroeconomists",
    "maceconomic": "Macroeconomic",
    "policyirrelevance": "policy-irrelevance",
    "policy-irrelevance": "policy-irrelevance",
    "output-inflation": "Output-Inflation",
    "jean-pascal": "Jean-Pascal",
    "regress-that": "regress -- that",
    "name-that": "name -- that",
    "assumes-and": "assumes -- and",
    "politics-specifically": "politics -- specifically",
    # OCR roman-numeral misreads of the journal volume.
    "xxvzzz": "XXXVIII", "xxvizi": "XXXVIII", "xxvlil": "XXVIII",
    "xxvziz": "XXVIII", "xxvziz.": "XXVIII.", "xxvzzz.": "XXXVIII.",
    # Leading letter lost to a drop cap.
    "acroeconometric": "macroeconometric",
    "acroeconomists": "macroeconomists",
    "acroeconomics": "macroeconomics",
    "acroeconomist": "macroeconomist",
    # OCR letter confusion.
    "unernployment": "unemployment",
    "hercowitz": "Hercowitz",
    "malinvaud": "Malinvaud",
    # Reference-list OCR damage (page 15).
    "joiinand": "JOHN AND",
    "ofunemployment": "of unemployment",
    "macmilployment": "macmil-",
    "butkiewicz": "Butkiewicz",
    "kenuetii": "Kenneth",
    "kennetii": "Kenneth",
    "gahtii": "Garth",
    "saloner": "Saloner",
    "rotemberg": "Rotemberg",
    "ectations": "expectations",
    "carnegie-rochester": "Carnegie-Rochester",
    "agapplied": "aggregate applied",
    "roconomics": "macroeconomics",
}

# `lowercaseUPPERCASE` seam inside an alphabetic run is a gutter fusion.
SEAM = re.compile(r"\b([a-z]{3,})([A-Z][a-z]{2,})\b")
KNOWN_SEAMS = {
    ("develop", "Monetary"): ("developments", "Monetary"),
}


def fix(text: str) -> str:
    for bad, good in FUSIONS.items():
        # Reference lists are set in small caps, so the broken token may appear
        # as JOIINAND / Joiinand / joiinand. Match case-insensitively and, when
        # the source was all-caps, emit the correction in caps too.
        def repl(m: re.Match, _good: str = good) -> str:
            if m.group(0).isupper() and not _good.isupper():
                return _good.upper()
            return _good

        text = re.sub(rf"\b{re.escape(bad)}\b", repl, text, flags=re.IGNORECASE)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    hits: list[str] = []
    for page in model["pages"]:
        for el in page["elements"]:
            if el["kind"] != "text":
                continue
            new = fix(el["text"])
            if new != el["text"]:
                hits.append(new[:70])
                el["text"] = new
    print(f"[gutter] elements repaired: {len(hits)}")
    for h in hits[:8]:
        print(f"   {h}")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
