import pbir_mcp_server as m

print('syntax OK')
srv = m.PBIRMCPServer()
print(f'Tools: {len(srv.tools)}')
for t in ['run_python', 'register_custom_tool', 'list_custom_tools', 'delete_custom_tool']:
    status = 'OK' if t in srv.tools else 'MISSING'
    print(f'  {status}  {t}')

# --- test run_python against the real report ---
from pathlib import Path

REPORT = r'C:\Users\RZHOU\Desktop\Supplier Dashboard\Supplier Suspend Dashboard.Report'

print('\n=== run_python: list all visual names ===')
code = """
names = []
for page_dir in report.page_dirs():
    pdata = read_json(page_dir / 'page.json')
    for visual_dir in report.visual_dirs(page_dir):
        vdata = read_json(visual_dir / 'visual.json')
        names.append({'page': pdata.get('displayName', page_dir.name), 'visual': vdata.get('name')})
result = names
"""
ctx = srv._build_exec_context({'project_path': REPORT})
out = srv._run_python_code(code, ctx)
for row in out[:5]:
    print(' ', row)
print(f'  ... {len(out)} total')

# --- test register_custom_tool ---
print('\n=== register_custom_tool ===')
defn = srv._save_and_register_custom_tool({
    'name': '__test_tool__',
    'description': 'Test: count visuals on a page.',
    'code': 'result = len(list(report.visual_dirs(report.find_page_dir(args["page"]))))',
    'schema': {
        'type': 'object',
        'properties': {
            'project_path': {'type': 'string'},
            'page': {'type': 'string'},
        },
        'required': ['page'],
    },
})
print(' saved:', defn)

print('\n=== list_custom_tools ===')
print(srv._list_custom_tools())

print('\n=== call the custom tool ===')
result = srv.tools['__test_tool__'][2]({'project_path': REPORT, 'page': 'Overview'})
print(' visual count on Overview:', result)

print('\n=== delete_custom_tool ===')
print(srv._delete_custom_tool('__test_tool__'))
print('Tools after delete:', '__test_tool__' in srv.tools)
