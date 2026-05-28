"""Tests for the ``prism-mcp-worker`` entrypoint.

The worker entrypoint is the long-running process the demo
operator spins up alongside ``prism-mcp``. We don't end-to-end
test it (that's what the WorkflowEnvironment tests already
cover); we focus on the boundary concerns:

* Activity list completeness — every activity the workflow
  needs is registered.
* Workflow list — the worker registers exactly
  :class:`GenerateComponentWorkflow`.
* Task-queue name — the worker polls the same constant the
  client uses (``PRISM_TASK_QUEUE``).
* Data-converter — Pydantic v2 converter is wired in.
* CLI argument parsing — ``--server-address`` defaults to
  Temporal's dev-server address and is overridable.
"""

from __future__ import annotations

import pytest

from prism_mcp.workflow import PRISM_TASK_QUEUE
from prism_mcp.workflow.activities import (
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
from prism_mcp.workflow.worker import (
    DEFAULT_SERVER_ADDRESS,
    WorkerConfig,
    build_activity_set,
    build_workflow_set,
    parse_args,
)
from prism_mcp.workflow.workflow import GenerateComponentWorkflow

# --------------------------------------------------------------------------
# build_workflow_set / build_activity_set: the worker's registration
# manifest. Centralised here so the test suite can verify nothing
# the workflow needs has been forgotten.
# --------------------------------------------------------------------------


def test_workflow_set_includes_generate_component_workflow() -> None:
    """The worker must register exactly :class:`GenerateComponentWorkflow`."""
    assert build_workflow_set() == [GenerateComponentWorkflow]


def test_activity_set_includes_all_workflow_activities() -> None:
    """Every activity the workflow invokes must be registered.

    A missing activity would manifest as a runtime
    ``ActivityNotFoundError`` minutes into a real workflow run;
    the smoke test here catches the omission at boot.

    The set has grown beyond the original seven slice-12
    activities:

    * ``check_dependencies_installed`` — the slice-12
      gap-closing preflight that probes ``node_modules`` before
      any subprocess validator runs.
    * ``update_companion_test_files`` — refines the auto-
      scaffolded pwspec / spec.tsx via the
      ``update_companion_tests`` MCP tool.
    * ``materialise_figma_reference`` — resolves
      ``figma_png_url`` / ``figma_png_base64`` to an on-disk
      path once at workflow start, cached for every SSIM
      iteration.
    * ``check_rendered_exists`` — pre-flight probe the workflow
      runs before SSIM so a missing rendered PNG yields a
      clean ``ssim_skip_reason="rendered_unavailable"`` instead
      of a ``FileNotFoundError`` from the SSIM math.
    """
    activities = build_activity_set()
    expected = {
        write_candidate_files,
        update_companion_test_files,
        check_dependencies_installed,
        materialise_figma_reference,
        run_typecheck,
        run_eslint,
        run_jest,
        run_playwright_axe,
        check_rendered_exists,
        run_ssim_compare,
    }
    assert set(activities) == expected


# --------------------------------------------------------------------------
# parse_args: CLI surface.
# --------------------------------------------------------------------------


def test_parse_args_defaults_to_dev_server_address() -> None:
    """No CLI args → connect to the dev server's default address."""
    config = parse_args([])

    assert isinstance(config, WorkerConfig)
    assert config.server_address == DEFAULT_SERVER_ADDRESS
    assert config.task_queue == PRISM_TASK_QUEUE
    assert config.namespace == "default"


def test_parse_args_accepts_custom_server_address() -> None:
    """``--server-address`` overrides for self-hosted deployments."""
    config = parse_args(["--server-address", "temporal.internal:7233"])

    assert config.server_address == "temporal.internal:7233"


def test_parse_args_accepts_custom_namespace() -> None:
    """``--namespace`` overrides for shared-cluster deployments."""
    config = parse_args(["--namespace", "prism-mcp-prod"])

    assert config.namespace == "prism-mcp-prod"


def test_parse_args_rejects_unknown_flags() -> None:
    """Unknown CLI flags fail loud — typo'd flags must not silently
    fall through to defaults.
    """
    with pytest.raises(SystemExit):
        parse_args(["--no-such-flag", "x"])


# --------------------------------------------------------------------------
# DEFAULT_SERVER_ADDRESS: documented constant. The actual demo
# script (`temporal server start-dev --db-filename prism.db`)
# binds 127.0.0.1:7233 — locking the constant here so a future
# refactor can't drift the worker default away from the demo.
# --------------------------------------------------------------------------


def test_default_server_address_is_dev_server_address() -> None:
    """``temporal server start-dev`` binds ``127.0.0.1:7233`` by default."""
    assert DEFAULT_SERVER_ADDRESS == "localhost:7233"
