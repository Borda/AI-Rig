"""Tests for ``bin/propagate_shared.py``.

The tool keeps byte-identical cross-plugin shared files in sync from a single canonical source. These tests use a
synthetic MANIFEST over ``tmp_path`` to verify drift detection (``check``) and syncing (``apply``) without touching the
real repository manifest.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent / "bin" / "propagate_shared.py"
_spec = importlib.util.spec_from_file_location("propagate_shared", _MOD_PATH)
assert _spec and _spec.loader
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


def _manifest() -> list[dict[str, object]]:
    """Return a one-entry manifest matching the tmp tree built below.

    Examples:
        >>> _manifest()[0]["copies"]
        ['b/hook.js', 'c/hook.js']
    """
    return [{"canonical": "a/hook.js", "copies": ["b/hook.js", "c/hook.js"]}]


def _tree(root: Path, canonical: str, b: str, c: str) -> None:
    """Create a/hook.js, b/hook.js, c/hook.js with the given contents.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _tree(Path(directory), "A", "B", "C")
        ...     (Path(directory) / "b" / "hook.js").read_text()
        'B'
    """
    for sub, content in (("a", canonical), ("b", b), ("c", c)):
        (root / sub).mkdir(parents=True, exist_ok=True)
        (root / sub / "hook.js").write_text(content, encoding="utf-8")


def test_check_clean_when_all_match(tmp_path: Path) -> None:
    """No findings when every copy equals the canonical."""
    _tree(tmp_path, "X\n", "X\n", "X\n")
    assert ps.check(tmp_path, _manifest()) == []


def test_check_reports_each_drifted_copy(tmp_path: Path) -> None:
    """Each copy differing from canonical is reported once."""
    _tree(tmp_path, "X\n", "OLD\n", "X\n")
    findings = ps.check(tmp_path, _manifest())
    assert len(findings) == 1
    assert "b/hook.js" in findings[0]


def test_check_reports_missing_canonical(tmp_path: Path) -> None:
    """A missing canonical file is reported, not silently skipped."""
    manifest = [{"canonical": "gone/hook.js", "copies": ["b/hook.js"]}]
    findings = ps.check(tmp_path, manifest)
    assert len(findings) == 1
    assert "canonical missing" in findings[0]


def test_apply_syncs_drifted_copies(tmp_path: Path) -> None:
    """Overwrite drifted copies and return their paths before confirming a clean state."""
    _tree(tmp_path, "NEW\n", "OLD\n", "NEW\n")
    updated = ps.apply(tmp_path, _manifest())
    assert updated == ["b/hook.js"]
    assert (tmp_path / "b" / "hook.js").read_text() == "NEW\n"
    assert ps.check(tmp_path, _manifest()) == []


def test_main_check_exit_codes(tmp_path: Path) -> None:
    """Main --check exits 1 on drift, 0 when clean (via monkeypatched MANIFEST)."""
    _tree(tmp_path, "X\n", "OLD\n", "X\n")
    original = ps.MANIFEST
    ps.MANIFEST = _manifest()
    try:
        assert ps.main(["--root", str(tmp_path)]) == 1
        ps.main(["--apply", "--root", str(tmp_path)])
        assert ps.main(["--root", str(tmp_path)]) == 0
    finally:
        ps.MANIFEST = original


def test_real_manifest_prefixes_migrated_copies() -> None:
    """Migrated _shared copies keep the ``foundry--`` source-plugin prefix.

    quality-stack.md and cross-validation-protocol.md copies were renamed so a consumer plugin's own _shared file can
    never collide with (or be silently overwritten by) a propagated copy; reverting a copy to the bare canonical
    basename would re-open that collision and orphan consumer references.
    """
    for entry in ps.MANIFEST:
        canonical = str(entry["canonical"])
        if canonical.endswith(("_shared/quality-stack.md", "_shared/cross-validation-protocol.md")):
            for copy in entry["copies"]:
                assert Path(str(copy)).name.startswith("foundry--"), copy


@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
def test_noop_apply_reports_success_on_legacy_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    """No-op propagation must report success without requiring Unicode stdout."""
    _tree(tmp_path, "X\n", "X\n", "X\n")
    monkeypatch.setattr(ps, "MANIFEST", _manifest())
    with io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n") as output:
        monkeypatch.setattr(sys, "stdout", output)
        assert ps.main(["--apply", "--root", str(tmp_path)]) == 0
        output.flush()
        assert output.buffer.getvalue() == b"OK: all shared copies already in sync\n"
