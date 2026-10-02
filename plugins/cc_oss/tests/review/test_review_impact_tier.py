"""Tests for review_impact_tier.py: review depth follows the code path a change sits on, not its size.

A small change to a private helper on the main feature path must get the full specialist lineup, while a change that
only reaches visualization or test code may get the lighter one. Any doubt about impact must fall back to the full
lineup, because a trimmed review of a risky change is the failure this script exists to prevent.
"""

from __future__ import annotations

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
    def test_private_helper_on_main_path_is_full(self) -> None:
        """A private helper called by a public main-path function gets the full review.

        This is the small-but-risky case: the diff touches only `_prep`, yet `run` reaches users through it.
        """
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_prep"))
        tier, reason = rit.classify(payload, _callers(("pkg.core::run", "src/pkg/core.py")))
        assert (tier, "pkg.core::run" in reason) == ("FULL", True)

    def test_public_symbol_without_internal_callers_is_full(self) -> None:
        """A changed public function is user-facing API even when nothing inside the repo calls it."""
        payload = _payload(_module("src/pkg/core.py", "pkg.core::run"))
        assert rit.classify(payload, _callers())[0] == "FULL"

    @pytest.mark.parametrize(
        "module",
        [
            pytest.param(_module("src/pkg/viz/draw.py", "pkg.viz.draw::box"), id="leaf-viz-public"),
            pytest.param(_module("tests/test_core.py", "tests.test_core::test_run"), id="test-module"),
            pytest.param(_module("src/pkg/core.py", "pkg.core::_unused"), id="private-no-callers"),
        ],
    )
    def test_off_main_path_is_light(self, module: dict) -> None:
        """Changes reaching only leaf, test or uncalled private code get the light review."""
        assert rit.classify(_payload(module), _callers())[0] == "LIGHT"

    def test_public_testing_helpers_are_not_test_code(self) -> None:
        """A public `pkg.testing` helper module is shipped API, so a change there is full, not test-only."""
        payload = _payload(_module("src/pkg/testing/utils.py", "pkg.testing.utils::make_sample"))
        assert rit.classify(payload, _callers())[0] == "FULL"

    def test_leaf_function_called_from_main_path_is_full(self) -> None:
        """A leaf-path match never lowers the tier when a main-path function calls into it."""
        payload = _payload(_module("src/pkg/plot.py", "pkg.plot::render"))
        assert rit.classify(payload, _callers(("pkg.api::export", "src/pkg/api.py")))[0] == "FULL"

    def test_widely_imported_module_is_full(self) -> None:
        """A main-path module with five or more importers is full even for a private-only change."""
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_x", rdeps=5))
        assert rit.classify(payload, _callers())[0] == "FULL"

    def test_module_level_change_on_main_path_is_full(self) -> None:
        """A change with no mapped symbol (constants, imports) in a main-path module is full."""
        assert rit.classify(_payload(_module("src/pkg/core.py")), _callers())[0] == "FULL"


class TestFailSafe:
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
        assert (tier, reason.startswith("impact unknown")) == ("FULL", True)

    def test_symbol_cap_exceeded_is_full(self) -> None:
        """More changed symbols than the query cap are not silently skipped; the run fails safe to FULL."""
        symbols = [f"pkg.viz.draw::_f{i}" for i in range(rit.SYMBOL_CAP + 1)]
        payload = _payload(_module("src/pkg/viz/draw.py", *symbols))
        assert rit.classify(payload, _callers())[0] == "FULL"

    def test_missing_codemap_binary_is_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a codemap-py binary on PATH the real fn-blast runner fails safe to FULL."""
        monkeypatch.setattr(rit.shutil, "which", lambda _name: None)
        payload = _payload(_module("src/pkg/core.py", "pkg.core::_prep"))
        assert rit.classify(payload)[0] == "FULL"
