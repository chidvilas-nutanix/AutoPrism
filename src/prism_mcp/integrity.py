"""Integrity verification for downloaded tarballs.

npm registry metadata exposes two integrity hints per version:

* ``dist.integrity``: an SRI string like ``sha512-<base64>``. Modern
  registries always emit this. Multiple algorithms separated by spaces
  are allowed by the SRI spec; we accept the first algorithm we
  recognize.
* ``dist.shasum``: legacy sha1 hex. Always present on real npm
  publishes; kept for older mirrors that don't emit ``integrity``.

We prefer ``integrity`` when available because SRI is collision-resistant
under modern adversarial assumptions, but we fall back to ``shasum`` so
the server still works against any registry that returns either field.
"""

from __future__ import annotations

import base64
import hashlib

_SRI_ALGORITHMS = {
    "sha256": hashlib.sha256,
    "sha384": hashlib.sha384,
    "sha512": hashlib.sha512,
}


class IntegrityError(ValueError):
    """Raised when a downloaded tarball fails verification."""


def verify(
    data: bytes,
    integrity: str | None = None,
    shasum: str | None = None,
) -> None:
    """Verify ``data`` against an SRI ``integrity`` string or sha1
    ``shasum``.

    Args:
        data (bytes): the raw bytes of the downloaded tarball.
        integrity (str | None): npm SRI string such as
            ``"sha512-AbCd..."``; if multiple algorithms are present
            (space-separated) the first recognized one is used.
        shasum (str | None): legacy sha1 hex digest.

    Raises:
        IntegrityError: if neither hint was supplied, the SRI format is
            malformed, the algorithm is unknown, or the computed digest
            does not match the expected value.
    """
    if integrity:
        _verify_integrity(data, integrity)
        return
    if shasum:
        _verify_shasum(data, shasum)
        return
    raise IntegrityError(
        "no integrity or shasum supplied; refusing to trust tarball"
    )


def _verify_integrity(data: bytes, integrity: str) -> None:
    """Verify against an npm SRI string.

    Args:
        data (bytes): payload to hash.
        integrity (str): SRI string. Space-separated alternatives are
            tried in order; the first algorithm we know wins.

    Raises:
        IntegrityError: on malformed SRI, unknown algorithm, or hash
            mismatch.
    """
    for token in integrity.split():
        if "-" not in token:
            continue
        algo, _, b64 = token.partition("-")
        algo_lower = algo.lower()
        if algo_lower not in _SRI_ALGORITHMS:
            continue
        expected_bytes = _safe_b64decode(b64)
        actual_bytes = _SRI_ALGORITHMS[algo_lower](data).digest()
        if actual_bytes != expected_bytes:
            raise IntegrityError(
                f"tarball {algo_lower} digest mismatch "
                f"(expected {b64}, got "
                f"{base64.b64encode(actual_bytes).decode('ascii')})"
            )
        return

    raise IntegrityError(
        f"no recognized SRI algorithm in integrity={integrity!r}; "
        f"supported: {sorted(_SRI_ALGORITHMS)}"
    )


def _verify_shasum(data: bytes, shasum: str) -> None:
    """Verify against a legacy sha1 hex digest.

    Args:
        data (bytes): payload to hash.
        shasum (str): expected sha1 hex digest. Case-insensitive.

    Raises:
        IntegrityError: on hash mismatch.
    """
    expected = shasum.strip().lower()
    actual = hashlib.sha1(data, usedforsecurity=False).hexdigest()
    if actual != expected:
        raise IntegrityError(
            f"tarball sha1 mismatch (expected {expected}, got {actual})"
        )


def _safe_b64decode(value: str) -> bytes:
    """Decode an SRI base64 chunk, raising :class:`IntegrityError` on
    malformed input.

    Args:
        value (str): base64 payload from the SRI string.

    Returns:
        bytes: the decoded digest bytes.

    Raises:
        IntegrityError: when the input isn't valid base64.
    """
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise IntegrityError(f"malformed SRI base64: {value!r}") from exc
