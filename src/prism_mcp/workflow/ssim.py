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
* input flexibility: the Figma MCP returns short-lived URLs or
  base64 PNGs, *not* on-disk paths. The :func:`materialise_image`
  helper accepts ``path`` / ``url`` / ``base64`` and produces a
  :class:`pathlib.Path` the rest of the pipeline can consume.

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

import base64
import logging
import re
import tempfile
from pathlib import Path

import httpx
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity

from prism_mcp.workflow.contracts import SsimVerdict

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Image source materialisation. The Figma MCP returns screenshots as
# short-lived signed URLs (preferred — fewer tokens) or, optionally,
# inline base64 PNG. Neither is a path SSIM can read directly, so we
# normalise to "PNG bytes on disk" up front and reuse the existing
# path-based math unchanged.
# --------------------------------------------------------------------------


_DATA_URL_RE = re.compile(r"^data:image/(?P<fmt>[a-zA-Z0-9+]+);base64,(?P<b64>.+)$")
"""Match an RFC-2397 data URL with a base64 image payload.

The Figma MCP ``enableBase64Response: true`` response embeds the
PNG as a data URL. We split the prefix so the body can be fed to
:func:`base64.b64decode` directly.
"""


_DOWNLOAD_TIMEOUT_SECONDS = 30.0
"""HTTP timeout for the Figma signed-URL download.

Figma's signed URLs serve fast (sub-second on the happy path), but
the timeout protects against a hung connection wedging the
workflow on a transient CDN outage. Generous enough that a
slow corporate proxy still completes; short enough that the
workflow doesn't sit silent for minutes.
"""


def materialise_image(
    *,
    path: str | Path | None = None,
    url: str | None = None,
    base64_data: str | None = None,
    suffix: str = ".png",
) -> Path:
    """Resolve *any one* of three input shapes to an on-disk path.

    Exactly one of ``path`` / ``url`` / ``base64_data`` must be
    set. The Figma MCP gives us URLs by default and base64 only on
    explicit opt-in; this helper papers over both so the rest of
    the SSIM pipeline can keep its existing path-based contract.

    Args:
        path (str | Path | None): pre-existing local file. Returned
            verbatim as a :class:`pathlib.Path` after an existence
            check (so callers can't accidentally pass a typo through).
        url (str | None): HTTPS URL (typically a Figma signed link).
            Downloaded to a temp file and returned. Raises
            :class:`httpx.HTTPError` on transport/status errors so
            the workflow's activity-retry layer can react.
        base64_data (str | None): raw base64 PNG bytes (no header)
            *or* an RFC-2397 ``data:image/png;base64,...`` data URL.
            Decoded into a temp file.
        suffix (str): file extension used when materialising a temp
            file. Defaults to ``.png`` since both Figma exports and
            Playwright screenshots are PNG.

    Returns:
        Path: a file that exists on disk and contains the image
        bytes. For the URL / base64 inputs, the file lives under
        ``tempfile.gettempdir()`` and is the caller's responsibility
        to clean up *if* they care — the OS purges ``/tmp`` on
        reboot, and our workflow life-cycle is short enough that
        leaking a few hundred KB per run is acceptable.

    Raises:
        ValueError: when zero or multiple input modes are set.
        FileNotFoundError: when ``path`` is set but the file is
            missing — surfaced eagerly instead of letting Pillow
            fail later with a less helpful message.
    """
    provided = sum(
        x is not None for x in (path, url, base64_data)
    )
    if provided != 1:
        raise ValueError(
            "materialise_image() requires exactly one of path / url "
            f"/ base64_data; got {provided}"
        )

    if path is not None:
        resolved = Path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"image path not found: {resolved}")
        return resolved

    if base64_data is not None:
        return _materialise_from_base64(base64_data, suffix=suffix)

    assert url is not None
    return _materialise_from_url(url, suffix=suffix)


def _materialise_from_url(url: str, *, suffix: str) -> Path:
    """Download ``url`` synchronously and return the temp file path.

    Uses :mod:`httpx` (already a project dep via the registry
    client) so we keep the dependency surface tight. Synchronous
    because this is called from the SSIM ``compare_to_figma`` tool
    handler — that handler is itself async, but the download is a
    one-shot blocking operation and pulling in ``asyncio`` here
    would force every test caller to await it.
    """
    logger.info("downloading image url=%s", url)
    response = httpx.get(
        url, timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
    )
    response.raise_for_status()
    # ``delete=False`` so the file outlives this function — the
    # caller (SSIM compare) needs to open it by path. The ``with``
    # block closes the file handle on exit but leaves the bytes
    # on disk; ``tmp.name`` survives the close.
    with tempfile.NamedTemporaryFile(
        prefix="prism-mcp-figma-",
        suffix=suffix,
        delete=False,
    ) as tmp:
        tmp.write(response.content)
    logger.info(
        "materialised image from url path=%s bytes=%d",
        tmp.name,
        len(response.content),
    )
    return Path(tmp.name)


def _materialise_from_base64(payload: str, *, suffix: str) -> Path:
    """Decode a base64 (or data-URL) payload to a temp file."""
    match = _DATA_URL_RE.match(payload)
    body = match.group("b64") if match else payload
    try:
        raw = base64.b64decode(body, validate=True)
    except ValueError as exc:
        raise ValueError(
            "materialise_image() base64_data is not valid base64; "
            "expected raw base64 PNG bytes or a "
            "'data:image/...;base64,...' data URL"
        ) from exc
    # See _materialise_from_url for the rationale on
    # ``delete=False`` + ``with``.
    with tempfile.NamedTemporaryFile(
        prefix="prism-mcp-figma-",
        suffix=suffix,
        delete=False,
    ) as tmp:
        tmp.write(raw)
    logger.info(
        "materialised image from base64 path=%s bytes=%d",
        tmp.name,
        len(raw),
    )
    return Path(tmp.name)

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
