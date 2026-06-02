"""Fetch + strip the two X-Ray Master File pages into walker fixtures.

One-off driver written to support
``docs/x-ray-walker-investigation.md`` §12 "Bonus" — persisting both
investigation pages (X-Ray-3 and X-Ray-4) as repeatable inputs in
``tests/fixtures/figma/``.

Run from the repo root::

    .venv/bin/python -m scripts.fetch_x_ray_fixtures

Requires ``FIGMA_TOKEN`` in the environment (typically read from
``.env`` by ``python-dotenv`` or set manually). The two figma pages
are the ones recorded in §1 and §11.1 of the investigation note.

Why a script rather than a test fixture generator: the existing
``_strip_fixture.py`` CLI reads raw REST responses from disk; this
driver wraps the ``_fetch_figma_tree`` async helper so we don't have
to round-trip through curl + manual disk file handling.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from prism_mcp.figma.fetch import _fetch_figma_tree, parse_figma_url
from tests.fixtures.figma._strip_fixture import strip

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "figma"

FIXTURES_TO_FETCH: list[tuple[str, str, str]] = [
    (
        "x-ray-3-results-progress-empty.json",
        "https://www.figma.com/design/au4217fUWv0x4p4surKH44/"
        "X-Ray-Master-File---A11y-Annotations?node-id=8464-80286",
        "Results - Test Details - Progress Empty",
    ),
    (
        "x-ray-4-gold-image-list.json",
        "https://www.figma.com/design/au4217fUWv0x4p4surKH44/"
        "X-Ray-Master-File---A11y-Annotations?node-id=954-132281",
        "Gold Image List",
    ),
]


async def _fetch_one(
    output_name: str,
    url: str,
    label: str,
) -> None:
    print(f"\n=== Fetching {label!r} ({url})")
    parsed = parse_figma_url(url)
    print(
        f"    file_key={parsed.file_key!r}  node_id={parsed.node_id!r}"
    )
    raw_document = await _fetch_figma_tree(
        parsed=parsed,
        figma_token=None,
        depth=20,
        bypass_cache=False,
    )
    stripped = strip(raw_document)
    raw_bytes = len(json.dumps(raw_document))
    out_path = FIXTURE_DIR / output_name
    out_path.write_text(
        json.dumps(stripped, separators=(",", ":")), encoding="utf-8"
    )
    print(
        f"    raw_document {raw_bytes:,} B -> "
        f"{out_path.name} {out_path.stat().st_size:,} B"
    )


async def _main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    if not FIXTURE_DIR.exists():
        raise SystemExit(f"fixture dir missing: {FIXTURE_DIR}")
    for name, url, label in FIXTURES_TO_FETCH:
        await _fetch_one(name, url, label)


if __name__ == "__main__":
    asyncio.run(_main())
