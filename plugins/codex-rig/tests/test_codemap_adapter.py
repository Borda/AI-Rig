"""Acceptance and unit checks for the optional codemap-py structural-context adapter."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = PLUGIN_ROOT / "shared" / "codemap_adapter.py"


def _load_adapter() -> ModuleType:
    """Load the adapter module directly, mirroring this suite's other shared-script tests."""
    specification = importlib.util.spec_from_file_location("codemap_adapter", ADAPTER_PATH)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _write_fake_codemap_py(bin_dir: Path, script_body: str) -> None:
    """Install a fake `codemap-py` executable on a directory meant to be prepended to PATH.

    Uses an absolute-path shebang (POSIX) rather than `/usr/bin/env bash`/python, so the fake binary still runs when a
    test intentionally narrows `PATH` to prove absence/isolation.
    """
    if os.name == "nt":
        fake = bin_dir / "codemap-py.bat"
        fake.write_text(f'@echo off\r\n"{sys.executable}" "{bin_dir / "codemap_py_fake.py"}" %*\r\n')
    else:
        fake = bin_dir / "codemap-py"
        fake.write_text(f"#!{sys.executable}\n{script_body}")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (bin_dir / "codemap_py_fake.py").write_text(script_body)


_HEALTHY_DOCTOR = {
    "python": sys.executable,
    "version": "3.12.4",
    "implementation": "cpython",
    "supported": True,
    "plugin_root": "/fake/codemap-py",
    "index_path": "/fake/codemap-py/.cache/codemap/proj.json",
}

_CLEAN_QUERY = {"index": {"query_complete": True, "not_covered": [], "degraded": 0, "stale": False}}
_DEGRADED_QUERY = {
    "index": {"query_complete": False, "not_covered": ["dynamic-dispatch"], "degraded": 0, "stale": False}
}
_STALE_QUERY = {"index": {"query_complete": True, "not_covered": [], "degraded": 0, "stale": True}}
_STALE_DEGRADED_QUERY = {
    "index": {"query_complete": False, "not_covered": ["dynamic-dispatch"], "degraded": 1, "stale": True}
}
# A provider new enough to report which index it opened, agreeing with what `doctor` resolved.
_AGREEING_QUERY = {
    "index": {
        "query_complete": True,
        "not_covered": [],
        "degraded": 0,
        "stale": False,
        "index_path": _HEALTHY_DOCTOR["index_path"],
    }
}
# The same healthy answer, but opened from a different index than `doctor` resolved: the two
# processes disagree about which index is this project's. Fabricated here rather than staged as a
# real double index, since the adapter's job is to report the disagreement, not to produce one.
_DIVERGENT_INDEX_PATH = "/fake/other-root/.cache/codemap/proj.json"
_DIVERGENT_QUERY = {
    "index": {
        "query_complete": True,
        "not_covered": [],
        "degraded": 0,
        "stale": False,
        "index_path": _DIVERGENT_INDEX_PATH,
    }
}


def _fake_script(doctor_payload: dict, doctor_exit: int, query_payload: dict, query_exit: int) -> str:
    """Build a fake ``codemap-py`` dispatcher that answers ``doctor --json`` and ``query <sub>``."""
    return f"""
import json, sys
if sys.argv[1:2] == ["doctor"]:
    print(json.dumps({doctor_payload!r} if False else {doctor_payload}))
    sys.exit({doctor_exit})
if sys.argv[1:2] == ["query"]:
    print(json.dumps({query_payload}))
    sys.exit({query_exit})
sys.exit(2)
"""


