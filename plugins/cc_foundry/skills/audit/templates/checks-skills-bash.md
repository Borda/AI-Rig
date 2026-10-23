# Skill Checks — Bash Correctness (23, 30)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 23 — Bash command misuse / native tool substitution

```bash
printf "=== Check 23: Bash misuse candidates ===\n"
grep -rn '\bcat \|`cat ' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# ' &&
printf "  hint: replace cat with Read tool\n" || true
grep -rn '\bgrep \|\brg \b' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*grep\|Grep tool\|Use Grep' &&
printf "  hint: replace grep/rg with Grep tool\n" || true
grep -rn '\bfind \b.*-name\|\bls \b.*\*' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Glob\|Use Glob\|Glob tool' &&
printf "  hint: replace find/ls with Glob tool\n" || true
grep -rn 'echo .* >\|tee ' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Write tool\|Use Write' &&
printf "  hint: replace echo-redirect/tee with Write tool\n" || true
grep -rn '\bsed \b\|\bawk \b' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Edit tool\|Use Edit\|awk.*{print\|awk.*BEGIN' &&
printf "  hint: replace sed/awk text-substitution with Edit tool\n" || true
printf "✓: Check 23 scan complete\n"
```

After scan, apply model reasoning to each match — exclude cases where shell command genuinely necessary. Flag only where native tool is direct drop-in.

Never flag a shared-doc load — a `_shared/`, `modes/` or `templates/` file printed by `cat "$VAR/foo.md"` or by a plugin-owned `bin/` loader (`python "${CLAUDE_PLUGIN_ROOT}/bin/<loader>.py" … foo.md`). The Read tool is no drop-in there: it path-matches the version-pinned cache path and re-prompts after every plugin update, while the Bash form prompts once (`plugins/CLAUDE.md` §Loading Shared Docs). Both Bash forms are compliant; never propose converting one into the other.

| Shell command | Preferred native tool | Severity |
| -- | -- | -- |
| `cat <file>` | Read tool | medium |
| `grep`/`rg` for content search | Grep tool | medium |
| `find`/`ls` for file listing | Glob tool | medium |
| `echo … >` / `tee` to write a file | Write tool | medium |
| `sed`/`awk` for text substitution | Edit tool | medium |

### Sub-check 23a — python inline policy (CLAUDE.md / MEMORY.md violation)

`Bash(python:*)` in allow list, covers bare `python script.py`. But `python -c "..."` does NOT match `Bash(python:*)` — Claude Code's permission matcher tokenizes the full prefix, so `python -c` needs a separate `Bash(python -c:*)` entry (intentionally absent). Any `python -c` in a skill body pauses for a permission prompt mid-workflow; user deny = phase fails. Enforcement mechanism for the inline-Python antipattern, not a coverage gap.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 23a: python inline policy ===\n"
find "$_ROOT" -path "*skills/*/SKILL.md" -exec grep -Hn 'python -c\b' {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' &&
printf "  hint: python not in allow list by design — move logic to bin/*.py or use native tools (Read/Write/Edit/Bash with jq)\n" || true
printf "=== Check 23a: heredoc python policy ===\n"
find "$_ROOT" -path "*skills/*/SKILL.md" -exec grep -Hn "python << '\|python <<\"" {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' &&
printf "  hint: CLAUDE.md bans heredoc python; use bin/*.py instead\n" || true
printf "✓: Check 23a scan complete\n"  # timeout: 5000
```

Severity: **high** — permission prompt mid-workflow blocks automation; user deny = skill phase fails.

| Sub-check | Pattern | Severity |
| -- | -- | -- |
| 23a — python -c inline | `python -c` in skill body | high |
| 23a — python heredoc | `python << '` in skill body | high |

**Report only** — never auto-fix; some Bash invocations in example/illustration code blocks intentional.

### Sub-check 23b — `# timeout:` annotation without shell enforcement

`# timeout: N` on a bash line is a hint to Claude Code's Bash tool — no effect when the command runs outside the tool (bin/ scripts, CI, direct shell). Hard enforcement needs `timeout S <cmd>` prefix (bash) or `--timeout S` via argparse passed to every blocking call (Python subprocess). See `bin-authoring-guide.md §Timeout Policy` for patterns and ms→s conversion table.

Rules:

- **Bash call sites**: line with `# timeout: N` must have `timeout S` shell prefix (S = N ÷ 1000); no internal fallback exists.
- **Python call sites**: shell `timeout S` wrapper optional — timeout enforced internally via `--timeout` argparse parameter whose `default=` must equal N ÷ 1000 (from the calling site's `# timeout: N` annotation).
- **Python `bin/` scripts with subprocess**: must expose `--timeout SECS` and pass it to every `subprocess.*` call.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_skill_contracts.py" --check 23b --root "$_ROOT"  # timeout: 30000
```

After scan, apply model reasoning — exclude lines inside illustration/example code blocks (marked `# ✗`, surrounded by explanatory prose, or not reachable as actual tool-call commands). Flag only live executable lines. Severity: **medium** — bash comment-only timeout silently ignored at runtime; Python script missing `--timeout` default has no internal enforcement.

| Sub-check | Pattern | Severity |
| -- | -- | -- |
| 23b — bash comment-only timeout | `# timeout: N` without `timeout S` shell prefix (non-python invocations) | medium |
| 23b — subprocess no timeout= | `subprocess.*` call without `timeout=` in `bin/*.py` | medium |
| 23b — missing --timeout default | Python `bin/` script uses subprocess but no `--timeout` argparse arg | medium |

**Report only** — flag for human review; timeout default values must match `# timeout: N` at call site (N ÷ 1000).

### Sub-check 23c — `eval` for multi-value data output

Skill runs `eval "$(...)"`, `eval "$(python ...)"`, or `eval "$VAR ..."` on a script's stdout or a session-file value, instead of writing to TMPDIR files or running an allowlisted runner. `Bash(eval:*)` is no longer pre-approved, so every such block prompts — and an `eval` of a session file runs whatever a planted file holds. Distinct from shell-setup eval (ssh-agent) — those exempt.

```bash
# timeout: 10000
printf "=== Check 23c: eval for data output ===\n"
{ find plugins -mindepth 4 -maxdepth 4 -path 'plugins/*/skills/*/SKILL.md' -type f -print0
  find plugins -mindepth 4 -maxdepth 4 -path 'plugins/*/skills/_shared/*.md' -type f -print0
  find plugins -mindepth 5 -path 'plugins/*/skills/*/*' -name '*.md' -type f -print0
  find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md -type f -print0; } 2>/dev/null |
  xargs -0 -r awk 'BEGIN { tick = sprintf("%c", 96); open = "^(" tick tick tick tick "*|~~~~*)" } FNR == 1 { fence = "" } { line = $0; sub(/^([[:space:]]*>)*[[:space:]]*/, "", line); run = match(line, open) ? substr(line, 1, RLENGTH) : "" } fence == "" && run != "" { fence = run; next } fence != "" && run != "" && substr(run, 1, 1) == substr(fence, 1, 1) && length(run) >= length(fence) && substr(line, length(run) + 1) ~ /^[[:space:]]*$/ { fence = ""; next } fence != "" { print FILENAME ":" FNR ":" line }' |
  grep -E '(^|[^[:alnum:]_])eval[[:space:]]+(--[[:space:]]+)?["'"'"'$]' |
  grep -v 'ssh-agent\|direnv\|rbenv\|pyenv\|nvm\|# shell-setup' |
  grep -Ev '^[^:]*:[0-9]+:[[:space:]]*#' | head -20
```

No false-positive exemption for `parse-skill-flags.py` or `derive_codemap_target.py`: both write per-session sentinels with `--keep-state` and the caller reads them with `IFS= read -r`. Finding = any `eval "$…"` in a shipped block; fix = sentinel files for data (`bin-authoring-guide.md §Script Output Routing`), a closed `case` that maps a saved runner string to an array (`"${R[@]}" tool …`), or an allowlisted bin runner. Cross-block persistence is Sub-check 23d, not this one.

### Sub-check 23e — command-name rebind in a shipped block

Block rebinds what a command name runs: `hash`, `builtin`, `alias`, `enable`/`disable`, `functions`, `autoload`, `zmodload`, `emulate`, `mapfile`/`readarray`, `vared`, `zparseopts`, `getln` in command position; `set` with an `A` option, `read -a`/`-A` or a `$` name operand, `printf`/`print -v` (past other options and their values), a declaring builtin with `-n` or a `$` in the declared name; any mention of a name→program table (zsh `commands`/`functions`/`aliases`/`galiases`/`saliases`/`dis_*`/`fpath`, bash `BASH_CMDS`/`BASH_ALIASES`, attached option prefix included); or an indirect expansion `${(P)…}`/`${!…}`. Blueprint danger filter denies all of these, so such block never gets a manifest entry and prompts every run; a later harmless-looking command would run whatever the rebind pointed at. `eval` = Sub-check 23c. Scans only `bash`/`sh`/`zsh`/`shell` fences. Each line loses its trailing comment (a `#` inside quotes kept), then its quotes and backslashes (whitespace inside quotes stays part of its word, so `2>"a b" set …` keeps `set` in command position), as the shell would join `\hash` or `co""mmands`, then its redirections with their targets (`2>/dev/null`, `hash>x`, `{fd}>x`), so an operand after one is still read; printed lines show that form. A line ending in `\` joins the next and reports where the command ends. A command word with `$` after its first character (`h$'\x61'sh`) is flagged outright: its name is decoded only at run time. Command position = line start or after `; & | ( { ! $(`, past assignments, redirections and precommand words (`command`, `noglob`, `exec`, `env`, `sudo`, `timeout N` …) with their options and option values (`command --`, `sudo -u root`); an option carrying `v` or `V` ends the prefix, so a `command -v` lookup is not flagged. Advisory: the danger filter stays authoritative. Each word below opens with a bracket class so this block never matches its own text.

```bash
# timeout: 10000
printf "=== Check 23e: command-name rebind ===\n"
CMD='(:[0-9]+:|[;&|({`!]|\$\()[[:space:]]*((then|do|else|[[:alpha:]_][[:alnum:]_]*=[^[:space:]]*|[0-9]*&?[<>][^[:space:]]*|command|noglob|nocorrect|exec|env|sudo|doas|nohup|time|nice|timeout[[:space:]]+[^[:space:]]+|-[^[:space:]vV]*([[:space:]]+[^-[:space:]][^[:space:]]*)?)[[:space:]]+)*'
REB='([h]ash|[b]uiltin|[a]lias|[u]nalias|[e]nable|[d]isable|[f]unctions|[a]utoload|[z]modload|[e]mulate|[m]apfile|[r]eadarray|[v]ared|[z]parseopts|[g]etln)([[:space:]]|$)'
{ find plugins -mindepth 4 -maxdepth 4 -path 'plugins/*/skills/*/SKILL.md' -type f -print0
  find plugins -mindepth 4 -maxdepth 4 -path 'plugins/*/skills/_shared/*.md' -type f -print0
  find plugins -mindepth 5 -path 'plugins/*/skills/*/*' -name '*.md' -type f -print0
  find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md -type f -print0; } 2>/dev/null |
  xargs -0 -r awk 'BEGIN { tick = sprintf("%c", 96); sq = sprintf("%c", 39); soh = sprintf("%c", 1); open = "^(" tick tick tick tick "*|~~~~*)" } FNR == 1 { fence = ""; held = "" } { line = $0; sub(/^([[:space:]]*>)*[[:space:]]*/, "", line); run = match(line, open) ? substr(line, 1, RLENGTH) : "" } fence == "" && run != "" { fence = run; lang = substr(line, length(run) + 1); sub(/^[[:space:]]*/, "", lang); shell = lang ~ /^(bash|sh|zsh|shell)([^[:alnum:]_-]|$)/; next } fence != "" && run != "" && substr(run, 1, 1) == substr(fence, 1, 1) && length(run) >= length(fence) && substr(line, length(run) + 1) ~ /^[[:space:]]*$/ { fence = ""; held = ""; next } fence != "" && shell { line = held line; held = ""; if (line ~ /(^|[^\\])(\\\\)*\\$/) { held = substr(line, 1, length(line) - 1); next } q = ""; out = ""; for (i = 1; i <= length(line); i++) { c = substr(line, i, 1); if (q == "" && c == "#" && (i == 1 || substr(line, i - 1, 1) ~ /[[:space:]]/)) break; if (c == "\\" && q != sq) { out = out c substr(line, i + 1, 1); i++; continue } if (q == "" && (c == "\"" || c == sq)) q = c; else if (q != "" && c == q) q = ""; else if (q != "" && (c == " " || c == "\t")) c = soh; out = out c } gsub(/[\\"]/, "", out); gsub(sq, "", out); gsub(/([0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})?(&>>?|[<>]+&?)([ \t]*[^ \t;&|(<>][^ \t;&|<>]*)?/, " ", out); print FILENAME ":" FNR ":" out }' |
  grep -E -e "$CMD$REB" \
    -e "$CMD"'[s]et[[:space:]]([^;&|]*[[:space:]])?[-+][[:alpha:]]*A' \
    -e "$CMD"'[r]ead[[:space:]]([^;&|]*[[:space:]])?-[[:alpha:]]*[aA]' -e "$CMD"'[r]ead[[:space:]][^;&|<>]*[$`]' \
    -e "$CMD"'[p]rintf?([[:space:]]+-[[:alpha:]]+([[:space:]]+[^[:space:]-][^[:space:]]*)*)*[[:space:]]+-[[:alpha:]]*v' \
    -e "$CMD"'[[:alnum:]_-]*[[:alnum:]_]\$' \
    -e "$CMD"'([d]eclare|[t]ypeset|[l]ocal|[e]xport|[r]eadonly|int[e]ger|[f]loat)[[:space:]]([^;&|]*[[:space:]])?(-[[:alpha:]]*n|[^[:space:]=]*\$[^[:space:]=]*=)' \
    -e '[^[:alnum:]_./-]([-+][[:alpha:]]*)?([c]ommands|[f]unctions|al[i]ases|gal[i]ases|sal[i]ases|[d]is_[[:lower:]_]+|[f]path|[F]PATH|[B]ASH_CMDS|[B]ASH_ALIASES)([^[:alnum:]_./-]|$)' \
    -e '\$\{(\([^)]*P|!)' | tr '\001' ' ' | head -20
```

Finding = any hit in a shipped block; fix = `command -v tool` for a lookup, call the program by its real name or path, and pass values through sentinel files rather than through a variable named at run time.

**Sub-check 23d** — shell variable used for state across separate Bash tool calls. **Not grep-detectable** (requires cross-block analysis of Bash call boundaries, which are runtime not lexical). Flag during curator per-file review only: when auditing a skill, scan for `VAR=$(...)` pattern in one fenced block and `"$VAR"` or `[ -z "$VAR" ]` in a later fenced block with no `cat "${TMPDIR:-/tmp}/...-${CSID}"` supplying `VAR` between them.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 23c — eval in a shipped block | `eval "$…"` of script output or a session-file value | high | no — replace with TMPDIR-file pattern per `bin-authoring-guide.md §Script Output Routing`, a closed-`case` runner array, or an allowlisted bin runner |
| 23d — cross-call shell var | `$VAR` in block N, set in earlier block M, no TMPDIR bridge | medium | no — curator flag only |
| 23e — command-name rebind | rebind command in command position, name→program table, or `${(P)…}`/`${!…}` | medium | no — `command -v` lookup, real program name, sentinel files |

**Report only** — never auto-fix; replacement requires understanding the script's full output contract.

## Check 30 — Plugin skill bash operational correctness

Four static-grep patterns catching silent failures in skill SKILL.md bash blocks. Run across both `.claude/skills/` and `plugins/*/skills/` — bugs appear in any skill.

### 30a — Pipe exit code capture (PIPESTATUS)

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30a: Pipe exit code capture ===\n"
# tail/head always exit 0 — $? after them ≠ upstream cmd's exit
find "$_ROOT" -path "*skills/*" -name "*.md" -exec grep -Hn '| tail\b\|| head\b' {} + 2>/dev/null |
  grep -v 'PIPESTATUS\|pipefail\|#.*tail\|#.*head' |
  grep -v '^Binary' &&
printf "  hint: set -o pipefail; \$? then captures the real upstream exit, not tail/head's (always 0) — PIPESTATUS is bash-only, absent under zsh\n" || true
printf "✓: Check 30a scan complete\n"  # timeout: 5000
```

Severity: **critical** — gate commands appear to pass on genuine failure; `$?` after `cmd | tail -N` = tail's exit code (0), not cmd's.

Fix pattern: `set -o pipefail; cmd 2>&1 | tail -N; EXIT=$?` — never `${PIPESTATUS[0]}`, absent under zsh (Claude Code's Bash tool login shell on macOS).

### 30b — SKIP variable guard missing

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30b: SKIP variable guard ===\n"
# anchor at word start — FORTIFY_SKIP_VARIANT=1 is a printed inter-block signal, not a guard variable; self-excluded because this template quotes the pattern (precedent: Check 27 Step 2). Two -e patterns, not (^|…) — `^` inside an ERE group is not portable to BSD grep.
find "$_ROOT" -path "*skills/*" -name "*.md" ! -name "checks-skills*.md" -exec grep -Hn -E -e '^SKIP_[A-Z_]*=1' -e '[^A-Za-z0-9_]SKIP_[A-Z_]*=1' {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '#' | while IFS= read -r match; do
    file=$(echo "$match" | cut -d: -f1)
    grep -q '\[ "\${SKIP_' "$file" 2>/dev/null ||
      printf "⚠ SKIP guard missing: %s — SKIP variable set but no conditional guard found\n" "$file"
done
printf "✓: Check 30b scan complete\n"  # timeout: 5000
```

Severity: **critical** — `SKIP_RUFF=1` set by tool detection, but the runner call (`"${R[@]}" ruff check`) runs unconditionally; detection is cosmetic.

Fix pattern: `[ "${SKIP_RUFF:-0}" -ne 1 ] && "${R[@]}" ruff check ...` — `R` is the array a closed `case` maps the saved runner string to (`R=(uv run)`); never `eval`, and never a bare `$RUNNER` prefix: a multi-token runner ("uv run") does not word-split under zsh (Claude Code's Bash tool login shell on macOS).

### 30c — Agent filename convention mismatch (model reasoning)

Cannot be caught by grep alone — requires reading spawn prompt and consolidator read pattern in same file.

Flag when skill file:

1. Spawns agents with prompt instructing them to write findings to file named with plugin-prefixed format (e.g. `foundry:sw-engineer.md`)
2. AND consolidator reads files using bare-name format (e.g. `sw-engineer.md`)

These never match → all agent findings silently dropped.

Severity: **high**

Fix: standardize to bare agent name in both spawn prompt and consolidator read pattern (e.g. `sw-engineer.md`).

### 30d — TEST_CMD used with pytest-specific flags without PYTEST_CMD split

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30d: TEST_CMD/PYTEST_CMD split ===\n"
find "$_ROOT" -path "*skills/*" -name "*.md" -exec grep -Hn \
  '\$TEST_CMD.*--tb\b\|\$TEST_CMD.*--co\b\|\$TEST_CMD.*::\|\$TEST_CMD.*--cov\b\|\$TEST_CMD.*--doctest' {} + 2>/dev/null |
  grep -v 'PYTEST_CMD\|#' | grep -v '^Binary' &&
printf "  hint: derive PYTEST_CMD for pytest-specific flags; TEST_CMD=tox or make won't accept --tb/--co/::/--cov\n" || true
printf "✓: Check 30d scan complete\n"  # timeout: 5000
```

Severity: **high** — skill fails silently on tox/make projects when pytest-specific flags appended to TEST_CMD.

Fix: after detecting TEST_CMD, derive `PYTEST_CMD` for targeted runs: `tox` → `PYTEST_CMD="uv run pytest"`; `make test` → `PYTEST_CMD="uv run pytest"`.

**Report only** — no auto-fix; resolution requires understanding each skill's runner detection block.

### 30e — Heredoc python in skill bodies

Heredoc python blocks (`python << 'EOF'`) banned by CLAUDE.md. Distinct from 23a (targets `python -c` one-liners); 30e catches multi-line heredoc forms that bypass one-liner size limit.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30e: Heredoc python ===\n"
find "$_ROOT" -path "*skills/*" -name "*.md" -exec grep -Hn "python <<\|python << '" {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' &&
printf "  hint: CLAUDE.md bans python heredoc; use bin/*.py script instead\n" || true
printf "✓: Check 30e scan complete\n"  # timeout: 5000
```

Severity: **high** — heredoc triggers permission prompt; user deny = workflow block; violates CLAUDE.md §Pre-Authorized Operations.

### 30f — Missing exit on confirmed failure path

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30f: Missing exit on confirmed failure ===\n"
find "$_ROOT" -path "*skills/*" -name "*.md" -exec grep -Hn \
  'GENUINE.FAILURE\|all retries failed\|failed.*abort\|error.*critical\|cannot continue' {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' | while IFS= read -r match; do
    file=$(echo "$match" | cut -d: -f1)
    line=$(echo "$match" | cut -d: -f2)
    context=$(awk "NR>=$line && NR<=$((line+3))" "$file" 2>/dev/null)
    echo "$context" | grep -q 'exit [1-9]' ||
      printf "⚠ missing exit: %s:%s — failure detected but execution continues\n" "$file" "$line"
  done
printf "✓: Check 30f scan complete\n"  # timeout: 5000
```

Severity: **high** — workflow continues past a detected failure; downstream steps produce misleading output or incorrect partial results.

Fix pattern: add `exit 1` (or appropriate non-zero exit) immediately after the failure is confirmed; do NOT continue to next step.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 30a — pipe exit code | `\| tail` / `\| head` without PIPESTATUS | critical | no |
| 30b — SKIP guard missing | `SKIP_X=1` with no `[ "${SKIP_X:-0}" ]` guard | critical | no |
| 30c — filename mismatch | spawn filename ≠ consolidator filename (model reasoning) | high | no |
| 30d — TEST_CMD+pytest flags | `$TEST_CMD --tb` / `--co` / `::` / `--cov` without PYTEST_CMD | high | no |
| 30e — heredoc python | `python <<` in skill body | high | no |
| 30f — missing exit | confirmed failure with no `exit 1` within 3 lines | high | no |
