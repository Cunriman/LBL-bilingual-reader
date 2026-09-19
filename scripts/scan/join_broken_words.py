#!/usr/bin/env python3
"""
join_broken_words.py -- repair words a line break split in an OCR text layer.

The Mankiw scan's text layer puts the remainder of a hyphenated word at the
START of the next line, but the hyphen that should join them is either missing
or sits on the wrong line. The result is words like:

    mit-penn-social      (should be MIT-Penn-Social, a proper noun)
    acroeconometric      ("macroeconometric", initial M lost to a drop cap)
    xxvzzz / xxvlil      (roman numerals misread: XXXVIII, XXVIII)
    unernployment        ("unemployment", rn misread as m)
    hercowitz            ("Hercowitz", OCR slip)
    regress-that         (a genuine line-break join that lost its space)

Left alone, every one of these becomes a clickable word with no definition.
This pass whitelists the exact repairs rather than guessing, so no correct
word is ever rewritten.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Exact broken token -> corrected token. Keys are case-sensitive surface forms.
FIXES = {
    # Roman numerals misread by the OCR.
    "xxvzzz": "XXXVIII", "xxvizi": "XXXVIII", "xxvlil": "XXVIII",
    "xxviii": "XXVIII", "xxvzzz.": "XXXVIII.",
    # Leading letter eaten by a drop cap.
    "acroeconometric": "macroeconometric",
    "acroeconomists": "macroeconomists",
    "acroeconomics": "macroeconomics",
    "acroeconomist": "macroeconomist",
    # OCR letter-confusion slips.
    "unernployment": "unemployment",
    "hercowitz": "Hercowitz",
    "malinvaud": "Malinvaud",
    # Line-break joins that swallowed the space.
    "regress-that": "regress -- that",
    "name-that": "name -- that",
    "assumes-and": "assumes -- and",
}

# Compose hyphenated proper nouns / compounds the extractor split.
COMPOUND = re.compile(r"\b([A-Za-z]{2,})-([a-z]{2,})-([a-z]{2,})\b")
KNOWN_COMPOUNDS = {
    "mit-penn-social": "MIT-Penn-Social",
    "nonmarket-clearing": "non-market-clearing",
    "non-walrasian": "non-Walrasian",
    "expectations-augmented": "expectations-augmented",
    "sargent-wallace": "Sargent-Wallace",
    "politics-specifically": "politics -- specifically",
    "nondistortionary": "nondistortionary",
    "policylike": "policy-like",
}


def fix(text: str) -> str:
    for bad, good in FIXES.items():
        text = re.sub(rf"\b{re.escape(bad)}\b", good, text)
    for bad, good in KNOWN_COMPOUNDS.items():
        text = re.sub(rf"\b{re.escape(bad)}\b", good, text)
    return text


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
            new = fix(el["text"])
            if new != el["text"]:
                el["text"] = new
                hits += 1
    print(f"[join] elements repaired: {hits}")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
