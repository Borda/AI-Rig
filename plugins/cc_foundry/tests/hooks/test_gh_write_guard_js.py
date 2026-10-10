"""Subprocess tests for ``hooks/gh-write-guard.js``, the hard deny for GitHub writes made through ``gh``.

The shipped allow lists pre-approve ``Bash(gh api repos/*)`` and ``Bash(gh api graphql:*)``, which also match writes
(``gh api repos/o/r/issues -f title=x`` is a POST, a GraphQL query can be a mutation). The guard exits 2 on every gh
write, which Claude Code applies before any allow rule. Contracts tested here:

* **Writes blocked** — every known write subcommand, however the shell spells it: behind flags, aliases, grouping,
  ``bash -c``, ``eval``, a here-string, quoting or interpreter code.
* **Shell syntax only where unquoted** — a quoted ``>``/``<``/``&`` stays an argument; an unquoted redirection is
  dropped with its target, wherever it sits.
* **``gh api`` fails closed** — an explicit method, ``--input``, a method-override or run-time header name, REST
  fields, a GraphQL mutation, a query file or a query the command text does not show, an unknown option; flags
  placed before the ``api`` word count as gh counts them.
* **Every documented alias** of the gh 2.102 reference (committed as ``tests/fixtures/gh_reference_aliases.json``)
  reads as the command it names.
* **Reads pass** — ``pr view/diff/checks/list``, ``issue view/list``, ``gh api`` GET with ``--paginate``, GraphQL
  queries (GraphQL ``$variables`` included), ``auth status``, ``pr checkout``; unknown subcommands reach the normal
  permission prompt.
* **Fail closed without an exact argv** (quoting the lexer cannot follow, gh in interpreter code) **or without the
  lexer**, and **shipped in every hook plugin** with its lexer, registered on Bash.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parents[3]
GUARDED_PLUGINS = ["cc_foundry", "cc_oss", "cc_develop", "cc_research"]
GH_REFERENCE_ALIASES = PLUGINS_DIR / "cc_foundry" / "tests" / "fixtures" / "gh_reference_aliases.json"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to run the hook")


def _bash(command: object) -> dict:
    """Build a ``PreToolUse(Bash)`` payload.

    Examples:
        >>> _bash("gh pr view 1")["tool_input"]["command"]
        'gh pr view 1'
    """
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}


def _alias_cases() -> list:
    """Pair every alias spelling in the committed gh reference extract with the canonical command it names.

    A command alias (``repo autolink new``) pairs with its command. A group alias (``agents``) pairs with every command
    under its group, so ``agents create`` sits beside ``agent-task create``. The fixture holds every command heading of
    ``gh help reference`` with its ``Aliases`` entries; its ``source`` field names the gh version.
    """
    commands = json.loads(GH_REFERENCE_ALIASES.read_text(encoding="utf-8"))["commands"]
    cases = []
    for command in commands:
        canonical = command["path"]
        members = [path for path in (entry["path"] for entry in commands) if path.startswith(f"{canonical} ")]
        for alias in command["aliases"]:
            if " " in canonical:
                cases.append(pytest.param(alias, canonical, id=alias))
            else:
                cases.extend(
                    pytest.param(alias + member[len(canonical) :], member, id=alias + member[len(canonical) :])
                    for member in members
                )
    return cases


@_skip_node_unavailable
class TestWritesBlocked:
    """Every gh subcommand that writes to GitHub exits 2, whatever wraps or spells it."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr create -t x -b y", id="pr-create"),
            pytest.param("gh pr new -t x", id="pr-new-alias"),
            pytest.param("gh pr merge 1 --squash", id="pr-merge"),
            pytest.param("gh pr review 1 --approve", id="pr-review"),
            pytest.param("gh pr update-branch 1", id="pr-update-branch"),
            pytest.param("gh issue comment 3 -b x", id="issue-comment"),
            pytest.param("gh issue develop 3", id="issue-develop"),
            pytest.param("gh release upload v1 a.tgz", id="release-upload"),
            pytest.param("gh release delete-asset v1 a.tgz", id="release-delete-asset"),
            pytest.param("gh gist rename 1 a b", id="gist-rename"),
            pytest.param("gh repo sync --force", id="repo-sync-force"),
            pytest.param("gh repo edit --visibility public", id="repo-edit"),
            pytest.param("gh repo rename x", id="repo-rename"),
            pytest.param("gh repo delete o/r --yes", id="repo-delete"),
            pytest.param("gh repo deploy-key add k.pub", id="repo-deploy-key-add"),
            pytest.param("gh repo autolink create JIRA- https://x/", id="repo-autolink-create"),
            pytest.param("gh run rerun 1", id="run-rerun"),
            pytest.param("gh run cancel 1", id="run-cancel"),
            pytest.param("gh workflow run ci.yml", id="workflow-run"),
            pytest.param("gh workflow enable ci.yml", id="workflow-enable"),
            pytest.param("gh workflow disable ci.yml", id="workflow-disable"),
            pytest.param("gh label create bug", id="label-create"),
            pytest.param("gh label clone o/other", id="label-clone"),
            pytest.param("gh secret set TOKEN", id="secret-set"),
            pytest.param("gh secret remove TOKEN", id="secret-remove-alias"),
            pytest.param("gh variable set X --body 1", id="variable-set"),
            pytest.param("gh cache delete --all", id="cache-delete"),
            pytest.param("gh cs delete -c x", id="codespace-group-alias"),
            pytest.param("gh project item-add 1 --url u", id="project-item-add"),
            pytest.param("gh discussion comment 5 -b x", id="discussion-comment"),
            pytest.param("gh ssh-key add k.pub", id="ssh-key-add"),
        ],
    )
    def test_write_subcommand_is_blocked(self, run_hook, command: str) -> None:
        """Each command changes state on GitHub: a hard deny with the subcommand named in the reason."""
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"
        assert "gh write blocked" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr -R o/r create", id="repo-flag-before-verb"),
            pytest.param("gh -R o/r pr create", id="repo-flag-before-group"),
            pytest.param("gh pr --repo=o/r create", id="joined-repo-flag"),
            pytest.param("gh pr --web create", id="unknown-flag-before-verb"),
            pytest.param("bash -c 'gh pr create -t x'", id="bash-c"),
            pytest.param("eval 'gh issue close 3'", id="eval"),
            pytest.param("cd /x && { GH.EXE pr merge 1; }", id="grouped-case-and-exe"),
            pytest.param("bash <<< 'gh release create v1'", id="here-string"),
            pytest.param('gh "pr" "comment" 1 -b x', id="quoted-words"),
            pytest.param("python3 -c \"import os; os.system('gh pr comment 1 -b x')\"", id="interpreter-code"),
            pytest.param("echo 1 | xargs gh issue close", id="xargs"),
            pytest.param("gh api $'repos/o/r/issues' -f title=x", id="ansi-c-endpoint"),
            pytest.param("$'gh' api repos/o/r/issues -f title=x", id="ansi-c-program"),
            pytest.param("gh pr $'create' --title x --body y", id="ansi-c-verb"),
            pytest.param("gh api -X $'POST' repos/o/r/issues", id="ansi-c-method"),
            pytest.param('gh api repos/o/r/issues -f "title=`echo x`"', id="backticks-in-a-field"),
            pytest.param("g''h api repos/o/r/issues -f title=x", id="split-quotes-program"),
            pytest.param('gh pr cr""eate --title x --body y', id="split-quotes-verb"),
            pytest.param("\\gh pr create --title x --body y", id="backslash-program"),
            pytest.param("/opt/homebrew/bin/gh pr create --title x", id="absolute-path-program"),
            pytest.param("GH pr create --title x --body y", id="upper-case-program"),
            pytest.param("command gh pr create --title x", id="command-builtin"),
            pytest.param("env GH_HOST=github.com gh pr create --title x", id="env-prefix"),
            pytest.param("exec gh pr merge 1", id="exec-builtin"),
            pytest.param("sh -c 'gh pr merge 1'", id="sh-c"),
            pytest.param(">out gh pr merge 1", id="redirection-before-program"),
            pytest.param("gh pr 2>&1 merge 1", id="fd-duplication-between-words"),
            pytest.param("gh pr merge>/dev/null 1", id="redirection-glued-to-verb"),
            pytest.param('gh api repos/o/r/issues --input - <<EOF\n{"title":"x"}\nEOF', id="heredoc-input"),
        ],
    )
    def test_spelling_cannot_hide_a_write(self, run_hook, command: str) -> None:
        """Flags, wrappers, quoting and nested shell source are resolved before the subcommand is matched."""
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param('gh pr $"create" --title x --body y', id="locale-string-verb"),
            pytest.param("gh pr $'cr\\x65ate' -t x", id="ansi-c-hex-escape-verb"),
            pytest.param("gh pr $'\\143reate' -t x", id="ansi-c-octal-escape-verb"),
            pytest.param("gh pr $'\\u0063reate' -t x", id="ansi-c-unicode-escape-verb"),
            pytest.param("$'\\x67h' pr create -t x", id="ansi-c-escape-program"),
            pytest.param('gh pr "`echo create`" --title x', id="backticks-as-verb"),
            pytest.param('gh pr "$(echo merge)" 1', id="substitution-as-verb"),
            pytest.param('gh pr "$VERB" 1', id="variable-as-verb"),
            pytest.param('gh repo autolink "$X" a b', id="variable-as-second-verb-word"),
            pytest.param('gh "$G" create', id="variable-as-group"),
            pytest.param("gh pr $'\\x{63}reate' -t x", id="hex-brace-escape-verb"),
            pytest.param("$'\\x{67}h' pr create -t x", id="hex-brace-escape-program"),
            pytest.param("x=$'\\c\\\\'; gh pr create # '", id="control-escape-before-a-write"),
            pytest.param("cat <<$'EOF'\nhi\nEOF\ngh pr merge 1", id="ansi-c-heredoc-delimiter"),
            pytest.param("gh pr $'\\qmerge' 1", id="unknown-escape-verb"),
            pytest.param("x=$$'\\'; gh pr create #'", id="dollars-before-a-quote"),
            pytest.param('x="${y:-\'"\'}"; gh pr create # "\'', id="single-quote-in-double-quoted-expansion"),
            pytest.param('x="${a%"b"}"; gh pr merge 1', id="write-after-nested-expansion-quotes"),
            pytest.param('x="$${y:-"}"; gh pr create # "', id="dollars-before-a-brace-in-double-quotes"),
            pytest.param('x="${y:-\\}\'"\'}"; gh pr create # "\'', id="escaped-brace-then-single-quote"),
            pytest.param('x="${ gh pr create; }"', id="bash-5-3-command-brace"),
            pytest.param('x="${|gh pr create;}"', id="bash-5-3-value-brace"),
            pytest.param("cat <<EOF\n${ gh pr create; }\nEOF", id="bash-5-3-command-brace-in-heredoc"),
            pytest.param('gh pr view "${ gh pr create --title t --body b; }"', id="bash-5-3-command-brace-in-a-read"),
            pytest.param(
                'gh api "repos/o/r/pulls/${ gh pr create --title t --body b; }"',
                id="bash-5-3-command-brace-in-an-endpoint",
            ),
            pytest.param("cat <<EOF\n$$$(gh pr create)\nEOF", id="pid-then-substitution-in-heredoc"),
            pytest.param(
                "gh pr view 1 <<EO\\\nF\nx\nEOF\ngh pr create --title t --body b", id="continued-heredoc-delimiter"
            ),
            pytest.param("gh pr view 1 $\\\n'\\''; gh pr create #'", id="continued-ansi-c-string"),
            pytest.param(
                "gh pr view 1 <<EOF\n$\\\n(gh pr create --title t --body b)\nEOF", id="continued-substitution"
            ),
            pytest.param('gh api "repos/o/r/pulls/$\\\n(gh pr create)"', id="continued-substitution-in-an-endpoint"),
            pytest.param('x="${\\\n gh pr create; }"', id="continued-command-brace"),
            pytest.param(
                'gh pr view "`echo \\"\'\\"; gh pr create --title t --body b; \\"\'\\"`"',
                id="escaped-double-quote-in-backticks",
            ),
            pytest.param("echo hi # note \\\ngh pr create", id="comment-does-not-continue"),
        ],
    )
    def test_encoded_or_run_time_subcommand_is_blocked(self, run_hook, command: str) -> None:
        """An escaped subcommand decodes where bash and zsh agree; quoting they disagree on, or a run-time verb, blocks.

        ``$'\\x63reate'`` is ``create`` to both shells, so it reads as the write it spells. ``$"create"``, ``\\u``,
        ``\\x{…}``, ``\\c``, unknown escapes and such heredoc delimiters decode differently between the shells, so the
        guard has no exact argv and blocks, gh word or not. A variable or substitution where a write verb could stand
        hides the subcommand, so the guard cannot show it is a read.
        """
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("echo pr create --title t --body b | xargs -n9 gh", id="xargs-supplies-group-and-verb"),
            pytest.param("echo create --title t --body b | xargs gh pr", id="xargs-supplies-the-verb"),
            pytest.param("echo create | xargs -I{} gh pr {} --title t --body b", id="xargs-replacement-as-verb"),
            pytest.param("echo repos/o/r/issues -f title=x | xargs gh api", id="xargs-supplies-api-fields"),
            pytest.param("echo 'gh pr create --title t --body b' | sh", id="echo-piped-to-sh"),
            pytest.param("printf '%s\\n' 'gh pr create --title t --body b' | bash", id="printf-escapes-piped-to-bash"),
            pytest.param("echo 'gh pr create --title t --body b' | bash -s", id="piped-to-bash-s"),
            pytest.param("sh < <(echo 'gh pr create --title t --body b')", id="process-substitution-as-stdin"),
            pytest.param("source <(echo 'gh pr create --title t --body b')", id="source-process-substitution"),
            pytest.param("echo 'gh pr create --title t --body b' | xargs -0 sh -c", id="xargs-supplies-sh-c-string"),
            pytest.param("printf -v $'a[\\x24(gh pr create --title t --body b)]' x", id="printf-v-subscript"),
            pytest.param("read $'a[\\x24(gh pr create --title t --body b)]' <<< x", id="read-subscript"),
            pytest.param("let 'a[$(gh pr create --title t --body b)]=1'", id="let-subscript"),
            pytest.param("declare 'a[$(gh pr create --title t --body b)]=1'", id="declare-subscript"),
            pytest.param("test -v 'a[$(gh pr create --title t --body b)]'", id="test-v-subscript"),
            pytest.param("trap 'gh pr create --title t --body b' EXIT", id="trap-handler"),
            pytest.param("source /dev/stdin <<< 'gh pr create --title t --body b'", id="source-stdin-here-string"),
            pytest.param(". /dev/stdin <<'EOF'\ngh pr create --title t --body b\nEOF", id="dot-stdin-heredoc"),
            pytest.param("cat <<EOF\nEO\\\nF\ngh pr create --title t --body b\nEOF", id="continued-terminator-line"),
            pytest.param("cat <\\\n<EOF\nx'\nEOF\ngh pr create --title t --body b #'", id="continued-heredoc-operator"),
        ],
    )
    def test_write_through_another_command_is_blocked(self, run_hook, command: str) -> None:
        """A write that reaches gh through xargs, a shell reading stdin, a subscript or a trap still blocks.

        Each spelling runs ``gh pr create`` (or a POST) in bash and zsh while gh's own words never appear as one
        literal invocation: xargs appends them, a shell runs what ``echo`` prints, a builtin evaluates a ``$(…)`` in
        an array subscript, ``trap``/``source`` run their argument or stdin, or a backslash-newline joins a heredoc
        line the shells read differently from the raw text.
        """
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"


