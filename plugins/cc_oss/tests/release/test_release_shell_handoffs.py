"""Exercise release instructions across fresh shells and host argument substitution."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _oss_shell_plugin import render_shell
from _skill_source import read_skill_source
from _text_io import bash_block_after, bash_blocks, bash_path

_PLUGIN = Path(__file__).resolve().parents[2]
_RELEASE = _PLUGIN / "skills/release"
_skip_shell_unavailable = pytest.mark.skipif(shutil.which("bash") is None, reason="Release instructions require Bash.")


def _text(relative: str = "SKILL.md") -> str:
    """Read the current release procedure."""
    return read_skill_source(_RELEASE / relative)


def _run(script: str, root: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    """Execute a shipped shell fragment with isolated session state."""
    env = {
        **os.environ,
        "TMPDIR": bash_path(root),
        "CLAUDE_CODE_SESSION_ID": "handoff",
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
        **extra,
    }
    env.pop("ARGUMENTS", None)
    return subprocess.run(
        [shutil.which("bash"), "-c", render_shell(script, Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "oss")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@_skip_shell_unavailable
@pytest.mark.parametrize(
    ("mode", "stage_name", "live_name", "success"),
    [
        pytest.param("notes", "CHANGELOG.md", "other.md", True, id="new-staged-changelog"),
        pytest.param("notes", "other.md", "CHANGELOG.md", False, id="missing-candidate-refuses-live-fallback"),
        pytest.param("prepare", "other.md", "CHANGELOG.md", True, id="prepare-keeps-live-changelog"),
    ],
)
def test_changelog_guard_recovers_edit_path(
    tmp_path: Path, mode: str, stage_name: str, live_name: str, success: bool
) -> None:
    """A fresh notes shell validates the candidate, while prepare validates the live path."""
    stage = tmp_path / "candidate"
    stage.mkdir()
    (stage / stage_name).write_text("new\n", encoding="utf-8", newline="\n")
    (tmp_path / live_name).write_text("live\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-changelog-file-handoff").write_text("CHANGELOG.md\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-stage-handoff").write_text(bash_path(stage) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-handoff").write_text(mode + "\n", encoding="utf-8", newline="\n")

    block = bash_block_after(_text(), "persist its exact path for fresh-shell append validation")
    completed = _run(block, tmp_path)

    assert (completed.returncode == 0) is success, completed.stderr
    assert (tmp_path / "release-changelog-file-handoff").read_text(encoding="utf-8") == "CHANGELOG.md\n"


@_skip_shell_unavailable
@pytest.mark.parametrize(
    ("arguments", "expected_rest", "sentinel", "expected_value"),
    [
        pytest.param("prepare v1.3.0 --summary", "v1.3.0", "prepare-version", "v1.3.0", id="prepare-version"),
        pytest.param("prepare --summary v1.3.0", "v1.3.0", "prepare-version", "v1.3.0", id="flags-before-version"),
        pytest.param("demo v1..v2 --migration", "v1..v2", "demo-range", "v1..v2", id="demo-range"),
        pytest.param("audit v1.3.0 --changelog", "v1.3.0", "audit-target", "v1.3.0", id="audit-target"),
        pytest.param("audit", "", "audit-target", "", id="mode-without-tail"),
    ],
)
def test_host_substitution_preserves_mode_tail(
    tmp_path: Path, arguments: str, expected_rest: str, sentinel: str, expected_value: str
) -> None:
    """Literal host substitution reaches downstream modes without an exported ARGUMENTS variable."""
    block = next(block for block in bash_blocks(_text()) if "parse-skill-flags.py" in block)
    completed = _run(block.replace("$ARGUMENTS", arguments), tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "release-rest-handoff").read_text(encoding="utf-8").strip() == expected_rest
    setup = tmp_path / "release-setup-handoff"
    setup.mkdir()
    (setup / "LAST_TAG").write_text("v1\n", encoding="utf-8", newline="\n")
    (setup / "SOURCE_TAG_REF").write_text("", encoding="utf-8", newline="\n")
    mode = arguments.split()[0]
    consumer = bash_blocks(_text(f"modes/{mode}.md"))[0]
    downstream = _run(consumer, tmp_path)
    assert downstream.returncode == 0, downstream.stderr
    assert (tmp_path / f"release-{sentinel}-handoff").read_text(encoding="utf-8").strip() == expected_value


@_skip_shell_unavailable
@pytest.mark.parametrize(
    ("ledger", "total", "breaking"),
    [
        pytest.param("NET-STATE-ADD: ⚠️ Breaking Changes: old to new\n", 1, 1, id="reclassified-breaking"),
        pytest.param("REMOVED: ⚠️ Breaking Changes: missing API\n", 1, 1, id="removed-breaking"),
        pytest.param("REMOVED: 🚀 Added: missing API\n", 1, 0, id="nonbreaking-removal"),
        pytest.param("CROSS_CYCLE_MATCH: old claim\n", 0, 0, id="cross-cycle-not-a-waiver"),
        pytest.param("", 0, 0, id="no-waivers-clears-stale-count"),
    ],
)
def test_inline_truth_check_persists_waiver_counts(tmp_path: Path, ledger: str, total: int, breaking: int) -> None:
    """Inline breaking reclassifications produce the approval count before artifacts can be written."""
    (tmp_path / "ledger").write_text(ledger, encoding="utf-8", newline="\n")
    (tmp_path / "release-waived-handoff").write_text("ledger\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-unconfirmed-handoff").write_text("9\n", encoding="utf-8", newline="\n")
    lines = _text().splitlines()
    section = "\n".join(lines[lines.index("## Truth check") : lines.index("## Breaking-change classification")])
    blocks = bash_blocks(section)

    completed = _run(blocks[0] if blocks else "", tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "release-unconfirmed-handoff").read_text(encoding="utf-8").strip() == str(total)
    assert (tmp_path / "release-unconfirmed-breaking-handoff").read_text(encoding="utf-8").strip() == str(breaking)


@_skip_shell_unavailable
def test_inline_truth_check_rejects_missing_ledger(tmp_path: Path) -> None:
    """Absent inline evidence cannot silently become a zero-waiver approval decision."""
    lines = _text().splitlines()
    section = "\n".join(lines[lines.index("## Truth check") : lines.index("## Breaking-change classification")])
    blocks = bash_blocks(section)

    completed = _run(blocks[0] if blocks else "", tmp_path)

    assert completed.returncode != 0
    assert not (tmp_path / "release-unconfirmed-breaking-handoff").exists()


@_skip_shell_unavailable
@pytest.mark.parametrize(
    ("ledger", "total", "breaking", "stale"),
    [
        pytest.param("NET-STATE-ADD: ⚠️ Breaking Changes: rename API\n", 1, 1, 0, id="delegated-breaking-needs-review"),
        pytest.param("REMOVED: 🚀 Added: absent API\n", 1, 0, 2, id="nonbreaking-clears-stale-breaking-count"),
        pytest.param("", 0, 0, 1, id="empty-ledger-clears-stale-breaking-count"),
    ],
)
def test_delegated_truth_check_refreshes_breaking_count(
    tmp_path: Path, ledger: str, total: int, breaking: int, stale: int
) -> None:
    """Reconciled delegated evidence replaces the count left by an earlier inline invocation."""
    (tmp_path / "ledger").write_text(ledger, encoding="utf-8", newline="\n")
    (tmp_path / "release-waived-handoff").write_text("ledger\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-envelope-unconfirmed-handoff").write_text(str(total) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-envelope-unconfirmed-breaking-handoff").write_text(
        str(breaking) + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "release-unconfirmed-breaking-handoff").write_text(str(stale) + "\n", encoding="utf-8", newline="\n")
    reconcile = next(block for block in bash_blocks(_text("modes/delegation.md")) if "LEDGER_BREAKING=" in block)

    completed = _run(reconcile, tmp_path, UNCONFIRMED="", UNCONFIRMED_BREAKING="")

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "release-unconfirmed-handoff").read_text(encoding="utf-8") == f"{total}\n"
    assert (tmp_path / "release-unconfirmed-breaking-handoff").read_text(encoding="utf-8") == f"{breaking}\n"


@_skip_shell_unavailable
@pytest.mark.parametrize("breaking", [0, 2])
def test_breaking_gate_reloads_verified_count(tmp_path: Path, breaking: int) -> None:
    """A fresh approval shell reads the verified count instead of trusting an inherited value."""
    (tmp_path / "release-unconfirmed-breaking-handoff").write_text(str(breaking) + "\n", encoding="utf-8", newline="\n")
    lines = _text().splitlines()
    gate = "\n".join(lines[lines.index("## Delegation strategy") : lines.index("## Mode Detection")])
    block = next((block for block in bash_blocks(gate) if "read -r UNCONFIRMED_BREAKING" in block), "")

    completed = _run(block + '\nprintf "breaking=%s\\n" "$UNCONFIRMED_BREAKING"', tmp_path, UNCONFIRMED_BREAKING="9")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"breaking={breaking}\n"


@_skip_shell_unavailable
def test_diff_stat_accepts_a_two_ref_range(tmp_path: Path, discovery_repo: Path) -> None:
    """The shipped diff command accepts a real commit range as one Git revision argument."""
    command = next(line for line in _text().splitlines() if line.startswith("git diff --stat"))
    repo = discovery_repo
    # Own two-commit history: a shallow CI checkout has no HEAD~1 to diff against.
    _commit_file(repo, "one\n")
    _commit_file(repo, "two\n")
    work = tmp_path / "run"
    work.mkdir()
    completed = _run('cd "$RELEASE_REPO"\nRANGE=HEAD~1..HEAD\n' + command, work, RELEASE_REPO=bash_path(repo))

    assert completed.returncode == 0, completed.stderr
    assert "file" in completed.stdout


def _commit_file(repo: Path, body: str) -> None:
    """Commit a tracked file holding *body*, so the commit carries a real change."""
    (repo / "tracked.txt").write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True, timeout=10)
    subprocess.run(
        ["git", "-c", "core.hooksPath=", "commit", "-q", "-m", "range fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=10,
    )


def _commit_at(repo: Path, date: str) -> None:
    """Add a scratch commit whose author and committer timestamps share the supplied offset."""
    subprocess.run(
        ["git", "-c", "core.hooksPath=", "commit", "-q", "--allow-empty", "-m", "fixture change"],
        cwd=repo,
        env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        check=True,
        capture_output=True,
        timeout=10,
    )


@pytest.fixture
def discovery_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create isolated Git history with controlled identities and no user configuration or hooks."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Release Fixture")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Release Fixture")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True, timeout=10)
    _commit_at(tmp_path, "2025-12-31T00:00:00+00:00")
    return tmp_path


@_skip_shell_unavailable
@pytest.mark.parametrize(
    ("recovery_fails", "result_count", "has_gap", "commit_date", "search_date"),
    [
        pytest.param(True, 500, True, "2026-01-01T00:00:00+00:00", "2026-01-01", id="failed-recovery-keeps-context"),
        pytest.param(False, 500, True, "2026-01-01T00:00:00+00:00", "2026-01-01", id="bounded-recovery-still-capped"),
        pytest.param(
            False, 2, False, "2026-01-01T00:00:00+00:00", "2026-01-01", id="bounded-recovery-replaces-context"
        ),
        pytest.param(False, 2, False, "2026-01-11T01:00:00+09:00", "2026-01-10", id="positive-offset-uses-utc-date"),
    ],
)
def test_capped_discovery_does_not_fetch_all_closed_prs(
    discovery_repo: Path, recovery_fails: bool, result_count: int, has_gap: bool, commit_date: str, search_date: str
) -> None:
    """Bounded recovery retains contextual results and reports its gap on failure or another cap."""
    _commit_at(discovery_repo, "2026-04-02T00:00:00+00:00")
    _commit_at(discovery_repo, commit_date)
    _commit_at(discovery_repo, "2026-03-01T00:00:00+00:00")
    block = next(block for block in bash_blocks(_text()) if "PR_FIELDS=" in block)
    block = block[block.index('PR_FIELDS="') :]
    boundaries = """
