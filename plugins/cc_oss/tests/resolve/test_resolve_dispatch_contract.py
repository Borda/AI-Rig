"""Guard resolve dispatch routing, item caps, and question-count contracts."""

import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

_RESOLVE = Path(__file__).resolve().parents[2] / "skills/resolve"
_PLUGIN = _RESOLVE.parents[1]
_BASH = shutil.which("bash")
_ZSH = shutil.which("zsh")
#: Shells a fenced resolve block must run under: the Bash tool runs the login shell, zsh on macOS, where a bare
#: ``$VAR`` never word-splits — a loop over it runs once with every id glued together.
_SHELLS = [
    pytest.param(_BASH, id="bash", marks=pytest.mark.skipif(_BASH is None, reason="bash not installed")),
    pytest.param(_ZSH, id="zsh", marks=pytest.mark.skipif(_ZSH is None, reason="zsh not installed")),
]
_skip_no_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="The resolve block reads JSON with jq")


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


@_skip_no_jq
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("count", [21, 60])
def test_step_8_prelude_accepts_selection_above_former_cap(tmp_path: Path, count: int, shell: str) -> None:
    """A selection larger than the former 20-item cap enters dispatch in one pass, under bash and zsh alike.

    Challenge often rejects many items, so a per-pass cap forced needless reruns; load is now bounded per agent, and the
    prelude must not block or trim any selection size. Under zsh a bare ``set -- $SELECTED_ITEMS`` kept the whole list
    as one argument, so every multi-item selection blocked as "selected item 1 2 3 missing".
    """
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
        [shell, "-c", _step_8_prelude(dispatch, list(range(1, count + 1)))],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert b"! BLOCKED" not in result.stdout
    assert (impl_dir / "selected-items.txt").read_text(encoding="utf-8").strip().split() == [
        str(item_id) for item_id in range(1, count + 1)
    ]


def test_selection_gate_never_caps_or_trims_the_selection() -> None:
    """Step 3d passes every selected ID to dispatch and only carries a notice for a very large selection.

    The former over-20 question deferred the remainder to a rerun; its replacement must not ask, trim, or defer. The
    notice rides the >=19 call's question text, a field the call renders while the user still chooses the scope: reply
    text before a tool call may come back as an empty progress update on 5.5-family models, and a task subject is not
    rendered when task tools are disabled and lands only after the user's last answer.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    selection = skill[skill.index("## Step 3d") : skill.index("## Step 7b join")]
    notice = selection[selection.index("**No per-pass item cap**") :]
    bulk_text = skill[skill.index("**Bulk question text**") : skill.index("**ESSENTIAL — exactly these 4 options")]
    assert "More than 20 items were selected" not in skill
    assert "never trim a selection or defer a remainder to a rerun" in notice
    assert "rides in the ≥19 call's Q1 question text (**Bulk question text** line 4, more than 50 pending)" in notice
    assert "never in a task subject" in notice
    assert "TaskUpdate(task_id=TASK_IMPL, subject=" not in skill
    assert "4. `→ N pending items — selecting more than 50 runs them all in this pass" in bulk_text
    assert "the ≥19 call when more than 50 items are pending" in bulk_text
    assert "print one line in the reply" not in notice
    assert "the report's summary carries Step 3d's large-selection line" in skill
    assert "No question, no trim." in notice
    assert "AskUserQuestion" not in notice


def test_challenge_phase_bounds_items_per_agent() -> None:
    """Phase 1 splits each challenge domain into chunks of at most 12 items, even when one file holds more.

    One challenger per whole domain grew without bound (4 tool calls per item), so a large domain exceeded the agent
    stall budget. File affinity once let a single file's chunk grow past 12 — a one-file PR with 25 comments handed one
    challenger ~100 calls — so the cap now binds that case too, with the file cut into ordered concurrent chunks.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    caps = dispatch[dispatch.index("**Caps**") : dispatch.index("**Parallel specialist-worktree dispatch**")]
    phase1 = dispatch[dispatch.index("### Phase 1: Challenge") : dispatch.index("**Challenge double-timeout gate**")]
    assert "AskUserQuestion" not in caps
    assert "Spawn wave cap" in caps
    assert "-le 20" not in dispatch
    assert "**Chunk each domain group at `CHALLENGE_CHUNK=12` items.**" in phase1
    assert "splits into at least `ceil(n_d/12)` chunks" in phase1
    assert "one more whenever whole-file packing would push a chunk past 12" in phase1
    assert "No chunk ever holds more than 12 items." in phase1
    assert "Keep every file's items in one chunk while that file holds ≤12 items" in phase1
    assert "One file with more than 12 items → that file's items alone, priority order, cut into `ceil(n/12)`" in phase1
    assert "they fire concurrently with every other chunk" in phase1
    assert "over 12 allowed" not in dispatch
    assert "`logic-1`, `logic-2`" in phase1
    assert "one row per fired chunk" in phase1


def test_selection_table_keeps_one_row_per_item_id() -> None:
    """No resolve instruction clusters LOW items into composite rows, because the selection gate checks every id.

    The taxonomy's LOW Grouping Rule once told report mode to compress past 18 pending items into composite rows with no
    per-id cell; the hook denied that table on every first call. Skill text, taxonomy and README now agree on one row
    per item at every pending count.
    """
    resolve_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(_RESOLVE.rglob("*.md")))
    report = (_RESOLVE / "modes/report-intelligence.md").read_text(encoding="utf-8")
    taxonomy = (_RESOLVE.parent / "_shared/review-section-taxonomy.md").read_text(encoding="utf-8")
    readme = (_PLUGIN / "README.md").read_text(encoding="utf-8")
    assert [match.group(0) for match in re.finditer(r"(?i)[^.]*\b(?:composite|cluster)", resolve_text)] == [
        match.group(0) for match in re.finditer(r"(?i)[^.]*\bnever clustered into composite", resolve_text)
    ]
    assert "the displayed table keeps one row per item id at every pending count" in report
    assert "Never cluster LOW items into composite display rows" in taxonomy
    assert "Compress until total ≤ 18" not in taxonomy
    assert "LOW findings are never clustered" in readme
    assert "cluster into composite rows" not in readme


def test_large_selection_table_is_routed_to_the_table_file() -> None:
    """Step 3d sends any table over the preview cap — always the ≥19 compressed table — to the run's table file.

    The host hides a preview over 2000 characters and clips one past the terminal height with no scroll, so a 25-row
    table in the previews would leave most rows unseen; the question text names the file and every preview a summary.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    cap = skill[skill.index("**Preview cap — MANDATORY**") : skill.index("**Cap mechanics")]
    large = skill[
        skill.index("**≥19 pending items — context-budget mode**") : skill.index("<!-- branch: main-path — commit-mode")
    ]
    assert "**2000 characters and 12 rendered lines**" in cap
    assert "`$IMPL_DIR/action-items-table.md` with the Write tool, before the call" in cap
    assert "Add line 6 of the **Bulk question text**" in cap
    assert "the same compact summary within the cap" in cap
    assert "with every row in `$IMPL_DIR/action-items-table.md` per **Preview cap**" in large
    assert "19+ rows never fit the `preview` of every bulk-action option" in large
    assert "6. `→ Full item table: <IMPL_DIR>/action-items-table.md`" in skill


def test_readme_states_the_real_implementation_caps() -> None:
    """The README guard rail names both item caps and the serial cases, matching dispatch's Phase 2 split rules."""
    readme = (_PLUGIN / "README.md").read_text(encoding="utf-8")
    guard = readme[readme.index("**Guard rails:**") : readme.index("More than 20 conflicted files")]
    assert "at most 5 items each (8 with per-specialist dispatch)" in guard
    assert "sequential dispatch runs one worktree at a time" in guard
    assert "runs as a serial chain of agents" in guard
    assert "implementation groups at most 5, all in parallel waves" not in readme


def _phase2_text() -> str:
    """Return the Phase 2 implementation section of the action-item dispatch mode."""
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    return dispatch[dispatch.index("### Phase 2: Implementation") : dispatch.index("### Phase 3: Merge-back")]


@pytest.mark.parametrize(
    ("mode", "split"),
    [
        pytest.param("auto", "≤5 items/spawn (`GROUP_CAP=5`); one file past 5 → chained links", id="auto"),
        pytest.param("sequential", "same split as `auto`", id="sequential"),
        pytest.param(
            "per-specialist", "≤8 items/spawn (`GROUP_CAP=8`); one file past 8 → chained links", id="per-specialist"
        ),
    ],
)
def test_every_dispatch_mode_caps_items_per_spawn(mode: str, split: str) -> None:
    """Each Step 3d width keeps a numeric per-spawn item cap, so no mode hands one agent an unbounded group.

    The per-specialist width once had no split at all, and a single file's items had no ceiling even in auto: one agent
    received 14-17 items and ran 109-147 turns. Every width row must name its cap and none may say "none".
    """
    phase2 = _phase2_text()
    row = next(line for line in phase2.splitlines() if line.startswith(f"| `{mode}` |"))
    assert split in row
    assert "none" not in row


