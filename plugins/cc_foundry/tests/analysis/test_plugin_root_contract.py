"""Keep executable plugin documents bound to their resolved root and rendering loaders."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script
from _plugin_payload import install_foundry
from check_agent_plugin_root import collect, scan_file
from extract_code_blocks import parse_blocks
from render_plugin_root import render_document

_PLUGINS = Path(__file__).resolve().parents[4] / "plugins"
_NAMES = ("cc_foundry", "cc_oss", "cc_develop", "cc_research", "codemap-py")
_BOOTSTRAP = "${CLAUDE_PLUGIN_ROOT}"
_ROOT_USE = re.compile(r"\$(?:PLUGIN_ROOT\b|\{PLUGIN_ROOT\})/bin/")
_RAW_CAT = re.compile(r"\bcat\s+[\"']?\$\{?([A-Z_]+)\}?/([^\s\"'`]+\.md)")
_RAW_READ = re.compile(
    r"\bRead\s+[\"'`]?\$\{?(?:PLUGIN_ROOT|_FOUNDRY_SHARED|_OSS_SHARED|_DEV_SHARED|_RESEARCH_SHARED|_SHARED"
    r"|_FS|_MODES|_TPL|AUDIT_TPL)\}?/([^\s\"'`]+\.md)"
)


def _documents(plugin: Path) -> list[Path]:
    """Find the live skill and reference documents that the plugin emits."""
    return collect(plugin, [])


def _unresolved_blocks(plugin: Path) -> list[str]:
    """Identify helper calls with no preceding resolver or checked setup sentinel."""
    findings = []
    for document in _documents(plugin):
        for block in parse_blocks(document.read_text(encoding="utf-8"), str(document)):
            if not block.is_code or block.lang_detected != "bash":
                continue
            lines = block.content.splitlines()
            uses = [index for index, line in enumerate(lines) if _ROOT_USE.search(line)]
            if not uses:
                continue
            prefix = lines[: uses[0] + 1]
            guarded = any(
                line.lstrip().startswith("PLUGIN_ROOT=$(")
                and "resolve_plugin_root.py" in line
                and "|| exit $?; export PLUGIN_ROOT" in line
                for line in prefix
            )
            setup = document == plugin / "skills/setup/SKILL.md" and any(
                "setup-plugin-root-${CSID}" in line
                and ("IFS= read -r PLUGIN_ROOT" in line or 'PLUGIN_ROOT="$_PR"' in line)
                for line in prefix
            )
            bootstrap_setup = document == plugin / "skills/setup/SKILL.md" and any(
                line.lstrip().startswith("PLUGIN_ROOT=$(") and "resolve_plugin_root.py" in line for line in prefix
            )
            checked = any(
                ('[ -n "$PLUGIN_ROOT" ] ||' in line or '[ -z "$PLUGIN_ROOT" ] &&' in line) and "exit 1" in line
                for line in prefix
            )
            if not guarded and not ((setup or bootstrap_setup) and checked):
                findings.append(f"{document.relative_to(_PLUGINS)}:{block.line_start + uses[0] + 1}")
    return findings


def _unrendered_deliveries(plugin: Path) -> list[str]:
    """Find raw shell and Read-tool deliveries of known token-bearing fragments."""
    documents = _documents(plugin)
    affected = {
        path.name for path in documents if _BOOTSTRAP in path.read_text(encoding="utf-8") and path.name != "SKILL.md"
    }
    findings = []
    for document in documents:
        findings.extend(
            f"{document.relative_to(_PLUGINS)}:{number}: {line.strip()}"
            for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1)
            if any(Path(match.group(1)).name in affected for match in _RAW_READ.finditer(line))
        )
        for block in parse_blocks(document.read_text(encoding="utf-8"), str(document)):
            if not block.is_code or block.lang_detected != "bash":
                continue
            for offset, line in enumerate(block.content.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                loaded = {
                    Path(match.group(2)).name
                    for match in _RAW_CAT.finditer(line)
                    if match.group(1) != "_CODEMAP_SHARED"
                    or _BOOTSTRAP
                    in (_PLUGINS / "codemap-py/claude-skills/_shared" / match.group(2)).read_text(encoding="utf-8")
                }
                if loaded.intersection(affected):
                    findings.append(f"{document.relative_to(_PLUGINS)}:{block.line_start + offset}: {line.strip()}")
    return findings


@pytest.mark.parametrize("name", _NAMES)
def test_helper_blocks_resolve_before_using_plugin_root(name: str) -> None:
    """A separate Bash invocation must establish its own helper root before its first call."""
    assert _unresolved_blocks(_PLUGINS / name) == []


@pytest.mark.parametrize("name", _NAMES)
def test_live_documents_do_not_default_the_host_root(name: str) -> None:
    """Skills, nested fragments and agents must retain the literal renderable bootstrap token."""
    assert [finding for path in _documents(_PLUGINS / name) for finding in scan_file(path)] == []


@pytest.mark.parametrize("name", _NAMES)
def test_bootstrap_fragments_are_delivered_through_rendering(name: str) -> None:
    """Raw executable document loads cannot leave a bootstrap token for the recipient's shell."""
    assert _unrendered_deliveries(_PLUGINS / name) == []


@pytest.mark.integration
@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
def test_distill_nested_prompt_preserves_literal_install_path(tmp_path: Path) -> None:
    """A rendered nested prompt must preserve spaces, dollars and apostrophes in its resolver operand."""
    home = tmp_path / "space $dollar apostrophe'"
    root = install_foundry(home)
    document = (_PLUGINS / "cc_foundry/skills/distill/modes/executables.md").read_text(encoding="utf-8")
    rendered = render_document(document, root).splitlines()
    prompt = next(
        index for index, line in enumerate(rendered) if line.startswith('Agent(subagent_type="foundry:sw-engineer"')
    )
    prologue = rendered[prompt + 1].replace('\\"', '"')

    result = subprocess.run(
        ["bash", "-c", 'python() { "$TEST_PYTHON" "$@"; };\n' + prologue + '\nprintf "%s\\n" "$PLUGIN_ROOT"'],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home), "TEST_PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == root.as_posix() + "\n"
    assert _BOOTSTRAP not in prologue
