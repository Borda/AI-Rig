"""Tests for ``bin/cm_json.py`` — codemap JSON answers -> session sentinels for ``rename-refs``."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

import cm_json
import pytest

_BIN = Path(__file__).resolve().parents[2] / "bin"
_PREFIX = "codemap-demo"
_CSID = "sess1"


def _file_symlink_is_available() -> bool:
    """Return whether this host can create and resolve a file symlink."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("target\n", encoding="utf-8")
        try:
            link.symlink_to(target)
        except OSError:
            return False
        return link.is_symlink() and link.read_text(encoding="utf-8") == "target\n"


_skip_file_symlink_unavailable = pytest.mark.skipif(
    not _file_symlink_is_available(), reason="file symlink creation is unavailable on this host"
)


@pytest.fixture(autouse=True)
def _session_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point sentinels at ``tmp_path`` with a fixed session token."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", _CSID)


def _sentinel(tmp_path: Path, name: str) -> str | None:
    """Return a sentinel's text, ``None`` when it was not written."""
    path = tmp_path / f"{_PREFIX}-{name}-{_CSID}"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _plant(tmp_path: Path, name: str, text: str) -> None:
    """Pre-write a stale sentinel so a test can prove it gets overwritten."""
    (tmp_path / f"{_PREFIX}-{name}-{_CSID}").write_text(text, encoding="utf-8")


def _write_json(tmp_path: Path, payload: object, name: str = "in.json") -> str:
    """Write ``payload`` as JSON under ``tmp_path`` and return the path string."""
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _first_match(src: str, *fields: str, default: str | None = None) -> list[str]:
    """Build a ``first-match`` argv for ``src`` with ``KEY=NAME`` field specs."""
    argv = ["first-match", "--input", src, "--output-prefix", _PREFIX]
    for field in fields:
        argv += ["--field", field]
    return argv if default is None else [*argv, "--default", default]


# ---------------------------------------------------------------------------
# first-match
# ---------------------------------------------------------------------------


class TestFirstMatch:
    """``first-match`` copies requested fields of ``matches[0]``; defaults fill the gaps."""

    def test_writes_each_field_to_its_named_sentinel(self, tmp_path: Path) -> None:
        """Two fields of one answer land in two sentinels named ``<prefix>-<name>-<CSID>``."""
        src = _write_json(tmp_path, {"matches": [{"module": "pkg.auth", "qualified_name": "check"}]})

        rc = cm_json.main(_first_match(src, "module=sym-module", "qualified_name=sym-qname"))

        assert rc == 0
        assert (_sentinel(tmp_path, "sym-module"), _sentinel(tmp_path, "sym-qname")) == ("pkg.auth\n", "check\n")

    def test_only_the_first_match_is_used(self, tmp_path: Path) -> None:
        """A later match never leaks into the sentinel — the skill narrows to one match first."""
        src = _write_json(tmp_path, {"matches": [{"type": "class"}, {"type": "function"}]})

        cm_json.main(_first_match(src, "type=sym-type"))

        assert _sentinel(tmp_path, "sym-type") == "class\n"

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param({"matches": [{}]}, id="field-absent"),
            pytest.param({"matches": [{"path": None}]}, id="field-null"),
            pytest.param({"matches": [{"path": ""}]}, id="field-empty"),
            pytest.param({"matches": []}, id="no-matches"),
            pytest.param({}, id="no-matches-key"),
            pytest.param({"matches": [None]}, id="non-dict-match"),
            pytest.param([], id="non-dict-payload"),
        ],
    )
    def test_default_replaces_absent_null_or_empty(self, tmp_path: Path, answer: object) -> None:
        """Every shape of "no usable value" yields ``--default``."""
        src = _write_json(tmp_path, answer)

        cm_json.main(_first_match(src, "path=sym-path", default="."))

        assert _sentinel(tmp_path, "sym-path") == ".\n"

    def test_multiline_value_is_collapsed_to_one_line(self, tmp_path: Path) -> None:
        """A sentinel is one line, so an embedded newline becomes a space."""
        src = _write_json(tmp_path, {"matches": [{"module": "a\nb"}]})

        cm_json.main(_first_match(src, "module=sym-module"))

        assert _sentinel(tmp_path, "sym-module") == "a b\n"

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("", id="empty-file"),
            pytest.param("not json", id="not-json"),
            pytest.param("{'single': 'quotes'}", id="python-repr"),
        ],
    )
    def test_unparsable_input_overwrites_stale_sentinel_and_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
    ) -> None:
        """A stale value must not survive a failed read: the sentinel is reset to the default."""
        _plant(tmp_path, "sym-module", "stale.module\n")
        bad = tmp_path / "in.json"
        bad.write_text(content, encoding="utf-8")

        rc = cm_json.main(_first_match(str(bad), "module=sym-module"))

        assert rc == 0
        assert _sentinel(tmp_path, "sym-module") == "\n"
        assert "wrote fail-closed defaults" in capsys.readouterr().err

    def test_missing_input_file_overwrites_stale_sentinel_and_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An absent input file behaves like unparsable input."""
        _plant(tmp_path, "sym-module", "stale.module\n")

        rc = cm_json.main(_first_match(str(tmp_path / "missing.json"), "module=sym-module"))

        assert rc == 0
        assert _sentinel(tmp_path, "sym-module") == "\n"
        assert "cannot read JSON" in capsys.readouterr().err

    def test_reads_stdin_when_input_is_dash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--input -`` reads the answer from stdin, so a caller can pipe a captured variable."""
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"matches": [{"module": "from.stdin"}]}'))

        cm_json.main(_first_match("-", "module=sym-module"))

        assert _sentinel(tmp_path, "sym-module") == "from.stdin\n"


