---
name: integration
description: 'Codemap integration: audit|plan|apply|sync|demo; skip query/index rebuilds.'
---

NOT for: structural queries (`$codemap-py:query-code`) or index rebuilds (`$codemap-py:scan-codebase`).

# Integration

Adapter for `codemap-py integrate` (`src/codemap_py/integration.py`). Claude Code or Codex may target Claude, Codex, or both; this skill runs only native plugin-manager CLIs, never another runtime's model. `check` is removed with no alias.

| Mode | Args | Mutation | Exit |
| -- | -- | -- | -- |
| `audit` | `[--runtime {claude,codex,both}] [--json] [--since YYYY-MM-DD]` | none | 0 pass/warn; 1 fail; 2 bad syntax |
| `plan` | `[--runtime ...] [--consumers <csv>] [--source {local-candidate,release}] [--out <artifact>]` | report artifact only | 0; 2 bad syntax |
| `apply` | `--plan <artifact> --approve <sha256>` | verified source checkout only | 0; 1 drift/fs; 2 bad approve/syntax |
| `sync` | `--source {local-candidate,release} --plan <artifact> --approve <sha256> [--runtime ...]` | local runtime plugin state | 0; 1 partial-fail/journal; 2 bad approve |
| `demo` | `[--runtime ...]` | disposable evidence only | 0; 1 fail |

`--approve` requires `apply`/`sync`, a saved plan, and its displayed SHA-256. It never authorizes new targets, remote publication, Git mutation, marketplace/instruction edits, or deletion.

Closed set: Claude `foundry`, `oss`, `develop`, `research`; Codex `codex-rig`; provider `codemap-py`. Cross-check both marketplace and plugin manifests before mutation. `--runtime codex` selects only `codex-rig`, `claude` selects the four Claude consumers, and omitted/`both` selects all five. This is explicit mapping, not discovery.

## Safety invariants

- `plan` records protocol/schema, operation ID, targets, before hashes, desired identity, argv, ordered operations, rollback identity, expected state, and SHA-256.
- Mutation revalidates target and before state immediately; drift invalidates approval. Source writes are atomic with before-images and reject foreign/modified markers, escapes, symlinks, installed caches, dirty overlap, and unverified identity.
- `sync` rejects unbuilt candidate sources, installed-byte/hash mismatch, and mutable/default-branch release or rollback identity; never assumes latest.
- On later-target failure, stop; rollback only approved operations. Claim completion/rollback only after post-state hashes verify.
- "Push" means local allowlisted source wiring plus native local runtime installation. Never `git push`, marketplace mutation, release publication, or direct installed-cache edit.

The active Codex consumer contract (requires the `codex-rig` plugin) is its shipped `shared/codemap-contract.md`: its adapter validates `CODEMAP_BIN` first or PATH fallback once, runs the provider-owned probe/query surface, persists one context artifact, and lets specialists reuse that artifact. The provider-managed `codemap-py-integration.md` block is metadata-only; identity/protocol/timestamp fields do not wire the launcher or prove active guidance. Audit checks provider identity and reachable active consumer guidance separately and reports missing, unreachable, or outdated guidance as bounded source maintenance (or an existing approved `plan_sync` target). Do not borrow another plugin's shared script or edit installed caches. Distinguish installed-byte/hash evidence from current-session activation: a native listing without session provenance is not proof, and matching source hashes alone do not prove semantic currency.

## Runtime note

Codex has no `bin/` PATH entry or plugin-root variable. Resolve its installed root once, substitute `PLUGIN_ROOT`, and retain it in reasoning. Codex has no `AskUserQuestion`: print plan summary and SHA-256, then wait for the next user message before `apply`/`sync`.

When `--runtime` includes Codex (`codex`, `both`, or omitted), discover the active `codex-rig` via native CLI, never hand-edit config:

```bash
codex plugin marketplace list --json
codex plugin list --marketplace borda-ai-rig --json
```

If installed Codex lacks documented `--json`, use text output and mark structured comparison unavailable. After `sync` installs/reinstalls `codex-rig` or `codemap-py`, say: "Start a new Codex session before relying on the updated plugin — this session's tool list was resolved before the update."

## Workflow

### 1. Resolve mode

Case-insensitive: empty or starting with `audit` → audit; otherwise `plan`, `apply`, `sync`, or `demo`. Any other input: ask which of those five modes and wait.

### 2. Run it

```bash
PLUGIN_ROOT/bin/codemap-py integrate audit [--runtime <r>] [--json] [--since YYYY-MM-DD]
PLUGIN_ROOT/bin/codemap-py integrate plan [--runtime <r>] [--consumers <csv>] [--source <s>] [--out <artifact>]
PLUGIN_ROOT/bin/codemap-py integrate apply --plan <artifact> --approve <sha256>
PLUGIN_ROOT/bin/codemap-py integrate sync --source <s> --plan <artifact> --approve <sha256> [--runtime <r>]
PLUGIN_ROOT/bin/codemap-py integrate demo [--runtime <r>]
```

`audit`: bounded read-only provider/consumer/version/content/managed-block/index/log/usage inspection. It never runs `plan`, `apply`, `sync`, index, query self-heal, native mutation, or global-instruction installation. It checks provider identity and active consumer guidance reachability/content separately; report `consumer_query_guidance_missing`, `consumer_query_guidance_unreachable`, or `consumer_query_guidance_drift` as source maintenance, not active wiring. Same-version content mismatch is high-severity drift; a native listing without provenance is `session_catalog: unobservable`. Codex has CLI/tool shards but no skill-start hook; host hooks provide no token usage, so report evidence limits, not fresh-session activation or token savings. `--json` uses schema 2 (`codemap-py.integration.v2`); `--since` filters telemetry.

`plan`: write only; print artifact, targets, and SHA-256. `apply`: require matching shown SHA-256 and explicit user approval. `sync`: same gate plus explicit source; give the fresh-session instruction above if applicable. `demo`: audit plus one representative structural smoke query; evidence is disposable unless a mutation is separately approved, and it makes no token-savings or current-session activation claim.

### 3. Report

Explain `0` success, `1` runtime/filesystem failure or partial-sync journal, and `2` syntax/approval failure. On `sync` exit `1`, report state (`planned → approved → applying:<t> → verified:<t> → complete`, or `rollback-started → rollback-succeeded|rollback-failed → recovery-required`); for `recovery-required`, give only engine-reported bounded recovery commands.
