"""Tests for the tarball integrity verifier."""

from __future__ import annotations

import base64
import hashlib

import pytest

from prism_mcp.integrity import IntegrityError, verify


def _payload() -> bytes:
    return b"prism-reactjs tarball bytes"


def _sri_for(data: bytes, algo: str) -> str:
    """Compose an npm-style SRI string for ``data`` using ``algo``."""
    digest = hashlib.new(algo, data).digest()
    return f"{algo}-{base64.b64encode(digest).decode('ascii')}"


def test_verify_passes_for_matching_sha512_integrity() -> None:
    """A correct sha512 SRI string returns silently."""
    data = _payload()
    integrity = _sri_for(data, "sha512")

    verify(data, integrity=integrity)


def test_verify_passes_for_matching_sha256_integrity() -> None:
    """Other SRI algorithms (sha256) are also accepted."""
    data = _payload()
    integrity = _sri_for(data, "sha256")

    verify(data, integrity=integrity)


def test_verify_raises_on_integrity_mismatch() -> None:
    """A mutated payload fails verification with a clear message."""
    integrity = _sri_for(_payload(), "sha512")

    with pytest.raises(IntegrityError, match="digest mismatch"):
        verify(b"tampered bytes", integrity=integrity)


def test_verify_raises_on_malformed_sri() -> None:
    """Strings without a known algorithm prefix are rejected."""
    with pytest.raises(IntegrityError, match="no recognized SRI"):
        verify(_payload(), integrity="md5-deadbeef")


def test_verify_falls_back_to_shasum_when_integrity_missing() -> None:
    """Without ``integrity`` we accept a matching sha1 hex digest."""
    data = _payload()
    shasum = hashlib.sha1(data, usedforsecurity=False).hexdigest()

    verify(data, shasum=shasum)


def test_verify_raises_on_shasum_mismatch() -> None:
    """A wrong sha1 fails verification."""
    with pytest.raises(IntegrityError, match="sha1 mismatch"):
        verify(_payload(), shasum="0" * 40)


def test_verify_raises_when_no_hint_supplied() -> None:
    """Refusing to verify means refusing to trust."""
    with pytest.raises(IntegrityError, match="no integrity or shasum"):
        verify(_payload())


def test_verify_integrity_first_recognized_algo_wins() -> None:
    """Multiple SRI alternatives are tried until one is recognized."""
    data = _payload()
    integrity = f"md5-deadbeef {_sri_for(data, 'sha512')}"
    verify(data, integrity=integrity)
