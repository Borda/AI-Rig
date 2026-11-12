"""Keep release receipt repositories and Git presentation hooks outside the trust boundary."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered

SHARED = Path(__file__).resolve().parents[3] / "shared"


@pytest.fixture
def release(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load release validation with its sibling gate helper available."""
    monkeypatch.syspath_prepend(str(SHARED))
    return load_module_registered("sweep_release_git_safety", SHARED / "release_evidence.py")


def test_receipt_repository_must_match_gate_source(release: ModuleType, tmp_path: Path) -> None:
    """Reject artifact-selected repositories before attempting any Git inspection."""
    source, hostile = tmp_path / "source", tmp_path / "hostile"
    source.mkdir()
    hostile.mkdir()
    metadata = {"release_evidence": {"schema_version": 1, "repository": str(hostile)}}
    with pytest.raises(SystemExit, match="release-evidence-repository-source-mismatch"):
        release.validate_release_evidence(metadata, tmp_path, set(), expected_repository=source)


@pytest.mark.integration
def test_patch_id_ignores_repository_textconv(
    release: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch equivalence uses actual blob bytes and never executes a configured text converter."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "source.txt").write_text("source\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("*.txt diff=hostile\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    head = (
        subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )
    expected = release._patch_id(tmp_path, head)
    marker = tmp_path / "executed"
    script = tmp_path / "textconv.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint('altered')\n", encoding="utf-8")
    command = shlex.join([Path(sys.executable).as_posix(), script.as_posix()])
    subprocess.run(["git", "-C", str(tmp_path), "config", "diff.hostile.textconv", command], check=True)
    assert release._patch_id(tmp_path, head) == expected
    assert not marker.exists()
