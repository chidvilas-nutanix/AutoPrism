"""Tests for slice-12 Temporal activities.

Activities are the *only* place the workflow is allowed to touch
the filesystem, the network, or spawn subprocesses — workflow code
itself must stay deterministic. So these tests focus on the things
that can go wrong at the activity layer:

* **Subprocess invocation**: each ``run_<validator>`` activity must
  invoke the right ``./node_modules/.bin/<bin>`` argv, **scoped**
  to ``src/scratch/Generated/<Name>/`` (not the whole codebase),
  with ``cwd=services_root``.
* **Exit-code mapping**: 0 → ok=True, non-zero → ok=False, no
  silent swallowing.
* **Tail truncation**: the contract enforces a 4 KB tail-cap, but
  the activity must also *capture* both streams so we never lose
  the error excerpt.
* **Timing**: ``duration_ms`` must be a positive integer (the
  workflow surfaces it in the reflection prompt; bogus zeros would
  confuse the LLM).
* **Workspace setup**: ``write_candidate_files`` writes JSX +
  pwspec.ts + a scoped ``tsconfig.json`` to
  ``services/src/scratch/Generated/<Name>/`` — we verify exact
  paths and contents so the validators can find them.
* **Dependency preflight**: ``check_dependencies_installed`` is the
  slice-12 gap-closing first link of the chain. It must short-
  circuit cleanly when ``node_modules/.bin/`` is empty, with a
  remediation hint in ``stdout_tail``.
* **SSIM bridge**: ``run_ssim_compare`` delegates to the pure
  :mod:`prism_mcp.workflow.ssim` module and returns the right
  :class:`SsimVerdict` shape.

Subprocess calls are mocked end-to-end via :func:`unittest.mock.patch`
on :func:`asyncio.create_subprocess_exec` — no node, no npm, no
real filesystem outside ``tmp_path``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from prism_mcp.workflow.activities import (
    _REQUIRED_PLAYWRIGHT_BROWSERS,
    CandidateInput,
    ServicesContext,
    SsimInput,
    check_dependencies_installed,
    playwright_browsers_dir,
    run_eslint,
    run_jest,
    run_playwright_axe,
    run_ssim_compare,
    run_typecheck,
    write_candidate_files,
)
from prism_mcp.workflow.contracts import (
    SsimVerdict,
    ValidatorKind,
)

# --------------------------------------------------------------------------
# Subprocess-mocking helper. Returns a fake (proc, communicate) pair so
# the activity sees a normal subprocess interface.
# --------------------------------------------------------------------------


class _FakeProc:
    """Minimal stand-in for :class:`asyncio.subprocess.Process`."""

    def __init__(self, *, exit_code: int, stdout: bytes, stderr: bytes):
        self.returncode = exit_code
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _patch_subprocess(
    *,
    exit_code: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
):
    """Patch ``asyncio.create_subprocess_exec`` for one test.

    Returns the :class:`AsyncMock` so the test can inspect the
    call's args (the ``cwd`` kwarg + positional ``argv`` in
    particular).
    """
    fake = AsyncMock(
        return_value=_FakeProc(
            exit_code=exit_code, stdout=stdout, stderr=stderr
        )
    )
    return patch(
        "prism_mcp.workflow.activities.asyncio.create_subprocess_exec",
        fake,
    )


def _make_services_root_with_bins(
    tmp_path: Path, *, names: tuple[str, ...] | None = None
) -> Path:
    """Materialise a fake ``services/node_modules/.bin/<name>`` tree.

    Each binary is an empty executable so the
    :func:`check_dependencies_installed` activity's
    :func:`os.access(X_OK)` probe returns ``True``. ``names``
    defaults to the full four-binary set; tests pass a subset to
    exercise the missing-binary path.
    """
    if names is None:
        names = ("tsc", "eslint", "jest", "playwright")
    services_root = tmp_path / "services"
    bin_dir = services_root / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return services_root


def _make_playwright_browsers_dir(
    tmp_path: Path,
    *,
    engines: tuple[str, ...] | None = None,
) -> Path:
    """Materialise a fake Playwright browsers cache.

    Playwright stores each engine in a ``<engine>-<version>/``
    subdir under the cache root. Tests don't care about the
    version, only that the right engine subdir exists, so we
    pick a stable placeholder.

    Returns the cache root so tests can ``monkeypatch.setenv``
    ``PLAYWRIGHT_BROWSERS_PATH`` to it.
    """
    if engines is None:
        engines = _REQUIRED_PLAYWRIGHT_BROWSERS
    cache = tmp_path / "playwright-cache"
    cache.mkdir(parents=True, exist_ok=True)
    for engine in engines:
        (cache / f"{engine}-9999").mkdir()
    return cache


@pytest.fixture
def _all_browsers_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point Playwright at a tmp cache with all three engines.

    The vast majority of activity tests don't care about the
    browser check — they just want the ``check_dependencies_installed``
    probe to not fail on browsers so they can focus on the binary
    probe (or on other activities entirely). Using a per-test
    ``PLAYWRIGHT_BROWSERS_PATH`` env override keeps the suite
    hermetic regardless of whether the developer's laptop has
    Playwright browsers installed.
    """
    cache = _make_playwright_browsers_dir(tmp_path)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    return cache


