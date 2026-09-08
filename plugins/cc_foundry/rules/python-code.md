---
description: Python coding standards — docstrings, deprecation, version policy, library API awareness, multi-OS portability, PyTorch AMP
paths:
  - '**/*.py'
---

> **Precedence — the rule closer to the code wins.** These are plugin-level defaults. Where a project states its own convention that conflicts with anything here — in its `CLAUDE.md`, its own `rules/`, a linter/formatter config it enforces, or a consistent established style in the surrounding code — the project's convention wins. Follow it and do not "correct" the codebase toward this file. Apply these rules only where the project is silent. When a project convention looks like an oversight rather than a decision, say so once and still follow the project.

## Docstring Style

- **Google style (Napoleon)** — no exceptions unless user explicitly requests otherwise

- Never switch NumPy style based on project type, existing code, or own judgement

- Every public function/class/module needs docstring; at least one `Examples` section per public function

  - Omit only when user **explicitly says skip examples** (e.g., "no examples needed", "skip the Examples section")
  - Brevity request or "minimal" docstring does NOT qualify

- **Docstrings only where Python binds them**: module (first statement), function, class, method. A triple-quoted string trailing a module- or class-level variable is not a docstring — Python attaches it to nothing, `__doc__` stays unset. Use a `#:` comment directly above the assignment instead (Sphinx `autodoc` reads it as the attribute doc):

  ```python
  #: Per-directory memo of the (width, extension) pair that last matched.
  _FRAME_LAYOUT_HINTS: dict[Path, tuple[int, str]] = {}
  ```

- **Opening line states purpose in plain English** — move formulas, assignments, config literals, call notation, and other code-shaped detail into the description or a section below. First line says what the object is for, not how it is computed.

## Deprecation

Use `pyDeprecate`, never `warnings.warn`. Import from `deprecate` (not `pyDeprecate`). Not installed → add it, don't fall back.

| Target | API |
| -- | -- |
| function / method | `@deprecated(target=new_fn, deprecated_in="X.Y", remove_in="Z.W")` |
| class (incl. Enum, dataclass) | `@deprecated_class(target=NewClass, ...)` — v0.6.0+ |
| instance | `deprecated_instance(new_obj, ...)` — v0.6.0+ |

- **Never `@deprecated` on a class** — it emits `UserWarning` and silently delegates; `deprecated_class` is the correct API (transparent proxy: attribute access, calls, `isinstance()`, instantiation all forward with `FutureWarning`).
- Lifecycle: deprecate in minor → keep ≥1 minor cycle → remove in next major.
- Check the installed version before writing the code (`deprecate.__version__`); the API differs across versions and training memory is not evidence (§Library API Awareness). Below v0.6.0 with upgrading blocked, `deprecated_class`/`deprecated_instance` don't exist — ask before upgrading, never upgrade silently.

<!-- verified: 2026-04-06 against pyDeprecate 0.6.x; re-verify if upgraded past 0.6.x -->

## Python Version Policy

