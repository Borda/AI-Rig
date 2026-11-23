"""Contributor-run integrity helper: Git metadata and the user's checkout stay unchanged around contributor code.

The checks run the shipped CLI against real repositories and a real linked worktree, the shape a code review uses,
because the failure they guard against is state written through the shared common Git directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "shared" / "contributor_run_integrity.py"
_ENV = os.environ | {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits, change times and symlinks")


def _git(repo: Path, *args: str) -> str:
    """Run one Git command that must succeed and return its output."""
    done = subprocess.run(["git", "-C", str(repo), *args], env=_ENV, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _helper(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the helper CLI and return its result without raising on a non-zero exit."""
    cmd = [sys.executable, "-I", str(HELPER), *args]
    return subprocess.run(cmd, env=_ENV, capture_output=True, text=True, check=False, cwd=cwd)


def _populate(directory: Path, count: int) -> None:
    """Create enough ordinary files to exercise the diagnostic storage and output limits."""
    for number in range(count):
        (directory / f"entry-{number}").touch()


def _populate_runs(family: Path, count: int) -> None:
    """Create report groups that exceed the checkout's review-run diagnostic budget."""
    for number in range(1, count + 1):
        run = family / f"run-{number:03}"
        run.mkdir(parents=True)
        (run / "gate.json").write_text("{}", encoding="utf-8")


def _set_lock_attribute(lock: Path) -> None:
    """Write a real user extended attribute through the API available on this host."""
    if hasattr(os, "setxattr"):
        os.setxattr(lock, "user.integrity", b"changed")
    else:
        subprocess.run(["/usr/bin/xattr", "-w", "user.integrity", "changed", str(lock)], check=True)


def _lock_metadata_available(lock: Path) -> bool:
    """Check whether this actual test file exposes creation time, access metadata and filesystem flags."""
    try:
        if sys.platform == "darwin":
            attributes = subprocess.check_output(["/usr/bin/xattr", str(lock)]).splitlines()
            acl = subprocess.check_output(["/bin/ls", "-lde", str(lock)]).splitlines()[1:]
            return set(attributes) <= {b"com.apple.provenance"} and not acl
        if sys.platform.startswith("linux") and not os.listxattr(lock):
            birth = subprocess.check_output(["/usr/bin/stat", "-c", "%W", str(lock)]).strip()
            flags = subprocess.check_output(["/usr/bin/lsattr", "-d", str(lock)]).split()
            return int(birth) > 0 and bool(flags)
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    return False


