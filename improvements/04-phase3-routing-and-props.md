# 04 — Phase 3: Tier-1 routing + prop resolution

> Roadmap **P3** — "the exact props layer." Two coupled halves:
>
> - **Part A — Tier-1 routing** (the keystone that makes the P2 catalog
>   *live* in the pipeline): **DONE** (2026-06-24).
> - **Part B — prop resolution** (`.d.ts` schema + Figma
>   `componentProperties` → exact typed props): **DONE** (2026-06-24).
>
> Read `02-phase1-fetch-fix.md` (made `componentKey` available) and
> `03-phase2-catalog.md` (turns the key into a Prism family) first.
> Claims cite `path:line` / `module.function`.

---

## 0. TL;DR

**Part A (shipped).** The walker now runs a deterministic **Tier-1
routing** pass after the DFS: it resolves every region's P1
`figma_component` identity through the P2 catalog and records the
authoritative Prism family on `MappedRegion.prism_resolution`,
promoting it into the headline recommendation — *unless* an audited
pattern role already claimed a finer sub-component. This turns the
mapper from "BM25 guess" into "exact `componentKey` → Prism family" for
the majority of real-page components.

Measured end-to-end on three X-Ray pages: of the **872** agenda rows
that carry a real design-system identity, **775 (88.9%)** now ship a
deterministic Prism family; **613** via a precomputed catalog key-hit
and **162** via the page-fallback cascade. (The instance-level number
is higher — 97.7%, `03-phase2-catalog.md` — because whole-screen
*template* frames surface as agenda rows that legitimately have no
single atomic Prism component.)

**Part B (shipped).** The walker now runs a second post-DFS pass that
turns each routed region's Figma `componentProperties` into **exact typed
props** on `MappedRegion.prism_props` — `type={ButtonTypes.PRIMARY}`,
`disabled={false}`, `appearance="default"`, `children`. It is
**value-driven**: a Figma axis value `Primary` matches `ButtonTypes`'
string value `"primary"` (→ `ButtonTypes.PRIMARY`) without needing the
axis *name* to match the prop name. A new prop-schema artifact
(`data/prism_prop_schema.json`, 339 components / 1,874 props) supplies
the enum value maps + union literal sets the entity index lacks.

Measured on three X-Ray pages: across the **configurable** leaf
components (Button, Badge, Input, Checkbox, Alert, …) **75% (303/406)**
of Figma axes that correspond to a real Prism prop resolve
deterministically, at ~100% precision (Button 80–98%). The headline
*finding*: a large share of Figma variant axes are **design-system
visual descriptors, not props** — Tables/Select/Menu are built
declaratively from `dataSource`/`columns`, so their per-cell axes
(`Type=Normal`, `Double-Line`) have no prop by construction (4%,
44/1,059). The resolver correctly *declines* those rather than inventing
props. §6–§9 document the build, the data, and the finding.

---

# PART A — Tier-1 routing (DONE)

## A1. Where it plugs in (data flow)

The walker's two-phase shape (verified in `01-current-state-analysis.md`)
is unchanged; routing is a third, additive phase:

```
walk_tree(tree_json, components, component_sets, styles, catalog=None)
  │
  ├─ _visit() DFS …………………… emits MappedRegion rows; P1 stamps
  │                              region.figma_component (componentKey,
  │                              name, description, doc_url)
  ├─ _resolve_pending_mappings() … BM25/dense fuzzy candidates +
  │                              PATTERN_TO_PRIMARY headline (Tier-3/Tier-2)
  └─ _apply_catalog_routing(ctx) ★ NEW (Tier-1): catalog.resolve_region()
                                 → region.prism_resolution + headline
```

Key fact that makes Part A *clean*: each region's `.mapping` is a fresh
`model_copy` (`walker.py:1996`), so the routing pass can mutate each
region's headline independently after dedup, and `region.figma_component`
(P1) is already populated by DFS time.

## A2. What shipped (code)

- **`figma/models.py`** — new optional field
  `MappedRegion.prism_resolution: RegionResolution | None` (the Tier-1
  outcome: Prism family + `method` + `confidence` + `source`).
  `to_lean_response` surfaces a compact 4-field form **only when the
  identity resolved**, so every pre-P3 fixture/mock row is byte-for-byte
  unchanged (the no-identity row never gains the key).
