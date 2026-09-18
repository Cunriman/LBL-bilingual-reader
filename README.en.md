# doc-bilingual-reader

[中文](README.md) | **English**

Translate an English document (paper PDF / Word) into Chinese and produce **a single self-contained HTML reader**:
the original layout is preserved, a Chinese translation sits directly below each English sentence, every English word is clickable for its definition, phonetics, and pronunciation, and text inside figures is clickable too. The output is fully offline and ready to share.

> Zero external dependencies in the output: images inlined as base64, dictionary inlined, no CDN, no network requests.
> All word lookups happen once at build time; when a reader clicks a word it's a pure local lookup — instant, offline, and never fails.

## Demo

Using an 8-page macroeconomics handout (LaTeX PDF) as an example:

| Original PDF | Bilingual HTML |
|---|---|
| ![Original PDF](examples/original-p1.png) | ![Bilingual HTML](examples/bilingual-top.png) |

Click-to-look-up (phonetics + part of speech + Chinese definition, fully local popup):

![Click-to-look-up](examples/bilingual-wordpop.png)

> Demo screenshots come from a public course handout (Econ 302 Handout 1, Mankiw macroeconomics) and are used only to illustrate the tool.

## Features

- **Original layout preserved** — every character's PDF coordinates are reproduced with absolute positioning; no flow re-layout that would break multi-column or wrapped layouts
- **Sentence-level bilingual** — the Chinese translation sits directly below each English sentence, not word-for-word
- **Click-to-look-up** — every English word opens a popup with phonetics, part of speech, and Chinese definitions; definitions are pre-fetched and inlined at build time
- **Offline pronunciation** — uses the browser's local speech synthesis; no network needed
- **Clickable text inside figures** — OCR generates transparent hotspots over images so words in charts are clickable too
- **Formulas as text** — formulas are extracted as text/LaTeX instead of cropped images, so they stay copyable and searchable
- **Hard translation quality gate** — the build verifies translations and fails (exit code 3) on missing sentences, placeholders, or untranslated text

## Quick Start

```bash
# Setup (pymupdf required; the three OCR packages are only for figure hotspots, skippable)
PY="C:/Users/zhang/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
"$PY" -m pip install pymupdf pillow numpy rapidocr-onnxruntime

# 1. Extract text + coordinates + split sentences
"$PY" scripts/extract.py input.pdf -o model.json --stats

# 1b. LaTeX-typeset PDFs need a merge pre-pass (skip for normal Word/web-exported PDFs)
"$PY" scripts/merge_lines.py model.json -o merged.json --formulas formulas.json

# 2. (Model) translate sentence by sentence → translations.json + terms.json

# 3a. Pre-fetch and inline all word definitions
"$PY" scripts/prefetch_dict.py merged.json -o dict.json --terms terms.json --cache .dictcache.json --workers 4

# 3b. Figure text → clickable hotspots (only when images exist)
"$PY" scripts/ocr_hotspots.py merged.json -o hotspots.json --min-conf 55

# 4. Build the final HTML (passes a translation audit first)
"$PY" scripts/build_html.py merged.json translations.json dict.json -o out.html --hotspots hotspots.json --title "Paper Title · Bilingual"
```

The full workflow and design decisions live in [SKILL.md](SKILL.md).

## Pipeline

```
extract.py        extract text + coordinates + sentences   → model.json
(model translates)                                        → translations.json + terms.json
prefetch_dict.py  pre-fetch & inline all definitions       → dict.json
ocr_hotspots.py   OCR figure text                          → hotspots.json
build_html.py     build final HTML (with audit gate)       → out.html
```

## Dictionary Sources

Word lookups happen **once at build time** and are inlined into the HTML; clicking is a pure local lookup with zero network requests. Without a local dictionary you'll see many "not found" entries:

- [ECDICT](https://github.com/skywind3000/ECDICT) (`ecdict.csv`, 770k entries):

```bash
"$PY" scripts/prefetch_dict.py model.json -o dict.json \
  --terms terms.json --ecdict /path/to/ecdict.csv --max-rank 30000 \
  --cache .dictcache.json --workers 4
```

Resolution priority: `terms.json` > local ECDICT > cache > network (Baidu `fanyi.baidu.com/sug` → Youdao `dict.youdao.com/suggest` — server-side requests, no CORS restrictions).

**Why lookups must happen at build time**: the output is opened as a `file://` page, and browsers block cross-origin calls to Chinese dictionary APIs (Youdao/Baidu send no CORS headers; `api.dictionaryapi.dev` is unreachable from mainland China). This is enforced by the browser security model and cannot be worked around in code — so definitions are fetched once and inlined, the runtime makes zero requests, and behavior is identical online or offline.

## Known Limitations

- **Scanned PDFs unsupported** — pure-image PDFs have no text layer; OCR them first
- **Two-column papers may interleave** — sorting is by y-coordinate; group by x first if needed
- **Complex formulas may distort as text** — matrices, multi-line alignment, nested integrals; fix manually in `translations.json` under `::latex`
- **Pages grow taller** — by design; nothing is cropped
- **Acronyms are never looked up online** — BLEU / WMT / RNN get nonsense dictionary definitions; add domain meanings in `terms.json`

## File Structure

```
README.md             project intro (Chinese)
README.en.md          project intro (English)
SKILL.md              full workflow & design decisions
scripts/
  extract.py          stage 1: PDF/docx → geometric model JSON
  merge_lines.py      stage 1b: ligature expansion / fragment merge / fraction folding / paragraph reflow
  prefetch_dict.py    stage 3a: pre-fetch & inline word definitions
  ocr_hotspots.py     stage 3b: figure text → clickable hotspots
  build_html.py       stage 4: model + translations + dict → single-file HTML (with audit gate)
references/
  formulas.example.json   formula linearization override example
  terms.example.json      domain glossary example
examples/             demo screenshots
```

## License

[MIT](LICENSE)
