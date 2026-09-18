<!-- Loaded by research:data-steward (sonnet + medium) -->

# Storage and Loading Patterns — data-steward reference

> Reference document, NOT an agent definition — contextual material for `research:data-steward`.

Loaded by data-steward in `acquisition` mode before Step 2. Contains: DVC versioning, Polars tabular loading, HuggingFace datasets, 3D volumetric data loading.

<storage-and-loading-patterns>

## Data Version Control (DVC)

```bash
# Verify remote configured BEFORE push — `dvc push` exits 0 with "no remote storage"
# when unconfigured; .dvc stub records hash nothing can resolve.
dvc remote list  # must list at least one; if empty: dvc remote add -d myremote s3://bucket/path

dvc add data/raw/dataset.zip
git add data/raw/dataset.zip.dvc .gitignore
dvc push --verbose  # --verbose surfaces upload errors bare push swallows

git checkout v1.2.0
dvc checkout
```

## Polars (modern pandas alternative for tabular data)

```python
import polars as pl

df = pl.scan_csv("data.csv").filter(pl.col("label") != -1).collect()  # lazy eval

train = df.filter(pl.col("subject_id").is_in(train_subjects))
test = df.filter(pl.col("subject_id").is_in(test_subjects))
```

Use Polars over pandas: >1M rows, lazy eval needed, or speed matters.

## HuggingFace datasets

```python
from datasets import load_dataset

ds = load_dataset("cifar10", split="train[:10%]")
ds = load_dataset("imagenet-1k", streaming=True)  # streaming for large datasets
ds.save_to_disk("data/processed/")
ds = load_from_disk("data/processed/")
```

## 3D Volumetric Data Loading (medical imaging)

Patch-based 3D Dataset: `self.volumes` + `self.patch_size` in init; `__getitem__` = random patch (train), center crop (val/test) — returns `{"image": patch_array}`.

Key considerations, volumetric data:

- **Memory**: volumes = GBs — use lazy loading:

  ```python
  volume = np.load("scan.npy", mmap_mode="r")  # "r" = read-only, "r+" = read-write

  import h5py

  # 'w' TRUNCATES existing file — use 'a' to append. NEVER open 'w' while any reader
  # (DataLoader worker, debug session) is open — concurrent 'w' corrupts active reads.
  with h5py.File("data.h5", "w") as f:
      # Align chunk size to patch size (e.g. 64x64x64) for minimal partial reads
      f.create_dataset("volumes", shape=(N, D, H, W), chunks=(1, 64, 64, 64), dtype="float32")
      # POPULATE after create_dataset — unwritten datasets return all-zeros silently.

  with h5py.File("data.h5", "r") as f:  # 'r' safe for concurrent multi-worker DataLoaders
      ds = f["volumes"]
      patch = ds[idx, z : z + 64, y : y + 64, x : x + 64]
  ```

- **Patch extraction**: train on patches, infer with sliding window + overlap for boundary smoothing

- **Orientation**: normalize to canonical (RAS/LPS) before training

- **Spacing**: resample to isotropic voxel spacing if model needs uniform resolution

</storage-and-loading-patterns>
