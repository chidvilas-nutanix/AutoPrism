# ruff: noqa: N803
# Sharma 2005 published test vectors use uppercase Lab channel names
# (L1, L2) so the test signature mirrors the paper directly.
"""Reference-value tests for the hand-rolled color math.

CIEDE2000 has more edge cases than any sane person wants to verify
by hand, so we pin every change to Sharma 2005's published test
vectors — if these pass, the implementation is correct. Same for
Oklab vs Ottosson's own published examples.

Refs:
    Sharma 2005 supplementary test data:
    http://www2.ece.rochester.edu/~gsharma/ciede2000/
    Ottosson 2020 Oklab post:
    https://bottosson.github.io/posts/oklab/
"""

from __future__ import annotations

import numpy as np
import pytest

from prism_mcp.color import (
    apca_contrast,
    apca_luminance,
    delta_e_2000,
    hex_to_lab,
    hex_to_oklab,
    hex_to_rgb,
    linear_to_oklab,
    oklab_distance,
    srgb_to_linear,
    wcag_contrast_ratio,
    wcag_relative_luminance,
    xyz_to_lab,
)

# --------------------------------------------------------------------------
# hex parsing
# --------------------------------------------------------------------------


def test_hex_to_rgb_handles_six_digit_with_hash() -> None:
    """``#1B6BCC`` → (0.106, 0.420, 0.800)."""
    rgb = hex_to_rgb("#1B6BCC")
    np.testing.assert_allclose(
        rgb, [0x1B / 255.0, 0x6B / 255.0, 0xCC / 255.0], rtol=1e-7
    )


def test_hex_to_rgb_handles_no_hash_lowercase() -> None:
    """``1b6bcc`` (no hash, lowercase) parses the same as ``#1B6BCC``."""
    np.testing.assert_allclose(hex_to_rgb("1b6bcc"), hex_to_rgb("#1B6BCC"))


def test_hex_to_rgb_expands_three_digit_shorthand() -> None:
    """``#fff`` → ``#ffffff`` → (1, 1, 1)."""
    np.testing.assert_allclose(hex_to_rgb("#fff"), [1.0, 1.0, 1.0])
    np.testing.assert_allclose(hex_to_rgb("#0f6"), hex_to_rgb("#00ff66"))


def test_hex_to_rgb_drops_alpha_channel() -> None:
    """``#1B6BCCFF`` and ``#1B6BCC80`` map to the same RGB triple."""
    np.testing.assert_allclose(hex_to_rgb("#1B6BCC80"), hex_to_rgb("#1B6BCC"))


def test_hex_to_rgb_rejects_garbage() -> None:
    """Anything that isn't 3/6/8 hex digits is a ValueError."""
    with pytest.raises(ValueError):
        hex_to_rgb("not a color")
    with pytest.raises(ValueError):
        hex_to_rgb("#12345")  # 5 digits


# --------------------------------------------------------------------------
# sRGB transfer curve
# --------------------------------------------------------------------------


def test_srgb_to_linear_low_branch_is_linear() -> None:
    """Below 0.04045, the function is c/12.92 (linear segment)."""
    # 0.04 / 12.92 ≈ 0.003095
    np.testing.assert_allclose(
        srgb_to_linear(np.array([0.04, 0.04, 0.04])),
        [0.04 / 12.92, 0.04 / 12.92, 0.04 / 12.92],
    )


def test_srgb_to_linear_high_branch_known_values() -> None:
    """50% gray sRGB → ~0.2140 linear (a well-known checkpoint)."""
    linear = srgb_to_linear(np.array([0.5, 0.5, 0.5]))
    np.testing.assert_allclose(linear, [0.2140, 0.2140, 0.2140], rtol=1e-3)


def test_srgb_to_linear_preserves_endpoints() -> None:
    """0 stays 0 and 1 stays 1 — the curve passes through both."""
    np.testing.assert_allclose(srgb_to_linear(np.array([0.0])), [0.0])
    np.testing.assert_allclose(srgb_to_linear(np.array([1.0])), [1.0])


# --------------------------------------------------------------------------
# CIE Lab (D65)
# --------------------------------------------------------------------------


def test_white_in_lab_is_100_0_0() -> None:
    """``#FFFFFF`` → Lab (100, 0, 0) by definition of the D65 reference."""
    lab = hex_to_lab("#FFFFFF")
    np.testing.assert_allclose(lab, [100.0, 0.0, 0.0], atol=0.01)


