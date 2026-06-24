# 01 — Current-state analysis (code-verified)

> Status: DONE (2026-06-24). A code-grounded map of today's Figma → Prism
> pipeline and the exact location of the `componentKey` gap. Written so the next
> agent does not have to re-trace the codebase. Every claim cites `path:line` or
> `module.function`.

## 1. The pipeline, end to end

The public MCP tool is `map_figma_tree` (`src/prism_mcp/server.py:540`). Its flow:

```
map_figma_tree(input: MapFigmaTreeInput)              server.py:540
  → parse_figma_url(input.node_url)                   figma/fetch.py:210
  → try_load_mock(parsed)  [offline short-circuit]    figma/mocks.py
  → tree_json = await _fetch_figma_tree(**kwargs)      figma/fetch.py:361  ← GAP
  → walk_tree(tree_json=tree_json, ...,                figma/walker.py:163
              map_figma_node_fn=_bound_map_figma_node)
  → leanify_tree_mapping(result, response_detail)     figma/models.py:688
```

### 1.1 Fetch (`figma/fetch.py`)
- `_fetch_figma_tree(...)` (`fetch.py:361`) performs the REST GET to
  `/v1/files/:key/nodes?ids=:id&depth=N`, with retries + a 1-hour disk cache.
- On success it returns **`_unwrap_response(payload, node_id)`** (`fetch.py:454`),
  which returns **only** `payload["nodes"][node_id]["document"]`.
- **This is the gap.** The Figma `/nodes` response shape (verified on
  `docs/_audit_data/xray_login.json`) is:

  ```jsonc
  { "nodes": { "<node_id>": {
      "document":      { ...the SceneNode tree... },
      "components":    { "<componentId>": { "key", "name", "description",
                                            "remote", "documentationLinks",
                                            "componentSetId"? } },
      "componentSets": { "<componentSetId>": { "key", "name", "description" } },
      "styles":        { "<styleId>": { "key", "name", "styleType" } },
      "schemaVersion": 0
  } } }
  ```

  `_unwrap_response` keeps `document` and **discards `components`,
  `componentSets`, `styles`.** That sibling `components` map is the *only* thing
  that turns an instance's node-local `componentId` into the **global
  `componentKey`** — the deterministic join into a Prism component.

### 1.2 Walk (`figma/walker.py`)
- `walk_tree(*, tree_json, ...)` (`walker.py:163`) takes the **document only**.
  It has no parameter for the maps — they never arrive.
- DFS in `_visit` (`walker.py:854`) runs a 7-pass noise filter
  (`figma/filter.py`), role routing (`figma/routing.py`), and 6 pattern
  detectors (`figma/patterns.py`), emitting one `MappedRegion` per kept node.
- INSTANCE nodes are emitted via `_emit_instance_equivalent_without_recursion`
  (`walker.py:1146`, the "Fix B" instance-boundary short-circuit) or
  `_emit_simple_region` (`walker.py:1210`). **Neither reads `node["componentId"]`**
  — there is nothing to resolve it against.
- Each region's component pick comes from `map_figma_node`
  (`figma_mapping.py:660`): BM25 + dense hybrid + RRF, plus small role/shape
  synonym bonuses. Resolved lazily after the DFS in
  `_resolve_pending_mappings` (`walker.py:1774`).

### 1.3 Identity today is fuzzy, not exact
- The only *deterministic* signal is `primary_recommendation`
  (`figma_mapping.py:864` `_resolve_primary_recommendation`), and it is driven
  **solely** by `PATTERN_TO_PRIMARY` (`figma_mapping.py:372`) — just **6 pattern
  roles** (`icon`, `stat-list`, `table-column`, `tab-strip`, `button-group`,
  `kpi-tile`). Everything else is BM25/dense guesswork.
- The slash-name regex `_KNOWN_COMPONENT_SLASH_RE` (`routing.py:208`,
  `^[A-Z][A-Za-z0-9]*(?:/[A-Z][A-Za-z0-9]*)+$`) matches **none** of the real
  emoji/space-decorated Figma names (`"Action/ ✅ Button"`), so even the lexical
  fallback is weak. (That is a P4-coverage-doc item, separate from P1.)

## 2. What the data proves (so we trust the fix)

From the audit scripts, which already do the exact resolution we want to port:

- `docs/_audit_data/analyze_xray2.py` and `aggregate_validation.py` resolve every
  INSTANCE as:
  ```python
  node = list(data["nodes"].values())[0]
  doc  = node["document"]
  cm   = node.get("components", {})        # componentId → entry
  sm   = node.get("componentSets", {})     # componentSetId → entry
  # for an INSTANCE n:
  e        = cm.get(n["componentId"])      # the entry
  setId    = e.get("componentSetId")
  setentry = sm.get(setId)
  logical  = (setentry or {}).get("name") or e.get("name")
  desc     = (setentry or {}).get("description") or e.get("description")
  key      = e.get("key")                  # ← global componentKey
  remote   = e.get("remote")
  ```
- Measured results (roadmap §1.6): across **65,327** real instances on 12 X-Ray
  product pages, **100%** resolve through the components map; **82%** are an
  exact key in bK52 today, **+13%** are `remote` keys from the other 4 libraries
  (deterministic once the catalog spans all 5), and only **<1%** are genuinely
  local/detached. Design-system coverage excl. noise = **99%**.

**Conclusion (same as both source docs):** preserving the `components` map is the
single highest-leverage unlock. It is the keystone P1.

## 3. Output shapes touched by the fix

- `MappedRegion` (`figma/models.py:392`) — the per-region agenda row. We add an
  optional `figma_component` identity object here.
- `FigmaTreeMapping.to_lean_response` (`figma/models.py:557`) — the lean wire
  shape. We surface `figma_component` on each lean agenda row (compact + high
  value; it is the whole point of the fix).
- `MapFigmaTreeInput` (`figma/models.py:28`) — unchanged; the maps come from the
  fetch, not the caller.

## 4. Backward-compatibility constraints discovered

- `_fetch_figma_tree` is imported/asserted as **returning the `document` dict**
  by: `server.py`, `tests/test_figma_fetch.py`, `test_figma_fetch_integration.py`,
  `test_server_tools.py`, `test_figma_map_tree_tool.py`, `scripts/fetch_x_ray_fixtures.py`.
  → The fix must keep `_fetch_figma_tree(...) -> dict[document]` intact and add a
  **sibling** entrypoint that also returns the maps.
- `walk_tree` is called by `server.py` and many tests with
  `map_figma_node_fn=None` (stub path). New map params must be **optional** and
  default to empty so every existing call keeps working byte-for-byte.
- Style: ruff line-length 80, double quotes, **no f-strings in logger calls**
  (`G` rule — use `%s` lazy args), heavy Google-style docstrings, `extra="forbid"`
  on all boundary models.

## 5. The fix in one diagram

```
BEFORE:  _fetch_figma_tree → document only → walk_tree(tree_json) → fuzzy pick
AFTER:   _fetch_figma_tree_full → FetchedTree{document, components,
                                              component_sets, styles}
              ↓ (server threads maps)
         walk_tree(tree_json, components, component_sets, styles)
              ↓ (emit helpers resolve componentId → entry)
         MappedRegion.figma_component = {component_key, component_name,
                                         remote, description, doc_url, …}
         (_fetch_figma_tree remains a thin wrapper → .document, unchanged)
```

Detailed change record: `02-phase1-fetch-fix.md`.
