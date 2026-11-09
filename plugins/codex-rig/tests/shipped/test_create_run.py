"""Acceptance checks for portable workflow run-directory creation.

``shared/create_run.py`` is a shipped command-line helper, so nearly every check runs it through a real subprocess; only
the frozen-clock and destination-collision checks replace their clock or filesystem boundary.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from itertools import repeat
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CREATE_RUN = PLUGIN_ROOT / "shared" / "create_run.py"
_REAL_ITERDIR = Path.iterdir
_PR41_IDENTITY = b'{"number": 41}\n'
_FINDINGS = b"retained report\r\n"


def _run_create(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the helper through its shipped command-line boundary."""
    return subprocess.run([sys.executable, str(CREATE_RUN), *arguments], capture_output=True, text=True, check=False)


def _promote(root: Path, skill: str, source: Path) -> subprocess.CompletedProcess[str]:
    """Promote *source* into its PR-indexed namespace under *root* through the command line."""
    return _run_create("--skill", skill, "--root", str(root), "--promote-pr-run", str(source))


def _new_run(root: Path, skill: str) -> Path:
    """Create one real timestamp run for *skill* and return its directory."""
    completed = _run_create("--skill", skill, "--root", str(root))
    assert completed.returncode == 0, completed.stderr
    return Path(completed.stdout.strip())


def _new_review_run(root: Path, number: object = 17) -> Path:
    """Create one real timestamp run with its collected PR identity."""
    run_directory = _new_run(root, "code-review")
    (run_directory / "pr.json").write_text(json.dumps({"number": number}), encoding="utf-8")
    return run_directory


def _collect_remediation_run(root: Path) -> Path:
    """Create a remediation run holding PR 41 as nested identity, a decoy root PR 99, and retained findings."""
    source = _new_run(root, "code-remediate")
    (source / "pr").mkdir()
    (source / "pr/pr.json").write_bytes(_PR41_IDENTITY)
    (source / "pr.json").write_text('{"number": 99}', encoding="utf-8")
    (source / "findings-input.txt").write_bytes(_FINDINGS)
    return source


def _promote_remediation_runs(root: Path, count: int) -> None:
    """Collect and promote *count* remediation runs for PR 41, so the namespace already holds that many."""
    for _ in range(count):
        promoted = _promote(root, "code-remediate", _collect_remediation_run(root))
        assert promoted.returncode == 0, promoted.stderr


def _without_nested_identity(source: Path) -> None:
    """Leave a remediation run with no nested PR identity at all."""


def _with_boolean_nested_identity(source: Path) -> None:
    """Give a remediation run a nested PR identity whose number is the boolean true."""
    (source / "pr").mkdir()
    (source / "pr/pr.json").write_text('{"number": true}', encoding="utf-8")


def _review_run_without_identity(root: Path) -> Path:
    """Create a review run that never collected a PR identity."""
    return _new_run(root, "code-review")


def _review_run_with_boolean_identity(root: Path) -> Path:
    """Create a review run whose collected PR number is the boolean true."""
    return _new_review_run(root, number=True)


def _load_create_run() -> ModuleType:
    """Load the helper for a collision injected at the filesystem boundary."""
    return load_module("create_run_under_test", CREATE_RUN)