def test_black_in_lab_is_0_0_0() -> None:
    """``#000000`` → Lab (0, 0, 0)."""
    lab = hex_to_lab("#000000")
    np.testing.assert_allclose(lab, [0.0, 0.0, 0.0], atol=0.01)


def test_pure_red_lab_well_known() -> None:
    """``#FF0000`` → approximately Lab(53.24, 80.09, 67.20)."""
    lab = hex_to_lab("#FF0000")
    np.testing.assert_allclose(lab, [53.24, 80.09, 67.20], atol=0.5)


def test_xyz_to_lab_batched() -> None:
    """The function broadcasts cleanly over an (N, 3) input."""
    xyz = np.array([[0.5, 0.5, 0.5], [1.0, 1.0, 1.0]])
    lab = xyz_to_lab(xyz)
    assert lab.shape == (2, 3)


# --------------------------------------------------------------------------
# Oklab (Ottosson published reference values)
# --------------------------------------------------------------------------


def test_oklab_white_is_one_zero_zero() -> None:
    """``#FFFFFF`` → Oklab(1, 0, 0). Direct from Ottosson's post."""
    oklab = hex_to_oklab("#FFFFFF")
    np.testing.assert_allclose(oklab, [1.0, 0.0, 0.0], atol=1e-3)


def test_oklab_black_is_origin() -> None:
    """``#000000`` → Oklab(0, 0, 0)."""
    oklab = hex_to_oklab("#000000")
    np.testing.assert_allclose(oklab, [0.0, 0.0, 0.0], atol=1e-3)


def test_oklab_pure_red_matches_ottosson_reference() -> None:
    """Pure red (#FF0000) → Oklab(0.6279, 0.2249, 0.1258).

    The reference triple comes from Ottosson 2020. We allow a 1e-3
    tolerance for round-off through the cube-root.
    """
    oklab = hex_to_oklab("#FF0000")
    np.testing.assert_allclose(oklab, [0.6280, 0.2249, 0.1258], atol=2e-3)


def test_oklab_pure_green_matches_ottosson_reference() -> None:
    """Pure green (#00FF00) → approx Oklab(0.866, -0.234, 0.179)."""
    oklab = hex_to_oklab("#00FF00")
    np.testing.assert_allclose(oklab, [0.8664, -0.2339, 0.1795], atol=2e-3)


def test_oklab_pure_blue_matches_ottosson_reference() -> None:
    """Pure blue (#0000FF) → approx Oklab(0.452, -0.032, -0.312)."""
    oklab = hex_to_oklab("#0000FF")
    np.testing.assert_allclose(oklab, [0.4520, -0.0324, -0.3115], atol=2e-3)


def test_oklab_distance_zero_for_identical_colors() -> None:
    """Self-distance is 0 to floating-point precision."""
    o = hex_to_oklab("#1B6BCC")
    np.testing.assert_allclose(oklab_distance(o, o), 0.0, atol=1e-12)


def test_oklab_distance_red_vs_green_is_large() -> None:
    """Pure red vs pure green sits well above 0.5 in Oklab Euclidean."""
    d = oklab_distance(hex_to_oklab("#FF0000"), hex_to_oklab("#00FF00"))
    assert d > 0.5


# --------------------------------------------------------------------------
# CIEDE2000 - Sharma 2005 published reference vectors
# --------------------------------------------------------------------------

