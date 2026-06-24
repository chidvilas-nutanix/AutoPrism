# 03 — Phase 2: the component catalog (Identity layer)

> Status: **DONE** (2026-06-24). Roadmap **P2** — "everything keys off
> identity." This is the durable record: what we built, the data behind
> it, how it resolves, the measured coverage, and the P3 hand-off. Read
> `02-phase1-fetch-fix.md` first (P1 made the `componentKey` *available*;
> P2 turns it into a Prism component). Claims cite `path:line` /
> `module.function`.

## 0. TL;DR

P2 ships a deterministic **`componentKey → Prism component` catalog**
spanning all five publishing libraries, plus the resolver P3 will route
on. Built offline from the Figma `/components` + `/component_sets`
dumps, committed as `src/prism_mcp/figma/data/figma_catalog.json`
(3,850 entries), served at runtime by :class:`FigmaCatalog` with **zero
network / zero rplib** dependency.

Measured on three real X-Ray pages (4,534 instances): **93.0% raw /
96.3% excluding noise / 97.7% design-system coverage** — past the
roadmap's ≥95% non-viz target. Every target is validated to be one of
the 38 real prism-react v2 component families.

Nothing is wired into the walker yet — that is **P3** (promote a key hit
to `primary_recommendation`, bypass the fuzzy ranker). P2 delivers the
engine + the data + the metric.

## 1. The core realization (why catalog *and* cascade)

The validated audit numbers (roadmap §1.6: 93% map / 82% exact) came
from running a **resolution cascade** on the *page-provided*
`components` / `componentSets` maps — not from a precomputed catalog.
So P2 has two halves that share one cascade:

- **The cascade** (`catalog.resolve_prism_component`, `catalog.py:154`)
  is the engine: description styleguide-URL slug → Prism, else
  ds-slug → Prism, else name-family → Prism.
- **The catalog** (`data/figma_catalog.json`) is a *precomputed cache*
  of the cascade applied to every published library component, keyed by
  global `componentKey` for O(1) runtime lookup.

The payoff of keeping both: when an instance's key is **not** in the
catalog (a remote component published by a library we did not ingest),
:meth:`FigmaCatalog.resolve_region` falls back to running the cascade on
the page-provided name+description (surfaced by P1). Those remote
components carry the *same* styleguide URL, so the answer is identical
and deterministic. On `xray_cloudconnect` this fallback recovers **650**
instances (≈18% of all resolved) that a catalog-only lookup would miss.

## 2. Source data (measured, all five libraries)

Fetched live via `FIGMA_TOKEN` → `/v1/files/:key/{components,
component_sets}`, cached under `docs/_audit_data/catalog_raw/` (10 raw
dumps, reproducible offline). Totals match the roadmap §1.2 inventory:

| Library (key) | components | sets | styleguide URLs |
|---|---|---|---|
| Design Library `bK52…` | 2,780 | 114 | 1,997 (own-or-set) |
| Templates `Z0OT…` | 107 | 18 | 0 |
| Design Library for Visualizations `XNpH…` | 325 | 19 | 0 |
| Spec Doc `KVbK…` | 444 | 25 | 0 |
| Color Primitives `5de1…` | 14 | 4 | 0 |

Two structural facts the builder relies on (verified on the dumps):

1. **Variant components carry no logical identity of their own.** A
   variant's `name` is the variant string (`"Darkmode=True"`,
   `"Type=Primary"`); the logical name + styleguide URL live on its
   **component set**. The link is
   `containing_frame.containingStateGroup.nodeId → set.node_id`
   (2,522 / 2,780 components linked in bK52). The builder joins on this
   so variants resolve through their set.
2. **Styleguide slugs live under multiple sections**, not just
   `#/Components/…` — e.g. `#/Layouts/Structure?id=scrollbar`. The slug
   regex captures the `?id=` slug under *any* section
   (`catalog.py:_STYLEGUIDE_RE`).

## 3. What was built, file by file

