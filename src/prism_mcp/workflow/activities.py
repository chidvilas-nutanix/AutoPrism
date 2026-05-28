"""Temporal activities for the slice-12 component-generation loop.

Activities are the *only* place in the workflow package allowed to
perform side effects — :mod:`subprocess`, filesystem writes,
network I/O. Everything else (the workflow class itself) must stay
deterministic for Temporal's replay-safety guarantees.

Each ``run_<validator>`` activity is a thin async wrapper around
:func:`asyncio.create_subprocess_exec` that:

1. Invokes the matching JS validator binary directly out of
   ``services/node_modules/.bin/`` — scoped to *just* the candidate
   sub-tree (``src/scratch/Generated/<Name>/``) so noise from
   pre-existing errors elsewhere in the Prism codebase does not
   pollute the LLM's reflection feedback.
2. Captures stdout + stderr, tail-truncates per the
   :data:`MAX_TAIL_BYTES` contract.
3. Times the run.
4. Returns a :class:`ValidatorResult` for the workflow to fold into
   the iteration's :class:`CandidateResult`.

The :func:`write_candidate_files` activity is the bridge between
"Cursor has produced a JSX string" and "the validators can run":
it writes the JSX (plus optional companion ``pwspec.ts``) plus a
per-``<Name>`` ``tsconfig.json`` into the scratch sub-tree.

The :func:`check_dependencies_installed` activity is the slice-12
gap-closing preflight: it verifies the four critical JS binaries
exist before any other subprocess validator is allowed to spawn.
Without it, missing ``node_modules`` surfaces as ``ENOENT`` deep
inside :func:`asyncio.create_subprocess_exec` and the LLM gets a
confusing "no such file or directory: tsc" instead of an
actionable "run ``npm install`` in ``services/``".

Why scope to ``src/scratch/Generated/<Name>/``?
------------------------------------------------

The Prism library's own ``npm run typecheck`` / ``npm run eslint``
scripts validate the *entire* codebase. They surface ~2 pre-existing
``tsc`` errors in ``DatePicker.tsx`` and hundreds of pre-existing
ESLint warnings unrelated to anything the LLM is generating. Those
errors would short-circuit the iteration loop on every run with
noise unrelated to the candidate. Scoping each invocation to just
the candidate directory eliminates the noise floor: the LLM sees
only the errors *its own code* introduced.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from temporalio import activity

from prism_mcp.workflow.contracts import (
    SsimVerdict,
    ValidatorKind,
    ValidatorResult,
)
from prism_mcp.workflow.ssim import (
    compute_ssim_from_paths,
    materialise_image,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Path conventions. Centralised so the activities + the
# prism-mcp-setup preflight + the workflow itself can't drift apart
# on where the scratch tree lives.
# --------------------------------------------------------------------------


_SCRATCH_PARENT_PARTS = ("src", "scratch", "Generated")
"""Path segments under ``services/`` where generated artefacts live.

The ``scratch`` segment isolates LLM-produced code so it doesn't
pollute Prism's real component tree on accidental git commits
(adding ``services/src/scratch/`` to ``.gitignore`` is the
recommended companion change for the demo operator).
"""


_REQUIRED_VALIDATOR_BINARIES = ("tsc", "eslint", "jest", "playwright")
"""The four ``node_modules/.bin/`` binaries every iteration needs.

These names match the validators in :data:`ValidatorKind` (minus
``dependencies`` which checks for the others, and ``ssim`` which
is pure-Python via :mod:`scikit-image`). If the JS toolchain
evolves and a validator is replaced (e.g. ``biome`` for
``eslint``), update this constant and the matching ``run_X``
activity together.
"""


_REQUIRED_PLAYWRIGHT_BROWSERS = ("chromium", "firefox", "webkit")
"""Browser engines Playwright will spawn during ``playwright test``.

Matches the three ``projects:`` entries in the Prism library's
``playwright.config.ts``. With any of these missing, the
``run_playwright_axe`` activity exits non-zero on
``browserType.launch: Executable doesn't exist`` *deep* inside the
Playwright runner's stack trace — which obscures the real fix:
``npx playwright install``. We probe the cache up front so the
``dependencies`` validator returns a clean remediation hint
instead.
"""


def _scratch_dir(services_root: str | Path, component_name: str) -> Path:
    """Return the absolute path to ``<Name>``'s scratch directory."""
    return Path(services_root, *_SCRATCH_PARENT_PARTS, component_name)


def _scratch_dir_rel(component_name: str) -> str:
    """The scratch dir expressed *relative to* ``services_root``.

    Subprocess validators run with ``cwd=services_root``, so passing
    the relative form keeps their command lines short and stable
    across operator machines (no absolute paths in npm/eslint
    output that would invalidate log snapshots).
    """
    return "/".join((*_SCRATCH_PARENT_PARTS, component_name))


# --------------------------------------------------------------------------
# Scratch tsconfig template. Per-<Name> so concurrent workflow runs
# can validate independently and so the tsc invocation can be
# ``tsc --noEmit -p <scratch>/tsconfig.json`` instead of relying on
# the project-wide tsconfig (which includes the *entire* src tree
# and surfaces unrelated pre-existing errors).
# --------------------------------------------------------------------------


