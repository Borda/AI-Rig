"""Admit copied change-impact fixtures to Codex-native isolated cells.

Purpose: validate immutable copied-fixture and frozen-index coordinates before a provider cell starts, then map the
existing native Codex transport into the common change-impact result row. Scope: this is only the held-out source-impact
fixture boundary; it never reads graph-suite tasks, answers, evaluator history, or behavioral oracle material. Usage:
the Codex agentic runner constructs one runtime for each fresh fixture cell, optionally probes all A/B/C homes during a
no-model preflight, and invokes ``run_cell`` only for an admitted coordinate. Outputs: a parsed immutable coordinate or
a normalized native transport row. Failure: malformed coordinates, source or index drift, sandbox contamination, and
malformed native streams fail closed with contextual evidence. Used by: ``run-codex-agentic.py`` and focused fixture-
runtime regression tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from _bench_common import change_impact_contracts as contracts
from _bench_common.provider_parity_contracts import treatment_adherence


_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class FixtureRuntimeCoordinate:
    """Immutable identity required for one fresh copied-fixture runtime."""

    source_fingerprint: str
    raw_index_sha256: str
    scan_version: int
    index_scan_root: str

    @classmethod
    def from_mapping(cls, coordinate: Mapping[str, Any]) -> "FixtureRuntimeCoordinate":
        """Parse one complete fixture runtime coordinate without accepting extra fields."""
        expected = {"source_fingerprint", "raw_index_sha256", "scan_version", "index_scan_root"}
        if set(coordinate) != expected:
            raise ValueError("fixture runtime coordinate fields drifted")
        source_fingerprint = coordinate["source_fingerprint"]
        raw_index_sha256 = coordinate["raw_index_sha256"]
        scan_version = coordinate["scan_version"]
        index_scan_root = coordinate["index_scan_root"]
        if (
            not isinstance(source_fingerprint, str)
            or _SHA256.fullmatch(source_fingerprint) is None
            or not isinstance(raw_index_sha256, str)
            or _SHA256.fullmatch(raw_index_sha256) is None
            or type(scan_version) is not int
            or scan_version < 1
            or not isinstance(index_scan_root, str)
            or not index_scan_root
        ):
            raise ValueError("fixture runtime coordinate is malformed")
        return cls(source_fingerprint, raw_index_sha256, scan_version, index_scan_root)


def validate_fixture_runtime(
    source_root: Path, index_path: Path, coordinate: Mapping[str, Any]
) -> FixtureRuntimeCoordinate:
    """Fail closed unless one copied source tree and local index match their immutable coordinate."""
    admitted = FixtureRuntimeCoordinate.from_mapping(coordinate)
    source_root = Path(source_root).resolve(strict=True)
    index_path = Path(index_path).resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError("fixture runtime source root is not a directory")
    expected_index_path = source_root / ".cache" / "codemap" / f"{source_root.name}.json"
    if index_path != expected_index_path:
        raise ValueError("fixture runtime index must use the copied source resolver path")
    contracts.verify_source_fingerprint(source_root, admitted.source_fingerprint)
    index_bytes = index_path.read_bytes()
    if hashlib.sha256(index_bytes).hexdigest() != admitted.raw_index_sha256:
        raise ValueError("fixture runtime index bytes drifted")
    try:
        metadata = json.loads(index_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("fixture runtime index is malformed") from exc
    if not isinstance(metadata, Mapping):
        raise ValueError("fixture runtime index is malformed")
    if metadata.get("scan_version") != admitted.scan_version:
        raise ValueError("fixture runtime scan version drifted")
    if metadata.get("scan_root") != admitted.index_scan_root or admitted.index_scan_root != str(source_root):
        raise ValueError("fixture runtime index scan root drifted")
    return admitted


class FixtureCodexRuntime:
    """Run unscored copied-fixture prompts through structural Codex isolation and transport."""

    def __init__(
        self,
        *,
        runner: Any,
        native: Any,
        source_root: Path,
        index_path: Path,
        coordinate: Mapping[str, Any],
        arm_envelope: Callable[[str], str],
    ) -> None:
        """Bind one structural runner to a single already-validated fixture coordinate."""
        self.runner = runner
        self.native = native
        self.source_root = Path(source_root)
        self.index_path = Path(index_path)
        self.coordinate = dict(coordinate)
        self.arm_envelope = arm_envelope
        validate_fixture_runtime(self.source_root, self.index_path, self.coordinate)

    def probe(self, arms: tuple[str, ...]) -> None:
        """Exercise every isolated treatment home without invoking a model."""
        for arm in arms:
            home = self._prepare_home(arm)
            try:
                self.native.probe_arm_home(home)
            finally:
                self._cleanup_home(home)

    def run_cell(self, task: Mapping[str, Any], arm: str, prompt: str) -> dict[str, Any]:
        """Run one fixture prompt and return only native transport and treatment evidence."""
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("fixture task requires a non-empty id")
        home = self._prepare_home(arm)
        started = time.monotonic()
        attempts: list[list[dict[str, Any]]] = []
        parsed = self.native.runtime.CodexParseResult()
        contamination = ""
        cleanup_error = ""
        try:
            command = self.runner.build_command(f"{self.arm_envelope(arm)}\n\n{prompt}")
            for attempt in range(3):
                remaining = self.runner.timeout - (time.monotonic() - started)
                if remaining <= 0:
                    parsed = self.native.runtime.CodexParseResult(
                        incomplete=True, error="fixture cell timeout", error_type="cell_timeout"
                    )
                    break
                parsed = self.native.runtime.parse_codex_jsonl(
                    self.runner.run_stream(command, home.env, timeout=remaining),
                    launcher_path=home.codemap_launcher_path,
                    allow_equivalent_launcher=True,
                    skill_path=home.codemap_skill_path,
                    skill_sha256=home.codemap_skill_sha256,
                )
                attempts.append(parsed.raw_events)
                try:
                    validate_fixture_runtime(self.source_root, self.index_path, self.coordinate)
                    if home.coordination_path is not None:
                        self.native._assert_coordination_root_idle(home.coordination_path)
                except ValueError as exc:
                    contamination = str(exc)
                    break
                retryable_empty = (
                    parsed.input_tokens == 0
                    and parsed.output_tokens == 0
                    and not parsed.output_text.strip()
                    and parsed.retryable
                )
                if not retryable_empty or attempt == 2:
                    break
        finally:
            try:
                self._cleanup_home(home)
            except ValueError as exc:
                cleanup_error = str(exc)
        codemap_compliance = self.native._arm_compliance(arm, parsed)
        contaminated = bool(contamination) or (arm == "A_plain" and parsed.codemap_observed_calls > 0)
        incomplete = parsed.incomplete or bool(contamination or cleanup_error)
        error = parsed.error
        error_type = parsed.error_type
        if contamination:
            error, error_type = f"runtime contamination: {contamination}", "runtime_contamination"
        elif cleanup_error:
            error, error_type = f"runner cleanup failed: {cleanup_error}", "cleanup_failed"
        elif contaminated and not error:
            error, error_type = "contaminated", "contaminated"
        return {
            "task_id": task_id,
            "arm": arm,
            "success": parsed.success and not contaminated and not incomplete,
            "incomplete": incomplete,
            "contaminated": contaminated,
            "report_text": parsed.output_text[parsed.last_tool_text_offset :].lstrip("\n"),
            "raw_events": parsed.raw_events,
            "input_tokens": parsed.input_tokens,
            "cached_input_tokens": parsed.cached_input_tokens,
            "output_tokens": parsed.output_tokens,
            "usage_complete": self._usage_complete(parsed, attempts),
            "elapsed_s": time.monotonic() - started,
            "command_calls": parsed.command_calls,
            "codemap_calls": parsed.codemap_calls,
            "codemap_used": parsed.codemap_observed_calls > 0,
            "treatment_adherence": treatment_adherence(
                arm, codemap_use_compliance=codemap_compliance, contaminated=contaminated
            ),
            "error": error,
            "error_type": error_type,
            "launcher_path": str(home.codemap_launcher_path) if home.codemap_launcher_path else None,
        }

    def close(self) -> None:
        """Release the runner's private credential chain after the stage closes."""
        self.runner.close()

    def _prepare_home(self, arm: str) -> Any:
        """Create one structural home only after rechecking the copied fixture coordinate."""
        validate_fixture_runtime(self.source_root, self.index_path, self.coordinate)
        return self.runner._prepare_verified_home(arm, fixture_runtime_coordinate=self.coordinate)

    def _cleanup_home(self, home: Any) -> None:
        """Discard permitted coordination scratch before deleting the disposable home."""
        try:
            if home.coordination_path is not None:
                self.native._cleanup_coordination_root(home.coordination_path)
        finally:
            home.cleanup()

    @staticmethod
    def _usage_complete(parsed: Any, attempts: list[list[dict[str, Any]]]) -> bool:
        """Report usage only when the final native turn is complete and there was no retry."""
        return (
            len(attempts) <= 1
            and parsed.completed
            and not parsed.malformed_usage
            and all(
                type(value) is int and value >= 0
                for value in (
                    parsed.raw_usage.get("input_tokens"),
                    parsed.raw_usage.get("cached_input_tokens", parsed.raw_usage.get("cache_read_input_tokens")),
                    parsed.raw_usage.get("output_tokens"),
                )
            )
        )