### 3.1 `figma/catalog_overrides.py` — the curation surface

- :data:`PRISM_V2_COMPONENTS` — the **38** canonical `src/components/
  v2/*` family names. Asserted to equal the live rplib dirs by
  `tests/test_figma_catalog.py::test_allowlist_matches_live_rplib_v2_dirs`
  (the P2 CI guard; skips offline). 29 are also same-named entities; 9
  are family modules (`Icons`, `Tables`, `Typography`, `Navigation`,
  `Layouts`, `List`, `Popover`, `Tutorial`, `Utility`) — the correct P2
  granularity (P3 picks the specific entity + props).
- Four tables, descending trust: `KEY_OVERRIDES` (empty by design),
  `STYLEGUIDE_SLUG_TO_PRISM` (29 slugs), `DS_SLUG_TO_PRISM` (16),
  `FAMILY_NAME_TO_PRISM` (URL-less libs; `None` = known no-equivalent).
  Seeded from the validated `docs/_audit_data/analyze_xray2.py` resolver
  and extended to the *complete* measured slug/family inventory.

### 3.2 `figma/catalog.py` — engine + data structures + loader

- **Cascade** `resolve_prism_component(name, desc, *, component_key)`
  (`catalog.py:154`) → :class:`ResolvedTarget` (prism, method, slug,
  doc_url, confidence). Seven tiers: `key-override` (1.0) ·
  `styleguide-id` (1.0) · `ds-slug` (0.95) · `icon-family` (0.9) ·
  `family-name` (0.7) · `family-unsupported` (0.0) · `unmapped` (0.0).
- **Normalization** `normalize_family` (`catalog.py:108`) strips emoji
  status markers (✅/⏳/🛑), trailing `(slot)`/`(detach asset)`, and `_`
  scaffolding before the family lookup.
- **Builder** `build_catalog_entries(libraries)` (`catalog.py:342`) does
  the component→set join and emits one :class:`CatalogEntry` per
  component **and** per set; `build_catalog` (`catalog.py:469`) wraps it
  with `assert_targets_valid` (fails the build on a non-canonical
  target) + stats, and serializes the artifact.
- **Runtime** :class:`FigmaCatalog` — `load()` (committed JSON, schema-
  checked), `lookup(key)`, `resolve(key)`, and the unified
  **`resolve_region(component_key, figma_name, description)`** →
  :class:`RegionResolution` (catalog-hit → else page-fallback cascade →
  else none). `get_catalog()` is the process singleton.

### 3.3 Generation + validation scripts

- `scripts/build_figma_catalog.py` — fetch (cache-first) → build →
  write `data/figma_catalog.json` + coverage report. `--refetch` forces
  a live pull. **Only place the catalog is generated.**
- `scripts/validate_catalog_coverage.py` — replays saved X-Ray
  `/nodes` page dumps through `resolve_region` and reports the P2
  metric (raw / excl-noise / design-system coverage, by source/method).

### 3.4 Exports

`figma/__init__.py` now exports `FigmaCatalog`, `CatalogEntry`,
`RegionResolution`, `resolve_prism_component`, `get_catalog`.

## 4. The committed artifact

`src/prism_mcp/figma/data/figma_catalog.json` — **3,850 entries**
(3,670 components + 180 sets), minified, 1.78 MB, deterministic
(entries pre-sorted). Entry-level resolution **83.0% mapped**:

| method | count |
|---|---|
| styleguide-id | 2,027 |
| family-name | 879 |
| icon-family | 281 |
| family-unsupported | 20 |
| ds-slug | 10 |
| unmapped | 633 |

(Entry-level % is low because thousands of rarely-used / viz / spec
components dilute it; *instance* coverage — weighted by real usage — is
the metric that matters, §5.) Top targets: Button 690, Input 616,
Select 307, Icons 281, Badge 206, Navigation 179, Tables 158.

