"""Grouped commit mode keeps one commit per topic group even when groups change the same file."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import resolve_group_replay as replay
from _oss_shell_plugin import render_shell
from _text_io import bash_blocks, bash_path, write_lf
from append_resolution import grouped_commits
from resolve_commit_hooks import CallBudget, commit_with_hooks, inspect_repository_hooks

_BIN = Path(__file__).resolve().parents[2] / "bin"
_REPLAY = _BIN / "resolve_group_replay.py"
_MERGE = _BIN / "merge_specialist_batch.py"
_PHASE3 = Path(__file__).resolve().parents[2] / "skills/resolve/modes/dispatch-phase3.md"
_IDENTITY = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
_ENV = {**{key: value for key, value in os.environ.items() if key != "RESOLVE_APPROVED_HOOKS"}, **_IDENTITY}
#: Harness ceiling of one Bash call, in milliseconds: a larger fence timeout is cut to it.
_HARNESS_MAX_MS = 600_000
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("git") is None, reason="the replay drives real git plumbing"),
]

#: Lines of the shared module; items edit lines far enough apart that each change applies on its own.
_SHARED_LINES = [f"value_{n} = {n}\n" for n in range(40)]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run one git command in ``repo`` and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=env or _ENV, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _edit_line(repo: Path, name: str, number: int, text: str) -> None:
    """Replace one line of a file in the working tree."""
    path = repo / name
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[number] = text
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


def _commit_item(repo: Path, item: int, edits: dict[str, tuple[int, str]], new_files: dict[str, str]) -> str:
    """Commit one implemented item on the current branch and return its commit id."""
    for name, (number, text) in edits.items():
        _edit_line(repo, name, number, text)
    for name, text in new_files.items():
        (repo / name).write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "--", *edits, *new_files)
    _git(repo, "commit", "-q", "-m", f"fix(core): item {item}")
    return _git(repo, "rev-parse", "HEAD")


def _build_run(
    tmp_path: Path,
    shared: str = "core.py",
    item_two_tests: bool = True,
    *,
    adjacent: bool = False,
    later_line: int = 30,
) -> tuple[Path, Path, str]:
    """Build a repository after a grouped-mode merge-back: three item commits collapsed into one staged diff.

    Items 1 and 3 were built in one worktree, item 2 in another; items 1, 2 and 3 all change ``shared`` at different
    lines, and item 2 also changes the test module unless ``item_two_tests`` is false.
    """
    repo, impl_dir = tmp_path / "repo", tmp_path / "impl"
    repo.mkdir()
    impl_dir.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / shared).write_text("".join(_SHARED_LINES), encoding="utf-8", newline="\n")
    (repo / "test_core.py").write_text("def test_core():\n    assert True\n", encoding="utf-8", newline="\n")
    _git(repo, "add", shared, "test_core.py")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "worktree-a")
    one = _commit_item(repo, 1, {shared: (2, "value_2 = 'two'\n")}, {"extra.py": "EXTRA = 1\n"})
    three = _commit_item(repo, 3, {shared: (6, "value_6 = 'six'\n")}, {})
    if not adjacent:
        _git(repo, "switch", "-q", "-c", "worktree-b", base)
    line = 7 if adjacent else later_line
    two_edits = {shared: (line, "value_30 = 'thirty'\n" if line == 30 else f"value_{line} = 'later'\n")}
    if item_two_tests:
        two_edits["test_core.py"] = (1, "    assert 1\n")
    two = _commit_item(repo, 2, two_edits, {})
    _git(repo, "switch", "-q", "main")
    rows = [{"item_id": 1, "sha": one, "group": "a"}, {"item_id": 3, "sha": three, "group": "a"}]
    rows.append({"item_id": 2, "sha": two, "group": "b"})
    (impl_dir / "phase2-commits.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    plan = impl_dir / "merge-plan.json"
    plan.write_text(json.dumps(rows if adjacent else [rows[0], rows[2], rows[1]]), encoding="utf-8")
    merged = subprocess.run(
        [sys.executable, str(_MERGE), "--plan", str(plan), "--commit-mode", "grouped", f"--base-sha={base}"],
        cwd=repo,
        env=_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert _git(repo, "rev-parse", "HEAD") == base, "the merge-back collapses every item into one staged diff"
    return repo, impl_dir, _git(repo, "write-tree")


@pytest.fixture
def grouped_run(tmp_path: Path) -> tuple[Path, Path, str]:
    """Return ``(repo, impl_dir, collapsed tree)`` of a grouped run whose groups share ``core.py``."""
    return _build_run(tmp_path)


def _summaries(impl_dir: Path, topic: str, items: str) -> Path:
    """Write the per-item subjects the grouped commit fence passes and return the file."""
    summaries = impl_dir / f"summaries-{topic}.txt"
    summaries.write_text("".join(f"fix(core): item {item}\n" for item in items.split()), encoding="utf-8")
    return summaries


def _commit_group(
    repo: Path, impl_dir: Path, topic: str, items: str, env: dict[str, str] | None = None, *, attempt_id: str = ""
) -> subprocess.CompletedProcess:
    """Run the replay helper for one group, as the grouped commit fence does."""
    summaries = _summaries(impl_dir, topic, items)
    args = ["--impl-dir", str(impl_dir), "--topic", topic, "--summaries-file", str(summaries), "--pr", "#7"]
    return subprocess.run(
        [sys.executable, str(_REPLAY), *args, "--items", items, "--attempt-id", attempt_id],
        cwd=repo,
        env=env or _ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _commit_group_in_process(
    repo: Path, impl_dir: Path, topic: str, items: str, monkeypatch: pytest.MonkeyPatch, version: str
) -> int:
    """Run one group through the helper's own functions, with the git version the run reports as ``version``."""
    monkeypatch.chdir(repo)
    for key, value in _IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RESOLVE_APPROVED_HOOKS", raising=False)
    summaries = tuple(_summaries(impl_dir, topic, items).read_text(encoding="utf-8").splitlines())
    git = replay.Git(
        shutil.which("git") or "git", CallBudget.starting_now(60), version, replay.supports_three_way(version)
    )
    request = replay.GroupRequest(impl_dir.resolve(), topic, summaries, "#7", tuple(items.split()), False)
    return replay.commit_group(git, request)


