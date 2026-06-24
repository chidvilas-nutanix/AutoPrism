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

For deterministic generation, call `map_figma_tree` with
`response_detail="codespec"` (roadmap P8): it returns a single
**render-ready `PrismCodeSpec`** — a nested tree of JSX nodes that
already carry their resolved Prism `tag`, `import_from`, typed
`props`, `children`/`text`, and `tokens`, plus a deduped `imports`
list. Your job is to **render it verbatim — do not re-pick
components, re-derive props, or add wrapper divs**. The default
`response_detail="lean"` agenda and per-region `map_figma_node` are
the *drill-down* tools you reach for only when the spec flags a node
(a `<div>` fallback, a low `confidence`, or a composite `note`).
prism-mcp does **not** build or validate code — Cursor (you) runs
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

### `response_detail="codespec"` — the render-ready tree (preferred)

This is the P8 deliverable and the **default path for code
generation**. The response is a single `PrismCodeSpec`:

- `roots` — the top-level JSX nodes, in render order. Each
  `PrismCodeNode` is fully resolved:
  - `tag` — the JSX element to emit (`Button`, `FlexLayout`,
    `MenuIcon`, `HeaderFooterLayout`, or `div` for an unresolved
    region).
  - `import_from` — the module to import `tag` from
    (`@nutanix-ui/prism-reactjs`), or `null` for a host `div`.
  - `props` — the typed props to emit: `{name, value, value_kind}`.
    Emit by `value_kind`: `expr` → `name={value}`; `string` →
    `name="value"`; `bool` → `name` (or `name={false}`); `slot` →
    `name={<ChildNode/>}` (the child with that `slot` renders into
    this prop, not as a flow child).
  - `text` — literal element text (`<Button>Save</Button>`), or
    `null`.
  - `children` — nested `PrismCodeNode`s, in order. A child whose
    `slot` is set fills the parent's named prop (a shell's `header` /
    `bodyContent` / …); a child whose `flex_grow` is `true` is wrapped
    in `<FlexItem flexGrow="1">`.
  - `tokens` — Prism design-token names this node references.
  - `source` (`catalog` / `pattern` / `shell` / `layout` / `icon` /
    `mapper` / `fallback`) + `confidence` — provenance. `fallback`
    means "no Prism component resolved" — see `notes`.
  - `notes` — flags: a `<div>` fallback reason, or a composite
    (`Table` / `Form` / `Modal` / `Tabs` / …) you should render from
    a `map_figma_node` example rather than nesting raw children.
- `imports` — the deduped `{component, module}` list. Emit exactly
  these imports; do not add or invent others.
- `tokens` — `hex → token-name`; substitute the token for the hex.
- `stats` — `nodes` / `resolved` / `fallbacks` / `roots` /
  `imports` / `max_depth`.
- `warnings` — assembly observations (fallback count, multi-root).

Render contract: walk `roots` depth-first and emit each node as
`<{tag} {props}>{text or children}</{tag}>`. The tree is the truth —
the only nodes that need a decision from you are `source:"fallback"`
`<div>`s (drill in with `map_figma_node`, or keep the div if it only
carries layout) and `notes`-flagged composites.

### `response_detail="lean"` — the inspection agenda

By default (`response_detail="lean"`) the response is shaped to be
small. Use it to *inspect / sanity-check* a page, or as the
drill-down surface behind the code spec. Top-level keys:

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
   once per page (with `response_detail="codespec"` for generation).
   Do not call it again to "re-fetch" — re-use the returned spec for
   the entire composition phase. Use `map_figma_node` for targeted
   per-region detail on the few nodes the spec flagged.
3. **Render the code spec verbatim.** When you have the
   `codespec`, emit each `PrismCodeNode` exactly as resolved —
   its `tag`, `import_from`, `props` (honour `value_kind`),
   `text`/`children`, `slot`, and `flex_grow`. Do **not** swap
   components, re-derive props, reorder children, or insert wrapper
   `<div>`s / inline CSS. The spec already ran the deterministic
   identity → props → layout → tokens → content cascade; second-
   guessing it re-introduces the drift P8 removed.