- **`figma/walker.py`**
  - `walk_tree` gains `catalog: FigmaCatalog | None = None` and
    `catalog_routing: bool = True`; both stored on `_WalkContext`.
  - `_apply_catalog_routing(ctx)` (`walker.py:2077`) — the pass. Returns
    the resolved count; the call site is `walker.py:286`.
  - `_promote_resolution_to_headline(region, res)` (`walker.py:2178`) —
    the override rule.
  - `_PATTERN_HEADLINE_CONFIDENCE = 1.0` (`walker.py:2063`) — mirrors
    `figma_mapping._PRIMARY_RECOMMENDATION_CONFIDENCE` (a test pins the
    coupling).
  - `summary["catalog_resolved"]` emitted **only when non-zero**
    (mirrors the conditional `dropped_<reason>` keys → no golden churn).
- **`figma/__init__.py`** — already exported the catalog symbols in P2;
  no change needed.
- **`server.py`** — **no change.** `map_figma_tree` already threads the
  `components` map (P1) and lets `catalog`/`catalog_routing` default, so
  the lazy `get_catalog()` singleton loads once on the first real page.

## A3. The resolution + override rule (the important decision)

`_apply_catalog_routing` is **lazy and fault-tolerant by construction**:

1. Returns `0` immediately if routing is disabled **or no region carries
   an identity** — so the 1.78 MB artifact is never read on the
   document-only / stub-mapper path (every walker + golden unit test).
2. A missing/corrupt artifact → logged warning + `0` return; the walk
   still ships the P1 identity and the fuzzy candidates (never raises).

For each region whose identity **maps** (`RegionResolution.is_mapped`):

| Condition | `prism_resolution` | headline (`primary_recommendation` / `suggested_component_name`) |
|---|---|---|
| Audited pattern already set the headline (`confidence ≥ 1.0`) | **set** (provenance) | **kept** — the pattern's finer sub-component (`TableColumn`, `ButtonGroup`) is strictly more specific than the catalog *family* and was audited at 100% agreement |
| Otherwise | **set** | **overwritten** with the catalog family — an exact `componentKey` (Tier-1) dominates the BM25/dense fuzzy ranker (Tier-3) |

Rationale string: `Tier-1 <source>: <method> (componentKey <8>… -> <Family>)`.

**Why a separate pass and not inside `map_figma_node`?** The mapper's
dedup `cache_key` is keyed on *fuzzy* inputs; two instances with
identical fuzzy signals but different `componentId`s would share a cache
entry. Resolving identity per-region *after* the copy avoids that trap
and keeps the deterministic engine (catalog) decoupled from the fuzzy
engine (BM25).

## A4. Surfacing

`prism_resolution` rides on every agenda row (full dump) and, compactly,
in the lean wire shape consumed by the LLM:

```json
"prism_resolution": {
  "prism_component": "Tables",
  "source": "catalog",          // or "page-fallback"
  "method": "family-name",       // cascade tier that fired
  "confidence": 0.7
}
```

## A5. Measured impact (end-to-end, agenda level)

`scripts/measure_tier1_routing.py` replays the saved `/nodes` page dumps
through the **full walker** (`map_figma_node_fn=None`, real catalog
injected) and buckets the **agenda** rows the LLM actually receives:

| Page | agenda | w/ DS identity | resolved (T1) | DS coverage |
|---|---|---|---|---|
| `xray_login` | 345 | 139 | 90 | 82.6% |
| `xray_9188_127717` | 77 | 39 | 38 | 97.4% |
| `xray_cloudconnect` | 1090 | 805 | 647 | 89.4% |
| **aggregate** | **1512** | **983** | **775** | **88.9%** |

Source split (aggregate): `catalog` 613, `page-fallback` 162. Methods:
`family-name` 513, `styleguide-id` 191, `icon-family` 66, `ds-slug` 5.

**Agenda-level (88.9%) vs instance-level (97.7%, P2).** The denominator
differs: P2 counts atomic visible `INSTANCE`s; this counts post-walk
agenda rows, which include whole-screen *template* frame instances
("Home - Cloud connect…", "Results - Sync to X-Ray…") that carry a
`componentKey` but have no single atomic Prism equivalent. The remaining
genuine misses are exactly those page frames + custom composites
("Comment card") — **not** catalog failures. 111 annotation/spec frames
("Focus order", "A11y Text") are excluded from the DS denominator (they
correctly resolve to nothing).

