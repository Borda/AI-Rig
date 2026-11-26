"""Contract tests for review_env_provision.py: a reviewer environment that never blocks and trusts only what it may.

Reviews recorded "cv2 not installed" or "venv lacks pycocotools" as confidence gaps although the base branch declared
those packages. The helper now installs every base-declared, default-index requirement into an ephemeral environment
outside the reviewed tree, and reports everything else with a cause: a name the PR adds, a direct reference, an index
the base or the user routes elsewhere, or an install failure (network, resolver conflict, build failure, unsupported
platform). These tests run against real git repositories and stub only the installer and interpreter processes.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import review_env_provision as rep
from _process import requires_git
from _review_repo_builder import commit, init_repo, pyproject


def _can_symlink() -> bool:
    """Probe whether this host lets the test process create a symbolic link (Windows may not)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(tmp, os.path.join(tmp, "probe"), target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


requires_symlinks = pytest.mark.skipif(not _can_symlink(), reason="symbolic links not permitted on this host")


@pytest.fixture(autouse=True)
def _isolated_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own index settings, uv overrides and temp root out of every test."""
    for name in [n for n in os.environ if n.startswith(("UV_", "PIP_"))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "system-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "system-data"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmproot"))
    read_text = Path.read_text

    def read_isolated(path: Path, *args, **kwargs) -> str:
        """Simulate absent machine-global configs while reading temporary configs from disk."""
        if path.name in {"uv.toml", "pip.conf", "pip.ini"} and not path.is_relative_to(tmp_path):
            raise FileNotFoundError(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_isolated)


def _repo(tmp_path: Path, base: dict[str, str | None], head: dict[str, str | None]) -> tuple[Path, str, str]:
    """Create a repository with one base and one head commit; return ``(repo, base sha, head sha)``."""
    repo = init_repo(tmp_path / "repo")
    base_sha = commit(repo, base, "base")
    head_sha = commit(repo, head, "head") if head else base_sha
    return repo, base_sha, head_sha


def _write_host_configs(home: Path, files: dict[str, str]) -> None:
    """Write host installer settings into the isolated home directory."""
    for name, content in files.items():
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _plan(repo: Path, base: str, head: str, choice: rep.Selection | None = None) -> rep.Plan:
    """Classify the selection for one base/head pair."""
    return rep.classify_selection(repo, base, head, choice or rep.Selection())[0]


def _gated(plan: rep.Plan) -> dict[str, str]:
    """Map each gated name to its gate reason."""
    return {g["name"]: g["gate_reason"] for g in plan.gated}


def _skips(plan: rep.Plan) -> dict[str, str]:
    """Map each deliberately skipped name to its reason."""
    return {s["name"]: s["reason"] for s in plan.skipped}


_OPENCV_GROUPS = '[dependency-groups]\nopencv-python = ["opencv-python>=5"]\nopencv-python-headless = ["opencv-python-headless>=5"]\n'
_ROUTING_CASES = json.loads(Path(__file__).with_name("routing_cases.json").read_text(encoding="utf-8"))
_PRIVATE_NAME = _ROUTING_CASES["private_name"]
_PUBLIC_NAME = _ROUTING_CASES["public_name"]
_PIP_INDEX = "[global]\nindex-url = https://private.invalid/simple\n"
_EXPLICIT_UV_INDEX = 'name = "local"\nurl = "https://private.invalid/simple"\nexplicit = true\n'
_ROUTED_NAMES = {
    "ordinary": set(),
    "name-routed": {_PRIVATE_NAME},
    "all-routed": {_PRIVATE_NAME, _PUBLIC_NAME},
}


@requires_git
class TestTrustClassification:
    """Only names the base declares, with plain version ranges and the default index, are installed."""

    @pytest.mark.parametrize("suffix", ["extra", "extra[dev]", "extra; python_version >= '3.10'"])
    def test_head_requirement_keeps_the_base_distribution_name(self, tmp_path: Path, suffix: str) -> None:
        """A malformed suffix cannot turn a trusted requirement into a different installer argument."""
        repo, base, head = _repo(
            tmp_path,
            {"pyproject.toml": pyproject(name="consumer", deps=("demo",))},
            {"pyproject.toml": pyproject(name="consumer", deps=(f"demo {suffix}",))},
        )
        plan = _plan(repo, base, head)
        assert (plan.install, _gated(plan)) == ({}, {"demo": "unsafe-spec"})

    def test_pr_added_name_stays_gated(self, tmp_path: Path) -> None:
        """A dependency the PR adds is gated as added-by-pr; the base's own dependency is planned.

        A new name is contributed code by provenance: it stays with the contributor-code gate however safe it looks.
        """
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy>=1.21",))},
                                 {"pyproject.toml": pyproject(deps=("numpy>=1.21", "brand-new-pkg"))})  # fmt: skip
        plan = _plan(repo, base, head)
        assert (list(plan.install), _gated(plan), plan.gated[0]["cause"]) == (
            ["numpy"], {"brand-new-pkg": "added-by-pr"}, "gated"
        )  # fmt: skip

    def test_direct_reference_is_gated_even_for_a_base_name(self, tmp_path: Path) -> None:
        """A VCS reference to a base-declared name is never fetched.

        The head rewrote ``foo>=1`` to a git URL: the name is trusted, its new source is not.
        """
        repo, base, head = _repo(
            tmp_path,
            {"pyproject.toml": pyproject(deps=("foo>=1",))},
            {"pyproject.toml": pyproject(deps=("foo @ git+https://example.invalid/foo.git",))},
        )
        plan = _plan(repo, base, head)  # fmt: skip
        assert ("foo" in plan.install, _gated(plan)) == (False, {"foo": "direct-reference"})

    def test_direct_reference_in_any_manifest_gates_the_name(self, tmp_path: Path) -> None:
        """A name pointed at a git URL in any manifest is gated, even when the selected declaration of it looks clean.

        The PR keeps ``foo>=1`` in ``pyproject.toml`` but points ``foo`` at a git URL in a docs requirements file. The
        gate's dependency report keeps one declaration per name and misses it, so the provisioner checks every file.
        """
        base_files = {"pyproject.toml": pyproject(deps=("foo>=1",)), "docs/requirements.txt": "foo>=1\n"}
        head_files = {"docs/requirements.txt": "foo @ git+https://example.invalid/foo.git\n"}
        repo, base, head = _repo(tmp_path, base_files, head_files)
        plan = _plan(repo, base, head)
        assert ("foo" in plan.install, _gated(plan)) == (False, {"foo": "direct-reference"})

    def test_specifier_outside_the_plain_grammar_is_gated(self, tmp_path: Path) -> None:
        """A marker naming anything but a PEP 508 variable never reaches an installer argv.

        The head appends a code-shaped marker to a trusted name; the requirement is gated and its text is not echoed.
        """
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy>=1",))},
                                 {"pyproject.toml": pyproject(deps=("numpy>=1; os.system('x')",))})  # fmt: skip
        plan = _plan(repo, base, head)
        assert (_gated(plan), "os.system" in json.dumps(plan.gated)) == ({"numpy": "unsafe-spec"}, False)

    def test_include_group_is_resolved_not_fetched_as_a_package(self, tmp_path: Path) -> None:
        """``{include-group = "test"}`` expands to the group's requirements; no package named ``test`` is planned.

        A flattening parser would read the table value as a requirement and fetch a PyPI package called ``test``.
        """
        tail = '[dependency-groups]\ntest = ["pytest>=8"]\ndev = [{include-group = "test"}, "ruff"]\n'
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(tail=tail)}, {})
        assert set(_plan(repo, base, head).install) == {"pytest", "ruff"}

    def test_documentation_requirements_are_not_selected(self, tmp_path: Path) -> None:
        """Docs-only requirement files stay out of a review environment.

        Root, ``requirements/`` and tests requirement files count; ``docs/`` and ``*-docs`` files do not.
        """
        files = {"pyproject.toml": pyproject(deps=("numpy",)), "docs/requirements.txt": "sphinx\n",
                 "requirements-docs.txt": "mkdocs\n", "requirements/test.txt": "pytest\n"}  # fmt: skip
        repo, base, head = _repo(tmp_path, files, {})
        assert set(_plan(repo, base, head).install) == {"numpy", "pytest"}


@requires_git
class TestIndexRouting:
    """A name the base or the user routes off the default index is gated, never fetched from PyPI."""

    @pytest.mark.integration
    @pytest.mark.parametrize("at_base", [pytest.param(True, id="base"), pytest.param(False, id="head")])
    @pytest.mark.parametrize(
        "files",
        [
            pytest.param({"pyproject.toml": '[tool.tox.env_run_base]\nset_env.PIP_INDEX_URL = "local"\n'}, id="tox"),
            pytest.param({".github/workflows/ci.yml": "env:\n  PIP_CONSTRAINT: inputs.txt\n"}, id="workflow"),
            pytest.param({"requirements/test.txt": "--index-url https://example.invalid/simple\n"}, id="nested-reqs"),
            pytest.param({"sub/Pipfile": '[[source]]\nurl = "https://example.invalid/simple"\n'}, id="nested-pipfile"),
            pytest.param(
                {"Pipfile.lock": '{"_meta": {"sources": [{"url": "https://example.invalid/simple"}]}}'},
                id="pipfile-lock",
            ),
            pytest.param({"poetry.lock": ""}, id="poetry-lock"),
            pytest.param({"pdm.lock": ""}, id="pdm-lock"),
            pytest.param({"pylock.test.toml": "lock-version = '1.0'\n"}, id="named-pylock"),
            pytest.param({"requirements.txt": "https://example.invalid/demo-1.0-py3-none-any.whl\n"}, id="wheel-url"),
            pytest.param({"requirements.txt": "./local-project\n"}, id="local-path"),
            pytest.param({"requirements.txt": "-e ./local-project\n"}, id="editable"),
        ],
    )
    def test_project_installer_inputs_gate_the_default_index(
        self, tmp_path: Path, at_base: bool, files: dict[str, str]
    ) -> None:
        """Routing from either revision applies to all names when its scope cannot be established."""
        ordinary = {"pyproject.toml": pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME))}
        routed = ordinary | {path: ordinary.get(path, "") + text for path, text in files.items()}
        repo, base, head = _repo(tmp_path, routed, ordinary) if at_base else _repo(tmp_path, ordinary, routed)
        plan = _plan(repo, base, head)
        assert (set(plan.install), set(_gated(plan))) == (set(), {_PRIVATE_NAME, _PUBLIC_NAME})

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("\ufeff--index-url https://example.invalid/simple\n", id="utf8-bom"),
            pytest.param('"--index-url" https://example.invalid/simple\n', id="quoted-option"),
            pytest.param('"-r" "nested options.txt"\n', id="quoted-include"),
            pytest.param('--pre "--extra-index-url https://example.invalid/simple\n', id="unclosed-quote"),
            pytest.param("-r ${INCLUDE_PATH}\n", id="variable-include"),
            pytest.param("# coding: latin-1\n", id="unsupported-encoding"),
        ],
    )
    def test_requirements_option_grammar_preserves_routing(self, tmp_path: Path, text: str) -> None:
        """Route quoted and encoded options as pip reads them, and gate uncertain files."""
        files = {
            "pyproject.toml": pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME)),
            "requirements.txt": text,
            "nested options.txt": '--pre "-i" https://example.invalid/simple\n',
        }
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head)
        assert (set(plan.install), set(_gated(plan))) == (set(), {_PRIVATE_NAME, _PUBLIC_NAME})

    def test_requirements_comment_ends_continuation_without_changing_the_name(self, tmp_path: Path) -> None:
        """A continued requirement remains a plain installer argument when the next line is a comment."""
        files = {"requirements.txt": "demo==1 \\\n# pinned\n"}
        repo, base, head = _repo(tmp_path, files, {})
        assert _plan(repo, base, head).install["demo"].texts == ["demo==1"]

    @pytest.mark.parametrize("name", ["PIP_PYPI_URL", "PIP_REQUIREMENT", "PIP_CONSTRAINT", "PIP_BUILD_CONSTRAINT"])
    def test_pip_host_options_gate_the_default_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        """Pip aliases and file selectors are routing even when their values stay private."""
        monkeypatch.setenv(name, "private-value")
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy",))}, {})
        plan = _plan(repo, base, head)
        assert _gated(plan) == {"numpy": "user-index-setting"}
        assert "private-value" not in json.dumps(plan.gated)

    @pytest.mark.parametrize(
        ("path", "settings"),
        [
            # uv reads machine config from XDG_CONFIG_DIRS on POSIX and from ProgramData on Windows.
            pytest.param(
                f"{'ProgramData' if sys.platform == 'win32' else 'system-config'}/uv/uv.toml",
                'index-url = "local"\n',
                id="uv-machine-config",
            ),
            pytest.param(".config/pip/pip.conf", "[install]\nrequirement = input.txt\n", id="pip-requirement"),
            pytest.param(".config/pip/pip.conf", "[global]\nconstraint = input.txt\n", id="pip-constraint"),
        ],
    )
    def test_host_config_sources_gate_the_default_index(self, tmp_path: Path, path: str, settings: str) -> None:
        """Read system uv locations and fail closed on non-neutral pip config keys."""
        _write_host_configs(tmp_path, {path: settings})
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy",))}, {})
        assert _gated(_plan(repo, base, head)) == {"numpy": "user-index-setting"}

    def test_selected_pip_supplies_distributor_config_locations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Consult real interpreter discovery for pip locations outside the standard host paths."""
        prefix = tmp_path / "selected"
        venv.EnvBuilder(symlinks=_can_symlink()).create(prefix)
        python = str(rep.env_python_path(prefix))
        site = Path(subprocess.check_output(
            [python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
        ).strip())  # fmt: skip
        config = tmp_path / "distributor" / "pip.conf"
        _write_host_configs(tmp_path, {"distributor/pip.conf": _PIP_INDEX})
        _write_host_configs(site, {
            "pip/__init__.py": "",
            "pip/_internal/__init__.py": "",
            "pip/_internal/configuration.py": f"def get_configuration_files():\n    return {{'global': [{str(config)!r}]}}\n",
        })  # fmt: skip
        monkeypatch.setattr(rep, "find_installer", lambda: rep.Installer("pip"))
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy",))}, {})
        assert rep.main(["--repo", str(repo), "--base", base, "--head", head, "--python", python, "--plan-only"]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["planned"] == {}
        [entry] = document["skipped_gated"]
        assert (entry["name"], entry["gate_reason"]) == ("numpy", "user-index-setting")

    @pytest.mark.integration
    @pytest.mark.parametrize("at_base", [pytest.param(True, id="base"), pytest.param(False, id="head")])
    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            pytest.param(case["files"], case["expect"], id=case["name"])
            for case in _ROUTING_CASES["cases"]
            if "oss" in case["readers"]
        ],
    )
    def test_project_routing_controls_default_index_plan(
        self, tmp_path: Path, files: dict[str, str | None], expected: str, at_base: bool
    ) -> None:
        """Keep only unrouted names eligible for PyPI whether routing was inherited or introduced."""
        ordinary = {"pyproject.toml": pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME))}
        repo, base, head = _repo(tmp_path, files, {}) if at_base else _repo(tmp_path, ordinary, files)
        plan = _plan(repo, base, head)
        routed = _ROUTED_NAMES[expected]
        assert set(plan.install) == {_PRIVATE_NAME, _PUBLIC_NAME} - routed
        assert _gated(plan) == dict.fromkeys(routed, "index-override")

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("path", "settings", "expected"),
        [
            pytest.param(
                "uv.toml", "managed = false\n[pip]\nrequired-version = '>=0.5'\n", "ordinary", id="uv-allowlist"
            ),
            pytest.param("uv.toml", "[pip]\nunmodeled = false\n", "all-routed", id="uv-pip-unknown-key"),
            pytest.param(
                "pyproject.toml", "[tool.uv.pip]\nunmodeled = []\n", "all-routed", id="project-uv-pip-unknown-key"
            ),
            pytest.param(
                "pyproject.toml", "[tool.uv]\nconstraint-dependencies = []\n", "all-routed", id="uv-constraints"
            ),
            pytest.param(
                "uv.toml", "[[index]]\nexplicit = true\ndefault = true\n", "all-routed", id="explicit-default"
            ),
            pytest.param("uv.toml", "[[index]]\nexplicit = false\n", "all-routed", id="implicit-index"),
            pytest.param("uv.toml", "[index]\nexplicit = true\n", "all-routed", id="invalid-index-table"),
            pytest.param(
                "uv.toml",
                f"index = [{{ name = 'local', explicit = true }}]\n[sources]\n{_PRIVATE_NAME} = {{ index = 'local' }}\n",
                "name-routed",
                id="uv-config-name-scoped",
            ),
            pytest.param(
                "pyproject.toml", "[[tool.pdm.resolution.mirrors]]\nbase_url = 'local'\n", "all-routed", id="nested-url"
            ),
            pytest.param(
                "pyproject.toml", "[tool.rye.sync]\nfind_links = ['local']\n", "all-routed", id="nested-find-links"
            ),
            pytest.param(
                "pyproject.toml", "[tool.hatch.version]\n[tool.hatch.envs]\n", "all-routed", id="hatch-mixed-tables"
            ),
            pytest.param(
                "pyproject.toml", "[tool.pdm.version]\n[tool.pdm.scripts]\n", "all-routed", id="pdm-mixed-tables"
            ),
            pytest.param("pdm.toml", "", "all-routed", id="pdm-config-presence"),
            pytest.param(".pdm.toml", "", "all-routed", id="dot-pdm-config-presence"),
            pytest.param("pixi.toml", "", "all-routed", id="pixi-config-presence"),
            pytest.param("poetry.toml", "", "all-routed", id="poetry-config-presence"),
            pytest.param("pip.ini", "[global]\n--INDEX_URL: local\n", "all-routed", id="pip-ini-option-normalization"),
            pytest.param(
                "pip.conf", "[global]\ntimeout = 30\n# index-url = local\n", "ordinary", id="pip-conf-comment"
            ),
            pytest.param(
                "Pipfile", "[packages]\ndemo = { index = 'local' }\n", "all-routed", id="pipfile-package-index"
            ),
            pytest.param(
                "Pipfile", "[packages]\nurllib3 = '*'\n# [[source]]\n", "ordinary", id="pipfile-ordinary-package"
            ),
            pytest.param(
                "Pipfile", "[docs]\ndemo = { path = './demo' }\n", "all-routed", id="pipfile-custom-group-source"
            ),
            pytest.param("Pipfile", "[packages]\ndemo = { url = 'local' }\n", "all-routed", id="pipfile-package-url"),
        ],
    )
    def test_unmodeled_routing_and_harmless_settings(
        self, tmp_path: Path, path: str, settings: str, expected: str
    ) -> None:
        """Accept only modeled uv options and distinguish actual installer keys from comments."""
        root = pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME))
        files = {"pyproject.toml": root, path: root + settings if path == "pyproject.toml" else settings}
        repo, base, head = _repo(tmp_path, files, {})
        assert set(_plan(repo, base, head).install) == {_PRIVATE_NAME, _PUBLIC_NAME} - _ROUTED_NAMES[expected]

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("path", "text"),
        [
            pytest.param("pyproject.toml", "[tool.uv\n", id="pyproject"),
            pytest.param("uv.toml", "[pip\n", id="uv-config"),
            pytest.param("pip.conf", "invalid ini\n", id="pip-conf"),
            pytest.param("pip.ini", "invalid ini\n", id="pip-ini"),
            pytest.param("Pipfile", "[[source\n", id="pipfile"),
            pytest.param("Pipfile.lock", "{", id="pipfile-lock"),
            pytest.param("uv.lock", "[[package\n", id="uv-lock"),
            pytest.param("uv.lock", "[package]\nname = 'requests'\n", id="uv-lock-package-table"),
            pytest.param(
                "uv.lock",
                "[[package]]\nsource = { registry = 'https://private.invalid/simple' }\n",
                id="uv-lock-unnamed-package",
            ),
            pytest.param("setup.cfg", "invalid ini\n", id="setup-cfg"),
            pytest.param("requirements-extra.txt", "-r absent.txt\n", id="missing-include"),
        ],
    )
    def test_unreadable_installer_input_gates_requirements_from_other_files(
        self, tmp_path: Path, path: str, text: str
    ) -> None:
        """Fail closed even when all requirements are available from another manifest."""
        files = {"requirements.txt": f"{_PRIVATE_NAME}\n{_PUBLIC_NAME}\n", path: text}
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head)
        assert plan.install == {}
        assert _gated(plan) == dict.fromkeys((_PRIVATE_NAME, _PUBLIC_NAME), "index-routing-undetermined")

    @pytest.mark.integration
    @pytest.mark.parametrize("at_base", [pytest.param(True, id="base"), pytest.param(False, id="head")])
    @pytest.mark.parametrize(
        ("source", "routes"),
        [
            pytest.param('source = { registry = "https://pypi.org/simple/" }', False, id="pypi"),
            pytest.param('source = { registry = "https://private.invalid/simple" }', True, id="registry"),
            pytest.param('source = { git = "https://example.invalid/repo#abc" }', True, id="git"),
            pytest.param('source = { url = "https://example.invalid/dist.whl" }', True, id="url"),
            pytest.param('source = { path = "./dist.whl" }', True, id="path"),
            pytest.param('source = { editable = "./packages/member" }', True, id="editable"),
            pytest.param('source = { directory = "./packages/member" }', True, id="directory"),
            pytest.param('source = { virtual = "./packages/member" }', True, id="virtual"),
            pytest.param("", True, id="missing-source"),
            pytest.param(
                'source = { registry = "https://pypi.org/simple", path = "./dist.whl" }', True, id="mixed-source"
            ),
        ],
    )
    def test_uv_lock_sources_control_each_named_requirement(
        self, tmp_path: Path, source: str, routes: bool, at_base: bool
    ) -> None:
        """Use PyPI only for a positively recognized registry source and keep routing scoped to the locked name."""
        ordinary = {"pyproject.toml": pyproject(name="demo", deps=(_PRIVATE_NAME, _PUBLIC_NAME))}
        locked = {
            "uv.lock": f'version = 1\n[[package]]\nname = "demo"\nsource = {{ virtual = "." }}\n'
            f'[[package]]\nname = "{_PRIVATE_NAME}"\n{source}\n'
        }
        repo, base, head = _repo(tmp_path, ordinary | locked, {}) if at_base else _repo(tmp_path, ordinary, locked)
        plan = _plan(repo, base, head)
        assert set(plan.install) == ({_PUBLIC_NAME} if routes else {_PRIVATE_NAME, _PUBLIC_NAME})
        assert _gated(plan) == ({_PRIVATE_NAME: "index-override"} if routes else {})

    @pytest.mark.integration
    @pytest.mark.parametrize("source_path", ["pyproject.toml", "packages/member/pyproject.toml"])
    def test_workspace_pyprojects_keep_sources_name_scoped(self, tmp_path: Path, source_path: str) -> None:
        """A uv source in either manifest gates its dependency without gating the rest of the workspace."""
        manifests = {
            "pyproject.toml": pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME)),
            "packages/member/pyproject.toml": pyproject(name="member", deps=("workspace-only",)),
        }
        source = f'[tool.uv.sources]\n{_PRIVATE_NAME} = {{ path = "local" }}\n'
        repo, base, head = _repo(tmp_path, manifests, {source_path: manifests[source_path] + source})
        plan = _plan(repo, base, head)
        assert set(plan.install) == {_PUBLIC_NAME}
        assert _gated(plan) == {_PRIVATE_NAME: "index-override"}

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("path", "manifest"),
        [
            pytest.param(
                "packages/member/pyproject.toml",
                pyproject(name="member", deps=(f"{_PRIVATE_NAME} @ ./local.whl",)),
                id="workspace-dependency",
            ),
            pytest.param(
                "packages/member/pyproject.toml",
                f'[build-system]\nrequires = ["{_PRIVATE_NAME} @ ./local.whl"]\n',
                id="workspace-build-requirement",
            ),
            pytest.param(
                "packages/member/setup.cfg",
                f"[options]\ninstall_requires =\n    {_PRIVATE_NAME} @ ./local.whl\n",
                id="workspace-setup-config",
            ),
        ],
    )
    def test_inherited_workspace_direct_source_gates_matching_root_name(
        self, tmp_path: Path, path: str, manifest: str
    ) -> None:
        """Carry unchanged workspace source routing forward when the root selects the same distribution."""
        project = pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME))
        repo, base, head = _repo(
            tmp_path, {"pyproject.toml": project, path: manifest}, {"other.txt": "unrelated change"}
        )
        plan = _plan(repo, base, head)
        assert set(plan.install) == {_PUBLIC_NAME}
        assert _gated(plan) == {_PRIVATE_NAME: "direct-reference"}

    @pytest.mark.integration
    def test_constraint_file_routes_direct_reference_without_selecting_constraints(self, tmp_path: Path) -> None:
        """An included constraint can route a selected name and does not add install candidates."""
        files = {
            "requirements.txt": f"-c constraints.txt\n{_PRIVATE_NAME}\n{_PUBLIC_NAME}\n",
            "constraints.txt": f"{_PRIVATE_NAME} @ ./local.whl\nconstraint-only==1\n",
        }
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head)
        assert set(plan.install) == {_PUBLIC_NAME}
        assert _gated(plan) == {_PRIVATE_NAME: "index-override"}

    def test_uv_source_routes_only_that_name(self, tmp_path: Path) -> None:
        """An explicit index used through ``[tool.uv.sources]`` gates the routed name and nothing else.

        This is the CPU-torch pattern: torch comes from another index, numpy from the default one.
        """
        tail = ('[tool.uv.sources]\ntorch = { index = "pytorch" }\n[[tool.uv.index]]\nname = "pytorch"\n'
                'url = "https://example.invalid/whl"\nexplicit = true\n')  # fmt: skip
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("torch", "numpy"), tail=tail)}, {})
        plan = _plan(repo, base, head)
        assert (list(plan.install), _gated(plan)) == (["numpy"], {"torch": "index-override"})

    def test_requirements_index_option_gates_every_name(self, tmp_path: Path) -> None:
        """An ``--extra-index-url`` line applies to every name, so none is fetched from the default index.

        Any name could resolve from the extra index, so a same-name package on PyPI is not proven to be the one meant.
        """
        files = {"pyproject.toml": pyproject(deps=("numpy",)),
                 "requirements.txt": "--extra-index-url https://example.invalid/simple\nscipy\n"}  # fmt: skip
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head)
        assert (plan.install, set(_gated(plan).values())) == ({}, {"index-override"})

    @pytest.mark.integration
    @pytest.mark.parametrize("at_base", [pytest.param(True, id="base"), pytest.param(False, id="head")])
    @pytest.mark.parametrize(
        ("path", "settings"),
        [
            pytest.param(
                ".github/workflows/ci.yml",
                "run: pip install --index-url https://user:secret@example.invalid/simple\n",
                id="workflow",
            ),
            pytest.param(
                "tox.ini", "[testenv]\nset_env = PIP_INDEX_URL=https://user:secret@example.invalid/simple\n", id="tox"
            ),
            pytest.param("Dockerfile", "RUN pip install -f./wheels requests\n", id="dockerfile"),
            pytest.param(
                "scripts/install.py", "options = ['--default-index=https://example.invalid/simple']\n", id="script"
            ),
        ],
    )
    def test_unparsed_project_routing_gates_every_name(
        self, tmp_path: Path, path: str, settings: str, at_base: bool
    ) -> None:
        """Treat unscoped routing as unresolved for every name and omit the setting's value."""
        ordinary = {"pyproject.toml": pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME))}
        routed = {path: settings}
        repo, base, head = _repo(tmp_path, ordinary | routed, {}) if at_base else _repo(tmp_path, ordinary, routed)
        plan = _plan(repo, base, head)
        assert plan.install == {}
        assert _gated(plan) == dict.fromkeys((_PRIVATE_NAME, _PUBLIC_NAME), "index-routing-undetermined")
        assert any(note.startswith(f"content scan: {path}:") for note in plan.notes)
        assert "secret" not in json.dumps((plan.gated, plan.notes))

    def test_repository_with_multiple_unparsed_routing_hits_gates_all_names(self, tmp_path: Path) -> None:
        """A CI command and a notebook command leave the affected names undetermined."""
        deps = ("numpy", "scipy", "pillow", "pyyaml", "requests", "tqdm", "matplotlib", "pydantic")
        files = {
            "pyproject.toml": pyproject(deps=deps),
            ".github/workflows/legacy.yml": "run: pip install torch --extra-index-url https://example.invalid/cpu\n",
            ".github/workflows/metrics.yml": "run: |\n  gh api repos/o/r/pulls \\\n    -f state=open\n",
            "docs/cookbook.ipynb": '{"cells": [{"source": ["!pip install x --extra-index-url https://example.invalid/"]}]}',
            "tests/legacy/frozen/req-1.4.txt": "# Installed with CPU-only torch (--extra-index-url ...)\nnumpy==1.26.4\n",
        }  # fmt: skip
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head)
        assert (plan.install, _gated(plan)) == ({}, dict.fromkeys(deps, "index-routing-undetermined"))
        assert any(note.startswith("content scan: docs/cookbook.ipynb:") for note in plan.notes)

    def test_pr_added_name_stays_gated_beside_scan_hits(self, tmp_path: Path) -> None:
        """Keep the PR-added reason for new names even when project routing is unresolved."""
        files = {
            "pyproject.toml": pyproject(deps=("numpy",)),
            ".github/workflows/ci.yml": "run: pip install --extra-index-url https://example.invalid/simple x\n",
        }
        repo, base, head = _repo(tmp_path, files, {"pyproject.toml": pyproject(deps=("numpy", "brand-new-pkg"))})
        plan = _plan(repo, base, head)  # fmt: skip
        assert (plan.install, _gated(plan)) == (
            {},
            {"numpy": "index-routing-undetermined", "brand-new-pkg": "added-by-pr"},
        )

    def test_base_direct_reference_behind_a_plain_spec_stays_gated(self, tmp_path: Path) -> None:
        """A name the base points at a git URL in one manifest is not base-declared, whatever another manifest says.

        Only a name whose every base declaration is a plain official-index spec counts as base-declared, so the name-
        collapse case (a direct reference hidden behind a plain spec elsewhere) never becomes installable.
        """
        files = {
            "pyproject.toml": pyproject(deps=("foo>=1", "numpy")),
            "docs/requirements.txt": "foo @ git+https://example.invalid/foo.git\n",
        }
        repo, base, head = _repo(tmp_path, files, {})  # fmt: skip
        plan = _plan(repo, base, head)
        assert (list(plan.install), _gated(plan)) == (["numpy"], {"foo": "direct-reference"})

    @pytest.mark.parametrize(
        "name", ["PIP_INDEX_URL", "PIP_FIND_LINKS", "PIP_CONFIG_FILE", "UV_INDEX", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS",
                 "UV_CONFIG_FILE"]
    )  # fmt: skip
    def test_user_index_setting_gates_and_hides_its_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        """A user index is never stripped to reach PyPI, and its value is never shown.

        The URL may carry credentials, so only the setting name appears in the report.
        """
        monkeypatch.setenv(name, "https://user:secret@example.invalid/simple")
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy",))}, {})
        plan = _plan(repo, base, head)
        assert (_gated(plan), "secret" in json.dumps(plan.gated)) == ({"numpy": "user-index-setting"}, False)

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("env", "home_files", "files", "expected"),
        [
            pytest.param(case["env"], case["home_files"], case["files"], case["expect"], id=case["name"])
            for case in _ROUTING_CASES["host_cases"]
            if "oss" in case["readers"]
        ],
    )
    def test_host_routing_controls_default_index_plan(
        self,
        tmp_path: Path,
        env: dict[str, str],
        home_files: dict[str, str],
        files: dict[str, str],
        expected: str,
    ) -> None:
        """Apply host environment and config routing to every selected name."""
        _write_host_configs(tmp_path, home_files)
        repo, base, head = _repo(tmp_path, files, {})
        with patch.dict(os.environ, env):
            plan = _plan(repo, base, head)
        routed = _ROUTED_NAMES[expected]
        assert set(plan.install) == {_PRIVATE_NAME, _PUBLIC_NAME} - routed
        assert _gated(plan) == dict.fromkeys(routed, "user-index-setting")

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("system", "path", "settings"),
        [
            pytest.param("Windows", "pip/pip.ini", _PIP_INDEX, id="windows-legacy-pip"),
            pytest.param("Windows", "ProgramData/pip/pip.ini", _PIP_INDEX, id="windows-system-pip"),
            pytest.param("Darwin", "user-data/pip/pip.conf", _PIP_INDEX, id="macos-user-pip"),
            pytest.param("Darwin", "system-data/pip/pip.conf", _PIP_INDEX, id="macos-system-pip"),
            pytest.param(
                "Darwin", ".config/uv/uv.toml", "[[index]]\n" + _EXPLICIT_UV_INDEX, id="host-uv-explicit-index"
            ),
            pytest.param(
                "Darwin", ".config/uv/uv.toml", "[[pip.index]]\n" + _EXPLICIT_UV_INDEX, id="host-uv-pip-explicit-index"
            ),
        ],
    )
    def test_host_installer_config_gates_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, path: str, settings: str
    ) -> None:
        """Read OS-specific configs and gate all names for indexes declared at host scope."""
        monkeypatch.setattr(rep.platform, "system", lambda: system)
        _write_host_configs(tmp_path, {path: settings})
        project = pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME))
        repo, base, head = _repo(tmp_path, {"pyproject.toml": project}, {})
        plan = _plan(repo, base, head)
        assert plan.install == {}
        assert _gated(plan) == dict.fromkeys((_PRIVATE_NAME, _PUBLIC_NAME), "user-index-setting")

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("scope", "settings", "gates"),
        [
            pytest.param(("Linux", "pip.conf"), _PIP_INDEX, True, id="posix-site"),
            pytest.param(("Windows", "pip.ini"), "[install]\nfind-links = ./wheels\n", True, id="windows-site"),
            pytest.param(("Linux", "pip.conf"), "[global]\ntimeout = 30\n", False, id="ordinary-site"),
        ],
    )
    def test_selected_python_site_config_controls_default_index_plan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        scope: tuple[str, str],
        settings: str,
        gates: bool,
    ) -> None:
        """Inspect the selected virtual environment's own pip site scope through the CLI."""
        system, filename = scope
        prefix = tmp_path / "selected"
        venv.EnvBuilder(symlinks=_can_symlink()).create(prefix)
        _write_host_configs(prefix, {filename: settings})
        python = str(rep.env_python_path(prefix))
        site = Path(
            subprocess.check_output(
                [python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
            ).strip()
        )
        marker = tmp_path / "site-started"
        (site / "probe.pth").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n", encoding="utf-8")
        monkeypatch.setattr(rep.platform, "system", lambda: system)
        monkeypatch.setattr(rep, "find_installer", lambda: rep.Installer("pip"))
        monkeypatch.setattr(rep, "host_spec", dict)
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME))}, {})
        argv = ["--repo", str(repo), "--base", base, "--head", head, "--plan-only"]

        assert rep.main([*argv, "--python", python]) == 0
        document = json.loads(capsys.readouterr().out)

        assert not marker.exists()
        names = {_PRIVATE_NAME, _PUBLIC_NAME}
        assert set(document["planned"]) == (set() if gates else names)
        assert {entry["name"]: entry["gate_reason"] for entry in document["skipped_gated"]} == (
            dict.fromkeys(names, "user-index-setting") if gates else {}
        )

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("found", "reason"),
        [
            pytest.param(True, "user-index-setting", id="local-match"),
            pytest.param(False, "index-routing-undetermined", id="no-local-match"),
        ],
    )
    def test_uv_python_request_requires_known_site_routing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        found: bool,
        reason: str,
    ) -> None:
        """Use uv's offline interpreter match and gate the plan when no local interpreter can be checked."""
        prefix = tmp_path / "selected"
        venv.EnvBuilder(symlinks=_can_symlink()).create(prefix)
        _write_host_configs(prefix, {"pip.ini" if os.name == "nt" else "pip.conf": _PIP_INDEX})
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=(_PRIVATE_NAME, _PUBLIC_NAME))}, {})
        monkeypatch.setattr(rep, "find_installer", lambda: rep.Installer("uv", "fake-uv"))
        monkeypatch.setattr(rep, "host_spec", dict)
        run_process = rep.run_process

        def run(argv: list[str], env: dict[str, str], cwd: Path, timeout: int) -> rep.RunResult:
            """Replace uv discovery while running the selected Python for real."""
            if argv[0] != "fake-uv":
                return run_process(argv, env, cwd, timeout)
            assert argv[1:] == [
                "--offline",
                "--no-config",
                "--no-python-downloads",
                "python",
                "find",
                "--no-project",
                "--",
                "3",
            ]
            return rep.RunResult(0, str(rep.env_python_path(prefix))) if found else rep.RunResult(1, "no match")

        monkeypatch.setattr(rep, "run_process", run)
        argv = ["--repo", str(repo), "--base", base, "--head", head, "--plan-only", "--python", "3"]
        assert rep.main(argv) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["planned"] == {}
        assert {entry["name"]: entry["gate_reason"] for entry in document["skipped_gated"]} == (
            dict.fromkeys((_PRIVATE_NAME, _PUBLIC_NAME), reason)
        )

    @pytest.mark.integration
    @pytest.mark.parametrize("mode", [pytest.param([], id="apply"), pytest.param(["--plan-only"], id="plan")])
    def test_review_tree_python_is_refused_before_environment_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: list[str]
    ) -> None:
        """An explicit interpreter inside the reviewed tree cannot participate in planning or provisioning."""
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=(_PUBLIC_NAME,))}, {})
        prefix = repo / ".venv"
        venv.EnvBuilder(symlinks=_can_symlink()).create(prefix)
        monkeypatch.setattr(rep, "find_installer", lambda: rep.Installer("pip"))
        monkeypatch.setattr(rep, "host_spec", dict)
        argv = ["--repo", str(repo), "--base", base, "--head", head, "--python", str(rep.env_python_path(prefix))]
        assert rep.main([*argv, *mode]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["planned"] == {}
        assert document["env_python"] is None
        assert document["skipped_gated"][0]["gate_reason"] == "index-routing-undetermined"
        assert not Path(document["env_dir"]).exists()

    @pytest.mark.parametrize(
        "option",
        [
            pytest.param("-ihttps://private.invalid/simple", id="short-attached"),
            pytest.param("--index=https://private.invalid/simple", id="long-prefix"),
            pytest.param("-f./wheels", id="find-links-attached"),
            pytest.param("--frobnicate https://private.invalid/simple", id="unknown-option"),
        ],
    )
    def test_index_option_in_any_spelling_gates_every_name(self, tmp_path: Path, option: str) -> None:
        """An index option pip reads in any spelling, or an option it does not model, keeps names off PyPI.

        ``-iURL`` routes ``internal-pkg`` to a private index exactly like ``-i URL``; fetching the same name from PyPI
        instead would install whatever a squatter published there.
        """
        files = {"pyproject.toml": pyproject(deps=("numpy",)), "requirements.txt": f"{option}\ninternal-pkg==1.0\n"}
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head)
        assert (plan.install, _gated(plan)) == ({}, {"numpy": "index-override", "internal-pkg": "index-override"})

    def test_attached_include_is_followed(self, tmp_path: Path) -> None:
        """``-rfile`` reads the included file like ``-r file``, so its routing and requirements are not lost."""
        files = {
            "pyproject.toml": pyproject(deps=("numpy",)),
            "requirements.txt": "-rshared.txt\n",
            "shared.txt": "-ihttps://private.invalid/simple\nscipy\n",
        }
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head)
        assert (plan.install, _gated(plan)) == ({}, {"numpy": "index-override", "scipy": "index-override"})

    @pytest.mark.parametrize("name", ["UV_OVERRIDE", "UV_TORCH_BACKEND"])
    def test_unlisted_uv_environment_override_gates_every_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
    ) -> None:
        """A ``UV_`` variable outside the known-harmless list gates every name.

        Such variables change resolution in ways a fixed list cannot know, and are never unset to force a run.
        """
        monkeypatch.setenv(name, "local")
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy",))}, {})
        assert _gated(_plan(repo, base, head)) == {"numpy": "uv-environment-override"}


