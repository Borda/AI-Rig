"""Guard resolve dispatch routing, item caps, and question-count contracts."""

import os
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_RESOLVE = Path(__file__).resolve().parents[2] / "skills/resolve"
_PLUGIN = _RESOLVE.parents[1]
_BASH = shutil.which("bash")


def _bash_path(path: Path) -> str:
    """Return the fixture path in Git Bash syntax on native Windows."""
    if sys.platform != "win32":
        return str(path)
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()


def _step_1_agent_block(skill: str) -> str:
    """Read the real Step 1 parser and sentinel producer Bash block."""
    start = skill.index("export CSID=", skill.index("Parse $ARGUMENTS:"))
    return skill[start : skill.index("echo skip >", start)]


def _step_8_prelude(dispatch: str, selected_ids: list[int]) -> str:
    """Read the real Step 8 consumer block with the selected IDs substituted."""
    start = dispatch.index("```bash", dispatch.index("Substitute the Step 3d selection")) + len("```bash\n")
    block = dispatch[start : dispatch.index("```", start)]
    return block.replace(
        'SELECTED_ITEMS="<space-separated selected ids>"',
        f'SELECTED_ITEMS="{" ".join(map(str, selected_ids))}"',
    )


def _bash_block_after(dispatch: str, heading: str) -> str:
    """Return the executable Bash block after a unique dispatch heading."""
    start = dispatch.index("```bash", dispatch.index(heading)) + len("```bash\n")
    return dispatch[start : dispatch.index("```", start)]


def _bash_block_containing(dispatch: str, marker: str) -> str:
    """Return the executable Bash block whose own body contains a unique marker.

    Unlike `_bash_block_after`, the marker sits inside the fence (a `#`-comment on a compaction boundary), not in the
    prose heading above it.
    """
    pos = dispatch.index(marker)
    start = dispatch.rindex("```bash", 0, pos) + len("```bash\n")
    return dispatch[start : dispatch.index("```", start)]


