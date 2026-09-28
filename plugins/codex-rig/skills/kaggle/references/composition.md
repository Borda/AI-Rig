<!-- file: composition.md — mode routing source of truth -->

# Mode composition contract

Select exactly one row. Read each named contract once, left to right; generate no unlisted section.

| Mode | Ordered contracts |
| -- | -- |
| `full` | `foundation.md(full)` → `eda.md` → `training.md` → `inference.md(attached)` → `submission.md` |
| `eda-only` | `foundation.md(eda-only)` → `eda.md` |
| `inference-only` | `foundation.md(inference-only)` → `inference.md(standalone)` → `submission.md` |

Apply `../../../shared/notebook-style.md` (repo-wide notebook standard) and `style-rules.md` (Kaggle-only delta) to every row. Load `modality-dispatch.md` only when selected section requests modality branch.

Write `.experiments/kaggle/<competition>.py`; add the `-inference` suffix only for `inference-only`.