@requires_git
class TestSelection:
    """Runtime, extras, test groups and test-imported optional packages; never the project itself."""

    def test_self_reference_expands_and_the_project_is_never_installed(self, tmp_path: Path) -> None:
        """``demo[coco]`` inside ``demo`` plans the extra's requirements, not the released ``demo`` package.

        Installing the released project would shadow the PR's source tree the review must run.
        """
        text = pyproject(name="demo", extras={"coco": ["pycocotools>=2"], "all": ["demo[coco]"]})
        repo, base, head = _repo(tmp_path, {"pyproject.toml": text}, {})
        plan = _plan(repo, base, head)
        assert (list(plan.install), _skips(plan)) == (["pycocotools"], {"demo": "project-itself"})

    def test_test_import_selects_the_declared_headless_opencv_wheel(self, tmp_path: Path) -> None:
        """Tests importing cv2 get the declared headless wheel from a group the default selection leaves out.

        Both OpenCV wheels are declared, each in its own conflicting group; only the headless one is chosen.
        """
        files = {"pyproject.toml": pyproject(deps=("numpy",), tail=_OPENCV_GROUPS), "tests/test_io.py": "import cv2\n"}
        repo, base, head = _repo(tmp_path, files, {})
        assert set(_plan(repo, base, head).install) == {"numpy", "opencv-python-headless"}

    def test_two_opencv_wheels_never_install_together(self, tmp_path: Path) -> None:
        """With every group selected, the GUI OpenCV wheel is skipped as a conflicting provider of ``cv2``.

        Both wheels write the same ``cv2`` package, so installing both leaves whichever came last.
        """
        files = {"pyproject.toml": pyproject(tail=_OPENCV_GROUPS)}
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head, rep.Selection(all_groups=True))
        assert (list(plan.install), _skips(plan)) == (
            ["opencv-python-headless"], {"opencv-python": "conflicting-provider"}
        )  # fmt: skip

    def test_undeclared_optional_import_is_named_not_installed(self, tmp_path: Path) -> None:
        """A commonly missing module no manifest declares is reported, never fetched on the reviewer's initiative.

        The trust model needs a base declaration; an import alone does not name a distribution with certainty.
        """
        files = {"pyproject.toml": pyproject(deps=("numpy",)), "tests/test_coco.py": "import pycocotools.coco\n"}
        repo, base, head = _repo(tmp_path, files, {})
        plan = _plan(repo, base, head)
        assert (list(plan.install), _skips(plan)) == (["numpy"], {"pycocotools": "undeclared-import"})

    def test_pytest_brings_declared_plugins(self, tmp_path: Path) -> None:
        """Tests import pytest; declared ``pytest-*`` plugins from any group come with it.

        Plugins load by entry point, never by import, so the import scan alone would miss them.
        """
        tail = '[dependency-groups]\ntest = ["pytest"]\nextra-tools = ["pytest-mock", "black"]\n'
        files = {"pyproject.toml": pyproject(tail=tail), "tests/test_a.py": "import pytest\n"}
        repo, base, head = _repo(tmp_path, files, {})
        assert set(_plan(repo, base, head).install) == {"pytest", "pytest-mock"}