def test_no_width_answer_lifts_the_per_spawn_cap() -> None:
    """The split applies in every mode and the former no-split trade is gone from the skill and its Step 3d label.

    A cap that one answer may drop is not a cap; the stall warning on the old label existed only because it could.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    phase2 = _phase2_text()
    assert "split at `GROUP_CAP` items per spawn in **every** mode" in phase2
    assert "no mode, chain or preview answer ever hands one spawn more than `GROUP_CAP` items" in phase2
    assert (
        "(c) Per specialist — one worktree per specialist, split at ≤8 items (fewer spawns; >8 items still split)"
        in skill
    )
    assert "(a) Auto — one worktree per specialist, split at ≤5 items, pool-capped waves (Recommended)" in skill
    assert ">~10 items" not in skill
    assert "skips only the ≤5 split" not in phase2
    assert "≤6 total" not in phase2


def test_same_file_overflow_runs_as_sequential_chained_links() -> None:
    """One file with more items than the cap becomes one group whose links run one after another.

    File affinity alone had no ceiling: keeping every file's items together let one file's 17 items reach a single
    agent. Links of at most the cap share one group tag so Phase 3 keeps their commit order, and each later link
    fast-forwards onto the previous link's tip instead of sharing a non-isolated path.
    """
    phase2 = _phase2_text()
    assert "its own **chained group**" in phase2
    assert "cut into `ceil(n/GROUP_CAP)` ordered links of ≤`GROUP_CAP`" in phase2
    assert "Links share one `group` tag and run strictly one after another" in phase2
    assert "links 5·5·5·2 (4 spawns, one at a time); `per-specialist` → links 8·8·1" in phase2
    assert "a chained group counts as **one** slot for its whole life" in phase2
    assert "then pins its worktree to link k's recorded tip (§Spawn base below)" in phase2
    assert "Items with no `.file` share no file to conflict on" in phase2
    assert "never chain however many there are" in phase2
    assert "A wave holding a chain returns only when that chain's last link's fences ran" in phase2
    assert "the chain's unspawned items stay `pending` (never marked `in_progress`" in phase2
    assert "`$IMPL_DIR/agent-watch-impl-<group_tag>.link<k>.tsv`" in phase2
    assert "Never a rewrite of `agent-watch-impl.tsv`" in phase2


def test_sw_engineer_runs_on_sonnet_unless_a_group_holds_an_xhigh_item() -> None:
    """A high- or medium-effort sw-engineer group spawns on sonnet and its pool; only an xhigh item keeps opus.

    The pool follows the spawn's effective model, not the agent name, so the override must also move the spawn from the
    opus ceiling to the sonnet one; other opus-tier specialists are never overridden. Medium-only groups (C1 fall-
    through) were added to a high-only rule on purpose, so the widening must stay stated wherever the tier is.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    readme = (_PLUGIN / "README.md").read_text(encoding="utf-8")
    phase2 = _phase2_text()
    wave = phase2[phase2.index("**Spawn wave cap**") : phase2.index("Snapshot the worktree list before dispatch")]
    assert "Pool = the spawn's effective model tier, not its agent name." in wave
    assert "a `foundry:sw-engineer` group whose max `ITEM_EFFORT` is `high` or `medium` — no `xhigh` item" in wave
    assert "Deliberate widening of a `high`-only sonnet rule: a `medium`-only group is C1 fall-through" in wave
    assert "Only `xhigh` keeps opus." in wave
    assert "`medium`-only groups are included on purpose, a deliberate widening of a `high`-only rule" in skill
    assert "medium-only groups are included deliberately" in readme
    assert 'passes `model="sonnet"` and draws from `CAP_SONNET`' in wave
    assert "any `xhigh` item → no `model` argument, opus frontmatter, `CAP_OPUS`" in wave
    assert "`foundry:solution-architect`/`perf-optimizer` → opus pool, never overridden" in wave
    assert "all draw from the opus pool" not in phase2
    assert '<model="sonnet", — §Spawn wave cap model tier only; drop otherwise>' in phase2


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
    (impl_dir / "c1-item-current.txt").write_text("1\n", encoding="utf-8", newline="\n")  # the brief block's record
    block = _bash_block_after(dispatch, "**SECURITY — every field below comes from")
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
    (impl_dir / "c1-item-now.txt").write_text("1\n", encoding="utf-8", newline="\n")  # as the Write tool would
    block = _bash_block_after(dispatch, "**SECURITY — never type a review comment")
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
    assert "a chained group resumes at its next link from chain-<tag>.tsv via the spawn-base block" in contract


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
    assert "| 1 | Q1 bulk · Q2 commit-mode · Q3 dispatch — no item question |" in skill
    assert "| 2-3 | Q1 bulk · Q2 items · Q3 commit-mode · Q4 dispatch |" in skill
    assert (
        "| 4-6 | Q1 bulk · Q2-Q3 items (2-3 each, balanced) | Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |"
        in skill
    )
    assert "Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |" in skill
    assert "Q4 dispatch (all four slots; no item checkboxes exist in this mode)" in skill
    assert "discard the commit-mode, topic-group, **and dispatch** answers from the same call" in skill
    assert 'echo "commit-mode=$_CM group-strategy=$_GS dispatch-mode=$_DM push=$_PA post-pr=$_PP"' in skill
    assert "`DISPATCH_MODE=sequential` narrows every wave to **one** group regardless of pool" in dispatch
    assert "`per-specialist` only widens the cap to `GROUP_CAP=8`" in dispatch
    assert "**Group-preview gate — `DISPATCH_MODE=preview` only" in dispatch
    assert "so the sentinel holds a width, never `preview`" in dispatch
    assert "re-runs the import-coupling merge at `GROUP_CAP=8`, then re-splits" in dispatch
    assert "(a) is both default and recommended, so it stays first" in skill


@pytest.mark.parametrize("band", ["1", "2-3", "4-6", "7-9", "10-18"])
def test_dispatch_question_shares_a_call_with_commit_mode(band: str) -> None:
    """Each Step 3d slot-table band asks how to parallelize in the same call that asks how to commit."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    row = next(line for line in skill.splitlines() if line.startswith(f"| {band} | Q"))
    calls = [cell for cell in row.strip("|").split("|")[1:] if "commit-mode" in cell]
    assert len(calls) == 1, row
    assert "dispatch" in calls[0], row


#: Step 3d's even-split rule for item-checkbox questions, quoted verbatim so the test below computes the same split.
_SPLIT_RULE = (
    "Split n pending items (2–18) into `q = ceil(n/3)` questions as evenly as possible: the first `n mod q` questions "
    "take `n//q + 1` items, the rest `n//q`"
)


def _balanced_split(pending: int) -> list[int]:
    """Item counts per checkbox question under Step 3d's even-split rule, larger questions first."""
    questions = math.ceil(pending / 3)
    size, larger = divmod(pending, questions)
    return [size + 1] * larger + [size] * (questions - larger)


def _call_one_item_questions(skill: str, pending: int) -> int:
    """Count the item-checkbox questions the Step 3d slot table puts in Call 1 for ``pending`` items."""
    table = skill[skill.index("| Pending | Call 1 slots |") : skill.index("Checkbox mode holds")]
    for line in table.splitlines():
        low, _, high = line.strip("|").split("|")[0].strip().partition("-")
        if low.isdigit() and int(low) <= pending <= int(high or low):
            match = re.search(r"Q(\d)(?:-Q(\d))? items", line)
            return int(match.group(2) or match.group(1)) - int(match.group(1)) + 1 if match else 0
    raise AssertionError(f"no slot-table band covers {pending} pending items")


@pytest.mark.parametrize("pending", range(2, 19))
def test_item_checkbox_split_never_asks_a_one_option_question(pending: int) -> None:
    """Every checkbox split Step 3d documents holds 2-3 items per question and fits the slot table's band.

    A greedy fill to 3 left a single-item question at 4, 7, 10, 13 and 16 pending items; ``AskUserQuestion`` rejects a
    question with fewer than two options, so the picker could not be answered.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")

    sizes = _balanced_split(pending)

    assert _SPLIT_RULE in skill
    assert sum(sizes) == pending
    assert 2 <= min(sizes) <= max(sizes) <= 3
    assert len(sizes) <= 6
    assert _call_one_item_questions(skill, pending) == min(len(sizes), 3)


@pytest.mark.parametrize("band", ["0, closed items present", "1", "2-3", "4-6", "7-9", "10-18"])
def test_every_selection_call_leads_with_the_bulk_question(band: str) -> None:
    """Every Step 3d selection call asks the bulk question first, so its table preview is the first screen.

    With the bulk page after the item checkboxes, the user picked items from short labels and reached the table last.
    The two-call band checks both calls.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    table = skill[skill.index("| Pending | Call 1 slots |") : skill.index("Checkbox mode holds")]
    row = next(line for line in table.splitlines() if line.startswith(f"| {band} | "))
    calls = row.strip("|").split("|")[1].split("→ Call 2:")
    assert [call.strip().split(" · ")[0] for call in calls] == ["Q1 bulk"] * len(calls)


