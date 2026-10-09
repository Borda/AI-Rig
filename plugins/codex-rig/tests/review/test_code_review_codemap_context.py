"""Require Code Review's persisted Codemap probe for Python diffs and bound each specialist's follow-up queries.

A review of a Python diff must carry ``codemap-context.json`` even when Codemap is not installed: the artifact is what
shows why structural evidence was absent. The provider itself stays optional, so an ``absent`` artifact passes while a
missing one fails closed. A current artifact must also be internally true: one standard ``diff-impact`` query over this
run's own diff file, with a status and reasons that its recorded query outcome implies. Follow-ups live in ``codemap-
followups/<role>-<NN>.json``, bind to a triggered role, and stay within the documented routes and limit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from test_review_completion_gate import _assessed_pr, _module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = PLUGIN_ROOT / "shared" / "codemap_adapter.py"
VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
# Placeholder `_run` replaces with this run's own absolute diff file, which it also creates.
_RUN_DIFF = "<run>/diff.patch"
_MAPPED_ANSWER = {
    "base": "diff-file:diff.patch",
    "changed_files": 1,
    "changed_modules": [{"module": "pkg.mod", "path": "pkg/mod.py", "changed_symbols": [], "risk": "LOW"}],
    "unmapped_files": [],
    "test_impact": {"test_files": [], "total": 0, "pytest_cmd": ""},
    "highest_risk": "LOW",
}
_UNMAPPED_ANSWER = {**_MAPPED_ANSWER, "changed_modules": [], "unmapped_files": ["pkg/new.py"]}
_UNMAPPED_GAP = "all 1 changed Python files unmapped by the index"
# `git diff` for a modified Python module, the default content of every run's `diff.patch`.
_MODIFIED_DIFF = (
    "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
)
# `git diff` deleting `pkg/util.py`; the provider reads no post-image, so it answers zero changed files as complete.
_DELETION_DIFF = (
    "diff --git a/pkg/util.py b/pkg/util.py\ndeleted file mode 100644\nindex 0000001..0000000\n--- a/pkg/util.py\n"
    "+++ /dev/null\n@@ -1,2 +0,0 @@\n-def helper():\n-    return 1\n"
)
_DELETION_ANSWER = {**_MAPPED_ANSWER, "changed_files": 0, "changed_modules": [], "highest_risk": "LOW"}
_DELETION_GAP = "1 deleted Python modules not analysed (importers unchecked): pkg/util.py"
_DOCS_DIFF = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
_DOCTOR = {
    "python": sys.executable,
    "version": "3.12.4",
    "implementation": "cpython",
    "supported": True,
    "plugin_root": "/fake/codemap-py",
    "index_path": "/fake/codemap-py/.cache/codemap/proj.json",
}


@pytest.fixture(name="validator")
def _validator() -> ModuleType:
    """Load the shipped Code Review validator by file path."""
    return _module(VALIDATOR)


@pytest.fixture(name="assessed_review")
def _assessed_review(tmp_path: Path) -> Path:
    """Build one complete assessed review run through the shared completion-gate builder."""
    return _assessed_pr.__wrapped__(tmp_path)


def _query(**overrides: object) -> dict[str, object]:
    """Return one adapter query record for a mapped, complete ``diff-impact`` answer."""
    record: dict[str, object] = {
        "subcommand": "diff-impact",
        "target": None,
        "exit_code": 0,
        "stale": False,
        "query_complete": True,
        "not_covered": [],
        "degraded_count": 0,
        "error": None,
        "index_path": None,
        "completeness_reason": None,
        "root_mismatch": False,
        "note": None,
        "changed_files": 1,
        "adapter_gap": None,
        "answer": _MAPPED_ANSWER,
        "answer_truncated": {},
        "input_rejected": False,
    }
    record.update(overrides)
    return record


def _artifact(
    probe_status: str = "available",
    queries: list[dict[str, object]] | None = None,
    status: str | None = None,
    reasons: list[str] | None = None,
    **overrides: object,
) -> dict[str, object]:
    """Return one current review artifact; by default one mapped query when the provider was usable, none otherwise."""
    payload: dict[str, object] = {
        "protocol_version": "codemap-py.integration.v1",
        "artifact_schema_version": 4,
        "category": "review",
        "query_kind": "standard",
        "target": None,
        "diff_file": _RUN_DIFF,
        "status": status or probe_status,
        "status_reasons": reasons or [],
        "probe": {"status": probe_status, "detail": f"probe reported {probe_status}", "launcher": None, "doctor": None},
        "queries": ([_query()] if probe_status == "available" else []) if queries is None else queries,
        "index_path_divergence": [],
    }
    payload.update(overrides)
    return payload


def _historical_artifact() -> dict[str, object]:
    """Return a schema-3 artifact as the adapter wrote it before the review batch read a diff file."""
    return {
        "protocol_version": "codemap-py.integration.v1",
        "artifact_schema_version": 3,
        "category": "review",
        "query_kind": "standard",
        "target": None,
        "status": "available",
        "probe": {"status": "available", "detail": "doctor healthy", "launcher": None, "doctor": None},
        "queries": [{"subcommand": "diff-impact", "query_complete": True, "error": None}],
        "index_path_divergence": [],
    }


def _follow_up(kind: str = "callers", target: object = "pkg.mod::fn") -> dict[str, object]:
    """Return one follow-up artifact as the adapter writes it for a fact route."""
    subcommand = {"callers": "fn-rdeps", "dependencies": "rdeps", "test-impact": "test-impact"}.get(kind, "fn-blast")
    record = _query(subcommand=subcommand, target=target, changed_files=None, answer={"called_by": []})
    return _artifact(queries=[record], query_kind=kind, target=target, category="review", diff_file=None)


def _run(
    tmp_path: Path,
    files: str,
    artifact: object = None,
    follow_ups: dict[str, object] | None = None,
    diff: str = _MODIFIED_DIFF,
) -> Path:
    """Write one review run: changed files, routing that triggers two roles, a diff file, and optional artifacts.

    A recorded ``diff_file`` of ``<run>/<name>`` becomes that file's absolute path inside this run.
    """
    (tmp_path / "files.txt").write_text(files, encoding="utf-8")
    (tmp_path / "diff.patch").write_text(diff, encoding="utf-8", newline="\n")
    routing = {"triggered_roles": ["qa-specialist", "sw-engineer"]}
    (tmp_path / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8")
    if isinstance(artifact, dict):
        recorded = artifact.get("diff_file")
        if isinstance(recorded, str) and recorded.startswith("<run>/"):
            artifact = {**artifact, "diff_file": str((tmp_path / recorded.removeprefix("<run>/")).resolve())}
        (tmp_path / "codemap-context.json").write_text(json.dumps(artifact), encoding="utf-8")
    elif isinstance(artifact, str):
        (tmp_path / "codemap-context.json").write_text(artifact, encoding="utf-8")
    for name, payload in (follow_ups or {}).items():
        (tmp_path / "codemap-followups").mkdir(exist_ok=True)
        (tmp_path / "codemap-followups" / name).write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def _codemap_failure(validator: ModuleType, run: Path, metadata: dict[str, object] | None = None) -> str | None:
    """Return the codemap check's failure code, or `None` when the run satisfies it."""
    try:
        validator._validate_codemap_context(run, metadata or {})
    except SystemExit as error:
        return str(error.code)
    return None