class _FakeInstaller:
    """Stand-in for uv and the environment interpreter: records every process and installs into a dict."""

    def __init__(self) -> None:
        self.fail: dict[str, str] = {}
        self.calls: list[tuple[list[str], Path]] = []
        self.installed: dict[str, str] = {}
        self.batch_error = ""
        self.version = "3.12.1"
        self.base_executable = Path(sys.executable)

    def run(self, argv: list[str], env: dict[str, str], cwd: Path, timeout: int) -> rep.RunResult:
        """Create the environment interpreter or install requirement strings, failing names listed in ``fail``."""
        if "-S" in argv:
            prefix = cwd if argv[0] == str(rep.env_python_path(cwd)) else Path(sys.prefix)
            identity = [self.version, str(prefix), str(rep.env_python_path(prefix)), str(self.base_executable)]
            return rep.RunResult(0, json.dumps(identity))
        self.calls.append((list(argv), Path(cwd)))
        if "venv" in argv:
            python = rep.env_python_path(Path(argv[-1]))
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
            return rep.RunResult(0, "")
        names = [rep.parse_requirement(r, "test").name for r in argv[argv.index("--cache-dir") + 2 :]]  # type: ignore[union-attr]
        if self.batch_error and len(names) > 1:
            return rep.RunResult(1, self.batch_error)
        failing = [self.fail[n] for n in names if n in self.fail]
        if failing:
            return rep.RunResult(1, failing[0])
        self.installed.update(dict.fromkeys(names, "1.0"))
        return rep.RunResult(0, "")

    def probe(self, env_python: Path, cwd: Path) -> dict[str, object] | None:
        """Answer the interpreter probe with fixed markers and whatever was installed so far."""
        if not env_python.exists():
            return None
        return {"markers": {"python_version": "3.12", "sys_platform": "linux"}, "distributions": dict(self.installed),
                "base_prefix": "/base"}  # fmt: skip

    def installs(self) -> list[list[str]]:
        """Return every install argv."""
        return [argv for argv, _ in self.calls if "install" in argv]


