<!-- Loaded by foundry:perf-optimizer (opus + high) -->

# ML / GPU Profiling (foundry:perf-optimizer specialized guidance)

Read only for GPU/ML profiling: CUDA, PyTorch training, model inference, DataLoader bottlenecks, or mixed precision. Skip pure CPU/IO profiling.

## PyTorch Profiler

```python
import torch
from torch.profiler import profile, record_function, ProfilerActivity

with profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
    record_shapes=True,
    profile_memory=True,
    with_stack=True,
) as prof:
    with record_function("model_inference"):
        output = model(input_batch)

print(prof.key_averages().table(sort_by="cuda_time_total", row_limit=20))
prof.export_chrome_trace("trace.json")
```

## GPU Utilization Monitoring

```bash
nvidia-smi dmon -s u
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.free --format=csv -l 1

uv tool install nvitop  # or: pip install nvitop
nvitop
```

> **Platform notes** — the nvidia-smi and CUDA calls above apply only to NVIDIA GPUs:
>
> - **Apple MPS**: use `torch.profiler` with `torch.device("mps")`; no nvidia-smi; monitor via Activity Monitor (GPU History) or Instruments (Metal System Trace)
> - **AMD ROCm**: replace `nvidia-smi` with `rocm-smi`; `torch.profiler` with `ProfilerActivity.CPU` works; omit `ProfilerActivity.CUDA`
> - **Intel Arc**: use Intel VTune Profiler or `torch.profiler` with XPU backend; no nvidia-smi

## DataLoader Bottleneck Detection

`data_fraction = data_time / step_time`; `cpu_bound = data_fraction > 0.3` means pipeline CPU-bound. Increase `num_workers`; add `pin_memory=True`, `persistent_workers=True`; or use faster augmentations (e.g. albumentations) when they dominate `data_time`.

## DataLoader Optimization

**Throughput params** (`num_workers`, `persistent_workers`, `pin_memory`, `prefetch_factor`): owned by `foundry:perf-optimizer` — tune from `data_fraction` ratio (see Detection above). Set `num_workers > 0`, `pin_memory=True`, `persistent_workers=True` as first fix when DataLoader bottlenecks. **Correctness/reproducibility** (`worker_init_fn` seeding, split isolation, leakage detection): see `research:data-steward` (requires `research` plugin). If `research` plugin unavailable, apply throughput tuning only, flag correctness audit out-of-scope.

## Mixed Precision (torch.amp — PyTorch 2.0+)

```python
# PyTorch 2.0+: device-agnostic API (torch.cuda.amp deprecated in 2.4)
from torch.amp import autocast, GradScaler

scaler = GradScaler("cuda")
for batch in loader:
    with autocast("cuda", dtype=torch.float16):
        output = model(batch)
        loss = criterion(output, targets)
    scaler.scale(loss).backward()
    scaler.step(optimizer)
    scaler.update()
# fp16: ~50% memory reduction; faster on Tensor Core GPUs
# measure: torch.cuda.memory_allocated() / torch.cuda.max_memory_allocated()
```

```python
# bfloat16: no GradScaler needed — bfloat16 has float32 exponent range, no underflow risk
with autocast("cuda", dtype=torch.bfloat16):
    output = model(batch)
    loss = criterion(output, targets)
loss.backward()
optimizer.step()
```

## Distributed Training Profiling

Measure all-reduce time to profile DDP overhead. Common bottlenecks:

- Gradient bucket too small → too many all-reduce calls: `DDP(model, bucket_cap_mb=25)` (increase for large models)
- Uneven data distribution → fast workers wait for slow: `DistributedSampler(drop_last=True)` equalizes batches # NOTE: drops up to (world_size-1) samples per epoch — do not use in eval loops
- SyncBatchNorm overhead in small-batch regime: only use `sync_batchnorm` when `batch_per_gpu < 16`

## 3D Volumetric Data Performance

See `research:data-steward` (requires `research` plugin) — contains mmap (`np.load(..., mmap_mode="r")`), HDF5 chunk alignment, patch extraction patterns.

## torch.compile

```python
# PyTorch 2.0+
model = torch.compile(model)  # default (inductor backend)
model = torch.compile(model, mode="reduce-overhead")  # small batches
model = torch.compile(model, mode="max-autotune")     # max speed, slower compile
model = torch.compile(model, dynamic=True)            # prevents per-shape recompilation
# helps: repeated forward passes, simple/regular ops, training loops
# hurts: very dynamic shapes, heavy Python control flow, first inference (JIT cost)
```
