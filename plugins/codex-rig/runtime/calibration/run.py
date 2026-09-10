#!/usr/bin/env python3
"""Run Codex Rig static and behavioral calibration, then write its result artifact.

## Purpose

detect routing, skill-contract, and calibration-fixture drift before a plugin release or behavior-changing workflow
edit. It combines static checks with optional behavioral evidence so maintainers can see both contract failures and the
confidence limits of the available observations.

## Scope

validates shipped files and local observations under a selected layout; it does not invoke GitHub or live models. The
run may execute local helper and self-test commands, but paid model collection is a separate explicit workflow handled
by ``run_live_ab.py``.

## Usage

run ``python runtime/calibration/run.py --layout plugin`` from the plugin root after changing skills, agents, or
calibration data. Use the source layout when checking a repository checkout and the plugin layout when validating files
as they will be installed and packaged.

## Used by

Release and implementation verification workflows, package maintainers, and calibration acceptance tests. Its result is
also consumed by release-readiness decisions that require named checks, artifact paths, and explicit confidence gaps.

## Outputs

writes a timestamped ``result.json`` containing check status, findings, behavioral metrics, confidence, and explicit
confidence limits. The same report directory records check logs, leak findings, recommendations, and self-test artifacts
needed to explain a failed result.

## Failure

contract drift produces named failed checks and a non-zero result; absent optional live observations lower confidence
rather than fabricating evidence. Malformed inputs, missing shipped assets, or failed local gates are preserved in the
report so maintainers can distinguish a real regression from incomplete evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_contract import Layout, build_prompt, candidate_findings, prompt_sha256, role_context, task_contract_sha256


SKILLS = (
    "code-review",
    "implement",
    "code-remediate",
    "audit",
    "calibrate",
    "release",
    "investigate",
    "sync",
    "manage",
    "assess",
    "optimize",
    "research",
)
AGENTS = (
    "sw-engineer",
    "qa-specialist",
    "squeezer",
    "doc-scribe",
    "security-auditor",
    "data-steward",
    "cicd-steward",
    "linting-expert",
    "oss-shepherd",
    "solution-architect",
    "web-explorer",
    "curator",
    "challenger",
    "scientist",
    "delegation-lead",
)
DEFAULT_MODEL = "gpt-5.6-terra"
REVIEW_MODEL = "gpt-5.6-terra"
CRITICAL_MODEL = "gpt-5.6-sol"
SUPPORT_MODEL = "gpt-5.6-luna"
SUPPORTED_ACTIVE_MODELS = {DEFAULT_MODEL, CRITICAL_MODEL, SUPPORT_MODEL}
SOL_MODEL_AGENTS = {
    "security-auditor",
    "solution-architect",
}
LUNA_MODEL_AGENTS = {
    "cicd-steward",
    "delegation-lead",
    "doc-scribe",
    "linting-expert",
    "oss-shepherd",
    "web-explorer",
}
TERRA_MODEL_AGENTS = set(AGENTS) - SOL_MODEL_AGENTS - LUNA_MODEL_AGENTS
HIGH_EFFORT_AGENTS = set(AGENTS)
RECURRENCE_POLICY_LINK = "../../shared/native-skill-contract.md#recurrence-and-root-cause-policy"
RECURRENCE_POLICY_SKILLS = frozenset({"code-remediate", "implement", "investigate"})
RECURRENCE_POLICY_ROLES = frozenset({"delegation-lead"})
RECURRENCE_CASE_CONTRACT: dict[str, tuple[str, tuple[str, ...]]] = {
    "recurrence-initial-obstacle": ("implement", ("initial-obstacle-not-recorded",)),
    "recurrence-second-occurrence-investigate": (
        "investigate",
        (
            "recurrence-investigation-required",
            "root-cause-evidence-required",
            "recurrence-reset-evidence-missing",
        ),
    ),
    "recurrence-third-occurrence-human-handoff": (
        "delegation-lead",
        (
            "recurrence-human-handoff-required",
            "human-handoff-missing",
            "attempted-actions-missing",
            "shared-obstacle-evidence-missing",
        ),
    ),
}
MODEL_STALL_CASE_CONTRACT: dict[str, tuple[str, tuple[str, ...]]] = {
    "model-stall-advisory-escalation": (
        "delegation-lead",
        (
            "reasoning-progress-not-assessed",
            "model-stall-escalation-required",
            "stall-ledger-missing",
        ),
    ),
    "model-stall-human-handoff": (
        "delegation-lead",
        (
            "post-escalation-human-handoff-required",
            "model-stall-handoff-evidence-missing",
            "next-step-proposal-missing",
        ),
    ),
    "model-stall-progress-without-closure": (
        "delegation-lead",
        (
            "closure-condition-not-recorded",
            "evidence-backed-attempt-escalation-required",
            "progress-without-closure-ledger-missing",
        ),
    ),
    "model-stall-user-directed-progress": (
        "delegation-lead",
        (
            "user-directed-progress-misclassified",
            "false-advisory-escalation",
            "user-decision-evidence-missing",
        ),
    ),
    "model-stall-advisory-route-safety": (
        "delegation-lead",
        (
            "advisory-read-only-sandbox-unverified",
            "advisory-mutation-fence-missing",
            "human-handoff-required-when-route-unavailable",
        ),
    ),
}
EXPLICIT_SOL_ROUTING_CASE_CONTRACT: dict[str, tuple[str, tuple[str, ...]]] = {
    "explicit-sol-automatic-route-rejected": (
        "delegation-lead",
        (
            "explicit-sol-request-required",
            "automatic-sol-routing-rejected",
            "terra-parent-session-required",
        ),
    ),
    "explicit-sol-advisory-boundary": (
        "delegation-lead",
        (
            "sol-advisory-boundary-violated",
            "sol-evidence-handover-missing",
            "terra-parent-acceptance-required",
        ),
    ),
}


@dataclass(slots=True)
class Paths:
    """Hold all paths used by the calibration run."""

    layout: str
    root: Path
    asset_root: Path
    calibration_dir: Path
    skills_dir: Path
    roles_dir: Path
    shared_dir: Path
    timestamp: str
    out_dir: Path
    project_cfg: Path | None
    tasks: Path
    benchmarks: Path
    behavioral_cases: Path
    behavioral_observations: Path
    behavioral_scorer: Path
    live_ab_runner: Path
    live_ab_tasks: Path
    live_contract: Path
    live_route_policy: Path
    accepted_route_evidence: Path
    behavioral_result: Path
    quality_gates: Path
    helper_cli_contract: Path
    native_skill_contract: Path
    run_gates: Path
    run_py: Path
    write_result_py: Path
    final_handoff_py: Path
    create_run: Path
    codemap_adapter: Path
    collect_diff: Path
    github_read: Path | None
    collect_pr: Path
    select_git_remote: Path
    sync_manifest: Path
    find_review_report: Path
    validate_artifacts: Path
    code_review_validate_artifacts: Path
    code_review_review_routing: Path
    codex_harness: Path
    checks: Path
    leaks: Path
    recommendations: Path
    result: Path

    @classmethod
    def create(cls, layout: str = "plugin", root: Path | None = None) -> "Paths":
        """Create the output directory and return resolved calibration paths."""
        project_root = (root or Path.cwd()).resolve()
        asset_root = Path(__file__).resolve().parents[2] if layout == "plugin" else project_root / ".codex"
        calibration_dir = asset_root / ("runtime/calibration" if layout == "plugin" else "calibration")
        skills_dir = asset_root / "skills"
        roles_dir = asset_root / ("roles" if layout == "plugin" else "agents")
        shared_dir = asset_root / ("shared" if layout == "plugin" else "skills/_shared")
        base_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
        reports_dir = project_root / ".reports" / "codex" / "calibration"
        for attempt in range(100):
            timestamp = base_timestamp if attempt == 0 else f"{base_timestamp}-{attempt:02d}"
            out_dir = reports_dir / timestamp
            try:
                out_dir.mkdir(parents=True)
            except FileExistsError:
                continue
            break
        else:
            raise RuntimeError("unable-to-create-unique-calibration-artifact-directory")
        return cls(
            layout=layout,
            root=project_root,
            asset_root=asset_root,
            calibration_dir=calibration_dir,
            skills_dir=skills_dir,
            roles_dir=roles_dir,
            shared_dir=shared_dir,
            timestamp=timestamp,
            out_dir=out_dir,
            project_cfg=asset_root / "config.toml" if layout == "source" else None,
            tasks=calibration_dir / "tasks.json",
            benchmarks=calibration_dir / "benchmarks.json",
            behavioral_cases=calibration_dir / "behavioral-cases.json",
            behavioral_observations=calibration_dir / "behavioral-observations.jsonl",
            behavioral_scorer=calibration_dir / "score_behavioral.py",
            live_ab_runner=calibration_dir / "run_live_ab.py",
            live_ab_tasks=calibration_dir / "live-ab-tasks.json",
            live_contract=calibration_dir / "live_contract.py",
            live_route_policy=calibration_dir / "live-route-policy.json",
            accepted_route_evidence=calibration_dir / "accepted-route-evidence.json",
            behavioral_result=out_dir / "behavioral.json",
            quality_gates=shared_dir / "quality-gates.md",
            helper_cli_contract=shared_dir / "helper-cli-contract.md",
            native_skill_contract=shared_dir / "native-skill-contract.md",
            run_gates=shared_dir / ("run_gates.py" if layout == "plugin" else "run-gates.sh"),
            run_py=calibration_dir / "run.py",
            write_result_py=shared_dir / "write-result.py",
            final_handoff_py=shared_dir / "final_handoff.py",
            create_run=shared_dir / "create_run.py",
            codemap_adapter=shared_dir / "codemap_adapter.py",
            collect_diff=shared_dir / ("collect_diff.py" if layout == "plugin" else "collect-diff.sh"),
            github_read=shared_dir / "github_read.py" if layout == "plugin" else None,
            collect_pr=shared_dir / ("collect_pr.py" if layout == "plugin" else "collect-pr.sh"),
            select_git_remote=shared_dir / "select-git-remote.py",
            sync_manifest=asset_root / ("package-manifest.json" if layout == "plugin" else "sync-manifest.json"),
            find_review_report=shared_dir / "find-review-report.py",
            validate_artifacts=shared_dir / "validate-artifacts.py",
            code_review_validate_artifacts=skills_dir / "code-review" / "validate_artifacts.py",
            code_review_review_routing=skills_dir / "code-review" / "review_routing.py",
            codex_harness=project_root / ".github" / "codex-harness.sh",
            checks=out_dir / "checks.txt",
            leaks=out_dir / "leaks.txt",
            recommendations=out_dir / "recommendations.md",
            result=out_dir / "result.json",
        )


@dataclass(slots=True)
class CalibrationRun:
    """Track mutable calibration state and provide check helpers."""

    paths: Paths
    fails: int = 0
    leaks: int = 0
    checks_failed: list[str] = field(default_factory=list)
    accepted_route_evidence_current: bool = False

    def mark_check_failed(self, check_id: str) -> None:
        """Record a failed check once."""
        if check_id not in self.checks_failed:
            self.checks_failed.append(check_id)

    def append_check(self, line: str) -> None:
        """Append a single check detail line."""
        append_line(self.paths.checks, line)

    def append_leak(self, line: str) -> None:
        """Append a single leak detail line."""
        append_line(self.paths.leaks, line)

    def leak_only(self, check_id: str, line: str) -> None:
        """Record a leak that should fail status through leak count."""
        self.append_leak(line)
        self.mark_check_failed(check_id)
        self.leaks += 1

    def fail_and_leak(self, check_id: str, line: str, count: int = 1) -> None:
        """Record a check failure with matching fail and leak counts."""
        self.append_leak(line)
        self.mark_check_failed(check_id)
        self.fails += count
        self.leaks += count


def append_line(path: Path, line: str) -> None:
    """Append one UTF-8 line to a file, creating parent directories if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_text(path: Path) -> str:
    """Read a text file or return an empty string when it is absent."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def normalize_regex(pattern: str) -> str:
    """Convert shell-era POSIX space classes to Python regex syntax."""
    return pattern.replace("[[:space:]]", r"\s")


def check_contains(run: CalibrationRun, file: Path, pattern: str, check_id: str) -> None:
    """Check that a file contains a case-insensitive regex pattern."""
    text = read_text(file)
    try:
        matched = re.search(normalize_regex(pattern), text, flags=re.IGNORECASE | re.MULTILINE) is not None
    except re.error:
        matched = pattern.lower() in text.lower()
    if not matched:
        run.leak_only(check_id, f"missing:{pattern}:{file}")


def top_level_setting(file: Path, key: str) -> str | None:
    """Read a top-level TOML string setting before tables or developer instructions."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*(?:#.*)?$")
    for line in read_text(file).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^\s*developer_instructions\s*=", line) or re.match(r"^\s*\[", line):
            return None
        match = pattern.match(line)
        if not match:
            continue
        raw = match.group(1).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            return raw[1:-1]
        return raw
    return None


def frontmatter_setting(file: Path, key: str) -> str | None:
    """Read one scalar from a Markdown YAML frontmatter block."""
    lines = read_text(file).splitlines()
    if not lines or lines[0] != "---":
        return None
    for line in lines[1:]:
        if line == "---":
            return None
        prefix = f"{key}:"
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip("\"'")
    return None


def role_file(run: CalibrationRun, role: str) -> Path:
    """Return the canonical role file for the active layout."""
    if run.paths.layout == "plugin":
        return run.paths.roles_dir / role / "ROLE.md"
    return run.paths.roles_dir / f"{role}.toml"


def role_setting(run: CalibrationRun, file: Path, key: str) -> str | None:
    """Read a role setting from plugin frontmatter or source TOML."""
    if run.paths.layout == "plugin":
        return frontmatter_setting(file, key)
    return top_level_setting(file, key)


def check_model(run: CalibrationRun, file: Path, label: str, check_id: str, expected: str) -> None:
    """Check the configured top-level model value."""
    if top_level_setting(file, "model") == expected:
        run.append_check(f"{label}:model=ok")
        return
    run.append_check(f"{label}:model=fail")
    run.fail_and_leak(check_id, f"model-not-{expected}:{file}")


def check_review_model(run: CalibrationRun, file: Path, label: str) -> None:
    """Check the project review model policy."""
    if top_level_setting(file, "review_model") == REVIEW_MODEL:
        run.append_check(f"{label}:review_model=ok")
        return
    run.append_check(f"{label}:review_model=fail")
    run.fail_and_leak("review-model-policy", f"review-model-not-{REVIEW_MODEL}:{file}")


def check_reasoning_effort(run: CalibrationRun, file: Path, expected: str, label: str, check_id: str) -> None:
    """Check the configured model reasoning effort."""
    pattern = rf'^\s*model_reasoning_effort\s*=\s*"{re.escape(expected)}"'
    if re.search(pattern, read_text(file), flags=re.MULTILINE):
        run.append_check(f"{label}:effort={expected}")
        return
    run.append_check(f"{label}:effort=fail")
    run.fail_and_leak(check_id, f"reasoning-effort-mismatch:{label}:expected={expected}:{file}")


def check_supported_active_models(run: CalibrationRun) -> None:
    """Reject active model names outside the configured model policy."""
    role_files = [role_file(run, agent) for agent in AGENTS]
    files = [*([run.paths.project_cfg] if run.paths.project_cfg is not None else []), *role_files]
    for file in files:
        for setting in ("model", "review_model"):
            value = role_setting(run, file, setting) if file in role_files else top_level_setting(file, setting)
            if value is not None and value not in SUPPORTED_ACTIVE_MODELS:
                run.fail_and_leak("supported-model-policy", f"unsupported-active-model:{value}:{file}")


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _derive_role_assignments(
    scores: dict[str, dict[str, Any]], route_policy: dict[str, Any], adjudication: dict[str, Any]
) -> dict[str, str]:
    """Derive active role models from accepted paired evidence."""
    assignments = {agent: DEFAULT_MODEL for agent in AGENTS}
    gain_threshold = float(adjudication["candidate_quality_gain_threshold"])
    cost_ratio_max = float(adjudication["candidate_aggregate_cost_ratio_max"])

    for route_id in ("sol-critical-high", "terra-general-high"):
        comparisons = scores[route_id]["live_route_acceptance"]["routes"][route_id]["comparisons"]
        by_role: dict[str, list[dict[str, Any]]] = {}
        for comparison in comparisons:
            by_role.setdefault(comparison["role"], []).append(comparison)
        for role, rows in by_role.items():
            quality_regressions = sum(not row["quality_ok"] for row in rows)
            mean_gain = sum(row["candidate_f1"] - row["baseline_f1"] for row in rows) / len(rows)
            aggregate_cost_ratio = math.prod(float(row["cost_ratio"]) for row in rows) ** (1 / len(rows))
            candidate_wins = quality_regressions == 0 and (
                mean_gain >= gain_threshold or aggregate_cost_ratio <= cost_ratio_max
            )
            model_key = "candidate_model" if candidate_wins else "baseline_model"
            assignments[role] = route_policy[route_id][model_key]
    for override in adjudication.get("human_overrides", []):
        model = override["model"]
        for role in override["roles"]:
            assignments[role] = model
    return assignments


def check_accepted_route_evidence(run: CalibrationRun) -> None:
    """Bind active model pins to hashed paid evidence and classify its roster freshness."""
    try:
        payload = json.loads(run.paths.accepted_route_evidence.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 2 or payload.get("reasoning_effort") != "high":
            raise ValueError("unsupported accepted-route evidence schema or effort")
        evidence_skill_roster = payload.get("skill_roster")
        if not isinstance(evidence_skill_roster, list) or not all(
            isinstance(skill, str) for skill in evidence_skill_roster
        ):
            raise ValueError("accepted route evidence skill roster is malformed")
        sol_selection = payload.get("sol_role_selection")
        if sol_selection != {
            "mode": "bounded-read-only-advisory",
            "parent_model": DEFAULT_MODEL,
            "parent_owns_final_acceptance": True,
            "requires_explicit_user_request_or_agent_selection": True,
        }:
            raise ValueError("explicit Sol selection policy mismatch")

        scores: dict[str, dict[str, Any]] = {}
        observed_rows = 0
        for item in payload["evidence_files"]:
            path = run.paths.asset_root / ("runtime" if run.paths.layout == "plugin" else "") / item["path"]
            if _sha256_file(path) != item["sha256"]:
                raise ValueError(f"evidence hash mismatch: {item['path']}")
            if "rows" in item:
                row_count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
                if row_count != item["rows"]:
                    raise ValueError(f"evidence row mismatch: {item['path']}")
                observed_rows += row_count
                continue
            score = json.loads(path.read_text(encoding="utf-8"))
            route_id = item["route"]
            route_status = score["live_route_acceptance"]["routes"][route_id]["status"]
            if route_status != item["strict_status"]:
                raise ValueError(f"strict route status mismatch: {route_id}")
            scores[route_id] = score

        if observed_rows != payload["observed_live_calls"]:
            raise ValueError("observed live call count mismatch")
        if scores["luna-support-high"]["live_route_acceptance"]["routes"]["luna-support-high"]["status"] != "fail":
            raise ValueError("Luna evidence must fail closed")

        route_policy = json.loads(run.paths.live_route_policy.read_text(encoding="utf-8"))["routes"]
        derived = _derive_role_assignments(scores, route_policy, payload["adjudication"])
        declared = {agent: model for model, agents in payload["active_assignments"].items() for agent in agents}
        configured = {agent: expected_agent_model(agent) for agent in AGENTS}
        if set(declared) != set(AGENTS) or declared != derived or configured != derived:
            raise ValueError("accepted route assignments do not match evidence or active pins")
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        run.fail_and_leak("accepted-route-evidence", f"accepted-route-evidence-invalid:{exc}")
        return

    if tuple(evidence_skill_roster) != SKILLS:
        run.append_check(f"accepted-route-evidence=archived-stale:skill-roster-mismatch:live-calls={observed_rows}")
        return

    run.accepted_route_evidence_current = True
    run.append_check(
        "accepted-route-evidence=ok:"
        f"live-calls={observed_rows}:luna-agents={len(LUNA_MODEL_AGENTS)}:sol-agents={len(SOL_MODEL_AGENTS)}"
    )


def check_behavioral_cases_version(run: CalibrationRun) -> None:
    """Ensure behavioral case versions only move one commit-relative step."""
    if run.paths.layout == "plugin":
        run.append_check("behavioral-version=skipped:immutable-plugin-fixture")
        return
    if not run.paths.behavioral_cases.exists():
        return
    if not (run.paths.root / ".git").exists():
        run.append_check("behavioral-version=skipped:no-git")
        return

    head_cases = run.paths.out_dir / "behavioral-cases.head.json"
    git_result = subprocess.run(
        ["git", "show", "HEAD:.codex/calibration/behavioral-cases.json"],
        cwd=run.paths.root,
        text=True,
        capture_output=True,
        check=False,
    )
    if git_result.returncode != 0:
        run.append_check("behavioral-version=skipped:no-head-version")
        return
    head_cases.write_text(git_result.stdout, encoding="utf-8")

    try:
        head_version = read_json_version(head_cases)
        current_version = read_json_version(run.paths.behavioral_cases)
        if not same_or_single_commit_bump(parse_version(head_version), parse_version(current_version)):
            raise ValueError(
                "current version must equal HEAD or be one commit-relative bump: "
                f"HEAD={head_version}, current={current_version}"
            )
    except Exception as exc:  # noqa: BLE001 - calibration must report the concrete parsing/version blocker.
        run.fail_and_leak("behavioral-version-policy", f"behavioral-version-gap:{exc}")
        return

    run.append_check(f"behavioral-version=ok:HEAD={head_version}:current={current_version}")


def check_sync_manifest_config_scope(run: CalibrationRun) -> None:
    """Validate that sync manages current nested agent settings without stale keys."""
    if run.paths.layout == "plugin":
        try:
            payload = json.loads(run.paths.sync_manifest.read_text(encoding="utf-8"))
            packaged_skills = {item["id"] for item in payload["skills"]}
            packaged_roles = {item["id"] for item in payload["roles"]}
            missing_skills = set(SKILLS) - packaged_skills
            if missing_skills:
                raise ValueError(f"required skills missing: {sorted(missing_skills)}")
            if packaged_roles != set(AGENTS):
                raise ValueError(f"role roster mismatch: {sorted(packaged_roles ^ set(AGENTS))}")
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            run.fail_and_leak("package-manifest-scope", f"package-manifest-scope-invalid:{exc}")
            return
        run.append_check("package-manifest-scope=ok")
        return

    try:
        config_scope = json.loads(run.paths.sync_manifest.read_text(encoding="utf-8"))["config"]
        for key in ("root_keys", "feature_keys", "agent_keys", "agent_names", "skill_paths"):
            values = config_scope.get(key)
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(value, str) and value for value in values)
            ):
                raise ValueError(f"{key} must be a non-empty string list")
            if len(values) != len(set(values)):
                raise ValueError(f"{key} contains duplicates")
        expected_agent_keys = {"max_threads", "max_depth", "job_max_runtime_seconds"}
        if set(config_scope["agent_keys"]) != expected_agent_keys:
            raise ValueError("agent_keys must manage current [agents] settings")
        if "commit_attribution" not in config_scope["root_keys"]:
            raise ValueError("commit_attribution must remain a managed root key")
        expected_attribution = "Co-authored-by: Codex <codex@openai.com>"
        if (
            run.paths.project_cfg is None
            or top_level_setting(run.paths.project_cfg, "commit_attribution") != expected_attribution
        ):
            raise ValueError("commit_attribution missing or changed")
        stale_root = expected_agent_keys & set(config_scope["root_keys"])
        if stale_root:
            raise ValueError(f"stale root keys: {sorted(stale_root)}")
        if "child_agents_md" in config_scope["feature_keys"]:
            raise ValueError("stale feature key: child_agents_md")
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        run.fail_and_leak("sync-manifest-config-scope", f"sync-manifest-config-scope-invalid:{exc}")
        return

    run.append_check("sync-manifest-config-scope=ok")


