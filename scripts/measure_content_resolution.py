"""Measure icon + text-binding resolution on real pages (roadmap P6).

``measure_token_resolution.py`` answers *"do colors/text resolve to Prism
design tokens?"*. This driver answers the **content** question P6 owns:

* **Icons** — of the regions the walker tagged as an icon (``role='icon'``),
  how many resolved to a concrete Prism ``*Icon`` component (so codegen emits
  ``<MenuIcon />`` instead of an inline ``<svg>`` or a guess)?
* **Text binding** — of the regions that carry text **and** resolved to a
  component (via the P2/P3 catalog), how many know *which prop* that text
  fills (``children`` / ``label`` / ``title`` …)?

It replays saved page dumps through the real
:func:`prism_mcp.figma.walker.walk_tree` with a **real** :class:`IconIndex`
built hermetically from the committed prop-schema artifact's 206 ``*Icon``
components (no tarball download, no network) and the committed P3 prop
schema. ``map_figma_node_fn=None`` keeps it identity-independent — the only
component resolutions are the deterministic catalog ones, so the numbers are
a floor (the live tool also has BM25/semantic suggestions on top).

Run from the repo root::

    uv run python scripts/measure_content_resolution.py
    uv run python scripts/measure_content_resolution.py path/to/page.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from prism_mcp.figma.content import _normalize_icon, build_icon_index
from prism_mcp.figma.prop_schema import DATA_PATH as PROP_SCHEMA_PATH
from prism_mcp.figma.walker import walk_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "docs" / "_audit_data"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "figma"

# Same screen set as the layout / token drivers so every P-phase metric is
# measured over identical pages. Missing paths are skipped.
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
    """Return the ``{"document": …, "components": …}`` node from a dump.

    Handles the three on-disk shapes (raw ``/nodes`` response, a single-node
    ``{"document": …}`` wrapper, and a bare committed fixture) — identical to
    the layout / token drivers' loaders.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "nodes" in raw:
        return next(iter(raw["nodes"].values()))
    if isinstance(raw, dict) and "document" in raw:
        return raw
    return {"document": raw}


def _build_icon_index() -> Any:
    """Build the icon vocabulary from the committed prop-schema artifact.

    Hermetic: reads the same ``data/prism_prop_schema.json`` the live tool
    ships, collects every component whose name ends in ``Icon`` (the 206-icon
    Prism vocabulary), and feeds them to :func:`build_icon_index` — the exact
    builder the server calls, just sourced offline instead of from the index.
    """
    artifact = json.loads(PROP_SCHEMA_PATH.read_text(encoding="utf-8"))
    names = [n for n in artifact.get("components", {}) if n.endswith("Icon")]
    version = str(artifact.get("rplib_version", "local"))
    index = build_icon_index(names, version=version)
    print(f"# icon index: {len(index)} icons from {PROP_SCHEMA_PATH.name}\n")
    return index


def _region_text(region: Any) -> str | None:
    """Return the region's representative text, mirroring the walker pass."""
    for key in ("title", "label", "text", "value", "header"):
        value = region.content_slots.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# Generic Figma vector/layer names that carry no glyph identity — a region
# named ``"Vector 39"`` / ``"Fill 3"`` / ``"Button Icon"`` is an un-renamed SVG
# path, not a nameable icon, so NO deterministic resolver can map it. These
# split the raw icon count into "addressable" (designer named the glyph) and
# "unnamed" (an upstream design-hygiene ceiling, not a resolver miss).
_GENERIC_ICON_TOKENS: frozenset[str] = frozenset(
    {
        "",
        "vector",
        "fill",
        "group",
        "ellipse",
        "rectangle",
        "rect",
        "union",
        "subtract",
        "intersect",
        "exclude",
        "path",
        "shape",
        "mask",
        "frame",
        "line",
        "polygon",
        "star",
        "boolean",
        "clip",
        "oval",
        "compound",
        "background",
        "button",
        "content",
        "container",
        "wrapper",
        "placeholder",
        "image",
        "img",
        "bg",
    }
)


def _icon_hint(region: Any) -> str:
    return str(region.content_slots.get("icon_name_hint") or region.name or "")


def _is_addressable_icon(name: str) -> bool:
    """``True`` when an icon name plausibly identifies a glyph.

    Normalizes like the resolver, strips a trailing run number
    (``"vector39"`` → ``"vector"``), and rejects the generic Figma
    primitive-layer vocabulary.
    """
    norm = _normalize_icon(name).rstrip("0123456789")
    return bool(norm) and norm not in _GENERIC_ICON_TOKENS


