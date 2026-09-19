#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Strip the running head that OCR glued onto each page's first sentence(s).

Why this exists
---------------
A journal scan carries a running head on one line at the very top of every
page, and it is not prose:

    even pages   "1646  Journal of Economic Literature, Vol. XXVIII (December 1990)"
    odd pages    "<Author>: A <Title> Course in Macroeconomics      1653"

That line sits only ~9pt above the body's first line, spans the column gutter,
and therefore gets merged into the first paragraph by `reflow_paragraphs.py`.
`reflow_scan.py` later splits it at the gutter, so each half lands on a
different column -- which is why the damage shows up TWICE per page, and why a
regex anchored on the whole line only ever catches one of the two halves:

    left  column: "<Author>: A <Title> ployment observed over ..."
    right column: "Course in Macroeconomics 1653 First, real business ..."

The second half is what makes this script worth having. An earlier one-off pass
removed only the "Vol. XXVIII (December 1990)" half, because that is the piece
that looks like a citation; 21 fragments of the other half survived on the
Mankiw scan -- and so did their Chinese, because the translators faithfully
translated the junk they were handed.

Journal names, author names and titles are supplied on the command line
(`--en-piece`, repeatable), never hard-coded: the same script has to work on
the next paper. `--probe` reads the document and tells you what to pass.

What it does
------------
For every text element whose head matches a running-head signature, strip the
fragments from the start of each English sentence and each Chinese segment.
A sentence or segment left with nothing but punctuation is dropped from both
sides together, so the one-Chinese-per-English-sentence invariant that
`build_html.py` enforces is preserved.

Any divergence -- a side that goes empty while its counterpart still carries
content -- is reported and makes the run fail, never silently padded.

Usage
-----
    # 1. see what furniture this scan carries
    python fix_headers.py model.json --probe

    # 2. strip it (each --en-piece is one half of the running head)
    python fix_headers.py model.json zh.json -o out.json -z out.zh.json \
        --en-piece "Journal of Economic Literature" \
        --en-piece "Mankiw: A Quick Refresher" \
        --en-piece "Course in Macroeconomics" \
        --zh-piece "《经济文献杂志》" --zh-piece "曼昆" \
        --continuations continuations.json --report
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

# --- generic fragments, true of any journal scan -----------------------------
# Applied in a loop from the start of the string, so the order in this list is
# the order they get consumed: folio, then volume line, then the caller's pieces.
GENERIC_EN = [
    re.compile(r"^\s*\d{3,4}\s+"),                                    # running folio
    re.compile(r"^\s*Vol\.\s*[IVXL]+\s*\(\s*[A-Z][a-z]+\s+\d{4}\s*\)\s*[,.]?\s*", re.I),
    re.compile(r"^\s*[（(]\s*[A-Z][a-z]+\s+\d{4}\s*[）)]\s*[,.]?\s*"),
]

GENERIC_ZH = [
    re.compile(r"^\s*\d{3,4}\s*"),
    re.compile(r"^\s*《[^》]{1,60}》\s*(?:[（(][^）)]{0,40}[）)])?\s*[，,、]?\s*"),
    re.compile(r"^\s*[（(]\s*[A-Za-z][^）)]{0,60}[）)]\s*[，,、]?\s*"),
    re.compile(r"^\s*[，,、]\s*"),
]

# Letters or CJK -- anything that means the fragment still says something.
SUBSTANCE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def _piece_body(text: str) -> str:
    """A literal piece as a whitespace-tolerant regex body."""
    toks = [re.escape(t) for t in re.split(r"\s+", text.strip()) if t]
    return r"\s*".join(toks)


def build_rules(en_pieces: list[str], zh_pieces: list[str]):
    """Assemble the strip lists and the head signature from the caller's pieces."""
    en = list(GENERIC_EN)
    zh = list(GENERIC_ZH)
    sig = [r"Vol\.\s*[IVXL]+\s*\("]

    for p in en_pieces:
        body = _piece_body(p)
        # A running head may present the piece mid-line, after the other half.
        en.append(re.compile(r"^\s*" + body + r"\s*[:：]?\s*", re.I))
        sig.append(body)
    for p in zh_pieces:
        zh.append(re.compile(r"^\s*" + _piece_body(p) + r"\s*[:：]?\s*"))

    signature = re.compile(
        r"^(?:\s*\d{3,4}\s+)?\s*(?:" + "|".join(sig) + r")", re.I)
    return en, zh, signature


