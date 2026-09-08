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

import os
import re
import shlex
import shutil
import subprocess
import sys
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
    e = {**os.environ, **(env or {})}
    # Force the script's CSID = os.environ.get("CSID") or "shared" fallback regardless of
    # the host shell — a real Claude Code session (like the one running this suite) never
    # sets CSID itself, only CLAUDE_CODE_SESSION_ID (which the script does not read).
    e.pop("CSID", None)
    return subprocess.run(
        [*launcher, *args],
        capture_output=True,
        text=True,
        env=e,
        cwd=cwd,
    )


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
    d = tmp_path / "tmp"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


_skip_posix_entrypoints_unavailable = pytest.mark.skipif(
    POSIX_ENTRYPOINTS_UNAVAILABLE,
    reason="the bash shim and the source-based state reader need a POSIX shell",
)


@_skip_posix_entrypoints_unavailable
class TestArgumentValidation:
    """Bad CLI shapes must fail fast with exit 3 and never touch tmpfiles."""

    def test_unknown_flag(self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path) -> None:
        """An unknown long flag exits 3 with a stderr message."""
        r = _run_setup(
            launcher,
            "--bogus",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == _EXIT_BAD_ARGS
        assert "unknown argument" in r.stderr

    def test_arguments_without_value(self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path) -> None:
        """Reject an arguments option without a following value."""
        r = _run_setup(
            launcher,
            "--arguments",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == _EXIT_BAD_ARGS
        assert "needs a value" in r.stderr

    def test_arguments_equals_form(self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path) -> None:
        """Accept the equals form of the arguments option."""
        r = _run_setup(
            launcher,
            "--arguments=--incremental",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 0, r.stderr
        assert _read_state(Path(r.stdout.strip()))["SCAN_ARGS_RAW"] == "--incremental"


# ---------------------------------------------------------------------------
# Missing scan-index binary
# ---------------------------------------------------------------------------


@_skip_posix_entrypoints_unavailable
class TestMissingScanIndex:
    """Bogus ``CLAUDE_PLUGIN_ROOT`` ⇒ scan-index validation fails (exit 1)."""

    def test_bogus_plugin_root(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path, tmp_path: Path
    ) -> None:
        """Pointing ``CLAUDE_PLUGIN_ROOT`` at an empty dir surfaces the missing-binary error."""
        empty = tmp_path / "no-plugin"
        empty.mkdir()
        r = _run_setup(
            launcher,
            "--arguments",
            "",
            env={"CLAUDE_PLUGIN_ROOT": str(empty), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 1
        assert "scan-index binary not found" in r.stderr
        # No state file should have been printed.
        assert r.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Happy path — state file + per-PROJ_SLUG tmpfiles
# ---------------------------------------------------------------------------


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


@_skip_posix_entrypoints_unavailable
class TestHappyPath:
    """Normal invocation produces a sourceable state file and per-slug tmpfiles."""

    def _run_minimal(self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path) -> dict[str, str]:
        """Run the minimal setup invocation and return the sourced state."""
        r = _run_setup(
            launcher,
            "--arguments",
            "",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 0, r.stderr
        state_path = Path(r.stdout.strip())
        assert state_path.is_file(), f"state file not created at {state_path}"
        return _read_state(state_path)

    def test_minimal_invocation_writes_sourceable_state(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path
    ) -> None:
        """No flags writes a sourceable state file with project identity and scan command fields."""
        state = self._run_minimal(launcher, fake_repo, isolated_tmpdir)
        assert state["PROJ_NAME"] == fake_repo.name
        # PROJ_SLUG ends in the sanitised repo basename — the script strips underscores
        # and other punctuation, so compare on the sanitised form.
        sanitised_repo = "".join(c for c in fake_repo.name if c.isalnum() or c == "-")
        assert state["PROJ_SLUG"].endswith(sanitised_repo)
        assert state["SCAN_BIN"].endswith("/bin/scan-index")
        assert state["SCAN_ARGS_RAW"] == ""

    def test_minimal_invocation_writes_per_slug_tmpfiles(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path
    ) -> None:
        """Per-PROJ_SLUG tmpfiles exist with content matching the sourceable state.

        ``CSID`` env var is unset in the test invocation (only inherited by real ``export CSID=...`` callers), so the
        script's ``"shared"`` fallback applies — every written filename carries a terminal ``-shared``.
        """
        state = self._run_minimal(launcher, fake_repo, isolated_tmpdir)
        slug = state["PROJ_SLUG"]
        assert (isolated_tmpdir / "codemap-proj-slug-shared").read_text() == slug
        assert (isolated_tmpdir / f"codemap-proj-name-{slug}-shared").read_text() == fake_repo.name
        assert (isolated_tmpdir / f"codemap-scan-bin-{slug}-shared").read_text() == state["SCAN_BIN"]
        assert (isolated_tmpdir / f"codemap-scan-args-{slug}-shared").read_text() == ""

    def test_minimal_invocation_does_not_create_incremental_sentinel(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path
    ) -> None:
        """No ``--incremental`` request leaves the fallback sentinel absent."""
        state = self._run_minimal(launcher, fake_repo, isolated_tmpdir)
        slug = state["PROJ_SLUG"]
        assert not (isolated_tmpdir / f"codemap-incremental-noop-{slug}-shared").exists()

    def test_root_flag_overrides_proj_name(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path, tmp_path: Path
    ) -> None:
        """Derive the project name from an explicitly selected root."""
        other = tmp_path / "alt-project"
        other.mkdir()
        r = _run_setup(
            launcher,
            "--arguments",
            f"--root {other}",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 0, r.stderr
        state = _read_state(Path(r.stdout.strip()))
        assert state["PROJ_NAME"] == "alt-project"
        assert state["SCAN_ARGS_RAW"] == f"--root {other}"

    def test_root_flag_with_spaces_overrides_proj_name(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path, tmp_path: Path
    ) -> None:
        """A quoted ``--root`` path containing spaces still drives project identity."""
        other = tmp_path / "alt project with spaces"
        other.mkdir()
        raw_args = f"--root {shlex.quote(str(other))}"
        r = _run_setup(
            launcher,
            "--arguments",
            raw_args,
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 0, r.stderr
        state = _read_state(Path(r.stdout.strip()))
        assert state["PROJ_NAME"] == other.name
        assert str(other) in state["SCAN_ARGS_RAW"]

    def test_root_dot_keeps_repo_basename(self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path) -> None:
        """Resolve like an absent ``--root`` — basename('.') would be empty."""
        r = _run_setup(
            launcher,
            "--arguments",
            "--root .",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 0, r.stderr
        assert _read_state(Path(r.stdout.strip()))["PROJ_NAME"] == fake_repo.name


# ---------------------------------------------------------------------------
# ``--incremental`` sentinel behaviour
# ---------------------------------------------------------------------------


@_skip_posix_entrypoints_unavailable
class TestIncrementalSentinel:
    """Combine incremental mode with prior-index detection."""

    def test_sentinel_created_when_no_prior_index(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path
    ) -> None:
        """Write the incremental sentinel when no prior index exists."""
        r = _run_setup(
            launcher,
            "--arguments",
            "--incremental",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 0, r.stderr
        # Informational message routed to stderr — keeps stdout reserved for state path.
        assert "No prior index" in r.stderr

        state = _read_state(Path(r.stdout.strip()))
        slug = state["PROJ_SLUG"]
        sentinel = isolated_tmpdir / f"codemap-incremental-noop-{slug}-shared"
        assert sentinel.is_file(), "incremental-noop sentinel should have been created"

    def test_sentinel_absent_when_prior_index_exists(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path
    ) -> None:
        """Skip the incremental sentinel when a prior index exists."""
        cache_dir = fake_repo / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        (cache_dir / f"{fake_repo.name}.json").write_text("{}")

        r = _run_setup(
            launcher,
            "--arguments",
            "--incremental",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 0, r.stderr
        assert "No prior index" not in r.stderr

        state = _read_state(Path(r.stdout.strip()))
        slug = state["PROJ_SLUG"]
        assert not (isolated_tmpdir / f"codemap-incremental-noop-{slug}-shared").exists()

    def test_sentinel_absent_for_prefixed_lookalike_flag(
        self, launcher: list[str], fake_repo: Path, isolated_tmpdir: Path
    ) -> None:
        """Ignore an option that only resembles the incremental flag."""
        r = _run_setup(
            launcher,
            "--arguments",
            "--incremental-foo",
            env={"CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 0, r.stderr
        assert "No prior index" not in r.stderr


# ---------------------------------------------------------------------------
# Shim delegation
# ---------------------------------------------------------------------------


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
    masked = (re.sub(r"-\d+-", "-<pid>-", p.name) for p in tmpdir.iterdir())
    return sorted(re.sub(r"^codemap-scan-state-.*$", "codemap-scan-state-<rand>", name) for name in masked)


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

    def test_shim_propagates_failure_exit_code(self, fake_repo: Path, isolated_tmpdir: Path, tmp_path: Path) -> None:
        """A non-zero exit from the port reaches the caller unchanged through the shim."""
        empty = tmp_path / "no-plugin"
        empty.mkdir()
        r = _run_setup(
            _SH_ARGV,
            "--arguments",
            "",
            env={"CLAUDE_PLUGIN_ROOT": str(empty), "TMPDIR": str(isolated_tmpdir)},
            cwd=str(fake_repo),
        )
        assert r.returncode == 1
        assert "scan-index binary not found" in r.stderr
