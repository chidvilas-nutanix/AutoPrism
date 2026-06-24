# Worklog — Figma → Prism codegen build-out

> Chronological log. **Newest entry on top.** Each entry is one unit of work
> with: date, who/what, the change, the verification, and a pointer to the
> detailed doc. Keep entries short; put the depth in the numbered docs.

---

## 2026-06-24 — Fix: stale `low_confidence` warnings on a deterministic spec (DONE)

**Status: DONE.** Post-P8 correctness fix ("fix 1" from the deep-dive on
request `4043dd96-aefd-4503-852c-99de5a7f1806`). The X-Ray "Tests - Landing"
`codespec` resolved correctly (84/86 nodes deterministic) but shipped **34
`low_confidence` warnings**, so the consuming agent distrusted it and
hand-rolled the page. Full record: `09-warning-ordering-fix.md`.

**Root cause.** A **warning-ordering bug**, not a mapping bug. The per-region
audit (`_maybe_emit_low_confidence_warning`) ran at the tail of
`_resolve_pending_mappings` — i.e. **before** `_apply_catalog_routing`. Its
own guard (`if primary_recommendation is not None: return`) was therefore
dead: every region's headline was still `None`, so the audit flagged each one
against the fuzzy candidate the catalog was about to override. The warnings
were stale by construction, and `map_figma_tree` surfaces `mapping.warnings`
on every response mode.

**What shipped.**
- `figma/walker.py`: removed the audit loop from `_resolve_pending_mappings`;
  added `_emit_low_confidence_warnings(ctx)` (iterates `ctx.agenda`, since
  `mapping_jobs` is cleared by the resolver); `walk_tree` now calls it **after**
  `_apply_catalog_routing` + prop/content passes. Audit logic + threshold
  (`0.05`) unchanged — only *when* it reads the headline moved.
- `tests/test_figma_tier1_routing.py`: new
  `test_catalog_hit_suppresses_low_confidence_warning` pins both arms — a
  catalog-resolved instance emits no warning; an un-cascadable one still does.

**Measured (actual run, 34 warnings cross-referenced vs final node source).**
**32 suppressed** (final source deterministic: catalog/pattern/icon/layout/
shell); **2 remain** and are *honestly* low-confidence — `8658:56367`
(`Navigation/Header → NavBarLayout`) and `9188:127717` (root
`Tests - Landing → Pagination`), both `source: mapper` (the targets of the
deferred fix 3).

**Verification.**
```bash
uv run ruff check src/prism_mcp/figma/walker.py tests/test_figma_tier1_routing.py
#   -> only the 5 pre-existing walker.py findings; 0 new
uv run pytest -q                                            # all pass, 7 skipped
```

**Next (not done — explicitly scoped out).** Fix 2 (confidence floor on the
`_element_for` mapper rung), fix 3 (page-root/nav-header → shell/container),
fix 4 (`Input/Search` cascade → `Input`, not `Typography`).

---

## 2026-06-24 — P8 landed: code-spec output + Cursor contract (DONE)

**Status: DONE.** Roadmap **P8** — the unifier. The five per-region layers
(identity / props / layout / tokens / content) now fold into one **render-ready
`PrismCodeSpec`** that the skill emits 1:1, closing the last spot the LLM could
improvise. Full record: `08-phase8-codespec.md`.

**The unlock.** Everything was already resolved and stashed on `MappedRegion` /
`LayoutNode`; P8 only **assembles** it. `build_code_spec(mapping)` joins the
agenda + the flattened layout forest by id, picks each node's JSX element via a
trust-ordered cascade (icon → catalog → high-conf pattern → shell → layout →
fuzzy mapper → `<div>`), recovers the single page tree via a conservative
bbox-containment re-parent, and prunes empty/redundant `<div>` scaffolding.

**What shipped.**
- `figma/codespec.py` (new): `PrismCodeNode` / `PrismCodeSpec` + `build_code_spec`
  (`_element_for` cascade, `_reparent_roots` containment, `_prune_redundant_wrappers`
  zero-div pass, `_collect_imports` dedup).
