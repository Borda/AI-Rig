| Anti-pattern | Recommendation |
| -- | -- |
| Leaky abstraction | Add `__all__`, use private names (`_`) for internals |
| Circular dependencies | Extract shared types to third module; invert one dependency |
| God module | Split by cohesion; each module one job |
| Missing `__all__` | Add `__all__` to every `__init__.py` |
| Breaking change without deprecation | Use typing_extensions.deprecated (PEP 702); add deprecation in vX.Y, remove in vZ.W |
| Over-abstraction | Flatten; prefer composition over deep inheritance |
| Mutable default arguments | Use `field(default_factory=list)` in dataclasses; `= None` with guard in functions |
| Tight ML-framework coupling | Lazy imports; device-agnostic design; dependency injection |
| Type-annotation circular import | Use `from __future__ import annotations` + `TYPE_CHECKING` guard: `if TYPE_CHECKING: from module import Type` — eliminates runtime import while preserving type checker support |
| Destructive migration before consumer cutover | Use expand-contract: add new columns, deploy reader of new columns, then drop old columns in separate migration after all readers migrated |
| Undocumented boundary placement | Write ADR before any restructure; must state ownership principle so future engineers don't re-create same ambiguity |
| LSP violation | Subclass overrides with `NotImplementedError`/`pass` body or call sites use `isinstance`/cast before using base type → flatten hierarchy; prefer Protocol structural typing over ABC enforcement |
| ISP violation | Protocol or ABC with >5 methods where callers use only a partial subset → split into focused protocols per usage cluster; Protocol over ABC for structural typing in Python |
