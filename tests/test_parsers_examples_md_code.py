"""Tests for the code-body extractor over ``X.examples.md`` files.

The existing ``parsers/examples_md.py`` only captures titles + code
strings into :class:`Entity.examples`. Slice 9 needs richer per-block
metadata (imports, ``@example-id`` markers, anti-pattern + a11y
classification) to feed an embedding index, so we add a sibling parser
that returns :class:`ExampleChunk`s.

These tests pin the contract before any consumer (embeddings, related
components) is written.
"""

from __future__ import annotations

from pathlib import Path

from prism_mcp.parsers.examples_md_code import (
    ExampleChunk,
    parse_example_code_blocks,
    walk_example_chunks,
)


def test_basic_jsx_block_becomes_one_chunk() -> None:
    """A single ``jsx`` fence under a title produces one chunk."""
    markdown = """
Basic Example
```jsx
import { Button } from '@nutanix-ui/prism-reactjs';

<Button>Hello</Button>
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="Button")

    assert len(chunks) == 1
    chunk = chunks[0]
    assert isinstance(chunk, ExampleChunk)
    assert chunk.component_name == "Button"
    assert chunk.title == "Basic Example"
    assert chunk.imports == ["Button"]
    assert chunk.is_anti_pattern is False
    assert chunk.is_a11y_block is False
    assert "<Button>" in chunk.code


def test_jsx_harmony_fence_is_also_a_chunk() -> None:
    """``jsx harmony`` is the styleguidist runnable tag; treat it like jsx."""
    markdown = """
Harmony Example
```jsx harmony
import { Modal } from '@nutanix-ui/prism-reactjs';

<Modal visible>hi</Modal>
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="Modal")

    assert len(chunks) == 1
    assert chunks[0].imports == ["Modal"]


def test_noeditor_under_accessibility_heading_is_a11y_block() -> None:
    """A noeditor fence under "Accessibility Guidelines" is captured but
    flagged so the embedding filter can drop it while the slice 11
    a11y aggregator can still consume it.
    """
    markdown = """
## Accessibility Guidelines
```jsx harmony noeditor
import { Paragraph } from '@nutanix-ui/prism-reactjs';

<Paragraph>Use aria-label.</Paragraph>
```

Real Example
```jsx
import { Button } from '@nutanix-ui/prism-reactjs';
<Button />
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="Button")

    assert len(chunks) == 2
    a11y, real = chunks
    assert a11y.is_a11y_block is True
    assert a11y.is_anti_pattern is False
    assert real.is_a11y_block is False


def test_noeditor_outside_accessibility_is_just_skipped_metadata() -> None:
    """noeditor outside an Accessibility heading is still skipped from
    embedding (caller filters on the flag) but is not auto-tagged a11y.
    """
    markdown = """
## Setup
```jsx noeditor
// just docs, not runnable
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="X")

    assert len(chunks) == 1
    assert chunks[0].is_a11y_block is False
    assert chunks[0].is_noeditor is True


def test_anti_pattern_under_dont_heading() -> None:
    """Examples under "Don't" / "Anti-pattern" sections are flagged."""
    markdown = """
## Don't do this
```jsx
import { Button } from '@nutanix-ui/prism-reactjs';
<Button kind="bad" />
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="Button")

    assert chunks[0].is_anti_pattern is True


def test_extracts_example_id_marker_from_first_body_line() -> None:
    """``// @example-id <id>`` becomes ``ExampleChunk.example_id``."""
    markdown = """
Presentation
```jsx
// @example-id button-presentation
import { Button } from '@nutanix-ui/prism-reactjs';
<Button />
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="Button")

    assert chunks[0].example_id == "button-presentation"


def test_imports_parse_only_from_prism_reactjs_package() -> None:
    """Imports from ``react`` or other packages must be ignored.

    The embedding model only benefits from knowing the *component*
    palette; ``React``, ``useState``, etc. are noise.
    """
    markdown = """
Multi-import
```jsx
import React, { useState } from 'react';
import { Button, Modal, Tooltip } from '@nutanix-ui/prism-reactjs';
import classnames from 'classnames';
<Button />
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="Modal")

    assert sorted(chunks[0].imports) == ["Button", "Modal", "Tooltip"]


def test_multiline_imports_are_parsed() -> None:
    """The real Button.examples.md uses multi-line import blocks."""
    markdown = """
Multi-line imports
```jsx
import {
  ChevronDownIcon,
  FlexLayout,
  Button,
  Menu,
  MenuItem
} from '@nutanix-ui/prism-reactjs';
<Button />
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="Button")

    assert sorted(chunks[0].imports) == [
        "Button",
        "ChevronDownIcon",
        "FlexLayout",
        "Menu",
        "MenuItem",
    ]


def test_chunk_with_no_prism_imports_keeps_empty_list() -> None:
    """A chunk that only imports React-side stuff yields imports=[]."""
    markdown = """
Misc
```jsx
import React from 'react';
<div>plain</div>
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="X")

    assert chunks[0].imports == []


def test_empty_input_returns_empty_list() -> None:
    """Empty markdown is a no-op."""
    assert parse_example_code_blocks("", component_name="X") == []


def test_title_falls_back_to_section_heading() -> None:
    """When no bare title line precedes the fence, use the section."""
    markdown = """
## Composition

```jsx
import { Button } from '@nutanix-ui/prism-reactjs';
<Button />
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="Button")

    assert chunks[0].title == "Composition"


def test_walk_example_chunks_finds_every_md_file(tmp_path: Path) -> None:
    """``walk_example_chunks`` flattens every component's examples."""
    src = tmp_path / "src" / "components" / "v2"
    (src / "Button").mkdir(parents=True)
    (src / "Modal").mkdir(parents=True)
    (src / "Button" / "Button.examples.md").write_text(
        "First\n"
        "```jsx\n"
        "import { Button } from '@nutanix-ui/prism-reactjs';\n"
        "<Button />\n"
        "```\n",
        encoding="utf-8",
    )
    (src / "Modal" / "Modal.examples.md").write_text(
        "First\n"
        "```jsx\n"
        "import { Modal } from '@nutanix-ui/prism-reactjs';\n"
        "<Modal />\n"
        "```\n",
        encoding="utf-8",
    )

    chunks = walk_example_chunks(tmp_path)

    assert {c.component_name for c in chunks} == {"Button", "Modal"}
    assert all(len(c.imports) == 1 for c in chunks)


def test_walk_example_chunks_missing_dir_returns_empty(tmp_path: Path) -> None:
    """A package without the src tree returns ``[]`` and a warning."""
    assert walk_example_chunks(tmp_path) == []


def test_multiple_blocks_preserve_source_order() -> None:
    """Chunks come back in document order so callers can cite line ranges."""
    markdown = """
First
```jsx
import { A } from '@nutanix-ui/prism-reactjs';
<A />
```

Second
```jsx
import { B } from '@nutanix-ui/prism-reactjs';
<B />
```

Third
```jsx
import { C } from '@nutanix-ui/prism-reactjs';
<C />
```
"""

    chunks = parse_example_code_blocks(markdown, component_name="X")

    assert [c.title for c in chunks] == ["First", "Second", "Third"]
    assert [c.imports for c in chunks] == [["A"], ["B"], ["C"]]
