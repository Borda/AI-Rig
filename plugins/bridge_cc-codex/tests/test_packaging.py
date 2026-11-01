"""Acceptance checks for the bridge's install-shaped package boundary.

The disposable package is built once per module by the real build script. Tests that only read it share that tree; tests
that execute or mutate it work on a private copy so no case can leak state into another.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]
BUILD_SCRIPT = PLUGIN_ROOT / "scripts" / "build_package.py"
VALIDATE_SCRIPT = PLUGIN_ROOT / "scripts" / "validate_package.py"

if str(PLUGIN_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "bin"))

import bridge_diagnose  # noqa: E402  (loaded from the installed-plugin-equivalent bin directory)

_VALIDATE_SPECIFICATION = importlib.util.spec_from_file_location("bridge_validate_package", VALIDATE_SCRIPT)
assert _VALIDATE_SPECIFICATION is not None
assert _VALIDATE_SPECIFICATION.loader is not None
validate_package = importlib.util.module_from_spec(_VALIDATE_SPECIFICATION)
_VALIDATE_SPECIFICATION.loader.exec_module(validate_package)

#: Runtime files whose absence or change the doctor's payload fingerprint must notice.
FINGERPRINTED_RUNTIME_FILES = ("bin/bridge_consent.py", "rules/claude-transfer-policy.txt", "rules/provider-consent.md")


class _Terminal(io.StringIO):
    """Stand in for the operator terminal that alone may mint a one-time consent token."""

    def isatty(self) -> bool:
        """Report a terminal so the installed helper accepts the mint."""
        return True


def _read_json(path: Path) -> dict[str, object]:
    """Read one JSON file as UTF-8."""
    return json.loads(path.read_text(encoding="utf-8"))


def _run(script: Path, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a Python script with the current interpreter, capturing text output without raising on failure."""
    return subprocess.run([sys.executable, str(script), *args], check=False, capture_output=True, text=True, **kwargs)


