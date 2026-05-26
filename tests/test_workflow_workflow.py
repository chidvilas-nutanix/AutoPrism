"""End-to-end tests for :class:`GenerateComponentWorkflow`.

We exercise the real Temporal workflow runtime via
:meth:`temporalio.testing.WorkflowEnvironment.start_time_skipping`,
which spins up a local Go-based test server (~1 second cold start
per test session). To keep tests hermetic, we register *stub*
activities under the same names the workflow looks up — that's
the canonical Temporal pattern for unit-testing workflow
orchestration logic without invoking real subprocesses.

What we lock down:

* **Fail-fast ordering**: typecheck → eslint → jest →
  playwright_axe, stop at first failure. The reflection prompt
  needs the *failing* validator to be the last one in the list.
* **All-passed terminates**: a fully-green iteration moves the
  workflow to ``final_state == "passed"`` and ``run()`` returns.
* **Max-iterations cap**: three consecutive failed submissions
  move the workflow to ``final_state == "failed"`` and reject
  any further submit.
* **SSIM is gated**: SSIM is invoked only when every subprocess
  validator passes AND ``figma_png_path`` is set on the input.
* **Status query**: ``status()`` reflects the current iteration
  and most-recent result at any point in the run.
* **Update return shape**: ``submit_candidate`` returns the
  :class:`CandidateResult` synchronously (the SOTA-named pattern
  from the Temporal Update launch blog).
"""

from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from prism_mcp.workflow import PRISM_TASK_QUEUE
from prism_mcp.workflow.activities import (
    CandidateInput,
    ServicesContext,
    SsimInput,
)
from prism_mcp.workflow.contracts import (
    CandidateResult,
    SsimVerdict,
    SubmitInput,
    ValidatorKind,
    ValidatorResult,
    WorkflowStartInput,
)
from prism_mcp.workflow.workflow import GenerateComponentWorkflow

# --------------------------------------------------------------------------
# Stub activity factories — each test composes its own set so the
# tests can override per-validator behaviour.
# --------------------------------------------------------------------------


def _stub_write_candidate_files():
    """Stub for :func:`write_candidate_files` — no filesystem touch."""

    @activity.defn(name="write_candidate_files")
    async def stub(input: CandidateInput) -> ServicesContext:
        return ServicesContext(
            services_root=input.services_root,
            component_name=input.component_name,
        )

    return stub


_VALIDATOR_ACTIVITY_NAMES: dict[ValidatorKind, str] = {
    ValidatorKind.dependencies: "check_dependencies_installed",
    ValidatorKind.typecheck: "run_typecheck",
    ValidatorKind.eslint: "run_eslint",
    ValidatorKind.jest: "run_jest",
    ValidatorKind.playwright_axe: "run_playwright_axe",
}
"""Validator kind → registered activity name.

The workflow looks activities up by their registered name, not
by the function object, so the stubs registered in tests must
match. ``ssim`` is intentionally omitted because the SSIM
activity has a different input/output type and is built via
:func:`_stub_ssim` instead.
"""


def _stub_validator(*, kind: ValidatorKind, exit_code: int, tail: str = ""):
    """Build a stub for one of the subprocess validator activities."""

    activity_name = _VALIDATOR_ACTIVITY_NAMES[kind]

    @activity.defn(name=activity_name)
    async def stub(ctx: ServicesContext) -> ValidatorResult:
        return ValidatorResult(
            kind=kind,
            exit_code=exit_code,
            stdout_tail=tail,
            stderr_tail="",
            duration_ms=1,
        )

    return stub


def _stub_ssim(score: float):
    """Build a stub for the SSIM activity."""

    @activity.defn(name="run_ssim_compare")
    async def stub(input: SsimInput) -> SsimVerdict:
        return SsimVerdict(score=score, region=None)

    return stub


def _ok_validator(kind: ValidatorKind):
    return _stub_validator(kind=kind, exit_code=0)


