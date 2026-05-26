# ruff: noqa: N806
# Color science uses paper-direct uppercase variable names
# (L, a, b, C1, C_bar, dLp, kL, kC, kH, SL, SC, SH, RT, RC, T, G, H_bar,
# etc.) — renaming to lowercase would make this code unreadable to
# anyone cross-referencing Sharma 2005 / Ottosson 2020.
"""Color science for the slice-11 ``map_token`` tool.

We hand-roll the math here rather than reaching for ``colour-science``
or ``colormath`` because:

* The two formulae we need (Oklab, CIEDE2000) are mathematically frozen
  — Ottosson's Oklab matrices haven't moved since 2020 and CIEDE2000
  has been the industry standard for 25 years.
* The friend's POC stays small: every new dep is a supply-chain
  surface, a CI build minute, and a docker layer.
* All math is one ``numpy`` matmul or one closed-form expression; we
  already have ``numpy>=2.0`` from slice 9.

What's here, in order of how a request flows:

1. :func:`hex_to_rgb` — ``"#1B6BCC"`` → ``(0.106, 0.420, 0.800)`` in
   the ``[0, 1]`` range.
2. :func:`srgb_to_linear` — undo the sRGB transfer curve so the
   subsequent matrix multiplications operate on physical-light values.
3. :func:`linear_to_xyz` — D65-illuminant transform; needed by Lab.
4. :func:`xyz_to_lab` — CIE Lab D65 reference white.
5. :func:`linear_to_oklab` — Ottosson 2020 (LMS-cubed-root pipeline).
6. :func:`delta_e_2000` — CIEDE2000 distance in CIE Lab (the
   industry-standard "just noticeable" scale).
7. :func:`oklab_distance` — plain Euclidean in Oklab (modern
   perceptually-uniform fast metric used by Tailwind v4, shadcn/ui).

All functions accept either a single 3-tuple/array or an ``(N, 3)``
batch and return a matching shape — vectorisation is essential when
ranking against ~60 color tokens per query.

References:
    Ottosson, B. (2020) "A perceptual color space for image
    processing." https://bottosson.github.io/posts/oklab/

    Sharma, G., Wu, W., Dalal, E. (2005) "The CIEDE2000 color-
    difference formula: Implementation notes, supplementary test
    data, and mathematical observations."
"""

from __future__ import annotations

import re

import numpy as np

# --------------------------------------------------------------------------
# sRGB hex parsing
# --------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def hex_to_rgb(hex_str: str) -> np.ndarray:
    """Parse ``"#RRGGBB"`` / ``"#RGB"`` / ``"#RRGGBBAA"`` to sRGB ``[0, 1]``.

    The alpha channel (when present) is dropped — token matching is
    a property of the chromatic dimensions only; an opaque ``#FF0000``
    and a 50%-transparent ``#FF000080`` are the *same* color token
    for our purposes.

    Args:
        hex_str (str): hex color string with optional ``#`` prefix.
            3-digit shorthand is expanded (``"#0f6"`` → ``"#00ff66"``).

    Returns:
        np.ndarray: shape ``(3,)`` float64 array of sRGB values in
        ``[0.0, 1.0]``.

    Raises:
        ValueError: when ``hex_str`` isn't a valid 3/6/8-digit hex.
    """
    match = _HEX_RE.match(hex_str.strip())
    if not match:
        raise ValueError(f"not a valid hex color: {hex_str!r}")
    body = match.group(1)
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    # Always drop alpha (last 2 chars when len == 8).
    body = body[:6]
    r = int(body[0:2], 16) / 255.0
    g = int(body[2:4], 16) / 255.0
    b = int(body[4:6], 16) / 255.0
    return np.array([r, g, b], dtype=np.float64)


