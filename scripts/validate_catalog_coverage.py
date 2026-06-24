"""Measure the P2 catalog's instance-level coverage on real pages.

The roadmap's P2 success metric is "% of instances resolved across the
X-Ray pages (target >=95% non-viz)". This driver replays saved
``/v1/files/:key/nodes`` page dumps through the *real*
:class:`prism_mcp.figma.catalog.FigmaCatalog` — the same lookup P3
routing will use — and reports resolution, weighted by real usage.

For every visible ``INSTANCE`` it resolves
``componentId -> page components map -> global key -> catalog`` and
buckets the outcome:

* ``resolved``   — catalog hit that maps to a Prism component.
* ``unsupported``— catalog hit, but a known no-prism family (viz, brand).
* ``miss``       — key absent from the catalog (un-ingested / detached).
* ``noise``      — ``_`` scaffolding / a11y annotation (correctly ignored).

Run from the repo root::

    uv run python scripts/validate_catalog_coverage.py
    uv run python scripts/validate_catalog_coverage.py xray_login.json

Reads page dumps from ``docs/_audit_data/`` (no network).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from prism_mcp.figma.catalog import FigmaCatalog

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "docs" / "_audit_data"

DEFAULT_PAGES = [
    "xray_login.json",
    "xray_9188_127717.json",
    "xray_cloudconnect.json",
]

_NOISE_TOKENS = (
    "a11y",
    "focus order",
    "annotation",
    "skip link",
    "@spec",
    "@metadata",
)


def _is_noise(*names: str) -> bool:
    """A region is noise if any of its names is scaffolding/annotation."""
    for name in names:
        low = (name or "").strip().lower()
        if low.startswith("_") or low.startswith("@"):
            return True
        if any(tok in low for tok in _NOISE_TOKENS):
            return True
    return False


def _collect_instances(
    node: dict, visible: bool = True, acc: list | None = None
) -> list[tuple[str, str]]:
    """Return ``(componentId, instance_name)`` for visible instances."""
    acc = acc if acc is not None else []
    vis = node.get("visible", True) and visible
    if vis and node.get("type") == "INSTANCE":
        acc.append((node.get("componentId"), node.get("name", "")))
    for child in node.get("children", []) or []:
        _collect_instances(child, vis, acc)
    return acc


def _validate_page(path: Path, catalog: FigmaCatalog) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    node = next(iter(raw["nodes"].values()))
    document = node["document"]
    comp_map = node.get("components", {})
    set_map = node.get("componentSets", {})

    instances = _collect_instances(document)
    buckets = Counter()
    by_method = Counter()
    by_source = Counter()
    by_prism = Counter()
    unmapped_keys: Counter = Counter()

    for component_id, inst_name in instances:
        cm = comp_map.get(component_id) or {}
        set_id = cm.get("componentSetId")
        sm = set_map.get(set_id) if set_id else None
        logical = (sm or {}).get("name") or cm.get("name", "")
        desc = cm.get("description") or (sm or {}).get("description", "")

        if _is_noise(inst_name, logical):
            buckets["noise"] += 1
            continue

        res = catalog.resolve_region(
            component_key=cm.get("key"),
            figma_name=logical,
            description=desc,
            component_set_key=(sm or {}).get("key"),
        )
        if res.is_mapped:
            buckets["resolved"] += 1
            by_method[res.method] += 1
            by_source[res.source] += 1
            by_prism[res.prism_component] += 1
        elif not cm.get("remote", False) and sm is None:
            # remote=False + no set => a locally-built / detached frame
            # (the genuine Tier-3 fallback bucket, not design-system).
            buckets["local"] += 1
        else:
            buckets["miss"] += 1
            unmapped_keys[logical or inst_name or "(blank)"] += 1

    total = len(instances)
    non_noise = total - buckets["noise"]
    ds_denom = non_noise - buckets["local"]
    resolved = buckets["resolved"]
    return {
        "page": path.name,
        "total": total,
        "buckets": dict(buckets),
        "coverage_pct": round(100 * resolved / total, 1) if total else 0.0,
        "coverage_excl_noise_pct": (
            round(100 * resolved / non_noise, 1) if non_noise else 0.0
        ),
        "ds_coverage_pct": (
            round(100 * resolved / ds_denom, 1) if ds_denom else 0.0
        ),
        "by_source": dict(by_source.most_common()),
        "by_method": dict(by_method.most_common()),
        "by_prism": dict(by_prism.most_common(12)),
        "top_unmapped": unmapped_keys.most_common(10),
    }


def main() -> int:
    catalog = FigmaCatalog.load()
    pages = sys.argv[1:] or DEFAULT_PAGES

    agg = Counter()
    print(f"catalog: {len(catalog)} entries\n")
    for name in pages:
        path = AUDIT_DIR / name
        if not path.is_file():
            print(f"  (skip {name}: not found)")
            continue
        r = _validate_page(path, catalog)
        for k, v in r["buckets"].items():
            agg[k] += v
        agg["total"] += r["total"]
        print(f"=== {r['page']} ===")
        print(f"  instances            : {r['total']}")
        print(f"  buckets              : {r['buckets']}")
        print(f"  coverage             : {r['coverage_pct']}%")
        print(f"  coverage excl noise  : {r['coverage_excl_noise_pct']}%")
        print(f"  design-system cov.   : {r['ds_coverage_pct']}%")
        print(f"  by source            : {r['by_source']}")
        print(f"  by method            : {r['by_method']}")
        print(f"  top Prism            : {r['by_prism']}")
        print(f"  top unmapped         : {r['top_unmapped']}\n")

    total = agg["total"]
    resolved = agg["resolved"]
    non_noise = total - agg["noise"]
    ds_denom = non_noise - agg["local"]
    print("=== AGGREGATE ===")
    print(f"  instances            : {total}")
    print(f"  buckets              : {dict(agg)}")
    print(
        f"  coverage             : "
        f"{round(100 * resolved / total, 1) if total else 0}%"
    )
    print(
        f"  coverage excl noise  : "
        f"{round(100 * resolved / non_noise, 1) if non_noise else 0}%"
    )
    print(
        f"  design-system cov.   : "
        f"{round(100 * resolved / ds_denom, 1) if ds_denom else 0}%"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
