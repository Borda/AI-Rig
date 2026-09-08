"""Tests for ``bin/check_readme_drift.py``.

The checker verifies two independently selectable classes of README fact against disk: an explicit version marker
against the plugin manifest, and ``.py``/``.sh`` bin-script references against files that actually exist in the plugin.
These tests build disposable plugin trees under ``tmp_path`` and assert the exit code and findings for clean, version
drift, stale-reference, and per-subcheck cases.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent / "bin" / "check_readme_drift.py"
_spec = importlib.util.spec_from_file_location("check_readme_drift", _MOD_PATH)
assert _spec and _spec.loader
crd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crd)


def _messages(findings: list[crd.Finding]) -> list[str]:
    """Return the message text of each finding, for substring assertions.

    Args:
        findings: Findings returned by ``check_plugin``.

    Returns:
        One message string per finding, in the original order.

    Examples:
        >>> _messages([crd.Finding(crd.FindingKind.VERSION, "old")])
        ['old']
    """
    return [f.message for f in findings]


def _make_plugin(root: Path, *, version: str, readme: str, bin_scripts: tuple[str, ...] = ()) -> Path:
    """Create a minimal plugin tree and return its directory.

    Args:
        root: Parent directory (typically ``tmp_path``).
        version: Version string written to ``.claude-plugin/plugin.json``.
        readme: Contents of ``README.md``.
        bin_scripts: Basenames to create under ``bin/``.

    Returns:
        The plugin directory path.
    """
    plugin = root / "myplugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        f'{{"name": "myplugin", "version": "{version}"}}', encoding="utf-8"
    )
    (plugin / "README.md").write_text(readme, encoding="utf-8")
    bin_dir = plugin / "bin"
    bin_dir.mkdir()
    for name in bin_scripts:
        (bin_dir / name).write_text("# stub\n", encoding="utf-8")
    return plugin


def test_clean_plugin_passes(tmp_path: Path) -> None:
    """A README whose facts match disk produces no findings."""
    plugin = _make_plugin(
        tmp_path,
        version="1.2.3",
        readme="Current version: `1.2.3`.\n\nShared bin/ scripts: `run.py`.\n",
        bin_scripts=("run.py",),
    )
    assert crd.check_plugin(plugin) == []


def test_version_marker_drift_flagged(tmp_path: Path) -> None:
    """A stale 'Current version' marker is reported."""
    plugin = _make_plugin(
        tmp_path,
        version="2.0.0",
        readme="Current version: `1.0.0`.\n",
    )
    findings = crd.check_plugin(plugin)
    assert len(findings) == 1
    assert findings[0].kind is crd.FindingKind.VERSION
    assert "1.0.0" in findings[0].message and "2.0.0" in findings[0].message


def test_stale_bin_reference_flagged(tmp_path: Path) -> None:
    """A bin/ script named in a bin/ line but absent on disk is reported."""
    plugin = _make_plugin(
        tmp_path,
        version="1.0.0",
        readme="Current version: `1.0.0`.\n\nShared bin/ scripts: `gone.sh`.\n",
        bin_scripts=("kept.py",),
    )
    findings = crd.check_plugin(plugin)
    assert len(findings) == 1
    assert findings[0].kind is crd.FindingKind.BIN_REFS
    assert "gone.sh" in findings[0].message


def test_script_reference_off_bin_line_ignored(tmp_path: Path) -> None:
    """A ``.py`` token on a line that does not mention bin/ is not a bin reference."""
    plugin = _make_plugin(
        tmp_path,
        version="1.0.0",
        readme="Current version: `1.0.0`.\n\nRun `train.py` to start training.\n",
        bin_scripts=(),
    )
    assert crd.check_plugin(plugin) == []


def test_reference_existing_elsewhere_ignored(tmp_path: Path) -> None:
    """A referenced script that exists outside bin/ (e.g. tests/) is not drift."""
    plugin = _make_plugin(
        tmp_path,
        version="1.0.0",
        readme="Current version: `1.0.0`.\n\nSee bin/ and `test_helper.py`.\n",
        bin_scripts=(),
    )
    (plugin / "tests").mkdir()
    (plugin / "tests" / "test_helper.py").write_text("# stub\n", encoding="utf-8")
    assert crd.check_plugin(plugin) == []


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 1 with findings printed, 0 when clean."""
    plugin = _make_plugin(
        tmp_path,
        version="9.9.9",
        readme="Current version: `0.0.1`.\n",
    )
    assert crd.main([str(plugin)]) == 1
    assert "README-DRIFT" in capsys.readouterr().out

    plugin2 = _make_plugin(
        tmp_path / "clean",
        version="1.0.0",
        readme="Current version: `1.0.0`.\n",
    )
    assert crd.main([str(plugin2)]) == 0