class TestCreateRun:
    """Keep ordinary timestamp allocation behavior stable."""

    def test_emits_one_new_native_path(self, tmp_path: Path) -> None:
        """Create a bounded skill artifact directory without shell variables.

        A plain `--skill`/`--root` call prints exactly one new UTC-stamped directory under the skill's folder.
        """
        completed = _run_create("--skill", "code-review", "--root", str(tmp_path))

        assert completed.returncode == 0, completed.stderr
        created = Path(completed.stdout.strip())
        assert created.is_dir()
        assert created.parent == tmp_path / "code-review"
        assert _load_create_run().TIMESTAMP_RUN_ID.fullmatch(created.name)
        assert completed.stderr == ""

    def test_repeated_clock_allocates_isolated_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Keep frozen-clock invocations isolated while preserving an earlier run's evidence."""
        module = _load_create_run()
        clock = mock.Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        monkeypatch.setattr(module, "datetime", clock)
        monkeypatch.setattr(sys, "argv", [str(CREATE_RUN), "--skill", "audit", "--root", str(tmp_path)])

        assert module.main() == 0
        first = Path(capsys.readouterr().out.strip())
        sentinel = first / "evidence.txt"
        sentinel.write_bytes(b"first run")
        assert module.main() == 0
        second = Path(capsys.readouterr().out.strip())

        assert first != second
        assert first.parent == second.parent == tmp_path / "audit"
        assert first.name.startswith("2026-01-02T03-04-05-000000Z-")
        assert second.is_dir()
        assert not (second / sentinel.name).exists()
        assert sentinel.read_bytes() == b"first run"

    def test_rejects_path_like_skill_id(self, tmp_path: Path) -> None:
        """Prevent a skill argument from escaping the artifact root.

        A skill id of `../escape` must exit 2 with a clear message and create nothing beside the root.
        """
        completed = _run_create("--skill", "../escape", "--root", str(tmp_path))

        assert completed.returncode == 2
        assert "invalid skill id" in completed.stderr
        assert not (tmp_path.parent / "escape").exists()


