"""Measure prop-resolution accuracy on real pages (roadmap P3 Part B).

``measure_tier1_routing.py`` answers *"which component is each region?"*
(routing). This driver answers the next question: *"once a region is
routed, how many of its Figma ``componentProperties`` do we turn into a
typed Prism prop?"* — the **prop-level coverage** the codegen layer
relies on to emit ``type={ButtonTypes.PRIMARY}`` instead of a bare tag.

It replays saved page dumps through the real
:func:`prism_mcp.figma.walker.walk_tree` (hermetic — ``map_figma_node_fn
=None``, injected :class:`~prism_mcp.figma.catalog.FigmaCatalog` +
:class:`~prism_mcp.figma.prop_schema.PropSchemaIndex`), then for every
*routed* region with Figma ``componentProperties`` re-runs the
deterministic :func:`~prism_mcp.figma.props.resolve_props` cascade to
tally resolved vs unresolved properties.

Denominator convention — a Figma property counts toward coverage only
when it *could* be a prop. ``INSTANCE_SWAP`` axes (the swapped child is
its own region) and curated ``ignore_axes`` (design-only scaffolding)
are reported separately and excluded, exactly as the resolver treats
them.

Run from the repo root::

    uv run python scripts/measure_prop_resolution.py
    uv run python scripts/measure_prop_resolution.py xray_cloudconnect.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from prism_mcp.figma.catalog import FigmaCatalog
from prism_mcp.figma.prop_schema import PropSchemaIndex
from prism_mcp.figma.props import resolve_props
from prism_mcp.figma.walker import walk_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "docs" / "_audit_data"

DEFAULT_PAGES = [
    "xray_login.json",
    "xray_9188_127717.json",
    "xray_cloudconnect.json",
]

_NO_LIMITS = {"max_depth": 100, "max_nodes": 500_000, "max_agenda": 100_000}

# Families that Prism builds *declaratively* — from `dataSource` /
# `columns` / `options` / children, not from scalar props. Their Figma
# variant axes ("Table Cell Type = Normal", "Select Type = Action") are
# design-system visual descriptors with no prop equivalent, verified
# against the v2 prop schemas (TableCell/TableRow/Select expose only
# object props). Reported separately so the configurable-component
# coverage number is not drowned out by axes that *cannot* be props.
_DECLARATIVE_FAMILIES = frozenset(
    {
        "Tables",
        "Select",
        "Menu",
        "Dashboard",
        "Navigation",
        "Notification",
        "Icons",
        "Calendar",
        "Breadcrumb",
        "Tabs",
    }
)


def _collect_component_properties(
    node: dict[str, Any], out: dict[str, dict[str, Any]]
) -> None:
    """Recursively map ``node id -> componentProperties`` for instances."""
    props = node.get("componentProperties")
    if isinstance(props, dict) and props:
        out[str(node.get("id", ""))] = props
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            _collect_component_properties(child, out)


def _measure_page(
    path: Path, catalog: FigmaCatalog, schema: PropSchemaIndex
) -> dict[str, Any]:
    """Walk one page and tally prop resolution over its routed regions."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    node = next(iter(raw["nodes"].values()))
    document = node["document"]

    node_props: dict[str, dict[str, Any]] = {}
    _collect_component_properties(document, node_props)

    mapping = walk_tree(
        tree_json=document,
        components=node.get("components", {}),
        component_sets=node.get("componentSets", {}),
        styles=node.get("styles", {}),
        map_figma_node_fn=None,
        catalog=catalog,
        prop_schema=schema,
        **_NO_LIMITS,
    )

    routed = [r for r in mapping.agenda if r.prism_resolution is not None]
    routed_with_props = [r for r in routed if node_props.get(r.id)]

    resolved = 0
    unresolved = 0
    instance_swap = 0
    regions_full = 0
    by_method: Counter = Counter()
    miss_samples: Counter = Counter()
    fam_resolved: Counter = Counter()
    fam_missable: Counter = Counter()

    for region in routed_with_props:
        res = region.prism_resolution
        figma_name = (
            region.figma_component.component_name
            if region.figma_component
            else None
        )
        comp_schema = (
            schema.for_region(res.prism_component, figma_name) if res else None
        )
        if comp_schema is None:
            continue
        outcome = resolve_props(node_props[region.id], comp_schema)
        resolved += len(outcome.props)
        fam_resolved[res.prism_component] += len(outcome.props)
        for p in outcome.props:
            by_method[p.method] += 1
        region_unresolved = 0
        for u in outcome.unresolved:
            if u.figma_kind == "INSTANCE_SWAP":
                instance_swap += 1
            else:
                unresolved += 1
                region_unresolved += 1
                fam_missable[res.prism_component] += 1
                miss_samples[
                    f"{res.prism_component}:{u.axis}={u.figma_value}"
                ] += 1
        if region_unresolved == 0:
            regions_full += 1

    denom = resolved + unresolved
    # Per-family coverage: resolved / (resolved + missable). Separates
    # configurable leaf components (Button/Badge/Input) from declarative
    # ones (Tables), whose Figma variant axes are design-system internal
    # descriptors with no Prism prop by construction.
    fam_cov: dict[str, str] = {}
    cfg_r = cfg_d = dec_r = dec_d = 0
    for fam in sorted(set(fam_resolved) | set(fam_missable)):
        r = fam_resolved[fam]
        d = r + fam_missable[fam]
        fam_cov[fam] = f"{r}/{d} = {round(100 * r / d) if d else 0}%"
        if fam in _DECLARATIVE_FAMILIES:
            dec_r += r
            dec_d += d
        else:
            cfg_r += r
            cfg_d += d

    return {
        "page": path.name,
        "instances_with_props": len(node_props),
        "routed_with_props": len(routed_with_props),
        "regions_fully_resolved": regions_full,
        "props_resolved": resolved,
        "props_unresolved": unresolved,
        "instance_swap": instance_swap,
        "prop_coverage_pct": (
            round(100 * resolved / denom, 1) if denom else 0.0
        ),
        "configurable_resolved": cfg_r,
        "configurable_missable": cfg_d,
        "declarative_resolved": dec_r,
        "declarative_missable": dec_d,
        "by_method": dict(by_method.most_common()),
        "by_family_coverage": fam_cov,
        "top_misses": miss_samples.most_common(12),
    }


