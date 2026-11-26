"""Every per-run sentinel a later resolve step reads is emptied when a run starts, so no run reads the previous run's.

Step 4 publishes the PR's refs (head commit, fork remote, cross-repo flag, ...). Report mode never reaches that publish,
so without a reset at Step 1 a report run that follows a PR run in the same session would read the previous PR's head:
its gap ledger, drift markers and remote-state line would all be keyed to the wrong PR.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _oss_shell_plugin import render_shell
from _text_io import bash_block_after, write_lf

_PLUGIN = Path(__file__).resolve().parents[3]
_MODES = _PLUGIN / "skills/resolve/modes"
_PR_REFS = _PLUGIN / "bin/resolve_pr_refs.py"
_BASH = shutil.which("bash")
_SESSION = "resolve-run-start-test"
#: A shell redirect that writes one session sentinel, capturing its name.
_SENTINEL_WRITE = re.compile(r'>\s*"\$\{TMPDIR:-/tmp\}/(resolve-[a-z0-9-]+)-\$\{CSID\}"')


def _published_by_pr_refs() -> set[str]:
    """Return every sentinel name the PR-ref helper can write, read from its string constants."""
    tree = ast.parse(_PR_REFS.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(r"resolve-[a-z-]+", node.value)
    }


def _written_by(mode: str) -> set[str]:
    """Return the session sentinels a mode file's shell lines write."""
    lines = (_MODES / mode).read_text(encoding="utf-8").splitlines()
    return {match for line in lines for match in _SENTINEL_WRITE.findall(line.split("  #", 1)[0])}


def test_every_step_4_sentinel_is_written_at_run_start() -> None:
    """Each sentinel Step 4 publishes is emptied or rewritten by Step 1, before any step can read it."""
    step_4 = _published_by_pr_refs() | _written_by("checkout.md")
    assert {"resolve-pr-head-oid", "resolve-fork-remote", "resolve-base-ref"} <= step_4, "the scan finds Step 4's refs"
    assert sorted(step_4 - _written_by("preflight.md")) == []


@pytest.mark.skipif(_BASH is None or shutil.which("git") is None, reason="the parse block is Bash")
def test_report_run_after_a_pr_run_starts_without_its_pr_head(tmp_path: Path) -> None:
    """A report-mode run's parse block empties the PR head a previous PR run left in the same session."""
    repo, tmp = tmp_path / "repo", tmp_path / "tmp"
    repo.mkdir()
    tmp.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    stale = {
        "resolve-pr-head-oid": "c" * 40,
        "resolve-fork-remote": "origin",
        "resolve-is-cross-repo": "false",
        "resolve-remote-imports": f"base {'c' * 40} {'d' * 40}",
    }
    for name, value in stale.items():
        write_lf(tmp / f"{name}-{_SESSION}", f"{value}\n")
    block = bash_block_after((_MODES / "preflight.md").read_text(encoding="utf-8"), "Parse $ARGUMENTS:")
    env = os.environ | {
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp),
        "ARGUMENTS": "report",
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run(
        [_BASH, "-c", render_shell(block, _PLUGIN, "oss")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in stale:
        assert (tmp / f"{name}-{_SESSION}").read_text(encoding="utf-8") == "", f"{name} is emptied at run start"
