"""Walker integration tests against curated golden fixtures.

Each fixture ``tests/fixtures/figma/<name>.json`` is paired with
a ``<name>.expected.json`` that captures the stable shape of the
walker's output (summary, agenda IDs+roles+names, layout topology,
and the drop-by-reason histogram). The walker is exercised with
``map_figma_node_fn=None`` so the test is independent of the live
Prism library index — the e2e candidate-quality test lives in
``test_figma_walk_e2e.py``.

Regenerating goldens
---------------------

If the walker changes intentionally, regenerate the goldens with::

    pytest tests/test_figma_walker.py --update-figma-golden

and review the resulting diff before committing.

Design doc anchors: §4.6 (agenda + layout-tree split), §8.1-8.3
(worked examples), §11.3 (golden fixtures).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from prism_mcp.figma import FigmaTreeMapping, walk_tree

# Anchored relative to this test file so the suite stays portable.
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "figma"


# ----------------------------------------------------------------------
# Fixture discovery.
# ----------------------------------------------------------------------


def _discover_fixtures() -> list[Path]:
    """Return all ``*.json`` Figma fixtures excluding the goldens."""
    return sorted(
        p
        for p in FIXTURE_DIR.glob("*.json")
        if not p.name.endswith(".expected.json")
    )


# ----------------------------------------------------------------------
# Golden representation.
# ----------------------------------------------------------------------


def _to_golden(mapping: FigmaTreeMapping) -> dict[str, Any]:
    """Project a :class:`FigmaTreeMapping` down to the golden shape."""
    return {
        "summary": mapping.summary,
        "agenda": [
            {
                "id": region.id,
                "role": region.role,
                "name": region.name,
            }
            for region in mapping.agenda
        ],
        "layout_tree": [
            {
                "id": node.id,
                "role": node.role,
                "name": node.name,
                "children_ids": node.children_ids,
            }
            for node in mapping.layout_tree
        ],
        "dropped_by_reason": dict(
            Counter(item.reason for item in mapping.dropped)
        ),
        "warnings": list(mapping.warnings),
    }


def _read_golden(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_golden(path: Path, golden: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(golden, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
# Generic golden-comparison test (one parametrised case per fixture).
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_path",
    _discover_fixtures(),
    ids=lambda p: p.stem,
)
def test_walk_tree_matches_golden(
    fixture_path: Path,
    update_figma_golden: bool,
) -> None:
    """The walker output for each fixture matches the committed golden.

    The walker is deterministic given a fixed input — same JSON in,
    same agenda/layout/dropped histogram out. This test pins that
    invariant for every fixture in ``tests/fixtures/figma/``. When
    intentionally changing walker behaviour, regenerate the goldens
    with ``pytest --update-figma-golden``.
    """
    tree = json.loads(fixture_path.read_text(encoding="utf-8"))
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )
    actual = _to_golden(mapping)

    expected_path = fixture_path.with_suffix(".expected.json")

    if update_figma_golden:
        _write_golden(expected_path, actual)
        pytest.skip(
            f"--update-figma-golden: regenerated {expected_path.name}; "
            "review the diff before committing"
        )

    assert expected_path.exists(), (
        f"missing golden {expected_path}; regenerate with "
        "pytest --update-figma-golden"
    )

    expected = _read_golden(expected_path)
    assert actual == expected, (
        f"walker output diverged from golden for {fixture_path.name}; "
        "regenerate intentionally with pytest --update-figma-golden"
    )


# ----------------------------------------------------------------------
# Spot-check assertions per worked example (design doc §8.1-§8.3).
# Each test pins a *specific* claim made in the design doc so a
# golden regeneration accident can't silently break the documented
# behaviour.
# ----------------------------------------------------------------------


def test_small_tile_fixture_emits_tile_plus_two_stat_lists() -> None:
    """§8.1: 626:986 must produce one tile + two stat-list regions.

    This pins the qualitative claim of the worked example separately
    from the byte-for-byte golden comparison above. If the walker
    grows new patterns in the future, the byte-level golden will
    diff but this assertion holds.
    """
    tree = json.loads(
        (FIXTURE_DIR / "figma-node-626-986.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )

    roles = [r.role for r in mapping.agenda]
    assert "instance" in roles, "tile (626:987) should be mapped as instance"
    assert roles.count("stat-list") == 2, (
        "expected 2 stat-list rows for Cluster Details + Cluster Details Copy"
    )
    agenda_ids = {r.id for r in mapping.agenda}
    assert {"626:987", "626:988", "626:999"} <= agenda_ids


def test_hamburger_icon_fixture_collapses_to_single_icon_region() -> None:
    """§8.3: 3-stripe hamburger BOOLEAN_OPERATION collapses to 1 row.

    The pass-5 icon coalescer must absorb all three RECTANGLE
    children with reason ``icon_internal`` and produce a single
    MappedRegion with ``role='icon'``. This is a high-frequency
    case (every page has 50-100 icons); regressing it would blow
    up the agenda size.
    """
    tree = json.loads(
        (FIXTURE_DIR / "hamburger-icon.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )

    assert len(mapping.agenda) == 1
    assert mapping.agenda[0].role == "icon"
    assert mapping.agenda[0].name == "Menu"

    icon_drops = [d for d in mapping.dropped if d.reason == "icon_internal"]
    assert len(icon_drops) == 3, (
        "all 3 rectangle strokes should be folded into the icon"
    )


def test_table_column_fixture_collapses_to_single_table_column() -> None:
    """§4.5.2: Table/Column FRAME with N Cells collapses to 1 row.

    The column-of-cells pattern must produce exactly one
    MappedRegion with ``role='table-column'`` and absorb the
    header + every cell. Regressing this would force the LLM to
    instantiate N <TableColumn> rows separately — exactly the
    duplication the pattern was designed to prevent.
    """
    tree = json.loads(
        (FIXTURE_DIR / "table-column.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )

    assert len(mapping.agenda) == 1
    assert mapping.agenda[0].role == "table-column"
    assert mapping.agenda[0].id == "8000:1"


def test_opportunities_page_collapses_278_nodes_to_under_50_regions() -> None:
    """§8.4: full page (278 nodes) compresses to a manageable agenda.

    The walker's whole reason for existing is to shrink raw Figma
    JSON down to an LLM-digestible plan. We assert the documented
    compression ratio holds: a 200-700 node real page produces
    at most ~50 agenda rows (the ``max_agenda`` soft cap).
    """
    tree = json.loads(
        (FIXTURE_DIR / "opportunities-page.json").read_text(encoding="utf-8")
    )
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )

    assert mapping.summary["input_nodes"] >= 250, (
        "opportunities-page.json should be a realistic-scale fixture"
    )
    assert mapping.summary["agenda_size"] <= 50, (
        "agenda must stay under the max_agenda soft cap"
    )
    # The dropped histogram should be dominated by structural noise,
    # not by safety-rail bails (which would manifest as warnings).
    assert not mapping.warnings, (
        f"unexpected walker warnings: {mapping.warnings!r}"
    )
