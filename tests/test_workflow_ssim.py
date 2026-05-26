"""Tests for the slice-12 SSIM perceptual visual-diff helper.

We test :func:`compute_ssim_from_paths` (the public API) against
synthetic PNG fixtures so the suite is hermetic — no Figma API
calls, no Playwright screenshot capture. The math itself is
:mod:`scikit-image`'s job; our job is to verify:

* identical images return ``score ~= 1.0`` and ``bucket == "pass"``
* gentle blur / small overlay drops the score but stays in the
  ``warn`` band
* completely different images drop into ``fail``
* we resize unequal-sized inputs before comparing (Figma exports
  and rendered screenshots almost never have matching dimensions)
* we surface a meaningful ``region`` hint when the score isn't pass
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from prism_mcp.workflow.ssim import compute_ssim_from_paths


def _write_png(
    path: Path,
    *,
    size: tuple[int, int] = (256, 128),
    fill: tuple[int, int, int] = (240, 240, 240),
) -> None:
    """Write a synthetic solid-fill PNG for testing."""
    img = Image.new("RGB", size, fill)
    img.save(path)


def _write_text_png(
    path: Path,
    *,
    size: tuple[int, int] = (256, 128),
    fill: tuple[int, int, int] = (240, 240, 240),
    text_xy: tuple[int, int] = (10, 10),
    text: str = "Submit",
) -> None:
    """Write a synthetic PNG with a few text pixels for structure."""
    img = Image.new("RGB", size, fill)
    drawer = ImageDraw.Draw(img)
    drawer.text(text_xy, text, fill=(20, 20, 20))
    img.save(path)


# --------------------------------------------------------------------------
# Identity / near-identity: pass bucket.
# --------------------------------------------------------------------------


def test_identical_images_score_close_to_one(tmp_path: Path) -> None:
    """Same PNG compared with itself → SSIM ~= 1.0 → pass bucket."""
    a = tmp_path / "a.png"
    _write_text_png(a)

    verdict = compute_ssim_from_paths(figma_png=a, rendered_png=a)

    assert verdict.score == pytest.approx(1.0, abs=1e-6)
    assert verdict.bucket == "pass"
    assert verdict.ok is True


def test_tiny_overlay_stays_in_pass_or_warn_bucket(tmp_path: Path) -> None:
    """A 1-pixel-wide top-row tweak should not crash into fail —
    real anti-aliasing differences across Figma vs Chrome rendering
    look like this.
    """
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _write_text_png(a)
    img = Image.open(a).convert("RGB")
    # Tweak one pixel — emulates anti-aliasing noise.
    img.putpixel((0, 0), (200, 200, 200))
    img.save(b)

    verdict = compute_ssim_from_paths(figma_png=a, rendered_png=b)

    assert verdict.bucket in ("pass", "warn")
    assert verdict.ok is True


# --------------------------------------------------------------------------
# Clear difference: fail bucket.
# --------------------------------------------------------------------------


def test_inverted_image_drops_to_fail_bucket(tmp_path: Path) -> None:
    """An inverted image is structurally completely different."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _write_text_png(a, fill=(240, 240, 240))
    _write_text_png(b, fill=(20, 20, 20))  # inverted background

    verdict = compute_ssim_from_paths(figma_png=a, rendered_png=b)

    assert verdict.bucket == "fail"
    assert verdict.ok is False


def test_fail_bucket_carries_region_hint(tmp_path: Path) -> None:
    """When SSIM fails we surface *where* the difference lives so
    Cursor's reflection prompt can be specific about what to fix.
    """
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    # Disagreement isolated to the top half — the region hint
    # should mention "top".
    img_a = Image.new("RGB", (256, 256), (240, 240, 240))
    img_b = Image.new("RGB", (256, 256), (240, 240, 240))
    drawer = ImageDraw.Draw(img_b)
    drawer.rectangle((0, 0, 256, 128), fill=(0, 0, 0))
    img_a.save(a)
    img_b.save(b)

    verdict = compute_ssim_from_paths(figma_png=a, rendered_png=b)

    assert verdict.bucket == "fail"
    assert verdict.region is not None
    assert "top" in verdict.region


# --------------------------------------------------------------------------
# Resize behaviour: Figma exports rarely match the rendered DOM size.
# --------------------------------------------------------------------------


def test_unequal_sizes_are_resized_before_comparison(tmp_path: Path) -> None:
    """Different-sized inputs do not raise — the larger image is
    resampled to the smaller image's dimensions and then compared.
    A Figma 1440px frame vs a 1280px Playwright screenshot must
    not be an automatic fail.
    """
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _write_png(a, size=(512, 256))
    _write_png(b, size=(256, 128))

    verdict = compute_ssim_from_paths(figma_png=a, rendered_png=b)

    # Same fill colour at any resolution → near-identity.
    assert verdict.score == pytest.approx(1.0, abs=1e-3)


# --------------------------------------------------------------------------
# Error cases — surface them early so an activity bug doesn't return
# a bogus pass.
# --------------------------------------------------------------------------


def test_missing_figma_path_raises_file_not_found(tmp_path: Path) -> None:
    """A bad path should fail at the I/O boundary, not silently
    return ``score=0``.
    """
    a = tmp_path / "missing.png"
    b = tmp_path / "exists.png"
    _write_png(b)

    with pytest.raises(FileNotFoundError):
        compute_ssim_from_paths(figma_png=a, rendered_png=b)


def test_zero_byte_image_raises_value_error(tmp_path: Path) -> None:
    """A truncated or empty PNG should raise, not lie."""
    a = tmp_path / "empty.png"
    a.write_bytes(b"")
    b = tmp_path / "ok.png"
    _write_png(b)

    with pytest.raises(Exception, match="image"):
        compute_ssim_from_paths(figma_png=a, rendered_png=b)


def test_single_pixel_inputs_are_handled(tmp_path: Path) -> None:
    """SSIM windows require a minimum size; tiny inputs must
    upscale before comparison, not crash.
    """
    a = tmp_path / "tiny_a.png"
    b = tmp_path / "tiny_b.png"
    Image.fromarray(np.array([[[240, 240, 240]]], dtype=np.uint8)).save(a)
    Image.fromarray(np.array([[[10, 10, 10]]], dtype=np.uint8)).save(b)

    # The exact score isn't pinned — we only require that the call
    # returns a valid verdict (no ValueError from win_size).
    verdict = compute_ssim_from_paths(figma_png=a, rendered_png=b)
    assert -1.0 <= verdict.score <= 1.0
