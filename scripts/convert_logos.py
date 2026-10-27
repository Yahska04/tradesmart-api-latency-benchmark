"""Convert official TradeSmart logo artwork (docs/brand-source/*.ai) into web assets.

The Illustrator ``.ai`` files are PDF-compatible, so PyMuPDF can read them as vector pages.
Nothing is redrawn or recoloured: every output embeds the original vector artwork via
``Page.show_pdf_page`` (optionally clipped to a region) and is then exported to SVG / PNG.

Source files (checked by rendering them):
    tradesmart_logo.ai  -> full horizontal logo (4 bars + "TRADE SMART" in Dark Blue)
    TS-logo.ai          -> identical full-colour horizontal logo (no separate monogram file exists)
    TS-logo_white.ai    -> horizontal logo with white wordmark, for dark backgrounds
The monogram (the four-bar symbol) is the left part of the official artwork, clipped out
at the gap between symbol and wordmark - it is not recreated.

Run:  .venv\\Scripts\\python scripts\\convert_logos.py
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "brand-source"
OUT = ROOT / "assets"

PNG_LOGO_HEIGHT_PX = 480     # high-res raster for the horizontal logo
PNG_MONOGRAM_PX = 512
FAVICON_PX = 64
# Gap between the symbol (bars end at x~194pt) and the wordmark (starts at x~214pt).
SYMBOL_RIGHT_EDGE_PT = 200.0


def _artwork_bounds(page: pymupdf.Page, max_x: float | None = None) -> pymupdf.Rect:
    """Union of all vector drawing bounds, clamped to the page (curve control points
    can poke a couple of points outside the page box)."""
    rect = pymupdf.Rect()
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"])
        if max_x is not None and r.x0 >= max_x:
            continue
        rect = r if rect.is_empty else rect | r
    rect &= page.rect
    if max_x is not None:
        rect.x1 = min(rect.x1, max_x)
    return rect


def _placed_page(src_path: Path, clip: pymupdf.Rect, square: bool = False,
                 pad: float = 0.0) -> pymupdf.Document:
    """New single-page PDF containing the clipped source artwork (vector, untouched)."""
    src = pymupdf.open(src_path)
    w, h = clip.width + 2 * pad, clip.height + 2 * pad
    if square:
        w = h = max(w, h)
    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    x0 = (w - clip.width) / 2
    y0 = (h - clip.height) / 2
    page.show_pdf_page(pymupdf.Rect(x0, y0, x0 + clip.width, y0 + clip.height), src, 0,
                       clip=clip, keep_proportion=True)
    return doc


def _export(doc: pymupdf.Document, stem: str, target_px: int, by: str = "height") -> None:
    page = doc[0]
    svg = page.get_svg_image(text_as_path=True)
    (OUT / f"{stem}.svg").write_text(svg, encoding="utf-8")
    ref = page.rect.height if by == "height" else max(page.rect.width, page.rect.height)
    zoom = target_px / ref
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
    pix.save(OUT / f"{stem}.png")
    print(f"  {stem}.svg / .png  ({pix.width}x{pix.height}px, alpha={bool(pix.alpha)})")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print("Converting official TradeSmart artwork ->", OUT)

    colour = SRC / "tradesmart_logo.ai"
    white = SRC / "TS-logo_white.ai"
    mono_src = SRC / "TS-logo.ai"

    src_page = pymupdf.open(colour)[0]
    full = _artwork_bounds(src_page)
    _export(_placed_page(colour, full), "tradesmart_logo", PNG_LOGO_HEIGHT_PX)

    white_page = pymupdf.open(white)[0]
    _export(_placed_page(white, _artwork_bounds(white_page)), "tradesmart_logo_white",
            PNG_LOGO_HEIGHT_PX)

    mono_page = pymupdf.open(mono_src)[0]
    symbol = _artwork_bounds(mono_page, max_x=SYMBOL_RIGHT_EDGE_PT)
    mono_doc = _placed_page(mono_src, symbol, square=True, pad=4.0)
    _export(mono_doc, "ts_monogram", PNG_MONOGRAM_PX, by="max")

    page = mono_doc[0]
    zoom = FAVICON_PX / page.rect.width
    fav = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
    fav.save(OUT / "favicon.png")
    print(f"  favicon.png ({fav.width}x{fav.height}px)")


if __name__ == "__main__":
    main()
