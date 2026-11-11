"""Exercise bounded command inspection and permission fallback through the real Node hooks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[3] / "hooks"


def _guard(hook: str, command: str, root: Path = HOOKS) -> subprocess.CompletedProcess[str]:
    """Inspect command text without executing it, with a deadline shorter than the host hook timeout."""
    return subprocess.run(
        ["node", str(root / hook)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize(
    ("hook", "program", "tail"),
    [
        pytest.param("commit-guard.js", "git", "push -f origin main", id="git"),
        pytest.param("gh-write-guard.js", "gh", "pr create", id="gh"),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "cat <<''; {program} {tail}",
        "cat <<END\n{program} {tail}\n",
        "P={program}; $P {tail}",
        "$(echo {program}) {tail}",
        pytest.param(":;" * 16000 + "{program} {tail}", id="oversize"),
    ],
)
def test_unreadable_named_command_requests_permission(hook: str, program: str, tail: str, template: str) -> None:
    """Unreadable, dynamic and resource-limited commands ask instead of allowing or denying execution."""
    result = _guard(hook, template.replace("{program}", program).replace("{tail}", tail))
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "ask"
    assert decision["permissionDecisionReason"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("hook", "command"),
    [
        pytest.param("commit-guard.js", r"echo {1..300}; $'\147it' push -f", id="git-ansi"),
        pytest.param("commit-guard.js", "git commit -m 'unbalanced; echo git push -f", id="git-unbalanced-message"),
        pytest.param("commit-guard.js", 'git push origin "?main', id="git-unbalanced-glob"),
        pytest.param("commit-guard.js", "git push origin ?main {1..300}", id="git-overflow-glob"),
        pytest.param("commit-guard.js", "git --config-env=alias.q=FOO q", id="git-runtime-alias"),
        pytest.param("gh-write-guard.js", r"echo {1..300}; $'\x67h' pr create", id="gh-ansi-program"),
        pytest.param("gh-write-guard.js", 'echo {1..300}; g"h" pr view', id="unreadable-read"),
    ],
)
def test_quoted_program_names_keep_permission_fallback(hook: str, command: str) -> None:
    """The fallback recognizes quote concatenation and ANSI-C program names without guessing argv."""
    result = _guard(hook, command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    ("hook", "program", "tail", "reason"),
    [
        pytest.param("commit-guard.js", "git", "push -f origin main", "force-push is forbidden", id="git"),
        pytest.param("gh-write-guard.js", "gh", "pr create", "gh write blocked", id="gh"),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "echo {1..300}; {program} {tail}",
        "echo 'unterminated; {program} {tail}",
        'echo "unterminated; {program} {tail}',
        "echo $(unterminated; {program} {tail}",
        pytest.param("echo " + "$(" * 2100 + ":" + ")" * 2100 + "; {program} {tail}", id="deep"),
        pytest.param(":;" * 300 + "{program} {tail}", id="many-commands"),
        pytest.param("echo " + "x " * 3000 + "; {program} {tail}", id="many-words"),
    ],
)
def test_plain_updates_remain_blocked_beside_unreadable_text(
    hook: str, program: str, tail: str, reason: str, template: str
) -> None:
    """The coarse reading preserves force bans and write token gates independently of surrounding syntax."""
    result = _guard(hook, template.replace("{program}", program).replace("{tail}", tail))
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("hook", "command", "reason"),
    [
        pytest.param("commit-guard.js", 'echo {1..300}; g"i"t push -f', "force-push is forbidden", id="git-split"),
        pytest.param("gh-write-guard.js", "echo {1..300}; gh pr $'create'", "gh write blocked", id="gh-ansi-verb"),
    ],
)
def test_coarse_quoted_updates_keep_the_write_ban(hook: str, command: str, reason: str) -> None:
    """Quote stripping cannot downgrade a readable write beside unreadable syntax to an ordinary prompt."""
    result = _guard(hook, command)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        "echo ${!x}; git push --force origin main",
        "echo ${!x}; command git push --force origin main",
        r"echo $'\c@'; git push --force origin main",
        r"git push origin main $'\c@' --force",
        "bash <<< 'git push --force'; echo ${!x}",
        "echo ${!x}; bash <<< 'git push --force'",
        "echo ${!x}; bash -c 'git push --force'",
        "echo ${!x}; bash -c -e 'git push --force'",
        "echo ${!x}; bash --command 'git push --force'",
        "bash -c 'echo ${!x}; git push --force'",
        pytest.param("".join(f"git add f{n};" for n in range(260)) + "git push --force origin main", id="call-budget"),
        pytest.param(
            "".join(f"git -c alias.p=status p x{n};" for n in range(130)) + "git push --force",
            id="alias-budget",
        ),
        pytest.param('git push "--force" origin main; echo ' + "a" * 17000, id="quoted-force-size"),
        pytest.param('git push origin "+main"; echo ' + "a" * 17000, id="quoted-refspec-size"),
        pytest.param("find -exec " * 1400 + "; git push --force", id="deep-wrapper-prefix"),
    ],
)
def test_plain_force_scan_precedes_unreadable_syntax_and_limits(command: str) -> None:
    """A readable force push blocks independently of lexer ordering and inspection budgets."""
    result = _guard("commit-guard.js", command)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "force-push is forbidden" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        pytest.param('gh repo sync "--force"; echo ' + "a" * 17000, "gh force update blocked", id="size"),
        pytest.param("echo ${!x}; gh repo sync --force", "gh force update blocked", id="unreadable-prefix"),
        pytest.param("echo ${!x}; command gh repo sync --force", "gh force update blocked", id="wrapped-force"),
        pytest.param(
            "find -exec " * 1400 + "; gh repo sync --force", "gh force update blocked", id="deep-wrapper-prefix"
        ),
        pytest.param("echo ${!x}; gh issue create -t x -b y", "gh write blocked", id="write-token"),
        pytest.param("bash <<< 'gh repo sync --force'; echo ${!x}", "gh force update blocked", id="stdin"),
        pytest.param("echo ${!x}; bash <<< 'gh repo sync --force'", "gh force update blocked", id="stdin-tail"),
        pytest.param("echo ${!x}; bash -c 'gh repo sync --force'", "gh force update blocked", id="source-tail"),
        pytest.param("echo ${!x}; bash -c -e 'gh repo sync --force'", "gh force update blocked", id="source-options"),
        pytest.param("bash -c 'echo ${!x}; gh repo sync --force'", "gh force update blocked", id="shell-source"),
    ],
)
def test_coarse_gh_write_scan_keeps_force_and_token_gates(command: str, reason: str) -> None:
    """Unreadable syntax cannot downgrade a plainly named gh write to ordinary permission."""
    result = _guard("gh-write-guard.js", command)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
def test_oversize_heredoc_data_is_not_a_force_update(hook: str) -> None:
    """An oversized data heredoc never supplies executable force commands to the raw scan."""
    command = (
        "cat > notes.md <<'EOF'\ngit push --force is forbidden here\ngh repo sync --force\nEOF\necho " + "a" * 17000
    )
    result = _guard(hook, command)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert result.stderr == ""


@pytest.mark.parametrize(
    "command",
    [
        "bash <<< \"git config alias.p 'push --force'\"; git p",
        "bash <<'EOF'\ngit config alias.p 'push --force'\nEOF\ngit p",
        'bash -c "git config alias.p \'push --force\'" sh "$HOME"; git p',
        'sh -c "git config alias.p \'push --force\'" sh "$1"; git p',
        "printf '%s\\n' \"git config alias.p 'push --force'\" > s.sh; bash s.sh; git p",
        "git p; git config alias.p 'push --force'",
        "git p; git -c alias.p='push --force' status",
    ],
)
def test_alias_assignments_resolve_across_all_source_contexts(command: str) -> None:
    """Alias values from every discovered source apply before any invocation is classified."""
    result = _guard("commit-guard.js", command)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "force-push is forbidden" in result.stderr


@pytest.mark.parametrize(
    "form",
    [
        "--add alias.p 'push --force'",
        "--replace-all alias.p 'push --force' old",
        "set --append alias.p 'push --force'",
        "set --all alias.p 'push --force'",
        "set --value old alias.p 'push --force'",
        "alias.p 'push --force' old",
        "--global --type=path alias.p 'push --force'",
        "--local --bool alias.p 'push --force'",
        "--int alias.p 'push --force'",
        "--path alias.p 'push --force'",
    ],
)
def test_config_set_actions_record_alias_values(form: str) -> None:
    """Every supported config assignment action contributes its alias value to force detection."""
    result = _guard("commit-guard.js", f"git config {form}; git p")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "force-push is forbidden" in result.stderr


@pytest.mark.parametrize(
    "form",
    [
        "--get alias.p",
        "alias.p",
        "--get-all alias.p",
        "--get-regexp alias.p",
        "--get-urlmatch alias.p https://example.test",
        "--list",
        "-l",
        "get alias.p",
        "list",
        "--unset alias.p",
        "--unset-all alias.p",
        "unset --all alias.p",
    ],
)
def test_config_read_and_unset_actions_have_no_guard_verdict(form: str) -> None:
    """Reading or removing config does not invent an unreadable alias assignment."""
    result = _guard("commit-guard.js", f"git config {form}")
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    ("form", "decision"),
    [
        pytest.param("--file elsewhere --get alias.p", "ask", id="external-alias-read"),
        pytest.param("-f elsewhere alias.p 'push --force'", "ask", id="external-alias-write"),
        pytest.param("--blob HEAD:config --get alias.p", "ask", id="blob-alias"),
        pytest.param("--edit", None, id="editor-without-alias"),
        pytest.param("-e", None, id="short-editor-without-alias"),
        pytest.param("--unknown alias.p value", "ask", id="unknown-alias-option"),
    ],
)
def test_unreadable_config_sources_request_permission(form: str, decision: str | None) -> None:
    """Unsupported config sources ask only when the arguments name an alias key."""
    result = _guard("commit-guard.js", f"git config {form}; git p")
    assert result.returncode == 0, result.stderr
    verdict = json.loads(result.stdout or "{}").get("hookSpecificOutput", {})
    assert verdict.get("permissionDecision") == decision
    assert result.stderr == ""


def test_unreadable_tail_does_not_invent_a_second_push() -> None:
    """The real push still requires a token without counting the synthetic unreadable reading."""
    result = _guard("commit-guard.js", "git push origin main; echo ${!x}")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "more than one push" not in result.stderr
    assert "git push blocked" in result.stderr


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
@pytest.mark.parametrize(
    "command", ["echo ${HOME}h", "echo g${HOME}", "echo ${HOME}t", "$EDITOR file", '"$PY" -m pytest']
)
def test_argument_fragments_do_not_invent_a_program(hook: str, command: str) -> None:
    """Single-letter fragments in argument values never name a protected executable."""
    result = _guard(hook, command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
@pytest.mark.parametrize(
    "command",
    [
        "git commit -m \"$(cat <<'EOF'\nordinary commit message\nEOF\n)\"",
        "git log --format='%h %s' | head",
        "gh pr view 1 --json title -q .title",
        "[[ -n $(git status --porcelain) ]] && echo dirty",
        'for f in $(git ls-files); do wc -l "$f"; done',
        "$EDITOR file",
        '"$PY" -m pytest',
        "$TOOL push -f",
        "hub push -f",
    ],
)
def test_ordinary_reads_messages_and_unnamed_programs_keep_no_verdict(hook: str, command: str) -> None:
    """Existing read workflows, message data and documented unnamed executables stay outside the write gates."""
    result = _guard(hook, command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("command", ["gh run view -lw", "gh run view -l -w", "gh pr list -dw", "gh pr list -S -w"])
def test_unknown_short_option_values_do_not_hide_watch(command: str) -> None:
    """Every short option containing w closes the automatic read lane."""
    result = _guard("github-read-allow.js", command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
@pytest.mark.parametrize("command", ["echo hello", "echo $HOME", "true"])
def test_unavailable_library_asks_for_every_bash_command(hook: str, command: str, tmp_path: Path) -> None:
    """A non-loadable library conservatively delegates every Bash command to ordinary permission."""
    shutil.copy(HOOKS / hook, tmp_path / hook)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "shell-git.js").mkdir()
    result = _guard(hook, command, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
@pytest.mark.parametrize("command", ["echo ${(U)HOME}", "echo 'unfinished", pytest.param("$(" * 2100, id="deep")])
def test_unreadable_unrelated_command_has_no_guard_verdict(hook: str, command: str) -> None:
    """Unreadable syntax naming neither protected program leaves ordinary permissions in charge."""
    result = _guard(hook, command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("prefix", ["((n=1<<2))", "echo ${n:-1<<2}", "((n=1<<2)); ((m=n>>1))"])
@pytest.mark.parametrize(
    ("hook", "command", "message"),
    [
        pytest.param("commit-guard.js", "command git push -f origin main", "force-push is forbidden", id="git"),
        pytest.param("gh-write-guard.js", "command gh pr create", "gh write blocked", id="gh"),
    ],
)
def test_shifts_do_not_swallow_following_commands(prefix: str, hook: str, command: str, message: str) -> None:
    """Arithmetic and parameter shifts leave a later wrapped command visible to its write guard."""
    result = _guard(hook, prefix + "\n" + command)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert message in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "git config alias.zz push && git zz -f origin main",
        "git config --local alias.zz 'push -f' && git zz origin main",
    ],
)
def test_alias_defined_in_command_cannot_hide_force_push(command: str) -> None:
    """An alias configured earlier in the same command remains subject to the unconditional force ban."""
    result = _guard("commit-guard.js", command)
    assert result.returncode == 2, result.stderr
    assert "force-push is forbidden" in result.stderr


@pytest.mark.parametrize("program", ["deno", "bun", "php", "Rscript", "java", "julia", "swift", "custom-runner"])
@pytest.mark.parametrize(
    ("hook", "code"),
    [
        pytest.param("commit-guard.js", 'system("git push -f")', id="git"),
        pytest.param("gh-write-guard.js", 'system("gh pr create")', id="gh"),
    ],
)
def test_unknown_eval_option_cannot_hide_protected_program(program: str, hook: str, code: str) -> None:
    """Code-bearing options on unlisted executables never silently pass a named git or gh invocation."""
    result = _guard(hook, f"{program} -e '{code}'")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_config_set_requires_write_approval() -> None:
    """Changing gh's configured browser requires the write gate before it can execute commands."""
    result = _guard("gh-write-guard.js", "gh config set browser custom-browser")
    assert result.returncode == 2, result.stderr
    assert "gh config set" in result.stderr


