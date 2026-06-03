"""Build the curated FigmaTreeMapping mock for node 753:20750
(Figma-basics file copy of the original 3800:49763 "Networking ~
Expanded Row" frame from Expand Clusters in PC).

The original Figma file (file key ``SzP22zLyApL9R5nsQYheeo``) is not
reachable through the Cursor Figma plugin, so the design was duplicated
into a permission-friendly file (``QjBuSKHooZN4GEzA2rJy6P``) and
re-anchored on node ``753:20750``. The mock content (agenda IDs,
bboxes, hex colours, …) is preserved verbatim — the only thing that
changed is the (file_key, node_id) the loader keys on, i.e. the
filename produced by ``main()``.

This is a hand-curated "perfect ideal output" — only the
high-quality, semantically meaningful regions appear in the
agenda. Repeated cells/rows are collapsed into a single
``Table`` mapping with ``content_slots.cell_count`` mirroring
the walker's pattern detector. The full Pydantic model
(:class:`prism_mcp.figma.models.FigmaTreeMapping`) is the source of
truth for the schema; serialising via ``model_dump(mode="json")``
makes sure the resulting JSON round-trips through
``FigmaTreeMapping.model_validate``.

Output:
    mocks/figma_tree/QjBuSKHooZN4GEzA2rJy6P__753_20750.json
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
# Helpers — these mirror what the real walker emits so the JSON looks
# indistinguishable from a fresh ``map_figma_tree`` run.
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
# Layout tree — parent/child JSX nesting, root first.
# ---------------------------------------------------------------------------


LAYOUT_TREE: list[LayoutNode] = [
    LayoutNode(
        id="3800:49764",
        name="Modal/Fullpage",
        role="composed-region",
        bbox=(7783.0, 10347.0, 1440.0, 1004.0),
        children_ids=["3800:49807", "3800:49780", "3800:50100", "3800:49765"],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=0.0,
            gap_consistent=True,
            confidence=1.0,
            absolute_children=[],
            flow_children=[
                "3800:49807",
                "3800:49780",
                "3800:50100",
                "3800:49765",
            ],
            rationale="figma_auto_layout",
        ),
    ),
    LayoutNode(
        id="3800:49807",
        name="Header",
        role="layout-container",
        bbox=(7783.0, 10347.0, 1440.0, 50.0),
        children_ids=["3800:49816", "3800:49810"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="space-between",
            align_items="center",
            gap=None,
            gap_consistent=False,
            confidence=0.92,
            flow_children=["3800:49816", "3800:49810"],
            rationale=(
                "row score 0.92 (title left @x=7803, controls right @x=9148, "
                "centered within 50 px header)"
            ),
        ),
    ),
    LayoutNode(
        id="3800:49810",
        name="Controls",
        role="button-group",
        bbox=(9148.0, 10366.0, 55.0, 12.0),
        children_ids=["3800:49811", "3800:49813", "3800:49814"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="end",
            align_items="center",
            gap=12.0,
            gap_consistent=True,
            confidence=0.95,
            flow_children=["3800:49811", "3800:49813", "3800:49814"],
            rationale="row score 0.95 (3 children left→right with 12 px gaps)",
        ),
    ),
    LayoutNode(
        id="3800:49780",
        name="Navigation/Subheader/Steps",
        role="tab-strip",
        bbox=(7783.0, 10397.0, 1440.0, 50.0),
        children_ids=["3800:49782"],
        layout=LayoutAnalysis(
            direction="single",
            justify_content="center",
            align_items="center",
            confidence=1.0,
            flow_children=["3800:49782"],
            rationale="single child container",
        ),
    ),
    LayoutNode(
        id="3800:49782",
        name="Steps container",
        role="composed-region",
        bbox=(8100.0, 10412.0, 806.0, 20.0),
        children_ids=[
            "3800:49783",
            "3800:49787",
            "3800:49791",
            "3800:49795",
            "3800:49799",
            "3800:49803",
        ],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="center",
            align_items="center",
            gap=30.0,
            gap_consistent=True,
            confidence=1.0,
            flow_children=[
                "3800:49783",
                "3800:49787",
                "3800:49791",
                "3800:49795",
                "3800:49799",
                "3800:49803",
            ],
            rationale=(
                "row score 1.00 (6 step items left→right with 30 px gaps, "
                "top-aligned within 0 px)"
            ),
        ),
    ),
    LayoutNode(
        id="3800:50100",
        name="Body",
        role="layout-container",
        bbox=(7783.0, 10457.0, 1440.0, 844.0),
        children_ids=["3800:49817", "3800:50101", "3800:49818"],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=12.0,
            gap_consistent=False,
            confidence=0.90,
            flow_children=["3800:49817", "3800:50101", "3800:49818"],
            rationale=(
                "column score 0.90 (instruction → label → table, "
                "left-aligned within 0 px)"
            ),
        ),
    ),
    LayoutNode(
        id="3800:49818",
        name="Selected Hosts Table",
        role="composed-region",
        bbox=(7803.0, 10534.0, 1400.0, 501.0),
        children_ids=["3800:49819", "3800:50130"],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=0.0,
            gap_consistent=True,
            confidence=1.0,
            flow_children=["3800:49819", "3800:50130"],
            rationale="figma_auto_layout",
        ),
    ),
    LayoutNode(
        id="3800:49819",
        name="Table/Normal Hosts",
        role="table-column",
        bbox=(7803.0, 10534.0, 1400.0, 101.0),
        children_ids=["3800:50120"],
        layout=LayoutAnalysis(
            direction="single",
            confidence=1.0,
            flow_children=["3800:50120"],
            rationale="single child container",
        ),
    ),
    LayoutNode(
        id="3800:50130",
        name="Uplinks sub-table (expanded)",
        role="composed-region",
        bbox=(7803.0, 10650.0, 1400.0, 350.0),
        children_ids=["3800:50131", "3800:50132"],
        layout=LayoutAnalysis(
            direction="column",
            justify_content="start",
            align_items="stretch",
            gap=8.0,
            gap_consistent=True,
            confidence=1.0,
            flow_children=["3800:50131", "3800:50132"],
            rationale="figma_auto_layout",
        ),
    ),
    LayoutNode(
        id="3800:49765",
        name="Footer",
        role="layout-container",
        bbox=(7783.0, 11301.0, 1440.0, 50.0),
        children_ids=["3800:49767", "3800:49772"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="space-between",
            align_items="center",
            gap=None,
            gap_consistent=False,
            confidence=0.94,
            flow_children=["3800:49767", "3800:49772"],
            rationale=(
                "row score 0.94 (back-button left @x=7794, action-group right "
                "@x=8928, centered within 50 px footer)"
            ),
        ),
    ),
    LayoutNode(
        id="3800:49772",
        name="Footer action group",
        role="button-group",
        bbox=(8928.0, 11310.0, 285.0, 32.0),
        children_ids=["3800:49773", "3800:49775", "3800:49777"],
        layout=LayoutAnalysis(
            direction="row",
            justify_content="end",
            align_items="center",
            gap=10.0,
            gap_consistent=True,
            confidence=1.0,
            flow_children=["3800:49773", "3800:49775", "3800:49777"],
            rationale=(
                "row score 1.00 (3 buttons left→right with 10 px gaps, "
                "vertically centered)"
            ),
        ),
    ),
]


# ---------------------------------------------------------------------------
# Agenda — one MappedRegion per logical Prism component decision.
# ---------------------------------------------------------------------------


_TOKEN_MAPPINGS_COMMON: list[TokenMapping] = [
    TokenMapping(
        hex="#FFFFFF",
        token_name="color/background/primary",
        token_hex="#FFFFFF",
        bucket="exact",
    ),
    TokenMapping(
        hex="#15171A",
        token_name="color/text/primary",
        token_hex="#15171A",
        bucket="exact",
    ),
    TokenMapping(
        hex="#586678",
        token_name="color/text/secondary",
        token_hex="#586678",
        bucket="exact",
    ),
    TokenMapping(
        hex="#EDF0F2",
        token_name="color/border/secondary",
        token_hex="#EDF0F2",
        bucket="exact",
    ),
]


AGENDA: list[MappedRegion] = [
    # ----- Modal shell -----
    MappedRegion(
        id="3800:49764",
        aliased_ids=["3800:49763", "753:20750"],
        name="Modal/Fullpage",
        role="composed-region",
        bbox=(7783.0, 10347.0, 1440.0, 1004.0),
        parent_chain=["Networking ~ Expanded Row"],
        content_slots={
            "title": "Expand Cluster - bigtwin11",
        },
        structural_hints=[
            "1440x1004 fullpage modal",
            "vertical stack: header / steps / body / footer",
        ],
        children_summary=(
            "GROUP Header(1 TEXT title + 1 FRAME Controls) "
            "FRAME Navigation/Subheader/Steps(1 FRAME Container) "
            "FRAME Body(1 TEXT instruction + 1 TEXT label + 1 FRAME table) "
            "GROUP Footer(1 FRAME Action/Button + 1 FRAME Buttons)"
        ),
        hex_colors=["#FFFFFF", "#EDF0F2"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            layout_mode="VERTICAL",
            opacity=None,
        ),
        reference_jsx_slice=(
            "{/* 3800:49764 */}\n<FullPageModal title=\"Expand Cluster - "
            "bigtwin11\" visible={true} onClose={...}>...</FullPageModal>"
        ),
        shape_bucket="modal",
        mapping=_mk_mapping(
            node_name="Modal/Fullpage",
            primary="FullPageModal",
            primary_recommendation="FullPageModal",
            primary_recommendation_rationale=(
                "name 'Modal/Fullpage' matches alias hint 'FullPageModal Modal'"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "FullPageModal",
                    score=0.42,
                    why=["modal", "fullpage", "fullpagemodal"],
                    summary=(
                        "Full-viewport modal with header, step nav, body, "
                        "footer slots."
                    ),
                ),
                _mk_candidate(
                    "Modal",
                    score=0.18,
                    why=["modal"],
                    summary="Generic dialog wrapper.",
                    source="both",
                ),
                _mk_candidate(
                    "Dialog",
                    score=0.09,
                    why=["modal", "dialog"],
                    summary="Dialog primitive used inside the modal family.",
                    source="hybrid",
                ),
            ],
            related=["NavigationHeader", "Steps", "Footer", "Button"],
            a11y_blocks=[
                (
                    "Set role=\"dialog\" and aria-modal=\"true\". Use "
                    "aria-labelledby to point at the title text."
                ),
                "Trap focus inside the modal while it's open.",
            ],
            token_mappings=_TOKEN_MAPPINGS_COMMON,
            decompositions=[
                "FullPageModal + NavigationHeader",
                "FullPageModal + Steps",
            ],
        ),
    ),
    # ----- Header -----
    MappedRegion(
        id="3800:49807",
        name="Header",
        role="layout-container",
        bbox=(7783.0, 10347.0, 1440.0, 50.0),
        parent_chain=["Networking ~ Expanded Row", "Modal/Fullpage"],
        content_slots={
            "title": "Expand Cluster - bigtwin11",
        },
        structural_hints=[
            "1440x50 horizontal header strip",
            "title left, controls right",
        ],
        children_summary="TEXT Title + FRAME Controls(QuestionMark + Cross)",
        hex_colors=["#FFFFFF", "#EDF0F2", "#15171A"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#EDF0F2",
            border_width=1.0,
            layout_mode="HORIZONTAL",
            padding=(0.0, 20.0, 0.0, 20.0),
        ),
        reference_jsx_slice=(
            "{/* 3800:49807 */}\n<FlexLayout className=\"header\" "
            "alignItems=\"center\" justifyContent=\"space-between\">"
            "{title}{controls}</FlexLayout>"
        ),
        shape_bucket="banner",
        mapping=_mk_mapping(
            node_name="Header",
            primary="FlexLayout",
            candidates=[
                _mk_candidate(
                    "FlexLayout",
                    score=0.31,
                    why=["header", "row", "flex"],
                    summary="Row/column wrapper with gap and alignment.",
                ),
                _mk_candidate(
                    "NavigationHeader",
                    score=0.22,
                    why=["header", "navigation"],
                    summary=(
                        "App-level navigation bar with title + controls slots."
                    ),
                ),
            ],
            related=["Paragraph", "IconButton"],
            token_mappings=_TOKEN_MAPPINGS_COMMON,
        ),
    ),
    MappedRegion(
        id="3800:49816",
        name="Header Title",
        role="text",
        bbox=(7803.0, 10364.0, 164.0, 16.0),
        parent_chain=["Modal/Fullpage", "Header"],
        content_slots={
            "value": "Expand Cluster - bigtwin11",
        },
        structural_hints=["14px semibold text"],
        children_summary="single TEXT node",
        hex_colors=["#15171A"],
        box_style=BoxStyle(),
        reference_jsx_slice=(
            "{/* 3800:49816 */}\n<Paragraph weight=\"medium\">"
            "Expand Cluster - bigtwin11</Paragraph>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Header Title",
            primary="Paragraph",
            candidates=[
                _mk_candidate(
                    "Paragraph",
                    score=0.27,
                    why=["text", "title"],
                    summary="Body text primitive (size, weight, color props).",
                ),
                _mk_candidate(
                    "TextLabel",
                    score=0.18,
                    why=["title", "label"],
                    summary="Inline label primitive.",
                ),
                _mk_candidate(
                    "Title",
                    score=0.12,
                    why=["title"],
                    summary="Heading primitive.",
                    source="hybrid",
                ),
            ],
            token_mappings=[_TOKEN_MAPPINGS_COMMON[1]],
        ),
    ),
    MappedRegion(
        id="3800:49810",
        name="Header Controls",
        role="button-group",
        bbox=(9148.0, 10366.0, 55.0, 12.0),
        parent_chain=["Modal/Fullpage", "Header"],
        content_slots={
            "items": ["help", "divider", "close"],
        },
        structural_hints=[
            "horizontal cluster of 2 icon buttons separated by a 1px divider",
        ],
        children_summary=(
            "FRAME Icon/Controls/Question Mark + RECTANGLE Divider + "
            "FRAME Icon/System/Cross"
        ),
        hex_colors=["#586678", "#B8BFCA"],
        box_style=BoxStyle(
            layout_mode="HORIZONTAL",
            gap=12.0,
        ),
        reference_jsx_slice=(
            "{/* 3800:49810 */}\n<FlexLayout itemGap=\"M\" alignItems=\""
            "center\">{helpBtn}{divider}{closeBtn}</FlexLayout>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Header Controls",
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
                    why=["button", "controls", "group"],
                    summary="Group of related buttons sharing context.",
                ),
                _mk_candidate(
                    "FlexLayout",
                    score=0.21,
                    why=["controls", "row"],
                    summary="Row/column wrapper with gap and alignment.",
                ),
            ],
            related=["IconButton", "Button"],
        ),
    ),
    MappedRegion(
        id="3800:49811",
        name="Help icon button",
        role="icon",
        bbox=(9148.0, 10366.0, 12.0, 12.0),
        parent_chain=["Modal/Fullpage", "Header", "Controls"],
        content_slots={
            "icon_name_hint": "QuestionIcon",
            "label": "Help",
        },
        structural_hints=["12x12 icon trigger"],
        children_summary="VECTOR Icon Background",
        hex_colors=["#586678"],
        box_style=BoxStyle(),
        reference_jsx_slice=(
            "{/* 3800:49811 */}\n<IconButton aria-label=\"Help\">"
            "<QuestionIcon size=\"small\" /></IconButton>"
        ),
        shape_bucket="icon",
        mapping=_mk_mapping(
            node_name="Help icon button",
            primary="IconButton",
            primary_recommendation="Icon",
            primary_recommendation_rationale=(
                "pattern role 'icon' → Icon (used inside IconButton)"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "IconButton",
                    score=0.34,
                    why=["icon", "button", "help"],
                    summary="Icon-only button (a11y label required).",
                ),
                _mk_candidate(
                    "QuestionIcon",
                    score=0.30,
                    why=["question", "help", "icon"],
                    summary="Question-mark icon glyph.",
                    kind="icon",
                ),
                _mk_candidate(
                    "Icon",
                    score=0.12,
                    why=["icon"],
                    summary="Generic icon primitive.",
                ),
            ],
        ),
    ),
    MappedRegion(
        id="3800:49814",
        name="Close icon button",
        role="icon",
        bbox=(9191.0, 10366.0, 12.0, 12.0),
        parent_chain=["Modal/Fullpage", "Header", "Controls"],
        content_slots={
            "icon_name_hint": "CloseIcon",
            "label": "Close",
        },
        structural_hints=["12x12 close-X glyph"],
        children_summary="VECTOR Icon Background",
        hex_colors=["#586678"],
        box_style=BoxStyle(),
        reference_jsx_slice=(
            "{/* 3800:49814 */}\n<IconButton aria-label=\"Close\">"
            "<CloseIcon size=\"small\" /></IconButton>"
        ),
        shape_bucket="icon",
        mapping=_mk_mapping(
            node_name="Close icon button",
            primary="IconButton",
            primary_recommendation="Icon",
            primary_recommendation_rationale=(
                "pattern role 'icon' → Icon (used inside IconButton)"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "IconButton",
                    score=0.36,
                    why=["icon", "button", "close"],
                    summary="Icon-only button (a11y label required).",
                ),
                _mk_candidate(
                    "CloseIcon",
                    score=0.32,
                    why=["close", "cross", "icon"],
                    summary="Close/cross icon glyph.",
                    kind="icon",
                ),
            ],
        ),
    ),
    # ----- Steps -----
    MappedRegion(
        id="3800:49780",
        aliased_ids=["3800:49782"],
        name="Navigation/Subheader/Steps",
        role="tab-strip",
        bbox=(7783.0, 10397.0, 1440.0, 50.0),
        parent_chain=["Modal/Fullpage"],
        content_slots={
            "items": [
                "Select Host",
                "Choose Host Type",
                "Configure Host",
                "Networking",
                "Software Check",
                "Review",
            ],
            "value": "Networking",
            "cell_count": 6,
        },
        structural_hints=[
            "horizontal step bar, 6 items, current=4 (Networking)",
            "first 3 marked finish, current process, last 2 wait",
        ],
        children_summary=(
            "FRAME Container(6 FRAME 'Step N' each with Status/Badge + TEXT)"
        ),
        hex_colors=["#1B6DC6", "#586678", "#FFFFFF", "#B8BFCA"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#EDF0F2",
            border_width=1.0,
            layout_mode="HORIZONTAL",
            padding=(15.0, 0.0, 15.0, 0.0),
        ),
        reference_jsx_slice=(
            "{/* 3800:49780 */}\n<Steps current={3} data={[\n"
            "  { title: 'Select Host' }, { title: 'Choose Host Type' },\n"
            "  { title: 'Configure Host' }, { title: 'Networking' },\n"
            "  { title: 'Software Check' }, { title: 'Review' }\n"
            "]} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Navigation/Subheader/Steps",
            primary="Steps",
            primary_recommendation="TabBar",
            primary_recommendation_rationale=(
                "pattern role 'tab-strip' → TabBar (overridden to Steps by "
                "alias hint 'Subheader Nav Header')"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "Steps",
                    score=0.46,
                    why=["steps", "navigation", "subheader"],
                    summary=(
                        "Step indicator for multi-step workflows with finish/"
                        "process/wait states."
                    ),
                ),
                _mk_candidate(
                    "TabBar",
                    score=0.21,
                    why=["tab", "navigation"],
                    summary="Horizontal tab strip.",
                ),
                _mk_candidate(
                    "NavigationHeader",
                    score=0.14,
                    why=["navigation", "header"],
                    summary="Top app-bar.",
                    source="hybrid",
                ),
            ],
            related=["StepItem"],
            a11y_blocks=[
                "Render role=\"tablist\" with aria-orientation=\"horizontal\".",
                (
                    "Each step is role=\"tab\" with aria-selected and "
                    "aria-current=\"step\" for the active item."
                ),
            ],
            token_mappings=[
                TokenMapping(
                    hex="#1B6DC6",
                    token_name="color/primary/500",
                    token_hex="#1B6DC6",
                    bucket="exact",
                ),
                TokenMapping(
                    hex="#B8BFCA",
                    token_name="color/border/interactive",
                    token_hex="#B8BFCA",
                    bucket="exact",
                ),
            ],
            decompositions=["Steps + StepItem"],
        ),
    ),
    # ----- Body: instruction text -----
    MappedRegion(
        id="3800:49817",
        name="Instruction",
        role="text",
        bbox=(7803.0, 10467.0, 383.0, 21.0),
        parent_chain=["Modal/Fullpage", "Body"],
        content_slots={
            "value": (
                "Select the appropriate active and backup uplinks for the "
                "hosts."
            ),
        },
        structural_hints=["13px body copy"],
        children_summary="single TEXT node",
        hex_colors=["#15171A"],
        box_style=BoxStyle(),
        reference_jsx_slice=(
            "{/* 3800:49817 */}\n<Paragraph>"
            "Select the appropriate active and backup uplinks for the hosts."
            "</Paragraph>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Instruction",
            primary="Paragraph",
            candidates=[
                _mk_candidate(
                    "Paragraph",
                    score=0.29,
                    why=["text", "paragraph", "body"],
                    summary="Body text primitive (size, weight, color props).",
                ),
            ],
            token_mappings=[_TOKEN_MAPPINGS_COMMON[1]],
        ),
    ),
    # ----- Body: list label -----
    MappedRegion(
        id="3800:50101",
        name="List label",
        role="text",
        bbox=(7803.0, 10502.0, 200.0, 16.0),
        parent_chain=["Modal/Fullpage", "Body"],
        content_slots={
            "value": "List of 2 Selected Hosts",
        },
        structural_hints=["13px bold label", "selection summary"],
        children_summary="single TEXT node",
        hex_colors=["#15171A"],
        box_style=BoxStyle(),
        reference_jsx_slice=(
            "{/* 3800:50101 */}\n<TextLabel size={TextLabel.TEXT_LABEL_SIZE."
            "MEDIUM} weight=\"bold\">List of 2 Selected Hosts</TextLabel>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="List label",
            primary="TextLabel",
            candidates=[
                _mk_candidate(
                    "TextLabel",
                    score=0.31,
                    why=["label", "text", "list"],
                    summary="Inline label primitive (size + weight props).",
                ),
                _mk_candidate(
                    "Paragraph",
                    score=0.17,
                    why=["text"],
                    summary="Body text primitive.",
                ),
            ],
            token_mappings=[_TOKEN_MAPPINGS_COMMON[1]],
        ),
    ),
    # ----- Hosts table (the outer table with 2 visible rows) -----
    MappedRegion(
        id="3800:49819",
        aliased_ids=["3800:49818", "3800:49820"],
        name="Selected Hosts Table",
        role="table-column",
        bbox=(7803.0, 10534.0, 1400.0, 121.0),
        parent_chain=["Modal/Fullpage", "Body", "Frame 1"],
        content_slots={
            "header": "Model/Serial Number | Host Position | Active Uplink "
            "| Backup Uplink",
            "items": ["NX-3060-G5 19FM6F160438", "NX-3060-G5 19FM6F160439"],
            "cell_count": 2,
            "first_cell_sample": (
                "row1: NX-3060-G5 | 19FM6F160438 | B | eth 0 | "
                "eth 1, eth 2, eth 3, eth 4, eth 5, eth 6, eth 7"
            ),
        },
        structural_hints=[
            "4-column hosts table with expandable rows",
            "row 1 expanded, row 2 collapsed",
            "row 2 active/backup cells render italic placeholder",
        ],
        children_summary=(
            "4 FRAME 'Table/Column' (Model/Serial, Host Position, Active "
            "Uplink, Backup Uplink) each with 2 'Table/Table Cell' rows"
        ),
        hex_colors=["#FFFFFF", "#EDF0F2", "#15171A", "#1B6DC6"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#EDF0F2",
            border_width=1.0,
            corner_radius=4.0,
            layout_mode="VERTICAL",
        ),
        reference_jsx_slice=(
            "{/* 3800:49819 */}\n<Table aria-label=\"Selected Hosts\"\n"
            "  columns={HOST_COLUMNS} dataSource={HOST_ROWS} rowKey=\"key\"\n"
            "  rowExpand={{ rows: ['host-1'], render: row => "
            "<UplinkSubTable /> }}\n"
            "  sort={{ sortable: ['model', 'position', 'active', 'backup'] }}"
            "\n/>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Selected Hosts Table",
            primary="Table",
            primary_recommendation="TableColumn",
            primary_recommendation_rationale=(
                "pattern role 'table-column' → TableColumn (overridden to "
                "Table at composition; outer wrapper is Table, inner "
                "agenda entries can use TableColumn for per-column metadata)"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "Table",
                    score=0.52,
                    why=["table", "column", "row", "cell"],
                    summary=(
                        "Data table with sortable columns, row expansion, "
                        "pagination."
                    ),
                ),
                _mk_candidate(
                    "TableColumn",
                    score=0.34,
                    why=["table", "column"],
                    summary="Single column descriptor inside a Table.",
                ),
                _mk_candidate(
                    "DataTable",
                    score=0.12,
                    why=["data", "table"],
                    summary="Higher-level data-grid wrapper.",
                    source="hybrid",
                ),
            ],
            related=["TableColumn", "TableCell", "TableRow", "Sorter"],
            a11y_blocks=[
                (
                    "Wrap in role=\"table\" (Prism's Table does this by "
                    "default). Provide an aria-label for the table."
                ),
                (
                    "Mark sortable headers with aria-sort and expose visible "
                    "sort indicators (already provided by sort prop)."
                ),
                (
                    "Expandable rows: trigger has aria-expanded; expanded "
                    "panel uses role=\"row\" and the right aria-controls."
                ),
            ],
            token_mappings=_TOKEN_MAPPINGS_COMMON,
            decompositions=[
                "Table + TableColumn",
                "Table + Sorter",
            ],
        ),
    ),
    # ----- Uplinks expanded sub-table -----
    MappedRegion(
        id="3800:50131",
        name="Uplinks search",
        role="instance",
        bbox=(7818.0, 10661.0, 360.0, 28.0),
        parent_chain=[
            "Modal/Fullpage",
            "Body",
            "Selected Hosts Table",
            "Uplinks sub-table",
        ],
        content_slots={
            "placeholder": "Type to filter uplinks by label or LLDP Neighbor",
        },
        structural_hints=["input with leading search icon", "360px width"],
        children_summary="FRAME Search icon + TEXT placeholder",
        hex_colors=["#FFFFFF", "#B8BFCA", "#586678"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#B8BFCA",
            border_width=1.0,
            corner_radius=2.0,
        ),
        reference_jsx_slice=(
            "{/* 3800:50131 */}\n<Input search={true} placeholder=\"Type to "
            "filter uplinks by label or LLDP Neighbor\" />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Uplinks search",
            primary="Input",
            candidates=[
                _mk_candidate(
                    "Input",
                    score=0.41,
                    why=["search", "input", "filter"],
                    summary=(
                        "Text input. Set `search={true}` for the search-icon "
                        "variant."
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
    MappedRegion(
        id="3800:50132",
        name="Uplinks Table",
        role="table-column",
        bbox=(7803.0, 10697.0, 1400.0, 303.0),
        parent_chain=[
            "Modal/Fullpage",
            "Body",
            "Selected Hosts Table",
            "Uplinks sub-table",
        ],
        content_slots={
            "header": "Uplink | Status | Label | LLDP Neighbor | Interface "
            "Speed | Virtual Switch | Bonding Type | Uplink Type",
            "items": [
                "eth 0 / Up / label123 / / 10G / vs0 / Active-Backup / Active",
                "eth 1 / Up / label273 / (VLAN tooltip) / 10G / vs0 / "
                "Active-Backup / Backup",
                "eth 2 / Up / label423 / sample-uplink-2 / 1G / vs0 / "
                "Active-Backup / Backup",
                "eth 3 / Down / label573 / sample-uplink-1 / 1G / vs0 / "
                "Active-Backup / Backup",
                "eth 4 / Up / label723 / sample-uplink-3 / 1G / vs0 / "
                "Active-Backup / Backup",
                "eth 7 / Up / label873 / sample-uplink-3 / 1G / vs0 / "
                "Active-Backup / Backup",
            ],
            "cell_count": 6,
        },
        structural_hints=[
            "8-column uplinks table",
            "Label column header has bell/info icon",
            "Virtual Switch + Uplink Type columns use inline Select cells",
            "LLDP Neighbor cell shows always-open Tooltip on eth-1",
        ],
        children_summary=(
            "8 FRAME 'Table/Column' with 6 Table/Table Cell rows each"
        ),
        hex_colors=[
            "#F4F6F8",
            "#FFFFFF",
            "#EDF0F2",
            "#1B8836",
            "#D02550",
            "#15171A",
        ],
        box_style=BoxStyle(
            background_color="#F4F6F8",
            corner_radius=4.0,
            padding=(12.0, 16.0, 16.0, 16.0),
            layout_mode="VERTICAL",
            gap=8.0,
        ),
        reference_jsx_slice=(
            "{/* 3800:50132 */}\n<Table aria-label=\"Uplinks\"\n"
            "  columns={UPLINK_COLUMNS} dataSource={UPLINK_ROWS} "
            "rowKey=\"key\"\n"
            "  sort={{ sortable: ['uplink','status','label','lldp','speed',"
            "'vs','bonding','uplinkType'] }}\n/>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Uplinks Table",
            primary="Table",
            primary_recommendation="TableColumn",
            primary_recommendation_rationale=(
                "pattern role 'table-column' → TableColumn (composed inside "
                "an outer Table)"
            ),
            primary_recommendation_confidence=1.0,
            candidates=[
                _mk_candidate(
                    "Table",
                    score=0.54,
                    why=["table", "column", "uplink"],
                    summary="Data table with sortable columns.",
                ),
                _mk_candidate(
                    "TableColumn",
                    score=0.32,
                    why=["table", "column"],
                    summary="Single column descriptor.",
                ),
            ],
            related=[
                "TableColumn",
                "TableCell",
                "TableRow",
                "Select",
                "Tooltip",
                "Input",
            ],
            a11y_blocks=[
                (
                    "Wrap in role=\"table\". Provide aria-label and "
                    "aria-sort per column."
                ),
                (
                    "Status dot must be exposed via accessible text (e.g. "
                    "'Up'/'Down') alongside the colour."
                ),
            ],
            token_mappings=[
                TokenMapping(
                    hex="#F4F6F8",
                    token_name="color/background/neutral",
                    token_hex="#F4F6F8",
                    bucket="exact",
                ),
                TokenMapping(
                    hex="#1B8836",
                    token_name="color/status/success",
                    token_hex="#1B8836",
                    bucket="exact",
                ),
                TokenMapping(
                    hex="#D02550",
                    token_name="color/status/critical",
                    token_hex="#D02550",
                    bucket="exact",
                ),
            ],
        ),
    ),
    # ----- LLDP cell tooltip (the highlighted feature in the design) -----
    MappedRegion(
        id="3800:50145",
        name="VLAN/MAC tooltip",
        role="instance",
        bbox=(8430.0, 10761.0, 230.0, 56.0),
        parent_chain=[
            "Modal/Fullpage",
            "Body",
            "Uplinks Table",
            "row eth-1",
            "LLDP Neighbor",
        ],
        content_slots={
            "title": "VLAN / MAC tooltip",
            "items": ["VLAN: vlan.27", "MAC Address: 3E:42:68:8F:6E:4F"],
        },
        structural_hints=[
            "dark popover anchored to LLDP Neighbor cell of eth-1",
            "open by default in the design (acts as call-out)",
        ],
        children_summary=(
            "FRAME tooltip with 2 'label : value' rows + tail caret"
        ),
        hex_colors=["#2C3038", "#FFFFFF", "#B8BFCA"],
        box_style=BoxStyle(
            background_color="#2C3038",
            corner_radius=3.0,
            padding=(6.0, 10.0, 6.0, 10.0),
            gap=4.0,
            has_shadow=True,
        ),
        reference_jsx_slice=(
            "{/* 3800:50145 */}\n<Tooltip visible={true} placement=\"right\""
            "\n  content={<dl><dt>VLAN</dt><dd>vlan.27</dd>"
            "<dt>MAC Address</dt><dd>3E:42:68:8F:6E:4F</dd></dl>}\n>"
            "<span className=\"lldp-anchor\" /></Tooltip>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="VLAN/MAC tooltip",
            primary="Tooltip",
            candidates=[
                _mk_candidate(
                    "Tooltip",
                    score=0.49,
                    why=["tooltip", "popover", "vlan"],
                    summary=(
                        "Informative popover triggered by hover/focus, "
                        "supports controlled `visible` for design-time."
                    ),
                ),
                _mk_candidate(
                    "Popover",
                    score=0.21,
                    why=["popover"],
                    summary="Generic popover container.",
                    source="hybrid",
                ),
            ],
            related=["PortalProvider"],
            a11y_blocks=[
                (
                    "Tooltip content must have role=\"tooltip\" and be linked"
                    " to the trigger via aria-describedby."
                ),
            ],
            token_mappings=[
                TokenMapping(
                    hex="#2C3038",
                    token_name="color/background/inverse",
                    token_hex="#2C3038",
                    bucket="exact",
                ),
            ],
        ),
    ),
    # ----- Virtual Switch select (collapsed: one MappedRegion stands for
    #       all 6 rows; cell_count carries the multiplicity) -----
    MappedRegion(
        id="3800:50160",
        name="Virtual Switch Select",
        role="instance",
        bbox=(8810.0, 10750.0, 100.0, 28.0),
        parent_chain=[
            "Modal/Fullpage",
            "Body",
            "Uplinks Table",
            "Virtual Switch column",
        ],
        content_slots={
            "label": "Virtual Switch",
            "value": "vs0",
            "items": ["vs0", "vs1", "vs2", "vs3"],
            "cell_count": 6,
        },
        structural_hints=[
            "100px clearable select",
            "shown × clear icon + : caret per row",
        ],
        children_summary="FRAME pill with TEXT + Icon × + Icon :",
        hex_colors=["#FFFFFF", "#B8BFCA"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#B8BFCA",
            border_width=1.0,
            corner_radius=2.0,
        ),
        reference_jsx_slice=(
            "{/* 3800:50160 */}\n<Select rowsData={VS_OPTIONS} "
            "selectedRow={vs0} clearable={true} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Virtual Switch Select",
            primary="Select",
            candidates=[
                _mk_candidate(
                    "Select",
                    score=0.46,
                    why=["select", "dropdown", "switch"],
                    summary="Single-select dropdown with clearable prop.",
                ),
                _mk_candidate(
                    "SelectDropdown",
                    score=0.21,
                    why=["select", "dropdown"],
                    summary="Lower-level dropdown primitive.",
                ),
            ],
            related=["Input", "Dropdown"],
        ),
    ),
    # ----- Uplink Type select (collapsed for 6 rows) -----
    MappedRegion(
        id="3800:50180",
        name="Uplink Type Select",
        role="instance",
        bbox=(9080.0, 10750.0, 116.0, 28.0),
        parent_chain=[
            "Modal/Fullpage",
            "Body",
            "Uplinks Table",
            "Uplink Type column",
        ],
        content_slots={
            "label": "Uplink Type",
            "items": ["Active", "Backup"],
            "value": "Backup",
            "cell_count": 6,
        },
        structural_hints=[
            "116px clearable select",
            "row 1 = Active, rows 2-6 = Backup",
        ],
        children_summary="FRAME pill with TEXT + Icon × + Icon :",
        hex_colors=["#FFFFFF", "#B8BFCA"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#B8BFCA",
            border_width=1.0,
            corner_radius=2.0,
        ),
        reference_jsx_slice=(
            "{/* 3800:50180 */}\n<Select rowsData={UPLINK_TYPE_OPTIONS} "
            "selectedRow={backup} clearable={true} />"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Uplink Type Select",
            primary="Select",
            candidates=[
                _mk_candidate(
                    "Select",
                    score=0.45,
                    why=["select", "uplink", "type"],
                    summary="Single-select dropdown with clearable prop.",
                ),
            ],
            related=["Input", "Dropdown"],
        ),
    ),
    # ----- Status dot (Up/Down indicator inside Status column) -----
    MappedRegion(
        id="3800:50200",
        name="Status indicator",
        role="instance",
        bbox=(8240.0, 10755.0, 50.0, 16.0),
        parent_chain=[
            "Modal/Fullpage",
            "Body",
            "Uplinks Table",
            "Status column",
        ],
        content_slots={
            "items": ["Up", "Down"],
            "cell_count": 6,
        },
        structural_hints=[
            "circular dot (8px) + text label",
            "filled-green for Up, ring-red for Down",
        ],
        children_summary="ELLIPSE dot + TEXT label",
        hex_colors=["#1B8836", "#D02550", "#15171A"],
        box_style=BoxStyle(),
        reference_jsx_slice=(
            "{/* 3800:50200 */}\n<StatusDot value=\"Up\" /> { /* or "
            "\"Down\" → renders an outline red dot */ }"
        ),
        shape_bucket="icon",
        mapping=_mk_mapping(
            node_name="Status indicator",
            primary="StatusIcon",
            candidates=[
                _mk_candidate(
                    "StatusIcon",
                    score=0.39,
                    why=["status", "icon", "dot"],
                    summary="Status pill/icon for Up/Down/Warning states.",
                ),
                _mk_candidate(
                    "Badge",
                    score=0.16,
                    why=["badge", "status"],
                    summary="Counter / status pill.",
                ),
            ],
            related=["Tag"],
        ),
    ),
    # ----- Footer -----
    MappedRegion(
        id="3800:49765",
        name="Footer",
        role="layout-container",
        bbox=(7783.0, 11301.0, 1440.0, 50.0),
        parent_chain=["Modal/Fullpage"],
        content_slots={
            "items": ["Back", "Cancel", "Skip Networking", "Next"],
        },
        structural_hints=[
            "1440x50 horizontal footer strip",
            "Back left, action group right (Cancel / Skip Networking / Next)",
            "Next button disabled",
        ],
        children_summary=(
            "FRAME Action/Button Back + FRAME Buttons(Cancel + Skip "
            "Networking + Next)"
        ),
        hex_colors=["#FFFFFF", "#EDF0F2"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#EDF0F2",
            border_width=1.0,
            layout_mode="HORIZONTAL",
            padding=(9.0, 20.0, 9.0, 20.0),
        ),
        reference_jsx_slice=(
            "{/* 3800:49765 */}\n<FlexLayout className=\"footer\" "
            "alignItems=\"center\" justifyContent=\"space-between\">"
            "<Button type=\"secondary\"><BackIcon />Back</Button>"
            "<ButtonGroup>{cancel}{skip}{next}</ButtonGroup></FlexLayout>"
        ),
        shape_bucket="banner",
        mapping=_mk_mapping(
            node_name="Footer",
            primary="FlexLayout",
            candidates=[
                _mk_candidate(
                    "FlexLayout",
                    score=0.30,
                    why=["footer", "row"],
                    summary="Row/column wrapper.",
                ),
                _mk_candidate(
                    "ButtonGroup",
                    score=0.18,
                    why=["buttons", "group"],
                    summary="Group of buttons.",
                ),
            ],
        ),
    ),
    MappedRegion(
        id="3800:49767",
        name="Back button",
        role="instance",
        bbox=(7794.0, 11310.0, 78.0, 32.0),
        parent_chain=["Modal/Fullpage", "Footer"],
        content_slots={
            "label": "Back",
            "icon_name_hint": "BackIcon",
        },
        structural_hints=[
            "secondary button with leading icon",
            "78x32 px",
        ],
        children_summary="FRAME Content(Button Icon + TEXT 'Back')",
        hex_colors=["#FFFFFF", "#B8BFCA", "#15171A"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#B8BFCA",
            border_width=1.0,
            corner_radius=2.0,
            padding=(8.0, 15.0, 8.0, 15.0),
        ),
        reference_jsx_slice=(
            "{/* 3800:49767 */}\n<Button type={Button.ButtonTypes.SECONDARY}>"
            "<BackIcon size=\"small\" />Back</Button>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Back button",
            primary="Button",
            candidates=[
                _mk_candidate(
                    "Button",
                    score=0.41,
                    why=["button", "back", "secondary"],
                    summary="Standard button with type/size variants.",
                ),
                _mk_candidate(
                    "BackIcon",
                    score=0.20,
                    why=["back", "arrow", "icon"],
                    summary="Back/left-chevron icon.",
                    kind="icon",
                ),
            ],
            related=["BackIcon"],
        ),
    ),
    MappedRegion(
        id="3800:49773",
        name="Cancel button",
        role="instance",
        bbox=(8928.0, 11310.0, 73.0, 32.0),
        parent_chain=["Modal/Fullpage", "Footer", "Footer action group"],
        content_slots={
            "label": "Cancel",
        },
        structural_hints=["secondary button", "73x32 px"],
        children_summary="single TEXT label",
        hex_colors=["#FFFFFF", "#B8BFCA", "#15171A"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#B8BFCA",
            border_width=1.0,
            corner_radius=2.0,
            padding=(8.0, 15.0, 8.0, 15.0),
        ),
        reference_jsx_slice=(
            "{/* 3800:49773 */}\n<Button type={Button.ButtonTypes.SECONDARY}>"
            "Cancel</Button>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Cancel button",
            primary="Button",
            candidates=[
                _mk_candidate(
                    "Button",
                    score=0.41,
                    why=["button", "cancel", "secondary"],
                    summary="Standard button.",
                ),
            ],
        ),
    ),
    MappedRegion(
        id="3800:49775",
        name="Skip Networking button",
        role="instance",
        bbox=(9011.0, 11310.0, 133.0, 32.0),
        parent_chain=["Modal/Fullpage", "Footer", "Footer action group"],
        content_slots={
            "label": "Skip Networking",
        },
        structural_hints=["secondary button", "133x32 px"],
        children_summary="single TEXT label",
        hex_colors=["#FFFFFF", "#B8BFCA", "#15171A"],
        box_style=BoxStyle(
            background_color="#FFFFFF",
            border_color="#B8BFCA",
            border_width=1.0,
            corner_radius=2.0,
            padding=(8.0, 15.0, 8.0, 15.0),
        ),
        reference_jsx_slice=(
            "{/* 3800:49775 */}\n<Button type={Button.ButtonTypes.SECONDARY}>"
            "Skip Networking</Button>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Skip Networking button",
            primary="Button",
            candidates=[
                _mk_candidate(
                    "Button",
                    score=0.41,
                    why=["button", "skip", "secondary"],
                    summary="Standard button.",
                ),
            ],
        ),
    ),
    MappedRegion(
        id="3800:49777",
        name="Next button",
        role="instance",
        bbox=(9154.0, 11310.0, 59.0, 32.0),
        parent_chain=["Modal/Fullpage", "Footer", "Footer action group"],
        content_slots={
            "label": "Next",
        },
        structural_hints=[
            "primary button, disabled state",
            "59x32 px",
        ],
        children_summary="single TEXT label",
        hex_colors=["#1B6DC6", "#FFFFFF"],
        box_style=BoxStyle(
            background_color="#1B6DC6",
            corner_radius=2.0,
            padding=(8.0, 15.0, 8.0, 15.0),
            opacity=0.5,
        ),
        reference_jsx_slice=(
            "{/* 3800:49777 */}\n<Button disabled={true}>Next</Button>"
        ),
        shape_bucket="block",
        mapping=_mk_mapping(
            node_name="Next button",
            primary="Button",
            candidates=[
                _mk_candidate(
                    "Button",
                    score=0.43,
                    why=["button", "next", "primary"],
                    summary="Standard button.",
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
]


# ---------------------------------------------------------------------------
# Tokens — every visible hex in the design mapped to its Prism token name.
# ---------------------------------------------------------------------------


TOKENS: dict[str, str] = {
    "#FFFFFF": "color/background/primary",
    "#15171A": "color/text/primary",
    "#586678": "color/text/secondary",
    "#8990A3": "color/text/secondary-label",
    "#EDF0F2": "color/border/secondary",
    "#B8BFCA": "color/border/interactive",
    "#F4F6F8": "color/background/neutral",
    "#1B6DC6": "color/primary/500",
    "#1B8836": "color/status/success",
    "#D02550": "color/status/critical",
    "#2C3038": "color/background/inverse",
}


# ---------------------------------------------------------------------------
# Dropped — audit trail for nodes the walker discarded.
# ---------------------------------------------------------------------------


DROPPED: list[DroppedNode] = [
    DroppedNode(
        id="3800:49763",
        name="Networking ~ Expanded Row",
        type="FRAME",
        reason="same_bbox_passthrough_collapsed",
        detail="page-level frame collapsed into 3800:49764 (Modal/Fullpage)",
    ),
    DroppedNode(
        id="3800:49766",
        name="Footer Background",
        type="RECTANGLE",
        reason="invisible_decoration",
        detail="background fill captured by parent's box_style",
    ),
    DroppedNode(
        id="3800:49808",
        name="Header Background",
        type="RECTANGLE",
        reason="invisible_decoration",
        detail="background fill captured by parent's box_style",
    ),
    DroppedNode(
        id="3800:49809",
        name="Header Divider",
        type="RECTANGLE",
        reason="invisible_decoration",
        detail="1px divider captured by parent's border",
    ),
    DroppedNode(
        id="3800:49781",
        name="Steps Header Divider",
        type="RECTANGLE",
        reason="invisible_decoration",
        detail="1px divider captured by parent's border",
    ),
    DroppedNode(
        id="3800:49779",
        name="Footer Divider",
        type="RECTANGLE",
        reason="invisible_decoration",
        detail="1px divider captured by parent's border",
    ),
    DroppedNode(
        id="3800:49813",
        name="Header Controls Divider",
        type="RECTANGLE",
        reason="tiny_decorative",
        detail="1x10 separator between help and close icons",
    ),
    DroppedNode(
        id="3800:49812",
        name="Question icon glyph",
        type="VECTOR",
        reason="icon_internal",
        detail="captured by 3800:49811 IconButton mapping",
    ),
    DroppedNode(
        id="3800:49815",
        name="Close icon glyph",
        type="VECTOR",
        reason="icon_internal",
        detail="captured by 3800:49814 IconButton mapping",
    ),
    DroppedNode(
        id="3800:49770",
        name="Back icon glyph",
        type="VECTOR",
        reason="icon_internal",
        detail="captured by 3800:49767 Back button mapping",
    ),
    DroppedNode(
        id="3800:49784",
        name="Step 1 badge",
        type="FRAME",
        reason="folded_into_pattern",
        detail="absorbed into Steps mapping (3800:49780)",
    ),
    DroppedNode(
        id="3800:49786",
        name="Step 1 label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="'Select Host' captured in items[0] of Steps",
    ),
    DroppedNode(
        id="3800:49788",
        name="Step 2 badge",
        type="FRAME",
        reason="folded_into_pattern",
        detail="absorbed into Steps mapping",
    ),
    DroppedNode(
        id="3800:49790",
        name="Step 2 label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="'Choose Host Type' captured in items[1] of Steps",
    ),
    DroppedNode(
        id="3800:49792",
        name="Step 3 badge",
        type="FRAME",
        reason="folded_into_pattern",
        detail="absorbed into Steps mapping",
    ),
    DroppedNode(
        id="3800:49794",
        name="Step 3 label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="'Configure Host' captured in items[2] of Steps",
    ),
    DroppedNode(
        id="3800:49796",
        name="Step 4 badge",
        type="FRAME",
        reason="folded_into_pattern",
        detail="absorbed into Steps mapping (current)",
    ),
    DroppedNode(
        id="3800:49798",
        name="Step 4 label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="'Networking' captured in items[3] (current) of Steps",
    ),
    DroppedNode(
        id="3800:49800",
        name="Step 5 badge",
        type="FRAME",
        reason="folded_into_pattern",
        detail="absorbed into Steps mapping",
    ),
    DroppedNode(
        id="3800:49802",
        name="Step 5 label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="'Software Check' captured in items[4] of Steps",
    ),
    DroppedNode(
        id="3800:49804",
        name="Step 6 badge",
        type="FRAME",
        reason="folded_into_pattern",
        detail="absorbed into Steps mapping",
    ),
    DroppedNode(
        id="3800:49806",
        name="Step 6 label",
        type="TEXT",
        reason="captured_as_content_slot",
        detail="'Review' captured in items[5] of Steps",
    ),
    DroppedNode(
        id="3800:49823",
        name="Model/Serial vertical divider",
        type="RECTANGLE",
        reason="invisible_decoration",
        detail="captured by Table's column divider styling",
    ),
    DroppedNode(
        id="3800:49829",
        name="Model/Serial Row 1 cell",
        type="FRAME",
        reason="captured_as_content_slot",
        detail="rendered via dataSource[0] in hosts Table",
    ),
    DroppedNode(
        id="3800:49834",
        name="Model/Serial Row 2 cell",
        type="FRAME",
        reason="captured_as_content_slot",
        detail="rendered via dataSource[1] in hosts Table",
    ),
    DroppedNode(
        id="3800:50121",
        name="Hosts table cells y > 10700 (overflow)",
        type="FRAME",
        reason="tiny_decorative",
        detail=(
            "Figma source contains 17 additional hidden rows used as design "
            "scratch; clipped to the visible viewport (modal viewport ends "
            "at y=11247)"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Summary.
# ---------------------------------------------------------------------------


SUMMARY: dict[str, int] = {
    "input_nodes": 386,
    "kept_for_mapping": len(AGENDA),
    "dropped_total": len(DROPPED),
    "agenda_size": len(AGENDA),
    "tokens_count": len(TOKENS),
    "warnings_count": 0,
    "max_depth": 20,
    "max_agenda": 100,
    "dropped_invisible_decoration": 5,
    "dropped_icon_internal": 3,
    "dropped_tiny_decorative": 2,
    "dropped_folded_into_pattern": 6,
    "dropped_captured_as_content_slot": 9,
    "dropped_same_bbox_passthrough_collapsed": 1,
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
        / "QjBuSKHooZN4GEzA2rJy6P__753_20750.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # mode="json" emits primitives only (tuples → lists, BaseModel→dict).
    dumped = tree.model_dump(mode="json")
    out_path.write_text(json.dumps(dumped, indent=2) + "\n")
    # Sanity check: round-trips back through the model.
    restored = FigmaTreeMapping.model_validate(json.loads(out_path.read_text()))
    assert restored == tree, "round-trip mismatch — fix the source dataclasses"
    print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes)")
    print(
        f"agenda={len(AGENDA)} layout_tree={len(LAYOUT_TREE)} "
        f"tokens={len(TOKENS)} dropped={len(DROPPED)}"
    )


if __name__ == "__main__":
    main()
