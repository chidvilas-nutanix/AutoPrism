"""Tests for the Figma componentProperties -> Prism props resolver (P3 B)."""

from __future__ import annotations

from prism_mcp.figma.prop_schema import ComponentPropSchema, PropSchema
from prism_mcp.figma.props import resolve_props


def _schema(family: str, **props: PropSchema) -> ComponentPropSchema:
    return ComponentPropSchema(
        component=family, family=family, props=dict(props)
    )


_BUTTON = _schema(
    "Button",
    type=PropSchema(
        name="type",
        kind="enum",
        enum_name="ButtonTypes",
        enum_members={
            "PRIMARY": "primary",
            "SECONDARY": "secondary",
            "DARK_PRIMARY": "dark-primary",
        },
        values=["primary", "secondary", "dark-primary"],
    ),
    appearance=PropSchema(
        name="appearance",
        kind="union",
        values=["default", "square", "underline"],
    ),
    disabled=PropSchema(name="disabled", kind="boolean"),
    children=PropSchema(name="children", kind="node"),
)


def _variant(value: str) -> dict[str, object]:
    return {"type": "VARIANT", "value": value}


def _by_prop(resolution) -> dict[str, object]:
    return {p.prop: p for p in resolution.props}


# -----------------------------------------------------------------------
# Value-driven matching (the core bridge)
# -----------------------------------------------------------------------


def test_value_matches_enum_member_expression() -> None:
    """A Figma value ``Primary`` resolves to ``ButtonTypes.PRIMARY``."""
    res = resolve_props({"Weight": _variant("Primary")}, _BUTTON)

    prop = _by_prop(res)["type"]
    assert prop.value == "ButtonTypes.PRIMARY"
    assert prop.value_kind == "expr"
    assert prop.method == "value"


def test_value_matches_hyphenated_enum() -> None:
    """``Dark Primary`` normalizes to the ``dark-primary`` enum value."""
    res = resolve_props({"Weight": _variant("Dark Primary")}, _BUTTON)

    assert _by_prop(res)["type"].value == "ButtonTypes.DARK_PRIMARY"


def test_value_matches_union_literal() -> None:
    """A union value is emitted as a quotable string literal."""
    res = resolve_props({"Style": _variant("Square")}, _BUTTON)

    prop = _by_prop(res)["appearance"]
    assert prop.value == "square"
    assert prop.value_kind == "string"


def test_name_plus_value_takes_precedence() -> None:
    """When the axis name equals the prop name, that prop wins."""
    # "Appearance" name-matches the `appearance` union; value "Default".
    res = resolve_props({"Appearance": _variant("Default")}, _BUTTON)

    prop = _by_prop(res)["appearance"]
    assert prop.value == "default"
    assert prop.method == "name+value"


# -----------------------------------------------------------------------
# Booleans
# -----------------------------------------------------------------------


def test_boolean_resolves_by_axis_name() -> None:
    """``Disabled=False`` maps to the ``disabled`` boolean prop."""
    res = resolve_props({"Disabled": _variant("False")}, _BUTTON)

    prop = _by_prop(res)["disabled"]
    assert prop.value == "false"
    assert prop.value_kind == "bool"
    assert prop.method == "bool-name"


def test_boolean_true_value() -> None:
    res = resolve_props({"Disabled": _variant("True")}, _BUTTON)

    assert _by_prop(res)["disabled"].value == "true"


def test_boolean_kind_property() -> None:
    """A Figma ``BOOLEAN`` property type is treated as boolean too."""
    res = resolve_props(
        {"Disabled": {"type": "BOOLEAN", "value": True}}, _BUTTON
    )

    assert _by_prop(res)["disabled"].value == "true"


# -----------------------------------------------------------------------
# TEXT and INSTANCE_SWAP
# -----------------------------------------------------------------------


def test_text_property_maps_to_children() -> None:
    """A TEXT property defaults to ``children``."""
    res = resolve_props(
        {"Label#1:0": {"type": "TEXT", "value": "Save"}}, _BUTTON
    )

    prop = _by_prop(res)["children"]
    assert prop.value == "Save"
    assert prop.method == "text"


def test_instance_swap_is_unresolved_not_a_prop() -> None:
    """An INSTANCE_SWAP is recorded as a nested-instance hint.

    Uses a non-ignored axis name ("Leading Slot"); Button's curated
    ignore-list deliberately drops its design-only "Icon" axis, which
    would otherwise be filtered before the INSTANCE_SWAP branch.
    """
    res = resolve_props(
        {"Leading Slot": {"type": "INSTANCE_SWAP", "value": "abc:123"}},
        _BUTTON,
    )

    assert res.props == []
    assert len(res.unresolved) == 1
    assert res.unresolved[0].figma_kind == "INSTANCE_SWAP"


# -----------------------------------------------------------------------
# Misses + ignores
# -----------------------------------------------------------------------


def test_unmappable_value_is_unresolved() -> None:
    """A value in no enum/union and no name/curated match stays unresolved."""
    res = resolve_props({"Weight": _variant("Nonexistent")}, _BUTTON)

    assert res.props == []
    assert res.unresolved[0].axis == "Weight"


def test_global_ignore_axis_dropped() -> None:
    """A global design-only axis (``Placed On``) is silently dropped."""
    res = resolve_props({"Placed On": _variant("Base")}, _BUTTON)

    assert res.props == []
    assert res.unresolved == []


def test_family_ignore_axis_dropped() -> None:
    """A Button-specific design-only axis (``Icon``) is dropped."""
    res = resolve_props({"Icon": _variant("Left")}, _BUTTON)

    assert res.props == []
    assert res.unresolved == []


def test_axis_node_suffix_is_stripped() -> None:
    """The Figma ``#<nodeId>`` suffix does not defeat resolution."""
    res = resolve_props({"Weight#42:7": _variant("Secondary")}, _BUTTON)

    assert _by_prop(res)["type"].value == "ButtonTypes.SECONDARY"


def test_multiple_axes_resolve_together() -> None:
    """A realistic instance resolves several props in one pass."""
    res = resolve_props(
        {
            "Weight": _variant("Primary"),
            "Disabled": _variant("False"),
            "Label#1:0": {"type": "TEXT", "value": "Go"},
            "Placed On": _variant("Base"),  # ignored
        },
        _BUTTON,
    )

    by = _by_prop(res)
    assert by["type"].value == "ButtonTypes.PRIMARY"
    assert by["disabled"].value == "false"
    assert by["children"].value == "Go"
    assert "Placed On" not in {u.axis for u in res.unresolved}


def test_empty_component_properties() -> None:
    """No properties -> empty resolution, no error."""
    res = resolve_props({}, _BUTTON)

    assert res.props == []
    assert res.unresolved == []
    assert res.component == "Button"
