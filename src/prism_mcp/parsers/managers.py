"""Filesystem walker for manager singletons (``I18nManager``, ``ThemeManager``).

Per the published shape (verified against
``prism-ui-prism-reactjs-lib-master/services/src/managers/``) every
manager d.ts file declares a class and exports a default singleton:

.. code-block:: typescript

    declare class I18nManager { ... }
    declare const instance: I18nManager;
    export default instance;

We surface the class methods because that's the actual API the LLM
will call (``I18nManager.t(...)``). The ``Entity.name`` is the file
stem (which equals the class name in this library), and methods land
in ``signature`` with ``kind="method"``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from prism_mcp.entities import Entity
from prism_mcp.parsers.components import PRISM_PACKAGE_NAME
from prism_mcp.parsers.dts import ParsedClass, parse_classes

logger = logging.getLogger(__name__)

LIB_MANAGERS_DIR = "lib/managers"
MANAGER_SUFFIX = "Manager"


def walk_managers(
    package_root: Path,
    version: str,
    package_name: str = PRISM_PACKAGE_NAME,
) -> list[Entity]:
    """Return one :class:`Entity` per ``*Manager`` class.

    Args:
        package_root (Path): the extracted ``package/`` directory.
        version (str): tarball version string.
        package_name (str): scoped npm name used to compose
            ``import_path``.

    Returns:
        list[Entity]: manager entities sorted by name. Helper classes
        without a ``Manager`` suffix are skipped to avoid noise from
        internal types.
    """
    managers_dir = package_root / LIB_MANAGERS_DIR
    if not managers_dir.is_dir():
        logger.warning(
            "no managers dir at %s; skipping manager pass",
            managers_dir,
        )
        return []

    entities: list[Entity] = []
    for dts_path in sorted(managers_dir.glob("*.d.ts")):
        try:
            text = dts_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read manager d.ts %s: %s", dts_path, exc)
            continue
        for cls in parse_classes(text):
            if not cls.name.endswith(MANAGER_SUFFIX):
                continue
            entities.append(_entity_from_class(cls, version, package_name))
    entities.sort(key=lambda e: e.name)
    return entities


def _entity_from_class(
    cls: ParsedClass, version: str, package_name: str
) -> Entity:
    """Compose a manager :class:`Entity` from a parsed class."""
    return Entity(
        name=cls.name,
        type="manager",
        version=version,
        summary=cls.description or "",
        import_path=(f"import {cls.name} from '{package_name}';"),
        signature=list(cls.methods),
        examples=[],
        deprecated=cls.deprecated,
    )
