"""
Remove slicer-specific object groups incorrectly added to non-slicer visuals.
Affected groups: header (color props), date, slider, general
"""
import json
from pathlib import Path

REPORT_PATH = Path(r"C:\Users\RZHOU\Desktop\Supplier Dashboard\Supplier Suspend Dashboard.Report")
SLICER_ONLY_GROUPS = {"date", "slider"}

def load(p): return json.loads(p.read_text(encoding="utf-8"))
def save(p, d): p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")

fixed = []
for path in sorted(REPORT_PATH.rglob("visual.json")):
    data = load(path)
    vtype = data.get("visual", {}).get("visualType", "")
    if vtype == "slicer":
        continue
    objects = data.get("visual", {}).get("objects", {})
    dirty = False
    # Remove date/slider entirely (never valid on non-slicers)
    for grp in SLICER_ONLY_GROUPS:
        if grp in objects:
            del objects[grp]
            dirty = True
    # Remove general.outlineColor if that's the only property (we added it)
    if "general" in objects:
        items = objects["general"]
        if isinstance(items, list) and len(items) == 1:
            props = items[0].get("properties", {})
            if list(props.keys()) == ["outlineColor"]:
                del objects["general"]
                dirty = True
    # Remove header color props we added (keep show:false if it was there)
    if "header" in objects:
        items = objects["header"]
        if isinstance(items, list) and len(items) == 1:
            props = items[0].get("properties", {})
            # Only remove if BOTH background+fontColor present and no show property
            if "background" in props and "fontColor" in props and "show" not in props:
                del objects["header"]
                dirty = True
    if dirty:
        save(path, data)
        fixed.append(path.parent.name)

print(f"Cleaned {len(fixed)} visuals: {', '.join(fixed)}")
