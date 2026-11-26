"""Reviewer gate environments come from base-declared, default-index packages and live outside the checkout.

The plan trusts a package by name only when the review base already declares it with a plain version range; names the
head adds, direct references and non-range specifiers stay with the contributor-code gate. Index overrides and user
index routing block the plan. The environment root must lie outside the reviewed checkout, and a failed build records a
fixed cause instead of a generic environment limit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from argparse import Namespace
from pathlib import Path
from types import ModuleType

import pytest
from _fake_tools import fake_bin as _fake_bin
from _fake_tools import tool_calls as _calls
from _modules import load_module_scoped

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = PLUGIN_ROOT / "shared" / "gate_env_provision.py"
TRAILER = "Co-authored-by: Codex <codex@openai.com>"
_ROUTING_TABLE = json.loads(Path(__file__).with_name("routing_cases.json").read_text(encoding="utf-8"))


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the provisioning helper with its sibling guard importable."""
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    return load_module_scoped("gate_env_provision_under_test", HELPER_PATH, monkeypatch)


@pytest.fixture(autouse=True)
def routing_home(helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep inherited installer settings and user configs out of plan fixtures."""
    for name in tuple(os.environ):
        if name.startswith(("UV_", "PIP_")):
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "PROGRAMDATA"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(home / "system"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(home / "data"))
    return home


def _write_files(root: Path, files: dict[str, str]) -> None:
    """Create fixture files relative to the checkout or the caller's home."""
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _git(repository: Path, *arguments: str) -> str:
    """Run one Git command in ``repository`` and return its stripped output."""
    done = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit(repository: Path, files: dict[str, str], message: str) -> str:
    """Write ``files``, commit them and return the new head."""
    _write_files(repository, files)
    if files:
        _git(repository, "add", "--", *files)
    _git(repository, "commit", "-qm", f"{message}\n\n{TRAILER}")
    return _git(repository, "rev-parse", "HEAD")


def _pyproject(*dependencies: str, extra: str = "") -> str:
    """Return a minimal pyproject declaring ``dependencies``."""
    listed = ", ".join(json.dumps(item) for item in dependencies)
    return f'[project]\nname = "demo"\nversion = "1"\ndependencies = [{listed}]\n{extra}'


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create an initialised empty repository."""
    path = tmp_path / "checkout"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Codex Rig Test")
    _git(path, "config", "user.email", "codex-rig@example.invalid")
    return path


def _plan(helper: ModuleType, repository: Path, base: str, head: str, env_root: Path, *extra: str) -> dict:
    """Write a plan through the CLI entry point and return it."""
    out = env_root.parent / "plan.json"
    argv = ["plan", "--repo", str(repository), "--base", base, "--head", head, "--python", sys.executable]
    exit_code = helper.main([*argv, "--env-root", str(env_root), "--out", str(out), *extra])
    plan = json.loads(out.read_text(encoding="utf-8"))
    plan["exit_code"] = exit_code
    return plan


def test_trusts_only_base_declared_version_ranges(helper: ModuleType, repository: Path, tmp_path: Path) -> None:
    """Base-declared ranges are installed; added names, direct references and non-ranges stay gated."""
    base = _commit(repository, {"pyproject.toml": _pyproject("numpy>=1.20", "pytest", "requests", "tool==1")}, "base")
    head = _commit(
        repository,
        {
            "pyproject.toml": _pyproject(
                "numpy>=1.21", "pytest", "requests @ git+https://example.invalid/r.git", "tool==1 --hash=x", "newpkg"
            ),
        },
        "head",
    )

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["exit_code"] == 0
    assert plan["blocked"] is None
    assert plan["requirements"] == ["numpy>=1.21", "pytest"]
    assert {entry["name"]: entry["reason"] for entry in plan["gated"]} == {
        "newpkg": "new dependency, verify provenance",
        "requests": "non-index dependency reference",
        "tool": "head specifier not a version range",
    }
    assert Path(plan["env"]).parent == (tmp_path / "envs").resolve()
    # Every path the plan hands to a process or a caller is in the host's own form, so string prefixes compare.
    assert plan["env_root"] == str((tmp_path / "envs").resolve())
    assert plan["run_gates_args"] == ["--tool-cache", plan["env_root"]]
    assert plan["worktree_run_gates_args"] == ["--project-env", plan["env"]]
    install = next(command for command in plan["commands"] if command["id"] == "install")
    assert install["argv"][-2:] == ["numpy>=1.21", "pytest"]
    assert "--only-binary" in install["argv"]
    assert "--no-deps" in install["argv"]
    assert install["env"]["UV_CACHE_DIR"].startswith(plan["env_root"])
    assert plan["gate_environment"]["PATH_PREPEND"].startswith(plan["env"])


def test_plans_only_the_selected_lane(helper: ModuleType, repository: Path, tmp_path: Path) -> None:
    """Runtime dependencies plus the named group and extra are planned; other tables and the project itself are not."""
    extra = (
        '[project.optional-dependencies]\nopencv = ["opencv-python>=4"]\nheadless = ["opencv-python-headless>=4"]\n'
        'demo-all = ["demo[opencv]"]\n'
        '[dependency-groups]\ntest = ["pytest>=8", {include-group = "base-test"}]\nbase-test = ["pycocotools"]\n'
        'docs = ["mkdocs"]\n'
    )
    head = _commit(repository, {"pyproject.toml": _pyproject("numpy", extra=extra)}, "base")

    plan = _plan(helper, repository, head, head, tmp_path / "envs", "--group", "test", "--extra", "opencv")

    assert plan["requirements"] == ["numpy", "opencv-python>=4", "pycocotools", "pytest>=8"]
    assert plan["available_lanes"] == {
        "groups": ["base-test", "docs", "test"],
        "extras": ["demo-all", "headless", "opencv"],
    }


def test_unknown_lane_is_invalid_input(helper: ModuleType, repository: Path, tmp_path: Path) -> None:
    """A group the project does not declare cannot be planned."""
    head = _commit(repository, {"pyproject.toml": _pyproject("numpy")}, "base")
    argv = ["plan", "--repo", str(repository), "--base", head, "--head", head, "--python", sys.executable]

    assert (
        helper.main([*argv, "--env-root", str(tmp_path / "envs"), "--out", str(tmp_path / "p.json"), "--group", "x"])
        == 2
    )


def test_planned_environment_feeds_an_unbound_gate_run(helper: ModuleType, repository: Path, tmp_path: Path) -> None:
    """The plan's gate arguments and environment let an unbound gate import the uninstalled project from source."""
    (repository / "src" / "demo_pkg").mkdir(parents=True)
    head = _commit(repository, {"pyproject.toml": _pyproject(), "src/demo_pkg/__init__.py": "VALUE = 7\n"}, "base")
    plan = _plan(helper, repository, head, head, tmp_path / "envs")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", plan["env"]], check=True, capture_output=True)
    gate_env = plan["gate_environment"]
    environment = {
        **os.environ,
        "VIRTUAL_ENV": gate_env["VIRTUAL_ENV"],
        "PYTHONPATH": gate_env["PYTHONPATH"],
        "PATH": os.pathsep.join((gate_env["PATH_PREPEND"], os.environ.get("PATH", ""))),
    }
    probe = 'python -c "import sys, demo_pkg; print(sys.prefix); print(demo_pkg.VALUE)"'
    skipped = [part for gate in ("lint", "format", "types", "review") for part in (f"--skip-{gate}", "out of scope")]
    output = tmp_path / "gates"
    argv = [sys.executable, str(PLUGIN_ROOT / "shared" / "run_gates.py"), "--out", str(output), *plan["run_gates_args"]]

    done = subprocess.run(
        [*argv, "--tests", probe, *skipped],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    gates = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    assert Path(gates["tool_cache"]) == Path(plan["env_root"])
    printed = (output / "checks" / "tests.stdout.txt").read_text(encoding="utf-8").split()
    assert (Path(printed[0]).resolve(), printed[1]) == (Path(plan["env"]).resolve(), "7")


def test_index_override_blocks_the_plan(helper: ModuleType, repository: Path, tmp_path: Path) -> None:
    """A head that routes packages to another index cannot be provisioned from the default index."""
    base = _commit(repository, {"pyproject.toml": _pyproject("numpy")}, "base")
    extra = '[tool.uv]\nindex-url = "https://mirror.invalid/simple"\n'
    head = _commit(repository, {"pyproject.toml": _pyproject("numpy", extra=extra)}, "head")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["exit_code"] == 1
    assert plan["blocked"]["cause"] == "index-override"


def test_user_index_routing_blocks_without_recording_its_value(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User routing is named by variable only, since its value can carry credentials."""
    base = _commit(repository, {"pyproject.toml": _pyproject("numpy")}, "base")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:secret@mirror.invalid/simple")

    plan = _plan(helper, repository, base, base, tmp_path / "envs")

    assert plan["blocked"] == {"cause": "user-index-routing", "detail": "PIP_INDEX_URL"}
    assert "secret" not in json.dumps(plan)


@pytest.mark.parametrize(
    "variable",
    [
        "UV_INDEX",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "PIP_FIND_LINKS",
        "PIP_NO_INDEX",
        "PIP_CONFIG_FILE",
        "UV_CONFIG_FILE",
    ],
)
def test_index_environment_presence_blocks_even_with_an_empty_value(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """An empty index setting or config override still fails the default-index provenance check."""
    head = _commit(repository, {"pyproject.toml": _pyproject("numpy")}, "base")
    monkeypatch.setenv(variable, "")

    plan = _plan(helper, repository, head, head, tmp_path / "envs")

    assert plan["exit_code"] == 1
    assert plan["blocked"] == {"cause": "user-index-routing", "detail": variable}


def test_windows_discovers_legacy_user_config_and_programdata(helper: ModuleType, routing_home: Path) -> None:
    """Check native Windows discovery with explicit user and system directories on every test host."""
    machine = routing_home / "machine"
    roaming = routing_home / "roaming"
    environment = {"APPDATA": str(roaming), "PROGRAMDATA": str(machine)}

    files = helper._user_config_files(environment, routing_home, "win32")

    assert {
        routing_home / "pip/pip.ini", roaming / "pip/pip.ini", roaming / "uv/uv.toml",
        machine / "pip/pip.ini", machine / "uv/uv.toml",
    } <= set(files)  # fmt: skip


def test_environment_root_inside_the_checkout_is_refused(helper: ModuleType, repository: Path) -> None:
    """The reviewer environment never lives among the reviewed inputs."""
    head = _commit(repository, {"pyproject.toml": _pyproject("numpy")}, "base")
    argv = ["plan", "--repo", str(repository), "--base", head, "--head", head, "--python", sys.executable]

    with pytest.raises(SystemExit) as raised:
        helper.main([*argv, "--env-root", str(repository / ".gate-env"), "--out", str(repository.parent / "p.json")])

    assert raised.value.code == 2


def test_changed_hook_configuration_is_not_seeded(helper: ModuleType, repository: Path, tmp_path: Path) -> None:
    """Hook repositories chosen by the reviewed change are never installed by the provisioning step."""
    base = _commit(repository, {"pyproject.toml": _pyproject("numpy"), ".pre-commit-config.yaml": "repos: []\n"}, "a")
    head = _commit(repository, {".pre-commit-config.yaml": "repos: [{repo: local, hooks: []}]\n"}, "b")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["hooks"]["cause"] == "hook-config-changed"
    assert all(command["id"] != "hooks" for command in plan["commands"])


@pytest.fixture
def host_case(request: pytest.FixtureRequest, routing_home: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Populate the case's host config and index environment before planning."""
    case = request.param
    _write_files(routing_home, case["home_files"])
    for name, value in case["env"].items():
        monkeypatch.setenv(name, value)
    return case


@pytest.mark.parametrize(
    "host_case",
    [pytest.param(case, id=case["name"]) for case in _ROUTING_TABLE["host_cases"] if "codex" in case["readers"]],
    indirect=True,
)
def test_host_routing_controls_default_index_plan(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_case: dict
) -> None:
    """Allow both default-index installs on an ordinary host and block both when the host routes indexes."""
    fake_bin = _fake_bin(tmp_path / "bin", "uv")
    monkeypatch.setenv("PATH", os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))))
    head = _commit(repository, host_case["files"], "host routing")

    plan = _plan(helper, repository, head, head, tmp_path / "envs")

    expected = host_case["expect"]
    ordinary_names = {_ROUTING_TABLE["private_name"], _ROUTING_TABLE["public_name"]}
    runnable = plan["requirements"] if plan["blocked"] is None else []
    expected_names = {"ordinary": ordinary_names, "all-routed": set()}
    assert {requirement.partition("==")[0] for requirement in runnable} == expected_names[expected]
    assert (plan["blocked"] or {}).get("cause") == {"ordinary": None, "all-routed": "user-index-routing"}[expected]
    assert plan["exit_code"] == int(expected == "all-routed")
    assert _calls(fake_bin) == []


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(case["files"], case["expect"], id=case["name"])
        for case in _ROUTING_TABLE["cases"]
        if "codex" in case["readers"]
    ],
)
def test_project_routing_controls_default_index_plan(
    helper: ModuleType,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
    expected: str,
) -> None:
    """Plan both names, skip the bound name, or refuse based on the actual project files."""
    fake_bin = _fake_bin(tmp_path / "bin", "uv")
    monkeypatch.setenv("PATH", os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))))
    head = _commit(repository, files, "project routing")

    plan = _plan(helper, repository, head, head, tmp_path / "envs")

    private, public = _ROUTING_TABLE["private_name"], _ROUTING_TABLE["public_name"]
    expected_names = {"ordinary": {private, public}, "name-routed": {public}, "all-routed": set()}
    runnable = plan["requirements"] if plan["blocked"] is None else []
    assert {requirement.partition("==")[0] for requirement in runnable} == expected_names[expected]
    assert plan["exit_code"] == int(expected == "all-routed")
    expected_gates = [{"name": private, "reason": "index-routing"}] if expected == "name-routed" else []
    assert plan["gated"] == expected_gates
    assert _calls(fake_bin) == []


