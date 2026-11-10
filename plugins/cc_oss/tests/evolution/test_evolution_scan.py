"""End-to-end tests for the evolution orchestrator: full scan, resume, rescore, overrides, lock and extend.

Each test builds a small, fully dated fixture history with a Keep a Changelog file whose entries name commit SHAs, runs
the ``scope`` and ``run`` subcommands in-process, and checks the store and report. Store-equality tests compare the
scored cells of an incremental store with those of a single ``--full`` scan of the same inputs, which is the plan's
equivalence claim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import evolution_scan
import pytest
from _evolution_common import read_jsonl, resolve_store_root
from _evolution_repos import ChangelogSection, RepoBuilder
from evolution_store import acquire_lock

#: Exit code the orchestrator uses for a ``! BLOCKED`` stop.
_BLOCKED = 2


def _history(repo: RepoBuilder) -> dict[str, str]:
    """Build three releases: core (0.1.0), parser + renderer (0.2.0), two fixes (0.3.0); return named SHAs."""
    repo.write("src/app.py", "def core():\n    value = 1\n    return value\n")
    shas = {"core": repo.commit("feat: add core", day=0)}
    repo.tag("v0.1.0", day=0)
    repo.write("src/parse.py", "def parse(text):\n    parts = text.split(',')\n    return parts\n")
    shas["parser"] = repo.commit("feat: add parser", day=1)
    repo.write("src/render.py", "def render(value):\n    text = str(value)\n    return text\n")
    shas["renderer"] = repo.commit("feat: add renderer", day=2)
    repo.tag("v0.2.0", day=3)
    repo.write("src/parse.py", "def parse(text):\n    parts = text.strip().split(',')\n    return parts\n")
    shas["fix_parser"] = repo.commit("fix: strip parser input", day=4)
    repo.write("src/app.py", "def core():\n    value = 2\n    return value\n")
    shas["fix_core"] = repo.commit("fix: core value", day=5)
    _changelog(repo, shas, extra=None)
    repo.commit("docs: changelog", day=6)
    repo.tag("v0.3.0", day=6)
    return shas


def _changelog(repo: RepoBuilder, shas: dict[str, str], extra: ChangelogSection | None) -> None:
    """Write the changelog for the fixture history, optionally with a newer section on top."""
    short = repo.short
    sections = [
        ChangelogSection(
            "0.3.0",
            day=6,
            fixed=[f"Strip parser input ({short(shas['fix_parser'])})", f"Core value ({short(shas['fix_core'])})"],
        ),
        ChangelogSection(
            "0.2.0", day=3, added=[f"CSV parser ({short(shas['parser'])})", f"Renderer ({short(shas['renderer'])})"]
        ),
        ChangelogSection("0.1.0", day=0, added=[f"Core ({short(shas['core'])})"]),
    ]
    repo.write_changelog(([extra] if extra else []) + sections)


@pytest.fixture
def scanned(evo_repo: RepoBuilder, tmp_sentinels: Path) -> dict[str, object]:
    """Return a fixture repo with its first full scan already run (scope: all)."""
    shas = _history(evo_repo)
    assert (
        evolution_scan.main(["scope", "--args", "evolution v0.1.0->v0.3.0", "--project-root", str(evo_repo.path)]) == 0
    )
    root = Path(_sentinel_value(tmp_sentinels, "analyse-evo-root"))
    assert _run(root, evo_repo.path) == 0
    return {"repo": evo_repo, "root": root, "shas": shas, "tmp": tmp_sentinels}


def _sentinel_value(tmp: Path, name: str) -> str:
    """Read a session sentinel written under the isolated TMPDIR."""
    return (tmp / f"{name}-shared").read_text(encoding="utf-8").strip()


def _run(root: Path, project: Path, *extra: str) -> int:
    """Run the ``run`` subcommand for a store root."""
    return evolution_scan.main(
        ["run", "--root", str(root), "--project-root", str(project), "--today", "2024-02-01", *extra]
    )


def _cells(root: Path) -> dict[str, list[tuple[str, float, bool]]]:
    """Return the last scored row per fix as sorted ``(column_id, soft_share, binary)`` triples."""
    records, _ = read_jsonl(root / "store" / "rows.jsonl")
    last = {rec["fix_id"]: rec for rec in records}
    return {
        fix_id: sorted((c["column_id"], round(c["soft_share"], 6), bool(c.get("binary"))) for c in rec["cells"])
        for fix_id, rec in last.items()
    }


def _binary_columns(root: Path) -> set[str]:
    """Return every real column any fix links to in the binary view."""
    return {col for cells in _cells(root).values() for col, _, binary in cells if binary}


class TestFullScan:
    """A first scan over three releases."""

    def test_links_parser_fix_to_parser_feature(self, scanned: dict[str, object]) -> None:
        """The parser fix links to the 0.2.0 "CSV parser" column, and the core fix to the 0.1.0 "Core" column.

        This is the feature-level lineage the user asked for: each fix names the feature it repairs.
        """
        root = scanned["root"]
        columns = {rec["column_id"]: rec for rec in read_jsonl(root / "store" / "columns.jsonl")[0]}
        titles = {columns[col]["title"].split(" (")[0] for col in _binary_columns(root)}
        assert titles == {"CSV parser", "Core"}

    def test_writes_report_with_header_and_sentinel(self, scanned: dict[str, object]) -> None:
        """The report opens with a YAML header, and the report-file sentinel names it for the analyse hook.

        The Step 6a follow-up gate reads that sentinel; a missing or wrong path would block delivery.
        """
        report = Path(_sentinel_value(scanned["tmp"], "analyse-report-file"))
        text = report.read_text(encoding="utf-8")
        assert text.startswith("---\nTitle: ")
        assert (report.parent / "report.html").is_file()
        assert "provisional" in text


class TestStoreEquivalence:
    """Incremental runs equal a single ``--full`` scan of the same inputs."""

    def test_resume_after_torn_row_equals_full_scan(self, scanned: dict[str, object]) -> None:
        """A crash that tore the last row line is resumed, and the result equals a ``--full`` rebuild.

        The torn line is the write-protocol boundary a killed run leaves behind.
        """
        root, project = scanned["root"], scanned["repo"].path
        rows = root / "store" / "rows.jsonl"
        rows.write_bytes(rows.read_bytes()[:-20])
        assert _run(root, project) == 0
        resumed = _cells(root)
        assert _run(root, project, "--full", "true") == 0
        assert resumed == _cells(root)

    def test_rescore_needs_no_blame(self, scanned: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
        """A rerun with unchanged inputs reuses every evidence record, so blame is never called.

        Rescoring from stored evidence is what makes revisions cheap on a large history.
        """

        def _fail(*_args: object, **_kwargs: object) -> dict[str, object]:
            """Fail the test when blame runs."""
            raise AssertionError("blame must not run on an unchanged store")

        monkeypatch.setattr(evolution_scan, "collect_evidence", _fail)
        before = _cells(scanned["root"])
        assert _run(scanned["root"], scanned["repo"].path) == 0
        assert _cells(scanned["root"]) == before

    def test_rescore_reuses_stored_entries(self, scanned: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
        """A rerun with unchanged inputs reuses the stored entries, so no changelog linkage or GitHub lookup runs.

        Entry building (PR/issue lookups, symbol footprints) dominated a real rescore at over 300 s; a rescore must cost
        only scoring and rendering.
        """

        def _fail(*_args: object, **_kwargs: object) -> dict[str, object]:
            """Fail the test when entries are rebuilt."""
            raise AssertionError("entries must not be rebuilt on an unchanged repository")

        monkeypatch.setattr(evolution_scan, "build_entries", _fail)
        before = _cells(scanned["root"])
        assert _run(scanned["root"], scanned["repo"].path) == 0
        assert _cells(scanned["root"]) == before

    def test_uncommitted_changelog_edit_rebuilds_entries(
        self, scanned: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An uncommitted changelog edit in a user checkout invalidates the cached entries.

        HEAD and the tag map are unchanged here, so only the working-tree digest can detect the new text.
        """
        calls: list[object] = []
        real = evolution_scan.build_entries

        def _spy(*args: object, **kwargs: object) -> dict[str, object]:
            """Record the call and delegate to the real builder."""
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(evolution_scan, "build_entries", _spy)
        changelog = scanned["repo"].path / "CHANGELOG.md"
        changelog.write_text(changelog.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        assert _run(scanned["root"], scanned["repo"].path) == 0
        assert len(calls) == 1

    def test_override_survives_full_rebuild(self, scanned: dict[str, object]) -> None:
        """A ``deny`` override removes a link and stays applied after ``--full`` deletes ``store/``.

        Overrides are the only user-authored data in the store, so a rebuild must never lose them.
        """
        root = scanned["root"]
        linked = sorted(_binary_columns(root))
        fix_id = next(fid for fid, cells in _cells(root).items() if any(col == linked[0] and b for col, _, b in cells))
        (root / "overrides.csv").write_text(
            f"fix_id,column_id,action,note\n{fix_id},{linked[0]},deny,wrong link\n", encoding="utf-8"
        )
        assert _run(root, scanned["repo"].path, "--full", "true") == 0
        assert linked[0] not in _binary_columns(root)
        assert (root / "overrides.csv").is_file()

    def test_extend_adds_new_release_rows(self, scanned: dict[str, object]) -> None:
        """A new release with a renderer fix adds one row; older rows keep the same cells.

        Extending must not change rows whose inputs did not change.
        """
        repo, root, shas = scanned["repo"], scanned["root"], scanned["shas"]
        before = _cells(root)
        repo.write("src/render.py", "def render(value):\n    text = repr(value)\n    return text\n")
        shas["fix_render"] = repo.commit("fix: render repr", day=7)
        _changelog(
            repo, shas, ChangelogSection("0.4.0", day=8, fixed=[f"Render repr ({repo.short(shas['fix_render'])})"])
        )
        repo.commit("docs: changelog 0.4.0", day=8)
        repo.tag("v0.4.0", day=8)
        assert (
            evolution_scan.main(["scope", "--args", "evolution v0.1.0->v0.4.0", "--project-root", str(repo.path)]) == 0
        )
        assert _run(root, repo.path) == 0
        after = _cells(root)
        assert {fid: after[fid] for fid in before} == before
        assert len(after) == len(before) + 1


class TestGuards:
    """Stops that protect the store."""

    def test_held_lock_blocks_run(self, scanned: dict[str, object], capsys: pytest.CaptureFixture[str]) -> None:
        """A second run while the lock is held stops with ``! BLOCKED`` and names the holder.

        Two writers on one store would interleave rows; the lock must refuse the second.
        """
        root = scanned["root"]
        acquire_lock(resolve_store_root(root.parents[1], [root.parent.name, root.name]))
        capsys.readouterr()
        assert _run(root, scanned["repo"].path) == _BLOCKED
        out = capsys.readouterr().out
        assert "LOCK_HELD=" in out
        assert "! BLOCKED" in out

    def test_missing_scope_blocks_run(self, evo_repo: RepoBuilder, tmp_sentinels: Path) -> None:
        """Without a range and without a saved scope the run stops instead of guessing a scope.

        A background run must never pick the scan scope on the user's behalf.
        """
        _history(evo_repo)
        assert evolution_scan.main(["scope", "--args", "evolution", "--project-root", str(evo_repo.path)]) == 0
        root = Path(_sentinel_value(tmp_sentinels, "analyse-evo-root"))
        assert _run(root, evo_repo.path) == _BLOCKED

    def test_scope_asks_when_no_range_given(
        self, evo_repo: RepoBuilder, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The scope step reports the release count and asks for the scope when none was given.

        The skill asks the user only when ``ASK_SCOPE=true``.
        """
        _history(evo_repo)
        assert evolution_scan.main(["scope", "--args", "evolution", "--project-root", str(evo_repo.path)]) == 0
        lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines() if "=" in line)
        assert (lines["ASK_SCOPE"], lines["RELEASES"], lines["LAST"]) == ("true", "3", "0.3.0")


def test_scope_answer_file_drives_run(evo_repo: RepoBuilder, tmp_sentinels: Path) -> None:
    """The user's answer written to ``scope-choice.txt`` is used and saved in the manifest.

    This is the path the skill takes after the ``evolution-scope`` question.
    """
    _history(evo_repo)
    assert evolution_scan.main(["scope", "--args", "evolution", "--project-root", str(evo_repo.path)]) == 0
    root = Path(_sentinel_value(tmp_sentinels, "analyse-evo-root"))
    (root / "scope-choice.txt").write_text("last:2\n", encoding="utf-8")
    assert _run(root, evo_repo.path) == 0
    manifest = json.loads((root / "store" / "manifest.json").read_text(encoding="utf-8"))
    assert (manifest["scope"]["kind"], manifest["scope"]["from"]) == ("last", "0.2.0")


class TestRenderRequest:
    """``render-request`` writes request items equal to what a run writes, without a store or a clone."""

    _SPEC: ClassVar[list[dict[str, Any]]] = [
        {
            "fix_id": "fix-1",
            "fix_entry": {"entry_id": "e-1", "kind": "fixed", "release": "0.3.0", "text": "Strip parser input"},
            "candidates": [
                {"column_id": "col-render", "title": "Add renderer", "kind": "added", "release": "0.2.0"},
                {"column_id": "col-parse", "title": "Add parser", "kind": "added", "release": "0.2.0"},
            ],
            "evidence": {"status": "ok", "files": ["src/parse.py"]},
        }
    ]

    def test_items_equal_production_builder(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Each item carries the production prompt, its reuse key and the candidate IDs in request order."""
        spec, out = tmp_path / "spec.json", tmp_path / "request.json"
        spec.write_text(json.dumps(self._SPEC), encoding="utf-8")
        assert evolution_scan.main(["render-request", "--spec", str(spec), "--out", str(out)]) == 0
        entry = self._SPEC[0]
        expected = evolution_scan.request_item("fix-1", entry["fix_entry"], entry["candidates"], entry["evidence"])
        assert json.loads(out.read_text(encoding="utf-8")) == [expected]
        assert expected["candidates"] == ["col-render", "col-parse"]
        assert "ITEMS=1" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "spec",
        [
            pytest.param([], id="empty-list"),
            pytest.param({"fix_id": "x"}, id="not-a-list"),
            pytest.param([{"fix_id": "x", "fix_entry": {"entry_id": "e"}, "candidates": []}], id="no-candidates"),
            pytest.param([{"fix_id": "x", "fix_entry": {}, "candidates": [{"column_id": "c"}]}], id="no-entry-id"),
            pytest.param(
                [{"fix_id": " ", "fix_entry": {"entry_id": "e"}, "candidates": [{"column_id": "c"}]}], id="blank-fix-id"
            ),
            pytest.param(
                [{"fix_id": "x", "fix_entry": {"entry_id": "e"}, "candidates": ["c"]}], id="non-dict-candidate"
            ),
        ],
    )
    def test_bad_spec_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], spec: object) -> None:
        """A malformed spec stops with a ``! BLOCKED`` line and writes no request file."""
        path, out = tmp_path / "spec.json", tmp_path / "request.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        assert evolution_scan.main(["render-request", "--spec", str(path), "--out", str(out)]) == _BLOCKED
        assert "! BLOCKED" in capsys.readouterr().out
        assert not out.exists()

    def test_malformed_json_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A spec file that is not JSON stops with a ``! BLOCKED`` line, not a traceback."""
        path, out = tmp_path / "spec.json", tmp_path / "request.json"
        path.write_text("{not json", encoding="utf-8")
        assert evolution_scan.main(["render-request", "--spec", str(path), "--out", str(out)]) == _BLOCKED
        assert "! BLOCKED" in capsys.readouterr().out
        assert not out.exists()

    def test_missing_spec_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A spec path that does not exist stops with a ``! BLOCKED`` line, not a traceback."""
        path, out = tmp_path / "absent.json", tmp_path / "request.json"
        assert evolution_scan.main(["render-request", "--spec", str(path), "--out", str(out)]) == _BLOCKED
        assert "! BLOCKED" in capsys.readouterr().out
        assert not out.exists()


