"""LESS extractor + walker for design tokens.

PRD section 4 / Slice 6 says: emit ``token`` entities with ``value``,
``category``, ``source_file`` from ``src/styles/v2/*.less``. PRD section
5 fixes the category vocabulary to
``color | spacing | typography | z-index | animation | focus``.

The extractor is a deliberate **regex over LESS** (PRD section 6 calls
this out as the acceptable shape for tokens). LESS variable declarations
are flat enough — ``@name: value;`` — that a regex with comment stripping
is more legible than a full LESS parser and avoids a heavy dep.

Category is inferred from the file the token lives in: the upstream
library organizes tokens by file (``Colors.less``, ``Z-Index.less``,
etc.). Unknown filenames fall back to ``spacing`` because the
catch-all ``Variables.less`` is the spacing/sizing dumping ground in
practice.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from prism_mcp.entities import Entity

logger = logging.getLogger(__name__)

STYLES_V2_DIR = "src/styles/v2"

# Token: ``@name: value;`` at column zero (after optional whitespace).
# We require the trailing ``;`` so we don't accidentally pick up
# multi-line @import directives or mixin definitions that contain a
# ``:`` on the first line.
_TOKEN_RE = re.compile(
    r"^[ \t]*@(?P<name>[\w-]+)\s*:\s*(?P<value>[^;\n]+?);",
    re.MULTILINE,
)

# Two comment shapes appear in LESS:
# - ``//`` to end of line (also used for our copyright headers).
# - ``/* ... */`` blocks (rare in the upstream tokens, but seen).
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# Filename -> token category. Files not in this table fall back to
# ``spacing`` (the most common variable kind). Names match the upstream
# layout under ``src/styles/v2/``.
_CATEGORY_BY_FILENAME: dict[str, str] = {
    "Colors.less": "color",
    "ChartCategorialColors.less": "color",
    "ChartLinearColors.less": "color",
    "Typography.less": "typography",
    "Z-Index.less": "z-index",
    "Animation.less": "animation",
    "Focus.less": "focus",
    # Variables.less is the catch-all spacing/sizing file in the
    # upstream library; we map it explicitly so the fallback below
    # only triggers for genuinely unknown files.
    "Variables.less": "spacing",
}
DEFAULT_TOKEN_CATEGORY = "spacing"


def walk_tokens(package_root: Path, version: str) -> list[Entity]:
    """Return one ``token`` :class:`Entity` per LESS variable declaration.

    Args:
        package_root (Path): extracted ``package/`` directory.
        version (str): tarball version label.

    Returns:
        list[Entity]: tokens sorted by name. The same variable name
        appearing in two files (rare) generates two entities with
        different ``source_file`` values; the indexer deduplicates by
        ``(type, name)`` and logs a warning if collisions occur.
    """
    styles_dir = package_root / STYLES_V2_DIR
    if not styles_dir.is_dir():
        logger.warning(
            "no styles v2 dir at %s; skipping token pass", styles_dir
        )
        return []

    entities: list[Entity] = []
    for less_path in sorted(styles_dir.glob("*.less")):
        category = _CATEGORY_BY_FILENAME.get(
            less_path.name, DEFAULT_TOKEN_CATEGORY
        )
        try:
            text = less_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read less %s: %s", less_path, exc)
            continue
        relative_source = str(less_path.relative_to(package_root))
        entities.extend(
            _extract_tokens(
                text=text,
                category=category,
                version=version,
                source_file=relative_source,
            )
        )
    entities.sort(key=lambda e: e.name)
    return entities


def _extract_tokens(
    *,
    text: str,
    category: str,
    version: str,
    source_file: str,
) -> list[Entity]:
    """Pull ``@name: value;`` declarations out of one LESS file's text.

    Args:
        text (str): raw LESS source.
        category (str): token category to stamp on every emitted entity.
        version (str): tarball version label.
        source_file (str): in-tarball path used for traceability.

    Returns:
        list[Entity]: tokens in declaration order.
    """
    stripped = _strip_less_comments(text)
    return [
        Entity(
            name=match.group("name"),
            type="token",
            version=version,
            category=category,
            value=match.group("value").strip(),
            source_file=source_file,
            summary=_summary_for_token(
                name=match.group("name"),
                value=match.group("value").strip(),
                category=category,
            ),
            import_path="",
        )
        for match in _TOKEN_RE.finditer(stripped)
    ]


def _strip_less_comments(text: str) -> str:
    """Remove ``//`` line comments and ``/* */`` blocks.

    Strings inside LESS values can never contain ``//`` legally outside a
    comment, so the simple regex replace is safe here even though it
    would be naïve for, say, JavaScript.

    Args:
        text (str): raw LESS.

    Returns:
        str: source with comments replaced by blank space (preserving
        line structure so regex line anchors keep working).
    """
    no_blocks = _BLOCK_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)
    return _LINE_COMMENT_RE.sub("", no_blocks)


def _summary_for_token(name: str, value: str, category: str) -> str:
    """Build a short human-readable summary string for a token.

    A token's "summary" doubles as part of the BM25 synthetic doc, so a
    bit of redundancy helps Cursor's prose queries land. Color hex
    values get the ``"color"`` word; spacing/z-index/animation gets the
    category written out.

    Args:
        name (str): variable name (without the leading ``@``).
        value (str): the raw LESS value.
        category (str): token category.

    Returns:
        str: short prose like ``"color token #1B6BCC"``.
    """
    return f"{category} token {value}"
