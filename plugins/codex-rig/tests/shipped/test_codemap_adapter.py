"""Acceptance and unit checks for the optional codemap-py structural-context adapter.

Fake providers (a stubbed subprocess boundary or a tiny executable on ``PATH``) cover the adapter's decision logic;
tests that need the genuine ``codemap-py`` grammar or ``git`` output run the real tools and are gated on the CLI being
usable in this environment.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from _modules import load_module_registered

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = PLUGIN_ROOT / "shared" / "codemap_adapter.py"
_LAUNCHER_NAME = "codemap-py.cmd" if os.name == "nt" else "codemap-py"
_STUB_LAUNCHER = "/explicit/codemap-py"


def _load_adapter() -> ModuleType:
    """Load the adapter module directly, mirroring this suite's other shared-script tests."""
    return load_module_registered("codemap_adapter", ADAPTER_PATH)


ADAPTER = _load_adapter()


def _write_fake_codemap_py(bin_dir: Path, script_body: str) -> None:
    """Install a fake `codemap-py` executable on a directory meant to be prepended to PATH.

    Uses an absolute-path shebang (POSIX) rather than `/usr/bin/env bash`/python, so the fake binary still runs when a
    test intentionally narrows `PATH` to prove absence/isolation.
    """
    if os.name == "nt":
        fake = bin_dir / "codemap-py.cmd"
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

# The review batch's diff-impact reads a caller-supplied diff. Fake providers never open it, and the adapter reads it
# back only for an answer carrying `changed_files`, to name deleted Python modules; such tests write a real diff file.
_REVIEW_DIFF = Path("run") / "diff.patch"
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
    print(json.dumps({doctor_payload}))
    sys.exit({doctor_exit})
if sys.argv[1:2] == ["query"]:
    print(json.dumps({query_payload}))
    sys.exit({query_exit})
sys.exit(2)
"""


_HEALTHY_SCRIPT = _fake_script(_HEALTHY_DOCTOR, 0, _CLEAN_QUERY, 0)


def _answering(query_payload: dict, query_exit: int = 0) -> Callable[..., tuple[int, dict | None, str | None]]:
    """Return a `_run_json` side effect: a healthy doctor, and every other command answered with *query_payload*."""

    def answer(argv: list[str], timeout: float, cwd: Path | None = None) -> tuple[int, dict | None, str | None]:
        """Answer one stubbed provider command."""
        return (0, _HEALTHY_DOCTOR, None) if argv[1] == "doctor" else (query_exit, query_payload, None)

    return answer


def _commands(run_json: mock.Mock) -> list[list[str]]:
    """Return the argument vector of every command the adapter started through the stubbed boundary."""
    return [call.args[0] for call in run_json.call_args_list]


def _run_adapter_cli(env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped adapter script as the skills do and return the finished process."""
    return subprocess.run(
        [sys.executable, str(ADAPTER_PATH), *arguments], capture_output=True, text=True, env=env, check=False
    )


@pytest.fixture(name="adapter", scope="module")
def _adapter() -> ModuleType:
    """Return the shipped adapter module, loaded once for the file."""
    return ADAPTER


@pytest.fixture
def stub_provider(adapter: ModuleType) -> Iterator[Callable[..., mock.Mock]]:
    """Stub launcher resolution and the subprocess boundary so decision logic runs without spawning anything.

    Yields an installer ``(query_payload=_CLEAN_QUERY, query_exit=0, side_effect=None)`` that selects what every query
    answers (a healthy doctor always answers) and returns the ``_run_json`` mock for call assertions.
    """
    resolution = adapter.LauncherResolution(_STUB_LAUNCHER, adapter.STATUS_AVAILABLE, "test launcher")
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(adapter, "_resolve_codemap_executable", autospec=True, return_value=resolution)
        )
        run_json = stack.enter_context(mock.patch.object(adapter, "_run_json", autospec=True))

        def install(query_payload: dict = _CLEAN_QUERY, query_exit: int = 0, side_effect=None) -> mock.Mock:
            """Select the stubbed provider's answers."""
            run_json.side_effect = side_effect or _answering(query_payload, query_exit)
            return run_json

        install()
        yield install


_CODEMAP_BIN_DIR = PLUGIN_ROOT.parent / "codemap-py" / "bin"
_PATH_WITH_CODEMAP = f"{_CODEMAP_BIN_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


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


REAL_CLI_READY = _real_cli_ready(_PATH_WITH_CODEMAP)
_requires_real_cli = pytest.mark.skipif(
    not REAL_CLI_READY, reason="real codemap-py CLI unavailable (installed-plugin isolation or no eligible CPython)"
)


