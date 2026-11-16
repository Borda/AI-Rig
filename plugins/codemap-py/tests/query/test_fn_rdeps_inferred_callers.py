"""``fn-rdeps`` marks callers reached only through inferred call edges.

Instance-call inference adds call edges carrying an index-only ``inferred`` key. A consumer that rewrites every caller
(``rename-refs``) needs to tell those lower-confidence callers apart, so a ``called_by`` row gains ``inferred: true``
when every edge that makes its symbol a caller is inferred. A row with at least one recorded edge keeps its historic
shape byte for byte, and no row ever carries ``inferred: false``.

The scan-driven tests build a real project whose receivers the scanner types (constructor assignment, ``super()``), so
the edge shape under test is the one the scanner writes. The in-process tests then edit that real index to reach edge
combinations the scanner does not produce today (a recorded and an inferred edge to one target, an alias-spelled
recorded edge, two symbol records sharing a qualified name) and pin that the marker never claims more than the edges
prove.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _scan_helpers import index_path_for, run_query, run_scan, write_tree
from codemap_py.query import callgraph, coverage, index_io

_SHOP = """\
class Meter:
    def __init__(self, n):
        self.n = n

    def update(self):
        return self.n


def poke():
    return Meter.update(Meter(1))


class Gauge(Meter):
    def __init__(self, n):
        super().__init__(n)


class Dial(Meter):
    def __init__(self, n):
        super().__init__(n)
        self.spare = Meter(0)
"""

_USER = """\
from shop import Meter


def read_meter():
    meter = Meter(3)
    return meter.update()
"""

_TEST_SHOP = """\
from shop import Meter


def test_update():
    meter = Meter(2)
    assert meter.update() == 2
