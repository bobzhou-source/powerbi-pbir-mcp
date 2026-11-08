---
name: powerbi-pbir-mcp
description: Use when an AI agent must create, read, edit or validate a Power BI Project (.pbip) report saved in PBIR format, including pages, visuals, themes, layout, semantic model tables, DAX measures, relationships, hierarchies, perspectives and row-level security (RLS) roles, through the powerbi-pbir-mcp MCP server.
---

# powerbi-pbir-mcp

Local MCP server (Python 3.10+, standard library only, stdio transport) for editing Power BI Project reports saved in PBIR format. Source: https://github.com/bobzhou-source/powerbi-pbir-mcp

## Install and register

```sh
pipx install git+https://github.com/bobzhou-source/powerbi-pbir-mcp.git
codex mcp add powerbi-pbir-report-mcp -- powerbi-pbir-mcp
```

Any MCP client can launch the `powerbi-pbir-mcp` command over stdio (or `python -m pbir_mcp_server` from a clone).

## Agent instructions

1. Start every session with `load_project` on a `.pbip`, `.pbir`, `.Report` or `definition` folder (or `create_project` to scaffold a Desktop-stable PBIP), then `pbir_report_summary` to learn existing pages and visuals.
2. Before binding visuals, inspect the model with `list_semantic_model_tables`, `read_semantic_model` or `search_semantic_model` so every field reference exists.
3. Batch page, visual, theme and layout changes into one `pbir_apply_plan` call instead of many single edits; use `multi_report_apply_plan` for the same change across reports.
4. Model changes go through the dedicated tools: `manage_measure` (DAX), `manage_relationship`, `manage_hierarchy`, `manage_perspective`, `set_table_storage_mode`, and `manage_rls_role` / `bulk_create_rls_roles` for row-level security. Review with `tmdl_diff`.
5. Always finish with `validate_report`. It checks JSON shape, bindings, layout bounds and common Power BI Desktop blockers; fix every error before telling the user the report is ready.
6. Close Power BI Desktop (or reopen the project afterwards) so it does not overwrite files the server just changed.

## Tool reference (selection)

| Area | Tools |
|---|---|
| Project | `load_project`, `create_project`, `get_current_project`, `list_projects`, `validate_report` |
| Report | `pbir_report_summary`, `get_report_context`, `pbir_apply_plan`, `multi_report_apply_plan`, `cross_report_index`, `delete_page`, `duplicate_page`, `reorder_pages`, `set_visual_title`, `set_page_background` |
| Theme | `generate_theme`, `recolor_report` |
| Semantic model | `list_semantic_model_tables`, `read_semantic_model`, `search_semantic_model`, `get_relationships`, `manage_measure`, `manage_relationship`, `manage_hierarchy`, `manage_perspective`, `list_perspectives`, `set_table_storage_mode`, `tmdl_diff` |
| Sources | `get_table_source`, `find_source_tables`, `describe_source_tables`, `run_source_query`, `get_distinct_values` |
| Security | `list_rls_roles`, `manage_rls_role`, `bulk_create_rls_roles`, `bulk_delete_rls_roles` |
| Extensibility | `run_python`, `register_custom_tool`, `list_custom_tools`, `delete_custom_tool` |

## Related

- Enterprise AI and billing work where this server was used: https://robertzhou.dev/work/parsifal-billing/
- Portfolio: https://robertzhou.dev

Author: Robert Zhou (https://robertzhou.dev) · ORCID 0009-0009-2155-7215 · Colby College
