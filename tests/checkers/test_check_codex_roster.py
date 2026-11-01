"""Contract: the ``codex-rig-roster`` hook reports every drift between Codex Rig's rosters and the files they describe.

Each case builds a plugin tree that satisfies every roster, applies exactly one drift, and expects the checker to exit 1
with a message naming that drift, so a check that stopped comparing something would turn its case green.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_codex_roster.py"
ADAPTIVE_SKILL, STANDARD_SKILL = "implement", "assess"
Mutation = Callable[[Path], object]


@pytest.fixture(scope="module", name="checker")
def _checker() -> ModuleType:
    """Load the roster checker by file path, as the hook runs it."""
    spec = importlib.util.spec_from_file_location("codex_rig_check_codex_roster", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(root: Path, relative: str, text: str) -> None:
    """Write one UTF-8 file under ``root``, creating its directory."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json(root: Path, relative: str, payload: object) -> None:
    """Write one JSON file under ``root``."""
    _write(root, relative, json.dumps(payload))


def _case(case_id: str, target: str) -> dict[str, str]:
    """Return one minimal behavioral case."""
    return {"id": case_id, "target": target}


def _write_assignments(root: Path, roles: dict[str, tuple[str, str]], checker: ModuleType) -> None:
    """Write an accepted-route record that assigns each role its model and effort."""
    grouped: dict[str, dict[str, list[str]]] = {}
    for role, (model, effort) in roles.items():
        grouped.setdefault(model, {}).setdefault(effort, []).append(role)
    _write_json(
        root,
        checker.ASSIGNMENT_RECORD,
        {
            "active_assignments": {
                "roles": grouped,
                "parent": checker.PARENT_ROUTE,
                "deep_review": checker.DEEP_REVIEW_ROUTE,
            },
            "active_assignment_basis": {"gpt6_quality_cost_evidence": "pending-paired-evaluation"},
            "historical_assignments": {name: {} for name in checker.HISTORICAL_ASSIGNMENTS},
        },
    )


def _write_route_table(root: Path, checker: ModuleType) -> None:
    """Write the contract table and the skills' adapter invocations that agree with it."""
    invocation = f"run {checker.ADAPTER_INVOCATION} --target a"
    _write(
        root,
        checker.CONTRACT,
        f"## Route selection per skill\n\n| Skill | Category | Route selection | Why |\n| -- | -- | -- | -- |\n"
        f"| `{ADAPTIVE_SKILL}` | `implementation` | adaptive — passes `--query-kind` | x |\n"
        f"| `{STANDARD_SKILL}` | `analysis` | standard batch — no `--query-kind` | x |\n"
        "| `code-review` | `review` | standard batch — no `--query-kind` | x |\n\n## Next\n",
    )
    _write(root, f"skills/{ADAPTIVE_SKILL}/SKILL.md", f"{invocation} --query-kind callers\n")
    _write(root, f"skills/{STANDARD_SKILL}/SKILL.md", f"{invocation}\n")
    _write(root, "skills/code-review/references/source-and-scope.md", f"{invocation}\n")
    _write(root, "skills/code-remediate/references/selection.md", "no adapter call\n")


@pytest.fixture(name="plugin_root")
def _plugin_root(tmp_path: Path, checker: ModuleType) -> Path:
    """Build a plugin tree in which every roster agrees with the files."""
    roles = sorted(checker.ROLE_ASSIGNMENTS)
    for skill in checker.EXPECTED_SKILLS:
        _write(tmp_path, f"skills/{skill}/SKILL.md", f"# {skill}\n")
    for reference in checker.EXPECTED_KAGGLE_REFERENCES:
        _write(tmp_path, f"skills/kaggle/references/{reference}", "# stage\n")
    for role in roles:
        _write(tmp_path, f"roles/{role}/ROLE.md", f"# {role}\n- Trigger: <{role}> work\n")
    _write(
        tmp_path,
        checker.RUNNER,
        f"SKILLS = {tuple(sorted(checker.EXPECTED_SKILLS - checker.UNCALIBRATED_SKILLS))!r}\n"
        f"AGENTS = {tuple(roles)!r}\n"
        f"ROLE_TASK_CUES = { ({role: f'<{role}>' for role in roles})!r}\n",
    )
    cases = [_case(case_id, "challenge-resolve") for case_id in checker.CHALLENGE_RESOLVE_CASES]
    cases += [_case(case_id, "implement") for family in checker.ESCALATION_CASES.values() for case_id in family]
    _write_json(tmp_path, checker.CASES, {"cases": cases})
    _write_assignments(tmp_path, checker.ROLE_ASSIGNMENTS, checker)
    _write_route_table(tmp_path, checker)
    return tmp_path


