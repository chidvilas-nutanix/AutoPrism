"""Build the committed Figma → Prism catalog artifact (roadmap P2).

Fetches (or loads from cache) the ``/v1/files/:key/components`` and
``/component_sets`` dumps for the five publishing libraries, resolves
every component to a Prism family via
:mod:`prism_mcp.figma.catalog`, and writes the versioned artifact to
``src/prism_mcp/figma/data/figma_catalog.json`` (committed to the repo).

Run from the repo root::

    uv run python scripts/build_figma_catalog.py            # cache-first
    uv run python scripts/build_figma_catalog.py --refetch  # force fetch

``FIGMA_TOKEN`` (read from ``.env``) is required only when a raw dump is
missing or ``--refetch`` is passed; a pure rebuild from cached dumps
needs no network. Raw dumps are cached under
``docs/_audit_data/catalog_raw/`` so the build is reproducible offline.

This is the *only* place the catalog is generated; the runtime
(:class:`prism_mcp.figma.catalog.FigmaCatalog`) merely loads the result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from prism_mcp.figma.catalog import (
    DATA_PATH,
    LibraryDump,
    build_catalog,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "docs" / "_audit_data" / "catalog_raw"

# The five publishing libraries (the four URL-less ones contribute the
# +13% "remote" coverage on real pages — roadmap §1.6). Consumer/spec
# files (Product Navigation, EBR & NU table, token-only files) publish
# nothing and are intentionally excluded.
LIBRARIES: list[tuple[str, str]] = [
    ("bK52NYtDhya7uiW7dvObaQ", "Design Library"),
    ("Z0OTY6BFR7oTCpRjCzs7lz", "Templates"),
    ("XNpH8JZdAYA3KSwODFqmSA", "Design Library for Visualizations"),
    ("KVbKkR0QAFRbTVpwysQLWw", "Spec Doc"),
    ("5de1bNROZOtWBx8c4ArKwr", "Color Primitives"),
]


def _figma_get(key: str, endpoint: str, token: str) -> dict:
    """GET ``/v1/files/:key/:endpoint`` with small retry/backoff."""
    url = f"https://api.figma.com/v1/files/{key}/{endpoint}"
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.load(resp)
        except Exception as exc:  # re-raised after retries exhausted
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"figma GET {url} failed: {last_exc}")


def _load_or_fetch(
    key: str, endpoint: str, list_key: str, *, refetch: bool, token: str | None
) -> list[dict]:
    """Return the ``meta[list_key]`` array, fetching+caching on miss."""
    cache_path = RAW_DIR / f"{key}.{endpoint}.json"
    if cache_path.is_file() and not refetch:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        if not token:
            raise SystemExit(
                f"missing raw dump {cache_path.name} and no FIGMA_TOKEN set; "
                f"set FIGMA_TOKEN in .env and re-run (optionally --refetch)"
            )
        payload = _figma_get(key, endpoint, token)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload.get("meta", {}).get(list_key, [])


def _detect_rplib_version() -> str:
    """Best-effort read of the cached rplib version (provenance only)."""
    cache_root = Path.home() / ".cache" / "prism-mcp"
    pkg = cache_root / "latest" / "package" / "package.json"
    try:
        return json.loads(pkg.read_text(encoding="utf-8")).get("version", "")
    except (OSError, ValueError):
        versions = [
            p.name
            for p in cache_root.glob("*")
            if p.is_dir() and p.name[0].isdigit()
        ]
        return sorted(versions)[-1] if versions else ""


def _print_report(artifact: dict) -> None:
    stats = artifact["stats"]
    print("\n=== Figma → Prism catalog ===")
    print(f"  rplib_version : {artifact['rplib_version'] or '(unknown)'}")
    print(f"  generated_at  : {artifact['generated_at']}")
    print(f"  total entries : {stats['total_entries']}")
    print(
        f"  mapped        : {stats['mapped_entries']} "
        f"({stats['mapped_pct']}%)"
    )
    print("\n  by method:")
    for method, count in stats["by_method"].items():
        print(f"     {method:20s} {count}")
    print("\n  by library:")
    for lib, count in stats["by_library"].items():
        print(f"     {lib:36s} {count}")
    print("\n  top Prism components:")
    for comp, count in list(stats["by_prism_component"].items())[:15]:
        print(f"     {comp:14s} {count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="force re-fetch of all raw dumps from the Figma API",
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    token = os.environ.get("FIGMA_TOKEN", "").strip() or None

    dumps: list[LibraryDump] = []
    for key, name in LIBRARIES:
        components = _load_or_fetch(
            key, "components", "components", refetch=args.refetch, token=token
        )
        component_sets = _load_or_fetch(
            key,
            "component_sets",
            "component_sets",
            refetch=args.refetch,
            token=token,
        )
        print(
            f"  {name:36s} components={len(components):5d} "
            f"sets={len(component_sets):4d}"
        )
        dumps.append(
            LibraryDump(
                key=key,
                name=name,
                components=components,
                component_sets=component_sets,
            )
        )

    artifact = build_catalog(dumps, rplib_version=_detect_rplib_version())

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Minified + deterministic (entries are pre-sorted in the builder):
    # a 3.8k-entry generated artifact is reviewed by regenerating, not by
    # reading the diff, so compactness beats pretty-printing here.
    DATA_PATH.write_text(
        json.dumps(artifact, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _print_report(artifact)
    print(f"\nwrote {DATA_PATH.relative_to(REPO_ROOT)} "
          f"({DATA_PATH.stat().st_size:,} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