def _mark_recovered(run: Path, *, candidate: bool = True) -> Path:
    """Write what the native-provenance recovery helper leaves behind: its marker and, by default, its candidate."""
    (run / "native-recovery").mkdir()
    (run / "native-recovery" / "inspection-summary.json").write_text("{}", encoding="utf-8")
    if candidate:
        identity = {"schema_version": 8, "manifest_kind": "native-wave", "dispatch_protocol": "paged-context-v6"}
        (run / "specialist-manifest.native-recovery.candidate.json").write_text(json.dumps(identity), encoding="utf-8")
    return run


@pytest.mark.parametrize(
    ("files", "artifact", "expected"),
    [
        pytest.param("README.md\ndocs/guide.rst\n", None, None, id="non-python-diff-needs-no-artifact"),
        pytest.param("src/pkg/mod.py\n", None, "codemap-context-missing-for-python-diff", id="python-diff-missing"),
        pytest.param("src/pkg/mod.pyi\n", None, None, id="stub-only-diff-has-nothing-to-map"),
        pytest.param("src/pkg/MOD.PY\n", None, None, id="uppercase-suffix-is-not-mapped"),
        pytest.param(
            "src\\pkg\\mod.py\n", None, "codemap-context-missing-for-python-diff", id="windows-separator-diff-missing"
        ),
        pytest.param("tests/test_mod.py\n", None, "codemap-context-missing-for-python-diff", id="test-only-diff"),
        pytest.param(
            '"pkg/mod\\303\\251.py"\n', None, "codemap-context-missing-for-python-diff", id="git-quoted-python-path"
        ),
        pytest.param('"docs/calf\\303\\251.md"\n', None, None, id="git-quoted-non-python-path"),
        pytest.param("src/pkg/mod.py\n", _artifact(), None, id="available-provider-passes"),
        pytest.param("src/pkg/mod.py\n", _artifact("absent"), None, id="absent-provider-passes-with-reason"),
        pytest.param("src/pkg/mod.py\n", _artifact("incompatible"), None, id="pre-query-incompatible-passes"),
        pytest.param(
            "src/pkg/mod.py\n",
            _artifact(
                queries=[_query(error="index is not valid JSON", exit_code=1, answer=None, changed_files=None)],
                status="incompatible",
                reasons=["diff-impact: index is not valid JSON"],
            ),
            None,
            id="post-query-incompatible-with-reason-passes",
        ),
        pytest.param(
            "src/pkg/mod.py\n",
            _artifact(
                queries=[_query(stale=True, not_covered=["dynamic-dispatch"])],
                status="stale+degraded",
                reasons=["diff-impact: not covered: dynamic-dispatch", "diff-impact: stale: index older than source"],
            ),
            None,
            id="composed-status-passes",
        ),
        pytest.param(
            "src/pkg/mod.py\n",
            _artifact(
                queries=[_query(answer=_UNMAPPED_ANSWER, adapter_gap=_UNMAPPED_GAP)],
                status="degraded",
                reasons=[f"diff-impact: {_UNMAPPED_GAP}"],
            ),
            None,
            id="all-unmapped-degraded-passes",
        ),
        pytest.param(
            "src/pkg/mod.py\n",
            _artifact("skipped", query_kind="skip"),
            "codemap-context-skipped-for-python-diff",
            id="skip-rejected",
        ),
        pytest.param(
            "README.md\n",
            _artifact("skipped", query_kind="skip"),
            "codemap-context-invalid:query_kind",
            id="skip-is-never-a-review-route",
        ),
        pytest.param(
            "src/pkg/mod.py\n", _artifact(status="unknown"), "codemap-context-invalid:status", id="unknown-status"
        ),
        pytest.param(
            "src/pkg/mod.py\n",
            _artifact("absent", probe={"status": "absent", "detail": " "}),
            "codemap-context-invalid:probe.detail",
            id="absent-without-reason-rejected",
        ),
        pytest.param(
            "src/pkg/mod.py\n",
            _artifact(protocol_version="codemap-py.integration.v0"),
            "codemap-context-invalid:protocol_version",
            id="foreign-protocol-rejected",
        ),
        pytest.param(
            "src/pkg/mod.py\n",
            _artifact(category="implementation"),
            "codemap-context-invalid:category",
            id="wrong-category-rejected",
        ),
        pytest.param(
            "src/pkg/mod.py\n", "{not json", "codemap-context-invalid:unreadable-json", id="malformed-json-rejected"
        ),
        pytest.param("src/pkg/mod.py\n", "[]", "codemap-context-invalid:not-json-object", id="json-array-rejected"),
    ],
)
def test_python_diff_requires_auditable_context_artifact(
    validator: ModuleType, tmp_path: Path, files: str, artifact: object, expected: str | None
) -> None:
    """A Python diff carries a valid context artifact; provider absence passes only with its recorded reason.

    Each row is one collected changed-file listing plus whatever the probe persisted. The missing-artifact rows
    reproduce observed review runs that skipped the probe for Python changes; the absent and incompatible rows prove the
    provider stays optional. Only paths the provider can map from the diff file trigger the requirement.
    """
    run = _run(tmp_path, files, artifact)

    assert _codemap_failure(validator, run) == expected