- `models.py`: `response_detail="codespec"` → `leanify_tree_mapping` routes
  through `build_code_spec` (lazy import to dodge the model↔codespec cycle).
  **No** walker / lean / full change → all goldens byte-identical.
- `server.py` tool docstring + `SERVER_INSTRUCTIONS`, and the
  `figma-page-to-prism` skill, now lead with "call `codespec`, render verbatim,
  drill into `map_figma_node` only for flagged nodes".

**Measured (8 pages, deterministic floor, `map_figma_node_fn=None`).** **75.2%**
of spec nodes (1203/1600) resolve to a real Prism element (`catalog` 714 /
`layout` 405 / `icon` 78 / `shell` 6); the prune leaves only genuine-unresolved
`<div>`s; 6/8 pages collapse to a single root.

**Verification.**
```bash
uv run ruff check src/prism_mcp/figma/codespec.py tests/test_figma_codespec.py \
  scripts/measure_codespec.py src/prism_mcp/server.py        # clean
uv run pytest -q                                             # 881 passed, 7 skipped
uv run python scripts/measure_codespec.py                    # 75.2% resolved
```

**Next.** P7 (noise/annotation filtering — cheap, lifts every metric) and P9
(validation harness on the 18 vetted X-Ray pages). The composite decomposition
(Tables columns / Form items) remains the P6 follow-up.

---

## 2026-06-24 — P6 landed: content, slots & icons (DONE)

**Status: DONE** (icons + text→prop binding; slot/children composition scoped
as a follow-up). Roadmap **P6** — the "right glyph, right prop" layer. An icon
region now resolves to a concrete Prism `*Icon`, and a routed region's text
binds to the correct component prop. Full record: `07-phase6-content.md`.

**The unlock.** The walker already collapsed each glyph into one `role='icon'`
region with an `icon_name_hint`, captured region text into `content_slots`, and
shipped both the 206-icon vocabulary and the P3 prop schema — but nothing
*joined* them. P6 adds the two pure resolvers that do.

**What shipped.**
- `figma/content.py` (new): `resolve_icon` — normalized-name cascade (exact →
  curated synonym → conservative uniqueness-guarded fuzzy → unresolved) with a
  **generic-name stoplist**; and `bind_text_content` — named text prop from the
  P3 schema (by priority) with a `children` fallback for body-text leaves and
  **no** binding for containers.
- new `PrismIcon` / `ContentBinding`; `MappedRegion` += `prism_icon` /
  `content_binding`; lean surfaces both.
- `walk_tree(icon_index=…)` + `_resolve_region_content` seam (value-only, runs
  after prop resolution); **no-op when `icon_index is None`** → goldens
  byte-identical. `server.py` builds the index from `library.index()`.
- **Correctness guard found via measurement:** the fuzzy tier confidently
  mis-mapped structural layer names (`"Group"`→`GroupByIcon`,
  `"Icon + Text"`→`BoldTextIcon`). Added `_GENERIC_ICON_NAMES` stoplist →
  those resolve to `None`; method mix is now `exact 32 / synonym 4 / fuzzy 1`.

**Measured (8 pages, real 206-icon vocabulary).** Icons **43.5%** of
*addressable* (named) regions → a Prism icon (**14.8%** of all `role='icon'`;
the rest are un-renamed SVG path layers — a design-hygiene ceiling, not a
resolver limit); text→prop binding **56.7%** of routed text-carrying regions
(`children` 101 / `text` 12 / `label` 5). Deterministic floor — the semantic
layer routes more regions on top.

**Verification.**
```bash
uv run python scripts/measure_content_resolution.py   # icons 14.8/43.5%, bind 56.7%
uv run pytest -q                                       # 847 passed, 7 skipped
```
(+28 tests over the P5 baseline of 819. The 5 pre-existing `walker.py` ruff
findings remain untouched, as in P3/P4/P5.)

