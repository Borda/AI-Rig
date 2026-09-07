"""Codemap-py integrate engine — check/plan/apply/sync/demo.

Black-box against the public API in ``src/codemap_py/integration.py``: ``run``,
``build_plan``, ``compute_plan_sha256``, ``load_plan``, ``verify_approval``,
``apply_plan``, ``sync_plan``, ``build_audit_report``, ``run_demo``, ``resolve_targets``,
``Journal``, ``IntegrationError``/``RefusalError``/``ApprovalError``, and the module's own
named internal helpers (``_render_managed_block``, ``_managed_block_status``,
``_unsafe_windows_batch_argv``, ``_resolve_native_command``) that w-engine.md calls out as
test-writer-facing.

Every apply/sync test builds a disposable fixture repo under ``tmp_path`` mirroring the
closed target set's directory shape (``plugins/cc_*``, ``plugins/codex-rig``,
``plugins/codemap-py``) and ``monkeypatch.chdir``s into it so ``index_paths.canonical_root()``
resolves there — the real ``plugins/cc_*``/``plugins/codemap-py`` trees are never touched.
Native-CLI-dependent ``sync`` behavior is exercised by monkeypatching the module's own
``_native_json_probe``/``_run_native_required`` seams rather than requiring an installed
``claude``/``codex`` CLI on the test runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from enum import Enum
from pathlib import Path

import pytest

from codemap_py import integration

_PATH_CLASSES = {"normal": "repo", "spaces_nonascii": "a repo café"}
_SOURCE_CHECKOUT = (Path(__file__).resolve().parents[4] / integration.PROVIDER_DIR).is_dir()


# --------------------------------------------------------------------------------------
# Fixture-tree builder — disposable consumer/provider trees, never the real repo.
# --------------------------------------------------------------------------------------


def _seed(path: Path, text: str) -> None:
    """Write *text* verbatim (LF-only, no OS newline translation) so fixtures are byte-exact cross-platform."""
    path.write_text(text, newline="\n")


def _write_manifest(plugin_dir: Path, runtime: integration.Runtime, name: str, version: str) -> None:
    """Write the minimal runtime manifest used to build an integration fixture.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     _write_manifest(Path(directory), integration.Runtime.CLAUDE, "demo", "1.0.0")
    ...     json.loads((Path(directory) / ".claude-plugin" / "plugin.json").read_text())["name"]
    'demo'
    """
    manifest_dir = plugin_dir / (".claude-plugin" if runtime == integration.Runtime.CLAUDE else ".codex-plugin")
    manifest_dir.mkdir(parents=True, exist_ok=True)
    _seed(manifest_dir / "plugin.json", json.dumps({"name": name, "version": version}))


def _build_repo(base: Path, *, path_class: str = "normal") -> Path:
    """Build a disposable repo tree with every closed-set consumer + provider manifest."""
    root = base / _PATH_CLASSES[path_class]
    root.mkdir(parents=True)
    for target in integration.ALL_TARGETS:
        _write_manifest(root / target.plugin_dir, target.runtime, target.consumer, "1.0.0")
    _write_manifest(root / integration.PROVIDER_DIR, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, "9.9.9")
    _write_manifest(root / integration.PROVIDER_DIR, integration.Runtime.CODEX, integration.PROVIDER_NAME, "9.9.9")
    return root


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    """Return every regular file's repo-relative path mapped to its bytes."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _git(root: Path, *args: str) -> None:
    """Run a successful git command in an integration fixture repository."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


def _git_commit_all(root: Path) -> None:
    """Commit all current fixture files with a local test identity."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture baseline")


@pytest.fixture(name="path_class", params=["normal", "spaces_nonascii"])
def _path_class(request: pytest.FixtureRequest) -> str:
    """Return the requested normal or space-and-non-ASCII fixture path class."""
    return request.param


@pytest.fixture(name="repo")
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_class: str) -> Path:
    """Provide a disposable repository as the current working directory."""
    root = _build_repo(tmp_path, path_class=path_class)
    monkeypatch.chdir(root)
    return root


# --------------------------------------------------------------------------------------
# audit — zero-write.
# --------------------------------------------------------------------------------------


