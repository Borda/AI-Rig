"""Frozen-grammar corpus tests for the codemap scan-index extractor.

These tests pin two properties of ``bin/scan-index`` against a small corpus of
Python source that straddles the CPython 3.11/3.12 grammar boundary
(``tests/corpus/``):

1. Determinism — scanning the same tree twice yields byte-identical index JSON
   once the two volatile top-level keys (``scanned_at``, ``git_sha``) are stripped.
   No ``datetime.now`` value or other run-to-run entropy leaks into the index body.
2. Grammar degradation tracks the running interpreter — a module whose syntax the
   running CPython cannot parse is recorded as ``status="degraded"`` and never
   silently dropped or partially indexed; a module the running CPython accepts is
   indexed normally. The corpus's post-3.11 modules therefore degrade on the 3.10
   and 3.11 matrix cells and index cleanly on 3.12+, which is where the multi-minor
   comparison actually happens.

The extractor is always invoked through ``sys.executable`` so the grammar it parses
with is exactly the interpreter running this test — that identity is what makes the
degradation correlation exact on every matrix cell.

Why not use ``ast.parse(feature_version=(3, 11))`` as the degradation oracle: on a
3.12+ host ``feature_version`` down-gates the PEG grammar (so PEP 695 type-parameter
syntax is rejected) but does NOT down-gate the tokenizer, so PEP 701 f-string quote
reuse still parses. It is a reliable oracle only for grammar-gated features, so it is
used below solely to assert — interpreter-independently — that the corpus contains
grammar-level post-3.11 syntax. The real per-file degradation oracle is the running
interpreter's own parse.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
_SCAN_INDEX = _TESTS_DIR.parent / "bin" / "scan-index"

# Top-level index keys that legitimately vary between two scans of identical source.
_VOLATILE_KEYS = ("scanned_at", "git_sha")

# Corpus modules whose syntax first became valid in CPython 3.12.
_POST_311_MODULES = ("pep695_type_params", "fstring_nesting_312")
_ACCEPTED_MODULE = "accepted_311"
# What the post-3.11 modules must be recorded as on the running interpreter: degraded below 3.12, indexed from 3.12 on.
_POST_311_STATUS = "degraded" if sys.version_info < (3, 12) else "ok"


def _corpus_files(corpus_dir: Path) -> list[Path]:
    """Return the corpus ``*.py`` modules, excluding the pytest ``conftest.py`` guard."""
    return sorted(p for p in corpus_dir.glob("*.py") if p.name != "conftest.py")


def _running_grammar_rejects(source: str) -> bool:
    """Return True when the running interpreter cannot parse *source*.

    Mirrors exactly what ``scan-index`` sees, because both this test and the extractor subprocess run under the same
    ``sys.executable``.
    """
    try:
        ast.parse(source)
    except SyntaxError:
        return True
    return False


def _make_corpus_tree(tmp_path: Path, corpus_dir: Path) -> Path:
    """Copy the corpus into a fresh, git-free source tree and return its path.

    The tree carries no ``.git`` so ``git_sha`` resolves identically (None) on every scan regardless of the host
    repository. A single tree is reused across the two scans of a determinism test so ``scan_root`` — a legitimately
    path-dependent field the spec keeps — is held constant.
    """
    src = tmp_path / "src"
    src.mkdir(parents=True)
    for f in _corpus_files(corpus_dir):
        shutil.copy2(f, src / f.name)
    return src


def _scan(src: Path, index_dir: Path) -> dict:
    """Run ``scan-index`` over *src*, writing into *index_dir*, and return the index dict."""
    index_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(_SCAN_INDEX), "--root", str(src)],
        env={**os.environ, "CODEMAP_INDEX_DIR": str(index_dir)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"scan-index failed: {result.stderr}"
    written = list(index_dir.rglob("*.json"))
    assert len(written) == 1, f"expected exactly one index file, got {written}"
    return json.loads(written[0].read_text())


def _module_status(index: dict) -> dict:
    """Map module names to status strings from an index payload.

    >>> _module_status({"modules": [{"name": "pkg.mod", "status": "ok"}]})
    {'pkg.mod': 'ok'}
    """
    return {m["name"]: m.get("status") for m in index["modules"]}


def _strip_volatile(index: dict) -> dict:
    """Remove scan timestamps and git identity before comparing indexes.

    >>> _strip_volatile({"git_sha": "abc", "scanned_at": "now", "modules": []})
    {'modules': []}
    """
    return {k: v for k, v in index.items() if k not in _VOLATILE_KEYS}


def _canonical(index: dict) -> str:
    """Serialize an index with stable key ordering for deterministic assertions."""
    return json.dumps(index, sort_keys=True, ensure_ascii=False)


def _status_mismatches(status: dict, corpus_dir: Path) -> list[str]:
    """Describe every corpus file whose recorded status disagrees with the running interpreter's own parse."""
    mismatches = []
    for f in _corpus_files(corpus_dir):
        rejected = _running_grammar_rejects(f.read_text())
        expected = "degraded" if rejected else "ok"
        if status.get(f.stem) != expected:
            mismatches.append(
                f"{f.name}: running-grammar rejected={rejected} but scan-index status={status.get(f.stem)!r}"
            )
    return mismatches


