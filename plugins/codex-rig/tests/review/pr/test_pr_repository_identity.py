"""Exercise selector-to-validator repository identity across both workflow consumers.

The review validator and the remediation validator both bind a pull request to the repository chosen by the real
`select-git-remote.py` selector. Cases cover the exact diff-command receipt, host and repository case variation, and
rejection of wrong or malformed identities.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

import pytest
from test_code_remediate_final_outcome_validation import VALIDATOR as REMEDIATION
from test_review_finding_identity import _load_validator

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REVIEW = _load_validator()
Receipts = tuple[dict, dict, dict, dict]


def _review_identity(routing: dict, remote: dict, target: dict, checkout: dict) -> None:
    """Validate the PR routing identity through the review validator."""
    REVIEW._validate_pr_routing_identity(routing, remote)


def _remediation_identity(routing: dict, remote: dict, target: dict, checkout: dict) -> None:
    """Validate the PR identity through the remediation validator."""
    REMEDIATION._validate_code_remediate_pr_identity(routing, remote, target, checkout)


def _review_diff(routing: dict, remote: dict, target: dict, checkout: dict) -> None:
    """Validate the diff receipt through the review validator."""
    REVIEW._validate_pr_diff_provenance(checkout, routing)


IDENTITY_ROUTES = pytest.mark.parametrize(
    "validate_identity",
    [pytest.param(_review_identity, id="review"), pytest.param(_remediation_identity, id="remediation")],
)
DIFF_ROUTES = pytest.mark.parametrize(
    "validate_diff", [pytest.param(_review_diff, id="review"), pytest.param(_remediation_identity, id="remediation")]
)


@pytest.fixture(scope="module")
def identity_receipts_template(tmp_path_factory: pytest.TempPathFactory) -> Receipts:
    """Build real selector receipts once per worker; individual cases receive deep copies.

    A real git repository with an `origin` remote is queried through the shipped selector CLI, so the receipts carry
    exactly what the selector reports for a PR URL whose owner and repository differ in case from the remote.
    """
    tmp_path = tmp_path_factory.mktemp("identity-receipts")
    url = "https://github.com/Example/Widgets/pull/7"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/example/widgets.git"], check=True
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


@pytest.fixture
def identity_receipts(identity_receipts_template: Receipts) -> Receipts:
    """Isolate nested receipt mutations while retaining the real selector's baseline."""
    return copy.deepcopy(identity_receipts_template)


@pytest.mark.installed_plugin
def test_identity_receipts_isolate_nested_mutations(identity_receipts: Receipts, identity_receipts_template: Receipts):
    """One parameter case cannot change the shared selector identity used by later cases.

    The per-case receipts are deep copies of the module-scoped template, so mutating nested fields in a copy must leave
    the template untouched.
    """
    identity_receipts[1]["expected"]["repository"] = "different/project"
    identity_receipts[0]["base_repo"] = "Different/Project"

    assert identity_receipts_template[1]["expected"]["repository"] == "example/widgets"
    assert identity_receipts_template[0]["base_repo"] == "Example/Widgets"


