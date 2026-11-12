"""Tests for ``build_codemap_batch.py``.

Covers:
    - ``build_batch_request``: central-first ordering, five queries per module,
      ``uncovered`` flag placement, empty-module request.
    - ``main()`` entry point: batch JSON written + modules printed (diff mocked),
      ``--modules`` override, ``--queries`` filter, bad-arg exit codes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import build_codemap_batch as bcb
import pytest

_CENTRAL = {"cmd": "central", "args": ["--top", "5"]}


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required to execute the skill's batch assignment")
def test_refactor_recipe_places_batch_in_run_directory(tmp_path: Path) -> None:
    """The refactor recipe produces an output accepted by the run-artifact fence."""
    run_dir = tmp_path / ".developments"
    run_dir.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "dev-refactor-dev-dir-batch-recipe").write_text(str(run_dir) + "\n", encoding="utf-8", newline="\n")
    lines = (Path(bcb.__file__).parent.parent / "skills/refactor/SKILL.md").read_text(encoding="utf-8").splitlines()
    assignment = next(line for line in lines if line.lstrip().startswith("_BATCH_REQ="))
    reload_line = next(line for line in lines if line.startswith("IFS= read -r DEV_DIR <"))
    result = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            f'{reload_line}\n{assignment}\n"$1" "$2" "$_BATCH_REQ" --modules pkg.mod',
            "recipe",
            sys.executable,
            bcb.__file__,
        ],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(temp), "CSID": "batch-recipe"},
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((run_dir / "codemap-rdeps-batch.json").read_text(encoding="utf-8"))[0] == _CENTRAL


@pytest.mark.parametrize("destination", ["source.py", "scratch/batch.json"])
def test_rejects_output_outside_run_directory(
    destination: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact cannot overwrite project sources or arbitrary files in the session temp directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "batch-fence")
    out = tmp_path / destination
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("preserve", encoding="utf-8")
    assert bcb.main([str(out), "--modules", "pkg.mod"]) == 1
    assert out.read_text(encoding="utf-8") == "preserve"


class TestBuildBatchRequest:
    """Assemble the ordered codemap-py query batch array."""

    def test_central_alone_when_no_modules(self):
        """An empty module list yields exactly the central query.

        With nothing changed there is still a baseline to render, so the request must not be empty.
        """
        assert bcb.build_batch_request([]) == [_CENTRAL]

    def test_five_queries_per_module_in_order(self):
        """Each module contributes the five pre-flight queries after central.

        ``fn-rdeps``/``fn-blast`` are intentionally absent: they need ``module::fn`` qnames a name-only diff cannot
        supply.
        """
        req = bcb.build_batch_request(["pkg.mod"])
        assert len(req) == 1 + 5
        assert [item["cmd"] for item in req[1:]] == ["rdeps", "mock-rdeps", "uncovered", "xrefs", "undocumented"]

    @pytest.mark.parametrize(
        ("index", "expected_args"),
        [
            pytest.param(1, ["pkg.mod"], id="rdeps-module-only"),
            pytest.param(2, ["pkg.mod"], id="mock-rdeps-module-only"),
            pytest.param(3, ["--top", "20", "pkg.mod"], id="uncovered-flags-before-module"),
            pytest.param(4, ["pkg.mod", "--broken"], id="xrefs-flag-after-module"),
        ],
    )
    def test_argument_placement(self, index, expected_args):
        """Flags precede the module only for ``uncovered``; other queries append them.

        The provider parses each query's argv positionally, so a misplaced flag silently changes what is asked.
        """
        assert bcb.build_batch_request(["pkg.mod"])[index]["args"] == expected_args

    def test_two_modules_grouped_per_module(self):
        """Queries stay grouped per module: all five for the first, then all five for the second.

        Consumers slice the response by module, so interleaving the queries would mix answers between modules.
        """
        req = bcb.build_batch_request(["a.one", "b.two"])
        assert len(req) == 1 + 10
        assert all("a.one" in item["args"] for item in req[1:6])
        assert all("b.two" in item["args"] for item in req[6:])


class TestMain:
    """Write a query request and report its derived modules."""

    @pytest.fixture(name="out")
    def _out_fixture(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Return the request path under the standalone run-artifact directory."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "batch-output")
        run_dir = tmp_path / ".developments"
        run_dir.mkdir()
        return run_dir / "batch.json"

    @pytest.mark.parametrize(
        ("diff_files", "printed", "rdeps_args"),
        [
            pytest.param(["src/pkg/mod.py"], "pkg.mod", [["pkg.mod"]], id="single-module"),
            pytest.param(
                ["src/pkg/__init__.py", "src/pkg/mod.py"], "pkg pkg.mod", [["pkg"], ["pkg.mod"]], id="package-and-child"
            ),
            pytest.param([], "", [], id="empty-diff-central-only"),
        ],
    )
    @mock.patch("build_codemap_batch._git_diff_files", autospec=True)
    def test_diff_modules_written_and_printed(self, mock_diff, out, capsys, diff_files, printed, rdeps_args):
        """The diff's modules reach stdout and the request file, with central first and five queries per module.

        A package ``__init__`` changed beside a child needs its own reverse-dependency query, and an empty diff still
        writes the central baseline and exits 0.
        """
        mock_diff.return_value = diff_files

        assert bcb.main([str(out)]) == 0

        request = json.loads(out.read_text(encoding="utf-8"))
        assert capsys.readouterr().out.strip() == printed
        assert request[0] == _CENTRAL
        assert len(request) == 1 + 5 * len(rdeps_args)
        assert [item["args"] for item in request if item["cmd"] == "rdeps"] == rdeps_args

    def test_missing_argument_exits_1(self, capsys):
        """No output path prints usage on stderr and exits 1.

        The skill passes the path positionally, so a missing one is a wiring bug that must surface loudly.
        """
        assert bcb.main([]) == 1
        assert "usage" in capsys.readouterr().err

    def test_help_exits_zero(self, capsys):
        """``--help`` prints usage to stdout and exits 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc:
            bcb.main(["--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

    @pytest.mark.parametrize(
        "modules_arg",
        [
            pytest.param("pkg.a pkg.b", id="plain-names"),
            pytest.param("pkg.a --evil --top pkg.b", id="option-like-names-dropped"),
        ],
    )
    @mock.patch("build_codemap_batch._git_diff_files", autospec=True)
    def test_modules_override_skips_diff_derivation(self, mock_diff, out, capsys, modules_arg):
        """Honor the caller's module list without inspecting Git state, dropping names that look like CLI flags.

        The refactor skill already holds ``AFFECTED_MODULES``; deriving from the diff would silently swap its explicit
        scope for unrelated files. ``--evil``/``--top`` cannot be told apart from options once they reach a batch
        query's argv position, so they never make it into the request.
        """
        assert bcb.main([str(out), "--modules", modules_arg]) == 0

        request = json.loads(out.read_text(encoding="utf-8"))
        mock_diff.assert_not_called()
        assert capsys.readouterr().out.strip() == "pkg.a pkg.b"
        assert request[0] == _CENTRAL
        assert len(request) == 11
        assert [item["args"] for item in request if item["cmd"] == "rdeps"] == [["pkg.a"], ["pkg.b"]]

    @mock.patch("build_codemap_batch._git_diff_files", autospec=True, return_value=[])
    def test_queries_filter_omits_central(self, mock_diff, out):
        """Emit only per-module rdeps items, no central baseline.

        A caller naming exact query families asked for exactly those; an implicit central item would re-bill baseline
        context it never reads.
        """
        assert bcb.main([str(out), "--modules", "pkg.a pkg.b", "--queries", "rdeps"]) == 0

        assert json.loads(out.read_text(encoding="utf-8")) == [
            {"cmd": "rdeps", "args": ["pkg.a"]},
            {"cmd": "rdeps", "args": ["pkg.b"]},
        ]

    def test_unknown_query_family_exits_1(self, out, capsys):
        """An unknown ``--queries`` name exits 1 with the offending name on stderr and writes nothing.

        A typo'd family silently dropped would produce an empty batch that reads as "no callers found"; fail loudly
        instead.
        """
        assert bcb.main([str(out), "--modules", "pkg.a", "--queries", "bogus"]) == 1
        assert "bogus" in capsys.readouterr().err
        assert not out.exists()
