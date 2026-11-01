"""Regression checks for selecting assessed code-review reports for remediation.

Covers the standalone report finder (`shared/find-review-report.py`): PR-scoped and flat run discovery, blocking by
newer unfinished or damaged runs, explicit-result intake with producer validation, and per-finding evidence lookup. The
CLI and the artifact-producing fixtures run for real; only report layouts built by hand are synthetic.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _artifact_files import read_json
from test_review_completion_gate import _assessed_pr, _module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
FINDER_PATH = PLUGIN_ROOT / "shared" / "find-review-report.py"
CREATE_RUN_PATH = PLUGIN_ROOT / "shared" / "create_run.py"
PR_URL = "https://github.com/acme/widgets/pull/123"
INVALID = "invalid-review-report-rerun-code-review"
INCOMPLETE = "matching-review-incomplete:"
UNPROMOTED = "matching-review-candidate-unpromoted"
CLOSED = "matching-review-closed-not-remediable"
UNAVAILABLE = "matching-review-unavailable-rerun-code-review"
MISSING = "missing-matching-review-report"
LOCAL_SCOPES = pytest.mark.parametrize("scope", ["working-tree", "path", "commit"])
BOTH_TARGETS = pytest.mark.parametrize("target", ["123", PR_URL])
MALFORMED_TARGETS = ["malformed-target", "https://example.com/acme/widgets/pull/123", PR_URL.replace("pull", "PULL")]


def _load_finder() -> Any:
    """Load the standalone report finder from its shipped plugin path."""
    specification = importlib.util.spec_from_file_location("find_review_report", FINDER_PATH)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def finder() -> Any:
    """Fresh copy of the standalone report finder."""
    return _load_finder()


def _run_finder(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the finder CLI in a real subprocess and capture its text output."""
    return subprocess.run([sys.executable, str(FINDER_PATH), *args], capture_output=True, text=True, check=False)


def _write_files(directory: Path, files: dict[str, str]) -> None:
    """Write each named text file into an existing directory."""
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")


def _identity(number: int = 123, url: str = PR_URL) -> str:
    """Return the JSON text of a collected `pr.json` identity."""
    return json.dumps({"number": number, "url": url})


def _assessed_result_text(scope: str = "pr", **metadata: object) -> str:
    """Return the JSON text of a minimal assessed result for the given scope."""
    return json.dumps(
        {"metadata": {"scope": scope, "review_decision": {"recommendation": "needs-more-work"}, **metadata}}
    )


def _write_report(root: Path, timestamp: str, *, unavailable: bool, number: int = 123, url: str = PR_URL) -> Path:
    """Create a minimal flat PR-identified report; `unavailable` makes it a terminal collection failure."""
    report_dir = root / timestamp
    report_dir.mkdir()
    if unavailable:
        (report_dir / "pr-target.txt").write_text(PR_URL + "\n", encoding="utf-8")
    else:
        (report_dir / "pr.json").write_text(_identity(number, url), encoding="utf-8")
    metadata: dict[str, object] = {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}
    if unavailable:
        metadata["review_status"] = "unavailable"
    result_path = report_dir / "result.json"
    result_path.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    return result_path


def _write_unavailable_report(root: Path, timestamp: str) -> Path:
    """Create a flat report recording a terminal collection failure."""
    return _write_report(root, timestamp, unavailable=True)


def _write_malformed_report(root: Path, timestamp: str) -> Path:
    """Create a flat report whose result file is not JSON."""
    report_dir = root / timestamp
    report_dir.mkdir()
    result_path = report_dir / "result.json"
    result_path.write_text("not-json", encoding="utf-8")
    return result_path


def _write_closed_report(root: Path, timestamp: str) -> Path:
    """Create a PR-identified terminal close result that remediation must not consume."""
    report_dir = root / timestamp
    report_dir.mkdir()
    (report_dir / "pr.json").write_text(_identity(), encoding="utf-8")
    result_path = report_dir / "result.json"
    result_path.write_text(
        json.dumps({"metadata": {"scope": "pr", "review_status": "closed", "close_decision": {"code": "DUPLICATE"}}}),
        encoding="utf-8",
    )
    return result_path


def _write_candidate_report(root: Path, timestamp: str, *, pull_number: int = 123) -> Path:
    """Create an assessed PR candidate that has not passed artifact validation."""
    report_dir = root / timestamp
    report_dir.mkdir()
    (report_dir / "pr.json").write_text(
        _identity(pull_number, f"https://github.com/acme/widgets/pull/{pull_number}"), encoding="utf-8"
    )
    candidate_path = report_dir / "result.candidate.json"
    candidate_path.write_text(_assessed_result_text(), encoding="utf-8")
    return candidate_path


def _write_nested_report(
    root: Path,
    run_name: str,
    *,
    pull_number: int = 123,
    result_name: str = "result.json",
    review_status: str | None = None,
    result_text: str | None = None,
) -> Path:
    """Create one report in the PR-scoped, numerically ordered run topology.

    `result_text` replaces the default assessed result so a caller can store damaged content from the start.
    """
    report_dir = root / f"pr-{pull_number}" / run_name
    report_dir.mkdir(parents=True)
    (report_dir / "pr.json").write_text(
        _identity(pull_number, f"https://github.com/acme/widgets/pull/{pull_number}"), encoding="utf-8"
    )
    metadata: dict[str, object] = {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}
    if review_status is not None:
        metadata["review_status"] = review_status
    result_path = report_dir / result_name
    result_path.write_text(json.dumps({"metadata": metadata}) if result_text is None else result_text, encoding="utf-8")
    return result_path


def _drop_identity(result: Path) -> None:
    """Delete the `pr.json` identity beside a result."""
    (result.parent / "pr.json").unlink()


def _overwrite_identity(text: str) -> Callable[[Path], None]:
    """Return a damage step that replaces the `pr.json` identity beside a result with the given text."""

    def damage(result: Path) -> None:
        (result.parent / "pr.json").write_text(text, encoding="utf-8")

    return damage


def _overwrite_result(text: str) -> Callable[[Path], None]:
    """Return a damage step that replaces a result file's content with the given text."""

    def damage(result: Path) -> None:
        result.write_text(text, encoding="utf-8")

    return damage


def _render_and_bind(run: Path, handoff: dict, result: dict) -> None:
    """Write an edited handoff, re-render it, and copy the fresh digests into the result's final-handoff binding."""
    handoff_path = run / "final-handoff.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, run / "final.md", run / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update(
        handoff_sha256=validation["handoff_sha256"], rendered_sha256=validation["rendered_sha256"]
    )


@pytest.fixture(name="assessed_local")
def _assessed_local(tmp_path: Path) -> Path:
    """Build a complete local artifact using the existing real producer fixture."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result_path = run / "result.json"
    result = read_json(result_path)
    result["metadata"]["scope"] = "working-tree"
    commit = result["metadata"]["reviewed_commit_sha"]
    (run / "reviewed-commit.txt").write_text(commit + "\n", encoding="utf-8", newline="\n")
    handoff = read_json(run / "final-handoff.json")
    snapshot = handoff["tables"][0]
    snapshot["heading"] = "Review Snapshot"
    snapshot["rows"][0]["cells"] = ["Scope", "working-tree"]
    snapshot["rows"][1]["cells"] = ["Revision", f"diff sha256:{result['metadata']['review_input_sha256']}"]
    snapshot["rows"][2]["cells"] = ["CI", "unavailable"]
    snapshot["rows"][3]["cells"] = ["Type", "docs"]
    snapshot["rows"][-2]["cells"] = ["Commit", commit + " (working-tree base)"]
    _render_and_bind(run, handoff, result)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    completed = _run_finder("--complete-run", str(run), "--parent-thread-id", "thread")
    assert completed.returncode == 0, completed.stderr
    return result_path


def _downgrade_to_schema_two(result_path: Path) -> None:
    """Rewrite a schema-v3 local result as a schema-v2 archive: no reviewer assessments, v2 handoff presentation."""
    run = result_path.parent
    result = read_json(result_path)
    result["schema_version"] = 2
    del result["metadata"]["reviewer_assessments"]
    handoff = read_json(run / "final-handoff.json")
    handoff["presentation_version"] = 2
    del handoff["tables"][0]["reviewers"]
    _render_and_bind(run, handoff, result)
    result_path.write_text(json.dumps(result), encoding="utf-8")


def _mark_draft(result_path: Path) -> None:
    """Mark the result as an unfinished draft."""
    result = read_json(result_path)
    result["metadata"]["review_status"] = "draft"
    result_path.write_text(json.dumps(result), encoding="utf-8")


def _drop_final_handoff_binding(result_path: Path) -> None:
    """Remove the result's final-handoff binding."""
    result = read_json(result_path)
    del result["metadata"]["final_handoff"]
    result_path.write_text(json.dumps(result), encoding="utf-8")


