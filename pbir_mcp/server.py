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

from .report import PBIRReport
from .semantic_model import PBIRSemanticModel
from .shared import *


CUSTOM_TOOLS_DIR = Path(__file__).resolve().parent.parent / "custom_tools"


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
                "server, database, the primary source table/view name, query length, "
                "and the list of columns defined in the model. Set include_sql=true only when "
                "you need the full partition SQL/DAX expression. "
                "Use this before run_source_query to discover the server/database and build an "
                "efficient query (e.g. SELECT DISTINCT on the source_table directly rather than "
                "wrapping the full partition query)."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "table": {"type": "string", "description": "Semantic model table name (e.g. 'Suspend Comments')."},
                    "include_sql": {"type": "boolean", "default": False},
                    "max_sql_chars": {"type": "integer", "default": 4000},
                },
                "required": ["project_path", "table"],
                "additionalProperties": False,
            },
            lambda args: self.semantic_model(args).get_table_source(
                args["table"],
                include_sql=bool(args.get("include_sql", False)),
                max_sql_chars=int(args.get("max_sql_chars", 4000)),
            ),
        )
        self.add_tool(
            "run_source_query",
            (
                "Run a SQL query directly against a SQL Server data source using Windows Authentication. "
                "Use preview=true to fetch only 10 rows (fast check before committing to a full query). "
                "Use mode='summary' to return only columns, row_count, truncated, and one sample row. "
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
                    "mode": {
                        "type": "string",
                        "enum": ["rows", "summary"],
                        "default": "rows",
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
                mode=str(args.get("mode", "rows")),
            ),
        )
        self.add_tool(
            "find_source_tables",
            (
                "Compact SQL Server schema discovery. Finds tables/views by keyword without dumping broad "
                "INFORMATION_SCHEMA results. Use before describe_source_tables."
            ),
            {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "database": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "default": []},
                    "max_results": {"type": "integer", "default": 50},
                },
                "required": ["server", "database"],
                "additionalProperties": False,
            },
            lambda args: self._find_source_tables(
                server=args["server"],
                database=args["database"],
                keywords=args.get("keywords") or [],
                max_results=min(int(args.get("max_results", 50)), 500),
            ),
        )
        self.add_tool(
            "describe_source_tables",
            (
                "Compact SQL Server column discovery for explicit tables. Pass table names as "
                "'schema.table' or 'table'. Returns grouped columns and counts."
            ),
            {
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "database": {"type": "string"},
                    "tables": {"type": "array", "items": {"type": "string"}},
                    "max_columns_per_table": {"type": "integer", "default": 100},
                },
                "required": ["server", "database", "tables"],
                "additionalProperties": False,
            },
            lambda args: self._describe_source_tables(
                server=args["server"],
                database=args["database"],
                tables=args["tables"],
                max_columns_per_table=int(args.get("max_columns_per_table", 100)),
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
            "Run structural validation. Returns compact counts by default; set include_messages=true for all errors/warnings.",
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "include_messages": {"type": "boolean", "default": False},
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: validation_summary(
                self.report(args).validate().as_dict(),
                include_messages=bool(args.get("include_messages", False)),
            ),
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
            (
                "Parse the SemanticModel referenced by the loaded report. "
                "Use summary=true first for a compact table/field overview; use summary=false only "
                "when you need every column/measure detail before add_visual_from_template."
            ),
            {
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "summary": {"type": "boolean", "default": True},
                    "max_columns_per_table": {"type": "integer", "default": 20},
                    "include_system": {"type": "boolean", "default": False},
                },
                                "required": ["project_path"],
"additionalProperties": False,
            },
            lambda args: self.report(args).read_semantic_model(
                summary=bool(args.get("summary", True)),
                max_columns_per_table=int(args.get("max_columns_per_table", 20)),
                include_system=bool(args.get("include_system", False)),
            ),
        )
        self.add_tool(
            "list_projects",
            (
                "Scan a directory recursively for Power BI .pbip project files. Returns name, path, "
                "and version for each project found. By default skips staged export folders created "
                "beside an already-listed .pbip to avoid duplicate clone candidates."
            ),
            {
                "type": "object",
                "properties": {
                    "search_root": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 4},
                    "exclude_export_folders": {"type": "boolean", "default": True},
                },
                "required": ["search_root"],
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

    @staticmethod
    def _connect_sql_server(server: str, database: str):
        try:
            import pyodbc
        except ImportError:
            raise PBIRError("pyodbc is not installed. Run: pip install pyodbc")
        conn_str = f"DRIVER={{SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
        try:
            return pyodbc.connect(conn_str, timeout=15)
        except Exception as exc:
            raise PBIRError(f"Could not connect to {server}/{database}: {exc}") from exc

    def _find_source_tables(
        self,
        server: str,
        database: str,
        keywords: list[str],
        max_results: int = 50,
    ) -> dict[str, Any]:
        clean_keywords = [str(k).strip().lower() for k in keywords if str(k).strip()]
        where = ["TABLE_TYPE IN ('BASE TABLE', 'VIEW')"]
        params: list[Any] = []
        if clean_keywords:
            keyword_clauses = []
            for keyword in clean_keywords:
                keyword_clauses.append("(LOWER(TABLE_SCHEMA) LIKE ? OR LOWER(TABLE_NAME) LIKE ?)")
                like = f"%{keyword}%"
                params.extend([like, like])
            where.append("(" + " OR ".join(keyword_clauses) + ")")
        sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
            "FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        )
        limit = max(1, int(max_results))
        conn = self._connect_sql_server(server, database)
        try:
            cursor = conn.cursor()
            cursor.execute(sql, *params)
            rows = cursor.fetchmany(limit + 1)
        except Exception as exc:
            raise PBIRError(f"Schema query failed: {exc}") from exc
        finally:
            conn.close()
        returned = rows[:limit]
        return {
            "server": server,
            "database": database,
            "keywords": clean_keywords,
            "table_count": len(returned),
            "truncated": len(rows) > limit,
            "tables": [
                {
                    "schema": row[0],
                    "name": row[1],
                    "full_name": f"{row[0]}.{row[1]}",
                    "type": row[2],
                }
                for row in returned
            ],
        }

    def _describe_source_tables(
        self,
        server: str,
        database: str,
        tables: list[str],
        max_columns_per_table: int = 100,
    ) -> dict[str, Any]:
        requested = [str(t).strip() for t in tables if str(t).strip()]
        if not requested:
            raise PBIRError("describe_source_tables requires at least one table.")
        clauses: list[str] = []
        params: list[Any] = []
        for table in requested:
            parts = [p.strip("[] ") for p in table.split(".") if p.strip("[] ")]
            if len(parts) >= 2:
                schema_name, table_name = parts[-2], parts[-1]
                clauses.append("(TABLE_SCHEMA = ? AND TABLE_NAME = ?)")
                params.extend([schema_name, table_name])
            else:
                clauses.append("(TABLE_NAME = ?)")
                params.append(parts[0])
        sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, ORDINAL_POSITION "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE {' OR '.join(clauses)} "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        conn = self._connect_sql_server(server, database)
        try:
            cursor = conn.cursor()
            cursor.execute(sql, *params)
            rows = cursor.fetchall()
        except Exception as exc:
            raise PBIRError(f"Column query failed: {exc}") from exc
        finally:
            conn.close()

        limit = max(0, int(max_columns_per_table))
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = f"{row[0]}.{row[1]}"
            entry = grouped.setdefault(
                key,
                {"schema": row[0], "name": row[1], "full_name": key, "column_count": 0, "columns": []},
            )
            entry["column_count"] += 1
            if len(entry["columns"]) < limit:
                entry["columns"].append({
                    "name": row[2],
                    "dataType": row[3],
                    "ordinal": row[4],
                })
        for entry in grouped.values():
            if entry["column_count"] > len(entry["columns"]):
                entry["columns_truncated"] = entry["column_count"] - len(entry["columns"])
        found = set(grouped)
        missing = [
            table for table in requested
            if table not in found and not any(key.endswith(f".{table}") for key in found)
        ]
        return {
            "server": server,
            "database": database,
            "requested": requested,
            "table_count": len(grouped),
            "tables": list(grouped.values()),
            "missing": missing,
        }

    def _run_source_query(
        self,
        server: str,
        database: str,
        sql: str,
        preview: bool = False,
        max_rows: int = 200,
        mode: str = "rows",
    ) -> dict[str, Any]:
        if mode not in {"rows", "summary"}:
            raise PBIRError("mode must be one of: rows, summary")
        limit = 10 if preview else max_rows
        conn = self._connect_sql_server(server, database)

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
        if mode == "summary":
            return {
                "columns": columns,
                "row_count": len(rows),
                "truncated": truncated,
                "sample_row": list(rows[0]) if rows else None,
            }
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
            "validation": validation_summary(validation),
        }

    def tool_list_projects(self, args: dict[str, Any]) -> dict[str, Any]:
        root = Path(args["search_root"]).expanduser().resolve()
        if not root.exists():
            raise PBIRError(f"Directory does not exist: {root}")
        max_depth = int(args.get("max_depth", 4))
        exclude_export_folders = bool(args.get("exclude_export_folders", True))
        projects: list[dict[str, Any]] = []

        def scan(path: Path, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                sibling_pbip_stems = {p.stem for p in path.glob("*.pbip")}
                for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_file() and child.suffix.lower() == ".pbip":
                        try:
                            version = read_json(child).get("version", "?")
                        except PBIRError:
                            version = "?"
                        projects.append({"name": child.stem, "path": str(child), "version": version})
                    elif child.is_dir() and not child.name.startswith("."):
                        if exclude_export_folders and child.name in sibling_pbip_stems:
                            continue
                        scan(child, depth + 1)
            except PermissionError:
                pass

        scan(root, 0)
        return {
            "search_root": str(root),
            "project_count": len(projects),
            "exclude_export_folders": exclude_export_folders,
            "projects": projects,
        }

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
