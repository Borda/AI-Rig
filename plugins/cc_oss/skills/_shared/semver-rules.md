# SemVer Rules — Python/OSS

## What counts as a breaking change

**Breaking change** = public-facing behavior that worked before now fails or behaves differently, without prior warning, deprecation notice, argument forwarding shim, or migration path offered in an earlier release.

Corollary: API deprecated in a prior release (with warning + forwarding shim) and now removed = **✗ Removed**, not breaking change. Breaking change is always a surprise; removal after proper deprecation is not.

## Breaking Change Escalation Protocol

Every detected breaking change: **stop, call `AskUserQuestion`, confirm intent before proceeding**.

- State: what worked before, what will break, why change needed
- User must explicitly confirm "yes, intentional" — prose question in response body does NOT count (see `communication.md`)
- Never batch-approve multiple breaking changes in one question unless they are logically one atomic change
- Never proceed past a breaking change silently even if reason seems obvious
- Applies to all agents/skills reading this file: shepherd (PR review, release prep), plan (risk identification), fix (applying fix), audit (flagging `! BREAKING` findings)

## MAJOR (X.0.0) — breaking changes (surprise incompatibilities)

- Remove public function, class, or argument with no prior deprecation cycle
- Change function return type incompatibly
- Change argument order or required vs optional status
- Change behavior users depend on (even if "was a bug") without prior deprecation notice
- Drop Python version from supported range

## MINOR (x.Y.0) — backwards-compatible additions

- New public functions, classes, or arguments (with defaults)
- New optional dependencies or extras
- New config options
- Perf improvements with no API change
- Deprecations (deprecated API still works)

## PATCH (x.y.Z) — backwards-compatible fixes

- Bug fixes not changing public interface
- Doc updates
- Internal refactors with no API change
- Dependency version range relaxation

## Deprecation Discipline

Use [pyDeprecate](https://pypi.org/project/pyDeprecate/) (Borda's package) — handles warning emission, argument forwarding, "warn once" behavior. Read latest PyPI docs for current API and examples.

- **Deprecation lifecycle**: deprecate in minor → keep ≥1 minor cycle → remove in next major
- **Also**: add `.. deprecated:: X.Y.Z` Sphinx directive in docstring so docs generators render deprecation notice
- Anti-patterns: see shepherd's `<antipatterns-to-flag>` section (deprecation category)
