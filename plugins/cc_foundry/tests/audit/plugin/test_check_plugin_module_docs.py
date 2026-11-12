"""Behavior tests for the repository-wide plugin module-documentation checker."""

from __future__ import annotations

from pathlib import Path

import check_plugin_module_docs as checker
import pytest

_MISSING_ENTRY = ["cc_example/bin/entry.py: missing module docstring"]


def _write_modules(plugins: Path, files: dict[str, str]) -> None:
    """Create each ``relative path -> source`` synthetic shipped module under ``plugins`` with parent directories."""
    for rel, source in files.items():
        path = plugins / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


class TestRun:
    """Covers the scan of every plugin's modules for a documented module docstring."""

    @pytest.mark.parametrize(
        ("root", "files"),
        [
            pytest.param(
                ("plugins",), {"cc_example/bin/entry.py": "print('missing')\n"}, id="any-plugin-without-policy"
            ),
            pytest.param(
                ("plugins",),
                {
                    "cc_example/bin/entry.py": "print('missing')\n",
                    "codex-rig/.reports/calibration/fixture.py": "print('generated')\n",
                },
                id="generated-report-modules-ignored",
            ),
            pytest.param(
                ("node_modules", "checkout", "plugins"),
                {
                    "cc_example/bin/entry.py": "print('missing')\n",
                    "cc_example/node_modules/vendor.py": "print('vendor')\n",
                },
                id="exclusions-relative-to-scan-root",
            ),
        ],
    )
    def test_reports_only_first_party_module_without_docstring(
        self, tmp_path: Path, root: tuple[str, ...], files: dict[str, str]
    ) -> None:
        """A first-party module with no docstring is found; generated and vendored modules are not.

        A module in any plugin is found even when that plugin has no special policy. Ignored calibration evidence is not
        shipped plugin code, and an excluded ancestor of the scan root (``node_modules/checkout``) must not hide first-
        party files while a nested ``node_modules`` still is excluded.
        """
        plugins = tmp_path.joinpath(*root)
        _write_modules(plugins, files)

        assert checker.run(plugins) == _MISSING_ENTRY

    def test_enforces_codex_rig_rich_docs_inside_the_general_scan(self, tmp_path: Path) -> None:
        """Codex Rig's documented six-section policy is retained without a plugin-only hook.

        A one-line docstring is enough for a generic plugin but must list every missing Codex Rig section.
        """
        plugins = tmp_path / "plugins"
        _write_modules(plugins, {"codex-rig/bin/entry.py": '"""Brief module docs."""\n'})

        assert checker.run(plugins) == [
            "codex-rig/bin/entry.py: missing Codex Rig sections: "
            "## Purpose, ## Scope, ## Usage, ## Outputs, ## Failure, ## Used by"
        ]

    def test_accepts_clean_generic_and_codex_rig_modules(self, tmp_path: Path) -> None:
        """A valid generic docstring and the richer Codex Rig form both pass one scan."""
        plugins = tmp_path / "plugins"
        rich = "\n".join(f"{section}\n" + "x" * 120 for section in checker.RICH_DOC_SECTIONS)
        _write_modules(
            plugins,
            {
                "cc_example/bin/entry.py": '"""Run the example command."""\n',
                "codex-rig/bin/entry.py": f'"""{rich}\n"""\n',
            },
        )

        assert checker.run(plugins) == []

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(b"# calf\xe9\n", id="non-utf8-source"),
            pytest.param(("value = " + "(" * 1000 + "1" + ")" * 1000).encode(), id="deep-ast"),
        ],
    )
    def test_unreadable_module_does_not_abort_other_findings(self, tmp_path: Path, source: bytes) -> None:
        """Decoder and parser depth failures are reported while the rest of the scan continues."""
        plugins = tmp_path / "plugins"
        _write_modules(plugins, {"cc_example/bin/entry.py": "value = 1\n"})
        broken = plugins / "cc_example" / "bin" / "broken.py"
        broken.write_bytes(source)

        findings = checker.run(plugins)

        assert len(findings) == 2
        assert findings[0].startswith("cc_example/bin/broken.py: cannot parse module (")
        assert findings[1:] == _MISSING_ENTRY