# --------------------------------------------------------------------------
# write_candidate_files: writes JSX + optional pwspec + scoped tsconfig
# into services/src/scratch/Generated/<Name>/.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_candidate_files_creates_jsx(tmp_path: Path) -> None:
    """JSX always written to ``<Name>.jsx`` under the scratch dir."""
    services_root = tmp_path / "services"
    services_root.mkdir()

    ctx = await write_candidate_files(
        CandidateInput(
            services_root=str(services_root),
            component_name="ConfirmationModal",
            jsx_code="import {Modal} from '@nutanix-ui/prism-reactjs';\n",
            companion_test_code=None,
        )
    )

    jsx_path = (
        services_root
        / "src"
        / "scratch"
        / "Generated"
        / "ConfirmationModal"
        / "ConfirmationModal.jsx"
    )
    assert jsx_path.is_file()
    assert "Modal" in jsx_path.read_text(encoding="utf-8")
    assert ctx.component_name == "ConfirmationModal"
    assert ctx.services_root == str(services_root)


@pytest.mark.asyncio
async def test_write_candidate_files_creates_scoped_tsconfig(
    tmp_path: Path,
) -> None:
    """Every candidate dir gets a ``tsconfig.json`` that extends
    the project tsconfig but scopes ``include`` to just the
    candidate sub-tree. Without it ``tsc -p`` would have nothing
    to anchor to and either fall back to the whole codebase or
    error out — both regressions of the slice-12 contract.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()

    await write_candidate_files(
        CandidateInput(
            services_root=str(services_root),
            component_name="Btn",
            jsx_code="export const Btn = () => null;\n",
            companion_test_code=None,
        )
    )

    tsconfig_path = (
        services_root
        / "src"
        / "scratch"
        / "Generated"
        / "Btn"
        / "tsconfig.json"
    )
    assert tsconfig_path.is_file()
    body = tsconfig_path.read_text(encoding="utf-8")
    # Four-level climb back to services/tsconfig.json.
    assert "../../../../tsconfig.json" in body
    # Scope-down to the candidate dir.
    assert "./**/*.tsx" in body
    assert "./**/*.jsx" in body
    # Pwspec exclusion so typecheck doesn't choke on Playwright-only types.
    assert "*.pwspec.ts" in body


@pytest.mark.asyncio
async def test_write_candidate_files_creates_companion_pwspec(
    tmp_path: Path,
) -> None:
    """When supplied, the pwspec lives alongside the JSX.

    The AlphaCodium AI-test stage places the test next to the
    component so Playwright's globbing picks it up automatically.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()

    await write_candidate_files(
        CandidateInput(
            services_root=str(services_root),
            component_name="X",
            jsx_code="// jsx body\n",
            companion_test_code="// pwspec body\n",
        )
    )

    pwspec_path = (
        services_root / "src" / "scratch" / "Generated" / "X" / "X.pwspec.ts"
    )
    assert pwspec_path.is_file()
    assert "pwspec body" in pwspec_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_write_candidate_files_overwrites_previous_iteration(
    tmp_path: Path,
) -> None:
    """A second iteration's submission overwrites the first.

    The workflow runs ``write_candidate_files`` once per
    ``submit_candidate`` — we must replace stale code, not append.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()

    await write_candidate_files(
        CandidateInput(
            services_root=str(services_root),
            component_name="X",
            jsx_code="// first\n",
            companion_test_code=None,
        )
    )
    await write_candidate_files(
        CandidateInput(
            services_root=str(services_root),
            component_name="X",
            jsx_code="// second\n",
            companion_test_code=None,
        )
    )

    jsx_path = services_root / "src" / "scratch" / "Generated" / "X" / "X.jsx"
    body = jsx_path.read_text(encoding="utf-8")
    assert "second" in body
    assert "first" not in body


# --------------------------------------------------------------------------
# check_dependencies_installed: the slice-12 gap-closing preflight.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_dependencies_passes_when_all_binaries_and_browsers_present(
    tmp_path: Path,
    _all_browsers_present: Path,
) -> None:
    """All four binaries + all three browsers present → ``ok=True``."""
    services_root = _make_services_root_with_bins(tmp_path)
    ctx = ServicesContext(services_root=str(services_root), component_name="X")

    result = await check_dependencies_installed(ctx)

    assert result.kind == ValidatorKind.dependencies
    assert result.exit_code == 0
    assert result.ok is True
    # The pass message names every binary AND every browser so the
    # operator can confirm the right cache dir was inspected.
    for name in ("tsc", "eslint", "jest", "playwright"):
        assert name in result.stdout_tail
    for engine in _REQUIRED_PLAYWRIGHT_BROWSERS:
        assert engine in result.stdout_tail


@pytest.mark.asyncio
async def test_check_dependencies_fails_with_remediation_when_bin_missing(
    tmp_path: Path,
    _all_browsers_present: Path,
) -> None:
    """Missing binary → ``ok=False`` + remediation hint in ``stdout_tail``.

    The hint must mention ``npm install`` *and* the prism-mcp-setup
    fallback so the LLM (or the operator reading the workflow
    history) has both the one-line fix and the deeper diagnostic.
    """
    services_root = _make_services_root_with_bins(
        tmp_path, names=("tsc", "eslint")
    )
    ctx = ServicesContext(services_root=str(services_root), component_name="X")

    result = await check_dependencies_installed(ctx)

    assert result.kind == ValidatorKind.dependencies
    assert result.exit_code != 0
    assert result.ok is False
    assert "jest" in result.stdout_tail
    assert "playwright" in result.stdout_tail
    assert "npm install" in result.stdout_tail
    assert "prism-mcp-setup" in result.stdout_tail


@pytest.mark.asyncio
async def test_check_dependencies_fails_when_bin_present_but_not_executable(
    tmp_path: Path,
    _all_browsers_present: Path,
) -> None:
    """A non-executable binary is just as broken as a missing one.

    Operators occasionally end up with a stripped-permissions
    ``node_modules`` after restoring from a tarball or unzipping
    a CI artifact. Treat that case the same as missing so we don't
    silently spawn a "Permission denied" later.
    """
    services_root = _make_services_root_with_bins(tmp_path)
    bin_path = services_root / "node_modules" / ".bin" / "jest"
    mode = bin_path.stat().st_mode
    bin_path.chmod(mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    assert not os.access(bin_path, os.X_OK)

    ctx = ServicesContext(services_root=str(services_root), component_name="X")
    result = await check_dependencies_installed(ctx)

    assert result.ok is False
    assert "jest" in result.stdout_tail


@pytest.mark.asyncio
async def test_check_dependencies_fails_when_node_modules_missing(
    tmp_path: Path,
    _all_browsers_present: Path,
) -> None:
    """``services/node_modules`` doesn't exist at all → ``ok=False``."""
    services_root = tmp_path / "services"
    services_root.mkdir()
    ctx = ServicesContext(services_root=str(services_root), component_name="X")

    result = await check_dependencies_installed(ctx)

    assert result.ok is False
    for name in ("tsc", "eslint", "jest", "playwright"):
        assert name in result.stdout_tail