def test_probe_absent_when_codemap_py_not_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Report `absent` without running any subprocess when the binary is missing."""
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    result = adapter.probe_codemap()

    assert result.status == adapter.STATUS_ABSENT
    assert result.doctor is None


def test_probe_available_when_doctor_reports_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Report ``available`` once ``doctor --json`` returns a supported interpreter."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    result = adapter.probe_codemap()

    assert result.status == adapter.STATUS_AVAILABLE
    assert result.doctor is not None
    assert result.doctor.supported is True


def test_explicit_codemap_bin_wins_over_path_for_probe_and_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Use the explicit launcher consistently when PATH contains another codemap-py."""
    explicit_bin = tmp_path / "explicit"
    path_bin = tmp_path / "path"
    explicit_bin.mkdir()
    path_bin.mkdir()
    explicit_doctor = dict(_HEALTHY_DOCTOR, version="explicit")
    path_doctor = dict(_HEALTHY_DOCTOR, supported=False, version="path")
    _write_fake_codemap_py(explicit_bin, _fake_script(explicit_doctor, 0, _CLEAN_QUERY, 0))
    _write_fake_codemap_py(path_bin, _fake_script(path_doctor, 0, _CLEAN_QUERY, 1))
    explicit_launcher = explicit_bin / ("codemap-py.bat" if os.name == "nt" else "codemap-py")
    monkeypatch.setenv("CODEMAP_BIN", str(explicit_launcher))
    monkeypatch.setenv("PATH", str(path_bin))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    assert context.probe.doctor is not None
    assert context.probe.doctor.version == "explicit"
    assert context.status == adapter.STATUS_AVAILABLE
    assert context.queries[0].exit_code == 0


@pytest.mark.parametrize("invalid_kind", ["missing", "directory", "not-executable", "relative", "symlink"])
def test_invalid_explicit_codemap_bin_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid_kind: str
) -> None:
    """Reject a nonempty invalid, relative, or symlink configured launcher."""
    path_bin = tmp_path / "path"
    path_bin.mkdir()
    _write_fake_codemap_py(path_bin, _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0))
    configured = tmp_path / invalid_kind
    if invalid_kind == "directory":
        configured.mkdir()
    elif invalid_kind == "not-executable":
        configured.write_text("not executable", encoding="utf-8")
    elif invalid_kind == "relative":
        _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0))
        monkeypatch.chdir(tmp_path)
        configured = Path("codemap-py")
    elif invalid_kind == "symlink":
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        _write_fake_codemap_py(target_dir, _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0))
        configured.symlink_to(target_dir / ("codemap-py.bat" if os.name == "nt" else "codemap-py"))
    monkeypatch.setenv("CODEMAP_BIN", str(configured))
    monkeypatch.setenv("PATH", str(path_bin))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    assert context.status == adapter.STATUS_INCOMPATIBLE
    assert context.probe.launcher is None
    assert "CODEMAP_BIN" in context.probe.detail
    assert context.queries == ()


def test_empty_codemap_bin_falls_back_to_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Treat an empty launcher setting as absent and resolve codemap-py through PATH."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0))
    monkeypatch.setenv("CODEMAP_BIN", "")
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    assert context.status == adapter.STATUS_AVAILABLE
    expected_launcher = str(tmp_path / ("codemap-py.bat" if os.name == "nt" else "codemap-py"))
    assert os.path.normcase(context.probe.launcher) == os.path.normcase(expected_launcher)


def test_gather_context_resolves_once_and_reuses_launcher_for_compact_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep doctor and query on one launcher even if PATH changes after the probe."""
    adapter = _load_adapter()
    launcher = "/explicit/codemap-py"
    resolved: list[None] = []
    commands: list[list[str]] = []

    def _resolve():
        """Return the fixed launcher resolution and count resolution attempts."""
        resolved.append(None)
        return adapter.LauncherResolution(launcher, adapter.STATUS_AVAILABLE, "test launcher")

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Return healthy doctor/query payloads while recording command arguments."""
        commands.append(argv)
        if argv[1] == "doctor":
            monkeypatch.setenv("PATH", "/changed-after-doctor")
            return 0, _HEALTHY_DOCTOR, None
        return 0, _CLEAN_QUERY, None

    monkeypatch.setattr(adapter, "_resolve_codemap_executable", _resolve)
    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context("review")

    assert len(resolved) == 1
    assert commands == [[launcher, "doctor", "--json"], [launcher, "query", "--compact", "diff-impact"]]
    assert context.probe.launcher == launcher