def _changed_lines(repo: Path, revision: str, name: str) -> list[str]:
    """Return the added lines one commit made to one file."""
    diff = _git(repo, "show", "--format=", "--unified=0", revision, "--", name)
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def _make_item_one_stale(repo: Path, impl_dir: Path, tmp_path: Path) -> None:
    """Point item 1's record at a commit whose preimage no longer matches the head.

    The recorded commit edits a line that, by the time of the merge-back, no longer reads as its preimage — the shape a
    conflict resolved by hand during the merge-back leaves behind.
    """
    rows = [json.loads(line) for line in (impl_dir / "phase2-commits.jsonl").read_text(encoding="utf-8").splitlines()]
    side = tmp_path / "side"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(side), "HEAD")
    _commit_item(side, 8, {"core.py": (2, "value_2 = 'draft'\n")}, {})
    rows[0]["sha"] = _commit_item(side, 1, {"core.py": (2, "value_2 = 'two'\n")}, {"extra.py": "EXTRA = 1\n"})
    (impl_dir / "phase2-commits.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _stage_direct_items(repo: Path, impl_dir: Path, rows: dict[str, tuple[str, int, str]]) -> None:
    """Record directly implemented items (``{id: (file, line, text)}``) and stage their edits as the merge-back does."""
    for item, (name, number, text) in rows.items():
        _edit_line(repo, name, number, text)
        with (impl_dir / "c1-item-files.tsv").open("a", encoding="utf-8", newline="\n") as record:
            record.write(f"{item}\t{name}\n")
    _git(repo, "add", "--", *{name for name, _, _ in rows.values()})


def test_shared_file_is_split_between_group_commits(grouped_run: tuple[Path, Path, str]) -> None:
    """Each group commits only its own items' hunks of a file both groups changed, one commit per group."""
    repo, impl_dir, collapsed = grouped_run
    base = _git(repo, "rev-parse", "HEAD")
    logic = _commit_group(repo, impl_dir, "logic", "1 3")
    assert logic.returncode == 0, logic.stdout + logic.stderr
    tests = _commit_group(repo, impl_dir, "tests", "2")
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "2"
    assert _changed_lines(repo, "HEAD~1", "core.py") == ["value_2 = 'two'", "value_6 = 'six'"]
    assert _changed_lines(repo, "HEAD", "core.py") == ["value_30 = 'thirty'"]
    assert _git(repo, "show", "--format=", "--name-only", "HEAD~1").split() == ["core.py", "extra.py"]
    assert _git(repo, "show", "--format=", "--name-only", "HEAD").split() == ["core.py", "test_core.py"]
    assert "[resolve group attempt] PR #7 — items 1 3" in _git(repo, "log", "-1", "--format=%B", "HEAD~1")
    assert "[resolve group attempt] PR #7 — items 2" in _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert _git(repo, "rev-parse", "HEAD^{tree}") == collapsed, "the groups together equal the collapsed diff"
    assert _git(repo, "diff", "--cached", "--name-only") == "", "nothing stays staged after the last group"
    assert "GROUP_REPLAY_FALLBACK" not in logic.stdout + tests.stdout


def test_later_groups_stay_staged_between_group_commits(grouped_run: tuple[Path, Path, str]) -> None:
    """After the first group commits, the other group's changes are still staged for its own commit."""
    repo, impl_dir, _ = grouped_run
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    staged = _git(repo, "diff", "--cached", "--unified=0", "--", "core.py")
    assert "+value_30 = 'thirty'" in staged
    assert "value_2 = 'two'" not in staged
    assert _git(repo, "diff", "--name-only") == "", "the working tree is never rewritten"


def test_change_that_no_longer_applies_takes_the_file_whole(
    grouped_run: tuple[Path, Path, str], tmp_path: Path
) -> None:
    """A recorded change that does not apply to the head is replaced by the whole target file, and named.

    The whole file also carries the later group's change to it, so the line and the commit message name that item.
    """
    repo, impl_dir, collapsed = grouped_run
    _make_item_one_stale(repo, impl_dir, tmp_path)
    logic = _commit_group(repo, impl_dir, "logic", "1 3")
    assert logic.returncode == 0, logic.stdout + logic.stderr
    assert "GROUP_REPLAY_FALLBACK=core.py (item 1" in logic.stdout
    assert "also carries items 2 of a later group" in logic.stdout
    assert "core.py (item 1, group logic" in (impl_dir / "group-replay-fallbacks.txt").read_text(encoding="utf-8")
    body = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "Also carries later-group changes in files taken whole: core.py (items 2)" in body
    assert body.index("Also carries") < body.index("\n---\n"), "the note stays above the trailers"
    tests = _commit_group(repo, impl_dir, "tests", "2")
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert _git(repo, "show", "--format=", "--name-only", "HEAD") == "test_core.py"
    assert _git(repo, "rev-parse", "HEAD^{tree}") == collapsed
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_group_whose_changes_landed_whole_earlier_reports_success(tmp_path: Path) -> None:
    """A later group whose only change rode in an earlier group's whole file closes as landed, never as failed."""
    repo, impl_dir, collapsed = _build_run(tmp_path, item_two_tests=False)
    _make_item_one_stale(repo, impl_dir, tmp_path)
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    head = _git(repo, "rev-parse", "HEAD")
    tests = _commit_group(repo, impl_dir, "tests", "2")
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert "items 2 already landed in the commit of group(s) logic" in tests.stdout
    assert _git(repo, "rev-parse", "HEAD") == head, "no empty commit is made for the landed group"
    assert "2" in (impl_dir / "group-replay-committed.txt").read_text(encoding="utf-8").split()
    assert _git(repo, "rev-parse", "HEAD^{tree}") == collapsed
    assert _git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize("first", [pytest.param("4", id="docs-group-first"), pytest.param("5", id="style-group-first")])
def test_direct_items_sharing_a_file_commit_in_either_group_order(
    grouped_run: tuple[Path, Path, str], first: str
) -> None:
    """Two directly implemented items of different groups sharing a file: the first group lands both, the second closes.

    Neither has a commit to replay, so their changes cannot be separated; the first group's commit carries both and
    names the other, and the second group reports it landed. The groups that come after still commit normally.
    """
    repo, impl_dir, _ = grouped_run
    (repo / "notes.md").write_text("".join(f"note {n}\n" for n in range(20)), encoding="utf-8", newline="\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-q", "-m", "notes", "--", "notes.md")  # pathspec: the collapsed diff stays staged
    _stage_direct_items(repo, impl_dir, {"4": ("notes.md", 2, "note two\n"), "5": ("notes.md", 15, "note fifteen\n")})
    expected = _git(repo, "write-tree")
    second = "5" if first == "4" else "4"
    head = _git(repo, "rev-parse", "HEAD")
    landing = _commit_group(repo, impl_dir, f"g{first}", first)
    assert landing.returncode == 0, landing.stdout + landing.stderr
    assert f"also carries items {second} of a later group" in landing.stdout
    assert f"item {second} of a later group is also implemented directly" in landing.stdout
    landed = _commit_group(repo, impl_dir, f"g{second}", second)
    assert landed.returncode == 0, landed.stdout + landed.stderr
    assert f"already landed in the commit of group(s) g{first}" in landed.stdout
    assert _git(repo, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    assert _commit_group(repo, impl_dir, "tests", "2").returncode == 0
    assert _git(repo, "rev-parse", "HEAD^{tree}") == expected
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_direct_item_committed_first_keeps_only_its_own_change(grouped_run: tuple[Path, Path, str]) -> None:
    """A directly implemented item's group, committed before the groups sharing its file, carries only its hunk.

    The later groups' recorded changes are reverse-applied out of the working file, so each later group still makes its
    own commit.
    """
    repo, impl_dir, _ = grouped_run
    _stage_direct_items(repo, impl_dir, {"4": ("core.py", 10, "value_10 = 'ten'\n")})
    expected = _git(repo, "write-tree")
    docs = _commit_group(repo, impl_dir, "docs", "4")
    assert docs.returncode == 0, docs.stdout + docs.stderr
    assert "GROUP_REPLAY_FALLBACK" not in docs.stdout
    assert _changed_lines(repo, "HEAD", "core.py") == ["value_10 = 'ten'"]
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    assert _changed_lines(repo, "HEAD", "core.py") == ["value_2 = 'two'", "value_6 = 'six'"]
    assert _commit_group(repo, impl_dir, "tests", "2").returncode == 0
    assert _changed_lines(repo, "HEAD", "core.py") == ["value_30 = 'thirty'"]
    assert _git(repo, "rev-parse", "HEAD^{tree}") == expected
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_direct_item_overlapping_a_later_change_takes_the_file_whole(grouped_run: tuple[Path, Path, str]) -> None:
    """A later change the direct item rewrote cannot be undone from the working file, so the file goes in whole.

    The partial separation is discarded, the later items are named, and their groups still close: one commits only its
    other file, the other has nothing left on the shared file but its test module.
    """
    repo, impl_dir, _ = grouped_run
    _stage_direct_items(repo, impl_dir, {"4": ("core.py", 30, "value_30 = 'rewritten'\n")})
    expected = _git(repo, "write-tree")
    docs = _commit_group(repo, impl_dir, "docs", "4")
    assert docs.returncode == 0, docs.stdout + docs.stderr
    assert "item 2's recorded change could not be separated from the working file" in docs.stdout
    assert "also carries items 1 2 3 of a later group" in docs.stdout
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    assert _git(repo, "show", "--format=", "--name-only", "HEAD") == "extra.py"
    assert _commit_group(repo, impl_dir, "tests", "2").returncode == 0
    assert _git(repo, "show", "--format=", "--name-only", "HEAD") == "test_core.py"
    assert _git(repo, "rev-parse", "HEAD^{tree}") == expected
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_file_edited_after_its_item_commit_lands_whole(grouped_run: tuple[Path, Path, str]) -> None:
    """A file only one group touches lands with its merge-back content, so a later edit never stays staged.

    The merge-back can change an item's file after the item was committed (a conflict resolved by hand); replaying the
    original commit would then leave the difference staged and stop the run at the empty-index check.
    """
    repo, impl_dir, _ = grouped_run
    (repo / "extra.py").write_text("EXTRA = 2  # resolved during merge-back\n", encoding="utf-8")
    _git(repo, "add", "extra.py")
    target = _git(repo, "write-tree")
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    assert _commit_group(repo, impl_dir, "tests", "2").returncode == 0
    assert "EXTRA = 2" in _git(repo, "show", "HEAD~1:extra.py")
    assert _git(repo, "rev-parse", "HEAD^{tree}") == target
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_replaying_a_settled_group_reports_the_existing_commit(grouped_run: tuple[Path, Path, str]) -> None:
    """A settled receipt reports its original commit as successful without creating a second commit."""
    repo, impl_dir, _ = grouped_run
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    head = _git(repo, "rev-parse", "HEAD")
    again = _commit_group(repo, impl_dir, "logic", "1 3")
    assert again.returncode == 0, again.stdout + again.stderr
    assert f"already committed {head[:12]}" in again.stdout
    assert _git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("path", [" path.py", "path.py ", " path.py ", " ", "path\r.py", "path.py\r", "path\u2028.py"])
def test_direct_file_ledger_preserves_path_bytes(tmp_path: Path, path: str) -> None:
    """The reader removes the tab and LF separators while keeping every character of the Git path."""
    (tmp_path / "c1-item-files.tsv").write_bytes(f"1\t{path}\n2\tplain.py\n".encode())

    assert replay.load_direct_files(tmp_path) == {"1": [path], "2": ["plain.py"]}


def test_index_changed_between_groups_is_refused(grouped_run: tuple[Path, Path, str]) -> None:
    """A staged change made outside the replay stops the next group instead of being committed into it."""
    repo, impl_dir, _ = grouped_run
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "unrelated.py").write_text("X = 1\n", encoding="utf-8")
    _git(repo, "add", "unrelated.py")
    refused = _commit_group(repo, impl_dir, "tests", "2")
    assert refused.returncode == 1
    assert "git read-tree" in refused.stdout
    assert _git(repo, "rev-parse", "HEAD") == head


def test_directly_implemented_item_takes_its_files_whole(grouped_run: tuple[Path, Path, str]) -> None:
    """An item with no implementation commit contributes its recorded files whole from the collapsed diff."""
    repo, impl_dir, collapsed = grouped_run
    (repo / "notes.md").write_text("notes\n", encoding="utf-8")
    _git(repo, "add", "notes.md")
    write_lf(impl_dir / "c1-item-files.tsv", "4\tnotes.md\n")
    assert _commit_group(repo, impl_dir, "logic", "1 3").returncode == 0
    assert _commit_group(repo, impl_dir, "tests", "2").returncode == 0
    docs = _commit_group(repo, impl_dir, "docs", "4")
    assert docs.returncode == 0, docs.stdout + docs.stderr
    assert _git(repo, "show", "--format=", "--name-only", "HEAD") == "notes.md"
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert collapsed != _git(repo, "rev-parse", "HEAD^{tree}"), "the direct item's file was staged after the collapse"


def test_git_without_index_three_way_still_splits(
    grouped_run: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git older than 2.32 cannot merge three-way in the index; changes that apply cleanly still split by hunk."""
    repo, impl_dir, collapsed = grouped_run
    assert _commit_group_in_process(repo, impl_dir, "logic", "1 3", monkeypatch, "git version 2.31.8") == 0
    assert _commit_group_in_process(repo, impl_dir, "tests", "2", monkeypatch, "git version 2.31.8") == 0
    assert _changed_lines(repo, "HEAD~1", "core.py") == ["value_2 = 'two'", "value_6 = 'six'"]
    assert _changed_lines(repo, "HEAD", "core.py") == ["value_30 = 'thirty'"]
    assert _git(repo, "rev-parse", "HEAD^{tree}") == collapsed


@pytest.mark.parametrize(
    ("version", "reason"),
    [
        pytest.param("git version 2.31.8", "needs git 2.32 or later (git version 2.31.8 found)", id="older-git"),
        pytest.param("", "needs git 2.32 or later (of unknown version found)", id="unrecognised-git"),
        pytest.param("git version 2.54.0", "recorded change no longer applies", id="three-way-git"),
    ],
)
def test_fallback_reason_names_the_git_version_when_three_way_is_unavailable(
    grouped_run: tuple[Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    version: str,
    reason: str,
) -> None:
    """A file taken whole says why: a change that no longer applies, or a three-way merge this git cannot run."""
    repo, impl_dir, _ = grouped_run
    _make_item_one_stale(repo, impl_dir, tmp_path)
    assert _commit_group_in_process(repo, impl_dir, "logic", "1 3", monkeypatch, version) == 0
    [line] = [line for line in capsys.readouterr().out.splitlines() if line.startswith("GROUP_REPLAY_FALLBACK=")]
    assert line.startswith("GROUP_REPLAY_FALLBACK=core.py (item 1, group logic: ")
    assert reason in line


def test_fence_timeout_exceeds_the_replay_worst_case() -> None:
    """The grouped commit fence outlasts the helper, so the Bash call is never killed with a group tree staged."""
    lines = _PHASE3.read_text(encoding="utf-8").splitlines()
    [call] = [line for line in lines if 'bin/resolve_group_replay.py" "${_BUILD_ARGS[@]}"' in line]
    match = re.search(r"# timeout: (\d+)", call)
    assert match, "the replay call carries its own Bash timeout"
    assert replay.WORST_CASE_SECONDS * 1000 < min(int(match.group(1)), _HARNESS_MAX_MS)
    assert "--files" not in call, "the helper derives the group's paths itself, byte for byte"


def test_commit_that_outlives_the_budget_restores_the_index(
    grouped_run: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit stopped by the time budget leaves no commit and the real index back on the target."""
    repo, impl_dir, collapsed = grouped_run

    def _spent(*_args: object) -> int:
        """Stand in for a hook run that outlives the budget."""
        raise subprocess.TimeoutExpired("git", 0)

    monkeypatch.setattr(replay, "commit_with_hooks", _spent)
    head = _git(repo, "rev-parse", "HEAD")
    assert _commit_group_in_process(repo, impl_dir, "logic", "1 3", monkeypatch, "git version 2.54.0") == 1
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "write-tree") == collapsed


def _approve_hook(
    repo: Path, hooks: Path, payload: str, monkeypatch: pytest.MonkeyPatch, *, name: str = "pre-commit"
) -> str:
    """Install ``payload`` as a repository hook and return the digest that approves it."""
    hooks.mkdir(exist_ok=True)
    script = hooks / f"{name}-payload.py"
    script.write_text(payload, encoding="utf-8")
    hook = hooks / name
    command = shlex.join([sys.executable.replace("\\", "/"), script.as_posix()])
    hook.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    _git(repo, "config", "core.hooksPath", hooks.as_posix())
    monkeypatch.chdir(repo)
    for key, value in _IDENTITY.items():
        monkeypatch.setenv(key, value)
    return str(inspect_repository_hooks("git")["sha256"])


def _close_out_committed_group(repo: Path, impl_dir: Path, bash: str, fence_status: str | None) -> list[str]:
    """Run the shipped close-out with a spent group and one later item still pending."""
    scratch = impl_dir.parent
    write_lf(scratch / "resolve-impl-dir-test-group", f"{bash_path(impl_dir)}\n")
    write_lf(impl_dir / "group-commit-now.txt.done", "logic 1 3\n")
    if fence_status is not None:
        write_lf(impl_dir / "group-commit-status.txt", fence_status + "\n")
    write_lf(impl_dir / "group-ids.txt", "1\n3\n")
    write_lf(impl_dir / "item-tasks.tsv", "1\tt1\n3\tt3\n2\tt2\n")
    [close_out] = [block for block in bash_blocks(_PHASE3.read_text(encoding="utf-8")) if "--close-out" in block]
    script = scratch / "group-closeout.sh"
    write_lf(script, render_shell(close_out, _REPLAY.parents[1], "oss"))
    result = subprocess.run(
        [bash, bash_path(script)],
        cwd=repo,
        env={
            **_ENV,
            "TMPDIR": bash_path(scratch),
            "CLAUDE_CODE_SESSION_ID": "test-group",
            "CLAUDE_PLUGIN_ROOT": bash_path(_REPLAY.parents[1]),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.splitlines()


@pytest.mark.skipif(shutil.which("bash") is None, reason="The grouped close-out runs under Bash")
@pytest.mark.parametrize(
    ("times_out", "fence_status", "rewrite"),
    [
        pytest.param(True, None, False, id="timeout-missing-status"),
        pytest.param(False, "failed", False, id="nonzero-stale-status"),
        pytest.param(True, "failed", True, id="formatter-retry-then-timeout"),
    ],
)
def test_commit_call_failure_after_head_advances_records_the_spent_group(
    grouped_run: tuple[Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    times_out: bool,
    fence_status: str | None,
    rewrite: bool,
) -> None:
    """A real commit survives a hook timeout or a nonzero command result and cannot be replayed twice."""
    repo, impl_dir, _ = grouped_run
    before = _git(repo, "rev-parse", "HEAD")
    later_diff = _git(repo, "diff", "--cached", "--", "test_core.py")
    summaries = _summaries(impl_dir, "logic", "1 3")
    args = ["--impl-dir", str(impl_dir), "--topic", "logic", "--summaries-file", str(summaries), "--pr", "#7"]
    write_lf(impl_dir / "group-commit-attempt.txt", "current\n")
    if rewrite:
        formatter = (
            "from pathlib import Path\np = Path('core.py')\ntext = p.read_bytes()\n"
            "if not text.endswith(b'# formatted\\n'):\n"
            "    p.write_bytes(text + b'# formatted\\n')\n    raise SystemExit(1)\n"
        )
        _approve_hook(repo, tmp_path / "hooks", formatter, monkeypatch)
    payload = "import time\ntime.sleep(8)\n" if times_out else "pass\n"
    digest = _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch, name="post-commit")
    monkeypatch.setenv("RESOLVE_APPROVED_HOOKS", digest)
    monkeypatch.setattr(replay, "BUDGET_SECONDS", 4)
    if not times_out:

        def nonzero_after_commit(executable: str, args: list[str], files: list[str], timeout: float) -> int:
            """Return a failed command status only after Git actually commits."""
            assert commit_with_hooks(executable, args, files, timeout) == 0
            return 4

        monkeypatch.setattr(replay, "commit_with_hooks", nonzero_after_commit)

    exit_code = replay.main([*args, "--items", "1 3", "--attempt-id", "current"])

    assert _git(repo, "rev-parse", "HEAD^") == before
    assert exit_code == 4
    head = _git(repo, "rev-parse", "HEAD")
    assert (impl_dir / replay.COMMITTED_RECORD).read_text(encoding="utf-8").splitlines() == ["1", "3"]
    (impl_dir / replay.COMMITTED_RECORD).unlink()
    closed = _close_out_committed_group(repo, impl_dir, shutil.which("bash") or "bash", fence_status)
    assert closed[0].startswith("! BLOCKED — group commit created")
    assert closed[1:] == ["TaskUpdate target: item=1 task=t1", "TaskUpdate target: item=3 task=t3"]
    assert _git(repo, "write-tree") == _git(repo, "rev-parse", "HEAD^{tree}")
    assert replay.main([*args, "--items", "1 3"]) == 4
    assert _git(repo, "rev-parse", "HEAD") == head

    # Reconcile the only edit still pending, then resume with the next group.
    _git(repo, "add", "test_core.py")
    assert _git(repo, "diff", "--cached", "--", "test_core.py") == later_diff
    (impl_dir / replay.TARGET_FILE).write_text(_git(repo, "write-tree") + "\n", encoding="utf-8")
    resumed = _commit_group(repo, impl_dir, "tests", "2")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert _git(repo, "rev-parse", "HEAD^") == head
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert (impl_dir / replay.COMMITTED_RECORD).read_text(encoding="utf-8").splitlines() == ["2"]


@pytest.mark.parametrize(
    ("command_code", "approval"),
    [
        pytest.param(0, "", id="success-without-hooks"),
        pytest.param(0, "approved", id="success-with-approved-hooks"),
        pytest.param(1, "approved", id="failed-with-approved-hooks"),
    ],
)
def test_unrelated_child_commit_is_refused(
    grouped_run: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command_code: int,
    approval: str,
) -> None:
    """A different tree on the same parent cannot prove the group's commit, regardless of the command status."""
    repo, impl_dir, _ = grouped_run
    before = _git(repo, "rev-parse", "HEAD")
    other = _git(repo, "commit-tree", f"{before}^{{tree}}", "-p", before, "-m", "unrelated writer")

    def advance_unrelated(*_args: object) -> int:
        """Advance the real branch during the commit call, independently of the group's staged tree."""
        _git(repo, "update-ref", "HEAD", other, before)
        return command_code

    monkeypatch.setattr(replay, "commit_with_hooks", advance_unrelated)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("RESOLVE_APPROVED_HOOKS", approval)
    summaries = _summaries(impl_dir, "logic", "1 3")
    args = ["--impl-dir", str(impl_dir), "--topic", "logic", "--summaries-file", str(summaries), "--pr", "#7"]

    assert replay.main([*args, "--items", "1 3"]) == 1
    assert "tree" in capsys.readouterr().out
    assert _git(repo, "rev-parse", "HEAD") == other
    assert not (impl_dir / replay.COMMITTED_RECORD).exists()


@pytest.mark.parametrize("interrupt_during_proof", [False, True])
def test_interrupted_helper_recovers_the_commit_during_closeout(
    grouped_run: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interrupt_during_proof: bool,
) -> None:
    """Close-out recovers the commit both before attribution and during the later item proof."""
    repo, impl_dir, _ = grouped_run
    monkeypatch.chdir(repo)
    monkeypatch.delenv("RESOLVE_APPROVED_HOOKS", raising=False)
    # The replay runs in this process, so a runner with no global git identity needs it from the environment.
    for key, value in _IDENTITY.items():
        monkeypatch.setenv(key, value)
    summaries = _summaries(impl_dir, "logic", "1 3")
    args = ["--impl-dir", str(impl_dir), "--topic", "logic", "--summaries-file", str(summaries), "--pr", "#7"]
    args += ["--items", "1 3", "--attempt-id", "current"]
    write_lf(impl_dir / "group-commit-attempt.txt", "current\n")

    def interrupt_after_commit(executable: str, command: list[str], files: list[str], timeout: float) -> int:
        """Interrupt the replay process immediately after its real Git process creates the commit."""
        assert commit_with_hooks(executable, command, files, timeout) == 0
        raise KeyboardInterrupt

    def interrupt_item_proof(*_args: object) -> tuple[str, ...]:
        """Interrupt item verification after Git has returned the attributed commit."""
        raise KeyboardInterrupt

    hook, replacement = (
        ("_prove_items", interrupt_item_proof)
        if interrupt_during_proof
        else ("commit_with_hooks", interrupt_after_commit)
    )
    with monkeypatch.context() as interruption:
        interruption.setattr(replay, hook, replacement)
        with pytest.raises(KeyboardInterrupt):
            replay.main(args)
    head = _git(repo, "rev-parse", "HEAD")
    receipt = impl_dir / "group-replay-receipt-logic.txt"
    assert receipt.exists() is interrupt_during_proof
    assert not interrupt_during_proof or receipt.read_text(encoding="utf-8").startswith(f"commit {head} target ")
    subdirectory = repo / "child"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)
    assert replay.main(["--close-out", str(impl_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == ["committed-blocked", "1", "3"]
    assert replay.main(args) == 4
    assert _git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("direct", [False, True])
def test_hook_reverting_group_item_leaves_only_that_item_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direct: bool
) -> None:
    """A commit containing the other item cannot complete a reverted direct or commit-backed edit."""
    repo, impl_dir, _ = _build_run(tmp_path)
    _stage_direct_items(repo, impl_dir, {"4": ("core.py", 22, "value_22 = 'direct'\n")})
    item, old, new = (4, "value_22 = 'direct'", "value_22 = 22") if direct else (1, "value_2 = 'two'", "value_2 = 2")
    payload = _rewrite_shared_blob(f"blob.replace({old.encode()!r}, {new.encode()!r})").replace("shared.txt", "core.py")
    approved = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch)}

    result = _commit_group(repo, impl_dir, "logic", f"{item} 3", env=approved)

    assert result.returncode == 4, result.stdout + result.stderr
    assert f"items {item} could not be proven" in result.stdout
    assert (impl_dir / replay.COMMITTED_RECORD).read_text(encoding="utf-8").splitlines() == ["3"]
    assert old not in _git(repo, "show", "HEAD:core.py")
    target = (impl_dir / replay.TARGET_FILE).read_text(encoding="utf-8").strip()
    assert old in _git(repo, "show", f"{target}:core.py")


def test_rewritten_path_cannot_reuse_earlier_absorption_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook can remove an edit on a shared path even when an earlier receipt proved it was present there."""
    repo, impl_dir, _ = _build_run(tmp_path)
    hooks = tmp_path / "hooks"
    approved = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, hooks, "pass\n", monkeypatch)}
    first = _commit_group(repo, impl_dir, "logic", "1 3", env=approved)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "also carries items 2" in first.stdout

    _stage_direct_items(repo, impl_dir, {"4": ("core.py", 22, "value_22 = 'direct'\n")})
    write_lf(impl_dir / replay.TARGET_FILE, _git(repo, "write-tree") + "\n")
    payload = _rewrite_shared_blob("blob.replace(b\"value_30 = 'thirty'\", b'value_30 = 30')").replace(
        "shared.txt", "core.py"
    )
    approved["RESOLVE_APPROVED_HOOKS"] = _approve_hook(repo, hooks, payload, monkeypatch)
    result = _commit_group(repo, impl_dir, "tests", "2 4", env=approved)

    assert result.returncode == 4, result.stdout + result.stderr
    assert "value_30 = 'thirty'" not in _git(repo, "show", "HEAD:core.py")
    assert "2" not in (impl_dir / replay.COMMITTED_RECORD).read_text(encoding="utf-8").splitlines()


def test_already_landed_requires_the_absorbed_entry_to_survive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A later group cannot close a direct item after an intervening commit removes its absorbed blob."""
    repo, impl_dir, _ = _build_run(tmp_path, item_two_tests=False)
    _stage_direct_items(repo, impl_dir, {"4": ("core.py", 22, "value_22 = 'direct'\n")})
    approved = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", "pass\n", monkeypatch)}
    first = _commit_group(repo, impl_dir, "logic", "1 2 3", env=approved)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "also carries items 4" in first.stdout

    _edit_line(repo, "core.py", 22, "value_22 = 22\n")
    _git(repo, "-c", f"core.hooksPath={os.devnull}", "commit", "-qm", "remove direct edit", "--", "core.py")
    write_lf(impl_dir / replay.TARGET_FILE, _git(repo, "write-tree") + "\n")
    result = _commit_group(repo, impl_dir, "direct", "4")

    assert result.returncode == 3, result.stdout + result.stderr
    assert "4" not in (impl_dir / replay.COMMITTED_RECORD).read_text(encoding="utf-8").splitlines()


def test_commit_budget_is_shared_by_both_hook_attempts(
    grouped_run: tuple[Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A formatter hook slow on both attempts runs out of one shared budget, so the call never outlasts its timeout.

    Each attempt alone fits the timeout; only a budget shared by the first attempt and the retry stops the second.
    """
    repo, _, _ = grouped_run
    payload = (
        "import sys, time\nfrom pathlib import Path\n"
        "time.sleep(2)\n"
        "p = Path('extra.py')\n"
        "if 'formatted' not in p.read_text(encoding='utf-8'):\n"
        "    p.write_text('EXTRA = 1  # formatted\\n', encoding='utf-8', newline='\\n')\n"
        "    sys.exit(1)\n"
    )
    monkeypatch.setenv("RESOLVE_APPROVED_HOOKS", _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch))
    head = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(subprocess.TimeoutExpired):
        commit_with_hooks("git", ["commit", "-q", "-m", "slow"], ["extra.py"], 3)
    assert _git(repo, "rev-parse", "HEAD") == head
    with pytest.raises(subprocess.TimeoutExpired):
        commit_with_hooks("git", ["commit", "-q", "-m", "late"], None, 0)
    assert CallBudget.starting_now(600).cap(30) == 30


def test_hook_rewrite_of_a_shared_file_is_recorded_byte_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved formatter that rewrites a shared non-ASCII file is folded in, and the later group closes as landed.

    The hook's recovery commits the whole working file, so the later group's change to it rides along; the helper names
    it, folds the rewrite into the target under the file's exact name, and leaves nothing staged.
    """
    shared = "café.py"
    repo, impl_dir, _ = _build_run(tmp_path, shared=shared, item_two_tests=False)
    payload = (
        "import sys\nfrom pathlib import Path\n"
        f"p = Path({shared!r})\n"
        "text = p.read_text(encoding='utf-8')\n"
        "if not text.endswith('# formatted\\n'):\n"
        "    p.write_text(text + '# formatted\\n', encoding='utf-8', newline='\\n')\n"
        "    sys.exit(1)\n"
    )
    approved = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch)}
    logic = _commit_group(repo, impl_dir, "logic", "1 3", env=approved)
    assert logic.returncode == 0, logic.stdout + logic.stderr
    assert f"GROUP_REPLAY_FALLBACK={shared} (item 1, group logic: approved hooks" in logic.stdout
    assert "also carries items 2 of a later group" in logic.stdout
    tests = _commit_group(repo, impl_dir, "tests", "2", env=approved)
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert "already landed in the commit of group(s) logic" in tests.stdout
    committed = _git(repo, "show", f"HEAD:{shared}")
    assert committed.endswith("# formatted")
    assert "value_30 = 'thirty'" in committed
    assert _git(repo, "diff", "--cached", "--name-only") == "", "the rewrite is in the target, never staged as a revert"


def test_approved_formatter_can_fix_a_file_with_adjacent_group_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A formatter that needs a complete staged file can fix adjacent edits and land both groups' changes."""
    repo, impl_dir, _ = _build_run(tmp_path, item_two_tests=False, adjacent=True)
    payload = (
        "import subprocess, sys\nfrom pathlib import Path\n"
        "p = Path('core.py')\n"
        "text = p.read_text(encoding='utf-8')\n"
        "if \"value_6 = 'six'\\n\" in text:\n"
        "    if subprocess.run(['git', 'diff', '--quiet', '--', str(p)]).returncode:\n"
        "        sys.exit('formatter cannot restore the unstaged edit beside its fix')\n"
        "    p.write_text(text.replace(\"value_6 = 'six'\\n\", 'value_6 = \"six\"\\n'), encoding='utf-8')\n"
        "    sys.exit(1)\n"
    )
    approved = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch)}
    logic = _commit_group(repo, impl_dir, "logic", "1 3", env=approved)
    assert logic.returncode == 0, logic.stdout + logic.stderr
    assert 'value_6 = "six"' in _git(repo, "show", "HEAD:core.py")
    assert "value_7 = 'later'" in _git(repo, "show", "HEAD:core.py")
    tests = _commit_group(repo, impl_dir, "tests", "2", env=approved)
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert "items 2 already landed" in tests.stdout
    assert _git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize(
    ("format_line", "rewrites", "restages"),
    [
        pytest.param(28, True, False, id="distant-formatter-edit"),
        pytest.param(6, True, True, id="adjacent-formatter-restages"),
        pytest.param(28, False, False, id="no-hook-rewrite"),
    ],
)
def test_whole_file_absorbs_nearby_independent_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_line: int, rewrites: bool, restages: bool
) -> None:
    """Both nearby edits land in one commit even when a formatter changes the staged file."""
    repo, impl_dir, _ = _build_run(tmp_path, item_two_tests=False, later_line=8)
    _edit_line(repo, "core.py", format_line, f"value_{format_line} = 'formatted'   \n")
    _git(repo, "add", "core.py")
    payload = (
        "import subprocess, sys\nfrom pathlib import Path\n"
        "p = Path('core.py')\n"
        "text = p.read_text(encoding='utf-8')\n"
        "formatted = text.replace('   \\n', '\\n')\n"
        f"if {rewrites!r} and formatted != text:\n"
        "    p.write_text(formatted, encoding='utf-8', newline='\\n')\n"
        f"    if {restages!r}:\n"
        "        subprocess.run(['git', 'add', '--', str(p)], check=True)\n"
        "    else:\n"
        "        sys.exit(1)\n"
    )
    approved = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch)}
    first = _commit_group(repo, impl_dir, "first", "1 3", env=approved)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "value_8 = 'later'" in _git(repo, "show", "HEAD:core.py")
    expected_line = f"value_{format_line} = 'formatted'" + ("" if rewrites else "   ")
    assert _git(repo, "show", "HEAD:core.py").splitlines()[format_line] == expected_line
    assert _git(repo, "diff", "--cached", "--", "core.py") == ""
    assert "also carries items 2" in first.stdout
    head = _git(repo, "rev-parse", "HEAD")
    assert 2 not in grouped_commits(impl_dir, repo)
    later = _commit_group(repo, impl_dir, "later", "2", env=approved, attempt_id="later")
    assert later.returncode == 0, later.stdout + later.stderr
    assert "items 2 already landed" in later.stdout
    assert grouped_commits(impl_dir, repo)[2] == head[:12]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert _git(repo, "diff", "--name-only") == ""


