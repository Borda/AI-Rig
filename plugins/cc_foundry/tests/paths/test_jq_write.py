"""Tests for ``bin/jq_write.py``.

Happy path uses real ``jq`` against ``tmp_path`` JSON files; failure paths mock ``subprocess.run`` to assert exit codes
without depending on jq's exact error messages.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


import jq_write  # noqa: E402

_HAS_JQ = shutil.which("jq") is not None
_requires_jq = pytest.mark.skipif(not _HAS_JQ, reason="jq not installed on this host")


class TestParseJqArgs:
    """_parse_jq_args: triplet validation for --arg pairs."""

    def test_empty(self) -> None:
        """No extras → empty list passes through."""
        assert jq_write._parse_jq_args([]) == []

    def test_one_arg_pair(self) -> None:
        """Single ``--arg k v`` triplet validates."""
        assert jq_write._parse_jq_args(["--arg", "k", "v"]) == ["--arg", "k", "v"]

    def test_two_arg_pairs(self) -> None:
        """Multiple ``--arg`` triplets all validate."""
        extras = ["--arg", "a", "1", "--arg", "b", "2"]
        assert jq_write._parse_jq_args(extras) == extras

    def test_missing_value_returns_none(self) -> None:
        """Return no parsed argument when its value is missing."""
        assert jq_write._parse_jq_args(["--arg", "k"]) is None

    def test_missing_name_and_value_returns_none(self) -> None:
        """Trailing bare ``--arg`` → None."""
        assert jq_write._parse_jq_args(["--arg"]) is None

    def test_non_arg_token_passthrough(self) -> None:
        """Unknown tokens (e.g. ``--argjson``) pass through unchanged."""
        extras = ["--argjson", "n", "42"]
        # --argjson is in _ALLOWED_FLAGS → passthrough without ValueError.
        assert jq_write._parse_jq_args(extras) == extras


@_requires_jq
class TestRunJqWriteHappyPath:
    """run_jq_write: real jq round-trip against tmp_path JSON."""

    def test_identity_filter(self, tmp_path: Path) -> None:
        """Preserve file contents when applying the identity filter."""
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"a": 1, "b": [2, 3]}), encoding="utf-8")
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}

    def test_field_update_with_arg(self, tmp_path: Path) -> None:
        """Substitute a named argument value into the filter."""
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"version": "0.1.0"}), encoding="utf-8")
        rc = jq_write.run_jq_write(target, ".version = $v", ["--arg", "v", "0.2.0"])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"version": "0.2.0"}

    def test_tmp_file_cleaned_up_on_success(self, tmp_path: Path) -> None:
        """Remove the temporary output after a successful write."""
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"x": 1}), encoding="utf-8")
        jq_write.run_jq_write(target, ".", [])
        assert not (tmp_path / "obj.json.tmp").exists()


class TestRunJqWriteErrorPaths:
    """run_jq_write: exit-code contract for failure modes."""

    def test_missing_target_returns_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Missing target file → exit 1 with stderr message."""
        rc = jq_write.run_jq_write(tmp_path / "absent.json", ".", [])
        assert rc == 1
        assert "target not found" in capsys.readouterr().err

    def test_outside_allowed_roots_returns_4(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Target outside cwd and TMPDIR → exit 4 (distinct from missing-target 1)."""
        cwd = tmp_path / "cwd"
        tmpdir = tmp_path / "tmp"
        outside = tmp_path / "outside"
        for d in (cwd, tmpdir, outside):
            d.mkdir()
        target = outside / "obj.json"
        target.write_text("{}", encoding="utf-8")
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("TMPDIR", str(tmpdir))
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 4
        assert "outside allowed roots" in capsys.readouterr().err

    def test_jq_nonzero_exit_returns_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Jq returns non-zero → exit 2, tmp file cleaned up."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")

        def _fake_run(*_args: Any, stderr: Any = None, **_kw: Any) -> subprocess.CompletedProcess[str]:
            """Return jq's configured nonzero result for this error path."""
            return subprocess.CompletedProcess(args=_args, returncode=3, stdout="", stderr="jq: parse error\n")

        monkeypatch.setattr(jq_write.subprocess, "run", _fake_run)
        rc = jq_write.run_jq_write(target, ".bad(", [])
        assert rc == 2
        assert not (tmp_path / "obj.json.tmp").exists()
        # The fake stderr from jq plus our own message both surface.
        err = capsys.readouterr().err
        assert "jq filter failed" in err

    def test_jq_not_installed_returns_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Jq binary absent → FileNotFoundError → exit 2."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")

        def _boom(*_args: Any, **_kw: Any) -> Any:
            """Simulate jq being unavailable on PATH."""
            raise FileNotFoundError("jq")

        monkeypatch.setattr(jq_write.subprocess, "run", _boom)
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 2
        assert not (tmp_path / "obj.json.tmp").exists()
        assert "jq invocation failed" in capsys.readouterr().err

    def test_tmp_file_cleaned_up_on_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failure path removes ``<target>.tmp`` to avoid stale artifacts."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")

        def _fake_run(*_args: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
            """Write the partial temporary result then report jq failure."""
            # Simulate jq writing something to .tmp before failing.
            (tmp_path / "obj.json.tmp").write_text("garbage", encoding="utf-8")
            return subprocess.CompletedProcess(args=_args, returncode=1, stdout="", stderr="")

        monkeypatch.setattr(jq_write.subprocess, "run", _fake_run)
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 2
        assert not (tmp_path / "obj.json.tmp").exists()


class TestMain:
    """Main: CLI surface — argv parsing + exit codes."""

    def test_no_args_returns_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No target/filter → exit 3 with usage line."""
        rc = jq_write.main([])
        assert rc == 3
        assert "Usage:" in capsys.readouterr().err

    def test_only_target_returns_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Target without filter → exit 3."""
        rc = jq_write.main(["file.json"])
        assert rc == 3
        assert "Usage:" in capsys.readouterr().err

    def test_malformed_arg_returns_3(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Reject an argument option missing its name or value."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")
        rc = jq_write.main([str(target), ".", "--arg", "name-only"])
        assert rc == 3
        assert "malformed --arg" in capsys.readouterr().err

    def test_missing_target_returns_1(self, tmp_path: Path) -> None:
        """Target file missing → exit 1."""
        rc = jq_write.main([str(tmp_path / "absent.json"), "."])
        assert rc == 1

    @_requires_jq
    def test_happy_path_end_to_end(self, tmp_path: Path) -> None:
        """Full main() round-trip with real jq."""
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"k": 1}), encoding="utf-8")
        rc = jq_write.main([str(target), ".k = 2"])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"k": 2}

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage and exit 0 (argparse SystemExit)."""
        with pytest.raises(SystemExit) as exc:
            jq_write.main(["--help"])
        assert exc.value.code == 0
        assert "jq_write.py" in capsys.readouterr().out

    @_requires_jq
    def test_golden_arg_passthrough_invocation(self, tmp_path: Path) -> None:
        """Manage call form ``<target> '<filter>' --arg rule '<rule>'`` reaches jq untouched."""
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"rule": "old"}), encoding="utf-8")
        rc = jq_write.main([str(target), ".rule = $rule", "--arg", "rule", "new-value"])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"rule": "new-value"}
