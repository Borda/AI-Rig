<!-- file: foundation.md — selected by composition.md -->

# Notebook foundation contract

Generate header, environment setup, imports, and path constants. Use variant selected by `composition.md`.

## Variant matrix

| Variant | Title suffix | Package setup | Scope |
| -- | -- | -- | -- |
| `full` | `⚡PTL + <ModelLibrary>` for neural training | online download by default; frozen offline packages when requested | training and inference dependencies |
| `eda-only` | `— EDA` | online download; never offline-only | exploration dependencies only |
| `inference-only` | `— Inference` | frozen offline packages | inference dependencies only |

## Section 1: Header and setup

Start with a `# %% [markdown]` cell containing grounded competition title, 2–3 sentences describing selected scope and approach, and competition URL when known. In inference-only mode, name grounded checkpoint path or Kaggle input dataset.

Follow with one setup cell:

- Online: `# ! pip download -q <library> --dest frozen_packages/`, then install from that directory with online fallback.
- Offline: `# ! cp -r ../input/python-packages/frozen_packages .`, then install with `--no-index --find-links frozen_packages/` and clearly disclosed fallback only when internet is allowed.
- Put modality-specific packages selected by `modality-dispatch.md` here, never later in notebook.
- In inference-only mode, exclude training callbacks, logger packages, and training metrics not needed to deserialize model.

## Section 2: Imports and paths

Use one `# %%` cell containing imports and global paths only:

- Standard library first (`glob`, `os`, `Path` as needed), then NumPy/pandas/plotting, then torch/model packages, then sklearn/XGBoost.
- Import `tqdm` with `from tqdm.auto import tqdm` and use it for every visible notebook progress bar (data scans, training-adjacent loops, and inference). Never use Rich progress bars.
- Always import `torch` for neural inference/training notebooks.
- Suppress only specific noisy warning category; do not blanket-ignore exceptions.
- Define grounded `PATH_DATASET`, `PATH_OUTPUT`, and when needed `PATH_MODELS`/`PATH_CHECKPOINT` as ALL_CAPS.
- Print package and device versions immediately after imports.
- For neural training, call `pl.seed_everything(42)` and seed every non-Lightning split/sampler explicitly.

Keep stage configuration out of this cell. EDA, data, model, training, and inference constants belong immediately before their respective stage.

## Foundation lens

Print resolved paths, device, and package versions. Check required input paths without claiming Kaggle-only paths exist locally during generation.
