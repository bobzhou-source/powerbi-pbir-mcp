"""
Applies the full expected palette to all visuals and validates.
Expected result: 0 changes (everything already styled), no validation errors.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pbir_mcp_server import PBIRReport

REPORT_PATH = r"C:\Users\RZHOU\Desktop\Supplier Dashboard\Supplier Suspend Dashboard.Report"

# Colors confirmed by PBI Desktop ground-truth reads
PALETTE = {
    # Container chrome
    "title":            "#172033",
    "title_background": "#EAF2FB",
    "background":       "#FFFFFF",
    # Chart data & axes
    "data_primary":     "#1F5F99",
    "axis_labels":      "#364152",
    "axis_titles":      "#364152",
    # Card visuals
    "card_value":       "#344054",
    "card_label":       "#475467",
    # Data labels on charts
    "labels":           "#344054",
    # List slicers
    "slicer_items":     "#344054",
    # Date-range slicer-specific
    "slicer_header_bg":   "#EAF2FB",
    "slicer_header_text": "#172033",
    "slicer_date_bg":     "#FFFFFF",
    "slicer_date_text":   "#172033",
    "slicer_slider":      "#1F5F99",
    "slicer_outline":     "#D0D5DD",
}

report = PBIRReport(REPORT_PATH)

print("=== Applying palette (idempotent check) ===")
result = report.recolor_visuals(targets=PALETTE, validate_after=True)

changed = result.get("changed", [])
print(f"Changed: {result['changed_count']} visual(s)")
for c in changed:
    print(f"  CHANGED  page={c['page']}  visual={c['name']}")

val = result.get("validation", {})
print(f"\n=== Validation ===")
print(f"Pages:   {val.get('page_count', '?')}")
print(f"Visuals: {val.get('visual_count', '?')}")
errors = val.get("errors", [])
warnings = val.get("warnings", [])
if errors:
    print("ERRORS:")
    for e in errors:
        print(f"  [ERR]  {e}")
else:
    print("Errors: none")
if warnings:
    print("Warnings:")
    for w in warnings:
        print(f"  [WARN] {w}")
else:
    print("Warnings: none")

print(f"\n{'ALL GOOD' if not errors and result['changed_count'] == 0 else 'REVIEW NEEDED'}")
