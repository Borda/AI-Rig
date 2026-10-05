#!/usr/bin/env python3
"""Disposable-source-copy runtime proof for the codemap-py install probes.

The developer git checkout cannot be deleted from a pytest process running inside
it, so the probes instead build from a DISPOSABLE COPY of the tracked plugin
source that is then deleted, making the source checkout unavailable without
touching the developer checkout:

1. capture the authoritative exec-mode map from the REAL repo's git index
   (``real_source_mode_map``) BEFORE any copy exists; copy the plugin working
   tree (minus caches/tests/junk) into a temp checkout OUTSIDE the repo and
   build the candidate by running THAT COPY's ``build_package.py --mode-map``
   pointed at the captured map (so the copy is ``SOURCE_ROOT`` but the copy's
   OWN synthesized git index is never consulted for modes) — the launcher
   therefore keeps its executable bit in the candidate even on a
   ``core.filemode=false`` host, where a fresh ``git add -A`` in the copy would
   otherwise record every file as ``100644`` regardless of its real bit;
2. install from the temp marketplace via the runtime CLI;
3. DELETE the whole temp source tree (copy + candidate + marketplace) — the source
   the installed bytes came from is now literally unavailable
   (``source_checkout_unavailable``);
4. execute ``doctor``/``index``/``query`` from the installed cache bytes THROUGH THE
   SHIPPED LAUNCHER (``bin/codemap-py``; no Python-entry fallback — a non-executable
   launcher is a probe failure, not a silent reroute) under a scrubbed env: no
   ``PYTHONPATH``/``CLAUDE_PLUGIN_*``/``CODEMAP_*`` (except a controlled non-forbidden
   ``CODEMAP_PYTHON``), and no forbidden path (temp checkout OR developer repo) in env,
   argv, or installed bytes — proving no channel back to any source tree.

The developer checkout itself is never referenced (proven by the env/argv/byte
scans over BOTH forbidden roots). Full developer-checkout deletion remains a
Phase 6 release-acceptance cell on a runner that can drop the whole workspace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import build_package

_MIN_MINOR, _MAX_MINOR_EXCLUSIVE = 11, 15
_MAJOR, _PROBE_FIELDS = "3", 3
# Version-specific names first: a bare ``python3`` on PATH may resolve to an
# ineligible build (e.g. a 3.10 framework python shadowing an eligible 3.14).
_INTERP_NAMES = ("python3.14", "python3.13", "python3.12", "python3.11", "python3", "python")
_STEPS: dict[str, list[str]] = {"doctor": ["doctor", "--json"], "index": ["index"], "query": ["query", "central"]}
_FORBIDDEN_ENV_KEYS = ("PYTHONPATH", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")
# Working-tree junk never copied into the disposable source (tests are not needed to build).
_STAGE_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    ".cache",
    ".reports",
    ".temp",
    ".pytest_cache",
    ".claude",
    "tests",
    "*.pyc",
    ".coverage*",
    ".DS_Store",
)


def stage_disposable_source(repo_root: Path, checkout_parent: Path) -> Path:
    """Copy the plugin working tree into a disposable checkout OUTSIDE the repo; return its root.

    ``git init`` + ``git add`` gives the copy a working index for reasons unrelated to exec
    modes or payload membership (e.g. other tools that expect a git root). The copy's own
    index is authority for NEITHER: a fresh ``git add -A`` on a ``core.filemode=false`` host
    records ``100644`` for every file regardless of its real on-disk bit, AND ``copytree``
    (filtered only by ``_STAGE_IGNORE`` junk patterns, not by tracked status) copies any
    untracked working-tree file straight into the copy — so the copy's own re-synthesized
    index would treat it as "tracked" there too. ``build_from_checkout`` always overrides the
    copy's index with the REAL repo's map (``real_source_mode_map``, captured before this copy
    is made) for BOTH modes and membership (``build_package._iter_source_payload`` draws
    ``_INCLUDE_DIRS`` membership from that same map's keys) — so an untracked file physically
    present in the copy (e.g. a concurrent wave's WIP) is still excluded from the shipped
    payload, never a build failure or a leaked file.
    """
    checkout = checkout_parent / "codemap-py"
    shutil.copytree(repo_root / "plugins" / "codemap-py", checkout, ignore=_STAGE_IGNORE)
    for argv in (["git", "-C", str(checkout), "init", "-q"], ["git", "-C", str(checkout), "add", "-A"]):
        subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True)
    return checkout


def real_source_mode_map(repo_root: Path) -> dict[str, bool]:
    """Return the authoritative exec-mode map from the REAL repo's git index.

    Must be called BEFORE ``stage_disposable_source`` makes any copy: the copy's own freshly synthesized index is not
    authoritative (see ``stage_disposable_source``). The real repo's tracked history IS authoritative regardless of the
    build host's ``core.filemode`` setting, since ``git ls-files --stage`` reports the mode recorded at commit time, not
    a live re-check of the working-tree bit.
    """
    return build_package._git_exec_modes(repo_root / "plugins" / "codemap-py")


def write_real_mode_map(repo_root: Path, dest: Path) -> Path:
    """Write the REAL repo's authoritative exec-mode map to ``dest`` as JSON; return ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(real_source_mode_map(repo_root)), encoding="utf-8")
    return dest


def build_from_checkout(checkout: Path, candidate: Path, mode_map_path: Path) -> tuple[bool, str]:
    """Build the candidate by running the disposable COPY's builder (copy is SOURCE_ROOT).

    ``mode_map_path`` (``write_real_mode_map``'s output) overrides the copy's own synthesized git index — the copy is
    never the mode authority.
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(checkout / "scripts" / "build_package.py"),
            "--out",
            str(candidate),
            "--mode-map",
            str(mode_map_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def _within(base: Path, path: Path) -> bool:
    """Return whether ``path`` is ``base`` itself or nested inside it (both resolved)."""
    base, path = base.resolve(), path.resolve()
    return path == base or base in path.parents


def _make_scratch_project(root: Path) -> Path:
    """Create a minimal two-module Python project the CLI can index and query."""
    proj = root / "scratch-proj"
    pkg = proj / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod_b.py").write_text("def b():\n    return 1\n")
    (pkg / "mod_a.py").write_text("from pkg.mod_b import b\n\n\ndef a():\n    return b()\n")
    return proj


def _nonforbidden_interpreter(forbidden: list[str], search_path: str) -> str | None:
    """Return an eligible CPython (3.11-3.14) on ``search_path`` not under any forbidden root."""
    for name in _INTERP_NAMES:
        found = shutil.which(name, path=search_path)
        if not found or any(root in str(Path(found).resolve()) for root in forbidden):
            continue
        try:
            out = subprocess.run(
                [found, "-c", "import sys;v=sys.version_info;print(sys.implementation.name,v.major,v.minor)"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        parts = out.stdout.split()
        if len(parts) != _PROBE_FIELDS:
            continue
        impl, major, minor = parts
        if (
            impl == "cpython"
            and major == _MAJOR
            and minor.isdigit()
            and _MIN_MINOR <= int(minor) < _MAX_MINOR_EXCLUSIVE
        ):
            return found
    return None


def _scrubbed_env(forbidden: list[str], interpreter: str | None) -> dict[str, str]:
    """Return an env with no forbidden path and no plugin/PYTHONPATH/CODEMAP leakage."""
    env: dict[str, str] = {}
    for key, val in os.environ.items():
        if key in _FORBIDDEN_ENV_KEYS or key.startswith("CODEMAP_"):
            continue
        if key == "PATH":
            env["PATH"] = os.pathsep.join(e for e in val.split(os.pathsep) if not any(r in e for r in forbidden))
            continue
        if not any(root in val for root in forbidden):
            env[key] = val
    if interpreter:
        env["CODEMAP_PYTHON"] = interpreter
    return env


def _env_is_clean(env: dict[str, str], forbidden: list[str]) -> bool:
    """Return whether the scrubbed env has no forbidden keys and no forbidden path anywhere."""
    if any(key in env for key in _FORBIDDEN_ENV_KEYS):
        return False
    for key, val in env.items():
        if key.startswith("CODEMAP_") and key != "CODEMAP_PYTHON":
            return False
        if any(root in val for root in forbidden):
            return False
    return True


def _launcher_path(installed_path: Path) -> Path:
    """Return the shipped launcher for the host (``codemap-py`` POSIX, ``.cmd`` on Windows)."""
    return installed_path / "bin" / ("codemap-py.cmd" if os.name == "nt" else "codemap-py")


def _launcher_executable(installed_path: Path) -> bool:
    """Return whether the shipped launcher can be executed directly from installed bytes."""
    launcher = _launcher_path(installed_path)
    return launcher.is_file() if os.name == "nt" else os.access(launcher, os.X_OK)


def _installed_cmd(installed_path: Path, args: list[str]) -> list[str]:
    """Run the CLI via the SHIPPED launcher only.

    There is deliberately NO Python-entry fallback: a non-executable launcher is a
    real failure (e.g. a build that stripped its executable bit), surfaced by the
    ``launcher_executable`` check and the command's non-zero exit — never silently
    routed around, which would hide exactly the metadata loss this proof exists to catch.
    """
    return [str(_launcher_path(installed_path)), *args]


def _no_source_refs(installed_path: Path, forbidden: list[str]) -> bool:
    """Return whether no installed-cache file embeds any forbidden root absolute path."""
    needles = [root.encode("utf-8") for root in forbidden]
    for path in installed_path.rglob("*"):
        if path.is_file():
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if any(needle in data for needle in needles):
                return False
    return True


def _run(cmd: list[str], proj: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one installed-CLI command; a non-executable launcher surfaces as exit 126, not a raise."""
    try:
        return subprocess.run(cmd, cwd=str(proj), env=env, capture_output=True, text=True, timeout=120, check=False)
    except OSError as error:
        return subprocess.CompletedProcess(cmd, returncode=126, stdout="", stderr=f"exec failed: {error}")


@dataclass(frozen=True)
class RuntimeProof:
    """Outcome of one source-independent runtime proof against installed bytes.

    Dataclass (not TypedDict): named construction and attribute access;
    converted to a dict by :meth:`to_dict` at the JSON-emission boundary.

    Attributes:
        ok: Whether every check in ``checks`` passed.
        checks: Per-channel verdicts (source deletion, env/argv/byte leaks, command exits).
        interpreter: Non-forbidden interpreter the launcher ran under, or ``None``.
        detail: Per-command stderr, or a ``skipped`` note when the proof did not run.
        execution_path: How the commands ran (``"launcher"``), or ``None`` when skipped.

    Examples:
        >>> RuntimeProof(ok=False, checks={}, detail={"skipped": "x"}).to_dict()["ok"]
        False
    """

    ok: bool
    checks: dict[str, bool]
    detail: dict[str, str]
    interpreter: str | None = None
    execution_path: str | None = None

    def to_dict(self) -> dict:
        """Return the JSON-envelope projection of this proof.

        Returns:
            A dict with ``ok``, ``checks``, ``execution_path``, ``interpreter``, ``detail``.

        Examples:
            >>> sorted(RuntimeProof(ok=True, checks={}, detail={}).to_dict())
            ['checks', 'detail', 'execution_path', 'interpreter', 'ok']
        """
        return {
            "ok": self.ok,
            "checks": self.checks,
            "execution_path": self.execution_path,
            "interpreter": self.interpreter,
            "detail": self.detail,
        }


def runtime_proof(
    installed_path: Path, workdir: Path, install_sources: list[Path], forbidden_roots: list[Path]
) -> RuntimeProof:
    """Delete the disposable source, then prove source-independent execution from installed bytes.

    Args:
        installed_path: the installed plugin root under the disposable cache.
        workdir: disposable dir holding the scratch project (survives deletion).
        install_sources: dirs to delete before execution (the temp source tree).
        forbidden_roots: paths that must not leak via env/argv/bytes (temp checkout + repo).

    Returns:
        A :class:`RuntimeProof` whose ``ok`` is true only when every check passed.
    """
    forbidden = [str(root.resolve()) for root in forbidden_roots]
    for source in install_sources:
        shutil.rmtree(source, ignore_errors=True)
    source_checkout_unavailable = all(not source.exists() for source in install_sources)

    search_path = os.pathsep.join(
        e for e in os.environ.get("PATH", "").split(os.pathsep) if not any(r in e for r in forbidden)
    )
    interpreter = _nonforbidden_interpreter(forbidden, search_path)
    env = _scrubbed_env(forbidden, interpreter)
    proj = _make_scratch_project(workdir)

    launcher_executable = _launcher_executable(installed_path)
    commands = {name: _installed_cmd(installed_path, args) for name, args in _STEPS.items()}
    argv_clean = all(root not in part for root in forbidden for cmd in commands.values() for part in cmd)
    runs = {name: _run(cmd, proj, env) for name, cmd in commands.items()}

    plugin_root_installed = False
    if runs["doctor"].returncode == 0:
        try:
            plugin_root_installed = _within(installed_path, Path(json.loads(runs["doctor"].stdout)["plugin_root"]))
        except (json.JSONDecodeError, KeyError, OSError):
            plugin_root_installed = False

    checks = {
        "source_checkout_unavailable": source_checkout_unavailable,
        "source_deleted": source_checkout_unavailable,
        "env_clean": _env_is_clean(env, forbidden),
        "argv_clean": argv_clean,
        "no_source_refs": _no_source_refs(installed_path, forbidden),
        "launcher_executable": launcher_executable,
        "doctor_ok": runs["doctor"].returncode == 0,
        "plugin_root_installed": plugin_root_installed,
        "index_ok": runs["index"].returncode == 0,
        "query_ok": runs["query"].returncode == 0,
    }
    return RuntimeProof(
        ok=all(checks.values()),
        checks=checks,
        detail={name: run.stderr.strip() for name, run in runs.items()},
        interpreter=interpreter,
        execution_path="launcher",
    )
