"""Build the curated FigmaTreeMapping mock for node 752:13805
(Figma-basics file copy of the original 4147:20326 "NCM Drift
Management" page).

The original Figma file (file key ``bIC4hWispNBHWeQ2Chmw7b``) is not
reachable through the Cursor Figma plugin, so the design was duplicated
into a permission-friendly file (``QjBuSKHooZN4GEzA2rJy6P``) and
re-anchored on node ``752:13805``. The mock content (agenda IDs,
bboxes, hex colours, …) is preserved verbatim — the only thing that
changed is the (file_key, node_id) the loader keys on, i.e. the
filename produced by ``main()``.

Output:
    mocks/figma_tree/QjBuSKHooZN4GEzA2rJy6P__752_13805.json
"""

from __future__ import annotations

import json
from pathlib import Path

from prism_mcp.figma.models import (
    BoxStyle,
    DroppedNode,
    FigmaTreeMapping,
    LayoutAnalysis,
    LayoutNode,
    MappedRegion,
)
from prism_mcp.workflow.figma_mapping import (
    CandidateMatch,
    FigmaNodeMapping,
    TokenMapping,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_candidate(
    name: str,
    score: float,
    *,
    why: list[str] | None = None,
    summary: str = "",
    source: str = "both",
    kind: str = "component",
) -> CandidateMatch:
    return CandidateMatch(
        name=name,
        type=kind,
        score=score,
        why_matched=why or [name.lower()],
        summary=summary,
        source=source,
    )


def _mk_mapping(
    *,
    node_name: str,
    primary: str,
    candidates: list[CandidateMatch],
    related: list[str] | None = None,
    a11y_blocks: list[str] | None = None,
    token_mappings: list[TokenMapping] | None = None,
    examples: list[str] | None = None,
    decompositions: list[str] | None = None,
    primary_recommendation: str | None = None,
    primary_recommendation_rationale: str = "",
    primary_recommendation_confidence: float = 0.0,
) -> FigmaNodeMapping:
    return FigmaNodeMapping(
        node_name=node_name,
        suggested_component_name=primary,
        candidates=candidates,
        related=related or [],
        a11y_blocks=a11y_blocks or [],
        token_mappings=token_mappings or [],
        examples=examples or [],
        candidate_decompositions=decompositions or [],
        primary_recommendation=primary_recommendation,
        primary_recommendation_rationale=primary_recommendation_rationale,
        primary_recommendation_confidence=primary_recommendation_confidence,
    )


# ---------------------------------------------------------------------------
# Token table
# ---------------------------------------------------------------------------


TOKENS: dict[str, str] = {
    "#FFFFFF": "color/background/primary",
    "#FAFCFD": "color/background/canvas",
    "#22272E": "color/text/primary",
    "#36454F": "color/text/secondary",
    "#627282": "color/text/tertiary",
    "#C2C8CE": "color/text/inverse-secondary",
    "#DCE1E5": "color/border/interactive",
    "#EDF1F4": "color/border/secondary",
    "#F5F8FA": "color/background/neutral",
    "#E6F4FE": "color/background/info-subtle",
    "#1B6DC0": "color/primary/500",
    "#22A5F7": "color/primary/400",
    "#FF7273": "color/status/critical",
    "#FFB146": "color/status/warning",
    "#E5EBF0": "color/background/tag-neutral",
    "#FCEBD9": "color/background/tag-warning",
    "#B65A11": "color/text/tag-warning",
}


_T_BG = TokenMapping(
    hex="#FFFFFF",
    token_name="color/background/primary",
    token_hex="#FFFFFF",
    bucket="exact",
)
_T_TEXT = TokenMapping(
    hex="#22272E",
    token_name="color/text/primary",
    token_hex="#22272E",
    bucket="exact",
)
_T_TEXT_2 = TokenMapping(
    hex="#36454F",
    token_name="color/text/secondary",
    token_hex="#36454F",
    bucket="exact",
)
_T_TEXT_3 = TokenMapping(
    hex="#627282",
    token_name="color/text/tertiary",
    token_hex="#627282",
    bucket="exact",
)
_T_BORDER = TokenMapping(
    hex="#EDF1F4",
    token_name="color/border/secondary",
    token_hex="#EDF1F4",
    bucket="exact",
)
_T_PRIMARY = TokenMapping(
    hex="#1B6DC0",
    token_name="color/primary/500",
    token_hex="#1B6DC0",
    bucket="exact",
)
_T_INFO_SUBTLE = TokenMapping(
    hex="#E6F4FE",
    token_name="color/background/info-subtle",
    token_hex="#E6F4FE",
    bucket="exact",
)
_T_HEADER_BG = TokenMapping(
    hex="#F5F8FA",
    token_name="color/background/neutral",
    token_hex="#F5F8FA",
    bucket="exact",
)
_T_CRITICAL = TokenMapping(
    hex="#FF7273",
    token_name="color/status/critical",
    token_hex="#FF7273",
    bucket="exact",
)
_T_WARNING = TokenMapping(
    hex="#FFB146",
    token_name="color/status/warning",
    token_hex="#FFB146",
    bucket="exact",
)
_T_INVERSE_TEXT = TokenMapping(
    hex="#C2C8CE",
    token_name="color/text/inverse-secondary",
    token_hex="#C2C8CE",
    bucket="exact",
)
_T_DARK = TokenMapping(
    hex="#22272E",
    token_name="color/background/inverse",
    token_hex="#22272E",
    bucket="exact",
)


_TOKENS_PAGE: list[TokenMapping] = [
    _T_BG,
    _T_TEXT,
    _T_TEXT_2,
    _T_TEXT_3,
    _T_BORDER,
    _T_PRIMARY,
]


_TOKENS_TABLE: list[TokenMapping] = [
    _T_BG,
    _T_TEXT,
    _T_TEXT_2,
    _T_BORDER,
    _T_HEADER_BG,
    _T_INFO_SUBTLE,
    _T_PRIMARY,
]


# ---------------------------------------------------------------------------
# Layout tree — parent/child JSX nesting, root first.
# Coordinates are taken verbatim from Figma's REST geometry for node
# 4147:20326 (NCM Drift Management — Cluster abc Drift Details open).
# ---------------------------------------------------------------------------


LAYOUT_TREE: list[LayoutNode] = [
    LayoutNode(
        id="4147:20326",
        name="Drift Management page",
        role="composed-region",
        bbox=(24427.0, 11985.0, 1280.0, 800.0),
        children_ids=[
            "4147:20352",
            "4147:20327",
            "4147:20364",
            "4147:20465",
        ],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=0.0,
            gap_consistent=True,
            confidence=0.9,
            flow_children=[
                "4147:20352",
                "4147:20327",
            ],
            absolute_children=[
                "4147:20364",
                "4147:20465",
            ],
            rationale=(
                "column score 0.90 (top dark nav + sub-nav header flow "
                "vertically; main body and side details panel are "
                "absolutely positioned side-by-side beneath them)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20352",
        name="Top dark navigation",
        role="banner",
        bbox=(24427.0, 11985.0, 1280.0, 60.0),
        children_ids=["4147:20353", "4147:20356", "4147:20357"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="space-between",
            align_items="center",
            gap=None,
            gap_consistent=False,
            confidence=0.93,
            flow_children=[
                "4147:20353",
                "4147:20356",
                "4147:20357",
            ],
            rationale=(
                "row score 0.93 (menu+app-switcher left, search center, "
                "alerts/tasks/settings/name right within a 60 px header)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20327",
        name="Sub-header (title + selects + tabs)",
        role="layout-container",
        bbox=(24427.0, 12046.0, 1280.0, 102.0),
        children_ids=["4147:20328", "4147:20338"],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=0.0,
            gap_consistent=True,
            confidence=0.96,
            flow_children=["4147:20328", "4147:20338"],
            rationale=(
                "column score 0.96 (title+selects row at y=12056, tabs "
                "row at y=12098, both 1240 wide centered)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20328",
        name="Title + Project + Account row",
        role="layout-container",
        bbox=(24447.0, 12056.0, 1240.0, 32.0),
        children_ids=["4147:20329", "4147:20331"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="start",
            align_items="center",
            gap=18.0,
            gap_consistent=True,
            confidence=0.92,
            flow_children=["4147:20329", "4147:20331"],
            rationale=(
                "row score 0.92 (title left @x=24447, controls cluster "
                "right @x=24630, baseline-aligned within 32 px)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20338",
        name="Tabs",
        role="tab-strip",
        bbox=(24447.0, 12098.0, 207.0, 50.0),
        children_ids=["4147:20340", "4147:20341", "4147:20342"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="start",
            align_items="end",
            gap=24.0,
            gap_consistent=True,
            confidence=0.95,
            flow_children=[
                "4147:20340",
                "4147:20341",
                "4147:20342",
            ],
            rationale=(
                "row score 0.95 (3 visible tab labels left→right with "
                "24 px gaps, indicator on Drifts)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20364",
        name="Main body (action toolbar + filter + table)",
        role="layout-container",
        bbox=(24427.0, 12148.0, 830.0, 637.0),
        children_ids=["4147:20365", "4147:20371", "4147:20375"],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=20.0,
            gap_consistent=True,
            confidence=0.97,
            flow_children=[
                "4147:20365",
                "4147:20371",
                "4147:20375",
            ],
            rationale=(
                "column score 0.97 (action toolbar → filter bar → table, "
                "20 px gaps, left-aligned within 0 px)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20365",
        name="Action toolbar",
        role="button-group",
        bbox=(24447.0, 12168.0, 790.0, 32.0),
        children_ids=[
            "4147:20367",
            "4147:20368",
            "4147:20369",
            "4147:20370",
        ],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="space-between",
            align_items="center",
            gap=10.0,
            gap_consistent=False,
            confidence=0.9,
            flow_children=[
                "4147:20367",
                "4147:20368",
                "4147:20369",
                "4147:20370",
            ],
            rationale=(
                "row score 0.90 (Ignore/Restore/Report Actions left "
                "cluster, Policy: Policy abc anchored right within 790 px)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20375",
        name="Drifts table region",
        role="composed-region",
        bbox=(24447.0, 12272.0, 790.0, 429.0),
        children_ids=["4147:20376", "4147:20384"],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=10.0,
            gap_consistent=True,
            confidence=1.0,
            flow_children=["4147:20376", "4147:20384"],
            rationale=(
                "column score 1.00 (info row at y=12272 + table body at "
                "y=12298, both 790 px wide)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20465",
        name="Drift Details side panel",
        role="composed-region",
        bbox=(25257.0, 12168.0, 430.0, 596.0),
        children_ids=["4147:20466", "4147:20473", "4147:20479", "4147:20484"],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=12.0,
            gap_consistent=False,
            confidence=0.94,
            flow_children=[
                "4147:20466",
                "4147:20473",
                "4147:20479",
                "4147:20484",
            ],
            rationale=(
                "column score 0.94 (panel header → subhead with pager → "
                "attribute sub-table → ignore-details, all 390 px wide)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20466",
        name="Side panel header",
        role="layout-container",
        bbox=(25257.0, 12168.0, 430.0, 50.0),
        children_ids=["4147:20467", "4147:20468"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="space-between",
            align_items="center",
            gap=None,
            gap_consistent=False,
            confidence=0.93,
            flow_children=["4147:20467", "4147:20468"],
            rationale=(
                "row score 0.93 (Drift Details title left, layout/close "
                "icons right, centered within 50 px header)"
            ),
        ),
    ),
    LayoutNode(
        id="4147:20479",
        name="Attribute sub-table",
        role="table-column",
        bbox=(25277.0, 12264.0, 390.0, 120.0),
        children_ids=["4147:20480", "4147:20481", "4147:20482", "4147:20483"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="start",
            align_items="stretch",
            gap=0.0,
            gap_consistent=True,
            confidence=1.0,
            flow_children=[
                "4147:20480",
                "4147:20481",
                "4147:20482",
                "4147:20483",
            ],
            rationale=(
                "row score 1.00 (4 columns Attribute|Relation|Expected|"
                "Detected, 98 px each)"
            ),
        ),
    ),
]


# ---------------------------------------------------------------------------
# Agenda — one MappedRegion per logical Prism component decision.
# ---------------------------------------------------------------------------


AGENDA: list[MappedRegion] = [
    # ----- Page root -----
    MappedRegion(
        id="4147:20326",
        aliased_ids=["752:13805"],
        name="Drift Management page",
        role="composed-region",
        bbox=(24427.0, 11985.0, 1280.0, 800.0),
        parent_chain=[],
        content_slots={
            "title": "Drift Management",
        },
        structural_hints=[
            "1280x800 page frame",
            (
                "vertical stack: top dark nav / sub-header (title + "
                "selects + tabs) / body row (table + side details panel)"
            ),
        ],
        children_summary=(
            "FRAME Navigation/Header(top dark nav) + FRAME Navigation"
            "(sub-header w/ Drift Mgmt title, Project, Account, Tabs) + "
            "FRAME Main body(action toolbar + filter bar + drifts table) + "
            "FRAME Drift Details(side panel)"
        ),
        hex_colors=["#FFFFFF", "#FAFCFD", "#22272E", "#EDF1F4"],
        box_style=BoxStyle(
            background_color="#FAFCFD",
            layout_mode="VERTICAL",
        ),
        reference_jsx_slice=(
            "{/* 4147:20326 */}\n<div className=\"drift-page\">"
            "{topNav}{subHeader}<div className=\"drift-body\">"
            "{mainBody}{sidePanel}</div></div>"
        ),
        shape_bucket="page",
        mapping=_mk_mapping(
            node_name="Drift Management page",
            primary="Page",
            primary_recommendation="Page",
            primary_recommendation_rationale=(
                "1280x800 page-bucketed frame with stacked top-nav, "
                "sub-header, and body regions"
            ),
            primary_recommendation_confidence=0.9,
            candidates=[
                _mk_candidate(
                    "Page",
                    score=0.4,
                    why=["page", "1280", "800"],
                    summary="Standard SaaS page shell.",
                ),
                _mk_candidate(
                    "FlexLayout",
                    score=0.18,
                    why=["row", "column", "container"],
                    summary="Generic row/column wrapper.",
                ),
            ],
            related=["NavigationHeader", "Tabs", "Table"],
            a11y_blocks=[
                "Use a single <main> region for the body content.",
            ],
            token_mappings=_TOKENS_PAGE,
            decompositions=[
                "NavigationHeader + SubHeader + MainBody + DetailsPanel",
            ],
        ),
    ),
    # ----- Top dark navigation -----
    MappedRegion(
        id="4147:20352",
        name="Top dark navigation",
        role="banner",
        bbox=(24427.0, 11985.0, 1280.0, 60.0),
        parent_chain=["Drift Management page"],
        content_slots={
            "items": [
                "menu",
                "Cloud Manager",
                "search",
                "alerts",
                "tasks",
                "settings",
                "Name",
            ],
        },
        structural_hints=[
            "1280x60 dark navigation strip",
            "menu+app-switcher left, search center, status+name right",
        ],
        children_summary=(
            "FRAME Menu+Switchers(Menu icon + Cloud Manager pill) + "
            "INSTANCE Nav Search(search input) + FRAME Status+Name"
            "(Alerts + Tasks + Settings + Name link)"
        ),
        hex_colors=["#22272E", "#C2C8CE", "#FFFFFF"],
        box_style=BoxStyle(
            background_color="#22272E",
            layout_mode="HORIZONTAL",
            padding=(14.0, 16.0, 14.0, 16.0),
        ),
        reference_jsx_slice=(
            "{/* 4147:20352 */}\n<NavigationHeader\n"
            "  className=\"drift-topnav\" theme=\"dark\"\n"
            "  left={<><MenuIcon /><AppSwitcher value=\"Cloud Manager\" /></>}\n"
            "  center={<Input search placeholder=\"Search here...\" />}\n"
            "  right={<><AlertIcon /><TaskIcon /><SettingsIcon />"
            "<UserMenu>Name</UserMenu></>}\n/>"
        ),
        shape_bucket="banner",
        mapping=_mk_mapping(
            node_name="Top dark navigation",
            primary="NavigationHeader",
            primary_recommendation="NavigationHeader",
            primary_recommendation_rationale=(
                "pattern role 'banner' on a 60 px-tall dark strip with "
                "left/center/right slots → NavigationHeader"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "NavigationHeader",
                    score=0.46,
                    why=["navigation", "header", "banner"],
                    summary="Top app-bar with left/center/right slots.",
                ),
                _mk_candidate(
                    "FlexLayout",
                    score=0.21,
                    why=["row", "header"],
                    summary="Row/column wrapper.",
                ),
            ],
            related=["AppSwitcher", "Input", "IconButton"],
            token_mappings=[
                _T_DARK,
                _T_INVERSE_TEXT,
                _T_BG,
            ],
        ),
    ),
    # ----- Cloud Manager app-switcher -----
    MappedRegion(
        id="4147:20355",
        name="Cloud Manager app switcher",
        role="instance",
        bbox=(24479.0, 11999.0, 240.0, 32.0),
        parent_chain=["Drift Management page", "Top dark navigation"],
        content_slots={
            "label": "Cloud Manager",
            "icon_name_hint": "CloudManagerMiniIcon",
        },
        structural_hints=[
            "240x32 dark pill with leading mini icon and trailing chevron",
        ],
        children_summary=(
            "INSTANCE _Navigation/AppSwitcher(Logo+Text + Button Icon)"
        ),
        hex_colors=["#22272E", "#FFFFFF"],
        box_style=BoxStyle(
            background_color="#22272E",
            border_color="#FFFFFF",
            border_width=1.0,
            corner_radius=4.0,
            padding=(0.0, 10.0, 0.0, 10.0),
        ),
        reference_jsx_slice=(
            "{/* 4147:20355 */}\n<AppSwitcher value=\"Cloud Manager\" "
            "icon={<CloudManagerMiniIcon />} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Cloud Manager app switcher",
            primary="AppSwitcher",
            candidates=[
                _mk_candidate(
                    "AppSwitcher",
                    score=0.42,
                    why=["app", "switcher", "navigation"],
                    summary=(
                        "Top-nav product switcher pill with icon + label "
                        "+ chevron."
                    ),
                ),
                _mk_candidate(
                    "Select",
                    score=0.18,
                    why=["dropdown", "select"],
                    summary="Generic dropdown.",
                ),
            ],
            related=["CloudManagerMiniIcon", "Dropdown"],
        ),
    ),
    # ----- Search input -----
    MappedRegion(
        id="4147:20356",
        name="Top nav search",
        role="instance",
        bbox=(24953.0, 11999.0, 350.0, 32.0),
        parent_chain=["Drift Management page", "Top dark navigation"],
        content_slots={
            "placeholder": "Search here...",
        },
        structural_hints=[
            "350x32 rounded search input with leading magnifier glyph",
            "trailing info + star affordances",
        ],
        children_summary=(
            "FRAME Search(Icon + placeholder text) + FRAME 2 Icon"
            "(Information + Empty Star)"
        ),
        hex_colors=["#22272E", "#C2C8CE"],
        box_style=BoxStyle(
            background_color="#22272E",
            border_color="#C2C8CE",
            border_width=1.0,
            corner_radius=14.0,
            padding=(0.0, 12.0, 0.0, 12.0),
        ),
        reference_jsx_slice=(
            "{/* 4147:20356 */}\n<Input search={true} theme=\"dark\" "
            "placeholder=\"Search here...\" suffix={<><InfoIcon />"
            "<StarIcon /></>} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Top nav search",
            primary="Input",
            candidates=[
                _mk_candidate(
                    "Input",
                    score=0.41,
                    why=["search", "input", "filter"],
                    summary=(
                        "Text input. Set `search={true}` for the "
                        "search-icon variant."
                    ),
                ),
                _mk_candidate(
                    "TextInput",
                    score=0.18,
                    why=["text", "input"],
                    summary="Alias for Input.",
                ),
            ],
            related=["InputAddon", "Icon"],
        ),
    ),
    # ----- Status + Name button group on the right of top nav -----
    MappedRegion(
        id="4147:20357",
        name="Top nav status + name",
        role="button-group",
        bbox=(25537.0, 12009.0, 150.0, 12.0),
        parent_chain=["Drift Management page", "Top dark navigation"],
        content_slots={
            "items": ["alerts", "tasks", "settings", "Name"],
        },
        structural_hints=[
            "horizontal cluster of 3 icon buttons + 1 user link",
            "12px icon glyphs",
        ],
        children_summary=(
            "FRAME Alerts(Icon/System/Alert) + FRAME Tasks(Icon/System/"
            "Task) + INSTANCE Icon/System/Settings + INSTANCE Action/Link"
        ),
        hex_colors=["#C2C8CE", "#FFFFFF"],
        box_style=BoxStyle(
            layout_mode="HORIZONTAL",
            gap=20.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20357 */}\n<ButtonGroup itemGap=\"M\">"
            "<IconButton aria-label=\"Alerts\"><AlertIcon /></IconButton>"
            "<IconButton aria-label=\"Tasks\"><TaskIcon /></IconButton>"
            "<IconButton aria-label=\"Settings\"><SettingsIcon /></IconButton>"
            "<UserMenu>Name</UserMenu></ButtonGroup>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Top nav status + name",
            primary="ButtonGroup",
            primary_recommendation="ButtonGroup",
            primary_recommendation_rationale=(
                "pattern role 'button-group' → ButtonGroup"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "ButtonGroup",
                    score=0.38,
                    why=["button", "group", "icons"],
                    summary="Group of related buttons sharing context.",
                ),
                _mk_candidate(
                    "FlexLayout",
                    score=0.21,
                    why=["row", "icons"],
                    summary="Row wrapper.",
                ),
            ],
            related=["IconButton", "AlertIcon", "TaskIcon", "SettingsIcon"],
        ),
    ),
    # ----- Sub-header (title + project + account + tabs) -----
    MappedRegion(
        id="4147:20327",
        name="Sub-header",
        role="layout-container",
        bbox=(24427.0, 12046.0, 1280.0, 102.0),
        parent_chain=["Drift Management page"],
        content_slots={
            "title": "Drift Management",
            "items": ["Project", "Account"],
        },
        structural_hints=[
            "1280x102 white sub-header with bottom border",
            "title + selects row above tabs row",
        ],
        children_summary=(
            "FRAME Title+Selects(Title + Project Select + Account Select) "
            "+ FRAME Tabs(Drifts + Policies + Rules)"
        ),
        hex_colors=["#FFFFFF", "#22272E", "#EDF1F4"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#EDF1F4",
            border_width=1.0,
            layout_mode="VERTICAL",
            padding=(0.0, 20.0, 0.0, 20.0),
        ),
        reference_jsx_slice=(
            "{/* 4147:20327 */}\n<div className=\"drift-pageheader\">"
            "{titleRow}{tabs}</div>"
        ),
        shape_bucket="banner",
        mapping=_mk_mapping(
            node_name="Sub-header",
            primary="FlexLayout",
            candidates=[
                _mk_candidate(
                    "FlexLayout",
                    score=0.30,
                    why=["header", "column"],
                    summary="Row/column wrapper.",
                ),
                _mk_candidate(
                    "PageHeader",
                    score=0.22,
                    why=["page", "header"],
                    summary="Page-level header with title and actions.",
                ),
            ],
            token_mappings=[_T_BG, _T_TEXT, _T_BORDER],
        ),
    ),
    # ----- Page title text -----
    MappedRegion(
        id="4147:20329",
        name="Drift Management title",
        role="text",
        bbox=(24447.0, 12061.0, 142.0, 22.0),
        parent_chain=["Drift Management page", "Sub-header"],
        content_slots={
            "value": "Drift Management",
        },
        structural_hints=["18px semibold heading"],
        children_summary="single TEXT node",
        hex_colors=["#22272E"],
        box_style=BoxStyle(),
        reference_jsx_slice=(
            "{/* 4147:20329 */}\n<Title size=\"h2\">Drift Management</Title>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Drift Management title",
            primary="Title",
            candidates=[
                _mk_candidate(
                    "Title",
                    score=0.31,
                    why=["title", "heading"],
                    summary="Heading primitive (h1/h2/…).",
                ),
                _mk_candidate(
                    "Paragraph",
                    score=0.16,
                    why=["text"],
                    summary="Body text primitive.",
                ),
            ],
            token_mappings=[_T_TEXT],
        ),
    ),
    # ----- Project select -----
    MappedRegion(
        id="4147:20334",
        name="Project select",
        role="instance",
        bbox=(24679.0, 12056.0, 155.0, 32.0),
        parent_chain=["Drift Management page", "Sub-header"],
        content_slots={
            "label": "Project",
            "value": "Default Project",
            "items": ["Default Project", "Project 1"],
        },
        structural_hints=["155x32 split-button select"],
        children_summary=(
            "INSTANCE Action/Button(Default Project) + Separator + "
            "INSTANCE Action/Icon Button(Chevron)"
        ),
        hex_colors=["#FFFFFF", "#DCE1E5", "#22272E"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#DCE1E5",
            border_width=1.0,
            corner_radius=2.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20334 */}\n<Select rowsData={PROJECT_OPTIONS} "
            "selectedRow={projectDefault} onSelectedChange={setProject} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Project select",
            primary="Select",
            candidates=[
                _mk_candidate(
                    "Select",
                    score=0.45,
                    why=["select", "dropdown", "project"],
                    summary="Single-select dropdown.",
                ),
                _mk_candidate(
                    "SplitButton",
                    score=0.21,
                    why=["split", "button"],
                    summary="Button + dropdown split control.",
                ),
            ],
            related=["Dropdown", "Input"],
        ),
    ),
    # ----- Account select -----
    MappedRegion(
        id="4147:20337",
        name="Account select",
        role="instance",
        bbox=(24911.0, 12056.0, 147.0, 32.0),
        parent_chain=["Drift Management page", "Sub-header"],
        content_slots={
            "label": "Account",
            "value": "Accounts-List",
            "items": ["Accounts-List", "Account 1"],
        },
        structural_hints=["147x32 split-button select"],
        children_summary=(
            "INSTANCE Action/Button(Accounts-List) + Separator + "
            "INSTANCE Action/Icon Button(Chevron)"
        ),
        hex_colors=["#FFFFFF", "#DCE1E5", "#22272E"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#DCE1E5",
            border_width=1.0,
            corner_radius=2.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20337 */}\n<Select rowsData={ACCOUNT_OPTIONS} "
            "selectedRow={accountList} onSelectedChange={setAccount} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Account select",
            primary="Select",
            candidates=[
                _mk_candidate(
                    "Select",
                    score=0.45,
                    why=["select", "dropdown", "account"],
                    summary="Single-select dropdown.",
                ),
            ],
            related=["Dropdown", "Input"],
        ),
    ),
    # ----- Tabs -----
    MappedRegion(
        id="4147:20338",
        name="Tabs",
        role="tab-strip",
        bbox=(24447.0, 12098.0, 207.0, 50.0),
        parent_chain=["Drift Management page", "Sub-header"],
        content_slots={
            "items": ["Drifts", "Policies", "Rules"],
            "value": "Drifts",
            "cell_count": 3,
        },
        structural_hints=[
            "horizontal tab strip with bottom-border indicator",
            "Drifts active",
        ],
        children_summary=(
            "INSTANCE Navigation/Tab(Drifts active) + INSTANCE Tab"
            "(Policies) + INSTANCE Tab(Rules)"
        ),
        hex_colors=["#22272E", "#1B6DC0", "#EDF1F4"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            layout_mode="HORIZONTAL",
            gap=24.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20338 */}\n<Tabs data={[\n"
            "  { key: 'drifts', title: 'Drifts' },\n"
            "  { key: 'policies', title: 'Policies' },\n"
            "  { key: 'rules', title: 'Rules' }\n"
            "]} defaultActiveKey=\"drifts\" />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Tabs",
            primary="Tabs",
            primary_recommendation="Tabs",
            primary_recommendation_rationale=(
                "pattern role 'tab-strip' with 3 visible labels + active "
                "indicator → Tabs"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "Tabs",
                    score=0.46,
                    why=["tab", "navigation", "drifts"],
                    summary="Horizontal tab strip with active indicator.",
                ),
                _mk_candidate(
                    "TabBar",
                    score=0.21,
                    why=["tab", "bar"],
                    summary="Lower-level tab bar primitive.",
                ),
            ],
            related=["TabItem"],
            a11y_blocks=[
                "Render role=\"tablist\" with aria-orientation=\"horizontal\".",
                (
                    "Each tab uses role=\"tab\" with aria-selected on the "
                    "active item."
                ),
            ],
            token_mappings=[_T_TEXT, _T_PRIMARY, _T_BORDER],
        ),
    ),
    # ----- Action toolbar (Ignore / Restore / Report Actions / Policy) ----
    MappedRegion(
        id="4147:20365",
        name="Action toolbar",
        role="button-group",
        bbox=(24447.0, 12168.0, 790.0, 32.0),
        parent_chain=["Drift Management page", "Main body"],
        content_slots={
            "items": [
                "Ignore",
                "Restore",
                "Report Actions",
                "Policy: Policy abc",
            ],
        },
        structural_hints=[
            "horizontal toolbar above the drifts table",
            "Ignore/Restore/Report Actions clustered left, Policy split-button right",
        ],
        children_summary=(
            "FRAME Frame 1321315110(Ignore + Restore + Report Actions) + "
            "INSTANCE Action/Button(Policy: Policy abc)"
        ),
        hex_colors=["#FFFFFF", "#DCE1E5", "#22272E"],
        box_style=BoxStyle(
            layout_mode="HORIZONTAL",
            gap=10.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20365 */}\n<FlexLayout className=\"drift-action-toolbar\""
            " alignItems=\"center\" itemGap=\"M\">\n"
            "  <Button type={Button.ButtonTypes.SECONDARY} disabled>Ignore"
            "</Button>\n"
            "  <Button type={Button.ButtonTypes.SECONDARY} disabled>Restore"
            "</Button>\n"
            "  <Button type={Button.ButtonTypes.SECONDARY} disabled>"
            "Report Actions ▾</Button>\n"
            "  <FlexItem flexGrow={1} />\n"
            "  <Button type={Button.ButtonTypes.SECONDARY}>Policy: Policy abc"
            " ▾</Button>\n</FlexLayout>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Action toolbar",
            primary="ButtonGroup",
            primary_recommendation="ButtonGroup",
            primary_recommendation_rationale=(
                "pattern role 'button-group' on a 32 px-tall row of 4 "
                "buttons → ButtonGroup"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "ButtonGroup",
                    score=0.38,
                    why=["button", "group", "actions"],
                    summary="Group of related buttons sharing context.",
                ),
                _mk_candidate(
                    "Toolbar",
                    score=0.21,
                    why=["toolbar", "actions"],
                    summary="Top-of-table action bar.",
                ),
            ],
            related=["Button", "FlexLayout"],
        ),
    ),
    # ----- Ignore button (disabled) -----
    MappedRegion(
        id="4147:20367",
        name="Ignore button",
        role="instance",
        bbox=(24447.0, 12168.0, 70.0, 32.0),
        parent_chain=[
            "Drift Management page",
            "Main body",
            "Action toolbar",
        ],
        content_slots={
            "label": "Ignore",
        },
        structural_hints=["secondary button, disabled state"],
        children_summary="single TEXT label",
        hex_colors=["#FFFFFF", "#DCE1E5"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#DCE1E5",
            border_width=1.0,
            corner_radius=2.0,
            padding=(8.0, 15.0, 8.0, 15.0),
            opacity=0.5,
        ),
        reference_jsx_slice=(
            "{/* 4147:20367 */}\n<Button type={Button.ButtonTypes.SECONDARY}"
            " disabled>Ignore</Button>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Ignore button",
            primary="Button",
            candidates=[
                _mk_candidate(
                    "Button",
                    score=0.41,
                    why=["button", "ignore", "secondary"],
                    summary="Standard button with type/size variants.",
                ),
            ],
            a11y_blocks=[
                (
                    "Disabled state must be conveyed via aria-disabled, not "
                    "just colour; preserve focusability for screen readers."
                ),
            ],
        ),
    ),
    # ----- Restore button (disabled) -----
    MappedRegion(
        id="4147:20368",
        name="Restore button",
        role="instance",
        bbox=(24527.0, 12168.0, 78.0, 32.0),
        parent_chain=[
            "Drift Management page",
            "Main body",
            "Action toolbar",
        ],
        content_slots={
            "label": "Restore",
        },
        structural_hints=["secondary button, disabled state"],
        children_summary="single TEXT label",
        hex_colors=["#FFFFFF", "#DCE1E5"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#DCE1E5",
            border_width=1.0,
            corner_radius=2.0,
            padding=(8.0, 15.0, 8.0, 15.0),
            opacity=0.5,
        ),
        reference_jsx_slice=(
            "{/* 4147:20368 */}\n<Button type={Button.ButtonTypes.SECONDARY}"
            " disabled>Restore</Button>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Restore button",
            primary="Button",
            candidates=[
                _mk_candidate(
                    "Button",
                    score=0.41,
                    why=["button", "restore", "secondary"],
                    summary="Standard button.",
                ),
            ],
        ),
    ),
    # ----- Report Actions split button -----
    MappedRegion(
        id="4147:20369",
        name="Report Actions button",
        role="instance",
        bbox=(24615.0, 12168.0, 138.0, 32.0),
        parent_chain=[
            "Drift Management page",
            "Main body",
            "Action toolbar",
        ],
        content_slots={
            "label": "Report Actions",
            "icon_name_hint": "ChevronIcon",
        },
        structural_hints=[
            "secondary split-button with trailing chevron",
            "disabled state",
        ],
        children_summary=(
            "FRAME Content(Button Text + INSTANCE Button Icon chevron)"
        ),
        hex_colors=["#FFFFFF", "#DCE1E5"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#DCE1E5",
            border_width=1.0,
            corner_radius=2.0,
            padding=(8.0, 15.0, 8.0, 15.0),
            opacity=0.5,
        ),
        reference_jsx_slice=(
            "{/* 4147:20369 */}\n<Button type={Button.ButtonTypes.SECONDARY}"
            " disabled>\n  Report Actions <ChevronIcon size=\"small\" />\n"
            "</Button>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Report Actions button",
            primary="Button",
            candidates=[
                _mk_candidate(
                    "Button",
                    score=0.41,
                    why=["button", "report", "actions"],
                    summary="Standard button (split-button via chevron icon).",
                ),
            ],
            related=["ChevronIcon"],
        ),
    ),
    # ----- Policy: Policy abc split button -----
    MappedRegion(
        id="4147:20370",
        name="Policy split button",
        role="instance",
        bbox=(25083.0, 12168.0, 154.0, 32.0),
        parent_chain=[
            "Drift Management page",
            "Main body",
            "Action toolbar",
        ],
        content_slots={
            "label": "Policy: Policy abc",
            "icon_name_hint": "ChevronIcon",
        },
        structural_hints=[
            "secondary split-button anchored to the right of the toolbar",
        ],
        children_summary=(
            "FRAME Content(Button Text + INSTANCE Button Icon chevron)"
        ),
        hex_colors=["#FFFFFF", "#DCE1E5", "#22272E"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#DCE1E5",
            border_width=1.0,
            corner_radius=2.0,
            padding=(8.0, 15.0, 8.0, 15.0),
        ),
        reference_jsx_slice=(
            "{/* 4147:20370 */}\n<Button type={Button.ButtonTypes.SECONDARY}>"
            "Policy: Policy abc <ChevronIcon size=\"small\" /></Button>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Policy split button",
            primary="Button",
            candidates=[
                _mk_candidate(
                    "Button",
                    score=0.42,
                    why=["button", "policy", "split"],
                    summary="Standard button.",
                ),
                _mk_candidate(
                    "Select",
                    score=0.18,
                    why=["select", "dropdown"],
                    summary="Dropdown alternative for policy picker.",
                ),
            ],
            related=["ChevronIcon", "Select"],
        ),
    ),
    # ----- Filter bar (favourite + chips + Modify Filters) -----
    MappedRegion(
        id="4147:20372",
        name="Filter bar",
        role="composed-region",
        bbox=(24447.0, 12220.0, 790.0, 32.0),
        parent_chain=["Drift Management page", "Main body"],
        content_slots={
            "items": [
                "Group by = None",
                "Time = All Time",
                "Status = Open",
            ],
            "label": "Modify Filters",
        },
        structural_hints=[
            "32 px-tall pill bar with leading favourites split + 3 input "
            "bubbles + trailing Modify Filters link",
        ],
        children_summary=(
            "FRAME Favourite Prefix(Star + Chevron) + INSTANCE Filter Input "
            "Base(3 Input/Bubble chips) + FRAME Controls(Modify Filters link)"
        ),
        hex_colors=["#FFFFFF", "#DCE1E5", "#1B6DC0"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#DCE1E5",
            border_width=1.0,
            corner_radius=4.0,
            layout_mode="HORIZONTAL",
            padding=(0.0, 8.0, 0.0, 8.0),
            gap=8.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20372 */}\n<FilterBar\n"
            "  prefix={<><StarIcon /><ChevronIcon /></>}\n"
            "  chips={[\n"
            "    { key: 'group', label: 'Group by = None' },\n"
            "    { key: 'time', label: 'Time = All Time' },\n"
            "    { key: 'status', label: 'Status = Open' }\n"
            "  ]}\n"
            "  trailing={<><FilterIcon /> Modify Filters</>} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Filter bar",
            primary="FilterBar",
            candidates=[
                _mk_candidate(
                    "FilterBar",
                    score=0.39,
                    why=["filter", "chips", "bar"],
                    summary="Filter input with chip rendering.",
                ),
                _mk_candidate(
                    "Input",
                    score=0.18,
                    why=["filter", "input"],
                    summary="Base input primitive used inside FilterBar.",
                ),
            ],
            related=["FilterIcon", "StarIcon", "ChipBubble"],
        ),
    ),
    # ----- Table info row (Viewing all 33 drifts | Group By | pager | per-page)
    MappedRegion(
        id="4147:20376",
        name="Drifts table info row",
        role="composed-region",
        bbox=(24447.0, 12272.0, 790.0, 16.0),
        parent_chain=[
            "Drift Management page",
            "Main body",
            "Drifts table region",
        ],
        content_slots={
            "title": "Viewing all 33 drifts",
            "label": "Group By: None",
            "items": ["1 - 20 of 100", "20 Per Page"],
        },
        structural_hints=[
            "row above the table summarising count, grouping, "
            "pagination and per-page selector",
        ],
        children_summary=(
            "FRAME Frame 2402(TEXT 'Viewing all 33 drifts' + Group By "
            "drop) + INSTANCE Table/Table Controls(pager + per-page)"
        ),
        hex_colors=["#22272E", "#36454F", "#22A5F7"],
        box_style=BoxStyle(
            layout_mode="HORIZONTAL",
            gap=16.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20376 */}\n<TableInfo count=\"Viewing all 33 drifts\""
            " groupBy=\"None\" page=\"1 - 20 of 100\" perPage={20} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Drifts table info row",
            primary="FlexLayout",
            candidates=[
                _mk_candidate(
                    "FlexLayout",
                    score=0.27,
                    why=["row", "info"],
                    summary="Row wrapper with gap + alignment.",
                ),
                _mk_candidate(
                    "Pagination",
                    score=0.21,
                    why=["pager", "perPage"],
                    summary="Pagination control.",
                ),
            ],
            related=["Pagination", "Select"],
        ),
    ),
    # ----- Drifts table -----
    MappedRegion(
        id="4147:20384",
        aliased_ids=["4147:20385", "4147:20375"],
        name="Drifts table",
        role="table-column",
        bbox=(24447.0, 12298.0, 790.0, 403.0),
        parent_chain=[
            "Drift Management page",
            "Main body",
            "Drifts table region",
        ],
        content_slots={
            "header": (
                "Actions | Entity Name | Entity type | Rule Name | "
                "Severity | Status | Last Detected"
            ),
            "items": [
                "Cluster abc | Cluster | System Rule 3 + System tag | "
                "High | Ignored | 1 hour ago",
                "VM-Two | VM | Abc Rule | High | Open | 2 hours ago",
                "VM-Three | VM | System Rule 3 + System tag | Medium | "
                "Open | 3 hours ago",
                "VM-Four | VM | System Rule + System tag | Medium | "
                "Open | 1 Day ago",
                "VM-Five | VM | System Rule 3 + System tag | Medium | "
                "Open | Nov 15, 2023, 05:21 PM",
                "VM-Six | VM | Rule 2323 + Custom tag | Medium | "
                "Open | Nov 15, 2023, 12:45:21 PM",
                "VM-Seven | VM | Rule 3423eds | Medium | Open | "
                "Nov 15, 2023, 09:45:21 PM",
                "VM-Eight | VM | System Rule + System tag | Medium | "
                "Open | Nov 15, 2023, 10:45:21 PM",
            ],
            "value": "Cluster abc",
            "cell_count": 8,
        },
        structural_hints=[
            "7-column drifts table with row-level checkbox selection",
            "row 1 (Cluster abc) selected — light blue (#E6F4FE) row fill",
            "Severity column renders triangle (High) / diamond (Medium) "
            "mini status icons",
            "Rule Name column shows clickable underlined link plus a "
            "trailing System/Custom tag",
        ],
        children_summary=(
            "INSTANCE Table/Column Actions(checkbox) + INSTANCE Table/"
            "Column Entity Name + INSTANCE Table/Column Entity type + "
            "INSTANCE Table/Column Rule Name (Tag variant) + FRAME "
            "Table/Column Severity + FRAME Table/Column Status + FRAME "
            "Table/Column Last Detected"
        ),
        hex_colors=[
            "#FFFFFF",
            "#F5F8FA",
            "#E6F4FE",
            "#22272E",
            "#36454F",
            "#1B6DC0",
            "#FF7273",
            "#FFB146",
            "#E5EBF0",
            "#FCEBD9",
        ],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#EDF1F4",
            border_width=1.0,
            corner_radius=4.0,
            layout_mode="VERTICAL",
        ),
        reference_jsx_slice=(
            "{/* 4147:20384 */}\n<Table\n"
            "  className=\"drift-table\"\n"
            "  columns={DRIFT_COLUMNS}\n"
            "  dataSource={DRIFT_ROWS}\n"
            "  rowKey=\"key\"\n"
            "  structure={{\n"
            "    overflowColumns: true,\n"
            "    columnWidths: {\n"
            "      entityName: '110px', entityType: '90px', "
            "ruleName: '200px',\n"
            "      severity: '94px', status: '82px', "
            "lastDetected: '162px'\n"
            "    }\n"
            "  }}\n"
            "  rowSelection={{ type: Table.ROW_SELECTION_TYPES.CHECKBOX, "
            "selected: ['r1'] }}\n"
            "  sort={{ sortable: ['entityName','entityType','ruleName',"
            "'severity','status','lastDetected'] }}\n/>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Drifts table",
            primary="Table",
            primary_recommendation="Table",
            primary_recommendation_rationale=(
                "pattern role 'table-column' on a 7-column table with "
                "row-level selection → Table (outer wrapper)"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "Table",
                    score=0.55,
                    why=["table", "drift", "columns", "rows"],
                    summary=(
                        "Data table with sortable columns, row "
                        "selection, custom cell renderers."
                    ),
                ),
                _mk_candidate(
                    "TableColumn",
                    score=0.32,
                    why=["table", "column"],
                    summary="Per-column descriptor.",
                ),
                _mk_candidate(
                    "DataTable",
                    score=0.12,
                    why=["data", "table"],
                    summary="Higher-level data-grid wrapper.",
                    source="hybrid",
                ),
            ],
            related=[
                "TableColumn",
                "TableCell",
                "Tag",
                "StatusTriangleMiniIcon",
                "StatusDiamondMiniIcon",
                "Checkbox",
            ],
            a11y_blocks=[
                (
                    "Wrap in role=\"table\". Provide aria-label and "
                    "aria-sort per column."
                ),
                (
                    "Severity icon must be paired with text (High/Medium) "
                    "so screen readers don't lose the signal."
                ),
                (
                    "Row selection: checkbox column header acts as a "
                    "select-all and must use aria-checked / aria-label."
                ),
            ],
            token_mappings=_TOKENS_TABLE,
            decompositions=["Table + TableColumn + Tag"],
        ),
    ),
    # ----- Side details panel -----
    MappedRegion(
        id="4147:20465",
        name="Drift Details panel",
        role="composed-region",
        bbox=(25257.0, 12168.0, 430.0, 596.0),
        parent_chain=["Drift Management page"],
        content_slots={
            "title": "Drift Details",
        },
        structural_hints=[
            "430x596 white panel right of the drifts table",
            "header / cluster subhead with pager / attribute sub-table / "
            "ignore-details section",
        ],
        children_summary=(
            "FRAME Header(title + layout/close icons) + FRAME Subhead"
            "(Cluster abc Custom Rul 3 + pager 1-5 of 5) + FRAME Sub-table"
            "(Attribute|Relation|Expected|Detected) + FRAME Ignore Details"
        ),
        hex_colors=["#FFFFFF", "#EDF1F4", "#22272E"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#EDF1F4",
            border_width=1.0,
            corner_radius=4.0,
            layout_mode="VERTICAL",
            padding=(16.0, 18.0, 16.0, 18.0),
            has_shadow=True,
        ),
        reference_jsx_slice=(
            "{/* 4147:20465 */}\n<Panel\n"
            "  className=\"drift-details\"\n"
            "  title=\"Drift Details\"\n"
            "  actions={<><LayoutToggleIcon /><CloseIcon /></>}\n>"
            "{subhead}{subTable}{ignoreDetails}</Panel>"
        ),
        shape_bucket="sidebar",
        mapping=_mk_mapping(
            node_name="Drift Details panel",
            primary="Panel",
            candidates=[
                _mk_candidate(
                    "Panel",
                    score=0.41,
                    why=["panel", "details", "side"],
                    summary="Right-side details panel with header + body.",
                ),
                _mk_candidate(
                    "Drawer",
                    score=0.21,
                    why=["drawer", "details"],
                    summary="Slide-in drawer.",
                    source="hybrid",
                ),
            ],
            related=["IconButton", "CloseIcon"],
            a11y_blocks=[
                (
                    "Panel close button must have aria-label=\"Close "
                    "Drift Details\"."
                ),
            ],
            token_mappings=[_T_BG, _T_TEXT, _T_BORDER],
        ),
    ),
    # ----- Subhead with pager -----
    MappedRegion(
        id="4147:20473",
        name="Drift subhead with pager",
        role="composed-region",
        bbox=(25277.0, 12238.0, 390.0, 16.0),
        parent_chain=[
            "Drift Management page",
            "Drift Details panel",
        ],
        content_slots={
            "title": "Cluster abc Custom Rul 3",
            "label": "1 - 5 of 5",
        },
        structural_hints=[
            "subhead row: bold cluster name on the left, "
            "page-of-results pager on the right",
        ],
        children_summary=(
            "TEXT 'Cluster abc Custom Rul 3' + INSTANCE Table/Table "
            "Controls(prev + 1-5 of 5 + next)"
        ),
        hex_colors=["#22272E", "#22A5F7"],
        box_style=BoxStyle(
            layout_mode="HORIZONTAL",
            gap=10.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20473 */}\n<FlexLayout justifyContent=\"space-between\""
            " alignItems=\"center\">\n"
            "  <Title size=\"h4\">Cluster abc Custom Rul 3</Title>\n"
            "  <Pagination current={1} total={5} pageSize={5} />\n"
            "</FlexLayout>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Drift subhead with pager",
            primary="FlexLayout",
            candidates=[
                _mk_candidate(
                    "FlexLayout",
                    score=0.30,
                    why=["row", "header"],
                    summary="Row wrapper.",
                ),
                _mk_candidate(
                    "Pagination",
                    score=0.21,
                    why=["pager"],
                    summary="Page-of-N pager.",
                ),
            ],
            related=["Title", "Pagination"],
        ),
    ),
    # ----- Attribute sub-table -----
    MappedRegion(
        id="4147:20479",
        aliased_ids=["4147:20478"],
        name="Attribute sub-table",
        role="table-column",
        bbox=(25277.0, 12264.0, 390.0, 120.0),
        parent_chain=[
            "Drift Management page",
            "Drift Details panel",
        ],
        content_slots={
            "header": "Attribute | Relation | Expected | Detected",
            "items": [
                "Enable VPC | EQUALS | - | True",
                "Assigned VRAM | GREATER THAN | 2048 | 1024",
            ],
            "cell_count": 2,
        },
        structural_hints=[
            "4-column table with 2 visible drift attribute rows",
            "all cells text-only, single-line truncation with ellipsis",
        ],
        children_summary=(
            "4 INSTANCE Table/Column (Attribute, Relation, Expected, "
            "Detected) each with 2 INSTANCE Table/Table Cell rows"
        ),
        hex_colors=["#FFFFFF", "#EDF1F4", "#22272E"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#EDF1F4",
            border_width=1.0,
            corner_radius=4.0,
            layout_mode="VERTICAL",
        ),
        reference_jsx_slice=(
            "{/* 4147:20479 */}\n<Table\n"
            "  className=\"drift-subtable\"\n"
            "  columns={SUB_COLUMNS}\n"
            "  dataSource={SUB_ROWS}\n"
            "  rowKey=\"key\"\n"
            "  structure={{\n"
            "    overflowColumns: true,\n"
            "    columnWidths: { attribute: '92px', relation: '78px',\n"
            "      expected: '88px', detected: '100px' }\n"
            "  }}\n"
            "  sort={{ sortable: ['attribute','relation','expected',"
            "'detected'] }}\n/>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Attribute sub-table",
            primary="Table",
            primary_recommendation="Table",
            primary_recommendation_rationale=(
                "pattern role 'table-column' on a 4-column drift detail "
                "table → Table (outer wrapper)"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "Table",
                    score=0.5,
                    why=["table", "attribute", "drift"],
                    summary="Data table with sortable columns.",
                ),
                _mk_candidate(
                    "TableColumn",
                    score=0.3,
                    why=["table", "column"],
                    summary="Per-column descriptor.",
                ),
            ],
            related=["TableColumn", "TableCell"],
            token_mappings=[_T_BG, _T_TEXT, _T_BORDER],
        ),
    ),
    # ----- Ignore Details (label/value rows) -----
    MappedRegion(
        id="4147:20484",
        name="Ignore Details section",
        role="composed-region",
        bbox=(25277.0, 12403.0, 390.0, 171.0),
        parent_chain=[
            "Drift Management page",
            "Drift Details panel",
        ],
        content_slots={
            "title": "Ignore Details",
            "items": [
                "Ignored By | shivam.roy@nutanix.com",
                "Ignored on | Dec 03, 2024, 03:33:12 PM",
                (
                    "Reason to Ignore | Drift is irrelevant on the "
                    "selected resource."
                ),
            ],
            "cell_count": 3,
        },
        structural_hints=[
            "section with a 13px bold heading and 3 label/value rows",
            "label column left at 130 px, value column flexes",
        ],
        children_summary=(
            "TEXT 'Ignore Details' + 3 FRAME 'Widget Row' (label TEXT + "
            "value TEXT)"
        ),
        hex_colors=["#22272E", "#627282"],
        box_style=BoxStyle(
            layout_mode="VERTICAL",
            gap=12.0,
        ),
        reference_jsx_slice=(
            "{/* 4147:20484 */}\n<section className=\"drift-details__section\""
            ">\n"
            "  <h3>Ignore Details</h3>\n"
            "  <FieldRow label=\"Ignored By\" value=\"shivam.roy@nutanix.com\" "
            "/>\n"
            "  <FieldRow label=\"Ignored on\" value=\"Dec 03, 2024, 03:33:12 "
            "PM\" />\n"
            "  <FieldRow label=\"Reason to Ignore\" value=\"Drift is "
            "irrelevant on the selected resource.\" />\n"
            "</section>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Ignore Details section",
            primary="DescriptionList",
            candidates=[
                _mk_candidate(
                    "DescriptionList",
                    score=0.32,
                    why=["label", "value", "details"],
                    summary="Label/value list (dl/dt/dd primitive).",
                ),
                _mk_candidate(
                    "FlexLayout",
                    score=0.18,
                    why=["column", "rows"],
                    summary="Column wrapper.",
                ),
            ],
            related=["TextLabel", "Paragraph"],
            token_mappings=[_T_TEXT, _T_TEXT_3],
        ),
    ),
]


