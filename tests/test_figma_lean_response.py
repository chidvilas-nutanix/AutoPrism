"""Tests for the lean ``map_figma_tree`` response transform.

The walker and ``map_figma_node`` still compute the *full*
:class:`FigmaTreeMapping`; this suite pins the OUTPUT-shaping layer
added on top of it:

* :meth:`FigmaTreeMapping.to_lean_response` — the trimmed wire shape
  that ``map_figma_tree`` ships by default so the Cursor LLM's
  context window is not flooded with per-row retrieval payload
  (raw JSX ``examples``, ``a11y_blocks``, full ``candidates``) or the
  potentially-thousands-of-rows ``dropped`` audit list.
* :func:`leanify_tree_mapping` — the ``response_detail`` dispatch
  both the live-walker and curated-mock server paths route through.
  ``"full"`` MUST reproduce ``model_dump()`` byte-for-byte
  (regression-safe); ``"lean"`` MUST drop the heavy payload.

The walker / golden suites are intentionally untouched by this
change — they call ``walk_tree`` directly and never go through the
serialisation layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from prism_mcp.figma import FigmaTreeMapping, leanify_tree_mapping, walk_tree
from prism_mcp.figma.models import (
    AbsolutePos,
    BoxStyle,
    DroppedNode,
    LayoutNode,
    MapFigmaTreeInput,
    MappedRegion,
)
from prism_mcp.figma_mapping import (
    CandidateMatch,
    FigmaNodeMapping,
    TokenMapping,
)
from prism_mcp.server import build_server

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "figma"

# Sentinels that only ever appear in the *heavy* per-row retrieval
# payload. The lean response must strip every one of them; the full
# response must keep them. Using unmistakable tokens makes the
# substring assertions unambiguous.
_JSX_SENTINEL = "RAW_JSX_EXAMPLE_BODY_SENTINEL"
_A11Y_SENTINEL = "A11Y_BLOCK_SENTINEL"
_WHY_SENTINEL = "WHY_MATCHED_SENTINEL"


# --------------------------------------------------------------------------
# Builders — a richly-populated mapping so "what got dropped" is visible.
# --------------------------------------------------------------------------


def _rich_node_mapping(
    top_summary: str = "TOP_CANDIDATE_SUMMARY",
) -> FigmaNodeMapping:
    """A FigmaNodeMapping carrying every heavy field the lean path drops."""
    return FigmaNodeMapping(
        node_name="Tile",
        suggested_component_name="Tile",
        candidates=[
            CandidateMatch(
                name=f"Cand{i}",
                type="component",
                score=0.9 - i * 0.1,
                why_matched=[_WHY_SENTINEL, f"tok{i}"],
                summary=top_summary if i == 0 else f"summary for Cand{i}",
                source="both",
            )
            for i in range(5)
        ],
        related=["RelatedA", "RelatedB", "RelatedC"],
        a11y_blocks=[
            f"{_A11Y_SENTINEL} block {i}: keep aria-* tidy" for i in range(3)
        ],
        token_mappings=[
            TokenMapping(
                hex="#112233",
                token_name="color/brand/500",
                token_hex="#112233",
                bucket="near",
            )
        ],
        examples=[
            f"{_JSX_SENTINEL} <Tile>{'x' * 400}</Tile>" for _ in range(3)
        ],
        candidate_decompositions=["Tile + RelatedA", "Tile + RelatedB"],
        primary_recommendation="Tile",
        primary_recommendation_rationale="pattern role 'kpi-tile' -> Tile",
        primary_recommendation_confidence=1.0,
    )


def _rich_region() -> MappedRegion:
    """One agenda row exercising every kept + dropped MappedRegion field."""
    return MappedRegion(
        id="626:987",
        aliased_ids=["626:986", "626:985"],
        name="Tile",
        role="kpi-tile",
        bbox=(940.0, 521.0, 320.0, 309.0),
        parent_chain=["Root", "Page", "Body", "Grid", "Cell"],
        content_slots={
            "title": "Top 5 Shares",
            "items": ["a", "b"],
            "cell_count": 5,
        },
        structural_hints=["320x309 ~square", "3-row vertical stack"],
        children_summary="FRAME Header(1 TEXT)",
        hex_colors=["#22272E", "#112233"],
        box_style=BoxStyle(
            background_color="#EDF0F2",
            corner_radius=2.0,
            has_shadow=True,
        ),
        reference_jsx_slice=f"{_JSX_SENTINEL} reference slice <Tile/>",
        mapping=_rich_node_mapping(),
        absolute_pos=AbsolutePos(
            top=10.0, left=20.0, width=320.0, height=309.0, z_order=1
        ),
        shape_bucket="tile",
    )


def _rich_mapping() -> FigmaTreeMapping:
    """A full FigmaTreeMapping with a non-trivial agenda + dropped list."""
    return FigmaTreeMapping(
        layout_tree=[
            LayoutNode(
                id="626:987",
                name="Tile",
                role="kpi-tile",
                bbox=(940.0, 521.0, 320.0, 309.0),
                children_ids=["626:988"],
            )
        ],
        agenda=[_rich_region()],
        tokens={"#22272E": "color/text/primary", "#112233": "color/brand/500"},
        dropped=[
            DroppedNode(
                id=f"9:{i}",
                name=f"Decoration {i}",
                type="RECTANGLE",
                reason="invisible_decoration" if i % 2 else "tiny_decorative",
                detail="auto-generated decoration absorbed by the filter",
            )
            for i in range(20)
        ],
        summary={
            "input_nodes": 42,
            "kept_for_mapping": 1,
            "dropped_total": 20,
            "agenda_size": 1,
            "tokens_count": 2,
            "warnings_count": 0,
            "dropped_invisible_decoration": 10,
            "dropped_tiny_decorative": 10,
        },
        warnings=["heads up: one region had low confidence"],
    )


def _heavy_stub_mapper():
    """A map_figma_node stub returning a heavy per-row payload.

    Lets the x-ray-4 walk produce realistic full-size rows (raw JSX
    ``examples`` + ``a11y_blocks`` + 5 ``candidates``) WITHOUT standing
    up the real fastembed encoder / Prism index, so the lean-vs-full
    size comparison is hermetic.
    """

    def stub(**kwargs: object) -> FigmaNodeMapping:
        name = str(kwargs.get("node_name", ""))
        return FigmaNodeMapping(
            node_name=name,
            suggested_component_name="HeavyComponent",
            candidates=[
                CandidateMatch(
                    name=f"Cand{i}",
                    type="component",
                    score=0.9 - i * 0.1,
                    why_matched=[_WHY_SENTINEL, f"tok{i}", "extra", "tokens"],
                    summary="A fairly descriptive one-line candidate summary.",
                    source="both",
                )
                for i in range(5)
            ],
            related=["RelatedA", "RelatedB", "RelatedC"],
            a11y_blocks=[
                f"{_A11Y_SENTINEL} {i}: " + "guidance " * 30 for i in range(3)
            ],
            token_mappings=[
                TokenMapping(
                    hex="#112233",
                    token_name="color/brand/500",
                    token_hex="#112233",
                    bucket="near",
                )
            ],
            examples=[
                f"{_JSX_SENTINEL} <div>" + "x" * 700 + "</div>"
                for _ in range(3)
            ],
            candidate_decompositions=["HeavyComponent + RelatedA"],
            primary_recommendation="HeavyComponent",
            primary_recommendation_rationale="pattern role -> HeavyComponent",
            primary_recommendation_confidence=1.0,
        )

    return stub


def _json_len(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------
# Input field default.
# --------------------------------------------------------------------------


def test_map_figma_tree_input_defaults_to_lean() -> None:
    """The new field defaults to ``"lean"`` so existing callers get the
    trimmed payload automatically."""
    assert (
        MapFigmaTreeInput(
            node_url="https://figma.com/design/abc"
        ).response_detail
        == "lean"
    )


def test_map_figma_tree_input_accepts_full_and_rejects_other() -> None:
    full = MapFigmaTreeInput(
        node_url="https://figma.com/design/abc", response_detail="full"
    )
    assert full.response_detail == "full"
    with pytest.raises(ValidationError):
        MapFigmaTreeInput(
            node_url="https://figma.com/design/abc", response_detail="verbose"
        )


# --------------------------------------------------------------------------
# full == legacy model_dump() (byte-for-byte regression safety).
# --------------------------------------------------------------------------


def test_full_detail_is_byte_for_byte_model_dump() -> None:
    """``response_detail="full"`` reproduces today's exact output."""
    mapping = _rich_mapping()
    full = leanify_tree_mapping(mapping, "full")
    legacy = mapping.model_dump()
    assert full == legacy
    # Byte-for-byte at the JSON-serialisation level the MCP transport
    # actually ships.
    assert json.dumps(full, sort_keys=True) == json.dumps(
        legacy, sort_keys=True
    )


