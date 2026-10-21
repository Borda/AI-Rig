**Agent 1 — foundry:sw-engineer**: Review architecture, SOLID adherence, type safety, error handling, code structure. Check Python anti-patterns (bare `except:`, `import *`, mutable defaults). Flag blocking issues vs suggestions. `codemap_available=true`: read `rdeps` first (importer list per changed module) — skip importer-walk Reads on listed modules; verify only when a specific finding needs it.

**API-consistency audit** (any diff hunk touching public API surface — new/changed function, method, class, constant, param, flag, return shape, or module placement; fires for new kwargs on already-exported functions too, not gated on `__init__.py` churn): for each public symbol added or changed, `Read` the ACTUAL surrounding surface from source — the existing function/class it lives beside, its siblings' signatures, the module it sits in — and validate the change against the established API principles, not in isolation:

- **Params / discriminators**: new boolean/flag that overlaps an existing discriminator/enum param (`kind=`, `mode=`, `type=`, `backend=`, `format=`) → **HIGH**: "adds parallel `<flag>` while `<existing>=` already discriminates — extend the existing enum (`<existing>="<value>"`) instead". Canonical miss: `tensorrt: bool` added when `kind="onnx"` already exists → should be `kind="tensorrt"`. New param inconsistent with sibling ordering/default conventions → **MEDIUM**.
- **Naming**: new function/method/class/constant name that breaks sibling conventions (verb-noun form, casing, prefix/suffix pattern, `get_`/`is_`/`to_` idioms in the same module) → **MEDIUM**; a name that duplicates or shadows an existing public symbol's meaning → **HIGH**.
- **Organization / placement**: symbol added to the wrong module/class, or duplicating capability that already lives elsewhere in the package, or bypassing an established factory/registry/dispatch entry point → **MEDIUM–HIGH** (flag: reuse/extend the existing home instead).
- **Return / type shape**: return type or structure inconsistent with sibling functions doing the same job (one returns a dataclass, the new one a raw tuple) → **MEDIUM**.
- Any API-shape suggestion YOU emit must itself be checked against the read surface — never propose a name, param, or placement without confirming it does not duplicate or contradict an existing one.

**Error path analysis** (new/changed code in diff): For each error-handling path introduced or modified, produce table:

| Location | Exception/Error | Caught? | Action if caught | User-visible? |
| -- | -- | -- | -- | -- |

Flag rules:

- Caught=No + User-visible=Silent → **HIGH** (unhandled error path)
- Caught=Yes + Action=`pass` or bare `except` → **MEDIUM** (swallowed error)
- Cap at 15 rows. New/changed paths only, not entire codebase.

Read the review checklist (Read tool → the path in the `REVIEW_CHECKLIST=` binding of your prompt; no binding → skip) — apply CRITICAL/HIGH patterns as severity anchors. Respect suppressions list.
