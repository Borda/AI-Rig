#!/usr/bin/env python3
"""Check that Codex Rig's skill, role, calibration and route rosters match their pinned tables.

Every check compares a directory listing or a documentation table with a roster written out below. A roster that
drifts breaks no behaviour: a renamed skill, an unlisted case id or a stale route row only makes this bookkeeping
disagree with the files, so the comparison runs as a pre-commit hook instead of a pytest test. Behaviour that depends
on the rosters (frontmatter, runtime policy, scoring, manifest records) stays covered by the pytest suite.

Checks, each reporting every difference it finds:

- the skill cards on disk, the retired review and resolve skills, and the Kaggle reference set
- the role cards on disk
- the calibration case ids of the adversarial loop and the recurrence and model-stall escalation families
- the calibration runner's skill roster, and one task cue per role in each role card's trigger line
- the active model and effort assignment record
- the Codemap route-selection table against each skill's own adapter invocation

Usage: ``check_codex_roster.py [--root PLUGIN_ROOT]``. The root defaults to ``plugins/codex-rig`` beside this script.
Exit 1 when any difference is found; each line is ``path: message`` and names the roster to update.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

#: Workflow skills the plugin ships, one ``skills/<id>/SKILL.md`` each.
EXPECTED_SKILLS = frozenset(
    {
        "agent-shims",
        "challenge-resolve",
        "assess",
        "audit",
        "calibrate",
        "code-remediate",
        "code-review",
        "implement",
        "investigate",
        "kaggle",
        "manage",
        "optimize",
        "profile",
        "release",
        "research",
        "sync",
    }
)
#: Workflow skills that were retired and must not return.
RETIRED_SKILLS = frozenset({"review", "resolve"})
#: Skill the calibration runner's required roster leaves out because it carries no artifact workflow.
UNCALIBRATED_SKILLS = frozenset({"agent-shims"})
#: Notebook composition stage references under ``skills/kaggle/references``.
EXPECTED_KAGGLE_REFERENCES = frozenset(
    {
        "composition.md",
        "eda.md",
        "foundation.md",
        "inference.md",
        "modality-dispatch.md",
        "style-rules.md",
        "submission.md",
        "training.md",
    }
)
#: Role id -> (model, reasoning effort) of the active assignment record; the role cards on disk are the same roster.
ROLE_ASSIGNMENTS = {
    "challenger": ("gpt-6.1-sol", "high"),
    "cicd-steward": ("gpt-6-luna", "high"),
    "curator": ("gpt-6-luna", "high"),
    "data-steward": ("gpt-6.1-sol", "high"),
    "delegation-lead": ("gpt-6-luna", "high"),
    "doc-scribe": ("gpt-6.1-terra", "high"),
    "linting-expert": ("gpt-6-luna", "medium"),
    "oss-shepherd": ("gpt-6-luna", "high"),
    "qa-specialist": ("gpt-6.1-sol", "medium"),
    "scientist": ("gpt-6.1-sol", "high"),
    "security-auditor": ("gpt-6.1-sol", "high"),
    "solution-architect": ("gpt-6.1-sol", "high"),
    "squeezer": ("gpt-6.1-sol", "medium"),
    "sw-engineer": ("gpt-6.1-sol", "medium"),
    "web-explorer": ("gpt-6.1-terra", "medium"),
}
#: Behavioral cases whose target is ``challenge-resolve``.
CHALLENGE_RESOLVE_CASES = frozenset(
    {
        "adversarial-loop-independent-closure",
        "adversarial-loop-stop-conditions",
        "adversarial-loop-owner-and-response-binding",
        "adversarial-loop-progress-transcript",
        "adversarial-loop-premature-progress-table",
        "adversarial-loop-five-step-convergence",
        "adversarial-loop-closure-and-discovery",
        "adversarial-loop-repeated-review-trigger",
        "challenge-resolve-large-scope-premature-stop",
    }
)
#: Escalation case id prefix -> the stages that family keeps, regardless of target.
ESCALATION_CASES = {
    "recurrence-": frozenset(
        {
            "recurrence-initial-obstacle",
            "recurrence-second-occurrence-investigate",
            "recurrence-third-occurrence-human-handoff",
        }
    ),
    "model-stall-": frozenset(
        {
            "model-stall-advisory-escalation",
            "model-stall-human-handoff",
            "model-stall-progress-without-closure",
            "model-stall-user-directed-progress",
            "model-stall-advisory-route-safety",
        }
    ),
}
#: Skills whose adapter invocation lives in a trigger-loaded reference rather than in SKILL.md.
INVOCATION_REFERENCES = {"code-review": "source-and-scope.md", "code-remediate": "selection.md"}
#: Models and efforts of the non-role routes in the assignment record.
PARENT_ROUTE = {"model": "gpt-6.1-sol", "reasoning_effort": "medium"}
DEEP_REVIEW_ROUTE = {"model": "gpt-6.1-sol", "reasoning_effort": "high", "activation": "explicit-effort-override"}
HISTORICAL_ASSIGNMENTS = frozenset({"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"})

ADAPTER_INVOCATION = "codemap_adapter.py context"
ROUTE_HEADING = "## Route selection per skill"
CALIBRATION = "runtime/calibration"
CONTRACT = "shared/codemap-contract.md"
ASSIGNMENT_RECORD = f"{CALIBRATION}/accepted-route-evidence.json"
CASES = f"{CALIBRATION}/behavioral-cases.json"
RUNNER = f"{CALIBRATION}/run.py"


def _diff(label: str, found: set[str], expected: set[str] | frozenset[str]) -> list[str]:
    """Return one message naming the missing and unexpected entries of ``label``, or nothing when they match."""
    if found == expected:
        return []
    return [f"{label}: missing={sorted(expected - found)} unexpected={sorted(found - expected)}"]


def _load_json(path: Path) -> Any:
    """Read one UTF-8 JSON file; a missing or malformed file raises ``ValueError`` naming it."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path.name}: cannot read JSON ({error})") from error


