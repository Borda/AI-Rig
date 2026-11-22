"""Profile selection checks and opt-in host tests for single-process gates in a read-only linked worktree.

Route selection and decline reasons run everywhere with synthetic inputs. The host tests launch the shipped policy: set
CODEX_RIG_TEST_MACOS_WORKTREE_SANDBOX=1 on a macOS host with Homebrew framework Python in a virtual environment and
permission to launch sandbox-exec. Only the public Ruff test also needs a native Ruff binary.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest
from _modules import load_module, load_module_scoped
from _platform import FILE_SYMLINKS_AVAILABLE, HARD_LINKS_AVAILABLE

SHARED = Path(__file__).resolve().parents[3] / "shared"
FRAMEWORK = Path(sys.base_prefix).resolve()
RUNTIME = FRAMEWORK / "Resources/Python.app/Contents/MacOS/Python"
CELLAR = next((p for p in FRAMEWORK.parents if p.name == "Cellar"), None)
SANDBOX = Path("/usr/bin/sandbox-exec")
RUFF = os.environ.get("CODEX_RIG_TEST_MACOS_RUFF") or shutil.which("ruff")
builder = load_module("worktree_sandbox", SHARED / "worktree_sandbox.py")
ENABLED = os.environ.get("CODEX_RIG_TEST_MACOS_WORKTREE_SANDBOX") == "1"
# Each host requirement gets its own skip so a skipped run names exactly what is missing.
OPT_IN = pytest.mark.skipif(
    not ENABLED, reason="set CODEX_RIG_TEST_MACOS_WORKTREE_SANDBOX=1 to launch the host sandbox"
)
NATIVE_SANDBOX = pytest.mark.skipif(not SANDBOX.is_file(), reason="requires /usr/bin/sandbox-exec")
FRAMEWORK_VENV = pytest.mark.skipif(
    not (RUNTIME.is_file() and CELLAR and sys.prefix != sys.base_prefix),
    reason="requires a virtual environment backed by Homebrew framework CPython",
)
NATIVE_RUFF = pytest.mark.skipif(
    not RUFF, reason="requires native Ruff on PATH or its absolute path in CODEX_RIG_TEST_MACOS_RUFF"
)


def _quoted_names_available() -> bool:
    """Probe whether this host's file system accepts a double quote inside a file name."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / 'probe"name').touch()
        except OSError:
            return False
    return True


QUOTED_NAMES = pytest.mark.skipif(not _quoted_names_available(), reason="file system rejects quotes in names")
NEEDS_LINKS = pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot create symbolic links")
POSIX_OWNERSHIP = pytest.mark.skipif(not hasattr(os, "getuid"), reason="needs POSIX ownership checks")
INNER_TEST = """from pathlib import Path
import os
import time
import socket
import subprocess
import sys
import pytest
import sample
# Homebrew runtime libraries load through their opt symlinks, which need only metadata grants.
import decimal
import lzma
import ssl

def test_source_and_scratch_access():
    assert sample.answer == 42
    root = Path(__file__).parent
    for private in (root / ".git", root / ".env.local", root / "nested/marker"):
        with pytest.raises(PermissionError):
            private.read_bytes()
    # Content outside every grant, including an escaping committed link's target, reads as absent.
    for outside in (root.parent / "main/.git/config", root / "alias"):
        with pytest.raises(FileNotFoundError):
            outside.read_bytes()
    # Ancestors stay resolvable; existing paths outside every grant look absent, hiding existence, size and times.
    assert Path.cwd().resolve() == root
    root.parent.stat()
    sibling = root.parent / ("two" if root.name == "one" else "one")
    for outside in (root.parent / "private-marker", sibling, root.parent / "main"):
        with pytest.raises(FileNotFoundError):
            outside.stat()
        assert not outside.exists()
    with pytest.raises(PermissionError):
        (root / "sample.py").open("a")
    with pytest.raises(PermissionError):
        subprocess.run([sys.executable, "-V"], check=True)
    with socket.socket() as sock, pytest.raises(PermissionError):
        sock.bind(("127.0.0.1", 0))
    scratch = Path(os.environ["TMPDIR"])
    (scratch / "ready").write_text("waiting")
    deadline = time.monotonic() + 15
    while not (scratch / "release").is_file():
        assert time.monotonic() < deadline, "parent never released the pair"
        time.sleep(0.02)
    (scratch / "observed").write_text("complete")
"""


@pytest.mark.parametrize(
    ("exit_code", "diagnostic", "available"),
    [
        pytest.param(0, b"", True, id="native-capability-present"),
        pytest.param(71, b"sandbox-exec: sandbox_apply: Operation not permitted\n", False, id="nested-seatbelt"),
    ],
)
def test_capability_probe_executes_only_system_true(exit_code: int, diagnostic: bytes, available: bool) -> None:
    """Positive and known nested results come only from a credential-free system executable."""
    result = subprocess.CompletedProcess([], exit_code, stderr=diagnostic)
    with mock.patch.object(builder.subprocess, "run", autospec=True, return_value=result) as probe:
        assert builder._probe("fixture-profile") is available
    assert probe.call_args.args[0] == [str(builder._SANDBOX), "-p", "fixture-profile", "/usr/bin/true"]
    assert probe.call_args.kwargs["env"] == {}


def test_unexpected_probe_failure_stops_route_selection() -> None:
    """An unexpected sandbox policy refusal must never become a standard-route fallback."""
    result = subprocess.CompletedProcess([], 71, stderr=b"sandbox-exec: unexpected refusal")
    with (
        mock.patch.object(builder.subprocess, "run", autospec=True, return_value=result),
        pytest.raises(RuntimeError, match="worktree-sandbox-probe-failed"),
    ):
        builder._probe("fixture-profile")


