"""Tests for ``hooks/lib/shell-git.js``, the git-invocation finder shared by ``commit-guard.js`` and ``rule-inject.js``.

The library answers one question for both hooks: which git subcommands does a Bash command run, however the shell
spells it. The guard blocks pushes on the answer and the injector delivers the commit rule on it, so a spelling the
library misses is a force-push bypass in one hook and a lost rule in the other. Contracts tested directly here:

* **Recall** — grouping, prefixes, quoting, substitutions, heredocs fed to a shell and shell source handed to another
  program all surface the invocation; the pre-lexer coarse detector's hits are never lost. The program name ignores
  case and ``.exe``; brace expansion is resolved.
* **Inline aliases** — ``-c``/``--config-env`` aliases expand to what they run; an alias whose value is set at run
  time reads as ``push --force``.
* **Interpreter code** — ``python -c``/``perl -e``/``node -e`` code naming git and push reads as a push.
* **Precision where it matters** — quoted text, commit-message heredocs and look-alike subcommands do not surface one.
* **Redirections** — an unquoted redirection is dropped with its target and fd number, decided where quoting is known:
  a quoted ``>`` stays an argument, ``'a>'&`` still ends a command, a plain git alias keeps ``>`` as an argument.
* **Fail closed** — quoting the lexer cannot follow, an oversized brace expansion, and unknown git global options widen
  the git answer, never narrow it; for any other program the answer is null instead of a guessed argv.

Each case runs the shipped module in ``node``; the hook-level tests cover how each hook acts on the answer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LIBRARY = Path(__file__).resolve().parents[1] / "hooks" / "lib" / "shell-git.js"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to run the library")


def _node(script: str) -> subprocess.CompletedProcess[str]:
    """Run a node script with the library bound to ``lib``."""
    return subprocess.run(
        ["node", "-e", f"const lib = require({json.dumps(str(LIBRARY))});{script}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


def _readings(command: object) -> set[str]:
    """Return every subcommand reading the library finds in ``command``, each joined with single spaces."""
    proc = _node(f"process.stdout.write(JSON.stringify(lib.gitSubcommandArgs({json.dumps(command)})));")
    assert proc.returncode == 0, proc.stderr
    return {" ".join(args) for args in json.loads(proc.stdout)}


def _invocations(command: object, program: str) -> list[list[str]] | None:
    """Return every invocation of ``program`` the library finds in ``command``, as word lists, or None for no argv."""
    proc = _node(
        f"process.stdout.write(JSON.stringify(lib.programInvocations({json.dumps(command)}, {json.dumps(program)})));"
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _unreadable(command: str) -> bool:
    """Return the ``info.unreadable`` flag ``programInvocations`` sets for ``command``."""
    proc = _node(
        f"const info = {{}}; lib.programInvocations({json.dumps(command)}, 'gh', info);"
        " process.stdout.write(JSON.stringify(info.unreadable));"
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _unreadable_reason(command: str, program: str = "gh") -> str:
    """Return the ``info.unreadableReason`` text ``programInvocations`` sets for ``command``."""
    proc = _node(
        f"const info = {{}}; lib.programInvocations({json.dumps(command)}, {json.dumps(program)}, info);"
        " process.stdout.write(JSON.stringify(info.unreadableReason));"
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@_skip_node_unavailable
class TestModule:
    """The module loads cleanly and exposes only its one entry point."""

    def test_passes_node_syntax_check(self) -> None:
        """``node --check`` accepts the file, as the pre-commit hook and both requiring hooks need."""
        proc = subprocess.run(
            ["node", "--check", str(LIBRARY)], capture_output=True, text=True, timeout=15, check=False
        )

        assert proc.returncode == 0, proc.stderr

    def test_exports_only_the_entry_points(self) -> None:
        """A narrow surface keeps the lexer internals free to change without breaking any hook.

        ``gitSubcommandArgs`` serves the git hooks; ``programInvocations`` serves the gh write guard, which reads gh
        invocations from the same lexer. ``XARGS_TAIL`` is the word ``programInvocations`` puts where xargs supplies
        arguments at run time, so the gh guard can tell ``xargs gh api`` from a complete ``gh api`` argv.
        """
        proc = _node("process.stdout.write(JSON.stringify(Object.keys(lib)));")

        assert json.loads(proc.stdout) == ["gitSubcommandArgs", "programInvocations", "XARGS_TAIL"]

    @pytest.mark.parametrize("command", [None, 42, ["git", "push"]])
    def test_non_string_command_yields_nothing(self, command: object) -> None:
        """A malformed ``tool_input.command`` is no invocation rather than a crash inside a hook."""
        assert _readings(command) == set()


@_skip_node_unavailable
class TestRecall:
    """Every spelling that runs git surfaces the subcommand with its arguments."""

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param("git push --force", "push --force", id="plain"),
            pytest.param("cd /x && git push", "push", id="after-operator"),
            pytest.param("{ git push --force; }", "push --force", id="brace-group"),
            pytest.param("( git push --force )", "push --force", id="subshell"),
            pytest.param("if true; then git push --force; fi", "push --force", id="if-then"),
            pytest.param("while true; do git push -f; done", "push -f", id="loop-body"),
            pytest.param("! git push", "push", id="negation"),
            pytest.param("f() { git push -f; }; f", "push -f", id="function-body"),
            pytest.param("time git push --force", "push --force", id="time"),
            pytest.param("command git commit -m x", "commit -m x", id="command"),
            pytest.param("nohup git push", "push", id="nohup"),
            pytest.param("exec git add f", "add f", id="exec"),
            pytest.param("sudo -u root git push -f", "push -f", id="sudo-option-value"),
            pytest.param("sudo -u git git push -f", "push -f", id="sudo-user-named-git"),
            pytest.param("env -u HOME git push --force", "push --force", id="env-unset-value"),
            pytest.param("GIT_TRACE=1 /usr/bin/git push", "push", id="assignment-and-path"),
            # xargs replaces `{}` and supplies no visible words: both read as run-time text
            pytest.param("ls | xargs -I{} git add {}", "add $(xargs) $(xargs)", id="xargs-with-options"),
            pytest.param('git -C "dir with space" push', "push", id="quoted-dir-with-space"),
            pytest.param('git -C "a;b" push -f', "push -f", id="quoted-dir-with-operator"),
            pytest.param('"git" commit -m x', "commit -m x", id="quoted-argv0"),
            pytest.param("git push $'--force'", "push --force", id="ansi-c-quoting"),
            pytest.param("$'\\x67it' push -f", "push -f", id="ansi-c-hex-escape-program"),
            pytest.param("git push $'\\x2df'", "push -f", id="ansi-c-hex-escape-option"),
            pytest.param("echo `git push -f`", "push -f", id="backticks"),
            pytest.param('echo "$(git push -f)"', "push -f", id="substitution-in-double-quotes"),
            pytest.param("diff <(git push -f) x", "push -f", id="process-substitution"),
            pytest.param("cat <<EOF\n$(git push -f)\nEOF", "push -f", id="substitution-in-unquoted-heredoc"),
            pytest.param('bash -c "git push -f"', "push -f", id="bash-c"),
            pytest.param("sh -c 'cd /x && git push -f'", "push -f", id="sh-c-compound"),
            pytest.param("eval 'git push -f'", "push -f", id="eval"),
            pytest.param("env -S 'git push -f'", "push -f", id="env-split-string"),
            pytest.param("bash <<'EOF'\ngit push -f\nEOF", "push -f", id="heredoc-to-shell"),
            pytest.param("cat <<'EOF' | sh\ncd x; git push -f\nEOF", "push -f", id="heredoc-piped-to-shell"),
            pytest.param("git push 2>&1 | tee log", "push", id="redirection-not-background"),
            pytest.param("BASH -c 'git push -f'", "push -f", id="runner-name-case"),
            pytest.param("bash <<< 'git push -f'", "push -f", id="here-string-to-shell"),
            pytest.param('sh <<<"git push origin +main"', "push origin +main", id="here-string-glued"),
            pytest.param("<<< 'git push -f' bash", "push -f", id="here-string-before-runner"),
            pytest.param("cat <<< hi\ntime git push -f\nhi", "push -f", id="here-string-then-next-line"),
            pytest.param("cat <<<hi\nsudo GIT push origin main", "push origin main", id="here-string-glued-next-line"),
        ],
    )
    def test_invocation_is_found(self, command: str, reading: str) -> None:
        """The reading a hook acts on is among those returned, whatever wraps the call."""
        assert reading in _readings(command)

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param("GIT push --force", "push --force", id="upper-case"),
            pytest.param("Git push -f", "push -f", id="mixed-case"),
            pytest.param("/usr/bin/GIT push -f", "push -f", id="upper-case-path"),
            pytest.param("GIT.EXE push -f", "push -f", id="upper-case-exe"),
            pytest.param("git.exe push -f", "push -f", id="exe"),
        ],
    )
    def test_every_spelling_of_the_program_counts(self, command: str, reading: str) -> None:
        """The program name ignores case and ``.exe``: macOS and Windows file systems run ``GIT`` as git.

        Probed live on macOS: ``/usr/bin/GIT --version`` reports git, so a case-sensitive match was a bypass.
        """
        assert reading in _readings(command)

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param("{git,} push -f", "push -f", id="program"),
            pytest.param("git {push,} -f", "push -f", id="subcommand"),
            pytest.param("git pu{s..s}h -f", "push -f", id="letter-sequence"),
            pytest.param("git push {-f,origin} main", "push -f origin main", id="option"),
            pytest.param("git push origin {+,}main", "push origin +main main", id="refspec-prefix"),
            pytest.param("git push -{f..f} origin", "push -f origin", id="option-sequence"),
        ],
    )
    def test_shell_expansion_is_resolved(self, command: str, reading: str) -> None:
        """Unquoted brace expansion is resolved before the words are read, as bash does, wherever the braces sit."""
        assert reading in _readings(command)


@_skip_node_unavailable
class TestInlineAliases:
    """An alias defined with ``-c``/``--config-env`` on the command line changes which command runs."""

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param("git -c alias.p=push p -f", "push -f", id="alias"),
            pytest.param("git -c alias.yolo='push --force' yolo", "push --force", id="alias-with-arguments"),
            pytest.param("git -c alias.P=push p -f", "push -f", id="alias-name-ignores-case"),
            pytest.param("git -c 'alias.y=-c alias.z=push z' y -f", "push -f", id="alias-of-global-options"),
            pytest.param("git -c alias.x='!git push -f' x", "push -f", id="shell-alias"),
            pytest.param("git -c alias.x='!git' x push -f", "push -f", id="shell-alias-arguments"),
        ],
    )
    def test_inline_alias_expands(self, command: str, reading: str) -> None:
        """An alias defined in the command itself is read as what it runs.

        Every definition form here was probed live with ``status`` as the value before being pinned.
        """
        assert reading in _readings(command)

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git --config-env=alias.q=FOO q", id="config-env-joined"),
            pytest.param("git --config-env alias.q=FOO q", id="config-env-separate"),
            pytest.param('git -c alias.q="$CMD" q', id="value-from-variable"),
            pytest.param("git -c alias.a=b -c alias.b=a a", id="alias-loop"),
        ],
    )
    def test_unresolvable_alias_reads_as_force_push(self, command: str) -> None:
        """An alias whose value the environment holds, or a loop, reads as ``push --force`` so a guard fails closed.

        The loop case also proves the expansion terminates: git itself reports ``alias loop detected``.
        """
        assert "push --force" in _readings(command)

    def test_builtin_keeps_its_literal_reading_beside_a_same_named_alias(self) -> None:
        """A builtin wins over a same-named alias, so the literal reading stays beside the expansion.

        Probed live: ``git -c alias.status=log status`` runs status, not log.
        """
        readings = _readings("git -c alias.push=status push -f")

        assert {"push -f", "status -f"} <= readings


@_skip_node_unavailable
class TestInlineAutocorrect:
    """An inline ``help.autocorrect`` that runs git's guess for a mistyped subcommand lets any subcommand run as push.

    git 2.54 probed: ``1``, ``immediate``, ``true``, ``yes``, ``2``, ``-1`` and the bare key run the correction (a
    mistyped status subcommand runs status); ``0``, ``false``, ``off``, ``no``, the empty value, ``show``, ``prompt``
    (no terminal) and ``never`` do not. A misspelling git maps to push is easy to find (``pus``, ``pussh``), so every
    subcommand under an enabling value reads as ``push --force``.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git -c help.autocorrect=immediate pussh --force origin main", id="immediate"),
            pytest.param("git -c help.autocorrect=1 status", id="numeric-delay"),
            pytest.param("git -c help.autocorrect=-1 status", id="negative-immediate"),
            pytest.param("git -c help.autocorrect status", id="bare-key-is-true"),
            pytest.param("time git -c help.autoCorrect=immediate pus -f", id="key-case-ignored"),
            pytest.param("git --config-env help.autocorrect=V pus -f", id="config-env-run-time-value"),
            pytest.param('git -c help.autocorrect="$V" status', id="variable-run-time-value"),
            pytest.param("git -c help.autocorrect=0 -c help.autocorrect=1 status", id="last-value-wins-on"),
        ],
    )
    def test_enabling_value_reads_every_subcommand_as_force_push(self, command: str) -> None:
        """A value that runs the correction, or one only the environment holds, adds a ``push --force`` reading."""
        assert "push --force" in _readings(command)

    @pytest.mark.parametrize(
        "value",
        ["0", "false", "FALSE", "off", "no", "", "never", "show", "prompt"],
    )
    def test_disabling_value_adds_nothing(self, value: str) -> None:
        """A value that never runs a guess leaves only the literal reading."""
        assert _readings(f"git -c help.autocorrect={value} status") == {"status"}

    def test_last_value_wins_off(self) -> None:
        """A later disabling entry overrides an earlier enabling one, as git reads repeated ``-c`` keys."""
        assert _readings("git -c help.autocorrect=1 -c help.autocorrect=0 status") == {"status"}