def _both_modes_plugin(root: Path) -> Path:
    """Create a plugin whose README drifts on both the version marker and a bin/ ref."""
    return _make_plugin(
        root,
        version="2.0.0",
        readme="Current version: `1.0.0`.\n\nShared bin/ scripts: `gone.sh`.\n",
        bin_scripts=("kept.py",),
    )


@pytest.mark.parametrize("spec", ["version,bin-refs", "bin-refs,version", " Version , BIN-REFS "])
def test_parse_kinds_accepts_every_selectable_spelling(spec: str) -> None:
    """Selector parsing is order-, case-, and whitespace-insensitive."""
    assert crd.parse_kinds(spec) == set(crd.SELECTABLE_KINDS)


def test_parse_kinds_rejects_unknown_token() -> None:
    """An unrecognised subcheck name raises ValueError naming the token."""
    with pytest.raises(ValueError, match="nope"):
        crd.parse_kinds("version,nope")


def test_no_arg_default_runs_both_subchecks(tmp_path: Path) -> None:
    """check_plugin without an explicit kinds set reports both drift classes."""
    findings = crd.check_plugin(_both_modes_plugin(tmp_path))
    assert {f.kind for f in findings} == set(crd.SELECTABLE_KINDS)


@pytest.mark.parametrize(
    ("mode", "expected", "excluded"),
    [
        pytest.param("version", "1.0.0", "gone.sh", id="version"),
        pytest.param("bin-refs", "gone.sh", "plugin.json", id="bin-refs"),
    ],
)
def test_single_subcheck_reports_only_its_own_findings(tmp_path: Path, mode: str, expected: str, excluded: str) -> None:
    """Selecting one subcheck yields that class of finding only."""
    findings = crd.check_plugin(_both_modes_plugin(tmp_path), crd.parse_kinds(mode))
    messages = _messages(findings)
    assert len(messages) == 1
    assert expected in messages[0]
    assert excluded not in messages[0]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [pytest.param("version", "1.0.0", id="version"), pytest.param("bin-refs", "gone.sh", id="bin-refs")],
)
def test_main_single_subcheck_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str, expected: str
) -> None:
    """A subcheck with findings still drives exit code 1 when selected alone."""
    plugin = _both_modes_plugin(tmp_path)
    rc = crd.main([str(plugin), "--check", mode])
    out = capsys.readouterr().out
    assert rc == 1
    assert expected in out


def test_main_subcheck_clean_for_unrelated_drift_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A plugin drifting only on its version marker passes the bin-refs subcheck."""
    plugin = _make_plugin(tmp_path, version="2.0.0", readme="Current version: `1.0.0`.\n")
    rc = crd.main([str(plugin), "--check", "bin-refs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[bin-refs]" in out


def test_main_unknown_subcheck_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unknown --check mode exits 2 with the token named on stderr."""
    plugin = _both_modes_plugin(tmp_path)
    assert crd.main([str(plugin), "--check", "bogus"]) == 2
    assert "bogus" in capsys.readouterr().err
