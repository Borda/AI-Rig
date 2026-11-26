"""Hold the grouped-commit replay helper to an explicit mutating-helper registration."""

import build_blueprint_manifest as bbm
import pytest


def test_name_is_recognised_as_a_committing_helper() -> None:
    """The replay helper creates commits, so the danger filter must see its name as mutating."""
    assert bbm._MUTATING_HELPER.search("resolve_group_replay.py")


def test_registered_call_is_admitted_with_its_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """With its guard listed the helper call is admitted; without it the same command is refused, never kept."""
    known = frozenset({"@python:PLUGIN_ROOT"})
    assert bbm._helper_use("$PLUGIN_ROOT/bin/resolve_group_replay.py", known) is None
    monkeypatch.delitem(bbm._MUTATING_HELPERS, "resolve_group_replay.py")
    assert bbm._helper_use("$PLUGIN_ROOT/bin/resolve_group_replay.py", known) == (
        "mutating helper resolve_group_replay.py"
    )


def test_guard_states_the_commit_boundary() -> None:
    """The registration records the effect being auto-allowed: private index, hook-approval boundary, no push."""
    guard = bbm._MUTATING_HELPERS["resolve_group_replay.py"]
    assert all(term in guard for term in ("private index", "hook-approval boundary", "never pushes"))