def read_json_version(path: Path) -> str:
    """Read a required string version from a JSON fixture file."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"missing string version in {path}")
    return version.strip()


def parse_version(value: str) -> tuple[int, ...]:
    """Parse a dotted numeric version into comparable integer parts."""
    parts = value.split(".")
    if not parts or any(not part.isdecimal() for part in parts):
        raise ValueError(f"version must be dotted numeric: {value!r}")
    return tuple(int(part) for part in parts)


def same_or_single_commit_bump(head: tuple[int, ...], current: tuple[int, ...]) -> bool:
    """Return whether current is unchanged or one version step from HEAD."""
    width = max(len(head), len(current))
    head_parts = head + (0,) * (width - len(head))
    current_parts = current + (0,) * (width - len(current))
    if current_parts == head_parts:
        return True
    for index, (head_part, current_part) in enumerate(zip(head_parts, current_parts, strict=True)):
        if head_part == current_part:
            continue
        if current_part != head_part + 1:
            return False
        return all(part == 0 for part in current_parts[index + 1 :])
    return False


def check_skill_frontmatter(run: CalibrationRun, file: Path, skill: str) -> None:
    """Validate minimal YAML frontmatter for a skill file."""
    lines = read_text(file).splitlines()
    ok = len(lines) >= 4 and lines[0] == "---"
    if ok:
        try:
            end = lines[1:].index("---") + 1
        except ValueError:
            ok = False
        else:
            frontmatter = lines[1:end]
            ok = any(line.startswith("name:") for line in frontmatter) and any(
                line.startswith("description:") for line in frontmatter
            )
    if ok:
        run.append_check(f"skill-frontmatter:{skill}=ok")
        return
    run.fail_and_leak("skill-schema-all", f"skill-frontmatter-invalid:{file}")


def expected_agent_model(agent: str) -> str | None:
    """Return the expected model for an agent."""
    if agent in SOL_MODEL_AGENTS:
        return CRITICAL_MODEL
    if agent in LUNA_MODEL_AGENTS:
        return SUPPORT_MODEL
    if agent in TERRA_MODEL_AGENTS:
        return DEFAULT_MODEL
    return None


def expected_agent_effort(agent: str) -> str | None:
    """Return the expected reasoning effort for an agent."""
    if agent in HIGH_EFFORT_AGENTS:
        return "high"
    return None


def check_agent_model(run: CalibrationRun, agent: str, file: Path) -> None:
    """Check an agent model against policy."""
    expected = expected_agent_model(agent)
    if expected is None:
        run.fail_and_leak("agent-model-policy", f"agent-model-policy-missing:{agent}")
        return
    if role_setting(run, file, "model") == expected:
        run.append_check(f"agent-model:{agent}={expected}")
        return
    run.fail_and_leak("agent-model-policy", f"agent-model-mismatch:{agent}:expected={expected}:{file}")


def check_agent_effort(run: CalibrationRun, agent: str, file: Path) -> None:
    """Check an agent reasoning effort against policy."""
    expected = expected_agent_effort(agent)
    if expected is None:
        run.fail_and_leak("agent-effort-policy", f"agent-effort-policy-missing:{agent}")
        return
    if role_setting(run, file, "model_reasoning_effort") == expected:
        run.append_check(f"agent-effort:{agent}={expected}")
        return
    run.fail_and_leak("agent-effort-policy", f"agent-effort-mismatch:{agent}:expected={expected}:{file}")


def check_core_configs(run: CalibrationRun) -> None:
    """Run project, skill, shared contract, and agent configuration checks."""
    run.paths.checks.write_text(f"calibration-start:{run.paths.timestamp}\n", encoding="utf-8")
    if run.paths.project_cfg is not None:
        check_model(run, run.paths.project_cfg, "project-config", "project-model-default", DEFAULT_MODEL)
        check_review_model(run, run.paths.project_cfg, "project-config")
        check_reasoning_effort(run, run.paths.project_cfg, "high", "project-config", "reasoning-effort-policy")
    else:
        run.append_check("project-config=not-applicable:plugin-layout")
    check_supported_active_models(run)
    check_accepted_route_evidence(run)

    for skill in SKILLS:
        skill_file = run.paths.skills_dir / skill / "SKILL.md"
        if not skill_file.exists():
            run.fail_and_leak("skill-schema-all", f"missing-skill:{skill}")
            continue
        check_skill_frontmatter(run, skill_file, skill)
        check_contains(run, skill_file, "^# ", "skill-schema-all")
        check_contains(run, skill_file, "Input Schema", "native-skill-contract")
        check_contains(run, skill_file, "Workflow", "skill-schema-all")
        check_contains(run, skill_file, "Fail-[Ff]ast Rules", "native-skill-contract")
        check_contains(run, skill_file, "Quality Gates", "native-skill-contract")
        check_contains(run, skill_file, "Calibration Hooks", "native-skill-contract")
        check_contains(run, skill_file, "Output Contract", "skill-schema-all")
        check_contains(run, skill_file, "quality-gates", "skill-schema-all")
        check_contains(
            run,
            skill_file,
            rf"(?:\.reports/codex/{skill}/|create_run\.py --skill {skill})",
            "skill-schema-all",
        )
        check_contains(run, skill_file, "result-template.json", "skill-schema-all")
        check_contains(run, skill_file, "helper-cli-contract", "skill-schema-all")
        template_file = skill_file.with_name("result-template.json")
        if not template_file.exists():
            run.fail_and_leak("skill-schema-all", f"missing-result-template:{skill}")
            continue
        try:
            json.loads(template_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            run.fail_and_leak("skill-schema-all", f"invalid-result-template:{skill}:{exc.lineno}")
            continue
        for field_name in ("status", "checks_run", "checks_failed", "findings", "confidence", "artifact_path"):
            check_contains(run, template_file, f'"{field_name}"', "skill-schema-all")
        if run.paths.project_cfg is not None:
            check_contains(
                run,
                run.paths.project_cfg,
                rf'path\s*=\s*"skills/{skill}"',
                "skill-registration-project",
            )

    for file, check_id in (
        (run.paths.tasks, "fixed-task-set"),
        (run.paths.benchmarks, "benchmark-pattern-checks"),
        (run.paths.behavioral_cases, "behavioral-metrics"),
        (run.paths.behavioral_observations, "behavioral-metrics"),
        (run.paths.behavioral_scorer, "behavioral-metrics"),
        (run.paths.live_ab_runner, "behavioral-metrics"),
        (run.paths.live_ab_tasks, "behavioral-metrics"),
        (run.paths.live_contract, "behavioral-metrics"),
        (run.paths.live_route_policy, "behavioral-metrics"),
        (run.paths.accepted_route_evidence, "accepted-route-evidence"),
        (run.paths.helper_cli_contract, "shared-script-selftests"),
        (run.paths.sync_manifest, "shared-script-selftests"),
    ):
        if not file.exists():
            name = file.stem.replace("-", "_")
            run.fail_and_leak(check_id, f"missing-{name}:{file}")

    check_behavioral_cases_version(run)
    check_sync_manifest_config_scope(run)
    check_fixed_task_and_behavioral_rosters(run)
    check_shared_confidence_contracts(run)
    helper_names = (
        "run.py",
        "run_live_ab.py",
        "score_behavioral.py",
        "find-review-report.py",
        "select-git-remote.py",
        "write-result.py",
        "final_handoff.py",
        "validate-artifacts.py",
    )
    helper_names += (
        ("create_run.py", "run_gates.py", "collect_diff.py", "github_read.py", "collect_pr.py", "codemap_adapter.py")
        if run.paths.layout == "plugin"
        else ("run-gates.sh", "collect-diff.sh", "collect-pr.sh")
    )
    if run.paths.layout == "source":
        helper_names += ("codex-harness.sh",)
    for helper_name in helper_names:
        check_contains(run, run.paths.helper_cli_contract, re.escape(helper_name), "shared-script-selftests")
    check_packaged_recurrence_policy_links(run)
    check_agents(run)


def check_fixed_task_and_behavioral_rosters(run: CalibrationRun) -> None:
    """Validate fixed task schema and behavioral coverage for every configured skill."""
    try:
        tasks_payload = json.loads(run.paths.tasks.read_text(encoding="utf-8"))
        tasks = tasks_payload["tasks"]
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("tasks must be a non-empty list")
        ids: list[str] = []
        task_skills: set[str] = set()
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise ValueError(f"task {index} is not an object")
            for key in ("id", "skill", "goal", "success"):
                if not isinstance(task.get(key), str) or not task[key].strip():
                    raise ValueError(f"task {index} needs non-empty {key}")
            ids.append(task["id"])
            task_skills.add(task["skill"])
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        if task_skills != set(SKILLS):
            raise ValueError(f"task skill roster mismatch: {sorted(task_skills ^ set(SKILLS))}")

        cases_payload = json.loads(run.paths.behavioral_cases.read_text(encoding="utf-8"))
        case_targets = {case.get("target") for case in cases_payload.get("cases", []) if isinstance(case, dict)}
        missing_targets = sorted(set(SKILLS) - case_targets)
        if missing_targets:
            raise ValueError(f"behavioral skill targets missing: {missing_targets}")
        validate_recurrence_case_contract(cases_payload)
        validate_model_stall_case_contract(cases_payload)
        validate_explicit_sol_routing_case_contract(cases_payload)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        run.fail_and_leak("fixed-task-set", f"fixed-task-roster-invalid:{exc}")
        return
    run.append_check("fixed-task-roster=ok")


def validate_recurrence_case_contract(cases_payload: dict[str, Any]) -> None:
    """Require one observed calibration case for every recurrence escalation stage."""
    cases = cases_payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("behavioral cases must be a list")
    recurrence_cases = {
        case.get("id"): case
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("id"), str) and case["id"].startswith("recurrence-")
    }
    if set(recurrence_cases) != set(RECURRENCE_CASE_CONTRACT):
        raise ValueError("recurrence behavioral case roster mismatch")
    for case_id, (expected_target, expected_findings) in RECURRENCE_CASE_CONTRACT.items():
        case = recurrence_cases[case_id]
        if case.get("target") != expected_target:
            raise ValueError(f"recurrence case target mismatch: {case_id}")
        if case.get("expected_findings") != list(expected_findings):
            raise ValueError(f"recurrence case findings mismatch: {case_id}")


def validate_model_stall_case_contract(cases_payload: dict[str, Any]) -> None:
    """Require calibration coverage for advisory and human stall escalation."""
    cases = cases_payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("behavioral cases must be a list")
    stall_cases = {
        case.get("id"): case
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("id"), str) and case["id"].startswith("model-stall-")
    }
    if set(stall_cases) != set(MODEL_STALL_CASE_CONTRACT):
        raise ValueError("model stall behavioral case roster mismatch")
    for case_id, (expected_target, expected_findings) in MODEL_STALL_CASE_CONTRACT.items():
        case = stall_cases[case_id]
        if case.get("target") != expected_target:
            raise ValueError(f"model stall case target mismatch: {case_id}")
        if case.get("expected_findings") != list(expected_findings):
            raise ValueError(f"model stall case findings mismatch: {case_id}")


def validate_explicit_sol_routing_case_contract(cases_payload: dict[str, Any]) -> None:
    """Require coverage for explicit-only Sol selection and advisory handoff."""
    cases = cases_payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("behavioral cases must be a list")
    sol_routing_cases = {
        case.get("id"): case
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("id"), str) and case["id"].startswith("explicit-sol-")
    }
    if set(sol_routing_cases) != set(EXPLICIT_SOL_ROUTING_CASE_CONTRACT):
        raise ValueError("explicit Sol routing behavioral case roster mismatch")
    for case_id, (expected_target, expected_findings) in EXPLICIT_SOL_ROUTING_CASE_CONTRACT.items():
        case = sol_routing_cases[case_id]
        if case.get("target") != expected_target:
            raise ValueError(f"explicit Sol routing case target mismatch: {case_id}")
        if case.get("expected_findings") != list(expected_findings):
            raise ValueError(f"explicit Sol routing case findings mismatch: {case_id}")


def find_misplaced_packaged_recurrence_policy_links(
    skill_files: tuple[Path, ...], role_files: tuple[Path, ...]
) -> list[Path]:
    """Return packaged files whose recurrence link conflicts with retry ownership."""
    misplaced: list[Path] = []
    for path in skill_files:
        link_present = RECURRENCE_POLICY_LINK in read_text(path)
        if link_present != (path.parent.name in RECURRENCE_POLICY_SKILLS):
            misplaced.append(path)
    for path in role_files:
        link_present = RECURRENCE_POLICY_LINK in read_text(path)
        if link_present != (path.parent.name in RECURRENCE_POLICY_ROLES):
            misplaced.append(path)
    return misplaced


def check_packaged_recurrence_policy_links(run: CalibrationRun) -> None:
    """Require recurrence links only on workflows that own repeated attempts."""
    if run.paths.layout != "plugin":
        return
    skill_files = tuple(sorted(run.paths.skills_dir.glob("*/SKILL.md")))
    role_files = tuple(sorted(run.paths.roles_dir.glob("*/ROLE.md")))
    misplaced = find_misplaced_packaged_recurrence_policy_links(skill_files, role_files)
    if misplaced:
        for path in misplaced:
            run.fail_and_leak("recurrence-policy-links", f"misplaced-recurrence-policy-link:{path}")
        return
    required_count = len(RECURRENCE_POLICY_SKILLS) + len(RECURRENCE_POLICY_ROLES)
    run.append_check(f"recurrence-policy-links=ok:owners={required_count}")


def check_shared_confidence_contracts(run: CalibrationRun) -> None:
    """Check shared confidence policy wording in shared contract files."""
    for shared_contract in (run.paths.quality_gates, run.paths.native_skill_contract):
        if not shared_contract.exists():
            run.fail_and_leak("confidence-policy", f"missing-shared-confidence-contract:{shared_contract}")
            continue
        check_contains(run, shared_contract, "confidence-not-acceptable", "confidence-policy")
        check_contains(run, shared_contract, "confidence-very-questionable", "confidence-policy")
        check_contains(run, shared_contract, "cautious-low", "confidence-policy")
        check_contains(run, shared_contract, "fair but not automatic", "confidence-policy")
        check_contains(run, shared_contract, "confidence_gap_closures", "confidence-policy")
        check_contains(run, shared_contract, "confidence_recovery", "confidence-policy")


def check_agents(run: CalibrationRun) -> None:
    """Run registration, schema, confidence, model, and effort checks for agents."""
    for agent in AGENTS:
        if run.paths.project_cfg is not None:
            check_contains(run, run.paths.project_cfg, rf"\[agents\.{agent}\]", "agent-registration-project")
        agent_file = role_file(run, agent)
        if not agent_file.exists():
            run.fail_and_leak("agent-schema-all", f"missing-agent-file:{agent}")
            run.mark_check_failed("agent-model-policy")
            run.mark_check_failed("agent-effort-policy")
            continue
        patterns = (
            (
                r"^role_id:",
                r"^name:",
                r"^model:",
                r"^model_reasoning_effort:",
                r"^approval_policy:",
                r"^sandbox_mode:",
                r"^fallback_modes:",
                "Trigger and skip boundaries",
                "Evidence ownership",
                "Execution constraints",
                "Handover contract",
                "Confidence contract",
            )
            if run.paths.layout == "plugin"
            else (
                r"^name\s*=",
                "developer_instructions",
                "Scope",
                "Boundaries",
                "Evidence Standard",
                "TRIGGER when",
                "SKIP when",
                "NOT for",
                "Output Format",
                "Output Contract",
            )
        )
        for pattern in patterns:
            check_contains(
                run,
                agent_file,
                pattern,
                "agent-schema-all"
                if pattern
                in {
                    r"^role_id:",
                    r"^name:",
                    r"^model:",
                    r"^model_reasoning_effort:",
                    r"^approval_policy:",
                    r"^sandbox_mode:",
                    r"^fallback_modes:",
                    r"^name\s*=",
                    "developer_instructions",
                }
                else "native-agent-contract",
            )
        if run.paths.layout == "source":
            check_contains(run, agent_file, r"\.codex/AGENTS\.md confidence contract", "confidence-policy")
        check_agent_model(run, agent, agent_file)
        check_agent_effort(run, agent, agent_file)


def check_native_runtime_leaks(run: CalibrationRun) -> None:
    """Scan native skills and agents for non-Codex runtime vocabulary leaks."""
    targets = sorted(run.paths.skills_dir.glob("*/SKILL.md"))
    if run.paths.layout == "plugin":
        targets.extend(sorted(run.paths.roles_dir.glob("*/ROLE.md")))
    else:
        targets.extend(sorted(run.paths.roles_dir.glob("*.toml")))
    patterns = {
        "external-path-variable": re.compile("CLAUDE_PLUGIN_ROOT"),
        "interactive-widget": re.compile("AskUserQuestion"),
        "task-widget-create": re.compile("TaskCreate"),
        "task-widget-update": re.compile("TaskUpdate"),
        "background-runner": re.compile("run_in_background"),
        "web-fetch-tool": re.compile("WebFetch"),
        "web-search-tool": re.compile("WebSearch"),
        "frontmatter-tools": re.compile(r"^tools\s*:", re.MULTILINE),
        "frontmatter-max-turns": re.compile(r"^maxTurns\s*:", re.MULTILINE),
        "frontmatter-isolation": re.compile(r"^isolation\s*:", re.MULTILINE),
        "frontmatter-memory": re.compile(r"^memory\s*:", re.MULTILINE),
        "frontmatter-tool-allowlist": re.compile("allowed-tools"),
        "frontmatter-disable-model": re.compile("disable-model-invocation"),
    }
    found = 0
    for path in targets:
        text = read_text(path)
        for label, pattern in patterns.items():
            if pattern.search(text):
                run.append_leak(f"native-runtime-leak:{label}:{path.relative_to(run.paths.asset_root)}")
                found += 1
    if found:
        run.mark_check_failed("native-runtime-leakage")
        run.fails += found
        run.leaks += found


def is_executable(path: Path) -> bool:
    """Return whether a path exists and is executable."""
    return path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK))


def cli_argv(path: Path, *arguments: str | Path) -> list[str | Path]:
    """Build a portable argv vector for a Python helper or native executable."""
    prefix: list[str | Path] = [sys.executable, path] if path.suffix == ".py" else [path]
    return [*prefix, *arguments]


def gate_argv(command: str) -> list[str]:
    """Split a fixture gate command, binding a bare interpreter token to the running Python.

    Windows ships no ``python3`` on PATH and a POSIX ``python`` may resolve to Python 2, so the interpreter already
    executing this calibration run is the only portable target.
    """
    argv = shlex.split(command)
    if argv and argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    return argv


def run_command(
    args: list[str | Path],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return captured text output."""
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def check_python_syntax(run: CalibrationRun, path: Path, label: str) -> None:
    """Compile a Python file and record a shared-script selftest failure on syntax errors."""
    if not path.exists():
        run.fail_and_leak("shared-script-selftests", f"shared-script-missing:{path}")
        return
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(run.paths.out_dir / "pycache")
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        run.fail_and_leak("shared-script-selftests", f"shared-script-syntax:{label}")


