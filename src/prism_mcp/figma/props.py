"""Figma ``componentProperties`` -> typed Prism props (roadmap P3 Part B).

Given a routed region's Prism component (from P2 catalog routing) and the
Figma instance's ``componentProperties``, produce **exact, typed JSX
props**. The hard-won insight from the Part B research
(`improvements/04-phase3-routing-and-props.md` §6) is that Figma axis
*names* rarely match Prism prop names, but Figma axis *values* very often
match a Prism enum's string value or a union literal — so value-driven
matching, not name-driven, carries most of the load.

The cascade, per Figma property:

1. **TEXT** -> a text prop (curated, else ``children``).
2. **INSTANCE_SWAP** -> recorded as a nested-instance hint, not a prop
   (the swapped child is its own region in the walker).
3. **VARIANT / BOOLEAN**:
   a. boolean value (``True``/``False``) -> a ``boolean`` prop, matched
      by name then curated override.
   b. otherwise **name+value**: prop whose normalized name equals the
      axis and whose enum/union value set contains the value.
   c. otherwise **value-only**: the unique enum/union prop whose value
      set contains the value.
   d. otherwise **curated** axis->prop override.
   e. otherwise unresolved (kept for the coverage metric).

Pure + deterministic: no network, no LLM. Output feeds codegen and is
surfaced (compactly) in the lean walker response.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from prism_mcp.figma.prop_overrides import (
    FamilyOverrides,
    is_ignored_axis,
    overrides_for,
)
from prism_mcp.figma.prop_schema import (
    ComponentPropSchema,
    PropKind,
    PropSchema,
)

_BOOL_TRUE = frozenset({"true", "yes", "on", "1"})
_BOOL_FALSE = frozenset({"false", "no", "off", "0"})
_VALUE_SEP_RE = re.compile(r"[\s_/]+")
_NAME_DROP_RE = re.compile(r"[^a-z0-9]+")


def _norm_value(text: str) -> str:
    """Normalize a variant *value* for set membership (keeps hyphens).

    ``"Dark Primary"`` -> ``"dark-primary"`` so it matches an enum value
    like ``"dark-primary"``.
    """
    return _VALUE_SEP_RE.sub("-", text.strip().lower())


def _norm_name(text: str) -> str:
    """Normalize an *identifier* for name matching (drops all punctuation).

    ``"Full Width"`` / ``"full_width"`` -> ``"fullwidth"`` so a Figma axis
    collides with the camelCase prop ``fullWidth``; ``"Placed on:"`` ->
    ``"placedon"`` so trailing punctuation does not defeat an ignore-list
    or curated lookup.
    """
    return _NAME_DROP_RE.sub("", text.lower())


class ResolvedProp(BaseModel):
    """One Figma property resolved to a typed Prism prop.

    Args:
        prop (str): Prism prop name (e.g. ``"type"``).
        value (str): JSX-ready value — an expression (``ButtonTypes.PRIMARY``),
            a string (``square``), a bool (``true``), or text.
        value_kind (Literal): how ``value`` must be emitted: ``expr`` ->
            ``prop={value}``; ``string`` -> ``prop="value"``; ``bool`` ->
            ``prop`` / ``prop={false}``.
        prop_kind (PropKind): the target prop's schema kind.
        source_axis (str): the (cleaned) Figma property name.
        figma_value (str): the raw Figma value.
        method (str): which cascade rung fired (provenance / debugging).
        confidence (float): 0-1 heuristic confidence.
    """

    model_config = ConfigDict(extra="forbid")

    prop: str
    value: str
    value_kind: Literal["expr", "string", "bool"]
    prop_kind: PropKind
    source_axis: str
    figma_value: str
    method: str
    confidence: float


class UnresolvedProp(BaseModel):
    """A Figma property the resolver could not map deterministically.

    Args:
        axis (str): cleaned Figma property name.
        figma_value (str): raw Figma value.
        figma_kind (str): Figma property type (``VARIANT`` / ``TEXT`` / …).
        reason (str): why it was not resolved.
    """

    model_config = ConfigDict(extra="forbid")

    axis: str
    figma_value: str
    figma_kind: str
    reason: str


class PropResolution(BaseModel):
    """Outcome of resolving one instance's ``componentProperties``.

    Args:
        component (str): the Prism component the props belong to.
        props (list[ResolvedProp]): resolved typed props.
        unresolved (list[UnresolvedProp]): the residue.
    """

    model_config = ConfigDict(extra="forbid")

    component: str
    props: list[ResolvedProp] = []
    unresolved: list[UnresolvedProp] = []


def _clean_axis(axis_raw: str) -> str:
    """Strip Figma's ``#<nodeId>`` suffix from a property key."""
    return axis_raw.split("#", 1)[0].strip()


