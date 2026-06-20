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
  session (`tools/list` returns `map_figma_tree`).
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

## Working rules

1. **Single source of truth.** This skill drives the whole
   page-generation flow. Do not invent extra steps or skip
   phases.
2. **One mapping round-trip.** Call `map_figma_tree` exactly
   once per page. Do not call it again to "re-fetch" — re-use
   the returned `FigmaTreeMapping` for the entire composition
   phase.
3. **Tokens beat hexes.** When composing JSX, prefer the
   tokens returned by `map_figma_tree` over raw hex colors.
   Use the hex only when the bucket is `loose` or `no-match`.
4. **Do not invent components.** Only use Prism component names
   that appear in the agenda's candidates list OR in
   `mapping.primary_recommendation`. When a region has a
   `primary_recommendation` with confidence ≥ 0.8, prefer it; it's
   a deterministic pattern-derived pick that has overridden the
   ranker. Otherwise pick the top candidate, and pause to ask the
   user when its score is below `0.3` (the `low_confidence`
   warning surfaces these in `mapping.warnings`).
5. **Reference JSX is a hint, not the truth.** The
   `reference_jsx` slice in each agenda row tells you *which
   Figma child* maps to *which JSX element* — it is the
   structural prior. Compose against the candidate component's
   real prop API, not the raw Figma classnames.
6. **Respect the layout tree.** Nest JSX in the order implied
   by `layout_tree[*].children_ids`. The agenda is per-region,
   but the **layout_tree** is the spatial parent–child
   relationship.
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
[A3] Verify FIGMA_TOKEN is reachable (env or skill config). If
     missing, stop with a clear setup message.

PHASE B — GATHER FROM FIGMA (one tool call to the Figma plugin)
[B1] Call figma.get_design_context(nodeId=<parsed>) ONCE.
     - On success: capture the React+Tailwind JSX string.
     - On the known-bug failure (returns only the instruction string
       "IMPORTANT: After you call this tool, you MUST..." with no
       actual JSX): treat reference_jsx as empty and warn the user.
[B2] (Optional) Call figma.get_variable_defs(nodeId=<parsed>) ONCE
     to get the hex→token name map. Skip on failure; degrades
     gracefully.

PHASE C — MAP (one tool call to prism-mcp)
[C1] Call prism-mcp.map_figma_tree(
         node_url=<full URL the user pasted>,
         reference_jsx=<from B1, or null>,
         variable_defs=<from B2, or null>,
         figma_token=<PAT read from .env at repo root>)
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
       - returns FigmaTreeMapping

PHASE D — PLAN (no tool calls; LLM-side reasoning)
[D1] Read summary + layout_tree + agenda_summary.
[D2] Sanity-check the summary:
     - If summary.agenda_size == 0, ask the user "did you pick the
       right node? Here's what we dropped: <reasons>".
     - If summary.dropped_by_reason has any reason ≥ 80% of total,
       warn the user (something is suspicious).
[D3] Decide on the page-level skeleton: top-level <FlexLayout>?
     <Page>? <AppShell>? based on the role of the root region.
[D4] Plan the order of work using the layout_tree (root → leaves
     in DFS order). Pre-allocate import names to avoid clashes.

PHASE E — COMPOSE (no tool calls; LLM writes JSX)
For each region in agenda order:
  [E1] Pull the full agenda row (already in-context — no extra
       tool call needed; the full agenda was returned in C1).
  [E2] Pick the top candidate. If candidate score < 0.3 OR the
       top candidate is not in the list of Prism components, ask
       the user to confirm.
  [E3] Compose JSX for the region using:
       - the candidate component name + its import path
       - content_slots (title, items, etc.) for the props/children
       - structural_hints to inform layout (e.g. flexDirection)
       - hex_colors mapped via tokens (use token names not hexes
         whenever the token is "exact" or "near" — fall back to
         hex only for "loose" or "no-match" buckets)
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
     - the count of dropped nodes by reason (from summary)
     - any warnings (low-confidence candidates, missing tokens,
       walker safety-rail trips)
     - the validation status if Phase G ran
