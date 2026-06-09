import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pbir_mcp_server import (
    PAGE_SCHEMA,
    PAGES_METADATA_SCHEMA,
    REPORT_SCHEMA,
    VERSION_SCHEMA,
    PBIRReport,
    PBIRMCPServer,
)


TEST_TMP_ROOT = Path(os.environ.get("PBIR_MCP_TEST_TMP", r"C:\tmp"))


def temp_dir() -> tempfile.TemporaryDirectory:
    if not TEST_TMP_ROOT.exists():
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def make_fixture(root: Path) -> Path:
    report = root / "Sales.Report"
    definition = report / "definition"
    pages = definition / "pages"
    write_json(
        report / "definition.pbir",
        {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
            "version": "4.0",
            "datasetReference": {"byPath": {"path": "../Sales.SemanticModel"}},
        },
    )
    write_json(
        definition / "report.json",
        {
            "$schema": REPORT_SCHEMA,
            "themeCollection": {
                "baseTheme": {
                    "name": "CY25SU12",
                    "reportVersionAtImport": {"visual": "2.9.0", "page": "2.1.0", "report": "3.3.0"},
                    "type": "SharedResources",
                }
            },
        },
    )
    write_json(definition / "version.json", {"$schema": VERSION_SCHEMA, "version": "4.0.0"})
    write_json(pages / "pages.json", {"$schema": PAGES_METADATA_SCHEMA, "pageOrder": ["Overview"], "activePageName": "Overview"})
    write_json(
        pages / "Overview" / "page.json",
        {
            "$schema": PAGE_SCHEMA,
            "name": "Overview",
            "displayName": "Overview",
            "displayOption": "FitToPage",
            "width": 1280,
            "height": 720,
        },
    )
    (pages / "Overview" / "visuals").mkdir(parents=True)
    return report


