"""The slice-12 :class:`GenerateComponentWorkflow` itself.

This is the durable orchestration on top of the slice-12 Activities:

* :meth:`run` is the workflow entrypoint. It waits — via
  :func:`temporalio.workflow.wait_condition` — until the workflow
  hits a terminal state (``passed``, ``failed``, or ``cancelled``)
  and returns the final :class:`WorkflowStatus`.
* :meth:`submit_candidate` is a
  :func:`temporalio.workflow.update` handler — the SOTA-named
  primitive from Temporal's 2024 launch blog. The Cursor agent
  loop calls it once per iteration with new JSX (+ optional
  pwspec) and gets back a synchronous :class:`CandidateResult`.
* :meth:`status` is a :func:`temporalio.workflow.query` handler —
  read-only snapshot useful for ``get_component_status``.

Determinism / replay safety
---------------------------

Temporal workflow code must replay deterministically on worker
restarts: same inputs + same Activity results → same code path.
That means **no module-level imports** of :mod:`subprocess`,
:mod:`pathlib`, :mod:`time`, :mod:`random`. We sidestep all four:

* String paths are built with ``str.format`` — pure, deterministic.
* All wall-clock measurement happens inside Activities (which get
  the result recorded in the event log).
* Random IDs are the caller's responsibility (Temporal supplies
  the workflow ID).

Iteration model
---------------

Per the slice-12 AlphaCodium plan:

1. Cursor calls ``submit_candidate`` with its first JSX + AI-test.
2. We write the candidate files, then run validators in fail-fast
   order: typecheck → eslint → jest → playwright_axe.
3. If they all pass AND ``figma_png_path`` is set, run the SSIM
   activity.
4. Return the :class:`CandidateResult`. If ``all_passed``, the
   workflow terminates (``final_state="passed"``). If we've now
   exhausted ``max_iterations``, terminate as ``"failed"``.
5. Otherwise, wait for the next ``submit_candidate``.

The reflection prompt is computed *outside* the workflow (in the
MCP tool layer) so the workflow stays minimal — it's the LLM
that needs the prompt, not the orchestrator.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    # Pydantic + the contract module are pure-Python and safe to
    # import inside the workflow sandbox. The
    # ``imports_passed_through`` block tells Temporal not to wrap
    # them with the sandbox import-hook (which can mis-classify
    # third-party packages and slow down imports).
    from prism_mcp.workflow.activities import (
        CandidateInput,
        ServicesContext,
        SsimInput,
        check_dependencies_installed,
        run_eslint,
        run_jest,
        run_playwright_axe,
        run_ssim_compare,
        run_typecheck,
        write_candidate_files,
    )
    from prism_mcp.workflow.contracts import (
        CandidateResult,
        SsimVerdict,
        SubmitInput,
        ValidatorKind,
        ValidatorResult,
        WorkflowStartInput,
        WorkflowStatus,
        build_delivery_hint,
    )


# --------------------------------------------------------------------------
# Activity timeouts. Conservative enough for real npm scripts (some
# Playwright runs take ~5 minutes) but not infinite so a hung
# subprocess can't wedge the workflow.
# --------------------------------------------------------------------------

_FILE_IO_TIMEOUT = timedelta(seconds=30)
_DEPENDENCIES_TIMEOUT = timedelta(seconds=15)
_FAST_VALIDATOR_TIMEOUT = timedelta(minutes=2)
_SLOW_VALIDATOR_TIMEOUT = timedelta(minutes=15)
_SSIM_TIMEOUT = timedelta(seconds=60)


# --------------------------------------------------------------------------
# Two-tier validator chain (SOTA "collect all feedback per iteration").
#
# Tier 1 — Hard gates: must pass before the rest of the chain runs at
# all. Today the only member is ``dependencies``, which is a few
# filesystem stat()s and surfaces an actionable "run npm install"
# remediation. Without it, every downstream subprocess would fail
# with ``ENOENT`` and the LLM would get a confusing "tsc not found"
# instead of "install the JS deps".
#
# Tier 2 — Informational validators: typecheck, eslint, jest,
# playwright + axe. The SOTA AlphaCodium pattern is to **run them all
# regardless of any one's outcome** so the LLM sees the full panel of
# errors in a single iteration. Strict fail-fast (the old behaviour)
# starved the LLM of independent signals — e.g. an eslint formatting
# issue would never surface until after typecheck went green, costing
# an extra iteration round-trip per validator. The new policy
# trades a small amount of wasted compute (running jest/playwright
# against syntactically broken code is mostly noise) for far fewer
# iteration cycles in practice.
#
# Why keep ``dependencies`` as a hard gate and demote ``typecheck``?
# Because ``dependencies`` failures produce *cascading binary-not-found*
# errors that are not actionable feedback (the LLM cannot ``npm install``
# from inside its own generated code). Typecheck failures, on the other
# hand, produce TS error messages the LLM *can* act on — and eslint /
# jest / playwright add orthogonal signals (style, runtime, a11y) that
# are useful even when types are broken.
#
# SSIM gating is unchanged: it only runs when *every* Tier-2 validator
# passes (a broken render is meaningless to visually diff).
# --------------------------------------------------------------------------


_HARD_GATE_VALIDATORS: list[tuple[ValidatorKind, object, timedelta]] = [
    (
        ValidatorKind.dependencies,
        check_dependencies_installed,
        _DEPENDENCIES_TIMEOUT,
    ),
]
"""Validators whose failure short-circuits the iteration immediately.

