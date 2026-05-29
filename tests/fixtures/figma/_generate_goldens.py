"""Regenerate ``*.expected.json`` golden files from the current walker.

This is an internal developer script, NOT a test. Run it with:

    .venv/bin/python -m tests.fixtures.figma._generate_goldens

after intentionally changing walker behaviour. Pytest provides a
``--update-figma-golden`` flag for the same purpose; this script
exists as a stand-alone fallback for ad-hoc debugging.

The golden captures the *stable* shape of the walker output that
should not regress silently:

- ``summary`` — the full structured summary dict.
- ``agenda`` — minimal ``[{id, role, name}]`` snapshot. The
  ``mapping`` field intentionally omitted because it depends on
  the live Prism library index (covered by the e2e test
  separately).
- ``layout_tree`` — ``[{id, role, name, children_ids}]`` so
  parent/child topology is asserted but bboxes / tokens aren't.
- ``dropped_by_reason`` — histogram only. Per-node ``DroppedNode``
  details are too noisy to lock down at this stage.

The golden is what ``tests/test_figma_walker.py`` compares against.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from prism_mcp.figma import FigmaTreeMapping, walk_tree

FIXTURE_DIR = Path(__file__).parent


def build_golden(mapping: FigmaTreeMapping) -> dict[str, Any]:
    return {
        "summary": mapping.summary,
        "agenda": [
            {
                "id": region.id,
                "role": region.role,
                "name": region.name,
            }
            for region in mapping.agenda
        ],
        "layout_tree": [
            {
                "id": node.id,
                "role": node.role,
                "name": node.name,
                "children_ids": node.children_ids,
            }
            for node in mapping.layout_tree
        ],
        "dropped_by_reason": dict(
            Counter(item.reason for item in mapping.dropped)
        ),
        "warnings": list(mapping.warnings),
    }


def regenerate(fixture_path: Path) -> Path:
    tree = json.loads(fixture_path.read_text())
    mapping = walk_tree(
        tree_json=tree,
        reference_jsx=None,
        variable_defs=None,
        map_figma_node_fn=None,
    )
    golden = build_golden(mapping)
    expected_path = fixture_path.with_suffix(".expected.json")
    expected_path.write_text(
        json.dumps(golden, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return expected_path


def main() -> None:
    fixtures = sorted(
        p
        for p in FIXTURE_DIR.glob("*.json")
        if not p.name.endswith(".expected.json")
    )
    if not fixtures:
        print("no fixtures found in", FIXTURE_DIR)
        return
    for fixture in fixtures:
        out = regenerate(fixture)
        print(f"  wrote {out.name}")


if __name__ == "__main__":
    main()
