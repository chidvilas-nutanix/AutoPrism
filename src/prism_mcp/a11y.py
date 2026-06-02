"""Accessibility-rules surface for the slice-11 ``get_a11y_rules`` tool.

Prism ships **two** kinds of a11y guidance the LLM can consume:

1. **Global rules** in ``package/LLMS.md`` at the tarball root —
   project-wide directives like "treat ``Don't`` sections as
   anti-patterns" or "always validate against ``.d.ts``". Plain
   markdown with H2/H3 sections.

2. **Per-component a11y blocks** inside each ``*.examples.md`` file,
   marked with ``jsx noeditor`` fences and an ``A11y`` /
   ``Accessibility`` heading. The slice-9 parser already extracts
   these — every chunk with ``is_a11y_block=True`` is one of them.
   Here we just group + project them into a tool-friendly shape.

This module deliberately does *not* try to be a full markdown
renderer. Its job is to surface the *raw* a11y prose to Cursor so
the LLM can read it as input context; rendering is the LLM's
problem.

Public surface:

* :class:`A11yRules` Pydantic schema with ``global_rules`` (one
  per H2/H3 section in LLMS.md) and ``per_component`` (one entry
  per component that has any a11y block).
* :func:`build_a11y_rules` builds the schema from a tarball root +
  pre-extracted :class:`ExampleChunk` list.
* :func:`get_a11y_for_component` shortcuts to one component's rules,
  for the slice-11 ``get_a11y_rules(name="Modal")`` tool path.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from prism_mcp.parsers.examples_md_code import ExampleChunk

logger = logging.getLogger(__name__)

#: Where LLMS.md lives inside the tarball. Mirrors the
#: ``services/package.json#files`` declaration in the upstream
#: prism-ui repo.
LLMS_MD_FILENAME = "LLMS.md"

# Per-line markdown header detection.  The LLMS.md uses H1 for the
# document title and H2/H3 for individual rule groups; we treat any
# heading at depth >= 2 as the start of a new global-rules section.
# H1 (single ``#``) becomes the document title.
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")


class A11ySection(BaseModel):
    """One section of LLMS.md keyed by its heading.

    Attributes:
        heading (str): the heading text (without the leading ``#``).
        body (str): the prose under the heading up to the next
            heading of the same-or-shallower depth. Trailing
            whitespace stripped, internal blank lines preserved.
        depth (int): 2 for ``##``, 3 for ``###``, etc.
    """

    model_config = ConfigDict(frozen=True)

    heading: str
    body: str
    depth: int


class ComponentA11y(BaseModel):
    """All a11y guidance for one component.

    Attributes:
        component_name (str): e.g. ``"Modal"``.
        blocks (list[str]): the raw code/markdown bodies of the
            a11y chunks for this component, in order of appearance.
            We surface them as strings so the LLM can read both
            prose ("Make sure focus returns to the trigger.") and
            code snippets ("``<Modal ariaLabelledBy=...>``")
            verbatim.
        titles (list[str]): example titles aligned 1:1 with
            ``blocks`` so the LLM can correlate the prose with its
            origin section.
    """

    component_name: str
    blocks: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)


class A11yRules(BaseModel):
    """Tool-facing a11y aggregation for the whole library.

    Attributes:
        title (str | None): the H1 of LLMS.md, if any
            (e.g. ``"LLM Instructions for @nutanix-ui/prism-reactjs"``).
        global_rules (list[A11ySection]): every H2/H3 section in
            LLMS.md, in document order. Empty when no LLMS.md is
            shipped in the tarball.
        per_component (list[ComponentA11y]): one entry per component
            that has at least one ``is_a11y_block=True`` chunk in
            its ``*.examples.md`` file.

    Lookup performance: :meth:`find_by_component` builds a one-shot
    ``component_name → ComponentA11y`` dict on first call (cached as
    a private attribute) so subsequent lookups are O(1). The Figma
    walker calls into this path once per agenda row; on big pages
    (~50 regions) the linear scan would otherwise dominate the
    aggregator's cost. Build is lazy so the per-walk overhead is
    paid only when a real lookup happens.
    """

    title: str | None = None
    global_rules: list[A11ySection] = Field(default_factory=list)
    per_component: list[ComponentA11y] = Field(default_factory=list)

    _by_component_cache: dict[str, ComponentA11y] | None = PrivateAttr(
        default=None
    )

    def find_by_component(self, name: str) -> ComponentA11y | None:
        """Return the :class:`ComponentA11y` for ``name`` in O(1).

        The lookup is case-sensitive (Prism component identifiers
        are case-sensitive) and mirrors the contract of the
        module-level :func:`get_a11y_for_component` helper.

        First call materialises the lookup dict from
        :attr:`per_component`; subsequent calls reuse it. Safe to
        call from multiple threads in practice — concurrent first
        callers race to build the same dict but both write the same
        value, and dict assignment in CPython is atomic, so the
        worst case is one extra rebuild on a cold cache.
        """
        cache = self._by_component_cache
        if cache is None:
            cache = {c.component_name: c for c in self.per_component}
            self._by_component_cache = cache
        return cache.get(name)


def build_a11y_rules(
    package_root: Path,
    chunks: Iterable[ExampleChunk],
) -> A11yRules:
    """Aggregate global + per-component a11y guidance.

    Args:
        package_root (Path): the extracted ``package/`` directory.
        chunks (Iterable[ExampleChunk]): chunks from
            :func:`prism_mcp.parsers.examples_md_code.walk_example_chunks`.
            Only those with ``is_a11y_block=True`` are consumed.

    Returns:
        A11yRules: the assembled rules. ``global_rules`` is empty if
        ``LLMS.md`` is missing; ``per_component`` is empty if no
        chunk is flagged ``is_a11y_block``.
    """
    title, global_rules = _parse_llms_md(package_root)
    per_component = _aggregate_per_component(chunks)
    return A11yRules(
        title=title,
        global_rules=global_rules,
        per_component=per_component,
    )


def get_a11y_for_component(name: str, rules: A11yRules) -> ComponentA11y | None:
    """Return the :class:`ComponentA11y` for ``name``, or ``None``.

    The lookup is **case-sensitive** because Prism component names
    are case-sensitive identifiers (``Modal`` vs ``modal`` are
    different).

    Delegates to :meth:`A11yRules.find_by_component` so the lazy
    dict cache is shared with the Figma walker's per-region lookup
    path. Backward-compatible — the public signature and behaviour
    are unchanged.

    Args:
        name (str): component name (e.g. ``"Modal"``).
        rules (A11yRules): the aggregated rules.

    Returns:
        ComponentA11y | None: the row for that component, if any.
    """
    return rules.find_by_component(name)


def _parse_llms_md(
    package_root: Path,
) -> tuple[str | None, list[A11ySection]]:
    """Parse ``LLMS.md`` into (title, sections).

    Returns ``(None, [])`` if the file is missing or unreadable —
    we don't raise because a missing LLMS.md is a degraded mode but
    not a server-fatal one.
    """
    path = package_root / LLMS_MD_FILENAME
    if not path.is_file():
        logger.info("no LLMS.md at %s; a11y global rules will be empty", path)
        return None, []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("failed to read LLMS.md at %s: %s", path, exc)
        return None, []

    title: str | None = None
    sections: list[A11ySection] = []
    current_heading: str | None = None
    current_depth: int | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        """Push the in-flight section onto ``sections``."""
        if current_heading is not None and current_depth is not None:
            body = "\n".join(current_lines).strip("\n")
            sections.append(
                A11ySection(
                    heading=current_heading,
                    body=body,
                    depth=current_depth,
                )
            )

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match is None:
            current_lines.append(line)
            continue
        depth = len(match.group("hashes"))
        heading_text = match.group("text").strip()
        if depth == 1 and title is None:
            # First H1 wins as title; subsequent H1s become depth-1
            # sections (rare in practice — LLMS.md uses H2/H3).
            title = heading_text
            continue
        # Flush whatever was in progress before opening the new one.
        _flush()
        current_heading = heading_text
        current_depth = depth
        current_lines = []

    _flush()
    return title, sections


def _aggregate_per_component(
    chunks: Iterable[ExampleChunk],
) -> list[ComponentA11y]:
    """Group ``is_a11y_block`` chunks by component, preserving order.

    Multiple a11y blocks per component are concatenated as separate
    list entries — we don't merge their bodies because the LLM
    benefits from knowing where one rule ends and the next begins.
    """
    # Use insertion-ordered dict so the result mirrors the order
    # components were first encountered in the corpus.
    by_component: dict[str, ComponentA11y] = {}
    for chunk in chunks:
        if not chunk.is_a11y_block:
            continue
        row = by_component.setdefault(
            chunk.component_name,
            ComponentA11y(component_name=chunk.component_name),
        )
        row.blocks.append(chunk.code)
        row.titles.append(chunk.title)
    return list(by_component.values())
