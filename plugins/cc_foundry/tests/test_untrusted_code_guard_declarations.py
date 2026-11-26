"""Compare direct dependency references within and across manifests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import untrusted_code_guard as guard

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}  # fmt: skip
_DIRECT = "numpy @ git+https://example.invalid/numpy.git\n"


@pytest.mark.parametrize(
    ("path", "contents"),
    [
        pytest.param("pyproject.toml", "[tool.uv", id="pyproject"),
        pytest.param("uv.toml", "index = [", id="uv-config"),
        pytest.param("Pipfile", "[[source]", id="pipfile"),
        pytest.param("pip.conf", "[global", id="pip-conf"),
        pytest.param("pip.ini", "[install", id="pip-ini"),
        pytest.param("setup.cfg", "[options", id="setuptools-config"),
    ],
)
def test_unparsed_installer_config_has_a_global_mark(path: str, contents: str) -> None:
    """A present but unreadable routing config cannot establish default-index provenance."""
    _, marks, parsed = guard._read_dependency_file(path, contents)

    assert not parsed
    assert {label for label, _ in marks} == {f"{path.lower()}: unparsed"}


@pytest.mark.skipif(guard._toml_loads("a = 1") is None, reason="needs a TOML reader (Python 3.11+ or tomli)")
@pytest.mark.parametrize(
    ("path", "contents", "mark"),
    [
        pytest.param("pyproject.toml", "[tool.uv]\npackage = false", None, id="uv-package-setting"),
        pytest.param("uv.toml", "managed = false\n[pip]\npackage = false", None, id="uv-non-routing-settings"),
        pytest.param("pyproject.toml", "[tool.uv]\nnew-setting = false", "[tool.uv].new-setting", id="uv-unknown"),
        pytest.param(
            "pyproject.toml",
            "[tool.uv]\nconstraint-dependencies = []",
            "[tool.uv].constraint-dependencies",
            id="uv-constraints",
        ),
        pytest.param(
            "pyproject.toml",
            '[tool.uv.pip]\nfind-links = ["./wheels"]',
            "[tool.uv].pip.find-links",
            id="uv-pip-routing",
        ),
        pytest.param("uv.toml", "[pip]\nnew-setting = false", "uv.toml.pip.new-setting", id="uv-pip-unknown"),
        pytest.param("uv.toml", "pip = false", "uv.toml.pip: invalid settings", id="uv-pip-invalid"),
        pytest.param(
            "uv.toml", "index = [{explicit = true, default = true}]", "uv.toml.index", id="uv-explicit-default"
        ),
        pytest.param("uv.toml", "index = [{default = false}]", "uv.toml.index", id="uv-non-explicit-index"),
        pytest.param(
            "uv.toml",
            "index = [{explicit = true}]",
            guard.UV_EXPLICIT_INDEX_MARK + "uv.toml.index",
            id="uv-unbound-explicit-index",
        ),
        pytest.param(
            "uv.toml",
            '[sources]\nExample_Pkg = { path = "./example" }',
            "uv-source: example-pkg",
            id="uv-normalized-source-binding",
        ),
        pytest.param(
            "pyproject.toml",
            '[tool.poetry.dependencies]\nexample = {path = "../local"}',
            "[tool.poetry]: installer settings are not modeled",
            id="poetry-dependency-table",
        ),
        pytest.param(
            "pyproject.toml",
            "[tool.rye]\nmanaged = true",
            "[tool.rye]: installer settings are not modeled",
            id="rye-installer-table",
        ),
        pytest.param(
            "Pipfile",
            '[packages]\nexample = { index = "local" }',
            "Pipfile: index or source settings",
            id="pipfile-package-index",
        ),
        pytest.param(
            "Pipfile",
            '[dev-packages]\nexample = {path = "../local"}',
            "Pipfile: index or source settings",
            id="pipfile-dev-package-path",
        ),
        pytest.param(
            "Pipfile",
            '[docs]\nexample = {url = "https://mirror.invalid/example.whl"}',
            "Pipfile: index or source settings",
            id="pipfile-custom-group-url",
        ),
        pytest.param(
            "Pipfile",
            '[packages]\nexample = {version = "*", extras = ["docs"]}',
            None,
            id="pipfile-version-table",
        ),
        pytest.param("Pipfile", '[packages]\nsource = "*"\nindex = "*"', None, id="pipfile-package-names"),
    ],
)
def test_installer_config_marks_routing_by_scope(path: str, contents: str, mark: str | None) -> None:
    """Only understood settings and explicit index bindings leave other names on the default index."""
    _, marks, parsed = guard._read_dependency_file(path, contents)

    assert parsed
    assert {label for label, _ in marks} == ({mark} if mark else set())


@pytest.mark.parametrize("path", [".pdm.toml", "pdm.toml", "pixi.toml", "poetry.toml"])
def test_unmodeled_root_configs_are_discovered_and_gate_every_name(path: str) -> None:
    """Recognize root installer configs without relying on their contents."""
    _, marks, _ = guard._read_dependency_file(path, "")

    assert guard.is_dependency_manifest(path)
    assert marks
    assert all(not label.startswith(guard.UV_SOURCE_MARK) for label, _ in marks)


@pytest.mark.parametrize("path", ["pip.conf", "pip.ini"])
def test_pip_config_marks_normalized_options_without_values(path: str) -> None:
    """An underscore option routes even when the URL needs no interpolation and contains credentials."""
    _, marks, parsed = guard._read_dependency_file(path, "[install]\nindex_url = https://user:secret@mirror.invalid\n")

    assert parsed
    ((label, fingerprint),) = marks
    assert label == f"{path} [install].index-url"
    assert "secret" not in fingerprint


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a fixed identity and no user configuration."""
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **_GIT_ENV},
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    """Write ``files``, commit them and return the new commit SHA."""
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


