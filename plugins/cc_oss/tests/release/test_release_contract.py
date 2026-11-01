"""Execute the release gather contract against deterministic external command boundaries."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[2] / "skills/release/SKILL.md"
_RECORD = {
    "commit": "candidate",
    "pull_requests": [{"number": 17, "author": {"login": "squash-author"}, "base": "stable/1.x"}],
}
#: Helper arguments the gather block must pass, then the name of the resumable offset file it hands over.
_EXPECTED_ARGV = ["plug/bin/gh_batch_query.py", "commit-prs", "--range", "candidate-range", "--offset-file"]
_EXPECTED_OFFSET_NAME = "release-assoc-offset-contract"

_skip_shell_unavailable = pytest.mark.skipif(
    shutil.which("bash") is None, reason="The shipped release command requires bash."
)


def _gather_snippet() -> str:
    """Return the first bash fence of the release skill that calls the batched PR-association helper."""
    fences, current = [], None
    for line in _SKILL.read_text(encoding="utf-8").splitlines(keepends=True):
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip().startswith("```"):
            fences.append("".join(current))
            current = None
        else:
            current.append(line)
    return next(fence for fence in fences if "gh_batch_query.py" in fence)


def _run_gather(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the shipped gather block with a shell function standing in for the Python helper call.

    Returns the finished process and the file the stand-in writes its argument vector to.
    """
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
            'scenario="$1"; record="$2"; calls="$3"\n' + boundaries + _gather_snippet(),
            "release-contract",
            scenario,
            json.dumps(_RECORD),
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
    return completed, calls


def _recorded_call(calls: Path) -> tuple[list[str], str]:
    """Split the recorded helper invocation into its leading arguments and the offset file name."""
    argv = calls.read_text(encoding="utf-8").split()
    return argv[:-1], Path(argv[-1]).name


@_skip_shell_unavailable
@pytest.mark.integration
class TestGatherPrAssociation:
    """The shipped gather block discovers pull requests through the batched helper, keyed by candidate commit.

    The helper itself is covered by ``test_gh_batch_query.py``. Here only the Python call is replaced by a shell
    function, so the shipped bash (range reload, guard, error message) runs unchanged.
    """

    def test_streams_helper_records_unchanged(self, tmp_path: Path) -> None:
        """Gather passes the resolved range and a resumable offset file to the helper and relays its records."""
        completed, calls = _run_gather(tmp_path, "maintenance")

        assert completed.returncode == 0, completed.stderr
        assert _recorded_call(calls) == (_EXPECTED_ARGV, _EXPECTED_OFFSET_NAME)
        assert json.loads(completed.stdout) == _RECORD

    def test_fails_closed_when_batch_query_fails(self, tmp_path: Path) -> None:
        """A failing helper stops gather with a coverage error and emits no partial records.

        Continuing after a failed batch would classify a release from an incomplete PR association set.
        """
        completed, calls = _run_gather(tmp_path, "failure")

        assert completed.returncode != 0
        assert "PR association coverage unavailable" in completed.stderr
        assert not completed.stdout
        assert _recorded_call(calls) == (_EXPECTED_ARGV, _EXPECTED_OFFSET_NAME)

    def test_unresolved_range_never_queries(self, tmp_path: Path) -> None:
        """An empty range aborts gather before the helper is ever called.

        The range reload guard must fire first; querying with an empty range would silently match every commit.
        """
        completed, calls = _run_gather(tmp_path, "unresolved-range")

        assert completed.returncode != 0
        assert "release range unresolved" in completed.stderr
        assert not calls.exists()
