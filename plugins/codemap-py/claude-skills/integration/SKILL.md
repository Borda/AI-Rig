---
name: integration
description: |
  Adapter over `codemap-py integrate` — audit, plan, source-wire, locally sync, and demonstrate the
  codemap-py integration with its supported consumers. Trigger with `/codemap-py:integration
  audit|plan|apply|sync|demo [--runtime {claude,codex,both}] ...`. Default (no args) is `audit`.
  Skip for: running a structural query (use `/codemap-py:query-code`); explicit standalone index
  rebuild (use `/codemap-py:scan-codebase`).
argument-hint: audit [--runtime {claude,codex,both}] [--json] [--since YYYY-MM-DD] | plan [--runtime ...] [--consumers <csv>] [--source {local-candidate,release}] [--out <artifact>] | apply --plan <artifact> --approve <sha256> | sync --source {local-candidate,release} --plan <artifact> --approve <sha256> [--runtime ...] | demo [--runtime ...]
effort: medium
allowed-tools: Bash, AskUserQuestion
model: sonnet
---

<objective>

Runtime adapter over `codemap-py integrate` engine (`src/codemap_py/integration.py`). Targets Claude Code, Codex, or both. Never invokes another runtime's model; uses only its native plugin-manager CLI.

Five exact pinned CLI modes; retired `check` removed:

| Mode | Args | Mutation | Exit |
| -- | -- | -- | -- |
| `audit` | `[--runtime {claude,codex,both}] [--json] [--since YYYY-MM-DD]` | none | 0 pass/warn; 1 fail; 2 bad syntax |
| `plan` | `[--runtime ...] [--consumers <csv>] [--source {local-candidate,release}] [--out <artifact>]` | report artifact only | 0; 2 bad syntax |
| `apply` | `--plan <artifact> --approve <sha256>` | verified source checkout only | 0; 1 drift/fs; 2 bad approve/syntax |
| `sync` | `--source {local-candidate,release} --plan <artifact> --approve <sha256> [--runtime ...]` | local runtime plugin state | 0; 1 partial-fail/journal; 2 bad approve |
| `demo` | `[--runtime ...]` | disposable evidence only | 0; 1 fail |

`--approve` requires explicit mutation mode (`apply`/`sync`), saved plan artifact, and its user-shown SHA-256. It never authorizes new targets, remote publication, Git history/remote mutation, marketplace or user instruction-file edits, or data deletion.

Closed integration/reinstall set: Claude consumers `foundry`, `oss`, `develop`, `research` (provider `codemap-py`); Codex consumer `codex-rig` (provider `codemap-py`). Explicit mapping, not discovery/extension registry; cross-check both marketplace and plugin manifests before mutation. `--runtime codex`: only `codex-rig`; `--runtime claude`: four Claude consumers; `--runtime both` or omitted: all five.

Shared-engine safety invariants:

- `plan` records schema/protocol version, op ID, exact targets, before-state hashes, desired versions/refs/hashes, exact argv, ordered ops, rollback identities, expected post-state, and plan SHA-256.
- Every mutation revalidates target + before-state immediately before action; drift invalidates approval.
- Source writes use before-images + atomic per-file replace; `apply` refuses foreign/modified markers, path escapes, symlinks, installed-cache roots, dirty working-tree overlap, and unverified product identity.
- `sync` refuses plans whose source is absent from selected candidate, installed bytes mismatch selected hash, or release/rollback evidence names mutable/default-branch source. No implicit "latest".
- First-target success + second-target failure stops immediately. Rollback performs only approved-plan actions. Claim completion/rollback only after post-state hash verification.
- "Push" means only (1) updating allowlisted version-controlled consumer source integration from `codemap-py.integration.v2`, and (2) installing/reinstalling those built plugin versions locally via native runtime CLI. Never `git push`, remote marketplace mutation, release publication, or direct installed-cache edits.

Active consumer guidance is separate from provider metadata. For Codex, the `codex-rig` plugin's shipped `shared/codemap-contract.md` (requires the `codex-rig` plugin) is the active consumer contract: its adapter validates `CODEMAP_BIN` first or PATH fallback once, runs the provider-owned probe/query surface, persists one context artifact, and lets specialists reuse it. The provider-managed `codemap-py-integration.md` block is metadata-only; identity/protocol/timestamp fields do not wire a launcher or prove active guidance. Audit checks provider identity and reachable active consumer guidance separately and reports missing, unreachable, or outdated guidance as bounded source maintenance (or an existing approved `plan_sync` target). Do not borrow another plugin's shared script or edit installed caches. Installed-byte/hash evidence is separate from current-session activation; a native listing without session provenance is not proof, and matching source hashes alone do not prove semantic currency.

