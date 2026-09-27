"""Implementation hashing, repository fingerprints, and parity-runtime admission."""

import hashlib
import inspect
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path


# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)
from _bench_common.mutation_isolation import (
    verify_index_relocation,
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (
    PARITY_MANIFEST_PATH,
)

from _bench_claude.agentic.models import ToolCounts
from _bench_claude.agentic.ground_truth import GroundTruth
from _bench_claude.agentic.scoring import score_fix, score_read_crop


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a concrete benchmark input file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository_fingerprint(repo_path: Path) -> str:
    """Return the checked-out commit SHA, with a deterministic non-git test fallback."""
    completed = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return hashlib.sha256(str(repo_path.resolve()).encode("utf-8")).hexdigest()


def _validate_parity_runtime(
    repo_path: Path,
    index_path: Path,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Reject a canonical agentic run outside the locked target and index.

    ``index_relocation`` excuses the locked byte hash only: a run in its own worktree proves index identity through
    relocation provenance, while the commit, tree, worktree cleanliness, metadata, and module-count checks stay exactly
    as strict.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"experiment manifest {manifest_path} is not valid JSON: {exc}") from exc

    target = manifest["target_source"]
    if _repository_fingerprint(repo_path) != target["commit"]:
        raise ValueError(f"canonical run requires target commit {target['commit']}")
    tree = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD^{tree}"],
        capture_output=True,
        check=False,
        text=True,
    )
    if tree.returncode != 0 or tree.stdout.strip() != target["tree"]:
        raise ValueError(f"canonical run requires target tree {target['tree']}")
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True,
        check=False,
        text=True,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise ValueError("canonical run requires a clean target worktree")

    expected_index = manifest["index"]
    index_sha256 = _sha256_file(index_path)
    if index_relocation is None and index_sha256 != expected_index["raw_sha256"]:
        raise ValueError("canonical run requires the locked index bytes")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("canonical run index is not valid JSON") from exc
    if index_relocation is not None:
        verify_index_relocation(
            index_relocation,
            metadata=index,
            index_sha256=index_sha256,
            repo_path=repo_path,
            frozen_index_sha256=expected_index["raw_sha256"],
        )
    for index_field in ("git_sha", "scan_version"):
        if index.get(index_field) != expected_index[index_field]:
            raise ValueError(f"canonical run index {index_field} does not match the locked manifest")
    if len(index.get("modules", [])) != expected_index["module_count"]:
        raise ValueError("canonical run index module count does not match the locked manifest")


def _invokes_scan_query(command: str) -> bool:
    """Return whether a shell command executes scan-query at a command boundary."""
    boundary = r"(?:^|&&|\|\||;|\|)\s*"
    environment = r"(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    return re.search(rf"{boundary}{environment}(?:\S*/)?scan-query(?:\s|$)", command) is not None


def _codemap_use_attempted(tools: ToolCounts) -> bool:
    """Return whether telemetry contains a Codemap Skill or scan-query attempt."""
    return tools.codemap > 0 or tools.scan_query > 0


def _evaluator_provenance(task_type: str) -> tuple[str, str]:
    """Identify the actual task-family scorer and hash its implementation source."""
    if task_type == "read_crop":
        evaluator = score_read_crop
    elif task_type in ("fix_single", "fix_multicaller"):
        evaluator = score_fix
    else:
        evaluator = GroundTruth.score
    evaluator_id = f"{evaluator.__module__}.{evaluator.__qualname__}"
    return evaluator_id, hashlib.sha256(inspect.getsource(evaluator).encode("utf-8")).hexdigest()
