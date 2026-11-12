"""Keep specialist inspection permissions and handovers consistent with their owning workflows."""

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def test_python_reviewer_permits_route_scoped_reads_without_mutation() -> None:
    """The mandatory Python reviewer can inspect source while retaining the read-only route boundary."""
    lines = (PLUGIN_ROOT / "roles/sw-engineer/ROLE.md").read_text(encoding="utf-8").splitlines()
    constraint = next(line for line in lines if line.startswith("- In Code Review, follow"))

    assert "use only the selected route's scoped read-only tools or generated page reads" in constraint
    assert "no tools" not in constraint
    assert (
        "no edits, mutating commands, other execution, network, credential access, delegation, or escalation"
        in constraint
    )
    assert "The parent owns all mutations and acceptance." in constraint


def test_delegation_lead_requires_recovery_below_complete_handover_threshold() -> None:
    """Cautious specialist handovers carry recovery evidence and cannot be accepted as complete below 0.90."""
    lines = (PLUGIN_ROOT / "roles/delegation-lead/ROLE.md").read_text(encoding="utf-8").splitlines()
    confidence = next(line for line in lines if "Specialist confidence below 0.85" in line)

    assert "Specialist confidence below 0.85 fails handover gate" in confidence
    assert "0.85 to below 0.90 permit only incomplete or cautious handovers" in confidence
    assert "recorded objective evidence, recovery actions, and remaining limits" in confidence
    assert "Accept a specialist handover as complete only at 0.90 or above" in confidence


def test_oss_shepherd_routes_implementation_and_test_review_to_distinct_roles() -> None:
    """OSS lifecycle advice hands implementation correctness and test coverage to their respective specialists."""
    lines = (PLUGIN_ROOT / "roles/oss-shepherd/ROLE.md").read_text(encoding="utf-8").splitlines()
    handover = next(line for line in lines if line.startswith("Return: lifecycle decision"))

    assert "Hand implementation-correctness review to `sw-engineer`, test coverage to `qa-specialist`" in handover
    assert "to `code-review`" not in handover
    assert (
        "Runtime-changing and release-blocking implementation acceptance remain with parent or domain owner."
        in handover
    )
