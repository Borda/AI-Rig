#!/usr/bin/env python3
"""audit_static.py — deterministic Layer-1 pass for /foundry:audit.

Runs every mechanical (zero-LLM) config checker over a scope and aggregates the
results into one reproducible report. This is the deterministic layer the audit
skill consumes before spawning any judgment agents: the same driver runs in
pre-commit/CI, so the LLM audit rarely re-discovers a mechanical defect.

Each registered check is one of three kinds:
- ``scan``  — invoked as ``<script> --scan-dir <scope>``
- ``files`` — invoked with a globbed list of files under <scope> (skipped if none)
- ``whole`` — invoked with no scope (the checker walks the repo itself)

A check "fails" when its subprocess exits non-zero; its finding lines are the
non-empty stdout lines that are not a ``✓`` success line.

Usage:
    audit_static.py [--scan-dir plugins] [--jsonl <path>]

Exit code 0 when every check passes, 1 when any check reports findings,
2 on setup error (missing checker).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

#: Directory of the check scripts, resolved from this file so registry script names map to real paths.
BIN = Path(__file__).resolve().parent

#: Registry of deterministic checks. `script` is a bin/ filename; `kind` selects
#: how the scope is passed. `globs` (files kind) are patterns relative to scope.
CHECKS: list[dict[str, object]] = [
    {
        "id": "tag-symmetry",
        "kind": "files",
        "script": "check_tag_symmetry.py",
        "globs": ["*/agents/*.md", "*/skills/*/SKILL.md"],
    },
    {"id": "fence-symmetry", "kind": "files", "script": "check_fence_symmetry.py", "globs": ["**/*.md"]},
    {"id": "readme-drift", "kind": "scan", "script": "check_readme_drift.py"},
    {"id": "mode-dispatch", "kind": "scan", "script": "check_mode_dispatch.py"},
    {"id": "bash-persistence", "kind": "scan", "script": "check_bash_persistence.py"},
    {"id": "spawn-prompt-vars", "kind": "scan", "script": "check_spawn_prompt_vars.py"},
    {"id": "plugin-module-docs", "kind": "scan", "script": "check_plugin_module_docs.py"},
    {"id": "routing-links", "kind": "whole", "script": "check_routing_links.py"},
    {"id": "orphaned-bin", "kind": "whole", "script": "check_orphaned_bin.py"},
    {"id": "cli-flag-drift", "kind": "whole", "script": "check_cli_flag_drift.py"},
    {"id": "shared-drift", "kind": "whole", "script": "propagate_shared.py"},
    {"id": "codemap-guard", "kind": "whole", "script": "check_codemap_guard.py"},
    {"id": "agent-waits", "kind": "scan", "script": "check_agent_waits.py"},
]


def _argv(check: dict[str, object], scope: Path) -> list[str] | None:
    """Build the subprocess argv for a check, or None to skip (files kind, no matches).

    Args:
        check: A CHECKS registry entry.
        scope: Scope directory (for scan/files kinds).

    Returns:
        The argv list, or None when a files-kind check has no matching files.
    """
    script = str(BIN / str(check["script"]))
    kind = check["kind"]
    if kind == "scan":
        return [sys.executable, script, "--scan-dir", str(scope)]
    if kind == "whole":
        return [sys.executable, script]
    files: list[str] = []
    for pattern in check["globs"]:  # type: ignore[union-attr]
        files.extend(str(p) for p in sorted(scope.glob(str(pattern))))
    return [sys.executable, script, *files] if files else None


def _findings(stdout: str) -> list[str]:
    """Return the finding lines from a checker's stdout (drop blanks and ✓ lines)."""
    return [ln for ln in stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("✓")]


def run_checks(scope: Path) -> list[dict[str, object]]:
    """Run every registered check over scope and return per-check result dicts.

    Args:
        scope: Scope directory passed to scan/files checks.

    Returns:
        One result dict per check: ``{check, status, findings, lines}``.
        ``status`` is ``pass``, ``fail``, ``skipped`` (no files), or ``error`` (missing script).
    """
    results: list[dict[str, object]] = []
    for check in CHECKS:
        if not (BIN / str(check["script"])).is_file():
            results.append(
                {
                    "check": check["id"],
                    "status": "error",
                    "findings": 0,
                    "lines": [f"missing checker: {check['script']}"],
                }
            )
            continue
        argv = _argv(check, scope)
        if argv is None:
            results.append({"check": check["id"], "status": "skipped", "findings": 0, "lines": []})
            continue
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell; fixed checker commands
                argv, capture_output=True, text=True, timeout=120, env=env
            )
        except subprocess.TimeoutExpired:
            # A runaway checker must not crash the whole audit (breaks the 0/1/2
            # exit contract) — record it as an errored row and keep going.
            results.append(
                {
                    "check": check["id"],
                    "status": "error",
                    "findings": 0,
                    "lines": [f"checker timed out after 120s: {check['script']}"],
                }
            )
            continue
        # Findings come from stdout only; a passing check has none by definition
        # (stderr carries benign runtime warnings, not audit findings).
        lines = _findings(proc.stdout) if proc.returncode != 0 else []
        status = "pass" if proc.returncode == 0 else "fail"
        results.append({"check": check["id"], "status": status, "findings": len(lines), "lines": lines})
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Deterministic Layer-1 static audit pass")
    parser.add_argument("--scan-dir", default="plugins", help="scope directory (default: plugins)")
    parser.add_argument("--jsonl", help="write one JSON result object per check to this file")
    args = parser.parse_args(argv)

    scope = Path(args.scan_dir)
    if not scope.is_dir():
        sys.stderr.write(f"audit_static: scope not found: {scope}\n")
        return 2

    results = run_checks(scope)
    if args.jsonl:
        Path(args.jsonl).write_text("".join(json.dumps(r) + "\n" for r in results), encoding="utf-8")

    failed = 0
    for r in results:
        if r["status"] == "fail":
            failed += 1
            print(f"⚠ {r['check']}: {r['findings']} finding(s)")
            for ln in r["lines"][:20]:
                print(f"    {ln}")
        elif r["status"] == "error":
            print(f"! {r['check']}: {r['lines'][0]}")
        elif r["status"] == "skipped":
            print(f"– {r['check']}: skipped (no files in scope)")
        else:
            print(f"✓ {r['check']}")
    total = sum(int(r["findings"]) for r in results)  # type: ignore[arg-type]
    print(f"\nLayer-1 static audit: {failed} check(s) with findings, {total} finding(s) total over {scope}/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