def test_bulk_choice_never_discards_checked_or_typed_picks() -> None:
    """A bulk answer adds to the user's ticks and typed ids, and Skip all yields to them; the question text says so.

    With the bulk page first, a user answering it and then ticking items on later tabs lost every tick: (a)-(c) replaced
    the checked set, and nothing on the page told the user to leave it unanswered to cherry-pick.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    resolution = skill[skill.index("**Bulk-action resolution**") : skill.index("**Item checkbox questions**:")]
    text = skill[skill.index("**Bulk question text**") : skill.index("**ESSENTIAL — exactly these 4 options")]
    assert "a bulk answer never discards an explicit pick" in resolution
    assert "- (a) → `SELECTED_ITEMS` = all pending `[req]` IDs ∪ checked IDs ∪ typed IDs" in resolution
    assert "- (b) → `SELECTED_ITEMS` = all pending `[suggest]` IDs ∪ checked IDs ∪ typed IDs" in resolution
    assert "- (c) → `SELECTED_ITEMS` = all pending [req+suggest] IDs ∪ typed IDs" in resolution
    assert (
        "(d) with any checked or typed ID → resolve exactly as unanswered below: the explicit picks win." in resolution
    )
    assert "closed items excluded" not in resolution
    assert (
        "leave this unanswered and tick them on the next tabs. (a)–(c) add every item you tick or type; (d) applies"
        " only when you tick or type none.` — every selection call carrying item-checkbox questions" in text
    )


def test_closed_only_branch_states_the_same_bulk_resolution() -> None:
    """The closed-only branch says typed ids join any bulk answer and Skip all yields to them, as the resolution does.

    The branch once said Skip all "stops as usual" while the resolution rules made Skip all plus typed ids resolve as
    unanswered; that call carries no cherry-pick line explaining the union, so the branch text is the only statement.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    branch = skill[skill.index("- **Zero pending, closed items present**") : skill.index("- **Zero pending, no closed")]
    assert "Bulk (a)/(b)/(c) selects no closed IDs; (d) stops as usual." not in branch
    assert "typed IDs join any answer and (d) yields to them, while (d) with nothing typed stops as usual" in branch


@pytest.mark.parametrize("pending", [4, 5, 7, 10, 13, 16])
def test_step_3d_split_examples_are_the_rules_own_output(pending: int) -> None:
    """Each worked split example in Step 3d matches the rule, and no greedy "first 9" wording is left.

    The counts are the ones a greedy fill got wrong (a 1-item remainder) or that a two-call split must keep off Call 2.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    step = skill[skill.index("## Step 3d: User item selection") : skill.index("<!-- branch: main-path — commit-mode")]

    assert f"{pending} → {'+'.join(map(str, _balanced_split(pending)))}" in step
    assert "first 9" not in step
    assert "items 1-9" not in step


def test_context_budget_mode_asks_commit_mode_and_dispatch_together() -> None:
    """The ≥19-item single call carries commit mode and dispatch granularity side by side."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    assert "Q1 bulk action · Q2 commit-mode · Q3 topic-group · Q4 dispatch" in skill


