"""Regression coverage for isolated change-impact fixture runtime admission."""

from __future__ import annotations

import hashlib
import json
import ntpath
import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from _bench_codex import fixture_runtime
from _bench_codex import runtime as codex_runtime
from _bench_codex.structural import diff_impact
from _bench_common import change_impact_contracts as contracts
from _bench_common import change_impact_stage as stage

_QUOTA_SOURCE = "def quota(limit: int) -> int:\n    return limit\n"
_DRIFTED_QUOTA_SOURCE = "def quota(limit: int) -> int:\n    return limit + 1\n"
_COMPLETED_STREAM = json.dumps(
    {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3}}
)


def _fixture_runtime_coordinate(source_root: Path, index_path: Path) -> dict[str, object]:
    """Return one complete immutable coordinate for a minimal copied fixture."""
    metadata = json.loads(index_path.read_bytes())
    return {
        "source_fingerprint": contracts.source_fingerprint(source_root),
        "raw_index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "scan_version": metadata["scan_version"],
        "index_scan_root": metadata["scan_root"],
    }


class _Home:
    """Provide the native home fields used by the fixture adapter and record its disposal."""

    codemap_launcher_path = None
    codemap_skill_path = None
    codemap_skill_sha256 = ""
    auth_provisioned = False

    def __init__(self, coordination_path: Path) -> None:
        self.env: dict[str, str] = {}
        self.coordination_path = coordination_path
        self.cleaned: list[Path] = []

    def cleanup(self) -> None:
        """Record disposal of the isolated home."""
        self.cleaned.append(self.coordination_path)


class _Runner:
    """Expose only the existing structural runner seams used by fixture cells."""

    timeout = 10.0
    _auth_state = None

    def __init__(self, home: _Home) -> None:
        self.home = home
        self.prepared: list[dict[str, object]] = []

    def _prepare_verified_home(self, _arm: str, **kwargs: object) -> _Home:
        """Record fixture admission instead of preparing a real disposable Codex home."""
        self.prepared.append(dict(kwargs))
        return self.home

    def build_command(self, prompt: str) -> list[str]:
        """Keep the generated prompt visible to the fake native transport."""
        return [prompt]

    def run_stream(self, _command: list[str], _env: dict[str, str], **_kwargs: object) -> str:
        """Return one successful native stream with complete usage evidence."""
        return _COMPLETED_STREAM

    def close(self) -> None:
        """Satisfy the runtime context close boundary."""
        return None


class _DriftingRunner(_Runner):
    """Runner whose native stream rewrites the copied fixture source while the cell is running."""

    def __init__(self, home: _Home, source_file: Path) -> None:
        super().__init__(home)
        self.source_file = source_file

    def run_stream(self, command: list[str], env: dict[str, str], **kwargs: object) -> str:
        """Mutate the fixture source, then return the successful stream."""
        self.source_file.write_text(_DRIFTED_QUOTA_SOURCE, encoding="utf-8")
        return super().run_stream(command, env, **kwargs)


def _runtime_for(runner: _Runner, files: SimpleNamespace) -> fixture_runtime.FixtureCodexRuntime:
    """Build a fixture runtime over the stub runner and inert native coordination seams."""
    native = SimpleNamespace(
        runtime=codex_runtime,
        _arm_compliance=lambda _arm, _parsed: None,
        _assert_coordination_root_idle=lambda _path: None,
        _cleanup_coordination_root=lambda _path: [],
    )
    return fixture_runtime.FixtureCodexRuntime(
        runner=runner,
        native=native,
        source_root=files.source_root,
        index_path=files.index_path,
        coordinate=files.coordinate,
        arm_envelope=lambda _arm: "fixture treatment",
    )


@pytest.fixture
def files(tmp_path: Path) -> SimpleNamespace:
    """Clean model-visible source tree with its fixture-local frozen index and matching coordinate."""
    source_root = tmp_path / "repo"
    source_root.mkdir()
    source_file = source_root / "impact.py"
    source_file.write_text(_QUOTA_SOURCE, encoding="utf-8")
    index_path = source_root / ".cache" / "codemap" / "repo.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(
        json.dumps({"scan_version": 13, "scan_root": str(source_root), "modules": {}}), encoding="utf-8"
    )
    coordinate = _fixture_runtime_coordinate(source_root, index_path)
    return SimpleNamespace(
        source_root=source_root, source_file=source_file, index_path=index_path, coordinate=coordinate
    )


@pytest.fixture
def scanned_files() -> Iterator[SimpleNamespace]:
    """Scan the real source fixture in a fresh disposable checkout."""
    with stage._fixture_workspace() as (source_root, index_path):
        yield SimpleNamespace(
            source_root=source_root,
            index_path=index_path,
            coordinate=_fixture_runtime_coordinate(source_root, index_path),
        )


