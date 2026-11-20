#!/usr/bin/env python
"""dev_scope_findings.py — validate refactor scope findings and prove every special case is pinned by a passing test.

``/develop:refactor`` Step 1 writes every labelled scope finding to ``<run-dir>/scope-findings.md``; Step 3 maps each
special case to characterization tests in ``<run-dir>/special-case-tests.md``. This helper checks both files and runs
the mapped tests, so every reader fails closed: a missing, empty or malformed file is never read as "no special cases".

Findings file, one line per finding (or the single line ``none · 0 findings``)::

    F<n> · special-case|coupling|smell|hotspot · <anchor> · <claim>

Map file, one line per mapped test::

    F<n> · <test-path>::<test-name>

A special case is pinned only when every mapped node reports a pass in the JUnit report (``xunit1``, whose ``file``
attribute maps each case back to its node id) and no mapped node appears as ``XPASS`` in pytest's ``-rX`` summary.
Strict xfail turns an unmarked xpass into a failure; the summary also catches an explicit ``strict=False`` marker,
static or applied at run time. Skips, xfails, xpasses, failures, errors and unreported nodes never pin.

``pin`` persists its verdict in ``<run-dir>/special-case-verdict.json``: the SHA-256 of the findings file and of the
map, and the unpinned labels with the nodes that did not pass. ``accept-unpinned`` copies exactly that verdict's
unpinned labels and nodes into ``<run-dir>/accepted-unpinned.json``, with the same hashes, and never re-runs tests.
``verify`` is the Step 4 entry check. ``reset`` deletes map, verdict, acceptance and reports; Step 1 and the first
Step 3 entry call it. A characterization run (not this helper) that ends with pytest exit 2-4 keeps its earlier
behaviour: the gate reports it as a failed characterization run.

States and routes (the gates route on the printed ``GATE FAIL`` / ``! GATE ERROR`` / ``! BLOCKED`` line):

==========================================  =========  ==============  ==========================================
state                                       exit       printed         route
==========================================  =========  ==============  ==========================================
findings missing, empty or malformed        1          ``! BLOCKED``   re-run Step 1; no acceptance
map row malformed, unsafe, stale label or   1          ``! BLOCKED``   fix the map (Step 3); no acceptance
missing test file
``pin``: every non-accepted case pinned     0          ``PIN OK``      verdict written, continue
``pin``: unmapped, or a node not passed,    3          ``GATE FAIL``   verdict written; fix, accept or abort
not reported or xpassed
``pin``: no verdict (pytest 2-5, report     6          ``GATE ERROR``  no verdict written; fix the run
missing or invalid, duplicate or foreign
report ids)
``pin``: runner refused or faulted          its exit   ``GATE ERROR``  no verdict written; recover the runner
``accept-unpinned``: verdict missing or     6          ``GATE ERROR``  re-run the Step 3 gate; nothing accepted
stale
``accept-unpinned``: current verdict        0          ``ACCEPTED``    records exactly the verdict's labels
``verify``: no special cases                0          ``VERIFY OK``   continue
``verify``: verdict missing or stale        1          ``! BLOCKED``   re-run the Step 3 gate
``verify``: current verdict, PIN OK         0          ``VERIFY OK``   continue
``verify``: current verdict, unpinned and   0          ``VERIFY OK``   continue; accepted nodes left out
fully covered by a current acceptance
``verify``: current verdict, unpinned not   1          ``! BLOCKED``   re-run the Step 3 gate or accept
covered by a current acceptance
stale acceptance (hashes differ)            —          ``STALE``       ignored; every label enforced
==========================================  =========  ==============  ==========================================

Usage:
    python dev_scope_findings.py reset|check|pin|accept-unpinned|verify <run-dir>
    python dev_scope_findings.py baseline-filter <run-dir> <selection.json> <out.json> [<accepted-red-file>]

``check`` validates the findings file. ``baseline-filter`` copies a pre-edit baseline selection without the accepted
red characterization file and without accepted nodes that no other special case maps, collecting the other tests of
a file that holds one; a file it cannot enumerate blocks. Files the shell reads back use LF line endings on every OS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

FINDINGS_NAME = "scope-findings.md"
MAP_NAME = "special-case-tests.md"
VERDICT_NAME = "special-case-verdict.json"
ACCEPTED_NAME = "accepted-unpinned.json"
JUNIT_NAME = "special-case-junit.xml"
NONE_LINE = "none · 0 findings"
EXIT_BLOCKED = 1
EXIT_PIN_FAILED = 3
EXIT_NO_VERDICT = 6

#: One finding: label, class, space-free anchor, non-empty claim, separated by `` · ``.
_FINDING_RE = re.compile(
    r"^F(?P<n>[1-9][0-9]*) · (?P<cls>special-case|coupling|smell|hotspot) · (?P<anchor>\S+) · \S.*$"
)
#: One map row: label and a ``path::test`` node id without spaces.
_MAP_RE = re.compile(r"^F(?P<n>[1-9][0-9]*) · (?P<node>\S+::\S+)$")
#: A Windows drive prefix (``C:`` or ``C:\\``), which makes a path absolute or drive-relative there.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
#: Pytest exit codes that still carry a per-test verdict: all passed, or some tests failed.
_VERDICT_CODES = frozenset({0, 1})
#: Pytest's own exit codes (0-5); anything else came from the runner wrapper, not from pytest.
_PYTEST_CODES = frozenset(range(6))
#: Why pytest stopped without a verdict, named for the gate message.
_PYTEST_FAULTS = {2: "pytest interrupted", 3: "pytest internal error", 4: "pytest usage error", 5: "no tests collected"}


class ScopeError(Exception):
    """Raised with the reason a findings, map or verdict file cannot be trusted (state ``!

    BLOCKED``).
    """


class NoVerdict(Exception):
    """Raised when pytest or its runner produced no per-test verdict (state ``GATE ERROR``)."""

    def __init__(self, cause: str, code: int) -> None:
        """Keep the cause text and the exit code the gate reports."""
        super().__init__(cause)
        self.code = code


def _lines(path: Path) -> list[str]:
    """Return the non-blank lines of ``path``, stripped.

    Raises:
        ScopeError: When the file is missing or unreadable.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScopeError(f"{path} missing or unreadable") from exc
    return [line.strip() for line in text.splitlines() if line.strip()]


