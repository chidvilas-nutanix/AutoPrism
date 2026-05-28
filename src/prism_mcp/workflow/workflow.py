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
        FigmaReferenceInput,
        RenderedExistsInput,
        ServicesContext,
        SsimInput,
        UpdateCompanionFilesInput,
        check_dependencies_installed,
        check_rendered_exists,
        materialise_figma_reference,
        run_eslint,
        run_jest,
        run_playwright_axe,
        run_ssim_compare,
        run_typecheck,
        update_companion_test_files,
        write_candidate_files,
    )
    from prism_mcp.workflow.contracts import (
        CandidateResult,
        SsimSkipReason,
        SsimVerdict,
        SubmitInput,
        UpdateCompanionTestsInput,
        UpdateCompanionTestsResult,
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
_FIGMA_MATERIALISE_TIMEOUT = timedelta(seconds=60)
"""How long to wait for a Figma reference download/decode.

Generous because Figma signed URLs can be slow on first hit
(the CDN serves them cold) and base64 payloads can be several
MB. We retry the activity (Temporal's default policy) on
transient failures.
"""

_RENDERED_EXISTS_TIMEOUT = timedelta(seconds=10)
"""How long to wait for a single ``Path.is_file()`` check.

Tight because the activity does one filesystem stat. Generous
enough to absorb a slow worker startup or a contended disk; not
so long that a wedged check would mask a real bug.
"""


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
        self._materialised_figma_path: str | None = None

    # ------------------------------------------------------------------
    # Workflow entrypoint.
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, input: WorkflowStartInput) -> WorkflowStatus:
        """Hold the workflow open until a terminal condition.

        We use :func:`workflow.wait_condition` so updates to
        ``self._final_state`` from inside :meth:`submit_candidate`
        immediately wake up this coroutine.

        Before waiting, if the start input carries any of the
        three Figma reference channels (path, URL, or base64),
        we materialise the PNG to disk via the
        ``materialise_figma_reference`` activity and cache the
        result path on ``self._materialised_figma_path``. Every
        SSIM iteration reuses that cached path — pay the
        download/decode cost once per workflow, not once per
        iteration.
        """
        self._input = input
        if input.has_figma_reference:
            ref_result = await workflow.execute_activity(
                materialise_figma_reference,
                FigmaReferenceInput(
                    figma_png_path=input.figma_png_path,
                    figma_png_url=input.figma_png_url,
                    figma_png_base64=input.figma_png_base64,
                ),
                start_to_close_timeout=_FIGMA_MATERIALISE_TIMEOUT,
            )
            self._materialised_figma_path = ref_result.path
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
        ssim, ssim_skip_reason = await self._maybe_run_ssim(validators)

        result = CandidateResult(
            iteration=self._iteration,
            component_name=self._input.component_name,
            validators=validators,
            ssim=ssim,
            ssim_skip_reason=ssim_skip_reason,
            figma_reference_present=self._materialised_figma_path is not None,
        )
        self._last_result = result
        self._update_final_state(result)
        return result

    # ------------------------------------------------------------------
    # Update handler — Cursor's update_companion_tests.
    # ------------------------------------------------------------------

    @workflow.update
    async def update_companion_tests(
        self, input: UpdateCompanionTestsInput
    ) -> UpdateCompanionTestsResult:
        """Refine the auto-scaffolded pwspec and/or jest spec.

        Used after the LLM has stabilised a component's behaviour
        and wants to upgrade the workflow's auto-scaffolds with
        real assertions (axe checks, visual regression, prop-driven
        renders). The refined tests take effect on the next
        ``submit_candidate`` call.

        This update *does not* re-run the validator chain — it
        just writes files to disk. That keeps each update's
        responsibility narrow: ``submit_candidate`` runs
        validators, ``update_companion_tests`` adjusts the test
        bodies they validate.

        Raises:
            ApplicationError: if the workflow has no input yet
                (called before ``submit_candidate`` seeded the
                scratch tree) or has already terminated.
        """
        await workflow.wait_condition(lambda: self._input is not None)
        assert self._input is not None
        if self._final_state != "running":
            raise ApplicationError(
                f"workflow already {self._final_state!r}; cannot accept "
                "more test refinements"
            )
        result = await workflow.execute_activity(
            update_companion_test_files,
            UpdateCompanionFilesInput(
                services_root=self._input.services_root,
                component_name=self._input.component_name,
                pwspec_code=input.pwspec_code,
                spec_code=input.spec_code,
            ),
            start_to_close_timeout=_FILE_IO_TIMEOUT,
        )
        return UpdateCompanionTestsResult(
            component_name=result.component_name,
            wrote_pwspec=result.wrote_pwspec,
            wrote_spec=result.wrote_spec,
            pwspec_path=result.pwspec_path,
            spec_path=result.spec_path,
        )

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
        """Write the JSX + (optional) pwspec/spec to the scratch tree.

        The activity auto-scaffolds pwspec + jest spec at iteration
        1 if neither ``companion_test_code`` nor ``companion_spec_code``
        are supplied; subsequent iterations preserve any LLM
        refinements written via ``update_companion_tests``.
        """
        assert self._input is not None
        return await workflow.execute_activity(
            write_candidate_files,
            CandidateInput(
                services_root=self._input.services_root,
                component_name=self._input.component_name,
                jsx_code=input.jsx_code,
                companion_test_code=input.companion_test_code,
                companion_spec_code=input.companion_spec_code,
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
    ) -> tuple[SsimVerdict | None, SsimSkipReason | None]:
        """Invoke SSIM only when:

        * every subprocess validator passed (otherwise the visual
          diff is meaningless — the component might not even render);
        * the workflow has a materialised Figma reference path
          (resolved at workflow start from any of
          ``figma_png_path`` / ``figma_png_url`` / ``figma_png_base64``);
        * the templated rendered PNG actually exists on disk
          (verified via :func:`check_rendered_exists` so a missing
          screenshot becomes a clean
          ``ssim_skip_reason="rendered_unavailable"`` instead of a
          ``FileNotFoundError`` from inside the SSIM math).

        The Figma reference is resolved exactly once per workflow
        in :meth:`run` and cached on ``self._materialised_figma_path``,
        so this method never pays the download/decode cost itself.

        Returns:
            tuple[SsimVerdict | None, SsimSkipReason | None]: a
            two-tuple where exactly one element is non-``None``
            unless SSIM was bypassed for an existing reason:

            * ``(verdict, None)`` — SSIM ran, see ``verdict``.
            * ``(None, "rendered_unavailable")`` — SSIM was
              attempted but the rendered PNG was missing.
            * ``(None, None)`` — SSIM was not attempted at all
              (no Figma reference, or an earlier validator failed).
              The workflow already conveys "no Figma reference"
              via ``CandidateResult.figma_reference_present``;
              an earlier validator failure is encoded directly in
              ``CandidateResult.validators``. Neither needs its
              own skip reason today.
        """
        assert self._input is not None
        if self._materialised_figma_path is None:
            return None, None
        if not all(v.ok for v in validators):
            return None, None
        rendered_png_path = self._input.rendered_png_path_template.format(
            services_root=self._input.services_root,
            component_name=self._input.component_name,
        )
        exists_result = await workflow.execute_activity(
            check_rendered_exists,
            RenderedExistsInput(rendered_png_path=rendered_png_path),
            start_to_close_timeout=_RENDERED_EXISTS_TIMEOUT,
        )
        if not exists_result.exists:
            return None, "rendered_unavailable"
        verdict = await workflow.execute_activity(
            run_ssim_compare,
            SsimInput(
                figma_png_path=self._materialised_figma_path,
                rendered_png_path=rendered_png_path,
            ),
            start_to_close_timeout=_SSIM_TIMEOUT,
        )
        return verdict, None

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
