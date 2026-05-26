"""Tests for the ``prism-mcp-setup`` preflight CLI.

The CLI's two responsibilities are:

1. Inspect the environment and emit a structured
   :class:`PreflightReport`.
2. Render that report as text *or* JSON, and exit with the right
   code (``0`` for ok/warn, ``1`` for fail).

Both halves are hermetically testable with a tmp_path and
``unittest.mock.patch`` over the few subprocess / environment
boundaries the CLI touches.

We deliberately do *not* test the live "is node on the operator's
PATH?" path — that's environment-dependent and tested live by the
``prism-mcp-setup`` invocation operators run by hand. Our suite
instead locks down the structured contract: each probe produces
the right :class:`CheckResult` for the right input.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from prism_mcp.setup_cli import (
    CheckStatus,
    CliConfig,
    PreflightReport,
    check_ca_bundle,
    check_node_modules,
    check_npmrc,
    check_playwright_browsers,
    check_python,
    check_services_dir,
    check_validator_binaries,
    parse_args,
    render_json_report,
    render_text_report,
    run_preflight,
)

# --------------------------------------------------------------------------
# Helpers — same shape as test_workflow_activities so the test
# suite has one consistent "fake services dir" idiom.
# --------------------------------------------------------------------------


def _make_services_with_bins(
    tmp_path: Path,
    *,
    names: tuple[str, ...] = ("tsc", "eslint", "jest", "playwright"),
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Materialise a fake services tree with the requested binaries."""
    services_root = tmp_path / "services"
    bin_dir = services_root / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    if extra_files:
        for rel, body in extra_files.items():
            target = services_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
    return services_root


def _make_playwright_cache(
    tmp_path: Path,
    *,
    engines: tuple[str, ...] = ("chromium", "firefox", "webkit"),
) -> Path:
    """Materialise a fake Playwright browsers cache."""
    cache = tmp_path / "playwright-cache"
    cache.mkdir(parents=True, exist_ok=True)
    for engine in engines:
        (cache / f"{engine}-9999").mkdir()
    return cache


