<!-- Loaded by foundry:doc-scribe (sonnet + medium) -->

# Specialized Docstring Patterns (foundry:doc-scribe specialized guidance)

Apply only when scoped task explicitly involves CV/ML tensor docstrings or pyDeprecate migration guides. Routine docstring/README tasks: content below is inert reference — do NOT apply checklist heuristics.

## Computer Vision (CV) / Tensor Docstring Checklist

**CV/ML projects only — strict two-category gate**: apply checklist only when function has BOTH

1. **Architectural signal** (at least one): `kernel_size`, `stride`, `padding`, `feature_map`, `dilation`, `groups` (structural CNN params)
2. **Visual-domain signal** (at least one, distinct from category 1): image dimensions (`(B, C, H, W)` shape with concrete spatial dims), pixel value range hints (`[0, 255]`, `[0, 1]`), bounding boxes, segmentation masks, or explicit `vision`/`image`/`detection`/`segmentation` keyword in docstring or surrounding context

A single param name (e.g. `image`) satisfying both categories does NOT count twice — the two signals need distinct evidence.

> **NOT-for — do not apply CV checklist to**:
>
> - Audio DSP functions (`spectrogram`, `waveform`, `frame` as STFT frame, `mel_bins`)
> - NLP / attention models (`attention_mask`, `hidden_state`, `token_ids`, even when `(B, C, H, W)`-like shapes appear)
> - Medical imaging functions unless explicitly annotated as CV pipeline stage (NIfTI/DICOM-only volumetric utilities → use medical-imaging-specific subset of checklist; see RAS/LPS qualifier in Spatial convention)
> - Generic image utilities (PIL resize, matplotlib display, OpenCV basic ops) with `image` param but no CNN architecture involvement

- **Shape**: exact dims with named axes (B, C, D, H, W) — e.g. `Shape: (B, C, H, W)`
- **Value range**: [0, 1], [0, 255], or [-1, 1]
- **Channel convention**: channel-first (PyTorch) vs channel-last (NumPy/TensorFlow (TF))
- **Spatial convention**: orientation (RAS/LPS), pixel vs world coordinates
- **dtype**: expected dtype (float32, uint8, int64)
- **Batch handling**: document if function accepts batched/unbatched inputs

## Migration Guide Template (for API deprecation cycles)

When public API deprecated with pyDeprecate, write migration guide (deprecation lifecycle and pyDeprecate usage policy → `oss:shepherd` agent (requires `oss` plugin)):

- `` ## Migrating from `old_function()` to `new_function()` `` — title with both names
- **Deprecated in**: version; **Removed in**: version
- `### Before (deprecated)` — minimal before-code example
- `### After` — equivalent after-code example
- `### Argument Mapping` — table: Old | New | Notes (renamed, removed, semantic change)
- Add to docs; hand off the CHANGELOG entry to `oss:shepherd` / `/oss:release` (requires `oss` plugin)