class TestPromotePrRun:
    """Verify safe post-collection promotion into a PR-indexed namespace."""

    @pytest.mark.parametrize("prior_runs", [0, 1])
    def test_remediation_uses_nested_identity_and_preserves_complete_runs(
        self, tmp_path: Path, prior_runs: int
    ) -> None:
        """Save consecutive remediation runs under their collected PR without losing intake evidence.

        The PR comes from the nested `pr/pr.json` (41), never the decoy root `pr.json` (99); the namespace already holds
        zero or one earlier run, and every earlier run keeps its retained findings.
        """
        _promote_remediation_runs(tmp_path, prior_runs)
        source = _collect_remediation_run(tmp_path)

        completed = _promote(tmp_path, "code-remediate", source)

        expected = tmp_path / "code-remediate" / "pr-41" / f"run-{prior_runs + 1:03d}"
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == str(expected)
        assert not source.exists()
        assert (expected / "pr/pr.json").read_bytes() == _PR41_IDENTITY
        assert (expected / "findings-input.txt").read_bytes() == _FINDINGS
        assert not (tmp_path / "code-remediate/pr-99").exists()
        assert (tmp_path / "code-remediate/pr-41/run-001/findings-input.txt").read_bytes() == _FINDINGS

    @pytest.mark.parametrize(
        "prepare_identity",
        [
            pytest.param(_without_nested_identity, id="missing-nested-identity"),
            pytest.param(_with_boolean_nested_identity, id="boolean-pr-number"),
        ],
    )
    def test_remediation_rejects_missing_or_invalid_nested_identity(
        self, tmp_path: Path, prepare_identity: Callable[[Path], None]
    ) -> None:
        """Never infer a remediation target from unrelated root-level review metadata.

        A root `pr.json` naming PR 41 is present, but the nested identity is absent or a boolean, so promotion must stop
        and leave the source run in place.
        """
        source = _new_run(tmp_path, "code-remediate")
        (source / "pr.json").write_text('{"number": 41}', encoding="utf-8")
        prepare_identity(source)

        completed = _promote(tmp_path, "code-remediate", source)

        assert completed.returncode != 0
        assert "authoritative pr.json" in completed.stderr or "positive integer" in completed.stderr
        assert source.is_dir()
        assert not (tmp_path / "code-remediate/pr-41").exists()

    def test_uses_authoritative_identity_and_first_index(self, tmp_path: Path) -> None:
        """Move the complete timestamp run under the PR number collected in that run.

        A review run holding `pr.json` for PR 41 and an extra evidence file lands in `pr-41/run-001` intact.
        """
        source = _new_review_run(tmp_path, number=41)
        marker = source / "review-notes.md"
        marker.write_text("retained evidence\n", encoding="utf-8")

        completed = _promote(tmp_path, "code-review", source)

        expected = tmp_path / "code-review" / "pr-41" / "run-001"
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == str(expected)
        assert not source.exists()
        assert (expected / "pr.json").read_text(encoding="utf-8") == '{"number": 41}'
        assert (expected / marker.name).read_text(encoding="utf-8") == "retained evidence\n"

    def test_increments_numerically_with_minimum_width(self, tmp_path: Path) -> None:
        """Choose max plus one without lexicographic ordering or fixed-width overflow.

        With `run-009` and `run-1000` present, the next index is `run-1001`, not a width-bound or string-ordered pick.
        """
        source = _new_review_run(tmp_path, number=41)
        pr_directory = tmp_path / "code-review" / "pr-41"
        (pr_directory / "run-009").mkdir(parents=True)
        (pr_directory / "run-1000").mkdir()

        completed = _promote(tmp_path, "code-review", source)

        expected = pr_directory / "run-1001"
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == str(expected)
        assert expected.is_dir()

    def test_rejects_malformed_run_sibling(self, tmp_path: Path) -> None:
        """Stop rather than allocate around an ambiguous per-PR sequence.

        A `run-latest` directory beside the numbered runs cannot be ordered, so promotion fails and keeps the source.
        """
        source = _new_review_run(tmp_path)
        (tmp_path / "code-review" / "pr-17" / "run-latest").mkdir(parents=True)

        completed = _promote(tmp_path, "code-review", source)

        assert completed.returncode != 0
        assert "malformed promoted run sibling: run-latest" in completed.stderr
        assert source.is_dir()

    def test_rejects_valid_named_file_sibling(self, tmp_path: Path) -> None:
        """Require every existing sequence member to be a real directory.

        A regular file named `run-001` has a valid name but is not a run, so promotion fails and keeps the source.
        """
        source = _new_review_run(tmp_path)
        sibling = tmp_path / "code-review" / "pr-17" / "run-001"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("not a run directory\n", encoding="utf-8")

        completed = _promote(tmp_path, "code-review", source)

        assert completed.returncode != 0
        assert "promoted run sibling is not a directory: run-001" in completed.stderr
        assert source.is_dir()

    def test_rejects_source_outside_exact_skill_root(self, tmp_path: Path) -> None:
        """Prevent promotion from importing an unrelated directory into review evidence.

        The source run lives under a different root than the one named, so it is not a direct child of the skill root.
        """
        source = _new_review_run(tmp_path / "other-root")

        completed = _promote(tmp_path, "code-review", source)

        assert completed.returncode != 0
        assert "promotion source must be a direct child of the skill root" in completed.stderr
        assert source.is_dir()

    def test_rejects_unsupported_skill(self, tmp_path: Path) -> None:
        """Keep PR-indexed promotion closed to review and remediation workflows.

        An `audit` run has no PR namespace, so promotion is refused and the run stays where it was created.
        """
        source = _new_run(tmp_path, "audit")

        completed = _promote(tmp_path, "audit", source)

        assert completed.returncode != 0
        assert "PR run promotion is supported only for code-review" in completed.stderr
        assert source.is_dir()

    @pytest.mark.parametrize(
        ("make_source", "message"),
        [
            pytest.param(
                _review_run_without_identity, "promotion source has no authoritative pr.json", id="missing-identity"
            ),
            pytest.param(
                _review_run_with_boolean_identity,
                "pr.json number must be a positive integer",
                id="boolean-identity",
            ),
        ],
    )
    def test_rejects_missing_or_invalid_pr_identity(
        self, tmp_path: Path, make_source: Callable[[Path], Path], message: str
    ) -> None:
        """Require a collected positive-integer PR identity before moving review evidence.

        A run with no `pr.json`, or one whose number is the boolean true (not integer PR one), is refused and stays put.
        """
        source = make_source(tmp_path)

        completed = _promote(tmp_path, "code-review", source)

        assert completed.returncode != 0
        assert message in completed.stderr
        assert source.is_dir()

    @pytest.mark.parametrize("contender_has_evidence", [False, True])
    @mock.patch.object(Path, "iterdir", autospec=True)
    def test_retries_after_destination_collision(
        self, iterdir: mock.Mock, tmp_path: Path, contender_has_evidence: bool
    ) -> None:
        """Preserve an empty or populated contender that appears after the sequence scan."""
        module = _load_create_run()
        source = _new_review_run(tmp_path, number=41)
        pr_directory = tmp_path / "code-review" / "pr-41"
        contender = pr_directory / "run-001"

        def collide_once(path: Path) -> Iterator[Path]:
            """Create a competing run after capturing the first directory listing."""
            siblings = list(_REAL_ITERDIR(path))
            if iterdir.call_count == 1:
                contender.mkdir()
                if contender_has_evidence:
                    (contender / "other.txt").write_bytes(b"other owner")
            return iter(siblings)

        iterdir.side_effect = collide_once
        promoted = module.promote_pr_run(tmp_path, "code-review", source)

        expected = pr_directory / "run-002"
        assert promoted == expected
        assert (expected / "pr.json").is_file()
        assert contender.is_dir()
        assert not (contender / "pr.json").exists()
        assert list(_REAL_ITERDIR(contender)) == ([contender / "other.txt"] if contender_has_evidence else [])

    def test_promotes_legacy_timestamp_run(self, tmp_path: Path) -> None:
        """Retain promotion of directories allocated before randomized suffixes were introduced."""
        source = tmp_path / "code-review" / "2026-01-02T03-04-05-000000Z"
        source.mkdir(parents=True)
        (source / "pr.json").write_bytes(_PR41_IDENTITY)

        completed = _promote(tmp_path, "code-review", source)

        assert completed.returncode == 0, completed.stderr
        promoted = Path(completed.stdout.strip())
        assert promoted == tmp_path / "code-review" / "pr-41" / "run-001"
        assert (promoted / "pr.json").read_bytes() == _PR41_IDENTITY

    def test_stale_claim_preserves_its_sequence_gap(self, tmp_path: Path) -> None:
        """Keep an interrupted promoter's claim rather than stealing its index or deleting its state."""
        source = _new_review_run(tmp_path, number=41)
        claim = tmp_path / "code-review" / "pr-41" / ".run-001.claim"
        claim.mkdir(parents=True)

        completed = _promote(tmp_path, "code-review", source)

        assert completed.returncode == 0, completed.stderr
        assert Path(completed.stdout.strip()) == claim.parent / "run-002"
        assert claim.is_dir()
        assert not (claim.parent / ".run-002.claim").exists()

    def test_parallel_cli_runs_promote_without_shared_evidence(self, tmp_path: Path) -> None:
        """Exercise real concurrent creators and promoters against one PR namespace."""
        with ThreadPoolExecutor(max_workers=6) as executor:
            sources = list(executor.map(_new_review_run, repeat(tmp_path, 12)))
            completed = list(executor.map(partial(_promote, tmp_path, "code-review"), sources))

        destinations = [Path(result.stdout.strip()) for result in completed]
        assert [result.returncode for result in completed] == [0] * 12
        assert len(set(sources)) == len(set(destinations)) == 12
        assert all(not source.exists() for source in sources)
        assert all(
            (destination / "pr.json").read_text(encoding="utf-8") == '{"number": 17}' for destination in destinations
        )
        assert sorted(path.name for path in destinations) == [f"run-{index:03d}" for index in range(1, 13)]
        assert sorted(path.name for path in destinations[0].parent.iterdir()) == [
            f"run-{index:03d}" for index in range(1, 13)
        ]

    @mock.patch.object(Path, "iterdir", autospec=True)
    def test_claim_removed_during_scan_does_not_abort_promotion(self, iterdir: mock.Mock, tmp_path: Path) -> None:
        """Tolerate a concurrent promoter releasing its claim after the directory listing."""
        module = _load_create_run()
        source = _new_review_run(tmp_path, number=41)
        claim = tmp_path / "code-review" / "pr-41" / ".run-001.claim"
        claim.mkdir(parents=True)

        def released_claim(path: Path) -> Iterator[Path]:
            """Yield a listing whose first claim was released before inspection."""
            siblings = list(_REAL_ITERDIR(path))
            if claim.exists():
                claim.rmdir()
            return iter(siblings)

        iterdir.side_effect = released_claim
        promoted = module.promote_pr_run(tmp_path, "code-review", source)

        assert (promoted / "pr.json").read_bytes() == b'{"number": 41}'
        assert not claim.exists()