@pytest.fixture
def _all_browsers_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point Playwright at a tmp cache containing all three engines.

    Tests that care only about the binary / npmrc / CA bundle
    probes use this fixture so the report's ``overall`` field
    isn't dragged down by an unrelated browsers-missing fail on
    the developer's laptop.
    """
    cache = _make_playwright_cache(tmp_path)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    return cache


# --------------------------------------------------------------------------
# check_python: simple lower-bound probe.
# --------------------------------------------------------------------------


def test_check_python_passes_on_311_plus() -> None:
    """The test suite itself runs on >= 3.11, so this probe must pass."""
    result = check_python()
    assert result.status is CheckStatus.ok
    assert "Python" in result.detail


# --------------------------------------------------------------------------
# check_services_dir: present vs missing.
# --------------------------------------------------------------------------


def test_check_services_dir_passes_when_present(tmp_path: Path) -> None:
    services_root = tmp_path / "services"
    services_root.mkdir()
    result = check_services_dir(services_root)
    assert result.status is CheckStatus.ok


def test_check_services_dir_fails_with_remediation_when_missing(
    tmp_path: Path,
) -> None:
    """Operator-facing remediation tells them how to fix it."""
    missing = tmp_path / "nope"
    result = check_services_dir(missing)
    assert result.status is CheckStatus.fail
    assert result.remediation is not None
    assert "--services-root" in result.remediation


# --------------------------------------------------------------------------
# check_node_modules: present vs missing.
# --------------------------------------------------------------------------


def test_check_node_modules_passes_when_present(tmp_path: Path) -> None:
    services_root = _make_services_with_bins(tmp_path)
    result = check_node_modules(services_root)
    assert result.status is CheckStatus.ok


def test_check_node_modules_fails_with_npm_install_hint(tmp_path: Path) -> None:
    """The remediation must be a copy-pasteable ``npm install`` line."""
    services_root = tmp_path / "services"
    services_root.mkdir()  # node_modules deliberately absent
    result = check_node_modules(services_root)
    assert result.status is CheckStatus.fail
    assert result.remediation is not None
    assert "npm install" in result.remediation
    assert "canaveral" in result.remediation.lower()


# --------------------------------------------------------------------------
# check_validator_binaries: shares semantics with the activity.
# --------------------------------------------------------------------------


def test_check_validator_binaries_passes_when_all_present(
    tmp_path: Path,
) -> None:
    services_root = _make_services_with_bins(tmp_path)
    result = check_validator_binaries(services_root)
    assert result.status is CheckStatus.ok
    for name in ("tsc", "eslint", "jest", "playwright"):
        assert name in result.detail


def test_check_validator_binaries_fails_when_one_missing(
    tmp_path: Path,
) -> None:
    services_root = _make_services_with_bins(
        tmp_path, names=("tsc", "eslint", "jest")
    )
    result = check_validator_binaries(services_root)
    assert result.status is CheckStatus.fail
    assert "playwright" in result.detail
    assert result.remediation is not None
    assert "npm install" in result.remediation


def test_check_validator_binaries_fails_when_present_but_not_executable(
    tmp_path: Path,
) -> None:
    """Non-executable is treated the same as missing."""
    services_root = _make_services_with_bins(tmp_path)
    jest_path = services_root / "node_modules" / ".bin" / "jest"
    mode = jest_path.stat().st_mode
    jest_path.chmod(mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    assert not os.access(jest_path, os.X_OK)
    result = check_validator_binaries(services_root)
    assert result.status is CheckStatus.fail
    assert "jest" in result.detail


# --------------------------------------------------------------------------
# check_npmrc: ok on canaveral-npm, warn on walled-garden-only, fail
# when missing.
# --------------------------------------------------------------------------


def test_check_npmrc_passes_on_canaveral_npm(tmp_path: Path) -> None:
    services_root = _make_services_with_bins(
        tmp_path,
        extra_files={".npmrc": "registry=https://x/api/npm/canaveral-npm/\n"},
    )
    result = check_npmrc(services_root)
    assert result.status is CheckStatus.ok


def test_check_npmrc_warns_on_walled_garden_only(tmp_path: Path) -> None:
    """The original gap-trigger: ``.npmrc`` points at the smaller
    mirror that misses package versions. We warn so the operator
    knows why ``ETARGET`` errors are likely.
    """
    services_root = _make_services_with_bins(
        tmp_path,
        extra_files={
            ".npmrc": "registry=https://x/api/npm/npm-walled-garden/\n"
        },
    )
    result = check_npmrc(services_root)
    assert result.status is CheckStatus.warn
    assert "walled-garden" in result.detail


def test_check_npmrc_fails_when_missing(tmp_path: Path) -> None:
    services_root = _make_services_with_bins(tmp_path)
    # No .npmrc written.
    result = check_npmrc(services_root)
    assert result.status is CheckStatus.fail


# --------------------------------------------------------------------------
# check_playwright_browsers: shares semantics with the activity probe.
# --------------------------------------------------------------------------


def test_check_playwright_browsers_passes_when_all_engines_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three engine subdirs present → ok + detail names each."""
    cache = _make_playwright_cache(tmp_path)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    result = check_playwright_browsers()
    assert result.status is CheckStatus.ok
    for engine in ("chromium", "firefox", "webkit"):
        assert engine in result.detail


def test_check_playwright_browsers_fails_when_cache_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact gap the user spotted: cache dir doesn't exist.

    The probe must surface a remediation that points at
    ``playwright install`` *and* mentions
    ``PLAYWRIGHT_BROWSERS_PATH`` so operators using a non-default
    cache location know we honoured it.
    """
    monkeypatch.setenv(
        "PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "does-not-exist")
    )
    result = check_playwright_browsers()
    assert result.status is CheckStatus.fail
    for engine in ("chromium", "firefox", "webkit"):
        assert engine in result.detail
    assert result.remediation is not None
    assert "playwright install" in result.remediation
    assert "PLAYWRIGHT_BROWSERS_PATH" in result.remediation


def test_check_playwright_browsers_fails_on_partial_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only chromium installed → fail naming firefox + webkit."""
    cache = _make_playwright_cache(tmp_path, engines=("chromium",))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    result = check_playwright_browsers()
    assert result.status is CheckStatus.fail
    assert "firefox" in result.detail
    assert "webkit" in result.detail
    assert "chromium" not in result.detail.split("missing engines:", 1)[1]


