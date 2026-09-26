"""Test host manifest agreement and committed version continuity."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


_MOD_PATH = Path(__file__).resolve().parent.parent / "bin" / "check_plugin_version_sync.py"
_spec = importlib.util.spec_from_file_location("check_plugin_version_sync", _MOD_PATH)
assert _spec and _spec.loader
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)


def _committed_file(tmp_path: Path, relative_path: str, content: str) -> Path:
    """Create a committed source baseline and return its path for CLI checks."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", relative_path], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    return path


def _committed_json(tmp_path: Path, relative_path: str, data: dict) -> Path:
    """Create a committed JSON baseline for a shipped plugin file."""
    return _committed_file(tmp_path, relative_path, json.dumps(data))


def _plugin(root: Path, name: str, claude: str | None, codex: str | None) -> Path:
    """Create a plugin dir with the requested host manifests (None = omit that host)."""
    plugin = root / name
    if claude is not None:
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": claude}), encoding="utf-8"
        )
    if codex is not None:
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": codex}), encoding="utf-8"
        )
    return plugin


@pytest.mark.packaging
class TestFindDesyncs:
    """Flag disagreeing dual-manifest pairs only."""

    def test_matching_pair_is_clean(self, tmp_path: Path) -> None:
        """A dual-host plugin whose manifests agree produces no findings.

        The everyday post-bump state: both manifests were bumped together, so
        the gate must stay silent.
        """
        _plugin(tmp_path, "dual", "1.2.3", "1.2.3")
        assert vs.find_desyncs(tmp_path) == []

    def test_mismatch_is_reported_with_both_versions(self, tmp_path: Path) -> None:
        """A version bump applied to one host manifest only is flagged, naming both values.

        This is the incident shape: codemap-py's Claude manifest was bumped to
        0.31.3 while the Codex manifest stayed 0.31.2 — two installs claiming
        different releases of identical code.
        """
        _plugin(tmp_path, "dual", "0.31.3", "0.31.2")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert "0.31.3" in findings[0] and "0.31.2" in findings[0]

    def test_single_host_plugins_ignored(self, tmp_path: Path) -> None:
        """Plugins shipping only one host manifest are out of scope.

        Most plugins are Claude-only (or Codex-only, like codex-rig); they have no counterpart to desync from and must
        not produce noise.
        """
        _plugin(tmp_path, "claude-only", "9.9.9", None)
        _plugin(tmp_path, "codex-only", None, "8.8.8")
        assert vs.find_desyncs(tmp_path) == []

    def test_missing_version_field_is_flagged(self, tmp_path: Path) -> None:
        """A dual-host manifest without a ``version`` string is a finding, not a pass.

        Silently treating an absent field as matching would let a malformed manifest disable the gate exactly when it is
        needed.
        """
        plugin = _plugin(tmp_path, "dual", "1.0.0", "1.0.0")
        (plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "dual"}), encoding="utf-8")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert ".codex-plugin" in findings[0]

    def test_unparseable_manifest_is_flagged(self, tmp_path: Path) -> None:
        """Invalid JSON in either manifest is a finding rather than a crash or a pass.

        The checker runs in pre-commit; a corrupt file must fail the gate with a location, never traceback.
        """
        plugin = _plugin(tmp_path, "dual", "1.0.0", "1.0.0")
        (plugin / ".claude-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert ".claude-plugin" in findings[0]


@pytest.mark.packaging
class TestMain:
    """CLI exit codes mirror the findings."""

    @pytest.mark.parametrize(
        ("claude", "codex", "expected"),
        [pytest.param("1.0.0", "1.0.0", 0, id="in-sync"), pytest.param("1.0.1", "1.0.0", 1, id="desynced")],
    )
    def test_exit_codes(self, tmp_path: Path, capsys, claude: str, codex: str, expected: int) -> None:
        """Exit 0 when every pair agrees, 1 on any desync (with a VERSION-DESYNC line).

        pre-commit keys purely on the exit code; the printed finding is what the committer acts on.
        """
        _plugin(tmp_path, "dual", claude, codex)
        rc = vs.main(["--scan-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == expected
        assert ("VERSION-DESYNC" in out) == bool(expected)

    def test_real_repo_scan_is_clean(self) -> None:
        """The actual repository passes — bridge and codemap-py pairs agree.

        Guards the live tree: a merge that desyncs a real pair fails here even
        before the pre-commit hook runs.
        """
        repo_plugins = Path(__file__).resolve().parents[3] / "plugins"
        assert vs.find_desyncs(repo_plugins) == []


@pytest.mark.packaging
class TestHeadContinuity:
    """Version fields use the same field in committed HEAD as their baseline."""

    def test_skipped_integer_version_fails(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A jump from 1 to 3 fails even when the JSON is otherwise valid."""
        path = _committed_json(tmp_path, "plugins/example/runtime/contract.json", {"schema_version": 1})
        path.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "schema_version" in capsys.readouterr().out

    def test_new_nested_contract_starts_at_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A new nested version family cannot begin at 3."""
        path = _committed_json(tmp_path, "plugins/example/skills/review/result-template.json", {"metadata": {}})
        path.write_text(json.dumps({"metadata": {"action_contract_version": 3}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "metadata.action_contract_version" in capsys.readouterr().out

    def test_numeric_version_field_starts_at_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A generic integer version field cannot bypass the HEAD continuity gate."""
        path = _committed_json(tmp_path, "plugins/example/runtime/contract.json", {"payload": {}})
        path.write_text(json.dumps({"payload": {"version": 3}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "payload.version" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("current", "expected"),
        [pytest.param(2, 0, id="next-protocol"), pytest.param(3, 1, id="skipped-protocol")],
    )
    def test_nested_json_protocol_continuity(
        self, tmp_path: Path, monkeypatch, capsys, current: int, expected: int
    ) -> None:
        """A shipped nested integer protocol advances at most once from HEAD."""
        path = _committed_json(tmp_path, "plugins/example/package-manifest.json", {"bootstrap": {"protocol": 1}})
        path.write_text(json.dumps({"bootstrap": {"protocol": current}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
        if expected:
            assert "bootstrap.protocol" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("current", "expected"),
        [pytest.param(2, 0, id="next"), pytest.param(3, 1, id="skip")],
    )
    def test_json_protocol_suffix_continuity(
        self, tmp_path: Path, monkeypatch, capsys, current: int, expected: int
    ) -> None:
        """A shipped integer field ending in `_protocol` keeps its HEAD baseline."""
        path = _committed_json(tmp_path, "plugins/example/runtime/contract.json", {"bootstrap_protocol": 1})
        path.write_text(json.dumps({"bootstrap_protocol": current}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
        if expected:
            assert "bootstrap_protocol" in capsys.readouterr().out

    @pytest.mark.parametrize("field", ["schema", "journal_schema"])
    def test_schema_named_fields_cannot_skip_head(self, tmp_path: Path, monkeypatch, capsys, field: str) -> None:
        """A schema field using either shipped naming convention cannot jump over version 2."""
        path = _committed_json(tmp_path, "plugins/example/runtime/contract.json", {field: 1})
        path.write_text(json.dumps({field: 3}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert field in capsys.readouterr().out

    def test_removed_dual_host_manifest_fails_without_selected_files(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A removed tracked host manifest cannot turn a dual-host plugin into a silent single-host one."""
        plugin = _plugin(tmp_path / "plugins", "example", "0.1.0", "0.1.0")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "plugins/example"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "baseline",
            ],
            check=True,
        )
        (plugin / ".codex-plugin" / "plugin.json").unlink()
        (plugin / ".codex-plugin").rmdir()
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins"]) == 1
        assert ".codex-plugin" in capsys.readouterr().out

    def test_removed_single_host_manifest_fails_without_selected_files(self, tmp_path: Path, monkeypatch) -> None:
        """Deleting the only tracked release manifest must fail while plugin code remains."""
        manifest = _committed_file(
            tmp_path, "plugins/example/.claude-plugin/plugin.json", json.dumps({"name": "example", "version": "0.1.0"})
        )
        writer = tmp_path / "plugins/example/bin/writer.py"
        writer.parent.mkdir()
        writer.write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "plugins/example/bin/writer.py"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "writer",
            ],
            check=True,
        )
        manifest.unlink()
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins"]) == 1

    def test_python_schema_constant_cannot_skip_head(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A shipped Python writer's schema constant cannot jump from 1 to 3."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION = 1\n")
        path.write_text("SCHEMA_VERSION = 3\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "SCHEMA_VERSION" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("name", "baseline", "current"),
        [
            pytest.param("_SCHEMA", 2, 4, id="package-schema"),
            pytest.param("PROTOCOL", 1, 3, id="role-link-protocol"),
            pytest.param("SCHEMA_VERSION", 1, 3, id="version-suffix"),
        ],
    )
    def test_shipped_python_schema_families_cannot_skip_head(
        self, tmp_path: Path, monkeypatch, capsys, name: str, baseline: int, current: int
    ) -> None:
        """Known shipped static schema and protocol names obey HEAD continuity."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", f"{name} = {baseline}\n")
        path.write_text(f"{name} = {current}\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert name in capsys.readouterr().out

    @pytest.mark.parametrize("name", ["_SCHEMA", "PROTOCOL"])
    def test_shipped_python_schema_families_accept_next(self, tmp_path: Path, monkeypatch, name: str) -> None:
        """The extra shipped names accept one increment from committed HEAD."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", f"{name} = 2\n")
        path.write_text(f"{name} = 3\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 0

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            pytest.param(1, 0, id="unchanged"),
            pytest.param(2, 0, id="next"),
            pytest.param(0, 1, id="downgrade"),
            pytest.param(3, 1, id="skip"),
        ],
    )
    def test_python_literal_version_boundaries(self, tmp_path: Path, monkeypatch, current: int, expected: int) -> None:
        """Only the committed integer or its immediate successor is accepted."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION: int = 1\n")
        path.write_text(f"SCHEMA_VERSION: int = {current}\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected

    def test_new_python_version_starts_at_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A new static version family begins at 1 within an existing module."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "VALUE = 1\n")
        path.write_text("VALUE = 1\nSCHEMA_VERSION = 2\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "new version family" in capsys.readouterr().out

    def test_removed_python_literal_version_fails(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Replacing a static version with a dynamic expression cannot evade the gate."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION = 1\n")
        path.write_text("SCHEMA_VERSION = version()\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "nonliteral version mutation" in capsys.readouterr().out

    def test_later_dynamic_assignment_does_not_hide_version_removal(self, tmp_path: Path, monkeypatch) -> None:
        """A later dynamic assignment overrides an earlier literal in the same module."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION = 1\n")
        path.write_text("SCHEMA_VERSION = 1\nSCHEMA_VERSION = version()\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1

    def test_intermediate_dynamic_version_assignment_fails(self, tmp_path: Path, monkeypatch) -> None:
        """A later literal cannot conceal an intervening dynamic version mutation."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION = 1\n")
        path.write_text("SCHEMA_VERSION = 1\nSCHEMA_VERSION = version()\nSCHEMA_VERSION = 1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1

    @pytest.mark.parametrize(
        ("current", "expected"),
        [pytest.param(2, 0, id="next"), pytest.param(3, 1, id="skip")],
    )
    def test_python_serialized_dict_protocol_continuity(
        self, tmp_path: Path, monkeypatch, capsys, current: int, expected: int
    ) -> None:
        """A literal protocol in a serialized Python dictionary keeps its HEAD baseline."""
        path = _committed_file(
            tmp_path, "plugins/example/bin/writer.py", 'def payload():\n    return {"bootstrap_protocol": 1}\n'
        )
        path.write_text(f'def payload():\n    return {{"bootstrap_protocol": {current}}}\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
        if expected:
            assert "bootstrap_protocol" in capsys.readouterr().out

    def test_new_inline_reference_to_existing_format_is_allowed(self, tmp_path: Path, monkeypatch) -> None:
        """A newly added dictionary can refer to a format already at version 2."""
        baseline = 'def existing():\n    return {"schema": 2}\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", baseline)
        path.write_text(baseline + 'def payload():\n    return {"schema": 2}\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 0

    def test_new_python_dict_version_family_starts_at_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A new dictionary key cannot borrow another version family's HEAD baseline."""
        baseline = 'def existing():\n    return {"schema": 2}\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", baseline)
        path.write_text(baseline + 'PAYLOAD = {"fresh_protocol": 9}\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        output = capsys.readouterr().out
        assert "fresh_protocol" in output
        assert "new version family must start at 1" in output

    def test_new_python_dict_reference_cannot_skip_existing_format(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A new dictionary scope cannot claim a version beyond the existing format's next step."""
        baseline = 'def existing():\n    return {"schema": 2}\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", baseline)
        path.write_text(baseline + 'def payload():\n    return {"schema": 9}\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "schema" in capsys.readouterr().out

    def test_inserted_inline_reference_cannot_hide_protocol_jump(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A new earlier dictionary cannot take the old occurrence's baseline."""
        path = _committed_file(
            tmp_path, "plugins/example/bin/writer.py", 'def old():\n    return {"bootstrap_protocol": 1}\n'
        )
        path.write_text(
            'def new():\n    return {"bootstrap_protocol": 1}\ndef old():\n    return {"bootstrap_protocol": 3}\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "bootstrap_protocol" in capsys.readouterr().out

    def test_inserted_inline_reference_preserves_existing_version(self, tmp_path: Path, monkeypatch) -> None:
        """An earlier reference to version 1 can precede an unchanged version 2."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", 'def old():\n    return {"schema": 2}\n')
        path.write_text(
            'def new():\n    return {"schema": 1}\ndef old():\n    return {"schema": 2}\n', encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 0

    def test_sibling_producer_cannot_mask_schema_jump(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """One producer cannot borrow another producer's higher schema baseline."""
        path = _committed_file(
            tmp_path,
            "plugins/example/bin/writer.py",
            'def producer_a():\n    return {"schema": 1}\ndef producer_b():\n    return {"schema": 2}\n',
        )
        path.write_text(
            'def producer_a():\n    return {"schema": 3}\ndef producer_b():\n    return {"schema": 2}\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "producer_a" in capsys.readouterr().out

    def test_new_sibling_producer_preserves_schema_baseline(self, tmp_path: Path, monkeypatch) -> None:
        """A newly inserted function does not shift an existing producer's baseline."""
        path = _committed_file(
            tmp_path, "plugins/example/bin/writer.py", 'def producer_a():\n    return {"schema": 2}\n'
        )
        path.write_text(
            'def new_producer():\n    return {"schema": 1}\ndef producer_a():\n    return {"schema": 2}\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 0

    def test_class_method_scope_keeps_its_own_schema_baseline(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """Methods with the same name in different classes retain separate versions."""
        path = _committed_file(
            tmp_path,
            "plugins/example/bin/writer.py",
            'class A:\n    def payload(self):\n        return {"schema": 1}\nclass B:\n    def payload(self):\n        return {"schema": 2}\n',
        )
        path.write_text(
            'class A:\n    def payload(self):\n        return {"schema": 3}\nclass B:\n    def payload(self):\n        return {"schema": 2}\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "class:A.function:payload" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("current", "expected"),
        [pytest.param(2, 0, id="one-step"), pytest.param(3, 1, id="masked-jump")],
    )
    def test_named_dicts_in_one_function_keep_separate_versions(
        self, tmp_path: Path, monkeypatch, capsys, current: int, expected: int
    ) -> None:
        """A higher sibling version cannot mask a named dictionary's jump."""
        source = 'def payload():\n    first = {"schema": 1}\n    second = {"schema": 2}\n    return first, second\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", source)
        path.write_text(source.replace('first = {"schema": 1}', f'first = {{"schema": {current}}}'), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
        if expected:
            assert "first" in capsys.readouterr().out

    def test_inserting_named_dict_in_same_function_is_allowed(self, tmp_path: Path, monkeypatch) -> None:
        """A new named dictionary does not shift stable assignments in its function."""
        source = 'def payload():\n    first = {"schema": 1}\n    second = {"schema": 2}\n    return first, second\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", source)
        path.write_text(
            'def payload():\n    added = {"schema": 1}\n    first = {"schema": 1}\n    second = {"schema": 2}\n    return added, first, second\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 0

    def test_changed_unnamed_dicts_in_one_function_fail_closed(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A duplicate unnamed field lacks enough identity to prove its change safe."""
        source = 'def payload(which):\n    if which:\n        return {"schema": 1}\n    return {"schema": 2}\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", source)
        path.write_text(source.replace('return {"schema": 1}', 'return {"schema": 3}'), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "ambiguous changed inline version producers" in capsys.readouterr().out

    def test_reordered_unnamed_dict_versions_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        """Equal multisets still cannot prove which unnamed producer changed."""
        source = 'def payload(which):\n    if which:\n        return {"schema": 1}\n    return {"schema": 2}\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", source)
        path.write_text(
            'def payload(which):\n    if which:\n        return {"schema": 2}\n    return {"schema": 1}\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1

    def test_augmented_python_version_assignment_fails(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A post-literal augmented mutation cannot hide the effective version."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION = 1\n")
        path.write_text("SCHEMA_VERSION = 1\nSCHEMA_VERSION += 2\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "SCHEMA_VERSION" in capsys.readouterr().out

    def test_complete_plugin_removal_is_allowed(self, tmp_path: Path, monkeypatch) -> None:
        """Removing an entire plugin does not leave a manifest obligation."""
        manifest = _committed_file(
            tmp_path, "plugins/example/.claude-plugin/plugin.json", json.dumps({"version": "0.1.0"})
        )
        manifest.unlink()
        manifest.parent.rmdir()
        manifest.parent.parent.rmdir()
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins"]) == 0

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            pytest.param(1, 0, id="new-family-one"),
            pytest.param(2, 1, id="new-family-two"),
        ],
    )
    def test_new_nested_family_boundary(self, tmp_path: Path, monkeypatch, current: int, expected: int) -> None:
        """A new nested field accepts only its first integer version."""
        path = _committed_json(tmp_path, "plugins/example/skills/review/result-template.json", {"metadata": {}})
        path.write_text(json.dumps({"metadata": {"action_contract_version": current}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            pytest.param("0.4.7", 0, id="unchanged"),
            pytest.param("0.4.8", 0, id="next-patch"),
            pytest.param("0.5.0", 0, id="next-minor"),
            pytest.param("0.4.9", 1, id="skipped-patch"),
            pytest.param("0.6.0", 1, id="skipped-minor"),
            pytest.param("0.4.6", 1, id="downgrade"),
        ],
    )
    def test_manifest_semver_continuity(self, tmp_path: Path, monkeypatch, current: str, expected: int) -> None:
        """A single-host manifest accepts only unchanged, one patch, or one minor."""
        path = _committed_json(tmp_path, "plugins/example/.claude-plugin/plugin.json", {"version": "0.4.7"})
        path.write_text(json.dumps({"version": current}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            pytest.param("1.0.0", 0, id="next-major"),
            pytest.param("1.0.1", 1, id="major-plus-patch"),
            pytest.param("2.0.0", 1, id="skipped-major"),
        ],
    )
    def test_manifest_major_continuity(self, tmp_path: Path, monkeypatch, current: str, expected: int) -> None:
        """A major bump resets both lower components and cannot skip a major."""
        path = _committed_json(tmp_path, "plugins/example/.claude-plugin/plugin.json", {"version": "0.59.0"})
        path.write_text(json.dumps({"version": current}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
