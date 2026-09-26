"""Guard resolve dispatch routing, item caps, and question-count contracts."""

import os
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_RESOLVE = Path(__file__).resolve().parents[1] / "skills/resolve"
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
        text=True,
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
        text=True,
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
        text=True,
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
        text=True,
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
    assert skill.index("**Over-20 selection gate**") < skill.index(
        'TaskUpdate(task_id=TASK_SELECT, status="completed")'
    )
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


def test_question_maximum_includes_dispatch_cap_and_group_labels() -> None:
    """The published longest path must count both Step 8 prompts."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    question_contract = skill[skill.index("- **AskUserQuestion usage**:") :]

    assert "| 10-18 |" in skill
    assert "**`GROUP_STRATEGY=labels` only**" in dispatch
    assert (
        "the normal action-item path, after successful source resolution and without diagnostic or conflict recovery, takes at most 5 calls"
        in question_contract
    )
    assert "10-18 pending: two checkbox pages + commit-mode follow-up" in question_contract
    assert "+ labels question + push-auth/post-pr" in question_contract
    assert "4 calls without the optional grouped-labels question" in question_contract


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
        text=True,
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
        text=True,
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
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is not dirty
    assert (impl_dir / "c1-brief-1.md").exists() is not dirty


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
