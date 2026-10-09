"""Contract test: every ``bin/*.py`` script's ``--help`` flag behaves per the stdlib argparse contract.

Consolidates 13 near-identical ``test_help_exits_0``/``test_help_flag_exits_zero``
bodies that previously lived one per script test file (see
``.temp/test-audit/cc_oss.md`` judgement call 2) into a single parametrized
test that walks ``bin/`` dynamically via ``conftest.py``'s module loader —
so a newly added script is covered automatically without a companion
help-only test file. Scripts already covered by a dedicated test asserting
a genuine per-script side effect (subprocess/git never invoked on the help
path, or literal-stdout consumption via ``subprocess.run``) stay out of
this file to avoid asserting the same fact twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Mirrors conftest.py's own _BIN_DIR — not a second loader: the modules
# themselves are already exec'd into sys.modules by conftest's autouse
# `_load_bin_modules()` before any test runs; this glob only supplies the
# names to parametrize over.
_BIN_DIR = Path(__file__).parent.parent / "bin"

#: Scripts whose --help path already has a dedicated test elsewhere that
#: additionally asserts a genuine per-script side effect — re-asserting
#: "usage: exits 0" here would be duplicate coverage, not new signal.
_COVERED_BY_DEDICATED_TEST = {
    "commit_action_item",  # test_commit_action_item.py — asserts git/subprocess never invoked
    "commit_all_items",  # test_commit_all_items.py — asserts git/subprocess never invoked
    "commit_lint_fixes",  # test_commit_lint_fixes.py — asserts git/subprocess never invoked
    "resolve_preflight",  # test_resolve_preflight.py — asserts git/subprocess never invoked
    "parse_resolve_args",  # test_parse_resolve_args.py — subprocess.run, consumer evals stdout
    "parse_skill_flags",  # test_parse_skill_flags.py — subprocess.run, consumer evals stdout
    "release_setup",  # test_release_setup.py — asserts `which` (git) never invoked on --help
}

#: Scripts whose main() parses --help via argparse but catches the
#: resulting SystemExit internally and returns an int instead of letting
#: it propagate (normalises argparse's exit-2 to the bin/ convention of
#: exit-1) — cannot use the standard "pytest.raises(SystemExit)" shape.
_RETURNS_INSTEAD_OF_RAISING = {"fetch_gh_data_group2", "detect_thread_type"}

_ALL_SCRIPTS = sorted(p.stem.replace("-", "_") for p in _BIN_DIR.glob("*.py"))

_STANDARD_HELP_SCRIPTS = [
    name for name in _ALL_SCRIPTS if name not in _COVERED_BY_DEDICATED_TEST and name not in _RETURNS_INSTEAD_OF_RAISING
]


@pytest.mark.parametrize("module_name", [pytest.param(name, id=name) for name in _STANDARD_HELP_SCRIPTS])
def test_help_flag_exits_zero(module_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Print command usage before exiting successfully.

    Every script in ``bin/`` except the dedicated-test and no-help-surface exclusions (module-level sets above) uses
    argparse's own ``-h`` handling directly, which always raises before any other argument is validated. A script
    dropped into ``bin/`` later is picked up here automatically via the live directory glob — nothing to wire by hand.
    """
    module = sys.modules[module_name]
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("module_name", [pytest.param(name, id=name) for name in sorted(_RETURNS_INSTEAD_OF_RAISING)])
def test_help_flag_returns_zero_without_raising(module_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Print usage and return 0 without raising ``SystemExit``.

    ``fetch_gh_data_group2.py`` and ``detect_thread_type.py`` wrap argparse's ``parse_args`` in a try/except that
    catches the SystemExit raised for ``-h`` and converts it to a plain return value, so that bad positional args
    normalise to the bin/ convention's exit-1 instead of argparse's own exit-2. The --help path still exits 0 overall,
    just via a returned int rather than a propagated exception.
    """
    module = sys.modules[module_name]
    rc = module.main(["--help"])
    assert rc == 0
    assert "usage:" in capsys.readouterr().out
