"""Keep the Claude and Codex ephemeral dependency overlays on one index-routing content scan.

Both review hosts refuse to fetch a package whose index routing they cannot determine. The content scan is what finds
routing set outside the parsed dependency files, such as an index variable in a CI workflow; a pattern edited on one
host only would let that host fetch a privately routed name the other host keeps off the public index.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_PROCEDURE = ROOT / "plugins/cc_oss/skills/review/templates/agent-prompts.md"
CODEX_CONTRACT = ROOT / "plugins/codex-rig/shared/native-skill-contract.md"
GIT = shutil.which("git")


def _scan_patterns(path: Path) -> list[str]:
    """Return the ``-e`` patterns of the routing content-scan ``git grep`` command one host's procedure prescribes."""
    text = path.read_text(encoding="utf-8")
    command = re.search(r"`(git grep -n -I -i -E [^`]+)`", text)
    assert command is not None, f"no routing content-scan command in {path.name}"
    return re.findall(r"-e '([^']+)'", command.group(1))


def test_both_hosts_run_the_same_routing_scan() -> None:
    """The Claude procedure and the Codex contract prescribe byte-identical scan patterns."""
    assert _scan_patterns(CLAUDE_PROCEDURE) == _scan_patterns(CODEX_CONTRACT)


@pytest.fixture
def routing_tree(tmp_path: Path) -> Path:
    """Write files that set or mention package-index routing in places no dependency-file parse reads."""
    files = {
        ".github/workflows/tests.yml": "    env:\n      UV_INDEX_URL: ${{ secrets.ACME_INDEX }}\n",
        "Dockerfile": "ARG PIP_EXTRA_INDEX_URL\n",
        "tox.ini": "[testenv]\ninstall_command = pip install -i https://packages.example.invalid/simple {opts}\n",
        "requirements/prod.in": "--find-links /wheels\n",
        "requirements/base.in": "-i https://packages.example.invalid/simple\n",
        "Pipfile": '[[source]]\nurl = "https://packages.example.invalid/simple"\n',
        "scripts/build.sh": "uv pip install --index https://packages.example.invalid/simple acme\n",
        "clean/build.sh": "rm -f build\ngrep -i pattern notes.txt\n",
        "clean/module.py": 'subprocess.run(["rm", "-f", path])\nprint("indexing")\n',
    }
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path


@pytest.mark.integration
@pytest.mark.skipif(GIT is None, reason="git is required to execute the prescribed routing scan")
def test_routing_scan_finds_routing_outside_dependency_files(routing_tree: Path) -> None:
    """The prescribed scan reports every routing setting in CI, container, tox, nested requirements and scripts.

    Ordinary short flags elsewhere (``rm -f``, ``grep -i``) stay quiet, so the scan does not refuse every repository
    that merely contains a shell script.
    """
    patterns = [arg for pattern in _scan_patterns(CODEX_CONTRACT) for arg in ("-e", pattern)]
    result = subprocess.run(
        [GIT, "-C", str(routing_tree), "grep", "--no-index", "-l", "-I", "-i", "-E", *patterns, "--", "."],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sorted(Path(line).as_posix() for line in result.stdout.splitlines()) == [
        ".github/workflows/tests.yml",
        "Dockerfile",
        "Pipfile",
        "requirements/base.in",
        "requirements/prod.in",
        "scripts/build.sh",
        "tox.ini",
    ]