@pytest.fixture
def native_probe_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Give the real selector and profile builders benign fixture inputs for both native tools."""
    root = tmp_path.resolve()
    worktree = root / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("fixture registration", encoding="utf-8")
    (worktree / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    checks = root / "checks"
    scratch = checks / "scratch"
    scratch.mkdir(parents=True)
    main = root / "main"
    ruff = main / ".venv/bin/ruff"
    ruff.parent.mkdir(parents=True)
    (ruff.parent.parent / "pyvenv.cfg").touch()
    ruff.write_bytes(bytes.fromhex("cffaedfe"))
    # Co-locate the fixture libraries and their opt lookup so the layout needs no symlinks.
    cellar = root / "opt"
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    framework = cellar / f"python@{version}/framework"
    (framework / f"lib/python{version}/site-packages").mkdir(parents=True)
    (framework / "pyvenv.cfg").touch()
    runtime = framework / "Resources/Python.app/Contents/MacOS/Python"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    interpreter = framework / "bin" / f"python{version}"
    interpreter.parent.mkdir()
    interpreter.touch()
    for formula in ("openssl@3", "xz", "mpdecimal"):
        (cellar / formula / "lib").mkdir(parents=True)
    head = "a" * 40
    content = b"\0".join((os.fsencode(worktree), head.encode(), b"test_fixture.py"))
    digest = hashlib.sha256(content).hexdigest()
    inventory = checks / f"tracked-source-{digest}.bin"
    inventory.write_bytes(content)
    binding = {"path": str(inventory), "head": head, "sha256": digest}
    pytest_argv = [
        str(interpreter),
        "-B",
        str(SHARED / "run_gates.py"),
        "--_python-tests-child",
        str(worktree),
        str(scratch / "proof.json"),
        '["sample"]',
        "[]",
        str(interpreter),
        json.dumps({"tracked_inventory": binding}),
    ]
    monkeypatch.syspath_prepend(str(SHARED))
    runner = load_module_scoped("native_probe_gate_runner", SHARED / "run_gates.py", monkeypatch)
    macos = mock.Mock(
        wraps=sys, platform="darwin", prefix=str(framework), base_prefix=str(framework), version_info=sys.version_info
    )
    monkeypatch.setattr(runner, "sys", macos)
    monkeypatch.setattr(runner, "worktree_sandbox", builder)
    monkeypatch.setattr(runner.shutil, "which", lambda *args, **kwargs: str(ruff))
    monkeypatch.setattr(runner, "_ruff_configuration_reason", lambda *args: None)
    monkeypatch.setattr(builder, "sys", macos)
    monkeypatch.setattr(builder, "_CELLARS", (cellar,))
    monkeypatch.setattr(builder, "native_available", lambda: True)
    monkeypatch.setattr(builder, "_linked_main", lambda *args: main)
    filter_path = builder._filter
    # The fixture models macOS policy paths even when pytest runs on Windows.
    monkeypatch.setattr(builder, "_filter", lambda kind, path: filter_path(kind, Path(path).as_posix()))
    paths = {name: checks / f"{name}.txt" for name in ("stdout", "stderr")}
    commands = {"lint": ("ruff check .", ruff), "tests": (pytest_argv, inventory)}
    return runner, root, head, worktree, scratch, paths, commands


@pytest.mark.parametrize("gate", ["lint", "tests"])
@pytest.mark.parametrize(
    "probe_error",
    [
        pytest.param(FileNotFoundError(errno.ENOENT, "fixture launch error"), id="missing-launcher"),
        pytest.param(NotADirectoryError(errno.ENOTDIR, "fixture launch error"), id="invalid-launch-path"),
    ],
)
def test_final_probe_launch_errors_stop_the_selected_gate(
    native_probe_inputs: tuple, gate: str, probe_error: OSError
) -> None:
    """Report a final probe OS error as a preparation failure with zero workload dispatches."""
    runner, root, head, worktree, scratch, paths, commands = native_probe_inputs
    command, _ = commands[gate]
    with (
        mock.patch.object(builder.subprocess, "run", autospec=True, side_effect=probe_error) as probe,
        mock.patch.object(runner, "execute_command", autospec=True, return_value=(0, 0.0)) as workload,
    ):
        code, _, route = runner._launch_gate(gate, command, 5, paths, root, head, worktree, {}, scratch, True)
    probe.assert_called_once()
    assert probe.call_args.args[0][-1] == "/usr/bin/true"
    assert route["mode"] == "not-started", route
    assert code == 1
    assert "worktree-profile-preparation-failed:worktree-sandbox-probe-failed: launch:" in paths["stderr"].read_text(
        encoding="utf-8"
    )
    workload.assert_not_called()


@pytest.mark.parametrize("gate", ["lint", "tests"])
def test_missing_optional_native_input_uses_one_standard_launch(native_probe_inputs: tuple, gate: str) -> None:
    """An absent optional profile input leaves the original gate eligible before any native probe."""
    runner, root, head, worktree, scratch, paths, commands = native_probe_inputs
    command, optional_input = commands[gate]
    optional_input.unlink()
    with (
        mock.patch.object(builder.subprocess, "run", autospec=True) as probe,
        mock.patch.object(runner, "execute_command", autospec=True, return_value=(0, 0.0)) as workload,
    ):
        code, _, route = runner._launch_gate(gate, command, 5, paths, root, head, worktree, {}, scratch, True)
    assert route == {"mode": "standard", "reason": f"worktree sandbox declined: {builder.INPUT_MISSING}"}
    assert code == 0
    probe.assert_not_called()
    workload.assert_called_once()
    assert workload.call_args.args[0] == command


def test_launch_profile_scopes_metadata_to_grants_and_lookup_ancestors(native_probe_inputs: tuple) -> None:
    """Grant metadata only to the worktree and lookup ancestors, and make every other read look absent."""
    _, root, _, worktree, scratch, _, commands = native_probe_inputs
    _, ruff = commands["lint"]
    success = subprocess.CompletedProcess([], 0, stderr=b"")
    with mock.patch.object(builder.subprocess, "run", autospec=True, return_value=success):
        prepared = builder.prepare_ruff(ruff, ["check", "--no-fix", "."], worktree, scratch, {})
    assert prepared is not None
    lines = prepared[0][2].splitlines()
    assert "(allow file-read-metadata)" not in lines
    # An operation-specific default would outrank the file-read* grants and hide the runtime from dyld.
    assert not any(line.startswith("(deny file-read-metadata") for line in lines)
    default = lines.index("(deny file-read* (with errno ENOENT))")
    content = next(
        index for index, line in enumerate(lines) if line.startswith("(allow file-read* file-map-executable")
    )
    private = next(index for index, line in enumerate(lines) if line.startswith("(deny file-read* file-map-executable"))
    grant = next(index for index, line in enumerate(lines) if line.startswith("(allow file-read-metadata "))
    assert default < content < private < grant
    literals = set(re.findall(r'\(literal "([^"]*)"\)', lines[grant]))
    assert set(re.findall(r'\(subpath "([^"]*)"\)', lines[grant])) == {worktree.as_posix()}
    assert {path.as_posix() for path in (*worktree.parents, *scratch.parents, *ruff.parents)} <= literals
    assert (root / "opt").as_posix() not in literals
    assert ruff.as_posix() not in literals


def _quoted_private_name(worktree: Path) -> None:
    """Commit-style private file whose name would inject profile syntax into a content deny rule."""
    (worktree / '.env."quoted').write_text("fixture only", encoding="utf-8")


def _private_input(worktree: Path) -> None:
    """Private-named file that Ruff would discover itself but the profile must hide."""
    (worktree / ".env.local").write_text("fixture only", encoding="utf-8")


def _linked_input(worktree: Path) -> None:
    """Symbolic link inside the source that Ruff could follow to content outside the grants."""
    (worktree / "linked.py").symlink_to(worktree / "pytest.ini")


def _worker_configuration(worktree: Path) -> None:
    """Project configuration requesting pytest workers, which the single-process profile cannot run."""
    (worktree / "pytest.ini").write_text("[pytest]\naddopts = -n 2\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("gate", "arrange", "reason"),
    [
        pytest.param(
            "tests",
            _quoted_private_name,
            "worktree sandbox declined: unsupported sandbox path",
            marks=QUOTED_NAMES,
            id="pytest-private-name-with-quote",
        ),
        pytest.param(
            "lint",
            _private_input,
            "worktree sandbox declined: Ruff input includes private, linked or nested paths",
            id="ruff-private-input",
        ),
        pytest.param(
            "lint",
            _linked_input,
            "worktree sandbox declined: Ruff input includes private, linked or nested paths",
            marks=NEEDS_LINKS,
            id="ruff-linked-input",
        ),
        pytest.param(
            "tests",
            _worker_configuration,
            "pytest arguments or project configuration request a distribution option",
            id="pytest-worker-configuration",
        ),
    ],
)
def test_checkout_inputs_that_decline_confinement_keep_one_named_standard_launch(
    native_probe_inputs: tuple, gate: str, arrange: Callable[[Path], None], reason: str
) -> None:
    """A checkout can decline confinement, but the receipt names why and the original command runs once unchanged.

    Contributed names, links and configuration can make the profile inapplicable. That decline must be visible in the
    recorded route, must never start the sandbox probe, and must launch the original command with the caller's own
    environment rather than a prepared one, so the caller's execution decision covers exactly what ran.
    """
    runner, root, head, worktree, scratch, paths, commands = native_probe_inputs
    command, _ = commands[gate]
    arrange(worktree)
    with (
        mock.patch.object(builder.subprocess, "run", autospec=True) as probe,
        mock.patch.object(runner, "execute_command", autospec=True, return_value=(0, 0.0)) as workload,
    ):
        code, _, route = runner._launch_gate(gate, command, 5, paths, root, head, worktree, {}, scratch, True)
    assert route == {"mode": "standard", "reason": reason}
    assert code == 0
    probe.assert_not_called()
    workload.assert_called_once()
    assert workload.call_args.args[0] == command
    assert "prepared_environment" not in workload.call_args.kwargs


_LOCATION_REASON = "explicit pytest config, root or argument file needs the standard route"


@pytest.mark.parametrize(
    ("filename", "document", "route"),
    [
        pytest.param(
            "pytest.ini",
            "[pytest]\naddopts = -c ../other.ini\n",
            {"mode": "standard", "reason": _LOCATION_REASON},
            id="ini-config-file",
        ),
        pytest.param(
            "pytest.ini",
            "[pytest]\naddopts = --rootdir=..\n",
            {"mode": "standard", "reason": _LOCATION_REASON},
            id="ini-root-directory",
        ),
        pytest.param(
            "pytest.ini",
            "[pytest]\naddopts = @../arguments.txt\n",
            {"mode": "standard", "reason": _LOCATION_REASON},
            id="ini-argument-file",
        ),
        pytest.param(
            "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = ["--config-file", "../other.ini"]\n',
            {"mode": "standard", "reason": _LOCATION_REASON},
            id="pyproject-config-file",
        ),
        pytest.param(
            "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = ["--rootdir", ".."]\n',
            {"mode": "standard", "reason": _LOCATION_REASON},
            id="pyproject-root-directory",
        ),
        pytest.param(
            "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = ["@../arguments.txt"]\n',
            {"mode": "standard", "reason": _LOCATION_REASON},
            id="pyproject-argument-file",
        ),
        pytest.param(
            "pytest.ini",
            "[pytest]\naddopts = -k local\n",
            {"mode": "macos-worktree", "reason": "declared single-process pytest"},
            id="ini-clean-options",
        ),
        pytest.param(
            "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = ["-k", "local"]\n',
            {"mode": "macos-worktree", "reason": "declared single-process pytest"},
            id="pyproject-clean-options",
        ),
    ],
)
def test_project_addopts_naming_locations_keep_the_standard_route(
    native_probe_inputs: tuple, filename: str, document: str, route: dict[str, str]
) -> None:
    """Project addopts get the same location checks as the command line before the route is chosen.

    Configured options are expanded into pytest's command line. A configuration file, root directory or argument file
    named there can lie outside the worktree, where the confined child would read it as absent and fail a run the
    standard route completes. Options naming no location keep the confined route.
    """
    runner, root, head, worktree, scratch, paths, commands = native_probe_inputs
    command, _ = commands["tests"]
    (worktree / "pytest.ini").unlink()
    (worktree / filename).write_text(document, encoding="utf-8")
    success = subprocess.CompletedProcess([], 0, stderr=b"")
    with (
        mock.patch.object(builder.subprocess, "run", autospec=True, return_value=success) as probe,
        mock.patch.object(runner, "execute_command", autospec=True, return_value=(0, 0.0)) as workload,
    ):
        code, _, observed = runner._launch_gate("tests", command, 5, paths, root, head, worktree, {}, scratch, True)
    assert observed == route
    assert code == 0
    workload.assert_called_once()
    confined = route["mode"] == "macos-worktree"
    assert probe.called is confined
    assert (workload.call_args.args[0] == command) is not confined
    assert ("prepared_environment" in workload.call_args.kwargs) is confined


@dataclass(frozen=True)
class _ProfileInputs:
    """The real profile builders' fixture inputs: layout roots, the pytest argv and the native Ruff executable."""

    root: Path
    worktree: Path
    scratch: Path
    argv: list[str]
    ruff: Path
    monkeypatch: pytest.MonkeyPatch

    @property
    def framework(self) -> Path:
        """Return the fixture's framework runtime, which is also its selected virtual environment."""
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        return self.root / "opt" / f"python@{version}/framework"

    @property
    def inventory(self) -> Path:
        """Return the tracked inventory the pytest argv binds."""
        return Path(json.loads(self.argv[9])["tracked_inventory"]["path"])

    def bind_inventory(self, path: Path, content: bytes) -> list[str]:
        """Write ``content`` at ``path`` and return the pytest argv bound to it with that content's digest."""
        path.write_bytes(content)
        binding = json.loads(self.argv[9])["tracked_inventory"]
        binding.update(path=str(path), sha256=hashlib.sha256(content).hexdigest())
        return [*self.argv[:9], json.dumps({"tracked_inventory": binding})]


