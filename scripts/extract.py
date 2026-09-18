#!/usr/bin/env python3
"""
extract.py -- Stage 1 of the doc-bilingual-reader pipeline.

Reads a text-layer PDF (and, when available, a .docx) and emits a single
JSON "document model" that later stages consume:

    python extract.py <input.pdf|input.docx> -o model.json [--font-dir out/fonts]

The model preserves absolute geometry (page size + per-element boxes) so the
HTML renderer can replicate the original layout with position:absolute.

Element kinds:
    text    -- a text span with bbox, font info, and reconstructed text
    formula -- a region classified as a formula (replaced by its own text/LaTeX)
    image   -- a raster image (bbox + extracted bytes, later inlined as base64)
    vector  -- a vector drawing region (path/line art) kept as a region marker

Coordinate system: PDF points, origin top-left, y grows downward.
This matches CSS pixel-ish layout after scaling by 96/72 = 4/3, but the
renderer uses mm/px explicitly, so we keep points and convert there.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: PyMuPDF is required. Install it into the managed venv:\n"
        '  "C:/Users/zhang/.workbuddy/binaries/python/envs/default/Scripts/pip.exe" install pymupdf\n'
    )
    sys.exit(2)


# --------------------------------------------------------------------------
# Formula heuristics
# --------------------------------------------------------------------------
# A span is "math-ish" if it is dominated by math glyphs/operators, or if it
# uses a math-italic font that PyMuPDF reports as a symbol-ish face.
MATH_CHARS = set("∫∮∑∏√∞∂∇≈≠≤≥±∓×÷⋅∙∘⊗⊕∈∉⊂⊃⊆⊇∪∩∀∃⇒⇔→←↔∝≪≫∼≃≡⊥∥∠′″⟨⟩⌈⌉⌊⌋αβγδεζηθικλμνξπρστυφχψωΑΒΓΔΕΘΛΞΠΣΦΨΩ·−–—⁄")
MATH_OPERATOR_RE = re.compile(r"[=+\-*/^_<>|(){}\[\]\\]")
MATH_FONT_RE = re.compile(
    r"(math|symbol|cmsy|cmmi|cmex|stix|mathjax|mt-extra|xits|latinmodern-math|dejavu.*math|asana|cambria.?math|times.?new.?roman)",
    re.IGNORECASE,
)

# Tokens that look like variables/subscripts in a formula, e.g. "p_i", "x^2",
# "alpha_t", "W_q". Presence of several of these plus an operator is strong
# evidence of a formula.
FORMULA_TOKEN_RE = re.compile(r"\b[A-Za-z]{1,3}(?:_\{?[A-Za-z0-9]+\}?|\^\{?[A-Za-z0-9]+\}?)")

# Text that is mostly digits/punctuation/math, with a high math-glyph density.
def _math_score(text: str) -> float:
    if not text:
        return 0.0
    stripped = text.strip()
    if len(stripped) < 2:
        return 0.0
    total = len(stripped)
    hits = 0
    for ch in stripped:
        if ch in MATH_CHARS:
            hits += 1
        elif ch.isdigit():
            hits += 0.5
        elif MATH_OPERATOR_RE.match(ch):
            hits += 0.7
        elif ch.isspace():
            continue
    return hits / total


def _is_formula_span(span: dict, line_span_count: int, line_text: str = "") -> bool:
    """Heuristic: decide whether a span belongs to a formula region.

    Signals, strongest first:
      1. Math font face (cmmi/cmsy/STIX/...).
      2. Dense math glyphs/operators.
      3. Variable-with-subscript tokens (p_i, W_q, alpha_t).
      4. The span sits alone on a short line inside a wider text column,
         which is how display equations are laid out.
    """
    text = span.get("text", "")
    if not text.strip():
        return False
    stripped = text.strip()
    font = span.get("font", "") or ""
    score = _math_score(stripped)
    font_is_math = bool(MATH_FONT_RE.search(font))
    tokens = FORMULA_TOKEN_RE.findall(stripped)
    has_eq = "=" in stripped
    word_like = len(re.findall(r"[A-Za-z]{4,}", stripped))

    if font_is_math and score >= 0.2:
        return True
    if has_eq and (tokens or score >= 0.3) and word_like <= 2:
        return True
    if score >= 0.6 and len(stripped) >= 4:
        return True
    if line_span_count <= 2 and score >= 0.4 and word_like <= 1:
        return True
    # Alone on its line, short, with subscript tokens -> display equation.
    if line_span_count <= 2 and tokens and len(stripped) <= 90 and word_like <= 3:
        return True
    return False


# --------------------------------------------------------------------------
# Sentence segmentation (English, paper-aware)
# --------------------------------------------------------------------------
ABBREVIATIONS = {
    "et al", "e.g", "i.e", "cf", "vs", "etc", "fig", "figs", "eq", "eqs",
    "ref", "refs", "no", "nos", "vol", "pp", "sec", "app", "tab", "tabs",
    "approx", "resp", "min", "max", "avg", "std", "dr", "prof", "mr", "ms",
    "mrs", "st", "inc", "ltd", "co", "dept", "univ", "nat", "int", "ieee",
    "acm", "proc", "conf", "stat", "al", "ca", "circa", "esp",
}

_SENT_END_RE = re.compile(r'(?<=[.!?])["\'\)\]]?\s+')
_TRAILING_NUM_RE = re.compile(r"(?:\b[A-Za-z]{1,4}|\d+)\.$")
_INITIAL_RE = re.compile(r"(?:^|\s)[A-Z]\.$")


def split_sentences(text: str) -> list[str]:
    """Split a paragraph into sentences, resisting common abbreviation traps."""
    text = text.strip()
    if not text:
        return []

    # Protect decimal numbers and abbreviations with a sentinel.
    # NOTE: chr(0) must be expressed via a function replacement -- "\x00" is not
    # a legal escape inside an re.sub replacement template.
    SENT = chr(0)
    protected = text
    protected = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + SENT + m.group(2), protected)

    def _protect_abbrev(m: re.Match) -> str:
        return m.group(0).replace(".", SENT)

    protected = re.sub(
        r"\b(" + "|".join(re.escape(a) for a in ABBREVIATIONS) + r")\.",
        _protect_abbrev,
        protected,
        flags=re.IGNORECASE,
    )
    protected = re.sub(r"\b([A-Z])\.(?=\s*[A-Z])", lambda m: m.group(1) + SENT, protected)

    parts = _SENT_END_RE.split(protected)
    out: list[str] = []
    for p in parts:
        restored = p.replace("\x00", ".").strip()
        if restored:
            out.append(restored)

    # Merge fragments that are clearly not real sentences (e.g. "Fig." alone).
    merged: list[str] = []
    for s in out:
        if merged and len(merged[-1]) < 12 and not re.search(r"[.!?]$", merged[-1]):
            merged[-1] = merged[-1] + " " + s
        else:
            merged.append(s)
    return merged


# --------------------------------------------------------------------------
# Block assembly
# --------------------------------------------------------------------------
@dataclass
class Element:
    kind: str
    bbox: list[float]
    text: str = ""
    font: str = ""
    size: float = 0.0
    flags: int = 0
    sentences: list[str] = field(default_factory=list)
    image_b64: str = ""
    image_ext: str = "png"


def _flags_to_style(flags: int) -> dict[str, bool]:
    return {
        "italic": bool(flags & 2),
        "bold": bool(flags & 16),
        "serifed": bool(flags & 4),
    }


def extract_pdf(path: str, font_dir: str | None) -> dict[str, Any]:
    doc = fitz.open(path)
    pages: list[dict[str, Any]] = []
    fonts_out: dict[str, str] = {}

    for pno in range(len(doc)):
        page = doc[pno]
        rect = page.rect
        raw = page.get_text("dict")

        elements: list[Element] = []
        # Collect images first so we can dedupe overlapping text later.
        image_rects: list[fitz.Rect] = []

        for img in page.get_images(full=True):
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            for r in rects:
                if r.is_empty or r.get_area() <= 0:
                    continue
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:  # CMYK -> RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    ext = "png"
                    data = pix.tobytes("png")
                    b64 = base64.b64encode(data).decode("ascii")
                except Exception:
                    continue
                image_rects.append(r)
                elements.append(
                    Element(
                        kind="image",
                        bbox=[r.x0, r.y0, r.x1, r.y1],
                        image_b64=b64,
                        image_ext=ext,
                    )
                )

        def _covered_by_image(r: fitz.Rect) -> bool:
            for ir in image_rects:
                inter = r & ir
                if inter.is_empty:
                    continue
                if inter.get_area() >= 0.6 * max(r.get_area(), 1e-6):
                    return True
            return False

        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = "".join(s.get("text", "") for s in spans).strip()
                if not line_text:
                    continue

                for s in spans:
                    stext = s.get("text", "")
                    if not stext.strip():
                        continue
                    bbox = s.get("bbox", [0, 0, 0, 0])
                    r = fitz.Rect(bbox)
                    if _covered_by_image(r):
                        continue

                    kind = "text"
                    if _is_formula_span(
                        {"text": stext, "font": s.get("font", "")},
                        len(spans),
                        line_text,
                    ):
                        kind = "formula"

                    el = Element(
                        kind=kind,
                        bbox=list(bbox),
                        text=stext,
                        font=s.get("font", ""),
                        size=float(s.get("size", 0.0)),
                        flags=int(s.get("flags", 0)),
                    )
                    if kind == "text":
                        el.sentences = split_sentences(stext)
                    elements.append(el)

                    fname = s.get("font", "")
                    if fname and fname not in fonts_out:
                        fonts_out[fname] = ""

        # Merge adjacent same-style text spans into paragraph-ish rows.
        elements = _merge_text_runs(elements)

        pages.append(
            {
                "page": pno + 1,
                "width": rect.width,
                "height": rect.height,
                "elements": [
                    {
                        "kind": e.kind,
                        "bbox": [round(v, 2) for v in e.bbox],
                        "text": e.text,
                        "font": e.font,
                        "size": round(e.size, 2),
                        "style": _flags_to_style(e.flags),
                        "sentences": e.sentences,
                        **({"image_b64": e.image_b64, "image_ext": e.image_ext} if e.kind == "image" else {}),
                    }
                    for e in elements
                ],
            }
        )

    return {
        "source": os.path.basename(path),
        "source_type": "pdf",
        "page_count": len(doc),
        "unit": "pt",
        "fonts": sorted(fonts_out.keys()),
        "pages": pages,
    }


def _merge_text_runs(elements: list[Element]) -> list[Element]:
    """Join horizontally-adjacent text spans on the same baseline into runs."""
    out: list[Element] = []
    texts = [e for e in elements if e.kind in ("text", "formula")]
    others = [e for e in elements if e.kind not in ("text", "formula")]
    texts.sort(key=lambda e: (round(e.bbox[1], 1), e.bbox[0]))

    buf: Element | None = None
    for e in texts:
        if buf is None:
            buf = Element(**{**asdict(e)})
            continue
        same_size = abs(buf.size - e.size) < 0.6
        same_kind = buf.kind == e.kind
        v_close = abs(((buf.bbox[1] + buf.bbox[3]) / 2) - ((e.bbox[1] + e.bbox[3]) / 2)) < max(
            buf.size * 0.55, 3.0
        )
        gap = e.bbox[0] - buf.bbox[2]
        if same_size and same_kind and v_close and -1.5 <= gap <= max(buf.size * 1.6, 8.0):
            sep = "" if gap < 0.35 else " "
            if buf.text.endswith(("-", "\u2010")) and sep == " " and len(buf.text) > 2:
                buf.text = buf.text[:-1] + e.text
            else:
                buf.text = buf.text + sep + e.text
            buf.bbox = [
                min(buf.bbox[0], e.bbox[0]),
                min(buf.bbox[1], e.bbox[1]),
                max(buf.bbox[2], e.bbox[2]),
                max(buf.bbox[3], e.bbox[3]),
            ]
        else:
            buf.sentences = split_sentences(buf.text)
            out.append(buf)
            buf = Element(**{**asdict(e)})
    if buf is not None:
        buf.sentences = split_sentences(buf.text)
        out.append(buf)

    out.extend(others)
    out.sort(key=lambda e: (round(e.bbox[1], 1), e.bbox[0]))
    return out


def extract_docx(path: str) -> dict[str, Any]:
    """Minimal docx reader: paragraphs only, no geometry (docx has none)."""
    import zipfile
    import xml.etree.ElementTree as ET

    NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    paras: list[str] = []
    for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        txt = "".join(t.text or "" for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        if txt.strip():
            paras.append(txt.strip())
    body = "\n".join(paras)
    return {
        "source": os.path.basename(path),
        "source_type": "docx",
        "page_count": 1,
        "unit": "flow",
        "fonts": [],
        "pages": [
            {
                "page": 1,
                "width": 595.0,
                "height": 842.0,
                "flow": True,
                "elements": [
                    {
                        "kind": "text",
                        "bbox": [0, 0, 595, 0],
                        "text": para,
                        "font": "",
                        "size": 11.0,
                        "style": {"italic": False, "bold": False, "serifed": True},
                        "sentences": split_sentences(para),
                    }
                    for para in paras
                ],
            }
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        sys.stderr.write(f"ERROR: input not found: {args.input}\n")
        return 1

    ext = os.path.splitext(args.input)[1].lower()
    if ext == ".pdf":
        model = extract_pdf(args.input, None)
    elif ext in (".docx", ".doc"):
        model = extract_docx(args.input)
    else:
        sys.stderr.write(f"ERROR: unsupported input type: {ext}\n")
        return 1

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, separators=(",", ":"))

    if args.stats:
        n_txt = sum(1 for p in model["pages"] for e in p["elements"] if e["kind"] == "text")
        n_fml = sum(1 for p in model["pages"] for e in p["elements"] if e["kind"] == "formula")
        n_img = sum(1 for p in model["pages"] for e in p["elements"] if e["kind"] == "image")
        n_sent = sum(len(e.get("sentences", [])) for p in model["pages"] for e in p["elements"])
        print(json.dumps({
            "pages": model["page_count"],
            "text_spans": n_txt,
            "formula_spans": n_fml,
            "images": n_img,
            "sentences": n_sent,
            "fonts": len(model["fonts"]),
            "output": args.output,
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
