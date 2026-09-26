"""Tests for ``bin/release_append_marker.py``.

``_is_valid_commit``/``_tag_advanced_past`` mock ``subprocess.run``/``which`` — no real ``git`` invocations.
``_marker_path``/``_read_marker`` are exercised against ``tmp_path`` via the ``--marker-dir`` override so no test
touches a real ``.temp/`` directory.

Two-subprocess-call sequencing: ``resolve``/``is-valid`` call ``_is_valid_commit`` then (only when it's True)
``_tag_advanced_past`` — tests covering that combined path use an iterator side effect to give each call its own return
code rather than one blanket value.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

import release_append_marker as ram


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status returned by a fake Git command."""
        self.returncode = returncode
        self.stdout = stdout


def _sequenced_run(monkeypatch: pytest.MonkeyPatch, *returncodes: int) -> None:
    """Patch ``subprocess.run`` to return each of ``returncodes`` in call order."""
    calls = iter(returncodes)
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=next(calls)))


def _write_marker(root: Path, content: str, encoding: str = "utf-8") -> None:
    """Seed a versioned marker for CLI behavior tests."""
    path = ram._marker_path("main", str(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)


def _write_receipt(path: Path, range_text: str, sha: str, draft: Path) -> None:
    """Create an exact-byte completion receipt for marker command tests."""
    path.write_text(
        json.dumps(
            {
                "branch": "main",
                "range": range_text,
                "head": "fedcba123",
                "endpoint": sha,
                "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
                "last_tag": "",
                "tag_sha": None,
                "saved_marker_sha256": None,
                "baseline": "deadbeef123",
            }
        ),
        encoding="utf-8",
    )


def _fake_marker_git(argv: list[str], **_kwargs: Any) -> _FakeCompleted:
    """Return a stable attached branch, HEAD, or range endpoint to marker tests."""
    if "symbolic-ref" in argv:
        return _FakeCompleted(stdout="main\n")
    if "HEAD^{commit}" in argv:
        return _FakeCompleted(stdout="fedcba123\n")
    return _FakeCompleted(stdout="deadbeef123\n")


# ---------------------------------------------------------------------------
# Pure functions — _marker_path / _read_marker
# ---------------------------------------------------------------------------


def test_marker_path_joins_branch_into_filename(tmp_path: Path) -> None:
    """Build a branch-bound marker path in the versioned state namespace."""
    path = ram._marker_path("main", str(tmp_path))
    assert path == tmp_path / "release-state-v2" / ram.branch_state_key("main") / "marker"


def test_read_marker_missing_file_is_empty(tmp_path: Path) -> None:
    """No marker file yet → empty string, no exception."""
    assert ram._read_marker("main", str(tmp_path)) == ""


def test_read_marker_strips_whitespace(tmp_path: Path) -> None:
    """Stored sha is stripped of surrounding whitespace/newline."""
    _write_marker(tmp_path, "deadbeef123\n", encoding="utf-8")
    assert ram._read_marker("main", str(tmp_path)) == "deadbeef123"


@pytest.mark.parametrize("target_exists", [True, False])
def test_linked_marker_blocks_guard_and_resolve_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target_exists: bool
) -> None:
    """A linked state marker must stop append before a full draft overwrite."""
    monkeypatch.chdir(tmp_path)
    marker = ram._marker_path("main", str(tmp_path / ".temp"))
    marker.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    if target_exists:
        outside.write_bytes(b"deadbeef1234\n")
    marker.symlink_to(outside)

    assert ram.main(["guard", "--branch", "main"]) == 1
    assert ram.main(["resolve", "--branch", "main", "--last-tag", "v1", "--marker-dir", str(tmp_path / ".temp")]) == 1
    assert "marker path must not traverse a symlink" in capsys.readouterr().err
    assert marker.is_symlink()
    if target_exists:
        assert outside.read_bytes() == b"deadbeef1234\n"
    else:
        assert not outside.exists()


