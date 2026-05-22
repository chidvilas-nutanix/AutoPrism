"""Filesystem walker that turns an extracted tarball into component Entities.

Layout assumptions match the actual ``@nutanix-ui/prism-reactjs``
publish (PRD section 5 + verified against
``prism-ui-prism-reactjs-lib-master/services``):

* d.ts files live under ``lib/components/v2/<Name>/*.d.ts``.
* examples live under ``src/components/v2/<Name>/<Name>.examples.md``.
* Deprecated components live under ``src/components/deprecated/`` and
  are out of scope for v1 (PRD section 4).

A single component folder can ship several *sub*-components in
neighboring files (e.g. ``Button`` + ``ButtonGroup`` +
``TextButtonGroup`` under ``Button/``). PRD Slice 3 calls for
"per-subcomponent granularity", so we treat every ``*.d.ts`` in the
folder as its own entity, deriving the name from the file name and the
``export interface <Name>Props`` declaration found inside.
"""

from __future__ import annotations

import logging
from pathlib import Path

from prism_mcp.entities import Entity
from prism_mcp.parsers.dts import parse_interfaces
from prism_mcp.parsers.examples_md import parse_examples, parse_summary

logger = logging.getLogger(__name__)

V2_COMPONENT_LIB_GLOB = "lib/components/v2"
V2_COMPONENT_SRC_GLOB = "src/components/v2"
PRISM_PACKAGE_NAME = "@nutanix-ui/prism-reactjs"


def walk_components(
    package_root: Path,
    version: str,
    package_name: str = PRISM_PACKAGE_NAME,
) -> list[Entity]:
    """Walk ``package_root`` and emit one :class:`Entity` per component.

    Args:
        package_root (Path): the extracted ``package/`` directory, i.e.
            ``Cache.package_dir(version)``.
        version (str): the published version tag — stamped on each
            Entity so consumers can detect cache staleness.
        package_name (str): scoped npm name; used to compose the
            canonical ``import_path``.

    Returns:
        list[Entity]: components found under ``lib/components/v2/``.
        Entities for sub-components share their parent folder's
        examples file because tsc emits separate d.ts files but the
        repo ships one ``X.examples.md`` per logical component.
    """
    lib_root = package_root / V2_COMPONENT_LIB_GLOB
    src_root = package_root / V2_COMPONENT_SRC_GLOB
    if not lib_root.is_dir():
        logger.warning(
            "no v2 component lib dir at %s; skipping component pass",
            lib_root,
        )
        return []

    entities: list[Entity] = []
    for folder in sorted(p for p in lib_root.iterdir() if p.is_dir()):
        folder_examples_md = _find_examples_md(src_root, folder.name)
        entities.extend(
            _entities_from_folder(
                folder=folder,
                examples_md_path=folder_examples_md,
                version=version,
                package_name=package_name,
            )
        )
    return entities


def _entities_from_folder(
    *,
    folder: Path,
    examples_md_path: Path | None,
    version: str,
    package_name: str,
) -> list[Entity]:
    """Return one Entity per ``.d.ts`` file in ``folder``."""
    entities: list[Entity] = []
    for dts_path in sorted(folder.glob("*.d.ts")):
        if dts_path.name.endswith(".spec.d.ts"):
            continue
        try:
            text = dts_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read d.ts %s: %s", dts_path, exc)
            continue
        # ``Path.stem`` only strips the last suffix, leaving ``Button.d``
        # for ``Button.d.ts``. We want ``Button``.
        component_name = dts_path.name.removesuffix(".d.ts")
        interfaces = parse_interfaces(text)
        primary = _select_primary_interface(interfaces, component_name)
        if primary is None:
            continue
        entity = _entity_from_interface(
            interface_name=component_name,
            members=primary.members,
            deprecated=primary.deprecated,
            examples_md_path=examples_md_path,
            version=version,
            package_name=package_name,
        )
        entities.append(entity)
    return entities


def _select_primary_interface(
    interfaces: list,
    file_stem: str,
):
    """Pick the ``Props`` interface that matches the file name.

    A typical d.ts file (``Button.d.ts``) exports several interfaces;
    the props interface follows the convention ``<Stem>Props``. If we
    don't find that exact match we fall back to any ``*Props``
    interface, which is enough for sub-component files like
    ``ButtonGroup.d.ts`` whose props interface is also ``ButtonGroupProps``.

    Args:
        interfaces (list): output of :func:`parse_interfaces`.
        file_stem (str): the d.ts file name without its extension.

    Returns:
        ParsedInterface | None: the chosen interface, or ``None`` when
        no plausible props interface was found.
    """
    if not interfaces:
        return None
    expected = f"{file_stem}Props"
    for interface in interfaces:
        if interface.name == expected:
            return interface
    for interface in interfaces:
        if interface.name.endswith("Props"):
            return interface
    return None


def _entity_from_interface(
    *,
    interface_name: str,
    members,
    deprecated: bool,
    examples_md_path: Path | None,
    version: str,
    package_name: str,
) -> Entity:
    """Compose an :class:`Entity` from a parsed interface + examples."""
    summary = ""
    examples = []
    if examples_md_path is not None and examples_md_path.is_file():
        try:
            text = examples_md_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "failed to read examples %s: %s", examples_md_path, exc
            )
        else:
            summary = parse_summary(text)
            examples = parse_examples(text)

    return Entity(
        name=interface_name,
        type="component",
        version=version,
        summary=summary,
        import_path=_canonical_import(interface_name, package_name),
        signature=list(members),
        examples=list(examples),
        deprecated=deprecated,
    )


def _find_examples_md(src_root: Path, folder_name: str) -> Path | None:
    """Locate ``<folder>/<folder>.examples.md`` under ``src/`` if present.

    Args:
        src_root (Path): ``src/components/v2`` inside the tarball.
        folder_name (str): name of the lib subdirectory currently
            being walked.

    Returns:
        Path | None: matching examples file, or ``None`` when neither
        the folder nor the file is present (some entries ship with no
        examples).
    """
    candidate = src_root / folder_name / f"{folder_name}.examples.md"
    if candidate.is_file():
        return candidate
    return None


def _canonical_import(name: str, package_name: str) -> str:
    """Compose the canonical ``import`` line for ``name``.

    All Prism components are exported from the package root, so:

        import { Button } from '@nutanix-ui/prism-reactjs';

    is the canonical form. We hand the LLM the entire statement (not
    just the package) so it can paste verbatim.

    Args:
        name (str): component identifier (e.g. ``"Button"``).
        package_name (str): the scoped npm package.

    Returns:
        str: a ready-to-paste import statement.
    """
    return f"import {{ {name} }} from '{package_name}';"
