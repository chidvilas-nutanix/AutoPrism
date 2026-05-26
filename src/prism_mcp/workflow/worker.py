"""``prism-mcp-worker`` — the Temporal worker entrypoint.

This long-running process is what makes the slice-12 demo work:
it polls Temporal's task queue (``PRISM_TASK_QUEUE``), runs
:class:`GenerateComponentWorkflow` instances + their activities,
and writes durable history back to the server.

The demo operator runs three terminals:

1. ``temporal server start-dev --db-filename prism.db`` — the
   Temporal service.
2. ``prism-mcp`` — the stdio MCP server Cursor talks to.
3. ``prism-mcp-worker`` — *this* entrypoint.

We keep the entrypoint small and testable by factoring the
registration data into :func:`build_workflow_set` /
:func:`build_activity_set`, and the CLI surface into
:func:`parse_args`. The actual ``main()`` is the thinnest possible
shell that wires them together.

Pydantic data converter
-----------------------

Every Pydantic v2 model in the contract module needs the
:data:`temporalio.contrib.pydantic.pydantic_data_converter` to
serialise cleanly across the worker/client boundary. Without it
Temporal falls back to ``BaseModel.dict()`` (Pydantic v1) which
drops typed fields and emits deprecation warnings.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from prism_mcp.workflow import PRISM_TASK_QUEUE
from prism_mcp.workflow.activities import (
    check_dependencies_installed,
    run_eslint,
    run_jest,
    run_playwright_axe,
    run_ssim_compare,
    run_typecheck,
    write_candidate_files,
)
from prism_mcp.workflow.workflow import GenerateComponentWorkflow

logger = logging.getLogger(__name__)


DEFAULT_SERVER_ADDRESS = "localhost:7233"
"""Default Temporal frontend address.

Matches ``temporal server start-dev``'s default bind. Demo
operators don't need to override this. Self-hosted / cloud
deployments override with ``--server-address``.
"""


# --------------------------------------------------------------------------
# CLI dataclass — keeps parse_args() testable without standing up
# a real Temporal connection.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerConfig:
    """Parsed CLI configuration for the worker process.

    Args:
        server_address (str): Temporal frontend host:port.
        namespace (str): Temporal namespace to register against.
        task_queue (str): the task queue to poll. Defaults to
            :data:`prism_mcp.workflow.PRISM_TASK_QUEUE` — the
            same constant the MCP server uses to enqueue work.
    """

    server_address: str
    namespace: str
    task_queue: str


def parse_args(argv: Sequence[str]) -> WorkerConfig:
    """Parse ``argv`` into a :class:`WorkerConfig`.

    Args:
        argv (Sequence[str]): CLI arguments, *excluding* the
            program name. Tests pass ``[]`` for defaults.

    Returns:
        WorkerConfig: parsed config.
    """
    parser = argparse.ArgumentParser(
        prog="prism-mcp-worker",
        description=(
            "Temporal worker for the slice-12 component-generation "
            "workflow. Polls the prism-mcp task queue and runs the "
            "AlphaCodium iteration loop."
        ),
    )
    parser.add_argument(
        "--server-address",
        default=DEFAULT_SERVER_ADDRESS,
        help=(
            "Temporal frontend host:port. Defaults to the dev "
            "server's bind (localhost:7233)."
        ),
    )
    parser.add_argument(
        "--namespace",
        default="default",
        help="Temporal namespace to register against.",
    )
    args = parser.parse_args(list(argv))
    return WorkerConfig(
        server_address=args.server_address,
        namespace=args.namespace,
        task_queue=PRISM_TASK_QUEUE,
    )


# --------------------------------------------------------------------------
# Registration manifests — single source of truth so the worker and
# its tests can't drift apart.
# --------------------------------------------------------------------------


def build_workflow_set() -> list[type]:
    """Return every workflow class the worker should register."""
    return [GenerateComponentWorkflow]


def build_activity_set() -> list[object]:
    """Return every activity function the worker should register.

    Returned as ``list[object]`` because :func:`temporalio.activity.defn`
    erases the underlying signature to a callable protocol that
    isn't typeable across activities.
    """
    return [
        write_candidate_files,
        check_dependencies_installed,
        run_typecheck,
        run_eslint,
        run_jest,
        run_playwright_axe,
        run_ssim_compare,
    ]


# --------------------------------------------------------------------------
# main() — the actual entrypoint. Kept minimal: connect, build,
# run forever.
# --------------------------------------------------------------------------


async def _run_worker(config: WorkerConfig) -> None:
    """Connect + register + block on incoming work."""
    logger.info(
        "connecting worker target=%s namespace=%s queue=%s",
        config.server_address,
        config.namespace,
        config.task_queue,
    )
    client = await Client.connect(
        config.server_address,
        namespace=config.namespace,
        data_converter=pydantic_data_converter,
    )
    async with Worker(
        client,
        task_queue=config.task_queue,
        workflows=build_workflow_set(),
        activities=build_activity_set(),
    ):
        logger.info("worker ready; polling for tasks (Ctrl+C to stop)")
        # Block forever (until SIGINT / cancellation). The Worker
        # context manager handles graceful shutdown.
        await asyncio.Event().wait()


def main() -> None:
    """Console-script entrypoint registered in ``pyproject.toml``."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = parse_args(sys.argv[1:])
    try:
        asyncio.run(_run_worker(config))
    except KeyboardInterrupt:
        logger.info("worker shutting down on SIGINT")


if __name__ == "__main__":
    main()
