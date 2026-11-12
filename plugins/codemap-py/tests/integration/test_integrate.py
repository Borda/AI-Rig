"""Codemap-py integrate engine — audit/plan/apply/sync/demo.

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
The native-CLI boundary (``_native_json_probe``/``_run_native_required``) is stubbed by the ``repo`` fixture, so no
test depends on an installed ``claude``/``codex`` CLI; one test keeps the real probe to prove that a missing CLI is a
quiet ``None``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from enum import Enum
from pathlib import Path

import pytest
from _host_skips import skip_file_symlink_unavailable
from _scan_helpers import tree_snapshot
from codemap_py import integration

_PATH_CLASSES = {"normal": "repo", "spaces_nonascii": "a repo café"}
_SOURCE_CHECKOUT = (Path(__file__).resolve().parents[4] / integration.PROVIDER_DIR).is_dir()
_ABSENT_FIELDS = {"manifest_present": False, "name_matches": False, "source_version": None, "installed_version": None}
_GUIDANCE_PATH = "skills/inspect/SKILL.md"


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


def _write_provider_manifest(repo: Path) -> None:
    """Make the fixture's source provider claim the running package version, as an in-sync checkout does."""
    _write_manifest(
        repo / integration.PROVIDER_DIR, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, integration.__version__
    )


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


def _record(ts: str = "2026-08-18T11:00:00Z", **fields: object) -> dict[str, object]:
    """Return one telemetry record stamped with the running package version unless *fields* override ``v``."""
    return {"ts": ts, "v": integration.__version__, **fields}


def _write_log(repo: Path, relative: str, *records: dict[str, object]) -> None:
    """Write *records* as JSON lines to ``<repo>/.cache/codemap/logs/<relative>``."""
    path = repo / ".cache" / "codemap" / "logs" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    _seed(path, "".join(json.dumps(record) + "\n" for record in records))


def _run_json(repo: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    """Run the integrate CLI from the fixture provider directory and return ``(exit_code, json_payload)``."""
    code = integration.run(list(argv), repo / integration.PROVIDER_DIR)
    return code, json.loads(capsys.readouterr().out)


def _finding_codes(report: dict) -> set[str]:
    """Return the set of finding codes in an audit report or payload."""
    return {finding["code"] for finding in report["findings"]}


def _native_provider_entry(install_path: Path) -> dict[str, object]:
    """Return a Claude ``plugin list`` entry for the Codemap provider installed at *install_path*."""
    return {
        "id": "codemap-py@borda-ai-rig",
        "version": integration.__version__,
        "enabled": True,
        "installPath": str(install_path),
    }


@pytest.fixture(name="path_class", params=["normal", "spaces_nonascii"])
def _path_class(request: pytest.FixtureRequest) -> str:
    """Return the requested normal or space-and-non-ASCII fixture path class."""
    return request.param


@pytest.fixture(name="repo")
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_class: str) -> Path:
    """Provide a disposable repository as the working directory with native CLI discovery reporting nothing.

    The native probe would otherwise spawn the host's ``claude``/``codex`` CLI, making results depend on what happens to
    be installed; tests that need installed plugins call ``native_probe``.
    """
    root = _build_repo(tmp_path, path_class=path_class)
    monkeypatch.chdir(root)
    monkeypatch.setattr(integration.native, "_native_json_probe", lambda argv: None)
    real_git_dirty = integration.apply_sync._git_dirty
    monkeypatch.setattr(
        integration.apply_sync,
        "_git_dirty",
        lambda root, rel: real_git_dirty(root, rel) if (root / ".git").is_dir() else False,
    )
    return root


@pytest.fixture
def native_probe(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], None]:
    """Return a setter that makes every native JSON probe answer with the given payload."""

    def install(payload: object) -> None:
        monkeypatch.setattr(integration.native, "_native_json_probe", lambda argv: payload)

    return install


