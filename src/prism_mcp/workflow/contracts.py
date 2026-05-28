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


SsimSkipReason = Literal["rendered_unavailable"]
"""Enumerates the reasons SSIM did not produce a verdict despite a
Figma reference being available and all subprocess validators passing.

Today only one such reason exists: ``"rendered_unavailable"`` —
Playwright's pwspec did not write the screenshot at the templated
``rendered_png_path``. The most common cause is the auto-scaffolded
pwspec being a smoke test that doesn't navigate / capture; the LLM
is expected to refine the pwspec via ``update_companion_tests`` so
the next iteration emits a real screenshot.

Kept as a ``Literal`` (not an open string) so a typo in any caller
flips a Pydantic validation error rather than silently skipping the
reflection nudge. Adding a new reason in the future is a deliberate,
schema-visible change.
"""


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
        figma_reference_present (bool): mirrored from the workflow
            start input — ``True`` when *any* of
            ``figma_png_path`` / ``figma_png_url`` /
            ``figma_png_base64`` was supplied. When ``False`` the
            reflection prompt nudges the LLM to pass a reference
            on the next ``start_generate_component`` so SSIM can
            run. Defaults to ``True`` so existing call sites that
            don't populate it remain backwards-compatible.
        ssim_skip_reason (SsimSkipReason | None): when SSIM was
            attempted (Figma reference present + all subprocess
            validators passing) but did not produce a verdict, the
            reason it was skipped instead of crashing. Today only
            ``"rendered_unavailable"`` exists — the templated
            ``rendered_png_path`` was missing because the
            auto-scaffold pwspec is a smoke test that doesn't write
            a screenshot. The reflection prompt converts this into
            an explicit refinement nudge for the LLM.
    """

    model_config = ConfigDict(extra="forbid")

    iteration: int
    component_name: str
    validators: list[ValidatorResult] = Field(default_factory=list)
    ssim: SsimVerdict | None = None
    figma_reference_present: bool = True
    ssim_skip_reason: SsimSkipReason | None = None

    @property
    def all_passed(self) -> bool:
        """``True`` iff every validator passed AND SSIM is non-fail.

        The three failure branches are intentionally separate so
        the reader can see the contract:

        1. **Subprocess validators**: all of tsc / eslint / jest /
           playwright_axe must pass (and the dependencies hard
           gate before them).
        2. **SSIM verdict**: if SSIM ran, its bucket must not be
           ``"fail"`` (warn counts as a soft pass).
        3. **SSIM skip reason**: if SSIM was *attempted* (Figma
           reference present + validators all passing) but
           skipped because the rendered screenshot was missing,
           the iteration does not pass — the workflow needs the
           LLM to refine the pwspec via
           ``update_companion_tests`` so SSIM can run on the
           next iteration. A skip with no reason set (e.g.
           ``figma_reference_present == False``) is *not* a
           failure: the user opted out of SSIM by not supplying
           a Figma reference.
        """
        if not all(v.ok for v in self.validators):
            return False
        if self.ssim is not None and not self.ssim.ok:
            return False
        return self.ssim_skip_reason != "rendered_unavailable"

    @property
    def failing_kinds(self) -> list[str]:
        """List of failing validator kind names, in execution order.

        SSIM is reported as ``"ssim"`` to keep the failing-kinds
        list shape-uniform for the LLM regardless of whether the
        failure came from a subprocess result, an SSIM verdict
        below threshold, or the SSIM phase being skipped because
        the rendered PNG was missing.
        """
        out: list[str] = [v.kind.value for v in self.validators if not v.ok]
        ssim_failed = self.ssim is not None and not self.ssim.ok
        ssim_skipped = self.ssim_skip_reason == "rendered_unavailable"
        if ssim_failed or ssim_skipped:
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

    Figma reference resolution
    --------------------------

    The SSIM activity needs a *local PNG path* to compare against,
    but Figma MCP usually hands the agent a temporary URL or a
    base64 payload, not a file path. Three fields allow each
    transport, in priority order:

    1. ``figma_png_path`` — already-on-disk path. Cheapest. The
       workflow uses it verbatim.
    2. ``figma_png_url`` — HTTPS URL. The
       ``materialise_figma_reference`` activity downloads the
       PNG once at workflow start, caches it on disk, and reuses
       the cached path on every SSIM iteration.
    3. ``figma_png_base64`` — inline base64 payload. The same
       materialisation activity decodes and caches it.

    All three are optional — when none are supplied the workflow
    skips the SSIM stage entirely. The reflection prompt nudges
    the LLM to supply at least one on the next start when SSIM
    was skipped for lack of a reference.

    Args:
        component_name (str): PascalCase identifier — used both as
            the workspace folder name and the artifact basename.
        services_root (str): absolute path to the Prism library's
            ``services/`` directory.
        max_iterations (int): cap on ``submit_candidate`` rounds.
            Defaults to :data:`DEFAULT_MAX_ITERATIONS`.
        figma_png_path (str | None): absolute path to a Figma
            export already on disk. Highest priority.
        figma_png_url (str | None): HTTPS URL the activity will
            download once at workflow start. Reused across
            iterations.
        figma_png_base64 (str | None): inline base64 PNG payload.
            Decoded once at workflow start. Useful when Figma MCP
            returned an embedded image rather than a URL.
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
    figma_png_url: str | None = None
    figma_png_base64: str | None = None
    rendered_png_path_template: str = (
        "{services_root}/playwright-output/{component_name}.png"
    )
    max_wait_seconds: int = DEFAULT_WORKFLOW_TIMEOUT_SECONDS

    @property
    def has_figma_reference(self) -> bool:
        """Whether *any* of the three Figma reference channels is set.

        SSIM is gated on this being ``True``. The workflow uses
        this to decide whether to call
        ``materialise_figma_reference``.
        """
        return any(
            (
                self.figma_png_path,
                self.figma_png_url,
                self.figma_png_base64,
            )
        )


class SubmitInput(BaseModel):
    """Inputs to the ``submit_candidate`` update.

    Args:
        jsx_code (str): the candidate JSX body. Written verbatim to
            ``services/src/scratch/Generated/<Name>/<Name>.jsx``.
        companion_test_code (str | None): optional pwspec.ts. If
            ``None`` and no pwspec exists yet the workflow auto-
            scaffolds one; if ``None`` and a pwspec already exists
            the workflow preserves it. The LLM rarely supplies
            pwspec via this path — :class:`UpdateCompanionTestsInput`
            is the canonical refinement channel.
        companion_spec_code (str | None): optional jest spec.tsx,
            same write-once-then-preserve semantics as
            ``companion_test_code``.
    """

    model_config = ConfigDict(extra="forbid")

    jsx_code: str
    companion_test_code: str | None = None
    companion_spec_code: str | None = None


class UpdateCompanionTestsInput(BaseModel):
    """Inputs to the ``update_companion_tests`` workflow update.

    The auto-scaffold path (in :class:`SubmitInput`) seeds
    iteration 1 with a minimal pwspec + jest spec so the
    validator chain has something to run. Once the LLM has a
    stable component shape and wants to add behaviour-specific
    assertions (axe checks, visual regression, prop-driven
    rendering), it calls ``update_companion_tests`` to refine
    the test files in place. The next ``submit_candidate`` round
    will run the validators against the refined tests.

    Either field can be ``None`` to leave that test file
    untouched. Supplying both in one call is the typical
    pattern when the LLM has finalised a behavioural contract
    and wants jest + Playwright to assert it together.

    Args:
        pwspec_code (str | None): the Playwright pwspec body to
            write. ``None`` means leave the existing pwspec on
            disk untouched.
        spec_code (str | None): the jest spec.tsx body to write.
            ``None`` means leave the existing spec untouched.
    """

    model_config = ConfigDict(extra="forbid")

    pwspec_code: str | None = None
    spec_code: str | None = None


class UpdateCompanionTestsResult(BaseModel):
    """Result of the ``update_companion_tests`` workflow update.

    Lets the LLM verify which files actually changed (e.g. when
    only one of the two was passed) without having to re-read
    the scratch dir.

    Args:
        component_name (str): echoed for cross-referencing.
        wrote_pwspec (bool): ``True`` when the pwspec was
            written (i.e. ``pwspec_code`` was non-None).
        wrote_spec (bool): ``True`` when the spec was written.
        pwspec_path (str): absolute path to the pwspec file on
            disk (whether or not we wrote to it this call).
        spec_path (str): absolute path to the spec.tsx file on
            disk.
        next_step_hint (str): what the LLM should do next. Typically
            "call submit_candidate again to re-run validators
            against the refined tests."
    """

    model_config = ConfigDict(extra="forbid")

    component_name: str
    wrote_pwspec: bool
    wrote_spec: bool
    pwspec_path: str
    spec_path: str
    next_step_hint: str = (
        "Call submit_candidate again to re-run the validators against "
        "the refined tests."
    )


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

Then call prism.submit_candidate again with corrected code.{figma_nudge}{rendered_unavailable_nudge}"""
"""ReflexiCoder-style 3-question reflection wrapper.

The exact wording of the 3 questions is load-bearing: per the
paper's ablation, the *structure* of the 3 questions accounts
for most of the iteration gain. Don't drift that wording without
re-running the eval. The trailing ``{figma_nudge}`` and
``{rendered_unavailable_nudge}`` slots are the only places we
ever extend — see :data:`_FIGMA_REFERENCE_NUDGE` and
:data:`_RENDERED_UNAVAILABLE_NUDGE`.
"""


