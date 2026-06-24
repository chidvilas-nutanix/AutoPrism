# 02 — Phase 1: the fetch fix (preserve + capture component identity)

> Status: **DONE** (2026-06-24). Roadmap **P1** — the keystone. This is the
> durable record of the change: what we did, where, why, how it stays
> backward-compatible, and how it was verified. Every claim cites `path:line`
> or `module.function`. Cold readers: read `01-current-state-analysis.md`
> first (it proves *why* this is the highest-leverage unlock).

## 0. TL;DR

The Figma `/nodes` response carries a sibling **`components`** map that turns
every `INSTANCE`'s node-local `componentId` into a stable **global
`componentKey`**. The fetcher was throwing it away. We now:

1. **Preserve** `components` / `componentSets` / `styles` in fetch
   (`FetchedTree`), without changing the legacy `document`-only contract.
2. **Thread** those maps `server.map_figma_tree → walk_tree → _WalkContext`.
3. **Capture** the exact identity onto every INSTANCE/COMPONENT region as
   `MappedRegion.figma_component` (a new `FigmaComponentIdentity`), and
   **surface** it on the lean wire response.

Nothing maps to a Prism component *via the key* yet — that is **P2** (build the
`componentKey → Prism` catalog) and **P3** (Tier-1 routing). P1 makes the
deterministic signal *available*; today it rides alongside the existing fuzzy
pick. **642 passed, 6 skipped**; zero golden-fixture churn.

## 1. The gap (recap, see `01` for the full trace)

`_unwrap_response` returned only `payload["nodes"][node_id]["document"]`,
discarding the `components` / `componentSets` / `styles` siblings. `walk_tree`
had no parameter to receive them, so INSTANCE emission never read
`componentId`. Identity was therefore 100% fuzzy (BM25 + dense + RRF), even
though audit data shows **100%** of 65,327 real instances resolve through the
components map (roadmap §1.6).

## 2. What changed, file by file

### 2.1 `figma/fetch.py` — preserve the maps (backward-compatible)

- **New `FetchedTree` dataclass** (`fetch.py:106`) — frozen, four fields:
  `document`, `components`, `component_sets`, `styles`. Heavy docstring
  explains the maps are *siblings* of `document`.
- **New `_unwrap_response_full(payload, node_id) -> FetchedTree`**
  (`fetch.py:550`) — extracts `document` + all three maps, each defaulting to
  `{}` when absent (older mocks, partial responses).
- **New `_fetch_figma_tree_full(...) -> FetchedTree`** (`fetch.py:400`) — the
  real fetch path (REST GET, retries, 1-hour disk cache); both the cache-hit
  and cache-miss branches now return via `_unwrap_response_full`.
- **`_fetch_figma_tree(...)` is now a thin wrapper** (`fetch.py:500`) that
  awaits `_fetch_figma_tree_full` and returns `.document`. **Byte-for-byte
  identical** to the old behaviour for every existing caller.
- **`_unwrap_response(...)` is now a thin wrapper** (`fetch.py:601`):
  `return _unwrap_response_full(payload, node_id).document`.

> Why a sibling entrypoint instead of changing the return type? `01` §4: six
> call-sites + scripts assert `_fetch_figma_tree(...) -> dict[document]`.
> Keeping it intact = no churn; new callers opt into `_full`.

### 2.2 `figma/models.py` — the identity model + wire surface

- **New `FigmaComponentIdentity(BaseModel)`** (`models.py:392`,
  `extra="forbid"`). Fields: `component_id`, `component_key`,
  `component_name` (prefers the component-**set** name — the variant-family
  identity), `component_set_id`, `component_set_key`, `remote`, `description`,
  `doc_url`. The docstring spells out that `component_key` is the P2 join key.
- **`MappedRegion.figma_component: FigmaComponentIdentity | None = None`**
  (`models.py:562`) — optional; `None` for non-instances / legacy path.
- **`FigmaTreeMapping.to_lean_response`** (`models.py:630`) now emits
  `figma_component` on each lean agenda row (only when present — stays compact).

### 2.3 `figma/walker.py` — thread maps + resolve identity

- **`walk_tree`** (`walker.py:164`) gains optional `components`,
  `component_sets`, `styles` params (default `None`). Stored on
  `_WalkContext` (`walker.py:372-374`) as `or {}`.
- **`_parse_doc_url(description)`** (`walker.py:1257`) + `_DOC_URL_RE`
  (`walker.py:1248`) — pull the first `http(s)` styleguide URL out of a
  description, stripping trailing prose punctuation.
