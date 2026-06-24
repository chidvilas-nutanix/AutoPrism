"""Tests for the Prism prop-schema index (roadmap P3 Part B)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_mcp.entities import Member
from prism_mcp.figma.prop_schema import (
    PROP_SCHEMA_VERSION,
    ComponentPropSchema,
    PropSchema,
    PropSchemaIndex,
    build_family_schemas,
    build_prop_schema,
    classify_prop,
    get_prop_schema,
)


def _prop(name: str, type_str: str, **kw: object) -> Member:
    return Member(
        name=name,
        kind="prop",
        type=type_str,
        required=bool(kw.get("required", False)),
        default=kw.get("default"),  # type: ignore[arg-type]
        description=str(kw.get("description", "")),
    )


# -----------------------------------------------------------------------
# classify_prop
# -----------------------------------------------------------------------


def test_classify_enum_reference() -> None:
    """A prop whose type is a known enum becomes ``kind="enum"``."""
    enums = {"ButtonTypes": {"PRIMARY": "primary", "SECONDARY": "secondary"}}

    schema = classify_prop(_prop("type", "ButtonTypes"), enums)

    assert schema.kind == "enum"
    assert schema.enum_name == "ButtonTypes"
    assert schema.enum_members == enums["ButtonTypes"]
    assert set(schema.values) == {"primary", "secondary"}
    assert schema.accepts_string is False


def test_classify_enum_or_string_union() -> None:
    """``Enum | string`` is still an enum, but flags ``accepts_string``."""
    enums = {"BadgeColorTypes": {"RED": "red", "BLUE": "blue"}}

    schema = classify_prop(_prop("color", "BadgeColorTypes | string"), enums)

    assert schema.kind == "enum"
    assert schema.enum_name == "BadgeColorTypes"
    assert schema.accepts_string is True


def test_classify_string_literal_union() -> None:
    """A pure string-literal union becomes ``kind="union"``."""
    schema = classify_prop(_prop("textType", "'primary' | 'secondary'"), {})

    assert schema.kind == "union"
    assert schema.values == ["primary", "secondary"]


def test_classify_boolean_number_node_string_other() -> None:
    """The remaining kinds are classified by their primitive token."""
    assert classify_prop(_prop("disabled", "boolean"), {}).kind == "boolean"
    assert classify_prop(_prop("count", "number"), {}).kind == "number"
    assert (
        classify_prop(_prop("children", "React.ReactNode"), {}).kind == "node"
    )
    assert classify_prop(_prop("className", "string"), {}).kind == "string"
    assert (
        classify_prop(_prop("ref", "ButtonHTMLAttributes<T>"), {}).kind
        == "other"
    )


def test_classify_carries_required_and_default() -> None:
    """Required + default metadata survive classification."""
    schema = classify_prop(
        _prop("type", "ButtonTypes", required=True, default="primary"),
        {"ButtonTypes": {"PRIMARY": "primary"}},
    )

    assert schema.required is True
    assert schema.default == "primary"


# -----------------------------------------------------------------------
# build_family_schemas — enum pooling across sibling files
# -----------------------------------------------------------------------


def test_build_family_pools_enums_across_sibling_files(
    tmp_path: Path,
) -> None:
    """An enum in one file resolves a prop declared in a sibling file."""
    family = tmp_path / "Button"
    family.mkdir()
    (family / "buttonTypes.d.ts").write_text(
        'export declare enum ButtonTypes { PRIMARY = "primary", '
        'SECONDARY = "secondary" }',
        encoding="utf-8",
    )
    (family / "Button.d.ts").write_text(
        "import { ButtonTypes } from './buttonTypes';\n"
        "export interface ButtonProps {\n"
        "    type?: ButtonTypes;\n"
        "    disabled?: boolean;\n"
        "}\n",
        encoding="utf-8",
    )

    schemas = build_family_schemas("Button", sorted(family.glob("*.d.ts")))
    by_component = {s.component: s for s in schemas}

    button = by_component["Button"]
    assert button.family == "Button"
    # The enum lived in buttonTypes.d.ts but resolved Button's `type`.
    assert button.props["type"].kind == "enum"
    assert button.props["type"].enum_name == "ButtonTypes"
    assert button.props["disabled"].kind == "boolean"


def test_build_prop_schema_artifact_shape(tmp_path: Path) -> None:
    """The artifact carries version, components, and a family index."""
    family = tmp_path / "Badge"
    family.mkdir()
    (family / "Badge.d.ts").write_text(
        'export declare enum BadgeTypes { BADGE = "badge", TAG = "tag" }\n'
        "export interface BadgeProps { type?: BadgeTypes; }\n",
        encoding="utf-8",
    )

    artifact = build_prop_schema(
        {"Badge": sorted(family.glob("*.d.ts"))}, rplib_version="9.9.9"
    )

    assert artifact["schema_version"] == PROP_SCHEMA_VERSION
    assert artifact["rplib_version"] == "9.9.9"
    assert "Badge" in artifact["components"]
    assert artifact["families"]["Badge"]["main"] == "Badge"


# -----------------------------------------------------------------------
# PropSchemaIndex
# -----------------------------------------------------------------------


def _index() -> PropSchemaIndex:
    artifact = {
        "schema_version": PROP_SCHEMA_VERSION,
        "rplib_version": "test",
        "components": {
            "Table": ComponentPropSchema(
                component="Table", family="Tables", props={}
            ).model_dump(),
            "TableCell": ComponentPropSchema(
                component="TableCell",
                family="Tables",
                props={
                    "align": PropSchema(
                        name="align", kind="union", values=["left", "right"]
                    )
                },
            ).model_dump(),
        },
        "families": {
            "Tables": {
                "main": "Table",
                "components": ["Table", "TableCell"],
            }
        },
    }
    return PropSchemaIndex.from_artifact(artifact)


def test_index_version_mismatch_raises() -> None:
    """A drifted ``schema_version`` is a hard failure, not silent."""
    with pytest.raises(ValueError, match="schema_version"):
        PropSchemaIndex.from_artifact(
            {"schema_version": PROP_SCHEMA_VERSION + 99, "components": {}}
        )


def test_for_family_returns_main_component() -> None:
    """``for_family`` resolves the family directory to its main export."""
    index = _index()

    schema = index.for_family("Tables")

    assert schema is not None
    assert schema.component == "Table"


def test_for_region_selects_subcomponent_by_name() -> None:
    """The Figma instance name pins the most specific sub-component."""
    index = _index()

    # "Table/Table Cell" -> normalized "tabletablecell" contains
    # "tablecell" (len 9) which beats "table" (len 5).
    schema = index.for_region("Tables", "Table/Table Cell")

    assert schema is not None
    assert schema.component == "TableCell"


def test_for_region_falls_back_to_main_when_no_name_match() -> None:
    """A name with no sub-component token falls back to the family main."""
    index = _index()

    schema = index.for_region("Tables", "Table/Mystery Widget")

    assert schema is not None
    assert schema.component == "Table"


def test_for_region_without_name_is_family_main() -> None:
    """No Figma name => family main (the prior behaviour)."""
    assert _index().for_region("Tables", None).component == "Table"


# -----------------------------------------------------------------------
# Committed artifact smoke test
# -----------------------------------------------------------------------


def test_committed_artifact_loads_and_has_button() -> None:
    """The shipped ``prism_prop_schema.json`` loads and has known props."""
    index = get_prop_schema()

    button = index.for_family("Button")
    assert button is not None
    assert button.props["type"].kind == "enum"
    assert button.props["type"].enum_name == "ButtonTypes"
    assert "primary" in button.props["type"].values
    assert button.props["disabled"].kind == "boolean"
