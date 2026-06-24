"""Prism component prop schemas (roadmap P3 Part B, the props layer).

P3 Part A (`improvements/04-phase3-routing-and-props.md`) routes a Figma
region to a Prism component *family*. Part B turns the region's Figma
``componentProperties`` into **exact typed props** — and that needs a
machine-readable schema of every component's props: which are enums
(with their ``MEMBER -> value`` map, so we can emit
``type={ButtonTypes.PRIMARY}``), which are string-literal unions
(``appearance="square"``), which are booleans, etc.

Two halves, mirroring `catalog.py`:

* **Build-time** (offline, `scripts/build_prop_schema.py`): walk the
  cached rplib ``lib/components/v2/<Family>/*.d.ts``, parse each
  ``<Stem>Props`` interface (:func:`prism_mcp.parsers.dts.parse_interfaces`)
  and every ``enum`` (:func:`~prism_mcp.parsers.dts.parse_enums`),
  classify each prop's type against the family's enum pool, and
  serialize a versioned artifact (``data/prism_prop_schema.json``).
* **Run-time** (no network, no rplib): :class:`PropSchemaIndex` loads
  that committed JSON once per process and answers ``component`` /
  ``for_family`` in O(1). The P3 resolver (`figma/props.py`) consumes it.

The prop *names / required / default / JSDoc* already come for free from
the existing `.d.ts` parser; this module adds the **enum value maps**
and **union literal sets** that codegen needs and that the entity index
does not carry.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from prism_mcp.entities import Member
from prism_mcp.parsers.dts import parse_enums, parse_interfaces

logger = logging.getLogger(__name__)

PROP_SCHEMA_VERSION = 1
"""Bump when the artifact shape changes; the loader hard-fails on drift."""

DATA_PATH = Path(__file__).parent / "data" / "prism_prop_schema.json"
"""Committed artifact location (sibling of ``figma_catalog.json``)."""

PropKind = Literal[
    "enum", "union", "boolean", "number", "string", "node", "other"
]
"""Coarse classification of a prop's TS type, driving how a Figma
variant value is turned into a JSX value:

* ``enum``    — references a parsed enum; emit ``Enum.MEMBER``.
* ``union``   — string-literal union; emit the quoted literal.
* ``boolean`` — emit ``true`` / ``false`` (or a bare attribute).
* ``number`` / ``string`` / ``node`` — pass-through kinds.
* ``other``   — imported alias / complex type we do not model.
"""

# v2 family dirs whose "main" component is not the dir name. Most
# families (Button, Badge, Input, Select, …) export a same-named
# component; the handful that don't get a pin here so a catalog family
# (which is the *directory* name) resolves to the right Props interface.
_FAMILY_MAIN: dict[str, str] = {
    "Tables": "Table",
    "Layouts": "FlexLayout",
    "Icons": "Icon",
    "Typography": "Paragraph",
    "Notifications": "Notification",
    "Containers": "Container",
}

_NODE_TOKENS = frozenset(
    {"ReactNode", "ReactElement", "ReactChild", "Element", "JSX"}
)
_LITERAL_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_SEP_STRIP_RE = re.compile(r"[^a-z0-9]+")


class PropSchema(BaseModel):
    """One component prop, classified for codegen.

    Args:
        name (str): the Prism prop name (e.g. ``"type"``).
        kind (PropKind): coarse type classification.
        required (bool): no ``?`` on the declaration.
        default (str | None): ``@default`` JSDoc value, if any.
        enum_name (str | None): referenced enum identifier
            (``"ButtonTypes"``) when ``kind == "enum"``.
        enum_members (dict[str, str]): ``MEMBER -> value`` for the enum,
            so the resolver can map a Figma value back to ``Enum.MEMBER``.
        values (list[str]): allowed string values — the enum's values or
            the union's literals. Empty for boolean/number/node/other.
        accepts_string (bool): the type is ``Enum | string`` (or the
            union includes ``string``); a non-matching Figma value can
            be emitted as a raw string.
        description (str): JSDoc prose (trimmed).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: PropKind
    required: bool = False
    default: str | None = None
    enum_name: str | None = None
    enum_members: dict[str, str] = {}
    values: list[str] = []
    accepts_string: bool = False
    description: str = ""


class ComponentPropSchema(BaseModel):
    """Every prop of one component, keyed by prop name.

    Args:
        component (str): component identifier (e.g. ``"Button"``).
        family (str): owning v2 directory (e.g. ``"Button"``); this is
            what the P2 catalog resolves a region to.
        props (dict[str, PropSchema]): prop name -> schema.
    """

    model_config = ConfigDict(extra="forbid")

    component: str
    family: str
    props: dict[str, PropSchema] = {}