## A6. Tests + verification

`tests/test_figma_tier1_routing.py` (**+12**): catalog key-hit promotion,
page-fallback when the key is absent, audited-pattern headline is
preserved, `_promote_resolution_to_headline` override vs no-op, the
`_PATTERN_HEADLINE_CONFIDENCE` coupling guard, routing-disabled,
unmapped-entry, missing-artifact non-fatal, summary count, and lean
surfacing (present only when resolved).

```bash
uv run python scripts/measure_tier1_routing.py   # 88.9% agenda DS coverage
uv run ruff check src/prism_mcp/figma/walker.py src/prism_mcp/figma/models.py \
  scripts/measure_tier1_routing.py tests/test_figma_tier1_routing.py  # clean*
uv run pytest -q                                  # 684 passed, 7 skipped
```

\* 4 ruff findings remain in `walker.py` but are **pre-existing** (verified
on `git show HEAD:…/walker.py`): `RUF001` `×` in a docstring, two
`UP037` forward-ref quotes, one `RUF046` — all in untouched code, none
introduced by P3.

---

# PART B — prop resolution (DONE)

> Goal (roadmap P3 "exact props"): for a routed region, emit the precise
> typed Prism props — `type={ButtonTypes.PRIMARY}`, `disabled`,
> `appearance="square"` — from the Figma instance's `componentProperties`
> and the component's `.d.ts` prop schema.

## 6. What already exists (reuse, don't rebuild)

`parsers/dts.py::parse_interfaces` is a robust, bracket-depth-aware
`.d.ts` scanner that already returns, per `export interface XxxProps`, a
list of `entities.Member` with: `name`, `type` (textual TS type),
`required` (from `?`), `default` (from `@default` JSDoc), and
`description` (JSDoc prose). Example — `Button.d.ts` (rplib 2.54.0):

```ts
export interface ButtonProps … {
  disabled?: boolean;
  type?: ButtonTypes;                                  // enum-typed
  appearance?: 'default' | 'square' | 'mini' | …;       // union literal
  fullWidth?: boolean;
  textButtonSize?: TextButtonSizes;                     // enum-typed
}
```

So the **prop name / required / default / doc** layer is solved. Two
gaps remain in `dts.py`:

1. **Enum value sets.** `type?: ButtonTypes` only carries the *type name*
   `"ButtonTypes"`. The enum body
   `export declare enum ButtonTypes { PRIMARY = "primary", … }` is **not
   parsed** — we need a `parse_enums(source) -> {EnumName: {MEMBER:
   "value"}}` so we can emit `ButtonTypes.PRIMARY` for a design value of
   `"primary"`.
2. **Union literal sets.** `appearance?: 'default' | 'square' | …` — the
   allowed string literals must be extracted from the `type` string so a
   design value of `Square` maps to `appearance="square"`.

## 7. The Figma side — `componentProperties` (measured)

Probed `xray_cloudconnect.json`: **5,773** instances carry
`componentProperties`. Property-kind histogram:

| kind | count | shape | example |
|---|---|---|---|
| `VARIANT` | 14,981 | `{"Weight": {"value": "Primary", "type": "VARIANT"}}` | the variant axes |
| `INSTANCE_SWAP` | 1,004 | `{"Icon Instance#20863:77": {"type": "INSTANCE_SWAP", "value": "15:766"}}` | nested instance (value = `componentId`) |
| `TEXT` | 876 | `{"Text#58147:0": {"type": "TEXT", "value": "New"}}` | text override (key has `#nodeId`) |
| `BOOLEAN` | 5 | `{"…": {"type": "BOOLEAN", "value": true}}` | rare |

**Three findings that shape the design:**

1. **Booleans are modeled as VARIANT `"True"`/`"False"`**, not as the
   `BOOLEAN` kind (only 5 of those page-wide). E.g. `Action/Link` carries
   `"Disabled": {"value": "False", "type": "VARIANT"}`,
   `"Underline": "False"`, `"Icon": "Right"`. So the variant handler must
   special-case `"True"`/`"False"` → boolean prop.
2. **Axis name ≠ prop name.** Button's Figma axes are `Weight`,
   `Selection`, `Icon`, `Nav`, `Size`, `Disabled` — none of which is the
   Prism prop `type`/`appearance`. A name-only join fails; this is the
   roadmap's flagged curation risk.
