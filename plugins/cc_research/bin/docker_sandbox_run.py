#!/usr/bin/env python3
"""Run a command inside a sandboxed ``python:3.11-slim`` Docker container.

Two modes:
    ``--mode explore`` — read-only project mount; runs an exploratory script by path.
    ``--mode verify``  — read-only project mount + read-write ``.experiments`` mount; runs a
        metric command. The command is rejected if it contains shell metacharacters or
        destructive binaries (``rm``, ``dd``, ``truncate`` …) that could wipe the
        read-write ``.experiments`` mount — run non-trivial logic via a script entry point.

Limitation: that destructive-binary rejection is a defense-in-depth speed bump against
*accidental* wipes, not a security boundary — a deliberate payload routed through a sanctioned
interpreter (``python -c "...rmtree(...)"``) passes it; containment is the Docker isolation flags.

Network defaults to ``none``; override via ``SANDBOX_NETWORK`` environment variable.
Each ``docker run`` is capped at ``SANDBOX_TIMEOUT_SEC`` seconds (default 600) as a host-side
backstop; on expiry the container is killed via its ``--cidfile`` id and ``124`` is returned.
Every container also carries in-container resource quotas — ``--cpus`` (``SANDBOX_CPUS``,
default 2), ``--memory`` (``SANDBOX_MEMORY``, default ``2g``) and ``--pids-limit``
(``SANDBOX_PIDS_LIMIT``, default 512) — so a runaway metric command is throttled where it runs
instead of only being reaped at the wall-clock deadline. A malformed or non-positive override
falls back to its default; the quotas can be loosened or tightened, never disabled.

Usage:
    docker_sandbox_run.py --mode explore <script-path>
    docker_sandbox_run.py --mode verify  <metric-cmd>

Exit codes:
    Forwarded from ``docker run``; ``2`` = bad CLI args; ``124`` = timeout; ``127`` = no docker.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

IMAGE = "python:3.11-slim"
TMPFS_SIZE = "256m"
TMPFS_MOUNT = f"/tmp:rw,size={TMPFS_SIZE}"
DEFAULT_NETWORK = "none"
# Wall-clock cap for one ``docker run``; override via ``SANDBOX_TIMEOUT_SEC``.  The resource
# quotas below throttle a runaway container but never end it, so this deadline stays the only
# thing that stops a merely *slow* command.  Callers with their own shorter cap hit theirs first.
DEFAULT_TIMEOUT_SEC = 600
# Cap for the post-timeout ``docker kill`` itself — a wedged daemon must not re-hang the exit path.
_KILL_TIMEOUT_SEC = 15
# Per-container resource quotas, sized for the workload these sandboxes actually run — a pytest
# metric command or a short exploratory script — on a developer laptop: high enough that a
# legitimate run never notices them, low enough that a runaway one leaves the host usable.
# 2 cores: fits pytest with a couple of workers while leaving cores for the host on a 4-core
# machine; a spin loop then pins 2 cores instead of every core.  A host with fewer cores to
# spare lowers it via ``SANDBOX_CPUS`` — fractional values are allowed (``0.5``).
DEFAULT_CPUS = 2.0
# 2 GiB: covers the interpreter plus the usual scientific imports with headroom.  A leaking run
# is OOM-killed inside the container instead of pushing the host into swap.
DEFAULT_MEMORY = "2g"
# 512 processes: far above what pytest and its subprocesses need, low enough that a fork bomb
# exhausts the container's own allowance rather than the host pid table.
DEFAULT_PIDS_LIMIT = 512
# Accepted ``SANDBOX_MEMORY`` shape: positive number, optional docker size suffix (``512m``,
# ``2g``, ``1.5g``, or a bare byte count).  Anything else falls back to the default.
_MEMORY_PATTERN = re.compile(r"^\d+(?:\.\d+)?[bkmg]?$", re.IGNORECASE)
_MEMORY_SUFFIXES = "bkmgBKMG"
# Docker network modes that preserve sandbox isolation.  ``host`` is excluded by
# policy: it removes network namespace isolation and would allow exfiltration
# from inside the verify-mode container.
_ALLOWED_NETWORK_MODES: frozenset[str] = frozenset({"none", "bridge", "internal"})
# Shell metacharacters forbidden in verify-mode command strings.  These reach
# ``sh -c`` inside the container; ``SANDBOX_NETWORK=host`` would otherwise allow
# network exfiltration via embedded ``$(...)``, backticks, redirection, etc.
_VERIFY_FORBIDDEN_CHARS = frozenset(";&|$`<>\n\r\\")
# Destructive binaries forbidden as bare command tokens in verify mode.  The
# ``.experiments`` host dir is the one read-write mount; the metachar filter
# above blocks command *chaining* but not a single space-separated destructive
# invocation (e.g. ``rm -rf /workspace/.experiments/state``), which would silently
# wipe prior-iteration state that retro/significance analysis depends on.
# Defense-in-depth: reject these as whole-word tokens.  Legitimate metric commands
# (``pytest``, ``python``, ``echo`` …) are unaffected.
# Limitation: a speed bump against *accidental* destruction, not a security boundary — it
# cannot stop deliberate destruction expressed as a ``python -c`` interpreter payload.
# Such a payload token-splits to nothing here. Containment is the Docker
# isolation flags in the argv builders below, not this blocklist.
_VERIFY_FORBIDDEN_TOKENS: frozenset[str] = frozenset(
    {"rm", "rmdir", "unlink", "shred", "truncate", "dd", "mv", "mkfs", "find", "chmod", "chown"}
)


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Resource quotas applied to a single sandbox container.

    Each field renders to the ``docker run`` flag of the same purpose. Defaults are the
    module-level ``DEFAULT_*`` constants; :func:`_resolve_limits` builds an instance from the
    ``SANDBOX_*`` environment overrides.

    Attributes:
        cpus: CPU allowance for ``--cpus``; fractional cores allowed.
        memory: Memory allowance for ``--memory`` as a docker size string.
        pids: Process-count allowance for ``--pids-limit``.

    Examples:
        >>> SandboxLimits().as_flags()
        ['--cpus', '2', '--memory', '2g', '--pids-limit', '512']
        >>> SandboxLimits(cpus=1.5, memory="512m", pids=64).as_flags()
        ['--cpus', '1.5', '--memory', '512m', '--pids-limit', '64']
    """

    cpus: float = DEFAULT_CPUS
    memory: str = DEFAULT_MEMORY
    pids: int = DEFAULT_PIDS_LIMIT

    def as_flags(self) -> list[str]:
        """Render the quotas as ``docker run`` flags.

        Returns:
            Flag/value pairs in a stable order, ready to splice into a docker argv.

        Examples:
            >>> SandboxLimits(cpus=4, memory="8g", pids=1024).as_flags()[:2]
            ['--cpus', '4']
        """
        return ["--cpus", f"{self.cpus:g}", "--memory", self.memory, "--pids-limit", str(self.pids)]


