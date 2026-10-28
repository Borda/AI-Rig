"""Exercise package timing diagnostics through real isolated two-worker pytest runs."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.packaging
def test_package_shards_execute_the_complete_selection_once(diagnostic_child: tuple[Path, dict[str, str]]) -> None:
    """Partition real marker-selected cases without duplication, omissions, or ordinary-run filtering."""
    root, env = diagnostic_child
    (root / "test_probe.py").write_text(
        "import pytest\n"
        "@pytest.mark.installed_plugin\n"
        "@pytest.mark.parametrize('value', range(24))\n"
        "def test_selected(value):\n    pass\n"
        "def test_checkout_only():\n    assert False, 'outside installed selection'\n",
        encoding="utf-8",
    )
    selections: list[set[str]] = []
    for shard in (None, "0/4", "1/4", "2/4", "3/4", "0/4"):
        command = [sys.executable, "-m", "pytest", "-q", "-n", "2", "-m", "installed_plugin", "--strict-markers"]
        if shard is not None:
            command.extend(["--installed-package-shard", shard])
        result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        # Phase diagnostics expose the node IDs executed by actual xdist workers.
        selections.append(
            {
                line.split("[installed-package] call passed ", 1)[1].split(" duration=", 1)[0]
                for line in result.stdout.splitlines()
                if "[installed-package] call passed " in line
            }
        )
    complete, *shards = selections[:-1]
    assert len(complete) == 24
    assert all(shards), selections
    assert set.union(*shards) == complete
    assert sum(map(len, shards)) == len(complete), selections
    assert selections[-1] == shards[0]


@pytest.mark.integration
@pytest.mark.packaging
@pytest.mark.parametrize("shard", ["bad", "-1/4", "4/4", "0/0", "0/", "0/4/2"])
def test_package_shards_reject_invalid_coordinates(diagnostic_child: tuple[Path, dict[str, str]], shard: str) -> None:
    """Reject malformed shard requests before collecting or executing tests."""
    root, env = diagnostic_child
    (root / "test_probe.py").write_text("def test_probe():\n    assert False\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", f"--installed-package-shard={shard}"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 4
    assert "shard must be INDEX/COUNT with 0 <= INDEX < COUNT" in result.stderr


@pytest.mark.integration
@pytest.mark.packaging
@pytest.mark.parametrize("selection", ["empty-marker", "empty-shard", "failure"])
def test_package_shards_preserve_empty_and_failure_results(
    diagnostic_child: tuple[Path, dict[str, str]], selection: str
) -> None:
    """Empty partitions remain no-tests errors and selected test failures remain failures."""
    root, env = diagnostic_child
    (root / "test_probe.py").write_text(
        "import pytest\n@pytest.mark.installed_plugin\ndef test_probe():\n    assert False, 'selected failure'\n",
        encoding="utf-8",
    )
    destination = int.from_bytes(hashlib.sha256(b"test_probe.py::test_probe").digest(), "big") % 2
    shard = f"{1 - destination}/2" if selection == "empty-shard" else f"{destination}/2"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "2",
            "--installed-package-shard",
            shard,
            "-m",
            "live" if selection == "empty-marker" else "installed_plugin",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == (1 if selection == "failure" else 5), result.stdout + result.stderr
    if selection == "failure":
        assert "selected failure" in result.stdout


@pytest.fixture
def diagnostic_child(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Copy the real reporting hooks and their platform dependency outside checkout context."""
    for name in ("conftest.py", "_platform.py"):
        shutil.copy2(TESTS_ROOT / name, tmp_path / name)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["CODEX_RIG_PACKAGE_TIMINGS"] = "1"
    return tmp_path, env


@pytest.mark.integration
@pytest.mark.packaging
@pytest.mark.parametrize("phase", ["call", "teardown"])
def test_package_timings_survive_interrupted_workers(diagnostic_child: tuple[Path, dict[str, str]], phase: str) -> None:
    """A timeout retains the fast case and distinguishes call stalls from teardown stalls."""
    root, env = diagnostic_child
    (root / "test_probe.py").write_text(
        "from pathlib import Path\nimport time\nimport pytest\n"
        "def wait_for_release():\n"
        "    Path('waiting').touch()\n"
        "    deadline = time.monotonic() + 45\n"
        "    while not Path('release').exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.02)\n"
        "@pytest.fixture\ndef waiting_teardown():\n    yield\n    wait_for_release()\n"
        "def test_fast():\n    Path('fast').touch()\n"
        + (
            "def test_wait():\n    wait_for_release()\n"
            if phase == "call"
            else "def test_wait(waiting_teardown):\n    pass\n"
        ),
        encoding="utf-8",
    )
    # Windows communicate() timeouts omit partial pipe output until EOF; observe flushed output independently.
    progress_path = root / "progress.log"
    with progress_path.open("wb") as progress_file:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "pytest", "-q", "-n", "2", "test_probe.py"],
            cwd=root,
            env=env,
            stdout=progress_file,
            stderr=subprocess.STDOUT,
        )
        try:
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
                        and (phase == "call" or "[installed-package] call passed test_probe.py::test_wait" in progress)
                    ):
                        break
                else:
                    pytest.fail(
                        "diagnostic child exited before the interruption probe:\n"
                        + progress_path.read_text(encoding="utf-8", errors="replace")
                    )
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            output = progress_path.read_text(encoding="utf-8", errors="replace")
            assert "[installed-package] start test_probe.py::test_wait" in output
            assert "[installed-package] teardown passed test_probe.py::test_fast duration=" in output
            assert "[installed-package] teardown passed test_probe.py::test_wait" not in output
            assert ("[installed-package] call passed test_probe.py::test_wait" in output) == (phase == "teardown")
        finally:
            (root / "release").touch()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
                raise
    assert process.returncode == 0


@pytest.mark.integration
@pytest.mark.packaging
@pytest.mark.parametrize("enabled", [False, True])
def test_package_timings_keep_real_failure_output_and_are_opt_in(
    diagnostic_child: tuple[Path, dict[str, str]], enabled: bool
) -> None:
    """A real failing child keeps both streams; ordinary runs do not emit timing events."""
    root, env = diagnostic_child
    if not enabled:
        env.pop("CODEX_RIG_PACKAGE_TIMINGS")
    (root / "test_probe.py").write_text(
        "import sys\ndef test_failure():\n"
        "    print('probe stdout')\n    print('probe stderr', file=sys.stderr)\n    assert False, 'probe failure'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-u", "-m", "pytest", "-q", "-n", "2", "test_probe.py"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "probe stdout" in result.stdout
    assert "probe stderr" in result.stdout
    assert ("[installed-package] call failed test_probe.py::test_failure duration=" in result.stdout) == enabled
