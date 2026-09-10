## Personal Model Selection

Normal parent sessions use `gpt-5.6-terra` at `high`. Do not select or spawn `gpt-5.6-sol` automatically for an architecture, public-API, security, migration, or review label.

Use the Sol-pinned `solution-architect` or `security-auditor` only when the user explicitly requests a Sol advisory pass or explicitly selects that agent. Keep the main parent session on Terra. A Sol child is a bounded advisory: return its evidence in the workflow artifact and handover, then continue scope, implementation, verification, and the final response in the Terra parent.

## Local Test Execution

Run authorized local tests with the project's existing environment and test entrypoint, using `sandbox_permissions="use_default"` or omitting the override. Inline environment assignments, output redirection, temporary paths within writable roots, and test-suite membership do not by themselves justify escalation. A subprocess or localhost dependency alone is not proof that the sandbox blocks it.

When tests require an additional capability:

1. Identify the actual restriction from a sandbox failure or verified requirements under the active sandbox. Reuse established evidence for the same capability; do not repeat a known failing run merely to request approval. Distinguish local listeners, subprocess communication, and required filesystem access from intentional internet access; apply the owning network-approval workflow when internet access is needed.
2. Request only the required runtime permission, explaining the capability and relevant evidence. Test authorization does not authorize changing sandbox settings, credentials, or persistent permissions. Preserve the test contract, source selection, environment, and evidence; do not skip assertions or replace real interactions to avoid approval.
3. For recurring runs, prefer an existing stable project test entrypoint that owns its environment and log handling. Inspect what it executes before proposing a reusable `prefix_rule`; include the actual interpreter, wrapper, and script path needed to identify that entrypoint. Keep changing test selectors and report destinations outside the approved prefix. Never blanket-approve a shell, interpreter, general task runner, or gate runner accepting arbitrary commands.
4. Avoid timestamp-specific whole-shell approvals. Shell assignments and redirection can prevent the inner pytest command from matching an existing rule. Where supported, use tool output capture or the existing runner's log arguments; when command-local variables are necessary, a platform-supported explicit environment launcher can keep them as ordinary arguments. Account for that launcher in prefix matching; do not drop required variables or change test behavior. If no suitable entrypoint exists, keep approval specific to the invocation until a bounded project entrypoint is implemented and verified within authorized scope.

Before attributing another prompt to missing pytest approval, inspect the complete owning command and its matching rules with `codex execpolicy check`. Respect denied requests and existing stop conditions; never broaden or disguise a command to bypass approval. These instructions guide command selection and do not themselves grant execution permission.