@pytest.fixture
def profile_inputs(native_probe_inputs: tuple, monkeypatch: pytest.MonkeyPatch) -> _ProfileInputs:
    """Expose the fixture layout the real profile builders accept, before any case changes one input."""
    _, root, _, worktree, scratch, _, commands = native_probe_inputs
    return _ProfileInputs(root, worktree, scratch, commands["tests"][0], commands["lint"][1], monkeypatch)


def _pytest_unchanged(inputs: _ProfileInputs) -> list[str]:
    """Keep the accepted pytest inputs, the control that every decline below differs from."""
    return inputs.argv


def _pytest_native_unavailable(inputs: _ProfileInputs) -> list[str]:
    """Report the native sandbox unavailable before preparation reads any input."""
    inputs.monkeypatch.setattr(builder, "native_available", lambda: False)
    return inputs.argv


def _pytest_other_script(inputs: _ProfileInputs) -> list[str]:
    """Run a different script than the structured pytest child."""
    return [*inputs.argv[:2], str(inputs.root / "other.py"), *inputs.argv[3:]]


def _pytest_other_runtime_interpreter(inputs: _ProfileInputs) -> list[str]:
    """Record a different interpreter for the child than the one launched."""
    return [*inputs.argv[:8], str(inputs.root / "python"), inputs.argv[9]]