def test_pending_publication_blocks_marker_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A marker cannot certify a release while its branch has a pending journal."""
    monkeypatch.chdir(tmp_path)
    marker = ram._marker_path("main", str(tmp_path / ".temp"))
    marker.parent.mkdir(parents=True)
    marker.write_text("deadbeef123\n", encoding="utf-8")
    journal = tmp_path / ram.state_relative("main", "journal.json")
    journal.write_text('{"mode":"rollback"}\n', encoding="utf-8")
    monkeypatch.setattr(ram, "_is_valid_commit", lambda _sha: True)
    monkeypatch.setattr(ram, "_tag_advanced_past", lambda _sha, _tag: False)

    for action in ("is-valid", "resolve"):
        assert ram.main([action, "--branch", "main", "--last-tag", "v1"]) == 1
        output = capsys.readouterr()
        assert output.out == ""
        assert "pending append publication" in output.err
    assert ram.main(["guard", "--branch", "main"]) == 1
    assert "pending append publication" in capsys.readouterr().err
    journal.unlink()
    assert ram.main(["is-valid", "--branch", "main", "--last-tag", "v1"]) == 0
    assert capsys.readouterr().out == "true\n"


def test_pending_publication_blocks_guard_and_live_marker_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither setup nor a direct write may certify a pending publication."""
    monkeypatch.chdir(tmp_path)
    marker = ram._marker_path("main", None)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"ORIGINAL\n")
    journal = tmp_path / ram.state_relative("main", "journal.json")
    journal.write_bytes(b'{"mode":"rollback"}\n')
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(stdout="main\n"))

    assert ram.main(["guard", "--branch", "main"]) == 1
    assert ram.main(["write", "--branch", "main", "--sha", "abc123"]) == 1
    assert "pending append publication" in capsys.readouterr().err
    assert marker.read_bytes() == b"ORIGINAL\n"
    assert journal.read_bytes() == b'{"mode":"rollback"}\n'
    stage = tmp_path / "candidate"
    stage.mkdir()
    draft = stage / "DRAFT.md"
    draft.write_text("completed notes\n", encoding="utf-8")
    receipt = stage / "receipt.json"
    _write_receipt(receipt, "v1..deadbeef123", "deadbeef123", draft)
    recorded = json.loads(receipt.read_text(encoding="utf-8"))
    recorded["saved_marker_sha256"] = hashlib.sha256(marker.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(recorded), encoding="utf-8")
    monkeypatch.setattr(ram.subprocess, "run", _fake_marker_git)
    monkeypatch.setattr(ram, "_is_valid_commit", lambda sha: sha != "ORIGINAL")
    assert (
        ram.main(
            [
                "write",
                "--branch",
                "main",
                "--sha",
                "deadbeef123",
                "--receipt",
                str(receipt),
                "--draft",
                str(draft),
                "--marker-dir",
                str(stage / ".temp"),
            ]
        )
        == 0
    )
    assert ram._marker_path("main", str(stage / ".temp")).read_bytes() == b"deadbeef123\n"
    assert marker.read_bytes() == b"ORIGINAL\n"


# ---------------------------------------------------------------------------
# Pure function — _is_valid_commit (reachability, not object-DB existence)
# ---------------------------------------------------------------------------


def test_is_valid_commit_empty_sha_is_false() -> None:
    """Blank sha short-circuits to False without invoking git."""
    assert ram._is_valid_commit("") is False


def test_is_valid_commit_git_missing_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """No git on PATH → False, no subprocess call attempted."""
    monkeypatch.setattr(ram, "which", lambda _: None)
    assert ram._is_valid_commit("deadbeef") is False


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [pytest.param(0, True, id="is-ancestor-of-head"), pytest.param(1, False, id="not-ancestor-of-head-rebased-away")],
)
def test_is_valid_commit_reflects_ancestor_check(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    """Mirror ``git merge-base --is-ancestor``'s exit code."""
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=returncode))
    assert ram._is_valid_commit("deadbeef") is expected


