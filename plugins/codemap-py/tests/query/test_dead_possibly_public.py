"""Dead-code findings that sit where outside code can import them carry ``possibly_public``.

The index sees only the scanned project, so a function in a top-level module or a package ``__init__.py`` with no in-
project caller may still be a public API used downstream. ``dead-symbols`` and ``dead-modules`` keep listing such
findings but mark them with an additive ``possibly_public: true`` and a single fixed ``possibly_public_check`` text.
Each trigger that can fire has its own fixture, plus a negative control that must stay unmarked. Every case scans a
small project with the real ``scan-index`` and queries it with the real ``scan-query``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from _scan_helpers import index_path_for, run_query, write_tree

_BODY = "    total = 0\n    for step in range(3):\n        total += step\n        total *= 2\n    total -= 1\n    return total\n"
_SOURCES = {
    "lib.py": f"def top_level_api():\n{_BODY}",
    "pkg/__init__.py": f"def package_api():\n{_BODY}",
    "pkg/sub/__init__.py": f"def nested_init_api():\n{_BODY}",
    "pkg/deep/plain.py": f"def plain_helper():\n{_BODY}",
    "pkg/deep/exported.py": f"__all__ = ['listed_api']\n\n\ndef listed_api():\n{_BODY}\n\ndef unlisted_helper():\n{_BODY}",
    "pkg/deep/__init__.py": "",
}


def _scan(scan_index: Path, root: Path) -> None:
    """Run scan-index over *root* and assert it succeeds."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)], capture_output=True, text=True, cwd=str(root)
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def project(tmp_path: Path, scan_index: Path) -> Path:
    """Scan the fixture project once and return its root."""
    root = tmp_path / "demo"
    write_tree(root, _SOURCES)
    _scan(scan_index, root)
    return root


class TestDeadSymbolsPossiblyPublic:
    """Only symbols of a top-level module or a package ``__init__.py`` without ``__all__`` are marked."""

    @pytest.mark.parametrize(
        ("module", "symbol", "marked"),
        [
            pytest.param("lib", "top_level_api", True, id="top-level-module"),
            pytest.param("pkg", "package_api", True, id="top-level-package-init"),
            pytest.param("pkg.sub", "nested_init_api", True, id="nested-package-init"),
            pytest.param("pkg.deep.plain", "plain_helper", False, id="deep-plain-module-control"),
            pytest.param("pkg.deep.exported", "unlisted_helper", False, id="all-declared-module-control"),
        ],
    )
    def test_marker_follows_trigger(
        self, project: Path, scan_query: Path, module: str, symbol: str, marked: bool
    ) -> None:
        """Each reachable trigger marks its finding and each control stays unmarked, while all stay listed."""
        payload = run_query(scan_query, project, index_path_for(project), "dead-symbols")
        found = {(f["module"], f["qualified_name"]): f for f in payload["dead"]}
        assert (module, symbol) in found
        assert found[(module, symbol)].get("possibly_public", False) is marked

    def test_check_text_present_once_when_any_marked(self, project: Path, scan_query: Path) -> None:
        """The fixed check text appears once at payload level, never per finding."""
        payload = run_query(scan_query, project, index_path_for(project), "dead-symbols")
        assert isinstance(payload["possibly_public_check"], str)
        assert all("possibly_public_check" not in f for f in payload["dead"])

    def test_listed_name_in_all_is_not_a_finding(self, project: Path, scan_query: Path) -> None:
        """A name in ``__all__`` stays skipped, as before the hint existed."""
        payload = run_query(scan_query, project, index_path_for(project), "dead-symbols")
        assert ("pkg.deep.exported", "listed_api") not in {(f["module"], f["qualified_name"]) for f in payload["dead"]}


class TestDeadModulesPossiblyPublic:
    """A top-level module, a package ``__init__.py`` and a module declaring ``__all__`` are marked."""

    @pytest.mark.parametrize(
        ("module", "marked"),
        [
            pytest.param("lib", True, id="top-level-module"),
            pytest.param("pkg.sub", True, id="package-init"),
            pytest.param("pkg.deep.exported", True, id="all-declared"),
            pytest.param("pkg.deep.plain", False, id="deep-plain-module-control"),
        ],
    )
    def test_marker_follows_trigger(self, project: Path, scan_query: Path, module: str, marked: bool) -> None:
        """Each reachable trigger marks its finding and the control stays unmarked, while all stay listed."""
        payload = run_query(scan_query, project, index_path_for(project), "dead-modules")
        found = {f["name"]: f for f in payload["dead_modules"]}
        assert module in found
        assert found[module].get("possibly_public", False) is marked
        assert isinstance(payload["possibly_public_check"], str)

    def test_no_hint_text_when_nothing_marked(self, tmp_path: Path, scan_index: Path, scan_query: Path) -> None:
        """A project whose only finding is a deep plain module carries neither marker nor check text."""
        root = tmp_path / "plain"
        write_tree(
            root,
            {"pkg/__init__.py": "", "pkg/deep/__init__.py": "", "pkg/deep/plain.py": _SOURCES["pkg/deep/plain.py"]},
        )
        _scan(scan_index, root)
        payload = run_query(scan_query, root, index_path_for(root), "dead-symbols")
        assert payload["dead"]
        assert all("possibly_public" not in f for f in payload["dead"])
        assert "possibly_public_check" not in payload