_SCRATCH_TSCONFIG_TEMPLATE = """\
{
  "extends": "../../../../tsconfig.json",
  "compilerOptions": {
    "noEmit": true,
    "noImplicitAny": false
  },
  "include": [
    "./**/*.ts",
    "./**/*.tsx",
    "./**/*.jsx"
  ],
  "exclude": ["node_modules", "**/*.pwspec.ts"]
}
"""
"""Template body for ``<scratch>/<Name>/tsconfig.json``.

The four-level ``..`` chain climbs from
``services/src/scratch/Generated/<Name>/`` back to ``services/``
where the project's real ``tsconfig.json`` lives. The override
relaxes ``noImplicitAny`` because Cursor-generated JSX rarely
annotates props on a first pass; we lean on ESLint
(``react/prop-types``) for the prop-validation signal instead.
``**/*.pwspec.ts`` is excluded so the typecheck doesn't choke on
Playwright-only types that need a separate compile context.
"""


# --------------------------------------------------------------------------
# Auto-scaffolded test-file templates. The slice-12 hybrid strategy:
# the workflow always writes a minimal pwspec + jest spec at iteration 1
# so Playwright + Jest have something to run regardless of whether the
# LLM authored companion tests. The LLM can refine these via the
# ``update_companion_tests`` tool when behaviour-specific assertions
# are needed.
# --------------------------------------------------------------------------


_PWSPEC_SCAFFOLD_MARKER = "// prism-mcp:auto-scaffolded-pwspec"
"""Sentinel comment in the auto-scaffolded pwspec body.

Future tooling can grep for this marker to know whether the
pwspec is the workflow's own scaffold or one the LLM has
already refined. We never depend on the marker for behaviour —
the existence-check in :func:`write_candidate_files` is the
real gate — but the marker is a free debugging breadcrumb.
"""


_SPEC_SCAFFOLD_MARKER = "// prism-mcp:auto-scaffolded-spec"
"""Sentinel comment in the auto-scaffolded jest spec body.

See :data:`_PWSPEC_SCAFFOLD_MARKER`.
"""


def _scaffold_pwspec(component_name: str) -> str:
    """Return a minimal Playwright pwspec for a scratch component.

    The scaffold deliberately avoids assuming a pre-built
    styleguide route or harness page (Prism's own pwspecs use
    ``playwright-util.visitPage`` which depends on the
    ``services/www`` styleguide build — unavailable for
    scratch components). Instead we ship a single passing
    smoke test so:

    * ``run_playwright_axe`` finds at least one test and exits 0
      cleanly (no ``--pass-with-no-tests`` workaround needed).
    * The LLM has a working starting point to refine via
      ``update_companion_tests`` when behaviour-specific axe
      or visual assertions are warranted.

    The scaffold is intentionally a smoke test: it asserts a
    tautology so the playwright_axe validator passes from
    iteration 1. Crucially, **the smoke test does NOT write a
    screenshot**, which means SSIM is skipped on iteration 1 with
    ``ssim_skip_reason="rendered_unavailable"`` — the workflow's
    pre-check activity (``check_rendered_exists``) detects the
    missing PNG and converts what used to be a
    ``FileNotFoundError`` crash into a clean reflection-prompt
    nudge. The nudge tells the LLM to refine this scaffold via
    ``update_companion_tests`` once it knows how to mount the
    component, so the next iteration emits a real screenshot at
    the path the SSIM stage expects.

    The embedded ``REFINEMENT TEMPLATE`` block is the literal
    snippet the LLM should swap in when it has a working mount
    pattern. Keeping it inside the scaffold (as a comment) means
    Cursor can grep the just-written file for guidance without
    needing additional tool calls.
    """
    return f"""\
import {{ test, expect }} from '@playwright/test';

{_PWSPEC_SCAFFOLD_MARKER}
// Auto-scaffolded by prism-mcp's `start_generate_component` workflow.
// SMOKE TEST ONLY — does not capture pixels, so SSIM will be skipped
// with ssim_skip_reason="rendered_unavailable" on iteration 1.
//
// Refine via the `update_companion_tests` MCP tool once you know
// how to mount {component_name}. The validator-side contract is:
//
//   - Save a Playwright screenshot at:
//     services/playwright-output/{component_name}.png
//   - The path is relative to services_root the workflow received.
//
// REFINEMENT TEMPLATE (copy into update_companion_tests.pwspec_code):
// ----------------------------------------------------------------
//   import {{ test, expect }} from '@playwright/test';
//   import AxeBuilder from '@axe-core/playwright';
//
//   test('{component_name} renders + axe + screenshot', async ({{ page }}) => {{
//     await page.goto('http://localhost:5173/scratch/{component_name}');
//     // ^ replace with your project's harness URL.
//     const axe = await new AxeBuilder({{ page }}).analyze();
//     expect(axe.violations).toEqual([]);
//     await page.screenshot({{
//       path: 'playwright-output/{component_name}.png',
//       fullPage: false,
//     }});
//   }});
// ----------------------------------------------------------------

test.describe('{component_name} scaffolded suite', () => {{
  test('scaffold smoke', () => {{
    expect(true).toBe(true);
  }});
}});
"""