def test_full_detail_keeps_dropped_list_and_per_row_payload() -> None:
    mapping = _rich_mapping()
    full = leanify_tree_mapping(mapping, "full")
    assert isinstance(full["dropped"], list)
    assert len(full["dropped"]) == 20
    assert "dropped_summary" not in full
    assert "reduction" not in full
    # Per-row heavy payload survives in full mode.
    row_mapping = full["agenda"][0]["mapping"]
    assert row_mapping["examples"]
    assert row_mapping["a11y_blocks"]
    assert _JSX_SENTINEL in json.dumps(full)
    assert _A11Y_SENTINEL in json.dumps(full)


# --------------------------------------------------------------------------
# lean top-level shape.
# --------------------------------------------------------------------------


def test_lean_replaces_dropped_list_with_dropped_summary() -> None:
    mapping = _rich_mapping()
    lean = leanify_tree_mapping(mapping, "lean")
    assert "dropped" not in lean, "lean must omit the full dropped list"
    assert lean["dropped_summary"] == {
        "invisible_decoration": 10,
        "tiny_decorative": 10,
    }


def test_lean_keeps_layout_tokens_summary_warnings() -> None:
    mapping = _rich_mapping()
    lean = leanify_tree_mapping(mapping, "lean")
    assert lean["layout_tree"] == mapping.model_dump()["layout_tree"]
    assert lean["tokens"] == mapping.tokens
    assert lean["summary"] == mapping.summary
    assert lean["warnings"] == mapping.warnings


