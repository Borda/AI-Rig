"""Tests for ``bin/setup_scan_env.py`` — scan-codebase setup state file generation.

Every behavioural test runs twice: once against the Python port invoked directly, and
once through ``bin/setup_scan_env.sh``, the deprecated bash shim that ``exec``s it.
Both entry points must therefore satisfy the same contract, which is what keeps
pre-existing bash call sites working after the port.

Covered scenarios:
- Bad CLI args → exit 3, no state file.
- Missing ``scan-index`` binary (bogus ``CLAUDE_PLUGIN_ROOT``) → exit 1, message on stderr.
- Happy path — state file written, sourceable, KEY=VAL contents match expected fields,
  per-PROJ_SLUG tmpfiles populated.
- ``--root`` extraction overrides ``PROJ_NAME`` (basename of ``--root`` value, not repo).
- ``--incremental`` sentinel created when no prior index exists for ``PROJ_NAME``.
- ``--incremental`` sentinel absent when prior index exists.
- Shim/port equivalence — identical invocation yields identical exit code and state.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# Both entry points require the POSIX shell and source-based state reader.
POSIX_ENTRYPOINTS_UNAVAILABLE = sys.platform == "win32" or shutil.which("python3") is None

PLUGIN_ROOT = Path(__file__).parent.parent.parent  # contains real bin/scan-index
SCRIPT = PLUGIN_ROOT / "bin" / "setup_scan_env.py"
SHIM = PLUGIN_ROOT / "bin" / "setup_scan_env.sh"
_EXIT_BAD_ARGS = 3  # setup_scan_env.py's own argument-validation exit code

_PY_ARGV = [sys.executable, str(SCRIPT)]
_SH_ARGV = ["bash", str(SHIM)]

_skip_posix_entrypoints_unavailable = pytest.mark.skipif(
    POSIX_ENTRYPOINTS_UNAVAILABLE,
    reason="the bash shim and the source-based state reader need a POSIX shell",
)


@pytest.fixture(
    name="launcher",
    params=[
        pytest.param(_PY_ARGV, id="py"),
        pytest.param(_SH_ARGV, id="sh-shim"),
    ],
)
def _launcher(request: pytest.FixtureRequest) -> list[str]:
    """Return the argv prefix for one of the two supported entry points.

    Returns:
        ``[python, setup_scan_env.py]`` or ``[bash, setup_scan_env.sh]``.
    """
    return request.param


def _run_setup(
    launcher: list[str],
    *args: str,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one setup entry point with an explicit env override.

    Args:
        launcher: Argv prefix naming the entry point under test.
        *args: Positional arguments forwarded to the script.
        env: Environment overlay (``None`` ⇒ inherit only).
        cwd: Working directory for the invocation.

    Returns:
        Captured ``CompletedProcess`` with text-mode stdout/stderr.
    """
    # Force the script's CSID = os.environ.get("CSID") or "shared" fallback regardless of
    # the host shell — a real Claude Code session (like the one running this suite) never
    # sets CSID itself, only CLAUDE_CODE_SESSION_ID (which the script does not read). A test
    # that needs a CSID passes it through ``env``, which is applied after the pop.
    e = {k: v for k, v in os.environ.items() if k != "CSID"}
    e.update(env or {})
    return subprocess.run(
        [*launcher, *args],
        capture_output=True,
        text=True,
        env=e,
        cwd=cwd,
    )