#: Quotas used when a caller supplies none — immutable, so it is safe as a parameter default.
DEFAULT_LIMITS = SandboxLimits()


def find_destructive_tokens(arg: str) -> list[str]:
    """Return sorted destructive command tokens found in a verify-mode command string.

    Splits on whitespace-equivalent word boundaries and matches whole tokens against
    :data:`_VERIFY_FORBIDDEN_TOKENS`. Substrings never match (``pytest_rm`` is safe;
    ``rm`` is not).

    Args:
        arg: The verify-mode command string destined for ``sh -c``.

    Returns:
        Sorted list of forbidden tokens present in ``arg`` (empty when none).

    Examples:
        >>> find_destructive_tokens("pytest -q metric.py")
        []
        >>> find_destructive_tokens("rm -rf /workspace/.experiments/state")
        ['rm']
        >>> find_destructive_tokens("truncate -s 0 log && dd of=x")
        ['dd', 'truncate']
        >>> find_destructive_tokens("python -c 'import armor'")
        []
    """
    tokens = set(re.split(r"[^A-Za-z0-9_]+", arg))
    return sorted(tokens & _VERIFY_FORBIDDEN_TOKENS)


def _verify_rejection_reason(arg: str) -> str | None:
    """Return why a verify-mode command must be rejected, or ``None`` when it may run.

    Metacharacters are checked before destructive binaries and the first failure wins, so a
    command failing both is reported by its more fundamental problem (metacharacter check before token check).

    Args:
        arg: The verify-mode command string destined for ``sh -c``.

    Returns:
        Rejection message (without the program-name prefix), or ``None`` when both filters pass.

    Examples:
        >>> _verify_rejection_reason("pytest -q metric.py") is None
        True
        >>> "metacharacters" in _verify_rejection_reason("echo a; echo b")
        True
        >>> "destructive binaries" in _verify_rejection_reason("rm -rf /workspace")
        True
    """
    unsafe = sorted({ch for ch in arg if ch in _VERIFY_FORBIDDEN_CHARS})
    if unsafe:
        return (
            f"verify-mode command contains shell metacharacters {unsafe!r}; "
            "use a script entry point instead of inline shell composition"
        )
    destructive = find_destructive_tokens(arg)
    if destructive:
        return (
            f"verify-mode command uses destructive binaries {destructive!r} "
            "that could wipe the read-write .experiments mount; run the metric via a script entry point"
        )
    return None