def _install_plan(*items: tuple[str, str]) -> rep.Plan:
    """Build a plan that installs ``(name, requirement)`` pairs."""
    plan = rep.Plan()
    plan.install.update({name: rep.Planned(name, [text], ["runtime"], [""]) for name, text in items})
    return plan


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeInstaller:
    """Route every installer and interpreter process through the fake."""
    installer = _FakeInstaller()
    monkeypatch.setattr(rep, "run_process", installer.run)
    monkeypatch.setattr(rep, "probe_env", installer.probe)
    return installer


@pytest.fixture
def request_dir(tmp_path: Path) -> rep.ProvisionRequest:
    """Provision into a fresh environment directory under a private temp root."""
    return rep.ProvisionRequest(tmp_path / "root" / "repo-key" / "head", "python3", 60, 5)


_UV = rep.Installer("uv", "uv")
_OFFLINE = "error: Failed to fetch: `https://pypi.org/simple/numpy/`\n  Caused by: dns error"
_NO_WHEEL = "x has no wheels with a matching platform tag (e.g. `macosx_14_0_arm64`)"


class TestProvisioning:
    """Install never blocks, never touches the reviewed tree, and reuses a complete environment."""

    @pytest.mark.parametrize("text", ["other>=1", "demo suffix", "demo @ https://example.invalid/demo.whl"])
    def test_installer_rejects_arguments_outside_the_approved_name(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest, text: str
    ) -> None:
        """Check the final argument against the approved distribution before invoking the installer."""
        outcome = rep.provision(_install_plan(("demo", text)), _UV, request_dir)
        assert (fake.installs(), [failure["cause"] for failure in outcome.failed]) == ([], ["gated"])

    @pytest.mark.parametrize("change", ["version", "base_executable"])
    def test_rebound_interpreter_invalidates_completed_plan(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest, tmp_path: Path, change: str
    ) -> None:
        """Re-probe the same launcher and recreate its environment if the selected interpreter has changed."""
        alternate = tmp_path / "alternate-python"
        alternate.touch()
        plan = _install_plan(("demo", "demo"))
        first = rep.provision(plan, _UV, request_dir)
        setattr(fake, change, "3.13.1" if change == "version" else alternate)
        second = rep.provision(plan, _UV, request_dir)
        state = json.loads((request_dir.env_dir / "provision-state.json").read_text(encoding="utf-8"))
        assert (first.reused, second.reused, len(fake.installs())) == (False, False, 2)
        assert (state["python"]["version"], state["python"]["base_executable"]) == (
            fake.version, fake.base_executable.resolve().as_posix()
        )  # fmt: skip

    def test_individual_success_after_joint_failure_is_partial_and_retries(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest
    ) -> None:
        """A failed joint resolution never becomes ready or a reusable completed receipt."""
        plan = _install_plan(("demo", "demo"), ("example", "example"))
        fake.batch_error = "ResolutionImpossible"
        first = rep.provision(plan, _UV, request_dir)
        document = rep.build_document(request_dir.env_dir, "base", "head", plan, first, {})
        fake.batch_error = ""
        second = rep.provision(plan, _UV, request_dir)
        assert (document["status"], first.failed, len(first.installed)) == ("partial", [], 2)
        assert (second.reused, second.batch_failure, len(fake.installs())) == (False, None, 4)

    @requires_git
    def test_installer_never_names_or_runs_in_the_worktree(
        self, tmp_path: Path, fake: _FakeInstaller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No install argument names the PR worktree and no process runs from it.

        The installer reads configuration from its working directory; running it in the PR tree would apply PR-
        controlled ``uv.toml`` or ``pyproject.toml`` settings outside the gate.
        """
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy",))}, {})
        monkeypatch.setattr(rep, "find_installer", lambda: _UV)
        rep.main(["--repo", str(repo), "--base", base, "--head", head, "--out", str(tmp_path / "o.json")])
        install = fake.installs()[0]
        assert not any(str(repo.resolve()) in arg for argv, _ in fake.calls for arg in argv)
        assert not any(rep.is_inside(cwd, repo) for _, cwd in fake.calls)
        assert ("--no-config" in install, install[install.index("--default-index") + 1]) == (True, rep.DEFAULT_INDEX)

    def test_network_failure_is_a_cause_not_a_stop(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest
    ) -> None:
        """An offline host reports every name with cause ``network`` and is not retried name by name.

        Every name would fail the same way, so one batch attempt is the whole evidence; the environment still exists.
        """
        fake.fail = {"numpy": _OFFLINE}
        outcome = rep.provision(_install_plan(("numpy", "numpy"), ("scipy", "scipy")), _UV, request_dir)
        assert ({f["name"]: f["cause"] for f in outcome.failed}, len(fake.installs()), bool(outcome.env_python)) == (
            {"numpy": "network", "scipy": "network"}, 1, True
        )  # fmt: skip

    def test_joint_failure_retries_each_name_for_its_own_cause(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest
    ) -> None:
        """When the batch fails for one name, the others still install and only that name carries the cause.

        The joint failure is kept separately, so the report can show why the batch did not resolve.
        """
        fake.fail = {"badwheel": _NO_WHEEL}
        outcome = rep.provision(_install_plan(("numpy", "numpy"), ("badwheel", "badwheel")), _UV, request_dir)
        assert [(f["name"], f["cause"]) for f in outcome.failed] == [("badwheel", "unsupported-platform")]
        assert [r["name"] for r in outcome.installed] == ["numpy"]
        assert outcome.batch_failure == {"cause": "unsupported-platform", "error_tail": _NO_WHEEL}

    def test_complete_environment_is_reused_without_installing(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest
    ) -> None:
        """The same head and plan reuse a complete environment: the second run starts no installer.

        Provision-then-reuse is one contract; the first call is the arrange step.
        """
        plan = _install_plan(("numpy", "numpy"))
        rep.provision(plan, _UV, request_dir)
        calls_before = len(fake.calls)
        second = rep.provision(plan, _UV, request_dir)
        assert (second.reused, len(fake.calls), [r["name"] for r in second.installed]) == (
            True,
            calls_before,
            ["numpy"],
        )

    def test_incomplete_environment_installs_again_in_place(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest
    ) -> None:
        """A failed earlier run is retried into the same environment, never remembered as final.

        A transient network failure must not pin the environment as incomplete for every later review of the head.
        """
        plan = _install_plan(("numpy", "numpy"))
        fake.fail = {"numpy": _OFFLINE}
        rep.provision(plan, _UV, request_dir)
        fake.fail = {}
        second = rep.provision(plan, _UV, request_dir)
        created = sum("venv" in argv for argv, _ in fake.calls)
        assert (second.reused, created, [r["name"] for r in second.installed]) == (False, 1, ["numpy"])

    def test_marker_that_excludes_this_environment_is_skipped(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest
    ) -> None:
        """A requirement for another platform is skipped with its marker, not reported as an install failure.

        The marker is evaluated against the environment's own interpreter, so no installer call is spent on it.
        """
        plan = rep.Plan()
        plan.install["pywin32"] = rep.Planned("pywin32", ["pywin32; sys_platform == 'win32'"], ["runtime"],
                                              ["sys_platform == 'win32'"])  # fmt: skip
        outcome = rep.provision(plan, _UV, request_dir)
        assert (outcome.failed, fake.installs(), _skips(plan)) == ([], [], {"pywin32": "marker-excludes-host"})

    @requires_git
    def test_environment_location_is_outside_the_repository_and_keyed_by_head(self, tmp_path: Path) -> None:
        """Same repository and head always map to one directory under the temp root, never inside the tree.

        A different head of the same repository gets its own environment.
        """
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject()}, {"README.md": "x"})
        root = rep.env_root()
        first, again, other = (rep.env_dir_for(repo, head, root), rep.env_dir_for(repo, head, root),
                               rep.env_dir_for(repo, base, root))  # fmt: skip
        assert (first == again, first == other, rep.is_inside(first, root), rep.is_inside(first, repo)) == (
            True, False, True, False
        )  # fmt: skip

    def test_file_system_error_is_a_cause_not_a_traceback(
        self, fake: _FakeInstaller, request_dir: rep.ProvisionRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unwritable temp root or a full disk becomes ``env-create-failed`` in the document.

        Without the boundary the caller would get a traceback and no document, so the cause would be lost.
        """

        def full_disk(*_args: object, **_kwargs: object) -> rep.RunResult:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(rep, "_create_env", full_disk)
        outcome = rep.provision(_install_plan(("numpy", "numpy")), _UV, request_dir)
        assert [(f["cause"], f["error_tail"]) for f in outcome.failed] == [
            ("env-create-failed", "OSError: [Errno 28] No space left on device")
        ]

    def test_exhausted_budget_reports_pending_names_as_timeout(
        self, fake: _FakeInstaller, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Names the overall budget left no time for are reported with cause ``timeout``, and no installer runs late.

        The caller's own time cap would otherwise kill the run mid-install and leave no document at all.
        """
        clock = iter([0.0, 0.0, 0.0])  # budget start, lock wait start, environment creation; then time has run out
        monkeypatch.setattr(rep.time, "monotonic", lambda: next(clock, 100.0))
        request = rep.ProvisionRequest(tmp_path / "root" / "k" / "h", "python3", 60, 5, (), 50)
        outcome = rep.provision(_install_plan(("numpy", "numpy"), ("scipy", "scipy")), _UV, request)
        assert ({f["name"]: f["cause"] for f in outcome.failed}, fake.installs()) == (
            {"numpy": "timeout", "scipy": "timeout"}, []
        )  # fmt: skip

    def test_lock_file_left_by_an_ended_run_is_taken_at_once(self, tmp_path: Path) -> None:
        """A lock file no live process holds is taken without waiting, whatever it contains.

        A review block killed by its time cap must not block the next provisioning of the same head: the operating
        system releases the lock with the process, and the file itself is never the lock.
        """
        env_dir = tmp_path / "root" / "k" / "h"
        env_dir.parent.mkdir(parents=True)
        (env_dir.parent / "h.lock").write_text(f"{os.getpid()} 0\n", encoding="utf-8")
        lock = rep.EnvLock(env_dir, 0)
        assert lock.acquire() is True
        lock.release()

    def test_lock_admits_one_holder_across_processes(self, tmp_path: Path) -> None:
        """While another process holds an environment's lock no second holder gets it; once that process ends, one does.

        Two waiters that both judged a lock stale used to delete each other's lock file and provision one environment at
        the same time.
        """
        env_dir = tmp_path / "root" / "k" / "h"
        env_dir.parent.mkdir(parents=True)
        holder_code = (
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import review_env_provision as r; "
            "lock = r.EnvLock(Path(sys.argv[2]), 0); print(lock.acquire(), flush=True); sys.stdin.read()"
        )
        bin_dir = str(Path(rep.__file__).resolve().parent)
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, bin_dir, str(env_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )  # fmt: skip
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "True"
            contender = rep.EnvLock(env_dir, 0)
            assert contender.acquire() is False
        finally:
            holder.communicate(input="", timeout=60)
        assert contender.acquire() is True
        contender.release()

    @requires_git
    def test_unexpected_error_still_prints_a_document(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provisioning bug yields status ``unavailable`` with cause ``unknown`` and exit 0, never a traceback.

        The review then continues with the project interpreter and names the cause.
        """
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject()}, {})

        def broken(*_args: object, **_kwargs: object) -> object:
            raise ValueError("unexpected manifest shape")

        monkeypatch.setattr(rep, "classify_selection", broken)
        code = rep.main(["--repo", str(repo), "--base", base, "--head", head])
        document = json.loads(capsys.readouterr().out)
        assert (code, document["status"], document["cause"], document["error"]) == (
            0, "unavailable", "unknown", "ValueError: unexpected manifest shape"
        )  # fmt: skip

    @requires_git
    def test_unresolvable_ref_exits_2_with_a_document(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A caller error is the only non-zero exit, and it still prints a JSON document naming the error.

        Install failures never change the exit status; only an unresolvable repository or ref does.
        """
        repo, base, _ = _repo(tmp_path, {"pyproject.toml": pyproject()}, {})
        code = rep.main(["--repo", str(repo), "--base", base, "--head", "no-such-ref", "--plan-only"])
        assert (code, json.loads(capsys.readouterr().out)["status"]) == (2, "unavailable")


def _plant_interpreter(env_dir: Path, marker: Path) -> None:
    """Put an executable at the environment's interpreter path that records being run by creating ``marker``."""
    python = rep.env_python_path(env_dir)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(f'#!/bin/sh\n: > "{marker}"\n', encoding="utf-8")
    python.chmod(0o755)
    (env_dir / "provision-state.json").write_text('{"plan_digest": "any", "complete": true}\n', encoding="utf-8")


@requires_git
@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership, permission bits and shell interpreters")
class TestEnvironmentRoot:
    """Reviewer environments and caches live only under a root private to the invoking user."""

    @pytest.mark.parametrize("unsafe", ["shared-writable", "foreign-owner", "symlink"])
    def test_planted_environment_under_a_shared_root_is_never_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], unsafe: str
    ) -> None:
        """An interpreter another user planted under the temp root is neither run nor reported as the environment.

        On a host whose temp directory is shared, another local user could pre-create the root and plant ``bin/python``;
        probing or reusing it would run their code as the reviewer.
        """
        repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy",))}, {})
        if unsafe == "foreign-owner":
            other_user = os.getuid() + 1
            monkeypatch.setattr(rep.os, "getuid", lambda: other_user)
        root = rep.env_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        if unsafe == "symlink":
            (tmp_path / "attacker-root").mkdir(mode=0o700)
            root.symlink_to(tmp_path / "attacker-root", target_is_directory=True)
        else:
            root.mkdir(mode=0o700)
            root.chmod(0o777 if unsafe == "shared-writable" else 0o700)
        marker = tmp_path / "planted-interpreter-ran"
        _plant_interpreter(rep.env_dir_for(repo, head, root), marker)
        monkeypatch.setattr(rep, "find_installer", lambda: _UV)

        assert rep.main(["--repo", str(repo), "--base", base, "--head", head]) == 0
        document = json.loads(capsys.readouterr().out)

        assert not marker.exists()
        assert (document["status"], document["env_python"]) == ("unavailable", None)
        assert [failure["cause"] for failure in document["failed"]] == ["env-create-failed"]

    def test_root_is_created_private_and_named_for_the_user(self, fake: _FakeInstaller, tmp_path: Path) -> None:
        """The root, its repository directories and the installer caches are created owner-only, one root per user."""
        root = rep.env_root()
        request = rep.ProvisionRequest(rep.env_dir_for(_git_repo(tmp_path), "a" * 40, root), "python3", 60, 5)

        outcome = rep.provision(_install_plan(("numpy", "numpy")), _UV, request)

        assert outcome.env_python
        assert root.name == f"{rep.ENV_ROOT_NAME}-{os.getuid()}"
        assert [oct(path.stat().st_mode & 0o777) for path in (root, request.env_dir.parent)] == ["0o700", "0o700"]
        install = fake.installs()[0]
        assert rep.is_inside(Path(install[install.index("--cache-dir") + 1]), root)


