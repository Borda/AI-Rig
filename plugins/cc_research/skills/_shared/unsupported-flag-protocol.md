<!-- file: unsupported-flag-protocol.md — consumers: fortify/SKILL.md, judge/SKILL.md, plan/SKILL.md, retro/SKILL.md, run/SKILL.md, sweep/SKILL.md, topic/SKILL.md, verify/SKILL.md -->

## Unsupported Flag Protocol

After supported flags extracted from `$ARGUMENTS`, scan remaining tokens for any `--<token>`.

Found → invoke `AskUserQuestion` with this question text (not printed as reply text before the call, which can arrive as an empty progress update):

```text
! Unknown flag(s): `--<token>`. Supported: <SKILL_SUPPORTED_FLAGS>.
```

Options:

- (a) **Abort** — stop, re-invoke with correct flags
- (b) **Continue ignoring** — skip unknown flags, proceed

On Abort: stop.

> `<SKILL_SUPPORTED_FLAGS>` = consumer skill's supported-flag list (e.g. `` `--venue`, `--max-ablations`, `--skip-run` ``).
