"""Execute an approved plan, journalling each mutation so it can be rolled back."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from codemap_py import index_paths

# Reached through the module, not a bound name: tests patch these on the defining
# module (monkeypatch.setattr(integration.native, ...)), which a `from .native import`
# binding here would not see.
from . import native
from .managed_block import _managed_block_status, _mutate_content
from .native import _installed_version_lookup, _run_native
from .types import (
    _EXIT_OK,
    MARKETPLACE_NAME,
    ApprovalError,
    ConsumerTarget,
    IntegrationError,
    RefusalError,
    _cli_for,
    _find_target,
)
from .util import (
    _GIT_TIMEOUT_S,
    _SHA256_RE,
    _manifest_for,
    _report_dir,
    _sha256_bytes,
    _sha256_file,
    _utc_now_iso,
    compute_plan_sha256,
)


class Journal:
    """Append-only per-run journal and before-image store.

    Lives under a task-specific ``.reports/integrate/<ts>/`` directory — never inside a plugin cache — and is the
    durable record consulted when a mutation stops partway (state ``recovery-required``).
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / "journal.jsonl"

    def record(self, state: str, *, index: int | None = None, detail: dict | None = None) -> None:
        """Append one journal entry (``planned``/``approved``/``applying``/.../``complete``)."""
        entry = {"ts": _utc_now_iso(), "state": state, "index": index, "detail": detail or {}}
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def save_before_image(self, index: int, data: bytes) -> Path:
        """Persist *data* as the pre-mutation image for op *index*; return its path."""
        before_dir = self.directory / "before"
        before_dir.mkdir(parents=True, exist_ok=True)
        path = before_dir / f"{index}.bak"
        path.write_bytes(data)
        return path