def learn_zh_pieces(model: dict, zh: dict, head_signature: re.Pattern,
                    min_count: int = 2, max_len: int = 14) -> list[str]:
    """Chinese renderings of the running head, read off the document.

    Enumerating these by hand does not work. The translator's wording is theirs,
    and it is not even consistent within one paper: the Mankiw scan rendered
    "Course in Macroeconomics" as 速成课程 on one page, 快速复习 on another and
    bare 课程 on a third, so every --zh-piece list built by reading the output
    was one form short of complete.

    Two signals, both anchored on the same constraint -- only elements whose
    FIRST ENGLISH SENTENCE carries a running-head signature are examined. That
    constraint is what keeps the second signal safe. Without it, "share a
    prefix with another head" would sweep up ordinary enumerations: a paper
    introducing "在第一种情形中…" three times has three heads sharing a prefix,
    and stripping it would eat the prose.

      * folio-anchored -- a running head always carries the page number, so in
        the Chinese it reads as a short non-digit run before a 3-4 digit
        number: "宏观经济学速成课程 1655 困扰仅基于…".
      * shared prefix -- the longest prefix this head shares with at least
        min_count other heads: "曼昆：快速回顾 在实际数据中…" and
        "曼昆：快速回顾在经济周期中…" agree on "曼昆：快速回顾".

    Returned pieces go through the same factory as --zh-piece, so a learned
    form and a typed one behave identically.
    """
    heads: list[str] = []
    for page in model["pages"]:
        for el in page["elements"]:
            if el.get("kind") != "text":
                continue
            sents = el.get("sentences") or []
            if not sents or not head_signature.match(sents[0]):
                continue
            segs = zh.get(f'{page["page"]}#{el.get("_idx")}') or []
            if segs and segs[0].strip():
                heads.append(segs[0].strip())

    if len(heads) < min_count:
        return []

    # A piece must never contain a digit. The folio is stripped by its own
    # generic rule, and a piece that swallows part of one leaves the rest
    # behind: learning "宏观经济学速成课程 165" off a 14-character window turned
    # "…速成课程 1657 困扰…" into "7 困扰…".
    def head_before_digits(h: str) -> str:
        return re.split(r"\d", h, maxsplit=1)[0].strip()

    counts: Counter = Counter()
    for h in heads:
        cut = head_before_digits(h)
        for length in range(2, min(max_len, len(cut)) + 1):
            counts[cut[:length]] += 1

    pieces: set[str] = set()
    for h in heads:
        m = re.match(r"^([^\d《》\s][^\d《》]{1,13}?)\s*\d{3,4}\b", h)
        if m:
            pieces.add(m.group(1).strip())
        cut = head_before_digits(h)
        best = ""
        for length in range(2, min(max_len, len(cut)) + 1):
            if counts[cut[:length]] >= min_count:
                best = cut[:length]
        if len(best.strip()) >= 3:
            pieces.add(best.strip())
    return sorted(pieces, key=len, reverse=True)


def _strip(text: str, fragments: list[re.Pattern]) -> str:
    """Peel running-head fragments off the front, left to right."""
    for _ in range(8):
        before = text
        for rx in fragments:
            text = rx.sub("", text, count=1)
        if text == before:
            break
    return text.strip()


def _has_substance(text: str) -> bool:
    return bool(SUBSTANCE.search(text))