def _write_lf(path: Path, text: str) -> None:
    """Write ``text`` with LF line endings on every OS, so a shell ``read -r`` never sees a trailing CR."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _digest(path: Path) -> str:
    """Return the SHA-256 of a file's bytes, or of nothing when it does not exist."""
    return hashlib.sha256(path.read_bytes() if path.is_file() else b"").hexdigest()


def _hashes(run_dir: Path) -> dict[str, str]:
    """Return the current findings and map digests a verdict or acceptance is bound to."""
    return {"findings_sha256": _digest(run_dir / FINDINGS_NAME), "map_sha256": _digest(run_dir / MAP_NAME)}


def _label_key(label: str) -> int:
    """Sort key for ``F<n>`` labels.

    Examples:
        >>> sorted(["F10", "F2"], key=_label_key)
        ['F2', 'F10']
    """
    return int(label[1:])


def parse_findings(path: Path) -> dict[str, str]:
    """Validate the findings file and return ``{label: class}``.

    Args:
        path: ``scope-findings.md`` path.

    Returns:
        Mapping of label (``F1``) to finding class; empty for the explicit no-findings line.

    Raises:
        ScopeError: On a missing or empty file, a malformed line, a duplicate label, or a special case without a symbol
            anchor.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     p = Path(d) / "f.md"
        ...     text = "F1 · special-case · a.py::f · retry\\nF2 · smell · a.py::g · long\\n"
        ...     _ = p.write_text(text, encoding="utf-8")
        ...     parse_findings(p)
        {'F1': 'special-case', 'F2': 'smell'}
    """
    lines = _lines(path)
    if not lines:
        raise ScopeError(f"{path} is empty; state `{NONE_LINE}` when Step 1 found nothing")
    if lines == [NONE_LINE]:
        return {}
    found: dict[str, str] = {}
    for line in lines:
        match = _FINDING_RE.match(line)
        if not match:
            raise ScopeError(f"malformed finding line: {line!r}")
        label = f"F{match['n']}"
        if label in found:
            raise ScopeError(f"duplicate label {label}")
        if match["cls"] == "special-case" and "::" not in match["anchor"]:
            raise ScopeError(f"{label} special case needs a path::symbol anchor, got {match['anchor']!r}")
        found[label] = match["cls"]
    return found