def test_lean_reduction_telemetry_present_and_consistent() -> None:
    mapping = _rich_mapping()
    lean = leanify_tree_mapping(mapping, "lean")
    reduction = lean["reduction"]
    assert reduction["input_nodes"] == 42
    assert reduction["agenda_size"] == len(lean["agenda"]) == 1
    assert reduction["dropped_count"] == 20
    # Telemetry "in" must equal the measured full-dump JSON length and
    # exceed the "out" length.
    assert reduction["response_chars_full"] == _json_len(mapping.model_dump())
    assert reduction["response_chars_full"] > reduction["response_chars_lean"]


# --------------------------------------------------------------------------
# lean agenda-row shape.
# --------------------------------------------------------------------------


def test_lean_agenda_row_keeps_descriptive_fields() -> None:
    lean = leanify_tree_mapping(_rich_mapping(), "lean")
    row = lean["agenda"][0]
    for key in (
        "id",
        "name",
        "role",
        "bbox",
        "parent_chain",
        "shape_bucket",
        "children_summary",
        "content_slots",
        "structural_hints",
        "box_style",
        "hex_colors",
        "absolute_pos",
        "mapping",
    ):
        assert key in row, f"lean agenda row missing kept field {key!r}"
    assert row["id"] == "626:987"
    assert row["shape_bucket"] == "tile"
    assert row["box_style"]["background_color"] == "#EDF0F2"
    assert row["content_slots"]["cell_count"] == 5
    assert row["absolute_pos"]["z_order"] == 1


