<!-- file: style-rules.md — applied by composition.md, on top of ../../../shared/notebook-style.md -->

Apply `../../../shared/notebook-style.md` (repo-wide notebook standard) to every generated script first, then these Kaggle-only additions:

1. **Narrative frames the competition, not a generic goal**: where `notebook-style.md`'s narrative-depth anchor asks "how it advances the notebook's stated goal", state that goal in Kaggle terms in every section — target metric, leaderboard placement, submission quality — not a generic ML objective.
2. **Submission-format lens**: the lens cell that follows submission-file generation must confirm the file matches the competition's exact `sample_submission.csv` schema (column names, row count, dtypes) before the notebook ends — never assume the schema from memory, read it from grounded evidence.
