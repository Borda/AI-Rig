"""Protect Codex routing and completion decisions for changed Codemap contracts."""

from pathlib import Path

_SKILLS = Path(__file__).resolve().parents[2] / "codex-skills"


def _section(skill: str, heading: str) -> list[str]:
    """Read one workflow section as individual lines for contract assertions."""
    lines = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("### ")), len(lines))
    return lines[start:end]


def test_integration_routes_telemetry_before_defaulting_flags_to_audit() -> None:
    """Telemetry requests reach debrief while flags-only health requests retain their flags."""
    route = next(line for line in _section("integration", "### 1. Resolve mode") if line.startswith("First, if"))

    assert "telemetry" in route
    assert "$codemap-py:debrief-coding" in route
    assert "first token starts with `--` → audit" in route
    assert "preserve all flags" in route


def test_integration_never_retries_a_launcher_probe_failure() -> None:
    """Missing CPython stops the workload and preserves the launcher's diagnostic."""
    report = next(line for line in _section("integration", "### 3. Report") if line.startswith("Explain"))

    assert "`127`" in report
    assert "stderr" in report
    assert "never retry with another interpreter" in report
    assert "`audit`/`demo`" in report


def test_debrief_classifies_absence_from_stderr_instead_of_exit_one() -> None:
    """Crashes cannot masquerade as an empty telemetry dataset."""
    route = next(line for line in _section("debrief-coding", "### 1. Compute statistics") if "Exit `1`" in line)

    assert "only when stderr starts with `No codemap telemetry found`" in route
    assert "Any other exit `1` is a helper crash" in route
    assert "relay stderr" in route


def test_rename_preflight_retains_source_and_explicit_stale_consent() -> None:
    """Stale consent permits source-verified callers without querying ranges again after edits."""
    preflight = _section("rename-refs", "### 3. Report and confirm")
    apply = _section("rename-refs", "### 4. Apply symbol edits")

    assert any("recorded Proceed anyway" in line and "source-verified" in line for line in preflight)
    assert any("caller-ranges" in line and "source" in line for line in preflight)
    assert any("caller-ranges" in line and "without re-querying" in line for line in apply)
    assert any("1:1 substitutions" in line for line in apply)


def test_rename_completion_requires_identity_and_bounded_import_smoke() -> None:
    """Completion checks consumer identities and imports while keeping test execution advisory."""
    verify = _section("rename-refs", "### 6. Re-scan and verify")

    assert any("Changelog/history" in line and "advisory" in line for line in verify)
    assert any("(`caller`, `path`)" in line and "count" in line for line in verify)
    assert any("20-second" in line and "separate processes" in line for line in verify)
    assert any("pytest" in line and "advisory" in line for line in verify)
    assert any("both modes" in line and "complete, fresh post-query metadata" in line for line in verify)


def test_omitted_target_preserves_whole_change_scope_and_failure_routing() -> None:
    """Mapped and unmapped changes select a union; unavailable diff analysis still asks for a target."""
    route = (_SKILLS / "test-impact/SKILL.md").read_text(encoding="utf-8").splitlines()

    assert any("len(changed_modules) + len(unmapped_files) > 1" in line for line in route)
    assert any("--diff-impact" in line and "summarize_test_impact.py" in line for line in route)
    assert any("whole-change analysis unavailable" in line and "User Questions" in line for line in route)
    assert any("changed_modules[].errors" in line and "unmapped_files" in line for line in route)
    assert any("--no-mocks" in line and "test_impact_union.py" in line for line in route)
    assert any("target_caveats" in line and "diff_index" in line for line in route)
    assert any("original" in line and "--no-mocks" in line for line in route)
    assert any('PATH="PLUGIN_ROOT/bin:$PATH"' in line and "test_impact_union.py" in line for line in route)
