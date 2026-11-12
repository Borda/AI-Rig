"""Verify literal bootstrap rendering and owning-install validation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from render_plugin_root import render_document, render_file, serialize_root


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'python "${CLAUDE_PLUGIN_ROOT}/bin/run.py"', 'python "/space dir/$a`b\\c"d/bin/run.py"', id="double"
        ),
        pytest.param(
            "python '${CLAUDE_PLUGIN_ROOT}/bin/run.py'", "python '/space dir/$a`b\\c\"d/bin/run.py'", id="single"
        ),
    ],
)
def test_root_shell_roundtrip(source: str, expected: str) -> None:
    """Rendered arguments evaluate to literal path data without expanding dollars or backticks."""
    root = '/space dir/$a`b\\c"d'
    rendered = render_document(source, root)
    command = rendered.replace("python ", "printf '%s' ", 1)
    result = subprocess.run([shutil.which("bash") or "bash", "-c", command], capture_output=True, check=True)
    assert result.stdout.decode() == root + "/bin/run.py"
    assert render_document(expected, root) == expected


def test_single_quote_is_literal_inside_single_quotes() -> None:
    """A path apostrophe closes and reopens a single-quoted operand safely."""
    assert render_document("'${CLAUDE_PLUGIN_ROOT}/bin/a.py'", "/a'b") == "'/a'\\''b/bin/a.py'"


def test_unquoted_root_is_one_shell_operand() -> None:
    """Whitespace in an unquoted token is protected without quoting the whole document."""
    assert render_document("python ${CLAUDE_PLUGIN_ROOT}/bin/a.py", "/a b") == "python '/a b'/bin/a.py"


def test_nested_substitution_restores_quote_context() -> None:
    """Inner command quoting does not inherit the outer capture's quotes."""
    source = 'X="$(python "${CLAUDE_PLUGIN_ROOT}/a.py")"\npython "${CLAUDE_PLUGIN_ROOT}/b.py"'
    expected = 'X="$(python "/a\\$b/a.py")"\npython "/a\\$b/b.py"'
    assert render_document(source, "/a$b") == expected


def test_comment_apostrophe_does_not_change_shell_context() -> None:
    """A possessive word in a comment cannot alter escaping on the next command."""
    source = '# plugin\'s helper\npython "${CLAUDE_PLUGIN_ROOT}/a.py"'
    assert render_document(source, '/a"$b') == '# plugin\'s helper\npython "/a\\"\\$b/a.py"'


def test_backtick_substitution_tracks_inner_quote() -> None:
    """Legacy captures render the token according to the nested command's own quotes."""
    source = 'X="`python "${CLAUDE_PLUGIN_ROOT}/a.py"`"'
    assert render_document(source, "/a$b") == 'X="`python "/a\\$b/a.py"`"'


def test_markdown_fence_does_not_open_backtick_substitution() -> None:
    """Fence delimiters reset lexical context instead of acting as shell backticks."""
    source = '```bash\nX="`python "${CLAUDE_PLUGIN_ROOT}/a.py"`"\n```\n'
    expected = '```bash\nX="`python "/a\\$b/a.py"`"\n```\n'
    assert render_document(source, "/a$b") == expected


@pytest.mark.parametrize(
    ("root", "expected"),
    [
        pytest.param(r"C:\Users\A B\plugin", "C:/Users/A B/plugin", id="drive"),
        pytest.param(r"\\server\share\A B", "//server/share/A B", id="unc"),
    ],
)
def test_windows_paths_preserve_absolute_coordinates(root: str, expected: str) -> None:
    """Windows paths serialize independently of the machine running the renderer."""
    assert serialize_root(root) == expected
    assert render_document('"${CLAUDE_PLUGIN_ROOT}/bin/a.py"', root) == f'"{expected}/bin/a.py"'


def _install(home: Path, version: str) -> Path:
    """Create a valid install with a Markdown fragment containing a bootstrap token."""
    root = home / ".claude/plugins/cache/borda-ai-rig/foundry" / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin/resolve_plugin_root.py").write_text("# resolver\n", encoding="utf-8")
    (root / "mode.md").write_bytes(b'python "${CLAUDE_PLUGIN_ROOT}/bin/a.py"\r\n')
    return root


def test_render_file_uses_validated_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The selected owning install renders only its literal bootstrap token and preserves CRLF."""
    root = _install(tmp_path, "1.0.0")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert render_file(root / "mode.md") == f'python "{root.as_posix()}/bin/a.py"\r\n'.encode()


def test_render_file_rejects_mixed_install_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old loaded document cannot silently bootstrap the newer selected version."""
    old = _install(tmp_path, "1.0.0")
    active = _install(tmp_path, "2.0.0")
    registry = tmp_path / ".claude/plugins/installed_plugins.json"
    registry.write_text(
        json.dumps(
            {
                "plugins": {
                    "foundry@borda-ai-rig": [
                        {"installPath": str(active), "installedAt": "2026-01-01T00:00:00Z"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(ValueError, match="selected plugin install"):
        render_file(old / "mode.md")


def test_no_token_file_is_byte_identical(tmp_path: Path) -> None:
    """Legacy documents without bootstrap tokens need no root selection or decoding."""
    path = tmp_path / "legacy.md"
    path.write_bytes(b"raw\xff\r\n")
    assert render_file(path) == b"raw\xff\r\n"
