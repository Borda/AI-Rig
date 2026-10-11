<!-- file: pytest-config.md — consumers: qa-specialist.md -->

## pyproject.toml Configuration

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = [
  "--strict-markers",
  "--strict-config",
  "-ra",
]
markers = [
  "slow: marks tests as slow (deselect with '-m not slow')",
  "integration: requires external services or real I/O",
  "gpu: requires CUDA-capable GPU",
]
filterwarnings = [
  "error",
  "ignore::DeprecationWarning:third_party_module",
]

[tool.coverage.run]
source = ["src"]
omit = ["*/tests/*", "*/_vendor/*"]

[tool.coverage.report]
fail_under = 85
show_missing = true
```

## conftest.py Patterns

```python
# tests/conftest.py
import pytest
import numpy as np


@pytest.fixture(autouse=True)
def reset_random_seeds():
    np.random.seed(42)
    import random

    random.seed(42)
    try:
        import torch

        torch.manual_seed(42)
        torch.cuda.manual_seed_all(42)
    except ImportError:
        pass


@pytest.fixture
def tmp_data_dir(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "labels").mkdir()
    return tmp_path
```
