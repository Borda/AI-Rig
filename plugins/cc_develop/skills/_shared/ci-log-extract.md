## CI Log Extract Protocol

Fetch + parse GitHub Actions failed-job logs from a run ID or URL. Used by skills accepting `--ci-run <run-id-or-url>` to substitute CI logs for local pytest evidence.

> Each bash block below reloads its own state from `dev-ci-run-id-${CSID}` — shell variables do not survive between Bash calls. `CI_LOG_EVIDENCE` below means the log text the §Log Fetching block prints (also saved at `${TMPDIR:-/tmp}/dev-ci-log-${CSID}`), never a shell variable.

## §URL Normalization

Accept any of:

- Bare run ID: `12345678`
- Actions run URL: `https://github.com/owner/repo/actions/runs/12345678`
- Job-specific URL: `https://github.com/owner/repo/actions/runs/12345678/jobs/98765432`

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CI_RUN_ID < "${TMPDIR:-/tmp}/dev-ci-run-id-${CSID}" 2>/dev/null || CI_RUN_ID=""
_RAW_CI="$CI_RUN_ID"
case "$CI_RUN_ID" in *github.com*/runs/*) CI_RUN_ID="${CI_RUN_ID#*/runs/}"; CI_RUN_ID="${CI_RUN_ID%%[!0-9]*}";; esac
# digits-only gate: the value reaches gh argv, so an option-shaped value must fail here
case "$CI_RUN_ID" in ''|*[!0-9]*) printf "! --ci-run is neither a run ID nor an Actions run URL with /runs/<digits>: %s\n" "$_RAW_CI"; exit 1;; esac
echo "$CI_RUN_ID" > "${TMPDIR:-/tmp}/dev-ci-run-id-${CSID}"
```

After normalization the sentinel holds a bare integer; every later block reloads it. Fail fast if the value is neither digits nor a URL with a `/runs/<digits>` segment.

## §Log Fetching

One self-contained block: fetch, strip noise, fall back, print. Non-zero `gh` exit warns and continues with the partial log; a log with no failure signal triggers §Re-fetch Fallback inside the same block.

```bash
# timeout: 45000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CI_RUN_ID < "${TMPDIR:-/tmp}/dev-ci-run-id-${CSID}" 2>/dev/null || CI_RUN_ID=""
[ -n "$CI_RUN_ID" ] || { echo "! dev-ci-run-id missing — run §URL Normalization first"; exit 1; }
CI_LOG="${TMPDIR:-/tmp}/dev-ci-log-${CSID}"
CI_RAW="${TMPDIR:-/tmp}/dev-ci-log-raw-${CSID}"
CI_FALLBACK="${TMPDIR:-/tmp}/dev-ci-log-fallback-${CSID}"
gh run view "$CI_RUN_ID" --log-failed > "$CI_RAW" 2>&1 || printf "⚠ gh run view exited %s — continuing with partial log\n" "$?"
grep -v '::set-output\|##\[group\]\|##\[endgroup\]' "$CI_RAW" > "$CI_LOG"
if ! grep -qE 'FAILED|ERROR|error:|Traceback' "$CI_LOG"; then
  gh run view "$CI_RUN_ID" --log 2>&1 | grep -A 40 'FAILED\|ERROR\|Traceback' > "$CI_FALLBACK"
  [ -s "$CI_FALLBACK" ] && cat "$CI_FALLBACK" > "$CI_LOG"
fi
[ -s "$CI_LOG" ] || echo "! CI log unavailable for run $CI_RUN_ID — fall back to local pytest if possible"
wc -l "$CI_LOG"
head -200 "$CI_LOG"
```

## §Log Parsing

`gh run view --log-failed` output structure:

- Each failed job opens with header line: `<job-name>  <step-name>  <timestamp>`
- Test output follows, indented or prefixed with job/step name
- GitHub Actions metadata lines (`::set-output`, `##[group]`, `##[endgroup]`) are already stripped by the §Log Fetching block; lines starting with `Run ` (step runner echo) are noise — ignore them

**Signals to extract** (scan the printed log; surface each distinct failure mode separately):

| Signal | Pattern |
| -- | -- |
| Failing test name | `FAILED tests/path/test_file.py::test_name` |
| Assertion failure | `AssertionError: <message>` |
| Import failure | `ModuleNotFoundError: No module named '...'` or `ImportError: ...` |
| General error | `ERROR`, `error:` (case-sensitive check for `error:` avoids false hits on info lines) |
| Traceback start | `Traceback (most recent call last):` |

**Traceback parsing rule**: extract first frame pointing to **project source** — skip frames inside `site-packages/`, `_pytest/`, `pluggy/`, or stdlib paths. Project source frame = path not containing `site-packages`, matching project root prefix.

**Multiple failing jobs**: process each job block independently. Surface distinct failure modes as separate bullets — never merge unrelated failures into one summary.

The printed log is the evidence. The block prints the full line count and the saved path before the first 200 lines: count above 200 → Read the remainder from that path (offset 200) before concluding, so later failing jobs are never missed.

## §Re-fetch Fallback

Runs inside the §Log Fetching block when the stripped `--log-failed` output has no `FAILED`, `ERROR`, `error:` or `Traceback` line: full-log fetch (`gh run view --log`), grep limited to failure-adjacent context (full log is larger). Still empty after fallback: the block prints `CI log unavailable for run <id>`; note "CI log unavailable for run $CI_RUN_ID" in Final Report, fall back to local pytest if possible.

## §Integration Pattern

Skills use extracted evidence as follows:

1. Run §URL Normalization, then §Log Fetching; the printed log is `CI_LOG_EVIDENCE`.
2. Use `CI_LOG_EVIDENCE` as evidence source in Step 1 instead of running `$PYTEST_CMD` locally — skip local test run when `dev-ci-run-id-${CSID}` is non-empty and the log was printed.
3. Note in Final Report: `Diagnosed from CI run $CI_RUN_ID — local reproduction not attempted`.

Evidence handoff to agent spawn prompt:

```
CI evidence (run $CI_RUN_ID):
<paste the printed log — already capped at 200 lines>
```

Truncate at 200 lines to avoid context flood — leading lines (job headers + first tracebacks) carry most signal.
