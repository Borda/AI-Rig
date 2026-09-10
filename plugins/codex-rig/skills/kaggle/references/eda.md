<!-- file: eda.md — selected by composition.md -->

# EDA section contract

Generate EDA section after foundation. Use only grounded paths and schema fields.

## Section 3: EDA

Open with a `# %% [markdown]` EDA header.

### Just-in-time configuration

Define only EDA constants, including grounded target column and sample count:

```python
# %%
SAMPLE_N = 9
TARGET_COL = "<grounded-target-column>"
```

### Dataset overview

- Load grounded training table or file index.
- Display shape, head, dtypes, missing values, and appropriate descriptive statistics.
- Confirm referenced files exist on representative sample.
- Assert non-empty data, required columns, sample availability, and readable representative files immediately before using them. Do not wrap overview, sample, or chart cells in `try`/`except` or conditional skips.

### Target distribution

Plot target distribution. For regression, include robust quantiles/outlier context; for segmentation/detection, summarize annotation prevalence and empty-target frequency.

### Hypothesis validation

Create markdown hypothesis cell followed by executable check for each decision-driving question. At minimum consider:

- class/target balance → loss, sampling, or stratification;
- spatial/sequence dimensions → resize, crop, padding, or batching;
- duplicates, leakage, or grouped entities → split strategy;
- missing/corrupt files → dataset guards;
- label noise or empty annotations → augmentation and evaluation behavior.

Every check ends with printed finding and explicit design implication. Do not infer conclusion from plot without recording observed statistic.

### Modality display

Read `modality-dispatch.md`, select only grounded branch, and define its visualization helper immediately before first use. Adapt every placeholder column and path from fact table. Show representative samples and, where applicable, width/height, volume-shape, sequence-length, or point-count distributions.

### EDA lens

Display representative records/samples and print grounded schema, target properties, missingness, duplicate/leakage checks, and decisions carried into later stages. In EDA-only mode, retain these implications even though no later sections are generated.
