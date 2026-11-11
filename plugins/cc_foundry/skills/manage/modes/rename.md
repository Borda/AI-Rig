<!-- file: rename.md — consumers: SKILL.md (Step 4 dispatch) -->

> **Verification gate — every write-before-delete sequence below** (Update Agent, Update Skill, Update Rule): the `rm` is authorized **only** by a successful complete verify read and source comparison. Compare source and destination line counts; inspect the full content diff, allowing only the intended rename substitutions. Verification fails — file missing, content truncated, frontmatter absent, unexpected differences, or the Read itself errors — **STOP**: do not run the `rm`, leave the old file in place, print `! Rename aborted — <new-path> failed verification; <old-path> left untouched`, and hand back to the user. Deleting the source after an unverified write is unrecoverable; an orphaned new file is not.

### Rename comparison (agents/skills)

Substitute the old and new agent file paths, or the old and new skill directory paths, into this shared comparison block. For skills, run it before editing the copy and again after the complete verify read; for agents, run it after that read. Require identical relative inventories and symlink targets for skills, including dangling links. A diff exit of 1 displays changes for inspection, not permission to delete: allow only the intended rename substitutions and require equal line counts before the verification gate can pass.

```bash
# timeout: 5000
RENAME_SOURCE="<source-path>"
RENAME_TARGET="<destination-path>"
case "$RENAME_SOURCE:$RENAME_TARGET" in *'<'*'>'*) echo "! BLOCKED — rename paths not substituted"; exit 1;; esac
if [ -d "$RENAME_SOURCE" ]; then
    [ -d "$RENAME_TARGET" ] || exit 1
    diff -u <(cd "$RENAME_SOURCE" && find . -print | LC_ALL=C sort) <(cd "$RENAME_TARGET" && find . -print | LC_ALL=C sort) || exit 1
    diff -u <(cd "$RENAME_SOURCE" && find . -type l -exec sh -c 'printf "%s -> %s\n" "$1" "$(readlink "$1")"' sh {} \; | LC_ALL=C sort) <(cd "$RENAME_TARGET" && find . -type l -exec sh -c 'printf "%s -> %s\n" "$1" "$(readlink "$1")"' sh {} \; | LC_ALL=C sort) || exit 1
    RENAME_SOURCE="$RENAME_SOURCE/SKILL.md"
    RENAME_TARGET="$RENAME_TARGET/SKILL.md"
fi
[ -f "$RENAME_SOURCE" ] && [ -f "$RENAME_TARGET" ] || exit 1
RENAME_DIFF_STATUS=0
diff -u "$RENAME_SOURCE" "$RENAME_TARGET" || RENAME_DIFF_STATUS=$?
[ "$RENAME_DIFF_STATUS" -le 1 ] || exit 1
[ "$(wc -l < "$RENAME_SOURCE")" -eq "$(wc -l < "$RENAME_TARGET")" ] || exit 1
```

### Mode: Update Agent (rename)

Atomic rename — write new file before deleting old:

1. Read `.claude/agents/<old-name>.md` using the Read tool.

2. Write new file to `.claude/agents/<new-name>.md` using the Write tool (copy content of old file with `name:` line updated to `<new-name>`).

3. Read the complete new file: `Read(file_path=".claude/agents/<new-name>.md")`. Run the shared rename comparison block above with the old and new agent paths; compare its line count with the source and inspect `diff -u`: only the `name:` substitution is permitted — **verification gate applies**: fails → STOP, do not run the `rm` below.

```bash
rm .claude/agents/<old-name>.md # timeout: 5000
```

### Mode: Update Skill (rename)

Atomic rename — create new directory before removing old:

1. Copy the entire old directory, including hidden files, modes, templates, scripts and assets. Refuse an existing destination (including a dangling symlink):

   ```bash
   [ ! -e .claude/skills/<new-name> ] && [ ! -L .claude/skills/<new-name> ] || exit 1
   cp -R .claude/skills/<old-name> .claude/skills/<new-name> || exit 1  # timeout: 5000
   diff -r .claude/skills/<old-name> .claude/skills/<new-name> || exit 1
   ```

2. Run the shared rename comparison block above with the old and new skill directory paths to compare relative file inventories and file counts recursively, including hidden files; require byte equality for every copied file and matching symlink targets before editing. Read the copied SKILL.md and update its `name:` line in place using Edit.

   > After updating `name:` in frontmatter: also scan new SKILL.md body for TRIGGER conditions, NOT-for lines, and example invocations still referencing old skill name — update those inline with Edit tool before proceeding to Step 5.

3. Read the complete new SKILL.md, run the shared rename comparison block again to compare its line count with the source SKILL.md, and inspect the recursive diff again: only the intended SKILL.md rename substitutions may differ; every supporting file must remain identical. **Verification gate applies**: fails → STOP, do not run the `rm -r` below (it removes a whole directory).

   ```bash
   rm -r .claude/skills/<old-name>  # timeout: 5000
   ```

### Mode: Update Rule (rename)

Atomic update — write new file before deleting old:

1. Read `.claude/rules/<old-name>.md` using the Read tool.

2. Rule files have no `name:` frontmatter field — filename IS identifier. Copy to `.claude/rules/<new-name>.md` and verify identical bytes:

   ```bash
   cp .claude/rules/<old-name>.md .claude/rules/<new-name>.md && cmp -s .claude/rules/<old-name>.md .claude/rules/<new-name>.md || exit 1  # timeout: 5000
   ```

3. Read the complete new file: `Read(file_path=".claude/rules/<new-name>.md")`. Compare its line count with the source and require `cmp -s .claude/rules/<old-name>.md .claude/rules/<new-name>.md` to exit 0 — **verification gate applies**: fails → STOP, do not run the `rm` below.

```bash
rm .claude/rules/<old-name>.md  # timeout: 5000
```
