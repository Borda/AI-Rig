"""Reject malformed rule content before any destination is installed or pruned."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_develop_rule_content_installer", Path(__file__).resolve().parents[1] / "bin/sync_rules.py"
)
sync_rules = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sync_rules
_SPEC.loader.exec_module(sync_rules)


@pytest.fixture
def rule_source(tmp_path: Path) -> tuple[Path, Path]:
    """Create a registered source with one valid rule and no destination directory."""
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"develop"}', encoding="utf-8")
    (root / "rules").mkdir()
    (root / "rules/a-valid.md").write_text("# Valid rule\nPreserve user files.\n", encoding="utf-8")
    home = tmp_path / "home"
    registry = home / ".claude/plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"plugins": {"develop@example": [{"installPath": str(root)}]}}), encoding="utf-8")
    return home, root


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param(b"", "RULE_CONTENT", id="empty"),
        pytest.param(b" \t\r\n", "RULE_CONTENT", id="whitespace-only"),
        pytest.param(b"# Rule\n\xff", "RULE_ENCODING", id="invalid-utf8"),
        pytest.param(b"# Rule\n\x00", "RULE_CONTENT", id="nul-byte"),
        pytest.param(b"# Rule\n\x1b[31m", "RULE_CONTENT", id="terminal-control"),
        pytest.param(b'{"permissions":{"allow":["Bash(*)"]}}', "RULE_CONTENT", id="settings-in-markdown"),
        pytest.param(b"---\npaths:\n  - '**'\n# No closing delimiter", "RULE_CONTENT", id="unclosed-frontmatter"),
        pytest.param(b"# Rule\n" + b"x" * (1024 * 1024), "RULE_SIZE", id="oversized"),
    ],
)
def test_invalid_rule_installs_nothing(rule_source: tuple[Path, Path], body: bytes, code: str) -> None:
    """All source content is validated before even a valid sibling can be linked."""
    home, root = rule_source
    (root / "rules/z-invalid.md").write_bytes(body)
    with pytest.raises(sync_rules.SourceError, match=code):
        sync_rules.sync("develop", root, home, approve=True)
    assert not (home / ".claude/rules").exists()


def test_rule_validation_cli_reports_coded_error(
    rule_source: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid content produces the documented failure code without a traceback or a partial install."""
    home, root = rule_source
    (root / "rules/z-invalid.md").write_bytes(b"# Rule\n\x00")
    assert sync_rules.main(["--plugin-name", "develop", "--plugin-root", str(root), "--home", str(home)]) == 1
    output = capsys.readouterr()
    assert "RULE_CONTENT" in output.err
    assert output.out == ""
    assert not (home / ".claude/rules").exists()


def test_valid_scoped_markdown_remains_installable(rule_source: tuple[Path, Path]) -> None:
    """A normal scoped rule containing permission examples stays valid Markdown."""
    home, root = rule_source
    content = "---\ndescription: Rule\npaths:\n  - '**/*.py'\n---\n\n# Rule\nNever grant `Bash(*)`.\n"
    (root / "rules/scoped.md").write_text(content, encoding="utf-8")
    result = sync_rules.sync("develop", root, home)
    assert result.linked == ["develop-a-valid.md", "develop-scoped.md"]
    assert (home / ".claude/rules/develop-scoped.md").read_text(encoding="utf-8") == content