# --------------------------------------------------------------------------
# sRGB transfer curve
# --------------------------------------------------------------------------


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    """Apply the inverse sRGB EOTF so subsequent matrix math is correct.

    sRGB stores values gamma-corrected for CRT-era displays. All
    perceptually-meaningful color math (Lab, XYZ, Oklab) operates on
    linear-light values, so this transform is the first step every
    time we leave the sRGB color space.

    Args:
        rgb (np.ndarray): sRGB values in ``[0, 1]``. Shape ``(..., 3)``.

    Returns:
        np.ndarray: linear sRGB values in ``[0, 1]``, same shape.
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    threshold = 0.04045
    # The piecewise function: low values are linear, high values
    # follow the 2.4 gamma curve with a 0.055 offset.
    return np.where(
        rgb <= threshold,
        rgb / 12.92,
        np.power((rgb + 0.055) / 1.055, 2.4),
    )


# --------------------------------------------------------------------------
# CIE XYZ + CIE Lab (D65 illuminant, 2-degree observer)
# --------------------------------------------------------------------------

_M_RGB_TO_XYZ_D65 = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)

# CIE Standard Illuminant D65 reference white in XYZ, normalised so Y=1.
_D65_XYZ_WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float64)

# Lab's piecewise function thresholds.
_LAB_DELTA = 6.0 / 29.0
_LAB_DELTA_CUBED = _LAB_DELTA**3
_LAB_KAPPA = (1.0 / 3.0) * (29.0 / 6.0) ** 2  # 1/(3 * delta^2)


def linear_to_xyz(rgb_linear: np.ndarray) -> np.ndarray:
    """Convert linear sRGB to CIE XYZ under the D65 illuminant.

    Args:
        rgb_linear (np.ndarray): linear sRGB, shape ``(..., 3)``.

    Returns:
        np.ndarray: CIE XYZ values, same shape, Y normalised so the
        D65 white point gives Y=1.
    """
    rgb_linear = np.asarray(rgb_linear, dtype=np.float64)
    return rgb_linear @ _M_RGB_TO_XYZ_D65.T


def xyz_to_lab(xyz: np.ndarray) -> np.ndarray:
    """Convert CIE XYZ to CIE L*a*b* against the D65 reference white.

    Args:
        xyz (np.ndarray): CIE XYZ, shape ``(..., 3)``.

    Returns:
        np.ndarray: CIE Lab. L in ``[0, 100]``, a/b roughly
        ``[-128, 128]``. Same shape as input.
    """
    xyz = np.asarray(xyz, dtype=np.float64)
    norm = xyz / _D65_XYZ_WHITE
    f = np.where(
        norm > _LAB_DELTA_CUBED,
        np.cbrt(norm),
        _LAB_KAPPA * norm + 4.0 / 29.0,
    )
    fx = f[..., 0]
    fy = f[..., 1]
    fz = f[..., 2]
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)
    return np.stack([L, a, b], axis=-1)


def hex_to_lab(hex_str: str) -> np.ndarray:
    """Compose the full ``hex → sRGB → linear → XYZ → Lab`` pipeline."""
    return xyz_to_lab(linear_to_xyz(srgb_to_linear(hex_to_rgb(hex_str))))


# --------------------------------------------------------------------------
# Oklab (Ottosson 2020) - the modern perceptually-uniform space
# --------------------------------------------------------------------------

_M_RGB_LINEAR_TO_LMS = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ],
    dtype=np.float64,
)

_M_LMS_TO_OKLAB = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ],
    dtype=np.float64,
)


def linear_to_oklab(rgb_linear: np.ndarray) -> np.ndarray:
    """Convert linear sRGB to Oklab (Ottosson 2020).

    The two-matrix pipeline (with a cube-root non-linearity between)
    is the entirety of the Oklab transform — see
    https://bottosson.github.io/posts/oklab/.

    Args:
        rgb_linear (np.ndarray): linear sRGB, shape ``(..., 3)``.

    Returns:
        np.ndarray: Oklab values, shape ``(..., 3)``. L in
        approximately ``[0, 1]``, a/b roughly ``[-0.4, 0.4]``.
    """
    rgb_linear = np.asarray(rgb_linear, dtype=np.float64)
    lms = rgb_linear @ _M_RGB_LINEAR_TO_LMS.T
    # Cube root — preserves sign so a hypothetical negative LMS
    # (out-of-gamut) still produces a well-defined Oklab value.
    lms_cubed = np.sign(lms) * np.cbrt(np.abs(lms))
    return lms_cubed @ _M_LMS_TO_OKLAB.T


def hex_to_oklab(hex_str: str) -> np.ndarray:
    """Compose ``hex → sRGB → linear → Oklab``."""
    return linear_to_oklab(srgb_to_linear(hex_to_rgb(hex_str)))


def oklab_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Euclidean distance between Oklab points.

    Because Oklab is designed to be perceptually uniform, Euclidean
    distance directly approximates "how different do these look to a
    human" — no rotation / saturation corrections needed (that's the
    whole point of Oklab over Lab).

    Args:
        a (np.ndarray): Oklab values, shape ``(..., 3)``.
        b (np.ndarray): Oklab values, shape ``(..., 3)``.

    Returns:
        np.ndarray: distances, shape ``(...)``.
    """
    return np.linalg.norm(np.asarray(a) - np.asarray(b), axis=-1)


# --------------------------------------------------------------------------
# CIEDE2000 - the industry-standard human-perception color-difference
# --------------------------------------------------------------------------


