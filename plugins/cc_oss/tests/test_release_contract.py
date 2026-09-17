"""Execute the release gather contract against deterministic external command boundaries."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


_skip_shell_unavailable = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="The shipped release command requires bash and jq.",
)


@_skip_shell_unavailable
@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["maintenance", "empty", "failure"])
def test_release_gather_discovers_prs_by_candidate_commit(scenario: str) -> None:
    """Keep maintenance-branch squash authors, all pages, and failed discovery distinguishable."""
    skill = Path(__file__).resolve().parents[1] / "skills/release/SKILL.md"
    blocks = re.findall(r"```bash\n(.*?)```", skill.read_text(encoding="utf-8"), re.DOTALL)
    snippet = next(block for block in blocks if "COMMIT_SHAS=" in block)
    pages = [
        [
            {
                "number": 17,
                "title": "Maintenance fix",
                "user": {"login": "squash-author"},
                "base": {"ref": "stable/1.x"},
                "merge_commit_sha": "candidate",
                "merged_at": "2026-09-01",
            }
        ],
        [
            {
                "number": 18,
                "title": "Related fix",
                "user": {"login": "second-author"},
                "base": {"ref": "stable/1.x"},
                "merge_commit_sha": "candidate",
                "merged_at": "2026-09-02",
            }
        ],
    ]
    payload = json.dumps(pages if scenario == "maintenance" else [[]])
    # Shell functions replace only Git/GitHub boundaries; the shipped loop and jq run unchanged.
    boundaries = """
git() { [ "$1" = rev-list ] && [ "$2" = candidate-range ] || return 91; printf '%s\n' candidate; }
gh() {
    [ "$*" = 'api --method GET --paginate --slurp repos/{owner}/{repo}/commits/candidate/pulls -f per_page=100' ] || return 92
    [ "$1" = api ] || return 93
    [ "$scenario" != failure ] || return 1
    printf '%s' "$payload"
}
RANGE=candidate-range
"""
    completed = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            'scenario="$1"; payload="$2"\n' + boundaries + snippet,
            "release-contract",
            scenario,
            payload,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if scenario == "failure":
        assert completed.returncode != 0
        assert "PR association coverage unavailable for candidate" in completed.stderr
        assert not completed.stdout
        return
    assert completed.returncode == 0, completed.stderr
    gathered = json.loads(completed.stdout)
    assert gathered["commit"] == "candidate"
    assert [pr["author"]["login"] for pr in gathered["pull_requests"]] == (
        ["squash-author", "second-author"] if scenario == "maintenance" else []
    )
    assert all(pr["base"] == "stable/1.x" for pr in gathered["pull_requests"])
