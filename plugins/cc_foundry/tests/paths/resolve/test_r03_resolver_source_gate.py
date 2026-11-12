"""Exercise source resolution from the executing resolver's checkout and an unrelated repository."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import resolve_shared_path


@pytest.mark.parametrize("own_checkout", [True, False])
def test_source_manifest_requires_executing_resolver_repository(tmp_path: Path, own_checkout: bool) -> None:
    """A copied manifest in another repository cannot substitute its instructions for the resolver's own source."""
    source_root = tmp_path / "trusted"
    script = source_root / "plugins/cc_foundry/bin/resolve_shared_path.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(resolve_shared_path.__file__, script)
    cwd = source_root if own_checkout else tmp_path / "unrelated"
    plugin = cwd / "plugins/cc_foundry"
    manifest = plugin / ".claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"foundry"}', encoding="utf-8")
    shared = plugin / "skills/_shared"
    shared.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [sys.executable, str(script), "foundry", "skills/_shared"],
        cwd=cwd,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home), "CLAUDE_PLUGIN_ROOT": ""},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == (0 if own_checkout else 1)
    assert result.stdout.strip() == ("plugins/cc_foundry/skills/_shared" if own_checkout else "")