def test_untracked_only_python_file_does_not_require_probe(validator: ModuleType, tmp_path: Path) -> None:
    """A Python file listed only in `untracked.txt` is absent from the diff file, so the probe could only map nothing.

    The review's ``diff.patch`` carries tracked changes only; requiring a probe there would demand an answer that is
    always empty rather than evidence about the change.
    """
    run = _run(tmp_path, "README.md\n")
    (run / "untracked.txt").write_text("src/pkg/new_module.py\n", encoding="utf-8")

    assert _codemap_failure(validator, run) is None


@pytest.mark.parametrize(
    ("artifact", "expected"),
    [
        pytest.param(_artifact(queries=[]), "codemap-context-invalid:queries", id="available-without-query"),
        pytest.param(
            _artifact(queries=[_query(), _query()]), "codemap-context-invalid:queries", id="two-diff-impact-queries"
        ),
        pytest.param(
            _artifact("absent", queries=[_query()]), "codemap-context-invalid:queries", id="query-after-absent-probe"
        ),
        pytest.param(_artifact(query_kind="callers"), "codemap-context-invalid:query_kind", id="fact-route-primary"),
        pytest.param(_artifact(diff_file=None), "codemap-context-invalid:diff_file", id="query-without-diff-file"),
        pytest.param(_artifact(diff_file="diff.patch"), "codemap-context-invalid:diff_file", id="relative-diff-file"),
        pytest.param(
            _artifact(diff_file=str(Path(__file__).resolve())),
            "codemap-context-invalid:diff_file",
            id="diff-file-outside-run",
        ),
        pytest.param(
            _artifact(diff_file="<run>/files.txt"), "codemap-context-invalid:diff_file", id="diff-file-is-files-listing"
        ),
        pytest.param(
            _artifact(queries=[_query(root_mismatch=True)]),
            "codemap-context-invalid:status",
            id="available-over-root-mismatch",
        ),
        pytest.param(
            _artifact(queries=[_query(answer=_UNMAPPED_ANSWER)]),
            "codemap-context-invalid:answer",
            id="all-unmapped-without-gap",
        ),
        pytest.param(
            _artifact(queries=[_query(answer=_UNMAPPED_ANSWER, adapter_gap=_UNMAPPED_GAP)], status="degraded"),
            "codemap-context-invalid:status_reasons",
            id="degraded-without-reason",
        ),
        pytest.param(
            _artifact(queries=[_query(error="boom", exit_code=1, answer=None)], status="incompatible"),
            "codemap-context-invalid:status_reasons",
            id="post-query-incompatible-without-reason",
        ),
        pytest.param(
            _artifact(
                queries=[_query(error="diff file required, none supplied", exit_code=-1, answer=None)],
                status="degraded",
                reasons=["diff-impact: diff file required, none supplied"],
                diff_file=None,
            ),
            "codemap-context-invalid:diff_file",
            id="degraded-for-missing-diff-file",
        ),
        pytest.param(
            _artifact(queries=[{"subcommand": "diff-impact"}]),
            "codemap-context-invalid:queries",
            id="malformed-query-record",
        ),
        pytest.param(
            _artifact(status_reasons="none"), "codemap-context-invalid:status_reasons", id="reasons-not-a-list"
        ),
    ],
)
def test_current_artifact_status_must_follow_from_its_recorded_query(
    validator: ModuleType, tmp_path: Path, artifact: dict[str, object], expected: str
) -> None:
    """Reject a current artifact whose route, diff file, gap, status, or reasons disagree with what actually ran.

    The validator re-derives status and reasons from the recorded query with the adapter's own reduction, so a hand-
    written ``available`` cannot stand in for a mismatched, all-unmapped, or failed query.
    """
    run = _run(tmp_path, "src/pkg/mod.py\n", artifact)

    assert _codemap_failure(validator, run) == expected