# Sharma 2005, Table 1. Each row: (L1, a1, b1, L2, a2, b2, expected ΔE2000).
# The vectors deliberately stress every branch of the formula:
# rows 1-4 hit the blue region, row 5 the achromatic neutral path,
# row 6 the hue-wrap, and so on. If any of these fails the formula is
# wrong somewhere.
SHARMA_2005_REFERENCE_VECTORS = [
    (50.0, 2.6772, -79.7751, 50.0, 0.0, -82.7485, 2.0425),
    (50.0, 3.1571, -77.2803, 50.0, 0.0, -82.7485, 2.8615),
    (50.0, 2.8361, -74.0200, 50.0, 0.0, -82.7485, 3.4412),
    (50.0, -1.3802, -84.2814, 50.0, 0.0, -82.7485, 1.0000),
    (50.0, -1.1848, -84.8006, 50.0, 0.0, -82.7485, 1.0000),
    (50.0, -0.9009, -85.5211, 50.0, 0.0, -82.7485, 1.0000),
    (50.0, 0.0, 0.0, 50.0, -1.0, 2.0, 2.3669),
    (50.0, -1.0, 2.0, 50.0, 0.0, 0.0, 2.3669),
    (50.0, 2.4900, -0.0010, 50.0, -2.4900, 0.0009, 7.1792),
    (50.0, 2.4900, -0.0010, 50.0, -2.4900, 0.0010, 7.1792),
    (50.0, 2.4900, -0.0010, 50.0, -2.4900, 0.0011, 7.2195),
    (50.0, 2.4900, -0.0010, 50.0, -2.4900, 0.0012, 7.2195),
    (50.0, -0.0010, 2.4900, 50.0, 0.0009, -2.4900, 4.8045),
    (50.0, -0.0010, 2.4900, 50.0, 0.0010, -2.4900, 4.8045),
    (50.0, -0.0010, 2.4900, 50.0, 0.0011, -2.4900, 4.7461),
    (50.0, 2.5, 0.0, 50.0, 0.0, -2.5, 4.3065),
    (50.0, 2.5, 0.0, 73.0, 25.0, -18.0, 27.1492),
    (50.0, 2.5, 0.0, 61.0, -5.0, 29.0, 22.8977),
    (50.0, 2.5, 0.0, 56.0, -27.0, -3.0, 31.9030),
    (50.0, 2.5, 0.0, 58.0, 24.0, 15.0, 19.4535),
    (50.0, 2.5, 0.0, 50.0, 3.1736, 0.5854, 1.0000),
    (50.0, 2.5, 0.0, 50.0, 3.2972, 0.0, 1.0000),
    (50.0, 2.5, 0.0, 50.0, 1.8634, 0.5757, 1.0000),
    (50.0, 2.5, 0.0, 50.0, 3.2592, 0.3350, 1.0000),
    (60.2574, -34.0099, 36.2677, 60.4626, -34.1751, 39.4387, 1.2644),
    (63.0109, -31.0961, -5.8663, 62.8187, -29.7946, -4.0864, 1.2630),
    (61.2901, 3.7196, -5.3901, 61.4292, 2.2480, -4.9620, 1.8731),
    (35.0831, -44.1164, 3.7933, 35.0232, -40.0716, 1.5901, 1.8645),
    (22.7233, 20.0904, -46.6940, 23.0331, 14.9730, -42.5619, 2.0373),
    (36.4612, 47.8580, 18.3852, 36.2715, 50.5065, 21.2231, 1.4146),
    (90.8027, -2.0831, 1.4410, 91.1528, -1.6435, 0.0447, 1.4441),
    (90.9257, -0.5406, -0.9208, 88.6381, -0.8985, -0.7239, 1.5381),
    (6.7747, -0.2908, -2.4247, 5.8714, -0.0985, -2.2286, 0.6377),
    (2.0776, 0.0795, -1.1350, 0.9033, -0.0636, -0.5514, 0.9082),
]


@pytest.mark.parametrize(
    ("L1", "a1", "b1", "L2", "a2", "b2", "expected"),
    SHARMA_2005_REFERENCE_VECTORS,
)
def test_ciede2000_matches_sharma_reference(
    L1: float,
    a1: float,
    b1: float,
    L2: float,
    a2: float,
    b2: float,
    expected: float,
) -> None:
    """Every Sharma 2005 reference pair lands within 1e-4 of expected."""
    got = float(
        delta_e_2000(
            np.array([L1, a1, b1]),
            np.array([L2, a2, b2]),
        )
    )
    assert got == pytest.approx(expected, abs=1e-4)


def test_ciede2000_symmetric() -> None:
    """ΔE2000(A,B) == ΔE2000(B,A) for any sensible color pair."""
    lab_a = hex_to_lab("#1B6BCC")
    lab_b = hex_to_lab("#627386")
    assert delta_e_2000(lab_a, lab_b) == pytest.approx(
        delta_e_2000(lab_b, lab_a), abs=1e-12
    )


def test_ciede2000_self_distance_is_zero() -> None:
    """Self-distance is 0 to floating-point precision."""
    lab = hex_to_lab("#1B6BCC")
    assert delta_e_2000(lab, lab) == pytest.approx(0.0, abs=1e-12)


def test_ciede2000_broadcasts_over_batch() -> None:
    """A ``(3,)`` vs an ``(N, 3)`` produces an ``(N,)`` result."""
    target = hex_to_lab("#1B6BCC")
    palette = np.stack(
        [hex_to_lab("#000000"), hex_to_lab("#FFFFFF"), hex_to_lab("#1B6BCC")]
    )
    distances = delta_e_2000(target, palette)
    assert distances.shape == (3,)
    # The third entry is the target vs itself; should be 0.
    assert distances[2] == pytest.approx(0.0, abs=1e-12)


