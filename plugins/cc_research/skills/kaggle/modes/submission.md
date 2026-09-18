<!-- file: submission.md — selected by composition.md -->

# Submission section contract

Generate final section from grounded sample-submission or competition output evidence.

## CSV classification/regression

- Read grounded sample submission.
- Join predictions by stable ID if ID column exists; never rely on incidental row order.
- Assign exact grounded target column(s).
- Preserve required column order and row count.
- Write `submission.csv` without an index.

## Detection

- Use grounded coordinate order, scale, class mapping, score precision, empty-detection representation.
- Format one prediction record per required sample ID.
- Validate boxes finite, ordered, within expected image bounds.

## Segmentation or file outputs

- Restore original spatial shape.
- Use grounded file format, dtype, naming, compression, directory structure.
- Validate written file count against expected sample IDs.

## Submission lens

Verify before reporting completion:

- row/file count equals expected test count;
- columns/schema and order match grounded evidence;
- IDs unique, cover expected set;
- predictions contain no unintended NaN/Inf;
- values, labels, boxes, shapes satisfy grounded constraints.

End CSV workflows with `# ! head submission.csv`; equivalent listing/schema check for non-CSV. Display small sample, print final path.
