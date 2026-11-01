"""Acceptance checks for portable workflow run-directory creation.

``shared/create_run.py`` is a shipped command-line helper, so nearly every check runs it through a real subprocess; only
the destination-collision retry swaps the filesystem rename at its boundary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CREATE_RUN = PLUGIN_ROOT / "shared" / "create_run.py"
_REAL_RENAME = Path.rename
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
        assert created.name.endswith("Z")
        assert completed.stderr == ""

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

    @mock.patch.object(Path, "rename", autospec=True)
    def test_retries_after_destination_collision(self, rename: mock.Mock, tmp_path: Path) -> None:
        """Rescan after another promoter claims the selected run index.

        The first rename finds its destination already created by a concurrent promoter and fails; the retry rescans,
        sees that run, and lands in the next index.
        """

        def collide_once(path: Path, target: Path) -> Path:
            """Create the colliding destination on the first call, then delegate to the real rename."""
            if rename.call_count == 1:
                target.mkdir()
                raise FileExistsError(target)
            return _REAL_RENAME(path, target)

        module = _load_create_run()
        source = _new_review_run(tmp_path, number=41)
        rename.side_effect = collide_once

        promoted = module.promote_pr_run(tmp_path, "code-review", source)

        expected = tmp_path / "code-review" / "pr-41" / "run-002"
        assert rename.call_count == 2
        assert promoted == expected
        assert (expected / "pr.json").is_file()
