#!/usr/bin/env python3
"""
fix_last3.py -- the three residual defects in Mankiw's translation map.

These survive the general passes because each is a one-off alignment quirk
rather than a class of error, so they are repaired by explicit rule instead of
by a broader heuristic that might damage neighbouring text.

10#8  The English sentence splitter sees "...a period of high unemployment and
      low income,." as sentence 1 and "it lowers the demand..." as sentence 2,
      but the translator attached "部门转移理论的倡导者认为，这类证据并不具有说服力。"
      to the second Chinese slot. The two are simply off by one. Dropping the
      stray leading empty restores the correspondence, and sentence 1 keeps its
      own translation in slot 0.

15#29 / 15#39
      These are continuation fragments of bibliography entries that spilled
      onto a second line: "J., Dec. 1978, 88(352), pp. 78g821." continues
      MUELLBAUER, and "Econ., Apr. 1975, 83(2), pp. 241-54." continues
      SARGENT. The Chinese carries only the volume/date tail, because the
      author and title were translated with the previous line. Folding the two
      English fragments back into one unit and keeping the single Chinese tail
      removes the empty slot without inventing anything.

Usage:
  python fix_last3.py model.json translations.json -o model.out.json -z zh.out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys


def split_sentences(text: str) -> list[str]:
    """Same rule build_html.py uses, so sentence counts stay in step."""
    return [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p]

# English fragments that are a continuation of the entry above them, keyed by
# page#index, with a predicate for the two pieces to re-unify.
REF_CONTINUATIONS = {
    "15#29": re.compile(r"^J\.,\s*Dec\.\s*$"),
    "15#39": re.compile(r"^Econ\.,\s*Apr\.\s*$"),
}


def rejoin_reference(model: dict) -> int:
    """Merge a continuation fragment into the bibliography entry above it."""
    done = 0
    for page in model["pages"]:
        text_els = [e for e in page.get("elements", []) if e.get("kind") == "text"]
        for i, el in enumerate(text_els):
            key = f'{page["page"]}#{el["_idx"]}'
            pat = REF_CONTINUATIONS.get(key)
            if not pat:
                continue
            sents = el.get("sentences") or []
            if not sents or not pat.match(sents[0].strip()):
                continue
            # Fold this fragment's own text into a single sentence, then attach
            # it to the tail of the preceding entry.
            whole = (el.get("text") or "").strip()
            if not whole or i == 0:
                continue
            prev = text_els[i - 1]
            prev_text = (prev.get("text") or "").rstrip()
            if prev_text.endswith("-"):
                prev_text = prev_text[:-1]
            prev["text"] = prev_text + " " + whole
            prev["sentences"] = [prev["text"]]
            # Blank the now-absorbed fragment so it renders nothing.
            el["text"] = ""
            el["sentences"] = []
            done += 1
    return done


def splice_10_8(model: dict) -> int:
    """Repair one OCR-induced false sentence boundary on page 10.

    The line reads "...requires a period of high unemployment and low income,."
    -- an OCR artifact where a comma was read as a period. The sentence
    splitter therefore ends sentence 1 at "income,." and opens sentence 2 with
    "it lowers the demand...", even though the clause is a single sentence.

    Joining the two fragments yields the grammatically correct sentence, and
    happens to match the Chinese, which already renders the clause as one
    segment. The splice is anchored on the exact OCR string so it cannot fire
    anywhere else in the document.
    """
    BAD = "and low income,. it lowers the demand"
    GOOD = "and low income, it lowers the demand"
    for page in model["pages"]:
        if page.get("page") != 10:
            continue
        for el in page.get("elements", []):
            text = el.get("text") or ""
            if BAD not in text:
                continue
            el["text"] = text.replace(BAD, GOOD)
            el["sentences"] = split_sentences(el["text"])
            return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("translations")
    ap.add_argument("-o", "--output-model", required=True)
    ap.add_argument("-z", "--output-zh", required=True)
    args = ap.parse_args()

    with open(args.model, encoding="utf-8") as fh:
        model = json.load(fh)
    with open(args.translations, encoding="utf-8") as fh:
        zh = json.load(fh)

    n_ref = rejoin_reference(model)
    n_splice = splice_10_8(model)

    # 10#8: with the bogus boundary removed the English has 4 sentences while
    # the Chinese already carries 4 non-empty segments, so the stray leading
    # empty simply disappears.
    key = "10#8"
    if key in zh and zh[key] and not zh[key][0].strip():
        zh[key] = zh[key][1:]

    # Refresh 15#29 / 15#39: the tail now belongs to the entry above, and the
    # fragments themselves are empty, so both keys collapse to zero sentences.
    for k in ("15#29", "15#39"):
        prev_map = {"15#29": "15#28", "15#39": "15#38"}
        pk = prev_map[k]
        tail = "".join(s for s in zh.get(k, []) if s.strip()).strip()
        if tail and pk in zh:
            zh[pk] = [("".join(zh[pk]) + tail).strip()]
        zh[k] = []

    # Validate against the model.
    want = {}
    for page in model["pages"]:
        for el in page.get("elements", []):
            if el.get("kind") != "text":
                continue
            want[f'{page["page"]}#{el["_idx"]}'] = len(el.get("sentences") or [])

    bad = [(k, w, len(zh.get(k, []))) for k, w in want.items() if len(zh.get(k, [])) != w]
    empt = [(k, i) for k, v in zh.items() for i, s in enumerate(v) if not s.strip()]

    with open(args.output_model, "w", encoding="utf-8") as fh:
        json.dump(model, fh, ensure_ascii=False, indent=1)
    with open(args.output_zh, "w", encoding="utf-8") as fh:
        json.dump(zh, fh, ensure_ascii=False, indent=1)

    print(f"[fix_last3] reference fragments folded: {n_ref}")
    print(f"[fix_last3] OCR boundary splices:       {n_splice}")
    print(f"[fix_last3] wrote {args.output_model} / {args.output_zh}")

    if bad:
        print(f"[fix_last3] FAIL: {len(bad)} count mismatches")
        for k, w, g in bad[:20]:
            print(f"   {k}: want={w} got={g}")
        return 1
    if empt:
        print(f"[fix_last3] FAIL: {len(empt)} empty segments")
        for k, i in empt[:20]:
            print(f"   {k}[{i}]")
        return 1
    print("[fix_last3] OK: all keys aligned, no empty segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