def test_head_source_binding_gates_only_its_name_across_manifests(
    helper: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """A named source introduced in the head also applies to declarations in another file."""
    requirements = "example-pkg==1\nrequests==2\n"
    base = _commit(repository, {"pyproject.toml": _pyproject(), "requirements.txt": requirements}, "base")
    head = _commit(repository, {"uv.toml": '[sources]\nExample_Pkg = { path = "./example" }\n'}, "source")

    plan = _plan(helper, repository, base, head, tmp_path / "envs", "--requirements", "requirements.txt")

    assert plan["blocked"] is None
    assert plan["requirements"] == ["requests==2"]
    assert plan["gated"] == [{"name": "example-pkg", "reason": "index-routing"}]


@pytest.mark.parametrize(
    ("source", "expected", "cause"),
    [
        pytest.param('{registry = "https://pypi.org/simple/"}', ["internal-pkg==1", "requests==2"], None, id="pypi"),
        pytest.param('{registry = "https://user:secret@mirror.invalid/simple"}', [], "index-override", id="mirror"),
        pytest.param('{git = "https://git.invalid/pkg"}', ["requests==2"], None, id="git"),
        pytest.param('{url = "https://mirror.invalid/pkg.whl"}', ["requests==2"], None, id="url"),
        pytest.param('{path = "local/pkg.whl"}', ["requests==2"], None, id="path"),
        pytest.param('{directory = "local/pkg"}', ["requests==2"], None, id="local-directory"),
        pytest.param('{editable = "local/pkg"}', ["requests==2"], None, id="editable"),
        pytest.param('{virtual = "local/pkg"}', ["requests==2"], None, id="workspace"),
        pytest.param('{new_source_kind = "other"}', ["requests==2"], None, id="unknown-named-source"),
        pytest.param("not-toml [", [], "dependency-unparsed", id="unparsed"),
    ],
)
def test_locked_sources_bound_default_index_plan(
    helper: ModuleType, repository: Path, tmp_path: Path, source: str, expected: list[str], cause: str | None
) -> None:
    """Only a positively identified PyPI source retains eligibility for the locked name."""
    base = _commit(repository, {"pyproject.toml": _pyproject("internal-pkg==1", "requests==2")}, "base")
    lockfile = f'version = 1\n[[package]]\nname = "Internal_Pkg"\nversion = "1"\nsource = {source}\n'
    head = _commit(repository, {"uv.lock": lockfile}, "lock")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert (plan["blocked"] or {}).get("cause") == cause
    assert ([] if plan["blocked"] else plan["requirements"]) == expected
    assert "secret" not in json.dumps(plan)


def test_base_lock_source_still_gates_after_head_switches_to_pypi(
    helper: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """Changing an existing lock to PyPI cannot grant provenance the base did not establish."""
    lock_start = 'version = 1\n[[package]]\nname = "internal-pkg"\nversion = "1"\nsource = '
    base = _commit(
        repository,
        {
            "pyproject.toml": _pyproject("internal-pkg==1", "requests==2"),
            "uv.lock": lock_start + '{path = "local/pkg.whl"}\n',
        },
        "base",
    )
    head = _commit(repository, {"uv.lock": lock_start + '{registry = "https://pypi.org/simple"}\n'}, "head")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["blocked"] is None
    assert plan["requirements"] == ["requests==2"]
    assert plan["gated"] == [{"name": "internal-pkg", "reason": "index-routing"}]


def test_only_python_declarations_grant_python_install_trust(
    helper: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """A same-named npm declaration cannot authorize a Python install."""
    base = _commit(repository, {"package.json": '{"devDependencies": {"packaging": "^1"}}'}, "npm")
    head = _commit(repository, {"pyproject.toml": _pyproject("packaging>=24")}, "python")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["blocked"] is None
    assert plan["requirements"] == []
    assert plan["gated"] == [{"name": "packaging", "reason": "new dependency, verify provenance"}]


def test_selected_interpreter_site_config_blocks_default_index_plan(
    helper: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """Pip's site configuration belongs to the selected interpreter's prefix."""
    head = _commit(repository, {"pyproject.toml": _pyproject("requests")}, "base")
    selection = tmp_path / "selection"
    venv.EnvBuilder(symlinks=os.name != "nt").create(selection)
    selected = selection / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    config = selection / ("pip.ini" if os.name == "nt" else "pip.conf")
    config.write_text("[install]\nindex-url = https://user:secret@mirror.invalid/simple\n", encoding="utf-8")

    plan = _plan(helper, repository, head, head, tmp_path / "envs", "--python", str(selected))

    assert plan["blocked"]["cause"] == "user-index-routing"
    assert str(config) in plan["blocked"]["detail"]
    assert "secret" not in json.dumps(plan)


@pytest.mark.parametrize("variable", ["UV_OVERRIDE", "UV_CONSTRAINT", "UV_FUTURE_RESOLUTION_SETTING"])
def test_unmodeled_uv_environment_blocks_default_index_plan(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Unknown uv resolution settings, including empty overrides, block the plan by name."""
    head = _commit(repository, {"pyproject.toml": _pyproject("requests")}, "base")
    monkeypatch.setenv(variable, "")

    plan = _plan(helper, repository, head, head, tmp_path / "envs")

    assert plan["blocked"] == {"cause": "user-index-routing", "detail": variable}


def test_removed_base_index_config_still_refuses_default_index_provisioning(
    helper: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """Removing an index file does not establish default-index provenance for the base declarations."""
    files = {"pyproject.toml": _pyproject("example-pkg==1"), "uv.toml": 'index-url = "https://mirror.invalid/simple"\n'}
    base = _commit(repository, files, "base")
    _git(repository, "rm", "uv.toml")
    head = _commit(repository, {}, "remove installer config")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["exit_code"] == 1
    assert plan["blocked"]["cause"] == "base-index-routing"


@pytest.mark.parametrize(
    ("path", "option", "cause"),
    [
        pytest.param("requirements.txt", "-r custom.txt", "requirement-include", id="base-include"),
        pytest.param("lane.txt", "--index-url https://mirror.invalid/simple", "index-override", id="lane-index"),
        pytest.param("lane.txt", "-c custom.txt", "requirement-include", id="lane-constraint"),
    ],
)
def test_requirement_routing_refuses_the_lane(
    helper: ModuleType,
    repository: Path,
    tmp_path: Path,
    path: str,
    option: str,
    cause: str,
) -> None:
    """Check options from an unchanged base manifest and explicitly selected requirements files."""
    head = _commit(repository, {"pyproject.toml": _pyproject("example-pkg==1"), path: f"{option}\n"}, "base")

    plan = _plan(helper, repository, head, head, tmp_path / "envs", "--requirements", path)

    assert plan["exit_code"] == 1
    assert plan["blocked"]["cause"] == cause


def test_removed_routing_option_in_selected_base_requirements_refuses_the_lane(
    helper: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """Inspect a selected base requirements file even if its name is outside manifest discovery."""
    base = _commit(
        repository,
        {"pyproject.toml": _pyproject("example-pkg==1"), "lane.txt": "--index-url https://mirror.invalid/simple\n"},
        "base",
    )
    head = _commit(repository, {"lane.txt": "example-pkg==1\n"}, "remove lane routing")

    plan = _plan(helper, repository, base, head, tmp_path / "envs", "--requirements", "lane.txt")

    assert plan["exit_code"] == 1
    assert plan["blocked"]["cause"] == "base-index-routing"


@pytest.mark.parametrize(
    "suffix",
    ["x", "[extra] x", ">=word", ">=1; made_up_marker == 'linux'", ">=1;"],
)
def test_requirements_preserve_the_declared_distribution(
    helper: ModuleType, repository: Path, tmp_path: Path, suffix: str
) -> None:
    """Reject an invalid declaration without assembling it into an installer argument."""
    base = _commit(repository, {"pyproject.toml": _pyproject("Example_Pkg")}, "base")
    head = _commit(repository, {"pyproject.toml": _pyproject(f"Example_Pkg {suffix}")}, "head")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["requirements"] == []
    assert all(step["id"] != "install" for step in plan["commands"])
    assert plan["blocked"] or plan["gated"] == [{"name": "example-pkg", "reason": "head specifier not a version range"}]


@pytest.mark.parametrize(
    ("path", "content", "cause"),
    [
        pytest.param(
            "tox.ini", "[testenv]\ninstall_command = pip install --index-url mirror", "index-override", id="tox-ini"
        ),
        pytest.param(
            ".github/workflows/test.yml", "run: pip install --extra-index-url mirror", "index-override", id="ci"
        ),
        pytest.param(
            "package.json",
            '{"scripts":{"test":"uv pip install --index-url mirror"}}',
            "index-override",
            id="npm-script",
        ),
        pytest.param(
            "Pipfile.lock",
            '{"_meta":{"sources":[{"url":"https://mirror.invalid/simple"}]}}',
            "index-override",
            id="pipfile-lock",
        ),
        pytest.param("requirements/test.txt", "--index-url mirror\n", "index-override", id="requirements-directory"),
        pytest.param(
            "requirements.txt",
            "https://mirror.invalid/example_pkg-1-py3-none-any.whl\n",
            "index-override",
            id="bare-wheel",
        ),
        pytest.param("requirements.txt", "\ufeff--no-index\n", "index-override", id="bom"),
        pytest.param("requirements.txt", '--pre "--index-url=mirror"\n', "index-override", id="quoted-option"),
        pytest.param("requirements.txt", '--pre "-c" pins.in\n', "requirement-include", id="quoted-constraint"),
    ],
)
def test_project_installer_routing_refuses_default_index(
    helper: ModuleType, repository: Path, tmp_path: Path, path: str, content: str, cause: str
) -> None:
    """Refuse a default-index plan when the head establishes a route the planner cannot honor."""
    base = _commit(repository, {"pyproject.toml": _pyproject("example-pkg==1", "requests==2")}, "base")
    head = _commit(repository, {path: content}, "head")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["blocked"]["cause"] == cause
    assert plan["requirements"] == []
    assert all(step["id"] != "install" for step in plan["commands"])


def test_tox_tables_refuse_default_index(helper: ModuleType, repository: Path, tmp_path: Path) -> None:
    """A test runner's install configuration can route Python packages."""
    base = _commit(repository, {"pyproject.toml": _pyproject("example-pkg==1")}, "base")
    settings = '\n[tool.tox.env_run_base.set_env]\nPIP_INDEX_URL = "https://mirror.invalid/simple"\n'
    head = _commit(repository, {"pyproject.toml": _pyproject("example-pkg==1", extra=settings)}, "head")

    plan = _plan(helper, repository, base, head, tmp_path / "envs")

    assert plan["blocked"]["cause"] == "index-override"
    assert plan["requirements"] == []


def test_runtime_plain_requirement_cannot_shadow_selected_direct_source(
    helper: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """Keep explicit source bindings even when the runtime lane repeats the name."""
    base = _commit(
        repository,
        {"pyproject.toml": _pyproject("example-pkg==1", "requests==2"), "requirements.txt": "example-pkg==1"},
        "base",
    )
    head = _commit(repository, {"requirements.txt": "example-pkg @ https://mirror.invalid/pkg.whl"}, "head")

    plan = _plan(helper, repository, base, head, tmp_path / "envs", "--requirements", "requirements.txt")

    assert plan["blocked"] is None
    assert plan["requirements"] == ["requests==2"]
    assert plan["gated"] == [{"name": "example-pkg", "reason": "non-index dependency reference"}]


@pytest.mark.parametrize(
    "variable",
    ["PIP_PYPI_URL", "PIP_REQUIREMENT", "PIP_CONSTRAINT", "PIP_FUTURE_RESOLUTION_SETTING"],
)
def test_unmodeled_pip_environment_refuses_default_index(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Report the name of a potentially routing pip setting without its value."""
    head = _commit(repository, {"pyproject.toml": _pyproject("example-pkg")}, "base")
    monkeypatch.setenv(variable, "sensitive-value")

    plan = _plan(helper, repository, head, head, tmp_path / "envs")

    assert plan["blocked"] == {"cause": "user-index-routing", "detail": variable}
    assert "sensitive-value" not in json.dumps(plan)


@pytest.mark.parametrize(
    ("path", "text"),
    [
        # uv reads machine config from XDG_CONFIG_DIRS on POSIX and from ProgramData on Windows, rooted at home here.
        pytest.param(
            "uv/uv.toml" if sys.platform == "win32" else "system/uv/uv.toml",
            'index-url = "https://mirror.invalid/simple"',
            id="machine-uv",
        ),
        pytest.param(".config/pip/pip.conf", "[install]\nrequirement = custom.txt", id="pip-requirement"),
        pytest.param(".config/pip/pip.conf", "[install]\nconstraint = custom.txt", id="pip-constraint"),
        pytest.param(".config/pip/pip.conf", "[install]\nfuture-setting = custom.txt", id="pip-unknown"),
    ],
)
def test_host_files_refuse_default_index(
    helper: ModuleType, repository: Path, tmp_path: Path, routing_home: Path, path: str, text: str
) -> None:
    """Block host-side settings that can introduce an alternate source or include."""
    head = _commit(repository, {"pyproject.toml": _pyproject("example-pkg")}, "base")
    _write_files(routing_home, {path: text})

    plan = _plan(helper, repository, head, head, tmp_path / "envs")

    assert plan["blocked"] == {"cause": "user-index-routing", "detail": str(routing_home / path)}
    assert plan["requirements"] == []


@pytest.mark.skipif(sys.platform != "darwin", reason="pip discovers XDG data directories as site config on macOS")
def test_macos_pip_config_discovery_reads_xdg_data_dirs(
    helper: ModuleType, repository: Path, tmp_path: Path, routing_home: Path
) -> None:
    """Use pip's own global config discovery for the selected interpreter on macOS."""
    head = _commit(repository, {"pyproject.toml": _pyproject("example-pkg")}, "base")
    _write_files(routing_home, {"data/pip/pip.conf": "[install]\nindex-url = https://mirror.invalid/simple"})

    plan = _plan(helper, repository, head, head, tmp_path / "envs")

    assert plan["blocked"] == {"cause": "user-index-routing", "detail": str(routing_home / "data/pip/pip.conf")}
    assert plan["requirements"] == []


def _planned_install(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **fake: object
) -> tuple[dict, Path, Path]:
    """Plan a one-package install with fake ``uv`` and ``pre-commit`` first on ``PATH``; return plan, file and bin."""
    head = _commit(repository, {"pyproject.toml": _pyproject("numpy")}, "base")
    fake_bin = _fake_bin(tmp_path / "bin", "uv", "pre-commit", **fake)  # type: ignore[arg-type]
    monkeypatch.setenv("PATH", os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))))
    plan = _plan(helper, repository, head, head, tmp_path / "envs")
    plan.pop("exit_code")
    return plan, tmp_path / "plan.json", fake_bin


def _approval(plan: dict, **overrides: str) -> Namespace:
    """Record the apply arguments and digest independently of later edits to the plan."""
    approved = {key: plan[key] for key in ("repo", "base", "head", "python", "env_root", "plan_digest")}
    approved.update(overrides)
    return Namespace(
        repo=Path(approved["repo"]), base=approved["base"], head=approved["head"], python=Path(approved["python"]),
        env_root=Path(approved["env_root"]), plan_digest=approved["plan_digest"], timeout_seconds=60,
    )  # fmt: skip


def _apply_argv(approved: Namespace, plan_file: Path, out: Path) -> list[str]:
    """Pass the approval as literal arguments to the CLI."""
    return [
        "apply", "--repo", str(approved.repo), "--base", approved.base, "--head", approved.head,
        "--python", str(approved.python), "--env-root", str(approved.env_root), "--plan-digest", approved.plan_digest,
        "--plan", str(plan_file), "--out", str(out),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("output", "cause"),
    [
        pytest.param("error: Failed to fetch: dns error: failed to lookup address", "network-unavailable", id="dns"),
        pytest.param("No solution found when resolving dependencies", "resolver-conflict", id="conflict"),
        pytest.param("numpy==2.0 has no wheels with a matching platform tag", "no-compatible-wheel", id="wheel"),
    ],
)
def test_failed_install_records_its_cause(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str, cause: str
) -> None:
    """A failed build names a fixed cause from the installer's own diagnostic and stops at that step."""
    plan, _, _ = _planned_install(helper, repository, tmp_path, monkeypatch, output=output, exit_code=2)

    record = helper.apply_plan(plan, _approval(plan))

    assert (record["status"], record["cause"]) == ("failed", cause)
    assert [step["id"] for step in record["steps"]] == ["venv", "install"]
    assert record["steps"][-1]["exit_code"] == 2


def test_missing_installer_is_named(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent uv executable is its own cause, not a generic failure."""
    head = _commit(repository, {"pyproject.toml": _pyproject("numpy")}, "base")
    monkeypatch.setenv("PATH", str(_fake_bin(tmp_path / "empty-bin")))
    plan = _plan(helper, repository, head, head, tmp_path / "envs")
    plan.pop("exit_code")

    assert helper.apply_plan(plan, _approval(plan))["cause"] == "uv-unavailable"


@pytest.mark.parametrize(
    ("recompute_digest", "approve_new_digest"),
    [
        pytest.param(False, False, id="digest-kept"),
        pytest.param(True, False, id="digest-recomputed"),
        pytest.param(True, True, id="approved-plan-still-rederived"),
    ],
)
def test_apply_runs_only_the_commands_the_plan_derived(
    helper: ModuleType,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recompute_digest: bool,
    approve_new_digest: bool,
) -> None:
    """An edited plan file is refused before any command runs, even when the editor also recomputes its digest."""
    plan, _, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    approved = _approval(plan)
    marker = tmp_path / "planted-command-ran"
    plan["commands"][0]["argv"] = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    if recompute_digest:
        plan["plan_digest"] = helper.plan_digest(plan)

    record = helper.apply_plan(plan, _approval(plan) if approve_new_digest else approved)

    assert (record["status"], record["cause"]) == ("blocked", "plan-modified")
    assert not marker.exists()
    assert _calls(fake_bin) == []


def test_apply_builds_the_unedited_plan(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan written by ``plan`` and read back from disk applies as written."""
    plan, plan_file, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    out = tmp_path / "run" / "apply.json"

    assert helper.main(_apply_argv(_approval(plan), plan_file, out)) == 0
    assert [call["argv"][:2] for call in _calls(fake_bin)] == [["venv", "--no-config"], ["pip", "install"]]


@pytest.mark.parametrize("field", ["repo", "base", "head", "python", "env_root"])
def test_apply_rejects_changed_inputs_before_running_or_making_directories(
    helper: ModuleType,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    """A valid digest never overrides an individual trust input from the approved command."""
    plan, plan_file, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    executables = _fake_bin(tmp_path / "interpreters", "other-python")
    interpreter = executables / ("other-python.cmd" if os.name == "nt" else "other-python")
    alternatives = {
        "repo": str(tmp_path / "other-checkout"), "base": "other-base", "head": "other-head",
        "python": str(interpreter), "env_root": str(tmp_path / "other-env-root"),
    }  # fmt: skip
    approved = _approval(plan, **{field: alternatives[field]})
    capsys.readouterr()
    out = tmp_path / "missing-run" / "apply.json"
    assert helper.main(_apply_argv(approved, plan_file, out)) == 1
    assert json.loads(capsys.readouterr().out)["cause"] == "plan-modified"
    assert _calls(executables) == _calls(fake_bin) == []
    # Planning already created the private root; a refused apply adds nothing to it.
    assert not Path(plan["env"]).exists()
    assert sorted(Path(plan["env_root"]).iterdir()) == []
    assert approved.env_root == Path(plan["env_root"]) or not approved.env_root.exists()
    assert not out.parent.exists()


def test_apply_refuses_when_host_routing_appears_after_planning(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routing_home: Path
) -> None:
    """Apply checks the current host config against the approved plan before invoking uv."""
    plan, _, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    _write_files(routing_home, {".config/pip/pip.conf": "[global]\nfind-links = https://mirror.invalid/wheels\n"})

    result = helper.apply_plan(plan, _approval(plan))

    assert result["status"] == "blocked"
    assert result["cause"] == "plan-modified"
    assert _calls(fake_bin) == []


def test_apply_rejects_changed_lane_even_when_the_plan_is_rederived(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement plan for the same checkout and interpreter still needs its own approval."""
    plan, plan_file, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    _plan(helper, repository, plan["base"], plan["head"], Path(plan["env_root"]), "--exclude", "numpy")

    out = tmp_path / "apply.json"
    assert helper.main(_apply_argv(_approval(plan), plan_file, out)) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["cause"] == "plan-modified"
    assert _calls(fake_bin) == []


def test_installer_environment_is_an_allowlist(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uv resolution overrides and unrelated secrets in the caller's environment never reach the installer."""
    plan, _, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    for name in ("UV_CACHE_DIR", "UV_PYTHON", "UV_NO_CONFIG", "SERVICE_TOKEN"):
        monkeypatch.setenv(name, "set-by-caller")

    assert helper.apply_plan(plan, _approval(plan))["status"] == "ready"
    environments = [call["env"] for call in _calls(fake_bin)]
    assert environments
    assert all(not {"UV_PYTHON", "UV_NO_CONFIG", "SERVICE_TOKEN"} & set(env)
               for env in environments)  # fmt: skip
    assert all(env["UV_CACHE_DIR"].startswith(plan["env_root"]) for env in environments)


def test_reused_environment_needs_its_interpreter(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached ``pyvenv.cfg`` without an interpreter is rebuilt, never reported as a ready reused environment."""
    plan, _, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    Path(plan["env"]).mkdir(parents=True)
    (Path(plan["env"]) / "pyvenv.cfg").write_text("home = /nowhere\n", encoding="utf-8")

    record = helper.apply_plan(plan, _approval(plan))

    assert record["status"] == "ready"
    assert record["steps"][0]["id"] == "venv"
    assert record["steps"][0].get("status") != "reused"
    assert [call["argv"][0] for call in _calls(fake_bin)][:1] == ["venv"]


def test_cache_key_binds_the_full_version_and_selected_executable(
    helper: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """Distinct selected executables do not share cache keys even when they run the same Python version."""
    head = _commit(repository, {"pyproject.toml": _pyproject("requests")}, "base")
    selection = tmp_path / "selection"
    venv.EnvBuilder(symlinks=os.name != "nt").create(selection)
    selected = selection / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    first = _plan(helper, repository, head, head, tmp_path / "envs")
    second = _plan(helper, repository, head, head, tmp_path / "envs", "--python", str(selected))

    assert first["env"] != second["env"]
    assert first["python_version"] == second["python_version"] == ".".join(map(str, sys.version_info[:3]))


def test_cached_environment_must_run_as_the_planned_venv(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files in a cached directory do not prove the launcher can run Python as that environment."""
    plan, _, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    env_python = Path(plan["env_python"])
    env_python.parent.mkdir(parents=True)
    env_python.write_text("not an executable", encoding="utf-8")
    (Path(plan["env"]) / "pyvenv.cfg").write_text("home = nowhere\n", encoding="utf-8")

    record = helper.apply_plan(plan, _approval(plan))

    assert record["steps"][0].get("status") != "reused"
    assert _calls(fake_bin)[0]["argv"][0] == "venv"


def test_cache_validation_skips_installed_python_startup_hooks(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reusing a real cached venv does not execute site-packages startup hooks."""
    plan, _, _ = _planned_install(helper, repository, tmp_path, monkeypatch)
    assert helper.apply_plan(plan, _approval(plan))["status"] == "ready"
    done = subprocess.run(
        [plan["env_python"], "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    marker = tmp_path / "startup-hook-ran"
    (Path(done.stdout.strip()) / "probe.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n", encoding="utf-8"
    )

    record = helper.apply_plan(plan, _approval(plan))

    assert record["steps"][0] == {"id": "venv", "status": "reused"}
    assert not marker.exists()


def test_hooks_are_seeded_from_the_compared_configuration(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook cache is seeded from the base configuration bytes, not a working-tree file edited after the plan."""
    config = "repos: []\n"
    head = _commit(repository, {"pyproject.toml": _pyproject(), ".pre-commit-config.yaml": config}, "base")
    fake_bin = _fake_bin(tmp_path / "bin", "uv", "pre-commit")
    monkeypatch.setenv("PATH", os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))))
    plan = _plan(helper, repository, head, head, tmp_path / "envs")
    plan.pop("exit_code")
    (repository / ".pre-commit-config.yaml").write_text("repos: [{repo: local, hooks: []}]\n", encoding="utf-8")

    record = helper.apply_plan(plan, _approval(plan))

    assert record["status"] == "ready"
    hooks = next(call for call in _calls(fake_bin) if call["tool"] == "pre-commit")
    config_path = Path(hooks["argv"][hooks["argv"].index("--config") + 1])
    assert not config_path.resolve().is_relative_to(repository.resolve())
    assert (fake_bin / "hook-config-seen.yaml").read_text(encoding="utf-8") == config


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and permission bits")
@pytest.mark.parametrize("unsafe", ["group-writable", "foreign-owner", "symlink"])
def test_environment_root_not_private_to_the_caller_is_refused(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    """An environment root another user owns, can write, or redirects through a link is never planned into."""
    head = _commit(repository, {"pyproject.toml": _pyproject("numpy")}, "base")
    env_root = tmp_path / "envs"
    if unsafe == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        env_root.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    else:
        env_root.mkdir(mode=0o700)
    if unsafe == "group-writable":
        env_root.chmod(0o777)
    if unsafe == "foreign-owner":
        other_user = os.getuid() + 1
        monkeypatch.setattr(helper.os, "getuid", lambda: other_user)
    argv = ["plan", "--repo", str(repository), "--base", head, "--head", head, "--python", sys.executable]

    with pytest.raises(SystemExit) as raised:
        helper.main([*argv, "--env-root", str(env_root), "--out", str(tmp_path / "p.json")])

    assert raised.value.code == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and permission bits")
def test_environment_writable_by_others_is_not_reused(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached environment another user could have written into is refused before its interpreter runs.

    The root lets others enter (``0755``), so the environment's own write bits decide.
    """
    plan, _, fake_bin = _planned_install(helper, repository, tmp_path, monkeypatch)
    _fake_bin(Path(plan["env"]) / ("Scripts" if os.name == "nt" else "bin"), "python")
    (Path(plan["env"]) / "pyvenv.cfg").write_text("home = /nowhere\n", encoding="utf-8")
    Path(plan["env_root"]).chmod(0o755)
    Path(plan["env"]).chmod(0o777)

    record = helper.apply_plan(plan, _approval(plan))

    assert (record["status"], record["cause"]) == ("failed", "env-root-unsafe")
    assert _calls(fake_bin) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask and permission bits")
def test_environment_built_under_a_group_writable_umask_is_reused(
    helper: ModuleType, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second apply reuses the environment the first built, even when a ``002`` umask left it group-writable.

    The owner-only root keeps every other user out, so the environment's own group write bit cannot let anyone in.
    """
    previous = os.umask(0o002)
    try:
        plan, _, _ = _planned_install(helper, repository, tmp_path, monkeypatch)
        first = helper.apply_plan(plan, _approval(plan))
        second = helper.apply_plan(plan, _approval(plan))
    finally:
        os.umask(previous)

    assert Path(plan["env"]).stat().st_mode & 0o020
    assert (first["status"], second["status"]) == ("ready", "ready")
    assert second["steps"][0] == {"id": "venv", "status": "reused"}