def test_lean_agenda_row_drops_aliased_ids_and_reference_jsx_slice() -> None:
    lean = leanify_tree_mapping(_rich_mapping(), "lean")
    row = lean["agenda"][0]
    assert "aliased_ids" not in row
    assert "reference_jsx_slice" not in row


def test_lean_parent_chain_capped_to_last_three() -> None:
    lean = leanify_tree_mapping(_rich_mapping(), "lean")
    # Source chain is ["Root","Page","Body","Grid","Cell"].
    assert lean["agenda"][0]["parent_chain"] == ["Body", "Grid", "Cell"]


def test_lean_slim_mapping_shape() -> None:
    lean = leanify_tree_mapping(_rich_mapping(), "lean")
    slim = lean["agenda"][0]["mapping"]
    assert set(slim.keys()) == {
        "suggested_component_name",
        "primary_recommendation",
        "primary_recommendation_confidence",
        "description",
        "candidates",
    }
    assert slim["suggested_component_name"] == "Tile"
    assert slim["primary_recommendation"] == "Tile"
    assert slim["primary_recommendation_confidence"] == 1.0
    # description is the *top* candidate's one-line summary.
    assert slim["description"] == "TOP_CANDIDATE_SUMMARY"
    # candidates: top-3, each {name, score} ONLY.
    assert len(slim["candidates"]) == 3
    for cand in slim["candidates"]:
        assert set(cand.keys()) == {"name", "score"}
    assert [c["name"] for c in slim["candidates"]] == [
        "Cand0",
        "Cand1",
        "Cand2",
    ]


def test_lean_omits_raw_jsx_examples_and_a11y_blocks() -> None:
    mapping = _rich_mapping()
    lean_blob = json.dumps(leanify_tree_mapping(mapping, "lean"))
    full_blob = json.dumps(leanify_tree_mapping(mapping, "full"))
    # Present in full, gone in lean.
    for sentinel in (_JSX_SENTINEL, _A11Y_SENTINEL, _WHY_SENTINEL):
        assert sentinel in full_blob, f"{sentinel} should exist in full mode"
        assert sentinel not in lean_blob, f"{sentinel} must be stripped in lean"
    # The candidate-level retrieval metadata is gone too.
    assert '"source"' not in lean_blob
    assert "candidate_decompositions" not in lean_blob
    assert "token_mappings" not in lean_blob
    assert '"related"' not in lean_blob


def test_lean_handles_empty_candidates_gracefully() -> None:
    """A placeholder mapping (no candidates) still leans cleanly."""
    region = _rich_region()
    region.mapping = FigmaNodeMapping(
        node_name="Tile", suggested_component_name=None
    )
    mapping = FigmaTreeMapping(agenda=[region], summary={"input_nodes": 1})
    slim = leanify_tree_mapping(mapping, "lean")["agenda"][0]["mapping"]
    assert slim["candidates"] == []
    assert slim["description"] == ""
    assert slim["suggested_component_name"] is None


# --------------------------------------------------------------------------
# x-ray-4: order-of-magnitude reduction on a page with a large dropped list.
# --------------------------------------------------------------------------


def _walk_xray4_with_heavy_stub() -> FigmaTreeMapping:
    tree = json.loads(
        (FIXTURE_DIR / "x-ray-4-gold-image-list.json").read_text(
            encoding="utf-8"
        )
    )
    return walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=_heavy_stub_mapper(),
    )


def test_x_ray_4_lean_is_order_of_magnitude_smaller() -> None:
    mapping = _walk_xray4_with_heavy_stub()
    full_chars = _json_len(leanify_tree_mapping(mapping, "full"))
    lean_chars = _json_len(leanify_tree_mapping(mapping, "lean"))
    assert full_chars >= 10 * lean_chars, (
        f"expected an order-of-magnitude reduction on x-ray-4: "
        f"full={full_chars} lean={lean_chars} ratio={full_chars / lean_chars:.1f}x"
    )


