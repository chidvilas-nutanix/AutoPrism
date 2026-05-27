"""Tests for slice-12 library-asset readers.

The asset readers (`find_pwspec_example`, `find_snapshot_template`)
look up the *source-tree* files that don't ship in the published
npm tarball: ``.pwspec.ts`` and ``__snapshots__/*.spec.tsx.snap``.
These tests stand up a fake ``services_root`` with the v2 group
layout the production source repo uses, then verify the readers
glob to the right files and apply the right truncation policy.
"""

from __future__ import annotations

from pathlib import Path

from prism_mcp.library_assets import (
    PwspecExample,
    SnapshotTemplate,
    find_pwspec_example,
    find_snapshot_template,
)

# --------------------------------------------------------------------------
# Filesystem fixture helpers.
# --------------------------------------------------------------------------


def _write_pwspec(
    services_root: Path,
    group: str,
    component_name: str,
    body: str,
) -> Path:
    """Materialise ``services/src/components/v2/<group>/<Name>.pwspec.ts``."""
    path = (
        services_root
        / "src"
        / "components"
        / "v2"
        / group
        / f"{component_name}.pwspec.ts"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_snapshot(
    services_root: Path,
    group: str,
    component_name: str,
    body: str,
) -> Path:
    """Materialise ``.../<group>/__snapshots__/<Name>.spec.tsx.snap``."""
    path = (
        services_root
        / "src"
        / "components"
        / "v2"
        / group
        / "__snapshots__"
        / f"{component_name}.spec.tsx.snap"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


_SIMPLE_PWSPEC_BODY = """\
import { test, expect } from '@playwright/test';
import { themes, visitPage } from '@nutanix-ui/playwright-util';

test.describe.parallel('Button', () => {
  for (const theme of themes) {
    test(`renders ${theme}`, async ({ page }) => {
      await visitPage(page, 'Button', { theme });
      await expect(page.locator('button')).toBeVisible();
    });
  }
});
"""


_TWO_BLOCK_SNAPSHOT_BODY = """\
// Jest Snapshot v1, https://goo.gl/fbAQLP

exports[`Button renders default state 1`] = `
<button
  className="prism-button prism-button-primary"
  data-test-id="default"
/>
`;

exports[`Button renders disabled 1`] = `
<button
  className="prism-button prism-button-primary"
  data-test-id="disabled"
  disabled={true}
/>
`;
"""


# --------------------------------------------------------------------------
# find_pwspec_example
# --------------------------------------------------------------------------


def test_find_pwspec_example_returns_existing_source_with_advisory(
    tmp_path: Path,
) -> None:
    """Hit: ``found=True``, ``path`` set, ``code`` populated, ``note`` warns."""
    services_root = tmp_path / "services"
    pwspec_path = _write_pwspec(
        services_root, "Button", "Button", _SIMPLE_PWSPEC_BODY
    )

    result = find_pwspec_example(
        services_root=services_root,
        component_name="Button",
    )

    assert isinstance(result, PwspecExample)
    assert result.found is True
    assert result.path == str(pwspec_path)
    assert "test.describe" in (result.code or "")
    # Note must warn the LLM that the helpers won't run in scratch:
    assert "playwright-util" in result.note


def test_find_pwspec_example_returns_miss_with_fallback_note(
    tmp_path: Path,
) -> None:
    """Miss: ``found=False`` with an actionable fallback note."""
    services_root = tmp_path / "services"
    services_root.mkdir()

    result = find_pwspec_example(
        services_root=services_root,
        component_name="GhostComponent",
    )

    assert result.found is False
    assert result.path is None
    assert result.code is None
    assert "from scratch" in result.note


def test_find_pwspec_example_returns_miss_when_services_root_absent(
    tmp_path: Path,
) -> None:
    """Non-existent ``services_root`` is a miss, not a crash.

    The MCP tool surface must never raise on a missing
    services_root — that's the operator's contract violation,
    not a tool error.
    """
    result = find_pwspec_example(
        services_root=tmp_path / "does-not-exist",
        component_name="Button",
    )

    assert result.found is False
    assert result.code is None


def test_find_pwspec_example_globs_across_groups(tmp_path: Path) -> None:
    """The reader doesn't require the caller to know which group folder.

    Prism puts pwspecs under ``Button/``, ``Form/``, ``Tables/``, etc.;
    the LLM only knows the component name (``FormItemDatePicker``)
    and shouldn't have to guess the parent folder.
    """
    services_root = tmp_path / "services"
    _write_pwspec(
        services_root,
        group="Form",  # deliberately not the same as the component name
        component_name="FormItemDatePicker",
        body=_SIMPLE_PWSPEC_BODY,
    )

    result = find_pwspec_example(
        services_root=services_root,
        component_name="FormItemDatePicker",
    )

    assert result.found is True
    assert "Form/FormItemDatePicker.pwspec.ts" in (result.path or "")


def test_find_pwspec_example_truncates_oversized_source(tmp_path: Path) -> None:
    """Pwspecs over the 6 KB cap are truncated with a marker."""
    services_root = tmp_path / "services"
    # 10_000 bytes of valid TypeScript-ish content
    big_body = "// header\n" + ("test('x', () => {});\n" * 1_000)
    _write_pwspec(
        services_root,
        "Button",
        "BigButton",
        big_body,
    )

    result = find_pwspec_example(
        services_root=services_root,
        component_name="BigButton",
    )

    assert result.found is True
    code = result.code or ""
    assert "truncated" in code
    assert len(code.encode("utf-8")) <= 6_200  # cap + marker line


# --------------------------------------------------------------------------
# find_snapshot_template
# --------------------------------------------------------------------------


def test_find_snapshot_template_returns_content_and_counts_blocks(
    tmp_path: Path,
) -> None:
    """Hit: snapshot returned + ``block_count`` reflects ``exports[...]`` count."""
    services_root = tmp_path / "services"
    snap_path = _write_snapshot(
        services_root, "Button", "Button", _TWO_BLOCK_SNAPSHOT_BODY
    )

    result = find_snapshot_template(
        services_root=services_root,
        component_name="Button",
    )

    assert isinstance(result, SnapshotTemplate)
    assert result.found is True
    assert result.path == str(snap_path)
    assert result.block_count == 2
    assert "prism-button" in (result.content or "")
    assert "2 variant block" in result.note


def test_find_snapshot_template_returns_miss_for_unknown_component(
    tmp_path: Path,
) -> None:
    """Miss: ``found=False`` with an actionable fallback note."""
    services_root = tmp_path / "services"
    services_root.mkdir()

    result = find_snapshot_template(
        services_root=services_root,
        component_name="Ghost",
    )

    assert result.found is False
    assert result.content is None
    assert "search_examples" in result.note


def test_find_snapshot_template_truncates_oversized_content(
    tmp_path: Path,
) -> None:
    """Snapshots over the 4 KB cap are truncated with a marker."""
    services_root = tmp_path / "services"
    # ~30 KB of repeated ``exports[...]`` blocks.
    huge = "// header\n" + "\nexports[`X 1`] = `value`;\n" * 1_000
    _write_snapshot(services_root, "Icons", "Icons", huge)

    result = find_snapshot_template(
        services_root=services_root,
        component_name="Icons",
    )

    assert result.found is True
    assert result.block_count == 1_000  # block counter sees the full file
    content = result.content or ""
    assert "truncated" in content
    assert len(content.encode("utf-8")) <= 4_200  # cap + marker line
