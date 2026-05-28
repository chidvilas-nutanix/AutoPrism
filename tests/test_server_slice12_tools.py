"""Tests for the slice-12 MCP tools.

The slice-12 surface bridges the MCP server to the Temporal
workflow that runs the AlphaCodium iteration loop. We focus the
test surface on the load-bearing wiring:

* ``compare_to_figma`` — ad-hoc SSIM compare (no workflow);
  reuses the pure :func:`compute_ssim_from_paths` helper.
* ``start_generate_component`` — bridges the MCP request to a
  Temporal workflow start. Now accepts ``figma_png_url`` /
  ``figma_png_base64`` so Figma MCP's URL-shaped payloads land
  in the workflow without an extra agent round-trip.
* ``submit_candidate`` — bridges to a workflow Update, and
  importantly *attaches the ReflexiCoder reflection prompt* to
  failing candidates so Cursor has structured introspection.
* ``update_companion_tests`` — refines the auto-scaffolded
  pwspec / spec.tsx mid-iteration.
* ``get_component_status`` — operator/demo-UI snapshot of
  workflow state.

For the Temporal-touching tools we use a stub
:class:`temporalio.client.Client`-shaped object that records calls
and returns canned responses. The actual workflow mechanics are
already locked down by ``test_workflow_workflow.py``; here we're
only verifying that the MCP server forwards args + return values
correctly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from prism_mcp.cache import Cache
from prism_mcp.config import ServerConfig
from prism_mcp.embeddings import Encoder
from prism_mcp.library import Library
from prism_mcp.registry import RegistryClient
from prism_mcp.server import build_server
from prism_mcp.workflow.contracts import (
    CandidateResult,
    SsimVerdict,
    ValidatorKind,
    ValidatorResult,
    WorkflowStatus,
)

# --------------------------------------------------------------------------
# Reused stubs from the existing slice-9 tests (kept local so each
# test file can be reasoned about in isolation).
# --------------------------------------------------------------------------


PACKAGE = "@nutanix-ui/prism-reactjs"
VERSION = "2.54.0"
TARBALL_URL = f"https://reg.test/{PACKAGE}/-/prism-reactjs-{VERSION}.tgz"


def _stub_encoder() -> Encoder:
    """Deterministic 16-d encoder for hermetic embedding tests."""

    def encode(texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        out = np.zeros((len(texts), 16), dtype=np.float32)
        for i, text in enumerate(texts):
            digest = hashlib.sha256(text.encode("utf-8")).digest()[:16]
            raw = np.frombuffer(digest, dtype=np.uint8).astype(np.float32)
            norm = np.linalg.norm(raw)
            out[i] = raw / norm if norm > 0 else raw
        return out

    return encode


def _stub_reranker():
    def rerank(query: str, documents: list[str]) -> np.ndarray:
        scores = np.zeros(len(documents), dtype=np.float32)
        for i, doc in enumerate(documents):
            payload = (query + "\0" + doc).encode("utf-8")
            scores[i] = hashlib.sha256(payload).digest()[0] / 255.0
        return scores

    return rerank


def _library_factory(
    cache_root: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    encoder: Encoder | None = None,
    reranker: object | None = None,
) -> Callable[[], Library]:
    """Library-builder wired to a MockTransport for tests."""
    config = ServerConfig(
        registry_base_url="https://reg.test/api/npm/canaveral-npm/",
        package_name=PACKAGE,
        cache_dir=cache_root,
        auth_header="Basic dGVzdA==",
    )

    def factory() -> Library:
        client = RegistryClient(
            base_url=config.registry_base_url,
            auth_header=config.auth_header,
            transport=httpx.MockTransport(handler),
        )
        return Library(
            config=config,
            registry=client,
            cache=Cache(cache_root),
            encoder=encoder,
            reranker=reranker,  # type: ignore[arg-type]
        )

    return factory


def _registry_handler(tarball: bytes, document: dict) -> Callable:
    """Return a MockTransport handler serving a fixed tarball/manifest."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TARBALL_URL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(
            200,
            headers={"ETag": '"v1"'},
            content=json.dumps(document),
        )

    return handler


def _structured_dict(structured: object) -> dict:
    """FastMCP returns dict-or-list-shaped structured payloads."""
    assert isinstance(structured, dict)
    return structured


