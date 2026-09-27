"""Run the demonstration walkthrough of a query against the live index."""

from __future__ import annotations
import argparse
import contextlib
import json
from io import StringIO
from pathlib import Path
from codemap_py import index_paths, query, rwgate
from .audit import build_audit_report
from .managed_block import PROTOCOL_VERSION
from .types import Runtime, _EXIT_OK, _EXIT_RUNTIME
from .util import _report_dir


def _demo_query(identity: index_paths.IndexIdentity) -> dict:
    if not identity.index_path.is_file():
        return {"ran": False, "reason": "no index built yet"}
    capture = StringIO()
    try:
        # No lease here: query.main takes its own read lease around the load
        # (codemap_py.query._load_index_leased). Wrapping it in a second one would
        # parse the whole index twice per demo and re-introduce the caller-side
        # leasing the gate's module docstring forbids.
        with contextlib.redirect_stdout(capture):
            query.main(["central", "--top", "3", "--root", str(identity.root)])
        return {"ran": True, "output": capture.getvalue()[:2048]}
    except (rwgate.IndexBusy, rwgate.CoordinationUnavailable) as exc:
        return {"ran": False, "reason": str(exc)}
    except SystemExit as exc:
        return {"ran": False, "reason": f"query exited with {exc.code}"}


def run_demo(runtime: Runtime | str, plugin_root: Path) -> dict:
    """Run ``audit`` plus one representative structural-context query.

    Disposable evidence only — writes its JSON result under a fresh ``.reports/integrate/<ts>/`` directory and never
    mutates plan/approval state.
    """
    root = index_paths.canonical_root()
    identity = index_paths.resolve_index(root=root)
    demo = {
        "protocol": PROTOCOL_VERSION,
        "scope": "structural_smoke",
        "comparison": "not_performed",
        "token_measurement": {"status": "unavailable", "reason": "no model usage collected"},
        "audit": build_audit_report(runtime, plugin_root),
        "query_evidence": _demo_query(identity),
    }
    demo_dir = _report_dir(root)
    (demo_dir / "demo.json").write_text(json.dumps(demo, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    demo["report_path"] = str(demo_dir / "demo.json")
    return demo


def cmd_demo(ns: argparse.Namespace, plugin_root: Path) -> int:
    """Run ``integrate demo``; return ``0`` on a completed run, ``1`` when the query itself failed."""
    demo = run_demo(ns.runtime, plugin_root)
    print(json.dumps(demo, indent=2, sort_keys=True))
    if demo["query_evidence"]["ran"] is False and demo["query_evidence"]["reason"] != "no index built yet":
        return _EXIT_RUNTIME
    return _EXIT_OK
