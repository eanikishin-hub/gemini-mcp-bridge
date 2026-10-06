"""Удаление значка Gemini (✦) в правом нижнем углу картинки.

Перенесено без изменений логики из AndyShaman/gemini-webapi-mcp
(коммит 69d2f5220ad25c9dbe8146d9300283d5dfafb174, лицензия AGPL-3.0),
файл src/gemini_webapi_mcp/server.py, раздел «Watermark removal».
"""
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("most_gemini.watermark")

# ---------------------------------------------------------------------------
# Watermark removal — self-calibrating reverse alpha-blend.
#
# Gemini stamps a small neutral-white "✦" sparkle near the bottom-right corner.
# Its POSITION (margin from the corner), SIZE and OPACITY are NOT fixed — they
# drift with the generation pipeline AND with the model's auto-chosen native
# resolution. (txt2img landscape frames now carry the mark at margin≈96 /
# alpha≈0.27 — the slot the edit pipeline uses — while older portrait frames sat
# at margin≈32 / alpha≈0.50.) Hardcoding those constants stamped a dark ✦ ghost
# the moment Google moved the mark, so we no longer assume them.
#
# Instead we MEASURE everything from the frame, using the one invariant — the ✦
# SHAPE (assets/wm_alpha_edit.npy, normalized to 0..1):
#   1. localize — scan shape-correlation over position + scale in the corner;
#   2. estimate — least-squares fit the per-image opacity A of the white overlay
#      (L = bg + A·shape·(255-bg); background from a plane fit);
#   3. subtract — reverse the alpha-blend with alpha_map = shape·A, premult =
#      alpha_map·255  →  original = (watermarked - premult) / (1 - alpha_map);
#   4. self-check — if the spot now holds an inverse ghost, revert.
# Background-independent (clean on charcoal AND white) and resolution-independent
# (any native aspect ratio). Honours GEMINI_WM_KEEP=1 as a diagnostics kill-switch.
# ---------------------------------------------------------------------------
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_WHITE = 255.0                        # the ✦ is a neutral-white overlay
_ANCHOR_MARGINS = (96, 32)            # the two corner slots Gemini stamps the ✦ at
_CORR_GHOST = -0.30                   # post-removal corr below this → revert (would ghost)
_CORR_WORSE = 0.10                    # post-removal |corr| may not grow beyond this
_wm_shape = None                      # cache: normalized ✦ shape (0..1)


def _load_wm_shape():
    """Normalized ✦ shape (0..1) — the single calibrated invariant."""
    global _wm_shape
    import numpy as np
    if _wm_shape is None:
        alpha = np.load(_ASSETS_DIR / "wm_alpha_edit.npy").astype(np.float32)
        _wm_shape = alpha / float(alpha.max())
    return _wm_shape


def _shape_at(logo):
    """The ✦ shape bilinearly resized to `logo` px."""
    import numpy as np
    from PIL import Image
    shape = _load_wm_shape()
    if logo == shape.shape[0]:
        return shape
    return np.asarray(Image.fromarray((shape * 255).astype("uint8")).resize(
        (logo, logo), Image.BILINEAR), np.float32) / 255.0


def _plane_bg(g, shape):
    """Least-squares background plane fit over pixels outside the ✦ (shape≈0)."""
    import numpy as np
    m = shape < 0.06
    ys, xs = np.mgrid[0:g.shape[0], 0:g.shape[1]]
    A = np.c_[xs[m], ys[m], np.ones(int(m.sum()))]
    coef, *_ = np.linalg.lstsq(A, g[m], rcond=None)
    return coef[0] * xs + coef[1] * ys + coef[2]


def _star_corr(box, shape):
    """Correlation of background-subtracted luminance with the ✦ shape.

    ~+1 = bright sparkle present, ~0 = clean background, strongly negative = a
    dark ghost (e.g. over-subtraction). Background-robust via a plane fit.
    """
    import numpy as np
    g = box.mean(2)
    d = g - _plane_bg(g, shape)
    core = shape > 0.15
    dv = d[core] - d[core].mean()
    sv = shape[core] - shape[core].mean()
    denom = float(np.sqrt((dv ** 2).sum() * (sv ** 2).sum()))
    return float((dv * sv).sum() / denom) if denom > 1e-9 else 0.0


