"""Regression coverage for isolated change-impact fixture runtime admission."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _bench_common import change_impact_contracts as contracts
from _bench_codex import fixture_runtime


def _fixture_runtime_coordinate(source_root: Path, index_path: Path) -> dict[str, object]:
    """Return one complete immutable coordinate for a minimal copied fixture."""
    return {
        "source_fingerprint": contracts.source_fingerprint(source_root),
        "raw_index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "scan_version": 13,
        "index_scan_root": str(source_root),
    }


def _write_fixture_runtime(tmp_path: Path) -> tuple[Path, Path]:
    """Create a clean model-visible source tree and its fixture-local frozen index."""
    source_root = tmp_path / "repo"
    source_root.mkdir()
    (source_root / "impact.py").write_text("def quota(limit: int) -> int:\n    return limit\n", encoding="utf-8")
    index_path = source_root / ".cache" / "codemap" / "repo.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(
        json.dumps({"scan_version": 13, "scan_root": str(source_root), "modules": {}}), encoding="utf-8"
    )
    return source_root, index_path


def test_fixture_runtime_admission_requires_exact_source_and_index_coordinate(tmp_path: Path) -> None:
    """A copied fixture is admitted only when source bytes and index metadata match its coordinate."""
    source_root, index_path = _write_fixture_runtime(tmp_path)
    coordinate = _fixture_runtime_coordinate(source_root, index_path)

    admitted = fixture_runtime.validate_fixture_runtime(source_root, index_path, coordinate)

    assert admitted.source_fingerprint == coordinate["source_fingerprint"]
    (source_root / "impact.py").write_text("def quota(limit: int) -> int:\n    return limit + 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source fingerprint"):
        fixture_runtime.validate_fixture_runtime(source_root, index_path, coordinate)


def test_fixture_runtime_admission_rejects_index_root_drift(tmp_path: Path) -> None:
    """The cell index cannot describe a different source root even when its bytes are relocked."""
    source_root, index_path = _write_fixture_runtime(tmp_path)
    coordinate = _fixture_runtime_coordinate(source_root, index_path)
    index_path.write_text(
        json.dumps({"scan_version": 13, "scan_root": str(tmp_path / "other"), "modules": {}}), encoding="utf-8"
    )
    coordinate["raw_index_sha256"] = hashlib.sha256(index_path.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="scan root"):
        fixture_runtime.validate_fixture_runtime(source_root, index_path, coordinate)


def test_fixture_runtime_maps_native_stream_and_rechecks_coordinate(tmp_path: Path) -> None:
    """A fixture cell uses the structural home/stream seams and marks source drift as contamination."""
    source_root, index_path = _write_fixture_runtime(tmp_path)
    coordinate = _fixture_runtime_coordinate(source_root, index_path)
    prepared: list[dict[str, object]] = []
    cleaned: list[Path] = []

    class Home:
        """Provide the native home fields used by the fixture adapter."""

        env: dict[str, str] = {}
        coordination_path = tmp_path / "gate"
        codemap_launcher_path = None
        codemap_skill_path = None
        codemap_skill_sha256 = ""
        auth_provisioned = False

        def cleanup(self) -> None:
            """Record disposal of the isolated home."""
            cleaned.append(self.coordination_path)

    class Runner:
        """Expose only the existing structural runner seams used by fixture cells."""

        timeout = 10.0
        _auth_state = None

        def _prepare_verified_home(self, _arm: str, **kwargs: object) -> Home:
            """Record fixture admission instead of preparing a real disposable Codex home."""
            prepared.append(dict(kwargs))
            return Home()

        def build_command(self, prompt: str) -> list[str]:
            """Keep the generated prompt visible to the fake native transport."""
            return [prompt]

        def run_stream(self, _command: list[str], _env: dict[str, str], **_kwargs: object) -> str:
            """Return one successful native stream with complete usage evidence."""
            return json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3},
                }
            )

        def close(self) -> None:
            """Satisfy the runtime context close boundary."""
            return None

    native = SimpleNamespace(
        runtime=__import__("_bench_codex.runtime", fromlist=["runtime"]),
        _arm_compliance=lambda _arm, _parsed: None,
        _assert_coordination_root_idle=lambda _path: None,
        _cleanup_coordination_root=lambda _path: [],
    )
    runtime = fixture_runtime.FixtureCodexRuntime(
        runner=Runner(),
        native=native,
        source_root=source_root,
        index_path=index_path,
        coordinate=coordinate,
        arm_envelope=lambda _arm: "fixture treatment",
    )

    row = runtime.run_cell({"id": "CI-01"}, "A_plain", "report only")

    assert row["success"] is True
    assert row["usage_complete"] is True
    assert row["report_text"] == ""
    assert prepared == [{"fixture_runtime_coordinate": coordinate}]
    assert cleaned == [tmp_path / "gate"]