def test_analysis_query_uses_only_supported_compact_deps_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent the managed analysis mapping from passing symbol-only flags to deps."""
    adapter = _load_adapter()
    launcher = "/explicit/codemap-py"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution(launcher, adapter.STATUS_AVAILABLE, "test launcher"),
    )

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Return the matching doctor or query fixture while recording arguments."""
        commands.append(argv)
        return (0, _HEALTHY_DOCTOR, None) if argv[1] == "doctor" else (0, _CLEAN_QUERY, None)

    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context("analysis", target="pkg.core")

    assert context.status == adapter.STATUS_AVAILABLE
    assert commands == [
        [launcher, "doctor", "--json"],
        [launcher, "query", "--compact", "central"],
        [launcher, "query", "--compact", "deps", "pkg.core"],
    ]


def test_probe_incompatible_when_interpreter_unsupported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Report `incompatible` when `doctor` marks the resolved interpreter unsupported."""
    unsupported = dict(_HEALTHY_DOCTOR, supported=False)
    _write_fake_codemap_py(tmp_path, _fake_script(unsupported, 0, _CLEAN_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    result = adapter.probe_codemap()

    assert result.status == adapter.STATUS_INCOMPATIBLE


def test_probe_incompatible_when_doctor_exits_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Report ``incompatible`` when ``doctor --json`` itself fails."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 1, _CLEAN_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    result = adapter.probe_codemap()

    assert result.status == adapter.STATUS_INCOMPATIBLE
    assert result.launcher is None


@pytest.mark.parametrize("extension", [".BAT", ".Cmd", ".EXE", ".com"])
def test_simulated_windows_configured_launchers_are_accepted_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extension: str
) -> None:
    """Accept Windows executable filename conventions without requiring a POSIX execute bit."""
    adapter = _load_adapter()
    launcher = tmp_path / f"codemap-py{extension}"
    launcher.write_text("launcher", encoding="utf-8")

    with monkeypatch.context() as context:
        context.setattr(adapter.os, "name", "nt")
        assert adapter._configured_launcher_is_valid(launcher)


def test_gather_context_absent_never_runs_queries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Absence is non-fatal and short-circuits before any query subprocess runs."""
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("implementation", target="pkg.mod")

    assert context.status == adapter.STATUS_ABSENT
    assert context.queries == ()


def test_gather_context_available_when_all_queries_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Report `available` when the probe is healthy and every mapped query is exhaustive."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("implementation", target="pkg.mod")

    assert context.status == adapter.STATUS_AVAILABLE
    assert len(context.queries) == 3  # rdeps, coupled, test-impact


def test_skip_route_persists_auditable_context_without_resolving_codemap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A localized edit records its skip decision without spawning Codemap work."""
    adapter = _load_adapter()

    def _unexpected_resolution() -> object:
        """Fail if the skip route attempts optional launcher resolution."""
        raise AssertionError("skip must not resolve a Codemap launcher")

    monkeypatch.setattr(adapter, "_resolve_codemap_executable", _unexpected_resolution)

    context = adapter.gather_structural_context("implementation", target="pkg.mod::edit", query_kind="skip")

    assert context.query_kind == "skip"
    assert context.status == adapter.STATUS_SKIPPED
    assert context.probe.status == adapter.STATUS_SKIPPED
    assert context.queries == ()


@pytest.mark.parametrize(
    ("query_kind", "target", "expected_target", "expected_query"),
    [
        pytest.param("central", "pkg.mod::edit", "pkg.mod::edit", ["central", "--top", "5"], id="central"),
        pytest.param(
            "callers", "pkg.mod::edit", "pkg.mod::edit", ["fn-rdeps", "pkg.mod::edit", "--exclude-tests"], id="callers"
        ),
        pytest.param("blast", "pkg.mod::edit", "pkg.mod::edit", ["fn-blast", "pkg.mod::edit"], id="blast"),
        pytest.param("dependencies", "pkg.mod::edit", "pkg.mod::edit", ["rdeps", "pkg.mod"], id="dependencies"),
        pytest.param(
            "test-impact", "pkg.mod::edit", "pkg.mod::edit", ["test-impact", "pkg.mod::edit"], id="test-impact"
        ),
        pytest.param("coupling", "pkg.mod::edit", "pkg.mod::edit", ["coupled"], id="coupling"),
    ],
)
def test_fact_routes_run_doctor_and_exactly_one_compact_query(
    monkeypatch: pytest.MonkeyPatch,
    query_kind: str,
    target: str,
    expected_target: str | None,
    expected_query: list[str],
) -> None:
    """Each explicit fact route bounds Codemap work to one doctor and one compact query."""
    adapter = _load_adapter()
    launcher = "/explicit/codemap-py"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution(launcher, adapter.STATUS_AVAILABLE, "test launcher"),
    )

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Return healthy doctor/query payloads for target-shape route checks."""
        commands.append(argv)
        return (0, _HEALTHY_DOCTOR, None) if argv[1] == "doctor" else (0, _CLEAN_QUERY, None)

    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context("implementation", target=target, query_kind=query_kind)

    assert context.status == adapter.STATUS_AVAILABLE
    assert context.query_kind == query_kind
    assert context.target == expected_target
    assert commands == [[launcher, "doctor", "--json"], [launcher, "query", "--compact", *expected_query]]
    assert len(context.queries) == 1


