"""Acceptance checks for portable workflow run-directory creation."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CREATE_RUN = PLUGIN_ROOT / "shared" / "create_run.py"


def _run_create(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the helper through its shipped command-line boundary."""
    return subprocess.run(
        [sys.executable, str(CREATE_RUN), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _new_review_run(root: Path, number: object = 17) -> Path:
    """Create one real timestamp run with its collected PR identity."""
    completed = _run_create("--skill", "code-review", "--root", str(root))
    assert completed.returncode == 0, completed.stderr
    run_directory = Path(completed.stdout.strip())
    (run_directory / "pr.json").write_text(json.dumps({"number": number}), encoding="utf-8")
    return run_directory


def _load_create_run() -> ModuleType:
    """Load the helper for a collision injected at the filesystem boundary."""
    spec = importlib.util.spec_from_file_location("create_run_under_test", CREATE_RUN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCreateRun:
    """Keep ordinary timestamp allocation behavior stable."""

    def test_emits_one_new_native_path(self, tmp_path: Path) -> None:
        """Create a bounded skill artifact directory without shell variables."""
        completed = _run_create("--skill", "code-review", "--root", str(tmp_path))

        assert completed.returncode == 0, completed.stderr
        output = completed.stdout.strip()
        created = Path(output)
        assert created.is_dir()
        assert created.parent == tmp_path / "code-review"
        assert created.name.endswith("Z")
        assert completed.stderr == ""

    def test_rejects_path_like_skill_id(self, tmp_path: Path) -> None:
        """Prevent a skill argument from escaping the artifact root."""
        completed = _run_create("--skill", "../escape", "--root", str(tmp_path))

        assert completed.returncode == 2
        assert "invalid skill id" in completed.stderr
        assert not (tmp_path.parent / "escape").exists()


class TestPromotePrRun:
    """Verify safe post-collection promotion into a PR-indexed namespace."""

    def test_uses_authoritative_identity_and_first_index(self, tmp_path: Path) -> None:
        """Move the complete timestamp run under the PR number collected in that run."""
        source = _new_review_run(tmp_path, number=41)
        marker = source / "review-notes.md"
        marker.write_text("retained evidence\n", encoding="utf-8")

        completed = _run_create(
            "--skill",
            "code-review",
            "--root",
            str(tmp_path),
            "--promote-pr-run",
            str(source),
        )

        expected = tmp_path / "code-review" / "pr-41" / "run-001"
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == str(expected)
        assert not source.exists()
        assert (expected / "pr.json").read_text(encoding="utf-8") == '{"number": 41}'
        assert (expected / marker.name).read_text(encoding="utf-8") == "retained evidence\n"

    def test_increments_numerically_with_minimum_width(self, tmp_path: Path) -> None:
        """Choose max plus one without lexicographic ordering or fixed-width overflow."""
        source = _new_review_run(tmp_path, number=41)
        pr_directory = tmp_path / "code-review" / "pr-41"
        (pr_directory / "run-009").mkdir(parents=True)
        (pr_directory / "run-1000").mkdir()

        completed = _run_create(
            "--skill",
            "code-review",
            "--root",
            str(tmp_path),
            "--promote-pr-run",
            str(source),
        )

        expected = pr_directory / "run-1001"
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == str(expected)
        assert expected.is_dir()

    def test_rejects_malformed_run_sibling(self, tmp_path: Path) -> None:
        """Stop rather than allocate around an ambiguous per-PR sequence."""
        source = _new_review_run(tmp_path)
        malformed = tmp_path / "code-review" / "pr-17" / "run-latest"
        malformed.mkdir(parents=True)

        completed = _run_create(
            "--skill",
            "code-review",
            "--root",
            str(tmp_path),
            "--promote-pr-run",
            str(source),
        )

        assert completed.returncode != 0
        assert "malformed promoted run sibling: run-latest" in completed.stderr
        assert source.is_dir()

    def test_rejects_valid_named_file_sibling(self, tmp_path: Path) -> None:
        """Require every existing sequence member to be a real directory."""
        source = _new_review_run(tmp_path)
        sibling = tmp_path / "code-review" / "pr-17" / "run-001"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("not a run directory\n", encoding="utf-8")

        completed = _run_create(
            "--skill",
            "code-review",
            "--root",
            str(tmp_path),
            "--promote-pr-run",
            str(source),
        )

        assert completed.returncode != 0
        assert "promoted run sibling is not a directory: run-001" in completed.stderr
        assert source.is_dir()

    def test_rejects_source_outside_exact_skill_root(self, tmp_path: Path) -> None:
        """Prevent promotion from importing an unrelated directory into review evidence."""
        source = _new_review_run(tmp_path / "other-root")

        completed = _run_create(
            "--skill",
            "code-review",
            "--root",
            str(tmp_path),
            "--promote-pr-run",
            str(source),
        )

        assert completed.returncode != 0
        assert "promotion source must be a direct child of the skill root" in completed.stderr
        assert source.is_dir()

    def test_rejects_unsupported_skill(self, tmp_path: Path) -> None:
        """Keep PR-indexed promotion closed to the code-review workflow."""
        source = _run_create("--skill", "audit", "--root", str(tmp_path))
        assert source.returncode == 0, source.stderr
        source_path = Path(source.stdout.strip())

        completed = _run_create(
            "--skill",
            "audit",
            "--root",
            str(tmp_path),
            "--promote-pr-run",
            str(source_path),
        )

        assert completed.returncode != 0
        assert "PR run promotion is supported only for code-review" in completed.stderr
        assert source_path.is_dir()

    def test_rejects_missing_pr_identity(self, tmp_path: Path) -> None:
        """Require collected PR identity before moving review evidence."""
        missing_identity = _run_create("--skill", "code-review", "--root", str(tmp_path))
        assert missing_identity.returncode == 0, missing_identity.stderr
        missing_source = Path(missing_identity.stdout.strip())

        missing = _run_create(
            "--skill",
            "code-review",
            "--root",
            str(tmp_path),
            "--promote-pr-run",
            str(missing_source),
        )
        assert missing.returncode != 0
        assert "promotion source has no authoritative pr.json" in missing.stderr
        assert missing_source.is_dir()

    def test_rejects_invalid_pr_identity(self, tmp_path: Path) -> None:
        """Reject boolean identity instead of treating it as integer PR one."""
        invalid_source = _new_review_run(tmp_path, number=True)

        invalid = _run_create(
            "--skill",
            "code-review",
            "--root",
            str(tmp_path),
            "--promote-pr-run",
            str(invalid_source),
        )

        assert invalid.returncode != 0
        assert "pr.json number must be a positive integer" in invalid.stderr
        assert invalid_source.is_dir()

    def test_retries_an_atomic_destination_collision(self, tmp_path: Path, monkeypatch) -> None:
        """Rescan after another promoter claims the selected run index."""
        module = _load_create_run()
        source = _new_review_run(tmp_path, number=41)
        real_rename = Path.rename
        attempts = 0

        def _collide_once(path: Path, target: Path) -> Path:
            """Create one colliding destination before allowing the retry to proceed."""
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                target.mkdir()
                raise FileExistsError(target)
            return real_rename(path, target)

        monkeypatch.setattr(Path, "rename", _collide_once)

        promoted = module.promote_pr_run(tmp_path, "code-review", source)

        expected = tmp_path / "code-review" / "pr-41" / "run-002"
        assert attempts == 2
        assert promoted == expected
        assert (expected / "pr.json").is_file()
