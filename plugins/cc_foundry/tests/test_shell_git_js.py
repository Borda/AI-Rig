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
import shlex
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
        "command",
        [
            'fish -C "gh pr list" -c :',
            'fish --init-command="gh pr list" --command :',
            'fish -iC "gh pr list" -c :',
            'fish -C"gh pr list" -c :',
            'fish -ic "gh pr list"',
            'fish --command="gh pr list"',
            'exec 3<<< "gh pr list"; sh <&3',
            'exec 3<<< "gh pr list"; sh 0<&3-',
            'exec 3<<< "gh pr list"; sh 0<&3',
            'exec 3<<< "gh pr list"; sh <&3-',
            "exec 3<<'EOF'\ngh pr list\nEOF\nsh <&3",
            "printf '%s' 'gh pr list' >s; exec 3<s; sh <&3",
            "exec 3<<'EOF'\ngh pr list\nEOF\nexec 4<&3; sh <&4",
            "alias -p; gh pr list",
            "alias; gh pr list",
            "alias f; gh pr list",
            "function f { :; }; gh pr list",
            "function f { gh pr list; }; f",
            "function f() { gh pr list; }; f",
            'f() { [ -f "$x" ]; case "$x" in *) :;; esac; }; gh pr list',
            "alias f='gh pr list'; f",
            "grep -F eval '$x'; gh pr list",
            "env grep -F eval '$x'; gh pr list",
        ],
    )
    def test_final_round_literal_consumers_and_queries(self, command: str) -> None:
        """Startup code, saved input, definitions and data-position eval expose the literal read."""
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize("shell", ["nu", "pwsh", "dash", "ksh", "csh", "tcsh", "xonsh", "elvish"])
    def test_final_round_inline_shell_launchers(self, shell: str) -> None:
        """Every supported inline launcher reads its operand or refuses its foreign syntax."""
        option = "-Command" if shell == "pwsh" else "-c"
        command = f'{shell} {option} "gh pr list"'
        assert _unreadable(command) or _invocations(command, "gh") == [["gh", "pr", "list"]]

    @pytest.mark.parametrize(
        "command",
        [
            'fish -C "$x" -c :',
            'fish --init-command="$x" -c :',
            'exec 3<<< "$x"; sh <&3',
            'exec 3<<< "gh pr list"; exec 3<&-; sh <&3',
            'exec 3<<< "gh pr list"; exec 3<<< "$x"; sh <&3',
            'exec 3<"$x"; sh <&3',
            "function gh { :; }; gh pr list",
            "alias gh=':'; gh pr list",
            "function f { $x; }; gh pr list",
            'eval "$(cat f)"',
            r"env -S 'printf %s gh\ pr\ list' | sh",
        ],
    )
    def test_final_round_unknown_consumers_refuse(self, command: str) -> None:
        """Dynamic source, invalid descriptors, modeled-name shadows and rejected splits fail closed."""
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize(
        "name",
        [
            "gh",
            "git",
            "bash",
            "sh",
            "eval",
            "command",
            "builtin",
            "env",
            "xargs",
            "exec",
            "alias",
            "cat",
            "true",
            "tee",
        ],
    )
    def test_final_round_modeled_definition_names_refuse(self, name: str) -> None:
        """Definitions cannot replace a command whose execution or output the lexer models."""
        command = f"function {name} {{ :; }}; gh pr list"
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    def test_final_round_rejected_env_split_has_specific_reason(self) -> None:
        """The rejected split is handled at its cause instead of reaching the exception guard."""
        assert (
            _unreadable_reason(r"env -S 'printf %s gh\ pr\ list' | sh")
            == "an env split-string escape or expansion without a fixed reading"
        )

    def test_final_round_unexpected_lexer_error_is_unreadable(self) -> None:
        """An unexpected internal error cannot escape the public classifier or yield readable emptiness."""
        proc = _node(
            "const split = String.prototype.split;"
            "String.prototype.split = () => { throw new Error('injected lexer failure'); };"
            "const info = {}; const result = lib.programInvocations(':', 'gh', info);"
            "String.prototype.split = split; process.stdout.write(JSON.stringify({result, info}));"
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == {
            "result": None,
            "info": {"unreadable": True, "unreadableReason": "lexer error"},
        }

    @pytest.mark.parametrize("keyword", ["if", "for", "while", "case"])
    @pytest.mark.parametrize("parentheses", [True, False])
    def test_case_keyword_words_preserve_stdin_scope(self, keyword: str, parentheses: bool) -> None:
        """Case subjects and patterns cannot open or close a keyword compound's input scope."""
        pattern = f"({keyword})" if parentheses else f"{keyword})"
        command = f"case {keyword} in {pattern} sh;; esac <<<'gh pr list'"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "bash -c \"$(printf '%s' 'gh pr list')\"",
            "eval \"$(cat <<'EOF'\ngh pr list\nEOF\n)\"",
            "eval \"$(printf g; printf 'h pr list')\"",
            "printf '%s\\n' '#!/bin/sh' 'gh pr list' >s; chmod +x s; ./s",
            "dd of=s <<'EOF'\ngh pr list\nEOF\nbash s",
            "printf '%s' 'gh pr list' | env tee s; bash s",
            "sed 's/^/gh /' <<< 'pr list' >s; bash s",
            "echo() { printf '%s' 'gh pr list'; }; echo ok | sh",
            'zsh -c \'alias p="printf %s"\np "gh pr list" | sh\'',
            "printf '%s' 'gh pr list' | parallel 'sh -c \"{}\"'",
            "printf '%s' 'gh pr list' >s; bash --rcfile s -ic :",
        ],
    )
    def test_code_consumers_never_hide_generated_invocations(self, command: str) -> None:
        """A consumer reads proven bytes or refuses; unknown output cannot become a readable empty result."""
        invocations = _invocations(command, "gh")
        assert _unreadable(command) or (invocations is not None and ["gh", "pr", "list"] in invocations)

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("bash -c cat <<< 'a(b)'; gh pr list", [["gh", "pr", "list"]], id="command-stdin-is-data"),
            pytest.param("python -m x > out.txt; cat out.txt", [], id="ordinary-generator-output"),
            pytest.param("mkdir -p d && bash scripts/run.sh", [], id="preexisting-script"),
            pytest.param("git diff > p.diff; git apply --check p.diff", [], id="patch-data"),
            pytest.param(
                "cat <<'EOF' > s.sh\ngh pr list\nEOF\nbash s.sh", [["gh", "pr", "list"]], id="literal-heredoc-file"
            ),
            pytest.param(
                "printf '%s\\n' 'gh pr list' > s.sh; bash s.sh", [["gh", "pr", "list"]], id="literal-printer-file"
            ),
        ],
    )
    def test_consumer_checks_preserve_ordinary_commands(self, command: str, expected: list[list[str]]) -> None:
        """File data and a non-shell command's stdin remain readable alongside known generated scripts."""
        assert _invocations(command, "gh") == expected
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "eval \"$(printf g; printf 'h pr list')\"",
            "bash -c \"$(printf '%s' 'gh pr list')\"",
            "printf '%s' 'gh pr list' >s; exec ./s",
            "printf '%s' 'gh pr list' >s; command source s",
            "printf '%s' 'gh pr list' >s; BASH_ENV=s bash -c :",
            "printf '%s' 'gh pr list' >s; ENV=s sh -ic :",
            "printf '%s' 'gh pr list' >d/.zshenv; ZDOTDIR=d zsh -c :",
            "find . -exec sh -c 'gh pr list' \\;",
        ],
    )
    def test_proven_consumer_source_is_read_exactly(self, command: str) -> None:
        """Known substitution bytes, executable paths, startup files and templates expose the complete argv."""
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            'bash <<< "$SOURCE"',
            'bash -c "$""V"',
            'eval "$""V"',
            'eval "$(cat input.txt)"',
            "sed 's/^/gh /' <<< 'pr list' | eval sh",
            "python generator.py | sh",
            "sed -i 's/x/y/' s; bash s",
            "command cp input s; bash s",
            "nice mv input s; bash s",
            "timeout 5 install input s; bash s",
            "ln -s input s; bash s",
            "printf '%s' 'gh pr list' | env xargs tee s; bash s",
            "printf g >s; printf 'h pr list' >>s; sh s",
            "printf '%s' 'h pr list' | xargs -I{} sh -c 'g{}'",
            "printf '%s' 'h pr list' | parallel 'sh -c \"g{}\"'",
            "alias p='printf %s'; p 'gh pr list' | sh",
            "command() { printf '%s' 'gh pr list'; }; command echo ok | sh",
        ],
    )
    def test_unknown_consumer_source_fails_closed(self, command: str) -> None:
        """Unknown generators, ambiguous file writes and joined replacements cannot hide source bytes."""
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "cd project && .venv/bin/pre-commit run --files $(git diff --name-only) 2>&1 | grep -c ok",
            "cd project && .venv/bin/python -m pytest plugins/x -q 2>&1 | tail -6",
            '.venv/bin/python -m pytest -n 4 -q > output.txt 2>&1; echo "exit=$?"; tail -4 output.txt',
            'FILES=$(sed p .temp/a | tr a b) && echo "$FILES" > .temp/b && .venv/bin/python -m pytest $FILES',
            "S=project; mkdir -p $S/d; sed -e p $S/a.py > $S/b.py; .venv/bin/python $S/b.py > $S/inv 2>&1",
            "plugins/codemap-py/bin/codemap-py query --help",
            "generator ./s; bash s",
            "python generator.py s; bash s",
            "env dd; bash s",
            "sed -e p s; bash s",
            "xargs sed -e p; bash s",
            "xargs perl -pe 's/x/y/'; bash s",
            "awk -f program.awk s; ./s",
            "python -m pytest > out.txt; ./s",
            "python -m pytest 2>&1 > output.txt; ./s",
            "sed -i 's/x/y/' other; bash s",
            "perl -pi -e 's/x/y/' other; bash s",
            'tee "$OUT"; .venv/bin/python -m pytest',
            'tee "$OUT"; plugins/tool/bin/query --help',
            'echo "$TEXT" > "$OUT"; ./.venv/bin/python -m pytest',
            'tee "$FIRST"; ./"$SECOND"',
            "cp input other; ./s",
            "command cp input other; bash s",
            "nice mv input other; bash s",
            "timeout 5 install input other; bash s",
            "sudo ln -s input other; bash s",
        ],
    )
    def test_only_matching_modeled_writes_require_file_content_proof(self, command: str) -> None:
        """Unknown tools are not writers; unrelated sinks and expanded targets leave existing tools readable."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "grep -n ok f | tail -1 | cut -d: -f1 | xargs -I{} sh -c 'sed -n \"{}p\" f'",
            'bash -c ".venv/bin/python -m pytest \\$(cat $S/plan.txt) -q"',
            "/usr/bin/time -p bash -c \"for i in \\$(seq 30); do .venv/bin/python -c 'print(1)'; done\"",
            "P=.venv/bin/python; for flags in -B -E; do sh -c \"for i in 1 2 3; do $P $flags -c 'print(1)'; done\"; done",
            'export CSID=one; IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/x"; bash "$IMPL_DIR/p3.sh"; '
            'jq -c . "$IMPL_DIR/a.json" > "$IMPL_DIR/b.json"',
            "SP=project; PYTHONPATH=src $SP/venv39/bin/python -m pytest tests/a -q 2>&1 | tail",
            "R=project; for a in one two; do $R/.venv/bin/python check $a $R/src/x >/dev/null 2>$R/../.err; done",
            "~/.cache/pre-commit/x/bin/ruff check --no-fix src/a src/b",
            "git diff --name-only | grep ok | xargs pre-commit run ruff-format --files",
            "cd project && for f in d1 d2; do /bin/bash $f.sh 2>&1 | head -6; /bin/zsh $f.sh; done",
        ],
    )
    def test_runtime_arguments_and_existing_tool_paths_stay_readable(self, command: str) -> None:
        """Argument expansions and unrelated sinks do not turn ordinary tool runs into unknown source."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param('bash -c "gh pr list $ARG"', ["gh", "pr", "list", "$ARG"], id="variable-argument"),
            pytest.param('eval "gh pr list $(cat args)"', ["gh", "pr", "list", "$(cat args)"], id="eval-argument"),
            pytest.param('bash -c "gh pr list `cat args`"', ["gh", "pr", "list", "`cat args`"], id="backtick-argument"),
            pytest.param(
                "bash <<'EOF'\ngh pr list $(cat args)\nEOF", ["gh", "pr", "list", "$(cat args)"], id="heredoc-argument"
            ),
            pytest.param(
                "cat args | xargs -I{} sh -c 'gh pr list {}p'", ["gh", "pr", "list", "{}p"], id="replacement-argument"
            ),
            pytest.param(
                "cat args | parallel 'sh -c \"gh pr list {}p\"'", ["gh", "pr", "list", "{}p"], id="parallel-argument"
            ),
        ],
    )
    def test_runtime_source_arguments_preserve_argv(self, command: str, expected: list[str]) -> None:
        """Reading known source retains argument-position values exactly as the shell lexer spells them."""
        assert _invocations(command, "gh") == [expected]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            'tee "$R/b.json"; bash "$R/p3.sh"',
            'tee "${R}/b.json"; bash "$R/p3.sh"',
            'tee "$R/../.err"; $R/venv39/bin/python -m pytest',
            'tee "$OUT"; bash "$R/p3.sh"',
            'tee "$OUT"; bash existing.sh',
            'tee "$OUT"; bash "$SCRIPT"',
            'tee "$FIRST"; bash "$SECOND"',
            'tee "$P"; $P',
            'timeout 5 tee "$OUT"; bash s',
            'sudo dd of="$OUT"; ./s',
            'cp input "$OUT"; . s',
            'mv input s; bash "$SCRIPT"',
            'tee "$R/s"; bash "$S/s"',
            'tee "$R/../s"; bash "$R/s"',
            'tee "$OUT"; ~/.cache/pre-commit/x/bin/ruff check src/a',
            'tee "$OUT"; /usr/bin/python -m pytest',
            'tee "$OUT"; /bin/bash existing.sh',
            'P=.venv/bin/python; tee "$OUT"; $P -c pass',
            "P=./existing.sh; tee other.sh; $P",
            "tee other.sh; for f in d1 d2; do bash $f.sh; done",
            "tee d3.sh; for f in d1 d2; do bash $f.sh; done",
        ],
    )
    def test_expansion_paths_only_match_compatible_targets(self, command: str) -> None:
        """Expansion prefixes, normalized suffixes, literal tools and literal loop items bound path matches."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            'tee "$R/s.sh"; bash "$R/s.sh"',
            'tee "${R}/d/../s.sh"; bash "$R/s.sh"',
            'tee "$P"; "$P"',
            "tee .venv/bin/python; P=.venv/bin/python; $P -c pass",
            "P=./s; tee s; $P",
            "tee d1.sh; for f in d1 d2; do bash $f.sh; done",
            "tee d2.sh; for f in d1 d2; do /bin/bash ${f}.sh; done",
            "tee s.sh; for f in s; do . $f.sh; done",
            "tee /usr/bin/python; /usr/bin/python -m pytest",
            "tee /bin/bash; /bin/bash existing.sh",
            'tee "~/.cache/pre-commit/x/bin/ruff"; ~/.cache/pre-commit/x/bin/ruff check src/a',
            "cat args | xargs -I{} sh -c '{}'",
            "cat args | xargs -I{} sh -c '{} pr list'",
            "cat args | parallel 'sh -c \"{}\"'",
            'bash -c "$(cat code) pr list"',
            'bash -c "${CMD} pr list"',
            "bash -c '$CMD pr list'",
            "bash <<'EOF'\n$CMD pr list\nEOF",
            "P=/bin/bash; echo $(P=/bin/echo); sh -c '$P -c \"gh pr list\"'",
            "P=/bin/bash; eval 'P=/bin/echo'; sh -c '$P -c \"gh pr list\"'",
            'P=/bin/bash; printf -v P "%s" "$OTHER"; sh -c \'$P -c "gh pr list"\'',
        ],
    )
    def test_source_positions_and_matching_paths_require_proof(self, command: str) -> None:
        """Unknown command bytes and actual overwrites remain unreadable after narrowing argument values."""
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "P=/bin/bash; $P -c 'gh pr list'",
            "P=/bin/bash; bash -c '$P -c \"gh pr list\"'",
            "P=/bin/bash; printf '%s' 'gh pr list' > s.sh; $P s.sh",
            "P=/bin/bash; printf '%s' 'gh pr list' | $P",
        ],
    )
    def test_literal_path_command_keeps_its_shell_consumers(self, command: str) -> None:
        """Resolving an executable path must preserve both its inline source and generated script operand."""
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i 's/x/y/' ./s; bash s",
            "env sed --in-place=.bak -e 's/x/y/' s; ./s",
            "command perl -pi -e 's/x/y/' s; bash ./s",
            "nice perl -i.bak -pe 's/x/y/' s; source s",
            'install input "$SCRIPT"; bash "$SCRIPT"',
            'ln -s input "$SCRIPT"; bash "$SCRIPT"',
            "env xargs tee; bash s",
            "xargs -I{} dd of={}; bash s",
            "python generator.py > ./.venv/bin/python; .venv/bin/python -m pytest",
            "cp input plugins/tool/bin/query; plugins/tool/bin/query --help",
            "sed -i 's/x/y/' .venv/bin/python; .venv/bin/python -m pytest",
            "perl -pi -e 's/x/y/' plugins/tool/bin/query; plugins/tool/bin/query --help",
            "python helper.py 2>s; bash s",
            "python helper.py >|s; bash s",
            "python helper.py >>s; ./s",
            "python helper.py >s; bash --init-file=s -ic :",
            "python helper.py >s; env -S 'bash s'",
            'sudo tee "./s"; bash dir/../s',
            'tee "$SCRIPT"; bash "$SCRIPT"',
        ],
    )
    def test_matching_modeled_sinks_refuse_unknown_file_contents(self, command: str) -> None:
        """Normalized and possibly matching sink paths require proof, including literal tool overwrites."""
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize("consumer", ["grep bash", "rg bash", "cat bash", "env grep bash", "sudo cat bash"])
    def test_shell_named_data_does_not_select_stdin_consumer(self, consumer: str) -> None:
        """A shell name in ordinary argv does not execute the producer's expanded output."""
        command = f'echo "$TEXT" | {consumer}'
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            'echo "$TEXT" | grep env -S bash',
            "man bash <<'TEXT'\ngh pr create\nTEXT",
            "cat > helper.sh <<'TEXT'\ngh pr create\nTEXT\nman bash helper.sh",
            "man bash -O extglob -c 'echo data'",
            "man bash < script.sh | grep 'a\\|b'",
            "echo 'gh pr create' | runner } bash",
        ],
    )
    def test_ordinary_arguments_do_not_run_shell_source(self, command: str) -> None:
        """The command-position contract supersedes the former scan of unknown programs' arguments."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        ("written", "executed", "matched"),
        [
            pytest.param("logs/setup.sh", "scripts/setup.sh", False, id="distinct-directories"),
            pytest.param("./scripts/../w.sh", "./w.sh", True, id="normalized-literal-path"),
            pytest.param("'$OUT'", "'$RUN'", False, id="literal-dollar-paths"),
            pytest.param('"$OUT"', '"$OUT"', True, id="identical-expansions"),
            pytest.param('"$OUT"', '"$RUN"', False, id="distinct-runtime-paths"),
            pytest.param('"$OUT"', "$OUT", False, id="different-quoting"),
            pytest.param('"$OUT"', '"${OUT}"', False, id="different-expansion-spelling"),
            pytest.param('"$OUT"', '""$OUT', False, id="different-quote-placement"),
            pytest.param('"$OUT"', "w.sh", False, id="expanded-write-literal-run"),
            pytest.param("w.sh", '"$RUN"', False, id="literal-write-expanded-run"),
        ],
    )
    def test_written_script_matches_paths(self, written: str, executed: str, matched: bool) -> None:
        """Literal paths normalize; runtime paths match only identical words or compatible literal suffixes."""
        command = f'echo "$DATA" > {written}; sh {executed}'
        assert _unreadable(command) is matched
        assert _invocations(command, "gh") == (None if matched else [])

    @pytest.mark.parametrize(
        "command",
        [
            '"${R[@]}" pytest tests > "$_LOG" 2>&1',
            'printf \'%s\\n\' "$STATE_FILE" > "${TMPDIR:-/tmp}/state-ref-${CSID}"; . "$STATE_FILE"',
            'IFS= read -r METRIC_CMD < "${TMPDIR:-/tmp}/metric-${CSID}"; bash -c "$METRIC_CMD"',
            'IFS= read -r GUARD_CMD < "${TMPDIR:-/tmp}/guard-${CSID}"; bash -c "$GUARD_CMD"',
            'tee "$OUT"; $COMMAND',
            'tee "$OUT"; source "$SCRIPT"',
            'tee "$OUT"; bash --rcfile "$STARTUP" -ic :',
            'tee "$OUT"; BASH_ENV="$STARTUP" bash -c :',
            'tee "$OUT"; ${=COMMAND}',
        ],
    )
    def test_distinct_runtime_words_do_not_prove_generated_source(self, command: str) -> None:
        """Different variable words carry no static relation, including array commands and state-file reads."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize("runner", ["bash -c", "eval"])
    @pytest.mark.parametrize("operand", ['"$V"', "$V", '"${V}"', '"${V:-literal}"', '"${V:-gh pr list}"', '"${=V}"'])
    def test_plain_runtime_source_operands_stay_out_of_scope(self, runner: str, operand: str) -> None:
        """Whole variable operands remain unknown state values; later literal invocations stay visible."""
        command = f"{runner} {operand}; gh pr list"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "printf '%s' 'gh pr list' > \"$X\"; bash \"$X\"",
            "printf '%s' 'gh pr list' > \"$X\"; \"$X\"",
            "printf '%s' 'gh pr list' > \"$X\"; source \"$X\"",
            "printf '%s' 'gh pr list' > \"$X\"; bash --rcfile \"$X\" -ic :",
            'dd of="$X"; bash "$X"',
            'tee "$X"; bash --rcfile="$X" -ic :',
            'X=script.sh; dd of="$X" <<\'EOF\'\ngh pr list\nEOF\nbash "$X"',
        ],
    )
    def test_identical_runtime_file_words_keep_generated_code_visible(self, command: str) -> None:
        """Identical words and literal-bound dd targets still require proven source bytes."""
        invocations = _invocations(command, "gh")
        assert _unreadable(command) or (invocations is not None and ["gh", "pr", "list"] in invocations)

    @pytest.mark.parametrize(
        "command",
        [
            "print -f 'g%s pr list' h | sh",
            "env -S \"printf 'g%s pr list' h\" | sh",
            "echo 'gh pr list' | xargs -I X env -S 'sh -c \"X\"'",
            "echo 'gh pr list' | parallel -I X env -S 'sh -c \"X\"'",
            "env -S \"print -f 'g%s pr list' h\" > w.sh; source ./w.sh",
            "sudo env -S \"printf 'g%s pr list' h\" | sh",
            "echo 'gh pr list' | xargs -I X sh -c 'env -S \"sh -c X\"'",
            "echo 'gh pr list' | xargs -I X env -S 'env -S \"sh -c X\"'",
        ],
    )
    def test_reread_printers_and_placeholders_supply_source(self, command: str) -> None:
        """Split-string printers and placeholders in nested source keep their stdin provenance."""
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "producer",
        [
            r"echo -e '\u0067h pr list'",
            "echo -e 'gh pr list'",
            r"echo 'g\x68 pr list'",
            "echo -E 'gh pr list'",
            "print -u 2 'gh pr list'",
            'print "$TEXT"',
            "print -f '%b' 'gh pr list'",
            "env -S \"printf '%b' 'gh pr list'\"",
            "env -S \"print '$TEXT'\"",
            "sudo print -f '%c%s pr list' g h",
            "xargs printf '%s'",
            "xargs -I X printf '%s' X",
            "parallel -I X env -S \"printf '%s' X\"",
            "printf '%-999999999999s' x",
            "printf '%-3s' 'é'",
        ],
    )
    @pytest.mark.parametrize("sink", ["| sh", "> w.sh; source ./w.sh", '| eval "$(cat)"'])
    def test_printed_source_outside_closed_set_fails_closed(self, producer: str, sink: str) -> None:
        """Every printer path uses the same literal-only source contract and refusal reason."""
        command = f"{producer} {sink}"
        assert _invocations(command, "gh") is None
        assert _unreadable_reason(command) == "printed shell source outside the decoded set"

    @pytest.mark.parametrize(
        "template",
        [
            'eval "$({producer})"',
            'sh <<< "$({producer})"',
            "sh <<EOF\n$({producer})\nEOF",
            "cat <<EOF | sh\n$({producer})\nEOF",
        ],
    )
    @pytest.mark.parametrize(
        ("producer", "expected"),
        [
            pytest.param("printf 'g%s pr list' h", [["gh", "pr", "list"]], id="literal-substituted-source"),
            pytest.param("print -f '%b' 'gh pr list'", None, id="unsupported-substituted-source"),
            pytest.param('print "$TEXT"', None, id="expanded-substituted-source"),
        ],
    )
    def test_substituted_print_source_uses_closed_set(
        self, template: str, producer: str, expected: list[list[str]] | None
    ) -> None:
        """Substitution output becomes source when eval or an expanding shell input consumes it."""
        command = template.format(producer=producer)
        assert _invocations(command, "gh") == expected
        assert _unreadable(command) is (expected is None)

    @pytest.mark.parametrize(
        ("script_command", "expected"),
        [
            pytest.param("pytest -q tests", [], id="literal-script-without-gh"),
            pytest.param("gh pr list", [["gh", "pr", "list"]], id="literal-script-with-gh"),
        ],
    )
    def test_literal_printf_script_escapes_remain_readable(
        self, script_command: str, expected: list[list[str]]
    ) -> None:
        """Printed script escapes are decoded before shell parsing; quoted runtime code remains literal data."""
        command = (
            r"printf '#!/bin/bash\nset -o pipefail\n"
            + script_command
            + '\\nprintf "%%s\\\\n" "$?" > r.rc\\n\' > full.sh; bash full.sh'
        )
        assert _invocations(command, "gh") == expected
        assert _unreadable(command) is False

    @pytest.mark.parametrize("sink", ["| sh", "> w.sh; bash w.sh", "> w.sh; zsh w.sh"])
    def test_echo_backslash_source_fails_closed(self, sink: str) -> None:
        """Backslashes have shell-dependent echo semantics and are outside the decoded set."""
        command = r"echo 'gh\tpr\tlist' " + sink
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize("producer", ["printf '%s' \"$SOURCE\"", 'printf "$FORMAT"', 'echo "$SOURCE"'])
    @pytest.mark.parametrize("sink", ["| sh", "> w.sh; bash w.sh"])
    def test_nonliteral_print_output_fails_closed_only_when_executed(self, producer: str, sink: str) -> None:
        """Expanded arguments or formats make generated source unknown; ordinary printing remains readable."""
        assert _invocations(f"{producer} {sink}", "gh") is None
        assert _unreadable(f"{producer} {sink}") is True
        for data_sink in ("", "> notes.txt", "| cat"):
            assert _invocations(f"{producer} {data_sink}", "gh") == []
            assert _unreadable(f"{producer} {data_sink}") is False

    @pytest.mark.parametrize("conversion", ["format", "%b"])
    @pytest.mark.parametrize(
        ("escaped", "decoded", "allowed"),
        [
            pytest.param(r"\n", "\n", True, id="newline"),
            pytest.param(r"\t", "\t", True, id="tab"),
            pytest.param(r"\r", "\r", False, id="carriage-return"),
            pytest.param(r"\a", "\a", False, id="bell"),
            pytest.param(r"\b", "\b", False, id="backspace"),
            pytest.param(r"\f", "\f", False, id="form-feed"),
            pytest.param(r"\v", "\v", False, id="vertical-tab"),
            pytest.param(r"\\", "\\", True, id="backslash"),
            pytest.param(r"\147", "g", False, id="octal"),
            pytest.param(r"\x67", "g", False, id="hex"),
        ],
    )
    def test_printf_escape_allowlist(self, conversion: str, escaped: str, decoded: str, allowed: bool) -> None:
        """Only newline, tab and backslash format escapes are admitted; %b always refuses."""
        source = f'gh pr view "a{escaped}b"'
        producer = "printf " + ("'%b' " if conversion == "%b" else "") + shlex.quote(source)
        command = producer + " | sh"
        expected = [["gh", "pr", "view", f"a{decoded}b"]] if allowed and conversion == "format" else None
        assert _invocations(command, "gh") == expected
        assert _unreadable(command) is (expected is None)

    @pytest.mark.parametrize(
        "producer",
        [
            "printf 'gh pr view \\\"a b\\\"'",
            "printf " + shlex.quote(r"gh pr view \'a b\'"),
            "printf '%b' 'gh pr view a\\\"b'",
            "printf '%b' " + shlex.quote(r"gh pr view a\'b"),
            r"printf '%b pr list' '\0147h'",
            r"printf 'gh pr view a\0147b'",
            r"printf 'gh pr view \x25s'",
            r"printf 'gh pr cr\x65ate\n'",
        ],
    )
    def test_printf_removed_decodings_fail_closed(self, producer: str) -> None:
        """Former general decodings refuse instead of guessing generated source."""
        command = producer + " | sh"
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize("option", ["", "-e", "-E"])
    def test_echo_does_not_union_interpretations(self, option: str) -> None:
        """Default and option-selected echo decodings all refuse backslash source."""
        command = f"echo {option} " + shlex.quote(r"gh pr view a\tb") + " | sh"
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    def test_echo_unknown_and_octal_escapes_fail_closed(self) -> None:
        """Neither unknown nor octal echo escapes are modeled as executable output."""
        command = r"echo 'g\h pr list' | sh"
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True
        assert _invocations(r"echo 'gh pr view \147' | sh", "gh") is None
        assert _invocations(r"echo 'gh pr list\047' | sh", "gh") is None

    @pytest.mark.parametrize("producer", [r"printf 'gh\q pr list'", r"printf '%b' 'gh pr list\c'"])
    def test_unsupported_printf_escapes_still_fail_closed(self, producer: str) -> None:
        """Unsupported format escapes and escaped-string termination keep the conservative refusal."""
        assert _invocations(producer + " | sh", "gh") is None
        assert _unreadable(producer + " | sh") is True

    def test_printf_variable_assignment_has_no_printed_stdout(self) -> None:
        """Printf -v writes a variable, so its dynamic operands do not supply source to a redirected file."""
        command = 'printf -v result "%s" "$SOURCE" > w.sh; bash w.sh'
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "python make_script.py | python -c 'print(input())' > generated.sh && bash generated.sh",
            "sed 's/old()/new()/' input.sh > generated.sh; bash generated.sh",
            "cat input.sh > generated.sh; bash generated.sh",
        ],
    )
    def test_unknown_generated_script_contents_fail_closed(self, command: str) -> None:
        """An in-input generator invalidates the pre-existing-file residual at its code consumer."""
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    def test_generator_heredoc_input_stays_data(self) -> None:
        """A generator's heredoc is data; a heredoc read directly by a shell remains executable source."""
        command = "python -c 'print(\"echo ok\")' > generated.sh <<'EOF'\necho (data)\nEOF\nbash generated.sh"
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True
        assert _invocations("bash <<'EOF'\necho (data)\nEOF", "gh") is None
        assert _unreadable("bash <<'EOF'\necho (data)\nEOF") is True

    def test_generated_environment_source_fails_closed(self) -> None:
        """A runtime validation command does not prove the generated source bytes to the lexer."""
        command = r"""tmpenv=$(mktemp)  # timeout: 3000
trap 'rm -f "$tmpenv"' EXIT INT TERM
python "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" "$ARGUMENTS" >"$tmpenv"  # timeout: 5000
if grep -qvE "^[A-Z_][A-Z0-9_]*=([A-Za-z0-9_./:#@+-]*|'[^']*')$" "$tmpenv"; then
    echo "Error: parse-resolve-args.py emitted unexpected output — refusing to source"
    cat "$tmpenv"
    exit 1
fi
. "$tmpenv"
"""
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param(
                "echo 'gh pr list' | sed 's/list/view/' > generated.sh; bash generated.sh",
                None,
                id="transformed-output",
            ),
            pytest.param("python helper.py | tee generated.sh; bash generated.sh", None, id="forwarded-runtime-output"),
            pytest.param(
                "python helper.py | printf 'gh pr list' > generated.sh; bash generated.sh",
                [["gh", "pr", "list"]],
                id="literal-writer-ignores-pipe-input",
            ),
            pytest.param(
                "gh pr list > generated.sh; bash generated.sh",
                None,
                id="generator-invocation-remains-visible",
            ),
            pytest.param(
                "cat <<'EOF' | python -c 'print(input())' > generated.sh\necho (data)\nEOF\nbash generated.sh",
                None,
                id="transformed-heredoc-data",
            ),
        ],
    )
    def test_generated_output_residual_preserves_known_executions(
        self, command: str, expected: list[list[str]] | None
    ) -> None:
        """A literal writer may ignore pipe input; transformed code must fail closed at execution."""
        assert _invocations(command, "gh") == expected
        assert _unreadable(command) is (expected is None)

    @pytest.mark.parametrize("substitution", ["$(cat; bash)", '"$(cat; bash)"', '"`cat; bash`"'])
    def test_substitution_consumes_input_without_executing_outer_echo_data(self, substitution: str) -> None:
        """The nested shell consumes the incoming pipe; the final echo's expanded operand is ordinary output."""
        command = f"echo 'gh pr create' | echo {substitution}"
        assert _invocations(command, "gh") == [["gh", "pr", "create"]]
        assert _unreadable(command) is False
        assert _invocations(command + " | sh", "gh") is None
        assert _unreadable(command + " | sh") is True

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("{ eval $(:);echo $x;}", id="minimal-compound"),
            pytest.param('VALUE="$(echo "$x")" eval :', id="assignment-is-not-eval-source"),
            pytest.param('env VALUE="$(echo "$x")" eval :', id="wrapper-assignment-is-not-eval-source"),
            pytest.param('eval >"$(echo "$x")" :', id="redirection-is-not-eval-source"),
        ],
    )
    def test_eval_substitution_does_not_capture_sibling_prints(self, command: str) -> None:
        """Only substitution stdout reaches eval; sibling compound commands write ordinary output."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            'if eval "$(python helper)"; then echo "$x"; fi',
            '{ echo "$x"; eval "$(python helper)"; }',
            '{ eval "`python helper`"; echo "$x"; }',
            '{ eval "$(python helper)"; echo "gh pr create"; }',
        ],
    )
    def test_unknown_eval_output_fails_closed_inside_compounds(self, command: str) -> None:
        """Sibling output cannot prove unknown substitution bytes consumed by eval."""
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize(
        ("producer", "expected"),
        [
            pytest.param("printf 'g%s pr list' h", [["gh", "pr", "list"]], id="literal-source"),
            pytest.param('echo "$x"', None, id="runtime-source"),
            pytest.param("printf '%b' 'gh pr create'", None, id="unsupported-source"),
        ],
    )
    def test_eval_substitution_inside_compound_retains_source_checks(
        self, producer: str, expected: list[list[str]] | None
    ) -> None:
        """Narrowing capture scope still decodes or refuses the actual eval producer."""
        command = f'{{ eval "$({producer})"; echo "$status"; }}'
        assert _invocations(command, "gh") == expected
        assert _unreadable(command) is (expected is None)

    def test_historical_review_intake_refuses_unknown_eval_output(self) -> None:
        """Python output passed to eval is unknown code; sibling diagnostic echoes remain outside eval input."""
        command = (LIBRARY.parents[2] / "tests" / "fixtures" / "historical_review_intake.sh").read_text(
            encoding="utf-8"
        )
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True
        assert _unreadable_reason(command) == "printed shell source outside the decoded set"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("bash -oc pipefail 'gh pr list'", id="shell-value-before-command-flag"),
            pytest.param("env -S 'sh -s' <<'EOF'\ngh pr list\nEOF", id="env-split-inherits-heredoc"),
            pytest.param("if true; then sh; fi <<'EOF'\ngh pr list\nEOF", id="compound-trailing-heredoc"),
            pytest.param(
                "cat > w.sh <<'EOF'\ngh pr list\nEOF\nbash <<'RUN'\nbash w.sh\nRUN",
                id="script-discovered-in-shell-heredoc",
            ),
            pytest.param("echo 'gh pr list' | parallel -I X sh -c X", id="parallel-custom-replacement"),
            pytest.param("printf 'g%s pr list' h | sh", id="printf-format-constructs-program"),
            pytest.param("echo 'gh pr list' > w.sh; bash w.sh", id="printed-script-file"),
        ],
    )
    def test_reported_source_regressions(self, command: str) -> None:
        """Shell source remains visible through clusters, stdin, generated files and literal formatting."""
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("{ case x in (x) echo ok;; esac; }", id="case-after-brace-opener"),
            pytest.param(
                "printf '%s' 'data\\text' | xargs -I{} sh -c \"printf %s '{}'\"",
                id="literal-backslash-printf-argument",
            ),
            pytest.param("shopt -q extglob; echo ok", id="shopt-query"),
            pytest.param("bash --rcfile /dev/null -c 'echo ok'", id="shell-startup-file-option"),
            pytest.param("env echo eval 'shopt -s extglob; echo ok'", id="wrapped-print-data"),
        ],
    )
    def test_reported_readability_regressions(self, command: str) -> None:
        """Queries and quoted data do not make an ordinary command unreadable."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    def test_env_split_decodes_tab_in_command(self) -> None:
        """Env decodes split-string escapes before a shell interprets its command operand."""
        command = "env -S 'bash -c \"gh\\tpr\\tcreate\"'"
        assert _invocations(command, "gh") == [["gh", "pr", "create"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "compound",
        [
            "if true; then sh; fi",
            "while false; do sh; done",
            "for x in y; do sh; done",
            "case x in (x) sh;; esac",
            "{ sh; }",
            "(sh)",
        ],
    )
    @pytest.mark.parametrize("redirect", ["<<'EOF'\ngh pr list\nEOF", "<<< 'gh pr list'"])
    def test_trailing_compound_input_feeds_every_command(self, compound: str, redirect: str) -> None:
        """A closing keyword, brace or parenthesis retains the commands its input feeds."""
        command = f"{compound} {redirect}"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "env -S 'sh -s' <<< 'gh pr list'",
            "echo 'gh pr list' | env -S 'sh -s'",
            "echo 'gh pr list' > w.sh; env -S 'sh -s' < w.sh",
            "echo 'gh pr list' >> w.sh; source w.sh",
            "echo 'gh pr list' | tee w.sh; bash w.sh",
            "cat <<'EOF' | tee w.sh\ngh pr list\nEOF\nbash w.sh",
            "printf 'g%s pr list' h > w.sh; bash w.sh",
            "echo 'gh pr list' | parallel --replace X sh -c X",
            "echo 'gh pr list' | parallel --replace=X sh -c X",
            "echo 'gh pr list' | parallel -iX sh -c X",
            "echo 'gh pr list' | parallel -i X sh -c X",
            "echo 'gh pr list' | parallel -IX sh -c X",
            "{ echo 'gh pr list'; } > w.sh; bash w.sh",
            "echo 'gh pr list' > w.sh; if true; then sh; fi < w.sh",
        ],
    )
    def test_nested_stdin_and_written_print_output_are_source(self, command: str) -> None:
        """Wrapper stdin, replacement operands and printed script files preserve literal executable text."""
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "shopt -p extglob; echo ok",
            "shopt -qp extglob; echo ok",
            "shopt -qo posix; echo ok",
            "set -o; echo ok",
            "set +o; echo ok",
            "setopt; echo ok",
            "bash --init-file /dev/null -c 'echo ok'",
            "sudo echo eval 'shopt -s extglob; echo ok'",
            "echo 'gh pr list' > notes.txt; bash w.sh",
        ],
    )
    def test_queries_and_unexecuted_print_output_stay_readable(self, command: str) -> None:
        """Option inspection, wrapped print data and unrelated output files do not become source."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "printf 'g%-2spr list' h | sh",
            "print -f 'g%s pr list' h | sh",
            "print -nr 'gh pr list' | sh",
            "echo -n 'gh pr list' | sh",
            "printf '%s' g 'h pr list' | sh",
            "printf '%s%%s' 'gh pr list --json ' | sh",
        ],
    )
    def test_literal_printf_conversions_construct_source(self, command: str) -> None:
        """String, left-aligned width and literal percent conversions are applied before source lexing."""
        expected = ["gh", "pr", "list"]
        if "%%" in command:
            expected += ["--json", "%s"]
        assert _invocations(command, "gh") == [expected]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        ("escape", "value"),
        [
            pytest.param(r"\t", "\t", id="tab"),
            pytest.param(r"\n", "\n", id="newline"),
            pytest.param(r"\v", "\v", id="vertical-tab"),
            pytest.param(r"\f", "\f", id="form-feed"),
            pytest.param(r"\r", "\r", id="carriage-return"),
            pytest.param(r"\_", " ", id="space"),
            pytest.param(r"\#", "#", id="hash"),
            pytest.param(r"\$", "$", id="dollar"),
            pytest.param(r"\\", "\\", id="backslash"),
            pytest.param(r"\"", '"', id="double-quote"),
            pytest.param(r"\'", "'", id="single-quote"),
        ],
    )
    def test_env_split_escapes_preserve_argument_boundaries(self, escape: str, value: str) -> None:
        """Every supported env escape becomes literal argv data before the executable is read."""
        command = "env -S " + shlex.quote(f'gh pr view "a{escape}b"')
        assert _invocations(command, "gh") == [["gh", "pr", "view", f"a{value}b"]]
        assert _unreadable(command) is False

    def test_env_split_stop_escape_ignores_remaining_source(self) -> None:
        """The stop escape discards the rest of env's split string."""
        command = r"env -S 'gh pr list\c gh pr create'"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            r"env -S 'bash -c gh\qpr'",
            "env -S 'bash -c \"gh pr list\\c\"'",
            "printf '%s pr list' \"$PROGRAM\" | sh",
            "printf '%q pr list' gh | sh",
            "printf '%s' *.sh | sh",
            r"printf '%b' 'gh pr list\q' | sh",
            "shopt -qs extglob; echo ok",
            'shopt -q "$FLAGS"; echo ok',
        ],
    )
    def test_unknown_source_decoding_fails_closed(self, command: str) -> None:
        """Unsupported env escapes, runtime printf arguments and unmodeled formats cannot hide code."""
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    def test_script_discovery_reaches_a_fixed_point(self) -> None:
        """A later executable heredoc can expose a chain of earlier generated scripts."""
        command = (
            "cat > first.sh <<'ONE'\ngh pr list\nONE\n"
            "cat > second.sh <<'TWO'\nbash first.sh\nTWO\n"
            "bash <<'RUN'\nbash second.sh\nRUN"
        )
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    def test_source_beyond_the_nesting_limit_fails_closed(self) -> None:
        """Bounded recursion refuses remaining source instead of silently dropping its command."""
        command = "gh pr list"
        for _ in range(6):
            command = "bash -c " + shlex.quote(command)
        assert _invocations(command, "gh") is None
        assert _unreadable(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("bash -co pipefail 'gh pr list'", id="shell-cluster-value"),
            pytest.param("env --argv0 test -S 'gh pr list'", id="env-value-before-split"),
            pytest.param("cat > /tmp/s <<'EOF'\ngh pr list\nEOF\nsource -- /tmp/s", id="source-option-terminator"),
            pytest.param("cat > /tmp/s <<'EOF'\ngh pr list\nEOF\nenv -S 'bash /tmp/s'", id="nested-script-operand"),
            pytest.param("cat <<'EOF' | if true; then bash; fi\ngh pr list\nEOF", id="compound-inherits-stdin"),
            pytest.param("cat <<'EOF' | xargs -I{} sh -c '{}'\ngh pr list\nEOF", id="replacement-command-source"),
            pytest.param("env -S 'bash -c \"gh\\_pr\\_list\"'", id="env-split-space-escape"),
        ],
    )
    def test_source_positions_preserve_reported_argv(self, command: str) -> None:
        """Executable source retains its complete argv and remains readable."""
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize("program", ["echo", "printf", "print"])
    def test_printed_interpreter_options_are_data(self, program: str) -> None:
        """An interpreter named in a printing command's argv cannot switch shell options."""
        command = f"{program} bash -o rcquotes -c text"
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize("prefix", ["echo", "printf", "print", "env echo", "sudo printf"])
    def test_printed_command_operand_is_not_reread(self, prefix: str) -> None:
        """The same executed-position rule protects quoted data from a second shell parse."""
        command = f"{prefix} bash -c 'echo (data)'"
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize("shell", ["bash", "zsh", "sh", "dash"])
    @pytest.mark.parametrize("options", ["-co pipefail", "-eco pipefail", "-c -eo pipefail", "-o pipefail -c --"])
    def test_shell_clusters_consume_option_values(self, shell: str, options: str) -> None:
        """Value-taking letters consume their operands before the pending command string."""
        command = f"{shell} {options} 'git status'"
        assert _invocations(command, "git") == [["git", "status"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "option",
        [
            "--argv0 test",
            "--argv0=test",
            "--chdir test",
            "--chdir=test",
            "--unset test",
            "--unset=test",
            "-C test",
            "-Ctest",
            "-C=test",
            "-u test",
            "-utest",
            "-u=test",
            "-a test",
            "-atest",
            "-a=test",
            "-iu test",
        ],
    )
    def test_env_values_do_not_end_split_source_scan(self, option: str) -> None:
        """Every env value spelling leaves a following split-string option visible."""
        command = f"env {option} -S 'gh pr list'"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "runner",
        ["source -- /tmp/s", ". -- /tmp/s", "env -S 'bash /tmp/s'", "eval 'bash /tmp/s'", "bash -c 'bash /tmp/s'"],
    )
    def test_nested_source_discovers_written_scripts(self, runner: str) -> None:
        """Script operands in re-read text expose the body written by the enclosing input."""
        command = f"cat > /tmp/s <<'EOF'\ngh pr list\nEOF\n{runner}"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]

    @pytest.mark.parametrize(
        "compound",
        [
            "if true; then bash; fi",
            "while true; do bash; done",
            "until false; do bash; done",
            "for item in one; do bash; done",
            "case one in one) bash;; esac",
            "{ cat; bash; }",
            "(cat; bash)",
            "f() { cat; bash; }; f",
            "if true; then while true; do bash; done; fi",
        ],
    )
    def test_compound_commands_inherit_fed_stdin(self, compound: str) -> None:
        """Separators inside every compound retain the input inherited from its pipeline."""
        command = f"cat <<'EOF' | {compound}\ngh pr list\nEOF"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "wrapper",
        [
            "xargs -I{}",
            "xargs -I {}",
            "xargs -J{}",
            "xargs -J {}",
            "xargs -i",
            "xargs -i{}",
            "xargs --replace",
            "xargs --replace={}",
            "parallel",
        ],
    )
    def test_wrapper_replacement_command_reads_stdin(self, wrapper: str) -> None:
        """Both default and explicit wrapper replacement strings make the command operand input source."""
        command = f"cat <<'EOF' | {wrapper} sh -c '{{}}'\ngh pr list\nEOF"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize("option", ["-I slot", "-Jslot", "-islot", "--replace=slot"])
    def test_custom_replacement_inside_command_reads_stdin(self, option: str) -> None:
        """A custom replacement inside a longer command string still inserts executable stdin."""
        command = f"cat <<'EOF' | xargs {option} sh -c 'echo ready; slot'\ngh pr list\nEOF"
        assert _invocations(command, "gh") == [["gh", "pr", "list"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "cat <<'EOF' | if true; then cat; fi; bash\ngh pr list\nEOF",
            "cat <<'EOF' | for item in one; do cat; done; bash\ngh pr list\nEOF",
            "cat <<'EOF' | xargs -I token sh -c 'echo data' token\ngh pr list\nEOF",
        ],
    )
    def test_compound_scope_and_replacement_data_do_not_run_source(self, command: str) -> None:
        """A closed compound and replacement in positional data cannot feed an unrelated shell."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "man bash | col -b | grep -n 'alpha\\|beta' | head -5",
            "brew info bash | grep -E 'x\\|y'",
            "echo bash | sed 's/a\\|b/x/'",
            "which zsh | sed 's/\\/bin//'",
            "apropos zsh | grep 'a\\|b'",
            "ls /bin | grep -c 'sh\\|bash'",
            "bash -s | grep 'a\\|b'",
            "echo 'gh pr create' | cat; bash",
            "echo 'gh pr create' | cat && bash",
            "echo 'gh pr create' | cat || bash",
            "{ echo 'gh pr create' | cat; bash; }",
            "(echo 'gh pr create' | cat; bash)",
            "echo 'gh pr create'; echo \"$(bash)\" | cat",
            "cat <(man bash | grep 'a\\|b')",
        ],
    )
    def test_unfed_shell_candidates_do_not_consume_pipeline_text(self, command: str) -> None:
        """Only fed shell candidates consume printed text; pipeline heads and unrelated lists stay readable."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "wrapper",
        [
            "env -u bash MODE=x",
            "env --unset=bash MODE=x",
            "sudo -u bash",
            "sudo --user=bash",
            "xargs -I bash",
            "xargs --replace=bash",
            "time -f bash",
            "nohup",
            "command -p",
            "exec -a bash",
            "nice -n 5",
            "ssh -l bash host",
            "env -i sudo -u bash nohup",
        ],
    )
    def test_wrappers_select_their_program_not_shell_named_data(self, wrapper: str) -> None:
        """Modeled wrapper values and an unfed program's arguments do not consume pipeline text."""
        command = f"{wrapper} man zsh | grep 'a\\|b'"
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "wrapper",
        [
            "env -u bash MODE=x",
            "sudo -u bash",
            "xargs -n 1",
            "xargs --eof",
            "xargs --replace",
            "xargs --max-lines",
            "time -f bash",
            "nohup",
            "command -p",
            "exec -a bash",
            "nice -n 5",
            "ssh -l bash host",
            "env -i sudo -u bash nohup",
            "sudo --unmodeled-option",
        ],
    )
    def test_wrapped_shells_keep_pipeline_and_heredoc_source(self, wrapper: str) -> None:
        """A wrapper still exposes the actual shell's input, conservatively including unmodeled options."""
        for command in (
            f"echo 'gh pr create' | {wrapper} sh",
            f"{wrapper} sh <<'TEXT'\ngh pr create\nTEXT",
            f"cat > helper.sh <<'TEXT'\ngh pr create\nTEXT\n{wrapper} sh helper.sh",
        ):
            assert ["gh", "pr", "create"] in _invocations(command, "gh")
        decoded = f"printf 'gh pr create\\n' | {wrapper} sh"
        assert ["gh", "pr", "create"] in _invocations(decoded, "gh")
        assert _unreadable(decoded) is False

    @pytest.mark.parametrize(
        "command",
        [
            'bash helper.sh "fix(parser): read input"',
            'env MODE=test python3 -c "print(2)"',
            'env MODE=test python -W ignore -c "print(2)"',
            "cat > helper.sh <<'TEXT'\n#!/bin/bash\necho \"(data)\"\nTEXT\nbash helper.sh",
            "cat > notes.txt <<'TEXT'\necho (data)\nTEXT\nbash helper.sh",
            "# note\nls",
            "cat <<'TEXT'\necho (data)\nTEXT\nls; bash --version",
            'for tag in a b; do [[ "$tag" =~ ^r[0-9]+(\\.[0-9]+)+$ ]]; done',
            "time (ls | head -2)",
            "time -p (ls)",
            "helper(){ (ls); }; helper",
            "ls; bash --version",
            "ls; bash --help",
            "setopt SH_WORD_SPLIT; ls",
            "unsetopt shwordsplit; ls",
            'bash helper.sh -c "fix(parser): still data"',
            'bash helper.sh bash -c "fix(parser): still data"',
            "bash --version <<'TEXT'\necho (data)\nTEXT",
            "cat <<'TEXT'\ngh pr create; echo (data)\nTEXT\nbash",
            "cat <<'TEXT'\ngh pr create; echo (data)\nTEXT\necho ok | sh",
            "echo 'fix(parser): data'; bash",
            "cat <<'TEXT' | cat\ngh pr create; echo (data)\nTEXT\npython3 helper.py",
        ],
    )
    def test_data_arguments_and_grouping_remain_readable(self, command: str) -> None:
        """Only executable input is shell source; script operands and unrelated heredocs remain data."""
        assert _invocations(command, "gh") == []
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "env MODE=test bash -c 'gh pr create'",
            "bash -c 'gh pr create' name 'fix(parser): data'",
            "env -S \"bash -c 'gh pr create'\"",
            "eval 'gh' 'pr create'",
            "cat <<'TEXT' | sh\ngh pr create\nTEXT",
            "sh <<'TEXT'\ngh pr create\nTEXT",
            "echo 'gh pr create' | env MODE=test sh",
            "echo 'gh pr create' | timeout 60 bash",
            "echo 'gh pr create' | stdbuf -oL bash",
            "echo 'gh pr create' | doas bash",
            "echo 'gh pr create' | setsid bash",
            "echo 'gh pr create' | flock /tmp/lock bash",
            "echo 'gh pr create' | caffeinate -i bash",
            "echo 'gh pr create' | ionice -c3 sh",
            "echo 'gh pr create' | unbuffer -p bash",
            "echo 'gh pr create' | busybox sh",
            "timeout 60 bash <<'EOF'\ngh pr create\nEOF",
            "cat > w.sh <<'EOF'\ngh pr create\nEOF\ntimeout 60 bash w.sh",
            "echo 'gh pr create;' bash | sh",
            "echo 'gh pr create' | { cat; bash; }",
            "echo 'gh pr create' |& { cat; bash; }",
            "echo 'gh pr create' | (cat; bash)",
            "echo 'gh pr create' | { cat; (bash); }",
            "echo 'gh pr create' | echo $(cat; bash)",
            "echo 'gh pr create' | echo \"$(cat; bash)\"",
            "echo 'gh pr create' | echo \"`cat; bash`\"",
            "echo 'gh pr create' > >(cat; bash)",
            "echo 'gh pr create' |\nbash",
            "echo 'gh pr create' |\n# pipeline continues\nbash",
            "{ cat; bash; } <<'TEXT'\ngh pr create\nTEXT",
            "(cat; bash) <<< 'gh pr create'",
            "echo 'gh pr create' | { echo }; bash; }",
            'bash helper.sh "$(gh pr create)"',
            'bash helper.sh "`gh pr create`"',
            "time (gh pr create)",
            "for tag in a; do [[ x =~ (x) ]]; gh pr create; done",
            "bash --norc -o pipefail -lc 'gh pr create' name 'fix(parser): data'",
            "env --split-string=\"bash -c 'gh pr create'\"",
            "env -Sbash -c 'gh pr create'",
            "(echo 'gh pr create'; echo ok) | sh",
            "{ echo 'gh pr create'; } | sh",
            "sh < <(echo 'gh pr create')",
            "source <(echo 'gh pr create')",
            "{ . /dev/stdin; } <<< 'gh pr create'",
            "bash -c -e 'gh pr create'",
            "bash -c -x -o pipefail -- 'gh pr create'",
            "zsh -c -x 'gh pr create'",
            "cat > helper.sh <<'TEXT'\ngh pr create\nTEXT\nbash helper.sh",
            "cat <<'TEXT' > helper.sh\ngh pr create\nTEXT\n. ./helper.sh",
            "tee scripts/helper.sh <<'TEXT'\ngh pr create\nTEXT\nbash -e scripts/helper.sh",
        ],
    )
    def test_executable_text_still_exposes_writes(self, command: str) -> None:
        """Shell scripts, eval operands, substitutions and shell stdin keep their literal invocation."""
        assert ["gh", "pr", "create"] in _invocations(command, "gh")

    @pytest.mark.parametrize(
        "command",
        [
            "setopt SH_WORD_SPLIT; gh pr view 2",
            "setopt NO_SH_WORD_SPLIT; gh pr view 2",
            "unsetopt SH_WORD_SPLIT; gh pr view 2",
            "unsetopt NO_SH_WORD_SPLIT; gh pr view 2",
            "setopt UNSET; gh pr view 2",
            "setopt NO_UNSET; gh pr view 2",
            "setopt NO_NOMATCH; gh pr view 2",
            "setopt NO_PIPE_FAIL NO_VERBOSE NO_XTRACE; gh pr view 2",
            "echo ${=words}; gh pr view 2",
            "echo ${==words}; gh pr view 2",
            "echo $=words; gh pr view 2",
            'echo "${=words}"; gh pr view 2',
        ],
    )
    def test_runtime_splitting_preserves_literal_invocations(self, command: str) -> None:
        """Field splitting and either sense of reading-neutral options preserve the literal gh argv."""
        assert _invocations(command, "gh") == [["gh", "pr", "view", "2"]]
        assert _unreadable(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "ls name()(e:'gh pr create':)",
            "ls *(e:'gh pr create':)",
            "ls (#qe:'gh pr create':)",
            "setopt globsubst; gh pr view 2",
            "shopt -s extglob; gh pr view 2",
            "echo ${~words}; gh pr view 2",
            "echo ${!words}; gh pr view 2",
            "echo $[1+2]; gh pr view 2",
            "exec {descriptor}>output; gh pr view 2",
        ],
    )
    def test_executable_expansions_remain_unreadable(self, command: str) -> None:
        """Data/source boundaries never admit code-running globs, indirect expansions or syntax-changing options."""
        assert _unreadable(command) is True
        assert _invocations(command, "gh") is None

    @pytest.mark.parametrize(
        "command",
        [
            "timeout 5 zsh -o rcquotes -c 'echo ok'; gh pr view 1",
            "stdbuf -oL zsh -o rcquotes -c 'echo ok'",
            "echo 'gh pr create' | { cat; zsh -o rcquotes; }",
            "{ cat; zsh -o rcquotes; } <<< 'gh pr create'",
            "echo 'gh pr create' | zsh -o rcquotes < script.sh",
        ],
    )
    def test_fed_or_explicit_source_with_unknown_shell_options_is_unreadable(self, command: str) -> None:
        """Modeled wrappers expose their shell command, and fed shells cannot hide option changes."""
        assert _unreadable(command) is True
        assert _invocations(command, "gh") is None

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
            pytest.param(
                "printf 'a\\q' | sh", "gh", "outside the decoded set", id="unsupported-escape-printed-to-a-shell"
            ),
            pytest.param("gh pr {fd}>x create", "gh", "descriptor variable", id="variable-fd"),
            pytest.param("echo ${~x}", "git", "`$` form", id="zsh-glob-sigil-git"),
            pytest.param("shopt -s extglob; git status", "git", "switched at run time", id="runtime-option-git"),
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
        ("command", "words"),
        [
            pytest.param("exit '$(gh pr merge 1)'", ["gh", "pr", "merge", "1"], id="exit-count"),
            pytest.param("shift '$(gh pr merge 1)'", ["gh", "pr", "merge", "1"], id="shift-count"),
            pytest.param("return '$(gh pr merge 1)'", ["gh", "pr", "merge", "1"], id="return-count"),
            pytest.param("break '$(gh pr merge 1)'", ["gh", "pr", "merge", "1"], id="break-count"),
            pytest.param("repeat '$(gh pr merge 1)' true", ["gh", "pr", "merge", "1"], id="repeat-count"),
            pytest.param("shift -p '$(gh pr merge 1)'", ["gh", "pr", "merge", "1"], id="shift-after-option"),
        ],
    )
    def test_count_command_operand_is_arithmetic_and_re_expands(self, command: str, words: list[str]) -> None:
        """A count command evaluates its first operand as arithmetic, so a quoted ``$(gh …)`` in it runs and is found.

        ``exit '$(gh pr merge 1)'`` reads as a benign exit; the quoted substitution the author stored re-expands on
        arithmetic evaluation, running the gh write the guard must see.
        """
        assert words in _invocations(command, "gh")

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("set -E; gh pr view 1", id="set-errtrace-letter"),
            pytest.param("set -eET; gh pr view 1", id="set-trace-cluster"),
            pytest.param("set -o errtrace; gh pr view 1", id="set-o-errtrace"),
            pytest.param("set +o functrace; gh pr view 1", id="set-plus-o-functrace"),
            pytest.param("set -o xtrace -o verbose; gh pr view 1", id="set-o-xtrace-verbose"),
            pytest.param("echo ${X:?missing}; gh pr view 1", id="error-if-unset-colon"),
            pytest.param('echo "${X?needs it}"; gh pr view 1', id="error-if-unset"),
            pytest.param("echo ${X^^} ${X,,}; gh pr view 1", id="case-modification"),
        ],
    )
    def test_reading_neutral_forms_stay_readable(self, command: str) -> None:
        """Trace/verbose options and error-if-unset or case-modification expansions change no reading, so gh stays read.

        They were hard-blocking every ordinary Bash command that used them (round-10 F2); none changes quoting, globbing
        or word splitting, so the lexer reads past them and still finds the gh invocation.
        """
        assert ["gh", "pr", "view", "1"] in _invocations(command, "gh")
        assert _unreadable(command) is False

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
