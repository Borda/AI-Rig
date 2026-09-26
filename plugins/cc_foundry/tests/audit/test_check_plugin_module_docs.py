"""Behavior tests for the repository-wide plugin module-documentation checker."""

from __future__ import annotations

from pathlib import Path

import check_plugin_module_docs as checker


def _write_module(path: Path, source: str) -> None:
    """Create one synthetic shipped module with parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_requires_a_module_docstring_for_every_plugin(tmp_path: Path) -> None:
    """A module in any plugin is found even when that plugin has no special policy."""
    plugins = tmp_path / "plugins"
    _write_module(plugins / "cc_example" / "bin" / "entry.py", "print('missing')\n")

    assert checker.run(plugins) == ["cc_example/bin/entry.py: missing module docstring"]


def test_ignores_generated_report_modules(tmp_path: Path) -> None:
    """Do not classify ignored calibration evidence as shipped plugin modules."""
    plugins = tmp_path / "plugins"
    _write_module(plugins / "cc_example" / "bin" / "entry.py", "print('missing')\n")
    _write_module(plugins / "codex-rig" / ".reports" / "calibration" / "fixture.py", "print('generated')\n")

    assert checker.run(plugins) == ["cc_example/bin/entry.py: missing module docstring"]


def test_enforces_codex_rig_rich_docs_inside_the_general_scan(tmp_path: Path) -> None:
    """Codex Rig's documented six-section policy is retained without a plugin-only hook."""
    plugins = tmp_path / "plugins"
    _write_module(plugins / "codex-rig" / "bin" / "entry.py", '"""Brief module docs."""\n')

    findings = checker.run(plugins)

    assert findings == [
        "codex-rig/bin/entry.py: missing Codex Rig sections: "
        "## Purpose, ## Scope, ## Usage, ## Outputs, ## Failure, ## Used by"
    ]


def test_accepts_clean_generic_and_codex_rig_modules(tmp_path: Path) -> None:
    """A valid generic docstring and the richer Codex Rig form both pass one scan."""
    plugins = tmp_path / "plugins"
    _write_module(plugins / "cc_example" / "bin" / "entry.py", '"""Run the example command."""\n')
    rich = "\n".join(f"{section}\n" + "x" * 120 for section in checker.RICH_DOC_SECTIONS)
    _write_module(plugins / "codex-rig" / "bin" / "entry.py", f'"""{rich}\n"""\n')

    assert checker.run(plugins) == []