_FIGMA_REFERENCE_NUDGE = """

Note on visual validation
-------------------------
This workflow has no Figma reference, so SSIM was skipped. If
visual fidelity matters, cancel this run and call
`start_generate_component` again with `figma_png_url=...`
(or `figma_png_base64=...`) populated from the Figma MCP's
node screenshot. The workflow downloads the image once at
start and runs SSIM on every all-pass iteration."""
"""Reminder appended to the reflection prompt when SSIM is unavailable.

The text is intentionally a separate paragraph so the
ReflexiCoder 3-question core stays unchanged when the nudge
fires. We append rather than replace — the paper's ablation
shows the 3-question prompt is the load-bearing piece, and this
nudge is a complementary signal on top of it.
"""


_RENDERED_UNAVAILABLE_NUDGE = """

Note on the rendered screenshot
-------------------------------
All subprocess validators passed and a Figma reference is loaded,
but the pwspec did not write the rendered screenshot at:
  {rendered_png_path}

The auto-scaffolded pwspec is a smoke test that does NOT navigate
to the component or capture pixels. To unlock SSIM, refine the
pwspec via `update_companion_tests(workflow_id, pwspec_code=...)`
so it ends with a `await page.screenshot({{ path: "playwright-output/{component_name}.png", fullPage: false }})`
call after mounting the component. SSIM runs on every all-pass
iteration once the screenshot exists."""
"""Reminder appended to the reflection prompt when SSIM was attempted
but the rendered PNG was missing.

This nudge gives the LLM the literal path it needs to write to and
the tool to call (``update_companion_tests``) — keeping it
copy-paste actionable rather than a generic "fix the pwspec"
suggestion. The format placeholders (``{rendered_png_path}`` and
``{component_name}``) are substituted by
:func:`build_reflection_prompt` before the template is interpolated
into ``_REFLECTION_TEMPLATE`` so we don't double-format.
"""