def check_shared_scripts(run: CalibrationRun) -> None:
    """Check shared helper executability and smoke selftests."""
    cli_paths = {
        "calibration-run": run.paths.run_py,
        "calibration-live-ab": run.paths.live_ab_runner,
        "calibration-score": run.paths.behavioral_scorer,
        "collect-diff": run.paths.collect_diff,
        "collect-pr": run.paths.collect_pr,
        "find-review-report": run.paths.find_review_report,
        "run-gates": run.paths.run_gates,
        "escalation-ledger": run.paths.shared_dir / "escalation_ledger.py",
        "code-review-validate-artifacts": run.paths.code_review_validate_artifacts,
        "code-review-review-routing": run.paths.code_review_review_routing,
        "select-git-remote": run.paths.select_git_remote,
        "validate-artifacts": run.paths.validate_artifacts,
        "write-result": run.paths.write_result_py,
        "final-handoff": run.paths.final_handoff_py,
    }
    if run.paths.layout == "plugin":
        cli_paths["create-run"] = run.paths.create_run
        cli_paths["codemap-adapter"] = run.paths.codemap_adapter
        assert run.paths.github_read is not None
        cli_paths["github-read"] = run.paths.github_read
    if run.paths.codex_harness.exists():
        if run.paths.layout == "source":
            cli_paths["codex-harness"] = run.paths.codex_harness
    elif run.paths.layout == "source" and (run.paths.root / ".git").exists():
        run.fail_and_leak("shared-script-selftests", f"shared-script-missing:{run.paths.codex_harness}")
    else:
        run.append_check("shared-script-help:codex-harness=not-applicable:plugin-layout")
    for script in cli_paths.values():
        if not is_executable(script):
            run.fail_and_leak("shared-script-selftests", f"shared-script-not-executable:{script}")

    discovered: set[Path] = set()
    discovery_roots = (
        run.paths.calibration_dir.glob("*.py"),
        run.paths.shared_dir.glob("*.py"),
        (run.paths.skills_dir / "code-review").glob("*.py"),
    )
    if run.paths.layout == "source":
        discovery_roots += (run.paths.shared_dir.glob("*.sh"),)
    for candidates in discovery_roots:
        discovered.update(path for path in candidates if read_text(path).startswith("#!"))
    if run.paths.layout == "source":
        discovered.update(
            path for path in (run.paths.root / ".github").glob("*.sh") if read_text(path).startswith("#!")
        )
    if discovered != set(cli_paths.values()):
        missing = sorted(str(path) for path in discovered - set(cli_paths.values()))
        stale = sorted(str(path) for path in set(cli_paths.values()) - discovered)
        run.fail_and_leak(
            "shared-script-selftests",
            f"shared-script-help-roster-mismatch:missing={missing}:stale={stale}",
        )

    embedded_python_marker = "python3" + " -"
    if embedded_python_marker in read_text(run.paths.run_py):
        run.fail_and_leak("shared-script-selftests", f"shared-script-embedded-python:{run.paths.run_py}")
    if embedded_python_marker in read_text(run.paths.write_result_py):
        run.fail_and_leak("shared-script-selftests", f"shared-script-embedded-python:{run.paths.write_result_py}")
    check_python_syntax(run, run.paths.run_py, "run.py")
    check_python_syntax(run, run.paths.write_result_py, "write-result.py")
    check_python_syntax(run, run.paths.final_handoff_py, "final_handoff.py")
    check_python_syntax(run, run.paths.select_git_remote, "select-git-remote.py")
    check_python_syntax(run, run.paths.behavioral_scorer, "score_behavioral.py")
    check_python_syntax(run, run.paths.live_ab_runner, "run_live_ab.py")
    check_python_syntax(run, run.paths.live_contract, "live_contract.py")
    check_python_syntax(run, run.paths.code_review_validate_artifacts, "code-review/validate_artifacts.py")
    check_python_syntax(run, run.paths.code_review_review_routing, "code-review/review_routing.py")
    for label, script in cli_paths.items():
        result = run_command(cli_argv(script, "--help"))
        if result.returncode != 0 or "usage" not in result.stdout.lower():
            run.fail_and_leak("shared-script-selftests", f"shared-script-help-invalid:{label}")
    run_selftests(run)


