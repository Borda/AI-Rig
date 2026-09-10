<!-- file: submission.md — selected by composition.md -->

# Submission section contract

Generate final section from grounded sample-submission or competition output evidence.

## CSV classification/regression

- Read grounded sample submission.
- Join predictions by stable ID when ID column exists; do not rely on incidental row order.
- Assign exact grounded target column(s).
- Preserve required column order and row count.
- Write `submission.csv` without index.

## Detection

- Use grounded coordinate order, scale, class mapping, score precision, and empty-detection representation.
- Format one prediction record per required sample ID.
- Validate boxes are finite, ordered, and within expected image bounds.

## Segmentation or file outputs

- Restore original spatial shape.
- Use grounded file format, dtype, naming, compression, and directory structure.
- Validate number of written files against expected sample IDs.

## Submission lens

Always verify before reporting completion:

- row/file count equals expected test count;
- columns/schema and order match grounded evidence;
- IDs are unique and cover expected set;
- predictions contain no unintended NaN/Inf;
- values, labels, boxes, and shapes satisfy grounded constraints.

End CSV workflows with `# ! head submission.csv`; use equivalent listing/schema check for non-CSV outputs. Display small sample and print final path.