def _index_real_project(root: Path, files: dict[str, str]) -> None:
    """Write *files* under *root* and index them with the real provider."""
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    scan = subprocess.run(
        ["codemap-py", "index", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=dict(os.environ, PATH=_PATH_WITH_CODEMAP),
    )
    assert scan.returncode == 0, scan.stderr


_LEAF_CONSUMER_FILES = {
    "leaf.py": "def leaf():\n    return 1\n",
    "consumer.py": "import leaf\n\n\ndef use():\n    return leaf.leaf()\n",
    "nested/.keep": "",
}


def _indexed_real_fixture(tmp_path: Path) -> Path:
    """Index the two-module fixture with the real provider and return its root."""
    fixture = tmp_path / "proj"
    _index_real_project(fixture, _LEAF_CONSUMER_FILES)
    return fixture


# --- launcher selection and probing ---------------------------------------------------------------------------------


def _configure_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher setting at a path that does not exist."""
    return tmp_path / "missing"


def _configure_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher setting at a directory."""
    configured = tmp_path / "directory"
    configured.mkdir()
    return configured


def _configure_not_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher setting at a regular file without execute permission."""
    configured = tmp_path / "not-executable"
    configured.write_text("not executable", encoding="utf-8")
    return configured


def _configure_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher setting at a relative path that would resolve from the working directory."""
    _write_fake_codemap_py(tmp_path, _HEALTHY_SCRIPT)
    monkeypatch.chdir(tmp_path)
    return Path("codemap-py")


def _configure_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher setting at a symlink to a working launcher."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    _write_fake_codemap_py(target_dir, _HEALTHY_SCRIPT)
    configured = tmp_path / "symlink"
    configured.symlink_to(target_dir / _LAUNCHER_NAME)
    return configured


class TestLauncherSelection:
    """Resolve and probe the optional launcher from an explicit root, `CODEMAP_BIN`, or `PATH`."""

    def test_probe_absent_when_not_on_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter) -> None:
        """Report `absent` without running any subprocess when the binary is missing.

        PATH names an empty directory, the state of a machine without the optional provider installed.
        """
        monkeypatch.setenv("PATH", str(tmp_path))

        result = adapter.probe_codemap()

        assert result.status == adapter.STATUS_ABSENT
        assert result.doctor is None

    def test_provider_root_uses_active_install_without_path_search(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Use a caller-selected installed provider root when its CLI is absent from PATH.

        The provider's launcher lives only under the selected root's `bin`; PATH and `CODEMAP_BIN` offer nothing.
        """
        provider = tmp_path / "selected-provider"
        binary = provider / "bin"
        binary.mkdir(parents=True)
        _write_fake_codemap_py(binary, _HEALTHY_SCRIPT)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("CODEMAP_BIN", raising=False)

        result = adapter.probe_codemap(provider_root=provider)

        assert result.status == adapter.STATUS_AVAILABLE
        assert result.launcher == str(binary / _LAUNCHER_NAME)

    def test_invalid_provider_root_does_not_fall_back_to_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Preserve an invalid caller selection as evidence instead of silently using another provider.

        A healthy launcher is on PATH, but the caller named a root that does not exist, so PATH must not be consulted.
        """
        _write_fake_codemap_py(tmp_path, _HEALTHY_SCRIPT)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("CODEMAP_BIN", raising=False)

        result = adapter.probe_codemap(provider_root=tmp_path / "missing")

        assert result.status == adapter.STATUS_INCOMPATIBLE
        assert result.launcher is None
        assert "provider root" in result.detail

    def test_probe_cli_accepts_selected_provider_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], adapter
    ) -> None:
        """Expose the selected provider through the CLI used by installed review skills.

        The `probe --provider-root` entry point prints the resolved launcher as JSON for a root outside PATH.
        """
        provider = tmp_path / "provider"
        binary = provider / "bin"
        binary.mkdir(parents=True)
        _write_fake_codemap_py(binary, _HEALTHY_SCRIPT)
        monkeypatch.setenv("PATH", str(tmp_path))

        exit_code = adapter.main(["probe", "--provider-root", str(provider)])

        assert exit_code == 0
        assert json.loads(capsys.readouterr().out)["launcher"] == str(binary / _LAUNCHER_NAME)

    def test_probe_available_when_doctor_reports_supported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Report ``available`` once ``doctor --json`` returns a supported interpreter.

        A fake launcher on PATH answers `doctor` with a supported CPython, the healthy-install case.
        """
        _write_fake_codemap_py(tmp_path, _HEALTHY_SCRIPT)
        monkeypatch.setenv("PATH", str(tmp_path))

        result = adapter.probe_codemap()

        assert result.status == adapter.STATUS_AVAILABLE
        assert result.doctor is not None
        assert result.doctor.supported is True

    def test_probe_incompatible_when_interpreter_unsupported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Report `incompatible` when `doctor` marks the resolved interpreter unsupported.

        The launcher runs and answers, but names an interpreter below the supported floor.
        """
        _write_fake_codemap_py(tmp_path, _fake_script(dict(_HEALTHY_DOCTOR, supported=False), 0, _CLEAN_QUERY, 0))
        monkeypatch.setenv("PATH", str(tmp_path))

        result = adapter.probe_codemap()

        assert result.status == adapter.STATUS_INCOMPATIBLE

    def test_probe_incompatible_when_doctor_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Report ``incompatible`` when ``doctor --json`` itself fails.

        The fake launcher exits 1 from `doctor`, so no launcher is recorded as usable.
        """
        _write_fake_codemap_py(tmp_path, _fake_script(_HEALTHY_DOCTOR, 1, _CLEAN_QUERY, 0))
        monkeypatch.setenv("PATH", str(tmp_path))

        result = adapter.probe_codemap()

        assert result.status == adapter.STATUS_INCOMPATIBLE
        assert result.launcher is None

    def test_explicit_codemap_bin_wins_over_path_for_probe_and_query(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Use the explicit launcher consistently when PATH contains another codemap-py.

        PATH holds a failing, unsupported launcher while `CODEMAP_BIN` names a healthy one; both doctor and query must
        come from the explicit launcher.
        """
        explicit_bin = tmp_path / "explicit"
        path_bin = tmp_path / "path"
        explicit_bin.mkdir()
        path_bin.mkdir()
        explicit_doctor = dict(_HEALTHY_DOCTOR, version="explicit")
        path_doctor = dict(_HEALTHY_DOCTOR, supported=False, version="path")
        _write_fake_codemap_py(explicit_bin, _fake_script(explicit_doctor, 0, _CLEAN_QUERY, 0))
        _write_fake_codemap_py(path_bin, _fake_script(path_doctor, 0, _CLEAN_QUERY, 1))
        monkeypatch.setenv("CODEMAP_BIN", str(explicit_bin / _LAUNCHER_NAME))
        monkeypatch.setenv("PATH", str(path_bin))

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

        assert context.probe.doctor is not None
        assert context.probe.doctor.version == "explicit"
        assert context.status == adapter.STATUS_AVAILABLE
        assert context.queries[0].exit_code == 0

    @pytest.mark.parametrize(
        "configure",
        [
            pytest.param(_configure_missing, id="missing"),
            pytest.param(_configure_directory, id="directory"),
            pytest.param(_configure_not_executable, id="not-executable"),
            pytest.param(_configure_relative, id="relative"),
            pytest.param(_configure_symlink, id="symlink"),
        ],
    )
    def test_invalid_explicit_codemap_bin_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configure: Callable[..., Path], adapter
    ) -> None:
        """Reject a nonempty invalid, relative, or symlink configured launcher.

        A working launcher is on PATH, but a configured `CODEMAP_BIN` that is unusable must end the run as incompatible
        rather than silently falling back to PATH.
        """
        path_bin = tmp_path / "path"
        path_bin.mkdir()
        _write_fake_codemap_py(path_bin, _HEALTHY_SCRIPT)
        monkeypatch.setenv("CODEMAP_BIN", str(configure(tmp_path, monkeypatch)))
        monkeypatch.setenv("PATH", str(path_bin))

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

        assert context.status == adapter.STATUS_INCOMPATIBLE
        assert context.probe.launcher is None
        assert "CODEMAP_BIN" in context.probe.detail
        assert context.queries == ()

    def test_empty_codemap_bin_falls_back_to_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Treat an empty launcher setting as absent and resolve codemap-py through PATH.

        An exported but empty `CODEMAP_BIN` is common in shells and must not count as a configured launcher.
        """
        _write_fake_codemap_py(tmp_path, _HEALTHY_SCRIPT)
        monkeypatch.setenv("CODEMAP_BIN", "")
        monkeypatch.setenv("PATH", str(tmp_path))

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

        assert context.status == adapter.STATUS_AVAILABLE
        assert os.path.normcase(context.probe.launcher) == os.path.normcase(str(tmp_path / _LAUNCHER_NAME))

    @pytest.mark.parametrize("extension", [".BAT", ".Cmd", ".EXE", ".com"])
    def test_simulated_windows_launchers_accepted_case_insensitively(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extension: str, adapter
    ) -> None:
        """Accept Windows executable filename conventions without requiring a POSIX execute bit.

        The host OS is simulated, so the Windows branch runs on every runner with a non-executable file.
        """
        launcher = tmp_path / f"codemap-py{extension}"
        launcher.write_text("launcher", encoding="utf-8")
        monkeypatch.setattr(adapter.os, "name", "nt")

        assert adapter._configured_launcher_is_valid(launcher)


# --- query routes and batches ---------------------------------------------------------------------------------------


_NONE_SUPPLIED = "target required, none supplied"
_MALFORMED = "target malformed: expected a module, a module::symbol qname, or a dotted or bare symbol name"


class TestQueryRoutes:
    """Map a workflow category or explicit query kind to the bounded set of provider commands."""

    @mock.patch.object(ADAPTER, "_run_json", autospec=True)
    @mock.patch.object(ADAPTER, "_resolve_codemap_executable", autospec=True)
    def test_launcher_resolved_once_and_reused_for_queries(
        self, resolve: mock.Mock, run_json: mock.Mock, monkeypatch: pytest.MonkeyPatch, adapter
    ) -> None:
        """Keep doctor and query on one launcher even if PATH changes after the probe.

        The stubbed doctor call rewrites PATH, so a second resolution would pick a different launcher for the query.
        """
        resolve.return_value = adapter.LauncherResolution(_STUB_LAUNCHER, adapter.STATUS_AVAILABLE, "test launcher")

        def answer(argv: list[str], timeout: float, cwd: Path | None = None) -> tuple[int, dict | None, str | None]:
            """Answer doctor (changing PATH afterwards) and the query with healthy payloads."""
            if argv[1] == "doctor":
                monkeypatch.setenv("PATH", "/changed-after-doctor")
                return 0, _HEALTHY_DOCTOR, None
            return 0, _CLEAN_QUERY, None

        run_json.side_effect = answer

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

        assert resolve.call_count == 1
        assert _commands(run_json) == [
            [_STUB_LAUNCHER, "doctor", "--json"],
            [_STUB_LAUNCHER, "query", "--compact", "diff-impact", "--diff-file", str(_REVIEW_DIFF.absolute())],
        ]
        assert context.probe.launcher == _STUB_LAUNCHER

    def test_analysis_deps_uses_only_supported_arguments(self, stub_provider, adapter) -> None:
        """Prevent the managed analysis mapping from passing symbol-only flags to deps.

        The analysis category runs `central` and `deps <module>`; `deps` accepts no symbol-only options.
        """
        run_json = stub_provider()

        context = adapter.gather_structural_context("analysis", target="pkg.core")

        assert context.status == adapter.STATUS_AVAILABLE
        assert _commands(run_json) == [
            [_STUB_LAUNCHER, "doctor", "--json"],
            [_STUB_LAUNCHER, "query", "--compact", "central"],
            [_STUB_LAUNCHER, "query", "--compact", "deps", "pkg.core"],
        ]

    def test_absent_provider_never_runs_queries(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter) -> None:
        """Absence is non-fatal and short-circuits before any query subprocess runs.

        PATH holds nothing, so the implementation batch must stop at the probe with no queries recorded.
        """
        monkeypatch.setenv("PATH", str(tmp_path))

        context = adapter.gather_structural_context("implementation", target="pkg.mod")

        assert context.status == adapter.STATUS_ABSENT
        assert context.queries == ()

    def test_available_when_all_queries_clean(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter) -> None:
        """Report `available` when the probe is healthy and every mapped query is exhaustive.

        A real fake launcher on PATH answers the implementation batch (rdeps, coupled, test-impact) cleanly.
        """
        _write_fake_codemap_py(tmp_path, _HEALTHY_SCRIPT)
        monkeypatch.setenv("PATH", str(tmp_path))

        context = adapter.gather_structural_context("implementation", target="pkg.mod")

        assert context.status == adapter.STATUS_AVAILABLE
        assert len(context.queries) == 3  # rdeps, coupled, test-impact

    @mock.patch.object(ADAPTER, "_resolve_codemap_executable", autospec=True)
    def test_skip_route_persists_auditable_context_without_resolving_codemap(self, resolve: mock.Mock, adapter) -> None:
        """A localized edit records its skip decision without spawning Codemap work.

        The `skip` query kind is the workflow's explicit decision not to ask for structure; it must still leave an
        auditable artifact while never touching the launcher.
        """
        context = adapter.gather_structural_context("implementation", target="pkg.mod::edit", query_kind="skip")

        assert context.query_kind == "skip"
        assert context.status == adapter.STATUS_SKIPPED
        assert context.probe.status == adapter.STATUS_SKIPPED
        assert context.queries == ()
        resolve.assert_not_called()

    @pytest.mark.parametrize(
        ("query_kind", "target", "expected_query"),
        [
            pytest.param("central", "pkg.mod::edit", ["central", "--top", "5"], id="central"),
            pytest.param("callers", "pkg.mod::edit", ["fn-rdeps", "pkg.mod::edit", "--exclude-tests"], id="callers"),
            pytest.param("blast", "pkg.mod::edit", ["fn-blast", "pkg.mod::edit"], id="blast"),
            pytest.param("dependencies", "pkg.mod::edit", ["rdeps", "pkg.mod"], id="dependencies"),
            pytest.param("test-impact", "pkg.mod::edit", ["test-impact", "pkg.mod::edit"], id="test-impact"),
            pytest.param("coupling", "pkg.mod::edit", ["coupled"], id="coupling"),
            pytest.param(
                "callers", "pkg.mod.edit", ["fn-rdeps", "pkg.mod.edit", "--exclude-tests"], id="callers-dotted"
            ),
            pytest.param("callers", "edit", ["fn-rdeps", "edit", "--exclude-tests"], id="callers-bare"),
            pytest.param("callers", "pkg.mod", ["fn-rdeps", "pkg.mod", "--exclude-tests"], id="callers-module-only"),
            pytest.param("blast", "Klass.edit", ["fn-blast", "Klass.edit"], id="blast-class-qualified"),
            pytest.param("test-impact", "pkg.mod.edit", ["test-impact", "pkg.mod.edit"], id="test-impact-dotted"),
            pytest.param("dependencies", "pkg.mod", ["rdeps", "pkg.mod"], id="dependencies-module"),
        ],
    )
    def test_fact_route_runs_doctor_and_exactly_one_compact_query(
        self, stub_provider, adapter, query_kind: str, target: str, expected_query: list[str]
    ) -> None:
        """Each explicit fact route bounds Codemap work to one doctor and one compact query.

        Covers the qname spelling the skills pass, plus dotted, bare, and module-only targets: the provider resolves a
        unique dotted or bare name itself and lists candidates for an ambiguous one, so the adapter forwards them
        instead of recording "none supplied" for a supplied target.
        """
        run_json = stub_provider()

        context = adapter.gather_structural_context("implementation", target=target, query_kind=query_kind)

        assert context.status == adapter.STATUS_AVAILABLE
        assert context.query_kind == query_kind
        assert context.target == target
        assert _commands(run_json) == [
            [_STUB_LAUNCHER, "doctor", "--json"],
            [_STUB_LAUNCHER, "query", "--compact", *expected_query],
        ]
        assert len(context.queries) == 1

    @pytest.mark.parametrize(
        ("query_kind", "target", "expected_error"),
        [
            pytest.param("callers", None, _NONE_SUPPLIED, id="callers-none"),
            pytest.param("callers", "   ", _NONE_SUPPLIED, id="callers-blank"),
            pytest.param("callers", "pkg.mod::", _MALFORMED, id="callers-empty-symbol"),
            pytest.param("callers", "pkg.mod::edit::nested", _MALFORMED, id="callers-pkg.mod-edit-nested"),
            pytest.param("blast", None, _NONE_SUPPLIED, id="blast-none"),
            pytest.param("blast", "::edit", _MALFORMED, id="blast-edit"),
            pytest.param("dependencies", None, _NONE_SUPPLIED, id="dependencies-none"),
            pytest.param("dependencies", "pkg.mod::", _MALFORMED, id="dependencies-pkg.mod"),
            pytest.param("dependencies", "pkg.mod::edit::nested", _MALFORMED, id="dependencies-pkg.mod-edit-nested"),
            pytest.param("test-impact", None, _NONE_SUPPLIED, id="test-impact-none"),
            pytest.param("test-impact", "::edit", _MALFORMED, id="test-impact-edit"),
        ],
    )
    def test_fact_route_degrades_without_query_for_missing_or_malformed_target(
        self, stub_provider, adapter, query_kind: str, target: str | None, expected_error: str
    ) -> None:
        """Required compact facts never infer a missing or structurally malformed target, and say which one it was.

        A fact route with no usable target does not guess or execute a query subprocess after the doctor check. A
        supplied target must never be recorded as "none supplied": that false reason burned a follow-up without telling
        the caller what to fix.
        """
        run_json = stub_provider()

        context = adapter.gather_structural_context("implementation", target=target, query_kind=query_kind)

        assert context.status == adapter.STATUS_DEGRADED
        assert context.target == target
        assert _commands(run_json) == [[_STUB_LAUNCHER, "doctor", "--json"]]
        assert context.queries[0].error == expected_error

    @mock.patch.object(ADAPTER, "_resolve_codemap_executable", autospec=True)
    def test_invalid_query_kind_fails_before_launcher_resolution(self, resolve: mock.Mock, adapter) -> None:
        """Reject invalid routes at the public API boundary before optional work starts.

        An unknown `query_kind` is a caller bug and must raise before the launcher is even looked up.
        """
        with pytest.raises(ValueError, match="unknown query kind"):
            adapter.gather_structural_context("implementation", query_kind="not-a-route")

        resolve.assert_not_called()

    def test_unknown_category_is_rejected(self, adapter) -> None:
        """Refuse an undefined category rather than silently mapping it to an empty query set.

        A misspelled category would otherwise report a clean, empty structural context.
        """
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
        self, stub_provider, adapter, category: str, expected_subcommands: list[str]
    ) -> None:
        """A targetless standard batch omits target-requiring queries rather than reporting a false gap.

        Without a target, queries that need one are not part of the batch, so the context stays `available`.
        """
        run_json = stub_provider()

        context = adapter.gather_structural_context(category, target=None)

        assert context.status == adapter.STATUS_AVAILABLE
        assert [outcome.subcommand for outcome in context.queries] == expected_subcommands
        assert _commands(run_json) == [
            [_STUB_LAUNCHER, "doctor", "--json"],
            [_STUB_LAUNCHER, "query", "--compact", *expected_subcommands],
        ]

    def test_batch_of_only_target_requiring_queries_keeps_its_bounded_error(
        self, monkeypatch: pytest.MonkeyPatch, stub_provider, adapter
    ) -> None:
        """Never report `available` off zero executed queries when every mapped query needs a target.

        A category whose whole batch needs a target but gets none must record the bounded error, not an empty success.
        """
        run_json = stub_provider()
        monkeypatch.setitem(
            adapter.CATEGORY_QUERIES, "targeted-only", (adapter.QuerySpec("rdeps", requires_target=True),)
        )

        context = adapter.gather_structural_context("targeted-only", target=None)

        assert context.status == adapter.STATUS_DEGRADED
        assert context.queries[0].error == "target required, none supplied"
        assert _commands(run_json) == [[_STUB_LAUNCHER, "doctor", "--json"]]

    def test_review_batch_without_diff_file_never_runs_plain_diff_impact(self, stub_provider, adapter) -> None:
        """A review batch with no diff file records a bounded degraded outcome instead of diffing the tree against HEAD.

        In a detached worktree at the reviewed head, plain ``diff-impact`` compares the tree with itself and reports
        zero changed files as a complete answer, so the adapter must never start that query.
        """
        run_json = stub_provider()

        context = adapter.gather_structural_context("review")

        assert _commands(run_json) == [[_STUB_LAUNCHER, "doctor", "--json"]]
        assert context.status == adapter.STATUS_DEGRADED
        assert context.to_dict()["status_reasons"] == ["diff-impact: diff file required, none supplied"]

    def test_root_runs_provider_from_that_checkout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub_provider, adapter
    ) -> None:
        """Run doctor and every query from the absolute root, because the provider discovers its index from the CWD.

        Passing only ``--root`` from a nested review worktree let the provider open the parent repository's index;
        running from the root makes the opened index and the compared scan root name the same checkout.
        """
        run_json = stub_provider()
        monkeypatch.chdir(tmp_path)

        adapter.gather_structural_context("review", root=Path("checkout"), diff_file=_REVIEW_DIFF)

        checkout = tmp_path / "checkout"
        assert [call.args[2] for call in run_json.call_args_list] == [checkout, checkout]
        assert _commands(run_json)[1][:5] == [_STUB_LAUNCHER, "query", "--compact", "--root", str(checkout)]


# --- status reduction and provider detail ---------------------------------------------------------------------------

_AMBIGUOUS_TARGET = {
    "error": "Symbol 'build' is ambiguous: 2 indexed symbols or modules match that name.",
    "candidates": ["pkg.core::build", "pkg.util::build"],
    "candidate_count": 2,
    "rejected_target": "build",
}
_ROOT_MISMATCH_REASON = "diff-impact: root_mismatch: index built for another root"


class TestStatusReduction:
    """Reduce probe and query answers to one status and keep the provider's own detail as the reasons."""

    @pytest.mark.parametrize(
        ("query_payload", "category", "expected_status"),
        [
            pytest.param(_DEGRADED_QUERY, "review", "STATUS_DEGRADED", id="not-covered-present"),
            pytest.param(_STALE_QUERY, "audit", "STATUS_STALE", id="query-reports-stale"),
        ],
    )
    def test_completeness_metadata_sets_status(
        self, stub_provider, adapter, query_payload: dict, category: str, expected_status: str
    ) -> None:
        """Report `degraded` for non-exhaustive completeness metadata and `stale` for an index older than source.

        A query returning non-exhaustive completeness metadata degrades the context; a query whose index block flags the
        index older than source marks it stale.
        """
        stub_provider(query_payload)

        context = adapter.gather_structural_context(category, diff_file=_REVIEW_DIFF)

        assert context.status == getattr(adapter, expected_status)

    def test_stale_and_gap_from_one_query_compose(self, stub_provider, adapter) -> None:
        """Keep both caveats when a single query is stale and non-exhaustive at once.

        The serialized status is the combined `stale+degraded` token.
        """
        stub_provider(_STALE_DEGRADED_QUERY)

        context = adapter.gather_structural_context("audit")

        assert context.status == adapter.STATUS_STALE_DEGRADED
        assert context.to_dict()["status"] == "stale+degraded"

    def test_stale_and_gap_from_different_queries_compose(self, stub_provider, adapter) -> None:
        """Compose caveats raised by different queries so neither batch member masks the other.

        The audit batch's `undocumented` query is stale while its sibling is only degraded.
        """

        def answer(argv: list[str], timeout: float, cwd: Path | None = None) -> tuple[int, dict | None, str | None]:
            """Select stale or degraded query evidence by subcommand."""
            if argv[1] == "doctor":
                return 0, _HEALTHY_DOCTOR, None
            return (0, _STALE_QUERY, None) if argv[3] == "undocumented" else (0, _DEGRADED_QUERY, None)

        stub_provider(side_effect=answer)

        context = adapter.gather_structural_context("audit")

        assert context.status == adapter.STATUS_STALE_DEGRADED
        assert [outcome.stale for outcome in context.queries] == [True, False]
        assert [outcome.query_complete for outcome in context.queries] == [True, False]

    @pytest.mark.parametrize(
        ("query_kind", "payload", "exit_code", "expected"),
        [
            pytest.param(
                "test-impact",
                _AMBIGUOUS_TARGET,
                1,
                ("degraded", True, {k: v for k, v in _AMBIGUOUS_TARGET.items() if k != "error"}),
                id="ambiguous-target-keeps-candidates",
            ),
            pytest.param(
                "callers",
                {"error": "Symbol 'pkg.util::nothere' not found.", "rejected_target": "pkg.util::nothere"},
                1,
                ("degraded", True, {"rejected_target": "pkg.util::nothere"}),
                id="symbol-not-found",
            ),
            pytest.param(
                "dependencies",
                {"error": "module not indexed", "module": "pkg.nothere", "suggestions": ["pkg.core"]},
                3,
                ("degraded", True, {"module": "pkg.nothere", "suggestions": ["pkg.core"]}),
                id="module-not-indexed-from-provider-without-marker",
            ),
            pytest.param(
                "callers",
                {"error": "Index is v2 — call graph not available. Re-run /codemap-py:scan-codebase to upgrade."},
                1,
                ("incompatible", False, {}),
                id="provider-side-exit-1-without-marker",
            ),
            pytest.param(
                "callers",
                {"error": "argument contains a newline"},
                2,
                ("degraded", True, {}),
                id="provider-diagnosed-exit-2-with-error-text-only",
            ),
            pytest.param(
                "dependencies",
                {"error": "index failed self-check", "reason": "schema", "path": "/repo/index.json"},
                3,
                ("incompatible", False, {"reason": "schema", "path": "/repo/index.json"}),
                id="unloadable-index-exit-3",
            ),
        ],
    )
    def test_follow_up_target_rejection_degrades_and_keeps_provider_detail(
        self, stub_provider, adapter, query_kind: str, payload: dict, exit_code: int, expected: tuple
    ) -> None:
        """A target the provider refused degrades the follow-up and keeps its candidates; a provider failure does not.

        ``incompatible`` ends every structural follow-up for the run, so recording a misspelled, ambiguous, or unindexed
        target that way told the specialist a working provider was unusable, and dropped the very candidates the
        provider listed for the retry. Exits 1 and 3 also carry provider-side failures, which must stay
        ``incompatible``. A JSON error object holding only its text is recorded as ``{}``, never ``null``, so the
        recorded flag stays exactly what an artifact reader re-derives from the exit code and answer.
        """
        stub_provider(payload, exit_code)

        record = adapter.gather_structural_context("review", target="build", query_kind=query_kind).to_dict()

        query = record["queries"][0]
        assert (record["status"], query["input_rejected"], query["answer"]) == expected
        assert record["status_reasons"] == [f"{query['subcommand']}: {payload['error']}"]
        assert query["input_rejected"] == adapter.provider_rejected_input(query["exit_code"], query["answer"])

    def test_zero_caller_method_hint_is_kept_and_named_in_the_reasons(self, stub_provider, adapter) -> None:
        """A zero-caller method answer keeps the provider's hint and names it among the status reasons.

        ``query_complete`` stays true for such an answer, so ``called_by: []`` read alone says "unused"; the hint is the
        only sign that instance calls were never resolved, and stripping it handed a specialist false evidence.
        """
        hint = 'Find references with grep -rnE "\\.render\\b". 0 static callers for method Report.render: instance ...'
        payload = {
            "qname": "pkg.util::Report.render",
            "called_by": [],
            "count": 0,
            "hint": hint,
            "index": {"query_complete": True, "not_covered": [], "degraded": 0, "stale": False},
        }
        stub_provider(payload)

        record = adapter.gather_structural_context(
            "review", target="pkg.util::Report.render", query_kind="callers"
        ).to_dict()

        assert record["queries"][0]["answer"]["hint"] == hint
        assert record["status_reasons"] == [f"fn-rdeps: hint: {hint}"]
        assert record["status"] == "available"

    def test_review_diff_impact_discloses_an_unresolved_changed_method(
        self, tmp_path: Path, stub_provider, adapter
    ) -> None:
        """A review probe whose changed method has zero static callers is ``degraded`` and names the provider's hint.

        ``diff-impact`` is the one structural answer every Python review carries. It recorded ``caller_count: 0`` for a
        method reached only through an instance as ``available`` with no reason, so the change read as touching dead
        code. The provider now discloses it with a per-entry and a top-level ``hint`` plus the call graph's
        ``not_covered``; the adapter's generic reduction turns those into reasons and status with no ``diff-
        impact``-specific rule.
        """
        entry_hint = 'Find references with grep -rnE "\\.render\\b". 0 static callers for method Report.render: ...'
        top_hint = "caller_count 0 on a changed method ... Unresolved (1): pkg.util::Report.render"
        not_covered = ["dynamic-dispatch", "hook-callbacks", "string-dispatch"]
        payload = {
            "base": "diff-file:diff.patch",
            "changed_files": 1,
            "changed_modules": [
                {
                    "module": "pkg.util",
                    "path": "pkg/util.py",
                    "changed_symbols": ["pkg.util::Report.render"],
                    "fn_rdeps": [{"qname": "pkg.util::Report.render", "caller_count": 0, "hint": entry_hint}],
                    "risk": "MODERATE",
                }
            ],
            "unmapped_files": [],
            "hint": top_hint,
            "index": {"query_complete": True, "not_covered": not_covered, "degraded": 0, "stale": False},
        }
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text(
            "--- a/pkg/util.py\n+++ b/pkg/util.py\n@@ -6 +6 @@\n-        return 1\n+        return 2\n"
        )
        stub_provider(payload)

        record = adapter.gather_structural_context("review", diff_file=diff_file).to_dict()

        assert record["queries"][0]["answer"]["changed_modules"][0]["fn_rdeps"][0]["hint"] == entry_hint
        assert record["status_reasons"] == [
            f"diff-impact: not covered: {', '.join(not_covered)}",
            f"diff-impact: hint: {top_hint}",
        ]
        assert record["status"] == adapter.STATUS_DEGRADED

    @pytest.mark.parametrize(
        ("index_overrides", "expected_reasons"),
        [
            pytest.param(
                {},
                ["diff-impact: incomplete: root_mismatch", _ROOT_MISMATCH_REASON],
                id="provider-incomplete-root-mismatch",
            ),
            pytest.param(
                {"query_complete": True, "completeness_reason": None},
                [_ROOT_MISMATCH_REASON],
                id="root-mismatch-without-incomplete-flag",
            ),
        ],
    )
    def test_completeness_detail_is_kept_and_degrades_with_reason(
        self, tmp_path: Path, stub_provider, adapter, index_overrides: dict, expected_reasons: list[str]
    ) -> None:
        """Keep the provider's completeness reason, root mismatch, and note, and name them as the degraded cause.

        The observed review artifacts persisted ``degraded`` with no explanation because these fields were dropped; a
        reader could not tell a root mismatch from a provider gap.
        """
        payload = {**_ROOT_MISMATCH_QUERY, "index": {**_ROOT_MISMATCH_QUERY["index"], **index_overrides}}
        (tmp_path / "diff.patch").write_text(_PYTHON_CHANGE_DIFF, encoding="utf-8", newline="\n")
        stub_provider(payload)

        record = adapter.gather_structural_context("review", diff_file=tmp_path / "diff.patch").to_dict()

        query = record["queries"][0]
        assert (record["artifact_schema_version"], record["status"]) == (4, "degraded")
        assert (query["root_mismatch"], query["note"]) == (True, payload["index"]["note"])
        assert query["completeness_reason"] == payload["index"]["completeness_reason"]
        assert record["status_reasons"] == expected_reasons

    def test_usage_exit_without_provider_diagnosis_is_incompatible(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """An exit 2 carrying only argument-parser usage marks the provider incompatible, not the caller's input.

        Python's argument parser exits 2 for a flag it does not know, the same code the provider uses for input it
        diagnoses. Treating that usage exit as caller input kept follow-ups eligible against a provider that rejected
        the adapter's own command line, so only the provider's JSON ``error`` object may degrade instead.
        """
        (tmp_path / "diff.patch").write_text(_PYTHON_CHANGE_DIFF, encoding="utf-8", newline="\n")
        _write_fake_codemap_py(tmp_path, _USAGE_ERROR_FAKE)
        monkeypatch.setenv("PATH", str(tmp_path))

        record = adapter.gather_structural_context("review", diff_file=tmp_path / "diff.patch").to_dict()

        query = record["queries"][0]
        assert (record["status"], query["exit_code"], query["input_rejected"]) == ("incompatible", 2, False)
        assert record["status_reasons"] == [f"diff-impact: {query['error']}"]
        assert "unrecognized arguments" in query["error"]

    def test_answer_lists_and_text_are_bounded_with_original_lengths(self, stub_provider, adapter) -> None:
        """Keep a large answer small: cut lists and long strings, and record every cut's original length.

        A change set touching many modules must not turn the persisted artifact back into an unbounded dump.
        """
        module = {**_REAL_MAPPED_DIFF_IMPACT["changed_modules"][0], "importers": [f"user{i}" for i in range(9)]}
        payload = {
            **_REAL_MAPPED_DIFF_IMPACT,
            "changed_modules": [module] * 30,
            "test_impact": {"test_files": [], "total": 0, "pytest_cmd": "x" * 400},
        }
        stub_provider(payload)

        query = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF).to_dict()["queries"][0]

        assert len(query["answer"]["changed_modules"]) == adapter.ANSWER_LIST_LIMIT
        assert len(query["answer"]["changed_modules"][0]["importers"]) == 5
        assert query["answer_truncated"]["changed_modules"] == 30
        assert query["answer_truncated"]["changed_modules[0].importers"] == 9
        assert query["answer_truncated"]["test_impact.pytest_cmd"] == 400