def test_fact_route_without_target_records_bounded_error_after_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fact route with no target does not guess or execute a query subprocess."""
    adapter = _load_adapter()
    launcher = "/explicit/codemap-py"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution(launcher, adapter.STATUS_AVAILABLE, "test launcher"),
    )

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Return only the healthy doctor payload for the targetless fact route."""
        commands.append(argv)
        return 0, _HEALTHY_DOCTOR, None

    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context("implementation", query_kind="callers")

    assert context.status == adapter.STATUS_DEGRADED
    assert commands == [[launcher, "doctor", "--json"]]
    assert context.queries[0].error == "target required, none supplied"


@pytest.mark.parametrize(
    ("query_kind", "target"),
    [
        pytest.param("callers", None, id="callers-none"),
        pytest.param("callers", "pkg.mod", id="callers-module-only"),
        pytest.param("callers", "pkg.mod::", id="callers-empty-symbol"),
        pytest.param("callers", "pkg.mod::edit::nested", id="callers-pkg.mod-edit-nested"),
        pytest.param("blast", None, id="blast-none"),
        pytest.param("blast", "pkg.mod", id="blast-pkg.mod"),
        pytest.param("blast", "::edit", id="blast-edit"),
        pytest.param("dependencies", None, id="dependencies-none"),
        pytest.param("dependencies", "pkg.mod::", id="dependencies-pkg.mod"),
        pytest.param("dependencies", "pkg.mod::edit::nested", id="dependencies-pkg.mod-edit-nested"),
        pytest.param("test-impact", None, id="test-impact-none"),
        pytest.param("test-impact", "::edit", id="test-impact-edit"),
    ],
)
def test_fact_routes_degrade_without_query_for_missing_or_malformed_target(
    monkeypatch: pytest.MonkeyPatch, query_kind: str, target: str | None
) -> None:
    """Required compact facts never infer missing, incomplete, or module-only symbol targets."""
    adapter = _load_adapter()
    launcher = "/explicit/codemap-py"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution(launcher, adapter.STATUS_AVAILABLE, "test launcher"),
    )

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Return healthy doctor/query payloads for malformed-target route checks."""
        commands.append(argv)
        return 0, _HEALTHY_DOCTOR, None

    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context("implementation", target=target, query_kind=query_kind)

    assert context.status == adapter.STATUS_DEGRADED
    assert context.target == target
    assert commands == [[launcher, "doctor", "--json"]]
    assert context.queries[0].error == "target required, none supplied"


def test_invalid_query_kind_fails_before_launcher_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject invalid routes at the public API boundary before optional work starts."""
    adapter = _load_adapter()

    def _unexpected_resolution() -> object:
        """Fail if invalid query-kind validation resolves the optional launcher."""
        raise AssertionError("invalid query kind must fail before resolving Codemap")

    monkeypatch.setattr(adapter, "_resolve_codemap_executable", _unexpected_resolution)

    with pytest.raises(ValueError, match="unknown query kind"):
        adapter.gather_structural_context("implementation", query_kind="not-a-route")


