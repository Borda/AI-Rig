"""Exercise package timing diagnostics through real isolated two-worker pytest runs."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parents[2]
TIMING_ENV = "CODEX_RIG_PACKAGE_TIMINGS"
#: Bounds one child pytest run; sized for a loaded machine.
CHILD_PYTEST_TIMEOUT = 90
#: Probe whose ``test_fast`` finishes at once, plus a fixture/helper that blocks until ``release`` appears.
STALL_PROBE = (
    "from pathlib import Path\n"
    "import time\n"
    "import pytest\n"
    "def wait_for_release():\n"
    "    Path('waiting').touch()\n"
    "    deadline = time.monotonic() + 45\n"
    "    while not Path('release').exists() and time.monotonic() < deadline:\n"
    "        time.sleep(0.02)\n"
    "@pytest.fixture\ndef waiting_teardown():\n    yield\n    wait_for_release()\n"
    "def test_fast():\n    Path('fast').touch()\n"
)
#: Probe with 24 marker-selected cases and one unselected test that fails if it ever runs.
SHARD_PROBE = (
    "import pytest\n"
    "@pytest.mark.installed_plugin\n"
    "@pytest.mark.parametrize('value', range(24))\n"
    "def test_selected(value):\n    pass\n"
    "def test_checkout_only():\n    assert False, 'outside installed selection'\n"
)
#: Probe whose single test writes to both streams and then fails.
FAILURE_PROBE = (
    "import sys\n"
    "def test_failure():\n"
    "    print('probe stdout')\n    print('probe stderr', file=sys.stderr)\n    assert False, 'probe failure'\n"
)
DiagnosticChild = tuple[Path, dict[str, str]]


def _write_probe(root: Path, source: str) -> None:
    """Write the single probe test module the child pytest run collects."""
    (root / "test_probe.py").write_text(source, encoding="utf-8")


def _run_child(root: Path, env: dict[str, str], *pytest_args: str) -> subprocess.CompletedProcess[str]:
    """Run one bounded child pytest in ``root`` and capture both streams as text."""
    return subprocess.run(
        [sys.executable, "-u", "-m", "pytest", *pytest_args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=CHILD_PYTEST_TIMEOUT,
        check=False,
    )


def _executed_node_ids(root: Path, env: dict[str, str], shard: str | None) -> set[str]:
    """Run the marker-selected probe under two workers (optionally one shard) and return the node IDs they executed."""
    shard_options = ["--installed-package-shard", shard] if shard is not None else []
    result = _run_child(
        root, env, "-q", "-n", "2", "--dist", "loadgroup", "-m", "installed_plugin", "--strict-markers", *shard_options
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Phase diagnostics expose the node IDs executed by actual xdist workers.
    return {
        line.split("[installed-package] call passed ", 1)[1].split(" duration=", 1)[0]
        for line in result.stdout.splitlines()
        if "[installed-package] call passed " in line
    }


def _wait_until_stalled(
    process: subprocess.Popen[bytes], root: Path, progress_path: Path, call_completes: bool
) -> None:
    """Block until the probe worker is parked in its stall, failing if the child exits or never gets there.

    Progress is read from the flushed log file because Windows ``communicate()`` timeouts omit partial pipe output.
    """
    deadline = time.monotonic() + 30
    while True:
        progress = progress_path.read_text(encoding="utf-8", errors="replace")
        assert time.monotonic() < deadline, f"diagnostic workers never became ready:\n{progress}"
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            if (
                (root / "waiting").exists()
                and "[installed-package] teardown passed test_probe.py::test_fast duration=" in progress
                and (not call_completes or "[installed-package] call passed test_probe.py::test_wait" in progress)
            ):
                return
        else:
            pytest.fail("diagnostic child exited before the interruption probe:\n" + progress)


@pytest.fixture
def diagnostic_child(tmp_path: Path) -> DiagnosticChild:
    """Copy the real reporting hooks and their platform dependency outside checkout context."""
    for name in ("conftest.py", "_platform.py"):
        shutil.copy2(TESTS_ROOT / name, tmp_path / name)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env[TIMING_ENV] = "1"
    return tmp_path, env


@pytest.fixture
def launch_probe(
    diagnostic_child: DiagnosticChild,
) -> Generator[Callable[[str], tuple[subprocess.Popen[bytes], Path]], None, None]:
    """Return a launcher for a two-worker child pytest whose output goes to a file; always release and reap children."""
    root, env = diagnostic_child
    children: list[tuple[subprocess.Popen[bytes], object]] = []

    def launch(source: str) -> tuple[subprocess.Popen[bytes], Path]:
        """Start the child on ``source`` and return it with the path of its flushed progress log."""
        _write_probe(root, source)
        progress_path = root / "progress.log"
        stream = progress_path.open("wb")
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "pytest", "-q", "-n", "2", "test_probe.py"],
            cwd=root,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        children.append((process, stream))
        return process, progress_path

    yield launch

    (root / "release").touch()
    for process, stream in children:
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
        finally:
            stream.close()


class TestPackageShards:
    """``--installed-package-shard`` partitions the marker-selected tests deterministically."""

    @pytest.mark.integration
    @pytest.mark.packaging
    def test_execute_complete_selection_once(self, diagnostic_child: DiagnosticChild) -> None:
        """Four shards together run every marker-selected case exactly once, and an unsharded run filters nothing.

        A probe with 24 selected cases and one unselected failing test is run unsharded, as shards 0..3 of 4, and shard
        0 again. Shards must be non-empty, disjoint, and cover the unsharded set; the repeat must match shard 0.
        """
        root, env = diagnostic_child
        _write_probe(root, SHARD_PROBE)

        selections = [_executed_node_ids(root, env, shard) for shard in (None, "0/4", "1/4", "2/4", "3/4", "0/4")]

        complete, *shards, repeat = selections
        assert len(complete) == 24
        assert all(shards), selections
        assert set.union(*shards) == complete
        assert sum(map(len, shards)) == len(complete), selections
        assert repeat == shards[0]

    @pytest.mark.integration
    @pytest.mark.packaging
    def test_shared_fixture_groups_execute_once_across_shards_and_workers(
        self, diagnostic_child: DiagnosticChild
    ) -> None:
        """Keep each expensive fixture variant on one shard and worker without losing any selected case."""
        root, env = diagnostic_child
        _write_probe(
            root,
            "from pathlib import Path\nfrom uuid import uuid4\nimport pytest\n"
            "@pytest.fixture(scope='module', params=[\n"
            "    pytest.param(name, marks=pytest.mark.xdist_group(name=name))\n"
            "    for name in ('alpha', 'beta', 'gamma', 'delta')\n])\n"
            "def completed(request):\n"
            "    Path(f'{uuid4()}.setup').write_text(request.param, encoding='utf-8')\n"
            "    return request.param\n"
            "@pytest.mark.installed_plugin\n@pytest.mark.parametrize('value', range(6))\n"
            "def test_receipt(completed, value):\n"
            "    Path(f'{uuid4()}.call').write_text(f'{completed}-{value}', encoding='utf-8')\n"
            "    assert completed\n" + SHARD_PROBE,
        )

        selections = [_executed_node_ids(root, env, f"{index}/4") for index in range(4)]

        assert sum(map(len, selections)) == 48
        assert len(set.union(*selections)) == 48
        assert sorted(path.read_text(encoding="utf-8") for path in root.glob("*.setup")) == [
            "alpha",
            "beta",
            "delta",
            "gamma",
        ]
        calls = [path.read_text(encoding="utf-8") for path in root.glob("*.call")]
        assert sorted(calls) == sorted(
            f"{name}-{value}" for name in ("alpha", "beta", "gamma", "delta") for value in range(6)
        )

    @pytest.mark.integration
    @pytest.mark.packaging
    @pytest.mark.parametrize("shard", ["bad", "-1/4", "4/4", "0/0", "0/", "0/4/2"])
    def test_reject_invalid_coordinates(self, diagnostic_child: DiagnosticChild, shard: str) -> None:
        """Malformed shard requests are rejected with a usage error before any test is collected or run.

        The probe would fail if it ran, so exit code 4 plus the coordinate message proves nothing was executed.
        """
        root, env = diagnostic_child
        _write_probe(root, "def test_probe():\n    assert False\n")

        result = _run_child(root, env, f"--installed-package-shard={shard}")

        assert result.returncode == 4
        assert "shard must be INDEX/COUNT with 0 <= INDEX < COUNT" in result.stderr

    @pytest.mark.integration
    @pytest.mark.packaging
    @pytest.mark.parametrize(
        ("shard_offset", "marker", "returncode", "expected_output"),
        [
            pytest.param(0, "live", 5, "", id="empty-marker"),
            pytest.param(1, "installed_plugin", 5, "", id="empty-shard"),
            pytest.param(0, "installed_plugin", 1, "selected failure", id="failure"),
        ],
    )
    def test_preserve_empty_and_failure_results(
        self, diagnostic_child: DiagnosticChild, shard_offset: int, marker: str, returncode: int, expected_output: str
    ) -> None:
        """Empty partitions remain "no tests" errors and selected test failures remain failures.

        The one selected test fails and hashes to a known shard. Selecting an unused marker or the other shard yields
        exit 5; selecting its own shard and marker yields exit 1 with the failure message.
        """
        root, env = diagnostic_child
        _write_probe(
            root,
            "import pytest\n@pytest.mark.installed_plugin\ndef test_probe():\n    assert False, 'selected failure'\n",
        )
        destination = int.from_bytes(hashlib.sha256(b"test_probe.py::test_probe").digest(), "big") % 2

        result = _run_child(
            root,
            env,
            "-q",
            "-n",
            "2",
            "--installed-package-shard",
            f"{(destination + shard_offset) % 2}/2",
            "-m",
            marker,
        )

        assert result.returncode == returncode, result.stdout + result.stderr
        assert expected_output in result.stdout


class TestPackageTimings:
    """Opt-in phase timings stay useful when workers stall or tests fail."""

    @pytest.mark.integration
    @pytest.mark.packaging
    @pytest.mark.parametrize(
        ("wait_test", "call_completes"),
        [
            pytest.param("def test_wait():\n    wait_for_release()\n", False, id="call"),
            pytest.param("def test_wait(waiting_teardown):\n    pass\n", True, id="teardown"),
        ],
    )
    def test_survive_interrupted_workers(
        self,
        diagnostic_child: DiagnosticChild,
        launch_probe: Callable[[str], tuple[subprocess.Popen[bytes], Path]],
        wait_test: str,
        call_completes: bool,
    ) -> None:
        """A stalled worker retains the finished fast case and distinguishes call stalls from teardown stalls.

        One probe test finishes while the other blocks in its call or in its fixture teardown. While the child is still
        running, its flushed log must show the fast case fully reported and the stalled case started but not finished;
        once released, the child must exit cleanly.
        """
        root, _env = diagnostic_child
        process, progress_path = launch_probe(STALL_PROBE + wait_test)

        _wait_until_stalled(process, root, progress_path, call_completes)
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        output = progress_path.read_text(encoding="utf-8", errors="replace")
        (root / "release").touch()

        assert "[installed-package] start test_probe.py::test_wait" in output
        assert "[installed-package] teardown passed test_probe.py::test_fast duration=" in output
        assert "[installed-package] teardown passed test_probe.py::test_wait" not in output
        assert ("[installed-package] call passed test_probe.py::test_wait" in output) == call_completes
        assert process.wait(timeout=30) == 0

    @pytest.mark.integration
    @pytest.mark.packaging
    @pytest.mark.parametrize("enabled", [False, True])
    def test_keep_real_failure_output_and_are_opt_in(self, diagnostic_child: DiagnosticChild, enabled: bool) -> None:
        """A real failing child keeps both streams, and timing events appear only when the opt-in variable is set.

        The probe prints to stdout and stderr and then fails; both prints and the failure must survive in the output
        whether or not timing is enabled, while the call-phase timing event must appear exactly when it is enabled.
        """
        root, env = diagnostic_child
        env = {key: value for key, value in env.items() if enabled or key != TIMING_ENV}
        _write_probe(root, FAILURE_PROBE)

        result = _run_child(root, env, "-q", "-n", "2", "test_probe.py")

        assert result.returncode == 1, result.stdout + result.stderr
        assert "probe stdout" in result.stdout
        assert "probe stderr" in result.stdout
        assert ("[installed-package] call failed test_probe.py::test_failure duration=" in result.stdout) == enabled
