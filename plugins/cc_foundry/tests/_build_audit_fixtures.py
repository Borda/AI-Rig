"""Generator for the audit-log fixture corpus under ``fixtures/audit/``.

Two artifacts come out of this module:

``canonical_vectors.json``
    Record objects paired with the exact canonical bytes and SHA-256 the specification requires. Both the JavaScript
    writer and the Python verifier execute these, which is what keeps two languages agreeing byte-for-byte on a hash
    neither of them may compute its own way.

``logs/*.jsonl`` plus ``expectations.json``
    One log file per classification the verifier must reach, and the buckets each file is expected to produce. Written
    before the verifier exists on purpose: a corpus derived from a finished implementation only proves the
    implementation reproduces itself.

Canonicalization here is written out longhand from the specification rather than imported from the verifier, so a bug
in the verifier's own canonicalization cannot make these fixtures agree with it.

Usage:
    python plugins/cc_foundry/tests/_build_audit_fixtures.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = _TESTS_DIR / "fixtures" / "audit"

PLUGINS = ("cc_foundry", "cc_oss", "cc_develop", "cc_research")
VERSIONS = {"cc_foundry": "0.53.0", "cc_oss": "0.34.2", "cc_develop": "0.28.1", "cc_research": "0.21.1"}
PROJECT = "/home/example/workspace/demo"
SESSION = "11111111-2222-3333-4444-555555555555"
DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SRC = "skills/review/SKILL.md:12"

#: Fixed clock. Timestamps must be reproducible, so nothing here reads the wall clock.
_T0 = "2026-09-09T10:15:30.000Z"


def canonical(record: dict) -> str:
    """Return the canonical serialization of ``record`` with ``record_hash`` omitted.

    Longhand from the specification: UTF-8, no whitespace, keys sorted, non-ASCII left unescaped, integers only.

    Examples:
        >>> canonical({"b": 1, "a": None, "record_hash": "x"})
        '{"a":null,"b":1}'
    """
    body = {key: value for key, value in record.items() if key != "record_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(record: dict) -> str:
    """Return the SHA-256 hex of the record's canonical bytes."""
    return hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()


def sealed(record: dict) -> dict:
    """Return ``record`` with its ``record_hash`` filled in."""
    return {**record, "record_hash": record_hash(record)}


def _rid(tag: str) -> str:
    """Return a stable synthetic record id — a UUID-shaped string derived from ``tag``, never a random one."""
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-4{digest[13:16]}-a{digest[17:20]}-{digest[20:32]}"


def _base(tag: str, plugin: str, hook: str, phase: str, action_type: str) -> dict:
    """Return the fields every row carries, in the order the schema table lists them."""
    return {
        "record_id": _rid(tag),
        "timestamp": _T0,
        "agent_id": f"{plugin}/{hook}",
        "agent_version": VERSIONS[plugin],
        "session_id": SESSION,
        "project": PROJECT,
        "action_type": action_type,
        "outcome": "pending",
        "trust_level": "unknown",
        "parent_record_id": None,
        "prev_hash": None,
        "record_phase": phase,
    }


def before_row(
    tag: str,
    plugin: str,
    tool_use_id: str | None,
    *,
    blueprint: str = "passthrough",
    shape: str = "passthrough",
    session_id: str | None = SESSION,
) -> dict:
    """Return one ``pre_execution`` row for ``plugin``, with both lane verdicts spelled out.

    ``blueprint`` and ``shape`` each take ``allow``, ``passthrough``, ``none`` or ``module-error``; the effective
    verdict follows rank order, so a blueprint allow wins over a shape allow.
    """
    verdicts = [
        _verdict("blueprint", blueprint),
        _verdict("shape", shape),
    ]
    # The digest exists only when the blueprint lane actually normalized the command, i.e. it reached a decision.
    blueprint_decided = blueprint in ("allow", "passthrough")
    if blueprint == "allow":
        detail = {"decision": "allow", "lane": "blueprint", "rank": 1, "src": SRC, "digest": DIGEST}
    elif shape == "allow":
        detail = {"decision": "allow", "lane": "shape", "rank": 2}
        if blueprint_decided:
            detail["digest"] = DIGEST
    else:
        detail = {"decision": "passthrough" if "passthrough" in (blueprint, shape) else "none", "lane": "none"}
        if blueprint_decided:
            detail["digest"] = DIGEST
    detail["verdicts"] = verdicts
    row = _base(tag, plugin, "allow-dispatch", "pre_execution", "tool.bash")
    row["session_id"] = session_id
    row["tool_use_id"] = tool_use_id
    row["action_detail"] = detail
    row["trust_level"] = "plugin" if detail["decision"] == "allow" else "unknown"
    return sealed(row)