def test_is_valid_commit_uses_merge_base_is_ancestor_not_cat_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: must call ``merge-base --is-ancestor``, not ``cat-file -e`` (dangling-commit bug).

    ``cat-file -e`` only tests object-database existence — a commit orphaned by rebase/force-push stays reflog-protected
    (~90 days) and would still report "exists" under that check, silently treating a stale marker as valid. Git's
    ancestor check tests reachability from HEAD instead.
    """
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record reachability argv and return a successful ancestor check."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_run)
    ram._is_valid_commit("deadbeef")
    assert recorded == [["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", "deadbeef", "HEAD"]]


# ---------------------------------------------------------------------------
# Pure function — _tag_advanced_past (tag-blind marker fix)
# ---------------------------------------------------------------------------


def test_tag_advanced_past_empty_inputs_are_false() -> None:
    """No marker or no tag → nothing to compare, False without invoking git."""
    assert ram._tag_advanced_past("", "v1.0.0") is False
    assert ram._tag_advanced_past("deadbeef", "") is False


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        pytest.param(0, True, id="marker-is-ancestor-of-tag-superseded"),
        pytest.param(1, False, id="tag-predates-marker-normal-case"),
    ],
)
def test_tag_advanced_past_reflects_ancestor_check(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    """Mirror whether the marker is an ancestor of the tag."""
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=returncode))
    assert ram._tag_advanced_past("deadbeef", "v2.0.0") is expected


def test_tag_advanced_past_records_git_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invokes ``git merge-base --is-ancestor <marker> <last_tag>``."""
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record marker-versus-tag argv and return a successful ancestor check."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_run)
    ram._tag_advanced_past("deadbeef", "v2.0.0")
    assert recorded == [["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", "deadbeef", "v2.0.0"]]


# ---------------------------------------------------------------------------
# CLI: write
# ---------------------------------------------------------------------------


def test_write_persists_sha_creating_parent_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create the marker dir and stores the sha with a trailing newline."""
    marker_dir = tmp_path / "nested" / ".temp"
    draft = tmp_path / "DRAFT.md"
    draft.write_text("completed notes\n", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    _write_receipt(receipt, "v1..deadbeef123", "deadbeef123", draft)
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_marker_git)
    monkeypatch.setattr(ram, "_is_valid_commit", lambda _sha: True)
    rc = ram.main(
        [
            "write",
            "--branch",
            "main",
            "--sha",
            "deadbeef123",
            "--receipt",
            str(receipt),
            "--draft",
            str(draft),
            "--marker-dir",
            str(marker_dir),
        ]
    )
    assert rc == 0
    assert ram._marker_path("main", str(marker_dir)).read_text(encoding="utf-8") == "deadbeef123\n"


def test_direct_write_without_receipt_preserves_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft and range file alone cannot advance the marker before receipt creation."""
    monkeypatch.chdir(tmp_path)
    marker = ram._marker_path("main", None)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"OLD\n")
    (tmp_path / "DRAFT.md").write_text("completed notes\n", encoding="utf-8")
    (tmp_path / "completed-range").write_text("v1..deadbeef123\n", encoding="utf-8")
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(stdout="main\n"))

    assert ram.main(["write", "--branch", "main", "--sha", "deadbeef123"]) != 0
    assert marker.read_bytes() == b"OLD\n"


@pytest.mark.parametrize(
    ("range_text", "draft_text", "sha"),
    [
        pytest.param("v1..deadbeef123\n", "", "deadbeef123", id="missing-draft"),
        pytest.param("v1..deadbeef123\n", "completed notes\n", "cafebabe123", id="different-endpoint"),
        pytest.param("v1...deadbeef123\n", "completed notes\n", "deadbeef123", id="three-dot-range"),
    ],
)
def test_write_refuses_incomplete_or_mismatched_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, range_text: str, draft_text: str, sha: str
) -> None:
    """A bad completion input cannot replace an existing marker."""
    monkeypatch.chdir(tmp_path)
    marker = ram._marker_path("main", None)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"OLD\n")
    draft = tmp_path / "DRAFT.md"
    draft.write_text(draft_text, encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    _write_receipt(receipt, range_text.strip(), "deadbeef123", draft)
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_marker_git)
    monkeypatch.setattr(ram, "_is_valid_commit", lambda _sha: True)

    assert (
        ram.main(
            [
                "write",
                "--branch",
                "main",
                "--sha",
                sha,
                "--receipt",
                str(receipt),
                "--draft",
                str(draft),
            ]
        )
        == 1
    )
    assert marker.read_bytes() == b"OLD\n"


