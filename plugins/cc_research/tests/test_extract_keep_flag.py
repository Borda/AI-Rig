"""Tests for ``bin/extract-keep-flag.py`` — keep-items and optional venue extraction.

Covers:
* Keep-value extraction (the zsh ``BASH_REMATCH`` twin dropped this silently), multi-space form
* Stale compaction-contract clearing, and that a missing contract is not an error
* Missing slug / missing session id → exit 2
* ``--venue-choices``: valid, absent, and invalid values, and that keep state is already
  persisted when an invalid venue exits 2
* Argv holding a payload that starts with ``--`` — the reason this script avoids argparse
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _research_text import load_module

_mod = load_module(
    Path(__file__).resolve().parent.parent / "bin" / "extract-keep-flag.py", "research_extract_keep_flag"
)

option_value = _mod.option_value
main = _mod.main

_VENUES = "--venue-choices"
_CHOICES = "CVPR,NeurIPS,ICML,workshop"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated CWD (the contract path is CWD-relative) plus a private sentinel dir."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("TMPDIR", str(sentinels))
    monkeypatch.setenv("CSID", "testsess")
    return tmp_path


def _run(slug: str, arguments: str, *extra: str) -> int:
    """Run ``main`` for ``slug`` with the raw ``$ARGUMENTS`` string plus any extra argv, returning its exit code."""
    return main(["extract-keep-flag.py", slug, arguments, *extra])


def _sentinel(env_dir: Path, name: str) -> Path:
    """Path of the ``<name>-testsess`` sentinel written under the isolated sentinel dir."""
    return env_dir / "sentinels" / f"{name}-testsess"


class TestOptionValue:
    """``option_value`` pulls one option's value out of argv, in spaced or attached form."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param([_VENUES, "CVPR,ICML"], "CVPR,ICML", id="spaced"),
            pytest.param([f"{_VENUES}=CVPR"], "CVPR", id="attached"),
            pytest.param(["--other", "x"], "", id="absent"),
            pytest.param([_VENUES], "", id="dangling"),
        ],
    )
    def test_extracts_value(self, argv: list[str], expected: str) -> None:
        """The value follows the option (spaced) or its ``=`` (attached); absent or dangling yields an empty string."""
        assert option_value(argv, _VENUES) == expected


class TestInputErrors:
    """Missing required input fails with exit 2 instead of writing partial state."""

    def test_missing_slug_exits_two(self, env: Path) -> None:
        """Calling without the skill slug exits 2."""
        assert main(["extract-keep-flag.py"]) == 2

    def test_missing_session_id_exits_two(self, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``CSID`` or ``CLAUDE_CODE_SESSION_ID`` there is no sentinel name to write, so exit 2."""
        monkeypatch.delenv("CSID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        assert _run("fortify", "x") == 2


class TestKeepFlag:
    """The ``--keep "<items>"`` value is persisted to the keep-items sentinel for the compaction contract."""

    def test_value_persisted_and_printed(self, env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A quoted ``--keep`` value is written to the sentinel and echoed on stdout, quotes removed."""
        assert _run("fortify", 'run --keep "plan, decisions"') == 0

        assert _sentinel(env, "fortify-keep-items").read_text(encoding="utf-8") == "plan, decisions\n"
        assert capsys.readouterr().out == "plan, decisions\n"

    def test_absent_flag_writes_empty_line(self, env: Path) -> None:
        """Without ``--keep`` the sentinel holds a single empty line, so a later ``read -r`` finds nothing to keep."""
        assert _run("fortify", "run") == 0

        assert _sentinel(env, "fortify-keep-items").read_text(encoding="utf-8") == "\n"

    def test_stale_contract_cleared(self, env: Path) -> None:
        """A leftover compaction contract from an earlier run is deleted when a new run starts.

        The contract path is CWD-relative; a stale file would leak old preserve items into the next compaction.
        """
        contract = env / "work" / ".temp" / "state" / "skill-contract.md"
        contract.parent.mkdir(parents=True)
        contract.write_text("stale", encoding="utf-8")

        assert _run("fortify", "run") == 0
        assert not contract.exists()

    def test_payload_starting_with_dash(self, env: Path) -> None:
        """Arguments that begin with a flag are read as payload, not parsed as this script's own options.

        Five call sites pass ``"$ARGUMENTS"`` positionally and it often starts with a flag — the reason the script
        avoids argparse.
        """
        assert _run("fortify", '--venue CVPR --keep "a"', _VENUES, _CHOICES) == 0

        assert _sentinel(env, "fortify-keep-items").read_text(encoding="utf-8") == "a\n"
        assert _sentinel(env, "fortify-venue").read_text(encoding="utf-8") == "CVPR\n"


class TestVenueFlag:
    """The optional ``--venue`` value is validated against ``--venue-choices`` and persisted."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("run --venue CVPR", "CVPR\n", id="cvpr"),
            pytest.param("run --venue NeurIPS", "NeurIPS\n", id="neurips"),
            pytest.param("run --venue ICML", "ICML\n", id="icml"),
            pytest.param("run --venue workshop", "workshop\n", id="workshop"),
            pytest.param("run", "\n", id="absent-venue-writes-empty"),
        ],
    )
    def test_sentinel_value(self, env: Path, arguments: str, expected: str) -> None:
        """Each valid venue is persisted to the venue sentinel; an absent venue writes an empty line.

        Empty venue is legal — it is what makes the F6 skip rule fire; there is no default venue.
        """
        assert _run("fortify", arguments, _VENUES, _CHOICES) == 0

        assert _sentinel(env, "fortify-venue").read_text(encoding="utf-8") == expected

    def test_invalid_venue_exits_two(self, env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A venue outside the allowed choices exits 2 with a message listing the valid ones."""
        assert _run("fortify", "run --venue SIGGRAPH", _VENUES, _CHOICES) == 2

        assert "fortify: invalid --venue 'SIGGRAPH' — valid: CVPR, NeurIPS, ICML, workshop" in capsys.readouterr().err

    def test_invalid_venue_still_leaves_keep_state_written(self, env: Path) -> None:
        """Contract clearing and the keep sentinel happen first, exactly as the shell twin ordered them.

        An invalid venue aborts with exit 2, but the keep items were already persisted and no venue sentinel exists.
        """
        assert _run("fortify", 'run --venue SIGGRAPH --keep "a"', _VENUES, _CHOICES) == 2

        assert _sentinel(env, "fortify-keep-items").read_text(encoding="utf-8") == "a\n"
        assert not _sentinel(env, "fortify-venue").exists()

    def test_no_venue_sentinel_without_choices(self, env: Path) -> None:
        """Callers that never pass --venue-choices keep the original single-sentinel behaviour."""
        assert _run("sweep", "run --venue CVPR") == 0

        assert not _sentinel(env, "sweep-venue").exists()
