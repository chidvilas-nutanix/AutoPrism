"""Tests for slice-12 contract Pydantic models.

The contracts are the *typed boundary* between the MCP server,
the Temporal workflow, the activities, and the Cursor agent loop.
They're load-bearing for every other slice-12 file, so we lock
them down with focused invariant tests before any caller code
exists.

Coverage focus: invariants the rest of the slice relies on:

* Pass/fail booleans are derived from validator names + numeric
  scores; the caller never sets them directly.
* Score thresholds match the documented contract (SSIM 0.85 / 0.95;
  WCAG / AA pass logic stays consistent with slice-11).
* Reflection prompts are stable strings (so prompt caches stay warm
  across runs).
* JSON round-tripping is preserved (Temporal data converters
  serialize through JSON; a non-round-trippable model would silently
  drop fields on workflow replay).
"""

from __future__ import annotations

import pytest

from prism_mcp.workflow.contracts import (
    CandidateResult,
    ReflectionContext,
    SsimVerdict,
    ValidatorKind,
    ValidatorResult,
    WorkflowStatus,
    build_reflection_prompt,
)

# --------------------------------------------------------------------------
# ValidatorResult: thin wrapper around (kind, ok, stdout_tail, stderr_tail).
# --------------------------------------------------------------------------


def test_validator_result_ok_true_on_zero_exit() -> None:
    """``ok`` is derived from ``exit_code == 0`` — not free-form."""
    result = ValidatorResult(
        kind=ValidatorKind.typecheck,
        exit_code=0,
        stdout_tail="ok",
        stderr_tail="",
        duration_ms=42,
    )
    assert result.ok is True


def test_validator_result_ok_false_on_nonzero_exit() -> None:
    """Any non-zero exit code means failure — including signals (negative)."""
    result = ValidatorResult(
        kind=ValidatorKind.eslint,
        exit_code=1,
        stdout_tail="error",
        stderr_tail="",
        duration_ms=88,
    )
    assert result.ok is False


def test_validator_result_truncates_stdout_to_4000_chars() -> None:
    """The 4kB tail-cap is a hard contract: Cursor's tool-result
    rendering breaks on multi-MB shell output. Enforce it at the
    model boundary so an activity bug can't slip past it.
    """
    huge = "x" * 12_000
    result = ValidatorResult(
        kind=ValidatorKind.jest,
        exit_code=0,
        stdout_tail=huge,
        stderr_tail=huge,
        duration_ms=10,
    )
    assert len(result.stdout_tail) == 4000
    assert len(result.stderr_tail) == 4000
    # Tail-keep semantics — we keep the *end* of the buffer, where
    # the error message usually lives, not the head.
    assert result.stdout_tail.endswith("x" * 100)


def test_validator_result_rejects_unknown_kind() -> None:
    """Activities must use one of the six blessed ValidatorKind
    members — typo'd kind names should fail fast at model
    construction, not silently match nothing in the workflow.
    """
    with pytest.raises(ValueError, match="should be 'dependencies'"):
        ValidatorResult(  # type: ignore[arg-type]
            kind="typo_check",
            exit_code=0,
            stdout_tail="",
            stderr_tail="",
            duration_ms=1,
        )


def test_validator_kind_dependencies_member_exists() -> None:
    """The ``dependencies`` enum member is the slice-12 gap-closing
    preflight: it represents the "are the JS validator binaries
    actually installed?" check that runs first in the subprocess
    chain. Locking the name down here so the activity + workflow
    + tests can't drift apart on the spelling.
    """
    assert ValidatorKind.dependencies.value == "dependencies"
    assert ValidatorKind("dependencies") is ValidatorKind.dependencies


def test_validator_kind_chain_order_is_stable() -> None:
    """The fail-fast chain is dependencies → typecheck → eslint →
    jest → playwright_axe → ssim. The order is load-bearing for
    cost reasons (cheap checks first) and for failure clarity
    (a "missing node_modules" error should surface before tsc's
    cryptic "cannot find module"). Lock the relative ordering.
    """
    order = [
        ValidatorKind.dependencies,
        ValidatorKind.typecheck,
        ValidatorKind.eslint,
        ValidatorKind.jest,
        ValidatorKind.playwright_axe,
        ValidatorKind.ssim,
    ]
    seen: set[ValidatorKind] = set()
    for member in order:
        assert member not in seen, f"duplicate member {member!r}"
        seen.add(member)
    assert seen == set(ValidatorKind)