@_skip_node_unavailable
class TestShellSyntaxOnlyWhereUnquoted:
    """A quoted ``>``, ``<`` or ``&`` is an argument; only an unquoted one is a redirection or a command boundary.

    The lexer decides while it still sees the quoting. Deciding on dequoted words read ``-t '>'`` as a redirection: the
    flag lost its value and swallowed the ``-f`` that makes gh send a POST. Real gh 2.102 sent that POST.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh api repos/o/r/issues -t '>' -f title=x", id="template-quoted-gt"),
            pytest.param("gh api repos/o/r/issues -q '>' -ftitle=x", id="jq-quoted-gt-attached-field"),
            pytest.param("gh api repos/o/r/issues -q '<x' -ftitle=x", id="jq-quoted-lt-prefix"),
            pytest.param("gh api repos/o/r/issues -q '>' -XPOST", id="jq-quoted-gt-then-method"),
            pytest.param("gh api repos/o/r/issues -H '>' -ftitle=x", id="header-quoted-gt"),
            pytest.param("gh api repos/o/r/issues --jq '2>' -f title=x", id="jq-quoted-fd-redirection"),
            pytest.param("gh api repos/o/r/issues -t '<<' -f title=x", id="template-quoted-heredoc-operator"),
            pytest.param("gh api repos/o/r/issues -q '<<<' -f title=x", id="jq-quoted-here-string-operator"),
            pytest.param("gh api repos/o/r/issues -p '>&2' -ftitle=x", id="preview-quoted-dup"),
            pytest.param("gh api repos/o/r/issues -H '>x: 1' -ftitle=x", id="header-quoted-gt-prefix"),
            pytest.param("gh api repos/o/r/issues -q \\> -ftitle=x", id="jq-escaped-gt"),
            pytest.param("gh api graphql -q '>' -fquery=mutation{x}", id="graphql-quoted-gt"),
            pytest.param("gh pr --title '>' create --body y", id="pr-flag-value-quoted-gt"),
            pytest.param("gh issue --title '>' create --body y", id="issue-flag-value-quoted-gt"),
            pytest.param("gh pr -R o/r --title '>' merge 1", id="merge-flag-value-quoted-gt"),
            pytest.param('gh pr --title "<" create --body y', id="double-quoted-lt"),
            pytest.param("true '>'&gh api repos/o/r/issues -f title=x", id="quoted-gt-before-background"),
            pytest.param("echo 'a>'&gh pr create --title t --body b", id="quoted-trailing-gt-before-background"),
            pytest.param('echo "x<"&gh pr merge 1', id="double-quoted-trailing-lt-before-background"),
        ],
    )
    def test_quoted_operator_text_cannot_hide_a_write(self, run_hook, command: str) -> None:
        """Quoted operator text stays the flag value it is, and ``'a>'&`` still ends the command before gh.

        Each row exited 0 when operators were judged on dequoted words; real gh writes for each.
        """
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh 2>/dev/null pr create", id="before-the-group"),
            pytest.param("gh pr 2<<<x create", id="fd-here-string-between-words"),
            pytest.param("gh pr {fd}>x create", id="variable-fd"),
            pytest.param("gh api repos/o/r/issues -q x>/dev/null -ftitle=x", id="ends-a-flag-value"),
        ],
    )
    def test_unquoted_redirection_is_no_argument(self, run_hook, command: str) -> None:
        """Bash removes a redirection and its fd number before gh runs, so it cannot split the subcommand words."""
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr view 1 &>/dev/null", id="both-streams"),
            pytest.param("gh pr view 1 >>log 2>&1 </dev/null", id="append-duplicate-and-input"),
            pytest.param("gh pr list --json number >| out.json", id="clobber"),
            pytest.param("gh api --paginate repos/o/r/pulls >out.json 2>&1", id="attached-target"),
            pytest.param("echo a>&gh pr merge 1", id="gh-as-a-redirection-target"),
        ],
    )
    def test_redirection_around_a_read_passes(self, run_hook, command: str) -> None:
        """Redirections are no endpoints or positionals; ``>&gh`` names a file, so echo runs and gh never does."""
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 0, result.stderr


@_skip_node_unavailable
class TestNoExactArgvFailsClosed:
    """Without gh's exact argv, a command naming gh with ``api`` or a write verb is blocked rather than guessed.

    Quote-stripped or code-split words are not a superset of the argv: ``--title 'a b' create`` read without its
    quoting puts ``b`` where gh reads ``create``. The guard tests the raw text, and the text without quote characters.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh api repos/o/r/issues -q '>' -ftitle=x {1..300}", id="oversized-braces-api"),
            pytest.param("gh pr --title 'a b' create {1..300}", id="oversized-braces-quoted-space"),
            pytest.param("gh pr --title 'a b' create 'x", id="unbalanced-quote-quoted-space"),
            pytest.param('"g"h pr merge 1 \'x', id="unbalanced-quote-quoted-program-name"),
            pytest.param(
                "python3 -c \"import subprocess; subprocess.run(['gh','api','repos/o/r/issues','-q','>','-f','t=x'])\"",
                id="python-argument-list-quoted-gt",
            ),
            pytest.param(
                "python3 -c \"import subprocess; subprocess.run(['gh','pr','--title','a b','create'])\"",
                id="python-argument-list-quoted-space",
            ),
            pytest.param(
                "node -e \"require('child_process').execFileSync('gh', ['pr', '--title', 'a b', 'create'])\"",
                id="node-argument-array",
            ),
        ],
    )
    def test_command_naming_a_write_is_blocked(self, run_hook, command: str) -> None:
        """Each row writes when run; the guard cannot show it does not, so it blocks with the reason named."""
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"
        assert "cannot read this command's gh arguments exactly" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr view 1 'x", id="unbalanced-quote-read"),
            pytest.param("gh pr list {1..300}", id="oversized-braces-read"),
            pytest.param("python3 -c \"import subprocess; subprocess.run(['gh', 'pr', 'view', '1'])\"", id="code-read"),
            pytest.param("python3 -c 'print(1)' && gh issue list --label create", id="code-not-naming-gh"),
        ],
    )
    def test_command_without_a_write_word_passes(self, run_hook, command: str) -> None:
        """No ``api`` and no write verb beside gh passes to normal permission handling.

        Interpreter code that never names gh leaves the rest of the command read exactly, so ``--label create`` stays a
        label value there.
        """
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 0, result.stderr