@_skip_node_unavailable
class TestPathnameExpansion:
    """An unquoted glob in a git argument can expand to a planted file named like a flag or refspec.

    ``touch ./-f; git push origin -?`` runs ``git push origin -f``. The literal reading stays, and a second reading
    reads each unquoted glob argument as ``--force``, so a guard fails closed. Readings remain plain strings.
    """

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param("git push origin -?", "push origin --force", id="question-mark-flag"),
            pytest.param("git push origin ?main", "push origin --force", id="question-mark-refspec"),
            pytest.param("git push origin ma*", "push origin --force", id="star"),
            pytest.param("git push origin [+]main", "push origin --force", id="bracket"),
            pytest.param("bash -c 'git push origin ma*'", "push origin --force", id="nested-shell-source"),
        ],
    )
    def test_glob_argument_adds_a_force_reading(self, command: str, reading: str) -> None:
        """The glob argument is read as ``--force`` in an extra reading, also inside shell source a runner lexes."""
        assert reading in _readings(command)

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push origin '-?'", id="single-quoted"),
            pytest.param('git push origin "?main"', id="double-quoted"),
            pytest.param("git push origin \\?main", id="escaped"),
            pytest.param("git push origin $*", id="positional-parameters"),
            pytest.param("git push origin ${arr[0]}", id="array-subscript"),
            pytest.param("git -c alias.p='push origin ma*' p", id="plain-alias-value"),
        ],
    )
    def test_literal_glob_character_adds_nothing(self, command: str) -> None:
        """Quoted, escaped and parameter characters never expand; git splits a plain alias without globbing."""
        assert "push origin --force" not in _readings(command)

    def test_every_reading_is_a_plain_string(self) -> None:
        """The marks the lexer uses internally never leak: ``git add *.py`` reads as itself and as ``add --force``."""
        assert _readings("git add *.py") == {"add *.py", "add --force"}


