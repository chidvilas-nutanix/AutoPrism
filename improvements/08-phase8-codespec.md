# 08 — Phase 8: Code-Spec Output + Cursor Contract ("render this verbatim")

> Roadmap **P8** — fold the five per-region resolution layers (identity / props
> / layout / tokens / content) into one **render-ready tree** the Cursor skill
> emits 1:1, instead of leaving the LLM to assemble scattered agenda fields into
> JSX (where improvisation — and drift — creep in).
>
> Read `03`–`07` first: P8 consumes everything those phases hang off
> `MappedRegion` / `LayoutNode`. It adds **no** new resolution — it is a pure
> assembly + serialisation pass. Claims cite `path:line` / `module.function`.

---

## 0. TL;DR

P2–P6 each answer one question per region and stash the answer on the agenda:
`prism_resolution` (which component), `prism_props` (which props), the layout
node's `prism_layout` / `prism_shell` (which container), `box_style` tokens +
`typography` (which tokens), `prism_icon` / `content_binding` (which glyph /
which prop). The skill still had to **join** those by hand and decide nesting —
the last place the model could improvise.

P8 closes it with one pure module and an opt-in wire mode:

- New `figma/codespec.py`: `build_code_spec(mapping)` joins the agenda +
  layout forest by id and emits a `PrismCodeSpec` — a nested tree of
  `PrismCodeNode`s, each carrying its final JSX `tag`, `import_from`, typed
  `props`, `children`/`text`, `slot`, `flex_grow`, `tokens`, and
  `source`/`confidence`/`notes`. Deduped `imports` come for free.
- The **tag cascade** (`_element_for`) is a trust-ordered pick: icon → catalog
  identity → high-conf pattern → page shell → layout primitive → fuzzy mapper →
  `<div>` fallback.
- A conservative **bbox-containment re-parent** (`_reparent_roots`) recovers the
  single page tree the walker flattens at pure-container boundaries.
- A **prune** (`_prune_redundant_wrappers`) drops empty `<div/>` scaffolding and
  collapses single-child wrappers — the literal P8 "zero extra divs" metric.
- `models.py`: `response_detail="codespec"` routes `leanify_tree_mapping`
  through `build_code_spec` (lazy import — `codespec` imports the models). The
  walker and the lean/full shapes are **untouched**, so every golden is
  byte-identical.
- The `figma-page-to-prism` skill now leads with "call `codespec`, render it
  verbatim, drill into `map_figma_node` only for flagged nodes".

Measured over 8 real pages (deterministic floor, `map_figma_node_fn=None`):
**75.2%** of spec nodes resolve to a real Prism element (`catalog` 714 +
`layout` 405 + `icon` 78 + `shell` 6 of 1600); the prune leaves only
genuine-unresolved `<div>`s (no scaffolding); most pages collapse to a **single
root**. With the live mapper the resolved fraction climbs further (the floor
excludes BM25/semantic suggestions).

---

## 1. The gap, precisely

| | Before P8 | P8 |
|---|---|---|
| What the skill receives | a flat `agenda` + a forest `layout_tree`, joined by id | one nested `PrismCodeSpec` tree |
| Component decision | LLM re-reads `prism_resolution` / `candidates` per row | already on the node as `tag` |
| Props | LLM re-derives from `prism_props` + `content_binding` + `box_style` | already on the node as typed `props` / `text` |
| Nesting | LLM walks `children_ids`, guesses where flattened containers go | the tree IS the nesting (containment re-parented) |
| Imports | LLM tracks names it used | deduped `imports` list |
| Extra divs | LLM decides when a wrapper is needed | scaffolding pruned away |

The walker deliberately stays conservative about spatial structure (see the
X-Ray investigation §13), so `layout_tree` is a **forest** — pure containers
(FRAMEs that emit no region) return no id to their parent
(`walker.py:1316` returns `region_id=None`) yet append themselves separately
(`walker.py:1300-1314`). Empirically a complex page yields ~10 flat roots even
though it is one nested page. P8 owns turning that into a render tree.

