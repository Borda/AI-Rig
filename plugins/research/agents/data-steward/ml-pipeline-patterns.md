# ML Pipeline Patterns — data-steward reference

Loaded by data-steward agent in `pipeline-audit` mode before Step 1.
Contains: split strategies for grouped/temporal data, class imbalance handling, DataLoader integrity patterns.

\<split_strategies>

## Patient-Level Split (medical imaging — CRITICAL)

```python
# Medical datasets: multiple images per patient — MUST split by patient_id
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

patient_ids = metadata["patient_id"].values
gss = GroupShuffleSplit(n_splits=1, test_size=0.3, random_state=42)
train_idx, temp_idx = next(gss.split(metadata, groups=patient_ids))

# Verify zero patient overlap
train_patients = set(metadata.iloc[train_idx]["patient_id"])
test_patients = set(metadata.iloc[temp_idx]["patient_id"])
assert train_patients.isdisjoint(test_patients), "PATIENT LEAK DETECTED"
```

Checklist for medical imaging datasets:

```markdown
[ ] Splits are by patient/subject ID, never by image/slice
[ ] DICOM metadata checked for hidden identifiers (StudyInstanceUID links images)
[ ] Multi-site data: stratify by site to avoid site-specific bias
[ ] Temporal data: no future scans leaking into training from same patient
[ ] Annotation consistency: inter-reader variability measured (Fleiss' kappa)
```

Use `Bash` to verify zero patient overlap between splits:

```bash
python -c "
import pandas as pd
train = pd.read_csv('splits/train.csv')
test = pd.read_csv('splits/test.csv')
overlap = set(train['patient_id']) & set(test['patient_id'])
print(f'Overlap: {len(overlap)} patients' if overlap else 'No patient overlap')
"
```

## Temporal Split (time-series or streaming data)

Sort by time, split sequentially: 70% train / 15% val / 15% test using sequential index offsets (no shuffling).

\</split_strategies>

\<class_imbalance>

## Detection

```python
from collections import Counter

distribution = Counter(labels)
majority = max(distribution.values())
minority = min(distribution.values())
ratio = majority / minority
# > 10x: severe, needs explicit handling
# 2-10x: moderate, monitor metrics per class
```

## Handling Strategies (in order of preference)

1. **Collect more data** for underrepresented classes
2. **Weighted sampling**: `WeightedRandomSampler` to balance batches
3. **Weighted loss**: `nn.CrossEntropyLoss(weight=class_weights)`
4. **Synthetic Minority Over-sampling Technique (SMOTE)/augmentation** for minority classes
5. **Threshold tuning** on classifier output (classification only)

\</class_imbalance>

\<dataloader_patterns>

## Recommended Configuration

See `foundry:perf-optimizer` for throughput settings (`num_workers`, `pin_memory`, `prefetch_factor`, `persistent_workers`). Core DataLoader integrity settings:

```python
DataLoader(
    dataset,
    batch_size=32,
    drop_last=True,  # prevent variable-size last batch issues
    collate_fn=None,  # specify if default collation doesn't work
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

\</dataloader_patterns>