@pytest.mark.parametrize(
    ("diff", "artifact", "expected"),
    [
        pytest.param(
            _DELETION_DIFF,
            _artifact(
                queries=[_query(changed_files=0, answer=_DELETION_ANSWER, adapter_gap=_DELETION_GAP)],
                status="degraded",
                reasons=[f"diff-impact: {_DELETION_GAP}"],
            ),
            None,
            id="deletion-degraded-with-reason-passes",
        ),
        pytest.param(
            _DELETION_DIFF,
            _artifact(queries=[_query(changed_files=0, answer=_DELETION_ANSWER)]),
            "codemap-context-invalid:answer",
            id="deletion-recorded-as-bare-available",
        ),
        pytest.param(
            _DOCS_DIFF,
            _artifact(queries=[_query(changed_files=0, answer=_DELETION_ANSWER)]),
            "codemap-context-invalid:changed_files",
            id="python-listing-without-python-in-diff",
        ),
    ],
)
def test_zero_changed_python_answer_must_name_its_cause(
    validator: ModuleType, tmp_path: Path, diff: str, artifact: dict[str, object], expected: str | None
) -> None:
    """A Python diff whose probe read zero changed Python files passes only with the reason recorded.

    The provider maps post-images only, so deleting an imported module used to persist ``available`` with ``LOW`` risk
    and no reason. The validator recounts deletions from this run's ``diff.patch``, and a zero answer for a Python
    listing that the diff cannot explain fails even when the recorded status is internally consistent.
    """
    run = _run(tmp_path, "pkg/util.py\n", artifact, diff=diff)

    assert _codemap_failure(validator, run) == expected


