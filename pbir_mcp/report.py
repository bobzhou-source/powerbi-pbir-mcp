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
from pathlib import Path
from typing import Any, Callable

from .semantic_model import PBIRSemanticModel
from .shared import *


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
        local = self.report_root.parent / f".{self.report_root.name}.pbir_mcp_journal"
        # Deep Desktop/project paths can make the undo snapshot path exceed
        # Windows MAX_PATH once nested PBIR folders are copied into the journal.
        if len(str(local)) <= 140:
            return local
        digest = uuid.uuid5(uuid.NAMESPACE_URL, str(self.report_root.resolve())).hex[:12]
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", self.report_root.name)[:32].strip("_") or "report"
        return Path(tempfile.gettempdir()) / "pbir_mcp_journal" / f"{safe_name}_{digest}"

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
            "journal_dir": str(root),
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

    def read_semantic_model(
        self,
        summary: bool = False,
        max_columns_per_table: int = 20,
        include_system: bool = True,
    ) -> dict[str, Any]:
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
                    if not include_system and parsed.get("name", "").startswith(("LocalDateTable_", "DateTableTemplate_")):
                        continue
                    tables.append(parsed)
            except Exception as exc:
                tables.append({"name": tmdl_file.stem, "error": str(exc), "columns": [], "measures": []})
        if summary:
            limit = max(0, int(max_columns_per_table))
            compact_tables: list[dict[str, Any]] = []
            for table in tables:
                columns = table.get("columns") or []
                measures = table.get("measures") or []
                entry: dict[str, Any] = {
                    "name": table.get("name"),
                    "column_count": len(columns),
                    "measure_count": len(measures),
                    "sample_columns": [
                        {"name": col.get("name"), "dataType": col.get("dataType")}
                        for col in columns[:limit]
                    ],
                    "measures": [measure.get("name") for measure in measures],
                }
                if len(columns) > limit:
                    entry["columns_truncated"] = len(columns) - limit
                if table.get("source_hint"):
                    entry["source_hint"] = table.get("source_hint")
                if table.get("error"):
                    entry["error"] = table.get("error")
                compact_tables.append(entry)
            return {"semantic_model_path": str(sm_path), "table_count": len(compact_tables), "tables": compact_tables}
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