gh() {
    [ "$1" != api ] || { echo "unexpected exhaustive request" >&2; return 1; }
    case "$*" in *--search*)
        printf '%s\\n' "$@" > recovery-argv
        [ "$RECOVERY_FAILS" != True ] || return 1
        printf '%s\\n' "$RECOVERY_RESULT"
        return 0
    ;; esac
    printf '%s\\n' "$PR_RESULT"
}
"""
    records = [{"number": number} for number in range(500)]

    completed = _run(
        boundaries + block,
        discovery_repo,
        TRUNK="main",
        RANGE="HEAD~3..HEAD",
        PR_RESULT=json.dumps(records),
        RECOVERY_RESULT=json.dumps(records[:result_count]),
        RECOVERY_FAILS=str(recovery_fails),
    )

    assert completed.returncode == 0, completed.stderr
    assert ("coverage gap" in completed.stdout.lower() + completed.stderr.lower()) is has_gap
    assert json.loads(completed.stdout.splitlines()[-1]) == records[:result_count]
    assert "unexpected exhaustive request" not in completed.stderr
    recovery_args = (discovery_repo / "recovery-argv").read_text(encoding="utf-8").splitlines()
    assert recovery_args == [
        "pr",
        "list",
        "--state",
        "merged",
        "--limit",
        "500",
        "--json",
        "number,title,body,labels,mergedAt,author",
        "--search",
        f"merged:>={search_date}",
        "--base",
        "main",
    ]


@_skip_shell_unavailable
@pytest.mark.parametrize("has_gather", [True, False])
def test_adversarial_context_is_resolved_before_spawn(tmp_path: Path, has_gather: bool) -> None:
    """The reviewer receives literal gather-file guidance or a literal range fallback."""
    setup = tmp_path / "release-setup-handoff"
    setup.mkdir()
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "DATE").write_text("2026-11-11\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-range-handoff").write_text("v1..v2\n", encoding="utf-8", newline="\n")
    (tmp_path / ".temp").mkdir()
    gather = tmp_path / ".temp/release-gather-main-2026-11-11.md"
    gather.write_text("classified commits\n", encoding="utf-8")
    gather.rename(gather if has_gather else gather.with_name("unrelated.md"))
    mode = _text("modes/adversarial-review.md")
    block = bash_blocks(mode)[0]

    boundaries = "git() { printf 'topic/branch\\n'; }\ndate() { printf '2026-11-12\\n'; }\n"
    completed = _run(boundaries + block, tmp_path, GATHER_FILE="", RANGE="")

    assert completed.returncode == 0, completed.stderr
    values = dict(line.split("=", 1) for line in completed.stdout.splitlines())
    assert values["RANGE"] == "v1..v2"
    assert values["ADVERSARIAL_DIR"] == ".temp/release-adversarial-topic-branch-2026-11-12"
    assert (tmp_path / values["ADVERSARIAL_DIR"]).is_dir()
    assert (
        ".temp/release-gather-main-2026-11-11.md" if has_gather else "git log v1..v2 --no-merges --oneline"
    ) in values["GATHER_CONTEXT"]
    prompt = mode.split("<gather context>\n", 1)[1].split("\n</gather context>", 1)[0]
    assert prompt == "<GATHER_CONTEXT>"
    assert "$(" not in prompt


@_skip_shell_unavailable
def test_adversarial_review_stops_without_a_range(tmp_path: Path) -> None:
    """A missing range cannot expand the completeness check to the entire repository history."""
    completed = _run(bash_blocks(_text("modes/adversarial-review.md"))[0], tmp_path, RANGE="", GATHER_FILE="")

    assert completed.returncode != 0
    assert "adversarial release range unresolved" in completed.stderr