- Python 3.10 reaches EOL Oct 2026 — minimum for new projects is **3.11** (Python 3.11 reaches EOL Oct 2027; check [endoflife.date/python](https://endoflife.date/python) <!-- verified: 2026-04-08 --> for current schedule) <!-- re-verify: when Python 3.11 reaches EOL (Oct 2027) — bump minimum to 3.12 -->
- **Before writing any Python code**: read `pyproject.toml` (or `setup.cfg`/`setup.py`) to find `requires-python`; use only syntax/APIs available in that minimum version
- Version-gated features — **read pyproject.toml first if any of these requested**:
  - `match` statement (3.10+)
  - `TypeAlias` (3.10+)
  - `typing.ParamSpec` (3.10+)
  - `tomllib` (3.11+) — use `tomli` backport if requires-python < 3.11
  - `ExceptionGroup` (3.11+)
  - `Self` type (3.11+)
- Use `target-version = "py311"` in ruff/mypy configs for new projects

## Library API Awareness

Applies to **every code-touching agent**, not `foundry:sw-engineer` alone. Training data has fixed cutoff — training memory of what a package exposes is never evidence, regardless of version. Silent-drift APIs (renamed params, moved symbols, changed defaults) look plausible and pass a glance.

**Before using any third-party library feature — always, not only when version looks new**:

1. Check installed version: `python -c "import <pkg>; print(<pkg>.__version__)"` or `pip show <pkg>`
2. **Fetch the actual top-level public API** — `python -c "import <pkg>; print([n for n in dir(<pkg>) if not n.startswith('_')])"`, then `help(<pkg>.<thing>)` / read source / read versioned docs for the specific symbols used. Do this even when confident from memory — confirm the symbol, its signature, and its defaults against the installed package before writing the call
3. Use API matching the **installed** version — don't assume training knowledge current
4. Never fabricate a symbol/kwarg from memory; unverified against installed pkg = don't write it

**Never suggest upgrading library** solely because Claude doesn't recognise newer API. Project has version pinned for a reason — learn that version's API from docs; don't force updates on stable/stale projects.

## PyTorch AMP

- `torch.cuda.amp.autocast` deprecated since PyTorch 2.4 — use `torch.amp.autocast('cuda', ...)` instead
- `torch.cuda.amp.GradScaler` deprecated since PyTorch 2.4 — use `torch.amp.GradScaler('cuda')` instead
- Verify current stable release at pytorch.org when citing specific version numbers <!-- verified: 2026-04-06 -->

## Multi-OS Executables — a POSIX Assumption is a Defect

Scripts, hooks, `bin/` entry points, and CI steps run on Linux, macOS, and native Windows. Fix a portability break at its source; never skip the platform. (Test-side rules live in `python-testing.md` §Cross-OS Tests.)

- `pathlib` throughout: `Path(p).is_absolute()`, never `startswith("/")`; `PurePath(p).as_posix()` before hashing, serializing, or comparing — native separators change the digest. POSIX-absolute literals are unportable fixtures: `/host/x` resolves to `D:\host\x` on Windows.
- **Serialized telemetry or provenance paths are cross-host coordinates, not local paths.** Preserve the exact string; recognize declared POSIX and Windows absolute forms with `PurePosixPath` and `PureWindowsPath`; never convert through host `Path` before an exact comparison. Regressions exercise both forms on every host.
- Byte-asserted or hashed writes use `newline="\n"` or bytes — text mode emits CRLF on Windows.
- Sanitized subprocess `env=` keeps `SystemRoot`, `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `TEMP`, and `TMP` on win32, or the child Python aborts before running with `_Py_HashRandomization_Init: failed to get random numbers`. Temp dirs via `os.environ.get("TMPDIR") or tempfile.gettempdir()`, never a hardcoded `/tmp`.
- A CI `run:` step invoking a `.sh` file needs an explicit `shell: bash` — the Windows default shell dot-sources it and exits zero, running nothing (false green).
- Symlinks, file modes, and uid checks are capabilities: degrade in production code first.
- Green macOS is absence of regression, not Windows support. Prove Windows semantics with `PureWindowsPath` or `ntpath`; monkeypatching `os.name` does not change `pathlib`.

## Security

- `pickle.load` / `torch.load` on external data require `weights_only=True`

## Code Quality Rules

- Type annotations on all public interfaces
- No mutable default arguments
- No broad `except:` without re-raising or logging
- No `import *` — always explicit imports
- No global mutable state — use dependency injection
- `__all__` in `__init__.py` to define public API surface
- Never `__all__` in private module (`_foo.py`) — filename already marks whole module internal; `__all__` there fakes a public surface on something that has none
- Prefer composition over deep inheritance

## Structured Data — never a bare dict

A dict with known, fixed keys is the same failure as a bare string with fixed values: `d["retires"]` raises only at runtime, `d.get("retires")` silently returns `None`, and no tool flags a renamed key. Pick by what the data must do:

| Need | Use |
| -- | -- |
| Behaviour + validation, mutable, methods | `@dataclass` (`slots=True`, `frozen=True` when immutable) |
| Existing dict-shaped payload (JSON, API, `**kwargs`) — annotate without changing runtime type | `TypedDict` |
| Immutable, tuple-unpacked, hashable | `NamedTuple` |
| Field coercion/validation from untrusted input | `pydantic.BaseModel` — only where the dep already exists |

- **`dataclass` is the default** — reach for `TypedDict` only when the value must stay a real `dict` at runtime (already-parsed JSON, kwargs blob you don't own).
- `frozen=True` for anything used as a key, shared across threads, or passed to code you don't control.
- `slots=True` on dataclasses instantiated in loops or held in large collections.
- **Boundary mirrors §Closed Option Sets** — parse into the type once at the edge (`Config(**raw)`), pass the typed object inward; serialize back to plain dict only on the way out (`dataclasses.asdict`).
- 3+ positional args of the same type ⇒ the call site is unreadable and mis-orderable: make it a dataclass or force keyword-only (`*`).

## Closed Option Sets — never bare strings

Fixed, mutually exclusive options (severity, mode, status, kind, action, direction) = named type, declared once. Bare `str` re-states the set at every comparison: typo `"WANR"` evaluates false instead of raising; renamed member leaves stale literals nothing flags.

**Signals** (any one ⇒ closed set): docstring says `One of "a", "b"` · `argparse choices=(...)` whose value is branched on internally · same literals compared in 2+ places · dataclass field `str` with enumerable legal values.

| Case | Use |
| -- | -- |
| Branched on, carries behaviour, crosses module boundary | `class X(str, Enum)` — `X("bad")` raises |
| Local single-use annotation only | `X = Literal["a", "b"]` — type-check only |

```python
class Severity(str, Enum):
    FAIL = "FAIL"
    WARN = "WARN"
```

- `(str, Enum)` not `StrEnum` while `requires-python < 3.11` — mixin keeps `Severity.FAIL == "FAIL"` true, so serialization and migration-era string comparisons still work. Check `pyproject.toml` (§Python Version Policy) before assuming.
- **Enum inside, string at boundary** — CLI/JSON/file formats stay plain; convert once at edge (`Severity(raw)`), pass enum inward.
- **`==` never `is`** — `is` fails silently when value arrived as plain string from a boundary.
- **`.value` in f-strings** — bare `f"{Severity.WARN}"` renders `Severity.WARN` on non-mixin Enum.
- **Derive `argparse choices=`** from the enum (`[s.value for s in Severity]`) — hand-repeated literals drift.

## Complexity Thresholds

Enforce via ruff `C901` + `PLR` rules (see `foundry:linting-expert` for config). Hard limits per function:

| Metric | Limit | ruff rule | Refactor signal |
| -- | -- | -- | -- |
| Cyclomatic complexity (McCabe) | ≤12 | `C901` | extract sub-functions, guard clauses |
| Required arguments (no default) | ≤7 | style rule | primary rule — enforced in review; more than 7 required = introduce config dataclass |
| All arguments (incl. kwargs w/ defaults) | ≤12 | `PLR0913` | blunt ruff gate — kwargs with defaults may exceed 7 freely; ≤12 catches extreme cases |
| Branches (`if`/`elif`/`match`) | ≤12 | `PLR0912` | dispatch table, strategy pattern |
| Statements | ≤50 | `PLR0915` | split responsibility |
| Return points | ≤6 | `PLR0911` | consolidate early-return paths |

When a function exceeds any limit: **refactor first**. Adding `# noqa: PLR...` allowed only when refactoring genuinely impossible (generated code, parser output, protocol-mandated signature) — always add inline comment explaining why.
