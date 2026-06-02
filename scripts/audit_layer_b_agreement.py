"""Throwaway agreement audit for the proposed Layer B
``primary_recommendation`` field.

Per ``docs/handoff-spatial-and-ranker.md`` §4 and the
``spatial_layout_and_ranker_aa5be646.plan.md`` Layer-B gate, we
only land the soft recommendation if the deterministic
``PATTERN_TO_PRIMARY`` map agrees with the BM25 top-1 on ≥ 80% of
the pattern-role regions across the three big real-world
fixtures.

This script walks ``figma-d02-share-summary``,
``figma-e01-share-summary``, and ``figma-active-cluster-page`` with
the same curated in-memory :class:`Index` used by
``tests/test_figma_walk_e2e.py`` (no fastembed / no network) and
prints:

* per-fixture and overall agreement %,
* the disagreement count + the specific regions where they
  diverged.

Usage::

    python scripts/audit_layer_b_agreement.py

Exit code is 0 on a passing audit (≥ 80 %), 1 otherwise — handy
for CI smoke if we ever wire it.
"""

from __future__ import annotations

import functools
import json
import sys
from collections import Counter
from pathlib import Path

from prism_mcp.a11y import A11yRules
from prism_mcp.embeddings import ExampleHit
from prism_mcp.entities import Entity, Member
from prism_mcp.figma import walk_tree
from prism_mcp.graph import build_composition_graph
from prism_mcp.indexer import Index
from prism_mcp.tokens_index import build_color_token_index
from prism_mcp.workflow.figma_mapping import map_figma_node


# --------------------------------------------------------------------------
# Proposed PATTERN_TO_PRIMARY map (the candidate for Layer B).
# --------------------------------------------------------------------------


PATTERN_TO_PRIMARY: dict[str, str] = {
    "kpi-tile": "Tile",
    "stat-list": "StatList",
    "table-column": "TableColumn",
    "tab-strip": "TabBar",
    "button-group": "ButtonGroup",
    "icon": "Icon",
}


AGREEMENT_THRESHOLD = 0.80


# --------------------------------------------------------------------------
# Index — same curated entities as the e2e test plus the synonyms
# the deterministic recommendation needs to be checkable. Without
# every primary component existing in the index, BM25 obviously
# can't pick them, which would inflate the disagreement rate
# artificially.
# --------------------------------------------------------------------------


def _component(name: str, summary: str = "") -> Entity:
    return Entity(
        name=name,
        type="component",
        version="t",
        summary=summary or f"{name} component",
        import_path=f"@nutanix-ui/prism-reactjs/{name}",
        signature=[Member(name="children", kind="prop", type="ReactNode")],
    )


def _build_index() -> Index:
    return Index(
        entities=[
            _component("Tile", "Square card container with a title and stat."),
            _component("StatList", "Vertical list of stat label/value pairs."),
            _component("Paragraph", "Text paragraph used for body copy."),
            _component("Icon", "Single SVG icon."),
            _component("Table", "Data table with rows and columns."),
            _component("TableColumn", "Single column inside a Table."),
            _component("TabBar", "Horizontal tab strip."),
            _component("ButtonGroup", "Cluster of action buttons."),
            _component("Button", "Clickable button."),
            _component("FlexLayout", "Flexbox container."),
            _component("Modal", "Dialog overlay."),
            _component("Card", "Generic card."),
        ],
        version="t",
    )


class _NoHitsSearcher:
    def search(self, **kwargs: object) -> list[ExampleHit]:
        return []


def _build_map_fn() -> functools.partial:
    return functools.partial(
        map_figma_node,
        index=_build_index(),
        hybrid_searcher=_NoHitsSearcher(),
        composition_graph=build_composition_graph(chunks=[], version="t"),
        color_token_index=build_color_token_index(entities=[], version="t"),
        a11y_rules=A11yRules(
            version="t",
            title=None,
            global_rules=[],
            per_component=[],
        ),
    )


# --------------------------------------------------------------------------
# Audit.
# --------------------------------------------------------------------------


FIXTURE_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "figma"
FIXTURES = [
    "figma-d02-share-summary.json",
    "figma-e01-share-summary.json",
    "figma-active-cluster-page.json",
]


def _audit_fixture(fixture: str, map_fn) -> dict[str, object]:
    tree = json.loads((FIXTURE_DIR / fixture).read_text(encoding="utf-8"))
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=map_fn,
    )

    eligible = 0
    agree = 0
    disagreements: list[dict[str, str]] = []
    role_counts: Counter[str] = Counter()

    for region in mapping.agenda:
        primary = PATTERN_TO_PRIMARY.get(region.role)
        if primary is None:
            continue
        role_counts[region.role] += 1
        eligible += 1
        bm25_top = (
            region.mapping.candidates[0].name
            if region.mapping.candidates
            else None
        )
        if bm25_top == primary:
            agree += 1
        else:
            disagreements.append(
                {
                    "region_id": region.id,
                    "region_role": region.role,
                    "primary_recommendation": primary,
                    "bm25_top": bm25_top or "(none)",
                    "name": region.name,
                }
            )
    return {
        "fixture": fixture,
        "eligible_regions": eligible,
        "agreed": agree,
        "agreement_pct": (
            (agree / eligible * 100.0) if eligible else 0.0
        ),
        "role_breakdown": dict(role_counts),
        "disagreements": disagreements,
    }


def main() -> int:
    map_fn = _build_map_fn()
    results = [_audit_fixture(fx, map_fn) for fx in FIXTURES]

    print("=" * 72)
    print("Layer B agreement audit — primary_recommendation vs BM25 top-1")
    print("=" * 72)

    overall_eligible = 0
    overall_agree = 0
    for r in results:
        elig = int(r["eligible_regions"])
        agr = int(r["agreed"])
        pct = float(r["agreement_pct"])
        overall_eligible += elig
        overall_agree += agr
        print()
        print(
            f"[{r['fixture']}] eligible={elig} agreed={agr} "
            f"agreement={pct:.1f}%"
        )
        print(f"  role_breakdown = {r['role_breakdown']}")
        diss = r["disagreements"]
        if isinstance(diss, list) and diss:
            print(f"  disagreements ({len(diss)}):")
            for d in diss[:8]:
                print(
                    f"    - {d['region_role']:14s} {d['region_id']:30s} "
                    f"name={d['name']!r:30s} "
                    f"primary={d['primary_recommendation']:10s} "
                    f"bm25={d['bm25_top']}"
                )
            if len(diss) > 8:
                print(f"    ... and {len(diss) - 8} more")

    overall_pct = (
        (overall_agree / overall_eligible * 100.0)
        if overall_eligible
        else 0.0
    )
    print()
    print("-" * 72)
    print(
        f"OVERALL: eligible={overall_eligible} agreed={overall_agree} "
        f"agreement={overall_pct:.1f}%"
    )
    print(f"Threshold = {AGREEMENT_THRESHOLD * 100:.0f}%")
    print(
        "DECISION: "
        + (
            "LAND Layer B as soft override"
            if overall_pct >= AGREEMENT_THRESHOLD * 100
            else "NARROW or SKIP Layer B (audit failed)"
        )
    )
    return 0 if overall_pct >= AGREEMENT_THRESHOLD * 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())
