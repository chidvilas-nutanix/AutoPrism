"""Measure end-to-end Tier-1 routing yield on real pages (roadmap P3).

``scripts/validate_catalog_coverage.py`` answers *"of every visible
INSTANCE on a page, what fraction does the catalog resolve?"* — an
**instance-level** view taken *before* the walker runs.

This driver answers the complementary, codegen-facing question:
*"of the AGENDA rows the LLM actually receives — after the walker's
noise filter, pattern collapse, same-bbox dedup, and agenda trim — how
many now carry a deterministic Tier-1 ``prism_resolution`` instead of
only a fuzzy BM25/dense candidate list?"* Every agenda row is a
"build this component" instruction, so this is the **decision
coverage** the routing layer delivers.

It replays saved ``/v1/files/:key/nodes`` page dumps through the real
:func:`prism_mcp.figma.walker.walk_tree` with ``map_figma_node_fn=None``
(hermetic — no rplib index / ONNX needed) and the real
:class:`prism_mcp.figma.catalog.FigmaCatalog`, so it isolates the
Tier-1 identity signal. Generous caps disable truncation so the full
page is measured.

Run from the repo root::

    uv run python scripts/measure_tier1_routing.py
    uv run python scripts/measure_tier1_routing.py xray_login.json

Reads page dumps from ``docs/_audit_data/`` (no network).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from prism_mcp.figma.catalog import FigmaCatalog
from prism_mcp.figma.walker import walk_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "docs" / "_audit_data"

DEFAULT_PAGES = [
    "xray_login.json",
    "xray_9188_127717.json",
    "xray_cloudconnect.json",
]

# Annotation / spec-kit / page-frame names that carry a Figma
# componentKey but are deliberately *not* renderable Prism components.
# Mirrors ``validate_catalog_coverage._NOISE_TOKENS`` so the two
# coverage numbers use the same denominator convention.
_NOISE_TOKENS = (
    "a11y",
    "focus order",
    "annotation",
    "skip link",
    "html sections",
    "@spec",
    "@metadata",
)


def _is_noise(*names: str) -> bool:
    """``True`` when any name is scaffolding / annotation / spec frame."""
    for name in names:
        low = (name or "").strip().lower()
        if low.startswith("_") or low.startswith("@"):
            return True
        if any(tok in low for tok in _NOISE_TOKENS):
            return True
    return False

# Disable every safety cap so the measurement sees the whole page
# (truncation would bias the yield by dropping the lowest-importance
# rows, which skews against / for routing unpredictably).
_NO_LIMITS = {"max_depth": 100, "max_nodes": 500_000, "max_agenda": 100_000}


def _measure_page(path: Path, catalog: FigmaCatalog) -> dict:
    """Walk one page dump and bucket its agenda rows by routing outcome."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    node = next(iter(raw["nodes"].values()))

    mapping = walk_tree(
        tree_json=node["document"],
        components=node.get("components", {}),
        component_sets=node.get("componentSets", {}),
        styles=node.get("styles", {}),
        map_figma_node_fn=None,
        catalog=catalog,
        **_NO_LIMITS,
    )

    agenda = mapping.agenda
    with_identity = [r for r in agenda if r.figma_component is not None]
    resolved = [r for r in agenda if r.prism_resolution is not None]

    by_source: Counter = Counter()
    by_method: Counter = Counter()
    by_prism: Counter = Counter()
    for r in resolved:
        res = r.prism_resolution
        if res is None:  # defensive; the comprehension already filtered
            continue
        by_source[res.source] += 1
        by_method[res.method] += 1
        by_prism[res.prism_component] += 1

    # Split the unresolved identity rows into annotation/spec "noise"
    # (correctly has no Prism equivalent) vs a genuine miss so the
    # design-system denominator excludes the former.
    noise_unresolved = 0
    genuine_miss: Counter = Counter()
    for r in with_identity:
        if r.prism_resolution is not None:
            continue
        label = (
            r.figma_component.component_name if r.figma_component else ""
        ) or r.name
        if _is_noise(label, r.name):
            noise_unresolved += 1
        else:
            genuine_miss[label or "(blank)"] += 1

    n_agenda = len(agenda)
    n_identity = len(with_identity)
    n_resolved = len(resolved)
    ds_denom = n_identity - noise_unresolved
    return {
        "page": path.name,
        "agenda": n_agenda,
        "with_identity": n_identity,
        "resolved": n_resolved,
        "noise_unresolved": noise_unresolved,
        # Of every agenda decision, how many ship a deterministic family.
        "agenda_coverage_pct": (
            round(100 * n_resolved / n_agenda, 1) if n_agenda else 0.0
        ),
        # Of the agenda rows that *carry* a DS identity, how many resolve
        # (the rest are local/detached or un-ingested keys).
        "identity_coverage_pct": (
            round(100 * n_resolved / n_identity, 1) if n_identity else 0.0
        ),
        # Excluding annotation/spec frames from the denominator — the
        # fraction of *real* design-system components that route.
        "ds_coverage_pct": (
            round(100 * n_resolved / ds_denom, 1) if ds_denom else 0.0
        ),
        "by_source": dict(by_source.most_common()),
        "by_method": dict(by_method.most_common()),
        "by_prism": dict(by_prism.most_common(12)),
        "top_genuine_miss": genuine_miss.most_common(10),
    }