4. **Tokens beat hexes.** The `tokens` map is `hex → token-name`
   (the designer's `variable_defs`). When a hex has a non-empty
   token name, use the token name in JSX instead of the raw hex.
   When the value is empty (designer didn't name it), either keep
   the hex or call `map_figma_node` for that region — its
   `token_mappings` carry the closest Prism token plus a
   perceptual bucket (`exact` / `near` / `loose` / `no-match`);
   prefer the matched token for `exact`/`near`, fall back to the
   hex for `loose`/`no-match`.
5. **Do not invent components (drill-down path).** The codespec
   already chose every component for you. When you *do* drill into
   the lean agenda or `map_figma_node` (for a `<div>` fallback or a
   composite), only use Prism component names that appear in that
   region's `candidates` list, `suggested_component_name`, or
   `primary_recommendation`. Prefer a `primary_recommendation` with
   confidence ≥ 0.8; otherwise take the top candidate by `score`, and
   pause to ask the user when it is below `0.3`.
6. **Reference JSX is a hint, not the truth.** The whole-page
   `reference_jsx` you pass *into* `map_figma_tree` (from
   `get_design_context`) is used by the walker to sharpen ranking.
   The per-row `reference_jsx_slice` (which Figma child maps to
   which JSX element) is **omitted in the lean response** — pass
   `response_detail="full"` if you need those slices. Either way,
   compose against the candidate component's real prop API, not
   the raw Figma classnames.
7. **Respect the structure.** With the codespec, the tree's nesting
   is authoritative — render `children` in order, honour `slot` and
   `flex_grow`. (In the lean path, the equivalent is
   `layout_tree[*].children_ids`.) Spatial `layout` / `absolute_pos`
   hints remain conservative and usually absent; consume them only
   when present.
8. **Write into the user's project**, at the path they ask for
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
         response_detail="codespec")   # render-ready tree (P8)
     Use "codespec" for generation — it returns the PrismCodeSpec you
     render verbatim. Pass "lean" instead only when the user just
     wants to inspect the page, or "full" when they need every
     region's heavy detail (examples / a11y / full candidates) in a
     single payload. You can still drill per-region with
     map_figma_node in Phase E for any flagged node.

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
[D1] Read stats + warnings + the spec roots.
[D2] Sanity-check the response:
     - If stats.nodes == 0 (or roots is empty), ask the user "did
       you pick the right node?" and re-run lean to show the
       dropped_summary.
     - If stats.fallbacks is a large fraction of stats.nodes, warn
       the user — many regions did not resolve (likely an
       annotation master, a mostly-decorative frame, or the wrong
       node). The fallback `<div>`s are listed in the tree with a
       `notes` reason.
     - If stats.roots > 1, the page has spatially disjoint top
       frames; you will wrap the roots (see D3).
[D3] Decide the top-level wrapper: if a root is a shell
     (`HeaderFooterLayout` / `MainPageLayout` / `LeftNavLayout`),
     that IS the page skeleton — render it directly. If stats.roots
     > 1, wrap the roots in a Fragment (or the shell) in render
     order.
[D4] Collect the `imports` list — emit exactly those import lines.

PHASE E — COMPOSE (render the spec verbatim; drill down only on flags)
Walk the spec `roots` depth-first. For each PrismCodeNode:
  [E1] Emit `<{tag}` + its `props`:
       - `value_kind=="expr"`  → `name={value}`
       - `value_kind=="string"`→ `name="value"`
       - `value_kind=="bool"`  → `name` (or `name={false}`)
       - `value_kind=="slot"`  → `name={<Child/>}`, rendering the
         child whose `slot` equals `name` here instead of inline.
  [E2] Emit the body:
       - if `text` is set → `>{text}</{tag}>`
       - else recurse `children` (skipping any child already
         consumed as a `slot`), wrapping a child with
         `flex_grow==true` in `<FlexItem flexGrow="1">`.
       - a leaf with neither → self-close `<{tag} ... />`.
  [E3] Handle the flags — these are the ONLY nodes that need a
       decision:
       - `source=="fallback"` (`tag=="div"`): the region did not
         resolve. Either keep the `<div>` (if it is just a layout
         wrapper) or drill in with
         `map_figma_node(node_name=<node name>, node_type=<role>,
         hex_colors=…)` to find a real component, then render that.
       - a `notes` entry flagging a composite (`Table` / `Form` /
         `Modal` / `Tabs` / …): call `map_figma_node` for that node
         and imitate the returned `examples` JSX (these components
         take config props — `columns` / `items` — not raw
         children, so the spec leaves their sub-parts as siblings).
       - low `confidence` (< 0.3) on a non-fallback node: render it,
         but flag it to the user as a guess.
  [E4] Substitute tokens: replace any hex with its `tokens[hex]`
       name when non-empty; a node's own `tokens` list names the
       color / typography tokens it references.
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
(Phase E). Substitute `{code_spec_json}` and `{page_name}` with
the `PrismCodeSpec` returned by
`map_figma_tree(response_detail="codespec")`. Keep the template
stable so that runs are reproducible.

```text
You are emitting a Prism React .tsx file from a render-ready code spec.
RENDER IT VERBATIM. Do not pick components, derive props, reorder
children, or add wrapper <div>s / inline CSS — the spec is the truth.

Code spec (PrismCodeSpec: roots + imports + tokens + stats + warnings):
{code_spec_json}

Imports: emit exactly the `imports` list — `import { A, B } from "<module>"`.
Add nothing else; do NOT invent components.

For each PrismCodeNode, depth-first over `roots`:
1. Open `<{tag}` and emit every prop in `props` by value_kind:
   - expr   → name={value}
   - string → name="value"
   - bool   → name   (or name={false} when value is "false")
   - slot   → name={<Child/>}, where Child is the node in `children`
              whose `slot` == name (render it here, not inline).
2. Emit the body:
   - `text` set        → >{text}</{tag}>
   - else `children`   → recurse, skipping slot-consumed children;
                         wrap any child with flex_grow==true in
                         <FlexItem flexGrow="1">…</FlexItem>.
   - neither           → self-close <{tag} ... />.
3. Substitute tokens: anywhere a hex appears, use tokens[hex] when
   non-empty; a node's `tokens` list names the color/typography
   tokens it relies on.
4. Flags are the only nodes needing judgement:
   - source=="fallback" (tag=="div"): the region did not resolve.
     Keep the div if it is only a layout wrapper, else drill in with
     map_figma_node and render the real component it returns.
   - a `notes` composite flag (Table/Form/Modal/Tabs/…): imitate the
     map_figma_node `examples` JSX (these take config props, not raw
     children).
   - confidence < 0.3 on a non-fallback node: render it, but tell the
     user it is a guess.

Output a single .tsx file into the user's project (e.g. src/components/{page_name}/{page_name}.tsx).
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
        figma_token=<value of FIGMA_TOKEN read from .env>,
        response_detail="codespec")   # render-ready PrismCodeSpec
[4..M] (optional) prism-mcp.map_figma_node(node_name=..., node_type=...,
        reference_code=..., hex_colors=...) ONLY for the spec nodes
        flagged source:"fallback" or with a composite `note` —
        returns full candidates / examples / a11y / token buckets
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
