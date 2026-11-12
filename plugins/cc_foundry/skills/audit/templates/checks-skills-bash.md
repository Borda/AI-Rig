# Skill Checks — Bash Correctness (23, 30)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 23 — Bash command misuse / native tool substitution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 23: Bash misuse candidates ===\n"
find "$_ROOT" \( -name worktrees -o -name node_modules -o -name .venv -o -path '*/tests/*' \) -prune -o -type f -name '*.md' \( -path '*/agents/*' -o -path '*skills/*' -o -path '*/rules/*' \) -exec grep -Hn '\bcat \|`cat ' {} + 2>/dev/null |
grep -v '^Binary' | grep -v '# ' &&
printf "  hint: replace cat with Read tool\n" || true
find "$_ROOT" \( -name worktrees -o -name node_modules -o -name .venv -o -path '*/tests/*' \) -prune -o -type f -name '*.md' \( -path '*/agents/*' -o -path '*skills/*' -o -path '*/rules/*' \) -exec grep -Hn '\bgrep \|\brg \b' {} + 2>/dev/null |
grep -v '^Binary' | grep -v '# .*grep\|Grep tool\|Use Grep' &&
printf "  hint: replace grep/rg with Grep tool\n" || true
find "$_ROOT" \( -name worktrees -o -name node_modules -o -name .venv -o -path '*/tests/*' \) -prune -o -type f -name '*.md' \( -path '*/agents/*' -o -path '*skills/*' -o -path '*/rules/*' \) -exec grep -Hn '\bfind \b.*-name\|\bls \b.*\*' {} + 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Glob\|Use Glob\|Glob tool' &&
printf "  hint: replace find/ls with Glob tool\n" || true
find "$_ROOT" \( -name worktrees -o -name node_modules -o -name .venv -o -path '*/tests/*' \) -prune -o -type f -name '*.md' \( -path '*/agents/*' -o -path '*skills/*' -o -path '*/rules/*' \) -exec grep -Hn 'echo .* >\|tee ' {} + 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Write tool\|Use Write' &&
printf "  hint: replace echo-redirect/tee with Write tool\n" || true
find "$_ROOT" \( -name worktrees -o -name node_modules -o -name .venv -o -path '*/tests/*' \) -prune -o -type f -name '*.md' \( -path '*/agents/*' -o -path '*skills/*' -o -path '*/rules/*' \) -exec grep -Hn '\bsed \b\|\bawk \b' {} + 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Edit tool\|Use Edit\|awk.*{print\|awk.*BEGIN' &&
printf "  hint: replace sed/awk text-substitution with Edit tool\n" || true
printf "✓: Check 23 scan complete\n"
```

After scan, apply model reasoning to each match — exclude cases where shell command genuinely necessary. Flag only where native tool is direct drop-in.

Never flag a shared-doc load solely for using Bash — a token-free `_shared/`, `modes/` or `templates/` file may be printed by `cat "$VAR/foo.md"`; a document containing bootstrap tokens must use a plugin-owned loader or `render_plugin_root.py` after the block's guarded resolver prologue (`python "$PLUGIN_ROOT/bin/<loader>.py" … foo.md`). The Read tool is no drop-in there: it path-matches the version-pinned cache path and re-prompts after every plugin update, while the Bash form prompts once (`plugins/CLAUDE.md` §Loading Shared Docs). Both Bash forms are compliant within that token constraint; never propose converting one into the other unless rendering is required.

| Shell command | Preferred native tool | Severity |
| -- | -- | -- |
| `cat <file>` | Read tool | medium |
| `grep`/`rg` for content search | Grep tool | medium |
| `find`/`ls` for file listing | Glob tool | medium |
| `echo … >` / `tee` to write a file | Write tool | medium |
| `sed`/`awk` for text substitution | Edit tool | medium |

### Sub-check 23a — Captured inline Python permission coverage

`Bash(python:*)` covers bare `python script.py` and `python -c "..."`. A command substitution such as `VALUE=$(python -c "...")` can still reach the expansion permission gate unless the plugin's blueprint hook admits the command. Collect captures from all skill Markdown, including modes and templates; bare `python -c` is not a permission finding. Inline-Python language restrictions remain a separate authoring policy.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 23a: captured Python review candidates ===\n"
find "$_ROOT" \( -name worktrees -o -name node_modules -o -name .venv -o -path '*/tests/*' \) -prune -o -type f -path "*skills/*" -name '*.md' -exec grep -HnE '\$\([[:space:]]*python[[:space:]]+-c([[:space:]]|$)' {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' &&
printf "  hint: review the enclosing Bash block against its plugin blueprint manifest before flagging\n" || true
printf "=== Check 23a: heredoc python policy ===\n"
find "$_ROOT" \( -name worktrees -o -name node_modules -o -name .venv -o -path '*/tests/*' \) -prune -o -type f -path "*skills/*" -name '*.md' -exec grep -Hn "python << '\|python <<\"" {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' &&
printf "  hint: CLAUDE.md bans heredoc python; use bin/*.py instead\n" || true
printf "✓: Check 23a scan complete\n"  # timeout: 5000
```

For each capture candidate, resolve the owning plugin (its manifest directory locally, its active install record when installed), read its `blueprint-manifest.json`, and compare the enclosing executable Bash block using the hook's normalization contract. A whole-block digest hit or coverage of every logical command admits the block; a matching `src` path alone does not. Exclude admitted blocks and illustrative text. Only a live capture with verified missing coverage is a **high** permission finding. A missing or unreadable manifest leaves coverage **unverified**; report that limit rather than assert a permission failure. The grep output is candidate evidence, never a finding by itself.

| Sub-check | Pattern | Severity |
| -- | -- | -- |
| 23a — captured python -c | `$(python -c ...)` in a live Bash block without blueprint coverage | high after review |
| 23a — python heredoc | `python << '` in skill body | high |

**Report only** — never auto-fix; some Bash invocations in example/illustration code blocks intentional.

### Sub-check 23b — `# timeout:` annotation without shell enforcement

`# timeout: N` on a bash line supplies the timeout hint for Claude Code's Bash tool. The comment has no effect outside that tool (bin/ scripts, CI, direct shell), where the execution path must enforce its deadline using a supported runner or a Python `subprocess` timeout. Do not require a `timeout` binary: it is not available by default on macOS or native Windows. See `bin-authoring-guide.md §Timeout Policy` for patterns and ms→s conversion table.

Rules:

- **Bash tool call sites**: keep `# timeout: N` and pass N as the tool timeout; no shell wrapper is required.
- **Non-tool shell call sites**: require deadline enforcement by the runner or a portable wrapper (S = N ÷ 1000); a comment alone is insufficient.
- **Python call sites**: enforce the deadline internally; an exposed `--timeout` argparse parameter's `default=` must equal N ÷ 1000 (from the calling site's `# timeout: N` annotation). A fixed enforced timeout is sufficient when callers need no override.
- **Python `bin/` scripts with subprocess**: pass an enforced timeout to every blocking `subprocess.*` call; expose `--timeout SECS` only when callers need an override.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
python "$PLUGIN_ROOT/bin/check_skill_contracts.py" --check 23b --root "$_ROOT"  # timeout: 30000
```

After scan, apply model reasoning — exclude Bash tool call sites whose tool timeout uses the annotation, and lines inside illustration/example code blocks (marked `# ✗`, surrounded by explanatory prose, or not reachable as actual tool-call commands). The helper's shell-wrapper hint is not portable guidance; apply the execution-path rules above. Flag only live non-tool paths without enforcement or Python blocking calls without internal enforcement. Severity: **medium** — a deadline is silently ignored on that execution path.

