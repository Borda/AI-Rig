<!-- file: env-provision.md — consumers: /develop:fix, /develop:feature, /develop:refactor -->

# Recover a targeted Python test with a missing declared package

1. For a failed targeted run, inspect its output or full log for `ModuleNotFoundError` or `No module named`. Identify the missing import and its declared distribution in the current project's dependency files. Keep the original run evidence and cite the declaring file and line. This recovery applies only to a declared dependency and an original runner of `pytest` or `python -m pytest`.

2. From the project root, set `MISSING_MODULE` to the observed dotted import and `ENV_RECEIPT` to an unused absolute JSON path in a private session temp directory outside the project. Run this helper once per skill invocation and retain the receipt path in the test evidence and final report.

   ```bash
   PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
   python "$PLUGIN_ROOT/bin/dev_env_provision.py" --repo "$PWD" --out "${ENV_RECEIPT:?}" --probe-import "${MISSING_MODULE:?}"  # timeout: 120000
   ```

3. With a usable `env_python` in a `ready` or `partial` receipt, rerun the same targeted arguments once. Keep the original flags, targets, output capture and verdict nonce checks. Use the session's original pytest sentinel and pass the receipt as data. The runners put the project's `src` (when present) and root first on `PYTHONPATH` for the retry, since the provisioner never installs the project.

   - For a selector run, rerun the same `dev_test_targets.py` command with `--env "$ENV_RECEIPT"` added. Keep `--run` and any original `--record-dir` so the retry writes the selector record. The selector passes its ordered targets as literal argv with `--tb=short` and `-v`.
   - For a standalone short-runner call, rerun the same `run_pytest_short.py` command with `--env "$ENV_RECEIPT"` added, keeping its original ordered target list.
   - For a direct retry through `dev_run_tests.py pytest --env "$ENV_RECEIPT"`, add `--env` before the existing `--` boundary. Set the original arguments individually with `set --` in the same Bash call before running the retry, and require at least one original test target.

   ```bash
   PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   [ "$#" -gt 0 ] || exit 64
   python "$PLUGIN_ROOT/bin/dev_run_tests.py" pytest --env "${ENV_RECEIPT:?}" -- "$@"
   ```

4. Record the retry exit and applicable verdict, any remaining missing import or closed cause, and the receipt path. Keep existing evidence gates, including any required selector record. Reuse the receipt for subsequent eligible targeted runs. An unavailable environment or a refused command leaves the affected check unresolved with its original evidence.
