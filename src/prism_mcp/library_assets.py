"""Look up canonical test/snapshot patterns from the Prism source tree.

The Prism npm tarball *excludes* ``.pwspec.ts``, ``.snap`` and
``.spec.*`` files (per ``services/package.json``'s ``files`` array,
which has ``!**/*.pwspec.ts`` / ``!**/*.snap`` / ``!**/*.spec.*``
exclusions). So the MCP server's regular index — which reads from
``~/.cache/prism-mcp/<version>/package/`` — never sees them.

But those files are *gold* for the slice-12 iteration loop:

* ``<Name>.pwspec.ts`` is the canonical Playwright + axe-core test
  pattern for that component. Even though the existing pwspec uses
  Prism's own ``playwright-util`` helpers (``visitPage``, ``themes``,
  ``auditScreenshotHelper``) that won't run against scratch JSX,
  the *structure* (``test.describe`` per theme, locator + visual
  regression + a11y in one file) is what the AlphaCodium AI-test
  stage should imitate.
* ``__snapshots__/<Name>.spec.tsx.snap`` is the canonical
  rendered-DOM shape: every element, class, and ``data-*`` attribute
  the component produces in its default state. The LLM can use it
  as a structural ground truth when deciding which Prism subcomponents
  + props to compose into a candidate.

Path layout (read from ``services_root``)
-----------------------------------------

The library groups components by *family* under
``src/components/v2/<group>/``, with the file stem matching the
component identifier::

    services/src/components/v2/Button/Button.pwspec.ts
    services/src/components/v2/Button/Button.spec.tsx
    services/src/components/v2/Button/__snapshots__/Button.spec.tsx.snap
    services/src/components/v2/Form/FormItemDatePicker.pwspec.ts
    services/src/components/v2/Form/__snapshots__/FormItemDatePicker.spec.tsx.snap

The ``<group>`` varies (``Button``, ``Form``, ``Navigation``,
``Tables``, ``Utility``, ``Layouts``, etc.) so we glob the v2
directory and match on the file stem.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


_V2_COMPONENTS_REL = ("src", "components", "v2")
"""Path segments from ``services_root`` to the v2 component tree.

Centralised so the pwspec + snapshot lookups can't drift apart on
where the Prism source repo keeps its components. If the library
ever moves to ``src/components/v3``, only this constant changes.
"""


_PWSPEC_HEAD_BYTES = 6_000
"""Cap on how many bytes of pwspec to return inline.

The largest pwspec in the repo (``Modal.pwspec.ts``) is about
10 KB. The LLM benefits most from the *first* few hundred lines
(test.describe scaffolding, themes loop, locator construction);
the tail is usually variant enumeration that the agent doesn't
need. 6000 bytes captures the structure without bloating the
tool result.
"""


_SNAPSHOT_HEAD_BYTES = 4_000
"""Cap on how many bytes of snapshot to return inline.