# --------------------------------------------------------------------------
# Test: every new tool appears in list_tools().
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listed_tools_include_slice_12_surface() -> None:
    """Slice-12 surface + slice-12 design-to-code helpers must all register.

    The base slice-12 set is the 5 generation/reflection tools plus
    ``get_final_artefact`` (the 12.5 delivery hop). The design-to-
    code additions are ``map_figma_node`` (composite Figma → Prism
    mapper) and the two library-asset readers
    (``get_pwspec_example`` / ``get_snapshot_template``) that surface
    the source-tree-only assets that don't ship in the npm tarball.
    """
    server = build_server()

    tools = await server.list_tools()
    names = {tool.name for tool in tools}

    # The post-consolidation slice-12 surface (10 LLM-facing
    # tools + 3 marked INTERNAL via docstring prefix). The
    # asserted set is the *minimum* — extra tools registered by
    # later slices are tolerated.
    assert {
        "compare_to_figma",
        "start_generate_component",
        "submit_candidate",
        "update_companion_tests",
        "get_component_status",
        "get_final_artefact",
        "map_figma_node",
        "get_pwspec_example",
    } <= names

    # Tools removed in the slice-12.x consolidation must NOT be
    # registered — guards against an accidental re-introduction.
    for removed in (
        "list_entities",
        "check_contrast",
        "get_component_cluster",
        "get_snapshot_template",
        "map_token",
        "related_components",
        "get_a11y_rules",
        "reflect_on_spec",
    ):
        assert removed not in names, (
            f"{removed!r} was consolidated out — re-registering it "
            "violates the post-consolidation tool budget"
        )


# --------------------------------------------------------------------------
# compare_to_figma — pure SSIM, no Temporal.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_to_figma_returns_pass_for_identical_pngs(
    tmp_path: Path,
) -> None:
    """Identical PNGs → score~=1.0 → bucket=pass."""
    png = tmp_path / "identical.png"
    Image.new("RGB", (64, 64), (200, 200, 200)).save(png)

    server = build_server()
    _, structured = await server.call_tool(
        "compare_to_figma",
        {
            "figma_png_path": str(png),
            "rendered_png_path": str(png),
        },
    )

    body = _structured_dict(structured)
    assert body["bucket"] == "pass"
    assert body["score"] == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_compare_to_figma_returns_fail_for_different_pngs(
    tmp_path: Path,
) -> None:
    """Inverted PNGs → bucket=fail."""
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    Image.new("RGB", (64, 64), (240, 240, 240)).save(a)
    img_b = Image.new("RGB", (64, 64), (240, 240, 240))
    ImageDraw.Draw(img_b).rectangle((0, 0, 64, 32), fill=(0, 0, 0))
    img_b.save(b)

    server = build_server()
    _, structured = await server.call_tool(
        "compare_to_figma",
        {"figma_png_path": str(a), "rendered_png_path": str(b)},
    )

    body = _structured_dict(structured)
    assert body["bucket"] == "fail"
    assert body["region"] is not None


# --------------------------------------------------------------------------
# Stub Temporal client for the workflow-touching tools.
# --------------------------------------------------------------------------


def _stub_temporal_client(
    *,
    workflow_id: str = "wf-test-1",
    update_result: CandidateResult | None = None,
    query_result: WorkflowStatus | None = None,
) -> MagicMock:
    """Build a MagicMock shaped like a Temporal ``Client``.

    The MCP tools we test only call three Client methods:
    ``start_workflow``, ``get_workflow_handle``, and indirectly the
    handle's ``execute_update`` + ``query``. We stub at the handle
    level so the same fixture serves all three workflow tools.
    """
    handle = MagicMock(name="workflow_handle")
    handle.id = workflow_id
    handle.execute_update = AsyncMock(return_value=update_result)
    handle.query = AsyncMock(return_value=query_result)

    client = MagicMock(name="temporal_client")
    client.start_workflow = AsyncMock(return_value=handle)
    client.get_workflow_handle = MagicMock(return_value=handle)
    return client


def _passing_candidate() -> CandidateResult:
    """Helper: a fully-green CandidateResult."""
    return CandidateResult(
        iteration=1,
        component_name="Modal",
        validators=[
            ValidatorResult(
                kind=ValidatorKind.typecheck,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=1,
            ),
        ],
        ssim=None,
    )