def _run(checker: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the checker's entry point on ``root`` and return its exit code and output."""
    code = checker.main(["--root", str(root)])
    return code, capsys.readouterr().out


def _unlink(relative: str, root: Path) -> None:
    """Delete one file of the tree."""
    (root / relative).unlink()


def _create(relative: str, root: Path) -> None:
    """Add one empty file to the tree."""
    _write(root, relative, "# extra\n")


def _replace(relative: str, old: str, new: str, root: Path) -> None:
    """Replace ``old`` with ``new`` in one file of the tree; the text must be present."""
    text = (root / relative).read_text(encoding="utf-8")
    assert old in text, (relative, old)
    _write(root, relative, text.replace(old, new))


def _edit_json(relative: str, edit: Callable[[dict], object], root: Path) -> None:
    """Load one JSON file of the tree, apply ``edit`` to it and write it back."""
    payload = json.loads((root / relative).read_text(encoding="utf-8"))
    edit(payload)
    _write_json(root, relative, payload)


def _drop_case(prefix: str, payload: dict) -> None:
    """Remove every behavioral case whose id starts with ``prefix``."""
    payload["cases"] = [case for case in payload["cases"] if not case["id"].startswith(prefix)]


def _add_case(case_id: str, target: str, payload: dict) -> None:
    """Add one behavioral case."""
    payload["cases"].append(_case(case_id, target))


def _set_role(role: str, assignment: list[str], payload: dict) -> None:
    """Move a role to another model and effort group in the active record."""
    roles = payload["active_assignments"]["roles"]
    for efforts in roles.values():
        for names in efforts.values():
            names[:] = [name for name in names if name != role]
    roles.setdefault(assignment[0], {}).setdefault(assignment[1], []).append(role)


def _assign_again(role: str, assignment: list[str], payload: dict) -> None:
    """List a role under a second model and effort group without removing the first."""
    payload["active_assignments"]["roles"].setdefault(assignment[0], {}).setdefault(assignment[1], []).append(role)


def _move_curator_cue_to_qa(root: Path) -> None:
    """Take the curator's cue off its card and put it on the QA card, so only the wrong role owns it."""
    _replace("roles/curator/ROLE.md", "<curator>", "other", root)
    _replace("roles/qa-specialist/ROLE.md", "work", "<curator> work", root)


def _set_active(key: str, value: object, payload: dict) -> None:
    """Overwrite one key of the active assignment record."""
    payload["active_assignments"][key] = value


def _set_quality(value: str, payload: dict) -> None:
    """Overwrite the GPT-6 quality-evidence marker."""
    payload["active_assignment_basis"]["gpt6_quality_cost_evidence"] = value


def _drop_history(payload: dict) -> None:
    """Remove one historical assignment group."""
    del payload["historical_assignments"]["gpt-5.6-sol"]


def _break_json(relative: str, root: Path) -> None:
    """Replace one JSON file with text that is not JSON."""
    _write(root, relative, "{not json")


_CONTRACT = "shared/codemap-contract.md"
_RUNNER = "runtime/calibration/run.py"
_CASES = "runtime/calibration/behavioral-cases.json"
_RECORD = "runtime/calibration/accepted-route-evidence.json"
_DRIFTS = [
    pytest.param(partial(_unlink, "skills/audit/SKILL.md"), "skills: missing=['audit']", id="skill-card-removed"),
    pytest.param(partial(_create, "skills/surprise/SKILL.md"), "unexpected=['surprise']", id="skill-card-added"),
    pytest.param(partial(_create, "skills/review/SKILL.md"), "retired skill `review` is back", id="retired-skill"),
    pytest.param(partial(_unlink, "skills/kaggle/references/eda.md"), "missing=['eda.md']", id="kaggle-stage-removed"),
    pytest.param(
        partial(_create, "skills/kaggle/references/extra.md"), "unexpected=['extra.md']", id="kaggle-stage-added"
    ),
    pytest.param(partial(_unlink, "roles/curator/ROLE.md"), "roles: missing=['curator']", id="role-card-removed"),
    pytest.param(
        partial(_create, "roles/surprise/ROLE.md"), "roles: missing=[] unexpected=['surprise']", id="role-card-added"
    ),
    pytest.param(
        partial(_edit_json, _CASES, partial(_add_case, "adversarial-loop-new", "challenge-resolve")),
        "challenge-resolve case ids: missing=[] unexpected=['adversarial-loop-new']",
        id="loop-case-added",
    ),
    pytest.param(
        partial(_edit_json, _CASES, partial(_drop_case, "adversarial-loop-stop")),
        "missing=['adversarial-loop-stop-conditions']",
        id="loop-case-removed",
    ),
    pytest.param(
        partial(_edit_json, _CASES, partial(_drop_case, "recurrence-")),
        "recurrence-* case ids",
        id="recurrence-family-removed",
    ),
    pytest.param(
        partial(_edit_json, _CASES, partial(_add_case, "model-stall-extra", "delegation-lead")),
        "unexpected=['model-stall-extra']",
        id="model-stall-stage-added",
    ),
    pytest.param(partial(_break_json, _CASES), "cannot read", id="unreadable-case-file"),
    pytest.param(
        partial(_replace, _RUNNER, "'audit', ", ""), "SKILLS: missing=['audit']", id="calibration-roster-missing-skill"
    ),
    pytest.param(
        partial(_replace, _RUNNER, "AGENTS = (", "AGENTS = ('ghost', "),
        "ROLE_TASK_CUES and AGENTS list different roles",
        id="cue-roster-mismatch",
    ),
    pytest.param(
        partial(_replace, "roles/sw-engineer/ROLE.md", "<sw-engineer>", "something else"),
        "role-task-route:sw-engineer:missing",
        id="role-cue-removed",
    ),
    pytest.param(
        partial(_replace, "roles/qa-specialist/ROLE.md", "work", "<sw-engineer> work"),
        "role-task-route:sw-engineer:ambiguous:qa-specialist,sw-engineer",
        id="role-cue-shared",
    ),
    pytest.param(
        _move_curator_cue_to_qa, "role-task-route:curator:wrong-role:qa-specialist", id="role-cue-moved-to-other-role"
    ),
    pytest.param(
        partial(_edit_json, _RECORD, partial(_set_role, "challenger", ["gpt-6-luna", "high"])),
        "role `challenger` is ('gpt-6-luna', 'high')",
        id="role-model-changed",
    ),
    pytest.param(
        partial(_edit_json, _RECORD, partial(_assign_again, "curator", ["gpt-6.1-sol", "high"])),
        "a role is assigned more than once",
        id="role-assigned-twice",
    ),
    pytest.param(
        partial(_edit_json, _RECORD, partial(_set_active, "parent", {"model": "x", "reasoning_effort": "low"})),
        "active `parent` route",
        id="parent-route-changed",
    ),
    pytest.param(
        partial(_edit_json, _RECORD, partial(_set_active, "deep_review", {})),
        "active `deep_review` route",
        id="deep-route-dropped",
    ),
    pytest.param(
        partial(_edit_json, _RECORD, partial(_set_quality, "proven")),
        "quality evidence is `proven`",
        id="quality-claim",
    ),
    pytest.param(partial(_edit_json, _RECORD, _drop_history), "historical assignments", id="history-dropped"),
    pytest.param(
        partial(_replace, _CONTRACT, "adaptive — passes `--query-kind`", "standard batch — no `--query-kind`"),
        "row `implement` says `standard batch — no `--query-kind`` but the skill passes --query-kind",
        id="row-says-standard-skill-selects",
    ),
    pytest.param(
        partial(_replace, "skills/implement/SKILL.md", " --query-kind callers", ""),
        "row `implement` says `adaptive — passes `--query-kind`` but the skill omits --query-kind",
        id="row-says-adaptive-skill-omits",
    ),
    pytest.param(
        partial(
            _replace,
            _CONTRACT,
            "\n## Next",
            "| `audit` | `audit` | standard batch — no `--query-kind` | x |\n\n## Next",
        ),
        "route table rows: missing=[] unexpected=['audit']",
        id="row-without-consumer",
    ),
    pytest.param(
        partial(_replace, _CONTRACT, "| `assess` | `analysis` | standard batch — no `--query-kind` | x |\n", ""),
        "route table rows: missing=['assess']",
        id="consumer-without-row",
    ),
    pytest.param(
        partial(_unlink, "skills/code-review/references/source-and-scope.md"),
        "invocation reference `source-and-scope.md` is missing",
        id="invocation-reference-missing",
    ),
]


def test_passes_on_a_tree_where_every_roster_agrees(
    checker: ModuleType, plugin_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A plugin tree that satisfies every roster exits 0 and prints nothing.

    The baseline each drift case below departs from, so a failing case is caused by its one change alone.
    """
    assert _run(checker, plugin_root, capsys) == (0, "")


@pytest.mark.parametrize(("mutation", "message"), _DRIFTS)
def test_reports_each_roster_drift(
    checker: ModuleType, plugin_root: Path, capsys: pytest.CaptureFixture[str], mutation: Mutation, message: str
) -> None:
    """One drift in an otherwise consistent tree exits 1 with a line that names the drift and the roster to update."""
    mutation(plugin_root)

    code, output = _run(checker, plugin_root, capsys)

    assert code == 1
    assert message in output
