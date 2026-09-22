"""Diagnose locally installed bridge command compatibility.

Purpose: Check that the installed Codex and Claude command help surfaces still contain every bridge flag required by the
portable supervisor and bind the doctor to one complete installed Bridge payload. Scope: The normal mode reads the
plugin's pinned baseline and manifests, hashes required runtime/setup files, invokes only ``--help``, and emits one JSON
object describing missing commands, flags, or payload members. The optional live mode is an explicit operator probe that
sends a minimal structured request through the shared bridge core; it is never enabled by default. Usage: Run
``bridge_diagnose.py --direction both --workspace .`` for the free static check or add ``--live`` after obtaining
authority for a paid authenticated request. Outputs: The command prints one JSON object and appends static or live
findings to the workspace-local health log only when a full bridge request is performed. Failure: Missing executables,
nonzero help commands, malformed baselines, and missing required tokens are reported as findings rather than raising
tracebacks. Used by: The bridge setup skill and maintainers debugging a host CLI upgrade. The module has no network side
effect itself unless the caller explicitly opts into live mode, and it uses standard-library subprocess handling
throughout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import deque
from pathlib import Path
import subprocess
import sys
from typing import Any

# Keep sibling imports valid when repository-wide doctest collection imports this
# file without launching it as a script from its installed ``bin`` directory.
_BIN_DIRECTORY = str(Path(__file__).resolve().parent)
if _BIN_DIRECTORY not in sys.path:
    sys.path.insert(0, _BIN_DIRECTORY)

from bridge_call import BridgePaths, DEFAULT_EFFORT, DEFAULT_MODEL, DEFAULT_TIMEOUTS, Request, run_request  # noqa: E402


def diagnose(direction: str, workspace: Path, live: bool) -> dict[str, Any]:
    """Run static help checks and optionally one bounded live request per host.

    Payload completeness is checked before the baseline loads: a truncated
    install may be missing the baseline file itself, and that case must still
    produce a structured result naming the missing payload members instead of
    a bare error object.
    """
    targets = ("codex", "claude") if direction == "both" else (direction,)
    payload = _installed_payload_identity()
    if not payload["complete"]:
        return {
            "direction": direction,
            "live": live,
            "ok": False,
            "findings": [],
            "payload": payload,
            "health": _health_summary(workspace),
        }
    baseline = _load_baseline()
    findings = [_static_result(target, baseline[target]) for target in targets]
    if live:
        findings.extend(_live_result(target, workspace) for target in targets)
    return {
        "direction": direction,
        "live": live,
        "ok": payload["complete"] and all(item["ok"] for item in findings),
        "findings": findings,
        "payload": payload,
        "health": _health_summary(workspace),
    }


PAYLOAD_FILES = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".codex-mcp.json",
    "bin/bridge_call.py",
    "bin/bridge_diagnose.py",
    "bin/bridge_mcp.py",
    "bin/bridge_setup.py",
    "rules/cli-baseline.json",
    "schemas/envelope.schema.json",
    "schemas/harness-envelope.schema.json",
    "schemas/mcp-tools.schema.json",
    "schemas/setup-result.schema.json",
)


def _installed_payload_identity() -> dict[str, Any]:
    """Return a sanitized identity and digest for the installed runtime closure."""
    root = Path(__file__).resolve().parents[1]
    missing = [relative for relative in PAYLOAD_FILES if not _regular_payload_file(root / relative)]
    if missing:
        return {"name": "bridge", "version": "unknown", "complete": False, "fingerprint": None, "missing": missing}
    try:
        claude = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        codex = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "name": "bridge",
            "version": "unknown",
            "complete": False,
            "fingerprint": None,
            "missing": ["valid host manifests"],
        }
    version = claude.get("version") if isinstance(claude, dict) else None
    if (
        not isinstance(codex, dict)
        or claude.get("name") != "bridge"
        or codex.get("name") != "bridge"
        or not isinstance(version, str)
        or codex.get("version") != version
    ):
        return {
            "name": "bridge",
            "version": "unknown",
            "complete": False,
            "fingerprint": None,
            "missing": ["matching host manifest identity"],
        }
    digest = hashlib.sha256()
    for relative in PAYLOAD_FILES:
        normalized = Path(relative).as_posix().encode("utf-8")
        digest.update(len(normalized).to_bytes(4, "big"))
        digest.update(normalized)
        digest.update((root / relative).read_bytes())
    return {"name": "bridge", "version": version, "complete": True, "fingerprint": digest.hexdigest(), "missing": []}


def _regular_payload_file(path: Path) -> bool:
    """Accept only regular, non-symlink payload members."""
    return path.is_file() and not path.is_symlink()


MAX_HEALTH_LINES = 5_000


def _health_summary(workspace: Path) -> dict[str, Any]:
    """Summarize retained outcome counts, recent faults, and grouped reported cost.

    The log grows one line per bridge call and is never rotated by the bridge, so the summary reads a bounded tail
    rather than the complete history.
    """
    path = BridgePaths(workspace).root / "health.jsonl"
    fault_counts: dict[str, int] = {}
    latest_fault: dict[str, float] = {}
    cost_rollup: dict[str, float] = {}
    if not path.is_file():
        return {"fault_counts": fault_counts, "latest_fault": latest_fault, "cost_rollup": cost_rollup}
    with path.open(encoding="utf-8") as stream:
        lines = deque(stream, maxlen=MAX_HEALTH_LINES)
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if status in {"blocked", "timeout", "refused"}:
            fault_counts[status] = fault_counts.get(status, 0) + 1
            timestamp = record.get("ts")
            if isinstance(timestamp, (int, float)):
                latest_fault[status] = max(latest_fault.get(status, float(timestamp)), float(timestamp))
        cost = record.get("cost")
        if isinstance(cost, (int, float)):
            key = "/".join(str(record.get(field, "unknown")) for field in ("direction", "verb", "model"))
            cost_rollup[key] = cost_rollup.get(key, 0.0) + float(cost)
    return {"fault_counts": fault_counts, "latest_fault": latest_fault, "cost_rollup": cost_rollup}


def _load_baseline() -> dict[str, dict[str, list[str]]]:
    """Load the installed plugin baseline without depending on the current directory."""
    path = Path(__file__).resolve().parents[1] / "rules" / "cli-baseline.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"codex", "claude"}:
        raise ValueError("invalid CLI baseline")
    for target, entry in value.items():
        if not isinstance(entry, dict) or set(entry) != {"required"}:
            raise ValueError(f"invalid CLI baseline entry for {target}")
        required = entry["required"]
        if (
            not isinstance(required, list)
            or not required
            or not all(isinstance(token, str) and token for token in required)
        ):
            raise ValueError(f"invalid CLI baseline required tokens for {target}")
    return value


def _static_result(target: str, baseline: dict[str, list[str]]) -> dict[str, Any]:
    """Check one host's required help tokens without starting an agent request."""
    executable = "codex" if target == "codex" else "claude"
    command = [executable, "exec", "--help"] if target == "codex" else [executable, "--help"]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "target": target,
            "mode": "static",
            "ok": False,
            "missing": list(baseline["required"]),
            "error": str(error),
        }
    text = result.stdout + "\n" + result.stderr
    missing = [token for token in baseline["required"] if not _token_present(token, text)]
    return {
        "target": target,
        "mode": "static",
        "ok": result.returncode == 0 and not missing,
        "missing": missing,
        "returncode": result.returncode,
    }


