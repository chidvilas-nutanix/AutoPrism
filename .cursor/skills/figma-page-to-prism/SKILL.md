---
name: figma-page-to-prism
description: >-
  Orchestrates the end-to-end conversion of a Figma node (page, view,
  modal, or composed region) into a validated Prism React `.tsx` file.
  Trigger automatically when the user message contains a
  `figma.com/design/...` or `figma.com/file/...` URL AND asks for code
  generation ("build", "implement", "make", "convert this to
  React/Prism", "generate the page", or any synonym). Also trigger
  when the user explicitly invokes `/figma-page-to-prism <url>`. The
  skill is the single source of truth for the page-generation flow
  and walks the LLM through Phases A–H below; do not skip phases or
  reorder them.
metadata:
  surfaces:
    - ide
---

# Figma page → Prism

This skill turns a Figma node URL into a validated Prism React
`.tsx` file. It is the single source of truth for the page-level
flow that complements the per-node `map_figma_node` tool.

`map_figma_tree` returns a **lean** agenda by default (see "What
`map_figma_tree` returns" below) so it does not flood your context.
Drill into any single region on demand with `map_figma_node`, or
ask for everything at once with `response_detail="full"`. prism-mcp
does **not** build or validate code — Cursor (you) runs
tsc / eslint / tests in your own loop.

## When to use

Activate this skill when **both** are true:

1. The user message contains a Figma URL — `figma.com/design/...`
   or `figma.com/file/...`, with or without a `?node-id=...`
   query parameter.
2. The user asks for code generation — verbs like "build",
   "implement", "make", "convert to React/Prism", "generate the
   page", "scaffold", or any equivalent intent from the
   `skills-cursor` lexicon.

Also activate when the user explicitly invokes
`/figma-page-to-prism <url>`.

Do **not** activate for per-node single-component mapping; the
existing `map_figma_node` tool is sufficient there. Do not
activate when the user only wants to *inspect* the Figma node
(no code generation requested).

## Prerequisites

- `prism-mcp` is registered and reachable from this Cursor
  session (`tools/list` returns `map_figma_tree`). The server
  exposes **7 tools**: `echo`, `get_library_meta`,
  `search_entities`, `search_examples`, `get_entity`,
  `map_figma_node`, `map_figma_tree`. There are no
  build/validate/workflow tools — code validation is yours.
- The official Figma plugin is installed in Cursor and reachable
  via `figma.get_design_context` / `figma.get_variable_defs`.
- `FIGMA_TOKEN` is set in the user's environment (or in the
  repo's `.env`). The token must be a personal access token with
  *file:read* scope, generated at
  <https://www.figma.com/settings>. Without this token the
  internal `_fetch_figma_tree` call inside `map_figma_tree`
  cannot reach the Figma REST API.

If any prerequisite is missing, stop and tell the user how to
fix it — do not silently degrade.

## What `map_figma_tree` returns

By default (`response_detail="lean"`) the response is shaped to be
small. Top-level keys:

- `layout_tree` — the nested spatial structure. Each node carries
  `id`, `name`, `role`, `bbox`, `children_ids`, and an optional
  `layout` block. Nest your JSX in `children_ids` order.
- `agenda` — the ordered list of region decisions (pre-order DFS).
  Each **lean** row carries the descriptive fields —
  `id`, `name`, `role`, `bbox`, `parent_chain` (last 3 only),
  `shape_bucket`, `children_summary`, `content_slots`,
  `structural_hints`, `box_style`, `hex_colors`, `absolute_pos` —
  plus a slim `mapping`:
  - `suggested_component_name` — the headline pick.
  - `primary_recommendation` + `primary_recommendation_confidence`
    — a deterministic pattern-derived pick (confidence `1.0`)
    when the region role matched a known pattern, else `null`/`0.0`.
  - `description` — the top candidate's one-line summary.
  - `candidates` — top-3 as `{name, score}` **only**.
- `tokens` — `hex → token-name` map seeded from the designer's
  `variable_defs`. An empty-string value means the designer did
  not name that hex (see working rule 3).
- `dropped_summary` — `{reason: count}` map (replaces the full
  per-node `dropped` audit list).
- `summary` — counters: `input_nodes`, `agenda_size`,
  `dropped_total`, `tokens_count`, per-reason `dropped_<reason>`,
  the safety-rail limits, etc.
- `warnings` — soft observations (e.g. low-confidence picks,
  agenda-size overflow).
- `reduction` — telemetry: `input_nodes`, `agenda_size`,
  `dropped_count`, `response_chars_full`, `response_chars_lean`.

Heavy per-row payload — full `candidates` (with `why_matched` /
`summary` / `source`), `examples` (raw JSX), `a11y_blocks`,
`token_mappings` (with perceptual buckets), `related`,
`candidate_decompositions`, and `reference_jsx_slice` — is **not**
in the lean response. Get it one of two ways:

- **Per region (preferred):** call
  `map_figma_node(node_name, node_type?, reference_code?, hex_colors?)`
  for the one region you need detail on.
- **Whole page:** pass `response_detail="full"` to
  `map_figma_tree` to reproduce the legacy full payload in one
  shot (large — only do this when you genuinely need every
  region's detail).

## Working rules

1. **Single source of truth.** This skill drives the whole
   page-generation flow. Do not invent extra steps or skip
   phases.
2. **One mapping round-trip.** Call `map_figma_tree` exactly
   once per page (lean by default). Do not call it again to
   "re-fetch" — re-use the returned mapping for the entire
   composition phase. Use `map_figma_node` for targeted per-region
   detail; only pass `response_detail="full"` when you truly need
   every region's heavy payload up front.
3. **Tokens beat hexes.** The `tokens` map is `hex → token-name`
   (the designer's `variable_defs`). When a hex has a non-empty
   token name, use the token name in JSX instead of the raw hex.
   When the value is empty (designer didn't name it), either keep
   the hex or call `map_figma_node` for that region — its
   `token_mappings` carry the closest Prism token plus a
   perceptual bucket (`exact` / `near` / `loose` / `no-match`);
   prefer the matched token for `exact`/`near`, fall back to the
   hex for `loose`/`no-match`.
4. **Do not invent components.** Only use Prism component names
   that appear in a region's `candidates` list, in
   `suggested_component_name`, or in `primary_recommendation`.
   When a region has a `primary_recommendation` with confidence
   ≥ 0.8, prefer it — it is a deterministic pattern-derived pick
   that overrode the ranker. Otherwise pick the top candidate by
   `score`, and pause to ask the user when that score is below
   `0.3` (low-confidence picks are flagged in the top-level
   `warnings`). For the rationale behind a pick, or for richer
   options, drill in with `map_figma_node`.
5. **Reference JSX is a hint, not the truth.** The whole-page
   `reference_jsx` you pass *into* `map_figma_tree` (from
   `get_design_context`) is used by the walker to sharpen ranking.
   The per-row `reference_jsx_slice` (which Figma child maps to
   which JSX element) is **omitted in the lean response** — pass
   `response_detail="full"` if you need those slices. Either way,
   compose against the candidate component's real prop API, not
   the raw Figma classnames.
6. **Respect the layout tree.** Nest JSX in the order implied by
   `layout_tree[*].children_ids` — that is the authoritative
   parent→child structure. Derive flow direction from each
   region's `bbox` and `structural_hints`. The optional `layout`
   block and per-region `absolute_pos` are forward-compatible
   spatial hints but are **currently usually absent** (spatial
   inference is intentionally conservative right now); consume
   them only "when present" and never block on them.
7. **Write into the user's project**, at the path they ask for
   (e.g. `src/components/<PageName>/`). Do not create files
   outside the project the user is working in.

## Phase A–H orchestration

The skill must execute the phases in order. Each `[X#]` is one
discrete step.

```
PHASE A — INPUT
[A1] Parse the Figma node URL from the user's message.
[A2] Verify the Figma plugin is available (check tool availability
     in the current Cursor session).
[A3] Verify FIGMA_TOKEN is reachable (env or .env). If missing,
     stop with a clear setup message.

PHASE B — GATHER FROM FIGMA (one tool call to the Figma plugin)
[B1] Call figma.get_design_context(nodeId=<parsed>) ONCE.
     - On success: capture the React+Tailwind JSX string.
     - On the known-bug failure (returns only the instruction string
       "IMPORTANT: After you call this tool, you MUST..." with no
       actual JSX): treat reference_jsx as empty and warn the user.
[B2] (Optional) Call figma.get_variable_defs(nodeId=<parsed>) ONCE
     to get the hex→token name map. Skip on failure; degrades
     gracefully (the `tokens` map just ends up with empty values).

PHASE C — MAP (one tool call to prism-mcp)
[C1] Call prism-mcp.map_figma_tree(
         node_url=<full URL the user pasted>,
         reference_jsx=<from B1, or null>,
         variable_defs=<from B2, or null>,
         figma_token=<PAT read from .env at repo root>,
         response_detail="lean")   # default; omit unless overriding
     Leave response_detail at the default "lean". Only pass "full"
     when the user explicitly needs every region's heavy detail
     (examples / a11y / full candidates) in a single payload —
     otherwise drill per-region with map_figma_node in Phase E.

     ALWAYS pass `figma_token` explicitly — read the FIGMA_TOKEN
     value out of `.env` at the repo root (or the workspace `.env`)
     and forward it as the `figma_token` argument. Do NOT rely on
     the prism-mcp server inheriting FIGMA_TOKEN from process env:
     Cursor's MCP config in `.cursor/mcp.json` does not automatically
     propagate the user's shell `.env`, so the server-side fallback
     to `os.environ["FIGMA_TOKEN"]` will return `missing_token` even
     when the value is sitting in `.env`. Reading + forwarding the
     PAT from `.env` is the deterministic way to get it through.

     prism-mcp internally:
       - parses node_url
       - fetches the raw tree via the private _fetch_figma_tree
         (REST API + cache)
       - walks the tree (noise filter + routing + patterns)
       - returns the lean mapping (or full, if requested)

PHASE D — PLAN (no tool calls; LLM-side reasoning)
[D1] Read summary + layout_tree + agenda.
[D2] Sanity-check the response:
     - If summary.agenda_size == 0 (or agenda is empty), ask the
       user "did you pick the right node? Here's what we dropped:
       <dropped_summary>".
     - If any single reason in `dropped_summary` is ≥ 80% of
       `summary.dropped_total`, warn the user (something is
       suspicious — likely the wrong node or a mostly-decorative
       frame).
     - Glance at `reduction` to confirm the lean trim landed (a
       large gap between response_chars_full and response_chars_lean
       is normal and healthy).
[D3] Decide on the page-level skeleton: top-level <FlexLayout>?
     <Page>? <AppShell>? based on the role of the root region.
[D4] Plan the order of work using the layout_tree (root → leaves
     in DFS order). Pre-allocate import names to avoid clashes.

PHASE E — COMPOSE (LLM writes JSX; optional per-region drill-down)
For each region in agenda order:
  [E1] Read the lean agenda row (already in-context from C1).
  [E2] Pick the component:
       - If primary_recommendation is set AND
         primary_recommendation_confidence >= 0.8, use it.
       - Else pick the top candidate by score. If that score < 0.3
         OR the name is not a real Prism component, pause and ask
         the user which Prism component to use.
  [E2b] (Optional drill-down) When a region is ambiguous, low
        confidence, needs accessibility guidance, or you want an
        idiomatic example to imitate, call:
          map_figma_node(
              node_name=<region.name>,
              node_type=<region.role or the Figma type>,
              reference_code=<region's reference_jsx_slice if you
                              fetched full, else null>,
              hex_colors=<region.hex_colors>)
        That returns the full candidates (with why_matched),
        examples (raw JSX to imitate), a11y_blocks, related
        components, and token_mappings (with perceptual buckets)
        for just that one region.
  [E3] Compose JSX for the region using:
       - the chosen component name + its import path
       - content_slots (title, items, etc.) for the props/children
       - structural_hints to inform layout (e.g. flexDirection)
       - hex_colors mapped via tokens (use the token name from the
         `tokens` map when non-empty; otherwise use the hex or the
         map_figma_node token_mappings bucket per working rule 3)
       - **box_style for the visual identity of containers**:
         `background_color`, `border_color`, `border_width`,
         `corner_radius`, `padding` (T, R, B, L), `gap`,
         `layout_mode`, `has_shadow`, `opacity`. These are
         CSS-aligned exact values — pass them straight through as
         component props or inline styles. Padding is already in
         CSS shorthand order; map `layout_mode: "HORIZONTAL"` to
         `flexDirection: "row"`, `"VERTICAL"` to
         `flexDirection: "column"`. Skip any field that is
         `null`/missing — empty box_style means the FRAME paints
         nothing and the LLM should not introduce a wrapper.
  [E4] Respect the layout_tree's children_ids when nesting.
  [E5] Append to the in-memory page JSX.

PHASE F — WRITE
[F1] Write the assembled page JSX into the user's project, at the
     path they asked for (e.g. src/components/<PageName>/<PageName>.tsx).
[F2] Write any companion files the page needs (e.g. an index
     barrel or a test stub) alongside it.

PHASE G — VALIDATE (in Cursor's own loop)
prism-mcp does not build or validate code — you do.
  [G1] Typecheck / lint / test the written page with the
       project's normal tools (tsc, eslint, the test runner).
  [G2] On failure, fix the JSX and re-validate. Re-query
       map_figma_node / search_examples / get_entity for the
       offending region when you need a better component or
       prop usage. Repeat until green.

PHASE H — REPORT
[H1] Report to the user:
     - the generated file path
     - the count of regions written + candidates picked
     - the dropped-node counts by reason (from dropped_summary)
     - any warnings (low-confidence candidates, missing tokens,
       walker safety-rail trips)
     - the validation status from Phase G
```

The whole flow is at most: **1 Figma plugin call + 1
prism-mcp mapping call + optional N `map_figma_node` drill-down
calls + 1 file write**, plus whatever typecheck/lint/test
iterations Cursor runs locally. The MCP tool-call budget for a
page is small and predictable.

## Error handling

| Situation | Skill behavior |
|---|---|
| Figma plugin not installed | Stop, tell user to `/add-plugin figma`. |
| `FIGMA_TOKEN` not set | Stop, tell user how to get a PAT and where to put it. |
| `get_design_context` returns instruction-only string | Continue with empty `reference_jsx`. Note the warning. |
| `map_figma_tree` returns a `FetchError` with `code=file_not_found` or `code=node_not_found` | Show user the parsed file_key + node_id from the error detail; ask them to confirm URL. |
| `map_figma_tree` returns 0 agenda rows | Show `dropped_summary`; ask user if this is the right node (probably they picked an empty container). |
| Top candidate score < 0.3 (no good match) | Drill with `map_figma_node`; if still weak, pause and ask user which Prism component they'd prefer for that region. |
| Generated JSX fails tsc / lint | Fix in Cursor's normal edit + re-validate loop; re-query map_figma_node / get_entity for a better component if needed. |

`FetchError` codes the skill should be ready to interpret:

- `missing_token` — set `FIGMA_TOKEN` and retry.
- `invalid_token` — regenerate the PAT, then retry.
- `file_not_found` / `node_not_found` — confirm the URL with
  the user.
- `rate_limited` — wait and retry; the fetcher already does 3
  exponential-backoff retries internally, so a surfaced
  `rate_limited` error means we exhausted them.
- `network_timeout` / `transport_error` — usually transient;
  retry once; if it persists, surface to the user.
- `tree_too_large` — ask the user to pick a smaller / deeper
  node (the 10 MB cap exists to keep token usage sane).

## Composition prompt template

Use this prompt template verbatim at composition time
(Phase E). Substitute `{layout_tree_json}`, `{agenda_json}`,
`{tokens_json}`, and `{page_name}` with the corresponding
slices of the lean mapping. Keep the template stable so that
runs are reproducible.

```text
You are composing a Prism React JSX file from a structured page mapping.

Layout tree (spatial structure, root first; nest by children_ids):
{layout_tree_json}

Agenda (lean per-region rows: chosen component + top-3 {name, score}):
{agenda_json}

Tokens (hex → token-name; use the name when non-empty, NOT the raw hex):
{tokens_json}

For each region in agenda order:
0. If `box_style` is non-empty, render the visual identity first:
   `background_color`, `border_color`/`border_width`,
   `corner_radius`, `padding` (T, R, B, L), `gap`, `layout_mode`,
   `has_shadow`. Map them straight to CSS / Prism props.
1. Read the lean mapping row for the region.
2. If `mapping.primary_recommendation` is set AND
   `primary_recommendation_confidence >= 0.8`, prefer that component
   over the candidates list — the deterministic pattern detector
   matched a known role with high confidence. Otherwise pick the
   top candidate by score, and pause to ask the user if its score
   is < 0.3. The `description` is the one-line summary of the pick.
   If you need the rationale, an example to imitate, or a11y
   guidance, call map_figma_node for this one region.
3. Compose JSX using the chosen component + content_slots +
   structural_hints.
4. Use tokens by name (e.g. `color="color/primary/500"`) where Prism's
   props accept them and the `tokens` value is non-empty; otherwise
   inline the hex.
5. Respect the layout_tree's children_ids when nesting; derive flow
   direction from each region's bbox + structural_hints. If a node
   carries an optional `layout` block (currently usually absent),
   consume it when present:
   - `direction` → `flexDirection: row | column` (or CSS Grid for
     `grid`). `single` means render the lone child unwrapped;
     `stack` means render the parent as `position: relative` and
     emit each child with `position: absolute`.
   - `justify_content` / `align_items` → map straight to the
     CSS-named values (`start`, `end`, `center`, `space-between`,
     `space-around`, `space-evenly`, `stretch`, `baseline`).
   - `gap` → the CSS gap in px.
6. If a region carries a non-null `absolute_pos` (currently usually
   absent), render it with `position: absolute` using
   `absolute_pos.{top,left,width,height,z_order}`, and wrap the
   parent FRAME with `position: relative`. `z_order` is the literal
   CSS `zIndex`.

Output a single .tsx file into the user's project (e.g. src/components/{page_name}/{page_name}.tsx).
Include import statements for every Prism component you reference.
Do NOT invent components — only use names from the candidates / suggested_component_name / primary_recommendation.
Do NOT use any non-Prism dependency.
```

## Concrete tool-call sequence (reference run)

For the Active Cluster page (`?node-id=624-6826`):

```
[1] figma.get_design_context(nodeId="624:6826")
[2] figma.get_variable_defs(nodeId="624:6826")
[3] prism-mcp.map_figma_tree(
        node_url=...,
        reference_jsx=...,
        variable_defs=...,
        figma_token=<value of FIGMA_TOKEN read from .env>)
        # response_detail defaults to "lean"
[4..M] (optional) prism-mcp.map_figma_node(node_name=..., node_type=...,
        reference_code=..., hex_colors=...) for each ambiguous /
        low-confidence region that needs full candidates / examples /
        a11y / token buckets
[M+1..N-1] (no tool calls — LLM composes JSX in-memory)
[N] (skill writes the .tsx file via Cursor's file-write capability)
[N+1..] (no MCP calls — Cursor typechecks/lints/tests the page and
        iterates locally until green)
```

**Total MCP round-trips for mapping: 1** (plus optional
per-region `map_figma_node` drill-downs). Validation adds none —
Cursor runs tsc / lint / tests locally.

## References

Shipped with the repo (read these first):

- `README.md` + `SETUP.md` (repo root) — quickstart, the 7-tool
  surface, credentials, and model download.
- The MCP tool docstrings + `SERVER_INSTRUCTIONS` in
  `src/prism_mcp/server.py` — the canonical, always-current
  description of `map_figma_tree` / `map_figma_node` and the
  lean-vs-full contract.
- MCP tool: `prism-mcp.map_figma_tree` — the public entrypoint
  this skill calls. `parse_figma_url`, `_fetch_figma_tree`, and
  `walk_tree` are deliberately package-private; do not invoke
  them directly.

Internal working docs (under `docs/`, **gitignored — not shipped**;
present only in the team's working tree):

- `docs/prism-mcp-deep-dive.md` — code-verified server walkthrough
  (its header notes the verification loop / tools were removed).
- `docs/temporal-verification-removal.md` — what the trim removed
  (Temporal / AlphaCodium loop, SSIM, the scratch writer, 7 of 14
  tools) and why validation is now Cursor-owned.
- `docs/figma-page-to-prism-plan.md` — design spec for the walker,
  routing table, and pattern catalogue.