class TestFixtureRuntime:
    """Admission and cell execution of the copied change-impact fixture runtime."""

    def test_admits_matching_coordinate(self, files: SimpleNamespace) -> None:
        """A copied fixture is admitted when its source bytes and index metadata match the coordinate.

        The fixture tree is untouched since its coordinate was computed, so admission returns that same source
        fingerprint.
        """
        admitted = fixture_runtime.validate_fixture_runtime(files.source_root, files.index_path, files.coordinate)

        assert admitted.source_fingerprint == files.coordinate["source_fingerprint"]
        assert admitted.scan_version == 13
        assert admitted.index_scan_root == str(files.source_root)

    @pytest.mark.integration
    def test_admits_fresh_scans_at_distinct_roots(self, scanned_files: SimpleNamespace) -> None:
        """Admit schema-14 indexes scanned at two distinct roots with their own root identities."""
        first = fixture_runtime.validate_fixture_runtime(
            scanned_files.source_root, scanned_files.index_path, scanned_files.coordinate
        )
        with stage._fixture_workspace() as (second_root, second_index):
            second_coordinate = _fixture_runtime_coordinate(second_root, second_index)
            second = fixture_runtime.validate_fixture_runtime(second_root, second_index, second_coordinate)

            assert scanned_files.source_root != second_root
            assert first.scan_version == second.scan_version == 14
            assert first.index_scan_root == second.index_scan_root == "."
            assert first.source_fingerprint == second.source_fingerprint
            assert first.raw_index_sha256 != second.raw_index_sha256

    @pytest.mark.integration
    def test_rejects_copied_index_with_foreign_root_key(self, scanned_files: SimpleNamespace) -> None:
        """Refuse another checkout's index even when source and relocked byte coordinates match."""
        with stage._fixture_workspace() as (second_root, second_index):
            second_index.write_bytes(scanned_files.index_path.read_bytes())
            coordinate = _fixture_runtime_coordinate(second_root, second_index)
            assert coordinate["source_fingerprint"] == scanned_files.coordinate["source_fingerprint"]
            assert coordinate["raw_index_sha256"] == scanned_files.coordinate["raw_index_sha256"]
            assert second_root != scanned_files.source_root

            with pytest.raises(ValueError, match="fixture runtime index scan root drifted"):
                fixture_runtime.validate_fixture_runtime(second_root, second_index, coordinate)

    @pytest.mark.integration
    def test_rejects_missing_root_key(self, scanned_files: SimpleNamespace) -> None:
        """Relative root metadata requires the root key even when index bytes are relocked."""
        payload = json.loads(scanned_files.index_path.read_bytes())
        del payload["scan_root_key"]
        scanned_files.index_path.write_bytes(json.dumps(payload).encode("utf-8"))
        coordinate = _fixture_runtime_coordinate(scanned_files.source_root, scanned_files.index_path)

        with pytest.raises(ValueError, match="fixture runtime index scan root drifted"):
            fixture_runtime.validate_fixture_runtime(scanned_files.source_root, scanned_files.index_path, coordinate)

    @pytest.mark.integration
    @pytest.mark.parametrize("root_key", [None, "", "a" * 63, "A" * 64, "0" * 64])
    def test_rejects_invalid_root_key(self, scanned_files: SimpleNamespace, root_key: str | None) -> None:
        """Relative indexes admit only the exact lowercase root digest."""
        payload = json.loads(scanned_files.index_path.read_bytes())
        payload["scan_root_key"] = root_key
        scanned_files.index_path.write_bytes(json.dumps(payload).encode("utf-8"))
        coordinate = _fixture_runtime_coordinate(scanned_files.source_root, scanned_files.index_path)

        with pytest.raises(ValueError, match="fixture runtime index scan root drifted"):
            fixture_runtime.validate_fixture_runtime(scanned_files.source_root, scanned_files.index_path, coordinate)

    @pytest.mark.integration
    def test_rejects_relative_index_root_drift(self, scanned_files: SimpleNamespace) -> None:
        """A matching root key never admits a different index root even with relocked bytes."""
        payload = json.loads(scanned_files.index_path.read_bytes())
        payload["scan_root"] = "other"
        scanned_files.index_path.write_bytes(json.dumps(payload).encode("utf-8"))
        coordinate = _fixture_runtime_coordinate(scanned_files.source_root, scanned_files.index_path)

        with pytest.raises(ValueError, match="fixture runtime index scan root drifted"):
            fixture_runtime.validate_fixture_runtime(scanned_files.source_root, scanned_files.index_path, coordinate)

    @pytest.mark.integration
    def test_rejects_relative_coordinate_root_drift(self, scanned_files: SimpleNamespace) -> None:
        """A matching root key never admits a coordinate whose root differs from the index."""
        coordinate = {**scanned_files.coordinate, "index_scan_root": "other"}

        with pytest.raises(ValueError, match="fixture runtime index scan root drifted"):
            fixture_runtime.validate_fixture_runtime(scanned_files.source_root, scanned_files.index_path, coordinate)

    @pytest.mark.integration
    def test_admits_windows_normalized_root_key(
        self, scanned_files: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Use Codemap's Windows separator and case normalization for resolved root digests."""
        root = tmp_path / "MiXeD-Straße"
        shutil.copytree(scanned_files.source_root, root)
        index_path = root / ".cache" / "codemap" / f"{root.name}.json"
        payload = json.loads(scanned_files.index_path.read_bytes())
        identity = ntpath.normcase(str(root.resolve())).casefold()
        payload["scan_root_key"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        index_path.write_bytes(json.dumps(payload).encode("utf-8"))
        coordinate = _fixture_runtime_coordinate(root, index_path)
        monkeypatch.setattr(fixture_runtime, "os", SimpleNamespace(name="nt"), raising=False)

        admitted = fixture_runtime.validate_fixture_runtime(root, index_path, coordinate)

        assert admitted.index_scan_root == "."
        assert admitted.raw_index_sha256 == coordinate["raw_index_sha256"]

    @pytest.mark.integration
    def test_structural_admission_accepts_fresh_scanned_fixture(self, scanned_files: SimpleNamespace) -> None:
        """Admit a fresh relative-root index through the native structural runtime gate."""
        diff_impact._validate_locked_runtime(
            scanned_files.source_root,
            scanned_files.index_path,
            "C_strict",
            fixture_runtime_coordinate=scanned_files.coordinate,
        )

    @pytest.mark.integration
    def test_cell_admits_fresh_scanned_fixture(self, tmp_path: Path, scanned_files: SimpleNamespace) -> None:
        """Run a freshly scanned fixture through the adapter with a stubbed runner and transport."""
        home = _Home(tmp_path / "gate")
        runner = _Runner(home)

        row = _runtime_for(runner, scanned_files).run_cell({"id": "CI-01"}, "A_plain", "report only")

        assert row["success"] is True
        assert runner.prepared == [{"fixture_runtime_coordinate": scanned_files.coordinate}]
        assert home.cleaned == [tmp_path / "gate"]

    def test_rejects_source_drift(self, files: SimpleNamespace) -> None:
        """Admission fails once the copied source bytes differ from the coordinate fingerprint.

        One source file is edited after the coordinate was locked, which must surface as a source fingerprint mismatch
        rather than a silently admitted fixture.
        """
        files.source_file.write_text(_DRIFTED_QUOTA_SOURCE, encoding="utf-8")

        with pytest.raises(ValueError, match="source fingerprint"):
            fixture_runtime.validate_fixture_runtime(files.source_root, files.index_path, files.coordinate)

    def test_rejects_index_root_drift(self, tmp_path: Path, files: SimpleNamespace) -> None:
        """The cell index cannot describe a different source root even when its bytes are relocked.

        The index scan root is rewritten to another directory and the coordinate's index hash is refreshed, so only the
        scan-root check can catch the substitution.
        """
        files.index_path.write_text(
            json.dumps({"scan_version": 13, "scan_root": str(tmp_path / "other"), "modules": {}}), encoding="utf-8"
        )
        files.coordinate["raw_index_sha256"] = hashlib.sha256(files.index_path.read_bytes()).hexdigest()

        with pytest.raises(ValueError, match="scan root"):
            fixture_runtime.validate_fixture_runtime(files.source_root, files.index_path, files.coordinate)

    def test_cell_maps_native_stream_to_successful_row(self, tmp_path: Path, files: SimpleNamespace) -> None:
        """A fixture cell uses the structural home and stream seams and returns a clean, usage-complete row.

        The stub runner emits one completed turn with token usage; the cell must admit the coordinate when preparing the
        home, report success, and dispose of the coordination home afterwards.
        """
        home = _Home(tmp_path / "gate")
        runner = _Runner(home)

        row = _runtime_for(runner, files).run_cell({"id": "CI-01"}, "A_plain", "report only")

        assert row["success"] is True
        assert row["usage_complete"] is True
        assert row["report_text"] == ""
        assert runner.prepared == [{"fixture_runtime_coordinate": files.coordinate}]
        assert home.cleaned == [tmp_path / "gate"]

    def test_cell_marks_source_drift_as_contamination(self, tmp_path: Path, files: SimpleNamespace) -> None:
        """A fixture source edited while the cell runs turns the row into a runtime contamination.

        The native stream itself succeeds; the coordinate recheck after the stream is what must fail the row, otherwise
        a model that rewrites the copied fixture would score as a clean cell.
        """
        home = _Home(tmp_path / "gate")
        runner = _DriftingRunner(home, files.source_file)

        row = _runtime_for(runner, files).run_cell({"id": "CI-01"}, "A_plain", "report only")

        assert row["success"] is False
        assert row["error_type"] == "runtime_contamination"
        assert "source fingerprint" in row["error"]
        assert home.cleaned == [tmp_path / "gate"]
