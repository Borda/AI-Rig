"""Regression tests for the codemap prose/bash shared by research skills and the scientist agent.

Pinned failure modes:

* Research's gates were an unversioned inline copy: no contract resolution block, no ``cat`` of the shipped contract, no
  version marker, so the provider could move to v3 with research silently frozen at its transcribed v2 text.
* The wrapper preserves the gated ``codemap-py index`` preparation boundary and stays aligned with the provider's
  automatic-default, confirm-first opt-in, and frozen-mode rules.
* ``PROJ=$(basename "$(git rev-parse ...)") || PROJ=$(basename "$PWD")`` never fired its fallback: ``basename ""`` exits
  0, so in a non-git project PROJ was empty and codemap was silently off.
* ``_IDX`` defaulted to a CWD-relative ``.cache/codemap``.
* The retired ``codemap:`` skill prefix.

The Markdown is read line by line (fences located by their marker lines), so re-wrapping prose never breaks a check.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_RESEARCH = _REPO_ROOT / "plugins" / "cc_research"
_GATES = _RESEARCH / "skills" / "_shared" / "codemap-gates.md"
_CONTEXT = _RESEARCH / "skills" / "_shared" / "codemap-context.md"
_SCIENTIST = _RESEARCH / "agents" / "scientist.md"

_BASH_BLOCK_FILES = [_CONTEXT, _SCIENTIST]
_QUERY_TAILS = ("central --top 5", "rdeps pkg.mod --limit 10", "uncovered --top 20 pkg.mod")


def _find_posix_bash() -> str | None:
    """Return a bash that runs POSIX script syntax, if the host provides one."""
    if sys.platform != "win32":
        candidates = ["bash"]
    else:
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles(x86)")]
        candidates = [
            *(str(Path(root) / "Git" / sub / "bash.exe") for root in roots if root for sub in ("bin", "usr/bin")),
            *([shutil.which("bash")] if shutil.which("bash") else []),
        ]
    for candidate in candidates:
        try:
            probe = subprocess.run([candidate, "-c", "printf ok"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


_POSIX_BASH = _find_posix_bash()
_skip_no_posix_bash = pytest.mark.skipif(_POSIX_BASH is None, reason="requires a working POSIX bash")


@functools.cache
def _bash_blocks(path: Path) -> tuple[str, ...]:
    """Return the bodies of every ```bash fenced block in *path*, found by scanning fence marker lines.

    Examples:
        >>> path = getfixture("tmp_path") / "guide.md"
        >>> _ = path.write_text("```bash\\necho ready\\n```\\n")
        >>> _bash_blocks(path)
        ('echo ready\\n',)
    """
    blocks: list[str] = []
    body: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if body is None:
            body = [] if line.strip() == "```bash" else None
        elif line.strip() == "```":
            blocks.append("".join(f"{row}\n" for row in body))
            body = None
        else:
            body.append(line)
    return tuple(blocks)


def _shell_function(block: str, name: str) -> str:
    """Return the indented shell function ``name`` (opening line through the matching ``    }`` line) from *block*."""
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"    {name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "    }")
    return "\n".join(lines[start : end + 1])


