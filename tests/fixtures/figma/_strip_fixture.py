"""Strip a raw Figma REST response to walker-relevant fields only.

The walker (and its helpers) read a small, well-defined subset of the
Figma SceneNode JSON. Real REST responses for a full page are huge
(~2 MB) and dominated by ``layoutGrids``, ``constraints``,
``exportSettings``, ``individualCornerRadii``, ``fillGeometry``,
``strokeGeometry``, ``pluginData`` and other fields the walker never
touches. Committing the raw response to ``tests/fixtures/figma`` would
balloon the repo for no testing benefit.

This script transforms a raw response into a "stripped" fixture that
is byte-for-byte equivalent for every walker assertion. Run it
ad-hoc from the repo root:

    .venv/bin/python -m tests.fixtures.figma._strip_fixture \\
        --src ~/.cache/prism-mcp/figma/<file>-<id>-<depth>.json \\
        --dst tests/fixtures/figma/figma-<label>.json

The :data:`_KEEP_FIELDS` set is the **single source of truth** for
"what the walker actually reads". Any time you add a new Figma
field to one of the walker / patterns / utils helpers, add it here
too — otherwise the stripped fixture will silently drop the data
and the corresponding spot-check test will start to lie.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_KEEP_FIELDS: frozenset[str] = frozenset(
    {
        # Identity / topology --------------------------------------
        "id",
        "name",
        "type",
        "visible",
        "children",
        "absoluteBoundingBox",
        # TEXT-specific --------------------------------------------
        "characters",
        "style",
        # Paint / decoration ---------------------------------------
        "fills",
        "strokes",
        "strokeWeight",
        "strokeAlign",
        "opacity",
        "effects",
        # Geometry / corner --------------------------------------- (new)
        "cornerRadius",
        "rectangleCornerRadii",
        # Auto-layout properties --------------------------------- (new)
        "layoutMode",
        "layoutPositioning",
        "primaryAxisSizingMode",
        "counterAxisSizingMode",
        "primaryAxisAlignItems",
        "counterAxisAlignItems",
        "itemSpacing",
        "counterAxisSpacing",
        "paddingTop",
        "paddingRight",
        "paddingBottom",
        "paddingLeft",
        "clipsContent",
        # Component identity ---------------------------------------
        "componentId",
        "componentSetId",
    }
)
"""Fields kept by the stripper. Anything not listed is dropped.

When you teach a walker helper to read a new field, add it here in
the *same commit* and re-strip the fixtures so the regression tests
exercise the new code path against real data. Without that step the
walker quietly works in production but the fixtures hide bugs.
"""


def strip(node: Any) -> Any:
    """Return a deep copy of ``node`` containing only
    :data:`_KEEP_FIELDS`.

    Lists outside the ``children`` key are passed through unchanged
    so paint arrays (``fills``, ``strokes``, ``effects``) keep
    their structure. The ``children`` key is recursed into.
    """
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k not in _KEEP_FIELDS:
            continue
        if k == "children" and isinstance(v, list):
            out[k] = [strip(c) for c in v]
        else:
            out[k] = v
    return out


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Strip a raw Figma REST response into a walker fixture."
    )
    parser.add_argument(
        "--src",
        type=Path,
        required=True,
        help="Path to the raw unwrapped document JSON (e.g. the contents "
        "of `response['nodes'][nodeId]['document']`).",
    )
    parser.add_argument(
        "--dst",
        type=Path,
        required=True,
        help="Output path for the stripped fixture JSON.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    src_bytes = args.src.read_text(encoding="utf-8")
    raw = json.loads(src_bytes)
    stripped = strip(raw)
    args.dst.write_text(
        json.dumps(stripped, separators=(",", ":")), encoding="utf-8"
    )
    print(
        f"stripped {args.src} ({len(src_bytes)} B) -> "
        f"{args.dst} ({args.dst.stat().st_size} B)"
    )


if __name__ == "__main__":
    main()
