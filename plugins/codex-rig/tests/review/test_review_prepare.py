"""Exercise deterministic review preparation without model-authored provenance."""

from __future__ import annotations

import atexit
import functools
import hashlib
import importlib.util
import io
import itertools
import json
import math
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills/code-review"
HELPER = SKILL / "review_prepare.py"
_CONTEXT_READER = runpy.run_path(str(SKILL / "review_context.py"))["read_context"]
_CONTEXT_READ_CALL = runpy.run_path(str(SKILL / "review_context.py"))["render_read_call"]


#: Built review-input trees keyed by options and build environment; read only, copied for every caller.
_REVIEW_INPUT_TEMPLATES: dict[tuple[object, ...], Path] = {}


def _remove_review_input_templates(root: Path) -> None:
    """Delete this worker's template trees, including Git's read-only object files on Windows."""

    def retry_writable(function: Callable[[str], object], path: str, _: object) -> None:
        """Clear the read-only bit Git sets on objects, then retry the failed removal."""
        os.chmod(path, 0o700)
        function(path)

    # Python 3.12 renamed the error hook; both receive the same three arguments.
    handler = "onexc" if sys.version_info >= (3, 12) else "onerror"
    shutil.rmtree(root, **{handler: retry_writable})


@functools.cache
def _review_input_template_root() -> Path:
    """Create this worker's template directory once and remove it when the interpreter exits."""
    root = Path(tempfile.mkdtemp(prefix="review-inputs-")).resolve()
    atexit.register(_remove_review_input_templates, root)
    return root


def _review_inputs(
    tmp_path: Path,
    *,
    source_name: str = "widget.py",
    untracked: bool = False,
    second_file: bool = False,
    unchanged_caller: bool = False,
    deleted: bool = False,
    large_source: bool = False,
    large_patch: bool = False,
    unchanged: bool = False,
) -> Path:
    """Give the caller a private review-input tree without rebuilding identical Git evidence per test.

    Building the tree runs Git and the shipped diff collector about sixty times, which dominates these tests on
    Windows. The first request for an option set builds it once in a worker-private template directory; every caller
    receives its own copy whose absolute paths are rebased onto ``tmp_path``. The result is the tree the collector
    would have produced there: the receipt and snapshot paths are rewritten, the detached worktree is relinked to the
    copied repository, and Git-derived digests stay valid because they cover only repository-relative content.
    The cache key includes the process environment, working directory, interpreter, and the ``Path`` text and byte
    writers, so a test that changes Git configuration or text defaults before building still gets a fresh build under
    its own conditions. A shared fixture that patches the text writer tags it with a ``template_identity`` so every
    test under the same default reuses one tree instead of rebuilding for a fresh closure.
    """
    options = {
        "source_name": source_name,
        "untracked": untracked,
        "second_file": second_file,
        "unchanged_caller": unchanged_caller,
        "deleted": deleted,
        "large_source": large_source,
        "large_patch": large_patch,
        "unchanged": unchanged,
    }
    environment = frozenset(item for item in os.environ.items() if item[0] != "PYTEST_CURRENT_TEST")
    writer = getattr(Path.write_text, "template_identity", Path.write_text)
    key = (tuple(options.items()), environment, os.getcwd(), sys.executable, writer, Path.write_bytes)
    template = _REVIEW_INPUT_TEMPLATES.get(key)
    if template is None:
        template = _review_input_template_root() / str(len(_REVIEW_INPUT_TEMPLATES))
        template.mkdir()
        _build_review_inputs(template, **options)
        _REVIEW_INPUT_TEMPLATES[key] = template
    return _relocate_review_inputs(template, tmp_path)


def _relocate_review_inputs(template: Path, tmp_path: Path) -> Path:
    """Copy one template tree into ``tmp_path`` and rebase every recorded absolute location onto the copy."""
    target = tmp_path.resolve()
    for name in ("repository", "review"):
        shutil.copytree(template / name, target / name, symlinks=True)
    # The collector writes resolved POSIX paths into ASCII-escaped JSON receipts.
    old_text, new_text = (json.dumps(path.as_posix())[1:-1] for path in (template, target))
    for receipt in sorted((target / "review/local-source").glob("*.json")):
        content = receipt.read_text(encoding="utf-8")
        receipt.write_text(content.replace(old_text, new_text), encoding="utf-8", newline="\n")
    # Git links a linked worktree to its administrative directory through two absolute "gitdir" files.
    administrative = target / "repository/.git/worktrees"
    for admin in sorted(administrative.iterdir()) if administrative.is_dir() else []:
        linked = Path((admin / "gitdir").read_text(encoding="utf-8").strip())
        worktree_git = target / linked.relative_to(template)
        (admin / "gitdir").write_text(worktree_git.as_posix() + "\n", encoding="utf-8", newline="\n")
        # Git for Windows marks a file named .git hidden, Python 3.12+ copytree preserves that attribute through
        # CopyFile2, and truncating a hidden file with open(..., "w") fails with PermissionError there. Replacing the
        # copy sidesteps the truncate; no assertion depends on the hidden attribute.
        worktree_git.unlink()
        worktree_git.write_text(f"gitdir: {admin.as_posix()}\n", encoding="utf-8", newline="\n")
    template_markers = {os.fsencode(form) for form in (template.as_posix(), str(template), old_text)}
    stale = [
        path
        for path in target.rglob("*")
        if path.is_file() and not path.is_symlink() and any(marker in path.read_bytes() for marker in template_markers)
    ]
    assert not stale, f"review-input-template-path-retained:{stale}"
    return tmp_path / "review"


