"""Keep routine workflow execution and recoverable command errors under existing authority."""

import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.installed_plugin
def test_local_merge_approval_reaches_remediation_without_waiving_checks() -> None:
    """Keep local merge ownership and full-index review reaching remediation without implying merge consent."""
    native = (PLUGIN_ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
    skill = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    section = native.split("### Local merge approval", 1)[1].split("\n## ", 1)[0]
    for invariant in (
        "need explicit authorization for that exact action",
        "unrelated changes",
        "Explicit denials",
        "remote publication",
        "A merge is task-owned only when the current workflow run recorded starting it before `git merge`",
        "matching identities without that run record, such as a merge the user started, are not task-owned",
        "Review the entire merge index, including clean merged-in paths",
    ):
        assert invariant in section
    assert "A generic remediation request does not authorize local merge commit" in skill
    assert "write `merge-resolution.json` with `status=in-progress`" in skill
    assert "A merge in progress is task-owned only when this remediation run's own `merge-resolution.json`" in skill
    assert "a merge the user started toward the same target" in skill
    assert "automatically finish the task-owned merge" in skill
    assert "Do not merge target merely to refresh conflict-free PR" in skill
    assert "No-commit authorization never implies commit permission" in skill
    assert "required `Co-authored-by: Codex <codex@openai.com>` trailer" in skill


@pytest.mark.installed_plugin
def test_nested_review_exits_and_fallback_limits_reach_remediation() -> None:
    """Keep ineligible nested reviews and parent-fallback reports from looping or passing as independent coverage.

    A nested review can finish yet be unusable (closed, unavailable, superseded), and a dispatch-order failure can yield
    a serial parent-fallback report. Remediation needs a defined exit for the first and a visible residual limit for the
    second, otherwise it either reruns the review or certifies coverage it never received.
    """
    skill = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    handoff = (PLUGIN_ROOT / "shared/final-handoff-contract.md").read_text(encoding="utf-8")
    assert "A review that completes but cannot be admitted keeps its terminal status" in skill
    assert "Never rerun the review or repeat the fresh-review question for this unchanged target" in skill
    assert "`Continue with available findings` (the **No** branch below) and `Stop remediation`" in skill
    assert 'An admitted report recording `execution_mode="serial-fallback"`' in skill
    assert "never as independent review coverage" in skill
    assert "An ineligible returned result follows the remediation's own exit" in handoff


#: Commit message satisfying the shared commit template, reused by every disposable fixture commit.
_FIXTURE_MESSAGE = (
    "test: verify local merge proof\n\nChanges:\n- Create disposable merge evidence.\n\n"
    "Impact:\n- Exercise local merge verification.\n\nVerification:\n- Fixture state checked.\n\n"
    "Residual limits:\n- Disposable test repository only.\n\n---\n"
    "Co-authored-by: Codex <codex@openai.com>"
)
#: Every path the fixture merge changes relative to its first parent, including the clean target-only addition.
_REVIEWED_PATHS = {"shared.txt", "target.txt"}


def _git(repository: Path, *arguments: str, expected: int = 0) -> str:
    """Execute real Git in a disposable repository without inherited hooks or signing."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Workflow test",
            "-c",
            "user.email=workflow@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={repository / 'no-hooks'}",
            *arguments,
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stderr + result.stdout
    return result.stdout.strip()


@pytest.fixture
def reviewed_merge(tmp_path: Path) -> tuple[str, str, str]:
    """Stop a real conflicted merge after resolving, staging and reviewing every merge path.

    Returns the pre-merge HEAD, the merged target OID and the reviewed index tree captured with ``git write-tree``
    before the owning commit command, matching the shared merge commit proof.
    """
    shared = tmp_path / "shared.txt"
    _git(tmp_path, "init", "--initial-branch=topic")
    shared.write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "shared.txt")
    _git(tmp_path, "commit", "-m", _FIXTURE_MESSAGE)
    _git(tmp_path, "switch", "-c", "target")
    shared.write_text("target\n", encoding="utf-8")
    (tmp_path / "target.txt").write_text("clean target addition\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "shared.txt", "target.txt")
    _git(tmp_path, "commit", "-m", _FIXTURE_MESSAGE)
    target = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "switch", "topic")
    shared.write_text("topic\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "shared.txt")
    _git(tmp_path, "commit", "-m", _FIXTURE_MESSAGE)
    pre_merge = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "merge", "--no-commit", "--no-ff", target, expected=1)
    assert _git(tmp_path, "rev-parse", "MERGE_HEAD") == target
    shared.write_text("topic and target\n", encoding="utf-8")
    _git(tmp_path, "add", "--", *sorted(_REVIEWED_PATHS))
    assert _git(tmp_path, "diff", "--name-only", "--diff-filter=U") == ""
    assert set(_git(tmp_path, "diff", "--cached", "--name-only", pre_merge).splitlines()) == _REVIEWED_PATHS
    return pre_merge, target, _git(tmp_path, "write-tree")


@pytest.mark.integration
def test_merge_commit_proof_includes_clean_target_paths(reviewed_merge: tuple[str, str, str], tmp_path: Path) -> None:
    """Prove why merge acceptance needs first-parent paths, exact parents and the staged tree.

    A real conflicted merge also brings in a clean target-only file. Git's default combined diff omits it, so only the
    first-parent path set, the recorded parent order and the reviewed tree prove the whole merge was reviewed.
    """
    pre_merge, target, reviewed_tree = reviewed_merge
    template = (PLUGIN_ROOT / "shared/commit-response-template.md").read_text(encoding="utf-8")
    assert "git diff --no-renames --name-only -z <pre-merge-head> HEAD --" in template
    assert "git rev-list --parents -n 1 HEAD" in template
    assert "git rev-parse HEAD^{tree}" in template
    assert "before the owning command, capture the reviewed index tree with `git write-tree`" in template
    assert "never stage again after capturing the tree" in template
    _git(tmp_path, "commit", "--cleanup=verbatim", "-m", _FIXTURE_MESSAGE)

    assert _git(tmp_path, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [pre_merge, target]
    assert _git(tmp_path, "rev-parse", "HEAD^{tree}") == reviewed_tree
    actual_paths = set(
        _git(tmp_path, "diff", "--no-renames", "--name-only", "-z", pre_merge, "HEAD", "--").rstrip("\0").split("\0")
    )
    assert actual_paths == _REVIEWED_PATHS
    assert "target.txt" not in _git(tmp_path, "show", "--format=", "--name-only", "HEAD").splitlines()
    assert _git(tmp_path, "status", "--porcelain") == ""
    assert _git(tmp_path, "show", "-s", "--format=format:%B", "HEAD") == _FIXTURE_MESSAGE


@pytest.mark.integration
def test_merge_tree_proof_detects_restaged_worktree_drift(reviewed_merge: tuple[str, str, str], tmp_path: Path) -> None:
    """A tree captured at review time exposes content staged after the review.

    The worktree changes after the reviewed tree was captured, and the commit command re-stages it. The path set and
    parents still match, so only the review-time tree comparison catches the unreviewed content.
    """
    pre_merge, target, reviewed_tree = reviewed_merge
    (tmp_path / "shared.txt").write_text("unreviewed drift\n", encoding="utf-8")
    _git(tmp_path, "add", "--", *sorted(_REVIEWED_PATHS))
    _git(tmp_path, "commit", "--cleanup=verbatim", "-m", _FIXTURE_MESSAGE)

    assert _git(tmp_path, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:] == [pre_merge, target]
    assert _git(tmp_path, "rev-parse", "HEAD^{tree}") != reviewed_tree


def test_local_workflow_commands_use_effective_permissions() -> None:
    """Prevent local helpers, tests, and Git writes from inventing new approval requirements."""
    contract = (PLUGIN_ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
    section = contract.split("## Authorized Workflow Execution", 1)[1].split("\n## ", 1)[0]
    for required in (
        "all Codex skills",
        "`use_default`",
        "`git add`",
        "`git commit`",
        "`.git`",
        "packaged helpers",
        "configured checks",
        "current effective permissions",
        "do not infer a restriction from a command being state-changing",
        "does not grant arbitrary unsandboxed shell",
        "existing authorization",
    ):
        assert required in section


@pytest.mark.parametrize("relative", ["shared/commit-response-template.md", "assets/AGENTS.md"])
def test_commit_recovery_keeps_authority_and_checks_state(relative: str) -> None:
    """Require an evidence-bound parse repair without authorizing replay after a real denial."""
    contract = " ".join((PLUGIN_ROOT / relative).read_text(encoding="utf-8").split())
    for required in (
        "pre-execution syntax failure",
        "HEAD",
        "index",
        "existing authorization",
        "denial",
    ):
        assert required in contract
    assert "On denial, failure, or mismatch, do not retry automatically" not in contract


def test_commit_transport_requires_syntax_check_and_bounded_recovery() -> None:
    """Catch quoting mistakes before Git and keep recovery bounded to the original reviewed commit."""
    contract = " ".join((PLUGIN_ROOT / "shared/commit-response-template.md").read_text(encoding="utf-8").split())
    for required in (
        "`shlex.join`",
        "`bash -n`",
        "same reviewed paths and message",
        "one corrected attempt",
        "do not create a duplicate commit",
        "continue independent authorized checks",
    ):
        assert required in contract


def test_required_gate_and_review_do_not_invent_consent_holds() -> None:
    """Keep required checks authorized while retaining real runtime capability boundaries."""
    contract = (PLUGIN_ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
    skill = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    assert "Runnable plugin code blocks are preapproved workflow recipes" in contract
    assert "Existing grants or an applicable preapproval satisfy that capability" in contract
    assert "A timestamp-specific whole-command prefix is not reusable approval" in contract
    assert "`run_gates.py` and its configured checks are required authorized workflow work" in contract
    assert "`+review` selects report intake; it does not forbid" in skill
    assert "Never use `--skip-review` solely because" in skill


def test_git_reads_and_local_configuration_reuse_authorization() -> None:
    """Prevent harmless remote reads and local settings from being mistaken for remote publication."""
    policy = (PLUGIN_ROOT / "assets/AGENTS.md").read_text(encoding="utf-8")
    git = policy.split("- `git` CLI", 1)[1].split("- Never run `git`/`gh`", 1)[0]
    for authorized in (
        "task-scoped local repository operations and read-only remote access",
        "`ls-remote`",
        "`clone`",
        "`fetch`",
        "`remote update`",
        "`submodule update --remote`",
        "local config, upstream, and tracking changes",
        "existing grants or the configured native automatic approval reviewer",
        "only a verified missing capability",
    ):
        assert authorized in git
    assert "no push, clone, remote update, ls-remote" not in git
    assert "it still needs owning command's network approval" not in git
    for boundary in (
        "Never use `git` for remote repository mutation",
        "no push",
        "authorized `git pull --ff-only`",
        "verified upstream and confirmed clean index/worktree",
        "Do not merge, rebase, reset, discard user changes, or manually change tracking configuration merely to refresh",
        "PR-specific verified checkout and recovery protections",
    ):
        assert boundary in git
    assert "Destructive operations retain action-specific consent" in git
    assert "A denial or stricter host restriction" in policy
    assert "Never run `git`/`gh` with `--force`, `--force-with-lease`" in policy
    marketplace = (PLUGIN_ROOT / "skills/sync/SKILL.md").read_text(encoding="utf-8")
    assert "Never use `git clone`, edit marketplace configuration" in marketplace


@pytest.mark.parametrize("relative", ["shared/native-skill-contract.md", "skills/sync/SKILL.md"])
def test_setup_guidance_names_current_generated_default(relative: str) -> None:
    """Prevent callers from expecting the retired generated workspace-only profile."""
    text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
    assert "`local-workflow` when no default exists" in text
    assert "supplying `:workspace` when no default exists" not in text
    assert 'adds `default_permissions = ":workspace"` when no default exists' not in text


@pytest.mark.installed_plugin
def test_upfront_grouping_does_not_trigger_early_commit_failure() -> None:
    """Allow early preferences while retaining the fail-fast for staging before verified gates and a bound plan."""
    skill = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    fail_fast = skill.split("## Fail-fast Rules", 1)[1].split("## Quality Gates", 1)[0]
    assert "commit mode is selected before gates/result validation" not in fail_fast
    assert "must not stage or commit before gates/result validation" in fail_fast
    assert "does not have `<run-directory>/commit-plan.md`" in fail_fast
    assert "stages before recording its bound user choice" in fail_fast


@pytest.mark.installed_plugin
def test_rule_43a_distinguishes_automatic_requested_and_integration_commits() -> None:
    """Keep automatic ordering from overriding explicit commit requests or earlier checked integration."""
    skill = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    rule = skill.split("43a.", 1)[1].split("43b.", 1)[0]
    commit = skill.split("### 12:", 1)[1].split("## Fail-fast Rules", 1)[0]
    template = (PLUGIN_ROOT / "shared/commit-response-template.md").read_text(encoding="utf-8")
    assert "Automatic finding-fix commit units governed by steps 11–12" in rule
    assert "An explicit user request to commit overrides this automatic sequencing restriction" in rule
    assert "Step 03 target integration follows its own merge checks" in rule
    assert "A missing commit plan or unbound scope still fails" in rule
    assert "Do not wait solely for final report validation" in commit
    assert "does not waive required checks, ownership, destination or runtime restrictions" in commit
    assert "record the explicit request and remaining report checkpoint" in template
