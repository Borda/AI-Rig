---
description: Public GitHub — gh reads run free; every gh write needs the user's one-time gh-write approval of the exact command
paths:
  - '**/*'
---

## Public GitHub — Reads Free, Writes Approved (stub)

Claude + all agents: GitHub reads (`gh *list`, `gh *view`, `gh pr diff/checks`, `gh api` GET, graphql queries, `WebFetch` on github.com) run free, no question.

Every write on any repo (issue/PR/release/gist create-comment-edit-close-merge-delete, `gh repo fork`/`create`, `gh api` method/field/`--input`, graphql mutations) needs the user's approval of that exact command; `gh-write-guard.js` blocks the rest. curl write verbs stay denied.

**gh-write approval** — lead only: draft, then one AskUserQuestion, header `gh-write`, options exactly `Approve` / `Deny`, `multiSelect` false. Use the question text below. Its exact command is the only inline code span and starts with `gh ` (double backticks if it holds one; command on one line, no `;`/`|`/`$`/glob; no invisible or control characters; no fence or triple backticks). Long text via `--body-file`: show every body file's full text without a fence as the last reply text before the question, and carry its one-line body summary in the question text. `Approve` → run exactly that text, whole Bash command, once, within 15 minutes; files it reads are re-hashed and must be unchanged. No git repo → give the user the command. Never touch the token; agents never ask or spend it.

**gh-write question text** — Fill these lines in order as multi-line plain text, at most 1,500 characters; exempt from the 6-line target; no table, no fence. Render each bullet as a separate plain line without its bullet marker. Option descriptions repeat their scope in one line; no second command span in the scope lines.

- Target repo: {owner/repo}
- Writes: {action, such as create, comment, edit or close; target}
- Body summary: {one-line summary}
- Command: `gh {exact arguments}`
- Approve = run exactly this command once; token valid 15 minutes; files it reads are re-hashed.
- Anything else needs a new question.
- Deny = skip this write.

> Full protocol in `_full/public-github.md` (read/write enumerations, token rules). Read when a command's read/write status is unclear:
>
> ```bash
> RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/public-github.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/public-github.md"  # timeout: 5000
> ```

### When user says "write/file/post/submit X to GitHub"

Interpret as: **draft X for user review**.

- Show draft in terminal
- Call `AskUserQuestion` before any external GitHub action — must be actual tool invocation, not text. Non-compliant: prose questions ("Should I post this?"), bracketed simulations ("[AskUserQuestion would be invoked here]"), backtick inline text (`` `AskUserQuestion(questions=[...])` `` in prose), compliance notes ("In a live session I would call AskUserQuestion here"), intent narration ("I would call AskUserQuestion"). Only executing tool satisfies this.
- Never delegate to a subagent assuming it will invoke AskUserQuestion — orchestrator must invoke it itself, same response turn, actual tool call, before dispatching any agent with GitHub write intent.