@pytest.mark.parametrize("numbers", [pytest.param((41, 41), id="same-pr"), pytest.param((41, 42), id="different-pr")])
def test_pr_identity_preserves_frozen_clock_uniqueness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], numbers: tuple[int, int]
) -> None:
    """Make simultaneous PR runs identifiable without sharing a directory or evidence."""
    module = _load_create_run()
    clock = mock.Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", clock)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(CREATE_RUN), "--skill", "code-review", "--root", str(tmp_path), "--pr-number", str(numbers[0])],
    )
    assert module.main() == 0
    first = Path(capsys.readouterr().out.strip())
    (first / "evidence").write_bytes(b"first")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(CREATE_RUN), "--skill", "code-review", "--root", str(tmp_path), "--pr-number", str(numbers[1])],
    )
    assert module.main() == 0
    second = Path(capsys.readouterr().out.strip())
    assert first != second
    assert first.name.startswith(f"2026-01-02T03-04-05-000000Z-pr-{numbers[0]}-")
    assert second.name.startswith(f"2026-01-02T03-04-05-000000Z-pr-{numbers[1]}-")
    assert not (second / "evidence").exists()
    assert (first / "evidence").read_bytes() == b"first"


@pytest.mark.parametrize(
    ("branch", "slug"),
    [
        pytest.param("feature/topic", "feature-topic", id="slash-normalization"),
        pytest.param("feature-topic", "feature-topic", id="normalization-collision"),
        pytest.param("../../escape", "escape", id="path-like"),
        pytest.param("větev/" + "界" * 1000, "v-tev", id="long-unicode"),
        pytest.param("界" * 1000, "branch", id="unicode-only"),
        pytest.param("topic" * 100, "topic" * 8, id="long-ascii"),
    ],
)
def test_branch_identity_is_bounded_portable_and_digest_bound(tmp_path: Path, branch: str, slug: str) -> None:
    """Keep the original branch's digest when unsafe or oversized text is normalized."""
    completed = _run_create("--skill", "code-review", "--root", str(tmp_path), "--branch", branch)
    assert completed.returncode == 0, completed.stderr
    created = Path(completed.stdout.strip())
    assert created.parent == tmp_path / "code-review"
    assert len(created.name) < 128
    assert created.name.isascii()
    digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:12]
    assert f"-branch-{slug}-{digest}-" in created.name
    assert _load_create_run().TIMESTAMP_RUN_ID.fullmatch(created.name)


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(["--pr-number", "0"], id="zero"),
        pytest.param(["--pr-number", "-1"], id="negative"),
        pytest.param(["--branch", " "], id="blank-branch"),
        pytest.param(["--pr-number", "1", "--branch", "topic"], id="conflicting"),
    ],
)
def test_invalid_creation_identity_creates_nothing(tmp_path: Path, identity: list[str]) -> None:
    """Refuse malformed or ambiguous identity before allocating a run."""
    completed = _run_create("--skill", "code-review", "--root", str(tmp_path), *identity)
    assert completed.returncode == 2
    assert not (tmp_path / "code-review").exists()


