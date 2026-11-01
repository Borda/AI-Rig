"""Contract: the ``check-pure-kernels`` hook flags a kernel module that can touch files or processes.

The kernels derive values from supplied evidence only, so an I/O import, an ``open`` call, a write-like attribute or a
``del`` statement in one is a regression that its behaviour tests would not notice.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _repo_checker_run import run_checker

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_pure_kernels.py"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the checker as the hook does and capture its exit code and output."""
    return run_checker(_SCRIPT, *arguments)


def _kernel(tmp_path: Path, source: str) -> Path:
    """Write ``source`` as a kernel module and return its path."""
    module = tmp_path / "_kernel.py"
    module.write_text(source, encoding="utf-8", newline="\n")
    return module


@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param("import os\n", "imports `os`", id="import-os"),
        pytest.param("import os.path\n", "imports `os`", id="import-dotted-submodule"),
        pytest.param("from pathlib import Path\n", "imports `pathlib`", id="from-pathlib"),
        pytest.param("import subprocess\n", "imports `subprocess`", id="import-subprocess"),
        pytest.param("import shutil\n", "imports `shutil`", id="import-shutil"),
        pytest.param("import tempfile\n", "imports `tempfile`", id="import-tempfile"),
        pytest.param("def f(p):\n    return open(p)\n", "calls `open()`", id="call-open"),
        pytest.param("def f(s):\n    return eval(s)\n", "calls `eval()`", id="call-eval"),
        pytest.param("def f(s):\n    exec(s)\n", "calls `exec()`", id="call-exec"),
        pytest.param("def f(s):\n    return compile(s, 'x', 'exec')\n", "calls `compile()`", id="call-compile"),
        pytest.param("def f():\n    return input()\n", "calls `input()`", id="call-input"),
        pytest.param("def f(n):\n    return __import__(n)\n", "calls `__import__()`", id="call-dunder-import"),
        pytest.param(
            "def f(p):\n    p.write_text('x')\n", "write-like attribute `.write_text`", id="attribute-write-text"
        ),
        pytest.param("def f(p):\n    p.unlink()\n", "write-like attribute `.unlink`", id="attribute-unlink"),
        pytest.param(
            "def f(p):\n    return p.replace('a', 'b')\n", "write-like attribute `.replace`", id="attribute-replace"
        ),
        pytest.param("def f(d):\n    del d['k']\n", "`del` statement", id="del-statement"),
    ],
)
def test_flags_each_impure_construct(tmp_path: Path, source: str, message: str) -> None:
    """Each import, call, attribute or statement the kernels must avoid exits 1 with a line naming it and its
    location."""
    module = _kernel(tmp_path, source)

    result = _run(str(module))

    assert result.returncode == 1
    assert message in result.stdout
    assert f"{module.as_posix()}:" in result.stdout


def test_accepts_a_module_of_pure_computation(tmp_path: Path) -> None:
    """Imports of pure standard-library modules and plain arithmetic leave the kernel clean."""
    module = _kernel(
        tmp_path,
        "import hashlib\nfrom dataclasses import dataclass\n\n\ndef digest(data):\n    return hashlib.sha256(data)\n",
    )

    result = _run(str(module))

    assert (result.returncode, result.stdout) == (0, "")


def test_reports_a_missing_kernel_file(tmp_path: Path) -> None:
    """A path that does not exist is a finding, so a renamed kernel cannot silently leave the declared list."""
    result = _run(str(tmp_path / "gone.py"))

    assert result.returncode == 1
    assert "kernel file is missing" in result.stdout


def test_declared_kernels_resolve_under_the_root(tmp_path: Path) -> None:
    """With no file arguments the declared kernel list is read under ``--root``, and an empty root reports each
    module."""
    result = _run("--root", str(tmp_path))

    assert result.returncode == 1
    assert "_agent_shim_approval.py: kernel file is missing" in result.stdout
    assert "_agent_shim_lifecycle.py: kernel file is missing" in result.stdout