@pytest.mark.parametrize("command", ["gh pr view 1 --web", "gh pr checks 1 --watch", "gh pr view 1 -cw"])
def test_browser_and_watch_options_get_no_read_allow(command: str) -> None:
    """Browser execution and unbounded watches fall through to normal permissions."""
    result = _guard("github-read-allow.js", command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
@pytest.mark.parametrize("failure", ["throw", "missing"])
@pytest.mark.parametrize("command", ["git push -f; gh pr create", r"$'\147it' push -f; $'\x67h' pr create"])
def test_lexer_fault_requests_permission(hook: str, failure: str, command: str, tmp_path: Path) -> None:
    """Exceptions and missing lexer modules ask before a named program could bypass the guard."""
    shutil.copy(HOOKS / hook, tmp_path / hook)
    (tmp_path / "lib").mkdir()
    source = {
        "throw": "exports.gitSubcommandArgs = exports.programInvocations = () => { throw new RangeError('nesting'); };",
        "missing": "throw new Error('missing lexer');",
    }[failure]
    (tmp_path / "lib" / "shell-git.js").write_text(source, encoding="utf-8")
    result = _guard(hook, command, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("prefix", ["echo $((1<<2))", 'echo "$((1<<2))"'])
def test_arithmetic_expansion_keeps_a_later_write_visible(prefix: str) -> None:
    """Arithmetic expansion shifts are data rather than heredocs even inside double quotes."""
    result = _guard("gh-write-guard.js", prefix + "\ncommand gh pr create")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "gh write blocked" in result.stderr


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
def test_many_small_commands_remain_bounded(hook: str) -> None:
    """A realistic 31 KB command list cannot keep the hook busy until the host kills it."""
    result = _guard(hook, "echo x;" * 4400 + "git push -f; gh pr create")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "git config alias.p '!$runner git'; git p",
        "git config alias.p '!echo ${(U)HOME}; git push'; git p",
        "git config alias.p '!git p; git p; git p; git p; git p; git p'; git p",
    ],
)
def test_unreadable_or_recursive_alias_requests_permission(command: str) -> None:
    """Alias expansion shares the inspection budget and propagates unreadability instead of a synthetic force ban."""
    result = _guard("commit-guard.js", command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("command", ["gh pr list -Rowner/repo", "gh pr list -Sreview-requested:someone"])
def test_ambiguous_short_options_containing_w_get_no_read_allow(command: str) -> None:
    """A subcommand-independent read filter cannot distinguish short-option values from watch clusters."""
    result = _guard("github-read-allow.js", command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("command", ["echo .(e_gh pr create_)", "echo .(eXgh pr view 1X)"])
def test_zsh_qualifier_delimiter_cannot_hide_named_program(command: str) -> None:
    """A nonpunctuation qualifier delimiter does not join its gh command word in the fallback name scan."""
    result = _guard("gh-write-guard.js", command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_parameter_whitespace_cannot_create_a_phantom_heredoc() -> None:
    """A parameter's whitespace and delimiter-looking line cannot hide an intervening gh write."""
    result = _guard("gh-write-guard.js", "echo ${n:-1 <<2}\ncommand gh pr create\n2}")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "gh write blocked" in result.stderr


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
@pytest.mark.parametrize("program", ["git", "gh"])
def test_many_program_words_cannot_exhaust_hook_runtime(hook: str, program: str) -> None:
    """Repeated program words cannot amplify argv copies or guard classification past the deadline."""
    result = _guard(hook, "echo " + (program + " ") * 7750 + "; git push -f; gh pr create")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    ("hook", "command"),
    [
        pytest.param("commit-guard.js", r"$'\x67it\c@' push --force", id="git-control-escape"),
        pytest.param("gh-write-guard.js", r"$'\x67h\c@' config set prompt disabled", id="gh-control-escape"),
        pytest.param("commit-guard.js", "unset x; g${x-}it push --force", id="git-split-expansion"),
        pytest.param("gh-write-guard.js", "unset x; g${x-}h issue create --title x --body y", id="gh-split-expansion"),
        pytest.param("commit-guard.js", "g${x} push --force", id="git-prefix"),
        pytest.param("commit-guard.js", "${x}it push --force", id="git-suffix"),
        pytest.param("commit-guard.js", "g${x}t push --force", id="git-middle"),
        pytest.param("gh-write-guard.js", "${x}h issue create", id="gh-suffix"),
        pytest.param("commit-guard.js", "g$(printf it) push --force", id="git-substitution-fragment"),
        pytest.param("gh-write-guard.js", "$(printf g)h issue create", id="gh-substitution-fragment"),
        pytest.param("commit-guard.js", "$EDITOR file && git add file", id="runtime-before-git"),
        pytest.param("gh-write-guard.js", "$EDITOR file && gh pr list", id="runtime-before-gh"),
    ],
)
def test_runtime_or_refused_executable_requests_permission(hook: str, command: str) -> None:
    """Executable fragments and undecoded escapes cannot bypass the ordinary permission prompt."""
    result = _guard(hook, command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    ("hook", "command"),
    [
        pytest.param("commit-guard.js", "git push --force origin main; $EDITOR file", id="git-runtime-tail"),
        pytest.param("commit-guard.js", "git push -f origin main; echo 'x", id="git-quote-tail"),
        pytest.param("commit-guard.js", "git push origin +main; $(echo ls)", id="git-substitution-tail"),
        pytest.param(
            "commit-guard.js",
            "git push --force-with-lease origin main; " + "$(" * 40 + ":" + ")" * 40,
            id="git-nesting-tail",
        ),
        pytest.param("commit-guard.js", "git push -f origin main" + "; true" * 300, id="git-command-tail"),
        pytest.param("gh-write-guard.js", "gh repo sync --force; $EDITOR f", id="gh-runtime-tail"),
        pytest.param("commit-guard.js", "git push --force origin main; echo " + "a" * 17000, id="git-size-tail"),
        pytest.param(
            "gh-write-guard.js",
            "gh api -X PATCH repos/o/r/git/refs/heads/main -F force=true; echo " + "a" * 17000,
            id="gh-size-tail",
        ),
        pytest.param("gh-write-guard.js", "gh repo sync --force; echo 'x", id="gh-quote-tail"),
        pytest.param("commit-guard.js", "bash -c 'git push --force'; $EDITOR f", id="git-shell-force"),
        pytest.param("gh-write-guard.js", "bash -c 'gh repo sync --force'; $EDITOR f", id="gh-shell-force"),
        pytest.param(
            "commit-guard.js", "git -c alias.p='!git push --force; echo {1..300}' p", id="git-shell-alias-force"
        ),
    ],
)
def test_readable_force_update_outranks_unreadable_remainder(hook: str, command: str) -> None:
    """Readable force updates remain unconditional hard blocks even when another construct cannot be read."""
    result = _guard(hook, command)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "force" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("hook", "command", "message"),
    [
        pytest.param("commit-guard.js", "git push origin main; $EDITOR f", "git push blocked", id="git"),
        pytest.param("gh-write-guard.js", "gh issue create; $EDITOR f", "gh write blocked", id="gh"),
        pytest.param("commit-guard.js", "git push origin main; echo 'x", "git push blocked", id="git-quote"),
        pytest.param("gh-write-guard.js", "gh issue create; echo 'x", "gh write blocked", id="gh-quote"),
    ],
)
def test_readable_write_keeps_token_gate_before_unreadable_remainder(hook: str, command: str, message: str) -> None:
    """An unreadable tail cannot replace an exactly read write's token gate with ordinary permission."""
    result = _guard(hook, command)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert message in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "middle",
    [
        "git config --get alias.p status",
        "git config --get-all alias.p status",
        "git config --unset alias.p status",
        "git config --list alias.p status",
        "git config -l alias.p status",
        "git config get alias.p status",
        "git config unset alias.p status",
        "git config list alias.p status",
        "git config --file elsewhere alias.p status",
        "git config alias.p status",
        "git p; git config alias.p status",
    ],
)
def test_alias_read_or_reassignment_cannot_erase_force_value(middle: str) -> None:
    """Every assigned alias reading survives later reads, unsupported config forms and assignments."""
    result = _guard("commit-guard.js", "git config alias.p 'push --force'; " + middle + "; git p")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "force-push is forbidden" in result.stderr


