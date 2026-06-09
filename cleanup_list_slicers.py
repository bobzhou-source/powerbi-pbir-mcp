"""
Remove date, slider, and general.outlineColor from list slicers that aren't date-range slicers.
A list slicer is a slicer visual WITHOUT a pre-existing date or slider object block.
We detect these as slicers that had those groups added by our test (i.e., have them now but shouldn't).
"""
import json
from pathlib import Path

REPORT_PATH = Path(r"C:\Users\RZHOU\Desktop\Supplier Dashboard\Supplier Suspend Dashboard.Report")

def load(p): return json.loads(p.read_text(encoding="utf-8"))
def save(p, d): p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")

fixed = []
for path in sorted(REPORT_PATH.rglob("visual.json")):
    data = load(path)
    vtype = data.get("visual", {}).get("visualType", "")
    if vtype != "slicer":
        continue
    objects = data.get("visual", {}).get("objects", {})
    dirty = False

    # date and slider are date-range-slicer-only.
    # If the slicer has a "date" section with ONLY properties we wrote (background+fontColor),
    # and it has a "slider" section with ONLY color — remove them (we injected them)
    if "date" in objects:
        items = objects["date"]
        if isinstance(items, list) and len(items) == 1:
            props = items[0].get("properties", {})
            if set(props.keys()) <= {"background", "fontColor"}:
                del objects["date"]
                dirty = True

    if "slider" in objects:
        items = objects["slider"]
        if isinstance(items, list) and len(items) == 1:
            props = items[0].get("properties", {})
            if set(props.keys()) <= {"color"}:
                del objects["slider"]
                dirty = True

    # general.outlineColor — remove if it's the only property we added
    if "general" in objects:
        items = objects["general"]
        if isinstance(items, list) and len(items) == 1:
            props = items[0].get("properties", {})
            if list(props.keys()) == ["outlineColor"]:
                del objects["general"]
                dirty = True

    if dirty:
        save(path, data)
        fixed.append(path.parent.name)

print(f"Cleaned {len(fixed)} list slicers: {', '.join(fixed) or 'none'}")
