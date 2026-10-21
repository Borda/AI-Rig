<!-- file: rename.md — consumers: SKILL.md (Step 4 dispatch) -->

> **Verification gate — every write-before-delete sequence below** (Update Agent, Update Skill, Update Rule): the `rm` is authorized **only** by a successful verify read. Verification fails — file missing, content truncated, frontmatter absent, or the Read itself errors — **STOP**: do not run the `rm`, leave the old file in place, print `! Rename aborted — <new-path> failed verification; <old-path> left untouched`, and hand back to the user. Deleting the source after an unverified write is unrecoverable; an orphaned new file is not.

### Mode: Update Agent (rename)

Atomic rename — write new file before deleting old:

1. Read `.claude/agents/<old-name>.md` using the Read tool.

2. Write new file to `.claude/agents/<new-name>.md` using the Write tool (copy content of old file with `name:` line updated to `<new-name>`).

3. Verify new file exists and is valid: `Read(file_path=".claude/agents/<new-name>.md", limit=5)` — **verification gate applies**: fails → STOP, do not run the `rm` below.

```bash
rm .claude/agents/<old-name>.md # timeout: 5000
```

### Mode: Update Skill (rename)

Atomic rename — create new directory before removing old:

1. Create new directory:

   ```bash
   mkdir -p .claude/skills/<new-name>  # timeout: 5000
   ```

2. Read old SKILL.md, update `name:` line in frontmatter, Write to new location.

   > After updating `name:` in frontmatter: also scan new SKILL.md body for TRIGGER conditions, NOT-for lines, and example invocations still referencing old skill name — update those inline with Edit tool before proceeding to Step 5.

3. Verify new file exists: `Read(file_path=".claude/skills/<new-name>/SKILL.md", limit=5)` — **verification gate applies**: fails → STOP, do not run the `rm -r` below (it removes a whole directory).

   ```bash
   rm -r .claude/skills/<old-name>  # timeout: 5000
   ```

### Mode: Update Rule (rename)

Atomic update — write new file before deleting old:

1. Read `.claude/rules/<old-name>.md` using the Read tool.
2. Rule files have no `name:` frontmatter field — filename IS identifier. Write new file at `.claude/rules/<new-name>.md` with identical content.
3. Verify new file exists: `Read(file_path=".claude/rules/<new-name>.md", limit=5)` — **verification gate applies**: fails → STOP, do not run the `rm` below.

```bash
rm .claude/rules/<old-name>.md  # timeout: 5000
```