def test_audit_json_v2_fails_current_flat_runtime_logs(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Current flat telemetry must fail audit instead of inheriting declared-path health."""
    log_dir = repo / ".cache" / "codemap" / "logs"
    log_dir.mkdir(parents=True)
    _seed(
        log_dir / "cli_current.jsonl",
        json.dumps({"ts": "2026-08-18T11:00:00Z", "layer": "cli", "v": integration.__version__}) + "\n",
    )

    code = integration.run(["audit", "--runtime", "claude", "--json"], repo / integration.PROVIDER_DIR)
    assert code == integration._EXIT_RUNTIME
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == 2
    assert payload["protocol"] == "codemap-py.integration.v2"
    assert payload["status"] == "fail"
    assert {finding["code"] for finding in payload["findings"]} >= {
        "runtime_identity_missing",
        "runtime_log_isolation_bypassed",
    }


@pytest.mark.parametrize(
    ("runtime", "claude_index", "codex_index", "expects_split"),
    [
        ("both", "/indexes/claude.json", "/indexes/codex.json", True),
        ("both", "/indexes/shared.json", "/indexes/shared.json", False),
        ("claude", "/indexes/claude.json", None, False),
    ],
    ids=["both-diverge", "both-share", "one-runtime"],
)
def test_audit_index_paths_report_only_selected_runtime_divergence(
    repo: Path,
    runtime: str,
    claude_index: str,
    codex_index: str | None,
    expects_split: bool,
) -> None:
    """Nested and top-level telemetry paths fail only when selected runtimes disagree."""
    claude_log = repo / ".cache" / "codemap" / "logs" / "claude" / "nested"
    claude_log.mkdir(parents=True)
    _seed(
        claude_log / "cli.jsonl",
        json.dumps(
            {
                "ts": "2026-08-18T11:00:00Z",
                "layer": "cli",
                "runtime": "claude",
                "v": integration.__version__,
                "result": {"index": {"index_path": claude_index}},
            }
        )
        + "\n",
    )
    if codex_index is not None:
        codex_log = repo / ".cache" / "codemap" / "logs" / "codex"
        codex_log.mkdir(parents=True)
        _seed(
            codex_log / "cli.jsonl",
            json.dumps(
                {
                    "ts": "2026-08-18T11:00:01Z",
                    "layer": "cli",
                    "runtime": "codex",
                    "v": integration.__version__,
                    "result": {"index_path": codex_index},
                }
            )
            + "\n",
        )

    report = integration.build_audit_report(runtime, repo / integration.PROVIDER_DIR)
    codes = {finding["code"] for finding in report["findings"]}

    assert ("split_index_roots" in codes) is expects_split
    if expects_split:
        split = next(finding for finding in report["findings"] if finding["code"] == "split_index_roots")
        assert split["evidence"]["observed_runtime_paths"] == {
            "claude": [claude_index],
            "codex": [codex_index],
        }


@pytest.mark.parametrize(
    ("indexed_sha", "expected_stale_state"),
    [("current", None), ("stale", "stale")],
    ids=["matching-sha", "stale-sha"],
)
def test_audit_index_evidence_reports_degraded_and_stale_state_without_query(
    repo: Path, monkeypatch: pytest.MonkeyPatch, indexed_sha: str, expected_stale_state: str | None
) -> None:
    """Audit reads bounded index evidence: degraded modules persist, SHA drift is explicit, query stays forbidden."""
    identity = integration.index_paths.resolve_index(root=repo)
    identity.index_path.parent.mkdir(parents=True, exist_ok=True)
    _seed(
        identity.index_path,
        json.dumps(
            {
                "git_sha": indexed_sha,
                "modules": [{"path": "pkg/broken.py", "status": "degraded", "reason": "parse error"}],
            }
        ),
    )
    monkeypatch.setattr(integration.scanner, "get_git_sha", lambda root: "current")
    monkeypatch.setattr(
        integration.query,
        "main",
        lambda *args, **kwargs: pytest.fail("audit must not invoke query while inspecting index evidence"),
    )

    report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)
    findings = {finding["code"]: finding for finding in report["findings"]}

    assert findings["index_degraded"]["evidence"] == {
        "count": 1,
        "modules": [{"path": "pkg/broken.py", "reason": "parse error"}],
    }
    assert ("index_stale_or_unknown" in findings) is (expected_stale_state is not None)
    if expected_stale_state is not None:
        assert findings["index_stale_or_unknown"]["evidence"]["state"] == expected_stale_state


def test_audit_direct_records_are_reported_separately_and_do_not_observe_claude(repo: Path) -> None:
    """Per-invocation direct telemetry remains visible without satisfying selected-runtime evidence."""
    log_dir = repo / ".cache" / "codemap" / "logs" / "direct"
    log_dir.mkdir(parents=True)
    direct_index = "/indexes/direct.json"
    _seed(
        log_dir / "cli.jsonl",
        json.dumps(
            {
                "ts": "2026-08-18T11:00:00Z",
                "layer": "cli",
                "runtime": "direct",
                "v": integration.__version__,
                "result": {"index_path": direct_index},
            }
        )
        + "\n",
    )

    report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)
    codes = {finding["code"] for finding in report["findings"]}

    assert report["runtime_logs"]["direct"] == {
        "files": 1,
        "records": 1,
        "current_records": 1,
        "state": "observed",
        "truncated": False,
    }
    assert report["runtime_logs"]["selected"]["claude"]["state"] == "not_observed"
    assert report["shared_index"]["observed_runtime_paths"] == {"claude": [], "direct": [direct_index]}
    assert report["usage"]["telemetry_records"] == 0
    assert "runtime_logs_not_observed" in codes


def test_audit_exposes_monkeypatched_codex_rig_global_status(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex audits surface the read-only global-instructions status instead of silently omitting it."""
    monkeypatch.setattr(integration, "codex_rig_global_status", lambda: "authenticated")

    report = integration.build_audit_report("codex", repo / integration.PROVIDER_DIR)

    assert report["provider"]["codex_rig_global_instructions"] == "authenticated"


def test_audit_observes_same_version_native_content_divergence_and_session_catalog_limit(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Audit must fail same-version provider bytes while naming the native session-catalog boundary."""
    _write_manifest(
        repo / integration.PROVIDER_DIR, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, integration.__version__
    )
    (repo / integration.PROVIDER_DIR / "README.md").write_text("source bytes\n")
    native = tmp_path / "native-codemap"
    _write_manifest(native, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, integration.__version__)
    _write_manifest(native, integration.Runtime.CODEX, integration.PROVIDER_NAME, integration.__version__)
    (native / "README.md").write_text("installed bytes\n")
    monkeypatch.setattr(
        integration,
        "_native_json_probe",
        lambda argv: [
            {
                "id": "codemap-py@borda-ai-rig",
                "version": integration.__version__,
                "enabled": True,
                "installPath": str(native),
            }
        ],
    )

    report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)
    provider = report["provider"]["runtimes"]["claude"]["provider"]
    findings = {finding["code"]: finding for finding in report["findings"]}

    assert report["provider"]["runtimes"]["claude"]["session_catalog"] == {
        "state": "unobservable",
        "reason": "native_plugin_list_has_no_session_catalog_provenance",
    }
    assert provider["native_plugin"] == {
        "state": "observed",
        "name": integration.PROVIDER_NAME,
        "version": integration.__version__,
        "enabled": True,
        "source_path": str(native),
    }
    for key in ("source_content", "native_content"):
        content = provider[key]
        assert content["state"] == "observed"
        assert content["schema_version"] == 1
        assert len(content["sha256"]) == 64
        assert content["file_count"] >= 2
        assert content["bytes_hashed"] > 0
    assert provider["source_content"]["sha256"] != provider["native_content"]["sha256"]
    assert findings["provider_same_version_content_drift"] == {
        "code": "provider_same_version_content_drift",
        "severity": "high",
        "status": "fail",
        "evidence": {
            "source_version": integration.__version__,
            "installed_version": integration.__version__,
            "source_sha256": provider["source_content"]["sha256"],
            "native_sha256": provider["native_content"]["sha256"],
        },
        "affected_runtime": ["claude"],
        "remediation_kind": "plan_sync",
    }


@pytest.mark.parametrize("native_path", ["same-source", "unreadable-native"], ids=["same-digest", "unreadable"])
def test_audit_content_identity_equal_or_unknown_never_claims_drift(
    repo: Path, monkeypatch: pytest.MonkeyPatch, native_path: str
) -> None:
    """Equal bytes and unreadable native roots are distinct evidence states, neither a false drift finding."""
    _write_manifest(
        repo / integration.PROVIDER_DIR, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, integration.__version__
    )
    path = repo / integration.PROVIDER_DIR if native_path == "same-source" else repo / "missing-native-root"
    monkeypatch.setattr(
        integration,
        "_native_json_probe",
        lambda argv: [
            {
                "id": "codemap-py@borda-ai-rig",
                "version": integration.__version__,
                "enabled": True,
                "installPath": str(path),
            }
        ],
    )

    report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)
    provider = report["provider"]["runtimes"]["claude"]["provider"]
    codes = {finding["code"] for finding in report["findings"]}

    assert "provider_same_version_content_drift" not in codes
    if native_path == "same-source":
        assert provider["native_content"]["state"] == "observed"
        assert provider["native_content"]["sha256"] == provider["source_content"]["sha256"]
    else:
        assert provider["native_content"] == {"state": "unknown", "reason": "native_plugin_root_unreadable"}


def test_audit_observes_codex_native_source_path(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex native evidence uses the configured source path without assuming a cache root."""
    source = repo / integration.PROVIDER_DIR
    _write_manifest(source, integration.Runtime.CODEX, integration.PROVIDER_NAME, integration.__version__)
    monkeypatch.setattr(
        integration,
        "_native_json_probe",
        lambda argv: {
            "installed": [
                {
                    "name": integration.PROVIDER_NAME,
                    "version": integration.__version__,
                    "enabled": True,
                    "source": {"type": "local", "path": str(source)},
                }
            ]
        },
    )

    provider = integration.build_audit_report("codex", source)["provider"]["runtimes"]["codex"]["provider"]

    assert provider["native_plugin"] == {
        "state": "observed",
        "name": integration.PROVIDER_NAME,
        "version": integration.__version__,
        "enabled": True,
        "source_path": str(source),
    }
    assert provider["native_content"]["sha256"] == provider["source_content"]["sha256"]


@pytest.mark.parametrize(
    ("runtime", "payload", "consumer"),
    [
        pytest.param(integration.Runtime.CLAUDE, None, integration.PROVIDER_NAME, id="claude-provider"),
        pytest.param(integration.Runtime.CODEX, {"installed": []}, "foundry", id="codex-consumer"),
    ],
)
def test_audit_native_record_schema_is_stable_when_not_observed(
    runtime: integration.Runtime,
    payload: object,
    consumer: str,
) -> None:
    """Unavailable native discovery retains the normalized provider/consumer schema."""
    assert integration._native_plugin_record(runtime, payload, consumer) == {
        "state": "not_observed",
        "name": consumer,
        "version": None,
        "enabled": None,
        "source_path": None,
    }


def test_audit_warns_on_same_version_consumer_content_drift(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A selected consumer with equal versions but unequal observed bytes is a factual warning."""
    provider_source = repo / integration.PROVIDER_DIR
    _write_manifest(provider_source, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, integration.__version__)
    consumer_source = repo / "plugins/cc_foundry"
    (consumer_source / "README.md").write_text("source consumer\n")
    native_consumer = tmp_path / "native-foundry"
    _write_manifest(native_consumer, integration.Runtime.CLAUDE, "foundry", "1.0.0")
    (native_consumer / "README.md").write_text("native consumer\n")
    monkeypatch.setattr(
        integration,
        "_native_json_probe",
        lambda argv: [
            {
                "id": "codemap-py@borda-ai-rig",
                "version": integration.__version__,
                "enabled": True,
                "installPath": str(provider_source),
            },
            {
                "id": "foundry@borda-ai-rig",
                "version": "1.0.0",
                "enabled": True,
                "installPath": str(native_consumer),
            },
        ],
    )

    report = integration.build_audit_report("claude", provider_source)
    foundry = report["consumers"]["claude"]["foundry"]
    finding = next(item for item in report["findings"] if item["code"] == "consumer_same_version_content_drift")

    assert foundry["native_plugin"]["source_path"] == str(native_consumer)
    assert foundry["source_content"]["sha256"] != foundry["native_content"]["sha256"]
    assert finding["severity"] == "medium"
    assert finding["status"] == "warn"
    assert finding["evidence"]["consumer"] == "foundry"


@pytest.mark.parametrize(
    ("native_state", "expected_finding"),
    [
        ("matching", None),
        ("missing", "consumer_query_guidance_missing"),
        ("unreferenced", "consumer_query_guidance_unreachable"),
        ("stale", "consumer_query_guidance_drift"),
        ("source_missing", "consumer_query_guidance_missing"),
        ("source_unreferenced", "consumer_query_guidance_unreachable"),
        ("absent_consumer", None),
    ],
    ids=["matching", "missing", "unreferenced", "stale", "source-missing", "source-unreferenced", "optional-absent"],
)
def test_audit_checks_referenced_consumer_guidance_without_writes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_state: str, expected_finding: str | None
) -> None:
    """Metadata-only integration must not hide missing or stale consumer instructions."""
    source = repo / "plugins/codex-rig"
    native = tmp_path / "installed-consumer"
    _write_manifest(native, integration.Runtime.CODEX, "codex-rig", "1.0.0")
    for plugin in (source, native):
        (plugin / "shared").mkdir(exist_ok=True)
        (plugin / "skills/inspect").mkdir(parents=True)
        _seed(plugin / "shared/codemap-contract.md", "Use the verified launcher and reuse query evidence.\n")
        _seed(plugin / "skills/inspect/SKILL.md", "Follow ../../shared/codemap-contract.md for structural queries.\n")
    if native_state == "missing":
        (native / "shared/codemap-contract.md").unlink()
    elif native_state == "unreferenced":
        _seed(native / "skills/inspect/SKILL.md", "No query guidance loaded.\n")
    elif native_state == "stale":
        _seed(native / "shared/codemap-contract.md", "Outdated query instructions.\n")
    elif native_state == "source_missing":
        (source / "shared/codemap-contract.md").unlink()
    elif native_state == "source_unreferenced":
        _seed(source / "skills/inspect/SKILL.md", "No query guidance loaded.\n")
    installed = (
        []
        if native_state == "absent_consumer"
        else [{"name": "codex-rig", "version": "1.0.0", "enabled": True, "source": {"path": str(native)}}]
    )
    monkeypatch.setattr(integration, "_native_json_probe", lambda argv: {"installed": installed})
    before = _tree_snapshot(repo), _tree_snapshot(native)

    report = integration.build_audit_report("codex", repo / integration.PROVIDER_DIR)

    guidance = report["consumers"]["codex"]["codex-rig"]["query_guidance"]
    expected_source_state = native_state.removeprefix("source_") if native_state.startswith("source_") else "observed"
    assert guidance["source"]["state"] == expected_source_state
    if expected_source_state == "observed":
        assert guidance["source"]["referenced_by"] == ["skills/inspect/SKILL.md"]
    findings = [item for item in report["findings"] if item["code"].startswith("consumer_query_guidance_")]
    assert [item["code"] for item in findings] == ([] if expected_finding is None else [expected_finding])
    if expected_finding:
        assert findings[0]["status"] == "warn"
        assert findings[0]["evidence"]["consumer"] == "codex-rig"
    assert (_tree_snapshot(repo), _tree_snapshot(native)) == before


def test_audit_usage_reports_runtime_aggregates_without_raw_telemetry_payloads(repo: Path) -> None:
    """Audit exposes only per-runtime counts/timing and declares tokens unavailable instead of leaking record
    payloads."""
    log_dir = repo / ".cache" / "codemap" / "logs" / "claude"
    log_dir.mkdir(parents=True)
    _seed(
        log_dir / "activity.jsonl",
        "".join(
            json.dumps(record) + "\n"
            for record in (
                {
                    "ts": "2026-08-18T11:00:00Z",
                    "layer": "cli",
                    "runtime": "claude",
                    "v": integration.__version__,
                    "timing_ms": 7,
                    "argv": ["query", "--secret-argv"],
                    "result": {"target": "raw-result-secret"},
                },
                {
                    "ts": "2026-08-18T11:00:01Z",
                    "layer": "tool",
                    "runtime": "claude",
                    "v": integration.__version__,
                    "tool": "Read",
                    "target": "/private/raw-target.py",
                    "timing_ms": 11,
                },
                {
                    "ts": "2026-08-18T11:00:02Z",
                    "layer": "skill",
                    "event": "start",
                    "runtime": "claude",
                    "v": integration.__version__,
                    "skill": "codemap-py:query-code",
                    "intent": "raw-intent-secret",
                },
            )
        ),
    )

    report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)
    usage = report["usage"]
    rendered_usage = json.dumps(usage, sort_keys=True)

    assert usage["activity_by_runtime"] == {"claude": {"records": 3, "layers": {"cli": 1, "skill": 1, "tool": 1}}}
    assert usage["cli_timing_by_runtime"] == {"claude": {"count": 1, "total_ms": 7, "median_ms": 7, "p95_ms": 7}}
    assert usage["tool_counts_by_runtime"] == {"claude": {"Read": 1}}
    assert usage["skill_counts_by_runtime"] == {"claude": {"codemap-py:query-code": 1}}
    assert usage["token_measurement"] == {
        "status": "unavailable",
        "reason": "host_hook_contract_has_no_token_usage",
    }
    for raw_value in ("--secret-argv", "raw-result-secret", "/private/raw-target.py", "raw-intent-secret"):
        assert raw_value not in rendered_usage


def test_audit_usage_scopes_completion_refresh_and_avoidance_by_runtime_session(repo: Path) -> None:
    """Usage evidence joins nested/top-level completions only within each runtime/session and 600-second window."""
    logs = repo / ".cache" / "codemap" / "logs"
    claude_records = [
        {
            "ts": "2026-08-18T11:00:00Z",
            "layer": "cli",
            "cmd": "index",
            "runtime": "claude",
            "session": "same",
            "v": integration.__version__,
            "result": {"trigger": "query_self_heal"},
        },
        {
            "ts": "2026-08-18T11:00:02Z",
            "layer": "cli",
            "runtime": "claude",
            "session": "same",
            "v": integration.__version__,
            "result": {"module": "pkg.claude", "index": {"query_complete": True}},
        },
        {
            "ts": "2026-08-18T11:00:05Z",
            "layer": "tool",
            "tool": "Read",
            "target": "pkg/claude.py",
            "runtime": "claude",
            "session": "same",
            "v": integration.__version__,
        },
        {
            "ts": "2026-08-18T11:11:00Z",
            "layer": "tool",
            "tool": "Read",
            "target": "pkg/claude.py",
            "runtime": "claude",
            "session": "same",
            "v": integration.__version__,
        },
        {
            "ts": "2026-08-18T11:00:00Z",
            "layer": "cli",
            "cmd": "index",
            "runtime": "claude",
            "session": "refresh-only",
            "v": integration.__version__,
            "result": {"trigger": "claude_prompt_background"},
        },
    ]
    codex_records = [
        {
            "ts": "2026-08-18T11:00:00Z",
            "layer": "cli",
            "cmd": "index",
            "runtime": "codex",
            "session": "same",
            "v": integration.__version__,
            "result": {"trigger": "direct_cli"},
        },
        {
            "ts": "2026-08-18T11:00:03Z",
            "layer": "tool",
            "tool": "Read",
            "target": "pkg/claude.py",
            "runtime": "codex",
            "session": "same",
            "v": integration.__version__,
        },
        {
            "ts": "2026-08-18T11:00:00Z",
            "layer": "cli",
            "runtime": "codex",
            "session": "top-level",
            "v": integration.__version__,
            "result": {"module": "pkg.codex", "query_complete": True},
        },
        {
            "ts": "2026-08-18T11:00:04Z",
            "layer": "tool",
            "tool": "Grep",
            "target": "pkg/codex.py",
            "runtime": "codex",
            "session": "top-level",
            "v": integration.__version__,
        },
    ]
    for runtime, records in (("claude", claude_records), ("codex", codex_records)):
        log_dir = logs / runtime
        log_dir.mkdir(parents=True)
        _seed(log_dir / "usage.jsonl", "".join(json.dumps(record) + "\n" for record in records))
    _seed(
        logs / "legacy.jsonl",
        json.dumps(
            {
                "ts": "2026-08-18T11:00:05Z",
                "layer": "tool",
                "tool": "Read",
                "target": "pkg/claude.py",
                "session": "same",
                "v": "legacy",
            }
        )
        + "\n",
    )

    report = integration.build_audit_report("both", repo / integration.PROVIDER_DIR)
    findings = {finding["code"]: finding for finding in report["findings"]}

    assert {key: report["usage"][key] for key in ("telemetry_records", "cli_records", "skill_start_records")} == {
        "telemetry_records": 9,
        "cli_records": 5,
        "skill_start_records": 0,
    }
    assert findings["refresh_without_query"]["evidence"] == {"refresh_count": 2, "session_count": 2}
    assert findings["avoidance_after_complete_query"]["evidence"] == {
        "event_count": 2,
        "per_runtime": {"claude": 1, "codex": 1},
        "window_seconds": 600,
    }


def test_audit_codex_cli_without_skill_event_is_not_missing_telemetry(repo: Path) -> None:
    """Codex has no Skill hook, so its CLI records alone must not imply broken telemetry."""
    log_dir = repo / ".cache" / "codemap" / "logs" / "codex"
    log_dir.mkdir(parents=True)
    _seed(
        log_dir / "cli.jsonl",
        json.dumps(
            {
                "ts": "2026-08-18T11:00:00Z",
                "layer": "cli",
                "runtime": "codex",
                "session": "thread-1",
                "v": integration.__version__,
            }
        )
        + "\n",
    )

    report = integration.build_audit_report("codex", repo / integration.PROVIDER_DIR)

    assert "skill_telemetry_missing" not in {finding["code"] for finding in report["findings"]}


def test_audit_isolated_current_record_is_not_reported_as_isolation_bypass(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A matching runtime directory/field is not an isolation failure."""
    log_dir = repo / ".cache" / "codemap" / "logs" / "claude"
    log_dir.mkdir(parents=True)
    _seed(
        log_dir / "cli_current.jsonl",
        json.dumps(
            {
                "ts": "2026-08-18T11:00:00Z",
                "layer": "cli",
                "runtime": "claude",
                "v": integration.__version__,
            }
        )
        + "\n",
    )

    code = integration.run(["audit", "--runtime", "claude", "--json"], repo / integration.PROVIDER_DIR)
    assert code in {integration._EXIT_OK, integration._EXIT_RUNTIME}
    payload = json.loads(capsys.readouterr().out)

    assert {finding["code"] for finding in payload["findings"]}.isdisjoint(
        {"runtime_log_isolation_bypassed", "runtime_identity_missing"}
    )


def test_audit_empty_logs_warns_that_runtime_evidence_was_not_observed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No selected-runtime records must be a warning, never a declared-path pass."""
    code = integration.run(["audit", "--runtime", "claude", "--json"], repo / integration.PROVIDER_DIR)
    assert code == integration._EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "warn"
    assert {finding["code"] for finding in payload["findings"]} >= {"runtime_logs_not_observed"}


def test_check_is_rejected_without_a_compatibility_alias(repo: Path) -> None:
    """The removed ``check`` subcommand is a usage error, not an audit forwarding alias."""
    assert integration.run(["check", "--runtime", "claude"], repo / integration.PROVIDER_DIR) == integration._EXIT_USAGE


def test_sentinel_schema_stays_v1_while_body_protocol_is_v2() -> None:
    """The managed sentinel schema stays pinned at v1 while the block body speaks protocol v2.

    The 0.31.0 compat promise is exactly this split: existing v1 sentinels stay authenticable while the body protocol
    advances. A silent bump of either constant would pass every round-trip test yet break installed consumers, so the
    literals are pinned side by side here.
    """
    assert integration.BLOCK_SCHEMA_VERSION == 1
    assert integration.PROTOCOL_VERSION == "codemap-py.integration.v2"
    assert integration._render_managed_block("x\n").startswith("<!-- codemap-py:integration:begin v1 sha256=")


@pytest.mark.parametrize(("since", "expected_records"), [("2026-08-18", 1), ("2026-08-19", 0)])
def test_audit_since_bounds_runtime_evidence(
    repo: Path, capsys: pytest.CaptureFixture[str], since: str, expected_records: int
) -> None:
    """Include records on its date and excludes earlier telemetry."""
    log_dir = repo / ".cache" / "codemap" / "logs" / "claude"
    log_dir.mkdir(parents=True)
    _seed(
        log_dir / "cli.jsonl",
        json.dumps({"ts": "2026-08-18T00:00:00Z", "runtime": "claude", "v": integration.__version__}) + "\n",
    )

    assert (
        integration.run(["audit", "--runtime", "claude", "--since", since, "--json"], repo / integration.PROVIDER_DIR)
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["window"]["since"] == since
    assert payload["runtime_logs"]["selected"]["claude"]["records"] == expected_records


def test_audit_invalid_since_is_usage_error(repo: Path) -> None:
    """An invalid date never falls through as an unbounded audit request."""
    assert (
        integration.run(["audit", "--since", "2026-13-40"], repo / integration.PROVIDER_DIR) == integration._EXIT_USAGE
    )


def test_audit_skips_malformed_jsonl_without_claiming_a_record(repo: Path) -> None:
    """A malformed telemetry line is bounded input noise, not a false observation."""
    log_dir = repo / ".cache" / "codemap" / "logs" / "claude"
    log_dir.mkdir(parents=True)
    _seed(log_dir / "cli.jsonl", "not-json\n")

    report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)
    assert report["runtime_logs"]["selected"]["claude"]["records"] == 0
    assert {finding["code"] for finding in report["findings"]} >= {"runtime_logs_not_observed"}


def test_audit_reports_invalid_managed_block_without_mutating_it(repo: Path) -> None:
    """A tampered managed block is a failure and audit leaves its bytes unchanged."""
    target = next(item for item in integration.CLAUDE_TARGETS if item.consumer == "oss")
    path = repo / target.plugin_dir / integration.CONSUMER_MANAGED_FILE[target.consumer]
    path.parent.mkdir(parents=True)
    _seed(
        path,
        "<!-- codemap-py:integration:begin v1 sha256="
        + "0" * 64
        + " -->\ntampered\n<!-- codemap-py:integration:end -->\n",
    )
    before = path.read_bytes()

    report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)

    assert {finding["code"] for finding in report["findings"]} >= {"managed_block_invalid"}
    assert path.read_bytes() == before


def test_audit_reports_provider_and_consumer_drift_only_when_source_provider_is_trusted(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed-version comparisons activate after the source provider matches this runtime."""
    _write_manifest(
        repo / integration.PROVIDER_DIR, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, integration.__version__
    )
    monkeypatch.setattr(
        integration,
        "_native_json_probe",
        lambda argv: [
            {"id": "codemap-py@borda-ai-rig", "version": "older", "enabled": True},
            {"id": "oss@borda-ai-rig", "version": "other", "enabled": True},
        ],
    )

    report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)
    codes = {finding["code"] for finding in report["findings"]}

    assert {"provider_version_drift", "consumer_version_drift"} <= codes


def test_audit_never_calls_query_or_mutation_paths(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit reads evidence only; a query, plan mutation, or native write is a test failure."""
    before = _tree_snapshot(repo)

    def _forbidden(*args: object, **kwargs: object) -> None:
        """Fail if the audit unexpectedly reaches a query or mutation path."""
        raise AssertionError("audit invoked a forbidden mutation/query path")

    monkeypatch.setattr(integration.query, "main", _forbidden)
    monkeypatch.setattr(integration, "apply_plan", _forbidden)
    monkeypatch.setattr(integration, "sync_plan", _forbidden)
    monkeypatch.setattr(integration, "_run_native_required", _forbidden)
    monkeypatch.setattr(integration, "_native_json_probe", lambda argv: None)

    report = integration.build_audit_report("both", repo / integration.PROVIDER_DIR)

    assert report["status"] == "warn"
    assert _tree_snapshot(repo) == before


def test_audit_runtime_directory_record_mismatch_is_a_failure(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A record claiming Codex inside the Claude directory is a stable identity failure."""
    log_dir = repo / ".cache" / "codemap" / "logs" / "claude"
    log_dir.mkdir(parents=True)
    _seed(
        log_dir / "cli_current.jsonl",
        json.dumps(
            {
                "ts": "2026-08-18T11:00:00Z",
                "layer": "cli",
                "runtime": "codex",
                "v": integration.__version__,
            }
        )
        + "\n",
    )

    code = integration.run(["audit", "--runtime", "claude", "--json"], repo / integration.PROVIDER_DIR)
    assert code == integration._EXIT_RUNTIME
    payload = json.loads(capsys.readouterr().out)
    assert {finding["code"] for finding in payload["findings"]} >= {"runtime_identity_missing"}


def test_audit_is_zero_write(repo: Path) -> None:
    """Keep audit report generation from mutating the fixture tree."""
    before = _tree_snapshot(repo)
    report = integration.build_audit_report("both", repo / integration.PROVIDER_DIR)
    assert report["protocol"] == integration.PROTOCOL_VERSION
    assert report["status"] == "warn"
    assert _tree_snapshot(repo) == before


def test_audit_cli_json_exits_zero(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Emit a parseable v2 warning report for empty evidence."""
    code = integration.run(["audit", "--runtime", "both", "--json"], repo / integration.PROVIDER_DIR)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["protocol"] == integration.PROTOCOL_VERSION
    assert payload["schema_version"] == 2
    assert payload["status"] == "warn"


@pytest.mark.skipif(not _SOURCE_CHECKOUT, reason="not running from a source checkout")
def test_every_managed_file_target_exists_in_this_source_tree() -> None:
    """Every ``CONSUMER_MANAGED_FILE`` target is a real file in this checkout.

    The map names the host file each consumer's managed block is inserted into. A target that does not exist reads as a
    first-time insert against a path nothing ships, so the defect stays invisible until an apply run creates a stray
    file — which is exactly how ``foundry`` came to point at an absent ``skills/_shared/codemap-context.md``. Every
    other test here builds a disposable fixture tree, so none of them can catch it: this one deliberately asserts
    against the real source checkout.
    """
    repo_root = Path(__file__).resolve().parents[4]
    missing = [
        f"{target.plugin_dir}/{integration.CONSUMER_MANAGED_FILE[target.consumer]}"
        for target in integration.ALL_TARGETS
        if not (repo_root / target.plugin_dir / integration.CONSUMER_MANAGED_FILE[target.consumer]).is_file()
    ]
    assert missing == [], f"CONSUMER_MANAGED_FILE targets absent from the source tree: {missing}"


def test_audit_reports_absent_consumer_as_named_state_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed-set consumer with no manifest on disk is reported ``absent``, not raised as an error."""
    root = tmp_path / "no-oss"
    root.mkdir()
    for target in integration.ALL_TARGETS:
        if target.consumer != "oss":
            _write_manifest(root / target.plugin_dir, target.runtime, target.consumer, "1.0.0")
    _write_manifest(root / integration.PROVIDER_DIR, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, "9.9.9")
    monkeypatch.chdir(root)
    monkeypatch.setattr(integration, "_native_json_probe", lambda argv: None)
    report = integration.build_audit_report("claude", root / integration.PROVIDER_DIR)
    oss_status = report["consumers"]["claude"]["oss"]
    assert {
        key: oss_status[key] for key in ("manifest_present", "name_matches", "source_version", "installed_version")
    } == {
        "manifest_present": False,
        "name_matches": False,
        "source_version": None,
        "installed_version": None,
    }
    assert oss_status["managed_block"]["status"] == "absent"


def test_runtime_and_source_members_preserve_plan_json_values(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enum-backed integration plans preserve the CLI's original JSON strings."""
    monkeypatch.setattr(integration, "_native_json_probe", lambda argv: None)
    plan = integration.build_plan(
        integration.Runtime.CLAUDE,
        ["oss"],
        integration.Source.LOCAL_CANDIDATE,
        repo / integration.PROVIDER_DIR,
    )
    serialized = json.loads(json.dumps(plan))

    assert integration.Runtime.__bases__ == (str, Enum)
    assert integration.Source.__bases__ == (str, Enum)
    assert serialized["runtime"] == "claude"
    assert serialized["source"] == "local-candidate"
    assert serialized["ops"][0]["runtime"] == "claude"
    assert serialized["ops"][1]["desired"]["ref"] == "local-candidate"


# --------------------------------------------------------------------------------------
# plan — zero-mutation report artifact, stable SHA-256.
# --------------------------------------------------------------------------------------


def test_plan_writes_only_its_out_artifact(repo: Path, tmp_path: Path) -> None:
    """Write nothing under the fixture tree besides the named artifact."""
    before = _tree_snapshot(repo)
    out = tmp_path / "plan.json"
    code = integration.run(["plan", "--runtime", "claude", "--out", str(out)], repo / integration.PROVIDER_DIR)
    assert code == 0
    assert out.is_file()
    assert _tree_snapshot(repo) == before


def test_plan_default_out_confined_to_reports_dir(repo: Path) -> None:
    """Without ``--out``, the artifact lands only under the fixture's own ``.reports/integrate/``."""
    before = _tree_snapshot(repo)
    code = integration.run(["plan", "--runtime", "claude", "--consumers", "oss"], repo / integration.PROVIDER_DIR)
    assert code == 0
    after = _tree_snapshot(repo)
    changed = {k for k in after if after.get(k) != before.get(k)}
    assert changed
    assert all(k.startswith(".reports/integrate/") for k in changed)


def test_plan_sha256_is_stable_and_self_consistent(repo: Path) -> None:
    """The recorded ``plan_sha256`` equals the digest recomputed over the plan's own body."""
    plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
    assert integration.compute_plan_sha256(plan) == plan["plan_sha256"]


def test_plan_unknown_consumer_exits_usage(repo: Path) -> None:
    """An unrecognized ``--consumers`` name is a ``2``-class syntax error, never a lookup."""
    code = integration.run(
        ["plan", "--runtime", "claude", "--consumers", "not-a-target"], repo / integration.PROVIDER_DIR
    )
    assert code == integration._EXIT_USAGE


# --------------------------------------------------------------------------------------
# Approval digest.
# --------------------------------------------------------------------------------------


def test_approve_malformed_rejected(repo: Path) -> None:
    """A non-hex/wrong-length ``--approve`` value is ``approve_malformed``."""
    plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
    with pytest.raises(integration.ApprovalError) as exc:
        integration.verify_approval(plan, "not-a-sha256")
    assert exc.value.code == "approve_malformed"


def test_approve_mismatch_rejected(repo: Path) -> None:
    """A well-formed but wrong SHA-256 is ``approve_mismatch``."""
    plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
    with pytest.raises(integration.ApprovalError) as exc:
        integration.verify_approval(plan, "0" * 64)
    assert exc.value.code == "approve_mismatch"


def test_approve_correct_sha_proceeds(repo: Path) -> None:
    """The plan's own recorded SHA-256 verifies without raising."""
    plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
    integration.verify_approval(plan, plan["plan_sha256"])  # no raise


def test_apply_cli_bad_approve_exits_usage(repo: Path, tmp_path: Path) -> None:
    """Map invalid approval input to the documented command-line exit code."""
    plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
    plan_path = tmp_path / "plan.json"
    _seed(plan_path, json.dumps(plan))
    code = integration.run(["apply", "--plan", str(plan_path), "--approve", "nope"], repo / integration.PROVIDER_DIR)
    assert code == integration._EXIT_USAGE


# --------------------------------------------------------------------------------------
# apply — refusal matrix (each: exit-mapped RefusalError, target file left untouched).
# --------------------------------------------------------------------------------------


def _single_op_plan(repo: Path, consumer: str = "oss") -> dict:
    """Build the smallest valid apply plan for one consumer target."""
    return integration.build_plan("claude", [consumer], None, repo / integration.PROVIDER_DIR)


def _assert_refused(repo: Path, plan: dict, code: str, original_bytes: bytes | None) -> None:
    """Assert that an apply plan is refused without changing target bytes."""
    target_path = repo / plan["ops"][0]["path"]
    with pytest.raises(integration.RefusalError) as exc:
        integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)
    assert exc.value.code == code
    after = target_path.read_bytes() if target_path.is_file() else None
    assert after == original_bytes


def test_apply_refuses_installed_cache_root(repo: Path) -> None:
    """A target resolving under any ``plugins/cache/...`` tree is refused, never written."""
    plan = _single_op_plan(repo)
    plan["ops"][0]["path"] = "plugins/cache/oss/skills/_shared/codemap-context.md"
    plan["plan_sha256"] = integration.compute_plan_sha256(plan)
    _assert_refused(repo, plan, "installed_cache_root", None)


def test_apply_refuses_path_escape(repo: Path) -> None:
    """A target outside its consumer's own plugin directory is refused."""
    plan = _single_op_plan(repo)
    plan["ops"][0]["path"] = "plugins/some-other-dir/escape.md"
    plan["plan_sha256"] = integration.compute_plan_sha256(plan)
    _assert_refused(repo, plan, "path_escape", None)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevated privileges on Windows")
def test_apply_refuses_symlink_target(repo: Path) -> None:
    """A target path traversing a symlink is refused, even though the plan itself is unmodified.

    The symlink points at a sibling file *inside* the same consumer plugin dir, so the resolved path still passes the
    path-containment check — isolating ``symlink_target`` from ``path_escape`` (both fire on an out-of-tree symlink
    target; only this shape proves the symlink check specifically).
    """
    plan = _single_op_plan(repo)
    target_path = repo / plan["ops"][0]["path"]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    sibling = target_path.parent / "sibling.md"
    _seed(sibling, "not a managed block\n")
    os.symlink(sibling, target_path)
    _assert_refused(repo, plan, "symlink_target", sibling.read_bytes())


def test_apply_refuses_dirty_overlap(repo: Path) -> None:
    """Uncommitted local changes on the target file refuse the overlay."""
    plan = _single_op_plan(repo)
    target_path = repo / plan["ops"][0]["path"]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _seed(target_path, "tracked content\n")
    _git_commit_all(repo)
    original = target_path.read_bytes()
    _seed(target_path, "tracked content\nuncommitted local edit\n")
    dirtied = target_path.read_bytes()
    # before_hash in the plan was computed pre-git-commit against the same bytes; the refusal
    # fires on the *uncommitted* overlay, independent of before_hash matching or not.
    _assert_refused(repo, plan, "dirty_overlap", dirtied)
    assert dirtied != original


def test_apply_refuses_unverified_product_identity(repo: Path) -> None:
    """A consumer manifest whose ``name`` no longer matches the plan's target is refused."""
    plan = _single_op_plan(repo)
    manifest_path = repo / "plugins" / "cc_oss" / ".claude-plugin" / "plugin.json"
    _seed(manifest_path, json.dumps({"name": "tampered-name", "version": "1.0.0"}))
    _assert_refused(repo, plan, "unverified_product_identity", None)


def test_apply_refuses_foreign_or_modified_marker(repo: Path) -> None:
    """A managed block whose embedded sha256 does not match its own body is a foreign/tampered marker."""
    target = integration.CLAUDE_TARGETS[0]
    rel_path = f"{target.plugin_dir}/{integration.CONSUMER_MANAGED_FILE[target.consumer]}"
    target_path = repo / rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    real_block = integration._render_managed_block("some body\n")
    stamp_index = real_block.index("sha256=") + len("sha256=")
    flipped_digit = "0" if real_block[stamp_index] != "0" else "1"
    tampered = real_block[:stamp_index] + flipped_digit + real_block[stamp_index + 1 :]
    _seed(target_path, tampered)
    original = target_path.read_bytes()

    plan = integration.build_plan("claude", [target.consumer], None, repo / integration.PROVIDER_DIR)
    _assert_refused(repo, plan, "foreign_or_modified_marker", original)


def test_apply_refuses_drift_on_out_of_band_edit(repo: Path) -> None:
    """A target edited out of band between ``plan`` and ``apply`` invalidates the approval."""
    target = integration.CLAUDE_TARGETS[0]
    rel_path = f"{target.plugin_dir}/{integration.CONSUMER_MANAGED_FILE[target.consumer]}"
    target_path = repo / rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _seed(target_path, "original prose\n")

    plan = integration.build_plan("claude", [target.consumer], None, repo / integration.PROVIDER_DIR)
    _seed(target_path, "original prose\nedited after the plan was made\n")
    dirtied = target_path.read_bytes()
    _assert_refused(repo, plan, "drift", dirtied)


def test_apply_refuses_drift_when_block_unexpectedly_appears(repo: Path) -> None:
    """A managed block appearing where the plan expected a first-time insert is also drift."""
    target = integration.CLAUDE_TARGETS[0]
    plan = integration.build_plan("claude", [target.consumer], None, repo / integration.PROVIDER_DIR)
    assert plan["ops"][0]["first_time"] is True

    target_path = repo / plan["ops"][0]["path"]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _seed(target_path, integration._render_managed_block("unexpected pre-existing block\n"))
    dirtied = target_path.read_bytes()
    _assert_refused(repo, plan, "drift", dirtied)


# --------------------------------------------------------------------------------------
# apply — in-file mutation semantics.
# --------------------------------------------------------------------------------------


def test_apply_first_time_insert_preserves_existing_content(repo: Path) -> None:
    """A first-time apply appends the managed block; real pre-existing prose survives byte-for-byte."""
    target = integration.CLAUDE_TARGETS[0]
    rel_path = f"{target.plugin_dir}/{integration.CONSUMER_MANAGED_FILE[target.consumer]}"
    target_path = repo / rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    original = "# Codemap context\n\nHuman-authored notes that must survive.\n"
    _seed(target_path, original)

    plan = integration.build_plan("claude", [target.consumer], None, repo / integration.PROVIDER_DIR)
    integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)

    mutated = target_path.read_text()
    assert mutated.startswith(original)
    assert "codemap-py:integration:begin" in mutated


def test_apply_writes_lf_only_managed_block(repo: Path) -> None:
    """A first-time apply's managed block is LF-only on disk, regardless of host OS."""
    target = integration.CLAUDE_TARGETS[0]
    plan = integration.build_plan("claude", [target.consumer], None, repo / integration.PROVIDER_DIR)
    integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)

    target_path = repo / plan["ops"][0]["path"]
    data = target_path.read_bytes()
    assert b"\r\n" not in data
    assert b"codemap-py:integration:begin" in data


def test_apply_reapply_is_idempotent_zero_byte_noop(repo: Path) -> None:
    """Re-running the same approved plan against an already-wired file is a zero-byte no-op, exit 0."""
    plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
    result1 = integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)
    assert result1["state"] == "complete"
    target_path = repo / plan["ops"][0]["path"]
    bytes_after_first = target_path.read_bytes()

    result2 = integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)
    assert result2["state"] == "complete"
    assert target_path.read_bytes() == bytes_after_first


