import io

import numpy as np
from PIL import Image

from app.config import INK_NAMES, PURE_COLORS, RenderSettings
from app.render import crop_window, render


def _jpeg(w, h):
    x = np.linspace(0, 255, w, dtype=np.uint8)
    y = np.linspace(0, 255, h, dtype=np.uint8)
    arr = np.stack([np.tile(x, (h, 1)), np.tile(y[:, None], (1, w)), np.full((h, w), 128, np.uint8)], -1)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG")
    return buf.getvalue()


def _colors(png):
    im = Image.open(io.BytesIO(png)).convert("RGB")
    return im.size, {tuple(c) for c in np.asarray(im).reshape(-1, 3)}


def test_output_uses_only_pure_inks():
    size, colors = _colors(render(_jpeg(1440, 1080), RenderSettings()).frame_png)
    assert size == (800, 480)
    assert colors <= set(PURE_COLORS.values())
    assert len(colors) >= 4  # a colourful gradient should exercise several inks


def test_simulated_uses_measured_palette():
    cfg = RenderSettings()
    _, colors = _colors(render(_jpeg(1440, 1080), cfg).simulated_png)
    assert colors <= {tuple(cfg.measured_palette[n]) for n in INK_NAMES}


def test_portrait_blur_fill_and_crop_sizes():
    for mode in ("blur_fill", "crop"):
        size, _ = _colors(render(_jpeg(1080, 1440), RenderSettings(portrait_mode=mode)).frame_png)
        assert size == (800, 480)


def test_crop_centres_on_faces_and_clamps():
    # 4:3 source -> 5:3 window: full width height crop, vertical slide only
    l, t, r, b = crop_window(1000, 1000, 800, 480, focus=(0.4, 0.0, 0.6, 0.1))
    assert (l, r) == (0, 1000) and t == 0 and b == 600  # clamped to top
    l, t, r, b = crop_window(1000, 1000, 800, 480, focus=(0.4, 0.9, 0.6, 1.0))
    assert b == 1000 and t == 400  # clamped to bottom
    l, t, r, b = crop_window(2000, 600, 800, 480, focus=None)
    assert (r - l, b - t) == (1000, 600) and l == 500
