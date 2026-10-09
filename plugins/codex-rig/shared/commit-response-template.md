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
- Keep `---` before trailers. When Claude shaped the committed diff (code, review, diagnosis, or work Codex commits on Claude's behalf), put `Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>` on the line before the Codex trailer. End with exactly:

`Co-authored-by: Codex <codex@openai.com>`

## File-Free Commit Execution

As an application of the general approval contract, show the complete secret-free message in chat with the exact reviewed path list, then pass that same text as one message argument in one owning command that stages exactly those paths and commits them: `rtk git add -- <paths> && rtk git commit --cleanup=verbatim -m <message>`. Apply [Authorized Workflow Execution](native-skill-contract.md#authorized-workflow-execution): an authorized local commit uses existing runtime grants and does not require escalation merely because it writes `.git`. Use raw `git` when RTK is unavailable. Do not create a temporary or persistent commit-message file, ask for draft-file permissions, or schedule draft-file cleanup. Git's own internal commit files and local hooks remain normal Git behavior. Never shorten required detail to reduce the approval prompt.

For code-remediate's explicit-request sequencing override only, record the explicit request and remaining report checkpoint in `commit-plan.md`. Complete the owning workflow's required source/quality and ownership checks, then execute and verify the requested local commit without waiting solely for final report validation. Resume the normal handoff validation and promotion afterward with the real hashes and disposition. A grouping preference alone does not invoke this override; unrelated workflows retain their own gates.

When conversational commit authorization is actually missing, follow [User Questions](native-skill-contract.md#user-questions): invoke a permitted native control, using the packaged `ask_user` form when sync is unavailable, prohibited for authorization, or cannot fit every mode; use async only after verifying its suitability and the native form is unavailable. Keep dependent work pending; use plain chat only after establishing that no permitted native control is suitable. Binary canonical options are `Approve` and `Deny`, supplied separately; apply User Questions' recommendation suffix and decision-key labeling before presentation. An existing commit-mode question retains all feasible choices in one supported control. Runtime permission controls retain their actual options. This does not add a confirmation after an explicit commit request.

1. Before the command, inspect `git diff HEAD -- <paths>` for exactly the reviewed paths and `git status --porcelain=v1 -- <paths>` so untracked new files are read too; record pre-commit `HEAD`. Reviewed paths are explicit repo-relative file paths, never `.`, a directory, or a glob. Then check `git diff --cached --name-only`: any staged entry outside the reviewed paths is user state and stops execution before the command; do not unstage, reset, or commit it. An explicit commit request needs no additional conversational confirmation; retain any runtime-required approval. A summary-only request never authorizes commit.
2. The chained command needs shell text. Construct each POSIX argv segment with `shlex.join` or equivalent literal quoting: single quotes with each embedded apostrophe encoded as `'"'"'`, never three adjacent apostrophes. Before executing a constructed multiline shell command, validate its syntax without execution using the actual shell's parser (`bash -n` for Bash, `zsh -n` for Zsh; PowerShell's parser API for PowerShell). A parse check is read-only and does not require new consent. PowerShell single quotes double apostrophes, only when native argument passing preserves embedded quotes and newlines. Never use double-quoted shell interpolation, `eval`, command substitution, or expanding here-document to carry message text. Keep dollar signs, backticks, backslashes, quotes, blank lines, and Unicode literal; use LF line endings. Do not assume POSIX quoting works in PowerShell or that legacy Windows native argument passing is lossless. When the execution tool accepts only shell-free argv, or the observed shell has no success-only chaining operator (Windows PowerShell 5.1 has no `&&`), run `rtk git add -- <paths>` and then `rtk git commit --cleanup=verbatim -m <message>` as two argv calls with no other change. The same authorization and effective permissions cover both calls; a tool transport change alone does not require another approval.
3. Unsupported shell/native argument behavior, NUL characters, or command-size or encoding limits stop execution with the exact limitation. Do not silently fall back to a file, change the reviewed text, widen permissions, or introduce an interpreter wrapper to conceal the commit from approval. Use an available verified argv route before requesting approval; otherwise leave the commit pending for user direction.
4. Run with `use_default` or no sandbox override when the required repository and Git metadata paths are writable. Reuse any existing matching command allowance; do not request another approval or suppress an applicable saved allowance. If an actual missing capability requires runtime approval, request only that capability for the complete owning command under the shared approval contract. The short plain-English reason asks to stage the reviewed paths and create one local commit from them; it must not repeat the command, flags, message body, or full approval brief. The full message may appear in the runtime command approval and local process arguments; disclose that tradeoff, never include secrets. Do not promise network-free hooks without evidence; unexpected hook network needs retain their own approval boundary. This workflow does not promise a fixed number of host approval prompts.
5. After success, read committed UTF-8 message from raw Git output, not RTK summary: `git --no-pager show -s --format=format:%B HEAD` (the `format:` form adds no formatter newline). Compare it with text shown in chat after normalizing only one terminal LF on both sides; do not trim whitespace, collapse blank lines, or use shell capture that strips trailing newlines. For ordinary commits, read the committed file set with `git --no-pager show --no-renames --name-only -z --format= HEAD` and require it to equal the reviewed path list exactly as a set; `--no-renames` keeps a rename's old path and `-z` keeps non-ASCII paths unquoted. For merge commits, use the merge proof below instead of that default combined-diff path check. Verify new commit and post-commit index/worktree state before claiming success.
6. Classify a failure before stopping recovery. Inspect `HEAD`, the index, worktree, and merge state against the pre-command observations; a failed invocation may still have side effects. For a proven pre-execution syntax failure with unchanged state, correct the quoting, verify the same reviewed paths and message, validate syntax, and make one corrected attempt under existing authorization. No new commit consent or generic request to resume is needed. If staging succeeded but commit did not start, verify the index contains exactly the reviewed content and no unrelated changes, then run only the corrected commit segment under the same authorization. If a commit already exists, verify it under step 5 and do not create a duplicate commit.
   - On an actual user or runtime denial, uncertain execution, hook failure, concurrent changes, or mismatch, stop the affected mutation and diagnose read-only; a mismatch includes a committed file set that differs from the reviewed paths. Do not amend, reset, unstage, re-stage, or create a repair commit. Ask only for a concrete missing decision after safe diagnosis, preserving the denial and recurrence rules.
   - Report observed state and continue independent authorized checks. When recovery succeeds, resume the active workflow from its first unmet checkpoint. Retain the reviewed message in the conversation, not a recovery draft file.

## Required User-Facing Commit Summary

After creating or describing commits, report each commit separately with its hash and title, then concise bullets grouped by:

- **Behavior**: what changed and user-visible or methodological impact.
- **Affected surfaces**: principal components, workflows, or packages changed; do not dump unexplained filename list.
- **Verification**: concise final change-specific tests, lint, build, package, or manifest checks and their results; omit exploratory, repeated, superseded, and unrelated checks.
- **Residual limits**: skipped gates, warnings, deferred work, or remaining uncertainty; state `none known` when verified and no material limit remains.

For multiple commits, also state why the boundary exists. The user-facing summary may condense the commit body, but it must preserve every material behavior, impact, verification result, and residual limit. Never claim a check passed without concrete execution evidence.

## Merge Commit Proof

Before committing, require a task-owned merge, as defined in [Local merge approval](native-skill-contract.md#local-merge-approval), with no unmerged entries. Review every staged change against the recorded pre-merge HEAD, including clean merged-in target paths; the approved path set is the full first-parent change set, not only resolved conflicts. Unrelated staged entries stop the commit without unstaging or discarding them. Record the ordered intended parents from pre-merge HEAD and `MERGE_HEAD`. After that review of the staged index, and before the owning command, capture the reviewed index tree with `git write-tree`. The reviewed merge paths are already staged, so the owning command for a merge commit is `rtk git commit --cleanup=verbatim -m <message>` alone; never stage again after capturing the tree. Any later index change, such as re-staged worktree drift or a hook mutation, then fails the tree check below.

After committing, require `git rev-list --parents -n 1 HEAD` to match the recorded parents in order, `git rev-parse HEAD^{tree}` to equal the reviewed index tree, and `git diff --no-renames --name-only -z <pre-merge-head> HEAD --` to equal the complete reviewed path set. Preserve existing message/trailer checks and branch receipts. A differing tree, parent or path set fails verification; no automatic amend, reset or history rewrite is authorized.
