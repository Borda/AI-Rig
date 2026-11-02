# Development guide

Technical detail for contributors and agents working on AI-Rig itself. User-facing install, workflow, and role documentation is in [README.md](README.md). Each plugin has its own `DEVELOPMENT.md` (or `docs/development.md`) next to its README.

## Installed-all surface

**Claude Code (all six plugins installed)**

- 16 agents: Foundry (10), OSS (4), Research (2).
- 46 skills: Foundry (11), OSS (5), Develop (7), Research (10), Codemap-py (6), bridge_CC-Codex (7).
- 16 namespaced rules: 13 Foundry rules plus one quality-gates rule for each companion that ships rules.
- Host hooks; exact active-module counts are maintained in the [Claude guide](.claude/README.md).

**Codex**

- Codex Rig: 14 skills, 15 role cards, shared gates, calibration, and one optional read-only health hook.
- Codemap-py: six skills, no Codex hook manifest.
- bridge_CC-Codex: four Codex-side skills and the reverse MCP transport.

**OpenCode**

- OpenCode Rig: eleven skills, seven evidence tools, and seven installed role agents as a TypeScript npm package.

## Runtime distribution and trust boundaries

| Runtime         | Source inventory                                                                           | Distribution and trust boundary                                                                                                                                                       |
| --------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code     | Six peer plugins: Foundry, OSS, Develop, Research, Codemap-py, and bridge_CC-Codex.        | Marketplace packages expose plugin-loader agents, skills, hooks, and `CLAUDE.md`; setup projects only documented rules/settings and does not install credentials or remote authority. |
| Codex           | Codex Rig plus optional Codemap-py and bridge_CC-Codex.                                    | Marketplace packages expose namespaced skills and role cards; blank-agent injection or inline fallback is disclosed, while persistent named-agent selection remains unverified.       |
| OpenCode        | OpenCode Rig, one npm package with tools, skills, and installed role agents.               | npm installs versioned TypeScript bytes; `opencode-rig init` writes role agents into the user or project config, and the permission hook only escalates an already-configured answer. |
| Source checkout | `plugins/` is the source of truth; `.claude/` and `.codex/` are host guides/configuration. | Direct marketplace install uses published bytes; `Makefile` is a deliberate restore path that consumes the pushed remote and may project selected local policy.                       |

**Design principle:** make uncertainty, ownership, evidence, and recovery visible while keeping packages independently installable.

A workflow may route to a specialist, but a role card, task name, artifact, or green gate is not proof that a model followed it or that the resulting change is correct. Human review remains the acceptance boundary for consequential work.

## Checkout synchronization

Requires [RTK](https://github.com/rtk-ai/rtk) on `PATH`; `make preflight-sync` checks without changing installations, and every sync entrypoint runs it first. The `Makefile` installs from the pushed GitHub remote, not uncommitted local files.

Extra consent variants:

```bash
make sync-all APPROVE_CLAUDE_CONSENT=0   # leave standing consent unchanged
make sync-codex APPROVE_CLAUDE_CONSENT=1 # also approve standing Claude transfer consent
```

**Claude sync behavior**

- `sync-claude` fails when any managed plugin's install record (user, project, or local scope) differs from the version the freshly registered marketplace declares.
- It ends by listing Claude Code processes started before the install, with their working directories.
- Foundry, oss, develop and research warn at session start when the session runs a replaced copy of the plugin, naming both versions and the `claude --resume <session-id>` step.
- It deletes replaced plugin versions (cache dirs marked `.orphaned_at`) once the marker is at least 24 hours old; override with `make sync-claude ORPHAN_MIN_AGE_HOURS=<n>`. A session still running such a version loses its skill files after that point.
- Each external Claude marketplace/plugin add, update, uninstall, or install command has a 120-second timeout; set `EXTERNAL_PLUGIN_TIMEOUT_SECONDS` to a positive integer to change it.

**Bridge standing consent**

`make sync-all` records the Bridge's standing transfer policy by default when it has operator proof. Direct `make sync-codex` needs `APPROVE_CLAUDE_CONSENT=1` and follows the same rule.

- **Operator proof** is either an interactive terminal on stdin and stdout, or a one-time token minted beforehand with `bridge_consent.py issue-token` and passed as `BRIDGE_OPERATOR_TOKEN` (valid 5 minutes, consumed by one grant).
- **No proof** (piped or scripted run): the stage prints `consent unchanged: needs operator terminal or BRIDGE_OPERATOR_TOKEN` and the sync still succeeds.
- **Explicit `APPROVE_CLAUDE_CONSENT=1` without proof** fails, so a requested grant never silently does nothing. A supplied but invalid or expired token is an error, not a skip.
- **`APPROVE_CLAUDE_CONSENT=0`** (command line or environment) leaves consent unchanged. Other values fail before sync begins.
- After plugin installation and personal-policy sync succeed, Make runs this checkout's Bridge consent helper with `grant --approve`, then checks the stored grant in `CODEX_HOME` (default `~/.codex`). This local step uses checkout files; plugin installation still uses the remote.
- The grant covers task-relevant private repository context and ordinary inference through the provider configured in Claude Code, until revoked. Login, live setup probes, folder binding, and host permission checks keep their own approvals. It runs no provider call or setup skill; an active host may need a restart to load the recorded policy.

## Layout and local development

`plugins/` holds the source of truth. Codemap-py is the shared structural product, not a runtime dependency that silently changes either host. Claude Code loads each plugin's manifest, agents, skills, hooks, and instructions from its marketplace cache. Codex loads Codex Rig's skills and role cards from its package and leaves `.codex/config.toml` and global instructions untouched. OpenCode Rig loads from its repository package specifier, registered globally by `make sync-opencode` or by the checked-in `opencode.jsonc`.

```text
AI-Rig/
├── plugins/           # Claude, Codex, and OpenCode package source
├── .claude/README.md  # Claude host guide
├── .codex/README.md   # Codex host guide
├── opencode.jsonc     # project registration of the OpenCode Rig package
└── Makefile           # pushed-remote restore entry point
```

**Local development without installing**

- Claude Code: `claude --plugin-dir ./plugins/cc_foundry` loads one checked-out plugin for one session (repeat for other packages).
- Codex: use the Codex plugin manager.
- Package build and manifest checks, plus the owning plugin README and `DEVELOPMENT.md`, define the release-grade verification path; a source checkout is not a marketplace release.
- Either is a session-only test path. It publishes nothing, does not update the marketplace, and does not make `make sync-all` consume uncommitted files.
