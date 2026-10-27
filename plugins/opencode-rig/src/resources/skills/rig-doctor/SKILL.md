---
name: Rig Doctor
description: Check opencode-rig install health and report exact remediation steps.
---

# Rig Doctor

Run `rig_doctor` and interpret the report. Doctor is read-only.

## Checks

- Plugin is loaded and reports the expected version.
- `package.json`, `rig.json`, `src/version.ts`, and the README marker agree.
- Agent files are installed and stamped with the current version.
- Git is available and a local Git grant state can be read.
- The artifact root exists or can be created, and is gitignored.
- Configured gates are detectable.
- Stale run directories older than the configured TTL are reported.

## Response

Report each line as pass, warn, or fail. For every failure, give the exact command or edit that fixes it:

- Missing or stale agents: `opencode-rig init --project` (or `--upgrade`).
- Missing Git grant: answer **Approve always** once in the main checkout, or keep per-commit questions.
- Missing artifact ignore: add `.opencode-rig/` to `.gitignore`.
- No gates: configure a Makefile or package script, or set `options.gates`.

Do not change files while diagnosing. Do not run paid checks.