# --- index provenance -----------------------------------------------------------------------------------------------


class TestIndexProvenance:
    """Record which index each query opened and report disagreement with what `doctor` resolved."""

    def test_query_records_the_index_path_the_provider_reported(self, stub_provider, adapter) -> None:
        """Record each query's own index path so provenance names the file that answered.

        The provider reports the same path `doctor` resolved, so the divergence list stays empty yet serialized.
        """
        stub_provider(_AGREEING_QUERY)

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

        assert [outcome.index_path for outcome in context.queries] == [_HEALTHY_DOCTOR["index_path"]]
        assert context.to_dict()["queries"][0]["index_path"] == _HEALTHY_DOCTOR["index_path"]
        # Agreement is not a divergence: the evidence list stays empty and is still serialized.
        assert context.index_path_divergence == ()
        assert context.to_dict()["index_path_divergence"] == []

    def test_provider_without_index_path_records_none_and_claims_no_divergence(self, stub_provider, adapter) -> None:
        """Tolerate a provider predating the field: absence stays absent, never back-filled.

        A clean answer without `index_path` must not be compared against the probe's path.
        """
        stub_provider(_CLEAN_QUERY)

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

        assert context.status == adapter.STATUS_AVAILABLE
        assert [outcome.index_path for outcome in context.queries] == [None]
        assert context.to_dict()["queries"][0]["index_path"] is None
        # An unreported path must not be compared against the probe's — absence is not disagreement.
        assert context.index_path_divergence == ()

    def test_divergent_index_path_is_evidence_without_changing_status(self, stub_provider, adapter) -> None:
        """Report a query that opened a different index than `doctor` resolved, and keep both paths.

        The status must stay `available`: the answers themselves were complete and fresh. Folding the disagreement into
        the status would assert which of the two processes was wrong, and would cost the reader the two paths that make
        the disagreement diagnosable.
        """
        stub_provider(_DIVERGENT_QUERY)

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

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

    def test_not_indexed_exit_records_the_addressed_path_and_its_divergence(self, stub_provider, adapter) -> None:
        """Keep the path a failed load addressed: a wrong index dir is exactly what diverges.

        A not-indexed exit raised by a failed load reports the path at the payload root rather than under `index`. That
        path is the only provenance such a run has, and comparing it is the case the field most needs to cover — the
        query never opened the index the probe found. The provider's own words are kept: a fixed "target not indexed"
        label misnamed an index that failed to load as a caller's bad target.
        """
        stub_provider({"error": "index is not valid JSON", "path": _DIVERGENT_INDEX_PATH}, 3)

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

        outcome = context.queries[0]
        assert (outcome.exit_code, outcome.error, outcome.input_rejected) == (3, "index is not valid JSON", False)
        assert outcome.index_path == _DIVERGENT_INDEX_PATH
        assert [record.query_index_path for record in context.index_path_divergence] == [_DIVERGENT_INDEX_PATH]

    def test_not_indexed_exit_without_a_path_key_claims_nothing(self, stub_provider, adapter) -> None:
        """A not-indexed exit raised after a successful load carries no path, so none is invented.

        The module-not-indexed answer has no `path`, so no index path is recorded and nothing diverges.
        """
        stub_provider({"error": "module not indexed", "module": "pkg.missing", "suggestions": []}, 3)

        context = adapter.gather_structural_context("review", diff_file=_REVIEW_DIFF)

        assert context.queries[0].index_path is None
        assert context.index_path_divergence == ()

    def test_divergence_is_recorded_per_query_not_once_per_batch(self, stub_provider, adapter) -> None:
        """Attribute divergence to the query that diverged, leaving an agreeing sibling unaccused.

        In the audit batch only the `dead-modules` query opens a different index than the probe.
        """

        def answer(argv: list[str], timeout: float, cwd: Path | None = None) -> tuple[int, dict | None, str | None]:
            """Select agreeing or divergent query evidence by subcommand."""
            if argv[1] == "doctor":
                return 0, _HEALTHY_DOCTOR, None
            return (0, _AGREEING_QUERY, None) if argv[3] == "undocumented" else (0, _DIVERGENT_QUERY, None)

        stub_provider(side_effect=answer)

        context = adapter.gather_structural_context("audit")

        assert [record.subcommand for record in context.index_path_divergence] == ["dead-modules"]
        assert context.status == adapter.STATUS_AVAILABLE


