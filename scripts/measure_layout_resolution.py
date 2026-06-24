"""Measure layout resolution on real pages (roadmap P4).

``measure_tier1_routing.py`` answers *"which component is each region?"*
and ``measure_prop_resolution.py`` answers *"what props does it get?"*.
This driver answers the layout question: *"of the structural container
FRAMEs the walker keeps — the ones that would otherwise become
hand-written ``<div style={{display:'flex',…}}>`` — how many now carry a
Prism Layout primitive (FlexLayout / StackingLayout) with resolved
props?"* — the roadmap's "no divs / no CSS" metric.

It replays saved page dumps through the real
:func:`prism_mcp.figma.walker.walk_tree` (hermetic — no live mapper, no
catalog needed: layout is identity-independent) and tallies, over the
**structural containers** in ``layout_tree`` (roles ``layout-container`` /
``composed-region``), how many got a ``prism_layout``, split by primitive,
provenance (Figma auto-layout vs. geometry), and the props emitted.

Denominator convention — a container counts as "needs a layout wrapper"
only when it has a real flow direction. Single-child and overlap-``stack``
containers correctly resolve to ``None`` (no flex wrapper warranted) and
are reported separately, not as misses.

Run from the repo root::

    uv run python scripts/measure_layout_resolution.py
    uv run python scripts/measure_layout_resolution.py path/to/page.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from prism_mcp.figma.walker import walk_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "docs" / "_audit_data"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "figma"

# Live X-Ray page dumps (preferred — the real product screens) plus the
# committed walker fixtures (always present). Missing paths are skipped.
DEFAULT_PAGES: list[Path] = [
    AUDIT_DIR / "xray_login.json",
    AUDIT_DIR / "xray_9188_127717.json",
    AUDIT_DIR / "xray_cloudconnect.json",
    FIXTURE_DIR / "figma-active-cluster-page.json",
    FIXTURE_DIR / "opportunities-page.json",
    FIXTURE_DIR / "figma-d02-share-summary.json",
    FIXTURE_DIR / "x-ray-3-results-progress-empty.json",
    FIXTURE_DIR / "x-ray-4-gold-image-list.json",
]

_CONTAINER_ROLES = frozenset({"layout-container", "composed-region"})
_NO_LIMITS = {"max_depth": 100, "max_nodes": 500_000, "max_agenda": 100_000}


def _load_document(path: Path) -> dict[str, Any]:
    """Return the document dict from a fixture or a ``/nodes`` dump.

    Handles three on-disk shapes: a raw ``/nodes`` response
    (``{"nodes": {id: {"document": …, "components": …}}}``), a single-node
    ``{"document": …}`` wrapper, and a bare document (the committed
    fixtures). Returns the document plus any sibling maps in a tuple-free
    dict so the caller can thread ``components`` / ``componentSets`` /
    ``styles`` when present.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "nodes" in raw:
        node = next(iter(raw["nodes"].values()))
        return node
    if isinstance(raw, dict) and "document" in raw:
        return raw
    return {"document": raw}


