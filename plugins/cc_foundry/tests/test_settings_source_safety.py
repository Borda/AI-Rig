"""Keep setup defaults inside the executing plugin's installation."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import merge_settings_permissions
import pytest
import settings_json


def test_merge_env_refuses_defaults_outside_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository-owned defaults file cannot inject environment keys into user settings."""
    monkeypatch.chdir(tmp_path)
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    monkeypatch.setattr(settings_json, "_PLUGIN_ROOT", plugin, raising=False)
    settings = tmp_path / "settings.json"
    settings.write_text('{"theme":"dark"}', encoding="utf-8")
    defaults = tmp_path / "defaults.json"
    defaults.write_text('{"INJECTED":"value"}', encoding="utf-8")
    assert settings_json.main(["merge-env", str(settings), "--defaults", str(defaults)]) == 2
    assert settings.read_text(encoding="utf-8") == '{"theme":"dark"}'


@pytest.mark.parametrize("writer", ["settings_json.py", "jq_write.py"])
@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed on this host")
def test_settings_writers_share_permission_merge_lock(tmp_path: Path, writer: str) -> None:
    """A settings writer waits for a permission merge holding the lexical settings lock."""
    settings = tmp_path / "settings.json"
    settings.write_text('{"theme":"dark"}', encoding="utf-8")
    script = Path(settings_json.__file__).with_name(writer)
    options = {
        "settings_json.py": ["set-advisor", str(settings), "--model", "sonnet"],
        "jq_write.py": [str(settings), '.theme="light"'],
    }[writer]
    with merge_settings_permissions._settings_lock(settings):
        process = subprocess.Popen(
            [sys.executable, str(script), *options], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.4)
            assert settings.read_text(encoding="utf-8") == '{"theme":"dark"}'
        finally:
            process.kill()
            process.communicate(timeout=5)