@pytest.mark.skipif(_BASH is None, reason="Resolve Step 1 and thread intelligence use Bash")
@pytest.mark.parametrize(
    ("agent_flag", "expected_agent"),
    [
        pytest.param("", "route-by-table", id="default-table"),
        pytest.param("--agent sw-engineer", "foundry:sw-engineer", id="bare-agent"),
        pytest.param("--agent foundry:doc-scribe", "foundry:doc-scribe", id="prefixed-agent"),
        pytest.param("--agent bridge:implement", "route-by-table", id="bridge-is-not-classifier"),
    ],
)
def test_step_1_agent_sentinel_reaches_thread_intelligence(
    tmp_path: Path, agent_flag: str, expected_agent: str
) -> None:
    """Thread intelligence must consume the same normalized override as implementation."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    intelligence = (_RESOLVE / "modes/pr-intelligence.md").read_text(encoding="utf-8")
    environment = os.environ | {
        "ARGUMENTS": f"42 {agent_flag}".strip(),
        "CLAUDE_CODE_SESSION_ID": "resolve-intel-test",
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    producer = subprocess.run(
        [_BASH, "-c", _step_1_agent_block(skill)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert producer.returncode == 0, producer.stderr
    assert (tmp_path / "resolve-agent-override-resolve-intel-test").read_text(encoding="utf-8").strip() == (
        "" if not agent_flag else "bridge:implement" if agent_flag == "--agent bridge:implement" else expected_agent
    )

    start = intelligence.index("```bash", intelligence.index("Read the normalized Step 1 agent override")) + len(
        "```bash\n"
    )
    consumer_block = intelligence[start : intelligence.index("```", start)]
    consumer = subprocess.run(
        [_BASH, "-c", consumer_block],
        cwd=tmp_path,
        env=environment | {"ARGUMENTS": "42"},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert consumer.returncode == 0, consumer.stderr
    assert f"INTEL_AGENT={expected_agent}" in consumer.stdout


@pytest.mark.skipif(_BASH is None, reason="Resolve Step 1 and Step 8 use Bash")
@pytest.mark.parametrize(
    ("agent_flag", "expected_agent"),
    [
        pytest.param("", "bridge:implement", id="default-bridge"),
        pytest.param("--agent sw-engineer", "foundry:sw-engineer", id="bare-agent"),
        pytest.param("--agent foundry:doc-scribe", "foundry:doc-scribe", id="prefixed-agent"),
        pytest.param("--agent bridge:implement", "bridge:implement", id="explicit-bridge"),
    ],
)
def test_step_1_agent_sentinel_reaches_step_8_for_nine_medium_items(
    tmp_path: Path, agent_flag: str, expected_agent: str
) -> None:
    """The cleaned argument must not erase the selected agent before medium-item routing."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    environment = os.environ | {
        "ARGUMENTS": f"42 {agent_flag}".strip(),
        "CLAUDE_CODE_SESSION_ID": "resolve-route-test",
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    producer = subprocess.run(
        [_BASH, "-c", _step_1_agent_block(skill) + '\nprintf "CLEAN=%s\\n" "$ARGUMENTS"'],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert producer.returncode == 0, producer.stderr
    assert "CLEAN=42" in producer.stdout
    sentinel = tmp_path / "resolve-agent-override-resolve-route-test"
    assert sentinel.read_text(encoding="utf-8").strip() == ("" if not agent_flag else expected_agent)
    (tmp_path / "resolve-commit-mode-resolve-route-test").write_text("each\n", encoding="utf-8", newline="\n")
    impl_dir = tmp_path / "implementation"
    impl_dir.mkdir()
    (impl_dir / "action-items.jsonl").write_text(
        "".join(json.dumps({"id": item_id}) + "\n" for item_id in range(1, 10)), encoding="utf-8", newline="\n"
    )
    (tmp_path / "resolve-impl-dir-resolve-route-test").write_text(
        _bash_path(impl_dir) + "\n", encoding="utf-8", newline="\n"
    )

    consumer = subprocess.run(
        [_BASH, "-c", _step_8_prelude(dispatch, list(range(1, 10))) + '\nprintf "ROUTE=%s\\n" "$IMPL_AGENT"'],
        cwd=tmp_path,
        env=environment | {"ARGUMENTS": "42"},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert consumer.returncode == 0, consumer.stderr
    assert f"ROUTE={expected_agent}" in consumer.stdout
    assert (tmp_path / "resolve-impl-dir-resolve-route-test").read_text(encoding="utf-8").strip() == _bash_path(
        impl_dir
    )
    assert (impl_dir / "selected-items.txt").read_text(encoding="utf-8").strip().split() == [
        str(item_id) for item_id in range(1, 10)
    ]


@pytest.mark.skipif(_BASH is None, reason="Resolve Step 8 uses Bash")
@pytest.mark.parametrize(
    ("count", "allowed"),
    [pytest.param(20, True, id="hard-cap"), pytest.param(21, False, id="above-hard-cap")],
)
def test_step_8_prelude_enforces_hard_cap_before_item_file(tmp_path: Path, count: int, allowed: bool) -> None:
    """A 21-item selection cannot enter dispatch even if an earlier prompt was skipped."""
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    (tmp_path / "resolve-agent-override-resolve-cap-test").write_text("\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-commit-mode-resolve-cap-test").write_text("each\n", encoding="utf-8", newline="\n")
    impl_dir = tmp_path / "implementation"
    impl_dir.mkdir()
    (impl_dir / "action-items.jsonl").write_text(
        "".join(json.dumps({"id": item_id}) + "\n" for item_id in range(1, count + 1)), encoding="utf-8", newline="\n"
    )
    (tmp_path / "resolve-impl-dir-resolve-cap-test").write_text(
        _bash_path(impl_dir) + "\n", encoding="utf-8", newline="\n"
    )
    environment = os.environ | {
        "ARGUMENTS": "42",
        "CLAUDE_CODE_SESSION_ID": "resolve-cap-test",
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run(
        [_BASH, "-c", _step_8_prelude(dispatch, list(range(1, count + 1)))],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is allowed
    if not allowed:
        assert b"! BLOCKED" in result.stdout
        assert b"selected action items exceed the 20-item hard cap" in result.stdout
        assert (tmp_path / "resolve-impl-dir-resolve-cap-test").read_text(encoding="utf-8").strip() == _bash_path(
            impl_dir
        )
        assert not (impl_dir / "selected-items.txt").exists()


def test_over20_choice_is_bounded_before_task_creation() -> None:
    """A bulk selection above the cap needs an explicit scope decision before tasks exist."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    gate = skill[skill.index("**Over-20 selection gate**") : skill.index("## Step 3e")]
    assert "AskUserQuestion" in gate
    assert "first 20 selected items" in gate
    assert "rerun for the remaining items" in gate
    assert "stop without creating tasks" in gate
    assert skill.index("**Over-20 selection gate**") < skill.index("## Step 7b join")
    assert "proceed with all" not in gate
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    cap = dispatch[dispatch.index("**Caps**") : dispatch.index("**Parallel specialist-worktree dispatch**")]
    assert "AskUserQuestion" not in cap
    assert "Spawn wave cap" in cap


def test_explicit_agent_bypasses_c1_and_reaches_specialist_dispatch() -> None:
    """Real Agent overrides bypass C1 while the bridge Skill remains a routing marker."""
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    prelude = dispatch[: dispatch.index("**Concurrency guard")]
    c1 = dispatch[dispatch.index("**C1 —") : dispatch.index("**Only `each` mode commits here.")]
    phase2 = dispatch[dispatch.index("Group `SURVIVING_ITEMS`") : dispatch.index("**File-ownership tiebreak")]

    assert 'IFS= read -r _AGENT_OVERRIDE < "$_AGENT_FILE"' in prelude
    assert 'IMPL_AGENT="${_AGENT_OVERRIDE:-bridge:implement}"' in prelude
    assert "`IMPL_AGENT=bridge:implement` (default or explicit)" in c1
    assert "An explicit real Agent type bypasses C1" in c1
    assert "Phase 1+2" in c1
    assert "use the `change` table when `IMPL_AGENT=bridge:implement` (default or explicit)" in phase2
    assert "A group resolved to `bridge:implement` is a routing error: block dispatch" in phase2
    assert 'Skill(skill="bridge:implement"' in c1


def test_question_maximum_counts_step_3d_calls_plus_push_confirmation() -> None:
    """The published longest normal path is three Step 3d calls plus the Step 10 push confirmation.

    Labels and the post-PR action moved to the Step 3d follow-up call. Push authorization cannot move: it must show the
    diff stat, which exists only after implementation, so Step 10 still confirms it.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    question_contract = skill[skill.index("- **AskUserQuestion usage**:") :]

    assert "| 10-18 |" in skill
    assert (
        "the normal action-item path, after successful source resolution and without diagnostic or conflict recovery, takes at most 3 calls at Step 3d"
        in question_contract
    )
    assert "plus 1 Step 10 push confirmation unless the push intent was an explicit" in question_contract
    assert "**`GROUP_STRATEGY=labels` only** — file missing or empty" in dispatch


@pytest.mark.skipif(_BASH is None, reason="Resolve Step 8 uses Bash")
@pytest.mark.parametrize(
    ("selected_ids", "case_name"),
    [
        pytest.param(list(range(1, 11)), "first-ten", id="first-ten"),
        pytest.param([2, 4, 6], "required-only", id="required-only"),
    ],
)
def test_all_mode_closeout_requires_item_work_record(tmp_path: Path, selected_ids: list[int], case_name: str) -> None:
    """A bulk commit closes selected work, not stalled or deferred item tasks."""
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    impl_dir = tmp_path / case_name
    impl_dir.mkdir()
    (tmp_path / "resolve-impl-dir-resolve-closeout-test").write_text(
        f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n"
    )
    (impl_dir / "selected-items.txt").write_text(
        " ".join(map(str, selected_ids)) + "\n", encoding="utf-8", newline="\n"
    )
    (impl_dir / "item-tasks.tsv").write_text(
        "".join(f"{item_id}\ttask-{item_id}\n" for item_id in range(1, 13)), encoding="utf-8", newline="\n"
    )
    (impl_dir / "skipped-items.txt").write_text("", encoding="utf-8", newline="\n")
    (impl_dir / "challenge-log.txt").write_text("", encoding="utf-8", newline="\n")
    (impl_dir / "merge-result.json").write_text(
        '{"applied": [1], "conflict": null, "remaining": []}\n', encoding="utf-8", newline="\n"
    )
    (impl_dir / "phase2-commits.jsonl").write_text(
        json.dumps({"item_id": selected_ids[0], "sha": "abc1234", "group": "one"}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (impl_dir / "c1-item-files.tsv").write_text(f"{selected_ids[1]}\ta.py\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [_BASH, "-c", _bash_block_after(dispatch, "After the commit succeeds, flip ")],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": "resolve-closeout-test", "TMPDIR": str(tmp_path)},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert [line for line in result.stdout.splitlines() if line.startswith("TaskUpdate target:")] == [
        f"TaskUpdate target: item={item_id} task=task-{item_id}" for item_id in selected_ids[:2]
    ]


@pytest.mark.skipif(_BASH is None or shutil.which("jq") is None, reason="C1 fence uses Bash and jq")
@pytest.mark.parametrize(
    ("actual_shared", "claimed_shared", "remaining", "blockers", "expected_error"),
    [
        pytest.param(True, False, [], [], "C1 files_touched differs from Git paths", id="unreported-companion"),
        pytest.param(False, True, [], [], "C1 files_touched differs from Git paths", id="falsely-reported-companion"),
        pytest.param(False, False, ["run tests"], [], "C1 cannot mark remaining work DONE", id="remaining-work"),
        pytest.param(False, False, [], ["test failure"], "C1 cannot mark remaining work DONE", id="blocker"),
        pytest.param(False, False, [], [], "", id="exact-git-paths"),
    ],
)
def test_c1_rejects_unattributed_or_incomplete_done_work(
    tmp_path: Path,
    actual_shared: bool,
    claimed_shared: bool,
    remaining: list[str],
    blockers: list[str],
    expected_error: str,
) -> None:
    """A bridge reply closes only fully resolved work with exact Git path attribution."""
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    assert "one item per C1 call" in dispatch
    repo = tmp_path / "repo"
    repo.mkdir()
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    (tmp_path / "resolve-impl-dir-resolve-c1-test").write_text(
        f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "resolve-commit-mode-resolve-c1-test").write_text("stage\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-pr-number-resolve-c1-test").write_text("42\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-pr-ref-resolve-c1-test").write_text("#42\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Resolve Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "resolve@example.invalid"], cwd=repo, check=True)
    (repo / "a.py").write_text("old\n", encoding="utf-8", newline="\n")
    (repo / "shared.py").write_text("old\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "a.py", "shared.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    (impl_dir / "c1-head-1.txt").write_text(
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True), encoding="utf-8", newline="\n"
    )
    (repo / "a.py").write_text("new\n", encoding="utf-8", newline="\n")
    if actual_shared:
        (repo / "shared.py").write_text("new\n", encoding="utf-8", newline="\n")
    (impl_dir / "action-items.jsonl").write_text(
        json.dumps({"id": 1, "file": "a.py", "author": "reviewer", "full_comment_text": "fix"}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (impl_dir / "c1-reply-1.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "verdict": "DONE",
                "findings": ["first"],
                "files_touched": ["a.py", "shared.py"] if claimed_shared else ["a.py"],
                "remaining": remaining,
                "blockers": blockers,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    block = _bash_block_after(dispatch, "**SECURITY — every field below comes from")
    block = block.replace('"<this batch\'s first item id — same value used for the brief file above>"', '"1"')
    nested = repo / "nested"
    nested.mkdir()
    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=nested if not actual_shared and not claimed_shared else repo,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": "resolve-c1-test", "TMPDIR": str(tmp_path)},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if expected_error:
        assert result.returncode != 0
        assert expected_error in result.stderr
        assert not (impl_dir / "c1-item-files.tsv").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (impl_dir / "c1-item-files.tsv").read_text(encoding="utf-8").splitlines() == ["1\ta.py"]


def test_c1_reply_contract_uses_bridge_public_object() -> None:
    """The resolve brief and parser must accept the bridge's validated object shape."""
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    schema = json.loads(
        (_PLUGIN.parent / "bridge_cc-codex/schemas/harness-envelope.schema.json").read_text(encoding="utf-8")
    )
    c1 = dispatch[dispatch.index("**C1 —") : dispatch.index("**Only `each` mode commits here.")]
    assert schema["type"] == "object"
    assert schema["properties"]["status"]["type"] == "string"
    assert '"Return your result in the bridge object fields:' in c1
    assert '"status=complete, verdict=DONE or UNCERTAIN' in c1
    assert 'item.get("files_touched")' in dispatch


@pytest.mark.skipif(_BASH is None or shutil.which("jq") is None, reason="C1 preflight uses Bash and jq")
@pytest.mark.parametrize("dirty", [False, True])
def test_c1_brief_requires_clean_git_state(tmp_path: Path, dirty: bool) -> None:
    """A second unstaged item cannot be attributed using the first item's dirty worktree."""
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Resolve Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "resolve@example.invalid"], cwd=repo, check=True)
    (repo / "a.py").write_text("old\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    if dirty:
        (repo / "a.py").write_text("new\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-impl-dir-resolve-c1-brief").write_text(
        f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n"
    )
    (impl_dir / "action-items.jsonl").write_text(
        json.dumps({"id": 1, "file": "a.py", "line": 1, "full_comment_text": "fix"}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    block = _bash_block_after(dispatch, "**SECURITY — never type a review comment")
    block = block.replace('"<this batch\'s first item id>"', '"1"')
    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=repo,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": "resolve-c1-brief", "TMPDIR": str(tmp_path)},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert (result.returncode == 0) is not dirty
    assert (impl_dir / "c1-brief-1.md").exists() is not dirty


@pytest.mark.skipif(_BASH is None, reason="Phase1/Phase2 contract refresh uses Bash")
def test_phase1_to_phase2_boundary_refreshes_contract(tmp_path: Path) -> None:
    """A compaction between Phase 1 and Phase 2 must resume mid-dispatch, not at Step 3d.

    Regression for the incident where the only refresh before this one was Step 3d's item-selection gate, and the next
    was after the whole Phase 2 implementation loop — a compaction anywhere across Phase 1's challenge agents or Phase
    2's worktree-held implementation re-asked an already-answered item-selection gate on resume.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    block = _bash_block_containing(dispatch, "boundary1: Phase 1 challenge done")

    session = "resolve-boundary1-test"
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    (tmp_path / f"resolve-pr-number-{session}").write_text("1542\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-impl-dir-{session}").write_text(f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ
        | {
            "CLAUDE_CODE_SESSION_ID": session,
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
            "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    contract = (tmp_path / ".temp/state/skill-contract.md").read_text(encoding="utf-8")
    assert "- skill: oss:resolve · phase: Phase 2 dispatch (after Phase 1 challenge verdicts)" in contract
    assert "pr=1542" in contract
    assert f"impl-dir={_bash_path(impl_dir)}" in contract
    assert "challenge-log.txt" in contract
    assert "item-tasks.tsv" in contract
    assert "never re-issue Step 3d" in contract


def test_dispatch_granularity_question_offers_every_width_and_a_group_preview() -> None:
    """Step 3d must let the user set Phase 2 wave width without weakening any grouping guard.

    The four widths are a closed set, so each one needs its own literal Bash block: a single block carrying a default
    beside a substitution comment would silently record that default on every run.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")

    assert "Dispatch-granularity question — multiSelect: FALSE" in skill
    for mode in ("auto", "sequential", "per-specialist", "preview"):
        assert f'echo {mode} > "${{TMPDIR:-/tmp}}/resolve-dispatch-mode-${{CSID}}"' in skill, mode
    assert 'echo auto > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000' in skill
    assert "| ≤3 | Q1 items · Q2 bulk · Q3 commit-mode · Q4 dispatch |" in skill
    assert (
        "| 4-6 | Q1-Q2 items (≤3 each) · Q3 bulk | Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |" in skill
    )
    assert "Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |" in skill
    assert "Q4 dispatch (all four slots; no item checkboxes exist in this mode)" in skill
    assert "discard the commit-mode, topic-group, **and dispatch** answers from the same call" in skill
    assert 'echo "commit-mode=$_CM group-strategy=$_GS dispatch-mode=$_DM push=$_PA post-pr=$_PP"' in skill
    assert "`DISPATCH_MODE=sequential` narrows every wave to **one** group regardless of pool" in dispatch
    assert "`per-specialist` skips only the ≤5 split" in dispatch
    assert "**Group-preview gate — `DISPATCH_MODE=preview` only" in dispatch
    assert "so the sentinel holds a width, never `preview`" in dispatch


@pytest.mark.parametrize("band", ["≤3", "4-6", "7-9", "10-18"])
def test_dispatch_question_shares_a_call_with_commit_mode(band: str) -> None:
    """Each Step 3d slot-table band asks how to parallelize in the same call that asks how to commit."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    row = next(line for line in skill.splitlines() if line.startswith(f"| {band} |"))
    calls = [cell for cell in row.strip("|").split("|")[1:] if "commit-mode" in cell]
    assert len(calls) == 1, row
    assert "dispatch" in calls[0], row


def test_context_budget_mode_asks_commit_mode_and_dispatch_together() -> None:
    """The ≥19-item single call carries commit mode and dispatch granularity side by side."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    assert "Q1 bulk action · Q2 commit-mode · Q3 topic-group · Q4 dispatch" in skill


#: Post-Step-3d `AskUserQuestion` mentions that are error recovery, a user-elected gate, or an explicit "no ask" note.
_POST_SELECTION_ASK_ALLOWLIST = (
    "No confirming commit found",  # straggler gate: unresolved item status
    "**Push confirmation — one `AskUserQuestion` call.**",  # Step 10: scope-bearing push confirmation
    "Assign a topic label to each implemented item",  # Step 8: typed-labels file lost (recovery)
    "no new `AskUserQuestion` here",  # Step 11 states it reads the stored answer
    "Phase 2 groups are formed",  # group preview, elected at Step 3d via Custom dispatch
    "Challenge double-timeout gate",  # Phase 1: a retry also timed out; user decides, batched per wave
)


def _post_selection_text() -> str:
    """Join every instruction that executes after the Step 3d gate on the action-item path."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    conflicts = (_RESOLVE / "modes/conflict-resolution.md").read_text(encoding="utf-8")
    return "\n".join(
        (
            skill[skill.index("## Step 7b join") : skill.index("## Step 12")],
            conflicts[conflicts.index("### 7b: Verify and complete merge") :],
            (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8"),
            (_RESOLVE / "modes/lint-qa-gate.md").read_text(encoding="utf-8"),
        )
    )


def test_post_selection_steps_ask_only_for_recovery_or_elected_preview() -> None:
    """Steps 7b–11 must not re-ask anything Step 3d can collect up front.

    After the Step 3d answer the user leaves; any predictable question later (push, labels, over-20) parked real runs
    for tens of minutes. Only error recovery and the group preview the user chose at Step 3d may still ask.
    """
    unexpected = [
        line
        for line in _post_selection_text().splitlines()
        if "AskUserQuestion" in line and not any(marker in line for marker in _POST_SELECTION_ASK_ALLOWLIST)
    ]
    assert unexpected == []


def test_push_question_lives_in_step_3d_with_fixed_blocks() -> None:
    """Push and post-PR answers are recorded at Step 3d by one fixed block per closed-set value.

    A single block carrying a default would record that default unedited on every run, so each value owns its own
    literal block, and Step 10 must hold none of them.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    selection = skill[skill.index("## Step 3d") : skill.index("## Step 7b join")]
    push_step = skill[skill.index("## Step 10: Push") : skill.index("## Step 11")]
    blocks = [
        'echo push > "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}"',
        'echo skip > "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}"',
        'echo open > "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}"',
        'echo skip > "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}"',
    ]
    assert [block in selection for block in blocks] == [True] * 4
    assert [block in push_step for block in blocks] == [False] * 4
    assert "Push question — multiSelect: FALSE" in selection
    assert "# substitute" not in selection
    assert "**Over-20 selection gate** — rides the ≥19 band's follow-up call" in selection


@pytest.mark.skipif(_BASH is None, reason="The Step 10 push read-back is Bash")
@pytest.mark.parametrize(
    ("sentinel", "expected"),
    [
        pytest.param("push", "push", id="authorized"),
        pytest.param("skip", "skip", id="declined"),
        pytest.param(None, "unset", id="lost-answer"),
    ],
)
def test_step_10_reads_recorded_push_answer(tmp_path: Path, sentinel: str | None, expected: str) -> None:
    """Step 10 reads the stored Step 3d push intent in the same call that computes the push scope.

    A missing file reads as `unset`, never as `skip`, and routes to the full push confirmation (push + post-PR). With no
    fork remote recorded the scope step refuses (`⛔`, exit 1), which Step 10 records as `not-attempted` and never
    pushes.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    block = _bash_block_after(skill, "**Push intent from Step 3d, confirmation here")
    session = f"resolve-push-{expected}"
    if sentinel is not None:
        (tmp_path / f"resolve-push-auth-{session}").write_text(f"{sentinel}\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ
        | {
            "CLAUDE_CODE_SESSION_ID": session,
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
            "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines()[0] == f"PUSH_AUTH={expected}"
    assert "⛔ Step 10: FORK_REMOTE/HEAD_REF unresolved" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="Resolve Step 1 uses Bash")
def test_step_1_resets_push_answer_to_unset(tmp_path: Path) -> None:
    """A new run must not inherit the previous run's `push` answer from the same session."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    start = skill.index("export CSID=", skill.index("Parse $ARGUMENTS:"))
    block = skill[start : skill.index("# defence-in-depth", start)]
    session = "resolve-push-reset"
    (tmp_path / f"resolve-push-auth-{session}").write_text("push\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ
        | {
            "ARGUMENTS": "42",
            "CLAUDE_CODE_SESSION_ID": session,
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
            "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / f"resolve-push-auth-{session}").read_text(encoding="utf-8").strip() == "unset"
    assert (tmp_path / f"resolve-push-status-{session}").read_text(encoding="utf-8").strip() == "none"


@pytest.mark.parametrize(
    "status", ["pushed", "blocked-guard", "blocked-permission", "rejected-non-ff", "skipped-by-user", "not-attempted"]
)
def test_step_10_records_each_push_status_with_its_own_block(status: str) -> None:
    """Every push outcome is a closed-set value, so each needs a literal block the report can trust.

    A shared block with a default would silently report that default whenever the orchestrator forgot to edit it.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    push_step = skill[skill.index("## Step 10: Push") : skill.index("## Step 11")]
    assert f'echo {status} > "${{TMPDIR:-/tmp}}/resolve-push-status-${{CSID}}"' in push_step


def test_blocked_push_keeps_guard_lines_verbatim_and_never_bypasses_the_guard() -> None:
    """A guard-blocked push saves the guard's own lines and the report ends with them.

    The guard and the missing allow rule are user safety controls; the skill may only relay their instructions.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    push_step = skill[skill.index("## Step 10: Push") : skill.index("## Step 11")]
    report = skill[skill.index("## Step 11") : skill.index("## Step 12")]
    template = (_RESOLVE / "templates/resolve-report.md").read_text(encoding="utf-8")
    assert "never create, touch, or edit a guard's authorization file yourself" in push_step
    assert "`$IMPL_DIR/push-unblock.txt`" in push_step
    assert "character for character" in push_step
    assert "`## Unblock push` section that repeats its lines verbatim" in report
    assert template.rstrip().splitlines()[-3] == "## Unblock push"


@pytest.mark.skipif(_BASH is None, reason="The Step 11 report preamble is Bash")
def test_step_11_surfaces_push_status_and_unblock_file(tmp_path: Path) -> None:
    """The final report reads the recorded status and the saved unblock lines, not the transcript."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    block = _bash_block_containing(skill, "boundary3: pre-final-report write")
    session = "resolve-report-push"
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    (impl_dir / "push-unblock.txt").write_text("! touch /x\ngit push\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-impl-dir-{session}").write_text(f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-push-status-{session}").write_text("blocked-guard\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-oss-resolve-{session}").write_text(
        f"{_bash_path(_RESOLVE)}\n", encoding="utf-8", newline="\n"
    )

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ
        | {
            "CLAUDE_CODE_SESSION_ID": session,
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
            "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PUSH_STATUS=blocked-guard" in result.stdout
    assert f"PUSH_UNBLOCK={_bash_path(impl_dir)}/push-unblock.txt\n! touch /x\ngit push\n" in result.stdout
    assert "push-status=blocked-guard" in (tmp_path / ".temp/state/skill-contract.md").read_text(encoding="utf-8")


@pytest.mark.skipif(_BASH is None, reason="The Step 8 prelude is Bash")
@pytest.mark.parametrize(
    ("sentinel", "expected"),
    [
        pytest.param("auto", "auto", id="auto"),
        pytest.param("sequential", "sequential", id="sequential"),
        pytest.param("per-specialist", "per-specialist", id="per-specialist"),
        pytest.param("preview", "preview", id="preview"),
        pytest.param("grouped", "auto", id="foreign-value-falls-back"),
        pytest.param(None, "auto", id="missing-sentinel-falls-back"),
    ],
)
def test_step_8_prelude_publishes_a_usable_dispatch_mode(tmp_path: Path, sentinel: str | None, expected: str) -> None:
    """Phase 2 must always receive one of the four widths, whatever Step 3d left behind.

    The width is a cost knob rather than a safety gate, unlike the commit mode validated beside it: a value this gate
    never writes — a stale sentinel from another question, or no file at all — degrades to the current pool-capped
    behaviour instead of blocking the dispatch.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    session = f"resolve-width-{expected}-{sentinel or 'absent'}"
    impl_dir = tmp_path / "implementation"
    impl_dir.mkdir()
    (impl_dir / "action-items.jsonl").write_text(json.dumps({"id": 1}) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-impl-dir-{session}").write_text(f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-agent-override-{session}").write_text("\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-commit-mode-{session}").write_text("each\n", encoding="utf-8", newline="\n")
    if sentinel is not None:
        (tmp_path / f"resolve-dispatch-mode-{session}").write_text(f"{sentinel}\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_BASH, "-c", _step_8_prelude(dispatch, [1])],
        cwd=tmp_path,
        env=os.environ
        | {
            "ARGUMENTS": "42",
            "CLAUDE_CODE_SESSION_ID": session,
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
            "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"DISPATCH_MODE={expected}" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="The Phase 2 boundary block is Bash")
@pytest.mark.parametrize(
    ("sentinel", "expected"),
    [
        pytest.param("per-specialist", "per-specialist", id="chosen-width"),
        pytest.param("grouped", "auto", id="foreign-value-falls-back"),
    ],
)
def test_phase2_boundary_records_dispatch_mode_in_contract(tmp_path: Path, sentinel: str, expected: str) -> None:
    """A compaction inside Phase 2 must resume with the width the user chose, not the default.

    Phase 2 holds worktrees open for minutes, so the contract written at this boundary is the only record of the width
    once the sentinel's own Bash call is gone.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    block = _bash_block_containing(dispatch, "boundary1: Phase 1 challenge done")

    session = f"resolve-boundary-width-{expected}"
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    (tmp_path / f"resolve-impl-dir-{session}").write_text(f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-dispatch-mode-{session}").write_text(f"{sentinel}\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-push-auth-{session}").write_text("push\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ
        | {
            "CLAUDE_CODE_SESSION_ID": session,
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
            "TMPDIR": str(tmp_path),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"DISPATCH_MODE={expected}" in result.stdout
    contract = (tmp_path / ".temp/state/skill-contract.md").read_text(encoding="utf-8")
    assert f"dispatch-mode={expected}" in contract
    assert "push-auth=push (Step 3d answer)" in contract


def test_deprecation_filter_reads_tagged_blob_and_keeps_uncertain_history(tmp_path: Path) -> None:
    """A tag's commit patch is not its file content, and absent history cannot prove unreleased API."""
    intelligence = (_RESOLVE / "modes/pr-intelligence.md").read_text(encoding="utf-8")
    assert 'git show "${LATEST_TAG}:${file_path}"' in intelligence
    assert "path missing" in intelligence
    assert "keep original classification" in intelligence
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Resolve Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "resolve@example.invalid"], cwd=tmp_path, check=True)
    (tmp_path / "api.py").write_text("def released_name():\n    pass\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "release"], cwd=tmp_path, check=True)
    subprocess.run(["git", "tag", "v1"], cwd=tmp_path, check=True)
    (tmp_path / "api.py").write_text("def current_name():\n    pass\n", encoding="utf-8", newline="\n")
    tagged = subprocess.run(["git", "show", "v1:api.py"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "released_name" in tagged.stdout
    assert "current_name" not in tagged.stdout
