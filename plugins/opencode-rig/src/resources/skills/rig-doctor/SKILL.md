---
name: Rig Doctor
description: Check opencode-rig install health and report exact remediation steps.
---

# Rig Doctor

Run `rig_doctor` with `action: "report"` and interpret the report. Diagnosis is read-only; deletion requires the separate confirmed cleanup path below.

## Checks

- Plugin is loaded and reports the expected version.
- `package.json`, `rig.json`, `src/version.ts`, and the README marker agree.
- Agent files are installed and stamped with the current version.
- Git is available and a local Git grant state can be read.
- The artifact root exists or can be created, and is gitignored.
- Configured gates are detectable.
- Stale run directories older than the configured TTL are reported.

Use `action: "assets"` (CLI: `opencode-rig doctor --assets`) to curate the shipped cards: `rig.json` roster agreement, skill frontmatter and headings, relative reference links, duplicated prose, and oversized skill or role cards. Roster drift and dead links fail; duplication and size warn.

## Response

Report each line as pass, warn, or fail. For every failure, give the exact command or edit that fixes it:

- Missing or stale agents: `opencode-rig init --project` (or `--upgrade`).
- Missing Git grant: answer **Approve always** once in the main checkout, or keep per-commit questions.
- Missing artifact ignore: add `.opencode-rig/` to `.gitignore`.
- No gates: configure a Makefile or package script, or set `options.gates`.
- Asset roster drift: update the installed package's `rig.json`, `src/resources/agents.json`, `src/skill-manifest.ts`, and `src/dispatch.ts` `ROLE_IDS` together with the card, keeping skill and role rosters consistent.
- Dead reference link or duplicated prose: fix the link, or move the shared text into a reference file.

Do not change files while diagnosing. Do not run paid checks.

## Confirmed cleanup

1. Run `rig_doctor` with `action: "report"` and show the stale-run count and configured TTL.
2. Obtain the user's explicit confirmation for deleting all stale run directories selected by the tool under that TTL. Without confirmation, stop after the read-only report.
3. Only after confirmation, call `rig_doctor` with `action: "clean"` and `confirm: true`. Report the removed directories or the tool's failure; do not delete runs manually or edit configuration as part of cleanup.
