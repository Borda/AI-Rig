---
description: Notebook authoring style — cell granularity, markdown narrative depth, plot framing, magics; jupytext `# %%` scripts and `.ipynb`
paths:
  - '**/*.ipynb'
  - '**/*.py'
---

Applies to every notebook created or edited — a Jupyter `.ipynb`, or a Jupytext `# %%` percent-format `.py` script destined to become one.

Hard constraints, no exceptions:

- **One action per cell** — one load, one transform, one plot, one check, one train call; never bundle load+display+validate to save cell count.
- **Every cell carries a why, not a what** — a comment or the preceding markdown sentence states the specific reason this step happens here; never restate what the code already shows.
- **Markdown before and after every plot** — the cell before states what the plot will show and the question it answers; the cell/comment after states the observed pattern and its implication. Never drop a chart on the reader cold.
- **Markdown blank lines are empty lines, never a bare `#`** — a lone `#` spacer renders as an empty level-1 heading in Jupyter/Kaggle, not whitespace.
- **`# ! cmd` inline, `%%bash` for 2+ shell commands** — never `get_ipython().run_line_magic(...)`.
- **`display()` over `print()` for pandas objects.**
- **Main path fails fast** — no `try`/`except` or silent fallback around required loads, training, inference, or output validation; assert preconditions and let unexpected errors halt.

Full standard — narrative depth, structured markdown, cell splitting, comment placement, docstrings, plot conventions — in `notebook-style.md`, resolved on demand:

```bash
RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/notebook-style.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/notebook-style.md"; cat "$RULE_FULL"  # timeout: 5000
```