def _tokens(type_str: str) -> list[str]:
    return _IDENT_RE.findall(type_str)


def _literals(type_str: str) -> list[str]:
    return [a or b for a, b in _LITERAL_RE.findall(type_str)]


def classify_prop(
    member: Member, enums: dict[str, dict[str, str]]
) -> PropSchema:
    """Classify one parsed ``Member`` (a prop) against the enum pool.

    Args:
        member (Member): a ``kind="prop"`` member from
            :func:`~prism_mcp.parsers.dts.parse_interfaces`.
        enums (dict[str, dict[str, str]]): the family's
            ``EnumName -> {MEMBER: value}`` pool.

    Returns:
        PropSchema: the classified prop. Precedence: enum reference
        (even within an ``Enum | string`` union) > string-literal union
        > boolean > number > React node > plain string > other.
    """
    type_str = member.type or ""
    toks = _tokens(type_str)
    lits = _literals(type_str)
    accepts_string = "string" in toks
    base = {
        "name": member.name,
        "required": member.required,
        "default": member.default,
        "description": member.description or "",
        "accepts_string": accepts_string,
    }

    enum_ref = next((t for t in toks if t in enums), None)
    if enum_ref is not None:
        members = enums[enum_ref]
        return PropSchema(
            kind="enum",
            enum_name=enum_ref,
            enum_members=members,
            values=list(dict.fromkeys(members.values())),
            **base,
        )
    if lits:
        return PropSchema(
            kind="union", values=list(dict.fromkeys(lits)), **base
        )
    if "boolean" in toks:
        return PropSchema(kind="boolean", **base)
    if "number" in toks:
        return PropSchema(kind="number", **base)
    if any(t in _NODE_TOKENS for t in toks):
        return PropSchema(kind="node", **base)
    if "string" in toks:
        return PropSchema(kind="string", **base)
    return PropSchema(kind="other", **base)


def _select_primary_interface(interfaces: list, file_stem: str):
    """Pick the ``<Stem>Props`` interface (mirrors components.py)."""
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


def build_family_schemas(
    family: str, dts_paths: list[Path]
) -> list[ComponentPropSchema]:
    """Build :class:`ComponentPropSchema`\\s for one v2 family directory.

    Enums are pooled across *all* of the family's ``.d.ts`` files first
    (so a prop in ``ButtonGroup.d.ts`` can reference an enum defined in a
    sibling ``buttonGroupTypes.d.ts``), then each file's primary
    ``<Stem>Props`` interface is classified against that pool.

    Args:
        family (str): the directory name (e.g. ``"Button"``).
        dts_paths (list[Path]): the ``*.d.ts`` files in the directory.

    Returns:
        list[ComponentPropSchema]: one per file that has a ``*Props``
        interface, in file order.
    """
    enum_pool: dict[str, dict[str, str]] = {}
    sources: dict[Path, str] = {}
    for path in dts_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read d.ts %s: %s", path, exc)
            continue
        sources[path] = text
        for parsed in parse_enums(text):
            # First definition wins; duplicates across files are rare and
            # identical in practice.
            enum_pool.setdefault(parsed.name, parsed.members)

    schemas: list[ComponentPropSchema] = []
    for path, text in sources.items():
        stem = path.name.removesuffix(".d.ts")
        interface = _select_primary_interface(parse_interfaces(text), stem)
        if interface is None:
            continue
        props: dict[str, PropSchema] = {}
        for member in interface.members:
            if member.kind != "prop":
                continue
            props[member.name] = classify_prop(member, enum_pool)
        schemas.append(
            ComponentPropSchema(component=stem, family=family, props=props)
        )
    return schemas


def _family_main(family: str, components: list[str]) -> str:
    """Pick the representative component for a family directory.

    Order: explicit pin (:data:`_FAMILY_MAIN`) > exact dir-name match >
    dir name minus a trailing ``s`` > shortest component name > first.
    """
    if family in _FAMILY_MAIN and _FAMILY_MAIN[family] in components:
        return _FAMILY_MAIN[family]
    if family in components:
        return family
    singular = family.rstrip("s")
    if singular in components:
        return singular
    return min(components, key=len) if components else family


