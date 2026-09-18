<!-- Loaded by research:data-steward (sonnet + medium) -->

# ML Pipeline Patterns — data-steward reference

> Reference document, NOT an agent definition — contextual material for `research:data-steward`.

Data-steward loads this in `pipeline-audit` mode before Step 1. Covers grouped/temporal split strategies, class-imbalance handling, and DataLoader integrity patterns.

<split-strategies>

## Patient-Level Split (medical imaging — CRITICAL)

```python
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

patient_ids = metadata["patient_id"].values
# random_state MUST be pinned — omitting varies split per run; patient-overlap assertion
# still passes (group-aware), silently masking non-reproducibility. Same seed needed for cross-run comparisons.
gss = GroupShuffleSplit(n_splits=1, test_size=0.3, random_state=42)
train_idx, temp_idx = next(gss.split(metadata, groups=patient_ids))

# Verify zero patient overlap
train_patients = set(metadata.iloc[train_idx]["patient_id"])
test_patients = set(metadata.iloc[temp_idx]["patient_id"])
assert train_patients.isdisjoint(test_patients), "PATIENT LEAK DETECTED"
```

Medical-imaging checklist:

```markdown
[ ] Splits are by patient/subject ID, never by image/slice
[ ] DICOM metadata checked for hidden identifiers (StudyInstanceUID links images)
[ ] Multi-site data: stratify by site to avoid site-specific bias
[ ] Temporal data: no future scans leaking into training from same patient
[ ] Annotation consistency: inter-reader variability measured (Fleiss' kappa)
[ ] `random_state` pinned and logged in artifacts (required for cross-run split reproducibility — group-overlap assertion alone does NOT guarantee reproducibility)
```

Verify zero patient overlap between splits with `verify_patient_split.py` from `bin/`:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/verify_patient_split.py" \
    --train splits/train.csv --test splits/test.csv
```

## Temporal Split (time-series or streaming data)

Sort by time; split sequentially into 70%/15%/15% train/val/test without shuffling.

**Caveats — apply BEFORE the sort/split:**

- **Duplicate timestamps**: tie-break deterministically (e.g. secondary sort by stable row index or surrogate key) — otherwise 70/85% boundary lands inside arbitrarily ordered tie, bleeds near-boundary leakage.
- **Multi-granularity time**: if data mixes event timestamps (ms) and day-level aggregates, normalise to single granularity (or split on coarser one) before sorting — global sort otherwise places aggregates non-deterministically.
- **Multi-entity datasets (e.g. per-patient time-series)**: global temporal sort does NOT isolate entity-level ordering — patient A's future can land before patient B's past. Combine with Patient-Level Split above: group by entity, sort within each group, allocate each group's rows to splits independently. Use Patient-Level Split as outer split, apply this temporal sort *within* each group.

</split-strategies>

<class-imbalance>

## Detection

```python
from collections import Counter

distribution = Counter(labels)
majority = max(distribution.values())
minority = min(distribution.values())
ratio = majority / minority  # >10x severe; 2-10x moderate
```

## Handling Strategies (in order of preference)

1. **Collect more data** for underrepresented classes
2. **Weighted sampling**: `WeightedRandomSampler` to balance batches
3. **Weighted loss**: `nn.CrossEntropyLoss(weight=class_weights)`
4. **SMOTE/augmentation** for minority classes
5. **Threshold tuning** on classifier output (classification only)

</class-imbalance>

<dataloader-patterns>

## Recommended Configuration

For throughput settings (`num_workers`, `pin_memory`, `prefetch_factor`, `persistent_workers`), see `foundry:perf-optimizer`; skip if foundry absent. Core integrity settings:

```python
DataLoader(
    dataset,
    batch_size=32,
    drop_last=True,
    collate_fn=None,
    worker_init_fn=...,  # set per-worker seed for reproducibility
)
```

## Reproducible DataLoader

```python
def worker_init_fn(worker_id):
    worker_seed = torch.initial_seed() % (2**32)
    numpy.random.seed(worker_seed)
    random.seed(worker_seed)


loader = DataLoader(
    dataset, worker_init_fn=worker_init_fn, generator=torch.Generator().manual_seed(42)
)
```

</dataloader-patterns>