def _report(tmp_path: Path, base_files: dict[str, str], head_files: dict[str, str]) -> dict[str, object]:
    """Build a base and a head commit and return the dependency report between them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, base_files)
    head = _commit(repo, head_files)
    tree = _git(repo, "ls-tree", "-r", "--name-only", head).splitlines()
    changed = {name: "M" for name in head_files}
    return guard._dependency_report(str(repo), base, head, tree, changed)[0]


def test_direct_reference_in_an_earlier_file_is_not_overwritten_by_a_plain_spec(tmp_path: Path) -> None:
    """The plain ``requirements.txt`` line sorts after the dev file and must not erase its direct reference."""
    report = _report(
        tmp_path,
        {"requirements.txt": "numpy>=1.0\n", "requirements-dev.txt": "numpy>=1.0\n"},
        {"requirements.txt": "numpy>=1.0\n", "requirements-dev.txt": _DIRECT},
    )

    assert report["non_index_references"] == ["numpy"]
    assert report["trusted_unchanged"] == 0


def test_a_name_declared_plainly_in_both_manifests_stays_trusted(tmp_path: Path) -> None:
    """An unchanged plain declaration is not a direct reference."""
    report = _report(
        tmp_path,
        {"requirements.txt": "numpy>=1.0\n", "requirements-dev.txt": "numpy>=1.0\n"},
        {"requirements.txt": "numpy>=1.1\n", "requirements-dev.txt": "numpy>=1.0\n"},
    )

    assert report["non_index_references"] == []
    assert report["trusted_unchanged"] == 1


def test_same_name_in_a_different_ecosystem_is_new(tmp_path: Path) -> None:
    """An unchanged npm dependency does not make a new Python dependency trusted."""
    report = _report(
        tmp_path,
        {"package.json": '{"devDependencies": {"demo": "^1"}}'},
        {"requirements.txt": "demo>=2\n"},
    )

    assert report["added"] == ["demo"]
    assert report["trusted_unchanged"] == 1


def test_direct_reference_cannot_inherit_trust_across_ecosystems(tmp_path: Path) -> None:
    """The same direct reference requires review when introduced in another ecosystem."""
    url = "git+https://example.invalid/demo.git"
    report = _report(
        tmp_path,
        {"package.json": f'{{"dependencies": {{"demo": "{url}"}}}}'},
        {"requirements.txt": f"demo @{url}\n"},
    )

    assert report["added"] == ["demo"]
    assert report["non_index_references"] == ["demo"]


@pytest.mark.skipif(guard._toml_loads("a = 1") is None, reason="needs a TOML reader (Python 3.11+ or tomli)")
@pytest.mark.parametrize(
    ("path", "template"),
    [
        pytest.param("uv.toml", '[[index]]\nurl = "{}"\n', id="uv-index"),
        pytest.param("uv.toml", '[[index]]\nname = "other"\nexplicit = true\nurl = "{}"\n', id="uv-explicit"),
        pytest.param("uv.toml", '[sources]\ndemo = {{ url = "{}" }}\n', id="uv-source"),
        pytest.param("pyproject.toml", '[tool.uv.pip]\nfind-links = ["{}"]\n', id="uv-pip"),
        pytest.param("pyproject.toml", '[[tool.poetry.source]]\nurl = "{}"\n', id="poetry"),
        pytest.param("Pipfile", '[[source]]\nurl = "{}"\n', id="pipenv"),
        pytest.param("pdm.toml", '[[source]]\nurl = "{}"\n', id="unmodeled-config"),
        pytest.param("pip.conf", "[install]\nindex-url = {}\n", id="pip"),
        pytest.param(".npmrc", "registry={}\n", id="npm"),
        pytest.param("requirements.txt", "--index-url {}\n", id="requirements"),
    ],
)
def test_source_replacements_are_reported_without_values(tmp_path: Path, path: str, template: str) -> None:
    """A source in an existing config changes the dependency report without exposing either URL."""
    before = "https://user:old-secret@old.invalid/simple"
    after = "https://user:new-secret@new.invalid/simple"

    report = _report(tmp_path, {path: template.format(before)}, {path: template.format(after)})

    assert len(report["index_overrides"]) == 1  # type: ignore[arg-type]
    assert "old-secret" not in repr(report)
    assert "new-secret" not in repr(report)


def test_source_replacement_compares_matching_config_paths(tmp_path: Path) -> None:
    """A route used by a different manifest in the base cannot hide a replacement here."""
    first = "[install]\nindex-url = https://old.invalid/simple\n"
    second = "[install]\nindex-url = https://new.invalid/simple\n"

    report = _report(tmp_path, {"pip.conf": first, "nested/pip.conf": second}, {"pip.conf": second})

    assert len(report["index_overrides"]) == 1  # type: ignore[arg-type]


def test_a_direct_reference_the_base_already_declares_stays_trusted(tmp_path: Path) -> None:
    """The same direct declaration at the base is not new, even beside a plain spec."""
    files = {"requirements.txt": "numpy>=1.0\n", "requirements-dev.txt": _DIRECT}

    report = _report(tmp_path, files, {**files, "requirements.txt": "numpy>=1.1\n"})

    assert report["non_index_references"] == []


def test_a_second_direct_reference_for_a_known_name_is_flagged(tmp_path: Path) -> None:
    """A direct spec the base does not declare is reported even when the base has another direct spec for the name."""
    other = "numpy @ git+https://example.invalid/other.git\n"
    report = _report(
        tmp_path,
        {"requirements.txt": _DIRECT},
        {"requirements.txt": _DIRECT, "requirements-dev.txt": other},
    )

    assert report["non_index_references"] == ["numpy"]


def test_requirements_keep_a_direct_reference_before_a_plain_spec(tmp_path: Path) -> None:
    """Retain and flag the new direct reference even when a plain requirement follows it."""
    head = _DIRECT + "numpy>=1.0\n"
    report = _report(tmp_path, {"requirements.txt": "numpy>=1.0\n"}, {"requirements.txt": head})

    assert report["non_index_references"] == ["numpy"]
    assert guard._from_requirements_txt(head)[0]["numpy"] == ("@ git+https://example.invalid/numpy.git", True)


@pytest.mark.skipif(guard._toml_loads("a = 1") is None, reason="needs a TOML reader (Python 3.11+ or tomli)")
def test_pyproject_extra_keeps_a_direct_reference_from_project_dependencies(tmp_path: Path) -> None:
    """Retain and flag a direct project dependency even when an extra declares a plain spec."""
    base = '[project]\ndependencies = ["numpy>=1.0"]\n'
    head = f'[project]\ndependencies = ["{_DIRECT.strip()}"]\n[project.optional-dependencies]\ndev = ["numpy>=1.0"]\n'
    report = _report(tmp_path, {"pyproject.toml": base}, {"pyproject.toml": head})

    assert report["non_index_references"] == ["numpy"]
    parsed = guard._from_pyproject(head)
    assert parsed is not None
    assert parsed[0]["numpy"] == ("@ git+https://example.invalid/numpy.git", True)


def test_setup_cfg_keeps_a_direct_reference_before_a_plain_spec(tmp_path: Path) -> None:
    """Retain and flag a direct install requirement even when a plain setup requirement follows it."""
    base = "[options]\ninstall_requires = numpy>=1.0\n"
    head = f"[options]\ninstall_requires = {_DIRECT}setup_requires = numpy>=1.0\n"
    report = _report(tmp_path, {"setup.cfg": base}, {"setup.cfg": head})

    assert report["non_index_references"] == ["numpy"]
    assert guard._from_setup_cfg(head)["numpy"] == ("@ git+https://example.invalid/numpy.git", True)


def test_package_json_dev_dependencies_keep_a_direct_reference_from_dependencies(tmp_path: Path) -> None:
    """Retain and flag a direct npm dependency even when devDependencies declares a plain version."""
    base = '{"dependencies": {"demo": "^1.0.0"}}'
    head = '{"dependencies": {"demo": "file:../demo"}, "devDependencies": {"demo": "^1.0.0"}}'
    report = _report(tmp_path, {"package.json": base}, {"package.json": head})

    assert report["non_index_references"] == ["demo"]
    assert guard._package_json(head)[0]["demo"] == ("file:../demo", True)


def test_a_new_direct_spec_before_the_base_spec_in_one_file_is_flagged(tmp_path: Path) -> None:
    """Report the new URL even when the last declaration remains identical to the base."""
    other = "numpy @ https://example.invalid/numpy.whl\n"
    report = _report(tmp_path, {"requirements.txt": _DIRECT}, {"requirements.txt": other + _DIRECT})

    assert report["non_index_references"] == ["numpy"]
    assert report["trusted_unchanged"] == 0


def test_identical_direct_specs_in_one_file_stay_trusted(tmp_path: Path) -> None:
    """Keep both base direct specs trusted while a plain version in the same file changes."""
    other = "numpy @ https://example.invalid/numpy.whl\n"
    report = _report(
        tmp_path,
        {"requirements.txt": _DIRECT + other + "numpy>=1.0\n"},
        {"requirements.txt": _DIRECT + other + "numpy>=1.1\n"},
    )

    assert report["non_index_references"] == []
    assert report["trusted_unchanged"] == 1


def test_two_plain_specs_in_one_file_keep_the_last_without_a_finding(tmp_path: Path) -> None:
    """Keep last-write-wins behavior for two registry requirements."""
    head = "numpy>=1.0\nnumpy>=1.1\n"
    report = _report(tmp_path, {"requirements.txt": "numpy>=1.0\n"}, {"requirements.txt": head})

    assert report["non_index_references"] == []
    assert report["trusted_unchanged"] == 1
    assert guard._from_requirements_txt(head)[0] == {"numpy": (">=1.1", False)}


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("-ihttps://mirror.invalid/simple", id="short-index-attached"),
        pytest.param("-i https://mirror.invalid/simple", id="short-index-separate"),
        pytest.param("--index-url=https://mirror.invalid/simple", id="long-index-equals"),
        pytest.param("--pypi-url https://mirror.invalid/simple", id="index-alias"),
        pytest.param("--extra-index=https://mirror.invalid/simple", id="long-prefix"),
        pytest.param("-f./wheels", id="short-find-links-attached"),
        pytest.param("--find-links ./wheels", id="long-find-links"),
        pytest.param("--no-index", id="no-index"),
        pytest.param("--trusted-host mirror.invalid", id="trusted-host"),
        pytest.param("--pre -ihttps://mirror.invalid/simple", id="second-option-on-line"),
        pytest.param("--frobnicate https://mirror.invalid/simple", id="unknown-option"),
        pytest.param("-x https://mirror.invalid/simple", id="unknown-short-option"),
    ],
)
def test_every_index_option_spelling_is_a_routing_mark(line: str) -> None:
    """Each spelling pip's option parser reads as index routing, and any option pip does not define, is a mark."""
    declarations, marks = guard._from_requirements_txt(f"{line}\ninternal-pkg==1.0\n")

    assert marks == {line}
    assert declarations == {"internal-pkg": ("==1.0", False)}


