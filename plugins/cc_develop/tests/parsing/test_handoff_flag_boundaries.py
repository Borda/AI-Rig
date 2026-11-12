"""Exercise clean argument handoffs between develop skills."""

from pathlib import Path

import dev_parse_args
import pytest
from derive_codemap_target import derive_target


@pytest.mark.parametrize(
    ("skill", "arguments", "clean", "variable", "value"),
    [
        pytest.param(
            "feature",
            "add retries --plan .plans/retries.md",
            "add retries",
            "PLAN_FILE",
            ".plans/retries.md",
            id="plan-feature-handoff",
        ),
        pytest.param(
            "feature",
            "123 --plan .plans/retries.md",
            "123",
            "PLAN_FILE",
            ".plans/retries.md",
            id="bare-issue-with-plan",
        ),
        pytest.param("fix", "--plan .plans/fix.md #42", "#42", "PLAN_FILE", ".plans/fix.md", id="leading-fix-plan"),
        pytest.param(
            "fix",
            "#42 --diagnosis .developments/diagnosis.md",
            "#42",
            "DIAG_FILE",
            ".developments/diagnosis.md",
            id="fix-diagnosis",
        ),
        pytest.param(
            "refactor",
            "simplify parser --plan .plans/parser.md",
            "simplify parser",
            "PLAN_FILE",
            ".plans/parser.md",
            id="refactor-plan",
        ),
        pytest.param("feature", "add retries --issue 42", "add retries", "ISSUE_REF", "42", id="feature-issue"),
        pytest.param("debug", "--issue 42 inspect timeout", "inspect timeout", "ISSUE_REF", "42", id="debug-issue"),
    ],
)
def test_handoff_values_do_not_become_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill: str, arguments: str, clean: str, variable: str, value: str
) -> None:
    """Keep file-valued flags and issue references out of goal and codemap target detection."""
    monkeypatch.setenv("CSID", "handoff")
    values = dev_parse_args.write_skill_files(skill, arguments, tmp_dir=tmp_path)
    actual = (tmp_path / f"dev-{skill}-clean-args-handoff").read_text().strip()
    assert actual == clean
    assert values[variable] == value
    assert derive_target(actual) == ("", "")