def _print_page(r: dict) -> None:
    print(f"=== {r['page']} ===")
    print(f"  agenda rows          : {r['agenda']}")
    print(f"  rows w/ DS identity  : {r['with_identity']}")
    print(f"  rows resolved (T1)   : {r['resolved']}")
    print(f"  annotation (no-prism): {r['noise_unresolved']}")
    print(f"  agenda coverage      : {r['agenda_coverage_pct']}%")
    print(f"  identity coverage    : {r['identity_coverage_pct']}%")
    print(f"  design-system cov.   : {r['ds_coverage_pct']}%")
    print(f"  by source            : {r['by_source']}")
    print(f"  by method            : {r['by_method']}")
    print(f"  top Prism            : {r['by_prism']}")
    print(f"  top genuine miss     : {r['top_genuine_miss']}\n")


def main() -> int:
    catalog = FigmaCatalog.load()
    pages = sys.argv[1:] or DEFAULT_PAGES

    agg = Counter()
    agg_source: Counter = Counter()
    agg_method: Counter = Counter()
    print(f"catalog: {len(catalog)} entries\n")
    for name in pages:
        path = AUDIT_DIR / name
        if not path.is_file():
            print(f"  (skip {name}: not found)")
            continue
        r = _measure_page(path, catalog)
        agg["agenda"] += r["agenda"]
        agg["with_identity"] += r["with_identity"]
        agg["resolved"] += r["resolved"]
        agg["noise_unresolved"] += r["noise_unresolved"]
        agg_source.update(r["by_source"])
        agg_method.update(r["by_method"])
        _print_page(r)

    agenda = agg["agenda"]
    identity = agg["with_identity"]
    resolved = agg["resolved"]
    ds_denom = identity - agg["noise_unresolved"]
    print("=== AGGREGATE ===")
    print(f"  agenda rows          : {agenda}")
    print(f"  rows w/ DS identity  : {identity}")
    print(f"  rows resolved (T1)   : {resolved}")
    print(f"  annotation (no-prism): {agg['noise_unresolved']}")
    print(
        f"  agenda coverage      : "
        f"{round(100 * resolved / agenda, 1) if agenda else 0}%"
    )
    print(
        f"  identity coverage    : "
        f"{round(100 * resolved / identity, 1) if identity else 0}%"
    )
    print(
        f"  design-system cov.   : "
        f"{round(100 * resolved / ds_denom, 1) if ds_denom else 0}%"
    )
    print(f"  by source            : {dict(agg_source.most_common())}")
    print(f"  by method            : {dict(agg_method.most_common())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
