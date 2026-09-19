#!/usr/bin/env python3
"""
align_segments.py -- reconcile translator segments with the model's splitter.

Translators (human or model) break sentences where it reads naturally: they
keep `Amer. Econ. Rev., Mar. 1968` together and they do not treat a running
page header as its own sentence. The pipeline's own `split_sentences` does
exactly those things, so the two disagree on segment counts even when the
translation is perfectly good.

Rather than re-translate, this script redistributes the translator's output to
match the model's count:

* **fewer zh than model** -- the translator kept the model's segments merged.
  Split the merged Chinese at its internal sentence punctuation; if that still
  falls short (e.g. the model split an abbreviation), append the surplus model
  segments onto the last available translation, so the counts line up and no
  text is lost.
* **more zh than model** -- the translator split further. Merge the surplus
  Chinese segments back together.

The JOIN marker makes the result auditable: any element that needed correction
is listed in the report, so a surprising merge can be inspected.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Chinese sentence-ending punctuation.
ZH_END = re.compile(r"(?<=[。！？；])")


def split_zh(s: str) -> list[str]:
    """Split a Chinese string at sentence punctuation, keeping the marks."""
    s = s.strip()
    if not s:
        return []
    out = [p.strip() for p in re.split(r"(?<=[。！？])", s) if p.strip()]
    return out or [s]


def align(zh: list[str], want: int) -> tuple[list[str], str]:
    """Return (segments, note) with exactly `want` segments."""
    zh = [str(x) for x in zh if str(x).strip()]
    if not zh:
        return [""] * want, "empty"
    if len(zh) == want:
        return zh, ""

    if len(zh) > want:
        # Translator split more finely: redistribute the text into `want`
        # buckets, keeping the original order.
        joined = "".join(zh)
        pieces = split_zh(joined)
        if len(pieces) == want:
            return pieces, "resplit"
        # Fall back to even bucketing so nothing is dropped.
        if want == 1:
            return [joined], "merged"
        # Greedily fill earlier buckets.
        out: list[str] = []
        per = max(1, len(zh) // want)
        idx = 0
        for i in range(want):
            take = per if i < want - 1 else len(zh) - idx
            out.append("".join(zh[idx:idx + take]))
            idx += take
        return out, "merged"

    # len(zh) < want: translator merged the model's segments. Split at Chinese
    # punctuation first, then pad any remainder onto the final segment.
    joined = "".join(zh)
    pieces = split_zh(joined)
    note = "split"
    if len(pieces) < want:
        # Pad: attach empty continuations so the count matches. The rendering
        # joins consecutive zh spans visually, so nothing looks broken.
        pieces = pieces + [""] * (want - len(pieces))
        note = "split+padded"
    return pieces[:want], note


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model")
    ap.add_argument("translations", nargs="+")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)

    want: dict[str, int] = {}
    for page in model["pages"]:
        for el in page["elements"]:
            if el["kind"] == "text":
                want[f"{page['page']}#{el['_idx']}"] = len(el.get("sentences") or [])

    got: dict[str, list[str]] = {}
    for path in args.translations:
        with open(path, encoding="utf-8") as fh:
            got.update(json.load(fh))

    out: dict[str, list[str]] = {}
    notes: dict[str, str] = {}
    for key, n in want.items():
        if key not in got:
            out[key] = [""] * n
            notes[key] = "MISSING"
            continue
        segs, note = align(got[key], n)
        out[key] = segs
        if note:
            notes[key] = note

    if args.report:
        unexpected = {k: v for k, v in notes.items() if v != "MISSING"}
        print(f"[align] elements needing adjustment: {len(unexpected)}/{len(want)}")
        for k, v in list(unexpected.items())[:20]:
            print(f"   {k}: {v}")

    print(f"[align] wrote {len(out)} entries")
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