3. **Value ≈ enum value (the deterministic bridge).** Figma `Weight =
   Primary` lowercased is `"primary"`, which is exactly
   `ButtonTypes.PRIMARY`'s value. So a **value-driven** match (Figma
   value → enum member whose string value equals it, case-insensitively)
   resolves the enum prop *without* needing the axis name — covering a
   large fraction of VARIANT props deterministically.

## 8. What shipped (code)

The plan above was built as a self-contained value-driven pipeline.

**(1) `parsers/dts.py` — enum parsing.** Added `parse_enums(source) ->
[ParsedEnum(name, members)]` (member → string value, source order) plus
`_strip_comments` (both `//` and `/* */`). The comment strip is load-
bearing: spec-library JSDoc embeds literal `enum X { … }` *examples* that
naive scanning double-counts — `_strip_comments` removes them so only
real declarations parse. Numeric / bare members fall back to the member
name (they can never value-match a Figma string, which is correct).

**(2) `figma/prop_schema.py` — the schema index (new).** `classify_prop`
turns a parsed `Member` + the family's enum pool into a `PropSchema`
(`kind ∈ {enum, union, boolean, number, string, node, other}`, plus
`enum_members`, `values`, `accepts_string`). `build_prop_schema` walks
all 38 v2 families (enums pooled *across* a family's sibling `.d.ts`
files so a prop can reference an enum defined next door) into a
committed artifact, `data/prism_prop_schema.json` (**339 components,
1,874 props, 346 KB**). `PropSchemaIndex` is the lazy, cached runtime
loader (mirrors `FigmaCatalog`); it hard-fails on `schema_version` drift.

Crucially, `PropSchemaIndex.for_region(family, figma_name)` picks the
right **sub-component**: the catalog routes to a *family* directory, but
an instance named `"Table/Table Cell"` needs `TableCell`'s schema, not
the generic `Table`. It chooses the family component whose normalized
name is the longest substring of the normalized Figma name.

**(3) `figma/props.py` — the resolver (new).** `resolve_props(component_
properties, schema) -> PropResolution(props, unresolved)`, a deterministic
cascade per Figma property:
- `TEXT` → `children` (or a curated text prop), `value_kind="string"`.
- `INSTANCE_SWAP` → recorded as a nested-instance hint, **not** a prop
  (the swapped child is its own region).
- boolean (`True`/`False`, or `BOOLEAN` kind) → a `boolean` prop by name.
- **name+value** → prop whose normalized name == axis *and* whose
  enum/union value set contains the value.
- **value-only** → the unique enum/union prop whose value set contains
  the value (the bridge that carries most of the load).
- **curated** axis→prop override.
- else `unresolved` (kept for the metric, *not* surfaced to the LLM).

Emitted `ResolvedProp`s carry a JSX-ready `value` + `value_kind`
(`expr` → `prop={ButtonTypes.PRIMARY}`; `string` → `prop="square"`;
`bool` → `prop` / `prop={false}`) plus `method`/`confidence` provenance.

**(4) `figma/prop_overrides.py` — curated residue (new).** Per-family
`ignore_axes` for verified design-only axes + a `GLOBAL_IGNORE_AXES` set
(`placedOn`/`placeOn`/`mode` — doc-surface/theme axes that are never a
prop on any v2 component). Every ignore was checked against the real prop
list; comments cite *why* each axis has no Prism prop.

**(5) Walker wiring (`walker.py`).** `_stash_component_properties` stashes
each instance's raw `componentProperties` into a ctx side-map at
region-construction time (kept off the model so it never inflates the
wire payload). `_resolve_region_props` is a new post-routing pass: for
every region with a `prism_resolution` *and* stashed properties, it looks
up the sub-component schema via `for_region` and runs `resolve_props`,
storing typed props on `MappedRegion.prism_props`. Lazy + fault-tolerant
exactly like routing (a missing artifact downgrades to a warning).
`models.py` adds the `prism_props` field and surfaces a **compact triple**
(`{prop, value, value_kind}`) in `to_lean_response` — only when non-empty,
so every existing fixture is byte-for-byte unchanged. New `walk_tree`
params: `prop_schema` (inject for tests) + `prop_resolution` (master
switch). `server.py` needs no change (defaults enable it).