def build_explore_command(
    arg: str,
    network: str,
    workdir: str,
    cidfile: str | None = None,
    limits: SandboxLimits = DEFAULT_LIMITS,
) -> list[str]:
    """Build the ``docker run`` argv for explore mode.

    The script path may be given with a leading ``./``; it's stripped before being
    appended to the container's ``/workspace/`` prefix to match the bash version.

    Args:
        arg: Workspace-relative path to the exploratory script.
        network: Docker network mode (e.g. ``"none"``).
        workdir: Host directory mounted at ``/workspace`` (read-only).
        cidfile: Path docker writes the container id to. Omitted from the argv when ``None``;
            :func:`main` always supplies one so the timeout path has a kill handle.
        limits: Resource quotas for the container; defaults to :data:`DEFAULT_LIMITS`. An
            exploratory script is as capable of wedging the host as a metric command, so
            explore mode carries the same quotas as verify mode.

    Returns:
        Argument list ready for ``subprocess.run`` (no shell).

    Raises:
        ValueError: if the script path contains ``..`` components (path traversal)
            or resolves to an absolute path.

    Examples:
        >>> build_explore_command("scripts/explore.py", "none", "/proj")[:3]
        ['docker', 'run', '--rm']
        >>> build_explore_command("./scripts/x.py", "none", "/proj")[-1]
        '/workspace/scripts/x.py'
        >>> "--network" in build_explore_command("a.py", "none", "/proj")
        True
        >>> build_explore_command("a.py", "none", "/proj", cidfile="/c.cid")[3:5]
        ['--cidfile', '/c.cid']
        >>> cmd = build_explore_command("a.py", "none", "/proj")
        >>> cmd[cmd.index("--cpus") : cmd.index("--cpus") + 2]
        ['--cpus', '2']
        >>> cmd[cmd.index("--memory") : cmd.index("--memory") + 2]
        ['--memory', '2g']
        >>> cmd[cmd.index("--pids-limit") : cmd.index("--pids-limit") + 2]
        ['--pids-limit', '512']
        >>> build_explore_command("../etc/passwd", "none", "/proj")
        Traceback (most recent call last):
            ...
        ValueError: Path traversal not allowed in script path: '../etc/passwd'
        >>> build_explore_command("./scripts/../../etc/passwd", "none", "/proj")
        Traceback (most recent call last):
            ...
        ValueError: Path traversal not allowed in script path: './scripts/../../etc/passwd'
        >>> build_explore_command("/etc/passwd", "none", "/proj")
        Traceback (most recent call last):
            ...
        ValueError: Absolute script path not allowed: '/etc/passwd'
    """
    script_raw = arg.removeprefix("./")
    script_path = Path(script_raw)
    # On Windows Path("/etc/passwd").is_absolute() is False (no drive); check posix form too.
    if script_path.is_absolute() or script_path.as_posix().startswith("/"):
        raise ValueError(f"Absolute script path not allowed: {arg!r}")
    if any(part == ".." for part in script_path.parts):
        raise ValueError(f"Path traversal not allowed in script path: {arg!r}")
    script = script_raw
    return [
        "docker",
        "run",
        "--rm",
        *(("--cidfile", cidfile) if cidfile else ()),
        "--network",
        network,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        *limits.as_flags(),
        "-v",
        f"{workdir}:/workspace:ro",
        "--tmpfs",
        TMPFS_MOUNT,
        "-w",
        "/workspace",
        IMAGE,
        "python",
        f"/workspace/{script}",
    ]