def _scaffold_spec(component_name: str) -> str:
    """Return a minimal Jest spec.tsx for a scratch component.

    The scaffold imports the candidate via its default export
    (Prism's repo-wide convention; see e.g.
    ``services/src/components/v2/Alert/Alert.spec.tsx``) and
    asserts that the import resolves to a defined value.
    Catches:

    * Missing/typo'd default export (LLM forgot ``export default``).
    * Syntax errors at module load time (would crash the import).

    We deliberately *don't* call ``render(<Component />)``: many
    Prism components have required props, and a default-render
    test would fail noisily on those without giving the LLM
    actionable feedback. The LLM upgrades this scaffold via
    ``update_companion_tests`` when it knows the prop shape.
    """
    return f"""\
import React from 'react';
import {component_name} from './{component_name}';

{_SPEC_SCAFFOLD_MARKER}
// Auto-scaffolded by prism-mcp's `start_generate_component` workflow.
// Refine via the `update_companion_tests` MCP tool to add
// behaviour-specific render + assertion patterns once {component_name}'s
// prop shape is stable.

describe('{component_name} scaffolded suite', () => {{
  it('module exports the component', () => {{
    expect({component_name}).toBeDefined();
  }});
}});

// React import is intentional: the scaffold may grow into a JSX-using
// render call when the LLM refines it via update_companion_tests.
void React;
"""


# --------------------------------------------------------------------------
# Activity input models. Each activity takes exactly one Pydantic
# argument so Temporal's data converter has a single schema to
# serialise (mixing positional args + kwargs is supported but harder
# to evolve safely across workflow-version upgrades).
# --------------------------------------------------------------------------


class CandidateInput(BaseModel):
    """Inputs for :func:`write_candidate_files`.

    Args:
        services_root (str): absolute path to
            ``prism-ui-prism-reactjs-lib/services``.
        component_name (str): PascalCase identifier (matches the
            file stem we write to disk).
        jsx_code (str): the candidate component's JSX body.
            Written verbatim — no syntax/imports massaging here.
        companion_test_code (str | None): optional pwspec.ts body.
            When supplied, it overwrites whatever pwspec was on
            disk (scaffolded or LLM-authored). When ``None``, the
            activity falls back to the auto-scaffold *only if*
            no pwspec exists yet (preserving any earlier
            LLM-supplied content across iterations).
        companion_spec_code (str | None): optional jest spec.tsx body.
            Same write-once semantics as ``companion_test_code``:
            supplied content overwrites; ``None`` triggers the
            scaffold only on the first iteration.
    """

    model_config = ConfigDict(extra="forbid")

    services_root: str
    component_name: str
    jsx_code: str
    companion_test_code: str | None = None
    companion_spec_code: str | None = None


class ServicesContext(BaseModel):
    """Lightweight pointer passed to every ``run_<validator>`` activity.

    Args:
        services_root (str): absolute path to the Prism library's
            ``services/`` directory.
        component_name (str): the component being validated.
            Surfaced verbatim in :class:`ValidatorResult` for
            cross-referencing logs.
    """

    model_config = ConfigDict(extra="forbid")

    services_root: str
    component_name: str


class SsimInput(BaseModel):
    """Inputs for :func:`run_ssim_compare`.

    Args:
        figma_png_path (str): path to the Figma export.
        rendered_png_path (str): path to the Playwright-captured
            screenshot.
    """

    model_config = ConfigDict(extra="forbid")

    figma_png_path: str
    rendered_png_path: str


class RenderedExistsInput(BaseModel):
    """Inputs for :func:`check_rendered_exists`.

    Args:
        rendered_png_path (str): the path the workflow templated
            for Playwright to write the screenshot to. The
            activity does not validate format or readability —
            only existence.
    """

    model_config = ConfigDict(extra="forbid")

    rendered_png_path: str


class RenderedExistsResult(BaseModel):
    """Output of :func:`check_rendered_exists`.

    Args:
        rendered_png_path (str): the path the activity inspected.
            Echoed back so workflow logs include both the path and
            the verdict on a single line.
        exists (bool): ``True`` iff the path resolved to an
            existing file (not just the parent directory). The
            workflow uses this to decide between calling
            :func:`run_ssim_compare` and recording an
            ``ssim_skip_reason`` of ``"rendered_unavailable"``.
    """

    model_config = ConfigDict(extra="forbid")

    rendered_png_path: str
    exists: bool


class FigmaReferenceInput(BaseModel):
    """Inputs for :func:`materialise_figma_reference`.

    Exactly one of the three optional fields is expected to be
    set (the workflow's ``WorkflowStartInput.has_figma_reference``
    gate ensures we never call the activity with all three
    ``None``). When more than one is set, ``figma_png_path`` wins
    over ``figma_png_url`` wins over ``figma_png_base64`` —
    matching :func:`prism_mcp.workflow.ssim.materialise_image`.

    Args:
        figma_png_path (str | None): pre-existing on-disk PNG.
        figma_png_url (str | None): HTTPS URL to download.
        figma_png_base64 (str | None): inline base64 PNG body
            (raw or RFC-2397 ``data:`` URL).
    """

    model_config = ConfigDict(extra="forbid")

    figma_png_path: str | None = None
    figma_png_url: str | None = None
    figma_png_base64: str | None = None


