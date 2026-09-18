<!-- Loaded by foundry:qa-specialist (sonnet + high) -->

# ML Testing (foundry:qa-specialist specialized guidance)

Read only for ML model testing: PyTorch, TensorFlow, JAX, model inference, training-loop verification, or tensor-shape checks. Skip non-ML Python tasks.

> **Framework scope**: patterns below use PyTorch. TF/JAX: adapt to `tf.debugging`/`jax.test_util` — seeding, assertion APIs, DataLoader patterns differ.

## Tensor Assertions (PyTorch)

```python
import torch
import torch.testing as tt


def test_model_output_shape():
    model = MyModel(num_classes=10)
    torch.manual_seed(0)
    batch = torch.randn(4, 3, 224, 224)
    output = model(batch)
    assert output.shape == (4, 10), f"Expected (4, 10), got {output.shape}"


def test_numerical_stability():
    tt.assert_close(actual, expected, rtol=1e-4, atol=1e-6)
```

## NumPy Assertions

```python
import numpy as np


def test_transform_preserves_range():
    np.random.seed(0)
    data = np.random.rand(100, 3)
    result = normalize(data)
    np.testing.assert_allclose(result.mean(axis=0), 0.0, atol=1e-6)
    np.testing.assert_allclose(result.std(axis=0), 1.0, atol=1e-6)
```

## GPU / CUDA Tests

> `reset_random_seeds` autouse fixture — see `_shared/pytest-config.md` for the canonical definition.

Mark GPU tests `@pytest.mark.gpu` + `@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA not available")` — CPU-only runners skip them without breaking the suite.

## DataLoader Testing

> **Determinism with `num_workers > 0`**: worker processes each have their own RNG state. Reproducibility tests are non-deterministic unless every worker is seeded via `worker_init_fn`. The `reset_random_seeds` autouse fixture seeds the main process only — does NOT propagate into worker processes. Pass a `worker_init_fn` and a `torch.Generator` to `DataLoader`, or restrict tests to `num_workers=0`.

```python
import random
import numpy as np
import torch


def seed_worker(worker_id: int) -> None:
    """Standard PyTorch worker seeding — call from DataLoader(worker_init_fn=seed_worker)."""
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)


def make_dataloader(dataset: torch.utils.data.Dataset, seed: int, num_workers: int = 0):
    g = torch.Generator()
    g.manual_seed(seed)
    return torch.utils.data.DataLoader(
        dataset,
        batch_size=4,
        num_workers=num_workers,
        worker_init_fn=seed_worker if num_workers > 0 else None,
        generator=g,
    )


def test_dataloader_reproducibility():
    # Seed explicitly — don't rely on autouse fixture ordering for dataset values
    torch.manual_seed(42)
    ds = torch.utils.data.TensorDataset(torch.randn(16, 3, 224, 224))
    loader1 = make_dataloader(ds, seed=42)
    loader2 = make_dataloader(ds, seed=42)
    # zip_longest: divergent batch counts (e.g. drop_last asymmetry) raise rather than silently truncate
    from itertools import zip_longest

    sentinel = object()
    for pair1, pair2 in zip_longest(loader1, loader2, fillvalue=sentinel):
        assert pair1 is not sentinel and pair2 is not sentinel, "loader batch counts diverged"
        (batch1,) = pair1
        (batch2,) = pair2
        torch.testing.assert_close(batch1, batch2)


def test_dataloader_no_nan():
    ds = torch.utils.data.TensorDataset(torch.randn(16, 3, 224, 224))
    loader = make_dataloader(ds, seed=42)
    for (batch,) in loader:
        assert not torch.any(torch.isnan(batch)), "NaN in batch"
        assert not torch.any(torch.isinf(batch)), "Inf in batch"
```

## Model Mode Assertions

```python
def test_evaluate_does_not_change_model_mode():
    """evaluate() must not leave model in train mode."""
    model = MyModel()
    model.train()
    evaluate(model, loader, criterion)
    assert not model.training, (
        "evaluate() must call model.eval() and not restore train mode"
    )


def test_evaluate_does_not_modify_parameters():
    """evaluate() must not update weights (torch.no_grad() contract)."""
    model = MyModel()
    params_before = {k: v.clone() for k, v in model.named_parameters()}
    evaluate(model, loader, criterion)
    for k, v in model.named_parameters():
        torch.testing.assert_close(
            v, params_before[k], msg=f"Parameter {k} changed during evaluate()"
        )
```
