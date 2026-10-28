"""Verify scoped text-only review excludes unsupported supporting source."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2]
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


SYMLINKS_SUPPORTED = _supports_symlinks()


def _git(root: Path, *arguments: str) -> str:
    """Run real Git against the isolated repository."""
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


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


def _fixture(tmp_path: Path, kind: str, *, untracked_nul: bool = False) -> tuple[Path, Path, Path, Path]:
    """Prepare an origin-bound plan with one unchanged supporting endpoint."""
    original = tmp_path / "original"
    original.mkdir()
    _git(original, "init", "-q")
    _git(original, "config", "user.name", "Fixture")
    _git(original, "config", "user.email", "fixture@example.invalid")
    _git(original, "config", "core.autocrlf", "false")
    (original / "primary.py").write_bytes(b"value = 1\n")
    (original / "target.py").write_bytes(b"target = 1\n")
    _write_support(original, kind)
    _git(original, "add", ".")
    _git(original, "commit", "-qm", "base")
    base = _git(original, "rev-parse", "HEAD")
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


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("binary", id="binary"),
        pytest.param("nul-utf8", id="nul-utf8"),
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="host cannot create symbolic links"),
            id="symlink",
        ),
    ],
)
def test_scoped_plan_rejects_unsupported_support(tmp_path: Path, kind: str) -> None:
    """A text primary must not admit an unsupported binary or link endpoint."""
    _, _, out, assignments = _fixture(tmp_path, kind)
    result = _run(
        CHUNKER,
        "scoped-plan",
        "--manifest",
        str(out / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(out / "scoped.json"),
    )
    assert result.returncode != 0
    assert "scoped-supporting-source-unsupported" in result.stderr


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("binary", id="binary"),
        pytest.param("nul-utf8", id="nul-utf8"),
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="host cannot create symbolic links"),
            id="symlink",
        ),
    ],
)
def test_scoped_check_rejects_rebound_unsupported_support(tmp_path: Path, kind: str) -> None:
    """Even coherently rebound prior artifacts cannot bypass final source checking."""
    original, mirror, out, assignments = _fixture(tmp_path, "text")
    scoped = out / "scoped.json"
    generated = _run(
        CHUNKER,
        "scoped-plan",
        "--manifest",
        str(out / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(scoped),
    )
    assert generated.returncode == 0, generated.stderr
    _write_support(original, kind)
    _write_support(mirror, kind)
    origin_file = tmp_path / "origin-support.json"
    mirror_file = tmp_path / "mirror-support.json"
    for root, output in ((original, origin_file), (mirror, mirror_file)):
        snapshot = _run(
            COLLECTOR, "--snapshot", "--repository", str(root), "--scope-path", "support.py", "--out", str(output)
        )
        assert snapshot.returncode == 0, snapshot.stderr
    manifest = json.loads(scoped.read_bytes())
    record = json.loads(mirror_file.read_bytes())["files"][0]
    unit = manifest["supporting_units"][0]
    unit.update(
        kind=record["kind"],
        encoding=record.get("encoding"),
        record_sha256=hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest(),
    )
    saved = out / manifest["origin_supporting_snapshot_path"]
    saved.write_bytes(origin_file.read_bytes())
    manifest["origin_supporting_snapshot_sha256"] = hashlib.sha256(saved.read_bytes()).hexdigest()
    scoped.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    result = _run(CHUNKER, "check", "--manifest", str(scoped))
    assert result.returncode != 0
    assert "scoped-supporting-source-unsupported" in result.stderr


def test_scoped_plan_keeps_supported_text(tmp_path: Path) -> None:
    """A normal unchanged text caller remains available for scoped review."""
    _, _, out, assignments = _fixture(tmp_path, "text")
    result = _run(
        CHUNKER,
        "scoped-plan",
        "--manifest",
        str(out / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(out / "scoped.json"),
    )
    assert result.returncode == 0, result.stderr


def test_scoped_plan_rejects_nul_untracked_primary(tmp_path: Path) -> None:
    """UTF-8-decodable binary bytes cannot hide in an untracked primary file."""
    _, _, out, assignments = _fixture(tmp_path, "text", untracked_nul=True)
    result = _run(
        CHUNKER,
        "scoped-plan",
        "--manifest",
        str(out / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(out / "scoped.json"),
    )
    assert result.returncode != 0
    assert "scoped-unsupported-source-unreviewed" in result.stderr