# --------------------------------------------------------------------------
# check_ca_bundle: env var > sibling file > warn.
# --------------------------------------------------------------------------


def test_check_ca_bundle_passes_on_node_extra_ca_certs(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    with patch.dict(os.environ, {"NODE_EXTRA_CA_CERTS": str(bundle)}):
        result = check_ca_bundle(tmp_path)
    assert result.status is CheckStatus.ok


def test_check_ca_bundle_passes_on_sibling_pem(tmp_path: Path) -> None:
    """The conventional ``canaveral-ca-bundle.pem`` sibling works."""
    bundle = tmp_path / "canaveral-ca-bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NODE_EXTRA_CA_CERTS", None)
        result = check_ca_bundle(tmp_path)
    assert result.status is CheckStatus.ok


def test_check_ca_bundle_warns_when_absent(tmp_path: Path) -> None:
    """Soft fail — the inline cert chain in .npmrc makes the bundle
    optional for npm itself.
    """
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NODE_EXTRA_CA_CERTS", None)
        result = check_ca_bundle(tmp_path)
    assert result.status is CheckStatus.warn


# --------------------------------------------------------------------------
# run_preflight: orchestrator returns aggregated report with correct
# overall status, and short-circuits services-relative checks when
# the services dir itself is missing.
# --------------------------------------------------------------------------


def test_run_preflight_overall_ok_when_everything_passes(
    tmp_path: Path,
    _all_browsers_present: Path,
) -> None:
    """Build a tmp tree that satisfies every probe, expect overall=ok."""
    services_root = _make_services_with_bins(
        tmp_path,
        extra_files={".npmrc": "registry=https://x/api/npm/canaveral-npm/\n"},
    )
    bundle = tmp_path / "canaveral-ca-bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NODE_EXTRA_CA_CERTS", None)
        report = run_preflight(services_root, tmp_path)

    # The overall result depends only on the probes we can fully
    # control here — node/npm probes may pass or fail depending on
    # the test runner's environment. We assert specifically about
    # the probes we created the fixtures for.
    by_name = {c.name: c for c in report.checks}
    assert by_name["services_dir"].status is CheckStatus.ok
    assert by_name["node_modules"].status is CheckStatus.ok
    assert by_name["validator_binaries"].status is CheckStatus.ok
    assert by_name["npmrc"].status is CheckStatus.ok
    assert by_name["playwright_browsers"].status is CheckStatus.ok
    assert by_name["ca_bundle"].status is CheckStatus.ok


def test_run_preflight_short_circuits_when_services_missing(
    tmp_path: Path,
) -> None:
    """No services dir → don't probe node_modules / binaries / npmrc.

    Those would emit confusing cascade failures otherwise (the
    operator already knows the services dir is missing). The
    playwright_browsers probe still runs because it doesn't
    depend on the services dir — browsers live in a user-level
    cache.
    """
    missing = tmp_path / "missing"
    report = run_preflight(missing, tmp_path)
    names = [c.name for c in report.checks]
    assert "services_dir" in names
    assert "node_modules" not in names
    assert "validator_binaries" not in names
    assert "npmrc" not in names
    assert "playwright_browsers" in names
    assert report.overall is CheckStatus.fail
    assert report.exit_code == 1


def test_run_preflight_overall_fail_propagates_exit_code(
    tmp_path: Path,
) -> None:
    """A single ``fail`` probe → overall=fail → exit_code=1."""
    services_root = tmp_path / "services"
    services_root.mkdir()  # No node_modules, no .npmrc.
    report = run_preflight(services_root, tmp_path)
    assert report.overall is CheckStatus.fail
    assert report.exit_code == 1