def test_gather_context_degraded_when_not_covered_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Report `degraded` when a query returns non-exhaustive completeness metadata."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _DEGRADED_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    assert context.status == adapter.STATUS_DEGRADED


def test_gather_context_stale_when_query_reports_stale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Report `stale` when a query's index block flags the index older than source."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _STALE_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("audit")

    assert context.status == adapter.STATUS_STALE


def test_gather_context_composes_stale_and_gap_reported_by_one_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep both caveats when a single query is stale and non-exhaustive at once."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _STALE_DEGRADED_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("audit")

    assert context.status == adapter.STATUS_STALE_DEGRADED
    assert context.to_dict()["status"] == "stale+degraded"


def test_gather_context_composes_stale_and_gap_split_across_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose caveats raised by different queries so neither batch member masks the other."""
    adapter = _load_adapter()
    launcher = "/explicit/codemap-py"
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution(launcher, adapter.STATUS_AVAILABLE, "test launcher"),
    )

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Select stale or degraded query evidence by subcommand while recording calls."""
        if argv[1] == "doctor":
            return 0, _HEALTHY_DOCTOR, None
        return (0, _STALE_QUERY, None) if argv[3] == "undocumented" else (0, _DEGRADED_QUERY, None)

    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context("audit")

    assert context.status == adapter.STATUS_STALE_DEGRADED
    assert [outcome.stale for outcome in context.queries] == [True, False]
    assert [outcome.query_complete for outcome in context.queries] == [True, False]


def test_query_records_the_index_path_the_provider_reported_for_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Record each query's own index path so provenance names the file that answered."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _AGREEING_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    assert [outcome.index_path for outcome in context.queries] == [_HEALTHY_DOCTOR["index_path"]]
    assert context.to_dict()["queries"][0]["index_path"] == _HEALTHY_DOCTOR["index_path"]
    # Agreement is not a divergence: the evidence list stays empty and is still serialized.
    assert context.index_path_divergence == ()
    assert context.to_dict()["index_path_divergence"] == []


def test_provider_without_index_path_records_none_and_claims_no_divergence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tolerate a provider predating the field: absence stays absent, never back-filled."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    assert context.status == adapter.STATUS_AVAILABLE
    assert [outcome.index_path for outcome in context.queries] == [None]
    assert context.to_dict()["queries"][0]["index_path"] is None
    # An unreported path must not be compared against the probe's — absence is not disagreement.
    assert context.index_path_divergence == ()


def test_divergent_index_path_is_recorded_as_evidence_without_changing_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Report a query that opened a different index than `doctor` resolved, and keep both paths.

    The status must stay `available`: the answers themselves were complete and fresh. Folding the disagreement into the
    status would assert which of the two processes was wrong, and would cost the reader the two paths that make the
    disagreement diagnosable.
    """
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _DIVERGENT_QUERY, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    assert context.status == adapter.STATUS_AVAILABLE
    assert [record.to_dict() for record in context.index_path_divergence] == [
        {
            "subcommand": "diff-impact",
            "doctor_index_path": _HEALTHY_DOCTOR["index_path"],
            "query_index_path": _DIVERGENT_INDEX_PATH,
        }
    ]
    # Neither path is reconciled away: the query keeps its own, the probe keeps its own.
    assert context.queries[0].index_path == _DIVERGENT_INDEX_PATH
    assert context.probe.doctor.index_path == _HEALTHY_DOCTOR["index_path"]


def test_not_indexed_exit_records_the_addressed_path_and_its_divergence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep the path a failed load addressed: a wrong index dir is exactly what diverges.

    A not-indexed exit raised by a failed load reports the path at the payload root rather than under `index`. That path
    is the only provenance such a run has, and comparing it is the case the field most needs to cover — the query never
    opened the index the probe found.
    """
    not_indexed = {"error": "index is not valid JSON", "path": _DIVERGENT_INDEX_PATH}
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, not_indexed, 3))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    outcome = context.queries[0]
    assert (outcome.exit_code, outcome.error) == (3, "target not indexed")
    assert outcome.index_path == _DIVERGENT_INDEX_PATH
    assert [record.query_index_path for record in context.index_path_divergence] == [_DIVERGENT_INDEX_PATH]


