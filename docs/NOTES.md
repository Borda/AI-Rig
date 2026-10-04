# Simple documentation

MkDocs source for the [Borda's AI-Rig](https://borda.github.io/.local/) documentation site.

Product pages (`cc_foundry.md`, `cc_oss.md`, `codex-rig.md`, etc.) are small snippet wrappers that include `plugins/*/README.md`. Edit the source READMEs for product content and the wrappers only for page metadata.

Write crosslinks relative to the Markdown file that owns the content, for example `[Execution contract](shared/native-skill-contract.md#authorized-workflow-execution)` in a plugin README. `docs/hooks.py` preserves that source location when expanding each product wrapper. It publishes linked repository guides and assets under `repository/`, follows their links, and rewrites targets to site-relative URLs while keeping existing product-page URLs. Existing links to this repository's GitHub `blob/main` and `tree/main` files also resolve locally when their targets exist; external websites remain external. Fenced examples are not rewritten. No source files are copied into `docs/` or modified by the build.

Use ordinary Markdown headings, lists, tables, and fenced code in included READMEs. Keep only the Contents list in a disclosure block, using `<details markdown="1">` and `<summary><strong>📋 Contents</strong></summary>` so MkDocs parses its links. Do not wrap substantive documentation in disclosure blocks: raw HTML can leave Markdown unparsed. Keep repository-specific global-instruction setup in `.codex/README.md` and link to it from the product page.

## Local build

```bash
python -m pip install --group pyproject.toml:docs   # or: uv sync --only-group docs
python -m mkdocs build          # output → site/
```

## Serve with live-reload

```bash
python -m mkdocs serve          # http://127.0.0.1:8000
```

Changes to `plugins/*/README.md`, `docs/index.md`, or `mkdocs.yml` reload automatically.

## CI

Docs deploy to GitHub Pages on every push to `main`, including changes to linked repository guides and assets. Workflow: `.github/workflows/docs.yml`.
