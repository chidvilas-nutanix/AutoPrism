"""Typed contracts shared between activities, workflow, and MCP tools.

The Slice 12 "validation loop" crosses three runtime boundaries:

1. **MCP tool → Workflow**: Cursor calls ``start_generate_component``
   or ``submit_candidate``; the MCP server hands the payload to a
   Temporal client which forwards it across the network.
2. **Workflow → Activity**: deterministic workflow code calls a
   side-effectful activity (``run_typecheck`` etc.) via the
   activity stub.
3. **Activity → Workflow**: results come back through Temporal's
   data converter, which serialises everything through JSON.

Every model below crosses at least one of those boundaries, so each
must round-trip through JSON without losing fields, and must be
constructable from a single ``model_validate`` call (no setter
sequences). We use Pydantic v2 ``ConfigDict(extra="forbid")`` to
catch typos at the schema boundary instead of at runtime.

Why a separate module
---------------------

Temporal's workflow sandbox rejects modules that perform
non-deterministic imports (``time``, ``random``, ``pathlib`` at
module level). Keeping contracts in a pure-Pydantic module means
``workflow.py`` can ``from .contracts import ...`` safely.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Tunables — single source of truth so workflow + tests share constants.
# --------------------------------------------------------------------------

MAX_TAIL_BYTES = 4_000
"""Hard cap on how many bytes of stdout/stderr we keep per validator.

Cursor's tool-result rendering breaks on multi-MB shell output.
4 KB is enough to hold the *tail* of a typical npm-script log — the
error message usually lives in the last few hundred lines.
"""

SSIM_PASS_THRESHOLD = 0.95
"""SSIM score at-or-above which we report ``pass``.

Per the screenshot-testing-2026 survey: ``>= 0.95`` is "virtually
identical" — well past the threshold where humans would notice
differences in a UI snapshot.
"""

SSIM_WARN_THRESHOLD = 0.85
"""SSIM score at-or-above which we report ``warn`` (passing but
worth one more refinement). Below this we report ``fail`` and the
workflow loop reacts.
"""

DEFAULT_MAX_ITERATIONS = 3
"""AlphaCodium's ablation showed gains plateau by iteration 3-4 on
CodeContests. Three is also the demo sweet spot — enough to show
iteration, short enough that the audience doesn't lose patience.
"""

DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 1_800
"""Maximum wall-clock we'll wait for the agent to drive a workflow
to a terminal state. 30 minutes is generous — most candidates
resolve in 3-5 minutes — but a hung Cursor session can wedge a
workflow indefinitely without this guard.
"""


class ValidatorKind(StrEnum):
    """Enumeration of the six validators the workflow knows about.

    Implemented as a :class:`enum.StrEnum` so JSON serialisation
    produces plain strings (``"typecheck"`` not ``"ValidatorKind.typecheck"``).

    Order matters: members are declared in the same fail-fast
    sequence the workflow executes them in. The leading
    ``dependencies`` check guards against the most common
    operator-error — running the demo before ``npm install`` has
    populated ``services/node_modules``. Without it, every other
    validator returns ``ENOENT`` from spawn and the LLM gets a
    confusing "tsc not found" instead of "install the JS deps".
    """

    dependencies = "dependencies"
    typecheck = "typecheck"
    eslint = "eslint"
    jest = "jest"
    playwright_axe = "playwright_axe"
    ssim = "ssim"


# --------------------------------------------------------------------------
# ValidatorResult — one row of feedback from one activity.
# --------------------------------------------------------------------------


class ValidatorResult(BaseModel):
    """The structured output of a single validator activity.

    Args:
        kind (ValidatorKind): which validator produced this row.
        exit_code (int): the subprocess exit code (or a synthetic
            non-zero value for the SSIM validator on failure).
        stdout_tail (str): the tail of standard output, hard-capped
            at :data:`MAX_TAIL_BYTES` so the workflow event log
            stays small. Use the tail (not the head) because the
            error message is almost always at the end of an npm
            script's output.
        stderr_tail (str): same shape for standard error.
        duration_ms (int): wall-clock duration. Surfaced to the LLM
            so it can reason about which validator is the bottleneck
            during iteration.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ValidatorKind
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_ms: int

    @field_validator("stdout_tail", "stderr_tail")
    @classmethod
    def _truncate_tail(cls, value: str) -> str:
        """Keep only the trailing :data:`MAX_TAIL_BYTES` characters.

        Centralising the truncation here means an activity author
        cannot forget it: every :class:`ValidatorResult` is safe
        to dump straight into a workflow event log.
        """
        if len(value) <= MAX_TAIL_BYTES:
            return value
        return value[-MAX_TAIL_BYTES:]

    @property
    def ok(self) -> bool:
        """``True`` iff ``exit_code == 0``."""
        return self.exit_code == 0