class TestNativeProbe:
    """The real native-CLI probe, kept unstubbed."""

    def test_missing_cli_probes_as_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Probing a native CLI that is not on ``PATH`` returns ``None`` instead of raising.

        Absence of ``claude``/``codex`` is a normal state for audits; the real probe, run with an empty ``PATH``, must
        treat it as "not observed" so a missing CLI never fails an audit.
        """
        monkeypatch.setenv("PATH", str(tmp_path))

        assert integration.native._native_json_probe(["claude", "plugin", "list", "--json"]) is None


class TestAuditRuntimeLogs:
    """Audit findings and usage evidence derived from the runtime telemetry logs."""

    def test_fails_current_flat_logs(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Current flat telemetry fails the audit instead of inheriting declared-path health.

        A current-version record sitting directly in the log root has no runtime identity and bypasses per-runtime
        isolation, so the audit must exit with the runtime-failure code and name both findings.
        """
        _write_log(repo, "cli_current.jsonl", _record(layer="cli"))

        code, payload = _run_json(repo, capsys, "audit", "--runtime", "claude", "--json")

        assert code == integration._EXIT_RUNTIME
        assert payload["schema_version"] == 2
        assert payload["protocol"] == "codemap-py.integration.v2"
        assert payload["status"] == "fail"
        assert _finding_codes(payload) >= {"runtime_identity_missing", "runtime_log_isolation_bypassed"}

    def test_reports_split_index_roots_when_runtimes_disagree(self, repo: Path) -> None:
        """Nested Claude and top-level Codex telemetry naming different index paths raise ``split_index_roots``.

        Each runtime's last-seen index path is read from its own log shape; when the selected runtimes disagree the
        finding must carry both observed paths as evidence.
        """
        _write_log(
            repo,
            "claude/nested/cli.jsonl",
            _record(layer="cli", runtime="claude", result={"index": {"index_path": "/indexes/claude.json"}}),
        )
        _write_log(
            repo,
            "codex/cli.jsonl",
            _record("2026-08-18T11:00:01Z", layer="cli", runtime="codex", result={"index_path": "/indexes/codex.json"}),
        )

        report = integration.build_audit_report("both", repo / integration.PROVIDER_DIR)

        split = next(finding for finding in report["findings"] if finding["code"] == "split_index_roots")
        assert split["evidence"]["observed_runtime_paths"] == {
            "claude": ["/indexes/claude.json"],
            "codex": ["/indexes/codex.json"],
        }

    def test_no_split_when_runtimes_share_index(self, repo: Path) -> None:
        """Both runtimes naming the same index path is not a split.

        Divergence, not mere presence of two runtimes, is the failure shape.
        """
        _write_log(
            repo,
            "claude/nested/cli.jsonl",
            _record(layer="cli", runtime="claude", result={"index": {"index_path": "/indexes/shared.json"}}),
        )
        _write_log(
            repo,
            "codex/cli.jsonl",
            _record(
                "2026-08-18T11:00:01Z", layer="cli", runtime="codex", result={"index_path": "/indexes/shared.json"}
            ),
        )

        report = integration.build_audit_report("both", repo / integration.PROVIDER_DIR)

        assert "split_index_roots" not in _finding_codes(report)

    def test_no_split_for_single_selected_runtime(self, repo: Path) -> None:
        """A single selected runtime can never be split, whatever other runtimes hold.

        Only the Claude log exists and only Claude is selected, so there is nothing to disagree with.
        """
        _write_log(
            repo,
            "claude/nested/cli.jsonl",
            _record(layer="cli", runtime="claude", result={"index": {"index_path": "/indexes/claude.json"}}),
        )

        report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)

        assert "split_index_roots" not in _finding_codes(report)

    def test_direct_records_reported_separately_and_not_observed_as_claude(self, repo: Path) -> None:
        """Per-invocation direct telemetry stays visible without satisfying the selected runtime's evidence.

        A record in the ``direct`` log directory is reported under its own heading; Claude is still "not observed", so a
        direct CLI run never masks a missing Claude session.
        """
        _write_log(
            repo,
            "direct/cli.jsonl",
            _record(layer="cli", runtime="direct", result={"index_path": "/indexes/direct.json"}),
        )

        report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)

        assert report["runtime_logs"]["direct"] == {
            "files": 1,
            "records": 1,
            "current_records": 1,
            "state": "observed",
            "truncated": False,
        }
        assert report["runtime_logs"]["selected"]["claude"]["state"] == "not_observed"
        assert report["shared_index"]["observed_runtime_paths"] == {"claude": [], "direct": ["/indexes/direct.json"]}
        assert report["usage"]["telemetry_records"] == 0
        assert "runtime_logs_not_observed" in _finding_codes(report)

    def test_usage_reports_aggregates_without_raw_payloads(self, repo: Path) -> None:
        """Audit exposes per-runtime counts and timing, declares tokens unavailable, and leaks no record payload.

        Three Claude records (cli, tool, skill) carry secret-looking argv, result, target and intent values; none may
        appear in the usage block.
        """
        _write_log(
            repo,
            "claude/activity.jsonl",
            _record(
                layer="cli",
                runtime="claude",
                timing_ms=7,
                argv=["query", "--secret-argv"],
                result={"target": "raw-result-secret"},
            ),
            _record(
                "2026-08-18T11:00:01Z",
                layer="tool",
                runtime="claude",
                tool="Read",
                target="/private/raw-target.py",
                timing_ms=11,
            ),
            _record(
                "2026-08-18T11:00:02Z",
                layer="skill",
                event="start",
                runtime="claude",
                skill="codemap-py:query-code",
                intent="raw-intent-secret",
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
        raw_values = ("--secret-argv", "raw-result-secret", "/private/raw-target.py", "raw-intent-secret")
        assert all(raw_value not in rendered_usage for raw_value in raw_values)

    def test_usage_scopes_refresh_and_avoidance_by_runtime_session(self, repo: Path) -> None:
        """Usage evidence joins completions only within each runtime/session and the 600-second window.

        Claude has a refresh, a complete answer, a tool read inside the window and one outside it; Codex has a refresh
        and read in a session with no answer, plus a top-level answer with a read. Same session ids across runtimes and
        a legacy flat record must not cross-join.
        """
        _write_log(
            repo,
            "claude/usage.jsonl",
            _record(layer="cli", cmd="index", runtime="claude", session="same", result={"trigger": "query_self_heal"}),
            _record(
                "2026-08-18T11:00:02Z",
                layer="cli",
                runtime="claude",
                session="same",
                result={"module": "pkg.claude", "index": {"query_complete": True}},
            ),
            _record(
                "2026-08-18T11:00:05Z",
                layer="tool",
                tool="Read",
                target="pkg/claude.py",
                runtime="claude",
                session="same",
            ),
            _record(
                "2026-08-18T11:11:00Z",
                layer="tool",
                tool="Read",
                target="pkg/claude.py",
                runtime="claude",
                session="same",
            ),
            _record(
                layer="cli",
                cmd="index",
                runtime="claude",
                session="refresh-only",
                result={"trigger": "claude_prompt_background"},
            ),
        )
        _write_log(
            repo,
            "codex/usage.jsonl",
            _record(layer="cli", cmd="index", runtime="codex", session="same", result={"trigger": "direct_cli"}),
            _record(
                "2026-08-18T11:00:03Z",
                layer="tool",
                tool="Read",
                target="pkg/claude.py",
                runtime="codex",
                session="same",
            ),
            _record(
                layer="cli",
                runtime="codex",
                session="top-level",
                result={"module": "pkg.codex", "query_complete": True},
            ),
            _record(
                "2026-08-18T11:00:04Z",
                layer="tool",
                tool="Grep",
                target="pkg/codex.py",
                runtime="codex",
                session="top-level",
            ),
        )
        _write_log(
            repo,
            "legacy.jsonl",
            _record(
                "2026-08-18T11:00:05Z", v="legacy", layer="tool", tool="Read", target="pkg/claude.py", session="same"
            ),
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

    def test_codex_cli_without_skill_event_is_not_missing_telemetry(self, repo: Path) -> None:
        """Codex has no Skill hook, so its CLI records alone must not imply broken telemetry.

        Only a Claude session is expected to log skill starts; flagging Codex for the same gap would be a false alarm.
        """
        _write_log(repo, "codex/cli.jsonl", _record(layer="cli", runtime="codex", session="thread-1"))

        report = integration.build_audit_report("codex", repo / integration.PROVIDER_DIR)

        assert "skill_telemetry_missing" not in _finding_codes(report)

    def test_isolated_current_record_is_not_an_isolation_bypass(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A record whose runtime field matches its runtime directory is not an isolation failure.

        This is the healthy shape of per-runtime telemetry; the bypass and identity findings must stay silent for it.
        """
        _write_log(repo, "claude/cli_current.jsonl", _record(layer="cli", runtime="claude"))

        code, payload = _run_json(repo, capsys, "audit", "--runtime", "claude", "--json")

        assert code in {integration._EXIT_OK, integration._EXIT_RUNTIME}
        assert _finding_codes(payload).isdisjoint({"runtime_log_isolation_bypassed", "runtime_identity_missing"})

    def test_empty_logs_warn_that_evidence_was_not_observed(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No selected-runtime records is a warning, never a declared-path pass.

        With empty logs there is nothing to observe, so the audit must say so (``warn``) instead of reporting healthy.
        """
        code, payload = _run_json(repo, capsys, "audit", "--runtime", "claude", "--json")

        assert code == integration._EXIT_OK
        assert payload["status"] == "warn"
        assert _finding_codes(payload) >= {"runtime_logs_not_observed"}

    @pytest.mark.parametrize(
        ("since", "expected_records"),
        [pytest.param("2026-08-18", 1, id="2026-08-18"), pytest.param("2026-08-19", 0, id="2026-08-19")],
    )
    def test_since_bounds_evidence(
        self, repo: Path, capsys: pytest.CaptureFixture[str], since: str, expected_records: int
    ) -> None:
        """``--since`` includes records on its date and excludes earlier telemetry.

        One Claude record is dated 2026-08-18: a ``--since`` of that day keeps it, the next day drops it.
        """
        _write_log(
            repo, "claude/cli.jsonl", {"ts": "2026-08-18T00:00:00Z", "runtime": "claude", "v": integration.__version__}
        )

        code, payload = _run_json(repo, capsys, "audit", "--runtime", "claude", "--since", since, "--json")

        assert code == 0
        assert payload["window"]["since"] == since
        assert payload["runtime_logs"]["selected"]["claude"]["records"] == expected_records

    def test_skips_malformed_jsonl_without_claiming_a_record(self, repo: Path) -> None:
        """A malformed telemetry line is bounded input noise, not a false observation.

        The only line is not JSON, so zero records are counted and the runtime stays "not observed".
        """
        log = repo / ".cache" / "codemap" / "logs" / "claude" / "cli.jsonl"
        log.parent.mkdir(parents=True)
        _seed(log, "not-json\n")

        report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)

        assert report["runtime_logs"]["selected"]["claude"]["records"] == 0
        assert _finding_codes(report) >= {"runtime_logs_not_observed"}

    def test_runtime_directory_record_mismatch_fails(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A record claiming Codex inside the Claude directory is a stable identity failure.

        Per-runtime isolation only holds if a directory's records agree with its name; a mismatch exits with the
        runtime-failure code.
        """
        _write_log(repo, "claude/cli_current.jsonl", _record(layer="cli", runtime="codex"))

        code, payload = _run_json(repo, capsys, "audit", "--runtime", "claude", "--json")

        assert code == integration._EXIT_RUNTIME
        assert _finding_codes(payload) >= {"runtime_identity_missing"}


class TestAuditNativeEvidence:
    """Audit findings derived from installed-plugin (native CLI) evidence."""

    @pytest.mark.parametrize(
        ("indexed_sha", "expected_stale_states"),
        [pytest.param("current", [], id="matching-sha"), pytest.param("stale", ["stale"], id="stale-sha")],
    )
    def test_index_evidence_reports_degraded_and_stale_state_without_query(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, indexed_sha: str, expected_stale_states: list[str]
    ) -> None:
        """Audit reads bounded index evidence: degraded modules persist, SHA drift is explicit, query stays forbidden.

        The on-disk index names one degraded module and an indexed git SHA that is either current or stale; a call to
        ``query.main`` would fail the test, proving the audit inspects the file directly.
        """
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
        stale_states = [f["evidence"]["state"] for f in report["findings"] if f["code"] == "index_stale_or_unknown"]
        assert findings["index_degraded"]["evidence"] == {
            "count": 1,
            "modules": [{"path": "pkg/broken.py", "reason": "parse error"}],
        }
        assert stale_states == expected_stale_states

    def test_exposes_codex_rig_global_status(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Codex audits surface the read-only global-instructions status instead of silently omitting it.

        The status source is stubbed to a known value so the report field can be matched exactly.
        """
        monkeypatch.setattr(integration.native, "codex_rig_global_status", lambda: "authenticated")

        report = integration.build_audit_report("codex", repo / integration.PROVIDER_DIR)

        assert report["provider"]["codex_rig_global_instructions"] == "authenticated"

    def test_same_version_provider_content_drift_and_session_catalog_limit(
        self, repo: Path, native_probe: Callable[[object], None], tmp_path: Path
    ) -> None:
        """Audit fails same-version provider bytes while naming the native session-catalog boundary.

        The source and the installed provider claim one version but differ in ``README.md``; the report must show both
        content identities, a high-severity drift finding, and that the native plugin list cannot prove which session
        runs which build.
        """
        _write_provider_manifest(repo)
        (repo / integration.PROVIDER_DIR / "README.md").write_text("source bytes\n")
        native = tmp_path / "native-codemap"
        _write_manifest(native, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, integration.__version__)
        _write_manifest(native, integration.Runtime.CODEX, integration.PROVIDER_NAME, integration.__version__)
        (native / "README.md").write_text("installed bytes\n")
        native_probe([_native_provider_entry(native)])

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
        assert all(
            provider[key]["state"] == "observed"
            and provider[key]["schema_version"] == 1
            and len(provider[key]["sha256"]) == 64
            and provider[key]["file_count"] >= 2
            and provider[key]["bytes_hashed"] > 0
            for key in ("source_content", "native_content")
        )
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

    def test_equal_content_is_not_drift(self, repo: Path, native_probe: Callable[[object], None]) -> None:
        """Native bytes equal to the source are observed with the same digest and raise no drift finding.

        The installed provider path is the source directory itself, so both content identities must match.
        """
        _write_provider_manifest(repo)
        native_probe([_native_provider_entry(repo / integration.PROVIDER_DIR)])

        report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)

        provider = report["provider"]["runtimes"]["claude"]["provider"]
        assert "provider_same_version_content_drift" not in _finding_codes(report)
        assert provider["native_content"]["state"] == "observed"
        assert provider["native_content"]["sha256"] == provider["source_content"]["sha256"]

    def test_unreadable_native_root_is_unknown_not_drift(
        self, repo: Path, native_probe: Callable[[object], None]
    ) -> None:
        """An unreadable native install root is "unknown" evidence, never a false drift finding.

        The native plugin list points at a directory that does not exist, so its bytes cannot be compared.
        """
        _write_provider_manifest(repo)
        native_probe([_native_provider_entry(repo / "missing-native-root")])

        report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)

        provider = report["provider"]["runtimes"]["claude"]["provider"]
        assert "provider_same_version_content_drift" not in _finding_codes(report)
        assert provider["native_content"] == {"state": "unknown", "reason": "native_plugin_root_unreadable"}

    def test_observes_codex_native_source_path(self, repo: Path, native_probe: Callable[[object], None]) -> None:
        """Codex native evidence uses the configured source path without assuming a cache root.

        The Codex plugin list reports a local source path; the audit must observe that path and hash it as the native
        content.
        """
        source = repo / integration.PROVIDER_DIR
        _write_manifest(source, integration.Runtime.CODEX, integration.PROVIDER_NAME, integration.__version__)
        native_probe(
            {
                "installed": [
                    {
                        "name": integration.PROVIDER_NAME,
                        "version": integration.__version__,
                        "enabled": True,
                        "source": {"type": "local", "path": str(source)},
                    }
                ]
            }
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
    def test_native_record_schema_is_stable_when_not_observed(
        self, runtime: integration.Runtime, payload: object, consumer: str
    ) -> None:
        """Unavailable native discovery retains the normalized provider/consumer record schema.

        Callers read the same keys whether or not the CLI answered, so an unobserved record must still carry them, with
        ``None`` values.
        """
        assert integration._native_plugin_record(runtime, payload, consumer) == {
            "state": "not_observed",
            "name": consumer,
            "version": None,
            "enabled": None,
            "source_path": None,
        }

    def test_warns_on_same_version_consumer_content_drift(
        self, repo: Path, native_probe: Callable[[object], None], tmp_path: Path
    ) -> None:
        """A selected consumer with equal versions but unequal observed bytes is a factual warning.

        The installed ``foundry`` differs from the source copy in ``README.md`` at the same version; this is a medium-
        severity warning, not a failure like provider drift.
        """
        provider_source = repo / integration.PROVIDER_DIR
        _write_provider_manifest(repo)
        (repo / "plugins/cc_foundry" / "README.md").write_text("source consumer\n")
        native_consumer = tmp_path / "native-foundry"
        _write_manifest(native_consumer, integration.Runtime.CLAUDE, "foundry", "1.0.0")
        (native_consumer / "README.md").write_text("native consumer\n")
        native_probe(
            [
                _native_provider_entry(provider_source),
                {
                    "id": "foundry@borda-ai-rig",
                    "version": "1.0.0",
                    "enabled": True,
                    "installPath": str(native_consumer),
                },
            ]
        )

        report = integration.build_audit_report("claude", provider_source)

        foundry = report["consumers"]["claude"]["foundry"]
        finding = next(item for item in report["findings"] if item["code"] == "consumer_same_version_content_drift")
        assert foundry["native_plugin"]["source_path"] == str(native_consumer)
        assert foundry["source_content"]["sha256"] != foundry["native_content"]["sha256"]
        assert finding["severity"] == "medium"
        assert finding["status"] == "warn"
        assert finding["evidence"]["consumer"] == "foundry"

    def test_version_drift_reported_only_when_source_provider_is_trusted(
        self, repo: Path, native_probe: Callable[[object], None]
    ) -> None:
        """Installed-version comparisons activate once the source provider matches this runtime.

        The source provider claims the running version and the installed provider and consumer report other versions, so
        both drift findings must appear.
        """
        _write_provider_manifest(repo)
        native_probe(
            [
                {"id": "codemap-py@borda-ai-rig", "version": "older", "enabled": True},
                {"id": "oss@borda-ai-rig", "version": "other", "enabled": True},
            ]
        )

        report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)

        assert {"provider_version_drift", "consumer_version_drift"} <= _finding_codes(report)


def _drop_native_contract(source: Path, native: Path) -> None:
    """Delete the installed consumer's contract file."""
    (native / "shared/codemap-contract.md").unlink()


def _unreference_native_contract(source: Path, native: Path) -> None:
    """Make the installed consumer's skill stop referencing the contract."""
    _seed(native / _GUIDANCE_PATH, "No query guidance loaded.\n")


def _stale_native_contract(source: Path, native: Path) -> None:
    """Make the installed consumer's contract differ from the source."""
    _seed(native / "shared/codemap-contract.md", "Outdated query instructions.\n")


def _drop_source_contract(source: Path, native: Path) -> None:
    """Delete the source consumer's contract file."""
    (source / "shared/codemap-contract.md").unlink()


def _unreference_source_contract(source: Path, native: Path) -> None:
    """Make the source consumer's skill stop referencing the contract."""
    _seed(source / _GUIDANCE_PATH, "No query guidance loaded.\n")


def _leave_guidance_intact(source: Path, native: Path) -> None:
    """Leave both consumer trees consistent."""


class TestAuditConsumerGuidance:
    """Metadata-only integration must not hide missing or stale consumer instructions."""

    @pytest.fixture
    def audit_guidance(
        self, repo: Path, tmp_path: Path, native_probe: Callable[[object], None]
    ) -> Callable[..., tuple[dict, bool]]:
        """Return a runner that mutates the source/native consumer trees, audits Codex, and reports write-freedom."""
        source = repo / "plugins/codex-rig"
        native = tmp_path / "installed-consumer"
        _write_manifest(native, integration.Runtime.CODEX, "codex-rig", "1.0.0")
        for plugin in (source, native):
            (plugin / "shared").mkdir(exist_ok=True)
            (plugin / "skills/inspect").mkdir(parents=True)
            _seed(plugin / "shared/codemap-contract.md", "Use the verified launcher and reuse query evidence.\n")
            _seed(plugin / _GUIDANCE_PATH, "Follow ../../shared/codemap-contract.md for structural queries.\n")

        def run(mutate: Callable[[Path, Path], None], *, installed: bool) -> tuple[dict, bool]:
            mutate(source, native)
            entry = {"name": "codex-rig", "version": "1.0.0", "enabled": True, "source": {"path": str(native)}}
            native_probe({"installed": [entry] if installed else []})
            before = tree_snapshot(repo), tree_snapshot(native)
            report = integration.build_audit_report("codex", repo / integration.PROVIDER_DIR)
            return report, (tree_snapshot(repo), tree_snapshot(native)) == before

        return run

    @pytest.mark.parametrize(
        ("mutate", "installed", "expected_finding"),
        [
            pytest.param(_leave_guidance_intact, True, None, id="matching"),
            pytest.param(_drop_native_contract, True, "consumer_query_guidance_missing", id="missing"),
            pytest.param(_unreference_native_contract, True, "consumer_query_guidance_unreachable", id="unreferenced"),
            pytest.param(_stale_native_contract, True, "consumer_query_guidance_drift", id="stale"),
            pytest.param(_leave_guidance_intact, False, None, id="optional-absent"),
        ],
    )
    def test_installed_guidance_findings(self, audit_guidance, mutate, installed: bool, expected_finding) -> None:
        """The installed consumer's guidance is compared with the source's, raising at most one named warning.

        Source guidance is intact and referenced; the installed copy is missing, unreferenced, stale or fine (or the
        consumer is not installed at all, which is optional and silent). The audit also must not write to either tree.
        """
        report, untouched = audit_guidance(mutate, installed=installed)

        guidance = report["consumers"]["codex"]["codex-rig"]["query_guidance"]
        findings = [item for item in report["findings"] if item["code"].startswith("consumer_query_guidance_")]
        assert guidance["source"]["state"] == "observed"
        assert guidance["source"]["referenced_by"] == [_GUIDANCE_PATH]
        assert [item["code"] for item in findings] == ([] if expected_finding is None else [expected_finding])
        assert all(item["status"] == "warn" and item["evidence"]["consumer"] == "codex-rig" for item in findings)
        assert untouched

    @pytest.mark.parametrize(
        ("mutate", "expected_source_state"),
        [
            pytest.param(_drop_source_contract, "missing", id="source-missing"),
            pytest.param(_unreference_source_contract, "unreferenced", id="source-unreferenced"),
        ],
    )
    def test_broken_source_guidance_is_reported(self, audit_guidance, mutate, expected_source_state: str) -> None:
        """A broken source guidance contract is reported as missing or unreachable, without writes.

        When the source consumer's own contract is absent or not referenced, the installed copy cannot be judged correct
        either, so the finding names the guidance as missing/unreachable and the trees stay untouched.
        """
        report, untouched = audit_guidance(mutate, installed=True)

        guidance = report["consumers"]["codex"]["codex-rig"]["query_guidance"]
        findings = [item for item in report["findings"] if item["code"].startswith("consumer_query_guidance_")]
        assert guidance["source"]["state"] == expected_source_state
        assert [item["code"] for item in findings] == [
            {"missing": "consumer_query_guidance_missing", "unreferenced": "consumer_query_guidance_unreachable"}[
                expected_source_state
            ]
        ]
        assert all(item["status"] == "warn" and item["evidence"]["consumer"] == "codex-rig" for item in findings)
        assert untouched


class TestAudit:
    """The ``audit`` entry points: invocation errors, zero-write guarantee and managed-block checks."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["check", "--runtime", "claude"], id="removed-check-subcommand"),
            pytest.param(["audit", "--since", "2026-13-40"], id="invalid-audit-since-date"),
            pytest.param(["plan", "--runtime", "claude", "--consumers", "not-a-target"], id="unknown-plan-consumer"),
        ],
    )
    def test_invalid_invocation_is_a_usage_error(self, repo: Path, argv: list[str]) -> None:
        """A removed subcommand, an invalid date, or an unknown consumer is a usage error, never silently accepted.

        The removed ``check`` subcommand is rejected rather than forwarded as an audit alias; an invalid ``--since``
        date never falls through as an unbounded audit request; an unrecognized ``--consumers`` name is a ``2``-class
        syntax error, never a lookup.
        """
        assert integration.run(argv, repo / integration.PROVIDER_DIR) == integration._EXIT_USAGE

    def test_reports_invalid_managed_block_without_mutating_it(self, repo: Path) -> None:
        """A tampered managed block is a failure and audit leaves its bytes unchanged.

        The block's embedded digest does not match its body, so it is reported ``managed_block_invalid``; audit is read-
        only, so the file must be byte-identical afterwards.
        """
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

        assert _finding_codes(report) >= {"managed_block_invalid"}
        assert path.read_bytes() == before

    def test_never_calls_query_or_mutation_paths(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Audit reads evidence only; reaching a query, plan mutation, or native write fails the test.

        Every path that could change state or run a query is replaced by a function that raises, then the full two-
        runtime audit runs and must still finish with a ``warn`` and an unchanged tree.
        """
        before = tree_snapshot(repo)

        def _forbidden(*args: object, **kwargs: object) -> None:
            """Fail if the audit unexpectedly reaches a query or mutation path."""
            raise AssertionError("audit invoked a forbidden mutation/query path")

        monkeypatch.setattr(integration.query, "main", _forbidden)
        monkeypatch.setattr(integration.apply_sync, "apply_plan", _forbidden)
        monkeypatch.setattr(integration.apply_sync, "sync_plan", _forbidden)
        monkeypatch.setattr(integration.native, "_run_native_required", _forbidden)

        report = integration.build_audit_report("both", repo / integration.PROVIDER_DIR)

        assert report["status"] == "warn"
        assert tree_snapshot(repo) == before

    def test_is_zero_write(self, repo: Path) -> None:
        """Audit report generation does not mutate the fixture tree.

        A fresh fixture repo with no logs yields a ``warn`` v2 report and leaves every file byte-identical.
        """
        before = tree_snapshot(repo)

        report = integration.build_audit_report("both", repo / integration.PROVIDER_DIR)

        assert report["protocol"] == integration.PROTOCOL_VERSION
        assert report["status"] == "warn"
        assert tree_snapshot(repo) == before

    def test_cli_json_exits_zero(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The CLI emits a parseable v2 warning report for empty evidence and exits 0.

        Absent evidence is a warning, not a failure, so scripts calling ``audit --json`` on a fresh repo succeed.
        """
        code, payload = _run_json(repo, capsys, "audit", "--runtime", "both", "--json")

        assert code == 0
        assert payload["protocol"] == integration.PROTOCOL_VERSION
        assert payload["schema_version"] == 2
        assert payload["status"] == "warn"

    @pytest.mark.skipif(not _SOURCE_CHECKOUT, reason="not running from a source checkout")
    def test_every_managed_file_target_exists_in_this_source_tree(self) -> None:
        """Every ``CONSUMER_MANAGED_FILE`` target is a real file in this checkout.

        The map names the host file each consumer's managed block is inserted into. A target that does not exist reads
        as a first-time insert against a path nothing ships, so the defect stays invisible until an apply run creates a
        stray file — which is exactly how ``foundry`` came to point at an absent ``skills/_shared/codemap-context.md``.
        Every other test here builds a disposable fixture tree, so none of them can catch it: this one deliberately
        asserts against the real source checkout.
        """
        repo_root = Path(__file__).resolve().parents[4]
        missing = [
            f"{target.plugin_dir}/{integration.CONSUMER_MANAGED_FILE[target.consumer]}"
            for target in integration.ALL_TARGETS
            if not (repo_root / target.plugin_dir / integration.CONSUMER_MANAGED_FILE[target.consumer]).is_file()
        ]
        assert missing == [], f"CONSUMER_MANAGED_FILE targets absent from the source tree: {missing}"

    def test_reports_absent_consumer_as_named_state_not_error(self, repo: Path) -> None:
        """A closed-set consumer with no manifest on disk is reported ``absent``, not raised as an error.

        The ``oss`` plugin directory is removed from the fixture tree; the audit must still list ``oss`` with no
        manifest, no versions and an absent managed block.
        """
        oss = next(target for target in integration.CLAUDE_TARGETS if target.consumer == "oss")
        shutil.rmtree(repo / oss.plugin_dir)

        report = integration.build_audit_report("claude", repo / integration.PROVIDER_DIR)

        oss_status = report["consumers"]["claude"]["oss"]
        assert {key: oss_status[key] for key in _ABSENT_FIELDS} == _ABSENT_FIELDS
        assert oss_status["managed_block"]["status"] == "absent"


class TestPlan:
    """The ``plan`` artifact: zero mutation outside its report and a stable SHA-256."""

    def test_sentinel_schema_stays_v1_while_body_protocol_is_v2(self) -> None:
        """The managed sentinel schema stays pinned at v1 while the block body speaks protocol v2.

        The 0.31.0 compat promise is exactly this split: existing v1 sentinels stay authenticable while the body
        protocol advances. A silent bump of either constant would pass every round-trip test yet break installed
        consumers, so the literals are pinned side by side here.
        """
        assert integration.BLOCK_SCHEMA_VERSION == 1
        assert integration.PROTOCOL_VERSION == "codemap-py.integration.v2"
        assert integration._render_managed_block("x\n").startswith("<!-- codemap-py:integration:begin v1 sha256=")

    def test_enum_members_serialise_to_cli_json_values(self, repo: Path) -> None:
        """Enum-backed integration plans preserve the CLI's original JSON strings.

        ``Runtime`` and ``Source`` are ``str`` enums; a plan built from members must serialise to the plain strings the
        command line uses, not to enum reprs.
        """
        plan = integration.build_plan(
            integration.Runtime.CLAUDE, ["oss"], integration.Source.LOCAL_CANDIDATE, repo / integration.PROVIDER_DIR
        )

        serialized = json.loads(json.dumps(plan))

        assert integration.Runtime.__bases__ == (str, Enum)
        assert integration.Source.__bases__ == (str, Enum)
        assert serialized["runtime"] == "claude"
        assert serialized["source"] == "local-candidate"
        assert serialized["ops"][0]["runtime"] == "claude"
        assert serialized["ops"][1]["desired"]["ref"] == "local-candidate"

    def test_writes_only_its_out_artifact(self, repo: Path, tmp_path: Path) -> None:
        """``plan --out`` writes nothing under the fixture tree besides the named artifact.

        The artifact goes to a path outside the repo, so the repo snapshot must be unchanged.
        """
        before = tree_snapshot(repo)
        out = tmp_path / "plan.json"

        code = integration.run(["plan", "--runtime", "claude", "--out", str(out)], repo / integration.PROVIDER_DIR)

        assert code == 0
        assert out.is_file()
        assert tree_snapshot(repo) == before

    def test_default_out_is_confined_to_reports_dir(self, repo: Path) -> None:
        """Without ``--out``, the artifact lands only under the fixture's own ``.reports/integrate/``.

        The default location must be inside the repo's report directory so a plan never writes into consumer sources.
        """
        before = tree_snapshot(repo)

        code = integration.run(["plan", "--runtime", "claude", "--consumers", "oss"], repo / integration.PROVIDER_DIR)

        after = tree_snapshot(repo)
        changed = {k for k in after if after.get(k) != before.get(k)}
        assert code == 0
        assert changed
        assert all(k.startswith(".reports/integrate/") for k in changed)

    def test_sha256_is_stable_and_self_consistent(self, repo: Path) -> None:
        """The recorded ``plan_sha256`` equals the digest recomputed over the plan's own body.

        Approval is bound to this digest, so recomputing it from the body must reproduce the stored value.
        """
        plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)

        assert integration.compute_plan_sha256(plan) == plan["plan_sha256"]


class TestApproval:
    """Approval digest verification."""

    @pytest.mark.parametrize(
        ("approval", "expected_code"),
        [
            pytest.param("not-a-sha256", "approve_malformed", id="non-hex-wrong-length"),
            pytest.param("0" * 64, "approve_mismatch", id="well-formed-but-wrong"),
        ],
    )
    def test_bad_approval_rejected(self, repo: Path, approval: str, expected_code: str) -> None:
        """A malformed ``--approve`` is ``approve_malformed``; a well-formed but wrong SHA-256 is ``approve_mismatch``.

        ``verify_approval`` distinguishes the two failure shapes by code, so a caller can tell a typo from a stale
        plan: a value that is not a SHA-256 at all, and a well-formed digest that does not match the plan.
        """
        plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)

        with pytest.raises(integration.ApprovalError) as exc:
            integration.verify_approval(plan, approval)

        assert exc.value.code == expected_code

    def test_correct_sha_verifies(self, repo: Path) -> None:
        """The plan's own recorded SHA-256 verifies without raising.

        This is the happy path every approved ``apply`` starts from.
        """
        plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)

        integration.verify_approval(plan, plan["plan_sha256"])

    def test_apply_cli_bad_approve_exits_usage(self, repo: Path, tmp_path: Path) -> None:
        """An invalid ``--approve`` value maps to the documented usage exit code.

        The plan file is valid; only the approval string ``nope`` is not a SHA-256.
        """
        plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
        plan_path = tmp_path / "plan.json"
        _seed(plan_path, json.dumps(plan))

        code = integration.run(
            ["apply", "--plan", str(plan_path), "--approve", "nope"], repo / integration.PROVIDER_DIR
        )

        assert code == integration._EXIT_USAGE


def _single_op_plan(repo: Path, consumer: str = "oss") -> dict:
    """Build the smallest valid apply plan for one consumer target."""
    return integration.build_plan("claude", [consumer], None, repo / integration.PROVIDER_DIR)


def _assert_refused(repo: Path, plan: dict, code: str, original_bytes: bytes | None) -> None:
    """Assert that an apply plan is refused with *code* without changing target bytes."""
    target_path = repo / plan["ops"][0]["path"]
    with pytest.raises(integration.RefusalError) as exc:
        integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)
    assert exc.value.code == code
    after = target_path.read_bytes() if target_path.is_file() else None
    assert after == original_bytes


def _first_target_path(repo: Path) -> Path:
    """Return the managed-file path of the first Claude consumer target, creating its parent directory."""
    target = integration.CLAUDE_TARGETS[0]
    path = repo / target.plugin_dir / integration.CONSUMER_MANAGED_FILE[target.consumer]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestApplyRefusals:
    """Each refusal is an exit-mapped ``RefusalError`` that leaves the target file untouched."""

    @pytest.mark.parametrize(
        ("target_path", "expected_code"),
        [
            pytest.param(
                "plugins/cache/oss/skills/_shared/codemap-context.md", "installed_cache_root", id="installed-cache-root"
            ),
            pytest.param("plugins/some-other-dir/escape.md", "path_escape", id="path-escape"),
        ],
    )
    def test_unsafe_target(self, repo: Path, target_path: str, expected_code: str) -> None:
        """A target under any ``plugins/cache/...`` tree, or outside its consumer's own plugin directory, is refused.

        Each unsafe target is refused with its own stable code and the target file is left untouched; the plan digest is
        recomputed so the refusal comes from the path check, never from a digest mismatch.
        """
        plan = _single_op_plan(repo)
        plan["ops"][0]["path"] = target_path
        plan["plan_sha256"] = integration.compute_plan_sha256(plan)

        _assert_refused(repo, plan, expected_code, None)

    @skip_file_symlink_unavailable
    def test_symlink_target(self, repo: Path) -> None:
        """A target path traversing a symlink is refused, even though the plan itself is unmodified.

        The symlink points at a sibling file *inside* the same consumer plugin dir, so the resolved path still passes
        the path-containment check — isolating ``symlink_target`` from ``path_escape`` (both fire on an out-of-tree
        symlink target; only this shape proves the symlink check specifically).
        """
        plan = _single_op_plan(repo)
        target_path = repo / plan["ops"][0]["path"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        sibling = target_path.parent / "sibling.md"
        _seed(sibling, "not a managed block\n")
        os.symlink(sibling, target_path)

        _assert_refused(repo, plan, "symlink_target", sibling.read_bytes())

    def test_dirty_overlap(self, repo: Path) -> None:
        """Uncommitted local changes on the target file refuse the overlay.

        The target is committed and then edited without committing; the refusal fires on that uncommitted overlay,
        independent of the plan's ``before_hash`` matching or not.
        """
        plan = _single_op_plan(repo)
        target_path = repo / plan["ops"][0]["path"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _seed(target_path, "tracked content\n")
        _git_commit_all(repo)
        _seed(target_path, "tracked content\nuncommitted local edit\n")

        _assert_refused(repo, plan, "dirty_overlap", target_path.read_bytes())

    def test_unverified_product_identity(self, repo: Path) -> None:
        """A consumer manifest whose ``name`` no longer matches the plan's target is refused.

        Applying to a directory that no longer identifies as the planned product could write into the wrong plugin.
        """
        plan = _single_op_plan(repo)
        manifest_path = repo / "plugins" / "cc_oss" / ".claude-plugin" / "plugin.json"
        _seed(manifest_path, json.dumps({"name": "tampered-name", "version": "1.0.0"}))

        _assert_refused(repo, plan, "unverified_product_identity", None)

    def test_foreign_or_modified_marker(self, repo: Path) -> None:
        """A managed block whose embedded sha256 does not match its own body is a foreign/tampered marker.

        One hex digit of the block's stamp is flipped, so the block no longer authenticates as Codemap's own.
        """
        target_path = _first_target_path(repo)
        real_block = integration._render_managed_block("some body\n")
        stamp_index = real_block.index("sha256=") + len("sha256=")
        flipped_digit = "0" if real_block[stamp_index] != "0" else "1"
        _seed(target_path, real_block[:stamp_index] + flipped_digit + real_block[stamp_index + 1 :])
        plan = integration.build_plan(
            "claude", [integration.CLAUDE_TARGETS[0].consumer], None, repo / integration.PROVIDER_DIR
        )

        _assert_refused(repo, plan, "foreign_or_modified_marker", target_path.read_bytes())

    def test_drift_on_out_of_band_edit(self, repo: Path) -> None:
        """A target edited out of band between ``plan`` and ``apply`` invalidates the approval.

        The plan records the file's hash; changing the file afterwards makes the approved plan stale.
        """
        target_path = _first_target_path(repo)
        _seed(target_path, "original prose\n")
        plan = integration.build_plan(
            "claude", [integration.CLAUDE_TARGETS[0].consumer], None, repo / integration.PROVIDER_DIR
        )
        _seed(target_path, "original prose\nedited after the plan was made\n")

        _assert_refused(repo, plan, "drift", target_path.read_bytes())

    def test_drift_when_block_unexpectedly_appears(self, repo: Path) -> None:
        """A managed block appearing where the plan expected a first-time insert is also drift.

        The plan was made when the file was absent (``first_time``); a block that exists by apply time means someone
        else wired it in the meantime.
        """
        plan = integration.build_plan(
            "claude", [integration.CLAUDE_TARGETS[0].consumer], None, repo / integration.PROVIDER_DIR
        )
        target_path = repo / plan["ops"][0]["path"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _seed(target_path, integration._render_managed_block("unexpected pre-existing block\n"))

        assert plan["ops"][0]["first_time"] is True
        _assert_refused(repo, plan, "drift", target_path.read_bytes())


class TestApplyMutation:
    """In-file mutation semantics of a successful apply."""

    def test_first_time_insert_preserves_existing_content(self, repo: Path) -> None:
        """A first-time apply appends the managed block; real pre-existing prose survives byte-for-byte.

        Human-authored notes in the host file must not be rewritten, only followed by the managed block.
        """
        target_path = _first_target_path(repo)
        original = "# Codemap context\n\nHuman-authored notes that must survive.\n"
        _seed(target_path, original)
        plan = integration.build_plan(
            "claude", [integration.CLAUDE_TARGETS[0].consumer], None, repo / integration.PROVIDER_DIR
        )

        integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)

        mutated = target_path.read_text()
        assert mutated.startswith(original)
        assert "codemap-py:integration:begin" in mutated

    def test_writes_lf_only_managed_block(self, repo: Path) -> None:
        """A first-time apply's managed block is LF-only on disk, regardless of host OS.

        CRLF would change the block's digest on Windows and make the next audit see drift.
        """
        plan = integration.build_plan(
            "claude", [integration.CLAUDE_TARGETS[0].consumer], None, repo / integration.PROVIDER_DIR
        )

        integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)

        data = (repo / plan["ops"][0]["path"]).read_bytes()
        assert b"\r\n" not in data
        assert b"codemap-py:integration:begin" in data

    def test_reapply_is_a_zero_byte_noop(self, repo: Path) -> None:
        """Re-running the same approved plan against an already-wired file is a zero-byte no-op, exit 0.

        Applying twice must be safe, so the second run reports ``complete`` and leaves the bytes unchanged.
        """
        plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
        first = integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)
        target_path = repo / plan["ops"][0]["path"]
        bytes_after_first = target_path.read_bytes()

        second = integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)

        assert first["state"] == "complete"
        assert second["state"] == "complete"
        assert target_path.read_bytes() == bytes_after_first


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
    path = root / target.plugin_dir / integration.CONSUMER_MANAGED_FILE[consumer]
    path.parent.mkdir(parents=True, exist_ok=True)
    _seed(path, text)
    return path


class TestJournalAndRollback:
    """Journal transitions and rollback of a partially failed multi-target apply."""

    def test_journal_records_full_success_sequence(self, repo: Path, tmp_path: Path) -> None:
        """A clean single-target apply journals ``approved -> applying -> verified -> complete``.

        The journal is the recovery record, so every state transition of a successful apply must be present in order.
        """
        plan = integration.build_plan("claude", ["oss"], None, repo / integration.PROVIDER_DIR)
        journal_dir = tmp_path / "journal"

        result = integration.apply_plan(
            plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR, journal_dir=journal_dir
        )

        states = [json.loads(line)["state"] for line in (journal_dir / "journal.jsonl").read_text().splitlines()]
        assert result["state"] == "complete"
        assert states == ["approved", "applying", "verified", "complete"]

    @pytest.mark.parametrize(
        ("first_consumer", "second_consumer"),
        [pytest.param("oss", "develop", id="oss-then-develop"), pytest.param("develop", "oss", id="develop-then-oss")],
    )
    def test_rollback_restores_first_target_in_both_orders(
        self, repo: Path, tmp_path: Path, first_consumer: str, second_consumer: str
    ) -> None:
        """First target verified, second fails -> rollback restores the first target's full original file.

        The second target's manifest identity is tampered after planning, so its write is refused mid-apply; the first
        target must be restored byte-for-byte whichever order the two consumers are planned in.
        """
        first_path = _seed_prose(repo, first_consumer, f"{first_consumer} original notes\n")
        _seed_prose(repo, second_consumer, f"{second_consumer} original notes\n")
        original_first_bytes = first_path.read_bytes()
        plan = integration.build_plan(
            "claude", [first_consumer, second_consumer], None, repo / integration.PROVIDER_DIR
        )
        _tamper_identity(repo, next(t for t in integration.CLAUDE_TARGETS if t.consumer == second_consumer))

        with pytest.raises(integration.IntegrationError) as exc:
            integration.apply_plan(
                plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR, journal_dir=tmp_path / "journal"
            )

        assert exc.value.detail["state"] == "rollback-succeeded"
        assert exc.value.detail["applied"] == [0]
        assert first_path.read_bytes() == original_first_bytes

    def test_rollback_failure_reports_recovery_required(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When rollback itself cannot restore the first target, the engine reports ``recovery-required``.

        Rollback failure is forced deterministically by stubbing ``Journal.save_before_image`` to a no-op — the first
        target then applies successfully (its own write is untouched) but leaves no before-image to restore from, so the
        hash check after rollback finds that the deleted file does not match the plan's recorded ``before_hash`` and
        reports ``rollback-failed``.
        """
        first_path = _seed_prose(repo, "oss", "oss original notes\n")
        _seed_prose(repo, "develop", "develop original notes\n")
        plan = integration.build_plan("claude", ["oss", "develop"], None, repo / integration.PROVIDER_DIR)
        _tamper_identity(repo, next(t for t in integration.CLAUDE_TARGETS if t.consumer == "develop"))
        monkeypatch.setattr(integration.Journal, "save_before_image", lambda self, index, data: None)

        with pytest.raises(integration.IntegrationError) as exc:
            integration.apply_plan(
                plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR, journal_dir=tmp_path / "journal"
            )

        assert exc.value.code == "recovery_required"
        assert exc.value.detail["state"] == "rollback-failed"
        assert exc.value.detail["recovery_commands"]
        assert not first_path.is_file()  # rollback fell back to unlink; no before-image existed to restore


class TestSync:
    """Native runtime sync: drift refusal before any native command runs, with the native boundary stubbed."""

    def test_refuses_drift_before_native_call(
        self, repo: Path, native_probe: Callable[[object], None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Installed state is revalidated immediately before every plugin op, and drift stops it early.

        The plan is made with nothing installed; by sync time the provider is installed at another version, so after the
        marketplace op (which has no before-state) the next op must refuse with ``drift``.
        """
        plan = integration.build_plan("claude", ["oss"], "local-candidate", repo / integration.PROVIDER_DIR)
        native_probe([{"id": "codemap-py@borda-ai-rig", "version": "9.9.9", "enabled": True}])
        calls: list[list[str]] = []
        monkeypatch.setattr(integration.native, "_run_native_required", lambda argv: calls.append(list(argv)))

        with pytest.raises(integration.IntegrationError) as exc:
            integration.sync_plan(plan, plan["plan_sha256"], "local-candidate", repo / integration.PROVIDER_DIR)

        assert exc.value.code == "drift"
        assert len(calls) == 1  # only the marketplace op (no before-state to drift-check) ran

    def test_approve_source_mismatch_is_approval_error(self, repo: Path) -> None:
        """Synchronization approval must name the source recorded in the plan.

        A plan made for ``local-candidate`` cannot be approved as ``release``; that is an approval error, not a drift.
        """
        plan = integration.build_plan("claude", ["oss"], "local-candidate", repo / integration.PROVIDER_DIR)

        with pytest.raises(integration.ApprovalError) as exc:
            integration.sync_plan(plan, plan["plan_sha256"], "release", repo / integration.PROVIDER_DIR)

        assert exc.value.code == "source_mismatch"

    def test_apply_and_sync_ignore_each_others_operation_kind(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mixed plan keeps source edits in apply and native runtime work in sync.

        The plan holds one source write and one marketplace sync: ``apply`` must write the source and run no native
        command; ``sync`` must then run the marketplace argv and leave the source bytes alone.
        """
        plan = integration.build_plan("claude", ["oss"], "local-candidate", repo / integration.PROVIDER_DIR)
        source_op = next(op for op in plan["ops"] if op["kind"] == "source_write")
        marketplace_op = next(op for op in plan["ops"] if op["kind"] == "runtime_sync" and op["role"] == "marketplace")
        plan["ops"] = [source_op, marketplace_op]
        plan["plan_sha256"] = integration.compute_plan_sha256(plan)
        native_calls: list[list[str]] = []
        monkeypatch.setattr(integration.native, "_run_native_required", lambda argv: native_calls.append(list(argv)))
        source_path = repo / source_op["path"]

        integration.apply_plan(plan, plan["plan_sha256"], repo / integration.PROVIDER_DIR)
        calls_after_apply = list(native_calls)
        source_bytes_after_apply = source_path.read_bytes()
        integration.sync_plan(plan, plan["plan_sha256"], "local-candidate", repo / integration.PROVIDER_DIR)

        assert "codemap-py:integration:begin" in source_bytes_after_apply.decode()
        assert calls_after_apply == []
        assert native_calls == marketplace_op["argv"]
        assert source_path.read_bytes() == source_bytes_after_apply


class TestWindowsQuoting:
    """Windows batch-quoting guard (pure logic; runs on every OS via ``windows=True``)."""

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
    def test_flags_unsafe_argv(self, arguments: list[str], unsafe: bool) -> None:
        """Spaces and shell metacharacters are flagged unsafe for a ``.bat``/``.cmd`` launcher.

        A batch launcher re-parses its command line, so any argument that would be split or interpreted must be flagged
        before it is run.
        """
        assert integration._unsafe_windows_batch_argv("claude.cmd", arguments) is unsafe

    def test_resolve_rejects_unsafe_argv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A Windows batch launcher refuses to resolve argv containing shell metacharacters.

        ``oss & whoami`` would run a second command through ``cmd.exe``, so resolution must fail with
        ``unsafe_windows_argv`` instead of building a command line.
        """
        fake_cmd = tmp_path / "claude.cmd"
        _seed(fake_cmd, "@echo off\n")
        monkeypatch.setattr(integration.shutil, "which", lambda name: str(fake_cmd))

        with pytest.raises(integration.IntegrationError) as exc:
            integration._resolve_native_command(["claude", "plugin", "install", "oss & whoami"], windows=True)

        assert exc.value.code == "unsafe_windows_argv"

    def test_resolve_builds_quoted_line_for_safe_argv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A safe argv on a Windows batch launcher resolves to one quoted shell command line.

        The launcher path is quoted and the clean arguments follow, and the caller is told to use a shell.
        """
        fake_cmd = tmp_path / "claude.cmd"
        _seed(fake_cmd, "@echo off\n")
        monkeypatch.setattr(integration.shutil, "which", lambda name: str(fake_cmd))

        resolved, shell = integration._resolve_native_command(
            ["claude", "plugin", "install", "oss@borda-ai-rig"], windows=True
        )

        assert shell is True
        assert resolved == f'"{fake_cmd}" plugin install oss@borda-ai-rig'


class TestDemo:
    """The ``demo`` command: disposable structural-smoke evidence only."""

    def test_returns_evidence_confined_to_its_own_report(self, repo: Path) -> None:
        """The demo describes structural smoke evidence without implying a paired or token experiment.

        It must return its audit and query evidence, say that no comparison was performed and tokens are unavailable,
        write its report, and touch nothing in the repo outside ``.reports/integrate/``.
        """
        before = tree_snapshot(repo)

        demo = integration.run_demo("claude", repo / integration.PROVIDER_DIR)

        after = tree_snapshot(repo)
        changed = {k for k in after if after.get(k) != before.get(k)}
        assert demo["protocol"] == integration.PROTOCOL_VERSION
        assert "audit" in demo
        assert "query_evidence" in demo
        assert demo["scope"] == "structural_smoke"
        assert demo["comparison"] == "not_performed"
        assert demo["token_measurement"]["status"] == "unavailable"
        assert Path(demo["report_path"]).is_file()
        assert all(k.startswith(".reports/integrate/") for k in changed)

    def test_cli_exits_zero_with_no_index_built(self, repo: Path) -> None:
        """The demo CLI exits 0 when there is simply no index yet.

        A missing index is the normal first-run state of the demo, not a failure.
        """
        assert integration.run(["demo", "--runtime", "claude"], repo / integration.PROVIDER_DIR) == 0
