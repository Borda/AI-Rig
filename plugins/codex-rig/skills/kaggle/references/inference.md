<!-- file: inference.md — selected by composition.md -->

# Inference section contract

Use context selected by `composition.md`:

- `attached`: Section 7 of full notebook, after training.
- `standalone`: Sections 3–6 of inference-only notebook, starting from grounded checkpoint.

## Attached context

Include both paths:

1. Run inference with trained in-memory model under `torch.no_grad()`.
2. Load best saved checkpoint/model artifact in separate cell and run equivalent prediction smoke check.

Use test loader from training pipeline, move only inference outputs to CPU, retain stable sample identifiers, and verify prediction count/shape before submission.

## Standalone context

### Section 3: Load model

Ground checkpoint path, format, model class, and required constructor arguments. Prefer importing `<competition>_model.py` emitted by training. If unavailable, define complete model class with verified imports rather than inventing API.

Choose loader by evidence:

- Lightning `.ckpt`: `Model.load_from_checkpoint(...)` with importable class.
- State dict `.pt`/`.pth`: construct verified architecture, load state dict, and check missing/unexpected keys.
- Serialized module: use `torch.load(..., map_location=DEVICE)` only when artifact is known to contain full trusted module.
- Custom detector/MONAI model: verify installed constructor and checkpoint contract first.

Fail clearly when no checkpoint matches; never index `sorted(...)[-1]` without empty-match guard. Set evaluation mode, move to selected device, and print model type, device, and parameter count.

### Section 4: Test data

Build label-free test Dataset/DataLoader or grounded modality equivalent:

- preserve sample ordering and stable IDs;
- use evaluation transforms matching training;
- set `shuffle=False`;
- assert batch shape and dtype;
- print sample and batch counts;
- assert non-empty test data before constructing or iterating loader; do not use conditional empty-data branch.

Detection may use single-image iteration when required by verified predictor API. Volumetric pipelines must preserve original shape metadata for output restoration.

### Section 5: Inference loop

Run under `torch.no_grad()` and choose output activation/decoding from grounded task:

- binary classification: sigmoid only when model returns logits;
- multiclass: softmax/argmax according to submission requirements;
- regression: retain scalar/vector predictions without classification transforms;
- detection: apply verified confidence filtering and class-aware NMS when model does not already do so;
- segmentation: restore predictions to grounded original dimensions with appropriate interpolation.

Collect predictions and IDs, then assert count, shape, dtype, finiteness, and expected range before post-processing. CPU transfer is allowed here, outside training loop.

### Section 6: Post-processing

Apply only post-processing justified by metric and output contract:

- threshold calibration for classification;
- box rescaling and formatting for detection;
- morphology/component filtering for segmentation;
- inverse transforms for normalized regression targets.

Keep parameters in just-in-time config cell. Define helpers immediately before use.

## Inference lens

Assert prediction sample is available, then show it; print prediction/ID counts and shapes, check NaN/Inf and range constraints, and compare attached in-memory versus reloaded path when both exist. Do not make required lens conditional.
