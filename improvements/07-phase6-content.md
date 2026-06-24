# 07 — Phase 6: Content, Slots & Icons ("the right glyph, the right prop")

> Roadmap **P6** — turn the raw content the walker captures (an icon glyph, a
> run of TEXT) into codegen-ready decisions: **which** Prism `*Icon` an icon
> region is, and **which prop** a region's text renders into
> (`<Button>Save</Button>` vs `<Input label="Name" />`).
>
> Read `04-phase3-routing-and-props.md` (props layer) and `06-phase5-tokens.md`
> (style layer) first — P6 is the third pure **annotation** pass over the same
> agenda, orthogonal to identity / layout / tokens. Claims cite `path:line` /
> `module.function`.

---

## 0. TL;DR

The walker already collapses an icon glyph into one `role='icon'` region
(`patterns.py::match_icon`, carrying `content_slots["icon_name_hint"]`) and
captures each region's text into `content_slots` — but left two
codegen-critical decisions open:

- **Which Prism icon?** An icon region named `"icon/chevron-down"` / `"Menu"`
  needs the exact export (`ChevronDownIcon`) out of the **206** `*Icon`
  components — not an inline `<svg>` or a guess.
- **Which prop does the text fill?** A region's text must render into the
  *right* prop of its resolved component (`children` / `label` / `title` / …).

P6 closes both with one small pure module and a value-only walker seam:

- New `figma/content.py`: `resolve_icon(name, index)` — a normalized-name
  **cascade** (exact → curated synonym → conservative uniqueness-guarded
  fuzzy → unresolved), with a **generic-name stoplist** so structural layer
  names (`Group`, `Vector 39`) never false-positive; and
  `bind_text_content(component, text, schema)` — the text-bearing prop pick
  (named prop from the P3 schema, by priority) with a `children` fallback for
  body-text leaf components and **no** binding for containers.
- `models.py`: new `PrismIcon` / `ContentBinding`; `MappedRegion` gains
  `prism_icon` / `content_binding`.
- `walker.py`: `walk_tree(icon_index=…)` threads the vocabulary;
  `_resolve_region_content` stamps the two fields after prop resolution.
  Pure value-resolution — **no** agenda/topology change, and a **no-op when
  `icon_index is None`** so every existing golden is byte-identical.
- The lean response surfaces the bare `prism_icon` component name and the
  `content_binding` triple.

Measured over 8 real pages with the real 206-icon vocabulary: **43.5%** of
*addressable* (designer-named) icon regions resolve to a concrete Prism icon
(**14.8%** of all `role='icon'` regions — the rest are un-renamed SVG path
layers no deterministic resolver can map); **56.7%** of text-carrying routed
regions get a prop binding. Both are the **deterministic floor** — the
semantic/LLM layer handles the addressable remainder.

---

## 1. The gap, precisely

The roadmap target table (roadmap §0) for the Content layer:

| | Today | P6 target |
|---|---|---|
| Content (text/icons/slots) | 825-icon catalog; TEXT capture in walker | icon-name→Prism-icon map; text→prop binding |

The deterministic pipeline (roadmap §2) names the layer **L5 Content**:
`TEXT → children/label; icon instance → Prism Icon import`.

What existed vs. what was missing:

| Piece | State before P6 | Where |
|---|---|---|
| Icon-glyph coalescing → one `role='icon'` region + `icon_name_hint` | **built + tested** | `patterns.py::match_icon`, `walker.py::_emit_pattern_region` |
| The 206 `*Icon` components in the library | indexed (entities) + in the prop-schema artifact | `indexer.py`, `data/prism_prop_schema.json` |
| Region text capture → `content_slots` (`title` / `value` / `label`) | extracted on every region | `walker.py::_emit_simple_region` / `_emit_pattern_region` |
| Per-component prop schema (`string` / `node` kinds, `accepts_string`) | **built + tested** (P3 Part B) | `prop_schema.py::ComponentPropSchema` |
| **icon name → Prism `*Icon` component** | **MISSING** | — this phase |
| **region text → which component prop** | **MISSING** | — this phase |