def _verdict(lane: str, decision: str) -> dict:
    """Return one entry of the two-element ``verdicts`` array."""
    if decision == "allow":
        entry = {"lane": lane, "decision": "allow"}
        if lane == "blueprint":
            entry["src"] = SRC
        return entry
    if decision == "module-error":
        return {"lane": lane, "decision": "none", "why": "module-error"}
    if decision == "none":
        return {"lane": lane, "decision": "none", "why": "not-applicable"}
    return {"lane": lane, "decision": "passthrough", "why": "no-match" if lane == "blueprint" else "shape-mismatch"}


def after_row(
    tag: str,
    plugin: str,
    tool_use_id: str | None,
    *,
    status: str = "ok",
    session_id: str | None = SESSION,
) -> dict:
    """Return one ``post_execution`` row observing that the tool call completed."""
    row = _base(tag, plugin, "audit-close", "post_execution", "tool.bash")
    row["session_id"] = session_id
    row["tool_use_id"] = tool_use_id
    row["action_detail"] = {"status": status, "event": "PostToolUse" if status == "ok" else "PostToolUseFailure"}
    row["outcome"] = "success" if status == "ok" else "failure"
    return sealed(row)


def lifecycle_row(tag: str, plugin: str, action_type: str, *, reason: str | None = None) -> dict:
    """Return one ``session.start`` or ``session.end`` row.

    Lifecycle rows carry no ``tool_use_id``.
    """
    row = _base(tag, plugin, "audit-close", "post_execution", action_type)
    row["action_detail"] = {} if reason is None else {"reason": reason}
    row["outcome"] = "success"
    return sealed(row)