@pytest.mark.parametrize("collected", [41, 42])
def test_pr_named_promotion_checks_collected_identity(tmp_path: Path, collected: int) -> None:
    """Move only matching PR runs and preserve all mismatched evidence before target allocation."""
    completed = _run_create("--skill", "code-review", "--root", str(tmp_path), "--pr-number", "41")
    assert completed.returncode == 0, completed.stderr
    source = Path(completed.stdout.strip())
    (source / "pr.json").write_text(json.dumps({"number": collected}), encoding="utf-8")
    promoted = _promote(tmp_path, "code-review", source)
    assert (promoted.returncode == 0) == (collected == 41)
    assert source.exists() == (collected != 41)
    assert (tmp_path / "code-review" / f"pr-{collected}").exists() == (collected == 41)


def test_branch_named_run_promotes_collected_pr(tmp_path: Path) -> None:
    """Keep branch-identified historical collection compatible with authoritative PR promotion."""
    created = _run_create("--skill", "code-review", "--root", str(tmp_path), "--branch", "feature/topic")
    assert created.returncode == 0, created.stderr
    source = Path(created.stdout.strip())
    (source / "pr.json").write_bytes(_PR41_IDENTITY)
    completed = _promote(tmp_path, "code-review", source)
    assert completed.returncode == 0, completed.stderr
    assert (Path(completed.stdout.strip()) / "pr.json").read_bytes() == _PR41_IDENTITY
    assert not source.exists()