@_skip_node_unavailable
class TestGhReferenceAliases:
    """Every alias the gh 2.102 reference documents reads exactly as the command it names.

    ``tests/fixtures/gh_reference_aliases.json`` is extracted from ``gh help reference``, so no gh install is needed. An
    alias the guard lacks reads as an unknown subcommand and passes while its canonical spelling blocks.
    """

    @pytest.mark.parametrize(("alias", "canonical"), _alias_cases())
    def test_alias_exits_like_its_command(self, run_hook, alias: str, canonical: str) -> None:
        """A write alias blocks and a read alias passes, exactly as the canonical spelling does."""
        expected = run_hook("gh-write-guard.js", _bash(f"gh {canonical} x")).returncode

        result = run_hook("gh-write-guard.js", _bash(f"gh {alias} x"))

        assert result.returncode == expected, result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh agent create 'fix bug'", id="agent"),
            pytest.param("gh agents create 'fix bug'", id="agents"),
            pytest.param("gh agent-tasks create 'fix bug'", id="agent-tasks"),
            pytest.param("gh skills publish", id="skills"),
            pytest.param("gh repo autolink new JIRA- https://x/", id="repo-autolink-new"),
        ],
    )
    def test_write_alias_is_blocked(self, run_hook, command: str) -> None:
        """The aliases once missing block outright, so the equality test above cannot pass by both sides passing."""
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"


