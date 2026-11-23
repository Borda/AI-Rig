"""Hook failure counts: what the hooks record about their own failures and what ``codemap-py doctor`` reports.

Every codemap hook fails open, so before these counters a hook that failed on every call looked exactly like a hook that
had nothing to do. Each hook now records a swallowed failure (exception class and UTC time only) and stamps its last
clean run under ``<log-dir>/<runtime>/hook-health/<hook>/``; ``doctor`` reads both runtimes back as ``hook_failures``.
These tests drive the real hook scripts where the failure can be forced through their input, and the reader against
missing, corrupt, FIFO and linked counter paths.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest
from _host_skips import skip_file_symlink_unavailable
from codemap_py import cli, runtime_log

_PLUGIN = Path(__file__).resolve().parents[2]
_HOOKS = _PLUGIN / "hooks"
_HOOK_TIMEOUT_S = 60
#: Hook roster restated here so a hook added without health recording fails the roster test, not silently.
_HOOK_STEMS = (
    "guard-redundant-scan",
    "inject-preamble",
    "log-skill-start",
    "log-tool-use",
    "record-exhausted",
    "seed-session",
)
#: Keys of the text lines doctor appends, in output order: every hook of the Claude runtime, then of Codex.
_HOOK_LINE_KEYS = [f"hook_failures.{runtime}.{hook}" for runtime in ("claude", "codex") for hook in _HOOK_STEMS]
_EMPTY = {"count": 0, "last_class": None, "last_time": None, "last_success": None}
#: A well-formed failure record name, used to prove a reader did not follow a link or read a FIFO.
_VALID_RECORD = f"fail-{1_700_000_000_000_000_000:020d}-0123abcd-OSError"
_RECORD_WRITER = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import _hookutil\n"
    "for _ in range(int(sys.argv[2])):\n"
    "    _hookutil.record_failure('log-tool-use', OSError('concurrent writer'))\n"
)


def _load(path: Path, name: str) -> ModuleType:
    """Import one hook-side file by path under a private module name."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hook_env(tmp_path: Path, runtime: str, **extra: str) -> dict[str, str]:
    """Return a hook subprocess environment with logging on, one runtime, and a private temp directory."""
    env = {key: value for key, value in os.environ.items() if key != "CODEMAP_LOG_DIR"}
    temp = tmp_path / "hook-tmp"
    temp.mkdir(exist_ok=True)
    env.update({"CODEMAP_LOGGING": "true", "CODEMAP_RUNTIME": runtime, "TMPDIR": str(temp)})
    env.update(extra)
    return env


