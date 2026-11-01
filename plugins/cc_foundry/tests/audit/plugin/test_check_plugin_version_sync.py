"""Test host manifest agreement and committed version continuity.

``check_plugin_version_sync.py`` keeps dual-host plugin manifests in agreement and compares every shipped version field
(manifest SemVer, JSON schema fields, Python constants and serialized dictionaries) against its committed ``HEAD``
baseline. The HEAD-continuity tests drive the checker against throwaway git repositories under ``tmp_path``: reading the
committed baseline through real git is the contract, so those repositories stay real.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest

_MOD_PATH = Path(__file__).resolve().parents[3] / "bin" / "check_plugin_version_sync.py"
_spec = importlib.util.spec_from_file_location("check_plugin_version_sync", _MOD_PATH)
assert _spec
assert _spec.loader
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)

_REAL_GIT = vs._git
_CONTRACT_JSON = "plugins/example/runtime/contract.json"
_RESULT_TEMPLATE_JSON = "plugins/example/skills/review/result-template.json"
_WRITER_PY = "plugins/example/bin/writer.py"
_CANONICAL_LOOP = "plugins/codex-rig/shared/adversarial_loop.py"
_COPY_LOOP = "plugins/cc_oss/bin/adversarial_loop.py"


def _run_git(repo: Path, *args: str) -> None:
    """Run one git command inside ``repo`` and fail the test if it errors."""
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def _commit_paths(repo: Path, *paths: str, message: str = "baseline") -> None:
    """Initialise ``repo`` if needed and commit ``paths`` as the HEAD baseline."""
    _run_git(repo, "init", "-q")
    _run_git(repo, "add", *paths)
    _run_git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", message)


def _committed_file(tmp_path: Path, relative_path: str, content: str) -> Path:
    """Create a committed source baseline and return its path for CLI checks."""
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    _commit_paths(tmp_path, relative_path)
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


def _git_failing_on(operation: str, canonical: str) -> Callable[..., subprocess.CompletedProcess]:
    """Build a ``_git`` stand-in that fails only the selected read of ``canonical`` and defers otherwise to real git."""

    def fake(args: list[str], **kwargs) -> subprocess.CompletedProcess:
        if args[0] == operation and args[-1] in {canonical, f"HEAD:{canonical}"}:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="canonical read failed")
        return _REAL_GIT(args, **kwargs)

    return fake


@pytest.fixture
def scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., int]:
    """Return a runner that executes the checker from ``tmp_path`` over ``plugins`` and the given changed files."""
    monkeypatch.chdir(tmp_path)

    def run(*changed: Path) -> int:
        return vs.main(["--scan-dir", "plugins", *(str(path.relative_to(tmp_path)) for path in changed)])

    return run


@pytest.fixture
def discontinuities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    """Return a runner that lists HEAD-continuity findings for changed files, relative to ``tmp_path``."""
    monkeypatch.chdir(tmp_path)

    def run(*changed: Path) -> list[str]:
        return vs.find_head_discontinuities([str(path.relative_to(tmp_path)) for path in changed])

    return run


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
        assert "0.31.3" in findings[0]
        assert "0.31.2" in findings[0]

    def test_single_host_plugins_ignored(self, tmp_path: Path) -> None:
        """Plugins shipping only one host manifest are out of scope.

        Most plugins are Claude-only (or Codex-only, like codex-rig); they have no counterpart to desync from and must
        not produce noise.
        """
        _plugin(tmp_path, "claude-only", "9.9.9", None)
        _plugin(tmp_path, "codex-only", None, "8.8.8")
        assert vs.find_desyncs(tmp_path) == []

    @pytest.mark.parametrize(
        ("manifest_dir", "content"),
        [
            pytest.param(".codex-plugin", json.dumps({"name": "dual"}), id="missing-version-field"),
            pytest.param(".claude-plugin", "{not json", id="unparseable-json"),
        ],
    )
    def test_unusable_manifest_is_flagged(self, tmp_path: Path, manifest_dir: str, content: str) -> None:
        """A dual-host manifest without a ``version`` string, or with invalid JSON, is a finding, not a pass.

        Silently treating an absent field as matching would let a malformed manifest disable the gate exactly when it is
        needed; the checker runs in pre-commit, so a corrupt file must fail the gate with a location, never a traceback.
        """
        plugin = _plugin(tmp_path, "dual", "1.0.0", "1.0.0")
        (plugin / manifest_dir / "plugin.json").write_text(content, encoding="utf-8")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert manifest_dir in findings[0]


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


@pytest.mark.packaging
class TestHeadContinuity:
    """Version fields use the same field in committed HEAD as their baseline."""

    @pytest.mark.parametrize(
        ("relative_path", "baseline", "current", "expected", "fragment"),
        [
            pytest.param(
                _CONTRACT_JSON,
                {"schema_version": 1},
                {"schema_version": 3},
                1,
                "schema_version",
                id="skipped-integer-version",
            ),
            pytest.param(
                _CONTRACT_JSON,
                {"payload": {}},
                {"payload": {"version": 3}},
                1,
                "payload.version",
                id="generic-numeric-version-field",
            ),
            pytest.param(_CONTRACT_JSON, {"schema": 1}, {"schema": 3}, 1, "schema", id="schema-field"),
            pytest.param(
                _CONTRACT_JSON,
                {"journal_schema": 1},
                {"journal_schema": 3},
                1,
                "journal_schema",
                id="journal-schema-field",
            ),
            # A new nested contract may only begin at 1.
            pytest.param(
                _RESULT_TEMPLATE_JSON,
                {"metadata": {}},
                {"metadata": {"action_contract_version": 3}},
                1,
                "metadata.action_contract_version",
                id="new-nested-contract-not-at-one",
            ),
            pytest.param(
                _RESULT_TEMPLATE_JSON,
                {"metadata": {}},
                {"metadata": {"action_contract_version": 1}},
                0,
                "action_contract_version",
                id="new-nested-family-one",
            ),
            pytest.param(
                _RESULT_TEMPLATE_JSON,
                {"metadata": {}},
                {"metadata": {"action_contract_version": 2}},
                1,
                "action_contract_version",
                id="new-nested-family-two",
            ),
            # A nested integer protocol advances at most once from HEAD.
            pytest.param(
                "plugins/example/package-manifest.json",
                {"bootstrap": {"protocol": 1}},
                {"bootstrap": {"protocol": 2}},
                0,
                "bootstrap.protocol",
                id="nested-protocol-next",
            ),
            pytest.param(
                "plugins/example/package-manifest.json",
                {"bootstrap": {"protocol": 1}},
                {"bootstrap": {"protocol": 3}},
                1,
                "bootstrap.protocol",
                id="nested-protocol-skip",
            ),
            # An integer field ending in ``_protocol`` keeps its HEAD baseline.
            pytest.param(
                _CONTRACT_JSON,
                {"bootstrap_protocol": 1},
                {"bootstrap_protocol": 2},
                0,
                "bootstrap_protocol",
                id="protocol-suffix-next",
            ),
            pytest.param(
                _CONTRACT_JSON,
                {"bootstrap_protocol": 1},
                {"bootstrap_protocol": 3},
                1,
                "bootstrap_protocol",
                id="protocol-suffix-skip",
            ),
        ],
    )
    def test_json_version_field_continuity(
        self,
        tmp_path: Path,
        scan,
        capsys,
        relative_path: str,
        baseline: dict,
        current: dict,
        expected: int,
        fragment: str,
    ) -> None:
        """A shipped JSON version field may stay or advance one step from HEAD; a jump fails and names the field.

        Covers a skipped integer ``schema_version``, new nested contracts that must begin at 1, a generic integer
        ``version`` field, both shipped schema naming conventions, and nested or ``_protocol``-suffixed protocols.
        """
        path = _committed_json(tmp_path, relative_path, baseline)
        path.write_text(json.dumps(current), encoding="utf-8")

        assert scan(path) == expected
        assert (fragment in capsys.readouterr().out) == bool(expected)

    @pytest.mark.parametrize(
        ("baseline", "current", "expected", "fragment"),
        [
            pytest.param("_SCHEMA = 2", "_SCHEMA = 4", 1, "_SCHEMA", id="package-schema-skip"),
            pytest.param("PROTOCOL = 1", "PROTOCOL = 3", 1, "PROTOCOL", id="role-link-protocol-skip"),
            pytest.param("SCHEMA_VERSION = 1", "SCHEMA_VERSION = 3", 1, "SCHEMA_VERSION", id="version-suffix-skip"),
            pytest.param("_SCHEMA = 2", "_SCHEMA = 3", 0, "_SCHEMA", id="package-schema-next"),
            pytest.param("PROTOCOL = 2", "PROTOCOL = 3", 0, "PROTOCOL", id="role-link-protocol-next"),
            pytest.param("SCHEMA_VERSION: int = 1", "SCHEMA_VERSION: int = 1", 0, "SCHEMA_VERSION", id="unchanged"),
            pytest.param("SCHEMA_VERSION: int = 1", "SCHEMA_VERSION: int = 2", 0, "SCHEMA_VERSION", id="next"),
            pytest.param("SCHEMA_VERSION: int = 1", "SCHEMA_VERSION: int = 0", 1, "SCHEMA_VERSION", id="downgrade"),
            pytest.param("SCHEMA_VERSION: int = 1", "SCHEMA_VERSION: int = 3", 1, "SCHEMA_VERSION", id="skip"),
        ],
    )
    def test_python_constant_continuity(
        self, tmp_path: Path, scan, capsys, baseline: str, current: str, expected: int, fragment: str
    ) -> None:
        """A shipped static schema or protocol constant keeps its HEAD value or advances by exactly one.

        Covers the package schema, role-link protocol and ``*_VERSION`` name families plus the unchanged, next,
        downgrade, and skipped boundaries of an annotated constant.
        """
        path = _committed_file(tmp_path, _WRITER_PY, f"{baseline}\n")
        path.write_text(f"{current}\n", encoding="utf-8")

        assert scan(path) == expected
        assert (fragment in capsys.readouterr().out) == bool(expected)

    @pytest.mark.parametrize(
        ("baseline", "current", "expected", "fragment"),
        [
            # A literal protocol in a serialized dictionary keeps its HEAD baseline.
            pytest.param(
                'def payload():\n    return {"bootstrap_protocol": 1}\n',
                'def payload():\n    return {"bootstrap_protocol": 2}\n',
                0,
                "bootstrap_protocol",
                id="dict-protocol-next",
            ),
            pytest.param(
                'def payload():\n    return {"bootstrap_protocol": 1}\n',
                'def payload():\n    return {"bootstrap_protocol": 3}\n',
                1,
                "bootstrap_protocol",
                id="dict-protocol-skip",
            ),
            # Key writes establish the same schema baseline as dictionary literals.
            pytest.param(
                'def existing(payload):\n    payload["record_version"] = 1\n',
                'def existing(payload):\n    payload["record_version"] = 2\ndef preflight():\n    return {"record_version": 2}\n',
                0,
                "exceeds the next HEAD value (2)",
                id="key-assignment-next-with-new-reference",
            ),
            pytest.param(
                'def existing(payload):\n    payload["record_version"] = 1\n',
                'def existing(payload):\n    payload["record_version"] = 3\ndef preflight():\n    return {"record_version": 2}\n',
                1,
                "exceeds the next HEAD value (2)",
                id="key-assignment-skip",
            ),
            # A higher sibling version cannot mask a named dictionary's jump.
            pytest.param(
                'def payload():\n    first = {"schema": 1}\n    second = {"schema": 2}\n    return first, second\n',
                'def payload():\n    first = {"schema": 2}\n    second = {"schema": 2}\n    return first, second\n',
                0,
                "first",
                id="named-dicts-one-step",
            ),
            pytest.param(
                'def payload():\n    first = {"schema": 1}\n    second = {"schema": 2}\n    return first, second\n',
                'def payload():\n    first = {"schema": 3}\n    second = {"schema": 2}\n    return first, second\n',
                1,
                "first",
                id="named-dicts-masked-jump",
            ),
            # Adding a dictionary, function or named assignment must not shift an existing producer's baseline.
            pytest.param(
                'def existing():\n    return {"schema": 2}\n',
                'def existing():\n    return {"schema": 2}\ndef payload():\n    return {"schema": 2}\n',
                0,
                "schema",
                id="new-reference-to-existing-format",
            ),
            pytest.param(
                'def old():\n    return {"schema": 2}\n',
                'def new():\n    return {"schema": 1}\ndef old():\n    return {"schema": 2}\n',
                0,
                "schema",
                id="inserted-reference-preserves-existing-version",
            ),
            pytest.param(
                'def producer_a():\n    return {"schema": 2}\n',
                'def new_producer():\n    return {"schema": 1}\ndef producer_a():\n    return {"schema": 2}\n',
                0,
                "schema",
                id="new-sibling-producer-preserves-baseline",
            ),
            pytest.param(
                'def payload():\n    first = {"schema": 1}\n    second = {"schema": 2}\n    return first, second\n',
                "def payload():\n"
                '    added = {"schema": 1}\n'
                '    first = {"schema": 1}\n'
                '    second = {"schema": 2}\n'
                "    return added, first, second\n",
                0,
                "schema",
                id="inserted-named-dict-in-same-function",
            ),
            # A new dictionary key cannot borrow another family's baseline, nor start above 1.
            pytest.param(
                'def existing():\n    return {"schema": 2}\n',
                'def existing():\n    return {"schema": 2}\nPAYLOAD = {"fresh_protocol": 9}\n',
                1,
                "fresh_protocol: new version family must start at 1",
                id="new-dict-family-starts-at-one",
            ),
            pytest.param(
                "VALUE = 1\n",
                "VALUE = 1\nSCHEMA_VERSION = 2\n",
                1,
                "new version family",
                id="new-constant-family-starts-at-one",
            ),
            # An inline producer that jumps, or whose change cannot be attributed, fails closed.
            pytest.param(
                'def existing():\n    return {"schema": 2}\n',
                'def existing():\n    return {"schema": 2}\ndef payload():\n    return {"schema": 9}\n',
                1,
                "schema",
                id="new-scope-cannot-skip-existing-format",
            ),
            pytest.param(
                'def old():\n    return {"bootstrap_protocol": 1}\n',
                'def new():\n    return {"bootstrap_protocol": 1}\ndef old():\n    return {"bootstrap_protocol": 3}\n',
                1,
                "bootstrap_protocol",
                id="inserted-reference-cannot-hide-protocol-jump",
            ),
            pytest.param(
                'def producer_a():\n    return {"schema": 1}\ndef producer_b():\n    return {"schema": 2}\n',
                'def producer_a():\n    return {"schema": 3}\ndef producer_b():\n    return {"schema": 2}\n',
                1,
                "producer_a",
                id="sibling-producer-cannot-mask-jump",
            ),
            pytest.param(
                'class A:\n    def payload(self):\n        return {"schema": 1}\n'
                'class B:\n    def payload(self):\n        return {"schema": 2}\n',
                'class A:\n    def payload(self):\n        return {"schema": 3}\n'
                'class B:\n    def payload(self):\n        return {"schema": 2}\n',
                1,
                "class:A.function:payload",
                id="same-method-name-in-different-classes",
            ),
            pytest.param(
                'def payload(which):\n    if which:\n        return {"schema": 1}\n    return {"schema": 2}\n',
                'def payload(which):\n    if which:\n        return {"schema": 3}\n    return {"schema": 2}\n',
                1,
                "ambiguous changed inline version producers",
                id="changed-unnamed-dicts",
            ),
            pytest.param(
                'def payload(which):\n    if which:\n        return {"schema": 1}\n    return {"schema": 2}\n',
                'def payload(which):\n    if which:\n        return {"schema": 2}\n    return {"schema": 1}\n',
                1,
                "ambiguous changed inline version producers",
                id="reordered-unnamed-dicts",
            ),
            # A dynamic or augmented mutation of a committed static version cannot evade the gate.
            pytest.param(
                "SCHEMA_VERSION = 1\n",
                "SCHEMA_VERSION = version()\n",
                1,
                "nonliteral version mutation",
                id="literal-replaced-by-call",
            ),
            pytest.param(
                "SCHEMA_VERSION = 1\n",
                "SCHEMA_VERSION = 1\nSCHEMA_VERSION = version()\n",
                1,
                "SCHEMA_VERSION",
                id="later-dynamic-assignment",
            ),
            pytest.param(
                "SCHEMA_VERSION = 1\n",
                "SCHEMA_VERSION = 1\nSCHEMA_VERSION = version()\nSCHEMA_VERSION = 1\n",
                1,
                "SCHEMA_VERSION",
                id="intermediate-dynamic-assignment",
            ),
            pytest.param(
                "SCHEMA_VERSION = 1\n",
                "SCHEMA_VERSION = 1\nSCHEMA_VERSION += 2\n",
                1,
                "SCHEMA_VERSION",
                id="augmented-assignment",
            ),
        ],
    )
    def test_python_producer_continuity(
        self, tmp_path: Path, scan, capsys, baseline: str, current: str, expected: int, fragment: str
    ) -> None:
        """A version producer in shipped Python is judged against its own HEAD baseline, and ambiguity fails closed.

        Covers serialized dictionary protocols, key assignments, named dictionaries sharing a function, producers
        inserted beside existing ones (which keep their baselines), new version families that must start at 1, inline
        producers that jump or cannot be attributed, and dynamic or augmented mutation of a static literal.
        """
        path = _committed_file(tmp_path, _WRITER_PY, baseline)
        path.write_text(current, encoding="utf-8")

        assert scan(path) == expected
        assert (fragment in capsys.readouterr().out) == bool(expected)

    @pytest.mark.parametrize(
        ("new_version", "expected", "fragment"),
        [
            pytest.param(2, 0, "new version family", id="matches-deleted-sibling"),
            pytest.param(5, 1, "new version family", id="unrelated-deleted-sibling"),
        ],
    )
    def test_new_file_baseline_from_deleted_sibling(
        self, tmp_path: Path, scan, capsys, new_version: int, expected: int, fragment: str
    ) -> None:
        """A version constant moved into a new file passes only when it equals the deleted sibling's value.

        Splitting one committed module into several new files loses the constant's own path-keyed HEAD baseline. The
        deleted sibling in the same plugin excuses an exact match (a pure extraction) but is not a blanket exemption: a
        genuinely new family still has to start at 1.
        """
        old_path = _committed_file(tmp_path, _WRITER_PY, "SCHEMA_VERSION = 2\n")
        old_path.unlink()
        new_path = tmp_path / "plugins/example/bin/writer_pkg/types.py"
        new_path.parent.mkdir(parents=True)
        new_path.write_text(f"SCHEMA_VERSION = {new_version}\n", encoding="utf-8")

        assert scan(new_path) == expected
        assert (fragment in capsys.readouterr().out) == bool(expected)

    def test_removed_dual_host_manifest_fails_without_selected_files(self, tmp_path: Path, scan, capsys) -> None:
        """A removed tracked host manifest cannot turn a dual-host plugin into a silent single-host one."""
        plugin = _plugin(tmp_path / "plugins", "example", "0.1.0", "0.1.0")
        _commit_paths(tmp_path, "plugins/example")
        (plugin / ".codex-plugin" / "plugin.json").unlink()
        (plugin / ".codex-plugin").rmdir()

        assert scan() == 1
        assert ".codex-plugin" in capsys.readouterr().out

    def test_removed_single_host_manifest_fails_without_selected_files(self, tmp_path: Path, scan) -> None:
        """Deleting the only tracked release manifest must fail while plugin code remains."""
        manifest = _committed_file(
            tmp_path, "plugins/example/.claude-plugin/plugin.json", json.dumps({"name": "example", "version": "0.1.0"})
        )
        writer = tmp_path / _WRITER_PY
        writer.parent.mkdir()
        writer.write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
        _commit_paths(tmp_path, _WRITER_PY, message="writer")
        manifest.unlink()

        assert scan() == 1

    def test_complete_plugin_removal_is_allowed(self, tmp_path: Path, scan) -> None:
        """Removing an entire plugin does not leave a manifest obligation."""
        manifest = _committed_file(
            tmp_path, "plugins/example/.claude-plugin/plugin.json", json.dumps({"version": "0.1.0"})
        )
        manifest.unlink()
        manifest.parent.rmdir()
        manifest.parent.parent.rmdir()

        assert scan() == 0

    @pytest.mark.parametrize(
        ("copy_version", "expected"),
        [pytest.param(2, 0, id="copy-matches-canonical"), pytest.param(5, 1, id="copy-skips-canonical")],
    )
    def test_new_propagated_copy_takes_canonical_baseline(
        self, tmp_path: Path, scan, copy_version: int, expected: int
    ) -> None:
        """A MANIFEST copy new to a plugin is checked against its canonical's HEAD, not as a new family.

        ``propagate_shared.py`` adds a byte-identical copy of a canonical file to another plugin; the copy has no HEAD
        path of its own, so without the canonical baseline an unchanged ``SCHEMA_VERSION = 2`` read as a family that
        must start at 1. A copy that skips the canonical's value still fails.
        """
        _committed_file(tmp_path, _CANONICAL_LOOP, "SCHEMA_VERSION = 2\n")
        copy = tmp_path / _COPY_LOOP
        copy.parent.mkdir(parents=True)
        copy.write_text(f"SCHEMA_VERSION = {copy_version}\n", encoding="utf-8")

        assert scan(copy) == expected

    def test_new_propagated_copy_with_untracked_canonical_starts_at_one(self, tmp_path: Path, discontinuities) -> None:
        """An untracked canonical has no committed baseline and its copy can start a new version family."""
        _committed_file(tmp_path, "plugins/example/bin/existing.py", "VALUE = 1\n")
        canonical = tmp_path / _CANONICAL_LOOP
        canonical.parent.mkdir(parents=True)
        canonical.write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
        copy = tmp_path / _COPY_LOOP
        copy.parent.mkdir(parents=True)
        copy.write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")

        assert discontinuities(copy) == []

    @pytest.mark.parametrize(
        ("copy_version", "expected"),
        [
            pytest.param(1, [], id="new-family-starts-at-one"),
            pytest.param(
                4,
                [
                    f"{_CANONICAL_LOOP}:SCHEMA_VERSION: new version family must start at 1, got 4",
                    f"{_COPY_LOOP}:SCHEMA_VERSION: new version family must start at 1, got 4",
                ],
                id="deleted-sibling-cannot-supply-baseline",
            ),
        ],
    )
    def test_untracked_propagated_canonical_ignores_deleted_sibling(
        self, tmp_path: Path, discontinuities, copy_version: int, expected: list[str]
    ) -> None:
        """An untracked canonical and its copy start at one despite deleted siblings in either plugin."""
        old_path = _committed_file(tmp_path, "plugins/cc_oss/bin/unrelated.py", "SCHEMA_VERSION = 4\n")
        old_canonical_sibling = _committed_file(
            tmp_path, "plugins/codex-rig/shared/unrelated.py", "SCHEMA_VERSION = 4\n"
        )
        old_path.unlink()
        old_canonical_sibling.unlink()
        canonical = tmp_path / _CANONICAL_LOOP
        canonical.write_text(f"SCHEMA_VERSION = {copy_version}\n", encoding="utf-8")
        copy = tmp_path / _COPY_LOOP
        copy.write_bytes(canonical.read_bytes())

        assert discontinuities(canonical, copy) == expected

    @pytest.mark.parametrize("operation", ["ls-tree", "show"])
    @mock.patch.object(vs, "_git", autospec=True)
    def test_new_propagated_copy_requires_readable_canonical_head(
        self, mock_git: mock.MagicMock, tmp_path: Path, discontinuities, operation: str
    ) -> None:
        """A Git failure reading the canonical must not let a new copy restart its version family at one."""
        _committed_file(tmp_path, _CANONICAL_LOOP, "SCHEMA_VERSION = 2\n")
        copy = tmp_path / _COPY_LOOP
        copy.parent.mkdir(parents=True)
        copy.write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
        mock_git.side_effect = _git_failing_on(operation, _CANONICAL_LOOP)

        assert discontinuities(copy) == [f"{_COPY_LOOP}: cannot read committed canonical {_CANONICAL_LOOP}"]

    @pytest.mark.parametrize(
        ("baseline", "current", "expected"),
        [
            pytest.param("0.4.7", "0.4.7", 0, id="unchanged"),
            pytest.param("0.4.7", "0.4.8", 0, id="next-patch"),
            pytest.param("0.4.7", "0.5.0", 0, id="next-minor"),
            pytest.param("0.4.7", "0.4.9", 1, id="skipped-patch"),
            pytest.param("0.4.7", "0.6.0", 1, id="skipped-minor"),
            pytest.param("0.4.7", "0.4.6", 1, id="downgrade"),
            pytest.param("0.59.0", "1.0.0", 0, id="next-major"),
            pytest.param("0.59.0", "1.0.1", 1, id="major-plus-patch"),
            pytest.param("0.59.0", "2.0.0", 1, id="skipped-major"),
        ],
    )
    def test_manifest_semver_continuity(self, tmp_path: Path, scan, baseline: str, current: str, expected: int) -> None:
        """A single-host manifest accepts only unchanged, one patch, one minor, or one major step.

        A major bump resets both lower components and cannot skip a major; a skipped patch or minor and a downgrade
        fail.
        """
        path = _committed_json(tmp_path, "plugins/example/.claude-plugin/plugin.json", {"version": baseline})
        path.write_text(json.dumps({"version": current}), encoding="utf-8")

        assert scan(path) == expected
