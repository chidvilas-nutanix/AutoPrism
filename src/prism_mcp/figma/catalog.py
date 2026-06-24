"""The Figma → Prism component catalog (roadmap P2, the identity keystone).

P1 (``improvements/02-phase1-fetch-fix.md``) made each Figma instance's
global ``componentKey`` *available* on every region. P2 turns that key
into a **deterministic Prism component**: this module builds, ships, and
serves a cached ``componentKey -> {prism_component, ...}`` dictionary
spanning all five publishing libraries.

Two halves:

* **Build-time** (offline, run by ``scripts/build_figma_catalog.py``):
  :func:`build_catalog` consumes the raw ``/v1/files/:key/components`` +
  ``/component_sets`` dumps, resolves each component to a Prism family
  via the curated cascade in :mod:`prism_mcp.figma.catalog_overrides`,
  validates every target against :data:`~catalog_overrides.
  PRISM_V2_COMPONENTS`, and serializes a versioned JSON artifact
  (``data/figma_catalog.json``) that is committed to the repo.
* **Run-time** (no network, no rplib): :class:`FigmaCatalog` loads that
  committed JSON once per process and answers
  :meth:`~FigmaCatalog.lookup` in O(1). This is what P3 routing will
  consult to promote a key hit to the deterministic
  ``primary_recommendation``.

The resolution cascade, in descending trust:

1. ``key-override``    — explicit pin (:data:`~catalog_overrides.KEY_OVERRIDES`).
2. ``styleguide-id``   — ``#/Components/...?id=<slug>`` in the description.
3. ``ds-slug``         — ``ds.nutanix.design/components/<slug>``.
4. ``icon-family``     — name family / styleguide slug denotes an icon.
5. ``family-name``     — normalized slash-taxonomy family.
6. ``family-unsupported`` — family is known to have no prism equivalent.
7. ``unmapped``        — none of the above (genuine Tier-3 fallback).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from prism_mcp.figma.catalog_overrides import (
    DS_SLUG_TO_PRISM,
    FAMILY_NAME_TO_PRISM,
    KEY_OVERRIDES,
    PRISM_V2_COMPONENTS,
    STYLEGUIDE_SLUG_TO_PRISM,
)

CATALOG_SCHEMA_VERSION = 1
"""Bumped when the on-disk ``figma_catalog.json`` shape changes so a
stale artifact is rejected rather than silently mis-parsed."""

DATA_PATH = Path(__file__).parent / "data" / "figma_catalog.json"
"""Default location of the committed catalog artifact."""

ResolutionMethod = Literal[
    "key-override",
    "styleguide-id",
    "ds-slug",
    "icon-family",
    "family-name",
    "family-unsupported",
    "unmapped",
]

CatalogKind = Literal["component", "component_set"]

_CONFIDENCE: dict[ResolutionMethod, float] = {
    "key-override": 1.0,
    "styleguide-id": 1.0,
    "ds-slug": 0.95,
    "icon-family": 0.9,
    "family-name": 0.7,
    "family-unsupported": 0.0,
    "unmapped": 0.0,
}

# ``#/<Section>/Foo?id=bar`` styleguide anchor. Section is usually
# ``Components`` / ``Icons`` but also ``Layouts`` (e.g. Structure?id=
# scrollbar), so we capture the ``id=`` slug under any section.
_STYLEGUIDE_RE = re.compile(r"#/[^?]*\?id=([A-Za-z0-9_-]+)")
# ``ds.nutanix.design/components/<slug>`` newer doc host.
_DS_RE = re.compile(r"ds\.nutanix\.design/components/([A-Za-z0-9_-]+)")
# First http(s) URL in a description (the styleguide / ds link).
_URL_RE = re.compile(r"https?://\S+")
# Emoji + status markers Figma names carry (✅ ⏳ 🛑 arrows, etc.).
_DECORATION_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\u2190-\u21ff"
    "\u2b00-\u2bff\u2300-\u23ff\ufe0f]"
)
# Trailing ``(slot)`` / ``(detach asset)`` qualifiers.
_PAREN_TAIL_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_text(name: str) -> str:
    """Strip emoji/status markers and collapse whitespace in ``name``."""
    out = _DECORATION_RE.sub("", name or "")
    out = out.replace("\u00a0", " ")
    return re.sub(r"\s{2,}", " ", out).strip()


def normalize_family(logical_name: str) -> str:
    """Return the normalized slash-taxonomy *family* of ``logical_name``.

    The family is the first ``/``-segment, cleaned of emoji status
    markers, trailing parentheticals, and leading ``_`` scaffolding,
    then lower-cased — e.g. ``"Action/ ✅ Button"`` → ``"action"``,
    ``"⏳ Accordion (detach asset)"`` → ``"accordion"``. This is the key
    into :data:`~catalog_overrides.FAMILY_NAME_TO_PRISM`.
    """
    first = (logical_name or "").split("/", 1)[0]
    cleaned = _clean_text(first)
    cleaned = _PAREN_TAIL_RE.sub("", cleaned)
    return cleaned.lstrip("_ ").strip().lower()


def _parse_doc_url(description: str) -> str | None:
    """Return the first ``http(s)`` URL in ``description`` (cleaned)."""
    if not description:
        return None
    match = _URL_RE.search(description)
    if not match:
        return None
    return match.group(0).strip().rstrip(").,;")


@dataclass(frozen=True)
class ResolvedTarget:
    """Outcome of resolving one component to a Prism family.

    Args:
        prism_component (str): canonical v2 name, or ``""`` when the
            component has no prism-react equivalent / is unmapped.
        method (ResolutionMethod): which cascade tier fired.
        styleguide_slug (str | None): the ``?id=`` / ds slug, when the
            match came from a URL (kept for P3 prop resolution).
        doc_url (str | None): the full styleguide / ds URL, when present.
        confidence (float): trust score, keyed off ``method``.
    """

    prism_component: str
    method: ResolutionMethod
    styleguide_slug: str | None
    doc_url: str | None
    confidence: float


def resolve_prism_component(
    logical_name: str,
    description: str,
    *,
    component_key: str | None = None,
) -> ResolvedTarget:
    """Resolve one component to a Prism family via the curated cascade.

    Args:
        logical_name (str): the component's logical name — the
            component-*set* name when it belongs to a variant family,
            else the component's own name.
        description (str): the component's (or its set's) description,
            which on Design Library assets carries the styleguide URL.
        component_key (str | None): the global key, consulted only for
            :data:`~catalog_overrides.KEY_OVERRIDES`.

    Returns:
        ResolvedTarget: the resolved family + provenance. Never raises;
        an unresolvable input yields ``method="unmapped"`` and an empty
        ``prism_component``.
    """
    desc = description or ""
    doc_url = _parse_doc_url(desc)

    # 1. Explicit key override — highest trust.
    if component_key and component_key in KEY_OVERRIDES:
        return ResolvedTarget(
            KEY_OVERRIDES[component_key], "key-override", None, doc_url, 1.0
        )

    # 2. Styleguide slug (#/Components|Icons/...?id=).
    m = _STYLEGUIDE_RE.search(desc)
    if m:
        slug = m.group(1).lower()
        if slug.endswith("icon"):
            return ResolvedTarget(
                "Icons", "icon-family", slug, doc_url, _CONFIDENCE["icon-family"]
            )
        target = STYLEGUIDE_SLUG_TO_PRISM.get(slug)
        if target:
            return ResolvedTarget(
                target, "styleguide-id", slug, doc_url,
                _CONFIDENCE["styleguide-id"],
            )

    # 3. ds.nutanix.design slug.
    m = _DS_RE.search(desc)
    if m:
        slug = m.group(1).lower()
        target = DS_SLUG_TO_PRISM.get(slug)
        if target:
            return ResolvedTarget(
                target, "ds-slug", slug, doc_url, _CONFIDENCE["ds-slug"]
            )

    # 4-6. Name-based: icon family, then curated family map.
    family = normalize_family(logical_name)
    if family.startswith("icon"):
        return ResolvedTarget(
            "Icons", "icon-family", None, doc_url, _CONFIDENCE["icon-family"]
        )
    if family in FAMILY_NAME_TO_PRISM:
        target = FAMILY_NAME_TO_PRISM[family]
        if target is None:
            return ResolvedTarget("", "family-unsupported", None, doc_url, 0.0)
        return ResolvedTarget(
            target, "family-name", None, doc_url, _CONFIDENCE["family-name"]
        )

    # 7. Genuinely unmapped — Tier-3 fuzzy fallback territory.
    return ResolvedTarget("", "unmapped", None, doc_url, 0.0)


class CatalogEntry(BaseModel):
    """One resolved ``componentKey -> Prism`` catalog row.

    Args:
        component_key (str): the global Figma key — the deterministic
            join an instance's ``componentId`` resolves to via the
            ``/nodes`` ``components`` map (P1).
        prism_component (str): canonical v2 component family, or ``""``
            when unmapped / unsupported. Always a member of
            :data:`~catalog_overrides.PRISM_V2_COMPONENTS` when non-empty.
        kind (CatalogKind): ``"component"`` (a variant / standalone) or
            ``"component_set"`` (a variant family).
        method (ResolutionMethod): which cascade tier resolved it.
        confidence (float): trust score for ``method``.
        figma_name (str): the logical name used for resolution.
        figma_family (str): the normalized taxonomy family.
        library_key (str): publishing library file key.
        library_name (str): human-readable library name.
        styleguide_slug (str | None): the ``?id=`` / ds slug, if any.
        doc_url (str | None): styleguide / ds URL, if any.
        component_set_key (str | None): owning set's key, for variant
            components — lets P3 join props at the set level.
        node_id (str): source node id in the publishing file (trace).
    """

    model_config = ConfigDict(extra="forbid")

    component_key: str
    prism_component: str = ""
    kind: CatalogKind
    method: ResolutionMethod
    confidence: float
    figma_name: str
    figma_family: str = ""
    library_key: str
    library_name: str
    styleguide_slug: str | None = None
    doc_url: str | None = None
    component_set_key: str | None = None
    node_id: str = ""

    @property
    def is_mapped(self) -> bool:
        """``True`` when this key resolves to a real Prism component."""
        return bool(self.prism_component)


class RegionResolution(BaseModel):
    """A runtime identity resolution for one Figma region.

    The unified P2 output that P3 routing consumes. Produced by
    :meth:`FigmaCatalog.resolve_region`, which tries the precomputed
    catalog first (fast, authoritative for the ingested libraries) and
    falls back to running the cascade on the *page-provided* name +
    description (the P1 ``figma_component`` identity). The fallback is
    what covers remote components published by libraries the catalog has
    not ingested — they carry the same styleguide URL, so the cascade
    yields the same deterministic answer.

    Args:
        prism_component (str): canonical v2 family, or ``""`` if unresolved.
        method (ResolutionMethod): which cascade tier fired.
        confidence (float): trust score for ``method``.
        source (str): ``"catalog"`` (precomputed key hit),
            ``"page-fallback"`` (cascade on page identity), or ``"none"``.
        component_key (str): the global key that was resolved.
        component_set_key (str | None): owning set key, when known.
        styleguide_slug (str | None): the ``?id=`` / ds slug, if any.
        doc_url (str | None): styleguide / ds URL, if any.
    """

    model_config = ConfigDict(extra="forbid")

    prism_component: str = ""
    method: ResolutionMethod = "unmapped"
    confidence: float = 0.0
    source: Literal["catalog", "page-fallback", "none"] = "none"
    component_key: str = ""
    component_set_key: str | None = None
    styleguide_slug: str | None = None
    doc_url: str | None = None

    @property
    def is_mapped(self) -> bool:
        """``True`` when this region resolved to a real Prism component."""
        return bool(self.prism_component)


@dataclass(frozen=True)
class LibraryDump:
    """Raw ``/components`` + ``/component_sets`` payload for one library.

    Args:
        key (str): the file key.
        name (str): human-readable library name.
        components (list[dict]): the ``meta.components`` array.
        component_sets (list[dict]): the ``meta.component_sets`` array.
    """

    key: str
    name: str
    components: list[dict[str, Any]]
    component_sets: list[dict[str, Any]]


def _state_group_node_id(component: dict[str, Any]) -> str | None:
    """Return the owning component-set node id for a variant component."""
    frame = component.get("containing_frame") or {}
    group = frame.get("containingStateGroup") or frame.get(
        "containingComponentSet"
    )
    return (group or {}).get("nodeId")


def build_catalog_entries(
    libraries: list[LibraryDump],
) -> dict[str, CatalogEntry]:
    """Resolve every component + set across ``libraries`` into entries.

    For each library we index its component-sets by node id, emit one
    entry per set (keyed by the set key), then one entry per component
    (keyed by the component key). A variant component inherits its set's
    logical name + description (mirroring the runtime resolution in
    ``figma/walker.py::_resolve_figma_identity``), so ``"Darkmode=True"``
    resolves through ``"Illustration"`` rather than failing.

    Args:
        libraries (list[LibraryDump]): the five publishing libraries.

    Returns:
        dict[str, CatalogEntry]: keyed by component / set key, sorted for
        deterministic serialization. Later libraries win on the (rare)
        cross-library key collision.
    """
    entries: dict[str, CatalogEntry] = {}

    for lib in libraries:
        sets_by_node: dict[str, dict[str, Any]] = {
            s["node_id"]: s for s in lib.component_sets if s.get("node_id")
        }

        for s in lib.component_sets:
            key = s.get("key")
            if not key:
                continue
            name = s.get("name", "")
            desc = s.get("description", "")
            res = resolve_prism_component(name, desc, component_key=key)
            entries[key] = CatalogEntry(
                component_key=key,
                prism_component=res.prism_component,
                kind="component_set",
                method=res.method,
                confidence=res.confidence,
                figma_name=name,
                figma_family=normalize_family(name),
                library_key=lib.key,
                library_name=lib.name,
                styleguide_slug=res.styleguide_slug,
                doc_url=res.doc_url,
                node_id=s.get("node_id", ""),
            )

        for c in lib.components:
            key = c.get("key")
            if not key:
                continue
            set_node = _state_group_node_id(c)
            set_entry = sets_by_node.get(set_node) if set_node else None
            logical = (set_entry or {}).get("name") or c.get("name", "")
            desc = c.get("description") or (set_entry or {}).get(
                "description", ""
            )
            res = resolve_prism_component(logical, desc, component_key=key)
            entries[key] = CatalogEntry(
                component_key=key,
                prism_component=res.prism_component,
                kind="component",
                method=res.method,
                confidence=res.confidence,
                figma_name=logical,
                figma_family=normalize_family(logical),
                library_key=lib.key,
                library_name=lib.name,
                styleguide_slug=res.styleguide_slug,
                doc_url=res.doc_url,
                component_set_key=(set_entry or {}).get("key"),
                node_id=c.get("node_id", ""),
            )

    return dict(sorted(entries.items()))


def assert_targets_valid(entries: dict[str, CatalogEntry]) -> None:
    """Raise if any entry resolves to a non-canonical Prism name.

    The build's safety net: a typo'd or stale override target (a name
    not in :data:`~catalog_overrides.PRISM_V2_COMPONENTS`) fails the
    build rather than shipping an unrenderable spec.

    Raises:
        ValueError: listing every offending ``(key, prism_component)``.
    """
    bad = {
        k: e.prism_component
        for k, e in entries.items()
        if e.prism_component and e.prism_component not in PRISM_V2_COMPONENTS
    }
    if bad:
        sample = ", ".join(f"{k}->{v}" for k, v in list(bad.items())[:10])
        raise ValueError(
            f"{len(bad)} catalog entries resolve to a non-canonical Prism "
            f"component (not in PRISM_V2_COMPONENTS): {sample}"
        )


def catalog_stats(entries: dict[str, CatalogEntry]) -> dict[str, Any]:
    """Return coverage counters over ``entries`` for build reporting."""
    by_method: dict[str, int] = {}
    by_prism: dict[str, int] = {}
    by_library: dict[str, int] = {}
    mapped = 0
    for e in entries.values():
        by_method[e.method] = by_method.get(e.method, 0) + 1
        by_library[e.library_name] = by_library.get(e.library_name, 0) + 1
        if e.is_mapped:
            mapped += 1
            by_prism[e.prism_component] = by_prism.get(e.prism_component, 0) + 1
    total = len(entries)
    return {
        "total_entries": total,
        "mapped_entries": mapped,
        "mapped_pct": round(100 * mapped / total, 1) if total else 0.0,
        "by_method": dict(sorted(by_method.items())),
        "by_prism_component": dict(
            sorted(by_prism.items(), key=lambda kv: -kv[1])
        ),
        "by_library": dict(sorted(by_library.items())),
    }


def build_catalog(
    libraries: list[LibraryDump],
    *,
    rplib_version: str = "",
) -> dict[str, Any]:
    """Build the full, serializable catalog artifact from raw dumps.

    Args:
        libraries (list[LibraryDump]): the publishing libraries.
        rplib_version (str): the ``@nutanix-ui/prism-reactjs`` version
            the target vocabulary was validated against (provenance).

    Returns:
        dict: the JSON-serializable artifact (``schema_version``,
        ``generated_at``, ``libraries``, ``stats``, ``entries``).

    Raises:
        ValueError: if any resolved target is non-canonical.
    """
    entries = build_catalog_entries(libraries)
    assert_targets_valid(entries)
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rplib_version": rplib_version,
        "libraries": [
            {
                "key": lib.key,
                "name": lib.name,
                "components": len(lib.components),
                "component_sets": len(lib.component_sets),
            }
            for lib in libraries
        ],
        "stats": catalog_stats(entries),
        "entries": {
            k: e.model_dump(exclude_none=True) for k, e in entries.items()
        },
    }


class FigmaCatalog:
    """In-process, read-only view over the committed catalog artifact.

    Loaded once via :meth:`load` (or the process-wide :func:`get_catalog`
    singleton) and queried with :meth:`lookup`. No network, no rplib
    dependency — pure dict access, safe to call per-instance during a
    page walk.
    """

    def __init__(
        self, entries: dict[str, CatalogEntry], meta: dict[str, Any]
    ) -> None:
        self._entries = entries
        self._meta = meta

    @classmethod
    def from_artifact(cls, artifact: dict[str, Any]) -> FigmaCatalog:
        """Build a catalog from an already-parsed artifact dict."""
        schema = artifact.get("schema_version")
        if schema != CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"catalog schema_version {schema!r} != expected "
                f"{CATALOG_SCHEMA_VERSION}; rebuild data/figma_catalog.json"
            )
        entries = {
            k: CatalogEntry.model_validate(v)
            for k, v in artifact.get("entries", {}).items()
        }
        meta = {k: v for k, v in artifact.items() if k != "entries"}
        return cls(entries, meta)

    @classmethod
    def load(cls, path: Path | None = None) -> FigmaCatalog:
        """Load the catalog from ``path`` (defaults to :data:`DATA_PATH`).

        Raises:
            FileNotFoundError: if the artifact has not been generated.
            ValueError: on a schema mismatch.
        """
        target = path or DATA_PATH
        if not target.is_file():
            raise FileNotFoundError(
                f"figma catalog not found at {target}; generate it with "
                f"`uv run python scripts/build_figma_catalog.py`"
            )
        artifact = json.loads(target.read_text(encoding="utf-8"))
        return cls.from_artifact(artifact)

    def lookup(self, component_key: str | None) -> CatalogEntry | None:
        """Return the entry for ``component_key``, or ``None`` on miss."""
        if not component_key:
            return None
        return self._entries.get(component_key)

    def resolve(self, component_key: str | None) -> CatalogEntry | None:
        """Return the entry only if it maps to a real Prism component."""
        entry = self.lookup(component_key)
        return entry if entry and entry.is_mapped else None

    def resolve_region(
        self,
        *,
        component_key: str | None,
        figma_name: str = "",
        description: str = "",
        component_set_key: str | None = None,
    ) -> RegionResolution:
        """Resolve a region's Prism identity (catalog first, then cascade).

        The single entrypoint P3 routing calls with a region's P1
        :class:`~prism_mcp.figma.models.FigmaComponentIdentity`. Precedence:

        1. a precomputed catalog key hit that maps → ``source="catalog"``;
        2. else the cascade on the page-provided ``figma_name`` +
           ``description`` → ``source="page-fallback"`` (covers remote /
           un-ingested library components, which still carry their
           styleguide URL on the page's ``componentSets`` map);
        3. else the catalog's unmapped record (if the key was known) or an
           empty ``source="none"`` resolution.

        Args:
            component_key (str | None): the instance's global key.
            figma_name (str): the logical name (set name preferred) from
                the page identity.
            description (str): the component / set description from the
                page identity (carries the styleguide URL).
            component_set_key (str | None): owning set key, if known.

        Returns:
            RegionResolution: the resolved identity; never raises.
        """
        entry = self.lookup(component_key)
        if entry and entry.is_mapped:
            return RegionResolution(
                prism_component=entry.prism_component,
                method=entry.method,
                confidence=entry.confidence,
                source="catalog",
                component_key=component_key or "",
                component_set_key=entry.component_set_key or component_set_key,
                styleguide_slug=entry.styleguide_slug,
                doc_url=entry.doc_url,
            )

        cascade = resolve_prism_component(
            figma_name, description, component_key=component_key
        )
        if cascade.prism_component:
            return RegionResolution(
                prism_component=cascade.prism_component,
                method=cascade.method,
                confidence=cascade.confidence,
                source="page-fallback",
                component_key=component_key or "",
                component_set_key=component_set_key,
                styleguide_slug=cascade.styleguide_slug,
                doc_url=cascade.doc_url,
            )

        if entry is not None:
            return RegionResolution(
                method=entry.method,
                source="catalog",
                component_key=component_key or "",
                component_set_key=entry.component_set_key or component_set_key,
                doc_url=entry.doc_url,
            )
        return RegionResolution(
            method=cascade.method,
            source="none",
            component_key=component_key or "",
            component_set_key=component_set_key,
            doc_url=cascade.doc_url,
        )

    @property
    def meta(self) -> dict[str, Any]:
        """The artifact metadata (schema, libraries, stats, …)."""
        return dict(self._meta)

    def __len__(self) -> int:
        return len(self._entries)


@lru_cache(maxsize=1)
def get_catalog() -> FigmaCatalog:
    """Return the process-wide catalog singleton (lazy, cached)."""
    return FigmaCatalog.load()
