"""Tests for the ``examples.md`` parser."""

from __future__ import annotations

from prism_mcp.parsers.examples_md import parse_examples, parse_summary


def test_extracts_basic_fenced_block() -> None:
    """A single fenced block with a preceding title becomes one example."""
    markdown = """
Basic Example
```jsx
<Button>Hello</Button>
```
"""

    examples = parse_examples(markdown)

    assert len(examples) == 1
    assert examples[0].title == "Basic Example"
    assert "<Button>" in examples[0].code
    assert examples[0].kind == "usage"


def test_skips_noeditor_fences() -> None:
    """Accessibility / ``noeditor`` fences are not user-runnable examples."""
    markdown = """
## Accessibility Guidelines
```jsx noeditor
import { Foo } from 'styleguide';
```

Basic Example
```jsx
<Button />
```
"""

    examples = parse_examples(markdown)

    assert len(examples) == 1
    assert examples[0].title == "Basic Example"


def test_handles_multiple_examples_with_titles() -> None:
    """Each fence picks up the title line immediately preceding it."""
    markdown = """
First
```jsx
<A />
```

Second
```jsx
<B />
```
"""

    examples = parse_examples(markdown)

    assert [e.title for e in examples] == ["First", "Second"]
    assert "<A" in examples[0].code
    assert "<B" in examples[1].code


def test_dont_section_classifies_as_anti_pattern() -> None:
    """Examples under a 'Don't' heading are tagged ``anti-pattern``."""
    markdown = """
## Don't do this
```jsx
<Button kind="bad" />
```
"""

    examples = parse_examples(markdown)

    assert examples[0].kind == "anti-pattern"


def test_summary_returns_first_paragraph() -> None:
    """Summary is the leading prose, stripped of headings/fences."""
    markdown = """
A reusable Button component used to trigger primary actions.

## API
```jsx
<Button />
```
"""

    summary = parse_summary(markdown)

    assert summary.startswith("A reusable Button component")
    assert "API" not in summary
    assert "```" not in summary


def test_summary_returns_empty_when_starts_with_code() -> None:
    """No leading prose => empty summary."""
    markdown = """
```jsx
<Button />
```
"""

    assert parse_summary(markdown) == ""


def test_empty_input_yields_no_examples() -> None:
    """Empty markdown is handled gracefully."""
    assert parse_examples("") == []
    assert parse_summary("") == ""