def test_x_ray_4_lean_drops_dropped_list_but_keeps_counts() -> None:
    mapping = _walk_xray4_with_heavy_stub()
    lean = leanify_tree_mapping(mapping, "lean")
    assert "dropped" not in lean
    # dropped_summary sums to the same total the walker recorded.
    assert sum(lean["dropped_summary"].values()) == len(mapping.dropped)
    assert lean["reduction"]["dropped_count"] == len(mapping.dropped)
    assert mapping.dropped, "x-ray-4 is expected to have a large dropped list"


def test_x_ray_4_lean_has_no_raw_jsx_or_a11y() -> None:
    mapping = _walk_xray4_with_heavy_stub()
    lean_blob = json.dumps(leanify_tree_mapping(mapping, "lean"))
    full_blob = json.dumps(leanify_tree_mapping(mapping, "full"))
    for sentinel in (_JSX_SENTINEL, _A11Y_SENTINEL, _WHY_SENTINEL):
        assert sentinel in full_blob
        assert sentinel not in lean_blob


def test_x_ray_4_full_is_byte_for_byte_model_dump() -> None:
    mapping = _walk_xray4_with_heavy_stub()
    assert leanify_tree_mapping(mapping, "full") == mapping.model_dump()


# --------------------------------------------------------------------------
# Server wiring — the curated-mock short-circuit honours response_detail.
# --------------------------------------------------------------------------


def _write_mock(mocks_dir: Path, file_key: str, node_id_us: str) -> None:
    """Write a mock with a non-empty dropped list for (file_key, node_id)."""
    mapping = FigmaTreeMapping(
        agenda=[_rich_region()],
        dropped=[
            DroppedNode(
                id=f"9:{i}",
                name="x",
                type="RECTANGLE",
                reason="tiny_decorative",
            )
            for i in range(7)
        ],
        summary={"input_nodes": 3, "agenda_size": 1, "dropped_total": 7},
    )
    mocks_dir.mkdir(parents=True, exist_ok=True)
    (mocks_dir / f"{file_key}__{node_id_us}.json").write_text(
        json.dumps(mapping.model_dump(mode="json")), encoding="utf-8"
    )


async def _run_map_figma_tree(
    server: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    tool = server._tool_manager._tools["map_figma_tree"]
    result = await tool.run({"input": payload})
    if isinstance(result, dict):
        return result
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured
    content = getattr(result, "content", None)
    if content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    raise AssertionError(f"could not extract payload from {result!r}")


@pytest.mark.asyncio
async def test_mock_short_circuit_defaults_to_lean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A curated mock returned by the tool is leaned on the way out."""
    monkeypatch.setenv("PRISM_MCP_FIGMA_TREE_MOCKS_DIR", str(tmp_path))
    _write_mock(tmp_path, "demoKey", "1_1")
    server = build_server(enable_refresh_loop=False)

    payload = await _run_map_figma_tree(
        server,
        {"node_url": "https://www.figma.com/design/demoKey/x?node-id=1-1"},
    )
    assert "dropped" not in payload
    assert payload["dropped_summary"] == {"tiny_decorative": 7}
    assert "reduction" in payload
    # Heavy per-row payload stripped even for mocks.
    assert _JSX_SENTINEL not in json.dumps(payload)


@pytest.mark.asyncio
async def test_mock_short_circuit_full_returns_complete_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRISM_MCP_FIGMA_TREE_MOCKS_DIR", str(tmp_path))
    _write_mock(tmp_path, "demoKey", "1_1")
    server = build_server(enable_refresh_loop=False)

    payload = await _run_map_figma_tree(
        server,
        {
            "node_url": "https://www.figma.com/design/demoKey/x?node-id=1-1",
            "response_detail": "full",
        },
    )
    assert isinstance(payload["dropped"], list)
    assert len(payload["dropped"]) == 7
    assert "dropped_summary" not in payload
    assert "reduction" not in payload
    # Full mode keeps the heavy per-row payload.
    assert _JSX_SENTINEL in json.dumps(payload)
