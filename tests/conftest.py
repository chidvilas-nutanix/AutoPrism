"""Shared pytest fixtures.

Centralizes the synthetic-tarball builder and the registry-metadata
factory so each test reads as a behavior assertion rather than a setup
recipe. Keeping them here also means the on-disk shapes match the real
``@nutanix-ui/prism-reactjs`` tarball in exactly one place.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _strip_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove JFROG credentials from the test process environment.

    PRD section 9 (Security) says we never echo credentials. Tests run
    on developer laptops that may have ``JFROG_AUTH`` set in their
    shell; stripping the env makes the suite hermetic and avoids
    accidental network calls if a test forgets to mock the registry.
    """
    for key in ("JFROG_AUTH", "JFROG_EMAIL", "JFROG_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a temp cache dir and point the env var at it.

    Args:
        tmp_path (Path): per-test tmp dir.
        monkeypatch (pytest.MonkeyPatch): env patcher.

    Returns:
        Path: writable cache root.
    """
    root = tmp_path / "prism-cache"
    monkeypatch.setenv("PRISM_MCP_CACHE_DIR", str(root))
    return root


def build_tarball(files: Mapping[str, bytes | str]) -> bytes:
    """Build a gzipped tar of ``files`` rooted at ``package/``.

    Each key is a path **inside** ``package/`` (e.g.
    ``"package.json"`` or ``"lib/index.d.ts"``); each value is the
    file body as bytes or str.

    Args:
        files (Mapping[str, bytes | str]): in-tarball path -> body.

    Returns:
        bytes: gzipped tar bytes ready for ``Cache.install_tarball``.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for rel, body in files.items():
            payload = body.encode("utf-8") if isinstance(body, str) else body
            info = tarfile.TarInfo(name=f"package/{rel}")
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def make_latest_manifest(
    package_name: str,
    version: str,
    tarball_url: str,
    tarball_bytes: bytes,
) -> dict[str, Any]:
    """Return a per-version manifest as the registry's ``/latest`` returns it.

    Mirrors the real fields :meth:`Library._acquire_online` reads. We
    include both ``integrity`` (sha512 SRI) and ``shasum`` (sha1 hex)
    so tests can exercise either verification path.

    Args:
        package_name (str): scoped package name.
        version (str): semver of the published manifest.
        tarball_url (str): URL the manifest will point ``dist.tarball``
            at.
        tarball_bytes (bytes): payload used to compute the digests.

    Returns:
        dict: a manifest body matching ``GET <base>/<pkg>/latest``.
    """
    sha512 = base64.b64encode(hashlib.sha512(tarball_bytes).digest()).decode(
        "ascii"
    )
    sha1 = hashlib.sha1(tarball_bytes, usedforsecurity=False).hexdigest()
    return {
        "name": package_name,
        "version": version,
        "dist": {
            "tarball": tarball_url,
            "integrity": f"sha512-{sha512}",
            "shasum": sha1,
        },
    }


def make_prism_tarball(
    *,
    package_name: str = "@nutanix-ui/prism-reactjs",
    version: str = "2.54.0",
    components: Iterable[str] = ("Button", "Modal"),
    hooks: Iterable[str] = ("useFocusTrap",),
    managers: Iterable[str] = ("I18nManager",),
    utils: Iterable[str] = ("A11yUtils",),
    include_tokens: bool = True,
    extra_files: Mapping[str, str] | None = None,
) -> bytes:
    """Build a synthetic prism-reactjs tarball with real-shaped paths.

    Layout matches what the real publish pipeline produces:

    * ``package.json`` at the root.
    * ``lib/components/v2/<X>/<X>.d.ts`` (tsc declarations).
    * ``src/components/v2/<X>/<X>.examples.md`` (example sections).
    * ``lib/hooks/<use*>.d.ts`` (one arrow-const export each).
    * ``lib/managers/<X>Manager.d.ts`` (singleton + class).
    * ``lib/utils/<X>.d.ts`` (a mix of arrow exports).
    * ``src/styles/v2/Colors.less`` etc. (design tokens).

    Args:
        package_name (str): npm scoped name.
        version (str): semver to put in ``package.json``.
        components (Iterable[str]): component names to generate.
        hooks (Iterable[str]): hook names (must start with ``use``).
        managers (Iterable[str]): manager names (must end with
            ``Manager``).
        utils (Iterable[str]): util module stems.
        include_tokens (bool): include the styles tree.
        extra_files (Mapping[str, str] | None): additional in-package
            files keyed by path under ``package/``.

    Returns:
        bytes: gzipped tarball bytes.
    """
    files: dict[str, bytes | str] = {
        "package.json": json.dumps(
            {
                "name": package_name,
                "version": version,
                "main": "lib/index.js",
                "types": "lib/index.d.ts",
            }
        ),
    }

    if include_tokens:
        files.update(_make_styles_files())

    for name in components:
        files[f"lib/components/v2/{name}/{name}.d.ts"] = _make_component_dts(
            name
        )
        files[f"src/components/v2/{name}/{name}.examples.md"] = (
            _make_component_examples(name)
        )

    for hook in hooks:
        files[f"lib/hooks/{hook}.d.ts"] = _make_hook_dts(hook)

    for manager in managers:
        files[f"lib/managers/{manager}.d.ts"] = _make_manager_dts(manager)

    for util in utils:
        files[f"lib/utils/{util}.d.ts"] = _make_util_dts(util)

    if extra_files:
        for path, body in extra_files.items():
            files[path] = body

    return build_tarball(files)


def _make_component_dts(name: str) -> str:
    """Return a synthetic ``X.d.ts`` for ``name`` resembling tsc output."""
    return (
        f"import * as React from 'react';\n"
        f"\n"
        f"export interface {name}Props {{\n"
        f"    /** Customize additional class name. */\n"
        f"    className?: string;\n"
        f"    /** Disable the {name.lower()}'s events and state. */\n"
        f"    disabled?: boolean;\n"
        f"    /** Click handler. */\n"
        f"    onClick?: (event: React.MouseEvent) => void;\n"
        f"    /** Children rendered inside the {name.lower()}. */\n"
        f"    children?: React.ReactNode;\n"
        f"}}\n"
        f"\n"
        f"export declare const {name}: React.FC<{name}Props>;\n"
    )


def _make_component_examples(name: str) -> str:
    """Return a synthetic ``X.examples.md`` for ``name``."""
    return (
        f"Basic Example\n"
        f"```jsx\n"
        f"import {{ {name} }} from '@nutanix-ui/prism-reactjs';\n"
        f"\n"
        f"<{name} onClick={{handle}}>Hello</{name}>\n"
        f"```\n"
        f"\n"
        f"With Disabled\n"
        f"```jsx\n"
        f"<{name} disabled>Disabled</{name}>\n"
        f"```\n"
    )


def _make_hook_dts(name: str) -> str:
    """Return a synthetic ``<hook>.d.ts`` resembling tsc arrow-const output.

    The shape mirrors the real publish: ``export const X = (...) => ...``
    becomes ``export declare const X: (...) => R;`` in the d.ts.
    """
    return (
        "import * as React from 'react';\n"
        "\n"
        f"export type {name}Options = {{\n"
        "    /** When true, pressing Tab moves focus out of the trap. */\n"
        "    passThrough?: boolean;\n"
        "}};\n"
        "\n"
        "/**\n"
        " * Trap focus inside the container ref while mounted.\n"
        " */\n"
        f"export declare const {name}: ("
        "innerRef: React.RefObject<HTMLDivElement>, "
        f"options?: {name}Options"
        ") => React.RefObject<HTMLDivElement>;\n"
    )


def _make_manager_dts(name: str) -> str:
    """Return a synthetic ``<X>Manager.d.ts`` resembling tsc output."""
    return (
        "/**\n"
        f" * {name} singleton wrapping translations.\n"
        " */\n"
        f"declare class {name} {{\n"
        "    locale: string;\n"
        "    /** Initialize the manager with a mapping. */\n"
        "    initialize(i18nMap: Record<string, string>): void;\n"
        "    /** Translate a key for a module. */\n"
        "    t(moduleName: string, key: string, count?: number): string;\n"
        "    /** Update the current locale. */\n"
        "    setLocale(locale: string): void;\n"
        "}\n"
        f"declare const instance: {name};\n"
        "export default instance;\n"
    )


def _make_util_dts(stem: str) -> str:
    """Return a synthetic util d.ts (a couple of small arrow exports)."""
    return (
        "/**\n"
        " * Join given segments with '-' separator.\n"
        " */\n"
        "export declare const buildComponentId: "
        "(segments: string[]) => string;\n"
        "/**\n"
        " * Returns true when the given key starts with ``aria-``.\n"
        " */\n"
        "export declare const isAriaString: (value: string) => boolean;\n"
        f"// {stem} module marker, ignored by the parser\n"
    )


def _make_styles_files() -> dict[str, str]:
    """Return the small ``src/styles/v2/`` tree shared across slices."""
    return {
        "src/styles/v2/Colors.less": (
            "//\n"
            "// Color tokens for tests.\n"
            "//\n"
            "@color-primary: #1B6BCC;\n"
            "@color-secondary: #627386;\n"
            "@color-success: #5DBA00;\n"
            "/* multiline\n"
            "   comment with @color-bogus: #000; should not be parsed */\n"
        ),
        "src/styles/v2/Typography.less": (
            "@font-family-base: 'Inter', sans-serif;\n"
            "@font-size-md: 14px;\n"
            "@font-weight-bold: 700;\n"
        ),
        "src/styles/v2/Z-Index.less": ("@z-modal: 1000;\n@z-toast: 1200;\n"),
        "src/styles/v2/Animation.less": (
            "@animation-duration-fast: 150ms;\n"
            "@animation-easing-standard: cubic-bezier(0.4, 0, 0.2, 1);\n"
        ),
        "src/styles/v2/Focus.less": (
            "@focus-ring-color: @color-primary;\n@focus-ring-width: 2px;\n"
        ),
    }


@pytest.fixture()
def prism_tarball() -> bytes:
    """Return a synthetic Prism tarball usable by every slice."""
    return make_prism_tarball()


@pytest.fixture(autouse=True)
def _no_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``Path.home()`` to a per-test directory.

    Belt-and-suspenders: if a test forgets ``cache_root``, the default
    cache path still lands somewhere disposable instead of polluting
    ``~/.cache/prism-mcp`` on the developer's actual machine.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    if os.name == "nt":  # pragma: no cover - non-Windows CI
        monkeypatch.setenv("USERPROFILE", str(fake_home))
