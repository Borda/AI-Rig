**Deep plugin audit — find ALL gaps, no scope constraint.**

You are reviewing plugin files: may include skill SKILL.md files, agent .md files, shared .md files, rule .md files, or plugin.json manifests. Apply all checks below AND any other issues you find — scope constraint is intentionally absent. For bash operational correctness checks: apply only when the file under review contains bash code blocks; skip silently for agent/rule/manifest files that have no bash.

**Standard structural checks:**

- **Purpose and logical coherence**: role clearly defined? Scope right — not too broad, not too narrow? New user know when to reach for it vs similar one?
- **Structural completeness**: required sections present, tags balanced, step numbering sequential
- **Cross-reference validity**: every agent/skill name mentioned must exist on disk. Cross-reference against Step 2 inventory. Name absent from inventory = **broken cross-reference** (critical). No conditional language — by Step 3, inventory is known.
- **Verbosity and duplication**: bloated steps, repeated instructions, copy-paste between files
- **Content freshness**: outdated model names, stale version pins, deprecated API references
- **Hardcoded user paths**: any `/Users/` or `/home/<name>/` absolute path — must be `.claude/`, `~/`, or `git rev-parse --show-toplevel`. Flag every occurrence at medium severity regardless of context (negative examples not exempt).
- **Infinite loops**: follow-up chains creating cycles (flag, don't auto-fix)
- **Example value vs. token cost**: inline examples that restate surrounding prose in code, illustrate trivial cases, or would be better served by a project-local file

**Bash operational correctness** (applies to files with bash code blocks — skills primarily; skip if no bash present):

- **Pipe exit code capture**: any `cmd 2>&1 | tail -N` or `cmd 2>&1 | head -N` followed by `$?`, `GATE_EXIT=$?`, or `EXIT=$?` — `$?` captures tail/head exit status (always 0), not the actual command. Must use `${PIPESTATUS[0]}` or `set -o pipefail`. Severity: **critical**.
- **SKIP variable guard missing**: `SKIP_X=1` set in a detection block but subsequent runner commands that should be skipped lack an explicit `[ "${SKIP_X:-0}" -ne 1 ] &&` guard. Comments saying "skip if SKIP_X=1" without code guards are cosmetic only. Severity: **critical**.
- **Missing exit on genuine failure**: detecting "all retries failed" / "GENUINE FAILURE" but execution continues instead of `exit 1` (or equivalent stop instruction). Severity: **critical**.
- **Agent filename convention mismatch**: spawn prompt instructs agents to write to a plugin-prefixed filename (`foundry:sw-engineer.md`) but consolidator reads using bare-name pattern (`sw-engineer.md`) — filenames never match, all findings silently dropped. Severity: **high**.
- **TEST_CMD with pytest-specific flags**: `$TEST_CMD` (may resolve to `tox` or `make test`) used with `--tb`, `--co`, `::node_id`, `--cov=`, `--doctest-modules` without a separate `PYTEST_CMD` derivation — tox/make reject these flags. Severity: **high**.
- **Optional dependency invoked without availability check**: calling `/oss:review`, `/codex:*`, or any optional plugin without first checking it is installed. Severity: **high**.
- **TARGET / key variable unset in else branch**: conditional variable assignment where the `else` branch omits assignment, leaving the variable empty when passed downstream. Severity: **high**.
- **Destructive op without confirmation note**: `git checkout HEAD -- <file>`, `git reset --hard`, or any irreversible file operation presented as "just run this" with no explicit "confirm with user before running" note. Severity: **high**.
- **pytest-cov checked with wrong python**: `python -c "import pytest_cov"` bypasses the project virtualenv; must use `$RUNNER python -c "import pytest_cov"`. Severity: **medium**.

**Inter-skill handoff and spawn quality:**

- **--plan receiver missing**: skill documents it accepts `--plan <path>` handoff from `/develop:plan` but has no Step 1 handler that reads the file and skips codebase exploration. Severity: **high**.
- **Spawn context completeness**: agent spawned with no target files, no expected output format, no relevant context — the agent cannot do useful work. Severity: **high** (silent failures look like success).
- **File-handoff protocol**: when 2+ parallel agents write findings, verify each agent writes full output to a file AND returns only a compact JSON envelope. Missing either half breaks aggregation. Severity: **high**.

**Agent files within plugins**: for `agents/*.md` files, apply all structural checks (purpose, cross-references, NOT-for coverage, model tier, verbosity, paths, loops) in addition to the above. Bash checks do not apply to agent files.

**Manifest files** (`plugin.json`): check required fields (`name`, `version`, `description`, `author`), valid semver version string, and that `description` accurately reflects current plugin capabilities.

**No scope constraint**: report every issue you find at any severity. Findings outside the above categories are valid — use judgment. The goal is maximum recall, not precision.