NOT for: structural queries (use `/codemap-py:query-code`); standalone index rebuilds (use `/codemap-py:scan-codebase`).

</objective>

<inputs>

- **$ARGUMENTS**: optional:
  - Omitted or `audit` — zero-write inspection of provider, consumer, managed-block, index, runtime-log, and usage evidence.
  - `plan` — persist report artifact (targets, argv, hashes, rollback identities, plan SHA-256); no mutation.
  - `apply` — atomically update current-version managed blocks in allowlisted consumer source from approved plan.
  - `sync` — install/reinstall approved targets locally via native plugin-manager CLIs.
  - `demo` — run `audit` plus one representative structural smoke query; disposable evidence only.

</inputs>

<workflow>

## Step 1: Resolve mode

Parse `$ARGUMENTS` case-insensitively: empty or starts `audit` → audit; `plan` → plan; `apply` → apply; `sync` → sync; `demo` → demo. Otherwise ask `AskUserQuestion`: "Unrecognized command `$ARGUMENTS`. Which of the five modes did you mean?" Options: (a) `audit`, (b) `plan`, (c) `apply`, (d) `sync`, (e) `demo`. Wait for reply.

## Step 2: Run the mode

```bash
"${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py" integrate audit [--runtime <r>] [--json] [--since YYYY-MM-DD]  # timeout: 15000
```

**`audit`** — bounded read-only inspection: provider/consumer versions, observed provider content identity, managed blocks, active consumer guidance reachability/content, index identity, runtime-scoped logs, usage, findings. Reports `pass`, `warn`, or `fail`; never invokes `plan`, `apply`, `sync`, `index`, query self-heal, native plugin-manager mutation, or global-instruction installation. Missing, unreachable, or outdated active guidance is reported as `consumer_query_guidance_missing`, `consumer_query_guidance_unreachable`, or `consumer_query_guidance_drift` and treated as source maintenance, not proof of active wiring. Same-version content mismatch = high-severity drift; native listing without session provenance = `session_catalog: unobservable`. Codex supplies runtime-scoped CLI/tool shards but no skill-start hook; host hooks expose no token usage. Report these evidence limits; never claim live fresh-session activation or token savings. `--json` emits schema 2 (`codemap-py.integration.v2`); `--since` filters telemetry by date. Default text; JSON for downstream reasoning.

```bash
"${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py" integrate plan [--runtime <r>] [--consumers <csv>] [--source <s>] [--out <artifact>]  # timeout: 15000
```

**`plan`** — writes report artifact only. Relay CLI stdout artifact path, op count, and plan SHA-256 verbatim; never paraphrase hash.

```bash
"${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py" integrate apply --plan <artifact> --approve <sha256>  # timeout: 30000
```

**`apply`** — requires `--plan` + `--approve <sha256>` matching shown plan. Before execution, print plan summary + SHA-256; call `AskUserQuestion`: "Apply this plan? (targets: <consumers>, plan SHA-256: <sha256>)" Options: (a) Approve — run `apply` with exact SHA-256, (b) Cancel. Never construct/pass `--approve` without explicit confirmation. Maintainer/source-checkout operation; immutable-release users normally use `audit`, `sync`, `demo`. `apply` never runs reported native reinstall commands; `sync` never rewrites consumer source.

```bash
"${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py" integrate sync --source <s> --plan <artifact> --approve <sha256> [--runtime <r>]  # timeout: 60000
```

**`sync`** — same gate as `apply`: print plan summary + SHA-256; use `AskUserQuestion` before passing `--approve`. Also requires `--source {local-candidate,release}`. After successful Claude-consumer or `codemap-py` install/reinstall, say: "Run `/reload-plugins` (or start a fresh session) before relying on the updated plugin — this session's tool list was resolved before the update." If `--runtime` included `codex` and synced `codex-rig`/`codemap-py`, add: "Start a new Codex session before relying on the updated plugin there too."

```bash
"${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py" integrate demo [--runtime <r>]  # timeout: 20000
```

**`demo`** — runs `audit` + one representative structural smoke query; records protocol/version/evidence. Disposable unless user separately approves mutation. It does not claim a plain-vs-structural comparison, token savings, or current-session activation. Print returned report path.

## Step 3: Report

Report exit meaning: `0` success; `1` runtime/filesystem failure or partial-sync journal (see table); `2` bad syntax or approval. For `sync` exit `1`, report journal state (`planned → approved → applying:<t> → verified:<t> → complete`, or `rollback-started → rollback-succeeded|rollback-failed → recovery-required`). For `recovery-required`, relay only engine-reported bounded manual recovery commands; invent none.

</workflow>
