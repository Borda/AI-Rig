"""Regression tests for cc_develop's codemap prose and inline bash.

Pinned failure modes:

* Every hand-copied index-guard block defaulted to a CWD-relative ``.cache/codemap``
  while the provider anchors on the git toplevel, so a skill invoked from a subdirectory
  reported a false ``no_index``. The sweep below is tree-wide on purpose: the guard is
  hand-copied, so a per-file assertion would let the next copy drift in unnoticed.
* The gates wrapper preserves the gated ``codemap-py index`` preparation boundary and stays aligned with the provider's
  automatic-default, confirm-first opt-in, and frozen-mode rules.
* The retired ``codemap:`` skill prefix.

Prose assertions run on whitespace-collapsed text so they hold wherever a paragraph happens to wrap; assertions over
shell lines read the raw text because their spacing is part of what is pinned.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _develop_text import flat, load_module

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEVELOP = _REPO_ROOT / "plugins" / "cc_develop"
_GATES = _DEVELOP / "skills" / "_shared" / "codemap-gates.md"

# The CWD-relative default this finding removes; matched literally so a re-introduction fails.
_CWD_RELATIVE_DEFAULT = "${CODEMAP_INDEX_DIR:-.cache/codemap}"
_INDEX_DIR_DEFAULT_RE = re.compile(r"\$\{CODEMAP_INDEX_DIR:-([^}]*)\}")
_RETIRED_PREFIX_RE = re.compile(r"(?<![\w-])codemap:[a-z]")


def _tracked_text_files(root: Path) -> list[Path]:
    """Return every markdown/python source file under *root*, excluding tests.

    >>> paths = _tracked_text_files(_DEVELOP)
    >>> any(path.name == "codemap-context.md" for path in paths)
    True
    """
    return [
        p
        for p in root.rglob("*")
        if p.suffix in {".md", ".py"} and p.is_file() and "tests" not in p.relative_to(root).parts
    ]


# Read once at import: parametrization below and the tree-wide sweeps share the same immutable snapshot.
_PLUGIN_TEXTS = {path: path.read_text(encoding="utf-8") for path in _tracked_text_files(_DEVELOP)}


def _prose(path: Path) -> str:
    """Return *path*'s text with every whitespace run collapsed to one space, so wrapping never matters."""
    return flat(path.read_text(encoding="utf-8"))


def _files_matching(pattern: re.Pattern[str] | str) -> list[str]:
    """Return repo-relative paths of plugin files whose text matches the compiled pattern or contains the literal."""
    matches = pattern.search if isinstance(pattern, re.Pattern) else pattern.__contains__
    return [path.relative_to(_REPO_ROOT).as_posix() for path, text in _PLUGIN_TEXTS.items() if matches(text)]


class TestBatchChildInterpretation:
    """The review skill and shared context interpret each batch child on its own."""

    @pytest.mark.parametrize("clause", ["`ok: true`", "`result.index`", "`ok: false`", "valid empty"])
    def test_review_names_each_child_clause(self, clause: str) -> None:
        """The review skill spells out how a batch child is read.

        A successful batch process must not turn a failed child into an empty answer, so every clause of the child
        reading rule has to stay in the prompt.
        """
        assert clause in _prose(_DEVELOP / "skills/review/SKILL.md")

    @pytest.mark.parametrize(
        "stale_claim", ["one shared `index` coverage block covers the whole batch", "are tested via mock"]
    )
    def test_review_drops_superseded_claims(self, stale_claim: str) -> None:
        """The review skill no longer claims one shared coverage block or mock-tested children."""
        assert stale_claim not in _prose(_DEVELOP / "skills/review/SKILL.md")

    @pytest.mark.parametrize("relative", ["skills/_shared/codemap-context.md", "skills/review/SKILL.md"])
    @pytest.mark.parametrize(
        "clause",
        [
            "only when `query_complete` is absent",
            "`result.index`",
            "`ok: false`",
            "valid empty",
            "source-body",
            "same project",
            "current index",
            "query and flags",
            "`stale`",
        ],
    )
    def test_consumer_reuses_only_qualified_answers(self, relative: str, clause: str) -> None:
        """Copied prompt contracts keep every clause that stops them accepting failed/stale children.

        Dropping one would let a consumer reuse an answer from another project, index, or query, or forbid the source
        checks a degraded result still needs.
        """
        assert clause in _prose(_DEVELOP / relative)


