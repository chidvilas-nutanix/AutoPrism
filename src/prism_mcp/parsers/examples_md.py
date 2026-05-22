"""Markdown-section splitter for ``X.examples.md`` files.

The Prism examples format alternates between titles (plain text on
their own line, often "Basic Example", "With Disabled", etc.) and
fenced code blocks. We extract each fenced block plus the line of text
immediately preceding it as the example's title.

Heading detection is intentionally fuzzy: real ``.examples.md`` files
use mixed conventions — some have explicit headings (``## Foo``), some
just bare lines, and some prefix code fences with a JSDoc-style
``// @example-id default``. We treat the first non-empty, non-fence
line found in the 10 lines preceding a code fence as the title.

We also skip fences tagged with ``noeditor`` because the styleguide
uses those for "Accessibility Guidelines" sidebars rather than runnable
examples.
"""

from __future__ import annotations

from prism_mcp.entities import Example

_FENCE_MARKER = "```"
_NO_EDITOR_TAG = "noeditor"
_DONT_HEADINGS = ("Don't", "Do not", "Anti-pattern")


def parse_examples(markdown: str) -> list[Example]:
    """Extract every fenced example from ``markdown``.

    Args:
        markdown (str): raw contents of an ``X.examples.md`` file.

    Returns:
        list[Example]: examples in source order. The list may be empty
        when the file consists only of accessibility / noeditor blocks.
    """
    lines = markdown.splitlines()
    examples: list[Example] = []
    current_section: str = ""
    last_title_line: str = ""

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
            language_tag = stripped[len(_FENCE_MARKER) :]
            block_lines, end = _consume_fence(lines, i)
            i = end

            if _NO_EDITOR_TAG in language_tag:
                continue

            title = last_title_line or current_section
            example = Example(
                title=title,
                code="\n".join(block_lines).strip("\n"),
                kind=_classify_kind(current_section),
            )
            examples.append(example)
            last_title_line = ""
            continue

        if stripped:
            last_title_line = stripped
        i += 1

    return examples


def parse_summary(markdown: str) -> str:
    """Return the first paragraph of ``markdown`` for use as ``summary``.

    The summary is everything before the first fenced code block or the
    first ``##``-style heading, whichever comes first. Whitespace is
    collapsed and the result is truncated so it fits comfortably in
    LLM context.

    Args:
        markdown (str): raw markdown.

    Returns:
        str: cleaned summary; empty string when none could be found.
    """
    lines = markdown.splitlines()
    paragraph: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.startswith(_FENCE_MARKER):
            break
        if line.startswith("##"):
            break
        paragraph.append(line.lstrip("# ").strip())
    text = " ".join(paragraph).strip()
    return text[:280]


def _consume_fence(lines: list[str], start: int) -> tuple[list[str], int]:
    """Return the body lines and the index *after* the closing fence.

    Args:
        lines (list[str]): full document lines.
        start (int): index of the opening fence line.

    Returns:
        tuple[list[str], int]: body lines and the next index to scan.
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


def _classify_kind(section_heading: str) -> str:
    """Pick an :class:`Example` ``kind`` from the enclosing section.

    Slice 3 only emits ``"usage"`` and ``"anti-pattern"``. We promote
    examples under a "Don't" / "Do not" / "Anti-pattern" heading to
    anti-patterns so the LLM knows to avoid them.

    Args:
        section_heading (str): the current ``##``-level heading or
            empty string if none.

    Returns:
        str: one of the :data:`Example` kinds.
    """
    if any(needle in section_heading for needle in _DONT_HEADINGS):
        return "anti-pattern"
    return "usage"
