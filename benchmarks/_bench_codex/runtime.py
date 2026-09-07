"""Shared Codex stream normalization, rendering, and paid-stage lifecycle."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shlex
import sys
from typing import Any, TextIO
from uuid import uuid4

from rich.console import Console
from rich.panel import Panel

from _bench_common import presentation
from _bench_common.paid_lifecycle import paid_approval_token
from _bench_common.presentation import fmt_time, fmt_tok

_SENSITIVE_EVENT_KEYS = frozenset(
    {"access_token", "refresh_token", "id_token", "authorization", "cookie", "set-cookie"}
)
#: Legend body lines without their framing rules, so a terminal can panel them and a redirected run
#: can keep writing the plain rules its logs already carry.
STRUCTURAL_LEGEND_BODY = (
    "  treatments: A_plain=no Codemap, B_auto=direct Codemap available and optional, C_strict=Codemap Skill required",
    "  tasks:",
    "      SE: symbol extraction",
    "      FN: function-call graph",
    "      RV: review assistance",
    "      CQ: code quality",
    "      BR: blast radius",
    "      DG: debug from trace",
    "      FT: feature scaffolding",
    "      RI: real issue",
    "      DI: diff impact",
    "      GR: graph reasoning",
    "      MB: module blast radius",
    "  status: ✓ completed, ✗ failed",
    "  quality: continuous [0,1], ? unscoreable (higher is better)",
    "  progress: N completed cells / M planned cells",
    "  treatment: ✓ assigned arm followed, ✗ assigned arm not followed",
    "  codemap-used: ✓ Codemap call observed; ✗ no Codemap call (expected for A_plain) or required use missed (B/C)",
    "  query: ✓ exact expected query; ✗ mismatch; — not applicable",
    "  cohort: H headline; D diagnostic",
    "  input tokens: gross total; cached and fresh details remain in telemetry only (lower is better at equal quality)",
)
STRUCTURAL_OUTPUT_LEGEND = "\n".join(
    (presentation.LEGEND_OPEN_RULE, *STRUCTURAL_LEGEND_BODY, presentation.LEGEND_CLOSE_RULE)
)


@dataclass
class CodexParseResult:
    """Normalized Codex stream telemetry plus lossless parsed event records."""

    thread_id: str = ""
    output_text: str = ""
    last_tool_text_offset: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    command_calls: int = 0
    codemap_observed_calls: int = 0
    codemap_observed_successful_calls: int = 0
    codemap_observed_complete_calls: int = 0
    codemap_calls: int = 0
    codemap_successful_calls: int = 0
    codemap_compact_successful_calls: int = 0
    codemap_direct_calls: int = 0
    codemap_direct_successful_calls: int = 0
    codemap_direct_compact_successful_calls: int = 0
    codemap_skill_calls: int = 0
    codemap_skill_successful_calls: int = 0
    codemap_skill_compact_successful_calls: int = 0
    successful_query_arguments: list[list[str]] = field(default_factory=list)
    skill_delivery_observed: bool = False
    codemap_errors: int = 0
    fallback_calls: int = 0
    completed: bool = False
    incomplete: bool = False
    error: str = ""
    error_type: str = ""
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    malformed_lines: int = 0
    # Count of usage fields that could not be read as a token count. A nonzero
    # value means the reported cost of this turn is an undercount, not a cheap run.
    malformed_usage: int = 0
    raw_usage: dict[str, Any] = field(default_factory=dict)
    item_counts: dict[str, int] = field(default_factory=dict)
    tool_elapsed_s: float | None = None
    tool_result_tokens: int | None = None
    retryable: bool = False

    @property
    def success(self) -> bool:
        """Return whether the stream reached a successful terminal event."""
        return self.completed and not self.incomplete and not self.error


def _is_refresh_token_authentication_failure(error: str) -> bool:
    """Identify deterministic OAuth refresh failures that cannot succeed on retry."""
    normalized = error.casefold()
    return (
        "401" in normalized
        and "refresh token" in normalized
        and ("expired" in normalized or "already been used" in normalized or "already used" in normalized)
    )


def _redact_sensitive_text(value: str) -> str:
    """Remove standard credential representations from persisted provider errors."""
    value = re.sub(
        r"(?i)\b(authorization|cookie|set-cookie)\s*:\s*[^\r\n]*",
        r"\1: <redacted>",
        value,
    )
    value = re.sub(r"(?i)(bearer\s+)[^\s,;\]\}]+", r"\1<redacted>", value)
    return re.sub(
        r'(?i)(["\']?(?:access_token|refresh_token|id_token|authorization|cookie|set-cookie)["\']?\s*[:=]\s*["\'])[^"\']*',
        r"\1<redacted>",
        value,
    )


def _redact_sensitive_event(value: Any) -> Any:
    """Return a telemetry-safe projection without credential-valued fields."""
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if str(key).casefold() in _SENSITIVE_EVENT_KEYS else _redact_sensitive_event(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_event(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _redact_provider_error(value: Any) -> str:
    """Render one provider error without persisting credential values."""
    if isinstance(value, (Mapping, list)):
        return json.dumps(_redact_sensitive_event(value), sort_keys=True, default=str)
    return _redact_sensitive_text(str(value))


def _coerce_usage_int(value: Any) -> tuple[int, bool]:
    """Return one non-negative usage count plus whether the field was malformed.

    The previous ``_as_int`` returned ``0`` for every non-``int`` value, so a provider schema change that emitted
    ``"1234"`` or ``1234.0`` silently degraded a paid turn into a free-looking run with no signal anywhere. A numeric
    string or integral float is real usage and is coerced; anything that cannot represent a non-negative token count is
    reported as malformed so the caller can surface it instead of persisting a fabricated zero.

    An absent field is *not* malformed: most Codex usage events omit ``reasoning_output_tokens``, and flagging those
    would make the counter useless.
    """
    if value is None:
        return 0, False
    number: int | None = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(value) if math.isfinite(value) and value.is_integer() else None
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            number = None
    if number is None or number < 0:
        return 0, True
    return number, False


def _ingest_usage(result: CodexParseResult, usage: Mapping[str, Any]) -> None:
    """Fold one native usage event into the turn totals and count schema drift.

    ``max()`` rather than a running sum is deliberate: ``benchmarks/README.md``
    records that native Codex input usage is *cumulative within a turn* — its
    literal claim is only that cached input is a subset of gross input; the
    stronger reading that each usage event restates the turn total is this
    module's interpretation, not the README's assertion. An audit of every
    captured real stream (401 turns, 2026-08-13) found exactly one usage-bearing
    event per turn, always terminal — so ``max()``, ``sum()`` and last-wins are
    indistinguishable on real data and the cumulative property is unobservable
    there, while the subset claim held on all 401 events. The semantic stays
    pinned only by a synthetic fixture in ``tests/test_codex_runtime.py``
    (``test_usage_events_are_treated_as_cumulative_not_additive``). If a future
    CLI emits several usage events per turn, that fixture is the contract to
    revisit before changing this.
    """
    result.raw_usage.update(dict(usage))
    for attribute, value in (
        ("input_tokens", usage.get("input_tokens")),
        ("cached_input_tokens", usage.get("cached_input_tokens", usage.get("cache_read_input_tokens"))),
        ("output_tokens", usage.get("output_tokens")),
        ("reasoning_output_tokens", usage.get("reasoning_output_tokens")),
    ):
        count, malformed = _coerce_usage_int(value)
        if malformed:
            result.malformed_usage += 1
        setattr(result, attribute, max(getattr(result, attribute), count))


def _item_text(item: Mapping[str, Any]) -> str:
    """Read the first text-bearing field, joining mapping chunks without separators.

    Check ``text``, ``content``, then ``message``. Return an empty string when
    none contains a string or a list with mapping chunks.

    Examples:
        >>> _item_text({"content": [{"text": "one"}, {"text": " two"}]})
        'one two'
        >>> _item_text({})
        ''
    """
    for key in ("text", "content", "message"):
        value = item.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            chunks = [part.get("text", "") for part in value if isinstance(part, Mapping)]
            if chunks:
                return "".join(str(part) for part in chunks)
    return ""


def _command_text(item: Mapping[str, Any]) -> str:
    """Join command-related fields in fixed order, JSON-encoding non-string values.

    Missing fields contribute empty strings, so spacing is retained. This is diagnostic text assembly; it neither parses
    nor executes a command.
    """
    values = [item.get(key, "") for key in ("command", "cmd", "name", "arguments", "input")]
    return " ".join(value if isinstance(value, str) else json.dumps(value, sort_keys=True) for value in values)


def _tool_use_command(block: Mapping[str, Any]) -> str:
    """Return the shell command carried by one legacy assistant ``tool_use`` block.

    The previous code passed ``name + " " + _command_text(block)`` to the Codemap predicate, which could never match:
    the predicate requires ``$CODEMAP_BIN`` in first position, and both the prepended tool name *and*
    ``_command_text``'s own ``name`` field pushed it out of that slot, so the whole legacy attribution branch was
    unreachable-false. Stripping only the prefix is not enough — the command has to be read out of the block's ``input``
    payload directly.
    """
    payload = block.get("input")
    if isinstance(payload, Mapping):
        for key in ("command", "cmd"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    for key in ("command", "cmd"):
        value = block.get(key)
        if isinstance(value, str):
            return value
    return ""


def _unwrap_native_command(command: str) -> str | None:
    """Return a native command after at most one exact Codex zsh wrapper."""
    if not command or "\n" in command or "\r" in command:
        return None
    normalized = command.strip()
    try:
        parts = shlex.split(normalized)
    except ValueError:
        return None
    if parts[:2] != ["/bin/zsh", "-lc"]:
        return normalized
    if len(parts) != 3 or not parts[2] or "\n" in parts[2] or "\r" in parts[2]:
        return None
    return parts[2]


def _native_item_tokens(
    command: str,
    *,
    preserve_quotes: bool = False,
    allow_compound: bool = False,
) -> list[str] | None:
    """Tokenize a native command, retaining compound separators only when requested."""
    normalized = _unwrap_native_command(command)
    if normalized is None:
        return None
    try:
        lexer = shlex.shlex(normalized, posix=not preserve_quotes, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any("`" in token for token in tokens):
        return None
    if not allow_compound and any(any(character in ";&|<>()" for character in token) for token in tokens):
        return None
    return tokens


def _has_unquoted_comment(command: str) -> bool:
    """Return whether a shell comment can hide after an otherwise valid command."""
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if character == "\\" and quote != "'":
            index += 2
            continue
        if quote is not None:
            if character == quote:
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "#":
            return True
        index += 1
    return False


def validate_codex_stratum(model: str, reasoning_effort: str, manifest_path: Path) -> None:
    """Reject execution outside the model and effort declared by the active manifest."""
    try:
        configured = json.loads(Path(manifest_path).read_text(encoding="utf-8"))["model"]
        # Each declared stratum runs as its own nonpoolable study, like Claude's three tiers.
        strata = [configured["name"], *configured.get("additional_strata", [])]
        expected_effort = configured["reasoning_effort"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity model stratum is unavailable or malformed") from exc
    if not all(isinstance(name, str) for name in strata):
        raise ValueError("provider-parity model stratum list is malformed")
    if model not in strata:
        raise ValueError(f"Codex provider parity requires one of {', '.join(strata)}")
    if reasoning_effort != expected_effort:
        raise ValueError(f"Codex provider-parity reasoning effort must be {expected_effort}")


def _dedicated_compact_query(command: str) -> tuple[str, list[str]] | None:
    """Return an executable token and arguments for one standalone compact query."""
    normalized = _unwrap_native_command(command)
    if normalized is None:
        return None
    if _has_unquoted_comment(normalized):
        return None
    tokens = _native_item_tokens(command)
    quoted_tokens = _native_item_tokens(command, preserve_quotes=True)
    if (
        tokens is None
        or quoted_tokens is None
        or len(tokens) != len(quoted_tokens)
        or len(tokens) < 4
        or tokens[1:3] != ["query", "--compact"]
        or any(
            "$" in token and not (len(token) >= 2 and token.startswith("'") and token.endswith("'"))
            for token in quoted_tokens[3:]
        )
    ):
        return None
    arguments = tokens[3:]
    if (
        arguments[0] == "help"
        or arguments[0].startswith("-")
        or any(argument in {"-h", "--help"} for argument in arguments)
    ):
        return None
    return tokens[0], arguments


def _is_launcher_variable(executable: str, quoted_executable: str) -> bool:
    """Return whether an executable token expands the injected launcher variable."""
    return executable in {"$CODEMAP_BIN", "${CODEMAP_BIN}"} and quoted_executable != f"'{executable}'"


def is_absolute_launcher_path(launcher_path: str | Path | None) -> bool:
    """Return whether a serialized launcher path is absolute in a supported host syntax."""
    if launcher_path is None:
        return False
    path_text = str(launcher_path)
    return PurePosixPath(path_text).is_absolute() or PureWindowsPath(path_text).is_absolute()


def _is_verified_launcher(executable: str, launcher_path: str | Path | None) -> bool:
    """Return whether an executable token exactly matches the verified launcher."""
    return is_absolute_launcher_path(launcher_path) and executable == str(launcher_path)


def _canonical_query_arguments(
    command: str,
    *,
    launcher_path: str | Path | None = None,
    allow_equivalent_launcher: bool = False,
) -> list[str] | None:
    """Return arguments for a variable or explicitly credited verified launcher query."""
    query = _dedicated_compact_query(command)
    if query is None:
        return None
    executable, arguments = query
    quoted_tokens = _native_item_tokens(command, preserve_quotes=True)
    if quoted_tokens is None:
        return None
    if _is_launcher_variable(executable, quoted_tokens[0]):
        return arguments
    if allow_equivalent_launcher and (
        _is_verified_launcher(executable, launcher_path) or _is_verified_launcher(quoted_tokens[0], launcher_path)
    ):
        return arguments
    return None


def _records_compact_query_attempt(
    command: str,
    *,
    launcher_path: str | Path | None = None,
    allow_equivalent_launcher: bool = False,
) -> bool:
    """Return whether a native item records a compact-query attempt for C ordering."""
    return (
        _canonical_query_arguments(
            command,
            launcher_path=launcher_path,
            allow_equivalent_launcher=allow_equivalent_launcher,
        )
        is not None
    )


_SHELL_SEGMENT_BOUNDARIES = frozenset({";", "|", "&&", "||", "do", "then", "else"})
_LAUNCHER_MUTATING_BUILTINS = frozenset({"export", "readonly", "typeset", "declare", "local", "unset", "read"})


def _is_unquoted_segment_boundary(token: str, quoted_token: str) -> bool:
    """Return whether one token is shell syntax rather than quoted command data."""
    return token in _SHELL_SEGMENT_BOUNDARIES and token == quoted_token


def _mutates_launcher(tokens: Sequence[str]) -> bool:
    """Return whether a compound shell item can change the injected launcher."""
    for index, token in enumerate(tokens):
        bare = token.lstrip("'\"")
        if bare.startswith("CODEMAP_BIN=") or token in {"eval", "source", "."}:
            return True
        if token in {"for", "read", "unset"} and index + 1 < len(tokens) and tokens[index + 1] == "CODEMAP_BIN":
            return True
        if token in _LAUNCHER_MUTATING_BUILTINS and index + 1 < len(tokens):
            next_token = tokens[index + 1].lstrip("'\"")
            if next_token == "CODEMAP_BIN" or next_token.startswith("CODEMAP_BIN="):
                return True
    return False


def _observed_compact_query_arguments(command: str, *, launcher_path: str | Path | None = None) -> list[str] | None:
    """Return one defensible compact-query segment from a native shell item."""
    normalized = _unwrap_native_command(command)
    if normalized is None or _has_unquoted_comment(normalized):
        return None
    tokens = _native_item_tokens(command, allow_compound=True)
    quoted_tokens = _native_item_tokens(command, preserve_quotes=True, allow_compound=True)
    if tokens is None or quoted_tokens is None or len(tokens) != len(quoted_tokens):
        return None
    for index, executable in enumerate(tokens[:-3]):
        if index and not _is_unquoted_segment_boundary(tokens[index - 1], quoted_tokens[index - 1]):
            continue
        variable_launcher = _is_launcher_variable(executable, quoted_tokens[index])
        # Preserve an unquoted Windows launcher from telemetry: POSIX tokenization removes its backslashes.
        verified_launcher = _is_verified_launcher(executable, launcher_path) or _is_verified_launcher(
            quoted_tokens[index], launcher_path
        )
        if not variable_launcher and not verified_launcher:
            continue
        if variable_launcher and _mutates_launcher(tokens[:index]):
            continue
        if tokens[index + 1 : index + 3] != ["query", "--compact"]:
            continue
        arguments: list[str] = []
        for argument, quoted_argument in zip(tokens[index + 3 :], quoted_tokens[index + 3 :], strict=True):
            if _is_unquoted_segment_boundary(argument, quoted_argument) or (
                argument in {"&", "(", ")", "<", ">"} and argument == quoted_argument
            ):
                break
            arguments.append(argument)
        if not arguments or arguments[0] == "help" or arguments[0].startswith("-"):
            continue
        if any(argument in {"-h", "--help"} for argument in arguments):
            continue
        return arguments
    return None


def _observes_compact_codemap_query(command: str, *, launcher_path: str | Path | None = None) -> bool:
    """Return whether a native item contains a defensible compact-query execution."""
    return _observed_compact_query_arguments(command, launcher_path=launcher_path) is not None


_FALLBACK_TOOLS = frozenset(
    {
        "ack",
        "ag",
        "awk",
        "cat",
        "fd",
        "fgrep",
        "find",
        "egrep",
        "grep",
        "head",
        "less",
        "ls",
        "more",
        "nl",
        "rg",
        "sed",
        "tail",
        "tree",
    }
)


def _is_search_or_read_fallback(command: str) -> bool:
    """Return whether one native item is a search or read substituting for Codemap.

    ``fallback_calls`` previously counted *every* command observed after the
    first Codemap error, so an unrelated ``git status`` or ``pytest`` inflated a
    metric whose name claims the agent fell back to manual searching. Only the
    named search/read tools below are counted now, so the number means what it
    says.

    A compound or redirected item (``grep foo | head``) returns no dedicated
    token list and is therefore excluded. That is a deliberate conservative
    undercount: an over-broad count is what made the old metric unreadable.
    """
    tokens = _native_item_tokens(command)
    return bool(tokens) and Path(tokens[0]).name in _FALLBACK_TOOLS


def _is_codemap_command(
    command: str,
    *,
    launcher_path: str | Path | None = None,
    allow_equivalent_launcher: bool = False,
) -> bool:
    """Return whether a command satisfies the prospective canonical query form."""
    return (
        _canonical_query_arguments(
            command,
            launcher_path=launcher_path,
            allow_equivalent_launcher=allow_equivalent_launcher,
        )
        is not None
    )


def _is_compact_codemap_query(
    command: str,
    *,
    launcher_path: Path | None = None,
    allow_equivalent_launcher: bool = False,
) -> bool:
    """Return whether a command satisfies the canonical compact-query form."""
    return _is_codemap_command(
        command,
        launcher_path=launcher_path,
        allow_equivalent_launcher=allow_equivalent_launcher,
    )


def _command_output(item: Mapping[str, Any]) -> str:
    """Return the captured command output used for deterministic evidence checks."""
    value = item.get("aggregated_output", item.get("output", ""))
    return value if isinstance(value, str) else ""


def _query_output_complete(item: Mapping[str, Any]) -> bool:
    """Return whether output contains JSON proving a completed locked-index query."""
    output = _command_output(item)
    decoder = json.JSONDecoder()
    for offset, character in enumerate(output):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(output[offset:])
        except json.JSONDecodeError:
            continue
        index = payload.get("index") if isinstance(payload, Mapping) else None
        if isinstance(index, Mapping) and index.get("query_complete") is True:
            return True
    return False


def _query_output_compact_complete(item: Mapping[str, Any]) -> bool:
    """Return whether output contains an untruncated complete compact-query document."""
    output = _command_output(item)
    decoder = json.JSONDecoder()
    for offset, character in enumerate(output):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(output[offset:])
        except json.JSONDecodeError:
            continue
        index = payload.get("index") if isinstance(payload, Mapping) else None
        if (
            isinstance(index, Mapping)
            and index.get("query_complete") is True
            and index.get("compact") is True
            and index.get("truncated") is not True
        ):
            return True
    return False


def _canonical_skill_read(command: str, skill_path: Path | None) -> bool:
    """Return whether a dedicated command uses the runner-owned Skill binding."""
    if skill_path is None:
        return False
    normalized = _unwrap_native_command(command)
    return normalized is not None and normalized.strip() == 'cat "$CODEMAP_SKILL_FILE"'


def _completed_with_explicit_zero_exit(item: Mapping[str, Any]) -> bool:
    """Return whether one native command item completed with explicit exit zero."""
    return item.get("status") == "completed" and type(item.get("exit_code")) is int and item["exit_code"] == 0


def _canonical_query_output(item: Mapping[str, Any]) -> bool:
    """Return whether output is one complete compact-query JSON document."""
    try:
        payload = json.loads(_command_output(item))
    except (TypeError, json.JSONDecodeError):
        return False
    index = payload.get("index") if isinstance(payload, Mapping) else None
    return isinstance(index, Mapping) and index.get("query_complete") is True and index.get("compact") is True


def _exact_skill_read_output(item: Mapping[str, Any], skill_path: Path | None, skill_sha256: str) -> bool:
    """Return whether output exactly proves the currently locked Skill bytes."""
    if skill_path is None or not skill_sha256:
        return False
    try:
        locked_skill_bytes = skill_path.read_bytes()
        output_bytes = _command_output(item).encode("utf-8")
    except (OSError, UnicodeEncodeError):
        return False
    return (
        bool(locked_skill_bytes)
        and hashlib.sha256(locked_skill_bytes).hexdigest() == skill_sha256
        and output_bytes == locked_skill_bytes
    )


def _iter_lines(stream: str | bytes | Iterable[str | bytes]) -> Iterable[str]:
    """Decode UTF-8 with replacement and yield text lines from a stream representation.

    Split a scalar string or byte buffer with ``splitlines``. Iterable entries
    are already considered lines and retain any embedded newline characters.

    Examples:
        >>> list(_iter_lines("first\\nsecond\\n"))
        ['first', 'second']
        >>> list(_iter_lines([b"first\\n", "second"]))
        ['first\\n', 'second']
    """
    if isinstance(stream, bytes):
        stream = stream.decode("utf-8", errors="replace")
    if isinstance(stream, str):
        yield from stream.splitlines()
        return
    for line in stream:
        if isinstance(line, bytes):
            yield line.decode("utf-8", errors="replace")
        else:
            yield line


def _append_message_text(current: str, item: Mapping[str, Any]) -> str:
    """Preserve agent-message boundaries when reconstructing one response."""
    addition = _item_text(item)
    if not addition:
        return current
    return f"{current}\n{addition}" if current else addition


def parse_codex_jsonl(
    stream: str | bytes | Iterable[str | bytes],
    *,
    launcher_path: Path | None = None,
    allow_equivalent_launcher: bool = False,
    skill_path: Path | None = None,
    skill_sha256: str = "",
) -> CodexParseResult:
    """Parse Codex ``exec --json`` events into provider-neutral telemetry.

    Codex has used both ``item.completed`` events and Claude-compatible
    assistant blocks across CLI versions.  This parser accepts both shapes,
    deduplicates lifecycle events by item ID, and retains every valid parsed
    event in ``raw_events`` for audit/debugging.

    ``launcher_path`` may identify an observed exact executable path as either a native path or a serialized POSIX or
    Windows string. Set ``allow_equivalent_launcher`` only for a future run whose contract permits
    that verified path to receive canonical credit; its default preserves
    historical variable-only adherence during replay.

    ``codemap_observed_complete_calls`` counts untruncated compact JSON output;
    ``codemap_observed_successful_calls`` additionally requires explicit zero
    exit status. Compound segments can supply these observational signals but
    never canonical treatment credit.

    Observed vs configured: the ``codemap_*`` call and error counters are read
    from the stream, but the ``_skill_`` / ``_direct_`` split is *not*. It is
    derived from whether the caller passed ``skill_path``, which the runner does
    only for the C_strict home. Treat every skill-vs-direct number as a
    restatement of the arm assignment, not as evidence of Skill mediation; the
    only observational Skill signal is ``skill_delivery_observed``.
    """
    result = CodexParseResult()
    seen_items: set[str] = set()
    pending_items: set[str] = set()
    compact_query_attempt_seen = False
    saw_terminal = False
    saw_authentication_failure = False
    for raw_line in _iter_lines(stream):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            result.malformed_lines += 1
            continue
        if not isinstance(event, dict):
            result.malformed_lines += 1
            continue
        result.raw_events.append(_redact_sensitive_event(event))
        event_type = str(event.get("type", ""))
        if event_type == "thread.started":
            result.thread_id = str(event.get("thread_id", ""))

        usage = event.get("usage")
        if isinstance(usage, Mapping):
            _ingest_usage(result, usage)

        item = event.get("item")
        if isinstance(item, Mapping):
            item_id = str(item.get("id", ""))
            item_type = str(item.get("type", ""))
            command = _command_text(item)
            if event_type == "item.completed" and item_type:
                result.item_counts[item_type] = result.item_counts.get(item_type, 0) + 1
            if item_type == "agent_message" and event_type in {"", "item.completed"}:
                result.output_text = _append_message_text(result.output_text, item)
            if item_type in {"command_execution", "shell_command", "command"}:
                if event_type == "item.started" and item_id:
                    pending_items.add(item_id)
                if event_type == "item.completed" and item_id not in seen_items:
                    seen_items.add(item_id)
                    pending_items.discard(item_id)
                    result.last_tool_text_offset = len(result.output_text)
                    result.command_calls += 1
                    duration_ms = item.get("duration_ms", item.get("elapsed_ms"))
                    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
                        elapsed = max(float(duration_ms), 0.0) / 1000.0
                        result.tool_elapsed_s = (result.tool_elapsed_s or 0.0) + elapsed
                    skill_read_verified = (
                        _canonical_skill_read(command, skill_path)
                        and _completed_with_explicit_zero_exit(item)
                        and _exact_skill_read_output(item, skill_path, skill_sha256)
                    )
                    observed_query = _observes_compact_codemap_query(command, launcher_path=launcher_path)
                    credited_arguments = _canonical_query_arguments(
                        command,
                        launcher_path=launcher_path,
                        allow_equivalent_launcher=allow_equivalent_launcher,
                    )
                    if observed_query:
                        result.codemap_observed_calls += 1
                        observed_complete = _query_output_compact_complete(item)
                        if observed_complete:
                            result.codemap_observed_complete_calls += 1
                        if _completed_with_explicit_zero_exit(item) and observed_complete:
                            result.codemap_observed_successful_calls += 1
                    if credited_arguments is not None:
                        result.codemap_calls += 1
                        # CONFIGURED BY CONSTRUCTION, NOT OBSERVED. `skill_path` is
                        # non-None only for the C_strict home, so this split
                        # restates the caller's arm assignment; the stream carries no
                        # evidence distinguishing a Skill-mediated query from a direct
                        # one, because both end as the same `$CODEMAP_BIN` command.
                        # The immutable C home proves the installed-Skill treatment.
                        # A manual Skill-file read remains useful audit evidence, but
                        # requiring it would add ceremony unrelated to a query's use.
                        # Consequence: `_arm_compliance` for C_strict (see
                        # run-codex-structural.py) is a home-integrity claim plus a
                        # credited successful query — not proof the Skill was read.
                        # `skill_delivery_observed` is the separate observational signal.
                        delivery = "skill" if skill_path is not None else "direct"
                        if delivery == "direct":
                            result.codemap_direct_calls += 1
                        else:
                            result.codemap_skill_calls += 1
                        if not _completed_with_explicit_zero_exit(item) or not _canonical_query_output(item):
                            result.codemap_errors += 1
                        else:
                            result.codemap_successful_calls += 1
                            result.codemap_compact_successful_calls += 1
                            result.successful_query_arguments.append(credited_arguments)
                            if delivery == "direct":
                                result.codemap_direct_successful_calls += 1
                                result.codemap_direct_compact_successful_calls += 1
                            else:
                                result.codemap_skill_successful_calls += 1
                                result.codemap_skill_compact_successful_calls += 1
                    if not observed_query and result.codemap_errors and _is_search_or_read_fallback(command):
                        result.fallback_calls += 1
                    if skill_read_verified and not compact_query_attempt_seen:
                        result.skill_delivery_observed = True
                    if _records_compact_query_attempt(
                        command,
                        launcher_path=launcher_path,
                        allow_equivalent_launcher=allow_equivalent_launcher,
                    ):
                        compact_query_attempt_seen = True

        # Compatibility with older/fixture streams that use assistant blocks.
        message = event.get("message")
        if isinstance(message, Mapping):
            for block in message.get("content", []):
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "text":
                    text_item = {"text": str(block.get("text", ""))}
                    result.output_text = _append_message_text(result.output_text, text_item)
                if block.get("type") == "tool_use":
                    result.last_tool_text_offset = len(result.output_text)
                    name = str(block.get("name", ""))
                    command = _tool_use_command(block)
                    if name.lower() in {"bash", "shell", "command_execution"}:
                        result.command_calls += 1
                    if _is_codemap_command(
                        command,
                        launcher_path=launcher_path,
                        allow_equivalent_launcher=allow_equivalent_launcher,
                    ):
                        result.codemap_calls += 1
                        if result.skill_delivery_observed:
                            result.codemap_skill_calls += 1
                        else:
                            result.codemap_direct_calls += 1

        if event_type in {"turn.completed", "result", "response.completed"}:
            saw_terminal = True
            status = str(event.get("status", event.get("subtype", "completed"))).lower()
            if status in {"completed", "success", "succeeded", ""}:
                result.completed = True
            else:
                result.incomplete = True
                result.error = result.error or status
                result.error_type = result.error_type or "turn_incomplete"
        if event_type in {"error", "turn.failed", "response.failed"}:
            saw_terminal = True
            result.retryable = True
            result.incomplete = True
            error = event.get("error") or event.get("message") or event.get("detail")
            result.error = _redact_provider_error(error) if error else event_type
            native_error_type = event.get("error_type")
            if isinstance(native_error_type, str) and native_error_type:
                result.error_type = native_error_type
            elif event_type == "turn.failed":
                result.error_type = "turn_failed"
            elif event_type == "response.failed":
                result.error_type = "response_failed"
            else:
                result.error_type = "transport_error"
            if _is_refresh_token_authentication_failure(result.error):
                saw_authentication_failure = True
    if not saw_terminal and not result.error:
        result.incomplete = True
        result.error = "missing terminal event"
        result.error_type = "missing_terminal"
        result.retryable = not result.raw_events
    if pending_items:
        result.completed = False
        result.incomplete = True
        result.error = result.error or "terminal event left command items incomplete"
        result.error_type = result.error_type or "pending_item"
    if result.malformed_lines:
        result.completed = False
        result.incomplete = True
        result.error = result.error or f"malformed JSONL ({result.malformed_lines} line(s))"
        result.error_type = result.error_type or "malformed_stream"
    if saw_authentication_failure or _is_refresh_token_authentication_failure(result.error):
        result.error_type = "authentication_failed"
        result.retryable = False
    return result


def print_unified_paid_command(
    *,
    repo_path: Path,
    manifest_path: Path,
    index_path: Path,
    marketplace_root: Path,
    codemap_bin: Path,
    model: str,
    selectors: Sequence[str],
    scope_sha256: str,
    patch_pytest: str | None = None,
    index_relocation_path: Path | None = None,
) -> None:
    """Print the exact shell-safe command authorized by a unified dry run.

    A run whose index was relocated into its own worktree is admitted by that relocation's provenance, so the copyable
    command has to carry it; without it the paid run would be refused for the byte identity a worktree index cannot
    have.
    """
    quote = shlex.quote
    run_dir = Path("benchmarks") / "results" / f"codex-unified-{uuid4().hex[:12]}"
    prefix = f"CODEMAP_BENCH_PATCH_PYTEST={quote(patch_pytest)} " if patch_pytest else ""
    lines = [
        f"{prefix}python3 benchmarks/run-codex-structural.py \\",
        f"  --repo-path {quote(str(repo_path.resolve()))} \\",
        f"  --manifest-path {quote(str(manifest_path.resolve()))} \\",
        f"  --index-path {quote(str(index_path.resolve()))} \\",
        f"  --marketplace-root {quote(str(marketplace_root.resolve()))} \\",
        f"  --codemap-bin {quote(str(codemap_bin.resolve()))} \\",
        f"  --model {quote(model)} \\",
    ]
    if selectors:
        lines.append(f"  --tasks {quote(','.join(selectors))} \\")
    if index_relocation_path is not None:
        lines.append(f"  --index-relocation-path {quote(str(Path(index_relocation_path).resolve()))} \\")
    lines.append('  --auth-source "$HOME/.codex/auth.json" \\')
    lines.append(f"  --run-dir {quote(str(run_dir))} \\")
    lines.append(f"  --paid-approval {paid_approval_token(scope_sha256)}")
    print(presentation.format_paid_command_block(lines))


ARM_ROW_STYLES = presentation.ARM_ROW_STYLES
ARM_ROW_ANSI_CODES = {
    "A_plain": "33",
    "B_auto": "36",
    "C_strict": "35",
}
#: What each treatment's contract permits of Codemap, printed beside the probe's measured
#: availability. B_auto and C_strict both find the binary, so ``codemap=true`` alone made their rows
#: identical; the obligation belongs in its own field rather than overloading the measured fact.
ARM_CODEMAP_USE = {"A_plain": "forbidden", "B_auto": "optional", "C_strict": "required"}
_RESULT_ARM = re.compile(r"^\(\d+/\d+\)\s+.*\b(A_plain|B_auto|C_strict)\b")
_CONSOLE = presentation.benchmark_console()
_PROGRESS_PREFIX = re.compile(r"^\((\d+)/(\d+)\)")
_PROGRESS_SCOPE: ContextVar[tuple[int, int] | None] = ContextVar("codex_progress_scope", default=None)
#: Arm names are short enough to print unabbreviated, so the display label is the canonical name.
_DISPLAY_ARM_COLUMN_WIDTH = max(len(arm) for arm in ARM_ROW_ANSI_CODES)


def format_plan_row(task_id: str, repetition: int, arm: str) -> str:
    """Format one deterministic structural coordinate as an aligned terminal row."""
    return f"PLAN    {task_id:<5}  rep={repetition}  {arm}"


def format_structural_result_row(
    *,
    status: str,
    task_id: str,
    repetition: int,
    arm: str,
    input_tokens: int,
    cached_input_tokens: int,
    fresh_tokens: int | None,
    output_tokens: int,
    elapsed_s: float,
    quality: str,
    adherence: bool,
    codemap_used: bool,
    query_conformance: bool | None = None,
    headline_eligible: bool | None = None,
) -> str:
    """Format one structural result with stable columns and compact shared units."""
    del cached_input_tokens, fresh_tokens
    query_status = ""
    if query_conformance is not None:
        cohort = "" if headline_eligible is None else ("  cohort:H" if headline_eligible else "  cohort:D")
        query_status = f"  query:{'✓' if query_conformance else '✗'}{cohort}"
    display_arm = arm
    return (
        f"{status}  {task_id:<5}  rep={repetition}  {display_arm:<{_DISPLAY_ARM_COLUMN_WIDTH}}"
        f"  in={fmt_tok(input_tokens):>6}"
        f"  out={fmt_tok(output_tokens):>5}  time={fmt_time(elapsed_s):>5}"
        f"  quality={quality:>5}  treatment:{'✓' if adherence else '✗'}  codemap-used:{'✓' if codemap_used else '✗'}"
        f"{query_status}"
    )


@contextmanager
def progress_scope(*, completed_offset: int, total_cells: int) -> Iterator[None]:
    """Map native stage counters onto one aggregate terminal counter.

    The scope changes presentation only. Stage telemetry, metadata, and plain
    run logs retain their native progress coordinates so their checksums and
    standalone interpretation remain independent.

    Args:
        completed_offset: Cells completed by earlier stages.
        total_cells: Total cells across the unified execution.

    Raises:
        ValueError: If the aggregate coordinates cannot contain another cell.
    """
    if (
        type(completed_offset) is not int
        or completed_offset < 0
        or type(total_cells) is not int
        or total_cells <= 0
        or completed_offset >= total_cells
    ):
        raise ValueError("progress scope requires 0 <= completed_offset < total_cells")
    token = _PROGRESS_SCOPE.set((completed_offset, total_cells))
    try:
        yield
    finally:
        _PROGRESS_SCOPE.reset(token)


def _map_progress_row(row: str) -> str:
    """Apply the active aggregate offset to one native terminal row."""
    scope = _PROGRESS_SCOPE.get()
    match = _PROGRESS_PREFIX.match(row)
    if scope is None or match is None:
        return row
    local_completed, local_total = (int(value) for value in match.groups())
    completed_offset, total_cells = scope
    if local_total <= 0 or not 1 <= local_completed <= local_total or completed_offset + local_total > total_cells:
        raise ValueError("native progress row is outside the active aggregate progress scope")
    return f"({completed_offset + local_completed}/{total_cells}){row[match.end() :]}"


def print_arm_row(row: str, arm: str) -> None:
    """Print an arm row with scoped progress and interactive Rich color."""
    row = _map_progress_row(row)
    presentation.print_arm_row(row, arm, console=_CONSOLE)


def print_plan_row(row: str) -> None:
    """Print one no-model plan or probe row with interactive Rich arm color."""
    presentation.print_plan_row(row, console=_CONSOLE)


def print_section_rule(title: str) -> None:
    """Announce one run phase as a titled rule, or as ``== title ==`` when output is redirected."""
    presentation.print_section_rule(title, console=_CONSOLE)


def print_structural_legend() -> None:
    """Emit the structural legend as a panel on a terminal and as framed plain rules elsewhere."""
    presentation.print_legend(STRUCTURAL_LEGEND_BODY, console=_CONSOLE)


def format_probe_row(arm: str, fields: Mapping[str, Any]) -> str:
    """Format one capability probe row with its fields in fixed, tab-free columns."""
    return presentation.format_probe_row(arm, fields)


def probe_use(arm: str) -> str:
    """Return what one treatment's contract permits or demands of Codemap use.

    Args:
        arm: Canonical benchmark arm label.

    Returns:
        ``forbidden``, ``optional``, or ``required``.

    Raises:
        ValueError: If the row uses an unknown benchmark arm.
    """
    try:
        return ARM_CODEMAP_USE[arm]
    except KeyError as exc:
        raise ValueError(f"unknown benchmark arm {arm!r}") from exc


def _result_arm(row: str) -> str | None:
    """Return the canonical arm encoded in one human-readable result row."""
    match = _RESULT_ARM.search(row)
    return match.group(1) if match else None


def _row_arm(row: str) -> str | None:
    """Return the canonical arm of one result, plan, or probe row."""
    arm = _result_arm(row)
    if arm is not None:
        return arm
    return presentation.plan_row_arm(row)


def render_result_rows(
    rows: Iterable[str], output: TextIO, *, force_color: bool = False, hide_plan: bool = False
) -> None:
    """Render unwrapped result rows with terminal color and ANSI-free redirected output."""
    use_color = force_color or output.isatty()
    if not use_color:
        for row in rows:
            if not (hide_plan and row.startswith("PLAN ")):
                output.write(row)
        output.flush()
        return

    console = Console(
        file=output,
        force_terminal=use_color,
        color_system="standard" if use_color else None,
        highlight=False,
        markup=False,
        no_color=not use_color,
        legacy_windows=False if force_color else None,
        width=presentation.BENCHMARK_OUTPUT_WIDTH,
    )
    legend_lines: list[str] | None = None

    def flush_legend() -> None:
        """Render one accumulated plain legend section as a titled Rich panel."""
        nonlocal legend_lines
        if legend_lines is None:
            return
        body = "\n".join(line.rstrip("\r\n") for line in legend_lines[1:-1])
        # Both console and panel use the shared width, preventing terminal-width clipping on replay.
        console.print(
            Panel(
                body,
                title="Legend",
                subtitle="End legend",
                border_style="blue",
                width=presentation.BENCHMARK_OUTPUT_WIDTH,
            )
        )
        legend_lines = None

    for row in rows:
        if hide_plan and row.startswith("PLAN "):
            continue
        stripped = row.rstrip("\r\n")
        if legend_lines is not None:
            legend_lines.append(row)
            if stripped == presentation.LEGEND_CLOSE_RULE:
                flush_legend()
            continue
        if stripped == presentation.LEGEND_OPEN_RULE:
            legend_lines = [row]
            continue
        arm = _row_arm(row)
        if arm is None:
            output.write(row)
            continue
        if force_color:
            colored_row = f"\x1b[{ARM_ROW_ANSI_CODES[arm]}m{stripped}\x1b[0m\n"
            if output is sys.stdout:
                output.flush()
                os.write(output.fileno(), colored_row.encode("utf-8"))
            else:
                output.write(colored_row)
            continue
        console.print(row.rstrip("\n"), style=ARM_ROW_STYLES[arm], end="\n", soft_wrap=True)
    flush_legend()
    output.flush()
