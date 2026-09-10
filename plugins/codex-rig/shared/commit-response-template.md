# Commit Response Template

Use when user asks to commit or for commit summary.

## Required Commit Message Shape

Always use:

```text
<type>(<scope>): <title>

Changes:
- <what changed, including the affected surface and resulting behavior>
- <what changed, including the affected surface and resulting behavior>

Impact:
- <concrete user, developer, runtime, compatibility, or maintenance effect>
- <concrete user, developer, runtime, compatibility, or maintenance effect>

Verification:
- <concise final check that materially validates the committed change, with its result>

Residual limits:
- <remaining risk, warning, deferred work, or "None known">

---

Co-authored-by: Codex <codex@openai.com>
```

Rules:

- Creating a new commit does not authorize rewriting an existing commit. Never run `git commit --amend`, `git rebase`, `git reset`, squash, fixup, or an equivalent history rewrite unless the user explicitly requests that exact history operation. Never infer rewrite permission from a commit, cleanup, or commit-diet request.
- First line: Conventional Commit.
- `<type>` lowercase: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`, or `perf`.
- Prefer specific lowercase `<scope>`: `api`, `cli`, `config`, `tests`, `docs`, `ci`, `deps`, `packaging`, `models`, `data`, or `utils`.
- `<title>` imperative, concise, under 72 characters when practical.
- Body: always include four exact headings `Changes:`, `Impact:`, `Verification:`, and `Residual limits:` in that order.
- `Changes:` must list every meaningful behavioral, interface, workflow, policy, test, documentation, packaging, or operational change included in commit. Name affected surface and describe resulting behavior; filename-only inventory is insufficient.
- `Impact:` must state the concrete user, developer, runtime, compatibility, or maintenance effect for each change or tightly related group of changes. Generic impact claims such as "improves UX" or "makes things better" are insufficient.
- `Verification:` includes only final checks that materially validate the committed surfaces or their acceptance contract, each with a concrete result. Consolidate closely related checks and report a required broad gate once using its final outcome. Do not list exploratory probes, failure-first reproductions, setup or environment diagnostics, repeated reruns, superseded failures, or unrelated repository-wide gates. State an exact not-run reason only for a material change-specific acceptance gate; never imply that an unexecuted check passed.
- `Residual limits:` must list warnings, deferred work, and remaining uncertainty, or contain exactly `- None known` when no material limit remains.
- Extensive means complete and auditable, not padded: omit pure lint/format churn, generated cache, typo-only edits, and verification chronology unless they are whole change; combine tightly related details without hiding distinct effects.
- Keep `---` before trailer. End with exactly:

`Co-authored-by: Codex <codex@openai.com>`

## File-Free Commit Execution

As an application of the general approval contract, show the complete secret-free message in chat, then pass that same text as one message argument to `rtk git commit --cleanup=verbatim -m <message>`. Do not create a temporary or persistent commit-message file, ask for draft-file permissions, or schedule draft-file cleanup. Git's own internal commit files and local hooks remain normal Git behavior. Never shorten required detail to reduce the approval prompt.

When conversational commit authorization is actually missing, follow [User Questions](native-skill-contract.md#user-questions): a binary plain-text question ends `(yes / no)`; an existing commit-mode question retains its full supported choices. Runtime permission controls retain their actual options. This does not add a confirmation after an explicit commit request.

1. Finish authorized staging separately; inspect exact staged scope and record pre-commit `HEAD`. An explicit commit request needs no additional conversational confirmation; retain any runtime-required approval. A summary-only request never authorizes commit.
2. Prefer tool's shell-free argv interface when available. When execution tool accepts shell text, use literal quoting for observed shell: POSIX single quotes with each embedded apostrophe encoded as `'"'"'`; PowerShell single quotes with apostrophes doubled, only when native argument passing preserves embedded quotes and newlines. Never use double-quoted shell interpolation, `eval`, command substitution, or expanding here-document to carry message text. Keep dollar signs, backticks, backslashes, quotes, blank lines, and Unicode literal; use LF line endings. Do not assume POSIX quoting works in PowerShell or that legacy Windows native argument passing is lossless.
3. Unsupported shell/native argument behavior, NUL characters, or command-size or encoding limits stop execution with the exact limitation. Do not silently fall back to a file, change the reviewed text, widen permissions, or introduce an interpreter wrapper to conceal the commit from approval. Use an available verified argv route before requesting approval; otherwise leave the commit pending for user direction.
4. Treat the commit as a one-time state-changing command and omit `prefix_rule`. The short plain-English reason asks to create one local commit from the reviewed staged index; it must not repeat the command, flags, message body, or full approval brief. The full message may appear in the runtime command approval and local process arguments; disclose that tradeoff, never include secrets. Do not promise network-free hooks without evidence; unexpected hook network needs retain their own approval boundary. This workflow removes agent-created draft-file operations but does not promise a fixed number of host approval prompts.
5. After success, read committed UTF-8 message from raw Git output, not RTK summary: `git --no-pager show -s --format=format:%B HEAD` (the `format:` form adds no formatter newline). Compare it with text shown in chat after normalizing only one terminal LF on both sides; do not trim whitespace, collapse blank lines, or use shell capture that strips trailing newlines. Verify new commit and post-commit index/worktree state before claiming success.
6. On denial, failure, or mismatch, do not retry automatically or change the index. Inspect `HEAD` and status read-only before describing what happened: hooks can alter files and a failed invocation may still have side effects. Do not amend, reset, re-stage, or create a repair commit. Report any created hash, mismatch, changed files, and pending action; retain the reviewed message in the conversation, not a recovery draft file.

## Required User-Facing Commit Summary

After creating or describing commits, report each commit separately with its hash and title, then concise bullets grouped by:

- **Behavior**: what changed and user-visible or methodological impact.
- **Affected surfaces**: principal components, workflows, or packages changed; do not dump unexplained filename list.
- **Verification**: concise final change-specific tests, lint, build, package, or manifest checks and their results; omit exploratory, repeated, superseded, and unrelated checks.
- **Residual limits**: skipped gates, warnings, deferred work, or remaining uncertainty; state `none known` when verified and no material limit remains.

For multiple commits, also state why the boundary exists. The user-facing summary may condense the commit body, but it must preserve every material behavior, impact, verification result, and residual limit. Never claim a check passed without concrete execution evidence.
