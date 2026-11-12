"""Schema-v2 remediation checkout receipts: which collector routes may direct remediation onto a PR branch."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import ModuleType

import pytest
from _artifact_files import read_json
from test_collect_pr_git_recovery import _build_repository_template
from test_review_completion_gate import _assessed_pr, _module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HEAD_BRANCH = "widget-fix"
PR_URL = "https://github.com/acme/widgets/pull/123"
OTHER_PR_URL = "https://github.com/acme/widgets/pull/124"
#: The collector's canonical patch receipt, specified independently of its argument builder.
CANONICAL_DIFF_COMMAND = (
    "git -c core.quotePath=true diff --binary {base}...{head} --no-color --no-ext-diff --no-textconv "
    "--src-prefix=a/ --dst-prefix=b/ --unified=3 --inter-hunk-context=0 --diff-algorithm=myers "
    "--no-renames --no-relative --ignore-submodules=none --"
)


@pytest.fixture(scope="module")
def validator() -> ModuleType:
    """Load the shared artifact validator once; its receipt checks keep no state between calls."""
    return _module(PLUGIN_ROOT / "shared" / "validate-artifacts.py")


@pytest.fixture(name="remediation_pr")
def _remediation_pr(tmp_path: Path) -> Path:
    """Create a complete schema-v2 PR receipt ready for remediation routing mutations."""
    fixture_builder = getattr(_assessed_pr, "__wrapped__", None)
    assert callable(fixture_builder)
    return fixture_builder(tmp_path)


def _update_json(path: Path, **fields: object) -> dict:
    """Overwrite top-level fields of one JSON artifact and return the updated payload."""
    payload = read_json(path)
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


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
    _update_json(
        pr_dir / "pr.json",
        headRefName=HEAD_BRANCH,
        isCrossRepository=cross_repository,
        headRepository={"nameWithOwner": "acme/widgets"},
    )
    routing = _update_json(
        pr_dir / "pr-routing.json",
        checkout_method=method,
        checkout_mode=checkout_mode,
        local_checkout_command=command,
        same_repo=same_repo,
    )
    fallback_proof = {}
    if method == "git-original-branch-fallback":
        fallback_proof = {
            "gh_checkout_failure": {
                "command": f"gh pr checkout {routing['pr_url']}",
                "code": "command-failed:local-pr-checkout",
            }
        }
    _update_json(
        pr_dir / "local-checkout.json",
        checkout_method=method,
        checkout_mode=checkout_mode,
        command=command,
        local_branch=local_branch,
        **fallback_proof,
    )


def _validate_receipt(validator: ModuleType, pr_dir: Path) -> None:
    """Run the remediation PR-source check over the receipt artifacts currently on disk."""
    validator._validate_code_remediate_pr_source(
        pr_dir,
        read_json(pr_dir / "pr-routing.json"),
        read_json(pr_dir / "target-branch.json"),
        read_json(pr_dir / "local-checkout.json"),
    )


def _detached_modern(pr_dir: Path) -> None:
    """Write a review-only detached method falsely labeled as remediation."""
    head = read_json(pr_dir / "pr-routing.json")["head_oid"]
    _set_receipt(
        pr_dir, method="git-detached-review-fallback", command=f"git checkout --detach {head}", local_branch=HEAD_BRANCH
    )


def _native_empty_branch(pr_dir: Path) -> None:
    """Write a native checkout receipt without its required attached local branch."""
    _set_receipt(pr_dir, method="gh-pr-checkout", command=f"gh pr checkout {PR_URL}", local_branch="")


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
    _set_receipt(pr_dir, method="gh-pr-checkout", command=f"gh pr checkout {PR_URL}", local_branch=HEAD_BRANCH)
    _update_json(pr_dir / "local-checkout.json", command=f"gh pr checkout {OTHER_PR_URL}")


class TestRemediationReceipt:
    """Validation of the collector's checkout receipt before remediation prepares its branch."""

    @pytest.mark.parametrize("remote_ref", ["FETCH_HEAD", "immutable-head"])
    def test_accepts_verified_head_fetch_receipt(
        self, validator: ModuleType, remediation_pr: Path, remote_ref: str
    ) -> None:
        """Accept historical fetch state and the collector's immutable verified head identity."""
        head = read_json(remediation_pr / "pr-routing.json")["head_oid"]
        _update_json(
            remediation_pr / "pr-head-fetch.json", remote_ref=head if remote_ref == "immutable-head" else remote_ref
        )

        _validate_receipt(validator, remediation_pr)

    @pytest.mark.parametrize(
        "remote_ref",
        [None, "", "f" * 40, "a" * 39, "a" * 41, "A" * 40, "refs/heads/topic", pytest.param([], id="non-string")],
    )
    def test_rejects_unverified_head_fetch_ref(
        self, validator: ModuleType, remediation_pr: Path, remote_ref: object
    ) -> None:
        """Reject malformed references and valid-looking OIDs that do not identify the routed head."""
        _update_json(remediation_pr / "pr-head-fetch.json", remote_ref=remote_ref)

        with pytest.raises(SystemExit, match=r"^code-remediate-pr-source-oid-provenance-invalid$"):
            _validate_receipt(validator, remediation_pr)

    @pytest.mark.parametrize("field", ["local_head", "expected_head_oid"])
    def test_rejects_head_fetch_oid_disagreement(self, validator: ModuleType, remediation_pr: Path, field: str) -> None:
        """An immutable remote identity never bypasses the local and expected head bindings."""
        head = read_json(remediation_pr / "pr-routing.json")["head_oid"]
        _update_json(remediation_pr / "pr-head-fetch.json", remote_ref=head, **{field: "f" * 40})

        with pytest.raises(SystemExit, match=r"^code-remediate-pr-source-oid-provenance-invalid$"):
            _validate_receipt(validator, remediation_pr)

    @pytest.mark.integration
    def test_accepts_receipt_from_real_local_head_fetch(
        self, validator: ModuleType, remediation_pr: Path, tmp_path: Path
    ) -> None:
        """Consume the real collector's local Git fetch receipt without rewriting its produced fields."""
        repository_root = tmp_path / "git-repositories"
        repository_root.mkdir()
        repositories = _build_repository_template(repository_root)
        collector = _module(PLUGIN_ROOT / "shared" / "collect_pr.py")
        head = repositories.head
        run = partial(subprocess.run, cwd=repository_root / "worktree")
        collector._fetch_pr_head_ref(
            run,
            30,
            remediation_pr,
            collector._HeadFetchRequest(
                remote_name=str(repository_root / "source"),
                source_ref="refs/heads/topic",
                head_ref="topic",
                head_oid=head,
                source_policy="verified local PR source",
                error_prefix="pr-head",
            ),
        )
        _update_json(remediation_pr / "pr.json", headRefOid=head)
        _update_json(
            remediation_pr / "pr-routing.json", head_oid=head, local_checkout_command=f"git checkout --detach {head}"
        )
        _update_json(
            remediation_pr / "local-checkout.json",
            command=f"git checkout --detach {head}",
            expected_head=head,
            local_head=head,
            diff_head_oid=head,
        )
        _update_json(remediation_pr / "worktree-preflight.json", current_head=head, expected_head=head)

        assert read_json(remediation_pr / "pr-head-fetch.json")["remote_ref"] == head
        _validate_receipt(validator, remediation_pr)

    @pytest.mark.parametrize(
        "command_template",
        [
            pytest.param(CANONICAL_DIFF_COMMAND, id="canonical-collector"),
            pytest.param("git diff --binary {base}...{head} --", id="legacy-binary"),
            pytest.param(
                "git diff --binary --no-ext-diff --no-textconv {base}...{head} --", id="legacy-transform-disabled"
            ),
        ],
    )
    def test_accepts_exact_collector_diff_receipts(
        self, validator: ModuleType, remediation_pr: Path, command_template: str
    ) -> None:
        """Accept the current collector's safe command and both retained historical receipt formats."""
        routing = read_json(remediation_pr / "pr-routing.json")
        checkout = _update_json(
            remediation_pr / "local-checkout.json",
            diff_command=command_template.format(base=routing["base_oid"], head=routing["head_oid"]),
        )

        validator._validate_code_remediate_pr_identity(
            routing,
            read_json(remediation_pr / "remote-selection.json"),
            read_json(remediation_pr / "target-branch.json"),
            checkout,
        )

    @pytest.mark.parametrize(
        "command_template",
        [
            pytest.param(CANONICAL_DIFF_COMMAND.replace("--no-ext-diff", "--ext-diff"), id="external-transform"),
            pytest.param(CANONICAL_DIFF_COMMAND.replace("{head}", "f" * 40), id="wrong-head"),
            pytest.param(CANONICAL_DIFF_COMMAND + " widget.py", id="filtered-path"),
        ],
    )
    def test_rejects_altered_canonical_diff_receipts(
        self, validator: ModuleType, remediation_pr: Path, command_template: str
    ) -> None:
        """Canonical receipt admission must not accept changed transforms, revisions or source selection."""
        routing = read_json(remediation_pr / "pr-routing.json")
        checkout = _update_json(
            remediation_pr / "local-checkout.json",
            diff_command=command_template.format(base=routing["base_oid"], head=routing["head_oid"]),
        )

        with pytest.raises(SystemExit, match=r"^code-remediate-pr-local-diff-provenance-invalid$"):
            validator._validate_code_remediate_pr_identity(
                routing,
                read_json(remediation_pr / "remote-selection.json"),
                read_json(remediation_pr / "target-branch.json"),
                checkout,
            )

    @pytest.mark.parametrize(
        ("method", "command"),
        [
            pytest.param("gh-pr-checkout", f"gh pr checkout {PR_URL}", id="native-gh-pr-checkout"),
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
    def test_accepts_supported_route(self, validator: ModuleType, remediation_pr: Path, method: str, command: str):
        """Collector-native and verified same-repository attached checkout receipts are accepted.

        Starts from a complete assessed PR artifact and binds each supported checkout route to the PR head branch, so
        the validator must not raise for any route remediation is allowed to start from.
        """
        _set_receipt(remediation_pr, method=method, command=command, local_branch=HEAD_BRANCH)

        _validate_receipt(validator, remediation_pr)

    @pytest.mark.parametrize(
        ("artifact", "field", "value"),
        [
            pytest.param("local-checkout.json", "gh_checkout_failure", None, id="missing-failed-gh-proof"),
            pytest.param("local-checkout.json", "gh_checkout_failure", {}, id="empty-failed-gh-proof"),
            pytest.param(
                "local-checkout.json",
                "gh_checkout_failure",
                {"command": f"gh pr checkout {PR_URL}", "code": ""},
                id="missing-failure-code",
            ),
            pytest.param(
                "local-checkout.json",
                "gh_checkout_failure",
                {"command": f"gh pr checkout {OTHER_PR_URL}", "code": "command-failed"},
                id="failure-for-wrong-pr",
            ),
            pytest.param("pr.json", "headRepository", {"nameWithOwner": "someone/widgets"}, id="forged-same-repo-flag"),
            pytest.param("pr.json", "headRepository", None, id="missing-head-repository"),
        ],
    )
    def test_fallback_requires_proof_and_repository_identity(
        self, validator: ModuleType, remediation_pr: Path, artifact: str, field: str, value: object
    ):
        """A fallback receipt without failed-gh proof or a matching head repository is rejected.

        Starts from a valid same-repository fallback and damages one proof field, so the next branch-preparation
        consumer never receives a fallback it cannot trust.
        """
        _set_receipt(
            remediation_pr,
            method="git-original-branch-fallback",
            command=f"git checkout --no-guess {HEAD_BRANCH}",
            local_branch=HEAD_BRANCH,
        )
        _update_json(remediation_pr / artifact, **{field: value})

        with pytest.raises(SystemExit, match=r"^code-remediate-pr-routing-checkout-command-invalid$"):
            _validate_receipt(validator, remediation_pr)

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
    def test_rejects_invalid_route(self, validator: ModuleType, remediation_pr: Path, mutate: Callable[[Path], None]):
        """Receipts that could direct remediation onto an unsafe or contradictory branch are rejected.

        Each mutator writes one forbidden route (review-only detach, missing or wrong branch, wrong command, fork PR,
        routing/checkout disagreement) over an otherwise valid receipt.
        """
        mutate(remediation_pr)

        with pytest.raises(SystemExit, match="code-remediate-pr-routing-checkout-command-invalid"):
            _validate_receipt(validator, remediation_pr)