@_skip_node_unavailable
class TestApiFailsClosed:
    """``gh api`` is blocked unless the command text shows a read."""

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            pytest.param("gh api repos/o/r/issues -f title=x", "POST", id="rest-raw-field"),
            pytest.param("gh api repos/o/r/issues -F title=x", "POST", id="rest-typed-field"),
            pytest.param("gh api repos/o/r --raw-field=title=x", "POST", id="rest-long-field-joined"),
            pytest.param("gh api repos/o/r -ftitle=x", "POST", id="rest-field-attached"),
            pytest.param("gh api x -X PATCH", "-X PATCH", id="method-after-endpoint"),
            pytest.param("gh api repos/o/r/pulls -XPOST", "-X POST", id="method-attached"),
            pytest.param("gh api -iXPOST repos/o/r/pulls", "-X POST", id="method-in-cluster"),
            pytest.param("gh api --method=DELETE repos/o/r", "-X DELETE", id="method-long-joined"),
            pytest.param('gh api repos/o/r -X "$M"', "-X $M", id="method-run-time-value"),
            pytest.param("gh api repos/o/r/pulls -X POST", "-X POST", id="method-post"),
            pytest.param("gh api repos/o/r --method=$M -f a=b", "-X $M", id="method-joined-run-time-value"),
            pytest.param('gh api repos/o/r -X "$(echo GET)"', "-X $(echo GET)", id="method-substituted-value"),
            pytest.param("gh api repos/o/r -X GET -X POST -f a=b", "-X POST", id="later-method-wins"),
            pytest.param("gh api repos/o/r -X GET --input f.json", "--input", id="input-under-get"),
            pytest.param("gh api repos/o/r/issues -f title=x 2>/dev/null", "POST", id="fields-with-a-redirection"),
            pytest.param("gh api repos/o/r/rulesets --input file.json", "--input", id="input-body"),
            pytest.param(
                "gh api graphql -f query='mutation{addStar(input:{starrableId:\"x\"}){x}}'",
                "mutation",
                id="graphql-mutation",
            ),
            pytest.param(
                "gh api graphql -f query='query { a } mutation { b }'", "mutation", id="graphql-mutation-later"
            ),
            pytest.param("gh api graphql -F query=@q.graphql", "file", id="graphql-query-file"),
            pytest.param('gh api graphql -f query="$Q"', "query", id="graphql-variable-query"),
            pytest.param('gh api graphql -f query="$(cat q.graphql)"', "does not show", id="graphql-substituted-query"),
            pytest.param("gh api graphql --input body.json", "--input", id="graphql-input"),
            pytest.param("gh api repos/o/r --foo", "--foo", id="unknown-long-option"),
            pytest.param("gh api repos/o/r -Z", "-Z", id="unknown-short-option"),
            pytest.param("gh api a b", "endpoints", id="two-endpoints"),
            pytest.param('bash -c "gh api repos/o/r/issues -f title=x"', "POST", id="nested-shell-source"),
            pytest.param("gh -X POST api repos/o/r/issues", "-X POST", id="method-before-api-word"),
            pytest.param("gh -f title=x api repos/o/r/issues", "POST", id="field-before-api-word"),
            pytest.param("gh --method=POST api repos/o/r/issues", "-X POST", id="joined-method-before-api-word"),
            pytest.param("gh --input x.json api repos/o/r/issues", "--input", id="input-before-api-word"),
            pytest.param(
                "gh api -X GET -H 'X-HTTP-Method-Override: POST' repos/o/r/issues -f title=x",
                "method-override header",
                id="method-override-header-under-get",
            ),
            pytest.param(
                "gh api -X GET -H 'x-http-method-override:DELETE' repos/o/r",
                "method-override header",
                id="method-override-header-lower-case",
            ),
            pytest.param(
                "gh api -X GET --header='X-HTTP-Method: PATCH' repos/o/r",
                "method-override header",
                id="method-header-long-joined",
            ),
            pytest.param(
                "gh api -X GET -H 'X-Method-Override: PUT' repos/o/r",
                "method-override header",
                id="method-override-name",
            ),
            pytest.param(
                'gh api -X GET -H "$H" repos/o/r/issues -f title=x', "header whose name", id="run-time-header-name"
            ),
        ],
    )
    def test_api_write_shape_is_blocked(self, run_hook, command: str, reason: str) -> None:
        """A request that can write, or whose body the command text does not show, is a hard deny."""
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 2, f"{command!r} was not blocked"
        assert reason in result.stderr


