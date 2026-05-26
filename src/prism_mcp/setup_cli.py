"""``prism-mcp-setup`` — preflight diagnostic for the slice-12 demo.

This CLI is a **diagnostic only** — it never installs anything. It
inspects the operator's environment and tells them whether the
slice-12 component-generation workflow has every prerequisite it
needs to actually run, and where to look if anything is missing.

The demo operator typically runs three long-lived processes:

1. ``temporal server start-dev --db-filename prism.db`` — the
   workflow engine.
2. ``prism-mcp`` — the stdio MCP server Cursor talks to.
3. ``prism-mcp-worker`` — the Temporal worker that drives the
   AlphaCodium iteration loop.

Each of those processes depends on a different slice of the
toolchain. When a demo silently breaks, the failure usually
surfaces inside the Cursor UI as a cryptic stack trace from
``temporalio``, ``subprocess``, or ``axios`` — three different
runtimes, hard to triangulate.

``prism-mcp-setup`` runs all the boundary checks up front and
prints a single ``REPORT`` block (machine-parseable) plus a
human-readable list of remediation hints. Exit code is ``0``
when every check passes, ``1`` otherwise — so it can be wired
into a ``Makefile`` / CI smoke or just run by hand.

What we check
-------------

1. **Python**: 3.11+ (the project's ``requires-python``).
2. **Node.js**: present + version reported (no hard floor — we
   defer to the operator's CI environment).
3. **npm**: present + version reported.
4. **services directory**: the Prism library checkout exists at
   the path the operator told us about (or the default sibling
   path we infer relative to this repo).
5. **services/node_modules**: exists.
6. **services/node_modules/.bin/{tsc,eslint,jest,playwright}**:
   exist + are executable (same check as the in-workflow
   :func:`check_dependencies_installed` activity, kept in lock-step).
7. **services/.npmrc**: present + points at ``canaveral-npm``
   (the curated mirror that has the full upstream package set,
   not the smaller ``npm-walled-garden`` mirror that lacks
   recent versions).
8. **Canaveral CA bundle**: a PEM bundle is available so npm's
   TLS verification succeeds against Artifactory's self-signed
   chain. We look in the well-known sibling location next to
   the POC repo first.

Each check emits a structured :class:`CheckResult` and the
end-of-run report aggregates them so the operator sees both the
binary pass/fail and the diagnostic detail in one place.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

# Re-export the same constants + helpers the in-workflow check uses
# so the preflight cannot drift away from the runtime check on the
# spelling of the required binaries / browser engines or on where
# Playwright's cache lives.
from prism_mcp.workflow.activities import (
    _REQUIRED_VALIDATOR_BINARIES,
    _check_playwright_browsers,
    playwright_browsers_dir,
)

# --------------------------------------------------------------------------
# CheckResult: structured record per probe.
# --------------------------------------------------------------------------


class CheckStatus(StrEnum):
    """Tri-state per-check outcome.

    * ``ok``: probe passed — nothing for the operator to do.
    * ``warn``: probe passed but a soft expectation isn't met
      (e.g. ``.npmrc`` exists but points at the walled-garden
      mirror instead of ``canaveral-npm``). Demo will work but
      might hit ``ETARGET`` on edge-case packages.
    * ``fail``: probe failed — the demo will not work until
      this is fixed.
    """

    ok = "ok"
    warn = "warn"
    fail = "fail"


@dataclass(frozen=True)
class CheckResult:
    """One probe's outcome plus a human-readable diagnostic.

    Args:
        name: short stable identifier — used as a JSON key.
        status: tri-state outcome (see :class:`CheckStatus`).
        detail: one-line human summary suitable for terminal echo.
        remediation: optional next-step instruction — only present
            when ``status != ok``. Phrased as a copy-pasteable
            shell command when possible.
    """

    name: str
    status: CheckStatus
    detail: str
    remediation: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """Aggregated multi-check report.

    Args:
        services_root: absolute path to the Prism services tree
            we probed (echoed so the operator can confirm the
            tool checked the right checkout).
        checks: per-probe results in declaration order.
        overall: ``fail`` if any check failed, ``warn`` if any
            warned and none failed, otherwise ``ok``.
    """

    services_root: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def overall(self) -> CheckStatus:
        if any(c.status is CheckStatus.fail for c in self.checks):
            return CheckStatus.fail
        if any(c.status is CheckStatus.warn for c in self.checks):
            return CheckStatus.warn
        return CheckStatus.ok

    @property
    def exit_code(self) -> int:
        return 1 if self.overall is CheckStatus.fail else 0


# --------------------------------------------------------------------------
# Probe building blocks. Pure functions over the filesystem +
# subprocess so each is hermetically testable with a tmp_path.
# --------------------------------------------------------------------------


_MIN_PYTHON = (3, 11)
"""Lower bound matching the project's ``requires-python``."""