- **`_resolve_figma_identity(node, ctx)`** (`walker.py:1272`) — the core. For
  `INSTANCE` joins on `node["componentId"]`; for `COMPONENT` on the node's own
  `id`. Looks up `ctx.components`, prefers the owning `componentSets` entry's
  name/description, returns a `FigmaComponentIdentity` — or `None` when the
  maps are absent / node isn't an instance / id is detached.
- **Wired into both emit paths** so every kept instance/component region gets
  it: `_emit_simple_region` (`walker.py:1335`),
  `_emit_instance_equivalent_without_recursion` (`walker.py:1184`), and
  `_emit_pattern_region` (`walker.py:1433`).

### 2.4 `server.py` — use the full fetch and pass the maps

- Imports `_fetch_figma_tree_full` (`server.py:41`); `map_figma_tree` calls it
  (`server.py:676`), logs the map sizes (`server.py:681`, lazy `%d` args — `G`
  rule), and threads `fetched.document/components/component_sets/styles` into
  `walk_tree` (`server.py:700-706`).

### 2.5 `figma/__init__.py`

- Exports `FigmaComponentIdentity` (added to `__all__`) so tests and future
  catalog code import it from the package root.

## 3. Backward-compatibility guarantees

- `_fetch_figma_tree(...) -> dict` and `_unwrap_response(...) -> dict`
  unchanged in signature **and** value.
- `walk_tree` new params are optional + default empty ⇒ existing calls
  (incl. `map_figma_node_fn=None` unit-test path) are unaffected.
- `_resolve_figma_identity` early-returns `None` when `ctx.components` is empty,
  so **no maps ⇒ `figma_component` is `None`** ⇒ **no golden-fixture diffs**.
  Verified: the full e2e fixture suite passes untouched.
- `FigmaComponentIdentity` is additive + optional on the wire; lean rows only
  carry the key when resolved.

## 4. Tests

| File | Adds |
|---|---|
| `tests/test_figma_fetch.py` | `FetchedTree` / `_unwrap_response_full` preserve all maps; missing maps default to `{}`; `_fetch_figma_tree_full` returns populated maps; **legacy `_fetch_figma_tree` still returns document-only**. |
| `tests/test_figma_map_tree_tool.py` | Monkeypatches `_fetch_figma_tree_full` to return a `FetchedTree` with a `components` map; asserts the lean agenda surfaces `figma_component` (key/name/remote/doc_url). |
| `tests/test_figma_identity.py` *(new)* | `_parse_doc_url` extraction/trim; instance identity from the components map; set-name/set-key preference; **no-maps ⇒ `None`** (back-compat); detached `componentId` ⇒ `None`; COMPONENT-via-own-`id`. |

## 5. Verification

```bash
# focused
uv run ruff check tests/test_figma_identity.py tests/test_figma_fetch.py \
  tests/test_figma_map_tree_tool.py --output-format=concise   # all checks passed
uv run pytest tests/test_figma_identity.py tests/test_figma_fetch.py \
  tests/test_figma_map_tree_tool.py tests/test_figma_walker.py -q

# whole repo
uv run pytest -q        # 642 passed, 6 skipped (~4.3s)
```

The 6 skips are pre-existing + intentional: network-gated integration fetches
and the temporarily-disabled spatial-layout-inference tests
(`docs/x-ray-walker-investigation.md` §13). No new skips, no failures.

> Pre-existing `ruff` debt in untouched code (ambiguous `×`, a couple of
> forward-ref / redundant-cast nits in `layout_inference.py`, `patterns.py`,
> `utils.py`, older `walker.py` lines) was **left as-is** — out of scope for
> P1 and not introduced by this change.

## 6. What this unlocks / what's next

P1 only *captures* the key; it does not yet *route* on it. The pick you see in
the agenda is still the fuzzy one. The deterministic payoff lands next:

- **P2 — catalog.** Build `componentKey → Prism component` (+ set-key + doc-URL
  slug) from the 5 Figma libraries (`figma-source-links.md`). The
  `description`/`doc_url` we now surface is the styleguide slug the catalog
  keys on.
- **P3 — Tier-1 routing.** When `figma_component.component_key` hits the
  catalog, make it the `primary_recommendation` and **skip** BM25/dense for
  that region. Wire `figma_mapping._resolve_primary_recommendation`
  (`figma_mapping.py:864`) to consult the catalog before patterns.
- **P4 (coverage-doc).** The slash-name regex `_KNOWN_COMPONENT_SLASH_RE`
  (`routing.py:208`) still rejects emoji/space names like `"Action/ ✅ Button"`
  — separate lexical-fallback fix, now lower priority since the key is exact.

Until P2 lands, `figma_component` is an **observability win**: every agenda row
now shows the exact library component + global key + styleguide link it came
from, which is also the dataset we mine to build the catalog.