Order is execution order. The chain stops at the first non-zero
exit so the LLM gets the *most-actionable* error without noise
from the rest. Today only ``dependencies`` qualifies; future
candidates could be a binary-version-mismatch probe or a license
check.
"""


_INFORMATIONAL_VALIDATORS: list[tuple[ValidatorKind, object, timedelta]] = [
    (ValidatorKind.typecheck, run_typecheck, _FAST_VALIDATOR_TIMEOUT),
    (ValidatorKind.eslint, run_eslint, _FAST_VALIDATOR_TIMEOUT),
    (ValidatorKind.jest, run_jest, _FAST_VALIDATOR_TIMEOUT),
    (
        ValidatorKind.playwright_axe,
        run_playwright_axe,
        _SLOW_VALIDATOR_TIMEOUT,
    ),
]
"""Validators that always run when the hard gates passed.

Each contributes an independent error signal (types / style /
runtime / a11y+visual). All four run regardless of any one's
outcome so the reflection prompt aggregates *all* findings into
a single LLM round. This is the AlphaCodium ``Iterate on tests``
shape applied to a multi-validator chain.
"""


@workflow.defn
class GenerateComponentWorkflow:
    """Durable orchestration for the AlphaCodium iteration loop."""

    def __init__(self) -> None:
        self._input: WorkflowStartInput | None = None
        self._iteration: int = 0
        self._last_result: CandidateResult | None = None
        self._final_state: str = "running"

    # ------------------------------------------------------------------
    # Workflow entrypoint.
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, input: WorkflowStartInput) -> WorkflowStatus:
        """Hold the workflow open until a terminal condition.

        We use :func:`workflow.wait_condition` so updates to
        ``self._final_state`` from inside :meth:`submit_candidate`
        immediately wake up this coroutine.
        """
        self._input = input
        try:
            await workflow.wait_condition(
                lambda: self._final_state != "running",
                timeout=timedelta(seconds=input.max_wait_seconds),
            )
        except TimeoutError:
            self._final_state = "cancelled"
        return self._snapshot()

    # ------------------------------------------------------------------
    # Update handler — Cursor's submit_candidate.
    # ------------------------------------------------------------------

    @workflow.update
    async def submit_candidate(self, input: SubmitInput) -> CandidateResult:
        """Run one iteration of validators against the supplied code.

        Raises:
            ApplicationError: if the workflow is no longer accepting
                submissions (already terminal, or max iterations
                exhausted).
        """
        # Updates can arrive at the worker before ``run()`` has had
        # a chance to populate ``self._input`` — Temporal delivers
        # the update message and the workflow's first task in
        # parallel. The canonical Python-SDK pattern is to wait
        # here for the workflow to finish its own initialization
        # before processing the update.
        await workflow.wait_condition(lambda: self._input is not None)
        assert self._input is not None
        if self._final_state != "running":
            raise ApplicationError(
                f"workflow already {self._final_state!r}; cannot accept "
                "more submissions"
            )
        if self._iteration >= self._input.max_iterations:
            raise ApplicationError(
                f"max iterations ({self._input.max_iterations}) exhausted"
            )

        self._iteration += 1
        ctx = await self._write_candidate(input)
        validators = await self._run_subprocess_chain(ctx)
        ssim = await self._maybe_run_ssim(validators)

        result = CandidateResult(
            iteration=self._iteration,
            component_name=self._input.component_name,
            validators=validators,
            ssim=ssim,
        )
        self._last_result = result
        self._update_final_state(result)
        return result

    # ------------------------------------------------------------------
    # Query handler — read-only status snapshot.
    # ------------------------------------------------------------------

    @workflow.query
    def status(self) -> WorkflowStatus:
        """Return the workflow's current state. Safe to poll."""
        return self._snapshot()

    # ------------------------------------------------------------------
    # Internal helpers — all deterministic, all called from inside
    # the workflow sandbox so they cannot touch the filesystem or
    # the network. The heavy lifting is in the Activities.
    # ------------------------------------------------------------------

    async def _write_candidate(self, input: SubmitInput) -> ServicesContext:
        """Write the JSX + (optional) pwspec to the scratch tree."""
        assert self._input is not None
        return await workflow.execute_activity(
            write_candidate_files,
            CandidateInput(
                services_root=self._input.services_root,
                component_name=self._input.component_name,
                jsx_code=input.jsx_code,
                companion_test_code=input.companion_test_code,
            ),
            start_to_close_timeout=_FILE_IO_TIMEOUT,
        )

    async def _run_subprocess_chain(
        self, ctx: ServicesContext
    ) -> list[ValidatorResult]:
        """Run validators with two-tier semantics (hard gates + informational).

        Phase 1 — Hard gates. Each runs sequentially; the first
        failure short-circuits the entire iteration so the LLM
        gets a single, actionable remediation hint instead of a
        wall of cascading ``ENOENT`` errors.

        Phase 2 — Informational validators. Every validator runs
        regardless of its predecessors' outcome. This is the SOTA
        AlphaCodium-flavoured pattern: surface the *full* panel of
        errors per iteration so the LLM can fix multiple
        orthogonal issues in a single regeneration step.
        """
        validators: list[ValidatorResult] = []
        for _kind, activity_fn, timeout in _HARD_GATE_VALIDATORS:
            result = await workflow.execute_activity(
                activity_fn,
                ctx,
                start_to_close_timeout=timeout,
            )
            validators.append(result)
            if not result.ok:
                # Hard-gate failure aborts the iteration. The LLM
                # cannot fix a missing ``node_modules`` from inside
                # its own JSX, so running downstream validators
                # would just produce noise.
                return validators

        for _kind, activity_fn, timeout in _INFORMATIONAL_VALIDATORS:
            result = await workflow.execute_activity(
                activity_fn,
                ctx,
                start_to_close_timeout=timeout,
            )
            validators.append(result)
            # Intentionally no ``break`` here — collect every
            # validator's signal so the reflection prompt
            # aggregates them.
        return validators

    async def _maybe_run_ssim(
        self, validators: list[ValidatorResult]
    ) -> SsimVerdict | None:
        """Invoke SSIM only when:

        * every subprocess validator passed (otherwise the visual
          diff is meaningless — the component might not even render);
        * the start input included a ``figma_png_path`` (otherwise
          there's nothing to compare to).
        """
        assert self._input is not None
        if self._input.figma_png_path is None:
            return None
        if not all(v.ok for v in validators):
            return None
        rendered_png_path = self._input.rendered_png_path_template.format(
            services_root=self._input.services_root,
            component_name=self._input.component_name,
        )
        return await workflow.execute_activity(
            run_ssim_compare,
            SsimInput(
                figma_png_path=self._input.figma_png_path,
                rendered_png_path=rendered_png_path,
            ),
            start_to_close_timeout=_SSIM_TIMEOUT,
        )

    def _update_final_state(self, result: CandidateResult) -> None:
        """Promote ``_final_state`` after a candidate evaluation."""
        assert self._input is not None
        if result.all_passed:
            self._final_state = "passed"
            return
        if self._iteration >= self._input.max_iterations:
            self._final_state = "failed"

    def _snapshot(self) -> WorkflowStatus:
        """Materialise the current state as a :class:`WorkflowStatus`.

        Populates the slice-12.5 ``services_root`` + ``delivery_hint``
        fields so a freshly-polling agent has everything it needs to
        call :func:`get_final_artefact` without having to remember
        the original start input.
        """
        component_name = (
            self._input.component_name if self._input else "<unstarted>"
        )
        services_root = self._input.services_root if self._input else ""
        max_iterations = self._input.max_iterations if self._input else 0
        workflow_id = workflow.info().workflow_id
        # Delivery hint only fires when the workflow has actually
        # *passed* — failed / cancelled runs leave the scratch dir in
        # an indeterminate state and the agent should iterate or
        # cancel, not deliver.
        delivery_hint = (
            build_delivery_hint(
                workflow_id=workflow_id,
                component_name=component_name,
            )
            if self._final_state == "passed"
            else ""
        )
        return WorkflowStatus(
            workflow_id=workflow_id,
            component_name=component_name,
            services_root=services_root,
            iteration=self._iteration,
            max_iterations=max_iterations,
            last_result=self._last_result,
            final_state=self._final_state,  # type: ignore[arg-type]
            delivery_hint=delivery_hint,
        )