# --------------------------------------------------------------------------
# SsimVerdict — separate from ValidatorResult because the SSIM stage
# has a continuous (score) signal, not a discrete (exit_code) one.
# --------------------------------------------------------------------------


SsimBucket = Literal["pass", "warn", "fail"]


class SsimVerdict(BaseModel):
    """The output of the slice-12 SSIM Figma-vs-rendered compare.

    Args:
        score (float): SSIM in ``[-1, 1]`` (practically ``[0, 1]``).
            Higher is more similar.
        region (str | None): an optional textual hint about where
            the largest dissimilarity lives (e.g. ``"header"``,
            ``"button row"``). The activity sets this when the
            ``warn`` or ``fail`` bucket triggers — it's the
            single most actionable string the LLM gets for
            visual refinement.
    """

    model_config = ConfigDict(extra="forbid")

    score: float
    region: str | None = None

    @field_validator("score")
    @classmethod
    def _validate_score(cls, value: float) -> float:
        """Reject scores outside SSIM's mathematical range.

        Scores outside ``[-1, 1]`` always indicate an activity bug
        — most likely a wrong dtype, a non-grayscale image, or a
        mismatched data_range argument. Fail loud so we never
        ship a bogus pass to the LLM.
        """
        if not -1.0 <= value <= 1.0:
            raise ValueError(
                f"SSIM score {value!r} outside the [-1, 1] range; "
                "likely indicates an activity-side bug"
            )
        return value

    @property
    def bucket(self) -> SsimBucket:
        """Bucket the continuous score against the two thresholds."""
        if self.score >= SSIM_PASS_THRESHOLD:
            return "pass"
        if self.score >= SSIM_WARN_THRESHOLD:
            return "warn"
        return "fail"

    @property
    def ok(self) -> bool:
        """``True`` for ``pass`` or ``warn``; ``False`` for ``fail``.

        Warn-bucket counts as a soft pass so the iteration loop
        doesn't churn on a tolerable difference.
        """
        return self.bucket != "fail"


# --------------------------------------------------------------------------
# CandidateResult — the aggregate per-iteration view the workflow
# returns to the MCP server (and the LLM) after each ``submit_candidate``.
# --------------------------------------------------------------------------


class CandidateResult(BaseModel):
    """The full validator panel for one candidate submission.

    Args:
        iteration (int): 1-based iteration number within the
            workflow run.
        component_name (str): the component being iterated on.
        validators (list[ValidatorResult]): per-validator rows in
            the order they were executed (fail-fast order: tsc,
            eslint, jest, axe).
        ssim (SsimVerdict | None): the visual-diff verdict, or
            ``None`` when the iteration didn't reach the SSIM
            stage (a cheaper validator already failed and
            short-circuited the chain).
    """

    model_config = ConfigDict(extra="forbid")

    iteration: int
    component_name: str
    validators: list[ValidatorResult] = Field(default_factory=list)
    ssim: SsimVerdict | None = None

    @property
    def all_passed(self) -> bool:
        """``True`` iff every validator passed AND SSIM is non-fail."""
        if not all(v.ok for v in self.validators):
            return False
        # The ssim-failure branch is intentionally separate from
        # the validator loop so the reader can see the two-stage
        # contract: "all subprocess validators pass" AND "SSIM
        # didn't fall below the warn-bucket threshold".
        return not (self.ssim is not None and not self.ssim.ok)

    @property
    def failing_kinds(self) -> list[str]:
        """List of failing validator kind names, in execution order.

        SSIM is reported as ``"ssim"`` to keep the failing-kinds
        list shape-uniform for the LLM regardless of whether the
        failure came from a subprocess or the SSIM stage.
        """
        out: list[str] = [v.kind.value for v in self.validators if not v.ok]
        if self.ssim is not None and not self.ssim.ok:
            out.append("ssim")
        return out


