"""Verify local environment planning and provisioning without package downloads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import dev_env_provision as provision
import pytest

_ROUTING_CASES = json.loads(Path(__file__).with_name("routing_cases.json").read_bytes())


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a project with its own user config and temporary environment directories."""
    repo = tmp_path / "project"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    for key in tuple(os.environ):
        if key.startswith(("PIP_", "UV_")):
            monkeypatch.delenv(key)
    for key, path in {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_CONFIG_DIRS": home / "xdg-system",
        "APPDATA": home / "app-data",
        "PROGRAMDATA": home / "program-data",
    }.items():
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    prefix = tmp_path / "python-prefix"
    prefix.mkdir()
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(provision.shutil, "which", lambda name: None)
    return repo


@pytest.fixture
def routing_files(project: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> str:
    """Set up the case's real project and host declarations and return its expected routing scope."""
    case = request.param
    for name, content in case["files"].items():
        (project / name).write_text(content, encoding="utf-8")
    for name, content in case.get("home_files", {}).items():
        path = Path.home() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for name, value in case.get("env", {}).items():
        monkeypatch.setenv(name, value)
    return case["expect"]


@pytest.mark.parametrize(
    "routing_files",
    [
        pytest.param(case, id=case["name"])
        for group in ("cases", "host_cases")
        for case in _ROUTING_CASES[group]
        if "develop" in case["readers"]
    ],
    indirect=True,
)
def test_plan_respects_routing(project, routing_files, capsys):
    """Plan both, one or neither name according to the routing declarations."""
    private = f"{_ROUTING_CASES['private_name']}==1.0"
    public = f"{_ROUTING_CASES['public_name']}==2.32.3"
    planned, skipped = {
        "ordinary": ([private, public], []),
        "name-routed": ([public], [private]),
        "all-routed": ([], [private, public]),
    }[routing_files]

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["failed"] == []
    assert receipt["planned"] == planned
    assert receipt["skipped"] == [{"requirement": spec, "cause": "index-routing"} for spec in skipped]


def test_plan_reads_selected_declarations_without_running(project, monkeypatch, capsys):
    """Plan only reports safe specs without running an installer or creating an environment."""
    (project / "pyproject.toml").write_text(
        '[project]\nname = "local"\ndependencies = ["base>=1"]\n'
        '[project.optional-dependencies]\nextra = ["extra-dep"]\n'
        '[dependency-groups]\nci = ["ci-dep", {include-group = "tools"}]\n'
        'integration-tests = ["test-dep"]\ntools = ["included-dep"]\nother = ["opt-in"]\n'
        '[tool.uv]\ndev-dependencies = ["uv-dep"]\n',
        encoding="utf-8",
    )
    (project / "requirements").mkdir()
    (project / "requirements.txt").write_text("runtime-dep # comment\n", encoding="utf-8")
    (project / "requirements" / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    (project / "requirements-docs.txt").write_text("sphinx\n", encoding="utf-8")
    monkeypatch.setattr(provision, "_run", lambda *args, **kwargs: pytest.fail("plan launched the installer runner"))

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert set(receipt["planned"]) == {
        "base>=1",
        "extra-dep",
        "ci-dep",
        "test-dep",
        "included-dep",
        "uv-dep",
        "runtime-dep",
        "pytest",
    }
    assert receipt["env_python"] is None
    assert not provision.environment_root().exists()


def test_plan_filters_named_groups_extras_and_unsafe_specs(project, capsys):
    """Keep explicit groups, honor exclusions, and classify URLs, paths and unsafe options."""
    (project / "pyproject.toml").write_text(
        '[project]\nname = "local"\ndependencies = ["local", "drop_me", "safe[tls]>=1; python_version >= \'3.10\'"]\n'
        '[project.optional-dependencies]\nextra = ["extra"]\n[dependency-groups]\ntools = ["tooldep"]\n',
        encoding="utf-8",
    )
    (project / "requirements.txt").write_text(
        "direct @ https://example.invalid/direct.whl\n./local-1-py3-none-any.whl\nunsafe==\n",
        encoding="utf-8",
    )
    assert (
        provision.main(
            ["--repo", str(project), "--plan-only", "--no-extras", "--group", "tools", "--exclude", "drop-me"]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["planned"] == ["safe[tls]>=1; python_version >= '3.10'", "tooldep"]
    assert {item["requirement"]: item["cause"] for item in receipt["skipped"]} == {
        "direct": "direct-reference",
        "local": "direct-reference",
        "unsafe==": "unsafe-spec",
    }


def test_hash_pinned_requirements_are_planned_as_pip_reads_them(project, capsys):
    """A pip-compile file plans every pinned name: continuations join and per-requirement hashes are dropped."""
    (project / "requirements.txt").write_text(
        "requests==2.32.3 \\\n"
        "    --hash=sha256:aaaa \\\n"
        "    --hash=sha256:bbbb\n"
        "    # via app\n"
        "idna==3.7 --hash=sha256:cccc\n"
        "urllib3==2.2.2\n",
        encoding="utf-8",
    )

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert (receipt["planned"], receipt["skipped"]) == (["requests==2.32.3", "idna==3.7", "urllib3==2.2.2"], [])


@pytest.mark.parametrize(
    "option",
    [
        pytest.param("-ihttps://private.invalid/simple", id="short-attached"),
        pytest.param("--index-url https://private.invalid/simple", id="long"),
        pytest.param("--extra-index=https://private.invalid/simple", id="long-prefix"),
        pytest.param("-f./wheels", id="find-links-attached"),
        pytest.param("--frobnicate x", id="unknown-option"),
    ],
)
def test_index_routing_option_keeps_every_name_off_the_default_index(project, capsys, option):
    """A routing option in any spelling plans nothing: a name meant for another index is never fetched from PyPI."""
    (project / "pyproject.toml").write_text('[project]\nname = "local"\ndependencies = ["base>=1"]\n', encoding="utf-8")
    (project / "requirements.txt").write_text(f"{option}\ninternal-pkg==1.0\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == []
    assert receipt["skipped"][0]["cause"] == "index-routing"
    assert {item["requirement"]: item["cause"] for item in receipt["skipped"][1:]} == {
        "base>=1": "index-routing",
        "internal-pkg==1.0": "index-routing",
    }


@pytest.mark.parametrize(
    ("include", "cause"),
    [
        pytest.param("-r nested/deps.txt", "requirement-include", id="requirement-short"),
        pytest.param("-rnested/deps.txt", "requirement-include", id="requirement-short-attached"),
        pytest.param("--requirement=nested/deps.txt", "requirement-include", id="requirement-long-attached"),
        pytest.param("-c nested/deps.txt", "requirement-include", id="constraint-short"),
        pytest.param("-cnested/deps.txt", "requirement-include", id="constraint-short-attached"),
        pytest.param("--constraint nested/deps.txt", "requirement-include", id="constraint-long"),
        pytest.param("--constr=nested/deps.txt", "requirement-include", id="constraint-long-prefix"),
        pytest.param('--pre "--requirement" nested/deps.txt', "requirement-include", id="quoted-option"),
        pytest.param(r"--pre --\constraint nested/deps.txt", "requirement-include", id="escaped-option"),
        pytest.param("${REQ_INCLUDE}", "index-routing", id="interpolated-option"),
    ],
)
def test_nested_requirement_routing_keeps_all_declarations_off_the_default_index(
    project, capsys, monkeypatch, include, cause
):
    """A nested requirement or constraint file can change the index for packages from any declaration source."""
    (project / "nested").mkdir()
    (project / "nested" / "deps.txt").write_text("-r private.txt\n", encoding="utf-8")
    (project / "nested" / "private.txt").write_text("--index-url https://private.invalid/simple\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "local"\ndependencies = ["base>=1"]\n', encoding="utf-8")
    (project / "requirements.txt").write_text(f"{include}\ninternal-pkg==1.0\n", encoding="utf-8")
    (project / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    monkeypatch.setenv("REQ_INCLUDE", "-r nested/deps.txt")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == []
    assert receipt["skipped"][0]["cause"] == cause
    assert {item["requirement"]: item["cause"] for item in receipt["skipped"][1:]} == {
        "base>=1": "index-routing",
        "internal-pkg==1.0": "index-routing",
        "pytest": "index-routing",
    }


def test_uv_sources_keep_named_requirements_off_the_default_index(project, capsys):
    """Apply a source mapping to the normalized name across all declarations."""
    (project / "pyproject.toml").write_text(
        '[project]\nname = "local"\ndependencies = ["internal-pkg==1.0", "public-pkg"]\n'
        '[tool.uv.sources]\nInternal_Pkg = { index = "private" }\n'
        '[[tool.uv.index]]\nname = "private"\nurl = "https://private.invalid/simple"\nexplicit = true\n',
        encoding="utf-8",
    )
    (project / "requirements.txt").write_text("internal.pkg[extra]>=1\npytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == ["public-pkg", "pytest"]
    assert receipt["skipped"] == [
        {"requirement": "internal-pkg==1.0", "cause": "index-routing"},
        {"requirement": "internal.pkg[extra]>=1", "cause": "index-routing"},
    ]


@pytest.mark.parametrize(
    ("table", "settings"),
    [
        pytest.param("[[tool.uv.index]]", 'url = "https://private.invalid/simple"', id="implicit-index"),
        pytest.param(
            "[[tool.uv.index]]", 'url = "https://private.invalid/simple"\nexplicit = false', id="non-explicit-index"
        ),
        pytest.param(
            "[[tool.uv.index]]",
            'url = "https://private.invalid/simple"\nexplicit = true\ndefault = true',
            id="explicit-default-index",
        ),
        pytest.param(
            "[[tool.uv.index]]", 'url = "https://private.invalid/simple"\nexplicit = "true"', id="unmodeled-index"
        ),
        pytest.param("[tool.uv]", 'index-url = "https://private.invalid/simple"', id="index-url"),
        pytest.param("[tool.uv]", 'extra-index-url = ["https://private.invalid/simple"]', id="extra-index-url"),
        pytest.param("[tool.uv]", 'find-links = ["./wheels"]', id="find-links"),
        pytest.param("[tool.uv]", 'default-index = "https://private.invalid/simple"', id="default-index"),
        pytest.param("[tool.uv]", "no-index = true", id="no-index"),
        pytest.param("[tool.uv]", 'constraint-dependencies = ["internal-pkg<2"]', id="uv-constraints"),
        pytest.param("[tool.uv]", "unmodeled-option = true", id="unknown-uv-key"),
        pytest.param("[tool.uv.pip]", 'index-url = "https://private.invalid/simple"', id="uv-pip-routing"),
        pytest.param("[tool.uv.pip]", "unmodeled-option = true", id="unknown-uv-pip-key"),
        pytest.param("[tool.hatch.settings]", "enabled = true", id="unknown-hatch-section"),
        pytest.param(
            '[tool.pdm.version]\nsource = "scm"\n[tool.pdm.settings]', "enabled = true", id="pdm-with-version"
        ),
        pytest.param("[tool.pdm]", "", id="empty-pdm-table"),
        pytest.param("[[tool.unknown]]", "enabled = true", id="unknown-tool-array"),
        pytest.param(
            "[[tool.pdm.source]]", 'name = "private"\nurl = "https://private.invalid/simple"', id="pdm-source"
        ),
        pytest.param("[tool.pdm.source]", 'url = "https://private.invalid/simple"', id="source-table"),
        pytest.param(
            "[tool.pdm.resolution.version]", 'source = "https://private.invalid/simple"', id="nested-pdm-source"
        ),
        pytest.param(
            "[tool.poetry.dependencies]",
            'internal-pkg = [{url = "https://private.invalid/internal.whl"}]',
            id="poetry-url-in-array",
        ),
        pytest.param("[tool.pixi.pypi-options]", 'find-links = ["./wheels"]', id="pixi-find-links"),
    ],
)
def test_pyproject_global_routing_keeps_every_name_off_the_default_index(project, capsys, table, settings):
    """Refuse all candidates when a project-wide source has no modeled package scope."""
    (project / "pyproject.toml").write_text(
        f'[project]\nname = "local"\ndependencies = ["internal-pkg==1.0"]\n{table}\n{settings}\n',
        encoding="utf-8",
    )
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == []
    assert receipt["skipped"] == [
        {"requirement": "internal-pkg==1.0", "cause": "index-routing"},
        {"requirement": "pytest", "cause": "index-routing"},
    ]


@pytest.mark.parametrize(
    "table",
    [
        pytest.param("[[tool.coverage.index]]", id="index-table"),
        pytest.param("[[tool.coverage]]\n[[tool.coverage.index]]", id="nested-index-array"),
    ],
)
def test_plan_ignores_noninstaller_index_tables(project, capsys, table):
    """Ignore index declarations belonging to an unrelated tool."""
    (project / "pyproject.toml").write_text(f'{table}\nurl = "https://docs.invalid"\n', encoding="utf-8")
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert (receipt["planned"], receipt["skipped"], receipt["failed"]) == (["pytest"], [], [])


@pytest.mark.parametrize(
    ("settings", "planned", "skipped"),
    [
        pytest.param(
            'managed = false\ncache-keys = [{file = "uv.lock"}]\n[pip]\nenvironments = ["sys_platform == \'win32\'"]\n',
            ["internal-pkg", "pytest"],
            [],
            id="neutral-root-and-pip",
        ),
        pytest.param(
            'index = [{ name = "private", url = "https://private.invalid/simple", explicit = true }]\n'
            '[sources]\nInternal_Pkg = { index = "private" }\n',
            ["pytest"],
            ["internal-pkg"],
            id="explicit-index-with-scoped-source",
        ),
        pytest.param("[pip]\nunmodeled-option = true\n", [], ["internal-pkg", "pytest"], id="unknown-pip-key"),
    ],
)
def test_plan_applies_uv_toml_to_requirements(project, capsys, settings, planned, skipped):
    """Apply the same routing model to a standalone uv config and requirements-only project."""
    (project / "uv.toml").write_text(settings, encoding="utf-8")
    (project / "requirements.txt").write_text("internal-pkg\npytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["failed"] == []
    assert receipt["planned"] == planned
    assert receipt["skipped"] == [{"requirement": spec, "cause": "index-routing"} for spec in skipped]


@pytest.mark.parametrize(
    ("name", "source", "planned"),
    [
        pytest.param("internal-pkg", '{ registry = "https://pypi.org/simple" }', ["internal-pkg", "pytest"], id="pypi"),
        pytest.param(
            "internal-pkg", '{ registry = "https://pypi.org/simple/" }', ["internal-pkg", "pytest"], id="pypi-slash"
        ),
        pytest.param(
            "Internal_Pkg", '{ git = "https://git.invalid/pkg#abc123" }', ["pytest"], id="git-normalized-name"
        ),
        pytest.param("internal-pkg", '{ path = "wheels/pkg.whl" }', ["pytest"], id="local-archive"),
        pytest.param("internal-pkg", '{ url = "https://wheels.invalid/pkg.whl" }', ["pytest"], id="remote-archive"),
        pytest.param("internal-pkg", '{ directory = "packages/pkg" }', ["pytest"], id="directory"),
        pytest.param("internal-pkg", '{ editable = "packages/pkg" }', ["pytest"], id="editable"),
        pytest.param("internal-pkg", '{ virtual = "packages/pkg" }', ["pytest"], id="virtual"),
        pytest.param("internal-pkg", '{ future-source = "pkg" }', ["pytest"], id="unmodeled-named-source"),
        pytest.param(
            "internal-pkg", '{ registry = "https://mirror.invalid/simple" }', ["pytest"], id="alternate-registry"
        ),
        pytest.param("internal-pkg", '{ registry = "../simple" }', ["pytest"], id="local-registry"),
        pytest.param("transitive-pkg", "{}", [], id="missing-source-kind"),
    ],
)
def test_plan_respects_locked_sources(project, capsys, name, source, planned):
    """Apply named lock sources across project and requirements declarations."""
    (project / "pyproject.toml").write_text(
        '[project]\nname = "local"\ndependencies = ["internal-pkg"]\n', encoding="utf-8"
    )
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (project / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n'
        f'[[package]]\nname = "{name}"\nversion = "1.0"\nsource = {source}\n'
        '[[package]]\nname = "pytest"\nversion = "8.4.0"\nsource = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["failed"] == []
    assert receipt["planned"] == planned
    assert receipt["skipped"] == [
        {"requirement": name, "cause": "index-routing"} for name in ("internal-pkg", "pytest") if name not in planned
    ]


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param("pyproject.toml", b"[tool.uv", id="malformed-pyproject"),
        pytest.param("uv.toml", b"[[index]", id="malformed-uv-toml"),
        pytest.param("uv.toml", b"\xff", id="invalid-uv-toml-encoding"),
        pytest.param("uv.lock", b"[[package]", id="malformed-uv-lock"),
        pytest.param("uv.lock", b"\xff", id="invalid-uv-lock-encoding"),
    ],
)
def test_plan_refuses_unparseable_project_files(project, capsys, filename, content):
    """Refuse even valid requirements when a present TOML declaration cannot be parsed."""
    (project / filename).write_bytes(content)
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == []
    assert [item["cause"] for item in receipt["failed"]] == ["unsafe-spec"]


@pytest.mark.parametrize("filename", ["pdm.toml", ".pdm.toml", "pixi.toml", "poetry.toml"])
def test_unmodeled_installer_files_route_all_names(project, capsys, filename):
    """An opaque installer config blocks default-index planning even if empty."""
    (project / filename).touch()
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == []
    assert receipt["skipped"] == [{"requirement": "pytest", "cause": "index-routing"}]


@pytest.mark.parametrize(
    "variable",
    [
        "UV_INDEX",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "UV_CONFIG_FILE",
        "UV_OVERRIDE",
        "PIP_FIND_LINKS",
        "PIP_NO_INDEX",
        "PIP_CONFIG_FILE",
    ],
)
def test_empty_host_routing_variables_refuse_default_index(project, capsys, monkeypatch, variable):
    """The planner treats presence as routing without opening custom config paths."""
    monkeypatch.setenv(variable, "")
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == []
    assert receipt["skipped"] == [{"requirement": "pytest", "cause": "index-routing"}]


@pytest.mark.parametrize(
    ("system", "name"),
    [
        pytest.param("Linux", "xdg-system/pip/pip.conf", id="xdg-system-pip"),
        pytest.param("Linux", ".pip/pip.conf", id="legacy-user-pip"),
        pytest.param("Darwin", "Library/Application Support/pip/pip.conf", id="macos-user-pip"),
        pytest.param("Windows", "app-data/pip/pip.ini", id="windows-user-pip"),
        pytest.param("Windows", "pip/pip.ini", id="windows-legacy-user-pip"),
        pytest.param("Windows", "program-data/pip/pip.ini", id="windows-system-pip"),
    ],
)
def test_host_pip_configs_route_all_names(project, capsys, monkeypatch, system, name):
    """Read native config locations and recognize routing in a pip command section."""
    monkeypatch.setattr(provision.platform, "system", lambda: system)
    config = Path.home() / name
    config.parent.mkdir(parents=True)
    config.write_text("[install]\nfind-links = https://private.invalid/wheels\n", encoding="utf-8")
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == []
    assert receipt["skipped"] == [{"requirement": "pytest", "cause": "index-routing"}]


@pytest.mark.parametrize(
    ("system", "filename", "settings", "routed"),
    [
        pytest.param(
            "Linux", "pip.conf", "[global]\nindex-url = https://private.invalid/simple", True, id="posix-index"
        ),
        pytest.param("Darwin", "pip.conf", "[install]\nno-index = true", True, id="macos-no-index"),
        pytest.param(
            "Windows", "pip.ini", "[install]\nfind-links = https://private.invalid/wheels", True, id="win-links"
        ),
        pytest.param("Linux", "pip.conf", "[global]\npypi-url = https://mirror.invalid/simple", True, id="index-alias"),
        pytest.param("Linux", "pip.conf", "[install]\nconstraint = constraints.txt", True, id="constraint-file"),
        pytest.param("Linux", "pip.conf", "[install]\nrequirement = requirements.txt", True, id="requirement-file"),
        pytest.param("Linux", "pip.conf", "[install]\neditable = ../local", True, id="editable-source"),
        pytest.param("Linux", "pip.conf", "[global]\nunmodeled = true", True, id="unmodeled-site-setting"),
        pytest.param(
            "Linux",
            "pip.conf",
            "[global]\ntimeout = 30\nno_cache_dir = true\n[install]\npre = true",
            False,
            id="neutral-site-settings",
        ),
        pytest.param("Windows", "pip.ini", "[install", True, id="malformed-site-settings"),
    ],
)
def test_plan_respects_pip_site_config(project, capsys, monkeypatch, system, filename, settings, routed):
    """Read pip's interpreter-prefix config using the filename for the selected platform."""
    monkeypatch.setattr(provision.platform, "system", lambda: system)
    (Path(sys.prefix) / filename).write_text(settings, encoding="utf-8")
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (project / "constraints.txt").write_text("pytest @ https://mirror.invalid/pytest.whl\n", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["failed"] == []
    assert receipt["planned"] == ([] if routed else ["pytest"])
    assert receipt["skipped"] == ([{"requirement": "pytest", "cause": "index-routing"}] if routed else [])


@pytest.mark.integration
def test_plan_respects_pip_site_config_of_selected_python(project, capsys):
    """Use the config of a separately selected Python even if it links to the current runtime."""
    selected = project.parent / "selected"
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(selected)], check=True, timeout=20)
    filename = "pip.ini" if os.name == "nt" else "pip.conf"
    (selected / filename).write_text("[install]\nextra-index-url = https://private.invalid/simple", encoding="utf-8")
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    args = ["--repo", str(project), "--plan-only"]

    assert provision.main(args) == 0
    assert json.loads(capsys.readouterr().out)["planned"] == ["pytest"]
    assert provision.main([*args, "--python", str(provision._env_python(selected))]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["failed"] == []
    assert receipt["planned"] == []
    assert receipt["skipped"] == [{"requirement": "pytest", "cause": "index-routing"}]


@pytest.mark.parametrize(
    ("system", "name", "settings", "routed"),
    [
        pytest.param("Windows", "app-data/uv/uv.toml", "[index", True, id="malformed-windows-user-uv"),
        pytest.param("Windows", "program-data/uv/uv.toml", "no-index = true", True, id="windows-system-uv"),
        pytest.param("Linux", ".config/uv/uv.toml", "[sources]\nother = { workspace = true }", True, id="uv-source"),
        pytest.param(
            "Linux", ".config/uv/uv.toml", '[[index]]\nname = "private"\nexplicit = true', True, id="uv-explicit-index"
        ),
        pytest.param("Linux", ".config/uv/uv.toml", "managed = false", False, id="neutral-uv-config"),
        pytest.param("Linux", ".config/pip/pip.conf", "[global]\ntimeout = 30", False, id="neutral-pip-config"),
        pytest.param(
            "Linux",
            ".config/pip/pip.conf",
            "[global]\nbreak-system-packages = true\n",
            False,
            id="break-system-packages",
        ),
    ],
)
def test_host_configs_distinguish_routing_from_neutral_settings(
    project, capsys, monkeypatch, system, name, settings, routed
):
    """Host sources are global, while modeled non-routing settings still permit default-index planning."""
    monkeypatch.setattr(provision.platform, "system", lambda: system)
    monkeypatch.setenv("UV_NO_PROGRESS", "1")
    monkeypatch.setenv("UV_RUN_RECURSION_DEPTH", "1")  # exported by `uv run`
    config = Path.home() / name
    config.parent.mkdir(parents=True)
    config.write_text(settings, encoding="utf-8")
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (project / "pip.conf").write_text("[global]\nno-index = true", encoding="utf-8")

    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["planned"] == ([] if routed else ["pytest"])
    assert receipt["skipped"] == ([{"requirement": "pytest", "cause": "index-routing"}] if routed else [])


@pytest.mark.parametrize(
    ("output", "cause"),
    [
        pytest.param("Could not fetch URL: certificate verify failed", "network", id="network"),
        pytest.param("ResolutionImpossible: conflicting dependencies", "resolver-conflict", id="conflict"),
        pytest.param("wheel has no compatible platform tag", "unsupported-platform", id="platform"),
        pytest.param("ERROR: No matching distribution found", "no-wheel", id="no-wheel"),
        pytest.param("No module named pip", "installer-missing", id="missing-installer"),
    ],
)
def test_installer_failure_is_closed_data(project, monkeypatch, output, cause):
    """Translate simulated installer failures into the exact closed cause."""
    failure = subprocess.CompletedProcess([], 1, "", output)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: failure)
    with pytest.raises(provision._Issue) as raised:
        provision._run(["installer"], project, 1)
    assert raised.value.cause == cause


def test_no_toml_reader_is_unavailable(project, monkeypatch, capsys):
    """Report inability to read TOML without attempting to provision guessed requirements."""
    monkeypatch.setattr(provision, "tomllib", None)
    assert provision.main(["--repo", str(project)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "unavailable"
    assert [item["cause"] for item in receipt["failed"]] == ["no-toml-reader"]


@pytest.fixture
def inspected():
    """Configure the simulated interpreter's installed distributions and marker environment."""
    return {"present": [["pytest", "8.4.0"]], "environment": {}}


@pytest.fixture
def installer(monkeypatch, inspected):
    """Simulate environment creation and process-level inspection, recording every command."""
    commands = []

    def run(argv, cwd, timeout):
        """Create fake venv state or return the output of the selected fake child."""
        commands.append(argv)
        if "venv" in argv:
            env = Path(argv[-1])
            python = provision._env_python(env)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
            (env / "pyvenv.cfg").touch()
        if "import importlib.metadata" in " ".join(argv):
            return json.dumps({"installed": inspected["present"], "environment": inspected["environment"]})
        return ""

    monkeypatch.setattr(provision, "_run", run)
    return commands


def test_unchanged_project_reuses_environment_and_writes_only_outside(project, installer, capsys):
    """Reuse the same interpreter with no second install and keep the project tree byte-for-byte unchanged."""
    declarations = project / "requirements.txt"
    declarations.write_bytes(b"pytest>=8\n")
    target = project.parent / "receipt.json"
    args = ["--repo", str(project), "--out", str(target), "--probe-import", "pytest"]

    assert provision.main(args) == 0
    first = capsys.readouterr().out
    assert first == target.read_text(encoding="utf-8")
    assert provision.main(args) == 0
    second = json.loads(capsys.readouterr().out)

    assert second["reused"] is True
    assert second["status"] == "ready"
    assert second["env_python"] == json.loads(first)["env_python"]
    assert second["installed"] == [{"name": "pytest", "version": "8.4.0"}]
    assert second["import_probes"] == {"pytest": {"imports": True}}
    assert sum("--only-binary" in command for command in installer) == 1
    assert list(project.iterdir()) == [declarations]
    assert declarations.read_bytes() == b"pytest>=8\n"


def test_skipped_direct_reference_leaves_usable_partial_environment(project, installer, capsys):
    """Report partial when the venv exists and input was skipped, without forwarding the URL."""
    (project / "requirements.txt").write_text(
        "pytest\nhttps://example.invalid/example-1-py3-none-any.whl\n", encoding="utf-8"
    )
    assert provision.main(["--repo", str(project)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "partial"
    assert receipt["env_python"]
    assert all("example.invalid" not in " ".join(command) for command in installer)


def test_host_routing_prevents_every_install(project, installer, capsys, monkeypatch):
    """Provisioning never calls the installer when the host redirects dependency resolution."""
    monkeypatch.setenv("PIP_CONFIG_FILE", str(Path.home() / "custom.conf"))
    (project / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    assert provision.main(["--repo", str(project)]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["status"] == "partial"
    assert receipt["planned"] == []
    assert receipt["skipped"] == [{"requirement": "pytest", "cause": "index-routing"}]
    assert not any("--only-binary" in command for command in installer)


def test_timeout_is_unavailable_when_the_environment_cannot_be_created(project, monkeypatch, capsys):
    """Propagate a simulated child timeout as data without returning a broken interpreter."""

    def expire(command, **kwargs):
        """Simulate the subprocess reaching its individual timeout."""
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", expire)
    assert provision.main(["--repo", str(project)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["env_python"] is None
    assert receipt["status"] == "unavailable"
    assert [item["cause"] for item in receipt["failed"]] == ["timeout"]


def test_reused_environment_missing_a_planned_name_is_partial_and_reinstalls(project, installer, inspected, capsys):
    """A planned name absent from a reused environment makes it partial, and the next run installs again."""
    (project / "requirements.txt").write_text("pytest>=8\nnumpy>=1\n", encoding="utf-8")
    inspected["present"] = [["pytest", "8.4.0"], ["numpy", "2.0.0"]]
    assert provision.main(["--repo", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    inspected["present"] = [["pytest", "8.4.0"]]

    assert provision.main(["--repo", str(project)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert provision.main(["--repo", str(project)]) == 0

    assert (second["reused"], second["status"]) == (True, "partial")
    assert [(item.get("requirement"), item["cause"]) for item in second["failed"]] == [("numpy>=1", "not-installed")]
    assert sum("--only-binary" in command for command in installer) == 2


@pytest.mark.parametrize(
    ("system", "status"),
    [
        pytest.param("linux", "ready", id="marker-excludes-host"),
        pytest.param("win32", "partial", id="marker-applies"),
    ],
)
def test_marker_decides_whether_an_absent_name_is_a_gap(project, installer, inspected, capsys, system, status):
    """Judge marker applicability for the selected interpreter using its own environment."""
    marker = "sys_platform == 'win32'"
    (project / "requirements.txt").write_text(f"pytest\npywin32; {marker}\n", encoding="utf-8")
    inspected["environment"] = {"sys_platform": system}

    assert provision.main(["--repo", str(project)]) == 0
    receipt = json.loads(capsys.readouterr().out)

    assert receipt["status"] == status


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
def test_pip_fallback_installs_only_planned_wheels(tmp_path):
    """Install an approved wheel without resolving its metadata dependencies.

    A local ``--find-links`` directory stands in for the index, so the environment's own pip runs offline.
    """
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    payload = _wheel(tmp_path, "payload")
    _wheel(wheels, "plain")
    _wheel(wheels, "approved", f"payload @ {payload.as_uri()}")
    venv = tmp_path / "env"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, timeout=300)
    python = provision._env_python(venv)

    def install(name):
        """Run the fallback install command for ``name`` against the local wheel directory."""
        argv = provision._install_command(None, python, [name])
        index = argv.index("--index-url")
        argv[index : index + 2] = ["--no-index", "--find-links", str(wheels)]
        return subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)

    plain, approved = install("plain"), install("approved")

    assert plain.returncode == 0, plain.stderr
    assert approved.returncode == 0, approved.stderr
    listed = subprocess.run([str(python), "-m", "pip", "list"], capture_output=True, text=True, check=True)
    assert {"plain", "approved"}.issubset(listed.stdout.split())
    assert "payload" not in listed.stdout


@pytest.mark.parametrize(
    ("filename", "declaration", "planned"),
    [
        pytest.param(
            "tox.ini",
            "[testenv]\ninstall_command = pip install -i https://index.invalid {packages}\n",
            [],
            id="tox-ini",
        ),
        pytest.param(
            "pyproject.toml",
            '[tool.tox.env_run_base.set_env]\nPIP_INDEX_URL = "https://index.invalid"\n',
            [],
            id="embedded-tox",
        ),
        pytest.param(
            ".github/workflows/test.yml",
            "steps:\n  - run: pip install --extra-index-url https://index.invalid\n",
            [],
            id="workflow-installer",
        ),
        pytest.param(
            "ci/test.sh",
            "python -m pip install -i https://index.invalid -r requirements.txt\n",
            [],
            id="installer-script",
        ),
        pytest.param(
            ".github/workflows/test.yml",
            "env:\n  PIP_REQUIREMENT: other.txt\n",
            [],
            id="workflow-installer-environment",
        ),
        pytest.param("Pipfile", '[[source]]\nurl = "https://index.invalid"\n', [], id="pipfile-source"),
        pytest.param("Pipfile.lock", '{"_meta":{"sources":[{"url":"https://index.invalid"}]}}', [], id="pipfile-lock"),
        pytest.param("requirements/test.txt", "--index-url https://index.invalid\n", [], id="requirements-directory"),
        pytest.param("requirements/nested/test.in", "-c constraints.txt\n", [], id="nested-requirements-input"),
        pytest.param("requirements-route.txt", "\ufeff--index-url https://index.invalid\n", [], id="utf8-bom"),
        pytest.param("requirements-route.txt", "# coding: latin-1\nprivate-name\n", [], id="alternate-encoding"),
        pytest.param("requirements-route.txt", '"--index-url" https://index.invalid\n', [], id="quoted-leading-option"),
        pytest.param("requirements-route.txt", '--pre "--index-url\n', [], id="unparseable-option"),
        pytest.param(
            "requirements-route.txt",
            "https://index.invalid/private_name-1-py3-none-any.whl\n",
            ["public-name"],
            id="bare-wheel",
        ),
        pytest.param(
            "requirements-route.txt",
            "https://index.invalid/private_name-1.tar.gz#sha256=aa\n",
            ["public-name"],
            id="bare-source-archive",
        ),
        pytest.param("requirements-route.txt", "https://index.invalid/download\n", [], id="unattributed-source"),
        pytest.param(
            "requirements-route.txt",
            "private-name @ https://index.invalid/download\n",
            ["public-name"],
            id="named-direct-reference",
        ),
        pytest.param("requirements-route.txt", "-e ../local\n", [], id="unattributed-editable"),
        pytest.param(
            "requirements-route.txt",
            "extra-name\\\n# note\n",
            ["private-name", "public-name", "extra-name"],
            id="continued-requirement-then-comment",
        ),
    ],
)
def test_reader_routes_project_declarations(project, capsys, filename, declaration, planned):
    """Route only named direct sources and gate an opaque project-wide source."""
    (project / "pyproject.toml").write_text(
        '[project]\ndependencies = ["private-name", "public-name"]\n', encoding="utf-8"
    )
    path = project / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(declaration)
    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    assert json.loads(capsys.readouterr().out)["planned"] == planned


@pytest.mark.parametrize("variable", ["PIP_PYPI_URL", "PIP_REQUIREMENT", "PIP_CONSTRAINT", "PIP_BUILD_CONSTRAINT"])
def test_pip_environment_routes_plan(project, capsys, monkeypatch, variable):
    """An installer variable must have a modeled neutral role before default-index planning."""
    monkeypatch.setenv(variable, "configured")
    (project / "requirements.txt").write_text("public-name\n", encoding="utf-8")
    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == [{"cause": "index-routing", "requirement": "public-name"}]


@pytest.mark.parametrize(
    ("variable", "filename", "declaration"),
    [
        pytest.param(
            # uv reads machine config from XDG_CONFIG_DIRS on POSIX and from ProgramData on Windows.
            "PROGRAMDATA" if sys.platform == "win32" else "XDG_CONFIG_DIRS",
            "uv/uv.toml",
            'index-url = "https://index.invalid"\n',
            id="uv-machine-config",
        ),
        pytest.param(
            "XDG_DATA_DIRS",
            "pip/pip.conf",
            "[global]\nrequirement = configured.txt\n",
            id="pip-data-config",
            marks=pytest.mark.skipif(sys.platform != "darwin", reason="pip uses XDG_DATA_DIRS on macOS"),
        ),
    ],
)
def test_host_installer_discovers_config_sources(project, capsys, monkeypatch, variable, filename, declaration):
    """Find routing at OS-specific locations honored by the host installers."""
    config = project.parent / "system" / filename
    config.parent.mkdir(parents=True)
    config.write_text(declaration, encoding="utf-8")
    monkeypatch.setenv(variable, str(config.parent.parent))
    (project / "requirements.txt").write_text("public-name\n", encoding="utf-8")
    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == [{"cause": "index-routing", "requirement": "public-name"}]


def test_non_array_dependency_fields_do_not_declare_names(project, capsys):
    """Strings in array-valued TOML fields cannot contribute individual characters as packages."""
    (project / "pyproject.toml").write_text(
        '[project]\ndependencies = "runtime"\n[project.optional-dependencies]\nextra = "optional"\n'
        '[tool.uv]\ndev-dependencies = "development"\n',
        encoding="utf-8",
    )
    assert provision.main(["--repo", str(project), "--plan-only"]) == 0
    assert json.loads(capsys.readouterr().out)["planned"] == []


@pytest.mark.parametrize("spec", ["sample suffix", "sample[extra] suffix", "sample>=1; nonsense == 'value'"])
def test_installer_refuses_invalid_requirement_grammar(spec):
    """Prevent malformed declarations from becoming installer arguments."""
    with pytest.raises(provision._Issue, match="invalid"):
        provision._install_command(None, Path(sys.executable), [spec])


@pytest.mark.parametrize(
    ("option", "label"),
    [
        pytest.param("--extra-index=https://user:secret@index.invalid/simple", "--extra-index-url", id="long-prefix"),
        pytest.param("-ihttps://user:secret@index.invalid/simple", "--index-url", id="short-attached"),
        pytest.param('--pre "--find-links" https://user:secret@index.invalid/wheels', "--find-links", id="quoted"),
        pytest.param("-r https://user:secret@index.invalid/requirements.txt", "--requirement", id="include"),
        pytest.param("--future-option=https://user:secret@index.invalid", "requirements option", id="unknown-option"),
        pytest.param(
            "--index-url https://user:secret@index.invalid/${TOKEN}",
            "unresolved requirements variable",
            id="unresolved-variable",
        ),
    ],
)
def test_receipt_records_option_names_without_credentials(project, capsys, option, label):
    """Serialize canonical labels and causes without exposing option values."""
    (project / "requirements.txt").write_text(option + "\npublic-name\n", encoding="utf-8")
    output = project.parent / "receipt.json"
    assert provision.main(["--repo", str(project), "--plan-only", "--out", str(output)]) == 0
    stdout = capsys.readouterr().out
    assert stdout == output.read_text(encoding="utf-8")
    assert "secret" not in stdout
    assert json.loads(stdout)["skipped"][0]["requirement"] == label


@pytest.mark.integration
def test_marker_excluded_name_leaves_unseeded_environment_ready(project, capsys, monkeypatch):
    """Inspect an actual environment that has neither packaging nor pip installed."""
    root = provision.environment_root()
    bucket = root / ("a" * 64)
    bucket.mkdir(parents=True)
    venv = bucket / "venv"
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(venv)], check=True, timeout=30)
    python = provision._env_python(venv)
    subprocess.run(
        [str(python), "-I", "-c", "import importlib.util; assert importlib.util.find_spec('packaging') is None"],
        check=True,
        timeout=10,
    )
    receipt = {"planned": ["sample; python_version < '1'"], "failed": [], "import_probes": {}}
    args = provision._args([])
    assert provision._inspect(lambda argv: provision._run(argv, bucket, 10), python, args, receipt)
    assert receipt["failed"] == []