def _build_review_inputs(
    tmp_path: Path,
    *,
    source_name: str = "widget.py",
    untracked: bool = False,
    second_file: bool = False,
    unchanged_caller: bool = False,
    deleted: bool = False,
    large_source: bool = False,
    large_patch: bool = False,
    unchanged: bool = False,
) -> Path:
    """Write semantic reviewer decisions while leaving mechanical evidence to the producer."""
    spec = importlib.util.spec_from_file_location("prepare_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = tmp_path / "review"
    run.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    # Git checks out unchanged callers from HEAD, so writer LF alone cannot fix their checkout bytes.
    for command in (
        ["init", "-q"],
        ["config", "core.autocrlf", "false"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(["git", "-C", str(repository), *command], check=True, capture_output=True)
    stable_source = "unchanged = 'café'\n" * 18000 if large_source else ""
    (repository / source_name).write_text("value = 1\n" + stable_source, encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(repository), "add", source_name], check=True, capture_output=True)
    if second_file:
        (repository / "other.py").write_text("other = 1\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(repository), "add", "other.py"], check=True, capture_output=True)
    if unchanged_caller:
        (repository / "stable.py").write_text("caller = 5\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(repository), "add", "stable.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "Initial"], check=True, capture_output=True)
    (repository / source_name).write_text(
        "value = 2\n" + ("changed = 'café'\n" * 18000 if large_patch else stable_source), encoding="utf-8", newline="\n"
    )
    if unchanged:
        (repository / source_name).write_text("value = 1\n" + stable_source, encoding="utf-8", newline="\n")
    if second_file:
        (repository / "other.py").write_text("other = 2\n", encoding="utf-8", newline="\n")
    if deleted:
        (repository / source_name).unlink()
    if untracked:
        (repository / "new.txt").write_text("new_value = 7\n", encoding="utf-8", newline="\n")
    if unchanged:
        subprocess.run(["git", "-C", str(repository), "checkout", "--detach", "-q"], check=True, capture_output=True)
        (run / "diff.patch").write_bytes(b"")
    else:
        collected = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "shared/collect_diff.py"),
                "--review-worktree",
                "--repository",
                str(repository),
                "--out",
                str(run / "local-source"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert collected.returncode == 0, collected.stderr
        (run / "diff.patch").write_bytes((run / "local-source/diff.patch").read_bytes())
    if untracked:
        (run / "untracked.txt").write_text("new.txt\n", encoding="utf-8", newline="\n")
    roles = ["challenger", "qa-specialist"]
    routing = {
        "schema_version": 1,
        "risk_tier": "HIGH_RISK",
        "signals": dict.fromkeys(module.ROUTING_SIGNALS, False),
        "signal_evidence": {name: ["Scope checked."] for name in module.ROUTING_SIGNALS},
        "triggered_roles": roles,
        "trigger_reasons": {role: ["High-risk behavior."] for role in roles},
    }
    (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    briefs = {}
    for role in roles:
        (run / f"{role}-evidence.md").write_text(
            f"Scope: {source_name} at frozen revision. Excluded: unrelated callers.\nQuestion: {role} axis.\n"
            + "Relevant source evidence.\n" * 300,
            encoding="utf-8",
            newline="\n",
        )
        briefs[role] = {"axis": role, "evidence_path": f"{role}-evidence.md", "source_paths": [source_name]}
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    return run


def _conditional_review_inputs(tmp_path: Path) -> Path:
    """Declare a genuine non-Python documentation scope with only conditional reviewer axes."""
    run = _review_inputs(tmp_path, source_name="guide.md")
    roles = ["doc-scribe", "web-explorer"]
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing["signals"].update(axis_doc_scribe=True, axis_web_explorer=True)
    routing.update(
        risk_tier="LOCAL",
        triggered_roles=roles,
        trigger_reasons={role: ["Documentation source inspection."] for role in roles},
        independent_review_required=False,
        independence_requirement_evidence=None,
    )
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    briefs = {
        role: {"axis": role, "evidence_path": "qa-specialist-evidence.md", "source_paths": ["guide.md"]}
        for role in roles
    }
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    return run


@pytest.mark.integration
def test_fast_conditional_native_roster_preserves_independence_policy(tmp_path: Path) -> None:
    """Admit complete conditional-only launch evidence without changing core-role independence metadata."""
    run = _conditional_review_inputs(tmp_path)
    prepared = _prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    run, home, _ = _assembly_evidence(tmp_path, prepared_run=run, active_limit=1)
    assembled = _assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    assert {item["role"] for item in manifest["passes"]} == {"doc-scribe", "web-explorer"}
    assert summary["actual_mode"] == "independent-spawned"
    assert summary["capacity_limited"] is False
    assert summary["independence_required"] is False
    assert summary["independence_satisfied"] is False


def test_review_source_fixture_preserves_utf8_lf_under_windows_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep frozen source bytes portable under Windows text and Git checkout defaults."""
    write_text = Path.write_text
    git_config = tmp_path / "gitconfig"
    git_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def windows_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        """Apply Windows text defaults unless the fixture explicitly selects its byte format."""
        return write_text(path, data, encoding=encoding or "cp1252", errors=errors, newline=newline or "\r\n")

    monkeypatch.setattr(Path, "write_text", windows_write_text)
    run = _review_inputs(tmp_path, large_source=True, second_file=True, unchanged_caller=True)
    root = Path(json.loads((run / "local-source/review-worktree.json").read_bytes())["review_worktree"])
    source = (root / "widget.py").read_bytes()

    assert source == ("value = 2\n" + "unchanged = 'café'\n" * 18000).encode("utf-8")
    assert (root / "other.py").read_bytes() == b"other = 2\n"
    assert (root / "stable.py").read_bytes() == b"caller = 5\n"


def _prepare(
    run: Path,
    *,
    source_root: Path | None = None,
    expected_head: str | None = None,
    scope_path: str | None = None,
    challenge_only: bool = False,
    batches: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke the shipped producer as an installed-path command."""
    if source_root is None:
        source_root = Path(
            json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
        )
    command = [
        sys.executable,
        str(HELPER),
        "prepare",
        "--out",
        str(run),
        "--run-id",
        "bounded-review",
        "--parent-thread-id",
        "parent",
        "--source-root",
        str(source_root),
    ]
    if challenge_only:
        command.append("--challenge-only")
    if batches:
        command.append("--batches")
    if expected_head is not None:
        command.extend(["--expected-head", expected_head])
        base = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", f"{expected_head}^"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        command.extend(["--expected-diff-base", base])
    if scope_path is not None:
        command.extend(["--scope-path", scope_path])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_prepare_rejects_missing_signals_without_rewriting_routing(tmp_path: Path) -> None:
    """Reject omitted semantic booleans before canonicalization or reviewer preparation."""
    run = _review_inputs(tmp_path)
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    del routing["signals"]
    original = json.dumps(routing).encode("utf-8")
    routing_path.write_bytes(original)

    result = _prepare(run)

    assert result.returncode != 0
    assert "review-routing-signal-set-mismatch" in result.stderr
    assert routing_path.read_bytes() == original
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


def test_prepare_rejects_omitted_local_diff(tmp_path: Path) -> None:
    """Reject a complete checkout paired with an incomplete admitted patch."""
    run = _review_inputs(tmp_path, second_file=True)
    root = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    patch = subprocess.run(
        ["git", "-C", root, "diff", "--binary", "HEAD", "--", "widget.py"], check=True, capture_output=True
    ).stdout
    (run / "diff.patch").write_bytes(patch)
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-source-diff-stale" in result.stderr
    assert not (run / "dispatch.json").exists()


def test_prepare_rejects_missing_untracked_inventory(tmp_path: Path) -> None:
    """Keep retained untracked changes from disappearing before reviewer dispatch."""
    run = _review_inputs(tmp_path, untracked=True)
    (run / "untracked.txt").unlink()
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-source-untracked-stale" in result.stderr
    assert not (run / "dispatch.json").exists()


def test_prepare_accepts_declared_local_path_scope(tmp_path: Path) -> None:
    """Allow intentional path reviews without silently dropping whole-tree evidence."""
    run = _review_inputs(tmp_path, second_file=True, untracked=True)
    root = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    patch = subprocess.run(
        ["git", "-C", root, "diff", "--binary", "HEAD", "--", "widget.py"], check=True, capture_output=True
    ).stdout
    (run / "diff.patch").write_bytes(patch)
    (run / "untracked.txt").write_bytes(b"")
    result = _prepare(run, scope_path="widget.py")
    assert result.returncode == 0, result.stderr
    assert (run / "dispatch.json").exists()


def test_prepare_rejects_undelivered_changed_source(tmp_path: Path) -> None:
    """A full patch cannot hide a changed file from every reviewer context."""
    run = _review_inputs(tmp_path, second_file=True)
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-source-coverage-incomplete" in result.stderr
    diagnostic = json.loads(result.stderr.split("review-source-coverage-incomplete:", 1)[1])
    assert diagnostic == {"missing_diff_paths": [], "missing_source_paths": ["other.py"]}
    assert not (run / "dispatch.json").exists()


@pytest.mark.parametrize("default_encoding", ["utf-8", "cp1252"])
def test_prepare_accepts_complete_split_source_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_encoding: str
) -> None:
    """Reviewers may receive disjoint source while collectively covering every change."""
    read_text = Path.read_text

    def read_with_default(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        """Simulate the platform default while respecting explicit artifact encodings."""
        return read_text(path, encoding=encoding or default_encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_with_default)
    run = _review_inputs(tmp_path, second_file=True)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"] = ["other.py"]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    for context in plan["contexts"]:
        text = (run / context["context_path"]).read_text(encoding="utf-8")
        assert ("other = 2" in text) is (context["role_id"] == "challenger")
        assert ("value = 2" in text) is (context["role_id"] != "challenger")


@pytest.mark.parametrize("route", ["local", "commit", "pr"])
def test_prepare_delivers_deleted_file_comparison(tmp_path: Path, route: str) -> None:
    """Deliver exact deletion evidence even when the reviewed tip has no file record."""
    run = _review_inputs(tmp_path, deleted=route == "local")
    root = Path(json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"])
    head = None
    if route != "local":
        (root / "widget.py").unlink()
        subprocess.run(["git", "-C", str(root), "add", "widget.py"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "Delete",
            ],
            check=True,
            capture_output=True,
        )
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        base = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^"], check=True, capture_output=True, text=True
        ).stdout.strip()
        comparison = [f"{base}...{head}"] if route == "pr" else [base, head]
        (run / "diff.patch").write_bytes(
            subprocess.run(
                ["git", "-C", str(root), "diff", "--binary", *comparison, "--"], check=True, capture_output=True
            ).stdout
        )
        (run / "local-source/review-worktree.json").unlink()
        if route == "pr":
            (run / "local-checkout.json").write_text(
                json.dumps(
                    {"worktree": root.as_posix(), "expected_head": head, "diff_base_oid": base, "diff_head_oid": head}
                ),
                encoding="utf-8",
                newline="\n",
            )
    result = _prepare(run, source_root=root, expected_head=head)
    assert result.returncode == 0, result.stderr
    text = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert "widget.py (missing, SHA-256: None)" in text
    assert "deleted file mode" in text
    assert "-value = 1" in text
    assert not (root / "widget.py").exists()


@pytest.mark.parametrize("tampered", [False, True])
@pytest.mark.parametrize("scope_path", [None, "widget.py"])
def test_prepare_binds_preexisting_unchanged_caller(tmp_path: Path, tampered: bool, scope_path: str | None) -> None:
    """Admit HEAD-backed context while rejecting mutation after local collection."""
    run = _review_inputs(tmp_path, unchanged_caller=True)
    root = Path(json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"])
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"].append("stable.py")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    if tampered:
        (root / "stable.py").write_text("caller = 999\n", encoding="utf-8", newline="\n")
    result = _prepare(run, scope_path=scope_path)
    if tampered:
        assert result.returncode != 0
        assert "changed after collection" in result.stderr
        assert not (run / "dispatch.json").exists()
    else:
        assert result.returncode == 0, result.stderr
        text = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
        assert "caller = 5\n" in text
        assert "+value = 2" in text


def test_prepare_rejects_undeclared_missing_context(tmp_path: Path) -> None:
    """Missing context requires a verified deletion rather than a nonexistent brief path."""
    run = _review_inputs(tmp_path)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"].append("never-existed.py")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode == 2
    assert "review-brief-source-selection-invalid:challenger" in result.stderr
    assert not (run / "dispatch.json").exists()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write synthetic native rollout rows with portable line endings."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _record_native_schedule(
    run: Path,
    home: Path,
    children: dict[str, Path],
    *,
    pool_size: int = 4,
    scenario: str = "proper-refill",
    wave_index: int = 0,
) -> None:
    """Record coherent external allocation, task completion and final-answer join chronology."""
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    queue = [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in dispatch["calls"]
    ]
    actual = list(reversed(queue)) if scenario == "reversed-largest-order" else queue
    epoch = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp() + wave_index * 1000
    launches = {}
    joins = {}
    active_ends = []
    paths = {}

    def stamp(value: float) -> str:
        """Serialize fixture timestamps using the host's UTC event format."""
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    for index, role in enumerate(actual):
        if index < pool_size:
            start = epoch + 0.2 + index / 10
            end = epoch + ([5, 5.01, 20, 25][index] if scenario == "blocked-wait-coalesces-joins" else 5 + index * 5)
            if scenario == "join-before-terminal" and index == 0:
                end = epoch + 8
        else:
            released = min(active_ends)
            active_ends.remove(released)
            start = (
                max(joins.values())
                if scenario in {"wait-for-all-before-refill", "wait-again-with-free-slot"}
                else released
            ) + 0.25
            if scenario == "blocked-wait-coalesces-joins":
                start = epoch + 15.3
            elif "wait-again-with-free-slot" in scenario:
                start = epoch + 45.3
            elif scenario == "join-before-terminal":
                start = epoch + 8.3
            end = start + 10
        launch = epoch + 0.05 + index / 100 if scenario == "spawn-all-five" else start - 0.02
        if scenario == "join-before-terminal" and index >= pool_size:
            launch = epoch + 5.28
        launches[role] = launch
        joins[role] = epoch + 5.05 if scenario == "join-before-terminal" and index == 0 else end + 0.05
        active_ends.append(joins[role])
        rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        rows[0]["payload"]["timestamp"] = stamp(launch + 0.005)
        paths[rows[0]["payload"]["agent_path"]] = role
        terminal = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "task_complete")
        terminal.update(started_at=start, completed_at=end)
        _write_jsonl(children[role], rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    call_roles = {}
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
            arguments = json.loads(payload["arguments"])
            matches = [role for path, role in paths.items() if path.rsplit("/", 1)[-1] == arguments["task_name"]]
            if matches:
                role = matches[0]
                call_roles[payload["call_id"]] = role
                row["timestamp"] = stamp(launches[role])
        elif payload.get("type") == "function_call_output" and payload.get("call_id") in call_roles:
            row["timestamp"] = stamp(launches[call_roles[payload["call_id"]]] + 0.01)
        elif payload.get("type") == "agent_message" and payload.get("author") in paths:
            row["timestamp"] = stamp(joins[paths[payload["author"]]])
    if scenario in {"capacity-rejected", "capacity-rejected-repeat"}:
        # Existing rollout envelope, exact observed live refusal text. The host's
        # serialization of a failed spawn is not asserted by this parser fixture.
        targets = range(pool_size, len(actual)) if scenario == "capacity-rejected-repeat" else [pool_size]
        for index in targets:
            next_role = actual[index]
            successful = next(
                row
                for row in parent
                if row.get("payload", {}).get("type") == "function_call"
                and row["payload"].get("call_id") in call_roles
                and call_roles[row["payload"]["call_id"]] == next_role
            )
            refusal = json.loads(json.dumps(successful))
            refusal_id = f"capacity-refusal-{wave_index}-{index}"
            rejected_at = epoch + 1 if index == pool_size else launches[actual[index - 1]] + 0.5
            refusal["timestamp"] = stamp(rejected_at)
            refusal["payload"]["call_id"] = refusal_id
            parent.extend(
                [
                    refusal,
                    {
                        "type": "response_item",
                        "timestamp": stamp(rejected_at + 0.01),
                        "payload": {
                            "type": "function_call_output",
                            "call_id": refusal_id,
                            "output": "collab spawn failed: agent thread limit reached",
                        },
                    },
                ]
            )
    if scenario == "blocked-wait-coalesces-joins" or "wait-again-with-free-slot" in scenario:
        # A matched wait result marks parent resumption. Joins delivered while
        # this wait is pending alone do not establish scheduling opportunity.
        arguments = (
            {} if scenario.startswith("default-") else {"timeout_ms": 30000 if scenario.startswith("30s-") else 10000}
        )
        waits = [(4.9, 15.1)]
        if "wait-again-with-free-slot" in scenario:
            waits.append((15.2, 45.2))
        for index, (started, returned) in enumerate(waits):
            call_id = f"schedule-wait-{wave_index}-{index}"
            timed_out = not scenario.startswith("completed-") and not (
                index == 0 and scenario.startswith(("default-", "30s-"))
            )
            output = {"message": "Wait timed out." if timed_out else "Wait completed.", "timed_out": timed_out}
            parent.extend(
                [
                    {
                        "type": "response_item",
                        "timestamp": stamp(epoch + started),
                        "payload": {
                            "type": "function_call",
                            "name": "wait_agent",
                            "call_id": call_id,
                            "arguments": json.dumps(arguments),
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": stamp(epoch + returned),
                        "payload": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(output),
                        },
                    },
                ]
            )
    parent.sort(key=lambda row: row.get("timestamp", ""))
    _write_jsonl(parent_path, parent)


def _assembly_evidence(
    tmp_path: Path,
    *,
    malformed_continuation: bool = False,
    prepared_run: Path | None = None,
    home: Path | None = None,
    wave_index: int = 0,
    final_header: str = "complete",
    findings: dict[str, str] | None = None,
    blocking_counts: dict[str, int] | None = None,
    active_limit: int | None = None,
    compact_reader: bool = False,
) -> tuple[Path, Path, dict[str, Path]]:
    """Record dispatched calls, real context-reader output, and completed child turns."""
    run = prepared_run if prepared_run is not None else _review_inputs(tmp_path)
    if prepared_run is None:
        prepared = _prepare(run)
        assert prepared.returncode == 0, prepared.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    if not compact_reader:
        # Existing copy-error regressions deliberately exercise issued historical long-frame recipes.
        for call in dispatch["calls"]:
            if 'store("review-context-' in call["arguments"]["message"]:
                call["arguments"]["message"] = runpy.run_path(str(SKILL / "review_context.py"))["dispatch_message"](
                    run / "inspection-plan.json", call["role"], provenance_header=False
                )
        dispatch["dispatch_bytes"] = sum(len(call["arguments"]["message"].encode()) for call in dispatch["calls"])
        (run / "dispatch.json").write_bytes((json.dumps(dispatch, indent=2, sort_keys=True) + "\n").encode())
    if malformed_continuation:
        message = dispatch["calls"][0]["arguments"]["message"]
        dispatch["calls"][0]["arguments"]["message"] = message.replace(" --page 2", " --page WRONG", 1)
    home = home if home is not None else tmp_path / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    parent_path = sessions / "rollout-parent.jsonl"
    parent_rows: list[dict[str, object]] = (
        [json.loads(row) for row in parent_path.read_text(encoding="utf-8").splitlines()]
        if parent_path.exists()
        else [{"type": "session_meta", "payload": {"id": "parent"}}]
    )
    children = {}
    calls_by_role = {
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-"): call
        for call in dispatch["calls"]
    }
    for index, context in enumerate(plan["contexts"], start=1):
        role = context["role_id"]
        call = calls_by_role[role]
        arguments = call["arguments"]
        agent_path = f"/root/{arguments['task_name']}"
        identity = f"{wave_index}-{index}" if prepared_run is not None else str(index)
        thread = f"child-{identity}"
        turn = f"turn-{identity}"
        call_id = f"spawn-{identity}"
        message = arguments["message"]
        page_match = re.search(r"Read all (\d+) frozen review context pages", message)
        assert page_match is not None, "compact-review-page-count-invalid"
        page_count = int(page_match.group(1))
        if prepared_run is None:
            assert page_count > 1
        read_calls = re.findall(r"```javascript\n(.*?)\n```", message, flags=re.DOTALL)
        assert len(read_calls) == page_count, "review-dispatch-page-count-invalid"
        for page, read_call in enumerate(read_calls, 1):
            canonical = (
                _CONTEXT_READ_CALL(run / "inspection-plan.json", role, 1, sys.executable, page)
                if compact_reader
                else read_call
            )
            read_arguments = json.loads(canonical.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
            assert read_arguments["cmd"].endswith("--attempt 1" if page == 1 else f"--page {page}"), (
                "review-dispatch-page-command-invalid"
            )
        tool_rows = []
        header = ""
        for page, read_call in enumerate(read_calls, start=1):
            output = _CONTEXT_READER(run / "inspection-plan.json", role, 1, page)
            if page == 1:
                header = output.splitlines()[0]
            read_id = f"read-{index}-{page}"
            tool_rows.extend(
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "call_id": read_id,
                            "input": read_call,
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": read_id,
                            "output": [
                                {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
                                {"type": "input_text", "text": output},
                            ],
                        },
                    },
                ]
            )
        if compact_reader:
            # Native compact admission must prove the expanded command, not merely synthesize matching stdout.
            for page in range(1, page_count + 1):
                canonical = _CONTEXT_READ_CALL(run / "inspection-plan.json", role, 1, sys.executable, page)
                args = json.loads(canonical.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
                tool_rows.append(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "CommandExecution",
                                "command": [args["cmd"]],
                                "exit_code": 0,
                                "stdout": _CONTEXT_READER(run / "inspection-plan.json", role, 1, page),
                                "stderr": "",
                            },
                        },
                    }
                )
        if final_header == "missing":
            header = ""
        elif final_header == "truncated":
            header = header.replace(f"input={plan['review_input_sha256']}", f"input={plan['review_input_sha256'][:-1]}")
        final = (
            f"{header}\nNo finding.\n\n## Reviewer Assessment\n\nRating: 1\nRationale: The inspected scope is clean."
        )
        if run.parent.name == "batches":
            final = '## Reviewer Findings\n```json\n[]\n```\n\n## Reviewer Confidence\n```json\n{"score": 0.95, "scope": "Frozen source and declared interactions.", "gaps": [{"gap": "Synthetic offline client.", "status": "unresolved", "rationale": "Fixture proves admission without a live semantic reviewer (-0.05)."}]}\n```\n\n## Reviewer Assessment\nRating: 1\nRationale: The inspected scope is clean.'
        if findings and role in findings:
            final = ("" if run.parent.name == "batches" else header + "\n") + findings[role]
        parent_rows.extend(
            [
                {
                    "timestamp": "2026-01-01T10:00:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": call_id,
                        "arguments": json.dumps(arguments),
                    },
                },
                {
                    "timestamp": "2026-01-01T10:00:00.100Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"task_name": agent_path}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": agent_path,
                        "content": [{"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{final}"}],
                    },
                },
            ]
        )
        child = sessions / f"rollout-{thread}.jsonl"
        _write_jsonl(
            child,
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread,
                        "timestamp": "2026-01-01T10:00:00.050Z",
                        "parent_thread_id": "parent",
                        "agent_path": agent_path,
                        "agent_role": "default",
                        "source": {
                            "subagent": {"thread_spawn": {"parent_thread_id": "parent", "agent_path": agent_path}}
                        },
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {"turn_id": turn, "model": arguments["model"], "effort": arguments["reasoning_effort"]},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "content": [{"type": "encrypted_content", "encrypted_content": arguments["message"]}],
                    },
                },
                *tool_rows,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn,
                        "started_at": 100
                        + index
                        + wave_index * 1000
                        + ((index - 1) // active_limit * 20 if active_limit else 0),
                        "completed_at": 110
                        + index
                        + wave_index * 1000
                        + ((index - 1) // active_limit * 20 if active_limit else 0),
                        "last_agent_message": final,
                    },
                },
            ],
        )
        children[role] = child
    _write_jsonl(sessions / "rollout-parent.jsonl", parent_rows)
    _record_native_schedule(run, home, children, pool_size=active_limit or 4, wave_index=wave_index)
    (run / "specialist-assessments.json").write_text(
        json.dumps(
            {
                role: {
                    "confidence": 0.95,
                    "blocking_findings": blocking_counts.get(role, 0)
                    if blocking_counts is not None
                    else sum(
                        record["severity"] != "low"
                        for record in json.loads(re.search(r"```json\n(.*?)\n```", findings[role], re.DOTALL)[1])
                    )
                    if run.parent.name == "batches" and findings and role in findings and "```json\n" in findings[role]
                    else 0,
                }
                for role in children
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    return run, home, children


def _assemble(run: Path, home: Path, *, challenge_only: bool = False) -> subprocess.CompletedProcess[str]:
    """Invoke the shipped assembly command against synthetic native sessions."""
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "assemble",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            *(["--challenge-only"] if challenge_only else []),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("challenge", "cases"),
    [
        pytest.param(
            False,
            [
                "repair",
                "parent-miscopy",
                "wrapper-control",
                "wrapper-prefix",
                "wrapper-extra",
                "source-read",
                "no-diagnostic",
                "wrong-parent",
            ],
            id="five-role-wave",
        ),
        pytest.param(
            True,
            [
                "challenge-raw",
                "challenge-fenced",
                "challenge-nonblocker",
                "challenge-malformed",
                "challenge-malformed-shape",
                "challenge-unproven",
                "challenge-partial-digest",
                "challenge-with-findings",
            ],
            id="challenge-only",
        ),
    ],
)
def test_same_wave_dispatch_repair_requires_observed_preassessment_failure(
    tmp_path: Path, subtests: pytest.Subtests, challenge: bool, cases: list[str]
) -> None:
    """Recover a failed reader dispatch without dropping original allocation or valid sibling evidence.

    Every case of one wave shape alters the same prepared and assembled wave, so that wave is built once per shape and
    each case runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children, digests = _same_wave_repair_base(tmp_path, challenge=challenge)
    assembled = _tree_state(tmp_path)
    for case in cases:
        with subtests.test(case=case):
            _restore_tree(tmp_path, assembled)
            _check_same_wave_repair(run, home, children, case, challenge=challenge, digests=digests)


def _same_wave_repair_base(
    tmp_path: Path, *, challenge: bool
) -> tuple[Path, Path, dict[str, Path], tuple[str, str] | None]:
    """Prepare and record one complete five-role or challenge-only wave before a reader dispatch fails."""
    run = _review_inputs(tmp_path) if challenge else _five_role_review_inputs(tmp_path)
    digests = None
    final_message = ""
    if challenge:
        source_bytes = (run / "local-source/source-snapshot.json").read_bytes()
        diff_bytes = (run / "diff.patch").read_bytes()
        source_digest, diff_digest = digests = tuple(
            hashlib.sha256(content).hexdigest() for content in (source_bytes, diff_bytes)
        )
        request = {
            "goal": "Review the frozen implementation",
            "specification": f"Use source_sha256={source_digest} and diff_sha256={diff_digest}; return structured findings and assessment.",
            "done_when": "Independent source inspection completed.",
        }
        brief = f"Review request:\n{json.dumps(request, sort_keys=True)}\nFrozen source:\n{source_bytes.decode('utf-8')}\nFrozen diff:\n{diff_bytes.decode('utf-8')}\n"
        (run / "challenger-evidence.md").write_bytes(brief.encode("utf-8"))
        final_message = json.dumps(
            {
                "source_sha256": source_digest,
                "diff_sha256": diff_digest,
                "findings": [],
                "assessment": {"rating": 1, "rationale": "Completed frozen review."},
            }
        )
        routing = json.loads((run / "review-routing.json").read_text(encoding="utf-8"))
        routing.update(triggered_roles=["challenger"], trigger_reasons={"challenger": ["Bounded challenge."]})
        (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
        briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
        (run / "review-briefs.json").write_text(
            json.dumps({"challenger": briefs["challenger"]}), encoding="utf-8", newline="\n"
        )
    assert _prepare(run, challenge_only=challenge).returncode == 0
    run, home, children = _assembly_evidence(
        tmp_path,
        prepared_run=run,
        active_limit=4,
        findings={"challenger": final_message} if challenge else None,
        final_header="missing" if challenge else "complete",
    )
    return run, home, children, digests


def _check_same_wave_repair(
    run: Path,
    home: Path,
    children: dict[str, Path],
    case: str,
    *,
    challenge: bool,
    digests: tuple[str, str] | None,
) -> None:
    """Replace one reader dispatch with an observed or unproved failure and check repair plus reassembly."""
    if challenge:
        source_digest, diff_digest = digests
    role = "challenger" if challenge else "sw-engineer"
    child = children[role]
    original_rows = [json.loads(line) for line in child.read_text().splitlines()]
    rows = json.loads(json.dumps(original_rows))
    tool_rows = [
        row
        for row in rows
        if row["type"] == "response_item"
        and row["payload"].get("type") in {"custom_tool_call", "custom_tool_call_output"}
    ]
    first_call = tool_rows[0]
    first_call["payload"]["input"] = first_call["payload"]["input"].replace("review_context.py", "review_prepare.py")
    output = tool_rows[1]
    if case != "source-read":
        output["payload"]["output"] = [
            {"type": "input_text", "text": "Script completed\n"},
            {
                "type": "input_text",
                "text": "usage: review_prepare.py\nerror: unsupported arguments"
                if case not in {"no-diagnostic", "challenge-unproven"}
                else "Unknown failure",
            },
        ]
    rows = [row for row in rows if row not in tool_rows] + [first_call, output]
    if case.startswith("wrapper-"):
        cmd = json.loads(first_call["payload"]["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])[
            "cmd"
        ]
        wrapper = ["/bin/bash", "-lc", cmd]
        if case == "wrapper-prefix":
            wrapper.insert(2, "printf unrelated-effect")
        elif case == "wrapper-extra":
            wrapper.append("extra-positional")
        rows.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": wrapper,
                        "exit_code": 2,
                        "stdout": "",
                        "stderr": output["payload"]["output"][1]["text"],
                    },
                },
            }
        )
    terminal = next(
        row["payload"] for row in rows if row["type"] == "event_msg" and row["payload"].get("type") == "task_complete"
    )
    blocked_message = "Reader dispatch failed before source inspection.\n\n## Reviewer Assessment\nRating: 5\nRationale: No frozen source pages were available."
    if challenge:
        blocked = {
            "source_sha256": None,
            "diff_sha256": None,
            "findings": [],
            "assessment": {
                "rating": 1 if case == "challenge-nonblocker" else 5,
                "rationale": "No frozen source pages were available.",
            },
        }
        if case == "challenge-partial-digest":
            blocked["source_sha256"] = "a" * 64
        elif case == "challenge-with-findings":
            blocked["findings"] = [{"signature": "uninspected-claim"}]
        blocked_message = json.dumps(blocked)
        if case == "challenge-fenced":
            blocked_message = f"```adversarial-loop\n{blocked_message}\n```"
        elif case == "challenge-malformed-shape":
            del blocked["findings"]
            blocked_message = json.dumps(blocked)
        elif case == "challenge-malformed":
            blocked_message = "```adversarial-loop\n{broken JSON}\n```"
    terminal["last_agent_message"] = blocked_message
    if case in {"challenge-raw", "challenge-fenced"}:
        assert all(source_digest not in json.dumps(row) and diff_digest not in json.dumps(row) for row in rows)
        assert "source_sha256 and diff_sha256 as null" in json.dumps(rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("author") == rows[0]["payload"]["agent_path"]:
            payload["content"] = [
                {"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{blocked_message}"}
            ]
    _write_jsonl(parent_path, parent)
    if case == "wrong-parent":
        rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "unrelated"
    _write_jsonl(child, rows)
    if case == "parent-miscopy":
        parent_path = home / "sessions/rollout-parent.jsonl"
        parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
        child_name = rows[0]["payload"]["agent_path"].rsplit("/", 1)[1]
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                args = json.loads(payload["arguments"])
                if args["task_name"] == child_name:
                    # Only the first supplied block is malformed; later page commands remain canonical.
                    args["message"] = args["message"].replace("review_context.py", "review_prepare.py", 1)
                    payload["arguments"] = json.dumps(args)
                    for child_row in rows:
                        if child_row["type"] == "response_item" and child_row["payload"].get("type") == "agent_message":
                            child_row["payload"]["content"] = [
                                {"type": "encrypted_content", "encrypted_content": args["message"]}
                            ]
        _write_jsonl(parent_path, parent)
        _write_jsonl(child, rows)
    command = [
        sys.executable,
        str(HELPER),
        "prepare-repair",
        "--out",
        str(run),
        "--codex-home",
        str(home),
        "--role",
        role,
        "--kind",
        "incomplete-dispatch",
    ]
    prepared = subprocess.run(command, capture_output=True, text=True, check=False)
    if case in {
        "challenge-nonblocker",
        "challenge-malformed",
        "challenge-malformed-shape",
        "challenge-unproven",
        "challenge-partial-digest",
        "challenge-with-findings",
    }:
        expected = {
            "challenge-nonblocker": "assessment-content-invalid",
            "challenge-malformed": "assessment-content-invalid",
            "challenge-malformed-shape": "assessment-content-invalid",
            "challenge-unproven": "failure-unproven",
            "challenge-partial-digest": "assessment-content-invalid",
            "challenge-with-findings": "assessment-content-invalid",
        }[case]
        assert prepared.returncode != 0
        assert expected in prepared.stderr
        return
    if case not in {"repair", "parent-miscopy", "wrapper-control", "challenge-raw", "challenge-fenced"}:
        assert prepared.returncode != 0
        assert (
            "context-read-count-mismatch"
            if case == "source-read"
            else "failure-unproven"
            if case in {"no-diagnostic", "wrapper-prefix", "wrapper-extra"}
            else "review-child-session-not-unique"
        ) in prepared.stderr
        return
    assert prepared.returncode == 0, prepared.stderr
    arguments = json.loads(prepared.stdout)["arguments"]
    original_path = original_rows[0]["payload"]["agent_path"]
    new_path = original_path.removesuffix("_a1") + "_a2"
    thread = "dispatch-repair-thread"
    original_thread = original_rows[0]["payload"]["id"]
    replacement = json.loads(
        json.dumps(original_rows)
        .replace(original_path, new_path)
        .replace(original_thread, thread)
        .replace("--attempt 1", "--attempt 2")
        .replace("attempt=1", "attempt=2")
    )
    replacement[0]["payload"]["timestamp"] = "2026-01-01T10:01:00.050Z"
    for row in replacement:
        if row["type"] == "response_item" and row["payload"].get("type") == "agent_message":
            row["payload"]["content"] = [{"type": "encrypted_content", "encrypted_content": arguments["message"]}]
    terminal = replacement[-1]["payload"]
    terminal.update(started_at=1767261660.2, completed_at=1767261661.0)
    _write_jsonl(home / f"sessions/rollout-{thread}.jsonl", replacement)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
    parent.extend(
        [
            {
                "timestamp": "2026-01-01T10:01:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "repair-spawn",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "repair-spawn",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:01.100Z",
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Message Type: FINAL_ANSWER\nPayload:\n{terminal['last_agent_message']}",
                        }
                    ],
                },
            },
        ]
    )
    _write_jsonl(parent_path, parent)
    assembled = _assemble(run, home, challenge_only=challenge)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text())
    repaired = next(item for item in manifest["passes"] if item["role"] == role)
    assert repaired["selected_attempt"] == 2
    assert len(repaired["attempts"]) == 2
    if challenge:
        original = (run / repaired["attempts"][0]["output_path"]).read_text(encoding="utf-8").strip()
        assert original == blocked_message
        selected = json.loads((run / repaired["output_path"]).read_text(encoding="utf-8"))
        assert selected["source_sha256"] == source_digest
        assert selected["diff_sha256"] == diff_digest
        assert selected["assessment"]["rating"] == 1
    assert all(item["selected_attempt"] == 1 for item in manifest["passes"] if item["role"] != role)
    assert json.loads((run / "inspection-summary.json").read_text())["actual_mode"] == (
        "serial" if challenge else "parallel"
    )


def _tree_state(root: Path) -> dict[str, tuple[bytes, int] | None]:
    """Capture every directory and file below ``root`` with its exact bytes and modification time."""
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
    }


def _restore_tree(root: Path, state: dict[str, tuple[bytes, int] | None]) -> None:
    """Return ``root`` to a captured state so a subtest starts from exactly the bytes its shared build produced.

    Entries added since the capture are removed (children before parents), entries whose kind or bytes changed are
    rewritten with their recorded modification time, and removed directories are recreated. Restoring at the start of
    every subtest means leftovers of a failed subtest can never reach the next one.
    """
    for path in sorted(root.rglob("*"), reverse=True):
        recorded = state.get(path.relative_to(root).as_posix(), False)
        if recorded is not False and (recorded is None) == path.is_dir():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    for relative, recorded in state.items():
        path = root / relative
        if recorded is None:
            path.mkdir(exist_ok=True)
        elif not path.is_file() or path.read_bytes() != recorded[0]:
            path.write_bytes(recorded[0])
            os.utime(path, ns=(recorded[1], recorded[1]))
    assert _tree_state(root).keys() == state.keys(), "subtest-tree-restore-incomplete"


def _partial_dispatch_base(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    """Prepare one bounded batch and record complete native context reads before any launch failure is injected."""
    root = _review_inputs(tmp_path)
    source_root = json.loads((root / "local-source/review-worktree.json").read_bytes())["review_worktree"]
    prepared = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare",
            "--out",
            str(root),
            "--run-id",
            "bounded-review",
            "--parent-thread-id",
            "parent",
            "--source-root",
            source_root,
            "--batches",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    run = root / "batches/source-001"
    _, home, children = _assembly_evidence(tmp_path, prepared_run=run)
    return run, home, children


def _inject_partial_dispatch_failure(
    tmp_path: Path,
    run: Path,
    home: Path,
    children: dict[str, Path],
    *,
    parent_typo: bool,
    copied_reader: bool = False,
    failure: str = "missing-reader",
    opaque_transport: bool = False,
) -> tuple[list[dict[str, object]], str]:
    """Retain an actual reader-launch failure among ordered successful context reads of a prepared batch."""
    if copied_reader:
        installed_reader = tmp_path / "installed reader café" / "review_context.py"
        installed_reader.parent.mkdir()
        installed_reader.write_bytes((SKILL / "review_context.py").read_bytes())
        reader = runpy.run_path(str(SKILL / "review_context.py"))
        dispatch_path = run / "dispatch.json"
        dispatch = json.loads(dispatch_path.read_bytes())
        replacements = {}
        for call in dispatch["calls"]:
            previous = call["arguments"]["message"]
            issued = reader["dispatch_message"](
                run / "inspection-plan.json",
                call["role"],
                python_executable=sys.executable,
                provenance_header=False,
                reader_path=installed_reader,
            )
            replacements[previous] = issued
            replacements.update(
                zip(
                    re.findall(r"```javascript\n(.*?)\n```", previous, re.DOTALL),
                    re.findall(r"```javascript\n(.*?)\n```", issued, re.DOTALL),
                    strict=True,
                )
            )
            call["arguments"]["message"] = issued
        dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8", newline="\n")
        for path in [*children.values(), home / "sessions/rollout-parent.jsonl"]:
            retained = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            for row in retained:
                payload = row.get("payload", {})
                if payload.get("type") == "custom_tool_call":
                    payload["input"] = replacements[payload["input"]]
                elif payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                    arguments = json.loads(payload["arguments"])
                    arguments["message"] = replacements[arguments["message"]]
                    payload["arguments"] = json.dumps(arguments)
                elif payload.get("type") == "agent_message":
                    for item in payload.get("content", []):
                        if "encrypted_content" in item:
                            item["encrypted_content"] = replacements[item["encrypted_content"]]
            _write_jsonl(path, retained)
    role = "challenger"
    child = children[role]
    complete = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    rows = json.loads(json.dumps(complete))
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    assert len(calls) >= 3
    missing_reader = tmp_path / "missing reader café" / "review_context.py"
    render = runpy.run_path(str(SKILL / "review_context.py"))["render_read_call"]
    original_message = next(
        row["payload"]["content"][0]["encrypted_content"]
        for row in rows
        if row.get("payload", {}).get("type") == "agent_message"
    )
    sent = original_message
    commands = []
    duplicate = failure.startswith("duplicate-interpreter")
    failed_page = 1 if failure.endswith("-first") else len(calls) if failure.endswith("-last") else len(calls) - 1
    for page, call in enumerate(calls, 1):
        if (duplicate and page != failed_page) or (not duplicate and page <= len(calls) - 2):
            arguments = json.loads(call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
            command = arguments["cmd"] if os.name == "nt" else shlex.split(arguments["cmd"])
            successful = subprocess.run(command, capture_output=True, check=False)
            assert successful.returncode == 0
            assert successful.stderr == b""
            commands.append(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "item": {
                            "type": "CommandExecution",
                            "command": [arguments["cmd"]],
                            "exit_code": successful.returncode,
                            "stdout": successful.stdout.decode("utf-8"),
                            "stderr": successful.stderr.decode("utf-8"),
                        },
                    },
                }
            )
            continue
        if duplicate:
            original_args = json.loads(call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
            executable = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)
            duplicate_args = {**original_args, "cmd": executable + " " + original_args["cmd"]}
            malformed = call["input"].replace(
                json.dumps(original_args, ensure_ascii=False), json.dumps(duplicate_args, ensure_ascii=False)
            )
        else:
            malformed = render(run / "inspection-plan.json", role, 1, sys.executable, page, missing_reader)
        sent = sent.replace(call["input"], malformed)
        call["input"] = malformed
        arguments = json.loads(malformed.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        command = arguments["cmd"] if os.name == "nt" else shlex.split(arguments["cmd"])
        # Missing scripts fail before the reader can establish its UTF-8 output contract.
        failed = subprocess.run(
            command, capture_output=True, check=False, env={**os.environ, "PYTHONIOENCODING": "utf-8"}
        )
        diagnostic = (failed.stdout + failed.stderr).decode("utf-8")
        if duplicate:
            assert failed.returncode == 1
            assert "SyntaxError:" in diagnostic
        else:
            assert failed.returncode == 2
            assert "[Errno 2]" in diagnostic
            # Missing-script repr doubles Windows path separators.
            assert repr(str(missing_reader)) in diagnostic
        commands.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": [arguments["cmd"]],
                        "exit_code": failed.returncode,
                        "stdout": failed.stdout.decode("utf-8"),
                        "stderr": failed.stderr.decode("utf-8"),
                    },
                },
            }
        )
        if failure == "missing-reader-stdout":
            commands[-1]["payload"]["item"].update(stdout=diagnostic, stderr="")
        receipt = next(
            row["payload"]
            for row in rows
            if row.get("payload", {}).get("type") == "custom_tool_call_output"
            and row["payload"]["call_id"] == call["call_id"]
        )
        receipt["output"][1]["text"] = diagnostic
    rows[-1:-1] = commands
    terminal = rows[-1]["payload"]
    partial = (
        terminal["last_agent_message"]
        .replace("Rating: 1", "Rating: 4")
        .replace(
            "The inspected scope is clean.", "Frozen page content was unavailable after a proved reader-launch failure."
        )
    )
    if opaque_transport:
        sent = "opaque-host-transport:" + role
        partial = partial.replace("## Reviewer Assessment", "**Reviewer Assessment**")
    terminal["last_agent_message"] = partial
    old_path = rows[0]["payload"]["agent_path"]
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("author") == old_path:
            payload["content"][0]["text"] = f"Message Type: FINAL_ANSWER\nPayload:\n{partial}"
        elif (
            (parent_typo or opaque_transport)
            and payload.get("type") == "function_call"
            and payload.get("name") == "spawn_agent"
        ):
            arguments = json.loads(payload["arguments"])
            if arguments["task_name"] == old_path.rsplit("/", 1)[1]:
                arguments["message"] = sent
                payload["arguments"] = json.dumps(arguments)
    if parent_typo or opaque_transport:
        next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "agent_message")["content"][0][
            "encrypted_content"
        ] = sent
    _write_jsonl(child, rows)
    _write_jsonl(parent_path, parent)
    return complete, partial


def _partial_dispatch_evidence(
    tmp_path: Path,
    *,
    parent_typo: bool,
    copied_reader: bool = False,
    failure: str = "missing-reader",
    opaque_transport: bool = False,
) -> tuple[Path, Path, dict[str, Path], list[dict[str, object]], str]:
    """Retain an actual reader-launch failure among ordered successful context reads."""
    run, home, children = _partial_dispatch_base(tmp_path)
    complete, partial = _inject_partial_dispatch_failure(
        tmp_path,
        run,
        home,
        children,
        parent_typo=parent_typo,
        copied_reader=copied_reader,
        failure=failure,
        opaque_transport=opaque_transport,
    )
    return run, home, children, complete, partial


@pytest.mark.integration
@pytest.mark.parametrize("copied_reader", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "missing-reader",
        "missing-reader-stdout",
        "duplicate-interpreter",
        "duplicate-interpreter-first",
        "duplicate-interpreter-last",
    ],
)
def test_partial_dispatch_repair_retains_original_and_requires_complete_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subtests: pytest.Subtests,
    copied_reader: bool,
    failure: str,
) -> None:
    """Recover proved reader-launch copy errors while requiring complete independent replacement reads.

    Each reader-launch failure is crossed with every parent-typo, opaque-transport, and replacement-completeness
    combination. The prepared batch and its complete native reads are identical for all of them, so they are built once
    and every combination runs as an independent subtest from a byte-exact restore of that build.
    """
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    run, home, children = _partial_dispatch_base(tmp_path)
    prepared_batch = _tree_state(tmp_path)
    for parent_typo, opaque_transport, replacement in itertools.product(
        [False, True], [False, True], ["complete", "missing-page"]
    ):
        with subtests.test(parent_typo=parent_typo, opaque_transport=opaque_transport, replacement=replacement):
            _restore_tree(tmp_path, prepared_batch)
            _check_partial_dispatch_replacement(
                tmp_path,
                run,
                home,
                children,
                parent_typo=parent_typo,
                replacement=replacement,
                copied_reader=copied_reader,
                failure=failure,
                opaque_transport=opaque_transport,
            )


def _check_partial_dispatch_replacement(
    tmp_path: Path,
    run: Path,
    home: Path,
    children: dict[str, Path],
    *,
    parent_typo: bool,
    replacement: str,
    copied_reader: bool,
    failure: str,
    opaque_transport: bool,
) -> None:
    """Inject one proved reader-launch failure, then require the repair to retain it and read every page again."""
    complete, partial = _inject_partial_dispatch_failure(
        tmp_path,
        run,
        home,
        children,
        parent_typo=parent_typo,
        copied_reader=copied_reader,
        failure=failure,
        opaque_transport=opaque_transport,
    )
    original_bytes = children["challenger"].read_bytes()
    sibling_bytes = children["qa-specialist"].read_bytes()
    frozen = {path: path.read_bytes() for path in [run / "inspection-plan.json", run / "dispatch.json"]}
    prepared = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-repair",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            "incomplete-dispatch",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    arguments = json.loads(prepared.stdout)["arguments"]
    session = complete[0]["payload"]
    old_path, old_thread = session["agent_path"], session["id"]
    new_path, new_thread = old_path.removesuffix("_a1") + "_a2", "partial-dispatch-replacement"
    rows = json.loads(
        json.dumps(complete)
        .replace(old_path, new_path)
        .replace(old_thread, new_thread)
        .replace("--attempt 1", "--attempt 2")
        .replace("attempt=1", "attempt=2")
    )
    rows[0]["payload"]["timestamp"] = "2026-01-01T10:01:00.050Z"
    next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "agent_message")["content"][0][
        "encrypted_content"
    ] = arguments["message"]
    terminal = rows[-1]["payload"]
    terminal.update(started_at=1767261660.2, completed_at=1767261661.0)
    if replacement == "missing-page":
        tools = [row for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
        last_id = tools[-1]["payload"]["call_id"]
        rows = [row for row in rows if row.get("payload", {}).get("call_id") != last_id]
    _write_jsonl(home / f"sessions/rollout-{new_thread}.jsonl", rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    parent.extend(
        [
            {
                "timestamp": "2026-01-01T10:01:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "partial-repair-spawn",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "partial-repair-spawn",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:01.100Z",
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Message Type: FINAL_ANSWER\nPayload:\n{terminal['last_agent_message']}",
                        }
                    ],
                },
            },
        ]
    )
    _write_jsonl(parent_path, parent)
    assembled = subprocess.run(
        [sys.executable, str(HELPER), "assemble-wave", "--out", str(run), "--codex-home", str(home)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert children["challenger"].read_bytes() == original_bytes
    assert children["qa-specialist"].read_bytes() == sibling_bytes
    assert all(path.read_bytes() == content for path, content in frozen.items())
    if replacement == "missing-page":
        assert assembled.returncode != 0
        assert "review-inspection-context-read-count-mismatch:challenger" in assembled.stderr
        assert not (run / "specialist-manifest.json").exists()
        return
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    repaired = next(item for item in manifest["passes"] if item["role"] == "challenger")
    sibling = next(item for item in manifest["passes"] if item["role"] == "qa-specialist")
    assert manifest["schema_version"] == 8
    assert manifest["context_reader_path"] == str(
        tmp_path / "installed reader café" / "review_context.py"
        if copied_reader
        else (SKILL / "review_context.py").resolve()
    )
    assert repaired["selected_attempt"] == 2
    assert len(repaired["attempts"]) == 2
    assert (run / repaired["attempts"][0]["raw_output_path"]).read_bytes() == partial.encode("utf-8")
    assert (run / repaired["attempts"][1]["raw_output_path"]).read_bytes() == terminal["last_agent_message"].encode(
        "utf-8"
    )
    assert sibling["selected_attempt"] == 1
    assert len(sibling["attempts"]) == 1
    assert repaired["attempts"][1]["agent_thread_id"] == new_thread
    assert json.loads((run / "inspection-summary.json").read_bytes())["actual_mode"] == "parallel"


@pytest.mark.integration
def test_opaque_partial_dispatch_repair_requires_bound_delivery_and_unavailable_assessment(
    tmp_path: Path, subtests: pytest.Subtests
) -> None:
    """Opaque partial recovery cannot substitute delivery, source coordinates, or an unavailable assessment.

    Every damage alters the same recorded opaque reader-launch failure, so that evidence is built once and each damage
    runs as an independent subtest from a byte-exact restore of it. A refusal here is only meaningful while the
    undamaged evidence is accepted; that baseline is asserted by the ``duplicate-interpreter-first`` opaque-transport
    case of ``test_partial_dispatch_repair_retains_original_and_requires_complete_replacement``.
    """
    run, home, children, _, _ = _partial_dispatch_evidence(
        tmp_path, parent_typo=False, failure="duplicate-interpreter-first", opaque_transport=True
    )
    recorded = _tree_state(tmp_path)
    for damage in [
        "missing-delivery",
        "different-delivery",
        "duplicate-delivery",
        "changed-model",
        "changed-role",
        "missing-page",
        "duplicate-assessment",
        "missing-rationale",
        "clean-rating",
        "out-of-section-rating",
    ]:
        with subtests.test(damage=damage):
            _restore_tree(tmp_path, recorded)
            _check_opaque_repair_damage(run, home, children, damage)


def _check_opaque_repair_damage(run: Path, home: Path, children: dict[str, Path], damage: str) -> None:
    """Apply one delivery or assessment damage and require the opaque repair to refuse without writing."""
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    delivery = next(row for row in rows if row.get("payload", {}).get("type") == "agent_message")
    if damage == "missing-delivery":
        rows.remove(delivery)
    elif damage == "different-delivery":
        delivery["payload"]["content"][0]["encrypted_content"] = "different-transport"
    elif damage == "duplicate-delivery":
        rows.insert(rows.index(delivery), delivery)
    elif damage == "changed-model":
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                arguments = json.loads(payload["arguments"])
                if arguments["task_name"].startswith("review_challenger_"):
                    arguments["model"] = "unselected-model"
                    payload["arguments"] = json.dumps(arguments)
    elif damage in {"changed-role", "missing-page"}:
        calls = [row for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
        call = calls[-1]["payload"]
        if damage == "changed-role":
            call["input"] = call["input"].replace("--role challenger", "--role qa-specialist")
        else:
            rows = [row for row in rows if row.get("payload", {}).get("call_id") != call["call_id"]]
    else:
        terminal = rows[-1]["payload"]
        previous = terminal["last_agent_message"]
        if damage == "duplicate-assessment":
            message = previous + "\n## Reviewer Assessment\nRating: 4\nRationale: Duplicate.\n"
        elif damage == "missing-rationale":
            message = re.sub(r"(?m)^Rationale:.*$", "", previous)
        elif damage == "clean-rating":
            message = previous.replace("Rating: 4", "Rating: 1")
        else:
            message = previous.replace("Rating: 4", "**Other section**\nRating: 4")
        terminal["last_agent_message"] = message
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "agent_message" and payload.get("author") == rows[0]["payload"]["agent_path"]:
                payload["content"][0]["text"] = f"Message Type: FINAL_ANSWER\nPayload:\n{message}"
    _write_jsonl(child, rows)
    _write_jsonl(parent_path, parent)
    protected = {
        path: path.read_bytes() for path in [child, parent_path, run / "inspection-plan.json", run / "dispatch.json"]
    }
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-repair",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            "incomplete-dispatch",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode != 0
    expected = (
        "provenance-parent-spawn-mismatch:challenger:1"
        if damage in {"missing-delivery", "different-delivery", "duplicate-delivery", "changed-model"}
        else "review-repair-dispatch-cause-unproven:challenger:3"
        if damage == "changed-role"
        else "review-repair-dispatch-cause-unproven:challenger:1"
        if damage == "missing-page"
        else "review-repair-incomplete-assessment-missing:challenger"
        if damage == "clean-rating"
        else "review-assessment-content-invalid:challenger"
    )
    assert expected in result.stderr
    assert not (run / "repair-dispatch.challenger.json").exists()
    assert all(path.read_bytes() == retained for path, retained in protected.items())


@pytest.mark.integration
def test_duplicate_interpreter_repair_rejects_unproved_launch_failures(
    tmp_path: Path, subtests: pytest.Subtests
) -> None:
    """Keep a proved executable-as-source failure distinct from arbitrary syntax errors or missing coverage.

    Every damage alters the same recorded duplicate-interpreter launch failure, so that evidence is built once and each
    damage runs as an independent subtest from a byte-exact restore of it. A refusal here is only meaningful while the
    undamaged evidence is accepted; that baseline is asserted by the ``duplicate-interpreter`` cases of
    ``test_partial_dispatch_repair_retains_original_and_requires_complete_replacement``.
    """
    run, home, children, _, _ = _partial_dispatch_evidence(tmp_path, parent_typo=False, failure="duplicate-interpreter")
    recorded = _tree_state(tmp_path)
    for damage in [
        "generic-syntax",
        "wrong-diagnostic-path",
        "wrong-exit",
        "missing-command-receipts",
        "mismatched-command-output",
        "changed-interpreter",
        "changed-reader",
        "changed-plan",
        "changed-role",
        "changed-attempt",
        "changed-page",
        "changed-trailing-read",
        "second-duplicate",
        "missing-trailing-page",
    ]:
        with subtests.test(damage=damage):
            _restore_tree(tmp_path, recorded)
            _check_duplicate_interpreter_damage(tmp_path, run, home, children, damage)


def _check_duplicate_interpreter_damage(
    tmp_path: Path, run: Path, home: Path, children: dict[str, Path], damage: str
) -> None:
    """Apply one receipt or coordinate damage and require the exact repair refusal without rewriting the child."""
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    damaged = calls[-2]
    receipt = next(
        row["payload"]
        for row in rows
        if row.get("payload", {}).get("type") == "custom_tool_call_output"
        and row["payload"]["call_id"] == damaged["call_id"]
    )
    command = next(
        row["payload"]["item"]
        for row in rows
        if row.get("payload", {}).get("type") == "item_completed"
        and row["payload"].get("item", {}).get("exit_code") == 1
    )
    if damage == "generic-syntax":
        receipt["output"][1]["text"] = "SyntaxError: invalid syntax\n"
        command["stderr"] = receipt["output"][1]["text"]
    elif damage == "wrong-diagnostic-path":
        receipt["output"][1]["text"] = (
            '  File "unrelated-program", line 1\n    invalid\n    ^\n'
            "SyntaxError: Non-UTF-8 code starting with '\\xcf' in file unrelated-program on line 1, "
            "but no encoding declared; see https://peps.python.org/pep-0263/ for details\n"
        )
        command["stderr"] = receipt["output"][1]["text"]
    elif damage == "wrong-exit":
        command["exit_code"] = 2
    elif damage == "missing-command-receipts":
        rows = [
            row
            for row in rows
            if not (
                row.get("payload", {}).get("type") == "item_completed"
                and row["payload"].get("item", {}).get("type") == "CommandExecution"
            )
        ]
    elif damage == "mismatched-command-output":
        command["stderr"] += "unbound diagnostic\n"
    elif damage in {"changed-interpreter", "changed-reader", "changed-plan", "changed-role", "changed-attempt"}:
        old, new = {
            "changed-interpreter": (_json_text(sys.executable), _json_text(str(tmp_path / "unrelated-python"))),
            "changed-reader": ("review_context.py", "unrelated_program.py"),
            "changed-plan": ("inspection-plan.json", "unrelated-plan.json"),
            "changed-role": ("--role challenger", "--role qa-specialist"),
            "changed-attempt": ("--attempt 1", "--attempt 2"),
        }[damage]
        damaged["input"] = damaged["input"].replace(old, new)
    elif damage == "changed-page":
        damaged["input"] = damaged["input"].replace(f"--page {len(calls) - 1}", "--page 1")
    elif damage == "changed-trailing-read":
        calls[-1]["input"] = calls[-1]["input"].replace("--role challenger", "--role qa-specialist")
    elif damage == "second-duplicate":
        arguments = json.loads(calls[-1]["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        executable = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)
        duplicate = {**arguments, "cmd": executable + " " + arguments["cmd"]}
        calls[-1]["input"] = calls[-1]["input"].replace(
            json.dumps(arguments, ensure_ascii=False), json.dumps(duplicate, ensure_ascii=False)
        )
        final_receipt = next(
            row["payload"]
            for row in rows
            if row.get("payload", {}).get("type") == "custom_tool_call_output"
            and row["payload"]["call_id"] == calls[-1]["call_id"]
        )
        final_receipt["output"][1]["text"] = receipt["output"][1]["text"]
        trailing_command = next(
            row["payload"]["item"]
            for row in rows
            if row.get("payload", {}).get("type") == "item_completed"
            and row["payload"].get("item", {}).get("command") == [arguments["cmd"]]
        )
        trailing_command.update(command=[duplicate["cmd"]], exit_code=1, stdout="", stderr=receipt["output"][1]["text"])
    else:
        rows = [row for row in rows if row.get("payload", {}).get("call_id") != calls[-1]["call_id"]]
    _write_jsonl(child, rows)
    before = child.read_bytes()
    prepared = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-repair",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            "incomplete-dispatch",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert prepared.returncode != 0
    # Pin the exact refusal: a prefix match would also accept a regression that rejects before the damage is read.
    expected = {
        "generic-syntax": "review-repair-dispatch-cause-unproven:challenger:2",
        "wrong-diagnostic-path": "review-repair-dispatch-cause-unproven:challenger:2",
        "wrong-exit": "review-inspection-context-command-mismatch:challenger:2",
        "missing-command-receipts": "review-repair-dispatch-cause-unproven:challenger",
        "mismatched-command-output": "review-inspection-context-command-mismatch:challenger:2",
        "changed-interpreter": "review-repair-dispatch-cause-unproven:challenger:2",
        "changed-reader": "review-repair-dispatch-cause-unproven:challenger:2",
        "changed-plan": "review-repair-dispatch-cause-unproven:challenger:2",
        "changed-role": "review-repair-dispatch-cause-unproven:challenger:2",
        "changed-attempt": "review-repair-dispatch-cause-unproven:challenger:2",
        "changed-page": "review-repair-dispatch-cause-unproven:challenger:2",
        "changed-trailing-read": "review-repair-dispatch-cause-unproven:challenger:3",
        "second-duplicate": "review-repair-dispatch-cause-unproven:challenger:3",
        "missing-trailing-page": "review-repair-dispatch-cause-unproven:challenger:2",
    }[damage]
    assert prepared.stderr == expected + "\n"
    assert not (run / "repair-dispatch.challenger.json").exists()
    assert child.read_bytes() == before


@pytest.mark.integration
@pytest.mark.parametrize(
    ("copied_reader", "damages"),
    [
        pytest.param(
            False,
            [
                "unknown-tool",
                "changed-plan",
                "changed-role",
                "changed-page",
                "changed-attempt",
                "changed-interpreter",
                "mixed-command",
                "success-after-failure",
                "prefix-provenance",
                "changed-context",
            ],
            id="shipped-reader",
        ),
        pytest.param(True, ["altered-reader"], id="copied-reader"),
    ],
)
def test_partial_dispatch_repair_rejects_unproved_command_changes(
    tmp_path: Path, subtests: pytest.Subtests, copied_reader: bool, damages: list[str]
) -> None:
    """Do not turn unrelated commands or changed review coordinates into a reader-path correction.

    Damages that share one reader installation alter the same recorded failure, so that evidence is built once per
    installation and each damage runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children, complete, _ = _partial_dispatch_evidence(
        tmp_path, parent_typo=False, copied_reader=copied_reader
    )
    recorded = _tree_state(tmp_path)
    for damage in damages:
        with subtests.test(damage=damage):
            _restore_tree(tmp_path, recorded)
            _check_unproved_command_change(tmp_path, run, home, children, complete, damage)


def _check_unproved_command_change(
    tmp_path: Path, run: Path, home: Path, children: dict[str, Path], complete: list[dict[str, object]], damage: str
) -> None:
    """Apply one command or coordinate change and require the repair to refuse without rewriting the child."""
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    damaged = calls[-1]
    if damage == "unknown-tool":
        damaged["name"] = "unrelated_tool"
    elif damage == "changed-plan":
        damaged["input"] = damaged["input"].replace("inspection-plan.json", "unrelated-plan.json")
    elif damage == "changed-role":
        damaged["input"] = damaged["input"].replace("--role challenger", "--role qa-specialist")
    elif damage == "changed-page":
        damaged["input"] = damaged["input"].replace(f"--page {len(calls)}", "--page 1")
    elif damage == "changed-attempt":
        damaged["input"] = damaged["input"].replace("--attempt 1", "--attempt 2")
    elif damage == "changed-interpreter":
        arguments = json.loads(damaged["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        changed = {**arguments, "cmd": arguments["cmd"].replace(sys.executable, str(tmp_path / "other/python"))}
        previous = damaged["input"]
        damaged["input"] = previous.replace(
            json.dumps(arguments, ensure_ascii=False), json.dumps(changed, ensure_ascii=False)
        )
        assert damaged["input"] != previous
    elif damage == "mixed-command":
        damaged["input"] = damaged["input"].replace("review_context.py", "unrelated_program.py")
    elif damage == "success-after-failure":
        for row in rows:
            if row.get("payload", {}).get("call_id") == damaged["call_id"]:
                original = next(
                    original
                    for original in complete
                    if original.get("payload", {}).get("call_id") == damaged["call_id"]
                    and original["payload"]["type"] == row["payload"]["type"]
                )
                row["payload"] = json.loads(json.dumps(original["payload"]))
    elif damage == "prefix-provenance":
        receipt = next(
            row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call_output"
        )
        receipt["output"][1]["text"] = receipt["output"][1]["text"].replace("role=challenger", "role=unrelated")
    elif damage == "changed-context":
        context = run / "specialists/challenger-context.md"
        context.write_bytes(context.read_bytes() + b"\n")
    else:
        reader_path = tmp_path / "installed reader café/review_context.py"
        reader_path.write_bytes(reader_path.read_bytes() + b"\n# changed reader\n")
    _write_jsonl(child, rows)
    before = child.read_bytes()
    prepared = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-repair",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            "incomplete-dispatch",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert prepared.returncode != 0
    expected = (
        "provenance-context-hash-mismatch"
        if damage == "changed-context"
        else "manifest-context-reader-identity-invalid"
        if damage == "altered-reader"
        else "review-repair-"
    )
    assert expected in prepared.stderr
    assert not (run / "repair-dispatch.challenger.json").exists()
    assert child.read_bytes() == before


@pytest.mark.integration
def test_partial_dispatch_repair_rejects_changed_governing_evidence_before_dispatch(
    tmp_path: Path, subtests: pytest.Subtests
) -> None:
    """Reject changed governing evidence before allocating a correction that cannot pass assembly.

    Routing, briefs, and review input each change on the same recorded reader-launch failure, so that evidence is built
    once and each change runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children, _, _ = _partial_dispatch_evidence(tmp_path, parent_typo=False)
    recorded = _tree_state(tmp_path)
    for case, file_name, expected in [
        ("changed-routing", "review-routing.json", "review-wave-selection-changed"),
        ("changed-briefs", "review-briefs.json", "review-batch-briefs-changed"),
        ("changed-input", "diff.patch", "manifest-review-input-hash-mismatch"),
    ]:
        with subtests.test(case=case):
            _restore_tree(tmp_path, recorded)
            _check_changed_governing_evidence(run, home, children, file_name, expected)


def _check_changed_governing_evidence(
    run: Path, home: Path, children: dict[str, Path], file_name: str, expected: str
) -> None:
    """Change one governing input and require the repair to refuse before writing any correction."""
    path = (run.parent.parent if file_name == "review-briefs.json" else run) / file_name
    path.write_bytes(path.read_bytes() + b"\n")
    original = {child: child.read_bytes() for child in children.values()}
    prepared = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-repair",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            "incomplete-dispatch",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert prepared.returncode != 0
    assert expected in prepared.stderr
    assert not (run / "repair-dispatch.challenger.json").exists()
    assert all(child.read_bytes() == content for child, content in original.items())


def test_prepare_freezes_complete_wave_and_keeps_source_out_of_dispatch(tmp_path: Path) -> None:
    """Bind each canonical role to its size-ordered call without embedding source in dispatch."""
    run = _review_inputs(tmp_path)
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    assert dispatch["routing_sha256"] == hashlib.sha256((run / "review-routing.json").read_bytes()).hexdigest()
    assert dispatch["briefs_sha256"] == hashlib.sha256((run / "review-briefs.json").read_bytes()).hexdigest()
    assert [entry["role_id"] for entry in plan["contexts"]] == ["challenger", "qa-specialist"]
    assert len(dispatch["calls"]) == 2
    queued_roles = [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in dispatch["calls"]
    ]
    sizes = {entry["role_id"]: (run / entry["context_path"]).stat().st_size for entry in plan["contexts"]}
    assert queued_roles == sorted(sizes, key=lambda role: (-sizes[role], role))
    calls_by_role = dict(zip(queued_roles, dispatch["calls"], strict=True))
    assert set(calls_by_role) == set(sizes)
    for entry in plan["contexts"]:
        context = (run / entry["context_path"]).read_bytes()
        role = entry["role_id"]
        call = calls_by_role[role]
        assert hashlib.sha256(context).hexdigest() == entry["context_sha256"]
        assert context.startswith((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes())
        assert "Relevant source evidence." not in call["arguments"]["message"]
        assert call["arguments"]["fork_turns"] == "none"
        assert "agent_type" not in call["arguments"]
        assert call["arguments"]["task_name"] == (f"review_{role.replace('-', '_')}_{entry['context_sha256'][:12]}_a1")
        page_count = len(runpy.run_path(str(SKILL / "review_context.py"))["context_pages"](context.decode("utf-8")))
        sources = re.findall(r"```javascript\n(.*?)\n```", call["arguments"]["message"], re.DOTALL)
        assert len(sources) == page_count
        first_args = json.loads(sources[0].split("const args = ", 1)[1].split("; store(", 1)[0])
        key = re.search(r"review-context-[0-9a-f]{64}", sources[0])[0]
        for page, source in enumerate(sources, 1):
            canonical = _CONTEXT_READ_CALL(run / "inspection-plan.json", role, 1, sys.executable, page)
            args = json.loads(canonical.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
            assert args == {**first_args, "cmd": first_args["cmd"] + (f" --page {page}" if page != 1 else "")}
            if page > 1:
                assert source == (
                    '// @exec: {"max_output_tokens": 10000}\n'
                    + f'const args = load("{key}"); const r = await tools.exec_command({{...args, cmd: args.cmd + " --page {page}"}}); text(r.output);'
                )
        assert "append ` --page N`" not in call["arguments"]["message"]
    assert dispatch["context_bytes"] > dispatch["dispatch_bytes"]


def test_prepare_rejects_missing_role_before_freezing_any_context(tmp_path: Path) -> None:
    run = _review_inputs(tmp_path)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs.pop("challenger")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-brief-role-set-mismatch" in result.stderr
    assert not (run / "inspection-plan.json").exists()


def test_prepare_rejects_prose_only_source_claim_before_freezing(tmp_path: Path) -> None:
    """A claimed inspection cannot substitute for source bytes from the collected checkout."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text(
        "I inspected widget.py at the frozen revision.\n", encoding="utf-8", newline="\n"
    )
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"].pop("source_paths")
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-brief-source-selection-invalid:challenger" in result.stderr
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


def test_prepare_includes_exact_untracked_source(tmp_path: Path) -> None:
    """An untracked file has exact source bytes even though Git has no diff hunk for it."""
    run = _review_inputs(tmp_path, untracked=True)
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"] = ["new.txt"]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    context = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert "new_value = 7\n" in context
    assert "Untracked file: exact source bytes" in context
    assert "value = 2\n" not in context


@pytest.mark.parametrize("forged_diff", [False, True])
def test_prepare_accepts_only_matching_committed_detached_source(tmp_path: Path, forged_diff: bool) -> None:
    """Bind a committed review to the declared exact HEAD and selected source bytes."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    source = review / "widget.py"
    source.write_text("value = 3\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(review), "add", "widget.py"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(review),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Second",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(review), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "-C", str(review), "diff", "--binary", "HEAD^", "HEAD"], check=True, capture_output=True
    ).stdout
    if forged_diff:
        diff = diff.replace(b"+value = 3", b"+value = 999")
    (run / "diff.patch").write_bytes(diff)
    (run / "local-source/review-worktree.json").unlink()
    result = _prepare(run, source_root=review, expected_head=head)
    if forged_diff:
        assert result.returncode != 0
        assert "review-source-diff-stale" in result.stderr
        assert not (run / "inspection-plan.json").exists()
        return
    assert result.returncode == 0, result.stderr
    context = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert "value = 3\n" in context
    assert "+value = 3" in context


@pytest.mark.parametrize(
    ("destination", "rename"),
    [
        pytest.param("new.py", True, id="plain-rename"),
        pytest.param("café.py", True, id="quoted-rename"),
        pytest.param("café.py", False, id="quoted-update"),
    ],
)
def test_prepare_delivers_committed_destination(tmp_path: Path, destination: str, rename: bool) -> None:
    """Deliver exact source and patch bytes for renamed and quoted Git paths."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    (review / "widget.py").write_text("value = 1\n", encoding="utf-8", newline="\n")
    original = "old.py" if rename else destination
    (review / original).write_text("def renamed():\n    return 17\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(review), "add", original], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(review),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Base",
        ],
        check=True,
        capture_output=True,
    )
    if rename:
        subprocess.run(["git", "-C", str(review), "mv", original, destination], check=True, capture_output=True)
    else:
        (review / destination).write_text("def renamed():\n    return 18\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(review), "add", destination], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(review),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Rename",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(review), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    patch = subprocess.run(
        ["git", "-C", str(review), "diff", "--binary", "HEAD^", "HEAD"], check=True, capture_output=True
    ).stdout
    assert (b"rename to " in patch) is rename
    (run / "diff.patch").write_bytes(patch)
    (run / "local-source/review-worktree.json").unlink()
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    for brief in briefs.values():
        brief["source_paths"] = [destination]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")

    result = _prepare(run, source_root=review, expected_head=head)

    assert result.returncode == 0, result.stderr
    context = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert destination in context
    assert f"return {17 if rename else 18}" in context
    assert ("rename to " in context) is rename
    assert (run / "dispatch.json").exists()


def test_prepare_uses_nested_pr_receipt_and_rejects_ambiguous_receipts(tmp_path: Path) -> None:
    """Resolve the retained PR receipt at its actual path and reject competing receipts."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    (review / "widget.py").write_text("value = 3\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(review), "add", "widget.py"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(review),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "Second",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(review), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    base = subprocess.run(
        ["git", "-C", str(review), "rev-parse", "HEAD^"], check=True, capture_output=True, text=True
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "-C", str(review), "diff", "--binary", f"{base}...{head}", "--"], check=True, capture_output=True
    ).stdout
    (run / "diff.patch").write_bytes(diff)
    (run / "local-source/review-worktree.json").unlink()
    (run / "pr").mkdir()
    receipt = {"worktree": review.as_posix(), "expected_head": head, "diff_base_oid": base, "diff_head_oid": head}
    (run / "pr/local-checkout.json").write_text(json.dumps(receipt), encoding="utf-8", newline="\n")
    result = _prepare(run, source_root=review)
    assert result.returncode == 0, result.stderr
    assert "value = 3\n" in (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    (run / "local-checkout.json").write_text(json.dumps(receipt), encoding="utf-8", newline="\n")
    result = _prepare(run, source_root=review)
    assert result.returncode != 0
    assert "review-source-receipt-ambiguous" in result.stderr


def test_prepare_rejects_source_drift_before_freezing(tmp_path: Path) -> None:
    """A modified isolated checkout invalidates the retained source snapshot."""
    run = _review_inputs(tmp_path)
    review = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["review_worktree"]
    )
    (review / "widget.py").write_text("value = 99\n", encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert "Review worktree source changed after collection" in result.stderr
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


def test_prepare_rejects_unrelated_source_selection(tmp_path: Path) -> None:
    """An unchanged selected path cannot stand alone as review evidence."""
    run = _review_inputs(tmp_path)
    repository = Path(
        json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))["source_worktree"]
    )
    (repository / "unchanged.py").write_text("stable = True\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(repository), "add", "unchanged.py"], check=True, capture_output=True)
    # Selection outside the collector's frozen source inventory must fail closed.
    briefs = json.loads((run / "review-briefs.json").read_text(encoding="utf-8"))
    briefs["challenger"]["source_paths"] = ["unchanged.py"]
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert (
        "review-brief-source-unrelated:challenger" in result.stderr
        or "review-brief-source-selection-invalid:challenger" in result.stderr
    )
    assert not (run / "inspection-plan.json").exists()


def test_prepare_pages_context_larger_than_old_limit(tmp_path: Path) -> None:
    """Admit a complete large context through the bounded native page reader."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text("bounded evidence\n" * 5000, encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    context = (run / "specialists/challenger-context.md").read_bytes()
    assert len(context) > 65536
    assert context.count(b"bounded evidence\n") == 5000
    assert (run / "inspection-plan.json").exists()


def test_prepare_rejects_oversized_context_before_freezing_wave(tmp_path: Path) -> None:
    """Keep a context above the bounded native read ceiling out of the frozen wave."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text("bounded evidence\n" * 18000, encoding="utf-8", newline="\n")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-context-capacity-exceeded:challenger:262144-bytes" in result.stderr
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


@pytest.mark.parametrize("problem", ["changed-brief", "sensitive-evidence"])
def test_prepare_never_overwrites_frozen_evidence_or_retains_secrets(tmp_path: Path, problem: str) -> None:
    run = _review_inputs(tmp_path)
    if problem == "changed-brief":
        assert _prepare(run).returncode == 0
        original = (run / "inspection-plan.json").read_bytes()
        (run / "qa-specialist-evidence.md").write_text("Different source.", encoding="utf-8", newline="\n")
        expected = "review-frozen-artifact-conflict"
    else:
        (run / "qa-specialist-evidence.md").write_text(
            "Authorization: Bearer do-not-retain", encoding="utf-8", newline="\n"
        )
        expected = "review-context-sensitive-material"
    result = _prepare(run)
    assert result.returncode != 0
    assert expected in result.stderr
    if problem == "changed-brief":
        assert (run / "inspection-plan.json").read_bytes() == original
    else:
        assert not (run / "specialists").exists()


def test_assemble_binds_native_wave_and_preserves_child_outputs(tmp_path: Path, text_newline_default: None) -> None:
    """Accept a completed, overlapping wave with outputs bound to child finals."""
    run, home, children = _assembly_evidence(tmp_path)
    # Native history also contains non-spawn subagents; unrelated session shapes must not abort this wave.
    _write_jsonl(
        home / "sessions" / "unrelated.jsonl",
        [{"type": "session_meta", "payload": {"id": "unrelated", "source": {"subagent": "compact"}}}],
    )
    result = _assemble(run, home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 8
    assert manifest["manifest_kind"] == "native-wave"
    assert summary["actual_mode"] == "parallel"
    assert {item["role"] for item in manifest["passes"]} == set(children)
    for item in manifest["passes"]:
        role = item["role"]
        rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        final = rows[-1]["payload"]["last_agent_message"]
        assert (run / item["output_path"]).read_text(encoding="utf-8") == final + "\n"
        assert item["attempts"][0]["agent_thread_id"] == rows[0]["payload"]["id"]


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        pytest.param(
            "/resolved/python3: can't open file '/missing/review_context.py': [Errno 2] No such file or directory\n",
            "/missing/review_context.py",
            id="posix-coordinate",
        ),
        pytest.param(
            "C:\\Runtime\\python.exe: can't open file 'D:\\Missing\\review_context.py': [Errno 2] No such file or directory\r\n",
            "D:\\Missing\\review_context.py",
            id="windows-coordinate",
        ),
        pytest.param(
            "C:\\Runtime\\python.exe: can't open file 'D:\\\\Missing\\\\review_context.py': [Errno 2] No such file or directory\r\n",
            "D:\\Missing\\review_context.py",
            id="windows-repr-doubled-coordinate",
        ),
        pytest.param(
            "python: can't open file '/missing/review_context.py': [Errno 2] No such file or directory\n",
            None,
            id="relative-interpreter",
        ),
        pytest.param(
            "/resolved/tool: can't open file '/missing/review_context.py': [Errno 2] No such file or directory\n",
            None,
            id="unproved-interpreter",
        ),
        pytest.param(
            "/resolved/python3: can't open file '/missing/review_context.py': [Errno 2] No such file or directory\nSource bytes\n",
            None,
            id="mixed-output",
        ),
        pytest.param(None, None, id="absent-diagnostic"),
    ],
)
def test_missing_reader_diagnostic_preserves_serialized_coordinates(
    diagnostic: str | None, expected: str | None
) -> None:
    """Recognize only complete missing-script receipts without translating foreign host coordinates."""
    validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
    assert validator["_missing_reader_error_path"](diagnostic) == expected


def _json_text(value: str) -> str:
    """Return the text a string occupies inside a JSON document, as it appears in a serialized tool call."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


_POSIX_PYTHON = "/opt/py/python3.12"
_WINDOWS_PYTHON = "D:\\a\\repo\\.venv\\Scripts\\python.exe"


@pytest.mark.parametrize(
    ("diagnostic", "executable", "expected"),
    [
        pytest.param(
            "SyntaxError: source code cannot contain null bytes\n", _POSIX_PYTHON, False, id="bare-null-bytes-unbound"
        ),
        pytest.param(
            f'  File "{_POSIX_PYTHON}", line 1\n    \x7fELF\x02\x01\x01\nSyntaxError: source code cannot contain null bytes\n',
            _POSIX_PYTHON,
            True,
            id="framed-null-bytes-elf",
        ),
        pytest.param(
            f'  File "{_WINDOWS_PYTHON}", line 1\n    MZ\x90\nSyntaxError: source code cannot contain null bytes\r\n',
            _WINDOWS_PYTHON,
            True,
            id="framed-null-bytes-windows-path",
        ),
        pytest.param(
            f"SyntaxError: Non-UTF-8 code starting with '\\x84' in file {_POSIX_PYTHON} on line 2, but no encoding "
            "declared; see https://python.org/dev/peps/pep-0263/ for details\n",
            _POSIX_PYTHON,
            True,
            id="bare-non-utf8-elf-python310",
        ),
        pytest.param(
            f'  File "{_POSIX_PYTHON}", line 1\n    \x7fELF\x02\x01\x01\n    ^\nSyntaxError: invalid syntax\n',
            _POSIX_PYTHON,
            True,
            id="framed-invalid-syntax-elf-python310",
        ),
        pytest.param(
            f'  File "{_POSIX_PYTHON}", line 1\r\n    \x7fELF\x02\x01\x01\r\n    ^\r\nSyntaxError: invalid syntax\r\n',
            _POSIX_PYTHON,
            True,
            id="framed-invalid-syntax-elf-crlf",
        ),
        pytest.param(
            '  File "/other/python3", line 1\n    \x7fELF\x02\x01\x01\n    ^\nSyntaxError: invalid syntax\n',
            _POSIX_PYTHON,
            False,
            id="framed-invalid-syntax-names-other-file",
        ),
        pytest.param(
            f'  File "{_POSIX_PYTHON}", line 7\n    \x7fELF\x02\x01\x01\n    ^\nSyntaxError: invalid syntax\n',
            _POSIX_PYTHON,
            False,
            id="framed-invalid-syntax-wrong-line",
        ),
        pytest.param(
            f'  File "{_POSIX_PYTHON}", line 1\n    \x7fELF\x02\x01\x01\n    ^\nSyntaxError: invalid syntax\nextra\n',
            _POSIX_PYTHON,
            False,
            id="framed-invalid-syntax-trailing-output",
        ),
        pytest.param(
            f'  File "{_POSIX_PYTHON}", line 1\n    x = 1\n    ^\nSyntaxError: invalid syntax\n',
            _POSIX_PYTHON,
            False,
            id="framed-invalid-syntax-not-elf-echo",
        ),
        pytest.param(
            f'  File "{_POSIX_PYTHON}", line 1\n    \x7fELF\x02\x01\x01\nSyntaxError: invalid syntax\n',
            _POSIX_PYTHON,
            False,
            id="framed-invalid-syntax-no-caret",
        ),
        pytest.param(
            f"SyntaxError: Non-UTF-8 code starting with '\\xcf' in file {_POSIX_PYTHON} on line 1, but no encoding "
            "declared; see https://peps.python.org/pep-0263/ for details\n",
            _POSIX_PYTHON,
            True,
            id="bare-non-utf8-mach-o-python312",
        ),
        pytest.param(
            f"SyntaxError: Non-UTF-8 code starting with '\\xcf' in file {_POSIX_PYTHON} on line 2, but no encoding "
            "declared; see https://python.org/dev/peps/pep-0263/ for details\n",
            _POSIX_PYTHON,
            True,
            id="bare-non-utf8-mach-o-python310",
        ),
        pytest.param(
            f'  File "{_POSIX_PYTHON}", line 1\n    \ufffd\ufffd\n    ^\n'
            f"SyntaxError: Non-UTF-8 code starting with '\\xcf' in file {_POSIX_PYTHON} on line 1, but no encoding "
            "declared; see https://peps.python.org/pep-0263/ for details\n",
            _POSIX_PYTHON,
            True,
            id="framed-non-utf8-mach-o-python314",
        ),
        pytest.param("SyntaxError: invalid syntax\n", _POSIX_PYTHON, False, id="bare-invalid-syntax-unbound"),
        pytest.param(
            '  File "/other/python3", line 1\n    \x7fELF\nSyntaxError: source code cannot contain null bytes\n',
            _POSIX_PYTHON,
            False,
            id="frame-names-other-file",
        ),
        pytest.param(
            "SyntaxError: source code cannot contain null bytes\nSource bytes\n",
            _POSIX_PYTHON,
            False,
            id="mixed-output",
        ),
        pytest.param("SyntaxError: unterminated string literal\n", _POSIX_PYTHON, False, id="unrelated-syntax-error"),
        pytest.param(None, _POSIX_PYTHON, False, id="absent-diagnostic"),
    ],
)
def test_binary_source_diagnostic_accepts_interpreter_specific_receipts(
    diagnostic: str | None, executable: str, expected: bool
) -> None:
    """Running the interpreter binary as a script is proved across Python versions and binary formats.

    The ELF and Mach-O shapes were printed by real interpreters (3.10 through 3.14) reading their own executable,
    including the framed ``invalid syntax`` form that Python 3.10 prints for a python-build-standalone Linux binary; the
    Windows path form is a synthetic variant of the framed shape. The rejected shapes are unbound or unrelated output
    that must never prove a duplicated interpreter token. Development runs on a newer interpreter than CI, so this table
    is what keeps every version's output covered on every host.
    """
    validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
    assert validator["_binary_source_diagnostic"](diagnostic, executable) is expected


@pytest.mark.integration
@pytest.mark.parametrize("pragma_omission", ["neither", "failed", "retry", "both"])
@pytest.mark.parametrize("failed_coordinate", ["plan", "reader"])
def test_completed_reads_admit_only_one_proved_missing_plan_retry(
    tmp_path: Path, subtests: pytest.Subtests, pragma_omission: str, failed_coordinate: str
) -> None:
    """A nonexistent plan or reader may precede its exact page retry without replacing frozen source reads.

    Every damage rewrites the challenger rollout of the same assembled native wave, so that wave is built once per
    pragma and coordinate combination and each damage runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _tree_state(tmp_path)
    for damage in [
        "none",
        "none-first",
        "none-last",
        "wrong-error",
        "wrong-exit",
        "missing-command",
        "wrong-role",
        "wrong-attempt",
        "wrong-page",
        "wrong-reader",
        "wrong-interpreter",
        "missing-retry",
        "second-failure",
        "existing-plan",
        "historical-schema",
        "historical-reader",
        "wrong-receipt",
        "mixed-output",
        "wrong-command",
        "extra-tool",
        "altered-retry",
        "changed-later-page",
        "non-immediate",
        "different-pragma",
        "changed-inner-budget",
        "additional-pragma-rejection",
        "changed-second-coordinate",
        "wrapper-prefix",
        "wrapper-extra",
    ]:
        with subtests.test(damage=damage):
            _restore_tree(tmp_path, assembled)
            _check_missing_plan_retry(
                tmp_path,
                run,
                home,
                children,
                damage=damage,
                pragma_omission=pragma_omission,
                failed_coordinate=failed_coordinate,
            )


def _check_missing_plan_retry(
    tmp_path: Path,
    run: Path,
    home: Path,
    children: dict[str, Path],
    *,
    damage: str,
    pragma_omission: str,
    failed_coordinate: str,
) -> None:
    """Insert one real missing-plan or missing-reader failure before its retry and check read admission."""
    spec = importlib.util.spec_from_file_location("retry_prepare", HELPER)
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    commands = []
    for call in calls:
        args = json.loads(call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        result = subprocess.run(
            args["cmd"] if os.name == "nt" else shlex.split(args["cmd"]), capture_output=True, check=False
        )
        assert result.returncode == 0
        assert result.stderr == b""
        commands.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": [args["cmd"]],
                        "exit_code": 0,
                        "stdout": result.stdout.decode("utf-8"),
                        "stderr": "",
                    },
                },
            }
        )
    index = 0 if damage == "none-first" else len(calls) - 1 if damage == "none-last" else 1
    missing_plan = (
        tmp_path
        / "mistyped directory"
        / ("inspection-plan.json" if failed_coordinate == "plan" else "review_context.py")
    )
    failed_call = json.loads(json.dumps(calls[index]))
    failed_call["call_id"] = "proved-missing-plan"
    failed_call["input"] = prepare.validator.render_read_call(
        missing_plan if failed_coordinate == "plan" else run / "inspection-plan.json",
        "challenger",
        1,
        sys.executable,
        index + 1,
        reader_path=missing_plan if failed_coordinate == "reader" else None,
    )
    args = json.loads(failed_call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
    result = subprocess.run(
        args["cmd"] if os.name == "nt" else shlex.split(args["cmd"]), capture_output=True, check=False
    )
    diagnostic = (result.stdout + result.stderr).decode("utf-8")
    assert result.returncode == (1 if failed_coordinate == "plan" else 2)
    assert (
        diagnostic.startswith("review-context-read-failed:[Errno 2]")
        if failed_coordinate == "plan"
        else prepare.validator._missing_reader_error_path(diagnostic) == str(missing_plan)
    )
    failed_output = {
        "type": "custom_tool_call_output",
        "call_id": failed_call["call_id"],
        "output": [
            {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
            {"type": "input_text", "text": diagnostic},
        ],
    }
    failed_command = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "CommandExecution",
                "command": [args["cmd"]],
                "exit_code": result.returncode,
                "stdout": result.stdout.decode("utf-8"),
                "stderr": result.stderr.decode("utf-8"),
            },
        },
    }
    extra = [{"type": "response_item", "payload": failed_call}, {"type": "response_item", "payload": failed_output}]
    retry_row = next(row for row in rows if row.get("payload") == calls[index])
    position = rows.index(retry_row)
    rows[position:position] = extra
    commands.insert(index, failed_command)
    if pragma_omission in {"failed", "both"}:
        failed_call["input"] = failed_call["input"].split("\n", 1)[1]
    if pragma_omission in {"retry", "both"}:
        calls[index]["input"] = calls[index]["input"].split("\n", 1)[1]
    if damage == "non-immediate":
        rows[position : position + 2] = []
        rows[position + 2 : position + 2] = extra
    elif damage == "different-pragma":
        failed_call["input"] = '// @exec: {"max_output_tokens": 9999}\n' + failed_call["input"].split("\n", 1)[-1]
    elif damage == "changed-inner-budget":
        failed_call["input"] = failed_call["input"].replace('"max_output_tokens": 10000', '"max_output_tokens": 9999')
    elif damage == "wrong-error":
        failed_output["output"][1]["text"] = "review-context-read-failed:other failure\n"
        failed_command["payload"]["item"]["stderr"] = failed_output["output"][1]["text"]
        failed_command["payload"]["item"]["stdout"] = ""
    elif damage == "wrong-exit":
        failed_command["payload"]["item"]["exit_code"] = 3
    elif damage == "missing-command":
        commands.remove(failed_command)
    elif damage in {"wrong-role", "wrong-attempt", "wrong-page", "wrong-reader", "wrong-interpreter"}:
        old, new = {
            "wrong-role": ("--role challenger", "--role qa-specialist"),
            "wrong-attempt": ("--attempt 1", "--attempt 2"),
            "wrong-page": ("--page 2", "--page 1"),
            "wrong-reader": ("review_context.py", "unrelated_reader.py"),
            # The call input is JSON text, so a Windows path appears there with doubled backslashes.
            "wrong-interpreter": (_json_text(sys.executable), _json_text(str(tmp_path / "unrelated-python"))),
        }[damage]
        failed_call["input"] = failed_call["input"].replace(old, new)
    elif damage == "missing-retry":
        rows = [row for row in rows if row.get("payload", {}).get("call_id") != calls[index]["call_id"]]
        commands.pop(index + 1)
    elif damage == "second-failure":
        rows[position:position] = json.loads(json.dumps(extra))
        commands.insert(index, json.loads(json.dumps(failed_command)))
    elif damage == "wrong-receipt":
        failed_output["call_id"] = "unbound-receipt"
    elif damage == "mixed-output":
        failed_output["output"][1]["text"] += "unverified source bytes\n"
        failed_command["payload"]["item"]["stderr"] = failed_output["output"][1]["text"]
        failed_command["payload"]["item"]["stdout"] = ""
    elif damage == "wrapper-prefix":
        failed_command["payload"]["item"]["command"] = ["/bin/bash", "-lc", "printf unrelated-effect", args["cmd"]]
    elif damage == "wrapper-extra":
        failed_command["payload"]["item"]["command"] = ["/bin/bash", "-lc", args["cmd"], "extra-positional"]
    elif damage == "wrong-command":
        failed_command["payload"]["item"]["command"] = ["unrelated command"]
    elif damage == "extra-tool":
        failed_call["name"] = "unrelated_tool"
    elif damage == "altered-retry":
        calls[index]["input"] += "\nUnexpected execution."
    elif damage == "changed-later-page":
        calls[-1]["input"] = calls[-1]["input"].replace(f"--page {len(calls)}", "--page 1")
    elif damage == "additional-pragma-rejection":
        rejected = json.loads(json.dumps(calls[-1]))
        rejected["call_id"] = "second-incidental-correction"
        rejected["input"] = rejected["input"].replace(
            '// @exec: {"max_output_tokens": 10000}', '// @exec: "max_output_tokens": 10000', 1
        )
        last_position = rows.index(next(row for row in rows if row.get("payload") == calls[-1]))
        rows[last_position:last_position] = [
            {"type": "response_item", "payload": rejected},
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": rejected["call_id"],
                    "output": (
                        "exec pragma must be valid JSON with supported fields `yield_time_ms` and `max_output_tokens`: "
                        "trailing characters at line 1 column 20"
                    ),
                },
            },
        ]
    elif damage == "existing-plan":
        missing_plan.parent.mkdir()
        missing_plan.write_bytes((run / "inspection-plan.json").read_bytes())
    elif damage == "changed-second-coordinate":
        original_coordinate = (
            str(SKILL / "review_context.py") if failed_coordinate == "plan" else str(run / "inspection-plan.json")
        )
        failed_call["input"] = failed_call["input"].replace(
            _json_text(original_coordinate),
            _json_text(str(tmp_path / "second-missing-path" / Path(original_coordinate).name)),
        )
    rows[-1:-1] = commands
    _write_jsonl(child, rows)
    before = child.read_bytes()
    plan = run / "inspection-plan.json"
    dispatch = json.loads((run / "dispatch.json").read_bytes())
    manifest = {
        **prepare.manifest_header(json.loads(plan.read_bytes())),
        **prepare._retained_reader_identity(run, dispatch),
    }
    if damage == "historical-schema":
        manifest["schema_version"] = 7
    if damage == "historical-reader":
        manifest["context_reader_sha256"] = prepare.validator.LEGACY_SINGLE_CALL_READER_SHA256
    context_path = run / "specialists/challenger-context.md"
    attempt = {"attempt": 1, "context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest()}
    if damage in {"none", "none-first", "none-last"}:
        assembled = _assemble(run, home)
        assert assembled.returncode == 0, assembled.stderr
        prepare.validator._validate_context_read(
            rows, plan, "challenger", attempt, manifest, context_path.read_text(encoding="utf-8")
        )
    else:
        with pytest.raises(SystemExit, match=r"review-inspection-context-(?:read|command)-.*:challenger"):
            prepare.validator._validate_context_read(
                rows, plan, "challenger", attempt, manifest, context_path.read_text(encoding="utf-8")
            )
    assert child.read_bytes() == before


@pytest.mark.integration
def test_current_reader_admits_only_exact_optional_pragma_omission(tmp_path: Path, subtests: pytest.Subtests) -> None:
    """Optional outer decoration never relaxes the inner read, command receipt, or frozen source bytes.

    Every damage rewrites the challenger rollout of the same assembled native wave, so that wave is built once and each
    damage runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _tree_state(tmp_path)
    for damage in [
        "none",
        "no-final-newline",
        "different-pragma",
        "leading-space",
        "extra-newline",
        "changed-inner-budget",
        "changed-body",
        "changed-command",
        "missing-command",
        "historical-schema",
        "historical-reader",
    ]:
        with subtests.test(damage=damage):
            _restore_tree(tmp_path, assembled)
            _check_optional_pragma_omission(run, home, children, damage)


def _check_optional_pragma_omission(run: Path, home: Path, children: dict[str, Path], damage: str) -> None:
    """Omit the optional outer pragma from executed reads, apply one damage, and check read admission."""
    spec = importlib.util.spec_from_file_location("pragma_prepare", HELPER)
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    commands = []
    for call in calls:
        args = json.loads(call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        completed = subprocess.run(
            args["cmd"] if os.name == "nt" else shlex.split(args["cmd"]), capture_output=True, check=False
        )
        assert completed.returncode == 0
        assert completed.stderr == b""
        commands.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": [args["cmd"]],
                        "exit_code": 0,
                        "stdout": completed.stdout.decode("utf-8"),
                        "stderr": "",
                    },
                },
            }
        )
        assert call["input"].startswith('// @exec: {"max_output_tokens": 10000}\n')
        call["input"] = call["input"].split("\n", 1)[1].rstrip("\n") + "\n"
    if damage == "no-final-newline":
        for call in calls:
            call["input"] = call["input"].removesuffix("\n")
    elif damage == "different-pragma":
        calls[0]["input"] = '// @exec: {"max_output_tokens": 9999}\n' + calls[0]["input"]
    elif damage == "leading-space":
        calls[0]["input"] = " " + calls[0]["input"]
    elif damage == "extra-newline":
        calls[0]["input"] += "\n"
    elif damage == "changed-inner-budget":
        calls[0]["input"] = calls[0]["input"].replace('"max_output_tokens": 10000', '"max_output_tokens": 9999')
    elif damage == "changed-body":
        calls[0]["input"] = calls[0]["input"].replace("text(r.output);", "text(r);")
    elif damage == "changed-command":
        commands[0]["payload"]["item"]["command"] = ["unrelated command"]
    elif damage == "missing-command":
        commands.pop()
    rows[-1:-1] = commands
    _write_jsonl(child, rows)
    before = child.read_bytes()
    plan = run / "inspection-plan.json"
    dispatch = json.loads((run / "dispatch.json").read_bytes())
    manifest = {
        **prepare.manifest_header(json.loads(plan.read_bytes())),
        **prepare._retained_reader_identity(run, dispatch),
    }
    if damage == "historical-schema":
        manifest["schema_version"] = 7
    elif damage == "historical-reader":
        manifest["context_reader_sha256"] = prepare.validator.LEGACY_SINGLE_CALL_READER_SHA256
    context_path = run / "specialists/challenger-context.md"
    attempt = {"attempt": 1, "context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest()}
    if damage in {"none", "no-final-newline"}:
        prepare.validator._validate_context_read(
            rows, plan, "challenger", attempt, manifest, context_path.read_text(encoding="utf-8")
        )
        assembled = _assemble(run, home)
        assert assembled.returncode == 0, assembled.stderr
    else:
        with pytest.raises(SystemExit, match=r"review-inspection-context-(?:read|command)-.*:challenger"):
            prepare.validator._validate_context_read(
                rows, plan, "challenger", attempt, manifest, context_path.read_text(encoding="utf-8")
            )
    assert child.read_bytes() == before


@pytest.mark.integration
def test_completed_reads_admit_only_one_proved_pre_execution_pragma_retry(
    tmp_path: Path, subtests: pytest.Subtests
) -> None:
    """A host-rejected missing-braces pragma requires an immediate exact retry and complete executed reads.

    Every damage rewrites the challenger rollout of the same assembled native wave, so that wave is built once and each
    damage runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _tree_state(tmp_path)
    for damage in [
        "none",
        "omitted-retry-pragma",
        "truncated-json",
        "unsupported-field",
        "wrong-error",
        "mixed-output",
        "wrong-receipt",
        "executed",
        "wrong-page",
        "wrong-role",
        "changed-body",
        "changed-budget",
        "valid-json",
        "missing-retry",
        "non-immediate",
        "second-failure",
        "missing-command",
        "changed-source",
        "historical-schema",
        "historical-reader",
    ]:
        with subtests.test(damage=damage):
            _restore_tree(tmp_path, assembled)
            _check_pre_execution_pragma_retry(run, home, children, damage)


def _check_pre_execution_pragma_retry(run: Path, home: Path, children: dict[str, Path], damage: str) -> None:
    """Insert one host-rejected pragma before its retry, apply one damage, and check read admission."""
    spec = importlib.util.spec_from_file_location("rejected_pragma_prepare", HELPER)
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    calls = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    outputs = [row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call_output"]
    commands = []
    for call, output in zip(calls, outputs):
        args = json.loads(call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        commands.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": [args["cmd"]],
                        "exit_code": 0,
                        "stdout": output["output"][1]["text"],
                        "stderr": "",
                    },
                },
            }
        )
    index = 1
    failed_call = json.loads(json.dumps(calls[index]))
    failed_call["call_id"] = "host-rejected-pragma"
    failed_call["input"] = (
        failed_call["input"].replace(
            '// @exec: {"max_output_tokens": 10000}', '// @exec: "max_output_tokens": 10000', 1
        )
        + "\n"
    )
    failed_output = {
        "type": "custom_tool_call_output",
        "call_id": failed_call["call_id"],
        "output": (
            "exec pragma must be valid JSON with supported fields `yield_time_ms` and `max_output_tokens`: "
            "trailing characters at line 1 column 20"
        ),
    }
    extra = [{"type": "response_item", "payload": failed_call}, {"type": "response_item", "payload": failed_output}]
    position = rows.index(next(row for row in rows if row.get("payload") == calls[index]))
    rows[position:position] = extra
    if damage == "omitted-retry-pragma":
        calls[index]["input"] = calls[index]["input"].split("\n", 1)[1]
    elif damage == "truncated-json":
        failed_call["input"] = failed_call["input"].replace('// @exec: "', '// @exec: {"', 1)
        failed_output["output"] = failed_output["output"].replace(
            "trailing characters at line 1 column 20", "EOF while parsing an object at line 1 column 27"
        )
    elif damage == "unsupported-field":
        failed_call["input"] = failed_call["input"].replace(
            '// @exec: "max_output_tokens": 10000', '// @exec: {"unsupported": 10000}', 1
        )
        failed_output["output"] = failed_output["output"].replace(
            "trailing characters at line 1 column 20", "unknown field at line 1 column 23"
        )
    elif damage == "wrong-error":
        failed_output["output"] = "Script execution failed"
    elif damage == "mixed-output":
        failed_output["output"] += "\nunverified source"
    elif damage == "wrong-receipt":
        failed_output["call_id"] = "unbound-receipt"
    elif damage == "executed":
        commands.insert(index, json.loads(json.dumps(commands[index])))
    elif damage == "wrong-page":
        failed_call["input"] = failed_call["input"].replace("--page 2", "--page 1")
    elif damage == "wrong-role":
        failed_call["input"] = failed_call["input"].replace("--role challenger", "--role qa-specialist")
    elif damage == "changed-body":
        failed_call["input"] = failed_call["input"].replace("text(r.output);", "text(r);")
    elif damage == "changed-budget":
        failed_call["input"] = failed_call["input"].replace('"max_output_tokens": 10000', '"max_output_tokens": 9999')
    elif damage == "valid-json":
        failed_call["input"] = failed_call["input"].replace(
            '// @exec: "max_output_tokens": 10000', '// @exec: {"max_output_tokens": 9999}', 1
        )
    elif damage == "missing-retry":
        rows = [row for row in rows if row.get("payload", {}).get("call_id") != calls[index]["call_id"]]
        commands.pop(index)
    elif damage == "non-immediate":
        rows[position : position + 2] = []
        rows[position + 2 : position + 2] = extra
    elif damage == "second-failure":
        rows[position:position] = json.loads(json.dumps(extra))
    elif damage == "missing-command":
        commands.pop()
    elif damage == "changed-source":
        outputs[-1]["output"][1]["text"] += "unverified source\n"
    rows[-1:-1] = commands
    _write_jsonl(child, rows)
    before = child.read_bytes()
    plan = run / "inspection-plan.json"
    dispatch = json.loads((run / "dispatch.json").read_bytes())
    manifest = {
        **prepare.manifest_header(json.loads(plan.read_bytes())),
        **prepare._retained_reader_identity(run, dispatch),
    }
    if damage == "historical-schema":
        manifest["schema_version"] = 7
    elif damage == "historical-reader":
        manifest["context_reader_sha256"] = prepare.validator.LEGACY_SINGLE_CALL_READER_SHA256
    context_path = run / "specialists/challenger-context.md"
    attempt = {"attempt": 1, "context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest()}
    if damage in {"none", "omitted-retry-pragma", "truncated-json"}:
        prepare.validator._validate_context_read(rows, plan, "challenger", attempt, manifest, context_path.read_text())
        assembled = _assemble(run, home)
        assert assembled.returncode == 0, assembled.stderr
    else:
        with pytest.raises(SystemExit, match=r"review-inspection-context-(?:read|command)-.*:challenger"):
            prepare.validator._validate_context_read(
                rows, plan, "challenger", attempt, manifest, context_path.read_text()
            )
    assert child.read_bytes() == before


@pytest.mark.integration
def test_native_assembly_accepts_only_default_host_agent_selection(tmp_path: Path, subtests: pytest.Subtests) -> None:
    """Accept hosts without an agent selector while retaining model, delivery and lineage checks.

    Every delivery and agent-selector combination rewrites the launches of the same assembled native wave, so that wave
    is built once and each combination runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _tree_state(tmp_path)
    for delivery, agent_type in itertools.product(["plain", "opaque"], ["omitted", "default", "custom", "null"]):
        with subtests.test(delivery=delivery, agent_type=agent_type):
            _restore_tree(tmp_path, assembled)
            _check_host_agent_selection(run, home, children, delivery=delivery, agent_type=agent_type)


def _check_host_agent_selection(
    run: Path, home: Path, children: dict[str, Path], *, delivery: str, agent_type: str
) -> None:
    """Rewrite every launch with one agent selector and delivery mode and check assembly admission."""
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    for row in parent_rows:
        payload = row.get("payload", {})
        if payload.get("type") != "function_call" or payload.get("name") != "spawn_agent":
            continue
        arguments = json.loads(payload["arguments"])
        role = arguments["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        if agent_type == "omitted":
            arguments.pop("agent_type", None)
        else:
            arguments["agent_type"] = None if agent_type == "null" else agent_type
        if delivery == "opaque":
            arguments["message"] = "opaque-host-transport:" + role
            child_rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
            delivered = next(row for row in child_rows if row.get("payload", {}).get("type") == "agent_message")
            delivered["payload"]["content"][0]["encrypted_content"] = arguments["message"]
            _write_jsonl(children[role], child_rows)
        payload["arguments"] = json.dumps(arguments)
    _write_jsonl(parent_path, parent_rows)
    result = _assemble(run, home)
    if agent_type in {"omitted", "default"}:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "provenance-parent-spawn-mismatch" in result.stderr


@pytest.mark.integration
def test_native_assembly_binds_opaque_delivery_to_exact_audited_execution(
    tmp_path: Path, subtests: pytest.Subtests
) -> None:
    """Admit transported messages only with intact child delivery, exact controls, and source reads.

    Every damage alters the same prepared and assembled native wave, so that wave is built once and each damage runs as
    an independent subtest from a byte-exact restore of it.
    """
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _tree_state(tmp_path)
    for damage in [
        "none",
        "missing-delivery",
        "different-delivery",
        "duplicate-delivery",
        "extra-argument",
        "missing-argument",
        "changed-model",
        "page",
        "later-page",
        "boolean-blockers",
    ]:
        with subtests.test(damage=damage):
            _restore_tree(tmp_path, assembled)
            _check_opaque_delivery_damage(run, home, children, damage)


def _check_opaque_delivery_damage(run: Path, home: Path, children: dict[str, Path], damage: str) -> None:
    """Replace delivered messages with opaque transport, apply one damage, and check assembly admission."""
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    for row in parent_rows:
        payload = row.get("payload", {})
        if payload.get("type") != "function_call" or payload.get("name") != "spawn_agent":
            continue
        arguments = json.loads(payload["arguments"])
        role = arguments["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        opaque = arguments["message"] if damage == "boolean-blockers" else "opaque-host-transport:" + role
        arguments["message"] = opaque
        if role == "challenger" and damage == "extra-argument":
            arguments["unsupported"] = True
        if role == "challenger" and damage == "missing-argument":
            del arguments["fork_turns"]
        if role == "challenger" and damage == "changed-model":
            arguments["model"] = "unselected-model"
        payload["arguments"] = json.dumps(arguments)
        child_rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        delivery = next(row for row in child_rows if row.get("payload", {}).get("type") == "agent_message")
        delivery["payload"]["content"] = [{"type": "encrypted_content", "encrypted_content": opaque}]
        if role == "challenger":
            if damage == "missing-delivery":
                child_rows.remove(delivery)
            elif damage == "different-delivery":
                delivery["payload"]["content"][0]["encrypted_content"] = "another-transport"
            elif damage == "duplicate-delivery":
                child_rows.insert(child_rows.index(delivery), delivery)
            elif damage in {"page", "later-page"}:
                page_calls = [row for row in child_rows if row.get("payload", {}).get("type") == "custom_tool_call"]
                call = page_calls[0 if damage == "page" else 1]
                call["payload"]["input"] += "\nUnexpected execution."
        _write_jsonl(children[role], child_rows)
    _write_jsonl(parent_path, parent_rows)
    if damage == "boolean-blockers":
        path = run / "specialist-assessments.json"
        assessments = json.loads(path.read_bytes())
        assessments["challenger"]["blocking_findings"] = False
        path.write_text(json.dumps(assessments), encoding="utf-8", newline="\n")
    before = {path: path.read_bytes() for path in (parent_path, *children.values(), run / "dispatch.json")}
    result = _assemble(run, home)
    if damage == "none":
        assert result.returncode == 0, result.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        for item in manifest["passes"]:
            terminal = json.loads(before[children[item["role"]]].splitlines()[-1])["payload"]["last_agent_message"]
            assert (run / item["attempts"][0]["raw_output_path"]).read_bytes() == terminal.encode("utf-8")
    else:
        assert result.returncode != 0
        assert not (run / "specialist-manifest.json").exists()
        if damage == "boolean-blockers":
            assert "manifest-invalid-blocking-findings:challenger" in result.stderr
        elif damage in {"page", "later-page"}:
            page = 1 if damage == "page" else 2
            assert f"review-inspection-context-read-call-mismatch:challenger:{page}" in result.stderr
        elif damage in {
            "missing-delivery",
            "different-delivery",
            "duplicate-delivery",
            "missing-argument",
            "changed-model",
        }:
            assert "provenance-parent-spawn-mismatch:challenger:1" in result.stderr
        else:
            assert "review-inspection-launch-arguments-invalid:challenger:1" in result.stderr
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.integration
@pytest.mark.parametrize(
    "start", ["same-second-integer", "previous-second-integer", "prior-fractional-float", "prior-adjacent-float"]
)
def test_native_assembly_respects_observed_start_timestamp_precision(tmp_path: Path, start: str) -> None:
    """Admit overlapping integer-second start buckets while rejecting provably earlier starts."""
    run, home, children = _assembly_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    child_path = children["challenger"]
    child_rows = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    agent_path = child_rows[0]["payload"]["agent_path"]
    launch = next(
        row
        for row in parent_rows
        if row.get("payload", {}).get("name") == "spawn_agent"
        and json.loads(row["payload"]["arguments"])["task_name"] == agent_path.rsplit("/", 1)[-1]
    )
    launched_at = datetime.fromisoformat(launch["timestamp"].replace("Z", "+00:00")).timestamp()
    assert launched_at != int(launched_at)
    terminal = next(row["payload"] for row in child_rows if row.get("payload", {}).get("type") == "task_complete")
    terminal["started_at"] = {
        "same-second-integer": int(launched_at),
        "previous-second-integer": int(launched_at) - 1,
        "prior-fractional-float": launched_at - 0.001,
        "prior-adjacent-float": math.nextafter(launched_at, -math.inf),
    }[start]
    if start == "prior-adjacent-float":
        assert datetime.fromtimestamp(terminal["started_at"], timezone.utc) == datetime.fromtimestamp(
            launched_at, timezone.utc
        )
    _write_jsonl(child_path, child_rows)
    original = child_path.read_bytes()
    result = _assemble(run, home)
    if start == "same-second-integer":
        assert result.returncode == 0, result.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert {item["role"] for item in manifest["passes"]} == set(children)
    else:
        assert result.returncode != 0
        assert "review-inspection-parent-join-missing:challenger" in result.stderr
        assert not (run / "specialist-manifest.json").exists()
    assert child_path.read_bytes() == original


@pytest.mark.integration
@pytest.mark.parametrize("wait", ["prior-wave", "spanning-first-launch"])
def test_native_assembly_scopes_refill_checks_to_current_wave_dispatch(tmp_path: Path, wait: str) -> None:
    """Preserve earlier wave waits without admitting a wait that spans the next wave's launch."""
    run, home, _children = _assembly_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    returned = "2026-01-01T09:59:59Z" if wait == "prior-wave" else "2026-01-01T10:00:00.200Z"
    historical = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T09:59:58Z",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "earlier-wave-wait",
                "arguments": json.dumps({"timeout_ms": 10000}),
            },
        },
        {
            "type": "response_item",
            "timestamp": returned,
            "payload": {
                "type": "function_call_output",
                "call_id": "earlier-wave-wait",
                "output": json.dumps({"timed_out": False}),
            },
        },
    ]
    rows[1:1] = historical
    _write_jsonl(parent_path, rows)
    original = parent_path.read_bytes()
    result = _assemble(run, home)
    if wait == "prior-wave":
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "review-inspection-schedule-wait-invalid" in result.stderr
        assert not (run / "specialist-manifest.json").exists()
    assert parent_path.read_bytes() == original