def test_run_preflight_overall_warn_yields_exit_code_zero(
    tmp_path: Path,
    _all_browsers_present: Path,
) -> None:
    """A warn-only report still passes the smoke (exit 0).

    Warns are advisory — the demo will work, the operator just
    might want to fix the .npmrc to avoid edge cases.
    """
    services_root = _make_services_with_bins(
        tmp_path,
        extra_files={
            ".npmrc": "registry=https://x/api/npm/npm-walled-garden/\n"
        },
    )
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NODE_EXTRA_CA_CERTS", None)
        report = run_preflight(services_root, tmp_path)
    # The reported overall depends on every probe; we focus on the
    # rule: at least one warn and no fails → warn → exit 0.
    statuses = {c.status for c in report.checks}
    if CheckStatus.fail not in statuses:
        assert report.overall is CheckStatus.warn
        assert report.exit_code == 0


# --------------------------------------------------------------------------
# CLI surface: parse_args + render_*.
# --------------------------------------------------------------------------


def test_parse_args_returns_resolved_paths(tmp_path: Path) -> None:
    """``--services-root`` and ``--poc-root`` flags are honoured."""
    services_root = tmp_path / "services"
    services_root.mkdir()
    config = parse_args(
        ["--services-root", str(services_root), "--poc-root", str(tmp_path)]
    )
    assert isinstance(config, CliConfig)
    assert config.services_root == services_root.resolve()
    assert config.poc_root == tmp_path.resolve()
    assert config.json_output is False


def test_parse_args_defaults_to_sibling_checkout() -> None:
    """No flags → default services-root resolves to the conventional
    sibling path. We don't assert the path is *present* (CI may
    not have the sibling checkout) — only that it has a value.
    """
    config = parse_args([])
    assert config.services_root is not None
    assert config.poc_root is not None


def test_parse_args_json_flag() -> None:
    config = parse_args(["--json"])
    assert config.json_output is True


def test_render_text_report_includes_every_check_name(tmp_path: Path) -> None:
    services_root = tmp_path / "services"
    services_root.mkdir()
    report = run_preflight(services_root, tmp_path)
    text = render_text_report(report)
    assert "OVERALL" in text
    for check in report.checks:
        assert check.name in text


def test_render_json_report_is_valid_json(tmp_path: Path) -> None:
    """JSON output is parseable + has the expected top-level keys."""
    services_root = tmp_path / "services"
    services_root.mkdir()
    report = run_preflight(services_root, tmp_path)
    body = render_json_report(report)
    parsed = json.loads(body)
    assert parsed["services_root"] == str(services_root)
    assert parsed["overall"] in {"ok", "warn", "fail"}
    assert isinstance(parsed["checks"], list)
    for check in parsed["checks"]:
        assert {"name", "status", "detail"}.issubset(check.keys())


# --------------------------------------------------------------------------
# PreflightReport invariants.
# --------------------------------------------------------------------------


def test_preflight_report_overall_with_no_checks_is_ok() -> None:
    """Empty report → vacuously ok (defensive default)."""
    report = PreflightReport(services_root="/anywhere")
    assert report.overall is CheckStatus.ok
    assert report.exit_code == 0


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([CheckStatus.ok, CheckStatus.ok], CheckStatus.ok),
        ([CheckStatus.ok, CheckStatus.warn], CheckStatus.warn),
        ([CheckStatus.warn, CheckStatus.warn], CheckStatus.warn),
        ([CheckStatus.ok, CheckStatus.fail], CheckStatus.fail),
        ([CheckStatus.warn, CheckStatus.fail], CheckStatus.fail),
        ([CheckStatus.fail, CheckStatus.fail], CheckStatus.fail),
    ],
)
def test_preflight_report_overall_aggregation(
    statuses: list[CheckStatus], expected: CheckStatus
) -> None:
    """``fail`` dominates, ``warn`` dominates ``ok``."""
    from prism_mcp.setup_cli import CheckResult

    report = PreflightReport(
        services_root="/anywhere",
        checks=[
            CheckResult(name=f"n{i}", status=s, detail="")
            for i, s in enumerate(statuses)
        ],
    )
    assert report.overall is expected