@pytest.mark.parametrize(
    ("diff_name", "expected_status", "expected"),
    [
        pytest.param("diff.patch", "degraded", None, id="run-diff-deletion-degrades-and-passes"),
        pytest.param(
            "files.txt", "available", "codemap-context-invalid:diff_file", id="files-listing-as-diff-rejected"
        ),
    ],
)
def test_adapter_written_deletion_artifact_binds_to_run_diff(
    validator: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diff_name: str,
    expected_status: str,
    expected: str | None,
) -> None:
    """The adapter's own deletion-only artifact passes, while the same probe over ``files.txt`` is rejected.

    The provider is replaced in process by its real deletion-only answer, so the artifact is exactly what the shipped
    adapter persists. Pointing ``--diff-file`` at another run file reads as a diff with no Python change, which is how a
    hollow ``available`` used to pass.
    """
    adapter = _module(ADAPTER)
    payload = {**_DELETION_ANSWER, "index": {"query_complete": True, "stale": False, "root_mismatch": False}}
    monkeypatch.setattr(
        adapter,
        "_resolve_codemap_executable",
        lambda: adapter.LauncherResolution("/explicit/codemap-py", adapter.STATUS_AVAILABLE, "test launcher"),
    )
    monkeypatch.setattr(
        adapter, "_run_json", lambda argv, timeout, cwd=None: (0, _DOCTOR if argv[1] == "doctor" else payload, None)
    )
    run = _run(tmp_path, "pkg/util.py\n", diff=_DELETION_DIFF)
    artifact = adapter.gather_structural_context("review", diff_file=run / diff_name).to_dict()
    (run / "codemap-context.json").write_text(json.dumps(artifact), encoding="utf-8")

    failure = _codemap_failure(validator, run)

    assert (artifact["status"], failure) == (expected_status, expected)


_INDEX_V2_ERROR = "Index is v2: call graph requires v3"
_AMBIGUOUS_ERROR = "Symbol 'build' is ambiguous: 2 indexed symbols or modules match that name."
_AMBIGUOUS_ANSWER = {"candidates": ["pkg.a::build", "pkg.b::build"], "candidate_count": 2, "rejected_target": "build"}


def _failed_query(subcommand: str, exit_code: int, error: str, answer: object, rejected: bool) -> dict[str, object]:
    """Return one failed query record the way the adapter persists it, with the ``input_rejected`` flag supplied."""
    return _query(
        subcommand=subcommand,
        target=None if subcommand == "diff-impact" else "build",
        exit_code=exit_code,
        error=error,
        answer=answer,
        input_rejected=rejected,
        query_complete=False,
        changed_files=None,
    )


def _failed_follow_up(exit_code: int, error: str, answer: object, rejected: bool, status: str) -> dict[str, object]:
    """Return one ``callers`` follow-up artifact whose only query failed with the given provider evidence."""
    record = _failed_query("fn-rdeps", exit_code, error, answer, rejected)
    return {
        **_follow_up(target="build"),
        "queries": [record],
        "status": status,
        "status_reasons": [f"fn-rdeps: {error}"],
    }


