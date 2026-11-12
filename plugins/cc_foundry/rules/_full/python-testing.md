# Detailed testing and coding contracts

> Cross-file navigation: §Uncollected Tests means `python-testing.md` §Uncollected Tests; §Behaviour vs Bookkeeping Checks means `python-testing.md` §Behaviour vs Bookkeeping Checks. Both headings remain in the parent rule (`../python-testing.md`).

## Test Structure

- **Arrange-Act-Assert (AAA)**: one setup block, one `act`, one assertion group per test
  - Never second `act` in same test
  - Exception: logically-unified multi-step operations (e.g. cache set+get round-trip, encode+decode) count as one act when test scenario is a single contract; split when each step is independently testable behavior
- Each test validates exactly one scenario
- No `if`/`for` logic in test bodies
  - Exception: list-comprehension/generator inside `@pytest.mark.parametrize(...)` to build args — allowed if it spans \<30% of the decorator's own lines (lines inside the outer parentheses only, not the test body)
  - `while` loops count too. A branch or loop in the body means the test covers more than one scenario — parametrize the cases or split the test.
- Parametrize aggressively — 3+ test functions same structure → `@pytest.mark.parametrize`
- Test case IDs: use `pytest.param(..., id="slug")` per case — never `ids=[...]` on decorator; keeps ID and args co-located, survives reordering
  - Never pass a separate `ids=` in any form — list, tuple, or callable. Generated cases and mapping rows follow the same rule; attach each required ID to its own case.
  - Keep single, simple `str`, `bool`, `int`, and `float` cases bare and let pytest derive the default ID; a wish for a descriptive ID alone does not justify a `pytest.param` wrapper. Use a concise semantic ID for a generated oversized string whose default ID would be unreadable.
  - Wrap every tuple case, including multi-argument rows, as `pytest.param(..., id="meaningful-case")`: pass row arguments separately, and preserve a tuple-valued single argument as `pytest.param((...), id=...)`.
  - Use `pytest.param` with semantic IDs for functions, containers, and other opaque or unstable objects, retaining case-specific `marks=`. IDs describe behavior or intent, never memory addresses or object hashes.
  - Remove optional trailing commas that force short lists or calls onto multiple lines, then run the project's formatter/linter hooks (`claude-config.md` §Lint/Format). Retain required tuple commas, comments, and formatter-restored wrapping; skip ambiguous edits. Preserve values, argument unpacking, order, duplicates, marks, and assertions.
- Group topic-related tests into class; class name carries unit (and optionally condition) so method names describe expected outcome only. The shared prefix moves into the class name and comes out of every method name — the method reads as the assertion, not as a restatement of its subject:

```python
class TestParseArgs:  # subject stated once
    def test_rejects_unknown_flag(self): ...  # not test_parse_args_rejects_unknown_flag
    def test_defaults_to_install(self): ...  # not test_parse_args_defaults_to_install
```

## Cross-OS Tests

Companion to `python-code.md` §Multi-OS Executables, same scope: projects supporting more than one OS. Production portability is fixed there; these are the test-side rules.

- **Skips are the last resort.** Never a blanket `skipif(sys.platform == "win32")` — probe the capability and skip on `OSError`. Document each surviving skip and re-audit it.
- Test skips are **collection-time decorators only** (`pytest.mark.skipif`, `pytest.mark.skip`, or parametrized marks). Never call `pytest.skip()` from a test or fixture body.
- **Recurrent defect guard**: a test simulating another OS must supply every host-only API and constant it exercises rather than assuming the runner exports them. For absent surfaces such as `os.killpg` or `signal.SIGKILL`, install test doubles with `monkeypatch.setattr(..., raising=False)`, and have the regression first use `monkeypatch.delattr(..., raising=False)` to prove the missing-attribute case. Run the simulated branch on every host — never add an OS skip for it.

## File Layout

Mirror `src/` layout in `tests/unit/`: `src/foo/bar.py` → `tests/unit/foo/test_bar.py`

- A `tests/` directory past 15 test files groups them into topic subfolders named for what they test. A subfolder needs 4 or more test files; fewer stay in the parent directory, never a folder of 3 or fewer.
- `_`-prefixed helper modules and the tree-wide `conftest.py` stay at the test tree's root and resolve from any depth through that conftest's `sys.path` insert (needed under `--import-mode=importlib`). A narrower `conftest.py` may still sit in a subfolder (§Fixtures). Path anchors (`Path(__file__).resolve().parents[N]`) shift one per folder level: derive roots from one shared helper or conftest constant, not `parents[N]` repeated across files. Fixtures and data with a single consumer live beside it.