def test_linear_to_oklab_batched() -> None:
    """The conversion broadcasts over an (N, 3) input."""
    linear = srgb_to_linear(np.array([[0.5, 0.5, 0.5], [1.0, 0.0, 0.0]]))
    oklab = linear_to_oklab(linear)
    assert oklab.shape == (2, 3)


# --------------------------------------------------------------------------
# WCAG 2.1 contrast (reference: W3C errata + canonical examples)
# --------------------------------------------------------------------------


def test_wcag_luminance_white_is_one_black_is_zero() -> None:
    """Spec endpoints: L(#FFFFFF) == 1.0, L(#000000) == 0.0."""
    assert wcag_relative_luminance(hex_to_rgb("#FFFFFF")) == pytest.approx(
        1.0, abs=1e-12
    )
    assert wcag_relative_luminance(hex_to_rgb("#000000")) == pytest.approx(
        0.0, abs=1e-12
    )


def test_wcag_contrast_ratio_black_on_white_is_21() -> None:
    """Black-on-white = 21:1 (the published spec maximum)."""
    ratio = wcag_contrast_ratio(hex_to_rgb("#000000"), hex_to_rgb("#FFFFFF"))
    assert ratio == pytest.approx(21.0, abs=1e-9)


def test_wcag_contrast_ratio_is_symmetric() -> None:
    """The ratio doesn't care which color is fg vs bg.

    Pins that the formula uses lighter/darker, not fg/bg directly —
    a regression here would make every dark-mode call wrong.
    """
    fg, bg = hex_to_rgb("#1B6BCC"), hex_to_rgb("#FFFFFF")
    assert wcag_contrast_ratio(fg, bg) == pytest.approx(
        wcag_contrast_ratio(bg, fg), abs=1e-12
    )


def test_wcag_contrast_identical_colors_is_one() -> None:
    """Self-pair has ratio 1.0 by construction (lighter == darker)."""
    rgb = hex_to_rgb("#5DBA00")
    assert wcag_contrast_ratio(rgb, rgb) == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize(
    ("fg", "bg", "expected"),
    [
        # WCAG canonical worked example: #777 on #FFF ≈ 4.48:1
        # (fails AA body text by a hair; passes AA large text).
        ("#777777", "#FFFFFF", 4.48),
        # Common Nutanix blue on white — well above AAA threshold.
        ("#1B6BCC", "#FFFFFF", 5.18),
        # White-on-success-green: manual derivation gives ~2.45.
        # 5DBA00 linear ≈ (0.112, 0.497, 0); WCAG L ≈ 0.379;
        # ratio = (1.0 + 0.05) / (0.379 + 0.05) ≈ 2.45.
        ("#FFFFFF", "#5DBA00", 2.45),
    ],
)
def test_wcag_contrast_ratio_reference_values(
    fg: str, bg: str, expected: float
) -> None:
    """Spot-check WCAG ratios against the published / hand-derived values.

    Tolerance is 1% — published references typically report to 2 dp,
    and floating-point cumulative error from the gamma decode is
    well below that.
    """
    ratio = wcag_contrast_ratio(hex_to_rgb(fg), hex_to_rgb(bg))
    assert ratio == pytest.approx(expected, rel=0.01)


# --------------------------------------------------------------------------
# APCA Lc (reference: apca-w3 v0.1.9 published examples)
# --------------------------------------------------------------------------


def test_apca_luminance_white_is_one_black_is_clamped() -> None:
    """White ≈ 1.0; pure black gets the soft black-level clamp.

    APCA coefficients (0.2126729 + 0.7151522 + 0.0721750) sum to
    1.0000001 by design — Myndex chose the values to preserve a
    specific perceptual property at the expense of unity. So white
    is ≈ 1.0 up to ~1e-6, not exactly 1.0.
    """
    assert apca_luminance(hex_to_rgb("#FFFFFF")) == pytest.approx(1.0, abs=1e-6)
    # Pure black: Y=0 → clamp adds (blkThrs - 0)^blkClmp = 0.022^1.414
    expected_clamp = 0.022**1.414
    assert apca_luminance(hex_to_rgb("#000000")) == pytest.approx(
        expected_clamp, rel=1e-6
    )


def test_apca_contrast_normal_polarity_is_positive() -> None:
    """Dark text on light bg returns a POSITIVE Lc.

    Pins the polarity invariant that distinguishes APCA from WCAG.
    """
    lc = apca_contrast(hex_to_rgb("#000000"), hex_to_rgb("#FFFFFF"))
    assert lc > 0
    # Black on white is the maximum normal-polarity Lc; published
    # value is ~106 with rounding.
    assert lc == pytest.approx(106.0, abs=1.0)


