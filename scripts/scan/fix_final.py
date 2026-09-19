#!/usr/bin/env python3
"""
fix_final.py -- last two repairs before the Mankiw HTML build.

Both defects were caught by build_html.py's translation audit, and both are
consequences of the reflow stages rather than of the translation itself.

1. Running page header bleeding into the body text.
   The scan carries a header on every page ("Vol. XXXVIII (December 1990)").
   Because it sits directly above the first body line and shares its column,
   reflow_paragraphs.py glued it to the first sentence of the page:

     "Vol. XXXVIII (December 1990) Approximately five centuries ago, ..."

   The header is not part of the article prose, so it is trimmed from the
   head of any element whose text starts with that pattern. Stage 5 of the
   pipeline removes the bare header blocks; this removes the fused ones.

2. The reference page's right column is interleaved with stray page-header
   and page-number fragments, which the line merger swept into the first
   bibliography entry:

     "Vol. XXVIII (December 1990) Quart. J. Econ., May 1985, ..."

   Trimming the same header pattern here restores the entry to
   "Quart. J. Econ., May 1985, 100(2), pp. 52-38."

Both fixes are pure deletions of a recognisable header string, so they are
safe to re-run and cannot damage the surrounding prose.
"""

from __future__ import annotations

import argparse
import json
import re
import sys


# "Vol. XXXVIII (December 1990)" / "Vol. XXVIII (December 1990)" -- the OCR
# reads the volume as either XXXVIII or XXVIII depending on the page.
HEADER_RE = re.compile(
    r"^\s*Vol\.\s+X{0,3}(?:VIII|VII|VI|IX|IV|V|I)+\s*\(December\s+1990\)\s*",
    re.IGNORECASE,
)


def split_sentences(text: str) -> list[str]:
    """Split on sentence-final punctuation, then verify the count matches.

    build_html.py recomputes sentences from this function, so the translation
    map is keyed against it. Reusing the same rule keeps the two in step.
    """
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def fix_text(text: str) -> tuple[str, bool]:
    """Strip a leading running header. Returns (new_text, changed)."""
    m = HEADER_RE.match(text)
    if not m:
        return text, False
    trimmed = text[m.end():].lstrip()
    return trimmed, trimmed != text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    hits = 0
    for page in model["pages"]:
        for el in page.get("elements", []):
            text = el.get("text") or ""
            new_text, changed = fix_text(text)
            if not changed:
                continue
            hits += 1
            if args.report:
                print(f'  {page["page"]}#{el["_idx"]}:')
                print(f'     - {text[:110]}')
                print(f'     + {new_text[:110]}')
            el["text"] = new_text
            el["sentences"] = split_sentences(new_text)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False, indent=1)

    print(f"[fix_final] headers stripped: {hits}")
    print(f"[fix_final] wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
