"""Filesystem walker for utility modules.

Utils are the most heterogeneous of the parser passes:

* Some files (``A11yUtils.ts``) export many short arrow functions.
* Some (``ColorUtils.ts``) export a static-method class.
* Some (``Constants.ts``) export typed constants.

For Slice 5 we emit one :class:`Entity` per *callable* — functions and
classes. Constants are skipped because the LLM never "calls" them; they
land in design-token coverage (Slice 6) when they're tokens, or in the
``Entity.signature`` of the function that uses them otherwise.

The util tree is walked recursively because the upstream library has a
``lib/utils/v2/`` subdirectory that ships additional helpers alongside
the flat ``lib/utils/`` tree.
"""

from __future__ import annotations

import logging
from pathlib import Path

from prism_mcp.entities import Entity
from prism_mcp.parsers.components import PRISM_PACKAGE_NAME
from prism_mcp.parsers.dts import (
    ParsedClass,
    ParsedFunction,
    parse_classes,
    parse_functions,
)

logger = logging.getLogger(__name__)

LIB_UTILS_DIR = "lib/utils"


def walk_utils(
    package_root: Path,
    version: str,
    package_name: str = PRISM_PACKAGE_NAME,
) -> list[Entity]:
    """Return one :class:`Entity` per exported util callable.

    Args:
        package_root (Path): the extracted ``package/`` directory.
        version (str): tarball version string.
        package_name (str): scoped npm name used to compose
            ``import_path``.

    Returns:
        list[Entity]: util entities sorted by name.
    """
    utils_dir = package_root / LIB_UTILS_DIR
    if not utils_dir.is_dir():
        logger.warning("no utils dir at %s; skipping util pass", utils_dir)
        return []

    entities: list[Entity] = []
    for dts_path in sorted(utils_dir.rglob("*.d.ts")):
        try:
            text = dts_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read util d.ts %s: %s", dts_path, exc)
            continue
        for func in parse_functions(text):
            entities.append(_entity_from_function(func, version, package_name))
        for cls in parse_classes(text):
            entities.append(_entity_from_class(cls, version, package_name))
    entities.sort(key=lambda e: e.name)
    return entities


def _entity_from_function(
    func: ParsedFunction, version: str, package_name: str
) -> Entity:
    """Compose a util :class:`Entity` from a parsed function."""
    return Entity(
        name=func.name,
        type="util",
        version=version,
        summary=func.description or "",
        import_path=f"import {{ {func.name} }} from '{package_name}';",
        signature=list(func.params),
        examples=[],
        deprecated=func.deprecated,
    )


def _entity_from_class(
    cls: ParsedClass, version: str, package_name: str
) -> Entity:
    """Compose a util :class:`Entity` from a parsed class."""
    return Entity(
        name=cls.name,
        type="util",
        version=version,
        summary=cls.description or "",
        import_path=f"import {{ {cls.name} }} from '{package_name}';",
        signature=list(cls.methods),
        examples=[],
        deprecated=cls.deprecated,
    )
