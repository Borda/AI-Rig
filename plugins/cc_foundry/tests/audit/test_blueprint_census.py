"""Census of the blueprint manifest: no admitted entry belongs to a class the danger gate refuses.

The hook auto-allows a command only when it is an exact manifest hit, so the safety of the auto-allow is a property of
the finite set of entries the generator emits from shipped Markdown. These tests rebuild that set the way
``build_blueprint_manifest.block_entries`` does and check it with detectors that share no judge with the generator, so a
gap in the generator or a newly shipped block fails here: an entry that sources a file or runs a variable's text as a
command (data turned into code), a per-line entry bash cannot parse as a complete command, a block that writes heredoc
data normalization could alias, and a plugin helper whose source deletes trees or writes git history without review.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePath

import pytest

_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_ROOT / "plugins" / "cc_foundry" / "bin"))

import build_blueprint_manifest as bbm  # noqa: E402

#: ``.`` or ``source`` where a command starts: line start, after a separator or ``then``/``do``/``else``.
_SOURCING = re.compile(r"(?:^|[;&|({]|\bthen|\bdo|\belse)\s*(?:\.|source)\s+[\"'$\w/.]", re.MULTILINE)
#: A shell interpreter whose ``-c`` string is nothing but one variable.
_SHELL_STRING_FROM_VARIABLE = re.compile(
    r"\b(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*-\w*c\s+\"?\$\{?[A-Za-z_]\w*\}?\"?(?=[\s;&|)]|$)"
)
#: A heredoc opener that is not a ``<<<`` here-string, with the command that reads it.
_HEREDOC_LINE = re.compile(r"^(?P<command>.*?)(?<!<)<<-?\s*(?P<quote>['\"]?)[A-Za-z_]\w*(?P=quote)", re.MULTILINE)
#: Any heredoc opener on a line, so a second heredoc after a ``python -`` one is counted too.
_HEREDOC_OPENER = re.compile(r"(?<!<)<<(?!<)")
#: A Python script run from a plugin ``bin/`` directory, captured by file name.
_BIN_SCRIPT = re.compile(r"python3?\s+[\"']?[^\s\"']*/bin/([A-Za-z0-9_\-]+\.py)")
#: In a helper's source: a whole-tree delete, or a git subcommand that rewrites history, changes the work tree or
#: deletes a worktree, branch or tag. Reads (``branch --show-current``, ``worktree list``) and tracking-config writes
#: (``branch --set-upstream-to``) are not matched.
_HELPER_MUTATION = re.compile(
    r"shutil\.rmtree"
    r"|[\"'](?:commit|revert|reset|push|rebase|cherry-pick|merge|clean|stash|checkout|switch|am)[\"']"
    r"|[\"']worktree[\"'],\s*[\"'](?:remove|prune)[\"']"
    r"|[\"'](?:branch|tag)[\"'],\s*[\"']-[dD]"
)
#: Helpers the source scan matches on a string that is data, not a call: reviewed, with the reason.
_SCAN_FALSE_POSITIVES = {
    "audit_churn.py": '"revert" is a conventional-commit type in a set of names',
    "detect_codemap.py": '"checkout" is a project-name value in a doctest',
    "gh_batch_query.py": '"commit" is a JSON record key',
    "vitality_extract.py": '"push" is a CI event name',
}
#: A single- or double-quoted string, removed before counting heredoc openers the shell would see.
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_BASH = shutil.which("bash")
_skip_bash_unavailable = pytest.mark.skipif(_BASH is None, reason="bash not on PATH; the parse check needs it")


def _unparsed(texts: list[str]) -> list[str]:
    """Return the texts ``bash -n`` rejects when each stands alone in a subshell.

    One parse covers the whole set; only when it fails is each text parsed alone to name the offenders. ``-n`` reads
    without executing anything.
    """

    def parses(script: str) -> bool:
        result = subprocess.run([_BASH, "-n"], input=script, capture_output=True, text=True, timeout=60, check=False)
        return result.returncode == 0

    if parses("".join(f"(\n{text}\n)\n" for text in texts)):
        return []
    return [text for text in texts if not parses(f"(\n{text}\n)\n")]


def _helper_source(name: str) -> str:
    """Return the shipped source of a plugin ``bin/`` helper, or an empty string when no plugin ships it."""
    paths = sorted(_ROOT.glob(f"plugins/*/bin/{name}"))
    return paths[0].read_text(encoding="utf-8") if paths else ""


def _admitted_entries() -> list[tuple[str, str, str]]:
    """Rebuild every manifest entry as ``(source, kind, text)``, mirroring ``block_entries`` without its digests."""
    entries: list[tuple[str, str, str]] = []
    for plugin in bbm.TARGET_PLUGINS:
        plugin_dir = _ROOT / "plugins" / plugin
        for filepath in bbm.collect_md_files(plugin_dir):
            text = bbm._read_markdown(filepath)
            if text is None:
                continue
            relative = PurePath(filepath).relative_to(PurePath(plugin_dir))
            for block in bbm.parse_blocks(text, filepath):
                normalized = bbm.normalize(block.content) if block.lang_detected == "bash" else ""
                if not normalized or bbm.aliases_escaped_blank(block.content):
                    continue
                source = f"{plugin}/{bbm.posix_src(relative, block.line_start)}"
                found, _ = bbm.block_entries(normalized, source)
                texts = {bbm.sha256_text(normalized): normalized}
                texts.update((bbm.sha256_text(line), line) for line in bbm.split_logical_commands(normalized))
                entries.extend((source, record["kind"], texts[digest]) for digest, record in found.items())
    return entries


@pytest.fixture(scope="module")
def admitted() -> list[tuple[str, str, str]]:
    """Every entry the generator emits for the shipped plugins."""
    return _admitted_entries()


class TestShippedEntries:
    """Each refused class has zero admitted entries across the four plugins."""

    def test_corpus_is_not_empty(self, admitted: list[tuple[str, str, str]]) -> None:
        """The rebuild finds the shipped corpus, so the zero counts below are not vacuous.

        A broken path or an empty plugin list would make every other test in this class pass trivially.
        """
        assert len(admitted) > 1000

    def test_no_entry_sources_a_file(self, admitted: list[tuple[str, str, str]]) -> None:
        """No admitted entry runs a file's content as shell code with ``.`` or ``source``.

        State files hold values earlier steps wrote, sometimes from outside the skill (CI check names, paths); sourcing
        one auto-allowed would execute that data. Consumers read ``KEY=value`` lines instead.
        """
        assert [source for source, _, text in admitted if _SOURCING.search(text)] == []

    def test_no_entry_runs_a_variable_as_shell_code(self, admitted: list[tuple[str, str, str]]) -> None:
        """No admitted entry hands one variable's value to ``sh -c`` / ``bash -c`` as the whole command string.

        The value is read at run time from state or the environment; the digest covers the variable's name only, so an
        auto-allow would approve whatever command the value holds.
        """
        assert [source for source, _, text in admitted if _SHELL_STRING_FROM_VARIABLE.search(text)] == []

    @_skip_bash_unavailable
    def test_every_line_entry_is_a_complete_command(self, admitted: list[tuple[str, str, str]]) -> None:
        """Bash parses every per-line entry on its own (``if …; then``, ``fi`` and ``f() {`` all fail).

        The hook allows a multi-line command when every line is an entry; fragment entries would let lines from
        different blocks be joined into a compound no shipped block contains. Bash itself is the judge here, not the
        generator's own reader, so a gap in that reader cannot hide a fragment.
        """
        lines = sorted({text for _, kind, text in admitted if kind == "command"})
        assert _unparsed(lines) == []

    def test_no_entry_writes_heredoc_data(self, admitted: list[tuple[str, str, str]]) -> None:
        """Every admitted heredoc is Python code read through a quoted delimiter, never data.

        Normalization strips comments and trailing blanks inside heredoc bodies; in data that changes what is written,
        so two different commands would share one digest. In code read by ``python -`` the stripped text is inert.
        """
        data_heredocs = [
            source
            for source, _, text in admitted
            for match in _HEREDOC_LINE.finditer(text)
            if not (match.group("quote") and re.search(r"\bpython3?\s+-(?:\s|$)", match.group("command")))
        ]
        assert data_heredocs == []

    def test_no_line_holds_two_heredocs(self, admitted: list[tuple[str, str, str]]) -> None:
        """No admitted line opens a second heredoc beside a ``python -`` one.

        The code-heredoc exemption reads the line's first command; a data heredoc later on the same line would ride on
        it. A ``<<`` inside a quoted string (a grep pattern) is not an opener the shell sees, so quoted text is removed
        before counting.
        """
        assert [
            source
            for source, _, text in admitted
            for line in text.split("\n")
            if len(_HEREDOC_OPENER.findall(_QUOTED.sub("", line))) > 1
        ] == []

    def test_mutating_helpers_are_reviewed(self, admitted: list[tuple[str, str, str]]) -> None:
        """Every admitted plugin helper whose source deletes a tree or runs a git write is listed with a reason.

        The danger check reads the command line, not what a script does, and the generator's own net is the helper's
        name. This scan reads the helper's source instead, so a destructive helper with an innocent name still fails
        here until it is reviewed into ``_MUTATING_HELPERS``.
        """
        helpers = {name for _, _, text in admitted for name in _BIN_SCRIPT.findall(text)}
        mutating = {name for name in helpers if _HELPER_MUTATION.search(_helper_source(name))}
        assert mutating - set(bbm._MUTATING_HELPERS) - set(_SCAN_FALSE_POSITIVES) == set()


@pytest.mark.parametrize(
    ("detector", "text"),
    [
        pytest.param(
            _SOURCING,
            '[ -f "${TMPDIR:-/tmp}/oss-review-gate-${CSID}" ] && . "${TMPDIR:-/tmp}/oss-review-gate-${CSID}"',
            id="sourcing-state-file",
        ),
        pytest.param(_SOURCING, 'fi\nsource "$IMPL_DIR/pr-vars.sh"', id="sourcing-after-compound"),
        pytest.param(_SHELL_STRING_FROM_VARIABLE, 'bash -c "$METRIC_CMD"; METRIC_EXIT=$?', id="shell-string-variable"),
        pytest.param(_HEREDOC_LINE, "cat > \"$F\" <<'EOF'\nx\nEOF", id="data-heredoc"),
        pytest.param(_HELPER_MUTATION, 'subprocess.run([git, "reset", "--soft", base_sha])', id="helper-git-reset"),
        pytest.param(_HELPER_MUTATION, "shutil.rmtree(path)", id="helper-tree-delete"),
    ],
)
def test_detectors_recognize_refused_shapes(detector: re.Pattern[str], text: str) -> None:
    """Each census detector matches the shape it exists to find, so a zero count is a real absence.

    These are the shapes the shipped corpus held before the sink fixes: sourced state files and a fortify metric string
    run through ``bash -c``.
    """
    assert detector.search(text) is not None


@pytest.mark.parametrize("name", sorted(bbm._MUTATING_HELPERS))
def test_listed_mutating_helper_exists(name: str) -> None:
    """Each reviewed mutating helper still ships in some plugin's ``bin/`` directory.

    A stale entry would pre-approve whatever future script reuses the name, without the review the list stands for.
    """
    assert sorted(_ROOT.glob(f"plugins/*/bin/{name}")) != []