def special_cases(findings: dict[str, str]) -> list[str]:
    """Return special-case labels in numeric order.

    Examples:
        >>> special_cases({"F10": "special-case", "F2": "smell", "F3": "special-case"})
        ['F3', 'F10']
    """
    return sorted((label for label, cls in findings.items() if cls == "special-case"), key=_label_key)


def normalize_node(node: str) -> str:
    """Drop leading ``./`` segments and use ``/`` separators, as pytest reports the path part of a node id.

    Examples:
        >>> normalize_node("./tests/test_a.py::test_x"), normalize_node(".\\\\tests\\\\t.py::x")
        ('tests/test_a.py::test_x', 'tests/t.py::x')
    """
    file_part, sep, rest = node.partition("::")
    file_part = file_part.replace("\\", "/")
    while file_part.startswith("./"):
        file_part = file_part[2:]
    return file_part + sep + rest


def _safe_node(node: str, root: Path | None = None) -> bool:
    """Tell whether a node id names a ``.py`` file inside ``root`` without option or drive tricks.

    Mirrors the test runner's own target rule: no ``-``/``@`` prefix, no Windows drive prefix, and the path, symlinks
    resolved, stays inside the project root.

    Examples:
        >>> [_safe_node(n) for n in ("tests/test_a.py::t", "-k::x", "@args::x", "../t.py::x", "/t.py::x", "C:t.py::x")]
        [True, False, False, False, False, False]
    """
    file_part = node.split("::", 1)[0]
    if node.startswith(("-", "@")) or _DRIVE_RE.match(file_part) or not file_part.endswith(".py"):
        return False
    pure = PurePosixPath(file_part.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        return False
    base = (root or Path.cwd()).resolve()
    return (base / file_part).resolve().is_relative_to(base)


def read_map(path: Path, specials: list[str]) -> dict[str, list[str]]:
    """Validate every map row and return ``{label: [normalized node, ...]}``.

    Raises:
        ScopeError: On a malformed row, an unsafe or missing test file, or a label that is not a current special case.
    """
    mapped: dict[str, list[str]] = {}
    for row in _lines(path) if path.exists() else []:
        match = _MAP_RE.match(row)
        if not match:
            raise ScopeError(f"malformed map row: {row!r}")
        label, node = f"F{match['n']}", normalize_node(match["node"])
        if label not in specials:
            raise ScopeError(f"{label} is not a special case in the current findings file (stale map)")
        if not _safe_node(node):
            raise ScopeError(f"unsafe node id {node!r}")
        if not Path(node.split("::", 1)[0]).is_file():
            raise ScopeError(f"mapped test file missing for {label}: {node}")
        mapped.setdefault(label, []).append(node)
    return mapped


def _read_bound(run_dir: Path, name: str) -> dict | None:
    """Return a verdict or acceptance record when it is bound to the current findings and map, else ``None``."""
    path = run_dir / name
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        current = all(record[key] == value for key, value in _hashes(run_dir).items())
        unpinned = {str(label): [str(node) for node in nodes] for label, nodes in record["unpinned"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return {"unpinned": unpinned} if current else None


def read_acceptance(run_dir: Path) -> dict[str, list[str]]:
    """Return ``{label: [unpinned node, ...]}`` of the current acceptance; print and drop a stale one."""
    if not (run_dir / ACCEPTED_NAME).exists():
        return {}
    record = _read_bound(run_dir, ACCEPTED_NAME)
    if record is None:
        print("STALE ACCEPTANCE: findings or map changed since it was recorded — ignored; every label is enforced")
        return {}
    if record["unpinned"]:
        print(f"ACCEPTED UNPINNED: {' '.join(sorted(record['unpinned'], key=_label_key))}")
    return record["unpinned"]


def selects(node: str, test_id: str) -> bool:
    """Tell whether mapped ``node`` selects the reported ``test_id`` (itself, a class member or a parametrized case).

    Examples:
        >>> selects("t.py::test_p", "t.py::test_p[a b]"), selects("t.py::K", "t.py::K::test_m")
        (True, True)
        >>> selects("t.py::a", "t.py::ab")
        False
    """
    return test_id == node or test_id.startswith((node + "::", node + "["))


def junit_outcomes(report: Path) -> dict[str, str]:
    """Return ``{node id: outcome}`` from a pytest ``xunit1`` JUnit report.

    The node id is the case's ``file`` attribute, then the ``classname`` parts beyond that file's dotted module path,
    then ``name``. A skip of type ``pytest.xfail`` is an expected failure; a strict xpass is a failure naming ``XPASS``.

    Raises:
        NoVerdict: When the report is missing, invalid, lacks a ``file`` attribute, or repeats a node id.

    Examples:
        >>> import tempfile
        >>> xml = (
        ...     '<testsuites><testsuite>'
        ...     '<testcase file="t/Unit/test_a.py" classname="t.Unit.test_a" name="a"/>'
        ...     '<testcase file="t/Unit/test_a.py" classname="t.Unit.test_a.check_k" name="b[x y]">'
        ...     '<skipped type="pytest.skip"/></testcase>'
        ...     '<testcase file="t/a.b/test_x.py" classname="t.a.b.test_x" name="c"><skipped type="pytest.xfail"/>'
        ...     "</testcase>"
        ...     '<testcase file="t/a/b/test_x.py" classname="t.a.b.test_x" name="c"><error message="teardown"/>'
        ...     "</testcase></testsuite></testsuites>"
        ... )
        >>> with tempfile.TemporaryDirectory() as d:
        ...     p = Path(d) / "r.xml"
        ...     _ = p.write_text(xml, encoding="utf-8")
        ...     for item in sorted(junit_outcomes(p).items()):
        ...         print(*item)
        t/Unit/test_a.py::a PASSED
        t/Unit/test_a.py::check_k::b[x y] SKIPPED
        t/a.b/test_x.py::c XFAIL
        t/a/b/test_x.py::c ERROR
    """
    try:
        cases = list(ET.fromstring(report.read_text(encoding="utf-8")).iter("testcase"))  # noqa: S314 - own report
    except (OSError, ET.ParseError) as exc:
        raise NoVerdict(f"no readable JUnit report at {report.name}", EXIT_NO_VERDICT) from exc
    outcomes: dict[str, str] = {}
    for case in cases:
        outcome = "PASSED"
        for child in case:
            if child.tag == "skipped":
                outcome = "XFAIL" if child.get("type") == "pytest.xfail" else "SKIPPED"
            elif child.tag in ("failure", "error"):
                failed = {"failure": "FAILED"}.get(child.tag, "ERROR")
                outcome = "XPASS" if "XPASS" in (child.get("message") or "") else failed
        file_part = normalize_node(case.get("file") or "")
        if not file_part.endswith(".py"):
            raise NoVerdict("JUnit report has no file attribute (xunit1 not applied?)", EXIT_NO_VERDICT)
        module = file_part.removesuffix(".py").replace("/", ".")
        classname = case.get("classname") or ""
        classes = classname[len(module) + 1 :].split(".") if classname.startswith(module + ".") else []
        node = "::".join([file_part, *classes, case.get("name") or ""])
        if node in outcomes:
            raise NoVerdict(f"JUnit report repeats test id {node!r}", EXIT_NO_VERDICT)
        outcomes[node] = outcome
    return outcomes


def xpassed(output: str, nodes: list[str]) -> set[str]:
    """Return the mapped nodes that select a test pytest's ``-rX`` summary lists as ``XPASS``.

    Examples:
        >>> xpassed("XPASS t.py::test_p[a b] - why\\nXPASS t.py::other\\n", ["t.py::test_p", "t.py::test_q"])
        {'t.py::test_p'}
    """
    hits = [line[len("XPASS ") :] for line in output.splitlines() if line.startswith("XPASS ")]
    return {node for node in nodes for hit in hits if any(hit.startswith(node + tail) for tail in ("", "[", "::"))}


def _run(nodes: list[str], extra: list[str]) -> tuple[int, str]:
    """Run ``nodes`` through the sibling runner with ``extra`` flags; print and return its exit code and output."""
    runner = Path(__file__).resolve().with_name("dev_run_tests.py")
    argv = [sys.executable, str(runner), "pytest", "--", *nodes, "-p", "no:xdist", *extra]
    # argv list, no shell; the runner validates every node and the report path
    proc = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", check=False)  # noqa: S603
    print(proc.stdout + proc.stderr, end="")
    return proc.returncode, proc.stdout


def run_pins(run_dir: Path, nodes: list[str]) -> dict[str, str]:
    """Run ``nodes`` once and return ``{node: reason}`` for every node that did not pin (empty when all passed).

    Raises:
        NoVerdict: On a runner fault, a pytest exit without a verdict, an unusable report, or report ids that match no
            mapped node.
    """
    report = run_dir / JUNIT_NAME
    report.unlink(missing_ok=True)
    flags = ["-v", "-rX", "-o", "xfail_strict=true", "-o", "junit_family=xunit1", f"--junitxml={report}"]
    code, output = _run(nodes, flags)
    if code not in _PYTEST_CODES:
        raise NoVerdict("test runner did not run the special-case tests", code)
    if code not in _VERDICT_CODES:
        raise NoVerdict(_PYTEST_FAULTS[code], EXIT_NO_VERDICT)
    outcomes = junit_outcomes(report)
    if not any(selects(node, test_id) for node in nodes for test_id in outcomes):
        sample = next(iter(outcomes), "none")
        cause = f"report ids match no mapped node (pytest rootdir differs from the project root? got {sample!r})"
        raise NoVerdict(cause, EXIT_NO_VERDICT)
    xpasses = xpassed(output, nodes)
    reasons: dict[str, str] = {}
    for node in nodes:
        found = {outcome for test_id, outcome in outcomes.items() if selects(node, test_id)}
        bad = (found - {"PASSED"}) | ({"XPASS"} if node in xpasses else set())
        if not found or bad:
            reasons[node] = ", ".join(sorted(bad)) if found else "not reported"
    return reasons


def _load(run_dir: Path) -> tuple[list[str], dict[str, list[str]]]:
    """Validate findings and map; ``ScopeError`` means ``!

    BLOCKED``.
    """
    specials = special_cases(parse_findings(run_dir / FINDINGS_NAME))
    print(f"SPECIAL_CASES={len(specials)}{' ' + ' '.join(specials) if specials else ''}")
    return specials, read_map(run_dir / MAP_NAME, specials)


def cmd_check(run_dir: Path) -> int:
    """Validate the findings file and print its special cases."""
    specials = special_cases(parse_findings(run_dir / FINDINGS_NAME))
    print(f"SPECIAL_CASES={len(specials)}{' ' + ' '.join(specials) if specials else ''}")
    return 0


def cmd_pin(run_dir: Path) -> int:
    """Run the mapped tests of every non-accepted special case, persist the verdict and print it."""
    (run_dir / VERDICT_NAME).unlink(missing_ok=True)
    specials, mapped = _load(run_dir)
    accepted = read_acceptance(run_dir)
    active = [label for label in specials if label not in accepted]
    nodes = list(dict.fromkeys(node for label in active for node in mapped.get(label, [])))
    reasons = run_pins(run_dir, nodes) if nodes else {}
    unpinned: dict[str, list[str]] = {}
    shown: list[str] = []
    for label in active:
        bad = [node for node in mapped.get(label, []) if node in reasons]
        if label not in mapped or bad:
            unpinned[label] = bad
            shown.append(f"{label} ({'; '.join(f'{node} {reasons[node]}' for node in bad) or 'unmapped'})")
    _write_lf(run_dir / VERDICT_NAME, json.dumps({**_hashes(run_dir), "unpinned": unpinned}, indent=2) + "\n")
    if shown:
        print(f"GATE FAIL: special case not pinned — {'; '.join(shown)}")
        return EXIT_PIN_FAILED
    print(f"SPECIAL-CASE PIN OK: {len(active)} special case(s) pinned")
    return 0


def cmd_accept_unpinned(run_dir: Path) -> int:
    """Record exactly the current verdict's unpinned labels and nodes as accepted; never re-run tests."""
    _load(run_dir)
    verdict = _read_bound(run_dir, VERDICT_NAME)
    if verdict is None:
        raise NoVerdict(
            "no current Step 3 verdict for these findings and map — re-run the Step 3 gate", EXIT_NO_VERDICT
        )
    unpinned = {**read_acceptance(run_dir), **verdict["unpinned"]}
    _write_lf(run_dir / ACCEPTED_NAME, json.dumps({**_hashes(run_dir), "unpinned": unpinned}, indent=2) + "\n")
    print(f"ACCEPTED UNPINNED: {' '.join(sorted(unpinned, key=_label_key)) or 'none'}")
    return 0


def cmd_verify(run_dir: Path) -> int:
    """Step 4 entry: require a current verdict that is PIN OK or fully covered by a current acceptance."""
    specials, _ = _load(run_dir)
    if not specials:
        print("SPECIAL-CASE VERIFY OK: no special cases")
        return 0
    verdict = _read_bound(run_dir, VERDICT_NAME)
    if verdict is None:
        raise ScopeError("no current Step 3 verdict for these findings and map — re-run the Step 3 gate")
    uncovered = sorted(set(verdict["unpinned"]) - set(read_acceptance(run_dir)), key=_label_key)
    if uncovered:
        raise ScopeError(f"special case(s) {' '.join(uncovered)} not pinned and not accepted — re-run the Step 3 gate")
    print(f"SPECIAL-CASE VERIFY OK: {len(specials)} special case(s) pinned or accepted")
    return 0


def cmd_reset(run_dir: Path) -> int:
    """Delete map, verdict, acceptance and report, so nothing carries over from an earlier pass."""
    for name in (MAP_NAME, VERDICT_NAME, ACCEPTED_NAME, JUNIT_NAME):
        (run_dir / name).unlink(missing_ok=True)
    print("SPECIAL-CASE STATE RESET")
    return 0


def _collect(test_file: str) -> list[str]:
    """Return every test id in ``test_file`` from a collection run.

    Raises:
        ScopeError: When the file cannot be enumerated, so a baseline never silently drops its other tests.
    """
    code, output = _run([test_file], ["--co", "-q"])
    ids = [line.strip() for line in output.splitlines() if line.strip().startswith(test_file + "::")]
    if code != 0 or not ids:
        raise ScopeError(f"cannot enumerate tests in {test_file} to leave out only the accepted ones (exit {code})")
    return ids


def cmd_baseline_filter(run_dir: Path, selection: Path, output: Path, accepted_red: str) -> int:
    """Write the baseline selection without the accepted red file and accepted nodes no other special case maps."""
    tests = json.loads(selection.read_text(encoding="utf-8"))["tests"]
    _, mapped = _load(run_dir)
    accepted = read_acceptance(run_dir)
    kept_by_others = {node for label, nodes in mapped.items() if label not in accepted for node in nodes}
    dropped_nodes = {node for nodes in accepted.values() for node in nodes} - kept_by_others
    red = Path(accepted_red).resolve() if accepted_red.strip() else None
    remaining: list[str] = []
    for test in tests:
        if red is not None and Path(test).resolve() == red:
            continue
        dropped = sorted(node for node in dropped_nodes if node.split("::", 1)[0] == normalize_node(test))
        if not dropped:
            remaining.append(test)
            continue
        collected = _collect(normalize_node(test))
        remaining.extend(test_id for test_id in collected if not any(selects(node, test_id) for node in dropped))
        print(f"BASELINE: accepted-unpinned test(s) excluded from {test}: {' '.join(dropped)}")
    _write_lf(output, json.dumps(remaining) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and map each state to its exit code (see the module table).

    Args:
        argv: Argument list; ``sys.argv[1:]`` when ``None``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    choices = ("reset", "check", "pin", "accept-unpinned", "verify", "baseline-filter")
    parser.add_argument("command", choices=choices)
    parser.add_argument("run_dir", help="refactor run directory ($DEV_DIR)")
    parser.add_argument("extra", nargs="*", help="baseline-filter: <selection.json> <out.json> [<accepted-red-file>]")
    args = parser.parse_args(argv)
    if not args.run_dir.strip():
        print("! BLOCKED — refactor run directory missing; re-run Checkpoint init")
        return EXIT_BLOCKED
    run_dir = Path(args.run_dir).resolve()
    print(f"SCOPE_FINDINGS={run_dir / FINDINGS_NAME}")
    commands = {"reset": cmd_reset, "check": cmd_check, "pin": cmd_pin, "verify": cmd_verify}
    commands["accept-unpinned"] = cmd_accept_unpinned
    try:
        if args.command != "baseline-filter":
            return commands[args.command](run_dir)
        if len(args.extra) not in (2, 3):
            parser.error("baseline-filter needs <selection.json> <out.json> [<accepted-red-file>]")
        red = args.extra[2] if len(args.extra) == 3 else ""
        return cmd_baseline_filter(run_dir, Path(args.extra[0]), Path(args.extra[1]), red)
    except ScopeError as exc:
        print(f"! BLOCKED — {exc}; re-run Step 1 (findings) or Step 3 (map) before continuing")
        return EXIT_BLOCKED
    except NoVerdict as exc:
        print(f"! GATE ERROR: {exc} (exit {exc.code}) — no special-case verdict; fix the run, not the tests")
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
