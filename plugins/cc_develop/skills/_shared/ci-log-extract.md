## CI Log Extract Protocol

Fetch + parse GitHub Actions failed-job logs from a run ID or URL. Used by skills accepting `--ci-run <run-id-or-url>` to substitute CI logs for local pytest evidence.

## §URL Normalization

Accept any of:

- Bare run ID: `12345678`
- Actions run URL: `https://github.com/owner/repo/actions/runs/12345678`
- Job-specific URL: `https://github.com/owner/repo/actions/runs/12345678/jobs/98765432`

```bash
if echo "$CI_RUN_ID" | grep -q 'github\.com'; then
  _RAW_CI="$CI_RUN_ID"
  CI_RUN_ID=$(echo "$CI_RUN_ID" | grep -oE '/runs/[0-9]+' | grep -oE '[0-9]+' | head -1)
  [ -z "$CI_RUN_ID" ] && { printf "! --ci-run URL could not be parsed to a run ID: %s\n" "$_RAW_CI"; exit 1; }
fi
```

After normalization `CI_RUN_ID` is a bare integer. Fail fast if URL present but no `/runs/<digits>` segment found.

## §Log Fetching

```bash
CI_LOG_EVIDENCE=$(gh run view "$CI_RUN_ID" --log-failed 2>&1)  # timeout: 15000
GH_EXIT=$?
if [ $GH_EXIT -ne 0 ]; then
  printf "⚠ gh run view exited %s — continuing with partial log\n" "$GH_EXIT"
fi
```

Non-zero exit: print warning, continue. Empty or metadata-only result → §Re-fetch Fallback below.

## §Log Parsing

`gh run view --log-failed` output structure:

- Each failed job opens with header line: `<job-name>  <step-name>  <timestamp>`
- Test output follows, indented or prefixed with job/step name
- GitHub Actions metadata lines — filter as noise:
  - `::set-output`, `##[group]`, `##[endgroup]`, lines starting with `Run ` (step runner echo)

**Signals to extract** (scan full log; surface each distinct failure mode separately):

| Signal | Pattern |
| -- | -- |
| Failing test name | `FAILED tests/path/test_file.py::test_name` |
| Assertion failure | `AssertionError: <message>` |
| Import failure | `ModuleNotFoundError: No module named '...'` or `ImportError: ...` |
| General error | `ERROR`, `error:` (case-sensitive check for `error:` avoids false hits on info lines) |
| Traceback start | `Traceback (most recent call last):` |

**Traceback parsing rule**: extract first frame pointing to **project source** — skip frames inside `site-packages/`, `_pytest/`, `pluggy/`, or stdlib paths. Project source frame = path not containing `site-packages`, matching project root prefix.

**Multiple failing jobs**: process each job block independently. Surface distinct failure modes as separate bullets — never merge unrelated failures into one summary.

**Set evidence variable**:

```bash
CI_LOG_EVIDENCE=$(echo "$CI_LOG_EVIDENCE" | grep -v '::set-output\|##\[group\]\|##\[endgroup\]')
```

## §Re-fetch Fallback

If `CI_LOG_EVIDENCE` empty or contains only metadata lines after §Log Fetching:

```bash
CI_LOG_EVIDENCE=$(gh run view "$CI_RUN_ID" --log 2>&1 \
  | grep -A 40 'FAILED\|ERROR\|Traceback')  # timeout: 15000
```

Full log fetch is larger — grep limits to failure-adjacent context. Still empty after fallback: note "CI log unavailable for run $CI_RUN_ID" in Final Report, fall back to local pytest if possible.

## §Integration Pattern

Skills use extracted evidence as follows:

1. Set `CI_LOG_EVIDENCE` via §Log Fetching + §Log Parsing above.
2. Use `CI_LOG_EVIDENCE` as evidence source in Step 1 instead of running `$PYTEST_CMD` locally — skip local test run.
3. Note in Final Report: `Diagnosed from CI run $CI_RUN_ID — local reproduction not attempted`.

Evidence handoff to agent spawn prompt:

```
CI evidence (run $CI_RUN_ID):
<paste $CI_LOG_EVIDENCE — truncate to 200 lines if longer>
```

Truncate at 200 lines to avoid context flood — leading lines (job headers + first tracebacks) carry most signal.
