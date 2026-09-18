#!/usr/bin/env python3
"""
prefetch_dict.py -- Build-time dictionary prefetch.

The key insight: a translated document has a known, finite vocabulary. Instead
of looking words up when the reader clicks (which fails, because browsers block
cross-origin calls to Chinese dictionary APIs from a file:// page), we resolve
every word ONCE at build time from the server, and bake the results into the
HTML.

That makes clicking a pure local operation: instant, offline, reliable.

Sources are queried server-side, where no CORS policy applies:
  1. Baidu  fanyi.baidu.com/sug     -- rich: part of speech + multiple senses
  2. Youdao dict.youdao.com/suggest -- fallback: compact glosses
  3. A local ECDICT file, if given  -- offline, no rate limits, prefer this

Usage:
  python prefetch_dict.py model.json -o dict.json \
      [--ecdict ecdict.csv] [--terms terms.json] \
      [--max-rank 30000] [--workers 4] [--cache .dictcache.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_html import (  # noqa: E402
    collect_vocabulary,
    is_lookup_word,
    _lemma_candidates,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

POS_RE = re.compile(
    r"^\s*(n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|aux|abbr)\.\s*"
    r"|^\s*(n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|aux|abbr)\s+"
    r"|^\s*(名词|动词|形容词|副词|介词|连词|代词|数词|感叹词)\s*",
    re.IGNORECASE,
)


def _get(url: str, timeout: float = 8.0) -> bytes | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return None


def _clean_gloss(text: str) -> tuple[str, str]:
    """Split 'n. 顺序；序列' into ('n.', '顺序；序列').

    Dictionary payloads are inconsistent about the separator after the part of
    speech, so strip any leftover punctuation and stray whitespace.
    """
    t = (text or "").strip()
    m = POS_RE.match(t)
    if m:
        tag = (m.group(0) or "").strip()
        tag = re.sub(r"[.\s]+$", "", tag)
        if re.fullmatch(r"(n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|aux|abbr)",
                        tag, re.IGNORECASE):
            tag = tag + "."
        rest = t[m.end():].lstrip(" .．。:：、,，;；")
        return tag, rest.strip()
    return "", t.lstrip(" .．。:：、,，;；").strip()


def baidu_lookup(word: str) -> dict | None:
    """Baidu dictionary suggestion -- richest of the free endpoints.

    Returns a single combined gloss plus the first part of speech found.
    """
    url = "https://fanyi.baidu.com/sug"
    data = urllib.parse.urlencode({"kw": word}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json,text/plain,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=8.0) as r:
            if r.status != 200:
                return None
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None

    items = j.get("data") or []
    if not items:
        return None
    # The first entry is the headword itself; the rest are phrases.
    head = items[0]
    gloss = (head.get("v") or "").strip()
    if not gloss:
        return None
    pos, meaning = _clean_gloss(gloss)
    return {"t": pos, "m": meaning[:240], "src": "baidu"}


def youdao_lookup(word: str) -> dict | None:
    """Youdao compact gloss, used when Baidu returns nothing."""
    url = ("https://dict.youdao.com/suggest?num=1&doctype=json&q="
           + urllib.parse.quote(word))
    raw = _get(url)
    if not raw:
        return None
    try:
        j = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    entries = (j.get("data") or {}).get("entries") or []
    if not entries:
        return None
    exp = (entries[0].get("explain") or "").strip()
    if not exp:
        return None
    pos, meaning = _clean_gloss(exp)
    return {"t": pos, "m": meaning[:240], "src": "youdao"}


def load_ecdict(path: str, max_rank: int) -> dict[str, dict]:
    """Load ECDICT (csv or sqlite), filtered to reasonably frequent words."""
    out: dict[str, dict] = {}
    if not os.path.exists(path):
        return out

    if path.lower().endswith((".db", ".sqlite", ".sqlite3")):
        try:
            con = sqlite3.connect(path)
            cur = con.execute(
                "SELECT word, phonetic, translation, pos, frq FROM stardict"
            )
            for w, phon, trans, pos, frq in cur:
                try:
                    rank = int(frq)
                except (TypeError, ValueError):
                    rank = 0
                if max_rank and rank and rank > max_rank:
                    continue
                if not w:
                    continue
                out[w.lower()] = {
                    "p": (phon or "").strip(),
                    "t": _fmt_pos(pos or ""),
                    "m": _first_lines(trans or ""),
                }
            con.close()
        except Exception as e:
            sys.stderr.write(f"[prefetch] ecdict sqlite error: {e}\n")
        return out

    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 5:
                    continue
                w = row[0]
                if not w:
                    continue
                try:
                    rank = int(row[9] or 0)
                except ValueError:
                    rank = 0
                if max_rank and rank and rank > max_rank:
                    continue
                if not rank and not (row[5] or row[6]):
                    continue
                out[w.lower()] = {
                    "p": (row[1] or "").strip(),
                    "t": _fmt_pos(row[4] if len(row) > 4 else ""),
                    "m": _first_lines(row[3] if len(row) > 3 else ""),
                }
        sys.stderr.write(f"[prefetch] ecdict: loaded {len(out)} entries\n")
    except Exception as e:
        sys.stderr.write(f"[prefetch] ecdict csv error: {e}\n")
    return out


def _fmt_pos(raw: str) -> str:
    tags = re.findall(r"([a-z]+):", (raw or "").lower())
    if not tags:
        return ""
    seen: list[str] = []
    for t in tags:
        t = t + "."
        if t not in seen:
            seen.append(t)
    return "/".join(seen[:3])


def _first_lines(text: str, limit: int = 200) -> str:
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"[\n;；]", text.replace("\\n", "\n")) if p.strip()]
    return "；".join(parts)[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--ecdict", default=None,
                    help="local ECDICT csv/sqlite; consulted before the network")
    ap.add_argument("--max-rank", type=int, default=60000)
    ap.add_argument("--terms", default=None,
                    help="model-provided domain terms (highest priority)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cache", default=None,
                    help="JSON cache so re-runs do not re-query")
    ap.add_argument("--no-network", action="store_true",
                    help="use only local sources")
    ap.add_argument("--protect-acronyms", dest="protect_acronyms",
                    action="store_true", default=True,
                    help="skip network lookup for all-caps tokens (BLEU, WMT, RNN)")
    ap.add_argument("--no-protect-acronyms", dest="protect_acronyms",
                    action="store_false",
                    help="let the network answer acronyms too")
    args = ap.parse_args()

    model = json.load(open(args.model, encoding="utf-8"))
    vocab = collect_vocabulary(model)
    sys.stderr.write(f"[prefetch] document vocabulary: {len(vocab)} unique words\n")

    # Acronyms are almost always domain terms whose general dictionary entry is
    # wrong or absent -- looking up "BLEU" returns a French cheese, "WMT"
    # returns nothing useful. We never let the network answer a bare acronym;
    # terms.json must supply it, otherwise it stays unresolved and the UI says
    # so honestly instead of showing something misleading.
    surface: dict[str, None] = {}
    for page in model["pages"]:
        for el in page["elements"]:
            if el["kind"] != "text":
                continue
            for s in (el.get("sentences") or []):
                for tok in re.findall(r"[A-Za-z][A-Za-z'\u2019\-]*", s):
                    if is_lookup_word(tok):
                        surface[tok] = None

    acronyms: list[str] = []
    if args.protect_acronyms:
        acronyms = [t.lower() for t in surface if re.fullmatch(r"[A-Z]{2,}", t)]
        if acronyms:
            sys.stderr.write(
                f"[prefetch] {len(acronyms)} acronym(s) deferred to terms.json: "
                + ", ".join(acronyms[:15]) + "\n"
            )

    # Layer 1: model-provided terms win outright.
    terms: dict[str, dict] = {}
    if args.terms and os.path.exists(args.terms):
        terms = {k.lower(): v for k, v in
                 json.load(open(args.terms, encoding="utf-8")).items()}
        sys.stderr.write(f"[prefetch] terms.json: {len(terms)} entries\n")

    # Layer 2: local ECDICT.
    ecdict: dict[str, dict] = {}
    if args.ecdict:
        ecdict = load_ecdict(args.ecdict, args.max_rank)

    # Layer 3: persistent cache of previously fetched definitions.
    cache: dict[str, dict] = {}
    if args.cache and os.path.exists(args.cache):
        try:
            cache = json.load(open(args.cache, encoding="utf-8"))
            sys.stderr.write(f"[prefetch] cache: {len(cache)} entries\n")
        except Exception:
            cache = {}

    result: dict[str, dict] = {}
    need_network: list[str] = []

    for w in vocab:
        # Model-provided terms always win, and they win for the surface form
        # itself -- not merely once per lemma candidate. Checking this inside
        # the `for cand` loop meant a term like "bleu" was only honoured on
        # the first candidate iteration, so source-form terms could be
        # shadowed by a dictionary hit on a later candidate.
        if w in terms:
            result[w] = dict(terms[w])
            continue
        for cand in _lemma_candidates(w):
            if cand in terms:
                r = dict(terms[cand]); r["l"] = cand
                result[w] = r
                break
            if cand in ecdict:
                r = dict(ecdict[cand])
                if cand != w:
                    r["l"] = cand
                result[w] = r
                break
            if cand in cache:
                r = dict(cache[cand])
                if cand != w:
                    r["l"] = cand
                result[w] = r
                break
            # A bare surface-form match in the cache also counts.
            if cand in result:
                break
        if w not in result:
            if w in acronyms:
                continue  # leave for terms.json; do not guess from a dictionary
            need_network.append(w)

    sys.stderr.write(
        f"[prefetch] answered locally: {len(result)}, need network: {len(need_network)}\n"
    )

    failures: list[str] = []
    if need_network and not args.no_network:
        def fetch(word: str):
            r = baidu_lookup(word)
            if r:
                return word, r
            r = youdao_lookup(word)
            if r:
                return word, r
            # try the lemma forms before giving up
            for cand in _lemma_candidates(word)[1:]:
                r = baidu_lookup(cand) or youdao_lookup(cand)
                if r:
                    r["l"] = cand
                    return word, r
            return word, None

        done = 0
        total = len(need_network)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(fetch, w): w for w in need_network}
            for fut in as_completed(futs):
                w = futs[fut]
                try:
                    _, rec = fut.result()
                except Exception:
                    rec = None
                done += 1
                if rec:
                    result[w] = rec
                    cache[w] = {k: v for k, v in rec.items() if k != "l"}
                else:
                    failures.append(w)
                if done % 50 == 0 or done == total:
                    sys.stderr.write(f"[prefetch] {done}/{total} queried\n")
    elif need_network:
        failures = list(need_network)

    if args.cache:
        try:
            with open(args.cache, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
            sys.stderr.write(f"[prefetch] cache saved: {len(cache)} entries\n")
        except Exception as e:
            sys.stderr.write(f"[prefetch] cache write failed: {e}\n")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    kb = os.path.getsize(args.output) / 1024
    coverage = round(100.0 * len(result) / max(len(vocab), 1), 1)
    sys.stderr.write(
        f"[prefetch] {len(result)}/{len(vocab)} words resolved "
        f"({coverage}%), {kb:.0f} KB -> {args.output}\n"
    )
    if failures:
        sys.stderr.write(
            f"[prefetch] {len(failures)} unresolved, e.g. "
            + ", ".join(failures[:20]) + "\n"
        )

    print(json.dumps({
        "vocabulary": len(vocab),
        "resolved": len(result),
        "coverage": coverage,
        "unresolved": len(failures),
        "size_kb": round(kb, 1),
    }, ensure_ascii=False, indent=2))
    return 0 if coverage >= 90 else 0


if __name__ == "__main__":
    raise SystemExit(main())