def test_write_refuses_symlink_marker_without_touching_external_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker symlink must not redirect the write into an external file."""
    external = tmp_path / "outside"
    external.write_bytes(b"KEEP\n")
    marker_dir = tmp_path / ".temp"
    marker = ram._marker_path("main", str(marker_dir))
    marker.parent.mkdir(parents=True)
    marker.symlink_to(external)
    draft = tmp_path / "DRAFT.md"
    draft.write_text("completed notes\n", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    _write_receipt(receipt, "v1..deadbeef123", "deadbeef123", draft)
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_marker_git)
    monkeypatch.setattr(ram, "_is_valid_commit", lambda _sha: True)

    assert (
        ram.main(
            [
                "write",
                "--branch",
                "main",
                "--sha",
                "deadbeef123",
                "--receipt",
                str(receipt),
                "--draft",
                str(draft),
                "--marker-dir",
                str(marker_dir),
            ]
        )
        == 1
    )
    assert external.read_bytes() == b"KEEP\n"
    assert marker.is_symlink()


def test_write_on_detached_head_preserves_main_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Direct marker writes on detached HEAD must not change main's baseline."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "start",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach", "HEAD"], check=True)
    monkeypatch.chdir(repo)
    marker = ram._marker_path("main", None)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"ORIGINAL\n")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    draft = repo / "DRAFT.md"
    draft.write_text("completed notes\n", encoding="utf-8")
    receipt = repo / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "branch": "main",
                "range": f"base..{head}",
                "head": head,
                "endpoint": head,
                "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    assert (
        ram.main(
            [
                "write",
                "--branch",
                "main",
                "--sha",
                head,
                "--receipt",
                str(receipt),
                "--draft",
                str(draft),
            ]
        )
        == 1
    )
    assert marker.read_bytes() == b"ORIGINAL\n"


@pytest.mark.parametrize("start_kind", ["missing", "disjoint", "skips-saved", "skips-first-baseline"])
@pytest.mark.parametrize("action", ["receipt", "write"])
def test_direct_receipt_and_write_refuse_unrepresented_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, start_kind: str, action: str
) -> None:
    """A direct marker caller cannot skip undrafted history with an invalid range start."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commit = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "--allow-empty",
    ]
    subprocess.run([*commit, "-m", "first"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run([*commit, "-m", "second"], check=True)
    second = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run([*commit, "-m", "third"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    branch = subprocess.check_output(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], text=True).strip()
    if start_kind == "disjoint":
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side", base], check=True)
        subprocess.run([*commit, "-m", "side"], check=True)
        start = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", branch], check=True)
    else:
        start = second if start_kind in {"skips-saved", "skips-first-baseline"} else "f" * 40
    monkeypatch.chdir(repo)
    draft = repo / "DRAFT.md"
    draft.write_bytes(b"Notes for the selected range\n")
    range_file = repo / "range.txt"
    range_file.write_text(f"{start}..{head}\n", encoding="utf-8")
    receipt = repo / "receipt.json"
    marker = ram._marker_path(branch, None)
    if start_kind != "skips-first-baseline":
        marker.parent.mkdir(parents=True)
        marker.write_bytes((base + "\n").encode())

    if action == "receipt":
        assert (
            ram.main(
                [
                    "receipt",
                    "--branch",
                    branch,
                    "--range-file",
                    str(range_file),
                    "--draft",
                    str(draft),
                    "--output",
                    str(receipt),
                ]
            )
            == 1
        )
        assert not receipt.exists()
        return

    receipt.write_text(
        json.dumps(
            {
                "branch": branch,
                "range": f"{start}..{head}",
                "head": head,
                "endpoint": head,
                "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
                "last_tag": "",
                "tag_sha": None,
                "saved_marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest() if marker.exists() else None,
                "baseline": base if marker.exists() else None,
            }
        ),
        encoding="utf-8",
    )
    assert ram.main(["write", "--branch", branch, "--sha", head, "--receipt", str(receipt), "--draft", str(draft)]) == 1
    if start_kind == "skips-first-baseline":
        assert not marker.exists()
    else:
        assert marker.read_bytes() == (base + "\n").encode()


def test_write_refuses_saved_marker_changed_after_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A receipt cannot certify notes against a different live saved marker."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commit = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "--allow-empty",
    ]
    subprocess.run([*commit, "-m", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run([*commit, "-m", "next"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    branch = subprocess.check_output(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], text=True).strip()
    monkeypatch.chdir(repo)
    marker = ram._marker_path(branch, None)
    marker.parent.mkdir(parents=True)
    marker.write_bytes((base + "\n").encode())
    draft = repo / "DRAFT.md"
    draft.write_bytes(b"Notes cover next commit\n")
    range_file = repo / "range.txt"
    range_file.write_text(f"{base}..{head}\n", encoding="utf-8")
    receipt = repo / "receipt.json"

    assert (
        ram.main(
            [
                "receipt",
                "--branch",
                branch,
                "--range-file",
                str(range_file),
                "--draft",
                str(draft),
                "--output",
                str(receipt),
            ]
        )
        == 0
    )
    marker.write_bytes((head + "\n").encode())
    assert ram.main(["write", "--branch", branch, "--sha", head, "--receipt", str(receipt), "--draft", str(draft)]) == 1
    assert marker.read_bytes() == (head + "\n").encode()


@pytest.mark.parametrize("selected_tag", [False, True])
def test_first_marker_uses_root_or_selected_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selected_tag: bool
) -> None:
    """A first release accepts its root baseline or an explicit later tag."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commit = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "--allow-empty",
    ]
    subprocess.run([*commit, "-m", "root"], check=True)
    if selected_tag:
        subprocess.run([*commit, "-m", "tagged"], check=True)
        subprocess.run(["git", "-C", str(repo), "tag", "v2"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    branch = subprocess.check_output(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], text=True).strip()
    monkeypatch.chdir(repo)
    draft = repo / "DRAFT.md"
    draft.write_bytes(b"Completed first notes\n")
    range_file = repo / "range.txt"
    range_file.write_text(f"{head}..{head}\n", encoding="utf-8")
    receipt = repo / "receipt.json"
    tag_args = ["--last-tag", "v2"] if selected_tag else []

    assert (
        ram.main(
            [
                "receipt",
                "--branch",
                branch,
                *tag_args,
                "--range-file",
                str(range_file),
                "--draft",
                str(draft),
                "--output",
                str(receipt),
            ]
        )
        == 0
    )
    assert (
        ram.main(
            ["write", "--branch", branch, *tag_args, "--sha", head, "--receipt", str(receipt), "--draft", str(draft)]
        )
        == 0
    )
    assert ram._marker_path(branch, None).read_bytes() == (head + "\n").encode()


def test_first_marker_rejects_ambiguous_history_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A merged unrelated history needs an explicit release baseline."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git = ["git", "-C", str(repo)]
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run([*git, *identity, "commit", "-q", "--allow-empty", "-m", "first-root"], check=True)
    first = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    branch = subprocess.check_output([*git, "symbolic-ref", "--short", "HEAD"], text=True).strip()
    subprocess.run([*git, "checkout", "-q", "--orphan", "other"], check=True)
    subprocess.run([*git, *identity, "commit", "-q", "--allow-empty", "-m", "other-root"], check=True)
    subprocess.run([*git, "checkout", "-q", branch], check=True)
    subprocess.run(
        [*git, *identity, "merge", "-q", "--allow-unrelated-histories", "-s", "ours", "other", "-m", "merged"],
        check=True,
    )
    head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.chdir(repo)
    draft = repo / "DRAFT.md"
    draft.write_bytes(b"Notes\n")
    range_file = repo / "range.txt"
    range_file.write_text(f"{first}..{head}\n", encoding="utf-8")
    receipt = repo / "receipt.json"

    assert (
        ram.main(
            [
                "receipt",
                "--branch",
                branch,
                "--range-file",
                str(range_file),
                "--draft",
                str(draft),
                "--output",
                str(receipt),
            ]
        )
        == 1
    )
    assert not receipt.exists()


# ---------------------------------------------------------------------------
# CLI: is-valid
# ---------------------------------------------------------------------------


def test_is_valid_cli_prints_false_when_no_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No marker on disk → CLI prints "false"."""
    rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v1.0.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "false"


def test_is_valid_cli_prints_true_for_valid_unsuperseded_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker is an ancestor of HEAD and the tag predates it → CLI prints "true"."""
    _write_marker(tmp_path, "deadbeef\n", encoding="utf-8")
    _sequenced_run(monkeypatch, 0, 1)  # ancestor-of-HEAD: yes; ancestor-of-tag: no (tag predates marker)
    rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v1.0.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "true"


def test_is_valid_cli_prints_false_when_superseded_by_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker valid but a release tag landed at/after it → CLI prints "false" (tag-blind fix)."""
    _write_marker(tmp_path, "deadbeef\n", encoding="utf-8")
    _sequenced_run(monkeypatch, 0, 0)  # ancestor-of-HEAD: yes; ancestor-of-tag: yes (superseded)
    rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v2.0.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "false"


def test_is_valid_cli_prints_false_when_rebased_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker no longer an ancestor of HEAD (rebase/force-push) → CLI prints "false"."""
    _write_marker(tmp_path, "deadbeef\n", encoding="utf-8")
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=1))
    rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v1.0.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "false"


# ---------------------------------------------------------------------------
# CLI: resolve
# ---------------------------------------------------------------------------


def test_resolve_no_marker_falls_back_to_last_tag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No prior marker → RANGE = <last-tag>..HEAD; stderr notes first-baseline."""
    rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "v1.2.0..HEAD"
    assert "establishing first append baseline" in captured.err


def test_resolve_valid_marker_uses_incremental_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker valid + tag predates it → RANGE = <sha>..HEAD, not <last-tag>..HEAD."""
    _write_marker(tmp_path, "deadbeef1234\n", encoding="utf-8")
    _sequenced_run(monkeypatch, 0, 1)  # ancestor-of-HEAD: yes; ancestor-of-tag: no
    rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "deadbeef1234..HEAD"


def test_resolve_invalid_marker_falls_back_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker present but not resolvable in history (rebase-dangling) → falls back, stderr warns."""
    _write_marker(tmp_path, "deadbeef1234\n", encoding="utf-8")
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=1))
    rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "v1.2.0..HEAD"
    assert "not found in history" in captured.err


def test_resolve_marker_valid_but_superseded_by_later_tag_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tag-blind fix: a release cut between the marker and now overrides a stale-but-reachable marker."""
    _write_marker(tmp_path, "deadbeef1234\n", encoding="utf-8")
    _sequenced_run(monkeypatch, 0, 0)  # ancestor-of-HEAD: yes; ancestor-of-tag: yes (tag cut at/after marker)
    rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v2.0.0", "--marker-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "v2.0.0..HEAD"
    assert "superseded by a release tag" in captured.err


def test_resolve_records_git_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Record reachability checks against both ``HEAD`` and the latest tag."""
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record both marker validation checks and report success for each."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0)

    _write_marker(tmp_path, "deadbeef\n", encoding="utf-8")
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_run)
    ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
    assert recorded[0] == ["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", "deadbeef", "HEAD"]
    assert recorded[1] == ["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", "deadbeef", "v1.2.0"]