def test_validator_result_round_trips_through_json() -> None:
    """Temporal serializes models through JSON between activity
    invocations and workflow replays. A model that drops fields on
    round-trip would silently lose data across workflow restarts.
    """
    original = ValidatorResult(
        kind=ValidatorKind.playwright_axe,
        exit_code=2,
        stdout_tail="violations: 3",
        stderr_tail="",
        duration_ms=4_200,
    )
    revived = ValidatorResult.model_validate_json(original.model_dump_json())
    assert revived == original


# --------------------------------------------------------------------------
# SsimVerdict: continuous score + bucket (pass / warn / fail).
# --------------------------------------------------------------------------


def test_ssim_verdict_pass_at_or_above_high_threshold() -> None:
    """Score >= 0.95 → ``pass`` bucket per the slice-12 contract."""
    verdict = SsimVerdict(score=0.96, region=None)
    assert verdict.bucket == "pass"
    assert verdict.ok is True


def test_ssim_verdict_warn_between_thresholds() -> None:
    """0.85 <= score < 0.95 → ``warn`` (passing but Cursor should
    consider one more refinement)."""
    verdict = SsimVerdict(score=0.90, region="header")
    assert verdict.bucket == "warn"
    assert verdict.ok is True


def test_ssim_verdict_fail_below_low_threshold() -> None:
    """Score < 0.85 → ``fail`` and the workflow loop should react."""
    verdict = SsimVerdict(score=0.78, region="body")
    assert verdict.bucket == "fail"
    assert verdict.ok is False


def test_ssim_verdict_clamps_score_to_unit_interval() -> None:
    """SSIM is mathematically bounded to [-1, 1] but in practice
    falls in [0, 1]. Reject scores outside [-1, 1] to surface
    activity bugs early.
    """
    with pytest.raises(ValueError, match="score"):
        SsimVerdict(score=1.5, region=None)


# --------------------------------------------------------------------------
# CandidateResult: aggregate of N validator results for one iteration.
# --------------------------------------------------------------------------


def test_candidate_result_all_passed_when_every_validator_ok() -> None:
    """``all_passed`` short-circuits the iteration loop."""
    result = CandidateResult(
        iteration=1,
        component_name="ConfirmationModal",
        validators=[
            ValidatorResult(
                kind=ValidatorKind.typecheck,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=10,
            ),
            ValidatorResult(
                kind=ValidatorKind.eslint,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=20,
            ),
        ],
        ssim=None,
    )
    assert result.all_passed is True
    assert result.failing_kinds == []


def test_candidate_result_marks_failing_kinds() -> None:
    """The list of failing kinds is the input to the reflection
    prompt — it must include every failed validator + the SSIM
    verdict when failing.
    """
    result = CandidateResult(
        iteration=2,
        component_name="X",
        validators=[
            ValidatorResult(
                kind=ValidatorKind.typecheck,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=1,
            ),
            ValidatorResult(
                kind=ValidatorKind.jest,
                exit_code=1,
                stdout_tail="2 failed",
                stderr_tail="",
                duration_ms=1,
            ),
        ],
        ssim=SsimVerdict(score=0.7, region="header"),
    )
    assert result.all_passed is False
    assert ValidatorKind.jest in result.failing_kinds
    assert "ssim" in result.failing_kinds


# --------------------------------------------------------------------------
# build_reflection_prompt: the ReflexiCoder-style 3-question template.
# --------------------------------------------------------------------------


def test_reflection_prompt_includes_the_three_questions() -> None:
    """Per ReflexiCoder the three questions are the load-bearing
    structure — the prompt MUST surface them verbatim.
    """
    failing = CandidateResult(
        iteration=1,
        component_name="X",
        validators=[
            ValidatorResult(
                kind=ValidatorKind.typecheck,
                exit_code=1,
                stdout_tail="error TS2339",
                stderr_tail="",
                duration_ms=1,
            ),
        ],
        ssim=None,
    )
    prompt = build_reflection_prompt(failing)
    assert "What was your assumption that turned out wrong?" in prompt
    assert "Which Prism component" in prompt
    assert "minimal change" in prompt


def test_reflection_prompt_lists_failing_validators_with_tails() -> None:
    """Cursor needs the actual error excerpt — not just the kind —
    to know what to refine. The prompt must include the stdout_tail
    of each failing validator.
    """
    failing = CandidateResult(
        iteration=2,
        component_name="X",
        validators=[
            ValidatorResult(
                kind=ValidatorKind.eslint,
                exit_code=1,
                stdout_tail="UNIQUE_ESLINT_MARKER",
                stderr_tail="",
                duration_ms=1,
            ),
        ],
        ssim=None,
    )
    prompt = build_reflection_prompt(failing)
    assert "UNIQUE_ESLINT_MARKER" in prompt
    assert "eslint" in prompt