# --------------------------------------------------------------------------
# ReflectionContext — the pre-process scaffold the MCP server hands
# Cursor before the agent generates code. Built in Step 5
# (``reflection.py``) from the existing slice-1..11 indices.
# --------------------------------------------------------------------------


class ReflectionContext(BaseModel):
    """Structured AlphaCodium-flavored input bundle for code-gen.

    Args:
        component_name (str): the spec's component identifier.
        examples (list[str]): top-k JSX snippets retrieved via the
            slice-9 hybrid searcher.
        related (list[str]): collaborator components from the
            slice-10 composition graph.
        token_hints (list[str]): closest design-system color
            tokens for any hex literal in the spec, from
            slice-11.
        a11y_blocks (list[str]): per-component a11y guidance from
            slice-11 ``LLMS.md`` + chunk aggregator.
        candidate_decompositions (list[str]): the AlphaCodium
            "enumerate solutions" stage — 2 candidate
            compositional approaches the LLM should consider
            before picking one. Trimmed from the paper's 3-5 per
            the slice-12 design decision.
    """

    model_config = ConfigDict(extra="forbid")

    component_name: str
    examples: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    token_hints: list[str] = Field(default_factory=list)
    a11y_blocks: list[str] = Field(default_factory=list)
    candidate_decompositions: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# WorkflowStatus — the @workflow.query payload shape for the
# get_component_status MCP tool.
# --------------------------------------------------------------------------


WorkflowState = Literal["running", "passed", "failed", "cancelled"]


class WorkflowStartInput(BaseModel):
    """Inputs to :meth:`GenerateComponentWorkflow.run`.

    Args:
        component_name (str): PascalCase identifier — used both as
            the workspace folder name and the artifact basename.
        services_root (str): absolute path to the Prism library's
            ``services/`` directory.
        max_iterations (int): cap on ``submit_candidate`` rounds.
            Defaults to :data:`DEFAULT_MAX_ITERATIONS`.
        figma_png_path (str | None): absolute path to the Figma
            export. When ``None`` the workflow skips the SSIM stage.
        rendered_png_path_template (str): printf-style template for
            where Playwright writes the rendered screenshot;
            ``{services_root}`` and ``{component_name}`` are
            substituted before the SSIM activity is invoked. The
            workflow can't use :mod:`pathlib` (sandboxed) so we
            template the path here instead of computing it at
            workflow scope.
        max_wait_seconds (int): wall-clock cap on how long the
            workflow waits for the first ``submit_candidate``
            *and* for each iteration in between.
    """

    model_config = ConfigDict(extra="forbid")

    component_name: str
    services_root: str
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    figma_png_path: str | None = None
    rendered_png_path_template: str = (
        "{services_root}/playwright-output/{component_name}.png"
    )
    max_wait_seconds: int = DEFAULT_WORKFLOW_TIMEOUT_SECONDS


class SubmitInput(BaseModel):
    """Inputs to the ``submit_candidate`` update.

    Args:
        jsx_code (str): the candidate JSX body. Written verbatim to
            ``services/src/scratch/Generated/<Name>/<Name>.jsx``.
        companion_test_code (str | None): optional pwspec.ts the
            LLM produced from the AlphaCodium AI-test stage.
    """

    model_config = ConfigDict(extra="forbid")

    jsx_code: str
    companion_test_code: str | None = None


_DELIVERY_HINT_PASSED = (
    "Workflow passed. Call `get_final_artefact(workflow_id={workflow_id!r})` "
    "to retrieve the validated JSX + companion pwspec + tsconfig from the "
    "scratch dir, then write those bytes into the user's project tree at "
    "wherever the component should live (e.g. "
    "`<user-project>/src/components/{component_name}/{component_name}.jsx`). "
    "The scratch dir under "
    "`services/src/scratch/Generated/{component_name}/` is the validator's "
    "working directory only — treat it as a cache, not the final destination."
)
"""Delivery instruction the workflow attaches to its terminal state.

The slice-12 workflow validates code but does *not* know where the
user wants the final artefact to land. We surface this hint into
the workflow's terminal :class:`WorkflowStatus` and the
``submit_candidate`` response so the LLM agent (Cursor, Claude,
etc.) is reminded — at the exact moment a candidate passes — to
fetch the artefact and place it in the user's actual project.
Without the reminder the agent often "forgets" the delivery step
because the scratch dir is mentally indistinguishable from a
destination.
"""


