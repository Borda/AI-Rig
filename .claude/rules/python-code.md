---
description: Python coding standards — docstrings, deprecation, version policy, library API awareness, PyTorch AMP
paths:
  - '**/*.py'
---

## Docstring Style

- **Always Google style (Napoleon)** — no exceptions unless the user explicitly requests otherwise
- Never switch to NumPy style based on project type, existing code, or own judgement
- Every public function/class/module needs a docstring; at least one `Examples` section per public function — omit only when the user **explicitly says to skip examples** (e.g., "no examples needed", "skip the Examples section"); a request for brevity or a "minimal" docstring does NOT qualify

## Deprecation

**Version check first**: before generating any deprecation code, run `python3 -c "import deprecate; print(deprecate.__version__)"`. If the installed version differs from what Claude knows, read `help(deprecate)` or the project CHANGELOG before generating code — do not assume Claude knows the latest API. Do **not** upgrade pyDeprecate in projects that are on an older version and working fine.

**Never use `warnings.warn` for deprecation** — use `pyDeprecate` exclusively. Import from `deprecate`, not `pyDeprecate`:

**Deprecation lifecycle**: deprecate in minor release → keep for ≥1 minor cycle → remove in next major.

```python
from deprecate import deprecated  # correct
```

If `pyDeprecate` is not installed, add it — do not fall back to `warnings.warn`.

### Function / method deprecation

Both parts below are required — decorator alone is incomplete:

```python
from deprecate import deprecated


@deprecated(target=new_fn, deprecated_in="X.Y", remove_in="Z.W")
def old_fn(*args, **kwargs):
    """One-line summary.

    Args:
        ...

    Examples:
        ...
    """
    ...
```

### Class deprecation — use `deprecated_class` (v0.6.0+) <!-- verified: 2026-04-06; re-verify if pyDeprecate is upgraded past 0.6.x -->

**Do NOT apply `@deprecated` directly to a class** — use `deprecated_class`. Applying `@deprecated` to a class emits a `UserWarning` and silently delegates, but `deprecated_class` is the explicit, correct API for Enum, dataclass, and plain classes.

```python
from deprecate import deprecated_class


@deprecated_class(target=NewClass, deprecated_in="X.Y", remove_in="Z.W")
class OldClass: ...
```

`deprecated_class` wraps the class in a transparent proxy — per installed docs, attribute access, method calls, `isinstance()`, and instantiation all forward to `NewClass` with a `FutureWarning`.

### Instance deprecation — use `deprecated_instance` (v0.6.0+) <!-- verified: 2026-04-06; re-verify if pyDeprecate is upgraded past 0.6.x -->

```python
from deprecate import deprecated_instance

old_obj = deprecated_instance(new_obj, deprecated_in="X.Y", remove_in="Z.W")
```

## Python Version Policy

- Python 3.10 reaches EOL Oct 2026 — minimum for new projects is **3.11** (Python 3.11 reaches EOL Oct 2027; check [endoflife.date/python](https://endoflife.date/python) <!-- verified: 2026-04-08 --> for current schedule) <!-- re-verify: when Python 3.11 reaches EOL (Oct 2027) — bump minimum to 3.12 -->
- **Before writing any Python code**: read `pyproject.toml` (or `setup.cfg`/`setup.py`) to find `requires-python`; use only syntax/APIs available in that minimum version
- Version-gated features — **read pyproject.toml first if any of these are requested**:
  - `match` statement (3.10+)
  - `TypeAlias` (3.10+)
  - `typing.ParamSpec` (3.10+)
  - `tomllib` (3.11+) — use `tomli` backport if requires-python < 3.11
  - `ExceptionGroup` (3.11+)
  - `Self` type (3.11+)
- Use `target-version = "py311"` in ruff/mypy configs for new projects

## Library API Awareness

Claude's training data has a fixed cutoff — any library released or substantially updated after that point may have APIs Claude doesn't know about.

**Before using any third-party library feature**:

1. Check the installed version: `python3 -c "import <pkg>; print(<pkg>.__version__)"` or `pip show <pkg>`
2. Compare against what Claude was trained on: Claude's training cutoff is noted in the system context; any library with active development after that date may have new or changed APIs
3. If the installed version is newer than Claude's training snapshot: read the library's CHANGELOG or online docs first; `python3 -c "import <pkg>; help(<pkg>)"` is a fallback for offline inspection
4. Use the API that matches the **installed** version — do not assume Claude's training knowledge is current

**Never suggest upgrading a library** solely because Claude doesn't recognise a newer API. The project already has a version pinned for a reason — learn that version's API from docs; do not force updates on stable/stale projects.

## PyTorch AMP

- `torch.cuda.amp.autocast` deprecated since PyTorch 2.4 — stable replacement:
  `torch.amp.autocast('cuda', ...)` and `torch.amp.GradScaler('cuda')`
  (verify current stable release at pytorch.org before citing specific versions) <!-- verified: 2026-04-06 -->

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
