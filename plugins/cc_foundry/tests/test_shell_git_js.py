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
* **Fail closed** — quoting the lexer cannot follow, an oversized brace expansion, and unknown git global options widen
  the answer, never narrow it.

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


@_skip_node_unavailable
class TestModule:
    """The module loads cleanly and exposes only its one entry point."""

    def test_passes_node_syntax_check(self) -> None:
        """``node --check`` accepts the file, as the pre-commit hook and both requiring hooks need."""
        proc = subprocess.run(
            ["node", "--check", str(LIBRARY)], capture_output=True, text=True, timeout=15, check=False
        )

        assert proc.returncode == 0, proc.stderr

    def test_exports_only_git_subcommand_args(self) -> None:
        """A narrow surface keeps the lexer internals free to change without breaking either hook."""
        proc = _node("process.stdout.write(JSON.stringify(Object.keys(lib)));")

        assert json.loads(proc.stdout) == ["gitSubcommandArgs"]

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
            pytest.param("ls | xargs -I{} git add {}", "add {}", id="xargs-with-options"),
            pytest.param('git -C "dir with space" push', "push", id="quoted-dir-with-space"),
            pytest.param('git -C "a;b" push -f', "push -f", id="quoted-dir-with-operator"),
            pytest.param('"git" commit -m x', "commit -m x", id="quoted-argv0"),
            pytest.param("git push $'--force'", "push --force", id="ansi-c-quoting"),
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
            pytest.param("git push 2>&1 | tee log", "push 2>&1", id="redirection-not-background"),
            pytest.param("BASH -c 'git push -f'", "push -f", id="runner-name-case"),
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
