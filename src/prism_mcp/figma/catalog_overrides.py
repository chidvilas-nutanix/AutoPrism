"""Curated resolution tables for the Figma → Prism component catalog (P2).

This module is the **human-editable curation surface** for the catalog
builder in :mod:`prism_mcp.figma.catalog`. It holds four things, in
descending order of trust:

1. :data:`KEY_OVERRIDES` — explicit ``componentKey -> Prism component``
   pins for the handful of cases the cascade gets wrong. Highest trust;
   normally empty.
2. :data:`STYLEGUIDE_SLUG_TO_PRISM` — the ``#/Components/...?id=<slug>``
   styleguide-URL slug → Prism component. The Design Library (``bK52``)
   carries these URLs in ~72% of component descriptions, so this is the
   primary deterministic signal.
3. :data:`DS_SLUG_TO_PRISM` — the ``ds.nutanix.design/components/<slug>``
   slug → Prism component (the newer doc host).
4. :data:`FAMILY_NAME_TO_PRISM` — normalized slash-taxonomy *family*
   (e.g. ``Action/...`` → ``action``) → Prism component. The only signal
   the four URL-less libraries (Templates / Viz / Spec Doc / Color
   Primitives) and detached instances have.

Every *target* is validated against :data:`PRISM_V2_COMPONENTS` (the 38
``src/components/v2/*`` dirs that the rplib actually ships) at catalog
build time, so a typo'd or stale target fails the build rather than
silently producing an unrenderable spec.

These tables were seeded from the validated audit resolver
(``docs/_audit_data/analyze_xray2.py``) — which scored 93% map / 82%
exact-key across 65,327 real X-Ray instances — then extended to cover
the *complete* slug + family inventory measured across all five
publishing libraries (see ``improvements/03-phase2-catalog.md`` §2).

A value of ``None`` means "known design-system family with **no**
prism-react equivalent" (brand art, illustrations, pure scaffolding) —
distinct from a family we have simply never seen. The builder records
the former as ``method="family-unsupported"`` so coverage math can
separate "intentionally unsupported" from "genuinely unmapped".
"""

from __future__ import annotations

PRISM_V2_COMPONENTS: frozenset[str] = frozenset(
    {
        "Accordion",
        "Alert",
        "Badge",
        "Button",
        "Calendar",
        "Carousel",
        "Checkbox",
        "CodeInput",
        "Dashboard",
        "Divider",
        "Dropdown",
        "Form",
        "Icons",
        "Input",
        "Layouts",
        "List",
        "Loader",
        "MenuController",
        "Modal",
        "Navigation",
        "Notification",
        "Overlay",
        "Popover",
        "Progress",
        "Radio",
        "Scrollbar",
        "Select",
        "Separator",
        "Slider",
        "Sorter",
        "Tables",
        "TextArea",
        "TimePicker",
        "Tooltip",
        "TreeView",
        "Tutorial",
        "Typography",
        "Utility",
    }
)
"""The 38 canonical prism-react v2 component names (the ``src/
components/v2/*`` directory names of ``@nutanix-ui/prism-reactjs``).

This is the catalog's *target vocabulary*: every non-empty
``prism_component`` the builder emits must be a member, asserted at
build time and re-validated in tests against the live rplib entity
index. Sub-component / prop-level names (``TableColumn``, ``Icon``,
``ButtonGroup`` — see ``figma_mapping.PATTERN_TO_PRIMARY``) are a P3
concern; P2 resolves to the component *family*."""