def _retain_legacy_native_recipe(
    run: Path,
    home: Path,
    children: dict[str, Path],
    *,
    provenance_header: bool,
    reader_fixture: str = "legacy-context-reader",
) -> Path:
    """Retain the known historical reader and its issued messages with exact page call coordinates."""
    reader_path = Path(__file__).with_name("fixtures") / reader_fixture / "review_context.py"
    expected_digest = {
        "legacy-context-reader": "47024ba02c7dec6927356dca33ec44e9710de325fcc1fbb8ec56f66ad0a9c772",
        "legacy-all-page-context-reader": "c185dc007a261a2d9c0e449e5a888a7a771cd99336ebe8c7085432bc2b0d43c8",
    }[reader_fixture]
    assert hashlib.sha256(reader_path.read_bytes()).hexdigest() == expected_digest
    legacy = runpy.run_path(str(reader_path))
    dispatch_path = run / "dispatch.json"
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    for call in dispatch["calls"]:
        arguments = call["arguments"]
        role = arguments["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        message = legacy["dispatch_message"](
            run / "inspection-plan.json",
            role,
            1,
            dispatch["context_reader_python"],
            provenance_header=provenance_header,
            reader_path=reader_path,
        )
        arguments["message"] = message
        for row in parent_rows:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                sent = json.loads(payload["arguments"])
                if sent["task_name"] == arguments["task_name"]:
                    sent["message"] = message
                    payload["arguments"] = json.dumps(sent)
        child_rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        page = 0
        for row in child_rows:
            payload = row.get("payload", {})
            if payload.get("type") == "agent_message":
                payload["content"][0]["encrypted_content"] = message
            if payload.get("type") == "custom_tool_call":
                page += 1
                payload["input"] = legacy["render_read_call"](
                    run / "inspection-plan.json",
                    role,
                    1,
                    dispatch["context_reader_python"],
                    page,
                    reader_path,
                )
        _write_jsonl(children[role], child_rows)
    dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8", newline="\n")
    _write_jsonl(parent_path, parent_rows)
    return reader_path


