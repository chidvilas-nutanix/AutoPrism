"""Tests for the Figma walker Pydantic shapes (Phase 1 contracts).

These models are the typed boundary between the MCP tool, the
walker, and the eventual Cursor skill. Locking them down before any
walker behaviour exists means a schema drift surfaces as a clear
``ValidationError`` instead of silently-ignored input.

See ``docs/figma-page-to-prism-plan.md`` §4.1 (input), §4.2 (output).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prism_mcp.figma.models import (
    DroppedNode,
    FigmaTreeMapping,
    LayoutNode,
    MapFigmaTreeInput,
    MappedRegion,
)
from prism_mcp.workflow.figma_mapping import FigmaNodeMapping

# --------------------------------------------------------------------------
# MapFigmaTreeInput: the MCP tool input boundary.
# --------------------------------------------------------------------------


def test_map_figma_tree_input_minimal_required_fields() -> None:
    """Only ``node_url`` is required; everything else has a default."""
    input_obj = MapFigmaTreeInput(node_url="https://figma.com/design/abc")
    assert input_obj.node_url == "https://figma.com/design/abc"
    assert input_obj.reference_jsx is None
    assert input_obj.variable_defs is None
    assert input_obj.figma_token is None
    assert input_obj.max_depth == 20
    assert input_obj.max_nodes == 5000
    assert input_obj.max_agenda == 50
    assert input_obj.bypass_cache is False


def test_map_figma_tree_input_extra_field_rejected() -> None:
    """``extra="forbid"`` keeps schema drift from leaking through."""
    with pytest.raises(ValidationError):
        MapFigmaTreeInput(node_url="https://figma.com/design/abc", foo="bar")


def test_map_figma_tree_input_accepts_all_optional_fields() -> None:
    """All four optional inputs round-trip cleanly."""
    input_obj = MapFigmaTreeInput(
        node_url="https://figma.com/design/abc?node-id=1-2",
        reference_jsx="<Tile />",
        variable_defs={
            "#1166EE": "color/primary/500",
        },
        figma_token="figd_abc",
        max_depth=10,
        max_nodes=100,
        max_agenda=5,
        bypass_cache=True,
    )
    assert input_obj.variable_defs == {"#1166EE": "color/primary/500"}
    assert input_obj.bypass_cache is True


# --------------------------------------------------------------------------
# DroppedNode.
# --------------------------------------------------------------------------


def test_dropped_node_minimal() -> None:
    """Every drop must carry id / name / type / reason."""
    dn = DroppedNode(
        id="626:990",
        name="Row",
        type="RECTANGLE",
        reason="invisible_decoration",
    )
    assert dn.detail == ""


def test_dropped_node_extra_rejected() -> None:
    with pytest.raises(ValidationError):
        DroppedNode(
            id="1:1",
            name="x",
            type="RECTANGLE",
            reason="invisible_decoration",
            unknown="ignored",
        )


# --------------------------------------------------------------------------
# LayoutNode.
# --------------------------------------------------------------------------


def test_layout_node_minimal() -> None:
    ln = LayoutNode(
        id="626:987",
        name="Tile",
        role="component-instance",
        bbox=(940.0, 521.0, 320.0, 309.0),
    )
    assert ln.children_ids == []


def test_layout_node_bbox_must_be_4_tuple() -> None:
    with pytest.raises(ValidationError):
        LayoutNode(
            id="x",
            name="x",
            role="x",
            bbox=(1.0, 2.0, 3.0),
        )


def test_layout_node_extra_rejected() -> None:
    with pytest.raises(ValidationError):
        LayoutNode(
            id="x",
            name="x",
            role="x",
            bbox=(0.0, 0.0, 1.0, 1.0),
            ghost=True,
        )


# --------------------------------------------------------------------------
# MappedRegion — the agenda row.
# --------------------------------------------------------------------------


def _make_node_mapping() -> FigmaNodeMapping:
    """Construct a minimal ``FigmaNodeMapping`` for the test cases."""
    return FigmaNodeMapping(
        node_name="Tile",
        suggested_component_name="Tile",
    )


def test_mapped_region_minimal_required_fields() -> None:
    region = MappedRegion(
        id="626:987",
        name="Tile",
        role="component-instance",
        bbox=(940.0, 521.0, 320.0, 309.0),
        mapping=_make_node_mapping(),
    )
    assert region.aliased_ids == []
    assert region.parent_chain == []
    assert region.content_slots == {}
    assert region.structural_hints == []
    assert region.children_summary == ""
    assert region.hex_colors == []
    assert region.reference_jsx_slice is None


def test_mapped_region_extra_rejected() -> None:
    with pytest.raises(ValidationError):
        MappedRegion(
            id="x",
            name="x",
            role="x",
            bbox=(0.0, 0.0, 1.0, 1.0),
            mapping=_make_node_mapping(),
            stray="nope",
        )


def test_mapped_region_content_slots_accepts_mixed_types() -> None:
    """``content_slots`` is ``str | list[str] | int`` — patterns
    emit each shape (title vs items vs cell_count)."""
    region = MappedRegion(
        id="x",
        name="x",
        role="stat-list",
        bbox=(0.0, 0.0, 1.0, 1.0),
        content_slots={
            "title": "Top 5 Shares by Connections",
            "items": ["CBTest", "Intern19Share"],
            "cell_count": 26,
        },
        mapping=_make_node_mapping(),
    )
    assert region.content_slots["cell_count"] == 26
    assert isinstance(region.content_slots["items"], list)


# --------------------------------------------------------------------------
# FigmaTreeMapping aggregate.
# --------------------------------------------------------------------------


def test_figma_tree_mapping_defaults_empty() -> None:
    """An entirely-empty mapping is constructible — used by the
    walker stub before passes are wired in."""
    mapping = FigmaTreeMapping()
    assert mapping.layout_tree == []
    assert mapping.agenda == []
    assert mapping.tokens == {}
    assert mapping.dropped == []
    assert mapping.summary == {}
    assert mapping.warnings == []


def test_figma_tree_mapping_extra_rejected() -> None:
    with pytest.raises(ValidationError):
        FigmaTreeMapping(unexpected_field=1)


def test_figma_tree_mapping_round_trips_via_model_dump() -> None:
    """Pydantic's JSON-safe dump must preserve every field — the
    MCP transport layer serialises every tool return through JSON,
    and any field that doesn't survive that loop is silently
    dropped from the LLM's view."""
    mapping = FigmaTreeMapping(
        layout_tree=[
            LayoutNode(
                id="626:987",
                name="Tile",
                role="component-instance",
                bbox=(940.0, 521.0, 320.0, 309.0),
                children_ids=["626:988"],
            ),
        ],
        agenda=[
            MappedRegion(
                id="626:987",
                aliased_ids=["626:986"],
                name="Tile",
                role="component-instance",
                bbox=(940.0, 521.0, 320.0, 309.0),
                hex_colors=["#22272E"],
                mapping=_make_node_mapping(),
            ),
        ],
        tokens={"#22272E": "color/text/primary"},
        dropped=[
            DroppedNode(
                id="626:986",
                name="Impacted Cluster Copy",
                type="GROUP",
                reason="same_bbox_passthrough_collapsed",
                detail="collapsed into 626:987 (Tile)",
            ),
        ],
        summary={
            "input_nodes": 19,
            "kept_for_mapping": 1,
            "dropped_total": 1,
        },
        warnings=[],
    )

    dumped = mapping.model_dump()
    assert dumped["agenda"][0]["aliased_ids"] == ["626:986"]
    assert dumped["dropped"][0]["reason"] == "same_bbox_passthrough_collapsed"
    restored = FigmaTreeMapping.model_validate(dumped)
    assert restored == mapping
