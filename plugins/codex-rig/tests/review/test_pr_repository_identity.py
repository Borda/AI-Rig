"""Exercise selector-to-validator repository identity across both workflow consumers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_code_remediate_final_outcome_validation import VALIDATOR as REMEDIATION
from test_review_finding_identity import _load_validator

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
REVIEW = _load_validator()


@pytest.fixture
def identity_receipts(tmp_path: Path) -> tuple[dict, dict, dict, dict]:
    """Feed the real selector's output to collector-shaped routing and checkout receipts."""
    url = "https://github.com/Example/Widgets/pull/7"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/example/widgets.git"],
        check=True,
    )
    selected = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/select-git-remote.py"),
            "--expected-url",
            url,
            "--cwd",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    remote = json.loads(selected.stdout)
    assert remote["expected"] == {"host": "github.com", "repository": "example/widgets"}
    routing = {
        "base_identity_source": "pr_url",
        "pr_state": "OPEN",
        "base_host": "github.com",
        "base_repo": "Example/Widgets",
        "local_checkout_required": True,
        "local_checkout_command": f"gh pr checkout {url}",
        "force_policy": "forbidden",
        "pr_url": url,
        "base_oid": "a" * 40,
        "head_oid": "b" * 40,
    }
    target = {
        "remote": remote["remote"],
        "remote_url": remote["remote_url"],
        "expected_base_oid": routing["base_oid"],
        "local_head": routing["base_oid"],
        "expected_base_is_ancestor": True,
        "base_matches_pr_metadata": True,
        "base_relation": "matches-pr-metadata",
    }
    checkout = {
        "pr_url": url,
        "expected_head": routing["head_oid"],
        "local_head": routing["head_oid"],
        "diff_source": "verified-local-checkout",
        "diff_base_oid": routing["base_oid"],
        "diff_head_oid": routing["head_oid"],
        "diff_command": f"git diff --binary {routing['base_oid']}...{routing['head_oid']} --",
    }
    return routing, remote, target, checkout


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("route", ["review", "remediation"])
@pytest.mark.parametrize("host", ["github.com", "GitHub.COM"])
def test_selector_identity_accepts_case_variation(identity_receipts: tuple, route: str, host: str) -> None:
    """Casing preserved by the collector must not invalidate normalized selector identity."""
    routing, remote, target, checkout = identity_receipts
    routing["base_host"] = host
    before = json.dumps(identity_receipts, sort_keys=True)
    if route == "review":
        REVIEW._validate_pr_routing_identity(routing, remote)
    else:
        REMEDIATION._validate_code_remediate_pr_identity(routing, remote, target, checkout)
    assert json.dumps(identity_receipts, sort_keys=True) == before


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("route", ["review", "remediation"])
@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        pytest.param("base_repo", "different/widgets", "repository", id="different-owner"),
        pytest.param("base_repo", "example/different", "repository", id="different-repository"),
        pytest.param("base_repo", None, "repository", id="missing-repository"),
        pytest.param("base_repo", "", "repository", id="empty-repository"),
        pytest.param("base_repo", {}, "repository", id="malformed-repository"),
        pytest.param("base_host", "example.org", "host", id="different-host"),
        pytest.param("base_host", None, "host", id="missing-host"),
        pytest.param("base_host", "", "host", id="empty-host"),
    ],
)
def test_identity_rejects_non_equivalent_values(
    identity_receipts: tuple, route: str, field: str, value, error: str
) -> None:
    """Case normalization cannot erase wrong or malformed identity failures."""
    routing, remote, target, checkout = identity_receipts
    routing["base_repo"] = "example/widgets"
    routing[field] = value
    validator = (
        REVIEW._validate_pr_routing_identity if route == "review" else REMEDIATION._validate_code_remediate_pr_identity
    )
    arguments = (routing, remote) if route == "review" else (routing, remote, target, checkout)
    with pytest.raises(SystemExit, match=f"pr-remote-selection-{error}-mismatch"):
        validator(*arguments)


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("route", ["review", "remediation"])
@pytest.mark.parametrize("field", ["host", "repository"])
@pytest.mark.parametrize("value", [None, "", 17, pytest.param([], id="list"), "   "])
def test_matching_invalid_identity_still_rejects(identity_receipts: tuple, route: str, field: str, value) -> None:
    """Two matching malformed receipts must not constitute a valid repository identity."""
    routing, remote, target, checkout = identity_receipts
    routing["base_repo"] = "example/widgets"
    routing["base_host" if field == "host" else "base_repo"] = value
    remote["expected"][field] = value
    validator = (
        REVIEW._validate_pr_routing_identity if route == "review" else REMEDIATION._validate_code_remediate_pr_identity
    )
    arguments = (routing, remote) if route == "review" else (routing, remote, target, checkout)
    with pytest.raises(SystemExit, match=f"pr-remote-selection-{field}-mismatch"):
        validator(*arguments)
