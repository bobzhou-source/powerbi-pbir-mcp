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

from .shared import *


class PBIRSemanticModel:
    """Read/write RLS roles in a PBIP SemanticModel (TMDL format)."""

    def __init__(self, sm_root: Path) -> None:
        self.sm_root = sm_root
        self.roles_dir = sm_root / "definition" / "roles"
        self.model_tmdl = sm_root / "definition" / "model.tmdl"

    @classmethod
    def from_report(cls, report_root: Path) -> "PBIRSemanticModel | None":
        """Locate the referenced or sibling .SemanticModel folder from a .Report folder."""
        from .report import PBIRReport

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

    def get_table_source(
        self,
        table: str,
        include_sql: bool = True,
        max_sql_chars: int = 4000,
    ) -> dict[str, Any]:
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
                sql = sql.strip()
                result["sql_query_chars"] = len(sql)
                if include_sql:
                    result["sql_query"] = truncate_text(sql, int(max_sql_chars))
                    result["sql_query_truncated"] = len(result["sql_query"]) < len(sql)
                # Extract primary FROM source (first FROM clause) for efficient direct queries
                from_match = re.search(r'\bFROM\s+([\w\.\[\]]+)', sql, re.IGNORECASE)
                if from_match:
                    result["source_table"] = from_match.group(1)

        elif partition_type == "dax":
            dax_match = re.search(r'source\s*=\s*([\s\S]+?)(?:\n\t\w|\Z)', text)
            if dax_match:
                dax = dax_match.group(1).strip()
                result["dax_expression_chars"] = len(dax)
                if include_sql:
                    result["dax_expression"] = truncate_text(dax, int(max_sql_chars))
                    result["dax_expression_truncated"] = len(result["dax_expression"]) < len(dax)

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