def _run_hook(stem: str, stdin: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one shipped hook script the way its launcher does, with *stdin* as the event."""
    return subprocess.run(
        [sys.executable, str(_HOOKS / f"{stem}.py")],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=_HOOK_TIMEOUT_S,
        check=False,
    )


def _doctor_report(capsys: pytest.CaptureFixture[str], *args: str) -> str:
    """Run ``doctor`` in-process from the current directory and return its stdout."""
    capsys.readouterr()
    assert cli.main(["doctor", *args], plugin_root=_PLUGIN) == 0
    return capsys.readouterr().out


def _health_dir(project: Path, runtime: str, hook: str) -> Path:
    """Return the health directory the hooks write for *hook* under *runtime* in *project*."""
    return runtime_log.log_dir_for(runtime, root=project, override="") / "hook-health" / hook


def _record_files(directory: Path) -> list[str]:
    """Return the failure record names on disk in one health directory."""
    return [path.name for path in directory.iterdir() if path.name.startswith("fail-")]


def _record_concurrently(hookutil: ModuleType, writers: int) -> None:
    """Release *writers* threads together, each recording one failure of ``log-tool-use``."""
    barrier = threading.Barrier(writers)

    def write(_: int) -> None:
        barrier.wait()
        hookutil.record_failure("log-tool-use", OSError("concurrent writer"))

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(write, range(writers)))


def _record_from_processes(project: Path, env: dict[str, str], processes: int, each: int) -> None:
    """Start *processes* interpreters at once, each recording *each* failures through the shipped ``_hookutil``."""
    command = [sys.executable, "-c", _RECORD_WRITER, str(_HOOKS), str(each)]
    running = [subprocess.Popen(command, cwd=project, env=env) for _ in range(processes)]
    assert [proc.wait(timeout=_HOOK_TIMEOUT_S) for proc in running] == [0] * processes


def _record_series(hookutil: ModuleType, classes: list[type[BaseException]]) -> None:
    """Record one failure of ``log-tool-use`` per class in *classes*, in order."""
    for cls in classes:
        hookutil.record_failure("log-tool-use", cls("series"))


@pytest.fixture(name="project")
def _project(tmp_path: Path) -> Path:
    """Return a real git repository, so the hook anchor and the doctor anchor are both exercised."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=_HOOK_TIMEOUT_S)
    return root.resolve()


@pytest.fixture(name="in_project")
def _in_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in-process recorders and doctor from *project* with logging on under the Claude runtime."""
    monkeypatch.chdir(project)
    monkeypatch.delenv("CODEMAP_LOG_DIR", raising=False)
    monkeypatch.setenv("CODEMAP_LOGGING", "true")
    monkeypatch.setenv("CODEMAP_RUNTIME", "claude")
    return project


@pytest.fixture(name="hookutil")
def _hookutil(in_project: Path) -> ModuleType:
    """Load the shipped ``_hookutil`` by path, as the hooks import it, after the project environment is set."""
    return _load(_HOOKS / "_hookutil.py", "hook_health_hookutil")


class TestRoster:
    """The hook-side and doctor-side rosters cover exactly the shipped hooks."""

    def test_both_layers_name_every_shipped_hook(self) -> None:
        """Every hook script is in the writer roster and the doctor roster, in one order.

        ``record_failure`` ignores a name outside its roster, so a hook missing from it would fail silently forever.
        """
        shipped = tuple(sorted(path.stem for path in _HOOKS.glob("*.py") if not path.stem.startswith("_")))
        hookutil = _load(_HOOKS / "_hookutil.py", "hook_health_roster")

        assert (hookutil.HOOK_NAMES, runtime_log.HOOK_NAMES, shipped) == (_HOOK_STEMS, _HOOK_STEMS, _HOOK_STEMS)


class TestForcedHookFailure:
    """A failure forced inside a real hook shows a nonzero count in ``doctor --json`` for its own runtime only."""

    @pytest.mark.parametrize(
        ("stem", "runtime", "other"),
        [
            pytest.param("guard-redundant-scan", "claude", "codex", id="guard-claude"),
            pytest.param("guard-redundant-scan", "codex", "claude", id="guard-codex"),
            pytest.param("log-skill-start", "claude", "codex", id="skill-start-claude"),
            pytest.param("log-skill-start", "codex", "claude", id="skill-start-codex"),
            pytest.param("log-tool-use", "claude", "codex", id="tool-use-claude"),
            pytest.param("log-tool-use", "codex", "claude", id="tool-use-codex"),
            pytest.param("record-exhausted", "claude", "codex", id="exhausted-claude"),
            pytest.param("record-exhausted", "codex", "claude", id="exhausted-codex"),
            pytest.param("seed-session", "claude", "codex", id="seed-claude"),
        ],
    )
    def test_malformed_event_is_counted_for_its_runtime(
        self,
        stem: str,
        runtime: str,
        other: str,
        in_project: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A malformed event fails the hook open, and doctor counts one failure for that hook and runtime.

        The hook still exits 0 with no output; the swallowed ``JSONDecodeError`` is what doctor must surface.
        """
        result = _run_hook(stem, "{", in_project, _hook_env(tmp_path, runtime))

        failures = json.loads(_doctor_report(capsys, "--json"))["hook_failures"]

        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        assert failures[runtime][stem]["count"] == 1
        assert failures[runtime][stem]["last_class"] == "JSONDecodeError"
        assert failures[runtime][stem]["last_time"] is not None
        assert failures[other][stem] == _EMPTY

    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_inject_preamble_failure_is_counted_for_its_runtime(
        self, runtime: str, in_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A git probe that raises inside the preamble hook is swallowed and counted for that runtime.

        The preamble hook treats malformed stdin as an empty event by design, so its failure is forced one step later,
        at the first git call.
        """
        monkeypatch.setenv("CODEMAP_RUNTIME", runtime)
        hook = _load(_HOOKS / "inject-preamble.py", f"hook_health_preamble_{runtime}")

        def _git_fails(*_args: object, **_kwargs: object) -> str:
            raise OSError("forced git failure")

        monkeypatch.setattr(hook, "git_output", _git_fails)

        returned = hook.main()

        failures = json.loads(_doctor_report(capsys, "--json"))["hook_failures"]
        assert returned == 0
        assert (failures[runtime]["inject-preamble"]["count"], failures[runtime]["inject-preamble"]["last_class"]) == (
            1,
            "OSError",
        )

    def test_hook_run_from_subdirectory_is_read_by_doctor_at_the_root(
        self, in_project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Hook and doctor anchor the log directory with separate code, and still meet in one directory.

        The hook walks up for ``.git`` from a subdirectory; the CLI asks git for the top level from the root. A
        disagreement would put the record where doctor never looks, with both sides reporting success.
        """
        subdir = in_project / "pkg" / "inner"
        subdir.mkdir(parents=True)
        _run_hook("log-tool-use", "{", subdir, _hook_env(tmp_path, "claude"))

        failures = json.loads(_doctor_report(capsys, "--json"))["hook_failures"]

        assert failures["claude"]["log-tool-use"]["count"] == 1


class TestCleanRun:
    """A hook run that ends without an exception stamps the last success and records no failure."""

    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_early_return_stamps_last_success(
        self, runtime: str, in_project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An event the hook ignores still counts as a clean run, so a hook that stops running shows staleness."""
        _run_hook("log-skill-start", json.dumps({"tool_name": "Read"}), in_project, _hook_env(tmp_path, runtime))

        entry = json.loads(_doctor_report(capsys, "--json"))["hook_failures"][runtime]["log-skill-start"]

        assert (entry["count"], entry["last_class"]) == (0, None)
        assert entry["last_success"] is not None

    def test_logging_opt_out_writes_no_health_record(self, project: Path, tmp_path: Path) -> None:
        """``CODEMAP_LOGGING=false`` keeps hook health out of the log directory, like every other log write."""
        _run_hook("record-exhausted", "{", project, _hook_env(tmp_path, "claude", CODEMAP_LOGGING="false"))

        assert not (project / ".cache" / "codemap" / "logs" / "claude" / "hook-health").exists()


class TestRecorderSafety:
    """The recorder never changes how a hook ends, even when recording itself fails."""

    @pytest.mark.parametrize(
        "stem", ["guard-redundant-scan", "log-skill-start", "log-tool-use", "record-exhausted", "seed-session"]
    )
    def test_unwritable_log_directory_leaves_the_hook_silent(self, stem: str, project: Path, tmp_path: Path) -> None:
        """A log root that is a regular file makes recording fail; the failing hook still exits 0 with no output."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("occupied", encoding="utf-8")

        result = _run_hook(stem, "{", project, _hook_env(tmp_path, "claude", CODEMAP_LOG_DIR=str(blocker)))

        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")

    def test_recorder_error_of_any_class_never_reaches_the_hook(
        self, in_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected error inside the recorder is dropped; the hook returns 0 exactly as before."""
        hook = _load(_HOOKS / "seed-session.py", "hook_health_seed_session")

        def _broken(_hook: str) -> Path:
            raise RuntimeError("recorder broke")

        monkeypatch.setattr(hook._hookutil, "_health_dir", _broken)
        monkeypatch.setattr(sys, "stdin", io.StringIO("{"))

        assert hook.main() == 0

    def test_unexpected_exception_is_recorded_and_re_raised(self, hookutil: ModuleType, in_project: Path) -> None:
        """An exception outside a hook's swallowed classes keeps its old exit behaviour and is still counted."""
        with pytest.raises(RuntimeError, match="unexpected"), hookutil.hook_outcome("seed-session", (OSError,)):
            raise RuntimeError("unexpected")

        entry = runtime_log.hook_health("claude", root=in_project, override="")["seed-session"]
        assert (entry["count"], entry["last_class"], entry["last_success"]) == (1, "RuntimeError", None)

    def test_record_keeps_class_and_time_but_never_the_message(
        self, hookutil: ModuleType, in_project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Doctor output carries the exception class, never the message, path or identity it was raised with."""
        hookutil.record_failure("log-tool-use", PermissionError("secret-token /private/user/path"))

        report = _doctor_report(capsys, "--json")

        assert "PermissionError" in report
        assert "secret-token" not in report
        assert "/private/user/path" not in report

    @skip_file_symlink_unavailable
    def test_linked_health_directory_is_not_written_through(
        self, hookutil: ModuleType, in_project: Path, tmp_path: Path
    ) -> None:
        """A planted link at ``hook-health`` never redirects a record outside the log directory."""
        outside = tmp_path / "outside"
        outside.mkdir()
        logs = in_project / ".cache" / "codemap" / "logs" / "claude"
        logs.mkdir(parents=True)
        (logs / "hook-health").symlink_to(outside, target_is_directory=True)

        hookutil.record_failure("log-tool-use", OSError("redirected"))

        assert list(outside.iterdir()) == []

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="host does not expose FIFO creation")
    def test_fifo_at_hook_directory_does_not_block_the_recorder(self, hookutil: ModuleType, in_project: Path) -> None:
        """A FIFO where the per-hook directory belongs makes recording a no-op instead of a hang or an error."""
        health = in_project / ".cache" / "codemap" / "logs" / "claude" / "hook-health"
        health.mkdir(parents=True)
        os.mkfifo(health / "log-tool-use")

        hookutil.record_failure("log-tool-use", OSError("fifo"))

        assert runtime_log.hook_health("claude", root=in_project, override="")["log-tool-use"] == _EMPTY


class TestConcurrentWriters:
    """Concurrent hook processes never lose each other's failure records up to the retention count."""

    def test_concurrent_threads_are_all_counted(self, hookutil: ModuleType, in_project: Path) -> None:
        """Forty failures released at once give a count of exactly forty."""
        _record_concurrently(hookutil, 40)

        assert runtime_log.hook_health("claude", root=in_project, override="")["log-tool-use"]["count"] == 40

    def test_concurrent_processes_are_all_counted(self, project: Path, tmp_path: Path) -> None:
        """Eight interpreters recording five failures each give a count of exactly forty.

        Separate processes are the real case: every hook invocation is its own interpreter.
        """
        _record_from_processes(project, _hook_env(tmp_path, "codex"), processes=8, each=5)

        health = runtime_log.hook_health("codex", root=project, override="")
        assert health["log-tool-use"]["count"] == 40

    def test_count_is_capped_at_retention(self, hookutil: ModuleType, in_project: Path) -> None:
        """Eighty concurrent failures report the retention count, never more and never fewer."""
        _record_concurrently(hookutil, 80)

        assert runtime_log.hook_health("claude", root=in_project, override="")["log-tool-use"]["count"] == 50

    def test_oldest_records_are_trimmed_on_write(self, hookutil: ModuleType, in_project: Path) -> None:
        """Past the retention count the oldest records are deleted, keeping exactly the newest fifty."""
        _record_series(hookutil, [OSError] * 5 + [ValueError] * 50)

        names = _record_files(_health_dir(in_project, "claude", "log-tool-use"))
        assert (len(names), {name.rsplit("-", 1)[1] for name in names}) == (50, {"ValueError"})

    def test_trimming_keeps_the_newest_when_the_clock_does_not_advance(
        self, hookutil: ModuleType, in_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clock that returns one value for every call still orders records by when they were written.

        Windows before Python 3.13 reads a timer that advances every 15.6 ms, so a burst of failures shares one
        timestamp; ordering such records by their random token trimmed the newest ones instead of the oldest.
        """
        monkeypatch.setattr(hookutil.time, "time_ns", lambda: 1_700_000_000_000_000_000)
        _record_series(hookutil, [OSError] * 5 + [ValueError] * 50)

        names = _record_files(_health_dir(in_project, "claude", "log-tool-use"))
        assert (len(names), {name.rsplit("-", 1)[1] for name in names}) == (50, {"ValueError"})
        health = runtime_log.hook_health("claude", root=in_project, override="")["log-tool-use"]
        assert health["last_class"] == "ValueError"


def _missing(_base: Path) -> None:
    """Leave the runtime log directory absent."""


def _health_is_a_file(base: Path) -> None:
    """Put a regular file where the ``hook-health`` directory belongs."""
    base.mkdir(parents=True)
    (base / "hook-health").write_text("not a directory", encoding="utf-8")


def _hook_dir_is_a_file(base: Path) -> None:
    """Put a regular file where the per-hook directory belongs."""
    (base / "hook-health").mkdir(parents=True)
    (base / "hook-health" / "log-tool-use").write_text(_VALID_RECORD, encoding="utf-8")


def _junk_names(base: Path) -> None:
    """Fill the per-hook directory with names that are not records and a record-shaped directory."""
    directory = base / "hook-health" / "log-tool-use"
    directory.mkdir(parents=True)
    (directory / "fail-1-x-OSError").write_text("", encoding="utf-8")
    (directory / f"fail-{0:020d}-zzzzzzzz-OSError").write_text("", encoding="utf-8")
    (directory / f"fail-{0:020d}-0123abcd-Bad-Class").write_text("", encoding="utf-8")
    (directory / "notes.txt").write_text(_VALID_RECORD, encoding="utf-8")
    (directory / _VALID_RECORD).mkdir()
    (directory / "last-success").write_text("not a timestamp\n", encoding="utf-8")


def _fifo_hook_dir(base: Path) -> None:
    """Put a FIFO where the per-hook directory belongs."""
    (base / "hook-health").mkdir(parents=True)
    os.mkfifo(base / "hook-health" / "log-tool-use")


def _fifo_entries(base: Path) -> None:
    """Put a FIFO at the stamp path and a record-shaped FIFO in the per-hook directory."""
    directory = base / "hook-health" / "log-tool-use"
    directory.mkdir(parents=True)
    os.mkfifo(directory / "last-success")
    os.mkfifo(directory / _VALID_RECORD)


def _outside_records(base: Path) -> Path:
    """Return a directory outside the log tree holding a valid record and stamp."""
    outside = base.parent.parent / "outside-records"
    outside.mkdir(parents=True)
    (outside / _VALID_RECORD).write_text("", encoding="utf-8")
    (outside / "last-success").write_text(f"{1_700_000_000_000_000_000:020d}\n", encoding="utf-8")
    return outside


def _linked_health(base: Path) -> None:
    """Make ``hook-health`` a link to a directory whose per-hook entry holds valid records."""
    outside = _outside_records(base)
    (outside / "log-tool-use").mkdir()
    (outside / "log-tool-use" / _VALID_RECORD).write_text("", encoding="utf-8")
    base.mkdir(parents=True)
    (base / "hook-health").symlink_to(outside, target_is_directory=True)


def _linked_hook_dir(base: Path) -> None:
    """Make the per-hook directory a link to a directory holding valid records."""
    outside = _outside_records(base)
    (base / "hook-health").mkdir(parents=True)
    (base / "hook-health" / "log-tool-use").symlink_to(outside, target_is_directory=True)


_needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="host does not expose FIFO creation")


class TestReaderRobustness:
    """Doctor reads zero, without raising or hanging, from any counter path it did not write itself."""

    @pytest.mark.parametrize(
        "arrange",
        [
            pytest.param(_missing, id="missing"),
            pytest.param(_health_is_a_file, id="health-dir-is-file"),
            pytest.param(_hook_dir_is_a_file, id="hook-dir-is-file"),
            pytest.param(_junk_names, id="corrupt-names-and-stamp"),
            pytest.param(_fifo_hook_dir, id="fifo-hook-dir", marks=_needs_fifo),
            pytest.param(_fifo_entries, id="fifo-stamp-and-record", marks=_needs_fifo),
            pytest.param(_linked_health, id="linked-health-dir", marks=skip_file_symlink_unavailable),
            pytest.param(_linked_hook_dir, id="linked-hook-dir", marks=skip_file_symlink_unavailable),
        ],
    )
    def test_untrusted_counter_path_reads_as_zero(self, arrange: Callable[[Path], object], project: Path) -> None:
        """Anything other than a plain directory of well-formed records reads as no failures and no success."""
        arrange(runtime_log.log_dir_for("claude", root=project, override=""))

        health = runtime_log.hook_health("claude", root=project, override="")

        assert health["log-tool-use"] == _EMPTY

    def test_text_output_lists_every_runtime_and_hook(
        self, hookutil: ModuleType, in_project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Plain ``doctor`` adds one line per runtime and hook after the existing lines."""
        hookutil.record_failure("log-tool-use", ValueError("text"))

        tail = _doctor_report(capsys).splitlines()[-2 * len(_HOOK_STEMS) :]

        assert [line.split(":", 1)[0] for line in tail] == _HOOK_LINE_KEYS
        assert tail[_HOOK_LINE_KEYS.index("hook_failures.claude.log-tool-use")].startswith(
            "hook_failures.claude.log-tool-use: count=1 last_class=ValueError last_time="
        )
