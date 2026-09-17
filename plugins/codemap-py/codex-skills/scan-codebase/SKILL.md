---
name: scan-codebase
description: '`$codemap-py:scan-codebase [flags]` only: Python index; never auto-invoke; skip query/integration.'
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Scan Codebase

Python only: `ast.parse` records imports plus classes/functions/methods and line ranges for every `.py`; non-Python files are excluded. Writes `.cache/codemap/<project>.json` without external dependencies. A zero-Python project writes a valid empty index; downstream queries return no results. Symbol data lets `$codemap-py:query-code symbol`/`find-symbol` return target source instead of full-file reads (~70–94% fewer `Read` tokens).

NOT for: existing-index query (use `$codemap-py:query-code`); integration health (use `$codemap-py:integration`).

**Explicit invocation only** — Codex cannot declare `disable-model-invocation`; never call this skill autonomously, only for the user's literal `$codemap-py:scan-codebase` trigger.

## Runtime note

Codex has no `bin/` PATH entry or plugin-root variable. Resolve the installed root once, replace `PLUGIN_ROOT` literally in commands, and retain it in reasoning; shell state does not persist.

## Workflow

### 1. Scan

Parse only `--root <path>` and `--incremental`. For any other `--` token, report `! Unknown flag(s): <tokens>` then `Supported: --root <path>, --incremental` and stop; do not guess. Use this exact `Unknown flag(s)` wording.

```bash
PLUGIN_ROOT/bin/codemap-py index [--root <path>] [--incremental]
```

On Windows use `PLUGIN_ROOT\bin\codemap-py.cmd index ...`. `--root` names the index from `basename(<path>)`, unlike the default git-root basename. After a custom-root scan, retain the exact path printed by the scanner as `<emitted-index-path>`; a later query must use `--index <emitted-index-path> --root <same-root>`. `--root` is path resolution only and does not select the index. The scanner writes `<root>/.cache/codemap/<project>.json` (or `$CODEMAP_INDEX_DIR/<project>.json`) and prints indexed/degraded counts. On non-zero exit, report it and stop; do not retry silently (`1` index/filesystem failure, `2` syntax).

### 2. Report

Report module and degraded counts; degraded is informational and the index remains usable. For zero Python, report valid but empty. Then suggest:

```text
Index ready. Query it with $codemap-py:query-code — central --top 10, deps/rdeps <module>, coupled --top 10.
See $codemap-py:query-code for the full subcommand list.
```