def check_skills(root: Path) -> list[str]:
    """Compare the skill cards and Kaggle references on disk with their rosters.

    Examples:
        >>> check_skills(Path("/nonexistent"))[0].startswith("skills: missing=")
        True
    """
    discovered = {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}
    problems = _diff("skills", discovered, EXPECTED_SKILLS)
    problems += [
        f"skills: retired skill `{name}` is back; remove skills/{name} or drop it from RETIRED_SKILLS in scripts/check_codex_roster.py"
        for name in sorted(RETIRED_SKILLS & discovered)
    ]
    references = {path.name for path in (root / "skills" / "kaggle" / "references").glob("*.md")}
    return problems + _diff("skills/kaggle/references", references, EXPECTED_KAGGLE_REFERENCES)


def check_roles(root: Path) -> list[str]:
    """Compare the role cards on disk with the role roster."""
    return _diff("roles", {path.parent.name for path in (root / "roles").glob("*/ROLE.md")}, set(ROLE_ASSIGNMENTS))


def check_case_ids(root: Path) -> list[str]:
    """Compare the adversarial-loop and escalation case ids in the behavioral case file with their rosters."""
    cases = _load_json(root / CASES)["cases"]
    loop = {case["id"] for case in cases if case.get("target") == "challenge-resolve"}
    problems = _diff(f"{CASES} challenge-resolve case ids", loop, CHALLENGE_RESOLVE_CASES)
    for prefix, expected in ESCALATION_CASES.items():
        found = {case["id"] for case in cases if case["id"].startswith(prefix)}
        problems += _diff(f"{CASES} {prefix}* case ids", found, expected)
    return problems


def _runner_literals(path: Path) -> dict[str, Any]:
    """Return the literal values of ``SKILLS``, ``AGENTS`` and ``ROLE_TASK_CUES`` assigned at the top of the runner."""
    wanted = {"SKILLS", "AGENTS", "ROLE_TASK_CUES"}
    found: dict[str, Any] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in wanted:
                found[node.targets[0].id] = ast.literal_eval(node.value)
    return found


def _trigger_line(card: str) -> str:
    """Return the casefolded first ``- Trigger:`` line of a role card, or an empty string."""
    return next((line.casefold() for line in card.splitlines() if line.startswith("- Trigger:")), "")


def _route_issue(role: str, cue: str, triggers: dict[str, str]) -> str:
    """Return the routing problem of one role's cue (missing, wrong role, ambiguous), or an empty string."""
    owners = sorted(candidate for candidate, trigger in triggers.items() if cue.casefold() in trigger)
    if not owners:
        return f"role-task-route:{role}:missing"
    if role not in owners:
        return f"role-task-route:{role}:wrong-role:{','.join(owners)}"
    return "" if owners == [role] else f"role-task-route:{role}:ambiguous:{','.join(owners)}"


def check_calibration_roster(root: Path) -> list[str]:
    """Check the runner's skill roster covers the packaged workflows and every role owns one task cue."""
    literals = _runner_literals(root / RUNNER)
    missing = {"SKILLS", "AGENTS", "ROLE_TASK_CUES"} - literals.keys()
    if missing:
        return [f"{RUNNER}: no literal assignment for {sorted(missing)}; the roster checks need them"]
    packaged = {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}
    problems = _diff(f"{RUNNER} SKILLS", set(literals["SKILLS"]), packaged - UNCALIBRATED_SKILLS)
    cues, agents = literals["ROLE_TASK_CUES"], literals["AGENTS"]
    if set(cues) != set(agents):
        return [*problems, f"{RUNNER}: ROLE_TASK_CUES and AGENTS list different roles; make them the same set"]
    triggers = {
        role: _trigger_line((root / "roles" / role / "ROLE.md").read_text(encoding="utf-8"))
        for role in agents
        if (root / "roles" / role / "ROLE.md").is_file()
    }
    issues = (_route_issue(role, cue, triggers) for role, cue in cues.items())
    return problems + [
        f"roles: {issue}; give exactly one role card the cue in its `- Trigger:` line" for issue in issues if issue
    ]


