"""Run, supervise, and normalize one bridge request.

Purpose: Provide the single portable process boundary used by bridge skills and the MCP server. It turns an implement,
advise, or review request into a Codex or Claude child command, captures the child output, and returns one compact JSON
object rather than a transcript. Scope: This module owns argument construction, bounded foreground and detached
execution, JSONL parsing, contract validation, job records, transcript and incident files, and health logging. It
deliberately does not discover models, edit manifests, or implement a provider registry. Usage: Run ``bridge_call.py``
with the ``implement`` action and ``--task`` for a foreground request, or add ``--background`` and later use ``status``,
``result``, or ``cancel`` with the returned job id. ``bridge_mcp.py`` imports ``run_request`` for the reverse direction.
Outputs: Every command writes exactly one JSON object to stdout; artifacts live beneath ``<workspace>/.temp/bridge``.
Failure: Invalid requests, invalid model output, unavailable executables, timeouts, and structured child faults become a
validated result envelope and an incident when appropriate. Used by: Claude-facing bridge skills, the Codex-facing stdio
MCP server, and bridge diagnostics. The implementation supports Python 3.10+, uses Python's standard library, and uses
native ``taskkill`` process-tree control on Windows. Every termination path performs a bounded leader wait to collect
its actual exit status; unavailable status is diagnosed without inventing a return code.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import ntpath
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

#: Model passed to the child CLI when the caller names none; None leaves the choice to the child's own default.
DEFAULT_MODEL: str | None = None
#: Reasoning effort requested from the child when the caller gives none; must be one of EFFORTS.
DEFAULT_EFFORT = "medium"
#: Per-verb default wall-clock budget in seconds; its keys are also the set of valid bridge verbs.
DEFAULT_TIMEOUTS = {"implement": 600.0, "advise": 120.0, "review": 300.0}
#: Envelope fields returned to the caller, in order; every child answer must supply all of them.
CORE_FIELDS = ("status", "verdict", "findings", "files_touched", "remaining", "blockers")
#: Exact key set a child envelope must carry: the core fields plus the free-form details list.
PEER_FIELDS = (*CORE_FIELDS, "details")
#: Statuses a child itself may report in its envelope.
CORE_STATUSES = {"complete", "partial", "blocked"}
#: Statuses allowed in a finished result: the child-reported ones plus the bridge-assigned timeout and refused.
FINAL_STATUSES = CORE_STATUSES | {"timeout", "refused"}
#: Reasoning-effort levels ordered lowest to highest; a requested level is clamped against this order.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
#: Egress postures a Codex child may be started with; ``none`` is the default and the only one without a caller opt-in.
NETWORK_MODES = ("none", "github-read")
#: Hosts on which the ``github-read`` profile's enforcement was probed; elsewhere the opt-in is refused until verified.
_GITHUB_READ_PLATFORMS = ("darwin",)
#: Seconds one preflight probe under ``codex sandbox`` may take.
_PREFLIGHT_TIMEOUT = 30
#: Environment variable carrying the bridge nesting depth to child processes, used by the recursion guard.
DEPTH_ENVIRONMENT_VARIABLE = "CC_CODEX_BRIDGE_DEPTH"
#: Factor applied to the caller's timeout to get the hard cutoff at which the child process group is killed.
CHILD_TIMEOUT_MULTIPLIER = 1.2
#: Seconds a signalled child tree may take to exit on its own before the supervisor force-kills its process group.
CLEANUP_GRACE_SECONDS = 2.0
#: Seconds between liveness checks of the signalled child tree while the cleanup grace runs.
CLEANUP_POLL_SECONDS = 0.01
# This leaves substantial headroom below the smallest audited process argument
# limit after the Bridge prompt, schema, environment, and executable arguments.
#: Largest task text, in UTF-8 bytes, a request may carry before it is rejected as over the transport budget.
MAX_TASK_UTF8_BYTES = 16 * 1024
#: Largest assembled prompt, in UTF-8 bytes, that may be passed on the child command line.
MAX_PROMPT_UTF8_BYTES = 20 * 1024
#: Windows CreateProcess command-line limit, in UTF-16 code units, that a child command must stay within.
MAX_WINDOWS_COMMAND_UTF16_UNITS = 32_767
#: Stricter command-line limit, in UTF-16 code units, applied when the child runs through a Windows .cmd or .bat shim.
MAX_WINDOWS_BATCH_COMMAND_UTF16_UNITS = 8_000
#: Largest single line of child stdout, in bytes, that is buffered while parsing its JSON event stream.
MAX_CHILD_RECORD_BYTES = 256 * 1024
#: Largest child transcript file, in bytes, written to disk; longer payloads are truncated to this size.
MAX_CHILD_TRANSCRIPT_BYTES = 256 * 1024
#: Capture limit for child output in bytes: the transcript cap minus the stdout/stderr framing text added around it.
MAX_CHILD_OUTPUT_BYTES = MAX_CHILD_TRANSCRIPT_BYTES - len(b"stdout:\n\n\nstderr:\n\n")

# Environment passed to a bridge child, as an allowlist. Inheriting the parent
# environment wholesale hands every API key, cloud credential, and token in the
# session to the child; a bridge child needs its own provider auth and little
# else. Anything absent from these three sets is dropped.
#
# Base process needs, portable across hosts.
#: Basic process variables, portable across hosts, copied into the child environment when present.
CHILD_ENV_BASE_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL")
# Windows requires these or a child Python aborts during interpreter start-up
# with `_Py_HashRandomization_Init: failed to get random numbers`. SystemRoot in
# both spellings because the OS is case-insensitive and callers differ.
#: Variables additionally copied on Windows, without which a child Python fails to start.
CHILD_ENV_WINDOWS_KEYS = ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE")
# Provider auth and config discovery for the two supported children. Without
# these the child cannot authenticate and every call fails closed. Each child
# receives only its own vendor's set: a model-directed command inside a Codex
# child must not be able to read the Anthropic token, nor a Claude child the
# OpenAI key.
#: Provider credential, endpoint and config-directory variables copied so a Codex child can authenticate.
CHILD_ENV_CODEX_KEYS = ("CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL")
#: Provider credential, endpoint and config-directory variables copied so a Claude child can authenticate.
CHILD_ENV_CLAUDE_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
#: Both hosts' provider variables, used only for a child executable that is neither known host.
CHILD_ENV_PROVIDER_KEYS = (*CHILD_ENV_CODEX_KEYS, *CHILD_ENV_CLAUDE_KEYS)
#: Provider variable set selected by the child executable's name.
_CHILD_ENV_KEYS_BY_HOST = {"codex": CHILD_ENV_CODEX_KEYS, "claude": CHILD_ENV_CLAUDE_KEYS}
# Operator escape hatch: a comma-separated list of additional variable names to
# let through. The fixed sets above assume a direct-API-key login on a host with
# open egress; a corporate proxy (`HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`) or
# an enterprise auth path (`CLAUDE_CODE_USE_BEDROCK`, `AWS_PROFILE`,
# `GOOGLE_APPLICATION_CREDENTIALS`) needs names no fixed list can anticipate.
# Without this, such a host cannot use the bridge at all without editing plugin
# source, and the failure surfaces as an opaque provider error inside the child.
# Only the parent process can set this, so it widens nothing a caller controls.
#: Name of the operator-set variable listing extra comma-separated variable names to pass through to the child.
CHILD_ENV_EXTRA_VARIABLE = "BRIDGE_CHILD_ENV_EXTRA"
#: Pattern a name from the operator's extra-variable list must match to be passed to the child.
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Pattern for caller-supplied identifiers that reach child argv or prompt text: model, session id, run id.
#: The leading alphanumeric keeps a value from being parsed as a child CLI option.
_CALLER_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
#: Largest job record or cancellation marker, in bytes, read back from the artifact store; a record holds the task text,
#: bounded by the transport budget, plus one envelope, bounded by the child capture limit.
MAX_JOB_RECORD_BYTES = 1024 * 1024
#: Permission bits for bridge artifact directories, which hold task text and model output.
_ARTIFACT_DIRECTORY_MODE = 0o700
#: Permission bits for bridge artifact files: job records, transcripts and incident records.
_ARTIFACT_FILE_MODE = 0o600


class ArtifactBoundaryError(ValueError):
    """Report a rejected workspace artifact member without exposing its resolved destination."""


@dataclass(frozen=True)
class BridgePaths:
    """Contain paths for one workspace-local bridge artifact store."""

    workspace: Path
    artifact_root: Path = dataclass_field(init=False)

    def __post_init__(self) -> None:
        """Resolve and contain the artifact root before any bridge artifact operation."""
        try:
            workspace = self.workspace.resolve()
            artifact_root = (workspace / ".temp" / "bridge").resolve()
            artifact_root.relative_to(workspace)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("bridge artifact root escapes the trusted workspace") from error
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "artifact_root", artifact_root)

    @property
    def root(self) -> Path:
        """Return the workspace-local bridge artifact directory."""
        return self._member(self.artifact_root)

    @property
    def jobs(self) -> Path:
        """Return the detached-job directory."""
        return self._member(self.root / "jobs")

    @property
    def incidents(self) -> Path:
        """Return the incident-record directory."""
        return self._member(self.root / "incidents")

    def _member(self, path: Path, *, check_leaf: bool = True) -> Path:
        """Check current artifact ancestry and leaf ownership before each filesystem operation.

        Constructor containment cannot survive later directory replacement. Contained directory aliases remain valid;
        linked file leaves are rejected, and the health leaf keeps its stronger opened-descriptor checks separately.
        This check does not promise atomic protection against concurrent replacement during a subsequent OS operation.
        """
        try:
            path.relative_to(self.artifact_root)
            root = self.artifact_root.resolve()
            root.relative_to(self.workspace)
            if root != self.artifact_root:
                raise ValueError("artifact root changed")
            (path if check_leaf else path.parent).resolve().relative_to(root)
            if check_leaf:
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    metadata = None
                if metadata is not None and (
                    (_is_link_or_reparse_point(metadata) and not path.is_dir())
                    or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1)
                ):
                    raise ValueError("linked artifact file")
        except (OSError, RuntimeError, ValueError) as error:
            raise ArtifactBoundaryError(
                "bridge artifact member escapes or aliases the trusted artifact store"
            ) from error
        return path

    def prepare(self) -> None:
        """Create all bridge artifact directories for a request, accessible only to the current user.

        Job records and transcripts hold task text and model output, which the umask default would leave readable by
        other users of a shared host. ``chmod`` also tightens directories an earlier version created; where the platform
        or ownership refuses it, the request proceeds with the existing mode.
        """
        for directory in (self.root, self.jobs, self.incidents):
            member = self._member(directory)
            member.mkdir(mode=_ARTIFACT_DIRECTORY_MODE, parents=True, exist_ok=True)
            try:
                member.chmod(_ARTIFACT_DIRECTORY_MODE)
            except OSError:
                pass

    def relative(self, path: Path) -> str:
        """Return a stable workspace-relative artifact path when possible."""
        return self._member(path).relative_to(self.workspace).as_posix()


@dataclass(frozen=True)
class Request:
    """Represent one normalized bridge request before child dispatch."""

    verb: str
    task: str
    model: str | None
    effort: str
    timeout_seconds: float
    depth: int
    run_id: str
    workspace: Path
    direction: str
    background: bool = False
    session_id: str | None = None
    origin_workspace: Path | None = None
    supported_efforts: tuple[str, ...] = ()
    network: str = "none"


@dataclass(frozen=True)
class EnvelopeContext:
    """Bundle the request and observed-execution values every envelope of one attempt is built from.

    The same seven values repeat across each early-out and each post-execution branch of a single
    :func:`run_request` attempt, so the supervisor builds one context before the child runs and one after it, instead
    of threading the identical positional arguments through nine call sites. The context is an internal parameter
    bundle only: it is never serialized, and never reaches :func:`validate_envelope`.
    """

    request: Request
    paths: BridgePaths
    transcript_path: str | None
    tokens: dict[str, float]
    session_id: str | None
    duration: float
    substitution: dict[str, str] | None = None


def validate_caller_identifier(name: str, value: str | None) -> None:
    """Reject a caller-supplied model, session or run identifier that is not identifier-shaped.

    Both entry points, the CLI and the MCP server, pass these values into child argv or prompt text. A value starting
    with a dash would be parsed as a child CLI option, and free text would reach the child prompt and the logs.

    Args:
        name: Label used in the error message, such as ``--model`` or ``model``.
        value: Candidate value; ``None`` and the empty string mean "not supplied" and pass.

    Raises:
        ValueError: When ``value`` is not a string or does not match the identifier pattern.

    Examples:
        >>> validate_caller_identifier("--model", "gpt-5.1-codex")
        >>> validate_caller_identifier("--model", None)
        >>> validate_caller_identifier("--model", "-c")
        Traceback (most recent call last):
        ValueError: --model must match ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$
    """
    if value is None or value == "":
        return
    if not isinstance(value, str) or not _CALLER_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must match {_CALLER_IDENTIFIER.pattern}")


def validate_request_transport_budget(request: Request) -> None:
    """Reject task text whose encoded prompt cannot safely fit the local CLI transport."""
    if len(request.task.encode("utf-8")) > MAX_TASK_UTF8_BYTES:
        raise ValueError("task exceeds Bridge's encoded transport budget")
    if len(_prompt_with_budget(request).encode("utf-8")) > MAX_PROMPT_UTF8_BYTES:
        raise ValueError("task exceeds Bridge's encoded transport budget")
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "envelope.schema.json"
    command = (
        build_claude_argv(request, schema_path)
        if request.direction == "codex_to_claude"
        else build_codex_argv(request, schema_path)
    )
    # Missing peers still reach the spawn error path; this lookup only measures transport size.
    resolved_command = [_resolve_executable(command[0]) or command[0], *command[1:]]
    units = _windows_command_utf16_units(resolved_command)
    if units > MAX_WINDOWS_COMMAND_UTF16_UNITS or (
        _is_windows_batch_shim(resolved_command[0]) and units > MAX_WINDOWS_BATCH_COMMAND_UTF16_UNITS
    ):
        raise ValueError("task exceeds Bridge's encoded transport budget")


def _windows_command_utf16_units(command: list[str]) -> int:
    """Measure the Windows command line generated from one argv list, including its terminator."""
    return len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2 + 1


def _is_windows_batch_shim(executable: str) -> bool:
    """Identify a resolved Windows command shim whose Cmd.exe limit is lower than CreateProcess."""
    return Path(executable).suffix.lower() in {".bat", ".cmd"}


def validate_model_core(value: Any) -> dict[str, Any]:
    """Validate and return the strict peer result before removing transcript-only detail.

    Peer results include supporting ``details`` within the child capture budget for transcript storage. The supervisor
    removes that field before it validates and returns the public envelope without discarding evidence.
    """
    if not isinstance(value, dict) or set(value) != set(PEER_FIELDS):
        raise ValueError("model result must contain exactly the seven peer fields")
    if value["status"] not in CORE_STATUSES:
        raise ValueError("model result has an unsupported status")
    _validate_result_summary(value)
    _validate_string_list(value["details"], "details")
    return value


def _public_core(value: dict[str, Any]) -> dict[str, Any]:
    """Remove peer-only detail before the compact public-envelope boundary."""
    return {field: value[field] for field in CORE_FIELDS}


def _validate_result_summary(value: dict[str, Any]) -> None:
    """Enforce required public-result types without discarding valid captured facts."""
    verdict = value["verdict"]
    if not isinstance(verdict, str) or not verdict.strip():
        raise ValueError("model result verdict must be a non-empty string")
    for field in CORE_FIELDS[2:]:
        _validate_string_list(value[field], field)


def _validate_string_list(value: Any, field: str) -> None:
    """Reject malformed lists while retaining all facts within the capture budget."""
    if not isinstance(value, list):
        raise ValueError(f"model result {field} must be an array")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"model result {field} must contain strings")


def validate_envelope(value: Any) -> dict[str, Any]:
    """Validate and return the public harness-enriched result envelope."""
    required = {
        *CORE_FIELDS,
        "model",
        "effort",
        "effort_substituted",
        "cost",
        "tokens",
        "duration_seconds",
        "depth",
        "run_id",
        "incident",
        "session_id",
        "transcript_path",
        "verb",
        "direction",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("public envelope has missing or unknown fields")
    if value["status"] not in FINAL_STATUSES:
        raise ValueError("public envelope has an unsupported status")
    if value["verb"] not in DEFAULT_TIMEOUTS or value["direction"] not in {
        "claude_to_codex",
        "codex_to_claude",
    }:
        raise ValueError("public envelope has unsupported routing metadata")
    if isinstance(value["depth"], bool) or not isinstance(value["depth"], int) or value["depth"] < 0:
        raise ValueError("public envelope depth must be a non-negative integer")
    if (
        isinstance(value["duration_seconds"], bool)
        or not isinstance(value["duration_seconds"], (int, float))
        or value["duration_seconds"] < 0
    ):
        raise ValueError("public envelope duration_seconds must be non-negative")
    if value["cost"] is not None and (
        isinstance(value["cost"], bool) or not isinstance(value["cost"], (int, float)) or value["cost"] < 0
    ):
        raise ValueError("public envelope cost must be null or non-negative")
    if not isinstance(value["tokens"], dict) or not all(
        not isinstance(token, bool) and isinstance(token, (int, float)) and token >= 0
        for token in value["tokens"].values()
    ):
        raise ValueError("public envelope tokens must be a mapping of non-negative numbers")
    for field in ("model", "effort", "run_id", "transcript_path"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"public envelope {field} must be a non-empty string")
    for field in ("incident", "session_id"):
        if value[field] is not None and not isinstance(value[field], str):
            raise ValueError(f"public envelope {field} must be a string or null")
    substitution = value["effort_substituted"]
    if substitution is not None and (
        not isinstance(substitution, dict)
        or set(substitution) != {"requested", "applied", "reason"}
        or not all(isinstance(item, str) and item for item in substitution.values())
    ):
        raise ValueError("effort_substituted must be null or a complete substitution record")
    core = {field: value[field] for field in CORE_FIELDS}
    if value["status"] in CORE_STATUSES:
        _validate_result_summary(core)
    elif not isinstance(value["verdict"], str) or not value["verdict"]:
        raise ValueError("harness terminal result needs a verdict")
    return value


def _github_read_overrides(request: Request) -> list[str]:
    """Return the Codex overrides that allow only read requests to ``api.github.com``, on the verb's filesystem posture.

    The profile replaces the legacy sandbox mode rather than adding to it. ``codex exec`` enforces a domain allowlist
    only through a permission profile with the ``network_proxy`` feature on, and ``--ignore-user-config`` means the
    user's own profile is never loaded, so the bridge states every key itself:

    - ``network.mode="limited"`` restricts requests to GET, HEAD and OPTIONS; without it the domain allowlist alone lets
      an authenticated POST reach GitHub.
    - A profile that extends ``:workspace`` loses the protection the built-in profile gives ``.git``, ``.codex`` and
      ``.agents`` (writing a git hook or a Codex config would run code outside the sandbox), so each is set read-only.
    - The profile name is random per build, so no lower configuration layer (which a caller-chosen run id could be
      matched against) can predefine the same profile and merge extra domains into it.

    Args:
        request: Request whose verb picks the base profile (``implement`` extends the workspace profile, the read
            verbs the read-only one).

    Returns:
        Codex CLI arguments, to be placed where the sandbox selection would otherwise go.

    Examples:
        >>> from pathlib import Path
        >>> request = Request("advise", "t", None, "low", 1.0, 0, "run-1", Path("."), "claude_to_codex")
        >>> "--enable" in _github_read_overrides(request)
        True
    """
    base = ":workspace" if _is_write_verb(request.verb) else ":read-only"
    name = "bridge-github-" + uuid.uuid4().hex[:12]
    profile = f"permissions.{name}"
    protected = '{":workspace_roots"={".git"="read",".codex"="read",".agents"="read"}}'
    return [
        "-c",
        f"default_permissions={json.dumps(name)}",
        "-c",
        f"{profile}.extends={json.dumps(base)}",
        "-c",
        f"{profile}.filesystem={protected}",
        "-c",
        f"{profile}.network.enabled=true",
        "-c",
        f'{profile}.network.mode="limited"',
        "-c",
        f'{profile}.network.domains={{"api.github.com"="allow"}}',
        "--enable",
        "network_proxy",
    ]


def _sandbox_probe(
    overrides: list[str], workspace: Path, command: list[str], *, codex_home: Path, timeout: float
) -> tuple[int, str]:
    """Run one command under ``codex sandbox`` with the given overrides and return its exit code and stdout.

    ``CODEX_HOME`` points at an empty directory: the Codex child runs with ``--ignore-user-config``, so the user's
    config layer must not shape the probe either, and the proxy's certificate and log writes stay out of the real home.
    Managed requirements are not loaded (``codex sandbox`` applies them only with an explicit ``--permission-profile``,
    while the profile here is selected through ``default_permissions`` as in the child's run). On a host with managed
    requirements, whether they change the run's profile is unverified.
    """
    env = build_child_environment(host="codex")
    env["CODEX_HOME"] = str(codex_home)
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell
            _resolved_command(["codex", "sandbox", *overrides, "--", *command]),
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return result.returncode, result.stdout.strip()


def _curl_fields(output: str) -> tuple[str, str, str]:
    """Split the preflight's curl output into HTTP status, proxy CONNECT status and the proxy's refusal reason.

    Examples:
        >>> _curl_fields("403 000 blocked-by-method-policy")
        ('403', '000', 'blocked-by-method-policy')
        >>> _curl_fields("")
        ('', '', '')
    """
    status, connect, reason = [*output.split(maxsplit=2), "", "", ""][:3]
    return status, connect, reason.strip()


#: One preflight probe: what it proves, the command run in the sandbox, and the check on ``(exit code, curl fields)``.
_Probe = tuple[str, list[str], Callable[[int, tuple[str, str, str]], bool]]


def _preflight_probes(verb: str, workspace: Path) -> list[_Probe]:
    """List the preflight probes in order; the first is a control that proves the sandbox ran at all."""
    curl = ["curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code} %{http_connect} %header{x-proxy-error}"]
    if _is_write_verb(verb):
        control: _Probe = (
            "workspace write",
            ["/bin/sh", "-c", "echo x > control"],
            lambda code, _: code == 0 and (workspace / "control").exists(),
        )
    else:
        control = ("sandbox start", ["/bin/sh", "-c", "true"], lambda code, _: code == 0)
    return [
        control,
        (
            ".git write refusal",
            ["/bin/sh", "-c", "echo x > .git/preflight"],
            lambda code, _: code != 0 and not (workspace / ".git" / "preflight").exists(),
        ),
        # /rate_limit does not spend GitHub's unauthenticated quota, so the probe cannot rate-limit itself
        ("GET api.github.com", [*curl, "https://api.github.com/rate_limit"], lambda _, fields: fields[0] == "200"),
        (
            "POST refusal by the proxy",
            [*curl, "-X", "POST", "https://api.github.com/markdown"],
            lambda _, fields: fields[0] == "403" and fields[2] == "blocked-by-method-policy",
        ),
        (
            "example.com refusal",
            [*curl, "https://example.com"],
            lambda _, fields: fields[0] == "000" or fields[1] == "403" or fields[2] == "blocked-by-allowlist",
        ),
    ]


def _github_read_preflight(request: Request) -> str | None:
    """Prove on this Codex build that the ``github-read`` profile still enforces what the bridge relies on.

    Codex silently ignores an unknown profile key, so a release that renames ``network.mode`` or reshapes
    ``filesystem`` would drop that restriction without an error. Before an opted-in call, the same overrides run five
    probes under ``codex sandbox`` in a throwaway workspace with an empty Codex home, never the caller's:

    - a control: a trivial command must run (for a write verb, a workspace write must succeed), so the probes below
      cannot pass merely because the sandbox never started;
    - a write to ``.git`` must fail;
    - a GET to ``https://api.github.com/rate_limit`` must return 200 (that endpoint does not spend the quota);
    - a POST to ``https://api.github.com/markdown`` must get the proxy's 403 with ``x-proxy-error:
      blocked-by-method-policy``, not a status GitHub itself chose;
    - ``https://example.com`` must be refused by the proxy or unreachable.

    The whole preflight stays within the request's own time budget.

    Args:
        request: The opted-in request; its verb picks the base profile the probes use.

    Returns:
        ``None`` when every probe holds, otherwise a short description of the first one that did not.
    """
    deadline = time.monotonic() + request.timeout_seconds
    overrides = _github_read_overrides(request)
    with tempfile.TemporaryDirectory(prefix="bridge-preflight-") as scratch:
        workspace, codex_home = Path(scratch, "workspace"), Path(scratch, "codex-home")
        (workspace / ".git").mkdir(parents=True)
        codex_home.mkdir()
        for label, command, holds in _preflight_probes(request.verb, workspace):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"the request's time budget ran out before the {label} probe"
            code, output = _sandbox_probe(
                overrides, workspace, command, codex_home=codex_home, timeout=min(remaining, _PREFLIGHT_TIMEOUT)
            )
            if code < 0:  # timed out or could not spawn: a refusal check must not read that as "refused"
                return f"the {label} probe did not complete (timeout, spawn failure or signal)"
            if not holds(code, _curl_fields(output)):
                return f"{label} did not hold (exit {code}, output {output or 'none'!r})"
    return None


def build_codex_argv(request: Request, schema_path: Path) -> list[str]:
    """Build a verified local Codex CLI command for a bridge request."""
    if request.network not in NETWORK_MODES:
        raise ValueError(f"network must be one of: {', '.join(NETWORK_MODES)}")
    if request.network == "github-read" and sys.platform not in _GITHUB_READ_PLATFORMS:
        raise ValueError(
            f"--network github-read is verified on {', '.join(_GITHUB_READ_PLATFORMS)} only; "
            "fetch the GitHub data in the caller and pass it in the task file instead"
        )
    quoted_effort = json.dumps(request.effort)
    prompt = _prompt_with_budget(request)
    github_read = request.network == "github-read"
    common = [
        "-c",
        f"model_reasoning_effort={quoted_effort}",
        "-c",
        'approval_policy="never"',
        # Egress is denied explicitly rather than inherited. `workspace-write`
        # decides network access from `sandbox_workspace_write.network_access`,
        # so leaving it unset makes the bridge's egress posture a property of
        # whatever Codex defaults to in the installed version. Pinning it keeps
        # the child unable to send workspace contents anywhere, which is the
        # cheapest capability to remove: it still reads code and writes files.
        # A caller opt-in to `github-read` swaps this pin and `-s` for a
        # domain- and method-scoped permission profile (see _github_read_overrides).
        *([] if github_read else ["-c", "sandbox_workspace_write.network_access=false"]),
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--json",
        "--output-schema",
        str(schema_path),
    ]
    if request.model:
        common[0:0] = ["-m", request.model]
    if request.session_id:
        if not _is_write_verb(request.verb):
            raise ValueError("only implement may resume a Codex session")
        if request.origin_workspace is None or request.origin_workspace.resolve() != request.workspace.resolve():
            raise ValueError("resumed implement must use the originating workspace")
        return [
            "codex",
            "exec",
            "resume",
            request.session_id,
            prompt,
            *common,
            *(
                _github_read_overrides(request)
                if github_read
                else ["-c", f"sandbox_mode={json.dumps('workspace-write')}"]
            ),
        ]
    sandbox = "workspace-write" if _is_write_verb(request.verb) else "read-only"
    command = [
        "codex",
        "exec",
        *common,
        *(_github_read_overrides(request) if github_read else ["-s", sandbox]),
    ]
    if not _is_write_verb(request.verb):
        command.append("--ephemeral")
    return [*command, prompt]


def build_claude_argv(request: Request, schema_path: Path) -> list[str]:
    """Build the Claude print-mode command with its compatible peer schema."""
    if request.network != "none":
        raise ValueError("network opt-in is a Codex-side control with no equivalent for the Claude peer")
    effort, _ = normalize_effort(request.effort, "claude", request.supported_efforts)
    permission = "acceptEdits" if _is_write_verb(request.verb) else "plan"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema.pop("$schema", None)
    command = [
        "claude",
        "--print",
        _prompt_with_budget(request),
        "--effort",
        effort,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, sort_keys=True),
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--permission-mode",
        permission,
    ]
    if request.model:
        command[2:2] = ["--model", request.model]
    if not _is_write_verb(request.verb):
        command.extend(["--disallowed-tools", "Edit", "Write"])
    return command


def normalize_effort(
    requested: str, host: str, supported_efforts: Iterable[str] = ()
) -> tuple[str, dict[str, str] | None]:
    """Normalize a requested effort level and record one deterministic downgrade."""
    aliases = {"trivial": "minimal", "none": "minimal"}
    requested = aliases.get(requested, requested)
    if requested not in EFFORTS:
        raise ValueError(f"unsupported effort level: {requested}")
    host_value = "low" if host == "claude" and requested == "minimal" else requested
    supported = tuple(supported_efforts)
    if not supported or host_value in supported:
        return host_value, None
    positions = {level: index for index, level in enumerate(EFFORTS)}
    viable = [level for level in supported if level in positions and positions[level] <= positions.get(host_value, 0)]
    if not viable:
        raise ValueError(f"no supported effort at or below {host_value}")
    applied = max(viable, key=positions.__getitem__)
    return applied, {"requested": requested, "applied": applied, "reason": "target model capability"}


def _is_write_verb(verb: str) -> bool:
    """Identify the write-capable implement verb."""
    return verb == "implement"


def _unstarted_context(request: Request, paths: BridgePaths) -> EnvelopeContext:
    """Return the envelope context for a request rejected before any child observation exists."""
    return EnvelopeContext(request, paths, None, {}, None, 0.0)


def _lower_effort_retry(request: Request, budget: float) -> Request | None:
    """Build the one lower-effort retry of a request, or ``None`` when there is no lower tier or it would not fit.

    A lower tier's name can be longer than the one it replaces (``high`` to ``medium``), so a task near the transport
    limit can fit at the first effort and not at the retry's; skipping that retry keeps the first attempt's result
    instead of raising halfway through the call.
    """
    retry_effort = _lower_effort(request.effort, request.supported_efforts)
    if retry_effort is None:
        return None
    retry = Request(**{**request.__dict__, "timeout_seconds": budget, "effort": retry_effort})
    try:
        validate_request_transport_budget(retry)
    except ValueError:
        return None
    return retry


def run_request(
    request: Request,
    *,
    host: str = "codex",
    _attempt: int = 0,
    _recovery: dict[str, str] | None = None,
    _prior_incident: str | None = None,
    _job_path: Path | None = None,
) -> dict[str, Any]:
    """Run one foreground request and return a fully validated public envelope.

    A write-capable request also compares the workspace's protected paths (see :data:`PROTECTED_WORKSPACE_PATHS`) before
    and after the attempt and names any that changed in ``findings``: a peer could otherwise plant a Git hook or host
    settings that run code in a later session, without listing them in ``files_touched``.
    """
    guarded = _is_write_verb(request.verb) and _attempt == 0
    protected_before = _protected_state(request.workspace) if guarded else {}
    envelope = _run_request_attempt(
        request,
        host=host,
        _attempt=_attempt,
        _recovery=_recovery,
        _prior_incident=_prior_incident,
        _job_path=_job_path,
    )
    changed = _protected_changes(protected_before, request.workspace) if guarded else []
    if changed:
        envelope["findings"].append(
            "Protected workspace path changed during this write-capable peer run: "
            + ", ".join(changed)
            + ". Review it before any host session or Git command trusts this workspace."
        )
    return envelope


def _run_request_attempt(
    request: Request,
    *,
    host: str = "codex",
    _attempt: int = 0,
    _recovery: dict[str, str] | None = None,
    _prior_incident: str | None = None,
    _job_path: Path | None = None,
) -> dict[str, Any]:
    """Run one attempt of :func:`run_request`, including its bounded lower-effort retry."""
    if not math.isfinite(request.timeout_seconds) or request.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    validate_request_transport_budget(request)
    paths = BridgePaths(request.workspace.resolve())
    paths.prepare()
    try:
        request = _request_with_trusted_depth(request)
    except ValueError as error:
        # A corrupted depth variable or negative caller depth must surface as
        # a structured result, not as an opaque transport error; the envelope
        # itself needs a valid depth, so the rejected value is clamped there.
        reportable = Request(**{**request.__dict__, "depth": max(request.depth, 0)})
        return _terminal_envelope(_unstarted_context(reportable, paths), "blocked", str(error))
    if request.depth >= 1:
        return _terminal_envelope(_unstarted_context(request, paths), "refused", "recursion-depth")
    try:
        effective_effort, substitution = normalize_effort(request.effort, host, request.supported_efforts)
    except ValueError as error:
        return _terminal_envelope(_unstarted_context(request, paths), "blocked", str(error))
    effective_request = Request(**{**request.__dict__, "effort": effective_effort})
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "envelope.schema.json"
    command = (
        build_codex_argv(effective_request, schema_path)
        if host == "codex"
        else build_claude_argv(effective_request, schema_path)
    )
    preflight_seconds = 0.0
    if request.network == "github-read" and host == "codex" and _attempt == 0:
        # after validation and a cancel check, so a request that cannot run never reaches GitHub
        if _job_path is not None and _job_cancel_requested(_job_path, paths=paths):
            return _terminal_envelope(_unstarted_context(effective_request, paths), "blocked", "cancelled by job owner")
        preflight_started = time.monotonic()
        failure = _github_read_preflight(request)
        preflight_seconds = time.monotonic() - preflight_started
        # whole seconds: a fractional budget renders longer in the prompt and could push it past the transport cap
        remaining = math.floor(request.timeout_seconds - preflight_seconds)
        if failure is None and remaining < 1:
            failure = "it left less than one second of the request's time budget"
        if failure is not None:
            return _terminal_envelope(
                _unstarted_context(effective_request, paths),
                "refused",
                f"github-read preflight failed: {failure}",
                fault="network-preflight",
            )
        # from here on the request describes the child: its prompt, cutoff, retries and incidents use what remains
        effective_request = Request(**{**effective_request.__dict__, "timeout_seconds": float(remaining)})
        try:
            # the budget renders with ``{:g}``, whose length is not monotone (1e+06 vs 999998), so the shorter budget
            # can still lengthen the prompt past the transport cap; a retry revalidates and would raise there
            validate_request_transport_budget(effective_request)
        except ValueError as error:
            return _terminal_envelope(
                _unstarted_context(effective_request, paths),
                "refused",
                f"github-read preflight failed: the remaining budget makes the request too large ({error})",
                fault="network-preflight",
            )
        command = build_codex_argv(effective_request, schema_path)
    child_budget = effective_request.timeout_seconds
    before_delta = _workspace_state(effective_request.workspace) if _is_write_verb(effective_request.verb) else []
    child_cutoff = child_budget * CHILD_TIMEOUT_MULTIPLIER
    started = time.monotonic()
    if _job_path is None:
        outcome = _run_child(command, effective_request.workspace, child_cutoff)
    else:
        if _job_cancel_requested(_job_path, paths=paths):
            return _terminal_envelope(_unstarted_context(effective_request, paths), "blocked", "cancelled by job owner")
        outcome = _run_child(command, effective_request.workspace, child_cutoff, _job_path)
    duration = time.monotonic() - started
    transcript = _write_transcript(paths, outcome.stdout, outcome.stderr)
    event_info = _parse_output(outcome.stdout, host)
    observed = EnvelopeContext(
        effective_request,
        paths,
        transcript,
        event_info.tokens,
        event_info.session_id,
        duration,
        _recovery or substitution,
    )
    if outcome.output_limited:
        return _terminal_envelope(
            observed,
            "blocked",
            outcome.error or "child output exceeded the Bridge capture limit",
            fault="output-limit",
            workspace_delta=(
                _workspace_delta(before_delta, effective_request.workspace)
                if _is_write_verb(effective_request.verb)
                else None
            ),
            prior_incident=_prior_incident,
        )
    if outcome.timed_out:
        retry = _lower_effort_retry(effective_request, child_budget)
        if _attempt == 0 and effective_request.verb in {"advise", "review"} and retry is not None:
            recovery = {"requested": effective_request.effort, "applied": retry.effort, "reason": "timeout retry"}
            incident = _write_incident(
                paths, effective_request, "timeout", "hard cutoff reached; retrying once", transcript, None
            )
            return run_request(
                retry, host=host, _attempt=1, _recovery=recovery, _prior_incident=incident, _job_path=_job_path
            )
        return _terminal_envelope(
            observed,
            "timeout",
            "hard cutoff reached",
            workspace_delta=(
                _workspace_delta(before_delta, effective_request.workspace)
                if _is_write_verb(effective_request.verb)
                else None
            ),
            prior_incident=_prior_incident,
        )
    if outcome.error:
        return _terminal_envelope(
            observed,
            "blocked",
            outcome.error,
            workspace_delta=(
                _workspace_delta(before_delta, effective_request.workspace)
                if _is_write_verb(effective_request.verb)
                else None
            ),
            prior_incident=_prior_incident,
        )
    if outcome.returncode != 0:
        reason = event_info.error or f"child exited with code {outcome.returncode}"
        retry = _lower_effort_retry(effective_request, child_budget)
        # The stale-capability retry stays read-only: a failed write-capable
        # child may already have landed edits, so rerunning it can duplicate
        # or conflict with work that reached the worktree.
        if _attempt == 0 and not _is_write_verb(effective_request.verb) and _is_effort_failure(reason) and retry:
            recovery = {
                "requested": effective_request.effort,
                "applied": retry.effort,
                "reason": "structured unsupported effort",
            }
            incident = _write_incident(paths, effective_request, "unsupported-effort", reason, transcript, None)
            return run_request(
                retry, host=host, _attempt=1, _recovery=recovery, _prior_incident=incident, _job_path=_job_path
            )
        return _terminal_envelope(observed, "blocked", reason, prior_incident=_prior_incident)
    if event_info.core is None and event_info.error:
        # A zero exit can still carry a structured provider failure; surface
        # that cause instead of a generic invalid-model-result message.
        return _terminal_envelope(observed, "blocked", event_info.error, prior_incident=_prior_incident)
    try:
        core = _public_core(validate_model_core(event_info.core))
    except ValueError as error:
        return _terminal_envelope(observed, "blocked", f"invalid model result: {error}", prior_incident=_prior_incident)
    # Only the write-capable verb may report a resumable session; the others drop it from the envelope.
    reportable_session = observed if _is_write_verb(effective_request.verb) else replace(observed, session_id=None)
    envelope = _make_envelope(reportable_session, core, event_info.cost, _prior_incident)
    _record_optional_health(paths, envelope, observed.request.network)
    return envelope


def _lower_effort(current: str, supported_efforts: tuple[str, ...]) -> str | None:
    """Return one bounded lower effort level for a timeout or unsupported-effort retry.

    A retry keeps the original soft budget, so the only recovery with a real chance of finishing inside that budget is a
    cheaper attempt; escalating effort after a timeout would spend longer thinking against the same clock.
    """
    allowed = tuple(level for level in EFFORTS if not supported_efforts or level in supported_efforts)
    try:
        index = allowed.index(current)
    except ValueError:
        return None
    return allowed[index - 1] if index else None


def _is_effort_failure(reason: str) -> bool:
    """Recognize only structured effort failures eligible for one recovery attempt."""
    lowered = reason.lower()
    return "reasoning.effort" in lowered or "unsupported_value" in lowered or "invalid_enum_value" in lowered


@dataclass(frozen=True)
class ChildOutcome:
    """Capture child completion data without exposing a process object."""

    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    error: str | None
    output_limited: bool = False


@dataclass(frozen=True)
class ParsedOutput:
    """Contain model core, observed usage, session id, and structured error."""

    core: Any
    tokens: dict[str, float]
    cost: float | None
    session_id: str | None
    error: str | None


@dataclass
class _ChildOutputBuffer:
    """Retain bounded child output while compacting completed Codex command events."""

    stdout_parts: list[bytes] = dataclass_field(default_factory=list)
    stderr_parts: list[bytes] = dataclass_field(default_factory=list)
    pending_stdout: bytearray = dataclass_field(default_factory=bytearray)
    captured_bytes: int = 0
    output_limited: threading.Event = dataclass_field(default_factory=threading.Event)
    finalized: threading.Event = dataclass_field(default_factory=threading.Event)
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

    def append(self, stream: str, chunk: bytes) -> None:
        """Compact complete stdout records and enforce the retained-output budget."""
        with self.lock:
            if self.finalized.is_set():
                return
            if stream == "stderr":
                self._retain(stream, chunk)
            else:
                self.pending_stdout.extend(chunk)
                while (newline := self.pending_stdout.find(b"\n")) >= 0:
                    line = bytes(self.pending_stdout[: newline + 1])
                    del self.pending_stdout[: newline + 1]
                    # Enforce the raw per-record ceiling before a compactable
                    # command payload can disappear from retained output.
                    if len(line) > MAX_CHILD_RECORD_BYTES:
                        self.output_limited.set()
                        self._retain("stdout", line)
                        return
                    self._retain("stdout", _compact_command_event(line))
                    if self.output_limited.is_set():
                        return
            # One oversized record remains terminal; parsing it would remove
            # the per-record memory bound even when its tool output is expendable.
            if len(self.pending_stdout) > MAX_CHILD_RECORD_BYTES:
                self.output_limited.set()
            # Object fields can arrive in any order. Candidate records use the
            # independent raw-record budget until complete validation or final flush.
            candidate = self.pending_stdout.lstrip().startswith(b"{")
            if not candidate and self.captured_bytes + len(self.pending_stdout) > MAX_CHILD_OUTPUT_BYTES:
                self.output_limited.set()

    def _retain(self, stream: str, chunk: bytes) -> None:
        """Keep at most the shared byte budget and signal when a chunk exceeds it."""
        remaining = MAX_CHILD_OUTPUT_BYTES - self.captured_bytes
        accepted = chunk[: max(remaining, 0)]
        if accepted:
            target = self.stdout_parts if stream == "stdout" else self.stderr_parts
            target.append(accepted)
            self.captured_bytes += len(accepted)
        if len(chunk) > len(accepted):
            self.output_limited.set()

    def text(self) -> tuple[str, str]:
        """Flush a final unterminated stdout record and decode bounded output."""
        with self.lock:
            # An inherited pipe can outlive its cleanup grace. Freeze capture
            # before taking the snapshot so a late reader cannot alter its fault.
            self.finalized.set()
            if self.pending_stdout:
                self._retain("stdout", _compact_command_event(bytes(self.pending_stdout)))
                self.pending_stdout.clear()
            decoded = []
            remaining = MAX_CHILD_OUTPUT_BYTES
            for parts in (self.stdout_parts, self.stderr_parts):
                encoded = b"".join(parts).decode("utf-8", errors="replace").encode("utf-8")
                if len(encoded) > remaining:
                    self.output_limited.set()
                # Replacement characters can expand malformed bytes; count the
                # actual transcript encoding and never cut through a code point.
                value = encoded[:remaining].decode("utf-8", errors="ignore")
                decoded.append(value)
                remaining -= len(value.encode("utf-8"))
            return decoded[0], decoded[1]


def _compact_command_event(line: bytes) -> bytes:
    """Replace completed Codex command output with its UTF-8 size and content digest."""
    if b'"aggregated_output"' not in line:
        return line
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return line
    if not isinstance(record, dict) or record.get("type") != "item.completed":
        return line
    item = record.get("item")
    if not isinstance(item, dict) or item.get("type") != "command_execution":
        return line
    command_output = item.get("aggregated_output")
    if not isinstance(command_output, str):
        return line
    try:
        output_bytes = command_output.encode("utf-8")
    except UnicodeEncodeError:
        return line
    item = {
        **item,
        "aggregated_output_bytes": len(output_bytes),
        "aggregated_output_sha256": hashlib.sha256(output_bytes).hexdigest(),
    }
    del item["aggregated_output"]
    newline = "\n" if line.endswith(b"\n") else ""
    try:
        return (json.dumps({**record, "item": item}, separators=(",", ":")) + newline).encode("utf-8")
    except RecursionError:
        return line


def build_child_environment(
    source: Mapping[str, str] | None = None, platform: str | None = None, host: str | None = None
) -> dict[str, str]:
    """Build the child environment from an allowlist instead of inheriting the parent's.

    Only keys named in :data:`CHILD_ENV_BASE_KEYS`, the provider set for ``host``, on Windows
    :data:`CHILD_ENV_WINDOWS_KEYS`, and any the operator names in
    :data:`CHILD_ENV_EXTRA_VARIABLE` survive. Keys absent from ``source`` are omitted rather than
    set empty, so the child cannot tell a dropped variable from an unset one.

    Args:
        source: Environment to filter. Defaults to :data:`os.environ`.
        platform: Platform name to decide the Windows key set. Defaults to :data:`sys.platform`.
        host: Child host, ``"codex"`` or ``"claude"``, selecting :data:`CHILD_ENV_CODEX_KEYS` or
            :data:`CHILD_ENV_CLAUDE_KEYS`. Any other value, or ``None``, keeps both sets
            (:data:`CHILD_ENV_PROVIDER_KEYS`).

    Returns:
        Filtered environment mapping, without the bridge depth marker (the caller adds it).

    Examples:
        >>> build_child_environment({"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "s"}, platform="linux")
        {'PATH': '/bin'}
        >>> build_child_environment({"OPENAI_API_KEY": "k"}, platform="linux")
        {'OPENAI_API_KEY': 'k'}
        >>> build_child_environment({"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "a"}, platform="linux", host="codex")
        {'OPENAI_API_KEY': 'k'}
        >>> build_child_environment({"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "a"}, platform="linux", host="claude")
        {'ANTHROPIC_API_KEY': 'a'}
        >>> sorted(build_child_environment({"COMSPEC": "c", "PATH": "p"}, platform="win32"))
        ['COMSPEC', 'PATH']
        >>> sorted(build_child_environment({"COMSPEC": "c", "PATH": "p"}, platform="linux"))
        ['PATH']
        >>> extra = {"BRIDGE_CHILD_ENV_EXTRA": "HTTPS_PROXY, NO_PROXY", "HTTPS_PROXY": "p", "OTHER": "x"}
        >>> sorted(build_child_environment(extra, platform="linux"))
        ['HTTPS_PROXY']
    """
    env = os.environ if source is None else source
    active = sys.platform if platform is None else platform
    allowed = [*CHILD_ENV_BASE_KEYS, *_CHILD_ENV_KEYS_BY_HOST.get(host or "", CHILD_ENV_PROVIDER_KEYS)]
    if active == "win32":
        allowed.extend(CHILD_ENV_WINDOWS_KEYS)
    allowed.extend(_extra_child_environment_keys(env))
    return {key: env[key] for key in allowed if key in env}


def _extra_child_environment_keys(env: Mapping[str, str]) -> list[str]:
    """Parse the operator's additional-variable list into names.

    Malformed entries are skipped rather than raising: the list is host configuration read on every
    call, and one typo must not take the bridge down. The marker variable itself is never forwarded,
    so the child cannot read or re-widen the policy that produced its own environment.

    Args:
        env: Environment to read :data:`CHILD_ENV_EXTRA_VARIABLE` from.

    Returns:
        Valid variable names named by the operator, in the order given.

    Examples:
        >>> _extra_child_environment_keys({"BRIDGE_CHILD_ENV_EXTRA": "HTTPS_PROXY,NO_PROXY"})
        ['HTTPS_PROXY', 'NO_PROXY']
        >>> _extra_child_environment_keys({"BRIDGE_CHILD_ENV_EXTRA": " AWS_PROFILE , bad name ,"})
        ['AWS_PROFILE']
        >>> _extra_child_environment_keys({"BRIDGE_CHILD_ENV_EXTRA": "BRIDGE_CHILD_ENV_EXTRA"})
        []
        >>> _extra_child_environment_keys({})
        []
    """
    raw = env.get(CHILD_ENV_EXTRA_VARIABLE, "")
    names = (name.strip() for name in raw.split(","))
    return [name for name in names if name != CHILD_ENV_EXTRA_VARIABLE and _ENVIRONMENT_NAME.match(name)]


def _spawn_child(command: list[str], workspace: Path, timeout: float) -> subprocess.Popen[bytes] | ChildOutcome:
    """Start one child in its own process group, or return the outcome that replaces supervising it.

    A refused spawn and an adapter that supplies no pipes both finish the call before supervision can begin, so each
    returns a complete :class:`ChildOutcome` instead of a process for the caller to wait on.
    """
    # command[0] is the bare host name here; _resolved_command runs after this point. ntpath splits on both separators
    # and, unlike Path, never picks WindowsPath from os.name (simulated-Windows tests on Python < 3.12 would crash).
    host = os.path.splitext(ntpath.basename(command[0]))[0].lower() if command else None
    environment = build_child_environment(host=host)
    environment[DEPTH_ENVIRONMENT_VARIABLE] = str(_next_bridge_depth())
    kwargs: dict[str, Any] = {
        "cwd": workspace,
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    # Every peer owns a separate group so its supervisor can terminate the
    # complete peer tree without signalling the persistent supervisor itself.
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(_resolved_command(command), **kwargs)  # noqa: S603 - fixed executables, explicit argv.
    except OSError as error:
        return ChildOutcome("", "", None, False, str(error))
    if getattr(process, "stdout", None) is None or getattr(process, "stderr", None) is None:
        # Test doubles and unusual subprocess adapters can omit pipe objects;
        # production Popen always supplies them because both streams are PIPE.
        stdout, stderr = process.communicate(timeout=timeout)
        return ChildOutcome(stdout, stderr, process.returncode, False, None)
    return process


def _wait_for_child(
    process: subprocess.Popen[bytes],
    output: _ChildOutputBuffer,
    readers: list[threading.Thread],
    deadline: float,
    job_path: Path | None,
    paths: BridgePaths | None = None,
) -> tuple[bool, str | None, bool, bool]:
    """Supervise one running child until capture overflow, cancellation, the hard cutoff, or its own exit.

    Returns whether the cutoff was reached, the cooperative-cancellation reason when there is one, whether both output
    readers drained inside the cleanup grace, and whether this call already terminated the child's process group. The
    last flag lets the caller skip a second group signal: once the group is gone and its leader reaped, its id can be
    recycled, and a repeated signal could reach an unrelated process group.
    """
    while True:
        if output.output_limited.is_set():
            _terminate_process_group(process)
            return False, None, _finish_output_readers(readers), True
        if job_path is not None and _job_cancel_requested(job_path, paths=paths):
            _terminate_process_group(process)
            return False, "cancelled by job owner", _finish_output_readers(readers), True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            return True, None, _finish_output_readers(readers), True
        if process.poll() is not None:
            drained = _finish_output_readers(readers)
            if not drained:
                # A completed leader is not enough to close inherited pipes:
                # terminate the remaining group before returning its bounded output.
                _terminate_process_group(process)
                _finish_output_readers(readers)
            return False, None, drained, not drained
        time.sleep(min(0.05, remaining))


def _run_child(command: list[str], workspace: Path, timeout: float, job_path: Path | None = None) -> ChildOutcome:
    """Run and supervise an owned child, cleaning up its group/readers before propagating failures."""
    spawned = _spawn_child(command, workspace, timeout)
    if isinstance(spawned, ChildOutcome):
        return spawned
    process = spawned
    readers: list[threading.Thread] = []
    try:
        output = _ChildOutputBuffer()
        _start_output_readers(process, output, readers)
        timed_out, error, drained, terminated = _wait_for_child(
            process, output, readers, time.monotonic() + timeout, job_path, BridgePaths(workspace) if job_path else None
        )
        # Final flush can reveal that an incomplete record was not compactable.
        # Overflow wins on every exit path so timeout recovery cannot replay it.
        stdout, stderr = output.text()
        if output.output_limited.is_set():
            # Never signal a group twice: once it is gone and its leader reaped, its id can be recycled.
            if not terminated:
                _terminate_process_group(process)
            return ChildOutcome(
                stdout,
                stderr,
                process.returncode,
                False,
                f"child output exceeded {MAX_CHILD_OUTPUT_BYTES} byte capture limit",
                True,
            )
        if not drained:
            return ChildOutcome(
                stdout,
                stderr,
                process.returncode,
                False,
                "child output drain exceeded the Bridge cleanup grace",
            )
        return ChildOutcome(stdout, stderr, process.returncode, timed_out, error)
    except BaseException:
        # Once launched, a rejected boundary or interrupted setup/wait must not
        # leave the owned peer running. Retain partial reader startup for draining.
        try:
            _terminate_process_group(process)
        finally:
            _finish_output_readers(readers)
        raise


def _start_output_readers(
    process: subprocess.Popen[bytes], output: _ChildOutputBuffer, readers: list[threading.Thread]
) -> None:
    """Start pipe readers, recording each started thread for caller-owned exception cleanup."""
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is None:
            raise RuntimeError("stream must not be None")
        reader = threading.Thread(target=_read_child_stream, args=(name, stream, output), daemon=True)
        reader.start()
        readers.append(reader)


def _read_child_stream(stream_name: str, stream: Any, output: _ChildOutputBuffer) -> None:
    """Feed one child stream in bounded chunks until EOF or an output limit."""
    while not output.output_limited.is_set() and not output.finalized.is_set():
        chunk = stream.read(8 * 1024)
        if not chunk:
            return
        output.append(stream_name, chunk)


def _finish_output_readers(readers: list[threading.Thread]) -> bool:
    """Join output readers after child exit without letting inherited pipes extend the supervisor lifetime."""
    deadline = time.monotonic() + 2.0
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    # Closing a BufferedReader while another thread is blocked in read() can
    # wait on its internal lock forever. Callers terminate the owning group
    # instead, then make one further bounded join attempt.
    return not any(reader.is_alive() for reader in readers)


def _resolve_executable(executable: str) -> str | None:
    """Find an absolute executable using PATH only, without implicit Windows cwd search.

    Empty and dot PATH entries are ignored. Windows applies PATHEXT to bare names, including npm batch shims; explicit
    paths remain supported without falling back to a different executable.
    """
    windows = sys.platform == "win32" or os.name == "nt"
    path_module = ntpath if windows else os.path
    if path_module.dirname(executable):
        candidates = [os.path.abspath(executable)]
    else:
        names = [executable]
        if windows:
            extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
            extensions = [extension for extension in extensions if extension]
            if not any(executable.lower().endswith(extension.lower()) for extension in extensions):
                names = [executable + extension for extension in extensions]
        candidates = []
        for entry in os.environ.get("PATH", os.defpath).split(";" if windows else os.pathsep):
            directory = entry.strip('"') if windows else entry
            if not directory or path_module.normpath(directory) == ".":
                continue
            candidates.extend(os.path.abspath(os.path.join(directory, name)) for name in names)
    for candidate in candidates:
        if os.path.isfile(candidate) and (windows or os.access(candidate, os.X_OK)):
            return candidate
    return None


def _resolved_command(command: list[str]) -> list[str]:
    """Resolve argv to an absolute executable or raise the usual missing-file error."""
    resolved = _resolve_executable(command[0])
    if resolved is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), command[0])
    return [resolved, *command[1:]]


def _drain_terminated_child(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect buffered output after a group kill without blocking forever.

    A surviving grandchild can hold the inherited pipe write ends open past the group kill; an unbounded
    ``communicate()`` would then outlive every documented cutoff, so the drain itself is bounded.
    """
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    try:
        return process.communicate(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        return "", ""


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate the child tree and collect its leader's exit status within a bounded wait."""
    try:
        if os.name == "nt":
            if not _terminate_windows_process_tree(process.pid):
                process.kill()
            return
        kill_process_group = getattr(os, "killpg", None)
        if kill_process_group is None:
            process.kill()
            return
        if process.returncode is not None and _process_group_gone(process.pid):
            # A reaped leader with no group left: its id may already be recycled, so nothing here is ours to signal.
            return
        try:
            kill_process_group(process.pid, signal.SIGTERM)
        except OSError:
            process.kill()
            return
        if _wait_for_cleanup_grace(process):
            # The group is empty and its leader reaped, so its id may already be
            # recycled: a second signal could reach an unrelated process group.
            return
        try:
            # The leader can exit during the grace period while descendants retain
            # inherited pipes, so leader completion is never proof that its group ended.
            kill_process_group(process.pid, signal.SIGKILL)
        except OSError:
            pass
    finally:
        # External tree kills and Popen.kill() do not refresh Popen.returncode.
        # Reap every termination path without letting failed cleanup hang a request.
        try:
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            print("Bridge child exit status unavailable after bounded cleanup wait", file=sys.stderr)


def _wait_for_cleanup_grace(process: subprocess.Popen[str]) -> bool:
    """Allow cooperative child-tree exit for a bounded interval before force termination.

    Returns ``True`` as soon as no process remains in the child's group, so a tree that exits promptly does not pay the
    whole grace, and ``False`` when the grace expires with members left. The leader's own exit is not enough:
    descendants can outlive it while holding inherited pipes.
    """
    deadline = time.monotonic() + CLEANUP_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            # Also reaps the leader: an unreaped zombie still counts as a live group member.
            process.wait(timeout=min(CLEANUP_POLL_SECONDS, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            pass
        if _process_group_gone(process.pid):
            return True
        time.sleep(CLEANUP_POLL_SECONDS)
    return False


def _process_group_gone(process_group_id: int) -> bool:
    """Report whether no process remains in a POSIX process group.

    Any signalling failure other than "no such group" (for example a member this user may not signal) and a missing
    ``os.killpg`` both count as the group still being alive, so the caller keeps waiting rather than cutting short.
    """
    kill_process_group = getattr(os, "killpg", None)
    if kill_process_group is None:
        return False
    try:
        # Signal 0 delivers nothing; it only reports whether the group still has a member.
        kill_process_group(process_group_id, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def _terminate_windows_process_tree(pid: int) -> bool:
    """Force-terminate a Windows process and descendants with the native taskkill utility.

    ``taskkill`` runs under the same bounded-wait contract as every other termination path: an unbounded call could
    stall behind a slow or wedged tree enumeration and outlive the cutoff the rest of ``_terminate_process_group``
    establishes. A timed-out or failed ``taskkill`` falls back to ``Popen.kill()`` on the immediate child, same as any
    other failure to reach the utility.
    """
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell
            _resolved_command(["taskkill", "/PID", str(pid), "/T", "/F"]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _parse_output(stdout: str, host: str) -> ParsedOutput:
    """Parse Codex JSONL or Claude print JSON into normalized observed data."""
    records = _json_records(stdout)
    if host == "claude":
        # The host decides the parse shape; inferring it from the record count
        # would silently drop cost, usage, and session data whenever a second
        # parseable line appears on stdout.
        record = _claude_record(records)
        # An is_error result never carries a model core: decoding its string
        # payload (for example "429") would displace the structured error.
        if not isinstance(record, dict):
            core = record
        elif record.get("is_error") is True:
            core = None
        else:
            core = _decode_possible_json(record.get("structured_output", record.get("result", record)))
        usage = record.get("usage", record.get("modelUsage", {})) if isinstance(record, dict) else {}
        aggregate_cost = _cost_from(record) if isinstance(record, dict) else None
        return ParsedOutput(
            core,
            _number_fields(usage),
            aggregate_cost if aggregate_cost is not None else _cost_from(usage),
            record.get("session_id") if isinstance(record, dict) else None,
            _error_from(record) if isinstance(record, dict) else None,
        )
    core: Any = None
    tokens: dict[str, float] = {}
    cost: float | None = None
    session_id: str | None = None
    error: str | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        record_type = str(record.get("type", ""))
        if record_type == "thread.started":
            session_id = _first_string(record, ("thread_id", "threadId", "id")) or session_id
        if record_type == "turn.completed":
            usage = record.get("usage", {})
            tokens.update(_number_fields(usage))
            cost = _cost_from(usage) if _cost_from(usage) is not None else cost
        if record_type in {"turn.failed", "error"}:
            error = _error_from(record) or error
        candidate = _model_candidate(record)
        if candidate is not None:
            core = candidate
    return ParsedOutput(core, tokens, cost, session_id, error)


def _claude_record(records: list[Any]) -> Any:
    """Select the Claude print-mode response document from parsed stdout records.

    Warning or log lines can precede the response; the response is the last record carrying a recognizable print-mode
    field, never a bare count guess.
    """
    if len(records) == 1:
        return records[0]
    response_fields = ("structured_output", "result", "usage", "modelUsage", "is_error")
    for record in reversed(records):
        if isinstance(record, dict) and any(field in record for field in response_fields):
            return record
    return None


def _json_records(stdout: str) -> list[Any]:
    """Load JSON lines while retaining a standalone JSON response as one record."""
    records: list[Any] = []
    for line in stdout.splitlines():
        try:
            records.append(json.loads(line))
        except (ValueError, RecursionError):
            continue
    if records:
        return records
    try:
        return [json.loads(stdout)]
    except (ValueError, RecursionError):
        return []


def _model_candidate(record: dict[str, Any]) -> Any | None:
    """Extract a possible structured final answer from one event without guessing fields."""
    item = record.get("item")
    if isinstance(item, dict):
        for key in ("text", "content"):
            candidate = _decode_possible_json(item.get(key))
            if candidate is not None:
                return candidate
    for key in ("text", "result"):
        candidate = _decode_possible_json(record.get(key))
        if candidate is not None:
            return candidate
    return None


def _decode_possible_json(value: Any) -> Any | None:
    """Decode a JSON object embedded in a text result when present."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def _number_fields(value: Any) -> dict[str, float]:
    """Keep immediate numeric usage fields as portable token counters."""
    if not isinstance(value, dict):
        return {}
    cost_fields = {"cost", "cost_usd", "total_cost_usd"}
    return {
        str(key): float(item)
        for key, item in value.items()
        if key not in cost_fields and not isinstance(item, bool) and isinstance(item, (int, float)) and item >= 0
    }


def _cost_from(value: Any) -> float | None:
    """Return a reported aggregate cost from a host record or usage object."""
    if not isinstance(value, dict):
        return None
    for key in ("cost", "cost_usd", "total_cost_usd"):
        candidate = value.get(key)
        if not isinstance(candidate, bool) and isinstance(candidate, (int, float)) and candidate >= 0:
            return float(candidate)
    return None


def _error_from(value: Any) -> str | None:
    """Render a structured host error into one stable incident message."""
    if not isinstance(value, dict):
        return None
    if value.get("is_error") is True:
        terminal_reason = value.get("terminal_reason")
        api_status = value.get("api_error_status")
        provider_message = value.get("result")
        parts = [
            str(item)
            for item in (
                terminal_reason if isinstance(terminal_reason, str) else None,
                f"HTTP {api_status}" if isinstance(api_status, int) else None,
                provider_message if isinstance(provider_message, str) else None,
            )
            if item
        ]
        rendered = ": ".join(parts)
        return rendered if rendered else "provider request failed"
    error = value.get("error", value)
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    parameter = error.get("param")
    message = error.get("message")
    parts = [str(item) for item in (code, parameter, message) if item]
    rendered = ": ".join(parts)
    return rendered if rendered else None


def _first_string(value: dict[str, Any], keys: Iterable[str]) -> str | None:
    """Return the first non-empty string found under ordered candidate keys."""
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _prompt_with_budget(request: Request) -> str:
    """Add bounded execution context and review-specific adversarial instructions."""
    preamble = (
        f"You have a soft budget of {request.timeout_seconds:g} seconds. Return a compact structured result before it expires. "
        "If unfinished, use partial with remaining work; if blocked, name blockers instead of waiting. "
        "Keep verdict, findings, files_touched, remaining, and blockers decision-critical; put verbose supporting material only "
        "in details. The harness persists details in the transcript artifact and omits them from the public envelope. "
        f"Bridge depth is {request.depth}; run id is {request.run_id}.\n\n"
    )
    if request.verb == "review":
        preamble += (
            "Perform an adversarial read-only review: identify concrete correctness, safety, and regression risks. "
            "Do not edit files or invoke write-capable tools.\n\n"
        )
    elif _is_write_verb(request.verb):
        # A write-capable peer still loads its host's project instructions and can see the checkout's approval records:
        # a local Git approval grant would make a completion commit its default, and a pending push token could be
        # spent. A bridge child gains nothing from a grant or token: they are the authority records of the calling
        # session that owns the task, which reviews the diff and owns every commit and push.
        preamble += (
            "Leave every change unstaged and uncommitted: do not run git add, git commit, git push, or any other Git "
            "write. The caller reviews the diff and commits.\n\n"
        )
    if request.network == "github-read":
        # The sandbox already limits requests to GET/HEAD/OPTIONS on api.github.com; this tells the peer, so it does not
        # waste its budget on a write that would be refused, and asks it not to put workspace content in a query string.
        preamble += (
            "GitHub access is limited to read requests to api.github.com. Never create, edit, comment on, close, merge, "
            "label or delete anything on GitHub, and send no workspace content in a request.\n\n"
        )
    return preamble + request.task


def _trusted_inherited_depth() -> int:
    """Return the transport-owned bridge depth inherited by this process."""
    raw_depth = os.environ.get(DEPTH_ENVIRONMENT_VARIABLE, "0")
    try:
        depth = int(raw_depth)
    except ValueError as error:
        raise ValueError(f"invalid {DEPTH_ENVIRONMENT_VARIABLE}") from error
    if depth < 0:
        raise ValueError(f"invalid {DEPTH_ENVIRONMENT_VARIABLE}")
    return depth


def _request_with_trusted_depth(request: Request) -> Request:
    """Prevent caller input from lowering the transport-owned recursion depth."""
    if request.depth < 0:
        raise ValueError("depth must be a non-negative integer")
    return Request(**{**request.__dict__, "depth": max(request.depth, _trusted_inherited_depth())})


def _next_bridge_depth() -> int:
    """Increment the trusted depth inherited by a peer process."""
    return _trusted_inherited_depth() + 1


def _write_transcript(paths: BridgePaths, stdout: str, stderr: str) -> str:
    """Write one bounded child transcript and return its workspace-relative path."""
    path = paths._member(paths.root / f"raw-{time.time_ns()}-{uuid.uuid4().hex[:8]}.txt")
    payload = f"stdout:\n{stdout}\n\nstderr:\n{stderr}\n".encode()
    with open(path, "xb", opener=_private_opener) as stream:
        stream.write(payload[:MAX_CHILD_TRANSCRIPT_BYTES])
    return paths.relative(path)


def _workspace_state(workspace: Path) -> list[str]:
    """Capture a porcelain snapshot without failing when Git or a repository is absent.

    The probe is bounded: a hung ``git status`` (stale ``index.lock``, stalled
    network filesystem) runs before any request budget is armed and must not
    block dispatch outside every documented cutoff.
    """
    try:
        result = subprocess.run(  # noqa: S603 - executable resolved from PATH, explicit argv.
            # A write-capable child can set core.fsmonitor in the repository config;
            # git status would run that command here, outside the child's sandbox.
            _resolved_command(["git", "-c", "core.fsmonitor=false", "status", "--porcelain"]),
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in result.stdout.splitlines() if line]


#: Workspace paths whose content can run code in a later host session or Git command: Git hooks and repository config,
#: attribute-driven filters, Claude project settings and hooks, the project MCP server list, and Codex project config.
#: A Claude peer in acceptEdits print mode cannot write most of them, and Codex keeps the root ``.git`` and ``.codex``
#: read-only, but ``.claude`` and ``.mcp.json`` stay writable for a Codex peer, so every write run is checked.
PROTECTED_WORKSPACE_PATHS = (".git/hooks", ".git/config", ".gitattributes", ".claude", ".mcp.json", ".codex")
#: Subtrees skipped inside a protected directory: Claude-created Git worktrees are full checkouts, not configuration.
_PROTECTED_SKIP_DIRECTORIES = frozenset({"worktrees"})
#: Most entries fingerprinted per run, and most bytes hashed; past either, entries keep a size-and-mtime signature.
_PROTECTED_MAX_ENTRIES = 4000
_PROTECTED_MAX_HASH_BYTES = 8 * 1024 * 1024
#: Largest single file hashed; a larger one keeps a size-and-mtime signature.
_PROTECTED_MAX_FILE_HASH_BYTES = 256 * 1024


def _protected_state(workspace: Path) -> dict[str, str]:
    """Fingerprint every entry under :data:`PROTECTED_WORKSPACE_PATHS` without following links.

    Keys are workspace-relative POSIX paths. Small files are keyed by content digest, so a restored modification time
    does not hide an edit; links by their target; directories by presence. The walk is bounded by
    :data:`_PROTECTED_MAX_ENTRIES` and the hash budget, and never raises: an unreadable entry gets a fixed marker.
    """
    state: dict[str, str] = {}
    budget = [_PROTECTED_MAX_HASH_BYTES]
    try:
        root = Path(workspace).resolve()
    except (OSError, RuntimeError):
        return state
    for relative in PROTECTED_WORKSPACE_PATHS:
        for entry in _walk_protected(root / relative):
            if len(state) >= _PROTECTED_MAX_ENTRIES:
                return state
            state[entry.relative_to(root).as_posix()] = _protected_fingerprint(entry, budget)
    return state


def _walk_protected(path: Path) -> Iterable[Path]:
    """Yield ``path`` and, for a real directory, its descendants in sorted order, never entering a linked directory."""
    try:
        metadata = path.lstat()
    except OSError:
        return
    yield path
    if not stat.S_ISDIR(metadata.st_mode) or _is_link_or_reparse_point(metadata):
        return
    for directory, subdirectories, files in os.walk(path):
        subdirectories[:] = sorted(name for name in subdirectories if name not in _PROTECTED_SKIP_DIRECTORIES)
        for name in (*subdirectories, *sorted(files)):
            yield Path(directory, name)


def _protected_fingerprint(path: Path, budget: list[int]) -> str:
    """Return a change-revealing signature of one protected entry, spending the shared hash ``budget``."""
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return f"link:{os.readlink(path)}"
        if stat.S_ISDIR(metadata.st_mode):
            return "dir"
        if not stat.S_ISREG(metadata.st_mode):
            return f"special:{stat.S_IFMT(metadata.st_mode)}"
        if metadata.st_size > _PROTECTED_MAX_FILE_HASH_BYTES or metadata.st_size > budget[0]:
            return f"stat:{metadata.st_size}:{metadata.st_mtime_ns}:{metadata.st_mode}"
        budget[0] -= metadata.st_size
        with open(path, "rb", opener=_nonblocking_opener) as stream:
            digest = hashlib.sha256(stream.read(_PROTECTED_MAX_FILE_HASH_BYTES + 1)).hexdigest()
        return f"file:{metadata.st_mode}:{digest}"
    except OSError:
        return "unreadable"


def _protected_changes(before: dict[str, str], workspace: Path) -> list[str]:
    """Return the protected entries added, removed, or changed since ``before``, sorted."""
    after = _protected_state(workspace)
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def _workspace_delta(before: list[str], workspace: Path) -> list[str]:
    """Return changed porcelain lines between spawn and a killed implementation."""
    before_set = set(before)
    return [line for line in _workspace_state(workspace) if line not in before_set]


def _make_envelope(
    context: EnvelopeContext, core: dict[str, Any], cost: float | None, incident: str | None
) -> dict[str, Any]:
    """Merge validated core and observed values into the sole public result shape."""
    request = context.request
    envelope = {
        **core,
        "model": request.model or "host-default",
        "effort": request.effort,
        "effort_substituted": context.substitution,
        "cost": cost,
        "tokens": context.tokens,
        "duration_seconds": context.duration,
        "depth": request.depth,
        "run_id": request.run_id,
        "incident": incident,
        "session_id": context.session_id,
        "transcript_path": context.transcript_path,
        "verb": request.verb,
        "direction": request.direction,
    }
    return validate_envelope(envelope)


def _terminal_envelope(
    context: EnvelopeContext,
    status: str,
    reason: str,
    *,
    workspace_delta: list[str] | None = None,
    prior_incident: str | None = None,
    fault: str | None = None,
) -> dict[str, Any]:
    """Create, record, and return a harness-owned terminal result."""
    paths = context.paths
    if context.transcript_path is None:
        context = replace(context, transcript_path=_write_transcript(paths, "", reason))
    transcript_path = str(context.transcript_path)
    core = {
        "status": status,
        "verdict": reason,
        "findings": [],
        "files_touched": [],
        "remaining": [],
        "blockers": [reason],
    }
    incident = _write_incident(
        paths, context.request, fault or status, reason, transcript_path, workspace_delta, prior_incident
    )
    envelope = _make_envelope(context, core, None, incident)
    _record_optional_health(paths, envelope, context.request.network)
    return envelope


def _write_incident(
    paths: BridgePaths,
    request: Request,
    fault: str,
    reason: str,
    transcript_path: str,
    workspace_delta: list[str] | None,
    prior_incident: str | None = None,
) -> str:
    """Persist one compact incident record without copying child environment data."""
    path = paths.incidents / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}-{fault}.json"
    payload = {
        "fault": fault,
        "reason": reason,
        "model": request.model or "host-default",
        "effort": request.effort,
        "verb": request.verb,
        "network": request.network,
        "duration_budget_seconds": request.timeout_seconds,
        "transcript_path": transcript_path,
        "workspace_delta": workspace_delta or [],
        "prior_incident": prior_incident,
    }
    _write_json(path, payload, paths=paths)
    return paths.relative(path)


def _record_optional_health(paths: BridgePaths, envelope: dict[str, Any], network: str = "none") -> None:
    """Preserve primary delivery and report safe optional-telemetry diagnostics to the caller."""
    try:
        _append_health(paths, envelope, network)
    except (ArtifactBoundaryError, OSError):
        # Telemetry failure cannot erase completed work or obscure a primary failure.
        # Keep the message fixed because OS errors can include sensitive path content.
        envelope["findings"].append("Nonblocking health telemetry warning: health record could not be written safely.")


def _append_health(paths: BridgePaths, envelope: dict[str, Any], network: str = "none") -> None:
    """Append one cross-hop accounting record for every completed bridge call, including its egress posture."""
    payload = {
        key: envelope[key]
        for key in (
            "run_id",
            "verb",
            "direction",
            "model",
            "effort",
            "cost",
            "tokens",
            "duration_seconds",
            "status",
            "depth",
        )
    }
    payload["network"] = network
    payload["ts"] = time.time()
    path = paths._member(paths.root / "health.jsonl", check_leaf=False)
    with _open_regular_append(path) as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")


def _open_regular_append(path: Path) -> Any:
    """Open a predictable health file only when its opened inode has one link."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise ArtifactBoundaryError("bridge health artifact could not be inspected") from error
    if metadata is not None and (_is_link_or_reparse_point(metadata) or not stat.S_ISREG(metadata.st_mode)):
        raise ArtifactBoundaryError("bridge health artifact must be a regular file")
    if os.name == "nt":
        descriptor = _open_windows_nofollow_descriptor(path)
    else:
        # A FIFO swapped in after lstat must not wait for a reader before descriptor validation.
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise ArtifactBoundaryError("bridge health artifact could not be opened safely") from error
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode) or opened_metadata.st_nlink != 1:
            raise ArtifactBoundaryError("bridge health artifact must be a regular file")
        return os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
    except BaseException:
        os.close(descriptor)
        raise


def _is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    """Identify linked artifact members before using their predictable filename."""
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse_attribute)


def _open_windows_nofollow_descriptor(path: Path) -> int:
    """Open one Windows health member without dereferencing a reparse point."""
    import ctypes
    import msvcrt

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        4,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ArtifactBoundaryError("bridge health artifact could not be opened safely")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND)
    except OSError as error:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise ArtifactBoundaryError("bridge health artifact could not be opened safely") from error
    return descriptor


def start_background(request: Request) -> dict[str, Any]:
    """Start a detached supervisor and return its persistent job record."""
    validate_request_transport_budget(request)
    paths = BridgePaths(request.workspace.resolve())
    paths.prepare()
    job_id = str(uuid.uuid4())
    record_path = _job_record_path(request.workspace, job_id)
    record = {
        "job_id": job_id,
        "status": "queued",
        "request": _request_json(request),
        "pid": None,
        "result": None,
        "started_ts": time.time(),
    }
    _write_json(record_path, record, paths=paths)
    # No --task: other local users can read a process's argv, so the supervisor
    # loads the task from the job record written above.
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        request.verb,
        "--effort",
        request.effort,
        "--timeout-seconds",
        str(request.timeout_seconds),
        "--depth",
        str(request.depth),
        "--run-id",
        request.run_id,
        "--workspace",
        str(request.workspace),
        "--job-id",
        job_id,
        "--supervisor",
    ]
    if request.model:
        command.extend(["--model", request.model])
    if request.session_id:
        command.extend(["--session-id", request.session_id])
    if request.network != "none":
        command.extend(["--network", request.network])
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)  # noqa: S603 - invokes this installed script with explicit arguments.
    # The spawned supervisor records its own PID and running state; a launcher
    # write here could race a fast-failing supervisor and clobber its final
    # record with a stale queued-derived copy. The returned status matches the
    # record the caller will observe on its first poll.
    return {"job_id": job_id, "status": "queued"}


def job_status(workspace: Path, job_id: str) -> dict[str, Any]:
    """Return lifecycle state without exposing task text or the completed result."""
    record = _read_job(workspace, job_id)
    if record is None:
        return {"job_id": job_id, "status": "missing"}
    observed = _observed_status(record)
    if observed in {"stalled", "failed"}:
        # A dead supervisor is terminal regardless of any cancellation marker:
        # nothing remains alive to consume the marker, so reporting
        # cancel_requested would hide the signal that ends polling. A failed
        # supervisor exited after writing its own terminal record.
        status = observed
    elif _job_cancel_requested(_job_record_path(workspace, job_id), paths=BridgePaths(workspace)):
        status = "cancelled" if record.get("result") is not None else "cancel_requested"
    else:
        status = observed
    return {
        "job_id": job_id,
        "status": status,
        "pid": record.get("pid"),
    }


#: Seconds a job may stay queued before its observed status is downgraded to stalled.
QUEUED_STALL_SECONDS = 120.0


def _observed_status(record: dict[str, Any]) -> str:
    """Downgrade a dead ``running`` or expired ``queued`` state to ``stalled``.

    A supervisor killed without the chance to write its final record would otherwise report ``running`` forever; the
    caller needs a terminal-looking signal to stop polling and inspect the transcript instead. A ``queued`` record has
    no PID to probe yet, so it stalls on age: a healthy supervisor rewrites the record to ``running`` within moments of
    its spawn.
    """
    status = str(record["status"])
    pid = record.get("pid")
    if (
        status == "running"
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and not _supervisor_alive(pid, record.get("pid_started_ts"))
    ):
        return "stalled"
    started = record.get("started_ts")
    if (
        status == "queued"
        and isinstance(started, (int, float))
        and not isinstance(started, bool)
        and time.time() - float(started) > QUEUED_STALL_SECONDS
    ):
        return "stalled"
    return status


#: Seconds a PID's ``ps`` elapsed time may fall short of the supervisor's recorded run time and still count as the same
#: process: the record is written moments after start, ``etime`` has one-second resolution, and wall-clock adjustments
#: must not turn a live supervisor into a stalled one.
_SUPERVISOR_START_SLACK_SECONDS = 60.0


def _supervisor_alive(pid: int, started_ts: Any) -> bool:
    """Report whether a recorded supervisor still runs: its PID exists and, where checkable, is still the same process.

    A PID is recycled after its process exits, so existence alone can keep reporting a dead supervisor as running; and
    ``os.kill(pid, 0)`` fails with a permission error for a recycled PID another user owns, which the existence probe
    must read as alive. When the record carries the supervisor's own start timestamp, POSIX ``ps`` supplies the PID's
    owner and elapsed run time: a different owner (the supervisor always runs as this user), or a process younger than
    the recorded start, is another process that reused the PID. Only a positive mismatch counts as dead; an unavailable
    or unparsable ``ps`` keeps the existence verdict. Windows keeps the existence probe alone, as ``tasklist`` reports
    no start time, and records written before the timestamp existed keep it too.
    """
    if not _process_exists(pid):
        return False
    if os.name == "nt" or isinstance(started_ts, bool) or not isinstance(started_ts, (int, float)):
        return True
    identity = _posix_process_identity(pid)
    if identity is None:
        return True
    owner, elapsed = identity
    if hasattr(os, "getuid") and owner != os.getuid():
        return False
    return elapsed + _SUPERVISOR_START_SLACK_SECONDS >= time.time() - float(started_ts)


def _posix_process_identity(pid: int) -> tuple[int, float] | None:
    """Return a POSIX process's owner UID and elapsed run time in seconds, or ``None`` when ``ps`` cannot say."""
    try:
        probe = subprocess.run(  # noqa: S603 - argv list, no shell
            _resolved_command(["ps", "-o", "uid=", "-o", "etime=", "-p", str(pid)]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = probe.stdout.split()
    if probe.returncode != 0 or len(fields) != 2:
        return None
    try:
        return int(fields[0]), _elapsed_seconds(fields[1])
    except ValueError:
        return None


def _elapsed_seconds(value: str) -> float:
    """Convert a POSIX ``ps`` elapsed time, ``[[dd-]hh:]mm:ss``, to seconds.

    Raises:
        ValueError: When ``value`` is not in that form.

    Examples:
        >>> _elapsed_seconds("05:07"), _elapsed_seconds("01:00:00"), _elapsed_seconds("2-00:00:01")
        (307.0, 3600.0, 172801.0)
    """
    days, separator, clock = value.rpartition("-")
    parts = [int(part) for part in clock.split(":")]
    if not 2 <= len(parts) <= 3 or (separator and len(parts) != 3):
        raise ValueError(f"unrecognized elapsed time: {value!r}")
    hours, minutes, seconds = [0, *parts] if len(parts) == 2 else parts
    return float((int(days) if separator else 0) * 86400 + hours * 3600 + minutes * 60 + seconds)


def _process_exists(pid: int) -> bool:
    """Probe process liveness without Windows ``os.kill`` termination semantics."""
    if os.name == "nt":
        try:
            probe = subprocess.run(  # noqa: S603 - argv list, no shell
                _resolved_command(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return True
        except OSError:
            return True
        return probe.returncode == 0 and f'"{pid}"' in probe.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def job_result(workspace: Path, job_id: str) -> dict[str, Any]:
    """Return a completed public envelope or the job's current lifecycle state."""
    record = _read_job(workspace, job_id)
    if record is None:
        return {"job_id": job_id, "status": "missing"}
    if record.get("result") is not None:
        # The stored result crossed a disk boundary; re-validate so a tampered
        # or truncated record cannot masquerade as a validated envelope, even
        # when a late cancellation is about to overwrite the status fields.
        stored = validate_envelope(record["result"])
        if _job_cancel_requested(_job_record_path(workspace, job_id), paths=BridgePaths(workspace)):
            return _cancelled_envelope(stored)
        return stored
    observed = _observed_status(record)
    if observed not in {"stalled", "failed"} and _job_cancel_requested(
        _job_record_path(workspace, job_id), paths=BridgePaths(workspace)
    ):
        return {"job_id": job_id, "status": "cancel_requested"}
    if observed == "failed" and isinstance(record.get("error"), str):
        # The supervisor's own failure reason; without it a failed job reports no cause at all.
        return {"job_id": job_id, "status": observed, "error": record["error"]}
    return {"job_id": job_id, "status": observed}


def cancel_job(workspace: Path, job_id: str) -> dict[str, Any]:
    """Cooperatively request supervisor-owned cancellation without signalling a stored PID.

    The return is the same compact projection as ``job_status``: the record also holds the task text and any completed
    result, which lifecycle calls must not leak back into the caller's context.
    """
    record = _read_job(workspace, job_id)
    if record is None:
        return {"job_id": job_id, "status": "missing"}
    observed = _observed_status(record)
    if observed not in {"queued", "running"}:
        # Includes stalled: writing a marker no live supervisor can consume
        # would replace the terminal signal with cancel_requested forever.
        return {"job_id": job_id, "status": observed, "pid": record.get("pid")}
    _write_json(
        _job_cancel_path(workspace, job_id), {"job_id": _canonical_job_id(job_id)}, paths=BridgePaths(workspace)
    )
    return {"job_id": job_id, "status": "cancel_requested", "pid": record.get("pid")}


def _read_job(workspace: Path, job_id: str) -> dict[str, Any] | None:
    """Load an identity-matched job record through its contained canonical path."""
    canonical_id = _canonical_job_id(job_id)
    path = _job_record_path(workspace, canonical_id)
    if not path.is_file():
        return None
    try:
        value = _read_artifact_json(path)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("job_id") != canonical_id:
        raise ValueError("job record identity does not match its requested identifier")
    if not isinstance(value.get("status"), str):
        raise ValueError("job record has no valid lifecycle status")
    return value


def _canonical_job_id(job_id: str) -> str:
    """Require the lower-case UUID form generated by the bridge supervisor."""
    if not isinstance(job_id, str):
        raise ValueError("job_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(job_id)
    except ValueError as error:
        raise ValueError("job_id must be a canonical UUID") from error
    if str(parsed) != job_id:
        raise ValueError("job_id must be a canonical UUID")
    return job_id


def _job_record_path(workspace: Path, job_id: str) -> Path:
    """Return a lexical record path checked against the authoritative workspace artifact store."""
    canonical_id = _canonical_job_id(job_id)
    paths = BridgePaths(workspace.resolve())
    return paths._member(paths.jobs / f"{canonical_id}.json")


def _job_cancel_path(workspace: Path, job_id: str) -> Path:
    """Return the checked cooperative-cancellation member for one canonical job."""
    canonical_id = _canonical_job_id(job_id)
    paths = BridgePaths(workspace.resolve())
    return paths._member(paths.jobs / f"{canonical_id}.cancel.json")


def _job_cancel_requested(job_path: Path, *, paths: BridgePaths | None = None) -> bool:
    """Read a dedicated cancellation marker that record writes cannot overwrite.

    An unreadable, malformed, or identity-mismatched marker is treated as no
    cancellation instead of raising: this poll runs while a live child is
    supervised, and an exception here would kill the supervisor and orphan the
    write-capable child. A real cancellation rewrites the marker atomically.
    """
    cancel_path = job_path.with_name(f"{job_path.stem}.cancel.json")
    try:
        if paths is not None:
            paths._member(cancel_path)
        value = _read_artifact_json(cancel_path)
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("job_id") == job_path.stem


def _read_artifact_json(path: Path) -> Any:
    """Read one bounded JSON job artifact without following a link or waiting on a FIFO swapped in at its name.

    ``BridgePaths._member`` inspects the leaf with ``lstat`` first, but the name can be replaced before the read opens
    it. ``O_NOFOLLOW`` refuses a symlink at open time where the platform has the flag, ``O_NONBLOCK`` keeps a FIFO from
    blocking the poll loop, and the opened descriptor must be a regular single-link file. The read stops one byte past
    :data:`MAX_JOB_RECORD_BYTES`, so a planted huge file cannot exhaust memory.

    Raises:
        FileNotFoundError: When the artifact is absent.
        OSError: When the artifact cannot be opened safely, for example a symlink refused by ``O_NOFOLLOW``.
        ValueError: When the opened file is not a regular single-link file, exceeds the bound, or is not UTF-8 JSON.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("bridge job artifact must be a regular single-link file")
        data = stream.read(MAX_JOB_RECORD_BYTES + 1)
    if len(data) > MAX_JOB_RECORD_BYTES:
        raise ValueError("bridge job artifact exceeds its size bound")
    return json.loads(data.decode("utf-8"))


def _cancelled_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Turn a late cooperative cancellation marker into the authoritative result.

    Landed work must stay visible: ``findings``, ``files_touched``, and
    ``remaining`` from the completed child survive so a cancellation can never
    hide edits that already reached the worktree.
    """
    cancelled = {
        **envelope,
        "status": "blocked",
        "verdict": "cancelled by job owner",
        "blockers": ["cancelled by job owner"],
    }
    return validate_envelope(cancelled)


def _request_json(request: Request) -> dict[str, Any]:
    """Serialize a request into a job record without nonportable Path objects."""
    return {
        **request.__dict__,
        "workspace": str(request.workspace),
        "origin_workspace": str(request.origin_workspace) if request.origin_workspace else None,
        "supported_efforts": list(request.supported_efforts),
    }


#: Number of tries for the atomic file replace when a concurrent reader makes it raise PermissionError on Windows.
_REPLACE_ATTEMPTS = 40
#: Seconds to sleep between atomic file replace attempts.
_REPLACE_RETRY_SECONDS = 0.025


def _write_json(path: Path, value: dict[str, Any], *, paths: BridgePaths | None = None) -> None:
    """Atomically write stable JSON, checking bound targets and temporary members at each write seam."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    created = False
    try:
        if paths is not None:
            paths._member(path)
            paths._member(temporary)
        with open(temporary, "x", encoding="utf-8", newline="\n", opener=_private_opener) as stream:
            created = True
            stream.write(json.dumps(value, sort_keys=True) + "\n")
        _replace_with_retry(temporary, path, paths=paths)
    except (OSError, ArtifactBoundaryError):
        if not created:
            raise
        try:
            if paths is not None:
                paths._member(temporary)
            temporary.unlink(missing_ok=True)
        except ArtifactBoundaryError:
            pass  # Do not follow a replaced parent merely to clean up a failed write.
        raise


def _private_opener(path: str, flags: int) -> int:
    """Open an artifact file for :func:`open` so a newly created file is readable only by the current user."""
    return os.open(path, flags, _ARTIFACT_FILE_MODE)


def _nonblocking_opener(path: str, flags: int) -> int:
    """Open a file for :func:`open` without waiting for a FIFO writer, so a later regular-file check can refuse it."""
    return os.open(path, flags | getattr(os, "O_NONBLOCK", 0))


def _replace_with_retry(source: Path, target: Path, *, paths: BridgePaths | None = None) -> None:
    """Rename ``source`` over ``target``, retrying briefly when another process holds ``target`` open.

    On Windows a rename over a file that another process has open for reading fails with ``PermissionError`` (``WinError
    5``, a sharing violation) instead of succeeding as it does on POSIX. The supervisor polls the job record and the
    cancel marker while the caller rewrites them, so that window is hit routinely rather than rarely. The reader holds
    the file for one ``read_text`` call, so a bounded retry covers it; the last attempt re-raises.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            if paths is not None:
                paths._member(source)
                paths._member(target)
            source.replace(target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_SECONDS)


def _resolve_task(args: argparse.Namespace) -> str:
    """Return the task text, reading ``--task-file`` when the task is not inline.

    ``--task-file`` exists so a caller holding text it did not author — a pull-request comment, an issue body, a
    reviewer finding — never has to embed that text in the command line it composes. Quoting is then not a property of
    how carefully the caller escaped the text.
    """
    if args.task is not None and args.task_file is not None:
        raise ValueError("pass either --task or --task-file, not both")
    if args.task is not None:
        return args.task
    if args.task_file is None:
        if args.supervisor and args.job_id is not None:
            return _stored_job_task(Path(args.workspace).resolve(), args.job_id)
        raise ValueError("one of --task or --task-file is required")
    return _read_task_file(Path(args.task_file))


def _read_task_file(path: Path) -> str:
    """Read one bounded task file, refusing anything but a regular file.

    The read stops one byte past :data:`MAX_TASK_UTF8_BYTES`, so ``/dev/zero`` or a huge file cannot exhaust memory
    before the transport budget is checked.
    """
    try:
        with open(path, "rb", opener=_nonblocking_opener) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("--task-file must be a regular file")
            data = stream.read(MAX_TASK_UTF8_BYTES + 1)
    except OSError as error:
        raise ValueError(f"--task-file could not be read: {error}") from error
    if len(data) > MAX_TASK_UTF8_BYTES:
        raise ValueError("task exceeds Bridge's encoded transport budget")
    return data.decode("utf-8")


def _stored_job_task(workspace: Path, job_id: str) -> str:
    """Return the task text a detached supervisor's launcher stored in its job record."""
    record = _read_job(workspace, job_id)
    request = record.get("request") if record is not None else None
    task = request.get("task") if isinstance(request, dict) else None
    if not isinstance(task, str):
        raise ValueError("supervisor job record holds no task")
    return task


def _run_as_supervisor(request: Request, job_id: str) -> dict[str, Any]:
    """Run one detached job inside this process and leave its job record in a terminal state."""
    paths = BridgePaths(request.workspace)
    record_path = _job_record_path(request.workspace, job_id)
    record = _read_job(request.workspace, job_id)
    if record is None:
        raise ValueError("supervisor job record is missing")
    if not _job_cancel_requested(record_path, paths=paths):
        record["pid"] = os.getpid()
        # Lets status probes tell this process from a later one that reuses its PID (see _supervisor_alive).
        record["pid_started_ts"] = time.time()
        record["status"] = "running"
        _write_json(record_path, record, paths=paths)
    try:
        output = run_request(request, _job_path=record_path)
    except (OSError, ValueError) as error:
        # Leave a terminal record so lifecycle callers stop polling
        # a supervisor that died before writing its result.
        failed = _read_job(request.workspace, job_id)
        if failed is not None:
            failed["status"] = "failed"
            failed["error"] = str(error)
            _write_json(record_path, failed, paths=paths)
        raise
    record = _read_job(request.workspace, job_id)
    if record is None:
        raise ValueError("supervisor job record disappeared")
    if _job_cancel_requested(record_path, paths=paths):
        output = _cancelled_envelope(output)
        record["status"] = "cancelled"
    else:
        record["status"] = "finished"
    record["result"] = output
    _write_json(record_path, record, paths=paths)
    return output


def _request_from_args(args: argparse.Namespace) -> Request:
    """Build a validated public request from one command-line namespace."""
    workspace = Path(args.workspace).resolve()
    task = _resolve_task(args)
    if not task.strip():
        raise ValueError("--task must not be empty")
    if args.verb not in DEFAULT_TIMEOUTS:
        raise ValueError("unknown bridge verb")
    if args.session_id and not _is_write_verb(args.verb):
        raise ValueError("--session-id is available only for implement")
    for flag, value in (("--model", args.model), ("--session-id", args.session_id), ("--run-id", args.run_id)):
        validate_caller_identifier(flag, value)
    if args.background and not _is_write_verb(args.verb):
        raise ValueError("--background is available only for implement")
    if args.depth < 0:
        raise ValueError("--depth must be a non-negative integer")
    if args.network not in NETWORK_MODES:
        raise ValueError(f"--network must be one of: {', '.join(NETWORK_MODES)}")
    timeout = args.timeout_seconds if args.timeout_seconds is not None else DEFAULT_TIMEOUTS[args.verb]
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout-seconds must be a finite positive number")
    if args.effort is not None:
        normalize_effort(args.effort, "codex")
    request = Request(
        args.verb,
        task,
        args.model or DEFAULT_MODEL,
        args.effort or DEFAULT_EFFORT,
        float(timeout),
        args.depth,
        args.run_id or str(uuid.uuid4()),
        workspace,
        "claude_to_codex",
        args.background,
        args.session_id,
        workspace if args.session_id else None,
        network=args.network,
    )
    validate_request_transport_budget(request)
    return request


def _parser() -> argparse.ArgumentParser:
    """Create the stable public command parser for skills and lifecycle calls."""
    parser = argparse.ArgumentParser(description="Run a compact Claude/Codex bridge call.")
    commands = parser.add_subparsers(dest="command", required=True)
    for verb in DEFAULT_TIMEOUTS:
        child = commands.add_parser(verb)
        child.set_defaults(verb=verb)
        # Not argparse-mutually-exclusive: _resolve_task raises a ValueError that reaches
        # the caller inside the JSON error envelope, whereas argparse would exit 2 with a
        # bare usage string that no skill or MCP caller can parse.
        child.add_argument("--task")
        child.add_argument("--task-file", help="read the task from this file instead of the command line")
        child.add_argument("--model")
        child.add_argument("--effort")
        child.add_argument("--timeout-seconds", type=float)
        child.add_argument("--background", action="store_true")
        child.add_argument("--session-id")
        child.add_argument("--depth", type=int, default=0)
        # No argparse `choices`: a bad value is rejected in _request_from_args so it reaches the caller as JSON.
        child.add_argument(
            "--network",
            default="none",
            help="Codex egress: none (default) or github-read (GET/HEAD/OPTIONS to api.github.com only)",
        )
        child.add_argument("--run-id")
        child.add_argument("--workspace", default=".")
        child.add_argument("--job-id", help=argparse.SUPPRESS)
        child.add_argument("--supervisor", action="store_true", help=argparse.SUPPRESS)
    for command in ("status", "result", "cancel"):
        child = commands.add_parser(command)
        child.add_argument("--job-id", required=True)
        child.add_argument("--workspace", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the public bridge CLI and emit exactly one JSON object to stdout."""
    args = _parser().parse_args(argv)
    exit_code = 0
    try:
        if args.command in {"status", "result"}:
            output = (
                job_status(Path(args.workspace), args.job_id)
                if args.command == "status"
                else job_result(Path(args.workspace), args.job_id)
            )
        elif args.command == "cancel":
            output = cancel_job(Path(args.workspace), args.job_id)
        else:
            request = _request_from_args(args)
            if args.job_id is not None and not args.supervisor:
                raise ValueError("--job-id is valid only with --supervisor")
            if args.supervisor:
                if args.job_id is None:
                    raise ValueError("--supervisor requires --job-id")
                output = _run_as_supervisor(request, args.job_id)
            elif request.background:
                output = start_background(request)
            else:
                output = run_request(request)
    except (OSError, ValueError) as error:
        output = {"status": "error", "error": str(error)}
        exit_code = 2
    sys.stdout.write(json.dumps(output, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