@pytest.mark.parametrize(
    "command",
    ["git config --file elsewhere alias.p 'push -f'; git p"],
)
def test_unmodeled_alias_config_requests_permission(command: str) -> None:
    """Alias keys in config forms outside the assignment grammar require permission."""
    result = _guard("commit-guard.js", command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_repeated_alias_use_observes_intervening_assignment() -> None:
    """A second invocation of the same alias is inspected again after its configuration changes."""
    result = _guard("commit-guard.js", "git p; git config alias.p 'push --force'; git p")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "force-push is forbidden" in result.stderr


@pytest.mark.parametrize("count", [65, 70, 128, 256])
def test_git_invocation_budget_counts_calls_once(count: int) -> None:
    """Commands within the named invocation limit never spend their budget on alternative readings."""
    result = _guard("commit-guard.js", "; ".join(f"git add f{n}" for n in range(count)))
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_git_invocation_overflow_names_the_invocation_limit() -> None:
    """Exhausting plain git calls reports an invocation limit rather than alias expansion."""
    result = _guard("commit-guard.js", "; ".join(f"git add f{n}" for n in range(257)))
    assert result.returncode == 0, result.stderr
    assert "invocation limit exceeded" in json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", ["gh pr list -dw", "gh pr list -cdw"])
def test_web_flag_anywhere_in_short_cluster_gets_no_read_allow(command: str) -> None:
    """Every flag in a short cluster participates in the browser exclusion."""
    result = _guard("github-read-allow.js", command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("option", ["--search", "--jq", "--template", "--json", "--limit", "--repo"])
def test_web_spelling_in_option_value_keeps_read_allow(option: str) -> None:
    """A separate option value beginning with a dash cannot be mistaken for a browser flag."""
    result = _guard("github-read-allow.js", f"gh pr list {option} -wip")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize("option", ["-S", "-q", "-L", "-R", "--label", "-l"])
def test_w_value_after_unmodeled_option_gets_no_read_allow(option: str) -> None:
    """Only explicitly modeled long options can consume a word carrying the watch spelling."""
    result = _guard("github-read-allow.js", f"gh pr list {option} -wip")
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
def test_missing_library_requests_permission_for_argument_expansion(hook: str, tmp_path: Path) -> None:
    """A missing parser asks even for a Bash command with no protected program fragments."""
    shutil.copy(HOOKS / hook, tmp_path / hook)
    result = _guard(hook, "echo $HOME", tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert result.stderr == ""


@pytest.mark.parametrize("prefix", ["git commit -m '", "git -C . commit -m '"])
def test_oversize_commit_reason_offers_message_file(prefix: str) -> None:
    """The size refusal gives a practical file-based path for a long commit message."""
    result = _guard("commit-guard.js", prefix + "a" * 17000 + "'")
    assert result.returncode == 0, result.stderr
    assert "-F <file>" in json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("hook", "name"),
    [pytest.param("commit-guard.js", "git", id="git"), pytest.param("gh-write-guard.js", "gh", id="gh")],
)
@pytest.mark.parametrize(
    "spelling",
    [
        "{name}",
        "'/usr/bin/{name}'",
        "g'{rest}'",
        r"$'\x67{rest}\c@'",
        "g${x-}{rest}",
        "g${x}",
        "${x}{rest}",
        "{name}.exe",
        r"g\{rest}",
    ],
)
def test_missing_library_fallback_covers_library_names(hook: str, name: str, spelling: str, tmp_path: Path) -> None:
    """The real fallback guard recognizes every protected spelling in the shared library corpus."""
    command = spelling.replace("{name}", name).replace("{rest}", name[1:]) + " status"
    recognized = subprocess.run(
        [
            "node",
            "-e",
            "const lib = require(process.argv[1]); process.stdout.write(JSON.stringify(lib.namesProgram(process.argv[2], process.argv[3])));",
            str(HOOKS / "lib" / "shell-git.js"),
            command,
            name,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert recognized.returncode == 0, recognized.stderr
    assert json.loads(recognized.stdout) is True
    shutil.copy(HOOKS / hook, tmp_path / hook)
    result = _guard(hook, command, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