def _failing_candidate() -> CandidateResult:
    """Helper: a failing CandidateResult with a real SSIM verdict."""
    return CandidateResult(
        iteration=2,
        component_name="Modal",
        validators=[
            ValidatorResult(
                kind=ValidatorKind.eslint,
                exit_code=1,
                stdout_tail="UNIQUE_LINT_ERROR",
                stderr_tail="",
                duration_ms=1,
            ),
        ],
        ssim=SsimVerdict(score=0.5, region="top-left"),
    )


# --------------------------------------------------------------------------
# start_generate_component
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_generate_component_returns_workflow_id() -> None:
    """The tool starts a workflow and returns its ID for follow-ups."""
    client = _stub_temporal_client(workflow_id="wf-new-1")
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "start_generate_component",
        {
            "component_name": "Modal",
            "services_root": "/srv/prism/services",
        },
    )

    body = _structured_dict(structured)
    assert body["workflow_id"] == "wf-new-1"
    assert body["component_name"] == "Modal"
    client.start_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_generate_component_forwards_figma_png_url() -> None:
    """``figma_png_url`` arg lands on ``WorkflowStartInput`` verbatim.

    This is the load-bearing change for the Figma MCP integration:
    Figma returns URLs, not paths, and the workflow's
    ``materialise_figma_reference`` activity is what makes that
    work. The tool must forward the URL untouched so the activity
    can download it.
    """
    client = _stub_temporal_client(workflow_id="wf-url-1")
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    await server.call_tool(
        "start_generate_component",
        {
            "component_name": "Modal",
            "services_root": "/srv/prism/services",
            "figma_png_url": "https://figma.example/x.png",
        },
    )

    client.start_workflow.assert_awaited_once()
    call = client.start_workflow.await_args
    # The second positional arg is the WorkflowStartInput payload.
    start_input = call.args[1]
    assert start_input.figma_png_url == "https://figma.example/x.png"
    assert start_input.figma_png_path is None
    assert start_input.figma_png_base64 is None
    assert start_input.has_figma_reference is True


@pytest.mark.asyncio
async def test_start_generate_component_forwards_figma_png_base64() -> None:
    """``figma_png_base64`` is forwarded so the activity can decode it."""
    client = _stub_temporal_client(workflow_id="wf-b64-1")
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    await server.call_tool(
        "start_generate_component",
        {
            "component_name": "Modal",
            "services_root": "/srv/prism/services",
            "figma_png_base64": "aGVsbG8=",
        },
    )

    start_input = client.start_workflow.await_args.args[1]
    assert start_input.figma_png_base64 == "aGVsbG8="
    assert start_input.has_figma_reference is True


@pytest.mark.asyncio
async def test_start_generate_component_returns_context_bundle_on_fallback() -> (
    None
):
    """Library acquisition failure → ``context`` bundle is empty but
    schema-stable.

    Without a configured library_factory the slice-9..11 indices are
    unreachable; the helper must catch the failure and return an
    empty-list shape so the LLM still sees a parseable response.
    The ``imitation_pwspec`` sub-dict is always present and reports
    ``found=False`` when no services_root pwspec matches.
    """
    client = _stub_temporal_client(workflow_id="wf-ctx-fallback")
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "start_generate_component",
        {
            "component_name": "Modal",
            "services_root": "/nonexistent/services",
        },
    )
    body = _structured_dict(structured)
    assert "context" in body
    ctx = body["context"]
    assert ctx["component_name"] == "Modal"
    assert ctx["examples"] == []
    assert ctx["related"] == []
    assert ctx["a11y_blocks"] == []
    assert ctx["token_hints"] == []
    pwspec = ctx["imitation_pwspec"]
    assert pwspec["found"] is False
    assert pwspec["component_name"] == "Modal"


@pytest.mark.asyncio
async def test_start_generate_component_returns_imitation_pwspec_when_present(
    tmp_path: Path,
) -> None:
    """An existing ``services/src/components/v2/<X>/<X>.pwspec.ts`` is
    surfaced via the ``imitation_pwspec`` sub-dict so the LLM has a
    concrete authoring template at iteration 1.

    Regression for the May-2026 testing report where iteration 1
    produced thin smoke tests because the LLM had no model of what
    a real Prism pwspec looks like.
    """
    services = tmp_path / "services"
    component_dir = services / "src" / "components" / "v2" / "Modal"
    component_dir.mkdir(parents=True)
    pwspec_path = component_dir / "Modal.pwspec.ts"
    pwspec_path.write_text(
        "import { test } from '@playwright/test';\n"
        "test('Modal visual', async () => {});\n",
        encoding="utf-8",
    )

    client = _stub_temporal_client(workflow_id="wf-ctx-pwspec")
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "start_generate_component",
        {
            "component_name": "Modal",
            "services_root": str(services),
        },
    )
    body = _structured_dict(structured)
    pwspec = body["context"]["imitation_pwspec"]
    assert pwspec["found"] is True
    assert pwspec["path"] == str(pwspec_path)
    assert "test('Modal visual'" in pwspec["code"]


