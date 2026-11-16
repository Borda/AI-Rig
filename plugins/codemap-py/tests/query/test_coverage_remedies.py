"""Every active completeness reducer carries its own one-line remedy in the coverage block.

A result can be incomplete for several reasons at once — a stale index that also misses a new untracked file and holds
a qualname collision. The coverage block used to name only the first veto, so the remaining reasons lost their remedy,
and a collision only said "verify with grep" with no command that fixes it. These tests pin the repaired contract:

- one case per reducer: its ``remedies`` entry, its ``completeness_reason`` and its note segment;
- co-occurring reducers: every remedy survives, in veto-priority order, in the full and the compact block;
- an old consumer that reads only the pre-existing keys still gets the first veto, the legacy flags and a note;
- a single-reason note keeps the exact wording it had before (collision excepted: it gained a fix command);
- remedy text carries no identifier from the index (no file, module, symbol or path).

Unit cases drive :func:`_cmd_coverage` over a hand-built base so each reducer is isolated; the end-to-end case runs the
real ``scan-index`` and ``scan-query`` over a git repository, since the CLI answer is the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _scan_helpers import index_path_for, init_repo, run_git, run_query, run_scan
from codemap_py.query import coverage

_NOTE_PREFIX = "All 4 indexed modules were searched. ⚠ This result may be incomplete — "

#: Remedy text per reducer, written out independently of the implementation table.
_EXPECTED_REMEDY = {
    "root_mismatch": "Re-scan the current root, or query with a matching --root.",
    "stale": "Re-run /codemap-py:scan-codebase to update.",
    "stale_undetermined": "Re-run /codemap-py:scan-codebase, or verify with grep.",
    "module_degraded": "verify with grep.",
    "degraded": "see the degraded_files list (each with its parse error) for the files that may hide an edge into "
    "this result.",
    "untracked": "re-scan or verify with grep.",
    "collision": "the collisions list in the index file (see index_path) names each kept and dropped path — exclude "
    "the unwanted copy ([tool.codemap] exclude or .codemapignore) or list its intended root first in [tool.codemap] "
    "src_roots, then run a full /codemap-py:scan-codebase without --incremental.",
    "symbol_alias_ambiguous": "see symbol_alias_limitations for the alias and reason, then verify that path with grep.",
}

#: Single-reason note detail as emitted before every reducer carried a remedy (collision excluded: it changed).
_LEGACY_DETAIL = {
    "root_mismatch": "the index was built for a different project root than the one queried (--root or CWD differs "
    "from the index's scan_root) — this result describes another tree. Re-scan the current root, or query with a "
    "matching --root.",
    "stale": "the index is stale (source files changed since last scan). Re-run /codemap-py:scan-codebase to update.",
    "stale_undetermined": "source fingerprints could not be read, so whether the index is stale is UNKNOWN — this "
    "answer may describe an out-of-date tree. Re-run /codemap-py:scan-codebase, or verify with grep.",
    "module_degraded": "the queried module failed to parse and was skipped; verify with grep.",
    "degraded": "2 module(s) failed to parse and were skipped — see the degraded_files list (each with its parse "
    "error) for the files that may hide an edge into this result.",
    "untracked": "1 new .py file(s) are not represented in the index and may hide an edge; re-scan or verify with grep.",
    "symbol_alias_ambiguous": "a rejected top-level alias path may hide a caller of this symbol; see "
    "symbol_alias_limitations for the alias and reason, then verify that path with grep.",
}

#: Keys an old consumer reads from the full block; unchanged in name and meaning.
_LEGACY_FULL_KEYS = {
    "total_modules",
    "total_symbols",
    "degraded",
    "degraded_files",
    "star_import_modules",
    "has_call_graph",
    "stale",
    "untracked_py",
    "collision_count",
    "root_mismatch",
    "note",
    "query_complete",
    "exhaustive",
    "completeness_reason",
    "index_path",
}

_INDEX_PATH = "/project/.cache/codemap/project.json"
_ALIAS_RECORD = {"alias": "pkg.secret_alias", "reason": "ambiguous"}


def _base(**overrides: object) -> dict:
    """Return a healthy shared-coverage base for four modules, with *overrides* applied."""
    base: dict = {
        "total_modules": 4,
        "total_symbols": 9,
        "degraded": 0,
        "degraded_files": [],
        "star_import_modules": 0,
        "has_call_graph": True,
        "stale": False,
        "untracked_py": [],
        "collision_count": 0,
        "root_mismatch": False,
        "_collision_names": frozenset(),
    }
    base.update(overrides)
    return base


@pytest.fixture
def block(monkeypatch: pytest.MonkeyPatch):
    """Return a builder that renders the coverage block for a given base, command and output mode."""

    def build(
        base: dict,
        *,
        command: str = "rdeps",
        compact: bool = False,
        alias_records: list[dict[str, str]] | None = None,
        **kwargs: object,
    ) -> dict:
        records = alias_records or []
        monkeypatch.setattr(coverage, "_coverage", lambda _index: base)
        monkeypatch.setattr(coverage, "_should_compact_coverage", lambda: compact)
        monkeypatch.setattr(coverage, "_alias_limitations_for_target", lambda _index, _target: records)
        monkeypatch.setattr(coverage, "_symbol_alias_limitations", lambda _index: records)
        monkeypatch.setattr(coverage.state, "_LOADED_INDEX_PATH", _INDEX_PATH)
        return coverage._cmd_coverage({}, command=command, **kwargs)

    return build


#: One isolated case per reducer: (reducer, base overrides, command, module_status, module_name, alias records).
_SINGLE_REDUCER_CASES = [
    pytest.param("root_mismatch", {"root_mismatch": True}, "rdeps", None, None, None, id="root-mismatch"),
    pytest.param("stale", {"stale": True}, "rdeps", None, None, None, id="stale"),
    pytest.param("stale_undetermined", {"stale_undetermined": True}, "central", None, None, None, id="undetermined"),
    pytest.param("module_degraded", {}, "deps", "degraded", "pkg.mod", None, id="module-degraded"),
    pytest.param("degraded", {"degraded": 2}, "rdeps", None, None, None, id="degraded"),
    pytest.param("untracked", {"untracked_py": ["scratch.py"]}, "rdeps", None, None, None, id="untracked"),
    pytest.param("collision", {"collision_count": 1}, "central", None, None, None, id="collision-wide"),
    pytest.param(
        "collision",
        {"collision_count": 1, "_collision_names": frozenset({"pkg.mod"})},
        "symbols",
        "ok",
        "pkg.mod",
        None,
        id="collision-local",
    ),
    pytest.param("symbol_alias_ambiguous", {}, "fn-rdeps", None, None, [_ALIAS_RECORD], id="alias-ambiguous"),
]


class TestSingleReducer:
    """Each reducer on its own yields exactly one remedy, the matching reason slug and a note ending in that remedy."""

    @pytest.mark.parametrize(
        ("reducer", "overrides", "command", "module_status", "module_name", "alias_records"), _SINGLE_REDUCER_CASES
    )
    def test_reducer_carries_its_remedy(
        self, block, reducer, overrides, command, module_status, module_name, alias_records
    ):
        """The full block names the reducer, carries its one-line remedy and ends the note with the same text."""
        cov = block(
            _base(**overrides),
            command=command,
            module_status=module_status,
            module_name=module_name,
            alias_records=alias_records,
        )

        assert (cov["query_complete"], cov["completeness_reason"]) == (False, reducer)
        assert cov["remedies"] == [{"reason": reducer, "remedy": _EXPECTED_REMEDY[reducer]}]
        assert cov["note"].endswith(_EXPECTED_REMEDY[reducer])
        assert "\n" not in cov["remedies"][0]["remedy"]

    @pytest.mark.parametrize(
        ("reducer", "overrides", "command", "module_status", "module_name", "alias_records"),
        [case for case in _SINGLE_REDUCER_CASES if case.values[0] != "collision"],
    )
    def test_single_reason_note_keeps_its_wording(
        self, block, reducer, overrides, command, module_status, module_name, alias_records
    ):
        """A one-reducer note is byte-identical to the wording consumers already saw; only collision gained a fix."""
        cov = block(
            _base(**overrides),
            command=command,
            module_status=module_status,
            module_name=module_name,
            alias_records=alias_records,
        )

        assert cov["note"] == _NOTE_PREFIX + _LEGACY_DETAIL[reducer]

    def test_collision_names_a_fix_command(self, block):
        """A collision remedy says how to remove the collision, not only how to work around it."""
        cov = block(_base(collision_count=1), command="central")

        remedy = cov["remedies"][0]["remedy"]
        assert "[tool.codemap] exclude" in remedy
        assert "src_roots" in remedy
        assert "/codemap-py:scan-codebase without --incremental" in remedy

    def test_measured_staleness_supersedes_undetermined(self, block):
        """Known staleness and unknown staleness never both apply; the measured one wins."""
        cov = block(_base(stale=True, stale_undetermined=True), command="central")

        assert [entry["reason"] for entry in cov["remedies"]] == ["stale"]


class TestCoOccurringReducers:
    """Several reducers at once each keep their remedy, in veto-priority order."""

    def test_stale_untracked_and_collision_each_keep_a_remedy(self, block):
        """Stale plus untracked plus collision yields three remedies, all of them in the note as well."""
        base = _base(stale=True, untracked_py=["scratch.py"], collision_count=1)

        cov = block(base, command="rdeps")

        assert cov["completeness_reason"] == "stale"
        assert cov["remedies"] == [
            {"reason": "stale", "remedy": _EXPECTED_REMEDY["stale"]},
            {"reason": "untracked", "remedy": _EXPECTED_REMEDY["untracked"]},
            {"reason": "collision", "remedy": _EXPECTED_REMEDY["collision"]},
        ]
        assert cov["note"] == (
            _NOTE_PREFIX
            + _LEGACY_DETAIL["stale"]
            + " Also: "
            + _LEGACY_DETAIL["untracked"]
            + " Also: 1 qualname collision(s) in the index dropped a module — it may hide an edge; "
            + _EXPECTED_REMEDY["collision"]
        )

    def test_every_wide_reducer_in_priority_order(self, block):
        """All wide reducers at once come out root mismatch first, then staleness, then the graph-level ones."""
        base = _base(root_mismatch=True, stale=True, degraded=2, untracked_py=["scratch.py"], collision_count=1)

        cov = block(base, command="central")

        assert [entry["reason"] for entry in cov["remedies"]] == [
            "root_mismatch",
            "stale",
            "degraded",
            "untracked",
            "collision",
        ]
        assert cov["completeness_reason"] == "root_mismatch"

    def test_local_query_ignores_untracked_but_keeps_its_other_reducers(self, block):
        """A local read stays immune to untracked files while its own parse failure and staleness both surface."""
        base = _base(stale=True, untracked_py=["scratch.py"], collision_count=1)

        cov = block(base, command="deps", module_status="degraded", module_name="pkg.mod")

        assert [entry["reason"] for entry in cov["remedies"]] == ["stale", "module_degraded"]

    def test_alias_veto_leads_and_keeps_the_rest(self, block):
        """The alias veto stays the reported reason, and staleness behind it keeps its remedy."""
        cov = block(_base(stale=True), command="fn-blast", alias_records=[_ALIAS_RECORD])

        assert cov["completeness_reason"] == "symbol_alias_ambiguous"
        assert [entry["reason"] for entry in cov["remedies"]] == ["symbol_alias_ambiguous", "stale"]

    def test_query_complete_keeps_the_first_veto(self):
        """The legacy single-verdict helper reports the highest-priority reducer of the full set."""
        base = _base(stale=True, untracked_py=["scratch.py"], collision_count=1)

        verdict = coverage._query_complete(base, command="rdeps", module_status=None, module_name=None)

        assert verdict == (False, "stale")

    def test_wide_complete_keeps_the_first_graph_veto(self):
        """The legacy wide helper still names only the first graph-level reducer."""
        verdict = coverage._wide_complete(_base(untracked_py=["scratch.py"], collision_count=1))

        assert verdict == (False, "untracked")


class TestCompactBlock:
    """The coverage diet keeps every remedy whenever the answer is incomplete."""

    def test_compact_incomplete_block_keeps_every_remedy(self, block):
        """The compact block carries every reducer's remedy, the note and the index path it points at."""
        base = _base(stale=True, untracked_py=["scratch.py"], collision_count=1)

        compact = block(base, command="rdeps", compact=True)

        assert compact["compact"] is True
        assert compact["remedies"] == [
            {"reason": "stale", "remedy": _EXPECTED_REMEDY["stale"]},
            {"reason": "untracked", "remedy": _EXPECTED_REMEDY["untracked"]},
            {"reason": "collision", "remedy": _EXPECTED_REMEDY["collision"]},
        ]
        assert _EXPECTED_REMEDY["collision"] in compact["note"]
        assert compact["index_path"] == _INDEX_PATH

    def test_compact_truncated_alias_remedy_points_at_the_full_list(self, block):
        """A compact block that shows only a sample of alias records says how to see the rest."""
        records = [{"alias": f"pkg.alias_{i}", "reason": "ambiguous"} for i in range(12)]

        compact = block(_base(), command="fn-central", compact=True, alias_records=records)

        remedy = compact["remedies"][0]["remedy"]
        assert compact["remedies"][0]["reason"] == "symbol_alias_ambiguous"
        assert "shows 8 of 12" in remedy
        assert "Run without --compact" in remedy

    def test_complete_compact_block_has_no_remedies(self, block):
        """A complete compact answer stays as lean as before."""
        compact = block(_base(), command="rdeps", compact=True)

        assert compact["query_complete"] is True
        assert "remedies" not in compact


