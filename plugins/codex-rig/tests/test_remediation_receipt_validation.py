"""Regression checks for schema-v2 remediation checkout receipt validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from test_review_completion_gate import _assessed_pr, _module


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HEAD_BRANCH = "widget-fix"


@pytest.fixture(name="remediation_pr")
def _remediation_pr(tmp_path: Path) -> Path:
    """Create a complete schema-v2 PR receipt ready for remediation routing mutations."""
    fixture_builder = getattr(_assessed_pr, "__wrapped__", None)
    assert callable(fixture_builder)
    return fixture_builder(tmp_path)


def _set_receipt(
    pr_dir: Path,
    *,
    method: str,
    command: str,
    local_branch: str,
    checkout_mode: str = "remediate",
    same_repo: bool = True,
    cross_repository: bool = False,
) -> None:
    """Bind a collector receipt to the complete PR artifact's authoritative identity."""
    pr_path = pr_dir / "pr.json"
    pr_payload = json.loads(pr_path.read_text(encoding="utf-8"))
    pr_payload.update(
        headRefName=HEAD_BRANCH,
        isCrossRepository=cross_repository,
        headRepository={"nameWithOwner": "acme/widgets"},
    )
    pr_path.write_text(json.dumps(pr_payload), encoding="utf-8")

    routing_path = pr_dir / "pr-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing.update(
        checkout_method=method,
        checkout_mode=checkout_mode,
        local_checkout_command=command,
        same_repo=same_repo,
    )
    routing_path.write_text(json.dumps(routing), encoding="utf-8")

    checkout_path = pr_dir / "local-checkout.json"
    checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
    checkout.update(
        checkout_method=method,
        checkout_mode=checkout_mode,
        command=command,
        local_branch=local_branch,
    )
    if method == "git-original-branch-fallback":
        checkout["gh_checkout_failure"] = {
            "command": f"gh pr checkout {routing['pr_url']}",
            "code": "command-failed:local-pr-checkout",
        }
    checkout_path.write_text(json.dumps(checkout), encoding="utf-8")


@pytest.mark.parametrize(
    ("method", "command"),
    [
        pytest.param(
            "gh-pr-checkout",
            "gh pr checkout https://github.com/acme/widgets/pull/123",
            id="native-gh-pr-checkout",
        ),
        pytest.param(
            "git-original-branch-fallback",
            f"git checkout --no-guess {HEAD_BRANCH}",
            id="same-repository-no-guess-fallback",
        ),
        pytest.param(
            "git-original-branch-fallback",
            f"git checkout --track -b {HEAD_BRANCH} origin/{HEAD_BRANCH}",
            id="same-repository-track-fallback",
        ),
    ],
)
def test_remediation_receipt_supported_route_is_accepted(remediation_pr: Path, method: str, command: str) -> None:
    """Accept collector-native and verified same-repository attached checkout receipts."""
    _set_receipt(remediation_pr, method=method, command=command, local_branch=HEAD_BRANCH)

    _module(PLUGIN_ROOT / "shared" / "validate-artifacts.py")._validate_code_remediate_pr_source(
        remediation_pr,
        json.loads((remediation_pr / "pr-routing.json").read_text(encoding="utf-8")),
        json.loads((remediation_pr / "target-branch.json").read_text(encoding="utf-8")),
        json.loads((remediation_pr / "local-checkout.json").read_text(encoding="utf-8")),
    )


@pytest.mark.parametrize(
    "artifact, field, value",
    [
        pytest.param("local-checkout.json", "gh_checkout_failure", None, id="missing-failed-gh-proof"),
        pytest.param("local-checkout.json", "gh_checkout_failure", {}, id="empty-failed-gh-proof"),
        pytest.param(
            "local-checkout.json",
            "gh_checkout_failure",
            {"command": "gh pr checkout https://github.com/acme/widgets/pull/123", "code": ""},
            id="missing-failure-code",
        ),
        pytest.param(
            "local-checkout.json",
            "gh_checkout_failure",
            {"command": "gh pr checkout https://github.com/acme/widgets/pull/124", "code": "command-failed"},
            id="failure-for-wrong-pr",
        ),
        pytest.param("pr.json", "headRepository", {"nameWithOwner": "someone/widgets"}, id="forged-same-repo-flag"),
        pytest.param("pr.json", "headRepository", None, id="missing-head-repository"),
    ],
)
def test_remediation_fallback_requires_proof_and_repository_identity(
    remediation_pr: Path, artifact: str, field: str, value: object
) -> None:
    """Reject fallback receipts the next branch-preparation consumer cannot accept."""
    _set_receipt(
        remediation_pr,
        method="git-original-branch-fallback",
        command=f"git checkout --no-guess {HEAD_BRANCH}",
        local_branch=HEAD_BRANCH,
    )
    path = remediation_pr / artifact
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit, match="^code-remediate-pr-routing-checkout-command-invalid$"):
        _module(PLUGIN_ROOT / "shared" / "validate-artifacts.py")._validate_code_remediate_pr_source(
            remediation_pr,
            json.loads((remediation_pr / "pr-routing.json").read_text(encoding="utf-8")),
            json.loads((remediation_pr / "target-branch.json").read_text(encoding="utf-8")),
            json.loads((remediation_pr / "local-checkout.json").read_text(encoding="utf-8")),
        )


