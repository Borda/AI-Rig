---
description: Python coding standards — docstrings, deprecation, version policy, PyTorch AMP
paths:
  - '**/*.py'
---

## Docstring Style

- **Always Google style (Napoleon)** — no exceptions unless the user explicitly requests otherwise
- Never switch to NumPy style based on project type, existing code, or own judgement
- Every public function/class/module needs a docstring; at least one `Examples` section per public function

## Deprecation

Use `pyDeprecate` (Borda's library — https://pypi.org/project/pyDeprecate/ <!-- verified: Borda-owned pypi package -->) over raw `warnings.warn`:

```python
@deprecated(target=new_fn, deprecated_in="X.Y", remove_in="Z.W")
def old_fn(...): ...
```

- Also add `.. deprecated:: X.Y.Z` Sphinx directive in the docstring
- Deprecation lifecycle: deprecate in minor release → keep for ≥1 minor cycle → remove in next major

## Python Version Policy

- Python 3.9 reached EOL Oct 2025 — minimum for new projects is 3.10
- **Before writing any Python code**: read `pyproject.toml` (or `setup.cfg`/`setup.py`) to find `requires-python`; use only syntax/APIs available in that minimum version
- Version-gated features: `match` (3.10+), `TypeAlias` (3.10+), `tomllib` (3.11+), `ExceptionGroup` (3.11+), `Self` type (3.11+), `typing.ParamSpec` (3.10+)
- Use `target-version = "py310"` in ruff/mypy configs for new projects

## PyTorch AMP

- `torch.cuda.amp.autocast` deprecated in PyTorch 2.4
- Use `torch.amp.autocast('cuda', ...)` and `torch.amp.GradScaler('cuda')`

## Security

- `pickle.load` / `torch.load` on external data require `weights_only=True`

## Code Quality Rules

- Type annotations on all public interfaces
- No mutable default arguments
- No broad `except:` without re-raising or logging
- No `import *` — always explicit imports
- No global mutable state — use dependency injection
- `__all__` in `__init__.py` to define public API surface
- Prefer composition over deep inheritance