def build_reflection_prompt(
    result: CandidateResult,
    *,
    rendered_png_path: str | None = None,
) -> str:
    """Render the reflection prompt for a failing candidate.

    Args:
        result (CandidateResult): the just-evaluated candidate.
            Returns ``""`` when ``all_passed`` AND the SSIM phase
            was not skipped for ``"rendered_unavailable"``;
            otherwise renders the ReflexiCoder 3-question core
            plus any applicable nudge.
        rendered_png_path (str | None): the templated path the
            workflow expected Playwright to write the screenshot
            to. Only used to interpolate
            :data:`_RENDERED_UNAVAILABLE_NUDGE`; when ``None`` (or
            when the skip reason isn't ``"rendered_unavailable"``)
            the nudge is omitted.

    Returns:
        str: the rendered prompt, or ``""`` when the candidate
        truly passed (validators ok + no SSIM skip reason).
        The prompt always includes the ReflexiCoder 3-question
        core; when the workflow had no Figma reference
        (i.e. ``figma_reference_present == False``) it also
        appends a paragraph telling the LLM to pass
        ``figma_png_url`` on the next ``start_generate_component``
        so future iterations can run SSIM. When SSIM was attempted
        but the rendered screenshot was missing
        (``ssim_skip_reason == "rendered_unavailable"``), it
        appends a paragraph pointing the LLM at
        ``update_companion_tests`` with the exact path the pwspec
        must write to.
    """
    rendered_unavailable = (
        result.ssim_skip_reason == "rendered_unavailable"
    )
    if result.all_passed and not rendered_unavailable:
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
    if rendered_unavailable:
        lines.append(
            "  - ssim: skipped — rendered screenshot missing at the "
            "templated path (see note below)"
        )
    figma_nudge = (
        _FIGMA_REFERENCE_NUDGE if not result.figma_reference_present else ""
    )
    rendered_unavailable_nudge = (
        _RENDERED_UNAVAILABLE_NUDGE.format(
            rendered_png_path=rendered_png_path or "<unknown path>",
            component_name=result.component_name,
        )
        if rendered_unavailable
        else ""
    )
    return _REFLECTION_TEMPLATE.format(
        validator_lines="\n".join(lines),
        figma_nudge=figma_nudge,
        rendered_unavailable_nudge=rendered_unavailable_nudge,
    )
