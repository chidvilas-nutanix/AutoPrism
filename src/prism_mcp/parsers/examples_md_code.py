"""Code-body extractor for ``X.examples.md`` files.

Slice 9 needs richer metadata than :mod:`prism_mcp.parsers.examples_md`
captures — specifically: the raw code body, the *prism-reactjs imports*
present in it, the optional ``@example-id`` marker, and per-block flags
that let downstream consumers (the embedding index, the slice 11 a11y
aggregator) decide whether to include the chunk.

Design notes / why this lives in a sibling module:

* The existing :mod:`prism_mcp.parsers.examples_md` populates
  :class:`prism_mcp.entities.Example` which is consumed by every
  component entity built in Slice 3. Mutating it risks breaking the
  Slice 3 BM25 synthetic doc. We add a sibling parser instead so the
  v1 surface stays untouched (per the friend's CI invariant on the
  `team_unknown_rplib_poc` repo).
* This parser **captures everything** — even noeditor / anti-pattern
  blocks — and tags them. Filtering is a *caller* concern. The
  embedding index drops noeditor + anti-pattern + deprecated;
  ``get_a11y_rules`` (slice 11) keeps only ``is_a11y_block`` chunks.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# Source-of-truth path inside the extracted tarball. Mirrors the
# constant in ``parsers/components.py`` so the two parsers walk the
# same physical layout.
EXAMPLES_SRC_DIR = "src/components/v2"

# Source-of-truth for what "noeditor" and "Don't" mean. Mirrors the
# constants in ``examples_md.py``; kept private so the two parsers can
# diverge if real-world conventions split.
_FENCE_MARKER = "```"
_NO_EDITOR_TAG = "noeditor"
_DONT_HEADINGS = ("Don't", "Do not", "Anti-pattern")
_A11Y_HEADING_NEEDLE = "Accessibility"

# We treat ``jsx`` and ``jsx harmony`` as runnable react examples; the
# styleguidist `harmony` tag is a runtime annotation that just means
# "evaluate in the harmony scope". Any other fence (bash, text,
# typescript) is not extracted at all.
_JSX_LANGUAGE_TAGS = ("jsx",)

# ``import { A, B, C as Aliased } from '@nutanix-ui/prism-reactjs';``
# Multi-line braces appear in the real Button.examples.md so DOTALL is
# required. We deliberately match only the single Prism package so
# noise like ``import React from 'react'`` doesn't leak in.
_PRISM_IMPORT_RE = re.compile(
    r"""
    import \s*
    \{ (?P<names> [^}]+ ) \}
    \s* from \s*
    ['"]@nutanix-ui/prism-reactjs['"]
    """,
    re.VERBOSE | re.DOTALL,
)

# ``// @example-id <slug>`` — must be alone on a line (the styleguide's
# convention). Allow leading whitespace because some authors indent.
_EXAMPLE_ID_RE = re.compile(r"^\s*//\s*@example-id\s+(?P<id>[\w\-]+)\s*$")


class ExampleChunk(BaseModel):
    """One fenced ``jsx`` block extracted from a ``*.examples.md`` file.

    Args:
        component_name (str): the parent component's name (the stem of
            the ``*.examples.md`` file). Used as the "owner" anchor in
            embeddings and graph edges.
        example_id (str | None): the ``// @example-id <slug>`` marker
            from the first body line, if present.
        title (str): the nearest preceding non-empty, non-fence line,
            or the most recent ``##``-level heading as fallback.
        code (str): the raw fence body, with the closing fence stripped
            and surrounding blank lines trimmed.
        language_tag (str): the full fence tag string after the opening
            triple backticks (e.g. ``"jsx"``, ``"jsx harmony noeditor"``).
            Kept verbatim so callers can recover whatever subtlety we
            didn't model explicitly.
        imports (list[str]): identifiers imported from
            ``@nutanix-ui/prism-reactjs``. Empty when no such import
            appears. Other packages are ignored deliberately.
        is_noeditor (bool): ``True`` when the fence has the
            ``noeditor`` tag. Such blocks are not user-runnable.
        is_a11y_block (bool): ``True`` when ``is_noeditor`` is set AND
            the enclosing ``##`` section name contains
            ``"Accessibility"``. Slice 11's a11y aggregator keys on
            this flag.
        is_anti_pattern (bool): ``True`` when the enclosing section
            heading is one of ``"Don't"`` / ``"Do not"`` /
            ``"Anti-pattern"``. The embedding index drops these so
            the LLM never gets shown a sample it should avoid.
    """

    model_config = ConfigDict(extra="forbid")

    component_name: str
    example_id: str | None = None
    title: str
    code: str
    language_tag: str
    imports: list[str]
    is_noeditor: bool = False
    is_a11y_block: bool = False
    is_anti_pattern: bool = False


def parse_example_code_blocks(
    markdown: str,
    component_name: str,
) -> list[ExampleChunk]:
    """Walk ``markdown`` and emit one :class:`ExampleChunk` per ``jsx`` fence.

    Args:
        markdown (str): raw contents of an ``X.examples.md`` file.
        component_name (str): name of the parent component (file stem)
            used to stamp each chunk's ``component_name``.

    Returns:
        list[ExampleChunk]: chunks in document order. Non-jsx fences
        (bash, text, etc.) are skipped entirely; jsx and jsx-harmony
        fences are returned even when flagged (noeditor /
        anti-pattern). The caller decides what to filter.
    """
    lines = markdown.splitlines()
    chunks: list[ExampleChunk] = []
    current_section = ""
    last_title_line = ""

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("#") and stripped.lstrip("#").strip():
            current_section = stripped.lstrip("#").strip()
            last_title_line = current_section
            i += 1
            continue

        if stripped.startswith(_FENCE_MARKER):
            language_tag = stripped[len(_FENCE_MARKER) :].strip()
            body, end = _consume_fence(lines, i)
            i = end
            if not _is_jsx_fence(language_tag):
                continue
            chunk = _build_chunk(
                component_name=component_name,
                title=last_title_line or current_section,
                section=current_section,
                body_lines=body,
                language_tag=language_tag,
            )
            chunks.append(chunk)
            last_title_line = ""
            continue

        if stripped:
            last_title_line = stripped
        i += 1

    return chunks


def walk_example_chunks(package_root: Path) -> list[ExampleChunk]:
    """Walk every ``*.examples.md`` under ``package_root`` and flatten chunks.

    Mirrors :func:`prism_mcp.parsers.components.walk_components`'s
    layout assumption: examples files live at
    ``src/components/v2/<Name>/<Name>.examples.md`` in the extracted
    tarball. Files outside that tree are not picked up because the
    upstream library's publish config only ships those.

    Args:
        package_root (Path): the extracted ``package/`` directory.

    Returns:
        list[ExampleChunk]: chunks from every examples file, in
        filesystem-iteration order, with each chunk's
        ``component_name`` set to the folder name (which equals the
        file stem by convention).
    """
    src_root = package_root / EXAMPLES_SRC_DIR
    if not src_root.is_dir():
        logger.warning(
            "no v2 component src dir at %s; skipping example-chunk pass",
            src_root,
        )
        return []
    chunks: list[ExampleChunk] = []
    for md_path in sorted(src_root.rglob("*.examples.md")):
        component_name = md_path.name.removesuffix(".examples.md")
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read examples %s: %s", md_path, exc)
            continue
        chunks.extend(
            parse_example_code_blocks(text, component_name=component_name)
        )
    return chunks


def _consume_fence(lines: list[str], start: int) -> tuple[list[str], int]:
    """Return ``(body_lines, index_after_closing_fence)``.

    Mirrors :func:`prism_mcp.parsers.examples_md._consume_fence` so
    fence handling stays bug-for-bug compatible between the two
    parsers. We don't share the helper to avoid a cross-parser import
    that would couple the modules.

    Args:
        lines (list[str]): full document lines.
        start (int): index of the opening fence line.

    Returns:
        tuple[list[str], int]: body lines (without the fences
        themselves) and the next index to scan. When a closing fence
        is missing we return everything to end-of-file so a malformed
        block doesn't silently swallow the rest of the doc.
    """
    body: list[str] = []
    i = start + 1
    n = len(lines)
    while i < n:
        if lines[i].strip().startswith(_FENCE_MARKER):
            return body, i + 1
        body.append(lines[i])
        i += 1
    return body, n


def _is_jsx_fence(language_tag: str) -> bool:
    """Return ``True`` when ``language_tag`` opens a jsx / jsx-harmony fence.

    The styleguide's tag conventions allow any whitespace-separated
    suffixes (``noeditor``, ``harmony``, ``static``...). We treat any
    fence whose *first* token is ``jsx`` as a jsx fence. Other
    languages are ignored entirely.

    Args:
        language_tag (str): the substring after the opening triple
            backticks, leading/trailing whitespace already stripped.

    Returns:
        bool: ``True`` for ``"jsx"``, ``"jsx harmony"``,
        ``"jsx harmony noeditor"``, etc.
    """
    if not language_tag:
        return False
    first_token = language_tag.split()[0].lower()
    return first_token in _JSX_LANGUAGE_TAGS


def _build_chunk(
    *,
    component_name: str,
    title: str,
    section: str,
    body_lines: list[str],
    language_tag: str,
) -> ExampleChunk:
    """Assemble one :class:`ExampleChunk` from its parsed parts."""
    code = "\n".join(body_lines).strip("\n")
    example_id = _extract_example_id(body_lines)
    imports = _extract_prism_imports(code)
    is_noeditor = _NO_EDITOR_TAG in language_tag.lower()
    is_a11y_block = is_noeditor and _A11Y_HEADING_NEEDLE in section
    is_anti_pattern = any(needle in section for needle in _DONT_HEADINGS)
    return ExampleChunk(
        component_name=component_name,
        example_id=example_id,
        title=title,
        code=code,
        language_tag=language_tag,
        imports=imports,
        is_noeditor=is_noeditor,
        is_a11y_block=is_a11y_block,
        is_anti_pattern=is_anti_pattern,
    )


def _extract_example_id(body_lines: list[str]) -> str | None:
    """Return the ``@example-id`` slug from the first matching body line.

    We scan from the top until we hit either a match or the first
    non-comment / non-whitespace line. That mirrors the styleguide's
    convention (the marker, if present, is always at the very top of
    the block).

    Args:
        body_lines (list[str]): fence body lines.

    Returns:
        str | None: the slug, or ``None`` when no marker is present.
    """
    for raw in body_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        match = _EXAMPLE_ID_RE.match(raw)
        if match:
            return match.group("id")
        if not stripped.startswith("//"):
            return None
    return None


def _extract_prism_imports(code: str) -> list[str]:
    """Return identifiers imported from ``@nutanix-ui/prism-reactjs``.

    The regex matches *every* prism import in the body so a block with
    two separate import statements (rare, but legal) doesn't lose half
    its identifiers. Names are deduplicated and returned in source
    order so deterministic test assertions are possible.

    ``import { Foo as Bar } from '...';`` is normalised to ``Foo``
    because the *original* identifier is what indexes against the
    component palette.

    Args:
        code (str): the fence body.

    Returns:
        list[str]: import names, deduplicated, in source order. Empty
        when no prism imports appear in the body.
    """
    seen: dict[str, None] = {}
    for match in _PRISM_IMPORT_RE.finditer(code):
        names_block = match.group("names")
        for raw in names_block.split(","):
            cleaned = raw.strip()
            if not cleaned:
                continue
            # Strip ``as Alias`` aliases — we want the original symbol.
            head = cleaned.split(" as ", 1)[0].strip()
            if head and head not in seen:
                seen[head] = None
    return list(seen.keys())