def _read_state(state_file: Path) -> dict[str, str]:
    """Parse the script's KEY='value' state file into a dict.

    Bash sourcing semantics are emulated via a small subshell that ``source``s
    the file and prints each variable — preserves the script's contract that
    the file is sourceable.

    Args:
        state_file: Path written by ``setup_scan_env.py``.

    Returns:
        Mapping ``{PROJ_SLUG, SCAN_BIN, SCAN_ARGS_RAW, PROJ_NAME}``.
    """
    script = (
        f'source "{state_file}" && '
        'printf "PROJ_SLUG\\t%s\\n" "$PROJ_SLUG" && '
        'printf "SCAN_BIN\\t%s\\n" "$SCAN_BIN" && '
        'printf "SCAN_ARGS_RAW\\t%s\\n" "$SCAN_ARGS_RAW" && '
        'printf "PROJ_NAME\\t%s\\n" "$PROJ_NAME"'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("\t")
        out[key] = value
    return out


def _executable_scan_index(plugin_root: Path) -> Path:
    """Create an executable ``bin/scan-index`` under ``plugin_root`` and return its path."""
    scan_index = plugin_root / "bin" / "scan-index"
    scan_index.parent.mkdir(parents=True)
    scan_index.write_text("#!/bin/sh\nexit 0\n")
    scan_index.chmod(0o755)
    return scan_index


def _tmpfile_shapes(tmpdir: Path) -> list[str]:
    """List the tmpfile names in ``tmpdir`` with the per-run random parts masked out.

    Two names are unequal across separate runs by design: half the handoff tmpfiles
    embed ``os.getpid()`` so concurrent same-project scans cannot race, and the state
    file carries an unguessable ``mkstemp`` suffix so it cannot be symlink-squatted.
    Masking both leaves the set of names each entry point is expected to produce.

    Args:
        tmpdir: Directory the run used as ``TMPDIR``.

    Returns:
        Sorted names with the PID and the state-file suffix replaced by placeholders.
    """
    masked = (re.sub(r"-\d+-", "-<pid>-", p.name) for p in tmpdir.rglob("*") if p.is_file())
    return sorted(re.sub(r"^codemap-scan-state-.*$", "codemap-scan-state-<rand>", name) for name in masked)


@pytest.fixture(name="fake_repo")
def _fake_repo(tmp_path: Path) -> Path:
    """Return a throwaway directory acting as a fake repo root.

    setup_scan_env.py falls back to the cwd when ``git rev-parse --show-toplevel``
    fails, so no real git repo is needed.

    Returns:
        Directory path used as the working directory for script invocations.
    """
    return tmp_path


@pytest.fixture(name="isolated_tmpdir")
def _isolated_tmpdir(tmp_path: Path) -> Path:
    """Provide a fresh ``TMPDIR`` so per-PROJ_SLUG tmpfiles don't leak across tests.

    Returns:
        Directory path passed as ``TMPDIR`` to the script.
    """
    base = tmp_path / "tmp"
    base.mkdir()
    user = str(os.getuid()) if hasattr(os, "getuid") else hashlib.sha256(getpass.getuser().encode()).hexdigest()[:16]
    directory = base / f"codemap-scan-{user}"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture(name="run_setup")
def _run_setup_factory(
    launcher: list[str], fake_repo: Path, isolated_tmpdir: Path
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a runner for the current entry point inside the fake repo with an isolated ``TMPDIR``.

    The runner takes the script arguments plus an optional ``plugin_root`` (default: this plugin) and ``extra_env``
    overlay, so each test names only what makes its scenario different.
    """

    def run(
        *args: str, plugin_root: Path | str = PLUGIN_ROOT, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the entry point with ``CLAUDE_PLUGIN_ROOT``, ``TMPDIR`` and any extra variables set."""
        env = {"CLAUDE_PLUGIN_ROOT": str(plugin_root), "TMPDIR": str(isolated_tmpdir.parent), **(extra_env or {})}
        return _run_setup(launcher, *args, env=env, cwd=str(fake_repo))

    return run


@_skip_posix_entrypoints_unavailable
class TestArgumentValidation:
    """Bad CLI shapes must fail fast with exit 3 and never touch tmpfiles."""

    @pytest.mark.parametrize(
        ("bad_args", "stderr_fragment"),
        [
            pytest.param(("--bogus",), "unknown argument", id="unknown-long-flag"),
            pytest.param(("--arguments",), "needs a value", id="arguments-option-without-value"),
        ],
    )
    def test_rejects_malformed_arguments(
        self,
        run_setup: Callable[..., subprocess.CompletedProcess[str]],
        bad_args: tuple[str, ...],
        stderr_fragment: str,
    ) -> None:
        """An unknown long flag, or an arguments option without a following value, exits 3 with a stderr message.

        Validation fails fast with the dedicated exit code and a message naming the problem, before any state file or
        tmpfile is written, for both the Python port and the bash shim.
        """
        r = run_setup(*bad_args)

        assert r.returncode == _EXIT_BAD_ARGS
        assert stderr_fragment in r.stderr

    def test_accepts_arguments_equals_form(self, run_setup: Callable[..., subprocess.CompletedProcess[str]]) -> None:
        """``--arguments=<value>`` is accepted and stored exactly like ``--arguments <value>``.

        Callers that build the option as one token must not hit the missing-value error.
        """
        r = run_setup("--arguments=--incremental")

        assert r.returncode == 0, r.stderr
        assert _read_state(Path(r.stdout.strip()))["SCAN_ARGS_RAW"] == "--incremental"


@_skip_posix_entrypoints_unavailable
class TestMissingScanIndex:
    """Bogus ``CLAUDE_PLUGIN_ROOT`` ⇒ scan-index validation fails (exit 1)."""

    def test_bogus_plugin_root_exits_1(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], tmp_path: Path
    ) -> None:
        """Pointing ``CLAUDE_PLUGIN_ROOT`` at an empty dir surfaces the missing-binary error.

        Under the ``sh-shim`` launcher this also proves a non-zero exit from the port reaches the caller unchanged
        through the shim.
        """
        empty = tmp_path / "no-plugin"
        empty.mkdir()

        r = run_setup("--arguments", "", plugin_root=empty)

        assert r.returncode == 1
        assert "scan-index binary not found" in r.stderr
        # No state file should have been printed.
        assert r.stdout.strip() == ""


@_skip_posix_entrypoints_unavailable
class TestHappyPath:
    """Normal invocation produces a sourceable state file and per-slug tmpfiles."""

    @pytest.fixture(name="minimal_state")
    def _minimal_state(self, run_setup: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, str]:
        """Run the minimal setup invocation and return the sourced state."""
        r = run_setup("--arguments", "")
        assert r.returncode == 0, r.stderr
        state_path = Path(r.stdout.strip())
        assert state_path.is_file(), f"state file not created at {state_path}"
        return _read_state(state_path)

    def test_minimal_invocation_writes_sourceable_state(self, minimal_state: dict[str, str], fake_repo: Path) -> None:
        """No flags writes a sourceable state file with project identity and scan command fields."""
        # PROJ_SLUG ends in the sanitised repo basename — the script strips underscores
        # and other punctuation, so compare on the sanitised form.
        sanitised_repo = "".join(c for c in fake_repo.name if c.isalnum() or c == "-")

        assert minimal_state["PROJ_NAME"] == fake_repo.name
        assert minimal_state["PROJ_SLUG"].endswith(sanitised_repo)
        assert minimal_state["SCAN_BIN"].endswith("/bin/scan-index")
        assert minimal_state["SCAN_ARGS_RAW"] == ""

    def test_minimal_invocation_writes_per_slug_tmpfiles(
        self, minimal_state: dict[str, str], isolated_tmpdir: Path, fake_repo: Path
    ) -> None:
        """Per-PROJ_SLUG tmpfiles exist with content matching the sourceable state.

        ``CSID`` env var is unset in the test invocation (only inherited by real ``export CSID=...`` callers), so the
        script's ``"shared"`` fallback applies — every written filename carries a terminal ``-shared``.
        """
        slug = minimal_state["PROJ_SLUG"]

        assert (isolated_tmpdir / "codemap-proj-slug-shared").read_text() == slug
        assert (isolated_tmpdir / f"codemap-proj-name-{slug}-shared").read_text() == fake_repo.name
        assert (isolated_tmpdir / f"codemap-scan-bin-{slug}-shared").read_text() == minimal_state["SCAN_BIN"]
        assert (isolated_tmpdir / f"codemap-scan-args-{slug}-shared").read_text() == ""

    def test_minimal_invocation_leaves_incremental_sentinel_absent(
        self, minimal_state: dict[str, str], isolated_tmpdir: Path
    ) -> None:
        """No ``--incremental`` request leaves the fallback sentinel absent."""
        slug = minimal_state["PROJ_SLUG"]

        assert not (isolated_tmpdir / f"codemap-incremental-noop-{slug}-shared").exists()

    @pytest.mark.parametrize("project_dir", ["alt-project", "alt project with spaces"])
    def test_root_flag_overrides_proj_name(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], tmp_path: Path, project_dir: str
    ) -> None:
        """The project name comes from the basename of an explicit ``--root`` path, even one containing spaces.

        A shell-quoted ``--root`` value must still drive project identity, and the raw argument string must be handed on
        unchanged for the scanner.
        """
        other = tmp_path / project_dir
        other.mkdir()
        raw_args = f"--root {shlex.quote(str(other))}"

        r = run_setup("--arguments", raw_args)

        assert r.returncode == 0, r.stderr
        state = _read_state(Path(r.stdout.strip()))
        assert state["PROJ_NAME"] == project_dir
        assert state["SCAN_ARGS_RAW"] == raw_args

    def test_root_dot_keeps_repo_basename(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], fake_repo: Path
    ) -> None:
        """``--root .`` resolves like an absent ``--root`` — basename('.') would be empty."""
        r = run_setup("--arguments", "--root .")

        assert r.returncode == 0, r.stderr
        assert _read_state(Path(r.stdout.strip()))["PROJ_NAME"] == fake_repo.name


