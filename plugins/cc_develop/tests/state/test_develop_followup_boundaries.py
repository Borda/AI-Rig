"""Exercise source ownership and Codemap gate session trust boundaries."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import dev_codemap_gate
import dev_run_dir
import dev_shared_resolve
import pytest


def test_forged_source_manifest_does_not_establish_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository cannot nominate its own protocol files by copying the develop manifest name."""
    plugin = tmp_path / "plugins" / "cc_develop"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "develop"}', encoding="utf-8")
    (plugin / "skills" / "_shared").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dev_shared_resolve.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "forged")
    assert dev_shared_resolve.main(["--init"]) == 1
    assert not (tmp_path / "dev-shared-forged").exists()


@mock.patch("dev_codemap_gate._run_codemap_resolve", autospec=True, return_value=("false", 0))
def test_gate_reads_sanitized_session_flag(resolve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate input and output use exactly the existing session-path sanitizer."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "a/../b")
    dev_run_dir.write_sentinel("dev-fix-codemap", "off")
    assert dev_codemap_gate.main(["gate", "fix"]) == 0
    resolve.assert_called_once_with(Path(dev_codemap_gate.__file__).parent, "off")
    assert dev_run_dir.read_sentinel("dev-fix-codemap-enabled") == "false"


@mock.patch("dev_codemap_gate._run_codemap_resolve", autospec=True, return_value=("false", 0))
def test_gate_ignores_foreign_owned_flag(resolve, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign-owned mode flag is treated as absent before calling the resolver."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "foreign")
    dev_run_dir.write_sentinel("dev-fix-codemap", "strict")
    monkeypatch.setattr(dev_run_dir, "_owned_by_caller", lambda status: False)
    monkeypatch.setattr(dev_codemap_gate, "_owned_by_caller", lambda status: False)
    assert dev_codemap_gate.main(["gate", "fix"]) == 0
    resolve.assert_called_once_with(Path(dev_codemap_gate.__file__).parent, "auto")


def test_gate_ignores_foreign_owned_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even an otherwise valid plan directory cannot be selected by a foreign session sentinel."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "foreign")
    namespace = tmp_path / "dev-plan-42-foreign"
    namespace.mkdir()
    dev_run_dir.write_sentinel("dev-plan-ns-current", str(namespace))
    monkeypatch.setattr(dev_run_dir, "_owned_by_caller", lambda status: False)
    with pytest.raises(SystemExit, match="1"):
        dev_codemap_gate._resolve_paths("plan", tmp_path)


def test_gate_ignores_foreign_owned_plan_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The opened raw flag in a plan namespace receives the same ownership check as flat flags."""
    raw = tmp_path / "codemap-raw"
    raw.write_text("strict\n", encoding="utf-8")
    monkeypatch.setattr(dev_codemap_gate, "_owned_by_caller", lambda status: False)
    assert dev_codemap_gate._first_line(raw) == ""


def test_gate_resolver_currency_uses_same_session_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child resolver's currency write agrees with the gate's sanitized output path."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "a/../b")
    dev_run_dir.write_sentinel("dev-fix-codemap", "off")
    assert dev_codemap_gate.main(["gate", "fix"]) == 0
    assert dev_run_dir.read_sentinel("dev-codemap-currency") == "off"


def test_gate_refuses_empty_sanitized_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An identity stripped to nothing cannot select different default sessions in the gate and child."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "../")
    assert dev_codemap_gate.main(["gate", "fix", "--skip"]) == 1
    assert list(tmp_path.iterdir()) == []
