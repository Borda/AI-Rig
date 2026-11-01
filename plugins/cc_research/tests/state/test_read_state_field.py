"""Tests for ``bin/read_state_field.py`` — dotted-path JSON field reader.

Covers:
* Top-level field lookup
* Nested dotted field lookup (canonical: ``config.metric.direction``)
* Default returned when any segment is missing
* CLI exit codes: missing file → exit 2; unreadable JSON → exit 1
* End-to-end subprocess invocation guarding the shebang + ``__main__`` wiring
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _research_text import load_module

_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "read_state_field.py"
_mod = load_module(_SCRIPT, "research_read_state_field")

read_field = _mod.read_field
main = _mod.main


def _write_state(path: Path, payload: object) -> Path:
    """Write ``payload`` as JSON to ``path`` and return ``path`` for chaining.

    Examples:
        >>> path = _write_state(getfixture("tmp_path") / "state.json", {"status": "done"})
        >>> json.loads(path.read_text())["status"]
        'done'
    """
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestReadFieldPure:
    """Unit tests for the pure ``read_field()`` function."""

    @pytest.mark.parametrize(
        ("data", "dotted_path", "expected"),
        [
            pytest.param({"status": "completed"}, "status", "completed", id="top-level-field"),
            pytest.param(
                {"config": {"metric": {"direction": "higher"}}},
                "config.metric.direction",
                "higher",
                id="nested-dotted-field",
            ),
            pytest.param({"n": 42}, "n", "42", id="numeric-terminal-coerced-to-string"),
        ],
    )
    def test_present_field_resolves_to_string(self, data: dict[str, object], dotted_path: str, expected: str) -> None:
        """A present field resolves to its value as a string.

        A single segment resolves to the top-level value; the canonical nested case is ``config.metric.direction``; a
        non-string terminal value is converted via ``str()``.
        """
        assert read_field(data, dotted_path) == expected

    @pytest.mark.parametrize(
        ("data", "dotted_path", "default", "expected"),
        [
            pytest.param(
                {"config": {"metric": {}}}, "config.metric.direction", "higher", "higher", id="segment-missing"
            ),
            pytest.param({"a": "scalar"}, "a.b", "fallback", "fallback", id="terminal-not-dict"),
        ],
    )
    def test_default_returned_when_path_unresolvable(
        self, data: dict[str, object], dotted_path: str, default: str, expected: str
    ) -> None:
        """The default is returned when an intermediate key is absent or a scalar stops the traversal.

        With ``config.metric`` present but empty, the missing ``direction`` segment yields the default. With a scalar at
        ``a``, the traversal cannot descend into ``a.b`` and likewise yields the default rather than raising.
        """
        assert read_field(data, dotted_path, default=default) == expected

    @pytest.mark.parametrize("dotted_path", ["", ".", "a.", ".a", "a..b"])
    def test_invalid_dotted_path_raises(self, dotted_path: str) -> None:
        """Empty dotted paths and empty path segments are rejected explicitly."""
        with pytest.raises(ValueError, match="non-empty"):
            read_field({"a": 1}, dotted_path)


class TestMainCli:
    """In-process ``main()`` tests using ``capsys``."""

    @pytest.mark.parametrize(
        ("state", "extra_args", "expected_out"),
        [
            pytest.param(
                {"config": {"metric": {"direction": "lower"}}},
                ["config.metric.direction"],
                "lower\n",
                id="nested-field",
            ),
            pytest.param({"config": {}}, ["config.metric.direction", "--default", "higher"], "higher\n", id="default"),
            pytest.param({}, ["missing"], "\n", id="empty-line-without-default"),
        ],
    )
    def test_prints_field_or_default(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        state: dict[str, object],
        extra_args: list[str],
        expected_out: str,
    ) -> None:
        """The field value is printed; an absent field prints ``--default``, or an empty line without the flag.

        The canonical nested lookup is ``config.metric.direction``; a skill capturing the output must always get one
        line, even when nothing resolves.
        """
        path = _write_state(tmp_path / "state.json", state)

        rc = main([str(path), *extra_args])

        assert rc == 0
        assert capsys.readouterr().out == expected_out

    @pytest.mark.parametrize(
        ("filename", "text", "message"),
        [
            pytest.param("nope.json", "{}", "not a regular file", id="missing-file"),
            pytest.param("state.json", "{not json", "cannot read", id="malformed-json"),
            pytest.param("state.json", "[1, 2, 3]", "not an object", id="top-level-list"),
        ],
    )
    def test_unusable_state_file_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], filename: str, text: str, message: str
    ) -> None:
        """A missing file, unparsable JSON or a non-object top level exits 1 with the reason on stderr.

        ``text`` is always written to ``state.json``; the missing-file case points at ``nope.json`` instead.
        """
        (tmp_path / "state.json").write_text(text, encoding="utf-8")

        rc = main([str(tmp_path / filename), "any.field"])

        assert rc == 1
        assert message in capsys.readouterr().err

    def test_oversized_state_file_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file over the size cap → exit 1 before it is parsed, so a stray huge file never loads into memory."""
        state = _write_state(tmp_path / "state.json", {"config": {"metric": {"direction": "lower"}}})
        monkeypatch.setattr(_mod, "MAX_STATE_BYTES", 8)

        rc = main([str(state), "config.metric.direction"])

        assert rc == 1
        assert "not a run state file" in capsys.readouterr().err


class TestSubprocessIntegration:
    """End-to-end subprocess check: missing file path → exit 1 contract."""

    def test_missing_file_subprocess(self, tmp_path: Path) -> None:
        """Invoke via subprocess to exercise the shebang + ``__main__`` wiring.

        Spec: ``Exit 1 on file/parse error`` — missing file routes through the
        file-validation branch and returns exit 1.
        """
        missing = tmp_path / "missing.json"
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), str(missing), "a.b"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "not a regular file" in result.stderr