def _print_page(r: dict[str, Any]) -> None:
    print(f"=== {r['page']} ===")
    print(f"  instances w/ props      : {r['instances_with_props']}")
    print(f"  routed regions w/ props : {r['routed_with_props']}")
    print(f"  fully-resolved regions  : {r['regions_fully_resolved']}")
    print(f"  props resolved          : {r['props_resolved']}")
    print(f"  props unresolved        : {r['props_unresolved']}")
    print(f"  instance-swap (excl.)   : {r['instance_swap']}")
    cfg_d = r["configurable_missable"]
    cfg_pct = round(100 * r["configurable_resolved"] / cfg_d) if cfg_d else 0
    print(f"  raw axis coverage       : {r['prop_coverage_pct']}%")
    print(
        f"  CONFIGURABLE coverage   : {cfg_pct}% "
        f"({r['configurable_resolved']}/{cfg_d})"
    )
    print(f"  by method               : {r['by_method']}")
    print(f"  by family coverage      : {r['by_family_coverage']}")
    print(f"  top misses              : {r['top_misses']}\n")


def main() -> int:
    catalog = FigmaCatalog.load()
    schema = PropSchemaIndex.load()
    pages = sys.argv[1:] or DEFAULT_PAGES
    print(f"catalog: {len(catalog)} entries | prop schema: {len(schema)} "
          f"components\n")

    agg = Counter()
    agg_method: Counter = Counter()
    for name in pages:
        path = AUDIT_DIR / name
        if not path.is_file():
            print(f"  (skip {name}: not found)")
            continue
        r = _measure_page(path, catalog, schema)
        for key in (
            "routed_with_props",
            "regions_fully_resolved",
            "props_resolved",
            "props_unresolved",
            "instance_swap",
            "configurable_resolved",
            "configurable_missable",
            "declarative_resolved",
            "declarative_missable",
        ):
            agg[key] += r[key]
        agg_method.update(r["by_method"])
        _print_page(r)

    denom = agg["props_resolved"] + agg["props_unresolved"]
    cfg_d = agg["configurable_missable"]
    dec_d = agg["declarative_missable"]
    print("=== AGGREGATE ===")
    print(f"  routed regions w/ props : {agg['routed_with_props']}")
    print(f"  fully-resolved regions  : {agg['regions_fully_resolved']}")
    print(f"  props resolved          : {agg['props_resolved']}")
    print(f"  instance-swap (excl.)   : {agg['instance_swap']}")
    print(
        f"  raw axis coverage       : "
        f"{round(100 * agg['props_resolved'] / denom, 1) if denom else 0}%"
        f"  ({agg['props_resolved']}/{denom})"
    )
    print(
        f"  CONFIGURABLE coverage   : "
        f"{round(100 * agg['configurable_resolved'] / cfg_d) if cfg_d else 0}%"
        f"  ({agg['configurable_resolved']}/{cfg_d}) "
        f"<- Button/Badge/Input/Checkbox/Alert/..."
    )
    print(
        f"  declarative axes        : "
        f"{round(100 * agg['declarative_resolved'] / dec_d) if dec_d else 0}%"
        f"  ({agg['declarative_resolved']}/{dec_d}) "
        f"<- Tables/Select/Menu: variant axes are not props"
    )
    print(f"  by method               : {dict(agg_method.most_common())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