def run_selftests(run: CalibrationRun) -> None:
    """Run smoke tests for shared helpers that are safe offline."""
    selftest_dir = run.paths.out_dir / "selftest"
    selftest_dir.mkdir(parents=True, exist_ok=True)
    success_command = "exit 0"
    failure_command = "exit 1"
    timeout_command = "Start-Sleep -Seconds 2" if sys.platform == "win32" else "sleep 2"

    if is_executable(run.paths.run_gates):
        result = run_command(
            cli_argv(
                run.paths.run_gates,
                "--out",
                selftest_dir / "gates",
                "--lint",
                success_command,
                "--format",
                success_command,
                "--types",
                success_command,
                "--tests",
                success_command,
                "--review",
                success_command,
            )
        )
        if result.returncode != 0 or not (selftest_dir / "gates" / "gates.json").exists():
            run.fail_and_leak("shared-script-selftests", "selftest-missing:gates.json")

        failed_gates_dir = selftest_dir / "failed-gates"
        failed_result = run_command(
            cli_argv(
                run.paths.run_gates,
                "--out",
                failed_gates_dir,
                "--lint",
                failure_command,
                "--format",
                success_command,
                "--types",
                success_command,
                "--tests",
                success_command,
                "--review",
                success_command,
            )
        )
        failed_payload = json.loads((failed_gates_dir / "gates.json").read_text(encoding="utf-8"))
        if failed_result.returncode == 0 or failed_payload.get("status") != "fail":
            run.fail_and_leak("shared-script-selftests", "selftest-fail-open:run-gates")

        exit_125_dir = selftest_dir / "exit-125-gates"
        exit_125 = run_command(
            cli_argv(
                run.paths.run_gates,
                "--out",
                exit_125_dir,
                "--lint",
                "exit 125",
                "--format",
                success_command,
                "--skip-types",
                "synthetic no typed target",
                "--tests",
                success_command,
                "--review",
                success_command,
            )
        )
        exit_125_payload = json.loads((exit_125_dir / "gates.json").read_text(encoding="utf-8"))
        lint_gate = next(item for item in exit_125_payload["checks"] if item["id"] == "lint")
        if exit_125.returncode == 0 or lint_gate.get("status") != "fail" or lint_gate.get("exit_code") != 125:
            run.fail_and_leak("shared-script-selftests", "selftest-fail-open:run-gates-exit-125")

        timeout_dir = selftest_dir / "timeout-gates"
        timed_out = run_command(
            cli_argv(
                run.paths.run_gates,
                "--out",
                timeout_dir,
                "--lint",
                timeout_command,
                "--format",
                success_command,
                "--skip-types",
                "synthetic no typed target",
                "--tests",
                success_command,
                "--review",
                success_command,
                "--timeout-seconds",
                "1",
            )
        )
        timeout_payload = json.loads((timeout_dir / "gates.json").read_text(encoding="utf-8"))
        if timed_out.returncode != 124 or timeout_payload.get("status") != "timeout":
            run.fail_and_leak("shared-script-selftests", "selftest-fail-open:run-gates-timeout")

    if is_executable(run.paths.write_result_py):
        metadata = {
            "confidence_gaps": ["synthetic writer selftest does not execute project behavior"],
            "confidence_gap_closures": [
                {
                    "gap": "synthetic writer selftest does not execute project behavior",
                    "status": "unresolved",
                    "rationale": "writer selftest validates result shape only",
                }
            ],
            "confidence_recovery": {
                "initial_confidence": 0.9,
                "final_confidence": 0.95,
                "status": "fair",
                "evidence": ["write-result selftest produced a JSON artifact"],
                "recovery_actions": ["included mandatory confidence metadata"],
                "remaining_limits": [],
            },
        }
        writer_result = selftest_dir / "gates" / "result.json"
        result = run_write_result(run, writer_result, metadata)
        if result.returncode != 0 or not writer_result.exists():
            run.fail_and_leak("shared-script-selftests", "selftest-missing:result.json")

    if is_executable(run.paths.collect_diff):
        diff_repo = selftest_dir / "collect-diff-repo"
        diff_repo.mkdir(parents=True, exist_ok=True)
        fixture = diff_repo / "fixture.txt"
        fixture.write_text("before\n", encoding="utf-8")
        setup_commands = (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "codex-selftest@example.invalid"],
            ["git", "config", "user.name", "Codex Selftest"],
            ["git", "add", "fixture.txt"],
            ["git", "commit", "-q", "-m", "selftest fixture"],
        )
        if any(run_command(command, cwd=diff_repo).returncode != 0 for command in setup_commands):
            run.fail_and_leak("shared-script-selftests", "selftest-setup:collect-diff")
        fixture.write_text("after\n", encoding="utf-8")
        result = run_command(
            cli_argv(run.paths.collect_diff, "--scope", "working-tree", "--out", selftest_dir / "diff"),
            cwd=diff_repo,
        )
        expected_files = ("status.txt", "diff.patch", "files.txt", "diffstat.txt", "numstat.txt", "untracked.txt")
        for expected in expected_files:
            if result.returncode != 0 or not (selftest_dir / "diff" / expected).exists():
                run.fail_and_leak("shared-script-selftests", f"selftest-missing:collect-diff:{expected}")

    if is_executable(run.paths.collect_pr):
        result = run_command([sys.executable, "-m", "py_compile", run.paths.collect_pr])
        if result.returncode != 0:
            run.fail_and_leak("shared-script-selftests", "selftest-syntax:collect-pr")
    if run.paths.github_read is not None and is_executable(run.paths.github_read):
        result = run_command([sys.executable, "-m", "py_compile", run.paths.github_read])
        if result.returncode != 0:
            run.fail_and_leak("shared-script-selftests", "selftest-syntax:github-read")
    if run.paths.select_git_remote.exists():
        selftest_select_git_remote(run, selftest_dir)
    if run.paths.live_ab_runner.exists() and run.paths.live_route_policy.exists():
        selftest_live_ab_contract(run, selftest_dir)
    if run.paths.code_review_validate_artifacts.exists():
        selftest_review_validator(run, selftest_dir)

    if is_executable(run.paths.find_review_report):
        selftest_find_review_report(run, selftest_dir)

    if is_executable(run.paths.validate_artifacts) and is_executable(run.paths.write_result_py):
        selftest_validate_artifacts(run, selftest_dir)
        if run.paths.layout == "source":
            selftest_code_remediate_pr_identity(run)
        else:
            run.append_check("code-remediate-validator-selftest=not-applicable:source-agent-config-required")


