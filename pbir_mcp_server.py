#!/usr/bin/env python
"""MCP server for editing Power BI PBIR report definitions.

The server intentionally uses only the Python standard library so Codex can
launch it without a package install step.
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import io
import json
import re
import shutil
import sys
import tempfile
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SERVER_NAME = "powerbi-pbir-report-mcp"
SERVER_VERSION = "0.2.0"

PAGE_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.0.0/schema.json"
PAGES_METADATA_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.1.0/schema.json"
REPORT_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.0.0/schema.json"
VERSION_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json"
VISUAL_CONTAINER_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.9.0/schema.json"

DEFAULT_THEME_NAME = "CY26SU05"
DEFAULT_THEME_VERSION = {"visual": "2.9.0", "page": "2.0.0", "report": "3.0.0"}
DEFAULT_PAGE_SIZE = {"width": 1280, "height": 720}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PAGE_DISPLAY_OPTIONS = {"DeprecatedDynamic", "FitToPage", "FitToWidth", "ActualSize", "ActualSizeTopLeft"}
PAGE_TOP_LEVEL_KEYS = {
    "$schema", "name", "displayName", "displayOption", "height", "width",
    "filterConfig", "pageBinding", "objects", "type", "visibility",
    "visualInteractions", "autoPageGenerationConfig", "annotations", "howCreated",
}
PAGES_METADATA_KEYS = {"$schema", "pageOrder", "activePageName", "landingPageName"}
POSITION_KEYS = {"x", "y", "z", "height", "width", "tabOrder", "angle"}
VISUAL_CONTAINER_KEYS = {
    "$schema", "name", "position", "visual", "visualGroup", "parentGroupName",
    "filterConfig", "isHidden", "annotations", "howCreated",
}
VISUAL_GROUP_MODES = {"ScaleMode", "ScrollMode"}

# Power BI only renders a visual's data when the queryState role names match the
# data roles in that visual type's capabilities. A cartesian chart handed
# "Axis"/"Values"/"Legend" instead of "Category"/"Y"/"Series" draws an empty
# canvas while still passing schema validation. To spare callers from memorizing
# each visual's exact role names, map the common/intuitive aliases to the
# canonical PBIR role per visual-type family. Inner keys are lowercased aliases;
# values are the canonical role names Power BI actually renders.
_CARTESIAN_ROLES = {  # bar / column / line / area: category axis, legend, value
    "axis": "Category", "category": "Category", "x": "Category",
    "legend": "Series", "series": "Series", "group": "Series",
    "values": "Y", "value": "Y", "y": "Y",
    "y2": "Y2", "secondaryvalues": "Y2",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
    "smallmultiples": "SmallMultiples", "small_multiples": "SmallMultiples",
}
_CATEGORICAL_ROLES = {  # pie / donut: the legend is the Category role, values is Y
    "axis": "Category", "category": "Category", "legend": "Category", "group": "Category",
    "values": "Y", "value": "Y", "y": "Y",
    "details": "Details", "detail": "Details",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_CARD_ROLES = {
    "values": "Values", "value": "Values", "y": "Values",
    "field": "Values", "fields": "Values", "data": "Values",
}
_CARD_V2_ROLES = {
    "values": "Data", "value": "Data", "y": "Data",
    "field": "Data", "fields": "Data", "data": "Data",
    "referenceLabels": "ReferenceLabels", "referencelabels": "ReferenceLabels",
    "reference_labels": "ReferenceLabels", "referencelabel": "ReferenceLabels",
    "additionalMeasure": "AdditionalMeasure", "additionalmeasure": "AdditionalMeasure",
    "additional_measure": "AdditionalMeasure", "additional": "AdditionalMeasure",
}
_COMBO_ROLES = {
    "category": "Category", "x": "Category", "axis": "Category",
    "columnY": "ColumnY", "columny": "ColumnY", "column_y": "ColumnY", "bar": "ColumnY", "column": "ColumnY",
    "lineY": "LineY", "liney": "LineY", "line_y": "LineY", "line": "LineY",
    "series": "Series", "legend": "Series",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_WATERFALL_ROLES = {
    "category": "Category", "x": "Category", "axis": "Category",
    "y": "Y", "values": "Y", "value": "Y",
    "breakdown": "Breakdown", "break": "Breakdown",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_SCATTER_ROLES = {
    "x": "X",
    "y": "Y",
    "size": "Size",
    "details": "Details", "detail": "Details",
    "legend": "Legend", "series": "Legend",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_FUNNEL_ROLES = {
    "category": "Category", "x": "Category", "axis": "Category",
    "y": "Y", "values": "Y", "value": "Y",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_TREEMAP_ROLES = {
    "category": "Category", "details": "Category", "groups": "Category",
    "values": "Values", "value": "Values", "y": "Values",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_KPI_ROLES = {
    "indicator": "Indicator", "value": "Indicator", "values": "Indicator", "y": "Indicator",
    "trendLine": "TrendLine", "trendline": "TrendLine", "trend": "TrendLine", "trend_line": "TrendLine",
    "goal": "Goal", "target": "Goal",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_GAUGE_ROLES = {
    "y": "Y", "value": "Y", "values": "Y", "indicator": "Y",
    "minValue": "MinValue", "minvalue": "MinValue", "min": "MinValue", "min_value": "MinValue",
    "maxValue": "MaxValue", "maxvalue": "MaxValue", "max": "MaxValue", "max_value": "MaxValue",
    "targetValue": "TargetValue", "targetvalue": "TargetValue", "target": "TargetValue", "target_value": "TargetValue",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_MAP_ROLES = {
    "location": "Location", "category": "Location",
    "size": "Size", "value": "Size", "values": "Size",
    "tooltips": "Tooltips", "tooltip": "Tooltips",
}
_NO_DATA_ROLES: dict[str, str] = {}
_ACTION_BUTTON_ROLES: dict[str, str] = {}
_PAGE_NAV_ROLES: dict[str, str] = {}
_ADV_SLICER_ROLES = {
    "field": "Values", "fields": "Values", "values": "Values", "value": "Values",
}
_TABLE_ROLES = {
    "values": "Values", "value": "Values", "y": "Values",
    "columns": "Values", "column": "Values", "fields": "Values", "field": "Values",
}
_MATRIX_ROLES = {
    "rows": "Rows", "row": "Rows", "axis": "Rows", "category": "Rows",
    "columns": "Columns", "column": "Columns", "legend": "Columns", "series": "Columns",
    "values": "Values", "value": "Values", "y": "Values",
}
_SLICER_ROLES = {
    "values": "Values", "value": "Values", "field": "Values", "fields": "Values",
    "column": "Values", "axis": "Values", "category": "Values",
}
VISUAL_ROLE_ALIASES: dict[str, dict[str, str]] = {
    "card": _CARD_ROLES,
    "multiRowCard": _CARD_ROLES,
    "cardVisual": _CARD_V2_ROLES,
    "clusteredBarChart": _CARTESIAN_ROLES,
    "barChart": _CARTESIAN_ROLES,
    "stackedBarChart": _CARTESIAN_ROLES,
    "hundredPercentStackedBarChart": _CARTESIAN_ROLES,
    "clusteredColumnChart": _CARTESIAN_ROLES,
    "columnChart": _CARTESIAN_ROLES,
    "stackedColumnChart": _CARTESIAN_ROLES,
    "hundredPercentStackedColumnChart": _CARTESIAN_ROLES,
    "lineChart": _CARTESIAN_ROLES,
    "areaChart": _CARTESIAN_ROLES,
    "stackedAreaChart": _CARTESIAN_ROLES,
    "pieChart": _CATEGORICAL_ROLES,
    "donutChart": _CATEGORICAL_ROLES,
    "tableEx": _TABLE_ROLES,
    "pivotTable": _MATRIX_ROLES,
    "slicer": _SLICER_ROLES,
    "lineClusteredColumnComboChart": _COMBO_ROLES,
    "lineStackedColumnComboChart": _COMBO_ROLES,
    "actionButton": _ACTION_BUTTON_ROLES,
    "pageNavigator": _PAGE_NAV_ROLES,
    "advancedSlicerVisual": _ADV_SLICER_ROLES,
    "waterfallChart": _WATERFALL_ROLES,
    "scatterChart": _SCATTER_ROLES,
    "bubbleChart": _SCATTER_ROLES,
    "funnelChart": _FUNNEL_ROLES,
    "treemap": _TREEMAP_ROLES,
    "kpi": _KPI_ROLES,
    "gauge": _GAUGE_ROLES,
    "filledMap": _MAP_ROLES,
    "azureMap": _MAP_ROLES,
    "image": _NO_DATA_ROLES,
    "shape": _NO_DATA_ROLES,
    "textbox": _NO_DATA_ROLES,
}
# Visual-type lookups are case-insensitive.
_VISUAL_ROLE_ALIASES_LC = {key.lower(): value for key, value in VISUAL_ROLE_ALIASES.items()}


def normalize_query_roles(
    visual_type: str | None,
    query_roles: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Map caller-supplied role names to the canonical PBIR roles for a visual type.

    Returns ``(normalized_roles, remaps)`` where ``remaps`` lists ``{"from", "to"}``
    entries for every role that was renamed, so the caller can be told what changed.
    Roles already canonical, or not recognized as an alias, pass through unchanged so
    raw/advanced role names keep working. When two source roles collapse to the same
    canonical role their projections are concatenated.
    """
    aliases = _VISUAL_ROLE_ALIASES_LC.get((visual_type or "").lower())
    if not aliases:
        return dict(query_roles), []
    canonical = set(aliases.values())
    normalized: dict[str, Any] = {}
    remaps: list[dict[str, str]] = []
    for role, projection in query_roles.items():
        target = role if role in canonical else aliases.get(role.lower(), role)
        if target != role:
            remaps.append({"from": role, "to": target})
        if target in normalized:
            existing = normalized[target]
            existing = existing if isinstance(existing, list) else [existing]
            addition = projection if isinstance(projection, list) else [projection]
            normalized[target] = existing + addition
        else:
            normalized[target] = projection
    return normalized, remaps


def unknown_query_roles(visual_type: str | None, query: Any) -> list[str]:
    """Return queryState role names a known visual type cannot render (else [])."""
    aliases = _VISUAL_ROLE_ALIASES_LC.get(str(visual_type or "").lower())
    if not aliases or not isinstance(query, dict):
        return []
    state = query.get("queryState")
    if not isinstance(state, dict):
        return []
    allowed = set(aliases.values())
    return [role for role in state if role not in allowed]


STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "dark": {
        "theme_name": "Dark",
        "primary": "#5B9BD5",
        "accent": "#ED7D31",
        "background": "#1F1F2E",
        "text": "#E8E6E3",
        "good": "#4DCFB3",
        "neutral": "#FFD166",
        "bad": "#FF6B6B",
        "page_background_transparency": 0,
    },
    "corporate_blue": {
        "theme_name": "Corporate-Blue",
        "primary": "#003087",
        "accent": "#009CDE",
        "background": "#F5F7FA",
        "text": "#1D2C4D",
        "good": "#00A651",
        "neutral": "#F5A623",
        "bad": "#C8001F",
        "page_background_transparency": 0,
    },
    "minimal_white": {
        "theme_name": "Minimal-White",
        "primary": "#2D7DD2",
        "accent": "#97CC04",
        "background": "#FFFFFF",
        "text": "#333333",
        "good": "#37B679",
        "neutral": "#F28E2B",
        "bad": "#E15759",
        "page_background_transparency": 100,
    },
}


class PBIRError(RuntimeError):
    pass


def json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PBIRError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json_dumps(data) + "\n", encoding="utf-8")
    tmp.replace(path)


def deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _blend(c1: str, c2: str, t: float) -> str:
    """Blend c1 toward c2; t=1.0 returns c1, t=0.0 returns c2."""
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return _rgb_to_hex(round(r1 * t + r2 * (1 - t)), round(g1 * t + g2 * (1 - t)), round(b1 * t + b2 * (1 - t)))


def _luminance(c: str) -> float:
    r, g, b = _hex_to_rgb(c)
    return 0.299 * r + 0.587 * g + 0.114 * b


def _is_dark(c: str) -> bool:
    return _luminance(c) < 140


def _hex_to_hsl(h: str) -> tuple[float, float, float]:
    r, g, b = [x / 255.0 for x in _hex_to_rgb(h)]
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        hue = (g - b) / d + (6 if g < b else 0)
    elif mx == g:
        hue = (b - r) / d + 2.0
    else:
        hue = (r - g) / d + 4.0
    return (hue / 6.0) * 360.0, s, l


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    h /= 360.0

    def _hue2rgb(p: float, q: float, t: float) -> float:
        if t < 0:
            t += 1.0
        if t > 1:
            t -= 1.0
        if t < 1 / 6:
            return p + (q - p) * 6.0 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2.0 / 3.0 - t) * 6.0
        return p

    if s == 0:
        r = g = b = l
    else:
        q = l * (1 + s) if l < 0.5 else l + s - l * s
        p = 2.0 * l - q
        r, g, b = _hue2rgb(p, q, h + 1 / 3), _hue2rgb(p, q, h), _hue2rgb(p, q, h - 1 / 3)
    return _rgb_to_hex(round(r * 255), round(g * 255), round(b * 255))


def _rotate_hue(c: str, degrees: float) -> str:
    h, s, l = _hex_to_hsl(c)
    return _hsl_to_hex((h + degrees) % 360.0, s, l)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _generate_data_colors(primary: str, accent: str) -> list[str]:
    h1, s1, l1 = _hex_to_hsl(primary)
    h2, s2, l2 = _hex_to_hsl(accent)
    return [
        primary,
        accent,
        _hsl_to_hex(h1, s1, _clamp(l1 + 0.18, 0.1, 0.9)),
        _hsl_to_hex(h2, s2, _clamp(l2 + 0.18, 0.1, 0.9)),
        _hsl_to_hex((h1 + 240) % 360, s1, l1),
        _hsl_to_hex((h1 + 120) % 360, s1, l1),
        _hsl_to_hex(h1, s1, _clamp(l1 - 0.18, 0.1, 0.9)),
        _hsl_to_hex(h2, s2, _clamp(l2 - 0.18, 0.1, 0.9)),
        _hsl_to_hex((h1 + 60) % 360, _clamp(s1 * 0.8, 0, 1), _clamp(l1 + 0.08, 0.1, 0.9)),
        _hsl_to_hex((h1 + 300) % 360, _clamp(s1 * 0.9, 0, 1), l1),
    ]


def _solid(c: str) -> dict[str, Any]:
    return {"solid": {"color": c}}


def _color_expr(color: str) -> dict[str, Any]:
    """PBIR visual.json color format: solid wrapper around a Literal expression."""
    return {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color}'"}}}}}


def _get_color_expr(val: Any) -> str | None:
    """Extract hex color string from a _color_expr value, or None if not that format."""
    try:
        raw = val["solid"]["color"]["expr"]["Literal"]["Value"]
        return raw.strip("'")
    except (KeyError, TypeError, AttributeError):
        return None


_VTYPE_SHORT: dict[str, str] = {
    "card": "card", "cardNew": "card", "cardVisual": "cardv2", "slicer": "slicer",
    "tableEx": "table", "pivotTable": "pivot",
    "lineChart": "line", "areaChart": "area",
    "clusteredBarChart": "bar", "barChart": "bar", "stackedBarChart": "bar",
    "clusteredColumnChart": "col", "columnChart": "col", "stackedColumnChart": "col",
    "lineClusteredColumnComboChart": "combo", "lineStackedColumnComboChart": "combostack",
    "donutChart": "donut", "pieChart": "pie", "treemap": "tree",
    "filledMap": "map", "map": "map", "shapeMap": "map",
    "funnel": "funnel", "gauge": "gauge", "kpi": "kpi",
    "scatterChart": "scatter", "waterfallChart": "waterfall",
    "ribbonChart": "ribbon", "multiRowCard": "mcard",
    "textbox": "text", "image": "img", "shape": "shape",
    "pageNavigator": "pagenav", "actionButton": "actionbtn", "advancedSlicerVisual": "advslicr",
    "bubbleChart": "bubble", "funnelChart": "funnel", "azureMap": "azuremap",
}


def _make_table_abbrevs(table_names: list[str]) -> dict[str, str]:
    used: set[str] = set()
    result: dict[str, str] = {}
    for name in sorted(table_names, key=lambda n: n.lower()):
        words = name.replace("/", " ").replace("_", " ").replace("-", " ").split()
        abbr = "".join(w[0].upper() for w in words if w)
        if abbr in used:
            abbr = "".join(w[:2].capitalize() for w in words if w)
        if abbr in used:
            abbr = name.replace(" ", "")[:8]
        if abbr in used:
            abbr = abbr + str(sum(1 for k in used if k.startswith(abbr)))
        used.add(abbr)
        result[name] = abbr
    return result


RECOLOR_TARGETS = frozenset({
    "title", "title_background", "background",
    "data_primary", "axis_labels", "axis_titles",
    "card_value", "card_label", "labels",
    "slicer_items",
    "slicer_header_bg", "slicer_header_text",
    "slicer_date_bg", "slicer_date_text",
    "slicer_slider", "slicer_outline",
})


def _objects_item_props(data: dict, container: str, group: str) -> dict:
    """Return visual.{container}.{group}[0].properties, creating nodes as needed."""
    node = data.setdefault("visual", {}).setdefault(container, {})
    arr = node.setdefault(group, [])
    if not arr:
        arr.append({})
    return arr[0].setdefault("properties", {})


def _apply_color_target(data: dict, target: str, color: str) -> bool:
    """Set one color target in visual.json data in-place. Returns True if the file changed."""
    c = _color_expr(color)
    if target == "title":
        props = _objects_item_props(data, "visualContainerObjects", "title")
        if _get_color_expr(props.get("fontColor")) == color:
            return False
        props["fontColor"] = c
        return True
    if target == "title_background":
        props = _objects_item_props(data, "visualContainerObjects", "title")
        if _get_color_expr(props.get("background")) == color:
            return False
        props["background"] = c
        return True
    if target == "background":
        # PBI uses visualContainerObjects.background.color (no show property needed).
        props = _objects_item_props(data, "visualContainerObjects", "background")
        if _get_color_expr(props.get("color")) == color:
            return False
        props["color"] = c
        return True
    if target == "data_primary":
        # PBI Desktop uses "fill" (not "defaultColor") for the actual color override.
        # "defaultColor": {"solid": {}} signals "using custom fill" to PBI.
        props = _objects_item_props(data, "objects", "dataPoint")
        if _get_color_expr(props.get("fill")) == color:
            return False
        props["fill"] = c
        props["defaultColor"] = {"solid": {}}
        return True
    if target == "axis_labels":
        changed = False
        for axis in ("categoryAxis", "valueAxis"):
            props = _objects_item_props(data, "objects", axis)
            if _get_color_expr(props.get("labelColor")) != color:
                props["labelColor"] = c
                changed = True
        return changed
    if target == "axis_titles":
        changed = False
        for axis in ("categoryAxis", "valueAxis"):
            props = _objects_item_props(data, "objects", axis)
            if _get_color_expr(props.get("titleColor")) != color:
                props["titleColor"] = c
                changed = True
        return changed
    if target in ("card_value", "labels"):
        # card_value = big number on a card visual; labels = data labels on charts.
        # Both map to objects.labels.color in PBIR.
        props = _objects_item_props(data, "objects", "labels")
        if _get_color_expr(props.get("color")) == color:
            return False
        props["color"] = c
        return True
    if target == "card_label":
        props = _objects_item_props(data, "objects", "categoryLabels")
        if _get_color_expr(props.get("color")) == color:
            return False
        props["color"] = c
        return True
    if target == "slicer_items":
        props = _objects_item_props(data, "objects", "items")
        if _get_color_expr(props.get("fontColor")) == color:
            return False
        props["fontColor"] = c
        return True
    is_slicer = data.get("visual", {}).get("visualType") == "slicer"
    if target == "slicer_header_bg":
        if not is_slicer:
            return False
        props = _objects_item_props(data, "objects", "header")
        if _get_color_expr(props.get("background")) == color:
            return False
        props["background"] = c
        return True
    if target == "slicer_header_text":
        if not is_slicer:
            return False
        props = _objects_item_props(data, "objects", "header")
        if _get_color_expr(props.get("fontColor")) == color:
            return False
        props["fontColor"] = c
        return True
    if target == "slicer_date_bg":
        if not is_slicer:
            return False
        props = _objects_item_props(data, "objects", "date")
        if _get_color_expr(props.get("background")) == color:
            return False
        props["background"] = c
        return True
    if target == "slicer_date_text":
        if not is_slicer:
            return False
        props = _objects_item_props(data, "objects", "date")
        if _get_color_expr(props.get("fontColor")) == color:
            return False
        props["fontColor"] = c
        return True
    if target == "slicer_slider":
        if not is_slicer:
            return False
        props = _objects_item_props(data, "objects", "slider")
        if _get_color_expr(props.get("color")) == color:
            return False
        props["color"] = c
        return True
    if target == "slicer_outline":
        if not is_slicer:
            return False
        props = _objects_item_props(data, "objects", "general")
        if _get_color_expr(props.get("outlineColor")) == color:
            return False
        props["outlineColor"] = c
        return True
    return False


def build_pbi_theme(
    name: str,
    primary: str,
    accent: str,
    background: str,
    text: str,
    good: str,
    neutral_color: str,
    bad: str,
    page_bg_transparency: int,
) -> dict[str, Any]:
    dark = _is_dark(background)
    white, black = "#FFFFFF", "#000000"

    if dark:
        fg_secondary = _blend(text, background, 0.60)
        fg_tertiary = _blend(text, background, 0.30)
        bg_light = _blend(white, background, 0.12)
        bg_neutral = _blend(white, background, 0.25)
        bg_outspace = _blend(black, background, 0.85)
    else:
        fg_secondary = _blend(text, white, 0.55)
        fg_tertiary = _blend(text, white, 0.28)
        bg_light = _blend(background, white, 0.85) if background.upper() != white else "#F3F2F1"
        bg_neutral = _blend(background, white, 0.65) if background.upper() != white else "#C8C6C4"
        bg_outspace = bg_light

    page_vs: dict[str, Any] = {
        "outspace": [{"color": _solid(bg_outspace)}],
        "background": [{
            "show": page_bg_transparency < 100,
            "transparency": page_bg_transparency,
            "color": _solid(background),
        }],
    }

    global_vs: dict[str, Any] = {}
    if dark:
        global_vs = {
            "background": [{"show": False}],
            "title": [{"fontColor": _solid(text)}],
            "categoryAxis": [{
                "labelColor": _solid(fg_secondary),
                "gridlineColor": _solid(bg_light),
                "titleColor": _solid(fg_secondary),
            }],
            "valueAxis": [{
                "labelColor": _solid(fg_secondary),
                "gridlineColor": _solid(bg_light),
                "titleColor": _solid(fg_secondary),
            }],
            "legend": [{"labelColor": _solid(fg_secondary)}],
            "labels": [{"color": _solid(text)}],
        }

    visual_styles: dict[str, Any] = {"page": {"*": page_vs}}
    if global_vs:
        visual_styles["*"] = {"*": global_vs}

    return {
        "name": name,
        "dataColors": _generate_data_colors(primary, accent),
        "foreground": text,
        "foregroundNeutralSecondary": fg_secondary,
        "foregroundNeutralTertiary": fg_tertiary,
        "background": background,
        "backgroundLight": bg_light,
        "backgroundNeutral": bg_neutral,
        "tableAccent": primary,
        "good": good,
        "neutral": neutral_color,
        "bad": bad,
        "visualStyles": visual_styles,
    }