def test_not_indexed_exit_without_a_path_key_claims_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A not-indexed exit raised after a successful load carries no path, so none is invented."""
    not_indexed = {"error": "module not indexed", "module": "pkg.missing", "suggestions": []}
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, not_indexed, 3))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = _load_adapter()

    context = adapter.gather_structural_context("review")

    assert context.queries[0].index_path is None
    assert context.index_path_divergence == ()


def test_divergence_is_recorded_per_query_not_once_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attribute divergence to the query that diverged, leaving an agreeing sibling unaccused."""
    adapter = _load_adapter()
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution("/explicit/codemap-py", adapter.STATUS_AVAILABLE, "test launcher"),
    )

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Select agreeing or divergent query evidence by subcommand."""
        if argv[1] == "doctor":
            return 0, _HEALTHY_DOCTOR, None
        return (0, _AGREEING_QUERY, None) if argv[3] == "undocumented" else (0, _DIVERGENT_QUERY, None)

    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context("audit")

    assert [record.subcommand for record in context.index_path_divergence] == ["dead-modules"]
    assert context.status == adapter.STATUS_AVAILABLE


def test_gather_context_rejects_unknown_category(tmp_path: Path) -> None:
    """Refuse an undefined category rather than silently mapping it to an empty query set."""
    adapter = _load_adapter()

    with pytest.raises(ValueError, match="unknown category"):
        adapter.gather_structural_context("not-a-real-category")


@pytest.mark.parametrize(
    ("category", "expected_subcommands"),
    [
        pytest.param("analysis", ["central"], id="analysis-drops-deps"),
        pytest.param("implementation", ["coupled"], id="implementation-drops-rdeps-and-test-impact"),
    ],
)
def test_standard_batch_without_target_runs_only_target_free_queries(
    monkeypatch: pytest.MonkeyPatch, category: str, expected_subcommands: list[str]
) -> None:
    """A targetless standard batch omits target-requiring queries rather than reporting a false gap."""
    adapter = _load_adapter()
    launcher = "/explicit/codemap-py"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution(launcher, adapter.STATUS_AVAILABLE, "test launcher"),
    )

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Return healthy doctor/query payloads while recording target-free batch calls."""
        commands.append(argv)
        return (0, _HEALTHY_DOCTOR, None) if argv[1] == "doctor" else (0, _CLEAN_QUERY, None)

    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context(category, target=None)

    assert context.status == adapter.STATUS_AVAILABLE
    assert [outcome.subcommand for outcome in context.queries] == expected_subcommands
    assert commands == [[launcher, "doctor", "--json"], [launcher, "query", "--compact", *expected_subcommands]]


