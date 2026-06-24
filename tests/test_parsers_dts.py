"""Tests for the ``.d.ts`` parser."""

from __future__ import annotations

from prism_mcp.parsers.dts import (
    parse_classes,
    parse_enums,
    parse_functions,
    parse_interfaces,
)


def test_parses_simple_props_interface() -> None:
    """Single interface with primitive types and one optional prop."""
    source = """
    import * as React from 'react';

    export interface ButtonProps {
        className?: string;
        disabled?: boolean;
        children: React.ReactNode;
    }
    """

    interfaces = parse_interfaces(source)

    assert len(interfaces) == 1
    iface = interfaces[0]
    assert iface.name == "ButtonProps"

    by_name = {m.name: m for m in iface.members}
    assert set(by_name) == {"className", "disabled", "children"}
    assert by_name["className"].type == "string"
    assert by_name["className"].required is False
    assert by_name["disabled"].required is False
    assert by_name["children"].required is True
    assert by_name["children"].type == "React.ReactNode"


def test_parses_function_type_with_arrow_and_parens() -> None:
    """``(event) => void`` types must not be split on the comma."""
    source = """
    export interface XProps {
        onClick?: (event: React.MouseEvent, extra: number) => void;
    }
    """

    iface = parse_interfaces(source)[0]
    members = {m.name: m for m in iface.members}

    assert "onClick" in members
    assert (
        members["onClick"].type
        == "(event: React.MouseEvent, extra: number) => void"
    )


def test_parses_jsdoc_description_and_default() -> None:
    """JSDoc descriptions and ``@default`` tags attach to the right prop."""
    source = """
    export interface XProps {
        /**
         * Customize additional class name.
         * @default `none`
         */
        className?: string;
    }
    """

    iface = parse_interfaces(source)[0]
    member = iface.members[0]

    assert member.description == "Customize additional class name."
    assert member.default == "none"


def test_parses_quoted_member_names() -> None:
    """Members like ``'aria-disabled'?: ...`` are unquoted in the result."""
    source = """
    export interface XProps {
        'aria-disabled'?: boolean | 'true' | 'false';
    }
    """

    iface = parse_interfaces(source)[0]
    member = iface.members[0]

    assert member.name == "aria-disabled"
    assert member.required is False
    assert "boolean" in member.type


def test_handles_multiple_interfaces_in_one_file() -> None:
    """Both interfaces are returned in source order."""
    source = """
    export interface AProps { a: string; }
    export interface BProps { b: number; }
    """

    interfaces = parse_interfaces(source)

    assert [i.name for i in interfaces] == ["AProps", "BProps"]


def test_handles_extends_clause() -> None:
    """``extends`` between header and body doesn't confuse the brace finder."""
    source = """
    export interface ButtonProps extends Omit<React.HTMLProps<HTMLButtonElement>, 'ref'> {
        kind?: 'primary' | 'secondary';
    }
    """

    iface = parse_interfaces(source)[0]

    assert iface.name == "ButtonProps"
    assert iface.members[0].name == "kind"


def test_strips_line_comments_inside_body() -> None:
    """`` // ...`` lines don't get parsed as members."""
    source = """
    export interface XProps {
        // internal note
        className?: string;
    }
    """

    iface = parse_interfaces(source)[0]

    assert [m.name for m in iface.members] == ["className"]


def test_returns_empty_list_when_no_interfaces() -> None:
    """A file with no interface declarations yields ``[]``."""
    source = "export declare const x: number;"

    assert parse_interfaces(source) == []


def test_deprecated_interface_flag_is_set() -> None:
    """``@deprecated`` JSDoc on the interface itself is captured."""
    source = """
    /**
     * Old buttons.
     * @deprecated use NewButton instead.
     */
    export interface OldButtonProps {
        x?: string;
    }
    """

    iface = parse_interfaces(source)[0]

    assert iface.deprecated is True


def test_handles_nested_object_type_in_member() -> None:
    """Object type ``{ ... }`` inside a member must not split the body."""
    source = """
    export interface XProps {
        config?: { width: number; height: number };
        name: string;
    }
    """

    iface = parse_interfaces(source)[0]
    by_name = {m.name: m for m in iface.members}

    assert set(by_name) == {"config", "name"}
    assert by_name["config"].type == "{ width: number; height: number }"


# -----------------------------------------------------------------------
# Function parser (Slice 5)
# -----------------------------------------------------------------------


def test_parse_functions_handles_classic_function_declaration() -> None:
    """``export declare function`` is parsed into params + return."""
    source = """
    export declare function useFocusTrap(
        ref: React.RefObject<HTMLElement>,
        options?: FocusTrapOptions
    ): void;
    """

    funcs = parse_functions(source)

    assert len(funcs) == 1
    assert funcs[0].name == "useFocusTrap"
    assert funcs[0].shape == "function"
    assert funcs[0].return_type == "void"
    names = [p.name for p in funcs[0].params]
    assert names == ["ref", "options"]
    assert funcs[0].params[0].required is True
    assert funcs[0].params[1].required is False


def test_parse_functions_handles_arrow_const() -> None:
    """``export declare const X: (...) => R`` is the tsc arrow shape."""
    source = """
    export declare const useFocusTrap: (
        ref: React.RefObject<HTMLDivElement>,
        options?: UseFocusTrapOptions
    ) => React.RefObject<HTMLDivElement>;
    """

    funcs = parse_functions(source)

    assert len(funcs) == 1
    assert funcs[0].name == "useFocusTrap"
    assert funcs[0].shape == "arrow-const"
    assert "RefObject" in funcs[0].return_type
    assert {p.name for p in funcs[0].params} == {"ref", "options"}


