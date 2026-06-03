"""On-disk ``FigmaTreeMapping`` mocks used to short-circuit
``map_figma_tree`` for known (file_key, node_id) pairs.

The MCP tool checks this directory FIRST, before the REST fetch +
walker pipeline. When a curated mock exists it is returned
verbatim — perfect for demos and for keeping CI / Cursor flows
hermetic against rate limits on the Figma API. When the file is
missing the loader returns ``None`` and the tool falls through to
the live pipeline unchanged.

File name convention
--------------------

``<file_key>__<node_id_with_underscore>.json``

* ``file_key`` is the Figma file key (the path component right
  after ``/design/`` or ``/file/`` in the URL).
* ``node_id_with_underscore`` is the canonical colon-form id with
  the colon replaced by ``_`` so the file name is filesystem-safe
  on case-sensitive and case-insensitive systems alike.

Examples (canonical → file name):

* ``SzP22zLyApL9R5nsQYheeo`` + ``3800:49763`` →
  ``SzP22zLyApL9R5nsQYheeo__3800_49763.json``
* ``abc123`` + ``1:1`` → ``abc123__1_1.json``

Mocks directory resolution
--------------------------

In order of precedence:

1. ``PRISM_MCP_FIGMA_TREE_MOCKS_DIR`` env var (absolute path).
   The harness uses this to point at a hermetic test fixture
   directory.
2. ``<repo_root>/mocks/figma_tree`` where ``repo_root`` is two
   parents up from the ``prism_mcp`` package directory
   (``…/src/prism_mcp/__init__.py`` → ``…/src`` → ``…``). This
   is the conventional location for demo / hand-curated fixtures
   that live in source control.

Both forms are *opt-in*: the directory only has to exist when
you want the short-circuit to fire. A missing directory is a
silent miss (returns ``None``), not an error.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import prism_mcp
from prism_mcp.figma.fetch import ParsedFigmaUrl
from prism_mcp.figma.models import FigmaTreeMapping

logger = logging.getLogger(__name__)

_ENV_VAR = "PRISM_MCP_FIGMA_TREE_MOCKS_DIR"
_DEFAULT_SUBDIR = ("mocks", "figma_tree")


def _resolve_mocks_dir() -> Path:
    """Return the directory the loader scans for mock JSON files.

    Resolution order matches the module docstring.
    """
    env_override = os.environ.get(_ENV_VAR)
    if env_override:
        return Path(env_override).expanduser().resolve()
    package_dir = Path(prism_mcp.__file__).resolve().parent
    repo_root = package_dir.parent.parent
    return repo_root.joinpath(*_DEFAULT_SUBDIR)


def _safe_filename_for(file_key: str, node_id: str) -> str:
    """Build the canonical mock filename for a parsed Figma URL.

    Node ids are colon-form (``"3800:49763"``); we swap the colon
    for an underscore so the filename works on every filesystem.
    """
    safe_node = node_id.replace(":", "_")
    return f"{file_key}__{safe_node}.json"


def mock_path_for(parsed: ParsedFigmaUrl) -> Path:
    """Return the absolute path the mock would live at, regardless
    of whether the file actually exists. Useful for tooling that
    wants to print the expected location in an error message."""
    return _resolve_mocks_dir() / _safe_filename_for(
        parsed.file_key, parsed.node_id
    )


def try_load_mock(parsed: ParsedFigmaUrl) -> FigmaTreeMapping | None:
    """Try to load a curated ``FigmaTreeMapping`` for ``parsed``.

    Returns ``None`` (a clean miss) when:

    * the mocks directory does not exist,
    * the per-(file_key, node_id) JSON file is not present, or
    * the file is present but a JSON / Pydantic parse error blocked
      validation (logged at WARNING — the caller falls through to
      the live walker, never silently serving a corrupt mock).

    Returns a fully-validated :class:`FigmaTreeMapping` on hit.
    """
    path = mock_path_for(parsed)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        mapping = FigmaTreeMapping.model_validate(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "figma_tree mock present but unreadable at %s: %s; "
            "falling through to live walker",
            path,
            exc,
        )
        return None
    except Exception as exc:  # pydantic.ValidationError + friends
        logger.warning(
            "figma_tree mock at %s failed FigmaTreeMapping validation: %s; "
            "falling through to live walker",
            path,
            exc,
        )
        return None
    logger.info(
        "figma_tree mock hit file_key=%s node_id=%s path=%s "
        "agenda=%d layout_tree=%d tokens=%d dropped=%d",
        parsed.file_key,
        parsed.node_id,
        path,
        len(mapping.agenda),
        len(mapping.layout_tree),
        len(mapping.tokens),
        len(mapping.dropped),
    )
    return mapping