def build_delivery_hint(*, workflow_id: str, component_name: str) -> str:
    """Render the delivery hint for a terminal/passing workflow.

    Pure formatting — kept beside :data:`_DELIVERY_HINT_PASSED` so
    callers can't drift the wording. Returns the empty string for
    non-passing states; the workflow + ``submit_candidate`` helpers
    treat an empty string as "no hint to surface".
    """
    return _DELIVERY_HINT_PASSED.format(
        workflow_id=workflow_id, component_name=component_name
    )


class WorkflowStatus(BaseModel):
    """Snapshot of a workflow's state at query time.

    Args:
        workflow_id (str): the Temporal workflow ID. The MCP
            client uses this to correlate ``submit_candidate``
            calls with the right workflow execution.
        component_name (str): the spec's component identifier.
        services_root (str): absolute path to the Prism library's
            ``services/`` directory the workflow was started against.
            Echoed back so the LLM agent doesn't need to remember
            the original start input — it can drive
            ``get_final_artefact`` from just the ``workflow_id``.
        iteration (int): the current 1-based iteration number;
            equals ``max_iterations`` when the cap has been hit.
        max_iterations (int): the bounded-loop cap (default 3
            per the AlphaCodium ablation).
        last_result (CandidateResult | None): the most-recently
            evaluated submission, or ``None`` before the first
            ``submit_candidate`` call.
        final_state (WorkflowState): ``running`` while the
            workflow is still accepting submissions; one of
            ``passed``/``failed``/``cancelled`` once it has
            terminated.
        delivery_hint (str): empty string while ``running``; a
            concrete next-step instruction once ``final_state``
            becomes ``passed`` (telling the agent to call
            ``get_final_artefact``). Surfaced into every
            ``get_component_status`` response so the agent gets
            the reminder even on a late-arriving status poll.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    component_name: str
    services_root: str = ""
    iteration: int
    max_iterations: int
    last_result: CandidateResult | None = None
    final_state: WorkflowState = "running"
    delivery_hint: str = ""


# --------------------------------------------------------------------------
# Reflection-prompt builder. Lives here (with the contracts) instead
# of in workflow.py because it has zero non-determinism — it's a
# pure string-formatter over a CandidateResult.
# --------------------------------------------------------------------------


_REFLECTION_TEMPLATE = """\
The validators found:
{validator_lines}

Before generating new code, answer briefly:
  1. What was your assumption that turned out wrong?
  2. Which Prism component or prop did you misuse?
  3. What is the minimal change that would fix this?

Then call prism.submit_candidate again with corrected code."""
"""ReflexiCoder-style 3-question reflection wrapper.

The exact wording is load-bearing: per the paper's ablation, the
*structure* of the 3 questions accounts for most of the iteration
gain. Don't drift the wording without re-running the eval.
"""


def build_reflection_prompt(result: CandidateResult) -> str:
    """Render the reflection prompt for a failing candidate.

    Args:
        result (CandidateResult): the just-evaluated candidate.
            Must have ``all_passed == False`` for the prompt to
            be non-empty; passing candidates return ``""`` so the
            workflow can use the prompt as a falsy guard.

    Returns:
        str: the rendered prompt, or ``""`` when the candidate
        passed.
    """
    if result.all_passed:
        return ""

    lines: list[str] = []
    for validator in result.validators:
        if validator.ok:
            continue
        lines.append(
            f"  - {validator.kind.value}: {validator.stdout_tail[:400]}"
        )
    if result.ssim is not None and not result.ssim.ok:
        region = result.ssim.region or "unspecified region"
        lines.append(
            f"  - ssim: score={result.ssim.score:.3f} below threshold "
            f"{SSIM_WARN_THRESHOLD} (diff likely in {region})"
        )
    return _REFLECTION_TEMPLATE.format(validator_lines="\n".join(lines))
