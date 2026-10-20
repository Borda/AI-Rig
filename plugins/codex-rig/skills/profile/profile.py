"""Summarize explicitly selected native Codex session telemetry without copying conversation content.

## Purpose

Project native session JSONL into observed token counters and inclusive completed-tool timing. The profile skill uses
this deterministic reader to explain a session while leaving native Codex collection, /status and /usage intact.

## Scope

Read only caller-selected files and an optional local rate sheet. Never discover other sessions, invoke models, access
credentials or networks, enable instrumentation, or infer reasoning time from a wall-clock residual. Separate inputs
remain separate reports: neither agent ancestry nor a grand total is inferred from file names or concurrent activity.

## Usage

Run through the plugin Python launcher with repeated --input paths and optional --rates JSON. Latest native thread
counters take precedence over legacy cumulative token_count events; counters are not summed across turns or streams.
Rates contain as_of, source and models mapping model names to input_per_million, cached_input_per_million and
output_per_million USD rates. Pricing requires observed request increments to explain cumulative usage from zero under
known contexts. Missing attribution produces an unavailable estimate instead of guessed billing. ``--check-report``
validates a retained sanitized report and derives canonical Results rows without reopening its private source paths.

## Outputs

Print schema-1 JSON containing source prefix hashes, capture lengths, observed counters, tool-class counts and inclusive
durations, optional supplied-rate estimates, and explicit limits. Raw prompts, reasoning, commands, tool arguments and
results are never projected. Inputs are opened read-only; stdout is the only report destination.

## Failure

Malformed complete JSON or recognized telemetry, invalid rates, duplicate inputs, counter inconsistencies and changed
captured prefixes exit one with bounded diagnostics that omit raw content. Unknown valid event families are ignored. An
incomplete final JSONL record degrades a live capture; append growth is accepted after prefix verification.

## Used by

The installed profile skill retains this JSON as profile.json before producing its canonical workflow handoff. Tests
exercise the CLI and the read-only verify_capture function with native record shapes on every supported platform.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
TOOL_KINDS = {"CommandExecution", "McpToolCall", "FileChange", "CollabAgentToolCall", "Extension"}
PRICING_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "total_tokens")
BASE_LIMITS = (
    "inclusive-tool-time-overlaps-and-is-not-session-wall-time",
    "no-tool-or-skill-token-attribution",
    "selected-file-may-omit-prior-history-or-agents",
)


def native_usage(value: Any) -> dict[str, int | None]:
    """Validate counters and preserve unavailable optional subsets without inventing zeros."""
    if not isinstance(value, dict):
        raise ValueError("invalid-token-usage")
    result = {key: value.get(key) for key in TOKEN_FIELDS}
    for key, count in result.items():
        if count is None and key not in {"input_tokens", "output_tokens", "total_tokens"}:
            continue
        if type(count) is not int or count < 0:
            raise ValueError("invalid-token-counter")
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        raise ValueError("token-total-mismatch")
    for subset, parent in (
        ("cached_input_tokens", "input_tokens"),
        ("cache_write_input_tokens", "input_tokens"),
        ("reasoning_output_tokens", "output_tokens"),
    ):
        if result[subset] is not None and result[subset] > result[parent]:
            raise ValueError("token-subset-exceeds-total")
    return result


def verify_capture(path: Path, capture: dict[str, Any]) -> int:
    """Verify an immutable captured prefix and return appended bytes without rejecting normal live growth."""
    remaining = capture["bytes"]
    hashed = hashlib.sha256()
    with path.open("rb") as handle:
        while remaining:
            block = handle.read(min(remaining, 1024 * 1024))
            if not block:
                raise ValueError("capture-prefix-truncated")
            hashed.update(block)
            remaining -= len(block)
        if hashed.hexdigest() != capture["sha256"]:
            raise ValueError("capture-prefix-changed")
        handle.seek(0, 2)
        return max(0, handle.tell() - capture["bytes"])


def item_duration(payload: dict[str, Any], item: dict[str, Any]) -> float | None:
    """Prefer native execution duration, then an explicit completed-item timestamp interval."""
    duration = item.get("duration")
    if duration is not None:
        if not isinstance(duration, dict):
            raise ValueError("invalid-tool-duration")
        seconds, nanos = duration.get("secs"), duration.get("nanos")
        if type(seconds) is not int or type(nanos) is not int or seconds < 0 or not 0 <= nanos < 1000000000:
            raise ValueError("invalid-tool-duration")
        return seconds * 1000 + nanos / 1000000
    if "durationMs" in item:
        value = item["durationMs"]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid-tool-duration")
        return float(value)
    start, end = payload.get("started_at_ms"), payload.get("completed_at_ms")
    if start is None or end is None:
        return None
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        raise ValueError("invalid-tool-timestamps")
    return float(end - start)


def summarize(path: Path) -> tuple[dict[str, Any], str | None]:
    """Read a bounded native prefix and project only observed usage and completed tool timing."""
    samples: dict[str, dict[str, int | None]] = {}
    resets: set[str] = set()
    models: set[str] = set()
    active_model = None
    pricing_covered = True
    tools: dict[str, tuple[str, float | None]] = {}
    identity = None
    limits = list(BASE_LIMITS)
    hashed = hashlib.sha256()
    with path.open("rb") as handle:
        handle.seek(0, 2)
        captured_size = handle.tell()
        handle.seek(0)
        remaining = captured_size
        while remaining:
            raw = handle.readline(remaining)
            if not raw:
                raise ValueError("capture-prefix-truncated")
            remaining -= len(raw)
            hashed.update(raw)
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                if remaining == 0 and not raw.endswith(b"\n"):
                    limits.append("incomplete-final-row")
                    break
                raise ValueError("invalid-json-row") from None
            if not isinstance(row, dict):
                raise ValueError("invalid-row-envelope")
            kind = row.get("type")
            if not isinstance(kind, str):
                raise ValueError("invalid-row-envelope")
            if kind not in {"session_meta", "turn_context", "token_usage_record", "event_msg"}:
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("invalid-telemetry-payload")
            if kind == "session_meta":
                value = payload.get("id")
                if not isinstance(value, str) or not value or identity is not None:
                    raise ValueError("invalid-session-identity")
                identity = value
            elif kind == "turn_context":
                model = payload.get("model")
                active_model = model if isinstance(model, str) and model else None
                if isinstance(model, str) and model:
                    models.add(model)
            stream = None
            if kind == "token_usage_record":
                stream, observed = "thread_token_usage", payload.get("thread_token_usage")
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                if info is None:
                    continue
                if not isinstance(info, dict):
                    raise ValueError("invalid-token-info")
                stream, observed = "legacy_total_token_usage", info.get("total_token_usage")
            if stream is not None:
                current = native_usage(observed)
                previous = samples.get(stream)
                if stream == "thread_token_usage":
                    increment = native_usage(payload["usage"]) if payload.get("usage") is not None else None
                    if current != previous:
                        baseline = previous or dict.fromkeys(PRICING_FIELDS, 0)
                        # A cumulative prefix can contain earlier unobserved models or missing requests.
                        # Price it only when observed request increments explain every charged counter from zero.
                        pricing_covered &= (
                            bool(active_model)
                            and increment is not None
                            and all(
                                current[key] is not None
                                and baseline[key] is not None
                                and increment[key] is not None
                                and current[key] - baseline[key] == increment[key]
                                for key in PRICING_FIELDS
                            )
                        )
                if previous and any(
                    current[key] is not None and previous[key] is not None and current[key] < previous[key]
                    for key in TOKEN_FIELDS
                ):
                    resets.add(stream)
                samples[stream] = current
            if kind == "event_msg" and payload.get("type") == "item_completed":
                item = payload.get("item")
                if not isinstance(item, dict):
                    raise ValueError("invalid-completed-item")
                if not isinstance(item.get("type"), str):
                    raise ValueError("invalid-completed-item")
                if item["type"] not in TOOL_KINDS:
                    continue
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    raise ValueError("tool-item-id-required")
                tools[item_id] = (item["type"], item_duration(payload, item))
    capture = {"bytes": captured_size, "sha256": hashed.hexdigest()}
    capture["appended_bytes"] = verify_capture(path, capture)
    if capture["appended_bytes"]:
        limits.append("source-grew-after-capture")
    if not identity:
        limits.append("session-identity-not-observed")
    stream = "thread_token_usage" if "thread_token_usage" in samples else "legacy_total_token_usage"
    if stream not in samples or stream in resets:
        tokens = {
            "usage": None,
            "basis": "unavailable",
            "reason": "cumulative-counter-decreased" if stream in resets else "token-usage-not-observed",
        }
    else:
        tokens = {"usage": samples[stream], "basis": stream, "reason": None}
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "timed_calls": 0, "inclusive_duration_ms": None}
    )
    for tool_kind, duration in tools.values():
        group = grouped[tool_kind]
        group["calls"] += 1
        if duration is not None:
            group["timed_calls"] += 1
            group["inclusive_duration_ms"] = (group["inclusive_duration_ms"] or 0) + duration
    return {
        "source": path.as_posix(),
        "capture": capture,
        "models": sorted(models),
        "model_attribution_complete": bool(samples.get("thread_token_usage")) and pricing_covered and not resets,
        "tokens": tokens,
        "tools": [{"kind": key, **grouped[key]} for key in sorted(grouped)],
        "limits": limits,
    }, identity


def read_rates(path: Path) -> tuple[dict[str, Any], str]:
    """Load an explicit local USD rate sheet without embedding or fetching provider prices."""
    raw = path.read_bytes()
    try:
        rates = json.loads(raw)
        date.fromisoformat(rates["as_of"])
        if not isinstance(rates["source"], str) or not rates["source"]:
            raise ValueError
        if not isinstance(rates["models"], dict):
            raise ValueError
        for model, values in rates["models"].items():
            if not isinstance(model, str) or not isinstance(values, dict):
                raise ValueError
            for key in ("input_per_million", "cached_input_per_million", "output_per_million"):
                value = values[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("invalid-rate-sheet") from None
    return rates, hashlib.sha256(raw).hexdigest()


def estimate_cost(session: dict[str, Any], rates: tuple[dict[str, Any], str] | None) -> dict[str, Any]:
    """Estimate only complete single-model usage, treating cached and reasoning counts as subsets."""
    reason = None
    counters = session["tokens"]["usage"]
    if rates is None:
        reason = "rates-not-supplied"
    elif counters is None:
        reason = "token-usage-unavailable"
    elif (
        not session["model_attribution_complete"]
        or len(session["models"]) != 1
        or session["models"][0] not in rates[0]["models"]
    ):
        reason = "model-attribution-unavailable"
    elif counters["cached_input_tokens"] is None or counters["cache_write_input_tokens"] != 0:
        reason = "cache-accounting-unavailable"
    if reason:
        return {"estimate_usd": None, "basis": "unavailable", "reason": reason}
    sheet, rate_digest = rates
    prices = sheet["models"][session["models"][0]]
    amount = sum(
        Decimal(count) * Decimal(str(prices[key]))
        for count, key in (
            (counters["input_tokens"] - counters["cached_input_tokens"], "input_per_million"),
            (counters["cached_input_tokens"], "cached_input_per_million"),
            (counters["output_tokens"], "output_per_million"),
        )
    ) / Decimal(1000000)
    return {
        "estimate_usd": format(amount.normalize(), "f"),
        "basis": "supplied-rates-estimate-not-billing",
        "rates_sha256": rate_digest,
        "as_of": sheet["as_of"],
        "rates": {key: prices[key] for key in ("input_per_million", "cached_input_per_million", "output_per_million")},
        "reason": None,
    }


def report_rows(report: Any) -> list[list[str]]:
    """Validate retained measurement structure and derive the exact rows consumed by artifact admission."""
    if not isinstance(report, dict) or set(report) != {"schema_version", "aggregation", "sessions"}:
        raise ValueError("invalid-profile-report")
    if (
        type(report["schema_version"]) is not int
        or report["schema_version"] != 1
        or report["aggregation"] != "separate-sessions-no-grand-total"
        or not isinstance(report["sessions"], list)
    ):
        raise ValueError("invalid-profile-report")
    rows = []
    for index, session in enumerate(report["sessions"], start=1):
        if not isinstance(session, dict) or set(session) != {
            "source",
            "capture",
            "models",
            "model_attribution_complete",
            "tokens",
            "tools",
            "limits",
            "label",
            "cost",
        }:
            raise ValueError("invalid-profile-session")
        source = session["source"]
        if (
            not isinstance(source, str)
            or not (PurePosixPath(source).is_absolute() or PureWindowsPath(source).is_absolute())
            or session["label"] != f"session-{index}"
        ):
            raise ValueError("invalid-profile-source")
        capture = session["capture"]
        if (
            not isinstance(capture, dict)
            or set(capture) != {"bytes", "sha256", "appended_bytes"}
            or any(type(capture[key]) is not int or capture[key] < 0 for key in ("bytes", "appended_bytes"))
            or not isinstance(capture["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", capture["sha256"])
        ):
            raise ValueError("invalid-profile-capture")
        if (
            not isinstance(session["models"], list)
            or any(not isinstance(model, str) or not model for model in session["models"])
            or type(session["model_attribution_complete"]) is not bool
        ):
            raise ValueError("invalid-profile-models")
        if (
            not isinstance(session["limits"], list)
            or not all(
                isinstance(limit, str)
                and limit
                in {*BASE_LIMITS, "incomplete-final-row", "source-grew-after-capture", "session-identity-not-observed"}
                for limit in session["limits"]
            )
            or not set(BASE_LIMITS).issubset(session["limits"])
        ):
            raise ValueError("invalid-profile-limits")
        tokens = session["tokens"]
        if not isinstance(tokens, dict) or set(tokens) != {"usage", "basis", "reason"}:
            raise ValueError("invalid-profile-tokens")
        counters = tokens["usage"]
        if counters is None:
            if tokens["basis"] != "unavailable" or tokens["reason"] not in {
                "token-usage-not-observed",
                "cumulative-counter-decreased",
            }:
                raise ValueError("invalid-profile-tokens")
        elif (
            not isinstance(counters, dict)
            or set(counters) != set(TOKEN_FIELDS)
            or native_usage(counters) != counters
            or tokens["basis"] not in {"thread_token_usage", "legacy_total_token_usage"}
            or tokens["reason"] is not None
        ):
            raise ValueError("invalid-profile-tokens")
        if not isinstance(session["tools"], list):
            raise ValueError("invalid-profile-tools")
        seen = set()
        duration = 0.0
        calls = timed = 0
        for tool in session["tools"]:
            if (
                not isinstance(tool, dict)
                or set(tool) != {"kind", "calls", "timed_calls", "inclusive_duration_ms"}
                or not isinstance(tool["kind"], str)
                or tool["kind"] not in TOOL_KINDS
                or tool["kind"] in seen
            ):
                raise ValueError("invalid-profile-tools")
            if (
                type(tool["calls"]) is not int
                or type(tool["timed_calls"]) is not int
                or not 0 <= tool["timed_calls"] <= tool["calls"]
            ):
                raise ValueError("invalid-profile-tools")
            value = tool["inclusive_duration_ms"]
            if (tool["timed_calls"] == 0 and value is not None) or (
                tool["timed_calls"] > 0 and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)
            ):
                raise ValueError("invalid-profile-tools")
            seen.add(tool["kind"])
            calls += tool["calls"]
            timed += tool["timed_calls"]
            duration += value or 0
        if not math.isfinite(duration):
            raise ValueError("invalid-profile-tools")
        cost = session["cost"]
        if not isinstance(cost, dict):
            raise ValueError("invalid-profile-cost")
        if cost.get("estimate_usd") is None:
            if (
                set(cost) != {"estimate_usd", "basis", "reason"}
                or cost["basis"] != "unavailable"
                or cost["reason"]
                not in {
                    "rates-not-supplied",
                    "token-usage-unavailable",
                    "model-attribution-unavailable",
                    "cache-accounting-unavailable",
                }
            ):
                raise ValueError("invalid-profile-cost")
        else:
            if (
                set(cost) != {"estimate_usd", "basis", "rates_sha256", "as_of", "rates", "reason"}
                or not isinstance(cost["rates_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", cost["rates_sha256"])
                or not isinstance(cost["rates"], dict)
                or set(cost["rates"]) != {"input_per_million", "cached_input_per_million", "output_per_million"}
            ):
                raise ValueError("invalid-profile-cost")
            if any(
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in cost["rates"].values()
            ):
                raise ValueError("invalid-profile-cost")
            try:
                date.fromisoformat(cost["as_of"])
            except (ValueError, TypeError):
                raise ValueError("invalid-profile-cost") from None
            sheet = {"models": {model: cost["rates"] for model in session["models"]}, "as_of": cost["as_of"]}
            if estimate_cost(session, (sheet, cost["rates_sha256"])) != cost:
                raise ValueError("invalid-profile-cost")
        rows.append(
            [
                session["label"],
                f"{counters['total_tokens']} total" if counters is not None else "Unavailable",
                f"{duration / 1000:.6g} s inclusive ({timed}/{calls} timed)" if timed else "Unavailable",
                f"USD {cost['estimate_usd']} estimated" if cost["estimate_usd"] is not None else "Unavailable",
                "Selected prefix only",
            ]
        )
    return rows or [["Unavailable", "Unavailable", "Unavailable", "Unavailable", "No selected telemetry"]]


def main() -> int:
    """Print a sanitized report or one bounded diagnostic without exposing raw input."""
    parser = argparse.ArgumentParser(description="Read selected native Codex JSONL; print sanitized session profiles.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--input",
        type=Path,
        action="append",
        help="Explicit session JSONL; repeat for separate agent/session rows",
    )
    selection.add_argument(
        "--check-report",
        type=Path,
        help="Validate retained JSON and derive canonical Results rows without reopening source logs",
    )
    parser.add_argument("--rates", type=Path, help="Optional local USD per-million model rates with as_of and source")
    arguments = parser.parse_args()
    try:
        if arguments.check_report:
            if arguments.rates:
                parser.error("rates cannot be combined with report checking")
            try:
                report = json.loads(arguments.check_report.read_bytes())
            except (ValueError, UnicodeError):
                raise ValueError("invalid-profile-json") from None
            try:
                rows = report_rows(report)
            except (KeyError, TypeError):
                raise ValueError("invalid-profile-report") from None
            observed = any(
                session["tokens"]["usage"] is not None or any(tool["timed_calls"] for tool in session["tools"])
                for session in report["sessions"]
            )
            print(json.dumps({"rows": rows, "observed": observed}))
            return 0
        paths = [path.resolve(strict=True) for path in arguments.input]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate-input-path")
        rates = read_rates(arguments.rates) if arguments.rates else None
        sessions = []
        identities: set[str] = set()
        for index, path in enumerate(paths, start=1):
            session, identity = summarize(path)
            if identity and identity in identities:
                raise ValueError("duplicate-session-identity")
            if identity:
                identities.add(identity)
            session["label"] = f"session-{index}"
            session["cost"] = estimate_cost(session, rates)
            sessions.append(session)
        report = {"schema_version": 1, "aggregation": "separate-sessions-no-grand-total", "sessions": sessions}
        report_rows(report)
        print(json.dumps(report, allow_nan=False))
    except OSError:
        print("profile: source-or-rates-unreadable", file=sys.stderr)
        return 1
    except RecursionError:
        # Older supported decoders reject deep JSON before field validation; never classify it as a live tail.
        print("profile: invalid-json-nesting", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"profile: {error}", file=sys.stderr)
        return 1
    except OverflowError:
        print("profile: invalid-numeric-telemetry", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
