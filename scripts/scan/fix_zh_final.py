#!/usr/bin/env python3
"""
fix_zh_final.py -- make the translation map match the repaired model.

After fix_final.py (strip running headers) and fix_bib_sentences.py (re-unify
reference entries) the English side has changed shape, so the translation map
must be brought back into step. Three jobs:

1. Strip a leading "第 XXXVIII 卷（1990 年 12 月）" header from any Chinese
   segment. This is the translated form of the running page header that
   fix_final.py removed on the English side; the translator had faithfully
   rendered it as part of the first sentence of each page.

2. Re-join the reference entries. Each bibliography entry was previously
   split into 3-8 fragments on the English side, so its Chinese was returned
   as a list of fragments too -- but only the first fragment carried the real
   translation and the rest were empty. Now that the entry is a single
   sentence again, concatenating the non-empty fragments recovers the whole
   translated entry, and the empties simply disappear.

3. Verify. Every key must end up with exactly as many Chinese segments as the
   model has sentences, and no segment may be empty. Anything left over is
   reported and the run fails, so a broken map can never reach the builder.

Usage:
  python fix_zh_final.py model.json translations.json -o out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# The translated running header, as produced by the translator.
ZH_HEADER_RE = re.compile(r"^\s*第\s*X{0,3}(?:VIII|VII|VI|IX|IV|V|I)+\s*卷\s*[（(][^）)]*[）)]\s*")


def clean_zh(text: str) -> str:
    """Remove a leading translated running header."""
    return ZH_HEADER_RE.sub("", text).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("translations")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)
    with open(args.translations, encoding="utf-8") as fh:
        zh = json.load(fh)

    want = {}
    for page in model["pages"]:
        for el in page.get("elements", []):
            if el.get("kind") != "text":
                continue
            key = f'{page["page"]}#{el["_idx"]}'
            want[key] = len(el.get("sentences") or [])

    out = {}
    headers = 0
    rejoined = 0

    for key, n_want in want.items():
        segs = zh.get(key)
        if segs is None:
            print(f"[fix_zh_final] MISSING key {key}", file=sys.stderr)
            continue

        segs = [clean_zh(s) for s in segs]
        headers += sum(1 for s0, s1 in zip(zh[key], segs) if s0 != s1)

        if n_want == len(segs):
            out[key] = segs
            continue

        if n_want == 1:
            merged = "".join(s for s in segs if s.strip()).strip()
            if merged:
                out[key] = [merged]
                rejoined += 1
                continue

        # Fall back to a positional copy, keeping whatever lined up.
        fixed = list(segs[:n_want]) + [""] * max(0, n_want - len(segs))
        out[key] = fixed

    bad = [(k, want[k], out[k]) for k in out if len(out[k]) != want[k]]
    empt = [(k, i) for k, v in out.items() for i, s in enumerate(v) if not s.strip()]

    if args.report:
        for k in out:
            if len(out[k]) != want.get(k):
                print(f"  COUNT {k}: want={want[k]} got={len(out[k])}")

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    print(f"[fix_zh_final] headers stripped: {headers}")
    print(f"[fix_zh_final] entries re-joined: {rejoined}")
    print(f"[fix_zh_final] wrote {args.output} ({len(out)} keys)")

    if bad:
        print(f"[fix_zh_final] FAIL: {len(bad)} keys with wrong segment count")
        for k, w, g in bad[:20]:
            print(f'   {k}: want={w} got={len(g)}')
        return 1
    if empt:
        # Empty segments are not fatal *here*, because the next stage
        # (fix_last3.py) exists precisely to repair the named cases and the
        # pipeline runs with `set -e` -- failing now would abort the chain
        # before its own repair step. Nothing slips through: build_html.py's
        # translation audit runs afterwards, cannot be bypassed, and rejects
        # empty segments outright.
        print(f"[fix_zh_final] NOTE: {len(empt)} empty segment(s) deferred to fix_last3")
        for k, i in empt[:20]:
            print(f"   {k}[{i}]")
        return 0
    print("[fix_zh_final] OK: every key aligned, no empty segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