class FigmaReferenceResult(BaseModel):
    """Result of :func:`materialise_figma_reference`.

    Args:
        path (str | None): absolute path to the on-disk PNG, or
            ``None`` when no input field was supplied (caller
            should not invoke the activity in that case, but the
            field exists for symmetry).
        source (str): which input branch the activity took —
            ``"path"``, ``"url"``, ``"base64"``, or ``"none"``.
            Echoed back so the workflow can log a clear "we
            downloaded from URL X to path Y" line.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None
    source: str


# --------------------------------------------------------------------------
# Filesystem activity — writes generated code to the scratch tree.
# --------------------------------------------------------------------------


@activity.defn
async def write_candidate_files(input: CandidateInput) -> ServicesContext:
    """Materialise the candidate JSX + auto-scaffolded test files on disk.

    The destination layout matches the slice-12 design decision:

    ``services/src/scratch/Generated/<Name>/<Name>.jsx``         (always)
    ``services/src/scratch/Generated/<Name>/<Name>.pwspec.ts``    (always)
    ``services/src/scratch/Generated/<Name>/<Name>.spec.tsx``     (always)
    ``services/src/scratch/Generated/<Name>/tsconfig.json``       (always)

    The scoped ``tsconfig.json`` is what lets :func:`run_typecheck`
    invoke ``tsc -p <scratch>/tsconfig.json`` and only see this
    candidate's files. Without it tsc would pick up the project-wide
    ``services/tsconfig.json`` and surface every pre-existing error
    in the Prism codebase on top of the candidate's own errors.

    Test-file write semantics
    -------------------------

    The pwspec and spec.tsx files use **write-once-then-preserve**
    semantics so the workflow's auto-scaffold seeds iteration 1 but
    later iterations don't clobber LLM-refined content:

    * If ``companion_test_code`` / ``companion_spec_code`` is
      supplied, it overwrites whatever was on disk. This is the
      ``update_companion_tests`` tool's path.
    * If the param is ``None`` and no test file exists yet (first
      iteration), the activity writes the auto-scaffold from
      :func:`_scaffold_pwspec` / :func:`_scaffold_spec`.
    * If the param is ``None`` and a test file already exists
      (subsequent iterations), the activity leaves it alone —
      preserving any refinement the LLM has made via
      ``update_companion_tests`` against an earlier iteration.

    Args:
        input (CandidateInput): see the class docstring.

    Returns:
        ServicesContext: the pointer subsequent validator activities
        need (``services_root`` + ``component_name``).
    """
    services_root = Path(input.services_root)
    dest_dir = _scratch_dir(services_root, input.component_name)
    dest_dir.mkdir(parents=True, exist_ok=True)

    jsx_path = dest_dir / f"{input.component_name}.jsx"
    jsx_path.write_text(input.jsx_code, encoding="utf-8")
    logger.info(
        "wrote jsx candidate path=%s bytes=%d", jsx_path, len(input.jsx_code)
    )

    tsconfig_path = dest_dir / "tsconfig.json"
    tsconfig_path.write_text(_SCRATCH_TSCONFIG_TEMPLATE, encoding="utf-8")
    logger.info("wrote scoped tsconfig path=%s", tsconfig_path)

    pwspec_path = dest_dir / f"{input.component_name}.pwspec.ts"
    _materialise_companion_file(
        path=pwspec_path,
        supplied_body=input.companion_test_code,
        scaffold_factory=lambda: _scaffold_pwspec(input.component_name),
        kind="pwspec",
    )

    spec_path = dest_dir / f"{input.component_name}.spec.tsx"
    _materialise_companion_file(
        path=spec_path,
        supplied_body=input.companion_spec_code,
        scaffold_factory=lambda: _scaffold_spec(input.component_name),
        kind="spec",
    )

    return ServicesContext(
        services_root=input.services_root,
        component_name=input.component_name,
    )


class UpdateCompanionFilesInput(BaseModel):
    """Inputs for :func:`update_companion_test_files`.

    Mirror of :class:`prism_mcp.workflow.contracts.UpdateCompanionTestsInput`,
    but materialised at the activity boundary so Temporal's data
    converter has its usual single-Pydantic-arg shape.

    Args:
        services_root (str): same as :class:`CandidateInput`.
        component_name (str): same as :class:`CandidateInput`.
        pwspec_code (str | None): when set, overwrite the pwspec
            file. ``None`` is a no-op for that file.
        spec_code (str | None): when set, overwrite the spec.tsx
            file. ``None`` is a no-op.
    """

    model_config = ConfigDict(extra="forbid")

    services_root: str
    component_name: str
    pwspec_code: str | None = None
    spec_code: str | None = None


class UpdateCompanionFilesResult(BaseModel):
    """Result of :func:`update_companion_test_files`.

    Mirror of :class:`prism_mcp.workflow.contracts.UpdateCompanionTestsResult`
    but without the prose ``next_step_hint`` (the workflow layer
    composes that from ``component_name``).
    """

    model_config = ConfigDict(extra="forbid")

    component_name: str
    wrote_pwspec: bool
    wrote_spec: bool
    pwspec_path: str
    spec_path: str


@activity.defn
async def update_companion_test_files(
    input: UpdateCompanionFilesInput,
) -> UpdateCompanionFilesResult:
    """Overwrite the candidate's pwspec and/or jest spec.

    This is the LLM's path for refining the auto-scaffolded test
    files. Differs from :func:`write_candidate_files` in two ways:

    * It does **not** touch the JSX or tsconfig — those are owned
      by the ``submit_candidate`` flow.
    * Either input field can be ``None`` to leave that test file
      alone; only the supplied fields are overwritten.

    The activity raises :class:`FileNotFoundError` if the scratch
    dir doesn't exist yet — the LLM must have called
    ``submit_candidate`` at least once to seed the scratch tree
    before refining tests.

    Args:
        input (UpdateCompanionFilesInput): see the class docstring.

    Returns:
        UpdateCompanionFilesResult: which files were actually
        written, plus their absolute paths.
    """
    services_root = Path(input.services_root)
    dest_dir = _scratch_dir(services_root, input.component_name)
    if not dest_dir.is_dir():
        raise FileNotFoundError(
            f"scratch dir {dest_dir} does not exist; call "
            "submit_candidate at least once before refining tests"
        )

    pwspec_path = dest_dir / f"{input.component_name}.pwspec.ts"
    wrote_pwspec = False
    if input.pwspec_code is not None:
        pwspec_path.write_text(input.pwspec_code, encoding="utf-8")
        wrote_pwspec = True
        logger.info(
            "refined pwspec path=%s bytes=%d",
            pwspec_path,
            len(input.pwspec_code),
        )

    spec_path = dest_dir / f"{input.component_name}.spec.tsx"
    wrote_spec = False
    if input.spec_code is not None:
        spec_path.write_text(input.spec_code, encoding="utf-8")
        wrote_spec = True
        logger.info(
            "refined jest spec path=%s bytes=%d",
            spec_path,
            len(input.spec_code),
        )

    return UpdateCompanionFilesResult(
        component_name=input.component_name,
        wrote_pwspec=wrote_pwspec,
        wrote_spec=wrote_spec,
        pwspec_path=str(pwspec_path),
        spec_path=str(spec_path),
    )


def _materialise_companion_file(
    *,
    path: Path,
    supplied_body: str | None,
    scaffold_factory: object,
    kind: str,
) -> None:
    """Write a companion test file with write-once-then-preserve semantics.

    Three branches:

    1. ``supplied_body`` is non-None → overwrite. The LLM (via
       ``update_companion_tests`` or by submitting candidate
       inputs with companion code) gets the final word.
    2. ``supplied_body`` is None and ``path`` doesn't exist → write
       the scaffold. First-iteration default.
    3. ``supplied_body`` is None and ``path`` exists → no-op.
       Preserves prior LLM refinement across iterations.

    ``scaffold_factory`` is a zero-arg callable so the (potentially
    expensive-string) scaffold isn't built when we hit the no-op
    branch. ``kind`` is a short label used only in the log line.
    """
    if supplied_body is not None:
        path.write_text(supplied_body, encoding="utf-8")
        logger.info(
            "wrote llm-supplied %s path=%s bytes=%d",
            kind,
            path,
            len(supplied_body),
        )
        return
    if path.exists():
        logger.info(
            "preserved existing %s path=%s bytes=%d",
            kind,
            path,
            path.stat().st_size,
        )
        return
    body = scaffold_factory()  # type: ignore[operator]
    path.write_text(body, encoding="utf-8")
    logger.info(
        "wrote scaffold %s path=%s bytes=%d", kind, path, len(body)
    )


# --------------------------------------------------------------------------
# Dependency preflight activity. The slice-12 gap-closing check —
# returns a structured ValidatorResult so the workflow's existing
# fail-fast handling works uniformly. No subprocess.
# --------------------------------------------------------------------------


def _check_binaries(services_root: Path) -> tuple[list[str], list[str]]:
    """Return (present, missing) binary names for the four validators.

    Pulled out into a sync helper so the prism-mcp-setup preflight
    can share the exact same check without going through the
    Temporal activity dispatch layer.
    """
    bin_dir = services_root / "node_modules" / ".bin"
    present: list[str] = []
    missing: list[str] = []
    for name in _REQUIRED_VALIDATOR_BINARIES:
        path = bin_dir / name
        if path.exists() and os.access(path, os.X_OK):
            present.append(name)
        else:
            missing.append(name)
    return present, missing


def playwright_browsers_dir() -> Path:
    """Resolve the directory Playwright stores browser engines under.

    Resolution order (matches Playwright's own logic):

    1. ``PLAYWRIGHT_BROWSERS_PATH`` env var, *if* it is set and not
       the special string ``"0"``. The ``"0"`` value tells Playwright
       to store browsers inside ``services/node_modules/playwright/.local-browsers/``
       — we don't handle that edge case here because the workflow's
       normal operator setup (and Cursor's MCP sandbox) never use it.
    2. macOS default: ``~/Library/Caches/ms-playwright/``.
    3. Windows default: ``%LOCALAPPDATA%\\ms-playwright``.
    4. Linux + other unices default: ``~/.cache/ms-playwright``.

    Exposed (non-underscored) because the prism-mcp-setup preflight
    surfaces the resolved path to the operator — they need to know
    where to point ``ls`` if they want to debug a partial install.
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", str(Path.home()))
        return Path(local_app_data) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _check_playwright_browsers(
    cache_dir: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Return (present, missing) engine names for the three browsers.

    Playwright stores each engine in a ``<engine>-<version>/`` subdir
    of the cache root (e.g. ``chromium-1194/``, ``firefox-1495/``,
    ``webkit-2215/``). We don't care about the version, only that
    *some* subdir whose name starts with the engine exists — that
    matches Playwright's own resolution: as long as the configured
    engine version's directory exists, ``browserType.launch`` will
    succeed.

    Args:
        cache_dir: optional override. Tests pass a ``tmp_path``;
            production callers leave it ``None`` to use
            :func:`playwright_browsers_dir`.
    """
    cache = cache_dir if cache_dir is not None else playwright_browsers_dir()
    if not cache.is_dir():
        return [], list(_REQUIRED_PLAYWRIGHT_BROWSERS)
    available: set[str] = set()
    for child in cache.iterdir():
        if not child.is_dir():
            continue
        # Subdir name is "<engine>-<version>" — split on the first
        # dash so "chromium-headless-shell-1194" still maps to
        # "chromium" rather than the verbatim engine literal.
        head = child.name.split("-", 1)[0]
        available.add(head)
    present = [b for b in _REQUIRED_PLAYWRIGHT_BROWSERS if b in available]
    missing = [b for b in _REQUIRED_PLAYWRIGHT_BROWSERS if b not in available]
    return present, missing


_DEPENDENCIES_OK_TEMPLATE = (
    "All required JS validator binaries present + executable: {bin_names}.\n"
    "  services_root={services_root}\n"
    "  bin_dir={bin_dir}\n"
    "All required Playwright browsers installed: {browser_names}.\n"
    "  playwright_cache={browser_cache}\n"
)


_BIN_MISSING_LINE = (
    "Missing required JS validator binaries: {missing}.\n"
    "  bin_dir={bin_dir}\n"
    "  Fix: cd {services_root} && npm install\n"
    "  (If npm install hits TLS / 404, see scripts/build_canaveral_ca_bundle.sh\n"
    "   and the canaveral-npm block in services/.npmrc.)\n"
)


_BROWSER_MISSING_LINE = (
    "Missing required Playwright browsers: {missing}.\n"
    "  playwright_cache={browser_cache}\n"
    "  Fix: cd {services_root} && ./node_modules/.bin/playwright install\n"
    "  (One-time, ~600MB; honours PLAYWRIGHT_BROWSERS_PATH if set.)\n"
)


_DEPENDENCIES_REMEDIATION_FOOTER = (
    "You can also run `prism-mcp-setup` for the full preflight diagnostic.\n"
)


@activity.defn
async def check_dependencies_installed(ctx: ServicesContext) -> ValidatorResult:
    """Verify every operator-level dependency the chain needs.

    Two probes, both pure filesystem:

    1. The four ``node_modules/.bin/`` binaries
       (``tsc``, ``eslint``, ``jest``, ``playwright``) exist + are
       executable.
    2. Every Playwright browser engine
       (``chromium``, ``firefox``, ``webkit``) is downloaded into
       the cache directory (default ``~/Library/Caches/ms-playwright``
       on macOS, ``~/.cache/ms-playwright`` on Linux, overridable
       via ``PLAYWRIGHT_BROWSERS_PATH``).

    Runs *first* in the workflow's fail-fast chain. Without it,
    missing binaries surface as ``ENOENT`` from ``subprocess`` and
    missing browsers surface as ``browserType.launch: Executable
    doesn't exist`` deep inside Playwright's test-runner stack —
    both confusing for the LLM. With it, the workflow's reflection
    prompt gets a single clean "install X" line.

    Args:
        ctx (ServicesContext): the services-root pointer. Same shape
            as every other ``run_<validator>`` activity so the
            workflow can iterate the chain uniformly.

    Returns:
        ValidatorResult: ``kind=ValidatorKind.dependencies``,
        ``exit_code=0`` when *both* probes pass, ``exit_code=1``
        otherwise. The remediation hint lives in ``stdout_tail``
        (not ``stderr_tail``) because the workflow's reflection
        prompt uses ``stdout_tail[:400]`` as the per-validator
        excerpt the LLM sees.
    """
    started = time.monotonic()
    services_root = Path(ctx.services_root)
    bin_present, bin_missing = _check_binaries(services_root)
    browser_cache = playwright_browsers_dir()
    browsers_present, browsers_missing = _check_playwright_browsers(
        browser_cache
    )
    elapsed_ms = int((time.monotonic() - started) * 1_000)
    bin_dir = services_root / "node_modules" / ".bin"

    if not bin_missing and not browsers_missing:
        message = _DEPENDENCIES_OK_TEMPLATE.format(
            bin_names=", ".join(bin_present),
            services_root=services_root,
            bin_dir=bin_dir,
            browser_names=", ".join(browsers_present),
            browser_cache=browser_cache,
        )
        logger.info(
            "dependencies check passed services_root=%s "
            "binaries=%s browsers=%s",
            services_root,
            bin_present,
            browsers_present,
        )
        return ValidatorResult(
            kind=ValidatorKind.dependencies,
            exit_code=0,
            stdout_tail=message,
            stderr_tail="",
            duration_ms=elapsed_ms,
        )

    # Build the failure message in deterministic order so the
    # reflection prompt (which truncates at 400 chars) consistently
    # surfaces the most-actionable hint first.
    parts: list[str] = []
    if bin_missing:
        parts.append(
            _BIN_MISSING_LINE.format(
                missing=", ".join(bin_missing),
                services_root=services_root,
                bin_dir=bin_dir,
            )
        )
    if browsers_missing:
        parts.append(
            _BROWSER_MISSING_LINE.format(
                missing=", ".join(browsers_missing),
                services_root=services_root,
                browser_cache=browser_cache,
            )
        )
    parts.append(_DEPENDENCIES_REMEDIATION_FOOTER)
    message = "".join(parts)
    logger.warning(
        "dependencies check failed services_root=%s "
        "missing_bin=%s missing_browsers=%s",
        services_root,
        bin_missing,
        browsers_missing,
    )
    return ValidatorResult(
        kind=ValidatorKind.dependencies,
        exit_code=1,
        stdout_tail=message,
        stderr_tail="",
        duration_ms=elapsed_ms,
    )


# --------------------------------------------------------------------------
# Subprocess helper — single chokepoint so every run_<validator>
# activity behaves identically (capture, time, tail-truncate, return).
# --------------------------------------------------------------------------


async def _run_validator(
    *,
    kind: ValidatorKind,
    argv: list[str],
    cwd: str,
) -> ValidatorResult:
    """Spawn ``argv[0] argv[1:]`` and package the result.

    Centralising the subprocess shape here means each ``run_X``
    wrapper stays a thin builder of its own ``argv`` and the test
    suite only needs to mock :func:`asyncio.create_subprocess_exec`
    once to cover every validator.

    We invoke validator binaries directly out of
    ``node_modules/.bin/`` (not via ``npm run X``) for three
    reasons:

    1. **Scope**: ``npm run X`` runs the package.json script,
       which validates the *whole* Prism codebase. We need to
       scope to the candidate sub-tree.
    2. **Speed**: skipping the npm wrapper saves ~200ms of
       process startup per iteration.
    3. **Errors**: a missing binary surfaces as a clear
       FileNotFoundError instead of npm's wrapped error format.
    """
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    elapsed_ms = int((time.monotonic() - started) * 1_000)
    logger.info(
        "ran validator kind=%s argv=%s exit=%s duration_ms=%d",
        kind.value,
        argv,
        proc.returncode,
        elapsed_ms,
    )
    return ValidatorResult(
        kind=kind,
        # subprocess returncode is Optional[int] in stubs; default
        # to a sentinel non-zero so the workflow treats unexpected
        # state as a failure instead of a silent pass.
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout_tail=stdout_bytes.decode("utf-8", errors="replace"),
        stderr_tail=stderr_bytes.decode("utf-8", errors="replace"),
        duration_ms=elapsed_ms,
    )


def _bin(services_root: str | Path, name: str) -> str:
    """Return the relative path to a ``node_modules/.bin/`` binary.

    Returned as ``"./node_modules/.bin/<name>"`` so it works under
    ``cwd=services_root`` on every platform. Absolute paths would
    also work but they'd embed the operator's home directory in
    every log line.
    """
    del services_root
    return f"./node_modules/.bin/{name}"


# --------------------------------------------------------------------------
# Subprocess-backed validator activities. Each delegates to
# _run_validator with a scoped argv targeting just the candidate's
# scratch directory.
# --------------------------------------------------------------------------


@activity.defn
async def run_typecheck(ctx: ServicesContext) -> ValidatorResult:
    """Run ``tsc --noEmit -p <scratch>/<Name>/tsconfig.json``.

    The per-``<Name>`` tsconfig (written by
    :func:`write_candidate_files`) extends the project-wide one but
    scopes ``include`` to just the candidate directory. That keeps
    pre-existing tsc errors elsewhere in the Prism codebase out of
    the LLM's feedback channel.

    Cheapest validator + catches the largest class of bugs (missing
    props, wrong types) — runs first among the subprocess validators
    in the fail-fast chain (the ``dependencies`` check runs before it).
    """
    scratch = _scratch_dir_rel(ctx.component_name)
    return await _run_validator(
        kind=ValidatorKind.typecheck,
        argv=[
            _bin(ctx.services_root, "tsc"),
            "--noEmit",
            "-p",
            f"{scratch}/tsconfig.json",
        ],
        cwd=ctx.services_root,
    )


@activity.defn
async def run_eslint(ctx: ServicesContext) -> ValidatorResult:
    """Run ESLint over just the candidate directory.

    Uses the same ``eslint/eslint.prod.json`` config + same
    ``--ext`` glob set + same ``--max-warnings 0`` strictness as
    Prism's own ``npm run eslint`` script — only the *path*
    argument differs. That keeps the candidate held to the
    library's own quality bar without surfacing the hundreds of
    pre-existing warnings elsewhere in the codebase.
    """
    scratch = _scratch_dir_rel(ctx.component_name)
    return await _run_validator(
        kind=ValidatorKind.eslint,
        argv=[
            _bin(ctx.services_root, "eslint"),
            "--config",
            "eslint/eslint.prod.json",
            scratch,
            "--ext",
            ".js,.jsx,.ts,.tsx",
            "--max-warnings",
            "0",
        ],
        cwd=ctx.services_root,
    )


@activity.defn
async def run_jest(ctx: ServicesContext) -> ValidatorResult:
    """Run Jest over just the candidate directory.

    ``--testPathPattern`` filters Jest's default discovery to files
    under the scratch dir. ``--passWithNoTests`` makes the activity
    benign when the AlphaCodium AI-test stage didn't produce a
    companion test — the workflow can still iterate on tsc/eslint
    feedback without Jest failing the round outright.
    """
    scratch = _scratch_dir_rel(ctx.component_name)
    return await _run_validator(
        kind=ValidatorKind.jest,
        argv=[
            _bin(ctx.services_root, "jest"),
            "--testPathPattern",
            scratch,
            "--passWithNoTests",
        ],
        cwd=ctx.services_root,
    )


@activity.defn
async def run_playwright_axe(ctx: ServicesContext) -> ValidatorResult:
    """Run Playwright tests scoped to the candidate directory.

    The :func:`write_candidate_files` activity always seeds a
    pwspec at iteration 1 (auto-scaffold) so Playwright's "no
    tests found" failure mode is no longer reachable —
    accordingly we no longer pass ``--pass-with-no-tests``. If
    the LLM later refines the pwspec via ``update_companion_tests``
    and introduces real axe-core / visual assertions, those
    failures bubble up as actionable feedback the LLM can act
    on without needing the no-tests safety net.

    A pwspec typically pairs ``@axe-core/playwright`` assertions
    with visual / interaction expectations, so a single
    Playwright run covers both the a11y panel and the
    per-component behaviour. Auto-scaffolds start with a smoke
    test only; real axe checks come once the LLM upgrades the
    pwspec.
    """
    scratch = _scratch_dir_rel(ctx.component_name)
    return await _run_validator(
        kind=ValidatorKind.playwright_axe,
        argv=[
            _bin(ctx.services_root, "playwright"),
            "test",
            scratch,
        ],
        cwd=ctx.services_root,
    )


# --------------------------------------------------------------------------
# SSIM activity — wraps the pure helper. No subprocess.
# --------------------------------------------------------------------------


@activity.defn
async def materialise_figma_reference(
    input: FigmaReferenceInput,
) -> FigmaReferenceResult:
    """Resolve a Figma PNG reference to an on-disk path **once per workflow**.

    Called from the workflow's ``run`` entrypoint *before* the
    first ``submit_candidate`` lands. The result path is cached
    on the workflow instance and reused by every iteration's SSIM
    activity, so the network/decode cost is paid at most once
    per workflow regardless of how many iterations run.

    Branches:

    * ``figma_png_path`` set → returns it verbatim. No I/O.
    * ``figma_png_url`` set → :func:`materialise_image` downloads
      to ``tempfile.gettempdir()`` and we return that path.
    * ``figma_png_base64`` set → :func:`materialise_image`
      decodes to ``tempfile.gettempdir()``.
    * Nothing set → returns ``path=None``, ``source="none"``.
      The workflow should never invoke the activity in this
      branch (the ``has_figma_reference`` gate prevents it),
      but we handle it defensively so a stray call surfaces
      cleanly instead of as an unhandled exception.

    Args:
        input (FigmaReferenceInput): one of three input shapes.

    Returns:
        FigmaReferenceResult: the resolved path + which input
        branch was used (for logging and the ``source`` field
        the workflow surfaces in its status snapshot).
    """
    if input.figma_png_path is not None:
        return FigmaReferenceResult(
            path=input.figma_png_path,
            source="path",
        )
    if input.figma_png_url is not None:
        resolved = materialise_image(url=input.figma_png_url)
        logger.info(
            "materialised figma reference url=%s path=%s",
            input.figma_png_url,
            resolved,
        )
        return FigmaReferenceResult(path=str(resolved), source="url")
    if input.figma_png_base64 is not None:
        resolved = materialise_image(base64_data=input.figma_png_base64)
        logger.info(
            "materialised figma reference base64 bytes=%d path=%s",
            len(input.figma_png_base64),
            resolved,
        )
        return FigmaReferenceResult(path=str(resolved), source="base64")
    return FigmaReferenceResult(path=None, source="none")


@activity.defn
async def run_ssim_compare(input: SsimInput) -> SsimVerdict:
    """Compute the SSIM verdict for two PNG paths on disk.

    Thin async wrapper around :func:`compute_ssim_from_paths` so
    the workflow can ``await`` it like any other activity. The
    SSIM math is CPU-bound and quick (~50ms on a 1280x800 image),
    so we don't bother offloading to a thread pool.

    Both paths must exist before this activity is invoked. The
    workflow gates the call on :func:`check_rendered_exists` so
    a missing ``rendered_png_path`` yields a clean
    ``ssim_skip_reason`` instead of an unhandled
    :class:`FileNotFoundError`.
    """
    return compute_ssim_from_paths(
        figma_png=Path(input.figma_png_path),
        rendered_png=Path(input.rendered_png_path),
    )


@activity.defn
async def check_rendered_exists(
    input: RenderedExistsInput,
) -> RenderedExistsResult:
    """Verify the templated rendered PNG exists before SSIM runs.

    The Slice 12 SSIM stage assumes Playwright wrote a screenshot
    at ``{services_root}/playwright-output/{component_name}.png``
    by the time all subprocess validators have passed. The
    auto-scaffolded pwspec is a smoke test that doesn't navigate
    or capture, so on iteration 1 the file is typically absent.
    Without this gate, :func:`run_ssim_compare` raises
    :class:`FileNotFoundError` and the workflow crashes — robbing
    the LLM of any actionable signal. This pre-check converts the
    crash into a clean ``ssim_skip_reason="rendered_unavailable"``
    that the reflection prompt can act on.

    The activity is deliberately tiny (single ``Path.exists()``
    call) so it pays its registration cost without adding
    measurable latency. Wrapping it in an activity rather than
    inlining a check at workflow scope is required by Temporal's
    sandbox: workflow code can't touch the filesystem directly.
    """
    target = Path(input.rendered_png_path)
    exists = target.is_file()
    logger.info(
        "checked rendered png path=%s exists=%s", input.rendered_png_path, exists
    )
    return RenderedExistsResult(
        rendered_png_path=input.rendered_png_path,
        exists=exists,
    )
