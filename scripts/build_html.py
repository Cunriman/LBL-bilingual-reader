#!/usr/bin/env python3
"""
build_html.py -- Stage 3 of the doc-bilingual-reader pipeline.

Takes the geometry model from extract.py plus a translation map produced by
the model, and emits ONE self-contained HTML file:

  * absolute-positioned page canvases replicating the source layout
  * the Chinese translation rendered directly beneath each source sentence
  * every English word wrapped as a clickable lookup token
  * a compact offline dictionary inlined as JSON
  * offline pronunciation via Web Speech API
  * inline images (base64) and KaTeX-rendered formulas

Usage:
  python build_html.py model.json translations.json dict.json -o out.html \
      [--title "..."] [--mode below|side|toggle]

translations.json format (produced by the LLM):
  { "<span_id>": ["l1", "l2", ...], ... }
  where <span_id> is "<page>#<element_index>" and each entry is the list of
  Chinese translations whose i-th item corresponds to sentences[i].

dict.json format:
  { "word": {"p": "phonetic", "m": "中文释义", "t": "词性"}, ... }
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys


# --------------------------------------------------------------------------
# Word tokenisation for clickable lookups
# --------------------------------------------------------------------------
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*[A-Za-z]|[A-Za-z]")

# Words we never make clickable (too short / purely functional noise).
SKIP_LOOKUP = {"a", "an", "the", "of", "in", "on", "at", "to", "is", "am", "are",
               "was", "were", "be", "by", "it", "as", "or", "and", "for", "we",
               "he", "she", "they", "i", "you", "do", "so", "if", "no", "not"}


def is_lookup_word(raw: str) -> bool:
    """True when a surface token deserves a dictionary entry.

    Single letters are excluded on purpose: they come from abbreviations such
    as "e.g." / "i.e." / "Fig. a" and looking them up yields nonsense like
    "the 5th letter of the alphabet". They stay visible, just not clickable.
    """
    if len(raw) < 2:
        return False
    return raw.lower() not in SKIP_LOOKUP


def tokenize_html(text: str, sent_idx: int, span_id: str) -> str:
    """Wrap English words in <w> tokens carrying sentence + span identity."""
    out: list[str] = []
    pos = 0
    for m in WORD_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        raw = m.group(0)
        low = raw.lower()
        if not is_lookup_word(raw):
            out.append(html.escape(raw))
        else:
            out.append(
                f'<w data-w="{html.escape(low, quote=True)}" '
                f'data-s="{span_id}:{sent_idx}">{html.escape(raw)}</w>'
            )
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


# --------------------------------------------------------------------------
# Vocabulary collection -- know the exact word list BEFORE rendering
# --------------------------------------------------------------------------
def _lemma_candidates(word: str) -> list[str]:
    """Cheap English inflection stripping, so lookups can hit base forms.

    Ordered by likelihood; the caller tries each in turn against the dictionary.
    """
    w = word.lower()
    out = [w]
    if len(w) > 3:
        if w.endswith("ies"):
            out.append(w[:-3] + "y")
        if w.endswith("es"):
            out.append(w[:-2])
            out.append(w[:-1])
        if w.endswith("s") and not w.endswith("ss"):
            out.append(w[:-1])
        if w.endswith("ing"):
            out.append(w[:-3])
            out.append(w[:-3] + "e")
        if w.endswith("ed"):
            out.append(w[:-2])
            out.append(w[:-1])
            out.append(w[:-3] + "e") if len(w) > 4 else None
        if w.endswith("er"):
            out.append(w[:-2])
            out.append(w[:-1])
        if w.endswith("est"):
            out.append(w[:-3])
        if w.endswith("ly"):
            out.append(w[:-2])
    # preserve insertion order, drop duplicates
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def collect_vocabulary(model: dict) -> list[str]:
    """Every word a reader could click, lowercased and de-duplicated.

    This is what makes build-time prefetch possible: the word list is known
    before the HTML is written, so definitions can be baked in rather than
    fetched at click time.
    """
    words: dict[str, None] = {}
    for page in model["pages"]:
        for el in page["elements"]:
            texts: list[str] = []
            if el["kind"] == "text":
                texts = el.get("sentences") or [el.get("text", "")]
            elif el["kind"] == "formula":
                # Formula bodies are not clickable, but the model may still
                # supply definitions for symbols; skip to keep the list tight.
                continue
            for t in texts:
                for m in WORD_RE.finditer(t):
                    raw = m.group(0)
                    if not is_lookup_word(raw):
                        continue
                    words[raw.lower()] = None
    return list(words.keys())


def resolve_definition(word: str, dictionary: dict) -> dict | None:
    """Look a word up, falling back through inflected forms."""
    for cand in _lemma_candidates(word):
        if cand in dictionary:
            return dictionary[cand]
    return None


def audit_dictionary(vocab: list[str], dictionary: dict) -> dict:
    """Report coverage so the build can warn about thin dictionaries."""
    missing = [w for w in vocab if resolve_definition(w, dictionary) is None]
    return {
        "total": len(vocab),
        "missing": len(missing),
        "coverage": round(100.0 * (len(vocab) - len(missing)) / max(len(vocab), 1), 1),
        "missing_sample": missing[:40],
    }



# --------------------------------------------------------------------------
# Translation completeness gate
# --------------------------------------------------------------------------
# Markers that betray untranslated or placeholder content. If any of these end
# up in the output the deliverable is wrong, so we fail the build instead.
PLACEHOLDER_PATTERNS = [
    r"【待译】", r"\[待译\]", r"待翻译", r"TODO", r"TBD", r"XXX",
    r"\{\{", r"__TRANSLATION__", r"PLACEHOLDER",
]


def audit_translations(model: dict, translations: dict) -> list[str]:
    """Return a list of problems that must block the build.

    Checks:
      * every text element has an entry
      * entry length equals the sentence count
      * no translator placeholder markers leaked through
      * no entry is identical to its source (a sign it was never translated)
    """
    problems: list[str] = []

    for page in model["pages"]:
        pno = page["page"]
        for idx, el in enumerate(page["elements"]):
            if el["kind"] != "text":
                continue
            span_id = f"{pno}#{idx}"
            sents = el.get("sentences") or []
            if not sents:
                continue
            entry = translations.get(span_id)
            if entry is None:
                # A bare numeral on its own line is a page number or a list
                # marker -- glossing it in Chinese adds nothing, so it is
                # exempt from the coverage requirement.
                if re.fullmatch(r"\d{1,3}", (el.get("text") or "").strip()):
                    continue
                problems.append(f"{span_id}: 缺少译文（{len(sents)} 句）")
                continue
            if not isinstance(entry, list):
                problems.append(f"{span_id}: 译文不是数组")
                continue
            if len(entry) != len(sents):
                problems.append(
                    f"{span_id}: 译文 {len(entry)} 条，原文 {len(sents)} 句，数量不匹配"
                )
                continue
            for i, zt in enumerate(entry):
                if not str(zt).strip():
                    problems.append(f"{span_id}[{i}]: 译文为空")
                    continue
                for pat in PLACEHOLDER_PATTERNS:
                    if re.search(pat, str(zt), re.IGNORECASE):
                        problems.append(f"{span_id}[{i}]: 含占位符 {pat}")
                # A "translation" identical to the English source means the
                # model echoed the input instead of translating it.
                src = sents[i].strip()
                if str(zt).strip() == src and re.search(r"[A-Za-z]{3,}", src):
                    problems.append(f"{span_id}[{i}]: 译文与原文相同，未翻译")

    return problems


# --------------------------------------------------------------------------
# Text measurement (approximate, no external font metrics needed)
# --------------------------------------------------------------------------
# Latin advance widths are ~0.5em on average for Times/Arial at body sizes;
# CJK glyphs are full-width (1.0em). We add a safety factor because the browser
# may pick a wider fallback face.
LATIN_EM = 0.505
CJK_EM = 1.02
SAFETY = 1.06


def _text_advance(text: str, size: float) -> float:
    """Estimated single-line advance width of `text` at `size`."""
    if not text:
        return 0.0
    cjk = sum(
        1 for ch in text
        if 0x2E80 <= ord(ch) <= 0x9FFF or 0xF900 <= ord(ch) <= 0xFAFF
        or 0xFF00 <= ord(ch) <= 0xFF60
    )
    latin = len(text) - cjk
    return (latin * size * LATIN_EM + cjk * size * CJK_EM) * SAFETY


def measure_lines(text: str, size: float, width: float) -> int:
    """Estimate how many visual lines `text` occupies in `width` px."""
    if not text:
        return 1
    width = max(width, size * 2.0)
    total = 0.0
    cjk = 0
    for ch in text:
        o = ord(ch)
        if 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFF60:
            cjk += 1
    total = (len(text) - cjk) * size * LATIN_EM + cjk * size * CJK_EM
    total *= SAFETY
    return max(1, int(total / width) + (1 if total % width > width * 0.02 else 0))


def estimate_height(el: dict, zh: list[str], size: float, width: float) -> float:
    """Height the element will occupy once Chinese is inserted beneath sentences."""
    sentences = el.get("sentences") or [el.get("text", "")]
    lead = size * 1.18
    zh_size = size * 0.94
    zh_lead = zh_size * 1.62
    h = 0.0
    for i, sent in enumerate(sentences):
        h += measure_lines(sent, size, width) * lead
        zt = zh[i] if i < len(zh) else ""
        if zt:
            h += measure_lines(zt, zh_size, width - 16) * zh_lead + 4.5
    return h * 1.02 + 1.0


# --------------------------------------------------------------------------
# Page layout: push elements down so inserted Chinese never overlaps
# --------------------------------------------------------------------------
def _column_right(page_w: float, x0: float, y0: float, y1: float) -> float:
    """Right edge of the text column containing a span.

    PDF text spans report the width of their glyphs, not the width of the
    column, so a title's bbox can be much narrower than the space it actually
    has. We widen to a standard margin-derived column edge.
    """
    # Typical paper margins: ~72pt left/right, ~54pt for two-column layouts.
    if page_w <= 0:
        return x0 + 200.0
    margin = 72.0 if page_w > 500 else 28.0
    right = page_w - margin
    if right <= x0 + 20:
        right = page_w - 24.0
    return right


def relayout_page(elements: list[dict], translations: dict, page_no: int,
                  page_h: float, page_w: float = 595.0, gap: float = 3.0) -> list[dict]:
    """Grow each element to fit its translation and push later ones down.

    Two passes:
      1. Width pass  -- widen narrow spans so wrapped copy has room.
      2. Height pass -- grow to fit Chinese, then push subsequent elements down
                        (in reading order) so nothing overlaps.
    """
    ordered = sorted(
        range(len(elements)),
        key=lambda i: (round(elements[i]["bbox"][1], 1), elements[i]["bbox"][0]),
    )

    out = [dict(e) for e in elements]
    out = [dict(e, _i=i) for i, e in enumerate(out)]

    # --- pass 1: widen text boxes toward their column edge -----------------
    for idx in ordered:
        el = out[idx]
        if el["kind"] != "text":
            continue
        x0, y0, x1, y1 = el["bbox"]
        size = el.get("size") or 11.0
        natural = max(x1 - x0, 4.0)
        room = _column_right(page_w, x0, y0, y1) - x0
        if room <= natural:
            continue
        # Only widen when it helps: the span is a heading-line or its text
        # actually needs more room than it was given.
        need = _text_advance(el.get("text", ""), size)
        if need > natural * 1.02 or size >= 13.0:
            el["bbox"] = [x0, y0, x0 + min(room, max(natural, need * 1.02, natural)), y1]

    # --- pass 2: grow for Chinese and de-overlap ---------------------------
    last_bottom = -1e9
    gap = 3.0

    for idx in ordered:
        el = out[idx]
        x0, y0, x1, y1 = el["bbox"]
        w = max(x1 - x0, 4.0)
        size = el.get("size") or 11.0
        span_id = f"{page_no}#{idx}"
        tr = translations or {}

        if el["kind"] == "text":
            zh = tr.get(span_id) or []
            new_h = estimate_height(el, zh, size, w)
            new_h = max(new_h, max(y1 - y0, size))

            top = y0 if y0 >= last_bottom + gap else last_bottom + gap
            el["bbox"] = [x0, top, x1, top + new_h]
            last_bottom = top + new_h

        elif el["kind"] == "formula":
            h = max(y1 - y0, size * 1.2)
            top = y0 if y0 >= last_bottom + gap else last_bottom + gap
            el["bbox"] = [x0, top, x1, top + h]
            last_bottom = top + h

        else:  # image
            h = max(y1 - y0, 4.0)
            top = y0 if y0 >= last_bottom + gap else last_bottom + gap
            el["bbox"] = [x0, top, x1, top + h]
            last_bottom = top + h
            # Images reserve their full height: nothing may be pushed on top
            # of them, because their internal OCR hotspots are positioned
            # relative to the image box and would drift if it were resized.

    return out


# --------------------------------------------------------------------------
# Font stack
# --------------------------------------------------------------------------
SERIF = "'Times New Roman', 'SimSun', 'Songti SC', Texas, Georgia, serif"
SANS = "'Helvetica Neue', Helvetica, Arial, 'Microsoft YaHei', 'PingFang SC', sans-serif"
CJK = "'Songti SC', 'SimSun', 'Microsoft YaHei', 'Noto Serif CJK SC', serif"


def font_stack(font_name: str, style: dict) -> str:
    n = (font_name or "").lower()
    if any(k in n for k in ("times", "serif", "tiro", "nimbusrom", "minion", "georgia", "cmr")):
        return SERIF
    if any(k in n for k in ("cour", "mono", "consol")):
        return "'Cascadia Mono', Consolas, 'Courier New', monospace"
    return SANS


# --------------------------------------------------------------------------
# Page rendering
# --------------------------------------------------------------------------
def render_element(el: dict, page_no: int, idx: int, tr: dict, mode: str,
                   hotspots: dict | None = None) -> str:
    span_id = f"{page_no}#{idx}"
    x0, y0, x1, y1 = el["bbox"]
    w = max(x1 - x0, 4.0)
    h = max(y1 - y0, 4.0)
    size = el.get("size") or 11.0
    style = el.get("style", {})

    if el["kind"] == "image":
        src = el.get("image_b64", "")
        ext = el.get("image_ext", "png")
        img = (
            f'<img style="width:100%;height:100%;object-fit:contain;display:block" '
            f'src="data:image/{ext};base64,{src}" alt="">'
        )
        # Overlay OCR-derived clickable hotspots, if this image had text.
        hs = (hotspots or {}).get(span_id)
        hot = ""
        if hs and hs.get("words"):
            nat_w = hs.get("nat_w") or 0
            nat_h = hs.get("nat_h") or 0
            if nat_w > 0 and nat_h > 0:
                sx = 100.0 / nat_w
                sy = 100.0 / nat_h
                parts = []
                for wd in hs["words"]:
                    l = wd["x"] * sx
                    t = wd["y"] * sy
                    ww = max(wd["w_px"] * sx, 0.6)
                    hh = max(wd["h_px"] * sy, 0.6)
                    parts.append(
                        f'<w class="hot" style="left:{l:.3f}%;top:{t:.3f}%;'
                        f'width:{ww:.3f}%;height:{hh:.3f}%" '
                        f'data-w="{html.escape(wd["w"], quote=True)}"></w>'
                    )
                hot = f'<div class="hotlayer">{"".join(parts)}</div>'
        return (
            f'<div class="el imgbox" style="left:{x0}px;top:{y0}px;'
            f'width:{w}px;height:{h}px">{img}{hot}</div>'
        )

    if el["kind"] == "formula":
        # Formula text is rendered as-is (recognised, not cropped).
        body = html.escape(el.get("text", ""))
        latex = (tr or {}).get(f"{span_id}::latex")
        return (
            f'<div class="el formula" style="left:{x0}px;top:{y0}px;'
            f'min-width:{w}px;height:{h}px;line-height:{h}px;font-size:{size}px" '
            f'data-id="{span_id}" data-latex="{html.escape(latex or "", quote=True)}">'
            f'<span class="fx">{body}</span></div>'
        )

    # --- ordinary text -----------------------------------------------------
    sentences = el.get("sentences") or [el.get("text", "")]
    zh = (tr or {}).get(span_id) or []

    # A bare numeral sitting on its own is a page number / list marker: a
    # Chinese gloss underneath is pure noise, so suppress it.
    plain = el.get("text", "").strip()
    bodyless = bool(re.fullmatch(r"\d{1,3}", plain))
    is_short = len(plain) <= 24

    parts: list[str] = []
    lead = float(size) * 1.18
    for i, sent in enumerate(sentences):
        inner = tokenize_html(sent, i, span_id)
        parts.append(f'<span class="sent" data-s="{span_id}:{i}">{inner}</span>')
        if bodyless:
            continue
        zt = (zh[i] if i < len(zh) else "") or ""
        if zt:
            parts.append(
                f'<span class="zh" data-for="{span_id}:{i}" '
                f'style="line-height:{lead}px">{html.escape(zt)}</span>'
            )

    ffam = font_stack(el.get("font", ""), style)
    decor = []
    if style.get("italic"):
        decor.append("italic")
    if style.get("bold"):
        decor.append("bold")
    fstyle = "italic" if style.get("italic") else "normal"
    fweight = "600" if style.get("bold") else "400"

    # Short standalone lines ("thus", "Recall that") are tightly laid out; give
    # their translation room so it does not collapse into a 1-char column.
    cls = "el txt short" if is_short else "el txt"
    if is_short:
        lead = max(lead, float(size) * 1.5)

    return (
        f'<div class="{cls}" style="left:{x0}px;top:{y0}px;width:{w}px;'
        f'height:{h}px;font-size:{size}px;font-family:{ffam};'
        f'font-style:{fstyle};font-weight:{fweight};line-height:{lead}px" '
        f'data-id="{span_id}">'
        + "".join(parts)
        + "</div>"
    )

def expand_dictionary(model: dict, dictionary: dict) -> tuple[dict, dict]:
    """Build the inlined lookup table covering EVERY clickable word.

    At build time we know the exact vocabulary of the document, so we resolve
    each word -- trying inflected forms when the surface form is not present --
    and emit a table keyed by the surface form. Clicking is then a plain object
    lookup with no stemming or network access at runtime.

    Returns (expanded_dict, report).
    """
    vocab = collect_vocabulary(model)
    expanded: dict[str, dict] = {}
    resolved_direct = 0
    resolved_lemma = 0
    missing: list[str] = []

    for w in vocab:
        if w in dictionary:
            expanded[w] = dict(dictionary[w])
            resolved_direct += 1
            continue
        hit = resolve_definition(w, dictionary)
        if hit:
            # Carry the base form so the UI can say "词形还原自 X".
            rec = dict(hit)
            lemma = next(
                (c for c in _lemma_candidates(w) if c in dictionary), ""
            )
            if lemma and lemma != w:
                rec["l"] = lemma
            expanded[w] = rec
            resolved_lemma += 1
        else:
            missing.append(w)

    report = {
        "vocabulary": len(vocab),
        "resolved_direct": resolved_direct,
        "resolved_via_lemma": resolved_lemma,
        "unresolved": len(missing),
        "coverage": round(
            100.0 * (len(vocab) - len(missing)) / max(len(vocab), 1), 1
        ),
        "unresolved_sample": missing[:40],
    }
    return expanded, report


def build(model: dict, translations: dict, dictionary: dict, title: str, mode: str,
          hotspots: dict | None = None) -> tuple[str, dict]:
    # Resolve every clickable word up front and inline the result, so the
    # rendered page never needs to look anything up at click time.
    inlined, dict_report = expand_dictionary(model, dictionary)
    pages_html: list[str] = []

    for page in model["pages"]:
        pno = page["page"]
        pw = page.get("width", 595.0)
        ph = page.get("height", 842.0)
        flow = page.get("flow", False)

        if flow:
            els = [
                render_element(el, pno, idx, translations, mode, hotspots)
                for idx, el in enumerate(page["elements"])
            ]
            pages_html.append(
                f'<section class="page flow" data-page="{pno}">'
                f'<div class="flowbody">{"".join(els)}</div></section>'
            )
            continue

        # Grow + de-overlap before rendering so Chinese never collides.
        laid = relayout_page(page["elements"], translations, pno, ph, pw)
        els = [
            render_element(el, pno, el.get("_i", idx), translations, mode, hotspots)
            for idx, el in enumerate(laid)
        ]
        # Page height grows to contain everything (so nothing is clipped).
        used = max((e["bbox"][3] for e in laid), default=ph)
        final_h = max(ph, used + 24.0)
        pages_html.append(
            f'<section class="page" data-page="{pno}" '
            f'style="width:{pw}px;height:{final_h:.1f}px">{"".join(els)}</section>'
        )

    dict_json = json.dumps(inlined, ensure_ascii=False, separators=(",", ":"))
    meta = {
        "source": model.get("source", ""),
        "page_count": model.get("page_count", 0),
        "mode": mode,
    }

    html_out = TEMPLATE.replace("/*__DICT__*/", dict_json).replace(
        "/*__META__*/", json.dumps(meta, ensure_ascii=False)
    ).replace("<!--__TITLE__-->", html.escape(title)).replace(
        "<!--__PAGES__-->", "".join(pages_html)
    )
    return html_out, dict_report


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title><!--__TITLE__--></title>
<style>
:root{
  --bg:#f4f5f7; --page-bg:#ffffff; --fg:#1a1a1a; --fg-dim:#5c5c5c;
  --zh:#0f4c81; --zh-bg:rgba(15,76,129,.055); --w-hover:#ffe9a8;
  --w-open:#ffd166; --line:#e3e5e9; --accent:#185fa5;
  --card:#ffffff; --card-line:#d8dbe0;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--fg);
  font-family:'Times New Roman','SimSun','Songti SC',serif;
  -webkit-font-smoothing:antialiased;
}
#bar{
  position:sticky; top:0; z-index:500; display:flex; align-items:center; gap:14px;
  padding:9px 18px; background:rgba(255,255,255,.92); backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line); font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
  font-size:13px; flex-wrap:wrap;
}
#bar .brand{font-weight:600;color:var(--accent);white-space:nowrap}
#bar .sp{flex:1}
#bar .st{color:var(--fg-dim);white-space:nowrap}
button.tb{
  font:inherit; font-size:12.5px; padding:5px 11px; border:1px solid var(--card-line);
  background:#fff; color:var(--fg); border-radius:7px; cursor:pointer;
}
button.tb:hover{border-color:var(--accent);color:var(--accent)}
button.tb[aria-pressed="true"]{background:var(--accent);color:#fff;border-color:var(--accent)}
#doc{padding:22px 0 70px;display:flex;flex-direction:column;align-items:center;gap:22px}
.page{
  position:relative; background:var(--page-bg); overflow:hidden;
  box-shadow:0 1px 3px rgba(0,0,0,.09),0 8px 26px rgba(0,0,0,.055);
  max-width:100%;
}
.page.flow{width:min(820px,94vw);height:auto;padding:34px 42px}
.flowbody .el{position:static !important;width:auto !important;min-width:0 !important}
.flowbody .txt{margin:0 0 15px}
.el{position:absolute}
.el.imgbox{overflow:visible}
.el.imgbox img{width:100%;height:100%;object-fit:contain;display:block}
.hotlayer{position:absolute;inset:0}
.hotlayer w{
  position:absolute; display:block; border-radius:2px;
  background:transparent; cursor:pointer;
}
.hotlayer w:hover{background:rgba(255,209,102,.42); box-shadow:0 0 0 1px rgba(186,117,23,.5)}
.hotlayer w.on{background:rgba(255,209,102,.68); box-shadow:0 0 0 1px rgba(186,117,23,.8)}
.el.formula{
  white-space:nowrap; color:#111; font-family:'Cambria Math','Latin Modern Math',serif;
  padding:0 2px; border-radius:3px;
}
.el.formula:hover{background:rgba(24,95,165,.06); outline:1px dashed rgba(24,95,165,.35)}
.fx{font-style:italic}
.txt{white-space:normal; word-break:normal; overflow-wrap:break-word; min-height:1em}
.sent{display:inline}
w{
  cursor:pointer; border-radius:2.5px; padding:0 .5px;
  transition:background .09s ease;
}
w:hover{background:var(--w-hover)}
w.on{background:var(--w-open); box-shadow:0 0 0 1px rgba(0,0,0,.07)}
.zh{
  display:block; margin:1px 0 3px; color:var(--zh);
  background:var(--zh-bg); border-left:2px solid rgba(15,76,129,.32);
  padding:1.5px 7px 2.5px; border-radius:0 3px 3px 0;
  font-family:'Songti SC','SimSun','Microsoft YaHei',serif;
  font-size:.94em;
}
body.zh-off .zh{display:none}
body.zh-dim .zh{opacity:.4}
/* Short standalone lines ("thus", "Recall that") have a box barely wider than
   the text itself, so the translation would wrap one char per line. Let the box
   grow and keep the gloss on a single line. */
.el.txt.short{width:auto; min-width:var(--w-min,120px)}
.el.txt.short .zh{white-space:nowrap; display:inline-block; margin-top:2px}
#pop{
  position:absolute; z-index:900; display:none; min-width:236px; max-width:340px;
  background:var(--card); border:1px solid var(--card-line); border-radius:11px;
  box-shadow:0 10px 32px rgba(0,0,0,.16);
  font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
  font-size:13px; color:var(--fg); overflow:hidden;
}
#pop .hd{
  display:flex; align-items:center; gap:8px; padding:10px 13px 8px;
  border-bottom:1px solid var(--line);
}
#pop .hw{font-size:17px; font-weight:600; letter-spacing:.1px}
#pop .ph{color:var(--fg-dim); font-size:12px; font-family:'Courier New',monospace}
#pop .spk{
  margin-left:auto; width:27px;height:27px;border-radius:50%;
  border:1px solid var(--card-line); background:#fff; cursor:pointer;
  display:flex;align-items:center;justify-content:center;padding:0;flex:none;
}
#pop .spk:hover{border-color:var(--accent);background:#f0f6fc}
#pop .spk svg{width:14px;height:14px;fill:var(--accent)}
#pop .bd{padding:9px 13px 12px; line-height:1.62}
#pop .pos{
  display:inline-block; font-size:11px; padding:1px 6px; border-radius:4px;
  background:#eef2f7; color:#4a5a6b; margin-right:6px; font-style:italic;
}
#pop .miss{color:var(--fg-dim); font-size:12px}
#pop .loading{color:var(--fg-dim); font-size:12px}
#pop .src{margin-top:5px; font-size:11px; color:var(--fg-dim); font-style:italic}
#pop .foot{padding:6px 13px 9px; border-top:1px solid var(--line); font-size:11.5px; color:var(--fg-dim)}
#pop .foot a{color:var(--accent);text-decoration:none}
@media print{
  #bar,#pop{display:none}
  body{background:#fff}
  .page{box-shadow:none;break-after:page;margin:0}
  #doc{gap:0;padding:0}
}
</style>
</head>
<body>
<div id="bar">
  <span class="brand">双语对照阅读</span>
  <span class="st" id="st"></span>
  <span class="sp"></span>
  <button class="tb" id="b-zh" aria-pressed="true">中文对照</button>
  <button class="tb" id="b-all">全文展开</button>
  <button class="tb" id="b-print">打印 / 存 PDF</button>
</div>
<div id="doc"><!--__PAGES__--></div>
<div id="pop" role="dialog" aria-label="单词释义">
  <div class="hd">
    <span class="hw" id="p-hw"></span>
    <span class="ph" id="p-ph"></span>
    <button class="spk" id="p-spk" title="朗读">
      <svg viewBox="0 0 24 24"><path d="M3 10v4h4l5 4V6L7 10H3zm13.5 2a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4zM14 3.2v2.1a6.8 6.8 0 0 1 0 13.4v2.1a8.9 8.9 0 0 0 0-17.6z"/></svg>
    </button>
  </div>
  <div class="bd" id="p-bd"></div>
  <div class="foot">点击空白处关闭　·　Esc</div>
</div>
<script>
var DICT=/*__DICT__*/;
var META=/*__META__*/;
(function(){
  var pop=document.getElementById('pop');
  var pHw=document.getElementById('p-hw'), pPh=document.getElementById('p-ph');
  var pBd=document.getElementById('p-bd'), pSpk=document.getElementById('p-spk');
  var cur=null, curWord='';

  document.getElementById('st').textContent =
    META.page_count + ' 页 · 离线可用 · 点词查义';

  function esc(s){return String(s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}

  function place(t){
    var r=t.getBoundingClientRect();
    var w=pop.offsetWidth, h=pop.offsetHeight;
    var x=r.left+window.scrollX+r.width/2-w/2;
    var y=r.bottom+window.scrollY+9;
    if(y+window.scrollY+h>document.body.scrollHeight) y=r.top+window.scrollY-h-9;
    if(y<0) y=r.bottom+window.scrollY+9;
    var maxX=document.documentElement.clientWidth-w-10;
    x=Math.max(10,Math.min(x,maxX));
    pop.style.left=x+'px'; pop.style.top=y+'px';
  }

  // All definitions are baked into DICT at build time -- every word that can
  // be clicked was collected, looked up and inlined before the file was
  // written. Clicking is therefore a pure local lookup: no network, no
  // latency, and it works identically online and offline.
  //
  // DICT maps a surface form to its entry, so inflected words are resolved
  // without any stemming logic at runtime.

  function renderEntry(word, e){
    if(!e){
      pBd.innerHTML = '<span class="miss">生成时未能查到该词。</span>' +
        '<div class="src">可在<a href="https://dict.youdao.com/result?word=' +
        encodeURIComponent(word) + '&lang=en" target="_blank" rel="noopener">' +
        '有道词典</a>查看</div>';
      return;
    }
    var body = '';
    if(e.t) body += '<span class="pos">'+esc(e.t)+'</span>';
    body += esc(e.m || '');
    if(e.l && e.l !== word){
      body += '<div class="src">词形还原自 <b>'+esc(e.l)+'</b></div>';
    }
    pBd.innerHTML = body;
  }

  function show(t, word){
    var e = DICT[word];
    pHw.textContent = word;
    pPh.textContent = e ? (e.p || '') : '';
    renderEntry(word, e);
    pop.style.display = 'block';
    place(t);
    if(cur) cur.classList.remove('on');
    t.classList.add('on');
    cur = t; curWord = word;
  }

  function hide(){
    pop.style.display='none';
    if(cur){cur.classList.remove('on');cur=null;}
  }

  function speak(w){
    if(!('speechSynthesis' in window)) return;
    window.speechSynthesis.cancel();
    var u=new SpeechSynthesisUtterance(w);
    u.lang='en-US'; u.rate=.92;
    var vs=window.speechSynthesis.getVoices()||[];
    for(var i=0;i<vs.length;i++){
      if(/^en(-|_)?/i.test(vs[i].lang)){u.voice=vs[i];break;}
    }
    window.speechSynthesis.speak(u);
  }

  document.addEventListener('click',function(ev){
    var t=ev.target.closest ? ev.target.closest('w') : null;
    if(t){ ev.stopPropagation(); show(t,t.getAttribute('data-w')); return; }
    if(pop.contains(ev.target)) return;
    hide();
  },true);

  pSpk.addEventListener('click',function(ev){ev.stopPropagation(); if(curWord) speak(curWord);});
  document.addEventListener('keydown',function(ev){ if(ev.key==='Escape') hide(); });
  window.addEventListener('resize',function(){ if(pop.style.display==='block'&&cur) place(cur); });

  var zhOn=true;
  document.getElementById('b-zh').addEventListener('click',function(){
    zhOn=!zhOn;
    document.body.classList.toggle('zh-off',!zhOn);
    this.setAttribute('aria-pressed',zhOn?'true':'false');
    this.textContent=zhOn?'中文对照':'仅原文';
  });

  var allOn=false;
  document.getElementById('b-all').addEventListener('click',function(){
    allOn=!allOn;
    document.body.classList.toggle('zh-dim',allOn);
    this.setAttribute('aria-pressed',allOn?'true':'false');
    this.textContent=allOn?'淡化原文':'全文展开';
  });

  document.getElementById('b-print').addEventListener('click',function(){window.print();});

  if('speechSynthesis' in window){ window.speechSynthesis.getVoices(); }
})();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("translations")
    ap.add_argument("dictionary", nargs="?", default=None)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--hotspots", default=None,
                    help="JSON from ocr_hotspots.py for in-figure clickable text")
    ap.add_argument("--mode", default="below", choices=["below", "side", "toggle"])
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="build even if the translation audit fails (debug only)")
    args = ap.parse_args()

    model = json.load(open(args.model, encoding="utf-8"))
    translations = json.load(open(args.translations, encoding="utf-8"))
    dictionary = {}
    if args.dictionary and os.path.exists(args.dictionary):
        dictionary = json.load(open(args.dictionary, encoding="utf-8"))
    hotspots = {}
    if args.hotspots and os.path.exists(args.hotspots):
        hotspots = json.load(open(args.hotspots, encoding="utf-8"))

    # Hard gate: never ship a document with missing or placeholder translations.
    problems = audit_translations(model, translations)
    if problems:
        shown = problems[:25]
        sys.stderr.write("TRANSLATION AUDIT FAILED\n")
        for p in shown:
            sys.stderr.write(f"  - {p}\n")
        if len(problems) > len(shown):
            sys.stderr.write(f"  ... and {len(problems) - len(shown)} more\n")
        if not args.allow_incomplete:
            sys.stderr.write(
                "\nFix translations.json so every text element has exactly one "
                "Chinese sentence per source sentence, then re-run.\n"
                "Translate the content yourself -- do not leave placeholders.\n"
            )
            return 3

    title = args.title or model.get("source", "双语对照阅读")
    html_out, dict_report = build(
        model, translations, dictionary, title, args.mode, hotspots
    )

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html_out)

    size_mb = os.path.getsize(args.output) / 1048576

    # Surface dictionary coverage: a low number means readers will hit
    # "生成时未能查到该词" often, which is a build-quality problem, not a
    # runtime one. Warn loudly rather than silently shipping a thin dictionary.
    if dict_report["unresolved"]:
        sys.stderr.write(
            f"[build] dictionary coverage {dict_report['coverage']}% "
            f"({dict_report['unresolved']} words unresolved)\n"
        )
        if dict_report["unresolved_sample"]:
            sys.stderr.write(
                "  e.g. " + ", ".join(dict_report["unresolved_sample"][:15]) + "\n"
            )
        if dict_report["coverage"] < 95:
            sys.stderr.write(
                "[build] WARNING: coverage below 95%. Provide a fuller dictionary "
                "(see --ecdict / --max-rank in prefetch_dict.py) or extend "
                "terms.json, otherwise many words will show no definition.\n"
            )

    print(json.dumps({
        "output": args.output,
        "size_mb": round(size_mb, 2),
        "translation_entries": len(translations),
        "dictionary": dict_report,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