class TestEntriesKey:
    """The entries cache key follows the entries stage's own inputs only."""

    _INDEX: ClassVar[dict[str, Any]] = {"fingerprint": "idx", "head": {"sha": "abc"}}

    @pytest.mark.parametrize("stage", ["evidence", "scores", "adjudication_prompt", "column_map"])
    def test_later_stage_bump_keeps_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
        """Bumping a later stage's algorithm version leaves the entries key unchanged.

        A prompt-only or scoring-only change must not repeat the changelog parse and GitHub linkage lookups.
        """
        before = evolution_scan._entries_key(tmp_path, self._INDEX, gh_available=False)
        monkeypatch.setitem(evolution_scan.ALGO_VERSIONS, stage, evolution_scan.ALGO_VERSIONS[stage] + 1)
        assert evolution_scan._entries_key(tmp_path, self._INDEX, gh_available=False) == before

    def test_entries_bump_changes_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bumping the entries algorithm version invalidates the cached entries."""
        before = evolution_scan._entries_key(tmp_path, self._INDEX, gh_available=False)
        monkeypatch.setitem(evolution_scan.ALGO_VERSIONS, "entries", evolution_scan.ALGO_VERSIONS["entries"] + 1)
        assert evolution_scan._entries_key(tmp_path, self._INDEX, gh_available=False) != before