def _replace_final_text(result_path: Path) -> None:
    """Replace the rendered final text with unvalidated prose."""
    (result_path.parent / "final.md").write_text("Unvalidated replacement final.\n", encoding="utf-8")


def _replace_diff(result_path: Path) -> None:
    """Replace the reviewed diff with a different one."""
    (result_path.parent / "diff.patch").write_text("Unreviewed replacement diff.\n", encoding="utf-8")


def _change_manifest_parent(result_path: Path) -> None:
    """Record a different producing thread in the specialist manifest."""
    manifest_path = result_path.parent / "specialist-manifest.json"
    manifest = read_json(manifest_path)
    manifest["parent_thread_id"] = "different-producer"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


class TestBrokenReports:
    """A damaged review artifact keeps its recovery anchor but never blocks lookups for other PRs."""

    @pytest.mark.parametrize(
        "broken_result",
        [
            pytest.param(
                json.dumps(
                    {
                        "metadata": {
                            "scope": "pr",
                            "review_decision": {"recommendation": None},
                            "review_validation": None,
                        }
                    }
                ).encode(),
                id="null-review-validation",
            ),
            pytest.param(
                json.dumps(
                    {"metadata": {"scope": "pr", "review_decision": {"recommendation": None}, "review_validation": []}}
                ).encode(),
                id="list-review-validation",
            ),
            pytest.param(
                json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": []}}}).encode(),
                id="unhashable-recommendation",
            ),
            pytest.param(b"\xff", id="non-utf8-result"),
            pytest.param(b"[" * 1500 + b"]" * 1500, id="excessively-nested-result"),
        ],
    )
    def test_cli_names_broken_review_and_ignores_unrelated_pr(self, tmp_path: Path, broken_result: bytes) -> None:
        """Broken review artifacts retain their recovery anchor without blocking other PRs.

        The newest run for PR 123 is unreadable in one of several ways. The CLI, run for real, reports that run for PR
        123 and for the explicit path, and reports nothing found for PR 456.
        """
        older = _write_nested_report(tmp_path, "run-001")
        newer = _write_nested_report(tmp_path, "run-002")
        newer.write_bytes(broken_result)

        matching = _run_finder("--reports-dir", str(tmp_path), "--target", "123")
        unrelated = _run_finder("--reports-dir", str(tmp_path), "--target", "456")
        explicit = _run_finder("--result", str(newer))

        assert older.is_file()
        assert (matching.returncode, matching.stderr.strip()) == (1, f"{INVALID}:{newer}")
        assert (unrelated.returncode, unrelated.stderr.strip()) == (1, MISSING)
        assert (explicit.returncode, explicit.stderr.strip()) == (1, f"{INVALID}:{newer}")

    def test_cli_damaged_pr_identity_and_target_leave_other_pr_unblocked(self, tmp_path: Path) -> None:
        """A non-UTF-8 identity is invalid for its directory but does not poison unrelated lookups.

        Both `pr.json` and `pr-target.txt` of the newest PR 123 run hold undecodable bytes.
        """
        newer = _write_nested_report(tmp_path, "run-002")
        (newer.parent / "pr.json").write_bytes(b"\xff")
        (newer.parent / "pr-target.txt").write_bytes(b"\xff")

        matching = _run_finder("--reports-dir", str(tmp_path), "--target", "123")
        unrelated = _run_finder("--reports-dir", str(tmp_path), "--target", "456")

        assert (matching.returncode, matching.stderr.strip()) == (1, f"{INVALID}:{newer}")
        assert (unrelated.returncode, unrelated.stderr.strip()) == (1, MISSING)

    def test_broken_review_does_not_hide_another_pr_review(self, finder: Any, tmp_path: Path) -> None:
        """A malformed result for one PR cannot prevent discovery of another PR's result.

        PR 123 has a result with null review validation; PR 456 has a good one, and the lookup for 456 finds it.
        """
        _write_nested_report(
            tmp_path,
            "run-002",
            result_text=json.dumps(
                {"metadata": {"scope": "pr", "review_decision": {"recommendation": None}, "review_validation": None}}
            ),
        )
        other = _write_nested_report(tmp_path, "run-001", pull_number=456)

        assert finder.find_latest_review_report("456", [tmp_path]) == other

    @BOTH_TARGETS
    @pytest.mark.parametrize("url", ["not-a-pr-url", "https://github.com/acme/widgets/pull/456"])
    def test_malformed_newer_pr_url_cannot_revive_older_review(
        self, finder: Any, tmp_path: Path, target: str, url: str
    ) -> None:
        """A newer run with a damaged URL remains the recovery anchor for its PR.

        The newer run's URL is unparsable or names another PR. The library and the real CLI both refuse to fall back to
        the older assessed run and point at the newer one.
        """
        _write_nested_report(tmp_path, "run-001")
        newer = _write_nested_report(tmp_path, "run-002")
        (newer.parent / "pr.json").write_text(json.dumps({"number": 123, "url": url}), encoding="utf-8")

        with pytest.raises(LookupError, match=INVALID):
            finder.find_latest_review_report(target, [tmp_path])
        result = _run_finder("--reports-dir", str(tmp_path), "--target", target)

        assert result.returncode == 1
        assert result.stderr.strip() == f"{INVALID}:{newer}"

    def test_different_repository_url_does_not_block_matching_pr(self, finder: Any, tmp_path: Path) -> None:
        """A valid different repository identity is not a damaged identity for the requested PR.

        Run 2 belongs to `other/widgets` PR 123; a lookup by the `acme/widgets` URL keeps using run 1.
        """
        expected = _write_nested_report(tmp_path, "run-001")
        other = _write_nested_report(tmp_path, "run-002")
        (other.parent / "pr.json").write_text(
            _identity(url="https://github.com/other/widgets/pull/123"), encoding="utf-8"
        )

        assert finder.find_latest_review_report(PR_URL, [tmp_path]) == expected

    @BOTH_TARGETS
    @pytest.mark.parametrize("url", ["not-a-pr-url", "https://github.com/acme/widgets/pull/456"])
    def test_flat_damaged_identity_blocks_older_review(
        self, finder: Any, tmp_path: Path, target: str, url: str
    ) -> None:
        """A flat report's retained number identifies a damaged newer review as the recovery anchor.

        The newer flat run keeps number 123 but its URL is unparsable or points at PR 456.
        """
        _write_report(tmp_path, "2026-01-01", unavailable=False)
        _write_report(tmp_path, "2026-02-01", unavailable=False, url=url)

        with pytest.raises(LookupError, match=INVALID):
            finder.find_latest_review_report(target, [tmp_path])

    @BOTH_TARGETS
    def test_flat_conflicting_identity_uses_recorded_target_for_fresh_review(
        self, finder: Any, tmp_path: Path, target: str
    ) -> None:
        """A damaged newer identity cannot hide its recorded PR target and revive older findings.

        The newer run's `pr.json` names PR 456 with an invalid URL, but its `pr-target.txt` records PR 123.
        """
        _write_report(tmp_path, "2026-01-01", unavailable=False)
        newer = _write_report(tmp_path, "2026-02-01", unavailable=False, number=456, url="invalid-url")
        (newer.parent / "pr-target.txt").write_text(PR_URL + "\n", encoding="utf-8")

        with pytest.raises(LookupError, match=INVALID) as error:
            finder.find_latest_review_report(target, [tmp_path])

        assert str(error.value) == f"{INVALID}:{newer}"