class TestOldConsumer:
    """A consumer written before ``remedies`` existed reads the same keys with the same meaning."""

    @staticmethod
    def _legacy_reader(index: dict) -> tuple[bool, bool, str, bool]:
        """Read the coverage block the way a pre-remedies consumer does: only long-standing keys."""
        complete = index.get("query_complete", index.get("exhaustive"))
        return complete, index["exhaustive"], index["completeness_reason"], "incomplete" in index["note"]

    def test_multi_reducer_block_reads_as_before(self, block):
        """The first veto, the legacy flags and the warning note are unchanged for an old reader."""
        cov = block(_base(stale=True, untracked_py=["scratch.py"], collision_count=1), command="rdeps")

        assert self._legacy_reader(cov) == (False, False, "stale", True)
        assert _LEGACY_FULL_KEYS <= cov.keys()

    def test_complete_block_gains_no_key(self, block):
        """A complete full block carries exactly the keys it always carried."""
        cov = block(_base(), command="rdeps")

        assert set(cov) == _LEGACY_FULL_KEYS
        assert cov["completeness_reason"] == "ok"


class TestIdentityFreeRemedies:
    """Remedy text never carries a name taken from the index, so telemetry needs no new identity scrub for it."""

    def test_no_index_identifier_reaches_a_remedy(self, block):
        """File names, colliding module names and alias names stay out of every remedy string."""
        base = _base(
            stale=True,
            degraded=1,
            degraded_files=[{"path": "pkg/secret_broken.py", "error": "bad"}],
            untracked_py=["pkg/secret_new.py"],
            collision_count=1,
            _collision_names=frozenset({"pkg.secret_dup"}),
        )

        cov = block(base, command="fn-rdeps", alias_records=[_ALIAS_RECORD], query_target="pkg.secret_target::fn")

        assert len(cov["remedies"]) == 5
        assert not any("secret" in entry["remedy"] or "pkg" in entry["remedy"] for entry in cov["remedies"])