@pytest.mark.parametrize(
    ("artifact", "follow_ups", "expected"),
    [
        pytest.param(
            _artifact(
                queries=[_failed_query("diff-impact", 1, _INDEX_V2_ERROR, {}, rejected=False)],
                status="incompatible",
                reasons=[f"diff-impact: {_INDEX_V2_ERROR}"],
            ),
            {},
            None,
            id="honest-provider-side-failure-is-incompatible",
        ),
        pytest.param(
            _artifact(
                queries=[_failed_query("diff-impact", 1, _INDEX_V2_ERROR, {}, rejected=True)],
                status="degraded",
                reasons=[f"diff-impact: {_INDEX_V2_ERROR}"],
            ),
            {},
            "codemap-context-invalid:input_rejected",
            id="forged-provider-side-failure-as-rejected-input",
        ),
        pytest.param(
            _artifact(
                queries=[
                    _failed_query("diff-impact", 2, "diff file unreadable", {"path": "/gone.patch"}, rejected=True)
                ],
                status="degraded",
                reasons=["diff-impact: diff file unreadable"],
            ),
            {},
            None,
            id="honest-provider-diagnosed-exit-2",
        ),
        pytest.param(
            _artifact(
                queries=[_failed_query("diff-impact", 2, "usage: codemap-py query [-h]", None, rejected=True)],
                status="degraded",
                reasons=["diff-impact: usage: codemap-py query [-h]"],
            ),
            {},
            "codemap-context-invalid:input_rejected",
            id="forged-usage-exit-2-as-rejected-input",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": _failed_follow_up(1, _AMBIGUOUS_ERROR, _AMBIGUOUS_ANSWER, True, "degraded")},
            None,
            id="honest-rejected-target-follow-up",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": _failed_follow_up(1, _AMBIGUOUS_ERROR, _AMBIGUOUS_ANSWER, False, "incompatible")},
            "codemap-followup-invalid:sw-engineer-01.json:input_rejected",
            id="rejected-target-follow-up-recorded-as-provider-failure",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": _failed_follow_up(1, _INDEX_V2_ERROR, {}, True, "degraded")},
            "codemap-followup-invalid:sw-engineer-01.json:input_rejected",
            id="forged-provider-side-follow-up-as-rejected-input",
        ),
    ],
)
def test_recorded_input_rejection_must_follow_from_exit_code_and_answer(
    validator: ModuleType,
    tmp_path: Path,
    artifact: dict[str, object],
    follow_ups: dict[str, object],
    expected: str | None,
) -> None:
    """A query's ``input_rejected`` flag is re-derived from its exit code and recorded answer, never trusted.

    The flag alone keeps a batch with no successful query ``degraded`` instead of ``incompatible``, and so decides
    whether follow-ups may run. A provider-side failure (an exit 1 index error, or an argument-parser usage exit 2)
    hand-marked as rejected input passed as ``degraded`` and admitted follow-ups after an unusable probe; the honest
    rows show a target the provider refused, or input it diagnosed itself, still degrades and passes.
    """
    run = _run(tmp_path, "src/pkg/mod.py\n", artifact, follow_ups)

    assert _codemap_failure(validator, run) == expected


def test_volunteered_probe_on_non_python_review_must_read_run_diff(validator: ModuleType, tmp_path: Path) -> None:
    """A probe a non-Python review ran anyway is optional, but once run it must still read this run's diff file.

    The distinct code tells the reviewer the probe itself was not required, only its evidence was malformed.
    """
    run = _run(tmp_path, "README.md\n", _artifact(diff_file=None))

    assert _codemap_failure(validator, run) == "codemap-context-invalid:diff_file:volunteered-probe"


@pytest.mark.parametrize(
    ("artifact", "follow_ups", "expected"),
    [
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": _follow_up(), "sw-engineer-03.json": _follow_up("test-impact", "pkg.mod")},
            None,
            id="bounded-follow-ups-pass",
        ),
        pytest.param(
            _artifact(
                queries=[_query(answer=_UNMAPPED_ANSWER, adapter_gap=_UNMAPPED_GAP)],
                status="degraded",
                reasons=[f"diff-impact: {_UNMAPPED_GAP}"],
            ),
            {"qa-specialist-01.json": _follow_up("dependencies", "pkg.mod")},
            None,
            id="degraded-probe-allows-follow-up",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-04.json": _follow_up()},
            "codemap-followup-name-invalid:sw-engineer-04.json",
            id="fourth-follow-up-rejected",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-00.json": _follow_up()},
            "codemap-followup-name-invalid:sw-engineer-00.json",
            id="zero-sequence-rejected",
        ),
        pytest.param(
            _artifact(),
            {"notes.json": _follow_up()},
            "codemap-followup-name-invalid:notes.json",
            id="unnumbered-file-rejected",
        ),
        pytest.param(
            _artifact(),
            {"security-x-01.json": _follow_up()},
            "codemap-followup-role-unknown:security-x-01.json",
            id="renamed-role-cannot-buy-queries",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": _follow_up("blast")},
            "codemap-followup-invalid:sw-engineer-01.json:query_kind",
            id="transitive-route-rejected",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": _follow_up(target=None)},
            "codemap-followup-invalid:sw-engineer-01.json:target",
            id="untargeted-follow-up-rejected",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": {**_follow_up(), "category": "implementation"}},
            "codemap-followup-invalid:sw-engineer-01.json:category",
            id="category-mismatch-rejected",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": {**_follow_up(), "queries": [_query(subcommand="rdeps", target="pkg.mod::fn")]}},
            "codemap-followup-invalid:sw-engineer-01.json:queries",
            id="follow-up-ran-another-route",
        ),
        pytest.param(
            _artifact(),
            {"sw-engineer-01.json": {**_follow_up(), "status": "degraded"}},
            "codemap-followup-invalid:sw-engineer-01.json:status",
            id="follow-up-status-contradicts-query",
        ),
        pytest.param(
            _artifact("absent"),
            {"sw-engineer-01.json": _follow_up()},
            "codemap-followup-after-unusable-probe:absent",
            id="follow-up-after-absent-provider-rejected",
        ),
        pytest.param(
            _artifact("incompatible"),
            {"sw-engineer-01.json": _follow_up()},
            "codemap-followup-after-unusable-probe:incompatible",
            id="incompatible-provider-never-retried",
        ),
        pytest.param(
            None,
            {"sw-engineer-01.json": _follow_up()},
            "codemap-context-missing-for-python-diff",
            id="follow-up-without-primary-probe-rejected",
        ),
    ],
)
def test_specialist_follow_ups_stay_within_roles_routes_and_limit(
    validator: ModuleType, tmp_path: Path, artifact: object, follow_ups: dict[str, object], expected: str | None
) -> None:
    """Follow-ups bind to a triggered role, use fact routes, sequence 01-03 per role, and follow a usable probe.

    The role in each name must be one this review's routing triggered, so inventing role names cannot multiply the per-
    specialist limit, and each follow-up's own status must follow from the query it records.
    """
    run = _run(tmp_path, "src/pkg/mod.py\n", artifact, follow_ups)

    assert _codemap_failure(validator, run) == expected