# --------------------------------------------------------------------------
# check_dependencies_installed: Playwright-browser probe (slice-12
# follow-on gap — the binary exists but its browser engines don't).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_dependencies_fails_when_browsers_cache_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache directory does not exist → all three browsers reported missing.

    This is the exact shape of the operator-error that motivated
    the probe: ``playwright`` JS package installed (via ``npm install``)
    but ``npx playwright install`` never run, so the cache root is
    absent and every engine launches with ``Executable doesn't exist``.
    """
    services_root = _make_services_root_with_bins(tmp_path)
    monkeypatch.setenv(
        "PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "does-not-exist")
    )
    ctx = ServicesContext(services_root=str(services_root), component_name="X")

    result = await check_dependencies_installed(ctx)

    assert result.ok is False
    for engine in _REQUIRED_PLAYWRIGHT_BROWSERS:
        assert engine in result.stdout_tail
    assert "playwright install" in result.stdout_tail


@pytest.mark.asyncio
async def test_check_dependencies_fails_on_partial_browser_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only chromium present → firefox + webkit reported as missing.

    Operators sometimes install a single engine via
    ``npx playwright install chromium`` to save bandwidth. The
    Prism playwright config defines all three projects, so a
    ``playwright test`` invocation will still fail for any missing
    engine — fail-fast here surfaces exactly which engines need
    to be installed.
    """
    services_root = _make_services_root_with_bins(tmp_path)
    cache = _make_playwright_browsers_dir(tmp_path, engines=("chromium",))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    ctx = ServicesContext(services_root=str(services_root), component_name="X")

    result = await check_dependencies_installed(ctx)

    assert result.ok is False
    assert "firefox" in result.stdout_tail
    assert "webkit" in result.stdout_tail
    assert "playwright install" in result.stdout_tail


