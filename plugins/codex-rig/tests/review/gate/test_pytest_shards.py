"""Timed pytest shards retain independent outcomes, source proof, and archived logs."""

import json
import sys
from pathlib import Path

import pytest
from _gate_runner_support import RUN_GATES, _gate_check, _load_run_gates, _load_validator, _receipt_text, _skip_gates
from _modules import load_module_scoped
from _platform import FILE_SYMLINKS_AVAILABLE

HEAD = "a" * 40
# Deadline wide enough for the collection probe to start on a loaded Windows runner, far below the slow test.
SHARD_TIMEOUT = 6
SELECTION = ["-q", "-msmoke", "--ignore=second/test_ignored.py", "--", "first", "second", "third"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Build real pytest inputs and supply a committed-inventory observation without invoking Git."""
    checkout = tmp_path / "project"
    sources = {
        "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["."]\nmarkers = ["smoke: selected tests"]\n',
        "example.py": "value = 1\n",
        "first/test_slow.py": (
            "import time\nimport pytest\nimport example\n"
            "@pytest.mark.smoke\ndef test_selectable_slow():\n    time.sleep(60)\n"
        ),
        "second/test_bad.py": (
            "import pytest\nimport example\n"
            "@pytest.mark.smoke\ndef test_middle_failure():\n    assert example.value == 2\n"
        ),
        "second/test_ignored.py": "raise RuntimeError('excluded file was collected')\n",
        "third/test_good.py": (
            "import pytest\nimport example\n"
            "@pytest.mark.smoke\ndef test_final_pass():\n    assert example.value == 1\n"
            "def test_unselected():\n    raise RuntimeError('marker lost')\n"
        ),
        "fourth/test_needs_alpha.py": (
            "import pytest\nimport absent_module_alpha\n@pytest.mark.smoke\ndef test_alpha():\n    pass\n"
        ),
        "fifth/test_needs_beta.py": (
            "import pytest\nimport absent_module_beta\n@pytest.mark.smoke\ndef test_beta():\n    pass\n"
        ),
        "sixth/test_import_unproven.py": "def test_without_project_import():\n    pass\n",
    }
    for relative, content in sources.items():
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    gates = _load_run_gates(monkeypatch)
    monkeypatch.setattr(gates, "_observe_source", lambda *_: ({"head": HEAD, "status": ""}, None))
    monkeypatch.setattr(gates, "_tracked_source_paths", lambda *_, **__: {name.encode() for name in sources})
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    return gates, checkout, tmp_path / "evidence"


@pytest.fixture
def gap_records(monkeypatch):
    """Load the same shipped gap reader used in gate promotion."""
    return load_module_scoped("run_gap_records", RUN_GATES.with_name("run_gap_records.py"), monkeypatch)


def _run(project, monkeypatch, arguments: list[str], *extra: str) -> int:
    """Run the normal CLI against the fixture project and its retained gate output."""
    gates, checkout, output = project
    argv = [
        str(RUN_GATES),
        "--out",
        str(output),
        "--worktree",
        str(checkout),
        "--expected-head",
        HEAD,
        "--pytest-python",
        sys.executable,
        "--pytest-import",
        "example",
        "--pytest-args-json",
        json.dumps(arguments),
        *_skip_gates("tests"),
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return gates.main()


def test_timeout_does_not_hide_code_failure_in_canonical_logs(project, monkeypatch, gap_records):
    """A timeout retains its own diagnosis while the canonical logs expose the code failure."""
    _, checkout, output = project
    assert _run(project, monkeypatch, SELECTION, "--shards", "3", "--timeout-seconds", str(SHARD_TIMEOUT)) == 124

    payload = _load_validator()._validate_gates(output)
    check = _gate_check(output, "tests")
    assert (payload["status"], payload["checks_failed"], check["status"], check["exit_code"]) == (
        "timeout",
        ["tests"],
        "timeout",
        124,
    )
    first, second, third = check["shards"]
    assert [
        (row["id"], row["selected_paths"], row["status"], row["exit_code"], row.get("cause")) for row in check["shards"]
    ] == [
        ("tests-shard-001", ["first"], "timeout", 124, "timeout"),
        ("tests-shard-002", ["second"], "fail", 1, "command-failed"),
        ("tests-shard-003", ["third"], "pass", 0, None),
    ]
    assert first["duration_seconds"] >= SHARD_TIMEOUT
    failure_output = _receipt_text(output, second, "stdout")
    assert check["cause"] == "timeout"
    canonical_output = _receipt_text(output, check, "stdout")
    assert f"--- {second['stdout']} (command-failed) ---" in canonical_output
    assert failure_output in canonical_output
    assert "test_middle_failure" in failure_output
    assert "excluded file was collected" not in failure_output
    assert "1 passed, 1 deselected" in _receipt_text(output, third, "stdout")
    assert third["python_imports"]["invoked_interpreter"] == Path(sys.executable).as_posix()
    assert set(third["python_imports"]["tests"]) == {(checkout / "third/test_good.py").as_posix()}
    assert [row["timeout_seconds"] for row in check["shards"]] == [SHARD_TIMEOUT] * 3
    drafts, omitted = gap_records.gate_drafts(payload)
    assert [draft.id for draft in drafts] == ["gate-tests-timeout"]
    assert omitted == []


def test_rerun_archives_shard_logs_and_accepts_unsharded_receipts(project, monkeypatch):
    """A failed shard controls the gate before a passing unsharded rerun archives its exact evidence."""
    _, _, output = project
    ordinary_selection = ["-q", "-m", "smoke", "third"]
    assert _run(project, monkeypatch, ordinary_selection) == 0
    legacy = (output / "gates.json").read_bytes()
    assert _run(project, monkeypatch, [*SELECTION[:-3], "second", "third"], "--shards", "2") == 1
    initial = _gate_check(output, "tests")
    failed, passed = initial["shards"]
    assert (initial["status"], failed["status"], passed["status"]) == ("fail", "fail", "pass")
    log = output / failed["stdout"]
    contents = log.read_bytes()
    proof = output / "checks/tests-shard-002.python-imports.json"
    proof_contents = proof.read_bytes()

    assert _run(project, monkeypatch, ordinary_selection) == 0
    validator = _load_validator()
    assert (output / "gate-attempts/001/gates.json").read_bytes() == legacy
    archive = output / "gate-attempts/002"
    assert validator._validate_gates(archive)["status"] == "fail"
    assert validator._validate_gates(output)["status"] == "pass"
    assert "shards" not in _gate_check(output, "tests")
    assert not log.exists()
    assert not proof.exists()
    assert (archive / failed["stdout"]).read_bytes() == contents
    assert (archive / proof.relative_to(output)).read_bytes() == proof_contents
    (archive / passed["stdout"]).write_text("different log\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"^gate-evidence-digest-mismatch$"):
        validator._validate_gates(archive)


def test_source_change_between_shards_keeps_started_gate_failed(project, monkeypatch):
    """A source guard rejection after a passing shard records a failed gate and leaves later work unstarted."""
    gates, _, output = project
    clean = {"head": HEAD, "status": ""}
    dirty = {"head": HEAD, "status": " M example.py"}
    observations = iter([(clean, None)] * 8 + [(dirty, "source-dirty:changed before next shard")])
    monkeypatch.setattr(gates, "_observe_source", lambda *_: next(observations))

    assert _run(project, monkeypatch, [*SELECTION[:-3], "third", "first", "second"], "--shards", "3") == 1

    payload = _load_validator()._validate_gates(output)
    assert payload["checks_failed"] == ["tests"]
    assert "refusal" not in payload
    passed, refused, also_refused = _gate_check(output, "tests")["shards"]
    assert passed["status"] == "pass"
    assert refused["cause"] == also_refused["cause"] == "source-refused"
    assert "execution_route" not in refused
    assert "execution_route" not in also_refused


def test_sharding_requires_a_nonempty_selection_per_shard(project, monkeypatch, capsys):
    """Reject excess shard counts before publishing a gate or accidentally collecting the default suite."""
    _, _, output = project
    with pytest.raises(SystemExit) as error:
        _run(project, monkeypatch, ["-q", "--", "third"], "--shards", "2")
    assert error.value.code == 2
    assert "at least 2 pytest paths" in capsys.readouterr().err
    assert not (output / "gates.json").exists()


@pytest.mark.parametrize("origin", ["cli", "environment", "config"])
def test_tests_gate_requires_call_phase(project, monkeypatch, origin):
    """A clean collection-only exit cannot satisfy the tests gate."""
    _, checkout, output = project
    options = ["-q"]
    if origin == "cli":
        options.append("--collect-only")
    elif origin == "environment":
        monkeypatch.setenv("PYTEST_ADDOPTS", "--co")
    else:
        (checkout / "pytest.ini").write_text("[pytest]\naddopts = --collectonly\npythonpath = .\n", encoding="utf-8")

    assert _run(project, monkeypatch, [*options, "third/test_good.py::test_final_pass"]) == 1
    check = _gate_check(output, "tests")
    assert check["status"] == "fail"
    assert "collect-only" in _receipt_text(output, check, "stderr")


def test_all_setup_skips_leave_execution_unproven(project, monkeypatch):
    """A zero exit with selected items but only setup reports cannot pass."""
    _, checkout, output = project
    (checkout / "first/test_slow.py").write_text(
        "import example\nimport pytest\n\n@pytest.mark.skip\ndef test_skip():\n    pass\n", encoding="utf-8"
    )

    assert _run(project, monkeypatch, ["-q", "first"]) == 1
    check = _gate_check(output, "tests")
    assert check["python_imports"]["pytest"]["collected"] == 1
    assert check["python_imports"]["pytest"]["calls"] == 0
    assert "call-phase reports" in _receipt_text(output, check, "stderr")


def test_shards_verify_selection_again_before_calls(project, monkeypatch):
    """If a plugin changes selection between probe and execution, the affected shard runs no test calls."""
    _, checkout, output = project
    (checkout / "third/conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n    if config.option.collectonly:\n        items.pop()\n",
        encoding="utf-8",
    )
    args = ["-q", "--", "third", "sixth"]

    assert _run(project, monkeypatch, args, "--shards", "2") == 1
    shard = _gate_check(output, "tests")["shards"][0]
    assert shard["python_imports"]["pytest"]["calls"] == 0
    assert "selection changed" in _receipt_text(output, shard, "stderr")


@pytest.mark.parametrize(
    ("addopts", "diagnostic"),
    [
        pytest.param('["third"]', "overlap", id="shared-path"),
        pytest.param(
            '["-p", "pytest_cov.plugin", "--cov=example", "--cov-fail-under=80"]', "coverage-enabled", id="coverage"
        ),
    ],
)
def test_shards_use_native_pytest_configuration(project, monkeypatch, capsys, addopts, diagnostic):
    """Use the effective settings of the child pytest, including its native TOML table."""
    _, checkout, output = project
    (checkout / "pyproject.toml").write_text(
        f'[tool.pytest.ini_options]\n[tool.pytest]\npythonpath = ["."]\naddopts = {addopts}\n', encoding="utf-8"
    )

    # The budget only bounds a hang; importing pytest-cov to collect can exceed 2 seconds on a cold Windows host.
    assert (
        _run(
            project,
            monkeypatch,
            ["-q", "--", "first", "second/test_bad.py"],
            "--shards",
            "2",
            "--timeout-seconds",
            "30",
        )
        == 2
    )
    assert diagnostic in capsys.readouterr().err
    assert not (output / "gates.json").exists()


@pytest.mark.parametrize(
    ("cli", "environment", "config", "message"),
    [
        pytest.param("--cov=example --cov-fail-under=80", "", "", "coverage-enabled", id="command-line-coverage"),
        pytest.param("", "--cov=example", "", "coverage-enabled", id="environment-coverage"),
        pytest.param("", "", "--cov=example", "coverage-enabled", id="configured-coverage"),
        pytest.param("", "./third", "", "overlap", id="environment-path"),
        pytest.param("", "--", "", "could not verify pytest collection", id="environment-separator"),
        pytest.param("", "", "./third", "overlap", id="configured-path"),
        pytest.param("-oaddopts=./third", "", "", "overlap", id="command-line-override"),
        pytest.param("", "--override-ini addopts=./third", "", "overlap", id="environment-override"),
        pytest.param("--config-file=custom.ini", "", "", "could not verify pytest collection", id="explicit-config"),
        pytest.param(
            "--plugin-selection=third", "", "", "could not verify pytest collection", id="unknown-plugin-option"
        ),
    ],
)
def test_shards_refuse_coverage_or_unproven_shared_options(
    project, monkeypatch, capsys, cli, environment, config, message
):
    """Shards refuse coverage and any shared selection the runner cannot prove disjoint before execution."""
    _, checkout, output = project
    monkeypatch.setenv("PYTEST_ADDOPTS", environment)
    cli = f"-p pytest_cov.plugin {cli}"
    (checkout / "pytest.ini").write_text(f"[pytest]\npythonpath = .\naddopts = {config}\n", encoding="utf-8")
    code = _run(project, monkeypatch, ["-q", *cli.split(), "--", "third", "sixth"], "--shards", "2")
    if environment == "--" and sys.version_info < (3, 12):
        # Before 3.12 argparse drops the repeated "--", so pytest still collects the selection and the gate's own
        # collection proof passes: nothing is unverified, the shards run, and the fixture's failing test fails the gate.
        assert code == 1
        assert (output / "gates.json").exists()
        return
    assert code == 2
    assert message in capsys.readouterr().err
    assert not (output / "gates.json").exists()


def test_shards_refuse_inherited_configuration(project, monkeypatch, capsys):
    """Pytest's effective configuration must be inside the guarded checkout."""
    _, checkout, output = project
    (checkout / "pyproject.toml").unlink()
    (checkout.parent / "pytest.ini").write_text("[pytest]\naddopts = ./third\n", encoding="utf-8")

    assert _run(project, monkeypatch, ["-q", "--", "third", "sixth"], "--shards", "2") == 2
    assert "inherited pytest configuration" in capsys.readouterr().err
    assert not (output / "gates.json").exists()


def test_coverage_threshold_applies_to_unsharded_suite(project, monkeypatch, capsys):
    """Keep the combined suite's coverage result and refuse to apply the threshold to separate processes."""
    _, checkout, output = project
    (checkout / "example.py").write_text("def left():\n    return 1\n\ndef right():\n    return 2\n", encoding="utf-8")
    left = "third/test_good.py"
    right = "sixth/test_import_unproven.py"
    (checkout / left).write_text(
        "import example\n\ndef test_left():\n    assert example.left() == 1\n", encoding="utf-8"
    )
    (checkout / right).write_text(
        "import example\n\ndef test_right():\n    assert example.right() == 2\n", encoding="utf-8"
    )
    options = ["-q", "--cov=example", "--cov-fail-under=80", "-p", "pytest_cov.plugin"]

    assert _run(project, monkeypatch, [*options, left]) == 1
    assert "75.00%" in _receipt_text(output, _gate_check(output, "tests"), "stdout")
    assert _run(project, monkeypatch, [*options, left, right]) == 0
    assert "100.00%" in _receipt_text(output, _gate_check(output, "tests"), "stdout")
    receipt = (output / "gates.json").read_bytes()

    assert _run(project, monkeypatch, [*options[:-2], "-ppytest_cov.plugin", "--", left, right], "--shards", "2") == 2
    assert "coverage-enabled" in capsys.readouterr().err
    assert (output / "gates.json").read_bytes() == receipt


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks")
def test_shards_refuse_a_file_and_its_symlink_alias(project, monkeypatch, capsys):
    """Two explicit paths resolving to the same test file cannot run in separate shards."""
    _, checkout, output = project
    target = "third/test_good.py"
    alias = "third/test_alias.py"
    (checkout / alias).symlink_to(checkout / target)
    assert _run(project, monkeypatch, ["-q", "--", target, alias], "--shards", "2") == 2
    assert "overlap" in capsys.readouterr().err
    assert not (output / "gates.json").exists()


@pytest.mark.parametrize(
    ("paths", "statuses"),
    [
        pytest.param(["first", "second/test_bad.py"], ["missing-command", "fail"], id="missing-command-first"),
        pytest.param(["second/test_bad.py", "first"], ["fail", "missing-command"], id="failed-test-first"),
    ],
)
def test_shard_receipt_prefers_a_failed_test_over_exit_127(project, monkeypatch, paths, statuses):
    """Publish both shard results and retain the failed test command's status and diagnostics."""
    _, checkout, output = project
    (checkout / "first/test_slow.py").write_text(
        "import example\nimport pytest\n\ndef test_exit_127():\n    pytest.exit(str(example.value), returncode=127)\n",
        encoding="utf-8",
    )
    assert _run(project, monkeypatch, ["-q", "--", *paths], "--shards", "2") == 1

    check = _gate_check(output, "tests")
    assert [row["status"] for row in check["shards"]] == statuses
    missing = next(row for row in check["shards"] if row["status"] == "missing-command")
    assert (missing["exit_code"], missing["cause"]) == (127, "missing-command")
    assert (check["status"], check["exit_code"], check["cause"]) == ("fail", 1, "command-failed")
    assert "test_middle_failure" in _receipt_text(output, check, "stdout")
    assert _load_validator()._validate_gates(output)["checks_failed"] == ["tests"]


def test_missing_package_draft_survives_another_shards_code_failure(project, monkeypatch, gap_records):
    """An assertion stays visible and the other shard's missing dependency still calls for provisioning."""
    _, _, output = project
    assert _run(project, monkeypatch, ["-q", "-msmoke", "--", "fourth", "second/test_bad.py"], "--shards", "2") == 1

    check = _gate_check(output, "tests")
    assert [(row["status"], row["cause"]) for row in check["shards"]] == [
        ("fail", "missing-python-package"),
        ("fail", "command-failed"),
    ]
    assert (check["status"], check["cause"], check["missing_modules"]) == (
        "fail",
        "command-failed",
        ["absent_module_alpha"],
    )
    assert "test_middle_failure" in _receipt_text(output, check, "stdout")
    payload = _load_validator()._validate_gates(output)
    assert payload["checks_failed"] == ["tests"]
    drafts, omitted = gap_records.gate_drafts(payload)
    assert [draft.id for draft in drafts] == ["provisioning-not-attempted-tests"]
    assert omitted == []


def test_aggregate_lists_the_missing_modules_of_every_shard(project, monkeypatch, gap_records):
    """Multiple dependency shards retain their inventory and produce distinct provisioned package gaps."""
    _, _, output = project
    assert _run(project, monkeypatch, ["-q", "-msmoke", "--", "fourth", "fifth"], "--shards", "2") == 1

    check = _gate_check(output, "tests")
    assert (check["cause"], check["missing_modules"]) == (
        "missing-python-package",
        ["absent_module_alpha", "absent_module_beta"],
    )
    payload = _load_validator()._validate_gates(output)
    drafts, omitted = gap_records.gate_drafts({**payload, "project_env": "review-env"})
    assert [draft.id for draft in drafts] == [
        "provisioning-failed-absent-module-alpha",
        "provisioning-failed-absent-module-beta",
    ]
    assert omitted == []


def test_unproven_import_does_not_hide_code_failure(project, monkeypatch, gap_records):
    """An inconclusive import is an environment gap and keeps the assertion as the canonical diagnosis."""
    _, _, output = project
    assert _run(project, monkeypatch, ["-q", "--", "sixth", "second/test_bad.py"], "--shards", "2") == 1

    check = _gate_check(output, "tests")
    assert [row["cause"] for row in check["shards"]] == ["import-origin-unproven", "command-failed"]
    assert check["cause"] == "command-failed"
    assert "test_middle_failure" in _receipt_text(output, check, "stdout")
    drafts, omitted = gap_records.gate_drafts(_load_validator()._validate_gates(output))
    assert [draft.id for draft in drafts] == ["gate-tests-import-origin-unproven"]
    assert omitted == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param(["-q", "--", "second", "second/test_bad.py"], "overlap", id="directory-and-inner-file"),
        pytest.param(
            ["-q", "--", "third/test_good.py", "third/test_good.py::test_final_pass"], "overlap", id="file-and-node"
        ),
        pytest.param(
            ["-q", "--", "third/test_good.py::test_parameter", "third/test_good.py::test_parameter[1]"],
            "overlap",
            id="node-and-parameter",
        ),
        pytest.param(["-q", "--", "./third", "third"], "overlap", id="relative-alias"),
        pytest.param(["-q", "--", "third/", "first", "third"], "overlap", id="trailing-separator"),
        pytest.param(["-q", "--", "second/../third", "third"], "overlap", id="parent-step"),
        pytest.param(["-q", "third", "--", "first", "second"], "overlap", id="positional-before-separator"),
    ],
)
def test_shards_refuse_selectors_that_collect_a_test_twice(project, monkeypatch, capsys, arguments, message):
    """Every collected test belongs to exactly one shard, so overlapping or shared selectors stop before any run."""
    _, checkout, output = project
    with (checkout / "third/test_good.py").open("a", encoding="utf-8") as stream:
        stream.write('\n@pytest.mark.parametrize("value", [1, 2])\ndef test_parameter(value):\n    assert value\n')
    assert _run(project, monkeypatch, arguments, "--shards", "2", "--timeout-seconds", "3") == 2
    assert message in capsys.readouterr().err
    assert not (output / "gates.json").exists()