Jest ``.snap`` files can balloon to >50 KB for components with
many variants (``Icons.spec.tsx.snap`` is ~80 KB). The default
projection is the first ~4 KB, which holds the very first
``exports[...]`` block — typically the "renders default state"
shape, which is the most useful canonical pattern.
"""


# --------------------------------------------------------------------------
# Data shapes — Pydantic for easy MCP tool-result serialisation.
# --------------------------------------------------------------------------


class PwspecExample(BaseModel):
    """Output of :func:`find_pwspec_example`.

    Args:
        component_name (str): echoed input.
        found (bool): ``True`` when a matching pwspec exists.
        path (str | None): absolute path to the pwspec file, or
            ``None`` on miss. Absolute (not relative to
            ``services_root``) so the agent can pass it back to
            another tool without re-resolving.
        code (str | None): the pwspec source, capped at
            :data:`_PWSPEC_HEAD_BYTES`. Includes a ``// truncated``
            marker when capped.
        note (str): human-readable advisory. On hit, it
            describes how the example differs from a scratch
            pwspec (``playwright-util`` helpers won't work).
            On miss, it suggests the agent fall back to
            writing a pwspec from scratch.
    """

    model_config = ConfigDict(extra="forbid")

    component_name: str
    found: bool
    path: str | None = None
    code: str | None = None
    note: str = ""


class SnapshotTemplate(BaseModel):
    """Output of :func:`find_snapshot_template`.

    Args:
        component_name (str): echoed input.
        found (bool): ``True`` when a matching snapshot exists.
        path (str | None): absolute path to the ``.snap`` file.
        content (str | None): snapshot content, capped at
            :data:`_SNAPSHOT_HEAD_BYTES`.
        block_count (int): number of distinct ``exports[...]``
            blocks in the snapshot — gives the LLM a sense of
            how many variants the component covers.
        note (str): advisory text.
    """

    model_config = ConfigDict(extra="forbid")

    component_name: str
    found: bool
    path: str | None = None
    content: str | None = None
    block_count: int = 0
    note: str = ""


_AssetKind = Literal["pwspec", "snapshot"]


# --------------------------------------------------------------------------
# Public API.
# --------------------------------------------------------------------------


def find_pwspec_example(
    *,
    services_root: str | Path,
    component_name: str,
) -> PwspecExample:
    """Return the existing ``<Name>.pwspec.ts`` for the library component.

    Glob-walks ``<services_root>/src/components/v2/`` for the
    matching stem so callers don't have to know which group
    folder the component lives in. The first match wins; if the
    Prism repo ever has two components named the same we'd want
    a hard error here, but in practice the names are unique
    across the v2 tree.

    Args:
        services_root (str | Path): absolute path to the Prism
            library's ``services/`` directory.
        component_name (str): the component identifier (case-
            sensitive, PascalCase).

    Returns:
        PwspecExample: ``found=True`` with the pwspec content
        and an advisory note, or ``found=False`` with a
        fallback note when no match exists.
    """
    candidate = _locate_asset(
        services_root=services_root,
        component_name=component_name,
        kind="pwspec",
    )
    if candidate is None:
        return PwspecExample(
            component_name=component_name,
            found=False,
            note=(
                "No matching pwspec.ts in the Prism library for "
                f"'{component_name}'. Fall back to writing a "
                "Playwright + axe-core test from scratch — keep "
                "it scoped to the scratch dir and mount the "
                "candidate component directly (do not rely on "
                "Prism's `playwright-util.visitPage` helper, "
                "which depends on the styleguide build)."
            ),
        )
    raw = candidate.read_text(encoding="utf-8")
    truncated = _truncate(raw, _PWSPEC_HEAD_BYTES)
    logger.info(
        "located pwspec component=%s path=%s bytes=%d",
        component_name,
        candidate,
        len(raw),
    )
    return PwspecExample(
        component_name=component_name,
        found=True,
        path=str(candidate),
        code=truncated,
        note=(
            "This pwspec uses Prism's `playwright-util` helpers "
            "(visitPage, themes, auditScreenshotHelper) which "
            "depend on the styleguide build at services/www. "
            "When generating a pwspec for a scratch component, "
            "imitate the *structure* (test.describe per theme, "
            "locator targeting, screenshot + axe in one spec) "
            "but mount the component directly instead of "
            "visiting the styleguide. The conventional "
            "data-test-id selector pattern (e.g. "
            "`page.locator('[data-test-id=\"my-id\"]')`) still "
            "applies."
        ),
    )


def find_snapshot_template(
    *,
    services_root: str | Path,
    component_name: str,
) -> SnapshotTemplate:
    """Return the existing Jest ``.snap`` for the library component.

    Same glob strategy as :func:`find_pwspec_example`. Snapshots
    live one level deeper under ``__snapshots__/`` and are named
    ``<Name>.spec.tsx.snap`` (matching the ``<Name>.spec.tsx``
    file that produced them).

    Args:
        services_root (str | Path): see
            :func:`find_pwspec_example`.
        component_name (str): the component identifier.

    Returns:
        SnapshotTemplate: ``found=True`` with the snapshot
        excerpt + block count, or ``found=False`` with a note.
    """
    candidate = _locate_asset(
        services_root=services_root,
        component_name=component_name,
        kind="snapshot",
    )
    if candidate is None:
        return SnapshotTemplate(
            component_name=component_name,
            found=False,
            note=(
                "No matching Jest snapshot in the Prism library "
                f"for '{component_name}'. Fall back to inspecting "
                "the component's examples (`search_examples`) or "
                "its types (`get_entity`) for the expected DOM "
                "shape."
            ),
        )
    raw = candidate.read_text(encoding="utf-8")
    block_count = raw.count("\nexports[")
    truncated = _truncate(raw, _SNAPSHOT_HEAD_BYTES)
    logger.info(
        "located snapshot component=%s path=%s blocks=%d bytes=%d",
        component_name,
        candidate,
        block_count,
        len(raw),
    )
    return SnapshotTemplate(
        component_name=component_name,
        found=True,
        path=str(candidate),
        content=truncated,
        block_count=block_count,
        note=(
            "This snapshot captures the canonical rendered DOM "
            "(elements, classes, data-* attributes) for "
            f"'{component_name}'. Use it as a structural ground "
            "truth: a candidate component that wraps "
            f"`{component_name}` should produce DOM that mirrors "
            "these classes / data attributes in its tested "
            f"state. {block_count} variant block(s) total; only "
            "the first is shown here."
        ),
    )


# --------------------------------------------------------------------------
# Internal helpers.
# --------------------------------------------------------------------------


def _locate_asset(
    *,
    services_root: str | Path,
    component_name: str,
    kind: _AssetKind,
) -> Path | None:
    """Glob the v2 tree for the requested asset.

    Returns the first match (alphabetical by group folder name
    for determinism) or ``None`` on miss. Quietly returns
    ``None`` when ``services_root`` doesn't exist instead of
    raising — the MCP tool surface should never crash on a
    missing services_root; the LLM should hear "not found" and
    move on.
    """
    root = Path(services_root, *_V2_COMPONENTS_REL)
    if not root.is_dir():
        logger.info(
            "v2 components dir not found services_root=%s", services_root
        )
        return None

    if kind == "pwspec":
        glob = f"*/{component_name}.pwspec.ts"
    else:
        glob = f"*/__snapshots__/{component_name}.spec.tsx.snap"

    matches = sorted(root.glob(glob))
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "multiple %s matches for %s; using first %s",
            kind,
            component_name,
            matches[0],
        )
    return matches[0]


def _truncate(text: str, max_bytes: int) -> str:
    """Cap ``text`` at ``max_bytes`` of UTF-8, adding a marker."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    # Cap by *character* count using bytes as the proxy; close
    # enough for ASCII-dominant TypeScript / JSX source.
    head = text[:max_bytes]
    return head + "\n\n// ... truncated for MCP response size ..."