## 9. Measured impact + the key finding

`scripts/measure_prop_resolution.py` replays the three X-Ray pages
through the real walker (hermetic) and re-runs `resolve_props` over every
*routed* region's properties, tallying resolved vs unresolved (excluding
`INSTANCE_SWAP` and the verified design-only axes).

| Bucket | Coverage | Notes |
|---|---|---|
| **Configurable** (Button, Badge, Input, Checkbox, Alert, …) | **75% (303/406)** | the props a generator sets; ~100% precision |
| Button alone | **80–98%** | `type`, `disabled`, `appearance`, `children` |
| **Declarative** (Tables, Select, Menu, …) | **4% (44/1,059)** | variant axes are *not* props (see below) |
| Raw (all axes) | 23.7% (347/1,465) | dominated by declarative axes |

**The finding that matters for codegen:** Figma component *variant axes*
describe a **design-system visual variant**; they frequently do **not**
correspond to a Prism component prop. Two regimes:

- **Configurable leaf components** — Button/Badge/Checkbox/Alert expose
  scalar enum/union/boolean props, and their Figma axes map cleanly
  (`Weight=Primary` → `type={ButtonTypes.PRIMARY}`). The resolver nails
  these deterministically.
- **Declarative/structural components** — Prism Tables/Select are
  configured from `dataSource`/`columns`/`options` objects, *not* scalar
  props (verified: `TableCell`/`TableRow`/`Select` expose only object
  props). Their Figma per-cell axes (`Type=Normal`, `Side Panel
  Selection`, `Double-Line`) have **no Prism prop**, so the resolver
  correctly emits nothing rather than inventing a wrong prop.

So the honest, useful number is the **configurable** 75% at high
precision — not a single blended figure. The remaining configurable
residue is mostly more design-only axes (`Button:Type=Left Icon`,
`Position`) and a few semantic value-maps (Badge `State=Info` → `color`)
left for future curation.

## 10. Tests + verification

- `tests/test_parsers_dts.py` (+6): string/const/numeric/bare enums,
  the JSDoc-embedded-example regression guard, multi-enum, absent.
- `tests/test_figma_prop_schema.py` (+13): `classify_prop` for every
  kind (incl. `Enum | string`), cross-file enum pooling, artifact shape,
  `schema_version` drift raises, `for_family` / `for_region`
  sub-component selection, committed-artifact smoke test.
- `tests/test_figma_props.py` (+18): value→enum (incl. hyphenated),
  value→union, name+value precedence, booleans, text→children,
  instance-swap, misses, global + family ignores, `#nodeId` stripping,
  multi-axis.
- `tests/test_figma_prop_resolution_walker.py` (+9): end-to-end emission,
  lean compact surfacing, sub-component selection through the walker,
  `prop_resolution=False`, no-props, unrouted, missing-schema tolerance,
  `prop_resolved` summary suppression.
- **Full suite: 724 passed, 7 skipped.** New modules + scripts + tests
  are `ruff`-clean. (`walker.py` retains 5 *pre-existing* `UP037`/
  `RUF046`/`RUF001` lints unrelated to P3 — left per repo convention.)

## 11. Status

| Item | Status |
|---|---|
| Part A — Tier-1 routing (catalog → headline + `prism_resolution`) | **DONE** |
| Part A — measurement (`measure_tier1_routing.py`, 88.9%) | **DONE** |
| Part A — tests (`test_figma_tier1_routing.py`, +12) | **DONE** |
| Part B — `.d.ts` enum parsing (`parse_enums` + `_strip_comments`) | **DONE** |
| Part B — prop-schema index + artifact (`prism_prop_schema.json`) | **DONE** |
| Part B — variant→prop resolver + curated ignores | **DONE** |
| Part B — walker wiring + lean `prism_props` surfacing | **DONE** |
| Part B — measurement (configurable **75%**, declarative finding) | **DONE** |
| Part B — tests (+46 across 4 files) | **DONE** |

### Future work (not blocking)
- **Semantic value-maps** for status axes (Badge `State=Info` → blue
  `color`) — a per-family `{axis: {figma_value: prism_value}}` layer.
- **Declarative emit** for Tables/Select — synthesize `columns`/
  `dataSource` from the cell/row regions instead of per-cell props (a
  different mechanism than scalar prop resolution).