def test_parse_functions_captures_description_and_deprecated() -> None:
    """JSDoc summary and ``@deprecated`` attach to the right function."""
    source = """
    /**
     * Trap focus inside the container.
     * @deprecated use the new hook
     */
    export declare function useFocusTrap(ref: any): void;
    """

    func = parse_functions(source)[0]

    assert func.description == "Trap focus inside the container."
    assert func.deprecated is True


def test_parse_functions_returns_multiple_in_order() -> None:
    """Two exports yield two parsed callables in source order."""
    source = """
    export declare const a: (x: number) => number;
    export declare const b: (y: string) => string;
    """

    funcs = parse_functions(source)

    assert [f.name for f in funcs] == ["a", "b"]


def test_parse_functions_handles_param_default_value() -> None:
    """``= default`` in a param is captured and removes ``required``."""
    source = """
    export declare const f: (a: number, b?: number) => number;
    """

    func = parse_functions(source)[0]

    by_name = {p.name: p for p in func.params}
    assert by_name["b"].required is False


# -----------------------------------------------------------------------
# Class parser (Slice 5)
# -----------------------------------------------------------------------


def test_parse_classes_extracts_method_signatures() -> None:
    """``declare class X { method(): R }`` produces method members."""
    source = """
    /**
     * I18n singleton.
     */
    declare class I18nManager {
        locale: string;
        constructor();
        initialize(map: Record<string, string>): void;
        /** Translate. */
        t(moduleName: string, key: string, count?: number): string;
    }
    declare const instance: I18nManager;
    export default instance;
    """

    classes = parse_classes(source)

    assert len(classes) == 1
    cls = classes[0]
    assert cls.name == "I18nManager"
    method_names = [m.name for m in cls.methods]
    # `locale` is a field, `constructor` is excluded.
    assert method_names == ["initialize", "t"]
    initialize = cls.methods[0]
    assert initialize.kind == "method"
    assert "Record" in initialize.type
    t = cls.methods[1]
    assert t.description == "Translate."


def test_parse_classes_marks_static_methods_in_description() -> None:
    """``static`` modifier surfaces in the description."""
    source = """
    declare class Util {
        static helper(value: number): string;
    }
    """

    cls = parse_classes(source)[0]
    method = cls.methods[0]

    assert method.name == "helper"
    assert method.description == "static method."


def test_parse_classes_handles_deprecated_jsdoc() -> None:
    """``@deprecated`` on a class lifts to :class:`ParsedClass`."""
    source = """
    /**
     * Old.
     * @deprecated use NewManager.
     */
    declare class OldManager {
        t(key: string): string;
    }
    """

    cls = parse_classes(source)[0]

    assert cls.deprecated is True


# -----------------------------------------------------------------------
# Enum parser (P3 Part B: prop resolution)
# -----------------------------------------------------------------------


def test_parse_enums_string_members() -> None:
    """``export declare enum`` yields a MEMBER -> value map in order."""
    source = """
    export declare enum ButtonTypes {
        PRIMARY = "primary",
        SECONDARY = "secondary",
        DESTRUCTIVE = "destructive"
    }
    """

    enums = parse_enums(source)

    assert len(enums) == 1
    assert enums[0].name == "ButtonTypes"
    assert enums[0].members == {
        "PRIMARY": "primary",
        "SECONDARY": "secondary",
        "DESTRUCTIVE": "destructive",
    }
    # Insertion order is preserved (Figma value -> member lookup relies
    # on nothing, but stable order keeps the artifact deterministic).
    assert list(enums[0].members) == ["PRIMARY", "SECONDARY", "DESTRUCTIVE"]


def test_parse_enums_ignores_jsdoc_embedded_examples() -> None:
    """A literal ``enum X { ... }`` *inside* JSDoc is not a declaration.

    Regression guard: the spec-library d.ts files document enums in
    prose, which naive scanning double-counts as empty enums.
    """
    source = """
    export declare enum BadgeTypes {
        BADGE = "badge",
        TAG = "tag"
    }
    export interface BadgeProps {
        /**
         * Change display to badge or tag style.
         *
         * enum BadgeTypes {<br />
         *   BADGE = 'badge',<br />
         *   TAG = 'tag'<br />
         * }
         */
        type?: BadgeTypes;
    }
    """

    enums = parse_enums(source)

    assert len(enums) == 1
    assert enums[0].name == "BadgeTypes"
    assert enums[0].members == {"BADGE": "badge", "TAG": "tag"}


def test_parse_enums_const_and_numeric_and_bare() -> None:
    """``const enum`` works; numeric / bare members fall back to name."""
    source = """
    export declare const enum Mixed {
        A = "alpha",
        B = 2,
        C
    }
    """

    enums = parse_enums(source)

    assert enums[0].name == "Mixed"
    # String literal keeps its value; numeric + bare fall back to the
    # member name (they can never value-match a Figma variant string).
    assert enums[0].members == {"A": "alpha", "B": "2", "C": "C"}


def test_parse_enums_multiple_in_one_file() -> None:
    """Several enums in one file are each captured independently."""
    source = """
    export declare enum A { X = "x" }
    export declare enum B { Y = "y", Z = "z" }
    """

    enums = parse_enums(source)

    assert [e.name for e in enums] == ["A", "B"]
    assert enums[1].members == {"Y": "y", "Z": "z"}


def test_parse_enums_absent() -> None:
    """A file with no enums yields an empty list (not an error)."""
    assert parse_enums("export interface P { a?: string; }") == []
