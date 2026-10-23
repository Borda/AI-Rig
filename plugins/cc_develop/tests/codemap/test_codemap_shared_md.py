"""Regression tests for cc_develop's codemap prose and inline bash.

Pinned failure modes:

* Every hand-copied index-guard block defaulted to a CWD-relative ``.cache/codemap``
  while the provider anchors on the git toplevel, so a skill invoked from a subdirectory
  reported a false ``no_index``. The sweep below is tree-wide on purpose: the guard is
  hand-copied, so a per-file assertion would let the next copy drift in unnoticed.
* The gates wrapper preserves the gated ``codemap-py index`` preparation boundary and stays aligned with the provider's
  automatic-default, confirm-first opt-in, and frozen-mode rules.
* The retired ``codemap:`` skill prefix.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEVELOP = _REPO_ROOT / "plugins" / "cc_develop"
_GATES = _DEVELOP / "skills" / "_shared" / "codemap-gates.md"

# The CWD-relative default this finding removes; matched literally so a re-introduction fails.
_CWD_RELATIVE_DEFAULT = "${CODEMAP_INDEX_DIR:-.cache/codemap}"


def test_review_interprets_each_batch_child_independently() -> None:
    """A successful batch process cannot turn a failed child into an empty answer."""
    text = (_DEVELOP / "skills/review/SKILL.md").read_text(encoding="utf-8")
    for field in ("`ok: true`", "`result.index`", "`ok: false`", "valid empty"):
        assert field in text
    assert "one shared `index` coverage block covers the whole batch" not in text
    assert "are tested via mock" not in text


@pytest.mark.parametrize("relative", ["skills/_shared/codemap-context.md", "skills/review/SKILL.md"])
def test_consumer_reuses_only_qualified_answers(relative: str) -> None:
    """Copied prompt contracts must not accept failed/stale children or forbid source checks."""
    text = (_DEVELOP / relative).read_text(encoding="utf-8")
    for clause in (
        "only when `query_complete` is absent",
        "`result.index`",
        "`ok: false`",
        "valid empty",
        "source-body",
        "same project",
        "current index",
        "query and flags",
        "`stale`",
    ):
        assert clause in text


def _load(path: Path, name: str) -> ModuleType:
    """Load *path* under a unique module name — both plugins ship a ``codemap_resolve``."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_no_cwd_relative_index_dir_anywhere_in_the_plugin():
    """The index lives under the git toplevel; anchoring on the CWD is a false no_index."""
    offenders = [
        p.relative_to(_REPO_ROOT).as_posix()
        for p in _tracked_text_files(_DEVELOP)
        if _CWD_RELATIVE_DEFAULT in p.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"CWD-relative codemap index dir in: {offenders}"


@pytest.mark.parametrize(
    ("path", "default"),
    [
        pytest.param(
            path,
            default,
            id=f"{path.relative_to(_DEVELOP).as_posix()}:{default}",
        )
        for path in _tracked_text_files(_DEVELOP)
        for default in re.findall(r"\$\{CODEMAP_INDEX_DIR:-([^}]*)\}", path.read_text(encoding="utf-8"))
    ],
)
def test_every_index_dir_override_is_root_anchored(path: Path, default: str) -> None:
    """Each surviving `CODEMAP_INDEX_DIR:-` default must name the resolved repository root."""
    assert default.startswith("$_ROOT/"), f"{path.name}: unanchored default {default!r}"


def test_gates_resolve_and_read_the_shipped_contract():
    """The wrapper loads the contract from the active codemap-py install rather than transcribing it.

    Optional-provider exception: the gates have no value without codemap-py, so the wrapper reads the
    installed provider's file through its own resolver copy (registry tier first) instead of a frozen
    local copy — and never globs the cache itself.
    """
    text = _GATES.read_text(encoding="utf-8")

    assert (
        '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/resolve_shared_path.py" codemap-py claude-skills/_shared'
        in text
    )
    assert 'cat "$_CODEMAP_SHARED/codemap-gates.md"' in text
    assert "codemap-py--" not in text
    assert "plugins/cache" not in text
    assert "Contract (version as loaded)" in text
    assert "Fallback when codemap-py plugin absent" in text


def test_gate_wrapper_preserves_provider_preparation_contract():
    """The wrapper states default preparation, opt-in confirmation, and the gated launcher boundary."""
    text = _GATES.read_text(encoding="utf-8")

    assert "Gate A prepares automatically by default" in text
    assert "`LAZY_CODEMAP` is the explicit confirm-first preference" in text
    assert "Inherited `SCAN_NO_AUTOBUILD=1` freezes both gates" in text
    assert "dev_codemap_gate.py <current-skill> --skip" in text
    assert "pass `--no-heal` to every later query" in text
    assert "contract's own build/rebuild action is the gated `codemap-py index` launcher" in text
    assert "apply verbatim, with one override" not in text, "the contract no longer disagrees"
    assert "scan-index" not in text, "the retired alias has no reason to appear in a wrapper"


def test_fix_later_query_reloads_enabled_and_currency_state():
    """Persisted Gate B choices govern both the shared batch and later direct-caller query."""
    skill = (_DEVELOP / "skills" / "fix" / "SKILL.md").read_text(encoding="utf-8")
    assert 'IFS= read -r CODEMAP_ENABLED  < "${TMPDIR:-/tmp}/dev-fix-codemap-enabled-${CSID}"' in skill
    assert 'IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}"' in skill
    assert "codemap-py query --timeout 5 $_CODEMAP_NO_HEAL fn-rdeps" in skill


def test_gates_read_the_sentinel_the_resolver_writes():
    """A prefix mismatch would leave the stale gate permanently blind."""
    gate = _load(_DEVELOP / "bin" / "dev_codemap_gate.py", "dev_gate_sentinel_check")

    assert f"{gate.CURRENCY_PREFIX}-${{CSID}}" in _GATES.read_text(encoding="utf-8")


def test_no_retired_codemap_skill_prefix():
    """The plugin is `codemap-py:`; the bare `codemap:<skill>` prefix is retired.

    The skill name must follow the colon immediately — a prose colon ("With codemap: effort sizing is structural") and
    the ``# codemap: integrated-via-shared`` marker are not plugin references and must not be flagged.
    """
    offenders = [
        p.relative_to(_REPO_ROOT).as_posix()
        for p in _tracked_text_files(_DEVELOP)
        if re.search(r"(?<![\w-])codemap:[a-z]", p.read_text(encoding="utf-8"))
    ]

    assert offenders == [], f"retired `codemap:<skill>` prefix in: {offenders}"
