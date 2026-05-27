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
from prism_mcp.workflow.ssim import compute_ssim_from_paths

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
        companion_test_code (str | None): optional pwspec.ts body
            produced by the AlphaCodium AI-test stage. ``None``
            skips writing the test file.
    """

    model_config = ConfigDict(extra="forbid")

    services_root: str
    component_name: str
    jsx_code: str
    companion_test_code: str | None = None


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


# --------------------------------------------------------------------------
# Filesystem activity — writes generated code to the scratch tree.
# --------------------------------------------------------------------------


@activity.defn
async def write_candidate_files(input: CandidateInput) -> ServicesContext:
    """Materialise ``input.jsx_code`` (and optional pwspec) on disk.

    The destination layout matches the slice-12 design decision:

    ``services/src/scratch/Generated/<Name>/<Name>.jsx``
    ``services/src/scratch/Generated/<Name>/<Name>.pwspec.ts`` (optional)
    ``services/src/scratch/Generated/<Name>/tsconfig.json`` (always)

    The scoped ``tsconfig.json`` is what lets :func:`run_typecheck`
    invoke ``tsc -p <scratch>/tsconfig.json`` and only see this
    candidate's files. Without it tsc would pick up the project-wide
    ``services/tsconfig.json`` and surface every pre-existing error
    in the Prism codebase on top of the candidate's own errors.

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

    if input.companion_test_code is not None:
        pwspec_path = dest_dir / f"{input.component_name}.pwspec.ts"
        pwspec_path.write_text(input.companion_test_code, encoding="utf-8")
        logger.info(
            "wrote companion pwspec path=%s bytes=%d",
            pwspec_path,
            len(input.companion_test_code),
        )
    return ServicesContext(
        services_root=input.services_root,
        component_name=input.component_name,
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

    A pwspec written by the AlphaCodium AI-test stage typically
    pairs ``@axe-core/playwright`` assertions with visual /
    interaction expectations, so a single Playwright run covers
    both the a11y panel and the per-component behaviour.

    Skips Prism's full styleguide rebuild — pwspec authors are
    expected to either mount the component themselves or point at
    an already-built styleguide URL via ``baseURL``.

    Passing ``--pass-with-no-tests``
    --------------------------------

    The AlphaCodium AI-test stage is *optional* — early iterations
    often produce JSX without a companion ``pwspec.ts``. Without
    the flag, Playwright exits non-zero on "no tests found" and
    short-circuits the iteration on a non-actionable error: the
    LLM cannot iterate its way out of "no tests" because writing
    tests is a separate concern from fixing the component. With
    ``--pass-with-no-tests`` (built-in since Playwright 1.43) the
    Playwright run becomes benign in that case — the validator
    panel still has a fresh row, but the LLM is free to push
    typecheck/eslint/jest fixes first and add pwspec content in a
    later round.
    """
    scratch = _scratch_dir_rel(ctx.component_name)
    return await _run_validator(
        kind=ValidatorKind.playwright_axe,
        argv=[
            _bin(ctx.services_root, "playwright"),
            "test",
            scratch,
            "--pass-with-no-tests",
        ],
        cwd=ctx.services_root,
    )


# --------------------------------------------------------------------------
# SSIM activity — wraps the pure helper. No subprocess.
# --------------------------------------------------------------------------


@activity.defn
async def run_ssim_compare(input: SsimInput) -> SsimVerdict:
    """Compute the SSIM verdict for two PNG paths on disk.

    Thin async wrapper around :func:`compute_ssim_from_paths` so
    the workflow can ``await`` it like any other activity. The
    SSIM math is CPU-bound and quick (~50ms on a 1280x800 image),
    so we don't bother offloading to a thread pool.
    """
    return compute_ssim_from_paths(
        figma_png=Path(input.figma_png_path),
        rendered_png=Path(input.rendered_png_path),
    )
