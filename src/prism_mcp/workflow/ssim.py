"""Pure-Python SSIM perceptual visual diff for slice 12.

This is the Tier 2 of the visual-validation chain — the SOTA
replacement for pixel diff when comparing a Figma export against a
Playwright-rendered DOM screenshot. Per the screenshot-testing-2026
survey:

  SSIM (Structural Similarity Index) measures structural similarity
  (luminance + contrast + structure). Tolerates anti-aliasing,
  catches real structural changes. >= 0.95 = virtually identical;
  0.85..0.95 = visibly close (warn); < 0.85 = visibly different
  (fail).

We rely on :func:`skimage.metrics.structural_similarity` for the
math itself; this module's value-add is:

* image loading + size normalisation (Figma exports rarely match
  the rendered DOM exactly — we resize the larger one down)
* graceful handling of tiny inputs (SSIM windows have a minimum
  size; without padding the call raises)
* a textual ``region`` hint computed from the SSIM "gradient"
  image so the LLM's reflection prompt can be specific about
  where the visual mismatch lives

Why this is a separate module from the activity
-----------------------------------------------

The activity layer (``activities.run_ssim_compare``) needs to be
imported from inside the Temporal workflow sandbox, which forbids
arbitrary filesystem access at import time. Keeping the pure math
here means the activity is a thin :func:`asyncio` wrapper while
this module can be reused by ad-hoc tool calls (``compare_to_figma``)
without spinning up a workflow.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity

from prism_mcp.workflow.contracts import SsimVerdict

logger = logging.getLogger(__name__)

_MIN_WINDOW = 7
"""SSIM's default Gaussian window size.

Inputs smaller than ``_MIN_WINDOW`` along either axis would make
:func:`structural_similarity` raise. We upsample tiny inputs to
this minimum so the call never crashes on degenerate fixtures.
"""


def compute_ssim_from_paths(
    *,
    figma_png: Path,
    rendered_png: Path,
) -> SsimVerdict:
    """Compute the SSIM verdict between two PNG paths on disk.

    The two images do not need to match in size: the larger one
    is resampled to the smaller one's dimensions before comparison.
    This is the Figma-vs-DOM common case — Figma frames are often
    1440px wide while Playwright captures at 1280px.

    Args:
        figma_png (Path): the design reference (whatever the Figma
            API returned, written to disk).
        rendered_png (Path): the generated component's screenshot
            (whatever Playwright wrote in the previous activity).

    Returns:
        SsimVerdict: ``score`` + ``region`` + (derived) ``bucket``.

    Raises:
        FileNotFoundError: when either path doesn't exist. We let
            Pillow's own ``UnidentifiedImageError`` propagate for
            corrupt PNGs.
    """
    if not figma_png.exists():
        raise FileNotFoundError(f"figma_png not found: {figma_png}")
    if not rendered_png.exists():
        raise FileNotFoundError(f"rendered_png not found: {rendered_png}")

    figma_arr = _load_grayscale(figma_png)
    rendered_arr = _load_grayscale(rendered_png)
    figma_arr, rendered_arr = _normalise_shapes(figma_arr, rendered_arr)

    # ``data_range`` is required when the input is uint8 — without
    # it scikit-image emits a deprecation warning and silently uses
    # the input's value range, which can drift across SSIM versions.
    score, full = structural_similarity(
        figma_arr,
        rendered_arr,
        data_range=255,
        full=True,
    )
    region = _region_hint(full) if score < 0.95 else None

    logger.info(
        "ssim compare figma=%s rendered=%s score=%.4f region=%s",
        figma_png,
        rendered_png,
        score,
        region,
    )
    return SsimVerdict(score=float(score), region=region)


def _load_grayscale(path: Path) -> np.ndarray:
    """Load ``path`` as a 2-D uint8 grayscale array.

    SSIM doesn't need colour to be useful for layout regressions
    — it measures luminance + contrast + structure. Working in
    grayscale halves memory and makes the ``region`` heatmap
    interpretable without a colour-channel reduction step.
    """
    with Image.open(path) as image:
        image.load()
        return np.array(image.convert("L"), dtype=np.uint8)


def _normalise_shapes(
    a: np.ndarray, b: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Resample both arrays to common dimensions before SSIM.

    Three rules, applied in order:

    1. Pad both arrays up to :data:`_MIN_WINDOW` on each axis so
       the Gaussian window fits even for degenerate (e.g.
       1-pixel) inputs.
    2. Resample the larger image to the smaller one's dimensions
       — Lanczos for clean down-sampling.
    3. Make sure both are 2-D uint8 after the resample.
    """
    h_target = min(a.shape[0], b.shape[0])
    w_target = min(a.shape[1], b.shape[1])
    h_target = max(h_target, _MIN_WINDOW)
    w_target = max(w_target, _MIN_WINDOW)
    return _resize(a, h_target, w_target), _resize(b, h_target, w_target)


def _resize(arr: np.ndarray, height: int, width: int) -> np.ndarray:
    """Resize ``arr`` to ``(height, width)`` using Pillow's Lanczos.

    We round-trip through Pillow so we get a real image resampler
    instead of nearest-neighbour numpy slicing — SSIM is sensitive
    to nearest-neighbour aliasing artefacts.
    """
    if arr.shape == (height, width):
        return arr
    pil = Image.fromarray(arr, mode="L")
    resized = pil.resize((width, height), Image.Resampling.LANCZOS)
    return np.array(resized, dtype=np.uint8)


def _region_hint(ssim_map: np.ndarray) -> str:
    """Return a textual hint for where the SSIM map is weakest.

    Splits the SSIM "full" map into a 3x3 grid (top/middle/bottom
    by left/center/right) and returns the label of the cell with
    the *lowest* mean similarity. Three columns by three rows is
    coarse enough to be reliable on small images and specific
    enough to be useful in a reflection prompt ("diff likely in
    top-right" reads naturally).

    Args:
        ssim_map (np.ndarray): the per-pixel SSIM map returned by
            :func:`structural_similarity` when ``full=True``.

    Returns:
        str: a label like ``"top-left"`` / ``"middle"`` /
        ``"bottom-right"``.
    """
    rows = np.array_split(ssim_map, 3, axis=0)
    label_rows = ("top", "middle", "bottom")
    label_cols = ("left", "center", "right")
    best_label = "middle"
    best_score = float("inf")
    for r_label, r_block in zip(label_rows, rows, strict=True):
        cols = np.array_split(r_block, 3, axis=1)
        for c_label, cell in zip(label_cols, cols, strict=True):
            cell_score = float(cell.mean())
            if cell_score < best_score:
                best_score = cell_score
                best_label = (
                    "middle"
                    if r_label == "middle" and c_label == "center"
                    else f"{r_label}-{c_label}"
                )
    return best_label
