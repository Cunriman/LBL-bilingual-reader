#!/usr/bin/env python3
"""
merge_lines.py -- collapse same-baseline fragments into logical lines.

LaTeX-produced PDFs (Computer Modern fonts) emit each math atom as its own
span. A single displayed equation like

    Unemployment Rate = Unemployed / Labor Force x 100

arrives as five separate elements at five nearly-equal y coordinates, and the
extractor's text/formula classifier mislabels some of them (a lone "x" becomes
a *text* element whose only sentence is "x" -- which would get "translated"
into nonsense).

This script rebuilds logical lines by grouping elements whose baselines fall
within a tolerance, sorts each group left-to-right, joins their text, and then
re-runs a math test over the joined string so the whole equation is classified
as one unit.

The output keeps the same schema so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Elements whose vertical midpoints differ by less than this are one line.
Y_TOL = 4.0

# A line is treated as *math* when it has little prose and visible math signal.
WORD_RE = re.compile(r"[A-Za-z]{3,}")

# Symbols that only ever appear in typeset mathematics as this document uses it.
MATH_SYMBOLS = set("=+-−×÷≠≤≥∑∏∫√∞←→↔∈∂αβγδεζηθικλμνξπρστυφχψω"
                   "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΠΡΣΤΥΦΧΨΩΔ")

# Fonts in this document that are used for math variables and operators.
MATH_FONTS = ("CMMI", "CMSY", "CMEX", "CMR8", "CMSY8", "CMMI8", "CMCSC")


def _is_math_font(font: str) -> bool:
    f = font or ""
    return any(f.startswith(p) for p in MATH_FONTS)


def looks_like_math(text: str, fonts: set[str]) -> bool:
    """Decide whether a merged line is an equation rather than prose.

    Two signals, in order of reliability:

    1. An explicit relation symbol (`=`, `≥`, `∑`, `∂`, `α` ...) combined with
       few *prose* words. "GDP = C + I + G + NX" has an `=` and zero prose.
    2. Math-only fonts (CMMI/CMSY/CMEX) with no prose at all.

    Prose is counted with a *sentence-like* test rather than a plain word
    count: English words carry function words (the/of/is/are/that/this) and
    verbs, whereas equation labels are bare nouns ("Consumption",
    "Investment", "Government Purchases"). Counting function words separates
    "C = Consumption" (no function words -> equation) from
    "Profit = Revenue minus Labor Cost" (still no function words, but the
    line has no prose sentence structure either, so it is an equation too).
    """
    t = text.strip()
    if not t:
        return False

    words = WORD_RE.findall(t)
    # Function words only occur in real prose.
    has_function_word = any(w.lower() in _FUNCTION_WORDS for w in words)
    sym = sum(1 for ch in t if ch in MATH_SYMBOLS)
    has_math_font = any(_is_math_font(f) for f in fonts)
    has_relation = any(ch in t for ch in "=≥≤≠→←∑∏∫∂")

    # A displayed equation with a relation symbol and no function words.
    if has_relation and not has_function_word:
        return True

    # Symbol-only line (operators, Greek letters, subscripts).
    if not words:
        return sym > 0 or has_math_font

    # Long prose sentences always contain function words; if this line has
    # several prose words AND function words, it is prose.
    if has_function_word:
        return False

    # No function words and no relation: only call it math on strong signal.
    return sym >= 2 and has_math_font


# Function words whose presence marks a line as real English prose.
_FUNCTION_WORDS = {
    "the", "and", "that", "this", "with", "for", "from", "are", "was",
    "were", "has", "have", "had", "not", "but", "which", "when", "then",
    "than", "will", "would", "can", "could", "should", "may", "might",
    "must", "been", "being", "does", "did", "its", "their", "there",
    "here", "them", "they", "your", "you", "our", "out", "into", "onto",
    "also", "some", "such", "more", "most", "other", "only", "even",
    "because", "since", "while", "where", "what", "how", "why", "who",
    "each", "every", "both", "either", "neither", "much", "many", "very",
    "just", "like", "about", "after", "before", "between", "under", "over",
    "these", "those", "them", "us", "we", "he", "she", "it", "is", "as",
    "at", "by", "in", "on", "of", "to", "or", "if", "so", "up", "down",
    "no", "do", "be", "an", "a",
}


def _merge_fractions(elements: list[dict]) -> list[dict]:
    """Fold numerator / denominator rows into the fraction's main line.

    A LaTeX display fraction is three stacked pieces: the numerator (raised),
    the rule with the operator ("= x 100"), and the denominator (lowered).
    After baseline grouping they are three separate elements. We detect a
    short element directly above and directly below a formula line, with
    overlapping x-ranges, and absorb them.
    """
    used = [False] * len(elements)
    out: list[dict] = []
    for i, el in enumerate(elements):
        if used[i]:
            continue
        if el["kind"] != "formula":
            out.append(el)
            continue

        x0, y0, x1, y1 = el["bbox"]
        height = max(y1 - y0, 1.0)

        above = below = None
        for j, other in enumerate(elements):
            if j == i or used[j] or other["kind"] == "image":
                continue
            ox0, oy0, ox1, oy1 = other["bbox"]
            # Vertical proximity: within ~1.6 line heights either way.
            dy = abs(oy0 - y1)
            if oy0 >= y0:
                continue
            if dy > height * 1.7:
                continue
            # Horizontal overlap required.
            ov = min(x1, ox1) - max(x0, ox0)
            if ov <= 0:
                continue
            if above is None or oy0 > above["bbox"][1]:
                above = other
                above_j = j
        for j, other in enumerate(elements):
            if j == i or used[j] or other["kind"] == "image":
                continue
            ox0, oy0, ox1, oy1 = other["bbox"]
            if oy0 <= y0:
                continue
            dy = oy0 - y1
            if dy > height * 1.7:
                continue
            ov = min(x1, ox1) - max(x0, ox0)
            if ov <= 0:
                continue
            if below is None or oy0 < below["bbox"][1]:
                below = other
                below_j = j

        pieces: list[str] = []
        joined = False
        if above is not None and below is not None:
            # Only fold when both sides are short (a fraction, not a paragraph).
            at = (above.get("text") or "").strip()
            bt = (below.get("text") or "").strip()
            if (at and bt and len(at) <= 40 and len(bt) <= 40
                    and len(above.get("sentences") or []) <= 1
                    and len(below.get("sentences") or []) <= 1):
                # Rewrite the stacked fraction as a linear expression so the
                # formula reads left-to-right: "Rate = Numerator/Denominator x N".
                m = re.match(r"^(.*?)\s*(=|×|÷)\s*(.*)$", el["text"])
                if m:
                    head, op, tail = m.group(1).strip(), m.group(2), m.group(3).strip()
                    frac = f"{at} / {bt}"
                    if op == "=":
                        pieces = [f"{head} = {frac}"] + ([tail] if tail else [])
                    else:
                        # e.g. "Unemployment Rate = × 100" -> the operator and
                        # trailing factor sit outside the fraction.
                        core = f"{head} = {frac}"
                        if op == "×":
                            core += " ×"
                        elif op == "÷":
                            core += " ÷"
                        pieces = [core] + ([tail] if tail else [])
                else:
                    pieces = [f"{el['text'].strip()} = {at} / {bt}"]
                joined = True
                used[above_j] = True
                used[below_j] = True

        if joined:
            ny0 = min(y0, above["bbox"][1])
            ny1 = max(y1, below["bbox"][3])
            nx0 = min(x0, above["bbox"][0], below["bbox"][0])
            nx1 = max(x1, above["bbox"][2], below["bbox"][2])
            text = " ".join(p for p in pieces if p)
            text = re.sub(r"\s{2,}", " ", text).strip()
            out.append({
                "kind": "formula",
                "bbox": [nx0, ny0, nx1, ny1],
                "text": text,
                "font": el.get("font", ""),
                "size": el.get("size", 11.0),
                "style": {"italic": False, "bold": False, "serifed": True},
                "sentences": [],
                "fraction": True,
            })
        else:
            out.append(el)

    out.sort(key=lambda e: (round(e["bbox"][1], 1), e["bbox"][0]))
    return out


def _absorb_subparts(elements: list[dict]) -> list[dict]:
    """Drop fragments already covered by a larger element, and glue the
    remaining orphan pieces of a stacked equation to their anchor line.

    Two residual cases survive baseline grouping:

    a) *Contained duplicates* -- a superscript or a repeated glyph whose box
       lies inside a bigger element (e.g. a stray "n" inside the summation
       line). These are dropped.
    b) *Nested fractions* -- an inner fraction's pieces hover just above a
       line with no counterpart below. If an orphan sits within ~1.6 line
       heights above a formula and overlaps it horizontally, and the formula
       already looks like a fraction, the orphan is folded in.
    """
    n = len(elements)
    drop = set()

    for i, el in enumerate(elements):
        if el["kind"] == "image":
            continue
        ax0, ay0, ax1, ay1 = el["bbox"]
        for j, other in enumerate(elements):
            if i == j or j in drop or other["kind"] == "image":
                continue
            bx0, by0, bx1, by1 = other["bbox"]
            # A tiny element fully inside a bigger one is a duplicate.
            if el["kind"] == "formula" and other["kind"] == "formula":
                if (bx0 >= ax0 - 1 and bx1 <= ax1 + 1
                        and by0 >= ay0 - 1 and by1 <= ay1 + 1
                        and (bx1 - bx0) * (by1 - by0)
                        < 0.5 * (ax1 - ax0) * (ay1 - ay0)):
                    drop.add(j)

    kept: list[dict] = []
    for i, el in enumerate(elements):
        if i in drop:
            continue
        kept.append(el)

    # Fold orphan math fragments up into the following formula line.
    #
    # This must stay VERY conservative: two legitimately distinct equations
    # often sit one above the other (G = G / T = T). Gluing those is worse
    # than leaving them apart. So an orphan is only absorbed when it cannot
    # stand alone -- i.e. it carries no relation symbol of its own and is
    # short enough to be a stacking piece of the line beneath it.
    out: list[dict] = []
    consumed = set()
    for i, el in enumerate(kept):
        if i in consumed:
            continue
        if el["kind"] != "formula":
            out.append(el)
            continue
        orphan_txt: list[str] = []
        ox0, oy0, ox1, oy1 = el["bbox"]
        h = max(oy1 - oy0, 1.0)
        for j, other in enumerate(kept):
            if j == i or j in consumed:
                continue
            if other["kind"] != "formula":
                continue
            bx0, by0, bx1, by1 = other["bbox"]
            if by0 >= oy0:
                continue
            if oy0 - by1 > h * 1.2:
                continue
            # Must be tightly stacked: the orphan's bottom nearly touches.
            if oy0 - by1 > 4.0:
                continue
            ov = min(ox1, bx1) - max(ox0, bx0)
            if ov <= 0:
                continue
            t = (other.get("text") or "").strip()
            if not t or len(t) > 16:
                continue
            # A self-contained equation is never an orphan piece.
            if re.search(r"[=≥≤≠]", t):
                continue
            # Must stay within the anchor's own text footprint.
            if bx0 < ox0 - 6 or bx1 > ox1 + 6:
                continue
            orphan_txt.append(t)
            consumed.add(j)
        if orphan_txt:
            merged = " ".join(orphan_txt) + " " + el["text"].strip()
            merged = re.sub(r"\s{2,}", " ", merged).strip()
            el = dict(el)
            el["text"] = merged
        out.append(el)

    out.sort(key=lambda e: (round(e["bbox"][1], 1), e["bbox"][0]))
    return out


def reflow_sentences(elements: list[dict]) -> list[dict]:
    """Rebuild whole sentences from the fragmentary line-split output.

    `extract.py` splits each visual line into sentences. In a typeset PDF a
    single sentence is spread over several lines, so most "sentences" come
    out truncated ("...used to discipline thinking and test" / "assumptions.").
    Translating those fragments one by one would produce incoherent Chinese.

    This pass walks the text elements in reading order and glues a line to
    the next when it does not end a sentence, undoing end-of-line hyphenation
    ("repre-" + "sentative" -> "representative"). A line that begins a new
    list item, heading, or display equation stops the run.
    """
    LINE_END = re.compile(r'[.!?:;]["”’)]*$')

    # Structural markers that always start a fresh unit.
    NEW_UNIT = re.compile(
        r'^\s*(?:'
        r'\(?[a-h]\)'            # (a) (b) ...
        r'|\d+\.\s'              # 1. 2.
        r'|[-–—•]\s'             # dash bullets
        r')'
    )
    HEADINGISH = re.compile(r'^[A-Z][A-Za-z ,\-()/&]{2,60}$')

    out: list[dict] = []
    buf: list[dict] = []      # elements feeding the current sentence run
    buf_text = ""

    def flush():
        nonlocal buf, buf_text
        if buf:
            merged = dict(buf[0])
            merged["text"] = buf_text
            merged["sentences"] = [buf_text]
            merged["bbox"] = [
                min(e["bbox"][0] for e in buf),
                min(e["bbox"][1] for e in buf),
                max(e["bbox"][2] for e in buf),
                max(e["bbox"][3] for e in buf),
            ]
            out.append(merged)
        buf, buf_text = [], ""

    for el in elements:
        if el.get("kind") != "text":
            flush()
            out.append(el)
            continue

        t = (el.get("text") or "").strip()
        if not t:
            continue
        # A short ALL-CAPS-ish line is a heading: never joined to a body line.
        starts_new = bool(NEW_UNIT.match(t)) or (
            len(t) <= 60 and HEADINGISH.match(t) and not LINE_END.search(t)
        )

        if not buf:
            buf, buf_text = [el], t
            if LINE_END.search(t):
                flush()
            continue

        if starts_new:
            flush()
            buf, buf_text = [el], t
            if LINE_END.search(t):
                flush()
            continue

        # Join to the running sentence.
        prev = buf_text
        if prev.endswith("-"):
            # End-of-line hyphenation: drop the hyphen and glue the halves.
            buf_text = prev[:-1] + t
        else:
            buf_text = prev + " " + t
        buf.append(el)

        if LINE_END.search(t):
            flush()

    flush()

    # Re-split each rebuilt paragraph into real sentences.
    SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z(])')
    # Tokens ending in a period that are abbreviations, not sentence ends.
    ABBREV_TAIL = re.compile(
        r'(?:\bvs|\bVs|\bvs\.|\bfig|\bFigs?|\be\.g|\bi\.e|\bet\s+al|\bno|\bcf'
        r'|\bal|\beq|\bEq|\bMankiw\s+\d+|\bch|\bCh|\bpp|\bMr|\bDr)$',
        re.IGNORECASE,
    )

    def split_sents(text: str) -> list[str]:
        raw = [p.strip() for p in SPLIT.split(text) if p.strip()]
        merged: list[str] = []
        for part in raw:
            if merged and ABBREV_TAIL.search(merged[-1].rstrip()):
                merged[-1] = merged[-1].rstrip() + " " + part
            else:
                merged.append(part)
        # A bare list marker ("1.", "(a)", "5.") is not a sentence: fold it
        # onto the heading/body text that follows, so the rendered page does
        # not show the number twice (once as the marker, once as a line).
        folded: list[str] = []
        for part in merged:
            if (folded and re.fullmatch(r"\(?[a-z\d]{1,3}[.)]?", folded[-1])
                    and not re.fullmatch(r"\(?[a-z\d]{1,3}[.)]?", part)):
                folded[-1] = folded[-1] + " " + part
            else:
                folded.append(part)
        return folded or [text]

    final: list[dict] = []
    for el in out:
        if el.get("kind") != "text":
            final.append(el)
            continue
        text = el["text"].strip()
        el["sentences"] = split_sents(text)
        final.append(el)
    return final


# Words whose letters were extracted as typographic ligatures. LaTeX PDFs set
# "fi"/"fl"/"ff" as single glyphs (U+FB01 etc.), so "profit" arrives as
# "proﬁt" and would be unclickable -- the dictionary only knows the ASCII
# spelling. Normalising at the source fixes the lookup, the tokenizer, and
# the rendered text all at once.
LIGATURES = {
    "\ufb00": "ff",   # ff
    "\ufb01": "fi",   # fi
    "\ufb02": "fl",   # fl
    "\ufb03": "ffi",  # ffi
    "\ufb04": "ffl",  # ffl
    "\u0132": "IJ",
    "\u0133": "ij",
    "\u0152": "OE",
    "\u0153": "oe",
    "\u00c6": "AE",
    "\u00e6": "ae",
}
_LIG_RE = re.compile("|".join(map(re.escape, LIGATURES)))


def normalize_ligatures(text: str) -> str:
    """Expand typographic ligatures to their ASCII letter sequences."""
    if not text:
        return text
    return _LIG_RE.sub(lambda m: LIGATURES[m.group(0)], text)


def merge_page(elements: list[dict]) -> list[dict]:
    """Group fragments by baseline into one element per logical line."""
    # Bucket by vertical midpoint so ascenders/descenders do not split a line.
    buckets: list[list[dict]] = []
    for el in elements:
        y0, y1 = el["bbox"][1], el["bbox"][3]
        mid = (y0 + y1) / 2.0
        placed = False
        for b in buckets:
            bmid = sum((e["bbox"][1] + e["bbox"][3]) / 2.0 for e in b) / len(b)
            # Compare against the running mean of the bucket.
            if abs(mid - bmid) <= Y_TOL:
                b.append(el)
                placed = True
                break
        if not placed:
            buckets.append([el])

    out: list[dict] = []
    for b in buckets:
        b.sort(key=lambda e: e["bbox"][0])
        if len(b) == 1:
            # Even a single-fragment line may be mislabelled by the extractor
            # (an equation emitted as one span is tagged `text`, and would
            # then be "translated"). Re-run the math test on it.
            el = dict(b[0])
            if el["kind"] == "text":
                fonts = {el.get("font", "")}
                if looks_like_math(el.get("text", ""), fonts):
                    el["kind"] = "formula"
                    el["sentences"] = []
            out.append(el)
            continue

        x0 = min(e["bbox"][0] for e in b)
        y0 = min(e["bbox"][1] for e in b)
        x1 = max(e["bbox"][2] for e in b)
        y1 = max(e["bbox"][3] for e in b)

        # Images are never merged into a line.
        if any(e["kind"] == "image" for e in b):
            out.extend(b)
            continue

        # Join the pieces, inserting a space only when the gap is wide enough
        # to represent one (avoids "MPL" + "L" becoming "MPL L" spuriously...
        # but here wide gaps ARE word breaks, so a threshold is required).
        parts: list[str] = []
        prev_x1 = None
        for e in b:
            t = (e.get("text") or "")
            if prev_x1 is not None:
                gap = e["bbox"][0] - prev_x1
                size = e.get("size", 11.0) or 11.0
                if gap > size * 0.16 and parts and not parts[-1].endswith(" "):
                    parts.append(" ")
            parts.append(t)
            prev_x1 = e["bbox"][2]

        text = "".join(parts)
        text = re.sub(r"\s{2,}", " ", text).strip()
        if not text:
            continue

        fonts = {e.get("font", "") for e in b}
        size = max((e.get("size") or 0) for e in b) or 11.0

        if looks_like_math(text, fonts):
            out.append({
                "kind": "formula",
                "bbox": [x0, y0, x1, y1],
                "text": text,
                "font": sorted(fonts)[0],
                "size": size,
                "style": {"italic": False, "bold": False, "serifed": True},
                "sentences": [],
            })
        else:
            # Prose line: keep it as one text element awaiting one translation.
            out.append({
                "kind": "text",
                "bbox": [x0, y0, x1, y1],
                "text": text,
                "font": sorted(fonts)[0],
                "size": size,
                "style": {"italic": False, "bold": False, "serifed": False},
                "sentences": [text],
            })

    out.sort(key=lambda e: (round(e["bbox"][1], 1), e["bbox"][0]))
    # Second pass: fold stacked display fractions into their operator line.
    out = _merge_fractions(out)
    # Third pass: drop contained duplicates, fold residual orphan pieces.
    return _absorb_subparts(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--formulas", default=None,
                    help="JSON map of element id -> corrected formula text, for "
                         "equations the extractor could not linearise")
    args = ap.parse_args()

    m = json.load(open(args.model, encoding="utf-8"))

    # Expand ligatures across the whole model first: this must happen before
    # tokenising or dictionary collection, otherwise fi/fl/ff words are keyed
    # under their ligature spelling and never resolve.
    lig_fixed = 0
    for p in m["pages"]:
        for el in p["elements"]:
            t = el.get("text")
            if t and _LIG_RE.search(t):
                el["text"] = normalize_ligatures(t)
                lig_fixed += 1
            for k in ("sentences",):
                if el.get(k):
                    el[k] = [normalize_ligatures(x) for x in el[k]]

    before = after = 0
    for p in m["pages"]:
        before += len(p["elements"])
        p["elements"] = merge_page(p["elements"])
        p["elements"] = reflow_sentences(p["elements"])
        after += len(p["elements"])
        # Re-index: element id is "<page>#<index>".
        for i, el in enumerate(p["elements"]):
            el["_idx"] = i

    # Apply hand-corrected formula overrides BEFORE writing the output -- the
    # dump used to sit above this block, so corrections only touched memory
    # and never reached disk.
    if args.formulas:
        import os as _os
        if _os.path.exists(args.formulas):
            fixes = json.load(open(args.formulas, encoding="utf-8"))
            # Match on the ORIGINAL text, not the element id: element indices
            # shift as soon as fragments are merged, so id-keyed overrides
            # silently miss. The raw string is stable and human-verifiable.
            norm = lambda s: re.sub(r"\s+", " ", (s or "")).strip()
            keyed = {norm(k): v for k, v in fixes.items()}
            applied = 0
            dropped = []
            for p in m["pages"]:
                for el in p["elements"]:
                    if el["kind"] != "formula":
                        continue
                    t = norm(el["text"])
                    if t in keyed:
                        el["text"] = keyed[t]
                        el["_override"] = True
                        applied += 1
            # Drop any override that matched nothing -- a stale entry means the
            # source text changed and the correction no longer applies.
            for k in fixes:
                if norm(k) not in {norm(e["text"]) for pp in m["pages"]
                                   for e in pp["elements"] if e["kind"] == "formula"} \
                   and not any(e.get("_override") and e["text"] == fixes[k]
                               for pp in m["pages"] for e in pp["elements"]):
                    dropped.append(k[:40])
            sys.stderr.write(f"[merge] applied {applied} formula overrides\n")
            for d in dropped:
                sys.stderr.write(f"[merge]   override unmatched: {d!r}\n")

    # Drop formula elements whose text was blanked by an override (they were
    # fragments whose content was folded into the corrected equation above).
    for p in m["pages"]:
        p["elements"] = [e for e in p["elements"]
                         if not (e["kind"] == "formula" and not e["text"].strip())]
        for i, el in enumerate(p["elements"]):
            el["_idx"] = i

    json.dump(m, open(args.output, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))

    n_form = sum(1 for p in m["pages"] for e in p["elements"] if e["kind"] == "formula")
    n_text = sum(1 for p in m["pages"] for e in p["elements"] if e["kind"] == "text")
    n_sent = sum(len(e.get("sentences") or [])
                 for p in m["pages"] for e in p["elements"])
    sys.stderr.write(f"[merge] ligatures expanded in {lig_fixed} elements\n")
    sys.stderr.write(
        f"[merge] {before} -> {after} elements "
        f"(text={n_text}, formula={n_form}, sentences={n_sent})\n"
    )
    print(json.dumps({
        "elements_before": before,
        "elements_after": after,
        "text": n_text,
        "formula": n_form,
        "sentences": n_sent,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