def _enum_member_expr(prop: PropSchema, nval: str) -> str | None:
    """Return ``Enum.MEMBER`` whose value normalizes to ``nval``."""
    if not prop.enum_name:
        return None
    for member, value in prop.enum_members.items():
        if _norm_value(value) == nval:
            return f"{prop.enum_name}.{member}"
    return None


def _union_literal(prop: PropSchema, nval: str) -> str | None:
    """Return the union literal whose normalized form equals ``nval``."""
    for literal in prop.values:
        if _norm_value(literal) == nval:
            return literal
    return None


def _value_in_prop(prop: PropSchema, nval: str) -> bool:
    return any(_norm_value(v) == nval for v in prop.values)


def _emit_value(
    prop: PropSchema,
    *,
    nval: str,
    axis: str,
    figma_value: str,
    method: str,
    confidence: float,
) -> ResolvedProp | None:
    """Build a :class:`ResolvedProp` for an enum/union *value* match."""
    if prop.kind == "enum":
        expr = _enum_member_expr(prop, nval)
        if expr is not None:
            return ResolvedProp(
                prop=prop.name,
                value=expr,
                value_kind="expr",
                prop_kind=prop.kind,
                source_axis=axis,
                figma_value=figma_value,
                method=method,
                confidence=confidence,
            )
    if prop.kind == "union" or (prop.kind == "enum" and prop.accepts_string):
        literal = _union_literal(prop, nval) or (
            figma_value if prop.accepts_string else None
        )
        if literal is not None:
            return ResolvedProp(
                prop=prop.name,
                value=literal,
                value_kind="string",
                prop_kind=prop.kind,
                source_axis=axis,
                figma_value=figma_value,
                method=method
                if _union_literal(prop, nval)
                else f"{method}-string",
                confidence=confidence
                if _union_literal(prop, nval)
                else confidence - 0.2,
            )
    return None


def _resolve_boolean(
    axis: str,
    figma_value: str,
    nval: str,
    schema: ComponentPropSchema,
    overrides: FamilyOverrides,
) -> ResolvedProp | None:
    """Resolve a True/False variant to a boolean prop (name then curated)."""
    bool_value = "true" if nval in _BOOL_TRUE else "false"
    axis_key = _norm_name(axis)

    target = schema.props.get(overrides.axis_to_prop.get(axis_key, ""))
    method = "bool-curated"
    confidence = 0.9
    if target is None or target.kind != "boolean":
        target = next(
            (
                p
                for p in schema.props.values()
                if p.kind == "boolean" and _norm_name(p.name) == axis_key
            ),
            None,
        )
        method = "bool-name"
        confidence = 0.92
    if target is None:
        return None
    return ResolvedProp(
        prop=target.name,
        value=bool_value,
        value_kind="bool",
        prop_kind="boolean",
        source_axis=axis,
        figma_value=figma_value,
        method=method,
        confidence=confidence,
    )