def build_verify_command(
    arg: str,
    network: str,
    workdir: str,
    cidfile: str | None = None,
    limits: SandboxLimits = DEFAULT_LIMITS,
) -> list[str]:
    """Build the ``docker run`` argv for verify mode.

    The ``.experiments`` subdir of ``workdir`` is mounted read-write so metric runs may
    log into it. The supplied metric command is passed to ``sh -c`` inside the container.

    Args:
        arg: Shell string executed inside the container as ``sh -c <arg>``.
        network: Docker network mode (e.g. ``"none"``).
        workdir: Host directory mounted at ``/workspace`` (read-only); its
            ``.experiments`` subdir is mounted read-write.
        cidfile: Path docker writes the container id to. Omitted from the argv when ``None``;
            :func:`main` always supplies one so the timeout path has a kill handle.
        limits: Resource quotas for the container; defaults to :data:`DEFAULT_LIMITS`.

    Returns:
        Argument list ready for ``subprocess.run`` (no shell).

    Examples:
        >>> cmd = build_verify_command("pytest -q", "bridge", "/proj")
        >>> cmd[-3:]
        ['sh', '-c', 'pytest -q']
        >>> any("experiments:rw" in c for c in cmd)
        True
        >>> build_verify_command("pytest -q", "none", "/proj", cidfile="/c.cid")[3:5]
        ['--cidfile', '/c.cid']
        >>> cmd[cmd.index("--cpus") : cmd.index("--cpus") + 2]
        ['--cpus', '2']
        >>> cmd[cmd.index("--memory") : cmd.index("--memory") + 2]
        ['--memory', '2g']
        >>> cmd[cmd.index("--pids-limit") : cmd.index("--pids-limit") + 2]
        ['--pids-limit', '512']
        >>> tight = build_verify_command("pytest -q", "none", "/proj", limits=SandboxLimits(cpus=1))
        >>> tight[tight.index("--cpus") : tight.index("--cpus") + 2]
        ['--cpus', '1']
    """
    # arg in verify mode is treated as shell string — Docker container is primary isolation boundary
    return [
        "docker",
        "run",
        "--rm",
        *(("--cidfile", cidfile) if cidfile else ()),
        "--network",
        network,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        *limits.as_flags(),
        "-v",
        f"{workdir}:/workspace:ro",
        "-v",
        f"{workdir}/.experiments:/workspace/.experiments:rw",
        "--tmpfs",
        TMPFS_MOUNT,
        "-w",
        "/workspace",
        IMAGE,
        "sh",
        "-c",
        arg,
    ]


def _parse_args(argv: list[str]) -> tuple[str, str]:
    """Parse ``--mode`` and the single positional argument; mirrors the bash interface.

    Bash accepts ``--mode X`` and ``--mode=X``; the final non-flag token becomes ``ARG``.
    Unrecognised flags are not supported — we mirror bash's catch-all positional capture.

    Args:
        argv: Raw argv tokens (without program name).

    Returns:
        ``(mode, arg)`` tuple; empty strings when absent.
    """
    mode = ""
    arg = ""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--mode":
            mode = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if tok.startswith("--mode="):
            mode = tok[len("--mode=") :]
            i += 1
            continue
        # Bash assigns ARG=$1 for every non-mode token, so the *last* one wins.
        arg = tok
        i += 1
    return mode, arg