def _failing_validator(kind: ValidatorKind, tail: str = "failure detail"):
    return _stub_validator(kind=kind, exit_code=1, tail=tail)


def _all_ok_subprocess_validators():
    """Stubs for every subprocess validator, all returning ``ok``.

    Order matches :data:`prism_mcp.workflow.workflow._SUBPROCESS_CHAIN`
    — dependencies first, then the four scoped JS validators.
    """
    return [
        _stub_write_candidate_files(),
        _ok_validator(ValidatorKind.dependencies),
        _ok_validator(ValidatorKind.typecheck),
        _ok_validator(ValidatorKind.eslint),
        _ok_validator(ValidatorKind.jest),
        _ok_validator(ValidatorKind.playwright_axe),
    ]


# --------------------------------------------------------------------------
# Boilerplate: WorkflowEnvironment fixture (one per test session).
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def env():
    """Module-scoped :class:`WorkflowEnvironment`.

    Uses ``start_local()`` (a real Temporal dev server in a
    subprocess) instead of ``start_time_skipping()`` because the
    latter races on Update-result delivery when the update handler
    terminates the workflow in the same tick — exactly the
    AlphaCodium pattern we use here.

    ``pydantic_data_converter`` is required for Pydantic v2 models
    to round-trip cleanly across the Temporal payload boundary;
    without it Temporal falls back to a v1-style ``dict()`` call
    that drops typed fields silently.
    """
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
    ) as e:
        yield e


def _start_input(
    *,
    max_iterations: int = 3,
    figma_png_path: str | None = None,
) -> WorkflowStartInput:
    return WorkflowStartInput(
        component_name="ConfirmationModal",
        services_root="/tmp/fake-services",
        max_iterations=max_iterations,
        figma_png_path=figma_png_path,
    )


def _build_worker(
    env: WorkflowEnvironment,
    activities: list[object],
) -> Worker:
    """Worker factory that disables the deterministic sandbox.

    Temporal's workflow sandbox blocks ``platform.python_implementation``
    and other non-deterministic stdlib calls. Coverage's ``sysmon``
    backend uses exactly those calls at import time inside the
    sandbox, so collecting coverage on workflow code fails with
    ``RestrictedWorkflowAccessError``. Tests use
    :class:`UnsandboxedWorkflowRunner` to opt out of the sandbox
    so coverage can instrument workflow code freely; the workflow
    itself is still tested for behaviour, just not for sandbox
    safety (the production worker keeps the default sandbox).
    """
    return Worker(
        env.client,
        task_queue=PRISM_TASK_QUEUE,
        workflows=[GenerateComponentWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


# --------------------------------------------------------------------------
# Happy path: all validators pass on first iteration.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_passes_on_first_green_submission(env) -> None:
    """One submission, all validators green → final_state=passed."""
    async with _build_worker(env, _all_ok_subprocess_validators()):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )
        result = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )

        assert isinstance(result, CandidateResult)
        assert result.all_passed is True
        assert [v.kind for v in result.validators] == [
            ValidatorKind.dependencies,
            ValidatorKind.typecheck,
            ValidatorKind.eslint,
            ValidatorKind.jest,
            ValidatorKind.playwright_axe,
        ]

        final_status = await handle.result()
        assert final_status.final_state == "passed"
        assert final_status.iteration == 1


