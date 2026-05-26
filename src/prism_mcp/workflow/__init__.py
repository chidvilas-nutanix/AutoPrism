"""Slice 12: AlphaCodium-flavored iteration loop on Temporal.

This subpackage hosts every piece of the slice-12 "validation loop"
SOTA stack:

* :mod:`prism_mcp.workflow.contracts` — typed inputs/outputs shared
  between Activities, Workflow, and the MCP tool surface. The single
  source of truth for "what does a validator return?".
* :mod:`prism_mcp.workflow.activities` — thin :func:`subprocess.run`
  wrappers around the Prism library's existing npm scripts
  (``tsc``, ``eslint``, ``jest``, ``playwright + axe``) plus the
  SSIM Figma-compare activity. Activities are the only place the
  Python process is allowed to touch the filesystem, the network,
  or a subprocess — :class:`temporalio.workflow.Workflow` code
  must stay deterministic.
* :mod:`prism_mcp.workflow.workflow` — the
  :class:`GenerateComponentWorkflow` class itself: bounded
  ``max_iterations=3`` loop, structured reflection prompt on
  failure, :func:`temporalio.workflow.update` handler for
  ``submit_candidate``, :func:`temporalio.workflow.query` handler
  for ``status``.
* :mod:`prism_mcp.workflow.worker` — the ``prism-mcp-worker``
  console-script entrypoint. Polls the
  :data:`PRISM_TASK_QUEUE` task queue against a
  :class:`temporalio.client.Client` and runs the workflow +
  activities.
* :mod:`prism_mcp.workflow.ssim` — the pure-function SSIM math
  (no Temporal involvement) so it can be reused by ad-hoc
  ``compare_to_figma`` tool calls without standing up a workflow.

Why this layout
---------------

We deliberately split the package by *runtime concern* (workflow vs
activity vs side-effect-free helper) instead of by *feature*, because
Temporal enforces that split at the runtime level: workflow code
runs inside a deterministic sandbox that re-executes on replay, and
must never import non-deterministic modules at the top level. Putting
``activities.py`` and ``workflow.py`` in separate files makes that
sandbox-safety boundary explicit and machine-checkable — the
workflow file should never need to import :mod:`subprocess` or
:mod:`pathlib.Path`.
"""

from __future__ import annotations

PRISM_TASK_QUEUE = "prism-mcp-component-generation"
"""The Temporal task queue every slice-12 worker polls.

A single string constant kept here (not in :mod:`config`) because
both the workflow client (in the MCP server process) and the worker
need to agree on it, and neither is configurable per deployment —
the queue is part of the workflow contract, not the runtime
configuration.
"""