def check_assignment_record(root: Path) -> list[str]:
    """Check the accepted-route record assigns every role its model and effort plus the parent and deep-review
    routes."""
    evidence = _load_json(root / ASSIGNMENT_RECORD)
    active = evidence["active_assignments"]
    assigned = [
        (role, model, effort)
        for model, efforts in active["roles"].items()
        for effort, roles in efforts.items()
        for role in roles
    ]
    problems = []
    if len(assigned) != len({role for role, _, _ in assigned}):
        problems.append(f"{ASSIGNMENT_RECORD}: a role is assigned more than once")
    actual = {role: (model, effort) for role, model, effort in assigned}
    for role in sorted(actual.keys() | ROLE_ASSIGNMENTS.keys()):
        if actual.get(role) != ROLE_ASSIGNMENTS.get(role):
            problems.append(
                f"{ASSIGNMENT_RECORD}: role `{role}` is {actual.get(role)}, expected {ROLE_ASSIGNMENTS.get(role)}"
            )
    for name, expected in (("parent", PARENT_ROUTE), ("deep_review", DEEP_REVIEW_ROUTE)):
        if active.get(name) != expected:
            problems.append(f"{ASSIGNMENT_RECORD}: active `{name}` route is {active.get(name)}, expected {expected}")
    quality = evidence.get("active_assignment_basis", {}).get("gpt6_quality_cost_evidence")
    if quality != "pending-paired-evaluation":
        problems.append(
            f"{ASSIGNMENT_RECORD}: GPT-6 quality evidence is `{quality}`, expected `pending-paired-evaluation`"
        )
    return problems + _diff(
        f"{ASSIGNMENT_RECORD} historical assignments", set(evidence["historical_assignments"]), HISTORICAL_ASSIGNMENTS
    )


def _documented_route_selection(contract: Path) -> dict[str, str]:
    """Return ``{skill: selection}`` from the contract's route-selection table, read line by line."""
    selection: dict[str, str] = {}
    in_section = False
    for line in contract.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line.strip() == ROUTE_HEADING
        elif in_section and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0].startswith("`") and cells[0].endswith("`"):
                selection[cells[0].strip("`")] = cells[2]
    return selection


def _adapter_invocations(root: Path) -> tuple[dict[str, str], list[str]]:
    """Return ``{skill: adapter invocation line}`` for every skill that calls the adapter, and unreadable references."""
    invocations: dict[str, str] = {}
    problems: list[str] = []
    for skill_file in sorted((root / "skills").glob("*/SKILL.md")):
        skill = skill_file.parent.name
        text = skill_file.read_text(encoding="utf-8")
        reference = skill_file.parent / "references" / INVOCATION_REFERENCES.get(skill, "")
        if skill in INVOCATION_REFERENCES and not reference.is_file():
            problems.append(f"skills/{skill}: invocation reference `{reference.name}` is missing")
        elif skill in INVOCATION_REFERENCES:
            text += "\n" + reference.read_text(encoding="utf-8")
        line = next((line for line in text.splitlines() if ADAPTER_INVOCATION in line), None)
        if line is not None:
            invocations[skill] = line
    return invocations, problems


def check_route_table(root: Path) -> list[str]:
    """Check the documented route table lists exactly the adapter-consuming skills and their `--query-kind` choice."""
    documented = _documented_route_selection(root / CONTRACT)
    invocations, problems = _adapter_invocations(root)
    problems += _diff(f"{CONTRACT} route table rows", set(documented), set(invocations))
    for skill in sorted(documented.keys() & invocations.keys()):
        adaptive = documented[skill].startswith("adaptive")
        if adaptive != ("--query-kind" in invocations[skill]):
            state = "passes" if "--query-kind" in invocations[skill] else "omits"
            problems.append(
                f"{CONTRACT}: row `{skill}` says `{documented[skill]}` but the skill {state} --query-kind; move the row"
            )
    return problems


CHECKS = (
    check_skills,
    check_roles,
    check_case_ids,
    check_calibration_roster,
    check_assignment_record,
    check_route_table,
)


def check_all(root: Path) -> list[str]:
    """Run every roster check under ``root`` and return the combined violations."""
    problems: list[str] = []
    for check in CHECKS:
        try:
            problems.extend(check(root))
        except (OSError, ValueError, KeyError, SyntaxError) as error:
            problems.append(
                f"{check.__name__}: cannot read the files this check compares ({type(error).__name__}: {error})"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    """Print every violation and return 1 when any exists."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "plugins" / "codex-rig",
        help="Codex Rig plugin root",
    )
    problems = check_all(parser.parse_args(argv).root)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