@_skip_node_unavailable
class TestInterpreterCode:
    """Code an interpreter runs is not shell syntax; a raw scan for git plus push reads it as a push."""

    @pytest.mark.parametrize(
        ("command", "token"),
        [
            pytest.param("python3 -c \"import os; os.system('git push -f')\"", "-f", id="python-os-system"),
            pytest.param(
                "python3 -c \"import subprocess; subprocess.run(['git','push','--mirror'])\"",
                "--mirror",
                id="python-argument-list",
            ),
            pytest.param("perl -e 'system(\"git push --force\")'", "--force", id="perl"),
            pytest.param("node -e \"require('child_process').execSync('git push -f')\"", "-f", id="node"),
        ],
    )
    def test_code_naming_a_push_reads_as_one_carrying_its_tokens(self, command: str, token: str) -> None:
        """The push reading carries every token of the code, so a force flag anywhere in it reaches the guard."""
        assert any(reading.split(" ")[0] == "push" and token in reading.split(" ") for reading in _readings(command))

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("node -e \"const a = []; a.push(1); require('./lib/shell-git.js')\"", id="array-push-method"),
            pytest.param("python3 -c \"print('pushing')\"", id="no-git"),
            pytest.param("node --check plugins/cc_foundry/hooks/commit-guard.js", id="script-path"),
        ],
    )
    def test_code_without_a_git_push_yields_nothing(self, command: str) -> None:
        """A ``.push(`` method, a file named ``shell-git.js`` or a word containing push is not a git push."""
        assert _readings(command) == set()

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param("git --future-opt value push -f", "push -f", id="unknown-option-takes-value"),
            pytest.param("git --future-flag push -f", "push -f", id="unknown-option-is-a-flag"),
            pytest.param("git --attr-source HEAD push -f", "push -f", id="known-value-option"),
            pytest.param("git --no-pager -c a=b push", "push", id="known-flag-and-value-option"),
        ],
    )
    def test_global_options_never_hide_the_subcommand(self, command: str, reading: str) -> None:
        """An unknown global option is read both as a flag and as value-taking, so neither reading can hide a push."""
        assert reading in _readings(command)


