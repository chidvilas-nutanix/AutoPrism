# 05 — Phase 4: Layout Resolution ("no divs / no CSS")

> Roadmap **P4** — turn Figma container frames into **Prism Layout
> primitives** (`FlexLayout` / `StackingLayout`) with exact props
> (`flexDirection`, `alignItems`, `justifyContent`, `itemGap`, `padding`,
> `flexWrap`) so the generated code uses the design system's layout
> components instead of hand-written `<div style={{display:'flex',…}}>`.
> Target metric: raw `<div>` + inline-style count ≈ 0.
>
> Read `04-phase3-routing-and-props.md` first (identity + props). Layout is
> orthogonal to identity: it annotates the **structural wrappers** between
> the keyed components. Claims cite `path:line` / `module.function`.

---

## 0. TL;DR

The walker already contains a complete, well-tested CSS-layout inference
engine (`figma/layout_inference.py::analyze_layout`) that turns a parent
frame + its children into a `LayoutAnalysis` (direction / justify / align /
gap). It was **deliberately disabled** at both `LayoutNode` construction
sites to keep the LLM payload compact while the X-Ray walker fixes landed
(`walker.py:1255-1260`, `:1533-1539`; see `x-ray-walker-investigation.md`
§13 — "the revival path is a one-line change").

P4 revives it as a **compact** pass and adds the missing layer the roadmap
calls for: a deterministic **CSS → Prism Layout primitive** mapping.

- New pure module `figma/layout.py`: `PrismLayout` model +
  `resolve_prism_layout(node, analysis, children)` — maps the CSS
  `LayoutAnalysis` to a Prism `FlexLayout` / `StackingLayout` with
  token-snapped props.