---

## 2. The model (`codespec.py`)

`PrismCodeNode` is the fully-resolved join:

```
figma_id   the source Figma id (join key back to the agenda)
tag        JSX element: Button / FlexLayout / MenuIcon / HeaderFooterLayout / div
import_from "@nutanix-ui/prism-reactjs", or None for a host <div>
props      [PrismProp{name, value, value_kind: expr|string|bool|slot}]
text       literal element text, or None
children   [PrismCodeNode]   (render order)
slot       fills a named parent prop (a shell's header/body/…), or None
flex_grow  wrap in <FlexItem flexGrow="1"> (parent marked it a fill child)
source     icon | catalog | pattern | shell | layout | mapper | fallback
confidence 0-1 trust in `tag`
tokens     Prism design-token names referenced (color + typography)
notes      <div> fallback reason; composite "pull an example" flag
```

`PrismCodeSpec` wraps `roots` + deduped `imports` + the `tokens` passthrough +
`stats` (`nodes` / `resolved` / `fallbacks` / `roots` / `imports` /
`max_depth`) + `warnings`.

---

## 3. The tag cascade (`_element_for`)

Trust-ordered, highest first (`codespec.py::_element_for`):

1. **icon** — `region.prism_icon` (a resolved `*Icon`); a leaf, no children.
2. **catalog** — `region.prism_resolution.is_mapped` (Tier-1 `componentKey`
   identity, authoritative).
3. **pattern** — `mapping.primary_recommendation` at confidence ≥ 0.8 (a
   deterministic role pick, e.g. `kpi-tile` → `Tile`).
4. **shell** — the layout node's `prism_shell` (page skeleton; its slots assign
   children to named props).
5. **layout** — the layout node's `prism_layout` (`FlexLayout` /
   `StackingLayout` / `ContainerLayout`, with its token-snapped props).