#: Post-Step-3d `AskUserQuestion` mentions that are error recovery, a user-elected gate, or an explicit "no ask" note.
_POST_SELECTION_ASK_ALLOWLIST = (
    "No confirming commit found",  # straggler gate: unresolved item status
    "**Push confirmation — one `AskUserQuestion` call.**",  # Step 10: scope-bearing push confirmation (git-push)
    "**Drift question — its own `AskUserQuestion` call",  # Step 10: target moved since Step 9 (BASE_FRESH=no only)
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


def test_step_10_push_confirmation_is_the_git_push_question() -> None:
    """Step 10 asks the ``git-push`` question, so the user's Approve records the push token the push guard spends.

    Any other question shape records nothing, and every push would stop at the guard. The drift choice cannot sit in a
    two-option question: it is its own call, asked first and only when the target moved. The explicit-refspec fallback
    is never asked again: no approval covers a push whose remote and ref are run-time values, so the user runs it.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    push_step = skill[skill.index("## Step 10: Push") : skill.index("## Step 11")]
    drift = push_step.index("**Drift question — its own `AskUserQuestion` call, `BASE_FRESH=no` only")
    confirmation = push_step.index("**Push confirmation — one `AskUserQuestion` call.**")
    options = push_step[confirmation : push_step.index("Q2 — `unset` intent only")]

    assert drift < confirmation
    assert "Q1 — push: header `git-push`, `multiSelect` false." in options
    assert "- **Approve** — this push, once" in options
    assert "- **Deny** — skip the push" in options
    assert "Re-sync target first" not in options
    # A push approval covers only this branch written out, so the run-time-valued fallback is handed to the user.
    assert "no second push question, no agent push" in push_step
    assert 'git push "$FORK_REMOTE"' not in push_step
    assert "! touch" not in push_step


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
    "status",
    [
        "pushed",
        "blocked-guard",
        "blocked-permission",
        "blocked-needs-manual-push",
        "rejected-non-ff",
        "skipped-by-user",
        "not-attempted",
    ],
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
@pytest.mark.parametrize(
    "status",
    [
        pytest.param("blocked-guard", id="guard-blocked"),
        pytest.param("blocked-needs-manual-push", id="handed-over-explicit-refspec"),
    ],
)
def test_step_11_surfaces_push_status_and_unblock_file(tmp_path: Path, status: str) -> None:
    """The final report reads the recorded status and the saved unblock lines, not the transcript.

    A push the guard blocked and a push handed to the user (no upstream tracking: the explicit-refspec form no approval
    covers) both end the report with the line the user runs.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    block = _bash_block_containing(skill, "boundary3: pre-final-report write")
    session = "resolve-report-push"
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    (impl_dir / "push-unblock.txt").write_text("git push origin pr-7\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-impl-dir-{session}").write_text(f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-push-status-{session}").write_text(f"{status}\n", encoding="utf-8", newline="\n")
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
    assert f"PUSH_STATUS={status}" in result.stdout
    assert f"PUSH_UNBLOCK={_bash_path(impl_dir)}/push-unblock.txt\ngit push origin pr-7\n" in result.stdout
    assert f"push-status={status}" in (tmp_path / ".temp/state/skill-contract.md").read_text(encoding="utf-8")


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


#: Phase 2 envelope fence markers: the ledger/worktree fence prints the chain tip, the other records skipped items.
_LEDGER_FENCE = 'echo "→ chain tip ${_GROUP_TAG}'
_SKIPPED_FENCE = "_SKIP_COUNT_BEFORE="
#: Marker line of the spawn-base block, which reads its tags from a Write-tool file instead of a placeholder.
_SPAWN_NOW_LINE = '_SPAWN_NOW="$IMPL_DIR/phase2-spawn-now.txt"'
_skip_no_fence_tools = pytest.mark.skipif(
    _BASH is None or shutil.which("jq") is None or shutil.which("git") is None,
    reason="The Phase 2 envelope fence uses Bash, jq and git",
)


class Lineage(NamedTuple):
    """A repository standing in for the PR checkout, with one base commit and one link commit on top."""

    repo: Path
    base: str
    tip: str
    stale: Path


def _git(cwd: Path, *args: str) -> str:
    """Run one git command in a fixture repository and return its stripped stdout."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def lineage(tmp_path: Path) -> Lineage:
    """Create a repo with a base and a tip commit, plus a stale worktree left on the base commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Resolve Test")
    _git(repo, "config", "user.email", "resolve@example.invalid")
    (repo / "core.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "core.py")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "core.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
    _git(repo, "commit", "-qam", "link commit")
    stale = tmp_path / "stale"
    _git(repo, "worktree", "add", "-q", "--detach", str(stale), base)
    return Lineage(repo, base, _git(repo, "rev-parse", "HEAD"), stale)


def _envelope_fence(tmp_path: Path, marker: str, tag: str = "core\n") -> str:
    """Write ``tag`` to ``phase2-fence-now.txt`` as the Write tool would; return the unedited envelope fence.

    The fence reads its group tag from that file and derives the chain link from disk, so its text runs verbatim.
    """
    _write_impl_file(tmp_path, "phase2-fence-now.txt", tag)
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    return _bash_block_containing(dispatch, marker)


def _run_resolve_block(
    block: str, tmp_path: Path, cwd: Path, base_sha: str, shell: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one Phase 2 block after the first spawn wave pinned ``phase2-base-sha``; ``impl`` is the run dir.

    ``base_sha`` seeds both the prelude's ``resolve-base-sha`` sentinel and the run dir's ``phase2-base-sha`` pin; an
    empty value leaves both unusable. ``shell`` defaults to bash.
    """
    _write_impl_file(tmp_path, "phase2-base-sha", f"{base_sha}\n")
    return _run_unpinned_block(block, tmp_path, cwd, base_sha, shell)


def _run_unpinned_block(
    block: str, tmp_path: Path, cwd: Path, base_sha: str, shell: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one Phase 2 block before any spawn pinned Phase 2: only the prelude's ``resolve-base-sha`` is recorded."""
    session = "resolve-chain-link"
    (tmp_path / "impl").mkdir(exist_ok=True)
    (tmp_path / f"resolve-impl-dir-{session}").write_text(
        f"{_bash_path(tmp_path / 'impl')}\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / f"resolve-base-sha-{session}").write_text(f"{base_sha}\n", encoding="utf-8", newline="\n")
    return subprocess.run(
        [shell or _BASH, "-c", block],
        cwd=cwd,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _write_impl_file(tmp_path: Path, name: str, content: str) -> None:
    """Write one file into the run dir ``impl`` the Phase 2 blocks read."""
    (tmp_path / "impl").mkdir(exist_ok=True)
    (tmp_path / "impl" / name).write_text(content, encoding="utf-8", newline="\n")


@_skip_no_fence_tools
@pytest.mark.parametrize(
    ("link", "envelope_name", "chain_rows"),
    [
        pytest.param("1", "phase2-envelope-core.json", "", id="unchained-or-first-link"),
        pytest.param("2", "phase2-envelope-core.link2.json", "1\t{base}\n", id="second-chain-link"),
    ],
)
def test_ledger_fence_tags_every_chain_link_with_the_shared_group(
    tmp_path: Path, lineage: Lineage, link: str, envelope_name: str, chain_rows: str
) -> None:
    """A chain link reads its own envelope file but lands its commits under the chain's one group tag.

    Phase 3 keeps commit order only within one group, so link k+1's commits must share link k's tag. Its own envelope
    file keeps the agent-watch row from reading link 1's envelope as already done, and the recorded tip is the next
    link's pinned base.
    """
    envelope = {"worktree": _bash_path(lineage.repo), "commits": [{"item_id": 7, "sha": lineage.tip}], "skipped": []}
    _write_impl_file(tmp_path, envelope_name, json.dumps(envelope))
    _write_impl_file(tmp_path, "chain-core.tsv", chain_rows.format(base=lineage.base))

    result = _run_resolve_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 0, result.stdout + result.stderr
    ledger = (tmp_path / "impl/phase2-commits.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(row) for row in ledger] == [{"item_id": 7, "sha": lineage.tip, "group": "core"}]
    assert f"→ chain tip core link {link}: {lineage.tip}" in result.stdout
    chain = (tmp_path / "impl/chain-core.tsv").read_text(encoding="utf-8").splitlines()
    assert chain[-1] == f"{link}\t{lineage.tip}"


@_skip_no_fence_tools
def test_ledger_fence_carries_the_base_forward_for_a_link_without_commits(tmp_path: Path, lineage: Lineage) -> None:
    """A link that committed nothing records its unchanged base as the tip, so the chain continues.

    The harness removes a worktree that ended without changes, so neither a worktree HEAD nor a commit is left to read.
    """
    envelope = {"worktree": "", "commits": [], "skipped": [{"item_id": 7, "reason": "already fixed"}]}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(envelope))

    result = _run_resolve_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"→ chain tip core link 1: {lineage.base}" in result.stdout
    assert (tmp_path / "impl/chain-core.tsv").read_text(encoding="utf-8") == f"1\t{lineage.base}\n"


@_skip_no_fence_tools
def test_ledger_fence_carries_the_phase2_pin_not_the_prelude_fingerprint(tmp_path: Path, lineage: Lineage) -> None:
    """A link 1 without commits hands link 2 the Phase 2 pin, never the older prelude ``resolve-base-sha``.

    The prelude fingerprint predates C1's ``each``-mode commits; carrying it forward would start link 2 on pre-C1 copies
    of the files C1 edited.
    """
    envelope = {"worktree": "", "commits": [], "skipped": [{"item_id": 7, "reason": "already fixed"}]}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(envelope))
    _write_impl_file(tmp_path, "phase2-base-sha", f"{lineage.tip}\n")

    result = _run_unpinned_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "impl/chain-core.tsv").read_text(encoding="utf-8") == f"1\t{lineage.tip}\n"


@_skip_no_fence_tools
@pytest.mark.parametrize(
    ("worktree_key", "base_key", "reason"),
    [
        pytest.param("repo", "base", "base mismatch", id="link-reported-base-mismatch"),
        pytest.param("stale", "tip", "", id="worktree-head-off-its-base"),
    ],
)
def test_ledger_fence_ends_the_lineage_when_a_link_is_off_its_base(
    tmp_path: Path, lineage: Lineage, worktree_key: str, base_key: str, reason: str
) -> None:
    """A link that could not pin its base, or whose HEAD does not descend from it, ends the chain with a record.

    An unpinned worktree sits on the default branch; handing its HEAD to the next link would fork a second lineage.
    """
    worktree = {"repo": lineage.repo, "stale": lineage.stale}[worktree_key]
    envelope = {"worktree": _bash_path(worktree), "commits": [], "skipped": [{"item_id": 9, "reason": reason}]}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(envelope))
    base = {"base": lineage.base, "tip": lineage.tip}[base_key]

    result = _run_resolve_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "⚠ group core link 1: lineage ended" in result.stdout
    assert "→ chain tip" not in result.stdout
    assert (tmp_path / "impl/chain-core.tsv").read_text(encoding="utf-8") == "1\tend\n"


@_skip_no_fence_tools
def test_ledger_fence_rerun_records_each_link_once(tmp_path: Path, lineage: Lineage) -> None:
    """Running the fence twice for one link leaves one lineage row and one commit row, and reports the same tip.

    The commits ledger has no dedup of its own, so a rerun after compaction must append nothing; the repeated tip line
    lets the orchestrator still spawn the next link from it.
    """
    envelope = {"worktree": _bash_path(lineage.repo), "commits": [{"item_id": 7, "sha": lineage.tip}], "skipped": []}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(envelope))
    block = _envelope_fence(tmp_path, _LEDGER_FENCE)

    first = _run_resolve_block(block, tmp_path, lineage.repo, lineage.base)
    rerun = _run_resolve_block(block, tmp_path, lineage.repo, lineage.base)

    assert (first.returncode, rerun.returncode) == (0, 0)
    assert (tmp_path / "impl/chain-core.tsv").read_text(encoding="utf-8") == f"1\t{lineage.tip}\n"
    assert len((tmp_path / "impl/phase2-commits.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert f"→ chain tip core link 1: {lineage.tip} (already recorded — nothing appended)" in rerun.stdout


@_skip_no_fence_tools
def test_ledger_fence_ingests_the_newest_link_not_an_earlier_one(tmp_path: Path, lineage: Lineage) -> None:
    """With link 1 recorded and link 2's envelope persisted, the fence ingests link 2 and never re-reads link 1.

    A typed link number could name link 1 again: its commits landed in the ledger twice, link 2's envelope was never
    ingested, and the spawn-base block re-spawned link 2. The link now comes from the newest envelope on disk.
    """
    link1 = {"worktree": "", "commits": [{"item_id": 3, "sha": lineage.base}], "skipped": []}
    link2 = {"worktree": _bash_path(lineage.repo), "commits": [{"item_id": 8, "sha": lineage.tip}], "skipped": []}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(link1))
    _write_impl_file(tmp_path, "phase2-envelope-core.link2.json", json.dumps(link2))
    _write_impl_file(tmp_path, "chain-core.tsv", f"1\t{lineage.base}\n")
    _write_impl_file(
        tmp_path, "phase2-commits.jsonl", json.dumps({"item_id": 3, "sha": lineage.base, "group": "core"}) + "\n"
    )

    result = _run_resolve_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 0, result.stdout + result.stderr
    ledger = [
        json.loads(row) for row in (tmp_path / "impl/phase2-commits.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["item_id"] for row in ledger] == [3, 8]
    assert (tmp_path / "impl/chain-core.tsv").read_text(encoding="utf-8").splitlines()[-1] == f"2\t{lineage.tip}"


@_skip_no_fence_tools
def test_ledger_fence_blocks_when_an_earlier_link_was_never_recorded(tmp_path: Path, lineage: Lineage) -> None:
    """A newest envelope two links past the lineage record blocks instead of ingesting out of order.

    Link k+1 spawns only after link k's fences ran, so a gap means a fence was skipped; ingesting anyway would record a
    tip whose predecessor row is missing and hand the next link a base no fence checked.
    """
    envelope = {"worktree": _bash_path(lineage.repo), "commits": [{"item_id": 8, "sha": lineage.tip}], "skipped": []}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(envelope))
    _write_impl_file(tmp_path, "phase2-envelope-core.link2.json", json.dumps(envelope))

    result = _run_resolve_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 1
    assert "! BLOCKED — chain-core.tsv records 0 link(s) but link 2's envelope is the newest" in result.stdout
    assert not (tmp_path / "impl/phase2-commits.jsonl").exists()


#: phase2-groups.tsv as written once before the first spawn: a two-link ``core`` chain and an unchained ``docs`` group.
_GROUPS_TSV = "core\t1\t3 4 5 6 7\ncore\t2\t8 9\ndocs\t1\t2\n"


def _spawn_base_block(tmp_path: Path, tags: str) -> str:
    """Write ``tags`` to ``phase2-spawn-now.txt`` as the Write tool would; return the unedited spawn-base block."""
    _write_impl_file(tmp_path, "phase2-spawn-now.txt", tags)
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    return _bash_block_containing(dispatch, _SPAWN_NOW_LINE)


@pytest.mark.skipif(_BASH is None, reason="The spawn-base block is Bash")
@pytest.mark.parametrize(
    ("chain_rows", "expected"),
    [
        pytest.param(
            "", f"→ spawn core link 1 base {'a' * 40} items 3 4 5 6 7", id="unchained-or-first-link-gets-phase2-pin"
        ),
        pytest.param(
            f"1\t{'b' * 40}\n", f"→ spawn core link 2 base {'b' * 40} items 8 9", id="next-link-gets-last-tip"
        ),
        pytest.param("1\tend\n", "⚠ core: lineage ended at link 1 — spawn nothing", id="ended-lineage-spawns-nothing"),
        pytest.param(
            f"1\t{'b' * 40}\n2\t{'c' * 40}\n", "✓ core: all 2 link(s) recorded — spawn nothing", id="finished-chain"
        ),
    ],
)
def test_spawn_base_block_pins_each_spawn_to_the_phase2_pin_or_the_recorded_tip(
    tmp_path: Path, chain_rows: str, expected: str
) -> None:
    """Each spawn's base is the Phase 2 pin for a fresh group and the last recorded tip for a chain's next link.

    The lineage and groups files are durable, so the same block resumes a chain after compaction instead of re-forming
    it off base, and names each link's items from the persisted groups rather than a re-derived grouping.
    """
    _write_impl_file(tmp_path, "phase2-groups.tsv", _GROUPS_TSV)
    _write_impl_file(tmp_path, "chain-core.tsv", chain_rows)

    result = _run_resolve_block(_spawn_base_block(tmp_path, "core docs"), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout
    assert f"→ spawn docs link 1 base {'a' * 40} items 2" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="The spawn-base block is Bash")
@pytest.mark.parametrize(
    ("groups", "tags", "base_sha", "message"),
    [
        pytest.param(_GROUPS_TSV, "core", "", "! BLOCKED — phase2-base-sha missing or invalid", id="no-pinnable-head"),
        pytest.param("", "core", "a" * 40, "phase2-groups.tsv missing", id="groups-never-persisted"),
        pytest.param(
            _GROUPS_TSV,
            "core-module",
            "a" * 40,
            "! BLOCKED — group tag core-module not in phase2-groups.tsv",
            id="re-derived-tag",
        ),
        pytest.param("core\t1\tseven\n", "core", "a" * 40, "has a malformed row", id="malformed-groups-row"),
        pytest.param(
            "core\t1\t1 2\ncore\t1\t3 4\n",
            "core",
            "a" * 40,
            "phase2-groups.tsv: group core link 1 is listed twice; rewrite it with the Write tool before any spawn",
            id="duplicate-tag-link-row",
        ),
        pytest.param(
            "core\t1\t1 2\ncore\t3\t3 4\n",
            "core",
            "a" * 40,
            "phase2-groups.tsv: group core skips a link number (want links 1..3, each once)",
            id="link-gap",
        ),
        pytest.param(
            "core\t1\t1 2\ndocs\t1\t2 5\n",
            "core",
            "a" * 40,
            "phase2-groups.tsv: item 2 sits in two rows",
            id="item-in-two-rows",
        ),
    ],
)
def test_spawn_base_block_refuses_to_spawn_from_unrecorded_state(
    tmp_path: Path, groups: str, tags: str, base_sha: str, message: str
) -> None:
    """A lost pin, a missing or inconsistent groups file, or a tag the groups file does not list blocks dispatch.

    Each would otherwise start a worktree off the recorded lineage: on the default branch, or under a slug re-derived
    after compaction that misses its chain file and forks a second lineage on the same file. A (tag, link) listed twice
    merged both rows into one spawn past the item cap on one envelope; a link gap read as a finished chain; an item in
    two rows gets two agents and duplicate commits.
    """
    _write_impl_file(tmp_path, "phase2-groups.tsv", groups)

    result = _run_resolve_block(_spawn_base_block(tmp_path, tags), tmp_path, tmp_path, base_sha)

    assert result.returncode == 1
    assert message in result.stdout
    assert "→ spawn" not in result.stdout


@pytest.mark.parametrize("shell", _SHELLS)
def test_spawn_base_block_consumes_its_tag_list(tmp_path: Path, shell: str) -> None:
    """The block reads this spawn's tags from the Write-tool file, one per line or space-separated, then consumes it.

    A placeholder kept the block off the blueprint manifest, so every wave and link prompted in unattended Run 2; the
    file keeps the text invariant. Consuming it means a stale list never re-spawns a group still in flight: a rerun
    without a fresh list blocks. Under zsh a loop over the bare tag variable ran once with both tags glued together and
    blocked every multi-group wave as an unknown tag.
    """
    _write_impl_file(tmp_path, "phase2-groups.tsv", _GROUPS_TSV)
    block = _spawn_base_block(tmp_path, "docs\ncore\n")

    first = _run_resolve_block(block, tmp_path, tmp_path, "a" * 40, shell)
    rerun = _run_resolve_block(block, tmp_path, tmp_path, "a" * 40, shell)

    assert first.returncode == 0, first.stdout + first.stderr
    assert f"→ spawn docs link 1 base {'a' * 40} items 2" in first.stdout
    assert f"→ spawn core link 1 base {'a' * 40} items 3 4 5 6 7" in first.stdout
    assert not (tmp_path / "impl/phase2-spawn-now.txt").exists()
    assert (tmp_path / "impl/phase2-spawn-now.txt.done").read_text(encoding="utf-8") == "docs\ncore\n"
    assert rerun.returncode == 1
    assert "phase2-spawn-now.txt missing; create it with the Write tool" in rerun.stdout
    assert "→ spawn" not in rerun.stdout


@pytest.mark.skipif(_BASH is None, reason="The spawn-base block is Bash")
@pytest.mark.parametrize(
    ("envelope_name", "watch_name", "expected"),
    [
        pytest.param(
            "phase2-envelope-docs.json",
            "agent-watch-impl.tsv",
            "⏳ core link 1: spawned, no envelope persisted yet — run agent_watch.py: pending → wait for its"
            " notification, timed_out → mark it ⏱; spawn nothing",
            id="link-in-flight",
        ),
        pytest.param(
            "phase2-envelope-core.json",
            "agent-watch-impl.tsv",
            "⏳ core link 1: envelope persisted, fences not run — run both envelope fences; spawn nothing",
            id="envelope-awaiting-fences",
        ),
        pytest.param(
            "phase2-envelope-docs.json",
            "agent-watch-challenge.tsv",
            "⚠ core link 1: recorded but no watch row — never launched",
            id="never-launched",
        ),
    ],
)
def test_spawn_base_block_never_spawns_a_recorded_link_twice(
    tmp_path: Path, envelope_name: str, watch_name: str, expected: str
) -> None:
    """A link the block already printed ``→ spawn`` for is refused when a resumed run declares its tag again.

    After a compaction mid-Phase 2 the in-flight group's items are not yet in the commits ledger, so the resume names
    its tag again; a second agent on the same items lands duplicate commits per item and blocks Phase 3. Other tags in
    the same list still spawn, and are recorded. The watch row, armed in the Agent() response itself, tells a launched
    link from one a compaction cut off before its Agent call.
    """
    _write_impl_file(tmp_path, "phase2-groups.tsv", _GROUPS_TSV)
    _write_impl_file(tmp_path, "phase2-spawned.tsv", "core\t1\n")
    _write_impl_file(tmp_path, envelope_name, "{}")
    _write_impl_file(tmp_path, watch_name, "impl-core\t/run/phase2-envelope-core.json\t900\n")

    result = _run_resolve_block(_spawn_base_block(tmp_path, "core docs"), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout
    assert "→ spawn core" not in result.stdout
    assert f"→ spawn docs link 1 base {'a' * 40} items 2" in result.stdout
    assert (tmp_path / "impl/phase2-spawned.tsv").read_text(encoding="utf-8") == "core\t1\ndocs\t1\n"


@pytest.mark.skipif(_BASH is None, reason="The spawn-base block is Bash")
def test_spawn_base_block_arms_a_never_launched_link_as_timed_out(tmp_path: Path) -> None:
    """A recorded link with no watch row gets a zero-deadline row, so the next watch check reports it timed out.

    A compaction between the spawn-base run and the Agent() response left the link recorded but never started; the
    resume printed "in flight" and waited for a completion notification that could never come. The armed row puts the
    link in front of ``agent_watch.py`` as ``timed_out``, the run marks it ⏱, and its items reach the straggler gate.
    """
    _write_impl_file(tmp_path, "phase2-groups.tsv", _GROUPS_TSV)
    _write_impl_file(tmp_path, "phase2-spawned.tsv", "core\t1\ncore\t2\n")
    _write_impl_file(tmp_path, "chain-core.tsv", f"1\t{'b' * 40}\n")

    result = _run_resolve_block(_spawn_base_block(tmp_path, "core"), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "its items stay pending for the Step 8 straggler gate; spawn nothing" in result.stdout
    assert "→ spawn" not in result.stdout
    armed = tmp_path / "impl/agent-watch-impl-core.link2.tsv"
    envelope = _bash_path(tmp_path / "impl/phase2-envelope-core.link2.json")
    assert armed.read_text(encoding="utf-8") == f"impl-core-link2\t{envelope}\t0\n"
    watch = subprocess.run(
        [sys.executable, str(_PLUGIN / "bin/agent_watch.py"), "--state-dir", str(tmp_path / "impl")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert watch.returncode == 0, watch.stderr
    assert "impl-core-link2" in watch.stdout
    assert "timed_out" in watch.stdout


@pytest.mark.skipif(_BASH is None, reason="The spawn-base block is Bash")
def test_spawn_base_block_spawns_the_next_link_of_a_recorded_chain(tmp_path: Path) -> None:
    """Link 2 of a chain spawns once link 1's tip is recorded, although link 1 itself is in the spawn record.

    The record is per link, so the guard against a second agent never stalls the chain the fences just advanced.
    """
    _write_impl_file(tmp_path, "phase2-groups.tsv", _GROUPS_TSV)
    _write_impl_file(tmp_path, "phase2-spawned.tsv", "core\t1\n")
    _write_impl_file(tmp_path, "chain-core.tsv", f"1\t{'b' * 40}\n")

    result = _run_resolve_block(_spawn_base_block(tmp_path, "core"), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"→ spawn core link 2 base {'b' * 40} items 8 9" in result.stdout
    assert (tmp_path / "impl/phase2-spawned.tsv").read_text(encoding="utf-8") == "core\t1\ncore\t2\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(_SPAWN_NOW_LINE, id="spawn-base"),
        pytest.param(_LEDGER_FENCE, id="ledger-fence"),
        pytest.param(_SKIPPED_FENCE, id="skipped-fence"),
    ],
)
def test_phase2_fixed_blocks_are_auto_allowed_verbatim(marker: str) -> None:
    """Each Phase 2 block run once per wave or link is an exact blueprint-manifest hit, so it never prompts.

    Run 2 is unattended: a block carrying a ``<placeholder>`` must be edited before it runs, misses the manifest, and
    parks the run on a permission prompt per group and per link.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": ""}}
    payload["tool_input"]["command"] = _bash_block_containing(dispatch, marker)

    proc = subprocess.run(
        ["node", str(_PLUGIN / "hooks" / "blueprint-allow.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


#: Marker line of each block run once per item, item batch, C1 item or topic group: each reads its runtime values from
#: a Write-tool file or a file an earlier block wrote, never from a ``<placeholder>``.
_PER_ITEM_BLOCKS = [
    pytest.param("modes/action-item-dispatch.md", 'jq -b -c --arg ids "$SELECTED_ITEMS"', id="selected-item-records"),
    pytest.param("modes/pr-intelligence.md", 'item-ids-now.txt" ] && _IDS=', id="item-records-on-demand"),
    pytest.param("modes/action-item-dispatch.md", '_C1_NOW="$IMPL_DIR/c1-item-now.txt"', id="c1-brief"),
    pytest.param(
        "modes/action-item-dispatch.md", 'IFS= read -r _BATCH_TAG < "$IMPL_DIR/c1-item-current.txt"', id="c1-commit"
    ),
    pytest.param("modes/action-item-dispatch.md", '_LOG_NOW="$IMPL_DIR/challenge-log-now.txt"', id="challenge-log"),
    pytest.param("modes/action-item-dispatch.md", '_GROUP_NOW="$IMPL_DIR/group-commit-now.txt"', id="group-commit"),
    pytest.param("SKILL.md", ': > "$IMPL_DIR/straggler-check-ids.txt"', id="straggler-gate"),
    pytest.param("SKILL.md", 'done < "$IMPL_DIR/straggler-check-ids.txt"', id="straggler-confirm"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(("relative", "marker"), _PER_ITEM_BLOCKS)
def test_per_item_blocks_are_auto_allowed_verbatim(relative: str, marker: str) -> None:
    """Each block run once per item or group carries no placeholder and is an exact blueprint-manifest hit.

    Run 2 is unattended: a block the orchestrator must edit before running misses the manifest and parks the run on one
    permission prompt per item — about fifty for a fifty-item selection. A block that runs ``rm`` is dropped from the
    manifest by the danger filter, so it prompts however invariant its text is.
    """
    block = _bash_block_containing((_RESOLVE / relative).read_text(encoding="utf-8"), marker)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": block}}

    proc = subprocess.run(
        ["node", str(_PLUGIN / "hooks" / "blueprint-allow.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert '="<' not in block
    assert "(<" not in block
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def _write_challenge_inputs(tmp_path: Path, log_now: str) -> None:
    """Persist what the challenge-log block reads: items, a chunk reply, a C1 reply, the task map and the line list."""
    items = "".join(json.dumps({"id": item, "full_comment_text": f"comment {item}"}) + "\n" for item in (1, 2, 3, 4))
    _write_impl_file(tmp_path, "action-items.jsonl", items)
    verdicts = {
        "items": [
            {
                "id": 1,
                "evidence": "VALID",
                "evidence_rationale": "ev1",
                "suggestion": "VALID",
                "suggestion_rationale": "s1",
            },
            {
                "id": 2,
                "evidence": "VALID",
                "evidence_rationale": "ev2",
                "suggestion": "REJECT",
                "suggestion_rationale": "s2",
                "alternative": "alt2",
            },
            {"id": 3, "evidence": "REJECT", "evidence_rationale": "ev3", "suggestion": "REJECT"},
        ]
    }
    _write_impl_file(tmp_path, "challenge-verdicts-logic.json", json.dumps(verdicts))
    _write_impl_file(tmp_path, "c1-reply-4.json", json.dumps({"findings": ["renamed the helper"]}))
    _write_impl_file(tmp_path, "item-tasks.tsv", "1\tt1\n2\tt2\n3\tt3\n4\tt4\n")
    _write_impl_file(tmp_path, "challenge-log-now.txt", log_now)


def _challenge_log_block() -> str:
    """Return the unedited shared challenge-log append block."""
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    return _bash_block_containing(dispatch, '_LOG_NOW="$IMPL_DIR/challenge-log-now.txt"')


@_skip_no_jq
@pytest.mark.parametrize("shell", _SHELLS)
def test_challenge_log_block_records_every_listed_item(tmp_path: Path, shell: str) -> None:
    """One run records a record per listed line, any resolution mix, tabs or spaces, last line unterminated.

    The four producers used to run the block once per item with substituted placeholders; the list keeps the text
    invariant, and one run per reply replaces one prompt per item.
    """
    _write_challenge_inputs(
        tmp_path, "1 as-suggested logic\n2\tself-resolved\tlogic\r\n3 rejected logic\n4 codex-direct 4"
    )

    result = _run_resolve_block(_challenge_log_block(), tmp_path, tmp_path, "a" * 40, shell)

    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp_path / "impl/challenge-log.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split(" finding=")[0] for line in log] == [
        "id=1 resolution=as-suggested evidence=VALID suggestion=VALID",
        "id=2 resolution=self-resolved evidence=VALID suggestion=REJECT",
        "id=3 resolution=rejected evidence=REJECT suggestion=—",
        "id=4 resolution=codex-direct evidence=VALID suggestion=VALID",
    ]
    assert log[1].endswith("evidence_why=ev2 suggestion_why=s2 detail=alt2")
    assert log[3].endswith(
        "evidence_why=renamed the helper suggestion_why=renamed the helper detail=renamed the helper"
    )
    assert "TaskUpdate target (deleted): item=3 task=t3" in result.stdout
    assert not (tmp_path / "impl/challenge-log-now.txt").exists()


@_skip_no_jq
@pytest.mark.parametrize(
    ("log_now", "message"),
    [
        pytest.param(
            "1 as-suggested logic\n2 as-suggested docs\n", "challenge-verdicts-docs.json missing/empty", id="no-reply"
        ),
        pytest.param(
            "1 as-suggested logic\nx rejected logic\n", "challenge-log line 2: item id 'x' not numeric", id="bad-id"
        ),
        pytest.param(
            "1 as-suggested logic\n9 rejected logic\n", "item 9 not found in action-items.jsonl", id="unknown-item"
        ),
    ],
)
def test_challenge_log_block_records_nothing_when_one_line_fails(tmp_path: Path, log_now: str, message: str) -> None:
    """Every line is checked before any record lands, and the list is kept, so fixing the cause and rerunning works.

    A stop after the first record would leave the log half-written and the rerun would add that record twice.
    """
    _write_challenge_inputs(tmp_path, log_now)

    result = _run_resolve_block(_challenge_log_block(), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 1
    assert message in result.stdout
    assert not (tmp_path / "impl/challenge-log.txt").exists()
    assert (tmp_path / "impl/challenge-log-now.txt").read_text(encoding="utf-8") == log_now


@_skip_no_jq
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    ("relative", "marker", "ids_file"),
    [
        pytest.param(
            "modes/action-item-dispatch.md", 'jq -b -c --arg ids "$SELECTED_ITEMS"', "selected-items.txt", id="step-8"
        ),
        pytest.param("modes/pr-intelligence.md", 'item-ids-now.txt" ] && _IDS=', "item-ids-now.txt", id="on-demand"),
    ],
)
def test_item_records_blocks_print_each_listed_record(
    tmp_path: Path, relative: str, marker: str, ids_file: str, shell: str
) -> None:
    """Both per-item details blocks print each listed record once, reading the ids from a file, never a placeholder.

    One unedited run covers every id, where the old blocks took one substituted ``_ID="<id>"`` run — and one prompt —
    per item.
    """
    _write_impl_file(tmp_path, ids_file, "3 1\n")
    _write_impl_file(tmp_path, "action-items.jsonl", "".join(json.dumps({"id": n}) + "\n" for n in (1, 2, 3)))
    block = _bash_block_containing((_RESOLVE / relative).read_text(encoding="utf-8"), marker)

    result = _run_resolve_block(block, tmp_path, tmp_path, "a" * 40, shell)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ['{"id":1}', '{"id":3}']


@_skip_no_fence_tools
def test_c1_brief_block_hands_its_item_id_to_the_commit_fence(tmp_path: Path, lineage: Lineage) -> None:
    """The brief block reads the C1 item id from the Write-tool file, consumes it, and records it for the commit fence.

    The commit fence once retyped the id as a placeholder "same value used for the brief file above"; reading the
    recorded id makes the brief, the Codex reply and the commit refer to one item by construction.
    """
    _write_impl_file(
        tmp_path,
        "action-items.jsonl",
        json.dumps({"id": 7, "full_comment_text": "x", "file": "a.py", "line": 1}) + "\n",
    )
    _write_impl_file(tmp_path, "c1-item-now.txt", "7")
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    block = _bash_block_containing(dispatch, '_C1_NOW="$IMPL_DIR/c1-item-now.txt"')

    result = _run_resolve_block(block, tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"→ C1 brief for item 7: {_bash_path(tmp_path / 'impl/c1-brief-7.md')}" in result.stdout
    assert (tmp_path / "impl/c1-item-current.txt").read_text(encoding="utf-8") == "7\n"
    assert not (tmp_path / "impl/c1-item-now.txt").exists()
    assert "Item 7: x  File: a.py  Line: 1" in (tmp_path / "impl/c1-brief-7.md").read_text(encoding="utf-8")


@_skip_no_jq
@pytest.mark.parametrize("shell", _SHELLS)
def test_group_commit_fence_reads_topic_and_ids_from_its_file(tmp_path: Path, shell: str) -> None:
    """The grouped-commit fence takes the topic and every item id from one Write-tool line and consumes it.

    With no commit or C1 record for either id, each is warned by name under the topic and the group stops before
    committing — proof both ids were split out of the line, under zsh too.
    """
    _write_impl_file(tmp_path, "group-commit-now.txt", "tests 3 7\n")
    (tmp_path / "resolve-pr-number-resolve-chain-link").write_text("42\n", encoding="utf-8", newline="\n")
    (tmp_path / "resolve-pr-ref-resolve-chain-link").write_text("#42\n", encoding="utf-8", newline="\n")
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    block = _bash_block_containing(dispatch, '_GROUP_NOW="$IMPL_DIR/group-commit-now.txt"')

    result = _run_resolve_block(block, tmp_path, tmp_path, "a" * 40, shell)

    assert result.returncode == 1
    assert "⚠ item 3 has no Phase-2 commit and no C1 record" in result.stdout
    assert "⚠ item 7 has no Phase-2 commit and no C1 record" in result.stdout
    assert "! BLOCKED — group 'tests' has no resolvable files" in result.stdout
    assert (tmp_path / "impl/group-commit-status.txt").read_text(encoding="utf-8") == "failed\n"
    assert not (tmp_path / "impl/group-commit-now.txt").exists()


@_skip_no_fence_tools
@pytest.mark.parametrize("shell", _SHELLS)
def test_straggler_confirmation_checks_every_gate_id_in_one_run(tmp_path: Path, lineage: Lineage, shell: str) -> None:
    """The gate block lists its check ids in a file; the confirmation block then confirms each id in a single run.

    Item 3 has its per-item commit; item 12 does not, and item 1 never matches inside "No.12". Before, the confirmation
    took one hand-substituted ``_ITEM_ID`` per run, a prompt per open item in unattended Run 2.
    """
    _git(lineage.repo, "commit", "-q", "--allow-empty", "-m", "fix(core): thing [resolve No.3]")
    _git(lineage.repo, "commit", "-q", "--allow-empty", "-m", "fix(core): other [resolve No.12]")
    _write_impl_file(tmp_path, "item-tasks.tsv", "1\tt1\n3\tt3\n5\tt5\n")
    _write_impl_file(tmp_path, "skipped-items.txt", "5\talready fixed\n")
    (tmp_path / "resolve-commit-mode-resolve-chain-link").write_text("each\n", encoding="utf-8", newline="\n")
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    gate = _bash_block_containing(skill, ': > "$IMPL_DIR/straggler-check-ids.txt"')
    gated = _run_resolve_block(gate, tmp_path, lineage.repo, lineage.tip, shell)

    result = _run_resolve_block(
        _bash_block_containing(skill, 'done < "$IMPL_DIR/straggler-check-ids.txt"'),
        tmp_path,
        lineage.repo,
        lineage.tip,
        shell,
    )

    assert gated.returncode == 0, gated.stdout + gated.stderr
    assert (tmp_path / "impl/straggler-check-ids.txt").read_text(encoding="utf-8") == "1\n3\n"
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NO MATCH — item 1 has no [resolve No.1] commit" in result.stdout
    assert "MATCH — item 3: " in result.stdout
    assert "[resolve No.3]" in result.stdout


@_skip_no_fence_tools
def test_spawn_base_block_pins_link_one_to_head_at_the_first_spawn(tmp_path: Path, lineage: Lineage) -> None:
    """With no pin yet, the block pins Phase 2 to the current HEAD, not to the prelude's earlier fingerprint.

    The prelude records ``resolve-base-sha`` before C1 commits its ``each``-mode items onto the PR branch; pinning to
    that older sha would hand specialists pre-C1 copies of the files C1 just edited.
    """
    _write_impl_file(tmp_path, "phase2-groups.tsv", _GROUPS_TSV)

    result = _run_unpinned_block(_spawn_base_block(tmp_path, "docs"), tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"→ spawn docs link 1 base {lineage.tip} items 2" in result.stdout
    assert (tmp_path / "impl/phase2-base-sha").read_text(encoding="utf-8") == f"{lineage.tip}\n"


@_skip_no_fence_tools
def test_spawn_base_block_keeps_the_first_pin_for_later_waves(tmp_path: Path, lineage: Lineage) -> None:
    """A later wave or a resumed run reuses the recorded pin even after HEAD moved, so all groups share one base."""
    _write_impl_file(tmp_path, "phase2-groups.tsv", _GROUPS_TSV)

    result = _run_resolve_block(_spawn_base_block(tmp_path, "docs"), tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"→ spawn docs link 1 base {lineage.base} items 2" in result.stdout


_DRIFT_LINE = 'echo "⚠ base HEAD moved during Phase 2:'


@_skip_no_fence_tools
@pytest.mark.parametrize(
    ("pin_key", "expected", "absent"),
    [
        pytest.param(
            "tip", "→ HEAD advanced before Phase 2 spawned", "⚠ base HEAD moved", id="pre-phase2-commits-are-not-drift"
        ),
        pytest.param(
            "base", "⚠ base HEAD moved during Phase 2", "→ HEAD advanced", id="move-past-the-phase2-pin-is-drift"
        ),
    ],
)
def test_fingerprint_check_reports_drift_only_past_the_phase2_pin(
    tmp_path: Path, lineage: Lineage, pin_key: str, expected: str, absent: str
) -> None:
    """Commits that landed before Phase 2 spawned are not reported as an external write; a move past the pin is.

    The prelude fingerprint predates C1's ``each``-mode commits, while the worktrees sit on the later Phase 2 pin.
    Either way the reset anchor ``resolve-base-sha`` follows HEAD, so the merge fence's guard still holds.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    pin = {"base": lineage.base, "tip": lineage.tip}[pin_key]
    _write_impl_file(tmp_path, "phase2-base-sha", f"{pin}\n")

    result = _run_unpinned_block(_bash_block_containing(dispatch, _DRIFT_LINE), tmp_path, lineage.repo, lineage.base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout
    assert absent not in result.stdout
    anchor = (tmp_path / "resolve-base-sha-resolve-chain-link").read_text(encoding="utf-8")
    assert anchor == f"{lineage.tip}\n"


def test_every_phase2_spawn_pins_its_worktree_with_an_allow_listed_command() -> None:
    """Every Phase 2 spawn pins its worktree at step 0 and reports a base mismatch instead of editing off-base code.

    Subagent worktrees branch from the default branch unless the user configured otherwise; the pin uses ``git checkout
    -B`` because the plugin allow list carries ``git checkout`` and not ``git reset``, and an un-allowed command parks a
    background agent on a permission prompt.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    allow = json.loads((_PLUGIN / ".claude-plugin/permissions-allow.json").read_text(encoding="utf-8"))
    template = dispatch[dispatch.index('Agent(subagent_type="<specialist>", isolation="worktree"') :]
    assert "BASE — step 0, before any item: git status --porcelain must print nothing" in template
    assert "git checkout -B <that branch> <base sha printed by the spawn-base block>" in template
    assert "git merge-base --is-ancestor <same sha> HEAD" in template
    assert 'with reason \\"base mismatch\\"' in template
    assert "ff-only" not in _phase2_text()
    assert "every Phase 2 worktree was pinned to `phase2-base-sha` at step 0" in dispatch
    assert "Bash(git checkout:*)" in allow
    assert not [entry for entry in allow if entry.startswith("Bash(git reset")]


@pytest.mark.skipif(
    _BASH is None or shutil.which("jq") is None, reason="The Phase 2 skipped-item fence uses Bash and jq"
)
def test_skipped_fence_reads_the_chain_link_envelope(tmp_path: Path) -> None:
    """A later chain link's skipped items are recorded from that link's own envelope, not the first link's file.

    A base mismatch returns every item of the link as skipped; those items must still reach the Step 11 report.
    """
    earlier = {"worktree": "", "commits": [], "skipped": [{"item_id": 4, "reason": "already fixed"}]}
    envelope = {"worktree": "", "commits": [], "skipped": [{"item_id": 9, "reason": "base mismatch"}]}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(earlier))
    _write_impl_file(tmp_path, "phase2-envelope-core.link2.json", json.dumps(earlier))
    _write_impl_file(tmp_path, "phase2-envelope-core.link3.json", json.dumps(envelope))
    _write_impl_file(tmp_path, "skipped-recorded.tsv", "core\t1\ncore\t2\n")

    result = _run_resolve_block(_envelope_fence(tmp_path, _SKIPPED_FENCE), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "impl/skipped-items.txt").read_text(encoding="utf-8") == "9\tbase mismatch\n"


@pytest.mark.skipif(
    _BASH is None or shutil.which("jq") is None, reason="The Phase 2 skipped-item fence uses Bash and jq"
)
def test_skipped_fence_backfills_a_link_whose_fence_never_ran(tmp_path: Path) -> None:
    """A newest envelope past the skipped record records every missing link, oldest first, instead of stopping.

    Link k's skipped fence missed (a compaction between the two fences) while spawn-base, which gates on the chain row,
    already ran link k+1. A gap guard that blocked there re-blocked every later link of the chain and no block could
    record link k; the fence now reads link k's durable envelope and backfills it before link k+1.
    """
    link1 = {"worktree": "", "commits": [], "skipped": [{"item_id": 4, "reason": "already fixed"}]}
    link2 = {"worktree": "", "commits": [], "skipped": [{"item_id": 9, "reason": "not reproducible"}]}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(link1))
    _write_impl_file(tmp_path, "phase2-envelope-core.link2.json", json.dumps(link2))

    result = _run_resolve_block(_envelope_fence(tmp_path, _SKIPPED_FENCE), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "→ group core link 1: skipped items backfilled — its own skipped-items fence never ran" in result.stdout
    skipped = (tmp_path / "impl/skipped-items.txt").read_text(encoding="utf-8")
    assert skipped == "4\talready fixed\n9\tnot reproducible\n"
    assert (tmp_path / "impl/skipped-recorded.tsv").read_text(encoding="utf-8") == "core\t1\ncore\t2\n"


@_skip_no_fence_tools
@pytest.mark.parametrize("shell", _SHELLS)
def test_chain_recovers_when_a_compaction_skips_one_skipped_items_fence(
    tmp_path: Path, lineage: Lineage, shell: str
) -> None:
    """The run's own blocks, replayed in order, recover a chain whose link-1 skipped-items fence never ran.

    Replays the r5 probe: link 1's ledger fence records its tip, a compaction cuts off its skipped-items fence,
    spawn-base still spawns link 2 from the chain row, and link 2's fences then run. Every block exits 0 and both links'
    skip reasons reach ``skipped-items.txt`` — before, link 2's skipped-items fence blocked for good and the chain's
    items reached the straggler gate without their reasons.
    """
    _write_impl_file(tmp_path, "phase2-groups.tsv", _GROUPS_TSV)
    link1 = {"worktree": "", "commits": [], "skipped": [{"item_id": 4, "reason": "already fixed"}]}
    link2 = {"worktree": "", "commits": [], "skipped": [{"item_id": 9, "reason": "not reproducible"}]}
    spawn1 = _spawn_base_block(tmp_path, "core")
    run1 = _run_resolve_block(spawn1, tmp_path, lineage.repo, lineage.base, shell)
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(link1))
    ledger1 = _run_resolve_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base, shell)
    spawn2 = _run_resolve_block(_spawn_base_block(tmp_path, "core"), tmp_path, lineage.repo, lineage.base, shell)
    _write_impl_file(tmp_path, "phase2-envelope-core.link2.json", json.dumps(link2))
    ledger2 = _run_resolve_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base, shell)

    skipped2 = _run_resolve_block(
        _envelope_fence(tmp_path, _SKIPPED_FENCE), tmp_path, lineage.repo, lineage.base, shell
    )

    assert [run.returncode for run in (run1, ledger1, spawn2, ledger2, skipped2)] == [0, 0, 0, 0, 0], skipped2.stdout
    assert f"→ spawn core link 2 base {lineage.base} items 8 9" in spawn2.stdout
    skipped = (tmp_path / "impl/skipped-items.txt").read_text(encoding="utf-8")
    assert skipped == "4\talready fixed\n9\tnot reproducible\n"


@pytest.mark.skipif(
    _BASH is None or shutil.which("jq") is None, reason="The Phase 2 skipped-item fence uses Bash and jq"
)
def test_skipped_fence_rerun_records_each_link_once(tmp_path: Path) -> None:
    """Running the skipped-item fence twice for one link records its skipped items once.

    A rerun after compaction re-ran the append and listed each skipped item twice in the Step 11 report. The fence
    consumes its tag file, so each run first writes the tag again, as a resumed orchestrator does.
    """
    envelope = {"worktree": "", "commits": [], "skipped": [{"item_id": 9, "reason": "already fixed"}]}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(envelope))

    first = _run_resolve_block(_envelope_fence(tmp_path, _SKIPPED_FENCE), tmp_path, tmp_path, "a" * 40)
    rerun = _run_resolve_block(_envelope_fence(tmp_path, _SKIPPED_FENCE), tmp_path, tmp_path, "a" * 40)

    assert (first.returncode, rerun.returncode) == (0, 0)
    assert (tmp_path / "impl/skipped-items.txt").read_text(encoding="utf-8") == "9\talready fixed\n"
    assert "skipped items already recorded" in rerun.stdout


@pytest.mark.skipif(_BASH is None, reason="The Phase 2 envelope fences are Bash")
@pytest.mark.parametrize(
    "marker", [pytest.param(_LEDGER_FENCE, id="ledger"), pytest.param(_SKIPPED_FENCE, id="skipped")]
)
def test_envelope_fences_take_the_chain_link_from_disk(tmp_path: Path, marker: str) -> None:
    """Neither fence carries a link or tag placeholder; with no envelope persisted for the tag, both stop early.

    A typed link number was a second source of truth beside the lineage file, and a wrong one re-ingested link 1; a
    substituted tag kept each fence off the blueprint manifest, so every link prompted twice in unattended Run 2.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")

    result = _run_resolve_block(_envelope_fence(tmp_path, marker), tmp_path, tmp_path, "a" * 40)

    assert '="<' not in _bash_block_containing(dispatch, marker)
    assert result.returncode == 1
    assert "phase2-envelope-core.json missing/empty" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="The Phase 2 envelope fences are Bash")
@pytest.mark.parametrize(
    "marker", [pytest.param(_LEDGER_FENCE, id="ledger"), pytest.param(_SKIPPED_FENCE, id="skipped")]
)
@pytest.mark.parametrize("tag", [pytest.param("core docs\n", id="two-tags"), pytest.param("Core!\n", id="invalid")])
def test_envelope_fences_block_unless_the_file_holds_one_valid_tag(tmp_path: Path, marker: str, tag: str) -> None:
    """Both fences block when the Write-tool file holds two tags or a malformed one, before reading any envelope.

    A merged or guessed tag would ingest the wrong group's envelope; blocking sends the orchestrator back to write the
    one tag whose envelope it just persisted.
    """
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps({"worktree": "", "commits": [], "skipped": []}))

    result = _run_resolve_block(_envelope_fence(tmp_path, marker, tag), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 1
    assert "phase2-fence-now.txt must hold exactly one group tag" in result.stdout


@pytest.mark.skipif(_BASH is None, reason="The Phase 2 envelope fences are Bash")
@pytest.mark.parametrize(
    "marker", [pytest.param(_LEDGER_FENCE, id="ledger"), pytest.param(_SKIPPED_FENCE, id="skipped")]
)
def test_envelope_fences_block_without_a_written_tag(tmp_path: Path, marker: str) -> None:
    """With no tag file, both fences block and name the Write-tool file to create, never a placeholder to edit."""
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps({"worktree": "", "commits": [], "skipped": []}))
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")

    result = _run_resolve_block(_bash_block_containing(dispatch, marker), tmp_path, tmp_path, "a" * 40)

    assert result.returncode == 1
    assert "phase2-fence-now.txt missing; create it with the Write tool" in result.stdout


@_skip_no_fence_tools
def test_skipped_fence_consumes_the_tag_the_ledger_fence_reads(tmp_path: Path, lineage: Lineage) -> None:
    """The ledger fence keeps the tag file for the skipped-items fence, which consumes it after reading.

    A tag left behind would let the next group's fences silently re-run this finished group instead of blocking until
    that group's own tag is written.
    """
    envelope = {"worktree": _bash_path(lineage.repo), "commits": [{"item_id": 7, "sha": lineage.tip}], "skipped": []}
    _write_impl_file(tmp_path, "phase2-envelope-core.json", json.dumps(envelope))
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")

    ledger = _run_resolve_block(_envelope_fence(tmp_path, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base)
    skipped = _run_resolve_block(_bash_block_containing(dispatch, _SKIPPED_FENCE), tmp_path, lineage.repo, lineage.base)
    next_group = _run_resolve_block(
        _bash_block_containing(dispatch, _LEDGER_FENCE), tmp_path, lineage.repo, lineage.base
    )

    assert (ledger.returncode, skipped.returncode) == (0, 0), ledger.stdout + skipped.stdout
    assert (tmp_path / "impl/skipped-recorded.tsv").read_text(encoding="utf-8") == "core\t1\n"
    assert (tmp_path / "impl/phase2-fence-now.txt.done").read_text(encoding="utf-8") == "core\n"
    assert next_group.returncode == 1
    assert "phase2-fence-now.txt missing" in next_group.stdout
