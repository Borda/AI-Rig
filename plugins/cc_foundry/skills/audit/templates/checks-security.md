<!-- file: checks-security.md — consumers: audit/SKILL.md Step 4 -->

<!-- Quick-reference: Check 35 ($ARGUMENTS injection), Check 36 (eval-unsafe output), Check 37 (hardcoded secrets). -->

<!-- security findings appear in a dedicated Security Findings section of the audit report, before functional findings. -->

## Check 35 — $ARGUMENTS shell injection risk security

Bash blocks in any SKILL.md that interpolate `$ARGUMENTS`, `$SCAN_ARGS`, or `$SCAN_QUERY` (or any unvalidated env var representing user-supplied input) directly into shell string without sanitization.

`$ARGUMENTS` is not an environment variable in the Bash tool environment — `env` contains no such entry. The argument text reaches the command as a literal written into the source before that source runs. **No shell-level construct sanitizes it**, because every one of them runs after the text has already been parsed as program source. What the patterns below differ on is how much of the blob the shell must interpret before something safer takes over, and how the construct fails when interpretation goes wrong.

**Least exposed** (satisfies Check 35):

- Passing the whole blob as one positional arg to a Python bin/ script (`python ... "$ARGUMENTS"`), which parses it in Python. The shell still has to get the quoting right for the argument to arrive as a single argv element — a bare `"` at a word boundary splits it into extra slots, and anything after a closing quote is read as shell source. What this buys is that the parsing logic itself is no longer shell: no `BASH_REMATCH`/`match` divergence, no re-splitting on the value's own spaces, and the failure is a Python-side error rather than a silently-empty capture.
- Whether the receiving script then uses `shlex` is the script's own business — do not assume it does. Repo examples that genuinely do: `cc_oss/bin/parse-skill-flags.py`, `cc_oss/bin/parse-resolve-args.py`, `codemap-py/bin/parse_scan_args.py`. `cc_foundry/bin/extract-keep-flag.py` regex-searches the raw blob instead, which is sufficient for its one quoted flag but is not shlex.

**Conditionally acceptable** — each bullet names the one variable the block must assign; read the bullet, not the block, to decide which. `$ARGUMENTS` itself never satisfies this tier — it is a literal written into the source, not a name a block can assign — so a bare `$ARGUMENTS` use reaches step 3. An alias (`$SCAN_ARGS`, `$SCAN_QUERY`) that no block assigns reads as empty, so the guard passes over nothing and the skill silently proceeds with no arguments: a finding, not a pass.

- `shlex.split(os.environ.get("ARGUMENTS", ""))` — Python-side splitting. The block must put `ARGUMENTS` in the child's environment itself (`ARGUMENTS="..." python ...`); the Bash tool environment does not carry it, so without that the script reads the empty string.
- `EXEC_ARGS="${ARGUMENTS#prefix}"`, then every later use of `EXEC_ARGS` either double-quoted (`[ -d "$EXEC_ARGS" ]`) or passed through `shlex.quote`. The named variable is `EXEC_ARGS`; the assignment alone is not handling. One unquoted `$EXEC_ARGS` in interpolation position drops the bullet to step 3.

**Never counts as handling** (does not satisfy Check 35):

- `[[ "$ARGUMENTS" =~ ^safe-pattern$ ]]` guard before use. Text carrying a quote or a newline breaks the block at parse time, so the guard never executes and every step below it in that block is skipped. It also cannot capture portably: `[[ =~ ]]` sets `BASH_REMATCH` under bash but `match` under zsh, so `${BASH_REMATCH[1]}` silently yields the empty string there and the guarded value is lost rather than rejected.
- `[[ "$ARGUMENTS" == *"--flag"* ]]` substring tests. A substring match fires on the flag name appearing anywhere, including inside a quoted value the user passed to a different flag.

**Unsafe patterns** (flag as security):

- `eval "cmd $ARGUMENTS"` or `bash -c "... $ARGUMENTS ..."` (direct shell eval)
- `python -c "... $ARGUMENTS ..."` (inline python with injected argument)
- Unquoted `$ARGUMENTS` in heredoc expansion position