@pytest.mark.parametrize(
    ("line", "mark"),
    [
        pytest.param("-rbase.txt", "include: -rbase.txt", id="short-attached"),
        pytest.param("-r base.txt", "include: -r base.txt", id="short-separate"),
        pytest.param("--requirement=base.txt", "include: --requirement=base.txt", id="long-equals"),
        pytest.param("-cconstraints.txt", "include: -cconstraints.txt", id="constraint-attached"),
        pytest.param("-e.", "editable: -e.", id="editable-attached"),
    ],
)
def test_every_include_spelling_is_an_include_mark(line: str, mark: str) -> None:
    """Includes, constraints and editables are recognized whatever the option spelling."""
    assert guard._from_requirements_txt(f"{line}\n")[1] == {mark}


def test_installer_choice_options_are_not_routing() -> None:
    """Options that pick wheels, pre-releases or hash checking do not route names to another index."""
    text = "--pre\n--only-binary :all:\n--prefer-binary\n--require-hashes\nnumpy>=1\n"

    assert guard._from_requirements_txt(text) == ({"numpy": (">=1", False)}, set())


def test_hash_pinned_requirements_are_read_as_pip_reads_them() -> None:
    """Continuation lines join their requirement, and per-requirement hash options are dropped from the spec."""
    text = (
        "requests==2.32.3 \\\n"
        "    --hash=sha256:aaaa \\\n"
        "    --hash=sha256:bbbb\n"
        "    # via app\n"
        "idna==3.7 --hash=sha256:cccc\n"
        "urllib3==2.2.2\n"
    )

    declarations, marks = guard._from_requirements_txt(text)

    assert marks == set()
    assert declarations == {
        "requests": ("==2.32.3", False),
        "idna": ("==3.7", False),
        "urllib3": ("==2.2.2", False),
    }