# --------------------------------------------------------------------------------------
# Journal transitions.
# --------------------------------------------------------------------------------------


def test_journal_records_full_success_sequence(repo: Path, tmp_path: Path) -> None:
    """A clean single-target apply journals ``approved -> applying -> verified -> complete``."""
    plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
    journal_dir = tmp_path / "journal"
    result = integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR, journal_dir=journal_dir)
    assert result["state"] == "complete"
    states = [json.loads(line)["state"] for line in (journal_dir / "journal.jsonl").read_text().splitlines()]
    assert states == ["approved", "applying", "verified", "complete"]


# --------------------------------------------------------------------------------------
# Rollback — partial multi-target failure, both target orders (Phase-4 exit requirement).
# --------------------------------------------------------------------------------------


def _tamper_identity(root: Path, target: integration.ConsumerTarget) -> None:
    """Replace a managed target marker with content that fails identity checks."""
    dirname = ".claude-plugin" if target.runtime == integration.Runtime.CLAUDE else ".codex-plugin"
    manifest_path = root / target.plugin_dir / dirname / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["name"] = "tampered-name"
    _seed(manifest_path, json.dumps(manifest))


def _seed_prose(root: Path, consumer: str, text: str) -> Path:
    """Create a consumer README containing caller-provided unmanaged prose."""
    target = next(t for t in integration.CLAUDE_TARGETS if t.consumer == consumer)
    rel_path = f"{target.plugin_dir}/{integration.CONSUMER_MANAGED_FILE[consumer]}"
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _seed(path, text)
    return path