def _resolve_positive_float(raw: str | float, default: float) -> float:
    """Coerce an environment override into a positive float, falling back to ``default``.

    Shared contract for every numeric ``SANDBOX_*`` override: a malformed or non-positive value
    falls back rather than raising, because each of these is a containment backstop that must
    never end up disabled by a typo in the environment.

    Args:
        raw: Raw env value, or the default itself when the variable is unset.
        default: Value returned when ``raw`` is unparsable or ``<= 0``.

    Returns:
        The parsed value when positive, otherwise ``default``; always ``> 0``.

    Examples:
        >>> _resolve_positive_float("30", 600)
        30.0
        >>> _resolve_positive_float("not-a-number", 600)
        600.0
        >>> _resolve_positive_float("-5", 600)
        600.0
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)


def _resolve_timeout(raw: str | int) -> float:
    """Coerce a ``SANDBOX_TIMEOUT_SEC`` value into a positive number of seconds.

    A malformed or non-positive override falls back to :data:`DEFAULT_TIMEOUT_SEC` rather than
    raising: this is a backstop against a hung container, so it must never end up disabled.

    Args:
        raw: Raw env value, or :data:`DEFAULT_TIMEOUT_SEC` when the variable is unset.

    Returns:
        Timeout in seconds, always ``> 0``.

    Examples:
        >>> _resolve_timeout(600)
        600.0
        >>> _resolve_timeout("30")
        30.0
        >>> _resolve_timeout("not-a-number")
        600.0
        >>> _resolve_timeout("0")
        600.0
        >>> _resolve_timeout("-5")
        600.0
    """
    return _resolve_positive_float(raw, DEFAULT_TIMEOUT_SEC)


def _resolve_memory(raw: str) -> str:
    """Coerce a ``SANDBOX_MEMORY`` value into a docker size string.

    Same fallback contract as :func:`_resolve_timeout`: a value outside docker's size grammar — a
    bad suffix, a non-numeric value, zero — falls back to :data:`DEFAULT_MEMORY` instead of
    raising, so the memory cap survives a malformed override. Only the shape and positivity are
    checked here; docker still rejects a well-formed value it considers too small at run time.

    Args:
        raw: Raw env value, or :data:`DEFAULT_MEMORY` when the variable is unset.

    Returns:
        A docker size string accepted by ``--memory``; never empty, never zero.

    Examples:
        >>> _resolve_memory("512m")
        '512m'
        >>> _resolve_memory("1.5G")
        '1.5G'
        >>> _resolve_memory("4096")
        '4096'
        >>> _resolve_memory("2 gigs")
        '2g'
        >>> _resolve_memory("0m")
        '2g'
        >>> _resolve_memory("-1g")
        '2g'
    """
    candidate = str(raw).strip()
    if not _MEMORY_PATTERN.match(candidate):
        return DEFAULT_MEMORY
    return candidate if float(candidate.rstrip(_MEMORY_SUFFIXES)) > 0 else DEFAULT_MEMORY


def _resolve_pids_limit(raw: str | int) -> int:
    """Coerce a ``SANDBOX_PIDS_LIMIT`` value into a positive process count.

    Same fallback contract as :func:`_resolve_timeout`. A fractional override truncates toward
    zero, and a value that truncates to ``0`` falls back — docker reads ``0`` as *unlimited*,
    which would silently remove the fork-bomb ceiling this flag exists to impose.

    Args:
        raw: Raw env value, or :data:`DEFAULT_PIDS_LIMIT` when the variable is unset.

    Returns:
        Maximum process count, always ``>= 1``.

    Examples:
        >>> _resolve_pids_limit("64")
        64
        >>> _resolve_pids_limit("bogus")
        512
        >>> _resolve_pids_limit("0")
        512
        >>> _resolve_pids_limit("0.5")
        512
    """
    count = int(_resolve_positive_float(raw, DEFAULT_PIDS_LIMIT))
    return count if count > 0 else DEFAULT_PIDS_LIMIT


def _resolve_limits(env: Mapping[str, str]) -> SandboxLimits:
    """Build the container resource quotas from the ``SANDBOX_*`` environment overrides.

    Args:
        env: Environment mapping to read ``SANDBOX_CPUS``, ``SANDBOX_MEMORY`` and
            ``SANDBOX_PIDS_LIMIT`` from. Unset or empty values keep the module defaults.

    Returns:
        Quotas for one sandbox container; every field positive.

    Examples:
        >>> _resolve_limits({})
        SandboxLimits(cpus=2.0, memory='2g', pids=512)
        >>> _resolve_limits({"SANDBOX_CPUS": "1", "SANDBOX_MEMORY": "512m"})
        SandboxLimits(cpus=1.0, memory='512m', pids=512)
        >>> _resolve_limits({"SANDBOX_PIDS_LIMIT": "bogus"}).pids
        512
    """
    return SandboxLimits(
        cpus=_resolve_positive_float(env.get("SANDBOX_CPUS") or DEFAULT_CPUS, DEFAULT_CPUS),
        memory=_resolve_memory(env.get("SANDBOX_MEMORY") or DEFAULT_MEMORY),
        pids=_resolve_pids_limit(env.get("SANDBOX_PIDS_LIMIT") or DEFAULT_PIDS_LIMIT),
    )


def _kill_container(cidfile: str) -> None:
    """Best-effort ``docker kill`` of the container id recorded in ``cidfile``.

    Only called after a timeout, once ``subprocess.run`` has killed the docker *client*: the
    container itself survives that and would keep running unsupervised. Every failure here is
    swallowed — the caller is already returning a timeout exit code and has no better recourse.

    Args:
        cidfile: Path docker wrote the container id to; may be missing or empty.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        cid = Path(cidfile).read_text(encoding="utf-8").strip()
        if cid:
            subprocess.run(["docker", "kill", cid], check=False, timeout=_KILL_TIMEOUT_SEC, capture_output=True)


