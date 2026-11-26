"""Tests for review_impact_tier.py: review depth follows the code path a change sits on, not its size.

A small change to a private helper on the main feature path must get the full specialist lineup, while a change that
only reaches visualization or test code may get the lighter one. Any doubt about impact must fall back to the full
lineup, because a trimmed review of a risky change is the failure this script exists to prevent.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest
import review_impact_tier as rit


def _module(path: str, *symbols: str, rdeps: int = 0) -> dict:
    """Build one diff-impact changed-module entry."""
    return {"path": path, "changed_symbols": list(symbols), "rdep_count": rdeps}


def _payload(*modules: dict, unmapped: tuple[str, ...] = ()) -> dict:
    """Build a diff-impact payload from changed-module entries."""
    return {"changed_modules": list(modules), "unmapped_files": list(unmapped)}


def _callers(*pairs: tuple[str, str]):
    """Return a fake fn-blast answering with the given (caller qname, caller path) pairs for every symbol."""
    return lambda _qname: {"blast_radius": [{"caller": c, "path": p} for c, p in pairs]}


class TestClassify:
    """Classify: the tier follows the code path a changed symbol sits on, never the diff size."""

    def test_private_helper_on_main_path_is_full(self) -> None:
        """A private helper called by a public main-path function gets the full review.

        This is the small-but-risky case: the diff touches only `_prep`, yet `run` reaches users through it.
        """
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_prep"))
        tier, reason = rit.classify(payload, _callers(("pkg.core::run", "src/pkg/core.py")))
        assert tier == "FULL"
        assert "pkg.core::run" in reason

    @pytest.mark.parametrize(
        ("module", "callers", "expected_tier"),
        [
            pytest.param(
                _module("src/pkg/core.py", "pkg.core::run"), _callers(), "FULL", id="public-symbol-without-callers-full"
            ),
            pytest.param(
                _module("src/pkg/testing/utils.py", "pkg.testing.utils::make_sample"),
                _callers(),
                "FULL",
                id="public-testing-helpers-are-not-test-code",
            ),
            pytest.param(
                _module("src/pkg/plot.py", "pkg.plot::render"),
                _callers(("pkg.api::export", "src/pkg/api.py")),
                "FULL",
                id="leaf-function-called-from-main-path-full",
            ),
            pytest.param(
                _module("src/pkg/core.py", "pkg.core::_x", rdeps=5),
                _callers(),
                "FULL",
                id="widely-imported-module-full",
            ),
            pytest.param(_module("src/pkg/core.py"), _callers(), "FULL", id="module-level-change-on-main-path-full"),
            pytest.param(
                _module("src/pkg/viz/draw.py", "pkg.viz.draw::box"), _callers(), "LIGHT", id="leaf-viz-public-light"
            ),
            pytest.param(
                _module("tests/test_core.py", "tests.test_core::test_run"), _callers(), "LIGHT", id="test-module-light"
            ),
            pytest.param(
                _module("src/pkg/core.py", "pkg.core::_unused"), _callers(), "LIGHT", id="private-no-callers-light"
            ),
        ],
    )
    def test_tier_follows_the_code_path_a_change_sits_on(self, module: dict, callers, expected_tier: str) -> None:
        """Review depth follows the code path: main-path or API changes are FULL, leaf/test/uncalled ones LIGHT.

        A changed public function is user-facing API even when nothing inside the repo calls it, and a public
        ``pkg.testing`` helper module is shipped API, not test-only. A leaf-path match never lowers the tier when a
        main-path function calls into it; a main-path module with five or more importers is full even for a private-only
        change; and a change with no mapped symbol (constants, imports) in a main-path module is full. Changes reaching
        only leaf, test or uncalled private code get the light review.
        """
        assert rit.classify(_payload(module), callers)[0] == expected_tier


class TestFailSafe:
    """Classify: any input it cannot fully map or query falls back to the full lineup."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(None, id="unreadable"),
            pytest.param({}, id="no-modules"),
            pytest.param(_payload(_module("src/pkg/x.py", "pkg.x::_f"), unmapped=("src/pkg/y.py",)), id="unmapped-py"),
        ],
    )
    def test_unknown_impact_is_full(self, payload: object) -> None:
        """Input the script cannot fully map yields FULL, never a trimmed lineup."""
        assert rit.classify(payload, _callers())[0] == "FULL"

    def test_failed_blast_query_is_full(self) -> None:
        """A failed fn-blast query for a private symbol means unknown callers, so FULL."""
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_prep"))
        tier, reason = rit.classify(payload, lambda _qname: None)
        assert tier == "FULL"
        assert reason.startswith("impact unknown")

    @pytest.mark.parametrize(
        ("fields", "cause"),
        [
            pytest.param({"rdep_count": "unknown"}, rit.ImpactCause.BAD_ENTRY, id="non-numeric-importer-count"),
            pytest.param({"rdep_count": True}, rit.ImpactCause.BAD_ENTRY, id="boolean-importer-count"),
            pytest.param({"rdep_count": -1}, rit.ImpactCause.BAD_ENTRY, id="negative-importer-count"),
            pytest.param(
                {"path": [], "module": "pkg.core"},
                rit.ImpactCause.MODULE_WITHOUT_PATH,
                id="malformed-path-with-module-fallback",
            ),
            pytest.param({"changed_symbols": 42}, rit.ImpactCause.BAD_ENTRY, id="non-list-changed-symbols"),
            pytest.param(
                {"changed_symbols": ["pkg.core::_prep", None]},
                rit.ImpactCause.BAD_ENTRY,
                id="non-string-changed-symbol",
            ),
        ],
    )
    def test_malformed_module_is_unknown_impact(self, fields: dict, cause: rit.ImpactCause) -> None:
        """A malformed field cannot be ignored to classify the remaining module evidence as LIGHT."""
        payload = _payload({**_module("src/pkg/core.py", "pkg.core::_prep"), **fields})
        tier, reason = rit.classify(payload, _callers())

        assert (tier, rit.cause_of(reason)) == ("FULL", cause)

    @pytest.mark.parametrize(
        "fields",
        [
            pytest.param({"unmapped_files": "pkg/core.py"}, id="unmapped-files-not-a-list"),
            pytest.param({"unmapped_files": [None]}, id="unmapped-file-not-a-string"),
            pytest.param({"index": []}, id="index-not-an-object"),
            pytest.param({"index": {"stale": 1}}, id="stale-flag-not-boolean"),
            pytest.param({"index": {"root_mismatch": "true"}}, id="root-mismatch-flag-not-boolean"),
        ],
    )
    def test_malformed_change_set_is_unknown_impact(self, fields: dict) -> None:
        """Unavailable coverage and index provenance cannot be mistaken for complete mapping."""
        payload = {**_payload(_module("src/pkg/core.py", "pkg.core::_prep")), **fields}
        tier, reason = rit.classify(payload, _callers())

        assert (tier, rit.cause_of(reason)) == ("FULL", rit.ImpactCause.UNREADABLE_OUTPUT)

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param(None, id="caller-entry-not-an-object"),
            pytest.param({}, id="caller-entry-without-name-or-path"),
            pytest.param({"caller": [], "path": "src/pkg/core.py"}, id="caller-name-not-a-string"),
            pytest.param({"caller": "pkg.core::run", "path": {}, "module": "pkg.viz"}, id="caller-path-not-a-string"),
        ],
    )
    def test_malformed_caller_entry_is_a_failed_query(self, entry: object) -> None:
        """An unusable caller entry cannot count as evidence that a private function has no main-path callers."""
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_prep"))
        tier, reason = rit.classify(payload, lambda _qname: {"blast_radius": [entry]})

        assert (tier, rit.cause_of(reason)) == ("FULL", rit.ImpactCause.FN_BLAST_FAILED)

    def test_malformed_caller_after_main_path_is_still_a_failed_query(self) -> None:
        """An earlier main-path caller cannot make the rest of a malformed query trustworthy."""
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_prep"))
        callers = [{"caller": "pkg.core::run", "path": "src/pkg/core.py"}, None]
        tier, reason = rit.classify(payload, lambda _qname: {"blast_radius": callers})

        assert (tier, rit.cause_of(reason)) == ("FULL", rit.ImpactCause.FN_BLAST_FAILED)

    @pytest.mark.parametrize(
        ("exit_code", "stderr", "expected"),
        [
            pytest.param(0, b"", "LIGHT", id="successful-empty-blast-radius"),
            pytest.param(1, b"", "FULL", id="failed-query-with-empty-blast-radius"),
            pytest.param(0, b"\xff", "FULL", id="undecodable-provider-output"),
        ],
    )
    def test_caller_subprocess_must_succeed_to_trim_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, stderr: bytes, expected: str
    ) -> None:
        """Run a provider that prints valid caller JSON and vary whether its execution actually succeeds."""
        script = tmp_path / "provider.py"
        script.write_text(
            f"import sys\nprint({json.dumps({'blast_radius': []})!r})\n"
            f"sys.stderr.buffer.write({stderr!r})\nraise SystemExit({exit_code})\n",
            encoding="utf-8",
        )
        if os.name == "nt":
            launcher = tmp_path / "codemap-py.cmd"
            launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        else:
            launcher = tmp_path / "codemap-py"
            launcher.write_text(f"#!{sys.executable}\n{script.read_text(encoding='utf-8')}", encoding="utf-8")
            launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_prep"))

        tier, reason = rit.classify(payload)

        assert tier == expected
        assert rit.cause_of(reason) == (rit.ImpactCause.FN_BLAST_FAILED if expected == "FULL" else None)

    def test_symbol_cap_exceeded_is_full(self) -> None:
        """More changed symbols than the query cap are not silently skipped; the run fails safe to FULL."""
        symbols = [f"pkg.viz.draw::_f{i}" for i in range(rit.SYMBOL_CAP + 1)]
        payload = _payload(_module("src/pkg/viz/draw.py", *symbols))
        assert rit.classify(payload, _callers())[0] == "FULL"

    @mock.patch("review_impact_tier.shutil.which", new=mock.create_autospec(shutil.which, return_value=None))
    def test_missing_codemap_binary_is_full(self) -> None:
        """Without a codemap-py binary on PATH the real fn-blast runner fails safe to FULL.

        ``classify`` is called without a blast function, so it uses the default runner; with no ``codemap-py`` found the
        private symbol's callers are unknown and the review must not be trimmed.
        """
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_prep"))
        assert rit.classify(payload)[0] == "FULL"


class TestStdinCap:
    """Main: diff-impact JSON read from stdin is bounded before parsing."""

    @pytest.mark.parametrize(
        ("stdin_text", "expected_line"),
        [
            pytest.param("{}", "FULL · impact unknown: no changed modules mapped", id="within-cap-parsed"),
            pytest.param(
                '{"changed_modules": []}' + " " * 64,
                "FULL · impact unknown: diff-impact input exceeds 32 characters",
                id="over-cap-not-parsed",
            ),
        ],
    )
    def test_main_bounds_stdin_and_fails_safe(
        self,
        stdin_text: str,
        expected_line: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Diff-impact JSON within the cap is classified; larger input is never read whole and reports FULL.

        The calling skill pipes codemap output straight into this script and redirects stdout into the impact sentinel,
        so an oversized payload must still yield a FULL line and exit 0 instead of being slurped into memory.
        """
        stream = io.StringIO(stdin_text)
        monkeypatch.setattr(rit, "MAX_INPUT_CHARS", 32)
        monkeypatch.setattr(rit.sys, "stdin", stream)
        exit_code = rit.main([])
        assert exit_code == 0
        assert capsys.readouterr().out.strip() == expected_line
        assert stream.tell() <= 33