@pytest.mark.parametrize(
    ("first_consumer", "second_consumer"),
    [pytest.param("oss", "develop", id="oss-then-develop"), pytest.param("develop", "oss", id="develop-then-oss")],
)
def test_rollback_restores_first_target_both_orders(
    repo: Path, tmp_path: Path, first_consumer: str, second_consumer: str
) -> None:
    """First target verified, second fails -> rollback restores the first target's full original file."""
    first_path = _seed_prose(repo, first_consumer, f"{first_consumer} original notes\n")
    _seed_prose(repo, second_consumer, f"{second_consumer} original notes\n")
    original_first_bytes = first_path.read_bytes()

    plan = integration.build_plan("claude", [first_consumer, second_consumer], None, repo / integration.PROVIDER_DIR)
    second_target = next(t for t in integration.CLAUDE_TARGETS if t.consumer == second_consumer)
    _tamper_identity(repo, second_target)

    journal_dir = tmp_path / "journal"
    with pytest.raises(integration.IntegrationError) as exc:
        integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR, journal_dir=journal_dir)

    assert exc.value.detail["state"] == "rollback-succeeded"
    assert exc.value.detail["applied"] == [0]
    assert first_path.read_bytes() == original_first_bytes


def test_rollback_failure_reports_recovery_required(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When rollback itself cannot restore the first target, the engine reports ``recovery-required``.

    Rollback failure is forced deterministically by stubbing ``Journal.save_before_image`` to a no-op — the first target
    then applies successfully (its own write is untouched) but leaves no before-image to restore from, so the hash check
    after rollback finds that the deleted file does not match the plan's recorded ``before_hash`` and reports
    ``rollback-failed``.
    """
    first_path = _seed_prose(repo, "oss", "oss original notes\n")
    _seed_prose(repo, "develop", "develop original notes\n")

    plan = integration.build_plan("claude", ["oss", "develop"], None, repo / integration.PROVIDER_DIR)
    develop_target = next(t for t in integration.CLAUDE_TARGETS if t.consumer == "develop")
    _tamper_identity(repo, develop_target)
    monkeypatch.setattr(integration.Journal, "save_before_image", lambda self, index, data: None)

    journal_dir = tmp_path / "journal"
    with pytest.raises(integration.IntegrationError) as exc:
        integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR, journal_dir=journal_dir)
    assert exc.value.code == "recovery_required"
    assert exc.value.detail["state"] == "rollback-failed"
    assert exc.value.detail["recovery_commands"]
    assert not first_path.is_file()  # rollback fell back to unlink; no before-image existed to restore


# --------------------------------------------------------------------------------------
# sync — drift refusal before any native command runs (CI-safe: no real claude/codex CLI).
# --------------------------------------------------------------------------------------


def test_sync_refuses_drift_before_native_call(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revalidate installed state immediately before every plugin op; drift stops it early."""
    monkeypatch.setattr(integration, "_native_json_probe", lambda argv: None)
    plan = integration.build_plan("claude", ["oss"], "local-candidate", repo / integration.PROVIDER_DIR)

    fake_installed = [{"id": "codemap-py@borda-ai-rig", "version": "9.9.9", "enabled": True}]
    monkeypatch.setattr(integration, "_native_json_probe", lambda argv: fake_installed)
    calls: list[list[str]] = []
    monkeypatch.setattr(integration, "_run_native_required", lambda argv: calls.append(list(argv)))

    with pytest.raises(integration.IntegrationError) as exc:
        integration.sync_plan(plan, plan["plan_sha256"], "local-candidate", repo / integration.PROVIDER_DIR)
    assert exc.value.code == "drift"
    assert len(calls) == 1  # only the marketplace op (no before-state to drift-check) ran


def test_sync_approve_source_mismatch_is_approval_error(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require synchronization approval to match the source recorded in the plan."""
    monkeypatch.setattr(integration, "_native_json_probe", lambda argv: None)
    plan = integration.build_plan("claude", ["oss"], "local-candidate", repo / integration.PROVIDER_DIR)
    with pytest.raises(integration.ApprovalError) as exc:
        integration.sync_plan(plan, plan["plan_sha256"], "release", repo / integration.PROVIDER_DIR)
    assert exc.value.code == "source_mismatch"


def test_apply_and_sync_ignore_each_others_operation_kind(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mixed plan keeps source edits in apply and native runtime work in sync."""
    monkeypatch.setattr(integration, "_native_json_probe", lambda argv: None)
    plan = integration.build_plan("claude", ["oss"], "local-candidate", repo / integration.PROVIDER_DIR)
    source_op = next(op for op in plan["ops"] if op["kind"] == "source_write")
    marketplace_op = next(op for op in plan["ops"] if op["kind"] == "runtime_sync" and op["role"] == "marketplace")
    plan["ops"] = [source_op, marketplace_op]
    plan["plan_sha256"] = integration.compute_plan_sha256(plan)
    native_calls: list[list[str]] = []
    monkeypatch.setattr(integration, "_run_native_required", lambda argv: native_calls.append(list(argv)))

    integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)
    source_path = repo / source_op["path"]
    assert "codemap-py:integration:begin" in source_path.read_text()
    assert native_calls == []

    source_bytes_after_apply = source_path.read_bytes()
    integration.sync_plan(plan, plan["plan_sha256"], "local-candidate", repo / integration.PROVIDER_DIR)
    assert native_calls == marketplace_op["argv"]
    assert source_path.read_bytes() == source_bytes_after_apply


# --------------------------------------------------------------------------------------
# win_quoting — Windows batch-quoting guard (pure logic; runs on every OS via windows=True).
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "unsafe"),
    [
        pytest.param(["install", "oss@borda-ai-rig"], False, id="clean-argv"),
        pytest.param(["install", "oss name with spaces"], True, id="space-in-arg"),
        pytest.param(["install", "oss&whoami"], True, id="ampersand-injection"),
        pytest.param(["install", 'oss"quoted"'], True, id="quote-in-arg"),
        pytest.param(["install", ""], True, id="empty-arg"),
    ],
)
def test_win_quoting_guard_flags_unsafe_argv(arguments: list[str], unsafe: bool) -> None:
    """Flag spaces/shell-metacharacters unsafe for a ``.bat``/``.cmd`` launcher."""
    assert integration._unsafe_windows_batch_argv("claude.cmd", arguments) is unsafe