def load_plan(path: Path) -> dict:
    """Load and structurally validate a saved plan artifact.

    Raises:
        IntegrationError: the file is missing, unreadable, or missing required keys (exit ``1``).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrationError("plan_unreadable", f"cannot read plan artifact: {exc}") from exc
    required = {"schema_version", "op_id", "ops", "plan_sha256"}
    if not isinstance(data, dict) or not required.issubset(data):
        raise IntegrationError("plan_malformed", "plan artifact is missing required fields")
    return data


def verify_approval(plan: dict, approve: str) -> None:
    """Bind ``--approve`` to the plan's own recomputed SHA-256.

    Raises:
        ApprovalError: *approve* is not 64 lowercase hex characters, or does not match the
            plan's recomputed digest (exit ``2``).
    """
    if not _SHA256_RE.match(approve):
        raise ApprovalError("approve_malformed", "--approve must be a 64-hex sha256")
    if compute_plan_sha256(plan) != approve:
        raise ApprovalError("approve_mismatch", "--approve does not match this plan's SHA-256")


def _is_contained(path: Path, base: Path) -> bool:
    """Return whether *path* is *base* or a descendant using their path components.

    This lexical test does not resolve symlinks or ``..`` components and does
    not touch the filesystem; callers must establish any physical containment.

    Examples:
        >>> _is_contained(Path('repo/file.py'), Path('repo'))
        True
        >>> _is_contained(Path('other/file.py'), Path('repo'))
        False
    """
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _is_installed_cache_path(path: Path) -> bool:
    """Return True when *path* resolves under any ``.../plugins/cache/...`` tree."""
    parts = path.parts
    return any(parts[i] == "plugins" and parts[i + 1] == "cache" for i in range(len(parts) - 1))


def _git_dirty(root: Path, rel_path: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            ["git", "status", "--porcelain", "--", rel_path],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(result.stdout.strip())


def _refuse_if(condition: bool, code: str, message: str) -> None:
    if condition:
        raise RefusalError(code, message)


def _refuse_unverified_identity(target: ConsumerTarget, root: Path) -> None:
    manifest = _manifest_for(target, root)
    _refuse_if(
        not isinstance(manifest, dict) or manifest.get("name") != target.consumer,
        "unverified_product_identity",
        "consumer manifest name does not match the plan's target",
    )


def _classify_mutation(op: dict, target_path: Path) -> str:
    """Decide ``"insert"`` | ``"replace"`` | ``"noop"`` for one op's enclosing file, right now.

    Runs the structural foreign/modified check on whatever managed block currently exists
    (independent of the plan's own bookkeeping), then layers idempotency and drift on top:
    a file already matching the plan's ``expected_post_state`` hash is a safe no-op re-apply,
    not drift; anything matching neither the plan's ``before_hash`` nor its post-state hash is
    drift, as is a first_time/update expectation that no longer matches reality.

    Raises:
        RefusalError: ``foreign_or_modified_marker`` or ``drift``.
    """
    current_bytes = target_path.read_bytes() if target_path.is_file() else None
    current_text = current_bytes.decode("utf-8") if current_bytes is not None else ""
    status = _managed_block_status(current_text)
    _refuse_if(
        status == "foreign_or_modified", "foreign_or_modified_marker", "existing managed block failed integrity check"
    )
    current_hash = _sha256_bytes(current_bytes) if current_bytes is not None else None
    if current_hash == op["expected_post_state"]["hash"]:
        return "noop"
    _refuse_if(
        current_hash != op["before_hash"], "drift", "target changed since the plan was made; approval invalidated"
    )
    action = "insert" if op["first_time"] else "replace"
    _refuse_if(
        action == "replace" and status == "absent",
        "drift",
        "expected an existing managed block; sentinel is now missing",
    )
    _refuse_if(action == "insert" and status != "absent", "drift", "expected no managed block yet; one now exists")
    return action


def _validate_source_write(op: dict, root: Path) -> tuple[Path, str]:
    """Revalidate one ``source_write`` op immediately before mutating.

    Returns:
        ``(resolved_path, action)`` — *action* is ``"insert"``, ``"replace"``, or ``"noop"``
        (see :func:`_classify_mutation`).

    Raises:
        RefusalError: path escape, symlink, installed-cache root, dirty overlap, unverified
            product identity, a foreign/modified existing managed block, or drift.
    """
    target = _find_target(op["runtime"], op["consumer"])
    plugin_dir = (root / target.plugin_dir).resolve()
    target_path = (root / op["path"]).resolve()
    _refuse_if(_is_installed_cache_path(target_path), "installed_cache_root", "target resolves under a plugin cache")
    _refuse_if(not _is_contained(target_path, plugin_dir), "path_escape", "target escapes its consumer directory")
    _refuse_if(
        (root / op["path"]).is_symlink() or plugin_dir.is_symlink(), "symlink_target", "target path traverses a symlink"
    )
    _refuse_if(_git_dirty(root, op["path"]), "dirty_overlap", "target has uncommitted changes; refusing to overlay")
    _refuse_unverified_identity(target, root)
    return target_path, _classify_mutation(op, target_path)


def _atomic_write(path: Path, content: str | bytes) -> None:
    # Write raw bytes in binary mode: the managed block is self-authenticating (its embedded
    # sha256 stamps its exact body), so the on-disk bytes must equal ``content`` UTF-8-encoded
    # with LF line endings on every OS. Text mode translates ``\n`` to ``\r\n`` on Windows,
    # which would break the post-write hash check and corrupt the marker's integrity stamp.
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _apply_one(op: dict, root: Path, journal: Journal) -> None:
    target_path, action = _validate_source_write(op, root)
    journal.record("applying", index=op["index"])
    if action == "noop":
        journal.record("verified", index=op["index"], detail={"noop": True})
        return
    original_text = ""
    if target_path.is_file():
        original_bytes = target_path.read_bytes()
        journal.save_before_image(op["index"], original_bytes)
        original_text = original_bytes.decode("utf-8")
    _atomic_write(target_path, _mutate_content(original_text, op["new_block"], action))
    actual_hash = _sha256_file(target_path)
    if actual_hash != op["expected_post_state"]["hash"]:
        raise IntegrationError("post_state_mismatch", "write completed but the post-write hash does not match the plan")
    journal.record("verified", index=op["index"])


def _rollback_source_writes(applied: list[dict], root: Path, journal: Journal) -> str:
    ok = True
    for op in reversed(applied):
        before_path = journal.directory / "before" / f"{op['index']}.bak"
        target_path = root / op["path"]
        try:
            if before_path.is_file():
                _atomic_write(target_path, before_path.read_bytes())
            else:
                target_path.unlink(missing_ok=True)
            if _sha256_file(target_path) != op["before_hash"]:
                ok = False
        except OSError:
            ok = False
    return "rollback-succeeded" if ok else "rollback-failed"


def _recovery_commands_source(applied: list[dict], journal: Journal) -> list[str]:
    commands = []
    for op in applied:
        before = journal.directory / "before" / f"{op['index']}.bak"
        commands.append(f"cp {before} {op['path']}")
    return commands


def _handle_apply_failure(op: dict, applied: list[dict], root: Path, journal: Journal, exc: IntegrationError) -> None:
    """Stop, roll back any already-applied targets, and re-raise a bounded error."""
    journal.record("stopped", index=op["index"], detail={"code": exc.code, "message": str(exc)})
    if not applied:
        raise exc
    journal.record("rollback-started")
    state = _rollback_source_writes(applied, root, journal)
    journal.record(state)
    detail = {
        "state": state,
        "failed_index": op["index"],
        "applied": [a["index"] for a in applied],
        "journal": str(journal.directory),
    }
    if state == "rollback-failed":
        detail["recovery_commands"] = _recovery_commands_source(applied, journal)
        raise IntegrationError("recovery_required", "rollback failed; manual recovery required", detail=detail)
    raise IntegrationError(exc.code, f"{exc}; rolled back {len(applied)} prior target(s)", detail=detail)


def apply_plan(plan: dict, approve: str, plugin_root: Path, journal_dir: Path | None = None) -> dict:
    """Execute every ``source_write`` op in *plan*.

    Args:
        plan: A plan artifact as returned by :func:`load_plan` / :func:`build_plan`.
        approve: The plan's own SHA-256, as shown to the user.
        plugin_root: codemap-py's own resolved plugin root (unused by mutation, kept for
            call-site symmetry with the other ``cmd_*`` entry points).
        journal_dir: Explicit journal directory (tests only); defaults to a fresh
            ``.reports/integrate/<ts>/``.

    Returns:
        ``{"state": "complete", "applied": [...], "journal": "..."}`` on success.

    Raises:
        ApprovalError: bad or mismatched ``--approve`` (exit ``2``).
        IntegrationError: any refusal, drift, or unrecoverable failure (exit ``1``).
    """
    del plugin_root  # kept for signature symmetry with cmd_sync/cmd_demo; mutation is source-relative
    verify_approval(plan, approve)
    root = index_paths.canonical_root()
    ops = [op for op in plan["ops"] if op["kind"] == "source_write"]
    journal = Journal(journal_dir or _report_dir(root))
    journal.record("approved", detail={"op_id": plan["op_id"]})
    applied: list[dict] = []
    for op in ops:
        try:
            _apply_one(op, root, journal)
        except IntegrationError as exc:
            _handle_apply_failure(op, applied, root, journal, exc)
        applied.append(op)
    journal.record("complete")
    return {"state": "complete", "applied": [op["index"] for op in applied], "journal": str(journal.directory)}


def cmd_apply(ns: argparse.Namespace, plugin_root: Path) -> int:
    """Run ``integrate apply``; return exit ``0`` (failures raise and are caught by :func:`run`)."""
    plan = load_plan(Path(ns.plan).expanduser())
    result = apply_plan(plan, ns.approve, plugin_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return _EXIT_OK


def _validate_runtime_sync(op: dict) -> None:
    """Revalidate one ``runtime_sync`` op's before-state immediately before executing it."""
    if op["role"] == "marketplace":
        return  # a marketplace refresh has no single before-hash worth redrift-checking
    cli = _cli_for(op["runtime"])
    installed_state = native._native_json_probe([cli, "plugin", "list", "--json"])
    current = _installed_version_lookup(op["runtime"])(op["consumer"], installed_state)
    _refuse_if(current != op["before_hash"], "drift", "installed state changed since the plan was made")


