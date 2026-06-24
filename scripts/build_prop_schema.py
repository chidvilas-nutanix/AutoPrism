"""Build the committed Prism prop-schema artifact (roadmap P3 Part B).

Walks the cached rplib ``lib/components/v2/<Family>/*.d.ts``, parses every
``<Stem>Props`` interface + ``enum`` declaration, classifies each prop's
type (enum / union / boolean / …) against the family's enum pool, and
writes the versioned artifact consumed at runtime by
:class:`prism_mcp.figma.prop_schema.PropSchemaIndex`.

Offline + reproducible: reads only the local rplib cache under
``~/.cache/prism-mcp/<version>/package``; no network.

Run from the repo root::

    uv run python scripts/build_prop_schema.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from prism_mcp.figma.prop_schema import DATA_PATH, build_prop_schema

CACHE_ROOT = Path.home() / ".cache" / "prism-mcp"
V2_REL = Path("package") / "lib" / "components" / "v2"


def _find_package_version() -> tuple[Path, str]:
    """Return ``(v2_root, version)`` for the newest cached rplib.

    Prefers the ``latest`` pointer; falls back to the highest numeric
    version directory. Raises if no cache is present.
    """
    candidates: list[tuple[str, Path]] = []
    latest = CACHE_ROOT / "latest" / V2_REL
    if latest.is_dir():
        version = ""
        pkg_json = CACHE_ROOT / "latest" / "package" / "package.json"
        try:
            version = json.loads(pkg_json.read_text()).get("version", "")
        except (OSError, ValueError):
            version = "latest"
        return latest, version
    for child in sorted(CACHE_ROOT.glob("*")):
        v2 = child / V2_REL
        if child.is_dir() and child.name[0].isdigit() and v2.is_dir():
            candidates.append((child.name, v2))
    if not candidates:
        raise SystemExit(
            f"no rplib cache under {CACHE_ROOT}; warm it by starting the "
            f"MCP server / running a search once."
        )
    version, v2_root = max(candidates, key=lambda kv: kv[0])
    return v2_root, version


def _collect_families(v2_root: Path) -> dict[str, list[Path]]:
    """Return ``family -> [*.d.ts paths]`` for every v2 directory."""
    families: dict[str, list[Path]] = {}
    for folder in sorted(p for p in v2_root.iterdir() if p.is_dir()):
        dts = [
            p
            for p in sorted(folder.glob("*.d.ts"))
            if not p.name.endswith(".spec.d.ts")
        ]
        if dts:
            families[folder.name] = dts
    return families


def main() -> int:
    v2_root, version = _find_package_version()
    families = _collect_families(v2_root)
    print(f"rplib {version}: {len(families)} v2 families at {v2_root}")

    artifact = build_prop_schema(families, rplib_version=version)
    n_components = len(artifact["components"])
    n_props = sum(
        len(c.get("props", {})) for c in artifact["components"].values()
    )
    kinds: dict[str, int] = {}
    for comp in artifact["components"].values():
        for prop in comp.get("props", {}).values():
            kinds[prop["kind"]] = kinds.get(prop["kind"], 0) + 1

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps(artifact, separators=(",", ":"), sort_keys=False) + "\n",
        encoding="utf-8",
    )
    size_kb = DATA_PATH.stat().st_size / 1024

    print(
        f"wrote {DATA_PATH.name}: {n_components} components, "
        f"{n_props} props, {size_kb:.0f} KB"
    )
    print(f"  families   : {len(artifact['families'])}")
    print(f"  prop kinds : {dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}")
    enum_props = kinds.get("enum", 0)
    union_props = kinds.get("union", 0)
    print(
        f"  resolvable : {enum_props} enum + {union_props} union "
        f"= {enum_props + union_props} value-typed props"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
