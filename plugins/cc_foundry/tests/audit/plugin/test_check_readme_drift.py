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

_MOD_PATH = Path(__file__).resolve().parents[3] / "bin" / "check_readme_drift.py"
_spec = importlib.util.spec_from_file_location("check_readme_drift", _MOD_PATH)
assert _spec
assert _spec.loader
crd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crd)


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


@pytest.fixture
def both_modes_plugin(tmp_path: Path) -> Path:
    """Plugin whose README drifts on both the version marker (2.0.0 vs 1.0.0) and a bin/ ref (gone.sh)."""
    return _make_plugin(
        tmp_path,
        version="2.0.0",
        readme="Current version: `1.0.0`.\n\nShared bin/ scripts: `gone.sh`.\n",
        bin_scripts=("kept.py",),
    )


class TestCheckPlugin:
    """``check_plugin`` reports README facts that disagree with disk."""

    @pytest.mark.parametrize(
        ("readme", "bin_scripts"),
        [
            pytest.param(
                "Current version: `1.2.3`.\n\nShared bin/ scripts: `run.py`.\n", ("run.py",), id="facts-match-disk"
            ),
            # A `.py` token on a line that does not mention bin/ is not a bin reference.
            pytest.param(
                "Current version: `1.2.3`.\n\nRun `train.py` to start training.\n", (), id="script-token-off-bin-line"
            ),
        ],
    )
    def test_clean_plugin_passes(self, tmp_path: Path, readme: str, bin_scripts: tuple[str, ...]) -> None:
        """A README whose facts match disk produces no findings.

        Covers a matching version marker with a bin/ line naming a script that exists, and a ``.py`` token on a line
        that never mentions bin/, which is not a bin reference.
        """
        plugin = _make_plugin(tmp_path, version="1.2.3", readme=readme, bin_scripts=bin_scripts)
        assert crd.check_plugin(plugin) == []

    @pytest.mark.parametrize(
        ("version", "readme", "bin_scripts", "kind", "fragments"),
        [
            pytest.param(
                "2.0.0",
                "Current version: `1.0.0`.\n",
                (),
                crd.FindingKind.VERSION,
                ("1.0.0", "2.0.0"),
                id="stale-version-marker",
            ),
            pytest.param(
                "1.0.0",
                "Current version: `1.0.0`.\n\nShared bin/ scripts: `gone.sh`.\n",
                ("kept.py",),
                crd.FindingKind.BIN_REFS,
                ("gone.sh",),
                id="stale-bin-reference",
            ),
        ],
    )
    def test_drift_is_reported_once_with_both_facts(
        self,
        tmp_path: Path,
        version: str,
        readme: str,
        bin_scripts: tuple[str, ...],
        kind: crd.FindingKind,
        fragments: tuple[str, ...],
    ) -> None:
        """A stale version marker, or a bin/ script named in the README but absent on disk, yields one finding.

        The version finding names the README and manifest versions; the bin finding names the missing script while a
        different script (``kept.py``) is present in ``bin/``.
        """
        findings = crd.check_plugin(_make_plugin(tmp_path, version=version, readme=readme, bin_scripts=bin_scripts))
        assert len(findings) == 1
        assert findings[0].kind is kind
        assert all(fragment in findings[0].message for fragment in fragments)

    def test_reference_existing_elsewhere_ignored(self, tmp_path: Path) -> None:
        """A referenced script that exists outside bin/ (e.g. tests/) is not drift.

        The README mentions ``test_helper.py`` beside a bin/ mention; the file lives under ``tests/`` rather than
        ``bin/``, and a reference that resolves anywhere in the plugin is not stale.
        """
        plugin = _make_plugin(
            tmp_path, version="1.0.0", readme="Current version: `1.0.0`.\n\nSee bin/ and `test_helper.py`.\n"
        )
        (plugin / "tests").mkdir()
        (plugin / "tests" / "test_helper.py").write_text("# stub\n", encoding="utf-8")
        assert crd.check_plugin(plugin) == []

    def test_no_arg_default_runs_both_subchecks(self, both_modes_plugin: Path) -> None:
        """``check_plugin`` without an explicit kinds set reports both drift classes."""
        findings = crd.check_plugin(both_modes_plugin)
        assert {f.kind for f in findings} == set(crd.SELECTABLE_KINDS)

    @pytest.mark.parametrize(
        ("mode", "expected", "excluded"),
        [
            pytest.param("version", "1.0.0", "gone.sh", id="version"),
            pytest.param("bin-refs", "gone.sh", "plugin.json", id="bin-refs"),
        ],
    )
    def test_single_subcheck_reports_only_its_own_findings(
        self, both_modes_plugin: Path, mode: str, expected: str, excluded: str
    ) -> None:
        """Selecting one subcheck yields that class of finding only.

        The plugin drifts on both facts, so a selector that leaked the other class would add a second finding.
        """
        messages = [f.message for f in crd.check_plugin(both_modes_plugin, crd.parse_kinds(mode))]
        assert len(messages) == 1
        assert expected in messages[0]
        assert excluded not in messages[0]