@_skip_posix_entrypoints_unavailable
class TestIncrementalSentinel:
    """Combine incremental mode with prior-index detection."""

    def test_created_when_no_prior_index(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], isolated_tmpdir: Path
    ) -> None:
        """Write the incremental sentinel when no prior index exists.

        With nothing to update incrementally, the run is flagged through a sentinel file so the caller can fall back to
        a full scan; the informational message goes to stderr, keeping stdout reserved for the state path.
        """
        r = run_setup("--arguments", "--incremental")

        assert r.returncode == 0, r.stderr
        assert "No prior index" in r.stderr
        slug = _read_state(Path(r.stdout.strip()))["PROJ_SLUG"]
        assert (isolated_tmpdir / f"codemap-incremental-noop-{slug}-shared").is_file()

    def test_absent_when_prior_index_exists(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], fake_repo: Path, isolated_tmpdir: Path
    ) -> None:
        """Skip the incremental sentinel when a prior index exists.

        A project that already has ``.cache/codemap/<name>.json`` can be scanned incrementally, so neither the sentinel
        nor the "No prior index" message may appear.
        """
        cache_dir = fake_repo / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        (cache_dir / f"{fake_repo.name}.json").write_text("{}")

        r = run_setup("--arguments", "--incremental")

        assert r.returncode == 0, r.stderr
        assert "No prior index" not in r.stderr
        slug = _read_state(Path(r.stdout.strip()))["PROJ_SLUG"]
        assert not (isolated_tmpdir / f"codemap-incremental-noop-{slug}-shared").exists()

    def test_lookalike_flag_is_rejected_not_enabled(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]]
    ) -> None:
        """An option that only resembles the incremental flag exits 5 instead of enabling incremental mode.

        ``--incremental-foo`` shares a prefix with the real flag; prefix matching would silently turn it into one.
        """
        r = run_setup("--arguments", "--incremental-foo")

        assert r.returncode == 5, r.stderr
        assert "! Unknown flag(s): --incremental-foo" in r.stderr
        assert "No prior index" not in r.stderr