def _measure_page(path: Path, icon_index: Any) -> dict[str, Any]:
    """Walk one page and tally icon + text-binding resolution."""
    node = _load_document(path)
    document = node["document"]

    mapping = walk_tree(
        tree_json=document,
        components=node.get("components", {}),
        component_sets=node.get("componentSets", {}),
        styles=node.get("styles", {}),
        map_figma_node_fn=None,
        icon_index=icon_index,
        **_NO_LIMITS,
    )

    icon_total = icon_resolved = 0
    addr_total = addr_resolved = 0
    icon_by_method: Counter = Counter()
    # Text-binding denominator: regions that both carry text AND resolved to a
    # component (a binding needs a target component to bind into).
    bind_total = bind_resolved = 0
    bind_by_prop: Counter = Counter()
    for region in mapping.agenda:
        if region.role == "icon":
            icon_total += 1
            addressable = _is_addressable_icon(_icon_hint(region))
            if addressable:
                addr_total += 1
            if region.prism_icon is not None:
                icon_resolved += 1
                icon_by_method[region.prism_icon.method] += 1
                if addressable:
                    addr_resolved += 1

        has_component = region.prism_resolution is not None
        if has_component and _region_text(region):
            bind_total += 1
            if region.content_binding is not None:
                bind_resolved += 1
                bind_by_prop[region.content_binding.prop] += 1

    return {
        "page": path.name,
        "icon_total": icon_total,
        "icon_resolved": icon_resolved,
        "icon_pct": (
            round(100 * icon_resolved / icon_total, 1) if icon_total else 0.0
        ),
        "addr_total": addr_total,
        "addr_resolved": addr_resolved,
        "addr_pct": (
            round(100 * addr_resolved / addr_total, 1) if addr_total else 0.0
        ),
        "icon_by_method": dict(icon_by_method.most_common()),
        "bind_total": bind_total,
        "bind_resolved": bind_resolved,
        "bind_pct": (
            round(100 * bind_resolved / bind_total, 1) if bind_total else 0.0
        ),
        "bind_by_prop": dict(bind_by_prop.most_common()),
    }


def _print_page(r: dict[str, Any]) -> None:
    print(f"=== {r['page']} ===")
    print(
        f"  icon region -> Prism icon : {r['icon_resolved']}/{r['icon_total']}"
        f"  ({r['icon_pct']}%)"
    )
    print(
        f"    of addressable (named)  : {r['addr_resolved']}/{r['addr_total']}"
        f"  ({r['addr_pct']}%)"
    )
    if r["icon_by_method"]:
        print(f"  icon by method            : {r['icon_by_method']}")
    print(
        f"  text region -> prop bind  : {r['bind_resolved']}/{r['bind_total']}"
        f"  ({r['bind_pct']}%)"
    )
    if r["bind_by_prop"]:
        print(f"  binding by prop           : {r['bind_by_prop']}")
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

    tot_icon = sum(r["icon_total"] for r in results)
    tot_icon_r = sum(r["icon_resolved"] for r in results)
    tot_addr = sum(r["addr_total"] for r in results)
    tot_addr_r = sum(r["addr_resolved"] for r in results)
    tot_bind = sum(r["bind_total"] for r in results)
    tot_bind_r = sum(r["bind_resolved"] for r in results)
    agg_method: Counter = Counter()
    agg_prop: Counter = Counter()
    for r in results:
        agg_method.update(r["icon_by_method"])
        agg_prop.update(r["bind_by_prop"])

    print("=" * 60)
    print("AGGREGATE")
    print(f"  pages                     : {len(results)}")
    icon_pct = round(100 * tot_icon_r / tot_icon, 1) if tot_icon else 0.0
    print(
        f"  icon region -> Prism icon : {tot_icon_r}/{tot_icon}  ({icon_pct}%)"
    )
    addr_pct = round(100 * tot_addr_r / tot_addr, 1) if tot_addr else 0.0
    print(
        f"    of addressable (named)  : {tot_addr_r}/{tot_addr}  ({addr_pct}%)"
    )
    print(f"  icon by method            : {dict(agg_method.most_common())}")
    bind_pct = round(100 * tot_bind_r / tot_bind, 1) if tot_bind else 0.0
    print(
        f"  text region -> prop bind  : {tot_bind_r}/{tot_bind}  ({bind_pct}%)"
    )
    print(f"  binding by prop           : {dict(agg_prop.most_common())}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
