## Cohesion Check

Read module, ask:

- Do all public names serve single, nameable purpose?
- Describe module in one sentence without "and"?
- If not — likely needs splitting.

## API Surface Audit

Grep tool (pattern `__all__`, file `src/mypackage/__init__.py`, output mode `content`) to see public exports.

List importable names using the project's existing interpreter: `python -c "import mypackage; print([x for x in dir(mypackage) if not x.startswith('_')])"` (replace `python` with that interpreter). Use `uv run python` only when `uv.lock` exists and the project uses uv. Requires package installed; side-effect-safe only — prefer Grep for `__all__` as zero-side-effect alternative.

Missing `__all__` = accidental API leakage. Everything importable becomes contract.

## Dependency Direction

Draw import graph. Healthy library:

- Core modules have no deps on higher-level modules
- Higher-level depend on core, not each other
- Circular imports = design smell requiring immediate intervention

## Testability Assessment

Design testable if:

- Dependencies injectable (not hardcoded)
- Side effects isolated at boundaries
- Pure functions preferred over stateful classes
- Protocols/ABCs define seams for mocks

## Unannotated Code Discipline

Reviewing code with no inline comments:

- Enumerate all import statements first — map dependency graph before reading method bodies
- Each public API change: compare signatures explicitly against previous version, even without flag comment
- Migrations: check all referenced column names against all deployed services, not just new service
- Don't rely on comment hints — assume comments absent or misleading
- Inline changelog comments (e.g. `# v1 had: def old_fn(x, y)`) authoritative for historical signatures — treat as CHANGELOG entry; don't reduce confidence for relying on them

## Python/ML Library Specifics

- **`__init__.py` exports** — public contract; audit before/after any structural change
- **Protocol vs ABC** — prefer `Protocol` for structural typing; use `ABC` only for enforced method inheritance
- **Dataclass vs NamedTuple** — dataclasses for mutable config; NamedTuple for immutable records
- **torch.nn.Module subclassing** — `forward()` only required override; `__init__` registers all parameters
- **Config objects** — dataclasses with `field(default_factory=...)` never mutable defaults
