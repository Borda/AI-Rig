---
description: pytest test design standards — structure, fixtures, parametrization, markers and selection, behaviour-vs-bookkeeping classification, mutation checks, cross-OS tests, mocking, test names, uncollected tests, prose-reading tests, test-style hook codes
paths:
  - tests/**/*.py
  - '**/test_*.py'
---

> **Precedence — the rule closer to the code wins.** These are plugin-level defaults. Where a project states its own convention that conflicts with anything here — in its `CLAUDE.md`, its own `rules/`, a linter/formatter config it enforces, or a consistent established style in the surrounding code — the project's convention wins. Follow it, don't "correct" the codebase toward this file. Apply these rules only where the project is silent. Project convention looks like an oversight rather than a decision → say so once, still follow the project.

> Extracted sections below bind before writing, changing or reviewing tests. Resolve the active installed Foundry root through its `bin/resolve_plugin_root.py --plugin-name foundry` helper (registry preferred), then Read `<resolved-root>/rules/_full/python-testing.md` at the named section. For maintenance inside this repository only, use the source-tree `plugins/cc_foundry/rules/_full/python-testing.md`. Never select a different installed version merely because it has a newer mtime.

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

> Before applying, Read `_full/python-testing.md` §Test Structure; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Test Names

- Name the observable outcome, never a verdict on it. Filler tokens carry no information about what broke: never use `correctly`, `properly`, `successfully`, `works`, `should`, or `as_expected` as a name token (`test_rejects_unknown_flag`, not `test_parse_works_correctly`).

## Uncollected Tests

A test pytest never collects or never fully runs passes silently forever. Three shapes hide one:

- **Nested `def test_*`** inside another function or test — pytest collects only module-level functions and class methods, so the inner test never runs.
- **Same-name redefinition** in one module or class (ruff `F811`) — the later definition replaces the earlier one, which then never runs.
- **Code after `return`** in a test — unreachable assertions never execute.

## Test Selection — Markers

> Before applying, Read `_full/python-testing.md` §Test Selection — Markers; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Parallel Runs (pytest-xdist)

- Wide runs go parallel (`-n <workers>`); keep focused single-file runs serial, where worker startup costs more than it saves.
- Drop `-n` when the failure itself is what you are reading: xdist interleaves worker output, hides `-x` ordering, and breaks `--pdb`. Reproduce a failure serially before diagnosing it.
- A test that passes serially and fails only under `-n` is a real defect, not an xdist artifact — usually shared machine-global state (a `${TMPDIR}` sentinel without its `-${CSID}` suffix, per `claude-config.md` §TMPDIR Sentinel Scoping; a fixed port; a file written outside `tmp_path`). Fix the isolation; never pin that suite serial to hide it.

## Behaviour vs Bookkeeping Checks

Classify a check before writing it. Decisive question: if this test did not exist, could anything actually break undetected? If not, it is linting and belongs in a pre-commit hook (or a Ruff rule), not pytest. A failure that means broken behaviour (guard verdict, parser reading, CLI exit code, anything a user or hook depends on) is a pytest test. A failure that only means drifted bookkeeping (README counts, export lists, byte-identical copies, manifest hashes regenerated from source) belongs in a pre-commit hook: kept in pytest it blocks fix rounds with no behaviour at risk and dilutes what coverage measures.

## Mutation Checks

A mutation check (break the code, prove the test fails) runs on a copy: copy the module into a private temp dir outside the repository and point the test's import there. Never rewrite the tracked file, even with restore in `finally` — an interrupted run leaves the repository mutated.

## Cross-OS Tests

> Before applying, Read `_full/python-testing.md` §Cross-OS Tests; binding inline. If it cannot be read, stop and report the missing prerequisite.

## File Layout

> Before applying, Read `_full/python-testing.md` §File Layout; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Seeding / Randomness

> Before applying, Read `_full/python-testing.md` §Seeding / Randomness; binding inline. If it cannot be read, stop and report the missing prerequisite.

## CUDA Skip Pattern

> Before applying, Read `_full/python-testing.md` §CUDA Skip Pattern; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Docstrings

> Before applying, Read `_full/python-testing.md` §Docstrings; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Mocking

> Before applying, Read `_full/python-testing.md` §Mocking; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Fixtures

> Before applying, Read `_full/python-testing.md` §Fixtures; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Helpers in Tests

> Before applying, Read `_full/python-testing.md` §Helpers in Tests; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Tests Reading Markdown or Prose

Tests that assert on Markdown, rule files, or other prose parse it line by line, so rewording or reflowing a paragraph cannot break an unrelated assertion:

- Split into lines, locate the heading's section, then apply per-line predicates.
- Read table cells by splitting a row on `|`, never by matching across the row.
- No multi-line or wrap-sensitive regex — a line break in prose is formatting, not content.
- Never edit the Markdown to fit a test. A test that breaks on a reflow is the defect; fix the test.

## TDD for New Features

Write tests before implementation — tests define contract.

## Doctests

> Before applying, Read `_full/python-testing.md` §Doctests; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Test-Softening Anti-patterns

> Before applying, Read `_full/python-testing.md` §Test-Softening Anti-patterns; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Mechanical Enforcement — Ruff

> Before applying, Read `_full/python-testing.md` §Mechanical Enforcement — Ruff; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Baseline Gate

All existing tests must pass before adding new code.