def test_follow_up_without_primary_artifact_fails_for_non_python_diff(validator: ModuleType, tmp_path: Path) -> None:
    """A follow-up directory needs the persisted probe even when the diff holds no Python file.

    Follow-ups only answer what the primary probe left open, so one with no primary artifact has no settled baseline.
    """
    run = _run(tmp_path, "README.md\n", None, {"sw-engineer-01.json": _follow_up()})

    assert _codemap_failure(validator, run) == "codemap-followup-without-context"


def test_real_adapter_absent_artifact_satisfies_requirement(validator: ModuleType, tmp_path: Path) -> None:
    """The artifact the shipped adapter writes when Codemap is absent passes the Python-diff requirement.

    This drives the real adapter CLI with no launcher on PATH and no explicit launcher, so the accepted reason text is
    exactly what a review run without Codemap persists.
    """
    run = tmp_path / "run"
    run.mkdir()
    _run(run, "src/pkg/mod.py\n")
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "CODEMAP_BIN"}
    env["PATH"] = str(empty_path)
    command = [sys.executable, str(ADAPTER), "context", "--category", "review"]
    command += ["--diff-file", str(run / "diff.patch"), "--out", str(run / "codemap-context.json")]

    completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)

    assert completed.returncode == 0, completed.stderr
    assert json.loads((run / "codemap-context.json").read_text(encoding="utf-8"))["status"] == "absent"
    assert _codemap_failure(validator, run) is None


def _candidate_codemap_errors(validator: ModuleType, run: Path, result_name: str) -> list[dict[str, str]]:
    """Run every review check against one result file and return only the codemap check's failures."""
    result_path = run / result_name
    if result_name != "result.json":
        shutil.copyfile(run / "result.json", result_path)
    report = validator.collect_result_errors(run, result_path, run, "thread", run)
    return [error for error in report["errors"] if error["step"] == "codemap-context"]


def test_fresh_candidate_for_python_diff_fails_without_context(validator: ModuleType, assessed_review: Path) -> None:
    """A fresh assessed candidate for a Python diff is rejected by the full review check list.

    Starting from a complete assessed review, the diff listing gains a Python module and no probe artifact exists; the
    registered codemap step must report the missing artifact before promotion.
    """
    (assessed_review / "files.txt").write_text("widget.txt\nsrc/widget.py\n", encoding="utf-8")

    errors = _candidate_codemap_errors(validator, assessed_review, "result.candidate.json")

    assert errors == [{"step": "codemap-context", "code": "codemap-context-missing-for-python-diff"}]


def test_fresh_candidate_for_python_diff_passes_with_absent_provider(
    validator: ModuleType, assessed_review: Path
) -> None:
    """An absent-provider artifact clears the codemap step of the full review check list.

    The same Python diff passes the codemap step once the probe persisted its `absent` status and reason.
    """
    (assessed_review / "files.txt").write_text("widget.txt\nsrc/widget.py\n", encoding="utf-8")
    (assessed_review / "codemap-context.json").write_text(json.dumps(_artifact("absent")), encoding="utf-8")

    errors = _candidate_codemap_errors(validator, assessed_review, "result.candidate.json")

    assert errors == []