@_skip_node_unavailable
class TestPrecision:
    """Text that names git without running it yields no push or commit reading."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("echo 'git push --force'", id="single-quoted-text"),
            pytest.param('echo "git push --force"', id="double-quoted-text"),
            pytest.param('git log --grep "push --force"', id="quoted-pattern"),
            pytest.param("git push-ish", id="look-alike-subcommand"),
            pytest.param("git status # git push -f", id="comment"),
            pytest.param(
                "git commit -m \"$(cat <<'EOF'\nfix: x\n\nDon't git push --force here.\nEOF\n)\"",
                id="commit-message-heredoc",
            ),
            pytest.param("cat <<< 'git push -f'", id="here-string-to-non-runner"),
        ],
    )
    def test_no_push_reading(self, command: str) -> None:
        """Quoted text, comments and heredoc bodies are data; only a real ``push`` subcommand counts."""
        assert not any(reading.split(" ")[0] == "push" for reading in _readings(command))

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push origin '{-f,x}'", id="single-quoted"),
            pytest.param('git push origin "\\{-f,x\\}"', id="escaped"),
        ],
    )
    def test_quoted_braces_are_not_expanded(self, command: str) -> None:
        """Quoted or escaped braces stay one literal refspec, so no reading gains a ``-f`` argument."""
        assert not any("-f" in reading.split(" ") for reading in _readings(command))

    def test_parameter_expansion_braces_are_not_brace_expansion(self) -> None:
        """``${HOME%%,*}`` holds a comma inside ``${…}``; reading it as brace expansion would invent words."""
        assert _readings("git log ${HOME%%,*}a") == {"log ${HOME%%,*}a"}


@_skip_node_unavailable
class TestHeredocBodies:
    """A quoted heredoc body handed to a data command is data, even on a line that opens with ``git push``.

    The coarse pass splits raw text at newlines, so a body line opening with ``git push`` read as a push and blocked a
    command that pushes nothing: release notes written with ``cat``, a commit message given on stdin. It skips such a
    body only when the lexer read the whole command and every command in it is a data command; any body a shell, an
    interpreter or an unknown program may run keeps its coarse reading.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("cat > notes.md <<'EOF'\nline one\ngit push origin main\nline three\nEOF", id="plain"),
            pytest.param("cat > n.md <<'EOF'\n  git push origin main\nEOF", id="indented"),
            pytest.param("cat > n.md <<'EOF'\n```\ngit push origin main\n```\nEOF", id="markdown-fence"),
            pytest.param("git commit -F - <<'EOF'\ngit push is documented here\nEOF", id="commit-message-on-stdin"),
            pytest.param('cat > n.md <<"EOF"\ngit push origin main\nEOF', id="double-quoted-delimiter"),
            pytest.param("cat > n.md <<\\EOF\ngit push origin main\nEOF", id="escaped-delimiter"),
            pytest.param("cat <<-'EOF' > n.md\n\tgit push -f origin main\n\tEOF", id="tab-stripping-force-text"),
            pytest.param("tee n.md <<'EOF'\ngit push --force\nEOF", id="tee"),
            pytest.param(
                "gh pr create --title x --body-file - <<'EOF'\ngit push origin main\nEOF", id="gh-body-on-stdin"
            ),
            pytest.param(
                "git commit -m \"$(cat <<'EOF'\nfix: x\n\ngit push now works\nEOF\n)\"",
                id="commit-message-substitution",
            ),
            pytest.param("mkdir -p docs && cat > docs/x.md <<'EOF'\ngit push origin main\nEOF", id="after-mkdir"),
        ],
    )
    def test_body_line_opening_with_push_is_data(self, command: str) -> None:
        """Every command here only stores or sends the body, so no reading is a push.

        Each was blocked as a push needing a token (round 9b live lane, F2), while the same text mid-line passed.
        """
        assert not any(reading.split(" ")[0] == "push" for reading in _readings(command))

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param("bash <<'EOF'\ngit push origin main\nEOF", "push origin main", id="bash"),
            pytest.param("sh <<'EOF'\n  git push origin main\nEOF", "push origin main", id="sh"),
            pytest.param("python3 <<'EOF'\ngit push origin main\nEOF", "push origin main", id="interpreter"),
            pytest.param(
                "cat <<'EOF' | awk '{system($0)}'\ngit push origin main\nEOF", "push origin main", id="piped-to-awk"
            ),
            pytest.param(
                "cat > x.sh <<'EOF'\ngit push origin main\nEOF\nchmod +x x.sh && ./x.sh",
                "push origin main",
                id="script-run-after",
            ),
            pytest.param(
                "cat <<'EOF' > >(sh)\ngit push origin main\nEOF", "push origin main", id="process-substitution"
            ),
            pytest.param(
                "git -c alias.x='!sh' x <<'EOF'\ngit push origin main\nEOF", "push origin main", id="git-inline-config"
            ),
            pytest.param(
                "git bisect run awk '{system($0)}' <<'EOF'\ngit push origin main\nEOF",
                "push origin main",
                id="git-subcommand-running-a-program",
            ),
            pytest.param("cat > n.md <<EOF\ngit push origin main\nEOF", "push origin main", id="unquoted-delimiter"),
            pytest.param(
                "cat > n.md <<'EOF'\ngit push origin main\nEOF\necho 'unbalanced", "push origin main", id="unparsed"
            ),
            pytest.param("sudo cat <<'EOF'\ngit push origin main\nEOF", "push origin main", id="prefix-program"),
            # `<<` the lexer reads as a heredoc where the shells read none: bash runs line 2 as a command
            pytest.param("echo $[ 1 << 'X' ]\ngit push origin main\nX", "push origin main", id="arithmetic-bracket"),
            pytest.param("echo ${x:-<<'X'}\ngit push origin main\nX", "push origin main", id="parameter-default"),
            pytest.param("(( cat << '2' ))\ngit push origin main\n2", "push origin main", id="arithmetic-command"),
            pytest.param(
                "cat <<'EOF'\r\nbody\r\nEOF\r\ngit push origin main\r\n", "push origin main", id="crlf-delimiter"
            ),
        ],
    )
    def test_body_a_program_may_run_keeps_its_reading(self, command: str, reading: str) -> None:
        """A body fed to a shell, interpreter or unknown program, an unquoted body or an unparsed command still reads.

        The coarse pass exists so the lexer only adds git detections; it gives up a body only where the whole command is
        known to treat it as data. That includes text where the lexer reads a heredoc the shells do not: inside
        ``$[…]``, ``${…}`` or ``((…))`` ``<<`` is a shift or literal text, and with CRLF line ends the delimiter word
        keeps its CR, so bash and zsh run the line the lexer would take for body text (probed live on both).
        """
        assert reading in _readings(command)

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("cat <<'EOF' <<'E2'\none\nEOF\ngit push origin main\nE2", id="second-heredoc-body"),
            pytest.param("cat <<'EOF'\ngit push origin main", id="unterminated-body"),
        ],
    )
    def test_shells_read_these_lines_as_body(self, command: str) -> None:
        """Both bash and zsh feed these lines to ``cat`` (probed live), so no reading is a push.

        Two heredocs on one line are read in order; a body with no delimiter line runs to the end of the input.
        """
        assert not any(reading.split(" ")[0] == "push" for reading in _readings(command))


