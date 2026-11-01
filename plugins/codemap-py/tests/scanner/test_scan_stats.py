"""Tests for ``bin/scan-stats.py`` — codemap index summary printer.

Covers:
* ``_resolve_root`` precedence: --root arg → git toplevel → cwd
* ``_resolve_root`` directory-traversal guard (--root outside cwd → exit 2)
* ``_load_index`` happy path and missing/oversized/vanished-file exits
* ``main()`` output for typical index shapes: no modules, ok+degraded, symbol totals, calls line
* ``main()`` top-5 ranking by rdep_count
* the argparse layer and the real ``SCAN_ARGS`` call site
"""

from __future__ import annotations

import doctest
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "scan-stats.py"
_spec = importlib.util.spec_from_file_location("codemap_scan_stats", _SCRIPT)
assert _spec, "scan-stats.py not found in bin/"
assert _spec.loader, "scan-stats.py not found in bin/"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_resolve_root = _mod._resolve_root
_load_index = _mod._load_index
main = _mod.main


def _write_index(tmp_path: Path, modules: list[dict[str, Any]]) -> None:
    """Write a minimal scan index JSON at the canonical path under *tmp_path*."""
    idx_dir = tmp_path / ".cache" / "codemap"
    idx_dir.mkdir(parents=True, exist_ok=True)
    (idx_dir / f"{tmp_path.name}.json").write_text(json.dumps({"modules": modules}))


@pytest.fixture(name="existing_index")
def _existing_index(tmp_path: Path) -> Path:
    """Project root holding an empty but loadable index."""
    _write_index(tmp_path, [])
    return tmp_path


@pytest.fixture(name="stats_project")
def _stats_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[list[dict[str, Any]]], None]:
    """Return a factory that writes an index and points ``SCAN_ARGS`` at its project root."""

    def _make(modules: list[dict[str, Any]]) -> None:
        _write_index(tmp_path, modules)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", f"--root {tmp_path.as_posix()}")

    return _make


