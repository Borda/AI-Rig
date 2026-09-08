"""Tests for ``build_codemap_batch.py``.

Covers:
    - ``build_batch_request``: central-first ordering, five queries per module,
      ``uncovered`` flag placement, empty-module request.
    - ``main()`` entry point: batch JSON written + modules printed (diff monkeypatched),
      bad-arg exit code.
"""

from __future__ import annotations

import json

import pytest

import build_codemap_batch as bcb


# ---------- Pure request builder ----------


class TestBuildBatchRequest:
    """Assemble the ordered codemap-py query batch array."""

    def test_central_always_first_and_alone_when_no_modules(self):
        """Empty module list yields exactly the central query."""
        assert bcb.build_batch_request([]) == [{"cmd": "central", "args": ["--top", "5"]}]

    def test_five_queries_per_module_in_order(self):
        """Each module contributes the five pre-flight queries after central.

        ``fn-rdeps``/``fn-blast`` are intentionally absent: they need ``module::fn``
        qnames a name-only diff cannot supply (2026-07 usage audit F1).
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
        """Flags precede the module only for ``uncovered``; other queries append them."""
        req = bcb.build_batch_request(["pkg.mod"])
        assert req[index]["args"] == expected_args

    def test_two_modules_grouped_per_module(self):
        """Queries stay grouped per module: all five for the first, then the second."""
        req = bcb.build_batch_request(["a.one", "b.two"])
        assert len(req) == 1 + 10
        first_block = [item for item in req[1:6]]
        assert all("a.one" in item["args"] for item in first_block)
        assert all("b.two" in item["args"] for item in req[6:])


# ---------- main() entry point ----------


class TestMain:
    """Write a query request and report its derived modules."""

    def test_writes_json_and_prints_modules(self, tmp_path, monkeypatch, capsys):
        """Monkeypatched diff yields modules → JSON on disk + stdout list, exit 0."""
        monkeypatch.setattr(bcb, "_git_diff_files", lambda: ["src/pkg/mod.py"])
        out = tmp_path / "batch.json"
        rc = bcb.main([str(out)])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "pkg.mod"
        req = json.loads(out.read_text(encoding="utf-8"))
        assert req[0] == {"cmd": "central", "args": ["--top", "5"]}
        assert len(req) == 6

    def test_no_changed_files_still_writes_central_request(self, tmp_path, monkeypatch, capsys):
        """Empty diff → central-only request, empty stdout line, exit 0."""
        monkeypatch.setattr(bcb, "_git_diff_files", lambda: [])
        out = tmp_path / "batch.json"
        assert bcb.main([str(out)]) == 0
        assert capsys.readouterr().out.strip() == ""
        assert json.loads(out.read_text(encoding="utf-8")) == [{"cmd": "central", "args": ["--top", "5"]}]

    def test_missing_argument_exits_1(self, capsys):
        """No output path → usage on stderr, exit 1, nothing written."""
        assert bcb.main([]) == 1
        assert "usage" in capsys.readouterr().err

    def test_help_exits_zero(self, capsys):
        """Print usage to stdout and exit 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc:
            bcb.main(["--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_modules_override_skips_diff_derivation(self, tmp_path, monkeypatch, capsys):
        """Honor the caller's module list without inspecting Git state.

        The refactor skill already holds AFFECTED_MODULES; deriving from the diff here would silently swap its explicit
        scope for unrelated files.
        """
        monkeypatch.setattr(bcb, "_git_diff_files", lambda: pytest.fail("diff derivation must be skipped"))
        out = tmp_path / "batch.json"
        assert bcb.main([str(out), "--modules", "pkg.a pkg.b"]) == 0
        assert capsys.readouterr().out.strip() == "pkg.a pkg.b"
        req = json.loads(out.read_text(encoding="utf-8"))
        assert req[0] == {"cmd": "central", "args": ["--top", "5"]}
        assert len(req) == 11

    def test_queries_filter_omits_central(self, tmp_path, monkeypatch):
        """Emit only per-module rdeps items, no central baseline.

        A caller naming exact query families asked for exactly those — an implicit central item would re-bill baseline
        context it never reads.
        """
        monkeypatch.setattr(bcb, "_git_diff_files", lambda: [])
        out = tmp_path / "batch.json"
        assert bcb.main([str(out), "--modules", "pkg.a pkg.b", "--queries", "rdeps"]) == 0
        req = json.loads(out.read_text(encoding="utf-8"))
        assert req == [{"cmd": "rdeps", "args": ["pkg.a"]}, {"cmd": "rdeps", "args": ["pkg.b"]}]

    def test_unknown_query_family_exits_1(self, tmp_path, capsys):
        """An unknown ``--queries`` name exits 1 with the offending name on stderr.

        A typo'd family silently dropped would produce an empty batch that reads as "no callers found" — fail loudly
        instead.
        """
        out = tmp_path / "batch.json"
        assert bcb.main([str(out), "--modules", "pkg.a", "--queries", "bogus"]) == 1
        assert "bogus" in capsys.readouterr().err
        assert not out.exists()