def _missing(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Return the *phrases* absent from *text*, so a failure names exactly what is missing."""
    return [phrase for phrase in phrases if phrase not in text]


def _effective_text(path: Path) -> str:
    """Return *path*'s text, plus the context contract when the file defers to it at runtime.

    ``run``/``verify`` no longer duplicate the reuse-gate prose inline — they ``cat`` and reference ``codemap-
    context.md`` — so their effective text includes its contract.
    """
    text = path.read_text(encoding="utf-8")
    if path not in _BASH_BLOCK_FILES and "codemap-context.md" in text:
        text += _CONTEXT.read_text(encoding="utf-8")
    return text


class TestContextProse:
    """What the shared context contract tells research about codemap answers."""

    def test_distinguishes_static_links_from_measured_coverage(self) -> None:
        """Research cannot infer runtime coverage from absent static callers or an empty package.

        The context must separate static test callers from measured line coverage, scope queries to the exact module,
        and treat missing measurements as unknown rather than zero.
        """
        text = _CONTEXT.read_text(encoding="utf-8")
        phrases = ("static test callers", "measured line coverage", "exact module", "unknown, not zero")

        assert "no test coverage" not in text
        assert _missing(text, phrases) == []

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param(p, id=p.relative_to(_RESEARCH).as_posix())
            for p in [*_BASH_BLOCK_FILES, _RESEARCH / "skills/run/SKILL.md", _RESEARCH / "skills/verify/SKILL.md"]
        ],
    )
    def test_allows_source_verification_and_gates_answer_reuse(self, path: Path) -> None:
        """Neither stale answers nor an old legacy flag may prohibit required source reads.

        Each consumer (directly or through the context it loads) must keep the reuse-gate vocabulary: the legacy-flag
        fallback, the ``stale`` marker, source-body reads and valid empty answers.
        """
        phrases = ("only when `query_complete` is absent", "`stale`", "source-body", "valid empty")

        assert _missing(_effective_text(path), phrases) == []


class TestQueryWrapper:
    """The shipped bash query blocks, run against a stub ``codemap-py`` in a real POSIX shell."""

    @pytest.mark.parametrize(
        ("exit_code", "payload", "stdout", "hits", "hint"),
        [
            pytest.param(0, '{"callers": [], "index": {"query_complete": true}}', None, "hits=1", "", id="valid-empty"),
            pytest.param(1, '{"callers": []}', "", "hits=0", "unavailable", id="failed-process-with-output"),
            pytest.param(0, '{"error": "not indexed"}', "", "hits=0", "unavailable", id="structured-error"),
            pytest.param(0, "", "", "hits=0", "unavailable", id="missing-output"),
        ],
    )
    @_skip_no_posix_bash
    def test_preserves_failure_vs_empty_answer(
        self, exit_code: int, payload: str, stdout: str | None, hits: str, hint: str, tmp_path: Path
    ) -> None:
        """A valid empty answer is kept while a failed, errored or empty reply is reported unavailable.

        The shipped ``_cq`` function runs against a stub CLI boundary; only a successful, well-formed response counts as
        a hit, so an empty list is an answer and a crashed query is not.
        """
        block = next(b for b in _bash_blocks(_CONTEXT) if "_cq()" in b)
        script = (
            'codemap-py() { printf "%s" "$PROBE_PAYLOAD"; return "$PROBE_EXIT"; }\n'
            "_CM_N=0 _CM_H=0\n" + _shell_function(block, "_cq") + "\n_cq rdeps pkg.mod\n"
            'printf "hits=%s" "$_CM_H" >&2\n'
        )

        result = subprocess.run(
            [_POSIX_BASH, "-c", script],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=os.environ | {"PROBE_PAYLOAD": payload, "PROBE_EXIT": str(exit_code)},
            check=True,
        )

        assert result.stdout.strip() == (payload if stdout is None else stdout)
        assert hits in result.stderr
        assert hint in result.stderr

    @_skip_no_posix_bash
    @pytest.mark.parametrize(
        ("enabled", "currency", "no_heal", "query_count"),
        [
            pytest.param("true", "stale", True, 3, id="continue-stale-persists-into-later-query"),
            pytest.param("true", "current", False, 3, id="fresh-index-allows-normal-query"),
            pytest.param("false", "stale", False, 0, id="persisted-skip-blocks-query-before-preflight"),
        ],
    )
    def test_reloads_consumer_sentinels_before_query(
        self, tmp_path: Path, enabled: str, currency: str, no_heal: bool, query_count: int
    ) -> None:
        """The shared query block obeys the persisted enabled/currency state on a later shell call.

        Sentinels written by an earlier call decide whether queries run at all and whether they pass ``--no-heal``; the
        block is run against a stub CLI that records each query it receives.
        """
        block = next(b for b in _bash_blocks(_CONTEXT) if "CODEMAP_SENTINEL_SLUG" in b)
        temp_dir = tmp_path / "tmp"
        temp_dir.mkdir()
        (temp_dir / "research-run-codemap-enabled-session").write_text(f"{enabled}\n", encoding="utf-8", newline="\n")
        (temp_dir / "research-codemap-currency-session").write_text(f"{currency}\n", encoding="utf-8", newline="\n")
        trace = tmp_path / "queries.txt"
        stub = (
            'codemap-py() { [ "$1" = query ] || return 0; shift; '
            'printf "%s\\n" "$*" >> "$TRACE"; printf \'%s\\n\' \'{"query_complete":true}\'; }\n'
        )
        env = os.environ | {
            "CSID": "session",
            "CODEMAP_SENTINEL_SLUG": "research-run",
            "TMPDIR": temp_dir.as_posix(),
            "TRACE": trace.as_posix(),
            "TARGET_MODULE": "pkg.mod",
            "TARGET_FN": "",
            "SCAN_NO_AUTOBUILD": "0",
        }

        subprocess.run([_POSIX_BASH, "-c", stub + block], cwd=tmp_path, env=env, check=True, capture_output=True)

        queries = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
        assert len(queries) == query_count
        assert all(("--no-heal" in query) is no_heal for query in queries)
        assert all(query.endswith(tail) for query, tail in zip(queries, _QUERY_TAILS))


class TestGatesWrapper:
    """``codemap-gates.md`` is a thin versioned wrapper over the provider's shipped contract."""

    def test_resolves_and_reads_shipped_contract(self) -> None:
        """An inline transcription cannot replace the contract read from the active codemap-py install.

        Optional-provider exception: the gates have no value without codemap-py, so the wrapper reads the installed
        provider's file through its own resolver copy (registry tier first) instead of a frozen local copy — and never
        globs the cache itself.
        """
        text = _GATES.read_text(encoding="utf-8")

        assert (
            '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared_path.py" codemap-py claude-skills/_shared'
            in text
        ), "provider contract is not resolved from the active install"
        assert 'cat "$_CODEMAP_SHARED/codemap-gates.md"' in text, "contract is never loaded"
        assert "codemap-py--" not in text, "manifested copy reintroduced"
        assert "plugins/cache" not in text, "newest-version cache glob reintroduced"
        assert "Contract (version as loaded)" in text, "no contract marker"
        assert "Fallback when codemap-py plugin absent" in text, "no graceful degradation"

    def test_does_not_transcribe_contract_body(self) -> None:
        """Re-listing the gate options inline is exactly the drift the wrapper removes."""
        text = _GATES.read_text(encoding="utf-8")

        assert "No codemap index for this project" not in text
        assert "Continue with stale data" not in text

    def test_preserves_provider_preparation_contract(self) -> None:
        """The wrapper states default preparation, opt-in confirmation, and the gated launcher boundary.

        It must also carry none of the retired wording (a forced override or the old ``scan-index`` alias).
        """
        text = _GATES.read_text(encoding="utf-8")
        required = (
            "Gate A prepares automatically by default",
            "`LAZY_CODEMAP` is the explicit confirm-first preference",
            "Inherited `SCAN_NO_AUTOBUILD=1` freezes both gates",
            "contract's own build/rebuild action is the gated `codemap-py index` launcher",
        )

        assert _missing(text, required) == []
        assert "apply verbatim, with one override" not in text, "the contract no longer disagrees"
        assert "scan-index" not in text, "the retired alias has no reason to appear in a wrapper"


class TestShippedBash:
    """The bash blocks embedded in the shared context and the scientist agent."""

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param(_GATES, id="gates"),
            pytest.param(_CONTEXT, id="context"),
            pytest.param(_SCIENTIST, id="scientist"),
        ],
    )
    def test_no_retired_codemap_skill_prefix(self, path: Path) -> None:
        """The plugin is `codemap-py:`; the bare `codemap:<skill>` prefix is retired.

        The skill name must follow the colon immediately, so a prose colon is not flagged.
        """
        text = path.read_text(encoding="utf-8")

        assert not re.search(r"(?<![\w-])codemap:[a-z]", text), f"retired `codemap:<skill>` prefix in {path.name}"

    @pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in _BASH_BLOCK_FILES])
    def test_index_dir_is_root_anchored(self, path: Path) -> None:
        """A CWD-relative default would report no_index whenever the session sits in a subdirectory.

        Every block that sets ``_IDX`` must anchor its default at the git root, never at the working directory.
        """
        blocks = [b for b in _bash_blocks(path) if "_IDX=" in b]

        assert blocks, f"no _IDX block found in {path.name}"
        assert all("${CODEMAP_INDEX_DIR:-.cache/codemap}" not in b for b in blocks), "CWD-relative default"
        assert all("${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}" in b for b in blocks)

    @pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in _BASH_BLOCK_FILES])
    @_skip_no_posix_bash
    def test_project_name_fallback_fires_outside_git_repository(self, path: Path, tmp_path: Path) -> None:
        """The project-name fallback yields the directory name when no git repository exists.

        ``basename ""`` exits 0, so the old ``||`` fallback was unreachable and ``PROJ`` went empty.
        """
        block = next(b for b in _bash_blocks(path) if "_ROOT=" in b)
        snippet = "\n".join(
            line for line in block.splitlines() if line.strip().startswith(("_ROOT=", '[ -n "$_ROOT"', "PROJ="))
        )
        assert snippet.strip(), f"no root/PROJ derivation found in {path.name}"
        workdir = tmp_path / "loose project"
        workdir.mkdir()

        result = subprocess.run(
            [_POSIX_BASH, "-c", f'{snippet}\nprintf "%s" "$PROJ"'],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            # A stray parent repo would mask the non-git path this test exists to exercise. The ceiling does that; the
            # environment is otherwise inherited, because replacing it with a POSIX PATH literal also dropped
            # SystemRoot, COMSPEC and the rest of the set a Windows child needs before it can start at all.
            env=os.environ | {"GIT_CEILING_DIRECTORIES": str(tmp_path)},
            check=True,
        )

        assert result.stdout == "loose project", f"{path.name}: PROJ was {result.stdout!r}"