@pytest.fixture(name="corpus_scans", scope="module")
def _corpus_scans(tmp_path_factory: pytest.TempPathFactory, corpus_dir: Path) -> tuple[dict, dict]:
    """Scan one copy of the corpus twice and return both index payloads.

    Every test here reads these two scans, so the extractor subprocess runs twice per module instead of once per test.
    The scans share one source tree so ``scan_root`` stays constant; the module-scoped setup runs outside the function-
    scoped autouse env fixtures, so telemetry and host-session variables are neutralised here.
    """
    base = tmp_path_factory.mktemp("grammar")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CODEMAP_LOGGING", "false")
        for name in ("CLAUDE_CODE_SESSION_ID", "CSID", "CODEX_THREAD_ID"):
            patch.delenv(name, raising=False)
        src = _make_corpus_tree(base, corpus_dir)
        return _scan(src, base / "idx_a"), _scan(src, base / "idx_b")


@pytest.mark.parametrize("module", [_ACCEPTED_MODULE, *_POST_311_MODULES])
def test_corpus_module_is_present(corpus_dir: Path, module: str) -> None:
    """Each frozen corpus module exists under its pinned name.

    The accepted baseline and the two post-3.11 modules are the whole corpus the other tests reason about; a rename or
    deletion would silently turn their expectations into no-ops.
    """
    assert module in {p.stem for p in _corpus_files(corpus_dir)}


def test_corpus_contains_grammar_gated_post311_syntax(corpus_dir: Path) -> None:
    """Interpreter-independent frozen-grammar guarantee via ``feature_version``.

    PEP 695 type parameters are grammar-gated, so ``feature_version=(3, 11)`` rejects them on every host interpreter —
    this asserts the corpus really does exercise post-3.11 grammar, without depending on which minor runs the test. The
    accepted baseline must conversely parse clean under the 3.11 grammar.
    """
    pep695 = (corpus_dir / "pep695_type_params.py").read_text()
    accepted = (corpus_dir / "accepted_311.py").read_text()
    with pytest.raises(SyntaxError):
        ast.parse(pep695, feature_version=(3, 11))
    ast.parse(accepted, feature_version=(3, 11))  # must not raise


def test_scan_index_is_deterministic(corpus_scans: tuple[dict, dict]) -> None:
    """Two scans of identical source produce identical index JSON minus volatile keys.

    Run-to-run entropy (timestamps, ordering) leaking into the index body would make every refresh look like a change.
    """
    first, second = corpus_scans

    assert _canonical(_strip_volatile(first)) == _canonical(_strip_volatile(second))


def test_degraded_records_are_stable(corpus_scans: tuple[dict, dict]) -> None:
    """The set of degraded module records is identical across two scans."""
    first, second = corpus_scans

    degraded_first = sorted((m["name"], m.get("reason")) for m in first["modules"] if m.get("status") == "degraded")
    degraded_second = sorted((m["name"], m.get("reason")) for m in second["modules"] if m.get("status") == "degraded")

    assert degraded_first == degraded_second


def test_degradation_tracks_running_grammar(corpus_scans: tuple[dict, dict], corpus_dir: Path) -> None:
    """Scan-index degrades a module iff the running interpreter cannot parse it.

    This is the exact correlation that holds on every matrix cell: the extractor and this test share
    ``sys.executable``, so their parse verdicts must agree for every corpus file.
    """
    first, _ = corpus_scans

    assert _status_mismatches(_module_status(first), corpus_dir) == []


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        pytest.param(_ACCEPTED_MODULE, "ok", id="accepted-baseline"),
        pytest.param("pep695_type_params", _POST_311_STATUS, id="pep695"),
        pytest.param("fstring_nesting_312", _POST_311_STATUS, id="fstring-nesting"),
    ],
)
def test_post311_modules_degrade_below_312(corpus_scans: tuple[dict, dict], module: str, expected: str) -> None:
    """Version-conditioned expectation — the multi-minor teeth of the corpus.

    On 3.10 and 3.11 the two post-3.11 modules must be degraded; on 3.12+ they must index cleanly. The accepted baseline
    indexes on every supported minor.
    """
    first, _ = corpus_scans

    assert _module_status(first).get(module) == expected, f"{module} on {sys.version_info[:2]}"