@pytest.mark.integration
def test_manifest_consumer_preserves_known_historical_reader_recipe(tmp_path: Path, subtests: pytest.Subtests) -> None:
    """Admit issued historical page recipes while rejecting unknown reader bytes at consumer intake.

    Every protocol, historical reader, and reader-tampering combination rewrites the same assembled wave, so that wave
    is built and assembled once and each combination runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    manifested = _tree_state(tmp_path)
    for protocol, reader_fixture, tampered_reader in itertools.product(
        ["paged-context-v6", "paged-context-v7"],
        ["legacy-context-reader", "legacy-all-page-context-reader"],
        [False, True],
    ):
        with subtests.test(protocol=protocol, reader_fixture=reader_fixture, tampered_reader=tampered_reader):
            _restore_tree(tmp_path, manifested)
            _check_historical_reader_recipe(
                tmp_path,
                run,
                home,
                children,
                protocol=protocol,
                reader_fixture=reader_fixture,
                tampered=tampered_reader,
            )


def _check_historical_reader_recipe(
    tmp_path: Path,
    run: Path,
    home: Path,
    children: dict[str, Path],
    *,
    protocol: str,
    reader_fixture: str,
    tampered: bool,
) -> None:
    """Retain one historical recipe in an assembled manifest and check consumer intake of its reader identity."""
    tampered_reader = tampered
    reader_path = _retain_legacy_native_recipe(
        run, home, children, provenance_header=protocol == "paged-context-v6", reader_fixture=reader_fixture
    )
    manifest_path = run / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest.update(
        dispatch_protocol=protocol,
        context_reader_path=str(reader_path.resolve()),
        context_reader_sha256=hashlib.sha256(reader_path.read_bytes()).hexdigest(),
    )
    if tampered_reader:
        reader_path = tmp_path / "unknown-reader" / "review_context.py"
        reader_path.parent.mkdir()
        reader_path.write_bytes(Path(manifest["context_reader_path"]).read_bytes() + b"\n# altered reader\n")
        manifest.update(
            context_reader_path=str(reader_path),
            context_reader_sha256=hashlib.sha256(reader_path.read_bytes()).hexdigest(),
        )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    retained = {path: path.read_bytes() for path in [manifest_path, *children.values(), *run.glob("*.raw.md")]}
    result = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
            "--out",
            str(run),
            "--manifest-only",
            "--codex-home",
            str(home),
            "--parent-thread-id",
            "parent",
            "--project-root",
            str(PLUGIN_ROOT.parents[1]),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == int(tampered_reader), result.stderr
    if tampered_reader:
        assert "manifest-context-reader-identity-invalid" in result.stderr
    assert {path: path.read_bytes() for path in retained} == retained


@pytest.mark.integration
@pytest.mark.parametrize("reader_fixture", ["current", "legacy-all-page-context-reader"])
def test_manifest_consumer_rejects_workdir_from_different_reader_recipe(tmp_path: Path, reader_fixture: str) -> None:
    """Keep current cwd-free calls and historical cwd-bearing calls distinct at consumer intake."""
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    manifest_path = run / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if reader_fixture != "current":
        reader_path = _retain_legacy_native_recipe(
            run, home, children, provenance_header=False, reader_fixture=reader_fixture
        )
        manifest.update(
            context_reader_path=str(reader_path.resolve()),
            context_reader_sha256=hashlib.sha256(reader_path.read_bytes()).hexdigest(),
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    child_path = children["qa-specialist"]
    rows = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    call = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "custom_tool_call")
    arguments = json.loads(call["input"].split("tools.exec_command(", 1)[1].split("); text", 1)[0])
    if reader_fixture == "current":
        assert "workdir" not in arguments
        arguments["workdir"] = str(run)
    else:
        assert arguments.pop("workdir") == str(run.resolve())
    call["input"] = (
        '// @exec: {"max_output_tokens": 10000}\n'
        f"const r = await tools.exec_command({json.dumps(arguments, ensure_ascii=False)}); text(r.output);"
    )
    _write_jsonl(child_path, rows)
    retained = {path: path.read_bytes() for path in [manifest_path, *children.values()]}
    result = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
            "--out",
            str(run),
            "--manifest-only",
            "--codex-home",
            str(home),
            "--parent-thread-id",
            "parent",
            "--project-root",
            str(PLUGIN_ROOT.parents[1]),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "review-inspection-context-read-call-mismatch:qa-specialist:1" in result.stderr
    assert {path: path.read_bytes() for path in retained} == retained


@pytest.mark.parametrize("operation", ["assemble", "recover"])
def test_native_producer_admits_runtime_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """Count full runtime admission in the ordinary and recovery producers."""
    run, home, children = _assembly_evidence(tmp_path)
    import review_prepare

    if operation == "recover":
        reader_path = _retain_legacy_native_recipe(run, home, children, provenance_header=True)

    validator = review_prepare.validator
    original = validator._validate_review_runtime
    calls = []

    def counted_runtime(*args: object, **kwargs: object) -> dict[str, object]:
        """Preserve actual native validation while counting requests."""
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(validator, "_validate_review_runtime", counted_runtime)
    if operation == "assemble":
        summary = review_prepare.assemble(run, home)
        assert summary == json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    else:
        recovered = review_prepare.recover_native_provenance(run, home, reader_path)
        summary = recovered["summary"]
        assert summary == json.loads((run / "native-recovery/inspection-summary.json").read_text(encoding="utf-8"))
    assert summary["actual_mode"] == "parallel"
    assert len(calls) == 1


def test_assembly_fixture_rejects_malformed_compact_continuation(tmp_path: Path) -> None:
    """Reject a malformed supplied page command instead of synthesizing a correct continuation."""
    with pytest.raises(AssertionError, match="review-dispatch-page-command-invalid"):
        _assembly_evidence(tmp_path, malformed_continuation=True)


def test_assemble_rejects_unparseable_rating_before_promoting_manifest(tmp_path: Path) -> None:
    """Catch a malformed model assessment before downstream result writing fails."""
    run, home, children = _assembly_evidence(tmp_path)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    rows[-1]["payload"]["last_agent_message"] = rows[-1]["payload"]["last_agent_message"].replace(
        "Rating: 1", "Rating: 5/5"
    )
    _write_jsonl(child, rows)
    result = _assemble(run, home)
    assert result.returncode != 0
    assert "review-assessment-content-invalid:challenger" in result.stderr
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.parametrize("main", [False, True])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "No findings.\n\nRating: 1\nRationale: Evidence is complete.\n\nConfidence: 0.97.",
            1,
            id="composed-contract",
        ),
        pytest.param(
            "No findings.\n\nRating: 2\n\nRationale: The visible label Rating: is documented.\n\nConfidence: 0.95.",
            2,
            id="blank-separator-opaque-rationale",
        ),
        pytest.param("Rating: 1\nRationale: Clean.\n\nRating: 5/5", None, id="invalid-duplicate-outside-pair"),
        pytest.param(
            "Rating: 1\nRationale: Clean.\n\nRationale: Conflicting.", None, id="duplicate-rationale-paragraph"
        ),
        pytest.param("Rating: 1\nRationale: Clean.\nContinuation of rationale.", None, id="multiline-rationale"),
        pytest.param("No findings.\nRating: 1\nRationale: Clean.", None, id="ambiguous-pair-paragraph"),
        pytest.param("Rating: 1\n\nRationale: ", None, id="blank-rationale"),
        pytest.param("Rating: 1/5\nRationale: Fraction.", None, id="fraction-rating"),
        pytest.param("Rating: 1. Rationale: Clean.", None, id="ordinary-inline"),
        pytest.param("```text\n\nRating: 1\nRationale: Example.\n\n```", None, id="fenced-pair"),
    ],
)
def test_ordinary_assessment_composes_with_surrounding_contract(
    tmp_path: Path, main: bool, body: str, expected: int | None
) -> None:
    """Retain a unique standalone pair without hiding malformed declarations or continuation text."""
    spec = importlib.util.spec_from_file_location("composed_assessment_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    response = tmp_path / "response.md"
    heading = "Main Reviewer Assessment" if main else "Reviewer Assessment"
    response.write_text(f"## {heading}\n\n{body}\n", encoding="utf-8", newline="\n")
    before = response.read_bytes()
    if expected is None:
        with pytest.raises(SystemExit, match="review-assessment-content-invalid:challenger"):
            module._retained_reviewer_rating(response, local_reviewer_wave=False, main=main, role="challenger")
    else:
        assert (
            module._retained_reviewer_rating(response, local_reviewer_wave=False, main=main, role="challenger")
            == expected
        )
    assert response.read_bytes() == before


@pytest.mark.integration
def test_assemble_admits_ordinary_composed_assessment_unchanged(tmp_path: Path) -> None:
    """Ordinary assembly preserves findings and confidence surrounding the original assessment pair."""
    run, home, children = _assembly_evidence(
        tmp_path,
        findings={
            "challenger": "## Reviewer Assessment\n\nNo findings.\n\nRating: 1\nRationale: Source is complete.\n\nConfidence: 0.97; unresolved link availability (-0.03)."
        },
    )
    before = children["challenger"].read_bytes()
    result = _assemble(run, home)
    assert result.returncode == 0, result.stderr
    assert children["challenger"].read_bytes() == before
    context = (run / "specialists/challenger-context.md").read_text(encoding="utf-8")
    assert "Put findings and confidence under their own separate headings" in context


@pytest.mark.parametrize(
    "response_text",
    [
        pytest.param(
            "## Reviewer Assessment\n\nRating: 1\nRationale: Looks clean.\n\n"
            "## Reviewer Assessment\n\nRating: 5/5\nRationale: Conflicting.\n",
            id="duplicate-assessment",
        ),
        pytest.param(
            "**Reviewer Assessment**\n\nRating: 1\nRationale: Retained response.\n", id="bold-heading-rating-1"
        ),
        pytest.param(
            "**Reviewer Assessment**\n\nRating: 4\nRationale: Retained response.\n", id="bold-heading-rating-4"
        ),
    ],
)
def test_retained_reviewer_rating_rejects_noncanonical_assessment(tmp_path: Path, response_text: str) -> None:
    """A completed reviewer output certifies its rating only through one canonical assessment heading.

    Two failure shapes are covered: a second assessment section that would hide a conflicting rating behind the
    first one, and an equivalent bold "incomplete-original" heading that is never accepted for a completed reviewer
    output whatever rating it carries.
    """
    spec = importlib.util.spec_from_file_location("rating_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    response = tmp_path / "response.md"
    response.write_text(response_text, encoding="utf-8", newline="\n")
    with pytest.raises(SystemExit, match="review-assessment-content-invalid:challenger"):
        module._retained_reviewer_rating(response, local_reviewer_wave=False, main=False, role="challenger")


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        pytest.param("Rating: 2\nRationale: One obligation remains.", 2, id="canonical-lines"),
        pytest.param("Rating: 2. Rationale: One obligation remains.", 2, id="same-sentence"),
        pytest.param("Rating: 2 Rationale: One obligation remains.", 2, id="same-line"),
        pytest.param("Rating: 2; Rationale: One obligation remains.", None, id="unsupported-semicolon"),
        pytest.param("Rating: 2, Rationale: One obligation remains.", None, id="unsupported-comma"),
        pytest.param("Rating: 2\r\nRationale: One obligation remains.", 2, id="crlf"),
        pytest.param("Rating: 2. Rationale: ", None, id="empty-rationale"),
        pytest.param("Rating: 2. Rationale:   ", None, id="blank-rationale"),
        pytest.param("Rating: 6. Rationale: Out of range.", None, id="invalid-rating"),
        pytest.param("Rating: 0\nRationale: Out of range.", None, id="zero-rating"),
        pytest.param("Rating: 2/5. Rationale: Fraction.", None, id="fraction-rating"),
        pytest.param("Rating: True. Rationale: Boolean.", None, id="boolean-rating"),
        pytest.param("Rating: 2\nRating: 3\nRationale: Conflicting.", None, id="conflicting-rating"),
        pytest.param("Rating: 2\nRating: invalid\nRationale: Hidden invalid.", None, id="duplicate-invalid-rating"),
        pytest.param("Rating: 1\nRating: 5/5\nRationale: Hidden fraction.", None, id="duplicate-fraction-rating"),
        pytest.param("Rating: 2. Rationale: First.\nRationale: Second.", None, id="duplicate-rationale"),
        pytest.param("Rating: 2\nRationale: First.\nRationale: Second.", None, id="canonical-duplicate-rationale"),
        pytest.param("Rating: 2. Rationale: First.\nRating: 3", None, id="inline-pair-duplicate-rating-line"),
        pytest.param("Rating: 2. Rationale: First. Rating: 3", 2, id="opaque-inline-rating-prose"),
        pytest.param("Rating: 2. Rationale: First. Rationale: Second.", 2, id="opaque-inline-rationale-prose"),
        pytest.param("Rating: 2\nRationale: The visible label Rating: is documented.", 2, id="opaque-rating-label"),
        pytest.param(
            "Rating: 2\nRationale: The visible label Rationale: is documented.", 2, id="opaque-rationale-label"
        ),
        pytest.param("Rationale: No rating.", None, id="missing-rating"),
        pytest.param("Rating: 2", None, id="missing-rationale"),
    ],
)
@pytest.mark.parametrize("profile", ["ordinary", "main", "batch"])
def test_retained_assessment_preserves_unambiguous_fields(
    tmp_path: Path, assessment: str, expected: int | None, profile: str
) -> None:
    """Presentation changes preserve exact ratings while missing, invalid, and conflicting labels fail."""
    spec = importlib.util.spec_from_file_location("assessment_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    response = tmp_path / "response.md"
    main = profile == "main"
    if profile != "batch" and assessment.startswith(("Rating: 2. Rationale:", "Rating: 2 Rationale:")):
        expected = None
    heading = "Main Reviewer Assessment" if main else "Reviewer Assessment"
    content = f"## {heading}\n\n{assessment}\n"
    if profile == "batch":
        content = (
            "## Reviewer Findings\n```json\n[]\n```\n\n## Reviewer Confidence\n```json\n"
            '{"score": 1.0, "scope": "Frozen source.", "gaps": []}\n```\n\n' + content
        )
    response.write_text(content, encoding="utf-8", newline="\n")
    before = response.read_bytes()
    if expected is None:
        with pytest.raises(SystemExit, match="review-assessment-content-invalid:challenger"):
            module._retained_reviewer_rating(
                response, local_reviewer_wave=False, main=main, role="challenger", batch_response=profile == "batch"
            )
    else:
        assert (
            module._retained_reviewer_rating(
                response, local_reviewer_wave=False, main=main, role="challenger", batch_response=profile == "batch"
            )
            == expected
        )
    if profile == "batch":
        if expected is None:
            with pytest.raises(SystemExit, match="review-batch-individual-findings-format:challenger"):
                module._batch_reviewer_findings(response, {"files": []}, "challenger")
        else:
            assert module._batch_reviewer_findings(response, {"files": []}, "challenger") == []
    assert response.read_bytes() == before


@pytest.mark.integration
def test_assemble_preserves_opaque_rationale_label_prose(tmp_path: Path) -> None:
    """Actual native assembly accepts field-label words within a retained one-line rationale."""
    run, home, children = _assembly_evidence(
        tmp_path,
        findings={
            "challenger": "## Reviewer Assessment\n\nRating: 2\nRationale: The visible label Rating: is documented."
        },
    )
    before = children["challenger"].read_bytes()
    result = _assemble(run, home)
    assert result.returncode == 0, result.stderr
    assert children["challenger"].read_bytes() == before


@pytest.mark.parametrize("batch_response", [False, True])
@pytest.mark.parametrize("claimed_batch_findings", [False, True])
def test_inline_metadata_assessment_requires_validated_batch_profile(
    tmp_path: Path, batch_response: bool, claimed_batch_findings: bool
) -> None:
    """An arbitrary pass marker cannot widen ordinary assessment parsing without validated batch admission."""
    spec = importlib.util.spec_from_file_location("metadata_assessment_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "qa.md").write_text(
        "## Reviewer Assessment\n\nRating: 2. Rationale: One obligation remains.\n", encoding="utf-8", newline="\n"
    )
    item = {"role": "qa-specialist", "mode": "inspection", "output_path": "qa.md"}
    if claimed_batch_findings:
        item["reviewer_findings"] = []
    metadata = {"reviewer_assessments": [{"role": "QA specialist", "rating": 2, "evidence": "qa.md"}]}
    if batch_response and claimed_batch_findings:
        module._validate_reviewer_assessments(
            tmp_path, metadata, {"qa-specialist": item}, batch_response=batch_response
        )
    else:
        with pytest.raises(SystemExit, match="review-assessment-content-invalid:qa-specialist"):
            module._validate_reviewer_assessments(
                tmp_path, metadata, {"qa-specialist": item}, batch_response=batch_response
            )


def test_pr_pass_import_proof_rejects_missing_test_origins(tmp_path: Path) -> None:
    """A passing PR test gate must prove selected test files came from the reviewed tree."""
    spec = importlib.util.spec_from_file_location("pr_proof_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worktree = tmp_path / "review-worktree"
    worktree.mkdir()
    origin = (worktree / "package.py").as_posix()
    proof = {
        "mode": "in-process-pytest",
        "status": "pass",
        "worktree": worktree.as_posix(),
        "invoked_interpreter": sys.executable,
        "runtime_interpreter": sys.executable,
        "sys_prefix": sys.prefix,
        "modules": {"package": {"status": "pass", "tracked": True, "reason": None, "origin": origin}},
    }
    with pytest.raises(SystemExit, match="pr-source-review-tests-import-proof-invalid"):
        module._validate_pr_tests_import_proof(
            [{"id": "tests", "status": "pass", "python_imports": proof}], worktree.as_posix()
        )
    selected = (worktree / "test_package.py").as_posix()
    proof["tests"] = {selected: {"status": "pass", "tracked": True, "reason": None, "origin": selected}}
    module._validate_pr_tests_import_proof(
        [{"id": "tests", "status": "pass", "python_imports": proof}], worktree.as_posix()
    )
    proof["tests"][selected]["tracked"] = False
    with pytest.raises(SystemExit, match="pr-source-review-tests-import-proof-invalid"):
        module._validate_pr_tests_import_proof(
            [{"id": "tests", "status": "pass", "python_imports": proof}], worktree.as_posix()
        )
    proof["tests"] = {
        (tmp_path / "outside.py").as_posix(): {
            "status": "pass",
            "tracked": True,
            "reason": None,
            "origin": (tmp_path / "outside.py").as_posix(),
        }
    }
    with pytest.raises(SystemExit, match="pr-source-review-tests-import-proof-invalid"):
        module._validate_pr_tests_import_proof(
            [{"id": "tests", "status": "pass", "python_imports": proof}], worktree.as_posix()
        )


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        pytest.param("review-briefs.json", "review-prepared-briefs-changed", id="changed-axis"),
        pytest.param("review-routing.json", "review-prepared-routing-changed", id="changed-trigger"),
    ],
)
def test_assemble_rejects_changed_preparation_semantics(tmp_path: Path, file_name: str, expected: str) -> None:
    """Do not label frozen child findings with an axis or trigger edited after dispatch."""
    run, home, _ = _assembly_evidence(tmp_path)
    path = run / file_name
    payload = json.loads(path.read_text(encoding="utf-8"))
    if file_name == "review-briefs.json":
        payload["challenger"]["axis"] = "changed axis after dispatch"
    else:
        payload["trigger_reasons"]["challenger"] = ["Changed trigger after dispatch."]
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

    result = _assemble(run, home)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (run / "specialist-manifest.json").exists()


def test_assemble_rejects_unbound_or_incomplete_wave(
    tmp_path: Path, subtests: pytest.Subtests, text_newline_default: None
) -> None:
    """Reject plausible rollout tampering instead of accepting a fabricated pass.

    Every tampering alters the same assembled native wave, so that wave is built once per newline default and each
    tampering runs as an independent subtest from a byte-exact restore of it.
    """
    run, home, children = _assembly_evidence(tmp_path)
    assembled = _tree_state(tmp_path)
    for case, problem, expected in [
        ("missing-child", "missing-child", "review-child-session-not-unique"),
        ("mismatched-read-receipt", "wrong-receipt", "review-inspection-context-read-output-mismatch"),
        ("extra-child-tool", "extra-tool", "review-inspection-context-read-count-mismatch"),
        ("wrong-child-model", "wrong-model", "provenance-role-model-policy-mismatch"),
        ("wrong-joined-output", "wrong-output", "review-inspection-parent-join-missing"),
        ("interrupted-wave", "interrupted-launches", "review-inspection-dispatch-interrupted"),
    ]:
        with subtests.test(case=case):
            _restore_tree(tmp_path, assembled)
            _check_unbound_wave_rejected(run, home, children, problem, expected)


def _check_unbound_wave_rejected(run: Path, home: Path, children: dict[str, Path], problem: str, expected: str) -> None:
    """Tamper with one child or parent rollout and require assembly to refuse without promoting outputs."""
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    parent = home / "sessions" / "rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
    if problem == "missing-child":
        child.unlink()
    elif problem == "wrong-receipt":
        receipt = next(row for row in rows if row["payload"].get("type") == "custom_tool_call_output")
        receipt["payload"]["output"][1]["text"] = "forged context"
    elif problem == "extra-tool":
        rows.insert(
            -1,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "unexpected",
                    "input": "text('extra')",
                },
            },
        )
    elif problem == "wrong-model":
        rows[1]["payload"]["model"] = "unrequested-model"
    elif problem == "wrong-output":
        dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
        challenger = next(call for call in dispatch["calls"] if call["role"] == "challenger")
        joined = next(
            row
            for row in parent_rows
            if row["payload"].get("author") == f"/root/{challenger['arguments']['task_name']}"
        )
        joined["payload"]["content"][0]["text"] += "\nforged final"
    else:
        _record_native_schedule(run, home, children, pool_size=1)
        rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
        parent_rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
        launches = [row for row in parent_rows if row.get("payload", {}).get("name") == "spawn_agent"]
        parent_rows.insert(
            parent_rows.index(launches[1]),
            {
                "timestamp": launches[1]["timestamp"],
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "delay", "input": "text('delay')"},
            },
        )
    if problem != "missing-child":
        _write_jsonl(child, rows)
    _write_jsonl(parent, parent_rows)
    result = _assemble(run, home)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (run / "specialist-manifest.json").exists()
    assert not (run / "inspection-summary.json").exists()


@pytest.mark.parametrize(
    ("page", "expected_body"),
    [pytest.param(1, b"\r\n" * 3000, id="crlf-page"), pytest.param(2, "é".encode(), id="utf8-tail")],
)
def test_context_reader_preserves_native_stdout_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: int, expected_body: bytes
) -> None:
    """Dispatch and native stdout preserve frozen CRLF and Unicode across a byte boundary."""
    content = b"\r\n" * 3000 + "é".encode()
    (tmp_path / "context.md").write_bytes(content)
    plan = tmp_path / "inspection-plan.json"
    plan.write_text(
        json.dumps(
            {
                "review_run_id": "portable-context",
                "review_input_sha256": "a" * 64,
                "contexts": [
                    {
                        "role_id": "challenger",
                        "context_path": "context.md",
                        "context_sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    reader = runpy.run_path(str(SKILL / "review_context.py"))
    assert "Read all 2 frozen review context pages" in reader["dispatch_message"](plan, "challenger")
    output = io.BytesIO()
    native_stdout = io.TextIOWrapper(output, encoding="cp1252", newline="\r\n")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", native_stdout)
        patch.setattr(
            sys,
            "argv",
            ["review_context.py", "--plan", str(plan), "--role", "challenger", "--attempt", "1", "--page", str(page)],
        )
        runpy.run_path(str(SKILL / "review_context.py"), run_name="__main__")
        native_stdout.flush()
    actual = output.getvalue()
    expected = reader["read_context"](plan, "challenger", 1, page).encode("utf-8")
    cli = subprocess.run(
        [
            sys.executable,
            str(SKILL / "review_context.py"),
            "--plan",
            str(plan),
            "--role",
            "challenger",
            "--attempt",
            "1",
            "--page",
            str(page),
        ],
        capture_output=True,
        check=True,
    )
    assert cli.stdout == actual == expected
    assert actual.endswith(expected_body)
    assert b"\r\r\n" not in actual
    assert f"codex-review-context-page {page}/2".encode() in actual


def test_batch_module_imports_without_sibling_path_in_importlib_collection(tmp_path: Path) -> None:
    """Root doctest discovery can import the helper by path without preloading sibling modules."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import importlib.util, sys; spec = importlib.util.spec_from_file_location('collected_batches', sys.argv[1]); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); assert module.CONTEXT_LIMIT == 65536",
            str(SKILL / "review_batches.py"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _five_role_review_inputs(tmp_path: Path) -> Path:
    """Declare five required axes on the existing exact-source public preparation fixture."""
    run = _review_inputs(tmp_path)
    roles = ["challenger", "data-steward", "doc-scribe", "qa-specialist", "sw-engineer"]
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["signals"].update(behavior_change=True, axis_data_steward=True, axis_doc_scribe=True)
    routing.update(triggered_roles=roles, trigger_reasons={role: ["Declared source review axis."] for role in roles})
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    (run / "files.txt").write_text("widget.py\n", encoding="utf-8", newline="\n")
    briefs = {}
    for index, role in enumerate(roles, 1):
        evidence = f"{role}-evidence.md"
        (run / evidence).write_text(
            f"Scope: widget.py. Question: {role}.\n" + "Evidence.\n" * (index * 100), encoding="utf-8", newline="\n"
        )
        briefs[role] = {"axis": role, "evidence_path": evidence, "source_paths": ["widget.py"]}
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    return run


def _finding_id_recovery_evidence(tmp_path: Path) -> tuple[Path, Path, dict[str, Path], str, str]:
    """Prepare admitted source origins and one actual context-read interaction carrying the copied namespace."""
    import test_review_batches as batches  # Reuse the established reciprocal batch fixture boundary.

    root = batches._batch_inputs(tmp_path)
    assert batches._batch_command(root, "prepare").returncode == 0
    home = tmp_path / "codex-home"
    schedule = json.loads((root / "batch-dispatch.json").read_bytes())
    record = batches._batch_finding("LOCAL_A", "medium", "Preserve the exact source obligation.")
    for wave in schedule["waves"]:
        source = root / wave["directory"]
        _assembly_evidence(
            tmp_path,
            prepared_run=source,
            home=home,
            wave_index=wave["wave"],
            findings={"challenger": batches._batch_output([record], 3)} if wave["wave"] == 1 else None,
            blocking_counts={"challenger": 1} if wave["wave"] == 1 else None,
        )
        admitted = batches._batch_command(source, "assemble-wave", home)
        assert admitted.returncode == 0, admitted.stderr
    inventory = json.loads((root / "batch-inventory.json").read_bytes())
    ranges = [
        {"path": item["path"], "start_line": 1, "end_line": len(item["content"].splitlines())}
        for item in inventory["source_snapshot"]["files"]
        if item["kind"] != "missing"
    ]
    briefs = {}
    for role in json.loads((root / "review-briefs.json").read_bytes()):
        (root / f"{role}-interactions.md").write_text(
            "Inspect the supplied original obligation.\n", encoding="utf-8", newline="\n"
        )
        briefs[role] = {
            "axis": "Source obligation interactions",
            "evidence_path": f"{role}-interactions.md",
            "source_paths": ranges,
            "paired_ranges": [
                [{"path": path, "start_line": 1, "end_line": 1} for path in ("widget.py", "other.py", "stable.py")]
            ],
        }
    (root / "interaction-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
    prepared = batches._batch_command(root, "prepare-interactions", home)
    assert prepared.returncode == 0, prepared.stderr
    identity = "source-001.challenger.LOCAL_A"
    interaction = json.loads((root / "interaction-dispatch.json").read_bytes())["waves"]
    wave = next(
        wave
        for wave in interaction
        if f"Source finding ID: {identity}\n"
        in (root / wave["directory"] / "specialists/qa-specialist-context.md").read_text(encoding="utf-8")
    )
    run = root / wave["directory"]
    original = batches._batch_output([{**record, "id": identity}], 3)
    _, home, children = _assembly_evidence(
        tmp_path,
        prepared_run=run,
        home=home,
        wave_index=len(schedule["waves"]) + interaction.index(wave) + 1,
        findings={"qa-specialist": original},
        blocking_counts={"qa-specialist": 1},
    )
    return run, home, children, original, original.replace(json.dumps(identity), json.dumps(record["id"]), 1)
