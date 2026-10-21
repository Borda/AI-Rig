"""Guard the Phase 3 resumed-plan fence against dropping the existing merge plan before it can be rebuilt."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_PHASE3 = _PLUGIN / "skills/resolve/modes/dispatch-phase3.md"
_BASH = shutil.which("bash")
_SESSION = "resume-plan-test"
_OLD_PLAN = '[{"item_id": "9"}]'


def _resume_fence() -> str:
    """Return the executable bash block that rebuilds the merge plan from ``merge-result.json``."""
    text = _PHASE3.read_text(encoding="utf-8")
    pos = text.index('--resume-from "$IMPL_DIR/merge-result.json"')
    start = text.rindex("```bash", 0, pos) + len("```bash\n")
    return text[start : text.index("```", start)]


def _run_fence(tmp_path: Path, merge_result: str | None, commit_rows: list[dict[str, str]]) -> tuple[int, Path]:
    """Run the resume fence in a scratch impl dir holding an old plan; return its exit code and the plan path."""
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    (tmp_path / f"resolve-impl-dir-{_SESSION}").write_text(f"{impl_dir.as_posix()}\n", encoding="utf-8", newline="\n")
    plan = impl_dir / "merge-plan.json"
    plan.write_text(_OLD_PLAN, encoding="utf-8", newline="\n")
    rows = "".join(json.dumps(row) + "\n" for row in commit_rows)
    (impl_dir / "phase2-commits.jsonl").write_text(rows, encoding="utf-8", newline="\n")
    (impl_dir / "action-items.jsonl").write_text("", encoding="utf-8", newline="\n")
    if merge_result is not None:
        (impl_dir / "merge-result.json").write_text(merge_result, encoding="utf-8", newline="\n")
    completed = subprocess.run(
        [_BASH, "-c", _resume_fence()],
        cwd=tmp_path,
        env=os.environ
        | {
            "CLAUDE_CODE_SESSION_ID": _SESSION,
            "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
            "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return completed.returncode, plan


@pytest.mark.skipif(_BASH is None, reason="The resume fence runs under Bash")
@pytest.mark.parametrize(
    ("merge_result", "commit_rows", "expected_rc", "expected_plan"),
    [
        pytest.param(None, [], 1, _OLD_PLAN, id="unreadable-remaining-keeps-the-old-plan"),
        pytest.param('{"remaining": []}', [], 0, "[]", id="empty-remaining-writes-an-empty-plan"),
        pytest.param(
            '{"remaining": ["1"]}',
            [{"item_id": "1", "sha": "a" * 40, "group": "g"}, {"item_id": "1", "sha": "b" * 40, "group": "g"}],
            1,
            None,
            id="failed-build-leaves-no-stale-plan",
        ),
    ],
)
def test_resume_fence_plan_survives_only_when_nothing_was_read(
    tmp_path: Path,
    merge_result: str | None,
    commit_rows: list[dict[str, str]],
    expected_rc: int,
    expected_plan: str | None,
) -> None:
    """The old plan survives an unreadable ``remaining`` and is removed when a build fails after reading.

    The old plan is the only record of the unapplied entries until the rebuild succeeds. Deleting it before the script
    has read ``merge-result.json`` made an unreadable result unrecoverable; a build that fails after reading must still
    not leave the full old plan for the merge fence to replay.
    """
    returncode, plan = _run_fence(tmp_path, merge_result, commit_rows)

    assert returncode == expected_rc
    assert (plan.read_text(encoding="utf-8") if plan.exists() else None) == expected_plan