def _run_docker(cmd: list[str], timeout: float, cidfile: str) -> int:
    """Run a ``docker run`` argv under a wall-clock cap, killing the container on expiry.

    Args:
        cmd: Full ``docker run`` argv, containing ``--cidfile <cidfile>``.
        timeout: Wall-clock cap in seconds.
        cidfile: Path docker writes the container id to; removed before returning.

    Returns:
        Exit code from ``docker run``; ``124`` on timeout, ``127`` when docker is not installed.
    """
    try:
        result = subprocess.run(cmd, check=False, timeout=timeout)
        return result.returncode
    except FileNotFoundError:
        print("docker_sandbox_run.py: 'docker' binary not found in PATH", file=sys.stderr)
        return 127
    except subprocess.TimeoutExpired:
        print(f"docker_sandbox_run.py: sandbox exceeded {timeout:g}s timeout; killing container", file=sys.stderr)
        _kill_container(cidfile)
        return 124
    finally:
        # Suppressed: a failed cleanup must not replace the exit code we are about to return.
        with contextlib.suppress(OSError):
            Path(cidfile).unlink(missing_ok=True)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None, cwd: str | None = None) -> int:
    """Entry point — mirrors ``docker-sandbox-run.sh`` behaviour exactly.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).
        env: Optional environment mapping (defaults to ``os.environ``).
        cwd: Optional working directory used as host mount (defaults to ``os.getcwd()``).

    Returns:
        Exit code forwarded from ``docker run``; ``2`` on bad CLI args, ``124`` on timeout.
    """
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    workdir = Path(cwd).as_posix() if cwd else Path(os.getcwd()).as_posix()

    # Honour only ``-h/--help`` via argparse; every other token flows through the manual
    # _parse_args below, which mirrors the bash interface (last non-flag token wins,
    # --mode=X form, bad mode/arg → exit 2). argparse's own positional/choices errors
    # would change the exit-2 message and the last-token-wins capture — keep the
    # manual parser as the sole argv authority so the observable contract is unchanged.
    if raw_argv in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="docker_sandbox_run.py",
            description="Run a command inside a sandboxed python:3.11-slim Docker container.",
        ).parse_args(["-h"])

    mode, arg = _parse_args(raw_argv)

    if not mode or not arg:
        print(
            "usage: docker_sandbox_run.py --mode <explore|verify> <script-path-or-metric-cmd>",
            file=sys.stderr,
        )
        return 2

    network = env.get("SANDBOX_NETWORK") or DEFAULT_NETWORK
    timeout = _resolve_timeout(env.get("SANDBOX_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC)
    limits = _resolve_limits(env)
    if network not in _ALLOWED_NETWORK_MODES:
        # ``host`` is explicitly rejected — it removes network-namespace isolation.
        print(
            f"docker_sandbox_run.py: Disallowed SANDBOX_NETWORK: {network!r} "
            f"(allowed: {sorted(_ALLOWED_NETWORK_MODES)})",
            file=sys.stderr,
        )
        return 2

    # Container-id handle for the post-timeout kill: unique per invocation so concurrent sandbox
    # runs never share one, and never pre-created here — docker writes the file itself.
    cidfile = str(Path(tempfile.gettempdir()) / f"docker-sandbox-{uuid.uuid4().hex}.cid")

    if mode == "explore":
        try:
            cmd = build_explore_command(arg, network, workdir, cidfile=cidfile, limits=limits)
        except ValueError as exc:
            print(f"docker_sandbox_run.py: {exc}", file=sys.stderr)
            return 2
    elif mode == "verify":
        # Verify mode forwards ``arg`` to ``sh -c`` inside the container: metacharacters can
        # chain arbitrary commands even on non-host networks, and a bare destructive
        # binary needs no metacharacter to wipe the read-write ``.experiments`` mount.
        reason = _verify_rejection_reason(arg)
        if reason:
            print(f"docker_sandbox_run.py: {reason}", file=sys.stderr)
            return 2
        cmd = build_verify_command(arg, network, workdir, cidfile=cidfile, limits=limits)
    else:
        # argparse choices should have rejected, but guard anyway.
        print(f"unknown mode: {mode} (expected: explore|verify)", file=sys.stderr)
        return 2

    return _run_docker(cmd, timeout, cidfile)


if __name__ == "__main__":
    sys.exit(main())
