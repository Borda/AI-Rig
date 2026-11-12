"""Exercise packaged helper imports with executable, unverified bytecode beside their source."""

from __future__ import annotations

import importlib.util
import os
import py_compile
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
PROBE = """
import importlib
import importlib.util
import runpy
import sys
from pathlib import Path

entry, root, action = map(Path, sys.argv[1:4])
sys.path[:0] = [str(entry.parent), str(root / 'shared')]
namespace = runpy.run_path(str(entry))
if str(action) == 'hook':
    agents = entry.parent.parent / 'AGENTS.md'
    agents.write_text('unmanaged instructions', encoding='utf-8')
    assert namespace['_managed_block_root'](entry.parent.parent, agents) == (False, None)
elif str(action) == 'finalize':
    module = namespace['_load_module']('source_only_writer', 'write-result.py')
    assert module.RESULT_SCHEMA_VERSION == 2
elif str(action) == 'batches':
    producer, validator = namespace['_helpers']()
    assert callable(producer.prepare)
    assert callable(validator.collect_result_errors)
assert callable(namespace[sys.argv[4]])
print('source executed')
"""
CONTROL = """
import importlib
import importlib.util
import runpy
import sys
from pathlib import Path

path = Path(sys.argv[1])
sys.path.insert(0, str(path.parent))
if sys.argv[2] == 'import':
    value = importlib.import_module(path.stem).value
elif sys.argv[2] == 'importlib':
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    value = module.value
else:
    value = runpy.run_path(str(path))['value']
print(value)
"""


def _plant_cache(source: Path, payload: str, mode: str) -> Path:
    """Compile a substitute payload into a cache that Python accepts for the untouched source."""
    planted = source.parent / "planted.py"
    planted.write_text(payload, encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "pycache_prefix", None)
        cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(exist_ok=True)
    invalidation = getattr(py_compile.PycInvalidationMode, mode)
    py_compile.compile(str(planted), cfile=str(cache), invalidation_mode=invalidation, doraise=True)
    if mode == "TIMESTAMP":
        metadata = source.stat()
        content = cache.read_bytes()
        cache.write_bytes(content[:8] + struct.pack("<II", int(metadata.st_mtime), metadata.st_size) + content[16:])
    planted.unlink()
    return cache


@pytest.fixture
def source_package(tmp_path: Path) -> Path:
    """Copy only shipped Python sources into a private package without importing any of them."""
    destination = tmp_path / "plugin"
    for directory in (
        "hooks",
        "scripts",
        "shared",
        "runtime/calibration",
        "skills/code-review",
        "skills/challenge-resolve",
    ):
        target = destination / directory
        target.mkdir(parents=True)
        for source in (PLUGIN_ROOT / directory).glob("*.py"):
            shutil.copy2(source, target / source.name)
    return destination