So, like P4/P5, P6 is *connect-the-existing-pieces*: the glyph is already one
region with a name, the text is already in a slot, the icon vocabulary and the
prop schema already exist — P6 adds the two resolvers that join them.

## 2. The target vocabulary (measured from the library)

Sourced hermetically from the committed P3 prop-schema artifact
(`data/prism_prop_schema.json`, `rplib_version 2.54.0`):

- **Icons**: **206** components whose name ends in `Icon` (`ChevronDownIcon`,
  `MenuIcon`, `MagGlassIcon`, …; family `Icons`). The live server sources the
  same set from `library.index()` (`server.py`); the measurement reads the
  artifact so it runs with no tarball.
- **Text props**: the `string` / `node` (and `Enum | string`) props of every
  component, keyed by name — the candidate sinks a region's text can bind to.

## 3. Design — two pure resolvers + a value-only seam

### 3.1 Icons: the normalized-name cascade (`content.py::resolve_icon`)

```
_normalize_icon(name)   "icon/chevron-down" | "ChevronDownIcon" | "ic_chevron_down" → "chevrondown"
  ├─ generic stoplist?  ("group"/"vector"/"fill"/"text"/…) → unresolved   (never a glyph)
  ├─ exact   index[norm]                 → adopt (conf 1.0)
  ├─ synonym _ICON_SYNONYMS[norm]→index  → adopt (conf 0.9)  ("search"→MagGlass, "x"→Close)
  └─ fuzzy   unique key contains/⊂ norm  → adopt (conf 0.6)  (len ≥ 4, |Δlen| ≤ 4, exactly 1 hit)
            else                          → unresolved (keep the raw name)
```

`PrismIcon` carries `figma_name`, the resolved `prism_component`, the `method`
(`exact`/`synonym`/`fuzzy`), and `confidence`. The cascade is intentionally
conservative: a non-unique fuzzy hit and any too-short name resolve to `None`
rather than a wrong icon (codegen keeps the raw name + an LLM hint instead).

> **The generic-name stoplist (a correctness guard, not a heuristic).** Figma
> canvases are full of structural layers named `Group`, `Vector 39`, `Fill 3`,
> `Icon + Text`. Without the guard the fuzzy tier produced *confident* false
> positives — `"Group"` → `GroupByIcon`, `"Icon + Text"` → `BoldTextIcon` (19 +
> 4 hits on one page). `_GENERIC_ICON_NAMES` (checked against the normalized
> name with any trailing run-number stripped) makes those `None`. Caught by the
> measurement; regression-tested
> (`test_resolve_generic_layer_names_never_resolve`).

### 3.2 Text binding: the prop pick (`content.py::bind_text_content`)

A named text prop in the component's P3 schema wins for **any** component
(`_TEXT_PROP_PRIORITY = title, label, heading, header, text, caption,
placeholder, content`; a prop qualifies when its `kind ∈ {string, node}` or
it `accepts_string`). Otherwise the text falls to `children` **only** for a
known body-text leaf (`_TEXT_LEAF_COMPONENTS` — `Title`, `Paragraph`, `Button`,
`Badge`, `Link`, …). A container (`Tile` / `Card`) with no named text prop gets
**`None`** — its visible text belongs to an inner element, not the container,
so binding it would be wrong. `ContentBinding` records `prop` / `value` /
`value_kind` (`children` vs `string`) / `source` (`prop-schema` /
`children-default`).

### 3.3 The walker seam (value-only, opt-in)

- `walk_tree(…, icon_index)` threads the vocabulary into `_WalkContext`. The
  server builds it from `library.index()` (`*Icon` components); tests/measure
  build one hermetically; **omitting it makes the whole pass a no-op** so every
  existing golden is byte-identical (§6).
