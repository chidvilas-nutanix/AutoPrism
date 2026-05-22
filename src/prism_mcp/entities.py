"""Entity model for the Prism MCP server.

Implements PRD section 5's data model: a discriminated union over five
entity types (``component | hook | manager | util | token``) that share
a small set of fields. We use Pydantic v2 dataclass-style models so the
MCP layer gets free JSON schema generation for tool input/output.

Slice 3 only populates ``component`` entities. Hooks/managers/utils
land in Slice 5 and tokens in Slice 6; the model is in place now so the
later slices only add data, not types.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EntityType = Literal["component", "hook", "manager", "util", "token"]
ComponentEntityType = Literal["component"]

MemberKind = Literal["prop", "param", "return", "method"]
ExampleKind = Literal["usage", "composition", "anti-pattern"]


class Member(BaseModel):
    """One prop, parameter, return, or method on an entity's signature.

    PRD section 5: ``Member { name, kind, type, required, default,
    description }``. ``default`` and ``description`` are optional
    because not every member surfaces them; ``required`` is explicit so
    the LLM doesn't have to infer it from the absence of a default.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: MemberKind
    type: str
    required: bool = False
    default: str | None = None
    description: str | None = None


class Example(BaseModel):
    """A single example block extracted from ``X.examples.md``.

    Args:
        title (str): the markdown heading or first non-fenced line that
            preceded the code block. Empty string if no title was
            found.
        code (str): the body of the fenced code block, language tag
            stripped.
        kind (ExampleKind): heuristically classified. Slice 3 marks
            everything as ``"usage"``; later slices can refine to
            ``"composition"`` (multi-component) or ``"anti-pattern"``
            (under a "Don't" heading).
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    code: str
    kind: ExampleKind = "usage"


class Entity(BaseModel):
    """One indexed Prism artifact.

    Fields mirror PRD section 5. ``value``, ``source_file``, and
    ``category`` are unused by component entities but kept here so the
    same model serializes hooks, managers, utils, and tokens later
    without an awkward discriminated subclass split.

    Args:
        name (str): canonical name as it would appear in an import.
        type (EntityType): one of the five entity-type literals.
        version (str): tarball version this entity was extracted from.
        category (str | None): coarse grouping, e.g. ``"form"``,
            ``"feedback"``. Populated when known.
        summary (str): first paragraph of the docs / JSDoc.
        import_path (str): canonical import line for the LLM.
        signature (list[Member]): props (for components/hooks) or
            params+return (for utils/managers).
        examples (list[Example]): may be empty.
        deprecated (bool): ``True`` for components under ``deprecated/``
            or with a ``@deprecated`` JSDoc tag.
        value (str | None): only set for ``token`` entities.
        source_file (str | None): only set for ``token`` entities.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: EntityType
    version: str
    summary: str = ""
    import_path: str = ""
    signature: list[Member] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)
    deprecated: bool = False
    category: str | None = None
    value: str | None = None
    source_file: str | None = None


def entity_key(entity: Entity) -> tuple[str, str]:
    """Return the lookup key ``(type, name)`` for ``entity``.

    Names are not globally unique across types (a ``Tooltip`` component
    and a ``Tooltip`` util could coexist), so the indexer uses the
    tuple form.

    Args:
        entity (Entity): entity to key.

    Returns:
        tuple[str, str]: ``(type, name)``.
    """
    return (entity.type, entity.name)