def _measure_page(path: Path) -> dict[str, Any]:
    """Walk one page and tally layout resolution over its containers."""
    node = _load_document(path)
    document = node["document"]

    mapping = walk_tree(
        tree_json=document,
        components=node.get("components", {}),
        component_sets=node.get("componentSets", {}),
        styles=node.get("styles", {}),
        map_figma_node_fn=None,
        **_NO_LIMITS,
    )

    containers = [
        n
        for n in mapping.layout_tree
        if n.role in _CONTAINER_ROLES and n.children_ids
    ]
    # A container is "not a div" when it carries a flow primitive OR a page
    # shell (the shell fully describes the route-anchoring frame's layout).
    resolved = [
        n
        for n in containers
        if n.prism_layout is not None or n.prism_shell is not None
    ]

    by_component: Counter = Counter()
    by_source: Counter = Counter()
    by_prop: Counter = Counter()
    gap_tokens: Counter = Counter()
    notes: Counter = Counter()
    fill_items = 0  # P4 #2: total FlexItem flexGrow children emitted.
    for n in resolved:
        pl = n.prism_layout
        if pl is None:  # pragma: no cover - the `resolved` filter guarantees set
            continue
        by_component[pl.component] += 1
        by_source[pl.source] += 1
        fill_items += len(pl.fill_child_ids)
        for prop_name, prop_value in pl.props.items():
            by_prop[prop_name] += 1
            if prop_name == "itemGap":
                gap_tokens[prop_value] += 1
        for note in pl.notes:
            notes[note] += 1

    # P4 #1: page shells live on any layout node (the route-anchoring frame),
    # not just the ``containers`` subset, so tally them over the whole tree.
    shells: Counter = Counter()
    for n in mapping.layout_tree:
        if n.prism_shell is not None:
            shells[n.prism_shell.component] += 1

    n_containers = len(containers)
    n_resolved = len(resolved)
    return {
        "page": path.name,
        "containers": n_containers,
        "resolved": n_resolved,
        "no_flow": n_containers - n_resolved,
        "coverage_pct": (
            round(100 * n_resolved / n_containers, 1) if n_containers else 0.0
        ),
        "by_component": dict(by_component.most_common()),
        "by_source": dict(by_source.most_common()),
        "by_prop": dict(by_prop.most_common()),
        "gap_tokens": dict(gap_tokens.most_common()),
        "notes": dict(notes.most_common(6)),
        "shells": dict(shells.most_common()),
        "fill_items": fill_items,
    }


def _print_page(r: dict[str, Any]) -> None:
    print(f"=== {r['page']} ===")
    print(f"  structural containers   : {r['containers']}")
    print(f"  resolved (prism_layout) : {r['resolved']}  ({r['coverage_pct']}%)")
    print(f"  no-flow (single/stack)  : {r['no_flow']}")
    print(f"  by primitive            : {r['by_component']}")
    print(f"  by source               : {r['by_source']}")
    print(f"  props emitted           : {r['by_prop']}")
    print(f"  itemGap tokens          : {r['gap_tokens']}")
    if r["shells"]:
        print(f"  page shells             : {r['shells']}")
    if r["fill_items"]:
        print(f"  FlexItem flexGrow kids  : {r['fill_items']}")
    if r["notes"]:
        print(f"  notes                   : {r['notes']}")
    print()


def main(argv: list[str]) -> int:
    pages = [Path(a) for a in argv] if argv else DEFAULT_PAGES
    pages = [p for p in pages if p.exists()]
    if not pages:
        print("no pages found; pass page-dump paths as arguments")
        return 1

    results = [_measure_page(p) for p in pages]
    for r in results:
        _print_page(r)

    tot_c = sum(r["containers"] for r in results)
    tot_r = sum(r["resolved"] for r in results)
    agg_component: Counter = Counter()
    agg_source: Counter = Counter()
    agg_prop: Counter = Counter()
    agg_shells: Counter = Counter()
    tot_fill = 0
    for r in results:
        agg_component.update(r["by_component"])
        agg_source.update(r["by_source"])
        agg_prop.update(r["by_prop"])
        agg_shells.update(r["shells"])
        tot_fill += r["fill_items"]

    print("=" * 60)
    print("AGGREGATE")
    print(f"  pages                   : {len(results)}")
    print(f"  structural containers   : {tot_c}")
    cov = round(100 * tot_r / tot_c, 1) if tot_c else 0.0
    print(f"  resolved (prism_layout) : {tot_r}  ({cov}%)")
    print(f"  no-flow (single/stack)  : {tot_c - tot_r}")
    print(f"  by primitive            : {dict(agg_component.most_common())}")
    print(f"  by source               : {dict(agg_source.most_common())}")
    print(f"  props emitted           : {dict(agg_prop.most_common())}")
    print(f"  page shells             : {dict(agg_shells.most_common())}")
    print(f"  FlexItem flexGrow kids  : {tot_fill}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