# --------------------------------------------------------------------------
# Slice-12 gap-closing preflight: dependencies failure short-circuits
# the entire chain before any JS validator can spawn.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_dependencies_failure_short_circuits_chain(
    env,
) -> None:
    """When ``check_dependencies_installed`` fails, *no* downstream
    validator runs and the candidate is marked failed with the
    remediation hint surfaced in the reflection prompt source.

    This is the slice-12 gap-closing guarantee: the operator (or
    the LLM) never sees a confusing ``ENOENT: tsc`` — they always
    see a structured "install JS deps first" error.
    """
    async with _build_worker(
        env,
        [
            _stub_write_candidate_files(),
            _failing_validator(
                ValidatorKind.dependencies,
                "Missing required JS validator binaries: tsc.",
            ),
            # These are *registered* so the workflow's activity
            # lookup succeeds, but the fail-fast chain must skip
            # them — the test asserts they were never invoked by
            # checking the validators list length below.
            _ok_validator(ValidatorKind.typecheck),
            _ok_validator(ValidatorKind.eslint),
            _ok_validator(ValidatorKind.jest),
            _ok_validator(ValidatorKind.playwright_axe),
        ],
    ):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(max_iterations=1),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )
        result = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )

        assert result.all_passed is False
        # Only the dependencies check ran — chain stopped on the
        # first non-ok validator per the fail-fast contract.
        assert [v.kind for v in result.validators] == [
            ValidatorKind.dependencies
        ]
        assert "Missing required JS" in result.validators[0].stdout_tail


# --------------------------------------------------------------------------
# Fail-fast: a failing typecheck must skip the later validators.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_fail_fast_stops_after_first_failure(env) -> None:
    """A typecheck failure must short-circuit the chain.

    The reflection prompt relies on the failing validator being
    the last entry in ``validators`` — that's the LLM's
    diagnostic anchor.
    """
    async with _build_worker(
        env,
        [
            _stub_write_candidate_files(),
            _ok_validator(ValidatorKind.dependencies),
            _failing_validator(ValidatorKind.typecheck, "TS2339"),
            _ok_validator(ValidatorKind.eslint),
            _ok_validator(ValidatorKind.jest),
            _ok_validator(ValidatorKind.playwright_axe),
        ],
    ):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(max_iterations=1),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )
        result = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )

        assert result.all_passed is False
        # Dependencies passed, then typecheck failed; eslint /
        # jest / playwright skipped by the fail-fast loop.
        assert [v.kind for v in result.validators] == [
            ValidatorKind.dependencies,
            ValidatorKind.typecheck,
        ]
        assert "TS2339" in result.validators[-1].stdout_tail


# --------------------------------------------------------------------------
# SSIM gating: only invoked when subprocess validators all pass
# AND figma_png_path is set on the start input.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_skips_ssim_when_no_figma_png(env) -> None:
    """``figma_png_path=None`` → ssim activity never invoked."""
    async with _build_worker(
        env,
        [*_all_ok_subprocess_validators(), _stub_ssim(score=0.0)],
    ):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(figma_png_path=None),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )
        result = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )
        # SSIM stub returns score=0 → would fail the run. Confirm
        # it was never called (the workflow still passes).
        assert result.ssim is None
        assert result.all_passed is True


@pytest.mark.asyncio
async def test_workflow_runs_ssim_when_figma_png_set(env) -> None:
    """``figma_png_path`` set → SSIM result attached to the candidate."""
    async with _build_worker(
        env,
        [*_all_ok_subprocess_validators(), _stub_ssim(score=0.97)],
    ):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(figma_png_path="/tmp/figma.png"),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )
        result = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )
        assert result.ssim is not None
        assert result.ssim.bucket == "pass"
        assert result.all_passed is True


@pytest.mark.asyncio
async def test_workflow_skips_ssim_when_subprocess_validator_failed(
    env,
) -> None:
    """Don't waste time on visual diff when typecheck already failed."""
    async with _build_worker(
        env,
        [
            _stub_write_candidate_files(),
            _ok_validator(ValidatorKind.dependencies),
            _failing_validator(ValidatorKind.typecheck),
            _ok_validator(ValidatorKind.eslint),
            _ok_validator(ValidatorKind.jest),
            _ok_validator(ValidatorKind.playwright_axe),
            _stub_ssim(score=0.99),
        ],
    ):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(figma_png_path="/tmp/figma.png", max_iterations=1),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )
        result = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )
        assert result.ssim is None
        assert result.all_passed is False