def selftest_select_git_remote(run: CalibrationRun, selftest_dir: Path) -> None:
    """Verify authoritative PR URL matching in a multi-remote local repository."""
    repo = selftest_dir / "remote-selection-repo"
    repo.mkdir(parents=True, exist_ok=True)
    commands = (
        ["git", "init", "-q", repo],
        ["git", "-C", repo, "remote", "add", "origin", "git@github.com:fork/repo.git"],
        ["git", "-C", repo, "remote", "add", "upstream", "https://github.com/owner/repo.git"],
    )
    if any(run_command(command).returncode != 0 for command in commands):
        run.fail_and_leak("shared-script-selftests", "selftest-setup:select-git-remote")
        return
    selected = run_command(
        [
            sys.executable,
            run.paths.select_git_remote,
            "--expected-url",
            "https://github.com/owner/repo/pull/17",
            "--cwd",
            repo,
        ]
    )
    if selected.returncode != 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:select-git-remote")
        return
    payload = json.loads(selected.stdout)
    if payload.get("remote") != "upstream":
        run.fail_and_leak("shared-script-selftests", "selftest-wrong:select-git-remote-fork-origin")
    missing = run_command(
        [
            sys.executable,
            run.paths.select_git_remote,
            "--expected-url",
            "https://github.com/owner/other/pull/1",
            "--cwd",
            repo,
        ]
    )
    if missing.returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:select-git-remote")


