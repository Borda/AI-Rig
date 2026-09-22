#!/usr/bin/env python3
"""Derive and synchronize deterministic code-review routing evidence.

## Purpose

Compute the minimum review tier, exact file/line evidence, and mandatory specialist signals from the normalized diff
artifacts produced by Codex Rig. Synchronize those mechanical fields into ``review-routing.json`` so arithmetic and path
classification are never authored from model memory.

## Scope

Read only ``files.txt``, ``untracked.txt``, and ``numstat.txt`` from one code-review run directory, then update only
``mechanical_risk_tier`` and ``mechanical_risk_evidence`` in its existing routing object. Semantic risk signals,
declared risk tier, triggered roles, and their evidence remain reviewer-owned and are preserved value-for-value while
the JSON representation is normalized.

## Usage

Run ``python review_routing.py --out <run-directory>`` after writing the semantic routing decisions and before creating
the specialist manifest. The command is idempotent, so a retry after regenerating diff evidence produces the same
canonical JSON when the inputs are unchanged.

## Used by

The code-review skill invokes this helper during T2 routing, and ``validate_artifacts.py`` imports the same derivation
function during the terminal contract check. Tests exercise the installed-path CLI and validator import independently so
packaging cannot silently separate the producer from the consumer.

## Outputs

Rewrite ``review-routing.json`` with deterministic indentation, sorted keys, and one trailing newline, then print the
updated path. The derived evidence records the unique changed-file count, total numeric additions plus deletions,
unknown-size rows, and any mechanically detected high-risk or configuration paths.

## Failure

Missing or malformed routing JSON, a non-object payload, missing or invalid reviewer ``risk_tier``, unreadable diff
evidence, or an unwritable output path exits non-zero with the underlying local error. The helper never invents semantic
signals or lowers the declared tier, and the final validator still rejects underclassification, incomplete signals, or
inconsistent specialist routing.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _path_tokens(path: str) -> set[str]:
    """Split a repository path into exact lowercase risk tokens.

    Separators are discarded rather than matched as substrings, so ``authentication.py`` yields ``authentication``
    while a path containing ``auth`` does not accidentally match it.

    Example:
        >>> sorted(_path_tokens("src/Auth-Config.py"))
        ['auth', 'config', 'py', 'src']
    """
    return {token for token in re.split(r"[/._-]+", path.lower()) if token}


def derive_mechanical_risk(out_dir: Path) -> tuple[str, list[str], set[str]]:
    """Derive the minimum tier, canonical evidence, and mandatory signals from collected diff facts."""
    paths: set[str] = set()
    for filename in ("files.txt", "untracked.txt"):
        path = out_dir / filename
        if path.exists():
            paths.update(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    changed_lines = 0
    unknown_size_rows = 0
    numstat = out_dir / "numstat.txt"
    if numstat.exists():
        for line in numstat.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t", 2)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                changed_lines += int(parts[0]) + int(parts[1])
            elif len(parts) >= 2:
                unknown_size_rows += 1

    lower_paths = {path.lower() for path in paths}
    evidence = [f"files={len(paths)}", f"changed_lines={changed_lines}", f"unknown_size_rows={unknown_size_rows}"]
    broad_names = {"pyproject.toml", "package.json", "cargo.toml", "uv.lock", "poetry.lock", "package-lock.json"}
    security_parts = {"auth", "authentication", "credential", "credentials", "security"}
    high_risk_parts = security_parts | {"migration", "migrations"}
    high_paths = sorted(
        path
        for path in lower_paths
        if path.startswith(".github/workflows/")
        or high_risk_parts.intersection(_path_tokens(path))
        or "deserial" in Path(path).name
    )
    config_paths = sorted(
        path
        for path in lower_paths
        if path in broad_names or path.endswith(("config.toml", "config.yaml", "config.yml"))
    )
    if high_paths:
        tier = "HIGH_RISK"
        evidence.append("high_risk_paths=" + ",".join(high_paths))
    elif len(paths) >= 8 or config_paths or unknown_size_rows:
        tier = "BROAD"
        if config_paths:
            evidence.append("config_or_dependency_paths=" + ",".join(config_paths))
    elif len(paths) < 3 and changed_lines < 50:
        tier = "TRIVIAL"
    else:
        tier = "LOCAL"

    mandatory_signals: set[str] = set()
    if any(path.startswith("tests/") or "/tests/" in path for path in lower_paths):
        mandatory_signals.add("test_or_error_path")
    if any(any(marker in path for marker in ("tensor", "dataset", "dataloader", "data/")) for path in lower_paths):
        mandatory_signals.update({"data_tensor_boundary", "axis_data_steward"})
    if any(path.startswith(".github/") for path in lower_paths):
        mandatory_signals.add("axis_cicd_steward")
    if any(path.endswith((".md", ".rst")) or path.startswith("docs/") for path in lower_paths):
        mandatory_signals.add("axis_doc_scribe")
    if high_paths and any(
        security_parts.intersection(_path_tokens(path)) or "deserial" in Path(path).name for path in high_paths
    ):
        mandatory_signals.add("axis_security_auditor")
    return tier, evidence, mandatory_signals


def synchronize_routing(out_dir: Path) -> Path:
    """Validate the reviewer tier and replace model-authored mechanical fields with derived evidence."""
    routing_path = out_dir / "review-routing.json"
    payload: Any = json.loads(routing_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {routing_path}")
    if payload.get("risk_tier") not in {"TRIVIAL", "LOCAL", "BROAD", "HIGH_RISK"}:
        raise ValueError(f"invalid-risk-tier:{payload.get('risk_tier')!r}")
    tier, evidence, _ = derive_mechanical_risk(out_dir)
    payload["mechanical_risk_tier"] = tier
    payload["mechanical_risk_evidence"] = evidence
    routing_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return routing_path


def parse_args() -> argparse.Namespace:
    """Parse the routing synchronization command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Code-review run directory.")
    return parser.parse_args()


def main() -> int:
    """Synchronize one review-routing artifact and print its path."""
    routing_path = synchronize_routing(parse_args().out)
    print(routing_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