# ---------------------------------------------------------------------------
# Dropped — audit trail for nodes the walker discarded.
# ---------------------------------------------------------------------------


DROPPED: list[DroppedNode] = [
    DroppedNode(
        id="4147:20354",
        name="Menu icon glyph",
        type="VECTOR",
        reason="icon_internal",
        detail="hamburger menu glyph captured by NavigationHeader",
    ),
    DroppedNode(
        id="4147:20330",
        name="Header divider",
        type="RECTANGLE",
        reason="invisible_decoration",
        detail="1px vertical divider captured by parent's border",
    ),
    DroppedNode(
        id="4147:20351",
        name="Tab strip vertical separator",
        type="INSTANCE",
        reason="tiny_decorative",
        detail="1x16 vertical separator between tabs and overflow column",
    ),
    DroppedNode(
        id="4147:20343",
        name="Tab 'Page 1' overflow #1",
        type="INSTANCE",
        reason="tiny_decorative",
        detail=(
            "scratch tab placeholders bracketed inside the Tabs frame; "
            "not part of the visible 3-tab strip"
        ),
    ),
    DroppedNode(
        id="4147:20344",
        name="Tab 'Page 1' overflow #2",
        type="INSTANCE",
        reason="tiny_decorative",
        detail="see 4147:20343",
    ),
    DroppedNode(
        id="4147:20345",
        name="Tab 'Page 1' overflow #3",
        type="INSTANCE",
        reason="tiny_decorative",
        detail="see 4147:20343",
    ),
    DroppedNode(
        id="4147:20346",
        name="Tab 'Page 1' overflow #4",
        type="INSTANCE",
        reason="tiny_decorative",
        detail="see 4147:20343",
    ),
    DroppedNode(
        id="4147:20347",
        name="Tab 'Page 1' overflow #5",
        type="INSTANCE",
        reason="tiny_decorative",
        detail="see 4147:20343",
    ),
    DroppedNode(
        id="4147:20348",
        name="Tab 'Page 1' overflow #6",
        type="INSTANCE",
        reason="tiny_decorative",
        detail="see 4147:20343",
    ),
    DroppedNode(
        id="4147:20349",
        name="Tab 'Page 1' overflow #7",
        type="INSTANCE",
        reason="tiny_decorative",
        detail="see 4147:20343",
    ),
    DroppedNode(
        id="4147:20350",
        name="Tab 'Page 1' overflow #8",
        type="INSTANCE",
        reason="tiny_decorative",
        detail="see 4147:20343",
    ),
    DroppedNode(
        id="4147:20373",
        name="Filter chip 'Time = All Time'",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="absorbed into Filter bar mapping (4147:20372)",
    ),
    DroppedNode(
        id="4147:20374",
        name="Filter chip 'Status = Open'",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="absorbed into Filter bar mapping (4147:20372)",
    ),
    DroppedNode(
        id="4147:20378",
        name="Viewing all 33 drifts text",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Drifts table info row title slot",
    ),
    DroppedNode(
        id="4147:20379",
        name="Table info divider",
        type="RECTANGLE",
        reason="invisible_decoration",
        detail="1px vertical divider captured by parent's border",
    ),
    DroppedNode(
        id="4147:20381",
        name="Group By: None text",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Drifts table info row label slot",
    ),
    DroppedNode(
        id="4147:20383",
        name="Table controls (pager + per-page)",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="absorbed into Drifts table info row mapping (4147:20376)",
    ),
    DroppedNode(
        id="4147:20386",
        name="Drifts table actions column",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail=(
            "checkbox column absorbed into Table mapping (4147:20384) "
            "via rowSelection prop"
        ),
    ),
    DroppedNode(
        id="4147:20387",
        name="Drifts table Entity Name column",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="column 1 of 7, captured by Table.dataSource",
    ),
    DroppedNode(
        id="4147:20388",
        name="Drifts table Entity type column",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="column 2 of 7, captured by Table.dataSource",
    ),
    DroppedNode(
        id="4147:20389",
        name="Drifts table Rule Name column (with tags)",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="column 3 of 7, link + Status/Tag captured by custom render",
    ),
    DroppedNode(
        id="4147:20390",
        name="Drifts table Severity column",
        type="FRAME",
        reason="folded_into_pattern",
        detail=(
            "column 4 of 7, status mini icons captured by custom render "
            "(StatusTriangleMiniIcon / StatusDiamondMiniIcon)"
        ),
    ),
    DroppedNode(
        id="4147:20413",
        name="Drifts table Status column",
        type="FRAME",
        reason="folded_into_pattern",
        detail="column 5 of 7, captured by Table.dataSource",
    ),
    DroppedNode(
        id="4147:20441",
        name="Drifts table Last Detected column",
        type="FRAME",
        reason="folded_into_pattern",
        detail="column 6 of 7, captured by Table.dataSource",
    ),
    DroppedNode(
        id="4147:20467",
        name="Drift Details title text",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Drift Details panel title slot",
    ),
    DroppedNode(
        id="4147:20468",
        name="Drift Details header actions",
        type="FRAME",
        reason="folded_into_pattern",
        detail=(
            "layout-toggle + close icon buttons absorbed into Panel "
            "actions slot"
        ),
    ),
    DroppedNode(
        id="4147:20475",
        name="Cluster abc Custom Rul 3 text",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Drift subhead title slot",
    ),
    DroppedNode(
        id="4147:20476",
        name="Sub-table pager controls",
        type="FRAME",
        reason="folded_into_pattern",
        detail="absorbed into Drift subhead mapping (4147:20473)",
    ),
    DroppedNode(
        id="4147:20480",
        name="Sub-table Attribute column",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="column 1 of 4, captured by Table.dataSource",
    ),
    DroppedNode(
        id="4147:20481",
        name="Sub-table Relation column",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="column 2 of 4, captured by Table.dataSource",
    ),
    DroppedNode(
        id="4147:20482",
        name="Sub-table Expected column",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="column 3 of 4, captured by Table.dataSource",
    ),
    DroppedNode(
        id="4147:20483",
        name="Sub-table Detected column",
        type="INSTANCE",
        reason="folded_into_pattern",
        detail="column 4 of 4, captured by Table.dataSource",
    ),
    DroppedNode(
        id="4147:20485",
        name="Ignore Details heading",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Ignore Details section title slot",
    ),
    DroppedNode(
        id="4147:20489",
        name="Ignored By label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Ignore Details section items[0]",
    ),
    DroppedNode(
        id="4147:20491",
        name="Ignored By value (shivam.roy@nutanix.com)",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Ignore Details section items[0]",
    ),
    DroppedNode(
        id="4147:20494",
        name="Ignored on label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Ignore Details section items[1]",
    ),
    DroppedNode(
        id="4147:20499",
        name="Reason to Ignore label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="captured in Ignore Details section items[2]",
    ),
    DroppedNode(
        id="4147:24908",
        name="'Close Drift Details' callout",
        type="INSTANCE",
        reason="invisible_decoration",
        detail=(
            "annotation callout pointing at the Close icon — design-time "
            "only, not part of the runtime UI"
        ),
    ),
    DroppedNode(
        id="4147:20502",
        name="Drifts table footer/overflow row",
        type="INSTANCE",
        reason="tiny_decorative",
        detail=(
            "design-time scratch row used by the Figma source; clipped "
            "to the visible viewport (page ends at y=12785)"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


SUMMARY: dict[str, int] = {
    "input_nodes": 857,
    "kept_for_mapping": len(AGENDA),
    "dropped_total": len(DROPPED),
    "agenda_size": len(AGENDA),
    "tokens_count": len(TOKENS),
    "warnings_count": 0,
    "max_depth": 20,
    "max_agenda": 100,
    "dropped_invisible_decoration": 3,
    "dropped_icon_internal": 1,
    "dropped_tiny_decorative": 10,
    "dropped_folded_into_pattern": 16,
    "dropped_captured_as_content_slot": 9,
}


# ---------------------------------------------------------------------------
# Assemble and write.
# ---------------------------------------------------------------------------


def main() -> None:
    tree = FigmaTreeMapping(
        layout_tree=LAYOUT_TREE,
        agenda=AGENDA,
        tokens=TOKENS,
        dropped=DROPPED,
        summary=SUMMARY,
        warnings=[],
    )
    out_path = (
        Path(__file__).resolve().parent.parent
        / "mocks"
        / "figma_tree"
        / "QjBuSKHooZN4GEzA2rJy6P__752_13805.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dumped = tree.model_dump(mode="json")
    out_path.write_text(json.dumps(dumped, indent=2) + "\n")
    restored = FigmaTreeMapping.model_validate(json.loads(out_path.read_text()))
    assert restored == tree, "round-trip mismatch — fix the source dataclasses"
    print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes)")
    print(
        f"agenda={len(AGENDA)} layout_tree={len(LAYOUT_TREE)} "
        f"tokens={len(TOKENS)} dropped={len(DROPPED)}"
    )


if __name__ == "__main__":
    main()