@_skip_posix_entrypoints_unavailable
class TestUntrustedPluginRoot:
    """``SCAN_BIN`` comes from this install, never from the environment or the working directory."""

    def test_foreign_plugin_root_is_rejected(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], isolated_tmpdir: Path, tmp_path: Path
    ) -> None:
        """A ``CLAUDE_PLUGIN_ROOT`` naming another directory exits 1 even when it ships an executable ``scan-index``.

        The old check only asked whether ``<root>/bin/scan-index`` was executable, so a hostile directory with such a
        file was accepted and its path was written into the state file the caller sources.
        """
        evil = tmp_path / "evil-plugin"
        _executable_scan_index(evil)

        r = run_setup("--arguments", "", plugin_root=evil)

        assert r.returncode == 1
        assert "not this script's plugin root" in r.stderr
        assert r.stdout.strip() == ""
        assert list(isolated_tmpdir.iterdir()) == []

    def test_unset_plugin_root_ignores_working_directory_plugin(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], fake_repo: Path
    ) -> None:
        """An empty ``CLAUDE_PLUGIN_ROOT`` resolves to this install, not to ``plugins/codemap-py`` under the cwd.

        The former default was cwd-relative, so running inside an untrusted checkout that carried an executable
        ``plugins/codemap-py/bin/scan-index`` put that checkout's path into ``SCAN_BIN``.
        """
        planted = _executable_scan_index(fake_repo / "plugins" / "codemap-py")

        r = run_setup("--arguments", "", plugin_root="")

        assert r.returncode == 0, r.stderr
        scan_bin = _read_state(Path(r.stdout.strip()))["SCAN_BIN"]
        assert Path(scan_bin) == (PLUGIN_ROOT / "bin" / "scan-index").resolve()
        assert Path(scan_bin) != planted