6. **mapper** — `mapping.suggested_component_name` (the fuzzy ranker's pick).
7. **fallback** — `<div>`, carrying a `note` so the gap is auditable.

Props come from `prism_props` (typed, P3) + a non-`children` `content_binding`
as an attribute (P6); a `children`-kind binding becomes the node's `text`.
Tokens come from `box_style.{background,border}_token` + `typography.style_token`
(P5). Composite families (`Table`/`Tables`/`Navigation`/`Modal`/…) get a `note`
to imitate a `map_figma_node` example rather than nest their decomposed
sub-parts.

---

## 4. Single tree: containment re-parent (`_reparent_roots`)

The forest's roots are the layout nodes no other node references. Each
orphan root is attached to the **smallest** other node whose bbox strictly
contains it (`_contains`, 1px tolerance, strictly-greater area). No cycle is
possible (a container is strictly bigger, so never a descendant), and a root
with no unambiguous container stays a root — **no node is ever lost**. On the
8-page set this collapses the typical ~10 roots to **1** for well-formed pages.

This is a localised containment check, not the heavier spatial layout inference
the walker avoids: it only re-attaches already-built nodes, never invents
direction/gap/alignment.

---

## 5. Zero extra divs: the prune (`_prune_redundant_wrappers`)

Bottom-up, on a **bare** fallback `<div>` (no props/text/tokens/slot/flexGrow —
`_is_bare_fallback`):

- **0 children** → dropped (an empty `<div/>` renders nothing).
- **1 child** → collapsed into that child (the child keeps its slot/flexGrow).
- **≥2 children** → kept (a real anonymous grouping; dropping it would
  re-flatten its children).

A bare fallback with tokens or a slot is **not** pruned (it still paints / fills
a shell slot). This is the explicit P8 success metric: the `<div>`s that survive
are genuine unresolved regions, not scaffolding.

> **Trade-off:** an empty leaf fallback that *did* paint an un-tokenised colour
> is dropped. P5 tokenises ~98.5% of backgrounds, so this is a <2% edge; the
> "zero extra divs" win is worth it. Un-tokenised painted boxes with children
> are retained (≥2-child rule), and any node with a colour token survives.

---

## 6. Wire + skill

- `MapFigmaTreeInput.response_detail` gains `"codespec"`; `leanify_tree_mapping`
  routes it through `build_code_spec` via a **lazy import** (the `codespec`
  module imports the models, so a top-level import would be circular). `"lean"`
  / `"full"` are untouched → all goldens byte-identical.
- `server.py` already threads `input.response_detail`; its tool docstring +
  `SERVER_INSTRUCTIONS` now document the codespec render contract.
- `.cursor/skills/figma-page-to-prism/SKILL.md`: the map phase calls
  `response_detail="codespec"`; the compose phase walks the tree and emits each
  node verbatim (props by `value_kind`, `slot`/`flex_grow` handling), drilling
  into `map_figma_node` **only** for `source:"fallback"` nodes and composite
  `notes`.

---

## 7. Metrics (`scripts/measure_codespec.py`)

Hermetic, `map_figma_node_fn=None` (the deterministic floor, same convention as
the P5/P6 drivers). 8 real pages:

```
spec nodes -> Prism element : 1203/1600  (75.2%)
by source : catalog 714 | layout 405 | fallback 397 | icon 78 | shell 6
roots collapsed to 1 on 6/8 pages (login + cloudconnect have disjoint top frames)
```

The 397 fallbacks are dominated by composite sub-parts (e.g. `table-column`
regions whose parent `Tables` resolved — the deferred-from-P6 decomposition) and
un-keyed annotation frames; with the live mapper many pick up a
`suggested_component_name` and leave the fallback bucket.

---

## 8. Tests

- `tests/test_figma_codespec.py` (27): the tag cascade (each rung + the
  precedence between them), props/text/tokens assembly, shell slots + flexGrow,
  the prune (drop empty / collapse single-child / keep multi-child), the
  containment re-parent (single root + disjoint stays split), import dedup +
  sort, the cycle/depth guards, stats, and the `leanify(..., "codespec")` shape.
- `tests/test_figma_map_tree_tool.py`: a new end-to-end case asserting the tool
  returns the `PrismCodeSpec` wire shape for `response_detail="codespec"`.
- Full suite: **881 passed, 7 skipped** (was 854) — every prior golden
  unchanged (codespec is opt-in).

---

## 9. Deferred

- **Composite decomposition** (Tables columns, Form items, Modal footer) — the
  P6 follow-up. P8 flags these with a `note`; binding the decomposed sub-parts
  into config props (`columns` / `items`) is its own increment.
- **Shell-as-root hoist** — when a fallback page-frame wraps a single detected
  shell, the shell could become the root. Low value (the wrapper renders as one
  `<div>`); skipped for v1.

---

## 10. File inventory

| File | Change |
|---|---|
| `src/prism_mcp/figma/codespec.py` | **new** — models + `build_code_spec` + cascade / re-parent / prune |
| `src/prism_mcp/figma/models.py` | `response_detail="codespec"`; `leanify_tree_mapping` routes it |
| `src/prism_mcp/figma/__init__.py` | export `PrismCodeSpec` / `PrismCodeNode` / `PrismImport` / `PrismProp` / `build_code_spec` |
| `src/prism_mcp/server.py` | tool docstring + `SERVER_INSTRUCTIONS` document the codespec contract |
| `.cursor/skills/figma-page-to-prism/SKILL.md` | render-verbatim flow + codespec response shape |
| `scripts/measure_codespec.py` | **new** — render-readiness / zero-div / root / import metrics |
| `tests/test_figma_codespec.py` | **new** — 27 assembly tests |
| `tests/test_figma_map_tree_tool.py` | new codespec wire-contract test |