def _pytest_proof_outside_scratch(inputs: _ProfileInputs) -> list[str]:
    """Ask the child to write its proof outside the private scratch directory."""
    return [*inputs.argv[:5], str(inputs.root / "proof.json"), *inputs.argv[6:]]


def _pytest_missing_environment_marker(inputs: _ProfileInputs) -> list[str]:
    """Remove the selected virtual environment's marker file."""
    (inputs.framework / "pyvenv.cfg").unlink()
    return inputs.argv


def _pytest_site_packages_file(inputs: _ProfileInputs) -> list[str]:
    """Replace the selected environment's site-packages directory with a regular file."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = inputs.framework / f"lib/python{version}/site-packages"
    site_packages.rmdir()
    site_packages.write_text("not a directory", encoding="utf-8")
    return inputs.argv


def _pytest_inventory_digest_not_hex(inputs: _ProfileInputs) -> list[str]:
    """Bind a value that is not a SHA-256 digest, naming an inventory path built from it that does not exist."""
    binding = json.loads(inputs.argv[9])["tracked_inventory"]
    binding.update(sha256="not-a-digest", path=str(inputs.scratch.parent / "tracked-source-not-a-digest.bin"))
    return [*inputs.argv[:9], json.dumps({"tracked_inventory": binding})]


def _pytest_inventory_not_digest_named(inputs: _ProfileInputs) -> list[str]:
    """Bind an inventory with the right bytes under a name that does not carry their digest."""
    return inputs.bind_inventory(inputs.scratch.parent / "other-inventory.bin", inputs.inventory.read_bytes())


def _pytest_inventory_for_other_worktree(inputs: _ProfileInputs) -> list[str]:
    """Bind a digest-named inventory that records another checkout root."""
    head = json.loads(inputs.argv[9])["tracked_inventory"]["head"]
    content = b"\0".join((os.fsencode(inputs.root / "other"), head.encode(), b"test_fixture.py"))
    return inputs.bind_inventory(
        inputs.scratch.parent / f"tracked-source-{hashlib.sha256(content).hexdigest()}.bin", content
    )


def _pytest_inventory_alias(inputs: _ProfileInputs) -> list[str]:
    """Give the bound inventory a second name outside the gate output."""
    os.link(inputs.inventory, inputs.root / "inventory-alias")
    return inputs.argv


def _pytest_redirected_library(inputs: _ProfileInputs) -> list[str]:
    """Point one Homebrew runtime library link outside its formula."""
    library = inputs.root / "opt/xz/lib"
    library.rmdir()
    (inputs.root / "elsewhere/lib").mkdir(parents=True)
    library.symlink_to(inputs.root / "elsewhere/lib", target_is_directory=True)
    return inputs.argv


def _pytest_oversized_profile(inputs: _ProfileInputs) -> list[str]:
    """Add more private worktree entries than the profile's size limit can deny."""
    for index in range(1500):
        (inputs.worktree / f".env.{index}").touch()
    return inputs.argv


