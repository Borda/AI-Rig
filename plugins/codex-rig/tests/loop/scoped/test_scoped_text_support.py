"""Verify scoped text-only review excludes unsupported supporting source.

The tests run the shipped chunk planner and diff collector as subprocesses against an origin-bound plan whose caller
(``support.py``) is unchanged. A binary, NUL-bearing or symbolic-link caller, or a NUL-bearing untracked primary file,
must be refused both when the scoped plan is created and when a coherently rebound plan is finally checked.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from _git_fixtures import git_text

PLUGIN = Path(__file__).resolve().parents[3]
CHUNKER = PLUGIN / "skills/challenge-resolve/chunk_diff.py"
COLLECTOR = PLUGIN / "shared/collect_diff.py"


def _supports_symlinks() -> bool:
    """Probe whether this host can create the links used by this regression."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "target").write_bytes(b"text\n")
        try:
            (root / "link").symlink_to("target")
        except (OSError, NotImplementedError):
            return False
        return (root / "link").is_symlink()


_requires_symlinks = pytest.mark.skipif(not _supports_symlinks(), reason="host cannot create symbolic links")
UNSUPPORTED_SUPPORT = [
    pytest.param("binary", id="binary"),
    pytest.param("nul-utf8", id="nul-utf8"),
    pytest.param("symlink", marks=_requires_symlinks, id="symlink"),
]