def _resolve_variant(
    axis: str,
    figma_value: str,
    schema: ComponentPropSchema,
    overrides: FamilyOverrides,
) -> ResolvedProp | None:
    """Resolve a non-boolean VARIANT value via the name/value/curated cascade."""
    nval = _norm_value(figma_value)
    axis_key = _norm_name(axis)

    named = next(
        (p for p in schema.props.values() if _norm_name(p.name) == axis_key),
        None,
    )
    if named is not None and named.kind in ("enum", "union"):
        emitted = _emit_value(
            named,
            nval=nval,
            axis=axis,
            figma_value=figma_value,
            method="name+value",
            confidence=0.97,
        )
        if emitted is not None:
            return emitted

    matches = [
        p
        for p in schema.props.values()
        if p.kind in ("enum", "union") and _value_in_prop(p, nval)
    ]
    if len(matches) == 1:
        emitted = _emit_value(
            matches[0],
            nval=nval,
            axis=axis,
            figma_value=figma_value,
            method="value",
            confidence=0.9,
        )
        if emitted is not None:
            return emitted

    curated = schema.props.get(overrides.axis_to_prop.get(axis_key, ""))
    if curated is not None:
        emitted = _emit_value(
            curated,
            nval=nval,
            axis=axis,
            figma_value=figma_value,
            method="curated",
            confidence=0.85,
        )
        if emitted is not None:
            return emitted
    return None


def resolve_props(
    component_properties: dict[str, Any],
    schema: ComponentPropSchema,
) -> PropResolution:
    """Resolve a Figma instance's ``componentProperties`` to typed props.

    Args:
        component_properties (dict[str, Any]): the Figma instance's
            ``componentProperties`` map (``axis -> {type, value}``).
        schema (ComponentPropSchema): the target component's prop schema.

    Returns:
        PropResolution: resolved props + the unresolved residue.
    """
    overrides = overrides_for(schema.family)
    resolved: list[ResolvedProp] = []
    unresolved: list[UnresolvedProp] = []

    for axis_raw, spec in (component_properties or {}).items():
        if not isinstance(spec, dict):
            continue
        figma_kind = str(spec.get("type", "VARIANT"))
        figma_value = "" if spec.get("value") is None else str(spec["value"])
        axis = _clean_axis(axis_raw)
        axis_key = _norm_name(axis)

        if is_ignored_axis(schema.family, axis_key):
            continue

        if figma_kind == "TEXT":
            prop = overrides.text_axis_to_prop.get(axis_key, "children")
            resolved.append(
                ResolvedProp(
                    prop=prop,
                    value=figma_value,
                    value_kind="string",
                    prop_kind="node",
                    source_axis=axis,
                    figma_value=figma_value,
                    method="text",
                    confidence=0.9,
                )
            )
            continue

        if figma_kind == "INSTANCE_SWAP":
            unresolved.append(
                UnresolvedProp(
                    axis=axis,
                    figma_value=figma_value,
                    figma_kind=figma_kind,
                    reason="nested instance (resolved as child region)",
                )
            )
            continue

        nval = _norm_value(figma_value)
        if figma_kind == "BOOLEAN" or nval in _BOOL_TRUE or nval in _BOOL_FALSE:
            emitted = _resolve_boolean(
                axis, figma_value, nval, schema, overrides
            )
            if emitted is not None:
                resolved.append(emitted)
            else:
                unresolved.append(
                    UnresolvedProp(
                        axis=axis,
                        figma_value=figma_value,
                        figma_kind=figma_kind,
                        reason="no boolean prop matches axis name",
                    )
                )
            continue

        emitted = _resolve_variant(axis, figma_value, schema, overrides)
        if emitted is not None:
            resolved.append(emitted)
        else:
            unresolved.append(
                UnresolvedProp(
                    axis=axis,
                    figma_value=figma_value,
                    figma_kind=figma_kind,
                    reason="value not in any enum/union; no name or "
                    "curated match",
                )
            )

    return PropResolution(
        component=schema.component, props=resolved, unresolved=unresolved
    )
