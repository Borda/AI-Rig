"""Exercise independently installed loader payloads before shared-file propagation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_FOUNDRY_BIN = _REPO / "plugins/cc_foundry/bin"


def _payload(home: Path, plugin: str, files: tuple[str, ...]) -> Path:
    """Assemble a standalone plugin with the canonical files the lead will propagate."""
    root = home / ".claude/plugins/cache/borda-ai-rig" / plugin / "1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": plugin}), encoding="utf-8")
    (root / "bin").mkdir()
    shutil.copy2(_FOUNDRY_BIN / "resolve_plugin_root.py", root / "bin")
    shutil.copy2(_FOUNDRY_BIN / "render_plugin_root.py", root / "bin")
    plugin_dir = "codemap-py" if plugin == "codemap-py" else f"cc_{plugin}"
    for name in files:
        shutil.copy2(_REPO / "plugins" / plugin_dir / "bin" / name, root / "bin")
    return root


@pytest.mark.parametrize(
    ("plugin", "files", "command", "doc"),
    [
        pytest.param(
            "develop",
            ("dev_load_shared.py", "dev_shared_resolve.py", "dev_run_dir.py"),
            ("dev_load_shared.py", "doc.md"),
            "skills/_shared/doc.md",
            id="develop-shared",
        ),
        pytest.param(
            "research",
            ("resolve_shared.py", "_sentinel_io.py"),
            ("resolve_shared.py", "--emit", "doc.md"),
            "skills/_shared/doc.md",
            id="research-shared",
        ),
        pytest.param(
            "research",
            ("load-agent-reference.py", "resolve_shared.py", "_sentinel_io.py"),
            ("load-agent-reference.py", "agent", "doc.md", "missing"),
            "references/agent/doc.md",
            id="research-sidecar",
        ),
        pytest.param("codemap-py", (), ("render_plugin_root.py", "../doc.md"), "doc.md", id="codemap-renderer"),
        pytest.param("oss", (), ("render_plugin_root.py", "../doc.md"), "doc.md", id="oss-renderer"),
    ],
)
@pytest.mark.integration
def test_installed_loader_emits_owning_root(
    tmp_path: Path,
    plugin: str,
    files: tuple[str, ...],
    command: tuple[str, ...],
    doc: str,
) -> None:
    """Every emission path renders the owning root with no environment plugin-root dependency."""
    root = _payload(tmp_path, plugin, files)
    document = root / doc
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_bytes(b'python "${CLAUDE_PLUGIN_ROOT}/bin/run.py"\r\n')
    environment = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "TMPDIR": str(tmp_path)}
    environment.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run([sys.executable, *command], cwd=root / "bin", env=environment, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == f'python "{root.as_posix()}/bin/run.py"\r\n'.encode()


@pytest.mark.integration
def test_nested_emission_uses_same_validated_root(tmp_path: Path) -> None:
    """A rendered outer document can invoke the renderer again for a nested document."""
    root = _payload(tmp_path, "oss", ())
    outer = root / "outer.md"
    outer.write_text(
        'python "${CLAUDE_PLUGIN_ROOT}/bin/render_plugin_root.py" "${CLAUDE_PLUGIN_ROOT}/inner.md"', encoding="utf-8"
    )
    (root / "inner.md").write_text('python "${CLAUDE_PLUGIN_ROOT}/bin/run.py"', encoding="utf-8")
    environment = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    first = subprocess.run(
        [sys.executable, str(root / "bin/render_plugin_root.py"), str(outer)],
        env=environment,
        capture_output=True,
        check=True,
    )
    assert first.stdout.decode() == f'python "{root.as_posix()}/bin/render_plugin_root.py" "{root.as_posix()}/inner.md"'
    second = subprocess.run(
        [sys.executable, str(root / "bin/render_plugin_root.py"), str(root / "inner.md")],
        env=environment,
        capture_output=True,
        check=True,
    )
    assert second.stdout.decode() == f'python "{root.as_posix()}/bin/run.py"'


@pytest.mark.integration
def test_resolver_failure_stops_workload(tmp_path: Path) -> None:
    """The guarded assignment preserves a resolver security failure before any helper executes."""
    home = tmp_path / "home with space"
    root = _payload(home, "foundry", ())
    registry = home / ".claude/plugins/installed_plugins.json"
    registry.write_text(
        json.dumps(
            {
                "plugins": {
                    "foundry@borda-ai-rig": [
                        {"installPath": str(tmp_path / "outside"), "installedAt": "2026-01-01T00:00:00Z"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    prologue = (
        f'PLUGIN_ROOT=$(python "{root.as_posix()}/bin/resolve_plugin_root.py" --plugin-name foundry) '
        "|| exit $?; export PLUGIN_ROOT\nprintf workload"
    )
    environment = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    result = subprocess.run([shutil.which("bash") or "bash", "-c", prologue], env=environment, capture_output=True)
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"SECURITY" in result.stderr