@_skip_node_unavailable
class TestRedirections:
    """A redirection is never an argument; only an unquoted ``<``, ``>`` or ``&>`` starts one.

    The lexer decides while it still sees the quoting. A decision on dequoted words read ``-t '>'`` as a redirection and
    glued ``'a>'&gh …`` into one word, hiding the second command.
    """

    @pytest.mark.parametrize(
        ("command", "words"),
        [
            pytest.param("gh api x 2>&1 >out <in >|c &>>d 1>&- <>rw", ["gh", "api", "x"], id="every-operator"),
            pytest.param("gh api x > out.json", ["gh", "api", "x"], id="separate-target"),
            pytest.param("gh 2>/dev/null pr create", ["gh", "pr", "create"], id="before-the-subcommand"),
            pytest.param("gh pr 2<<<x create", ["gh", "pr", "create"], id="fd-here-string"),
            pytest.param("gh pr {fd}>x create", ["gh", "pr", "create"], id="variable-fd"),
            pytest.param(
                "gh api x -q a>b -f c=d", ["gh", "api", "x", "-q", "a", "-f", "c=d"], id="operator-ends-a-word"
            ),
            pytest.param("gh api x -t '>' -f a=b", ["gh", "api", "x", "-t", ">", "-f", "a=b"], id="single-quoted"),
            pytest.param('gh api x -t "<<" -f a=b', ["gh", "api", "x", "-t", "<<", "-f", "a=b"], id="double-quoted"),
            pytest.param("gh api x -q \\> -f a=b", ["gh", "api", "x", "-q", ">", "-f", "a=b"], id="escaped"),
            pytest.param('gh pr view "2">x', ["gh", "pr", "view", "2"], id="quoted-digits-are-no-fd"),
            pytest.param("gh pr view 2&>x", ["gh", "pr", "view", "2"], id="and-redirection-takes-no-fd"),
            pytest.param("true '>'&gh pr create", ["gh", "pr", "create"], id="quoted-operator-before-background"),
            pytest.param("cat > >(gh pr create)", ["gh", "pr", "create"], id="process-substitution-target"),
        ],
    )
    def test_invocation_words(self, command: str, words: list[str]) -> None:
        """Exactly the words bash passes to gh: redirections, their targets and fd numbers gone, quoted text kept."""
        assert _invocations(command, "gh") == [words]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git 2>/dev/null push -f", id="before-the-subcommand"),
            pytest.param("git > x push -f", id="separate-target-before-the-subcommand"),
            pytest.param("{ git 2>/dev/null push -f; }", id="grouped"),
        ],
    )
    def test_redirection_never_reads_as_the_git_subcommand(self, command: str) -> None:
        """Bash runs ``git push -f`` for each; read as the subcommand, the redirection hid the push from both hooks."""
        assert "push -f" in _readings(command)

    def test_plain_alias_keeps_angle_brackets_as_arguments(self) -> None:
        """Git splits a plain alias itself, so ``>`` there is an argument and the ``-f`` after it a push flag."""
        assert "push > -f" in _readings("git -c alias.p='push > -f' p")


