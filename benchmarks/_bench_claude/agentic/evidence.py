"""Evidence-root isolation and staged codemap runtime for a sandboxed agent."""

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Sequence


# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.

from _bench_claude.agentic.config import REPO_ROOT


# ---------------------------------------------------------------------------
# Claude CLI runner
# ---------------------------------------------------------------------------


def _benchmark_evidence_roots(extra_roots: Sequence[Path] = ()) -> tuple[Path, ...]:
    """Return existing benchmark evidence roots that a Claude cell must not read.

    The parent launcher supplies the original evaluator checkout and result root through
    ``BENCHMARK_EVIDENCE_ROOTS``. Direct runner use falls back to this checkout, which
    contains the frozen runner, shared scorer, and prior result history.
    """
    encoded_roots = os.environ.get("BENCHMARK_EVIDENCE_ROOTS")
    if encoded_roots is None:
        raw_roots: list[object] = [str(REPO_ROOT)]
    else:
        try:
            raw_roots = json.loads(encoded_roots)
        except json.JSONDecodeError as exc:
            raise ValueError("BENCHMARK_EVIDENCE_ROOTS must be a JSON list of absolute paths") from exc
        if not isinstance(raw_roots, list) or not raw_roots:
            raise ValueError("BENCHMARK_EVIDENCE_ROOTS must name at least one evidence root")

    roots: list[Path] = []
    for raw_path in [*raw_roots, *(str(path) for path in extra_roots)]:
        if not isinstance(raw_path, str):
            raise ValueError("benchmark evidence roots must be strings")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            raise ValueError(f"benchmark evidence root must be absolute: {raw_path!r}")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"benchmark evidence root is unavailable: {candidate}") from exc
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _absolute_filetool_pattern(path: Path) -> str:
    """Render one absolute Claude file-tool pattern for a directory and descendants."""
    rendered = path.as_posix().lstrip("/")
    if not rendered:
        raise ValueError("benchmark evidence root cannot be the filesystem root")
    return f"//{rendered}/**"


def _claude_evidence_isolation_settings(
    cwd: Path,
    *,
    evidence_roots: Sequence[Path],
    runtime_paths: Sequence[Path],
) -> dict[str, object]:
    """Build Claude settings that deny benchmark evidence to file tools and sandboxed Bash.

    Claude ``Read`` denies block built-in file tools, including Grep and Glob. Its native sandbox applies ``denyRead``
    to Bash and every Bash child process; strict mode prevents the documented unsandboxed retry from reopening those
    paths.
    """
    try:
        worktree = cwd.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Claude worktree is unavailable for evidence isolation: {cwd}") from exc
    allowed_paths = [worktree]
    for runtime_path in runtime_paths:
        try:
            resolved = runtime_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Claude assigned runtime is unavailable: {runtime_path}") from exc
        if resolved not in allowed_paths:
            allowed_paths.append(resolved)

    denied_paths: list[Path] = []
    for root in evidence_roots:
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Claude evidence root is unavailable: {root}") from exc
        if resolved == Path(resolved.anchor):
            raise ValueError("Claude evidence root cannot be the filesystem root")
        if resolved not in denied_paths:
            denied_paths.append(resolved)
    if not denied_paths:
        raise ValueError("Claude evidence isolation requires at least one denied root")
    for allowed_path in allowed_paths:
        for denied_path in denied_paths:
            if allowed_path == denied_path or allowed_path.is_relative_to(denied_path):
                raise ValueError("Claude worktree or assigned runtime cannot be inside a denied evidence root")
            if denied_path.is_relative_to(allowed_path):
                raise ValueError("Claude evidence root cannot be inside an allowed worktree or runtime path")

    filetool_denies = [f"Read({_absolute_filetool_pattern(root)})" for root in denied_paths]
    return {
        "permissions": {"deny": filetool_denies},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "disabled": False,
                "denyRead": [str(path) for path in denied_paths],
                "allowRead": [str(path) for path in allowed_paths],
            },
        },
    }


@contextlib.contextmanager
def _claude_evidence_settings_file(
    cwd: Path,
    *,
    evidence_roots: Sequence[Path],
    runtime_paths: Sequence[Path],
) -> Iterator[Path]:
    """Write one short-lived Claude settings file for a single isolated cell."""
    descriptor, name = tempfile.mkstemp(prefix="claude-benchmark-isolation-", suffix=".json")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                _claude_evidence_isolation_settings(
                    cwd,
                    evidence_roots=evidence_roots,
                    runtime_paths=runtime_paths,
                ),
                handle,
                sort_keys=True,
            )
            handle.write("\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


@contextlib.contextmanager
def _staged_codemap_runtime(arm: str, cwd: Path) -> Iterator[Path | None]:
    """Copy a structural-arm runtime outside evaluator evidence before Claude starts.

    The checked-out plugin is benchmark evidence because it lives under the evaluator checkout. Claude must load a
    disposable runtime copy instead of depending on a nested sandbox allow-list exception that conflicts with the
    evidence deny.
    """
    if arm not in ("codemap", "combined", "B_auto", "C_strict"):
        yield None
        return
    # Deferred: ``runner`` imports this module for its evidence helpers, so importing it at module
    # level would close a cycle. Resolving the class here also keeps the fixture lookup patchable,
    # which tests rely on.
    from _bench_claude.agentic.runner import ModelRunner

    source_text = ModelRunner._codemap_plugin_dir()
    if source_text is None:
        raise RuntimeError(f"Codemap plugin fixture is required for {arm} but is unavailable")
    source = Path(source_text).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="claude-codemap-runtime-", dir=cwd.parent) as runtime_dir:
        runtime = Path(runtime_dir) / "codemap-py"
        shutil.copytree(
            source,
            runtime,
            symlinks=True,
            ignore=shutil.ignore_patterns(".cache", ".plans", ".reports", ".pytest_cache", "__pycache__", "tests"),
        )
        yield runtime