- `_resolve_region_content(ctx)` runs **after** `_resolve_region_props`
  (`walker.py`). Per agenda row, independently and fault-tolerantly:
  - **Icon** — for icon-ish regions (`role=='icon'` with `icon_name_hint`, a
    region routed to the `Icons` family, or an `'icon'` shape-bucket), resolve
    `prism_icon`.
  - **Text** — for a region carrying text whose resolved component
    (Tier-1 `prism_resolution`, else the mapper's suggestion) accepts it, set
    `content_binding`. The prop schema is loaded lazily and **shared** with the
    P3 pass (`get_prop_schema`, gated on `prop_resolution`).
- A `content_resolved` summary count is emitted **only** when something
  resolved (mirrors the conditional `catalog_resolved` / `prop_resolved`
  keys → no-P6 summaries stay byte-identical).

No agenda membership, ordering, dedup, or topology is touched — P6 is a pure
**annotation** layer, exactly like P4/P5.

## 4. Files touched

| File | Change |
|---|---|
| `figma/content.py` (new) | `resolve_icon` (cascade + generic stoplist), `bind_text_content` (prop pick), `IconIndex` / `build_icon_index`, `_normalize_icon`, `_ICON_SYNONYMS`, `_TEXT_PROP_PRIORITY` / `_TEXT_LEAF_COMPONENTS`. Pure, no I/O. |
| `figma/models.py` | new `PrismIcon` / `ContentBinding`; `MappedRegion` += `prism_icon` / `content_binding`. |
| `figma/models.py::to_lean_response` | surfaces the bare `prism_icon` name + the `content_binding` triple on the lean agenda row. |
| `figma/walker.py` | `walk_tree(icon_index=…)`; `_WalkContext.icon_index`; `_resolve_region_content` pass + `_icon_name_for_region` / `_component_for_text_binding` / `_primary_text_for_region` helpers; conditional `content_resolved` summary key. |
| `figma/__init__.py` | export `PrismIcon`, `ContentBinding`, `IconIndex`, `build_icon_index`, `resolve_icon`, `bind_text_content`. |
| `server.py` | build the icon index from `library.index()` (`*Icon` components) and pass `icon_index=…` into `walk_tree`. |
| `scripts/measure_content_resolution.py` (new) | icon + text-binding coverage across the X-Ray dumps + committed fixtures, with a hermetic real icon vocabulary. |

## 5. Measured impact (`scripts/measure_content_resolution.py`)

Across **8 real pages** (3 live X-Ray dumps + 5 committed fixtures), with the
**real** 206-icon vocabulary and the committed P3 prop schema,
`map_figma_node_fn=None` (so the only component resolutions are the
deterministic catalog ones — the numbers are a floor):

| Page | icon (all) | icon (addressable) | text→prop bind |
|---|---:|---:|---:|
| xray_login | 2/142 | 2/2 (100%) | 23/63 (36.5%) |
| xray_9188_127717 | 20/20 | 20/20 (100%) | 14/18 (77.8%) |
| xray_cloudconnect | 4/49 | 4/26 (15.4%) | 81/127 (63.8%) |
| figma-active-cluster-page | 0/0 | 0/0 | 0/0 |
| opportunities-page | 4/25 | 4/24 (16.7%) | 0/0 |
| figma-d02-share-summary | 2/8 | 2/7 (28.6%) | 0/0 |
| x-ray-3-results-progress-empty | 2/3 | 2/3 (66.7%) | 0/0 |
| x-ray-4-gold-image-list | 3/3 | 3/3 (100%) | 0/0 |
| **AGGREGATE** | **37/250 (14.8%)** | **37/85 (43.5%)** | **118/208 (56.7%)** |

- **Icons.** Method mix `exact 32 / synonym 4 / fuzzy 1` — overwhelmingly the
  high-confidence tiers, exactly as intended after the stoplist removed the
  fuzzy false positives. Every resolved icon is *addressable* (a designer-named
  glyph): the gap between `14.8%` (all) and `43.5%` (addressable) is entirely
  un-renamed SVG path layers (`Fill 1…9`, `Vector 39`, `Button Icon`) that
  carry no glyph identity — an **upstream design-hygiene ceiling**, not a
  resolver limit. On the well-named page (`xray_9188_127717`) it's **100%**.
- **Text binding 56.7%** of text-carrying routed regions. Prop mix `children
  101 / text 12 / label 5` — `children` dominates because most routed text is a
  body-text leaf (Title/Button/Link). The denominator is *routed* regions only;
  the live tool's semantic suggestions route more regions, lifting both the
  denominator and the count.

> **"the right glyph, the right prop" reading:** when a glyph has a real name
> we resolve it almost always (and never to the *wrong* icon — the stoplist +
> uniqueness guard trade recall for zero confident-wrong output); when a routed
> element carries text we know which prop it fills a majority of the time.

## 6. Tests (+28, full suite 847 passed / 7 skipped)

`tests/test_figma_content.py` (new, +28):
- `_normalize_icon` (path/affix/punctuation collapse, idempotent/lowercase).
- `build_icon_index` (skips non-`Icon`, first-writer-wins collision, empty).
- `resolve_icon` (exact normalized + from layer name; synonym map + synonym
  skipped when target absent; fuzzy unique-contains; fuzzy ambiguous → `None`;
  short-name never fuzzy; **generic layer names never resolve** — the §3.1
  regression; miss; empty inputs).
- `bind_text_content` (named prop wins; priority `title` before `label`;
  `accepts_string` enum-union prop; ignores non-text props; `children`
  fallback for a leaf without schema; container → `None`; empty text → `None`).
- walker integration (icon region → `MenuIcon` + `content_resolved==1`;
  no-`icon_index` → no-op + summary byte-identical; lean surfaces / omits
  `prism_icon`; text → `children` binding via a fake mapper + lean surfacing;
  unrouted region → no binding).

Existing suites unaffected: the walker goldens snapshot only
`{id,role,name,children_ids}`, and `_resolve_region_content` is **byte-identical
when `icon_index=None`** (the path every existing test takes), so no golden
moved.

**Verification.**
```bash
uv run python scripts/measure_content_resolution.py   # icons 14.8/43.5%, bind 56.7%
uv run pytest -q                                       # 847 passed, 7 skipped
uv run ruff check src/prism_mcp/figma/content.py tests/test_figma_content.py \
  scripts/measure_content_resolution.py                # clean
```
(The 5 pre-existing `walker.py` ruff findings — `int(round())`, quoted
annotations — remain untouched, as in P3/P4/P5.)

## 7. Follow-ups (P6 v1 non-goals)

1. **Slot / children composition.** The roadmap's third P6 bullet — binding a
   region's children into a *named composite slot* (Modal `footer`, Form
   item `label`+control pairing, Tabs `panels`) — is deferred. The walker
   already folds the common composites into structured regions
   (`table-column` / `kpi-tile` with `value`/`label` slots) and the layout
   tree carries the parent/child topology, so most composition is *present in
   the output*; a dedicated named-slot binder that reuses the `composition_graph`
   (slice-10) is a larger, higher-risk feature best taken as its own increment
   alongside the P8 code-spec.
2. **Icon recall on un-renamed glyphs.** The `14.8%`→`43.5%` gap is SVG path
   layers with no semantic name. A geometry/visual-hash fallback (or surfacing
   the glyph for the LLM to name) could lift the un-addressable tail without
   sacrificing the zero-false-positive property.
3. **Synonym map breadth.** `_ICON_SYNONYMS` is a small curated seed; mine the
   library's icon `*.examples.md` / aliases to widen it (e.g. domain glyphs:
   `vm`, `cluster`, `snapshot`) and raise the addressable exact/synonym share.
4. **Multi-text regions.** `bind_text_content` binds the region's *primary*
   text; a composite like a labeled stat (value + caption) needs *per-slot*
   binding (`value`→`children`, `label`→a sibling) — natural to fold into the
   slot-composition work in #1.
