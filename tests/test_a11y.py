"""Tests for the slice-11 a11y aggregator.

Pins:

* LLMS.md → title + ordered H2/H3 sections,
* missing LLMS.md gracefully degrades to ``(None, [])``,
* per-component aggregation respects insertion order,
* :func:`get_a11y_for_component` is case-sensitive (matches Prism),
* non-a11y chunks are silently ignored at aggregation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_mcp.a11y import (
    A11yRules,
    ComponentA11y,
    build_a11y_rules,
    get_a11y_for_component,
)
from prism_mcp.parsers.examples_md_code import ExampleChunk


@pytest.fixture()
def package_root(tmp_path: Path) -> Path:
    """Return a writable ``package/`` directory rooted at tmp_path."""
    root = tmp_path / "package"
    root.mkdir()
    return root


def _a11y_chunk(component: str, title: str, body: str) -> ExampleChunk:
    """Build an ``is_a11y_block=True`` chunk for ``component``."""
    return ExampleChunk(
        component_name=component,
        title=title,
        code=body,
        language_tag="jsx noeditor",
        imports=[component],
        is_noeditor=True,
        is_a11y_block=True,
    )


def _regular_chunk(component: str, title: str) -> ExampleChunk:
    """Build a non-a11y chunk — should be ignored by the aggregator."""
    return ExampleChunk(
        component_name=component,
        title=title,
        code=f"<{component} />",
        language_tag="jsx",
        imports=[component],
    )


# ---------------------------------------------------------------------------
# LLMS.md parsing
# ---------------------------------------------------------------------------


def test_missing_llms_md_returns_empty_global_rules(package_root: Path) -> None:
    """No LLMS.md → ``title=None, global_rules=[]``.

    Pins the degraded-mode contract: a Prism build that doesn't ship
    LLMS.md still returns a valid (empty) :class:`A11yRules`
    instead of crashing.
    """
    rules = build_a11y_rules(package_root=package_root, chunks=[])

    assert rules.title is None
    assert rules.global_rules == []
    assert rules.per_component == []


def test_llms_md_title_is_h1_global_rules_are_h2_sections(
    package_root: Path,
) -> None:
    """H1 → title, H2 sections become ordered global rules."""
    (package_root / "LLMS.md").write_text(
        "# LLM Instructions for @nutanix-ui/prism-reactjs\n"
        "\n"
        "Intro paragraph that belongs to no section.\n"
        "\n"
        "## Source Priority\n"
        "1. Use examples.md.\n"
        "2. Use d.ts.\n"
        "\n"
        "## Deprecation Rules\n"
        "If marked deprecated, don't use.\n",
        encoding="utf-8",
    )

    rules = build_a11y_rules(package_root=package_root, chunks=[])

    assert rules.title == "LLM Instructions for @nutanix-ui/prism-reactjs"
    assert [s.heading for s in rules.global_rules] == [
        "Source Priority",
        "Deprecation Rules",
    ]
    assert all(s.depth == 2 for s in rules.global_rules)
    assert "Use examples.md" in rules.global_rules[0].body
    assert "deprecated" in rules.global_rules[1].body


def test_llms_md_h3_subsections_are_separate_sections(
    package_root: Path,
) -> None:
    """H3 sections come out as separate sections (depth=3).

    Pins that the parser doesn't try to be clever about nesting —
    flat list of sections in document order, each tagged with its
    depth so the consumer can re-build a tree if they want.
    """
    (package_root / "LLMS.md").write_text(
        "## H2 first\n"
        "first body\n"
        "### H3 nested under H2\n"
        "nested body\n"
        "## H2 second\n"
        "second body\n",
        encoding="utf-8",
    )

    rules = build_a11y_rules(package_root=package_root, chunks=[])

    headings = [(s.heading, s.depth) for s in rules.global_rules]
    assert headings == [
        ("H2 first", 2),
        ("H3 nested under H2", 3),
        ("H2 second", 2),
    ]


# ---------------------------------------------------------------------------
# Per-component aggregation
# ---------------------------------------------------------------------------


def test_per_component_groups_a11y_chunks_in_order(
    package_root: Path,
) -> None:
    """Multiple a11y chunks per component → one row, ordered blocks."""
    chunks = [
        _a11y_chunk("Modal", "Focus return", "Return focus to trigger."),
        _a11y_chunk("Modal", "ARIA", "<Modal ariaLabelledBy='id' />"),
        _a11y_chunk("Button", "Disabled state", "Disabled buttons need aria."),
    ]

    rules = build_a11y_rules(package_root=package_root, chunks=chunks)

    by_name = {r.component_name: r for r in rules.per_component}
    assert set(by_name) == {"Modal", "Button"}
    assert by_name["Modal"].blocks == [
        "Return focus to trigger.",
        "<Modal ariaLabelledBy='id' />",
    ]
    assert by_name["Modal"].titles == ["Focus return", "ARIA"]
    assert len(by_name["Button"].blocks) == 1


def test_per_component_preserves_first_seen_order(
    package_root: Path,
) -> None:
    """Components appear in the order their first a11y chunk shows up."""
    chunks = [
        _a11y_chunk("Tooltip", "Tip", "tip body"),
        _a11y_chunk("Alert", "Alert", "alert body"),
        _a11y_chunk("Tooltip", "Tip2", "tip body 2"),
    ]

    rules = build_a11y_rules(package_root=package_root, chunks=chunks)

    assert [r.component_name for r in rules.per_component] == [
        "Tooltip",
        "Alert",
    ]


def test_per_component_ignores_non_a11y_chunks(package_root: Path) -> None:
    """Regular chunks (``is_a11y_block=False``) are excluded."""
    chunks = [
        _regular_chunk("Modal", "Basic example"),
        _a11y_chunk("Modal", "A11y", "a11y body"),
        _regular_chunk("Button", "Default"),
    ]

    rules = build_a11y_rules(package_root=package_root, chunks=chunks)

    # Only Modal has a true a11y chunk; Button is filtered out.
    assert [r.component_name for r in rules.per_component] == ["Modal"]


# ---------------------------------------------------------------------------
# get_a11y_for_component
# ---------------------------------------------------------------------------


def test_get_a11y_for_component_returns_match() -> None:
    """Happy path: returns the matching :class:`ComponentA11y`."""
    rules = A11yRules(
        per_component=[
            ComponentA11y(
                component_name="Modal",
                blocks=["a", "b"],
                titles=["t1", "t2"],
            ),
            ComponentA11y(
                component_name="Button",
                blocks=["x"],
                titles=["bt"],
            ),
        ]
    )

    match = get_a11y_for_component("Modal", rules)

    assert match is not None
    assert match.component_name == "Modal"
    assert match.blocks == ["a", "b"]


def test_get_a11y_for_component_is_case_sensitive() -> None:
    """Component names are case-sensitive Prism identifiers."""
    rules = A11yRules(
        per_component=[
            ComponentA11y(component_name="Modal", blocks=[], titles=[]),
        ]
    )

    assert get_a11y_for_component("modal", rules) is None
    assert get_a11y_for_component("Modal", rules) is not None


def test_get_a11y_for_component_missing_returns_none() -> None:
    """Unknown name → ``None`` (caller decides whether that's an error)."""
    rules = A11yRules()

    assert get_a11y_for_component("Anything", rules) is None