class TestPrScopedReviewRuns:
    """Discovery and terminal ordering for PR-scoped review runs."""

    @pytest.mark.parametrize(
        ("stored_url", "target"),
        [
            pytest.param(
                "https://github.com/Acme/Widgets/pull/123",
                "https://github.com/acme/widgets/pull/123",
                id="collected-name-casing",
            ),
            pytest.param(
                "https://github.com/acme/widgets/pull/123",
                "HTTPS://GITHUB.COM/ACME/WIDGETS/pull/123",
                id="requested-url-casing",
            ),
        ],
    )
    def test_url_casing_selects_newest_same_pr(self, finder: Any, tmp_path: Path, stored_url: str, target: str) -> None:
        """GitHub identity casing cannot hide the latest assessed review.

        The stored and requested URLs differ only in letter case; the newest run for the same PR is still chosen.
        """
        _write_nested_report(tmp_path, "run-001")
        newest = _write_nested_report(tmp_path, "run-002")
        (newest.parent / "pr.json").write_text(_identity(url=stored_url), encoding="utf-8")

        assert finder.find_latest_review_report(target, [tmp_path]) == newest

    @pytest.mark.parametrize(
        ("identity", "extra_files", "diagnostic"),
        [
            pytest.param(
                {"number": 123, "url": "https://github.com/Acme/Widgets/pull/123"}, {}, INCOMPLETE, id="identity"
            ),
            pytest.param({"number": 123}, {}, INCOMPLETE, id="partial-identity"),
            pytest.param(
                {"number": 123, "url": "https://github.com/Acme/Widgets/pull/123"},
                {"result.candidate.json": "{}"},
                f"{UNPROMOTED}:",
                id="candidate",
            ),
        ],
    )
    def test_url_casing_keeps_newer_pending_run_blocking(
        self, finder: Any, tmp_path: Path, identity: dict, extra_files: dict[str, str], diagnostic: str
    ) -> None:
        """Equivalent URL spelling cannot revive findings after a newer review starts.

        The request is spelled in upper case; run 2 has only an identity (full or partial) or an unpromoted candidate.
        """
        _write_nested_report(tmp_path, "run-001")
        pending = tmp_path / "pr-123" / "run-002"
        pending.mkdir()
        _write_files(pending, {"pr.json": json.dumps(identity), **extra_files})

        with pytest.raises(LookupError, match=diagnostic) as error:
            finder.find_latest_review_report("https://GITHUB.COM/ACME/WIDGETS/pull/123", [tmp_path])

        assert str(pending) in str(error.value)

    def test_url_casing_keeps_newer_flat_target_blocking(self, finder: Any, tmp_path: Path) -> None:
        """Equivalent URL spelling cannot revive flat findings after a newer collection starts.

        The newer flat run recorded only its `pr-target.txt` in lower case; the request is spelled in upper case.
        """
        _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
        pending = tmp_path / "2026-08-10T11-00-00Z"
        pending.mkdir()
        (pending / "pr-target.txt").write_text(PR_URL, encoding="utf-8")

        with pytest.raises(LookupError, match=INCOMPLETE) as error:
            finder.find_latest_review_report("https://GITHUB.COM/ACME/WIDGETS/pull/123", [tmp_path])

        assert str(pending) in str(error.value)

    @pytest.mark.parametrize(
        "target",
        [
            "https://example.com/acme/widgets/pull/123",
            "https://github.com/other/widgets/pull/123",
            "https://github.com/acme/other/pull/123",
            "https://github.com/acme/widgets/pull/456",
            "https://github.com/acme/widgets/PULL/123",
            "https://github.com/acme/widgets/pull/0123",
            "https://github.com/acme/widgets/pull/123?query=1",
            "https://github.com/acme/widgets/pull/123#fragment",
            "https://github.com:443/acme/widgets/pull/123",
            "https://user@github.com/acme/widgets/pull/123",
            "https://github.com/ac%6de/widgets/pull/123",
            "https://github.com/acme/widgets/pull/123/extra",
        ],
    )
    def test_url_identity_rejects_different_or_noncanonical_target(
        self, finder: Any, tmp_path: Path, target: str
    ) -> None:
        """Casing tolerance preserves canonical URL structure and repository identity.

        Other hosts, repositories, PR numbers, path forms, ports, credentials, queries and escapes never match run 1.
        """
        _write_nested_report(tmp_path, "run-001")

        with pytest.raises(LookupError, match=MISSING):
            finder.find_latest_review_report(target, [tmp_path])

    def test_selects_nested_assessed_result(self, finder: Any, tmp_path: Path) -> None:
        """Discover assessed output below the explicit PR and run directories.

        A single run under `pr-123/run-001` is found by the `#123` spelling.
        """
        assessed = _write_nested_report(tmp_path, "run-001")

        assert finder.find_latest_review_report("#123", [tmp_path]) == assessed

    @pytest.mark.parametrize(
        ("files", "target"),
        [
            pytest.param(
                {"pr.json": json.dumps({"number": 123}), "review-notes.md": "Preliminary findings; promotion failed."},
                "123",
                id="notes-without-result",
            ),
            pytest.param({"pr.json": _identity()}, "123", id="promoted-before-notes"),
            pytest.param({"pr.json": json.dumps({"number": 123})}, PR_URL, id="partial-identity-url-lookup"),
        ],
    )
    def test_newer_unfinished_run_blocks_older_review(
        self, finder: Any, tmp_path: Path, files: dict[str, str], target: str
    ) -> None:
        """Retained preliminary evidence or a started run must not disappear behind an older verdict.

        Run 2 has an identity, with or without notes, but no result yet; the older assessed run 1 stays hidden.
        """
        _write_nested_report(tmp_path, "run-001")
        pending = tmp_path / "pr-123" / "run-002"
        pending.mkdir()
        _write_files(pending, files)

        with pytest.raises(LookupError, match=INCOMPLETE) as error:
            finder.find_latest_review_report(target, [tmp_path])

        assert str(pending) in str(error.value)

    @pytest.mark.parametrize(
        ("artifact_name", "damage"),
        [
            pytest.param("result.candidate.json", _drop_identity, id="candidate-missing-identity"),
            pytest.param("result.candidate.json", _overwrite_identity("{"), id="candidate-malformed-identity"),
            pytest.param(
                "result.candidate.json", _overwrite_identity('{"number": 456}'), id="candidate-conflicting-identity"
            ),
            pytest.param("result.json", _overwrite_identity("{"), id="result-malformed-identity"),
        ],
    )
    def test_newer_identity_damaged_run_blocks_older_review(
        self, finder: Any, tmp_path: Path, artifact_name: str, damage: Callable[[Path], None]
    ) -> None:
        """Identity damage in a newer PR run cannot revive stale assessed findings.

        Run 2 keeps a result or candidate but its `pr.json` is missing, malformed or names another PR.
        """
        _write_nested_report(tmp_path, "run-001")
        pending = _write_nested_report(tmp_path, "run-002", result_name=artifact_name)
        damage(pending)

        with pytest.raises(LookupError, match=rf"{UNPROMOTED}|{INVALID}"):
            finder.find_latest_review_report("123", [tmp_path])

    @pytest.mark.parametrize(
        ("pending_name", "diagnostic"),
        [
            pytest.param("review-notes.md", INCOMPLETE, id="notes"),
            pytest.param("result.candidate.json", f"{UNPROMOTED}:", id="candidate"),
        ],
    )
    def test_later_collection_failure_keeps_incomplete_review_blocking(
        self, finder: Any, tmp_path: Path, pending_name: str, diagnostic: str
    ) -> None:
        """A failed collection cannot make an older assessment current again.

        Run 2 is unfinished (notes only, or an unpromoted candidate) and run 3 is a collection failure.
        """
        _write_nested_report(tmp_path, "run-001")
        pending = _write_nested_report(tmp_path, "run-002", result_name=pending_name)
        _write_nested_report(tmp_path, "run-003", review_status="unavailable")

        with pytest.raises(LookupError, match=diagnostic) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(pending.parent) in str(error.value)

    def test_completed_result_supersedes_earlier_incomplete_notes(self, finder: Any, tmp_path: Path) -> None:
        """Recovery needs a newer completed review, not deletion of retained evidence.

        Run 1 holds notes without a result; run 2 is assessed and wins.
        """
        incomplete = tmp_path / "pr-123" / "run-001"
        incomplete.mkdir(parents=True)
        _write_files(incomplete, {"pr.json": json.dumps({"number": 123}), "review-notes.md": "Preliminary findings."})
        assessed = _write_nested_report(tmp_path, "run-002")

        assert finder.find_latest_review_report("123", [tmp_path]) == assessed

    def test_notes_only_report_is_incomplete_not_missing(self, finder: Any, tmp_path: Path) -> None:
        """Diagnose an identified failed handoff independently of session continuity.

        The only run has an identity and notes but no result, so it is incomplete rather than absent.
        """
        run = tmp_path / "pr-123" / "run-001"
        run.mkdir(parents=True)
        _write_files(run, {"pr.json": json.dumps({"number": 123}), "review-notes.md": "Retained findings."})

        with pytest.raises(LookupError, match=INCOMPLETE):
            finder.find_latest_review_report("123", [tmp_path])

    def test_newer_malformed_result_does_not_resurrect_older_verdict(self, finder: Any, tmp_path: Path) -> None:
        """A failed current handoff cannot silently fall back to a stale assessed review.

        Run 2's result is `{}`; run 1 stays hidden and the lookup asks for a new code review.
        """
        _write_nested_report(tmp_path, "run-001")
        _write_nested_report(tmp_path, "run-002", result_text="{}")

        with pytest.raises(LookupError, match=INVALID):
            finder.find_latest_review_report("123", [tmp_path])

    @pytest.mark.parametrize(
        "earlier_run",
        [
            pytest.param({"result_name": "review-notes.md"}, id="notes"),
            pytest.param({"result_name": "result.candidate.json"}, id="candidate"),
            pytest.param({"review_status": "closed"}, id="closed"),
        ],
    )
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_drop_identity, id="missing-identity"),
            pytest.param(_overwrite_identity("{"), id="malformed-identity"),
            pytest.param(_overwrite_result("{"), id="malformed-result"),
        ],
    )
    def test_newest_damaged_result_owns_recovery_over_older_unfinished_or_closed_run(
        self, finder: Any, tmp_path: Path, earlier_run: dict[str, str], damage: Callable[[Path], None]
    ) -> None:
        """Prevent recovery of an older run from hiding the newest damaged review and its evidence.

        Run 1 is unfinished or closed and run 2 is damaged; the error names run 2 only and nothing is rewritten.
        """
        earlier = _write_nested_report(tmp_path, "run-001", **earlier_run)
        newest = _write_nested_report(tmp_path, "run-002")
        damage(newest)
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

        with pytest.raises(LookupError, match=INVALID) as error:
            finder.find_latest_review_report(PR_URL, [tmp_path])

        assert str(newest) in str(error.value)
        assert str(earlier) not in str(error.value)
        assert {path: path.read_bytes() for path in before} == before

    def test_newer_closed_result_supersedes_older_damaged_result(self, finder: Any, tmp_path: Path) -> None:
        """Keep a newer terminal close authoritative while identifying the actual retained close report.

        Run 1 lost its identity, but run 2 is a terminal close, so the close is reported and run 1 is not named.
        """
        earlier = _write_nested_report(tmp_path, "run-001")
        _drop_identity(earlier)
        newest = _write_nested_report(tmp_path, "run-002", review_status="closed")

        with pytest.raises(LookupError, match=CLOSED) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(newest) in str(error.value)
        assert str(earlier) not in str(error.value)

    @pytest.mark.parametrize(
        ("review_status", "result_text", "diagnostic"),
        [
            pytest.param(None, "{}", INVALID, id="invalid"),
            pytest.param("unavailable", None, UNAVAILABLE, id="unavailable"),
            pytest.param("closed", None, CLOSED, id="closed"),
        ],
    )
    def test_rejected_lookup_names_the_selected_report(
        self, finder: Any, tmp_path: Path, review_status: str | None, result_text: str | None, diagnostic: str
    ) -> None:
        """Give recovery the rejected artifact path instead of requiring it to guess another run.

        Run 1 is unavailable; run 2 is invalid, unavailable or closed and is the only report the error names.
        """
        older = _write_nested_report(tmp_path, "run-001", review_status="unavailable")
        newest = _write_nested_report(tmp_path, "run-002", review_status=review_status, result_text=result_text)

        with pytest.raises(LookupError, match=diagnostic) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(newest) in str(error.value)
        assert str(older) not in str(error.value)

    def test_orders_run_indexes_numerically(self, finder: Any, tmp_path: Path) -> None:
        """Prevent lexical ordering from making run 099 newer than run 100.

        Runs 99 and 100 are both assessed; the higher number wins.
        """
        _write_nested_report(tmp_path, "run-099")
        newest = _write_nested_report(tmp_path, "run-100")

        assert finder.find_latest_review_report("123", [tmp_path]) == newest

    def test_nested_run_takes_precedence_over_legacy_timestamp(self, finder: Any, tmp_path: Path) -> None:
        """Treat any matching PR-scoped run as newer than flat timestamp reports.

        A flat report dated far in the future still loses to a PR-scoped run.
        """
        _write_report(tmp_path, "9999-12-31T23-59-59Z", unavailable=False)
        nested = _write_nested_report(tmp_path, "run-001")

        assert finder.find_latest_review_report("123", [tmp_path]) == nested

    @pytest.mark.parametrize(
        ("newest_run", "diagnostic"),
        [
            pytest.param({"result_name": "result.candidate.json"}, UNPROMOTED, id="candidate"),
            pytest.param({"review_status": "closed"}, CLOSED, id="closed"),
        ],
    )
    def test_newer_nested_unpromoted_or_closed_run_blocks_assessed_run(
        self, finder: Any, tmp_path: Path, newest_run: dict[str, str], diagnostic: str
    ) -> None:
        """Require promotion of the highest numbered run, and a later close, before an earlier assessment is reused.

        Run 9 is assessed; run 10 is an unpromoted candidate or a terminal close, and the lookup names run 10.
        """
        _write_nested_report(tmp_path, "run-009")
        newest = _write_nested_report(tmp_path, "run-010", **newest_run)

        with pytest.raises(LookupError, match=diagnostic) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(newest) in str(error.value)

    @pytest.mark.parametrize(
        "earlier_run",
        [
            pytest.param({"result_name": "result.candidate.json"}, id="candidate"),
            pytest.param({"review_status": "closed"}, id="closed"),
        ],
    )
    def test_older_nested_run_does_not_block_assessed_run(
        self, finder: Any, tmp_path: Path, earlier_run: dict[str, str]
    ) -> None:
        """Prefer a promoted later run over an abandoned earlier candidate or an earlier terminal close.

        Run 9 is a candidate or a close and run 10 is assessed, so run 10 is selected.
        """
        _write_nested_report(tmp_path, "run-009", **earlier_run)
        assessed = _write_nested_report(tmp_path, "run-010")

        assert finder.find_latest_review_report("123", [tmp_path]) == assessed

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_overwrite_result(json.dumps({"metadata": {"scope": "pr"}})), id="result-without-decision"),
            pytest.param(_drop_identity, id="missing-identity"),
        ],
    )
    def test_nested_result_requires_valid_result_and_explicit_pr_identity(
        self, finder: Any, tmp_path: Path, damage: Callable[[Path], None]
    ) -> None:
        """Retain explicit result validation inside the new directory topology.

        The directory name alone is not a validated identity, and a result with no decision is not an assessment.
        """
        result = _write_nested_report(tmp_path, "run-001")
        damage(result)

        with pytest.raises(LookupError, match=INVALID):
            finder.find_latest_review_report("123", [tmp_path])

    def test_nested_result_rejects_directory_identity_disagreement(self, finder: Any, tmp_path: Path) -> None:
        """Require the PR directory and collected identity to name the same pull request.

        The `pr-123` directory holds an identity for PR 456, so a lookup for 456 finds nothing.
        """
        result = _write_nested_report(tmp_path, "run-001")
        (result.parent / "pr.json").write_text(
            _identity(456, "https://github.com/acme/widgets/pull/456"), encoding="utf-8"
        )

        with pytest.raises(LookupError, match=MISSING):
            finder.find_latest_review_report("456", [tmp_path])

    @pytest.mark.parametrize(
        ("pr_name", "run_name"),
        [
            pytest.param("pr-x", "run-001", id="pr-x"),
            pytest.param("pr-123", "run-x", id="pr-123-run-x"),
            pytest.param("pr-123", "run-01", id="pr-123-run-01"),
            pytest.param("pr-123", "run-000", id="pr-123-run-000"),
        ],
    )
    def test_ignores_malformed_nested_directory_names(
        self, finder: Any, tmp_path: Path, pr_name: str, run_name: str
    ) -> None:
        """Ignore directories outside the canonical PR and zero-padded run grammar.

        Complete reports under `pr-x`, `run-x`, `run-01` and `run-000` are never discovered.
        """
        report_dir = tmp_path / pr_name / run_name
        report_dir.mkdir(parents=True)
        _write_files(
            report_dir,
            {
                "pr.json": _identity(),
                "result.json": json.dumps(
                    {"metadata": {"scope": "pr", "review_decision": {"recommendation": "accept-as-is"}}}
                ),
            },
        )

        with pytest.raises(LookupError, match=MISSING):
            finder.find_latest_review_report("123", [tmp_path])


