"""Figma SceneNode taxonomy constants.

The Figma Plugin API defines ~38 ``type`` values; the REST API adds
a couple of synonyms (``CANVAS`` vs ``PAGE``, ``REGULAR_POLYGON`` vs
``POLYGON``). The walker routes on these and *only* these — never on
the layer ``name``, which is designer-controlled and noisy. See
design doc §2.2 and Appendix B for the routing rationale.
"""

from __future__ import annotations

MAPPABLE_TYPES: frozenset[str] = frozenset(
    {
        "FRAME",
        "INSTANCE",
        "COMPONENT",
        "COMPONENT_SET",
        "GROUP",
        "TRANSFORM_GROUP",
        "SECTION",
        "TEXT",
        "TEXT_PATH",
        "RECTANGLE",
        "ELLIPSE",
        "LINE",
        "STAR",
        "POLYGON",
        "REGULAR_POLYGON",
        "VECTOR",
        "BOOLEAN_OPERATION",
        "TABLE",
        "TABLE_CELL",
    }
)
"""Types the walker is willing to consider for routing.

Anything outside this set falls into the "unknown_type_fallback" path
in :mod:`prism_mcp.figma.routing` — treated as a passthrough GROUP and
logged to the ``dropped`` audit trail with reason
``"unknown_type_fallback"``. We never silently delete an unrecognised
node; that would erase signal for the user.
"""


DROP_TYPES: frozenset[str] = frozenset(
    {
        # Export marker — never renders.
        "SLICE",
        # FigJam-only node kinds (never appear in Figma Design files,
        # but defensive against accidentally targeting a FigJam file).
        "CONNECTOR",
        "STICKY",
        "SHAPE_WITH_TEXT",
        "STAMP",
        "WIDGET",
        "EMBED",
        "MEDIA",
        "LINK_UNFURL",
        "HIGHLIGHT",
        "WASHI_TAPE",
        "CODE_BLOCK",
        # Slides-only node kinds.
        "SLIDE",
        "SLIDE_GRID",
        "SLIDE_ROW",
        "INTERACTIVE_SLIDE_ELEMENT",
        "SLOT",
    }
)
"""Types whose presence is a hint the walker is on the wrong file.

These either don't render in a normal app page (``SLICE``) or come
from sibling Figma products (FigJam / Slides). Drop the node *and*
its subtree with reason ``"non_design_type"`` — none of these wrap
any descendant we'd want.
"""


LEAF_TYPES: frozenset[str] = frozenset(
    {
        "TEXT",
        "TEXT_PATH",
        "RECTANGLE",
        "ELLIPSE",
        "LINE",
        "STAR",
        "POLYGON",
        "REGULAR_POLYGON",
        "VECTOR",
    }
)
"""Types we treat as leaves — they may have children in Figma's
internal model (e.g. nested shape ops), but the walker doesn't
recurse past them for routing purposes.
"""


PASS5_ICON_CHILD_TYPES: frozenset[str] = frozenset(
    {
        "VECTOR",
        "RECTANGLE",
        "BOOLEAN_OPERATION",
        "LINE",
        "ELLIPSE",
        "STAR",
        "POLYGON",
        "REGULAR_POLYGON",
    }
)
"""Descendant types allowed inside a small icon-coalescing target.

The Pass-5 heuristic in :func:`prism_mcp.figma.patterns.match_icon`
treats a ≤24px container as an icon iff every descendant's type is in
this set. Empirically this catches Figma's pattern of encoding glyphs
as a stack of vector primitives without false-positiving on small
buttons (which contain TEXT or INSTANCE children).
"""


CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "FRAME",
        "GROUP",
        "TRANSFORM_GROUP",
        "SECTION",
        "COMPONENT",
        "COMPONENT_SET",
        "INSTANCE",
    }
)
"""Types that legitimately wrap other meaningful nodes.

Used by the noise filter's Pass 2 to short-circuit: if a node is a
container, we never treat it as "invisible decoration" because its
visibility is determined by its descendants, not its own fills.
"""