@pytest.mark.asyncio
async def test_check_dependencies_browser_probe_ignores_version_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe matches the engine *prefix*, not an exact version.

    Cache subdirs are named ``<engine>-<version>`` (e.g.
    ``chromium-1194``); the probe must succeed regardless of the
    version Playwright happens to be pinned to in this Prism
    snapshot. The ``chromium-headless-shell-1194`` subdir that
    Playwright sometimes adds alongside is treated as the same
    engine.
    """
    services_root = _make_services_root_with_bins(tmp_path)
    cache = tmp_path / "playwright-cache"
    cache.mkdir()
    (cache / "chromium-1194").mkdir()
    (cache / "chromium-headless-shell-1194").mkdir()
    (cache / "firefox-1495").mkdir()
    (cache / "webkit-2215").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    ctx = ServicesContext(services_root=str(services_root), component_name="X")

    result = await check_dependencies_installed(ctx)

    assert result.ok is True


@pytest.mark.asyncio
async def test_check_dependencies_combined_failure_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binaries *and* browsers both missing → both hints in one message.

    The reflection prompt truncates ``stdout_tail`` at 400 chars,
    so the binary hint goes first (it's the more fundamental fix
    — without ``node_modules`` the operator can't run
    ``playwright install`` via the ``./node_modules/.bin/`` path).
    """
    services_root = tmp_path / "services"
    services_root.mkdir()
    monkeypatch.setenv(
        "PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers")
    )
    ctx = ServicesContext(services_root=str(services_root), component_name="X")

    result = await check_dependencies_installed(ctx)

    assert result.ok is False
    assert "npm install" in result.stdout_tail
    assert "playwright install" in result.stdout_tail
    npm_idx = result.stdout_tail.index("npm install")
    pw_idx = result.stdout_tail.index("playwright install")
    assert npm_idx < pw_idx


# --------------------------------------------------------------------------
# playwright_browsers_dir: env-var-aware platform default.
# --------------------------------------------------------------------------


def test_playwright_browsers_dir_honours_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``PLAYWRIGHT_BROWSERS_PATH`` takes precedence over platform default."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert playwright_browsers_dir() == tmp_path


def test_playwright_browsers_dir_ignores_zero_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playwright treats ``"0"`` as "store inside node_modules"; we
    fall back to the platform default in that case so we don't end
    up probing a path Playwright wouldn't actually use either.
    """
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    result = playwright_browsers_dir()
    assert "ms-playwright" in str(result)


# --------------------------------------------------------------------------
# run_typecheck / run_eslint / run_jest / run_playwright_axe:
# each invokes the right binary, scoped to the candidate sub-tree,
# with cwd=services_root.
# --------------------------------------------------------------------------


_SCRATCH_REL = "src/scratch/Generated/Btn"
"""The relative path every validator must scope itself to."""


@pytest.mark.asyncio
async def test_run_typecheck_invokes_scoped_tsc(tmp_path: Path) -> None:
    """``run_typecheck`` must invoke ``./node_modules/.bin/tsc
    --noEmit -p <scratch>/<Name>/tsconfig.json`` — *not* the
    project-wide ``npm run typecheck``. The scoping is what
    prevents pre-existing tsc errors elsewhere in the codebase
    from polluting the candidate's feedback.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()
    ctx = ServicesContext(
        services_root=str(services_root), component_name="Btn"
    )

    with _patch_subprocess(
        exit_code=0, stdout=b"tsc ok\n", stderr=b""
    ) as patched:
        result = await run_typecheck(ctx)

    patched.assert_called_once()
    args, kwargs = patched.call_args
    assert args[0] == "./node_modules/.bin/tsc"
    assert "--noEmit" in args
    assert "-p" in args
    assert f"{_SCRATCH_REL}/tsconfig.json" in args
    assert kwargs["cwd"] == str(services_root)

    assert result.kind == ValidatorKind.typecheck
    assert result.exit_code == 0
    assert result.ok is True
    assert "tsc ok" in result.stdout_tail
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_run_eslint_invokes_scoped_eslint(tmp_path: Path) -> None:
    """ESLint must use the same config + max-warnings strictness as
    Prism's own script but with the scratch dir as the path arg.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()
    ctx = ServicesContext(
        services_root=str(services_root), component_name="Btn"
    )

    with _patch_subprocess(
        exit_code=1,
        stdout=b"error: unused-vars at line 4\n",
        stderr=b"",
    ) as patched:
        result = await run_eslint(ctx)

    args, kwargs = patched.call_args
    assert args[0] == "./node_modules/.bin/eslint"
    assert "--config" in args
    assert "eslint/eslint.prod.json" in args
    assert _SCRATCH_REL in args
    assert "--max-warnings" in args
    assert "0" in args
    assert kwargs["cwd"] == str(services_root)

    assert result.kind == ValidatorKind.eslint
    assert result.exit_code == 1
    assert result.ok is False
    assert "unused-vars" in result.stdout_tail


@pytest.mark.asyncio
async def test_run_jest_invokes_scoped_jest_with_pass_with_no_tests(
    tmp_path: Path,
) -> None:
    """Jest must filter to the scratch dir via ``--testPathPattern``
    and tolerate a missing companion pwspec via ``--passWithNoTests``
    so tsc/eslint feedback can drive iteration even when the
    AI-test stage didn't produce a spec.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()
    ctx = ServicesContext(
        services_root=str(services_root), component_name="Btn"
    )

    with _patch_subprocess(exit_code=0, stdout=b"", stderr=b"") as patched:
        result = await run_jest(ctx)

    args, _ = patched.call_args
    assert args[0] == "./node_modules/.bin/jest"
    assert "--testPathPattern" in args
    assert _SCRATCH_REL in args
    assert "--passWithNoTests" in args
    assert result.kind == ValidatorKind.jest