def _pytest_missing_inventory(inputs: _ProfileInputs) -> list[str]:
    """Remove the bound inventory before preparation reads it."""
    inputs.inventory.unlink()
    return inputs.argv


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        pytest.param(_pytest_unchanged, None, id="accepted-inputs"),
        pytest.param(_pytest_native_unavailable, builder.NATIVE_UNAVAILABLE, id="native-unavailable"),
        pytest.param(_pytest_other_script, builder.PYTEST_COMMAND, id="other-script"),
        pytest.param(_pytest_other_runtime_interpreter, builder.PYTEST_COMMAND, id="other-recorded-interpreter"),
        pytest.param(_pytest_proof_outside_scratch, builder.PYTEST_COMMAND, id="proof-outside-scratch"),
        pytest.param(_pytest_missing_environment_marker, builder.PYTEST_RUNTIME, id="missing-environment-marker"),
        pytest.param(_pytest_site_packages_file, builder.PYTEST_RUNTIME, id="site-packages-not-directory"),
        pytest.param(_pytest_inventory_digest_not_hex, builder.TRACKED_INVENTORY, id="inventory-digest-not-hex"),
        pytest.param(_pytest_inventory_not_digest_named, builder.TRACKED_INVENTORY, id="inventory-not-digest-named"),
        pytest.param(_pytest_inventory_for_other_worktree, builder.TRACKED_INVENTORY, id="inventory-other-worktree"),
        pytest.param(
            _pytest_inventory_alias,
            builder.TRACKED_INVENTORY,
            marks=pytest.mark.skipif(not HARD_LINKS_AVAILABLE, reason="host cannot hard-link files"),
            id="inventory-hard-linked",
        ),
        pytest.param(_pytest_redirected_library, builder.RUNTIME_LIBRARY, marks=NEEDS_LINKS, id="library-redirected"),
        pytest.param(_pytest_oversized_profile, builder.PROFILE_SIZE, id="profile-too-large"),
        pytest.param(_pytest_missing_inventory, builder.INPUT_MISSING, id="inventory-missing"),
    ],
)
def test_pytest_profile_declines_with_the_failed_precondition(
    profile_inputs: _ProfileInputs, arrange: Callable[[_ProfileInputs], list[str]], reason: str | None
) -> None:
    """Each unmet pytest profile precondition returns its own fixed reason instead of a confined launch.

    The reason is recorded in the gate receipt, so it must name the precondition that failed; accepted inputs reach the
    capability probe and return the confined launch.
    """
    argv = arrange(profile_inputs)
    success = subprocess.CompletedProcess([], 0, stderr=b"")
    with mock.patch.object(builder.subprocess, "run", autospec=True, return_value=success):
        prepared = builder.prepare_pytest(argv, profile_inputs.worktree, profile_inputs.scratch, {})
    if reason is None:
        assert isinstance(prepared, tuple), prepared
        assert prepared[0][0] == str(builder._SANDBOX)
    else:
        assert prepared == reason


def _ruff_unchanged(inputs: _ProfileInputs) -> tuple[Path, list[str]]:
    """Keep the accepted Ruff inputs, the control that every decline below differs from."""
    return inputs.ruff, ["check", "--no-fix", "."]


def _ruff_native_unavailable(inputs: _ProfileInputs) -> tuple[Path, list[str]]:
    """Report the native sandbox unavailable before preparation reads any input."""
    inputs.monkeypatch.setattr(builder, "native_available", lambda: False)
    return inputs.ruff, ["check", "--no-fix", "."]


def _ruff_fixing_command(inputs: _ProfileInputs) -> tuple[Path, list[str]]:
    """Request a Ruff command that has no read-only profile."""
    return inputs.ruff, ["check", "."]


def _ruff_unknown_install(inputs: _ProfileInputs) -> tuple[Path, list[str]]:
    """Use a native Ruff outside every known environment and Homebrew keg."""
    tool = inputs.root / "tools/ruff"
    tool.parent.mkdir()
    tool.write_bytes(inputs.ruff.read_bytes())
    return tool, ["check", "--no-fix", "."]


def _ruff_script(inputs: _ProfileInputs) -> tuple[Path, list[str]]:
    """Replace the known Ruff with a script."""
    inputs.ruff.write_bytes(b"#!/bin/sh\n")
    return inputs.ruff, ["format", "--check", "."]


def _ruff_inside_worktree(inputs: _ProfileInputs) -> tuple[Path, list[str]]:
    """Select a known environment whose Ruff lives inside the worktree it would read."""
    inputs.monkeypatch.setattr(builder, "_linked_main", lambda *args: inputs.worktree)
    tool = inputs.worktree / ".venv/bin/ruff"
    tool.parent.mkdir(parents=True)
    (inputs.worktree / ".venv/pyvenv.cfg").touch()
    tool.write_bytes(inputs.ruff.read_bytes())
    return tool, ["check", "--no-fix", "."]


def _ruff_missing(inputs: _ProfileInputs) -> tuple[Path, list[str]]:
    """Remove the known Ruff before preparation resolves it."""
    inputs.ruff.unlink()
    return inputs.ruff, ["check", "--no-fix", "."]


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        pytest.param(_ruff_unchanged, None, id="accepted-inputs"),
        pytest.param(_ruff_native_unavailable, builder.NATIVE_UNAVAILABLE, id="native-unavailable"),
        pytest.param(_ruff_fixing_command, builder.RUFF_COMMAND, id="fixing-command"),
        pytest.param(_ruff_unknown_install, builder.RUFF_INSTALL, id="unknown-install"),
        pytest.param(_ruff_script, builder.RUFF_BINARY, id="not-a-native-binary"),
        pytest.param(_ruff_inside_worktree, builder.GRANT_OVERLAP, id="executable-inside-worktree"),
        pytest.param(_ruff_missing, builder.INPUT_MISSING, id="executable-missing"),
    ],
)
def test_ruff_profile_declines_with_the_failed_precondition(
    profile_inputs: _ProfileInputs,
    arrange: Callable[[_ProfileInputs], tuple[Path, list[str]]],
    reason: str | None,
) -> None:
    """Each unmet Ruff profile precondition returns its own fixed reason instead of a confined launch."""
    executable, arguments = arrange(profile_inputs)
    success = subprocess.CompletedProcess([], 0, stderr=b"")
    with mock.patch.object(builder.subprocess, "run", autospec=True, return_value=success):
        prepared = builder.prepare_ruff(executable, arguments, profile_inputs.worktree, profile_inputs.scratch, {})
    if reason is None:
        assert isinstance(prepared, tuple), prepared
        assert prepared[0][-len(arguments) :] == arguments
    else:
        assert prepared == reason


