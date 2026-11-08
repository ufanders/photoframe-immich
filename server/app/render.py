"""Turn a photo into an 800x480 image using only the six Spectra 6 inks.

Usage as a CLI (handy for tuning without Immich):
    python -m app.render input.jpg output.png [--simulate sim.png]
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .config import INK_NAMES, PURE_COLORS, RenderSettings

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 as fractions


@dataclass
class Rendered:
    frame_png: bytes      # pure ink colours, what the device draws
    simulated_png: bytes  # measured ink colours, roughly what the panel will look like


# ---------- geometry ----------

def _union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def crop_window(src_w: int, src_h: int, dst_w: int, dst_h: int,
                focus: Box | None) -> tuple[int, int, int, int]:
    """Largest dst-aspect window inside the source, centred on focus (clamped to bounds)."""
    target = dst_w / dst_h
    if src_w / src_h > target:
        win_h, win_w = src_h, round(src_h * target)
    else:
        win_w, win_h = src_w, round(src_w / target)
    if focus:
        cx = (focus[0] + focus[2]) / 2 * src_w
        cy = (focus[1] + focus[3]) / 2 * src_h
    else:
        cx, cy = src_w / 2, src_h / 2
    left = int(round(min(max(cx - win_w / 2, 0), src_w - win_w)))
    top = int(round(min(max(cy - win_h / 2, 0), src_h - win_h)))
    return left, top, left + win_w, top + win_h


def cover(img: Image.Image, w: int, h: int, focus: Box | None) -> Image.Image:
    box = crop_window(img.width, img.height, w, h, focus)
    return img.crop(box).resize((w, h), Image.Resampling.LANCZOS)


def blur_fill(img: Image.Image, w: int, h: int) -> Image.Image:
    """Whole photo centred on a blurred, darkened enlargement of itself."""
    bg = cover(img, w, h, None).filter(ImageFilter.GaussianBlur(24))
    bg = ImageEnhance.Brightness(bg).enhance(0.7)
    fg = ImageOps.contain(img, (w, h), Image.Resampling.LANCZOS)
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return bg


def fit(img: Image.Image, cfg: RenderSettings, faces: list[Box]) -> Image.Image:
    w, h = cfg.width, cfg.height
    focus = _union(faces) if cfg.face_aware_crop else None
    portrait = img.height > img.width
    if portrait and cfg.portrait_mode == "blur_fill":
        return blur_fill(img, w, h)
    return cover(img, w, h, focus)


# ---------- colour ----------

def enhance(img: Image.Image, cfg: RenderSettings) -> Image.Image:
    for op, amount in ((ImageEnhance.Brightness, cfg.brightness),
                       (ImageEnhance.Contrast, cfg.contrast),
                       (ImageEnhance.Color, cfg.saturation),
                       (ImageEnhance.Sharpness, cfg.sharpness)):
        if amount != 1.0:
            img = op(img).enhance(amount)
    return img


def _palette_image(colors: list[tuple[int, int, int]]) -> Image.Image:
    # Pillow's quantize considers all 256 entries, so repeat the six colours
    # cyclically rather than padding with black; index % 6 recovers the ink.
    flat: list[int] = []
    for i in range(256):
        flat.extend(colors[i % len(colors)])
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat)
    return pal


def quantize(img: Image.Image, cfg: RenderSettings) -> np.ndarray:
    """Return an HxW array of ink indices (into INK_NAMES)."""
    measured = [tuple(cfg.measured_palette[n]) for n in INK_NAMES]
    dither = (Image.Dither.FLOYDSTEINBERG if cfg.dither == "floyd-steinberg"
              else Image.Dither.NONE)
    q = img.convert("RGB").quantize(palette=_palette_image(measured), dither=dither)
    return np.asarray(q, dtype=np.uint8) % len(INK_NAMES)


def _indexed_png(indices: np.ndarray, colors: list[tuple[int, int, int]]) -> bytes:
    im = Image.fromarray(indices, mode="P")
    im.putpalette([c for rgb in colors for c in rgb])
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render(image_bytes: bytes, cfg: RenderSettings, faces: list[Box] | None = None) -> Rendered:
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = enhance(fit(img, cfg, faces or []), cfg)
    idx = quantize(img, cfg)
    return Rendered(
        frame_png=_indexed_png(idx, [PURE_COLORS[n] for n in INK_NAMES]),
        simulated_png=_indexed_png(idx, [tuple(cfg.measured_palette[n]) for n in INK_NAMES]),
    )


if __name__ == "__main__":  # pragma: no cover
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--simulate", help="also write a preview using measured ink colours")
    a = ap.parse_args()
    r = render(Path(a.input).read_bytes(), RenderSettings())
    Path(a.output).write_bytes(r.frame_png)
    if a.simulate:
        Path(a.simulate).write_bytes(r.simulated_png)