def _to_pbi_literal(value: Any) -> str:
    """Return a raw PBI literal string (no wrapping dict)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, float):
        return f"{value}D"
    if isinstance(value, int):
        return f"{value}L"
    return str(value)


def pbir_literal(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, str):
        literal = "'" + value.replace("'", "''") + "'"
    elif isinstance(value, int):
        literal = f"{value}L"
    else:
        literal = str(value)
    return {"expr": {"Literal": {"Value": literal}}}


def pbir_literal_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    literal = (((value.get("expr") or {}).get("Literal") or {}).get("Value"))
    if not isinstance(literal, str):
        return None
    if literal == "true":
        return True
    if literal == "false":
        return False
    if literal.endswith("L") and literal[:-1].lstrip("-").isdigit():
        return int(literal[:-1])
    if len(literal) >= 2 and literal[0] == "'" and literal[-1] == "'":
        return literal[1:-1].replace("''", "'")
    return literal


def object_name(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def normalize_object_name(name: str, label: str = "name") -> str:
    if not name or not NAME_RE.match(name):
        raise PBIRError(
            f"Invalid {label} '{name}'. PBIR names must contain only letters, digits, underscores, or hyphens."
        )
    if len(name) > 50:
        raise PBIRError(f"Invalid {label} '{name}'. PBIR object names must be 50 characters or fewer.")
    return name


def unexpected_keys(data: dict[str, Any], allowed: set[str]) -> list[str]:
    return sorted(str(key) for key in data.keys() if key not in allowed)


def scalar_position(position: dict[str, Any] | None, next_z: int = 0) -> dict[str, float | int]:
    pos = dict(position or {})
    result: dict[str, float | int] = {
        "x": float(pos.get("x", 40)),
        "y": float(pos.get("y", 40)),
        "width": float(pos.get("width", 360)),
        "height": float(pos.get("height", 220)),
    }
    if "z" in pos:
        result["z"] = int(pos["z"])
    else:
        result["z"] = int(next_z)
    if "tabOrder" in pos:
        result["tabOrder"] = int(pos["tabOrder"])
    if "angle" in pos:
        result["angle"] = float(pos["angle"])
    return result


_DEFAULT_Z: dict[str, int] = {
    "slicer": 500, "advancedSlicerVisual": 500,
    "card": 1000, "cardNew": 1000, "cardVisual": 1000, "kpiVisual": 1000, "kpi": 1000, "multiRowCard": 1000,
    "actionButton": 3000, "pageNavigator": 3000,
}
_DEFAULT_Z_FALLBACK = 2000


def schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def text_result(data: Any) -> dict[str, Any]:
    if isinstance(data, str):
        text = data
    else:
        text = json_dumps(data)
    return {"content": [{"type": "text", "text": text}]}


def error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def validation_summary(validation: dict[str, Any], include_messages: bool = False) -> dict[str, Any]:
    errors = list(validation.get("errors") or [])
    warnings = list(validation.get("warnings") or [])
    result: dict[str, Any] = {
        "ok": bool(validation.get("ok")),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "page_count": validation.get("page_count", 0),
        "visual_count": validation.get("visual_count", 0),
    }
    if include_messages:
        result["errors"] = errors
        result["warnings"] = warnings
    elif errors:
        result["first_error"] = errors[0]
    return result


@dataclass
class ValidationResult:
    report_root: str
    ok: bool
    errors: list[str]
    warnings: list[str]
    page_count: int
    visual_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_root": self.report_root,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "page_count": self.page_count,
            "visual_count": self.visual_count,
        }


PLATFORM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"
PBIP_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json"
DEFINITION_PBIR_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json"
DEFINITION_PBISM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json"

DEFAULT_REPORT_SETTINGS: dict[str, Any] = {
    "allowChangeFilterTypes": True,
    "useStylableVisualContainerHeader": True,
    "queryLimitOption": "Auto",
    "useEnhancedTooltips": True,
    "exportDataMode": "AllowSummarized",
    "useDefaultAggregateDisplayName": True,
    "defaultDrillFilterOtherVisuals": True,
}


class PBIRReport:
    def __init__(self, project_path: str | Path) -> None:
        self.report_root = self.resolve_report_root(project_path)
        self.definition_dir = self.report_root / "definition"
        self.pages_dir = self.definition_dir / "pages"

    @classmethod
    def scaffold_project(
        cls,
        folder: str | Path,
        name: str,
        initial_page_display_name: str = "Overview",
        initial_page_name: str | None = None,
        page_width: float = 1280,
        page_height: float = 720,
    ) -> "PBIRReport":
        """Create a new empty PBIP project: Report scaffold + SemanticModel stub."""
        root = Path(folder).expanduser().resolve()
        if not root.exists():
            raise PBIRError(f"Parent folder does not exist: {root}")

        report_folder = root / f"{name}.Report"
        semantic_folder = root / f"{name}.SemanticModel"
        pbip_file = root / f"{name}.pbip"

        for existing in (report_folder, semantic_folder):
            if existing.exists():
                raise PBIRError(f"Already exists: {existing}")

        # .pbip entry point
        write_json(pbip_file, {
            "$schema": PBIP_SCHEMA,
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{name}.Report"}}],
            "settings": {"enableAutoRecovery": True},
        })

        # Report scaffold
        write_json(report_folder / ".platform", {
            "$schema": PLATFORM_SCHEMA,
            "metadata": {"type": "Report", "displayName": name},
            "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
        })
        write_json(report_folder / "definition.pbir", {
            "$schema": DEFINITION_PBIR_SCHEMA,
            "version": "4.0",
            "datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}},
        })

        definition = report_folder / "definition"
        write_json(definition / "report.json", {
            "$schema": REPORT_SCHEMA,
            "themeCollection": {
                "baseTheme": {
                    "name": DEFAULT_THEME_NAME,
                    "reportVersionAtImport": dict(DEFAULT_THEME_VERSION),
                    "type": "SharedResources",
                }
            },
            "resourcePackages": [
                {
                    "name": "SharedResources",
                    "type": "SharedResources",
                    "items": [
                        {
                            "name": DEFAULT_THEME_NAME,
                            "path": f"BaseThemes/{DEFAULT_THEME_NAME}.json",
                            "type": "BaseTheme",
                        }
                    ],
                }
            ],
            "settings": DEFAULT_REPORT_SETTINGS,
        })
        write_json(definition / "version.json", {"$schema": VERSION_SCHEMA, "version": "2.0.0"})

        page_name = normalize_object_name(initial_page_name or "Overview", "page name")
        pages = definition / "pages"
        write_json(pages / "pages.json", {
            "$schema": PAGES_METADATA_SCHEMA,
            "pageOrder": [page_name],
            "activePageName": page_name,
        })
        page_dir = pages / page_name
        write_json(page_dir / "page.json", {
            "$schema": PAGE_SCHEMA,
            "name": page_name,
            "displayName": initial_page_display_name,
            "displayOption": "FitToPage",
            "width": float(page_width),
            "height": float(page_height),
        })
        (page_dir / "visuals").mkdir(parents=True, exist_ok=True)

        # SemanticModel stub (content populated later by powerbi-modeling-mcp)
        write_json(semantic_folder / ".platform", {
            "$schema": PLATFORM_SCHEMA,
            "metadata": {"type": "SemanticModel", "displayName": name},
            "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
        })
        write_json(semantic_folder / "definition.pbism", {
            "$schema": DEFINITION_PBISM_SCHEMA,
            "version": "4.0",
            "settings": {"qnaEnabled": False},
        })

        return cls(report_folder)

    @staticmethod
    def resolve_report_root(project_path: str | Path) -> Path:
        raw = Path(project_path).expanduser()
        path = raw.resolve(strict=False)

        if path.suffix.lower() == ".pbip":
            if not path.exists():
                raise PBIRError(f"PBIP file does not exist: {path}")
            try:
                pbip_data = read_json(path)
                for artifact in pbip_data.get("artifacts", []):
                    report_path = (artifact.get("report") or {}).get("path")
                    if report_path:
                        candidate = (path.parent / report_path).resolve()
                        if candidate.is_dir() and (candidate / "definition.pbir").exists():
                            return candidate
            except Exception:
                pass
            return PBIRReport._single_report_folder(path.parent)

        if path.suffix.lower() == ".pbir":
            root = path.parent
            if (root / "definition").is_dir():
                return root
            raise PBIRError(f"PBIR file is not next to a definition folder: {path}")

        if path.is_dir():
            if path.name.lower() == "definition" and (path.parent / "definition.pbir").exists():
                return path.parent
            if (path / "definition.pbir").exists() and (path / "definition").is_dir():
                return path
            if path.suffix.lower() == ".report" and (path / "definition.pbir").exists():
                return path
            reports = [
                child
                for child in path.iterdir()
                if child.is_dir() and child.suffix.lower() == ".report" and (child / "definition.pbir").exists()
            ]
            if len(reports) == 1:
                return reports[0]
            if len(reports) > 1:
                names = ", ".join(str(p) for p in reports)
                raise PBIRError(f"Multiple .Report folders found. Pass one explicitly: {names}")

        for parent in [path, *path.parents]:
            if parent.is_dir() and (parent / "definition.pbir").exists() and (parent / "definition").is_dir():
                return parent

        raise PBIRError(
            "Could not resolve a PBIR report folder. Pass a .pbip file, a .pbir file, a .Report folder, or a definition folder."
        )

    @staticmethod
    def _single_report_folder(root: Path) -> Path:
        reports = [
            child
            for child in root.iterdir()
            if child.is_dir() and child.suffix.lower() == ".report" and (child / "definition.pbir").exists()
        ]
        if not reports:
            raise PBIRError(f"No .Report folder with definition.pbir found next to {root}")
        if len(reports) > 1:
            raise PBIRError("PBIP folder has multiple .Report folders; pass the intended .Report folder directly.")
        return reports[0]

    def rel(self, *parts: str) -> Path:
        return self.definition_dir.joinpath(*parts)

    def read_def_json(self, *parts: str) -> Any:
        return read_json(self.rel(*parts))

    def write_def_json(self, data: Any, *parts: str) -> None:
        write_json(self.rel(*parts), data)

    def report_json_path(self) -> Path:
        return self.rel("report.json")

    def pages_json_path(self) -> Path:
        return self.pages_dir / "pages.json"

    def load_pages_metadata(self) -> dict[str, Any]:
        path = self.pages_json_path()
        if path.exists():
            data = read_json(path)
            if not isinstance(data, dict):
                raise PBIRError(f"pages.json must contain a JSON object: {path}")
            data.setdefault("$schema", PAGES_METADATA_SCHEMA)
            return data
        return {"$schema": PAGES_METADATA_SCHEMA, "pageOrder": []}

    def save_pages_metadata(self, data: dict[str, Any]) -> None:
        data["$schema"] = PAGES_METADATA_SCHEMA
        write_json(self.pages_json_path(), data)

    def page_dirs(self) -> list[Path]:
        if not self.pages_dir.exists():
            return []
        return sorted(
            [child for child in self.pages_dir.iterdir() if child.is_dir()],
            key=lambda p: p.name.lower(),
        )

    def visual_dirs(self, page_dir: Path) -> list[Path]:
        visuals_dir = page_dir / "visuals"
        if not visuals_dir.exists():
            return []
        return sorted(
            [child for child in visuals_dir.iterdir() if child.is_dir()],
            key=lambda p: p.name.lower(),
        )

    def page_summary(self, page_dir: Path) -> dict[str, Any]:
        page = read_json(page_dir / "page.json")
        return {
            "name": page.get("name", page_dir.name),
            "displayName": page.get("displayName"),
            "displayOption": page.get("displayOption"),
            "width": page.get("width"),
            "height": page.get("height"),
            "visibility": page.get("visibility", "AlwaysVisible"),
            "visual_count": len(self.visual_dirs(page_dir)),
        }

    def find_page_dir(self, page: str) -> Path:
        if not page:
            raise PBIRError("Page name is required.")
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for page_dir in self.page_dirs():
            path = page_dir / "page.json"
            if not path.exists():
                continue
            data = read_json(path)
            candidates.append((page_dir, data))
            if page_dir.name == page or data.get("name") == page or data.get("displayName") == page:
                return page_dir
        all_names = [s for p, data in candidates for s in (p.name, data.get("name", ""), data.get("displayName", "")) if s]
        close = difflib.get_close_matches(page, all_names, n=3, cutoff=0.6)
        if close:
            hint = f" Did you mean: {', '.join(close)}?"
        else:
            known = ", ".join(data.get("displayName") or data.get("name") or p.name for p, data in candidates)
            hint = f" Known pages: {known}"
        raise PBIRError(f"Page not found: '{page}'.{hint}")

    def find_visual_dir(self, page_dir: Path, visual: str) -> Path:
        if not visual:
            raise PBIRError("Visual name is required.")
        candidates: list[str] = []
        for visual_dir in self.visual_dirs(page_dir):
            path = visual_dir / "visual.json"
            if not path.exists():
                continue
            data = read_json(path)
            title = self.extract_title(data)
            candidates.extend([visual_dir.name, data.get("name", ""), title or ""])
            if visual_dir.name == visual or data.get("name") == visual or title == visual:
                return visual_dir
        named = sorted({item for item in candidates if item})
        close = difflib.get_close_matches(visual, named, n=3, cutoff=0.6)
        if close:
            hint = f" Did you mean: {', '.join(close)}?"
        else:
            hint = f" Known visuals: {', '.join(named)}"
        raise PBIRError(f"Visual not found: '{visual}'.{hint}")

    def list_pages(self, include_visuals: bool = False) -> dict[str, Any]:
        metadata = self.load_pages_metadata()
        pages = [self.page_summary(page_dir) for page_dir in self.page_dirs() if (page_dir / "page.json").exists()]
        order = metadata.get("pageOrder") or []
        if order:
            order_map = {name: idx for idx, name in enumerate(order)}
            pages.sort(key=lambda page: (order_map.get(page["name"], 10_000), page.get("displayName") or ""))
        active = metadata.get("activePageName")
        landing = metadata.get("landingPageName")
        for page in pages:
            page["is_active"] = page["name"] == active
            page["is_landing"] = page["name"] == landing
            if include_visuals:
                page_dir = self.find_page_dir(page["name"])
                page["visuals"] = self.list_visuals(page["name"])["visuals"]
        return {
            "report_root": str(self.report_root),
            "activePageName": active,
            "landingPageName": landing,
            "pages": pages,
        }

    def list_visuals(
        self,
        page: str | None = None,
        _include_paths: bool = False,
        include_bindings: bool = False,
    ) -> dict[str, Any]:
        visuals: list[dict[str, Any]] = []
        page_dirs = [self.find_page_dir(page)] if page else self.page_dirs()
        for page_dir in page_dirs:
            page_data = read_json(page_dir / "page.json") if (page_dir / "page.json").exists() else {}
            for visual_dir in self.visual_dirs(page_dir):
                path = visual_dir / "visual.json"
                if not path.exists():
                    continue
                data = read_json(path)
                visual_config = data.get("visual") or {}
                entry: dict[str, Any] = {
                    "page": page_data.get("name", page_dir.name),
                    "pageDisplayName": page_data.get("displayName"),
                    "name": data.get("name", visual_dir.name),
                    "title": self.extract_title(data),
                    "visualType": visual_config.get("visualType"),
                    "isHidden": bool(data.get("isHidden", False)),
                    "position": data.get("position", {}),
                }
                if _include_paths:
                    entry["path"] = str(path)
                if include_bindings:
                    entry["bindings"] = self.extract_bindings(data)
                visuals.append(entry)
        return {"report_root": str(self.report_root), "visuals": visuals}

    def report_summary(
        self,
        include_visuals: bool = False,
        include_bindings: bool = False,
        include_raw: bool = False,
        include_paths: bool = False,
        include_layout: bool = False,
        max_visuals_per_page: int = 50,
        validate: bool = True,
        include_validation_messages: bool = False,
    ) -> dict[str, Any]:
        pages_result = self.list_pages(include_visuals=False)
        pages = pages_result["pages"]
        summary_pages = []
        visual_map: dict[str, list[dict[str, Any]]] = {}
        include_visual_details = include_visuals or include_bindings or include_raw
        if include_visual_details:
            all_visuals = self.list_visuals(_include_paths=True)["visuals"]
            for visual in all_visuals:
                compact = self.compact_visual(visual, include_bindings, include_raw, include_paths)
                visual_map.setdefault(str(visual.get("page")), []).append(compact)

        for page in pages:
            page_entry: dict[str, Any] = {
                "name": page.get("name"),
                "displayName": page.get("displayName"),
                "displayOption": page.get("displayOption"),
                "width": page.get("width"),
                "height": page.get("height"),
                "visual_count": page.get("visual_count", 0),
                "is_active": bool(page.get("is_active", False)),
                "is_landing": bool(page.get("is_landing", False)),
            }
            if include_visual_details:
                page_visuals = visual_map.get(str(page.get("name")), [])
                limit = max(0, int(max_visuals_per_page))
                page_entry["visuals"] = page_visuals[:limit]
                if len(page_visuals) > limit:
                    page_entry["visuals_truncated"] = len(page_visuals) - limit
            if include_layout:
                try:
                    page_dir = self.find_page_dir(str(page.get("name")))
                    page_entry["layout"] = self.page_layout_analysis(page_dir)
                except PBIRError as exc:
                    page_entry["layout"] = {"error": str(exc)}
            summary_pages.append(page_entry)

        theme_summary: dict[str, Any] = {}
        try:
            themes = self.manage_theme("list")
            collection = themes.get("themeCollection") or {}
            for key in ("baseTheme", "customTheme"):
                item = collection.get(key)
                if isinstance(item, dict):
                    theme_summary[key] = {"name": item.get("name"), "type": item.get("type")}
            theme_summary["registered_resource_count"] = len(themes.get("registeredResources") or [])
        except PBIRError as exc:
            theme_summary["error"] = str(exc)

        result: dict[str, Any] = {
            "report_root": str(self.report_root),
            "activePageName": pages_result.get("activePageName"),
            "landingPageName": pages_result.get("landingPageName"),
            "page_count": len(summary_pages),
            "visual_count": sum(int(page.get("visual_count") or 0) for page in pages),
            "pages": summary_pages,
            "theme": theme_summary,
        }
        if validate:
            result["validation"] = validation_summary(self.validate().as_dict(), include_validation_messages)
        return result

    def compact_visual(
        self,
        visual: dict[str, Any],
        include_bindings: bool = False,
        include_raw: bool = False,
        include_paths: bool = False,
    ) -> dict[str, Any]:
        compact: dict[str, Any] = {
            "name": visual.get("name"),
            "title": visual.get("title"),
            "visualType": visual.get("visualType"),
            "isHidden": bool(visual.get("isHidden", False)),
            "position": visual.get("position", {}),
        }
        if include_paths:
            compact["path"] = visual.get("path")
        if include_bindings or include_raw:
            path = visual.get("path")
            if path:
                visual_json = read_json(Path(path))
                if include_bindings:
                    compact["bindings"] = self.extract_bindings(visual_json)
                if include_raw:
                    compact["raw"] = visual_json
        return compact

    @staticmethod
    def extract_bindings(visual_json: dict[str, Any]) -> list[dict[str, Any]]:
        visual = visual_json.get("visual") or {}
        query = visual.get("query") or {}
        query_state = query.get("queryState") or {}
        bindings: list[dict[str, Any]] = []
        if not isinstance(query_state, dict):
            return bindings
        for role, state in query_state.items():
            projections = (state or {}).get("projections") if isinstance(state, dict) else None
            if not isinstance(projections, list):
                continue
            for projection in projections:
                if not isinstance(projection, dict):
                    continue
                binding: dict[str, Any] = {"role": role}
                if projection.get("queryRef"):
                    binding["queryRef"] = projection.get("queryRef")
                field_sum = PBIRReport.field_summary(projection.get("field"))
                if field_sum:
                    binding.update(field_sum)
                bindings.append(binding)
        return bindings

    @staticmethod
    def field_summary(field: Any) -> dict[str, Any]:
        if not isinstance(field, dict):
            return {}
        if isinstance(field.get("Measure"), dict):
            measure = field["Measure"]
            return {
                "kind": "measure",
                "table": ((measure.get("Expression") or {}).get("SourceRef") or {}).get("Entity"),
                "name": measure.get("Property"),
            }
        if isinstance(field.get("Column"), dict):
            column = field["Column"]
            return {
                "kind": "column",
                "table": ((column.get("Expression") or {}).get("SourceRef") or {}).get("Entity"),
                "name": column.get("Property"),
            }
        if isinstance(field.get("Aggregation"), dict):
            aggregation = field["Aggregation"]
            result = PBIRReport.field_summary(aggregation.get("Expression"))
            if result:
                result["aggregateFunction"] = aggregation.get("Function")
                result["kind"] = f"aggregated_{result.get('kind', 'field')}"
            return result
        return {}

    @staticmethod
    def extract_title(visual_json: dict[str, Any]) -> str | None:
        visual = visual_json.get("visual") or {}
        containers = visual.get("visualContainerObjects") or {}
        title_defs = containers.get("title") or []
        if title_defs and isinstance(title_defs, list):
            props = title_defs[0].get("properties") or {}
            text = pbir_literal_value(props.get("text"))
            if isinstance(text, str):
                return text
        return None

    def get_visual_detail(self, page: str, visual: str) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        visual_dir = self.find_visual_dir(page_dir, visual)
        data = read_json(visual_dir / "visual.json")
        vc = data.get("visual") or {}
        objects = vc.get("objects") or {}
        vco = vc.get("visualContainerObjects") or {}

        def _color_from_props(props: dict, *keys: str) -> str | None:
            for k in keys:
                val = props.get(k)
                if isinstance(val, dict):
                    return (val.get("solid") or {}).get("color")
            return None

        def _first_props(obj_group: dict, key: str) -> dict:
            items = obj_group.get(key) or []
            return (items[0].get("properties") or {}) if items else {}

        formatting: dict[str, Any] = {}
        # title
        title_props = _first_props(vco, "title")
        if title_props:
            formatting["title_show"] = pbir_literal_value(title_props.get("show"))
            formatting["title_text"] = pbir_literal_value(title_props.get("text"))
            formatting["title_color"] = _color_from_props(title_props, "fontColor")
        # background
        bg_props = _first_props(vco, "background")
        if bg_props:
            formatting["background_show"] = pbir_literal_value(bg_props.get("show"))
            formatting["background_color"] = _color_from_props(bg_props, "fillColor", "color")
        # data point
        dp_props = _first_props(objects, "dataPoint")
        if dp_props:
            formatting["data_primary_color"] = _color_from_props(dp_props, "defaultColor")
        # axes
        ca_props = _first_props(objects, "categoryAxis")
        if ca_props:
            formatting["axis_label_color"] = _color_from_props(ca_props, "labelColor")
            formatting["axis_title_color"] = _color_from_props(ca_props, "titleColor")
        # card value
        cv_props = _first_props(objects, "calloutValue")
        if cv_props:
            formatting["card_value_color"] = _color_from_props(cv_props, "color")
        # slicer header
        hdr_props = _first_props(objects, "header")
        if hdr_props:
            formatting["slicer_header_show"] = pbir_literal_value(hdr_props.get("show"))

        # remove None values from formatting
        formatting = {k: v for k, v in formatting.items() if v is not None}

        return {
            "name": data.get("name"),
            "visualType": vc.get("visualType"),
            "title": self.extract_title(data),
            "isHidden": bool(data.get("isHidden", False)),
            "position": data.get("position", {}),
            "bindings": self.extract_bindings(data),
            "formatting": formatting,
            "applied_objects": sorted(objects.keys()),
            "applied_container_objects": sorted(vco.keys()),
        }

    @staticmethod
    def _tmdl_unquote(name: str) -> str:
        """Strip TMDL single-quote wrapping used for identifiers with spaces."""
        name = name.strip()
        if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
            return name[1:-1]
        return name

    @staticmethod
    def _parse_tmdl_table_file(content: str) -> dict[str, Any]:
        """Extract table name, columns, and measures from a TMDL table file."""
        result: dict[str, Any] = {"name": None, "columns": [], "measures": [], "source_hint": None}
        lines = content.splitlines()

        def indent_of(line: str) -> int:
            n = 0
            for ch in line:
                if ch in "\t ":
                    n += 1
                else:
                    break
            return n

        i = 0
        while i < len(lines):
            raw = lines[i]
            s = raw.strip()
            if not s:
                i += 1
                continue
            ind = indent_of(raw)
            if ind == 0 and s.startswith("table "):
                result["name"] = PBIRReport._tmdl_unquote(s[6:])
            elif ind > 0 and s.startswith("column "):
                col: dict[str, Any] = {"name": PBIRReport._tmdl_unquote(s[7:]), "dataType": None}
                result["columns"].append(col)
                j = i + 1
                while j < len(lines):
                    cr = lines[j]
                    cs = cr.strip()
                    if not cs:
                        j += 1
                        continue
                    if indent_of(cr) <= ind:
                        break
                    if cs.startswith("dataType: "):
                        col["dataType"] = cs[10:].strip()
                    j += 1
            elif ind > 0 and s.startswith("measure "):
                rest = s[8:].strip()
                if "=" in rest:
                    m_name, _, m_expr = rest.partition("=")
                    result["measures"].append({"name": PBIRReport._tmdl_unquote(m_name), "expression": m_expr.strip() or None})
                else:
                    result["measures"].append({"name": PBIRReport._tmdl_unquote(rest), "expression": None})
            i += 1

        # Extract source connection via regex (simpler than nesting through the M block)
        sql_m = re.search(r'Sql\.Database\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"', content)
        if sql_m:
            result["source_hint"] = {"type": "SqlServer", "server": sql_m.group(1), "database": sql_m.group(2)}
        elif "AzureSQL.Database" in content:
            result["source_hint"] = {"type": "AzureSQL"}
        elif "PostgreSQL.Database" in content:
            result["source_hint"] = {"type": "PostgreSQL"}
        elif "Oracle.Database" in content:
            result["source_hint"] = {"type": "Oracle"}
        return result

    def semantic_model_path(self) -> Path | None:
        """Resolve the SemanticModel folder referenced by definition.pbir."""
        pbir = self.report_root / "definition.pbir"
        if not pbir.exists():
            return None
        try:
            data = read_json(pbir)
        except PBIRError:
            return None
        ref = ((data.get("datasetReference") or {}).get("byPath") or {}).get("path")
        if not isinstance(ref, str):
            return None
        candidate = (self.report_root / ref).resolve()
        return candidate if candidate.is_dir() else None

    def _find_pbip_file(self) -> Path | None:
        """Locate the .pbip entry point that references THIS report.

        A project folder may hold several .pbip files (e.g. benchmark copies), so match
        on the one whose artifacts point at this report_root; fall back to a name match,
        then a lone .pbip."""
        parent = self.report_root.parent
        pbips = sorted(parent.glob("*.pbip"))
        for p in pbips:
            try:
                data = read_json(p)
            except Exception:
                continue
            for artifact in data.get("artifacts", []):
                ref = (artifact.get("report") or {}).get("path")
                if ref and (p.parent / ref).resolve() == self.report_root:
                    return p
        stem = self.report_root.stem
        for p in pbips:
            if p.stem == stem:
                return p
        return pbips[0] if len(pbips) == 1 else None

    def export_project(
        self,
        output_dir: str | Path | None = None,
        zip_path: str | Path | None = None,
        keep_folder: bool = True,
    ) -> dict[str, Any]:
        """Bundle ONLY this project — its .pbip entry point, .Report folder, and the
        referenced .SemanticModel folder — into a clean staging folder and a .zip.

        Scoped to the single resolved project, so unrelated siblings in a shared parent
        folder (other .pbip copies, a stray .pbix, exports, scratch scripts) are NOT
        swept in. This is a PBIP *source* bundle, not a self-contained .pbix: an
        import-mode model carries definitions, not cached data, so the recipient unzips
        the folder, opens the .pbip in Power BI Desktop, and Refreshes from the source.
        """
        base = self.report_root.stem
        parent = self.report_root.parent
        dest = Path(output_dir).expanduser().resolve() if output_dir else (parent / f"{base} Export")
        stage = dest / base
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True, exist_ok=True)

        included: list[str] = []
        missing: list[str] = []
        # Skip machine-local, non-source artifacts: the .pbi cache folder (VertiPaq
        # cache.abf can be 10s of MB), local settings, and Python junk. These are
        # regenerated by Power BI Desktop on open and must not bloat a source bundle.
        ignore = shutil.ignore_patterns(".pbi", "cache.abf", "localSettings.json", "__pycache__", "*.pyc")

        shutil.copytree(self.report_root, stage / self.report_root.name, ignore=ignore)
        included.append(self.report_root.name)

        sm_path = self.semantic_model_path()
        if sm_path and sm_path.is_dir():
            shutil.copytree(sm_path, stage / sm_path.name, ignore=ignore)
            included.append(sm_path.name)
        else:
            missing.append("SemanticModel (datasetReference.byPath did not resolve to a folder)")

        pbip_file = self._find_pbip_file()
        if pbip_file and pbip_file.exists():
            shutil.copy2(pbip_file, stage / pbip_file.name)
            included.append(pbip_file.name)
        else:
            missing.append(".pbip entry point")

        # Strip UTF-8 BOM from every JSON/tmdl file in the staging area.
        # Power BI Desktop rejects files with BOM; source files may have been written
        # by other tools (VS Code, older editors) that add it.
        UTF8_BOM = b"\xef\xbb\xbf"
        for f in stage.rglob("*"):
            if f.is_file() and f.suffix.lower() in {".json", ".tmdl"}:
                raw = f.read_bytes()
                if raw.startswith(UTF8_BOM):
                    f.write_bytes(raw[3:])

        if zip_path:
            zp = Path(zip_path).expanduser().resolve()
            archive_base = zp.with_suffix("") if zp.suffix.lower() == ".zip" else zp
        else:
            archive_base = dest / base
        archive_base.parent.mkdir(parents=True, exist_ok=True)
        # base_dir = stage.name puts a single "<base>/..." top-level folder in the zip,
        # so it unzips to one clean, openable project folder.
        archive = Path(
            shutil.make_archive(str(archive_base), "zip", root_dir=str(stage.parent), base_dir=stage.name)
        )

        result: dict[str, Any] = {
            "zip_path": str(archive),
            "size_kb": round(archive.stat().st_size / 1024, 1),
            "included": included,
            "is_self_contained_pbix": False,
            "message": (
                f"Bundled project '{base}' to {archive}. Unzip and open {base}.pbip in Power BI "
                "Desktop, then Refresh to load data (a PBIP carries the model definition, not cached "
                "data, so this is not a self-contained .pbix)."
            ),
        }
        if missing:
            result["warnings"] = [f"Omitted (not found): {item}" for item in missing]
        if keep_folder:
            result["export_folder"] = str(stage)
        else:
            shutil.rmtree(stage)
            try:
                if not any(dest.iterdir()):
                    dest.rmdir()
            except Exception:
                pass
        return result

    @staticmethod
    def extract_pbix(pbix_path: str | Path, output_dir: str | Path | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Extract a .pbix/.pbit package using zip semantics."""
        import zipfile
        src = Path(pbix_path).expanduser().resolve()
        if not src.exists():
            raise PBIRError(f"PBIX file does not exist: {src}")
        dest = Path(output_dir).expanduser().resolve() if output_dir else src.with_suffix("")
        if dest.exists():
            if not overwrite:
                raise PBIRError(f"Output folder already exists: {dest}")
            if dest.parent == dest:
                raise PBIRError(f"Refusing to overwrite filesystem root: {dest}")
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(src, "r") as zf:
                infos = zf.infolist()
                names = [info.filename for info in infos]
                for info in infos:
                    target = (dest / info.filename).resolve()
                    if target != dest and dest not in target.parents:
                        raise PBIRError(f"Unsafe archive member escapes output folder: {info.filename}")
                zf.extractall(dest)
        except zipfile.BadZipFile as exc:
            raise PBIRError(f"Not a valid zip-backed PBIX/PBIT package: {src}") from exc
        return {
            "pbix_path": str(src),
            "extract_dir": str(dest),
            "file_count": len(names),
            "files": names[:200],
            "files_truncated": max(0, len(names) - 200),
        }

    @staticmethod
    def package_pbix(source_dir: str | Path, output_path: str | Path, overwrite: bool = False) -> dict[str, Any]:
        """Create a .pbix/.pbit package by zipping the extracted package folder contents."""
        import zipfile
        src = Path(source_dir).expanduser().resolve()
        if not src.is_dir():
            raise PBIRError(f"Source folder does not exist: {src}")
        out = Path(output_path).expanduser().resolve()
        if out.exists() and not overwrite:
            raise PBIRError(f"Output file already exists: {out}")
        out.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file in sorted(src.rglob("*")):
                resolved = file.resolve()
                if resolved == out:
                    continue
                if file.is_file():
                    zf.write(file, file.relative_to(src).as_posix())
                    count += 1
        return {"source_dir": str(src), "pbix_path": str(out), "file_count": count, "size_kb": round(out.stat().st_size / 1024, 1)}

    def journal_dir(self) -> Path:
        return self.report_root.parent / f".{self.report_root.name}.pbir_mcp_journal"

    def create_undo_snapshot(self, label: str = "manual", include_semantic_model: bool = True) -> dict[str, Any]:
        import time
        snap_id = time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]
        root = self.journal_dir()
        snap = root / snap_id
        snap.mkdir(parents=True, exist_ok=False)
        ignore = shutil.ignore_patterns(".pbi", "cache.abf", "__pycache__", "*.pyc")
        shutil.copytree(self.report_root, snap / self.report_root.name, ignore=ignore)
        included = [self.report_root.name]
        sm_path = self.semantic_model_path()
        if include_semantic_model and sm_path and sm_path.exists():
            shutil.copytree(sm_path, snap / sm_path.name, ignore=ignore)
            included.append(sm_path.name)
        manifest = {
            "id": snap_id,
            "label": label,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "report_root": str(self.report_root),
            "semantic_model_path": str(sm_path) if sm_path else None,
            "included": included,
        }
        write_json(snap / "manifest.json", manifest)
        return manifest

    def list_undo_snapshots(self) -> list[dict[str, Any]]:
        root = self.journal_dir()
        if not root.exists():
            return []
        snapshots = []
        for folder in sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name):
            manifest_path = folder / "manifest.json"
            if manifest_path.exists():
                try:
                    snapshots.append(read_json(manifest_path))
                    continue
                except PBIRError:
                    pass
            snapshots.append({"id": folder.name, "path": str(folder)})
        return snapshots

    def undo_last_action(self, snapshot_id: str | None = None) -> dict[str, Any]:
        snapshots = self.list_undo_snapshots()
        if not snapshots:
            raise PBIRError("No undo snapshots are available for this report.")
        manifest = next((s for s in snapshots if s.get("id") == snapshot_id), None) if snapshot_id else snapshots[-1]
        if not manifest:
            raise PBIRError(f"Undo snapshot not found: {snapshot_id}")
        snap = self.journal_dir() / str(manifest["id"])
        report_copy = snap / self.report_root.name
        if not report_copy.exists():
            raise PBIRError(f"Undo snapshot is missing report copy: {report_copy}")
        backup_root = self.journal_dir() / ("restore_backup_" + uuid.uuid4().hex[:8])
        backup_root.mkdir(parents=True, exist_ok=False)
        restored = []
        try:
            if self.report_root.exists():
                shutil.move(str(self.report_root), str(backup_root / self.report_root.name))
            shutil.copytree(report_copy, self.report_root)
            restored.append(str(self.report_root))
            sm_manifest = manifest.get("semantic_model_path")
            if sm_manifest:
                sm_path = Path(sm_manifest)
                sm_copy = snap / sm_path.name
                if sm_copy.exists():
                    if sm_path.exists():
                        shutil.move(str(sm_path), str(backup_root / sm_path.name))
                    shutil.copytree(sm_copy, sm_path)
                    restored.append(str(sm_path))
        except Exception:
            if not self.report_root.exists() and (backup_root / self.report_root.name).exists():
                shutil.move(str(backup_root / self.report_root.name), str(self.report_root))
            raise
        shutil.rmtree(backup_root, ignore_errors=True)
        return {"restored_snapshot": manifest, "restored": restored}

    def audit_report(
        self,
        max_visuals_per_page: int = 20,
        include_unused_measures: bool = True,
        role_memberships: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Run report quality checks suited for automated editing workflows."""
        issues: list[dict[str, Any]] = []
        no_data_visuals = {"textbox", "image", "shape", "actionButton", "pageNavigator"}
        pages = self.list_pages(include_visuals=False)["pages"]
        all_visuals = self.list_visuals(include_bindings=True)["visuals"]
        for page in pages:
            if int(page.get("visual_count") or 0) > max_visuals_per_page:
                issues.append({
                    "severity": "warning",
                    "code": "too_many_visuals",
                    "page": page.get("name"),
                    "message": f"Page has {page.get('visual_count')} visuals; threshold is {max_visuals_per_page}.",
                })
            try:
                layout = self.page_layout_analysis(self.find_page_dir(str(page.get("name"))))
                for item in layout.get("out_of_bounds", []):
                    issues.append({"severity": "warning", "code": "layout_overflow", "page": page.get("name"), "visual": item})
                for item in layout.get("overlaps", []):
                    issues.append({"severity": "info", "code": "layout_overlap", "page": page.get("name"), **item})
            except PBIRError as exc:
                issues.append({"severity": "error", "code": "layout_audit_failed", "page": page.get("name"), "message": str(exc)})
        for visual in all_visuals:
            vt = str(visual.get("visualType") or "")
            if vt not in no_data_visuals and not visual.get("bindings"):
                issues.append({
                    "severity": "warning",
                    "code": "visual_without_bindings",
                    "page": visual.get("page"),
                    "visual": visual.get("name"),
                    "visualType": vt,
                })

        used_measures = {
            (str(b.get("table")), str(b.get("name")))
            for visual in all_visuals
            for b in (visual.get("bindings") or [])
            if "measure" in str(b.get("kind", ""))
        }
        try:
            sm = PBIRSemanticModel.from_report(self.report_root)
            if sm:
                roles = sm.list_roles()
                if role_memberships is not None:
                    for role in roles:
                        members = role_memberships.get(role["name"], [])
                        if not members:
                            issues.append({"severity": "warning", "code": "rls_role_without_users", "role": role["name"]})
                elif roles:
                    issues.append({
                        "severity": "info",
                        "code": "rls_membership_unverified",
                        "message": "Local TMDL does not store Power BI Service role membership; pass role_memberships or use service tools.",
                        "role_count": len(roles),
                    })
            if include_unused_measures:
                semantic = self.read_semantic_model()
                for table in semantic.get("tables", []):
                    tname = table.get("name")
                    for measure in table.get("measures", []):
                        key = (str(tname), str(measure.get("name")))
                        if key not in used_measures:
                            issues.append({"severity": "info", "code": "unused_measure", "table": key[0], "measure": key[1]})
        except PBIRError as exc:
            issues.append({"severity": "info", "code": "semantic_audit_skipped", "message": str(exc)})

        counts: dict[str, int] = {}
        for issue in issues:
            counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
        return {
            "report_root": str(self.report_root),
            "ok": counts.get("error", 0) == 0,
            "issue_count": len(issues),
            "counts": counts,
            "issues": issues,
        }

    def render_preview(self, output_path: str | Path | None = None) -> dict[str, Any]:
        """Render a lightweight HTML layout preview: page canvases, visual boxes, titles, and bindings."""
        import html
        out = Path(output_path).expanduser().resolve() if output_path else self.report_root.parent / f"{self.report_root.stem}_preview.html"
        pages = self.list_pages(include_visuals=False)["pages"]
        visuals = self.list_visuals(include_bindings=True)["visuals"]
        by_page: dict[str, list[dict[str, Any]]] = {}
        for visual in visuals:
            by_page.setdefault(str(visual.get("page")), []).append(visual)
        parts = [
            "<!doctype html><meta charset='utf-8'><title>PBIR Preview</title>",
            "<style>body{font-family:Segoe UI,Arial,sans-serif;background:#f3f4f6;color:#111827;margin:24px}"
            ".page{position:relative;background:white;border:1px solid #d1d5db;margin:18px 0 36px;box-shadow:0 1px 4px #0002}"
            ".visual{position:absolute;border:1px solid #2563eb;background:#dbeafe99;box-sizing:border-box;overflow:hidden;padding:6px;font-size:12px}"
            ".title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta{color:#374151;font-size:11px}</style>",
            f"<h1>{html.escape(self.report_root.stem)} layout preview</h1>",
        ]
        max_width = 1100.0
        for page in pages:
            pw = float(page.get("width") or 1280)
            ph = float(page.get("height") or 720)
            scale = min(1.0, max_width / pw) if pw else 1.0
            parts.append(f"<h2>{html.escape(str(page.get('displayName') or page.get('name')))}</h2>")
            parts.append(f"<div class='page' style='width:{pw*scale:.1f}px;height:{ph*scale:.1f}px'>")
            for v in by_page.get(str(page.get("name")), []):
                pos = v.get("position") or {}
                x, y = float(pos.get("x", 0)) * scale, float(pos.get("y", 0)) * scale
                w, h = float(pos.get("width", 0)) * scale, float(pos.get("height", 0)) * scale
                title = html.escape(str(v.get("title") or v.get("name") or ""))
                vt = html.escape(str(v.get("visualType") or ""))
                bindings = ", ".join(
                    html.escape(f"{b.get('role')}:{b.get('table')}.{b.get('name')}")
                    for b in (v.get("bindings") or [])[:3]
                )
                parts.append(f"<div class='visual' style='left:{x:.1f}px;top:{y:.1f}px;width:{w:.1f}px;height:{h:.1f}px'>"
                             f"<div class='title'>{title}</div><div class='meta'>{vt}</div><div class='meta'>{bindings}</div></div>")
            parts.append("</div>")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(parts), encoding="utf-8")
        return {"preview_path": str(out), "page_count": len(pages), "visual_count": len(visuals)}

    def migrate_layout_to(
        self,
        target_project_path: str | Path,
        overwrite_pages: bool = False,
        remap_fields: bool = True,
    ) -> dict[str, Any]:
        """Copy report pages/visuals into another PBIR report, optionally remapping fields by name."""
        target = PBIRReport(target_project_path)
        try:
            target_semantic = target.read_semantic_model() if remap_fields else {"tables": []}
        except PBIRError:
            target_semantic = {"tables": []}
        fields_by_name: dict[tuple[str, str], list[str]] = {}
        target_tables = {t.get("name") for t in target_semantic.get("tables", [])}
        for table in target_semantic.get("tables", []):
            tname = str(table.get("name"))
            for col in table.get("columns", []):
                fields_by_name.setdefault(("column", str(col.get("name")).lower()), []).append(tname)
            for measure in table.get("measures", []):
                fields_by_name.setdefault(("measure", str(measure.get("name")).lower()), []).append(tname)

        def remap_visual_json(data: Any, remaps: list[dict[str, str]]) -> Any:
            if isinstance(data, dict):
                for node_name, kind in (("Measure", "measure"), ("Column", "column")):
                    node = data.get(node_name)
                    if isinstance(node, dict):
                        prop = str(node.get("Property", ""))
                        sr = ((node.get("Expression") or {}).get("SourceRef") or {})
                        old_table = sr.get("Entity")
                        candidates = fields_by_name.get((kind, prop.lower()), [])
                        if old_table not in target_tables and len(candidates) == 1:
                            sr["Entity"] = candidates[0]
                            remaps.append({"kind": kind, "field": prop, "from_table": str(old_table), "to_table": candidates[0]})
                for value in data.values():
                    remap_visual_json(value, remaps)
            elif isinstance(data, list):
                for value in data:
                    remap_visual_json(value, remaps)
            return data

        copied, skipped, field_remaps = [], [], []
        source_metadata = self.load_pages_metadata()
        target_metadata = target.load_pages_metadata()
        order = list(target_metadata.get("pageOrder") or [])
        for source_page in self.page_dirs():
            if not (source_page / "page.json").exists():
                continue
            dest_page = target.pages_dir / source_page.name
            if dest_page.exists():
                if not overwrite_pages:
                    skipped.append(source_page.name)
                    continue
                shutil.rmtree(dest_page)
            shutil.copytree(source_page, dest_page)
            if remap_fields:
                for visual_json in dest_page.glob("visuals/*/visual.json"):
                    data = read_json(visual_json)
                    remaps: list[dict[str, str]] = []
                    remap_visual_json(data, remaps)
                    if remaps:
                        write_json(visual_json, data)
                        field_remaps.extend({"page": source_page.name, "visual": visual_json.parent.name, **r} for r in remaps)
            if source_page.name not in order:
                order.append(source_page.name)
            copied.append(source_page.name)
        target_metadata["pageOrder"] = order
        if not target_metadata.get("activePageName") and source_metadata.get("activePageName") in order:
            target_metadata["activePageName"] = source_metadata.get("activePageName")
        target.save_pages_metadata(target_metadata)
        return {
            "source_report_root": str(self.report_root),
            "target_report_root": str(target.report_root),
            "copied_pages": copied,
            "skipped_pages": skipped,
            "field_remaps": field_remaps,
            "validation": validation_summary(target.validate().as_dict(), include_messages=True),
        }

    def git_commit(self, message: str = "PBIR MCP snapshot") -> dict[str, Any]:
        import subprocess
        parent = self.report_root.parent
        probe = subprocess.run(["git", "-C", str(parent), "rev-parse", "--show-toplevel"], text=True, capture_output=True)
        if probe.returncode != 0:
            raise PBIRError(f"Not inside a git repository: {parent}")
        paths = [self.report_root.name]
        sm = self.semantic_model_path()
        if sm and sm.parent == parent:
            paths.append(sm.name)
        add = subprocess.run(["git", "-C", str(parent), "add", *paths], text=True, capture_output=True)
        if add.returncode != 0:
            raise PBIRError(add.stderr.strip() or add.stdout.strip())
        commit = subprocess.run(["git", "-C", str(parent), "commit", "-m", message], text=True, capture_output=True)
        if commit.returncode != 0:
            raise PBIRError(commit.stderr.strip() or commit.stdout.strip())
        rev = subprocess.run(["git", "-C", str(parent), "rev-parse", "--short", "HEAD"], text=True, capture_output=True)
        return {"commit": rev.stdout.strip(), "message": message, "paths": paths}

    def git_rollback(self, ref: str) -> dict[str, Any]:
        import subprocess
        parent = self.report_root.parent
        paths = [self.report_root.name]
        sm = self.semantic_model_path()
        if sm and sm.parent == parent:
            paths.append(sm.name)
        result = subprocess.run(["git", "-C", str(parent), "checkout", ref, "--", *paths], text=True, capture_output=True)
        if result.returncode != 0:
            raise PBIRError(result.stderr.strip() or result.stdout.strip())
        return {"rolled_back_to": ref, "paths": paths}

    def read_semantic_model(self) -> dict[str, Any]:
        """Parse the referenced SemanticModel and return tables, columns, and measures."""
        sm_path = self.semantic_model_path()
        if sm_path is None:
            raise PBIRError(
                f"Could not locate SemanticModel referenced in {self.report_root / 'definition.pbir'}. "
                "Ensure datasetReference.byPath.path points to a valid .SemanticModel folder."
            )
        tables_dir = sm_path / "definition" / "tables"
        if not tables_dir.exists():
            return {"semantic_model_path": str(sm_path), "table_count": 0, "tables": [], "note": "No tables folder found."}
        tables = []
        for tmdl_file in sorted(tables_dir.glob("*.tmdl")):
            try:
                parsed = self._parse_tmdl_table_file(tmdl_file.read_text(encoding="utf-8-sig"))
                if parsed.get("name"):
                    tables.append(parsed)
            except Exception as exc:
                tables.append({"name": tmdl_file.stem, "error": str(exc), "columns": [], "measures": []})
        return {"semantic_model_path": str(sm_path), "table_count": len(tables), "tables": tables}

    def search_semantic_model_fields(
        self,
        query: str,
        table: str | None = None,
        field_type: str | None = None,
    ) -> dict[str, Any]:
        sm = self.read_semantic_model()
        q = query.lower()
        matches: list[dict[str, Any]] = []
        for t in sm.get("tables", []):
            if t.get("name", "").startswith(("LocalDateTable_", "DateTableTemplate_")):
                continue
            tname = t.get("name", "")
            if table and table.lower() not in tname.lower():
                continue
            table_hit = q in tname.lower()
            if field_type != "measure":
                for col in t.get("columns", []):
                    cname = col.get("name", "")
                    if table_hit or q in cname.lower():
                        matches.append({
                            "table": tname,
                            "name": cname,
                            "kind": "column",
                            "dataType": col.get("dataType"),
                            "queryRef": f"{tname}.{cname}",
                        })
            if field_type != "column":
                for meas in t.get("measures", []):
                    mname = meas.get("name", "")
                    if table_hit or q in mname.lower():
                        matches.append({
                            "table": tname,
                            "name": mname,
                            "kind": "measure",
                            "queryRef": f"{tname}.{mname}",
                        })
        return {"query": query, "match_count": len(matches), "matches": matches}

    def get_report_context(self) -> str:
        """Compact text orientation of the full report — pages, visuals, bindings, positions."""
        # Theme
        rpt = self.load_report_json()
        tc = rpt.get("themeCollection") or {}
        base = (tc.get("baseTheme") or {}).get("name", "?")
        custom = (tc.get("customTheme") or {}).get("name")
        theme_str = base + (f"+{custom}" if custom else "")

        # Semantic model summary
        table_abbrevs: dict[str, str] = {}
        sm_line = "TABLES: (no semantic model)"
        try:
            sm = self.read_semantic_model()
            all_tables = sm.get("tables", [])
            # strip PBI auto-generated date intelligence tables
            tables = [t for t in all_tables if not t.get("name", "").startswith(("LocalDateTable_", "DateTableTemplate_"))]
            table_abbrevs = _make_table_abbrevs([t["name"] for t in tables])
            parts = []
            for t in tables:
                abbr = table_abbrevs.get(t["name"], t["name"][:4])
                nc = len(t.get("columns", []))
                nm = len(t.get("measures", []))
                parts.append(f"{abbr}={t['name']}({nc}c/{nm}m)")
            sm_line = "TABLES: " + "  ".join(parts)
        except Exception:
            pass

        def _abbrev_binding(b: dict) -> str:
            role = b.get("role", "?")
            table = b.get("table", "")
            field = b.get("name", "")
            kind = b.get("kind", "")
            abbr_t = table_abbrevs.get(table, table[:4] if table else "?")
            k = "M" if "measure" in kind else "C"
            return f"{role}:[{k}]{abbr_t}.{field}"

        pages_result = self.list_pages()
        pages = pages_result["pages"]
        all_visuals = self.list_visuals(_include_paths=True, include_bindings=True)["visuals"]
        vmap: dict[str, list] = {}
        for v in all_visuals:
            vmap.setdefault(str(v["page"]), []).append(v)

        report_name = self.report_root.stem
        lines = [
            f"REPORT {report_name}  {len(pages)} pages  {len(all_visuals)} visuals  theme:{theme_str}",
            sm_line,
        ]

        for page in pages:
            pname = page["name"]
            display = page.get("displayName") or pname
            pw, ph = int(page.get("width") or 1280), int(page.get("height") or 720)
            flags = "+".join(f for f, k in [("active", "is_active"), ("landing", "is_landing")] if page.get(k))
            flag_str = f" +{flags}" if flags else ""
            page_visuals = sorted(
                vmap.get(pname, []),
                key=lambda v: (v["position"].get("y", 0), v["position"].get("x", 0)),
            )
            lines.append("")
            lines.append(f"PAGE {pname} \"{display}\" {pw}x{ph}{flag_str}  {len(page_visuals)} visuals")
            for v in page_visuals:
                vt = v.get("visualType") or "?"
                vts = _VTYPE_SHORT.get(vt, vt[:8])
                title = (v.get("title") or "")[:35]
                name = v.get("name", "")
                pos = v.get("position") or {}
                x, y = int(pos.get("x", 0)), int(pos.get("y", 0))
                vw, vh = int(pos.get("width", 0)), int(pos.get("height", 0))
                bindings = v.get("bindings") or []
                bstr = "  ".join(_abbrev_binding(b) for b in bindings[:4])
                if len(bindings) > 4:
                    bstr += f"  +{len(bindings)-4}more"
                hidden = " [hidden]" if v.get("isHidden") else ""
                lines.append(f"  {vts:<7} {name:<32} \"{title}\"  {bstr}  @{x},{y} {vw}x{vh}{hidden}")

        return "\n".join(lines)

    def create_page(
        self,
        display_name: str,
        name: str | None = None,
        width: float = 1280,
        height: float = 720,
        display_option: str = "FitToPage",
        make_active: bool = False,
        background: dict[str, Any] | None = None,
        visibility: str | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        page_name = normalize_object_name(name or object_name("ReportSection"), "page name")
        page_dir = self.pages_dir / page_name
        if page_dir.exists():
            raise PBIRError(f"Page already exists: {page_name}")
        page_dir.mkdir(parents=True, exist_ok=False)
        (page_dir / "visuals").mkdir(exist_ok=True)

        page: dict[str, Any] = {
            "$schema": PAGE_SCHEMA,
            "name": page_name,
            "displayName": display_name,
            "displayOption": display_option,
            "width": float(width),
            "height": float(height),
        }
        if background:
            page["objects"] = {"background": [{"properties": background}]}
        if visibility:
            page["visibility"] = visibility
        write_json(page_dir / "page.json", page)

        metadata = self.load_pages_metadata()
        order = list(metadata.get("pageOrder") or [])
        if page_name not in order:
            order.append(page_name)
        metadata["pageOrder"] = order
        if make_active or not metadata.get("activePageName"):
            metadata["activePageName"] = page_name
        self.save_pages_metadata(metadata)

        result: dict[str, Any] = {"page": self.page_summary(page_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def update_page(
        self,
        page: str,
        display_name: str | None = None,
        width: float | None = None,
        height: float | None = None,
        display_option: str | None = None,
        visibility: str | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        page_data = read_json(page_dir / "page.json")
        if display_name is not None:
            page_data["displayName"] = display_name
        if width is not None:
            page_data["width"] = float(width)
        if height is not None:
            page_data["height"] = float(height)
        if display_option is not None:
            page_data["displayOption"] = display_option
        if visibility is not None:
            if visibility == "":
                page_data.pop("visibility", None)
            else:
                page_data["visibility"] = visibility
        write_json(page_dir / "page.json", page_data)
        result: dict[str, Any] = {"page": self.page_summary(page_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def set_active_page(self, page: str, landing: bool = False, validate_after: bool = False) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        page_data = read_json(page_dir / "page.json")
        page_name = page_data.get("name", page_dir.name)
        metadata = self.load_pages_metadata()
        metadata["activePageName"] = page_name
        if landing:
            metadata["landingPageName"] = page_name
        self.save_pages_metadata(metadata)
        result: dict[str, Any] = {
            "activePageName": metadata.get("activePageName"),
            "landingPageName": metadata.get("landingPageName"),
        }
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def delete_page(self, page: str, validate_after: bool = False) -> dict[str, Any]:
        import shutil
        page_dir = self.find_page_dir(page)
        page_name = page_dir.name
        page_data = read_json(page_dir / "page.json")
        display_name = page_data.get("displayName") or page_data.get("name") or page_name
        shutil.rmtree(page_dir)
        metadata = self.load_pages_metadata()
        order = list(metadata.get("pageOrder") or [])
        if page_name in order:
            order.remove(page_name)
        metadata["pageOrder"] = order
        if metadata.get("activePageName") == page_name:
            metadata["activePageName"] = order[0] if order else ""
        if metadata.get("landingPageName") == page_name:
            metadata["landingPageName"] = order[0] if order else ""
        self.save_pages_metadata(metadata)
        result: dict[str, Any] = {"deleted": page_name, "displayName": display_name, "pageOrder": order}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def duplicate_page(
        self,
        source_page: str,
        new_display_name: str,
        new_name: str | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        import shutil
        source_dir = self.find_page_dir(source_page)
        new_name = new_name or normalize_object_name(new_display_name, "page name")
        target_dir = self.pages_dir / new_name
        if target_dir.exists():
            raise PBIRError(f"Page '{new_name}' already exists.")
        shutil.copytree(source_dir, target_dir)
        page_json_path = target_dir / "page.json"
        data = read_json(page_json_path)
        data["name"] = new_name
        data["displayName"] = new_display_name
        write_json(page_json_path, data)
        metadata = self.load_pages_metadata()
        order = list(metadata.get("pageOrder") or [])
        src_name = source_dir.name
        if src_name in order:
            order.insert(order.index(src_name) + 1, new_name)
        else:
            order.append(new_name)
        metadata["pageOrder"] = order
        self.save_pages_metadata(metadata)
        result: dict[str, Any] = {"page": self.page_summary(target_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def reorder_pages(self, page_order: list[str], validate_after: bool = False) -> dict[str, Any]:
        metadata = self.load_pages_metadata()
        known = {p.name for p in self.page_dirs() if (p / "page.json").exists()}
        unknown = [p for p in page_order if p not in known]
        if unknown:
            raise PBIRError(f"Unknown page names: {', '.join(unknown)}. Known: {', '.join(sorted(known))}")
        trailing = [p for p in (metadata.get("pageOrder") or []) if p not in page_order]
        metadata["pageOrder"] = page_order + trailing
        self.save_pages_metadata(metadata)
        result: dict[str, Any] = {"pageOrder": metadata["pageOrder"]}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def set_visual_title(
        self,
        page: str,
        visual: str,
        title: str | None = None,
        show: bool | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        visual_dir = self.find_visual_dir(page_dir, visual)
        path = visual_dir / "visual.json"
        data = read_json(path)
        vis = data.setdefault("visual", {})
        vc = vis.setdefault("visualContainerObjects", {})
        items = vc.setdefault("title", [{}])
        props = items[0].setdefault("properties", {})
        if title is not None:
            props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            props["text"] = {"expr": {"Literal": {"Value": f"'{title}'"}}}
        if show is not None:
            props["show"] = {"expr": {"Literal": {"Value": "true" if show else "false"}}}
        write_json(path, data)
        result: dict[str, Any] = {"visual": self.visual_summary(page_dir, visual_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def next_visual_z(self, page_dir: Path) -> int:
        max_z = -1
        for visual_dir in self.visual_dirs(page_dir):
            path = visual_dir / "visual.json"
            if not path.exists():
                continue
            data = read_json(path)
            z = data.get("position", {}).get("z")
            if isinstance(z, (int, float)):
                max_z = max(max_z, int(z))
        return max_z + 1

    @staticmethod
    def _overlap_area(a: dict[str, Any], b: dict[str, Any]) -> float:
        ox = max(0.0, min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"]))
        oy = max(0.0, min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"]))
        return ox * oy

    @staticmethod
    def _layout_ascii_grid(
        visuals: list[dict[str, Any]],
        page_width: float,
        page_height: float,
        cols: int = 64,
        rows: int = 16,
    ) -> str:
        owners = [[-1] * cols for _ in range(rows)]
        chars = [["·"] * cols for _ in range(rows)]
        for vi, v in enumerate(visuals):
            if v.get("isHidden"):
                continue
            gx0 = max(0, int(v["x"] / page_width * cols))
            gy0 = max(0, int(v["y"] / page_height * rows))
            gx1 = min(cols, max(gx0 + 1, round((v["x"] + v["width"]) / page_width * cols)))
            gy1 = min(rows, max(gy0 + 1, round((v["y"] + v["height"]) / page_height * rows)))
            label = v.get("title") or v.get("name") or "?"
            for gy in range(gy0, gy1):
                for gx in range(gx0, gx1):
                    if owners[gy][gx] != -1:
                        owners[gy][gx] = -2
                        chars[gy][gx] = "!"
                    else:
                        owners[gy][gx] = vi
                        ci = gx - gx0
                        h = gy1 - gy0
                        if h == 1 or gy == gy0:
                            chars[gy][gx] = label[ci] if ci < len(label) else "─"
                        elif gy == gy1 - 1:
                            chars[gy][gx] = "─"
                        elif gx == gx0 or gx == gx1 - 1:
                            chars[gy][gx] = "│"
                        else:
                            chars[gy][gx] = " "
        top = "┌" + "─" * cols + "┐"
        bot = "└" + "─" * cols + "┘"
        lines = [top] + ["│" + "".join(row) + "│" for row in chars] + [bot]
        return "\n".join(lines)

    def page_layout_analysis(self, page_dir: Path) -> dict[str, Any]:
        """Return overlap, out-of-bounds, coverage, and ASCII grid for a page."""
        page_data = read_json(page_dir / "page.json")
        pw = float(page_data.get("width") or DEFAULT_PAGE_SIZE["width"])
        ph = float(page_data.get("height") or DEFAULT_PAGE_SIZE["height"])
        visuals: list[dict[str, Any]] = []
        for visual_dir in self.visual_dirs(page_dir):
            vpath = visual_dir / "visual.json"
            if not vpath.exists():
                continue
            data = read_json(vpath)
            pos = data.get("position") or {}
            if not all(isinstance(pos.get(f), (int, float)) for f in ("x", "y", "width", "height")):
                continue
            visuals.append({
                "name": data.get("name", visual_dir.name),
                "title": self.extract_title(data),
                "x": float(pos["x"]),
                "y": float(pos["y"]),
                "width": float(pos["width"]),
                "height": float(pos["height"]),
                "isHidden": bool(data.get("isHidden", False)),
            })
        visible = [v for v in visuals if not v["isHidden"]]
        overlaps = []
        for i, a in enumerate(visible):
            for b in visible[i + 1:]:
                area = self._overlap_area(a, b)
                if area > 0:
                    overlaps.append({"a": a["name"], "b": b["name"], "overlap_px": round(area)})
        out_of_bounds = [
            v["name"] for v in visible
            if v["x"] < 0 or v["y"] < 0 or v["x"] + v["width"] > pw or v["y"] + v["height"] > ph
        ]
        total_area = sum(v["width"] * v["height"] for v in visible)
        page_area = pw * ph
        coverage_pct = round(100.0 * total_area / page_area, 1) if page_area > 0 else 0.0
        result: dict[str, Any] = {
            "page_width": pw,
            "page_height": ph,
            "visual_count": len(visuals),
            "visible_count": len(visible),
            "overlaps": overlaps,
            "out_of_bounds": out_of_bounds,
            "coverage_pct": coverage_pct,
        }
        if visuals:
            result["grid"] = self._layout_ascii_grid(visuals, pw, ph)
        return result

    def add_visual_from_template(
        self,
        page: str,
        template: str = "blank",
        title: str | None = None,
        name: str | None = None,
        position: dict[str, Any] | None = None,
        visual_type: str | None = None,
        query_roles: dict[str, Any] | None = None,
        sort: list[dict[str, Any]] | None = None,
        filter_config: list[dict[str, Any]] | None = None,
        properties: dict[str, Any] | None = None,
        raw_patch: dict[str, Any] | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        visual_name = normalize_object_name(name or object_name("Visual"), "visual name")
        visual_dir = page_dir / "visuals" / visual_name
        if visual_dir.exists():
            raise PBIRError(f"Visual already exists: {visual_name}")

        visual_kind = visual_type or self.visual_type_for_template(template)
        _z_seed = max(self.next_visual_z(page_dir), _DEFAULT_Z.get(visual_kind, _DEFAULT_Z_FALLBACK))
        visual_json: dict[str, Any] = {
            "$schema": VISUAL_CONTAINER_SCHEMA,
            "name": visual_name,
            "position": scalar_position(position, _z_seed),
            "visual": {
                "visualType": visual_kind,
                "drillFilterOtherVisuals": True,
            },
            "howCreated": "Default",
        }

        visual_config = visual_json["visual"]
        if title:
            visual_config["visualContainerObjects"] = {
                "title": [{"properties": {"show": pbir_literal(True), "text": pbir_literal(title)}}]
            }
        role_remaps: list[dict[str, str]] = []
        if query_roles:
            query_roles, role_remaps = normalize_query_roles(visual_kind, query_roles)
            visual_config["query"] = self.build_query(query_roles, sort=sort)
        elif sort:
            visual_config["query"] = self.build_query({}, sort=sort)
        if properties:
            visual_config.setdefault("objects", {})
            deep_merge(visual_config["objects"], properties)
        if filter_config:
            visual_json["filterConfig"] = {
                "filters": [self.build_visual_filter(f) for f in filter_config]
            }
        if raw_patch:
            deep_merge(visual_json, raw_patch)

        visual_dir.mkdir(parents=True, exist_ok=False)
        write_json(visual_dir / "visual.json", visual_json)
        result: dict[str, Any] = {"visual": self.visual_summary(page_dir, visual_dir)}
        if role_remaps:
            # Surface alias corrections (e.g. Axis -> Category) so the caller knows
            # the roles it sent were adjusted to render for this visual type.
            result["role_remaps"] = role_remaps
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    @staticmethod
    def visual_type_for_template(template: str) -> str:
        mapping = {
            "blank": "card",
            "card": "card",
            "cardVisual": "cardVisual",
            "cardv2": "cardVisual",
            "bar": "clusteredBarChart",
            "barChart": "clusteredBarChart",
            "clusteredBarChart": "clusteredBarChart",
            "column": "clusteredColumnChart",
            "columnChart": "clusteredColumnChart",
            "line": "lineChart",
            "lineChart": "lineChart",
            "table": "tableEx",
            "tableEx": "tableEx",
            "matrix": "pivotTable",
            "slicer": "slicer",
            "textbox": "textbox",
            "combo": "lineClusteredColumnComboChart",
            "comboChart": "lineClusteredColumnComboChart",
            "lineClusteredColumnComboChart": "lineClusteredColumnComboChart",
            "combostack": "lineStackedColumnComboChart",
            "lineStackedColumnComboChart": "lineStackedColumnComboChart",
            "actionButton": "actionButton",
            "actionbtn": "actionButton",
            "button": "actionButton",
            "pageNavigator": "pageNavigator",
            "pagenav": "pageNavigator",
            "advancedSlicerVisual": "advancedSlicerVisual",
            "advslicr": "advancedSlicerVisual",
            "buttonSlicer": "advancedSlicerVisual",
            "waterfallChart": "waterfallChart",
            "waterfall": "waterfallChart",
            "scatterChart": "scatterChart",
            "scatter": "scatterChart",
            "bubbleChart": "bubbleChart",
            "bubble": "bubbleChart",
            "funnelChart": "funnelChart",
            "funnel": "funnelChart",
            "treemap": "treemap",
            "kpi": "kpi",
            "gauge": "gauge",
            "filledMap": "filledMap",
            "filledmap": "filledMap",
            "azureMap": "azureMap",
            "azuremap": "azureMap",
            "image": "image",
            "shape": "shape",
        }
        if template not in mapping:
            raise PBIRError(f"Unknown template '{template}'. Use one of: {', '.join(sorted(mapping))}")
        return mapping[template]

    def visual_summary(self, page_dir: Path, visual_dir: Path) -> dict[str, Any]:
        page_data = read_json(page_dir / "page.json")
        data = read_json(visual_dir / "visual.json")
        return {
            "page": page_data.get("name", page_dir.name),
            "name": data.get("name", visual_dir.name),
            "title": self.extract_title(data),
            "visualType": (data.get("visual") or {}).get("visualType"),
            "position": data.get("position", {}),
        }

    @staticmethod
    def build_query(
        query_roles: dict[str, Any],
        sort: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        query_state: dict[str, Any] = {}
        for role, projections in query_roles.items():
            if not isinstance(projections, list):
                projections = [projections]
            query_state[role] = {
                "projections": [PBIRReport.build_projection(projection) for projection in projections]
            }
        result: dict[str, Any] = {"queryState": query_state}
        if sort:
            result["sortDefinition"] = {
                "sort": [PBIRReport.build_sort_entry(s) for s in sort]
            }
        return result

    @staticmethod
    def build_projection(spec: dict[str, Any]) -> dict[str, Any]:
        if "raw" in spec:
            raw = copy.deepcopy(spec["raw"])
            if "queryRef" not in raw:
                raise PBIRError("Raw projection must include queryRef.")
            return raw
        if "native_calc" in spec:
            nc = spec["native_calc"]
            nc_name = nc if isinstance(nc, str) else nc.get("name", "")
            if not nc_name:
                raise PBIRError("native_calc projection must include a name.")
            nc_expr = nc.get("expression") if isinstance(nc, dict) else None
            nc_field: dict[str, Any] = {"NativeVisualCalculation": {"Name": nc_name}}
            if nc_expr:
                nc_field["NativeVisualCalculation"]["Expression"] = nc_expr
            nc_qr = spec.get("queryRef") or nc_name
            nc_proj: dict[str, Any] = {"field": nc_field, "queryRef": nc_qr}
            for optional in ("nativeQueryRef", "displayName", "format", "active", "hidden"):
                if optional in spec:
                    nc_proj[optional] = spec[optional]
            return nc_proj
        table = spec.get("table")
        if not table:
            raise PBIRError("Projection must include table, or use raw.")
        if "measure" in spec:
            field_name = spec["measure"]
            field = {
                "Measure": {
                    "Expression": {"SourceRef": {"Entity": table}},
                    "Property": field_name,
                }
            }
        elif "column" in spec:
            field_name = spec["column"]
            column_field = {
                "Column": {
                    "Expression": {"SourceRef": {"Entity": table}},
                    "Property": field_name,
                }
            }
            aggregate = spec.get("aggregate")
            if aggregate:
                field = {
                    "Aggregation": {
                        "Expression": column_field,
                        "Function": PBIRReport.aggregate_function(aggregate),
                    }
                }
            else:
                field = column_field
        elif "hierarchy_level" in spec:
            hl = spec["hierarchy_level"]
            level = hl["level"]
            hierarchy = hl.get("hierarchy", "Date Hierarchy")
            variation_name = hl.get("variation_name", "Variation")
            variation_property = hl.get("variation_property", "Date")
            if hl.get("auto_date", True):
                field = {
                    "HierarchyLevel": {
                        "Expression": {
                            "Hierarchy": {
                                "Expression": {
                                    "PropertyVariationSource": {
                                        "Expression": {"SourceRef": {"Entity": table}},
                                        "Name": variation_name,
                                        "Property": variation_property,
                                    }
                                },
                                "Hierarchy": hierarchy,
                            }
                        },
                        "Level": level,
                    }
                }
                query_ref = spec.get("queryRef") or (
                    f"{table}.{variation_property}.{variation_name}.{hierarchy}.{level}"
                )
            else:
                hierarchy_column = hl["column"]
                field = {
                    "HierarchyLevel": {
                        "Expression": {
                            "Hierarchy": {
                                "Expression": {"SourceRef": {"Entity": table}},
                                "Hierarchy": hierarchy,
                            }
                        },
                        "Level": level,
                    }
                }
                query_ref = spec.get("queryRef") or f"{table}.{hierarchy_column}.{hierarchy}.{level}"
            result = {"field": field, "queryRef": query_ref}
            for optional in ("nativeQueryRef", "displayName", "format", "active", "hidden"):
                if optional in spec:
                    result[optional] = spec[optional]
            return result
        else:
            raise PBIRError("Projection must include measure, column, hierarchy_level, or native_calc.")
        query_ref = spec.get("queryRef") or f"{table}.{field_name}"
        result = {"field": field, "queryRef": query_ref}
        for optional in ("nativeQueryRef", "displayName", "format", "active", "hidden"):
            if optional in spec:
                result[optional] = spec[optional]
        return result

    @staticmethod
    def aggregate_function(name: str | int) -> int:
        if isinstance(name, int):
            return name
        mapping = {
            "sum": 0,
            "average": 1,
            "avg": 1,
            "distinctCount": 2,
            "distinct_count": 2,
            "min": 3,
            "max": 4,
            "count": 5,
            "median": 6,
            "standardDeviation": 7,
            "variance": 8,
        }
        key = str(name)
        if key not in mapping:
            raise PBIRError(f"Unknown aggregate '{name}'.")
        return mapping[key]

    @staticmethod
    def build_sort_entry(spec: dict[str, Any]) -> dict[str, Any]:
        direction = spec.get("direction", "Descending")
        if direction.lower() in ("asc", "ascending"):
            direction = "Ascending"
        else:
            direction = "Descending"
        field = PBIRReport.build_projection(spec)["field"]
        return {"field": field, "direction": direction}

    @staticmethod
    def build_visual_filter(spec: dict[str, Any]) -> dict[str, Any]:
        filter_type = spec.get("type", "Categorical")
        field_proj = PBIRReport.build_projection(spec)
        field = field_proj["field"]
        entry: dict[str, Any] = {
            "name": object_name("filter"),
            "field": field,
            "type": filter_type,
        }
        if "filter" in spec:
            entry["filter"] = spec["filter"]
            return entry

        table = spec.get("table", "")
        alias = table[0].lower() if table else "t"
        from_entry = {"Name": alias, "Entity": table, "Type": 0}
        filter_def: dict[str, Any] = {"Version": 2, "From": [from_entry], "Where": []}

        def _alias_field(f: dict[str, Any]) -> dict[str, Any]:
            import copy as _copy
            af = _copy.deepcopy(f)
            for node in [af.get("Measure"), af.get("Column"), af.get("HierarchyLevel")]:
                if node and isinstance(node.get("Expression"), dict):
                    sr = node["Expression"].get("SourceRef")
                    if sr:
                        sr.pop("Entity", None)
                        sr["Source"] = alias
                    pv = node["Expression"].get("PropertyVariationSource")
                    if pv:
                        pv.get("Expression", {}).get("SourceRef", {}).pop("Entity", None)
                        pv["Expression"]["SourceRef"]["Source"] = alias
            if "HierarchyLevel" in af:
                hl_node = af["HierarchyLevel"]
                h_expr = hl_node.get("Expression", {}).get("Hierarchy", {}).get("Expression", {})
                pv2 = h_expr.get("PropertyVariationSource")
                if pv2:
                    pv2.get("Expression", {}).get("SourceRef", {}).pop("Entity", None)
                    pv2["Expression"]["SourceRef"]["Source"] = alias
            return af

        aliased_field = _alias_field(field)

        kind = spec.get("filter_kind", "in")
        if kind in ("in", "not_in"):
            values = spec.get("values", [])
            where_cond: dict[str, Any] = {
                "Condition": {
                    "In": {
                        "Expressions": [aliased_field],
                        "Values": [[{"Literal": {"Value": _to_pbi_literal(v)}} for v in values] if values else []],
                    }
                }
            }
            if kind == "not_in":
                where_cond = {"Condition": {"Not": {"Expression": where_cond["Condition"]}}}
        elif kind == "comparison":
            op_map = {
                "eq": 0, "=": 0, "==": 0,
                "lt": 1, "<": 1,
                "le": 2, "<=": 2,
                "ge": 3, ">=": 3,
                "gt": 4, ">": 4,
                "ne": 5, "!=": 5, "<>": 5,
            }
            op_key = str(spec.get("operator", "ge")).lower()
            comparison_kind = op_map.get(op_key, 3)
            where_cond = {
                "Condition": {
                    "Comparison": {
                        "ComparisonKind": comparison_kind,
                        "Left": aliased_field,
                        "Right": {"Literal": {"Value": _to_pbi_literal(spec["value"])}},
                    }
                }
            }
        elif kind == "range":
            # Range: min_value / max_value with optional include_min / include_max (default True)
            range_conditions: list[dict[str, Any]] = []
            if "min_value" in spec:
                op_kind = 2 if spec.get("include_min", True) else 1  # >= or >
                range_conditions.append({
                    "Comparison": {
                        "ComparisonKind": op_kind,
                        "Left": aliased_field,
                        "Right": {"Literal": {"Value": _to_pbi_literal(spec["min_value"])}},
                    }
                })
            if "max_value" in spec:
                op_kind = 4 if spec.get("include_max", True) else 3  # <= or <
                range_conditions.append({
                    "Comparison": {
                        "ComparisonKind": op_kind,
                        "Left": aliased_field,
                        "Right": {"Literal": {"Value": _to_pbi_literal(spec["max_value"])}},
                    }
                })
            if not range_conditions:
                raise PBIRError("Range filter requires min_value and/or max_value.")
            if len(range_conditions) == 1:
                range_cond = range_conditions[0]
            else:
                range_cond = {"And": {"Left": {"Condition": range_conditions[0]}, "Right": {"Condition": range_conditions[1]}}}
            where_cond = {"Condition": range_cond}
            entry["type"] = spec.get("type", "Advanced")
        elif kind == "top_n":
            by_table = spec.get("by_table") or table
            by_measure = spec.get("by_measure")
            if not by_measure:
                raise PBIRError("TopN filter requires by_measure.")
            by_expr = {
                "Measure": {
                    "Expression": {"SourceRef": {"Entity": by_table}},
                    "Property": by_measure,
                }
            }
            direction = "Bottom" if spec.get("bottom") else "Top"
            where_cond = {
                "Condition": {
                    "TopN": {
                        "Direction": direction,
                        "Count": int(spec["top_n"]),
                        "Expression": aliased_field,
                        "OrderBy": [{"Direction": "Descending", "Expression": by_expr}],
                    }
                }
            }
            entry["type"] = spec.get("type", "TopN")
        elif kind == "relative_date":
            rd = spec.get("relative_date", spec)
            _period_map = {
                "days": 0, "weeks": 1, "months": 2, "years": 3,
                "calendarWeeks": 4, "calendarMonths": 5, "calendarYears": 6,
                "calendarweeks": 4, "calendarmonths": 5, "calendaryears": 6,
            }
            _dir_map = {"last": 0, "previous": 0, "next": 1, "this": 2, "current": 2}
            period_str = rd.get("period", "days") if isinstance(rd, dict) else spec.get("period", "days")
            period_code = _period_map.get(period_str)
            if period_code is None:
                raise PBIRError(f"RelativeDate period must be one of {list(_period_map.keys())}, got '{period_str}'.")
            direction_str = (rd.get("direction", "last") if isinstance(rd, dict) else spec.get("direction", "last"))
            direction_code = _dir_map.get(direction_str)
            if direction_code is None:
                raise PBIRError(f"RelativeDate direction must be 'last', 'next', or 'this', got '{direction_str}'.")
            count = (rd.get("count", 1) if isinstance(rd, dict) else spec.get("count", 1))
            include_today = (rd.get("include_today", False) if isinstance(rd, dict) else spec.get("include_today", False))
            where_cond = {
                "Condition": {
                    "RelativeDate": {
                        "FilterType": direction_code,
                        "Period": period_code,
                        "TimeUnitsCount": count,
                        "IncludeToday": include_today,
                        "Expression": aliased_field,
                    }
                }
            }
            entry["type"] = spec.get("type", "RelativeDate")
        else:
            raise PBIRError(f"Unknown filter_kind '{kind}'. Use in, not_in, comparison, range, top_n, or relative_date.")

        filter_def["Where"].append(where_cond)
        entry["filter"] = filter_def
        if spec.get("isHiddenInViewMode"):
            entry["isHiddenInViewMode"] = True
        if spec.get("isLockedInViewMode"):
            entry["isLockedInViewMode"] = True
        return entry

    def clone_visual(
        self,
        source_page: str,
        source_visual: str,
        target_page: str,
        name: str | None = None,
        title: str | None = None,
        position: dict[str, Any] | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        source_page_dir = self.find_page_dir(source_page)
        source_visual_dir = self.find_visual_dir(source_page_dir, source_visual)
        target_page_dir = self.find_page_dir(target_page)
        target_name = normalize_object_name(name or object_name("Visual"), "visual name")
        target_visual_dir = target_page_dir / "visuals" / target_name
        if target_visual_dir.exists():
            raise PBIRError(f"Visual already exists: {target_name}")
        data = read_json(source_visual_dir / "visual.json")
        data["name"] = target_name
        if position:
            data["position"] = scalar_position(position, self.next_visual_z(target_page_dir))
        else:
            pos = dict(data.get("position") or {})
            pos["z"] = self.next_visual_z(target_page_dir)
            data["position"] = scalar_position(pos, pos["z"])
        if title:
            visual = data.setdefault("visual", {})
            containers = visual.setdefault("visualContainerObjects", {})
            containers["title"] = [{"properties": {"show": pbir_literal(True), "text": pbir_literal(title)}}]
        target_visual_dir.mkdir(parents=True)
        write_json(target_visual_dir / "visual.json", data)
        mobile = source_visual_dir / "mobile.json"
        if mobile.exists():
            write_json(target_visual_dir / "mobile.json", read_json(mobile))
        result: dict[str, Any] = {"visual": self.visual_summary(target_page_dir, target_visual_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def set_visual_position(
        self,
        page: str,
        visual: str,
        position: dict[str, Any],
        validate_after: bool = False,
    ) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        visual_dir = self.find_visual_dir(page_dir, visual)
        path = visual_dir / "visual.json"
        data = read_json(path)
        current = dict(data.get("position") or {})
        current.update(position)
        data["position"] = scalar_position(current, int(current.get("z", self.next_visual_z(page_dir))))
        write_json(path, data)
        result: dict[str, Any] = {"visual": self.visual_summary(page_dir, visual_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def modify_visual_properties(
        self,
        page: str,
        visual: str,
        patch: dict[str, Any],
        validate_after: bool = False,
    ) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        visual_dir = self.find_visual_dir(page_dir, visual)
        path = visual_dir / "visual.json"
        data = read_json(path)
        deep_merge(data, patch)
        write_json(path, data)
        result: dict[str, Any] = {"visual": self.visual_summary(page_dir, visual_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def recolor_visuals(
        self,
        targets: dict[str, str],
        page: str | None = None,
        visual_types: list[str] | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        unknown = set(targets) - RECOLOR_TARGETS
        if unknown:
            raise PBIRError(
                f"Unknown recolor targets: {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(RECOLOR_TARGETS))}."
            )
        all_visuals = self.list_visuals(page, _include_paths=True)["visuals"]
        if visual_types:
            vt_lower = {vt.lower() for vt in visual_types}
            all_visuals = [v for v in all_visuals if (v.get("visualType") or "").lower() in vt_lower]
        changed = []
        for v in all_visuals:
            page_dir = self.find_page_dir(v["page"])
            visual_dir = self.find_visual_dir(page_dir, v["name"])
            path = visual_dir / "visual.json"
            data = read_json(path)
            did_change = False
            for t, c in targets.items():
                if _apply_color_target(data, t, c):
                    did_change = True
            if did_change:
                write_json(path, data)
                changed.append({"page": v["page"], "name": v["name"]})
        result: dict[str, Any] = {"changed_count": len(changed), "changed": changed}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def set_page_background(
        self,
        page: str,
        color: str,
        outspace_color: str | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        """Set canvas background and wallpaper (outspace) for a page.

        color       = canvas background hex (#RRGGBB)
        outspace    = wallpaper/area-outside-canvas hex; defaults to color
        """
        page_dir = self.find_page_dir(page)
        path = page_dir / "page.json"
        data = read_json(path)
        # Upgrade schema to 2.1.0 which PBI Desktop requires for page objects
        data["$schema"] = data["$schema"].replace("/2.0.0/", "/2.1.0/")
        out_color = outspace_color or color
        objects = data.setdefault("objects", {})
        objects["background"] = [{"properties": {"color": _color_expr(color)}}]
        objects["outspace"] = [{"properties": {"color": _color_expr(out_color)}}]
        write_json(path, data)
        result: dict[str, Any] = {"page": page, "background": color, "outspace": out_color}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def delete_visual(self, page: str, visual: str, validate_after: bool = False) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        visual_dir = self.find_visual_dir(page_dir, visual)
        shutil.rmtree(visual_dir)
        result: dict[str, Any] = {"deleted": str(visual_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def set_visual_filter(
        self,
        page: str,
        visual: str,
        filters: list[dict[str, Any]],
        merge: bool = False,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        page_dir = self.find_page_dir(page)
        visual_dir = self.find_visual_dir(page_dir, visual)
        path = visual_dir / "visual.json"
        data = read_json(path)
        new_filters = [self.build_visual_filter(f) for f in filters]
        if merge:
            existing = data.setdefault("filterConfig", {}).setdefault("filters", [])
            existing.extend(new_filters)
        else:
            data["filterConfig"] = {"filters": new_filters}
        write_json(path, data)
        result: dict[str, Any] = {"visual": self.visual_summary(page_dir, visual_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def set_page_filter(
        self,
        page: str,
        filters: list[dict[str, Any]],
        validate_after: bool = False,
    ) -> dict[str, Any]:
        """Set or replace page-level filters. Pass [] to clear all page filters."""
        page_dir = self.find_page_dir(page)
        path = page_dir / "page.json"
        data = read_json(path)
        built = [self.build_visual_filter(f) for f in filters]
        if built:
            data["filterConfig"] = {"filters": built}
        else:
            data.pop("filterConfig", None)
        write_json(path, data)
        result: dict[str, Any] = {
            "page": page_dir.name,
            "filter_count": len(built),
        }
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def manage_visual_calc(
        self,
        page: str,
        visual: str,
        action: str,
        role: str,
        name: str | None = None,
        expression: dict[str, Any] | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        """Add, remove, or list NativeVisualCalculation projections on a visual."""
        page_dir = self.find_page_dir(page)
        visual_dir = self.find_visual_dir(page_dir, visual)
        path = visual_dir / "visual.json"
        data = read_json(path)

        query_state = data.get("visual", {}).get("query", {}).get("queryState", {})

        if action == "list":
            found = []
            for r, role_data in query_state.items():
                for p in role_data.get("projections", []):
                    if "NativeVisualCalculation" in p.get("field", {}):
                        found.append({"role": r, "queryRef": p.get("queryRef"), "field": p["field"]})
            return {"visual": visual, "native_calcs": found}

        if action == "add":
            if not name:
                raise PBIRError("name is required for action='add'.")
            proj = self.build_projection({"native_calc": {"name": name, "expression": expression} if expression else name})
            role_data = query_state.setdefault(role, {"projections": []})
            role_data.setdefault("projections", []).append(proj)
            if "query" not in data.get("visual", {}):
                data.setdefault("visual", {})["query"] = {"queryState": query_state}
            else:
                data["visual"]["query"]["queryState"] = query_state
            write_json(path, data)
            result: dict[str, Any] = {"action": "added", "visual": visual, "role": role, "name": name}
            if validate_after:
                result["validation"] = validation_summary(self.validate().as_dict())
            return result

        if action == "remove":
            if not name:
                raise PBIRError("name is required for action='remove'.")
            removed = 0
            for r, role_data in query_state.items():
                before = role_data.get("projections", [])
                after = [p for p in before if not (
                    "NativeVisualCalculation" in p.get("field", {}) and
                    p.get("queryRef") == name
                )]
                if len(after) < len(before):
                    role_data["projections"] = after
                    removed += len(before) - len(after)
            if removed:
                data["visual"]["query"]["queryState"] = query_state
                write_json(path, data)
            result = {"action": "removed", "visual": visual, "name": name, "removed_count": removed}
            if validate_after:
                result["validation"] = validation_summary(self.validate().as_dict())
            return result

        raise PBIRError(f"Unknown action '{action}'. Use add, remove, or list.")

    # ── Bookmarks ─────────────────────────────────────────────────────────────

    @property
    def bookmarks_dir(self) -> Path:
        return self.definition_dir / "bookmarks"

    @property
    def bookmarks_index(self) -> Path:
        return self.bookmarks_dir / "bookmarks.json"

    def list_bookmarks(self) -> list[dict[str, Any]]:
        """Return all bookmarks from the bookmarks index."""
        if not self.bookmarks_index.exists():
            return []
        index = read_json(self.bookmarks_index)
        order: list[str] = index.get("bookmarkOrder", [])
        result = []
        for name in order:
            bm_path = self.bookmarks_dir / name / "bookmark.json"
            if bm_path.exists():
                bm = read_json(bm_path)
                result.append({
                    "name": bm.get("name", name),
                    "displayName": bm.get("displayName", name),
                    "path": str(bm_path),
                })
            else:
                result.append({"name": name, "displayName": name, "path": None})
        return result

    def manage_bookmark(
        self,
        action: str,
        name: str | None = None,
        display_name: str | None = None,
        page: str | None = None,
        visible_visuals: list[str] | None = None,
        hidden_visuals: list[str] | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        """Create, delete, or list bookmarks.

        action='create': create a visibility bookmark. name required.
        action='delete': remove bookmark by name.
        action='list': return all bookmarks.
        """
        if action == "list":
            return {"bookmarks": self.list_bookmarks()}

        if not name:
            raise PBIRError("name is required for bookmark create/delete.")

        if action == "delete":
            bm_dir = self.bookmarks_dir / name
            if bm_dir.exists():
                shutil.rmtree(bm_dir)
            if self.bookmarks_index.exists():
                index = read_json(self.bookmarks_index)
                order = [n for n in index.get("bookmarkOrder", []) if n != name]
                index["bookmarkOrder"] = order
                write_json(self.bookmarks_index, index)
            result: dict[str, Any] = {"action": "deleted", "name": name}
            if validate_after:
                result["validation"] = validation_summary(self.validate().as_dict())
            return result

        if action == "create":
            bm_name = name
            bm_display = display_name or name
            entities: dict[str, Any] = {}
            if page and (visible_visuals or hidden_visuals):
                page_dir = self.find_page_dir(page)
                page_data = read_json(page_dir / "page.json")
                page_internal_name = page_data.get("name", page_dir.name)
                visual_containers: dict[str, Any] = {}
                for vn in (visible_visuals or []):
                    visual_containers[vn] = {"singleVisual": {"display": {"mode": "visible"}}}
                for vn in (hidden_visuals or []):
                    visual_containers[vn] = {"singleVisual": {"display": {"mode": "hidden"}}}
                if visual_containers:
                    entities[page_internal_name] = {"visualContainers": visual_containers}

            bm_data: dict[str, Any] = {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/bookmark/1.0.0/schema.json",
                "name": bm_name,
                "displayName": bm_display,
            }
            if entities:
                bm_data["explorationState"] = {"entities": entities}

            bm_dir = self.bookmarks_dir / bm_name
            bm_dir.mkdir(parents=True, exist_ok=True)
            write_json(bm_dir / "bookmark.json", bm_data)

            # Update bookmarks index
            self.bookmarks_dir.mkdir(parents=True, exist_ok=True)
            if self.bookmarks_index.exists():
                index = read_json(self.bookmarks_index)
            else:
                index = {"bookmarkOrder": []}
            order = list(index.get("bookmarkOrder", []))
            if bm_name not in order:
                order.append(bm_name)
            index["bookmarkOrder"] = order
            write_json(self.bookmarks_index, index)

            result = {"action": "created", "name": bm_name, "displayName": bm_display}
            if validate_after:
                result["validation"] = validation_summary(self.validate().as_dict())
            return result

        raise PBIRError(f"Unknown bookmark action '{action}'. Use create, delete, or list.")

    # ── Conditional Formatting ─────────────────────────────────────────────────

    # Maps shorthand target names to (objects_section, property_name) tuples.
    # Callers can also pass "section.property" directly for full control.
    _CF_TARGET_PATH: dict[str, tuple[str, str]] = {
        "backColor": ("values", "backColor"),
        "fontColor": ("values", "fontColor"),
        "fill": ("dataPoint", "fill"),
        "dataPointColor": ("dataPoint", "color"),
        "calloutValue": ("calloutValue", "color"),
        "titleFontColor": ("title", "fontColor"),
        "labelColor": ("labels", "color"),
    }
    # Targets that live in visualContainerObjects, not objects
    _CF_VCO_TARGETS: frozenset[str] = frozenset({"titleFontColor", "titleBackground"})

    def set_conditional_format(
        self,
        page: str,
        visual: str,
        target: str,
        format_type: str,
        spec: dict[str, Any],
        validate_after: bool = False,
    ) -> dict[str, Any]:
        """Bind conditional formatting to a visual property.

        target: shorthand key from _CF_TARGET_PATH (e.g. 'backColor', 'calloutValue')
                or 'section.property' (e.g. 'values.backColor', 'dataPoint.fill').
        format_type: 'measure' | 'column' | 'clear'
          'measure': spec = {table, measure}
          'column':  spec = {table, column, aggregate: 0-5}
          'clear':   remove the target property entirely

        gradient and rules conditional formatting must be set via raw_patch on the visual
        (the PBIR expression structure is too complex to template reliably).
        """
        if format_type in ("gradient", "rules"):
            raise PBIRError(
                f"format_type='{format_type}' must be set via raw_patch on the visual. "
                "Build the LinearGradient2 or Rules expression manually and pass it as "
                "raw_patch={'visual': {'objects': {'<section>': [{'properties': {'<prop>': {...}}}]}}}."
            )

        page_dir = self.find_page_dir(page)
        visual_dir = self.find_visual_dir(page_dir, visual)
        path = visual_dir / "visual.json"
        data = read_json(path)

        # Resolve section + property from target
        if target in self._CF_TARGET_PATH:
            section, prop = self._CF_TARGET_PATH[target]
        elif "." in target:
            section, prop = target.split(".", 1)
        else:
            section, prop = target, target

        use_vco = target in self._CF_VCO_TARGETS
        vis = data.setdefault("visual", {})
        objects: dict[str, Any] = vis.setdefault("visualContainerObjects" if use_vco else "objects", {})

        if format_type == "clear":
            section_list = objects.get(section, [])
            if section_list and isinstance(section_list, list) and section_list[0].get("properties", {}).get(prop) is not None:
                del section_list[0]["properties"][prop]
                if not section_list[0]["properties"]:
                    objects.pop(section, None)
            write_json(path, data)
            result: dict[str, Any] = {"action": "cleared", "target": target, "visual": visual}
            if validate_after:
                result["validation"] = validation_summary(self.validate().as_dict())
            return result

        # Build the color expression
        table = spec.get("table")
        if not table:
            raise PBIRError("spec must include table.")

        if format_type == "measure":
            measure = spec.get("measure")
            if not measure:
                raise PBIRError("spec must include measure for format_type='measure'.")
            color_expr: dict[str, Any] = {
                "Measure": {
                    "Expression": {"SourceRef": {"Entity": table}},
                    "Property": measure,
                }
            }
        elif format_type == "column":
            column = spec.get("column")
            if not column:
                raise PBIRError("spec must include column for format_type='column'.")
            col_expr: dict[str, Any] = {
                "Column": {
                    "Expression": {"SourceRef": {"Entity": table}},
                    "Property": column,
                }
            }
            aggregate = spec.get("aggregate")
            if aggregate is not None:
                color_expr = {"Aggregation": {"Expression": col_expr, "Function": int(aggregate)}}
            else:
                color_expr = col_expr
        else:
            raise PBIRError(f"Unknown format_type '{format_type}'. Use measure, column, or clear.")

        wrapped: dict[str, Any] = {
            "solid": {"color": {"expr": color_expr}}
        }

        section_list: list[dict[str, Any]] = objects.setdefault(section, [{}])
        if not section_list:
            section_list.append({})
            objects[section] = section_list
        section_list[0].setdefault("properties", {})[prop] = wrapped

        write_json(path, data)
        result = {"action": "set", "target": target, "format_type": format_type, "visual": visual}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    # ── Bulk Visual Ops ────────────────────────────────────────────────────────

    def bulk_visual_op(
        self,
        page: str,
        op: str,
        filter_type: str | None = None,
        filter_name: str | None = None,
        filter_bounds: dict[str, Any] | None = None,
        update: dict[str, Any] | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        """Apply an operation to all visuals on a page matching the given filters.

        op: 'list' | 'delete' | 'hide' | 'show' | 'update_title' | 'update_position'
        filter_type: match visuals whose visualType equals this string
        filter_name: match visuals whose name contains this substring
        filter_bounds: dict with optional x_min/x_max/y_min/y_max keys to filter by position
        update: for update_title: {'title': '...'}, for update_position: position fields
        """
        page_dir = self.find_page_dir(page)
        matched_dirs: list[Path] = []

        for visual_dir in self.visual_dirs(page_dir):
            vpath = visual_dir / "visual.json"
            if not vpath.exists():
                continue
            vdata = read_json(vpath)
            vtype = (vdata.get("visual") or {}).get("visualType", "")
            vname = vdata.get("name", visual_dir.name)
            pos = vdata.get("position", {})

            if filter_type and vtype != filter_type:
                continue
            if filter_name and filter_name.lower() not in vname.lower():
                continue
            if filter_bounds:
                x = float(pos.get("x", 0))
                y = float(pos.get("y", 0))
                if filter_bounds.get("x_min") is not None and x < float(filter_bounds["x_min"]):
                    continue
                if filter_bounds.get("x_max") is not None and x > float(filter_bounds["x_max"]):
                    continue
                if filter_bounds.get("y_min") is not None and y < float(filter_bounds["y_min"]):
                    continue
                if filter_bounds.get("y_max") is not None and y > float(filter_bounds["y_max"]):
                    continue
            matched_dirs.append(visual_dir)

        matched = len(matched_dirs)
        affected = 0
        summary: list[dict[str, Any]] = []

        for visual_dir in matched_dirs:
            vpath = visual_dir / "visual.json"
            vdata = read_json(vpath)
            vname = vdata.get("name", visual_dir.name)
            vtype = (vdata.get("visual") or {}).get("visualType", "")
            summary.append({"name": vname, "type": vtype})

            if op == "list":
                continue

            if op == "delete":
                shutil.rmtree(visual_dir)
                affected += 1

            elif op == "hide":
                vdata["isHidden"] = True
                write_json(vpath, vdata)
                affected += 1

            elif op == "show":
                vdata.pop("isHidden", None)
                write_json(vpath, vdata)
                affected += 1

            elif op == "update_title":
                title_text = (update or {}).get("title", "")
                escaped_title = title_text.replace("'", "''")
                vdata.setdefault("visual", {}).setdefault("visualContainerObjects", {})
                vdata["visual"]["visualContainerObjects"]["title"] = [{
                    "properties": {
                        "text": {"expr": {"Literal": {"Value": f"'{escaped_title}'"}}}
                    }
                }]
                write_json(vpath, vdata)
                affected += 1

            elif op == "update_position":
                pos = vdata.get("position", {})
                for k, v in (update or {}).items():
                    if k in POSITION_KEYS:
                        pos[k] = float(v)
                vdata["position"] = pos
                write_json(vpath, vdata)
                affected += 1

            else:
                raise PBIRError(f"Unknown bulk_visual_op op '{op}'. Use: list, delete, hide, show, update_title, update_position.")

        result: dict[str, Any] = {"op": op, "matched": matched, "affected": affected, "visuals": summary}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    # ── Custom Visuals ─────────────────────────────────────────────────────────

    def list_custom_visuals(self) -> list[dict[str, Any]]:
        """Return custom visuals registered in report.json resourcePackages."""
        report = self.load_report_json()
        result = []
        for pkg in report.get("resourcePackages", []):
            for item in pkg.get("items", []):
                if item.get("type") == "CustomVisual":
                    result.append({
                        "name": item.get("name"),
                        "path": item.get("path"),
                        "packageName": pkg.get("name"),
                    })
        return result

    def import_custom_visual(
        self,
        pbiviz_path: str,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        """Import a .pbiviz custom visual file into the report.

        Copies the file to StaticResources/RegisteredResources/ and registers
        it in report.json under a RegisteredResources resourcePackage.
        The .pbiviz is a ZIP containing package.json with visual metadata.
        """
        import zipfile
        src = Path(pbiviz_path).expanduser().resolve()
        if not src.exists():
            raise PBIRError(f".pbiviz file not found: {src}")
        if not src.suffix.lower() == ".pbiviz":
            raise PBIRError(f"Expected a .pbiviz file, got: {src.name}")

        # Extract metadata from package.json inside the ZIP
        try:
            with zipfile.ZipFile(src, "r") as zf:
                pkg_json_names = [n for n in zf.namelist() if n.lower() == "package.json"]
                if not pkg_json_names:
                    raise PBIRError("No package.json found inside .pbiviz ZIP.")
                pkg_data = json.loads(zf.read(pkg_json_names[0]).decode("utf-8"))
        except zipfile.BadZipFile as e:
            raise PBIRError(f"Not a valid .pbiviz ZIP: {e}") from e

        visual_meta = pkg_data.get("visual", pkg_data)
        visual_name = visual_meta.get("name") or src.stem
        visual_guid = visual_meta.get("guid") or visual_name
        visual_version = visual_meta.get("version", "")

        # Copy to StaticResources/RegisteredResources/
        resource_dir = self.report_root / "StaticResources" / "RegisteredResources"
        resource_dir.mkdir(parents=True, exist_ok=True)
        dest_name = f"{visual_guid}.pbiviz"
        dest = resource_dir / dest_name
        shutil.copy2(src, dest)

        # Register in report.json
        report = self.load_report_json()
        packages: list[dict[str, Any]] = report.setdefault("resourcePackages", [])

        # Find or create RegisteredResources package
        rr_pkg: dict[str, Any] | None = None
        for pkg in packages:
            if pkg.get("type") == "RegisteredResources" and pkg.get("name") == "RegisteredResources":
                rr_pkg = pkg
                break
        if rr_pkg is None:
            rr_pkg = {"name": "RegisteredResources", "type": "RegisteredResources", "items": []}
            packages.append(rr_pkg)

        items: list[dict[str, Any]] = rr_pkg.setdefault("items", [])
        # Remove existing entry with same name/guid
        items[:] = [i for i in items if i.get("name") != visual_guid]
        items.append({
            "name": visual_guid,
            "path": dest_name,
            "type": "CustomVisual",
        })
        write_json(self.report_json_path(), report)

        result: dict[str, Any] = {
            "action": "imported",
            "name": visual_name,
            "guid": visual_guid,
            "version": visual_version,
            "path": str(dest),
        }
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def remove_custom_visual(
        self,
        guid: str,
        delete_file: bool = True,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        """Remove a custom visual registration from report.json (and optionally delete the file)."""
        report = self.load_report_json()
        removed_items: list[str] = []
        for pkg in report.get("resourcePackages", []):
            before = len(pkg.get("items", []))
            pkg["items"] = [i for i in pkg.get("items", []) if i.get("name") != guid]
            for _ in range(before - len(pkg["items"])):
                removed_items.append(guid)

        if delete_file:
            resource_dir = self.report_root / "StaticResources" / "RegisteredResources"
            for candidate in [resource_dir / f"{guid}.pbiviz"]:
                if candidate.exists():
                    candidate.unlink()

        write_json(self.report_json_path(), report)
        result: dict[str, Any] = {"action": "removed", "guid": guid, "removed_count": len(removed_items)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def load_report_json(self) -> dict[str, Any]:
        path = self.report_json_path()
        if not path.exists():
            raise PBIRError(f"Required PBIR report metadata file is missing: {path}")
        data = read_json(path)
        if not isinstance(data, dict):
            raise PBIRError(f"report.json must contain an object: {path}")
        return data

    def theme_version(self, report: dict[str, Any]) -> dict[str, str]:
        collection = report.get("themeCollection") or {}
        for item in (collection.get("baseTheme"), collection.get("customTheme")):
            if isinstance(item, dict) and isinstance(item.get("reportVersionAtImport"), dict):
                version = item["reportVersionAtImport"]
                if all(key in version for key in ("visual", "page", "report")):
                    return version
        return dict(DEFAULT_THEME_VERSION)

    def manage_theme(self, action: str, validate_after: bool = False, **kwargs: Any) -> dict[str, Any]:
        if action == "list":
            report = self.load_report_json()
            return {
                "themeCollection": report.get("themeCollection", {}),
                "resourcePackages": report.get("resourcePackages", []),
                "registeredResources": self.list_registered_resources(),
            }
        if action == "set_base":
            return self.set_base_theme(kwargs["name"], kwargs.get("type", "SharedResources"), validate_after)
        if action == "set_custom":
            return self.set_custom_theme(kwargs["name"], kwargs.get("theme"), kwargs.get("file_name"), validate_after)
        if action == "remove_custom":
            report = self.load_report_json()
            report.setdefault("themeCollection", {}).pop("customTheme", None)
            write_json(self.report_json_path(), report)
            result: dict[str, Any] = {"themeCollection": report.get("themeCollection", {})}
            if validate_after:
                result["validation"] = validation_summary(self.validate().as_dict())
            return result
        raise PBIRError("Unknown theme action. Use list, set_base, set_custom, or remove_custom.")

    def list_registered_resources(self) -> list[dict[str, Any]]:
        root = self.report_root / "StaticResources" / "RegisteredResources"
        if not root.exists():
            return []
        resources = []
        for file in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if file.is_file():
                resources.append({"name": file.name, "path": str(file), "size": file.stat().st_size})
        return resources

    def set_base_theme(self, name: str, theme_type: str = "SharedResources", validate_after: bool = False) -> dict[str, Any]:
        report = self.load_report_json()
        collection = report.setdefault("themeCollection", {})
        collection["baseTheme"] = {
            "name": name,
            "reportVersionAtImport": self.theme_version(report),
            "type": theme_type,
        }
        write_json(self.report_json_path(), report)
        result: dict[str, Any] = {"themeCollection": collection}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    def set_custom_theme(
        self,
        name: str,
        theme: dict[str, Any] | None,
        file_name: str | None = None,
        validate_after: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(theme, dict):
            raise PBIRError("set_custom requires a theme JSON object.")
        resource_dir = self.report_root / "StaticResources" / "RegisteredResources"
        resource_dir.mkdir(parents=True, exist_ok=True)
        file_name = file_name or f"{normalize_object_name(name, 'theme name')}.json"
        if not file_name.lower().endswith(".json"):
            file_name += ".json"
        normalize_object_name(Path(file_name).stem, "theme file name")
        theme_path = resource_dir / file_name
        write_json(theme_path, theme)

        report = self.load_report_json()
        collection = report.setdefault("themeCollection", {})
        collection["customTheme"] = {
            "name": Path(file_name).stem,
            "reportVersionAtImport": self.theme_version(report),
            "type": "RegisteredResources",
        }
        self.upsert_registered_theme_package(report, Path(file_name).stem, file_name)
        write_json(self.report_json_path(), report)
        result: dict[str, Any] = {"theme_path": str(theme_path), "themeCollection": collection}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    @staticmethod
    def upsert_registered_theme_package(report: dict[str, Any], item_name: str, file_name: str) -> None:
        # PBI Desktop does NOT write a RegisteredResources resourcePackages entry for custom themes.
        # It resolves them by name from StaticResources/RegisteredResources/ directly.
        # Remove stale RegisteredResources package entries that contain only theme JSON files,
        # but preserve packages that register custom visuals (.pbiviz files).
        packages = report.get("resourcePackages")
        if packages:
            def _is_theme_only_package(pkg: dict) -> bool:
                if pkg.get("type") != "RegisteredResources":
                    return False
                items = pkg.get("items") or []
                # Preserve if any item is a .pbiviz (custom visual)
                return not any(str(item.get("path", "")).lower().endswith(".pbiviz") for item in items)
            report["resourcePackages"] = [p for p in packages if not _is_theme_only_package(p)]

    def generate_theme(
        self,
        primary: str,
        accent: str | None = None,
        background: str = "#FFFFFF",
        text: str | None = None,
        theme_name: str = "Custom",
        page_background_transparency: int = 0,
        good: str = "#1AAB40",
        neutral_color: str = "#D9B300",
        bad: str = "#D64550",
        validate_after: bool = False,
    ) -> dict[str, Any]:
        if not accent:
            accent = _rotate_hue(primary, 150)
        if not text:
            text = "#E8E6E3" if _is_dark(background) else "#252423"
        theme = build_pbi_theme(
            name=theme_name,
            primary=primary,
            accent=accent,
            background=background,
            text=text,
            good=good,
            neutral_color=neutral_color,
            bad=bad,
            page_bg_transparency=page_background_transparency,
        )
        file_name = normalize_object_name(theme_name, "theme name") + ".json"
        return self.set_custom_theme(theme_name, theme, file_name, validate_after)

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        page_count = 0
        visual_count = 0

        required = [
            self.report_root / "definition.pbir",
            self.definition_dir,
            self.rel("report.json"),
            self.rel("version.json"),
            self.pages_dir,
        ]
        for path in required:
            if not path.exists():
                errors.append(f"Missing required PBIR path: {path}")

        for json_path in self.report_root.rglob("*.json"):
            try:
                read_json(json_path)
            except PBIRError as exc:
                errors.append(str(exc))

        metadata: dict[str, Any] = {}
        if self.pages_json_path().exists():
            try:
                metadata = self.load_pages_metadata()
                self.validate_pages_metadata(metadata, errors, warnings)
            except PBIRError as exc:
                errors.append(str(exc))
        page_names: set[str] = set()
        for page_dir in self.page_dirs():
            if not NAME_RE.match(page_dir.name):
                errors.append(f"Invalid page folder name: {page_dir}")
            page_path = page_dir / "page.json"
            if not page_path.exists():
                errors.append(f"Missing page.json: {page_path}")
                continue
            try:
                page = read_json(page_path)
            except PBIRError as exc:
                errors.append(str(exc))
                continue
            page_count += 1
            extra = unexpected_keys(page, PAGE_TOP_LEVEL_KEYS)
            if extra:
                errors.append(f"Page {page_path} has unsupported top-level keys: {', '.join(extra)}")
            for field in ("$schema", "name", "displayName", "displayOption"):
                if field not in page:
                    errors.append(f"Page {page_path} missing required field: {field}")
            page_name = str(page.get("name", page_dir.name))
            if not NAME_RE.match(page_name):
                errors.append(f"Invalid page name in {page_path}: {page_name}")
            if len(page_name) > 50:
                errors.append(f"Page name in {page_path} is longer than 50 characters.")
            if page_name in page_names:
                errors.append(f"Duplicate page name: {page_name}")
            if page_dir.name != page_name:
                warnings.append(f"Page folder '{page_dir.name}' does not match page name '{page_name}'.")
            page_names.add(page_name)
            display_option = page.get("displayOption")
            if display_option not in PAGE_DISPLAY_OPTIONS:
                errors.append(f"Page {page_path} has invalid displayOption: {display_option}")
            if display_option != "DeprecatedDynamic":
                for dim in ("width", "height"):
                    if not isinstance(page.get(dim), (int, float)) or page.get(dim) <= 0:
                        errors.append(f"Page {page_path} requires positive numeric {dim}.")
            width = page.get("width", DEFAULT_PAGE_SIZE["width"])
            height = page.get("height", DEFAULT_PAGE_SIZE["height"])
            visual_names: set[str] = set()
            visible_positions: list[tuple[str, dict[str, Any]]] = []
            for visual_dir in self.visual_dirs(page_dir):
                if not NAME_RE.match(visual_dir.name):
                    errors.append(f"Invalid visual folder name: {visual_dir}")
                visual_path = visual_dir / "visual.json"
                if not visual_path.exists():
                    errors.append(f"Missing visual.json: {visual_path}")
                    continue
                try:
                    visual = read_json(visual_path)
                except PBIRError as exc:
                    errors.append(str(exc))
                    continue
                visual_count += 1
                self.validate_visual(visual_path, visual, width, height, errors, warnings)
                visual_name = str(visual.get("name", visual_dir.name))
                if visual_name in visual_names:
                    errors.append(f"Duplicate visual name on page {page_name}: {visual_name}")
                visual_names.add(visual_name)
                if not visual.get("isHidden"):
                    pos = visual.get("position") or {}
                    if all(isinstance(pos.get(f), (int, float)) for f in ("x", "y", "width", "height")):
                        visible_positions.append((visual_name, {
                            "x": float(pos["x"]), "y": float(pos["y"]),
                            "width": float(pos["width"]), "height": float(pos["height"]),
                        }))
            for i, (na, pa) in enumerate(visible_positions):
                for nb, pb in visible_positions[i + 1:]:
                    if self._overlap_area(pa, pb) > 0:
                        warnings.append(f"Page '{page_name}': visuals '{na}' and '{nb}' overlap.")

        active = metadata.get("activePageName")
        if active and active not in page_names:
            errors.append(f"pages.json activePageName '{active}' does not match any page.")
        landing = metadata.get("landingPageName")
        if landing and landing not in page_names:
            errors.append(f"pages.json landingPageName '{landing}' does not match any page.")
        for name in metadata.get("pageOrder") or []:
            if name not in page_names:
                warnings.append(f"pages.json pageOrder contains missing page '{name}'.")

        return ValidationResult(
            report_root=str(self.report_root),
            ok=not errors,
            errors=errors,
            warnings=warnings,
            page_count=page_count,
            visual_count=visual_count,
        )

    @staticmethod
    def validate_pages_metadata(metadata: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
        extra = unexpected_keys(metadata, PAGES_METADATA_KEYS)
        if extra:
            errors.append(f"pages.json has unsupported top-level keys: {', '.join(extra)}")
        schema_value = metadata.get("$schema")
        if schema_value != PAGES_METADATA_SCHEMA:
            warnings.append(f"pages.json schema is '{schema_value}', new writes use '{PAGES_METADATA_SCHEMA}'.")
        order = metadata.get("pageOrder", [])
        if order is not None:
            if not isinstance(order, list) or any(not isinstance(item, str) for item in order):
                errors.append("pages.json pageOrder must be an array of page-name strings.")
            elif len(order) != len(set(order)):
                errors.append("pages.json pageOrder contains duplicate page names.")
        for key in ("activePageName", "landingPageName"):
            value = metadata.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(f"pages.json {key} must be a string when present.")

    @staticmethod
    def validate_visual(
        visual_path: Path,
        visual: dict[str, Any],
        page_width: Any,
        page_height: Any,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        extra = unexpected_keys(visual, VISUAL_CONTAINER_KEYS)
        if extra:
            errors.append(f"Visual {visual_path} has unsupported top-level keys: {', '.join(extra)}")
        for field in ("$schema", "name", "position"):
            if field not in visual:
                errors.append(f"Visual {visual_path} missing required field: {field}")
        if ("visual" in visual) == ("visualGroup" in visual):
            errors.append(f"Visual {visual_path} must include exactly one of visual or visualGroup.")
        if "visual" in visual and "visualType" not in (visual.get("visual") or {}):
            errors.append(f"Visual {visual_path} missing visual.visualType.")
        if "visualGroup" in visual:
            group = visual.get("visualGroup") or {}
            for field in ("displayName", "groupMode"):
                if field not in group:
                    errors.append(f"Visual group {visual_path} missing visualGroup.{field}.")
            if group.get("groupMode") not in VISUAL_GROUP_MODES:
                errors.append(f"Visual group {visual_path} has invalid groupMode: {group.get('groupMode')}")
        name = str(visual.get("name", ""))
        if name and not NAME_RE.match(name):
            errors.append(f"Invalid visual name in {visual_path}: {name}")
        if len(name) > 50:
            errors.append(f"Visual name in {visual_path} is longer than 50 characters.")
        if visual_path.parent.name != name:
            warnings.append(f"Visual folder '{visual_path.parent.name}' does not match visual name '{name}'.")
        pos = visual.get("position") or {}
        if not isinstance(pos, dict):
            errors.append(f"Visual {visual_path} position must be an object.")
            return
        extra_pos = unexpected_keys(pos, POSITION_KEYS)
        if extra_pos:
            errors.append(f"Visual {visual_path} position has unsupported keys: {', '.join(extra_pos)}")
        for field in ("x", "y", "width", "height"):
            if not isinstance(pos.get(field), (int, float)):
                errors.append(f"Visual {visual_path} position.{field} must be numeric.")
        if all(isinstance(pos.get(field), (int, float)) for field in ("x", "y", "width", "height")):
            if pos["width"] <= 0 or pos["height"] <= 0:
                errors.append(f"Visual {visual_path} width and height must be positive.")
            if pos["x"] < 0 or pos["y"] < 0:
                errors.append(f"Visual {visual_path} x and y must be non-negative.")
            if isinstance(page_width, (int, float)) and pos["x"] + pos["width"] > page_width:
                warnings.append(f"Visual {visual_path} extends beyond page width.")
            if isinstance(page_height, (int, float)) and pos["y"] + pos["height"] > page_height:
                warnings.append(f"Visual {visual_path} extends beyond page height.")
        visual_inner = visual.get("visual")
        if isinstance(visual_inner, dict):
            visual_type = visual_inner.get("visualType")
            unknown = unknown_query_roles(visual_type, visual_inner.get("query"))
            if unknown:
                allowed = sorted(set(_VISUAL_ROLE_ALIASES_LC[str(visual_type).lower()].values()))
                warnings.append(
                    f"Visual {visual_path} ({visual_type}) uses data role(s) "
                    f"{', '.join(sorted(unknown))} that this visual type does not render; "
                    f"expected one of {', '.join(allowed)}. The visual may appear empty."
                )


class PBIRSemanticModel:
    """Read/write RLS roles in a PBIP SemanticModel (TMDL format)."""

    def __init__(self, sm_root: Path) -> None:
        self.sm_root = sm_root
        self.roles_dir = sm_root / "definition" / "roles"
        self.model_tmdl = sm_root / "definition" / "model.tmdl"

    @classmethod
    def from_report(cls, report_root: Path) -> "PBIRSemanticModel | None":
        """Locate the referenced or sibling .SemanticModel folder from a .Report folder."""
        try:
            referenced = PBIRReport(report_root).semantic_model_path()
            if referenced and (referenced / "definition" / "model.tmdl").exists():
                return cls(referenced)
        except PBIRError:
            pass
        sm_path = Path(str(report_root).replace(".Report", ".SemanticModel"))
        if sm_path.exists() and (sm_path / "definition" / "model.tmdl").exists():
            return cls(sm_path)
        return None

    # ── TMDL helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _tmdl_name(name: str) -> str:
        """Wrap name in single quotes if it contains spaces."""
        return f"'{name}'" if " " in name else name

    def _parse_role_file(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        name = model_perm = pbi_id = ""
        table_permissions: list[dict[str, str]] = []
        column_permissions: list[dict[str, str]] = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if s.startswith("role "):
                name = s[5:].strip().strip("'")
            elif s.startswith("modelPermission:"):
                model_perm = s.split(":", 1)[1].strip()
            elif s.startswith("tablePermission "):
                rest = s[len("tablePermission "):]
                if "' = " in rest:
                    tbl, dax = rest.split("' = ", 1)
                    table_permissions.append({"table": tbl.strip().strip("'"), "dax": dax.strip()})
                elif '" = ' in rest:
                    tbl, dax = rest.split('" = ', 1)
                    table_permissions.append({"table": tbl.strip().strip('"'), "dax": dax.strip()})
                else:
                    # OLS table permission — check next line for metadataPermission
                    tbl = rest.strip().strip("'\"")
                    perm_val = "None"
                    if i + 1 < len(lines) and "metadataPermission:" in lines[i + 1]:
                        perm_val = lines[i + 1].strip().split(":", 1)[1].strip()
                        i += 1
                    table_permissions.append({"table": tbl, "permission": perm_val})
            elif s.startswith("columnPermission "):
                rest = s[len("columnPermission "):]
                perm_val = "None"
                if i + 1 < len(lines) and "metadataPermission:" in lines[i + 1]:
                    perm_val = lines[i + 1].strip().split(":", 1)[1].strip()
                    i += 1
                # rest: 'Table'[Column] or Table[Column]
                import re as _re
                m = _re.match(r"['\"]?(.+?)['\"]?\[(.+?)\]", rest)
                if m:
                    column_permissions.append({"table": m.group(1), "column": m.group(2), "permission": perm_val})
            elif s.startswith("annotation PBI_Id"):
                pbi_id = s.split("=", 1)[1].strip()
            i += 1
        return {"name": name, "modelPermission": model_perm or "read",
                "tablePermissions": table_permissions, "columnPermissions": column_permissions,
                "_pbi_id": pbi_id}

    def _write_role_file(
        self,
        name: str,
        table_permissions: list[dict[str, str]],
        model_permission: str = "read",
        pbi_id: str | None = None,
        column_permissions: list[dict[str, str]] | None = None,
    ) -> Path:
        self.roles_dir.mkdir(parents=True, exist_ok=True)
        if not pbi_id:
            pbi_id = uuid.uuid4().hex
        lines = [
            f"role {self._tmdl_name(name)}",
            f"\tmodelPermission: {model_permission}",
            "",
        ]
        for tp in table_permissions:
            if tp.get("dax"):
                lines.append(f"\ttablePermission {self._tmdl_name(tp['table'])} = {tp['dax']}")
            else:
                perm = tp.get("permission", "None")
                lines.append(f"\ttablePermission {self._tmdl_name(tp['table'])}")
                lines.append(f"\t\tmetadataPermission: {perm}")
                lines.append("")
        for cp in (column_permissions or []):
            perm = cp.get("permission", "None")
            lines.append(f"\tcolumnPermission {self._tmdl_name(cp['table'])}[{cp['column']}]")
            lines.append(f"\t\tmetadataPermission: {perm}")
            lines.append("")
        lines += [f"\tannotation PBI_Id = {pbi_id}", ""]
        path = self.roles_dir / f"{name}.tmdl"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _ref_line(self, name: str) -> str:
        return f"ref role {self._tmdl_name(name)}"

    def _update_model_ref(self, name: str, *, add: bool) -> None:
        text = self.model_tmdl.read_text(encoding="utf-8")
        ref = self._ref_line(name)
        if add:
            if ref not in text:
                text = text.rstrip("\n") + f"\n\n{ref}\n"
                self.model_tmdl.write_text(text, encoding="utf-8")
        else:
            lines = [ln for ln in text.splitlines(keepends=True) if ln.strip() != ref]
            self.model_tmdl.write_text("".join(lines), encoding="utf-8")

    # ── Public API ────────────────────────────────────────────────────────────

    def list_roles(self) -> list[dict[str, Any]]:
        if not self.roles_dir.exists():
            return []
        return [self._parse_role_file(f) for f in sorted(self.roles_dir.glob("*.tmdl"))]

    def manage_role(
        self,
        action: str,
        name: str,
        table_permissions: list[dict[str, str]] | None = None,
        model_permission: str = "read",
        column_permissions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """action: create | update | delete"""
        if action == "delete":
            path = self.roles_dir / f"{name}.tmdl"
            if not path.exists():
                raise PBIRError(f"Role '{name}' not found.")
            path.unlink()
            self._update_model_ref(name, add=False)
            return {"action": "deleted", "name": name}

        if action in ("create", "update"):
            if not table_permissions and not column_permissions:
                raise PBIRError("table_permissions or column_permissions is required for create/update.")
            # Preserve existing PBI_Id on update; reject create if already exists
            pbi_id: str | None = None
            existing = self.roles_dir / f"{name}.tmdl"
            if action == "create" and existing.exists():
                raise PBIRError(f"Role '{name}' already exists. Use action='update' to modify it.")
            if action == "update" and existing.exists():
                pbi_id = self._parse_role_file(existing).get("_pbi_id")
            path = self._write_role_file(
                name, table_permissions or [], model_permission,
                pbi_id=pbi_id, column_permissions=column_permissions,
            )
            self._update_model_ref(name, add=True)
            return {
                "action": action + "d",
                "name": name,
                "modelPermission": model_permission,
                "tablePermissions": table_permissions or [],
                "columnPermissions": column_permissions or [],
                "path": str(path),
            }

        raise PBIRError(f"Unknown role action '{action}'. Use create, update, or delete.")

    def bulk_create_rls_roles(
        self,
        table: str,
        column: str,
        values: list[str],
        role_prefix: str = "",
        additional_tables: list[dict[str, str]] | None = None,
        on_conflict: str = "skip",
    ) -> dict[str, Any]:
        """
        Create one RLS role per value in `values`.
        Each role gets: tablePermission <table> = [<column>] = "<value>"
        plus any extra table filters from additional_tables.
        on_conflict: "skip" leaves existing roles untouched; "overwrite" rewrites them.
        """
        created: list[str] = []
        skipped: list[str] = []
        overwritten: list[str] = []
        for value in values:
            role_name = f"{role_prefix}{value}" if role_prefix else value
            safe_name = re.sub(r'[\\/:*?"<>|]', '_', role_name)
            escaped_value = value.replace('"', '""')
            perms = [{"table": table, "dax": f'[{column}] = "{escaped_value}"'}]
            if additional_tables:
                perms.extend(additional_tables)
            existing = self.roles_dir / f"{safe_name}.tmdl" if self.roles_dir.exists() else None
            if existing and existing.exists():
                if on_conflict == "skip":
                    skipped.append(role_name)
                    continue
                self._write_role_file(safe_name, perms, pbi_id=self._parse_role_file(existing).get("_pbi_id"))
                self._update_model_ref(safe_name, add=True)
                overwritten.append(role_name)
            else:
                self._write_role_file(safe_name, perms)
                self._update_model_ref(safe_name, add=True)
                created.append(role_name)
        return {
            "created": created,
            "overwritten": overwritten,
            "skipped": skipped,
            "total_processed": len(created) + len(overwritten),
        }

    def get_table_source(self, table: str) -> dict[str, Any]:
        """Parse a table TMDL and return its data source connection info."""
        import re
        # Table files may be named with or without quotes
        candidates = [
            self.sm_root / "definition" / "tables" / f"{table}.tmdl",
            self.sm_root / "definition" / "tables" / f"'{table}'.tmdl",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            available = [p.stem.strip("'") for p in (self.sm_root / "definition" / "tables").glob("*.tmdl")]
            raise PBIRError(f"Table '{table}' not found. Available: {', '.join(available)}")

        text = path.read_text(encoding="utf-8")

        # Partition type
        pt_match = re.search(r"partition .+ = (m|dax)", text)
        partition_type = pt_match.group(1) if pt_match else "unknown"

        result: dict[str, Any] = {"table": table, "partition_type": partition_type}

        if partition_type == "m":
            # SQL Server connection: Sql.Database("server", "database", ...)
            conn_match = re.search(r'Sql\.Database\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"', text)
            if conn_match:
                result["server"] = conn_match.group(1)
                result["database"] = conn_match.group(2)

            # Inline SQL query inside [Query = "..."]
            query_match = re.search(r'\[Query\s*=\s*"([\s\S]*?)"\s*\]', text)
            if query_match:
                sql = query_match.group(1).replace('""', '"')
                result["sql_query"] = sql.strip()
                # Extract primary FROM source (first FROM clause) for efficient direct queries
                from_match = re.search(r'\bFROM\s+([\w\.\[\]]+)', sql, re.IGNORECASE)
                if from_match:
                    result["source_table"] = from_match.group(1)

        elif partition_type == "dax":
            dax_match = re.search(r'source\s*=\s*([\s\S]+?)(?:\n\t\w|\Z)', text)
            if dax_match:
                result["dax_expression"] = dax_match.group(1).strip()

        # List columns defined in the TMDL
        columns = re.findall(r'^\s+column (\S+)', text, re.MULTILINE)
        result["columns"] = [c.strip("'") for c in columns]

        return result

    def get_distinct_values(
        self,
        table: str,
        column: str,
        max_rows: int = 1000,
    ) -> dict[str, Any]:
        """Query the source database directly for distinct non-null values of a column."""
        try:
            import pyodbc
        except ImportError:
            raise PBIRError("pyodbc is not installed. Run: pip install pyodbc")
        source = self.get_table_source(table)
        server = source.get("server")
        database = source.get("database")
        source_table = source.get("source_table")
        if not server or not database:
            raise PBIRError(
                f"Could not determine data source for table '{table}'. "
                "Use run_source_query with a custom SQL query instead."
            )
        if not source_table:
            raise PBIRError(
                f"Could not determine source table for '{table}'. "
                "Use run_source_query with a custom SQL query instead."
            )
        sql = f"SELECT DISTINCT [{column}] FROM {source_table} WHERE [{column}] IS NOT NULL ORDER BY [{column}]"
        conn_str = f"DRIVER={{SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
        try:
            conn = pyodbc.connect(conn_str, timeout=15)
        except Exception as e:
            raise PBIRError(f"Could not connect to {server}/{database}: {e}")
        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchmany(max_rows)
        except Exception as e:
            raise PBIRError(f"Query failed: {e}")
        finally:
            conn.close()
        values = [row[0] for row in rows]
        return {
            "table": table,
            "column": column,
            "values": values,
            "row_count": len(values),
            "truncated": len(values) == max_rows,
            "source_table": source_table,
            "sql_used": sql,
        }

    def bulk_delete_rls_roles(
        self,
        names: list[str] | None = None,
        prefix: str | None = None,
    ) -> dict[str, Any]:
        """Delete roles by explicit list or by name prefix. One of names or prefix is required."""
        if not names and not prefix:
            raise PBIRError("Provide either names (list) or prefix.")
        existing = {r["name"] for r in self.list_roles()}
        if names:
            targets = [n for n in names if n in existing]
            not_found = [n for n in names if n not in existing]
        else:
            targets = [n for n in sorted(existing) if n.startswith(prefix)]  # type: ignore[arg-type]
            not_found = []
        deleted = []
        for name in targets:
            self.manage_role("delete", name)
            deleted.append(name)
        return {"deleted": deleted, "not_found": not_found, "total_deleted": len(deleted)}

    def list_tables(self, include_system: bool = False) -> list[dict[str, Any]]:
        """List SemanticModel tables with column and measure counts."""
        tables_dir = self.sm_root / "definition" / "tables"
        if not tables_dir.exists():
            return []
        result = []
        for path in sorted(tables_dir.glob("*.tmdl")):
            name = path.stem.strip("'")
            if not include_system and any(
                name.startswith(p) for p in ("DateTableTemplate_", "LocalDateTable_")
            ):
                continue
            text = path.read_text(encoding="utf-8")
            col_count = len(re.findall(r"^\s+column ", text, re.MULTILINE))
            measure_count = len(re.findall(r"^\s+measure ", text, re.MULTILINE))
            mode_match = re.search(r'\bmode:\s+(\w+)', text)
            result.append({
                "name": name,
                "columns": col_count,
                "measures": measure_count,
                "storage_mode": mode_match.group(1) if mode_match else "import",
            })
        return result

    def set_storage_mode(
        self,
        tables: list[str] | None,
        mode: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Change partition storage mode (import / directQuery / dual) for one or more tables."""
        valid_modes = {"directQuery", "import", "dual"}
        if mode not in valid_modes:
            raise PBIRError(f"Invalid mode '{mode}'. Valid: {', '.join(sorted(valid_modes))}")

        tables_dir = self.sm_root / "definition" / "tables"
        if not tables_dir.exists():
            raise PBIRError("No tables directory found in SemanticModel.")

        system_prefixes = ("DateTableTemplate_", "LocalDateTable_")
        all_files = sorted(tables_dir.glob("*.tmdl"))

        if not tables or tables == ["*"]:
            target_files = [
                f for f in all_files
                if not any(f.stem.strip("'").startswith(p) for p in system_prefixes)
            ]
        else:
            name_to_file: dict[str, Path] = {}
            for f in all_files:
                stem = f.stem.strip("'")
                name_to_file[stem] = f
                name_to_file[f.stem] = f
            not_found = [t for t in tables if t not in name_to_file]
            if not_found:
                available = sorted({f.stem.strip("'") for f in all_files})
                raise PBIRError(
                    f"Tables not found: {', '.join(not_found)}. "
                    f"Available: {', '.join(available)}"
                )
            target_files = [name_to_file[t] for t in tables]

        changed: list[dict] = []
        skipped: list[dict] = []
        warnings: list[str] = []
        mode_re = re.compile(r'\bmode:\s+(\w+)')

        for tmdl_file in target_files:
            table_name = tmdl_file.stem.strip("'")
            text = tmdl_file.read_text(encoding="utf-8-sig")

            # Skip calculated / DAX partitions — they have no physical storage mode
            pt_match = re.search(r'\bpartition\s+.+?\s*=\s*(\w+)', text)
            pt = pt_match.group(1).lower() if pt_match else "unknown"
            if pt in ("calculated", "dax"):
                skipped.append({
                    "table": table_name,
                    "reason": "calculated partition — not eligible for storage mode change",
                })
                continue

            # Warn about complex M queries that may not fold under DirectQuery
            if mode == "directQuery" and "[Query" in text and pt == "m":
                warnings.append(
                    f"{table_name}: uses an inline SQL [Query] in the M expression — "
                    "query folding is not guaranteed for complex SQL. "
                    "Consider pointing the partition at a database view for reliable DirectQuery."
                )

            mode_match = mode_re.search(text)
            if mode_match:
                old_mode = mode_match.group(1)
                if old_mode == mode:
                    skipped.append({"table": table_name, "reason": f"already {mode}"})
                    continue
                new_text = text.replace(f"mode: {old_mode}", f"mode: {mode}", 1)
            else:
                # No explicit mode line (import is default) — insert after the partition type line
                indent_match = re.search(r'^(\t+)partition\s', text, re.MULTILINE)
                content_indent = (indent_match.group(1) if indent_match else "\t") + "\t"
                new_text = re.sub(
                    r'(\t*partition\s+[^\n]+\n)',
                    lambda m: m.group(0) + content_indent + f"mode: {mode}\n",
                    text, count=1,
                )
                old_mode = "import (default)"

            changed.append({"table": table_name, "old_mode": old_mode, "new_mode": mode})
            if not dry_run:
                tmdl_file.write_text(new_text, encoding="utf-8")

        return {
            "mode": mode,
            "changed": changed,
            "skipped": skipped,
            "warnings": warnings,
            "dry_run": dry_run,
            "total_changed": len(changed),
        }

    def get_relationships(self, include_date_tables: bool = False) -> list[dict[str, Any]]:
        """Parse relationships.tmdl and return the join graph."""
        import re
        path = self.sm_root / "definition" / "relationships.tmdl"
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        results = []
        # Split on 'relationship' keyword at line start
        for block in re.split(r"(?m)^relationship ", text):
            block = block.strip()
            if not block:
                continue
            from_m = re.search(r"fromColumn:\s+(.+?)\.(.+)", block)
            to_m = re.search(r"toColumn:\s+(.+?)\.(.+)", block)
            if not from_m or not to_m:
                continue
            from_table = from_m.group(1).strip().strip("'")
            from_col = from_m.group(2).strip().strip("'")
            to_table = to_m.group(1).strip().strip("'")
            to_col = to_m.group(2).strip().strip("'")
            if not include_date_tables and any(
                p in t for p in ("LocalDateTable_", "DateTableTemplate_") for t in (from_table, to_table)
            ):
                continue
            rel_name = block.split("\n")[0].strip()
            entry: dict[str, Any] = {
                "from_table": from_table,
                "from_column": from_col,
                "to_table": to_table,
                "to_column": to_col,
            }
            if rel_name and not rel_name.startswith("fromColumn"):
                entry["name"] = rel_name
            results.append(entry)
        return results

    # ── TMDL write helpers ────────────────────────────────────────────────────

    def _find_table_file(self, table: str) -> Path:
        candidates = [
            self.sm_root / "definition" / "tables" / f"{table}.tmdl",
            self.sm_root / "definition" / "tables" / f"'{table}'.tmdl",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            td = self.sm_root / "definition" / "tables"
            available = [p.stem.strip("'") for p in td.glob("*.tmdl")] if td.exists() else []
            raise PBIRError(f"Table '{table}' not found. Available: {', '.join(available)}")
        return path

    @staticmethod
    def _measure_block(name: str, dax: str, format_string: str | None = None,
                       display_folder: str | None = None, description: str | None = None) -> str:
        """Build a single TMDL measure block (tab-indented, no trailing newline)."""
        tname = f"'{name}'" if " " in name else name
        if "\n" in dax:
            first_line = f"\tmeasure {tname} = ```"
            dax_lines = "\n".join(f"\t\t{ln}" for ln in dax.splitlines())
            body = f"{first_line}\n{dax_lines}\n\t\t```"
        else:
            body = f"\tmeasure {tname} = {dax}"
        if format_string:
            body += f'\n\t\tformatString: "{format_string}"'
        if display_folder:
            body += f'\n\t\tdisplayFolder: "{display_folder}"'
        if description:
            body += f'\n\t\tdescription: "{description}"'
        return body

    def manage_measure(
        self,
        action: str,
        table: str,
        name: str,
        dax: str | None = None,
        format_string: str | None = None,
        display_folder: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """action: create | update | delete | list"""
        if action == "list":
            path = self._find_table_file(table)
            text = path.read_text(encoding="utf-8")
            measures = []
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("measure "):
                    rest = s[8:].strip()
                    if "=" in rest:
                        mname, _, mexpr = rest.partition("=")
                        measures.append({"name": self._tmdl_unquote(mname.strip()), "dax": mexpr.strip()})
                    else:
                        measures.append({"name": self._tmdl_unquote(rest), "dax": None})
            return {"table": table, "measures": measures}

        if action in ("create", "update"):
            if not dax:
                raise PBIRError(f"dax is required for action='{action}'.")
            block = self._measure_block(name, dax, format_string, display_folder, description)
            path = self._find_table_file(table)
            text = path.read_text(encoding="utf-8")
            tname = f"'{name}'" if " " in name else name
            # Check if measure already exists
            pattern = re.compile(r'(^|\n)\t' + r'measure\s+' + re.escape(tname) + r'\s*[=\n]', re.MULTILINE)
            if action == "create" and pattern.search(text):
                raise PBIRError(f"Measure '{name}' already exists in table '{table}'. Use action='update' to replace.")
            if action == "update":
                if not pattern.search(text):
                    raise PBIRError(f"Measure '{name}' not found in table '{table}'. Use action='create' to add it.")
                # Find start/end of the measure block (tab-level 1)
                lines = text.splitlines(keepends=True)
                start = end = None
                in_block = False
                for i, raw in enumerate(lines):
                    stripped = raw.strip()
                    if not in_block and stripped.startswith(f"measure {tname}"):
                        start = i
                        in_block = True
                        continue
                    if in_block:
                        if raw.startswith("\t\t") or not raw.strip():
                            end = i
                        else:
                            break
                if start is not None:
                    end_idx = (end + 1) if end is not None else (start + 1)
                    new_lines = lines[:start] + [block + "\n"] + lines[end_idx:]
                    path.write_text("".join(new_lines), encoding="utf-8")
                return {"action": "updated", "table": table, "name": name, "path": str(path)}
            # create: append before annotation or at end
            ann_idx = text.find("\n\tannotation ")
            if ann_idx != -1:
                new_text = text[:ann_idx] + "\n" + block + text[ann_idx:]
            else:
                new_text = text.rstrip("\n") + "\n\n" + block + "\n"
            path.write_text(new_text, encoding="utf-8")
            return {"action": "created", "table": table, "name": name, "path": str(path)}

        if action == "delete":
            path = self._find_table_file(table)
            text = path.read_text(encoding="utf-8")
            tname = f"'{name}'" if " " in name else name
            lines = text.splitlines(keepends=True)
            start = end = None
            for i, raw in enumerate(lines):
                stripped = raw.strip()
                if start is None and stripped.startswith(f"measure {tname}"):
                    start = i
                    continue
                if start is not None:
                    if raw.startswith("\t\t") or not raw.strip():
                        end = i
                    else:
                        break
            if start is None:
                raise PBIRError(f"Measure '{name}' not found in table '{table}'.")
            end_idx = (end + 1) if end is not None else (start + 1)
            # also remove blank line before
            if start > 0 and not lines[start - 1].strip():
                start -= 1
            new_lines = lines[:start] + lines[end_idx:]
            path.write_text("".join(new_lines), encoding="utf-8")
            return {"action": "deleted", "table": table, "name": name, "path": str(path)}

        raise PBIRError(f"Unknown action '{action}'. Use create, update, delete, or list.")

    MEASURE_TEMPLATES: dict[str, dict[str, str]] = {
        "total": {"dax": "SUM('{source_table}'[{column}])", "format_string": "#,##0"},
        "average": {"dax": "AVERAGE('{source_table}'[{column}])", "format_string": "#,##0.00"},
        "distinct_count": {"dax": "DISTINCTCOUNT('{source_table}'[{column}])", "format_string": "#,##0"},
        "count_rows": {"dax": "COUNTROWS('{source_table}')", "format_string": "#,##0"},
        "yoy_growth": {
            "dax": "{base_measure} - CALCULATE({base_measure}, SAMEPERIODLASTYEAR('{date_table}'[{date_column}]))",
            "format_string": "#,##0",
        },
        "yoy_pct": {
            "dax": "DIVIDE({base_measure} - CALCULATE({base_measure}, SAMEPERIODLASTYEAR('{date_table}'[{date_column}])), CALCULATE({base_measure}, SAMEPERIODLASTYEAR('{date_table}'[{date_column}])))",
            "format_string": "0.00%",
        },
        "rolling_12_months": {
            "dax": "CALCULATE({base_measure}, DATESINPERIOD('{date_table}'[{date_column}], MAX('{date_table}'[{date_column}]), -12, MONTH))",
            "format_string": "#,##0",
        },
    }

    def add_measure_from_template(
        self,
        template: str,
        table: str,
        name: str,
        params: dict[str, Any] | None = None,
        action: str = "create",
    ) -> dict[str, Any]:
        if template not in self.MEASURE_TEMPLATES:
            raise PBIRError(f"Unknown measure template '{template}'. Available: {', '.join(sorted(self.MEASURE_TEMPLATES))}")
        p = dict(params or {})
        p.setdefault("source_table", table)
        try:
            dax = self.MEASURE_TEMPLATES[template]["dax"].format(**p)
        except KeyError as exc:
            raise PBIRError(f"Template '{template}' requires parameter: {exc.args[0]}") from exc
        return self.manage_measure(
            action=action,
            table=table,
            name=name,
            dax=dax,
            format_string=p.get("format_string", self.MEASURE_TEMPLATES[template].get("format_string")),
            display_folder=p.get("display_folder"),
            description=p.get("description", f"Generated from template '{template}'."),
        )

    # ── Perspectives ──────────────────────────────────────────────────────────

    @property
    def perspectives_dir(self) -> Path:
        return self.sm_root / "definition" / "perspectives"

    def list_perspectives(self) -> list[dict[str, Any]]:
        if not self.perspectives_dir.exists():
            return []
        result = []
        for path in sorted(self.perspectives_dir.glob("*.tmdl")):
            result.append(self._parse_perspective_file(path))
        return result

    def _parse_perspective_file(self, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        name = path.stem.strip("'")
        tables: list[dict[str, Any]] = []
        cur_table: dict[str, Any] | None = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("perspective "):
                name = self._tmdl_unquote(s[12:].strip())
            elif s.startswith("perspectiveTable "):
                cur_table = {"table": self._tmdl_unquote(s[17:].strip()), "measures": [], "columns": []}
                tables.append(cur_table)
            elif cur_table and s.startswith("perspectiveMeasure "):
                cur_table["measures"].append(self._tmdl_unquote(s[19:].strip()))
            elif cur_table and s.startswith("perspectiveColumn "):
                cur_table["columns"].append(self._tmdl_unquote(s[18:].strip()))
        return {"name": name, "tables": tables}

    def manage_perspective(
        self,
        action: str,
        name: str,
        tables: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """action: create | update | delete"""
        safe_name = name.replace("/", "_").replace("\\", "_")
        path = self.perspectives_dir / f"{safe_name}.tmdl"
        if action == "delete":
            if not path.exists():
                raise PBIRError(f"Perspective '{name}' not found.")
            path.unlink()
            return {"action": "deleted", "name": name}
        if action in ("create", "update"):
            if action == "create" and path.exists():
                raise PBIRError(f"Perspective '{name}' already exists. Use action='update' to replace.")
            if action == "update" and not path.exists():
                raise PBIRError(f"Perspective '{name}' not found. Use action='create' to add it.")
            self.perspectives_dir.mkdir(parents=True, exist_ok=True)
            pname = self._tmdl_name(name)
            lines = [f"perspective {pname}", ""]
            for tbl in (tables or []):
                tname = self._tmdl_name(tbl["table"])
                lines.append(f"\tperspectiveTable {tname}")
                for m in tbl.get("measures") or []:
                    lines.append(f"\t\tperspectiveMeasure {self._tmdl_name(m)}")
                for c in tbl.get("columns") or []:
                    lines.append(f"\t\tperspectiveColumn {self._tmdl_name(c)}")
                lines.append("")
            path.write_text("\n".join(lines), encoding="utf-8")
            return {"action": action + "d", "name": name, "path": str(path)}
        raise PBIRError(f"Unknown action '{action}'. Use create, update, or delete.")

    # ── TMDL diff ─────────────────────────────────────────────────────────────

    def tmdl_diff(self, other_path: str) -> dict[str, Any]:
        """Compare this SemanticModel's definition/ folder against another path."""
        import difflib
        self_def = self.sm_root / "definition"
        other_def = Path(other_path)
        if not other_def.exists():
            raise PBIRError(f"other_path does not exist: {other_path}")
        # If other_path is a .SemanticModel or SM root, drill into definition/
        if (other_def / "definition").exists():
            other_def = other_def / "definition"

        def collect(base: Path) -> dict[str, str]:
            out: dict[str, str] = {}
            if not base.exists():
                return out
            for f in base.rglob("*.tmdl"):
                rel = str(f.relative_to(base))
                text = f.read_text(encoding="utf-8-sig")
                # Strip lineageTag lines (noise)
                cleaned = "\n".join(
                    ln for ln in text.splitlines()
                    if not ln.strip().startswith("lineageTag:")
                )
                out[rel] = cleaned
            return out

        self_files = collect(self_def)
        other_files = collect(other_def)
        self_keys = set(self_files)
        other_keys = set(other_files)

        added = sorted(self_keys - other_keys)
        removed = sorted(other_keys - self_keys)
        changed = []
        unchanged = 0
        for key in sorted(self_keys & other_keys):
            if self_files[key] == other_files[key]:
                unchanged += 1
            else:
                diff = difflib.unified_diff(
                    other_files[key].splitlines(),
                    self_files[key].splitlines(),
                    fromfile=f"other/{key}",
                    tofile=f"self/{key}",
                    n=3,
                    lineterm="",
                )
                changed.append({"file": key, "diff": "\n".join(list(diff)[:120])})
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged_count": unchanged,
        }

    # ── Relationships ─────────────────────────────────────────────────────────

    @property
    def relationships_tmdl(self) -> Path:
        return self.sm_root / "definition" / "relationships.tmdl"

    def manage_relationship(
        self,
        action: str,
        from_table: str,
        from_column: str,
        to_table: str,
        to_column: str,
        cardinality: str | None = None,
        cross_filter: str | None = None,
        active: bool = True,
    ) -> dict[str, Any]:
        """action: create | delete"""
        ft = self._tmdl_name(from_table)
        fc = self._tmdl_name(from_column)
        tt = self._tmdl_name(to_table)
        tc = self._tmdl_name(to_column)

        def _rel_key(block: str) -> tuple[str, str, str, str] | None:
            fm = re.search(r"fromColumn:\s+(.+?)\.(.+)", block)
            tm = re.search(r"toColumn:\s+(.+?)\.(.+)", block)
            if not fm or not tm:
                return None
            return (fm.group(1).strip().strip("'"), fm.group(2).strip().strip("'"),
                    tm.group(1).strip().strip("'"), tm.group(2).strip().strip("'"))

        if action == "delete":
            if not self.relationships_tmdl.exists():
                raise PBIRError("No relationships.tmdl found.")
            text = self.relationships_tmdl.read_text(encoding="utf-8")
            blocks = re.split(r"(?m)^relationship\b", text)
            header = blocks[0]
            kept = []
            deleted = False
            for b in blocks[1:]:
                key = _rel_key(b)
                if key and key == (from_table, from_column, to_table, to_column):
                    deleted = True
                else:
                    kept.append(b)
            if not deleted:
                raise PBIRError(f"Relationship {from_table}.{from_column} -> {to_table}.{to_column} not found.")
            new_text = header + "".join("relationship" + b for b in kept)
            self.relationships_tmdl.write_text(new_text, encoding="utf-8")
            return {"action": "deleted", "from": f"{from_table}.{from_column}", "to": f"{to_table}.{to_column}"}

        if action == "create":
            lines = ["relationship"]
            lines.append(f"\tfromColumn: {ft}.{fc}")
            lines.append(f"\ttoColumn: {tt}.{tc}")
            if cardinality:
                lines.append(f"\tcardinality: {cardinality}")
            if cross_filter:
                lines.append(f"\tcrossFilteringBehavior: {cross_filter}")
            if not active:
                lines.append("\tisActive: false")
            block = "\n".join(lines) + "\n"
            if self.relationships_tmdl.exists():
                existing = self.relationships_tmdl.read_text(encoding="utf-8")
                new_text = existing.rstrip("\n") + "\n\n" + block
            else:
                new_text = block
            self.relationships_tmdl.write_text(new_text, encoding="utf-8")
            return {"action": "created", "from": f"{from_table}.{from_column}", "to": f"{to_table}.{to_column}"}

        raise PBIRError(f"Unknown action '{action}'. Use create or delete.")

    # ── Hierarchies ───────────────────────────────────────────────────────────

    def manage_hierarchy(
        self,
        action: str,
        table: str,
        name: str,
        levels: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """action: create | delete | list"""
        path = self._find_table_file(table)
        text = path.read_text(encoding="utf-8")
        hname = self._tmdl_name(name)

        if action == "list":
            hierarchies: list[dict[str, Any]] = []
            cur: dict[str, Any] | None = None
            for line in text.splitlines():
                s = line.strip()
                ind = len(line) - len(line.lstrip("\t"))
                if ind == 1 and s.startswith("hierarchy "):
                    cur = {"name": self._tmdl_unquote(s[10:].strip()), "levels": []}
                    hierarchies.append(cur)
                elif cur and ind == 2 and s.startswith("level "):
                    lvl_name = self._tmdl_unquote(s[6:].strip())
                    cur["levels"].append({"name": lvl_name})
                elif cur and ind == 3 and s.startswith("column:"):
                    cur["levels"][-1]["column"] = s[7:].strip().strip("'")
            return {"table": table, "hierarchies": hierarchies}

        if action == "delete":
            lines = text.splitlines(keepends=True)
            start = end = None
            for i, raw in enumerate(lines):
                s = raw.strip()
                ind = len(raw) - len(raw.lstrip("\t"))
                if start is None and ind == 1 and s.startswith(f"hierarchy {hname}"):
                    start = i
                    continue
                if start is not None:
                    if raw.startswith("\t\t") or not raw.strip():
                        end = i
                    else:
                        break
            if start is None:
                raise PBIRError(f"Hierarchy '{name}' not found in table '{table}'.")
            end_idx = (end + 1) if end is not None else (start + 1)
            if start > 0 and not lines[start - 1].strip():
                start -= 1
            path.write_text("".join(lines[:start] + lines[end_idx:]), encoding="utf-8")
            return {"action": "deleted", "table": table, "name": name}

        if action == "create":
            if not levels:
                raise PBIRError("levels is required for action='create'.")
            block_lines = [f"\thierarchy {hname}", ""]
            for lvl in levels:
                lname = self._tmdl_name(lvl["name"])
                lcol = self._tmdl_name(lvl["column"])
                block_lines.append(f"\t\tlevel {lname}")
                block_lines.append(f"\t\t\tcolumn: {lcol}")
                block_lines.append("")
            block = "\n".join(block_lines)
            ann_idx = text.find("\n\tannotation ")
            if ann_idx != -1:
                new_text = text[:ann_idx] + "\n" + block + text[ann_idx:]
            else:
                new_text = text.rstrip("\n") + "\n\n" + block + "\n"
            path.write_text(new_text, encoding="utf-8")
            return {"action": "created", "table": table, "name": name}

        raise PBIRError(f"Unknown action '{action}'. Use create, delete, or list.")


CUSTOM_TOOLS_DIR = Path(__file__).parent / "custom_tools"


class PBIRMCPServer:
    def __init__(self) -> None:
        self.current_report: PBIRReport | None = None
        self.progress_callback: Callable[[dict[str, Any]], None] | None = None
        self.tools: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], Any]]] = {}
        self.register_tools()
        self._relax_project_path_requirements()
        self._load_custom_tools()

    def report(self, args: dict[str, Any]) -> PBIRReport:
        path = args.get("project_path")
        if path:
            self.current_report = PBIRReport(path)
            return self.current_report
        if self.current_report is None:
            raise PBIRError("No PBIR project is loaded. Call load_project or pass project_path.")
        return self.current_report

    def semantic_model(self, args: dict[str, Any]) -> PBIRSemanticModel:
        report = self.report(args)
        sm_path = report.semantic_model_path()
        sm = PBIRSemanticModel(sm_path) if sm_path and (sm_path / "definition" / "model.tmdl").exists() else None
        if sm is None:
            sm = PBIRSemanticModel.from_report(report.report_root)
        if sm is None:
            raise PBIRError(
                f"No SemanticModel found next to {report.report_root}. "
                "Expected definition.pbir to reference a SemanticModel folder, or a sibling folder with the same base name ending in .SemanticModel."
            )
        return sm

    def add_tool(self, name: str, description: str, input_schema: dict[str, Any], fn: Callable[[dict[str, Any]], Any]) -> None:
        self.tools[name] = (description, input_schema, fn)

    def emit_progress(self, event: dict[str, Any]) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event)
        except Exception:
            pass

    def _relax_project_path_requirements(self) -> None:
        for description, input_schema, fn in self.tools.values():
            required = input_schema.get("required")
            if isinstance(required, list) and "project_path" in required:
                input_schema["required"] = [item for item in required if item != "project_path"]

    def register_tools(self) -> None:
        self.add_tool(
            "create_project",
            (
                "Scaffold a brand-new PBIP project: creates the .pbip entry point, Report folder "
                "(definition.pbir, report.json, version.json, initial page), and a SemanticModel stub "
                "folder (.platform, definition.pbism) ready for powerbi-modeling-mcp to populate. "
                "Call this first, then use powerbi-modeling-mcp to define tables/measures and export "
                "TMDL, then call read_semantic_model + pbir_apply_plan to add visuals."
            ),
            {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Parent directory to create the project in."},
                    "name": {"type": "string", "description": "Project display name (may include spaces, e.g. 'Supplier Dashboard')."},
                    "initial_page_display_name": {"type": "string", "default": "Overview"},
                    "initial_page_name": {"type": "string", "description": "Internal page name (alphanumeric). Defaults to 'Overview'."},
                    "page_width": {"type": "number", "default": 1280},
                    "page_height": {"type": "number", "default": 720},
                },
                "required": ["folder", "name"],
                "additionalProperties": False,
            },
            self.tool_create_project,
        )
        self.add_tool(
            "generate_theme",
            (
                "Generate a full Power BI theme JSON from a simple color palette and apply it to the loaded report. "
                "Derives data colors, text hierarchy, and background tints automatically from primary + accent + "
                "background inputs. For dark backgrounds, also overrides axis/legend text colors. "
                "Use apply_style_preset in pbir_apply_plan for one-click presets (dark, corporate_blue, minimal_white)."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "primary": {"type": "string", "description": "Primary data color (hex, e.g. #0078D4). Drives the data color palette."},
                    "accent": {"type": "string", "description": "Secondary data color (hex). Auto-derived from primary (150° hue shift) if omitted."},
                    "background": {"type": "string", "description": "Page canvas background color (hex, default #FFFFFF)."},
                    "text": {"type": "string", "description": "Main text/label color (hex). Auto-detected from background lightness if omitted."},
                    "theme_name": {"type": "string", "description": "Theme display name (default Custom)."},
                    "page_background_transparency": {"type": "integer", "description": "0=opaque background, 100=transparent (default 0)."},
                    "good": {"type": "string", "description": "Semantic good/positive color (default #1AAB40)."},
                    "neutral": {"type": "string", "description": "Semantic neutral/warning color (default #D9B300)."},
                    "bad": {"type": "string", "description": "Semantic bad/negative color (default #D64550)."},
                    "validate_after": {"type": "boolean"},
                },
                "required": ["project_path", "primary"],
                "additionalProperties": False,
            },
            self.tool_generate_theme,
        )
        self.add_tool(
            "recolor_report",
            (
                "Apply color overrides to many visuals at once without touching a theme file. "
                "Targets: title (title text color), title_background (colored band behind title text), "
                "background (visual container/panel fill), "
                "data_primary (default bar/line/point color), axis_labels, axis_titles, "
                "card_value (big number on card visual), card_label (category label text below the number), "
                "labels (data labels on charts), slicer_items (slicer list item text color), "
                "slicer_header_bg (slicer internal header bar background), "
                "slicer_header_text (slicer internal header bar text color), "
                "slicer_date_bg (date input box background on date range slicers), "
                "slicer_date_text (date input box text color on date range slicers), "
                "slicer_slider (range slider color on date range slicers), "
                "slicer_outline (slicer border/outline color). "
                "Scope with page and/or visual_types to limit which visuals are changed."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "targets": {
                        "type": "object",
                        "description": "Map of target → hex color. Valid keys: title, title_background, background, data_primary, axis_labels, axis_titles, card_value, card_label, labels, slicer_items, slicer_header_bg, slicer_header_text, slicer_date_bg, slicer_date_text, slicer_slider, slicer_outline.",
                        "additionalProperties": {"type": "string"},
                    },
                    "page": {"type": "string", "description": "Limit to this page only."},
                    "visual_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Limit to these visualType values (e.g. clusteredBarChart, card, lineChart).",
                    },
                    "validate_after": {"type": "boolean", "default": True},
                },
                "required": ["project_path", "targets"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).recolor_visuals(
                targets=args["targets"],
                page=args.get("page"),
                visual_types=args.get("visual_types"),
                validate_after=bool(args.get("validate_after", True)),
            ),
        )
        self.add_tool(
            "set_page_background",
            (
                "Set canvas background and wallpaper colors for a report page. "
                "color sets the canvas (the report drawing area). "
                "outspace_color sets the wallpaper — the area outside the canvas edges — "
                "which is what actually shows as the backdrop in PBI Desktop. "
                "Defaults outspace to the same color as the canvas if not specified."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string", "description": "Page name (internal, e.g. 'Overview')."},
                    "color": {"type": "string", "description": "Hex color for the canvas background."},
                    "outspace_color": {
                        "type": "string",
                        "description": "Hex color for the wallpaper/outspace. Defaults to color.",
                    },
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page", "color"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).set_page_background(
                page=args["page"],
                color=args["color"],
                outspace_color=args.get("outspace_color"),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "list_rls_roles",
            (
                "List all Row-Level Security (RLS) roles defined in the SemanticModel. "
                "Returns each role's name, modelPermission, and tablePermissions (table → DAX filter). "
                "Use manage_rls_role to create, update, or delete roles."
            ),
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: {"roles": self.semantic_model(args).list_roles()},
        )
        self.add_tool(
            "manage_rls_role",
            (
                "Create, update, or delete a Row-Level Security (RLS) or Object-Level Security (OLS) role. "
                "action=create: writes a new role TMDL file and registers it in model.tmdl. "
                "action=update: overwrites an existing role (preserves its PBI_Id). "
                "action=delete: removes the role file and its model.tmdl reference. "
                "table_permissions: list of {table, dax} for RLS row filters "
                "(e.g. '[ClientName] = \"Acme\"'). For OLS table-level hide, omit dax and add permission='None'. "
                "column_permissions: OLS column-level hide — {table, column, permission: 'None'|'Read'} — "
                "hides specific columns from this role (metadataPermission in TMDL). "
                "After saving, use 'View as' in PBI Desktop to test the role."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "delete"],
                        "description": "create | update | delete",
                    },
                    "name": {"type": "string", "description": "Role name (e.g. 'Supplier View')."},
                    "table_permissions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "string", "description": "Exact table name."},
                                "dax": {"type": "string", "description": "DAX row filter expression. Omit for OLS table hide."},
                                "permission": {"type": "string", "enum": ["None", "Read"], "description": "OLS table permission when dax is omitted."},
                            },
                            "required": ["table"],
                        },
                        "description": "RLS row filters or OLS table-level hides. For delete, omit entirely.",
                    },
                    "column_permissions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "string"},
                                "column": {"type": "string"},
                                "permission": {"type": "string", "enum": ["None", "Read"], "default": "None"},
                            },
                            "required": ["table", "column"],
                        },
                        "description": "OLS column-level permissions. permission='None' hides the column from this role entirely.",
                    },
                    "model_permission": {
                        "type": "string",
                        "enum": ["read", "readRefresh", "readWrite", "admin", "none"],
                        "default": "read",
                        "description": "Permission level. RLS/OLS roles are almost always 'read'.",
                    },
                },
                "required": ["project_path", "action", "name"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).manage_role(
                action=args["action"],
                name=args["name"],
                table_permissions=args.get("table_permissions"),
                model_permission=args.get("model_permission", "read"),
                column_permissions=args.get("column_permissions"),
            ),
        )
        self.add_tool(
            "bulk_create_rls_roles",
            (
                "Create one RLS role per value in a list, each restricted to rows where "
                "table[column] = value. Designed for the 'one role per supplier/client/region' pattern. "
                "Each role is named value (or role_prefix + value if prefix given). "
                "Use additional_tables to also filter related tables in the same role. "
                "on_conflict=skip (default) leaves existing roles untouched; "
                "on_conflict=overwrite rewrites them with the new filter."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "table": {
                        "type": "string",
                        "description": "Table name to filter (e.g. 'Suspend Comments').",
                    },
                    "column": {
                        "type": "string",
                        "description": "Column to match each value against (e.g. 'BillingSP').",
                    },
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of values — one role will be created per entry.",
                    },
                    "role_prefix": {
                        "type": "string",
                        "default": "",
                        "description": "Optional prefix for role names (e.g. 'Supplier_' → 'Supplier_Acme').",
                    },
                    "additional_tables": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "string"},
                                "dax": {"type": "string"},
                            },
                            "required": ["project_path", "table", "dax"],
                        },
                        "description": "Extra static table filters applied to every role (e.g. restrict a second table).",
                    },
                    "on_conflict": {
                        "type": "string",
                        "enum": ["skip", "overwrite"],
                        "default": "skip",
                        "description": "What to do if a role with that name already exists.",
                    },
                },
                "required": ["project_path", "table", "column", "values"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).bulk_create_rls_roles(
                table=args["table"],
                column=args["column"],
                values=args["values"],
                role_prefix=args.get("role_prefix", ""),
                additional_tables=args.get("additional_tables"),
                on_conflict=args.get("on_conflict", "skip"),
            ),
        )
        self.add_tool(
            "get_table_source",
            (
                "Parse a SemanticModel table's TMDL to return its data source connection details: "
                "server, database, the primary source table/view name, the full partition SQL query, "
                "and the list of columns defined in the model. "
                "Use this before run_source_query to discover the server/database and build an "
                "efficient query (e.g. SELECT DISTINCT on the source_table directly rather than "
                "wrapping the full partition query)."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "table": {"type": "string", "description": "Semantic model table name (e.g. 'Suspend Comments')."},
                },
                "required": ["project_path", "table"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).get_table_source(args["table"]),
        )
        self.add_tool(
            "run_source_query",
            (
                "Run a SQL query directly against a SQL Server data source using Windows Authentication. "
                "Use preview=true to fetch only 10 rows (fast check before committing to a full query). "
                "max_rows caps results (default 200, max 5000). "
                "Single-column results return a compact flat 'values' list instead of rows/columns — "
                "ideal for SELECT DISTINCT queries used to populate bulk_create_rls_roles. "
                "Multi-column results return 'columns' + 'rows' (arrays of arrays, not objects). "
                "Always reports 'row_count' and 'truncated' so you know if results were capped. "
                "Tip: query source_table directly (from get_table_source) rather than wrapping the "
                "full partition SQL — much faster on large tables."
            ),
            {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "SQL Server hostname."},
                    "database": {"type": "string", "description": "Database name."},
                    "sql": {"type": "string", "description": "SQL query to execute."},
                    "preview": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, fetches only 10 rows regardless of max_rows.",
                    },
                    "max_rows": {
                        "type": "integer",
                        "default": 200,
                        "description": "Maximum rows to return (capped at 5000).",
                    },
                },
                "required": ["server", "database", "sql"],
                "additionalProperties": False,
            },
            lambda args: self._run_source_query(
                server=args["server"],
                database=args["database"],
                sql=args["sql"],
                preview=bool(args.get("preview", False)),
                max_rows=min(int(args.get("max_rows", 200)), 5000),
            ),
        )
        self.add_tool(
            "get_distinct_values",
            (
                "Get the distinct non-null values of a column by querying the source database directly. "
                "Automatically resolves the server/database from the SemanticModel table's TMDL partition. "
                "Use this before bulk_create_rls_roles to discover supplier/client/region values. "
                "Returns a compact flat 'values' list. max_rows caps results (default 1000)."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "table": {"type": "string", "description": "SemanticModel table name (e.g. 'Suspend Comments')."},
                    "column": {"type": "string", "description": "Column to get distinct values for (e.g. 'BillingSP')."},
                    "max_rows": {"type": "integer", "default": 1000},
                },
                "required": ["project_path", "table", "column"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).get_distinct_values(
                table=args["table"],
                column=args["column"],
                max_rows=int(args.get("max_rows", 1000)),
            ),
        )
        self.add_tool(
            "bulk_delete_rls_roles",
            (
                "Delete multiple RLS roles at once by explicit name list or by name prefix. "
                "Use prefix to clean up all roles from a previous bulk_create run (e.g. prefix='Supplier_'). "
                "Returns lists of deleted and not_found names."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit list of role names to delete.",
                    },
                    "prefix": {
                        "type": "string",
                        "description": "Delete all roles whose name starts with this prefix.",
                    },
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: self.semantic_model(args).bulk_delete_rls_roles(
                names=args.get("names"),
                prefix=args.get("prefix"),
            ),
        )
        self.add_tool(
            "list_semantic_model_tables",
            (
                "List all user-defined tables in the SemanticModel with column and measure counts. "
                "Excludes auto-generated date tables by default. "
                "Use this to discover table names before calling get_table_source or get_distinct_values."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "include_system": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include auto-generated date/template tables.",
                    },
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: {"tables": self.semantic_model(args).list_tables(bool(args.get("include_system", False)))},
        )
        self.add_tool(
            "set_table_storage_mode",
            (
                "Change the storage mode of SemanticModel table partitions to import, directQuery, or dual. "
                "Use tables=[\"*\"] to switch every non-calculated, non-system table at once — typical when migrating "
                "an entire Import model to DirectQuery for live SQL data. "
                "Calculated (DAX) tables and auto-generated date tables are always skipped. "
                "dry_run=true previews what would change without writing any files. "
                "Returns per-table change details plus warnings for M-expression tables with inline SQL "
                "queries that may not fold — those tables may need database views for reliable DirectQuery. "
                "Call list_semantic_model_tables first to see current storage_mode per table. "
                "Restart Power BI Desktop / republish after switching modes."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "tables": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Table names to change, or [\"*\"] for all eligible tables.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["directQuery", "import", "dual"],
                        "description": "Target storage mode.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "Report changes without writing files.",
                    },
                },
                "required": ["project_path", "tables", "mode"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).set_storage_mode(
                tables=args.get("tables"),
                mode=args["mode"],
                dry_run=bool(args.get("dry_run", False)),
            ),
        )
        self.add_tool(
            "get_relationships",
            (
                "Return the SemanticModel relationship graph (table joins). "
                "Date table relationships are excluded by default. "
                "Use this to understand which tables are joined and design multi-table RLS filters."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "include_date_tables": {"type": "boolean", "default": False},
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: {
                "relationships": self.semantic_model(args).get_relationships(
                    bool(args.get("include_date_tables", False))
                )
            },
        )
        self.add_tool(
            "manage_measure",
            (
                "Create, update, delete, or list DAX measures in a SemanticModel table. "
                "action='create': add a new measure; dax is required. "
                "action='update': replace an existing measure's DAX and metadata. "
                "action='delete': remove a measure by name. "
                "action='list': return all measures in the table. "
                "Multiline DAX is supported. Also available as op='manage_measure' in pbir_apply_plan."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "update", "delete", "list"]},
                    "table": {"type": "string", "description": "Exact table name."},
                    "name": {"type": "string", "description": "Measure name."},
                    "dax": {"type": "string", "description": "DAX expression. Required for create/update."},
                    "format_string": {"type": "string", "description": "Format string (e.g. '#,##0.00', '$#,##0')."},
                    "display_folder": {"type": "string", "description": "Display folder name."},
                    "description": {"type": "string", "description": "Measure description."},
                },
                "required": ["project_path", "action", "table"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).manage_measure(
                action=args["action"],
                table=args["table"],
                name=args.get("name"),
                dax=args.get("dax"),
                format_string=args.get("format_string"),
                display_folder=args.get("display_folder"),
                description=args.get("description"),
            ),
        )
        self.add_tool(
            "list_perspectives",
            "List all perspectives defined in the SemanticModel.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: {"perspectives": self.semantic_model(args).list_perspectives()},
        )
        self.add_tool(
            "manage_perspective",
            (
                "Create, update, or delete a SemanticModel perspective. "
                "Perspectives define a named subset of tables, columns, and measures visible to report authors. "
                "action='create': write a new perspective TMDL file. "
                "action='update': replace an existing perspective. "
                "action='delete': remove the perspective. "
                "tables: [{table, measures: [...], columns: [...]}]. "
                "Also available as op='manage_perspective' in pbir_apply_plan."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "name": {"type": "string", "description": "Perspective name."},
                    "tables": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "string"},
                                "measures": {"type": "array", "items": {"type": "string"}},
                                "columns": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["table"],
                        },
                        "description": "Tables (with measures/columns) to include. Required for create/update.",
                    },
                },
                "required": ["project_path", "action", "name"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).manage_perspective(
                action=args["action"],
                name=args["name"],
                tables=args.get("tables"),
            ),
        )
        self.add_tool(
            "tmdl_diff",
            (
                "Compare this SemanticModel's TMDL definition folder against another SemanticModel path. "
                "Reports added files (in self, not in other), removed files, and changed files with unified diffs. "
                "Ignores lineageTag lines (they change on every Desktop save and are noise). "
                "Useful for reviewing what changed between two versions of a model."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "other_path": {"type": "string", "description": "Path to the other SemanticModel root or definition/ folder to compare against."},
                },
                "required": ["project_path", "other_path"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).tmdl_diff(args["other_path"]),
        )
        self.add_tool(
            "manage_relationship",
            (
                "Create or delete a relationship in the SemanticModel. "
                "Relationships are written to SemanticModel/definition/relationships.tmdl. "
                "action='create': add a new relationship with fromColumn → toColumn. "
                "action='delete': remove the relationship matching from_table.from_column → to_table.to_column. "
                "cardinality: 'manyToOne' (default), 'oneToOne', 'manyToMany'. "
                "cross_filter: 'singleDirection' (default), 'bothDirections'. "
                "active: false marks the relationship as inactive (used for role-playing dimensions)."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "delete"]},
                    "from_table": {"type": "string"},
                    "from_column": {"type": "string"},
                    "to_table": {"type": "string"},
                    "to_column": {"type": "string"},
                    "cardinality": {"type": "string", "enum": ["manyToOne", "oneToOne", "manyToMany"]},
                    "cross_filter": {"type": "string", "enum": ["singleDirection", "bothDirections"]},
                    "active": {"type": "boolean", "default": True},
                },
                "required": ["project_path", "action", "from_table", "from_column", "to_table", "to_column"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).manage_relationship(
                action=args["action"],
                from_table=args["from_table"],
                from_column=args["from_column"],
                to_table=args["to_table"],
                to_column=args["to_column"],
                cardinality=args.get("cardinality"),
                cross_filter=args.get("cross_filter"),
                active=bool(args.get("active", True)),
            ),
        )
        self.add_tool(
            "manage_hierarchy",
            (
                "Create, delete, or list hierarchies in a SemanticModel table. "
                "Hierarchies group columns into drill-down levels (e.g. Year → Quarter → Month → Day). "
                "action='create': add a hierarchy; levels=[{name, column}, ...] required. "
                "action='delete': remove a hierarchy by name. "
                "action='list': list all hierarchies in the table."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "delete", "list"]},
                    "table": {"type": "string"},
                    "name": {"type": "string", "description": "Hierarchy name."},
                    "levels": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "column": {"type": "string"},
                            },
                            "required": ["name", "column"],
                        },
                        "description": "Hierarchy levels in order. Required for create.",
                    },
                },
                "required": ["project_path", "action", "table"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).manage_hierarchy(
                action=args["action"],
                table=args["table"],
                name=args.get("name"),
                levels=args.get("levels"),
            ),
        )
        self.add_tool(
            "delete_page",
            "Delete a report page and remove it from the page order. Updates activePageName if needed.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).delete_page(
                args["page"], validate_after=bool(args.get("validate_after", False))
            ),
        )
        self.add_tool(
            "duplicate_page",
            (
                "Copy an existing page (all its visuals) to a new page with a new display name. "
                "The duplicate is inserted immediately after the source page in the page order."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "source_page": {"type": "string", "description": "Page to copy (internal name or displayName)."},
                    "new_display_name": {"type": "string", "description": "Display name for the new page."},
                    "new_name": {"type": "string", "description": "Internal page folder name (auto-generated if omitted)."},
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "source_page", "new_display_name"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).duplicate_page(
                source_page=args["source_page"],
                new_display_name=args["new_display_name"],
                new_name=args.get("new_name"),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "reorder_pages",
            (
                "Set the display order of report pages by providing an ordered list of page internal names. "
                "Any pages not listed are appended at the end in their original order."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page_order": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of page internal names (e.g. ['Overview', 'SupplierTrends']).",
                    },
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page_order"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).reorder_pages(
                page_order=args["page_order"],
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "set_visual_title",
            (
                "Set the title text of a visual, or show/hide the title bar. "
                "Shortcut for modify_visual_properties when you only need to change the title. "
                "Provide title to set the text (also auto-sets show=true). "
                "Provide show=false to hide the title bar without changing its text."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "visual": {"type": "string"},
                    "title": {"type": "string", "description": "New title text."},
                    "show": {"type": "boolean", "description": "Show or hide the title bar."},
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page", "visual"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).set_visual_title(
                page=args["page"],
                visual=args["visual"],
                title=args.get("title"),
                show=args.get("show"),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "run_python",
            (
                "Execute arbitrary Python code against the loaded report and semantic model. "
                "Use this when no existing tool covers the need — regex transforms, bulk renames, "
                "custom extractions, cross-visual analysis, or anything else. "
                "The script runs with these globals pre-bound: "
                "report (PBIRReport), sm (PBIRSemanticModel, if available), args (this call's args dict), "
                "read_json, write_json, _color_expr, _get_color_expr, _objects_item_props, deep_merge, "
                "Path, re, json, PBIRReport, PBIRSemanticModel, PBIRError. "
                "Set the 'result' variable to return data. "
                "Example: result = [v['name'] for p in report.page_dirs() "
                "for v in report.visual_dirs(p)]"
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "code": {"type": "string", "description": "Python code to execute. Set result = ... to return a value."},
                },
                "required": ["project_path", "code"],
                "additionalProperties": False,
            },
            lambda args: self._run_python_tool(args["code"], self._build_exec_context(args)),
        )
        self.add_tool(
            "register_custom_tool",
            (
                "Save a Python script as a named, reusable MCP tool that persists across server restarts. "
                "The tool is saved to custom_tools/{name}.json and immediately available in this session. "
                "The code body has the same globals as run_python (report, sm, args, read_json, etc.). "
                "Use this to promote a useful run_python script into a permanent tool. "
                "Provide schema as a JSON Schema object describing the tool's parameters."
            ),
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name (snake_case, no spaces)."},
                    "description": {"type": "string", "description": "What the tool does."},
                    "code": {"type": "string", "description": "Python body. Set result = ... to return a value."},
                    "schema": {
                        "type": "object",
                        "description": "JSON Schema for this tool's parameters. Include project_path if needed.",
                    },
                },
                "required": ["name", "description", "code"],
                "additionalProperties": False,
            },
            lambda args: self._save_and_register_custom_tool(args),
        )
        self.add_tool(
            "list_custom_tools",
            "List all saved custom tools with their names and descriptions.",
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            lambda args: self._list_custom_tools(),
        )
        self.add_tool(
            "delete_custom_tool",
            "Permanently delete a saved custom tool by name.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name to delete."},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            lambda args: self._delete_custom_tool(args["name"]),
        )
        self.add_tool(
            "load_project",
            "Load a PBIP/PBIR report project. Returns a compact validation summary.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                "required": ["project_path"],
                "additionalProperties": False,
            },
            self.tool_load_project,
        )
        self.add_tool(
            "get_current_project",
            "Return the currently loaded PBIR report project path, or null if none is loaded.",
            schema(),
            lambda args: {"report_root": str(self.current_report.report_root) if self.current_report else None},
        )
        self.add_tool(
            "validate_report",
            "Run full structural validation. Returns all errors and warnings.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: self.report(args).validate().as_dict(),
        )
        self.add_tool(
            "pbir_report_summary",
            "Compact report overview: pages, visual counts, theme, optional bindings and layout analysis. Use instead of list_pages+list_visuals+validate_report.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "include_visuals": {"type": "boolean", "default": False},
                    "include_bindings": {"type": "boolean", "default": False},
                    "include_raw": {"type": "boolean", "default": False},
                    "include_paths": {"type": "boolean", "default": False},
                    "include_layout": {"type": "boolean", "default": False},
                    "max_visuals_per_page": {"type": "integer", "default": 50},
                    "validate": {"type": "boolean", "default": True},
                    "include_validation_messages": {"type": "boolean", "default": False},
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            self.tool_report_summary,
        )
        self.add_tool(
            "get_report_context",
            (
                "ONE-CALL ORIENTATION. Returns a compact text summary of the full report: pages, every visual "
                "(type, name, title, field bindings, position), theme, and table abbreviations. "
                "Call this first at the start of any session — ~1-2k tokens for a typical report vs "
                "10-20k for report_summary with bindings. Then use get_visual or search_semantic_model to drill in."
            ),
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: self.report(args).get_report_context(),
        )
        self.add_tool(
            "search_semantic_model",
            (
                "Search the semantic model for fields matching a keyword — much cheaper than read_semantic_model "
                "when you only need a few fields. Matches table names AND field names. "
                "Optionally filter by table name or kind (column/measure)."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "query": {"type": "string", "description": "Keyword to match against table names and field names (case-insensitive substring)."},
                    "table": {"type": "string", "description": "Filter results to this table name (partial match)."},
                    "field_type": {"type": "string", "enum": ["column", "measure"], "description": "Return only columns or only measures."},
                },
                "required": ["project_path", "query"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).search_semantic_model_fields(
                query=args["query"],
                table=args.get("table"),
                field_type=args.get("field_type"),
            ),
        )
        self.add_tool(
            "read_semantic_model",
            "Parse the SemanticModel referenced by the loaded report. Returns tables, columns (with dataType), measures, and source connection hints. Call this before add_visual_from_template to get correct entity/column names.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: self.report(args).read_semantic_model(),
        )
        self.add_tool(
            "list_projects",
            "Scan a directory recursively for Power BI .pbip project files. Returns name, path, and version for each project found.",
            {
                "type": "object",
                "properties": {
                    "search_root": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 4},
                },
                "required": ["project_path", "search_root"],
                "additionalProperties": False,
            },
            self.tool_list_projects,
        )
        self.add_tool(
            "pbir_apply_plan",
            "Batch multiple page/visual/theme/role/model actions in one call, validating once at the end. Prefer over individual tools when making several changes. Supported ops: create_page, update_page, delete_page, duplicate_page, reorder_pages, set_active_page, add_visual, clone_visual, set_visual_position, set_visual_title, modify_visual_properties, delete_visual, manage_theme, apply_style_preset, recolor_visuals, set_page_background, set_visual_filter, set_page_filter, bulk_create_rls_roles, set_table_storage_mode, manage_measure, manage_perspective, manage_relationship, manage_hierarchy, validate.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False},
                    "validate_after": {"type": "boolean", "default": True},
                    "continue_on_error": {"type": "boolean", "default": False},
                    "auto_undo": {"type": "boolean", "default": True},
                    "auto_commit": {"type": "boolean", "default": False},
                    "commit_message": {"type": "string", "default": "PBIR MCP apply plan"},
                    "response": {
                        "type": "object",
                        "properties": {
                            "include_summary": {"type": "boolean", "default": False},
                            "include_action_results": {
                                "type": "string",
                                "enum": ["none", "errors", "all"],
                                "default": "errors",
                            },
                            "include_validation_messages": {"type": "boolean", "default": False},
                        },
                        "additionalProperties": False,
                    },
                    "actions": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                },
                "required": ["actions"],
                "additionalProperties": False,
            },
            self.tool_apply_plan,
        )
        self.add_tool(
            "list_pages",
            "List report pages with optional visuals.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "include_visuals": {"type": "boolean", "default": False},
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: self.report(args).list_pages(bool(args.get("include_visuals", False))),
        )
        self.add_tool(
            "list_visuals",
            "List visuals across the report or on a single page. Set include_bindings=true to see field assignments per visual.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "include_bindings": {"type": "boolean", "default": False},
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: self.report(args).list_visuals(
                args.get("page"), include_bindings=bool(args.get("include_bindings", False))
            ),
        )
        self.add_tool(
            "get_visual",
            (
                "Get full details for a single visual: type, position, field bindings per query role, "
                "formatting summary (custom colors, title text/color, background, slicer header), "
                "and which object groups are set. Use before modify_visual_properties to understand current state."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "visual": {"type": "string"},
                },
                "required": ["project_path", "page", "visual"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).get_visual_detail(args["page"], args["visual"]),
        )
        self.add_tool(
            "create_page",
            "Create a new page. Use pbir_apply_plan for bulk creation.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "display_name": {"type": "string"},
                    "name": {"type": "string"},
                    "width": {"type": "number", "default": 1280},
                    "height": {"type": "number", "default": 720},
                    "display_option": {"type": "string", "default": "FitToPage"},
                    "make_active": {"type": "boolean", "default": False},
                    "background": {"type": "object"},
                    "visibility": {
                        "type": "string",
                        "enum": ["HiddenInViewMode", ""],
                        "description": "Hide this page in view/publish mode (drillthrough/tooltip pages). Pass '' to make visible.",
                    },
                },
                "required": ["project_path", "display_name"],
                "additionalProperties": False,
            },
            self.tool_create_page,
        )
        self.add_tool(
            "set_active_page",
            "Set the default page Power BI opens.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "landing": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).set_active_page(args["page"], bool(args.get("landing", False))),
        )
        self.add_tool(
            "add_visual_from_template",
            "Add a visual. Use pbir_apply_plan when adding multiple visuals. "
            "query_roles maps each data role to its projection(s). Role names are "
            "normalized per visual type, so chart aliases Axis->Category, Values->Y, "
            "Legend->Series are accepted; cards/tables/slicers use Values; matrix uses "
            "Rows/Columns/Values; cardVisual uses Data. Any rename is reported back in "
            "result.role_remaps. sort sets the default sort order. filter_config adds "
            "visual-level filters.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "template": {"type": "string", "default": "card"},
                    "visual_type": {"type": "string"},
                    "title": {"type": "string"},
                    "name": {"type": "string"},
                    "position": {"type": "object"},
                    "query_roles": {
                        "type": "object",
                        "description": (
                            "Map of data role -> projection or list of projections. "
                            "Cartesian charts render Category/Y/Series (aliases "
                            "Axis/Values/Legend are auto-corrected); pie/donut use "
                            "Category/Y; card/table/slicer use Values; cardVisual uses "
                            "Data; matrix uses Rows/Columns/Values. Each projection is "
                            "{table, column|measure[, aggregate]}, "
                            "{table, hierarchy_level: {level, hierarchy?, auto_date?, "
                            "variation_name?, variation_property?, column?}}, "
                            "or {raw, queryRef}."
                        ),
                    },
                    "sort": {
                        "type": "array",
                        "description": (
                            "Default sort order. Each entry is a projection spec plus "
                            "direction ('Ascending' or 'Descending')."
                        ),
                        "items": {"type": "object"},
                    },
                    "filter_config": {
                        "type": "array",
                        "description": (
                            "Visual-level filters. Each entry is a projection spec plus "
                            "filter_kind ('in', 'not_in', 'comparison', 'range', 'top_n', 'relative_date'), "
                            "values/value, operator (eq/lt/le/ge/gt/ne), and type ('Categorical'/'Advanced'). "
                            "relative_date: {period: 'days'|'weeks'|'months'|'years', count: N, direction: 'last'|'next'|'this'}. "
                            "Pass filter: {...} directly for raw filter JSON."
                        ),
                        "items": {"type": "object"},
                    },
                    "properties": {"type": "object"},
                    "raw_patch": {"type": "object"},
                },
                "required": ["project_path", "page"],
                "additionalProperties": False,
            },
            self.tool_add_visual,
        )
        self.add_tool(
            "clone_visual",
            "Copy a visual to another page with optional new title and position.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "source_page": {"type": "string"},
                    "source_visual": {"type": "string"},
                    "target_page": {"type": "string"},
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                    "position": {"type": "object"},
                },
                "required": ["project_path", "source_page", "source_visual", "target_page"],
                "additionalProperties": False,
            },
            self.tool_clone_visual,
        )
        self.add_tool(
            "set_visual_position",
            "Resize or reposition a visual.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "visual": {"type": "string"},
                    "position": {"type": "object"},
                },
                "required": ["project_path", "page", "visual", "position"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).set_visual_position(args["page"], args["visual"], args["position"]),
        )
        self.add_tool(
            "modify_visual_properties",
            "Deep-merge a JSON patch into a visual.json.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "visual": {"type": "string"},
                    "patch": {"type": "object"},
                },
                "required": ["project_path", "page", "visual", "patch"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).modify_visual_properties(args["page"], args["visual"], args["patch"]),
        )
        self.add_tool(
            "delete_visual",
            "Delete a visual from a page.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "visual": {"type": "string"},
                },
                "required": ["project_path", "page", "visual"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).delete_visual(args["page"], args["visual"]),
        )
        self.add_tool(
            "set_visual_filter",
            "Set or merge visual-level filters on an existing visual. Each filter entry is a "
            "projection spec ({table, column|measure|hierarchy_level}) plus filter_kind ('in', "
            "'not_in', 'comparison', 'range', 'top_n', 'relative_date'), values/value, operator, and type. "
            "relative_date: add relative_date: {period: 'days'|'weeks'|'months'|'years', count: N, direction: 'last'|'next'|'this'}. "
            "Pass merge=true to append to existing filters; default replaces all.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "visual": {"type": "string"},
                    "filters": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "List of filter specs. Each: {table, column|measure|hierarchy_level, "
                            "filter_kind ('in'|'not_in'|'comparison'|'range'|'top_n'), "
                            "values (list for in/not_in), value+operator (comparison), "
                            "min_value/max_value+include_min/include_max (range), "
                            "top_n+by_measure[+by_table+bottom] (top_n), "
                            "type ('Categorical'/'Advanced'/'TopN'), "
                            "isHiddenInViewMode, isLockedInViewMode. "
                            "Pass filter: {...} for raw filter JSON."
                        ),
                    },
                    "merge": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, append to existing filterConfig.filters; otherwise replace.",
                    },
                },
                "required": ["project_path", "page", "visual", "filters"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).set_visual_filter(
                page=args["page"],
                visual=args["visual"],
                filters=args["filters"],
                merge=bool(args.get("merge", False)),
            ),
        )
        self.add_tool(
            "set_page_filter",
            (
                "Set or replace page-level filters applied to all visuals on a page. "
                "Accepts the same filter specs as set_visual_filter: "
                "Categorical IN: {table, column, values: [...]}. "
                "Categorical NOT IN: {table, column, values: [...], filter_kind: 'not_in'}. "
                "Comparison: {table, column, operator: '>='|'>'|'<='|'<'|'=', value: ...}. "
                "Range: {table, column, filter_kind: 'range', min_value, max_value}. "
                "TopN: {table, column, filter_kind: 'top_n', top_n: N, by_measure: '...'}. "
                "RelativeDate: {table, column, filter_kind: 'relative_date', relative_date: {period, count, direction}}. "
                "Raw: {raw: {...}}. Pass filters=[] to clear all page filters. "
                "Also available as op='set_page_filter' in pbir_apply_plan."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "filters": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                        "description": "List of filter specs. Pass [] to clear page filters.",
                    },
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page", "filters"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).set_page_filter(
                page=args["page"],
                filters=args.get("filters", []),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "manage_visual_calc",
            (
                "Add, remove, or list NativeVisualCalculation projections on a visual. "
                "NativeVisualCalculations are visual-level DAX expressions (RUNNINGSUM, RANK, etc.) "
                "that appear as extra columns/measures bound to a visual role. "
                "action='add': add a calc to the named role; requires name (the queryRef/calc name). "
                "action='remove': remove projections matching name from all roles. "
                "action='list': list all NativeVisualCalculation projections on the visual."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "visual": {"type": "string"},
                    "action": {"type": "string", "enum": ["add", "remove", "list"]},
                    "role": {"type": "string", "description": "Visual role name (e.g. 'Y', 'Values'). Required for add."},
                    "name": {"type": "string", "description": "Calc name / queryRef. Required for add/remove."},
                    "expression": {"type": "object", "description": "Optional PBIR expression object for the calc."},
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page", "visual", "action"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).manage_visual_calc(
                page=args["page"],
                visual=args["visual"],
                action=args["action"],
                role=args.get("role", ""),
                name=args.get("name"),
                expression=args.get("expression"),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "list_bookmarks",
            "List all bookmarks defined in the report.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: {"bookmarks": self.report(args).list_bookmarks()},
        )
        self.add_tool(
            "manage_bookmark",
            (
                "Create, delete, or list report bookmarks. "
                "action='create': create a bookmark that records visual visibility state. "
                "Supply page + visible_visuals/hidden_visuals to capture which visuals are shown or hidden. "
                "action='delete': remove a bookmark by name. "
                "action='list': list all bookmarks. "
                "Also available as op='manage_bookmark' in pbir_apply_plan."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "delete", "list"]},
                    "name": {"type": "string", "description": "Bookmark folder/internal name."},
                    "display_name": {"type": "string", "description": "Human-readable bookmark name."},
                    "page": {"type": "string", "description": "Page name (for visibility bookmarks)."},
                    "visible_visuals": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Visual names to mark as visible in this bookmark.",
                    },
                    "hidden_visuals": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Visual names to mark as hidden in this bookmark.",
                    },
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "action"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).manage_bookmark(
                action=args["action"],
                name=args.get("name"),
                display_name=args.get("display_name"),
                page=args.get("page"),
                visible_visuals=args.get("visible_visuals"),
                hidden_visuals=args.get("hidden_visuals"),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "set_conditional_format",
            (
                "Bind a measure or column to a visual property for conditional formatting. "
                "target: shorthand ('backColor', 'fontColor', 'fill', 'calloutValue', 'titleFontColor', 'labelColor', 'dataPointColor') "
                "or 'section.property' (e.g. 'values.backColor', 'dataPoint.fill'). "
                "format_type='measure': spec={table, measure} — color-returning measure drives the property. "
                "format_type='column': spec={table, column[, aggregate: 0-5]} — aggregated column drives the property. "
                "format_type='clear': removes the conditional format from the target property. "
                "Gradient and rules formats must be set via raw_patch (modify_visual_properties). "
                "Also available as op='set_conditional_format' in pbir_apply_plan."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "visual": {"type": "string"},
                    "target": {
                        "type": "string",
                        "description": "Property to format: 'backColor', 'fontColor', 'calloutValue', 'fill', 'dataPointColor', 'titleFontColor', 'labelColor', or 'section.property'.",
                    },
                    "format_type": {
                        "type": "string",
                        "enum": ["measure", "column", "clear"],
                    },
                    "spec": {
                        "type": "object",
                        "description": "For measure: {table, measure}. For column: {table, column[, aggregate]}.",
                    },
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page", "visual", "target", "format_type"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).set_conditional_format(
                page=args["page"],
                visual=args["visual"],
                target=args["target"],
                format_type=args["format_type"],
                spec=args.get("spec", {}),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "bulk_visual_op",
            (
                "Apply an operation to all visuals on a page that match the given filters. "
                "op: 'list' (return matched visuals), 'delete', 'hide' (set isHidden=true), 'show' (clear isHidden), "
                "'update_title' (update.title applied to all matched), 'update_position' (position fields from update). "
                "filter_type: match by exact visualType string. "
                "filter_name: match visuals whose name contains this substring. "
                "filter_bounds: {x_min, x_max, y_min, y_max} — filter by visual position. "
                "Useful for mass-rebranding, bulk cleanup, or hiding groups of visuals."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                    "op": {
                        "type": "string",
                        "enum": ["list", "delete", "hide", "show", "update_title", "update_position"],
                    },
                    "filter_type": {"type": "string", "description": "Filter by exact visualType."},
                    "filter_name": {"type": "string", "description": "Filter by name substring."},
                    "filter_bounds": {
                        "type": "object",
                        "description": "Filter by position: {x_min, x_max, y_min, y_max}.",
                        "properties": {
                            "x_min": {"type": "number"}, "x_max": {"type": "number"},
                            "y_min": {"type": "number"}, "y_max": {"type": "number"},
                        },
                    },
                    "update": {
                        "type": "object",
                        "description": "For update_title: {title: '...'}. For update_position: position fields.",
                    },
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "page", "op"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).bulk_visual_op(
                page=args["page"],
                op=args["op"],
                filter_type=args.get("filter_type"),
                filter_name=args.get("filter_name"),
                filter_bounds=args.get("filter_bounds"),
                update=args.get("update"),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "list_custom_visuals",
            "List custom (.pbiviz) visuals registered in the report.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: {"custom_visuals": self.report(args).list_custom_visuals()},
        )
        self.add_tool(
            "import_custom_visual",
            (
                "Import a .pbiviz custom visual file into the report. "
                "Copies the file to StaticResources/RegisteredResources/ and registers it in report.json. "
                "The .pbiviz must be a valid ZIP containing package.json with visual metadata (name, guid, version). "
                "After import, the visual type is available for add_visual_from_template using the guid as visual_type."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "pbiviz_path": {"type": "string", "description": "Absolute path to the .pbiviz file to import."},
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "pbiviz_path"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).import_custom_visual(
                pbiviz_path=args["pbiviz_path"],
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "remove_custom_visual",
            (
                "Remove a custom visual registration from report.json. "
                "Pass the visual guid (as shown by list_custom_visuals). "
                "Set delete_file=false to keep the .pbiviz file in StaticResources while unregistering it."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "guid": {"type": "string", "description": "Visual guid to remove."},
                    "delete_file": {"type": "boolean", "default": True},
                    "validate_after": {"type": "boolean", "default": False},
                },
                "required": ["project_path", "guid"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).remove_custom_visual(
                guid=args["guid"],
                delete_file=bool(args.get("delete_file", True)),
                validate_after=bool(args.get("validate_after", False)),
            ),
        )
        self.add_tool(
            "manage_theme",
            "List or update report theme metadata.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "action": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "theme": {"type": "object"},
                    "file_name": {"type": "string"},
                },
                "required": ["project_path", "action"],
                "additionalProperties": False,
            },
            self.tool_manage_theme,
        )
        self.add_tool(
            "export_project_zip",
            (
                "Bundle THIS PBIR project (its .pbip entry point + .Report folder + the referenced "
                ".SemanticModel folder) into a clean export folder and a .zip the user opens in Power BI "
                "Desktop. Scoped to the one resolved project, so unrelated siblings in a shared parent "
                "(other .pbip copies, a stray .pbix, exports, scripts) are NOT swept in. Call as the final "
                "step after building/modifying a report. NOTE: this is a PBIP source bundle, not a "
                "self-contained .pbix — the recipient Refreshes from the data source on open. Returns "
                "the zip path, size, the staged export folder, and what was included."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string", "description": "Path to the project folder or any file inside it. Uses the currently loaded project if omitted."},
                    "output_dir": {"type": "string", "description": "Directory to write the export into (e.g. C:\\Users\\RZHOU\\Downloads). The zip is named automatically as <ProjectName>.zip. Defaults to a '<ProjectName> Export' folder next to the project."},
                    "keep_folder": {"type": "boolean", "default": True, "description": "Keep the unzipped staged project folder alongside the .zip (default true). Set false to leave only the .zip."},
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            self.tool_export_project_zip,
        )

        self.add_tool(
            "pbix_extract",
            "Extract a zip-backed .pbix/.pbit package to a folder for round-trip editing or inspection.",
            {
                "type": "object",
                "properties": {
                    "pbix_path": {"type": "string"},
                    "output_dir": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["pbix_path"],
                "additionalProperties": False,
            },
            lambda args: PBIRReport.extract_pbix(args["pbix_path"], args.get("output_dir"), bool(args.get("overwrite", False))),
        )
        self.add_tool(
            "pbix_package",
            "Create a .pbix/.pbit file by zipping an extracted package folder.",
            {
                "type": "object",
                "properties": {
                    "source_dir": {"type": "string"},
                    "output_path": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["source_dir", "output_path"],
                "additionalProperties": False,
            },
            lambda args: PBIRReport.package_pbix(args["source_dir"], args["output_path"], bool(args.get("overwrite", False))),
        )
        self.add_tool(
            "create_undo_snapshot",
            "Create a restorable undo snapshot of the current report and referenced SemanticModel.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "label": {"type": "string", "default": "manual"},
                    "include_semantic_model": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
            lambda args: self.report(args).create_undo_snapshot(
                label=args.get("label", "manual"),
                include_semantic_model=bool(args.get("include_semantic_model", True)),
            ),
        )
        self.add_tool(
            "list_undo_snapshots",
            "List undo snapshots available for this report.",
            {"type": "object", "properties": {"project_path": {"type": "string"}}, "additionalProperties": False},
            lambda args: {"snapshots": self.report(args).list_undo_snapshots()},
        )
        self.add_tool(
            "undo_last_action",
            "Restore the most recent undo snapshot, or a specific snapshot id.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}, "snapshot_id": {"type": "string"}},
                "additionalProperties": False,
            },
            lambda args: self.report(args).undo_last_action(args.get("snapshot_id")),
        )
        self.add_tool(
            "audit_report",
            "Run a quality audit: empty visuals, crowded pages, layout overflow/overlap, unused measures, and RLS membership checks when membership data is provided.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "max_visuals_per_page": {"type": "integer", "default": 20},
                    "include_unused_measures": {"type": "boolean", "default": True},
                    "role_memberships": {"type": "object"},
                },
                "additionalProperties": False,
            },
            lambda args: self.report(args).audit_report(
                max_visuals_per_page=int(args.get("max_visuals_per_page", 20)),
                include_unused_measures=bool(args.get("include_unused_measures", True)),
                role_memberships=args.get("role_memberships"),
            ),
        )
        self.add_tool(
            "multi_report_apply_plan",
            "Apply the same pbir_apply_plan actions across every PBIR/PBIP report discovered under a folder.",
            {
                "type": "object",
                "properties": {
                    "search_root": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 4},
                    "actions": {"type": "array", "items": {"type": "object"}},
                    "dry_run": {"type": "boolean", "default": False},
                    "continue_on_error": {"type": "boolean", "default": True},
                    "validate_after": {"type": "boolean", "default": True},
                },
                "required": ["search_root", "actions"],
                "additionalProperties": False,
            },
            self.tool_multi_report_apply_plan,
        )
        self.add_tool(
            "render_report_preview",
            "Write a lightweight HTML preview showing pages, visual rectangles, titles, visual types, and bindings.",
            {"type": "object", "properties": {"project_path": {"type": "string"}, "output_path": {"type": "string"}}, "additionalProperties": False},
            lambda args: self.report(args).render_preview(args.get("output_path")),
        )
        self.add_tool(
            "migrate_report_layout",
            "Copy pages and visuals from the loaded/source report to a target report, with best-effort field rebinding by matching field names.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "target_project_path": {"type": "string"},
                    "overwrite_pages": {"type": "boolean", "default": False},
                    "remap_fields": {"type": "boolean", "default": True},
                },
                "required": ["target_project_path"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).migrate_layout_to(
                args["target_project_path"],
                overwrite_pages=bool(args.get("overwrite_pages", False)),
                remap_fields=bool(args.get("remap_fields", True)),
            ),
        )
        self.add_tool(
            "add_measure_from_template",
            "Create or update a DAX measure from a curated template.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "template": {"type": "string"},
                    "table": {"type": "string"},
                    "name": {"type": "string"},
                    "params": {"type": "object"},
                    "action": {"type": "string", "enum": ["create", "update"], "default": "create"},
                },
                "required": ["template", "table", "name"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).add_measure_from_template(
                template=args["template"],
                table=args["table"],
                name=args["name"],
                params=args.get("params"),
                action=args.get("action", "create"),
            ),
        )
        self.add_tool(
            "powerbi_service_request",
            "Call the Power BI REST API using args.token or POWERBI_ACCESS_TOKEN.",
            {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "default": "GET"},
                    "path": {"type": "string"},
                    "body": {"type": "object"},
                    "token": {"type": "string"},
                    "base_url": {"type": "string"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda args: self._powerbi_service_request(args.get("method", "GET"), args["path"], args.get("body"), args.get("token"), args.get("base_url")),
        )
        self.add_tool(
            "powerbi_refresh_dataset",
            "Trigger a Power BI Service dataset refresh.",
            {
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "string"},
                    "workspace_id": {"type": "string"},
                    "notify_option": {"type": "string", "default": "NoNotification"},
                    "token": {"type": "string"},
                },
                "required": ["dataset_id"],
                "additionalProperties": False,
            },
            lambda args: self._powerbi_service_request(
                "POST",
                f"/groups/{args['workspace_id']}/datasets/{args['dataset_id']}/refreshes" if args.get("workspace_id") else f"/datasets/{args['dataset_id']}/refreshes",
                {"notifyOption": args.get("notify_option", "NoNotification")},
                args.get("token"),
                None,
            ),
        )
        self.add_tool(
            "cross_report_index",
            "Index reports under a folder and answer cross-report questions about measure usage, missing RLS, or visual types.",
            {
                "type": "object",
                "properties": {
                    "search_root": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 4},
                    "measure": {"type": "string"},
                    "visual_type": {"type": "string"},
                    "reports_without_rls": {"type": "boolean", "default": False},
                },
                "required": ["search_root"],
                "additionalProperties": False,
            },
            self.tool_cross_report_index,
        )
        self.add_tool(
            "commit_report_version",
            "Git add and commit the report and referenced SemanticModel as a version snapshot.",
            {"type": "object", "properties": {"project_path": {"type": "string"}, "message": {"type": "string"}}, "additionalProperties": False},
            lambda args: self.report(args).git_commit(args.get("message", "PBIR MCP snapshot")),
        )
        self.add_tool(
            "rollback_report_version",
            "Restore the report and referenced SemanticModel paths from a git ref.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}, "ref": {"type": "string"}},
                "required": ["ref"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).git_rollback(args["ref"]),
        )

    def discover_reports(self, search_root: str | Path, max_depth: int = 4) -> list[PBIRReport]:
        root = Path(search_root).expanduser().resolve()
        if not root.exists():
            raise PBIRError(f"Search root does not exist: {root}")
        seen: set[Path] = set()
        reports: list[PBIRReport] = []

        def add_candidate(path: Path) -> None:
            try:
                report_root = PBIRReport.resolve_report_root(path)
            except PBIRError:
                return
            if report_root not in seen:
                seen.add(report_root)
                reports.append(PBIRReport(report_root))

        def scan(path: Path, depth: int) -> None:
            if depth > max_depth:
                return
            if path.name.startswith(".") and path != root:
                return
            if path.is_file() and path.suffix.lower() == ".pbip":
                add_candidate(path)
                return
            if path.is_dir():
                if path.suffix.lower() == ".report" and (path / "definition.pbir").exists():
                    add_candidate(path)
                    return
                try:
                    children = sorted(path.iterdir(), key=lambda p: p.name.lower())
                except PermissionError:
                    return
                for child in children:
                    scan(child, depth + 1)

        scan(root, 0)
        return reports

    def _powerbi_service_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        import os
        import urllib.error
        import urllib.request
        token = token or os.environ.get("POWERBI_ACCESS_TOKEN")
        if not token:
            raise PBIRError("Power BI access token is required. Pass token or set POWERBI_ACCESS_TOKEN.")
        base = (base_url or "https://api.powerbi.com/v1.0/myorg").rstrip("/")
        url = path if path.lower().startswith(("http://", "https://")) else base + "/" + path.lstrip("/")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                parsed: Any
                try:
                    parsed = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    parsed = raw
                return {"status": resp.status, "url": url, "body": parsed}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = raw
            raise PBIRError(f"Power BI REST request failed ({exc.code}) {url}: {parsed}") from exc

    def tool_multi_report_apply_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        reports = self.discover_reports(args["search_root"], int(args.get("max_depth", 4)))
        results = []
        errors = []
        dry_run = bool(args.get("dry_run", False))
        continue_on_error = bool(args.get("continue_on_error", True))
        for report in reports:
            try:
                local_args = {
                    "project_path": str(report.report_root),
                    "actions": args["actions"],
                    "dry_run": dry_run,
                    "continue_on_error": continue_on_error,
                    "validate_after": bool(args.get("validate_after", True)),
                }
                result = self.tool_apply_plan(local_args)
                results.append({"report_root": str(report.report_root), "result": result})
            except Exception as exc:
                item = {"report_root": str(report.report_root), "error": str(exc)}
                errors.append(item)
                results.append(item)
                if not continue_on_error:
                    break
        return {
            "search_root": str(Path(args["search_root"]).expanduser().resolve()),
            "report_count": len(reports),
            "success_count": len([r for r in results if "result" in r and r["result"].get("ok")]),
            "error_count": len(errors),
            "results": results,
        }

    def tool_cross_report_index(self, args: dict[str, Any]) -> dict[str, Any]:
        reports = self.discover_reports(args["search_root"], int(args.get("max_depth", 4)))
        indexed = []
        measure_filter = (args.get("measure") or "").lower()
        visual_type_filter = (args.get("visual_type") or "").lower()
        only_without_rls = bool(args.get("reports_without_rls", False))
        for report in reports:
            entry: dict[str, Any] = {"report_root": str(report.report_root), "name": report.report_root.stem}
            try:
                visuals = report.list_visuals(include_bindings=True)["visuals"]
                roles = []
                sm = PBIRSemanticModel.from_report(report.report_root)
                if sm:
                    roles = sm.list_roles()
                measure_uses = []
                visual_hits = []
                for visual in visuals:
                    if visual_type_filter and str(visual.get("visualType", "")).lower() == visual_type_filter:
                        visual_hits.append({k: visual.get(k) for k in ("page", "name", "title", "visualType")})
                    for binding in visual.get("bindings") or []:
                        if "measure" in str(binding.get("kind", "")):
                            measure_name = str(binding.get("name", ""))
                            if not measure_filter or measure_filter == measure_name.lower():
                                measure_uses.append({
                                    "measure": measure_name,
                                    "table": binding.get("table"),
                                    "page": visual.get("page"),
                                    "visual": visual.get("name"),
                                    "visualType": visual.get("visualType"),
                                })
                entry.update({
                    "page_count": len(report.page_dirs()),
                    "visual_count": len(visuals),
                    "rls_role_count": len(roles),
                    "measure_uses": measure_uses,
                    "visual_type_matches": visual_hits,
                })
            except Exception as exc:
                entry["error"] = str(exc)
            if only_without_rls and entry.get("rls_role_count"):
                continue
            indexed.append(entry)
        return {"report_count": len(indexed), "reports": indexed}

    def tool_report_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.report(args).report_summary(
            include_visuals=bool(args.get("include_visuals", False)),
            include_bindings=bool(args.get("include_bindings", False)),
            include_raw=bool(args.get("include_raw", False)),
            include_paths=bool(args.get("include_paths", False)),
            include_layout=bool(args.get("include_layout", False)),
            max_visuals_per_page=int(args.get("max_visuals_per_page", 50)),
            validate=bool(args.get("validate", True)),
            include_validation_messages=bool(args.get("include_validation_messages", False)),
        )

    def tool_apply_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        report = self.report(args)
        dry_run = bool(args.get("dry_run", False))
        self.emit_progress({
            "tool": "pbir_apply_plan",
            "phase": "started",
            "report_root": str(report.report_root),
            "dry_run": dry_run,
            "actions_total": len(args.get("actions") or []) if isinstance(args.get("actions"), list) else None,
        })
        if dry_run:
            with tempfile.TemporaryDirectory(prefix="pbir-plan-") as temp:
                temp_report_root = Path(temp) / report.report_root.name
                shutil.copytree(report.report_root, temp_report_root)
                sm_path = report.semantic_model_path()
                if sm_path and sm_path.exists():
                    shutil.copytree(sm_path, Path(temp) / sm_path.name)
                working_report = PBIRReport(temp_report_root)
                result = self.apply_plan_to_report(working_report, args, dry_run=True)
                result["source_report_root"] = str(report.report_root)
                self.emit_progress({
                    "tool": "pbir_apply_plan",
                    "phase": "finished",
                    "report_root": str(report.report_root),
                    "dry_run": True,
                    "ok": bool(result.get("ok")),
                    "actions_applied": result.get("actions_applied"),
                    "actions_failed": result.get("actions_failed"),
                })
                return result
        snapshot = None
        if bool(args.get("auto_undo", True)):
            snapshot = report.create_undo_snapshot("before pbir_apply_plan")
        result = self.apply_plan_to_report(report, args, dry_run=False)
        if snapshot:
            result["undo_snapshot"] = snapshot
        if result.get("ok") and bool(args.get("auto_commit", False)):
            result["git_commit"] = report.git_commit(args.get("commit_message", "PBIR MCP apply plan"))
        self.emit_progress({
            "tool": "pbir_apply_plan",
            "phase": "finished",
            "report_root": str(report.report_root),
            "dry_run": False,
            "ok": bool(result.get("ok")),
            "actions_applied": result.get("actions_applied"),
            "actions_failed": result.get("actions_failed"),
        })
        return result

    def apply_plan_to_report(self, report: PBIRReport, args: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        actions = args.get("actions")
        if not isinstance(actions, list):
            raise PBIRError("pbir_apply_plan requires actions to be an array.")
        response = args.get("response") or {}
        if not isinstance(response, dict):
            raise PBIRError("response must be an object when provided.")
        include_summary = bool(response.get("include_summary", False))
        include_action_results = str(response.get("include_action_results", "errors"))
        if include_action_results not in {"none", "errors", "all"}:
            raise PBIRError("response.include_action_results must be one of: none, errors, all.")
        include_validation_messages = bool(response.get("include_validation_messages", False))
        continue_on_error = bool(args.get("continue_on_error", False))
        validate_after = bool(args.get("validate_after", True))

        action_results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        created: dict[str, list[Any]] = {"pages": [], "visuals": []}
        changed: dict[str, list[Any]] = {"pages": [], "visuals": [], "themes": []}
        deleted: dict[str, list[Any]] = {"visuals": []}
        applied = 0

        for index, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                item = {"index": index, "ok": False, "error": "Action must be an object."}
                errors.append(item)
                action_results.append(item)
                if not continue_on_error:
                    break
                continue

            op = str(action.get("op") or "")
            self.emit_progress({
                "tool": "pbir_apply_plan",
                "phase": "action_started",
                "report_root": str(report.report_root),
                "dry_run": dry_run,
                "action_index": index,
                "actions_total": len(actions),
                "op": op or None,
            })
            try:
                result = self.apply_plan_action(report, action)
                applied += 1
                compact = self.compact_plan_action_result(index, op, result)
                self.collect_plan_changes(op, compact, created, changed, deleted)
                self.emit_progress({
                    "tool": "pbir_apply_plan",
                    "phase": "action_finished",
                    "report_root": str(report.report_root),
                    "dry_run": dry_run,
                    "action_index": index,
                    "actions_total": len(actions),
                    "op": op or None,
                    "ok": True,
                })
                if include_action_results == "all":
                    action_results.append(compact)
            except Exception as exc:
                item = {"index": index, "op": op or None, "ok": False, "error": str(exc)}
                errors.append(item)
                self.emit_progress({
                    "tool": "pbir_apply_plan",
                    "phase": "action_finished",
                    "report_root": str(report.report_root),
                    "dry_run": dry_run,
                    "action_index": index,
                    "actions_total": len(actions),
                    "op": op or None,
                    "ok": False,
                    "error": str(exc),
                })
                if include_action_results in {"errors", "all"}:
                    action_results.append(item)
                if not continue_on_error:
                    break

        validation: dict[str, Any] | None = None
        if validate_after:
            validation = report.validate().as_dict()

        ok = not errors and (validation is None or bool(validation.get("ok")))
        result_payload: dict[str, Any] = {
            "ok": ok,
            "dry_run": dry_run,
            "report_root": str(report.report_root),
            "actions_requested": len(actions),
            "actions_applied": applied,
            "actions_failed": len(errors),
            "created": created,
            "changed": changed,
            "deleted": deleted,
        }
        if errors:
            result_payload["errors"] = errors
        if action_results:
            result_payload["action_results"] = action_results
        if validation is not None:
            result_payload["validation"] = validation_summary(validation, include_validation_messages or not validation.get("ok"))
        if include_summary:
            result_payload["summary"] = report.report_summary(validate=False)
        return result_payload

    @staticmethod
    def action_value(action: dict[str, Any], *names: str, default: Any = None) -> Any:
        for name in names:
            if name in action:
                return action[name]
        return default

    def apply_plan_action(self, report: PBIRReport, action: dict[str, Any]) -> dict[str, Any]:
        op = str(action.get("op") or "")
        if op == "create_page":
            display_name = self.action_value(action, "display_name", "displayName")
            if not display_name:
                raise PBIRError("create_page action requires display_name.")
            return report.create_page(
                display_name=display_name,
                name=action.get("name"),
                width=float(action.get("width", 1280)),
                height=float(action.get("height", 720)),
                display_option=action.get("display_option", action.get("displayOption", "FitToPage")),
                make_active=bool(action.get("make_active", action.get("makeActive", False))),
                background=action.get("background"),
                visibility=action.get("visibility"),
                validate_after=False,
            )
        if op == "update_page":
            display_name = self.action_value(action, "display_name", "displayName")
            width = action.get("width")
            height = action.get("height")
            display_option = self.action_value(action, "display_option", "displayOption")
            return report.update_page(
                page=action["page"],
                display_name=display_name or None,
                width=float(width) if width is not None else None,
                height=float(height) if height is not None else None,
                display_option=display_option or None,
                visibility=action.get("visibility"),
                validate_after=False,
            )
        if op == "set_active_page":
            return report.set_active_page(
                page=action["page"],
                landing=bool(action.get("landing", False)),
                validate_after=False,
            )
        if op in {"add_visual", "add_visual_from_template"}:
            return report.add_visual_from_template(
                page=action["page"],
                template=action.get("template", "card"),
                title=action.get("title"),
                name=action.get("name"),
                position=action.get("position"),
                visual_type=action.get("visual_type", action.get("visualType")),
                query_roles=action.get("query_roles", action.get("queryRoles")),
                sort=action.get("sort"),
                filter_config=action.get("filter_config", action.get("filterConfig")),
                properties=action.get("properties"),
                raw_patch=action.get("raw_patch", action.get("rawPatch")),
                validate_after=False,
            )
        if op == "set_visual_filter":
            return report.set_visual_filter(
                page=action["page"],
                visual=action["visual"],
                filters=action["filters"],
                merge=bool(action.get("merge", False)),
                validate_after=False,
            )
        if op == "set_page_filter":
            return report.set_page_filter(
                page=action["page"],
                filters=action.get("filters", []),
                validate_after=False,
            )
        if op == "clone_visual":
            return report.clone_visual(
                source_page=self.action_value(action, "source_page", "sourcePage"),
                source_visual=self.action_value(action, "source_visual", "sourceVisual"),
                target_page=self.action_value(action, "target_page", "targetPage"),
                name=action.get("name"),
                title=action.get("title"),
                position=action.get("position"),
                validate_after=False,
            )
        if op == "set_visual_position":
            return report.set_visual_position(
                page=action["page"],
                visual=action["visual"],
                position=action["position"],
                validate_after=False,
            )
        if op in {"modify_visual_properties", "patch_visual"}:
            return report.modify_visual_properties(
                page=action["page"],
                visual=action["visual"],
                patch=action["patch"],
                validate_after=False,
            )
        if op == "delete_visual":
            return report.delete_visual(action["page"], action["visual"], validate_after=False)
        if op == "recolor_visuals":
            return report.recolor_visuals(
                targets=action["targets"],
                page=action.get("page"),
                visual_types=action.get("visual_types"),
                validate_after=False,
            )
        if op == "set_page_background":
            return report.set_page_background(
                page=action["page"],
                color=action["color"],
                outspace_color=action.get("outspace_color"),
                validate_after=False,
            )
        if op == "apply_style_preset":
            preset_name = str(action.get("preset", "")).lower()
            if preset_name not in STYLE_PRESETS:
                raise PBIRError(
                    f"Unknown preset '{preset_name}'. Available: {', '.join(STYLE_PRESETS)}."
                )
            p = STYLE_PRESETS[preset_name]
            return report.generate_theme(
                primary=p["primary"],
                accent=p.get("accent"),
                background=p.get("background", "#FFFFFF"),
                text=p.get("text"),
                theme_name=p.get("theme_name", preset_name),
                page_background_transparency=int(p.get("page_background_transparency", 0)),
                good=p.get("good", "#1AAB40"),
                neutral_color=p.get("neutral", "#D9B300"),
                bad=p.get("bad", "#D64550"),
                validate_after=False,
            )
        if op == "manage_theme":
            theme_action = action.get("action") or action.get("theme_action")
            if not theme_action:
                raise PBIRError("manage_theme action requires action or theme_action.")
            clean = {
                key: value
                for key, value in action.items()
                if key not in {"op", "action", "theme_action", "project_path", "validate_after"}
            }
            return report.manage_theme(str(theme_action), validate_after=False, **clean)
        if op == "validate":
            return {"validation": report.validate().as_dict()}
        if op == "delete_page":
            return report.delete_page(action["page"], validate_after=False)
        if op == "duplicate_page":
            return report.duplicate_page(
                source_page=self.action_value(action, "source_page", "sourcePage"),
                new_display_name=self.action_value(action, "new_display_name", "newDisplayName"),
                new_name=action.get("new_name") or action.get("newName"),
                validate_after=False,
            )
        if op == "reorder_pages":
            return report.reorder_pages(action["page_order"], validate_after=False)
        if op == "set_visual_title":
            return report.set_visual_title(
                page=action["page"],
                visual=action["visual"],
                title=action.get("title"),
                show=action.get("show"),
                validate_after=False,
            )
        if op == "bulk_create_rls_roles":
            sm = PBIRSemanticModel.from_report(report.report_root)
            if sm is None:
                raise PBIRError("No SemanticModel found next to this report.")
            return sm.bulk_create_rls_roles(
                table=action["table"],
                column=action["column"],
                values=action["values"],
                role_prefix=action.get("role_prefix", ""),
                additional_tables=action.get("additional_tables"),
                on_conflict=action.get("on_conflict", "skip"),
            )
        if op == "set_table_storage_mode":
            sm = PBIRSemanticModel.from_report(report.report_root)
            if sm is None:
                raise PBIRError("No SemanticModel found next to this report.")
            return sm.set_storage_mode(
                tables=action.get("tables"),
                mode=action["mode"],
                dry_run=bool(action.get("dry_run", False)),
            )
        if op == "manage_measure":
            sm = PBIRSemanticModel.from_report(report.report_root)
            if sm is None:
                raise PBIRError("No SemanticModel found next to this report.")
            return sm.manage_measure(
                action=action["action"],
                table=action["table"],
                name=action["name"],
                dax=action.get("dax"),
                format_string=action.get("format_string"),
                display_folder=action.get("display_folder"),
                description=action.get("description"),
            )
        if op == "add_measure_from_template":
            sm = PBIRSemanticModel.from_report(report.report_root)
            if sm is None:
                raise PBIRError("No SemanticModel found next to this report.")
            return sm.add_measure_from_template(
                template=action["template"],
                table=action["table"],
                name=action["name"],
                params=action.get("params"),
                action=action.get("action", "create"),
            )
        if op == "manage_perspective":
            sm = PBIRSemanticModel.from_report(report.report_root)
            if sm is None:
                raise PBIRError("No SemanticModel found next to this report.")
            return sm.manage_perspective(
                action=action["action"],
                name=action["name"],
                tables=action.get("tables"),
            )
        if op == "manage_relationship":
            sm = PBIRSemanticModel.from_report(report.report_root)
            if sm is None:
                raise PBIRError("No SemanticModel found next to this report.")
            return sm.manage_relationship(
                action=action["action"],
                from_table=action["from_table"],
                from_column=action["from_column"],
                to_table=action["to_table"],
                to_column=action["to_column"],
                cardinality=action.get("cardinality"),
                cross_filter=action.get("cross_filter"),
                active=bool(action.get("active", True)),
            )
        if op == "manage_hierarchy":
            sm = PBIRSemanticModel.from_report(report.report_root)
            if sm is None:
                raise PBIRError("No SemanticModel found next to this report.")
            return sm.manage_hierarchy(
                action=action["action"],
                table=action["table"],
                name=action["name"],
                levels=action.get("levels"),
            )
        if op == "manage_bookmark":
            return report.manage_bookmark(
                action=action["action"],
                name=action.get("name"),
                display_name=action.get("display_name", action.get("displayName")),
                page=action.get("page"),
                visible_visuals=action.get("visible_visuals", action.get("visibleVisuals")),
                hidden_visuals=action.get("hidden_visuals", action.get("hiddenVisuals")),
                validate_after=False,
            )
        if op == "set_conditional_format":
            return report.set_conditional_format(
                page=action["page"],
                visual=action["visual"],
                target=action["target"],
                format_type=action["format_type"],
                spec=action.get("spec", {}),
                validate_after=False,
            )
        if op == "bulk_visual_op":
            return report.bulk_visual_op(
                page=action["page"],
                op=action.get("bulk_op") or action.get("inner_op", "list"),
                filter_type=action.get("filter_type"),
                filter_name=action.get("filter_name"),
                filter_bounds=action.get("filter_bounds"),
                update=action.get("update"),
                validate_after=False,
            )
        raise PBIRError(
            "Unknown plan action op. Valid ops: create_page, update_page, set_active_page, delete_page, "
            "duplicate_page, reorder_pages, add_visual, clone_visual, set_visual_position, "
            "modify_visual_properties, set_visual_title, delete_visual, recolor_visuals, "
            "set_page_background, manage_theme, apply_style_preset, bulk_create_rls_roles, "
            "set_table_storage_mode, manage_measure, manage_perspective, manage_relationship, "
            "manage_hierarchy, set_page_filter, set_visual_filter, manage_bookmark, "
            "set_conditional_format, bulk_visual_op, validate."
        )

    @staticmethod
    def compact_plan_action_result(index: int, op: str, result: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {"index": index, "op": op, "ok": True}
        page = result.get("page")
        if isinstance(page, dict):
            compact["page"] = {
                "name": page.get("name"),
                "displayName": page.get("displayName"),
                "visual_count": page.get("visual_count"),
            }
        visual = result.get("visual")
        if isinstance(visual, dict):
            compact["visual"] = {
                "page": visual.get("page"),
                "name": visual.get("name"),
                "title": visual.get("title"),
                "visualType": visual.get("visualType"),
            }
        for key in ("activePageName", "landingPageName", "deleted", "theme_path", "changed_count", "changed"):
            if key in result:
                compact[key] = result[key]
        if "themeCollection" in result:
            collection = result.get("themeCollection") or {}
            compact["theme"] = {
                name: {"name": item.get("name"), "type": item.get("type")}
                for name, item in collection.items()
                if isinstance(item, dict)
            }
        if "validation" in result:
            compact["validation"] = validation_summary(result["validation"])
        return compact

    @staticmethod
    def collect_plan_changes(
        op: str,
        result: dict[str, Any],
        created: dict[str, list[Any]],
        changed: dict[str, list[Any]],
        deleted: dict[str, list[Any]],
    ) -> None:
        page = result.get("page")
        visual = result.get("visual")
        if op == "create_page" and isinstance(page, dict):
            created["pages"].append(page.get("name"))
        elif op in {"add_visual", "add_visual_from_template", "clone_visual"} and isinstance(visual, dict):
            created["visuals"].append({"page": visual.get("page"), "name": visual.get("name")})
        elif op in {"set_visual_position", "modify_visual_properties", "patch_visual", "set_visual_filter"} and isinstance(visual, dict):
            changed["visuals"].append({"page": visual.get("page"), "name": visual.get("name")})
        elif op == "update_page" and isinstance(page, dict):
            changed["pages"].append(page.get("name"))
        elif op == "set_active_page":
            changed["pages"].append(result.get("activePageName"))
        elif op == "delete_visual" and result.get("deleted"):
            deleted["visuals"].append(result["deleted"])
        elif op in {"manage_theme", "apply_style_preset"}:
            changed["themes"].append(result.get("theme_path") or "themeCollection")
        elif op == "recolor_visuals":
            for item in result.get("changed", []):
                changed["visuals"].append({"page": item["page"], "name": item["name"]})
        elif op == "set_page_background":
            changed["pages"].append(result.get("page"))
        elif op == "delete_page":
            deleted["pages"] = deleted.get("pages", [])
            deleted["pages"].append(result.get("deleted"))
        elif op in {"duplicate_page", "reorder_pages"}:
            page_val = result.get("page")
            if isinstance(page_val, dict):
                created["pages"].append(page_val.get("name"))
            else:
                changed["pages"].append(result.get("pageOrder"))
        elif op == "set_visual_title" and isinstance(result.get("visual"), dict):
            changed["visuals"].append({"page": result["visual"].get("page"), "name": result["visual"].get("name")})
        elif op == "bulk_create_rls_roles":
            created.setdefault("roles", []).extend(result.get("created", []))

    def tool_create_project(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        report = PBIRReport.scaffold_project(
            folder=args["folder"],
            name=name,
            initial_page_display_name=args.get("initial_page_display_name", "Overview"),
            initial_page_name=args.get("initial_page_name"),
            page_width=float(args.get("page_width", 1280)),
            page_height=float(args.get("page_height", 720)),
        )
        self.current_report = report
        folder = Path(args["folder"]).expanduser().resolve()
        return {
            "pbip_path": str(folder / f"{name}.pbip"),
            "report_root": str(report.report_root),
            "semantic_model_path": str(folder / f"{name}.SemanticModel"),
            "validation": validation_summary(report.validate().as_dict(), include_messages=True),
            "next_step": (
                "IMPORTANT: all subsequent tool calls must pass project_path=report_root to identify this project. "
                "Use powerbi-modeling-mcp connection_operations/Connect then "
                "table_operations/Create + measure_operations/Create, then "
                "database_operations/ExportToTmdlFolder targeting the semantic_model_path above. "
                "After that call read_semantic_model (with project_path=report_root) to get entity names, "
                "then pbir_apply_plan to add visuals."
            ),
        }

    def tool_export_project_zip(self, args: dict[str, Any]) -> dict[str, Any]:
        from urllib.parse import quote
        result = self.report(args).export_project(
            output_dir=args.get("output_dir"),
            zip_path=None,
            keep_folder=bool(args.get("keep_folder", True)),
        )
        zip_path = result.get("zip_path", "")
        if zip_path:
            filename = Path(zip_path).name
            result["download_url"] = f"/api/hermes/download?path={quote(zip_path)}&name={quote(filename)}"
            result["message"] = (
                f"Project exported to {zip_path} ({result.get('size_kb', '?')} KB). "
                f"Present the download_url as a clickable markdown link so the user can download the zip: "
                f"[Download {filename}]({result['download_url']})"
            )
        return result

    def tool_generate_theme(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.report(args).generate_theme(
            primary=args["primary"],
            accent=args.get("accent"),
            background=args.get("background", "#FFFFFF"),
            text=args.get("text"),
            theme_name=args.get("theme_name", "Custom"),
            page_background_transparency=int(args.get("page_background_transparency", 0)),
            good=args.get("good", "#1AAB40"),
            neutral_color=args.get("neutral", "#D9B300"),
            bad=args.get("bad", "#D64550"),
            validate_after=bool(args.get("validate_after", False)),
        )

    def _run_source_query(
        self,
        server: str,
        database: str,
        sql: str,
        preview: bool = False,
        max_rows: int = 200,
    ) -> dict[str, Any]:
        try:
            import pyodbc
        except ImportError:
            raise PBIRError("pyodbc is not installed. Run: pip install pyodbc")

        limit = 10 if preview else max_rows
        conn_str = f"DRIVER={{SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
        try:
            conn = pyodbc.connect(conn_str, timeout=15)
        except Exception as e:
            raise PBIRError(f"Could not connect to {server}/{database}: {e}")

        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            rows = cursor.fetchmany(limit)
        except Exception as e:
            raise PBIRError(f"Query failed: {e}")
        finally:
            conn.close()

        truncated = len(rows) == limit
        if len(columns) == 1:
            # Compact flat list for single-column queries (e.g. SELECT DISTINCT col)
            return {
                "values": [row[0] for row in rows],
                "row_count": len(rows),
                "truncated": truncated,
            }
        return {
            "columns": columns,
            "rows": [list(row) for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
        }

    # ── Python execution & custom tools ───────────────────────────────────────

    def _build_exec_context(self, args: dict[str, Any]) -> dict[str, Any]:
        """Build the globals dict available to AI-authored scripts."""
        import re as _re
        import json as _json
        ctx: dict[str, Any] = {
            # stdlib
            "re": _re,
            "json": _json,
            "Path": Path,
            # PBIR helpers
            "read_json": read_json,
            "write_json": write_json,
            "_color_expr": _color_expr,
            "_get_color_expr": _get_color_expr,
            "_objects_item_props": _objects_item_props,
            "_solid": _solid,
            "deep_merge": deep_merge,
            "validation_summary": validation_summary,
            # classes
            "PBIRReport": PBIRReport,
            "PBIRSemanticModel": PBIRSemanticModel,
            "PBIRError": PBIRError,
            # args passed to the script
            "args": args,
            # return slot — script sets this
            "result": None,
        }
        project_path = args.get("project_path")
        if project_path:
            ctx["report"] = PBIRReport(project_path)
            sm = PBIRSemanticModel.from_report(ctx["report"].report_root)
            if sm:
                ctx["sm"] = sm
        return ctx

    def _run_python_code(self, code: str, ctx: dict[str, Any]) -> Any:
        try:
            exec(compile(code, "<ai_script>", "exec"), ctx)
        except Exception as exc:
            tb = traceback.format_exc()
            raise PBIRError(f"Script error: {exc}\n{tb}") from exc
        return ctx.get("result")

    def _run_python_tool(self, code: str, ctx: dict[str, Any]) -> Any:
        """run_python tool handler — captures stdout alongside the result."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = self._run_python_code(code, ctx)
        captured = buf.getvalue()
        if captured:
            return {"result": result, "stdout": captured.rstrip("\n")}
        return result

    def _save_and_register_custom_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        name = normalize_object_name(args["name"], "tool name")
        defn = {
            "name": name,
            "description": args["description"],
            "code": args["code"],
            "schema": args.get("schema", {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
                "additionalProperties": True,
            }),
        }
        CUSTOM_TOOLS_DIR.mkdir(exist_ok=True)
        path = CUSTOM_TOOLS_DIR / f"{name}.json"
        path.write_text(json.dumps(defn, indent=2, ensure_ascii=False), encoding="utf-8")
        self._register_custom_tool_defn(defn)
        return {"saved": str(path), "name": name, "registered": True}

    def _list_custom_tools(self) -> dict[str, Any]:
        if not CUSTOM_TOOLS_DIR.exists():
            return {"tools": []}
        tools = []
        for path in sorted(CUSTOM_TOOLS_DIR.glob("*.json")):
            try:
                defn = json.loads(path.read_text(encoding="utf-8"))
                tools.append({"name": defn.get("name", path.stem), "description": defn.get("description", "")})
            except Exception:
                tools.append({"name": path.stem, "description": "(unreadable)"})
        return {"tools": tools}

    def _delete_custom_tool(self, name: str) -> dict[str, Any]:
        normalize_object_name(name, "tool name")
        path = CUSTOM_TOOLS_DIR / f"{name}.json"
        if not path.exists():
            raise PBIRError(f"Custom tool '{name}' not found.")
        path.unlink()
        if name in self.tools:
            del self.tools[name]
        return {"deleted": name}

    def _load_custom_tools(self) -> None:
        if not CUSTOM_TOOLS_DIR.exists():
            return
        for path in sorted(CUSTOM_TOOLS_DIR.glob("*.json")):
            try:
                defn = json.loads(path.read_text(encoding="utf-8"))
                self._register_custom_tool_defn(defn)
            except Exception:
                pass  # bad file — skip silently on startup

    def _register_custom_tool_defn(self, defn: dict[str, Any]) -> None:
        name = defn["name"]
        code = defn["code"]
        schema = defn.get("schema", {"type": "object", "properties": {"project_path": {"type": "string"}}, "additionalProperties": True})

        def handler(args: dict[str, Any], _code: str = code) -> Any:
            ctx = self._build_exec_context(args)
            return self._run_python_code(_code, ctx)

        self.add_tool(name, defn.get("description", f"Custom tool: {name}"), schema, handler)

    def tool_load_project(self, args: dict[str, Any]) -> dict[str, Any]:
        self.current_report = PBIRReport(args["project_path"])
        validation = self.current_report.validate().as_dict()
        return {
            "report_root": str(self.current_report.report_root),
            "definition_dir": str(self.current_report.definition_dir),
            "validation": validation_summary(validation, include_messages=True),
        }

    def tool_list_projects(self, args: dict[str, Any]) -> dict[str, Any]:
        root = Path(args["search_root"]).expanduser().resolve()
        if not root.exists():
            raise PBIRError(f"Directory does not exist: {root}")
        max_depth = int(args.get("max_depth", 4))
        projects: list[dict[str, Any]] = []

        def scan(path: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_file() and child.suffix.lower() == ".pbip":
                        try:
                            version = read_json(child).get("version", "?")
                        except PBIRError:
                            version = "?"
                        projects.append({"name": child.stem, "path": str(child), "version": version})
                    elif child.is_dir() and not child.name.startswith("."):
                        scan(child, depth + 1)
            except PermissionError:
                pass

        scan(root, 0)
        return {"search_root": str(root), "project_count": len(projects), "projects": projects}

    def tool_create_page(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.report(args).create_page(
            display_name=args["display_name"],
            name=args.get("name"),
            width=float(args.get("width", 1280)),
            height=float(args.get("height", 720)),
            display_option=args.get("display_option", "FitToPage"),
            make_active=bool(args.get("make_active", False)),
            background=args.get("background"),
            visibility=args.get("visibility"),
        )

    def tool_add_visual(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.report(args).add_visual_from_template(
            page=args["page"],
            template=args.get("template", "card"),
            title=args.get("title"),
            name=args.get("name"),
            position=args.get("position"),
            visual_type=args.get("visual_type"),
            query_roles=args.get("query_roles"),
            sort=args.get("sort"),
            filter_config=args.get("filter_config"),
            properties=args.get("properties"),
            raw_patch=args.get("raw_patch"),
        )

    def tool_clone_visual(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.report(args).clone_visual(
            source_page=args["source_page"],
            source_visual=args["source_visual"],
            target_page=args["target_page"],
            name=args.get("name"),
            title=args.get("title"),
            position=args.get("position"),
        )

    def tool_manage_theme(self, args: dict[str, Any]) -> dict[str, Any]:
        clean = {key: value for key, value in args.items() if key != "project_path"}
        return self.report(args).manage_theme(**clean)

    def handle_request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            params = message.get("params") or {}
            protocol_version = params.get("protocolVersion", "2024-11-05")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "Use this server to edit Power BI PBIR report definition folders. "
                        "Step 1: load a .pbip, .pbir, .Report, or definition folder path. "
                        "Step 2: call get_report_context for a full overview of pages, visuals, and theme before making any changes. "
                        "Step 3: batch all edits with pbir_apply_plan (validates once at the end) — avoid separate single-tool calls when multiple changes are needed. "
                        "Step 4: validate with validate_report before asking the user to reopen in Power BI Desktop. "
                        "Use run_python for data exploration or custom calculations not covered by the built-in tools."
                    ),
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            tools = []
            for name, (description, input_schema, _) in self.tools.items():
                tools.append({"name": name, "description": description, "inputSchema": input_schema})
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in self.tools:
                return {"jsonrpc": "2.0", "id": request_id, "result": error_result(f"Unknown tool: {name}")}
            try:
                result = self.tools[name][2](args)
                return {"jsonrpc": "2.0", "id": request_id, "result": text_result(result)}
            except Exception as exc:  # MCP tools should report errors as tool content.
                return {"jsonrpc": "2.0", "id": request_id, "result": error_result(str(exc))}
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


_TRANSPORT_MODE = "jsonl"


def read_message() -> dict[str, Any] | None:
    global _TRANSPORT_MODE
    buffer = sys.stdin.buffer
    line = buffer.readline()
    while line in (b"\r\n", b"\n"):
        line = buffer.readline()
    if line == b"":
        return None

    stripped = line.strip()
    if not stripped:
        return None

    if not stripped.lower().startswith(b"content-length:"):
        _TRANSPORT_MODE = "jsonl"
        return json.loads(stripped.decode("utf-8"))

    _TRANSPORT_MODE = "content-length"
    headers: dict[str, str] = {}
    while True:
        key, _, value = stripped.decode("utf-8").partition(":")
        headers[key.lower()] = value.strip()
        line = buffer.readline()
        if line == b"":
            return None
        stripped = line.strip()
        if not stripped:
            break
    length_raw = headers.get("content-length")
    if not length_raw:
        raise PBIRError("Missing Content-Length header.")
    body = buffer.read(int(length_raw))
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def send_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if _TRANSPORT_MODE == "content-length":
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header + body)
    else:
        sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


def run() -> None:
    server = PBIRMCPServer()
    while True:
        try:
            message = read_message()
            if message is None:
                break
            response = server.handle_request(message)
            if response is not None:
                send_message(response)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            send_message(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": str(exc)},
                }
            )


def run_sse(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run a small HTTP/SSE transport for local MCP-style JSON-RPC calls.

    GET  /events  opens an event stream.
    POST /message accepts one JSON-RPC message and returns its JSON-RPC response.
    """
    import http.server
    import queue
    import threading
    import urllib.parse

    mcp_server = PBIRMCPServer()
    subscribers: list[queue.Queue[str]] = []

    def publish(event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        for q in list(subscribers):
            q.put(payload)

    mcp_server.progress_callback = publish

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(fmt % args, file=sys.stderr)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/events":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q: queue.Queue[str] = queue.Queue()
            subscribers.append(q)
            try:
                self.wfile.write(b"event: ready\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        payload = q.get(timeout=15)
                        self.wfile.write(f"event: message\ndata: {payload}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError):
                pass
            finally:
                if q in subscribers:
                    subscribers.remove(q)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/message":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                message = json.loads(self.rfile.read(length).decode("utf-8"))
                publish({"phase": "received", "method": message.get("method"), "id": message.get("id")})
                response = mcp_server.handle_request(message)
                body = json.dumps(response or {}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                publish({"phase": "completed", "method": message.get("method"), "id": message.get("id")})
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    if len(sys.argv) > 1:
        host = sys.argv[1]
    if len(sys.argv) > 2:
        port = int(sys.argv[2])
    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"{SERVER_NAME} SSE transport listening on http://{host}:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
