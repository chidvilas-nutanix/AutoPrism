# 09 — Warning-ordering fix: stale `low_confidence` alarms on a deterministic spec

> **Status: DONE.** Post-P8 correctness fix. The `codespec` for the X-Ray
> "Tests - Landing" page (`9188:127717`) resolved correctly — 84/86 nodes
> deterministic — yet shipped **34 `low_confidence` warnings** that told the
> consuming agent the whole spec was unreliable. This was a **warning-emission
> ordering bug**, not a mapping bug. Fixed by moving the audit to run *after*
> Tier-1 catalog routing.

This is the "fix 1" from the deep-dive analysis of request
`4043dd96-aefd-4503-852c-99de5a7f1806` (fixes 2–4 deliberately deferred).

---

## Symptom

Running `map_figma_tree` (`response_detail="codespec"`) on the X-Ray
"Tests - Landing" page returned a spec the docs predicted would be high
confidence (`docs/figma-prism-mapper-coverage.md`: 504 instances, 99% mapped,
93% exact-key), but the output carried 34 `low_confidence` warnings such as:

```
low_confidence region 8658:56441 ('Action/ Button'): top candidate 'ConfirmModal'
  score=0.03 below threshold 0.05; the LLM should disclaim the auto-pick or
  fall back to atomic tools.
```

The consuming agent (correctly) read those warnings as "don't trust this spec"
and hand-rolled the page from the screenshot instead — even though
`8658:56441` had in fact resolved to **`Button`** via an exact `componentKey`
hit (`source: catalog`, `confidence: 1.0`) in the *same* output. The warnings
described a fuzzy candidate (`ConfirmModal`) the catalog had already overridden.

## Root cause — the audit ran before the override

The per-region `low_confidence` audit (`walker.py::_maybe_emit_low_confidence_warning`)
guards itself correctly:

```python
if region.mapping.primary_recommendation is not None:
    return  # a deterministic / pattern pick owns the headline — not low-confidence
```

…but it was **called too early**. In `walk_tree` the pipeline order was:

1. DFS emits regions; each production region queues a `map_figma_node` job.
2. `_resolve_pending_mappings(ctx)` runs the fuzzy mapper **and, at its tail,
   emitted the `low_confidence` warning for every job.**  ← here
3. `_apply_catalog_routing(ctx)` promotes each exact `componentKey` hit into
   the headline via `_promote_resolution_to_headline` (sets
   `mapping.primary_recommendation = res.prism_component`).

So at step 2 the catalog had **not run yet** — every region's
`primary_recommendation` was still `None`, the guard never fired, and the
audit flagged each region against the very fuzzy candidate step 3 was about
to discard. The warnings were *stale by construction*.

`map_figma_tree` surfaces `mapping.warnings` for **all** response modes (lean /
full / codespec), so the noise hit every consumer, not just codespec.

## The fix

Move the audit so it runs on the **final** headline pick — after routing.

- `walker.py::_resolve_pending_mappings` — removed the tail loop that emitted
  the warnings. Its job is now purely "resolve the queued mappings"; docstring
  updated from "three optimisations" to two + a note on where the audit went.
- `walker.py::_emit_low_confidence_warnings(ctx)` (new) — iterates
  `ctx.agenda` (not `mapping_jobs`, which `_resolve_pending_mappings` clears at
  its tail) and calls the unchanged `_maybe_emit_low_confidence_warning` per
  region.
- `walker.py::walk_tree` — calls `_emit_low_confidence_warnings(ctx)` **after**
  `_apply_catalog_routing` + the prop/content passes, before the agenda trim.

The audit logic and threshold (`_LOW_CONFIDENCE_THRESHOLD = 0.05`) are
untouched — only *when* it reads `primary_recommendation` changed. A region the
catalog resolved exactly now carries a headline, so the existing guard
suppresses its warning. Genuinely-unresolved regions (frames with no
`componentId`, the page root) still have `primary_recommendation is None` and
stay flagged.

### Why `ctx.agenda` and not `ctx.mapping_jobs`

The original tail loop iterated `ctx.mapping_jobs`, but that list is cleared at
the end of `_resolve_pending_mappings` (`walker.py`, `ctx.mapping_jobs.clear()`),
so reading it post-routing yields nothing. `ctx.agenda` holds the same regions
with their resolved mappings and persists. No region is double-flagged: the
stub path (`map_figma_node_fn is None`) already audits synchronously at emit
time against an *empty* placeholder mapping, and the guard's `not candidates`
short-circuit makes the agenda pass a no-op for those rows. The two paths are
mutually exclusive (`_invoke_mapping_fn` either queues a job **or** returns
`kwargs=None` for the synchronous stub), so a region is audited exactly once
with candidates.

## Verification

Cross-referencing the **actual run's** 34 warnings against the final node
`source` in the shipped codespec (`/tmp/beforeafter.py`):

```
ACTUAL RUN: 34 low_confidence warnings emitted
  -> SUPPRESSED by the fix (final source is deterministic): 32
  -> REMAIN (final source mapper/fallback): 2
      8658:56367  ('Navigation/Header') -> NavBarLayout  (source: mapper)
      9188:127717 ('Tests - Landing')   -> Pagination    (source: mapper)
```

The 2 survivors are *honestly* low-confidence — they genuinely fell to the
fuzzy mapper (the page root + the nav header shell, the targets of the
deferred fix 3). Re-walking the real cached payload
(`au4217fUWv0x4p4surKH44--9188_127717--12.json`) with the committed catalog +
a sub-threshold stub mapper confirms every catalog-resolved row drops out of
the warning set.

```bash
uv run ruff check src/prism_mcp/figma/walker.py tests/test_figma_tier1_routing.py
#   -> only the 5 pre-existing walker.py findings (lines 18,101,760,985,1770); 0 new
uv run pytest -q
#   -> all pass, 7 skipped (no regressions; test_figma_walker low_confidence test still green)
```

New regression test: `tests/test_figma_tier1_routing.py::
test_catalog_hit_suppresses_low_confidence_warning` walks a 2-instance page
through a sub-threshold stub mapper and pins both arms in one walk — the
catalog-resolved instance (`1:2 → Button`) emits **no** warning, while the
un-cascadable instance (`1:3`) still does.

## Scope / non-goals

This is fix 1 of 4 from the analysis. Deliberately **not** done here:

- **Fix 2** — confidence floor on the `_element_for` mapper rung (a 0.0-conf
  fuzzy pick should fall through to `layout`/`<div>`).
- **Fix 3** — resolve the page-root `COMPONENT` + nav header to a shell/
  container instead of letting them fuzzy-map (`Pagination` / `NavBarLayout`).
  These are the 2 survivors above.
- **Fix 4** — `Input/Search` catalog cascade picking `Typography` over `Input`.
