"""Exercise package timing diagnostics through real isolated two-worker pytest runs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parents[1]


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