def delta_e_2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """Compute the CIEDE2000 color difference between two Lab points.

    Implements the full Sharma 2005 reference formula with the five
    correction terms (SL, SC, SH, T, RT). All arguments are broadcast
    so you can pass ``(3,)`` vs ``(N, 3)`` and get back ``(N,)``.

    The result is in the standard "ΔE" scale where:
        * ``ΔE ≈ 1.0`` is the "just-noticeable difference" (JND)
          threshold for a trained observer.
        * ``ΔE ≤ 2`` colours are perceptually identical to most
          observers — what we call an ``exact_match``.
        * ``ΔE ≤ 5`` is "close enough that the LLM can confidently
          suggest this token" — our ``near_match`` bucket.
        * ``ΔE >= 10`` is obviously different — ``no_match``.

    Args:
        lab1 (np.ndarray): CIE Lab, shape ``(..., 3)``.
        lab2 (np.ndarray): CIE Lab, shape ``(..., 3)``.

    Returns:
        np.ndarray: ΔE2000 distances, shape after broadcasting.
    """
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]

    C1 = np.sqrt(a1**2 + b1**2)
    C2 = np.sqrt(a2**2 + b2**2)
    C_bar = (C1 + C2) / 2.0
    # G: the chroma-dependent rescaling that fixes Lab's blue-region
    # bias by inflating the ``a`` channel for low-chroma colours.
    G = 0.5 * (1.0 - np.sqrt(C_bar**7 / (C_bar**7 + 25.0**7)))

    a1p = (1.0 + G) * a1
    a2p = (1.0 + G) * a2
    C1p = np.sqrt(a1p**2 + b1**2)
    C2p = np.sqrt(a2p**2 + b2**2)

    # Hue in degrees, in [0, 360). atan2 returns radians in [-pi, pi];
    # we convert and wrap, with the convention that hue is undefined
    # (set to 0) when chroma is 0.
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360.0

    dLp = L2 - L1
    dCp = C2p - C1p

    # Hue difference: tricky edge cases when chroma is zero on either
    # side; protect with np.where so the formula stays vectorised.
    C_product = C1p * C2p
    dhp_raw = h2p - h1p
    dhp = np.where(
        dhp_raw > 180.0,
        dhp_raw - 360.0,
        np.where(dhp_raw < -180.0, dhp_raw + 360.0, dhp_raw),
    )
    dhp = np.where(C_product == 0.0, 0.0, dhp)
    dHp = 2.0 * np.sqrt(C_product) * np.sin(np.radians(dhp / 2.0))

    L_bar = (L1 + L2) / 2.0
    Cp_bar = (C1p + C2p) / 2.0

    # Mean hue: branch by quadrant, falling back to the unweighted
    # sum when one of the chromas is 0 (hue undefined there).
    h_sum = h1p + h2p
    h_diff_abs = np.abs(h1p - h2p)
    H_bar = np.where(
        C_product == 0.0,
        h_sum,
        np.where(
            h_diff_abs <= 180.0,
            h_sum / 2.0,
            np.where(
                h_sum < 360.0, (h_sum + 360.0) / 2.0, (h_sum - 360.0) / 2.0
            ),
        ),
    )

    # T: the hue-rotation correction. The four-term cosine sum
    # narrows down hue bands that the linear Lab model gets wrong.
    T = (
        1.0
        - 0.17 * np.cos(np.radians(H_bar - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * H_bar))
        + 0.32 * np.cos(np.radians(3.0 * H_bar + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * H_bar - 63.0))
    )

    # SL / SC / SH: per-dimension scaling that flattens lightness,
    # chroma, and hue contributions so equal numeric ΔE means equal
    # perceptual distance regardless of where you are in Lab space.
    SL = 1.0 + (0.015 * (L_bar - 50.0) ** 2) / np.sqrt(
        20.0 + (L_bar - 50.0) ** 2
    )
    SC = 1.0 + 0.045 * Cp_bar
    SH = 1.0 + 0.015 * Cp_bar * T

    # RT: rotation term, only meaningful in the blue region
    # (H ≈ 275 ± 25), zero elsewhere.
    dTheta = 30.0 * np.exp(-(((H_bar - 275.0) / 25.0) ** 2))
    RC = 2.0 * np.sqrt(Cp_bar**7 / (Cp_bar**7 + 25.0**7))
    RT = -np.sin(np.radians(2.0 * dTheta)) * RC

    # kL = kC = kH = 1 are the standard "graphic arts" weights —
    # the ones every reference implementation uses unless explicitly
    # asked otherwise.
    kL = kC = kH = 1.0

    term_L = dLp / (kL * SL)
    term_C = dCp / (kC * SC)
    term_H = dHp / (kH * SH)
    return np.sqrt(term_L**2 + term_C**2 + term_H**2 + RT * term_C * term_H)