class PBIRReportTests(unittest.TestCase):
    def test_create_page_add_visual_and_validate(self) -> None:
        with temp_dir() as temp:
            report_dir = make_fixture(Path(temp))
            report = PBIRReport(report_dir)

            initial = report.validate()
            self.assertTrue(initial.ok, initial.errors)
            self.assertEqual(initial.page_count, 1)

            page_result = report.create_page("Executive Summary", name="ExecutiveSummary", make_active=True, validate_after=True)
            self.assertTrue(page_result["validation"]["ok"])

            visual_result = report.add_visual_from_template(
                "Executive Summary",
                template="card",
                name="RevenueCard",
                title="Revenue",
                position={"x": 40, "y": 80, "width": 260, "height": 120},
                query_roles={"Values": [{"table": "Sales", "measure": "Revenue"}]},
                validate_after=True,
            )
            self.assertTrue(visual_result["validation"]["ok"])

            pages = report.list_pages(include_visuals=True)
            self.assertEqual(len(pages["pages"]), 2)
            active = [page for page in pages["pages"] if page["is_active"]]
            self.assertEqual(active[0]["name"], "ExecutiveSummary")
            self.assertEqual(pages["activePageName"], "ExecutiveSummary")

            visuals = report.list_visuals("Executive Summary")["visuals"]
            self.assertEqual(len(visuals), 1)
            self.assertEqual(visuals[0]["visualType"], "card")
            self.assertEqual(visuals[0]["title"], "Revenue")

            visual_json = json.loads((report_dir / "definition" / "pages" / "ExecutiveSummary" / "visuals" / "RevenueCard" / "visual.json").read_text())
            self.assertIn("query", visual_json["visual"])

    def test_clone_and_reposition_visual(self) -> None:
        with temp_dir() as temp:
            report_dir = make_fixture(Path(temp))
            report = PBIRReport(report_dir)
            report.add_visual_from_template("Overview", template="bar", name="SalesBar", title="Sales")
            report.create_page("Detail", name="Detail")

            cloned = report.clone_visual("Overview", "SalesBar", "Detail", name="SalesBarCopy", title="Sales Copy", validate_after=True)
            self.assertTrue(cloned["validation"]["ok"])

            moved = report.set_visual_position("Detail", "SalesBarCopy", {"x": 100, "y": 120, "width": 500, "height": 300})
            self.assertEqual(moved["visual"]["position"]["x"], 100.0)
            self.assertEqual(moved["visual"]["position"]["width"], 500.0)

    def test_manage_custom_theme(self) -> None:
        with temp_dir() as temp:
            report_dir = make_fixture(Path(temp))
            report = PBIRReport(report_dir)

            result = report.manage_theme(
                "set_custom",
                name="CorporateTheme",
                theme={"name": "CorporateTheme", "dataColors": ["#005A9E", "#E87722"]},
                validate_after=True,
            )
            self.assertTrue(result["validation"]["ok"])
            self.assertTrue((report_dir / "StaticResources" / "RegisteredResources" / "CorporateTheme.json").exists())

            themes = report.manage_theme("list")
            self.assertIn("customTheme", themes["themeCollection"])
            self.assertEqual(len(themes["registeredResources"]), 1)

    def test_set_active_page_can_set_landing_page(self) -> None:
        with temp_dir() as temp:
            report_dir = make_fixture(Path(temp))
            report = PBIRReport(report_dir)
            report.create_page("Executive Summary", name="ExecutiveSummary")

            result = report.set_active_page("Executive Summary", landing=True, validate_after=True)
            self.assertTrue(result["validation"]["ok"])
            self.assertEqual(result["activePageName"], "ExecutiveSummary")
            self.assertEqual(result["landingPageName"], "ExecutiveSummary")

            pages = report.list_pages()
            landing = [page for page in pages["pages"] if page["is_landing"]]
            self.assertEqual(landing[0]["name"], "ExecutiveSummary")

    def test_validate_rejects_schema_breaking_visual_fields(self) -> None:
        with temp_dir() as temp:
            report_dir = make_fixture(Path(temp))
            report = PBIRReport(report_dir)
            report.add_visual_from_template("Overview", template="card", name="RevenueCard")

            visual_path = report_dir / "definition" / "pages" / "Overview" / "visuals" / "RevenueCard" / "visual.json"
            visual_json = json.loads(visual_path.read_text(encoding="utf-8"))
            visual_json["unsupported"] = True
            visual_json["position"]["bad"] = 1
            write_json(visual_path, visual_json)

            validation = report.validate()
            self.assertFalse(validation.ok)
            self.assertTrue(any("unsupported top-level keys" in error for error in validation.errors))
            self.assertTrue(any("position has unsupported keys" in error for error in validation.errors))

    def test_mcp_server_exposes_expected_tools(self) -> None:
        server = PBIRMCPServer()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            }
        )
        tool_names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertIn("load_project", tool_names)
        self.assertIn("create_page", tool_names)
        self.assertIn("add_visual_from_template", tool_names)
        self.assertIn("set_active_page", tool_names)
        self.assertIn("manage_theme", tool_names)
        self.assertIn("create_project", tool_names)
        self.assertIn("pbir_apply_plan", tool_names)
        self.assertIn("pbir_report_summary", tool_names)
        self.assertIn("read_semantic_model", tool_names)
        self.assertIn("list_projects", tool_names)

    def test_scaffold_project_creates_all_files(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            report = PBIRReport.scaffold_project(root, "My Dashboard")
            # PBIP entry point
            self.assertTrue((root / "My Dashboard.pbip").exists())
            # Report folder files
            self.assertTrue((root / "My Dashboard.Report" / ".platform").exists())
            self.assertTrue((root / "My Dashboard.Report" / "definition.pbir").exists())
            self.assertTrue((root / "My Dashboard.Report" / "definition" / "report.json").exists())
            self.assertTrue((root / "My Dashboard.Report" / "definition" / "version.json").exists())
            self.assertTrue((root / "My Dashboard.Report" / "definition" / "pages" / "pages.json").exists())
            self.assertTrue((root / "My Dashboard.Report" / "definition" / "pages" / "Overview" / "page.json").exists())
            # SemanticModel stub
            self.assertTrue((root / "My Dashboard.SemanticModel" / ".platform").exists())
            self.assertTrue((root / "My Dashboard.SemanticModel" / "definition.pbism").exists())
            # Validates cleanly
            result = report.validate()
            self.assertTrue(result.ok, result.errors)
            self.assertEqual(result.page_count, 1)
            # definition.pbir references SemanticModel by relative path
            pbir = json.loads((root / "My Dashboard.Report" / "definition.pbir").read_text())
            self.assertEqual(pbir["datasetReference"]["byPath"]["path"], "../My Dashboard.SemanticModel")
            # semantic_model_path resolves correctly
            sm = report.semantic_model_path()
            self.assertIsNotNone(sm)
            self.assertEqual(sm, (root / "My Dashboard.SemanticModel").resolve())
            # Desktop-stable metadata: version.json must be 2.0.0
            ver = json.loads((root / "My Dashboard.Report" / "definition" / "version.json").read_text())
            self.assertEqual(ver["version"], "2.0.0")
            # report.json must have resourcePackages so PBI Desktop can resolve the SharedResources theme
            rpt = json.loads((root / "My Dashboard.Report" / "definition" / "report.json").read_text())
            self.assertIn("resourcePackages", rpt)
            self.assertGreater(len(rpt["resourcePackages"]), 0)
            pkg_items = rpt["resourcePackages"][0]["items"]
            theme_name = rpt["themeCollection"]["baseTheme"]["name"]
            self.assertTrue(any(item["name"] == theme_name for item in pkg_items), "resourcePackages must reference the same theme as themeCollection")

    def test_scaffold_project_custom_page(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            PBIRReport.scaffold_project(
                root, "Sales", initial_page_display_name="Executive Summary",
                initial_page_name="Executive", page_width=1920, page_height=1080,
            )
            page_json = json.loads((root / "Sales.Report" / "definition" / "pages" / "Executive" / "page.json").read_text())
            self.assertEqual(page_json["displayName"], "Executive Summary")
            self.assertEqual(page_json["width"], 1920.0)
            pages_json = json.loads((root / "Sales.Report" / "definition" / "pages" / "pages.json").read_text())
            self.assertEqual(pages_json["pageOrder"], ["Executive"])
            self.assertEqual(pages_json["activePageName"], "Executive")

    def test_scaffold_project_duplicate_fails(self) -> None:
        from pbir_mcp_server import PBIRError
        with temp_dir() as temp:
            root = Path(temp)
            PBIRReport.scaffold_project(root, "Sales")
            with self.assertRaises(PBIRError):
                PBIRReport.scaffold_project(root, "Sales")

    def test_layout_analysis_empty_page(self) -> None:
        with temp_dir() as temp:
            report_dir = make_fixture(Path(temp))
            report = PBIRReport(report_dir)
            page_dir = report.find_page_dir("Overview")
            layout = report.page_layout_analysis(page_dir)
            self.assertEqual(layout["overlaps"], [])
            self.assertEqual(layout["out_of_bounds"], [])
            self.assertEqual(layout["visual_count"], 0)
            self.assertEqual(layout["coverage_pct"], 0.0)
            self.assertNotIn("grid", layout)

    def test_layout_analysis_overlap_detection(self) -> None:
        with temp_dir() as temp:
            report_dir = make_fixture(Path(temp))
            report = PBIRReport(report_dir)
            report.add_visual_from_template("Overview", template="card", name="A", position={"x": 0, "y": 0, "width": 200, "height": 200})
            report.add_visual_from_template("Overview", template="card", name="B", position={"x": 100, "y": 100, "width": 200, "height": 200})
            report.add_visual_from_template("Overview", template="card", name="C", position={"x": 500, "y": 500, "width": 100, "height": 100})
            page_dir = report.find_page_dir("Overview")
            layout = report.page_layout_analysis(page_dir)
            self.assertEqual(len(layout["overlaps"]), 1)
            self.assertEqual({layout["overlaps"][0]["a"], layout["overlaps"][0]["b"]}, {"A", "B"})
            self.assertGreater(layout["overlaps"][0]["overlap_px"], 0)
            self.assertEqual(layout["out_of_bounds"], [])
            self.assertIn("grid", layout)

    def test_layout_overlap_warning_in_validate(self) -> None:
        with temp_dir() as temp:
            report_dir = make_fixture(Path(temp))
            report = PBIRReport(report_dir)
            report.add_visual_from_template("Overview", template="card", name="A", position={"x": 0, "y": 0, "width": 200, "height": 200})
            report.add_visual_from_template("Overview", template="card", name="B", position={"x": 100, "y": 100, "width": 200, "height": 200})
            result = report.validate()
            self.assertTrue(result.ok)
            self.assertTrue(any("overlap" in w for w in result.warnings))

    def test_parse_tmdl_table_file(self) -> None:
        tmdl = """table Sales
\tlineageTag: abc123

\tcolumn Revenue
\t\tdataType: double
\t\tlineageTag: def456
\t\tsourceColumn: Revenue

\tcolumn Region
\t\tdataType: string

\tmeasure TotalRevenue = SUM(Sales[Revenue])
\t\tformatString: $#,##0

\tpartition Sales = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = Sql.Database("myserver", "mydb", [Query="select ..."])
\t\t\t\tin
\t\t\t\t    Source
"""
        parsed = PBIRReport._parse_tmdl_table_file(tmdl)
        self.assertEqual(parsed["name"], "Sales")
        self.assertEqual(len(parsed["columns"]), 2)
        self.assertEqual(parsed["columns"][0]["name"], "Revenue")
        self.assertEqual(parsed["columns"][0]["dataType"], "double")
        self.assertEqual(parsed["columns"][1]["name"], "Region")
        self.assertEqual(len(parsed["measures"]), 1)
        self.assertEqual(parsed["measures"][0]["name"], "TotalRevenue")
        self.assertIsNotNone(parsed["source_hint"])
        self.assertEqual(parsed["source_hint"]["type"], "SqlServer")
        self.assertEqual(parsed["source_hint"]["server"], "myserver")
        self.assertEqual(parsed["source_hint"]["database"], "mydb")

    def test_parse_tmdl_quoted_names(self) -> None:
        """TMDL quotes identifiers that contain spaces."""
        tmdl = """table 'Suspend Comments'
\tlineageTag: abc123

\tcolumn BillID
\t\tdataType: int64

\tcolumn 'Billing SP'
\t\tdataType: string

\tmeasure 'Suspend Comment Rows' = COUNTROWS('Suspend Comments')
\t\tformatString: #,0

\tpartition 'Suspend Comments' = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet Source = Sql.Database("ME-VMDW01", "PAS_DW", [Query="select 1"])
\t\t\t\tin Source
"""
        parsed = PBIRReport._parse_tmdl_table_file(tmdl)
        self.assertEqual(parsed["name"], "Suspend Comments")
        self.assertEqual(parsed["columns"][0]["name"], "BillID")
        self.assertEqual(parsed["columns"][1]["name"], "Billing SP")
        self.assertEqual(parsed["measures"][0]["name"], "Suspend Comment Rows")
        self.assertEqual(parsed["source_hint"]["server"], "ME-VMDW01")

    def test_read_semantic_model(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            report_dir = make_fixture(root)
            # Create a minimal SemanticModel next to the report
            sm_dir = root / "Sales.SemanticModel" / "definition" / "tables"
            sm_dir.mkdir(parents=True)
            tmdl_content = "table Products\n\tcolumn Name\n\t\tdataType: string\n\tmeasure Count = COUNTROWS(Products)\n"
            (sm_dir / "Products.tmdl").write_text(tmdl_content, encoding="utf-8")
            report = PBIRReport(report_dir)
            result = report.read_semantic_model()
            self.assertEqual(result["table_count"], 1)
            self.assertEqual(result["tables"][0]["name"], "Products")
            self.assertEqual(result["tables"][0]["columns"][0]["name"], "Name")
            self.assertEqual(result["tables"][0]["measures"][0]["name"], "Count")

    def test_list_projects(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            make_fixture(root)
            (root / "Sales.pbip").write_text(json.dumps({"version": "4.0"}), encoding="utf-8")
            sub = root / "subdir"
            sub.mkdir()
            (sub / "Other.pbip").write_text(json.dumps({"version": "4.0"}), encoding="utf-8")
            server = PBIRMCPServer()
            result = server.tool_list_projects({"search_root": str(root)})
            names = [p["name"] for p in result["projects"]]
            self.assertIn("Sales", names)
            self.assertIn("Other", names)
            self.assertEqual(result["project_count"], 2)

    def test_stdio_uses_mcp_json_lines(self) -> None:
        server_path = Path(__file__).resolve().parents[1] / "pbir_mcp_server.py"
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        wire = "\n".join(json.dumps(message, separators=(",", ":")) for message in messages) + "\n"

        result = subprocess.run(
            [sys.executable, "-B", str(server_path)],
            input=wire,
            capture_output=True,
            cwd=server_path.parent,
            text=True,
            timeout=5,
            check=True,
        )

        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "powerbi-pbir-report-mcp")
        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertIn("get_current_project", tool_names)


    def test_update_page_display_name(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            report = PBIRReport.scaffold_project(root, "MyReport", initial_page_display_name="Overview")
            result = report.update_page("Overview", display_name="Executive Summary")
            self.assertEqual(result["page"]["displayName"], "Executive Summary")
            page_json = json.loads((report.pages_dir / "Overview" / "page.json").read_text(encoding="utf-8"))
            self.assertEqual(page_json["displayName"], "Executive Summary")
            self.assertEqual(page_json["name"], "Overview")

    def test_update_page_dimensions(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            report = PBIRReport.scaffold_project(root, "MyReport")
            result = report.update_page("Overview", width=1920, height=1080)
            self.assertEqual(result["page"]["width"], 1920.0)
            self.assertEqual(result["page"]["height"], 1080.0)
            page_json = json.loads((report.pages_dir / "Overview" / "page.json").read_text(encoding="utf-8"))
            self.assertEqual(page_json["width"], 1920.0)
            self.assertEqual(page_json["height"], 1080.0)
            self.assertEqual(page_json["displayName"], "Overview")

    def test_generate_theme_light(self) -> None:
        from pbir_mcp_server import build_pbi_theme
        theme = build_pbi_theme(
            name="TestLight", primary="#0078D4", accent="#E66C37",
            background="#FFFFFF", text="#252423",
            good="#1AAB40", neutral_color="#D9B300", bad="#D64550",
            page_bg_transparency=0,
        )
        self.assertEqual(theme["name"], "TestLight")
        self.assertEqual(theme["tableAccent"], "#0078D4")
        self.assertGreaterEqual(len(theme["dataColors"]), 8)
        # Light background: no global visual style overrides
        self.assertNotIn("*", theme["visualStyles"])
        # Page background entry present
        self.assertIn("page", theme["visualStyles"])

    def test_generate_theme_dark(self) -> None:
        from pbir_mcp_server import build_pbi_theme
        theme = build_pbi_theme(
            name="TestDark", primary="#5B9BD5", accent="#ED7D31",
            background="#1F1F2E", text="#E8E6E3",
            good="#4DCFB3", neutral_color="#FFD166", bad="#FF6B6B",
            page_bg_transparency=0,
        )
        # Dark background: global visual overrides should be present
        self.assertIn("*", theme["visualStyles"])
        vs = theme["visualStyles"]["*"]["*"]
        self.assertIn("background", vs)
        self.assertIn("title", vs)
        self.assertIn("categoryAxis", vs)

    def test_generate_theme_writes_file_and_updates_report(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            report = PBIRReport.scaffold_project(root, "Themed")
            result = report.generate_theme(
                primary="#0078D4",
                theme_name="MyBlue",
                page_background_transparency=0,
            )
            self.assertIn("theme_path", result)
            self.assertTrue(Path(result["theme_path"]).exists())
            theme_json = json.loads(Path(result["theme_path"]).read_text(encoding="utf-8"))
            self.assertEqual(theme_json["name"], "MyBlue")
            self.assertEqual(theme_json["tableAccent"], "#0078D4")
            report_json = json.loads((root / "Themed.Report" / "definition" / "report.json").read_text())
            self.assertEqual(report_json["themeCollection"]["customTheme"]["name"], "MyBlue")

    def test_apply_style_preset_dark(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            report = PBIRReport.scaffold_project(root, "DarkTest")
            server = PBIRMCPServer()
            server.current_report = report
            result = server.apply_plan_action(report, {"op": "apply_style_preset", "preset": "dark"})
            self.assertIn("theme_path", result)
            theme_json = json.loads(Path(result["theme_path"]).read_text(encoding="utf-8"))
            self.assertEqual(theme_json["name"], "Dark")
            self.assertIn("*", theme_json["visualStyles"])

    def test_apply_style_preset_invalid(self) -> None:
        from pbir_mcp_server import PBIRError
        with temp_dir() as temp:
            root = Path(temp)
            report = PBIRReport.scaffold_project(root, "Test")
            server = PBIRMCPServer()
            server.current_report = report
            with self.assertRaises(PBIRError):
                server.apply_plan_action(report, {"op": "apply_style_preset", "preset": "neon_pink"})

    def test_apply_plan_update_page(self) -> None:
        with temp_dir() as temp:
            root = Path(temp)
            report = PBIRReport.scaffold_project(root, "MyReport", initial_page_display_name="Overview")
            server = PBIRMCPServer()
            server.current_report = report
            result = server.apply_plan_action(report, {"op": "update_page", "page": "Overview", "display_name": "Executive Summary"})
            self.assertEqual(result["page"]["displayName"], "Executive Summary")


if __name__ == "__main__":
    unittest.main()
