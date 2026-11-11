"""Verify bounded challenge chunks cover changed source and reject stale evidence."""

from __future__ import annotations

import copy
import dataclasses
import errno
import hashlib
import importlib.util
import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from _artifact_files import read_json, write_json

CHUNKER = Path(__file__).resolve().parents[2] / "skills" / "challenge-resolve" / "chunk_diff.py"
_COORDINATOR_REQUEST = {
    "goal": "Review the complete changed-file contract",
    "specification": "Every changed file must preserve the fixture contract",
    "done_when": "All chunks and their interactions are independently clean",
}
_CLEAN_RESULT = (
    '{"status":"pass","metadata":{"adversarial_loop":{"reason":"clean","scores":[0],"rounds":[{"decision":"clean"}]}}}'
)
_PRIOR_FINDING = {
    "signature": "caller-origin",
    "tier": "high",
    "structural": False,
    "evidence": ["Caller record absent"],
    "source_paths": ["alpha.py"],
}


def _request_args(specification: str = "Fixture contract") -> tuple[str, ...]:
    """Return the request flags every `plan` call needs, with an optional specification."""
    return "--goal", "Review changed files", "--specification", specification, "--done-when", "Clean review"


REQUEST_ARGS = _request_args()


def _prior_specification(base: str = "Fixture contract") -> str:
    """Return a specification followed by the prior-findings block planning appends for the fixture finding."""
    return base + "\nPrior findings to reassess:\n" + json.dumps([_PRIOR_FINDING], sort_keys=True, ensure_ascii=True)