def selftest_live_ab_contract(run: CalibrationRun, selftest_dir: Path) -> None:
    """Verify paid-run planning, meaningful tool fixtures, and mixed-scope scoring."""
    live_dir = selftest_dir / "live-ab"
    runner_text = read_text(run.paths.live_ab_runner)
    for forbidden_or_required, should_exist in (
        ('"uniqueItems"', False),
        ("stdin=subprocess.DEVNULL", True),
        ("live-call-failed", True),
        ("live-paid-run-disabled-in-ci", True),
        ('choices=("chatgpt-subscription",)', True),
        ("_write_input_snapshot(", True),
        ("role_context(snapshot_root, role, layout)", True),
    ):
        if (forbidden_or_required in runner_text) is not should_exist:
            run.fail_and_leak(
                "shared-script-selftests",
                f"selftest-failed:live-ab-runner-safety:{forbidden_or_required}",
            )
    plan = run_command(
        [
            sys.executable,
            run.paths.live_ab_runner,
            "--cases",
            run.paths.behavioral_cases,
            "--tasks",
            run.paths.live_ab_tasks,
            "--route-policy",
            run.paths.live_route_policy,
            "--out",
            live_dir / "planned-run",
            "--root",
            run.paths.root,
            "--layout",
            run.paths.layout,
        ]
    )
    try:
        plan_payload = json.loads(plan.stdout)
    except json.JSONDecodeError:
        plan_payload = {}
    scopes = plan_payload.get("evidence_scopes", {})
    if (
        plan.returncode != 0
        or plan_payload.get("paid_model_calls") != 64
        or plan_payload.get("campaigns") != 2
        or plan_payload.get("strict_acceptance_possible") is not True
        or plan_payload.get("required_confirmation") != "--confirm-paid-run=chatgpt-subscription"
        or any(value != ["classification", "tool-use"] for value in scopes.values())
        or len(scopes) != 3
    ):
        run.fail_and_leak("shared-script-selftests", "selftest-failed:live-ab-plan")
        return
    if (live_dir / "planned-run").exists():
        run.fail_and_leak("shared-script-selftests", "selftest-failed:live-ab-plan-created-output")
        return

    paid_args = [
        sys.executable,
        run.paths.live_ab_runner,
        "--cases",
        run.paths.behavioral_cases,
        "--tasks",
        run.paths.live_ab_tasks,
        "--route-policy",
        run.paths.live_route_policy,
        "--root",
        run.paths.root,
        "--layout",
        run.paths.layout,
        "--confirm-paid-run",
        "chatgpt-subscription",
    ]
    ci_out = live_dir / "ci-blocked-run"
    ci_env = {**os.environ, "CI": "1", "GITHUB_ACTIONS": "1", "OPENAI_API_KEY": ""}
    ci_blocked = run_command([*paid_args, "--out", ci_out], env=ci_env)
    if ci_blocked.returncode == 0 or "live-paid-run-disabled-in-ci" not in ci_blocked.stderr or ci_out.exists():
        run.fail_and_leak("shared-script-selftests", "selftest-failed:live-ab-ci-paid-guard")
        return

    api_out = live_dir / "api-key-blocked-run"
    api_env = {
        **os.environ,
        "CI": "",
        "GITHUB_ACTIONS": "",
        "OPENAI_API_KEY": "selftest-must-not-be-used",
    }
    api_blocked = run_command([*paid_args, "--out", api_out], env=api_env)
    if (
        api_blocked.returncode == 0
        or "live-paid-run-api-key-auth-disallowed" not in api_blocked.stderr
        or api_out.exists()
    ):
        run.fail_and_leak("shared-script-selftests", "selftest-failed:live-ab-api-key-paid-guard")
        return

    cases_payload = json.loads(run.paths.behavioral_cases.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in cases_payload["cases"]}
    tasks_payload = json.loads(run.paths.live_ab_tasks.read_text(encoding="utf-8"))
    policy_payload = json.loads(run.paths.live_route_policy.read_text(encoding="utf-8"))
    tasks = tasks_payload["routes"]
    policy = policy_payload["routes"]
    snapshot_roles = {task["role"] for route_tasks in tasks.values() for task in route_tasks}
    snapshot_source = live_dir / "snapshot-source"
    if run.paths.layout == "plugin":
        for role in snapshot_roles:
            source = role_file(run, role)
            destination = snapshot_source / "roles" / role / "ROLE.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        source_codex = snapshot_source / ".codex"
        source_agents = source_codex / "agents"
        source_agents.mkdir(parents=True)
        (source_codex / "AGENTS.md").write_text(
            (run.paths.asset_root / "AGENTS.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        for role in snapshot_roles:
            source = role_file(run, role)
            (source_agents / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("calibration_live_ab_runner", run.paths.live_ab_runner)
    if spec is None or spec.loader is None:
        run.fail_and_leak("shared-script-selftests", "selftest-import:live-ab-runner")
        return
    live_runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live_runner)
    snapshot_root = live_runner._write_input_snapshot(
        live_dir / "snapshot-run",
        cases_payload,
        tasks_payload,
        policy_payload,
        snapshot_source,
        snapshot_roles,
        run.paths.layout,
    )
    snapshot_role = sorted(snapshot_roles)[0]
    snapshotted_context = role_context(snapshot_root, snapshot_role, run.paths.layout)
    source_role = (
        snapshot_source / "roles" / snapshot_role / "ROLE.md"
        if run.paths.layout == "plugin"
        else snapshot_source / ".codex" / "agents" / f"{snapshot_role}.toml"
    )
    source_role.write_text("changed after snapshot\n", encoding="utf-8")
    manifest = json.loads((live_dir / "snapshot-run" / "inputs" / "manifest.json").read_text(encoding="utf-8"))
    if (
        role_context(snapshot_root, snapshot_role, run.paths.layout) != snapshotted_context
        or manifest.get("roles") != sorted(snapshot_roles)
        or manifest.get("score_inputs", {}).get("root") != "inputs/root"
    ):
        run.fail_and_leak("shared-script-selftests", "selftest-failed:live-ab-input-snapshot")
        return
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows: list[dict[str, Any]] = []
    for route_id, route in sorted(policy.items()):
        for campaign_index in (1, 2):
            campaign_id = f"selftest:c{campaign_index}"
            for index, task in enumerate(tasks[route_id], start=1):
                case = cases[task["case_id"]]
                evidence_scope = task.get("evidence_scope", "classification")
                if evidence_scope == "tool-use" and campaign_index == 1:
                    fixture_dir = live_dir / "fixtures" / route_id
                    fixture_dir.mkdir(parents=True, exist_ok=True)
                    for relative, content in task["fixture_files"].items():
                        destination = fixture_dir / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_text(content, encoding="utf-8")
                    gate = run_command(gate_argv(task["gate_command"]), cwd=fixture_dir)
                    if gate.returncode == 0:
                        run.fail_and_leak("shared-script-selftests", f"selftest-vacuous:live-ab-fixture:{route_id}")
                pair_id = f"{campaign_id}:{route_id}:{index}"
                expected_prompt_sha = prompt_sha256(
                    build_prompt(
                        case,
                        candidate_findings(case["id"], cases),
                        task,
                        role_context(run.paths.asset_root, task["role"], run.paths.layout),
                    )
                )
                for pair_role in ("baseline", "candidate"):
                    expected = case["expected_findings"]
                    reported = [] if route_id == "sol-critical-high" and pair_role == "baseline" else expected
                    baseline_gate_failure = (
                        route_id == "sol-critical-high"
                        and campaign_index == 1
                        and index == 1
                        and pair_role == "baseline"
                    )
                    rows.append(
                        {
                            "case_id": case["id"],
                            "target": case["target"],
                            "source": "live-selftest",
                            "run_id": pair_id,
                            "observed_at": observed_at,
                            "reported_findings": reported,
                            "confidence": 0.0 if not reported else 1.0,
                            "campaign_id": campaign_id,
                            "pair_id": pair_id,
                            "pair_role": pair_role,
                            "route_id": route_id,
                            "model": route[f"{pair_role}_model"],
                            "reasoning_effort": route["effort"],
                            "role": task["role"],
                            "prompt_sha256": expected_prompt_sha,
                            "task_type": task["task_type"],
                            "task_contract_sha256": task_contract_sha256(task),
                            "evidence_scope": evidence_scope,
                            "input_tokens": 100,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                            "latency_ms": 1,
                            "outcome": "fail" if baseline_gate_failure else "pass",
                            "tool_failure_count": 0,
                            "check_failure_count": 1 if baseline_gate_failure else 0,
                            "estimated_cost_units": 140.0,
                            "pricing_ref": "normalized-token-v1:uncached+0.1*cached+4*output",
                        }
                    )
    observations = live_dir / "observations.jsonl"
    observations.parent.mkdir(parents=True, exist_ok=True)
    fixture_rows = run.paths.behavioral_observations.read_text(encoding="utf-8")
    observations.write_text(fixture_rows + "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    scorer_args: list[str | Path] = [
        sys.executable,
        run.paths.behavioral_scorer,
        "--cases",
        run.paths.behavioral_cases,
        "--observations",
        observations,
        "--route-policy",
        run.paths.live_route_policy,
        "--tasks",
        run.paths.live_ab_tasks,
        "--root",
        run.paths.asset_root,
        "--layout",
        run.paths.layout,
        "--require-live-routes",
        "--out",
        live_dir / "scored.json",
    ]
    scored = run_command(scorer_args)
    if scored.returncode != 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:live-ab-mixed-scope-score")
        return
    classification_only = live_dir / "classification-only.jsonl"
    classification_only.write_text(
        fixture_rows + "".join(json.dumps({**row, "evidence_scope": "classification"}) + "\n" for row in rows),
        encoding="utf-8",
    )
    scorer_args[scorer_args.index(observations)] = classification_only
    scorer_args[-1] = live_dir / "classification-only-scored.json"
    if run_command(scorer_args).returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:live-ab-missing-tool-scope")
    single_campaign = live_dir / "single-campaign.jsonl"
    single_campaign.write_text(
        fixture_rows + "".join(json.dumps(row) + "\n" for row in rows if row["campaign_id"].endswith(":c1")),
        encoding="utf-8",
    )
    scorer_args[scorer_args.index(classification_only)] = single_campaign
    scorer_args[-1] = live_dir / "single-campaign-scored.json"
    if run_command(scorer_args).returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:live-ab-single-campaign")
    substituted_tasks = live_dir / "substituted-tasks.jsonl"
    substituted_tasks.write_text(
        fixture_rows
        + "".join(
            json.dumps({**row, "role": "qa-specialist"}) + "\n"
            if row["route_id"] == "luna-support-high" and row["case_id"] == tasks["luna-support-high"][0]["case_id"]
            else json.dumps(row) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    scorer_args[scorer_args.index(single_campaign)] = substituted_tasks
    scorer_args[-1] = live_dir / "substituted-tasks-scored.json"
    if run_command(scorer_args).returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:live-ab-task-substitution")
    arbitrary_prompts = live_dir / "arbitrary-prompts.jsonl"
    arbitrary_prompts.write_text(
        fixture_rows + "".join(json.dumps({**row, "prompt_sha256": "f" * 64}) + "\n" for row in rows),
        encoding="utf-8",
    )
    scorer_args[scorer_args.index(substituted_tasks)] = arbitrary_prompts
    scorer_args[-1] = live_dir / "arbitrary-prompts-scored.json"
    if run_command(scorer_args).returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:live-ab-prompt-substitution")
    arbitrary_contracts = live_dir / "arbitrary-task-contracts.jsonl"
    arbitrary_contracts.write_text(
        fixture_rows + "".join(json.dumps({**row, "task_contract_sha256": "e" * 64}) + "\n" for row in rows),
        encoding="utf-8",
    )
    scorer_args[scorer_args.index(arbitrary_prompts)] = arbitrary_contracts
    scorer_args[-1] = live_dir / "arbitrary-task-contracts-scored.json"
    if run_command(scorer_args).returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:live-ab-task-contract-substitution")


def selftest_review_validator(run: CalibrationRun, selftest_dir: Path) -> None:
    """Exercise rollout-bound review validation and TRIVIAL semantic routing."""
    out = selftest_dir / "review-validator"
    specialists = out / "specialists"
    codex_home = out / "codex-home"
    sessions = codex_home / "sessions"
    specialists.mkdir(parents=True, exist_ok=True)
    sessions.mkdir(parents=True, exist_ok=True)
    (out / "files.txt").write_text("src/runtime.py\n", encoding="utf-8")
    (out / "untracked.txt").write_text("", encoding="utf-8")
    (out / "numstat.txt").write_text("5\t5\tsrc/runtime.py\n", encoding="utf-8")
    diff = out / "diff.patch"
    diff.write_text("diff --git a/src/runtime.py b/src/runtime.py\n", encoding="utf-8")
    review_input_sha = hashlib.sha256(diff.read_bytes()).hexdigest()
    role_card = run.paths.code_review_validate_artifacts.resolve().parents[2] / "roles" / "qa-specialist" / "ROLE.md"
    role_card_sha = hashlib.sha256(role_card.read_bytes()).hexdigest()
    context = specialists / "qa-specialist-context.md"
    context.write_text("# QA context\n\nInspect the runtime behavior change.\n", encoding="utf-8")
    context_sha = hashlib.sha256(context.read_bytes()).hexdigest()
    agent_name = f"review_qa_specialist_{context_sha[:12]}_a1"
    agent_path = f"/root/{agent_name}"
    output = specialists / "qa-specialist.md"
    message = (
        f"<!-- codex-review-provenance role=qa-specialist run=selftest-review "
        f"input={review_input_sha} context={context_sha} attempt=1 -->\nNo findings."
    )
    output.write_text(message + "\n", encoding="utf-8")
    output_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    parent_id = "parent-selftest"
    child_id = "child-selftest"
    turn_id = "turn-selftest"
    event_id = "event-selftest"
    spawn_call_id = "spawn-selftest"
    task_started = 1_800_000_000
    task_completed = task_started + 5
    parent_rows = [
        {"type": "session_meta", "payload": {"id": parent_id}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": spawn_call_id,
                "arguments": json.dumps(
                    {
                        "agent_type": "qa-specialist",
                        "fork_turns": "3",
                        "message": "Synthetic role-bound QA context.",
                        "task_name": agent_name,
                    }
                ),
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": parent_id,
                "turn_id": "parent-turn",
                "started_at_ms": task_started * 1000,
                "completed_at_ms": task_started * 1000 + 100,
                "item": {
                    "type": "SubAgentActivity",
                    "id": event_id,
                    "agent_thread_id": child_id,
                    "agent_path": agent_path,
                    "kind": "started",
                },
            },
        },
        {
            "timestamp": datetime.fromtimestamp(task_completed, timezone.utc).isoformat(),
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "id": "join-selftest",
                "author": agent_path,
                "recipient": "/root",
                "content": [
                    {
                        "type": "input_text",
                        "text": "\n".join(
                            (
                                "Message Type: FINAL_ANSWER",
                                "Task name: /root",
                                f"Sender: {agent_path}",
                                "Payload:",
                                message,
                            )
                        ),
                    }
                ],
            },
        },
    ]
    (sessions / f"rollout-{parent_id}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in parent_rows), encoding="utf-8"
    )
    child_rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "agent_path": agent_path,
                "agent_role": "qa-specialist",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "agent_path": agent_path,
                            "agent_role": "qa-specialist",
                        }
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_settings": {
                    "model": DEFAULT_MODEL,
                    "reasoning_effort": "high",
                    "approval_policy": "never",
                    "permission_profile": {
                        "type": "managed",
                        "file_system": {"type": "restricted", "entries": []},
                        "network": "restricted",
                    },
                },
            },
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": turn_id,
                "model": DEFAULT_MODEL,
                "effort": "high",
                "approval_policy": "never",
                "sandbox_policy": {"type": "read-only"},
                "permission_profile": {"type": "managed", "network": "restricted"},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
                "started_at": task_started,
                "completed_at": task_completed,
                "duration_ms": (task_completed - task_started) * 1000,
                "last_agent_message": message,
            },
        },
    ]
    child_rollout = sessions / f"rollout-{child_id}.jsonl"
    child_rollout.write_text("".join(json.dumps(row) + "\n" for row in child_rows), encoding="utf-8")

    signals = {
        key: key == "behavior_change"
        for key in (
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
        )
    }
    routing = {
        "schema_version": 1,
        "risk_tier": "TRIVIAL",
        "mechanical_risk_tier": "TRIVIAL",
        "mechanical_risk_evidence": ["files=1", "changed_lines=10", "unknown_size_rows=0"],
        "signals": signals,
        "signal_evidence": {
            key: ["synthetic positive" if value else "synthetic negative"] for key, value in signals.items()
        },
        "triggered_roles": ["qa-specialist"],
        "trigger_reasons": {"qa-specialist": ["behavior_change"]},
    }
    (out / "review-routing.json").write_text(json.dumps(routing, indent=2) + "\n", encoding="utf-8")
    attempt = {
        "agent_path": agent_path,
        "agent_thread_id": child_id,
        "attempt": 1,
        "context_path": str(context),
        "context_sha256": context_sha,
        "effort": "high",
        "event_id": event_id,
        "model": DEFAULT_MODEL,
        "output_path": str(output),
        "output_sha256": output_sha,
        "status": "completed",
        "turn_id": turn_id,
    }
    specialist_pass = {
        "role": "qa-specialist",
        "role_card_sha256": role_card_sha,
        "axis": "tests",
        "mode": "spawned",
        "trigger": "behavior_change",
        "confidence": 0.95,
        "blocking_findings": 0,
        "output_path": str(output),
        "attempts": [attempt],
        "selected_attempt": 1,
    }
    execution_plan = out / "execution-plan.json"
    execution_plan.write_text(
        json.dumps(
            {
                "review_run_id": "selftest-review",
                "capability_policy": {"task_sensitivity": "non-sensitive"},
                "consumer_policy": {
                    "consumer_id": "code-review",
                    "capability": "portable-read-only",
                    "promotion_status": "promoted",
                    "parent_mutations": "serial",
                    "canonical_gates": "serial",
                },
                "token_budgets": [
                    {
                        "wave_id": "review-wave",
                        "ceiling_tokens": 500,
                        "node_order": ["qa-specialist"],
                        "reservations": {"qa-specialist": 400},
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    read_only_controls = {
        "sandbox_mode": "read-only",
        "write_paths": [],
        "network": "restricted",
        "credentials": "unverified",
    }
    execution = {
        "schema_version": 2,
        "run_id": "selftest-review",
        "plan_sha256": hashlib.sha256(execution_plan.read_bytes()).hexdigest(),
        "claimed_mode": "serial",
        "configured_limit": 4,
        "write_approval": None,
        "capability_evidence": {
            "tier": "portable",
            "task_sensitivity": "non-sensitive",
            "network": {
                "mode": "restricted",
                "approval_policy": "never",
                "external_events": [],
            },
            "credentials": {
                "context_scan": "passed",
                "filesystem_isolation": "unverified",
            },
        },
        "stages": [
            {
                "stage_id": "review",
                "depends_on": [],
                "wave_id": "review-wave",
                "nodes": [
                    {
                        "node_id": "qa-specialist",
                        "role_id": "qa-specialist",
                        "role_card_sha256": role_card_sha,
                        "context_path": "specialists/qa-specialist-context.md",
                        "context_sha256": context_sha,
                        "mutation": "read-only",
                        "owned_paths": [],
                        "resource_locks": [],
                        "requested_controls": read_only_controls,
                        "observed_controls": {**read_only_controls, "enforced": True},
                        "attempts": [
                            {
                                "attempt": 1,
                                "status": "completed",
                                "error_type": None,
                                "agent_path": agent_path,
                                "agent_thread_id": child_id,
                                "spawn_call_id": spawn_call_id,
                                "turn_id": turn_id,
                                "start_event": {"event_id": event_id, "sequence": 1},
                                "terminal_event": {"event_id": turn_id, "sequence": 2},
                                "output_path": "specialists/qa-specialist.md",
                                "output_sha256": output_sha,
                            }
                        ],
                        "selected_attempt": 1,
                        "verifier_status": "passed",
                        "unresolved": [],
                        "join_event": {"event_id": "join-selftest", "sequence": 3},
                    }
                ],
            }
        ],
    }
    execution_manifest = out / "execution-manifest.json"
    execution_manifest.write_text(json.dumps(execution, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 3,
        "review_run_id": "selftest-review",
        "parent_thread_id": parent_id,
        "review_input_sha256": review_input_sha,
        "runtime_execution": {
            "manifest_path": execution_manifest.name,
            "manifest_sha256": hashlib.sha256(execution_manifest.read_bytes()).hexdigest(),
            "plan_path": execution_plan.name,
        },
        "passes": [specialist_pass],
    }
    manifest_path = out / "specialist-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    sections = (
        "Decision Summary",
        "Scope",
        "Risk Tier",
        "Files Inspected",
        "Specialist Passes",
        "Specialist Manifest",
        "Findings",
        "No-Finding Residual Risks",
        "Confidence Gaps",
        "Confidence Calibration",
    )
    (out / "review-notes.md").write_text(
        "# Review\n\n" + "\n\n".join(f"## {section}\n\nSynthetic evidence." for section in sections) + "\n",
        encoding="utf-8",
    )
    metadata = {
        "scope": "working-tree",
        "risk_tier": "TRIVIAL",
        "review_decision": {"recommendation": "accept-as-is", "summary": "Clean.", "rationale": "Synthetic pass."},
        "confidence_gaps": ["synthetic fixture does not inspect production code"],
        "confidence_gap_closures": [
            {
                "gap": "synthetic fixture does not inspect production code",
                "status": "unresolved",
                "rationale": "This selftest validates provenance and routing only",
            }
        ],
        "confidence_recovery": {
            "initial_confidence": 0.9,
            "final_confidence": 0.95,
            "status": "fair",
            "evidence": ["synthetic rollout-shaped fixture"],
            "recovery_actions": ["validated parent, child, model, context, and output bindings"],
            "remaining_limits": ["encrypted task plaintext cannot be inspected"],
        },
        "specialist_manifest": str(manifest_path),
        "specialist_passes": [specialist_pass],
        "review_run_id": "selftest-review",
        "review_input_sha256": review_input_sha,
        "fanout_substituted": False,
        "independence_satisfied": True,
        "independence_required": True,
        "execution_mode": "serial",
        "execution_evidence_level": "portable-read-restricted",
        "write_parallel_eligible": False,
    }
    result_path = out / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "checks_failed": [],
                "confidence": 0.95,
                "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                "metadata": metadata,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    command: list[str | Path] = [
        sys.executable,
        run.paths.code_review_validate_artifacts,
        "--out",
        out,
        "--result",
        result_path,
        "--codex-home",
        codex_home,
        "--project-root",
        run.paths.root,
        "--parent-thread-id",
        parent_id,
    ]
    if run_command(command).returncode != 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:review-validator-positive")
        return
    manifest["passes"][0]["attempts"][0]["agent_path"] = "/root/unbound_agent"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if run_command(command).returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:review-validator-context-binding")
    manifest["passes"][0]["attempts"][0]["agent_path"] = agent_path
    manifest["passes"][0]["attempts"][0]["model"] = CRITICAL_MODEL
    child_rows[2]["payload"]["model"] = CRITICAL_MODEL
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    child_rollout.write_text("".join(json.dumps(row) + "\n" for row in child_rows), encoding="utf-8")
    if run_command(command).returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:review-validator-role-model")
    manifest["passes"][0]["attempts"][0]["model"] = DEFAULT_MODEL
    child_rows[2]["payload"]["model"] = DEFAULT_MODEL
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    child_rollout.write_text("".join(json.dumps(row) + "\n" for row in child_rows), encoding="utf-8")
    metadata["review_decision"] = {
        "recommendation": "needs-more-work",
        "summary": "Coverage evidence is missing.",
        "rationale": "The remaining verification blocks merge.",
    }
    result_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "checks_failed": ["tests"],
                "confidence": 0.95,
                "findings": {"critical": 0, "high": 1, "medium": 0, "low": 0},
                "metadata": metadata,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if run_command(command).returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:review-validator-missing-merge-blocker-table")
    with (out / "review-notes.md").open("a", encoding="utf-8") as handle:
        handle.write(
            "\n## Review Findings and Merge Blocks\n\n"
            "| Finding / area | Required change | Evidence | Status |\n"
            "| --- | --- | --- | --- |\n"
            "| Test coverage | Add coverage for the changed error path. | tests gate failed | Required |\n"
        )
    if run_command(command).returncode != 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:review-validator-merge-blocker-table")


def selftest_code_remediate_pr_identity(run: CalibrationRun) -> None:
    """Reject remote identity, base OID, and checkout head substitutions."""
    spec = importlib.util.spec_from_file_location("shared_artifact_validator", run.paths.validate_artifacts)
    if spec is None or spec.loader is None:
        run.fail_and_leak("shared-script-selftests", "selftest-setup:code-remediate-pr-identity")
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    routing = {
        "base_identity_source": "pr_url",
        "base_host": "github.com",
        "base_repo": "owner/repo",
        "pr_state": "OPEN",
        "base_oid": "base-oid",
        "head_oid": "head-oid",
        "pr_number": 1,
        "pr_url": "https://github.com/owner/repo/pull/1",
    }
    remote = {
        "expected": {"host": "github.com", "repository": "owner/repo"},
        "remote": "upstream",
        "remote_url": "https://github.com/owner/repo.git",
    }
    target = {
        "remote": "upstream",
        "remote_url": "https://github.com/owner/repo.git",
        "expected_base_oid": "base-oid",
        "local_head": "base-oid",
        "base_matches_pr_metadata": True,
        "expected_base_is_ancestor": True,
        "base_relation": "matches-pr-metadata",
    }
    checkout = {
        "pr_url": "https://github.com/owner/repo/pull/1",
        "expected_head": "head-oid",
        "local_head": "head-oid",
        "diff_source": "verified-local-checkout",
        "diff_base_oid": "base-oid",
        "diff_head_oid": "head-oid",
        "diff_command": "git diff --binary base-oid...head-oid --",
    }
    module._validate_code_remediate_pr_identity(routing, remote, target, checkout)
    advanced_target = {
        **target,
        "local_head": "new-base-oid",
        "base_matches_pr_metadata": False,
        "expected_base_is_ancestor": True,
        "base_relation": "advanced",
    }
    module._validate_code_remediate_pr_identity(routing, remote, advanced_target, checkout)
    diverged_target = {
        **advanced_target,
        "expected_base_is_ancestor": False,
        "base_relation": "diverged",
    }
    try:
        module._validate_code_remediate_pr_identity(routing, remote, diverged_target, checkout)
    except SystemExit:
        pass
    else:
        run.fail_and_leak("shared-script-selftests", "selftest-fail-open:code-remediate-pr-target-divergence")
    mutations = (
        (routing, "pr_state", "MERGED"),
        (remote, "expected", {"host": "github.com", "repository": "other/repo"}),
        (target, "local_head", "wrong-base"),
        (checkout, "local_head", "wrong-head"),
    )
    for payload, key, bad_value in mutations:
        original = payload[key]
        payload[key] = bad_value
        try:
            module._validate_code_remediate_pr_identity(routing, remote, target, checkout)
        except SystemExit:
            pass
        else:
            run.fail_and_leak("shared-script-selftests", f"selftest-fail-open:code-remediate-pr-identity:{key}")
        finally:
            payload[key] = original


def bind_selftest_final_handoff(
    run: CalibrationRun, out_path: Path, metadata: dict[str, Any]
) -> subprocess.CompletedProcess[str] | None:
    """Render and attach one valid implementation handoff for writer selftests."""
    gates_path = out_path.parent / "gates.json"
    gates = json.loads(gates_path.read_text(encoding="utf-8"))
    gap_closures = metadata.get("confidence_gap_closures")
    recovery = metadata.get("confidence_recovery")
    if not isinstance(gap_closures, list) or not isinstance(recovery, dict):
        raise ValueError("selftest final handoff requires confidence metadata")
    handoff = {
        "schema_version": 1,
        "skill": "implement",
        "branch": "standard",
        "outcome": {"title": "Selftest", "summary": "The synthetic implementation artifact is valid."},
        "tables": [
            {
                "heading": "Implementation Result",
                "columns": ["Surface", "Outcome", "Verification", "Remaining limit"],
                "rows": [
                    {
                        "id": "SELFTEST-1",
                        "cells": ["artifact lifecycle", "pass", "shared selftest", "synthetic only"],
                        "source_ids": ["selftest:artifact"],
                    }
                ],
            }
        ],
        "source_records": [{"id": "selftest:artifact", "evidence": "development-notes.md"}],
        "source_coverage": {
            "source_records_total": 1,
            "represented_source_records_total": 1,
            "omitted_source_records_total": 0,
        },
        "verification": [
            {"check": check["id"], "status": check["status"], "evidence": check["stdout"]} for check in gates["checks"]
        ],
        "remaining": [],
        "next_steps": [],
        "confidence": {
            "score": 0.95,
            "band": "fair",
            "limits": recovery.get("remaining_limits", []),
            "gaps": gap_closures,
        },
        "artifacts": [{"label": "Result", "path": str(out_path)}],
        "caller_contract": None,
    }
    handoff_path = out_path.parent / "final-handoff.json"
    final_path = out_path.parent / "final.md"
    validation_path = out_path.parent / "final-handoff.validation.json"
    handoff_path.write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8", newline="\n")
    rendered = run_command(
        cli_argv(
            run.paths.final_handoff_py,
            "render",
            "--handoff",
            handoff_path,
            "--out-final",
            final_path,
            "--out-validation",
            validation_path,
        )
    )
    if rendered.returncode != 0:
        return rendered
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    metadata["final_handoff"] = {
        "schema_version": 1,
        "handoff_path": str(handoff_path),
        "handoff_sha256": validation["handoff_sha256"],
        "rendered_path": str(final_path),
        "rendered_sha256": validation["rendered_sha256"],
        "validation_path": str(validation_path),
        "branch": "standard",
    }
    return None


def run_write_result(run: CalibrationRun, out_path: Path, metadata: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Render the final handoff and run the shared writer with a valid selftest payload."""
    handoff_failure = bind_selftest_final_handoff(run, out_path, metadata)
    if handoff_failure is not None:
        return handoff_failure
    return run_command(
        cli_argv(
            run.paths.write_result_py,
            "--out",
            out_path,
            "--gates",
            out_path.parent / "gates.json",
            "--status",
            "pass",
            "--checks-run",
            "lint,format,types,tests,review",
            "--checks-failed",
            "",
            "--critical",
            "0",
            "--high",
            "0",
            "--medium",
            "0",
            "--low",
            "0",
            "--confidence",
            "0.95",
            "--metadata",
            json.dumps(metadata, separators=(",", ":")),
            "--artifact-path",
            out_path,
        )
    )


def selftest_find_review_report(run: CalibrationRun, selftest_dir: Path) -> None:
    """Check PR-scoped run selection and historical flat-directory fallback."""
    fixture = selftest_dir / "review-reports"
    older = fixture / "2026-01-01T00-00-00Z"
    newer = fixture / "pr-123" / "run-002"
    older.mkdir(parents=True, exist_ok=True)
    newer.mkdir(parents=True, exist_ok=True)
    assessed_result = (
        json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "accept-as-is"}}}) + "\n"
    )
    for directory in (older, newer):
        (directory / "result.json").write_text(assessed_result, encoding="utf-8")
        (directory / "pr.json").write_text(
            '{"number": 123, "url": "https://github.com/example/repo/pull/123"}\n',
            encoding="utf-8",
        )
    expected = newer / "result.json"
    result = run_command(cli_argv(run.paths.find_review_report, "--target", "#123", "--reports-dir", fixture))
    if result.returncode != 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:find-review-report:match")
    elif result.stdout.strip() != str(expected):
        run.fail_and_leak("shared-script-selftests", f"selftest-mismatch:find-review-report:{result.stdout.strip()}")
    missing = run_command(cli_argv(run.paths.find_review_report, "--target", "#999", "--reports-dir", fixture))
    if missing.returncode == 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:find-review-report:missing-target")

    compatibility_root = selftest_dir / "review-report-compatibility"
    current = compatibility_root / ".reports" / "codex" / "code-review" / "2026-01-01T00-00-00Z"
    legacy = compatibility_root / ".reports" / "codex" / "review" / "2026-01-02T00-00-00Z"
    for directory in (current, legacy):
        directory.mkdir(parents=True)
        (directory / "result.json").write_text(assessed_result, encoding="utf-8")
        (directory / "pr.json").write_text(
            '{"number": 321, "url": "https://github.com/example/repo/pull/321"}\n',
            encoding="utf-8",
        )
    compatibility = run_command(cli_argv(run.paths.find_review_report, "--target", "#321"), cwd=compatibility_root)
    expected_legacy = legacy.relative_to(compatibility_root) / "result.json"
    if compatibility.returncode != 0 or compatibility.stdout.strip() != str(expected_legacy):
        run.fail_and_leak("shared-script-selftests", "selftest-failed:find-review-report:legacy-fallback")


def selftest_validate_artifacts(run: CalibrationRun, selftest_dir: Path) -> None:
    """Create and validate a minimal implementation artifact fixture."""
    validate_dir = selftest_dir / "validate"
    validate_dir.mkdir(parents=True, exist_ok=True)
    (validate_dir / "development-notes.md").write_text(
        "\n".join(
            [
                "# Development Notes",
                "",
                "## Scope",
                "Selftest.",
                "",
                "## Acceptance Criteria",
                "Selftest.",
                "",
                "## Evidence",
                "Selftest.",
                "",
                "## Specialist Policy",
                "Selftest.",
                "",
                "## Gates",
                "Selftest.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (validate_dir / "confidence-calibration.md").write_text(
        "\n".join(
            [
                "# Confidence Calibration",
                "",
                "## Initial Confidence",
                "0.9",
                "",
                "## Objective Evidence",
                "Selftest artifact shape was created and validated locally.",
                "",
                "## Confidence Gaps",
                "No known gaps for this validator selftest.",
                "",
                "## Recovery Actions",
                "Selftest includes the confidence artifact and matching result metadata.",
                "",
                "## Recomputed Confidence",
                "0.95",
                "",
                "## Remaining Limits",
                "None for this synthetic validator fixture.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    metadata = {
        "confidence_gaps": ["synthetic validator selftest does not execute project behavior"],
        "confidence_gap_closures": [
            {
                "gap": "synthetic validator selftest does not execute project behavior",
                "status": "unresolved",
                "rationale": "selftest validates artifact shape only and intentionally does not execute project behavior",
            }
        ],
        "confidence_recovery": {
            "initial_confidence": 0.9,
            "final_confidence": 0.95,
            "status": "fair",
            "evidence": ["selftest artifact shape was created and validated locally"],
            "recovery_actions": ["included the confidence artifact and matching result metadata"],
            "remaining_limits": [],
        },
    }
    success_command = "exit 0"
    timeout_command = "Start-Sleep -Seconds 2" if sys.platform == "win32" else "sleep 2"
    gates = run_command(
        cli_argv(
            run.paths.run_gates,
            "--out",
            validate_dir,
            "--lint",
            success_command,
            "--format",
            success_command,
            "--types",
            success_command,
            "--tests",
            success_command,
            "--review",
            success_command,
        )
    )
    if gates.returncode != 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:validate-artifacts-run-gates")
        return
    result = run_write_result(run, validate_dir / "result.json", metadata)
    if result.returncode != 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:validate-artifacts-write-result")
        return
    validation = run_command(
        cli_argv(
            run.paths.validate_artifacts,
            "--skill",
            "implement",
            "--out",
            validate_dir,
            "--result",
            validate_dir / "result.json",
        )
    )
    if validation.returncode != 0:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:validate-artifacts")

    contradictory = run_command(
        cli_argv(
            run.paths.write_result_py,
            "--out",
            validate_dir / "contradictory.json",
            "--gates",
            validate_dir / "gates.json",
            "--status",
            "pass",
            "--checks-run",
            "lint,format,types,tests,review",
            "--checks-failed",
            "tests",
            "--critical",
            "0",
            "--high",
            "0",
            "--medium",
            "0",
            "--low",
            "0",
            "--confidence",
            "0.95",
            "--metadata",
            json.dumps(metadata, separators=(",", ":")),
            "--artifact-path",
            validate_dir / "contradictory.json",
        )
    )
    if contradictory.returncode == 0 or (validate_dir / "contradictory.json").exists():
        run.fail_and_leak("shared-script-selftests", "selftest-failed:write-result-accepted-contradiction")

    timeout_dir = selftest_dir / "gate-timeout"
    timeout = run_command(
        cli_argv(
            run.paths.run_gates,
            "--out",
            timeout_dir,
            "--timeout-seconds",
            "1",
            "--lint",
            timeout_command,
            "--format",
            success_command,
            "--skip-types",
            "synthetic configuration fixture has no typed target",
            "--tests",
            success_command,
            "--review",
            success_command,
        )
    )
    if timeout.returncode != 124:
        run.fail_and_leak("shared-script-selftests", "selftest-failed:run-gates-timeout-exit")
    else:
        timeout_payload = json.loads((timeout_dir / "gates.json").read_text(encoding="utf-8"))
        statuses = {check["id"]: check["status"] for check in timeout_payload["checks"]}
        if timeout_payload["status"] != "timeout" or statuses.get("lint") != "timeout":
            run.fail_and_leak("shared-script-selftests", "selftest-failed:run-gates-timeout-status")
        if statuses.get("types") != "not-applicable":
            run.fail_and_leak("shared-script-selftests", "selftest-failed:run-gates-not-applicable")


def run_benchmark_pattern_checks(run: CalibrationRun) -> None:
    """Check benchmark regex patterns against skill and agent instruction files."""
    if not run.paths.benchmarks.exists():
        return
    data = json.loads(run.paths.benchmarks.read_text(encoding="utf-8"))
    before = count_leak_prefix(run.paths.leaks, "benchmark-")
    for skill, patterns in data.get("skills", {}).items():
        if run.paths.layout == "plugin" and skill == "sync":
            patterns = (
                "active plugin cache",
                "package-manifest.json",
                "explicit user approval",
                "source-unavailable cache validation",
                "external agent",
            )
        path = run.paths.skills_dir / skill / "SKILL.md"
        text = read_text(path)
        if skill == "code-remediate":
            # Calibration checks every route; ordinary skill execution still loads this
            # reference only when evaluating or executing the parallel route.
            text += "\n" + read_text(path.parent / "references" / "parallel-lifecycle.md")
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE) is None:
                run.append_leak(f"benchmark-skill-miss:{skill}:{pattern}")
    if run.paths.layout == "plugin":
        run.append_check("benchmark-agent-patterns=covered-by-role-card-contract")
        agent_patterns: dict[str, list[str]] = {}
    else:
        agent_patterns = data.get("agents", {})
    for agent, patterns in agent_patterns.items():
        path = role_file(run, agent)
        text = read_text(path)
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE) is None:
                run.append_leak(f"benchmark-agent-miss:{agent}:{pattern}")
    new_fails = count_leak_prefix(run.paths.leaks, "benchmark-") - before
    if new_fails > 0:
        run.mark_check_failed("benchmark-pattern-checks")
        run.fails += new_fails
        run.leaks += new_fails


def count_leak_prefix(path: Path, prefix: str) -> int:
    """Count leak lines with a given prefix."""
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(prefix))


def run_behavioral_scoring(run: CalibrationRun, require_live_routes: bool = False) -> None:
    """Run behavioral scoring and import failures into calibration status."""
    if not (
        run.paths.behavioral_cases.exists()
        and run.paths.behavioral_observations.exists()
        and run.paths.behavioral_scorer.exists()
    ):
        return
    command: list[str | Path] = [
        sys.executable,
        run.paths.behavioral_scorer,
        "--cases",
        run.paths.behavioral_cases,
        "--observations",
        run.paths.behavioral_observations,
        "--route-policy",
        run.paths.live_route_policy,
        "--tasks",
        run.paths.live_ab_tasks,
        "--layout",
        run.paths.layout,
        "--root",
        run.paths.asset_root,
        "--out",
        run.paths.behavioral_result,
    ]
    if require_live_routes:
        command.append("--require-live-routes")
    result = run_command(command)
    if result.returncode != 0 and not run.paths.behavioral_result.exists():
        run.fail_and_leak("behavioral-metrics", f"behavioral-scorer-error:{run.paths.behavioral_scorer}")
        return

    payload = json.loads(run.paths.behavioral_result.read_text(encoding="utf-8"))
    overall = payload["overall"]
    freshness = payload.get("observation_freshness", {})
    run.append_check(
        "behavioral:"
        f"status={payload['status']}:"
        f"recall={overall['recall']}:"
        f"precision={overall['precision']}:"
        f"confidence_accuracy={overall['confidence_accuracy']}:"
        f"live_observations={freshness.get('live_observations', 0)}:"
        f"live_routes={payload.get('live_route_acceptance', {}).get('status', 'missing')}"
    )
    if payload["status"] != "fail":
        return

    checks_failed = payload.get("checks_failed") or ["behavioral-status"]
    for check in checks_failed:
        run.append_leak(f"behavioral-fail:{check}")
    run.mark_check_failed("behavioral-metrics")
    run.fails += len(checks_failed)
    run.leaks += len(checks_failed)


def metric(behavioral_payload: dict[str, Any] | None, name: str, default: float = 0.0) -> float:
    """Read a behavioral metric from raw gate metrics or overall metrics."""
    if not behavioral_payload:
        return default
    return behavioral_payload.get("gate_metrics_raw", behavioral_payload.get("overall", {})).get(name, default)


def rounded(value: float) -> float:
    """Round a metric for recommendation text."""
    return round(float(value), 3)


def top_case_gaps(behavioral_payload: dict[str, Any] | None, key: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return the largest false-positive or false-negative behavioral case gaps."""
    if not behavioral_payload:
        return []
    cases = [case for case in behavioral_payload.get("case_results", []) if int(case.get(key, 0)) > 0]
    return sorted(
        cases,
        key=lambda case: (int(case.get(key, 0)), float(case.get("confidence_error", 0.0))),
        reverse=True,
    )[:limit]


def confidence_outliers(behavioral_payload: dict[str, Any] | None, limit: int = 5) -> list[dict[str, Any]]:
    """Return cases with the largest confidence calibration error."""
    if not behavioral_payload:
        return []
    return sorted(
        behavioral_payload.get("case_results", []),
        key=lambda case: float(case.get("confidence_error", 0.0)),
        reverse=True,
    )[:limit]


def build_recommendations(
    behavioral_payload: dict[str, Any] | None,
    failed_checks: list[str],
    leak_total: int,
    accepted_route_evidence: bool,
) -> tuple[list[str], list[str]]:
    """Build calibration recommendations and follow-up items from metrics."""
    recommendations: list[str] = []
    follow_up: list[str] = []

    if failed_checks:
        recommendations.append("Fix failed calibration checks first: " + ", ".join(failed_checks) + ".")
    if leak_total:
        recommendations.append(
            f"Inspect leaks.txt and fix {leak_total} missing or mismatched config references before widening changes."
        )
    if not behavioral_payload:
        recommendations.append("Restore behavioral scoring output; behavioral.json was not produced.")
        return recommendations, follow_up

    thresholds = behavioral_payload.get("thresholds", {})
    raw = behavioral_payload.get("gate_metrics_raw", behavioral_payload.get("overall", {}))
    recall = float(raw.get("recall", 0.0))
    precision = float(raw.get("precision", 0.0))
    confidence_mae = float(raw.get("confidence_mae", 0.0))
    confidence_accuracy = max(0.0, 1.0 - confidence_mae)
    mean_overconfidence = float(raw.get("mean_overconfidence", 0.0))
    observations = int(raw.get("observations", 0))

    if observations < float(thresholds.get("min_observations", 1.0)):
        recommendations.append(
            f"Add behavioral observations: {observations} present, threshold is {rounded(thresholds.get('min_observations', 1.0))}."
        )
    if recall < float(thresholds.get("min_recall", 0.75)) or int(raw.get("fn", 0)) > 0:
        gaps = top_case_gaps(behavioral_payload, "fn")
        if gaps:
            detail = "; ".join(f"{case['case_id']} missed {case['fn']} expected finding(s)" for case in gaps)
            recommendations.append(f"Improve recall by addressing missing expected findings: {detail}.")
        else:
            recommendations.append(
                f"Improve behavioral recall from {rounded(recall)} toward threshold {rounded(thresholds.get('min_recall', 0.75))}."
            )
    if precision < float(thresholds.get("min_precision", 0.75)) or int(raw.get("fp", 0)) > 0:
        gaps = top_case_gaps(behavioral_payload, "fp")
        if gaps:
            detail = "; ".join(f"{case['case_id']} reported {case['fp']} unsupported finding(s)" for case in gaps)
            recommendations.append(
                "Improve precision by removing unsupported observations or updating expected ground truth with evidence: "
                f"{detail}."
            )
        else:
            recommendations.append(
                f"Improve behavioral precision from {rounded(precision)} toward threshold {rounded(thresholds.get('min_precision', 0.75))}."
            )
    if confidence_mae > float(thresholds.get("max_confidence_mae", 0.2)):
        recommendations.append(
            "Reduce confidence calibration error: "
            f"MAE {rounded(confidence_mae)} exceeds threshold {rounded(thresholds.get('max_confidence_mae', 0.2))}."
        )
    elif confidence_accuracy < 0.9:
        detail = "; ".join(
            f"{case['case_id']} confidence {case['confidence']} vs F1 {case['f1']}"
            for case in confidence_outliers(behavioral_payload, limit=3)
        )
        recommendations.append(
            f"Review stale confidence labels; confidence accuracy is {rounded(confidence_accuracy)}. Largest gaps: {detail}."
        )
    if mean_overconfidence > float(thresholds.get("max_mean_overconfidence", 0.15)):
        recommendations.append(
            "Reduce overconfidence: "
            f"mean overconfidence {rounded(mean_overconfidence)} exceeds threshold "
            f"{rounded(thresholds.get('max_mean_overconfidence', 0.15))}."
        )

    freshness = behavioral_payload.get("observation_freshness", {})
    live_routes = behavioral_payload.get("live_route_acceptance", {})
    if int(freshness.get("live_observations", 0) or 0) == 0 and not accepted_route_evidence:
        follow_up.append(
            "Add source=live-* observations from real Codex calibration prompts before treating fixture metrics as live model quality."
        )
    if live_routes.get("status") == "insufficient-evidence" and not accepted_route_evidence:
        follow_up.append(
            "Run the explicit paid paired campaign before promoting provisional Luna/Terra/Sol routes to measured acceptance."
        )
    if int(freshness.get("missing_observed_at", 0) or 0) > 0:
        follow_up.append("Backfill missing observed_at timestamps in behavioral observations.")
    if not recommendations:
        next_step = (
            "rerun paid calibration before changing model allocation or cost policy"
            if accepted_route_evidence
            else "collect live observations next"
        )
        recommendations.append(f"No blocking calibration fixes found; maintain the current gates and {next_step}.")
    return recommendations, follow_up


def write_result(run: CalibrationRun) -> None:
    """Write result.json and recommendations.md for the calibration run."""
    if not run.paths.leaks.exists():
        run.paths.leaks.touch()
    status = "fail" if run.fails > 0 or run.leaks > 0 else "pass"
    behavioral = (
        json.loads(run.paths.behavioral_result.read_text(encoding="utf-8"))
        if run.paths.behavioral_result.exists()
        else None
    )
    accepted_route_evidence = run.accepted_route_evidence_current
    recommendations, follow_up = build_recommendations(
        behavioral, run.checks_failed, run.leaks, accepted_route_evidence
    )
    confidence_gaps = ["fixture-heavy calibration does not fully prove live model behavior"]
    confidence_gap_closures = [
        {
            "gap": "fixture-heavy calibration does not fully prove live model behavior",
            "status": "unresolved",
            "rationale": "behavioral metrics include fixture observations; live observations are reported separately",
        }
    ]
    remaining_limits = ["live model quality still depends on live calibration observations"]
    if run.paths.accepted_route_evidence.exists() and not accepted_route_evidence:
        stale_gap = "accepted live-route evidence predates the current skill identity"
        confidence_gaps.append(stale_gap)
        confidence_gap_closures.append(
            {
                "gap": stale_gap,
                "status": "unresolved",
                "rationale": "the archived evidence roster includes retired skill names and is preserved only as history",
            }
        )
        remaining_limits.append("current implement and assess routes lack fresh paid live evidence")
    confidence = 0.95 if accepted_route_evidence else 0.9
    payload = {
        "status": status,
        "timestamp": run.paths.timestamp,
        "checks_run": [
            "project-model-default",
            "review-model-policy",
            "supported-model-policy",
            "accepted-route-evidence",
            "skill-schema-all",
            "skill-registration-project",
            "agent-registration-project",
            "agent-schema-all",
            "agent-model-policy",
            "native-skill-contract",
            "native-agent-contract",
            "recurrence-policy-links",
            "native-runtime-leakage",
            "confidence-policy",
            "fixed-task-set",
            "behavioral-version-policy",
            "benchmark-pattern-checks",
            "behavioral-metrics",
            "shared-script-selftests",
        ],
        "checks_failed": run.checks_failed,
        "findings": {"critical": 0, "high": run.leaks, "medium": 0, "low": 0},
        "confidence": confidence,
        "artifact_path": f".reports/codex/calibration/{run.paths.timestamp}/result.json",
        "metadata": {
            "layout": run.paths.layout,
            "confidence_gaps": confidence_gaps,
            "confidence_gap_closures": confidence_gap_closures,
            "confidence_recovery": {
                "initial_confidence": 0.9,
                "final_confidence": confidence,
                "status": "fair",
                "evidence": ["calibration checks completed and behavioral metrics were computed"],
                "recovery_actions": ["recorded fixture-vs-live observation counts and confidence calibration metrics"],
                "remaining_limits": remaining_limits,
            },
        },
        "leaks_found": run.leaks,
        "behavioral": behavioral,
        "recommendations": recommendations,
        "follow_up": follow_up,
        "artifacts": {
            "checks": f".reports/codex/calibration/{run.paths.timestamp}/checks.txt",
            "leaks": f".reports/codex/calibration/{run.paths.timestamp}/leaks.txt",
            "behavioral": f".reports/codex/calibration/{run.paths.timestamp}/behavioral.json",
            "recommendations": f".reports/codex/calibration/{run.paths.timestamp}/recommendations.md",
            "result": f".reports/codex/calibration/{run.paths.timestamp}/result.json",
        },
    }
    run.paths.result.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    write_recommendations(run, status, behavioral, recommendations, follow_up)


def write_recommendations(
    run: CalibrationRun,
    status: str,
    behavioral: dict[str, Any] | None,
    recommendations: list[str],
    follow_up: list[str],
) -> None:
    """Write the Markdown recommendations artifact."""
    lines = [
        "# Calibration Recommendations",
        "",
        f"Status: {status}",
        f"Checks failed: {', '.join(run.checks_failed) if run.checks_failed else 'none'}",
        f"Leaks found: {run.leaks}",
    ]
    if behavioral:
        overall = behavioral.get("overall", {})
        freshness = behavioral.get("observation_freshness", {})
        lines.extend(
            [
                "",
                "## Behavioral Summary",
                "",
                f"- Recall: {overall.get('recall')}",
                f"- Precision: {overall.get('precision')}",
                f"- F1: {overall.get('f1')}",
                f"- Confidence accuracy: {overall.get('confidence_accuracy')}",
                f"- Mean overconfidence: {overall.get('mean_overconfidence')}",
                f"- Fixture observations: {freshness.get('fixture_observations', 0)}",
                f"- Live observations: {freshness.get('live_observations', 0)}",
            ]
        )
    lines.extend(["", "## Recommendations", ""])
    lines.extend(f"- {item}" for item in recommendations)
    if follow_up:
        lines.extend(["", "## Follow-Up", ""])
        lines.extend(f"- {item}" for item in follow_up)
    leak_text = read_text(run.paths.leaks).strip()
    if leak_text:
        lines.extend(["", "## Leak Details", ""])
        lines.extend(f"- {line}" for line in leak_text.splitlines() if line.strip())
    run.paths.recommendations.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """Run all calibration checks and print the result path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--layout",
        choices=[layout.value for layout in Layout],
        default=Layout.PLUGIN.value,
        help="Asset layout: source reads project .codex; plugin reads the installed package.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Consuming project root used for reports and Git context.",
    )
    parser.add_argument(
        "--require-live-routes",
        action="store_true",
        help="Fail unless current observation inputs satisfy every configured strict live route.",
    )
    args = parser.parse_args()
    paths = Paths.create(args.layout, args.root)
    run = CalibrationRun(paths=paths)
    check_core_configs(run)
    check_native_runtime_leaks(run)
    check_shared_scripts(run)
    run_benchmark_pattern_checks(run)
    run_behavioral_scoring(run, args.require_live_routes)
    write_result(run)
    print(run.paths.result)
    return 1 if run.fails > 0 or run.leaks > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