# ---------------------------------------------------------------------------
# rdeps-summary
# ---------------------------------------------------------------------------


def _summary(tmp_path: Path, kind: str, answer: object) -> tuple[str | None, str | None, str | None]:
    """Run ``rdeps-summary`` and return the (count, EXHAUSTIVE, hint) sentinel texts."""
    src = _write_json(tmp_path, answer)
    assert cm_json.main(["rdeps-summary", "--kind", kind, "--input", src, "--output-prefix", _PREFIX]) == 0
    return (
        _sentinel(tmp_path, "rdep-count"),
        _sentinel(tmp_path, "rename-EXHAUSTIVE"),
        _sentinel(tmp_path, "rename-RDEP_HINT"),
    )


class TestRdepsSummary:
    """``rdeps-summary`` keeps the forward-first, fail-closed completeness read of the old inline code."""

    @pytest.mark.parametrize(
        ("index", "expected"),
        [
            pytest.param({"query_complete": True}, "true", id="query-complete-true"),
            pytest.param({"query_complete": False}, "false", id="query-complete-false"),
            pytest.param({"query_complete": False, "exhaustive": True}, "false", id="false-beats-legacy-true"),
            pytest.param({"query_complete": True, "exhaustive": False}, "true", id="true-beats-legacy-false"),
            pytest.param({"exhaustive": True}, "true", id="legacy-only-true"),
            pytest.param({"exhaustive": False}, "false", id="legacy-only-false"),
            pytest.param({}, "false", id="neither-key"),
            pytest.param({"query_complete": "true"}, "false", id="truthy-string-is-not-true"),
            pytest.param({"query_complete": 1}, "false", id="truthy-int-is-not-true"),
            pytest.param(None, "false", id="index-null"),
            pytest.param("complete", "false", id="index-not-a-dict"),
        ],
    )
    def test_exhaustive_is_true_only_for_literal_true(self, tmp_path: Path, index: object, expected: str) -> None:
        """A false ``query_complete`` must never be overridden by a legacy ``exhaustive: true``.

        The destructive ``--remove-if-no-callers`` gate reads this value; a ``//``-style fallback once armed it on an
        incomplete graph, so anything but a literal ``true`` — including a truthy string — reads ``false``.
        """
        _count, exhaustive, _hint = _summary(tmp_path, "fn", {"count": 1, "index": index})

        assert exhaustive == f"{expected}\n"

    def test_fn_kind_reads_count_and_normalises_hint(self, tmp_path: Path) -> None:
        """``fn`` reads ``count`` and collapses the hint's whitespace to single spaces."""
        answer = {"count": 0, "hint": "  obj.method()\n  unresolved  statically ", "index": {"query_complete": True}}

        count, exhaustive, hint = _summary(tmp_path, "fn", answer)

        assert (count, exhaustive, hint) == ("0\n", "true\n", "obj.method() unresolved statically\n")

    @pytest.mark.parametrize(
        ("raw_count", "expected"),
        [
            pytest.param(4, "4", id="int"),
            pytest.param(None, "0", id="null"),
            pytest.param("many", "0", id="string"),
            pytest.param(True, "0", id="bool-is-not-a-count"),
        ],
    )
    def test_fn_count_is_an_int_or_zero(self, tmp_path: Path, raw_count: object, expected: str) -> None:
        """A non-integer ``count`` reads as zero rather than leaking text into the shell."""
        count, _exhaustive, _hint = _summary(tmp_path, "fn", {"count": raw_count})

        assert count == f"{expected}\n"

    def test_module_kind_counts_imported_by_and_clears_a_stale_hint(self, tmp_path: Path) -> None:
        """``module`` counts ``imported_by`` and overwrites a hint left by an earlier symbol run."""
        _plant(tmp_path, "rename-RDEP_HINT", "hint from an earlier symbol run\n")
        answer = {"imported_by": ["a", "b", "c"], "hint": "must be ignored", "index": {"query_complete": True}}

        count, exhaustive, hint = _summary(tmp_path, "module", answer)

        assert (count, exhaustive, hint) == ("3\n", "true\n", "\n")

    @pytest.mark.parametrize("kind", [pytest.param("fn", id="fn"), pytest.param("module", id="module")])
    def test_unusable_input_is_fail_closed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str) -> None:
        """Garbage input resets all three sentinels to count 0, not exhaustive, no hint — never to stale values."""
        for name in ("rdep-count", "rename-EXHAUSTIVE", "rename-RDEP_HINT"):
            _plant(tmp_path, name, "stale\n")
        bad = tmp_path / "bad.json"
        bad.write_text("<html>", encoding="utf-8")

        rc = cm_json.main(["rdeps-summary", "--kind", kind, "--input", str(bad), "--output-prefix", _PREFIX])

        assert rc == 0
        assert (_sentinel(tmp_path, "rdep-count"), _sentinel(tmp_path, "rename-EXHAUSTIVE")) == ("0\n", "false\n")
        assert _sentinel(tmp_path, "rename-RDEP_HINT") == "\n"
        assert "wrote fail-closed defaults" in capsys.readouterr().err

    def test_reads_stdin_when_input_is_dash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--input -`` lets the skill pipe the captured query answer without a temp file."""
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"count": 2, "index": {"query_complete": true}}'))

        cm_json.main(["rdeps-summary", "--kind", "fn", "--input", "-", "--output-prefix", _PREFIX])

        assert (_sentinel(tmp_path, "rdep-count"), _sentinel(tmp_path, "rename-EXHAUSTIVE")) == ("2\n", "true\n")

    @pytest.mark.parametrize(
        ("index", "expected"),
        [
            pytest.param(
                {"hint": 'grep -rn "f"  to find\nhook callers'}, 'grep -rn "f" to find hook callers', id="kept"
            ),
            pytest.param({"hint": None}, "", id="null"),
            pytest.param({}, "", id="absent"),
        ],
    )
    def test_index_hint_reaches_its_own_sentinel(self, tmp_path: Path, index: object, expected: str) -> None:
        """``index.hint`` (hook-registered callers outside the static AST) is kept for the blast report.

        It is a different field from the zero-caller ``hint`` and was once dropped, so the report lost the advisory.
        """
        _plant(tmp_path, "rename-INDEX_HINT", "stale\n")

        _summary(tmp_path, "fn", {"count": 1, "index": {"query_complete": True, **index}})

        assert _sentinel(tmp_path, "rename-INDEX_HINT") == f"{expected}\n"


# ---------------------------------------------------------------------------
# stale / class-range / narrow
# ---------------------------------------------------------------------------


class TestStale:
    """``stale`` turns a ``check_index_smoke.py`` answer into ``true``, ``false`` or ``unknown``."""

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            pytest.param({"ok": True, "stale": True}, "true", id="stale"),
            pytest.param({"ok": True, "stale": False}, "false", id="fresh"),
            pytest.param({"ok": False, "stale": False}, "unknown", id="missing-index-never-fresh"),
            pytest.param({"ok": True, "stale": None}, "unknown", id="stale-null"),
            pytest.param({"ok": True}, "unknown", id="stale-absent"),
        ],
    )
    def test_freshness_value(self, tmp_path: Path, answer: object, expected: str) -> None:
        """Only an ``ok: true`` answer with a boolean ``stale`` decides freshness; anything else is unknown."""
        src = _write_json(tmp_path, answer)

        rc = cm_json.main(["stale", "--input", src, "--output-prefix", _PREFIX, "--name", "rename-STALE"])

        assert rc == 0
        assert _sentinel(tmp_path, "rename-STALE") == f"{expected}\n"

    def test_empty_smoke_output_is_unknown(self, tmp_path: Path) -> None:
        """A smoke check that printed nothing resets an old value to ``unknown`` rather than leaving it."""
        _plant(tmp_path, "rename-STALE", "false\n")
        (tmp_path / "empty.json").write_text("", encoding="utf-8")

        cm_json.main(
            ["stale", "--input", str(tmp_path / "empty.json"), "--output-prefix", _PREFIX, "--name", "rename-STALE"]
        )

        assert _sentinel(tmp_path, "rename-STALE") == "unknown\n"


class TestClassRange:
    """``class-range`` bounds a method rename to its exact enclosing class."""

    @pytest.mark.parametrize(
        ("qualified_name", "expected"),
        [
            pytest.param("Outer", "10 40", id="exact-class"),
            pytest.param("outer", "", id="case-differs"),
            pytest.param("Outer.inner", "", id="method-is-not-a-class"),
        ],
    )
    def test_exact_class_match_only(self, tmp_path: Path, qualified_name: str, expected: str) -> None:
        """The unanchored, case-insensitive find-symbol answer is filtered to type ``class`` plus exact name."""
        answer = {
            "matches": [
                {"type": "method", "qualified_name": "Outer.inner", "start_line": 12, "end_line": 14},
                {"type": "class", "qualified_name": "OuterHelper", "start_line": 50, "end_line": 60},
                {"type": "class", "qualified_name": "Outer", "start_line": 10, "end_line": 40},
            ]
        }
        src = _write_json(tmp_path, answer)

        cm_json.main(
            [
                "class-range",
                "--input",
                src,
                "--qualified-name",
                qualified_name,
                "--output-prefix",
                _PREFIX,
                "--name",
                "r",
            ]
        )

        assert _sentinel(tmp_path, "r") == f"{expected}\n"


class TestNarrow:
    """``narrow`` rewrites the find-symbol answer so every later ``matches[0]`` read is the intended symbol."""

    _ANSWER: ClassVar[dict[str, object]] = {
        "pattern": "old_fn",
        "matches": [{"qualified_name": "old_fn_v2"}, {"qualified_name": "old_fn"}],
        "count": 2,
    }

    def test_exact_name_keeps_only_exact_matches(self, tmp_path: Path) -> None:
        """A unique exact target never asks: the unanchored pattern's longer names drop out."""
        src = _write_json(tmp_path, self._ANSWER)

        rc = cm_json.main(["narrow", "--input", src, "--qualified-name", "old_fn"])

        assert rc == 0
        assert json.loads(Path(src).read_text(encoding="utf-8"))["matches"] == [{"qualified_name": "old_fn"}]

    def test_no_exact_match_leaves_the_answer_for_the_candidate_question(self, tmp_path: Path) -> None:
        """Without an exact match every candidate survives, so the skill can ask which one is meant."""
        src = _write_json(tmp_path, self._ANSWER)

        cm_json.main(["narrow", "--input", src, "--qualified-name", "missing"])

        assert json.loads(Path(src).read_text(encoding="utf-8")) == self._ANSWER

    def test_index_keeps_the_chosen_candidate(self, tmp_path: Path) -> None:
        """The chosen 0-based candidate becomes the only match, with count 1 and the original pattern."""
        src = _write_json(tmp_path, self._ANSWER)

        cm_json.main(["narrow", "--input", src, "--index", "1"])

        assert json.loads(Path(src).read_text(encoding="utf-8")) == {
            "pattern": "old_fn",
            "matches": [{"qualified_name": "old_fn"}],
            "count": 1,
        }

    @pytest.mark.parametrize("index", [pytest.param("2", id="past-the-end"), pytest.param("-1", id="negative")])
    def test_out_of_range_index_exits_4_and_leaves_the_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], index: str
    ) -> None:
        """A wrong index never falls through to an unnarrowed ``matches[0]``: exit 4, answer unchanged."""
        src = _write_json(tmp_path, self._ANSWER)

        rc = cm_json.main(["narrow", "--input", src, "--index", index])

        assert rc == 4
        assert json.loads(Path(src).read_text(encoding="utf-8")) == self._ANSWER
        assert "left unchanged" in capsys.readouterr().err

    @_skip_file_symlink_unavailable
    def test_symlinked_answer_file_is_refused(self, tmp_path: Path) -> None:
        """A planted link at the answer path is never written through."""
        target = tmp_path / "victim.json"
        target.write_text(json.dumps(self._ANSWER), encoding="utf-8")
        link = tmp_path / "answer.json"
        link.symlink_to(target)

        rc = cm_json.main(["narrow", "--input", str(link), "--index", "0"])

        assert rc == 1
        assert json.loads(target.read_text(encoding="utf-8")) == self._ANSWER


