---
description: Python coding standards — docstrings, deprecation, version policy, library API awareness, multi-OS portability, PyTorch AMP, pinned lint/format hooks
paths:
  - '**/*.py'
---

> **Precedence — the rule closer to the code wins.** These are plugin-level defaults. Where a project states its own convention that conflicts with anything here — in its `CLAUDE.md`, its own `rules/`, a linter/formatter config it enforces, or a consistent established style in the surrounding code — the project's convention wins. Follow it, don't "correct" the codebase toward this file. Apply these rules only where the project is silent. Project convention looks like an oversight rather than a decision → say so once, still follow the project.

> Extracted sections below bind before writing, changing or reviewing the corresponding Python code. Resolve the active installed Foundry root through its `bin/resolve_plugin_root.py --plugin-name foundry` helper (registry preferred), then Read `<resolved-root>/rules/_full/python-code.md` at the named section. For maintenance inside this repository only, use the source-tree `plugins/cc_foundry/rules/_full/python-code.md`. Never select a different installed version merely because it has a newer mtime.

## Docstring Style

> Before applying, Read `_full/python-code.md` §Docstring Style; binding inline. If it cannot be read, stop and report the missing prerequisite.

Google/Napoleon style only unless user explicitly requests otherwise; never switch to NumPy by project, surrounding style or judgment. Every public function/class/module needs a docstring, with `Examples` per public function unless explicitly waived; brevity is no waiver. Only module-first-statement/function/class/method strings bind as docstrings; variables use preceding `#:` comments for Sphinx. Opening line states purpose in plain English; code-shaped details follow. Exact variable example remains in the full section.

## Deprecation

> Before applying, Read `_full/python-code.md` §Deprecation; binding inline. If it cannot be read, stop and report the missing prerequisite.

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

> Before applying, Read `_full/python-code.md` §Library API Awareness; binding inline. If it cannot be read, stop and report the missing prerequisite.

Every code-touching agent must verify installed version, actual top-level public API, symbol signatures and defaults before any third-party feature use, even when confident. Training memory is never evidence; unverified symbol/kwarg → do not write it. Use the installed API; never recommend an upgrade solely because it is unfamiliar. Exact probe commands and drift rationale remain in the full section.

## PyTorch AMP

- `torch.cuda.amp.autocast` deprecated since PyTorch 2.4 — use `torch.amp.autocast('cuda', ...)` instead
- `torch.cuda.amp.GradScaler` deprecated since PyTorch 2.4 — use `torch.amp.GradScaler('cuda')` instead
- Verify current stable release at pytorch.org when citing specific version numbers <!-- verified: 2026-04-06 -->

## Multi-OS Executables — a POSIX Assumption is a Defect

> Before applying, Read `_full/python-code.md` §Multi-OS Executables — a POSIX Assumption is a Defect; binding inline. If it cannot be read, stop and report the missing prerequisite.

For multi-OS projects, scripts/hooks/entry points/CI run on every supported OS (Linux, macOS, native Windows); fix portability at source, never skip a supported platform. Use `pathlib`, serialized cross-host paths unchanged with `PurePosixPath`/`PureWindowsPath`, portable byte/newline and subprocess-environment handling, explicit Bash CI shell, capability degradation and real Windows-semantics evidence. Test-side rules: `python-testing.md` §Cross-OS Tests; full exact APIs, environment keys and failure cases bind before edits.

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

> Before applying, Read `_full/python-code.md` §Structured Data — never a bare dict; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Closed Option Sets — never bare strings

> Before applying, Read `_full/python-code.md` §Closed Option Sets — never bare strings; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Complexity Thresholds

> Before applying, Read `_full/python-code.md` §Complexity Thresholds; binding inline. If it cannot be read, stop and report the missing prerequisite.

Every Python function/class written or reviewed: before delivery enforce cyclomatic ≤12 (`C901`), required args ≤7 (more → config dataclass), all args ≤12 (`PLR0913`), branches ≤12 (`PLR0912`), statements ≤50 (`PLR0915`), returns ≤6 (`PLR0911`). Defaults may exceed 7 args. Refactor first; `# noqa: PLR...` / `# noqa: C901` only if genuinely impossible (generated code, parser output, protocol signature), with inline why. Full metric table and exact verify command bind; per-limit rationale: `_full/quality-gates.md` §Python Code Complexity.

## Lint/Format — Pinned Hooks, Not Bare Tools

<!-- policy-sibling: plugins/cc_foundry/rules/claude-config.md (§Lint/Format stub) -->

Project pins lint/format tools via `.pre-commit-config.yaml` (ruff, eslint, mdformat, prettier, codespell, …) → **never invoke those tools directly** (`ruff check`, `ruff format`, `eslint`, `mdformat`, …) — version/config drift vs CI. Invoke the specific hook: `pre-commit run <hook-id> --files <path>` (single hook, targeted files) · `pre-commit run --all-files` (full sweep) · `pre-commit run <hook-id> --all-files` (single hook, repo-wide). Hook ids come from that config.

- Applies to ad-hoc checks during edits — not just the commit-time run
- Hook missing/needed and not yet in config → add it to `.pre-commit-config.yaml` rather than shelling out around it
- No pre-commit pin in the project → direct tool use is fine