@_skip_node_unavailable
class TestFailClosed:
    """Where the lexer cannot follow the command, any git word anywhere counts."""

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param("git commit -m 'unbalanced; echo git push -f", "push -f", id="unbalanced-single-quote"),
            pytest.param('echo "unterminated && git push', "push", id="unbalanced-double-quote"),
            pytest.param("echo $(git push -f", "push -f", id="unterminated-substitution"),
        ],
    )
    def test_unparseable_quoting_still_finds_git(self, command: str, reading: str) -> None:
        """A lexer failure falls back to scanning every raw segment, so it cannot become a bypass."""
        assert reading in _readings(command)

    def test_unquoted_prose_counts_as_an_invocation(self) -> None:
        """``echo run git push -f`` names a push in plain words; the finder prefers this false positive to a miss."""
        assert "push -f" in _readings("echo run git push -f")

    def test_oversized_brace_expansion_fails_closed(self) -> None:
        """Nine two-way braces make 512 words, past the cap; the raw scan still reads a push carrying ``-f``.

        A cap that silently kept the first expansions could drop the one spelling the push.
        """
        readings = _readings("git push " + "{a,b}" * 9 + " -f")

        assert any(reading.startswith("push ") and reading.endswith(" -f") for reading in readings)

    @pytest.mark.parametrize(
        ("command", "reading"),
        [
            pytest.param('git push origin "?main', "push origin --force", id="unbalanced-quote"),
            pytest.param("git push origin ?main {1..300}", "push origin --force 1..300", id="oversized-braces"),
        ],
    )
    def test_glob_on_the_raw_path_reads_as_force(self, command: str, reading: str) -> None:
        """The raw scan knows no quoting, so every glob character there counts as unquoted."""
        assert reading in _readings(command)


