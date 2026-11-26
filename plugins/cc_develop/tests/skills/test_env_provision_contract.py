"""Guard the shared targeted recovery instructions and the read-only review boundary."""

from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skills"
LOAD = 'python "$PLUGIN_ROOT/bin/dev_load_shared.py" env-provision.md'


@pytest.mark.parametrize("skill", ["fix", "feature", "refactor"])
def test_mutating_skill_loads_shared_recovery(skill):
    """Each skill names the trigger and loads the same protocol using its guarded resolver."""
    lines = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    assert lines.count(LOAD) == 1
    start = lines.index(LOAD)
    assert 'resolve_plugin_root.py" --plugin-name develop) || exit $?' in lines[start - 2]
    assert "targeted Python test fails to import a declared package" in lines[start - 5]


def test_shared_recovery_names_trigger_receipt_and_same_target_rerun():
    """Pin the runtime trigger, helper call, literal argv rerun and evidence contract to their lines."""
    lines = (SKILLS / "_shared" / "env-provision.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "<!-- file: env-provision.md — consumers: /develop:fix, /develop:feature, /develop:refactor -->"
    steps = [line for line in lines if line[:2] in ("1.", "2.", "3.", "4.")]
    assert len(steps) == 4
    assert all(term in steps[0] for term in ("ModuleNotFoundError", "No module named", "declared dependency"))
    assert "once per skill invocation" in steps[1]
    assert "same targeted arguments" in steps[2]
    assert "receipt path" in steps[3]
    assert any('dev_env_provision.py" --repo "$PWD" --out "${ENV_RECEIPT:?}" --probe-import' in line for line in lines)
    assert any('dev_run_tests.py" pytest --env "${ENV_RECEIPT:?}" -- "$@"' in line for line in lines)


def test_step_three_retries_selector_with_its_record_and_keeps_direct_form():
    """Preserve the selector-owned evidence and the direct runner's argument boundary."""
    lines = (SKILLS / "_shared" / "env-provision.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("3. "))
    end = next(i for i, line in enumerate(lines) if line.startswith("4. "))
    step = lines[start:end]
    selector = next(line for line in step if "`dev_test_targets.py`" in line)
    assert "same `dev_test_targets.py` command" in selector
    assert '`--env "$ENV_RECEIPT"`' in selector
    assert "`--run`" in selector
    assert "`--record-dir`" in selector
    assert not any("selection JSON" in line or "recover the list" in line for line in step)
    assert any('dev_run_tests.py" pytest --env "${ENV_RECEIPT:?}" -- "$@"' in line for line in step)
    assert any("verdict nonce checks" in line for line in step)


def test_review_names_provisioner_only_as_closing_check():
    """The review consolidator reports the helper under Next steps and preserves the no-install boundary."""
    path = SKILLS / "review" / "templates" / "consolidator-prompt.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    bullet = next(line for line in lines if line.startswith("- A missing or out-of-spec Python package"))
    assert "`Next steps`" in bullet
    assert "`dev_env_provision.py --repo <root> --probe-import <module>`" in bullet
    assert bullet.endswith("Review agents never close it by installing anything.")