@pytest.mark.parametrize("mode", ["TIMESTAMP", "UNCHECKED_HASH"])
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        pytest.param("import", "cached", id="ordinary-import-consults-cache"),
        pytest.param("importlib", "cached", id="file-spec-consults-cache"),
        pytest.param("runpy", "source", id="explicit-source-path-ignores-cache"),
    ],
)
def test_loader_cache_controls(tmp_path: Path, mode: str, kind: str, expected: str) -> None:
    """Distinguish real cache execution from explicit source-path execution in a clean interpreter."""
    source = tmp_path / "helper.py"
    source.write_text("value = 'source'\n", encoding="utf-8")
    _plant_cache(source, "value = 'cached'\n", mode)
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPYCACHEPREFIX", "PYTHONPATH"}}

    result = subprocess.run(
        [sys.executable, "-c", CONTROL, str(source), kind],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.packaging
@pytest.mark.integration
@pytest.mark.parametrize("mode", ["TIMESTAMP", "UNCHECKED_HASH"])
@pytest.mark.parametrize(
    ("entry", "dependency", "action", "export"),
    [
        pytest.param(
            "hooks/session_start.py",
            "scripts/install_global_agents.py",
            "hook",
            "_managed_block_root",
            id="session-start",
        ),
        pytest.param(
            "shared/local_reviewer_wave.py",
            "shared/parallel_execution.py",
            "import",
            "validate_evidence",
            id="reviewer-wave",
        ),
        pytest.param(
            "shared/parallel_worktrees.py",
            "shared/collect_diff.py",
            "import",
            "_portable_relative",
            id="parallel-worktrees",
        ),
        pytest.param(
            "shared/remediation_finalize.py",
            "shared/write-result.py",
            "finalize",
            "_load_module",
            id="remediation-finalize",
        ),
        pytest.param(
            "shared/test_targets.py", "shared/codemap_adapter.py", "import", "heuristic_targets", id="test-targets"
        ),
        pytest.param(
            "shared/remediation_branch.py",
            "shared/collect_pr.py",
            "import",
            "_head_repository",
            id="remediation-branch",
        ),
        pytest.param(
            "shared/validate-artifacts.py", "shared/adversarial_loop.py", "import", "load_ledger", id="shared-validator"
        ),
        pytest.param(
            "shared/collect_pr.py", "shared/collect_diff.py", "import", "canonical_diff_arguments", id="pr-collector"
        ),
        pytest.param(
            "shared/run_gates.py", "shared/gate_test_approval.py", "import", "retain_approval", id="gate-runner"
        ),
        pytest.param(
            "shared/release_evidence.py", "shared/run_gates.py", "import", "terminate_process", id="release-evidence"
        ),
        pytest.param(
            "shared/write-result.py", "shared/gate_test_approval.py", "import", "validate_pass", id="result-writer"
        ),
        pytest.param(
            "runtime/calibration/run.py",
            "runtime/calibration/live_contract.py",
            "import",
            "build_prompt",
            id="calibration-runner",
        ),
        pytest.param(
            "runtime/calibration/score_behavioral.py",
            "runtime/calibration/live_contract.py",
            "import",
            "build_prompt",
            id="behavioral-scorer",
        ),
        pytest.param(
            "runtime/calibration/run_live_ab.py",
            "runtime/calibration/live_contract.py",
            "import",
            "build_prompt",
            id="live-runner",
        ),
        pytest.param(
            "skills/challenge-resolve/validate_evidence.py",
            "shared/adversarial_loop.py",
            "import",
            "load_ledger",
            id="challenge-validator",
        ),
        pytest.param(
            "skills/challenge-resolve/chunk_diff.py",
            "shared/collect_diff.py",
            "import",
            "diff_sections",
            id="chunk-planner",
        ),
        pytest.param(
            "skills/code-review/review_prepare.py",
            "skills/code-review/review_batches.py",
            "import",
            "prepare",
            id="review-preparer",
        ),
        pytest.param(
            "skills/code-review/review_routing.py",
            "shared/collect_diff.py",
            "import",
            "diff_sections",
            id="review-routing",
        ),
        pytest.param(
            "skills/code-review/validate_artifacts.py",
            "shared/codemap_adapter.py",
            "import",
            "collect_result_errors",
            id="review-validator",
        ),
        pytest.param(
            "skills/code-review/review_execution_validation.py",
            "shared/local_reviewer_wave.py",
            "import",
            "validate_local_reviewer_evidence",
            id="execution-validator",
        ),
        pytest.param(
            "skills/code-review/review_batches.py",
            "skills/code-review/review_prepare.py",
            "batches",
            "_helpers",
            id="review-batches",
        ),
    ],
)
def test_packaged_helpers_execute_source(
    source_package: Path, tmp_path: Path, mode: str, entry: str, dependency: str, action: str, export: str
) -> None:
    """Execute each helper's real import boundary while its dependency has a valid malicious cache."""
    marker = tmp_path / "cached-payload-executed"
    payload = (
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('cached payload executed')\n"
    )
    _plant_cache(source_package / dependency, payload, mode)
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPYCACHEPREFIX", "PYTHONPATH"}}

    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(source_package / entry), str(source_package), action, export],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert not marker.exists(), result.stderr
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "source executed"
