"""Contract: the ``check-codex-private-refs`` hook keeps private planning material out of the Codex Rig release.

The scan covers every UTF-8 file of the payload plus the parallel-execution documents, and mirrors the payload scope of
the plugin's package builder.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _repo_checker_run import run_checker

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_codex_private_refs.py"
_PLUGIN = "plugins/codex-rig"
_DOCUMENTS = (
    "assets/images/parallel-execution-architecture.svg",
    "README.md",
    "shared/portable-parallel-read.md",
    "skills/code-review/SKILL.md",
    "skills/code-review/references/extra.md",
    "skills/implement/SKILL.md",
    "skills/manage/SKILL.md",
)


def _write(root: Path, relative: str, content: str | bytes) -> None:
    """Write ``content`` at ``root / relative``, making parent directories."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker under ``root`` as the hook does."""
    return run_checker(_SCRIPT, "--root", str(root))


@pytest.fixture(name="release_root")
def _release_root(tmp_path: Path) -> Path:
    """Return a clean tree holding every parallel-execution document."""
    for document in _DOCUMENTS:
        _write(tmp_path, f"{_PLUGIN}/{document}", "Clean published text.\n")
    return tmp_path


def test_real_repository_passes() -> None:
    """The committed Codex Rig payload and documents are free of private references."""
    result = _run(ROOT)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("content", "reference"),
    [
        pytest.param("see plan_rollout-v2.md for details\n", "plan_rollout-v2.md", id="plan-file-name"),
        pytest.param("tracked as W505 upstream\n", "W505", id="work-item"),
        pytest.param("tracked as H12 upstream\n", "H12", id="hardening-item"),
        pytest.param("ships in P3a\n", "P3a", id="rollout-phase"),
        pytest.param("run /Users/jirka/work/tool\n", "/Users/jirka/work/tool", id="posix-personal-path"),
        pytest.param("run /Users/<name>/tool\n", "/Users/<name>/tool", id="placeholder-home-path"),
        pytest.param("run C:\\Users\\jirka\\tool\n", "C:\\Users\\jirka\\tool", id="windows-personal-path"),
        pytest.param("This is a GA candidate build\n", "GA candidate", id="retired-phrase"),
        pytest.param("lists plugin-package.json\n", "plugin-package.json", id="retired-file-name"),
    ],
)
def test_private_reference_in_payload_is_reported(release_root: Path, content: str, reference: str) -> None:
    """Planning identifiers and personal paths in any payload file are reported with the file and the match."""
    _write(release_root, f"{_PLUGIN}/shared/notes.md", content)

    result = _run(release_root)

    assert result.returncode == 1
    assert f"{_PLUGIN}/shared/notes.md" in result.stdout
    assert reference in result.stdout


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        pytest.param("package-manifest.json", "W505", id="package-manifest"),
        pytest.param("scripts/__pycache__/x.pyc.txt", "W505", id="bytecode-cache"),
        pytest.param(".reports/run/out.md", "W505", id="reports-tree"),
        pytest.param("shared/binary.bin", b"\xff\xfeW505", id="binary-file"),
        pytest.param("shared/clean.md", "workflow 12 and /usr/local placeholders\n", id="clean-prose"),
    ],
)
def test_excluded_or_clean_content_passes(release_root: Path, relative: str, content: str | bytes) -> None:
    """Build artefacts, binary files and ordinary prose are outside the payload scan."""
    _write(release_root, f"{_PLUGIN}/{relative}", content)

    result = _run(release_root)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("document", [pytest.param(name, id=name) for name in _DOCUMENTS])
@pytest.mark.parametrize(
    "link",
    [
        pytest.param("see .plans/active/todo.md", id="plans-directory"),
        pytest.param("see plan_multi-agent-parallelization", id="private-plan-name"),
    ],
)
def test_private_plan_link_in_parallel_document_is_reported(release_root: Path, document: str, link: str) -> None:
    """A published parallel-execution document must not link the private plans directory or plan."""
    _write(release_root, f"{_PLUGIN}/{document}", f"Intro.\n{link}\n")

    result = _run(release_root)

    assert result.returncode == 1
    assert f"{_PLUGIN}/{document}:2:" in result.stdout


def test_missing_parallel_document_is_reported(release_root: Path) -> None:
    """A renamed or removed published document fails loudly instead of shrinking the scan."""
    (release_root / _PLUGIN / "README.md").unlink()

    result = _run(release_root)

    assert result.returncode == 1
    assert f"{_PLUGIN}/README.md: published parallel-execution document is missing" in result.stdout
