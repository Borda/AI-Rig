"""Budget and pointer-integrity checks for the always-loaded global instruction template."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PLUGIN_ROOT / "assets" / "AGENTS.md"
DETAILS = PLUGIN_ROOT / "shared" / "global-baseline-details.md"
NATIVE_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"

# Codex reads the whole global template on every turn, so each byte costs context on all of them. Activity-specific
# detail belongs in ``shared/global-baseline-details.md``, loaded only when the template's trigger line names it.
# Raise this number deliberately, in the same change that justifies the added always-loaded text, never to make a
# failing run green. Raised by 217 bytes for the one ``PLUGIN_ROOT`` line the installer renders with the absolute
# installed root: without it no ``shared/<file>`` pointer resolves from a session outside this repository.
TEMPLATE_BYTE_BUDGET = 39035


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_global_template_stays_within_byte_budget() -> None:
    """Prevent always-loaded global instructions from growing unnoticed past the reviewed budget."""
    size = len(TEMPLATE.read_bytes())
    assert size <= TEMPLATE_BYTE_BUDGET, (
        f"assets/AGENTS.md is {size} bytes, over the {TEMPLATE_BYTE_BUDGET}-byte budget: move activity-specific detail "
        "into shared/global-baseline-details.md, or raise TEMPLATE_BYTE_BUDGET with a stated reason"
    )


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_template_shared_file_pointers_resolve_inside_the_package() -> None:
    """Fail when the template names a shipped shared file that the package does not contain."""
    manifest = json.loads((PLUGIN_ROOT / "package-manifest.json").read_text(encoding="utf-8"))
    shipped = {item["path"] for item in manifest["files"]}
    named = set(re.findall(r"\bshared/[\w.-]+\.(?:md|py)\b", TEMPLATE.read_text(encoding="utf-8")))
    assert "shared/global-baseline-details.md" in named
    assert named <= shipped, f"template points at unpackaged files: {sorted(named - shipped)}"


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_template_section_pointers_match_details_headings_exactly() -> None:
    """Keep every details section reachable from a trigger line, and every trigger line resolvable."""
    pointed = set(re.findall(r'global-baseline-details\.md` §"([^"]+)"', TEMPLATE.read_text(encoding="utf-8")))
    headings = set(re.findall(r"^## (.+)$", DETAILS.read_text(encoding="utf-8"), re.M))
    assert pointed == headings, f"unresolved: {sorted(pointed - headings)}; orphaned: {sorted(headings - pointed)}"


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_template_pointer_into_native_contract_resolves() -> None:
    """Keep the inline pytest rule's section reference valid in the shipped contract."""
    assert "§Sandboxed Test Runs" in TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"^## Sandboxed Test Runs$", NATIVE_CONTRACT.read_text(encoding="utf-8"), re.M)
