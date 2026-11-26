"""Exercise optional exact-head structural context through the adapter's skill CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from unittest import mock

import pytest
from _modules import load_module_registered

_PLUGIN = Path(__file__).resolve().parents[2]
_ADAPTER = load_module_registered("exact_head_codemap_adapter", _PLUGIN / "shared" / "codemap_adapter.py")
_VALIDATOR_PATH = _PLUGIN / "skills" / "code-review" / "validate_artifacts.py"
_HEAD = "a" * 40
_DIFF = "diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


@pytest.fixture
def review_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Collect a real detached mirror with changed and new Python source in a disposable repository."""
    root, run = tmp_path / "repo", tmp_path / "run"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "mod.py").write_text("def load():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "mod.py"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "base"],
                   cwd=root, check=True)  # fmt: skip
    (root / "mod.py").write_text("def load():\n    return 2\n", encoding="utf-8")
    (root / "consumer.py").write_text("from mod import load\n\nresult = load()\n", encoding="utf-8")
    report = run / "local-source"
    subprocess.run([sys.executable, str(_PLUGIN / "shared/collect_diff.py"), "--review-worktree",
                    "--repository", str(root), "--out", str(report)], check=True, capture_output=True)  # fmt: skip
    patch = report / "diff.patch"
    (run / "diff.patch").write_bytes(patch.read_bytes())
    monkeypatch.delenv("CODEMAP_BIN", raising=False)
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    monkeypatch.delenv("SCAN_NO_AUTOBUILD", raising=False)
    if (3, 11) <= sys.version_info[:2] < (3, 15):
        monkeypatch.setenv("CODEMAP_PYTHON", sys.executable)
    return run / "local-source-review-worktree", run, _PLUGIN.parent / "codemap-py"


@pytest.mark.integration
@pytest.mark.parametrize("pathspec", [pytest.param(".", id="working-tree"), pytest.param("mod.py", id="path")])
def test_local_mirror_context_describes_overlay_source(review_mirror: tuple[Path, Path, Path], pathspec: str) -> None:
    """Query the real producer for the mirror overlay, including an untracked caller absent from HEAD."""
    root, run, provider = review_mirror
    diff = subprocess.check_output(["git", "diff", "HEAD", "--", pathspec], cwd=root)
    (run / "diff.patch").write_bytes(diff)
    artifact = _run_adapter(root, run, provider)
    producer = json.loads((run / "codemap-structural-context.json").read_bytes())
    snapshot = (run / "local-source/source-snapshot.json").read_bytes()
    assert artifact["status"] == "available"
    assert producer["expected_head"] is None
    assert artifact["structural_context"]["source_snapshot_sha256"] == sha256(snapshot).hexdigest()
    module = artifact["queries"][0]["answer"]["changed_modules"][0]
    assert "consumer" in module["importers"]
    validator = load_module_registered("mirror_codemap_validator", _VALIDATOR_PATH)
    validator._validate_codemap_primary_review(artifact, run, ["mod.py"])


@pytest.mark.integration
@pytest.mark.parametrize("digest", [pytest.param("0" * 64, id="different-overlay"), pytest.param(None, id="missing")])
def test_primary_review_requires_the_mirror_snapshot_digest(
    review_mirror: tuple[Path, Path, Path], digest: str | None
) -> None:
    """Reject unbound structural evidence even when the overlay and receipt share a Git anchor."""
    root, run, provider = review_mirror
    artifact = _run_adapter(root, run, provider)
    record = artifact["structural_context"]
    assert artifact["status"] == "available"
    if digest is None:
        del record["source_snapshot_sha256"]
    else:
        record["source_snapshot_sha256"] = digest
    validator = load_module_registered("mirror_codemap_validator", _VALIDATOR_PATH)
    with pytest.raises(SystemExit, match=r"^codemap-context-invalid:source_snapshot_sha256$"):
        validator._validate_codemap_primary_review(artifact, run, ["mod.py"])


@pytest.mark.integration
def test_local_mirror_drift_refuses_structural_queries(review_mirror: tuple[Path, Path, Path]) -> None:
    """Refuse a changed overlay even if the mirror and its original diff still name the same commit."""
    root, run, provider = review_mirror
    (root / "consumer.py").write_text("result = 42\n", encoding="utf-8")
    artifact = _run_adapter(root, run, provider)
    assert artifact["status"] == "degraded"
    assert artifact["structural_context"]["cause"] == "source-unavailable"
    assert artifact["queries"] == []
    assert not (run / "codemap-structural-context.json").exists()
    validator = load_module_registered("mirror_codemap_validator", _VALIDATOR_PATH)
    validator._validate_codemap_primary_review(artifact, run, ["mod.py"])