class TestPromotedRunIntegration:
    """Join the allocator's printed topology to the remediation finder."""

    def test_finder_selects_the_promoted_run(self, finder: Any, tmp_path: Path) -> None:
        """Prevent producer and consumer path grammars from drifting apart.

        The real `create_run.py` allocates a staging run and promotes it to `pr-123/run-001`; the finder then selects
        the promoted result.
        """
        created = subprocess.run(
            [sys.executable, str(CREATE_RUN_PATH), "--skill", "code-review", "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        staging = Path(created.stdout.strip())
        _write_files(staging, {"pr.json": _identity(), "result.json": _assessed_result_text()})

        promoted = subprocess.run(
            [
                sys.executable,
                str(CREATE_RUN_PATH),
                *("--skill", "code-review", "--root", str(tmp_path), "--promote-pr-run", str(staging)),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        expected = tmp_path / "code-review" / "pr-123" / "run-001" / "result.json"
        assert promoted.returncode == 0, promoted.stderr
        assert Path(promoted.stdout.strip()) == expected.parent
        assert finder.find_latest_review_report("#123", [tmp_path / "code-review"]) == expected


class TestFlatReports:
    """Lookup over flat timestamped report directories."""

    def test_newer_unavailable_report_does_not_shadow_older_assessed_review(self, finder: Any, tmp_path: Path) -> None:
        """Keep automatic remediation bound to findings that were actually assessed.

        A later collection failure carries no findings, so the earlier assessed report is still selected.
        """
        assessed = _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
        _write_report(tmp_path, "2026-08-10T11-00-00Z", unavailable=True)

        assert finder.find_latest_review_report("123", [tmp_path]) == assessed

    @pytest.mark.parametrize("target", [PR_URL, "123", "#123"])
    def test_only_unavailable_reports_require_a_new_code_review(self, finder: Any, tmp_path: Path, target: str) -> None:
        """Do not let remediation consume an operational diagnostic as source findings.

        A pre-identity collection failure keeps its unavailable diagnosis for the URL form and for numeric lookup.
        """
        _write_report(tmp_path, "2026-08-10T11-00-00Z", unavailable=True)

        with pytest.raises(LookupError, match=UNAVAILABLE):
            finder.find_latest_review_report(target, [tmp_path])

    @pytest.mark.parametrize("target", ["123", PR_URL])
    @pytest.mark.parametrize(
        "newer_result",
        [
            pytest.param("{", id="malformed-result"),
            pytest.param(json.dumps({"metadata": {"scope": "pr", "review_status": "closed"}}), id="closed-result"),
        ],
    )
    def test_newer_target_only_result_blocks_stale_assessment(
        self, finder: Any, tmp_path: Path, newer_result: str, target: str
    ) -> None:
        """A flat run identified by its recorded PR target cannot revive old findings.

        The newer run holds only `pr-target.txt` and a damaged or closed-without-evidence result.
        """
        _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
        newer = tmp_path / "2026-08-10T11-00-00Z"
        newer.mkdir()
        _write_files(newer, {"pr-target.txt": PR_URL + "\n", "result.json": newer_result})

        with pytest.raises(LookupError, match=INVALID):
            finder.find_latest_review_report(target, [tmp_path])

    @pytest.mark.parametrize(
        ("pending_file", "payload"),
        [
            pytest.param("pr-target.txt", PR_URL + "\n", id="target-only"),
            pytest.param("pr.json", _identity(), id="identity-only"),
        ],
    )
    @BOTH_TARGETS
    def test_newer_collection_before_result_blocks_stale_assessment(
        self, finder: Any, tmp_path: Path, pending_file: str, payload: str, target: str
    ) -> None:
        """A started flat collection stays pending before notes or result exist.

        The newer run has only its target or identity file; the older assessed report stays hidden.
        """
        _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
        pending = tmp_path / "2026-08-10T11-00-00Z"
        pending.mkdir()
        (pending / pending_file).write_text(payload, encoding="utf-8")

        with pytest.raises(LookupError, match=INCOMPLETE) as error:
            finder.find_latest_review_report(target, [tmp_path])

        assert str(pending) in str(error.value)

    @pytest.mark.parametrize("pending_file", ["pr-target.txt", "pr.json"])
    @pytest.mark.parametrize("target", MALFORMED_TARGETS)
    def test_malformed_target_cannot_match_unrelated_pending_run(
        self, finder: Any, tmp_path: Path, pending_file: str, target: str
    ) -> None:
        """Missing parsed identity cannot equal a flat report's missing directory identity.

        The pending run belongs to PR 456; no malformed request may match it.
        """
        pending = tmp_path / "2026-08-10T11-00-00Z"
        pending.mkdir()
        payload = PR_URL.replace("123", "456") if pending_file == "pr-target.txt" else '{"number": 456}'
        (pending / pending_file).write_text(payload, encoding="utf-8")

        with pytest.raises(LookupError, match=MISSING):
            finder.find_latest_review_report(target, [tmp_path])

    @pytest.mark.parametrize("target", MALFORMED_TARGETS)
    def test_matching_malformed_target_is_not_pr_identity(self, finder: Any, tmp_path: Path, target: str) -> None:
        """An identical retained malformed string cannot bypass canonical URL validation.

        The pending run stores exactly the same malformed text as the request.
        """
        pending = tmp_path / "2026-08-10T11-00-00Z"
        pending.mkdir()
        (pending / "pr-target.txt").write_text(target, encoding="utf-8")

        with pytest.raises(LookupError, match=MISSING):
            finder.find_latest_review_report(target, [tmp_path])

    @pytest.mark.parametrize("target", ["0", "#0"])
    @pytest.mark.parametrize("stored_target", ["0", "malformed-target"])
    def test_zero_pr_target_cannot_match_pending_run(
        self, finder: Any, tmp_path: Path, target: str, stored_target: str
    ) -> None:
        """Neither numeric text nor two missing parsed numbers can admit a zero PR identity.

        Zero is not a PR number, whether it is stored as `0` or as unparsable text.
        """
        pending = tmp_path / "2026-08-10T11-00-00Z"
        pending.mkdir()
        (pending / "pr-target.txt").write_text(stored_target, encoding="utf-8")

        with pytest.raises(LookupError, match=MISSING):
            finder.find_latest_review_report(target, [tmp_path])

    @pytest.mark.parametrize(
        ("write_newer", "diagnostic"),
        [
            pytest.param(_write_closed_report, CLOSED, id="closed"),
            pytest.param(_write_candidate_report, UNPROMOTED, id="candidate"),
        ],
    )
    def test_newer_closed_or_candidate_report_blocks_older_assessed_review(
        self, finder: Any, tmp_path: Path, write_newer: Callable[[Path, str], Path], diagnostic: str
    ) -> None:
        """Prevent remediation from reviving stale findings after a newer close decision or unpromoted candidate.

        An assessed report is followed by a terminal close or a candidate that has not been validated and promoted.
        """
        _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
        newer = write_newer(tmp_path, "2026-08-10T11-00-00Z")

        with pytest.raises(LookupError, match=diagnostic) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(newer) in str(error.value)

    def test_candidate_only_report_requires_validation_and_promotion(self, finder: Any, tmp_path: Path) -> None:
        """Expose recoverable candidate state instead of reporting no prior review.

        The only report is an unpromoted candidate; the error names it.
        """
        candidate = _write_candidate_report(tmp_path, "2026-08-10T11-00-00Z")

        with pytest.raises(LookupError, match=UNPROMOTED) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(candidate) in str(error.value)

    def test_candidate_for_other_pull_request_does_not_block_assessed_review(self, finder: Any, tmp_path: Path) -> None:
        """Scope candidate recovery to the requested pull request rather than the report root.

        A newer candidate for PR 456 is irrelevant to the lookup for PR 123.
        """
        assessed = _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
        _write_candidate_report(tmp_path, "2026-08-10T11-00-00Z", pull_number=456)

        assert finder.find_latest_review_report("123", [tmp_path]) == assessed

    @LOCAL_SCOPES
    def test_pr_lookup_rejects_local_report_with_leftover_pr_identity(
        self, finder: Any, tmp_path: Path, scope: str
    ) -> None:
        """Explicit local intake must not widen PR discovery to unrelated local assessments.

        A local-scope result that still sits beside a `pr.json` is invalid for a PR lookup.
        """
        result = _write_report(tmp_path, "2026-08-10T11-00-00Z", unavailable=False)
        result.write_text(_assessed_result_text(scope), encoding="utf-8")

        with pytest.raises(LookupError) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(error.value) == f"{INVALID}:{result}"


class TestExplicitIntake:
    """Admission of an explicitly named result for remediation."""

    @pytest.mark.parametrize(
        ("metadata", "diagnostic"),
        [
            pytest.param(
                {"scope": "working-tree", "review_status": "unavailable"}, UNAVAILABLE, id="local-unavailable"
            ),
            pytest.param({"scope": "path", "review_status": "closed"}, CLOSED, id="local-closed"),
            pytest.param(
                {"scope": "unknown", "review_decision": {"recommendation": "needs-more-work"}},
                INVALID,
                id="unknown-scope",
            ),
            pytest.param(
                {"scope": [], "review_decision": {"recommendation": "needs-more-work"}}, INVALID, id="malformed-scope"
            ),
            pytest.param(
                {"scope": "commit", "review_decision": {"recommendation": "unknown"}}, INVALID, id="unknown-decision"
            ),
        ],
    )
    def test_local_intake_retains_rejection_boundaries(
        self, finder: Any, tmp_path: Path, metadata: dict[str, object], diagnostic: str
    ) -> None:
        """Preserve rejection categories and identify the exact recoverable report without admitting it.

        Unavailable, closed, unknown-scope and unknown-decision local results are each refused with their own code.
        """
        result = tmp_path / "result.json"
        result.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

        with pytest.raises(LookupError) as error:
            finder.require_assessed_review_result(result)

        assert str(error.value) == f"{diagnostic}:{result}"

    @LOCAL_SCOPES
    def test_local_candidate_still_requires_promotion(self, finder: Any, tmp_path: Path, scope: str) -> None:
        """Local scope support must not make a draft result consumable.

        A candidate file for any local scope is refused until it has been validated and promoted.
        """
        candidate = tmp_path / "result.candidate.json"
        candidate.write_text(_assessed_result_text(scope), encoding="utf-8")

        with pytest.raises(LookupError, match=rf"^{UNPROMOTED}:"):
            finder.require_assessed_review_result(candidate)

    @pytest.mark.parametrize(
        ("write_report", "diagnostic"),
        [
            pytest.param(_write_unavailable_report, UNAVAILABLE, id="unavailable"),
            pytest.param(_write_malformed_report, INVALID, id="malformed"),
            pytest.param(_write_closed_report, CLOSED, id="closed"),
            pytest.param(_write_candidate_report, UNPROMOTED, id="candidate"),
        ],
    )
    def test_nonassessed_report_is_rejected_as_remediation_input(
        self, finder: Any, tmp_path: Path, write_report: Callable[[Path, str], Path], diagnostic: str
    ) -> None:
        """Never treat a diagnostic, unreadable file, close decision or unpromoted candidate as source findings.

        A terminal proposal-level close is not source findings, an unreadable result fails closed, and an explicit
        candidate path must not bypass full artifact validation.
        """
        report = write_report(tmp_path, "2026-08-10T11-00-00Z")

        with pytest.raises(LookupError, match=diagnostic):
            finder.require_assessed_review_result(report)

    def test_invalid_report_diagnostic_retains_requested_path(self, finder: Any, tmp_path: Path) -> None:
        """Keep the supplied recovery target available when explicit admission rejects its report bytes.

        The result is `{}`; the error carries the exact path that was passed in.
        """
        path = _write_nested_report(tmp_path, "run-001", result_text="{}")

        with pytest.raises(LookupError, match=INVALID) as error:
            finder.require_assessed_review_result(path)

        assert str(path) in str(error.value)

    @pytest.mark.parametrize("intake", ["explicit", "target"])
    def test_pr_metadata_only_report_is_rejected(self, tmp_path: Path, intake: str) -> None:
        """PR intake must not accept a disposition without the producer's required evidence.

        A schema-v3 result with only a scope and a recommendation fails producer validation through both the explicit
        and the target intake, run through the real CLI.
        """
        report_dir = tmp_path / "run"
        report_dir.mkdir()
        (report_dir / "pr.json").write_text(json.dumps({"number": 123}), encoding="utf-8")
        result = report_dir / "result.json"
        result.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}},
                }
            ),
            encoding="utf-8",
        )
        args = (
            ["--result", str(result)] if intake == "explicit" else ["--target", "123", "--reports-dir", str(tmp_path)]
        )

        completed = _run_finder(*args)

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "review-validation-failed:" in completed.stderr

    @LOCAL_SCOPES
    def test_local_metadata_only_report_is_rejected(self, tmp_path: Path, scope: str) -> None:
        """A canonical filename and plausible disposition do not establish producer validation.

        A schema-v3 local result with only metadata fails producer validation in the real CLI.
        """
        result = tmp_path / "result.json"
        result.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "metadata": {"scope": scope, "review_decision": {"recommendation": "needs-more-work"}},
                }
            ),
            encoding="utf-8",
        )

        completed = _run_finder("--result", str(result), "--parent-thread-id", "thread")

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "review-validation-failed:" in completed.stderr

    def test_schema_three_without_retained_role_proof_is_readable_only_as_archive(self, tmp_path: Path) -> None:
        """Older current-format reports remain inspectable without certifying their provenance.

        The real CLI prints an `unverified-archive` record for a schema-v3 result that lacks role proof.
        """
        result = tmp_path / "result.json"
        result.write_text(json.dumps({"schema_version": 3, "metadata": {"scope": "pr"}}), encoding="utf-8")

        archive = _run_finder("--archive-result", str(result))

        assert archive.returncode == 0, archive.stderr
        assert json.loads(archive.stdout) == {
            "status": "unverified-archive",
            "path": str(result),
            "schema_version": 3,
            "scope": "pr",
        }