@_skip_node_unavailable
class TestReadsPass:
    """Reads, and gh commands the guard does not know, reach normal permission handling (exit 0)."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr view 1", id="pr-view"),
            pytest.param("gh pr diff 1", id="pr-diff"),
            pytest.param("gh pr checks 1", id="pr-checks"),
            pytest.param("gh pr list --json number", id="pr-list"),
            pytest.param("gh issue view 3", id="issue-view"),
            pytest.param("gh issue list --search delete", id="issue-list-searching-a-verb"),
            pytest.param("gh issue list --label create", id="issue-list-label-named-like-a-verb"),
            pytest.param("gh auth status", id="auth-status"),
            pytest.param("gh pr checkout 12", id="pr-checkout"),
            pytest.param("gh co 12", id="checkout-alias"),
            pytest.param("gh release download v1", id="release-download"),
            pytest.param("gh repo autolink list", id="third-level-read"),
            pytest.param("gh api repos/o/r --paginate", id="api-get-paginate"),
            pytest.param("gh api repos/o/r/pulls?state=open --jq '.[].number'", id="api-get-query-string"),
            pytest.param("gh api -H 'Accept: application/vnd.github+json' repos/o/r -i", id="api-get-header"),
            pytest.param("gh api graphql -f query='query { viewer { login } }'", id="graphql-query"),
            pytest.param("gh api graphql -f query='{ viewer { login } }'", id="graphql-shorthand-query"),
            pytest.param(
                "gh api graphql -f query='query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo){id}}'"
                " -f owner=o -F repo=r",
                id="graphql-query-with-graphql-variables",
            ),
            pytest.param("gh api graphql --paginate -f query='query { a }'", id="graphql-paginate"),
            pytest.param(
                'PR_ASSOCIATIONS=$(gh api --method GET --paginate --slurp "repos/{owner}/{repo}/commits/$CANDIDATE_SHA'
                '/pulls" -f per_page=100)',
                id="oss-release-credit-lookup",
            ),
            pytest.param(
                'gh api "search/code" --method GET --field "q=$export language:python" --jq \'.items[:5] | '
                ".[].repository.full_name' 2>/dev/null",
                id="oss-review-downstream-search",
            ),
            pytest.param(
                "UPDATED_AT=$(gh api graphql \\\n"
                "    -f query='query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo)"
                "{discussion(number:$number){updatedAt}}}' \\\n"
                "    -f owner='{owner}' -f repo='{repo}' -F number=$CLEAN_ARGS \\\n"
                "    --jq '.data.repository.discussion.updatedAt' 2>/dev/null)",
                id="oss-analyse-discussion-updated-at",
            ),
            pytest.param("gh api -XGET search/issues -f q=x", id="get-attached"),
            pytest.param(
                'gh api -H "Authorization: Bearer $TOKEN" repos/o/r', id="run-time-value-under-a-visible-name"
            ),
            pytest.param("gh --paginate=true api repos/o/r/pulls", id="read-flag-before-api-word"),
            pytest.param("gh -X GET api repos/o/r/pulls", id="get-before-api-word"),
            pytest.param("gh api repos/o/r --jq .name > out.json 2>&1", id="redirections-are-not-endpoints"),
            pytest.param("gh pr view 1 2> err.log", id="redirection-target-is-not-an-argument"),
            pytest.param("gh api --method=get search/issues -F q=x", id="get-joined-lower-case"),
            pytest.param('gh pr view "$N"', id="variable-after-read-verb"),
            pytest.param('gh repo view "$R" --json name', id="variable-after-third-level-read"),
            pytest.param('gh issue list --label "$L"', id="variable-as-flag-value"),
            pytest.param("gh pr view $(git rev-parse HEAD)", id="substitution-after-read-verb"),
            pytest.param("gh pr diff `cat n`", id="backticks-after-read-verb"),
            pytest.param("gh api $'repos/o/r/issues' --paginate", id="ansi-c-endpoint-read"),
            pytest.param("gh pr list --search $'a\\tb'", id="ansi-c-named-escape-read"),
            pytest.param('gh codespace ports -c "$CS"', id="codespace-flag-value-is-no-verb"),
            pytest.param("python3 -c 'print(1)' 'x$'", id="dollar-before-a-closing-quote-without-gh"),
            pytest.param('_OLD_PREFIX="${_OLD_DIRNAME%"$_OLD_MODULE_DIR"}"', id="codemap-rename-nested-quotes"),
            pytest.param('_esc="${_esc//\\"/\\\\\\"}"', id="foundry-quality-stack-escaped-quotes"),
            pytest.param('echo "pid $$ ${HOME}"', id="pid-in-double-quotes"),
            pytest.param("cat <<EOF\n$$(gh pr create)\nEOF", id="pid-then-text-in-heredoc"),
            pytest.param("cat <<'EO\\\nF'\nx\nEOF\nEO\\\nF", id="continuation-in-a-quoted-delimiter-is-literal"),
            pytest.param("echo 'a$\\\nb'; gh pr view", id="continuation-in-single-quotes-is-literal"),
            pytest.param("echo a\\\\\ngh pr view", id="escaped-backslash-is-no-continuation"),
            pytest.param("echo 1 | xargs gh pr view", id="xargs-appends-to-a-read"),
            pytest.param(
                "gh pr list --json number | jq -r '.[].number' | xargs -n1 gh pr view", id="xargs-read-pipeline"
            ),
            pytest.param("curl -fsSL https://example.invalid/install | sh", id="pipe-to-sh-without-gh"),
            pytest.param("printf -v x '%s' \"$y\"", id="printf-v-plain-name"),
            pytest.param("trap 'rm -f \"$tmpenv\"' EXIT INT TERM", id="oss-resolve-trap"),
            pytest.param("source .venv/bin/activate && gh pr view 1", id="source-a-file"),
            pytest.param(
                "gh api --method GET repos/:owner/:repo/pulls --paginate --field state=all", id="foundry-rule"
            ),
            pytest.param(
                'gh api --method GET "search/code" --field "q=from mypackage import language:python" --jq .items',
                id="oss-analyse-ecosystem-search",
            ),
            pytest.param("gh extension install o/gh-x", id="unknown-group"),
            pytest.param("gh mycustom create", id="user-alias-or-extension"),
            pytest.param("echo 'gh pr create'", id="quoted-text"),
            pytest.param("git log --author gh", id="gh-as-an-argument-word"),
            pytest.param("ls", id="no-gh"),
        ],
    )
    def test_read_is_not_blocked(self, run_hook, command: str) -> None:
        """Static allows keep pre-approving these; the guard adds no prompt and no block."""
        result = run_hook("gh-write-guard.js", _bash(command))

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {}}, id="other-tool"),
            pytest.param(_bash(None), id="no-command"),
            pytest.param(_bash(["gh", "pr", "create"]), id="non-string-command"),
        ],
    )
    def test_payload_without_a_bash_command_passes(self, run_hook, payload: dict) -> None:
        """Nothing to inspect is never a block."""
        result = run_hook("gh-write-guard.js", payload)

        assert result.returncode == 0, result.stderr

    def test_malformed_stdin_passes(self) -> None:
        """A payload that is not JSON leaves the call to normal permission handling."""
        hook = PLUGINS_DIR / "cc_foundry" / "hooks" / "gh-write-guard.js"
        result = subprocess.run(
            ["node", str(hook)], input="not json", capture_output=True, text=True, encoding="utf-8", timeout=15
        )

        assert result.returncode == 0, result.stderr


@_skip_node_unavailable
class TestMissingLibrary:
    """Without ``lib/shell-git.js`` the guard cannot lex, so it blocks every command naming gh with a write word."""

    @pytest.fixture
    def bare_hook(self, tmp_path: Path) -> Path:
        """Copy the guard to a directory with no ``lib/`` beside it, as a broken install would leave it."""
        hook = tmp_path / "bare-hooks" / "gh-write-guard.js"
        hook.parent.mkdir()
        hook.write_bytes((PLUGINS_DIR / "cc_foundry" / "hooks" / "gh-write-guard.js").read_bytes())
        return hook

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("gh pr create -t x", 2, id="write-blocked"),
            pytest.param("gh api repos/o/r", 2, id="any-api-blocked"),
            pytest.param("gh pr view 1", 0, id="read-passes"),
            pytest.param("ls", 0, id="no-gh-passes"),
        ],
    )
    def test_fails_closed(self, bare_hook: Path, command: str, expected: int) -> None:
        """A crash would exit 1, which Claude Code treats as non-blocking; the guard exits 2 for a write instead."""
        result = subprocess.run(
            ["node", str(bare_hook)],
            input=json.dumps(_bash(command)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

        assert result.returncode == expected, result.stderr
        assert ("shell-git.js" in result.stderr) == (expected == 2)


@_skip_node_unavailable
@pytest.mark.parametrize("plugin", GUARDED_PLUGINS)
class TestShippedInEveryHookPlugin:
    """Each plugin that ships the gh allows carries the guard and its lexer, so it holds when installed alone."""

    def test_registered_on_bash(self, plugin: str) -> None:
        """The plugin's hooks.json runs the guard on every Bash PreToolUse call."""
        hooks = json.loads((PLUGINS_DIR / plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        matchers = [
            entry.get("matcher")
            for entry in hooks["PreToolUse"]
            for hook in entry["hooks"]
            if hook["command"].endswith('/hooks/gh-write-guard.js"; done; exit 0')
        ]

        assert matchers == ["Bash"]

    def test_copy_blocks_a_write_from_its_own_tree(self, plugin: str) -> None:
        """The plugin's own copy loads its own lexer: a write through it is a hard deny."""
        hook = PLUGINS_DIR / plugin / "hooks" / "gh-write-guard.js"
        result = subprocess.run(
            ["node", str(hook)],
            input=json.dumps(_bash("gh api repos/o/r/issues -f title=x")),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

        assert result.returncode == 2
        assert "shell-git.js" not in result.stderr