def _detached_modern(pr_dir: Path) -> None:
    """Write a review-only detached method falsely labeled as remediation."""
    head = json.loads((pr_dir / "pr-routing.json").read_text(encoding="utf-8"))["head_oid"]
    _set_receipt(
        pr_dir,
        method="git-detached-review-fallback",
        command=f"git checkout --detach {head}",
        local_branch=HEAD_BRANCH,
    )


def _native_empty_branch(pr_dir: Path) -> None:
    """Write a native checkout receipt without its required attached local branch."""
    _set_receipt(
        pr_dir,
        method="gh-pr-checkout",
        command="gh pr checkout https://github.com/acme/widgets/pull/123",
        local_branch="",
    )


def _wrong_branch(pr_dir: Path) -> None:
    """Write an attached fallback receipt for a branch other than the PR head branch."""
    _set_receipt(
        pr_dir,
        method="git-original-branch-fallback",
        command=f"git checkout --no-guess {HEAD_BRANCH}",
        local_branch="other-branch",
    )


def _wrong_command(pr_dir: Path) -> None:
    """Write a fallback command that does not select the original PR branch."""
    _set_receipt(
        pr_dir,
        method="git-original-branch-fallback",
        command="git checkout --no-guess other-branch",
        local_branch=HEAD_BRANCH,
    )


def _cross_repository_fallback(pr_dir: Path) -> None:
    """Write an otherwise valid fallback receipt for a fork PR."""
    _set_receipt(
        pr_dir,
        method="git-original-branch-fallback",
        command=f"git checkout --no-guess {HEAD_BRANCH}",
        local_branch=HEAD_BRANCH,
        same_repo=False,
        cross_repository=True,
    )


def _routing_checkout_disagreement(pr_dir: Path) -> None:
    """Write routing proof that disagrees with the checkout command but preserves its method."""
    _set_receipt(
        pr_dir,
        method="gh-pr-checkout",
        command="gh pr checkout https://github.com/acme/widgets/pull/123",
        local_branch=HEAD_BRANCH,
    )
    checkout_path = pr_dir / "local-checkout.json"
    checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
    checkout["command"] = "gh pr checkout https://github.com/acme/widgets/pull/124"
    checkout_path.write_text(json.dumps(checkout), encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_detached_modern, id="detached-modern-receipt"),
        pytest.param(_native_empty_branch, id="native-empty-local-branch"),
        pytest.param(_wrong_branch, id="fallback-wrong-local-branch"),
        pytest.param(_wrong_command, id="fallback-wrong-command"),
        pytest.param(_cross_repository_fallback, id="fallback-cross-repository"),
        pytest.param(_routing_checkout_disagreement, id="routing-checkout-disagreement"),
    ],
)
def test_remediation_receipt_invalid_route_is_rejected(remediation_pr: Path, mutate: Callable[[Path], None]) -> None:
    """Reject receipts that could direct remediation onto an unsafe or contradictory branch."""
    mutate(remediation_pr)

    with pytest.raises(SystemExit, match="code-remediate-pr-routing-checkout-command-invalid"):
        _module(PLUGIN_ROOT / "shared" / "validate-artifacts.py")._validate_code_remediate_pr_source(
            remediation_pr,
            json.loads((remediation_pr / "pr-routing.json").read_text(encoding="utf-8")),
            json.loads((remediation_pr / "target-branch.json").read_text(encoding="utf-8")),
            json.loads((remediation_pr / "local-checkout.json").read_text(encoding="utf-8")),
        )