| Sub-check | Pattern | Severity |
| -- | -- | -- |
| 23b — non-tool comment-only timeout | `# timeout: N` on a non-tool execution path without a runner or internal deadline | medium |
| 23b — subprocess no timeout= | `subprocess.*` call without `timeout=` in `bin/*.py` | medium |
| 23b — missing configurable timeout | Python `bin/` script needs a caller override but exposes no matching `--timeout` default | medium |

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

A complete ordinary single-quoted first pattern after literal `grep` and alphabetic short options is inert data, so the scan masks that operand before matching rebinding names. Other operand shapes remain conservatively scanned; extend this bounded exception only after another concrete false finding establishes the need. Quoted command words, declaration targets, ANSI-C quoting, and concatenated operands retain their checks.

Block rebinds what a command name runs: `hash`, `builtin`, `alias`, `enable`/`disable`, `functions`, `autoload`, `zmodload`, `emulate`, `mapfile`/`readarray`, `vared`, `zparseopts`, `getln` in command position; `set` with an `A` option, `read -a`/`-A` or a `$` name operand, `printf`/`print -v` (past other options and their values), a declaring builtin with `-n` or a `$` in the declared name; any mention of a name→program table (zsh `commands`/`functions`/`aliases`/`galiases`/`saliases`/`dis_*`/`fpath`, bash `BASH_CMDS`/`BASH_ALIASES`, attached option prefix included); or an indirect expansion `${(P)…}`/`${!…}`. Blueprint danger filter denies all of these, so such block never gets a manifest entry and prompts every run; a later harmless-looking command would run whatever the rebind pointed at. `eval` = Sub-check 23c. Scans only `bash`/`sh`/`zsh`/`shell` fences. Each line loses its trailing comment (a `#` inside quotes kept), then its quotes and backslashes (whitespace inside quotes stays part of its word, so `2>"a b" set …` keeps `set` in command position), as the shell would join `\hash` or `co""mmands`, then its redirections with their targets (`2>/dev/null`, `hash>x`, `{fd}>x`), so an operand after one is still read; printed lines show that form. A line ending in `\` joins the next and reports where the command ends. A command word with `$` after its first character (`h$'\x61'sh`) is flagged outright: its name is decoded only at run time. Command position = line start or after `; & | ( { ! $(`, past assignments, redirections and precommand words (`command`, `noglob`, `exec`, `env`, `sudo`, `timeout N` …) with their options and option values (`command --`, `sudo -u root`); an option carrying `v` or `V` ends the prefix, so a `command -v` lookup is not flagged. Advisory: the danger filter stays authoritative. Each word below opens with a bracket class so this block never matches its own text.

```bash
# timeout: 10000
printf "=== Check 23e: command-name rebind ===\n"
TICK=$(awk 'BEGIN {printf "%c", 96}')  # backtick built live: a literal one stored in CMD reads as stored code to the danger gate
CMD='(:[0-9]+:|[;&|({'"$TICK"'!]|\$\()[[:space:]]*((then|do|else|[[:alpha:]_][[:alnum:]_]*=[^[:space:]]*|[0-9]*&?[<>][^[:space:]]*|command|noglob|nocorrect|exec|env|sudo|doas|nohup|time|nice|timeout[[:space:]]+[^[:space:]]+|-[^[:space:]vV]*([[:space:]]+[^-[:space:]][^[:space:]]*)?)[[:space:]]+)*'
REB='([h]ash|[b]uiltin|[a]lias|[u]nalias|[e]nable|[d]isable|[f]unctions|[a]utoload|[z]modload|[e]mulate|[m]apfile|[r]eadarray|[v]ared|[z]parseopts|[g]etln)([[:space:]]|$)'
{ find plugins -mindepth 4 -maxdepth 4 -path 'plugins/*/skills/*/SKILL.md' -type f -print0
  find plugins -mindepth 4 -maxdepth 4 -path 'plugins/*/skills/_shared/*.md' -type f -print0
  find plugins -mindepth 5 -path 'plugins/*/skills/*/*' -name '*.md' -type f -print0
  find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md -type f -print0; } 2>/dev/null |
  xargs -0 -r awk 'BEGIN { tick = sprintf("%c", 96); sq = sprintf("%c", 39); soh = sprintf("%c", 1); open = "^(" tick tick tick tick "*|~~~~*)" } FNR == 1 { fence = ""; held = "" } { line = $0; sub(/^([[:space:]]*>)*[[:space:]]*/, "", line); run = match(line, open) ? substr(line, 1, RLENGTH) : "" } fence == "" && run != "" { fence = run; lang = substr(line, length(run) + 1); sub(/^[[:space:]]*/, "", lang); shell = lang ~ /^(bash|sh|zsh|shell)([^[:alnum:]_-]|$)/; next } fence != "" && run != "" && substr(run, 1, 1) == substr(fence, 1, 1) && length(run) >= length(fence) && substr(line, length(run) + 1) ~ /^[[:space:]]*$/ { fence = ""; held = ""; next } fence != "" && shell { line = held line; held = ""; if (line ~ /(^|[^\\])(\\\\)*\\$/) { held = substr(line, 1, length(line) - 1); next } q = ""; out = ""; inert_start = 0; for (i = 1; i <= length(line); i++) { c = substr(line, i, 1); if (q == "" && c == "#" && (i == 1 || substr(line, i - 1, 1) ~ /[[:space:]]/)) break; if (c == "\\" && q != sq) { out = out c substr(line, i + 1, 1); i++; continue } if (q == "" && (c == "\"" || c == sq)) { q = c; prefix = substr(line, 1, i - 1); inert_start = c == sq && prefix ~ /(^|[;&|({!])[[:space:]]*grep[[:space:]]+(-[[:alpha:]]+[[:space:]]+)*$/ ? length(out) + 1 : 0 } else if (q != "" && c == q) { if (inert_start && (i == length(line) || substr(line, i + 1, 1) ~ /[[:space:];&|<>]/)) out = substr(out, 1, inert_start - 1) "_literal_grep_data_"; inert_start = 0; q = ""; } else if (q != "" && (c == " " || c == "\t")) c = soh; out = out c } gsub(/[\\"]/, "", out); gsub(sq, "", out); gsub(/([0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})?(&>>?|[<>]+&?)([ \t]*[^ \t;&|(<>][^ \t;&|<>]*)?/, " ", out); print FILENAME ":" FNR ":" out }' |
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

**Sub-check 23d** — shell variable used for state across separate Bash tool calls. Apply `severity-table.md` §Canonical defect classes (cross-call shell state: medium). **Not grep-detectable** (requires cross-block analysis of Bash call boundaries, which are runtime not lexical). Flag during curator per-file review only: when auditing a skill, scan for `VAR=$(...)` pattern in one fenced block and `"$VAR"` or `[ -z "$VAR" ]` in a later fenced block with no `cat "${TMPDIR:-/tmp}/...-${CSID}"` supplying `VAR` between them.

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
# Without pipefail, a pipeline reports its final command's status, not the upstream status.
find "$_ROOT" \( -name worktrees -o -name node_modules -o -name .venv -o -path '*/tests/*' \) -prune -o -path "*skills/*" -name "*.md" -type f -exec awk '
  BEGIN { tick = sprintf("%c", 96); fence = "^" tick tick tick tick "*" }
  FNR == 1 { shell = 0; fenced = 0; guarded = 0; pending = ""; held = "" }
  $0 ~ fence {
    match($0, fence); size = RLENGTH
    if (!fenced) {
      fenced = size; shell = $0 ~ (fence "(bash|sh|zsh|shell)([[:space:]]|$)")
      guarded = 0; pending = ""; held = ""; next
    }
    if (size >= fenced && substr($0, size + 1) ~ /^[[:space:]]*$/) {
      fenced = 0; shell = 0; guarded = 0; pending = ""; held = ""; next
    }
  }
  !shell || /^[[:space:]]*(#|$)/ { next }
  /set[[:space:]]+-[[:alpha:]]*o[[:space:]]+pipefail/ { guarded = 1; pending = "" }
  /set[[:space:]]+[+][[:alpha:]]*o[[:space:]]+pipefail/ { guarded = 0; pending = "" }
  {
    line = held $0; held = ""
    if (line ~ /\\$/) { held = substr(line, 1, length(line) - 1); next }
    $0 = line
    consumes = $0 ~ /\$\?|\$\{?[[:alnum:]_]*_RC([[:space:]}";]|$)/
    if (pending != "" && consumes) print pending
    pending = ""
    pipeline = match($0, /\|[[:space:]]*(head|tail)([[:space:]]|$)/)
    if (!guarded && pipeline && $0 !~ /PIPESTATUS|pipefail/) {
      candidate = FILENAME ":" FNR ":" $0
      if (substr($0, pipeline) ~ /\$\?|\$\{?[[:alnum:]_]*_RC([[:space:]}";]|$)|&&|\|\|/ || substr($0, 1, pipeline - 1) ~ /(^|[;[:space:]])(if|while|until)[[:space:]]/) print candidate
      else pending = candidate
    }
  }' {} + 2>/dev/null | awk '{ print; found=1 } END { if (found) print "  hint: review status dependence and intentional truncation before assigning severity" }'
printf "✓: Check 30a scan complete\n"  # timeout: 5000
```

Treat hits as candidates for model review. Confirm that `$?` or an `_RC` value is consumed after the pipeline, or that the pipeline gates `&&`, `||`, `if`, `while` or `until`; verify that the workflow needs the upstream status and that `pipefail` is not already enforced by the execution context. A resolver or display pipeline whose status is unused is not a finding. Severity: **critical** only when a verified gate can pass despite an upstream failure; otherwise report the supported severity or dismiss the candidate.

Fix pattern for a gate without intentional truncation: `set -o pipefail; cmd 2>&1 | tail -N; EXIT=$?`. Review `head` truncation separately because it can cause upstream SIGPIPE. Never `${PIPESTATUS[0]}`, absent under zsh (Claude Code's Bash tool login shell on macOS).

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

Severity: **high** — heredoc triggers permission prompt; user deny = workflow block; violates the inline-Python authoring policy.

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
| 30a — pipe exit code | `\| tail` / `\| head` followed by status consumption without pipefail | critical only for a verified gate after model review | no |
| 30b — SKIP guard missing | `SKIP_X=1` with no `[ "${SKIP_X:-0}" ]` guard | critical | no |
| 30c — filename mismatch | spawn filename ≠ consolidator filename (model reasoning) | high | no |
| 30d — TEST_CMD+pytest flags | `$TEST_CMD --tb` / `--co` / `::` / `--cov` without PYTEST_CMD | high | no |
| 30e — heredoc python | `python <<` in skill body | high | no |
| 30f — missing exit | confirmed failure with no `exit 1` within 3 lines | high | no |
