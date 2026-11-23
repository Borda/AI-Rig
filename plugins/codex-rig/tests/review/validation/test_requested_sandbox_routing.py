"""Keep skill-level sandbox requests usable for ordinary in-place project commands."""

from pathlib import Path


def test_requested_sandbox_covers_ordinary_in_place_gates_without_an_inner_profile() -> None:
    """Require host-owned confinement evidence and retain the strict fallback for an unverified host."""
    contract = Path(__file__).resolve().parents[3] / "shared" / "native-skill-contract.md"
    section = contract.read_text(encoding="utf-8").split("## Sandboxed Test Runs\n", 1)[1].split("\n## ", 1)[0]
    outer = next(line for line in section.splitlines() if line.startswith("- **Host-enforced execution.**"))
    assert all(
        token in outer
        for token in (
            "in-place remediation",
            'sandbox_permissions="use_default"',
            "descendants",
            "without its `--sandbox` option",
            "unrestricted approval rule",
            "host tool call",
        )
    )
    fallback = next(line for line in section.splitlines() if line.startswith("- **Independent confinement.**"))
    assert all(
        token in fallback for token in ("--sandbox-gate <gate>", "run_gates.py", "Linux", "Windows", "unexecuted")
    )
    assert "explicit workflow-wide sandbox request uses `--sandbox` for every gate" in fallback
