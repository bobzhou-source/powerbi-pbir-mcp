"""Smoke test: bulk_create_rls_roles, then clean up."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from pbir_mcp_server import PBIRSemanticModel

SM_PATH = r"C:\Users\RZHOU\Desktop\Supplier Dashboard\Supplier Suspend Dashboard.SemanticModel"
sm = PBIRSemanticModel(Path(SM_PATH))

TEST_SUPPLIERS = ["__TestSupplierA__", "__TestSupplierB__"]

print("=== Before ===")
print("Existing roles:", [r["name"] for r in sm.list_roles()])

print("\n=== bulk_create_rls_roles ===")
result = sm.bulk_create_rls_roles(
    table="Suspend Comments",
    column="BillingSP",
    values=TEST_SUPPLIERS,
    role_prefix="Supplier_",
)
print(json.dumps(result, indent=2))

print("\n=== Role file contents ===")
for name in result["created"]:
    path = sm.roles_dir / f"{name}.tmdl"
    print(f"--- {name}.tmdl ---")
    print(path.read_text(encoding="utf-8"))

print("\n=== All roles after ===")
for r in sm.list_roles():
    print(f"  {r['name']}: {r['tablePermissions']}")

print("\n=== on_conflict=skip (should skip both) ===")
r2 = sm.bulk_create_rls_roles(
    table="Suspend Comments", column="BillingSP",
    values=TEST_SUPPLIERS, role_prefix="Supplier_", on_conflict="skip",
)
print(json.dumps(r2, indent=2))

print("\n=== Cleanup ===")
for name in result["created"]:
    sm.manage_role("delete", name)
    print(f"  Deleted {name}")

print("\nRoles after cleanup:", [r["name"] for r in sm.list_roles()])
