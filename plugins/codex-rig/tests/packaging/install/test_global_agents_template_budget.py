"""Budget and pointer-integrity checks for the always-loaded global instruction template."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = PLUGIN_ROOT / "assets" / "AGENTS.md"
DETAILS = PLUGIN_ROOT / "shared" / "global-baseline-details.md"
NATIVE_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
CANONICAL_PRINCIPLES = PLUGIN_ROOT / "shared" / "delivery-principles.md"
#: First and last line of the four-principles block the canonical text states and every host surface copies.
PRINCIPLES_START = "- **Consistent, simple delivery.**"
PRINCIPLES_END = "preserve safety, provenance, and required verification."


def _text(path: Path) -> str:
    """Return one packaged Markdown file as text."""
    return path.read_text(encoding="utf-8")


def _headings(text: str) -> list[str]:
    """Return the titles of every second-level heading, line by line so prose reflow cannot matter."""
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def _numbered_labels(text: str) -> list[str]:
    """Return the bold label of every numbered list item written as ``N.

    **Label.**``.
    """
    matches = (re.match(r"\d+\. \*\*([^*]+)\.\*\*", line) for line in text.splitlines())
    return [match.group(1) for match in matches if match]


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_template_shared_file_pointers_resolve_inside_the_package() -> None:
    """Every ``shared/`` file the template names ships in the package manifest.

    A pointer to an unpackaged file would send an installed session to a document that does not exist.
    """
    manifest = json.loads(_text(PLUGIN_ROOT / "package-manifest.json"))
    shipped = {item["path"] for item in manifest["files"]}
    named = set(re.findall(r"\bshared/[\w.-]+\.(?:md|py)\b", _text(TEMPLATE)))

    assert "shared/global-baseline-details.md" in named
    assert named <= shipped, f"template points at unpackaged files: {sorted(named - shipped)}"


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_template_section_pointers_match_details_headings_exactly() -> None:
    """Every details section is reachable from a template trigger line, and every trigger line resolves.

    Section names are matched exactly in both directions, so a renamed heading or a stale pointer fails here.
    """
    pointed = set(re.findall(r'global-baseline-details\.md`\s+§"([^"]+)"', _text(TEMPLATE)))
    headings = set(_headings(_text(DETAILS)))

    assert pointed == headings, f"unresolved: {sorted(pointed - headings)}; orphaned: {sorted(headings - pointed)}"


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_template_pointer_into_native_contract_resolves() -> None:
    """The inline pytest rule's ``§Sandboxed Test Runs`` reference exists as a heading in the shipped contract."""
    assert "§Sandboxed Test Runs" in _text(TEMPLATE)
    assert "Sandboxed Test Runs" in _headings(_text(NATIVE_CONTRACT))


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_code_quality_label_references_resolve_to_one_item() -> None:
    """Label-based cross-references each match exactly one numbered item, since list numbers are renumbered.

    Numbers cannot carry references because items get renumbered; bold labels are the stable key, so they must be unique
    and every referenced label must exist.
    """
    texts = [_text(TEMPLATE), _text(DETAILS)]
    labels = [label for text in texts for label in _numbered_labels(text)]
    referenced = {
        label
        for text in texts
        for line in text.splitlines()
        if '§"Code standard details"' in line or "Code Quality, **" in line
        for label in re.findall(r"\*\*([^*]+)\*\*", line)
        if not label.endswith(".")
    }

    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    assert not duplicates, f"duplicate item labels: {duplicates}"
    assert referenced, "no label references found; the reference shape changed"
    assert referenced <= set(labels), f"label references without a matching item: {sorted(referenced - set(labels))}"


@pytest.mark.packaging
@pytest.mark.installed_plugin
def test_template_carries_the_canonical_delivery_principles() -> None:
    """The always-loaded template states the four delivery principles verbatim from the canonical text.

    A session that loads no workflow skill still reads the template, so the principles must be present there, and they
    must be the canonical wording every other host surface ships rather than a hand-written variant.
    """
    canonical = _text(CANONICAL_PRINCIPLES)
    assert PRINCIPLES_START in canonical, "the canonical text no longer states the first delivery principle"
    end = canonical.find(PRINCIPLES_END, canonical.find(PRINCIPLES_START))
    assert end >= 0, "the canonical text no longer states the last delivery principle"
    block = canonical[canonical.find(PRINCIPLES_START) : end + len(PRINCIPLES_END)]

    assert block in _text(TEMPLATE), "the template does not carry the canonical delivery-principles block"