@pytest.mark.asyncio
async def test_start_generate_component_accepts_spec_text() -> None:
    """``spec_text`` is plumbed through without breaking the response shape.

    The helper uses it as the searcher query when present; when
    omitted it falls back to ``component_name``. Either way, the
    response must still carry ``workflow_id`` + ``context``.
    """
    client = _stub_temporal_client(workflow_id="wf-spec-1")
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "start_generate_component",
        {
            "component_name": "Modal",
            "services_root": "/srv/prism/services",
            "spec_text": "Confirmation modal with destructive primary button #DC2626",
        },
    )

    body = _structured_dict(structured)
    assert body["workflow_id"] == "wf-spec-1"
    assert "context" in body
    # The workflow ID round-trip is the spine the rest of the agent
    # loop hangs off of; the call must not silently drop it when
    # context-building takes a fallback path.
    client.start_workflow.assert_awaited_once()


# --------------------------------------------------------------------------
# submit_candidate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_candidate_returns_validator_panel_on_pass() -> None:
    """On a passing candidate, the tool returns the panel + delivery hint.

    The delivery hint is the slice-12.5 contract: passing
    candidates must surface a concrete "now call
    get_final_artefact" instruction so the agent loop is
    reminded to deliver the validated code into the user's
    actual project tree (not leave it sitting in scratch/).
    """
    client = _stub_temporal_client(update_result=_passing_candidate())
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "submit_candidate",
        {"workflow_id": "wf-1", "jsx_code": "<x/>"},
    )

    body = _structured_dict(structured)
    assert body["all_passed"] is True
    assert body.get("reflection_prompt", "") == ""
    assert "get_final_artefact" in body["delivery_hint"]
    assert "Modal" in body["delivery_hint"]  # component echoed in hint


@pytest.mark.asyncio
async def test_submit_candidate_delivery_hint_empty_on_failure() -> None:
    """Failing candidates carry an empty ``delivery_hint`` so a
    wrapping client can use the field as a falsy guard.
    """
    client = _stub_temporal_client(update_result=_failing_candidate())
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "submit_candidate",
        {"workflow_id": "wf-1", "jsx_code": "<x/>"},
    )

    body = _structured_dict(structured)
    assert body["all_passed"] is False
    assert body["delivery_hint"] == ""


@pytest.mark.asyncio
async def test_submit_candidate_attaches_reflection_prompt_on_failure() -> None:
    """Failing candidate → tool attaches the ReflexiCoder reflection prompt.

    This is the SOTA-named UX: Cursor gets the structured 3-question
    introspection wrapper synchronously, so the next iteration's
    code-gen has the prompt readily.
    """
    client = _stub_temporal_client(update_result=_failing_candidate())
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "submit_candidate",
        {"workflow_id": "wf-1", "jsx_code": "<x/>"},
    )

    body = _structured_dict(structured)
    assert body["all_passed"] is False
    prompt = body["reflection_prompt"]
    assert "What was your assumption" in prompt
    assert "UNIQUE_LINT_ERROR" in prompt


# --------------------------------------------------------------------------
# update_companion_tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_companion_tests_forwards_pwspec_and_spec() -> None:
    """The tool wraps the workflow update's :class:`UpdateCompanionTestsInput`.

    Smoke test that both companion-file slots make it from the
    MCP tool surface through to the workflow update handler.
    """
    from prism_mcp.workflow.contracts import UpdateCompanionTestsResult

    update_result = UpdateCompanionTestsResult(
        component_name="Modal",
        wrote_pwspec=True,
        wrote_spec=True,
        pwspec_path="/scratch/Modal/Modal.pwspec.ts",
        spec_path="/scratch/Modal/Modal.spec.tsx",
    )
    client = _stub_temporal_client()
    client.get_workflow_handle.return_value.execute_update = AsyncMock(
        return_value=update_result
    )
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "update_companion_tests",
        {
            "workflow_id": "wf-1",
            "pwspec_code": "// pwspec body",
            "spec_code": "// spec body",
        },
    )

    body = _structured_dict(structured)
    assert body["wrote_pwspec"] is True
    assert body["wrote_spec"] is True
    assert body["component_name"] == "Modal"
    assert "submit_candidate" in body["next_step_hint"]