class TestResolveRoot:
    """_resolve_root: root precedence and directory-traversal guard."""

    def test_returns_absolute_path_from_scan_args(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``--root`` inside cwd resolves to its absolute form and is returned.

        The explicit option wins over git and cwd, so no git lookup is needed.
        """
        subdir = tmp_path / "proj"
        subdir.mkdir()
        monkeypatch.chdir(tmp_path)

        result = _resolve_root(f"--root {subdir.as_posix()}", timeout=5)

        assert result == str(subdir)

    def test_traversal_blocked_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--root`` outside cwd causes sys.exit(2) — directory traversal guard.

        SCAN_ARGS is user-influenced text; a root escaping the working directory must be refused before any file is
        read.
        """
        monkeypatch.chdir(tmp_path)
        outside = tmp_path.parent / "escaped"

        with pytest.raises(SystemExit) as exc_info:
            _resolve_root(f"--root {outside.as_posix()}", timeout=5)

        assert exc_info.value.code == 2

    @mock.patch("subprocess.check_output", autospec=True, return_value=b"/some/git/root")
    def test_falls_back_to_git_toplevel(self, mock_git: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Without ``--root`` in SCAN_ARGS, the root is ``git rev-parse --show-toplevel``.

        Git is stubbed: the contract is the decision to prefer git's toplevel over cwd, not git itself.
        """
        monkeypatch.chdir(tmp_path)

        result = _resolve_root("", timeout=5)

        assert result == "/some/git/root"
        mock_git.assert_called_once()

    @mock.patch("subprocess.check_output", autospec=True, side_effect=Exception("no git"))
    def test_falls_back_to_cwd_when_git_unavailable(
        self, mock_git: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When git rev-parse raises, cwd is returned as last resort.

        Outside a repository (or without git installed) the summary must still work against the current directory.
        """
        monkeypatch.chdir(tmp_path)

        result = _resolve_root("", timeout=5)

        assert result == os.path.abspath(str(tmp_path))
        mock_git.assert_called_once()


class TestLoadIndex:
    """_load_index: JSON loading and missing-file error path."""

    def test_returns_parsed_json(self, tmp_path: Path) -> None:
        """Valid index JSON is loaded and returned as dict.

        The loader reads ``.cache/codemap/<project>.json`` under the root and hands back the decoded payload unchanged.
        """
        modules = [{"name": "mod", "status": "ok", "symbols": []}]
        _write_index(tmp_path, modules)

        assert _load_index(str(tmp_path)) == {"modules": modules}

    def test_exits_1_when_file_missing(self, tmp_path: Path) -> None:
        """Absent index file causes sys.exit(1) (user hint: run /codemap-py:scan-codebase).

        A project that was never scanned has no index; the summary must stop with a hint instead of a traceback.
        """
        with pytest.raises(SystemExit) as exc_info:
            _load_index(str(tmp_path))

        assert exc_info.value.code == 1

    @mock.patch("os.path.getsize", autospec=True, return_value=_mod.MAX_INDEX_SIZE + 1)
    def test_exits_1_when_index_exceeds_size_limit(self, mock_size: mock.Mock, existing_index: Path) -> None:
        """Index file larger than MAX_INDEX_SIZE causes sys.exit(1) — DoS guard.

        The size is stubbed to one byte over the cap instead of writing a huge file; the guard must refuse to parse it.
        """
        with pytest.raises(SystemExit) as exc_info:
            _load_index(str(existing_index))

        assert exc_info.value.code == 1
        mock_size.assert_called()

    @mock.patch("builtins.open", autospec=True, side_effect=FileNotFoundError("gone"))
    def test_exits_1_on_race_condition_file_disappears(self, mock_open: mock.Mock, existing_index: Path) -> None:
        """Handle an index disappearing between its size check and open operation.

        A concurrent rescan can replace the index after the size check passed; the open failing must end in the same
        clean exit as a missing file.
        """
        with pytest.raises(SystemExit) as exc_info:
            _load_index(str(existing_index))

        assert exc_info.value.code == 1
        mock_open.assert_called()


class TestMain:
    """Report command-line statistics for every supported index shape."""

    def test_no_modules_prints_message_and_exits_0(self, stats_project, capsys: pytest.CaptureFixture[str]) -> None:
        """Empty module list emits 'No modules indexed.' and exits 0.

        A scan of a project without Python files is not an error; the user gets a plain message.
        """
        stats_project([])

        with pytest.raises(SystemExit) as exc_info:
            main([])

        assert exc_info.value.code == 0
        assert "No modules indexed." in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("modules", "present", "absent"),
        [
            # Only status ok modules count as indexed; degraded ones are reported separately.
            pytest.param(
                [
                    {"name": "a", "status": "ok", "rdep_count": 1, "symbols": []},
                    {"name": "b", "status": "ok", "rdep_count": 2, "symbols": []},
                    {"name": "c", "status": "degraded", "rdep_count": 0, "symbols": []},
                ],
                ("2 indexed", "1 degraded"),
                (),
                id="indexed-and-degraded-counts",
            ),
            # Total symbol count is printed as sum of all ok-module symbol lists.
            pytest.param(
                [
                    {"name": "x", "status": "ok", "rdep_count": 0, "symbols": [{}, {}]},
                    {"name": "y", "status": "ok", "rdep_count": 0, "symbols": [{}]},
                ],
                ("Symbols: 3",),
                (),
                id="symbol-total-summed-across-modules",
            ),
            # Degraded modules do not contribute to the symbol total.
            pytest.param(
                [
                    {"name": "ok_mod", "status": "ok", "rdep_count": 0, "symbols": [{}]},
                    {"name": "bad_mod", "status": "degraded", "rdep_count": 0, "symbols": [{}, {}]},
                ],
                ("Symbols: 1",),
                (),
                id="degraded-modules-excluded-from-symbol-count",
            ),
            # 'Calls:' line is printed when at least one resolved call edge exists.
            pytest.param(
                [{"name": "m", "status": "ok", "rdep_count": 0, "symbols": [{"calls": [{"target": "n"}]}]}],
                ("Calls:",),
                (),
                id="calls-line-present-for-v3-index",
            ),
            # 'Calls:' line is omitted when all call lists are empty.
            pytest.param(
                [{"name": "m", "status": "ok", "rdep_count": 0, "symbols": [{"calls": []}]}],
                (),
                ("Calls:",),
                id="calls-line-absent-when-no-call-edges",
            ),
        ],
    )
    def test_summary_lines(
        self,
        stats_project,
        capsys: pytest.CaptureFixture[str],
        modules: list[dict[str, Any]],
        present: tuple[str, ...],
        absent: tuple[str, ...],
    ) -> None:
        """The summary reports indexed/degraded counts, an ok-only symbol total and a Calls line only with call edges.

        Each case writes a small index and prints the summary; the named lines must appear (or stay absent) so a user
        reading the output can trust the totals and does not see a Calls line for an index without resolved edges.
        """
        stats_project(modules)

        main([])

        out = capsys.readouterr().out
        assert all(line in out for line in present)
        assert not any(line in out for line in absent)

    def test_top_modules_ranked_by_rdep_count(self, stats_project, capsys: pytest.CaptureFixture[str]) -> None:
        """Module with highest rdep_count appears first in 'Most central' list.

        Two modules are listed in ascending order in the index; the printed ranking must reorder them by dependents.
        """
        stats_project(
            [
                {"name": "low", "status": "ok", "rdep_count": 1, "symbols": []},
                {"name": "high", "status": "ok", "rdep_count": 99, "symbols": []},
            ]
        )

        main([])

        out = capsys.readouterr().out
        assert out.index("high") < out.index("low"), "highest rdep_count must appear first"


class TestArgparse:
    """CLI argument handling: ``--help`` and the real SCAN_ARGS call-site shape."""

    def test_help_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage and exit 0 (argparse default).

        ``--help`` must short-circuit before any index lookup and name the script in its usage text.
        """
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])

        assert exc_info.value.code == 0
        assert "scan-stats.py" in capsys.readouterr().out

    @mock.patch("subprocess.check_output", autospec=True)
    def test_golden_call_site_scan_args_env(
        self,
        mock_git: mock.Mock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Real call site (empty ``SCAN_ARGS`` env, no argv) still prints the summary.

        The skill invokes the script with no arguments and an empty ``SCAN_ARGS``; the root then comes from git's
        toplevel (stubbed to the temp project) and the summary is printed from that project's index.
        """
        _write_index(tmp_path, [{"name": "m", "status": "ok", "rdep_count": 1, "symbols": [{}]}])
        mock_git.return_value = str(tmp_path).encode()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")

        main([])

        assert "Modules: 1 indexed" in capsys.readouterr().out


def test_module_doctests_pass() -> None:
    """Doctest examples embedded in scan-stats.py must not regress.

    The examples double as user documentation of the helper functions, so a drifting output fails here.
    """
    results = doctest.testmod(_mod, verbose=False)

    assert results.failed == 0, f"{results.failed} doctest(s) failed"
