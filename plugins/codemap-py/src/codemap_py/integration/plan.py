"""Build the plan describing the source writes and runtime syncs a mutation would make."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

from codemap_py import __version__, index_paths

# Reached through the module, not a bound name: tests patch these on the defining
# module (monkeypatch.setattr(integration.native, ...)), which a `from .native import`
# binding here would not see.
from . import native
from .managed_block import (
    PROTOCOL_VERSION,
    _managed_block_body,
    _managed_block_status,
    _mutate_content,
    _render_managed_block,
)
from .native import _installed_version_lookup, _marketplace_entry
from .types import (
    _EXIT_OK,
    CONSUMER_MANAGED_FILE,
    MARKETPLACE_NAME,
    MARKETPLACE_REMOTE,
    PROVIDER_NAME,
    SCHEMA_VERSION,
    ConsumerTarget,
    Runtime,
    Source,
    _cli_for,
    _runtimes_of,
    resolve_targets,
)
from .util import _manifest_for, _report_dir, _sha256_bytes, _sha256_file, _utc_now_iso, compute_plan_sha256


def _source_write_op(index: int, target: ConsumerTarget, root: Path) -> dict:
    """Build one in-file managed-block ``source_write`` op for *target*.

    The target file is an allowlisted, existing-or-creatable path from :data:`CONSUMER_MANAGED_FILE`. Whether this op
    inserts (no sentinel present, including an absent file) or replaces (sentinel already present) is decided here, at
    plan time, from the file's current content — :func:`_classify_mutation` re-derives the same decision at apply time
    and refuses on any disagreement (drift).
    """
    rel_path = f"{target.plugin_dir}/{CONSUMER_MANAGED_FILE[target.consumer]}"
    abs_path = root / rel_path
    before_hash = _sha256_file(abs_path)
    original_text = abs_path.read_text(encoding="utf-8") if abs_path.is_file() else ""
    first_time = _managed_block_status(original_text) == "absent"
    manifest = _manifest_for(target, root)
    version = manifest.get("version") if isinstance(manifest, dict) else None
    new_block = _render_managed_block(_managed_block_body(target.runtime, target.consumer, version))
    mutated = _mutate_content(original_text, new_block, "insert" if first_time else "replace")
    return {
        "index": index,
        "kind": "source_write",
        "runtime": target.runtime,
        "consumer": target.consumer,
        "path": rel_path,
        "first_time": first_time,
        "before_hash": before_hash,
        "desired": {"version": version, "ref": None, "pkg_hash": None},
        "new_block": new_block,
        "argv": [],
        "rollback": {"kind": "restore_file", "identity": before_hash or "absent"},
        "expected_post_state": {"hash": _sha256_bytes(mutated.encode("utf-8"))},
    }


def _marketplace_source(source: Source, root: Path) -> str:
    """Return the marketplace ``add`` source string for *source*."""
    return str(root) if source == Source.LOCAL_CANDIDATE else MARKETPLACE_REMOTE


def _marketplace_sync_op(index: int, runtime: Runtime, source: Source, root: Path) -> dict:
    entry = _marketplace_entry(runtime)
    cli = _cli_for(runtime)
    if entry is None:
        argv = [cli, "plugin", "marketplace", "add", _marketplace_source(source, root)]
    else:
        refresh_verb = "update" if runtime == Runtime.CLAUDE else "upgrade"
        argv = [cli, "plugin", "marketplace", refresh_verb, MARKETPLACE_NAME]
    return {
        "index": index,
        "kind": "runtime_sync",
        "role": "marketplace",
        "runtime": runtime.value,
        "consumer": None,
        "before_hash": None,
        "desired": {"version": None, "ref": source.value, "pkg_hash": None},
        "argv": [argv],
        "rollback": {"kind": "none", "identity": "marketplace refresh is not rolled back independently"},
        "expected_post_state": {"registered": True},
    }


def _plugin_sync_op(index: int, runtime: Runtime, name: str, installed_state: object) -> dict:
    cli = _cli_for(runtime)
    current_version = _installed_version_lookup(runtime)(name, installed_state)
    if runtime == Runtime.CLAUDE:
        argv = (
            [cli, "plugin", "update", name]
            if current_version is not None
            else [cli, "plugin", "install", f"{name}@{MARKETPLACE_NAME}", "--scope", "user"]
        )
    else:
        # No `codex plugin update` verb is assumed unless a tested CLI actually exposes one;
        # `add` is idempotent add-or-refresh for the currently probed codex-cli.
        argv = [cli, "plugin", "add", f"{name}@{MARKETPLACE_NAME}"]
    rollback = (
        {"kind": "reinstall_previous", "identity": current_version}
        if current_version is not None
        else {"kind": "remove_first_install", "identity": "absent"}
    )
    return {
        "index": index,
        "kind": "runtime_sync",
        "role": "plugin",
        "runtime": runtime.value,
        "consumer": name,
        "before_hash": current_version,
        "desired": {"version": None, "ref": None, "pkg_hash": None},
        "argv": [argv],
        "rollback": rollback,
        "expected_post_state": {"installed": True},
    }


def _runtime_sync_ops(
    start_index: int, runtime: Runtime, source: Source, targets: Sequence[ConsumerTarget], root: Path
) -> list[dict]:
    """Return ops for one runtime: one marketplace refresh, then provider-then-consumer installs."""
    ops = [_marketplace_sync_op(start_index, runtime, source, root)]
    cli = _cli_for(runtime)
    installed_state = native._native_json_probe([cli, "plugin", "list", "--json"])
    names = [PROVIDER_NAME, *(t.consumer for t in targets)]
    ops.extend(_plugin_sync_op(start_index + 1 + i, runtime, name, installed_state) for i, name in enumerate(names))
    return ops


def build_plan(
    runtime: Runtime | str, consumers: Sequence[str] | None, source: Source | str | None, plugin_root: Path
) -> dict:
    """Build the unsigned integration plan for *runtime*.

    When *source* is given, the plan also carries ``runtime_sync`` ops (native plugin-manager
    argv, provider-then-consumer order per runtime); omitting it produces a source-only plan
    consumable only by ``apply``, never ``sync``.

    Args:
        runtime: A :class:`Runtime` member, or its plain value from the CLI.
        consumers: Explicit consumer-name subset, or ``None`` for every target in *runtime*.
        source: A :class:`Source` member, its plain value, or ``None`` for a source-only plan.
        plugin_root: codemap-py's own resolved plugin root (recorded, not mutated).

    Raises:
        IntegrationError: an entry in *consumers* is outside the closed target set (exit ``2``).
    """
    runtime = Runtime(runtime)
    source = Source(source) if source is not None else None
    root = index_paths.canonical_root()
    targets = resolve_targets(runtime, consumers)
    ops: list[dict] = [_source_write_op(i, target, root) for i, target in enumerate(targets)]
    if source is not None:
        for one_runtime in _runtimes_of(runtime):
            selected = [t for t in targets if t.runtime == one_runtime]
            ops.extend(_runtime_sync_ops(len(ops), one_runtime, source, selected, root))
    plan = {
        "schema_version": SCHEMA_VERSION,
        "protocol": PROTOCOL_VERSION,
        "op_id": uuid.uuid4().hex,
        "created_at": _utc_now_iso(),
        "runtime": runtime.value,
        "consumers": [t.consumer for t in targets],
        "source": source.value if source is not None else None,
        "provider": {"name": PROVIDER_NAME, "version": __version__, "root": str(plugin_root)},
        "ops": ops,
    }
    plan["plan_sha256"] = compute_plan_sha256(plan)
    return plan


def cmd_plan(ns: argparse.Namespace, plugin_root: Path) -> int:
    """Run ``integrate plan``; write the artifact and print its path + SHA-256."""
    plan = build_plan(ns.runtime, ns.consumers, ns.source, plugin_root)
    root = index_paths.canonical_root()
    out_path = Path(ns.out).expanduser() if ns.out else _report_dir(root) / "plan.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"plan": str(out_path), "plan_sha256": plan["plan_sha256"], "ops": len(plan["ops"])}, indent=2))
    return _EXIT_OK