@_skip_node_unavailable
class TestProgramInvocations:
    """``programInvocations`` finds any program the way the git readings do: one lexer for every guard."""

    @pytest.mark.parametrize(
        ("command", "words"),
        [
            pytest.param("gh pr create -t x", ["gh", "pr", "create", "-t", "x"], id="plain"),
            pytest.param("cd /x && { gh pr merge 1; }", ["gh", "pr", "merge", "1"], id="grouped-after-operator"),
            pytest.param("bash -c 'gh pr create'", ["gh", "pr", "create"], id="bash-c"),
            pytest.param('eval "gh issue close 3"', ["gh", "issue", "close", "3"], id="eval"),
            pytest.param("sudo -u u GH.EXE repo delete", ["GH.EXE", "repo", "delete"], id="prefix-case-and-exe"),
            pytest.param("bash <<< 'gh release create v1'", ["gh", "release", "create", "v1"], id="here-string"),
            pytest.param(
                "gh api graphql -f query='query { viewer { login } }'",
                ["gh", "api", "graphql", "-f", "query=query { viewer { login } }"],
                id="quoted-argument-stays-one-word",
            ),
            pytest.param(
                "gh api repos/o/r/pulls?state=open", ["gh", "api", "repos/o/r/pulls?state=open"], id="glob-plain"
            ),
        ],
    )
    def test_invocation_is_found_with_quoting_removed(self, command: str, words: list[str]) -> None:
        """Each invocation is its words from the program word on, quoting removed and glob marks never leaked."""
        assert words in _invocations(command, "gh")

    @pytest.mark.parametrize(
        ("command", "word"),
        [
            pytest.param("gh pr $'cr\\x65ate'", "create", id="hex"),
            pytest.param("gh pr $'\\x63\\x72eate'", "create", id="hex-one-and-two-digits"),
            pytest.param("gh pr $'\\143reate'", "create", id="octal"),
            pytest.param("gh pr $'a\\'b'", "a'b", id="escaped-quote"),
            pytest.param("gh pr $'a\\tb'", "a\tb", id="named-escape"),
            pytest.param("gh pr $'a\\\\b'", "a\\b", id="escaped-backslash"),
        ],
    )
    def test_ansi_c_escapes_both_shells_share_decode(self, command: str, word: str) -> None:
        """Named, octal and ``\\xHH`` escapes yield the text bash and zsh both pass to the program.

        Left undecoded, ``gh pr $'cr\\x65ate'`` read as ``x65ate``-shaped text while the shell ran ``gh pr create``.
        """
        assert ["gh", "pr", word] in _invocations(command, "gh")

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr $'\\u0063reate'", id="unicode-escape"),
            pytest.param("gh pr $'\\U00000063reate'", id="long-unicode-escape"),
            pytest.param("gh pr $'\\x{63}reate'", id="hex-brace-escape"),
            pytest.param("gh pr $'\\cA'", id="control-escape"),
            pytest.param("gh pr $'cre\\qate'", id="unknown-escape"),
            pytest.param("gh pr $'create\\x00junk'", id="nul"),
            pytest.param('gh pr $"create"', id="locale-string"),
            pytest.param("cat <<$'EOF'\nhi\nEOF\ngh pr merge 1", id="ansi-c-heredoc-delimiter"),
            pytest.param('cat <<$"EOF"\nhi\nEOF\ngh pr merge 1', id="locale-heredoc-delimiter"),
            pytest.param("x=$$'\\'; gh pr create #'", id="dollars-before-a-quote"),
            pytest.param('x="${y:-\'"\'}"; gh pr create # "\'', id="single-quote-in-double-quoted-expansion"),
            pytest.param('x="$${y:-"}"; gh pr create # "', id="dollars-before-a-brace-in-double-quotes"),
            pytest.param('x="$$(echo ")"); gh pr create # "', id="dollars-before-a-paren-in-double-quotes"),
            pytest.param('x="${y:-\\}\'"\'}"; gh pr create # "\'', id="escaped-brace-then-single-quote-in-expansion"),
            pytest.param('x="${ gh pr create; }"', id="command-brace-in-double-quotes"),
            pytest.param('x="${|gh pr create;}"', id="value-brace-in-double-quotes"),
            pytest.param('x="${\tgh pr create; }"', id="command-brace-after-a-tab"),
            pytest.param('x="${\ngh pr create; }"', id="command-brace-after-a-newline"),
            pytest.param("x=${ gh pr create; }", id="command-brace-unquoted"),
            pytest.param("cat <<EOF\n${ gh pr create; }\nEOF", id="command-brace-in-heredoc"),
            pytest.param("cat <<EOF\n$$${ gh pr create; }\nEOF", id="command-brace-after-pid-in-heredoc"),
            pytest.param('gh pr view "${ gh pr create --title t --body b; }"', id="command-brace-as-a-read-argument"),
            pytest.param("gh pr view 1 $\\\n'\\''; gh pr create #'", id="continued-ansi-c-string"),
            pytest.param("gh pr view 1 <<EO\\\nF\nx\nEOF\ngh pr create", id="continued-heredoc-delimiter"),
            pytest.param("cat <<EOF\n$\\\n(gh pr create)\nEOF", id="continued-substitution-in-heredoc"),
            pytest.param('gh pr view "$\\\n(gh pr create)"', id="continued-substitution-in-double-quotes"),
            pytest.param('x="${\\\n gh pr create; }"', id="continued-command-brace"),
            pytest.param("cat <<EOF\nEO\\\nF\ngh pr create\nEOF", id="continued-terminator-line"),
            pytest.param("cat <\\\n<EOF\nx\nEOF", id="continued-redirection-operator"),
            pytest.param("printf 'gh pr cr\\x65ate\\n' | sh", id="escapes-printed-to-a-stdin-shell"),
        ],
    )
    def test_quoting_the_shells_read_differently_has_no_exact_argv(self, command: str) -> None:
        """Escapes and locale strings bash and zsh decode differently leave no exact argv: the caller fails closed.

        The Bash tool runs the user's shell. zsh reads ``$'\\x{63}'`` as a NUL then ``{63}`` and ``$"git"`` as ``$git``,
        bash 3.2 has no ``\\u``; no single reading is right for every shell.
        """
        assert _invocations(command, "gh") is None

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git $'\\x{70}ush' -f origin main", id="hex-brace-subcommand"),
            pytest.param("$'\\x{67}it' push", id="hex-brace-program"),
            pytest.param('git $"push" -f', id="locale-subcommand"),
        ],
    )
    def test_quoting_the_shells_read_differently_reads_as_force_push(self, command: str) -> None:
        """Git reads ``push --force`` beside its raw-segment fallback: the unreadable text may hide the program word."""
        assert "push --force" in _readings(command)

    @pytest.mark.parametrize(
        ("command", "unreadable"),
        [
            pytest.param("gh pr $'\\x{63}reate'", True, id="unreadable-escape"),
            pytest.param('echo $"hello"', True, id="locale-string-without-the-program"),
            pytest.param("gh pr view 'x$'", False, id="dollar-before-a-closing-quote"),
            pytest.param("gh pr $'cr\\x65ate'", False, id="readable-escape"),
        ],
    )
    def test_unreadable_flag_reports_shell_dependent_quoting(self, command: str, unreadable: bool) -> None:
        """``info.unreadable`` comes from the lexer, not from a text search: ``'x$'`` is a plain quoted word."""
        assert _unreadable(command) is unreadable

    @pytest.mark.parametrize(
        ("command", "program", "construct"),
        [
            pytest.param("echo ${(U)HOME}", "gh", "${(flags)…}", id="zsh-flags"),
            pytest.param("echo ${(e)HOME}", "git", "${(flags)…}", id="zsh-eval-flag-git"),
            pytest.param('echo $"x"', "gh", '$"…"', id="locale-string"),
            pytest.param("echo .(e:'date':)", "gh", "glob qualifier", id="zsh-glob-qualifier"),
            pytest.param("echo $'\\u0041'", "git", "$'…'", id="ansi-c-escape"),
            pytest.param('x="${ gh pr view 1; }"', "gh", "${ cmd; }", id="command-brace"),
            pytest.param("x=$$'a'", "gh", "$$", id="dollars-before-a-quote"),
            pytest.param("printf 'a\\n' | sh", "gh", "stdin", id="backslash-printed-to-a-shell"),
        ],
    )
    def test_unreadable_reason_names_the_construct(self, command: str, program: str, construct: str) -> None:
        """``info.unreadableReason`` names the shell syntax that made the command unreadable, for any program.

        The guards block such a command; their message must name the real cause, not a push or a gh write the command
        may not hold (round 9b live lane, F5: ``echo ${(U)HOME}`` was "git push blocked").
        """
        assert construct in _unreadable_reason(command, program)

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr view 1", id="plain"),
            pytest.param("gh pr $'cr\\x65ate'", id="readable-escape"),
            pytest.param("gh pr view 'unbalanced", id="unbalanced-is-not-shell-dependent"),
        ],
    )
    def test_unreadable_reason_is_empty_for_readable_quoting(self, command: str) -> None:
        """A command bash and zsh read alike carries no reason, unbalanced quoting included (that is not unreadable)."""
        assert _unreadable_reason(command) == ""

    @pytest.mark.parametrize(
        ("command", "words"),
        [
            pytest.param('x="${a%"b"}"; gh pr merge 1', ["gh", "pr", "merge", "1"], id="nested-quotes-then-a-write"),
            pytest.param('gh pr view "${N:-1}"', ["gh", "pr", "view", "${N:-1}"], id="expansion-as-an-argument"),
            pytest.param('x="${a:-b\\}c}"; gh pr view 1', ["gh", "pr", "view", "1"], id="escaped-brace-stays-inside"),
            pytest.param('gh pr view "$$" 1', ["gh", "pr", "view", "$$", "1"], id="pid-before-the-closing-quote"),
            pytest.param(
                'echo "${HOME} ${#x} ${z[@]}"; gh pr view', ["gh", "pr", "view"], id="parameters-stay-readable"
            ),
            pytest.param(
                "cat <<EOF\n$$$(gh pr create)\nEOF", ["gh", "pr", "create"], id="pid-then-substitution-in-heredoc"
            ),
            pytest.param(
                'x="`echo \\"\'\\"; gh pr create; \\"\'\\"`"',
                ["gh", "pr", "create"],
                id="escaped-double-quote-in-backticks-in-double-quotes",
            ),
            pytest.param("echo hi # note \\\ngh pr create", ["gh", "pr", "create"], id="comment-does-not-continue"),
            pytest.param("trap 'gh pr create' EXIT", ["gh", "pr", "create"], id="trap-handler-is-source"),
            pytest.param("echo 'gh pr view 1' | sh", ["gh", "pr", "view", "1"], id="text-printed-to-a-stdin-shell"),
            pytest.param("let 'a[$(gh pr view 1)]=1'", ["gh", "pr", "view", "1"], id="subscript-substitution"),
            pytest.param("echo 1 | xargs gh pr view", ["gh", "pr", "view", "$(xargs)"], id="xargs-tail"),
        ],
    )
    def test_double_quotes_nest_inside_a_double_quoted_expansion(self, command: str, words: list[str]) -> None:
        """``"${x%"$y"}"`` nests its inner quotes in bash and zsh alike, so the command after it is still seen."""
        assert words in _invocations(command, "gh")

    def test_ansi_c_closing_quote_is_found_before_decoding(self) -> None:
        """The string ends where the shell ends it: a backslash skips one character, ``\\\\`` included.

        Decoding first let ``\\c`` consume a backslash, so the escaped quote after it closed nothing and the gh write
        that followed read as part of the string.
        """
        assert _invocations("x=$'a\\\\'; gh pr create # '", "gh") == [["gh", "pr", "create"]]

    def test_no_coarse_pass_splits_a_quoted_argument(self) -> None:
        """Only git keeps the quotes-ignored coarse pass; for gh it would split a quoted GraphQL query.

        The coarse pass exists to keep the pre-lexer git hooks' detections; gh has no earlier detector to preserve.
        """
        assert _invocations("gh api graphql -f query='{ a }'", "gh") == [["gh", "api", "graphql", "-f", "query={ a }"]]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("echo 'gh pr create'", id="quoted-text"),
            pytest.param("ls ~/.config/ghx", id="look-alike-word"),
            pytest.param("echo a>&gh pr merge 1", id="redirection-target"),
            pytest.param(None, id="non-string"),
        ],
    )
    def test_text_naming_gh_is_no_invocation(self, command: object) -> None:
        """Quoted text, look-alike words and a redirection target are data; a malformed command is no invocation."""
        assert _invocations(command, "gh") == []

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr --title 'a b' create 'x", id="unbalanced-quote"),
            pytest.param("gh pr --title 'a b' create {1..300}", id="oversized-braces"),
            pytest.param("echo $(gh pr create", id="unterminated-substitution"),
            pytest.param('bash -c "gh pr create \'x"', id="unbalanced-nested-source"),
            pytest.param("python3 -c \"import os; os.system('gh pr comment 1')\"", id="interpreter-code"),
        ],
    )
    def test_no_exact_argv_is_null(self, command: str) -> None:
        """Without an exact argv the answer is null, never a guess, and the caller fails closed.

        Quote-stripped or code-split words shift positionals: ``--title 'a b' create`` read without quoting puts ``b``
        where gh reads ``create``, so a guessed argv can hide a write instead of widening the answer.
        """
        assert _invocations(command, "gh") is None

    def test_heredoc_delimiter_keeps_a_carriage_return(self) -> None:
        """A heredoc delimiter word ends only at a blank, newline or operator, as in the shells: a CR stays in it.

        With CRLF line ends bash and zsh read the delimiter ``EOF\\r``, end the body at the line ``EOF\\r`` and run the
        next line (probed live). Stopping the word at the CR made the lexer read the rest as body, hiding a gh write.
        """
        command = "cat <<'EOF'\r\nbody\r\nEOF\r\ngh issue close 1 -c x\r\n"

        assert ["gh", "issue", "close", "1", "-c", "x\r"] in _invocations(command, "gh")

    def test_code_not_naming_the_program_keeps_the_answer(self) -> None:
        """Interpreter code that never names gh leaves the rest of the command exactly read."""
        assert _invocations("python3 -c 'print(1)' && gh pr view 1", "gh") == [["gh", "pr", "view", "1"]]

    def test_git_keeps_its_raw_segment_fallback(self) -> None:
        """Git readings fail closed by widening, so the git target still answers where any other program's is null."""
        assert ["git", "push", "x"] in _invocations("git push 'x", "git")

    def test_git_keeps_its_own_target(self) -> None:
        """Asking for git returns the git invocations the readings use, coarse pass included."""
        assert ["git", "push", "-f"] in _invocations("bash -c 'git push -f'", "GIT")