---

## 2026-06-24 — P5 landed: token & style resolution (DONE)

**Status: DONE.** Roadmap **P5** — the "tokens, not literals" layer. Region
colors and text now bind to Prism **design tokens** (`@dark-blue-2`,
`title-h2`) instead of `#1B6BCC` / `fontSize: 29px`. Full record:
`06-phase5-tokens.md`.

**The unlock.** The perceptual color index (`tokens_index.py::ColorTokenIndex`,
Oklab + CIEDE2000 over the LESS color tokens) already existed but the **page
walker never used it** — `_seed_tokens` only resolved designer-named hexes,
and TEXT styles were captured but never mapped. P5 wires the index in and adds
the one missing ramp (typography). Resolves from the **node tree alone**, so it
sidesteps the Variables-API `403` (roadmap §5 risk #2) and works on every file.

**What shipped.**
- `figma/tokens.py` (new): `resolve_color_token` — trust cascade (designer
  `variable_defs` → perceptual index `exact`/`near` → unresolved); and
  `resolve_typography` — curated Prism type ramp (`Variables.less`).
- `BoxStyle` += `background_token`/`border_token`; new `Typography` model;
  `MappedRegion` += `typography`; `utils.dominant_text_style` (largest TEXT).
- `walk_tree(color_token_index=…)` + `_resolve_region_tokens` seam (value-only
  — no agenda/topology change); lean surfaces the typography triple.
- **Bug fix** in `tokens_index.query`: a known role hint whose keywords matched
  zero token names returned `[]` (Prism tokens are named by hue, not role),
  violating the "never empty handed" contract. Caught by the measurement
  (background `0/842`); fixed → `829/842`.

**Measured (8 pages, real 105-token index).** Color **94.7%** page-hex /
**98.5%** background / **96.1%** border → token; typography **73.4%** of
text-carrying regions. (Perceptual-only floor — the live tool adds the
designer's exact variable names on top.)

**Verification.**
```bash
uv run python scripts/measure_token_resolution.py   # 94.7/98.5/96.1% color, 73.4% typo
uv run pytest -q                                     # 819 passed, 7 skipped (826 collected)
```
(+26 tests over the P4-follow-up baseline of 793. The 5 pre-existing
`walker.py` ruff findings remain untouched, as in P3/P4.)

---

## 2026-06-24 — P4 follow-ups landed: shells / FlexItem / Container / padding (DONE)

**Status: DONE.** All four P4 v1 non-goals (`05-phase4-layout.md` §3.4)
now ship in the same pure `figma/layout.py` + role-gated walker seam. Full
record: `05-phase4-layout.md` §7.

**What shipped.**
- **Page shells** — `detect_page_shell` (conservative geometric classifier
  on the page-scale frame) → `PrismPageShell` on `LayoutNode.prism_shell`
  (`MainPageLayout` / `HeaderFooterLayout` / `LeftNavLayout`), slot-name →
  child region id. Shell wins over a redundant `prism_layout`.
- **`FlexItem flexGrow`** — `detect_fill_children` (Figma `layoutGrow==1` /
  `layoutSizing*=="FILL"` on the main axis) → `PrismLayout.fill_child_ids`;
  a filling child upgrades a column `StackingLayout` to `FlexLayout`.
- **`ContainerLayout`** — styled non-flow boxes resolve to `ContainerLayout`
  with `backgroundColor` (white/dark/transparent by luminance) + `border`;
  colored fills stay on `box_style` for P5.
- **Padding** — `snap_padding(quad, component)` uses the target's token set
  (StackingLayout's 42 pairs vs FlexLayout's 9); asymmetric insets emit a
  `"-> use style"` escape note instead of a silent drop.

**Measured (8 pages).** Container coverage **82.4% → 86.4% (387/448)**;
+17 `ContainerLayout`, **6 page shells**, **73 FlexItem flexGrow** children,
padding props 58→62 (+ asymmetric now noted).

**Verification.**
```bash
uv run python scripts/measure_layout_resolution.py   # aggregate 86.4%
uv run pytest -q                                      # 793 passed, 7 skipped
```
(+27 tests over the P4 baseline of 766. The 5 pre-existing `walker.py` ruff
findings remain untouched, as in P3/P4.)

---

## 2026-06-24 — P4 landed: layout resolution (DONE)

**Status: DONE.** Roadmap **P4** — the "no divs / no CSS" layer. Figma
container frames now carry a deterministic **Prism Layout primitive**
(`FlexLayout` / `StackingLayout`) so codegen emits a layout component, not
a `<div style={{display:'flex',…}}>`. Full record: `05-phase4-layout.md`.

**The unlock.** The walker already had a complete, tested CSS-layout
engine (`figma/layout_inference.py::analyze_layout`) that was *disabled*
to keep output compact while the X-Ray fixes landed
(`x-ray-walker-investigation.md` §13). P4 revives it **compactly** and
adds the missing CSS→Prism mapping.

**What shipped.**
- `figma/layout.py` (new): `resolve_prism_layout` — maps the CSS
  `LayoutAnalysis` to a `PrismLayout` (primitive + token-snapped props);
  `snap_item_gap` (T-shirt ladder `XS=5…XXL=40`, verified in
  `Variables.less`), `snap_padding` (uniform / `{V}px-{H}px` / drop+note).
- `figma/models.py`: new `PrismLayout` model; `LayoutNode.prism_layout`.
- `figma/walker.py`: `_attach_prism_layout` (role-gated to structural
  containers; **no** per-child `absolute_pos` — that was the §13 bloat)
  revives the two disabled call sites. `figma/__init__.py`: exports.

**The rules.** `row`→FlexLayout; `column`→StackingLayout (pure stack) or
FlexLayout (when align/justify needed); `grid`→FlexLayout+flexWrap;
`single`/`stack`→none. `itemGap` named-token snap; `alignItems`/
`justifyContent` CSS→Prism, emitting only non-defaults.

**Measured (8 pages: 3 X-Ray dumps + 5 fixtures).** Structural-container
layout coverage **82.4% (369/448)**, up from 0% (all divs); **90%** via
Figma auto-layout at confidence 1.0. FlexLayout 277 / StackingLayout 92.
The 17.6% remainder is single-child / overlap frames that correctly need
no wrapper. `scripts/measure_layout_resolution.py`.

**Why it's safe.** Walker goldens snapshot only
`{id,role,name,children_ids}` from `layout_tree`, so the new field
doesn't churn them; the lean test compares full-vs-lean (both gain it).

**Tests.** +42 (`test_figma_layout.py` 37, `test_figma_layout_walker.py`
5). Full suite: **766 passed, 7 skipped**. New code ruff-clean (5
pre-existing `walker.py` findings untouched).

**Verification.**
```bash
uv run python scripts/measure_layout_resolution.py   # aggregate 82.4%
uv run pytest -q                                     # 766 passed, 7 skipped
```

**Next.** P5 (tokens) / P6 (content+icons) per the roadmap, or the P4
follow-ups in `05-phase4` §7: page shells (MainPageLayout/HeaderFooter/
LeftNav/SidePanel), `FlexItem flexGrow` (the filling child), and
`ContainerLayout` for styled boxes.

---

## 2026-06-24 — P3 Part B landed: prop resolution (DONE)

**Status: DONE.** Roadmap **P3 Part B** — Figma `componentProperties` →
exact typed Prism props. Full record: `04-phase3-routing-and-props.md`
(§8 build, §9 measured + finding, §10 tests). P3 (routing + props) is now
complete.

**What shipped.**
- `parsers/dts.py`: `parse_enums` (member→value map) + `_strip_comments`
  (strips JSDoc-embedded `enum X {…}` examples that would mis-parse).
- `figma/prop_schema.py` (new): `classify_prop` + `build_prop_schema` →
  committed `data/prism_prop_schema.json` (**339 components, 1,874 props,
  346 KB**); `PropSchemaIndex` lazy loader with `for_region(family,
  figma_name)` sub-component selection (`Table/Table Cell` → `TableCell`).
- `figma/props.py` (new): `resolve_props` — value-driven cascade
  (text→children, instance-swap hint, boolean-by-name, **name+value**,
  **value-only**, curated) emitting JSX-ready `value`+`value_kind`.
- `figma/prop_overrides.py` (new): verified per-family `ignore_axes` +
  `GLOBAL_IGNORE_AXES` (`placedOn`/`mode` — never a prop).
- `figma/walker.py`: `_stash_component_properties` + `_resolve_region_props`
  post-routing pass → `MappedRegion.prism_props`; lazy + fault-tolerant.
  `walk_tree` gains `prop_schema`/`prop_resolution`; `summary["prop_resolved"]`
  emitted only when non-zero.
- `figma/models.py`: new `prism_props` field; compact `{prop, value,
  value_kind}` triple in `to_lean_response`, only when non-empty (pre-P3
  fixtures byte-for-byte unchanged). `server.py`: no change.

**The bridge.** Figma axis *names* rarely match prop names, but axis
*values* match enum/union values: `Weight=Primary` →
`type={ButtonTypes.PRIMARY}` without a name match.

**The finding.** Many Figma variant axes are **design-system visual
descriptors, not props**. *Configurable* leaf components (Button/Badge/
Input/Checkbox/Alert) map cleanly; *declarative* components (Tables/
Select, built from `dataSource`/`columns`) have per-cell axes with no
Prism prop — the resolver correctly declines them.

**Measured (X-Ray).** Configurable coverage **75% (303/406)** at ~100%
precision (Button 80–98%); declarative **4% (44/1,059)** by construction;
raw 23.7%. `scripts/measure_prop_resolution.py`.

**Tests.** +46 across `test_parsers_dts.py`, `test_figma_prop_schema.py`,
`test_figma_props.py`, `test_figma_prop_resolution_walker.py`. Full
suite: **724 passed, 7 skipped**. New code ruff-clean.

**Verification.**
```bash
uv run python scripts/build_prop_schema.py        # 339 comp / 1,874 props
uv run python scripts/measure_prop_resolution.py  # configurable 75%
uv run pytest -q                                  # 724 passed, 7 skipped
```
(5 ruff findings in `walker.py` remain pre-existing, untouched by P3.)

**Next.** P4+ per the roadmap (token-by-name / a11y enrichment), or the
two non-blocking follow-ups in `04-phase3` §11: semantic value-maps
(Badge `State=Info`→`color`) and declarative emit (`columns`/`dataSource`
for Tables/Select).

---

## 2026-06-24 — P3 Part A landed: Tier-1 routing (DONE)

**Status: DONE.** Roadmap **P3 Part A** (the keystone that makes the P2
catalog *live*). Full record: `04-phase3-routing-and-props.md`. Part B
(prop resolution) shipped in the entry above.

**What shipped.**
- `figma/walker.py`: `_apply_catalog_routing` (`walker.py:2077`) — a
  post-DFS pass that resolves each region's P1 `figma_component` through
  the P2 catalog and records `MappedRegion.prism_resolution`, promoting
  it into the headline via `_promote_resolution_to_headline`
  (`walker.py:2178`). `walk_tree` gains `catalog` / `catalog_routing`
  args; `summary["catalog_resolved"]` emitted only when non-zero.
- `figma/models.py`: new optional `MappedRegion.prism_resolution`;
  surfaced compactly in `to_lean_response` **only when resolved** (no
  churn to pre-P3 fixtures/mocks).
- `server.py`: **no change** — already threads the `components` map; the
  catalog loads lazily via the cached `get_catalog()` singleton.

**The override rule.** Exact `componentKey` (Tier-1) beats BM25/dense
(Tier-3), so the catalog overwrites the headline — *except* when an
audited pattern role (`PATTERN_TO_PRIMARY`, confidence 1.0) already
claimed a finer sub-component (`TableColumn`, `ButtonGroup`); then the
catalog only corroborates via `prism_resolution`.

**Lazy + fault-tolerant.** No identity regions ⇒ artifact never read.
Missing/corrupt artifact ⇒ logged warning, walk still ships P1 identity
+ fuzzy candidates (never raises).

**Measured (X-Ray, agenda level).** Of 983 agenda rows with a DS
identity, **775 resolve (88.9% design-system coverage)** — 613 catalog
key-hits + 162 page-fallback. Lower than P2's instance-level 97.7%
because whole-screen template frames surface as agenda rows with no
atomic Prism equivalent (expected, not a catalog failure).

**Tests.** `tests/test_figma_tier1_routing.py` (+12). Full suite:
**684 passed, 7 skipped**.

**Verification.**
```bash
uv run python scripts/measure_tier1_routing.py   # 88.9% agenda DS coverage
uv run ruff check src/prism_mcp/figma/walker.py src/prism_mcp/figma/models.py \
  scripts/measure_tier1_routing.py tests/test_figma_tier1_routing.py
uv run pytest -q                                  # 684 passed, 7 skipped
```
(4 ruff findings in `walker.py` are pre-existing, verified on HEAD.)

**Next.** P3 Part B — extend `parsers/dts.py` (enum + union value sets),
build a per-family prop-schema index, and a Figma-variant → Prism-prop
resolver (value→enum bridge + curated residue), then emit typed props.
Data + plan in `04-phase3-routing-and-props.md` §6–§9.

---

## 2026-06-24 — P2 landed: the component catalog (DONE)

**Status: DONE.** Roadmap **P2** (the identity keystone) is complete.
Full record: `03-phase2-catalog.md`.

**What shipped.**
- Pulled `/components` + `/component_sets` for all 5 publishing libs via
  `FIGMA_TOKEN` (3,670 components + 180 sets); cached raw under
  `docs/_audit_data/catalog_raw/`.
- `figma/catalog_overrides.py`: the 38-name `PRISM_V2_COMPONENTS`
  allowlist + curated styleguide/ds slug + family tables (seeded from
  the validated `analyze_xray2.py` resolver, extended to the full
  measured inventory).
- `figma/catalog.py`: the resolution cascade (`resolve_prism_component`),
  the component→set-join builder (`build_catalog_entries` /
  `build_catalog`), and the runtime `FigmaCatalog` with the unified
  `resolve_region` (catalog hit → page-fallback cascade → none).
- `data/figma_catalog.json`: committed, versioned, **3,850 entries**
  (83% mapped at entry level), 1.78 MB minified.
- `scripts/build_figma_catalog.py` (generate) +
  `scripts/validate_catalog_coverage.py` (measure).
- Exports added to `figma/__init__.py`.

**Key realization.** The catalog is a *precomputed cache* of a
resolution cascade; when a key is absent (remote/un-ingested lib), the
runtime falls back to running the same cascade on the P1-surfaced
name/description — recovering ~16% of resolved instances deterministically.

**Measured (X-Ray, 4,534 instances).** 93.0% raw / 96.3% excl-noise /
**97.7% design-system coverage** — past the ≥95% non-viz target. Every
target validated ∈ the live rplib v2 dirs.

**Tests.** `tests/test_figma_catalog.py` (+30). Full suite: **672 passed,
7 skipped**.

**Verification.**
```bash
uv run python scripts/build_figma_catalog.py        # 3850 entries, 83% mapped
uv run python scripts/validate_catalog_coverage.py  # 97.7% design-system cov.
uv run ruff check src/prism_mcp/figma tests/test_figma_catalog.py scripts/*.py
uv run pytest -q                                     # 672 passed, 7 skipped
```

**Next.** P3 — wire `get_catalog().resolve_region(...)` into the walker
so a key hit becomes the `primary_recommendation` (skip BM25/dense),
then resolve exact props from the `.d.ts` schemas via the
`styleguide_slug` / `doc_url` the catalog now carries.

---

## 2026-06-24 — P1 landed: fetch fix + identity capture (DONE)

**Status: DONE.** Roadmap **P1** is complete. Full record:
`02-phase1-fetch-fix.md`.

**What shipped.**
- `figma/fetch.py`: `FetchedTree` + `_unwrap_response_full` +
  `_fetch_figma_tree_full` preserve `components`/`componentSets`/`styles`;
  legacy `_fetch_figma_tree`/`_unwrap_response` kept as thin `→ document`
  wrappers (zero churn for existing callers).
- `figma/models.py`: new `FigmaComponentIdentity`; optional
  `MappedRegion.figma_component`; surfaced in `to_lean_response`.
- `figma/walker.py`: `walk_tree` takes the maps → `_WalkContext`;
  `_resolve_figma_identity` (+ `_parse_doc_url`) joins `componentId →`
  global `componentKey` + logical name + styleguide URL, wired into all
  three emit paths.
- `server.py`: `map_figma_tree` uses `_fetch_figma_tree_full` and threads
  the maps into `walk_tree`; logs map sizes.
- `figma/__init__.py`: exports `FigmaComponentIdentity`.

**Tests.** New `tests/test_figma_identity.py`; extended
`test_figma_fetch.py` + `test_figma_map_tree_tool.py`. Back-compat asserted
(no maps ⇒ `figma_component is None` ⇒ no golden-fixture diffs).

**Verification.**
```bash
uv run ruff check tests/test_figma_identity.py tests/test_figma_fetch.py \
  tests/test_figma_map_tree_tool.py --output-format=concise   # passed
uv run pytest -q                                              # 642 passed, 6 skipped (~4.3s)
```
(6 skips pre-existing: network-gated fetch integration + disabled
spatial-layout tests.)

**Next.** P2 — build the `componentKey → Prism component` catalog from the 5
libraries in `figma-source-links.md`; then P3 Tier-1 routing so a key hit
becomes the `primary_recommendation` and bypasses the fuzzy ranker.

---

## 2026-06-24 — Session kickoff: docs scaffold + P1 (fetch fix)

**Context.** Starting from the two source-of-truth docs
(`docs/figma-to-prism-codegen-roadmap.md`, `docs/figma-prism-mapper-coverage.md`).
Goal for this session: stand up this `improvements/` paper trail, capture the
shared Figma links, write down the current-state analysis, and land the
roadmap's explicit *immediate next step* — the **fetch fix** (P1).

**Done so far.**
- Established a green baseline: `uv run pytest tests/test_figma_fetch.py
  tests/test_figma_walker.py tests/test_figma_models.py
  tests/test_figma_map_tree_tool.py -q` → **86 passed, 5 skipped** (~2.3s).
  (The 5 skips are network-gated integration tests.)
- Confirmed the real Figma `/nodes` response shape on saved audit data
  (`docs/_audit_data/xray_login.json`): `nodes[<id>]` carries
  `document`, `components`, `componentSets`, `styles`, `schemaVersion`.
  The `components` map is keyed by `componentId` →
  `{key, name, description, remote, documentationLinks}`. This is the map the
  fetcher currently discards.
- Created `improvements/` with `README.md`, this `worklog.md`,
  `figma-source-links.md`, `01-current-state-analysis.md`.

**Next.**
- Implement P1 in `figma/fetch.py` (backward-compatible `FetchedTree`), thread
  the maps through `server.map_figma_tree` → `walk_tree`, and capture the exact
  `componentKey` identity onto INSTANCE/COMPONENT regions. Detailed plan +
  result in `02-phase1-fetch-fix.md`.

**Verification commands used this session.**
```bash
# baseline
uv run pytest tests/test_figma_fetch.py tests/test_figma_walker.py \
  tests/test_figma_models.py tests/test_figma_map_tree_tool.py -q
```