@pytest.fixture
def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create the source and run destinations the existing review recipe passes to the adapter."""
    root, run = tmp_path / "checkout", tmp_path / "run"
    root.mkdir()
    run.mkdir()
    (run / "diff.patch").write_text(_DIFF, encoding="utf-8")
    (run / "local-checkout.json").write_text(
        json.dumps(
            {
                "worktree": root.as_posix(),
                "expected_head": _HEAD,
                "local_head": _HEAD,
                "head_matches_pr": True,
                "diff_source": "verified-local-checkout",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("CODEMAP_BIN", raising=False)
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    return root, run


def _run_adapter(root: Path, run: Path, provider: Path | None = None) -> dict:
    """Invoke the structural-context command the skills use and read its persisted result."""
    out = run / "codemap-context.json"
    arguments = ["context", "--category", "review", "--root", str(root), "--diff-file", str(run / "diff.patch")]
    if provider is not None:
        arguments += ["--provider-root", str(provider)]
    assert _ADAPTER.main([*arguments, "--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _record(root: Path, run: Path, status: str, cause: str | None) -> dict:
    """Build the relevant fields of a v1 context answer for the collected source."""
    answer = {
        "changed_files": 1,
        "changed_modules": [{"module": "mod"}],
        "index": {"query_complete": status == "available"},
    }
    fallback = {"method": "text-search", "confidence": "low", "query_complete": False}
    return {
        "schema": "codemap-py.structural-context",
        "schema_version": 1,
        "root": str(root.resolve()),
        "diff_file": str((run / "diff.patch").resolve()),
        "expected_head": _HEAD,
        "head": None if status == "not-applicable" else _HEAD,
        "status": status,
        "cause": cause,
        "detail": "provider detail" if cause else "",
        "degraded_reasons": ["completeness: partial"] if status == "degraded" else [],
        "query_complete": status == "available",
        "diff_impact": answer if status in ("available", "degraded") else None,
        "fallback": fallback if status == "unavailable" else None,
    }


@pytest.mark.parametrize(
    ("provider_status", "cause", "status", "reasons"),
    [
        pytest.param("available", None, "available", [], id="complete-answer"),
        pytest.param("degraded", None, "degraded", ["context: completeness: partial"], id="incomplete-answer"),
        pytest.param(
            "unavailable", "index-missing", "degraded", ["context: index-missing: provider detail"], id="missing-index"
        ),
        pytest.param(
            "unavailable",
            "index-build-failed",
            "degraded",
            ["context: index-build-failed: provider detail"],
            id="failed-build",
        ),
        pytest.param(
            "unavailable", "future-cause", "degraded", ["context: query-failed: provider detail"], id="future-cause"
        ),
        pytest.param("not-applicable", "non-python-scope", "available", [], id="non-python-diff"),
    ],
)
def test_verified_review_maps_provider_record(
    review: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    provider_status: str,
    cause: str | None,
    status: str,
    reasons: list[str],
) -> None:
    """Request context at the receipt head and map each producer outcome without querying again."""
    root, run = review
    diff_text = _DIFF.replace("mod.py", "notes.txt") if provider_status == "not-applicable" else _DIFF
    (run / "diff.patch").write_text(diff_text, encoding="utf-8")
    launcher = str(run / "codemap-py")
    monkeypatch.setattr(_ADAPTER.shutil, "which", mock.Mock(return_value=launcher))
    provider_record = _record(root, run, provider_status, cause)

    def reply(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Emit the provider record on stdout and through the requested sidecar."""
        assert argv[0:2] == [launcher, "context"]
        assert argv[argv.index("--root") + 1] == str(root.resolve())
        assert argv[argv.index("--expect-head") + 1] == _HEAD
        assert argv[argv.index("--diff-file") + 1] == str((run / "diff.patch").resolve())
        sidecar = Path(argv[argv.index("--out") + 1])
        assert sidecar == run.resolve() / "codemap-structural-context.json"
        sidecar.write_text(json.dumps(provider_record), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps(provider_record), "")

    child = mock.Mock(side_effect=reply)
    monkeypatch.setattr(_ADAPTER.subprocess, "run", child)
    output = _run_adapter(root, run)

    child.assert_called_once()
    assert output["status"] == status
    assert output["status_reasons"] == reasons
    assert output["structural_context"]["status"] == provider_status
    assert output["structural_context"]["fallback"] == provider_record["fallback"]
    expected = [] if provider_record["diff_impact"] is None else [{"module": "mod"}]
    assert [module for query in output["queries"] for module in query["answer"]["changed_modules"]] == expected


