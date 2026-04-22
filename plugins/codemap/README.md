# 🗂️ codemap — Claude Code Plugin

> **Every `/develop:fix`, `/develop:refactor`, and `/oss:review` you run gets blast-radius context automatically — without you doing anything.**

codemap builds a structural index of your Python project — import graph, blast-radius scores, function call graph — and injects that context into your existing `/develop` and `/oss` skills. Run setup once; after that it is invisible infrastructure. When you ask Claude to fix `auth.py`, the agent already knows which 38 other modules import it before it touches a single line.

You do not use codemap by querying it directly. You use it by wiring it in and letting other skills pick it up.

**Python first.** The scanner uses `ast.parse` to index `.py` files. Non-Python files are not scanned. Support for TypeScript, Go, and Rust is planned.

______________________________________________________________________

<details>
<summary><strong>📋 Contents</strong></summary>

- [What is codemap?](#what-is-codemap)
- [Why codemap?](#why-codemap)
- [Install](#install)
- [Quick start](#quick-start)
- [Skills reference](#skills-reference)
  - [integration](#integration)
  - [scan](#scan)
  - [query](#query)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Contributing / feedback](#contributing--feedback)

</details>

______________________________________________________________________

## 🤔 What is codemap?

codemap is a Claude Code plugin for Python projects. It pre-builds a structural index — who imports whom, which modules have the widest blast radius, how functions call each other — and injects that context into the `/develop` and `/oss` skills that do real code work. The index is built once and stays current via an optional post-commit hook. Every skill invocation that follows starts with structural awareness already in hand.

Without codemap, every Claude Code session starts blind: the agent gropes through the codebase with Glob and Grep, burning 20–30 tool calls just to understand structure before it can do any real work. On a 200-module project those calls still miss blast-radius risks and import cycles that a structural scan would surface instantly.

codemap solves this: scan once, wire in once, then every skill that touches code benefits automatically.

______________________________________________________________________

## 🎯 Why codemap?

### Without codemap

You ask Claude to refactor `auth.py`. The agent:

1. Globs every `.py` file to find the project layout.
2. Reads files one by one to discover what imports `auth`.
3. Guesses at blast radius from the files it happened to read.
4. Starts editing, discovers mid-refactor that `middleware.py` also imports `auth`, backtracks.
5. Times out on large projects before surfacing all affected modules.

On pytorch-lightning (646 modules), plain-arm agents hit the 300-second hard timeout on three out of eight benchmark tasks.

### With codemap

After , your existing skills are wired in. Now when you run , before spawning any agent the skill silently runs:

```bash
scan-query central --top 5         # which modules are highest risk overall?
scan-query rdeps mypackage.auth    # what breaks if auth changes?
```

That output is prepended to the agent spawn prompt as structural context. The agent starts the refactor already knowing full blast radius — no cold exploration, no mid-refactor surprise that also imports . Benchmark results across 48 runs on pytorch-lightning:

| Metric             |  Haiku   |  Sonnet  |   Opus   |
| :----------------- | :------: | :------: | :------: |
| Elapsed time       | **−51%** | **−60%** | **−71%** |
| Tool result tokens | **−85%** | **−84%** | **−93%** |
| Tool calls         | **−43%** | **−61%** | **−77%** |

Zero codemap timeouts. Three plain-arm timeouts.

______________________________________________________________________

## 📦 Install

<details>
<summary><strong>Prerequisites</strong></summary>

- Claude Code installed and working
- Python 3 on PATH (standard library only — no `pip install` required)
- Git (recommended — used for staleness detection and incremental rebuilds)

</details>

**Install the plugin**

Run this from the directory that **contains** your Borda-AI-Rig clone:

```bash
claude plugin marketplace add ./Borda-AI-Rig
claude plugin install codemap@borda-ai-rig
```

That's it. No build step. The scanner (`scan-index`) and query CLI (`scan-query`) are plain Python scripts — they run immediately.

**Make scan-query available in your terminal (optional)**

Inside Claude Code sessions, `scan-query` and `scan-index` are on PATH automatically via the plugin's `bin/` directory. To use them in your regular terminal too, add this to `~/.zshrc` or `~/.bashrc`:

```bash
CODEMAP_TOOLS=$(ls -d "$HOME/.claude/plugins/cache/borda-ai-rig/codemap"/*/bin 2>/dev/null | sort -V | tail -1)
[ -n "$CODEMAP_TOOLS" ] && export PATH="$PATH:$CODEMAP_TOOLS"
```

Reload your shell (`source ~/.zshrc`) and `scan-query` is available everywhere. This snippet always picks up the latest installed version automatically — no version pins to maintain.

<details>
<summary><strong>Upgrade</strong></summary>

```bash
cd Borda-AI-Rig && git pull
claude plugin install codemap@borda-ai-rig
```

</details>

<details>
<summary><strong>Uninstall</strong></summary>

```bash
claude plugin uninstall codemap
```

</details>

______________________________________________________________________

## ⚡ Quick start

Two commands — then forget about codemap and just use your normal skills.

**Step 1 — build the index:**

```text
/codemap:scan
```

Output:

```text
[codemap] ✓ .cache/scan/myproject.json
[codemap]   312 modules indexed, 2 degraded

Modules: 312 indexed, 2 degraded
Symbols: 4,821 (functions, classes, methods)
Calls:   18,340 resolved call edges (v3 index)

Most central (by rdep_count):
  89  myproject.models
  41  myproject.config
  38  myproject.utils
  27  myproject.exceptions
  19  myproject.auth
```

**Step 2 — wire codemap into your installed skills:**

```text
/codemap:integration init
```

This discovers all your installed `develop` and `oss` skills, shows a recommendation table, and injects the structural context block into each one you approve. It also offers to install a post-commit git hook so the index stays current automatically.

That is it. Now run your normal skills — codemap works silently in the background:

```text
/develop:fix auth.py         # agent already knows blast radius of auth before it starts
/develop:refactor models.py  # agent sees which 89 modules import models upfront
/oss:review                  # reviewer gets structural context on changed modules
```

If you ever want to explore structure manually, `/codemap:query` is there for you — but most users rarely need it.

______________________________________________________________________

## 🔧 Skills reference

______________________________________________________________________

### integration

**Trigger**: `/codemap:integration check | init [--approve]`

Two modes. Run `init` once to wire codemap into your existing skills and agents. Run `check` anytime to verify the setup is healthy.

#### check mode

A fast diagnostic with no side effects. Checks:

1. `scan-query` is reachable on PATH (or found via fallback locations)
2. The index file exists for the current project
3. The index age (warns if older than 7 days)
4. A smoke test: runs `central --top 3` and verifies output
5. Which installed skill files have the codemap injection block

Each check prints `✓`, `✗`, or `⚠` with a one-line remediation hint if needed.

```text
/codemap:integration check
```

#### init mode

Interactive onboarding for the current project:

1. Builds the index if it is missing (offers to run `/codemap:scan`)
2. Discovers all installed skills and agents across all plugins
3. Scores candidates by value tier (High / Medium / Low / Skip) based on whether structural context would help them
4. Presents a recommendation table and asks which to wire in
5. Inserts the correct injection block into each selected skill or agent file
6. Offers to install a `.git/hooks/post-commit` hook for automatic incremental rebuilds

```text
/codemap:integration init
```

Pass `--approve` to apply all High and Medium recommendations non-interactively:

```text
/codemap:integration init --approve
```

#### Manual injection

If you write custom skills or agents and want to add codemap yourself, drop this soft-check block before the first agent spawn. It runs when codemap is available and silently skips when it is not:

```bash
# Structural context (codemap — Python projects only, silent skip if absent)
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    scan-query central --top 5  # timeout: 5000
fi
# If results returned: prepend ## Structural Context (codemap) to the agent spawn prompt.
```

For skills that know the target module up front (refactor, fix), also add targeted queries:

```bash
scan-query rdeps "$TARGET_MODULE" 2>/dev/null  # timeout: 5000
scan-query deps  "$TARGET_MODULE" 2>/dev/null  # timeout: 5000
```

For agent `.md` files, add this instruction before the closing section:

```markdown
**Structural context (codemap — Python projects only)**: if `.cache/scan/<project>.json` exists,
run `scan-query central --top 5` (and `scan-query rdeps <target_module>` when a target is known)
**before** any Glob/Grep exploration for structural information. Skip silently if the index is absent.
```

### scan

**Trigger**: `/codemap:scan`

Builds the structural index by running `ast.parse` across every `.py` file in the project. Writes the index to `.cache/scan/<project>.json`. Reports how many modules were indexed, how many were degraded (parse errors), and which five modules have the highest blast radius.

#### Flags

| Flag            | What it does                                                                                                                       |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| _(none)_        | Full scan — re-parses every `.py` file                                                                                             |
| `--incremental` | Re-parse only files that changed since the last scan (uses git blob SHA comparison); falls back to full scan if no v3 index exists |
| `--root <path>` | Scan a specific directory instead of the git root                                                                                  |

#### When to run

Run a full scan once when you first set up the project. After that, `--incremental` is fast enough to run after any significant change. If you install the post-commit git hook (via `/codemap:integration init`), incremental rebuilds happen automatically in the background after every commit — you never need to think about it.

#### Performance

| Project size | Full scan | Incremental (5 files changed) |
| ------------ | --------- | ----------------------------- |
| ~200 modules | ~25s      | ~75ms                         |
| ~650 modules | ~60s      | ~75ms                         |

#### Example

```text
/codemap:scan
```

```text
/codemap:scan --incremental
```

______________________________________________________________________

<a id="query"></a>

<details>
<summary>

### query — full subcommand reference

</summary>

### query

**Trigger**: `/codemap:query <subcommand> [args]`

Queries the index. Every query checks staleness automatically — if Python files were committed after the index was built, you'll see a warning on stderr and a suggestion to re-scan. Results are still returned so the agent can decide whether to proceed or refresh first.

#### Module-level queries

These work with any v2 or v3 index.

| Subcommand          | What it answers                                                       |
| ------------------- | --------------------------------------------------------------------- |
| `rdeps <module>`    | What imports this module? (blast radius)                              |
| `deps <module>`     | What does this module import?                                         |
| `central [--top N]` | Which modules are imported by the most others? Default N=10           |
| `coupled [--top N]` | Which modules import the most others? Default N=10                    |
| `path <from> <to>`  | Shortest import chain between two modules; `null` means not connected |
| `list`              | All indexed modules with their file paths                             |

#### Symbol-level queries

Retrieve function or class source by name instead of reading the full file. Reduces token usage by ~94% compared to reading the whole file.

| Subcommand              | What it answers                                   |
| ----------------------- | ------------------------------------------------- |
| `symbol <name>`         | Source of a function, class, or method by name    |
| `symbols <module>`      | All symbols in a module with type and line range  |
| `find-symbol <pattern>` | Regex search across all symbol names in the index |

`symbol` accepts bare name (`authenticate`), qualified name (`MyClass.authenticate`), or a case-insensitive substring fallback.

#### Function-level call graph queries (v3 index)

These require a v3 index built by `/codemap:scan`. If your index is older (v2), the commands return a clear upgrade message.

| Subcommand             | What it answers                                        |
| ---------------------- | ------------------------------------------------------ |
| `fn-deps <qname>`      | What does this function call? (outgoing call edges)    |
| `fn-rdeps <qname>`     | What functions call this one? (incoming call edges)    |
| `fn-central [--top N]` | Most-called functions across the project. Default N=10 |
| `fn-blast <qname>`     | Transitive reverse-call BFS with depth levels          |

Use `module::function` format for qualified names, for example `mypackage.auth::validate_token` or `mypackage.auth::AuthMiddleware.process`.

**Call edge resolution types**: `import` = cross-module call with confirmed import scope; `local` = same-file call; `self` = `self.method()` call where the target class is known; `star` = call to a name from a star import where the source module could not be determined; `unresolved` = call target could not be matched.

#### Common patterns

```text
# Before refactoring auth.py — understand full blast radius
/codemap:query rdeps myproject.auth

# Before adding a dependency to models.py — see what already imports it
/codemap:query central --top 5

# Check if api and db are already coupled before adding a direct import
/codemap:query path myproject.api myproject.db

# Read just the validate_token function without loading the whole file
/codemap:query symbol validate_token

# Find all functions whose name starts with "validate"
/codemap:query find-symbol "^validate"

# Check transitive impact of changing fetch_user at the function level
/codemap:query fn-blast myproject.db::fetch_user

# Exclude test modules from blast-radius analysis
/codemap:query central --exclude-tests --top 10
```

</details>

## ⚙️ How it works

### The scanner (`scan-index`)

`scan-index` is a plain Python 3 script with no external dependencies. It:

1. Walks every `.py` file under the project root, skipping common non-source directories (`.git`, `.venv`, `__pycache__`, `dist`, `build`, and others).
2. Parses each file with `ast.parse` to extract import statements and symbol definitions (classes, functions, methods with line ranges).
3. Resolves call edges per function: cross-module calls tagged as `import`, same-file calls as `local`, `self.method()` patterns as `self`, star-import calls as `star`.
4. Computes graph metrics for each module: `rdep_count` (how many project modules import this one), `dep_count` (how many modules this one imports), `rcall_count` (how many functions across the project call any function in this module).
5. Stores per-file git blob SHAs (`file_shas`) so incremental rebuilds can identify exactly which files changed.
6. Writes everything to `.cache/scan/<project>.json` as a single JSON file.

Files that cannot be parsed (syntax errors, encoding issues) are marked `degraded` with a reason. The scan never aborts — a file that fails parsing is noted and skipped.

### The query CLI (`scan-query`)

`scan-query` is a companion Python 3 script that loads the index and answers structural questions. It checks staleness on every call by comparing current git blob SHAs against the stored `file_shas`. If files have changed, it warns to stderr and returns results anyway.

All output is JSON. This makes it easy to pipe directly into agent spawn prompts, shell scripts, or further analysis.

### The index file

The index lives at `.cache/scan/<project>.json` where `<project>` is the basename of the git root directory. It is a single flat JSON file — nothing needs to keep running. The format is versioned (`scan_version: 3` in current builds).

Key fields per module entry:

| Field            | Meaning                                                                                               |
| ---------------- | ----------------------------------------------------------------------------------------------------- |
| `name`           | Fully qualified module name (e.g. `mypackage.auth`)                                                   |
| `path`           | Path to the `.py` file relative to project root                                                       |
| `rdep_count`     | Number of project modules that import this one (blast-radius proxy)                                   |
| `dep_count`      | Number of modules this one imports (coupling proxy)                                                   |
| `rcall_count`    | Number of functions across the project that call into this module (function-level blast-radius proxy) |
| `direct_imports` | List of modules this file imports                                                                     |
| `symbols`        | Functions, classes, and methods with line ranges and call edges                                       |
| `status`         | `ok` or `degraded`                                                                                    |
| `is_test`        | Whether the file is in a test directory                                                               |
| `file_shas`      | Git blob SHA or MD5 hash for incremental rebuild detection                                            |

### How agents use it

When the develop plugin (or any codemap-integrated skill) spawns an agent, it runs `scan-query central --top 5` and optionally `scan-query rdeps <target_module>` first. The JSON output is prepended to the agent's spawn prompt as a `## Structural Context (codemap)` block. The agent starts its work already knowing which modules are highest risk and what depends on its target — no cold exploration required.

If codemap is not installed, the soft-check block silently skips and the skill works exactly as before.

______________________________________________________________________

## ⚙️ Configuration

codemap has no required configuration. Everything is automatic once installed.

### Index location

The index is always written to `.cache/scan/<project>.json` at the project root. This directory is gitignored by default in the borda-ai-rig artifact layout. The project name is derived from `basename $(git rev-parse --show-toplevel)` — the directory name of your git root.

### Non-git projects

`scan-index` falls back to MD5 file hashes when git is not available. Staleness detection and incremental rebuilds still work; they just use file content hashes instead of git blob SHAs.

### Custom scan root

If your Python source is not at the git root, pass `--root`:

```text
/codemap:scan --root src/mypackage
```

Or from the terminal:

```bash
scan-index --root src/mypackage
```

### Automatic index freshness (post-commit hook)

Install the hook once via `/codemap:integration init` and answer yes to the hook prompt. After that, every `git commit` triggers an incremental background rebuild automatically:

```bash
# .git/hooks/post-commit (installed by /codemap:integration init)
# codemap: incremental index rebuild — do not remove this line
if command -v scan-index >/dev/null 2>&1; then
    scan-index --incremental 2>/dev/null &
fi
```

The rebuild runs in the background — your commit completes immediately, the index updates silently within seconds.

______________________________________________________________________

## 🔍 Troubleshooting

### "index not found" or empty results

The index has not been built for this project yet. Run:

```text
/codemap:scan
```

### Stale index warning

`scan-query` detected that Python files were committed after the index was built. Run an incremental rebuild:

```text
/codemap:scan --incremental
```

Or a full rebuild if you have made large structural changes:

```text
/codemap:scan
```

### scan-query not found in the terminal

You are outside a Claude Code session where the plugin `bin/` directory is not on PATH. Add it to your shell config (see [Install](#install) — the shell PATH snippet). After reloading your shell, `scan-query` should be available. You can verify with:

```bash
command -v scan-query
```

<details>
<summary>

Degraded modules in the scan report

</summary>

### Degraded modules in the scan report

Some files could not be parsed — usually generated code, files with syntax errors, or files that use Python syntax features not yet supported by the standard library `ast` module. Degraded modules are skipped but the rest of the index is fully usable. To see which files are degraded:

```bash
python3 -c "
import json, os, subprocess
proj = os.path.basename(subprocess.check_output(['git', 'rev-parse', '--show-toplevel']).decode().strip())
d = json.load(open(f'.cache/scan/{proj}.json'))
for m in d['modules']:
    if m.get('status') == 'degraded':
        print(m['path'], '--', m.get('reason', 'unknown'))
"
```

Generated files (e.g. protobuf output) are expected to degrade. They are not part of your project's logical import graph.

</details>

### fn-\* commands return "upgrade required"

The function-level call graph queries (`fn-deps`, `fn-rdeps`, `fn-central`, `fn-blast`) require a v3 index. Your current index is older. Rebuild:

```text
/codemap:scan
```

### The develop plugin does not seem to use codemap

Run the integration check:

```text
/codemap:integration check
```

Look for `⚠ missing injection in:` lines pointing to specific skill files. If injection is missing, run:

```text
/codemap:integration init
```

and select the skills you want wired in.

______________________________________________________________________

<a id="contributing--feedback"></a>

## 🙏 Contributing / feedback

codemap lives in the `plugins/codemap/` directory of the Borda-AI-Rig repository.

**Found a bug or want a feature?** Open an issue in the repository. Include:

- Your Python version (`python3 --version`)
- The codemap version (`cat ~/.claude/plugins/cache/borda-ai-rig/codemap/*/.claude-plugin/plugin.json`)
- The error message or unexpected behavior
- The approximate size of the project you were scanning (module count from scan output)

**Want to extend codemap?**

The scanner and query CLI are standalone Python scripts in `plugins/codemap/bin/`. They have no external dependencies and are easy to read and modify. The index schema is versioned — if you add new fields, bump `SCAN_VERSION` in `scan-index` and handle the version check in `scan-query`.

Skills live in `plugins/codemap/skills/*/SKILL.md`. Adding a new skill means creating a new subdirectory with a `SKILL.md` following the existing pattern.

After any edit to agents, skills, or the index schema, update this README before committing — the plugin CLAUDE.md requires it.

**Plugin updates** propagate via the normal install path:

```bash
cd Borda-AI-Rig && git pull
claude plugin install codemap@borda-ai-rig
```

After upgrading, run `/codemap:integration check` to confirm everything is still wired correctly.
