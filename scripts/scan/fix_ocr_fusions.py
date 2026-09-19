#!/usr/bin/env python3
"""Repair OCR artefacts that no earlier stage catches.

Two classes of damage, both present in the Mankiw scan:

  * A doubled macro/micro stem. An earlier pass expanded clipped words --
    "acroeconomics" -> "macroeconomics", "roconomics" -> "macroeconomics" -- but
    its pattern was not anchored to a word boundary. "macroeconomics" itself
    contains "acroeconomics", so the pass fired a second time inside the
    already-correct word and produced "macmacroeconomics"; the same misfire
    turned "microeconomic" into "micmacroeconomic". 57 occurrences over the
    article. Dropping the second stem is exactly what restores the word:
    mac|macroeconomics -> macroeconomics, mic|macroeconomic -> microeconomic.

    The general lesson is recorded here rather than only in the fixer: a
    substitution table for clipped words must anchor with \\b on BOTH sides.
    Unanchored tables are silent -- they corrupt the very words they repair.

  * Plain OCR misspellings, kept as an explicit list. These are matched by
    nothing structural, so they are enumerated instead of guessed at. Running
    the repair over the same file twice is a no-op.

Apply this AFTER the sentence-splitting pass and before build_html.py, because
the repair is word-internal and leaves every sentence count unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# A macro/micro stem written twice in a row, immediately before "ro".
DOUBLED_STEM = re.compile(r"\b(mac|mic)(mac|mic)(?=ro)", re.IGNORECASE)

TYPO_FIXES = {
    "inconsistentcy": "inconsistency",
}

# Every form the doubled-stem rule is expected to find. Used only for the
# before/after report, so a future scan regression is visible in the output.
WATCH = ("macmacro", "micmacro", "Macmacro", "Micmacro")


def fix(text: str) -> str:
    # Case is preserved by keeping the first stem exactly as it appeared.
    text = DOUBLED_STEM.sub(lambda m: m.group(1), text)
    for bad, good in TYPO_FIXES.items():
        def repl(m: re.Match, _good: str = good) -> str:
            if m.group(0).isupper() and not _good.isupper():
                return _good.upper()
            return _good

        text = re.sub(rf"\b{re.escape(bad)}\b", repl, text, flags=re.IGNORECASE)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    before = {w: 0 for w in WATCH}
    after = {w: 0 for w in WATCH}
    changed = 0
    samples: list[str] = []

    for page in model["pages"]:
        for el in page["elements"]:
            if el["kind"] != "text":
                continue
            text = el.get("text") or ""
            for w in WATCH:
                before[w] += text.count(w)
            new = fix(text)
            for w in WATCH:
                after[w] += new.count(w)
            if new != text:
                changed += 1
                if args.report and len(samples) < 6:
                    samples.append(new[:80])
                el["text"] = new
            # Keep sentences in step: the repair never moves a boundary, but a
            # stale copy would still be shipped to the builder.
            sents = el.get("sentences")
            if sents:
                el["sentences"] = [fix(s) for s in sents]

    leftover = sum(after.values())
    print(f"[fusions] elements repaired: {changed}")
    if args.report:
        for s in samples:
            print(f"   {s}")
    print(f"[fusions] doubled stems before: {sum(before.values())} -> after: {leftover}")
    if leftover:
        sys.stderr.write("[fusions] FAIL: doubled stems survive the repair\n")
        return 1

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