def build_prop_schema(
    families: dict[str, list[Path]], *, rplib_version: str = ""
) -> dict[str, Any]:
    """Build the full serializable prop-schema artifact.

    Args:
        families (dict[str, list[Path]]): ``family -> [*.d.ts paths]``.
        rplib_version (str): provenance stamp.

    Returns:
        dict[str, Any]: ``{schema_version, rplib_version, components,
        families}`` where ``components`` is keyed by component name and
        ``families`` maps each dir to ``{main, components}``.
    """
    components: dict[str, dict[str, Any]] = {}
    family_index: dict[str, dict[str, Any]] = {}
    for family, paths in sorted(families.items()):
        names: list[str] = []
        for schema in build_family_schemas(family, sorted(paths)):
            components[schema.component] = schema.model_dump(
                exclude_defaults=True
            )
            names.append(schema.component)
        if names:
            family_index[family] = {
                "main": _family_main(family, names),
                "components": sorted(names),
            }
    return {
        "schema_version": PROP_SCHEMA_VERSION,
        "rplib_version": rplib_version,
        "components": dict(sorted(components.items())),
        "families": family_index,
    }


class PropSchemaIndex:
    """In-process, read-only view over the committed prop-schema artifact."""

    def __init__(
        self,
        components: dict[str, ComponentPropSchema],
        families: dict[str, dict[str, Any]],
        meta: dict[str, Any],
    ) -> None:
        self._components = components
        self._families = families
        self._meta = meta

    @classmethod
    def from_artifact(cls, artifact: dict[str, Any]) -> PropSchemaIndex:
        schema = artifact.get("schema_version")
        if schema != PROP_SCHEMA_VERSION:
            raise ValueError(
                f"prop-schema schema_version {schema!r} != expected "
                f"{PROP_SCHEMA_VERSION}; rebuild data/prism_prop_schema.json"
            )
        components = {
            name: ComponentPropSchema.model_validate(payload)
            for name, payload in artifact.get("components", {}).items()
        }
        families = artifact.get("families", {})
        meta = {
            k: v
            for k, v in artifact.items()
            if k not in ("components", "families")
        }
        return cls(components, families, meta)

    @classmethod
    def load(cls, path: Path | None = None) -> PropSchemaIndex:
        target = path or DATA_PATH
        if not target.is_file():
            raise FileNotFoundError(
                f"prop schema not found at {target}; generate it with "
                f"`uv run python scripts/build_prop_schema.py`"
            )
        return cls.from_artifact(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def component(self, name: str | None) -> ComponentPropSchema | None:
        """Return the schema for ``name`` (a component), or ``None``."""
        if not name:
            return None
        return self._components.get(name)

    def for_family(self, family: str | None) -> ComponentPropSchema | None:
        """Return the *main* component's schema for a family directory.

        This is the entrypoint P3 routing uses — the P2 catalog resolves
        a region to a family (the directory name), and the resolver needs
        the representative component's props.
        """
        if not family:
            return None
        entry = self._families.get(family)
        if entry is not None:
            return self._components.get(entry.get("main", ""))
        # The catalog family may already be a component name (e.g. when
        # an override pins to a specific export).
        return self._components.get(family)

    def for_region(
        self, family: str | None, figma_name: str | None = None
    ) -> ComponentPropSchema | None:
        """Resolve the best component schema for a routed region.

        The P2 catalog routes to a *family* directory, but a Figma
        instance is usually a specific sub-component — and its name says
        which: ``"Table/Table Cell"`` -> ``TableCell``,
        ``"Input/Text Input"`` -> ``TextInput``. Picking the right
        sub-component is what makes that component's props resolvable at
        all (the generic ``Table`` schema has none of a cell's props).

        Strategy: among the family's components, choose the one whose
        normalized name is the *longest* substring of the normalized
        Figma name (most specific wins), falling back to the family main.

        Args:
            family (str): the routed family (catalog ``prism_component``).
            figma_name (str | None): the instance's logical Figma name.

        Returns:
            ComponentPropSchema | None: the most specific matching
            component schema, or the family main, or ``None``.
        """
        entry = self._families.get(family or "")
        if entry is None or not figma_name:
            return self.for_family(family)
        target = _SEP_STRIP_RE.sub("", figma_name.lower())
        best: str | None = None
        best_len = 0
        for name in entry.get("components", []):
            norm = _SEP_STRIP_RE.sub("", name.lower())
            if norm and norm in target and len(norm) > best_len:
                best, best_len = name, len(norm)
        if best is not None:
            return self._components.get(best)
        return self.for_family(family)

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self._meta)

    def __len__(self) -> int:
        return len(self._components)


@lru_cache(maxsize=1)
def get_prop_schema() -> PropSchemaIndex:
    """Return the process-wide prop-schema singleton (lazy, cached)."""
    return PropSchemaIndex.load()