# --- change sets and diff files -------------------------------------------------------------------------------------

_PYTHON_CHANGE_DIFF = "diff --git a/leaf.py b/leaf.py\n--- a/leaf.py\n+++ b/leaf.py\n@@ -1,2 +1,2 @@\n def leaf():\n-    return 0\n+    return 1\n"
_NEW_MODULE_DIFF = (
    "diff --git a/new_mod.py b/new_mod.py\nnew file mode 100644\n--- /dev/null\n+++ b/new_mod.py\n"
    "@@ -0,0 +1,2 @@\n+def fresh():\n+    return 2\n"
)
# `git diff` output deleting `leaf.py`, which `consumer.py` in the real-CLI fixture imports.
_DELETED_MODULE_DIFF = (
    "diff --git a/leaf.py b/leaf.py\ndeleted file mode 100644\nindex 0000001..0000000\n--- a/leaf.py\n+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n-def leaf():\n-    return 1\n"
)
_DOCS_ONLY_DIFF = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
_DELETED_LEAF_GAP = "1 deleted Python modules not analysed (importers unchecked): leaf.py"
_UNMAPPED_GAP = "all 1 changed Python files unmapped by the index"
# Real `codemap-py query --compact diff-impact --diff-file` output for `_PYTHON_CHANGE_DIFF` against the two-module
# fixture the real-CLI tests index, with machine paths shortened. Fakes start from this so they cannot drift into a
# shape the provider never prints; `test_fake_diff_impact_shape_matches_real_cli` re-checks the keys.
_REAL_MAPPED_DIFF_IMPACT = {
    "base": "diff-file:/run/diff.patch",
    "changed_files": 1,
    "changed_modules": [
        {
            "module": "leaf",
            "path": "leaf.py",
            "changed_symbols": ["leaf::leaf"],
            "rdep_count": 1,
            "importers": ["consumer"],
            "coupled_internal_deps": None,
            "fn_rdeps": [{"qname": "leaf::leaf", "caller_count": 1}],
            "risk": "MODERATE",
        }
    ],
    "unmapped_files": [],
    "test_impact": {"test_files": [], "total": 0, "pytest_cmd": ""},
    "highest_risk": "MODERATE",
    "index": {
        "query_complete": True,
        "stale": False,
        "root_mismatch": False,
        "compact": True,
        "index_path": "/proj/.cache/codemap/proj.json",
        "method": "static-ast",
        "scope": "diff-impact",
    },
}
# Real output for `_NEW_MODULE_DIFF`: the one changed Python file is new, so the index maps it to no module.
_REAL_UNMAPPED_DIFF_IMPACT = {
    **_REAL_MAPPED_DIFF_IMPACT,
    "changed_modules": [],
    "unmapped_files": ["new_mod.py"],
    "highest_risk": "LOW",
}
# Real output for `_DELETED_MODULE_DIFF`: the provider drops a deleted file, so it reads no Python file at all.
_REAL_DELETION_DIFF_IMPACT = {
    **_REAL_MAPPED_DIFF_IMPACT,
    "changed_files": 0,
    "changed_modules": [],
    "unmapped_files": [],
    "highest_risk": "LOW",
}
# The provider's own report for a nested review worktree whose index lookup walked up to the parent repository; the
# diff file still maps its one changed file, so only the mismatch degrades the answer.
_ROOT_MISMATCH_QUERY = {
    **_REAL_MAPPED_DIFF_IMPACT,
    "index": {
        **_REAL_MAPPED_DIFF_IMPACT["index"],
        "query_complete": False,
        "root_mismatch": True,
        "completeness_reason": "root_mismatch",
        "note": "the index was built for a different project root than the one queried",
    },
}
_USAGE_ERROR_FAKE = f"""
import json, sys
if sys.argv[1:2] == ["doctor"]:
    print(json.dumps({_HEALTHY_DOCTOR}))
    sys.exit(0)
sys.stderr.write("usage: codemap-py query [-h]\\ncodemap-py query: error: unrecognized arguments: --diff-file\\n")
sys.exit(2)
"""