def probe(model: dict, zh: dict | None = None, min_pages: int = 3,
          width: int = 28, zh_width: int = 16) -> list[str]:
    """Phrases that begin an element on several different pages.

    Repetition is the general signal for page furniture: a body sentence does
    not start identically on three pages. This is how a caller finds the pieces
    to pass, on a scan whose journal nobody has seen before.

    Two details decide whether it works at all. Digits are masked, because the
    folio is part of the head and differs every page. And the window is CLIPPED
    TO A WORD BOUNDARY: the running head is fused to a different sentence on
    every page, so a fixed-width window lands mid-word and each page yields a
    different string -- a 64-char window finds nothing on the Mankiw scan,
    while the same window clipped at the last space finds all three pieces.

    The Chinese side is scanned separately, and it matters more than it looks.
    A translator hands back the running head rendered in Chinese, and its
    wording is theirs to choose -- the Mankiw scan came back with
    "宏观经济学速成课程 1655" for "Course in Macroeconomics 1655". Enumerating
    those forms by hand misses some; finding the ones that repeat across pages
    does not. Chinese has no word spaces, so its window is clipped by length.
    """
    rows: list[tuple[int, str, str]] = []

    def scan(get, w, clip_words, tag):
        seen: dict[str, set[int]] = defaultdict(set)
        for page in model["pages"]:
            for el in page["elements"]:
                if el.get("kind") != "text":
                    continue
                s = get(page["page"], el)
                if not s:
                    continue
                head = re.sub(r"\d+", "#", s[:w]).strip()
                if clip_words:
                    head = re.sub(r"\s+\S*$", "", head)
                head = re.sub(r"\s+", " ", head).strip()
                if len(head) >= 8:
                    seen[head].add(page["page"])
        for h, pages in seen.items():
            if len(pages) >= min_pages:
                rows.append((len(pages), tag, h))

    def en_head(pno, el):
        sents = el.get("sentences") or []
        return sents[0] if sents else ""

    def zh_head(pno, el):
        segs = (zh or {}).get(f'{pno}#{el.get("_idx")}') or []
        return segs[0] if segs else ""

    scan(en_head, width, True, "en")
    if zh is not None:
        scan(zh_head, zh_width, False, "zh")

    rows.sort(key=lambda r: (r[1], -r[0]))
    return [f"{tag}  {n:2d} pages  {h!r}" for n, tag, h in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", help="model JSON (with sentences)")
    ap.add_argument("translations", nargs="?",
                    help="translations JSON (omit with --probe)")
    ap.add_argument("-o", "--out", help="cleaned model JSON")
    ap.add_argument("-z", "--zh-out", help="cleaned translations JSON")
    ap.add_argument("--en-piece", action="append", default=[], metavar="TEXT",
                    help="one half of the running head, repeatable")
    ap.add_argument("--zh-piece", action="append", default=[], metavar="TEXT",
                    help="its Chinese counterpart, repeatable")
    ap.add_argument("--continuations", metavar="FILE",
                    help='JSON {"host#i": ["tail#j", "expected tail text"], ...}')
    ap.add_argument("--probe", action="store_true",
                    help="print phrases repeating across pages, change nothing")
    ap.add_argument("--no-learn-zh", action="store_true",
                    help="do not auto-collect Chinese running-head renderings")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    model = json.load(open(args.model, encoding="utf-8"))

    if args.probe:
        zh_probe = None
        if args.translations:
            zh_probe = json.load(open(args.translations, encoding="utf-8"))
        rows = probe(model, zh_probe)
        print("[fix_headers] phrases that begin elements on 3+ pages:")
        for r in rows:
            print("   " + r)
        if not rows:
            print("   (none -- this scan may carry no running head)")
        print("\npass one --en-piece / --zh-piece per fragment you want stripped.")
        print("pass --probe with the translation file too, to see the Chinese forms.")
        return 0

    if not (args.translations and args.out and args.zh_out):
        ap.error("translations, -o and -z are required unless --probe")

    zh = json.load(open(args.translations, encoding="utf-8"))

    # The signature has to exist before the Chinese forms can be learned, since
    # learning is scoped to elements that actually carry a running head.
    _, _, signature = build_rules(args.en_piece, [])
    zh_pieces = list(args.zh_piece)
    learned: list[str] = []
    if not args.no_learn_zh:
        learned = [p for p in learn_zh_pieces(model, zh, signature)
                   if p not in zh_pieces]
        zh_pieces.extend(learned)
    EN_FRAGMENTS, ZH_FRAGMENTS, head_signature = build_rules(
        args.en_piece, zh_pieces)

    continuations: dict[str, list[str]] = {}
    if args.continuations:
        continuations = json.load(open(args.continuations, encoding="utf-8"))

    touched = 0
    dropped_en = dropped_zh = 0
    divergences: list[str] = []
    samples: list[str] = []

    for page in model["pages"]:
        for el in page["elements"]:
            if el.get("kind") != "text":
                continue
            key = f'{page["page"]}#{el["_idx"]}'
            sents = el.get("sentences") or []
            if not sents:
                continue
            if not head_signature.match(sents[0]):
                continue

            touched += 1

            # --- English: clean each sentence, drop the ones that vanish -----
            new_en: list[str] = []
            for i, s in enumerate(sents):
                cleaned = _strip(s, EN_FRAGMENTS)
                if i == 0 and _has_substance(cleaned):
                    cleaned = cleaned.lstrip(".,;: ").strip()
                if _has_substance(cleaned):
                    new_en.append(cleaned)
                else:
                    dropped_en += 1

            # --- Chinese: same treatment, same indices ----------------------
            segs = list(zh.get(key) or [])
            new_zh: list[str] = []
            for i, s in enumerate(segs):
                cleaned = _strip(s, ZH_FRAGMENTS)
                if i == 0 and _has_substance(cleaned):
                    cleaned = cleaned.lstrip("，,。；;： ").strip()
                if _has_substance(cleaned):
                    new_zh.append(cleaned)
                else:
                    dropped_zh += 1

            el["sentences"] = new_en
            el["text"] = " ".join(new_en)
            zh[key] = new_zh

            if args.report and len(samples) < 8:
                samples.append(
                    f'{key}\n    EN {sents[0][:88]!r}\n    -> {new_en[0][:88]!r}'
                    if new_en else
                    f'{key}\n    EN {sents[0][:88]!r}\n    -> <dropped>'
                )

    # --- hand stranded cross-column tails back to their host sentence --------
    index = {
        f'{page["page"]}#{el["_idx"]}': el
        for page in model["pages"] for el in page["elements"]
    }
    stitched = 0
    for host_key, (tail_key, expected) in continuations.items():
        host, tail = index.get(host_key), index.get(tail_key)
        if host is None or tail is None:
            print(f"[fix_headers] FAIL: continuation {host_key}<-{tail_key} missing",
                  file=sys.stderr)
            return 1
        first = ((tail.get("sentences") or [""])[0]).strip()
        if not first.endswith(expected):
            print(f"[fix_headers] FAIL: {tail_key} starts {first[:70]!r}; expected it "
                  f"to end with {expected[:45]!r}", file=sys.stderr)
            return 1
        prev = host["sentences"][-1]
        # a hyphen left dangling at the page foot rejoins without a space
        host["sentences"][-1] = (prev[:-1] + first) if prev.endswith("-") else prev + " " + first
        host["text"] = " ".join(host["sentences"])
        tail["sentences"] = tail["sentences"][1:]
        tail["text"] = " ".join(tail["sentences"])

        # The tail's FIRST Chinese segment is the translation of the sentence
        # just moved to the host, so it has to move with it or the two sides
        # fall out of step and build_html.py refuses the file.
        #
        # Which case we are in is decided by counting. The fragment the tail
        # starts with is usually the running head, and the translator's
        # rendering of it ("宏观经济学速成课程 1655") is not something a
        # --zh-piece list reliably anticipates -- their wording differs page to
        # page. So: if the Chinese still has one segment more than the English
        # that remains, the strip did not touch it and it is removed here; if
        # the counts already agree, it went with the head and there is nothing
        # to do. Comparing counts is what makes this correct in both cases.
        tail_zh = list(zh.get(tail_key) or [])
        if len(tail_zh) == len(tail.get("sentences") or []) + 1:
            zh[tail_key] = tail_zh[1:]
        stitched += 1

    # --- verify the invariant build_html.py will enforce --------------------
    for page in model["pages"]:
        for el in page["elements"]:
            if el.get("kind") != "text":
                continue
            key = f'{page["page"]}#{el["_idx"]}'
            n_en = len(el.get("sentences") or [])
            n_zh = len(zh.get(key) or [])
            if n_en != n_zh:
                divergences.append(f"{key}: EN {n_en}  ZH {n_zh}")

    if args.report:
        print(f"[fix_headers] elements touched : {touched}")
        print(f"[fix_headers] EN sentences dropped: {dropped_en}")
        print(f"[fix_headers] ZH segments dropped : {dropped_zh}")
        print(f"[fix_headers] cross-column tails stitched: {stitched}")
        if learned:
            print(f"[fix_headers] Chinese head forms learned: {learned}")
        print("\n--- samples ---")
        for s in samples:
            print("  " + s)

    if divergences:
        print(f"\n[fix_headers] FAIL: {len(divergences)} elements diverged", file=sys.stderr)
        for d in divergences:
            print("   " + d, file=sys.stderr)
        return 1

    json.dump(model, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(zh, open(args.zh_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[fix_headers] OK: {touched} elements cleaned -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