Scan all `*/SKILL.md` and `*/skills/*/SKILL.md` files in scope. For each bash code block containing `$ARGUMENTS` (or an env-var alias like `$SCAN_ARGS`, `$SCAN_QUERY`), decide in this order and stop at the first match:

1. A **Least exposed** pattern appears in the same or a preceding line of that block → pass.
2. A **Conditionally acceptable** pattern appears in full — the variable that bullet names is assigned in this block, **and** the bullet's own condition on its later uses holds → pass. The assignment must be visible in the block; an inherited value does not count, because shell state does not survive between Bash calls.
3. Otherwise → flag. This includes a block whose only handling is a `[[ ]]` test or a substring comparison, a bare `$ARGUMENTS` use with no bin/ script behind it, and a **Conditionally acceptable** pattern whose named variable is never assigned or whose condition fails.

```bash
printf "=== Check 35: \$ARGUMENTS shell injection risk ===\n"
```

**Severity**: two tiers, decided by whether an interpreter receives the argument text as source.

- `security` — an **Unsafe pattern** above: `eval`, `bash -c`, `python -c`, or unquoted expansion in heredoc position. The text runs.
- `high` — a block that reaches step 3 with only a shell construct handling the blob: a `[[ =~ ]]` guard, a `case`, or a substring comparison. Nothing executes the text, so this is not an injection vector; it is a guard that cannot be relied on — `[[ =~ ]]` captures into `match` under zsh, and a quote or newline in the blob breaks the block at parse time, skipping every step below it.

Fix, both tiers: hand the whole blob to a bin/ script as one positional argument and parse it there (`plugins/cc_oss/bin/parse-skill-flags.py` for boolean and value flags, `plugins/codemap-py/bin/parse_scan_args.py` for a shlex-based reference).

## Check 36 — eval-unsafe bin/ output security

Python bin/ scripts producing shell variable assignments for `eval $()` in a calling SKILL.md must quote all dynamic values with `shlex.quote`. Unquoted values allow injection via crafted env var content.

**Safe pattern (required for eval-consumed output)**:

```python
import shlex
print(f"VAR={shlex.quote(value)}")
```

**Unsafe pattern (flag)**:

```python
print(f"VAR={value}")  # unquoted — injection risk if value contains shell metacharacters
```

Scan: for each Python script in `plugins/*/bin/` producing lines of form `VAR=value` (detectable via `print(f"VAR=` or `print("VAR=` in source), verify `shlex.quote` wraps value. Flag scripts with bare `print(f"... = {` patterns in assignment position without `shlex.quote`.

Exempt scripts producing only JSON output (no shell assignment format).

```bash
printf "=== Check 36: eval-unsafe bin/ output ===\n"
grep -rn 'print(f"[A-Z_]*=' plugins/*/bin/*.py 2>/dev/null \
  | grep -v 'shlex.quote' \
  | grep -v '\.json' \
  | while IFS= read -r hit; do
      printf "R36-WARN: potential eval-unsafe output: %s\n" "$hit"
    done  # timeout: 5000
```

**Severity**: `security` — exploitable only when calling SKILL.md passes crafted env var through eval-consumed bin/ script. Fix: wrap dynamic values in `shlex.quote()` before printing assignment strings.

## Check 37 — Hardcoded secrets in config security

Any hardcoded API key, token, password, or bearer credential in plugin `.md` files, `settings.json`, or hook `.js` files.

```bash
printf "=== Check 37: Hardcoded secrets in config ===\n"
grep -rniE '(api[-_]?key|token|secret|password|bearer)\s*[=:]\s*["'"'"'][a-zA-Z0-9+/=_-]{16,}["'"'"']' \
    plugins/ .claude/settings.json .claude/hooks/*.js 2>/dev/null \
    | grep -v '# example\|# placeholder\|YOUR_\|<your\|XXXXXX\|example.com'  # timeout: 5000
```

Any hit not an example/placeholder pattern → `security` finding.

**Severity**: `security` — immediate secret rotation required. Fix: remove secret from config; use env var reference (`$MY_API_KEY`) or system keychain; never commit secrets to plugin files.