class TestIndexDirAnchoring:
    """The index lives under the git toplevel; anchoring on the CWD is a false ``no_index``."""

    def test_no_cwd_relative_default_anywhere_in_plugin(self) -> None:
        """No plugin file carries the CWD-relative ``CODEMAP_INDEX_DIR`` default."""
        offenders = _files_matching(_CWD_RELATIVE_DEFAULT)
        assert offenders == [], f"CWD-relative codemap index dir in: {offenders}"

    @pytest.mark.parametrize(
        ("path", "default"),
        [
            pytest.param(path, default, id=f"{path.relative_to(_DEVELOP).as_posix()}:{default}")
            for path, text in _PLUGIN_TEXTS.items()
            for default in _INDEX_DIR_DEFAULT_RE.findall(text)
        ],
    )
    def test_every_override_default_is_root_anchored(self, path: Path, default: str) -> None:
        """Each surviving ``CODEMAP_INDEX_DIR:-`` default names the resolved repository root."""
        assert default.startswith("$_ROOT/"), f"{path.name}: unanchored default {default!r}"


class TestGatesWrapper:
    """The gates wrapper reads the provider's shipped contract instead of transcribing it."""

    @pytest.mark.parametrize(
        "present",
        [
            '"$PLUGIN_ROOT/bin/resolve_shared_path.py" codemap-py claude-skills/_shared',
            'cat "$_CODEMAP_SHARED/codemap-gates.md"',
            "Contract (version as loaded)",
            "Fallback when codemap-py plugin absent",
        ],
    )
    def test_loads_contract_from_active_install(self, present: str) -> None:
        """The wrapper resolves and reads the contract from the active codemap-py install.

        Optional-provider exception: the gates have no value without codemap-py, so the wrapper reads the installed
        provider's file through its own resolver copy (registry tier first) and keeps a fallback line for its absence.
        """
        assert present in _prose(_GATES)

    @pytest.mark.parametrize("absent", ["codemap-py--", "plugins/cache"])
    def test_never_globs_the_cache(self, absent: str) -> None:
        """The wrapper never pins a cache path or globs the plugin cache itself."""
        assert absent not in _prose(_GATES)

    @pytest.mark.parametrize(
        "present",
        [
            "Gate A prepares automatically by default",
            "`LAZY_CODEMAP` is the explicit confirm-first preference",
            "Inherited `SCAN_NO_AUTOBUILD=1` freezes both gates",
            "dev_codemap_gate.py <current-skill> --skip",
            "pass `--no-heal` to every later query",
            "contract's own build/rebuild action is the gated `codemap-py index` launcher",
        ],
    )
    def test_states_provider_preparation_contract(self, present: str) -> None:
        """The wrapper states default preparation, opt-in confirmation, frozen mode and the gated launcher boundary."""
        assert present in _prose(_GATES)

    @pytest.mark.parametrize(
        "absent",
        [
            pytest.param("apply verbatim, with one override", id="override-wording"),
            pytest.param("scan-index", id="retired-alias"),
        ],
    )
    def test_omits_superseded_preparation_wording(self, absent: str) -> None:
        """The contract no longer disagrees with the wrapper, and the retired ``scan-index`` alias is gone."""
        assert absent not in _prose(_GATES)

    def test_reads_the_sentinel_the_resolver_writes(self) -> None:
        """The gates read the currency sentinel under the prefix the resolver writes.

        A prefix mismatch would leave the stale gate permanently blind.
        """
        gate = load_module(_DEVELOP / "bin" / "dev_codemap_gate.py", "dev_gate_sentinel_check")

        assert f"{gate.CURRENCY_PREFIX}-${{CSID}}" in _GATES.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "line",
    [
        'IFS= read -r CODEMAP_ENABLED  < "${TMPDIR:-/tmp}/dev-fix-codemap-enabled-${CSID}"',
        'IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}"',
        "codemap-py query --timeout 5 $_CODEMAP_NO_HEAL fn-rdeps",
    ],
)
def test_fix_later_query_reloads_enabled_and_currency_state(line: str) -> None:
    """The fix skill reloads persisted Gate B choices before the later direct-caller query.

    The enabled and currency sentinels govern both the shared batch and the later query; the exact shell line is pinned,
    spacing included, because the permission blueprint matches it verbatim.
    """
    assert line in (_DEVELOP / "skills" / "fix" / "SKILL.md").read_text(encoding="utf-8")


def test_no_retired_codemap_skill_prefix() -> None:
    """The plugin is ``codemap-py:``; the bare ``codemap:<skill>`` prefix is retired.

    The skill name must follow the colon immediately — a prose colon ("With codemap: effort sizing is structural") and
    the ``# codemap: integrated-via-shared`` marker are not plugin references and must not be flagged.
    """
    offenders = _files_matching(_RETIRED_PREFIX_RE)
    assert offenders == [], f"retired `codemap:<skill>` prefix in: {offenders}"