@_skip_posix_entrypoints_unavailable
class TestSessionSuffix:
    """The ``CSID`` suffix names files inside the temp dir and nothing else."""

    @pytest.mark.parametrize("bad_csid", ["../escaped", "a/b", "..", "x y"])
    def test_unsafe_csid_falls_back_to_shared(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], isolated_tmpdir: Path, bad_csid: str
    ) -> None:
        """A ``CSID`` that is not a plain filename component is replaced by ``shared`` with a stderr warning.

        Left verbatim, ``../escaped`` or a value with a separator would move the sentinel writes out of ``TMPDIR``.
        """
        r = run_setup("--arguments", "", extra_env={"CSID": bad_csid})

        assert r.returncode == 0, r.stderr
        assert "CSID is not a plain filename component" in r.stderr
        assert (isolated_tmpdir / "codemap-proj-slug-shared").is_file()

    def test_plain_csid_is_the_terminal_suffix(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], isolated_tmpdir: Path
    ) -> None:
        """A UUID-shaped ``CSID`` is kept as the terminal suffix of the handoff files."""
        r = run_setup("--arguments", "", extra_env={"CSID": "8f3a-1c.x_9"})

        assert r.returncode == 0, r.stderr
        assert (isolated_tmpdir / "codemap-proj-slug-8f3a-1c.x_9").is_file()


@_skip_posix_entrypoints_unavailable
class TestPlantedFiles:
    """Files planted at the predictable sentinel names fail the run loudly instead of being followed."""

    def test_directory_at_handoff_name_exits_write_failed(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], isolated_tmpdir: Path
    ) -> None:
        """A directory at a handoff filename makes the rename fail: exit 4, no traceback, no temp-file residue.

        The rename refusal used to surface as an uncaught ``OSError``; a caller that ignored the exit status could then
        read whatever the planting party left behind.
        """
        (isolated_tmpdir / "codemap-proj-slug-shared").mkdir()

        r = run_setup("--arguments", "")

        assert r.returncode == 4
        assert "cannot write state" in r.stderr
        assert "Traceback" not in r.stderr
        assert r.stdout.strip() == ""
        assert not [p for p in isolated_tmpdir.iterdir() if p.name.startswith(".codemap-setup-tmp.")]

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no O_NOFOLLOW")
    def test_symlink_at_incremental_sentinel_is_not_followed(
        self, run_setup: Callable[..., subprocess.CompletedProcess[str]], isolated_tmpdir: Path, tmp_path: Path
    ) -> None:
        """A symlink planted at the incremental sentinel name is refused rather than created through.

        ``Path.touch`` follows a final-component symlink and creates its target; the dangling target here would
        therefore have appeared. The run must instead exit 4 and leave the target absent.
        """
        first = run_setup("--arguments", "--incremental")
        assert first.returncode == 0, first.stderr
        slug = _read_state(Path(first.stdout.strip()))["PROJ_SLUG"]
        sentinel = isolated_tmpdir / f"codemap-incremental-noop-{slug}-shared"
        sentinel.unlink()
        victim = tmp_path / "victim-created-through-link"
        sentinel.symlink_to(victim)

        second = run_setup("--arguments", "--incremental")

        assert second.returncode == 4
        assert not victim.exists()


@_skip_posix_entrypoints_unavailable
class TestShimDelegation:
    """Require the shell wrapper to pass arguments transparently to the Python port."""

    def test_shim_matches_port(self, fake_repo: Path, tmp_path: Path) -> None:
        """Same invocation through both entry points yields the same exit code and state.

        The state-file *path* differs by construction (``mkstemp`` picks a fresh name each run), so equality is asserted
        on the sourced fields instead.
        """
        py_tmp, sh_tmp = tmp_path / "py-tmp", tmp_path / "sh-tmp"
        py_tmp.mkdir()
        sh_tmp.mkdir()
        args = ("--arguments", "--root . --incremental")
        plugin_env = {"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)}

        py = _run_setup(_PY_ARGV, *args, env={**plugin_env, "TMPDIR": str(py_tmp)}, cwd=str(fake_repo))
        sh = _run_setup(_SH_ARGV, *args, env={**plugin_env, "TMPDIR": str(sh_tmp)}, cwd=str(fake_repo))

        assert (py.returncode, sh.returncode) == (0, 0), f"py={py.stderr} sh={sh.stderr}"
        assert py.stderr == sh.stderr
        assert _read_state(Path(py.stdout.strip())) == _read_state(Path(sh.stdout.strip()))
        assert _tmpfile_shapes(py_tmp) == _tmpfile_shapes(sh_tmp)