def test_promoted_historical_result_stays_readable_without_context(
    validator: ModuleType, assessed_review: Path
) -> None:
    """A promoted `result.json` predating the requirement is not re-judged by it.

    Prior completed reviews are revalidated for reuse lookup; applying the new requirement there would make every
    earlier Python-diff review unreadable rather than only blocking new candidates.
    """
    (assessed_review / "files.txt").write_text("widget.txt\nsrc/widget.py\n", encoding="utf-8")

    errors = _candidate_codemap_errors(validator, assessed_review, "result.json")

    assert errors == []


@pytest.mark.parametrize(
    ("recovery", "expected"),
    [
        pytest.param(None, "codemap-context-invalid:artifact_schema_version", id="new-run-rejects-schema-3"),
        pytest.param("full", None, id="recovered-run-keeps-schema-3"),
        pytest.param("marker-only", "codemap-context-invalid:artifact_schema_version", id="bare-marker-is-not-proof"),
    ],
)
def test_pre_diff_file_schema_is_readable_only_for_recovered_runs(
    validator: ModuleType, tmp_path: Path, recovery: str | None, expected: str | None
) -> None:
    """The pre-diff-file schema is accepted only beside both files the recovery helper writes.

    That schema ran ``diff-impact`` against the worktree's own ``HEAD`` and could report zero changed files as complete,
    so a new run must carry the current schema.
    """
    run = _run(tmp_path, "src/pkg/mod.py\n", _historical_artifact())
    if recovery is not None:
        _mark_recovered(run, candidate=recovery == "full")

    assert _codemap_failure(validator, run) == expected


@pytest.mark.parametrize(
    ("metadata", "recovery", "artifact", "expected"),
    [
        pytest.param(
            {"codemap_context": "not-required-historical"}, "full", None, None, id="recovered-run-records-exemption"
        ),
        pytest.param(
            {"codemap_context": "not-required-historical"},
            None,
            None,
            "codemap-context-historical-exemption-unproven",
            id="exemption-without-recovery-marker",
        ),
        pytest.param(
            {"codemap_context": "not-required-historical"},
            "marker-only",
            None,
            "codemap-context-historical-exemption-unproven",
            id="exemption-without-recovery-candidate",
        ),
        pytest.param(
            {"codemap_context": "not-required-historical"},
            "full",
            _artifact(),
            "codemap-context-historical-exemption-contradicted",
            id="exemption-beside-real-probe",
        ),
        pytest.param(
            {"codemap_context": "skipped"},
            "full",
            None,
            "codemap-context-metadata-invalid",
            id="unknown-exemption-value",
        ),
        pytest.param(
            {}, "full", None, "codemap-context-missing-for-python-diff", id="recovered-run-must-claim-exemption"
        ),
    ],
)
def test_recovered_pre_rule_run_needs_explicit_historical_exemption(
    validator: ModuleType,
    tmp_path: Path,
    metadata: dict[str, object],
    recovery: str | None,
    artifact: dict[str, object] | None,
    expected: str | None,
) -> None:
    """A recovered pre-rule run is exempt only when the result says so and the recovery helper's files prove it.

    The adapter is never run retroactively for such a run; the exemption is never inferred from the marker alone and
    never accepted for a run the recovery helper did not restore.
    """
    run = _run(tmp_path, "src/pkg/mod.py\n", artifact)
    if recovery is not None:
        _mark_recovered(run, candidate=recovery == "full")

    assert _codemap_failure(validator, run, metadata) == expected


def test_historical_exemption_passes_the_full_review_check_list(validator: ModuleType, assessed_review: Path) -> None:
    """The exemption recorded in candidate metadata clears the codemap step of the full review check list.

    This mirrors a recovered historical review: a Python diff, no probe artifact, the recovery helper's files, and the
    explicit ``not-required-historical`` reason in the candidate's metadata.
    """
    (assessed_review / "files.txt").write_text("widget.txt\nsrc/widget.py\n", encoding="utf-8")
    _mark_recovered(assessed_review)
    result = json.loads((assessed_review / "result.json").read_text(encoding="utf-8"))
    result["metadata"]["codemap_context"] = "not-required-historical"
    candidate = assessed_review / "result.candidate.json"
    candidate.write_text(json.dumps(result), encoding="utf-8")

    report = validator.collect_result_errors(assessed_review, candidate, assessed_review, "thread", assessed_review)

    assert [error for error in report["errors"] if error["step"] == "codemap-context"] == []