def _verify_plugin_installed(op: dict) -> None:
    cli = _cli_for(op["runtime"])
    installed_state = native._native_json_probe([cli, "plugin", "list", "--json"])
    if _installed_version_lookup(op["runtime"])(op["consumer"], installed_state) is None:
        raise IntegrationError("post_state_mismatch", f"{op['consumer']} is not enabled after sync")


def _sync_one(op: dict, journal: Journal) -> None:
    _validate_runtime_sync(op)
    journal.record("applying", index=op["index"])
    for argv in op["argv"]:
        native._run_native_required(argv)
    if op["role"] == "plugin":
        _verify_plugin_installed(op)
    journal.record("verified", index=op["index"])


def _rollback_runtime_sync(applied: list[dict], journal: Journal) -> str:
    del journal  # native rollback has no before-image to restore from; kept for signature symmetry
    ok = True
    for op in reversed(applied):
        if op["role"] != "plugin" or op["rollback"]["kind"] != "reinstall_previous":
            continue  # marketplace refreshes and first-installs are not reverted automatically
        cli = _cli_for(op["runtime"])
        verb = "install" if cli == "claude" else "add"
        argv = [cli, "plugin", verb, f"{op['consumer']}@{MARKETPLACE_NAME}"]
        try:
            completed = _run_native(argv)
            ok = ok and completed.returncode == 0
        except IntegrationError:
            ok = False
    return "rollback-succeeded" if ok else "rollback-failed"