## Mocking

Decorator or context manager — never hand-assigned attributes (`mod.fn = fake`), which leak into every later test in the session when an assertion fails before restore.

```python
@mock.patch("pkg.mod.fetch", autospec=True, return_value={"ok": True})  # patch WHERE USED, not where defined
def test_x(mock_fetch): ...
```

- **Patch the reference, not the origin** — `pkg.consumer.fetch`, not `pkg.source.fetch`; `from x import fetch` binds a new name the origin patch never reaches.
- `autospec=True` (or `spec=`) on anything non-trivial — a bare `Mock` happily accepts calls the real object would reject, so the test passes after the real signature changes.
  - Prefer the `@mock.patch(..., autospec=True)` decorator over a `with patch(...)` block: the patch then spans the whole test and adds no indented body.
  - Every `patch(...)`, `mock.patch(...)`, and `patch.object(...)` names its replacement shape: one of `autospec`, `spec`, `spec_set`, `new`, `new_callable`, or `wraps`, or a replacement object as the second positional argument. A patch with none of these is an unspecced `MagicMock`.
- Prefer `monkeypatch` (pytest fixture) for env vars, `cwd`, and attributes — auto-restores at teardown, no decorator stacking.
- Mock only what crosses a process boundary you don't own: network, clock, filesystem, subprocess, randomness. Mocking your own logic asserts the mock, not the code.
  - Speed never justifies mocking the thing under test. Tests whose contract is the real interaction — integration, packaging, installed-package, live-service, and end-to-end hook runs — stay real; mock only the boundaries that test does not cover.
- **Never add a mock to silence a newly failing test** — see §Test-Softening Anti-patterns; that hides the regression the test just caught.
- Assert the interaction when it is the contract (`assert_called_once_with`), not merely that the function returned.

## Fixtures

Repetitive setup goes in a fixture, not the test body — the body should read as Act + Assert, with Arrange reduced to naming what it needs. Same input built in 2+ tests → fixture.

```python
@pytest.fixture
def config(tmp_path):
    """Minimal valid config on disk."""
    (tmp_path / "cfg.toml").write_text('name = "demo"\n')
    return tmp_path / "cfg.toml"


def test_loads_name(config):  # Arrange is one parameter
    assert load(config).name == "demo"
```

- **Keep assertion-relevant values visible in the test.** A fixture hiding the exact value under assertion makes failures unreadable — factor out the scaffolding, not the thing being verified.
- **Parametrized inputs → factory fixture** (fixture returning a callable) when each test needs a different variant; a plain fixture returning one object forces copy-paste variants.
- Narrowest scope that works: default `function`. Widen to `module`/`session` only for genuinely expensive, immutable setup — a shared mutable fixture leaks state between tests and creates order-dependent failures.
- Use the built-ins before writing your own: `tmp_path`, `monkeypatch`, `capsys`, `caplog`.
- Shared across files → `conftest.py` at the narrowest covering directory; don't duplicate per file.
- Teardown via `yield` in the fixture — never `try`/`finally` in a test body (§Test-Softening Anti-patterns).

## Seeding / Randomness

- Never seed RNG except inside `autouse=True` fixture — not in test bodies, module level, or non-autouse fixtures
- If fixture needed project-wide, place in `tests/conftest.py` — don't duplicate per file
  - Per-file placement only when file needs different seed strategy
- Use pytest fixture resetting all RNG sources: `torch.manual_seed`, `numpy.random.seed`, `random.seed`, `torch.cuda.manual_seed_all`
- Fixture must use `autouse=True`

See `_shared/pytest-config.md` for the canonical `reset_random_seeds` autouse fixture.

## CUDA Skip Pattern

Use decorator form, not inline `if`:

```python
@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA not available")
def test_cuda_inference(): ...
```

## Docstrings

- Every test function/method needs a one-line docstring (max 120 chars) — **required even when it restates the test name**. The name is an identifier constrained by naming rules; the docstring is prose and is what surfaces in failure output and `-v` listings.
- Below the one-liner, add a short paragraph giving the **scenario or use case**: the starting state, what is exercised, and why that combination matters. This is what tells a reader whether a failure is a real regression or a stale expectation — the name alone never carries it.
- Module-level docstrings required

```python
def test_rejects_unknown_flag(self):
    """Unknown CLI flags are rejected instead of silently ignored.

    A typo'd flag that parses as a no-op is the failure this guards: the run
    appears to succeed while the intended option never took effect.
    """
```

## Test Selection — Markers

Markers exist so a suite can be sliced by what a test *requires*, not by what it's called or where it lives. The vocabulary itself is per-project; the discipline below is not.