def test_attached_index_option_added_by_the_head_is_an_override(tmp_path: Path) -> None:
    """A head that routes the same names to another index through ``-iURL`` is reported as an index override."""
    report = _report(
        tmp_path,
        {"requirements.txt": "internal-pkg==1.0\n"},
        {"requirements.txt": "-ihttps://mirror.invalid/simple\ninternal-pkg==1.0\n"},
    )

    assert report["index_overrides"] == ["requirements option --index-url"]


@pytest.mark.parametrize(
    ("content", "mark"),
    [
        pytest.param("\ufeff--no-index\n", "requirements option --no-index", id="utf8-bom"),
        pytest.param('--pre "--index-url" mirror\n', "requirements option --pre, --index-url", id="quoted-routing"),
        pytest.param('--pre "-r" "more pins.in"\n', "include: requirement file", id="quoted-include"),
        pytest.param('--pre "--constraint=locks.in"\n', "include: requirement file", id="quoted-constraint"),
        pytest.param('--pre "unterminated\n', "requirements option", id="invalid-shell-quote"),
        pytest.param("${ROUTING}\n", "unmodeled requirements input", id="env-substitution"),
        pytest.param("# coding: latin-1 coding: utf-8\n", "unmodeled requirements input", id="other-encoding"),
        pytest.param("https://mirror.invalid/pkg.whl\n", "unmodeled requirements input", id="bare-url"),
        pytest.param("dist/pkg.whl\n", "unmodeled requirements input", id="bare-path"),
        pytest.param("pkg-1-py3-none-any.whl\n", "unmodeled requirements input", id="bare-wheel"),
    ],
)
def test_requirements_routes_are_classified_or_refused(content: str, mark: str) -> None:
    """Classify every understood route and qualify inputs outside the reader's grammar."""
    _, routes, _ = guard._read_dependency_file("requirements.txt", content)

    assert {label for label, _ in routes} == {mark}