def test_disjoint_nodes_in_one_file_shard_separately(project, monkeypatch):
    """Different tests in one file, a longer name sharing a prefix and a sibling file are distinct selections."""
    gates, checkout, output = project
    nodes = ["third/test_good.py::test_final_pass", "third/test_good.py::test_final_pass_extra", "third/test_x.py"]
    argv = [str(RUN_GATES), "--out", str(output), "--worktree", str(checkout), "--expected-head", HEAD]
    argv += ["--pytest-python", sys.executable, "--pytest-import", "example", "--shards", "3"]
    argv += ["--pytest-args-json", json.dumps(["-q", "--", *nodes]), *_skip_gates("tests")]
    monkeypatch.setattr(sys, "argv", argv)

    assert gates.parse_args().shards == [(node,) for node in nodes]


def test_every_shard_obeys_required_sandbox_route(project, monkeypatch):
    """An unavailable required profile prevents every shard from launching the standard child."""
    _, _, output = project
    args = [*SELECTION[:-3], "second", "third"]
    assert _run(project, monkeypatch, args, "--shards", "2", "--sandbox-gate", "tests") == 1

    first, second = _gate_check(output, "tests")["shards"]
    assert first["execution_route"]["mode"] == second["execution_route"]["mode"] == "not-started"
    assert first["cause"] == second["cause"] == "sandbox-refused"
    assert "python_imports" not in first
    assert "python_imports" not in second
