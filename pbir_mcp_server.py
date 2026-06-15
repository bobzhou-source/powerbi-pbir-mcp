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
    "card": "card", "cardNew": "card", "slicer": "slicer",
    "tableEx": "table", "pivotTable": "pivot",
    "lineChart": "line", "areaChart": "area",
    "clusteredBarChart": "bar", "barChart": "bar", "stackedBarChart": "bar",
    "clusteredColumnChart": "col", "columnChart": "col", "stackedColumnChart": "col",
    "lineClusteredColumnComboChart": "combo", "lineStackedColumnComboChart": "combo",
    "donutChart": "donut", "pieChart": "pie", "treemap": "tree",
    "filledMap": "map", "map": "map", "shapeMap": "map",
    "funnel": "funnel", "gauge": "gauge", "kpi": "kpi",
    "scatterChart": "scatter", "waterfallChart": "waterfall",
    "ribbonChart": "ribbon", "multiRowCard": "mcard",
    "textbox": "text", "image": "img", "shape": "shape",
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
    return result


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
        visual_json: dict[str, Any] = {
            "$schema": VISUAL_CONTAINER_SCHEMA,
            "name": visual_name,
            "position": scalar_position(position, self.next_visual_z(page_dir)),
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
        if query_roles:
            visual_config["query"] = self.build_query(query_roles)
        if properties:
            visual_config.setdefault("objects", {})
            deep_merge(visual_config["objects"], properties)
        if raw_patch:
            deep_merge(visual_json, raw_patch)

        visual_dir.mkdir(parents=True, exist_ok=False)
        write_json(visual_dir / "visual.json", visual_json)
        result: dict[str, Any] = {"visual": self.visual_summary(page_dir, visual_dir)}
        if validate_after:
            result["validation"] = validation_summary(self.validate().as_dict())
        return result

    @staticmethod
    def visual_type_for_template(template: str) -> str:
        mapping = {
            "blank": "card",
            "card": "card",
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
    def build_query(query_roles: dict[str, Any]) -> dict[str, Any]:
        query_state: dict[str, Any] = {}
        for role, projections in query_roles.items():
            if not isinstance(projections, list):
                projections = [projections]
            query_state[role] = {
                "projections": [PBIRReport.build_projection(projection) for projection in projections]
            }
        return {"queryState": query_state}

    @staticmethod
    def build_projection(spec: dict[str, Any]) -> dict[str, Any]:
        if "raw" in spec:
            raw = copy.deepcopy(spec["raw"])
            if "queryRef" not in raw:
                raise PBIRError("Raw projection must include queryRef.")
            return raw
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
        else:
            raise PBIRError("Projection must include measure or column.")
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
        for child in visual_dir.iterdir():
            if child.is_file():
                child.unlink()
        visual_dir.rmdir()
        result: dict[str, Any] = {"deleted": str(visual_dir)}
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
        # Remove any stale RegisteredResources package entries we may have written previously.
        packages = report.get("resourcePackages")
        if packages:
            report["resourcePackages"] = [p for p in packages if p.get("type") != "RegisteredResources"]

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


class PBIRSemanticModel:
    """Read/write RLS roles in a PBIP SemanticModel (TMDL format)."""

    def __init__(self, sm_root: Path) -> None:
        self.sm_root = sm_root
        self.roles_dir = sm_root / "definition" / "roles"
        self.model_tmdl = sm_root / "definition" / "model.tmdl"

    @classmethod
    def from_report(cls, report_root: Path) -> "PBIRSemanticModel | None":
        """Locate the sibling .SemanticModel folder from a .Report folder."""
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
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("role "):
                name = s[5:].strip().strip("'")
            elif s.startswith("modelPermission:"):
                model_perm = s.split(":", 1)[1].strip()
            elif s.startswith("tablePermission "):
                rest = s[len("tablePermission "):]
                # rest: 'TableName' = <dax>
                if "' = " in rest:
                    tbl, dax = rest.split("' = ", 1)
                    table_permissions.append({"table": tbl.strip().strip("'"), "dax": dax.strip()})
                elif '" = ' in rest:
                    tbl, dax = rest.split('" = ', 1)
                    table_permissions.append({"table": tbl.strip().strip('"'), "dax": dax.strip()})
            elif s.startswith("annotation PBI_Id"):
                pbi_id = s.split("=", 1)[1].strip()
        return {"name": name, "modelPermission": model_perm or "read",
                "tablePermissions": table_permissions, "_pbi_id": pbi_id}

    def _write_role_file(
        self,
        name: str,
        table_permissions: list[dict[str, str]],
        model_permission: str = "read",
        pbi_id: str | None = None,
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
            lines.append(f"\ttablePermission {self._tmdl_name(tp['table'])} = {tp['dax']}")
        lines += ["", f"\tannotation PBI_Id = {pbi_id}", ""]
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
            if not table_permissions:
                raise PBIRError("table_permissions is required for create/update.")
            # Preserve existing PBI_Id on update
            pbi_id: str | None = None
            existing = self.roles_dir / f"{name}.tmdl"
            if action == "update" and existing.exists():
                pbi_id = self._parse_role_file(existing).get("_pbi_id")
            path = self._write_role_file(name, table_permissions, model_permission, pbi_id=pbi_id)
            self._update_model_ref(name, add=True)
            return {
                "action": action + "d",
                "name": name,
                "modelPermission": model_permission,
                "tablePermissions": table_permissions,
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
            perms = [{"table": table, "dax": f'[{column}] = "{value}"'}]
            if additional_tables:
                perms.extend(additional_tables)
            existing = self.roles_dir / f"{role_name}.tmdl" if self.roles_dir.exists() else None
            if existing and existing.exists():
                if on_conflict == "skip":
                    skipped.append(role_name)
                    continue
                self.manage_role("update", role_name, perms)
                overwritten.append(role_name)
            else:
                self.manage_role("create", role_name, perms)
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


CUSTOM_TOOLS_DIR = Path(__file__).parent / "custom_tools"


class PBIRMCPServer:
    def __init__(self) -> None:
        self.current_report: PBIRReport | None = None
        self.tools: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], Any]]] = {}
        self.register_tools()
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
        sm = PBIRSemanticModel.from_report(report.report_root)
        if sm is None:
            raise PBIRError(
                f"No SemanticModel found next to {report.report_root}. "
                "Expected a sibling folder with the same base name ending in .SemanticModel."
            )
        return sm

    def add_tool(self, name: str, description: str, input_schema: dict[str, Any], fn: Callable[[dict[str, Any]], Any]) -> None:
        self.tools[name] = (description, input_schema, fn)

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
                "required": ["primary"],
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
                "required": ["targets"],
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
                "required": ["page", "color"],
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
                "additionalProperties": False,
            },
            lambda args: {"roles": self.semantic_model(args).list_roles()},
        )
        self.add_tool(
            "manage_rls_role",
            (
                "Create, update, or delete a Row-Level Security role in the SemanticModel. "
                "action=create: writes a new role TMDL file and registers it in model.tmdl. "
                "action=update: overwrites an existing role (preserves its PBI_Id). "
                "action=delete: removes the role file and its model.tmdl reference. "
                "table_permissions is a list of {table, dax} objects where dax is the DAX filter "
                "expression (e.g. '[ClientName] = \"Acme\"' or '[Region] IN {\"West\", \"East\"}'). "
                "Multiple entries restrict different tables within the same role. "
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
                                "dax": {"type": "string", "description": "DAX filter expression."},
                            },
                            "required": ["table", "dax"],
                        },
                        "description": "Required for create/update. Omit for delete.",
                    },
                    "model_permission": {
                        "type": "string",
                        "enum": ["read", "readRefresh", "readWrite", "admin", "none"],
                        "default": "read",
                        "description": "Permission level. RLS roles are almost always 'read'.",
                    },
                },
                "required": ["action", "name"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).manage_role(
                action=args["action"],
                name=args["name"],
                table_permissions=args.get("table_permissions"),
                model_permission=args.get("model_permission", "read"),
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
                            "required": ["table", "dax"],
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
                "required": ["table", "column", "values"],
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
                "required": ["table"],
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
                "required": ["table", "column"],
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
                "required": ["tables", "mode"],
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
                "additionalProperties": False,
            },
            lambda args: {
                "relationships": self.semantic_model(args).get_relationships(
                    bool(args.get("include_date_tables", False))
                )
            },
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
                "required": ["page"],
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
                "required": ["source_page", "new_display_name"],
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
                "required": ["page_order"],
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
                "required": ["page", "visual"],
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
                "required": ["code"],
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
            "Return the currently loaded PBIR report project path.",
            schema(),
            lambda args: {"report_root": str(self.current_report.report_root) if self.current_report else None},
        )
        self.add_tool(
            "validate_report",
            "Run full structural validation. Returns all errors and warnings.",
            {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
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
                "required": ["query"],
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
                "required": ["search_root"],
                "additionalProperties": False,
            },
            self.tool_list_projects,
        )
        self.add_tool(
            "pbir_apply_plan",
            "Batch multiple page/visual/theme/role/model actions in one call, validating once at the end. Prefer over individual tools when making several changes. Supported ops: create_page, update_page, delete_page, duplicate_page, reorder_pages, set_active_page, add_visual, clone_visual, set_visual_position, set_visual_title, modify_visual_properties, delete_visual, manage_theme, apply_style_preset, recolor_visuals, set_page_background, bulk_create_rls_roles, set_table_storage_mode, validate.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False},
                    "validate_after": {"type": "boolean", "default": True},
                    "continue_on_error": {"type": "boolean", "default": False},
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
                "required": ["page", "visual"],
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
                },
                "required": ["display_name"],
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
                "required": ["page"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).set_active_page(args["page"], bool(args.get("landing", False))),
        )
        self.add_tool(
            "add_visual_from_template",
            "Add a visual. Use pbir_apply_plan when adding multiple visuals.",
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
                    "query_roles": {"type": "object"},
                    "properties": {"type": "object"},
                    "raw_patch": {"type": "object"},
                },
                "required": ["page"],
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
                "required": ["source_page", "source_visual", "target_page"],
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
                "required": ["page", "visual", "position"],
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
                "required": ["page", "visual", "patch"],
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
                "required": ["page", "visual"],
                "additionalProperties": False,
            },
            lambda args: self.report(args).delete_visual(args["page"], args["visual"]),
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
                "required": ["action"],
                "additionalProperties": False,
            },
            self.tool_manage_theme,
        )

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
        if dry_run:
            with tempfile.TemporaryDirectory(prefix="pbir-plan-") as temp:
                temp_report_root = Path(temp) / report.report_root.name
                shutil.copytree(report.report_root, temp_report_root)
                working_report = PBIRReport(temp_report_root)
                result = self.apply_plan_to_report(working_report, args, dry_run=True)
                result["source_report_root"] = str(report.report_root)
                return result
        return self.apply_plan_to_report(report, args, dry_run=False)

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
            try:
                result = self.apply_plan_action(report, action)
                applied += 1
                compact = self.compact_plan_action_result(index, op, result)
                self.collect_plan_changes(op, compact, created, changed, deleted)
                if include_action_results == "all":
                    action_results.append(compact)
            except Exception as exc:
                item = {"index": index, "op": op or None, "ok": False, "error": str(exc)}
                errors.append(item)
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
                properties=action.get("properties"),
                raw_patch=action.get("raw_patch", action.get("rawPatch")),
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
        raise PBIRError(
            "Unknown plan action op. Valid ops: create_page, update_page, set_active_page, delete_page, "
            "duplicate_page, reorder_pages, add_visual, clone_visual, set_visual_position, "
            "modify_visual_properties, set_visual_title, delete_visual, recolor_visuals, "
            "set_page_background, manage_theme, apply_style_preset, bulk_create_rls_roles, "
            "set_table_storage_mode, validate."
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
        elif op in {"set_visual_position", "modify_visual_properties", "patch_visual"} and isinstance(visual, dict):
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
                "Use powerbi-modeling-mcp connection_operations/Connect then "
                "table_operations/Create + measure_operations/Create, then "
                "database_operations/ExportToTmdlFolder targeting the semantic_model_path above. "
                "After that call read_semantic_model to get entity names, then pbir_apply_plan to add visuals."
            ),
        }

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
        elif self.current_report:
            ctx["report"] = self.current_report
            sm = PBIRSemanticModel.from_report(self.current_report.report_root)
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
                        "Step 4: validate with pbir_validate_report before asking the user to reopen in Power BI Desktop. "
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


if __name__ == "__main__":
    run()