def test_missing_launcher_records_nonfatal_reason(
    review: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record absent Codemap for the review batch also used by remediation."""
    root, run = review
    monkeypatch.setenv("PATH", str(run))
    monkeypatch.setattr(_ADAPTER.subprocess, "run", mock.Mock(autospec=True))
    artifact = _run_adapter(root, run)

    assert artifact["status"] == "absent"
    assert artifact["status_reasons"] == ["context: codemap-not-installed: codemap-py not found on PATH"]
    assert artifact["queries"] == []
    _ADAPTER.subprocess.run.assert_not_called()


@pytest.fixture(params=[True, False])
def has_receipt(request: pytest.FixtureRequest, review: tuple[Path, Path]) -> bool:
    """Supply a retained checkout receipt for only one of the legacy routes."""
    if not request.param:
        (review[1] / "local-checkout.json").unlink()
    return request.param


def test_legacy_query_keeps_the_collected_diff(
    review: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, has_receipt: bool
) -> None:
    """No receipt or an older launcher selects the existing doctor and diff-impact calls."""
    root, run = review
    launcher = str(run / "codemap-py")
    doctor = {
        "python": sys.executable,
        "version": "3.12",
        "implementation": "cpython",
        "supported": True,
        "plugin_root": str(root),
        "index_path": str(run / "index.json"),
    }
    answer = _record(root, run, "available", None)["diff_impact"]
    unsupported = (
        [subprocess.CompletedProcess([], 2, "", "codemap-py: unknown command 'context'")] if has_receipt else []
    )
    responses = [
        *unsupported,
        *(subprocess.CompletedProcess([], 0, json.dumps(item).encode(), b"") for item in (doctor, answer)),
    ]
    child = mock.Mock(side_effect=responses)
    monkeypatch.setattr(_ADAPTER.shutil, "which", mock.Mock(return_value=launcher))
    monkeypatch.setattr(_ADAPTER.subprocess, "run", child)

    artifact = _run_adapter(root, run)

    commands = [call.args[0] for call in child.call_args_list]
    assert [argv[1] for argv in commands[-2:]] == ["doctor", "query"]
    assert len(commands) == (3 if has_receipt else 2)
    assert commands[-1][-2:] == ["--diff-file", str(run / "diff.patch")]
    assert all(call.kwargs["cwd"] == root for call in child.call_args_list[-2:])
    assert "structural_context" not in artifact
    assert artifact["status"] == "available"


@pytest.mark.parametrize(
    ("child_result", "cause"),
    [
        pytest.param(subprocess.TimeoutExpired("context", 1), "codemap-timeout", id="deadline"),
        pytest.param(
            subprocess.CompletedProcess([], 127, "", "no Python"),
            "codemap-interpreter-unsupported",
            id="unsupported-interpreter",
        ),
        pytest.param(
            subprocess.CompletedProcess([], 2, "", '{"error": "invalid_context_request", "detail": "bad head"}'),
            "context-request-invalid",
            id="provider-refused-the-request",
        ),
        pytest.param(
            subprocess.CompletedProcess([], 2, "", "Traceback: boom"),
            "query-failed",
            id="provider-crash-on-usage-exit",
        ),
        pytest.param(subprocess.CompletedProcess([], 0, "[" * 10_000, ""), "query-failed", id="unparseable-response"),
        pytest.param(subprocess.CompletedProcess([], 2, "", "[" * 10_000), "query-failed", id="unparseable-diagnostic"),
    ],
)
def test_context_transport_failure_stays_optional(
    review: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    child_result: subprocess.TimeoutExpired | subprocess.CompletedProcess[str],
    cause: str,
) -> None:
    """Record a closed transport cause without starting a legacy retry."""
    root, run = review
    monkeypatch.setattr(_ADAPTER.shutil, "which", mock.Mock(return_value=str(run / "codemap-py")))
    result = mock.Mock(side_effect=[child_result])
    monkeypatch.setattr(_ADAPTER.subprocess, "run", result)

    artifact = _run_adapter(root, run)

    result.assert_called_once()
    assert artifact["status"] == "degraded"
    assert artifact["structural_context"]["cause"] == cause
    assert artifact["queries"] == []


@pytest.mark.parametrize(
    ("override", "cwd_is_root", "expected"),
    [
        pytest.param(".cache/codemap", False, "cwd", id="relative-override-resolves-against-cwd"),
        pytest.param(".cache/codemap", True, "run", id="relative-override-inside-root-uses-the-run"),
        pytest.param("", False, "run", id="no-override-uses-the-run"),
    ],
)
def test_context_index_base_is_outside_the_reviewed_root(
    review: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    override: str,
    cwd_is_root: bool,
    expected: str,
) -> None:
    """The index base passed to the producer is absolute and outside the reviewed checkout, whatever the override.

    A relative ``CODEMAP_INDEX_DIR`` such as ``.cache/codemap`` names a directory beside the caller's own checkout;
    joined under the reviewed root it made the producer refuse the request, so every review lost structural context.
    """
    root, run = review
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(root if cwd_is_root else caller)
    if override:
        monkeypatch.setenv("CODEMAP_INDEX_DIR", override)
    monkeypatch.setattr(_ADAPTER.shutil, "which", mock.Mock(return_value=str(run / "codemap-py")))
    record = _record(root, run, "unavailable", "index-missing")
    child = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(record), ""))
    monkeypatch.setattr(_ADAPTER.subprocess, "run", child)

    _run_adapter(root, run)

    argv = child.call_args.args[0]
    index_base = Path(argv[argv.index("--index-dir") + 1])
    want = (caller / override).resolve() if expected == "cwd" else (run / "codemap-index").resolve()
    assert (index_base, index_base.is_absolute(), index_base.is_relative_to(root.resolve())) == (want, True, False)


def test_mismatched_head_has_no_fallback(
    review: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry the producer's exact cause and discard any search hits from a mismatched checkout."""
    root, run = review
    record = _record(root, run, "unavailable", "head-mismatch")
    record["head"] = "b" * 40
    monkeypatch.setattr(_ADAPTER.shutil, "which", mock.Mock(return_value=str(run / "codemap-py")))
    child = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(record), ""))
    monkeypatch.setattr(_ADAPTER.subprocess, "run", child)

    artifact = _run_adapter(root, run)

    child.assert_called_once()
    assert artifact["status_reasons"] == ["context: head-mismatch: provider detail"]
    assert artifact["structural_context"]["fallback"] is None
    assert artifact["queries"] == []