# --------------------------------------------------------------------------
# WCAG 2.1 contrast (axe-core / slice-12 validator compatibility)
# --------------------------------------------------------------------------
#
# Reference: https://www.w3.org/TR/WCAG21/#dfn-relative-luminance
# WCAG 2.1 uses a piecewise gamma function that's almost-but-not-quite
# the standard sRGB EOTF. Don't substitute :func:`srgb_to_linear` here;
# the coefficients are tuned to the WCAG breakpoint (0.03928) which
# differs slightly from the canonical sRGB breakpoint (0.04045). The
# discrepancy is tiny in practice but axe-core / aXe / Lighthouse all
# use the WCAG formula verbatim; we match it so the slice-12 validator
# gates and our reported ratio agree to the last digit.


def wcag_relative_luminance(rgb: np.ndarray) -> np.ndarray:
    """Return WCAG 2.1 relative luminance in ``[0, 1]``.

    Per W3C WCAG 2.1 §dfn-relative-luminance:
    ``L = 0.2126 R + 0.7152 G + 0.0722 B``
    where each channel is gamma-decoded via
    ``c <= 0.03928 ? c/12.92 : ((c + 0.055)/1.055)^2.4``.

    Args:
        rgb (np.ndarray): one or more sRGB triplets in ``[0, 1]``.
            Shape ``(3,)`` for a single color or ``(N, 3)`` for batch.

    Returns:
        np.ndarray: relative luminance. Scalar for one color, ``(N,)``
        for batch.
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    linear = np.where(
        rgb <= 0.03928,
        rgb / 12.92,
        ((rgb + 0.055) / 1.055) ** 2.4,
    )
    return (
        0.2126 * linear[..., 0]
        + 0.7152 * linear[..., 1]
        + 0.0722 * linear[..., 2]
    )


def wcag_contrast_ratio(fg_rgb: np.ndarray, bg_rgb: np.ndarray) -> np.ndarray:
    """Return WCAG 2.1 contrast ratio ``(L_lighter + 0.05) / (L_darker + 0.05)``.

    Always returns a value in ``[1, 21]``: identical colors give 1.0,
    pure black on pure white gives 21.0.

    The 0.05 ambient-light offset is what makes the ratio bounded; it
    represents flare from a typical environment.

    Args:
        fg_rgb (np.ndarray): foreground sRGB triplet in ``[0, 1]``.
        bg_rgb (np.ndarray): background sRGB triplet in ``[0, 1]``.

    Returns:
        np.ndarray: contrast ratio scalar (or batch shape).
    """
    fg_lum = wcag_relative_luminance(fg_rgb)
    bg_lum = wcag_relative_luminance(bg_rgb)
    lighter = np.maximum(fg_lum, bg_lum)
    darker = np.minimum(fg_lum, bg_lum)
    return (lighter + 0.05) / (darker + 0.05)


# --------------------------------------------------------------------------
# APCA Lc (Accessible Perceptual Contrast Algorithm) — WCAG 3 draft
# --------------------------------------------------------------------------
#
# Reference: APCA-W3 version 0.1.9 "0.0.98G-4g-base-W3" by Andrew Somers
# (Myndex). The constants below are the W3 / public-beta values that
# emerging WCAG-3 / Visual Contrast guidelines use, and that the
# `apca-w3` npm package implements. Source-of-truth LaTeX:
# https://github.com/Myndex/SAPC-APCA/blob/master/documentation/APCA-W3-LaTeX.md
#
# What makes APCA different from WCAG 2.1:
#
# * **Polarity-sensitive**: dark text on a light background returns a
#   *positive* Lc; light text on a dark background returns a *negative*
#   Lc. WCAG 2.1 returns the same ratio either way, which mis-predicts
#   readability on dark themes.
# * **Soft black-level clamp** (~0.022 Y) approximates the eye's
#   sensitivity loss at very low luminance.
# * **Different exponents per polarity** because the eye reads
#   light-on-dark differently from dark-on-light at small font sizes.
#
# Lc ranges roughly ``[-108, +106]``. WCAG 3 draft guidance pairs Lc
# values with font weights/sizes via :func:`fontLookupAPCA`; we don't
# ship the lookup table here, just the raw Lc.

_APCA_MAIN_TRC = 2.4
_APCA_SR_CO = 0.2126729
_APCA_SG_CO = 0.7151522
_APCA_SB_CO = 0.0721750
_APCA_BLK_THRS = 0.022
_APCA_BLK_CLMP = 1.414
_APCA_LO_CLIP = 0.1  # |Sapc| below this is reported as Lc=0 (too low)
_APCA_LO_BOW_OFFSET = 0.027  # subtracted from positive Sapc before scaling
_APCA_LO_WOB_OFFSET = 0.027  # added to negative Sapc before scaling
_APCA_SCALE = 1.14  # bow + wob share the same scale at 0.1.9
_APCA_NORM_BG = 0.56  # exponent on bg in normal polarity
_APCA_NORM_TXT = 0.57  # exponent on txt in normal polarity
_APCA_REV_BG = 0.65  # exponent on bg in reverse polarity
_APCA_REV_TXT = 0.62  # exponent on txt in reverse polarity


def apca_luminance(rgb: np.ndarray) -> np.ndarray:
    """Return APCA-W3 estimated screen luminance Y in ``[0, 1]``.

    Uses APCA's own coefficients (not WCAG 2.1's), and applies the
    soft black-level clamp at ``Y < 0.022``. This is the input the
    APCA contrast formula expects, not a general-purpose luminance.

    Args:
        rgb (np.ndarray): sRGB triplet in ``[0, 1]``. Single or batch.

    Returns:
        np.ndarray: APCA Y, soft-clamped at the dark end.
    """
    rgb = np.asarray(rgb, dtype=np.float64)
    powered = rgb**_APCA_MAIN_TRC
    y = (
        _APCA_SR_CO * powered[..., 0]
        + _APCA_SG_CO * powered[..., 1]
        + _APCA_SB_CO * powered[..., 2]
    )
    # Soft-clamp dark luminances. The clamp is the published 0.1.9 form:
    # Y_clamped = Y + (blkThrs - Y)^blkClmp for Y < blkThrs, else Y.
    # We clip the base to >= 0 BEFORE the power because np.where
    # evaluates both branches eagerly — without the clip, the unused
    # branch raises ``invalid value encountered in power`` on Y values
    # above blkThrs (negative base, fractional exponent → NaN).
    soft = np.clip(_APCA_BLK_THRS - y, 0.0, None) ** _APCA_BLK_CLMP
    return np.where(y < _APCA_BLK_THRS, y + soft, y)


def apca_contrast(text_rgb: np.ndarray, bg_rgb: np.ndarray) -> np.ndarray:
    """Return APCA-W3 Lc for ``text`` on ``background``.

    Lc is in roughly ``[-108, +106]``:

    * **positive** → dark text on light background (normal polarity);
    * **negative** → light text on dark background (reverse polarity);
    * ``0.0`` is returned when ``|Sapc| < 0.1`` (the published "too-low
      to measure" clamp).

    Per W3 WCAG 3 draft guidance, ``|Lc|``:

    * ``>= 75`` — body text (10-14pt)
    * ``>= 60`` — headlines and large content
    * ``>= 45`` — fluent text, larger
    * ``>= 30`` — minimum for non-content (icons, etc.)
    * ``< 15`` — invisible / unusable

    Args:
        text_rgb (np.ndarray): text color sRGB in ``[0, 1]``.
        bg_rgb (np.ndarray): background color sRGB in ``[0, 1]``.

    Returns:
        np.ndarray: signed Lc. Scalar for one pair, batch shape
        otherwise.
    """
    y_txt = apca_luminance(text_rgb)
    y_bg = apca_luminance(bg_rgb)

    # Normal polarity branch: dark text on light bg (Y_bg > Y_txt).
    # Reverse polarity branch: light text on dark bg (Y_bg < Y_txt).
    # np.where lets us keep the formula fully vectorised for batch use.
    sapc_normal = (y_bg**_APCA_NORM_BG - y_txt**_APCA_NORM_TXT) * _APCA_SCALE
    sapc_reverse = (y_bg**_APCA_REV_BG - y_txt**_APCA_REV_TXT) * _APCA_SCALE
    sapc = np.where(y_bg > y_txt, sapc_normal, sapc_reverse)

    # Apply low-contrast clamp + offset → Lc.
    # Three regimes:
    #  - |Sapc| < loClip (0.1): too low to measure → 0.0
    #  - Sapc > 0: Lc = (Sapc - loBoWoffset) * 100
    #  - Sapc < 0: Lc = (Sapc + loWoBoffset) * 100
    return np.where(
        np.abs(sapc) < _APCA_LO_CLIP,
        0.0,
        np.where(
            sapc > 0,
            (sapc - _APCA_LO_BOW_OFFSET) * 100.0,
            (sapc + _APCA_LO_WOB_OFFSET) * 100.0,
        ),
    )