# Real `git diff` shapes for Python changes that carry no `---`/`+++` post-image pair at all.
_EMPTY_INIT_DELETED_DIFF = (
    "diff --git a/pkg/__init__.py b/pkg/__init__.py\ndeleted file mode 100644\nindex e69de29..0000000\n"
)
_PURE_RENAME_DIFF = "diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n"
_MODE_ONLY_DIFF = "diff --git a/run.py b/run.py\nold mode 100644\nnew mode 100755\n"
# Real `git diff` shapes under the default `core.quotePath`: every non-ASCII path is C-quoted with octal escapes.
_QUOTED_DELETION_DIFF = (
    'diff --git "a/pkg/mod\\303\\251.py" "b/pkg/mod\\303\\251.py"\ndeleted file mode 100644\n'
    'index 7a1b2c3..0000000\n--- "a/pkg/mod\\303\\251.py"\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def f():\n-    return 1\n'
)
_QUOTED_EMPTY_DELETION_DIFF = (
    'diff --git "a/pkg/\\303\\251.py" "b/pkg/\\303\\251.py"\ndeleted file mode 100644\nindex e69de29..0000000\n'
)
_QUOTED_RENAME_DIFF = (
    'diff --git "a/pkg/mod\\303\\251.py" b/pkg/mod.py\nsimilarity index 100%\n'
    'rename from "pkg/mod\\303\\251.py"\nrename to pkg/mod.py\n'
)