"""

#: The row ``fn-rdeps shop::Meter.update`` printed for a recorded caller before the marker existed.
_RECORDED_POKE_ROW = {"caller": "shop::poke", "module": "shop", "path": "shop.py"}
#: The row for a caller whose only edge to ``shop::Meter.update`` is a constructor-typed receiver call.
_INFERRED_READ_ROW = {"caller": "user::read_meter", "module": "user", "path": "user.py", "inferred": True}


@pytest.fixture(name="typed_project", scope="module")
def _typed_project(tmp_path_factory, scan_index) -> tuple[Path, Path]:
    """Scan a project with recorded and inferred method callers once; return ``(root, index_path)``."""
    root = tmp_path_factory.mktemp("typed")
    write_tree(root, {"shop.py": _SHOP, "user.py": _USER, "tests/__init__.py": "", "tests/test_shop.py": _TEST_SHOP})
    run_scan(scan_index, root)
    return root, index_path_for(root)


def _rows_by_caller(payload: dict) -> dict[str, dict]:
    """Index the ``called_by`` rows of an ``fn-rdeps`` payload by caller qname."""
    return {row["caller"]: row for row in payload["called_by"]}


class TestScannedInferredCallers:
    """``fn-rdeps`` over a real scanned index marks exactly the callers the scanner reached by inference."""

    def test_marks_caller_reached_only_through_an_inferred_edge(self, typed_project, scan_query):
        """A caller whose sole edge comes from a constructor-typed receiver carries ``inferred: true``.

        ``meter = Meter(3); meter.update()`` is the inference the marker exists for: rename-refs must be able to ask
        for confirmation before rewriting that call site.
        """
        root, index = typed_project

        rows = _rows_by_caller(run_query(scan_query, root, index, "fn-rdeps", "shop::Meter.update"))

        assert rows["user::read_meter"] == _INFERRED_READ_ROW

    def test_recorded_caller_row_is_byte_identical(self, typed_project, scan_query):
        """A caller with a recorded edge keeps the historic three-key row, with no ``inferred: false``.

        ``Meter.update(Meter(1))`` in the defining module is a recorded local edge; its serialized row must equal what
        consumers parsed before the marker existed.
        """
        root, index = typed_project

        rows = _rows_by_caller(run_query(scan_query, root, index, "fn-rdeps", "shop::Meter.update"))

        assert json.dumps(rows["shop::poke"]) == json.dumps(_RECORDED_POKE_ROW)

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            pytest.param(
                "shop::Gauge.__init__",
                {"caller": "shop::Gauge.__init__", "module": "shop", "path": "shop.py", "inferred": True},
                id="super-init-only",
            ),
            pytest.param(
                "shop::Dial.__init__",
                {"caller": "shop::Dial.__init__", "module": "shop", "path": "shop.py"},
                id="super-init-plus-recorded-constructor",
            ),
            pytest.param(
                "user::read_meter",
                {"caller": "user::read_meter", "module": "user", "path": "user.py"},
                id="recorded-constructor-only",
            ),
        ],
    )
    def test_constructor_target_marks_only_callers_without_any_recorded_edge(
        self, typed_project, scan_query, caller, expected
    ):
        """An ``__init__`` target weighs the merged ``Class()`` edges too before marking a caller inferred.

        ``Dial.__init__`` reaches ``Meter.__init__`` through an inferred ``super().__init__()`` and also calls
        ``Meter(0)``, a recorded edge the constructor merge counts, so it is a mixed caller and stays unmarked.
        """
        root, index = typed_project

        rows = _rows_by_caller(run_query(scan_query, root, index, "fn-rdeps", "shop::Meter.__init__"))

        assert rows[caller] == expected

    def test_compact_output_keeps_the_same_rows(self, typed_project, scan_query):
        """``--compact`` reshapes only the coverage block, so marked and unmarked rows match the full output."""
        root, index = typed_project
        full = run_query(scan_query, root, index, "fn-rdeps", "shop::Meter.update")

        compact = run_query(scan_query, root, index, "--compact", "fn-rdeps", "shop::Meter.update")

        assert compact["called_by"] == full["called_by"]

    def test_exclude_tests_keeps_the_marker_on_remaining_rows(self, typed_project, scan_query):
        """Dropping test-module callers leaves the production rows, marker included, unchanged."""
        root, index = typed_project

        payload = run_query(scan_query, root, index, "fn-rdeps", "shop::Meter.update", "--exclude-tests")

        assert payload["called_by"] == [_RECORDED_POKE_ROW, _INFERRED_READ_ROW]

    def test_tsv_renders_marked_and_unmarked_rows_under_one_header(self, typed_project, scan_query):
        """``--format tsv`` keeps working when only some rows carry the marker; unmarked rows get an empty cell.

        Without the fill, the ragged rows made the table refusal fire and ``fn-rdeps --format tsv`` exit non-zero for
        any target with both recorded and inferred callers.
        """
        root, index = typed_project

        result = subprocess.run(
            [
                sys.executable,
                str(scan_query),
                "--index",
                str(index),
                "--format",
                "tsv",
                "fn-rdeps",
                "shop::Meter.update",
            ],
            capture_output=True,
            text=True,
            cwd=str(root),
        )

        assert (result.returncode, result.stdout.splitlines()) == (
            0,
            [
                "caller\tmodule\tpath\tinferred",
                "shop::poke\tshop\tshop.py\t",
                "tests.test_shop::test_update\ttests.test_shop\ttests/test_shop.py\tTrue",
                "user::read_meter\tuser\tuser.py\tTrue",
            ],
        )


class TestInferredMarkerEdgeSemantics:
    """The marker needs positive evidence: an inferred edge to the target and no recorded edge to it."""

    @pytest.fixture(name="scanned_index")
    def _scanned_index(self, typed_project, monkeypatch) -> dict:
        """Return a private copy of the real scanned index, with query caches reset and cwd at the project root."""
        root, index = typed_project
        monkeypatch.chdir(root)
        monkeypatch.setattr(index_io, "_symbol_map_cache", None)
        monkeypatch.setattr(index_io, "_rev_graph_cache", None)
        monkeypatch.setattr(coverage, "_coverage_cache", None)
        return copy.deepcopy(json.loads(index.read_text()))

    @staticmethod
    def _symbol(index: dict, module: str, qualified_name: str) -> dict:
        """Return the stored symbol record ``module::qualified_name`` of *index*."""
        entry = next(m for m in index["modules"] if m["name"] == module)
        return next(s for s in entry["symbols"] if s["qualified_name"] == qualified_name)

    @staticmethod
    def _rows(capsys, index: dict) -> dict[str, dict]:
        """Run ``fn-rdeps shop::Meter.update`` in process and index its rows by caller."""
        callgraph.cmd_fn_rdeps(index, "shop::Meter.update")
        return _rows_by_caller(json.loads(capsys.readouterr().out))

    def test_recorded_and_inferred_edge_to_one_target_leave_the_row_unmarked(self, scanned_index, capsys):
        """A caller holding both a recorded and an inferred edge to the target is a recorded caller."""
        self._symbol(scanned_index, "shop", "poke")["calls"].append(
            {"target": "shop::Meter.update", "resolution": "local", "inferred": "constructor"}
        )

        rows = self._rows(capsys, scanned_index)

        assert rows["shop::poke"] == _RECORDED_POKE_ROW

    def test_recorded_edge_spelled_through_an_alias_still_counts_as_recorded(self, scanned_index, capsys):
        """A recorded edge naming an alias of the target is resolved before the caller is judged inferred.

        ``read_meter`` keeps its inferred edge and gains a recorded edge spelled ``compat::Meter.update``, a persisted
        alias of ``shop::Meter.update``. Comparing raw targets would miss the recorded edge and mark the row.
        """
        scanned_index["symbol_aliases"]["compat::Meter.update"] = "shop::Meter.update"
        self._symbol(scanned_index, "user", "read_meter")["calls"].append(
            {"target": "compat::Meter.update", "resolution": "import"}
        )

        rows = self._rows(capsys, scanned_index)

        assert rows["user::read_meter"] == {"caller": "user::read_meter", "module": "user", "path": "user.py"}

    def test_recorded_edge_on_an_earlier_record_with_the_same_name_counts(self, scanned_index, capsys):
        """Every symbol record sharing the caller's qualified name is weighed, not only the last one stored.

        The symbol map keeps the last record per name; the recorded edge sits on an earlier duplicate record of
        ``read_meter`` (a redefinition), and the marker must not ignore it.
        """
        user = next(m for m in scanned_index["modules"] if m["name"] == "user")
        earlier = {**self._symbol(scanned_index, "user", "read_meter")}
        earlier["calls"] = [{"target": "shop::Meter.update", "resolution": "import"}]
        user["symbols"].insert(0, earlier)

        rows = self._rows(capsys, scanned_index)

        assert rows["user::read_meter"] == {"caller": "user::read_meter", "module": "user", "path": "user.py"}
