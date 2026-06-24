# Figma source links (annotated)

> Every Figma URL the team has shared, grouped by file, annotated with its role
> in the design system and which roadmap phase consumes it. Captured 2026-06-24.
>
> **Note on node-ids:** Figma *URLs* use a hyphen between id parts
> (`node-id=21-1`); the REST API (`/v1/files/:key/nodes?ids=…`) and our internal
> JSON use a colon (`21:1`). `parse_figma_url` normalises hyphen → colon for us.
>
> These files are **catalog-build-time + token-resolution sources** (P2/P5/P6),
> *not* day-to-day page-mapping inputs. A product engineer pasting a screen to
> generate code points at a *product* file (e.g. an X-Ray page); these library
> files are what the deterministic catalog is *built from*.

## Team

| URL | Meaning |
|---|---|
| https://www.figma.com/files/910306468284021472/team/910583783880545700?fuid=1598325907788035106 | Team `910306468284021472` landing — the org that owns all 3 projects (Core UI, Specs+Tokens, Visualizations). |

## Project: Core UI

### Design Library — `bK52NYtDhya7uiW7dvObaQ`
**Role:** the primary catalog. 2,780 components · 114 component-sets · 825 icons ·
1,012 styleguide URLs in descriptions. **The only library that carries
`prism-styleguide` URLs** — i.e. a ready-made `componentKey → Prism` dictionary.
**Phase:** P2 (catalog identity) — the keystone source.

| URL | node (URL → REST) | Notes |
|---|---|---|
| https://www.figma.com/design/bK52NYtDhya7uiW7dvObaQ/Design-Library?node-id=21-1&m=dev | `21-1` → `21:1` | Library entry/cover area. |
| https://www.figma.com/design/bK52NYtDhya7uiW7dvObaQ/Design-Library?node-id=21-0&m=dev | `21-0` → `21:0` | Adjacent canvas/page root. |
| https://www.figma.com/design/bK52NYtDhya7uiW7dvObaQ/Design-Library?node-id=29773-52785&m=dev | `29773-52785` → `29773:52785` | Alert/Notification doc page (referenced in the coverage doc §3.4 — instances `Alert Banners/ ✅ Standard Alert Banner`, `Notification ⏳`, `Mini Alert Banner`, `Toast`, `Global Banner`). Good fixture for emoji/space name normalization. |
| https://www.figma.com/design/bK52NYtDhya7uiW7dvObaQ/Design-Library?node-id=1003-2090&m=dev | `1003-2090` → `1003:2090` | Component documentation region. |

### Templates — `Z0OTY6BFR7oTCpRjCzs7lz`
**Role:** page compositions (Onboarding, Entity Browser, VMs List). 107
components · 18 sets · **no styleguide URLs** → map by normalized name + curation.
**Phase:** P4 (layout) — canonical page-shell compositions to mine for
`MainPageLayout`/`HeaderFooterLayout`/`LeftNavLayout` inference.

| URL | node (URL → REST) | Notes |
|---|---|---|
| https://www.figma.com/design/Z0OTY6BFR7oTCpRjCzs7lz/Templates?node-id=2-1404&m=dev | `2-1404` → `2:1404` | A page template composition. |

## Project: Specs + Tokens

### Spec Doc — `KVbKkR0QAFRbTVpwysQLWw`
**Role:** specs + color styles. 444 components · 25 sets · 154 styles
(148 FILL). **Phase:** P5 (token & style resolution).

| URL | node (URL → REST) | Notes |
|---|---|---|
| https://www.figma.com/design/KVbKkR0QAFRbTVpwysQLWw/Spec-Doc?node-id=51954-54695&m=dev | `51954-54695` → `51954:54695` | Spec region. |
| https://www.figma.com/design/KVbKkR0QAFRbTVpwysQLWw/Spec-Doc?node-id=21620-174159&m=dev | `21620-174159` → `21620:174159` | Spec region. |

### Color Primitives — `5de1bNROZOtWBx8c4ArKwr`
**Role:** raw color palette. 14 components · 4 sets · 68 FILL styles.
**Phase:** P5 — the base palette that semantic color variables resolve onto.

| URL | node (URL → REST) | Notes |
|---|---|---|
| https://www.figma.com/design/5de1bNROZOtWBx8c4ArKwr/Color-Primitives?node-id=1003-2089&m=dev | `1003-2089` → `1003:2089` | Palette region. |

### Product Navigation Components — `0ykvdYvusVBthtVIMlrzJW`
**Role:** consumer/spec file — *uses* the library, **publishes nothing**
(0 components/sets/styles). **Phase:** reference only; not a catalog source.

| URL | node (URL → REST) | Notes |
|---|---|---|
| https://www.figma.com/design/0ykvdYvusVBthtVIMlrzJW/Product-Navigation-Components?node-id=0-1&m=dev | `0-1` → `0:1` | Canvas root. |

### Visualizations Tokens [Dark] — `aIltdJrmWl8Qof3qyp26k1`
**Role:** token variables only (exposed via the **Variables API**, not the
components endpoint). **Phase:** P5 — dark-theme token collection. Note the
known `variables/local` `403` (PAT scope) limitation.

