"""Argv-safety tests for ``review_impact_tier.run_fn_blast``.

A qualified name derives from changed file names in a pull request, so one starting with ``-`` would be read by
``codemap-py`` as an option. Such a name must never reach the subprocess; unknown impact maps to the full review.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest import mock

import pytest
import review_impact_tier as rit


@mock.patch("review_impact_tier.subprocess.run", autospec=True)
@mock.patch(
    "review_impact_tier.shutil.which", new=mock.create_autospec(shutil.which, return_value="/usr/bin/codemap-py")
)
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
    def test_unsafe_qname_returns_none_without_spawning(self, run: mock.Mock, qname: str) -> None:
        """No subprocess runs for an unsafe name, and the caller sees unknown impact.

        A name beginning with a dash would be parsed as a ``codemap-py`` option and one with whitespace could split into
        extra arguments, so the guard must refuse both while ``codemap-py`` is otherwise available.
        """
        assert rit.run_fn_blast(qname) is None
        run.assert_not_called()

    def test_ordinary_qname_queries_codemap(self, run: mock.Mock) -> None:
        """A normal qualified name is passed through as the last argument.

        The guard must not over-block: a plain ``module::function`` name reaches ``codemap-py fn-blast`` and the parsed
        blast radius comes back to the caller.
        """
        run.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps({"blast_radius": []}), stderr="")

        assert rit.run_fn_blast("pkg.mod::run") == {"blast_radius": []}
        assert run.call_args.args[0][-1] == "pkg.mod::run"