def check_python() -> CheckResult:
    """Probe: Python >= 3.11."""
    actual = sys.version_info[:2]
    if actual >= _MIN_PYTHON:
        return CheckResult(
            name="python",
            status=CheckStatus.ok,
            detail=(
                f"Python {actual[0]}.{actual[1]} ({platform.python_implementation()})"
            ),
        )
    return CheckResult(
        name="python",
        status=CheckStatus.fail,
        detail=(
            f"Python {actual[0]}.{actual[1]} below required "
            f"{_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}"
        ),
        remediation="Install Python 3.11+ via pyenv or your package manager.",
    )


def _capture_version(binary: str) -> tuple[bool, str]:
    """Return ``(found, version_or_error)`` for ``binary --version``.

    Helper for :func:`check_node` / :func:`check_npm`. We never
    raise — every failure path collapses to a structured
    :class:`CheckResult`.
    """
    found_path = shutil.which(binary)
    if not found_path:
        return False, f"binary {binary!r} not on PATH"
    try:
        completed = subprocess.run(
            [found_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"failed to invoke {binary} --version: {exc}"
    version = (completed.stdout or completed.stderr or "").strip()
    return True, version or "(empty version output)"


def check_node() -> CheckResult:
    """Probe: ``node --version`` returns *something*."""
    found, info = _capture_version("node")
    if not found:
        return CheckResult(
            name="node",
            status=CheckStatus.fail,
            detail=info,
            remediation=(
                "Install Node.js (LTS) via nvm or your package manager. "
                "The Prism library's CI tracks Node 20.x."
            ),
        )
    return CheckResult(
        name="node",
        status=CheckStatus.ok,
        detail=f"node {info}",
    )


def check_npm() -> CheckResult:
    """Probe: ``npm --version`` returns *something*."""
    found, info = _capture_version("npm")
    if not found:
        return CheckResult(
            name="npm",
            status=CheckStatus.fail,
            detail=info,
            remediation=(
                "Install npm (bundled with Node.js). If using nvm, "
                "run ``nvm use --lts``."
            ),
        )
    return CheckResult(
        name="npm",
        status=CheckStatus.ok,
        detail=f"npm {info}",
    )


def check_services_dir(services_root: Path) -> CheckResult:
    """Probe: ``services/`` exists at the resolved path."""
    if not services_root.is_dir():
        return CheckResult(
            name="services_dir",
            status=CheckStatus.fail,
            detail=f"services dir not found: {services_root}",
            remediation=(
                "Pass --services-root <path> or clone "
                "prism-ui-prism-reactjs-lib next to this repo."
            ),
        )
    return CheckResult(
        name="services_dir",
        status=CheckStatus.ok,
        detail=f"services dir present: {services_root}",
    )


def check_node_modules(services_root: Path) -> CheckResult:
    """Probe: ``services/node_modules`` exists."""
    nm = services_root / "node_modules"
    if not nm.is_dir():
        return CheckResult(
            name="node_modules",
            status=CheckStatus.fail,
            detail=f"node_modules missing: {nm}",
            remediation=(
                f"cd {services_root} && npm install\n"
                "  If npm install fails on TLS / 404, see the "
                "canaveral-npm registry block in services/.npmrc and "
                "scripts/build_canaveral_ca_bundle.sh."
            ),
        )
    return CheckResult(
        name="node_modules",
        status=CheckStatus.ok,
        detail=f"node_modules present: {nm}",
    )


def check_validator_binaries(services_root: Path) -> CheckResult:
    """Probe: every validator binary is present + executable.

    Shares its semantics with the in-workflow
    :func:`prism_mcp.workflow.activities.check_dependencies_installed`
    so the preflight and the runtime check can't drift apart on
    which binaries are required.
    """
    bin_dir = services_root / "node_modules" / ".bin"
    missing: list[str] = []
    for name in _REQUIRED_VALIDATOR_BINARIES:
        path = bin_dir / name
        if not (path.exists() and os.access(path, os.X_OK)):
            missing.append(name)
    if missing:
        return CheckResult(
            name="validator_binaries",
            status=CheckStatus.fail,
            detail=(
                f"missing or non-executable: {', '.join(missing)} in {bin_dir}"
            ),
            remediation=(
                f"cd {services_root} && npm install\n"
                "  (same as node_modules check above — npm install "
                "creates the .bin/ shims.)"
            ),
        )
    return CheckResult(
        name="validator_binaries",
        status=CheckStatus.ok,
        detail=(
            f"all present: {', '.join(_REQUIRED_VALIDATOR_BINARIES)} in {bin_dir}"
        ),
    )


def check_npmrc(services_root: Path) -> CheckResult:
    """Probe: ``services/.npmrc`` exists + points at ``canaveral-npm``.

    The slice-12 install gap originally surfaced because the
    out-of-the-box ``.npmrc`` pointed at
    ``npm-walled-garden`` — a curated mirror that doesn't have
    every upstream package version. Switching to ``canaveral-npm``
    (the full mirror) fixed the install. We warn — not fail —
    when the registry is set but isn't the canaveral one, because
    a custom registry might be intentional.
    """
    npmrc = services_root / ".npmrc"
    if not npmrc.is_file():
        return CheckResult(
            name="npmrc",
            status=CheckStatus.fail,
            detail=f".npmrc missing: {npmrc}",
            remediation=(
                "Copy the working .npmrc into services/ — it carries "
                "the Canaveral registry + CA chain + auth tokens."
            ),
        )
    body = npmrc.read_text(encoding="utf-8", errors="replace")
    has_canaveral = "canaveral-npm" in body
    has_walled_garden_only = "npm-walled-garden" in body and not has_canaveral
    if has_canaveral:
        return CheckResult(
            name="npmrc",
            status=CheckStatus.ok,
            detail=f".npmrc points at canaveral-npm: {npmrc}",
        )
    if has_walled_garden_only:
        return CheckResult(
            name="npmrc",
            status=CheckStatus.warn,
            detail=(
                f".npmrc points at npm-walled-garden only "
                f"(may 404 on newer packages): {npmrc}"
            ),
            remediation=(
                "Add the canaveral-npm registry block — see "
                "the .npmrc in this repo for the canonical shape."
            ),
        )
    return CheckResult(
        name="npmrc",
        status=CheckStatus.warn,
        detail=(
            f".npmrc has no recognised Nutanix registry; "
            f"may be intentional: {npmrc}"
        ),
    )


def check_playwright_browsers() -> CheckResult:
    """Probe: every Playwright browser engine is installed.

    Shares its semantics with the in-workflow
    :func:`prism_mcp.workflow.activities.check_dependencies_installed`
    so the preflight and the runtime check can't drift apart on
    which engines are required or on how the cache directory is
    resolved (``PLAYWRIGHT_BROWSERS_PATH`` env var > platform
    default).

    Returns ``fail`` when *any* engine is missing — the Prism
    library's ``playwright.config.ts`` defines projects for all
    three (chromium / firefox / webkit), so a single missing
    engine will fail the entire ``playwright test`` run with a
    confusing "Executable doesn't exist" deep in the Playwright
    stack. Surfacing it here is the slice-12 follow-on gap-close.
    """
    cache_dir = playwright_browsers_dir()
    present, missing = _check_playwright_browsers(cache_dir)
    if not missing:
        return CheckResult(
            name="playwright_browsers",
            status=CheckStatus.ok,
            detail=(
                f"all engines installed: {', '.join(present)} in {cache_dir}"
            ),
        )
    return CheckResult(
        name="playwright_browsers",
        status=CheckStatus.fail,
        detail=(
            f"missing engines: {', '.join(missing)} (cache_dir={cache_dir})"
        ),
        remediation=(
            "cd <services_root> && ./node_modules/.bin/playwright install\n"
            "  (One-time ~600MB download. Honours PLAYWRIGHT_BROWSERS_PATH "
            "if set, otherwise uses the platform default cache dir shown above.)"
        ),
    )


_CA_BUNDLE_NAMES = ("canaveral-ca-bundle.pem", "ca-chain.crt")
"""Candidate filenames for a pre-built Canaveral CA bundle.

The build script ``scripts/build_canaveral_ca_bundle.sh`` writes
``canaveral-ca-bundle.pem``; some operators reuse the raw
``ca-chain.crt`` shipped by the gatekeeper service. Accept either.
"""


def check_ca_bundle(poc_root: Path) -> CheckResult:
    """Probe: a Canaveral CA bundle PEM exists next to the POC repo.

    Soft check — if ``NODE_EXTRA_CA_CERTS`` is set we trust the
    operator; if not, we look for the conventional sibling file.
    Either path is acceptable for slice-12 because the updated
    ``.npmrc`` inlines the certificate chain via ``ca[]=`` lines,
    making the external bundle redundant for npm itself. The
    bundle is still helpful for Python and other tools that hit
    Artifactory.
    """
    extra_certs = os.environ.get("NODE_EXTRA_CA_CERTS")
    if extra_certs and Path(extra_certs).is_file():
        return CheckResult(
            name="ca_bundle",
            status=CheckStatus.ok,
            detail=f"NODE_EXTRA_CA_CERTS set: {extra_certs}",
        )
    for name in _CA_BUNDLE_NAMES:
        candidate = poc_root / name
        if candidate.is_file():
            return CheckResult(
                name="ca_bundle",
                status=CheckStatus.ok,
                detail=f"CA bundle present: {candidate}",
            )
    return CheckResult(
        name="ca_bundle",
        status=CheckStatus.warn,
        detail=(
            "No CA bundle found and NODE_EXTRA_CA_CERTS unset; "
            "the updated .npmrc inlines certs so npm should still "
            "work, but Python TLS to Artifactory may need attention."
        ),
        remediation=(
            "If you hit SELF_SIGNED_CERT_IN_CHAIN, run "
            "scripts/build_canaveral_ca_bundle.sh."
        ),
    )


# --------------------------------------------------------------------------
# Orchestrator: run every check, return a PreflightReport.
# --------------------------------------------------------------------------


def run_preflight(services_root: Path, poc_root: Path) -> PreflightReport:
    """Run every probe and return the aggregated report.

    The probes are ordered so a fundamental failure (no Python,
    no Node, missing services dir) is reported before a derived
    one (missing ``node_modules``). The operator scans the report
    top-down and stops at the first ``fail``.
    """
    report = PreflightReport(services_root=str(services_root))
    report.checks.append(check_python())
    report.checks.append(check_node())
    report.checks.append(check_npm())
    services_check = check_services_dir(services_root)
    report.checks.append(services_check)
    # Only run the services-relative checks when the dir exists —
    # otherwise the reports would be a confusing cascade of
    # "missing node_modules" etc that the operator already knows
    # about from the parent check.
    if services_check.status is CheckStatus.ok:
        report.checks.append(check_node_modules(services_root))
        report.checks.append(check_validator_binaries(services_root))
        report.checks.append(check_npmrc(services_root))
    # Playwright browsers live in a user-level cache, not under
    # services/, so we probe them independently of the services
    # cascade. Operators can have a valid checkout but still need
    # ``npx playwright install``.
    report.checks.append(check_playwright_browsers())
    report.checks.append(check_ca_bundle(poc_root))
    return report


# --------------------------------------------------------------------------
# CLI surface. parse_args() returns a frozen dataclass so it's
# hermetically testable without running main().
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CliConfig:
    services_root: Path
    poc_root: Path
    json_output: bool


_DEFAULT_SERVICES_REL = "../prism-ui-prism-reactjs-lib/services"
"""Default sibling-checkout convention.

The POC and the Prism library are typically cloned side-by-side
in ``~/workspace/prism-react/``; this default works without any
CLI flags for that layout.
"""


def _default_services_root() -> Path:
    """Resolve the default services-root relative to this file."""
    # src/prism_mcp/setup_cli.py → up three levels → POC root.
    poc_root = Path(__file__).resolve().parent.parent.parent
    return (poc_root / _DEFAULT_SERVICES_REL).resolve()


def _default_poc_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def parse_args(argv: Sequence[str]) -> CliConfig:
    """Parse ``argv`` into :class:`CliConfig`."""
    parser = argparse.ArgumentParser(
        prog="prism-mcp-setup",
        description=(
            "Preflight diagnostic for the prism-mcp slice-12 demo. "
            "Inspects the environment + reports what (if anything) "
            "needs to be installed. NEVER installs anything itself."
        ),
    )
    parser.add_argument(
        "--services-root",
        type=Path,
        default=None,
        help=(
            "Absolute path to the Prism library's services/ "
            "directory. Defaults to the sibling checkout at "
            f"{_DEFAULT_SERVICES_REL}."
        ),
    )
    parser.add_argument(
        "--poc-root",
        type=Path,
        default=None,
        help=(
            "Absolute path to the prism-mcp POC root (used to "
            "locate the CA bundle). Defaults to this repo."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit the report as machine-parseable JSON instead of text.",
    )
    args = parser.parse_args(list(argv))
    services_root = (
        args.services_root.resolve()
        if args.services_root is not None
        else _default_services_root()
    )
    poc_root = (
        args.poc_root.resolve()
        if args.poc_root is not None
        else _default_poc_root()
    )
    return CliConfig(
        services_root=services_root,
        poc_root=poc_root,
        json_output=args.json_output,
    )


# --------------------------------------------------------------------------
# Output formatting. Kept separate from the orchestration so tests
# can assert against the structured report without parsing terminal
# output.
# --------------------------------------------------------------------------


_STATUS_GLYPH = {
    CheckStatus.ok: "[ ok ]",
    CheckStatus.warn: "[warn]",
    CheckStatus.fail: "[FAIL]",
}


def render_text_report(report: PreflightReport) -> str:
    """Render the report as a multi-line plain-text block.

    Format intentionally borrows from ``kubectl describe`` — fixed
    glyph column, name column, then free-form detail. Easy for
    humans, also easy to ``grep`` for ``FAIL``.
    """
    lines: list[str] = []
    lines.append("prism-mcp-setup preflight report")
    lines.append(f"services_root: {report.services_root}")
    lines.append("")
    for check in report.checks:
        glyph = _STATUS_GLYPH[check.status]
        lines.append(f"{glyph} {check.name:<20} {check.detail}")
        if check.remediation:
            for line in check.remediation.splitlines():
                lines.append(f"        -> {line}")
    lines.append("")
    lines.append(f"OVERALL: {report.overall.value.upper()}")
    return "\n".join(lines)


def render_json_report(report: PreflightReport) -> str:
    """Render the report as a JSON object (CI-friendly)."""
    return json.dumps(
        {
            "services_root": report.services_root,
            "overall": report.overall.value,
            "checks": [asdict(c) for c in report.checks],
        },
        indent=2,
    )


def main() -> None:
    """Console-script entrypoint registered in ``pyproject.toml``."""
    config = parse_args(sys.argv[1:])
    report = run_preflight(config.services_root, config.poc_root)
    if config.json_output:
        sys.stdout.write(render_json_report(report) + "\n")
    else:
        sys.stdout.write(render_text_report(report) + "\n")
    sys.exit(report.exit_code)


if __name__ == "__main__":
    main()