@pytest.mark.integration
class TestAssessedLocalReports:
    """Real local review artifacts produced by the review fixtures and checked by both producer validators."""

    def test_schema_two_archive_is_readable_but_unverified(self, finder: Any, assessed_local: Path) -> None:
        """Keep archive access without admitting an old result as remediation evidence.

        A real local result is rewritten as schema v2. The CLI still prints it as an unverified archive, but the library
        refuses it for remediation and run completion.
        """
        _downgrade_to_schema_two(assessed_local)

        with pytest.raises(LookupError, match="historical-review-unverified-archive"):
            finder.require_assessed_review_result(assessed_local, parent_thread_id="thread")
        archive = _run_finder("--archive-result", str(assessed_local))
        assert archive.returncode == 0, archive.stderr
        assert json.loads(archive.stdout) == {
            "status": "unverified-archive",
            "path": str(assessed_local),
            "schema_version": 2,
            "scope": "working-tree",
        }
        with pytest.raises(LookupError, match="review-completion-requires-bound-schema-v3"):
            finder.complete_review_run(assessed_local.parent, parent_thread_id="thread")

    def test_schema_downgrade_cannot_admit_a_rewritten_review_for_remediation(self, assessed_local: Path) -> None:
        """Reject a rewritten current result even after its assessment fields are stripped.

        Downgrading a valid schema-v3 result to v2 turns it into an archive that the CLI refuses as input.
        """
        assert read_json(assessed_local)["schema_version"] == 3
        _downgrade_to_schema_two(assessed_local)

        downgraded = _run_finder("--result", str(assessed_local), "--parent-thread-id", "thread")

        assert downgraded.returncode == 1
        assert downgraded.stdout == ""
        assert "historical-review-unverified-archive" in downgraded.stderr

    def test_rewritten_review_notes_rating_mismatch_is_rejected(self, assessed_local: Path) -> None:
        """A schema-v3 result whose notes disagree with the recorded rating is rejected.

        The review notes are edited from `Rating: 1` to `Rating: 2` after the result was produced.
        """
        notes_path = assessed_local.parent / "review-notes.md"
        notes = notes_path.read_text(encoding="utf-8")
        assert notes.count("Rating: 1") == 1
        notes_path.write_text(notes.replace("Rating: 1", "Rating: 2"), encoding="utf-8")

        current = _run_finder("--result", str(assessed_local), "--parent-thread-id", "thread")

        assert current.returncode == 1
        assert current.stdout == ""
        assert "review-assessment-rating-mismatch:main reviewer" in current.stderr

    @pytest.mark.parametrize(
        ("scope", "commit_suffix"),
        [
            pytest.param("working-tree", " (working-tree base)", id="working-tree"),
            pytest.param("path", " (working-tree base)", id="path"),
            pytest.param("commit", "", id="commit"),
        ],
    )
    def test_local_report_requires_complete_validation(
        self, assessed_local: Path, scope: str, commit_suffix: str
    ) -> None:
        """Accept real local artifacts through both producer validators before intake.

        The fixture result is re-rendered for each local scope with a matching snapshot and commit label.
        """
        result = read_json(assessed_local)
        result["metadata"]["scope"] = scope
        handoff = read_json(assessed_local.parent / "final-handoff.json")
        handoff["tables"][0]["rows"][0]["cells"] = ["Scope", scope]
        handoff["tables"][0]["rows"][-2]["cells"] = [
            "Commit",
            result["metadata"]["reviewed_commit_sha"] + commit_suffix,
        ]
        _render_and_bind(assessed_local.parent, handoff, result)
        assessed_local.write_text(json.dumps(result), encoding="utf-8")

        completed = _run_finder("--result", str(assessed_local), "--parent-thread-id", "thread")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == str(assessed_local)
        assert completed.stderr == ""

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_mark_draft, id="draft"),
            pytest.param(_drop_final_handoff_binding, id="missing-handoff"),
            pytest.param(_replace_final_text, id="final-text"),
            pytest.param(_replace_diff, id="diff"),
            pytest.param(_change_manifest_parent, id="manifest-parent"),
        ],
    )
    def test_local_report_rejects_invalid_producer_evidence(
        self, assessed_local: Path, damage: Callable[[Path], None]
    ) -> None:
        """Exercise real validation failures instead of mocking the intake's proof boundary.

        A draft status, a missing handoff binding, replaced final text or diff, or a foreign producing thread each make
        the real validators refuse the result.
        """
        damage(assessed_local)

        completed = _run_finder("--result", str(assessed_local), "--parent-thread-id", "thread")

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "review-validation-failed:" in completed.stderr

    def test_local_alias_cannot_validate_its_sibling_instead(self, assessed_local: Path) -> None:
        """Never validate result.json while returning a different, unvalidated local file.

        An `other.json` beside the validated result carries a plausible disposition but no evidence of its own.
        """
        alias = assessed_local.with_name("other.json")
        alias.write_text(_assessed_result_text("working-tree"), encoding="utf-8")

        completed = _run_finder("--result", str(alias), "--parent-thread-id", "thread")

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert INVALID in completed.stderr