def _token_present(token: str, text: str) -> bool:
    """Match a required command or flag on a word boundary, not as a substring.

    A bare substring scan would keep reporting ``exec`` present inside ``execute`` long after the real subcommand
    disappeared from the help text.
    """
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text) is not None


def _live_result(target: str, workspace: Path) -> dict[str, Any]:
    """Run the smallest explicit paid probe only after the caller passed ``--live``."""
    direction = "claude_to_codex" if target == "codex" else "codex_to_claude"
    request = Request(
        "advise",
        "Return a JSON result that reports completion.",
        DEFAULT_MODEL,
        DEFAULT_EFFORT,
        DEFAULT_TIMEOUTS["advise"],
        0,
        "diagnostic",
        workspace.resolve(),
        direction,
    )
    result = run_request(request, host=target)
    return {
        "target": target,
        "mode": "live",
        "ok": result["status"] in {"complete", "partial"},
        "status": result["status"],
        "result": result,
    }


def main(argv: list[str] | None = None) -> int:
    """Parse the stable diagnostics CLI and emit exactly one JSON object."""
    parser = argparse.ArgumentParser(description="Check bridge host command compatibility.")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--direction", choices=("codex", "claude", "both"), default="both")
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args(argv)
    try:
        output = diagnose(args.direction, Path(args.workspace), args.live)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        output = {"ok": False, "error": str(error)}
    sys.stdout.write(json.dumps(output, sort_keys=True) + "\n")
    return 0 if output.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