class TestDiffReceipt:
    """The recorded diff command identifies the exact base and head, with or without driver protections."""

    @DIFF_ROUTES
    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param("", id="historical-command"),
            pytest.param("--no-ext-diff --no-textconv ", id="current-command"),
        ],
    )
    def test_accepts_historical_and_current_command(
        self, identity_receipts: Receipts, validate_diff: Callable[..., None], flags: str
    ) -> None:
        """Retained historical receipts and the current command both validate.

        The command is rebuilt around the receipt's base and head with either no driver flags (historical) or both
        `--no-ext-diff --no-textconv` flags (current); each must be accepted by the review and remediation validators.
        """
        routing, remote, target, checkout = identity_receipts
        checkout["diff_command"] = f"git diff --binary {flags}{routing['base_oid']}...{routing['head_oid']} --"

        validate_diff(routing, remote, target, checkout)

    @DIFF_ROUTES
    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param("--no-ext-diff ", id="partial-command"),
            pytest.param("--no-textconv ", id="other-partial-command"),
        ],
    )
    def test_rejects_partial_driver_protections(
        self, identity_receipts: Receipts, validate_diff: Callable[..., None], flags: str
    ) -> None:
        """A command carrying only one of the two driver protections is not a valid receipt.

        Admitting either flag alone would let a receipt claim protection it lacks, so only the exact historical or
        current command is accepted.
        """
        routing, remote, target, checkout = identity_receipts
        checkout["diff_command"] = f"git diff --binary {flags}{routing['base_oid']}...{routing['head_oid']} --"

        with pytest.raises(SystemExit, match="diff-provenance-invalid"):
            validate_diff(routing, remote, target, checkout)

    @pytest.mark.parametrize("flags", ["", "--no-ext-diff --no-textconv "])
    def test_detached_review_preserves_worktree_path(self, tmp_path: Path, identity_receipts: Receipts, flags: str):
        """Git option text inside the worktree path must not change the command identity.

        The detached review worktree's directory is literally named `literal diff --binary directory`; the validator has
        to read the command around that path rather than match the option text inside it.
        """
        routing, _remote, _target, checkout = identity_receipts
        routing["checkout_method"] = "git-detached-review-worktree"
        checkout["worktree"] = (tmp_path / "literal diff --binary directory").as_posix()
        checkout["diff_command"] = (
            f"git -C {checkout['worktree']} diff --binary {flags}{routing['base_oid']}...{routing['head_oid']} --"
        )

        REVIEW._validate_pr_diff_provenance(checkout, routing)

    @pytest.mark.installed_plugin
    def test_detached_review_preserves_windows_path(self, identity_receipts: Receipts, monkeypatch: pytest.MonkeyPatch):
        """The recorded cross-host path is compared without changing its separators.

        A receipt written on Windows carries `C:/review/worktree`; the validator, run with Windows path semantics, must
        accept it unchanged on any host.
        """
        routing, _, _, checkout = identity_receipts
        routing["checkout_method"] = "git-detached-review-worktree"
        checkout["worktree"] = "C:/review/worktree"
        checkout["diff_command"] = (
            f"git -C C:/review/worktree diff --binary {routing['base_oid']}...{routing['head_oid']} --"
        )
        monkeypatch.setattr(REVIEW, "Path", PureWindowsPath)

        REVIEW._validate_pr_diff_provenance(checkout, routing)


class TestRepositoryIdentity:
    """Routing and selector identities must agree after normalization, and only then."""

    @pytest.mark.installed_plugin
    @pytest.mark.integration
    @IDENTITY_ROUTES
    @pytest.mark.parametrize("host", ["github.com", "GitHub.COM"])
    def test_accepts_case_variation(
        self, identity_receipts: Receipts, validate_identity: Callable[..., None], host: str
    ) -> None:
        """Casing preserved by the collector does not invalidate the normalized selector identity.

        The routing host is set to an upper- or lower-case spelling while the owner and repository keep their PR-URL
        casing. Validation must pass and leave every receipt unchanged.
        """
        routing, remote, target, checkout = identity_receipts
        routing["base_host"] = host
        before = json.dumps(identity_receipts, sort_keys=True)

        validate_identity(routing, remote, target, checkout)

        assert json.dumps(identity_receipts, sort_keys=True) == before

    @pytest.mark.installed_plugin
    @pytest.mark.integration
    @IDENTITY_ROUTES
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
    def test_rejects_non_equivalent_values(
        self, identity_receipts: Receipts, validate_identity: Callable[..., None], field: str, value, error: str
    ) -> None:
        """Case normalization cannot erase wrong or malformed identity failures.

        One routing field is replaced by a different, missing, empty or malformed value while the selector still reports
        `example/widgets` on `github.com`; the mismatch error must name the affected field.
        """
        routing, remote, target, checkout = identity_receipts
        routing["base_repo"] = "example/widgets"
        routing[field] = value

        with pytest.raises(SystemExit, match=f"pr-remote-selection-{error}-mismatch"):
            validate_identity(routing, remote, target, checkout)

    @pytest.mark.installed_plugin
    @pytest.mark.integration
    @IDENTITY_ROUTES
    @pytest.mark.parametrize("field", ["host", "repository"])
    @pytest.mark.parametrize("value", [None, "", 17, pytest.param([], id="list"), "   "])
    def test_rejects_matching_invalid_values(
        self, identity_receipts: Receipts, validate_identity: Callable[..., None], field: str, value
    ) -> None:
        """Two matching malformed receipts do not constitute a valid repository identity.

        The routing and the selector both carry the same invalid host or repository, so equality alone would pass; the
        validator must still reject it as a mismatch for that field.
        """
        routing, remote, target, checkout = identity_receipts
        routing["base_repo"] = "example/widgets"
        routing["base_host" if field == "host" else "base_repo"] = value
        remote["expected"][field] = value

        with pytest.raises(SystemExit, match=f"pr-remote-selection-{field}-mismatch"):
            validate_identity(routing, remote, target, checkout)