@pytest.mark.parametrize("tool", [pytest.param("pytest", id="pytest"), pytest.param("ruff", id="ruff")])
def test_nested_sandbox_at_the_final_probe_declines_as_unavailable(profile_inputs: _ProfileInputs, tool: str) -> None:
    """A nested sandbox that refuses the final policy probe declines the profile rather than failing the gate."""
    nested = subprocess.CompletedProcess([], 71, stderr=b"sandbox-exec: sandbox_apply: Operation not permitted\n")
    inputs = profile_inputs
    with mock.patch.object(builder.subprocess, "run", autospec=True, return_value=nested) as probe:
        if tool == "pytest":
            prepared = builder.prepare_pytest(inputs.argv, inputs.worktree, inputs.scratch, {})
        else:
            prepared = builder.prepare_ruff(
                inputs.ruff, ["check", "--no-fix", "."], inputs.worktree, inputs.scratch, {}
            )
    assert prepared == builder.NATIVE_UNAVAILABLE
    assert probe.call_args.args[0][-1] == "/usr/bin/true"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('/fixture/quote"d', id="double-quote"),
        pytest.param("/fixture/back\\slash", id="backslash"),
        pytest.param("/fixture/new\nline", id="control-character"),
    ],
)
def test_profile_path_with_policy_syntax_is_declined(value: str) -> None:
    """A path that could end or escape an SBPL string declines the profile instead of being quoted."""
    with pytest.raises(builder._Declined, match=r"^unsupported sandbox path$"):
        builder._filter("literal", value)


@NEEDS_LINKS
@pytest.mark.timeout(10)
def test_link_cycle_in_a_grant_path_is_declined(tmp_path: Path) -> None:
    """A symbolic link cycle on a granted path declines the profile after the kernel's link bound, never looping."""
    loop = tmp_path / "loop"
    loop.symlink_to("loop")
    with pytest.raises(builder._Declined, match=r"^symbolic link loop in sandbox grant$"):
        builder._lookup_chain(loop / "child")


def _main_checkout_registration(root: Path) -> Path:
    """Return an ordinary checkout whose ``.git`` is a directory rather than a linked-worktree file."""
    work = root / "work"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    return work


def _two_line_registration(root: Path) -> Path:
    """Return a checkout whose ``.git`` file holds more than the single registration line."""
    work = root / "work"
    work.mkdir()
    (work / ".git").write_text("gitdir: one\ngitdir: two\n", encoding="utf-8")
    return work


def _crossed_registration(root: Path) -> Path:
    """Return a linked worktree whose administrative entry points back at a different worktree."""
    main = root / "main"
    main.mkdir()
    git = ["git", "-C", str(main), "-c", "core.hooksPath=/dev/null"]
    subprocess.run([*git, "init", "-q"], check=True)
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
    subprocess.run([*git, *identity, "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    for name in ("one", "two"):
        subprocess.run([*git, "worktree", "add", "-q", "--detach", str(root / name), "HEAD"], check=True)
    (main / ".git/worktrees/one/gitdir").write_text(f"{root / 'two/.git'}\n", encoding="utf-8")
    return root / "one"


@POSIX_OWNERSHIP
@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        pytest.param(_main_checkout_registration, "invalid worktree registration", id="directory-registration"),
        pytest.param(_two_line_registration, "invalid worktree registration", id="multi-line-registration"),
        pytest.param(_crossed_registration, "unexpected worktree registration", id="crossed-registration"),
    ],
)
def test_unverifiable_worktree_registration_declines_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrange: Callable[[Path], Path], reason: str
) -> None:
    """A checkout whose Git registration cannot be proven reciprocal keeps its standard route under a named reason."""
    root = tmp_path.resolve()
    work = arrange(root)
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700)
    monkeypatch.setattr(builder, "native_available", lambda: True)
    assert builder.prepare_ruff(root / "ruff", ["check", "--no-fix", "."], work, scratch, {}) == reason


def _prepare_command(main: Path, work: Path, scratch: Path) -> tuple[list[str], dict[str, str]]:
    """Prepare a checked-out fixture revision and its parent-bound command before taking the legacy snapshot."""
    git = ["git", "-C", str(main), "-c", "core.hooksPath=/dev/null"]
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(work), "HEAD"], check=True)
    (work / "alias").symlink_to(main.parent / "private-marker")
    (work / ".env.local").write_text("fixture only", encoding="utf-8")
    (work / "nested/.git").mkdir(parents=True)
    (work / "nested/marker").write_text("nested repository", encoding="utf-8")
    scratch.mkdir(mode=0o700)
    head = subprocess.check_output([*git, "rev-parse", "HEAD"]).strip()
    names = subprocess.check_output([*git, "ls-files", "-z"])
    content = b"\0".join((os.fsencode(work.as_posix()), head, names))
    inventory = scratch.parent / f"tracked-source-{hashlib.sha256(content).hexdigest()}.bin"
    inventory.write_bytes(content)
    binding = {"path": str(inventory), "head": head.decode("ascii"), "sha256": hashlib.sha256(content).hexdigest()}
    command = [
        sys.executable,
        "-B",
        str(SHARED / "run_gates.py"),
        "--_python-tests-child",
        str(work),
        str(scratch / "proof.json"),
        '["sample"]',
        '["-p","no:cacheprovider","-q","test_fixture.py"]',
        sys.executable,
        json.dumps({"tracked_inventory": binding}),
    ]
    prepared = builder.prepare_pytest(
        command,
        work,
        scratch,
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
        },
    )
    assert isinstance(prepared, tuple), f"host must support the production profile: {prepared}"
    assert builder.prepare_pytest(command, work, work, {}) == "overlapping or redirected gate roots"
    assert builder.prepare_pytest(command, main, scratch, {}) == "invalid worktree registration"
    assert builder.prepare_pytest(command, work, scratch, {"PYTHONPATH": str(main)}) == builder.PYTHONPATH_ESCAPE
    assert (
        builder.prepare_pytest(command, work, scratch, {"COVERAGE_FILE": str(main / ".coverage")})
        == builder.ARTIFACT_ESCAPE
    )
    assert builder.prepare_pytest([*command[:-1], "{}"], work, scratch, {}) == builder.INPUT_UNVERIFIED
    occupied = scratch / "retained-output"
    occupied.write_text("previous run", encoding="utf-8")
    assert builder.prepare_pytest(command, work, scratch, {}) == "invalid gate roots"
    occupied.unlink()
    assert prepared[1]["PYTEST_ADDOPTS"] == "-p no:cacheprovider"
    return prepared