def _write(path: Path, rows: list[dict], *, truncate_last: bool = False, torn_at: int | None = None) -> None:
    """Write ``rows`` as JSONL, optionally reproducing a torn line the way a concurrent append would leave one."""
    lines = [json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for row in rows]
    if torn_at is not None:
        lines[torn_at] = lines[torn_at][: len(lines[torn_at]) // 2]
    text = "\n".join(lines) + ("" if truncate_last else "\n")
    if truncate_last:
        text = text[: -len(lines[-1]) // 2]
    path.write_text(text, encoding="utf-8")


def build(fixture_dir: Path) -> dict:  # noqa: PLR0915  (a flat catalogue of cases; splitting it hides the corpus)
    """Write the whole corpus and return the expectations map."""
    logs = fixture_dir / "logs"
    if logs.exists():
        shutil.rmtree(logs)
    logs.mkdir(parents=True)

    expectations: dict[str, dict] = {}

    def case(name: str, rows: list[dict], expect: dict, **write_kwargs) -> None:
        """Emit one log file and record what the verifier must say about it."""
        _write(logs / f"{name}.jsonl", rows, **write_kwargs)
        expectations[f"{name}.jsonl"] = expect

    tid = "toolu_01aaaaaaaaaaaaaaaaaaaaaa"
    tid2 = "toolu_02bbbbbbbbbbbbbbbbbbbbbb"

    case(
        "clean-single-plugin",
        [
            lifecycle_row("s1-start", "cc_foundry", "session.start"),
            before_row("s1-before", "cc_foundry", tid, blueprint="allow"),
            after_row("s1-after", "cc_foundry", tid),
            lifecycle_row("s1-end", "cc_foundry", "session.end", reason="clear"),
        ],
        {"exit": 0, "buckets": {}},
    )

    case(
        "clean-four-plugin",
        [
            *(lifecycle_row(f"s4-start-{p}", p, "session.start") for p in PLUGINS),
            before_row("s4-before-foundry", "cc_foundry", tid, blueprint="allow"),
            *(before_row(f"s4-before-{p}", p, tid) for p in PLUGINS[1:]),
            *(after_row(f"s4-after-{p}", p, tid) for p in PLUGINS),
            *(lifecycle_row(f"s4-end-{p}", p, "session.end") for p in PLUGINS),
        ],
        {"exit": 0, "buckets": {}},
    )

    case(
        "incomplete-closure",
        [
            # Four plugins decide; only one reports the completion. Every other group in the file closes four ways,
            # which is what makes the single closer visible as a subset rather than as the file's normal shape.
            *(before_row(f"ic-before-{p}", p, tid) for p in PLUGINS),
            after_row("ic-after-foundry", "cc_foundry", tid),
            *(before_row(f"ic2-before-{p}", p, tid2) for p in PLUGINS),
            *(after_row(f"ic2-after-{p}", p, tid2) for p in PLUGINS),
            *(lifecycle_row(f"ic-end-{p}", p, "session.end") for p in PLUGINS),
        ],
        {"exit": 0, "buckets": {"incomplete-closure": 1, "observed-abstention": 2}},
    )

    case(
        "four-plugin-shape-allow",
        [
            # Every plugin ships the same shape module, so a shape match allows in all four. This is the population
            # the shape hook actually serves, and `multi-allow` is its normal state rather than an anomaly.
            *(before_row(f"sa-before-{p}", p, tid, shape="allow") for p in PLUGINS),
            *(after_row(f"sa-after-{p}", p, tid) for p in PLUGINS),
            *(lifecycle_row(f"sa-end-{p}", p, "session.end") for p in PLUGINS),
        ],
        {"exit": 0, "buckets": {"multi-allow": 1}},
    )

    case(
        "multi-allow",
        [
            before_row("ma-foundry", "cc_foundry", tid, blueprint="allow"),
            before_row("ma-oss", "cc_oss", tid, blueprint="allow"),
            before_row("ma-develop", "cc_develop", tid, shape="allow"),
            before_row("ma-research", "cc_research", tid),
            *(after_row(f"ma-after-{p}", p, tid) for p in PLUGINS),
            *(lifecycle_row(f"ma-end-{p}", p, "session.end") for p in PLUGINS),
        ],
        {"exit": 0, "buckets": {"multi-allow": 1}},
    )

    case(
        "observed-abstention",
        [
            *(before_row(f"oa-{p}", p, tid) for p in PLUGINS),
            *(after_row(f"oa-after-{p}", p, tid) for p in PLUGINS),
            *(lifecycle_row(f"oa-end-{p}", p, "session.end") for p in PLUGINS),
        ],
        {"exit": 0, "buckets": {"observed-abstention": 1}},
    )

    case(
        "incomplete-evidence",
        [
            *(
                before_row(f"ie-full-{p}", p, tid, blueprint="allow" if p == "cc_foundry" else "passthrough")
                for p in PLUGINS
            ),
            *(after_row(f"ie-full-after-{p}", p, tid) for p in PLUGINS),
            before_row("ie-part-foundry", "cc_foundry", tid2, blueprint="allow"),
            before_row("ie-part-oss", "cc_oss", tid2),
            *(after_row(f"ie-part-after-{p}", p, tid2) for p in PLUGINS[:2]),
            *(lifecycle_row(f"ie-end-{p}", p, "session.end") for p in PLUGINS),
        ],
        # The partial group is short two writers AND two closers, so both completeness checks fire on it. That is the
        # symmetry working, not double-counting: each side reports its own missing observers.
        {"exit": 0, "buckets": {"incomplete-evidence": 1, "incomplete-closure": 1}},
    )

    case(
        "no-decision-evidence",
        [
            *(after_row(f"nde-{p}", p, tid) for p in PLUGINS),
            *(lifecycle_row(f"nde-end-{p}", p, "session.end") for p in PLUGINS),
        ],
        {"exit": 0, "buckets": {"no-decision-evidence": 1}},
    )

    case(
        "repeat-observation",
        [
            before_row("ro-first", "cc_foundry", tid, blueprint="allow"),
            before_row("ro-second", "cc_foundry", tid, blueprint="allow"),
            after_row("ro-after", "cc_foundry", tid),
            lifecycle_row("ro-end", "cc_foundry", "session.end"),
        ],
        {"exit": 0, "buckets": {"repeat-observation": 1}},
    )

    case(
        "unjoinable-tool-use-id",
        [
            before_row("uj-t-before", "cc_foundry", None, blueprint="allow"),
            after_row("uj-t-after", "cc_foundry", None),
            lifecycle_row("uj-t-end", "cc_foundry", "session.end"),
        ],
        {"exit": 0, "buckets": {"unjoinable": 2}},
    )

    case(
        "dual-close",
        [
            before_row("dc-before", "cc_foundry", tid, blueprint="allow"),
            after_row("dc-ok", "cc_foundry", tid),
            after_row("dc-fail", "cc_oss", tid, status="error"),
            lifecycle_row("dc-end", "cc_foundry", "session.end"),
        ],
        {"exit": 0, "buckets": {"dual-close": 1, "incomplete-evidence": 0}},
    )

    case(
        "same-agent-duplicate-after",
        [
            before_row("sad-before", "cc_foundry", tid, blueprint="allow"),
            after_row("sad-after-1", "cc_foundry", tid),
            after_row("sad-after-2", "cc_foundry", tid),
            lifecycle_row("sad-end", "cc_foundry", "session.end"),
        ],
        {"exit": 0, "buckets": {"same-agent-duplicate-after-row": 1}},
    )

    case(
        "verdicts-mixed-and-module-error",
        [
            before_row("vm-mixed", "cc_foundry", tid, blueprint="passthrough", shape="none"),
            before_row("vm-error", "cc_oss", tid, blueprint="module-error", shape="passthrough"),
            *(after_row(f"vm-after-{p}", p, tid) for p in PLUGINS[:2]),
            lifecycle_row("vm-end", "cc_foundry", "session.end"),
        ],
        {
            "exit": 0,
            "buckets": {"observed-abstention": 1},
            "lane_counts": {"blueprint": {"passthrough": 1, "none": 1}, "shape": {"passthrough": 1, "none": 1}},
        },
    )

    corrupted = before_row("corrupt", "cc_foundry", tid, blueprint="allow")
    corrupted = {**corrupted, "project": "/home/example/workspace/other"}
    case(
        "corrupt-hash",
        [
            corrupted,
            after_row("corrupt-after", "cc_foundry", tid),
            lifecycle_row("corrupt-end", "cc_foundry", "session.end"),
        ],
        # Dropping the corrupted before-row leaves the group with after-rows only, which is exactly what a lost
        # record looks like from the reader's side.
        {"exit": 1, "buckets": {"corrupt-record": 1, "no-decision-evidence": 1}},
    )

    case(
        "truncated-final-line",
        [
            before_row("tf-before", "cc_foundry", tid, blueprint="allow"),
            after_row("tf-after", "cc_foundry", tid),
            lifecycle_row("tf-end", "cc_foundry", "session.end"),
        ],
        {"exit": 0, "buckets": {"truncated": 1}},
        truncate_last=True,
    )

    case(
        "truncated-mid-file",
        [
            before_row("tm-before", "cc_foundry", tid, blueprint="allow"),
            after_row("tm-after", "cc_foundry", tid),
            lifecycle_row("tm-end", "cc_foundry", "session.end"),
        ],
        # The torn line is the after-row, so the before-row it would have closed is left unobserved.
        {"exit": 0, "buckets": {"truncated": 1, "completion-unobserved": 1}},
        torn_at=1,
    )

    # Every row below is re-sealed after mutation. A schema-invalid row must carry a VALID hash, otherwise the case
    # would also be a corrupt-record and would force exit 1 — testing the wrong bucket and the wrong exit code.
    good = before_row("si-good", "cc_foundry", tid, blueprint="allow")
    missing_verdicts = {k: v for k, v in good.items()}
    missing_verdicts["action_detail"] = {k: v for k, v in good["action_detail"].items() if k != "verdicts"}
    missing_verdicts = sealed(missing_verdicts)
    verdicts_on_after = after_row("si-after-verdicts", "cc_oss", tid)
    verdicts_on_after["action_detail"] = {
        **verdicts_on_after["action_detail"],
        "verdicts": good["action_detail"]["verdicts"],
    }
    verdicts_on_after = sealed(verdicts_on_after)
    duplicate_lanes = before_row("si-dup-lane", "cc_develop", tid)
    duplicate_lanes["action_detail"] = {
        **duplicate_lanes["action_detail"],
        "verdicts": [_verdict("blueprint", "passthrough"), _verdict("blueprint", "passthrough")],
    }
    duplicate_lanes = sealed(duplicate_lanes)
    unknown_outcome = sealed({**after_row("si-outcome", "cc_research", tid), "outcome": "escalated"})
    case(
        "schema-invalid",
        [
            good,
            missing_verdicts,
            verdicts_on_after,
            duplicate_lanes,
            unknown_outcome,
            lifecycle_row("si-end", "cc_foundry", "session.end"),
        ],
        # The four non-conforming rows are excluded from grouping, so the one conforming before-row is left with no
        # observation closing it.
        {"exit": 0, "buckets": {"schema-invalid": 4, "completion-unobserved": 1}},
    )

    case(
        "completion-unobserved",
        [
            before_row("cu-before", "cc_foundry", tid, blueprint="allow"),
            lifecycle_row("cu-end", "cc_foundry", "session.end"),
        ],
        {"exit": 0, "buckets": {"completion-unobserved": 1}},
    )

    case(
        "in-flight-or-hard-kill",
        [
            lifecycle_row("ih-start", "cc_foundry", "session.start"),
            before_row("ih-before", "cc_foundry", tid, blueprint="allow"),
        ],
        {"exit": 0, "buckets": {"in-flight-or-hard-kill": 1}},
    )

    case(
        "interleaved-lifecycle",
        [
            lifecycle_row("il-start-foundry", "cc_foundry", "session.start"),
            lifecycle_row("il-start-oss", "cc_oss", "session.start"),
            before_row("il-before-foundry", "cc_foundry", tid, blueprint="allow"),
            lifecycle_row("il-start-develop", "cc_develop", "session.start"),
            before_row("il-before-oss", "cc_oss", tid),
            lifecycle_row("il-start-research", "cc_research", "session.start"),
            *(before_row(f"il-before-{p}", p, tid) for p in PLUGINS[2:]),
            *(after_row(f"il-after-{p}", p, tid) for p in PLUGINS),
            *(lifecycle_row(f"il-end-{p}", p, "session.end") for p in PLUGINS),
        ],
        {"exit": 0, "buckets": {}},
    )

    case(
        "_no-session",
        [
            before_row("ns-before", "cc_foundry", tid, blueprint="allow", session_id=None),
            after_row("ns-after", "cc_foundry", tid, session_id=None),
        ],
        {"exit": 0, "buckets": {"unjoinable": 2}},
    )

    (fixture_dir / "expectations.json").write_text(
        json.dumps(expectations, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_canonical_vectors(fixture_dir)
    return expectations


def _write_canonical_vectors(fixture_dir: Path) -> None:
    """Write the cross-language canonicalization vectors."""
    samples = [
        ("minimal", {"a": 1, "b": None}),
        ("key-order", {"z": 1, "a": 2, "m": 3}),
        ("null-explicit-vs-absent", {"parent_record_id": None, "prev_hash": None}),
        ("nested-object", {"action_detail": {"lane": "blueprint", "decision": "allow", "rank": 1}}),
        (
            "nested-array",
            {"verdicts": [{"lane": "blueprint", "decision": "allow"}, {"lane": "shape", "decision": "none"}]},
        ),
        ("non-ascii-unescaped", {"project": "/home/exämple/ünïcode/日本語"}),
        ("emoji-above-bmp", {"project": "/home/example/🚀/repo"}),
        ("solidus-and-quotes", {"src": 'a/b"c\\d'}),
        ("control-characters", {"src": "line\nnext\ttab"}),
        ("integer-bounds", {"lo": -9007199254740991, "hi": 9007199254740991}),
        ("empty-object-and-array", {"action_detail": {}, "verdicts": []}),
        ("record-hash-excluded", {"a": 1, "record_hash": "ffff"}),
        (
            "full-before-row",
            before_row("vector-before", "cc_foundry", "toolu_01vector", blueprint="allow"),
        ),
        ("full-after-row", after_row("vector-after", "cc_oss", "toolu_01vector")),
        ("full-lifecycle-row", lifecycle_row("vector-lifecycle", "cc_develop", "session.end", reason="clear")),
    ]
    vectors = [
        {"id": name, "record": record, "canonical": canonical(record), "sha256": record_hash(record)}
        for name, record in samples
    ]
    payload = {
        "_comment": (
            "Cross-language canonicalization contract. Consumed by the JavaScript audit library's suite and by the "
            "Python verifier's suite; both must reproduce `canonical` byte-for-byte and `sha256` exactly. `record_hash` "
            "is excluded from its own input, which the record-hash-excluded vector pins."
        ),
        "vectors": vectors,
    }
    (fixture_dir / "canonical_vectors.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """Build the corpus and report what was written.

    Examples:
        >>> isinstance(main.__doc__, str)
        True
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    expectations = build(args.out)
    print(f"wrote {len(expectations)} log fixtures and the canonical vectors under {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