@pytest.mark.parametrize(
    ("transform", "expected", "present"),
    [
        pytest.param("text", 0, True, id="same-blob"),
        pytest.param("text.replace(b\"value_2 = 'two'\", b'value_2 = \"two\"')", 4, True, id="formatted-blob"),
        pytest.param("text.replace(b\"value_22 = 'direct'\", b'value_22 = 22')", 4, False, id="reverted-direct-edit"),
    ],
)
def test_whole_file_absorbs_a_later_direct_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transform: str, expected: int, present: bool
) -> None:
    """A direct item's shared-file change needs an identical committed blob to count as absorbed."""
    repo, impl_dir, _ = _build_run(tmp_path, item_two_tests=False)
    _stage_direct_items(repo, impl_dir, {"4": ("core.py", 22, "value_22 = 'direct'\n")})
    payload = (
        "import subprocess\nfrom pathlib import Path\n"
        "p = Path('core.py')\n"
        "text = p.read_bytes()\n"
        f"p.write_bytes({transform})\n"
        "subprocess.run(['git', 'add', '--', str(p)], check=True)\n"
    )
    approved = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch)}
    first = _commit_group(repo, impl_dir, "first", "1 3", env=approved)
    assert first.returncode == expected, first.stdout + first.stderr
    assert ("value_22 = 'direct'" in _git(repo, "show", "HEAD:core.py")) is present
    absorbed = (impl_dir / replay.ABSORBED_RECORD).read_text(encoding="utf-8").splitlines()
    assert ("4" in {json.loads(row)["item"] for row in absorbed}) is (expected == 0)