def test_apca_contrast_reverse_polarity_is_negative() -> None:
    """Light text on dark bg returns a NEGATIVE Lc (polarity flip).

    Reverse polarity uses different exponents than normal polarity,
    so |Lc_reverse| ≈ 108 for the white-on-black pair (vs the +106
    for black-on-white above).
    """
    lc = apca_contrast(hex_to_rgb("#FFFFFF"), hex_to_rgb("#000000"))
    assert lc < 0
    assert lc == pytest.approx(-108.0, abs=1.5)


def test_apca_contrast_identical_colors_is_clamped_to_zero() -> None:
    """Self-pair has Sapc=0 → below loClip → Lc=0.0."""
    rgb = hex_to_rgb("#5DBA00")
    assert apca_contrast(rgb, rgb) == pytest.approx(0.0, abs=1e-12)


def test_apca_contrast_below_clip_returns_zero() -> None:
    """A faint near-grey pair has |Sapc| < loClip → reported as 0.

    Pins the "too low to measure" clamp. We pick two greys that are
    visually almost identical; the formula should refuse to report
    a value rather than emit a misleading tiny number.
    """
    lc = apca_contrast(hex_to_rgb("#888888"), hex_to_rgb("#8B8B8B"))
    assert lc == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize(
    ("text", "bg", "expected_lc"),
    [
        # Hand-derived from the APCA-W3 0.1.9 formula:
        # text=#111111 (Y≈0.00528 after clamp), bg=#E8E6DD (Y≈0.787).
        # Normal polarity: (0.787^0.56 - 0.00528^0.57) * 1.14 ≈ 0.939;
        # Sapc > 0 + offset: (0.939 - 0.027) * 100 ≈ 91.2
        ("#111111", "#E8E6DD", 91.2),
        # Mid-grey #888888 (136/255=0.5333) on white:
        # Y_txt ≈ 0.5333^2.4 ≈ 0.221, Y_bg ≈ 1.0.
        # Normal: (1.0^0.56 - 0.221^0.57) * 1.14 ≈ 0.658; Lc ≈ 63.1
        ("#888888", "#FFFFFF", 63.1),
        # Same colors reversed give the opposite-polarity Lc, which is
        # NOT just the negation because the exponents differ.
        # Reverse: (0.221^0.65 - 1.0^0.62) * 1.14 ≈ -0.713;
        # Lc ≈ (-0.713 + 0.027) * 100 ≈ -68.6
        ("#FFFFFF", "#888888", -68.6),
    ],
)
def test_apca_contrast_reference_values(
    text: str, bg: str, expected_lc: float
) -> None:
    """Pin Lc against hand-derived APCA-W3 0.1.9 values (±1 Lc tolerance).

    A 1-point tolerance is the standard in the APCA discussions —
    implementations can vary at the last bit from transcendental-
    power-function rounding.
    """
    lc = apca_contrast(hex_to_rgb(text), hex_to_rgb(bg))
    assert lc == pytest.approx(expected_lc, abs=1.0)


def test_wcag_contrast_ratio_batches_over_palette() -> None:
    """A ``(3,)`` fg vs ``(N, 3)`` palette returns an ``(N,)`` array."""
    fg = hex_to_rgb("#FFFFFF")
    palette = np.stack(
        [hex_to_rgb("#000000"), hex_to_rgb("#1B6BCC"), hex_to_rgb("#FFFFFF")]
    )
    ratios = wcag_contrast_ratio(fg, palette)
    assert ratios.shape == (3,)
    # Black vs white should be the max.
    assert ratios[0] == pytest.approx(21.0, abs=1e-9)
    # White vs white is 1.0.
    assert ratios[2] == pytest.approx(1.0, abs=1e-12)


def test_apca_contrast_batches_over_palette() -> None:
    """APCA also broadcasts cleanly over batches."""
    fg = hex_to_rgb("#000000")
    palette = np.stack(
        [hex_to_rgb("#FFFFFF"), hex_to_rgb("#000000"), hex_to_rgb("#888888")]
    )
    lcs = apca_contrast(fg, palette)
    assert lcs.shape == (3,)
    # First entry: black on white → high positive Lc.
    assert lcs[0] > 100
    # Second: black on black → clamped to 0.
    assert lcs[1] == pytest.approx(0.0, abs=1e-12)
    # Third: black on mid-grey → positive but smaller.
    assert 0 < lcs[2] < 100
