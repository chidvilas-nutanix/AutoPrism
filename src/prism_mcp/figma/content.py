"""Figma content resolution — icons + text-slot binding (roadmap P6).

The walker captures *what a region contains* — an icon glyph, a run of TEXT
— but leaves two codegen-critical decisions open:

* **Which Prism icon?** An icon region carries a Figma name
  (``"icon/chevron-down"``, ``"Menu"``); codegen needs the exact Prism
  component (``ChevronDownIcon``) out of the 213 ``*Icon`` exports.
* **Which prop does the text fill?** A region's text must render into the
  *right* prop of its resolved component — ``<Button>Save</Button>``
  (``children``) vs ``<Input label="Name" />`` (``label``) vs
  ``<Title>Overview</Title>`` (``children``).

Both resolvers here are pure + deterministic and mirror the P5 module shape:

* :func:`resolve_icon` — normalized-name match against an :class:`IconIndex`
  (built from the Prism icon vocabulary), with a small curated synonym map
  and a conservative, uniqueness-guarded fuzzy fallback.
* :func:`bind_text_content` — picks the text-bearing prop from the
  component's P3 prop schema (named text prop, by priority) with a
  ``children`` fallback for body-text components.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prism_mcp.figma.models import ContentBinding, PrismIcon

if TYPE_CHECKING:
    from prism_mcp.figma.prop_schema import ComponentPropSchema

# --------------------------------------------------------------------------
# Icon resolution.
# --------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_icon(name: str) -> str:
    """Reduce a Figma or Prism icon name to a comparable key.

    Lowercases, keeps only the last ``/``-segment (``icon/chevron-down`` →
    ``chevron-down``), strips non-alphanumerics, then peels the ``icon`` /
    ``logo`` affixes and a leading bare ``ic`` marker. So ``"icon/chevron-down"``,
    ``"ChevronDownIcon"``, and ``"ic_chevron_down"`` all reduce to
    ``"chevrondown"``.
    """
    s = name.strip().lower()
    s = s.rsplit("/", 1)[-1]
    s = _NON_ALNUM_RE.sub("", s)
    for affix in ("icon", "logo"):
        if s.startswith(affix) and len(s) > len(affix):
            s = s[len(affix) :]
        if s.endswith(affix) and len(s) > len(affix):
            s = s[: -len(affix)]
    if s.startswith("ic") and len(s) > 2:
        s = s[2:]
    return s


# Curated Figma-name → Prism-normalized-key synonyms. Only entries whose
# target is a real Prism icon (verified against the 213-icon vocabulary).
_ICON_SYNONYMS: dict[str, str] = {
    "hamburger": "menu",
    "search": "magglass",
    "magnifier": "magglass",
    "magnifyingglass": "magglass",
    "magnify": "magglass",
    "x": "close",
    "cross": "close",
    "dismiss": "close",
    "gear": "settings",
    "cog": "settings",
    "cogwheel": "settings",
    "pencil": "edit",
    "trash": "remove",
    "delete": "remove",
    "bin": "remove",
    "trashcan": "remove",
    "add": "plus",
    "expand": "plus",
}

_MIN_FUZZY_LEN = 4
"""A normalized name shorter than this never fuzzy-matches (``ai`` / ``vm``
would collide with too many icons); it must hit exact or synonym."""

# Generic Figma container / primitive / layout layer names. A region named
# ``"Group"`` / ``"Vector 39"`` / ``"Icon + Text"`` is structural scaffolding,
# not a nameable glyph — so it must NEVER resolve to an icon. Without this
# guard the fuzzy tier produces confident false positives (``"Group"`` →
# ``GroupByIcon``, ``"Icon + Text"`` → ``BoldTextIcon``). Checked against the
# normalized name with any trailing run-number stripped.
_GENERIC_ICON_NAMES: frozenset[str] = frozenset(
    {
        "vector",
        "group",
        "fill",
        "frame",
        "shape",
        "mask",
        "union",
        "subtract",
        "intersect",
        "exclude",
        "ellipse",
        "rectangle",
        "rect",
        "line",
        "path",
        "oval",
        "polygon",
        "star",
        "clip",
        "compound",
        "boolean",
        "layer",
        "component",
        "instance",
        "text",
        "background",
        "container",
        "wrapper",
        "placeholder",
        "image",
        "img",
    }
)

_TRAILING_DIGITS_RE = re.compile(r"\d+$")


@dataclass(frozen=True)
class IconIndex:
    """Normalized-name lookup over the Prism icon vocabulary.

    Attributes:
        by_norm (dict[str, str]): ``normalized-name → PrismComponentName``
            (``"chevrondown" → "ChevronDownIcon"``).
        version (str): library version stamp, for traceability.
    """

    by_norm: dict[str, str] = field(default_factory=dict)
    version: str = ""

    def __len__(self) -> int:
        return len(self.by_norm)


def build_icon_index(names: list[str], version: str = "") -> IconIndex:
    """Build an :class:`IconIndex` from Prism icon component names.

    Args:
        names (list[str]): Prism component names ending in ``Icon``
            (e.g. ``["ChevronDownIcon", "MenuIcon", …]``). Non-``Icon`` names
            and names that normalize to empty are skipped.
        version (str): library version label.

    Returns:
        IconIndex: ready to resolve. First writer wins on a normalized-key
        collision (rare), keeping the build deterministic for sorted input.
    """
    by_norm: dict[str, str] = {}
    for name in names:
        if not name.endswith("Icon"):
            continue
        norm = _normalize_icon(name)
        if norm:
            by_norm.setdefault(norm, name)
    return IconIndex(by_norm=by_norm, version=version)


def resolve_icon(figma_name: str, index: IconIndex) -> PrismIcon | None:
    """Resolve a Figma icon name to a Prism icon component (P6).

    Cascade: exact normalized match → curated synonym → conservative
    uniqueness-guarded fuzzy (one Prism icon whose normalized name
    contains / is contained by the query). Returns ``None`` when nothing
    resolves — the region keeps its raw name rather than a wrong icon.

    Args:
        figma_name (str): the Figma icon name / hint.
        index (IconIndex): the Prism icon vocabulary.

    Returns:
        PrismIcon | None: the resolved component + method/confidence.
    """
    if not figma_name or len(index) == 0:
        return None
    norm = _normalize_icon(figma_name)
    if not norm:
        return None
    # A generic container / primitive layer name is never a glyph — bail
    # before any tier so structural scaffolding can't false-positive.
    if _TRAILING_DIGITS_RE.sub("", norm) in _GENERIC_ICON_NAMES:
        return None

    exact = index.by_norm.get(norm)
    if exact is not None:
        return PrismIcon(
            figma_name=figma_name,
            prism_component=exact,
            method="exact",
            confidence=1.0,
        )

    syn_key = _ICON_SYNONYMS.get(norm)
    if syn_key is not None:
        syn = index.by_norm.get(syn_key)
        if syn is not None:
            return PrismIcon(
                figma_name=figma_name,
                prism_component=syn,
                method="synonym",
                confidence=0.9,
            )

    if len(norm) >= _MIN_FUZZY_LEN:
        hits = [
            comp
            for key, comp in index.by_norm.items()
            if (norm in key or key in norm) and abs(len(key) - len(norm)) <= 4
        ]
        if len(set(hits)) == 1:
            return PrismIcon(
                figma_name=figma_name,
                prism_component=hits[0],
                method="fuzzy",
                confidence=0.6,
            )
    return None


# --------------------------------------------------------------------------
# Text-slot binding.
# --------------------------------------------------------------------------

# Prop names that carry visible text, by descending priority. The first one a
# component declares (as a string / node / string-accepting prop) wins.
_TEXT_PROP_PRIORITY: tuple[str, ...] = (
    "title",
    "label",
    "heading",
    "header",
    "text",
    "caption",
    "placeholder",
    "content",
)

_TEXT_PROP_KINDS = frozenset({"string", "node"})

# Body-text leaf components whose text is rendered as ``children``. A
# component NOT in this set and lacking a named text prop gets **no** binding
# — we never bind text to a container (``Tile`` / ``Card``), whose visible
# text belongs to an inner element, not the container itself.
_TEXT_LEAF_COMPONENTS: frozenset[str] = frozenset(
    {
        "Title",
        "Paragraph",
        "TextLabel",
        "Text",
        "Label",
        "Heading",
        "Badge",
        "Tag",
        "Link",
        "Button",
        "Anchor",
    }
)


def bind_text_content(
    component: str,
    text: str,
    schema: ComponentPropSchema | None,
) -> ContentBinding | None:
    """Decide which prop ``text`` binds to for ``component`` (P6).

    Resolution: a named text prop in the schema (``title`` / ``label`` / …,
    by priority) wins for *any* component; otherwise the text falls to
    ``children`` only for a known body-text leaf (``Title`` / ``Button`` / …).
    A container or icon with no named text prop gets ``None`` — its visible
    text belongs to an inner element, not itself.

    Args:
        component (str): the resolved Prism component name.
        text (str): the region's representative text content.
        schema (ComponentPropSchema | None): the component's P3 prop schema,
            when available — the source of named text props.

    Returns:
        ContentBinding | None: the target prop + value, or ``None``.
    """
    text = (text or "").strip()
    if not text:
        return None

    if schema is not None:
        for prop_name in _TEXT_PROP_PRIORITY:
            prop = schema.props.get(prop_name)
            if prop is None:
                continue
            if prop.kind in _TEXT_PROP_KINDS or prop.accepts_string:
                return ContentBinding(
                    prop=prop_name,
                    value=text,
                    value_kind="string",
                    source="prop-schema",
                )

    if component in _TEXT_LEAF_COMPONENTS:
        return ContentBinding(
            prop="children",
            value=text,
            value_kind="children",
            source="children-default",
        )
    return None