def _build_shared_edit_run(
    tmp_path: Path, first: dict[int, str], later: dict[int, str], *, direct: bool
) -> tuple[Path, Path, str]:
    """Stage overlapping groups' files while retaining the original implementation records."""
    repo, impl_dir = tmp_path / "repo", tmp_path / "impl"
    repo.mkdir()
    impl_dir.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    path = "shared.txt"
    (repo / path).write_text("".join(_SHARED_LINES), encoding="utf-8", newline="\n")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    rows = []
    for item, edits in [(1, first), (2, later)]:
        if item == 2 and direct:
            (impl_dir / "c1-item-files.tsv").write_text(f"2\t{path}\n", encoding="utf-8", newline="\n")
            continue
        _git(repo, "switch", "-qc", f"item-{item}", base)
        for line, value in edits.items():
            _edit_line(repo, path, line, value + "\n")
        _git(repo, "add", path)
        _git(repo, "commit", "-qm", f"fix: item {item}")
        rows.append({"item_id": item, "sha": _git(repo, "rev-parse", "HEAD")})
    _git(repo, "switch", "-q", "main")
    for line, value in (first | later).items():
        _edit_line(repo, path, line, value + "\n")
    (impl_dir / "phase2-commits.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n"
    )
    _git(repo, "add", path)
    return repo, impl_dir, path