def _run(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped CLI with its normal evidence checks."""
    return subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True, check=False)


def _write_support(root: Path, kind: str) -> None:
    """Install one text, binary, or symbolic-link supporting endpoint."""
    path = root / "support.py"
    if path.is_symlink() or path.exists():
        path.unlink()
    if kind == "symlink":
        path.symlink_to("target.py")
    else:
        data = {"binary": b"\x00\xffbinary", "nul-utf8": b"\x00binary", "text": b"support = 1\n"}
        path.write_bytes(data[kind])


def _scoped_plan(out: Path, assignments: Path) -> subprocess.CompletedProcess[str]:
    """Generate ``scoped.json`` from the chunk manifest in ``out``."""
    return _run(
        CHUNKER,
        "scoped-plan",
        "--manifest",
        str(out / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(out / "scoped.json"),
    )


def _snapshot_support(root: Path, output: Path) -> None:
    """Snapshot ``support.py`` in ``root`` with the shipped collector and require success."""
    snapshot = _run(
        COLLECTOR, "--snapshot", "--repository", str(root), "--scope-path", "support.py", "--out", str(output)
    )
    assert snapshot.returncode == 0, snapshot.stderr


def _fixture(tmp_path: Path, kind: str, *, untracked_nul: bool = False) -> tuple[Path, Path, Path, Path]:
    """Prepare an origin-bound plan with one unchanged supporting endpoint."""
    original = tmp_path / "original"
    original.mkdir()
    git_text(original, "init", "-q")
    git_text(original, "config", "user.name", "Fixture")
    git_text(original, "config", "user.email", "fixture@example.invalid")
    git_text(original, "config", "core.autocrlf", "false")
    (original / "primary.py").write_bytes(b"value = 1\n")
    (original / "target.py").write_bytes(b"target = 1\n")
    _write_support(original, kind)
    git_text(original, "add", ".")
    git_text(original, "commit", "-qm", "base")
    base = git_text(original, "rev-parse", "HEAD")
    (original / "primary.py").write_bytes(b"value = 2\n")
    if untracked_nul:
        (original / "untracked.py").write_bytes(b"\x00value = 3\n")
    receipt = tmp_path / "receipt"
    collected = _run(COLLECTOR, "--review-worktree", "--repository", str(original), "--out", str(receipt))
    assert collected.returncode == 0, collected.stderr
    mirror = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])
    out = tmp_path / "chunks"
    scope = [] if untracked_nul else ["--scope-path", "primary.py"]
    planned = _run(
        CHUNKER,
        "plan",
        "--repository",
        str(mirror),
        "--origin-repository",
        str(original),
        "--diff-base",
        base,
        *scope,
        "--out",
        str(out),
        "--goal",
        "Review",
        "--specification",
        "Check caller",
        "--done-when",
        "Reviewed",
    )
    assert planned.returncode == 0, planned.stderr
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps(
            {
                "interactions": [
                    {
                        "chunk_id": "chunk-001",
                        "requirement": {
                            "id": "caller",
                            "source_paths": ["primary.py", "support.py"],
                            "claim": "Check caller",
                        },
                    }
                ],
                "audit_assignments": [{"id": "audit", "chunk_ids": ["chunk-001"]}],
            }
        ),
        encoding="utf-8",
    )
    return original, mirror, out, assignments


class TestScopedPlanSupport:
    """Scoped-plan creation over an unchanged supporting endpoint."""

    @pytest.mark.parametrize("kind", UNSUPPORTED_SUPPORT)
    def test_rejects_unsupported_support(self, tmp_path: Path, kind: str) -> None:
        """A text primary must not admit a binary, NUL-bearing or symbolic-link caller as supporting source.

        The caller is committed in the unsupported form from the start, so the planner itself has to refuse it.
        """
        _, _, out, assignments = _fixture(tmp_path, kind)

        result = _scoped_plan(out, assignments)

        assert result.returncode != 0
        assert "scoped-supporting-source-unsupported" in result.stderr

    def test_keeps_supported_text(self, tmp_path: Path) -> None:
        """A normal unchanged text caller remains available as supporting source for scoped review."""
        _, _, out, assignments = _fixture(tmp_path, "text")

        result = _scoped_plan(out, assignments)

        assert result.returncode == 0, result.stderr

    def test_rejects_nul_untracked_primary(self, tmp_path: Path) -> None:
        """UTF-8-decodable binary bytes cannot hide in an untracked primary file."""
        _, _, out, assignments = _fixture(tmp_path, "text", untracked_nul=True)

        result = _scoped_plan(out, assignments)

        assert result.returncode != 0
        assert "scoped-unsupported-source-unreviewed" in result.stderr


class TestScopedCheckSupport:
    """Final checking of a scoped plan whose supporting endpoint was swapped after planning."""

    @pytest.mark.parametrize("kind", UNSUPPORTED_SUPPORT)
    def test_rejects_rebound_unsupported_support(self, tmp_path: Path, kind: str) -> None:
        """Even coherently rebound prior artifacts cannot bypass final source checking.

        A valid text plan is generated, then the caller is replaced by an unsupported form in both checkouts and the
        manifest's supporting unit and saved origin snapshot are rewritten to match; the final check must still refuse
        the swapped source instead of trusting the matching hashes.
        """
        original, mirror, out, assignments = _fixture(tmp_path, "text")
        generated = _scoped_plan(out, assignments)
        assert generated.returncode == 0, generated.stderr
        _write_support(original, kind)
        _write_support(mirror, kind)
        origin_file = tmp_path / "origin-support.json"
        mirror_file = tmp_path / "mirror-support.json"
        _snapshot_support(original, origin_file)
        _snapshot_support(mirror, mirror_file)
        scoped = out / "scoped.json"
        manifest = json.loads(scoped.read_bytes())
        record = json.loads(mirror_file.read_bytes())["files"][0]
        unit = manifest["supporting_units"][0]
        unit.update(
            kind=record["kind"],
            encoding=record.get("encoding"),
            record_sha256=hashlib.sha256(
                json.dumps(record, sort_keys=True, ensure_ascii=True).encode("utf-8")
            ).hexdigest(),
        )
        saved = out / manifest["origin_supporting_snapshot_path"]
        saved.write_bytes(origin_file.read_bytes())
        manifest["origin_supporting_snapshot_sha256"] = hashlib.sha256(saved.read_bytes()).hexdigest()
        scoped.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        result = _run(CHUNKER, "check", "--manifest", str(scoped))

        assert result.returncode != 0
        assert "scoped-supporting-source-unsupported" in result.stderr
