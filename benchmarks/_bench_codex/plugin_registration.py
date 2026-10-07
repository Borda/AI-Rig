"""Codex plugin roster parsing and admission failure evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

#: Pattern matching a ``[plugins."name"]`` table header in Codex config text and capturing the plugin name.
PLUGIN_TABLE = re.compile(r'^\[plugins\."([^"]+)"\]', flags=re.MULTILINE)

#: Plugins the C arm installs into its disposable home; every other enabled name is host tooling.
TREATMENT_PLUGIN_NAMES = frozenset({"codemap-py", "codex-rig"})


def enabled_plugin_names(plugin_json: str) -> set[str]:
    """Return normalized enabled names from ``codex plugin list --json``.

    Examples:
        >>> sorted(enabled_plugin_names('{"installed": [{"name": "Codemap-Py", "enabled": true}]}'))
        ['codemap-py']
        >>> enabled_plugin_names("not json")
        set()
    """
    try:
        payload = json.loads(plugin_json)
    except json.JSONDecodeError:
        return set()
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("plugins", payload.get("installed", []))
    else:
        entries = []
    if not isinstance(entries, list):
        return set()
    names: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            names.add(entry.lower())
            continue
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name", entry.get("id", ""))).lower()
        if name and bool(entry.get("enabled", entry.get("active", True))):
            names.add(name)
    return names


def registered_plugin_tables(config_path: Path) -> list[str]:
    """Return the ``[plugins."<id>"]`` identifiers a Codex home currently registers.

    The permission composition rewrites ``config.toml`` in place, so a registration that
    disappears between two ``codex plugin list`` calls and a listing that comes back empty
    while the registration survives are different defects with the same symptom.

    Examples:
        >>> registered_plugin_tables(Path("nonexistent-config.toml"))
        []
    """
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError:
        return []
    return sorted(match.group(1) for match in PLUGIN_TABLE.finditer(text))


def registered_plugin_names(config_path: Path) -> frozenset[str]:
    """Return the plugin names a Codex home registers, dropping each marketplace qualifier.

    Codex identifies a registration as ``<name>@<marketplace>``; admission compares the names a home
    installed for itself, which is the part a benchmark arm controls.

    Examples:
        >>> registered_plugin_names(Path("nonexistent-config.toml"))
        frozenset()
    """
    return frozenset(table.partition("@")[0].lower() for table in registered_plugin_tables(config_path))


def treatment_admission(config_path: Path, code: int, stdout: str) -> tuple[bool, tuple[str, ...]]:
    """Decide whether one Codex home carries exactly the reviewed treatment pair it installed.

    Codex ships first-party plugins that a disposable home never registers and cannot remove, and they are present in
    every arm alike, so admission turns on ``config.toml``: the home may register the reviewed pair and nothing else,
    both must be enabled, and every remaining enabled name is returned as host tooling for the isolation evidence.

    Examples:
        >>> roster = '{"installed": [{"name": "codemap-py"}, {"name": "codex-rig"}, {"name": "gmail"}]}'
        >>> treatment_admission(Path("nonexistent-config.toml"), 0, roster)
        (False, ('gmail',))
    """
    enabled = enabled_plugin_names(stdout)
    admitted = (
        code == 0
        and TREATMENT_PLUGIN_NAMES <= enabled
        and registered_plugin_names(config_path) == TREATMENT_PLUGIN_NAMES
    )
    return admitted, tuple(sorted(enabled - TREATMENT_PLUGIN_NAMES))


def control_admission(config_path: Path, code: int, stdout: str) -> tuple[bool, tuple[str, ...]]:
    """Decide whether one Codex home is free of the treatment plugins the control arm forbids.

    The control home must neither register nor enable a treatment plugin; a registration that lists as disabled is
    rejected too, because the installed files would still be present. Host tooling is returned for the evidence.

    Examples:
        >>> control_admission(Path("nonexistent-config.toml"), 0, '{"installed": [{"name": "gmail"}]}')
        (True, ('gmail',))
    """
    enabled = enabled_plugin_names(stdout)
    admitted = code == 0 and not enabled & TREATMENT_PLUGIN_NAMES and not registered_plugin_names(config_path)
    return admitted, tuple(sorted(enabled - TREATMENT_PLUGIN_NAMES))


def plugin_listing_evidence(config_path: Path, code: int, stdout: str, stderr: str) -> str:
    """Summarize one ``codex plugin list`` result for a fail-closed registration error.

    A negative ``code`` is a signal death rather than a Codex exit status, which is why the raw
    return code is reported alongside the parsed roster instead of only the diagnosis.

    Examples:
        >>> plugin_listing_evidence(Path("nonexistent-config.toml"), -9, "", "")
        "rc=-9 enabled=[] registered_in_config=[] stdout='' stderr=''"
    """
    observed = sorted(enabled_plugin_names(stdout))
    registered = registered_plugin_tables(config_path)
    return (
        f"rc={code} enabled={observed} registered_in_config={registered} "
        f"stdout={stdout[:200]!r} stderr={stderr[:200]!r}"
    )
