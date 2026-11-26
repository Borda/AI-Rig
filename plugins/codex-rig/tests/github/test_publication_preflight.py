"""Exercise the publication preflight with the installed reader and a fake gh on PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "shared" / "publication_preflight.py"
PR_URL = "https://github.com/acme/widgets/pull/17"
HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "fix/update%route"
REF = f"refs/heads/{BRANCH}"
PR = {
    "state": "OPEN",
    "baseRefName": "main",
    "headRefName": BRANCH,
    "headRepository": {"name": "widgets"},
    "headRepositoryOwner": {"login": "contributor"},
    "headRefOid": HEAD,
    "isCrossRepository": True,
    "url": PR_URL,
    "number": 17,
    "title": "Update widgets",
    "baseRefOid": BASE,
}
PR_VIEW = ["pr", "view", PR_URL, "--json", ",".join(PR)]
REF_READ = ["api", "/repos/contributor/widgets/git/ref/heads/fix/update%25route", "--hostname", "github.com"]
HEAD_PERMISSION = ["repo", "view", "github.com/contributor/widgets", "--json", "viewerPermission"]
BASE_PERMISSION = ["repo", "view", "github.com/acme/widgets", "--json", "viewerPermission"]
FORK_PERMISSION = ["api", "/repos/acme/widgets/pulls/17", "--hostname", "github.com"]
REF_RESPONSE = {"ref": REF, "object": {"type": "commit", "sha": HEAD}}
UNKNOWN_FIELD = 'Unknown JSON field: "secret-field"\nAvailable fields:\n  title\n'

FAKE_GH = """\
import json, sys
from pathlib import Path

calls = Path("calls.jsonl")
index = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
args = sys.argv[1:]
if Path(sys.argv[0]).name != "fake_gh.py":
    args = [Path(sys.argv[0]).name, *args]
with calls.open("a", encoding="utf-8") as log:
    print(json.dumps(args), file=log)
expected, status, response = json.loads(Path("responses.json").read_text(encoding="utf-8"))[index]
assert args == expected, (args, expected)
if status:
    print("private-partial-stdout")
    print(response, file=sys.stderr)
else:
    print(json.dumps(response))
