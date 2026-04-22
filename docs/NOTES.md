# Simple documentation

MkDocs source for the [Borda's AI-Rig](https://borda.github.io/.local/) documentation site.

Plugin pages (`foundry.md`, `oss.md`, etc.) are symlinks to `plugins/*/README.md` — edit the source READMEs, not the symlinks.

## Local build

```bash
python -m pip install -r docs/requirements.txt
python -m mkdocs build          # output → site/
```

## Serve with live-reload

```bash
python -m mkdocs serve          # http://127.0.0.1:8000
```

Changes to `plugins/*/README.md`, `docs/index.md`, or `mkdocs.yml` reload automatically.

## CI

Docs deploy to GitHub Pages on every push to `main` that touches `plugins/*/README.md`, `docs/**`, or `mkdocs.yml`. Workflow: `.github/workflows/docs.yml`.