# ---------------------------------------------------------------------------
# index-path
# ---------------------------------------------------------------------------


class TestIndexPath:
    """``index-path`` looks a module's file path up in an index file's ``modules[]``."""

    @pytest.mark.parametrize(
        ("module", "expected"),
        [
            pytest.param("pkg.a", "src/pkg/a.py", id="found"),
            pytest.param("pkg.b", "src/pkg/b.py", id="found-later-entry"),
            pytest.param("pkg.zzz", "", id="absent"),
            pytest.param("pkg", "", id="prefix-is-not-a-match"),
        ],
    )
    def test_lookup(self, tmp_path: Path, module: str, expected: str) -> None:
        """Only an exact dotted name matches; a missing module yields an empty path."""
        index = {"modules": [{"name": "pkg.a", "path": "src/pkg/a.py"}, {"name": "pkg.b", "path": "src/pkg/b.py"}]}
        src = _write_json(tmp_path, index, "index.json")

        rc = cm_json.main(
            ["index-path", "--input", src, "--module", module, "--name", "old-index-path", "--output-prefix", _PREFIX]
        )

        assert rc == 0
        assert _sentinel(tmp_path, "old-index-path") == f"{expected}\n"

    def test_first_entry_with_the_name_wins_even_with_empty_path(self, tmp_path: Path) -> None:
        """The old ``next(...)`` stopped at the first name match; a later duplicate is never consulted."""
        index = {"modules": [{"name": "pkg.a", "path": ""}, {"name": "pkg.a", "path": "src/pkg/a.py"}]}
        src = _write_json(tmp_path, index, "index.json")

        cm_json.main(["index-path", "--input", src, "--module", "pkg.a", "--name", "p", "--output-prefix", _PREFIX])

        assert _sentinel(tmp_path, "p") == "\n"

    def test_unreadable_index_clears_the_sentinel(self, tmp_path: Path) -> None:
        """A missing index file resets the sentinel to empty so the caller falls back to dotted-path derivation."""
        _plant(tmp_path, "p", "stale/path.py\n")
        argv = ["index-path", "--input", str(tmp_path / "missing.json"), "--module", "m", "--name", "p"]

        rc = cm_json.main([*argv, "--output-prefix", _PREFIX])

        assert rc == 0
        assert _sentinel(tmp_path, "p") == "\n"


