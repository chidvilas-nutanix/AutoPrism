"""Filesystem walker for React hooks.

Per PRD section 4 and Slice 5, hook d.ts files live at
``lib/hooks/*.d.ts`` in the published tarball. Each file usually
exports exactly one identifier whose name starts with ``use``
(``useFocusTrap``, ``useResizeObserver``, ...). We tolerate multiple
exports per file — only the ones that look like hooks (name starts
with ``use``) become :class:`Entity` rows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from prism_mcp.entities import Entity
from prism_mcp.parsers.components import PRISM_PACKAGE_NAME
from prism_mcp.parsers.dts import ParsedFunction, parse_functions

logger = logging.getLogger(__name__)

LIB_HOOKS_DIR = "lib/hooks"
HOOK_NAME_PREFIX = "use"


def walk_hooks(
    package_root: Path,
    version: str,
    package_name: str = PRISM_PACKAGE_NAME,
) -> list[Entity]:
    """Return one :class:`Entity` per exported hook function.

    Args:
        package_root (Path): the extracted ``package/`` directory.
        version (str): tarball version string.
        package_name (str): scoped npm name used to compose
            ``import_path``.

    Returns:
        list[Entity]: hook entities sorted by name.
    """
    hooks_dir = package_root / LIB_HOOKS_DIR
    if not hooks_dir.is_dir():
        logger.warning("no hooks dir at %s; skipping hook pass", hooks_dir)
        return []

    entities: list[Entity] = []
    for dts_path in sorted(hooks_dir.glob("*.d.ts")):
        try:
            text = dts_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read hook d.ts %s: %s", dts_path, exc)
            continue
        for func in parse_functions(text):
            if not func.name.startswith(HOOK_NAME_PREFIX):
                continue
            entities.append(_entity_from_function(func, version, package_name))
    entities.sort(key=lambda e: e.name)
    return entities


def _entity_from_function(
    func: ParsedFunction, version: str, package_name: str
) -> Entity:
    """Compose a hook :class:`Entity` from one parsed function."""
    return Entity(
        name=func.name,
        type="hook",
        version=version,
        summary=func.description or "",
        import_path=f"import {{ {func.name} }} from '{package_name}';",
        signature=list(func.params),
        examples=[],
        deprecated=func.deprecated,
    )