@pytest.fixture
def stale_untracked_collision_repo(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Build a git repo whose index is stale, misses an untracked importer and records one qualname collision.

    The untracked file both changes the fingerprint (stale) and is missing from the index (untracked); the collision is
    written into the index exactly as ``scan-index`` records one.
    """
    root = tmp_path / "repo"
    root.mkdir()
    init_repo(root)
    (root / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")
    (root / "consumer.py").write_text("import leaf\n\ndef use(x):\n    return leaf.leaf_fn(x)\n")
    run_git(root, "add", "leaf.py", "consumer.py")
    run_git(root, "commit", "-q", "-m", "init")
    run_scan(scan_index, root)
    index_path = index_path_for(root)
    data = json.loads(index_path.read_text())
    data["collisions"] = [{"name": "dup", "kept": "dup.py", "dropped": ["vendor/dup.py"]}]
    index_path.write_text(json.dumps(data))
    (root / "scratch.py").write_text("import leaf\n")
    return root, index_path


class TestCliRemedies:
    """The real CLI reports every co-occurring remedy; ``--no-heal`` keeps the stale index in place."""

    @pytest.mark.parametrize("mode_flags", [pytest.param((), id="full"), pytest.param(("--compact",), id="compact")])
    def test_reports_stale_untracked_and_collision_remedies(
        self, stale_untracked_collision_repo, scan_query, mode_flags
    ):
        """Stale plus untracked plus collision yields three ordered remedies in the full and the compact block."""
        root, index_path = stale_untracked_collision_repo

        index = run_query(scan_query, root, index_path, "--no-heal", *mode_flags, "rdeps", "leaf")["index"]

        assert index["remedies"] == [
            {"reason": "stale", "remedy": _EXPECTED_REMEDY["stale"]},
            {"reason": "untracked", "remedy": _EXPECTED_REMEDY["untracked"]},
            {"reason": "collision", "remedy": _EXPECTED_REMEDY["collision"]},
        ]
        assert index["completeness_reason"] == "stale"
        assert _EXPECTED_REMEDY["collision"] in index["note"]
