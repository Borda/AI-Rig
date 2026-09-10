<!-- file: style-rules.md — applied by composition.md -->

Apply ALL of these in generated script:

01. **Every section `# %% [markdown]` header: explain what, why, how it advances goal**: after `## Section Name` write 2–4 sentences covering (a) what stage does, (b) why this approach chosen over alternatives, (c) how it contributes to competition objective (metric, leaderboard, submission quality). Notebook is public educational resource — write for reader seeing competition first time. Bare `## Title` headings with no explanation forbidden.
02. **Shell commands — `# ! cmd` (Python comment)**: write every shell command as `# ! cmd`; valid Python syntax, visible as comment in Jupyter. `%matplotlib inline` verbatim — never `get_ipython().run_line_magic(...)`; linter rejects `%` magic → fix linter config
03. `# ==============================` between logical blocks within cell (not every line — only major breaks)
04. `_=` to suppress matplotlib/pandas return values: `_= df["col"].plot(...)`
05. **Every plot: axis labels + grid + legend when multiple series**: always call `plt.xlabel("...")`, `plt.ylabel("...")`, `plt.grid(True)` after any plot; chart with multiple lines/bars/hues → add `plt.legend()` or pass `legend=True`; seaborn facets use `g.set_axis_labels("x label", "y label")`
06. No `if __name__ == '__main__':` guards
07. No argparse, no dataclasses for config
08. **Blank lines — empty lines only**: in Markdown/text cells, blank lines must contain no characters. Never emit `#` alone or `# ` on blank line; Kaggle renders either as empty H1. In code cells, use real empty line rather than blank comment line.
09. **`display()` over `print()` for pandas objects**: use `display(df.head())`, `display(df.dtypes)`, `display(metrics.dropna(axis=1, how="all").head())`; `print()` for scalars and status strings only. Pattern for Markdown/text: `# Last sentence.` → empty line → `# Next paragraph.`
10. **No doctests in ipy scripts**: doctests belong in package modules, not notebook scripts — `# %% [markdown]` cell above function cell IS explanation; don't duplicate as doctest
11. **Compact docstrings — never omit**: always include one-line docstring; never omit — narrative lives in `# %% [markdown]` cell immediately above function cell; full Google-style docstrings with `Args:`, `Returns:`, `Example:` blocks apply only after distillation to `src/` utils package
12. **Main path must fail fast**: do not put `try`/`except`, `if`/`else`, conditional expressions, or silent fallbacks around required data loads, samples, charts, lenses, training, inference, or submission validation. Assert required preconditions directly before action; let unexpected errors halt execution. Branch only for task-specific processing selected from grounded evidence, not to make required notebook step optional.