@pytest.mark.integration
class TestAssessedPrReports:
    """Real PR review artifacts produced by the review fixtures and checked by the producer validators."""

    def test_target_lookup_rejects_unverified_archive(self, tmp_path: Path) -> None:
        """Prevent automatic PR remediation from consuming a schema-two result.

        The fixture's schema-v3 PR result is marked schema v2; the target lookup refuses it as an archive.
        """
        run = _assessed_pr.__wrapped__(tmp_path)
        result = read_json(run / "result.json")
        result["schema_version"] = 2
        (run / "result.json").write_text(json.dumps(result), encoding="utf-8")

        targeted = _run_finder("--target", "123", "--reports-dir", str(run.parent.parent))

        assert targeted.returncode == 1
        assert targeted.stdout == ""
        assert "historical-review-unverified-archive" in targeted.stderr

    def test_intake_revalidates_recorded_producer_across_sessions(self, tmp_path: Path) -> None:
        """Validate current PR intake against its producer rather than the consumer thread.

        Explicit and target intake both accept the fixture's valid result from another session; replacing the reviewed
        diff afterwards makes the explicit intake fail validation.
        """
        run = _assessed_pr.__wrapped__(tmp_path)
        path = run / "result.json"
        intakes = [["--result", str(path)], ["--target", "123", "--reports-dir", str(run.parent.parent)]]

        accepted = [_run_finder(*args) for args in intakes]
        (run / "diff.patch").write_text("Unreviewed replacement diff.\n", encoding="utf-8")
        damaged = _run_finder("--result", str(path))

        assert [(p.returncode, Path(p.stdout.strip())) for p in accepted] == [(0, path), (0, path)], [
            p.stderr for p in accepted
        ]
        assert damaged.returncode == 1
        assert damaged.stdout == ""
        assert "review-validation-failed:" in damaged.stderr

    @staticmethod
    def _supersede_legacy_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finder: Any, **newer_run: str | None
    ) -> tuple[Path, Path]:
        """Return a legacy-root assessed run and a canonical-root run that replaces it as the current root."""
        legacy_run = _assessed_pr.__wrapped__(tmp_path / "legacy")
        canonical_root = tmp_path / "canonical" / "code-review"
        newer = _write_nested_report(canonical_root, **newer_run)
        monkeypatch.setattr(finder, "CURRENT_REPORTS_DIR", canonical_root)
        monkeypatch.setattr(finder, "LEGACY_REPORTS_DIR", legacy_run.parent.parent)
        return legacy_run, newer

    @pytest.mark.parametrize(
        "run_name",
        [pytest.param("run-002", id="higher-candidate"), pytest.param("run-001", id="equal-candidate")],
    )
    def test_legacy_result_is_superseded_by_canonical_candidate(
        self, finder: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_name: str
    ) -> None:
        """An equally or higher numbered canonical candidate supersedes a legacy PR result.

        The legacy run completes on its own, but explicit intake of its result is refused and names the candidate.
        """
        legacy_run, newer = self._supersede_legacy_run(
            tmp_path, monkeypatch, finder, run_name=run_name, result_name="result.candidate.json"
        )

        assert (
            finder.complete_review_run(legacy_run, parent_thread_id="thread") == (legacy_run / "final.md").read_bytes()
        )
        with pytest.raises(LookupError, match=UNPROMOTED) as error:
            finder.require_assessed_review_result(legacy_run / "result.json", parent_thread_id="thread")

        assert str(newer) in str(error.value)

    @pytest.mark.parametrize(
        ("review_status", "diagnostic"),
        [
            pytest.param("closed", CLOSED, id="equal-closed"),
            pytest.param(None, "review-completion-not-current", id="equal-assessed"),
        ],
    )
    def test_legacy_result_is_superseded_by_equal_canonical_run(
        self,
        finder: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        review_status: str | None,
        diagnostic: str,
    ) -> None:
        """An equally numbered canonical close or assessment supersedes a legacy PR result.

        The legacy run completes on its own, but explicit intake of its result is refused as not current or closed.
        """
        legacy_run, _newer = self._supersede_legacy_run(
            tmp_path, monkeypatch, finder, run_name="run-001", review_status=review_status
        )

        assert (
            finder.complete_review_run(legacy_run, parent_thread_id="thread") == (legacy_run / "final.md").read_bytes()
        )
        with pytest.raises(LookupError, match=diagnostic):
            finder.require_assessed_review_result(legacy_run / "result.json", parent_thread_id="thread")

    def test_candidate_beside_promoted_result_blocks_intake(self, finder: Any, tmp_path: Path) -> None:
        """A pending candidate in the same run cannot hide behind its promoted sibling.

        A copy of the promoted result is left as `result.candidate.json` in the same run.
        """
        run = _assessed_pr.__wrapped__(tmp_path)
        promoted = run / "result.json"
        candidate = run / "result.candidate.json"
        candidate.write_bytes(promoted.read_bytes())

        with pytest.raises(LookupError, match=UNPROMOTED) as error:
            finder.require_assessed_review_result(promoted, parent_thread_id="thread")

        assert str(candidate) in str(error.value)

    @pytest.mark.parametrize(
        ("pending_file", "content", "diagnostic"),
        [
            pytest.param("pr.json", _identity(), "matching-review-incomplete", id="collected-only"),
            pytest.param("result.candidate.json", "{}", UNPROMOTED, id="candidate-identity-lost"),
        ],
    )
    def test_explicit_intake_rejects_newer_pending_run(
        self, finder: Any, tmp_path: Path, pending_file: str, content: str, diagnostic: str
    ) -> None:
        """A valid older result cannot pass explicit intake while a newer PR run is unfinished.

        Run 2 is only collected, or only an empty candidate with no identity.
        """
        run = _assessed_pr.__wrapped__(tmp_path)
        pending = run.parent / "run-002"
        pending.mkdir()
        (pending / pending_file).write_text(content, encoding="utf-8")

        with pytest.raises(LookupError, match=diagnostic):
            finder.require_assessed_review_result(run / "result.json", parent_thread_id="thread")