| URL | node (URL → REST) | Notes |
|---|---|---|
| https://www.figma.com/design/aIltdJrmWl8Qof3qyp26k1/Visualizations-Tokens--Dark-?node-id=1356-21028&m=dev | `1356-21028` → `1356:21028` | Token region (dark). |
| https://www.figma.com/design/aIltdJrmWl8Qof3qyp26k1/Visualizations-Tokens--Dark-?node-id=1249-16514&m=dev | `1249-16514` → `1249:16514` | Token region (dark). |

### Visualizations Tokens [Light] — `5RITV5O0eIs0H5bck6fDyP`
**Role:** token variables only (light theme). **Phase:** P5.

| URL | node (URL → REST) | Notes |
|---|---|---|
| https://www.figma.com/design/5RITV5O0eIs0H5bck6fDyP/Visualizations-Tokens--Light-?node-id=4-0&m=dev | `4-0` → `4:0` | Canvas root (light). |
| https://www.figma.com/design/5RITV5O0eIs0H5bck6fDyP/Visualizations-Tokens--Light-?node-id=1249-16514&m=dev | `1249-16514` → `1249:16514` | Token region (light). |
| https://www.figma.com/design/5RITV5O0eIs0H5bck6fDyP/Visualizations-Tokens--Light-?node-id=1251-16516&m=dev | `1251-16516` → `1251:16516` | Token region (light). |

## Project: Visualizations

### Design Library for Visualizations — `XNpH8JZdAYA3KSwODFqmSA`
**Role:** charts / data-viz catalog. 325 components · 19 sets · 123 styles.
**Phase:** P2 + **cross-cutting risk #1** — prism-react may have *no* matching
chart components, so this branch may be a known-unsupported region. Quantify viz
coverage before promising codegen here.

| URL | node (URL → REST) | Notes |
|---|---|---|
| https://www.figma.com/design/XNpH8JZdAYA3KSwODFqmSA/Design-Library-for-Visualizations?node-id=0-1&m=dev | `0-1` → `0:1` | Canvas root. |
| https://www.figma.com/design/XNpH8JZdAYA3KSwODFqmSA/Design-Library-for-Visualizations?node-id=4-0&m=dev | `4-0` → `4:0` | Canvas root. |
| https://www.figma.com/design/XNpH8JZdAYA3KSwODFqmSA/Design-Library-for-Visualizations?node-id=603-202&m=dev | `603-202` → `603:202` | Viz component region. |
| https://www.figma.com/design/XNpH8JZdAYA3KSwODFqmSA/Design-Library-for-Visualizations?node-id=1249-16514&m=dev | `1249-16514` → `1249:16514` | Viz token/region. |

### Chart spec files (consume the viz lib; publish nothing)
**Role:** per-chart spec documents. **Phase:** P2 risk assessment for viz.

| Chart | URL | node (URL → REST) |
|---|---|---|
| Object Relationship Diagram — `xs6UQHlwjiPOCvkVUaEUG8` | https://www.figma.com/design/xs6UQHlwjiPOCvkVUaEUG8/Object-Relationship-Diagram?node-id=501-2&m=dev | `501-2` → `501:2` |
| Heat Map — `ynbkm60jszYliNutpfKsUz` | https://www.figma.com/design/ynbkm60jszYliNutpfKsUz/Heat-Map?node-id=2-39&m=dev | `2-39` → `2:39` |
| Bar Charts — `E3ws8ineZpgZMzG3yT6HQG` | https://www.figma.com/design/E3ws8ineZpgZMzG3yT6HQG/Bar-Charts?node-id=501-2&m=dev | `501-2` → `501:2` |

---

## Quick reference — file keys

| File key | File | Project | Catalog role |
|---|---|---|---|
| `bK52NYtDhya7uiW7dvObaQ` | Design Library | Core UI | **Primary catalog** (styleguide URLs) |
| `Z0OTY6BFR7oTCpRjCzs7lz` | Templates | Core UI | Page compositions (P4) |
| `KVbKkR0QAFRbTVpwysQLWw` | Spec Doc | Specs+Tokens | Specs + color styles (P5) |
| `5de1bNROZOtWBx8c4ArKwr` | Color Primitives | Specs+Tokens | Raw palette (P5) |
| `0ykvdYvusVBthtVIMlrzJW` | Product Navigation Components | Specs+Tokens | Consumer/spec (reference) |
| `aIltdJrmWl8Qof3qyp26k1` | Visualizations Tokens [Dark] | Specs+Tokens | Dark token vars (P5) |
| `5RITV5O0eIs0H5bck6fDyP` | Visualizations Tokens [Light] | Specs+Tokens | Light token vars (P5) |
| `XNpH8JZdAYA3KSwODFqmSA` | Design Library for Visualizations | Visualizations | Viz catalog (P2 risk) |
| `xs6UQHlwjiPOCvkVUaEUG8` | Object Relationship Diagram | Visualizations | Chart spec |
| `ynbkm60jszYliNutpfKsUz` | Heat Map | Visualizations | Chart spec |
| `E3ws8ineZpgZMzG3yT6HQG` | Bar Charts | Visualizations | Chart spec |

> Cross-reference: this matches the measured library inventory in
> `docs/figma-to-prism-codegen-roadmap.md` §1.2 and
> `docs/_audit_data/library_inventory.json`.
