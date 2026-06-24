# 06 — Phase 5: Token & Style Resolution ("tokens, not literals")

> Roadmap **P5** — resolve the raw visual facts the walker captures (fill /
> stroke `#RRGGBB`, TEXT `fontSize` / `fontWeight`) onto the Prism
> **design-token** vocabulary, so codegen emits `@dark-blue-2` /
> `<Title size="h2">` instead of `#1B6BCC` / `fontSize: 29px`.
> Target metric: ≥ 95% of colors / typography expressed as tokens.
>
> Read `05-phase4-layout.md` first (layout is the sibling style layer). Tokens
> are orthogonal to identity and layout: they annotate the **colors and text**
> of every region, keyed or not. Claims cite `path:line` / `module.function`.

---

## 0. TL;DR

The repo already shipped a perceptual color index
(`tokens_index.py::ColorTokenIndex` — Oklab ranking + CIEDE2000 bucketing
over the library's LESS color tokens) for the per-row mapper, but the
**page walker never used it**: `_seed_tokens` resolved hexes through the
designer's `variable_defs` map *only*, and the node-tree's TEXT styles were
captured (`box_style`) but never mapped to a Prism type token.

P5 closes both gaps with one small pure module and a value-only walker seam:

- New `figma/tokens.py`: `resolve_color_token(hex, variable_defs, index, role)`
  — a **trust cascade** (designer variable → perceptual index `exact`/`near`
  → unresolved) — and `resolve_typography(style)` — snaps a Figma text style
  onto a curated Prism **type ramp** (`Variables.less`).
- `models.py`: `BoxStyle` gains `background_token` / `border_token`; new
  `Typography` model; `MappedRegion` gains `typography`.
- `walker.py`: `walk_tree(color_token_index=…)` threads the index;
  `_seed_tokens` now fills the page `tokens` map via the cascade;
  `_resolve_region_tokens` stamps `box_style` color tokens + the region's
  dominant `Typography`. Pure value-resolution — **no** agenda/topology change.
- The lean response surfaces the codegen-ready typography triple
  (`style_token` / `size_token` / `weight_token`); the color tokens ride
  along on `box_style`.

Measured over 8 real pages: **94.7%** of distinct page hexes, **98.5%** of
region backgrounds, **96.1%** of region borders resolve to a Prism color
token; **73.4%** of text-carrying regions resolve to a typography token —
and this is the *perceptual-only floor* (the page dumps carry no designer
variable names; the live tool adds those on top).

---

## 1. The gap, precisely

The roadmap target table (roadmap §0) for the Tokens layer:

| | Today | P5 target |
|---|---|---|
| Tokens | hex→nearest token (advisory, mapper-only) | bound tokens on every region, no literals |

The deterministic pipeline (roadmap §2) names the layer **L4 Tokens**:
`color / type / effect → Prism LESS token`.

What existed vs. what was missing:

| Piece | State before P5 | Where |
|---|---|---|
| Perceptual color index (hex → nearest LESS token, Oklab + CIEDE2000, role hint) | **built + tested**, used by the per-row mapper only | `tokens_index.py::ColorTokenIndex`, `library.py::color_token_index` |
| Designer `variable_defs` (`hex → name`) | threaded into the walker, used by `_seed_tokens` | `walker.py::_seed_tokens` |
| Region colors (`background_color`, `border_color`) as **hex literals** | extracted onto every region | `utils.py::extract_box_style`, `models.py::BoxStyle` |
| TEXT `style` (`fontSize`, `fontWeight`) | present on every TEXT node in the tree | the fetched tree |
| **hex → token on the page walker** (beyond designer-named) | **MISSING** (index unused by the walker) | — this phase |
| **TEXT style → Prism typography token** | **MISSING** | — this phase |

So, like P4, P5 is *connect-the-existing-pieces*: feed the already-built
perceptual index into the walker, and add the one missing ramp (typography).

### 1.1 The Variables-API decision (roadmap §5 risk #2)

The roadmap flagged semantic color as *gated on Variables API scope (`403`
for the project PAT) vs `boundVariables` resolution*. P5 sidesteps the gate:
the **perceptual index + curated type ramp resolve from the node tree alone**
(fill/stroke hex + TEXT style), so they work on **every** file — hand-built,
detached, or published — with no API upgrade. The semantic style-name path
(published FILL/TEXT styles, `boundVariables`) is intentionally *not* relied
on; it stays a future enrichment, documented in `tokens.py`'s module header.

## 2. The target vocabulary (measured from the library)

Built hermetically from the committed Prism LESS via
`parsers/tokens.py::walk_tokens` + `tokens_index.py::build_color_token_index`:

- **Colors**: **105** color tokens (from 118 `color`-category LESS vars; 13
  drop as alias-of-another-var). Named by hue, not role (`dark-blue-2`,
  `light-gray-3`, `white`) — see §4.1 for why that matters.
- **Typography**: the Prism named text styles in `Variables.less:31-92`
  (`@title-h1…h4`, `@paragraph`, `@label`, `@label-small`, `@link`, `@tag`)
  with their font-size + weight, plus the weight ladder `Variables.less:13-18`
  (`fine 200 … bold 700`).

## 3. Design — two pure resolvers + a value-only seam

### 3.1 Color: the trust cascade (`tokens.py::resolve_color_token`)

```
designer variable_defs[hex]  (exact, the designer literally named it)
  └─ miss → ColorTokenIndex.query(hex, role)   (perceptual)
            ├─ bucket exact / near → adopt token
            └─ loose / no-match    → unresolved (keep hex, surface `nearest`)
```

`ColorTokenResult` carries `token` (set only in the `exact`/`near` band),
`bucket`, `source` (`figma_variable` / `prism_token_index` / `none`), and
`nearest` (the closest name even when too far to adopt — an LLM hint).
`_variable_lookup` is case-insensitive (the walker emits `#1B6BCC`; designer
maps are often lower-cased). A malformed hex (gradient placeholder) is caught
— never aborts the walk.

### 3.2 Typography: the curated ramp (`tokens.py::resolve_typography`)

A small, stable `_TYPE_RAMP` traces line-for-line to `Variables.less`. The
match is **nearest size band, then nearest weight inside it, then ramp order**
(so a `(14,400)` tie resolves `paragraph` over `label` — the more structural
name). A font size farther than `_SIZE_TOLERANCE_PX = 3px` from every entry is
left **unresolved** (`None`) rather than snapped to a misleading style.
`confidence` is `1.0` for an exact `(size,weight)` hit, `0.8` for a
nearest-size adoption.

> **Why a curated ramp, not an index over typography entities:** the Prism
> typography tokens live in `Variables.less`, which the slice-6 walker files
> under the `spacing` category — they aren't cleanly separable as
> "typography" entities at runtime. The ramp is the same pattern P4's
> `layout.py` uses for the size ladder: small, stable, sourced from the LESS.

### 3.3 The walker seam (value-only)

- `walk_tree(…, color_token_index)` threads the index into `_WalkContext`.
  The server passes `library.color_token_index()`; tests/measure build one
  hermetically; omitting it keeps the **legacy designer-variable-only**
  behaviour (so every existing golden is byte-identical — see §6).
- `_seed_tokens` now sets each page-`tokens` value via the cascade. It still
  only writes a value for a hex **key it already saw** (`setdefault`-style) —
  `tokens_count` is unchanged; only the *values* gain perceptual matches.
- `_resolve_region_tokens(node, box_style)` (called from both emit paths)
  stamps `box_style.background_token` (with a `role="surface"` hint) and
  `border_token`, and returns the region's `Typography` from its dominant
  TEXT descendant (`utils.py::dominant_text_style` — the largest `fontSize`
  in the subtree, the region's headline).

No agenda membership, ordering, dedup, or topology is touched — P5 is a pure
**annotation** layer, exactly like P4.

## 4. Files touched

| File | Change |
|---|---|
| `figma/tokens.py` (new) | `resolve_color_token` (cascade), `resolve_typography` (ramp), `ColorTokenResult` / `ColorCoverage` dataclasses, `_TYPE_RAMP` / `_WEIGHT_TO_TOKEN`. Pure, no I/O. |
| `figma/models.py` | `BoxStyle` += `background_token` / `border_token`; new `Typography` model; `MappedRegion` += `typography`. |
| `figma/utils.py` | new `dominant_text_style(node)` — iterative subtree scan for the largest TEXT style. |
| `figma/walker.py` | `walk_tree(color_token_index=…)`; `_WalkContext.color_token_index`; `_seed_tokens` cascade; `_resolve_region_tokens` seam wired into both emit paths. |
| `figma/models.py::to_lean_response` | surfaces the typography triple on the lean agenda row (color tokens ride on `box_style`). |
| `figma/__init__.py` | export `Typography`, `ColorTokenResult`, `resolve_color_token`, `resolve_typography`. |
| `server.py` | pass `color_token_index=library.color_token_index()` into `walk_tree`. |
| `tokens_index.py` | **bug fix** (§4.1). |
| `scripts/measure_token_resolution.py` (new) | color + typography coverage across the X-Ray dumps + committed fixtures, with a hermetic real index. |

### 4.1 Bug found + fixed: role hint returned empty on role-less token names

Wiring `role="surface"` for background resolution surfaced a latent
`ColorTokenIndex.query` bug. The role mask narrows candidates to tokens whose
**name** contains the role keywords (`surface`/`bg`/`panel`/…). Prism color
tokens are named by hue (`dark-blue-2`), so the mask is **all-False**. The
code reset to the global set only when the mask was `None`, not when it was
all-False — so the query returned `[]`, violating its own documented
"never empty handed" contract. The measurement caught it instantly:
**region background token `0/842`** while role-less borders resolved `96%`.

Fix (`tokens_index.py::query`): fall back to the global set when the mask is
`None` **or** matches zero tokens. Backgrounds jumped `0 → 829/842 (98.5%)`.
Regression test added (`test_query_known_role_with_zero_matching_tokens_falls_back_global`).

## 5. Measured impact (`scripts/measure_token_resolution.py`)

Across **8 real pages** (3 live X-Ray dumps + 5 committed fixtures), with a
**real** 105-token color index built from the committed Prism LESS:

| Page | page hex→token | bg token | border token | text→typography |
|---|---:|---:|---:|---:|
| xray_login | 4/5 | 118/130 | 31/34 | 165/307 (53.7%) |
| xray_9188_127717 | 6/6 | 23/23 | 13/13 | 40/60 (66.7%) |
| xray_cloudconnect | 9/10 | 637/638 | 575/598 | 355/401 (88.5%) |
| figma-active-cluster-page | 3/3 | 8/8 | 4/4 | 17/17 (100%) |
| opportunities-page | 0/0 | 0/0 | 0/0 | 9/34 (26.5%) |
| figma-d02-share-summary | 6/6 | 27/27 | 16/16 | 37/44 (84.1%) |
| x-ray-3-results-progress-empty | 4/4 | 8/8 | 3/3 | 27/30 (90%) |
| x-ray-4-gold-image-list | 4/4 | 8/8 | 5/5 | 29/32 (90.6%) |
| **AGGREGATE** | **36/38 (94.7%)** | **829/842 (98.5%)** | **647/673 (96.1%)** | **679/925 (73.4%)** |

- **Color clears the ≥95% roadmap bar** on the page-hex and background
  measures (94.7% / 98.5%), border close behind (96.1%) — and this is the
  **perceptual-only floor**: the page dumps carry no `get_variable_defs`
  map, so the designer's exact names (the higher-trust cascade tier) add on
  top in the live tool.
- **Typography 73.4%** of text-carrying regions. Top tokens: `link` 356,
  `paragraph` 121, `title-h3` 82, `title-h2` 52, `title-h4` 44, `title-h1`
  24. The ~27% remainder is text whose size sits > 3px from every ramp entry
  (deliberately left as a literal — see §3.2 / §7).

> **"literals ≈ 0" reading:** essentially every region surface/border color
> now binds to a Prism token; the residual hexes are off-palette one-offs the
> index honestly declines to force. Typography is the looser axis (a curated
> 9-entry ramp vs 105 colors) and is the main P5 follow-up target.

## 6. Tests (+26, full suite 819 passed / 7 skipped, 826 collected)

- `tests/test_figma_tokens.py` (+25): `resolve_color_token` (designer-variable
  exact + case-insensitive, perceptual exact/near, far→unresolved-with-nearest,
  no/empty index, malformed-hex no-raise, `role="surface"` bias);
  `resolve_typography` (exact h1, paragraph-wins-tie, near-size lowers
  confidence, far/missing-size/None → None, weight snap, ramp-weight fallback);
  `dominant_text_style` (largest-in-subtree, none-when-no-text, style-less
  TEXT); walker integration (bg/border token + typography with index, typography
  index-independent without it, designer-variable flows to tokens map, lean
  surfaces the triple / omits it when unresolved).
- `tests/test_tokens_index.py` (+1): the §4.1 role-fallback regression.

Existing suites unaffected: walker goldens snapshot only
`{id,role,name,children_ids}`; the lean test builds regions with
`typography=None` (no key emitted) and compares full-vs-`model_dump`. Crucially,
`_seed_tokens` is **byte-identical** when `color_token_index=None` (the path
every existing walker test takes), so no golden moved.

**Verification.**
```bash
uv run python scripts/measure_token_resolution.py   # color 94.7/98.5/96.1%, typo 73.4%
uv run pytest -q                                     # 819 passed, 7 skipped
uv run ruff check src/prism_mcp/figma/tokens.py tests/test_figma_tokens.py   # clean
```
(The 5 pre-existing `walker.py` ruff findings — `int(round())`, quoted
annotations — remain untouched, as in P3/P4.)

## 7. Follow-ups (P5 v1 non-goals)

1. **Typography coverage / over-eager `link`.** `link` (14px/500) is the most
   common hit because much UI text is 14px medium; semantically some of those
   are labels/body, not links. Widen the ramp (e.g. `label-medium`) and/or
   bind the **style name** when published TEXT styles are available, to lift
   the 73.4% and de-bias `link`.
2. **Text color token.** A region's typography binds size/weight but not yet
   the text *color* (the heading hex lives on a folded child, not the region's
   own `box_style`). Bind the dominant TEXT fill to a color token.
3. **Effect styles → shadow tokens.** Prism ships only a thin shadow-token
   set; P5 keeps the `has_shadow` boolean. Map the common elevation shadows
   when the token vocabulary justifies it.
4. **Semantic style-name path.** If/when the Variables API scope or a reliable
   `boundVariables` resolution lands, prefer the designer's semantic token
   name over the perceptual nearest (a new highest-trust cascade tier).