def test_comment_ends_a_continued_requirement() -> None:
    """Drop a comment even when it immediately follows a continuation without whitespace."""
    text = "demo" + chr(92) + "\n# comment\n"

    assert guard.requirements_logical_lines(text) == ["demo"]
    assert guard._from_requirements_txt(text) == ({"demo": ("", False)}, set())


@pytest.mark.parametrize("option", ["-r", "-c"])
def test_changed_nested_include_is_reported(tmp_path: Path, option: str) -> None:
    """A changed file reached through unchanged include directives is surfaced by path."""
    base = {
        "requirements.txt": f'{option} "config/pins list.in"\n',
        "config/pins list.in": "-r ../shared.lock\n",
        "shared.lock": "--index-url https://old.invalid/simple\n",
    }
    _report(tmp_path, base, {"shared.lock": "--index-url https://new.invalid/simple\n"})

    report = guard.build_report(str(tmp_path / "repo"), "HEAD", "HEAD^", False)

    assert ("shared.lock", "dependency_source_unchecked") in {
        (item["path"], item["category"]) for item in report["findings"]
    }


def test_vcs_fragment_replacement_is_reported(tmp_path: Path) -> None:
    """The fragment can select a different project from the same VCS repository."""
    before = _DIRECT.rstrip() + "#subdirectory=before\n"
    after = _DIRECT.rstrip() + "#subdirectory=after\n"

    report = _report(tmp_path, {"requirements.txt": before}, {"requirements.txt": after})

    assert report["non_index_references"] == ["numpy"]


@pytest.mark.parametrize("new_order", [(1, 0), (0,)])
def test_requirements_index_order_or_removal_is_reported(tmp_path: Path, new_order: tuple[int, ...]) -> None:
    """Preserve directive order and report removal, even when all surviving settings are unchanged."""
    routes = ["--index-url https://old.invalid/simple\n", "--index-url https://new.invalid/simple\n"]
    before = "".join(routes)
    after = "".join(routes[index] for index in new_order)

    report = _report(tmp_path, {"requirements.txt": before}, {"requirements.txt": after})

    assert report["index_overrides"] == ["requirements option --index-url"]
