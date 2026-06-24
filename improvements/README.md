# Improvements — Figma → Prism deterministic codegen

> Working folder for the build-out of the deterministic Figma → Prism-React
> code-spec engine described in `docs/figma-to-prism-codegen-roadmap.md` and
> `docs/figma-prism-mapper-coverage.md`.
>
> **Purpose of this folder:** a durable, append-only paper trail so any agent
> (or human) can pick up the work with *accurate* context — what we changed,
> why, what we learned, and what is next. Every research finding, decision,
> and code change gets written down here as it happens.

---

## How to use this folder (read me first)

1. **Start with `worklog.md`.** It is the chronological spine — newest entry on
   top. Each entry links to the detailed doc(s) for that unit of work.
2. **Read the numbered docs in order.** `NN-topic.md` files are the durable
   record for one phase / investigation. They are written so a cold reader can
   reconstruct the reasoning without re-deriving it.
3. **`figma-source-links.md`** is the catalog of every Figma file/page URL the
   team has shared, annotated with what each is for and which roadmap phase
   consumes it.
4. **Before you change code,** skim `01-current-state-analysis.md` so you do not
   re-discover the pipeline from scratch.

## Conventions (keep the trail accurate)

- **Append, don't rewrite.** Correct a prior finding with a new dated note that
  references the old one, rather than silently editing history.
- **Every code change** gets: (a) a `worklog.md` entry, (b) a numbered doc (or a
  section in one) describing the change, the rationale, the tests, and the
  verification command + result.
- **Cite code precisely** — `path:line` or `module.function`, not vibes.
- **Tie work to the roadmap.** Reference the phase (P1–P9 from the roadmap, or
  the 1–6 plan in the coverage doc) so progress maps onto the plan.
- **Status markers:** `DONE` / `IN PROGRESS` / `BLOCKED` / `NEXT`.

## Source-of-truth docs (in `docs/`, not here)

| Doc | What it is |
|---|---|
| `docs/figma-to-prism-codegen-roadmap.md` | The vision + 9-phase roadmap (P0–P9). The north star. |
| `docs/figma-prism-mapper-coverage.md` | Phase-0 coverage audit + real-page validation; the 1–6 phased plan. |
| `docs/figma-page-to-prism-plan.md` | Original walker design spec (routing table, pattern catalogue). |
| `docs/_audit_data/` | Raw exports + re-runnable analysis scripts that justify the numbers. |

## This folder's contents

| File | Status | What it covers |
|---|---|---|
| `README.md` | — | This index + conventions. |
| `worklog.md` | live | Chronological log of every work unit. |
| `figma-source-links.md` | live | All shared Figma URLs, annotated by role + phase. |
| `01-current-state-analysis.md` | DONE | Code-verified map of today's pipeline and the exact gap. |
| `02-phase1-fetch-fix.md` | DONE | P1: preserve + thread the `components`/`componentSets` maps + capture `componentKey` identity. |
| `03-phase2-catalog.md` | DONE | P2: the `componentKey → Prism` catalog across all 5 libs (97.7% design-system coverage); cascade + runtime resolver. |
| `04-phase3-routing-and-props.md` | DONE (A + B) | P3 Part A: Tier-1 routing wires the catalog into the walker (88.9% agenda coverage). Part B: prop resolution — `componentProperties` → typed props (`type={ButtonTypes.PRIMARY}`); **75%** configurable-component coverage, plus the design-axis-vs-prop finding. |
| `05-phase4-layout.md` | DONE | P4: layout resolution ("no divs"). Container frames → Prism `FlexLayout`/`StackingLayout` + token-snapped props (`itemGap`, `padding`, `alignItems`, `justifyContent`). **82.4%** structural-container coverage across 8 pages (90% from Figma auto-layout); revives the disabled CSS inference compactly. |

## The one-paragraph "why" (so the trail never loses the thread)

Nutanix product designs are assembled almost entirely from a **keyed**,
documented Figma component library. Every `INSTANCE` in a design references its
source component by `componentId`, which the Figma REST `/nodes` response
resolves to a **global `componentKey`** via a sibling `components` map. That key
is the deterministic join into a Prism component. **Today the server throws that
map away** (`figma/fetch.py::_unwrap_response` returns only `document`), forcing
the walker into fuzzy BM25/dense matching. Preserving the map (P1) is the
keystone every later phase depends on; building the `componentKey → Prism`
**catalog** (P2) and **Tier-1 routing (P3-A, done)** is what turns the mapper
from a "suggestion engine" into a deterministic "code-spec engine." **Prop
resolution (P3-B, done)** layers exact typed props
(`type={ButtonTypes.PRIMARY}`, `disabled`) onto each routed component by
matching Figma variant *values* to enum/union values — reaching **75%**
coverage on configurable leaf components (Button/Badge/Input/Checkbox) at
~100% precision. A key learning landed here: many Figma variant axes are
design-system visual descriptors with **no** Prism prop (declarative
components like Tables/Select are built from `dataSource`/`columns`), so the
resolver deliberately declines them rather than emitting wrong props.
**Layout (P4, done)** is the orthogonal "no divs" layer: the structural
FRAMEs *between* the keyed components — the ones Cursor would otherwise emit
as `<div style={{display:'flex',…}}>` — now resolve to a Prism `FlexLayout` /
`StackingLayout` with token-snapped props, reusing the walker's already-built
(but previously disabled) CSS inference. **82.4%** of structural containers
across 8 real pages now carry a deterministic layout primitive, 90% straight
from Figma's own auto-layout fields.
