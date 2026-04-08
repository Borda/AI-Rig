---
description: pytest test design standards — structure, fixtures, parametrization
paths:
  - tests/**/*.py
  - '**/test_*.py'
---

## Adding Tests — Process

**For new features, use the test-first approach** — see TDD section below.

1. Check all existing tests for the relevant scope first
2. Investigate if adding parametrization to existing tests (with minimal body changes) is sufficient
3. Only then create completely new test functions/cases

## What to Test — Priority Order

1. **Function goals / docs / intended user application** — tests that verify the contract and normal use
2. **Edge cases** — boundary values, empty inputs, extreme sizes, unusual combinations
3. **Exception handling** — only after the above are covered; don't lead with error-path tests

## Test Structure

- **Arrange-Act-Assert (AAA)**: one setup block, one `act`, one assertion group per test — never a second `act` in the same test
- Each test validates exactly one scenario
- No `if`/`for` logic in test bodies; exception: parametrize value generation when it covers \<30% of cases and enables a significantly larger parametrize list
- Parametrize aggressively — 3+ test functions with the same structure → `@pytest.mark.parametrize`
- Group topic-related tests into a class; class name carries unit (and optionally condition) so method names describe the expected outcome only

## File Layout

Mirror `src/` layout in `tests/unit/`: `src/foo/bar.py` → `tests/unit/foo/test_bar.py`

## Seeding / Randomness

- Never seed RNG anywhere except inside an `autouse=True` fixture — not in test bodies,
  not at module level, not in non-autouse fixtures
- Use a pytest fixture that resets all RNG sources: `torch.manual_seed`, `numpy.random.seed`, `random.seed`, `torch.cuda.manual_seed_all`
- Fixture should use `autouse=True`

```python
@pytest.fixture(autouse=True)
def reset_random_seeds():
    """Ensure reproducible random state for every test."""
    import random

    random.seed(42)
    try:
        import numpy as np

        np.random.seed(42)
    except ImportError:
        pass
    try:
        # ML stack: conditionally reset torch RNG — fixture works without it
        import torch

        torch.manual_seed(42)
        torch.cuda.manual_seed_all(42)
    except ImportError:
        pass
```

## CUDA Skip Pattern

Use the decorator form, not inline `if`:

```python
@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA not available")
def test_cuda_inference(): ...
```

## Docstrings

- Every test function/method needs at least a one-line docstring (max 120 chars)
- Complex tests: include the scenario the test is supposed to cover
- Module-level docstrings required

## Helpers in Tests

- If a helper has no shared logic across cases, split it into separate dedicated functions rather than a single branching helper
- Shared logic only → shared function

## TDD for New Features

For new features, follow Test-Driven Development — write tests before implementation; tests define the contract.

## Doctests

Doctests live in **source files** (`src/**/*.py`), not in test files — they are part of the module's documentation, not the test suite. Run them with:

```bash
python -m pytest --doctest-modules src/
```

Do not rely on `tests/**/*.py` globs to pick up doctests — they will be missed. Add `--doctest-modules src/` explicitly to the pytest invocation or to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "--doctest-modules"
testpaths = ["src", "tests"]
```

## Baseline Gate

All existing tests must pass before adding new code.