@pytest.mark.asyncio
async def test_update_companion_tests_optional_args_default_to_none() -> None:
    """Calling with neither arg is allowed; the workflow rejects it
    on its own (covered by ``test_workflow_workflow``). Here we
    just verify the MCP tool layer doesn't gate on either arg
    being supplied.
    """
    from prism_mcp.workflow.contracts import UpdateCompanionTestsResult

    update_result = UpdateCompanionTestsResult(
        component_name="Modal",
        wrote_pwspec=False,
        wrote_spec=False,
        pwspec_path="/scratch/Modal/Modal.pwspec.ts",
        spec_path="/scratch/Modal/Modal.spec.tsx",
    )
    client = _stub_temporal_client()
    client.get_workflow_handle.return_value.execute_update = AsyncMock(
        return_value=update_result
    )
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "update_companion_tests",
        {"workflow_id": "wf-noop"},
    )
    body = _structured_dict(structured)
    assert body["wrote_pwspec"] is False
    assert body["wrote_spec"] is False


# --------------------------------------------------------------------------
# get_component_status
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_component_status_returns_workflow_state() -> None:
    """Query result returned verbatim as a dict."""
    status = WorkflowStatus(
        workflow_id="wf-poll-1",
        component_name="Modal",
        iteration=2,
        max_iterations=3,
        last_result=_failing_candidate(),
        final_state="running",
    )
    client = _stub_temporal_client(query_result=status)
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "get_component_status",
        {"workflow_id": "wf-poll-1"},
    )

    body = _structured_dict(structured)
    assert body["workflow_id"] == "wf-poll-1"
    assert body["iteration"] == 2
    assert body["final_state"] == "running"


# --------------------------------------------------------------------------
# get_final_artefact — the slice-12.5 delivery tool. Reads from the
# scratch dir written by the workflow and returns the bytes so the
# agent can write them into the user's project.
# --------------------------------------------------------------------------


def _materialise_scratch_artefacts(
    services_root: Path,
    component_name: str,
    *,
    jsx_body: str,
    pwspec_body: str | None = None,
    tsconfig_body: str | None = None,
) -> Path:
    """Write the same files the ``write_candidate_files`` activity
    would have written for a passing workflow. Returns the
    candidate's scratch dir.
    """
    scratch_dir = (
        services_root / "src" / "scratch" / "Generated" / component_name
    )
    scratch_dir.mkdir(parents=True, exist_ok=True)
    (scratch_dir / f"{component_name}.jsx").write_text(
        jsx_body, encoding="utf-8"
    )
    if pwspec_body is not None:
        (scratch_dir / f"{component_name}.pwspec.ts").write_text(
            pwspec_body, encoding="utf-8"
        )
    if tsconfig_body is not None:
        (scratch_dir / "tsconfig.json").write_text(
            tsconfig_body, encoding="utf-8"
        )
    return scratch_dir


@pytest.mark.asyncio
async def test_get_final_artefact_returns_jsx_and_pwspec_on_pass(
    tmp_path: Path,
) -> None:
    """Happy path: workflow passed → tool returns the validated bytes.

    Exercises every key in the response contract so a regression
    in any field gets caught at the wiring layer.
    """
    services_root = tmp_path / "services"
    component_name = "ConfirmDeleteModal"
    jsx_body = "export const ConfirmDeleteModal = () => null;\n"
    pwspec_body = "import { test } from '@playwright/test';\n"
    tsconfig_body = '{"extends": "../../../../tsconfig.json"}\n'
    scratch_dir = _materialise_scratch_artefacts(
        services_root,
        component_name,
        jsx_body=jsx_body,
        pwspec_body=pwspec_body,
        tsconfig_body=tsconfig_body,
    )
    status = WorkflowStatus(
        workflow_id="wf-passed-1",
        component_name=component_name,
        services_root=str(services_root),
        iteration=3,
        max_iterations=3,
        last_result=_passing_candidate(),
        final_state="passed",
        delivery_hint="(unused by this tool)",
    )
    client = _stub_temporal_client(query_result=status)
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "get_final_artefact",
        {"workflow_id": "wf-passed-1"},
    )

    body = _structured_dict(structured)
    assert body["workflow_id"] == "wf-passed-1"
    assert body["component_name"] == component_name
    assert body["final_state"] == "passed"
    assert body["services_root"] == str(services_root)
    assert body["scratch_dir"] == str(scratch_dir)
    assert body["jsx_code"] == jsx_body
    assert body["companion_test_code"] == pwspec_body
    assert body["tsconfig_json"] == tsconfig_body
    assert component_name in body["suggested_target_path"]
    assert body["warning"] is None