def _git_repo(tmp_path: Path) -> Path:
    """Create a repository with one commit for tests that need only its identity."""
    repo, _, _ = _repo(tmp_path, {"pyproject.toml": pyproject()}, {})
    return repo


@requires_git
def test_document_carries_host_native_launch_environment(
    tmp_path: Path, fake: _FakeInstaller, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The launch environment is emitted by the helper in the host's own path form, never assembled by hand.

    ``PYTHONPATH`` joins the copy-relative source roots with this host's separator and ``PATH_PREPEND`` names the
    environment's own script directory, so a native Windows review imports a src-layout project instead of reading the
    import error as a missing package.
    """
    repo, base, head = _repo(tmp_path, {"pyproject.toml": pyproject(deps=("numpy",))}, {})
    monkeypatch.setattr(rep, "find_installer", lambda: _UV)

    assert rep.main(["--repo", str(repo), "--base", base, "--head", head]) == 0
    document = json.loads(capsys.readouterr().out)

    env_dir = Path(document["env_python"]).parent.parent
    scripts = env_dir / ("Scripts" if os.name == "nt" else "bin")
    assert document["gate_environment"] == {
        "VIRTUAL_ENV": str(env_dir),
        "PATH_PREPEND": os.pathsep.join((str(scripts), "/base")),
        "PYTHONPATH": os.pathsep.join(("src", ".")),
    }


def _wheel(directory: Path, name: str, requires: str = "") -> Path:
    """Write a minimal pure-Python wheel for ``name`` 1.0 whose metadata lists ``requires`` as a dependency."""
    path = directory / f"{name}-1.0-py3-none-any.whl"
    info = f"{name}-1.0.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n" + (
        f"Requires-Dist: {requires}\n" if requires else ""
    )
    files = {
        f"{name}/__init__.py": "",
        f"{info}/METADATA": metadata,
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{entry},,\n" for entry in [*files, f"{info}/RECORD"])
    with zipfile.ZipFile(path, "w") as archive:
        for entry, text in {**files, f"{info}/RECORD": record}.items():
            archive.writestr(entry, text)
    return path


@pytest.mark.integration
def test_pip_fallback_never_fetches_unplanned_wheel_dependencies(tmp_path: Path) -> None:
    """Install the planned wheels without fetching their unplanned index or direct dependencies.

    The local ``--find-links`` directory stands in for the index so both dependency types are available offline.
    """
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    payload = _wheel(wheels, "payload")
    _wheel(wheels, "plain", "payload")
    _wheel(wheels, "approved", f"payload @ {payload.as_uri()}")
    env_dir = tmp_path / "env"
    subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True, capture_output=True, timeout=300)
    env_python = str(rep.env_python_path(env_dir))

    def install(name: str) -> subprocess.CompletedProcess[str]:
        argv = rep.Installer("pip").install_argv(env_python, tmp_path / "cache", [name])
        index = argv.index("--index-url")
        argv[index : index + 2] = ["--no-index", "--find-links", str(wheels)]
        return subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)

    plain, approved = install("plain"), install("approved")

    assert plain.returncode == 0, plain.stderr
    assert approved.returncode == 0, approved.stderr
    listed = subprocess.run(
        [env_python, "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    names = {entry["name"] for entry in json.loads(listed.stdout)}
    assert {"plain", "approved", "payload"} & names == {"plain", "approved"}


def test_installer_environment_keeps_no_compiler_settings() -> None:
    """Installs are wheel-only, so compiler and build settings from the caller never reach an installer."""
    source = {"PATH": "/bin", "CC": "evil-cc", "CFLAGS": "-O0", "LDFLAGS": "-L/x", "SECRET_TOKEN": "s"}
    env = rep.installer_env(source, Path("/root"))
    assert (env["PATH"], {"CC", "CFLAGS", "LDFLAGS", "SECRET_TOKEN"} & set(env)) == ("/bin", set())


_DAY = 86400.0
_NOW = 1_800_000_000.0


def _owned_env(root: Path, name: str, age_days: float, payload: bytes = b"x" * 100) -> Path:
    """Create one environment directory this helper owns, last used ``age_days`` before ``_NOW``."""
    env_dir = root / "repo-key" / name
    (env_dir / "bin").mkdir(parents=True)
    (env_dir / "bin" / "python").write_bytes(payload)
    rep.mark_env(env_dir)
    stamp = _NOW - age_days * _DAY
    os.utime(env_dir / rep.MARKER_NAME, (stamp, stamp))
    return env_dir


def _write_files(directory: Path, files: dict[str, str]) -> Path:
    """Create ``directory`` with the given text files, all dated 90 days before ``_NOW``."""
    directory.mkdir(parents=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
        os.utime(directory / name, (_NOW - 90 * _DAY,) * 2)
    return directory


def _marker_bytes(env_dir: Path) -> int:
    """Return the marker file's size, part of what a prune frees."""
    return (env_dir / rep.MARKER_NAME).stat().st_size


class TestPrune:
    """Environments unused for seven days are deleted; nothing else under the root is ever touched."""

    @pytest.mark.parametrize(
        ("age_days", "pruned"),
        [
            pytest.param(7 + 1 / _DAY, True, id="one-second-past-seven-days"),
            pytest.param(7 - 60 / _DAY, False, id="one-minute-short-of-seven-days"),
        ],
    )
    def test_age_boundary(self, tmp_path: Path, age_days: float, pruned: bool) -> None:
        """An environment last used more than seven days ago is deleted; a younger one stays.

        Age is the marker's mtime, refreshed on every reuse, so an environment in regular use never ages out.
        """
        env_dir = _owned_env(tmp_path, "old", age_days)
        result = rep.prune_envs(tmp_path, tmp_path / "repo-key" / "current", now=_NOW)
        assert (env_dir.exists(), result["count"]) == (not pruned, int(pruned))

    def test_count_and_bytes_freed_are_reported(self, tmp_path: Path) -> None:
        """The result names how many environments were deleted and how many bytes that freed.

        The provisioning document carries this under ``pruned``, so a user can see what the cleanup did.
        """
        env_dir = _owned_env(tmp_path, "old", 30, b"y" * 1000)
        expected_bytes = 1000 + _marker_bytes(env_dir) + (env_dir / "bin").lstat().st_size
        result = rep.prune_envs(tmp_path, tmp_path / "repo-key" / "current", now=_NOW)
        assert (result["count"], result["bytes"], result["errors"]) == (1, expected_bytes, [])

    @pytest.mark.parametrize(
        "marker_files",
        [
            pytest.param({}, id="no-marker"),
            pytest.param({rep.MARKER_NAME: '{"tool": "something-else", "schema": 1}'}, id="other-tool-marker"),
            pytest.param({rep.MARKER_NAME: "not json"}, id="unreadable-marker"),
        ],
    )
    def test_directory_without_this_tools_marker_is_never_touched(
        self, tmp_path: Path, marker_files: dict[str, str]
    ) -> None:
        """An old directory under the root that this helper did not create stays, whatever its age.

        Shared installer caches and anything another tool keeps in the temp root must survive every prune.
        """
        foreign = _write_files(tmp_path / "repo-key" / "foreign", {"data": "keep", **marker_files})
        os.utime(foreign, (_NOW - 90 * _DAY,) * 2)
        result = rep.prune_envs(tmp_path, tmp_path / "repo-key" / "current", now=_NOW)
        assert ((foreign / "data").read_text(encoding="utf-8"), result["count"]) == ("keep", 0)

    def test_current_environment_is_never_pruned(self, tmp_path: Path) -> None:
        """The environment this run is about to use stays even when its marker is old.

        A long-idle head reviewed again must reuse its environment, not lose it to its own run's cleanup.
        """
        current = _owned_env(tmp_path, "current", 30)
        result = rep.prune_envs(tmp_path, current, now=_NOW)
        assert (current.exists(), result["count"]) == (True, 0)

    def test_environment_whose_lock_is_held_is_never_pruned(self, tmp_path: Path) -> None:
        """An old environment another run is provisioning right now (lock held by a live process) stays.

        Deleting it mid-install would break that run's review.
        """
        env_dir = _owned_env(tmp_path, "busy", 30)
        busy = rep.EnvLock(env_dir, 0)
        assert busy.acquire() is True
        try:
            result = rep.prune_envs(tmp_path, tmp_path / "repo-key" / "current", now=_NOW)
        finally:
            busy.release()
        assert (env_dir.exists(), result["count"]) == (True, 0)

    @requires_symlinks
    def test_symlinked_directories_are_not_followed(self, tmp_path: Path) -> None:
        """A link in the root pointing at an old owned environment elsewhere is skipped, and its target survives.

        A prune that followed links could delete anything the link points at, outside the temp root.
        """
        root, outside = tmp_path / "root", tmp_path / "outside"
        target = _owned_env(outside, "victim", 30)
        (root / "repo-key").mkdir(parents=True)
        (root / "repo-key" / "linked").symlink_to(target, target_is_directory=True)
        (root / "linked-repo").symlink_to(outside / "repo-key", target_is_directory=True)
        result = rep.prune_envs(root, root / "repo-key" / "current", now=_NOW)
        assert (target.exists(), result["count"]) == (True, 0)

    @requires_symlinks
    def test_link_inside_a_pruned_environment_keeps_its_target(self, tmp_path: Path) -> None:
        """Deleting an old environment removes a link inside it, never the file the link points at.

        Interpreter links in a virtual environment point at the base Python; that must never be deleted.
        """
        keep = tmp_path / "base-python"
        keep.write_text("interpreter", encoding="utf-8")
        env_dir = _owned_env(tmp_path / "root", "old", 30)
        (env_dir / "bin" / "python3").symlink_to(keep)
        result = rep.prune_envs(tmp_path / "root", tmp_path / "root" / "repo-key" / "current", now=_NOW)
        assert (env_dir.exists(), keep.read_text(encoding="utf-8"), result["count"]) == (False, "interpreter", 1)


@pytest.mark.parametrize(
    ("installer", "isolation_flag"),
    [
        pytest.param(rep.Installer("uv", "uv"), "--no-config", id="uv"),
        pytest.param(rep.Installer("pip"), "--isolated", id="pip"),
    ],
)
def test_install_command_is_wheel_only_on_the_pinned_index(installer: rep.Installer, isolation_flag: str) -> None:
    """Both installers fetch wheels only from the official index and ignore every discovered configuration.

    Building a source distribution runs downloaded code outside any sandbox, so no install may build one.
    """
    argv = installer.install_argv("/env/bin/python", Path("/root"), ["numpy>=1"])
    assert (isolation_flag in argv, argv[argv.index("--only-binary") + 1], rep.DEFAULT_INDEX in argv, "--no-deps" in argv) == (
        True, ":all:", True, True
    )  # fmt: skip


@pytest.mark.parametrize(
    ("output", "timed_out", "cause"),
    [
        pytest.param("Because pkg==1.0 has no usable wheels and building from source is disabled, ...", False,
                     "unsupported-platform", id="uv-sdist-only"),
        pytest.param("error: Request failed after 3 retries\n  Caused by: error sending request", False, "network",
                     id="uv-offline"),
        pytest.param("WARNING: Retrying ... Failed to establish a new connection: [Errno 8] nodename nor servname",
                     False, "network", id="pip-offline"),
        pytest.param("No solution found when resolving dependencies:\n  Because a==1 depends on b<1 and b>=2 ...",
                     False, "resolver-conflict", id="uv-conflict"),
        pytest.param("ERROR: ResolutionImpossible: for help visit ...", False, "resolver-conflict", id="pip-conflict"),
        pytest.param("error: Failed to build `pkg==1.0`\n  Caused by: The build backend returned an error", False,
                     "build-failure", id="uv-build"),
        pytest.param("ERROR: Could not find a version that satisfies the requirement x (from versions: none)", False,
                     "unsupported-platform", id="pip-no-dist"),
        pytest.param("anything", True, "timeout", id="timeout"),
        pytest.param("cannot start uv: [Errno 2] No such file or directory", False, "no-installer",
                     id="installer-missing"),
        pytest.param("/env/bin/python: No module named pip", False, "no-installer", id="pip-missing"),
        pytest.param("ERROR: direct reference refused: payload", False, "direct-reference",
                     id="transitive-direct-reference"),
        pytest.param("something new", False, "unknown", id="unknown"),
    ],
)  # fmt: skip
def test_installer_output_maps_to_one_cause(output: str, timed_out: bool, cause: str) -> None:
    """Every install failure carries one cause from the closed set.

    Network wins over the rest because an offline host fails every name with whatever message the index error adds.
    """
    assert rep.classify_failure(output, timed_out).value == cause


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        pytest.param("python_version >= '3.10'", True, id="version-true"),
        pytest.param("python_version < '3.10'", False, id="version-false"),
        pytest.param("sys_platform == 'win32' and python_version > '3'", False, id="and"),
        pytest.param("(sys_platform == 'win32') or (platform_machine == 'arm64')", True, id="or-parens"),
        pytest.param("'linux' in sys_platform", True, id="in"),
        pytest.param("python_version ~= '3.12'", True, id="compatible"),
        pytest.param("python_version >=", None, id="truncated"),
    ],
)
def test_marker_evaluation(marker: str, expected: bool | None) -> None:
    """Markers are evaluated for the environment's own interpreter; an unparsable one is left to the installer.

    Version variables compare as release tuples, so ``3.10`` sorts after ``3.9``.
    """
    env = {"python_version": "3.12", "sys_platform": "linux", "platform_machine": "arm64"}
    assert rep.evaluate_marker(marker, env) is expected


def test_host_spec_reports_os_and_arch_as_observed_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """OS and architecture come from the platform, never a guess; an absent accelerator carries its probe evidence.

    With no probe tool on PATH, CUDA is absent with the exact reason and TensorRT inherits it.
    """
    monkeypatch.setattr(rep, "_command_text", lambda argv, timeout=10: None)
    monkeypatch.setattr(rep.shutil, "which", lambda name: None)
    host = rep.host_spec()
    assert (host["os"], host["machine"], host["accelerators"]) == (platform.system(), platform.machine(), [])
    assert host["absent_accelerators"]["cuda"] == "nvidia-smi not on PATH"  # type: ignore[index]
    assert host["absent_accelerators"]["tensorrt"] == "needs CUDA: nvidia-smi not on PATH"  # type: ignore[index]