def _diff_reading_fake(query_payload: dict) -> str:
    """Build a fake provider that, like the real one, reads ``--diff-file`` relative to its own working directory."""
    return f"""
import json, os, sys
if sys.argv[1:2] == ["doctor"]:
    print(json.dumps({_HEALTHY_DOCTOR}))
    sys.exit(0)
diff_file = sys.argv[sys.argv.index("--diff-file") + 1]
if not os.path.isfile(diff_file):
    print(json.dumps({{"error": "diff file unreadable", "path": diff_file, "detail": "No such file or directory"}}))
    sys.exit(2)
print(json.dumps({query_payload}))
sys.exit(0)
"""


def _quoting_git(repo: Path, *arguments: str) -> str:
    """Run one git command in *repo* with ``core.quotePath`` forced on and return its UTF-8 stdout."""
    git = ["git", "-C", str(repo), "-c", "core.quotePath=true", "-c", "user.email=t@t", "-c", "user.name=t"]
    return subprocess.run([*git, *arguments], check=True, capture_output=True).stdout.decode("utf-8")


class TestChangeSet:
    """Persist the provider's change-set answer and degrade when the diff holds Python the provider never analysed."""

    @pytest.mark.parametrize(
        ("diff_text", "payload", "expected_status", "expected_gap"),
        [
            pytest.param(_PYTHON_CHANGE_DIFF, _REAL_MAPPED_DIFF_IMPACT, "available", None, id="python-change-mapped"),
            pytest.param(
                _NEW_MODULE_DIFF,
                _REAL_UNMAPPED_DIFF_IMPACT,
                "degraded",
                f"diff-impact: {_UNMAPPED_GAP}",
                id="every-python-file-unmapped",
            ),
            pytest.param(
                _DELETED_MODULE_DIFF,
                _REAL_DELETION_DIFF_IMPACT,
                "degraded",
                f"diff-impact: {_DELETED_LEAF_GAP}",
                id="deletion-only-python-diff",
            ),
            pytest.param(
                _PYTHON_CHANGE_DIFF + _DELETED_MODULE_DIFF.replace("leaf.py", "gone.py"),
                _REAL_MAPPED_DIFF_IMPACT,
                "degraded",
                "diff-impact: 1 deleted Python modules not analysed (importers unchecked): gone.py",
                id="deletion-beside-mapped-change",
            ),
            pytest.param(_DOCS_ONLY_DIFF, _REAL_DELETION_DIFF_IMPACT, "available", None, id="no-python-path-in-diff"),
        ],
    )
    def test_answer_is_kept_and_unanalysed_python_degrades(
        self,
        tmp_path: Path,
        stub_provider,
        adapter,
        diff_text: str,
        payload: dict,
        expected_status: str,
        expected_gap: str | None,
    ) -> None:
        """Persist the diff-impact answer itself and degrade a change set with Python the provider never analysed.

        The provider counts every changed Python post-image it reads, mapped or not, and calls the answer complete even
        when none maps or when the diff deletes a module it never reads. A deletion-only Python diff used to persist as
        bare ``available`` with ``highest_risk: LOW``; only a diff naming no Python path at all may map nothing and stay
        clean.
        """
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text(diff_text, encoding="utf-8", newline="\n")
        stub_provider(payload)

        record = adapter.gather_structural_context("review", diff_file=diff_file).to_dict()

        query = record["queries"][0]
        assert record["status"] == expected_status
        assert record["status_reasons"] == ([expected_gap] if expected_gap else [])
        assert query["answer"]["changed_modules"] == payload["changed_modules"]
        assert query["answer"]["unmapped_files"] == payload["unmapped_files"]
        assert "index" not in query["answer"]

    def test_relative_diff_file_resolves_against_caller_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """A relative diff file names a path under the caller's directory even though the provider runs from the root.

        The run directory a workflow prints is relative to the invoking checkout, while the provider now runs from
        ``--root``; passing the relative string through made a healthy provider report the diff unreadable. The fake
        provider opens the file from its own working directory, as the real one does.
        """
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "diff.patch").write_text(_PYTHON_CHANGE_DIFF, encoding="utf-8", newline="\n")
        (tmp_path / "checkout").mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_fake_codemap_py(bin_dir, _diff_reading_fake(_REAL_MAPPED_DIFF_IMPACT))
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.chdir(tmp_path)

        record = adapter.gather_structural_context(
            "review", root=Path("checkout"), diff_file=Path("run") / "diff.patch"
        ).to_dict()

        assert (record["status"], record["diff_file"]) == ("available", str(tmp_path / "run" / "diff.patch"))

    def test_rejected_diff_file_keeps_provider_reason_and_degrades(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Input the provider rejects keeps the provider's own words and degrades instead of marking it incompatible.

        ``incompatible`` forbids retries and follow-ups for the rest of the run, which is wrong for a provider that
        works and only refused a bad path.
        """
        _write_fake_codemap_py(tmp_path, _diff_reading_fake(_REAL_MAPPED_DIFF_IMPACT))
        monkeypatch.setenv("PATH", str(tmp_path))

        record = adapter.gather_structural_context("review", diff_file=tmp_path / "missing.patch").to_dict()

        assert record["status"] == "degraded"
        assert record["status_reasons"] == ["diff-impact: diff file unreadable: No such file or directory"]
        assert (record["queries"][0]["exit_code"], record["queries"][0]["input_rejected"]) == (2, True)

    @pytest.mark.parametrize(
        ("answer", "diff_text", "expected"),
        [
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                _EMPTY_INIT_DELETED_DIFF,
                "1 deleted Python modules not analysed (importers unchecked): pkg/__init__.py",
                id="empty-file-deletion-has-no-hunk",
            ),
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                _PURE_RENAME_DIFF,
                "1 deleted Python modules not analysed (importers unchecked): old.py",
                id="rename-deletes-old-module-path",
            ),
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                "--- a/pkg/util.py\t2026-10-08\n+++ /dev/null\t2026-10-08\n@@ -1 +0,0 @@\n-x = 1\n",
                "1 deleted Python modules not analysed (importers unchecked): pkg/util.py",
                id="plain-unified-diff-deletion",
            ),
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                _MODE_ONLY_DIFF,
                "1 Python files changed without a text hunk not analysed: run.py",
                id="mode-only-change-explains-zero",
            ),
            pytest.param(
                _REAL_MAPPED_DIFF_IMPACT, _PYTHON_CHANGE_DIFF + _MODE_ONLY_DIFF, None, id="mode-only-beside-mapped"
            ),
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                "".join(_DELETED_MODULE_DIFF.replace("leaf", f"m{index}") for index in range(7)),
                "7 deleted Python modules not analysed (importers unchecked): m0.py, m1.py, m2.py, m3.py, m4.py, +2 more",
                id="many-deletions-sampled-but-counted",
            ),
            pytest.param(
                _REAL_UNMAPPED_DIFF_IMPACT,
                _NEW_MODULE_DIFF + _DELETED_MODULE_DIFF,
                f"{_UNMAPPED_GAP}; {_DELETED_LEAF_GAP}",
                id="unmapped-and-deleted-both-named",
            ),
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                None,
                "diff file unreadable by the adapter: deleted Python modules unchecked",
                id="unreadable-diff-is-a-gap",
            ),
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                _QUOTED_DELETION_DIFF,
                "1 deleted Python modules not analysed (importers unchecked): pkg/modé.py",
                id="c-quoted-deletion",
            ),
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                _QUOTED_EMPTY_DELETION_DIFF,
                "1 deleted Python modules not analysed (importers unchecked): pkg/é.py",
                id="c-quoted-empty-file-deletion-has-no-hunk",
            ),
            pytest.param(
                _REAL_DELETION_DIFF_IMPACT,
                _QUOTED_RENAME_DIFF,
                "1 deleted Python modules not analysed (importers unchecked): pkg/modé.py",
                id="c-quoted-rename-from",
            ),
        ],
    )
    def test_gap_names_python_the_provider_never_read(
        self, adapter, answer: dict, diff_text: str | None, expected: str | None
    ) -> None:
        """Name every Python path a diff carries that the provider's post-image parser skipped.

        Each diff is a real ``git diff`` shape: an empty ``__init__.py`` deletion and a pure rename have no
        ``---``/``+++`` pair at all, so only their extended headers reveal the module that disappeared. A hunk-free
        change explains an answer that read nothing, but beside a mapped change it adds no structure and must not
        degrade the answer.
        """
        assert adapter.change_set_gap("diff-impact", answer, diff_text) == expected

    def test_real_git_quoted_deletion_names_the_unquoted_module(self, tmp_path: Path, adapter) -> None:
        """A real ``git diff`` deleting a non-ASCII module, with git set to C-quote it, still names that module.

        ``core.quotePath`` is passed on every git call, so the test never depends on the runner's global git config.
        Read verbatim, the quoted header ended in ``"`` rather than ``.py`` and the deleted module's importers went
        unreported.
        """
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "modé.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        _quoting_git(repo, "init", "-q")
        _quoting_git(repo, "add", ".")
        _quoting_git(repo, "commit", "-q", "-m", "base")
        _quoting_git(repo, "rm", "-q", "pkg/modé.py")
        diff = _quoting_git(repo, "diff", "--cached")
        assert '--- "a/pkg/mod\\303\\251.py"' in diff

        gap = adapter.change_set_gap("diff-impact", _REAL_DELETION_DIFF_IMPACT, diff)

        assert gap == "1 deleted Python modules not analysed (importers unchecked): pkg/modé.py"


# --- the adapter command line ---------------------------------------------------------------------------------------


class TestAdapterCli:
    """Drive the shipped `codemap_adapter.py` entry point the way installed skills do."""

    def test_context_persists_json_to_out_path(self, tmp_path: Path) -> None:
        """The ``context`` CLI mode prints JSON and persists the identical bytes to ``--out``.

        A fake launcher is first on PATH; the review category is queried with a diff file and an output path.
        """
        _write_fake_codemap_py(tmp_path, _HEALTHY_SCRIPT)
        out_path = tmp_path / "run" / "codemap-context.json"
        env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

        completed = _run_adapter_cli(
            env, "context", "--category", "review", "--diff-file", str(tmp_path / "diff.patch"), "--out", str(out_path)
        )

        assert completed.returncode == 0, completed.stderr
        stdout_payload = json.loads(completed.stdout)
        assert stdout_payload == json.loads(out_path.read_text(encoding="utf-8"))
        assert stdout_payload["status"] == "available"
        assert stdout_payload["protocol_version"] == "codemap-py.integration.v1"
        assert stdout_payload["artifact_schema_version"] == 4
        assert stdout_payload["diff_file"] == str(tmp_path / "diff.patch")
        assert stdout_payload["query_kind"] == "standard"
        launcher = stdout_payload["probe"]["launcher"]
        assert os.path.normcase(launcher) == os.path.normcase(str(tmp_path / _LAUNCHER_NAME))

    def test_skip_route_persists_without_codemap_on_path(self, tmp_path: Path) -> None:
        """The public CLI records an explicit skip even when no Codemap launcher can resolve.

        PATH holds nothing, yet the `skip` query kind must still write an auditable artifact and exit 0.
        """
        out_path = tmp_path / "run" / "codemap-context.json"

        completed = _run_adapter_cli(
            dict(os.environ, PATH=str(tmp_path)),
            *("context", "--category", "implementation", "--query-kind", "skip", "--out", str(out_path)),
        )

        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload == json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["query_kind"] == "skip"
        assert payload["status"] == "skipped"
        assert payload["probe"]["launcher"] is None
        assert payload["queries"] == []

    def test_probe_absent_exits_zero_and_reports_status(self, tmp_path: Path) -> None:
        """Absence is data, not a CLI failure — `probe` still exits `0`.

        PATH holds no launcher; callers read the `absent` status from the JSON rather than from the exit code.
        """
        completed = _run_adapter_cli(dict(os.environ, PATH=str(tmp_path)), "probe")

        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["status"] == "absent"

    def test_review_restores_provider_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Process-local review destinations must not leak into the caller or replace explicit frozen evidence.

        While gathering, the review CLI points the log directory at the run directory but keeps an explicit empty index
        directory and an explicit coordination directory; afterwards the caller's environment is exactly as it was, even
        when the provider call raises.
        """
        monkeypatch.setenv("CODEMAP_INDEX_DIR", "")
        monkeypatch.delenv("CODEMAP_LOG_DIR", raising=False)
        monkeypatch.setenv("CODEMAP_COORDINATION_DIR", str(tmp_path / "explicit-coordination"))
        monkeypatch.setenv("SCAN_NO_AUTOBUILD", "1")
        observed = []

        def unavailable_provider(*args, **kwargs):
            """Record the provider-facing environment, then fail like a crashed provider."""
            observed.append(
                {
                    key: os.environ.get(key)
                    for key in ("CODEMAP_INDEX_DIR", "CODEMAP_LOG_DIR", "CODEMAP_COORDINATION_DIR")
                }
            )
            raise RuntimeError("provider failed")

        monkeypatch.setattr(adapter, "gather_structural_context", unavailable_provider)

        with pytest.raises(RuntimeError, match="provider failed"):
            adapter.main(["context", "--category", "review", "--out", str(tmp_path / "run" / "context.json")])

        assert observed == [
            {
                "CODEMAP_INDEX_DIR": "",
                "CODEMAP_LOG_DIR": str(tmp_path / "run" / "codemap-logs"),
                "CODEMAP_COORDINATION_DIR": str(tmp_path / "explicit-coordination"),
            }
        ]
        assert os.environ["CODEMAP_INDEX_DIR"] == ""
        assert "CODEMAP_LOG_DIR" not in os.environ
        assert os.environ["CODEMAP_COORDINATION_DIR"] == str(tmp_path / "explicit-coordination")


# --- real codemap-py ------------------------------------------------------------------------------------------------

# A two-module package the real provider indexes for follow-up routes: `build` is defined twice, `helper` once, and
# `Report.render` is reached only through an instance, which static analysis never resolves. The never-called
# `QuarterlyLedgerReconciliation` methods have names long enough to push their zero-caller hints past the answer bound.
_FOLLOW_UP_FILES = {
    "pkg/__init__.py": "",
    "pkg/util.py": (
        "def helper():\n    return 1\n\n\ndef build():\n    return helper()\n\n\n"
        "class Report:\n    def __init__(self):\n        self.value = helper()\n\n"
        "    def render(self):\n        return self.value\n\n\n"
        "class QuarterlyLedgerReconciliation:\n"
        "    def reconcile_outstanding_balances_for_export(self):\n        return 0\n\n"
        "    def __enter__(self):\n        return self\n"
    ),
    "pkg/core.py": (
        "from pkg.util import Report, helper\n\n\ndef build():\n    return Report()\n\n\n"
        "def run():\n    report = build()\n    return report.render(), helper()\n"
    ),
}
_PROVIDER_ENV_KEYS = (
    "CODEMAP_BIN",
    "SCAN_NO_AUTOBUILD",
    "CODEMAP_INDEX_DIR",
    "CODEMAP_LOG_DIR",
    "CODEMAP_COORDINATION_DIR",
)


@pytest.fixture(name="follow_up_project", scope="module")
def _follow_up_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Index the follow-up package once with the real provider and return its root."""
    root = tmp_path_factory.mktemp("followup") / "proj"
    _index_real_project(root, _FOLLOW_UP_FILES)
    return root


def _init_dirty_checkout(source: Path) -> Path:
    """Create a git checkout with one committed module and an uncommitted edit; return its ``git diff HEAD`` file."""
    source.mkdir()
    for args in (("init", "-q"), ("config", "user.name", "Test"), ("config", "user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    (source / "leaf.py").write_text("def leaf():\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "leaf.py"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)
    (source / "leaf.py").write_text("def leaf():\n    return 1\n", encoding="utf-8")
    run = source.parent / "review"
    run.mkdir()
    diff = run / "diff.patch"
    diff.write_bytes(subprocess.check_output(["git", "-C", str(source), "diff", "HEAD"]))
    return diff


@_requires_real_cli
class TestRealCodemapCli:
    """Run the genuine `codemap-py` launcher: its grammar, answers, and provider-side refusals."""

    def test_root_scoped_query_runs_against_real_cli(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Unmocked regression: a root-scoped query drives the ACTUAL codemap-py grammar.

        ``--root`` must precede the subcommand. Most tests in this file fake the subprocess, so none exercises
        argparse's real grammar — the pre-fix argv order (``query central --root .``) passed those fakes yet errors
        against the real CLI. This runs the genuine launcher on a tiny real fixture and asserts a root-scoped query
        returns exit 0 with parseable metadata.
        """
        monkeypatch.setenv("PATH", _PATH_WITH_CODEMAP)
        fixture = _indexed_real_fixture(tmp_path)
        monkeypatch.chdir(fixture)
        resolution = adapter._resolve_codemap_executable()
        assert resolution.launcher is not None

        outcome = adapter._run_one_query(
            resolution.launcher, adapter.QuerySpec("central", requires_target=False), None, fixture, 30.0
        )

        assert outcome.exit_code == 0, outcome.error
        assert outcome.error is None

    @pytest.mark.parametrize(
        "index_dir_setting",
        [
            pytest.param(lambda tmp, run: ({}, run / "codemap-index"), id="unset"),
            pytest.param(lambda tmp, run: ({"CODEMAP_INDEX_DIR": ""}, run / "codemap-index"), id="empty"),
            pytest.param(lambda tmp, run: ({"CODEMAP_INDEX_DIR": str(tmp / "cache")}, tmp / "cache"), id="explicit"),
        ],
    )
    def test_review_cli_preparation_preserves_source_status(
        self, tmp_path: Path, index_dir_setting: Callable[..., tuple[dict[str, str], Path]]
    ) -> None:
        """Automatic cache preparation must not invalidate a review checkout lacking a cache ignore rule.

        A real git checkout with an uncommitted edit is reviewed through the CLI with the index directory unset, empty,
        or explicit; the cache lands outside the source tree, so `git status` is byte-identical and no `.cache` appears.
        """
        source = tmp_path / "source"
        diff = _init_dirty_checkout(source)
        run = diff.parent
        status_command = ["git", "-C", str(source), "status", "--porcelain=v1", "--untracked-files=all"]
        before = subprocess.check_output(status_command)
        overrides, expected_cache = index_dir_setting(tmp_path, run)
        env = {key: value for key, value in os.environ.items() if key not in _PROVIDER_ENV_KEYS}
        env.update(PATH=_PATH_WITH_CODEMAP, **overrides)

        result = _run_adapter_cli(
            env,
            *("context", "--category", "review", "--root", str(source), "--diff-file", str(diff)),
            *("--out", str(run / "codemap-context.json")),
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["queries"][0]["exit_code"] == 0, payload
        assert payload["queries"][0]["query_complete"] is True
        assert subprocess.check_output(status_command) == before
        assert not (source / ".cache").exists()
        assert Path(payload["probe"]["doctor"]["index_path"]) == expected_cache / "source.json"
        assert payload["index_path_divergence"] == []

    def test_usage_error_is_a_provider_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter) -> None:
        """The real provider rejects an unknown flag with a bare exit 2, which reduces to ``incompatible``.

        This pins the provider behaviour the adapter's exit-2 rule depends on: no JSON ``error`` object on stdout, usage
        on stderr, so the recorded outcome never claims the provider diagnosed caller input.
        """
        monkeypatch.setenv("PATH", _PATH_WITH_CODEMAP)
        fixture = _indexed_real_fixture(tmp_path)
        launcher = adapter._resolve_codemap_executable().launcher
        assert launcher is not None
        spec = adapter.QuerySpec("diff-impact", requires_target=False, extra_args=("--bogus-flag", "x"))

        outcome = adapter._run_one_query(launcher, spec, None, fixture, 30.0)

        assert (outcome.exit_code, outcome.input_rejected) == (2, False)
        assert adapter.reduce_status("available", [outcome]) == "incompatible"

    @pytest.mark.parametrize(
        ("query_kind", "target", "expected"),
        [
            pytest.param(
                "callers",
                "build",
                (False, True, "candidates", ["pkg.core::build", "pkg.util::build"]),
                id="ambiguous-bare-name-keeps-candidates",
            ),
            pytest.param(
                "callers",
                "pkg.util.helper",
                (True, False, "normalized_from", "pkg.util.helper"),
                id="dotted-name-resolves",
            ),
            pytest.param(
                "callers", "helper", (True, False, "normalized_from", "helper"), id="unique-bare-name-resolves"
            ),
            pytest.param(
                "dependencies",
                "pkg.nothere",
                (False, True, "rejected_target", "pkg.nothere"),
                id="unindexed-module-is-rejected-input",
            ),
            pytest.param(
                "test-impact",
                "pkg.nothere",
                (False, True, "rejected_target", "pkg.nothere"),
                id="missing-test-impact-module-is-rejected-input",
            ),
        ],
    )
    def test_follow_up_target_resolution(
        self,
        monkeypatch: pytest.MonkeyPatch,
        follow_up_project: Path,
        adapter,
        query_kind: str,
        target: str,
        expected: tuple,
    ) -> None:
        """The real provider resolves dotted and unique bare targets, and its refusals degrade instead of ending the
        run.

        An ambiguous, missing, or unindexed follow-up target was recorded as an ``incompatible`` provider, which forbids
        every later follow-up, and the provider's candidate list was dropped from the artifact.
        """
        monkeypatch.setenv("PATH", _PATH_WITH_CODEMAP)

        record = adapter.gather_structural_context(
            "review", target=target, root=follow_up_project, query_kind=query_kind
        ).to_dict()

        query = record["queries"][0]
        succeeded, rejected, key, value = expected
        assert (query["error"] is None, query["input_rejected"], query["answer"][key]) == (succeeded, rejected, value)
        assert record["status"] in {"degraded", "stale+degraded"}

    def test_zero_caller_method_keeps_the_dispatch_hint(
        self, monkeypatch: pytest.MonkeyPatch, follow_up_project: Path, adapter
    ) -> None:
        """A method called only through an instance keeps the provider's hint in the answer and the status reasons.

        ``report.render()`` is invisible to static analysis, so the real provider answers zero callers with
        ``query_complete`` unchanged; without the hint the follow-up read as proof that ``render`` is unused.
        """
        monkeypatch.setenv("PATH", _PATH_WITH_CODEMAP)

        record = adapter.gather_structural_context(
            "review", target="pkg.util::Report.render", root=follow_up_project, query_kind="callers"
        ).to_dict()

        answer = record["queries"][0]["answer"]
        assert (answer["count"], 'grep -rnE "\\.render\\b"' in answer["hint"]) == (0, True)
        assert f"fn-rdeps: hint: {answer['hint']}" in record["status_reasons"]

    @pytest.mark.parametrize(
        ("target", "action"),
        [
            pytest.param(
                "pkg.util::QuarterlyLedgerReconciliation.reconcile_outstanding_balances_for_export",
                'Find references with grep -rnE "\\.reconcile_outstanding_balances_for_export\\b".',
                id="method-keeps-its-search",
            ),
            pytest.param(
                "pkg.util::QuarterlyLedgerReconciliation.__enter__",
                "Never delete protocol method QuarterlyLedgerReconciliation.__enter__ on zero callers.",
                id="protocol-method-keeps-do-not-delete",
            ),
        ],
    )
    def test_hint_action_survives_the_answer_text_bound(
        self, monkeypatch: pytest.MonkeyPatch, follow_up_project: Path, adapter, target: str, action: str
    ) -> None:
        """A real hint longer than the persisted string bound is cut, and the cut copy still carries its action.

        The answer and its ``status_reasons`` line keep 300 characters. With the search placed last, every method named
        longer than ``Report.render`` lost its grep, and every protocol method lost "do not delete" mid-sentence.
        """
        monkeypatch.setenv("PATH", _PATH_WITH_CODEMAP)

        record = adapter.gather_structural_context(
            "review", target=target, root=follow_up_project, query_kind="callers"
        ).to_dict()

        query = record["queries"][0]
        assert (query["answer"]["count"], "hint" in query["answer_truncated"]) == (0, True)
        assert query["answer"]["hint"].startswith(action)
        assert f"fn-rdeps: hint: {action}" in "\n".join(record["status_reasons"])

    @pytest.mark.parametrize(
        ("root_name", "diff_text", "expected_status", "expected_reasons", "expected_changed"),
        [
            pytest.param("proj", _PYTHON_CHANGE_DIFF, "available", [], 1, id="root-owns-index"),
            pytest.param(
                "proj/nested",
                _PYTHON_CHANGE_DIFF,
                "degraded",
                [f"diff-impact: {_UNMAPPED_GAP}"],
                1,
                id="explicit-nested-root-owns-its-empty-index",
            ),
            pytest.param(
                "proj", _NEW_MODULE_DIFF, "degraded", [f"diff-impact: {_UNMAPPED_GAP}"], 1, id="new-module-unmapped"
            ),
            pytest.param(
                "proj",
                _DELETED_MODULE_DIFF,
                "degraded",
                [f"diff-impact: {_DELETED_LEAF_GAP}"],
                0,
                id="imported-module-deleted",
            ),
        ],
    )
    def test_review_diff_file_query(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        adapter,
        root_name: str,
        diff_text: str,
        expected_status: str,
        expected_reasons: list[str],
        expected_changed: int,
    ) -> None:
        """The real provider answers from the owning root; mismatches, new modules, and deletions surface as gaps.

        The caller's working directory is outside both trees and the diff file is passed relative to it, so only the
        adapter's own path handling decides which index the provider opens and which diff it reads. Deleting
        ``leaf.py``, which ``consumer.py`` imports, makes the real provider read zero files and report ``LOW`` risk as
        complete.
        """
        monkeypatch.setenv("PATH", _PATH_WITH_CODEMAP)
        _indexed_real_fixture(tmp_path)
        (tmp_path / "diff.patch").write_text(diff_text, encoding="utf-8", newline="\n")
        monkeypatch.chdir(tmp_path)

        record = adapter.gather_structural_context(
            "review", root=Path(root_name), diff_file=Path("diff.patch")
        ).to_dict()

        assert (record["status"], record["status_reasons"]) == (expected_status, expected_reasons)
        assert record["queries"][0]["answer"]["changed_files"] == expected_changed

    def test_fake_diff_impact_shape_matches_real_cli(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter
    ) -> None:
        """Keep the provider-faithful fake's keys identical to what the real provider prints for the same change.

        Fakes that invent fields the provider never emits let tests pass for answers no real run can produce.
        """
        monkeypatch.setenv("PATH", _PATH_WITH_CODEMAP)
        _indexed_real_fixture(tmp_path)
        (tmp_path / "diff.patch").write_text(_PYTHON_CHANGE_DIFF, encoding="utf-8", newline="\n")

        answer = adapter.gather_structural_context(
            "review", root=tmp_path / "proj", diff_file=tmp_path / "diff.patch"
        ).to_dict()["queries"][0]["answer"]

        expected = {key: value for key, value in _REAL_MAPPED_DIFF_IMPACT.items() if key != "index"}
        assert set(answer) == set(expected)
        assert set(answer["changed_modules"][0]) == set(expected["changed_modules"][0])
        assert {key: answer[key] for key in ("changed_files", "unmapped_files", "highest_risk")} == {
            key: expected[key] for key in ("changed_files", "unmapped_files", "highest_risk")
        }