class TestParseKinds:
    """``parse_kinds`` turns a ``--check`` selector into a set of subchecks."""

    @pytest.mark.parametrize("spec", ["version,bin-refs", "bin-refs,version", " Version , BIN-REFS "])
    def test_accepts_every_selectable_spelling(self, spec: str) -> None:
        """Selector parsing is order-, case-, and whitespace-insensitive.

        Every spelling of the two selectable names resolves to the full selectable set.
        """
        assert crd.parse_kinds(spec) == set(crd.SELECTABLE_KINDS)

    def test_rejects_unknown_token(self) -> None:
        """An unrecognised subcheck name raises ValueError naming the token.

        A typo'd selector must fail loudly instead of silently running a smaller check.
        """
        with pytest.raises(ValueError, match="nope"):
            crd.parse_kinds("version,nope")


class TestMain:
    """The CLI exit codes mirror the findings of the selected subchecks."""

    @pytest.mark.parametrize(
        ("version", "readme", "expected"),
        [
            pytest.param("9.9.9", "Current version: `0.0.1`.\n", 1, id="drift"),
            pytest.param("1.0.0", "Current version: `1.0.0`.\n", 0, id="clean"),
        ],
    )
    def test_exit_code_mirrors_findings(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str, readme: str, expected: int
    ) -> None:
        """Exit 1 with a README-DRIFT line when the README drifts, 0 when it is clean.

        pre-commit keys on the exit code; the printed finding is what the committer acts on.
        """
        plugin = _make_plugin(tmp_path, version=version, readme=readme)
        rc = crd.main([str(plugin)])
        assert rc == expected
        assert ("README-DRIFT" in capsys.readouterr().out) == bool(expected)

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [pytest.param("version", "1.0.0", id="version"), pytest.param("bin-refs", "gone.sh", id="bin-refs")],
    )
    def test_single_subcheck_with_findings_exits_one(
        self, both_modes_plugin: Path, capsys: pytest.CaptureFixture[str], mode: str, expected: str
    ) -> None:
        """A subcheck with findings still drives exit code 1 when selected alone."""
        rc = crd.main([str(both_modes_plugin), "--check", mode])
        assert rc == 1
        assert expected in capsys.readouterr().out

    def test_subcheck_clean_for_unrelated_drift_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A plugin drifting only on its version marker passes the bin-refs subcheck.

        The run still announces the ``[bin-refs]`` subcheck it performed, so a silent skip is distinguishable.
        """
        plugin = _make_plugin(tmp_path, version="2.0.0", readme="Current version: `1.0.0`.\n")
        rc = crd.main([str(plugin), "--check", "bin-refs"])
        assert rc == 0
        assert "[bin-refs]" in capsys.readouterr().out

    def test_unknown_subcheck_exits_two(self, both_modes_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unknown --check mode exits 2 with the token named on stderr."""
        assert crd.main([str(both_modes_plugin), "--check", "bogus"]) == 2
        assert "bogus" in capsys.readouterr().err
