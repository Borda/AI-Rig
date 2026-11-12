"""Contract: the ``check-plugin-inventories`` hook compares shipped file sets with their reviewed tables.

Two inventories are covered: skill files that spawn ``sw-engineer`` hypothesis investigators, and the hook and
permission-manifest files of the standalone ``cc_oss`` and ``cc_research`` payloads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _repo_checker_run import run_checker

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_plugin_inventories.py"
_REVIEWED_SITE = "plugins/cc_oss/skills/review/SKILL.md"
_INVESTIGATOR_TEXT = "Spawn a sw-engineer per hypothesis in parallel.\n"


def _touch(root: Path, relative: str, text: str = "") -> None:
    """Create ``root / relative`` with ``text``, making parent directories."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker under ``root`` as the hook does."""
    return run_checker(_SCRIPT, "--root", str(root))


def _build_payload(root: Path, plugin: str) -> None:
    """Populate ``plugins/<plugin>`` with exactly the files the checker's table expects, copied from the real tree."""
    base = ROOT / "plugins" / plugin
    for folder, pattern in (("hooks", "*.js"), ("hooks/lib", "*.js"), (".claude-plugin", "*.json")):
        for source in (base / folder).glob(pattern):
            _touch(root, f"plugins/{plugin}/{folder}/{source.name}")


@pytest.fixture(name="payload_root")
def _payload_root(tmp_path: Path) -> Path:
    """Return a tree whose ``cc_oss`` and ``cc_research`` payloads match the reviewed tables."""
    _build_payload(tmp_path, "cc_oss")
    _build_payload(tmp_path, "cc_research")
    return tmp_path


def test_real_repository_passes() -> None:
    """The committed tree satisfies both inventories."""
    result = _run(ROOT)

    assert result.returncode == 0, result.stdout + result.stderr


def test_matching_payloads_pass(payload_root: Path) -> None:
    """A tree holding exactly the expected files is clean."""
    result = _run(payload_root)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        pytest.param("plugins/cc_oss/hooks/commit-guard.js", "unexpected ['commit-guard.js']", id="oss-sibling-hook"),
        pytest.param("plugins/cc_oss/hooks/lib/extra.js", "unexpected ['extra.js']", id="oss-extra-lib-module"),
        pytest.param("plugins/cc_oss/.claude-plugin/stray.json", "unexpected ['stray.json']", id="oss-extra-manifest"),
        pytest.param(
            "plugins/cc_research/hooks/commit-guard.js", "unexpected ['commit-guard.js']", id="research-sibling-hook"
        ),
        pytest.param(
            "plugins/cc_research/.claude-plugin/stray.json", "unexpected ['stray.json']", id="research-extra-manifest"
        ),
    ],
)
def test_unexpected_payload_file_is_reported(payload_root: Path, relative: str, expected: str) -> None:
    """A file outside the table is reported with its directory, name and the table to update."""
    _touch(payload_root, relative)

    result = _run(payload_root)

    assert result.returncode == 1
    assert expected in result.stdout
    assert "PAYLOADS" in result.stdout


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        pytest.param("plugins/cc_oss/hooks/write-guard.js", "missing ['write-guard.js']", id="oss-hook"),
        pytest.param("plugins/cc_research/.claude-plugin/permissions-deny.json", "missing", id="research-manifest"),
    ],
)
def test_missing_payload_file_is_reported(payload_root: Path, relative: str, expected: str) -> None:
    """A file the table lists but the payload lacks is reported by name."""
    (payload_root / relative).unlink()

    result = _run(payload_root)

    assert result.returncode == 1
    assert expected in result.stdout


@pytest.mark.parametrize(
    ("relative", "text", "expected_rc"),
    [
        pytest.param(_REVIEWED_SITE, _INVESTIGATOR_TEXT, 0, id="reviewed-site"),
        pytest.param(
            "plugins/cc_oss/skills/review/modes/scope-context.md",
            _INVESTIGATOR_TEXT,
            0,
            id="reviewed-issue-investigator-mode",
        ),
        pytest.param("plugins/cc_oss/skills/new/SKILL.md", _INVESTIGATOR_TEXT, 1, id="unreviewed-site"),
        pytest.param("plugins/cc_oss/skills/new/modes/a.md", "Hypotheses go to each sw-engineer.\n", 1, id="reversed"),
        pytest.param(
            "plugins/cc_oss/skills/new/SKILL.md", "sw-engineer reviews.\nHypothesis testing.\n", 0, id="split"
        ),
        pytest.param("plugins/cc_oss/skills/tests/x.md", _INVESTIGATOR_TEXT, 0, id="tests-tree"),
    ],
)
def test_investigator_spawn_site_must_be_reviewed(
    payload_root: Path, relative: str, text: str, expected_rc: int
) -> None:
    """A skill file naming ``sw-engineer`` hypothesis investigators on one line must be in the reviewed table.

    The two words must share a line; text under a ``tests`` directory is not shipped skill instruction.
    """
    _touch(payload_root, relative, text)

    result = _run(payload_root)

    assert result.returncode == expected_rc, result.stdout + result.stderr
