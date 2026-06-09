#!/usr/bin/env python
"""MCP server for editing Power BI PBIR report definitions.

The server intentionally uses only the Python standard library so Codex can
launch it without a package install step.
"""

from __future__ import annotations

import copy
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
SERVER_VERSION = "0.1.0"

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
        known = ", ".join(data.get("displayName") or data.get("name") or p.name for p, data in candidates)
        raise PBIRError(f"Page not found: {page}. Known pages: {known}")

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
        known = ", ".join(sorted({item for item in candidates if item}))
        raise PBIRError(f"Visual not found: {visual}. Known visuals: {known}")

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

    def list_visuals(self, page: str | None = None, _include_paths: bool = False) -> dict[str, Any]:
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
        packages = report.setdefault("resourcePackages", [])
        package = None
        for candidate in packages:
            if candidate.get("name") == "RegisteredResources" and candidate.get("type") == "RegisteredResources":
                package = candidate
                break
        if package is None:
            package = {"name": "RegisteredResources", "type": "RegisteredResources", "items": []}
            packages.append(package)
        items = package.setdefault("items", [])
        path = f"RegisteredResources/{file_name}"
        existing = next((item for item in items if item.get("name") == item_name), None)
        payload = {"name": item_name, "path": path, "type": "CustomTheme"}
        if existing:
            existing.update(payload)
        else:
            items.append(payload)

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


class PBIRMCPServer:
    def __init__(self) -> None:
        self.current_report: PBIRReport | None = None
        self.tools: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], Any]]] = {}
        self.register_tools()

    def report(self, args: dict[str, Any]) -> PBIRReport:
        path = args.get("project_path")
        if path:
            self.current_report = PBIRReport(path)
            return self.current_report
        if self.current_report is None:
            raise PBIRError("No PBIR project is loaded. Call load_project or pass project_path.")
        return self.current_report

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
            "Batch multiple page/visual/theme actions in one call, validating once at the end. Prefer over individual tools when making several changes. Supported ops: create_page, update_page, set_active_page, add_visual, clone_visual, set_visual_position, modify_visual_properties, delete_visual, manage_theme, apply_style_preset, validate.",
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
                            "include_summary": {"type": "boolean", "default": True},
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
            "List visuals across the report or on a single page.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "page": {"type": "string"},
                },
                "additionalProperties": False,
            },
            lambda args: self.report(args).list_visuals(args.get("page")),
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
        include_summary = bool(response.get("include_summary", True))
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
        raise PBIRError(
            "Unknown plan action op. Use create_page, update_page, set_active_page, add_visual, clone_visual, "
            "set_visual_position, modify_visual_properties, delete_visual, manage_theme, apply_style_preset, or validate."
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
        for key in ("activePageName", "landingPageName", "deleted", "theme_path"):
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
                        "Load a PBIP, PBIR, .Report, or definition folder first. "
                        "For multiple changes use pbir_apply_plan to batch all actions and validate once. "
                        "For state overview use pbir_report_summary instead of list_pages+list_visuals. "
                        "Validate after edits before reopening in Power BI Desktop."
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
