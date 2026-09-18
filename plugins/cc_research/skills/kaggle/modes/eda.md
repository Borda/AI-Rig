<!-- file: eda.md — selected by composition.md -->

# EDA section contract

Generate the EDA section after the foundation. Use only grounded paths and schema fields.

## Section 3: EDA

Open with a `# %% [markdown]` EDA header.

### Just-in-time configuration

Define only EDA constants, including the grounded target column and sample count:

```python
# %%
SAMPLE_N = 9
TARGET_COL = "<grounded-target-column>"
```

### Dataset overview

- Load the grounded training table or file index.
- Display shape, head, dtypes, missing values, and appropriate descriptive statistics.
- Confirm referenced files exist on a representative sample.
- Fail fast on required resources — primary training table, file index, grounded target column: `assert not df_train.empty, "..."`. A competition guarantees these exist, so empty/missing means the load is broken, not a state to print and roll past. Never `if df_train.empty: print("SKIP — ..."); ` and continue.
- `if <resource> missing: ...` conditional-skip is valid **only** for optional resources — supplementary/external datasets, pretrained checkpoints, additional model storages — never for required ones. Even then, never skip silently: print what's missing and what section/analysis it skips.
- Treat absent columns, duplicate identifiers, and unreadable samples explicitly (report the finding) — these are not competition-guaranteed and may legitimately vary.

### Target distribution

Plot the target distribution. For regression, include robust quantiles/outlier context; for segmentation/detection, summarize annotation prevalence and empty-target frequency.

### Hypothesis validation

Create a markdown hypothesis cell followed by an executable check for each decision-driving question. At minimum consider:

- class/target balance → loss, sampling, or stratification;
- spatial/sequence dimensions → resize, crop, padding, or batching;
- duplicates, leakage, or grouped entities → split strategy;
- missing/corrupt files → dataset guards;
- label noise or empty annotations → augmentation and evaluation behavior.

Every check ends with a printed finding and explicit design implication. Do not infer a conclusion from a plot without recording the observed statistic.

### Modality display

Load `modality-dispatch.md`:

```bash
_KAGGLE_MODES="${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/skills/kaggle/modes"
cat "$_KAGGLE_MODES/modality-dispatch.md"  # timeout: 5000
```

Select only the grounded branch, and define its visualization helper immediately before first use. Adapt every placeholder column and path from the fact table. Show representative samples and, where applicable, width/height, volume-shape, sequence-length, or point-count distributions.

### EDA lens

Display representative records/samples and print the grounded schema, target properties, missingness, duplicate/leakage checks, and the decisions carried into later stages. In EDA-only mode, retain these implications even though no later sections are generated.
