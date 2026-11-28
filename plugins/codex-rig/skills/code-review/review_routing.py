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

Missing or malformed routing JSON, a non-object payload, missing or invalid reviewer ``risk_tier`` or nested boolean
``signals`` mapping, unreadable diff evidence, or an unwritable output path exits non-zero with the underlying local
error. The helper never invents semantic signals or lowers the declared tier, and the final validator still rejects
underclassification, incomplete signals, or inconsistent specialist routing.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# A source file cannot contain a bytecode cache directory.
sys.pycache_prefix = str(Path(__file__).resolve())

SHARED_DIRECTORY = Path(__file__).resolve().parents[2] / "shared"
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))
from collect_diff import diff_sections, read_git_path_lines  # noqa: E402

#: Exact set of routing signal names that a routing record must provide, each a boolean.
ROUTING_SIGNALS = {
    "behavior_change",
    "bug_fix",
    "test_or_error_path",
    "data_tensor_boundary",
    "high_candidate",
    "unresolved_material_assumption",
    "material_no_finding",
    "explicit_adversarial",
    "axis_solution_architect",
    "axis_security_auditor",
    "axis_data_steward",
    "axis_cicd_steward",
    "axis_linting_expert",
    "axis_doc_scribe",
    "axis_oss_shepherd",
    "axis_squeezer",
    "axis_scientist",
    "axis_web_explorer",
}

# Git emits decimal counters and either a plain path or a C-quoted byte string. Twenty decimal digits cover 64-bit
# counters and bound integer conversion when a collected artifact is damaged.
_NUMSTAT_ROW = re.compile(
    rb"([0-9]{1,20})\t([0-9]{1,20})\t"
    rb'([^\x00-\x1f\x7f"\\]+|"(?:[^\x00-\x1f\x7f"\\]|\\(?:[abfnrtv"\\]|[0-3][0-7]{2}))+")'
)


def _path_tokens(path: str) -> set[str]:
    """Split a repository path into exact lowercase risk tokens.

    Separators are discarded rather than matched as substrings, so ``authentication.py`` yields ``authentication``
    while a path containing ``auth`` does not accidentally match it.

    Example:
        >>> sorted(_path_tokens("src/Auth-Config.py"))
        ['auth', 'config', 'py', 'src']
    """
    return {token for token in re.split(r"[/._-]+", path.lower()) if token}


def literal_patch_paths(out_dir: Path) -> set[str] | None:
    """Recognize a literal PR listing only when every name matches the collected Git patch."""
    path = out_dir / "files.txt"
    patch = out_dir / "diff.patch"
    if path.is_file() and patch.is_file():
        content = patch.read_bytes()
        sections = diff_sections(content)
        if content and not sections:
            return None
        literal = "".join(f"{name}\n" for name in sorted(sections)).encode("utf-8")
        if path.read_bytes() in (literal, literal.replace(b"\n", b"\r\n")):
            return set(sections)
    return None


def read_review_paths(out_dir: Path, filename: str) -> set[str]:
    """Read a patch-confirmed literal listing or Git's quoted inventory."""
    if filename == "files.txt":
        literal = literal_patch_paths(out_dir)
        if literal is not None:
            return literal
    return read_git_path_lines(out_dir / filename)


def _numstat_size(numstat: Path, tracked_paths: set[str]) -> tuple[int, int]:
    """Count numeric Git numstat rows and identify tracked paths without numeric size evidence."""
    rows = numstat.read_bytes().splitlines() if numstat.exists() else []
    changed_lines = 0
    unknown_size_rows = 0
    sized_paths: set[str] = set()
    for row in rows:
        if row.count(b"\t") < 2:
            continue  # not a numstat row: a blank or diagnostic line; a path it would have sized is still counted below
        match = _NUMSTAT_ROW.fullmatch(row)
        if match is None:
            unknown_size_rows += 1
            continue
        added, deleted, raw_path = match.groups()
        if raw_path.startswith(b'"'):
            raw_path = codecs.escape_decode(raw_path[1:-1])[0]
        try:
            path = os.fsdecode(raw_path)
        except UnicodeDecodeError:
            unknown_size_rows += 1
            continue
        if path not in tracked_paths or path in sized_paths:
            unknown_size_rows += 1
            continue
        sized_paths.add(path)
        changed_lines += int(added) + int(deleted)

    # An invalid row can also account for a missing path, such as a binary row or a collection diagnostic.
    return changed_lines, max(unknown_size_rows, len(tracked_paths - sized_paths))


def derive_mechanical_risk(out_dir: Path) -> tuple[str, list[str], set[str]]:
    """Derive the minimum tier, canonical evidence, and mandatory signals from collected diff facts."""
    tracked_paths = read_review_paths(out_dir, "files.txt")
    paths = tracked_paths | read_review_paths(out_dir, "untracked.txt")
    changed_lines, unknown_size_rows = _numstat_size(out_dir / "numstat.txt", tracked_paths)

    lower_paths = {path.lower() for path in paths}
    evidence = [f"files={len(paths)}", f"changed_lines={changed_lines}", f"unknown_size_rows={unknown_size_rows}"]
    broad_names = {"pyproject.toml", "package.json", "cargo.toml", "uv.lock", "poetry.lock", "package-lock.json"}
    high_risk_parts = {
        "auth",
        "authentication",
        "oauth",
        "credential",
        "credentials",
        "security",
        "token",
        "tokens",
        "secret",
        "secrets",
        "jwt",
        "crypto",
        "cryptography",
        "migration",
        "migrations",
        "dockerfile",
    }
    high_paths = sorted(
        path
        for path in lower_paths
        if path.startswith((".github/workflows/", ".github/actions/"))
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
    return tier, evidence, mandatory_signals


def synchronize_routing(out_dir: Path) -> Path:
    """Validate the reviewer tier and boolean inventory before deriving mechanical fields."""
    routing_path = out_dir / "review-routing.json"
    payload: Any = json.loads(routing_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {routing_path}")
    if payload.get("risk_tier") not in {"TRIVIAL", "LOCAL", "BROAD", "HIGH_RISK"}:
        raise ValueError(f"invalid-risk-tier:{payload.get('risk_tier')!r}")
    signals = payload.get("signals")
    if not isinstance(signals, dict) or set(signals) != ROUTING_SIGNALS:
        raise ValueError("review-routing-signal-set-mismatch: supply the complete nested signals object")
    if not all(isinstance(value, bool) for value in signals.values()):
        raise ValueError("review-routing-signals-not-boolean")
    tier, evidence, _ = derive_mechanical_risk(out_dir)
    payload["mechanical_risk_tier"] = tier
    payload["mechanical_risk_evidence"] = evidence
    routing_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return routing_path


def parse_args() -> argparse.Namespace:
    """Parse the routing synchronization command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Code-review run directory.")
    return parser.parse_args()


def main() -> int:
    """Synchronize one review-routing artifact, record the routed checkpoint and print the artifact path."""
    out_dir = parse_args().out
    routing_path = synchronize_routing(out_dir)
    # Imported here: the validator imports this module for derivation and needs no run-state writer.
    from review_run_state import mark

    try:
        mark(out_dir, "routed")
    except (OSError, ValueError) as error:
        # The stub is a diagnostic, not routing evidence; a write error or refused stub must not fail routing.
        print(f"run-state-unrecorded:routed:{error}", file=sys.stderr)
    print(routing_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