def _load_chunker() -> ModuleType:
    """Import the packaged chunker by path so its private helpers are reachable."""
    spec = importlib.util.spec_from_file_location("challenge_chunk_diff", CHUNKER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def chunker() -> ModuleType:
    """Return a private copy of the chunker module, free of patches from other tests."""
    return _load_chunker()


@pytest.fixture(scope="module")
def _committed_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a committed baseline without background maintenance mutating its copies."""
    repository = tmp_path_factory.mktemp("committed-template") / "repository"
    repository.mkdir()
    for name in ("alpha.py", "beta.py", "removed.py"):
        (repository / name).write_text(f"VALUE = '{name}'\n", encoding="utf-8", newline="\n")
    for args in (
        ("init", "-q"),
        ("config", "maintenance.auto", "false"),
        ("add", "."),
        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"),
    ):
        subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)
    return repository


def _copy_changed_repository(template: Path, repository: Path) -> Path:
    """Copy the committed baseline and add tracked edits, a deletion, and untracked source."""
    shutil.copytree(template, repository, symlinks=True)
    (repository / "alpha.py").write_text("VALUE = 'changed alpha'\n", encoding="utf-8", newline="\n")
    (repository / "beta.py").write_text("VALUE = 'changed beta'\n", encoding="utf-8", newline="\n")
    (repository / "removed.py").unlink()
    (repository / "new.py").write_text("VALUE = 'new'\n", encoding="utf-8", newline="\n")
    return repository


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the packaged chunker through its public CLI."""
    return subprocess.run([sys.executable, str(CHUNKER), *args], text=True, capture_output=True, check=False)


def _plan(
    repository: Path, out: Path, *extra: str, request: tuple[str, ...] = REQUEST_ARGS
) -> subprocess.CompletedProcess[str]:
    """Plan a review of `repository` into `out` with the small fixture byte budget."""
    return _run("plan", "--repository", str(repository), "--out", str(out), "--budget-bytes", "900", *extra, *request)


def _check_cli(manifest: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run `check` on a manifest through the public CLI."""
    return _run("check", "--manifest", str(manifest), *extra)


@pytest.fixture
def changed_repository(_committed_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a private repository copy with tracked edits, a deletion, and untracked source."""
    repository = _copy_changed_repository(_committed_template, tmp_path / "repository")
    monkeypatch.chdir(repository)
    return repository


@pytest.fixture(scope="module")
def _shared_plan(_committed_template: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Plan the standard changed repository once per module; tests only ever copy the review directory.

    The repository stays read-only for every consumer, because the manifest embeds its path.
    """
    root = tmp_path_factory.mktemp("shared-plan")
    repository = _copy_changed_repository(_committed_template, root / "repository")
    out = root / "review"
    planned = _plan(repository, out)
    assert planned.returncode == 0, planned.stderr
    return repository, out


@pytest.fixture
def planned_review(
    _shared_plan: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Return the shared read-only repository and a private writable copy of its planned review."""
    repository, planned = _shared_plan
    out = tmp_path / "review"
    shutil.copytree(planned, out)
    monkeypatch.chdir(repository)
    return repository, out


def _prior_ledger() -> dict[str, object]:
    """Return a structurally valid earlier review with one open signature."""
    snapshot = {"revision": "test-revision", "diff_digest": "a" * 64}
    return {
        "schema_version": 1,
        "implementation_author": "solver",
        "current_snapshot": snapshot,
        "rounds": [
            {
                "index": 1,
                "reviewer": {"identity": "challenger", "independent": True},
                "snapshot": snapshot,
                "report_path": "review.json",
                "findings": [
                    {
                        "signature": "caller-origin",
                        "tier": "high",
                        "structural": False,
                        "disposition": "open",
                        "evidence": ["Caller record absent"],
                    }
                ],
            }
        ],
    }


def _write_prior(path: Path, ledger: dict[str, object], *, split: bool) -> None:
    """Write a caller ledger as a historical inline file or a current header plus append-only round log."""
    if not split:
        path.write_text(json.dumps(ledger), encoding="utf-8", newline="\n")
        return
    header = {key: value for key, value in ledger.items() if key != "rounds"} | {"schema_version": 2}
    path.write_text(json.dumps(header), encoding="utf-8", newline="\n")
    rounds = ledger["rounds"]
    assert isinstance(rounds, list)
    path.with_name("loop-rounds.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in rounds), encoding="utf-8", newline="\n"
    )


def _coverage_entry(
    source_paths: tuple[str, ...] = ("alpha.py",), chunk_ids: tuple[str, ...] = ("chunk-001",)
) -> dict[str, object]:
    """Return one prior-coverage record assigning the fixture signature to chunk reviews."""
    return {
        "signature": "caller-origin",
        "source_paths": list(source_paths),
        "review_kind": "chunk",
        "chunk_ids": list(chunk_ids),
    }


def _write_coverage(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    """Write a prior-coverage file and return its path."""
    path = tmp_path / "coverage.json"
    write_json(path, entries)
    return path


@pytest.fixture
def caller_run(tmp_path: Path) -> Path:
    """Return a caller run directory holding an inline `loop-ledger.json` with one open finding."""
    run = tmp_path / "caller-run"
    run.mkdir()
    write_json(run / "loop-ledger.json", _prior_ledger())
    return run


def _plan_continuation(
    repository: Path, out: Path, caller_run: Path, coverage: Path, request: tuple[str, ...] = REQUEST_ARGS
) -> subprocess.CompletedProcess[str]:
    """Plan a continuation review that reassesses the caller ledger's open finding."""
    prior = ("--prior-ledger", str(caller_run / "loop-ledger.json"), "--prior-coverage", str(coverage))
    return _plan(repository, out, *prior, "--caller-run", str(caller_run), request=request)


@pytest.fixture(scope="module")
def _shared_continuation_plan(
    _committed_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, Path, Path]:
    """Plan a continuation of the fixture finding once per module; return repository, review, and caller run.

    The repository and caller run stay read-only for every consumer, because the manifest embeds their paths.
    """
    root = tmp_path_factory.mktemp("continuation-plan")
    repository = _copy_changed_repository(_committed_template, root / "repository")
    caller = root / "caller-run"
    caller.mkdir()
    write_json(caller / "loop-ledger.json", _prior_ledger())
    out = root / "review"
    planned = _plan_continuation(repository, out, caller, _write_coverage(root, [_coverage_entry()]))
    assert planned.returncode == 0, planned.stderr
    return repository, out, caller


@pytest.fixture
def continuation_plan(
    _shared_continuation_plan: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Return a private writable copy of the planned continuation review and its read-only caller run."""
    repository, planned, caller = _shared_continuation_plan
    out = tmp_path / "continued-review"
    shutil.copytree(planned, out)
    monkeypatch.chdir(repository)
    return out, caller


class TestContinuationPlanning:
    """Planning a resumed review binds the caller ledger and its prior-finding assignments."""

    @pytest.mark.packaging
    def test_declared_continuation_requires_prior_ledger(
        self, changed_repository: Path, tmp_path: Path, caller_run: Path
    ) -> None:
        """A declared resumed review cannot silently discard its caller's findings.

        `--caller-run` is given without `--prior-ledger`; planning must refuse instead of starting a first-run review.
        """
        planned = _plan(changed_repository, tmp_path / "continued-review", "--caller-run", str(caller_run))

        assert planned.returncode != 0
        assert "prior-ledger-required-for-continuation" in planned.stderr

    @pytest.mark.packaging
    def test_checker_rejects_declared_continuation_without_prior_ledger(
        self, changed_repository: Path, tmp_path: Path, caller_run: Path
    ) -> None:
        """A continuation declaration cannot pass source checking with null prior evidence.

        A first-run manifest is edited after planning to claim it continues a caller run; `check` must notice the
        manifest carries no prior-ledger digest.
        """
        fresh_out = tmp_path / "fresh-review"
        assert _plan(changed_repository, fresh_out).returncode == 0
        manifest_path = fresh_out / "chunks.json"
        manifest = read_json(manifest_path)
        manifest["origin"] = {"kind": "continuation", "caller_run": str(caller_run)}
        write_json(manifest_path, manifest)

        checked = _check_cli(manifest_path)

        assert checked.returncode != 0
        assert "prior-ledger-required-for-continuation" in checked.stderr

    @pytest.mark.packaging
    @pytest.mark.parametrize(
        "coverage",
        [
            pytest.param([], id="empty-coverage"),
            pytest.param([_coverage_entry(("beta.py", "alpha.py"))], id="unsorted-source-paths"),
            pytest.param([_coverage_entry(("C:caller.py",))], id="drive-relative-path"),
            pytest.param([_coverage_entry(("caller\x00.py",))], id="nul-in-path"),
        ],
    )
    def test_rejects_invalid_prior_coverage(
        self, changed_repository: Path, tmp_path: Path, caller_run: Path, coverage: list[dict[str, object]]
    ) -> None:
        """Planning refuses coverage that omits the prior signature or lists noncanonical source paths.

        Each case is a different way the caller can hand over unusable assignments; none may reach chunk output.
        """
        out = tmp_path / "review"

        planned = _plan_continuation(changed_repository, out, caller_run, _write_coverage(tmp_path, coverage))

        assert planned.returncode != 0
        assert "prior-coverage-invalid" in planned.stderr
        assert not out.exists()

    @pytest.mark.packaging
    def test_records_prior_findings_in_manifest_and_request(self, continuation_plan: tuple[Path, Path]) -> None:
        """The manifest names the caller run and appends the prior finding to the review specification.

        A resumed review must tell the child reviewer which earlier signatures to reassess.
        """
        out, caller_run = continuation_plan

        manifest = read_json(out / "chunks.json")

        assert manifest["schema_version"] == 1
        assert manifest["origin"] == {"kind": "continuation", "caller_run": str(caller_run)}
        assert manifest["prior_findings"]["coverage"][0]["signature"] == "caller-origin"
        assert manifest["request"]["specification"] == _prior_specification()

    @pytest.mark.packaging
    def test_replanning_with_recorded_specification_keeps_it(
        self, changed_repository: Path, tmp_path: Path, caller_run: Path
    ) -> None:
        """Passing the already-expanded specification back does not append the prior-findings block twice.

        A continuation is planned again with the specification the first plan recorded, as a resumed run would. The
        manifest must hold that text unchanged, because a doubled block would no longer match the caller's ledger.
        """
        coverage = _write_coverage(tmp_path, [_coverage_entry()])
        out = tmp_path / "repeated-review"

        replanned = _plan_continuation(
            changed_repository, out, caller_run, coverage, request=_request_args(_prior_specification())
        )

        assert replanned.returncode == 0, replanned.stderr
        assert read_json(out / "chunks.json")["request"]["specification"] == _prior_specification()

    @pytest.mark.packaging
    def test_check_rejects_unsupported_schema_version_on_continuation(
        self, continuation_plan: tuple[Path, Path]
    ) -> None:
        """A continuation manifest claiming an unknown schema version is rejected as invalid.

        A valid planned continuation has its `schema_version` rewritten to 5 on disk. `check` must refuse it with
        `chunk-manifest-invalid` rather than trying to read a layout it does not know.
        """
        out, _ = continuation_plan
        manifest = read_json(out / "chunks.json")
        write_json(out / "chunks.json", manifest | {"schema_version": 5})

        rejected = _check_cli(out / "chunks.json")

        assert rejected.returncode != 0
        assert "chunk-manifest-invalid" in rejected.stderr

    @pytest.mark.packaging
    def test_check_rejects_continuation_whose_request_lost_prior_block(
        self, continuation_plan: tuple[Path, Path]
    ) -> None:
        """Resetting the specification to its plain text removes the prior-findings block and fails the check.

        A planned continuation has its recorded specification reset to the original plain text. `check` must report
        `prior-request-block-missing`, since the child reviewer would no longer be told which findings to reassess.
        """
        out, _ = continuation_plan
        manifest = read_json(out / "chunks.json")
        manifest["request"]["specification"] = "Fixture contract"
        write_json(out / "chunks.json", manifest)

        tampered = _check_cli(out / "chunks.json")

        assert tampered.returncode != 0
        assert "prior-request-block-missing" in tampered.stderr

    @pytest.mark.packaging
    @pytest.mark.parametrize(
        "split", [pytest.param(False, id="historical-inline"), pytest.param(True, id="current-split")]
    )
    def test_check_rechecks_caller_ledger_bytes(
        self, changed_repository: Path, tmp_path: Path, caller_run: Path, split: bool
    ) -> None:
        """A copied prior ledger cannot certify a caller ledger changed after planning.

        Both ledger layouts are pinned: a historical inline file by its bytes, and a current header with its round log
        by the framed digest of both files, so editing a recorded round after planning is caught like editing the
        header. The lifecycle (plan, clean check, edit, delete) is one contract and is kept in one test on purpose.
        """
        prior = caller_run / "loop-ledger.json"
        _write_prior(prior, _prior_ledger(), split=split)
        out = tmp_path / "review"
        planned = _plan_continuation(
            changed_repository, out, caller_run, _write_coverage(tmp_path, [_coverage_entry()])
        )
        assert planned.returncode == 0, planned.stderr
        manifest_path = out / "chunks.json"
        unchanged = _check_cli(manifest_path)
        changed = _prior_ledger()
        changed["rounds"][0]["findings"][0]["evidence"] = ["Caller record still absent"]
        _write_prior(prior, changed, split=split)
        edited = _check_cli(manifest_path)
        prior.unlink()
        removed = _check_cli(manifest_path)

        assert unchanged.returncode == 0
        assert not (out / "loop-rounds.jsonl").exists()
        assert (out / "prior-loop-rounds.jsonl").is_file() is split
        assert edited.returncode != 0
        assert "caller-ledger-changed" in edited.stderr
        assert removed.returncode != 0
        assert "caller-ledger-unavailable" in removed.stderr


class TestChunkPlanning:
    """Planning splits changed source into bounded chunks and keeps every file visible."""

    @pytest.mark.packaging
    def test_fresh_plan_without_prior_ledger(self, changed_repository: Path, tmp_path: Path) -> None:
        """A first review can plan and validate without an earlier ledger.

        The changed fixture repository is planned with no prior ledger. The manifest must record empty prior findings
        with null ledger path and digest, and the produced plan must pass its own `check`.
        """
        out = tmp_path / "fresh-review"

        fresh = _plan(changed_repository, out)

        assert fresh.returncode == 0, fresh.stderr
        manifest = read_json(out / "chunks.json")
        assert manifest["schema_version"] == 1
        assert manifest["prior_findings"] == {"ledger_path": None, "ledger_sha256": None, "coverage": []}
        assert _check_cli(out / "chunks.json").returncode == 0

    @pytest.mark.packaging
    @pytest.mark.parametrize("scope_mode", ["all", "changed"])
    def test_retains_staged_deletion(self, changed_repository: Path, tmp_path: Path, scope_mode: str) -> None:
        """Include a HEAD file removed from the index in each requested scope mode.

        `removed.py` is deleted with `git rm` and then scoped explicitly in `all` and `changed` mode. Either mode must
        list it, capture it as a missing file with a deletion diff, and produce a plan that passes `check`.
        """
        subprocess.run(["git", "rm", "removed.py"], cwd=changed_repository, check=True, capture_output=True)
        out = tmp_path / f"review-{scope_mode}"

        planned = _plan(changed_repository, out, "--scope-path", "removed.py", "--scope-mode", scope_mode)

        assert planned.returncode == 0, planned.stderr
        manifest_path = out / "chunks.json"
        manifest = read_json(manifest_path)
        assert manifest["inventory"] == ["removed.py"]
        chunk = manifest["chunks"][0]
        source = read_json(out / chunk["source_path"])
        assert source["files"] == [
            {
                "path": "removed.py",
                "kind": "missing",
                "sha256": None,
                "executable": False,
                "encoding": "utf-8",
                "content": "",
            }
        ]
        assert b"deleted file mode" in (out / chunk["diff_path"]).read_bytes()
        assert _check_cli(manifest_path).returncode == 0

    @pytest.mark.packaging
    def test_covers_changed_files_without_overlap(self, planned_review: tuple[Path, Path]) -> None:
        """A size split retains deletion and untracked bytes, and no path lands in two chunks.

        The shared plan covers two edits, a deletion and an untracked file under a small byte budget. The chunks must
        partition the inventory exactly once, include the untracked file's bytes, and pass `check`.
        """
        _, out = planned_review

        manifest = read_json(out / "chunks.json")

        assert manifest["inventory"] == ["alpha.py", "beta.py", "new.py", "removed.py"]
        assert len(manifest["chunks"]) >= 2
        assert sorted(path for chunk in manifest["chunks"] for path in chunk["scope_paths"]) == manifest["inventory"]
        assert any(
            record["path"] == "new.py"
            for chunk in manifest["chunks"]
            for record in read_json(out / chunk["source_path"])["files"]
        )
        assert _check_cli(out / "chunks.json").returncode == 0

    @pytest.mark.packaging
    @pytest.mark.parametrize("schema_version", [True, 2, 3, 4, 5])
    def test_check_rejects_unsupported_schema_version(
        self, planned_review: tuple[Path, Path], schema_version: object
    ) -> None:
        """A manifest claiming a schema version the checker does not support is rejected as invalid.

        The planned manifest is rewritten with `True`, 2, 3, 4 or 5 as its version. Each must fail with `chunk-manifest-
        invalid`; a boolean in particular must not pass as the integer 1.
        """
        _, out = planned_review
        manifest = read_json(out / "chunks.json")
        write_json(out / "chunks.json", manifest | {"schema_version": schema_version})

        rejected = _check_cli(out / "chunks.json")

        assert rejected.returncode != 0
        assert "chunk-manifest-invalid" in rejected.stderr

    @pytest.mark.packaging
    def test_check_rejects_source_changed_after_planning(self, changed_repository: Path, tmp_path: Path) -> None:
        """Editing a planned file after planning makes the recorded source stale and fails the check.

        A valid plan is made, then `alpha.py` is edited. `check` re-reads the repository and must report `chunk-source-
        changed`, so reviews of old source cannot be presented as reviews of the current tree.
        """
        out = tmp_path / "review"
        assert _plan(changed_repository, out).returncode == 0
        (changed_repository / "alpha.py").write_text("VALUE = 'later change'\n", encoding="utf-8", newline="\n")

        stale = _check_cli(out / "chunks.json")

        assert stale.returncode != 0
        assert "chunk-source-changed" in stale.stderr

    @pytest.mark.packaging
    def test_oversized_file_stays_visible_beside_smaller_files(self, changed_repository: Path, tmp_path: Path) -> None:
        """An oversized single file remains visible while smaller files get chunks.

        `alpha.py` is made far larger than the byte budget. It must appear in the inventory as its own oversize chunk
        instead of being dropped, with the other files still chunked and the plan passing `check`.
        """
        (changed_repository / "alpha.py").write_text("X = '" + "a" * 4000 + "'\n", encoding="utf-8", newline="\n")
        out = tmp_path / "review"

        planned = _plan(changed_repository, out)

        assert planned.returncode == 0, planned.stderr
        manifest = read_json(out / "chunks.json")
        assert manifest["inventory"] == ["alpha.py", "beta.py", "new.py", "removed.py"]
        assert [chunk["scope_paths"] for chunk in manifest["chunks"] if chunk["oversize"]] == [["alpha.py"]]
        assert _check_cli(out / "chunks.json").returncode == 0

    @pytest.mark.packaging
    def test_all_scope_mode_covers_unchanged_plugin_files(self, changed_repository: Path, tmp_path: Path) -> None:
        """A whole plugin review must retain unchanged tracked source too.

        A committed, unmodified `plugin/entry.py` is planned in `all` scope mode. It must be inventoried with an empty
        diff and its full source bytes, since a diff-only plan would review nothing of an unchanged plugin.
        """
        plugin = changed_repository / "plugin"
        plugin.mkdir()
        (plugin / "entry.py").write_text("VALUE = 'stable'\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "plugin/entry.py"], cwd=changed_repository, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "plugin"],
            cwd=changed_repository,
            check=True,
            capture_output=True,
        )
        out = tmp_path / "plugin-review"

        planned = _plan(changed_repository, out, "--scope-path", "plugin", "--scope-mode", "all")

        assert planned.returncode == 0, planned.stderr
        manifest = read_json(out / "chunks.json")
        assert manifest["inventory"] == ["plugin/entry.py"]
        chunk = manifest["chunks"][0]
        assert (out / chunk["diff_path"]).read_bytes() == b""
        assert chunk["diff_sha256"] == hashlib.sha256(b"").hexdigest()
        assert b"VALUE = 'stable'" in (out / chunk["source_path"]).read_bytes()
        assert _check_cli(out / "chunks.json").returncode == 0

    @pytest.mark.packaging
    @pytest.mark.parametrize("subdirectory", [pytest.param("", id="root"), pytest.param("nested", id="subdirectory")])
    def test_preserves_trailing_space_in_repository_root(
        self, changed_repository: Path, tmp_path: Path, subdirectory: str
    ) -> None:
        """Use the real root name when planning from a root or its subdirectory.

        The repository directory name ends in a space, which naive path normalisation would strip.
        """
        repository = changed_repository.rename(tmp_path / "repository ")
        physical_repository = repository.resolve(strict=True)
        (physical_repository / "nested").mkdir()
        out = tmp_path / "review"

        planned = _plan(physical_repository / subdirectory, out)

        assert planned.returncode == 0, planned.stderr
        assert read_json(out / "chunks.json")["repository"] == physical_repository.as_posix()
        assert _check_cli(out / "chunks.json").returncode == 0


class TestPriorFindingResults:
    """Child results must keep each caller finding's severity, structure, and source record."""

    @pytest.mark.parametrize(
        ("path", "expected_paths", "expected_kind", "expected_issue"),
        [
            pytest.param("missing.py", {"alpha.py"}, None, "prior-source-coverage-incomplete", id="absent-path"),
            pytest.param("removed.py", {"alpha.py", "removed.py"}, "missing", None, id="tracked-deletion"),
        ],
    )
    def test_source_record_must_capture_the_finding_path(
        self,
        changed_repository: Path,
        tmp_path: Path,
        chunker: ModuleType,
        path: str,
        expected_paths: set[str],
        expected_kind: str | None,
        expected_issue: str | None,
    ) -> None:
        """Only a captured file or tracked-deletion record satisfies prior source coverage.

        A path that git cannot show is never silently treated as covered; a deletion counts because its absence is the
        reviewed state.
        """
        run = tmp_path / "runs" / "chunk-001"
        run.mkdir(parents=True)
        snapshot = chunker.capture_source_snapshot(changed_repository, ["alpha.py", path])
        (run / "current-source.json").write_text(
            json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
        )
        (run / "result.json").write_text('{"status":"pass"}', encoding="utf-8", newline="\n")
        write_json(
            run / "loop-evidence.json",
            {"scope_paths": ["alpha.py", path], "current_source_path": "current-source.json", "supporting_paths": []},
        )
        ledger = _prior_ledger()
        ledger["rounds"][0]["findings"][0]["disposition"] = "verified-fixed"
        write_json(run / "loop-ledger.json", ledger)
        item = _coverage_entry((path,))
        results = {
            "chunks": [{"id": "chunk-001", "result_path": "runs/chunk-001/result.json"}],
            "interactions": [],
            "groups": [],
        }

        issue = chunker._prior_result_issue(tmp_path, results, item, _prior_ledger()["rounds"][0]["findings"][0])

        assert {record["path"] for record in snapshot["files"]} == expected_paths
        assert {record["path"]: record["kind"] for record in snapshot["files"]}.get(path) == expected_kind
        assert issue == expected_issue

    @pytest.mark.packaging
    def test_child_cannot_reclassify_verified_signature(
        self, changed_repository: Path, tmp_path: Path, chunker: ModuleType
    ) -> None:
        """A child disposition cannot silently lower a caller finding's severity or structural status.

        The caller recorded a structural high finding; the child ledger claims it is a fixed non-structural low one,
        which is a valid ledger on its own but contradicts the caller's attribution.
        """
        prior = _prior_ledger()
        prior["rounds"][0]["findings"][0]["structural"] = True
        write_json(tmp_path / "prior-loop-ledger.json", prior)
        child = copy.deepcopy(prior)
        child["rounds"][0]["findings"][0].update(tier="low", structural=False, disposition="verified-fixed")
        run = tmp_path / "runs" / "child"
        run.mkdir(parents=True)
        write_json(run / "loop-ledger.json", child)
        source = chunker.capture_source_snapshot(changed_repository, ["alpha.py"])
        (run / "current-source.json").write_text(
            json.dumps(source, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
        )
        write_json(
            run / "loop-evidence.json", {"scope_paths": ["alpha.py"], "current_source_path": "current-source.json"}
        )
        write_json(run / "result.json", {})
        manifest = {"prior_findings": {"ledger_path": "prior-loop-ledger.json", "coverage": [_coverage_entry()]}}
        results_path = tmp_path / "results.json"
        write_json(
            results_path,
            {
                "chunks": [{"id": "chunk-001", "result_path": "runs/child/result.json"}],
                "interactions": [],
                "groups": [],
            },
        )

        with pytest.raises(ValueError, match="prior-finding-attribution-mismatch:caller-origin"):
            chunker._check_prior_results(tmp_path, manifest, results_path)

        assert chunker.validate_ledger(child) == []


def _results_map(
    chunks: list[dict[str, object]], interactions: list[dict[str, object]] | None = None
) -> dict[str, object]:
    """Return a schema-1 result map naming each chunk's result file, with optional interaction decisions."""
    return {
        "schema_version": 1,
        "chunks": [{"id": chunk["id"], "result_path": f"runs/{chunk['id']}/result.json"} for chunk in chunks],
        "interactions": interactions or [],
        "groups": [],
    }


class TestResultMaps:
    """A result map must cover every chunk and every chunk pair with independent assessments."""

    @pytest.mark.packaging
    def test_rejects_clean_claim_without_each_chunk(self, planned_review: tuple[Path, Path]) -> None:
        """Reject a final coverage claim that omits a reviewed chunk.

        A result map with an empty chunk list is checked against a plan that has several chunks. `check` must fail with
        `chunk-results-invalid`, since a clean claim must name every chunk it covers.
        """
        _, out = planned_review
        results = out / "results.json"
        results.write_text('{"schema_version":1,"chunks":[]}\n', encoding="utf-8", newline="\n")

        checked = _check_cli(out / "chunks.json", "--results", str(results))

        assert checked.returncode != 0
        assert "chunk-results-invalid" in checked.stderr

    @pytest.mark.packaging
    def test_requires_independent_cross_chunk_reviews(self, planned_review: tuple[Path, Path]) -> None:
        """A clean result per file chunk cannot establish review of their interactions.

        The result map lists every chunk but no cross-chunk decisions, for a plan of at least two chunks. `check` must
        fail with `chunk-interaction-coverage-invalid`, because bugs between files can hide from per-file reviews.
        """
        _, out = planned_review
        chunks = read_json(out / "chunks.json")["chunks"]
        results = out / "results.json"
        write_json(results, _results_map(chunks))

        checked = _check_cli(out / "chunks.json", "--results", str(results))

        assert len(chunks) >= 2
        assert checked.returncode != 0
        assert "chunk-interaction-coverage-invalid" in checked.stderr

    @pytest.mark.packaging
    def test_no_interaction_decision_requires_source_rationale(self, planned_review: tuple[Path, Path]) -> None:
        """An empty no-interaction assertion cannot waive independent pair review.

        Every chunk pair carries a `no-interaction` decision whose evidence is only whitespace. `check` must fail with
        `chunk-interaction-decision-invalid`, since waiving a pair needs a stated source-based reason.
        """
        _, out = planned_review
        chunks = read_json(out / "chunks.json")["chunks"]
        interactions = [
            {
                "chunk_ids": [left["id"], right["id"]],
                "decision": "no-interaction",
                "evidence": "  ",
                "result_path": "runs/interactions/result.json",
            }
            for left, right in itertools.combinations(chunks, 2)
        ]
        results = out / "results.json"
        write_json(results, _results_map(chunks, interactions))

        checked = _check_cli(out / "chunks.json", "--results", str(results))

        assert len(chunks) >= 2
        assert checked.returncode != 0
        assert "chunk-interaction-decision-invalid" in checked.stderr

    @pytest.mark.packaging
    def test_parent_interaction_claims_are_not_independent_assessment(
        self, planned_review: tuple[Path, Path], chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject plausible parent decisions without an authenticated interaction assessment.

        Every pair carries a `no-interaction` decision with a rationale but no child result, so the parent alone vouches
        for it.
        """
        _, out = planned_review
        chunks = read_json(out / "chunks.json")["chunks"]
        results = out / "results.json"
        write_json(
            results,
            _results_map(chunks)
            | {"schema_version": 2}
            | {
                "interactions": [
                    {
                        "chunk_ids": [left["id"], right["id"]],
                        "decision": "no-interaction",
                        "evidence": "Parent claims no shared behavior",
                    }
                    for left, right in itertools.combinations(chunks, 2)
                ]
            },
        )
        monkeypatch.setattr(chunker, "_check_clean_run", lambda *args: None)

        with pytest.raises(ValueError, match="chunk-results-invalid"):
            chunker.check(out / "chunks.json", results)

        assert len(chunks) >= 3


def _stub_artifact_validator(monkeypatch: pytest.MonkeyPatch, chunker: ModuleType) -> list[list[str]]:
    """Replace only the separate artifact validator subprocess; return its recorded commands.

    Git inspection keeps running for real because the chunker's source checks are the contract under test.
    """
    calls: list[list[str]] = []
    real_run = chunker.subprocess.run

    def validate_or_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Record the public artifact validator while preserving local Git inspection."""
        if len(command) > 1 and Path(command[1]).name == "validate-artifacts.py":
            assert isinstance(kwargs.get("timeout"), int)
            assert 0 < kwargs["timeout"] <= 60
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return real_run(command, **kwargs)

    monkeypatch.setattr(chunker.subprocess, "run", validate_or_run)
    return calls


def _stub_clean_run(monkeypatch: pytest.MonkeyPatch, chunker: ModuleType, reason: str, scores: list[int]) -> None:
    """Make every child run look like it finished with `reason` and `scores`."""
    monkeypatch.setattr(chunker, "_check_clean_run", lambda *args, **kwargs: {"reason": reason, "scores": scores})


def _evidence(repository: Path, scope_paths: list[str], request: dict[str, object]) -> dict[str, object]:
    """Return schema-2 loop evidence tying a child run to its source and coordinator request."""
    return {
        "schema_version": 2,
        "repository": repository.resolve().as_posix(),
        "scope_paths": scope_paths,
        "current_source_path": "current-source.json",
        "request": request,
    }


def _stopped_result(reason: str = "plateau", scores: tuple[int, ...] = (6,)) -> dict[str, object]:
    """Return a failed child result whose adversarial loop stopped for `reason` after one round per score."""
    rounds = [{"decision": "plateau"} for _ in scores]
    return {
        "status": "fail",
        "metadata": {
            "adversarial_loop": {"status": "stopped", "reason": reason, "scores": list(scores), "rounds": rounds}
        },
    }


@dataclasses.dataclass
class StoppedReview:
    """A planned review whose first chunk has a failed child run and whose other chunks are unreviewed."""

    repository: Path
    out: Path
    chunks: list[dict[str, object]]
    run: Path
    validator_calls: list[list[str]]

    @property
    def manifest_path(self) -> Path:
        """Return the planned manifest."""
        return self.out / "chunks.json"

    @property
    def results_path(self) -> Path:
        """Return the result map naming only the first chunk."""
        return self.out / "results.json"

    @property
    def result_path(self) -> Path:
        """Return the failed first-chunk result."""
        return self.run / "result.json"

    @property
    def evidence_path(self) -> Path:
        """Return the first-chunk loop evidence."""
        return self.run / "loop-evidence.json"


@pytest.fixture
def stopped_review(
    planned_review: tuple[Path, Path], chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> StoppedReview:
    """Stage a failed child run for the first chunk and stub the separate artifact validator."""
    repository, out = planned_review
    manifest = read_json(out / "chunks.json")
    chunks = manifest["chunks"]
    first = chunks[0]
    run = out / "runs" / first["id"]
    run.mkdir(parents=True)
    (run / "current-source.json").write_bytes((out / first["source_path"]).read_bytes())
    (run / "current.diff").write_bytes((out / first["diff_path"]).read_bytes())
    write_json(run / "loop-evidence.json", _evidence(repository, first["scope_paths"], manifest["request"]))
    write_json(run / "result.json", _stopped_result())
    write_json(out / "results.json", _results_map(chunks[:1]))
    calls = _stub_artifact_validator(monkeypatch, chunker)
    assert len(chunks) >= 3
    return StoppedReview(repository, out, chunks, run, calls)


@pytest.mark.packaging
class TestCheckStopped:
    """A stopped review keeps the failed child visible and reports exactly what is still unreviewed."""

    def test_summary_binds_failed_child_and_lists_pending_slots(
        self, stopped_review: StoppedReview, chunker: ModuleType
    ) -> None:
        """A failed child remains visible while unreviewed chunks and interactions stay pending.

        Only the first chunk has a stopped, failed child run. The summary must bind the manifest and result hashes, list
        that run with its plateau scores, and name every other chunk and every chunk pair as still pending.
        """
        review = stopped_review
        ids = [chunk["id"] for chunk in review.chunks]

        summary = chunker.check_stopped(review.manifest_path, review.results_path)

        assert summary == {
            "schema_version": 1,
            "status": "stopped",
            "manifest_sha256": hashlib.sha256(review.manifest_path.read_bytes()).hexdigest(),
            "results_sha256": hashlib.sha256(review.results_path.read_bytes()).hexdigest(),
            "runs": [
                {"result_path": f"runs/{ids[0]}/result.json", "scores": [6], "decision": "plateau", "reason": "plateau"}
            ],
            "pending_chunks": ids[1:],
            "pending_interactions": [list(pair) for pair in itertools.combinations(ids, 2)],
            "pending_groups": [],
            "pending_prior_signatures": [],
        }
        assert len(review.validator_calls) == 1

    def test_rejects_stopped_run_with_no_review_round(self, stopped_review: StoppedReview, chunker: ModuleType) -> None:
        """A stopped child that never completed a review round cannot count as reviewed.

        The child result stopped for lack of an independent reviewer and recorded no scores. `check_stopped` must fail
        with `chunk-result-review-missing`, because a run that reviewed nothing may not hide its chunk from pending.
        """
        write_json(stopped_review.result_path, _stopped_result("independence-unavailable", scores=()))

        with pytest.raises(ValueError, match="chunk-result-review-missing"):
            chunker.check_stopped(stopped_review.manifest_path, stopped_review.results_path)

    def test_rejects_child_with_different_request(self, stopped_review: StoppedReview, chunker: ModuleType) -> None:
        """A child run that reviewed another goal cannot stand in for the coordinator request.

        The first chunk's loop evidence records an unrelated goal. `check_stopped` must fail with `chunk-result-request-
        mismatch`, so a review of the wrong question never counts toward this one.
        """
        evidence = read_json(stopped_review.evidence_path)
        evidence["request"]["goal"] = "unrelated review"
        write_json(stopped_review.evidence_path, evidence)

        with pytest.raises(ValueError, match="chunk-result-request-mismatch"):
            chunker.check_stopped(stopped_review.manifest_path, stopped_review.results_path)

    def test_cli_prints_the_same_summary(
        self,
        stopped_review: StoppedReview,
        chunker: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`check-stopped --summary` prints the summary that `check_stopped` returns.

        The command line entry is run in process against the stopped review. It must exit 0 and print JSON equal to the
        function result, so scripted callers and library callers see the same pending lists.
        """
        expected = chunker.check_stopped(stopped_review.manifest_path, stopped_review.results_path)
        argv = [str(CHUNKER), "check-stopped", "--manifest", str(stopped_review.manifest_path)]
        monkeypatch.setattr(sys, "argv", [*argv, "--results", str(stopped_review.results_path), "--summary"])

        status = chunker.main()

        assert status == 0
        assert json.loads(capsys.readouterr().out) == expected

    def test_check_rejects_stopped_run_as_complete(self, stopped_review: StoppedReview, chunker: ModuleType) -> None:
        """The strict `check` refuses a result map that leaves chunks unreviewed.

        The same stopped review that `check_stopped` summarizes names only one of its chunks. The strict check must fail
        with `chunk-results-invalid`, since a stopped review is a progress report and never a finished one.
        """
        with pytest.raises(ValueError, match="chunk-results-invalid"):
            chunker.check(stopped_review.manifest_path, stopped_review.results_path)

    def test_lists_pending_interactions_and_groups(self, stopped_review: StoppedReview, chunker: ModuleType) -> None:
        """Interactions and groups without a result file stay pending even when a decision was recorded.

        One pair and one group carry a reviewed decision but a null result path. The summary must still list that pair
        and group as pending, because a decision with no child result is not an independent assessment.
        """
        ids = [chunk["id"] for chunk in stopped_review.chunks]
        pending_map = read_json(stopped_review.results_path)
        pending_map["interactions"] = [
            {
                "chunk_ids": ids[:2],
                "decision": "reviewed",
                "evidence": "The pair shares a contract, but review has not completed",
                "result_path": None,
            }
        ]
        pending_map["groups"] = [
            {
                "chunk_ids": ids[:3],
                "evidence": "The group shares a contract, but review has not completed",
                "result_path": None,
            }
        ]
        write_json(stopped_review.results_path, pending_map)

        summary = chunker.check_stopped(stopped_review.manifest_path, stopped_review.results_path)

        assert ids[:2] in summary["pending_interactions"]
        assert summary["pending_groups"] == [ids[:3]]

    def test_rejects_result_path_outside_review(self, stopped_review: StoppedReview, chunker: ModuleType) -> None:
        """A result path that climbs out of the review directory is refused.

        The result map points its first chunk at `../outside/result.json`. `check_stopped` must fail with `chunk-
        artifact-path-invalid` instead of reading evidence from outside the review it is checking.
        """
        escaping = read_json(stopped_review.results_path)
        escaping["chunks"][0]["result_path"] = "../outside/result.json"
        write_json(stopped_review.results_path, escaping)

        with pytest.raises(ValueError, match="chunk-artifact-path-invalid"):
            chunker.check_stopped(stopped_review.manifest_path, stopped_review.results_path)


@dataclasses.dataclass
class CleanReview:
    """A planned review in which every chunk, pair, and the full group have a clean child run."""

    repository: Path
    out: Path
    manifest: dict[str, object]
    result_map: dict[str, object]
    interaction_request: dict[str, object]
    decisions: list[dict[str, object]]
    paths: list[str]
    source: bytes
    validator_calls: list[list[str]] = dataclasses.field(default_factory=list)

    @property
    def chunks(self) -> list[dict[str, object]]:
        """Return the planned chunks."""
        return self.manifest["chunks"]

    @property
    def manifest_path(self) -> Path:
        """Return the planned manifest."""
        return self.out / "chunks.json"

    @property
    def results(self) -> Path:
        """Return the complete result map."""
        return self.out / "results.json"

    @property
    def interaction_run(self) -> Path:
        """Return the run directory of the pair and group assessments."""
        return self.out / "runs" / "interactions"

    def run(self, index: int = 0) -> Path:
        """Return the run directory of chunk `index`."""
        return self.out / "runs" / self.chunks[index]["id"]


def _interaction(left: dict[str, object], right: dict[str, object], reviewed: bool) -> dict[str, object]:
    """Return the decision for one chunk pair: independently reviewed, or a rationale that they never interact."""
    return {
        "chunk_ids": [left["id"], right["id"]],
        "decision": "reviewed" if reviewed else "no-interaction",
        "evidence": (
            "The declared changed files share an interface in this fixture"
            if reviewed
            else f"{left['scope_paths']} and {right['scope_paths']} have no shared interface in this fixture"
        ),
        "result_path": "runs/interactions/result.json",
    }


def _build_clean_review(repository: Path, out: Path) -> CleanReview:
    """Plan `repository` and stage clean child runs for all chunks, pairs, and the three-chunk group."""
    chunker = _load_chunker()
    planned = _run(
        "plan",
        *("--repository", str(repository), "--out", str(out), "--budget-bytes", "900"),
        *("--goal", _COORDINATOR_REQUEST["goal"], "--specification", _COORDINATOR_REQUEST["specification"]),
        *("--done-when", _COORDINATOR_REQUEST["done_when"]),
    )
    assert planned.returncode == 0, planned.stderr
    manifest = read_json(out / "chunks.json")
    chunks = manifest["chunks"]
    assert manifest["request"] == _COORDINATOR_REQUEST
    assert len(chunks) >= 3
    for chunk in chunks:
        run = out / "runs" / chunk["id"]
        run.mkdir(parents=True)
        (run / "current-source.json").write_bytes((out / chunk["source_path"]).read_bytes())
        (run / "current.diff").write_bytes((out / chunk["diff_path"]).read_bytes())
        write_json(run / "loop-evidence.json", _evidence(repository, chunk["scope_paths"], _COORDINATOR_REQUEST))
        (run / "result.json").write_text(_CLEAN_RESULT, encoding="utf-8", newline="\n")
    reviewed = {(left["id"], right["id"]) for left, right in list(itertools.combinations(chunks[:3], 2))[:2]}
    interactions = [
        _interaction(left, right, (left["id"], right["id"]) in reviewed)
        for left, right in itertools.combinations(chunks, 2)
    ]
    groups = [
        {
            "chunk_ids": [chunk["id"] for chunk in chunks[:3]],
            "evidence": "Three chunks jointly define the fixture contract",
            "result_path": "runs/interactions/result.json",
        }
    ]
    paths = sorted({path for chunk in chunks for path in chunk["scope_paths"]})
    assert len(paths) == len(manifest["inventory"])
    decisions = [{key: entry[key] for key in ("chunk_ids", "decision", "evidence")} for entry in interactions] + [
        {"chunk_ids": entry["chunk_ids"], "decision": "reviewed", "evidence": entry["evidence"]} for entry in groups
    ]
    interaction_request = _COORDINATOR_REQUEST | {
        "specification": _COORDINATOR_REQUEST["specification"]
        + "\nInteraction assessment:\n"
        + json.dumps(decisions, sort_keys=True, ensure_ascii=True)
    }
    run = out / "runs" / "interactions"
    run.mkdir(parents=True)
    source, diff = chunker._material(repository, paths)
    (run / "current-source.json").write_bytes(source)
    (run / "current.diff").write_bytes(diff)
    write_json(run / "loop-evidence.json", _evidence(repository, paths, interaction_request))
    (run / "result.json").write_text(_CLEAN_RESULT, encoding="utf-8", newline="\n")
    result_map = _results_map(chunks) | {"interactions": interactions, "groups": groups}
    write_json(out / "results.json", result_map)
    return CleanReview(repository, out, manifest, result_map, interaction_request, decisions, paths, source)


@pytest.fixture(scope="module")
def _shared_clean_review(_committed_template: Path, tmp_path_factory: pytest.TempPathFactory) -> CleanReview:
    """Plan and stage the clean review once per module; tests only ever copy the review directory."""
    root = tmp_path_factory.mktemp("clean-review")
    repository = _copy_changed_repository(_committed_template, root / "repository")
    return _build_clean_review(repository, root / "review")


@pytest.fixture
def clean_review(
    _shared_clean_review: CleanReview, tmp_path: Path, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> CleanReview:
    """Return a private writable copy of the clean review with the artifact validator stubbed."""
    review = copy.deepcopy(_shared_clean_review)
    review.out = tmp_path / "review"
    shutil.copytree(_shared_clean_review.out, review.out)
    review.validator_calls = _stub_artifact_validator(monkeypatch, chunker)
    monkeypatch.chdir(review.repository)
    return review


def _write_interaction_request(review: CleanReview, request: dict[str, object]) -> None:
    """Replace the request recorded by the pair and group assessment run."""
    write_json(review.interaction_run / "loop-evidence.json", _evidence(review.repository, review.paths, request))


@pytest.mark.packaging
class TestCleanReviewCheck:
    """`check` accepts a fully assessed review and rejects each way its evidence can be wrong."""

    def test_accepts_fully_assessed_review(self, clean_review: CleanReview, chunker: ModuleType) -> None:
        """Child and interaction reviews that retain the coordinator request and current source pass the check.

        Every chunk, pair and the group have clean runs on the current source. `check` must return a summary binding the
        manifest and result hashes and listing each run as clean with a final score of 0.
        """
        review = clean_review

        summary = chunker.check(review.manifest_path, review.results)

        assert summary == {
            "schema_version": 1,
            "manifest_sha256": hashlib.sha256(review.manifest_path.read_bytes()).hexdigest(),
            "results_sha256": hashlib.sha256(review.results.read_bytes()).hexdigest(),
            "runs": [
                {"result_path": entry["result_path"], "scores": [0], "decision": "clean"}
                for entry in review.result_map["chunks"]
            ]
            + [{"result_path": "runs/interactions/result.json", "scores": [0], "decision": "clean"}],
        }

    def test_cli_prints_the_same_summary(
        self,
        clean_review: CleanReview,
        chunker: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`check --summary` prints the summary that `check` returns.

        The command line entry is run in process against the clean review. It must exit 0 and print JSON equal to the
        function result, so scripted callers and library callers get the same verdict.
        """
        review = clean_review
        expected = chunker.check(review.manifest_path, review.results)
        argv = [str(CHUNKER), "check", "--manifest", str(review.manifest_path), "--results", str(review.results)]
        monkeypatch.setattr(sys, "argv", [*argv, "--summary"])

        status = chunker.main()

        assert status == 0
        assert json.loads(capsys.readouterr().out) == expected

    @pytest.mark.parametrize("schema_version", [True, 3])
    def test_rejects_unsupported_result_map_schema(
        self, clean_review: CleanReview, chunker: ModuleType, schema_version: object
    ) -> None:
        """A result map claiming an unsupported schema version is refused.

        A copy of the clean result map is written with `True` or 3 as its version. `check` must fail with `chunk-
        results-invalid`; a boolean in particular must not be accepted as a version number.
        """
        unsupported = clean_review.out / "unsupported-results.json"
        write_json(unsupported, clean_review.result_map | {"schema_version": schema_version})

        with pytest.raises(ValueError, match="chunk-results-invalid"):
            chunker.check(clean_review.manifest_path, unsupported)

    @pytest.mark.parametrize(
        ("result", "message"),
        [
            pytest.param(
                _CLEAN_RESULT.replace("[0]", "[7]"), "chunk-result-score-invalid:chunk-001", id="nonzero-final-score"
            ),
            pytest.param(
                '{"status":"fail","metadata":{"adversarial_loop":{"reason":"clean"}}}',
                "chunk-result-not-clean:chunk-001",
                id="failed-status",
            ),
        ],
    )
    def test_rejects_unclean_child_result(
        self, clean_review: CleanReview, chunker: ModuleType, result: str, message: str
    ) -> None:
        """A child result that is failed or ends on a nonzero score cannot support a clean review.

        The first chunk's result is replaced by one ending on score 7 or by one with `fail` status. `check` must name
        the chunk in `chunk-result-score-invalid` or `chunk-result-not-clean` rather than accept it as clean.
        """
        (clean_review.run() / "result.json").write_text(result, encoding="utf-8", newline="\n")

        with pytest.raises(ValueError, match=message):
            chunker.check(clean_review.manifest_path, clean_review.results)

    @pytest.mark.parametrize("field", ["goal", "specification", "done_when"])
    def test_rejects_child_with_substituted_request_field(
        self, clean_review: CleanReview, chunker: ModuleType, field: str
    ) -> None:
        """A chunk review that changed any part of the coordinator request is refused.

        The first chunk's evidence records a substituted goal, specification or done-when text. `check` must fail with
        `chunk-result-request-mismatch` for that chunk, whichever one of the three fields was altered.
        """
        evidence_path = clean_review.run() / "loop-evidence.json"
        evidence = read_json(evidence_path)
        evidence["request"] = _COORDINATOR_REQUEST | {field: f"Substituted {field}"}
        write_json(evidence_path, evidence)

        with pytest.raises(ValueError, match="chunk-result-request-mismatch:chunk-001"):
            chunker.check(clean_review.manifest_path, clean_review.results)

    def test_rejects_child_evidence_without_request(self, clean_review: CleanReview, chunker: ModuleType) -> None:
        """Evidence written before requests were recorded cannot prove which request a chunk answered.

        The first chunk's evidence is downgraded to schema 1 and its request removed. `check` must fail with `chunk-
        result-request-missing`, since old evidence cannot show which question the review answered.
        """
        evidence_path = clean_review.run() / "loop-evidence.json"
        evidence = read_json(evidence_path) | {"schema_version": 1}
        evidence.pop("request")
        write_json(evidence_path, evidence)

        with pytest.raises(ValueError, match="chunk-result-request-missing:chunk-001"):
            chunker.check(clean_review.manifest_path, clean_review.results)

    @pytest.mark.parametrize("field", ["goal", "done_when"])
    def test_rejects_interaction_review_with_substituted_request_field(
        self, clean_review: CleanReview, chunker: ModuleType, field: str
    ) -> None:
        """An interaction review that changed the goal or done-when text is refused.

        The pair and group run records a substituted goal or done-when text beside the real decisions. `check` must fail
        with `chunk-result-request-mismatch` naming the first pair of chunks.
        """
        _write_interaction_request(clean_review, clean_review.interaction_request | {field: f"Substituted {field}"})

        with pytest.raises(ValueError, match=r"chunk-result-request-mismatch:chunk-001\+chunk-002"):
            chunker.check(clean_review.manifest_path, clean_review.results)

    def test_rejects_interaction_review_with_substituted_contract(
        self, clean_review: CleanReview, chunker: ModuleType
    ) -> None:
        """An interaction review that kept the decisions but swapped the contract text is refused.

        The pair and group run restates every decision but under a different contract text. `check` must fail with
        `chunk-result-request-mismatch`, since the decisions were assessed against a contract the coordinator never set.
        """
        specification = "Substituted contract\nInteraction assessment:\n" + json.dumps(
            clean_review.decisions, sort_keys=True, ensure_ascii=True
        )
        _write_interaction_request(clean_review, clean_review.interaction_request | {"specification": specification})

        with pytest.raises(ValueError, match=r"chunk-result-request-mismatch:chunk-001\+chunk-002"):
            chunker.check(clean_review.manifest_path, clean_review.results)

    def test_rejects_interaction_review_with_stale_source(self, clean_review: CleanReview, chunker: ModuleType) -> None:
        """An interaction review whose recorded source differs from the current source is refused.

        The pair and group run's source snapshot is overwritten with unrelated bytes. `check` must fail with `chunk-
        result-source-mismatch`, because the interactions would have been judged against other code.
        """
        (clean_review.interaction_run / "current-source.json").write_bytes(b"stale")

        with pytest.raises(ValueError, match=r"chunk-result-source-mismatch:chunk-001\+chunk-002"):
            chunker.check(clean_review.manifest_path, clean_review.results)

    @pytest.mark.parametrize(
        "specification",
        [
            pytest.param(_COORDINATOR_REQUEST["specification"] + " without interaction decisions", id="no-assessment"),
            pytest.param(None, id="last-decision-dropped"),
        ],
    )
    def test_rejects_interaction_review_missing_a_decision(
        self, clean_review: CleanReview, chunker: ModuleType, specification: str | None
    ) -> None:
        """The interaction review must restate every recorded pair and group decision.

        The interaction run's specification either carries no assessment block or drops the final decision. `check` must
        fail with `chunk-interaction-assessment-missing`, so no recorded decision can go unreviewed.
        """
        dropped = json.dumps(clean_review.decisions[:-1], sort_keys=True, ensure_ascii=True)
        text = specification or _COORDINATOR_REQUEST["specification"] + "\nInteraction assessment:\n" + dropped
        _write_interaction_request(clean_review, clean_review.interaction_request | {"specification": text})

        with pytest.raises(ValueError, match="chunk-interaction-assessment-missing"):
            chunker.check(clean_review.manifest_path, clean_review.results)

    def test_rejects_interaction_entry_naming_one_chunk(self, clean_review: CleanReview, chunker: ModuleType) -> None:
        """A pair decision that lists a single chunk leaves the pair uncovered.

        The first interaction entry in the result map is cut down to one chunk id. `check` must fail with `chunk-
        interaction-coverage-invalid`, since that pair no longer has any decision.
        """
        clean_review.result_map["interactions"][0]["chunk_ids"] = [clean_review.chunks[0]["id"]]
        write_json(clean_review.results, clean_review.result_map)

        with pytest.raises(ValueError, match="chunk-interaction-coverage-invalid"):
            chunker.check(clean_review.manifest_path, clean_review.results)


def _verified_fixed_ledger(run: Path, *, round_diff: bytes) -> dict[str, object]:
    """Return a caller-style ledger whose finding is verified fixed in a round that reviewed `round_diff`."""
    revision = read_json(run / "current-source.json")["revision"]
    ledger = _prior_ledger()
    ledger["rounds"][0]["findings"] = [
        {
            "signature": "caller-origin",
            "disposition": "verified-fixed",
            "tier": "high",
            "structural": False,
            "evidence": ["Caller record now present"],
        }
    ]
    ledger["current_snapshot"] = {
        "revision": revision,
        "diff_digest": hashlib.sha256((run / "current.diff").read_bytes()).hexdigest(),
    }
    ledger["rounds"][0]["snapshot"] = {"revision": revision, "diff_digest": hashlib.sha256(round_diff).hexdigest()}
    return ledger


def _stage_prior_review(run: Path, *, round_diff: bytes, source_file: bytes, diff_file: bytes) -> dict[str, object]:
    """Write a verified-fixed child ledger plus the reviewed source and diff files its round points at."""
    ledger = _verified_fixed_ledger(run, round_diff=round_diff)
    write_json(run / "loop-ledger.json", ledger)
    (run / "source-1.json").write_bytes(source_file)
    (run / "round-1.diff").write_bytes(diff_file)
    evidence = read_json(run / "loop-evidence.json")
    evidence["rounds"] = [{"source_path": "source-1.json", "supporting_source_path": None}]
    write_json(run / "loop-evidence.json", evidence)
    return ledger


@pytest.fixture
def stale_alpha(
    _committed_template: Path, tmp_path: Path, clean_review: CleanReview, chunker: ModuleType
) -> tuple[bytes, bytes]:
    """Return the first chunk's source and diff as they were before `alpha.py` last changed."""
    earlier = _copy_changed_repository(_committed_template, tmp_path / "earlier")
    (earlier / "alpha.py").write_text("VALUE = 'earlier reviewed alpha'\n", encoding="utf-8", newline="\n")
    return chunker._material(earlier, clean_review.chunks[0]["scope_paths"])


@pytest.fixture
def continued_review(
    clean_review: CleanReview, tmp_path: Path, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> CleanReview:
    """Turn the clean review into a continuation of a caller run that left `caller-origin` open.

    Result-map validation is stubbed because the clean-review tests already cover it; these tests isolate how the prior
    finding is traced through each chunk's own ledger.
    """
    review = clean_review
    monkeypatch.setattr(chunker, "_check_results", lambda *args: ([], "validated-in-other-test"))
    prior_bytes = (json.dumps(_prior_ledger()) + "\n").encode()
    (review.out / "prior-loop-ledger.json").write_bytes(prior_bytes)
    caller = tmp_path / "prior"
    caller.mkdir()
    (caller / "loop-ledger.json").write_bytes(prior_bytes)
    review.manifest["prior_findings"] = {
        "ledger_path": "prior-loop-ledger.json",
        "ledger_sha256": hashlib.sha256(prior_bytes).hexdigest(),
        "coverage": [_coverage_entry(chunk_ids=(review.chunks[0]["id"],))],
    }
    review.manifest["origin"] = {"kind": "continuation", "caller_run": str(caller)}
    review.manifest["request"]["specification"] = _prior_specification(_COORDINATOR_REQUEST["specification"])
    write_json(review.manifest_path, review.manifest)
    return review


@pytest.mark.packaging
class TestContinuedReviewCheck:
    """A continuation is clean only when the child ledger resolves the caller's finding against current source."""

    def _pending(self, review: CleanReview, chunker: ModuleType) -> list[str]:
        """Return the prior signatures `check_stopped` still lists as pending."""
        return chunker.check_stopped(review.manifest_path, review.results)["pending_prior_signatures"]

    def test_stopped_summary_lists_prior_signature_without_child_ledger(
        self, continued_review: CleanReview, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every chunk, pair, and group is done, yet the caller finding stays pending until a ledger mentions it.

        All child runs are stubbed clean for a continuation, but no child ledger exists. The summary must show no
        pending chunks, interactions or groups and still list `caller-origin` as the one pending prior signature.
        """
        _stub_clean_run(monkeypatch, chunker, "clean", [0])

        summary = chunker.check_stopped(continued_review.manifest_path, continued_review.results)

        assert summary["pending_chunks"] == []
        assert summary["pending_interactions"] == []
        assert summary["pending_groups"] == []
        assert summary["pending_prior_signatures"] == ["caller-origin"]

    def test_stopped_summary_lists_prior_signature_for_empty_child_ledger(
        self, continued_review: CleanReview, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child ledger with no rounds cannot account for the caller finding.

        The first chunk has a ledger file but it records no review rounds. The finding must stay listed as pending,
        since an empty ledger says nothing about whether the caller's finding was reassessed.
        """
        _stub_clean_run(monkeypatch, chunker, "clean", [0])
        empty = _prior_ledger() | {"rounds": []}
        write_json(continued_review.run() / "loop-ledger.json", empty)

        pending = self._pending(continued_review, chunker)

        assert pending == ["caller-origin"]

    @pytest.mark.parametrize(
        "ledger",
        [
            pytest.param(_prior_ledger() | {"rounds": []}, id="no-rounds"),
            pytest.param(
                _prior_ledger() | {"rounds": [_prior_ledger()["rounds"][0] | {"findings": []}]}, id="no-findings"
            ),
        ],
    )
    def test_check_rejects_unresolved_prior_finding(
        self, continued_review: CleanReview, chunker: ModuleType, ledger: dict[str, object]
    ) -> None:
        """A clean check needs the child ledger to carry the caller's finding to a resolved disposition.

        The child ledger holds either no rounds or a round with no findings. `check` must fail with `prior-finding-not-
        resolved` for `caller-origin` instead of treating silence as resolution.
        """
        write_json(continued_review.run() / "loop-ledger.json", ledger)

        with pytest.raises(ValueError, match="prior-finding-not-resolved:caller-origin"):
            chunker.check(continued_review.manifest_path, continued_review.results)

    def test_check_accepts_finding_verified_fixed_against_current_source(
        self, continued_review: CleanReview, chunker: ModuleType
    ) -> None:
        """A verified-fixed finding whose round reviewed the current source and diff satisfies the check.

        The child ledger marks `caller-origin` fixed in a round whose source and diff equal the current ones. `check`
        must accept it, which is the one path by which a continuation can legitimately close a caller finding.
        """
        run = continued_review.run()
        _stage_prior_review(
            run,
            round_diff=(run / "current.diff").read_bytes(),
            source_file=(run / "current-source.json").read_bytes(),
            diff_file=(run / "current.diff").read_bytes(),
        )

        summary = chunker.check(continued_review.manifest_path, continued_review.results)

        assert summary is not None

    def test_stopped_summary_clears_prior_signature_when_reviewed_source_is_current(
        self, continued_review: CleanReview, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pending list empties once the verified round reviewed exactly the current source and diff.

        The child ledger verifies the finding fixed against the current source and diff. `check_stopped` must then
        report no pending prior signatures, even though the child run itself stopped on a plateau.
        """
        run = continued_review.run()
        _stub_clean_run(monkeypatch, chunker, "plateau", [6])
        current = (run / "current.diff").read_bytes()
        _stage_prior_review(
            run, round_diff=current, source_file=(run / "current-source.json").read_bytes(), diff_file=current
        )

        pending = self._pending(continued_review, chunker)

        assert pending == []

    @pytest.mark.parametrize(
        ("stale_round", "stale_source", "stale_diff"),
        [
            pytest.param(True, True, True, id="whole-round-reviewed-earlier-source"),
            pytest.param(False, True, False, id="source-file-stale"),
            pytest.param(False, False, True, id="diff-file-stale"),
        ],
    )
    def test_stopped_summary_keeps_prior_signature_when_reviewed_source_is_stale(
        self,
        continued_review: CleanReview,
        chunker: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        stale_alpha: tuple[bytes, bytes],
        stale_round: bool,
        stale_source: bool,
        stale_diff: bool,
    ) -> None:
        """A fixed verdict reached against an earlier source or diff cannot clear the finding for current source.

        The verified round, its source file, or its diff file is replaced by what `alpha.py` looked like before its last
        change. In each case `caller-origin` must stay pending, since the fix was never shown on today's code.
        """
        run = continued_review.run()
        _stub_clean_run(monkeypatch, chunker, "plateau", [6])
        earlier_source, earlier_diff = stale_alpha
        current_source = (run / "current-source.json").read_bytes()
        current_diff = (run / "current.diff").read_bytes()
        _stage_prior_review(
            run,
            round_diff=earlier_diff if stale_round else current_diff,
            source_file=earlier_source if stale_source else current_source,
            diff_file=earlier_diff if stale_diff else current_diff,
        )

        pending = self._pending(continued_review, chunker)

        assert pending == ["caller-origin"]

    def test_stopped_summary_keeps_prior_signature_when_supporting_source_is_stale(
        self,
        continued_review: CleanReview,
        chunker: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        stale_alpha: tuple[bytes, bytes],
    ) -> None:
        """A round whose supporting source differs from the current supporting source cannot clear the finding.

        The primary source and diff are current but the round's supporting source is the earlier `alpha.py` snapshot.
        The finding must stay pending, since the fix may depend on code the review saw only in its older form.
        """
        run = continued_review.run()
        _stub_clean_run(monkeypatch, chunker, "plateau", [6])
        current_source = (run / "current-source.json").read_bytes()
        current_diff = (run / "current.diff").read_bytes()
        _stage_prior_review(run, round_diff=current_diff, source_file=current_source, diff_file=current_diff)
        evidence = read_json(run / "loop-evidence.json")
        evidence["current_supporting_source_path"] = "current-supporting-source.json"
        evidence["rounds"][0]["supporting_source_path"] = "reviewed-supporting-source.json"
        write_json(run / "loop-evidence.json", evidence)
        (run / "current-supporting-source.json").write_bytes(current_source)
        (run / "reviewed-supporting-source.json").write_bytes(stale_alpha[0])

        pending = self._pending(continued_review, chunker)

        assert pending == ["caller-origin"]

    def test_check_rejects_downgraded_prior_finding(self, continued_review: CleanReview, chunker: ModuleType) -> None:
        """A child ledger that lowers the caller finding to a non-blocking tier is a wrong attribution.

        The child ledger is valid on its own but records `caller-origin` as a low-tier finding. `check` must fail with
        `prior-finding-attribution-mismatch`, because a child may not downgrade what the caller recorded as high.
        """
        run = continued_review.run()
        current = (run / "current.diff").read_bytes()
        ledger = _stage_prior_review(
            run, round_diff=current, source_file=(run / "current-source.json").read_bytes(), diff_file=current
        )
        ledger["rounds"][0]["findings"][0].update(tier="low", structural=True)
        write_json(run / "loop-ledger.json", ledger)

        with pytest.raises(ValueError, match="prior-finding-attribution-mismatch:caller-origin"):
            chunker.check(continued_review.manifest_path, continued_review.results)

        assert chunker.validate_ledger(ledger) == []

    def test_stopped_summary_keeps_downgraded_prior_signature_pending(
        self, continued_review: CleanReview, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with every run clean, a mismatched attribution leaves the caller signature pending.

        Every child run is stubbed clean, yet the child ledger lowers `caller-origin` to a low tier. The summary must
        keep the signature pending instead of crediting the downgraded record as an answer.
        """
        run = continued_review.run()
        current = (run / "current.diff").read_bytes()
        ledger = _stage_prior_review(
            run, round_diff=current, source_file=(run / "current-source.json").read_bytes(), diff_file=current
        )
        ledger["rounds"][0]["findings"][0].update(tier="low", structural=True)
        write_json(run / "loop-ledger.json", ledger)
        _stub_clean_run(monkeypatch, chunker, "clean", [0])

        pending = self._pending(continued_review, chunker)

        assert pending == ["caller-origin"]

    def test_check_rejects_prior_source_path_without_captured_file(
        self, continued_review: CleanReview, chunker: ModuleType
    ) -> None:
        """Coverage naming a path no chunk captured cannot be reassessed against source.

        The continuation manifest assigns the finding to a source path that no chunk captured, with the recorded request
        updated to match. `check` must refuse it, since there is no source to reassess the finding against.
        """
        run = continued_review.run()
        current = (run / "current.diff").read_bytes()
        _stage_prior_review(
            run, round_diff=current, source_file=(run / "current-source.json").read_bytes(), diff_file=current
        )
        manifest = continued_review.manifest
        manifest["prior_findings"]["coverage"][0]["source_paths"] = ["missing-validator.py"]
        manifest["request"]["specification"] = manifest["request"]["specification"].replace(
            '"source_paths": ["alpha.py"]', '"source_paths": ["missing-validator.py"]'
        )
        write_json(continued_review.manifest_path, manifest)

        with pytest.raises(ValueError, match="prior-source-coverage-incomplete:caller-origin"):
            chunker.check(continued_review.manifest_path, continued_review.results)


@pytest.mark.integration
def test_committed_template_skips_git_maintenance(
    tmp_path_factory: pytest.TempPathFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep Git maintenance from changing the template while its consumers copy it.

    A global config that turns automatic maintenance on would race the module-scoped template copies; the template must
    pin it off locally. Git's own trace proves the commit ran and no maintenance child started.
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[maintenance]\n\tauto = true\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    trace = tmp_path / "git-trace.jsonl"
    monkeypatch.setenv("GIT_TRACE2_EVENT", trace.as_posix())

    template = _committed_template.__wrapped__(tmp_path_factory)

    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert any(event["event"] == "start" and "commit" in event["argv"] for event in events)
    assert not [event for event in events if event["event"] == "child_start" and "maintenance" in event.get("argv", [])]
    repository = _copy_changed_repository(template, tmp_path / "repository")
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repository, check=True, capture_output=True)
    assert head.stdout.strip()


def test_planning_captures_only_finished_groups(
    changed_repository: Path, tmp_path: Path, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Size the inventory without recapturing each growing candidate group."""
    capture = Mock(wraps=chunker.capture_source_snapshot)
    monkeypatch.setattr(chunker, "capture_source_snapshot", capture)
    output = chunker.plan(changed_repository, tmp_path / "chunks", [], 1_000_000, "changed", _COORDINATOR_REQUEST)
    manifest = read_json(output)
    assert len(manifest["chunks"]) == 1
    assert capture.call_count <= 3


def test_sizing_retains_non_utf8_git_paths(chunker: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Git-quoted non-UTF-8 paths retain their full patch when the shared parser cannot decode them."""
    path = "\udcff.py"
    snapshot = {"scope_paths": [path], "files": [{"path": path, "content": "value = 1\n"}]}
    patch = b'diff --git "a/\\377.py" "b/\\377.py"\n--- "a/\\377.py"\n+++ "b/\\377.py"\n@@ -1 +1 @@\n-old\n+new\n'
    monkeypatch.setattr(chunker, "capture_source_snapshot", Mock(return_value=snapshot))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=patch))
    monkeypatch.setattr(chunker.subprocess, "run", run)
    assert chunker._sized_groups(Path.cwd(), [path], 1_000_000, "HEAD") == [[path]]
    assert run.call_count == 2


def test_sizing_keeps_exact_budget_boundary(changed_repository: Path, chunker: ModuleType) -> None:
    """Incremental sizes agree with full source and patch bytes at the admission boundary."""
    inventory = chunker._inventory(changed_repository, ["."], "changed")
    source, diff = chunker._material(changed_repository, inventory)
    budget = len(source) + len(diff)
    assert chunker._sized_groups(changed_repository, inventory, budget, "HEAD") == [inventory]
    assert len(chunker._sized_groups(changed_repository, inventory, budget - 1, "HEAD")) > 1


@pytest.mark.integration
@pytest.mark.parametrize("pinned", [False, True])
def test_large_path_inventory_preserves_exact_material_with_bounded_argv(
    changed_repository: Path, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """Long inventories remain byte-exact without exceeding the portable Git argument budget."""
    paths = [f"sources/{index:04d}-{'module-' * 6}.py" for index in range(450)]
    _write_inventory(changed_repository, paths)
    snapshot = chunker.capture_source_snapshot(changed_repository, paths)
    source = (json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
    diff = chunker._git(changed_repository, "diff", "HEAD", "--binary", "--no-renames", "--", *paths)
    assert diff
    budget = len(source) + len(diff)
    assert len(" ".join(paths).encode()) > 24_000
    original_run = subprocess.run

    def bounded_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Exercise real Git while enforcing the portable path-argument ceiling."""
        selected = argv[argv.index("--") + 1 :] if "--" in argv else []
        assert sum(len(path.encode()) + 1 for path in selected) <= 24_000
        return original_run(argv, **kwargs)

    monkeypatch.setattr(chunker.subprocess, "run", bounded_run)
    assert chunker._sized_groups(changed_repository, paths, budget, "HEAD") == [paths]
    assert len(chunker._sized_groups(changed_repository, paths, budget - 1, "HEAD")) > 1
    assert chunker._material(changed_repository, paths) == (source, diff)
    manifest = chunker.plan(
        changed_repository,
        changed_repository.parent / "bounded-plan",
        [],
        budget,
        "changed",
        _COORDINATOR_REQUEST,
        diff_base_oid=snapshot["revision"] if pinned else None,
    )
    assert len(read_json(manifest)["chunks"]) == 1


def _write_inventory(repository: Path, paths: list[str]) -> None:
    """Create tracked, modified files for a long-argv source and patch capture."""
    for path in paths:
        destination = repository / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "inventory"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    for path in paths:
        (repository / path).write_text("VALUE = 2\n", encoding="utf-8", newline="\n")


def test_git_patch_timeout_distinguishes_inventory_and_single_path(
    chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bulk binary patches get 120 seconds while single-path inspection keeps its 30-second bound."""
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=b""))
    monkeypatch.setattr(chunker.subprocess, "run", run)
    chunker._git(Path.cwd(), "diff", "HEAD", "--binary", "--", "one.py", "two.py")
    assert run.call_args.kwargs["timeout"] == 120
    chunker._git(Path.cwd(), "diff", "HEAD", "--binary", "--", "one.py")
    assert run.call_args.kwargs["timeout"] == 30


def test_planner_rejects_excessive_inventory_before_capture(
    changed_repository: Path, tmp_path: Path, chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject an oversized inventory before expensive source reads or output creation."""
    monkeypatch.setattr(chunker, "MAX_FILES", 2)
    monkeypatch.setattr(chunker, "_inventory", lambda *args: [f"source-{index}.py" for index in range(3)])
    capture = Mock(side_effect=AssertionError("unexpected source read"))
    monkeypatch.setattr(chunker, "capture_source_snapshot", capture)
    with pytest.raises(ValueError, match=r"^chunk-file-count-limit:2$"):
        chunker.plan(changed_repository, tmp_path / "chunks", [], 900, "changed", _COORDINATOR_REQUEST)
    capture.assert_not_called()
    assert not (tmp_path / "chunks").exists()


def test_manifest_repository_is_bound_before_git_reads(
    planned_review: tuple[Path, Path], chunker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest cannot redirect inspection away from its retained source repository."""
    repository, out = planned_review
    manifest_path = out / "chunks.json"
    manifest = read_json(manifest_path)
    manifest["repository"] = out.resolve().as_posix()
    _redirect_source_repositories(out, manifest)
    write_json(manifest_path, manifest)
    git = Mock(side_effect=AssertionError("unexpected repository access"))
    monkeypatch.setattr(chunker.subprocess, "run", git)
    with pytest.raises(ValueError, match="chunk-source-repository-mismatch"):
        chunker.check(manifest_path, repository=repository)
    git.assert_not_called()


def _redirect_source_repositories(out: Path, manifest: dict[str, object]) -> None:
    """Forge every retained source repository coordinate to match a redirected manifest."""
    for chunk in manifest["chunks"]:
        snapshot_path = out / chunk["source_path"]
        snapshot = read_json(snapshot_path)
        snapshot["repository"] = manifest["repository"]
        write_json(snapshot_path, snapshot)


def test_git_reads_disable_fsmonitor_and_bound_runtime(chunker: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Git inspection explicitly disables executable fsmonitor config and limits runtime."""
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=b"revision"))
    monkeypatch.setattr(chunker.subprocess, "run", run)
    assert chunker._git(Path.cwd(), "rev-parse", "HEAD") == b"revision"
    argv = run.call_args.args[0]
    assert argv[argv.index("-c") + 1] == "core.fsmonitor=false"
    assert 0 < run.call_args.kwargs["timeout"] <= 60


@pytest.mark.integration
def test_plan_never_invokes_repository_fsmonitor(changed_repository: Path, tmp_path: Path) -> None:
    """Planning suppresses fsmonitor in every Git read, including shared source snapshots."""
    log = tmp_path / "monitor.log"
    script = tmp_path / "monitor.py"
    script.write_text(
        "import pathlib, sys\n"
        f"with pathlib.Path({str(log)!r}).open('a') as stream: stream.write('invoked\\n')\n"
        "sys.stdout.buffer.write(b'token\\0')\n",
        encoding="utf-8",
        newline="\n",
    )
    command = [sys.executable, str(script)]
    hook = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    subprocess.run(["git", "config", "core.fsmonitor", hook], cwd=changed_repository, check=True, capture_output=True)
    subprocess.run(["git", "ls-files"], cwd=changed_repository, check=True, capture_output=True)
    assert log.read_text(encoding="utf-8").strip() == "invoked"
    log.unlink()
    planned = _plan(changed_repository, tmp_path / "review")
    assert planned.returncode == 0, planned.stderr
    assert not log.exists()


def test_check_defaults_to_invoking_repository_and_accepts_explicit_root(
    changed_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller outside the planned repository must supply its trusted root explicitly."""
    repository, out = changed_repository, tmp_path / "review"
    planned = _plan(repository, out)
    assert planned.returncode == 0, planned.stderr
    manifest = out / "chunks.json"
    child = repository / "nested"
    child.mkdir(exist_ok=True)
    monkeypatch.chdir(child)
    assert _check_cli(manifest).returncode == 0
    monkeypatch.chdir(CHUNKER.parents[3])
    rejected = _check_cli(manifest)
    assert rejected.returncode == 2
    assert "chunk-source-repository-mismatch" in rejected.stderr
    assert _check_cli(manifest, "--repository", str(repository)).returncode == 0


def test_planner_help_documents_file_ceiling() -> None:
    """The CLI exposes the inventory ceiling before planning begins."""
    completed = _run("plan", "--help")
    assert completed.returncode == 0
    assert "10,000 files" in completed.stdout


def test_check_help_discloses_filter_execution() -> None:
    """Repository binding does not claim to disable configured clean or process filters."""
    completed = _run("check", "--help")
    assert completed.returncode == 0
    assert "clean/process filters may still execute" in " ".join(completed.stdout.split())


def test_output_check_disables_fsmonitor_and_bounds_runtime(
    chunker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ignored-output inspection uses the same Git execution restrictions as source inspection."""
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=b""))
    monkeypatch.setattr(chunker.subprocess, "run", run)
    destination = tmp_path / "reports"
    assert chunker._safe_output(tmp_path, destination) == destination.resolve()
    argv = run.call_args.args[0]
    assert argv[argv.index("-c") + 1] == "core.fsmonitor=false"
    assert 0 < run.call_args.kwargs["timeout"] <= 60


@pytest.mark.parametrize(
    ("error", "diagnostic"),
    [
        pytest.param(KeyError("private-key"), "inspection-failed", id="missing-field"),
        pytest.param(TypeError("private-type"), "inspection-failed", id="wrong-type"),
        pytest.param(RecursionError("private-path"), "inspection-failed", id="deep-json"),
        pytest.param(OSError(errno.E2BIG, "private-path"), "os-error:E2BIG", id="argv-too-long"),
        pytest.param(FileNotFoundError(errno.ENOENT, "private-path"), "os-error:ENOENT", id="missing-input"),
        pytest.param(PermissionError(errno.EACCES, "private-path"), "os-error:EACCES", id="unreadable-input"),
        pytest.param(OSError("private-path"), "os-error:UNKNOWN", id="unknown-os-error"),
        pytest.param(subprocess.TimeoutExpired("private-command", 30), "git-timeout", id="child-timeout"),
    ],
)
def test_cli_maps_unexpected_input_errors_to_exit_two(
    chunker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    diagnostic: str,
) -> None:
    """Malformed evidence and child failures produce a bounded diagnostic without private details."""
    monkeypatch.setattr(sys, "argv", [str(CHUNKER), "check", "--manifest", "unused.json"])
    monkeypatch.setattr(chunker, "check", Mock(side_effect=error))
    assert chunker.main() == 2
    captured = capsys.readouterr()
    assert captured.err == f"chunk-diff-error:{diagnostic}\n"
    assert captured.out == ""
