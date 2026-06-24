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
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


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


def truncate_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n... [truncated {len(value) - max_chars} chars]"


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
    elif warnings:
        result["first_warning"] = warnings[0]
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

__all__ = [name for name in globals() if not name.startswith("__")]
