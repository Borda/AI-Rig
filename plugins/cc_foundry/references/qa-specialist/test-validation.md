## Multi-level Test Validation

Every test must pass three levels, in sequence, before it's complete:

**Level 1 — Name/Scenario Clarity** Test function name must unambiguously declare what's tested. Format: `test_<unit>_<condition>_<expected>` or `test_<behavior>_when_<condition>`. If name alone is insufficient (complex scenario, multi-step flow, stateful sequence), add a one-line docstring: `"""Scenario: user does X with Y under Z, expects W."""` Criterion: reviewer understands the test's purpose without reading the body.

**Level 2 — Contract Validation (implementation-blind)** Apply **Black-box first** (see Core Principles): validate test purpose against SW goals/blueprints BEFORE inspecting test code. Ask: "Does this scenario represent real user behavior? Is the expected outcome derivable from the documented contract alone?" A scenario that can't be justified from docs without reading implementation asserts implementation detail — rewrite from contract.

**Level 3 — Coverage Completeness** Confirm test code is faithful to its declared scenario, covering all documented parameter variants, boundary values, and error paths named in it. Each parametrize case must map to a distinct documented sub-scenario — no duplicate under different framing, no declared variation missing from the list.

## Edge Case Matrix

For every public API entry point (function, class method, CLI flag, endpoint parameter), apply checklist:

- **Documented happy path**: test primary example from docs/docstring verbatim — baseline user expectation
- **Empty/null**: empty list, None, empty string, zero — only for parameters docs say are optional or nullable
- **Boundary values**: min, max, min±1, max±1 — derived from documented constraints (type hints, `Raises:` guards, `Args:` ranges)
- **Type mismatches**: wrong type, subtype, protocol-compatible alternative — only where docs specify accepted types
- **Size extremes**: single element, very large collection — for sequence parameters
- **State edge cases**: uninitialized state, double-initialization, use-after-close — only for stateful public classes
- **Concurrency**: shared state accessed from multiple threads — only when class/function documented as thread-safe
- **Error paths**: for each `Raises:` in docstring, verify test exercises that exception branch; missing `Raises:` coverage always primary finding
- **Adversarial inputs**: syntactically valid but semantically hostile inputs (negative lengths, NaN floats, control characters in strings) — applied to every parameter lacking explicit range restriction in docs

## Test Organization

Mirror `src/` layout in `tests/unit/`: `src/foo/bar.py` → `tests/unit/foo/test_bar.py`.

`tests/unit/` no I/O; `tests/integration/` real deps/I/O; `tests/e2e/` full system; `tests/smoke/` deploy sanity.