- Choose **semantic** markers describing a behavioral contract or an execution requirement — real component interaction, build/install contract, need for live external services or credentials.
- **Classify by contract, never by duration.** A marker is not a speed tier; use CI duration reports to investigate slow tests instead.
- Apply markers directly to tests or to homogeneous classes. **Never assign `pytestmark`**, including a list of markers — it silently marks everything a file later gains.
- Never infer membership from paths, names, platforms, or mocked subprocesses. Ordinary tests may stay unmarked.
- Register every selector in the project's test configuration and validate with `--strict-markers`, so a typo fails collection instead of silently selecting nothing.
- A marker for live external services also implies the integration contract and keeps its explicit opt-in guard. **Selection never authorizes network or paid execution**, and capability probes remain a separate mechanism.
- Reuse a repeated collection-time `pytest.mark.skipif(...)` condition as a named decorator (e.g. `_skip_node_unavailable`), preserving its predicate and reason. This does not authorize new skips or weaken the capability-probe policy.
- Keep full CI unfiltered; marker selection is for local and targeted runs.

## Helpers in Tests

- Helper with no shared logic across cases → split into separate dedicated functions, not single branching helper
- Shared logic only → shared function; helper must not branch on case type (no if/for selecting different behavior per caller)
- Share helpers and fixtures only within one test tree: `conftest.py` or an `_`-prefixed module (placement: §File Layout). Never import test helpers from another independently installable package's tests — a test shipped with its package runs without the surrounding repository.
- Production code never imports a test helper. Shipped executables (scripts, hooks, entry points) stay self-contained, and files kept byte-identical across packages stay identical — test convenience never justifies diverging a copy.

## Doctests

Doctests live in **source files** (`src/**/*.py`), not test files — part of module docs. Run with:

```bash
python -m pytest --doctest-modules src/
```

Don't rely on `tests/**/*.py` globs for doctests — missed. Add `--doctest-modules src/` explicitly to pytest invocation or `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "--doctest-modules"
testpaths = ["src", "tests"]
```

## Test-Softening Anti-patterns

Never soften a failing test to make it pass. Every softening pattern signals wrong testing approach or implementation bug — investigate root cause:

- **`try`/`except` in test body**: `try: <act>; assert ...; except: pass` = test always passes regardless of behavior; remove wrapper, fix implementation
- **`try`/`finally` in test body**: teardown belongs in pytest fixture `yield` + cleanup block — not inline `finally`; use `autouse` or explicit fixture instead
- **Silent `pytest.skip()` without root cause**: skipping without understanding why hides regressions; trace failure, fix or open tracked issue; never skip as first response to failure
- **`@pytest.mark.xfail` without `raises=` and issue link**: open-ended `xfail` = permanent silent hole; require `raises=<ExceptionType>` + `reason="<issue-URL>"` — both mandatory
- **Over-mocking to avoid real failures**: adding mocks after tests start failing (not upfront as isolation design) covers implementation bugs; remove mock to expose root cause
- **Widening tolerance to pass**: loosening `atol`/`rtol`, switching `==` to `in range`, or relaxing type checks without documented precision reason = hiding numerical or type bugs

When tempted to soften a test: stop — read the failure, find the implementation bug, fix that instead.

## Mechanical Enforcement — Ruff

Where a project's Ruff config selects these rules, they are enforced by code through the pinned `ruff-check` pre-commit hook, and its report is authoritative. One line per rule; the rule itself lives in the named section:

- `D100`–`D103` — module, public class, method or function without a docstring, scoped to test files with a negated `lint.per-file-ignores."!**/tests/**"` entry (§Docstrings)
- `W505` — docstring line over `lint.pycodestyle.max-doc-length` (120) (§Docstrings)
- `TID251` — `pytest.skip` banned through `lint.flake8-tidy-imports.banned-api`; skip/skipif marks are fine (§Cross-OS Tests, §Test-Softening Anti-patterns)
- `PT` (flake8-pytest-style) — parametrize and fixture shape (§Test Structure)
- `F811` — a redefined `def test_*` silently drops the first one (§Uncollected Tests)

Ruff cannot express the rest, so reviewers and `foundry:qa-specialist` check them by reading: `ids=` instead of `pytest.param(..., id=...)`, `pytestmark` assignment, `def test_*` nested in another function, `if`/`for`/`while` in a test body, filler tokens in a test name, and a `patch` without a replacement shape. Never add a custom checker script for a rule Ruff can enforce, and never keep a pytest test for a check that only guards bookkeeping (§Behaviour vs Bookkeeping Checks).