def _rewrite_shared_blob(transform: str, *, update_working_file: bool = False) -> str:
    """Build a hook that transforms the index blob and optionally updates the working file."""
    return (
        "import subprocess\nfrom pathlib import Path\n"
        "path = 'shared.txt'\n"
        "blob = subprocess.check_output(['git', 'show', ':' + path])\n"
        f"formatted = {transform}\n"
        "oid = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'], input=formatted).decode().strip()\n"
        "subprocess.run(['git', 'update-index', '--cacheinfo', '100644', oid, path], check=True)\n"
        f"if {update_working_file!r}:\n"
        "    Path(path).write_bytes(formatted)\n"
    )


@pytest.mark.parametrize(
    ("first_edits", "later_line", "direct", "hook"),
    [
        pytest.param({9: "NINE", 28: "FORMAT   "}, 11, False, "index", id="index-distant-format-close-edits"),
        pytest.param({9: "NINE"}, 25, True, "index", id="index-later-direct-item-unchanged-blob"),
        pytest.param({9: "NINE   "}, 10, False, "index", id="index-adjacent-edits"),
        pytest.param({9: "NINE", 28: "FORMAT   "}, 11, False, "worktree", id="worktree-distant-format-close-edits"),
        pytest.param({9: "NINE"}, 25, True, "worktree", id="worktree-later-direct-item-unchanged-blob"),
        pytest.param({9: "NINE   "}, 10, False, "worktree", id="worktree-adjacent-edits"),
        pytest.param({9: "NINE   "}, 11, False, "disabled", id="hooks-disabled-splits-close-edits"),
    ],
)
def test_group_replay_attributes_later_shared_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_edits: dict[int, str],
    later_line: int,
    direct: bool,
    hook: str,
) -> None:
    """Hook scope does not change attribution.

    With hooks disabled, each group keeps its own edit.
    """
    repo, impl_dir, path = _build_shared_edit_run(tmp_path, first_edits, {later_line: "LATER"}, direct=direct)
    env = _ENV
    if hook != "disabled":
        payload = _rewrite_shared_blob("blob.replace(b'   \\n', b'\\n')", update_working_file=hook == "worktree")
        env = {**env, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch)}

    first = _commit_group(repo, impl_dir, "first", "1", env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    head = _git(repo, "rev-parse", "HEAD")
    first_lines = _git(repo, "show", f"HEAD:{path}").splitlines()
    assert first_lines[later_line] == ("LATER" if hook != "disabled" else _SHARED_LINES[later_line].strip())
    format_line = max(first_edits)
    assert first_lines[format_line] == (
        first_edits[format_line].rstrip() if hook != "disabled" else first_edits[format_line]
    )
    assert (impl_dir / replay.ABSORBED_RECORD).exists() == (hook != "disabled")
    assert _git(repo, "diff", "--name-only") == (path if hook == "index" and not direct else "")

    later = _commit_group(repo, impl_dir, "later", "2", env=env)
    assert later.returncode == 0, later.stdout + later.stderr
    assert ("items 2 already landed" in later.stdout) == (hook != "disabled")
    assert (_git(repo, "rev-parse", "HEAD") == head) == (hook != "disabled")
    assert _git(repo, "show", f"HEAD:{path}").splitlines()[later_line] == "LATER"
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert (impl_dir / replay.COMMITTED_RECORD).read_text(encoding="utf-8").splitlines() == ["1", "2"]


def test_replay_conflict_reports_existing_commit_and_recovery_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook that removes one hunk and conflicts with another produces a committed recovery state."""
    repo, impl_dir, path = _build_shared_edit_run(tmp_path, {9: "FIRST"}, {11: "LATER", 25: "PENDING"}, direct=False)
    target = _git(repo, "write-tree")
    base = _git(repo, "rev-parse", "HEAD")
    payload = _rewrite_shared_blob("blob.replace(b'LATER', b'value_11 = 11').replace(b'PENDING', b'CONFLICT')")
    env = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch)}

    result = _commit_group(repo, impl_dir, "first", "1", env=env)

    assert result.returncode == 4, result.stdout + result.stderr
    assert "group 'first' committed, but the hook left pending edits that could not be replayed" in result.stdout
    assert f"pre-hook target {target}" in result.stdout
    assert "no commit created" not in result.stdout
    assert _git(repo, "rev-parse", "HEAD^") == base
    assert "CONFLICT" in _git(repo, "show", f"HEAD:{path}")
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert (impl_dir / replay.COMMITTED_RECORD).read_text(encoding="utf-8").splitlines() == ["1"]
    assert not (impl_dir / replay.ABSORBED_RECORD).exists()


def test_refresh_attributes_replay_that_leaves_head_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An item already in HEAD applies cleanly without changing the refreshed target."""
    repo, impl_dir, path = _build_shared_edit_run(tmp_path, {9: "FIRST"}, {25: "LATER"}, direct=False)
    _git(repo, "commit", "-qm", "both edits")
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    later = replay.load_item_commits(impl_dir)["2"]
    monkeypatch.chdir(repo)
    git = replay.Git("git", CallBudget.starting_now(30), three_way=True)

    assert replay._refresh_target(git, impl_dir, head_tree, head_tree, {path}, {path: {"2": later}}) == (
        head_tree,
        [],
        {path: {"2"}},
    )


@pytest.mark.parametrize("also_writes_worktree", [False, True])
def test_hook_that_commits_without_a_later_edit_leaves_it_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, also_writes_worktree: bool
) -> None:
    """A rewrite of the selected path alone cannot prove that the committed blob contains the later item."""
    repo, impl_dir, _ = _build_run(tmp_path, item_two_tests=False)
    payload = (
        "import subprocess\nfrom pathlib import Path\n"
        "if 'extra.py' in subprocess.check_output(['git', 'diff', '--cached', '--name-only'], text=True).splitlines():\n"
        "    blob = subprocess.check_output(['git', 'show', ':core.py'])\n"
        "    blob = blob.replace(b\"value_30 = 'thirty'\", b'value_30 = 30')\n"
        "    blob = blob.replace(b\"value_2 = 'two'\", b'value_2 = \"two\"')\n"
        "    oid = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'], input=blob).decode().strip()\n"
        "    subprocess.run(['git', 'update-index', '--cacheinfo', '100644', oid, 'core.py'], check=True)\n"
        f"    if {also_writes_worktree!r}:\n"
        "        Path('core.py').write_bytes(blob)\n"
    )
    approved = {**_ENV, "RESOLVE_APPROVED_HOOKS": _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch)}
    logic = _commit_group(repo, impl_dir, "logic", "1 3", env=approved)
    assert logic.returncode == 0, logic.stdout + logic.stderr
    assert "also carries items 2" not in logic.stdout
    assert "value_30 = 'thirty'" not in _git(repo, "show", "HEAD:core.py")
    assert not (impl_dir / replay.ABSORBED_RECORD).exists()
    assert 'value_2 = "two"' in _git(repo, "show", ":core.py")
    assert _git(repo, "diff", "--cached", "--unified=0", "--", "core.py").splitlines()[-2:] == [
        "-value_30 = 30",
        "+value_30 = 'thirty'",
    ]

    tests = _commit_group(repo, impl_dir, "tests", "2", env=approved)
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert "already landed" not in tests.stdout
    assert "value_30 = 'thirty'" in _git(repo, "show", "HEAD:core.py")
    assert 'value_2 = "two"' in _git(repo, "show", "HEAD:core.py")
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_successful_commit_has_time_to_refresh_its_target(
    grouped_run: tuple[Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Even a hook that uses the commit's whole time allowance gets its committed rewrite retained in the target."""
    repo, impl_dir, _ = grouped_run
    payload = (
        "import subprocess\nfrom pathlib import Path\n"
        "Path('extra.py').write_bytes(b'EXTRA = 2\\n')\n"
        "subprocess.run(['git', 'add', 'extra.py'], check=True)\n"
    )
    monkeypatch.setenv("RESOLVE_APPROVED_HOOKS", _approve_hook(repo, tmp_path / "hooks", payload, monkeypatch))
    git = replay.Git("git", CallBudget.starting_now(240), three_way=True)
    request = replay.GroupRequest(impl_dir, "logic", ("fix: logic",), "#7", ("1", "3"), False)

    def commit_to_deadline(executable: str, args: list[str], files: list[str], timeout: float) -> int:
        """Run the real hook, then advance the commit's deadline to simulate use of its full allowance."""
        code = commit_with_hooks(executable, args, files, timeout)
        git.budget = CallBudget.starting_now(0)
        return code

    monkeypatch.setattr(replay, "commit_with_hooks", commit_to_deadline)
    assert replay.commit_group(git, request) == 0
    assert "target not refreshed" not in capsys.readouterr().out
    assert _git(repo, "show", ":extra.py") == _git(repo, "show", "HEAD:extra.py") == "EXTRA = 2"