sys.exit(status)
"""


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write admitted routing and expose only the scripted GitHub executable for the read."""
    routing = {
        "pr_url": PR_URL,
        "pr_number": 17,
        "base_repo": "acme/widgets",
        "base_ref": "main",
        "head_repo": "contributor/widgets",
        "head_ref": BRANCH,
        "head_oid": HEAD,
    }
    (tmp_path / "pr-routing.json").write_text(json.dumps(routing), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        # CreateProcess resolves an exe for the bare gh command. Python runs each named command as a script.
        executable = Path(sys._base_executable)
        shutil.copy2(executable, bin_dir / "gh.exe")
        for command in ("pr", "api", "repo"):
            (tmp_path / command).write_text(FAKE_GH, encoding="utf-8")
        monkeypatch.setenv("PATH", str(executable.parent), prepend=os.pathsep)
        monkeypatch.setenv("PYTHONHOME", sys.base_prefix)
    else:
        script = tmp_path / "fake_gh.py"
        script.write_text(FAKE_GH, encoding="utf-8")
        launcher = bin_dir / "gh"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    monkeypatch.setenv("GH_TOKEN", "dummy")
    monkeypatch.setenv("GH_HOST", "x.invalid")
    return tmp_path


def _run(checkout: Path, *args: str) -> tuple[int, dict, str]:
    """Run the CLI, requiring one parseable JSON line and a clean diagnostic stream."""
    result = subprocess.run(
        [sys.executable, str(HELPER), "--routing", "pr-routing.json", *args],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stderr == "", result.stderr
    assert len(result.stdout.splitlines()) == 1
    return result.returncode, json.loads(result.stdout), result.stdout


def _responses(checkout: Path, *steps: tuple[list[str], int, object]) -> None:
    """Install a sequence of expected gh commands and their scripted responses."""
    (checkout / "responses.json").write_text(json.dumps(steps), encoding="utf-8")


def test_verifies_pr_identity_exact_ref_and_head_write_access(checkout: Path) -> None:
    """An exact branch commit and push permission on the PR head repository allow eligibility to continue."""
    _responses(
        checkout, (PR_VIEW, 0, PR), (REF_READ, 0, REF_RESPONSE), (HEAD_PERMISSION, 0, {"viewerPermission": "WRITE"})
    )

    code, verdict, _ = _run(checkout)

    assert code == 0
    assert verdict["status"] == "ok"
    assert verdict["retry"] is False
    assert verdict["pr"] == PR
    assert verdict["base_repo"] == "acme/widgets"
    assert verdict["head_repo"] == "contributor/widgets"
    assert verdict["ref"] == REF
    assert verdict["remote_head_oid"] == HEAD


def test_nonallowlisted_field_is_refused_before_running_gh(checkout: Path) -> None:
    """Caller-provided fields cannot expand the shipped PR query."""
    code, verdict, _ = _run(checkout, "--fields", "state,headRefName,baseRepository")

    assert code == 2
    assert verdict["status"] == "failed"
    assert verdict["reason"] == "unknown-json-field"
    assert verdict["retry"] is False
    assert not (checkout / "calls.jsonl").exists()


def test_field_projection_keeps_the_fixed_query_and_verification(checkout: Path) -> None:
    """A smaller output projection still verifies the full PR and exact head ref."""
    _responses(
        checkout, (PR_VIEW, 0, PR), (REF_READ, 0, REF_RESPONSE), (HEAD_PERMISSION, 0, {"viewerPermission": "ADMIN"})
    )

    code, verdict, _ = _run(checkout, "--fields", "title")

    assert code == 0
    assert verdict["pr"] == {"title": PR["title"]}
    assert verdict["ref"] == REF
    assert verdict["remote_head_oid"] == HEAD


@pytest.mark.parametrize(
    ("failure", "reason", "retry"),
    [
        pytest.param(UNKNOWN_FIELD, "unknown-json-field", False, id="unknown-field"),
        pytest.param(
            "The response contains terminal escape sequences",
            "terminal-escape-sequences-refused",
            False,
            id="terminal-escape",
        ),
        pytest.param("connection reset by peer", "connection-reset", True, id="network-reset"),
        pytest.param("HTTP 429: rate limit", "rate-limit", True, id="rate-limit"),
        pytest.param("an unexpected error occurred", "unclassified", False, id="unclassified"),
    ],
)
def test_reader_error_reports_safe_reason_and_retry_decision(
    checkout: Path, failure: str, reason: str, retry: bool
) -> None:
    """A reader failure carries only its safe reason and tells the workflow if it can retry."""
    _responses(checkout, (PR_VIEW, 1, failure))

    code, verdict, output = _run(checkout)

    assert code == 2
    assert verdict["status"] == "failed"
    assert verdict["reason"] == reason
    assert verdict["retry"] is retry
    assert "secret-field" not in output
    assert "Available fields" not in output
    assert "private-partial-stdout" not in output
    assert "pr" not in verdict


def test_second_reader_failure_finishes_locally_without_an_offer(checkout: Path) -> None:
    """A second failure retains the final reason and yields no verified fields or further retry."""
    _responses(checkout, (PR_VIEW, 1, "HTTP 429: rate limit"), (PR_VIEW, 1, "HTTP 429: rate limit"))

    assert _run(checkout)[1]["retry"] is True
    code, verdict, _ = _run(checkout, "--retry")

    assert code == 2
    assert verdict == {
        "status": "failed",
        "reason": "rate-limit",
        "stage": "pr-view",
        "retry": False,
        "next_action": "Keep delivery local and report the reason without offering publication.",
    }
    assert (checkout / "calls.jsonl").read_text(encoding="utf-8").splitlines() == [json.dumps(PR_VIEW)] * 2


def test_retry_that_recovers_proceeds_to_exact_ref_and_permissions(checkout: Path) -> None:
    """Recovery must verify the destination and existing fork permission rule before returning OK."""
    _responses(
        checkout,
        (PR_VIEW, 1, "connection reset by peer"),
        (PR_VIEW, 0, PR),
        (REF_READ, 0, REF_RESPONSE),
        (HEAD_PERMISSION, 0, {"viewerPermission": "READ"}),
        (FORK_PERMISSION, 0, {"maintainer_can_modify": True}),
        (BASE_PERMISSION, 0, {"viewerPermission": "MAINTAIN"}),
    )

    assert _run(checkout)[1]["retry"] is True
    assert _run(checkout, "--retry")[1]["status"] == "ok"


def test_fork_that_forbids_maintainer_edits_requires_head_write_access(checkout: Path) -> None:
    """Base permissions cannot qualify a fork that has disabled maintainer edits."""
    _responses(
        checkout,
        (PR_VIEW, 0, PR),
        (REF_READ, 0, REF_RESPONSE),
        (HEAD_PERMISSION, 0, {"viewerPermission": "READ"}),
        (FORK_PERMISSION, 0, {"maintainer_can_modify": False}),
    )

    code, verdict, _ = _run(checkout)

    assert code == 2
    assert verdict["reason"] == "publication-permission-unverified"
    assert verdict["retry"] is False


@pytest.mark.parametrize(
    ("remote_pr", "ref_response", "reason"),
    [
        pytest.param({**PR, "baseRefName": "release"}, REF_RESPONSE, "publication-pr-changed", id="retargeted"),
        pytest.param(PR, {**REF_RESPONSE, "ref": "refs/heads/other"}, "publication-ref-mismatch", id="wrong-ref"),
        pytest.param(
            PR, {**REF_RESPONSE, "object": {"type": "commit", "sha": BASE}}, "publication-ref-mismatch", id="moved-tip"
        ),
    ],
)
def test_pr_or_ref_drift_cannot_qualify(checkout: Path, remote_pr: dict, ref_response: dict, reason: str) -> None:
    """A changed base, different ref, or moved tip must stop eligibility without a retry."""
    _responses(checkout, (PR_VIEW, 0, remote_pr), (REF_READ, 0, ref_response))

    code, verdict, _ = _run(checkout)

    assert code == 2
    assert verdict["reason"] == reason
    assert verdict["retry"] is False