def _recovery_commands_sync(applied: list[dict]) -> list[str]:
    return [
        f"manually verify/reinstall: {op['consumer'] or 'marketplace'} ({op['rollback']['kind']})" for op in applied
    ]


def _handle_sync_failure(op: dict, applied: list[dict], journal: Journal, exc: IntegrationError) -> None:
    """Stop, best-effort roll back already-synced targets, and re-raise."""
    journal.record("stopped", index=op["index"], detail={"code": exc.code, "message": str(exc)})
    if not applied:
        raise exc
    journal.record("rollback-started")
    state = _rollback_runtime_sync(applied, journal)
    journal.record(state)
    detail = {
        "state": state,
        "failed_index": op["index"],
        "applied": [a["index"] for a in applied],
        "journal": str(journal.directory),
    }
    if state == "rollback-failed":
        detail["recovery_commands"] = _recovery_commands_sync(applied)
        raise IntegrationError("recovery_required", "rollback failed; manual recovery required", detail=detail)
    raise IntegrationError(exc.code, f"{exc}; rolled back {len(applied)} prior target(s)", detail=detail)


def sync_plan(plan: dict, approve: str, source: str, plugin_root: Path, journal_dir: Path | None = None) -> dict:
    """Execute every ``runtime_sync`` op in *plan*.

    Never mutates consumer source or global instructions — only the ordered native
    plugin-manager argv the plan already recorded.

    Args:
        plan: A plan artifact as returned by :func:`load_plan` / :func:`build_plan`.
        approve: The plan's own SHA-256, as shown to the user.
        source: ``"local-candidate"`` or ``"release"``; must match the plan's recorded source.
        plugin_root: codemap-py's own resolved plugin root (kept for call-site symmetry).
        journal_dir: Explicit journal directory (tests only).

    Raises:
        ApprovalError: bad ``--approve``, or *source* does not match the plan (exit ``2``).
        IntegrationError: any drift, native-command failure, or unrecoverable failure (exit ``1``).
    """
    del plugin_root
    verify_approval(plan, approve)
    if plan.get("source") != source:
        raise ApprovalError("source_mismatch", "--source does not match the plan's recorded source")
    root = index_paths.canonical_root()
    ops = [op for op in plan["ops"] if op["kind"] == "runtime_sync"]
    journal = Journal(journal_dir or _report_dir(root))
    journal.record("approved", detail={"op_id": plan["op_id"]})
    applied: list[dict] = []
    for op in ops:
        try:
            _sync_one(op, journal)
        except IntegrationError as exc:
            _handle_sync_failure(op, applied, journal, exc)
        applied.append(op)
    journal.record("complete")
    return {"state": "complete", "applied": [op["index"] for op in applied], "journal": str(journal.directory)}


def cmd_sync(ns: argparse.Namespace, plugin_root: Path) -> int:
    """Run ``integrate sync``; return exit ``0`` (failures raise and are caught by :func:`run`)."""
    plan = load_plan(Path(ns.plan).expanduser())
    result = sync_plan(plan, ns.approve, ns.source, plugin_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return _EXIT_OK
