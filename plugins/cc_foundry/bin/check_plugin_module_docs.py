#!/usr/bin/env python3
"""Validate maintainer-facing module documentation across every shipped plugin.

Purpose:
    Keep executable plugin modules discoverable without imposing presentation-only
    assertions on individual plugin test suites.

Scope:
    Walk shipped Python modules below each directory in ``--scan-dir``, excluding
    tests, caches, and generated report evidence. Every module needs a top-level
    docstring. Codex Rig additionally keeps its published six-section documentation contract for maintainers, which its
    package validation and author guidance
    already define.

Usage:
    Run ``python plugins/cc_foundry/bin/check_plugin_module_docs.py`` locally or
    through the ``check-plugin-module-docs`` pre-commit hook.

Outputs:
    Print one actionable finding per invalid module and exit 1; print a compact
    success line and exit 0 when all discovered plugin modules comply.

Failure:
    Exit 2 for a missing scan directory. Syntax errors and missing docstrings are
    reported as findings, so they block the commit without aborting the scan.

Used by:
    Root pre-commit configuration and Foundry's deterministic static-audit driver.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

RICH_DOC_PLUGIN = "codex-rig"
RICH_DOC_SECTIONS = ("## Purpose", "## Scope", "## Usage", "## Outputs", "## Failure", "## Used by")
RICH_DOC_MINIMUM_CHARACTERS = 700
EXCLUDED_DIRECTORY_NAMES = frozenset({"tests", "__pycache__", ".reports"})


def production_modules(plugins_dir: Path) -> list[Path]:
    """Return shipped plugin modules, excluding tests, caches, and generated reports."""
    return sorted(path for path in plugins_dir.rglob("*.py") if not EXCLUDED_DIRECTORY_NAMES.intersection(path.parts))


def findings_for_module(module: Path, plugins_dir: Path) -> list[str]:
    """Return documentation findings for one module without stopping the full scan."""
    relative = module.relative_to(plugins_dir).as_posix()
    try:
        document = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    except (OSError, SyntaxError) as error:
        return [f"{relative}: cannot parse module ({error})"]

    docstring = ast.get_docstring(document, clean=False) or ""
    if not docstring.strip():
        return [f"{relative}: missing module docstring"]

    # Codex Rig publishes a richer module-documentation contract. Keeping that
    # exception in this one repository-wide checker avoids a dedicated narrow test.
    if module.relative_to(plugins_dir).parts[0] != RICH_DOC_PLUGIN:
        return []

    absent = [section for section in RICH_DOC_SECTIONS if section not in docstring]
    if absent:
        return [f"{relative}: missing Codex Rig sections: {', '.join(absent)}"]
    if len(docstring) < RICH_DOC_MINIMUM_CHARACTERS:
        return [f"{relative}: Codex Rig docstring is under {RICH_DOC_MINIMUM_CHARACTERS} characters"]
    return []


def run(plugins_dir: Path) -> list[str]:
    """Collect documentation findings for every shipped module under ``plugins_dir``."""
    findings: list[str] = []
    for module in production_modules(plugins_dir):
        findings.extend(findings_for_module(module, plugins_dir))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Run the module-documentation check and return a conventional process status."""
    parser = argparse.ArgumentParser(description="Validate shipped plugin module docstrings")
    parser.add_argument("--scan-dir", default="plugins", help="root directory containing plugin folders")
    args = parser.parse_args(argv)
    plugins_dir = Path(args.scan_dir)
    if not plugins_dir.is_dir():
        print(f"check-plugin-module-docs: scan directory not found: {plugins_dir}", file=sys.stderr)
        return 2

    findings = run(plugins_dir)
    if findings:
        print("\n".join(findings))
        return 1
    print(f"✓ module documentation: {len(production_modules(plugins_dir))} plugin modules checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