STYLEGUIDE_SLUG_TO_PRISM: dict[str, str] = {
    "alert": "Alert",
    "badge": "Badge",
    "button": "Button",
    "buttongroup": "Button",
    "calendar": "Calendar",
    "datepicker": "Calendar",
    "carousel": "Carousel",
    "checkbox": "Checkbox",
    "dropdown": "Dropdown",
    "favorite": "Utility",
    "fileinput": "Input",
    "input": "Input",
    "inputnumber": "Input",
    "multiinput": "Input",
    "multiselectinput": "Select",
    "select": "Select",
    "selectdropdown": "Select",
    "orderedlist": "List",
    "unorderedlist": "List",
    "progress": "Progress",
    "radio": "Radio",
    "scrollbar": "Scrollbar",
    "slider": "Slider",
    "sorter": "Sorter",
    "table-1": "Tables",
    "textarea": "TextArea",
    "timepicker": "TimePicker",
    "title": "Typography",
    "tooltip": "Tooltip",
}
"""``#/Components/...?id=<slug>`` styleguide slug → Prism component.

Covers every styleguide slug measured in the Design Library except the
``*icon`` slugs (handled generically — any slug ending in ``icon`` →
``Icons`` — see :func:`prism_mcp.figma.catalog.resolve_prism_component`)
and ``nutanixlogo`` (brand art, no prism component → unmapped)."""


DS_SLUG_TO_PRISM: dict[str, str] = {
    "alert-banner": "Alert",
    "anchor-menu": "Navigation",
    "badge": "Badge",
    "buttons": "Button",
    "carousel": "Carousel",
    "checkbox-radio-toggle": "Checkbox",
    "input-field": "Input",
    "link": "Typography",
    "multi-select-input": "Select",
    "notification": "Notification",
    "number-input": "Input",
    "select-input": "Select",
    "slider-input": "Slider",
    "status-indicator": "Badge",
    "tabs": "Navigation",
    "tag": "Badge",
}
"""``ds.nutanix.design/components/<slug>`` slug → Prism component."""


FAMILY_NAME_TO_PRISM: dict[str, str | None] = {
    "accordion": "Accordion",
    "action": "Button",
    "alert banner": "Alert",
    "alert banners": "Alert",
    "app switcher": "Navigation",
    "badge": "Badge",
    "breadcrumb": "Navigation",
    "brand": None,
    "button": "Button",
    "card": "Layouts",
    "carousel": "Carousel",
    "cover page": None,
    "domain switcher": "Navigation",
    "dropdown": "Dropdown",
    "favorite": "Utility",
    "heading": "Typography",
    "icon": "Icons",
    "icons": "Icons",
    "illustration": None,
    "input": "Input",
    "label": "Typography",
    "legend": None,
    "list": "List",
    "loader": "Loader",
    "misc": None,
    "modal": "Modal",
    "nav menu row": "Navigation",
    "nav menu row label": "Navigation",
    "nav search": "Navigation",
    "navigation": "Navigation",
    "notification": "Notification",
    "popover": "Popover",
    "resultstatus": "Progress",
    "selection & control": "Checkbox",
    "separator": "Separator",
    "side panel": "Layouts",
    "status": "Badge",
    "structure": None,
    "table": "Tables",
    "tooltip": "Tooltip",
    "tutorial": "Tutorial",
    "widget": "Dashboard",
}
"""Normalized slash-taxonomy family → Prism component (or ``None`` when
the family has no prism-react equivalent).

The family is the first slash segment of the *logical* name (the
component-set name when the instance belongs to a set), lower-cased and
stripped of emoji status markers (✅/⏳/🛑), trailing parentheticals
(``(slot)``, ``(detach asset)``), and ``_`` scaffolding prefixes — see
:func:`prism_mcp.figma.catalog.normalize_family`. This is the lowest-
trust tier; it is the *only* signal the four URL-less libraries have."""


KEY_OVERRIDES: dict[str, str] = {}
"""Explicit ``componentKey -> Prism component`` pins (highest trust).

Use only for keys the cascade resolves wrong and that curation cannot
fix more generally via the slug/family tables. Each entry should carry a
one-line comment with the Figma component name and the reason. Empty by
design — every addition is a small admission the general rules missed a
case, so prefer fixing the tables above when the miss is systematic."""