> **Tracked vs local.** Only `figma_catalog.json` (under
> `src/.../figma/data/`) is committed — it ships with the package and is
> what CI + the unit tests load. The raw dumps (`catalog_raw/`) and the
> X-Ray page dumps the validator replays live under the **gitignored**
> `docs/_audit_data/`, so they are local build inputs. A fresh clone
> rebuilds with `uv run python scripts/build_figma_catalog.py --refetch`
> (needs `FIGMA_TOKEN`); coverage validation needs the local page dumps
> (or any saved `/nodes` response) and is therefore an offline check,
> not a CI gate.

## 5. Measured coverage (the P2 metric)

`uv run python scripts/validate_catalog_coverage.py` over three X-Ray
pages, 4,534 visible instances:

| page | instances | raw | excl-noise | design-system | catalog / fallback |
|---|---|---|---|---|---|
| Login (re-branded) | 312 | 69.9% | 89.7% | 91.2% | 190 / 28 |
| 9188 (table page) | 322 | 99.4% | 99.7% | **100.0%** | 306 / 14 |
| Cloud Connect | 3,900 | 94.3% | 96.4% | 97.9% | 3,028 / 650 |
| **AGGREGATE** | **4,534** | **93.0%** | **96.3%** | **97.7%** | 3,524 / 692 |

> "design-system coverage" = resolved ÷ (instances − noise − local),
> i.e. excluding `_`/a11y/annotation scaffolding and locally-built /
> detached frames. **97.7% ≥ the ≥95% non-viz target.** The remaining
> misses are genuine local composites (`Comment card`, `Empty States/
> Upgrade`, `HTML sections`) with no single prism-react equivalent.

## 6. Validation & tests

- `tests/test_figma_catalog.py` (30 tests): the full cascade (every
  tier + precedence), `normalize_family`, the builder's variant→set
  join, `assert_targets_valid`, loader round-trip + schema guard,
  `resolve_region` (catalog / page-fallback / none), and three
  **committed-artifact regression guards** (loads, all targets
  canonical, mapped-share ≥ 80%), plus the live-rplib allowlist guard.
- Full suite green: **672 passed, 7 skipped** (skips pre-existing:
  network-gated fetch + disabled spatial-layout tests).
- Pre-existing ruff debt in untouched walker/patterns/utils/
  layout_inference (ambiguous `×`, UP037, RUF046) left as-is — not P2.

## 7. Known limits / follow-ups

- **Login outlier (91.2%).** The re-branded login leans on local
  illustration + `Component 1` default-named frames; its design-system
  content resolves, the rest is genuinely local art.
- **Catalog does not (yet) span every referenced library.** ~692 of
  4,216 resolved instances come from the page-fallback cascade because
  their publishing file is not one of the five we ingest (e.g. an older
  `Action/Link` library). They resolve correctly via the shared
  cascade; ingesting those files would just move them from
  `page-fallback` to `catalog` source. Track new high-frequency misses
  via the validator's `top_unmapped`.
- **Visualizations (cross-cutting risk #1).** Viz components are in the
  catalog but most resolve `unmapped`/`family-unsupported` — prism-react
  has no charting equivalent. Treat as a known-unsupported region (do
  not fake it); quantify before promising codegen there.

## 8. P3 hand-off

P3 (Prop Resolution / Tier-1 routing) consumes this directly:

1. In the walker's `_resolve_pending_mappings` (or a new resolution
   pass), call `get_catalog().resolve_region(component_key=…,
   figma_name=…, description=…)` with the region's P1
   `figma_component` identity.
2. On `is_mapped`, set `FigmaNodeMapping.primary_recommendation =
   res.prism_component` with `confidence = res.confidence` and a
   rationale citing `res.method`/`res.source`, and **skip BM25/dense**
   for that region (demote the fuzzy ranker to Tier-3, per roadmap §2).
3. Use `res.styleguide_slug` + `res.doc_url` as the entry point for the
   `.d.ts` prop-schema resolution (P3 proper).

Detailed change record ends here; see `worklog.md` for the dated entry.
