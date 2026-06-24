"""Measure token & typography resolution on real pages (roadmap P5).

``measure_layout_resolution.py`` answers *"is this container a Prism Layout
instead of a div?"*. This driver answers the **"tokens, not literals"**
question P5 owns: *"of the colors and text the walker captured as raw
``#RRGGBB`` / ``fontSize: 18px`` literals, how many now resolve to a Prism
design token (``@dark-blue-2`` / ``title-h2``)?"* — the metric that decides
whether generated code reads like a Prism app or like inline CSS.

It replays saved page dumps through the real
:func:`prism_mcp.figma.walker.walk_tree` with a **real**
:class:`~prism_mcp.tokens_index.ColorTokenIndex` built hermetically from the
committed Prism ``*.less`` token files (no tarball download, no network).
``map_figma_node_fn=None`` keeps it identity-independent — color + typography
resolution is pure geometry/style.

Two coverage numbers, both a *floor*:

* **Color** — distinct visible page hexes (the walker's ``tokens`` map) that
  resolved to a token. The page dumps carry no ``get_variable_defs`` map, so
  this measures the *perceptual-index-only* path; the live tool also has the
  designer's exact variable names on top, so production coverage is ≥ this.
* **Typography** — of the agenda regions that actually carry text, how many
  got a Prism type-ramp ``Typography``.

Run from the repo root::

    uv run python scripts/measure_token_resolution.py
    uv run python scripts/measure_token_resolution.py path/to/page.json
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

from prism_mcp.figma.walker import walk_tree
from prism_mcp.parsers.tokens import walk_tokens
from prism_mcp.tokens_index import ColorTokenIndex, build_color_token_index

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "docs" / "_audit_data"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "figma"

# Same screen set as ``measure_layout_resolution.py`` so the two P-phase
# metrics are measured over identical pages. Missing paths are skipped.
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


def _load_document(path: Path) -> dict[str, Any]:
    """Return the ``{"document": …, "components": …}`` node from a dump.

    Handles the three on-disk shapes (raw ``/nodes`` response, a
    single-node ``{"document": …}`` wrapper, and a bare committed
    fixture) — identical to the layout driver's loader.
    """
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "nodes" in raw:
        return next(iter(raw["nodes"].values()))
    if isinstance(raw, dict) and "document" in raw:
        return raw
    return {"document": raw}

# Candidate roots for the committed Prism LESS token files. The library
# checkout is a sibling of the MCP repo in this workspace; the in-repo
# node_modules copies are the fallback. First hit with a ``src/styles/v2``
# wins. ``walk_tokens`` wants the package root (the dir that *contains*
# ``src/styles/v2``).
_PRISM_LIB_CANDIDATES = (
    REPO_ROOT.parent / "prism-ui-prism-reactjs-lib" / "services",
    REPO_ROOT / "prism-ui-prism-reactjs-lib" / "services",
)

_NO_LIMITS = {"max_depth": 100, "max_nodes": 500_000, "max_agenda": 100_000}


def _build_color_index() -> ColorTokenIndex:
    """Build the perceptual color index from the committed Prism LESS.

    Hermetic: parses ``Colors.less`` etc. with the same
    :func:`prism_mcp.parsers.tokens.walk_tokens` the slice-6 walker uses,
    then the same :func:`build_color_token_index` the live ``Library`` calls.
    Returns an empty index (coverage will read 0) if the library checkout
    isn't present — the script still runs and reports honestly.
    """
    for root in _PRISM_LIB_CANDIDATES:
        if (root / "src" / "styles" / "v2").is_dir():
            entities = walk_tokens(root, version="local")
            index = build_color_token_index(entities=entities, version="local")
            if len(index) > 0:
                print(f"# color index: {len(index)} tokens from {root}\n")
                return index
    print(
        "# WARNING: no Prism LESS token files found; color coverage will be 0.\n"
        "#          (looked under "
        + ", ".join(str(c) for c in _PRISM_LIB_CANDIDATES)
        + ")\n"
    )
    return build_color_token_index(entities=[], version="local")


def _region_has_text(region: Any) -> bool:
    """``True`` when the region carries text the designer would tokenize.

    A region "has text" when any of its content slots holds a non-empty
    string (or list of strings), or its children summary names a TEXT node.
    Used as the typography **denominator** so icon-only / pure-container
    regions don't count as misses.
    """
    for value in (region.content_slots or {}).values():
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(
            isinstance(v, str) and v.strip() for v in value
        ):
            return True
    summary = region.children_summary or ""
    return "TEXT" in summary


def _measure_page(path: Path, color_index: ColorTokenIndex) -> dict[str, Any]:
    """Walk one page and tally color + typography token resolution."""
    node = _load_document(path)
    document = node["document"]

    mapping = walk_tree(
        tree_json=document,
        components=node.get("components", {}),
        component_sets=node.get("componentSets", {}),
        styles=node.get("styles", {}),
        map_figma_node_fn=None,
        color_token_index=color_index,
        **_NO_LIMITS,
    )

    # Color: the page tokens map is hex -> token-name ("" when unresolved).
    hexes_total = len(mapping.tokens)
    hexes_resolved = sum(1 for v in mapping.tokens.values() if v)

    # Per-region surface/border tokenization.
    bg_total = bg_tok = border_total = border_tok = 0
    # Typography.
    text_regions = with_typo = 0
    typo_by_token: Counter = Counter()
    for region in mapping.agenda:
        box = region.box_style
        if box.background_color:
            bg_total += 1
            if box.background_token:
                bg_tok += 1
        if box.border_color:
            border_total += 1
            if box.border_token:
                border_tok += 1
        if _region_has_text(region):
            text_regions += 1
            if region.typography and region.typography.style_token:
                with_typo += 1
                typo_by_token[region.typography.style_token] += 1

    return {
        "page": path.name,
        "hexes_total": hexes_total,
        "hexes_resolved": hexes_resolved,
        "hex_pct": (
            round(100 * hexes_resolved / hexes_total, 1) if hexes_total else 0.0
        ),
        "bg_total": bg_total,
        "bg_tok": bg_tok,
        "border_total": border_total,
        "border_tok": border_tok,
        "text_regions": text_regions,
        "with_typo": with_typo,
        "typo_pct": (
            round(100 * with_typo / text_regions, 1) if text_regions else 0.0
        ),
        "typo_by_token": dict(typo_by_token.most_common()),
    }


def _print_page(r: dict[str, Any]) -> None:
    print(f"=== {r['page']} ===")
    print(
        f"  page hexes -> token     : {r['hexes_resolved']}/{r['hexes_total']}"
        f"  ({r['hex_pct']}%)"
    )
    print(f"  region background token : {r['bg_tok']}/{r['bg_total']}")
    print(f"  region border token     : {r['border_tok']}/{r['border_total']}")
    print(
        f"  text region typography  : {r['with_typo']}/{r['text_regions']}"
        f"  ({r['typo_pct']}%)"
    )
    if r["typo_by_token"]:
        print(f"  typography by token     : {r['typo_by_token']}")
    print()


def main(argv: list[str]) -> int:
    pages = [Path(a) for a in argv] if argv else list(DEFAULT_PAGES)
    pages = [p for p in pages if p.exists()]
    if not pages:
        print("no pages found; pass page-dump paths as arguments")
        return 1

    color_index = _build_color_index()
    results = [_measure_page(p, color_index) for p in pages]
    for r in results:
        _print_page(r)

    tot_hex = sum(r["hexes_total"] for r in results)
    tot_hex_r = sum(r["hexes_resolved"] for r in results)
    tot_bg = sum(r["bg_total"] for r in results)
    tot_bg_r = sum(r["bg_tok"] for r in results)
    tot_bd = sum(r["border_total"] for r in results)
    tot_bd_r = sum(r["border_tok"] for r in results)
    tot_txt = sum(r["text_regions"] for r in results)
    tot_txt_r = sum(r["with_typo"] for r in results)
    agg_typo: Counter = Counter()
    for r in results:
        agg_typo.update(r["typo_by_token"])

    print("=" * 60)
    print("AGGREGATE")
    print(f"  pages                   : {len(results)}")
    hex_pct = round(100 * tot_hex_r / tot_hex, 1) if tot_hex else 0.0
    print(f"  page hexes -> token     : {tot_hex_r}/{tot_hex}  ({hex_pct}%)")
    print(f"  region background token : {tot_bg_r}/{tot_bg}")
    print(f"  region border token     : {tot_bd_r}/{tot_bd}")
    typo_pct = round(100 * tot_txt_r / tot_txt, 1) if tot_txt else 0.0
    print(f"  text region typography  : {tot_txt_r}/{tot_txt}  ({typo_pct}%)")
    print(f"  typography by token     : {dict(agg_typo.most_common())}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
