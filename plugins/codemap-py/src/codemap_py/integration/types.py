"""The runtimes, consumers and error types the integration boundary is defined in."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

#: Schema revision of the JSON reports emitted by the plan and audit modes.
SCHEMA_VERSION = 2


#: Plugin name of the codemap provider being integrated.
PROVIDER_NAME = "codemap-py"


#: Name of the plugin marketplace that consumers install plugins from.
MARKETPLACE_NAME = "borda-ai-rig"


#: Git URL of the marketplace repository, used when the plan sources from the remote rather than a local checkout.
MARKETPLACE_REMOTE = "https://github.com/Borda/AI-Rig.git"


#: Exit status for a successful integrate command.
_EXIT_OK = 0


#: Exit status for a runtime failure; the default for integration errors.
_EXIT_RUNTIME = 1


#: Exit status for invalid arguments or an unknown target.
_EXIT_USAGE = 2


# Finalized Phase 5 consumer target map. Each entry is an explicit, allowlisted
# source-owned integration site; no runtime discovery or installed-cache mutation occurs.
#: Per-consumer relative path of the one file in which a managed block may be written.
CONSUMER_MANAGED_FILE: dict[str, str] = {
    "foundry": "skills/_shared/codemap-context.md",
    "oss": "skills/_shared/codemap-gates.md",
    "develop": "skills/_shared/codemap-context.md",
    "research": "skills/_shared/codemap-context.md",
    "codex-rig": "shared/codemap-py-integration.md",
}


class IntegrationError(Exception):
    """Bounded, structured integration failure.

    Attributes:
        code: Stable machine-readable error slug.
        exit_code: Process exit code this error maps to.
        detail: Structured supporting fields (argv, journal path, ...); never secrets.
    """

    def __init__(self, code: str, message: str, *, exit_code: int = _EXIT_RUNTIME, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.detail = detail or {}


class RefusalError(IntegrationError):
    """A safety invariant refused a mutation before touching anything (exit ``1``)."""

    def __init__(self, code: str, message: str, *, detail: dict | None = None) -> None:
        super().__init__(code, message, exit_code=_EXIT_RUNTIME, detail=detail)


class ApprovalError(IntegrationError):
    """Do not authorize the requested mutation (exit ``2``)."""

    def __init__(self, code: str, message: str, *, detail: dict | None = None) -> None:
        super().__init__(code, message, exit_code=_EXIT_USAGE, detail=detail)


class Runtime(str, Enum):
    """Host runtime a target belongs to, plus the ``BOTH`` selector.

    Inherits ``str`` (not ``enum.StrEnum`` — ``requires-python`` is ``>=3.10``) so members serialise into plan/report
    JSON as plain strings. ``BOTH`` is a CLI selector only: it never appears on a :class:`ConsumerTarget`, and
    :func:`_runtimes_of` expands it.
    """

    CLAUDE = "claude"
    CODEX = "codex"
    BOTH = "both"


class Source(str, Enum):
    """Which marketplace source a ``sync`` refreshes from."""

    LOCAL_CANDIDATE = "local-candidate"
    RELEASE = "release"


@dataclass(frozen=True)
class ConsumerTarget:
    """One closed-set integration target.

    Attributes:
        runtime: ``Runtime.CLAUDE`` or ``Runtime.CODEX`` — never ``Runtime.BOTH``.
        consumer: Installed plugin name — must equal that plugin's own manifest ``name``.
        plugin_dir: Repo-relative directory holding the consumer's source checkout.
    """

    runtime: Runtime
    consumer: str
    plugin_dir: str


#: Consumer plugins integrated through the Claude runtime.
CLAUDE_TARGETS: tuple[ConsumerTarget, ...] = (
    ConsumerTarget(Runtime.CLAUDE, "foundry", "plugins/cc_foundry"),
    ConsumerTarget(Runtime.CLAUDE, "oss", "plugins/cc_oss"),
    ConsumerTarget(Runtime.CLAUDE, "develop", "plugins/cc_develop"),
    ConsumerTarget(Runtime.CLAUDE, "research", "plugins/cc_research"),
)


#: Consumer plugins integrated through the Codex runtime.
CODEX_TARGETS: tuple[ConsumerTarget, ...] = (ConsumerTarget(Runtime.CODEX, "codex-rig", "plugins/codex-rig"),)


#: Closed set of every known integration target across both runtimes.
ALL_TARGETS: tuple[ConsumerTarget, ...] = CLAUDE_TARGETS + CODEX_TARGETS


#: Repository-relative directory of the codemap provider plugin.
PROVIDER_DIR = "plugins/codemap-py"


def _cli_for(runtime: Runtime | str) -> str:
    """Return the native plugin-manager executable name for *runtime*.

    Accepts a plain string too: ``runtime`` is read straight off a persisted plan op in
    the apply/sync paths, where it arrives as JSON text rather than a member.
    """
    return Runtime.CLAUDE.value if runtime == Runtime.CLAUDE else Runtime.CODEX.value


def _targets_for_runtime(runtime: Runtime) -> tuple[ConsumerTarget, ...]:
    """Return the closed-set targets for one runtime selector."""
    if runtime == Runtime.CLAUDE:
        return CLAUDE_TARGETS
    if runtime == Runtime.CODEX:
        return CODEX_TARGETS
    return ALL_TARGETS


def _runtimes_of(runtime: Runtime) -> tuple[Runtime, ...]:
    """Return the concrete runtimes a selector expands to (``BOTH`` fans out, others pass through)."""
    return (Runtime.CLAUDE, Runtime.CODEX) if runtime == Runtime.BOTH else (runtime,)


def resolve_targets(runtime: Runtime | str, consumers: Sequence[str] | None) -> list[ConsumerTarget]:
    """Return the closed-set targets selected by *runtime*, filtered by optional *consumers*.

    Args:
        runtime: A :class:`Runtime` member, or its plain value from the CLI.
        consumers: Explicit consumer-name subset, or ``None`` for every target in *runtime*.

    Returns:
        Targets in *consumers* order when given, else the registry's declared order.

    Raises:
        IntegrationError: an entry in *consumers* is outside the closed set for *runtime*
            (``unknown_target``, exit ``2`` — this is never a discovery registry).

    Examples:
        >>> [t.consumer for t in resolve_targets("codex", None)]
        ['codex-rig']
        >>> [t.consumer for t in resolve_targets("claude", ["oss"])]
        ['oss']
    """
    runtime = Runtime(runtime)
    pool = _targets_for_runtime(runtime)
    if consumers is None:
        return list(pool)
    by_name = {t.consumer: t for t in pool}
    unknown = [name for name in consumers if name not in by_name]
    if unknown:
        raise IntegrationError(
            "unknown_target",
            f"not in the closed target set for runtime={runtime.value!r}: {unknown}",
            exit_code=_EXIT_USAGE,
            detail={"unknown": unknown, "runtime": runtime.value},
        )
    return [by_name[name] for name in consumers]


def _find_target(runtime: Runtime | str, consumer: str) -> ConsumerTarget:
    """Return the registered :class:`ConsumerTarget` for *runtime*/*consumer*, or refuse it."""
    for target in ALL_TARGETS:
        if target.runtime == runtime and target.consumer == consumer:
            return target
    raise IntegrationError(
        "unknown_target", f"{consumer!r} ({runtime}) is not in the closed target set", exit_code=_EXIT_USAGE
    )