def test_validator_accepts_closed_unavailability_and_binds_the_reviewed_head(
    review: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept an unavailable index without waiving source binding or the diagnostic check."""
    root, run = review
    validator = load_module_registered("exact_head_codemap_validator", _VALIDATOR_PATH)
    (run / "files.txt").write_text("mod.py\n", encoding="utf-8")
    record = _record(root, run, "unavailable", "index-build-failed")
    monkeypatch.setattr(_ADAPTER.shutil, "which", mock.Mock(return_value=str(run / "codemap-py")))
    child = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(record), ""))
    monkeypatch.setattr(_ADAPTER.subprocess, "run", child)

    artifact = _run_adapter(root, run)
    validator._validate_codemap_context(run, {})

    artifact["structural_context"]["expected_head"] = "b" * 40
    (run / "codemap-context.json").write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(SystemExit, match="codemap-context-invalid:structural_context"):
        validator._validate_codemap_context(run, {})


@pytest.mark.parametrize(
    ("receipt", "fields"),
    [
        pytest.param(
            "pr/local-checkout.json",
            {
                "expected_head": _HEAD,
                "local_head": _HEAD,
                "head_matches_pr": True,
                "diff_source": "verified-local-checkout",
            },
            id="remediation-pr",
        ),
        pytest.param(
            "local-worktree-owner.json",
            {"expected_head": _HEAD, "checkout_mode": "local-review", "scope": "commit"},
            id="local-commit",
        ),
    ],
)
def test_review_uses_the_head_from_each_checkout_receipt(
    review: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, receipt: str, fields: dict
) -> None:
    """Use the reviewed head on the other existing worktree layouts."""
    root, run = review
    (run / "local-checkout.json").unlink()
    receipt_path = run / receipt
    receipt_path.parent.mkdir(exist_ok=True)
    receipt_path.write_text(json.dumps({"worktree": root.as_posix(), **fields}), encoding="utf-8")
    record = _record(root, run, "available", None)
    monkeypatch.setattr(_ADAPTER.shutil, "which", mock.Mock(return_value=str(run / "codemap-py")))
    child = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(record), ""))
    monkeypatch.setattr(_ADAPTER.subprocess, "run", child)

    artifact = _run_adapter(root, run)

    child.assert_called_once()
    argv = child.call_args.args[0]
    assert argv[1] == "context"
    assert argv[argv.index("--expect-head") + 1] == _HEAD
    assert artifact["status"] == "available"