class TestCanonicalRoot:
    """The canonical report root outranks the legacy root regardless of run numbers."""

    def test_supersedes_larger_legacy_run_index(
        self, finder: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy run number is never a cross-root recency claim.

        Legacy run 9 and canonical run 1 are both assessed; the canonical one wins.
        """
        legacy_root = tmp_path / "legacy"
        canonical_root = tmp_path / "canonical"
        _write_nested_report(legacy_root, "run-009")
        current = _write_nested_report(canonical_root, "run-001")
        monkeypatch.setattr(finder, "CURRENT_REPORTS_DIR", canonical_root)
        monkeypatch.setattr(finder, "LEGACY_REPORTS_DIR", legacy_root)

        assert finder.find_latest_review_report("123", [legacy_root, canonical_root]) == current


@pytest.fixture
def finding_with_direct_sources(tmp_path: Path) -> tuple[Path, list[dict]]:
    """Retain two distinct online origins and ordinary reviewer evidence for one confirmed finding."""
    origins = [
        {
            "id": f"review-comment-{index}",
            "parent_id": f"review-thread-{index}",
            "path": "widget.py",
            "line": index + 6,
            "body": body,
            "artifact": "review-threads.json",
            "candidate_id": "F1",
            "disposition": "confirmed",
        }
        for index, body in enumerate(("Guard the empty input.", "Guard the repeated empty input."), 1)
    ]
    specialist = tmp_path / "specialists" / "engineer.md"
    specialist.parent.mkdir()
    specialist.write_text("Both input paths require the same guard.\n", encoding="utf-8", newline="\n")
    (tmp_path / "review-notes.md").write_text("Confirmed input guard.\n", encoding="utf-8", newline="\n")
    report = {
        "metadata": {
            "review_findings": [
                {
                    "id": "F1",
                    "title": "Missing input guard",
                    "authors": ["Software engineer"],
                    "evidence": ["specialists/engineer.md", "widget.py:7"],
                }
            ],
            "reviewer_assessments": [{"role": "Software engineer", "evidence": "specialists/engineer.md"}],
            "source_findings": origins,
            "source_finding_mapping": [{"source_id": origin["id"], "candidate_id": "F1"} for origin in origins],
        }
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(report), encoding="utf-8", newline="\n")
    return path, origins


def _drop_supplemental_origins(metadata: dict) -> None:
    """Remove the optional online-origin ledger fields."""
    del metadata["source_findings"]
    del metadata["source_finding_mapping"]


def _empty_supplemental_origins(metadata: dict) -> None:
    """Leave the optional online-origin ledger fields present but empty."""
    metadata["source_findings"] = []
    metadata["source_finding_mapping"] = []


def _drop_origin_mapping(metadata: dict) -> None:
    """Remove the mapping that ties the last origin to the finding."""
    metadata["source_finding_mapping"].pop()


def _conflict_origin_mapping(metadata: dict) -> None:
    """Add a second mapping that ties an origin to a different finding."""
    metadata["source_finding_mapping"].append({"source_id": "review-comment-1", "candidate_id": "F2"})


def _drop_origin_record(metadata: dict) -> None:
    """Remove the last origin record while its mapping stays."""
    metadata["source_findings"].pop()


class TestFindingEvidence:
    """Per-finding evidence lookup that remediation uses to trace each action back to its sources."""

    def test_preserves_every_direct_origin(
        self, finder: Any, finding_with_direct_sources: tuple[Path, list[dict]]
    ) -> None:
        """Keep distinct online IDs and complete original records when one action reconciles multiple reports.

        One finding is confirmed by two review comments; both original records, the reviewer evidence and the notes path
        come back, and the report file is left unchanged.
        """
        path, origins = finding_with_direct_sources
        original = path.read_bytes()

        evidence = finder.finding_evidence(path, "F1")

        assert evidence["source_origins"] == origins
        assert evidence["author_evidence"] == [
            {"author": "Software engineer", "path": str(path.parent / "specialists/engineer.md")}
        ]
        # Retain the report's portable evidence label beside its resolved native filesystem path.
        assert evidence["artifact_evidence"] == [  # path-sep: native
            {"evidence": "specialists/engineer.md", "path": str(path.parent / "specialists/engineer.md")}
        ]
        assert evidence["source_evidence"] == ["widget.py:7"]
        assert evidence["review_notes"] == str(path.parent / "review-notes.md")
        assert path.read_bytes() == original

    @pytest.mark.parametrize(
        "drop_origins",
        [
            pytest.param(_drop_supplemental_origins, id="absent"),
            pytest.param(_empty_supplemental_origins, id="empty"),
        ],
    )
    def test_allows_no_supplemental_origins(
        self,
        finder: Any,
        finding_with_direct_sources: tuple[Path, list[dict]],
        drop_origins: Callable[[dict], None],
    ) -> None:
        """Do not require an optional online-origin ledger to read a finding's ordinary source evidence.

        The origin ledger fields are removed or emptied; the reviewer evidence is still returned.
        """
        path, _origins = finding_with_direct_sources
        report = read_json(path)
        drop_origins(report["metadata"])
        path.write_text(json.dumps(report), encoding="utf-8", newline="\n")

        evidence = finder.finding_evidence(path, "F1")

        assert evidence["source_origins"] == []
        assert evidence["source_evidence"] == ["widget.py:7"]

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_drop_origin_mapping, id="missing-relation"),
            pytest.param(_conflict_origin_mapping, id="conflicting-relation"),
            pytest.param(_drop_origin_record, id="missing-origin"),
        ],
    )
    def test_rejects_incomplete_direct_origin_relations(
        self, finder: Any, finding_with_direct_sources: tuple[Path, list[dict]], damage: Callable[[dict], None]
    ) -> None:
        """Reject broken relevant associations instead of silently losing one of a finding's source obligations.

        An origin without a mapping, a mapping to two findings, or a mapping without its origin record is refused.
        """
        path, _origins = finding_with_direct_sources
        report = read_json(path)
        damage(report["metadata"])
        path.write_text(json.dumps(report), encoding="utf-8", newline="\n")

        with pytest.raises(LookupError):
            finder.finding_evidence(path, "F1")

    def test_keeps_batched_origin_relations(
        self, finder: Any, finding_with_direct_sources: tuple[Path, list[dict]]
    ) -> None:
        """Preserve existing batched original records and reconciliation while fixing direct-review intake.

        The origin ledger uses the batched specialist-output form with a reconciliation note; both come back intact.
        """
        path, direct_origins = finding_with_direct_sources
        report = read_json(path)
        origins = [
            {
                "finding_id": f"source.software-engineer.origin-{index}",
                "original": {"id": f"origin-{index}", "summary": origin["body"]},
                "original_text": json.dumps(origin),
                "manifest_path": "source/specialist-manifest.json",
                "manifest_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "pass": {"role": "sw-engineer", "output_path": "specialists/engineer.md"},
                "disposition": "unresolved",
            }
            for index, origin in enumerate(direct_origins, 1)
        ]
        reconciliation = {"invariant": "Reject empty input.", "rationale": "One guard covers both input paths."}
        report["metadata"].update(
            source_findings=origins,
            source_finding_mapping={origin["finding_id"]: ["F1"] for origin in origins},
            source_finding_reconciliation={"F1": reconciliation},
        )
        path.write_text(json.dumps(report), encoding="utf-8", newline="\n")

        evidence = finder.finding_evidence(path, "F1")

        assert evidence["source_origins"] == origins
        assert evidence["source_reconciliation"] == reconciliation
