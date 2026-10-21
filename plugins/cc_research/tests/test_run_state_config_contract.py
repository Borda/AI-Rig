"""Pin the ``state.json`` ``config`` that ``/research:run`` writes to the fields its readers consume.

``run`` R1 once wrote ``"config": {}`` and never filled it, so ``/research:fortify`` stopped with ``.config missing
metric_cmd/guard_cmd`` on every run and ``/research:retro`` silently tested a lower-is-better metric as higher. These
tests parse the ``state.json`` template in run/SKILL.md and check that every ``config`` path fortify and retro read —
extracted from their own command text, not restated here — resolves in it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_TEMPLATE_RE = re.compile(r"Write initial `state\.json`.*?```json\n(?P<body>.*?)\n```", re.DOTALL)
#: fortify reads ``jq -r '.config.<path> // empty'``; retro reads ``read_state_field.py … "config.<path>"``.
_READERS = {
    "fortify": re.compile(r"jq -r '\.config\.(?P<path>[a-z_.]+) //"),
    "retro": re.compile(r'"config\.(?P<path>[a-z_.]+)"'),
}


def _run_config() -> dict:
    """Return the ``config`` object of the initial ``state.json`` template in run/SKILL.md."""
    found = _TEMPLATE_RE.search((_SKILLS / "run" / "SKILL.md").read_text(encoding="utf-8"))
    assert found, "run/SKILL.md lost its initial state.json template"
    return json.loads(found["body"])["config"]


def _reader_paths(skill: str) -> set[str]:
    """Return every ``config`` dotted path ``skill`` reads from ``state.json``."""
    text = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    return {match["path"] for match in _READERS[skill].finditer(text)}


def _resolve(config: dict, dotted: str) -> object:
    """Walk ``dotted`` through ``config``; raise ``KeyError`` naming the first missing segment."""
    cursor: object = config
    for segment in dotted.split("."):
        if not isinstance(cursor, dict) or segment not in cursor:
            raise KeyError(f"config.{dotted} — missing segment {segment!r}")
        cursor = cursor[segment]
    return cursor


@pytest.mark.parametrize(
    ("skill", "minimum"),
    [
        pytest.param("fortify", {"metric_cmd", "guard_cmd"}, id="fortify"),
        pytest.param("retro", {"metric_cmd", "metric.direction"}, id="retro"),
    ],
)
def test_every_reader_path_resolves_in_run_config(skill: str, minimum: set[str]) -> None:
    """Each ``config`` path a downstream skill reads exists in the config run writes.

    ``minimum`` guards the extraction itself: if a reader's command text changes shape so the regex finds nothing, the
    test fails instead of passing vacuously.
    """
    config = _run_config()
    paths = _reader_paths(skill)
    assert minimum <= paths
    for dotted in sorted(paths):
        _resolve(config, dotted)


def test_run_config_is_never_empty_and_names_a_direction() -> None:
    """The template carries a populated config with an explicit metric direction, not ``{}``."""
    config = _run_config()
    assert config
    assert _resolve(config, "metric.direction") in {"higher", "lower"}
