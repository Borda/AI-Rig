---
description: pytest test design standards — structure, fixtures, parametrization
paths:
  - tests/**/*.py
  - '**/test_*.py'
---

## Adding Tests — Process

**New features: test-first** — see TDD below.

1. Check existing tests for relevant scope first
2. Investigate if parametrizing existing tests (minimal body changes) suffices
3. Only then create new test functions/cases

## What to Test — Priority Order

1. **Function goals / docs / intended user application** — verify contract and normal use
2. **Edge cases** — boundary values, empty inputs, extreme sizes, unusual combinations
3. **Exception handling** — only after above; don't lead with error-path tests
   - When adding exception-handling tests, include at least one contract/normal-use test in same commit or point to existing — no error-path-only test files.

## Test Structure

- **Arrange-Act-Assert (AAA)**: one setup block, one `act`, one assertion group per test — never second `act` in same test
- Each test validates exactly one scenario
- No `if`/`for` logic in test bodies; exception: list-comprehension or generator used solely to build `@pytest.mark.parametrize` args, spanning fewer than 30% of lines in `parametrize` decorator call
- Parametrize aggressively — 3+ test functions with same structure → `@pytest.mark.parametrize`
- Group topic-related tests into class; class name carries unit (and optionally condition) so method names describe expected outcome only

## File Layout

Mirror `src/` layout in `tests/unit/`: `src/foo/bar.py` → `tests/unit/foo/test_bar.py`

## Seeding / Randomness

- Never seed RNG except inside `autouse=True` fixture — not in test bodies, module level, or non-autouse fixtures
- If fixture needed project-wide, place in `tests/conftest.py` — don't duplicate per file. Per-file placement only when file needs different seed strategy.
- Use pytest fixture resetting all RNG sources: `torch.manual_seed`, `numpy.random.seed`, `random.seed`, `torch.cuda.manual_seed_all`
- Fixture must use `autouse=True`

```python
@pytest.fixture(autouse=True)
def reset_random_seeds():
    """Ensure reproducible random state for every test."""
    import random
    random.seed(42)
    try:
        import numpy as np; np.random.seed(42)
    except ImportError:
        pass
    try:
        import torch; torch.manual_seed(42); torch.cuda.manual_seed_all(42)
    except ImportError:
        pass
```

## CUDA Skip Pattern

Use decorator form, not inline `if`:

```python
@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA not available")
def test_cuda_inference(): ...
```

## Docstrings

- Every test function/method needs at least one-line docstring (max 120 chars)
- Complex tests: include scenario being covered
- Module-level docstrings required

## Helpers in Tests

- Helper with no shared logic across cases → split into separate dedicated functions, not single branching helper
- Shared logic only → shared function

## TDD for New Features

Write tests before implementation; tests define contract.

## Doctests

Doctests live in **source files** (`src/**/*.py`), not test files — part of module docs, not test suite. Run with:

```bash
python -m pytest --doctest-modules src/
```

Don't rely on `tests/**/*.py` globs for doctests — missed. Add `--doctest-modules src/` explicitly to pytest invocation or `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "--doctest-modules"
testpaths = ["src", "tests"]
```

## Baseline Gate

All existing tests must pass before adding new code.