def _issue_operator_token(package: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Mint the one-time operator token in-process, with a fake terminal on stdin and stdout, and return it."""
    printed = _Terminal()
    with monkeypatch.context() as patch:
        patch.setenv("CODEX_HOME", str(home))
        patch.setattr(sys, "stdout", printed)
        patch.setattr(sys, "stdin", _Terminal())
        assert runpy.run_path(str(package / "bin/bridge_consent.py"))["main"](["issue-token"]) == 0
    return printed.getvalue().splitlines()[1]


@pytest.fixture(scope="module")
def built_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the disposable package once with the real build script; read-only tests share the result."""
    output = tmp_path_factory.mktemp("bridge-package") / "bridge"
    built = _run(BUILD_SCRIPT, "--output", str(output))
    assert built.returncode == 0, built.stderr
    return output


@pytest.fixture
def package_copy(built_package: Path, tmp_path: Path) -> Path:
    """Copy the built package so a test may execute or mutate it without touching the shared tree."""
    copy = tmp_path / "bridge"
    shutil.copytree(built_package, copy)
    return copy


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    """Create an empty native Codex home, so consent never touches the developer's real one."""
    home = tmp_path / "native-home"
    home.mkdir()
    return home


@pytest.fixture
def installed_package(package_copy: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the doctor at the built package copy, as if it were the installed plugin."""
    monkeypatch.setattr(bridge_diagnose, "__file__", str(package_copy / "bin/bridge_diagnose.py"))
    return package_copy


@pytest.mark.integration
@pytest.mark.packaging
class TestBuiltPackage:
    """The disposable package built from source is complete, clean, and valid on its own."""

    def test_validates_without_source_tree_context(self, built_package: Path, tmp_path: Path) -> None:
        """Package validation resolves payload paths from the built copy, run from an unrelated directory.

        A validator that quietly read the source tree would pass in a checkout and fail on a user's installed plugin.
        """
        validated = _run(VALIDATE_SCRIPT, str(built_package), cwd=tmp_path)
        assert validated.returncode == 0, validated.stderr

    def test_excludes_workspace_artifacts_and_tests(self, built_package: Path) -> None:
        """Run directories, macOS metadata and the test tree never ship in the package."""
        top_level = {path.name for path in built_package.iterdir()}
        assert {".temp", ".reports", "tests"}.isdisjoint(top_level)
        assert not any(path.name == ".DS_Store" for path in built_package.rglob("*"))

    @pytest.mark.parametrize(
        "relative",
        [
            "claude-skills/implement/SKILL.md",
            "codex-skills/implement/SKILL.md",
            "bin/bridge_setup.py",
            "schemas/setup-result.schema.json",
        ],
    )
    def test_ships_payload_file(self, built_package: Path, relative: str) -> None:
        """Each runtime file a host loads after install is present in the built package."""
        assert (built_package / relative).is_file()

    def test_ships_question_policy_with_its_details(self, built_package: Path) -> None:
        """The Codex question policy ships beside the details file it points to."""
        policy = built_package / "rules" / "codex-user-questions.md"
        assert policy.is_file()
        assert (policy.parent / "codex-user-questions-details.md").is_file()
        assert "codex-user-questions-details.md" in policy.read_text(encoding="utf-8")

    def test_codex_skills_resolve_the_question_policy_relative_link(self, built_package: Path) -> None:
        """Every Codex skill links the question policy by a relative path that lands on the shipped file.

        The link is relative to the skill directory, so it only resolves if the skill and rule keep their installed
        layout; a moved file would leave the skill pointing at nothing.
        """
        policy = built_package / "rules" / "codex-user-questions.md"
        skills = list((built_package / "codex-skills").glob("*/SKILL.md"))
        assert skills
        assert all("../../rules/codex-user-questions.md" in skill.read_text(encoding="utf-8") for skill in skills)
        assert all((skill.parent / "../../rules/codex-user-questions.md").resolve() == policy for skill in skills)


@pytest.mark.packaging
def test_disposable_package_has_no_nested_marketplace(built_package: Path) -> None:
    """The disposable package must not carry a redundant nested marketplace."""
    assert not (built_package / ".claude-plugin" / "marketplace.json").exists()


@pytest.mark.packaging
def test_claude_code_has_no_mcp_surface() -> None:
    """Claude Code must never auto-discover an MCP server for bridge.

    Claude-side skills never call ``bridge_implement``/``bridge_advise``/``bridge_review`` -- the Claude-to-Codex
    direction is CLI-only (`bin/bridge_call.py` -> `codex exec`). Claude Code auto-discovers any file literally named
    ``.mcp.json`` at a plugin's root regardless of manifest intent, so shipping one here would register a server Claude
    never uses and that previously broke (the ``${PLUGIN_ROOT}``/``${CLAUDE_PLUGIN_ROOT}`` mismatch). The absence of
    the file, not a correctly-configured one, is what shields Claude Code from it.
    """
    claude_manifest = _read_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    assert not (PLUGIN_ROOT / ".mcp.json").exists()
    assert "mcpServers" not in claude_manifest


@pytest.mark.packaging
def test_validate_package_rejects_a_stray_claude_mcp_file(package_copy: Path, tmp_path: Path) -> None:
    """Guard the no-MCP-surface contract against regression, not just document it.

    A future edit re-adding ``.mcp.json`` at the plugin root would silently reintroduce Claude Code's auto-discovery
    exposure; the package gate must refuse it rather than accept it as a harmless extra file.
    """
    (package_copy / ".mcp.json").write_text((package_copy / ".codex-mcp.json").read_text(encoding="utf-8"), "utf-8")

    validated = _run(VALIDATE_SCRIPT, str(package_copy), cwd=tmp_path)

    assert validated.returncode != 0
    assert "no MCP surface" in validated.stderr


@pytest.mark.packaging
class TestCodexMcpConfig:
    """The Codex MCP server entry points at the installed plugin root."""

    def test_manifest_selects_the_shipped_config(self) -> None:
        """The Codex manifest names the `.codex-mcp.json` file this package ships."""
        assert _read_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")["mcpServers"] == "./.codex-mcp.json"

    def test_server_resolves_from_installed_plugin_root(self) -> None:
        """The server command, script and working directory resolve against the installed plugin's own root."""
        server = _read_json(PLUGIN_ROOT / ".codex-mcp.json")["mcpServers"]["bridge"]
        assert server["command"] in {"python", "python3"}
        assert server["args"] == ["bin/bridge_mcp.py", "--stdio"]
        assert server["cwd"] == "."

    def test_env_vars_carry_the_recursion_guard_variable(self) -> None:
        """Pin ``env_vars`` absolutely, not just as a byproduct of the plugin-root variable check.

        `rules/recursion-guard.md` depends on ``CC_CODEX_BRIDGE_DEPTH`` reaching the peer MCP process through this list;
        `_validate_mcp` checks the plugin-root variable and script path but not `env_vars` content, so a silent edit
        dropping this variable would pass every existing gate while disabling the guard.
        """
        env_vars = _read_json(PLUGIN_ROOT / ".codex-mcp.json")["mcpServers"]["bridge"]["env_vars"]
        assert set(env_vars) >= {"HOME", "PATH", "CODEX_HOME", "CC_CODEX_BRIDGE_DEPTH"}


@pytest.mark.packaging
def test_source_package_validation_ignores_non_payload_test_and_cache_files() -> None:
    """Keep the validator's documented default useful in a development checkout.

    Run with no argument against the live source tree, so tests and bytecode caches there must not count as payload.
    """
    validated = _run(VALIDATE_SCRIPT)

    assert validated.returncode == 0, validated.stderr
    assert "Package validation passed" in validated.stdout


@pytest.mark.packaging
class TestHostManifests:
    """Each host manifest selects its own skill surface and nothing of the other host's."""

    @pytest.mark.parametrize(
        ("manifest", "skills_path", "skills_dir", "expected_skills"),
        [
            pytest.param(
                ".claude-plugin/plugin.json",
                "./claude-skills/",
                "claude-skills",
                {"advise", "cancel", "implement", "result", "review", "setup", "status"},
                id="claude",
            ),
            pytest.param(
                ".codex-plugin/plugin.json",
                "./codex-skills/",
                "codex-skills",
                {"advise", "implement", "review", "setup"},
                id="codex",
            ),
        ],
    )
    def test_selects_disjoint_skill_surface(
        self, manifest: str, skills_path: str, skills_dir: str, expected_skills: set[str]
    ) -> None:
        """Each host names the bridge plugin and discovers only its own skill directory.

        Prevents either host from discovering the other host's execution instructions. Version equality across the two
        manifests is enforced repo-wide by the `check-plugin-version-sync` pre-commit hook, so no version literal is
        pinned here.
        """
        data = _read_json(PLUGIN_ROOT / manifest)
        found = {path.parent.name for path in (PLUGIN_ROOT / skills_dir).glob("*/SKILL.md")}
        assert data["name"] == "bridge"
        assert data["skills"] == skills_path
        assert found == expected_skills

    def test_codex_display_name(self) -> None:
        """The Codex catalog shows the bridge under its public display name."""
        assert (
            _read_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")["interface"]["displayName"] == "bridge_CC-Codex"
        )


@pytest.mark.packaging
@pytest.mark.parametrize(
    ("marketplace", "expected_entry"),
    [
        pytest.param(
            ".claude-plugin/marketplace.json",
            {
                "description": "bridge_CC-Codex: guided setup plus bounded implementation, advice, and review across Claude Code and Codex.",
                "name": "bridge",
                "source": "./plugins/bridge_cc-codex",
            },
            id="claude",
        ),
        pytest.param(
            ".agents/plugins/marketplace.json",
            {
                "category": "Productivity",
                "name": "bridge",
                "policy": {"authentication": "ON_INSTALL", "installation": "AVAILABLE"},
                "source": {"path": "./plugins/bridge_cc-codex", "source": "local"},
            },
            id="codex",
        ),
    ],
)
def test_repository_marketplace_advertises_bridge(marketplace: str, expected_entry: dict[str, object]) -> None:
    """Each host's repository catalog lists the bridge with its exact install entry.

    Prevents a complete bridge package from becoming undiscoverable in either host catalog.
    """
    plugins = _read_json(REPOSITORY_ROOT / marketplace)["plugins"]
    assert next(entry for entry in plugins if entry["name"] == "bridge") == expected_entry


@pytest.mark.packaging
def test_diagnose_payload_fingerprint_stays_inside_the_validated_package_manifest() -> None:
    """Keep the doctor's completeness fingerprint aligned with the validated package manifest.

    The two file lists are maintained by hand in parallel; a runtime file added to the package gate but not the payload
    list silently escapes the completeness fingerprint that sync trusts, as the CLI baseline once did. ``.mcp.json`` is
    deliberately absent from both lists -- see `test_claude_code_has_no_mcp_surface`.
    """
    payload = set(bridge_diagnose.PAYLOAD_FILES)

    assert payload <= set(validate_package.REQUIRED_FILES)
    assert {".codex-mcp.json", "rules/cli-baseline.json"} <= payload


@pytest.mark.integration
@pytest.mark.packaging
class TestPackagedConsentHelper:
    """The consent helper runs from the built package layout, outside the source tree."""

    @pytest.fixture
    def consent_env(self, codex_home: Path) -> dict[str, str]:
        """Return the process environment with `CODEX_HOME` pointing at the empty native home."""
        return {**os.environ, "CODEX_HOME": str(codex_home)}

    def test_help_exits_zero(self, package_copy: Path, tmp_path: Path, consent_env: dict[str, str]) -> None:
        """The installed helper starts and prints usage when run from the built package."""
        result = _run(package_copy / "bin/bridge_consent.py", "--help", cwd=tmp_path, env=consent_env)
        assert result.returncode == 0, result.stderr

    def test_check_reports_absent_consent_without_writing_config(
        self, package_copy: Path, tmp_path: Path, codex_home: Path, consent_env: dict[str, str]
    ) -> None:
        """A read-only `check` on a fresh Codex home reports no consent and creates no config file.

        A status probe that wrote `config.toml` would itself grant state the operator never approved.
        """
        result = _run(package_copy / "bin/bridge_consent.py", "check", cwd=tmp_path, env=consent_env)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["consent"] == "absent"
        assert not (codex_home / "config.toml").exists()

    def test_operator_token_grant_configures_consent(
        self,
        package_copy: Path,
        tmp_path: Path,
        codex_home: Path,
        consent_env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Minting a token on the operator terminal, then granting with it, turns `check` from absent to configured.

        The grant runs as a separate non-interactive process with the token in its environment, as the installed
        workflow does; consent must work from the packaged layout end to end.
        """
        token = _issue_operator_token(package_copy, codex_home, monkeypatch)

        granted = _run(
            package_copy / "bin/bridge_consent.py",
            "grant",
            "--approve",
            cwd=tmp_path,
            env={**consent_env, "BRIDGE_OPERATOR_TOKEN": token},
            stdin=subprocess.DEVNULL,
        )
        checked = _run(package_copy / "bin/bridge_consent.py", "check", cwd=tmp_path, env=consent_env)

        assert granted.returncode == 0, granted.stderr
        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout)["consent"] == "configured"


@pytest.mark.integration
@pytest.mark.packaging
class TestInstalledPayloadIdentity:
    """The doctor fingerprints the installed runtime payload and flags a missing or changed file."""

    def test_pristine_package_is_complete(self, installed_package: Path) -> None:
        """A freshly built package reports a complete payload."""
        assert bridge_diagnose._installed_payload_identity()["complete"] is True

    @pytest.mark.parametrize("relative", FINGERPRINTED_RUNTIME_FILES)
    def test_missing_runtime_file_marks_payload_incomplete(self, installed_package: Path, relative: str) -> None:
        """Deleting a fingerprinted runtime file makes the payload incomplete and names that file.

        Setup consent depends on these files; a package missing one must fail diagnosis rather than look installed.
        """
        (installed_package / relative).unlink()

        identity = bridge_diagnose._installed_payload_identity()

        assert identity["complete"] is False
        assert relative in identity["missing"]

    @pytest.mark.parametrize("relative", FINGERPRINTED_RUNTIME_FILES)
    def test_changed_runtime_file_changes_fingerprint(self, installed_package: Path, relative: str) -> None:
        """Appending one byte to a fingerprinted runtime file changes the payload fingerprint.

        Sync trusts the fingerprint to detect a stale install, so any edit to these files must move it.
        """
        before = bridge_diagnose._installed_payload_identity()
        path = installed_package / relative
        path.write_bytes(path.read_bytes() + b"\n")

        after = bridge_diagnose._installed_payload_identity()

        assert after["fingerprint"] != before["fingerprint"]
