"""Measure code-spec assembly quality on real pages (roadmap P8).

The earlier drivers measure each resolution *layer* in isolation (does this
color/icon/prop resolve?). This one measures the **assembled deliverable** —
the :class:`prism_mcp.figma.codespec.PrismCodeSpec` the skill renders verbatim:

* **render-readiness** — what fraction of spec nodes resolved to a real Prism
  element vs the ``<div>`` fallback (the inverse is the "improvisation surface"
  the LLM would otherwise have to fill in);
* **zero extra divs** — the count of ``<div>`` nodes left after the prune (the
  literal P8 success metric);
* **single tree** — how many top-level roots survive the containment re-parent
  (1 = a clean page tree; >1 = spatially disjoint top frames);
* **imports** — the deduped Prism import count the file would need.

It replays saved page dumps through the real
:func:`prism_mcp.figma.walker.walk_tree` with a hermetic
:class:`~prism_mcp.figma.content.IconIndex` (the committed prop-schema
artifact's ``*Icon`` components) and ``map_figma_node_fn=None`` — so, like the
P5/P6 drivers, the numbers are a **floor**: only the deterministic catalog
resolutions count, and the live tool layers BM25/semantic suggestions on top.

Run from the repo root::

    uv run python scripts/measure_codespec.py
    uv run python scripts/measure_codespec.py path/to/page.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from prism_mcp.figma.codespec import build_code_spec
from prism_mcp.figma.prop_schema import DATA_PATH as PROP_SCHEMA_PATH
from prism_mcp.figma.walker import walk_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "docs" / "_audit_data"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "figma"

# The same screen set as the layout / token / content drivers so every
# P-phase metric is measured over identical pages. Missing paths are skipped.
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

_NO_LIMITS = {"max_depth": 100, "max_nodes": 500_000, "max_agenda": 100_000}


def _load_document(path: Path) -> dict[str, Any]:
    """Return the ``{"document": …, "components": …}`` node from a dump."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "nodes" in raw:
        return next(iter(raw["nodes"].values()))
    if isinstance(raw, dict) and "document" in raw:
        return raw
    return {"document": raw}


def _build_icon_index() -> Any:
    """Build the icon vocabulary from the committed prop-schema artifact."""
    from prism_mcp.figma.content import build_icon_index

    artifact = json.loads(PROP_SCHEMA_PATH.read_text(encoding="utf-8"))
    names = [n for n in artifact.get("components", {}) if n.endswith("Icon")]
    version = str(artifact.get("rplib_version", "local"))
    return build_icon_index(names, version=version)


def _walk_nodes(node: Any) -> list[Any]:
    out: list[Any] = []

    def _visit(n: Any) -> None:
        out.append(n)
        for child in n.children:
            _visit(child)

    for root in node:
        _visit(root)
    return out


def _measure_page(path: Path, icon_index: Any) -> dict[str, Any]:
    """Walk one page, assemble the spec, and tally its quality metrics."""
    doc = _load_document(path)
    mapping = walk_tree(
        tree_json=doc["document"],
        components=doc.get("components", {}),
        component_sets=doc.get("componentSets", {}),
        styles=doc.get("styles", {}),
        map_figma_node_fn=None,
        icon_index=icon_index,
        **_NO_LIMITS,
    )
    spec = build_code_spec(mapping)
    flat = _walk_nodes(spec.roots)
    by_source: Counter = Counter(n.source for n in flat)
    divs = sum(1 for n in flat if n.tag == "div")
    nodes = len(flat)
    resolved = nodes - by_source.get("fallback", 0)
    return {
        "page": path.name,
        "nodes": nodes,
        "resolved": resolved,
        "resolved_pct": round(100 * resolved / nodes, 1) if nodes else 0.0,
        "divs": divs,
        "roots": len(spec.roots),
        "imports": len(spec.imports),
        "max_depth": spec.stats.get("max_depth", 0),
        "by_source": dict(by_source.most_common()),
    }


def _print_page(r: dict[str, Any]) -> None:
    print(f"=== {r['page']} ===")
    print(
        f"  spec nodes -> Prism element : {r['resolved']}/{r['nodes']}"
        f"  ({r['resolved_pct']}%)"
    )
    print(f"  <div> fallbacks remaining   : {r['divs']}")
    print(f"  top-level roots             : {r['roots']}")
    print(f"  deduped imports / depth     : {r['imports']} / {r['max_depth']}")
    print(f"  by source                   : {r['by_source']}")
    print()


def main(argv: list[str]) -> int:
    pages = [Path(a) for a in argv] if argv else list(DEFAULT_PAGES)
    pages = [p for p in pages if p.exists()]
    if not pages:
        print("no pages found; pass page-dump paths as arguments")
        return 1

    icon_index = _build_icon_index()
    results = [_measure_page(p, icon_index) for p in pages]
    for r in results:
        _print_page(r)

    tot_nodes = sum(r["nodes"] for r in results)
    tot_resolved = sum(r["resolved"] for r in results)
    tot_divs = sum(r["divs"] for r in results)
    agg_source: Counter = Counter()
    for r in results:
        agg_source.update(r["by_source"])

    print("=" * 60)
    print("AGGREGATE")
    print(f"  pages                       : {len(results)}")
    pct = round(100 * tot_resolved / tot_nodes, 1) if tot_nodes else 0.0
    print(
        f"  spec nodes -> Prism element : {tot_resolved}/{tot_nodes}  ({pct}%)"
    )
    print(f"  <div> fallbacks remaining   : {tot_divs}")
    print(f"  by source                   : {dict(agg_source.most_common())}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
