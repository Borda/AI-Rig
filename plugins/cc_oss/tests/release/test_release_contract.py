"""Execute the release gather contract against deterministic external command boundaries."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_skip_shell_unavailable = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="The shipped release command requires bash.",
)


@_skip_shell_unavailable
@pytest.mark.integration
@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param("maintenance", id="records-streamed-unchanged"),
        pytest.param("failure", id="failed-batch-is-fail-closed"),
        pytest.param("unresolved-range", id="empty-range-never-queries"),
    ],
)
def test_release_gather_discovers_prs_by_candidate_commit(scenario: str, tmp_path: Path) -> None:
    """Keep the shipped block wired to the batched helper: right arguments, resumable offset, fail-closed on error.

    The helper itself is covered by ``test_gh_batch_query.py``. Here only the Python call is replaced by a shell
    function, so the shipped bash (range reload, guard, error message) runs unchanged.
    """
    skill = Path(__file__).resolve().parents[2] / "skills/release/SKILL.md"
    blocks = re.findall(r"```bash\n(.*?)```", skill.read_text(encoding="utf-8"), re.DOTALL)
    snippet = next(block for block in blocks if "gh_batch_query.py" in block)
    record = {
        "commit": "candidate",
        "pull_requests": [{"number": 17, "author": {"login": "squash-author"}, "base": "stable/1.x"}],
    }
    calls = tmp_path / "calls"
    boundaries = """
python() {
    printf '%s\\n' "$*" > "$calls"
    [ "$scenario" != failure ] || return 2
    printf '%s\\n' "$record"
}
RANGE=candidate-range
[ "$scenario" != unresolved-range ] || RANGE=""
"""
    completed = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            'scenario="$1"; record="$2"; calls="$3"\n' + boundaries + snippet,
            "release-contract",
            scenario,
            json.dumps(record),
            str(calls),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "contract",
            "CLAUDE_PLUGIN_ROOT": "plug",
        },
    )
    if scenario == "unresolved-range":
        assert completed.returncode != 0
        assert "release range unresolved" in completed.stderr
        assert not calls.exists()
        return
    argv = calls.read_text(encoding="utf-8").split()
    assert argv[:-1] == [
        "plug/bin/gh_batch_query.py",
        "commit-prs",
        "--range",
        "candidate-range",
        "--offset-file",
    ]
    assert Path(argv[-1]).name == "release-assoc-offset-contract"
    if scenario == "failure":
        assert completed.returncode != 0
        assert "PR association coverage unavailable" in completed.stderr
        assert not completed.stdout
        return
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == record
