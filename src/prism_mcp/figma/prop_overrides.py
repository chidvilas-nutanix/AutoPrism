"""Curated Figma-axis -> Prism-prop residue tables (roadmap P3 Part B).

The deterministic resolver (`figma/props.py`) bridges most Figma variant
properties to Prism props automatically:

* **value -> enum/union**: a Figma value like ``Primary`` matches
  ``ButtonTypes.PRIMARY``'s string value ``"primary"``; ``Square``
  matches the ``appearance`` literal ``"square"``.
* **name -> prop**: a Figma axis ``Disabled`` matches the ``disabled``
  boolean prop by (normalized) name.

What's left is the *residue*: axes whose **name** does not match a prop
and whose **value** is not in any enum/union value set — e.g. Figma
``Weight`` -> Prism ``type``, or design-only axes that have no prop at
all. Those need human knowledge, curated here, scoped per v2 family.

Keep these tables small and intentional. Prefer growing the
deterministic resolver over piling up overrides; only add an entry when
the bridge genuinely cannot infer it. Axis keys are matched on the
*normalized* axis name (lowercased, separators stripped) so ``"Full
Width"`` and ``fullWidth`` collide correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FamilyOverrides:
    """Curated overrides for one v2 family.

    Args:
        axis_to_prop (dict[str, str]): normalized Figma axis name -> Prism
            prop name. Used when neither name- nor value-matching fired.
            The resolver still classifies the *value* against the target
            prop's schema (enum member / union literal / boolean).
        text_axis_to_prop (dict[str, str]): normalized TEXT axis name ->
            Prism prop name. Defaults to ``children`` when unspecified.
        ignore_axes (frozenset[str]): normalized axis names that are
            design-only scaffolding with no prop equivalent (suppressed
            from the unresolved list so the metric is not penalised).
    """

    axis_to_prop: dict[str, str] = field(default_factory=dict)
    text_axis_to_prop: dict[str, str] = field(default_factory=dict)
    ignore_axes: frozenset[str] = field(default_factory=frozenset)


# Design-surface axes that exist on virtually every spec-library
# component and never correspond to a Prism prop — they place the
# instance on a doc canvas ("Placed On: Base") or pick a doc theme
# ("Mode: Light"). Excluded everywhere so they do not masquerade as
# unresolved props. Verified against the v2 prop lists: no component
# exposes a `placedOn` / `placeOn` / `mode` prop.
GLOBAL_IGNORE_AXES: frozenset[str] = frozenset(
    {"placedon", "placeon", "mode"}
)


# Keyed by v2 family (the directory name the P2 catalog resolves to).
# Normalized axis names: lowercase, all punctuation removed (see
# ``props._norm_name``). Each ``ignore`` entry was checked against the
# component's real prop list (``scripts/build_prop_schema.py`` output) —
# these Figma axes describe a *design-system visual variant* with no
# Prism prop equivalent, so counting them as misses would understate
# real coverage.
FAMILY_OVERRIDES: dict[str, FamilyOverrides] = {
    "Button": FamilyOverrides(
        # Figma models Button's style axis as "Weight" (Primary /
        # Secondary / …); the Prism prop is `type` (ButtonTypes). Most
        # values also value-match `type` directly, so this is mainly a
        # safety net for values that do not.
        axis_to_prop={"weight": "type"},
        # ButtonProps has only {type, appearance, disabled, fullWidth,
        # textButtonSize, children, ...} — no icon/size/underline/dotted
        # prop; icons are passed as children, so the icon-* axes are
        # design-only and the swapped icon is its own child region.
        ignore_axes=frozenset(
            {
                "nav",
                "icon",
                "showbuttonicon",
                "underline",
                "dotted",
                "size",
            }
        ),
    ),
    "Badge": FamilyOverrides(
        # BadgeProps has {align, appearance, color, type, textType, ...}.
        # "State"/"Selection" (Info/Warning) map to `color` only via a
        # semantic value map (Info -> blue) the resolver does not model —
        # emitting color="Info" would be wrong, so leave them out rather
        # than curate badly. "Icon" toggles a statusIcon node (design).
        ignore_axes=frozenset(
            {"nav", "icon", "underline", "state", "selection"}
        ),
    ),
    "Input": FamilyOverrides(
        # InputProps is configured by value/disabled/error/search/addons,
        # NOT by the design-layout axes Figma carries: "State"
        # (Empty/Filled is driven by `value`), label *position*
        # ("Label: Top"), and doc-surface axes. None are props.
        ignore_axes=frozenset(
            {
                "state",
                "label",
                "labels",
                "additionaltext",
                "showiconcontrolsquestion",
            }
        ),
    ),
    "Typography": FamilyOverrides(
        # Paragraph/heading text is configured by children + `type`;
        # the Figma "State"/"Label" axes are design-layout scaffolding.
        ignore_axes=frozenset({"state", "label"}),
    ),
}
"""Per-family curated residue. Start small; the deterministic resolver
covers the majority. Extend deliberately, with a worklog note."""


_EMPTY = FamilyOverrides()


def overrides_for(family: str | None) -> FamilyOverrides:
    """Return the curated overrides for ``family`` (empty when none)."""
    if not family:
        return _EMPTY
    return FAMILY_OVERRIDES.get(family, _EMPTY)


def is_ignored_axis(family: str | None, axis_key: str) -> bool:
    """``True`` when ``axis_key`` is a design-only axis with no Prism prop.

    Args:
        family (str | None): the routed family.
        axis_key (str): the *normalized* Figma axis name.
    """
    if axis_key in GLOBAL_IGNORE_AXES:
        return True
    return axis_key in overrides_for(family).ignore_axes