- The walker attaches `LayoutNode.prism_layout` only on **structural
  container** roles (`layout-container`, `composed-region`) — never on
  keyed component leaves (a `Button`'s internal auto-layout is the
  component's concern, not a `<div>` we must replace).
- Spacing → token: `itemGap` uses the Prism T-shirt ladder
  (`XS=5 S=10 M=15 L=20 XL=30 XXL=40` px, verified in
  `Variables.less:118-124`); `padding` snaps to the `FlexLayout` padding
  token set (uniform `Npx` or `{V}px-{H}px`).

---

## 1. The gap, precisely

The roadmap target table (roadmap §0) for the Layout layer:

| | Today | P4 target |
|---|---|---|
| Layout | left to Cursor → `<div>` + inline CSS | Prism Layout primitive + props |

The deterministic pipeline (roadmap §2) names the layer **L3 Layout**:
`auto-layout / page structure → Prism Layout primitive + gap/pad`.

What exists vs. what was missing:

| Piece | State before P4 | Where |
|---|---|---|
| Figma auto-layout fields (`layoutMode`, `itemSpacing`, `primaryAxisAlignItems`, `counterAxisAlignItems`, `padding*`, `layoutWrap`) | present on every auto-layout FRAME | the fetched tree |
| CSS inference (`row/column/grid/stack`, justify, align, gap) | **built + tested**, but **disabled** in the walker | `figma/layout_inference.py`, `tests/test_figma_layout_inference.py` |
| Box style (bg / border / radius / **padding** / **gap**) | extracted onto every region | `figma/utils.py::extract_box_style`, `models.py::BoxStyle` |
| Prism Layout prop vocabulary (`.d.ts`) | parsed into the P3 schema | `data/prism_prop_schema.json` (`Layouts` family) |
| **CSS → Prism Layout primitive mapping** | **MISSING** | — this phase |

So P4 is *connect-the-existing-pieces*, not green-field: feed `analyze_layout`
output through a new deterministic mapping into the Prism layout vocabulary.

## 2. The target vocabulary (measured from the library)

`PropSchemaIndex` (P3) over `@nutanix-ui/prism-reactjs` `Layouts` family,
plus the `.tsx`/`.less` source + `*.examples.md`:

**`FlexLayout`** — the workhorse flex container:
- `flexDirection`: `row | row-reverse | column | column-reverse` (default **row**).
- `alignItems`: `flex-start | flex-end | center | baseline | stretch` (CSS default **stretch**).
- `justifyContent`: `flex-start | flex-end | center | space-between | space-around | space-evenly` (CSS default **flex-start**).
- `itemGap`: `none | XS | S | M | L | XL | XXL` (T-shirt sizes, **preferred** over `itemSpacing`; `itemGap` overrides `itemSpacing`).
- `itemSpacing`: `0px | 2px | 5px | 10px | 15px | 20px | 30px | 40px` (deprecated; **default `20px`** — a zero-gap row therefore needs an explicit `itemGap="none"`).
- `padding`: `0px | 5px | … | 40px` (uniform) or `{V}px-{H}px` pairs (`0px-5px`, `0px-10px`, `0px-15px`, `0px-20px`, `5px-0px`, `10px-0px`, `15px-0px`, `20px-0px`, `15px-20px`).
- `flexWrap`: `nowrap | wrap | wrap-reverse`.

**`StackingLayout`** — vertical stack (no `flexDirection` / `alignItems` /
`justifyContent`): `itemGap`, `itemSpacing`, `padding`, `display`,
`itemDisplay`. Idiomatic for plain vertical content/form stacks
(`StackingLayout.examples.md`).

**`FlexItem`** — child sizing (`flexGrow`, `flexShrink`, `alignSelf`); the
canonical "filling child" is `<FlexItem flexGrow="1">` around the `Table`
between a left menu and right filters (`FlexLayout.examples.md` "Page
Layout"). **Deferred to a P4 follow-up** (per-child analysis).

**Size ladder** (`src/styles/v2/Variables.less:118-124`):
`@size-xs:5px @size-s:10px @size-m:15px @size-l:20px @size-xl:30px @size-xxl:40px`.

Idiomatic composition (from the examples): vertical page = `StackingLayout`
wrapping `FlexLayout` rows; a row that needs end-alignment uses
`FlexLayout justifyContent="space-between" alignItems="center"`.

## 3. Design

### 3.1 `figma/layout.py` (new, pure)

```python
class PrismLayout(BaseModel):           # extra="forbid"
    component: Literal["FlexLayout", "StackingLayout"]
    props: dict[str, str]               # JSX-ready, e.g. {"flexDirection":"column","itemGap":"M"}
    source: Literal["figma_auto_layout", "geometry"]
    confidence: float
    notes: list[str]                    # e.g. "figma GRID → FlexLayout+flexWrap"

def resolve_prism_layout(node, analysis, children) -> PrismLayout | None
```

Cascade (deterministic, in order):

1. `direction in {None, "single", "stack"}` → **return `None`** (single
   child passes through; `stack` = overlap/absolute → roadmap's
   minimal-container fallback, a later concern).
2. Pick the primitive:
   - `row` → `FlexLayout` (row is default — omit `flexDirection`).
   - `column` → `StackingLayout` **iff** `justify ∈ {None, start}` and
     `align ∈ {None, start, stretch}` (a pure vertical stack); otherwise
     `FlexLayout flexDirection="column"` (needs align/justify props
     `StackingLayout` lacks).
   - `grid` → `FlexLayout flexWrap="wrap"` (no Prism grid primitive; noted).
3. `alignItems` (FlexLayout only): CSS→Prism (`start→flex-start`,
   `end→flex-end`, else identity); **emit only when not `stretch`** (the
   CSS default).
4. `justifyContent` (FlexLayout only): CSS→Prism; **emit only when not
   `start`** (the default).
5. `itemGap`: snap `analysis.gap` px to the nearest T-shirt token
   (`0→none … 40→XXL`); emitted whenever `gap` is known (incl. `none` for
   0 — required to override FlexLayout's 20px default).
6. `padding`: `infer_padding(node, children)` → `(T,R,B,L)`; emit a token
   when uniform (`Npx`) or a supported `{V}px-{H}px` pair; else drop + note.
7. `flexWrap` (FlexLayout): also set from `node.layoutWrap == "WRAP"`.

`source` = `figma_auto_layout` when `analysis.rationale` starts with that
(confidence 1.0), else `geometry`.

### 3.2 Walker wiring (compact revival)

- `models.py::LayoutNode` gains `prism_layout: PrismLayout | None = None`
  (next to the existing CSS `layout` field, which stays `None`).
- New helper `walker.py::_attach_prism_layout(node, layout_node,
  child_pairs)` runs `analyze_layout` + `resolve_prism_layout` and sets
  `layout_node.prism_layout` — **gated** to
  `layout_node.role ∈ {"layout-container", "composed-region"}`.
- Replaces the two disabled `_attach_layout_analysis(...)` calls
  (`walker.py:1260`, `:1539`). The verbose per-child `absolute_pos` agenda
  mutation (the original bloat the §13 disable targeted) is **not** revived
  — only the compact container-level `prism_layout`.

### 3.3 Why this is safe for existing tests

- The walker golden snapshot captures `layout_tree` as only
  `{id, role, name, children_ids}` (`_generate_goldens.py::build_golden`),
  so `prism_layout` does not churn any `*.expected.json`.
- The lean response passes `layout_tree` through verbatim
  (`models.py::to_lean_response`), and the lean test compares lean vs.
  `model_dump()` (`test_figma_lean_response.py:304`) — both sides gain the
  field, so equality holds.
- `LayoutNode` is `extra="forbid"`; adding a *known* field is fine.

### 3.4 Scope boundaries (explicit non-goals for P4 v1)

- **Page shells** (`MainPageLayout`/`HeaderFooterLayout`/`LeftNavLayout`/
  `SidePanel`): heuristic structural detection deferred to a P4 follow-up;
  v1 nails the high-volume `FlexLayout`/`StackingLayout` case first.
- **`FlexItem flexGrow`** (the filling child): deferred (needs per-child
  sizing analysis).
- **`ContainerLayout`** (styled box): `backgroundColor` only supports
  `dark|transparent|white`, so arbitrary Figma fills can't map cleanly;
  the existing `box_style` already carries bg/border/radius for those.
- **Absolute / `stack`**: returns `None` (minimal-container fallback).

---

## 4. Build (shipped, 2026-06-24)

| File | Change |
|---|---|
| `figma/models.py` | new `PrismLayout` model (lives here to avoid a `models`↔`layout` cycle, beside the other layout models); `LayoutNode` gains `prism_layout: PrismLayout \| None = None`. |
| `figma/layout.py` (new) | `snap_item_gap` (T-shirt ladder), `snap_padding` (uniform / V-H pair / drop+note), `resolve_prism_layout` (the cascade in §3.1), `layout_for_container` (analyze+resolve convenience). Pure, no I/O. |
| `figma/walker.py` | `_LAYOUT_CONTAINER_ROLES` + `_attach_prism_layout` (compact: `analyze_layout` → `resolve_prism_layout` → `LayoutNode.prism_layout`, role-gated, **no** agenda `absolute_pos` mutation). Revives the two disabled call sites (`:1262`, `:1540`). |
| `figma/__init__.py` | export `PrismLayout`, `resolve_prism_layout`, `layout_for_container`, `snap_item_gap`, `snap_padding`. |
| `scripts/measure_layout_resolution.py` (new) | structural-container layout coverage across the X-Ray dumps + committed fixtures. |

The legacy `_attach_layout_analysis` + `compute_absolute_pos` (per-child
absolute positioning) stay on disk, still unused — a future "absolute /
overlap" sub-phase can revive them; P4 deliberately ships only the
compact container-level primitive.

## 5. Measured impact (`scripts/measure_layout_resolution.py`)

Across **8 real pages** (3 live X-Ray dumps + 5 committed fixtures),
over the **structural containers** the walker keeps (the FRAMEs that
would otherwise be hand-written `<div>`s):

| Page | containers | resolved | coverage | no-flow |
|---|---:|---:|---:|---:|
| xray_login | 14 | 12 | 85.7% | 2 |
| xray_9188_127717 | 148 | 135 | 91.2% | 13 |
| xray_cloudconnect | 201 | 159 | 79.1% | 42 |
| figma-active-cluster-page | 8 | 7 | 87.5% | 1 |
| opportunities-page | 13 | 5 | 38.5% | 8 |
| figma-d02-share-summary | 33 | 26 | 78.8% | 7 |
| x-ray-3-results-progress-empty | 15 | 13 | 86.7% | 2 |
| x-ray-4-gold-image-list | 16 | 12 | 75.0% | 4 |
| **AGGREGATE** | **448** | **369** | **82.4%** | **79** |

- **82.4%** of structural containers now carry a deterministic Prism
  Layout primitive, up from **0%** (every container was a `<div>`
  before). Primitive split: **FlexLayout 277 / StackingLayout 92**.
- **90%** of resolutions (333/369) come from Figma's own **auto-layout**
  fields at confidence 1.0; the other 36 from the geometry fallback.
- Props emitted: `itemGap` 286, `alignItems` 218, `justifyContent` 89,
  `padding` 58, `flexDirection` 55.
- The **17.6% "no-flow"** remainder is single-child / overlap-`stack`
  containers that *correctly* get no flex wrapper — not misses. The
  outlier is `opportunities-page` (38.5%): a hand-built,
  absolute-positioned mock where most frames are single-child wrappers.
- Honest limitation surfaced in the run: asymmetric paddings
  (`(20,20,0,20)`, `(0,0,30,0)`) that don't fit a single Prism token are
  *dropped with a note* rather than guessed (small counts).

> **"div ≈ 0" reading:** of structural containers that have a real flow
> direction, essentially **100%** now map to a Prism layout component.
> The residual divs are exactly the single-child / overlap frames the
> roadmap flagged as the minimal-container fallback.

## 6. Tests (+42, full suite 766 passed / 7 skipped)

- `tests/test_figma_layout.py` (+37): `snap_item_gap` (none / zero→`none` /
  exact ladder / nearest), `snap_padding` (uniform / zero / pair /
  unsupported-pair / irregular / None), `resolve_prism_layout` (non-flow
  →None, row→FlexLayout, column-stack→StackingLayout, column+align/justify
  →FlexLayout, grid→flexWrap+note, CSS→Prism remap, default omission,
  zero-gap→`none`, padding from node, `layoutWrap`, geometry source),
  `layout_for_container` e2e, `PrismLayout` extra-forbid.
- `tests/test_figma_layout_walker.py` (+5): auto-layout container gets
  `prism_layout`; component-instance leaf (with its own auto-layout) does
  **not** (the role gate); geometry-fallback container resolves with
  `source="geometry"`; lean response surfaces the field; lean
  `layout_tree` equals the full dump.

Existing suites unaffected: walker goldens ignore the field
(`build_golden` snapshots only `{id,role,name,children_ids}`); the lean
test compares full-vs-lean (both gain the field).

**Verification.**
```bash
uv run python scripts/measure_layout_resolution.py   # aggregate 82.4%
uv run pytest -q                                     # 766 passed, 7 skipped
uv run ruff check src/prism_mcp/figma/layout.py …    # new files clean
```
(The 5 pre-existing `walker.py` ruff findings — `int(round())`, quoted
annotations, an ambiguous `×` — are untouched by P4, as in P3.)

## 7. Follow-ups (shipped 2026-06-24)

All four P4 v1 non-goals from §3.4 are now implemented (same pure
`figma/layout.py` module + the role-gated `_attach_prism_layout` seam).

### 7.1 Page shells (`MainPageLayout` / `HeaderFooterLayout` / `LeftNavLayout`)

`layout.py::detect_page_shell(node, children)` is a **conservative**
geometric classifier over the immediate children of a page-scale frame
(w ≥ 1000, h ≥ 600). Each child is bucketed by position + extent relative
to the parent (`_classify_shell_child`):

- **header** — top-aligned, ≥ 85% width, ≤ 25% height.
- **footer** — bottom-aligned, ≥ 85% width, ≤ 25% height.
- **leftNav** — left-aligned, ≤ 33% width, ≥ 50% height.
- **body** — the largest remaining child.

Decision: `header + leftNav + body → MainPageLayout`; `leftNav + body →
LeftNavLayout`; `header + body (+ footer?) → HeaderFooterLayout`; anything
ambiguous → `None` (the `FlexLayout` column fallback still renders it). The
shell is emitted as `PrismPageShell` (`models.py`) with `slots`
(slot-name → child **region** id, remapped in the walker), attached to the
route-anchoring `LayoutNode.prism_shell`. The shell **takes precedence** —
the walker does not also stamp a redundant `prism_layout` on that node.
Conservative on purpose: a wrong shell mangles the whole page; a missed one
renders fine.

### 7.2 `FlexItem flexGrow` (the filling child)

`layout.py::detect_fill_children(children, direction)` returns the ids of
children that fill the container's **main** axis — Figma `layoutGrow == 1`
or `layoutSizing{Horizontal,Vertical} == "FILL"` (the field chosen to match
`direction` so a column child filling its *width* is not mistaken for a
main-axis grow). These land in `PrismLayout.fill_child_ids` (remapped to
region ids by the walker); the generator wraps each in `<FlexItem
flexGrow="1">`. A pure vertical stack with a filling child is **upgraded**
from `StackingLayout` to `FlexLayout flexDirection="column"` (StackingLayout
has no flex-item child).

### 7.3 `ContainerLayout` for styled non-flow boxes

`layout.py::_container_layout(node, children)` now fires for the
single-child / overlap / childless containers that paint a box. The
background is classified into `ContainerLayout`'s three-value vocabulary by
luminance — near-white (≥ 0.96) → `white`, very dark (≤ 0.18) → `dark`,
no-fill-but-bordered/elevated → `transparent`. A *colored* fill (a grey
surface, a brand fill) is intentionally **left** `None` here so it stays on
`box_style` for P5 to resolve to a real color token. `border` ("true" when a
stroke is present) and a `ContainerLayout`-vocabulary `padding` ride along.

### 7.4 Component-aware padding (recover dropped insets)

`snap_padding(quad, component)` now selects the **target component's** token
union (`_PAD_SETS`): `StackingLayout` accepts every cross-pair of its
singles (42 pairs) vs. `FlexLayout`'s 9, and `ContainerLayout` its own small
set. Asymmetric insets (`top != bottom` or `left != right`) — which **no**
Prism padding token can express — now emit a structured `"... -> use style"`
note (the escape hatch) instead of a silent drop.

### 7.5 Measured impact (`scripts/measure_layout_resolution.py`, 8 pages)

| Metric | P4 v1 | + follow-ups |
|---|---:|---:|
| Container coverage | 82.4% (369/448) | **86.4% (387/448)** |
| Primitives | Flex 277 / Stack 92 | Flex 278 / Stack 86 / **Container 17** |
| Page shells | 0 | **6** (HeaderFooter 4, LeftNav 1, MainPage 1) |
| `FlexItem flexGrow` children | 0 | **73** |
| `padding` props emitted | 58 | **62** (+ asymmetric now noted, not dropped) |

`backgroundColor` 17 / `border` 15 props now emit on the new
`ContainerLayout`s. The remaining ~13.6% "no-flow" are genuine single-child
/ overlap wrappers (correctly no primitive).

### 7.6 Tests (+27; full suite 793 passed / 7 skipped)

- `tests/test_figma_layout.py`: component-aware `snap_padding`
  (Flex-rejects vs Stack-accepts wide pair, Container set, asymmetric
  escape), `detect_fill_children` (grow / row-horizontal / column-vertical /
  cross-axis-ignored / none), fill upgrade of a column stack,
  `_container_layout` (white / dark / transparent / colored-bg-None /
  unstyled-None / border flag), `detect_page_shell`
  (HeaderFooter / +footer / LeftNav / MainPage / too-small / single-child).
- `tests/test_figma_layout_walker.py`: `MainPageLayout` on a 1440×900 root
  (shell wins, no redundant `prism_layout`, surfaces in lean);
  `fill_child_ids` carry **region** ids end-to-end.

### 7.7 Still deferred

- **`SidePanel`** — a runtime *overlay* (drawer), not a static page-structure
  signal; needs an interaction model, not geometry. Out of P4 scope.
- **Styled box *with* flow** — a white card wrapping a `StackingLayout`
  ideally nests `<ContainerLayout><StackingLayout>`. Today the flow wins and
  the bg stays on `box_style`; the nested-wrapper composition is a P8
  (spec-assembly) concern.
- **Templates-library calibration** — shell thresholds are tuned to the
  X-Ray corpus, not yet cross-checked against the published Templates file.