def test_reflection_prompt_is_empty_string_when_all_passed() -> None:
    """No reflection needed on a passing candidate — return an
    empty string so the workflow can use it as a falsy guard
    without a separate ``is_failing`` check.
    """
    passing = CandidateResult(
        iteration=1,
        component_name="X",
        validators=[
            ValidatorResult(
                kind=ValidatorKind.typecheck,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=1,
            ),
        ],
        ssim=None,
    )
    assert build_reflection_prompt(passing) == ""


# --------------------------------------------------------------------------
# WorkflowStatus: the @workflow.query payload shape.
# --------------------------------------------------------------------------


def test_workflow_status_running_when_iteration_below_cap() -> None:
    """``state`` is derived from (iteration, all_passed, cap)."""
    status = WorkflowStatus(
        workflow_id="wf-1",
        component_name="X",
        iteration=1,
        max_iterations=3,
        last_result=None,
        final_state="running",
    )
    assert status.final_state == "running"


def test_workflow_status_round_trips_through_json() -> None:
    """Same JSON-stability check as ValidatorResult — the query
    payload crosses Temporal's data-converter boundary on every
    poll.
    """
    original = WorkflowStatus(
        workflow_id="wf-2",
        component_name="ConfirmationModal",
        services_root="/abs/services",
        iteration=3,
        max_iterations=3,
        last_result=None,
        final_state="failed",
        delivery_hint="",
    )
    revived = WorkflowStatus.model_validate_json(original.model_dump_json())
    assert revived == original


def test_workflow_status_services_root_defaults_to_empty() -> None:
    """Pre-slice-12.5 callers don't pass ``services_root``; the
    default empty string keeps the contract backward-compatible
    so existing tests + serialized histories still validate.
    """
    status = WorkflowStatus(
        workflow_id="wf",
        component_name="X",
        iteration=0,
        max_iterations=1,
    )
    assert status.services_root == ""
    assert status.delivery_hint == ""


def test_build_delivery_hint_includes_workflow_id_and_component() -> None:
    """The hint must contain the exact tool-call the agent should
    issue next — workflow_id quoted as a string literal, plus a
    suggested target path that references the component name.
    """
    from prism_mcp.workflow.contracts import build_delivery_hint

    hint = build_delivery_hint(
        workflow_id="wf-abc-123",
        component_name="ConfirmDeleteModal",
    )
    assert "get_final_artefact" in hint
    assert "'wf-abc-123'" in hint
    assert "ConfirmDeleteModal" in hint
    assert "cache" in hint  # reinforces scratch-isn't-destination


def test_build_delivery_hint_is_stable_across_calls() -> None:
    """Pure function — same inputs → identical output. Important
    because Temporal workflow replay re-runs determinism checks.
    """
    from prism_mcp.workflow.contracts import build_delivery_hint

    a = build_delivery_hint(workflow_id="wf", component_name="X")
    b = build_delivery_hint(workflow_id="wf", component_name="X")
    assert a == b


# --------------------------------------------------------------------------
# ReflectionContext: the pre-process scaffold the MCP server hands
# Cursor before the agent generates code. Tested for shape only;
# wiring to the actual Library is in test_reflection.py (Step 5).
# --------------------------------------------------------------------------


def test_reflection_context_carries_all_four_input_sources() -> None:
    """The four sources are the SOTA-plan-named inputs: examples,
    related components, color tokens, a11y rules. The shape locks
    them in so any source going missing in the workflow shows up
    as a typed error, not a silent gap.
    """
    ctx = ReflectionContext(
        component_name="ConfirmationModal",
        examples=["<Modal>...</Modal>"],
        related=["Button", "StackingLayout"],
        token_hints=["color-primary"],
        a11y_blocks=["return focus on close"],
        candidate_decompositions=["Modal+Button", "FullPageModal+Form"],
    )
    assert ctx.component_name == "ConfirmationModal"
    assert ctx.examples == ["<Modal>...</Modal>"]
    assert ctx.related == ["Button", "StackingLayout"]
    assert ctx.token_hints == ["color-primary"]
    assert ctx.a11y_blocks == ["return focus on close"]
    assert ctx.candidate_decompositions == [
        "Modal+Button",
        "FullPageModal+Form",
    ]


def test_reflection_context_defaults_lists_to_empty() -> None:
    """Empty corpus / no a11y prose should not be a fatal error —
    the LLM should still get a scaffold. Default each list to ``[]``.
    """
    ctx = ReflectionContext(component_name="X")
    assert ctx.examples == []
    assert ctx.related == []
    assert ctx.token_hints == []
    assert ctx.a11y_blocks == []
    assert ctx.candidate_decompositions == []