def _reverse_at(arr, x0, y0, logo, alpha, premult):
    """Reverse the alpha-blend in arr[y0:y0+logo, x0:x0+logo] in place."""
    import numpy as np
    box = arr[y0:y0 + logo, x0:x0 + logo]
    inv = np.clip(1.0 - alpha, 1e-3, 1.0)[..., None]
    arr[y0:y0 + logo, x0:x0 + logo] = np.clip((box - premult) / inv, 0, 255)


def _estimate_alpha(arr, x0, y0, logo):
    """Least-squares per-image opacity A of the white ✦ overlay, from the frame.

    Model: L = bg + A·shape·(255-bg)  ⇒  A = <k, L-bg> / <k, k>, k = shape·(255-bg).
    Fits over all star pixels (shape>0) so partial-coverage edges constrain A too.
    """
    import numpy as np
    sh = _shape_at(logo)
    L = arr[y0:y0 + logo, x0:x0 + logo].mean(2)
    bg = _plane_bg(L, sh)
    k = sh * (_WHITE - bg)
    m = sh > 0.02
    denom = float((k[m] ** 2).sum())
    if denom < 1e-6:
        return None
    A = float((k[m] * (L[m] - bg[m])).sum() / denom)
    return float(np.clip(A, 0.0, 0.97))


def _remove_watermark(image_path: str, upscaled: bool = False) -> Optional[bool]:
    """Remove the Gemini ✦ from image_path in place by reverse alpha-blend.

    The mark is stamped at one of two fixed corner anchors (margin 96 or 32 from
    the bottom-right) — absolute offsets that don't depend on resolution. We
    process BOTH anchors deterministically rather than detecting the mark by
    correlation: a weak ✦ on a bright/textured background (e.g. snow) has a tiny
    correlation that no threshold can separate from false matches, but its
    per-image opacity still measures correctly. On the empty anchor the fitted
    opacity is ≈0, so the subtraction is a near no-op. A self-check reverts any
    anchor whose removal would ghost (dark inverse) or otherwise grow |corr| —
    so subtracting at an empty/textured anchor can never make things worse.

    upscaled doubles the anchors/logo for 2x frames. Returns True if anything was
    removed, else None. Honours GEMINI_WM_KEEP=1.
    """
    if os.environ.get("GEMINI_WM_KEEP") == "1":
        return None
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:
        logger.warning("numpy/PIL unavailable — watermark NOT removed: %s", exc)
        return None
    try:
        img = Image.open(image_path).convert("RGB")
        w, h = img.size
        arr = np.asarray(img, np.float32).copy()
    except Exception as exc:
        logger.warning("watermark removal failed to open %s: %s", image_path, exc)
        return None

    try:
        logo = 96 if upscaled else 48
        margins = [m * 2 for m in _ANCHOR_MARGINS] if upscaled else list(_ANCHOR_MARGINS)
        sh = _shape_at(logo)
        removed = False
        for margin in margins:
            x0, y0 = w - margin - logo, h - margin - logo
            if x0 < 0 or y0 < 0:
                continue
            box = arr[y0:y0 + logo, x0:x0 + logo]
            if box.shape[0] != logo or box.shape[1] != logo:
                continue
            A = _estimate_alpha(arr, x0, y0, logo)
            if A is None or A <= 0.0:
                continue                          # no overlay here (fit opacity ≈0)
            before_corr = _star_corr(box, sh)
            before = arr.copy()
            alpha = sh * A
            premult = (alpha * _WHITE)[..., None]
            _reverse_at(arr, x0, y0, logo, alpha, premult)
            after = _star_corr(arr[y0:y0 + logo, x0:x0 + logo], sh)
            if after < _CORR_GHOST or abs(after) > abs(before_corr) + _CORR_WORSE:
                arr[:] = before                   # would ghost/worsen — revert this anchor
                logger.info("✦ anchor m%d reverted (corr %.2f→%.2f): %s",
                            margin, before_corr, after, image_path)
                continue
            removed = True
            logger.info("✦ removed at m%d (logo %d A %.2f, corr %.2f→%.2f): %s",
                        margin, logo, A, before_corr, after, image_path)
        if removed:
            Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(image_path)
            return True
        return None
    except Exception as exc:
        logger.warning("watermark removal failed on %s: %s", image_path, exc)
        return None


# ---------------------------------------------------------------------------


def remove_watermark(image_path: str) -> Optional[bool]:
    """Убирает значок; сама понимает, увеличенная ли картинка (2x)."""
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            upscaled = max(im.size) >= 1800
    except Exception:
        upscaled = False
    return _remove_watermark(image_path, upscaled=upscaled)