# ---------------------------------------------------------------------------
# argument and filesystem boundaries
# ---------------------------------------------------------------------------


class TestBoundaries:
    """Prefix, field, and sentinel-name validation; symlink refusal; script invocation."""

    @pytest.mark.parametrize(
        "prefix",
        [
            pytest.param("../escape", id="traversal"),
            pytest.param("a/b", id="separator"),
            pytest.param("..", id="dotdot"),
            pytest.param("", id="empty"),
        ],
    )
    def test_unsafe_output_prefix_exits_3_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], prefix: str
    ) -> None:
        """A prefix that could leave ``TMPDIR`` is refused before any file is touched."""
        src = _write_json(tmp_path, {"matches": [{"module": "m"}]})
        before = sorted(p.name for p in tmp_path.iterdir())

        rc = cm_json.main(["first-match", "--input", src, "--output-prefix", prefix, "--field", "module=sym-module"])

        assert rc == 3
        assert sorted(p.name for p in tmp_path.iterdir()) == before
        assert "--output-prefix" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "field",
        [
            pytest.param("module", id="no-equals"),
            pytest.param("=name", id="empty-key"),
            pytest.param("module=", id="empty-name"),
            pytest.param("module=../x", id="name-with-separator"),
        ],
    )
    def test_bad_field_spec_is_a_usage_error(self, field: str) -> None:
        """A malformed ``--field`` is an argparse usage error (exit 2)."""
        with pytest.raises(SystemExit) as raised:
            cm_json.main(_first_match("x", field))

        assert raised.value.code == 2

    def test_bad_sentinel_name_is_a_usage_error(self) -> None:
        """``index-path --name`` must be a bare token, never a path."""
        argv = ["index-path", "--input", "x", "--module", "m", "--name", "a/b", "--output-prefix", _PREFIX]

        with pytest.raises(SystemExit) as raised:
            cm_json.main(argv)

        assert raised.value.code == 2

    def test_missing_subcommand_is_a_usage_error(self) -> None:
        """No subcommand is a usage error."""
        with pytest.raises(SystemExit) as raised:
            cm_json.main([])

        assert raised.value.code == 2

    @_skip_file_symlink_unavailable
    def test_planted_symlink_is_refused_not_followed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A symlink pre-planted at a guessed sentinel name must not redirect the write."""
        victim = tmp_path / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")
        (tmp_path / f"{_PREFIX}-sym-module-{_CSID}").symlink_to(victim)
        src = _write_json(tmp_path, {"matches": [{"module": "PWNED"}]})

        rc = cm_json.main(_first_match(src, "module=sym-module"))

        assert rc == 1
        assert victim.read_text(encoding="utf-8") == "keep\n"
        assert "symlink" in capsys.readouterr().err

    def test_runs_as_a_script_and_session_token_scopes_the_filename(self, tmp_path: Path) -> None:
        """Invoked the way the skill does (``python bin/cm_json.py``), the sibling import resolves."""
        src = _write_json(tmp_path, {"matches": [{"module": "pkg.mod"}]})
        argv = [sys.executable, str(_BIN / "cm_json.py"), *_first_match(src, "module=sym-module")]

        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "TMPDIR": str(tmp_path), "CSID": "scriptsess"},
        )

        assert done.returncode == 0, done.stderr
        assert (tmp_path / f"{_PREFIX}-sym-module-scriptsess").read_text(encoding="utf-8") == "pkg.mod\n"
