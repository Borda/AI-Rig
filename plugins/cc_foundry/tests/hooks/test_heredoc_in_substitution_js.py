"""A heredoc inside `$(…)` whose body closes the substitution early in bash 3.2 is unreadable syntax.

macOS /bin/bash 3.2 finds the end of a `$(` by scanning for its `)` before reading the heredoc, so body text after an
unbalanced `)` runs as commands there, while zsh and bash 4+ read the whole body as data. The lexer fails closed on that
shape and keeps reading the ordinary commit-message idiom, balanced parentheses included, as data.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SHELL_GIT = Path(__file__).resolve().parents[2] / "hooks" / "lib" / "shell-git.js"
NODE = shutil.which("node")
_skip_node_unavailable = pytest.mark.skipif(NODE is None, reason="node not available")

PROBE = (
    "const s = require(process.argv[1]); const info = {};"
    "const found = s.programInvocations(process.argv[2], 'git', info) || [];"
    "process.stdout.write(JSON.stringify({unreadable: info.unreadable, reason: info.unreadableReason,"
    " found: found.length}));"
)


def _read(command: str) -> dict:
    """Lex `command` with the git target and return what the lexer reported."""
    proc = subprocess.run(
        [NODE, "-e", PROBE, str(SHELL_GIT), command], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(proc.stdout)


@_skip_node_unavailable
class TestHeredocInSubstitution:
    """Heredocs opened inside `$(…)`."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param('x)"; git push origin main; echo "', id="round-10-shape"),
            pytest.param('x)"\ngit push origin main', id="close-line-then-push"),
            pytest.param("$'\\''\nx)\"; git push origin main; echo \"", id="ansi-c-quote-desync"),
            pytest.param('`\'`\nx)"; git push origin main; echo "', id="backtick-quote-desync"),
            pytest.param("'('\n)", id="quoted-paren-closes-for-bash"),
            pytest.param("Changes:\n1) scan parens", id="list-item-accepted-friction"),
        ],
    )
    def test_body_closing_the_substitution_is_unreadable(self, body: str) -> None:
        """A body bash 3.2 could end the `$(` inside fails closed instead of hiding a command as heredoc data.

        bash 3.2 closes the substitution at the first `)` past depth zero, so the rest of that line, or the next lines,
        run as commands; quoting the lexer and bash 3.2 read differently (`$'`, backticks) fails closed too.
        """
        command = f"echo -m \"$(cat <<'EOF'\n{body}\nEOF\n)\""
        result = _read(command)
        assert result["unreadable"] is True
        assert "heredoc inside" in result["reason"]

    @pytest.mark.parametrize(
        "message",
        [
            pytest.param("fix(scope): add guard", id="balanced-parens"),
            pytest.param("don't stop: (a) and (b)", id="apostrophe-and-pairs"),
            pytest.param("plain message", id="plain"),
        ],
    )
    def test_commit_message_idiom_stays_readable(self, message: str) -> None:
        """The usual `git commit -m "$(cat <<'EOF' … EOF )"` idiom stays readable data.

        Its closing `)` sits on its own line after the delimiter, and balanced pairs in the body never close early.
        """
        command = f"git commit -m \"$(cat <<'EOF'\n{message}\nEOF\n)\""
        result = _read(command)
        assert (result["unreadable"], result["found"] > 0) == (False, True)