def test_win_quoting_resolve_rejects_unsafe_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Windows batch launcher refuses to resolve argv containing shell metacharacters."""
    fake_cmd = tmp_path / "claude.cmd"
    _seed(fake_cmd, "@echo off\n")
    monkeypatch.setattr(integration.shutil, "which", lambda name: str(fake_cmd))
    with pytest.raises(integration.IntegrationError) as exc:
        integration._resolve_native_command(["claude", "plugin", "install", "oss & whoami"], windows=True)
    assert exc.value.code == "unsafe_windows_argv"


def test_win_quoting_resolve_builds_quoted_line_for_safe_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A safe argv on a Windows batch launcher resolves to one quoted shell command line."""
    fake_cmd = tmp_path / "claude.cmd"
    _seed(fake_cmd, "@echo off\n")
    monkeypatch.setattr(integration.shutil, "which", lambda name: str(fake_cmd))
    resolved, shell = integration._resolve_native_command(
        ["claude", "plugin", "install", "oss@borda-ai-rig"], windows=True
    )
    assert shell is True
    assert resolved == f'"{fake_cmd}" plugin install oss@borda-ai-rig'


# --------------------------------------------------------------------------------------
# demo — disposable evidence only.
# --------------------------------------------------------------------------------------


def test_demo_returns_evidence_confined_to_its_own_report(repo: Path) -> None:
    """Describe structural smoke evidence without implying a paired or token experiment."""
    before = _tree_snapshot(repo)
    demo = integration.run_demo("claude", repo / integration.PROVIDER_DIR)
    assert demo["protocol"] == integration.PROTOCOL_VERSION
    assert "audit" in demo
    assert "query_evidence" in demo
    assert demo["scope"] == "structural_smoke"
    assert demo["comparison"] == "not_performed"
    assert demo["token_measurement"]["status"] == "unavailable"
    assert Path(demo["report_path"]).is_file()

    after = _tree_snapshot(repo)
    changed = {k for k in after if after.get(k) != before.get(k)}
    assert all(k.startswith(".reports/integrate/") for k in changed)


def test_demo_cli_exits_zero_with_no_index_built(repo: Path) -> None:
    """Exit 0 when there is simply no index yet (not a failure)."""
    code = integration.run(["demo", "--runtime", "claude"], repo / integration.PROVIDER_DIR)
    assert code == 0