# --------------------------------------------------------------------------
# Max-iterations cap.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_caps_at_max_iterations(env) -> None:
    """After ``max_iterations`` failed submissions → final_state=failed.

    Subsequent submit_candidate calls fail with a TemporalApplicationError.
    """
    async with _build_worker(
        env,
        [
            _stub_write_candidate_files(),
            _ok_validator(ValidatorKind.dependencies),
            _failing_validator(ValidatorKind.typecheck),
            _ok_validator(ValidatorKind.eslint),
            _ok_validator(ValidatorKind.jest),
            _ok_validator(ValidatorKind.playwright_axe),
        ],
    ):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(max_iterations=2),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )
        # Two iterations, both fail.
        await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )
        last = await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<y/>"),
        )
        assert last.iteration == 2

        # Third submit must reject. Either the workflow rejects
        # while still running (``WorkflowUpdateFailedError``) or the
        # workflow has already terminated (``RPCError``). Both
        # outcomes correctly signal "no more submissions accepted"
        # — the precise exception depends on timing of the worker's
        # final tick vs the update arriving.
        with pytest.raises((WorkflowUpdateFailedError, RPCError)):
            await handle.execute_update(
                GenerateComponentWorkflow.submit_candidate,
                SubmitInput(jsx_code="<z/>"),
            )

        final = await handle.result()
        assert final.final_state == "failed"
        assert final.iteration == 2


# --------------------------------------------------------------------------
# Status query: reflects current state at any point.
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Slice-12.5 delivery-hint: terminal passing status carries an
# explicit "now call get_final_artefact" instruction so the agent
# loop is reminded to deliver the validated code into the user's
# actual project (the scratch dir is just the validator's cache).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_status_includes_delivery_hint_on_pass(env) -> None:
    """A passing workflow's terminal status must surface a hint
    pointing at ``get_final_artefact`` so the agent doesn't
    forget the delivery step.
    """
    async with _build_worker(env, _all_ok_subprocess_validators()):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )
        await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )

        final = await handle.result()
        assert final.final_state == "passed"
        assert "get_final_artefact" in final.delivery_hint
        assert final.component_name in final.delivery_hint
        # The terminal status echoes services_root so the agent
        # can drive get_final_artefact from just the workflow_id.
        assert final.services_root == "/tmp/fake-services"


@pytest.mark.asyncio
async def test_workflow_status_has_empty_delivery_hint_while_running(
    env,
) -> None:
    """Mid-flight queries return ``delivery_hint=""`` — only
    terminal-passed states fire the hint.
    """
    async with _build_worker(env, _all_ok_subprocess_validators()):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )

        initial = await handle.query(GenerateComponentWorkflow.status)
        assert initial.final_state == "running"
        assert initial.delivery_hint == ""

        # Drive to terminal so the workflow exits cleanly + the
        # worker context can shut down.
        await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )
        await handle.result()


@pytest.mark.asyncio
async def test_workflow_status_query_reflects_iteration(env) -> None:
    """``status()`` is callable while the workflow runs and after."""
    async with _build_worker(env, _all_ok_subprocess_validators()):
        handle = await env.client.start_workflow(
            GenerateComponentWorkflow.run,
            _start_input(),
            id=f"wf-{uuid.uuid4()}",
            task_queue=PRISM_TASK_QUEUE,
        )

        # Before any submission.
        initial = await handle.query(GenerateComponentWorkflow.status)
        assert initial.iteration == 0
        assert initial.final_state == "running"
        assert initial.last_result is None

        # After a passing submission.
        await handle.execute_update(
            GenerateComponentWorkflow.submit_candidate,
            SubmitInput(jsx_code="<x/>"),
        )
        after = await handle.query(GenerateComponentWorkflow.status)
        assert after.iteration == 1
        assert after.final_state == "passed"
        assert after.last_result is not None

        await handle.result()
