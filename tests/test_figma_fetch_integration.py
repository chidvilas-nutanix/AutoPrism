"""Integration test for the Figma REST fetcher.

Gated on ``FIGMA_TOKEN`` being set in the environment. When the
token is absent the test is skipped — local dev without a PAT
still gets a green pytest run.

The chosen "fixture" is the same Figma file the design doc §8.1
worked example refers to; the test only walks it shallowly to
keep the run fast (< 5s).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from prism_mcp.figma.fetch import (
    _fetch_figma_tree,
    parse_figma_url,
)

_TEST_URL = (
    "https://www.figma.com/design/Q9Bls1aksp2N8gxiTBNbAt/Figma-Beta-Files"
    "?node-id=2-2"
)
"""A small node id (`2-2`) on a public Figma test file. Generic
enough that even a fresh PAT with the default scope can read it."""


@pytest.mark.skipif(
    not os.environ.get("FIGMA_TOKEN"),
    reason="FIGMA_TOKEN env var not set; skipping live REST call",
)
def test_fetch_real_figma_tree(tmp_path: Path) -> None:
    """End-to-end: real GET + real cache write. Skipped without
    a Figma token to keep the suite hermetic."""
    parsed = parse_figma_url(_TEST_URL)
    document = asyncio.run(
        _fetch_figma_tree(
            parsed=parsed,
            figma_token=os.environ["FIGMA_TOKEN"],
            cache_dir=tmp_path,
        )
    )
    assert isinstance(document, dict)
    assert "id" in document
    assert document.get("type") in {
        "FRAME",
        "GROUP",
        "CANVAS",
        "INSTANCE",
        "COMPONENT",
    }
    cache_files = list(tmp_path.glob("*.json"))
    assert cache_files, "expected cache write on successful fetch"