```

The whole flow is at most: **1 Figma plugin call + 1
prism-mcp mapping call + 1 file write**, plus whatever
typecheck/lint/test iterations Cursor runs locally. The MCP
tool-call budget for a page is small and predictable.

## Error handling

| Situation | Skill behavior |
|---|---|
| Figma plugin not installed | Stop, tell user to `/add-plugin figma`. |
| `FIGMA_TOKEN` not set | Stop, tell user how to get a PAT and where to put it. |
| `get_design_context` returns instruction-only string | Continue with empty `reference_jsx`. Note the warning. |
| `map_figma_tree` returns a `FetchError` with `code=file_not_found` or `code=node_not_found` | Show user the parsed file_key + node_id from the error detail; ask them to confirm URL. |
| `map_figma_tree` returns 0 agenda rows | Show the dropped reasons; ask user if this is the right node (probably they picked an empty container). |
| Top candidate confidence < 0.3 (no good match) | Pause and ask user which Prism component they'd prefer for that region. |
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
(Phase E). Substitute `{layout_tree_json}`, `{agenda_summary_json}`,
`{tokens_json}`, and `{page_name}` with the corresponding
slices of the `FigmaTreeMapping`. Keep the template stable so
that runs are reproducible.

```text
You are composing a Prism React JSX file from a structured page mapping.

Layout tree (spatial structure, root first):
{layout_tree_json}

Agenda summary (per-region, top candidate only):
{agenda_summary_json}

Tokens (use these names, NOT raw hexes):
{tokens_json}

For each region in agenda order:
0. If `box_style` is non-empty, render the visual identity first:
   `background_color`, `border_color`/`border_width`,
   `corner_radius`, `padding` (T, R, B, L), `gap`, `layout_mode`,
   `has_shadow`. Map them straight to CSS / Prism props.
1. Look up the full mapping row in the agenda.
2. If `mapping.primary_recommendation` is set AND
   `primary_recommendation_confidence >= 0.8`, prefer that component
   over the candidates list — the deterministic pattern detector
   matched a known role with high confidence. Otherwise pick the
   top candidate, and pause to ask the user if its score is < 0.3.
3. Compose JSX using the chosen component + content_slots +
   structural_hints.
4. Use tokens by name (e.g. `color="color/primary/500"`) where Prism's
   props accept them; otherwise inline the hex.
5. Respect the layout_tree's parent/child relationships when nesting.
   For every layout_tree node, consume `layout` (when present):
   - `direction` → `flexDirection: row | column` (or CSS Grid for
     `grid`). `single` means render the lone child unwrapped;
     `stack` means render the parent as `position: relative` and
     emit each child with `position: absolute`.
   - `justify_content` / `align_items` → map straight to the
     CSS-named values (`start`, `end`, `center`, `space-between`,
     `space-around`, `space-evenly`, `stretch`, `baseline`).
   - `gap` → the CSS gap in px; `gap_consistent=false` means
     `gap=null` — fall back to per-child `marginRight` /
     `marginBottom` in flow order.
6. For each region whose id is in some parent's
   `layout.absolute_children`, render it with `position: absolute`
   using `region.absolute_pos.{top,left,width,height,z_order}`.
   The parent FRAME must wrap with `position: relative`. The
   `z_order` is the literal CSS `zIndex`.

Output a single .tsx file into the user's project (e.g. src/components/{page_name}/{page_name}.tsx).
Include import statements for every Prism component you reference.
Do NOT invent components — only use names from the candidates lists.
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
[4..N-1] (no tool calls — LLM composes JSX in-memory)
[N] (skill writes the .tsx file via Cursor's file-write capability)
[N+1..] (no MCP calls — Cursor typechecks/lints/tests the page and
        iterates locally until green)
```

**Total MCP round-trips for mapping: 2.** Validation adds none —
Cursor runs tsc / lint / tests locally.

## References

- Design doc: `docs/figma-page-to-prism-plan.md` (the
  authoritative spec for the walker, the routing table, the
  pattern catalogue, and the SERVER_INSTRUCTIONS extension).
- MCP tool: `prism-mcp.map_figma_tree` — the public entrypoint
  this skill calls. `parse_figma_url`, `_fetch_figma_tree`, and
  `walk_tree` are deliberately package-private; do not invoke
  them directly.