def _check_origins(command: tuple[list[str], dict[str, str]], work: Path, scratch: Path) -> None:
    """Run an isolated fixture and validate its exact worktree-bound import receipt after the rendezvous."""
    result = subprocess.run(
        command[0],
        cwd=work,
        env=command[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (scratch / "observed").read_text(encoding="utf-8") == "complete"
    proof = json.loads((scratch / "proof.json").read_bytes())
    assert proof["status"] == "pass"
    assert proof["modules"]["sample"] == {
        "origin": str(work / "sample.py"),
        "tracked": True,
        "status": "pass",
        "reason": None,
    }
    assert proof["tests"][str(work / "test_fixture.py")]["tracked"] is True


def _helper(name: str, *args: str, code: int = 0) -> dict:
    """Read the real helper's receipt and check its actual return code."""
    result = subprocess.run(
        [sys.executable, "-B", str(SHARED / name), *args], capture_output=True, text=True, check=False, timeout=20
    )
    assert result.returncode == code, (result.stderr, result.stdout)
    return json.loads(result.stdout)


def _overlap(root: Path, prepared: list[tuple[tuple[list[str], dict[str, str]], Path, Path]], claim: dict) -> None:
    """Require both real gates to enter their tests before either can complete, with an incompatible waiter."""
    with ThreadPoolExecutor(len(prepared)) as workers:
        futures = [workers.submit(_check_origins, *job) for job in prepared]
        try:
            deadline = time.monotonic() + 20
            while not all((scratch / "ready").is_file() for _, _, scratch in prepared):
                for future in futures:
                    if future.done():
                        future.result()
                        pytest.fail("gate completed before the pair was released")
                assert time.monotonic() < deadline, "both checks must enter their fixture"
                time.sleep(0.02)
            assert _helper(
                "review_checkout_slot.py", "acquire", "--checkout", str(root / "main"), "--owner", "new-review", code=1
            ) == {**claim, "status": "busy"}
        finally:
            for _, _, scratch in prepared:
                (scratch / "release").touch()
        for future in futures:
            future.result()


def _main(root: Path) -> tuple[Path, list[str]]:
    """Build the parent checkout used by each real native tool."""
    main = root / "main"
    main.mkdir()
    git = ["git", "-C", str(main), "-c", "core.hooksPath=/dev/null"]
    subprocess.run([*git, "init", "-q"], check=True)
    (main / "sample.py").write_text("answer = 42\n", encoding="utf-8")
    (main / "test_fixture.py").write_text(INNER_TEST, encoding="utf-8")
    subprocess.run([*git, "add", "--", "sample.py", "test_fixture.py"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    return main, git


@pytest.mark.integration
@OPT_IN
@NATIVE_SANDBOX
@FRAMEWORK_VENV
def test_prepared_worktree_gates_overlap_and_preserve_legacy_snapshot(tmp_path: Path) -> None:
    """Prove overlapping real isolated gates preserve a legacy snapshot and respect its admission claim."""
    root = tmp_path.resolve()
    main, git = _main(root)
    (root / "private-marker").write_text("fixture only", encoding="utf-8")
    # An ancestor config outside every grant must look absent to pytest's upward search, never break collection.
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--fixture-unknown-option"\n', encoding="utf-8"
    )
    legacy = root / "legacy"
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(legacy), "HEAD"], check=True)
    prepared = [
        (_prepare_command(main, work, scratch), work, scratch)
        for work, scratch in [(root / "one", root / "scratch-one"), (root / "two", root / "scratch-two")]
    ]
    claim = _helper("review_checkout_slot.py", "acquire", "--checkout", str(main), "--owner", "legacy-review")
    try:
        assert claim["status"] == "acquired"
        snapshot = root / "legacy-report" / "integrity.json"
        snapshot.parent.mkdir()
        receipt = _helper(
            "contributor_run_integrity.py",
            "snapshot",
            "--repo",
            str(legacy),
            "--main-checkout",
            str(main),
            "--watch-surface",
            "--out",
            str(snapshot),
        )
        _overlap(root, prepared, claim)
        verified = _helper(
            "contributor_run_integrity.py", "verify", "--snapshot", str(snapshot), "--expect", receipt["sha256"]
        )
        assert verified["ok"] is True
        assert verified["changed"] == []
    finally:
        assert (
            _helper("review_checkout_slot.py", "release", "--slot", claim["slot"], "--owner", "legacy-review")["status"]
            == "released"
        )


@dataclass(frozen=True)
class _PublicGate:
    """A committed checkout, its detached review worktree under the run directory and the gate's filtered env."""

    main: Path
    work: Path
    out: Path
    head: str
    environment: dict[str, str]


def _public_gate_checkout(root: Path, source_path: str, source: bytes) -> _PublicGate:
    """Commit lint inputs and a confined test, then add the detached review worktree the public gate runs in."""
    main, git = _main(root)
    selected = main / source_path
    selected.parent.mkdir(exist_ok=True)
    selected.write_bytes(source)
    marker = main / "selected-tool"
    marker.write_text("fixture for PATH discovery", encoding="utf-8")
    marker.chmod(0o755)
    (main / "ignored.py").write_bytes(b"import sys\n")
    subprocess.run([*git, "add", "--", "ignored.py"], check=True)
    (main / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (main / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    (main / "test_selected.py").write_text(
        "from pathlib import Path\nimport sample\nimport pytest\nimport os\nimport shutil\n"
        "def test_selected():\n"
        "    assert sample.answer == 42\n"
        "    assert os.environ['USER'] == 'gate-fixture'\n"
        "    assert 'EXAMPLE_TOKEN' not in os.environ\n"
        "    assert os.environ['PATH'].split(os.pathsep)[0] == str(Path.cwd())\n"
        "    assert shutil.which('selected-tool', mode=os.F_OK) == str(Path.cwd() / 'selected-tool')\n"
        "    (Path(os.environ['TMPDIR']) / 'attempt.cache').touch(exist_ok=False)\n"
        "    with pytest.raises(PermissionError):\n"
        "        Path('.git').read_bytes()\n"
        "    with pytest.raises(PermissionError):\n"
        "        Path('sample.py').open('a')\n",
        encoding="utf-8",
    )
    subprocess.run(
        [*git, "add", "--", source_path, "test_selected.py", ".gitignore", "ruff.toml", "selected-tool"], check=True
    )
    subprocess.run(
        [*git, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "gate inputs"],
        check=True,
    )
    head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    run = main / ".reports/review/run"
    run.mkdir(parents=True)
    work = run / "worktree"
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(work), head], check=True)
    environment = {
        "USER": "gate-fixture",
        "EXAMPLE_TOKEN": "fixture-only",
        "PATH": os.pathsep.join((str(work), str(main / ".venv/bin"), os.defpath)),
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    return _PublicGate(main, work, run / "out", head, environment)


def _run_public_gate(gate: _PublicGate, *lint_and_format: str) -> subprocess.CompletedProcess[str]:
    """Run the public gate CLI with the declared single-process pytest and the given lint and format options."""
    return subprocess.run(
        [
            sys.executable,
            "-B",
            str(SHARED / "run_gates.py"),
            "--out",
            str(gate.out),
            "--worktree",
            str(gate.work),
            "--expected-head",
            gate.head,
            *lint_and_format,
            "--pytest-python",
            sys.executable,
            "--pytest-import",
            "sample",
            "--single-process-pytest",
            "--pytest-args-json",
            '["-q","-p","no:cacheprovider","test_selected.py"]',
            "--skip-types",
            "unused in fixture",
            "--skip-review",
            "unused in fixture",
        ],
        cwd=gate.main,
        env=gate.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )


@pytest.mark.integration
@OPT_IN
@NATIVE_SANDBOX
@FRAMEWORK_VENV
def test_public_gate_confines_import_bound_pytest_without_ruff(tmp_path: Path) -> None:
    """The public CLI records a passing confined import-bound pytest receipt on a host with no native Ruff.

    This keeps the public sandbox route covered where only the Ruff checks are unavailable: lint and format are
    declared out of scope, and the tests gate runs the real profile end to end through the runner and validator.
    """
    gate = _public_gate_checkout(tmp_path.resolve(), "visible.py", b"x = 1\n")
    skipped = ("--skip-lint", "no native Ruff in fixture", "--skip-format", "no native Ruff in fixture")
    result = _run_public_gate(gate, *skipped)
    assert result.returncode == 0, (result.stdout, result.stderr)
    validator = load_module("gate_route_validator", SHARED / "validate-artifacts.py")
    receipt = validator._validate_gates(gate.out)
    assert receipt["checks_failed"] == []
    tests = next(row for row in receipt["checks"] if row["id"] == "tests")
    assert tests["execution_route"] == {"mode": "macos-worktree", "reason": "declared single-process pytest"}
    assert tests["python_imports"]["status"] == "pass"
    assert tests["python_imports"]["modules"]["sample"]["origin"] == str(gate.work / "sample.py")
    assert tests["python_imports"]["tests"][str(gate.work / "test_selected.py")]["tracked"] is True


@pytest.mark.integration
@OPT_IN
@NATIVE_SANDBOX
@FRAMEWORK_VENV
@NATIVE_RUFF
@pytest.mark.parametrize(
    ("source_path", "ruff_route"),
    [
        pytest.param("visible.py", "macos-worktree", id="complete-source"),
        pytest.param(".codex/visible.py", "standard", id="protected-source-uses-original-discovery"),
    ],
)
def test_public_gate_receipts_bind_native_tools_and_pytest(tmp_path: Path, source_path: str, ruff_route: str) -> None:
    """The public CLI retains both real Ruff failures and a passing confined import-bound pytest receipt."""
    source = b"import os\nx=1\n"
    gate = _public_gate_checkout(tmp_path.resolve(), source_path, source)
    main, work, out = gate.main, gate.work, gate.out
    binary = main / ".venv/bin/ruff"
    binary.parent.mkdir(parents=True)
    (main / ".venv/pyvenv.cfg").write_text("home = fixture", encoding="utf-8")
    shutil.copy2(str(RUFF), binary)
    result = _run_public_gate(gate, "--lint", "ruff check --no-fix .", "--format", "ruff format --check .")
    assert result.returncode == 1, (result.stdout, result.stderr)
    receipt = json.loads((out / "gates.json").read_bytes())
    checks = {row["id"]: row for row in receipt["checks"]}
    assert {name: checks[name]["execution_route"]["mode"] for name in ("lint", "format", "tests")} == {
        "lint": ruff_route,
        "format": ruff_route,
        "tests": "macos-worktree",
    }
    assert receipt["checks_failed"] == ["lint", "format"], (
        result.stderr,
        (out / checks["tests"]["stderr"]).read_text(),
        (out / checks["tests"]["stdout"]).read_text(),
    )
    lint = (out / checks["lint"]["stdout"]).read_text(encoding="utf-8")
    assert source_path in lint
    assert "F401" in lint
    assert "ignored.py" not in lint
    formatting = (out / checks["format"]["stdout"]).read_text(encoding="utf-8").lower()
    assert source_path in formatting
    assert "reformat" in formatting
    assert (work / source_path).read_bytes() == source
    proof = json.loads((out / "checks/tests.python-imports.json").read_bytes())
    assert proof["status"] == "pass"
    assert proof["modules"]["sample"]["origin"] == str(work / "sample.py")
    assert proof["tests"][str(work / "test_selected.py")]["tracked"] is True
    previous = (out / "gates.json").read_bytes()
    retry = subprocess.run(result.args, cwd=main, env=gate.environment, capture_output=True, text=True, timeout=45)
    assert retry.returncode == result.returncode, (retry.stdout, retry.stderr)
    archive = out / "gate-attempts/001"
    assert (archive / "gates.json").read_bytes() == previous
    validator = load_module("gate_retry_validator", SHARED / "validate-artifacts.py")
    assert validator._validate_gates(archive)["checks_failed"] == ["lint", "format"]
    current = validator._validate_gates(out)
    assert current["checks_failed"] == ["lint", "format"]
    repeated = next(row for row in current["checks"] if row["id"] == "tests")
    assert repeated["execution_route"] == checks["tests"]["execution_route"]
    assert repeated["python_imports"] == checks["tests"]["python_imports"]
