# Post-v1 Backlog

Items deliberately deferred past the v1 ship (Slices 1–8 complete; see
[`docs/prd/prism-reactjs-mcp-server.md`](docs/prd/prism-reactjs-mcp-server.md)).
The v1 surface (`echo`, `get_library_meta`, `list_entities`, `get_entity`,
`search_entities`) is observably complete; everything below is either a
known precision gap or a reach beyond the published tarball.

Each item should grow into a small grill / PRD slice before landing — do not
ship from this list directly.

---

## 1. Investigate `.tsx` fallback for inline `defaultProps` and enum literals

**Problem.** Our type extraction reads `lib/components/v2/**/*.d.ts` exclusively
(PRD §6 decision: `.d.ts`-first). Two real-world Prism patterns that the
compiled `.d.ts` cannot represent:

- **Inline `defaultProps`.** When a component declares
  `Button.defaultProps = { variant: 'primary' }` in the `.tsx`, the
  default *value* lands in JS at runtime but the `.d.ts` only records
  the prop's *type*. Our parsed `Member.default` is `None` for these.
- **Enum literal values.** `export enum Variant { Primary = 'primary' }`
  in the `.tsx` becomes `export declare enum Variant { Primary, ... }`
  in the `.d.ts` with the runtime-value annotation gone. An LLM that
  reads our `Entity` cannot tell what string literal to pass.

**Why deferred.** v1's PRD §10 calls this out as a measurable question;
without numbers on how often Prism actually uses these patterns we'd be
guessing at the cost / benefit of a `.tsx` parser dependency.

**Action.** Run the v1 server against a freshly-published Prism tarball,
tally components whose `signature` has `Member.default is None` for props
that the `.tsx` source *does* set defaults for. Same for enums. If the gap
is >15% of components, scope a v2 slice that introduces a TypeScript
parser (probably `tree-sitter-typescript` or `pyright`'s parser as a
library); if <5%, document the known precision floor in the README and
move on.

**Acceptance.** A short measurement note in `docs/` with the per-prop /
per-enum gap counts and a recommended path. No code changes ship from
this ticket directly — its output is a decision.

**Owner.** TBD.
**Effort estimate.** ~½ day of measurement, then either out (write-up
only) or a multi-slice follow-on if we adopt a `.tsx` parser.

---

## 2. Branding assets (icons, logos) reachable from the MCP server

**Problem.** Prism's branding assets live in `styleguide/` in the source
repo. They are **not** in the package.json `files` array and therefore
never make it into the npm tarball that v1 reads. The MCP server cannot
surface icon names, logo IDs, or asset paths today.

**Why deferred.** PRD §4 lists branding as explicitly out of scope for
v1 because the chosen source-of-truth (the published tarball) cannot
reach those assets. Picking a second source midway through the hackathon
would have multiplied the data-model and reliability surface.

**Action.** Pick one of the two paths PRD §10 lays out:

- **(a) Upstream conversation.** File an issue / PR against the Prism
  ReactJS publish config to add `styleguide/` to the `files` array (or
  emit a manifest under `lib/`). Pros: keeps v1's "one source of truth"
  invariant; v2 inherits the same tarball-only acquisition path. Cons:
  depends on the Prism UI team's roadmap.
- **(b) Second source.** Add a parallel acquisition path (small git
  clone of the styleguide repo, or a sibling npm package) that emits
  `icon` / `logo` entities. Pros: unblocks v2 without upstream changes.
  Cons: a second source means a second freshness/auth/cache story; the
  data model needs two new entity types and `get_entity` needs a routing
  layer.

**Acceptance.** Decision recorded as a one-page design note in `docs/`
naming the chosen path. If (a), an upstream issue/PR link. If (b), a v2
PRD slice scoped to one new entity type (start with `icon`; defer `logo`
until icon ships).

**Owner.** TBD. The upstream conversation is the user's; the agent can
own (b) once the path is picked.

**Effort estimate.** (a) one conversation + waiting on upstream; (b) a
~3-slice v2 feature with its own grill and PRD.

---

## Triage convention

When an item becomes the next thing to work on, move it out of this file:

1. Grill the scope until the success criteria are unambiguous (see
   `.cursor/skills/grill-me/SKILL.md`).
2. Write a slice into the PRD (see `.cursor/skills/write-prd/SKILL.md`)
   if it's a >1-day change, otherwise tackle it directly with TDD.
3. Delete the corresponding section from this file once the slice has
   shipped.

Items completed since v1 (none yet) get archived to a `## Done` section
at the bottom rather than deleted, so the trail of what was deferred
stays auditable.