@pytest.mark.asyncio
async def test_get_final_artefact_omits_optional_files_when_absent(
    tmp_path: Path,
) -> None:
    """No companion pwspec on disk → ``companion_test_code`` is ``None``.

    Some iterations pass on tsc + eslint + jest --passWithNoTests
    without ever needing a pwspec. The tool must not invent one
    or raise — just report ``None`` so the agent skips delivery
    of the optional artefact.
    """
    services_root = tmp_path / "services"
    component_name = "Btn"
    _materialise_scratch_artefacts(
        services_root,
        component_name,
        jsx_body="export const Btn = () => null;\n",
        # No pwspec, no tsconfig.
    )
    status = WorkflowStatus(
        workflow_id="wf-no-pwspec",
        component_name=component_name,
        services_root=str(services_root),
        iteration=1,
        max_iterations=3,
        last_result=_passing_candidate(),
        final_state="passed",
        delivery_hint="(unused)",
    )
    client = _stub_temporal_client(query_result=status)
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "get_final_artefact",
        {"workflow_id": "wf-no-pwspec"},
    )

    body = _structured_dict(structured)
    assert body["jsx_code"].startswith("export const Btn")
    assert body["companion_test_code"] is None
    assert body["tsconfig_json"] is None


@pytest.mark.asyncio
async def test_get_final_artefact_warns_when_workflow_not_passed(
    tmp_path: Path,
) -> None:
    """Workflow still running → response includes a ``warning`` field.

    The agent should re-check ``failing_kinds`` before delivering
    a not-yet-passed artefact to the user. Don't fail the call
    (the artefact might still be useful for debugging), just
    flag it.
    """
    services_root = tmp_path / "services"
    component_name = "WIP"
    _materialise_scratch_artefacts(
        services_root,
        component_name,
        jsx_body="// half-done\n",
    )
    status = WorkflowStatus(
        workflow_id="wf-running",
        component_name=component_name,
        services_root=str(services_root),
        iteration=1,
        max_iterations=3,
        last_result=_failing_candidate(),
        final_state="running",
    )
    client = _stub_temporal_client(query_result=status)
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    _, structured = await server.call_tool(
        "get_final_artefact",
        {"workflow_id": "wf-running"},
    )

    body = _structured_dict(structured)
    assert body["warning"] is not None
    assert "running" in body["warning"]
    assert body["jsx_code"] == "// half-done\n"


@pytest.mark.asyncio
async def test_get_final_artefact_raises_when_scratch_jsx_missing(
    tmp_path: Path,
) -> None:
    """Hard failure when the workflow says it ran but the JSX file
    isn't on disk — most likely cause is an operator wiping the
    scratch dir. Loud is better than silently returning ``None``.
    """
    services_root = tmp_path / "services"
    services_root.mkdir()
    # No scratch dir created.
    status = WorkflowStatus(
        workflow_id="wf-wiped",
        component_name="Gone",
        services_root=str(services_root),
        iteration=1,
        max_iterations=3,
        last_result=_passing_candidate(),
        final_state="passed",
        delivery_hint="(unused)",
    )
    client = _stub_temporal_client(query_result=status)
    server = build_server(
        temporal_client_factory=AsyncMock(return_value=client)
    )

    # FastMCP wraps tool exceptions; we accept either FileNotFoundError
    # directly or a structured tool-error. The important guarantee is
    # that the missing artefact is surfaced, not silently swallowed.
    with pytest.raises(Exception, match="No scratch JSX found"):
        await server.call_tool(
            "get_final_artefact",
            {"workflow_id": "wf-wiped"},
        )
