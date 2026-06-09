# Publishing

## GitHub

```powershell
git init
git add .
git commit -m "Initial Power BI PBIR MCP release"
gh repo create powerbi-pbir-mcp --public --source=. --remote=origin --push
```

Use `--private` instead of `--public` if this should not be public yet.

## Install From GitHub

```powershell
pipx install git+https://github.com/bobzhou-source/powerbi-pbir-mcp.git
codex mcp add powerbi-pbir-report-mcp -- powerbi-pbir-mcp
```

## Release Checklist

- Run `python -m unittest discover -s tests`.
- Open a generated PBIP in Power BI Desktop.
- Confirm repository URLs in `README.md` and `pyproject.toml`.
- Tag the release: `git tag v0.1.0 && git push --tags`.