class Baseline:
    """One snapshot of a repository together with the digest the caller keeps from it."""

    def __init__(self, out: Path) -> None:
        """Remember where the baseline file is written."""
        self.file = out
        self.expect = ""

    def take(self, repo: Path, *options: str) -> subprocess.CompletedProcess[str]:
        """Record the baseline for ``repo``, keep the printed digest and return the helper's result."""
        result = _helper("snapshot", "--repo", str(repo), *options, "--out", str(self.file))
        if result.returncode == 0:
            self.expect = json.loads(result.stdout)["sha256"]
        return result

    def verify(self) -> tuple[int, list[str]]:
        """Verify against the kept digest and return ``(exit code, changed labels)``."""
        result = _helper("verify", "--snapshot", str(self.file), "--expect", self.expect)
        changed = json.loads(result.stdout)["changed"] if result.stdout.strip() else []
        return result.returncode, changed

    def restart(self, observed: str, out: Path) -> subprocess.CompletedProcess[str]:
        """Request a distinct baseline for the ignored state identified by a failed verification."""
        return _helper(
            "restart-inspected",
            "--snapshot",
            str(self.file),
            "--expect",
            self.expect,
            "--inspected-sha256",
            observed,
            "--out",
            str(out),
        )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create a user's checkout with one commit, an ignored interpreter environment and an ignored report tree."""
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".venv/\n.reports/\n", encoding="utf-8")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("m\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def baseline(checkout: Path, tmp_path: Path) -> Baseline:
    """Record the checkout's Git metadata, as taken before any contributor code runs."""
    snap = Baseline(tmp_path / "integrity.json")
    snap.take(checkout)
    return snap


@pytest.fixture
def ignored_observation(checkout: Path, baseline: Baseline) -> str:
    """Capture the verifier's observation for a single change in an ignored report."""
    report = checkout / ".reports" / "result.txt"
    report.parent.mkdir()
    report.write_bytes(b"fixture result\n")
    failure = _helper("verify", "--snapshot", str(baseline.file), "--expect", baseline.expect)
    assert failure.returncode == 3
    observed = json.loads(failure.stdout)
    assert observed["changed"] == ["main-checkout:ignored"]
    assert observed["ignored_changes"] == {"main-checkout:ignored": {"added": [".reports/result.txt"]}}
    return observed["observed_sha256"]


@pytest.fixture
def review(checkout: Path, tmp_path: Path) -> Path:
    """Add a linked review worktree outside the user's checkout."""
    path = tmp_path / "review"
    _git(checkout, "worktree", "add", "-q", "--detach", str(path))
    return path


@pytest.fixture
def independent_review(checkout: Path, tmp_path: Path) -> Path:
    """Create a registered detached checkout in an independent Git repository with its own object store."""
    private = tmp_path / "private"
    _git(checkout, "clone", "-q", "--no-local", str(checkout), str(private))
    review = tmp_path / "private-review"
    _git(private, "worktree", "add", "-q", "--detach", str(review))
    return review


def _slot(*args: str) -> dict[str, str]:
    """Run an ownership transition or read its state using the shipped checkout scheduler."""
    done = subprocess.run(
        [sys.executable, "-I", str(HELPER.with_name("review_checkout_slot.py")), *args],
        env=_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


@pytest.fixture(params=[3, 4, 5])
def watched_baseline(checkout: Path, review: Path, tmp_path: Path, request: pytest.FixtureRequest) -> Baseline:
    """Capture a detached review, including retained snapshots from the aggregate-only schema."""
    snap = Baseline(tmp_path / "integrity.json")
    assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
    if request.param < 5:
        body = json.loads(snap.file.read_text(encoding="utf-8"))
        if request.param == 3:
            del body["ignored_details"]
        del body["marks"]["common:info/refs"]
        body["schema"] = request.param
        snap.file.write_text(json.dumps(body), encoding="utf-8")
        snap.expect = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return snap


class TestVerify:
    """The verdict after contributor code has run."""

    def test_unchanged_metadata_passes(self, baseline: Baseline) -> None:
        """Nothing written between snapshot and verify exits 0 with an empty change list."""
        assert baseline.verify() == (0, [])

    @_POSIX_ONLY
    def test_empty_environment_lock_ctime_preserves_only_the_same_lock(self, checkout: Path, tmp_path: Path) -> None:
        """A ctime-only touch is benign, while an ordinary replacement with identical size, mode and mtime is drift."""
        lock = checkout / ".venv" / ".lock"
        lock.touch()
        original = lock.stat()
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        lock.chmod(original.st_mode)
        changed_time = lock.stat().st_ctime_ns != original.st_ctime_ns
        expected = (3, ["main-checkout:ignored"]) if changed_time and not _lock_metadata_available(lock) else (0, [])
        assert snap.verify() == expected
        replacement = lock.with_name("replacement")
        replacement.touch()
        replacement.chmod(original.st_mode)
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        replacement.replace(lock)
        assert snap.verify() == (3, ["main-checkout:ignored"])

    @pytest.mark.parametrize("mutation", ["content", "link", "mtime", "mode", "sibling"])
    @_POSIX_ONLY
    def test_material_environment_lock_changes_hold(self, checkout: Path, tmp_path: Path, mutation: str) -> None:
        """Lock normalization preserves content, hardlinks, material metadata and changes to nearby files."""
        lock = checkout / ".venv" / ".lock"
        lock.touch()
        st = lock.stat()
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        operations = {
            "content": lambda: lock.write_bytes(b"changed"),
            "link": lambda: os.link(lock, tmp_path / "external"),
            "mtime": lambda: os.utime(lock, ns=(st.st_atime_ns, st.st_mtime_ns - 1_000_000_000)),
            "mode": lambda: lock.chmod(st.st_mode ^ 0o100),
            "sibling": lambda: (lock.parent / "marker").chmod((lock.parent / "marker").stat().st_mode),
        }
        operations[mutation]()
        assert snap.verify() == (3, ["main-checkout:ignored"])

    @pytest.mark.skipif(
        not hasattr(os, "setxattr") and not Path("/usr/bin/xattr").is_file(),
        reason="extended file attribute writer unavailable",
    )
    def test_extended_environment_lock_metadata_holds(self, checkout: Path, tmp_path: Path) -> None:
        """Adding an extended attribute changes the empty lock fingerprint even when ordinary mode and size match."""
        lock = checkout / ".venv" / ".lock"
        lock.touch()
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        _set_lock_attribute(lock)
        assert snap.verify() == (3, ["main-checkout:ignored"])
        # An existing opaque attribute prevents independent identification of a later metadata-only change.
        assert snap.take(checkout).returncode == 0
        before = lock.stat()
        lock.chmod(before.st_mode)
        expected = (3, ["main-checkout:ignored"]) if lock.stat().st_ctime_ns != before.st_ctime_ns else (0, [])
        assert snap.verify() == expected

    @pytest.mark.integration
    @pytest.mark.installed_plugin
    def test_targeted_pytest_failure_preserves_remediation_integrity(self, checkout: Path) -> None:
        """A failed pytest run and a child import leave the checkout intact while retaining the failure cache."""
        run = checkout / ".reports" / "run with spaces"
        run.mkdir(parents=True)
        with (checkout / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write("__pycache__/\n.pytest_cache/\n")
        # Put the active pytest entry point inside the checkout, distinct from the launcher's working directory.
        runner = Path(".venv") / ("pytest.exe" if os.name == "nt" else "pytest")
        entrypoint = next(path for path in distribution("pytest").files or () if path.name == runner.name)
        shutil.copy2(entrypoint.locate(), checkout / runner)
        (checkout / "child_module.py").write_text("VALUE = 42\n", encoding="utf-8")
        (checkout / "test_sample.py").write_text(
            "import subprocess\n"
            "import sys\n\n"
            "def test_child_import():\n"
            "    subprocess.run([sys.executable, '-c', 'import child_module; assert child_module.VALUE == 42'],"
            " check=True)\n\n"
            "def test_failure():\n"
            "    assert False\n\n"
            "def test_excluded():\n"
            "    assert False\n",
            encoding="utf-8",
        )
        snap = Baseline(run / "integrity.json")
        assert snap.take(checkout).returncode == 0

        env = _ENV | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "-k 'not excluded'"}
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        launcher = [sys.executable, "-I", str(HELPER.with_name("run_targeted_pytest.py"))]
        pytest_cmd = [str(runner), "-q", "-p", "no:xdist", "test_sample.py"]
        result = subprocess.run(
            [*launcher, "--checkout", str(checkout), str(run), "--", *pytest_cmd],
            cwd=checkout.parent,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "1 failed, 1 passed, 1 deselected" in result.stdout
        lastfailed = run / "pytest-cache" / "v" / "cache" / "lastfailed"
        assert json.loads(lastfailed.read_text(encoding="utf-8")) == {"test_sample.py::test_failure": True}
        assert not (checkout / ".pytest_cache").exists()
        assert not (checkout / "__pycache__").exists()
        assert snap.verify() == (0, [])

        bytecode = checkout / "__pycache__" / "explicit.pyc"
        bytecode.parent.mkdir()
        bytecode.write_bytes(b"explicit write")
        assert snap.verify() == (3, ["main-checkout:ignored"])

    @pytest.mark.integration
    @pytest.mark.parametrize("use_cache", [pytest.param(True, id="explicit-cache"), pytest.param(False, id="no-cache")])
    def test_nested_pytest_can_isolate_its_import_path(self, checkout: Path, use_cache: bool) -> None:
        """A project startup hook can launch isolated pytest with run-local cache and raw coverage settings."""
        run = checkout / ".reports" / "run with spaces"
        run.mkdir(parents=True)
        (checkout / "conftest.py").write_text(
            "import os, subprocess, sys\n"
            "env = os.environ.copy()\n"
            "def pytest_sessionstart(session):\n"
            "    env.pop('PYTHONPATH', None)\n"
            "    subprocess.run([sys.executable, '-m', 'pytest', '--noconftest', '-q', 'nested.py'], env=env, check=True)\n",
            encoding="utf-8",
        )
        (checkout / "nested.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def test_isolated(pytestconfig):\n"
            "    assert not pytestconfig.pluginmanager.hasplugin('codex_rig_pytest_artifacts')\n"
            "    data = Path(os.environ['COVERAGE_FILE'])\n"
            "    assert data.name == '.coverage'\n"
            "    assert data.parent == Path(os.environ['EXPECTED_RUN'])\n"
            "    assert Path(pytestconfig.getini('cache_dir')) == Path(os.environ['EXPECTED_CACHE'])\n",
            encoding="utf-8",
        )
        snap = Baseline(run / "integrity.json")
        assert snap.take(checkout).returncode == 0
        cache = run / ("selected cache" if use_cache else "pytest-cache")
        options = ["-o", f"cache_dir={cache}"] if use_cache else ["-p", "no:cacheprovider"]
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(HELPER.with_name("run_targeted_pytest.py")),
                "--checkout",
                str(checkout),
                str(run),
                "--",
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--collect-only",
                *options,
                "nested.py",
            ],
            env=_ENV
            | {
                "EXPECTED_RUN": str(run),
                "EXPECTED_CACHE": str(cache),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTEST_ADDOPTS": "",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert snap.verify() == (0, [])

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("selection", "expected"),
        [
            pytest.param("--lf", ["previous"], id="last-failed"),
            pytest.param("--ff", ["previous", "first", "last"], id="failed-first"),
            pytest.param("--sw", ["previous", "last"], id="stepwise"),
        ],
    )
    def test_protected_pytest_resumes_project_selection(
        self, checkout: Path, selection: str, expected: list[str]
    ) -> None:
        """A fresh run inherits the project's selected failures and later uses its own updated history."""
        run = checkout / ".reports/run"
        run.mkdir(parents=True)
        (checkout / "pytest.ini").write_text("[pytest]\ncache_dir = history with spaces\n", encoding="utf-8")
        (checkout / "test_order.py").write_text(
            "import os\n"
            "def test_first(): print('RUN=first')\n"
            "def test_previous(): print('RUN=previous'); assert not os.environ.get('FAIL_PREVIOUS')\n"
            "def test_last(): print('RUN=last')\n",
            encoding="utf-8",
        )
        env = _ENV | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "", "PYTHONDONTWRITEBYTECODE": "1"}
        cmd = [sys.executable, "-m", "pytest", "-q", "-s"]
        seed = subprocess.run([*cmd, "--sw"], cwd=checkout, env=env | {"FAIL_PREVIOUS": "1"}, capture_output=True)
        assert seed.returncode == 2, seed.stdout
        original = checkout / "history with spaces/v/cache/lastfailed"
        original_bytes = original.read_bytes()
        launcher = [
            sys.executable,
            "-I",
            str(HELPER.with_name("run_targeted_pytest.py")),
            "--checkout",
            str(checkout),
            str(run),
            "--",
            *cmd,
        ]
        result = subprocess.run([*launcher, selection], cwd=checkout, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert [line.split("RUN=")[1] for line in result.stdout.splitlines() if "RUN=" in line] == expected
        again = subprocess.run(
            [*launcher, "--lf", "--lfnf=none"], cwd=checkout, env=env, capture_output=True, text=True
        )
        assert again.returncode == 5, again.stdout + again.stderr
        assert original.read_bytes() == original_bytes

    @pytest.mark.integration
    @pytest.mark.parametrize("targeted", [pytest.param(True, id="targeted"), pytest.param(False, id="default-gate")])
    @pytest.mark.parametrize("report", ["xml", "xml:.reports/run/custom.xml"])
    def test_pytest_coverage_preserves_remediation_integrity(self, checkout: Path, targeted: bool, report: str) -> None:
        """Configured worker coverage produces raw data and XML while preserving existing checkout data."""
        run = checkout / ".reports" / "run"
        run.mkdir(parents=True)
        with (checkout / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write("__pycache__/\n.pytest_cache/\n.coverage*\n")
        (checkout / ".coverage").write_bytes(b"previous data")
        (checkout / ".coveragerc").write_text("[json]\noutput=.reports/run/measured.json\n", encoding="utf-8")
        (checkout / "sample.py").write_text("value = 42\n", encoding="utf-8")
        (checkout / "test_sample.py").write_text(
            "import sample\ndef test_value():\n    assert sample.value == 42\n", encoding="utf-8"
        )
        (checkout / "pytest.ini").write_text(
            "[pytest]\naddopts = -p pytest_cov.plugin -p xdist.plugin -n 1 --cov=sample"
            f" --cov-report={report} --cov-report=json --cov-report=html --cov-fail-under=100\n",
            encoding="utf-8",
        )
        snap = Baseline(run / "integrity.json")
        assert snap.take(checkout).returncode == 0
        env = _ENV | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "", "COVERAGE_FILE": ".coverage"}
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        if targeted:
            arguments = [
                "-I",
                str(HELPER.with_name("run_targeted_pytest.py")),
                "--checkout",
                str(checkout),
                str(run),
                "--",
                sys.executable,
                "-m",
                "pytest",
                "-q",
            ]
            data = run / ".coverage"
        else:
            executable = (
                "& '" + sys.executable.replace("'", "''") + "'"
                if sys.platform == "win32"
                else shlex.quote(sys.executable)
            )
            arguments = [
                str(HELPER.with_name("run_gates.py")),
                "--out",
                str(run),
                "--tests",
                f"{executable} -m pytest -q",
            ]
            for gate in ("lint", "format", "types", "review"):
                arguments.extend([f"--skip-{gate}", "not applicable"])
            data = run / "checks" / ".coverage"
        result = subprocess.run([sys.executable, *arguments], cwd=checkout, env=env, capture_output=True, text=True)
        assert result.returncode == 0, (
            result.stdout + result.stderr if targeted else (run / "checks/tests.stdout.txt").read_text(encoding="utf-8")
        )
        assert data.read_bytes().startswith(b"SQLite format 3")
        xml = data.with_name("coverage.xml") if report == "xml" else run / "custom.xml"
        assert 'filename="sample.py"' in xml.read_text(encoding="utf-8")
        assert "sample.py" in json.loads((run / "measured.json").read_text(encoding="utf-8"))["files"]
        assert (data.parent / "htmlcov/index.html").is_file()
        assert (checkout / ".coverage").read_bytes() == b"previous data"
        assert snap.verify() == (0, [])

    def test_ignored_drift_identifies_paths_in_each_checkout(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """A shared report edit and new review coverage data identify their source checkout and operation."""
        sibling = checkout / ".reports" / "sibling" / "evidence.json"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("{}", encoding="utf-8")
        coverage = review / ".coverage"
        with (review / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write(".coverage\n")
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        sibling.write_text('{"changed":true}', encoding="utf-8")
        (checkout / ".venv" / "marker").unlink()
        coverage.write_bytes(b"data")
        result = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert result.returncode == 3
        assert json.loads(result.stdout) == {
            "ok": False,
            "changed": ["main-checkout:ignored", "review:ignored"],
            "ignored_changes": {
                "main-checkout:ignored": {"modified": [".reports/sibling/evidence.json"], "removed": [".venv/marker"]},
                "review:ignored": {"added": [".coverage"]},
            },
        }
        assert "review_checkout_slot.py hold" in result.stderr

    def test_large_ignored_store_reports_directory_and_bounds_snapshot(self, checkout: Path, tmp_path: Path) -> None:
        """A large store reports its directory while a small sibling still identifies the exact changed file."""
        store = checkout / ".venv"
        _populate(store, 1024)
        report = checkout / ".reports" / "state.json"
        report.parent.mkdir()
        report.write_text("{}", encoding="utf-8")
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        assert snap.file.stat().st_size < 32_768
        (store / "entry-1023").write_bytes(b"changed")
        report.unlink()
        result = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert result.returncode == 3
        assert json.loads(result.stdout)["ignored_changes"] == {
            "main-checkout:ignored": {"directories": [".venv"], "removed": [".reports/state.json"]}
        }

    def test_long_ignored_change_list_reports_omissions(self, checkout: Path, baseline: Baseline) -> None:
        """The change count stays visible if the number of exact path changes exceeds the output limit."""
        _populate(checkout / ".venv", 70)
        result = _helper("verify", "--snapshot", str(baseline.file), "--expect", baseline.expect)
        assert result.returncode == 3
        expected = sorted(f".venv/entry-{number}" for number in range(70))
        assert json.loads(result.stdout)["ignored_changes"] == {
            "main-checkout:ignored": {"added": expected[:64], "omitted": 6}
        }

    def test_sibling_review_run_is_identified_in_large_report_archive(self, checkout: Path, tmp_path: Path) -> None:
        """Report archives can outgrow per-file detail while individual review runs remain distinguishable."""
        archive = checkout / ".reports" / "archive"
        archive.mkdir(parents=True)
        _populate(archive, 170)
        sibling = checkout / ".reports" / "codex" / "code-review" / "pr-27" / "run-002" / "gate.json"
        sibling.parent.mkdir(parents=True)
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        sibling.write_text("{}", encoding="utf-8")
        result = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert result.returncode == 3
        assert json.loads(result.stdout)["ignored_changes"] == {
            "main-checkout:ignored": {"added": [sibling.relative_to(checkout).as_posix()]}
        }

    def test_overflow_preserves_captured_groups_without_reporting_false_removals(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """Inserting an earlier run can shift another into overflow while an unchanged file remains on disk."""
        family = checkout / ".reports" / "codex" / "code-review" / "pr-1"
        _populate_runs(family, 130)
        captured = family / "run-001" / "gate.json"
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        captured.write_text('{"changed":true}', encoding="utf-8")
        earlier = family / "run-000"
        earlier.mkdir()
        (earlier / "new.json").touch()
        result = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert result.returncode == 3
        assert json.loads(result.stdout)["ignored_changes"] == {
            "main-checkout:ignored": {
                "directories": [".reports/codex/code-review"],
                "modified": [captured.relative_to(checkout).as_posix()],
            }
        }

    def test_historical_baseline_still_detects_ignored_writes(self, checkout: Path, baseline: Baseline) -> None:
        """The aggregate-only schema retains its failing interval when a fully inspected new interval opens."""
        body = json.loads(baseline.file.read_text(encoding="utf-8"))
        body.pop("ignored_details", None)
        body["schema"] = 3
        del body["marks"]["common:info/refs"]
        marker = checkout / ".venv" / "marker"
        st = marker.lstat()
        record = f"{marker}:{st.st_size}:{st.st_mode:o}:{st.st_ino}:{st.st_ctime_ns}:"
        body["marks"]["main-checkout:ignored"] = hashlib.sha256(record.encode()).hexdigest()
        baseline.file.write_text(json.dumps(body), encoding="utf-8")
        baseline.expect = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert baseline.verify() == (0, [])
        (checkout / ".venv" / "added").touch()
        failure = _helper("verify", "--snapshot", str(baseline.file), "--expect", baseline.expect)
        assert failure.returncode == 3
        observed = json.loads(failure.stdout)
        assert observed["changed"] == ["main-checkout:ignored"]
        assert "ignored_changes" not in observed
        fresh = Baseline(baseline.file.with_name("integrity-restart.json"))
        started = baseline.restart(observed["observed_sha256"], fresh.file)
        assert started.returncode == 0, started.stderr
        fresh.expect = json.loads(started.stdout)["sha256"]
        assert fresh.verify() == (0, [])
        assert baseline.verify() == (3, ["main-checkout:ignored"])

    @pytest.mark.parametrize(
        ("relative", "label"),
        [
            pytest.param(".git/config", "common:config", id="fsmonitor-in-config"),
            pytest.param(".git/hooks/post-checkout", "common:hooks/", id="planted-hook"),
            pytest.param(".git/codex-git-approval.json", "common:codex-git-approval.json", id="forged-grant"),
            pytest.param(".git/info/exclude", "common:info/", id="info-exclude"),
            pytest.param(".git/modules/sub/hooks/post-checkout", "common:modules", id="submodule-hook"),
        ],
    )
    def test_planted_git_state_fails_closed(
        self, checkout: Path, baseline: Baseline, relative: str, label: str
    ) -> None:
        """A write to the Git directory that a later host command would act on exits 3 and names the path.

        These are the paths the permission profiles leave writable to sandboxed code, so each is a way for contributor
        code to outlive the run.
        """
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write("[core]\n\tfsmonitor = /tmp/x\n")
        code, changed = baseline.verify()
        assert (code, label in changed) == (3, True)

    def test_moved_local_ref_fails_closed_but_a_fetched_remote_ref_does_not(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """A repointed branch is a change; a ``git fetch`` moving a remote-tracking ref is the user, not the run."""
        _git(checkout, "branch", "other")
        _git(checkout, "update-ref", "refs/remotes/origin/x", "HEAD")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        moved = _git(checkout, "commit-tree", "HEAD^{tree}", "-m", "moved")
        _git(checkout, "update-ref", "refs/remotes/origin/x", moved)
        fetched = snap.verify()
        _git(checkout, "update-ref", "refs/heads/other", moved)
        assert (fetched, snap.verify()) == ((0, []), (3, ["common:refs"]))

    def test_garbage_commondir_makes_git_fail_and_verify_never_passes(self, checkout: Path, baseline: Baseline) -> None:
        """A planted ``commondir`` that Git cannot follow stops the check with a failure code, which the contract treats
        as a stop."""
        (checkout / ".git" / "commondir").write_text("[core]\n", encoding="utf-8")
        code, _changed = baseline.verify()
        assert code in {1, 3}

    @_POSIX_ONLY
    def test_hook_mode_change_alone_fails_closed(self, checkout: Path, tmp_path: Path) -> None:
        """Making an existing hook executable changes no bytes but arms it, so file modes are fingerprinted."""
        hook = checkout / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o644)
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        hook.chmod(0o755)
        assert snap.verify() == (3, ["common:hooks/"])

    def test_include_target_edit_fails_closed_for_absolute_and_relative_paths(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """A file the Git configuration includes is part of the configuration, resolved against the file naming it."""
        absolute = tmp_path / "extra.gitconfig"
        absolute.write_text("[user]\n\tname = a\n", encoding="utf-8")
        relative = checkout / ".git" / "local.gitconfig"
        relative.write_text("[user]\n\temail = a@example.com\n", encoding="utf-8")
        _git(checkout, "config", "--add", "include.path", str(absolute))
        _git(checkout, "config", "--add", "include.path", "local.gitconfig")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        absolute.write_text("[core]\n\tfsmonitor = /tmp/x\n", encoding="utf-8")
        relative.write_text("[core]\n\tfsmonitor = /tmp/y\n", encoding="utf-8")
        assert snap.verify() == (3, sorted(["config:repo:include:local.gitconfig", f"config:repo:include:{absolute}"]))

    def test_edit_of_a_hooks_path_target_fails_closed(self, checkout: Path, tmp_path: Path) -> None:
        """A husky-style hooks directory named by ``core.hooksPath`` runs on the next commit, so its bytes count."""
        hooks = checkout / ".husky" / "_"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        _git(checkout, "config", "core.hooksPath", ".husky/_")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        assert snap.verify() == (3, ["config:repo:hooksPath"])

    def test_baseline_file_rewritten_by_contributor_code_is_rejected(self, checkout: Path, baseline: Baseline) -> None:
        """Re-recording a baseline over the changed state is caught: the kept digest no longer matches the file."""
        (checkout / ".git" / "hooks").mkdir(exist_ok=True)
        (checkout / ".git" / "hooks" / "post-checkout").write_text("evil\n", encoding="utf-8")
        forged = _helper("snapshot", "--repo", str(checkout), "--out", str(baseline.file))
        assert (forged.returncode, baseline.verify()) == (0, (3, ["snapshot-file"]))

    def test_unreadable_snapshot_is_a_failure_not_a_pass(self, tmp_path: Path) -> None:
        """A missing snapshot never reads as unchanged: verify exits 1 and says why."""
        result = _helper("verify", "--snapshot", str(tmp_path / "missing.json"), "--expect", "0" * 64)
        assert (result.returncode, "snapshot unreadable" in result.stderr) == (1, True)
        assert "review_checkout_slot.py hold" in result.stderr

    @_POSIX_ONLY
    def test_unreadable_fingerprinted_file_fails_closed(self, checkout: Path, baseline: Baseline) -> None:
        """A fingerprinted file made unreadable by contributor code is a change, never an uncaught traceback."""
        hook = checkout / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("x\n", encoding="utf-8")
        hook.chmod(0)
        code, changed = baseline.verify()
        hook.chmod(0o644)
        assert (code, "common:hooks/" in changed) == (3, True)

    @_POSIX_ONLY
    def test_a_file_already_unreadable_at_snapshot_stops_before_any_execution(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """A path that cannot be fingerprinted would stop every later verify, so the snapshot itself fails and records
        nothing."""
        hook = checkout / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("x\n", encoding="utf-8")
        hook.chmod(0)
        snap = Baseline(tmp_path / "integrity.json")
        result = snap.take(checkout)
        hook.chmod(0o644)
        assert (result.returncode, snap.file.exists()) == (1, False)


class TestUserCheckout:
    """The user's own checkout beside a review worktree."""

    def test_edit_to_a_tracked_file_is_caught_from_a_worktree_run(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """Contributor code in the review worktree that edits the user's own tree fails verification."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (checkout / "a.txt").write_text("tampered\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:dirty-content", "main-checkout:status"])

    def test_new_file_in_an_ignored_interpreter_environment_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """A planted file in the ignored virtual environment runs at every interpreter start, so it is a change."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (checkout / ".venv" / "evil.pth").write_text("import os\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:ignored"])

    @_POSIX_ONLY
    @pytest.mark.parametrize(
        "name", [pytest.param("site.pth", id="executable-surface"), pytest.param("data.bin", id="other-ignored-file")]
    )
    def test_same_size_write_with_the_modification_time_restored_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path, name: str
    ) -> None:
        """Resetting a file's modification time hides nothing: bytes are hashed for surfaces, change time for the
        rest."""
        target = checkout / ".venv" / name
        target.write_text("import os; x = 1\n", encoding="utf-8")
        stamp = target.stat().st_mtime_ns
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        target.write_text("import os; x = 2\n", encoding="utf-8")
        os.utime(target, ns=(stamp, stamp))
        assert snap.verify() == (3, ["main-checkout:ignored"])

    def test_content_edit_of_an_already_untracked_file_is_caught(self, checkout: Path, tmp_path: Path) -> None:
        """Rewriting a file the status already lists as untracked leaves the status line equal, so bytes are hashed."""
        (checkout / "scratch.py").write_text("one\n", encoding="utf-8")
        review = tmp_path / "review"
        _git(checkout, "worktree", "add", "-q", "--detach", str(review))
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (checkout / "scratch.py").write_text("two\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:dirty-content"])

    def test_relative_main_checkout_is_resolved_at_snapshot_time(self, checkout: Path, tmp_path: Path) -> None:
        """``--main-checkout .`` verifies from any working directory instead of raising a false alarm."""
        snap = Baseline(tmp_path / "integrity.json")
        result = _helper(
            "snapshot", "--repo", str(checkout), "--main-checkout", ".", "--out", str(snap.file), cwd=checkout
        )
        snap.expect = json.loads(result.stdout)["sha256"]
        assert snap.verify() == (0, [])

    def test_workflow_writes_inside_an_ignored_report_tree_are_not_changes(self, checkout: Path) -> None:
        """The documented layout puts the run directory and review worktree under the checkout's ignored report tree.

        Run artifacts, the baseline itself and the test runner's caches are written there on purpose, so none of them
        may trip the user-checkout check, while the worktree keeps its own coverage through ``--watch-surface``.
        """
        reports = checkout / ".reports" / "codex" / "code-review" / "pr-1"
        run, worktree = reports / "run-001", reports / "run-001-review-worktree"
        run.mkdir(parents=True)
        _git(checkout, "worktree", "add", "-q", "--detach", str(worktree))
        snap = Baseline(run / "integrity.json")
        taken = snap.take(worktree, "--main-checkout", str(checkout), "--watch-surface")
        (run / "review-notes.md").write_text("notes\n", encoding="utf-8")
        (worktree / ".pytest_cache").mkdir()
        (worktree / ".pytest_cache" / "v").write_text("1\n", encoding="utf-8")
        (worktree / ".mypy_cache").mkdir()
        (worktree / ".mypy_cache" / "x.json").write_text("{}\n", encoding="utf-8")
        assert (taken.returncode, snap.verify()) == (0, (0, []))


class TestCheckoutUnderReview:
    """What a run may leave in the checkout under review, which the agent never edits during review."""

    def test_large_new_ignored_group_releases_peers(self, checkout: Path, review: Path, tmp_path: Path) -> None:
        """A new private report group can exceed the detail budget when an unrelated old group remains unchanged."""
        unrelated = review / ".venv"
        unrelated.mkdir()
        _populate(unrelated, 130)
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        reports = review / ".reports"
        reports.mkdir()
        _populate(reports, 129)
        failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert failure.returncode == 3
        assert json.loads(failure.stdout)["ignored_changes"] == {"review:ignored": {"directories": [".reports"]}}
        assert "review_checkout_slot.py release" in failure.stderr
        os.link(reports / "entry-128", tmp_path / "external")
        linked = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert linked.returncode == 3
        assert "review_checkout_slot.py hold" in linked.stderr

    @_POSIX_ONLY
    @pytest.mark.parametrize(
        ("relative", "label"),
        [
            pytest.param("conftest.py", "surface:conftest.py", id="autoload-link"),
            pytest.param(".reports/bytecode.pyc", "review:ignored", id="ignored-bytecode-link"),
            pytest.param("untracked-link", "review:untracked", id="untracked-link"),
        ],
    )
    def test_external_link_target_drift_holds_the_slot(
        self, checkout: Path, review: Path, tmp_path: Path, relative: str, label: str
    ) -> None:
        """An unchanged review link can report a change to a file outside the isolated checkout."""
        target = tmp_path / "external"
        target.write_bytes(b"before")
        link = review / relative
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(target)
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        target.write_bytes(b"after")
        failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert failure.returncode == 3
        observed = json.loads(failure.stdout)
        assert label in observed["changed"]
        assert "review_checkout_slot.py hold" in failure.stderr
        assert ("observed_sha256" in observed) == (label == "review:ignored")

    @_POSIX_ONLY
    def test_linked_parent_of_tracked_surface_holds_the_slot(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """A tracked path can reach an external file through an ancestor even when its final component is regular."""
        directory = review / "package"
        directory.mkdir()
        (directory / "conftest.py").write_bytes(b"before")
        _git(review, "add", "package")
        _git(review, "commit", "-q", "-m", "track nested startup file")
        external = tmp_path / "external"
        directory.rename(external)
        directory.symlink_to(external, target_is_directory=True)
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        (external / "conftest.py").write_bytes(b"after")
        failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert failure.returncode == 3
        assert "surface:package/conftest.py" in json.loads(failure.stdout)["changed"]
        assert "review_checkout_slot.py hold" in failure.stderr

    @_POSIX_ONLY
    def test_truncated_private_changes_release_with_an_unchanged_external_link(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """All changed files can be private even when an unchanged sibling link points outside the checkout."""
        reports = review / ".reports"
        reports.mkdir()
        target = tmp_path / "external"
        target.touch()
        (reports / "external.pyc").symlink_to(target)
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        _populate(reports, 70)
        failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert failure.returncode == 3
        assert json.loads(failure.stdout)["ignored_changes"]["review:ignored"]["omitted"] == 6
        assert "review_checkout_slot.py release" in failure.stderr
        target.write_bytes(b"outside change")
        mixed = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert mixed.returncode == 3
        assert json.loads(mixed.stdout)["ignored_changes"]["review:ignored"]["omitted"] == 7
        assert "review_checkout_slot.py hold" in mixed.stderr

    @_POSIX_ONLY
    def test_opaque_ignored_group_with_removed_link_keeps_the_slot(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """The current contents of a changed directory cannot prove where its removed entries pointed."""
        reports = review / ".reports"
        reports.mkdir()
        _populate(reports, 130)
        target = tmp_path / "external"
        target.touch()
        link = reports / "cache.pyc"
        link.symlink_to(target)
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        link.unlink()
        (reports / "ordinary.pyc").touch()
        failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert failure.returncode == 3
        assert json.loads(failure.stdout)["ignored_changes"] == {"review:ignored": {"directories": [".reports"]}}
        assert "review_checkout_slot.py hold" in failure.stderr

    @_POSIX_ONLY
    def test_unreadable_auto_load_symlink_keeps_the_slot(self, checkout: Path, review: Path, tmp_path: Path) -> None:
        """An unreadable file behind a tracked auto-load symlink gives no basis for releasing the repository slot."""
        target = tmp_path / "startup.py"
        target.write_bytes(b"")
        (review / "conftest.py").symlink_to(target)
        _git(review, "add", "conftest.py")
        _git(review, "commit", "-q", "-m", "track startup symlink")
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        target.chmod(0)
        try:
            failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
            fresh = Baseline(tmp_path / "new.json")
            refused = fresh.take(review, "--main-checkout", str(checkout), "--watch-surface")
        finally:
            target.chmod(0o600)
        assert failure.returncode == 3
        assert json.loads(failure.stdout)["changed"] == ["surface:conftest.py"]
        assert "review_checkout_slot.py hold" in failure.stderr
        assert refused.returncode == 1
        assert "unreadable at snapshot" in refused.stderr
        assert not fresh.file.exists()

    def test_edit_to_a_tracked_non_surface_file_is_caught(self, review: Path, tmp_path: Path) -> None:
        """A rewritten test or helper module would run unreviewed under a later escalated command."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        (review / "a.txt").write_text("rewritten\n", encoding="utf-8")
        assert snap.verify() == (3, ["review:diff"])

    def test_new_untracked_file_is_caught(self, review: Path, tmp_path: Path) -> None:
        """A file the run wrote into the worktree outside caches is a change."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        (review / "helper.py").write_text("import os\n", encoding="utf-8")
        assert snap.verify() == (3, ["review:untracked"])

    def test_planted_auto_load_file_is_caught_by_name(self, review: Path, tmp_path: Path) -> None:
        """A conftest written into the checkout under review is named, so the report says which surface changed."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        (review / "conftest.py").write_text("import os\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "surface:conftest.py" in changed) == (3, True)


class TestLinkedWorktree:
    """A review worktree shares the common Git directory and has admin files of its own."""

    def test_worktree_commondir_redirect_is_caught(self, checkout: Path, review: Path, tmp_path: Path) -> None:
        """Repointing the worktree's ``commondir`` at a copy cannot hide an edit of the real configuration."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review)
        admin = next((checkout / ".git" / "worktrees").iterdir())
        copy = tmp_path / "copy.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(checkout), str(copy)], env=_ENV, check=True)
        (admin / "commondir").write_text(f"{copy}\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "git-dir-redirect:repo" in changed) == (3, True)

    def test_main_checkout_commondir_redirect_is_caught(self, checkout: Path, review: Path, tmp_path: Path) -> None:
        """A ``commondir`` file planted in the user's own ``.git`` redirects every later Git command there."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        copy = tmp_path / "copy.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(checkout), str(copy)], env=_ENV, check=True)
        (checkout / ".git" / "commondir").write_text(f"{copy}\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "git-dir-redirect:main" in changed) == (3, True)


@pytest.mark.integration
@pytest.mark.installed_plugin
class TestIndependentReview:
    """A separately registered review can release only its own checkout while both repositories stay guarded."""

    def test_clean_private_execution_releases_while_caller_is_held(
        self, checkout: Path, independent_review: Path, tmp_path: Path
    ) -> None:
        """An actual read-only project check can finish and release a separate review without clearing the peer hold."""
        peer = _slot("acquire", "--checkout", str(checkout), "--owner", "peer")
        _slot("hold", "--slot", peer["slot"], "--owner", "peer")
        own = _slot("acquire", "--checkout", str(independent_review), "--owner", "review")
        program = independent_review / "check_source.py"
        program.write_text(
            'from pathlib import Path\nassert Path("a.txt").read_text().strip() == "a"\n', encoding="utf-8"
        )
        _git(independent_review, "add", "check_source.py")
        _git(independent_review, "commit", "-q", "-m", "source check")
        snap = Baseline(tmp_path / "evidence" / "integrity.json")
        snap.file.parent.mkdir()
        assert snap.take(independent_review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        subprocess.run([sys.executable, "-I", "-B", str(program)], cwd=independent_review, env=_ENV, check=True)
        assert snap.verify() == (0, [])
        assert _slot("release", "--slot", own["slot"], "--owner", "review")["status"] == "released"
        assert _slot("status", "--slot", peer["slot"]) == dict(peer, status="inspection-required")

    def test_private_report_releases_only_the_independent_claim(
        self, checkout: Path, independent_review: Path, tmp_path: Path
    ) -> None:
        """A held caller claim stays held as the independent review checks its last writer and releases its own."""
        peer = _slot("acquire", "--checkout", str(checkout), "--owner", "peer")
        assert _slot("hold", "--slot", peer["slot"], "--owner", "peer")["status"] == "inspection-required"
        own = _slot("acquire", "--checkout", str(independent_review), "--owner", "review")
        assert own["slot"] != peer["slot"]
        snap = Baseline(tmp_path / "evidence" / "integrity.json")
        snap.file.parent.mkdir()
        assert snap.take(independent_review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        assert snap.verify() == (0, [])
        reports = independent_review / ".reports"
        reports.mkdir()
        _populate(reports, 129)
        failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert failure.returncode == 3
        state = json.loads(failure.stdout)
        assert state["changed"] == ["review:ignored"]
        assert "review_checkout_slot.py release" in failure.stderr
        assert Path(state["git_common_dir"]) == Path(own["slot"]).parent
        assert _slot("release", "--slot", own["slot"], "--owner", "review")["status"] == "released"
        assert _slot("status", "--slot", peer["slot"]) == dict(peer, status="inspection-required")

    @pytest.mark.parametrize(
        ("side", "surface", "label"),
        [
            pytest.param("review", "refs", "common:refs", id="private-ref"),
            pytest.param("caller", "refs", "main:common:refs", id="caller-ref"),
            pytest.param("review", "hooks", "common:hooks/", id="private-hook"),
            pytest.param("caller", "hooks", "main:common:hooks/", id="caller-hook"),
            pytest.param("caller", "tracked", "main-checkout:dirty-content", id="caller-tracked-file"),
            pytest.param("caller", "ignored", "main-checkout:ignored", id="caller-ignored-peer-report"),
        ],
    )
    def test_mixed_private_and_protected_drift_holds(
        self, checkout: Path, independent_review: Path, tmp_path: Path, side: str, surface: str, label: str
    ) -> None:
        """Private files cannot account for a simultaneous change to either repository or the real caller's files."""
        snap = Baseline(tmp_path / "evidence" / "integrity.json")
        snap.file.parent.mkdir()
        assert snap.take(independent_review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        (independent_review / "fresh.txt").touch()
        target = {"review": independent_review, "caller": checkout}[side]
        common = Path(_git(target, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        report = checkout / ".reports"
        report.mkdir()
        mutate = {
            "refs": lambda: _git(target, "branch", "unexpected"),
            "hooks": lambda: (common / "hooks" / "pre-commit").write_text("changed", encoding="utf-8"),
            "tracked": lambda: (target / "a.txt").write_text("changed", encoding="utf-8"),
            "ignored": lambda: (report / "peer-report").touch(),
        }
        mutate[surface]()
        failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert failure.returncode == 3
        state = json.loads(failure.stdout)
        assert {"review:untracked", label} <= set(state["changed"])
        assert "observed_sha256" not in state
        assert "review_checkout_slot.py hold" in failure.stderr

    def test_mismatched_private_worktree_registration_holds(
        self, checkout: Path, independent_review: Path, tmp_path: Path
    ) -> None:
        """A detached HEAD under worktrees is insufficient when its registration points back to a different checkout."""
        admin = Path(_git(independent_review, "rev-parse", "--absolute-git-dir"))
        (admin / "gitdir").write_bytes(os.fsencode(checkout / ".git") + b"\n")
        snap = Baseline(tmp_path / "evidence" / "integrity.json")
        snap.file.parent.mkdir()
        assert snap.take(independent_review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        (independent_review / "fresh.txt").touch()
        failure = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert failure.returncode == 3
        assert json.loads(failure.stdout)["changed"] == ["review:untracked"]
        assert "review_checkout_slot.py hold" in failure.stderr


class TestIndexFlagsAndRefs:
    """Git's own view of a checkout is steerable by contributor-writable state, so it is not trusted alone."""

    def test_assume_unchanged_edit_in_the_user_checkout_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """An index flag hides an edit from status and diff, so flagged files are fingerprinted by their own bytes."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        _git(checkout, "update-index", "--assume-unchanged", "a.txt")
        (checkout / "a.txt").write_text("tampered\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "main-checkout:index-flags" in changed, _git(checkout, "status", "--porcelain")) == (3, True, "")

    def test_skip_worktree_edit_in_the_review_worktree_is_caught(self, review: Path, tmp_path: Path) -> None:
        """A skip-worktree flag makes ``git diff HEAD`` empty, which must not make a rewritten test look unchanged."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        _git(review, "update-index", "--skip-worktree", "a.txt")
        (review / "a.txt").write_text("rewritten\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "review:index-flags" in changed) == (3, True)

    def test_replace_ref_is_a_changed_ref(self, checkout: Path, tmp_path: Path) -> None:
        """A ``refs/replace`` entry makes later Git reads return other bytes for a commit, so every namespace counts."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        other = _git(checkout, "commit-tree", "HEAD^{tree}", "-m", "planted")
        _git(checkout, "replace", "HEAD", other)
        assert snap.verify() == (3, ["common:refs"])

    @pytest.mark.parametrize("maintenance", ["gc", "update-server-info"])
    def test_git_maintenance_can_publish_verified_refs(self, checkout: Path, tmp_path: Path, maintenance: str) -> None:
        """Git-generated advertisements include all namespaces and fully peeled nested tags, without hiding edits."""
        _git(checkout, "tag", "-a", "inner", "-m", "inner")
        _git(checkout, "tag", "-a", "outer", "inner", "-m", "outer")
        _git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        _git(checkout, maintenance)
        assert snap.verify() == (0, [])
        advertised = checkout / ".git" / "info" / "refs"
        original = advertised.read_bytes()
        advertised.write_bytes(original.replace(b"refs/tags/outer^{}", b"refs/tags/forged^{}"))
        assert snap.verify() == (3, ["common:info/refs"])
        advertised.write_bytes(original)
        _git(checkout, "tag", "unexpected")
        _git(checkout, "update-server-info")
        assert snap.verify() == (3, ["common:refs"])

    @pytest.mark.parametrize("already_stale", [False, True])
    def test_remote_ref_refresh_preserves_advertisement(
        self, checkout: Path, tmp_path: Path, already_stale: bool
    ) -> None:
        """A remote ref can move before or after the snapshot while the advertisement waits for maintenance."""
        _git(checkout, "update-server-info")
        if already_stale:
            _git(checkout, "update-ref", "refs/remotes/upstream/main", "HEAD")
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        if not already_stale:
            _git(checkout, "update-ref", "refs/remotes/upstream/main", "HEAD")
        assert snap.verify() == (0, [])
        _git(checkout, "update-server-info")
        assert snap.verify() == (0, [])
        (checkout / ".git" / "info" / "refs").unlink()
        assert snap.verify() == (3, ["common:info/refs"])

    def test_packing_refs_is_not_a_change(self, checkout: Path, tmp_path: Path) -> None:
        """``git pack-refs`` moves ref values into ``packed-refs`` without changing any, which is storage, not state."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        _git(checkout, "pack-refs", "--all")
        assert snap.verify() == (0, [])

    @_POSIX_ONLY
    def test_editing_the_target_of_a_hook_symlink_is_caught(self, checkout: Path, tmp_path: Path) -> None:
        """A hook that is a symlink runs its target, so the target's bytes are part of the hook."""
        target = checkout / ".git" / "impl" / "pre-commit"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hooks = checkout / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "pre-commit").symlink_to(target)
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        target.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        assert snap.verify() == (3, ["common:hooks/"])


class TestSubmodules:
    """A submodule's Git directory is read by the user's own ``git status`` in the superproject."""

    def test_submodule_commondir_redirect_is_caught(self, checkout: Path, tmp_path: Path) -> None:
        """Pointing a submodule Git directory at another one would make Git read a planted configuration."""
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git(upstream, "init", "-q", "-b", "main")
        (upstream / "u.txt").write_text("u\n", encoding="utf-8")
        _git(upstream, "add", "u.txt")
        _git(upstream, "commit", "-q", "-m", "u")
        _git(checkout, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "sub")
        _git(checkout, "commit", "-q", "-m", "add submodule")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        (checkout / ".git" / "modules" / "sub" / "commondir").write_text(f"{tmp_path}\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "common:modules" in changed) == (3, True)


class TestNestedRepositoriesAndCaches:
    """What Git lists as one entry is walked, and tool caches are not mistaken for planted code."""

    def test_edit_inside_a_nested_repository_in_an_ignored_tree_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """An editable install cloned from VCS lists as a single directory entry, so its files are walked."""
        nested = checkout / ".venv" / "src" / "pkg"
        nested.mkdir(parents=True)
        _git(nested, "init", "-q", "-b", "main")
        (nested / "mod.py").write_text("X = 1\n", encoding="utf-8")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (nested / "mod.py").write_text("X = 2\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:ignored"])

    def test_edit_in_another_linked_worktree_inside_the_checkout_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """The user's own worktree nested in the checkout persists a planted edit into their next run of that branch."""
        other = checkout / "wt2"
        _git(checkout, "worktree", "add", "-q", "--detach", str(other))
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (other / "a.txt").write_text("planted\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "main-checkout:dirty-content" in changed) == (3, True)

    def test_bytecode_written_into_the_review_worktree_is_caught(self, review: Path, tmp_path: Path) -> None:
        """Python loads a matching ``.pyc`` instead of the scanned source, so a new bytecode file is a change."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        cache = review / ".venv" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "m.cpython-312.pyc").write_bytes(b"\0")
        code, changed = snap.verify()
        assert (code, "review:ignored" in changed) == (3, True)

    def test_tool_and_ide_caches_in_the_user_checkout_are_not_changes(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """A language server or Finder writing caches during a long review must not force a restart."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (checkout / ".DS_Store").write_bytes(b"\0")
        (checkout / ".mypy_cache").mkdir()
        (checkout / ".mypy_cache" / "x.json").write_text("{}\n", encoding="utf-8")
        (checkout / ".venv" / ".ruff_cache").mkdir()
        (checkout / ".venv" / ".ruff_cache" / "y").write_text("y\n", encoding="utf-8")
        assert snap.verify() == (0, [])

    def test_unignored_run_directory_is_not_a_status_change(self, checkout: Path, tmp_path: Path) -> None:
        """With every untracked file listed, the baseline written beside the run must not trip the first verify."""
        (checkout / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        _git(checkout, "commit", "-q", "-am", "stop ignoring reports")
        _git(checkout, "config", "status.showUntrackedFiles", "all")
        run = checkout / ".reports" / "run-001"
        run.mkdir(parents=True)
        worktree = checkout / ".reports" / "run-001-review-worktree"
        _git(checkout, "worktree", "add", "-q", "--detach", str(worktree))
        snap = Baseline(run / "integrity.json")
        snap.take(worktree, "--main-checkout", str(checkout), "--watch-surface")
        (run / "notes.md").write_text("notes\n", encoding="utf-8")
        assert snap.verify() == (0, [])


class TestRemediationInTheUserCheckout:
    """Remediation runs contributor code in the user's own checkout, where the agent edits tracked files on purpose."""

    def test_new_file_in_the_ignored_environment_is_caught_without_a_main_checkout(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """The interpreter environment beside the sources gets the same ignored-file coverage as in review."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        (checkout / ".venv" / "evil.pth").write_text("import os\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:ignored"])

    def test_the_agents_own_tracked_edit_is_not_a_change(self, checkout: Path, tmp_path: Path) -> None:
        """Remediation edits the checkout on purpose, so tracked content is not fingerprinted there."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        (checkout / "a.txt").write_text("fixed\n", encoding="utf-8")
        assert snap.verify() == (0, [])


@dataclass
class _RefRestart:
    """A real retained review claim and the parent's independently known branch endpoints."""

    checkout: Path
    baseline: Baseline
    slot: str
    old: str
    new: str

    @property
    def out(self) -> Path:
        """Return the fresh sibling evidence path."""
        return self.baseline.file.with_name("successor.json")

    def move(self, old: str, new: str) -> None:
        """Capture the prior checkout and move only its local target branch."""
        assert self.baseline.take(self.checkout).returncode == 0
        _git(self.checkout, "update-ref", "-m", "parent fast-forward", "refs/heads/main", new, old)

    def restart(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        """Pass the parent's evidence to the public recovery entry point."""
        options = {
            "--snapshot": str(self.baseline.file),
            "--expect": self.baseline.expect,
            "--branch": "main",
            "--old-oid": self.old,
            "--new-oid": self.new,
            "--slot": self.slot,
            "--owner": "review",
            "--out": str(self.out),
        }
        options.update(overrides)
        return _helper("restart-ref-fast-forward", *(arg for pair in options.items() for arg in pair))

    def assert_refused(self, **overrides: str) -> None:
        """Require unsafe restart to preserve the previous hold and leave no successor."""
        _slot("hold", "--slot", self.slot, "--owner", "review")
        done = self.restart(**overrides)
        assert done.returncode in (2, 3), done.stderr
        assert done.stderr.startswith("contributor-run-integrity:"), done.stderr
        assert not self.out.exists()
        assert _slot("status", "--slot", self.slot) == {
            "status": "inspection-required",
            "slot": self.slot,
            "owner": "review",
        }


@pytest.fixture
def ref_move(checkout: Path, tmp_path: Path) -> _RefRestart:
    """Leave main available to advance while a review owns the shared checkout on its topic branch."""
    _git(checkout, "commit", "--allow-empty", "-qm", "target baseline")
    _git(checkout, "switch", "-qc", "topic")
    old = _git(checkout, "rev-parse", "HEAD")
    new = _git(checkout, "commit-tree", "HEAD^{tree}", "-p", old, "-m", "published target")
    slot = _slot("acquire", "--checkout", str(checkout), "--owner", "review")
    return _RefRestart(checkout, Baseline(tmp_path / "integrity.json"), slot["slot"], old, new)


@pytest.mark.integration
@pytest.mark.installed_plugin
class TestRefFastForwardRestart:
    """Only one independently authorized local branch move can start a fresh exclusive review interval."""

    @pytest.mark.parametrize(
        "claim_action",
        [pytest.param(("status",), id="active"), pytest.param(("hold", "--owner", "review"), id="held")],
    )
    def test_attested_move_keeps_claim_and_restarts_verification(
        self, ref_move: _RefRestart, claim_action: tuple[str, ...]
    ) -> None:
        """The restarted interval verifies, the original still fails, and the same owner retains the exclusive slot."""
        ref_move.move(ref_move.old, ref_move.new)
        original = ref_move.baseline.file.read_bytes()
        assert ref_move.baseline.verify() == (3, ["common:refs"])
        _slot(*claim_action, "--slot", ref_move.slot)
        done = ref_move.restart()
        assert done.returncode == 0, done.stderr
        fresh = Baseline(ref_move.out)
        fresh.expect = json.loads(done.stdout)["sha256"]
        assert fresh.expect != ref_move.baseline.expect
        assert fresh.verify() == (0, [])
        assert ref_move.baseline.file.read_bytes() == original
        assert ref_move.baseline.verify() == (3, ["common:refs"])
        assert _slot("status", "--slot", ref_move.slot) == {"owner": "review", "slot": ref_move.slot, "status": "busy"}
        _git(ref_move.checkout, "tag", "later")
        assert fresh.verify() == (3, ["common:refs"])

    def test_fetch_while_formatting_in_linked_worktree(self, ref_move: _RefRestart, tmp_path: Path) -> None:
        """A real fetch can advance the unused base without losing the interrupted remediation edit."""
        caller = ref_move.checkout
        worktree = tmp_path / "linked"
        _git(caller, "worktree", "add", "-qb", "fix", str(worktree))
        assert ref_move.baseline.take(worktree, "--main-checkout", str(caller)).returncode == 0
        edit = worktree / "a.txt"
        edit.write_text("formatted local fix\n", encoding="utf-8")
        _git(caller, "fetch", "--no-tags", ".", ref_move.new + ":refs/heads/main")
        assert ref_move.baseline.verify() == (3, ["common:refs"])
        _slot("hold", "--slot", ref_move.slot, "--owner", "review")
        recovered = ref_move.restart()
        assert recovered.returncode == 0, recovered.stderr
        receipt = json.loads(recovered.stdout)
        successor = Baseline(ref_move.out)
        successor.expect = receipt["sha256"]
        assert successor.verify() == (0, [])
        assert edit.read_text(encoding="utf-8") == "formatted local fix\n"
        assert _git(worktree, "rev-parse", "HEAD") == ref_move.old
        assert _git(caller, "rev-parse", "main") == ref_move.new

    def test_incomplete_ref_inventory_preserves_hold(self, ref_move: _RefRestart) -> None:
        """Git warns about an unreadable ref that its normal listing otherwise omits from the hash."""
        ref_move.move(ref_move.old, ref_move.new)
        (ref_move.checkout / ".git" / "refs" / "heads" / "broken").write_text("invalid\n", encoding="ascii")
        ref_move.assert_refused()

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(("tag", "unexpected"), id="additional-ref-within-same-aggregate"),
            pytest.param(("config", "user.name", "other"), id="other-metadata"),
        ],
    )
    def test_other_drift_keeps_hold(self, ref_move: _RefRestart, change: tuple[str, ...]) -> None:
        """The expected target is insufficient when any other recorded state has changed."""
        ref_move.move(ref_move.old, ref_move.new)
        _git(ref_move.checkout, *change)
        ref_move.assert_refused()

    def test_rewind_keeps_hold(self, ref_move: _RefRestart) -> None:
        """A precisely described backward move fails even though restoring the old value would match the aggregate."""
        _git(ref_move.checkout, "update-ref", "refs/heads/main", ref_move.new, ref_move.old)
        ref_move.move(ref_move.new, ref_move.old)
        ref_move.assert_refused(**{"--old-oid": ref_move.new, "--new-oid": ref_move.old})

    def test_branch_checked_out_elsewhere_keeps_hold(self, ref_move: _RefRestart, tmp_path: Path) -> None:
        """An unrelated linked worktree can occupy the branch even while the guarded checkout stays on its topic."""
        _git(ref_move.checkout, "worktree", "add", "-q", str(tmp_path / "occupied"), "main")
        ref_move.move(ref_move.old, ref_move.new)
        ref_move.assert_refused()

    @pytest.mark.parametrize(
        ("option", "revision"),
        [
            pytest.param("--old-oid", "HEAD^", id="ancestor-that-was-never-the-prior-tip"),
            pytest.param("--new-oid", "HEAD", id="stale-attested-tip-despite-reflog-message"),
            pytest.param("--new-oid", "HEAD^{tree}", id="tree-object"),
        ],
    )
    def test_wrong_attestation_keeps_hold(self, ref_move: _RefRestart, option: str, revision: str) -> None:
        """Existing Git objects and a convincing reflog message cannot substitute for the attested commit pair."""
        ref_move.move(ref_move.old, ref_move.new)
        ref_move.assert_refused(**{option: _git(ref_move.checkout, "rev-parse", revision)})

    def test_unapproved_tip_keeps_hold_even_with_matching_reflog(self, ref_move: _RefRestart) -> None:
        """A different descendant and a matching reflog message cannot establish the parent's new target."""
        sibling = _git(ref_move.checkout, "commit-tree", "HEAD^{tree}", "-p", ref_move.old, "-m", "unapproved target")
        ref_move.move(ref_move.old, sibling)
        ref_move.assert_refused()

    def test_wrong_original_digest_keeps_hold(self, ref_move: _RefRestart) -> None:
        """Even a valid fast-forward requires the original evidence digest kept outside the snapshot."""
        ref_move.move(ref_move.old, ref_move.new)
        ref_move.assert_refused(**{"--expect": "0" * 64})

    def test_another_owner_cannot_restart(self, ref_move: _RefRestart) -> None:
        """The correct object IDs give a peer no authority over this run's exclusive claim."""
        ref_move.move(ref_move.old, ref_move.new)
        ref_move.assert_refused(**{"--owner": "peer"})

    def test_exclusive_claim_from_other_repository_cannot_restart(
        self, ref_move: _RefRestart, independent_review: Path
    ) -> None:
        """Equal owner names never transfer an exclusive claim between independent common directories."""
        other = _slot("acquire", "--checkout", str(independent_review), "--owner", "review")
        _slot("hold", "--slot", other["slot"], "--owner", "review")
        ref_move.move(ref_move.old, ref_move.new)
        ref_move.assert_refused(**{"--slot": other["slot"]})
        assert _slot("status", "--slot", other["slot"]) == dict(other, status="inspection-required")

    def test_existing_evidence_survives_restart_attempt(self, ref_move: _RefRestart) -> None:
        """A valid ref transition cannot overwrite a previous evidence file or clear the hold as a side effect."""
        ref_move.move(ref_move.old, ref_move.new)
        previous = ref_move.baseline.file.with_name("previous.json")
        previous.write_bytes(b"prior evidence")
        ref_move.assert_refused(**{"--out": str(previous)})
        assert previous.read_bytes() == b"prior evidence"

    def test_scoped_claim_cannot_restart(self, checkout: Path, tmp_path: Path) -> None:
        """A retained target-scoped hold cannot authorize a repository-wide ref change."""
        _git(checkout, "switch", "-qc", "topic")
        old = _git(checkout, "rev-parse", "HEAD")
        new = _git(checkout, "commit-tree", "HEAD^{tree}", "-p", old, "-m", "published target")
        target = _slot("acquire", "--checkout", str(checkout), "--local", "--owner", "review")
        ref_move = _RefRestart(checkout, Baseline(tmp_path / "integrity.json"), target["slot"], old, new)
        ref_move.move(old, new)
        ref_move.assert_refused()


@pytest.mark.integration
@pytest.mark.installed_plugin
class TestInspectedIntegrityRestart:
    """A new baseline can cover an inspected interval while the original evidence remains reproducible."""

    def test_restart_keeps_failed_interval_and_watches_new_changes(
        self, checkout: Path, baseline: Baseline, ignored_observation: str, tmp_path: Path
    ) -> None:
        """The new interval verifies only until another write, with the old snapshot still failing separately."""
        original_bytes = baseline.file.read_bytes()
        fresh = Baseline(tmp_path / "integrity-restart.json")
        started = baseline.restart(ignored_observation, fresh.file)
        assert started.returncode == 0, started.stderr
        fresh.expect = json.loads(started.stdout)["sha256"]
        assert fresh.expect != baseline.expect
        assert fresh.verify() == (0, [])
        assert baseline.file.read_bytes() == original_bytes
        assert baseline.verify() == (3, ["main-checkout:ignored"])
        (checkout / ".reports" / "another.txt").touch()
        assert fresh.verify() == (3, ["main-checkout:ignored"])

    def test_different_ignored_state_requires_new_inspection(
        self, checkout: Path, baseline: Baseline, ignored_observation: str, tmp_path: Path
    ) -> None:
        """An observation of the same change label cannot authorize later uninspected bytes."""
        (checkout / ".reports" / "result.txt").write_bytes(b"a different fixture result\n")
        out = tmp_path / "integrity-restart.json"
        assert baseline.restart(ignored_observation, out).returncode != 0
        assert not out.exists()

    def test_wrong_original_digest_rejects_matching_observation(
        self, baseline: Baseline, ignored_observation: str, tmp_path: Path
    ) -> None:
        """A correct inspected fingerprint cannot replace the original snapshot anchor."""
        baseline.expect = "0" * 64
        out = tmp_path / "integrity-restart.json"
        assert baseline.restart(ignored_observation, out).returncode != 0
        assert not out.exists()

    def test_git_metadata_drift_has_no_restart_fingerprint(
        self, checkout: Path, baseline: Baseline, ignored_observation: str, tmp_path: Path
    ) -> None:
        """A changed reference ends the eligible interval even with the inspected ignored report still present."""
        _git(checkout, "branch", "other")
        failure = _helper("verify", "--snapshot", str(baseline.file), "--expect", baseline.expect)
        assert failure.returncode == 3
        observed = json.loads(failure.stdout)
        assert observed["changed"] == ["common:refs", "main-checkout:ignored"]
        assert "observed_sha256" not in observed
        assert "review_checkout_slot.py hold" in failure.stderr
        out = tmp_path / "integrity-restart.json"
        assert baseline.restart(ignored_observation, out).returncode != 0
        assert not out.exists()

    def test_review_restart_keeps_review_worktree_watched(self, checkout: Path, review: Path, tmp_path: Path) -> None:
        """Inspecting the invoking checkout's ignored drift never exempts a later edit to the reviewed source."""
        old = Baseline(tmp_path / "integrity.json")
        assert old.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        (checkout / ".venv" / "marker").write_bytes(b"reviewed fixture data")
        failure = _helper("verify", "--snapshot", str(old.file), "--expect", old.expect)
        assert failure.returncode == 3
        observed = json.loads(failure.stdout)
        assert observed["changed"] == ["main-checkout:ignored"]
        assert "review_checkout_slot.py hold" in failure.stderr
        fresh = Baseline(tmp_path / "integrity-restart.json")
        started = old.restart(observed["observed_sha256"], fresh.file)
        assert started.returncode == 0, started.stderr
        fresh.expect = json.loads(started.stdout)["sha256"]
        assert fresh.verify() == (0, [])
        (review / "a.txt").write_bytes(b"changed review content")
        failure = _helper("verify", "--snapshot", str(fresh.file), "--expect", fresh.expect)
        assert failure.returncode == 3
        observed = json.loads(failure.stdout)
        assert any(label.startswith("review:") for label in observed["changed"])
        assert "observed_sha256" not in observed
        assert "review_checkout_slot.py release" in failure.stderr
        assert "restart-inspected" not in failure.stderr

    def test_detached_review_ignored_drift_can_start_an_inspected_interval(
        self, watched_baseline: Baseline, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """Both retained snapshot layouts admit exactly the inspected ignored change on the detached review side."""
        before = watched_baseline.file.read_bytes()
        logs = review / ".reports"
        logs.mkdir()
        report = logs / "compiled.pyc"
        report.write_bytes(b"known result")
        failure = _helper("verify", "--snapshot", str(watched_baseline.file), "--expect", watched_baseline.expect)
        assert failure.returncode == 3
        observed = json.loads(failure.stdout)
        assert observed["changed"] == ["review:ignored"]
        assert Path(observed["git_common_dir"]) == (checkout / ".git").resolve()
        assert "review_checkout_slot.py release" in failure.stderr
        assert "all writers" in failure.stderr
        assert "failed interval" in failure.stderr
        assert "user inspection" in failure.stderr
        assert "restart-inspected" in failure.stderr
        fresh = Baseline(tmp_path / "integrity-restart.json")
        started = watched_baseline.restart(observed["observed_sha256"], fresh.file)
        assert started.returncode == 0, started.stderr
        fresh.expect = json.loads(started.stdout)["sha256"]
        assert fresh.verify() == (0, [])
        assert watched_baseline.file.read_bytes() == before
        assert watched_baseline.verify() == (3, ["review:ignored"])
        report.write_bytes(b"subsequent fixture result")
        out = tmp_path / "unconfirmed.json"
        assert watched_baseline.restart(observed["observed_sha256"], out).returncode != 0
        assert not out.exists()
        assert fresh.verify() == (3, ["review:ignored"])

    def test_modified_ignored_review_file_holds_the_slot_and_allows_inspected_restart(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """An existing ignored file requires inspection before a held detached review can start a new interval."""
        report = review / ".reports" / "run.log"
        report.parent.mkdir()
        report.write_bytes(b"before")
        old = Baseline(tmp_path / "integrity.json")
        assert old.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        report.write_bytes(b"after")
        failure = _helper("verify", "--snapshot", str(old.file), "--expect", old.expect)
        assert failure.returncode == 3
        observed = json.loads(failure.stdout)
        assert observed["ignored_changes"] == {"review:ignored": {"modified": [".reports/run.log"]}}
        assert "review_checkout_slot.py hold" in failure.stderr
        assert "affected external" in failure.stderr
        fresh = Baseline(tmp_path / "integrity-restart.json")
        started = old.restart(observed["observed_sha256"], fresh.file)
        assert started.returncode == 0, started.stderr
        fresh.expect = json.loads(started.stdout)["sha256"]
        assert fresh.verify() == (0, [])
        assert old.verify() == (3, ["review:ignored"])

    def test_ignored_drift_on_attached_review_surface_is_ineligible(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """A watched draft on an attached branch cannot use the detached review exception."""
        _git(review, "switch", "-c", "draft")
        old = Baseline(tmp_path / "integrity.json")
        assert old.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        logs = review / ".reports"
        logs.mkdir()
        (logs / "result.txt").touch()
        failure = _helper("verify", "--snapshot", str(old.file), "--expect", old.expect)
        assert failure.returncode == 3
        observed = json.loads(failure.stdout)
        assert observed["changed"] == ["review:ignored"]
        assert "observed_sha256" not in observed
        assert "review_checkout_slot.py hold" in failure.stderr

    def test_unrecognized_review_fingerprint_keeps_the_slot_for_inspection(self, watched_baseline: Baseline) -> None:
        """The review prefix alone cannot assign a fingerprint introduced by another verifier to a private checkout."""
        body = json.loads(watched_baseline.file.read_text(encoding="utf-8"))
        body["marks"]["review:unknown"] = "retained-value"
        watched_baseline.file.write_text(json.dumps(body), encoding="utf-8")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        failure = _helper("verify", "--snapshot", str(watched_baseline.file), "--expect", digest)
        assert failure.returncode == 3
        assert json.loads(failure.stdout) == {"ok": False, "changed": ["review:unknown"]}
        assert "review_checkout_slot.py hold" in failure.stderr

    def test_existing_evidence_cannot_be_replaced(
        self, baseline: Baseline, ignored_observation: str, tmp_path: Path
    ) -> None:
        """Neither the failed snapshot nor an occupied sibling path can be reused as a destination."""
        original_bytes = baseline.file.read_bytes()
        other = tmp_path / "evidence.json"
        other.write_bytes(b"previous evidence")
        assert baseline.restart(ignored_observation, baseline.file).returncode != 0
        assert baseline.restart(ignored_observation, other).returncode != 0
        assert baseline.file.read_bytes() == original_bytes
        assert other.read_bytes() == b"previous evidence"

    def test_restart_cannot_change_excluded_output_directory(
        self, baseline: Baseline, ignored_observation: str, checkout: Path
    ) -> None:
        """A different directory would change the automatic evidence exclusion in a fresh snapshot."""
        out = checkout / ".reports" / "integrity-restart.json"
        assert baseline.restart(ignored_observation, out).returncode != 0
        assert not out.exists()
