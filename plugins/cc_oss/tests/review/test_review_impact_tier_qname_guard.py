"""Argv-safety tests for ``review_impact_tier.run_fn_blast``.

A qualified name derives from changed file names in a pull request, so one starting with ``-`` would be read by
``codemap-py`` as an option. Such a name must never reach the subprocess; unknown impact maps to the full review.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import review_impact_tier as rit


class TestRunFnBlastQnameGuard:
    """run_fn_blast: option-shaped or whitespace-bearing names are refused before the subprocess call."""

    @pytest.mark.parametrize(
        "qname",
        [
            pytest.param("-x::run", id="leading-dash"),
            pytest.param("--help", id="long-option"),
            pytest.param("pkg.mod::run extra", id="embedded-space"),
            pytest.param("pkg.mod::run\nnext", id="embedded-newline"),
        ],
    )
    def test_unsafe_qname_returns_none_without_spawning(self, monkeypatch: pytest.MonkeyPatch, qname: str) -> None:
        """No subprocess runs for an unsafe name, and the caller sees unknown impact."""
        calls: list[list[str]] = []
        monkeypatch.setattr(rit.shutil, "which", lambda _name: "/usr/bin/codemap-py")
        monkeypatch.setattr(rit.subprocess, "run", lambda cmd, **_kw: calls.append(cmd))
        assert rit.run_fn_blast(qname) is None
        assert calls == []

    def test_ordinary_qname_still_queries_codemap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A normal qualified name is passed through as the last argument."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            """Record the command and answer with an empty blast radius."""
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"blast_radius": []}), stderr="")

        monkeypatch.setattr(rit.shutil, "which", lambda _name: "/usr/bin/codemap-py")
        monkeypatch.setattr(rit.subprocess, "run", fake_run)
        assert rit.run_fn_blast("pkg.mod::run") == {"blast_radius": []}
        assert calls[0][-1] == "pkg.mod::run"