def test_standard_batch_of_only_target_requiring_queries_keeps_its_bounded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never report `available` off zero executed queries when every mapped query needs a target."""
    adapter = _load_adapter()
    launcher = "/explicit/codemap-py"
    commands: list[list[str]] = []
    monkeypatch.setitem(adapter.CATEGORY_QUERIES, "targeted-only", (adapter.QuerySpec("rdeps", requires_target=True),))
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution(launcher, adapter.STATUS_AVAILABLE, "test launcher"),
    )

    def _run_json(argv: list[str], timeout: float) -> tuple[int, dict | None, str | None]:
        """Return a healthy doctor payload for a query that must not execute."""
        commands.append(argv)
        return 0, _HEALTHY_DOCTOR, None

    monkeypatch.setattr(adapter, "_run_json", _run_json)

    context = adapter.gather_structural_context("targeted-only", target=None)

    assert context.status == adapter.STATUS_DEGRADED
    assert context.queries[0].error == "target required, none supplied"
    assert commands == [[launcher, "doctor", "--json"]]


def test_cli_context_persists_json_to_out_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The ``context`` CLI mode prints JSON and persists the identical bytes to ``--out``."""
    _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0))
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    out_path = tmp_path / "run" / "codemap-context.json"

    completed = subprocess.run(
        [sys.executable, str(ADAPTER_PATH), "context", "--category", "review", "--out", str(out_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    stdout_payload = json.loads(completed.stdout)
    file_payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert stdout_payload == file_payload
    assert stdout_payload["status"] == "available"
    assert stdout_payload["protocol_version"] == "codemap-py.integration.v1"
    assert stdout_payload["artifact_schema_version"] == 3
    assert stdout_payload["query_kind"] == "standard"
    expected_launcher = str(tmp_path / ("codemap-py.bat" if os.name == "nt" else "codemap-py"))
    assert os.path.normcase(stdout_payload["probe"]["launcher"]) == os.path.normcase(expected_launcher)


def test_cli_skip_route_persists_without_codemap_on_path(tmp_path: Path) -> None:
    """The public CLI records an explicit skip even when no Codemap launcher can resolve."""
    out_path = tmp_path / "run" / "codemap-context.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ADAPTER_PATH),
            "context",
            "--category",
            "implementation",
            "--query-kind",
            "skip",
            "--out",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, PATH=str(tmp_path)),
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["query_kind"] == "skip"
    assert payload["status"] == "skipped"
    assert payload["probe"]["launcher"] is None
    assert payload["queries"] == []


def test_cli_probe_absent_exits_zero_and_reports_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Absence is data, not a CLI failure — `probe` still exits `0`."""
    env = dict(os.environ, PATH=str(tmp_path))

    completed = subprocess.run(
        [sys.executable, str(ADAPTER_PATH), "probe"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "absent"


_CODEMAP_BIN_DIR = PLUGIN_ROOT.parent / "codemap-py" / "bin"


def _real_cli_ready(path_with_bin: str) -> bool:
    """Return whether the real codemap-py launcher answers ``doctor --json`` with an eligible interpreter."""
    if not (_CODEMAP_BIN_DIR / "codemap-py").is_file():
        return False
    try:
        completed = subprocess.run(
            ["codemap-py", "doctor", "--json"],
            capture_output=True,
            text=True,
            env=dict(os.environ, PATH=path_with_bin),
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


REAL_CLI_READY = _real_cli_ready(f"{_CODEMAP_BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.mark.skipif(
    not REAL_CLI_READY, reason="real codemap-py CLI unavailable (installed-plugin isolation or no eligible CPython)"
)
def test_root_scoped_query_runs_against_real_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unmocked regression: a root-scoped query drives the ACTUAL codemap-py grammar (``--root`` must precede the
    subcommand).

    Every other test in this file fakes the subprocess, so none exercises argparse's real grammar — the pre-fix argv
    order (``query central --root .``) passed those fakes yet errors against the real CLI. This runs the genuine
    launcher on a tiny real fixture and asserts a root-scoped query returns exit 0 with parseable metadata.
    """
    path_with_bin = f"{_CODEMAP_BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", path_with_bin)
    fixture = tmp_path / "proj"
    fixture.mkdir()
    (fixture / "leaf.py").write_text("def leaf():\n    return 1\n", encoding="utf-8")
    (fixture / "consumer.py").write_text("import leaf\n\n\ndef use():\n    return leaf.leaf()\n", encoding="utf-8")
    monkeypatch.chdir(fixture)
    scan = subprocess.run(
        ["codemap-py", "index", "--root", str(fixture)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert scan.returncode == 0, scan.stderr

    adapter = _load_adapter()
    resolution = adapter._resolve_codemap_executable()
    assert resolution.launcher is not None
    outcome = adapter._run_one_query(
        resolution.launcher, adapter.QuerySpec("central", requires_target=False), None, fixture, 30.0
    )

    assert outcome.exit_code == 0, outcome.error
    assert outcome.error is None