@pytest.mark.asyncio
async def test_run_jest_truncates_huge_stdout(tmp_path: Path) -> None:
    """Even a multi-MB jest log gets tail-capped to 4 KB."""
    services_root = tmp_path / "services"
    services_root.mkdir()
    ctx = ServicesContext(services_root=str(services_root), component_name="X")
    huge = b"x" * 20_000

    with _patch_subprocess(exit_code=0, stdout=huge, stderr=b""):
        result = await run_jest(ctx)

    assert len(result.stdout_tail) == 4_000
    assert result.stdout_tail.endswith("x" * 100)


@pytest.mark.asyncio
async def test_run_playwright_axe_invokes_scoped_playwright(
    tmp_path: Path,
) -> None:
    """Playwright runs against the scratch dir directly — no
    full styleguide rebuild, no project-wide pwspec sweep.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()
    ctx = ServicesContext(
        services_root=str(services_root), component_name="Btn"
    )

    with _patch_subprocess(exit_code=0, stdout=b"", stderr=b"") as patched:
        result = await run_playwright_axe(ctx)

    args, _ = patched.call_args
    assert args[0] == "./node_modules/.bin/playwright"
    assert "test" in args
    assert _SCRATCH_REL in args
    assert result.kind == ValidatorKind.playwright_axe


@pytest.mark.asyncio
async def test_run_typecheck_records_duration_ms(tmp_path: Path) -> None:
    """``duration_ms`` is monotonic + non-negative.

    Real subprocess invocations take 100ms+; the mocked invocation
    takes microseconds. Either way the field must be a non-negative
    integer for the reflection prompt's bottleneck-reasoning.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()
    ctx = ServicesContext(services_root=str(services_root), component_name="X")

    with _patch_subprocess(exit_code=0, stdout=b"", stderr=b""):
        result = await run_typecheck(ctx)

    assert isinstance(result.duration_ms, int)
    assert result.duration_ms >= 0


# --------------------------------------------------------------------------
# run_ssim_compare: thin async wrapper around the pure SSIM helper.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ssim_compare_returns_verdict(tmp_path: Path) -> None:
    """Identical PNGs round-trip through the activity to a pass verdict."""
    a = tmp_path / "a.png"
    Image.new("RGB", (64, 64), (200, 200, 200)).save(a)

    verdict = await run_ssim_compare(
        SsimInput(
            figma_png_path=str(a),
            rendered_png_path=str(a),
        )
    )

    assert isinstance(verdict, SsimVerdict)
    assert verdict.bucket == "pass"


@pytest.mark.asyncio
async def test_run_ssim_compare_surfaces_missing_path(
    tmp_path: Path,
) -> None:
    """Bad path → FileNotFoundError, not a fake pass."""
    real = tmp_path / "real.png"
    Image.new("RGB", (32, 32), (255, 255, 255)).save(real)
    missing = tmp_path / "missing.png"

    with pytest.raises(FileNotFoundError):
        await run_ssim_compare(
            SsimInput(
                figma_png_path=str(missing),
                rendered_png_path=str(real),
            )
        )
