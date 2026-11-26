"""Hold the review's instruction text to the helpers and records it hands work to.

Each class here checks one reader against its producer rather than one sentence against another:

- the publication gate block passes ``--fill-gaps`` only for a fill-gaps run, and the real parser accepts what it runs;
- the report and consolidator placeholders for an author claim name fields the claim helper actually writes;
- the report template gives a rerun hint only a command that tool accepts, and
- every promotion trigger names a unit that exists in the lineups where the trigger fires.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import find_review_report as frr
import pytest
import review_claim_repro as rcr
from _oss_shell_plugin import render_shell
from _skill_source import read_skill_source
from _verdict_publication import Publication, judged, missing

_PLUGIN = Path(__file__).resolve().parents[2]
_REVIEW = _PLUGIN / "skills/review"
_SESSION = "text-wiring-test"
_BASH = shutil.which("bash")
_PYTHON = shutil.which("python")
_skip_no_shell = pytest.mark.skipif(_BASH is None or _PYTHON is None, reason="bash or python unavailable")

#: What every run records in the flags sentinel before Step 5; the gate block reads only ``FILL_GAPS_MODE`` from it.
_FLAGS = {
    "fill-gaps": "REPLY_MODE=false\nWT_ENABLED=false\nFILL_GAPS_MODE=true\nDIRECT_PATH_MODE=false\n",
    "plain": "REPLY_MODE=false\nWT_ENABLED=false\nFILL_GAPS_MODE=false\nDIRECT_PATH_MODE=false\n",
}


def _lines(path: Path) -> list[str]:
    """Return the lines of one shipped review file."""
    return path.read_text(encoding="utf-8").splitlines()


def _line_starting(path: Path, prefix: str) -> str:
    """Return the one line of ``path`` that starts with ``prefix``."""
    matches = [line for line in _lines(path) if line.startswith(prefix)]
    assert len(matches) == 1, f"{path.name}: expected one line starting {prefix!r}, found {len(matches)}"
    return matches[0]


def _code_spans(text: str) -> list[str]:
    """Return every inline code span of one line."""
    return re.findall(r"`([^`]+)`", text)


def _gate_block() -> str:
    """Return the shipped bash block that runs the publication gate."""
    blocks = re.findall(r"```bash\n(.*?)```", read_skill_source(_REVIEW / "modes/post-agent.md"), re.DOTALL)
    return next(block for block in blocks if "find_review_report.py" in block and "--producer" in block)


def _run_gate(tmp_path: Path, flags: str | None) -> list[str]:
    """Run the shipped gate block against a recording stand-in for the helper and return the arguments it received.

    The stand-in plugin root holds only a ``find_review_report.py`` that writes its argument vector, so the result is
    exactly what the block's shell logic decided to pass, for the given flags sentinel (``None`` = no sentinel).
    """
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    out = tmp_path / "argv.json"
    stub = "import json, os, sys\nwith open(os.environ['ARGV_OUT'], 'w', encoding='utf-8') as handle:\n"
    stub += "    json.dump(sys.argv[1:], handle)\n"
    (root / "bin/find_review_report.py").write_text(stub, encoding="utf-8", newline="\n")
    report_dir = tmp_path / "run-001"
    report_dir.mkdir()
    sentinels = {"oss-review-report-dir": report_dir.as_posix()}
    if flags is not None:
        sentinels["oss-review-flags"] = flags
    for name, value in sentinels.items():
        (tmp_path / f"{name}-{_SESSION}").write_text(value + "\n", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": root.as_posix(),
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "TMPDIR": tmp_path.as_posix(),
        "ARGV_OUT": out.as_posix(),
    }
    result = subprocess.run(
        [_BASH, "-c", render_shell(_gate_block(), root, "oss")],
        cwd=tmp_path,
        env=env,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


@_skip_no_shell
class TestPublicationGateWiring:
    """The gate block asks the helper for the fill-gaps exemption exactly when the run is a fill-gaps run."""

    @pytest.mark.parametrize(
        ("flags", "fill_gaps"),
        [
            pytest.param(_FLAGS["fill-gaps"], True, id="fill-gaps-run"),
            pytest.param(_FLAGS["plain"], False, id="plain-run"),
            pytest.param(None, False, id="flags-sentinel-lost"),
        ],
    )
    def test_fill_gaps_flag_follows_the_run_mode(self, tmp_path: Path, flags: str | None, fill_gaps: bool) -> None:
        """Only a fill-gaps run passes ``--fill-gaps``; a plain run or a lost sentinel takes the strict gate."""
        argv = _run_gate(tmp_path, flags)
        assert ("--fill-gaps" in argv) is fill_gaps
        assert argv[:3] == ["--report", (tmp_path / "run-001/review-report.md").as_posix(), "--producer"]

    @pytest.mark.parametrize(
        "flags",
        [pytest.param(_FLAGS["fill-gaps"], id="fill-gaps-run"), pytest.param(_FLAGS["plain"], id="plain-run")],
    )
    def test_helper_accepts_the_arguments_the_block_passes(self, tmp_path: Path, flags: str) -> None:
        """The real parser takes each argument vector the block can produce, so the wiring cannot drift from the CLI."""
        args = frr._parse_args(_run_gate(tmp_path, flags))
        assert args.producer
        assert args.fill_gaps is (flags == _FLAGS["fill-gaps"])


def test_first_time_and_selected_fresh_review_share_conversation_collection() -> None:
    """Both ways to start a fresh full review reach the one conversation collection block."""
    preflight = _REVIEW / "modes/preflight.md"
    route = _line_starting(preflight, "A fresh full PR review")
    assert "no usable prior report" in route
    assert "selecting (a)" in route
    assert "below" in route


@pytest.fixture(name="reuse_records", params=[True, False])
def _reuse_records(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, str]:
    """Stage a pending run, with or without an earlier usable publication."""
    runs = tmp_path / ".reports/review/pr-7"
    pending = runs / "run-002"
    pending.mkdir(parents=True)
    if not request.param:
        return pending, ""
    old = runs / "run-001"
    old.mkdir()
    report = Publication("REVIEW_V1_PASS", "APPROVE", "complete", [judged("QA", "APPROVE")]).write(old)
    (old / "review-publication.published").touch()
    return pending, report.resolve().as_posix()


@_skip_no_shell
def test_existing_report_guard_surfaces_pending_and_published_paths(
    tmp_path: Path, reuse_records: tuple[Path, str]
) -> None:
    """The running shell guard exposes the pending run even when an older report is available to show."""
    preflight = read_skill_source(_REVIEW / "modes/preflight.md")
    blocks = re.findall(r"```bash\n(.*?)```", preflight, re.DOTALL)
    guard = next(block for block in blocks if "--path-out" in block and "oss-review-prior-report" in block)
    pending, selected = reuse_records
    (tmp_path / f"oss-review-pr-tag-{_SESSION}").write_text("7\n", encoding="utf-8", newline="\n")
    (tmp_path / f"oss-review-flags-{_SESSION}").write_text("DIRECT_PATH_MODE=false\n", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "TMPDIR": str(tmp_path),
    }
    result = subprocess.run(
        [_BASH, "-c", render_shell(guard, _PLUGIN, "oss")], cwd=tmp_path, env=env,
        encoding="utf-8", capture_output=True, check=False, timeout=15,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PRIOR_REPORT={selected}" in result.stdout.splitlines()
    assert f"PENDING_RUN={pending.resolve().as_posix()}" in result.stdout.splitlines()
    assert (tmp_path / f"oss-review-prior-report-{_SESSION}").read_text(encoding="utf-8").strip() == selected


def test_explicit_show_and_resume_use_their_selected_run() -> None:
    """Show uses usable publication evidence and resume authenticates the pending allocation before restoring it."""
    preflight = _REVIEW / "modes/preflight.md"
    show = _line_starting(preflight, "- An explicit request to show")
    resume = _line_starting(preflight, "- An explicit request to resume")
    recovery = _line_starting(_REVIEW / "modes/post-agent.md", "When preflight supplies `PENDING_RUN`")
    assert "using `PRIOR_REPORT`" in show
    assert "stop" in show
    assert "uses `PENDING_RUN`" in resume
    assert "whose `report-dir.txt` resolves to that directory" in recovery
    assert "first unmet checkpoint" in recovery


def test_publication_routes_carry_refusals_before_generic_recovery() -> None:
    """Prior evidence and a changed current draft reach their respective owners at both release entry points."""
    lines = _lines(_REVIEW / "modes/post-agent.md")
    terminal = lines.index("For a terminal release refusal, apply these branches in order:")
    ordinary = next(i for i, line in enumerate(lines) if line.startswith("- Exit 0 → the parent gates"))
    prior, current, other = lines[terminal + 2 : terminal + 5]
    assert "[gate] parent evidence: prior " in prior
    assert "prior-evidence branch" in prior
    assert "[carry] " in current
    assert "the parent owns the terminal draft" in current
    assert other.startswith("- Other `[gate] parent evidence:")
    route, generic = lines[ordinary + 1 : ordinary + 3]
    assert "[gate] parent evidence: prior " in route
    assert "[carry] " in route
    assert "matching refusal branch before generic parent recovery" in route
    assert generic.startswith("- `[gate] parent evidence: <file>")


def test_candidate_and_plan_bindings_use_the_allocated_head() -> None:
    """Fresh candidates and the fill-gaps plan bind to the published run's frozen revision."""
    post_agent = _REVIEW / "modes/post-agent.md"
    normal = _line_starting(post_agent, "Resolve `REVIEWED_COMMIT_SHA`")
    gate = _line_starting(post_agent, "**Publication gate**")
    assert "`$REPORT_DIR/head-sha.txt`" in normal
    assert "`$RUN_DIR/head-sha.txt`" in normal
    assert "Bind candidates and their checks to that exact revision" in normal
    assert "requires the plan's `head_sha` to equal the frozen head" in gate


def test_terminal_rejection_binding_uses_the_allocated_head() -> None:
    """Both terminal routes take the rejection suffix from the current run's frozen head."""
    terminal = _line_starting(_REVIEW / "modes/post-agent.md", "On either terminal path")
    assert "bind `PR_HEAD_SHA` from this run's `$REPORT_DIR/head-sha.txt`" in terminal


def test_consolidator_distinguishes_carried_gap_closures_from_open_gaps() -> None:
    """The stage instructions keep open carried records and append a zero-weight closure with the same ID."""
    prompt = _REVIEW / "templates/consolidator-prompt.md"
    stage = _line_starting(prompt, "**Gap ledger — after the report is written.**")
    assert "still open, stage nothing" in stage
    assert "same ID and text" in stage
    assert '"status": "closed"' in stage
    assert "zero or null" in stage


class TestAuthorClaimFields:
    """The author-claim line's placeholders name fields the claim helper writes, in the document that holds them."""

    @pytest.fixture(name="results")
    def _results(self) -> tuple[dict, dict]:
        """Return a real results document and its one claim record, from a run that starts no process."""
        plan = {
            "status": "ready",
            "reason": "",
            "host": {"os": "X"},
            "claims": [
                {
                    "id": "c1",
                    "claim_text": "2x faster",
                    "metric": "time",
                    "claimed_relative": -0.5,
                    "expected_direction": "decrease",
                    "hardware": {"comparable": True, "decision": "same-cpu-family"},
                }
            ],
            "argv": ["envpy", "-I", "harness.py", "bench.py"],
            "env_python": "/env/bin/python",
            "env_dir": "/env",
            "readable_roots": ["/env"],
            "base": "b" * 40,
            "head": "h" * 40,
            "sides": {"base": {"tree": "/trees/base"}, "head": {"tree": "/trees/head"}},
        }
        document = rcr.run_claims(plan, rcr.RunMode("none", "clause forbids running PR code"), 3, 30)
        return document, document["claims"][0]  # type: ignore[index]

    @staticmethod
    def _placeholders(line: str) -> list[str]:
        """Return the field-shaped placeholders of the line's first code span, in order."""
        template = next(span for span in _code_spans(line) if span.startswith("<claim_text>"))
        return re.findall(r"<([a-z_]+(?:\.[a-z_]+)?)>", template)

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(
                _line_starting(_REVIEW / "templates/consolidator-prompt.md", "**Author claims:**"), id="consolidator"
            ),
            pytest.param(_line_starting(_REVIEW / "templates/review-report.md", "**Author claim**:"), id="template"),
        ],
    )
    def test_placeholders_resolve_against_the_results(self, results: tuple[dict, dict], line: str) -> None:
        """Every placeholder is a key of its claim record (dotted keys nest) or of the results document itself."""
        document, record = results
        fields = self._placeholders(line)
        assert "rounds_completed" in fields
        assert "method.repeats" in fields
        for field in fields:
            head, _, tail = field.partition(".")
            holder = record if head in record else document
            assert head in holder, f"{field!r} is neither a claim-record key nor a results-document key"
            if tail:
                assert tail in holder[head], f"{field!r}: {head!r} has no {tail!r} key"


class TestProducerGateText:
    """Every file describing the publication gate states the same two exemptions, both limited to a fill-gaps run."""

    @pytest.mark.parametrize(
        ("path", "statement"),
        [
            pytest.param(
                "modes/agent-launch.md",
                "Fill-gaps runs use the plan's exact `roles` and write no lineup file",
                id="launch-lineup",
            ),
            pytest.param(
                "templates/consolidator-prompt.md",
                "Read `<REPORT_DIR>/lineup.md` for a fresh publication that reached fan-out",
                id="consolidator-lineup",
            ),
            pytest.param(
                "modes/verdict-states.md",
                "It refuses a fresh publication with no `lineup.md` as well; only a fill-gaps run (`--fill-gaps`)",
                id="states-lineup",
            ),
            pytest.param(
                "templates/consolidator-prompt.md",
                "refuses every UNVERIFIED candidate without the key, a candidate carried verbatim by a fill-gaps plan",
                id="consolidator-capability-kind",
            ),
            pytest.param(
                "modes/verdict-states.md",
                "an UNVERIFIED candidate without `missing_capability_kind` is refused outright",
                id="states-capability-kind",
            ),
            pytest.param(
                "modes/post-agent.md",
                "For a fresh fan-out, require `lineup.md`, exactly one completed or missing assessment",
                id="gate-summary",
            ),
        ],
    )
    def test_exemption_is_stated_where_the_rule_is(self, path: str, statement: str) -> None:
        """The rule and its fill-gaps exemption appear together in the file a reader meets the rule in."""
        assert any(statement in line for line in _lines(_REVIEW / path))


class TestRerunHints:
    """Every printed command round-trips through the parser and the mode's actual flag declarations."""

    @pytest.mark.parametrize(
        ("record", "expected"),
        [
            pytest.param("spawned | roles: Documentation reviewer", [], id="spawned"),
            pytest.param(
                "spawned | promoted: docs changed | roles: Documentation reviewer", [], id="promoted-and-spawned"
            ),
            pytest.param("dropped", [("true", "", "")], id="dropped-full-only"),
            pytest.param(
                "promoted-unslotted: docs changed | roles: Documentation reviewer, Linting expert",
                [("true", "", ""), ("false", "report", "Documentation reviewer,Linting expert")],
                id="unslotted-full-or-fill-its-roles",
            ),
        ],
    )
    def test_rerun_hint_parses_for_each_lineup_state(self, tmp_path: Path, record: str, expected: list) -> None:
        """Generated alternatives preserve the PR, mode and report path as distinct parser values."""
        report = tmp_path / "original checkout/.reports/review/pr-42/run-003/review-report.md"
        report.parent.mkdir(parents=True)
        report.with_name("lineup.md").write_text(f"docs+lint | {record}\n", encoding="utf-8")
        invocation = [
            sys.executable,
            str(_PLUGIN / "bin/find_review_report.py"),
            "--report",
            str(report),
            "--rerun-hints",
        ]
        shown = subprocess.run(invocation, capture_output=True, encoding="utf-8", check=True).stdout
        commands = [args for line in shown.splitlines() for args in _code_spans(line)]
        parsed = [self._parse_command(command) for command in commands]
        assert all(values["CLEAN_ARGS"] == "42" and values["VALUE_CANDIDATES"] == "" for values in parsed)
        assert [(v["FLAG_FULL"], v["VALUE_FILL_GAPS"], v["VALUE_ROLES"]) for v in parsed] == [
            (full, report.relative_to(tmp_path / "original checkout").as_posix() if path else "", roles)
            for full, path, roles in expected
        ]

    @staticmethod
    def _parse_command(command: str) -> dict[str, str]:
        """Feed a report hint to the real flag CLI using the declarations in review preflight."""
        cli = _line_starting(_REVIEW / "modes/preflight.md", 'python "$PLUGIN_ROOT/bin/parse-skill-flags.py"')
        flags = cli.split()[2:6]
        skill, arguments = command.split(maxsplit=1)
        assert skill == "/oss:review"
        invocation = [sys.executable, str(_PLUGIN / "bin/parse-skill-flags.py"), *flags, "--", arguments]
        output = subprocess.run(invocation, capture_output=True, encoding="utf-8", check=True).stdout
        return {name: value.strip("'\"") for name, _, value in (line.partition("=") for line in output.splitlines())}

    def test_report_uses_generated_commands_verbatim(self) -> None:
        """Step 5 supplies commands to both report writers instead of asking them to compose flag sequences."""
        assert any("--rerun-hints" in line for line in _lines(_REVIEW / "modes/post-agent.md"))
        assert any("RERUN_HINTS" in line for line in _lines(_REVIEW / "templates/consolidator-prompt.md"))
        assert any("RERUN_HINTS" in line for line in _lines(_REVIEW / "templates/review-report.md"))

    def test_generated_targeted_command_plans_the_missing_roles(self, tmp_path: Path) -> None:
        """The actual planner accepts the parsed hint from the original checkout and selects both missing roles."""
        run = tmp_path / ".reports/review/pr-7/run-001"
        run.mkdir(parents=True)
        reviewers = [
            judged("Software engineer", "MINOR_EDITS"),
            missing("Documentation reviewer"),
            missing("Linting expert"),
        ]
        lineup = "docs+lint | promoted-unslotted: docs changed | roles: Documentation reviewer, Linting expert\n"
        report = Publication("REVIEW_V1_PASS", "MINOR_EDITS", "incomplete", reviewers, lineup=lineup).write(run)
        head = "a" * 40
        (run / "head-sha.txt").write_text(head + "\n", encoding="utf-8")
        (run / "review-publication.published").touch()
        snap = tmp_path / "pr-meta.json"
        snap.write_text(json.dumps({"headRefOid": head}), encoding="utf-8")
        argv = [sys.executable, str(_PLUGIN / "bin/find_review_report.py"), "--report", str(report), "--rerun-hints"]
        line = subprocess.run(argv, capture_output=True, encoding="utf-8", check=True).stdout.strip()
        args = self._parse_command(next(span for span in _code_spans(line) if "--fill-gaps=" in span))
        plan = tmp_path / "plan.json"
        planner = [
            sys.executable,
            str(_PLUGIN / "bin/fill_gaps_scope.py"),
            "plan",
            "--pr",
            args["CLEAN_ARGS"],
            "--report",
            args["VALUE_FILL_GAPS"],
            "--roles",
            args["VALUE_ROLES"],
            "--snapshot",
            str(snap),
            "--plan-out",
            str(plan),
            "--head-out",
            str(tmp_path / "fill-head"),
        ]
        result = subprocess.run(planner, cwd=tmp_path, capture_output=True, encoding="utf-8", check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(plan.read_text(encoding="utf-8"))["units"][0]["roles"] == [
            "Documentation reviewer",
            "Linting expert",
        ]


def test_parent_evidence_repair_precedes_publication_retry() -> None:
    """The parent repairs its artifacts from original records before the consolidator recovery branch."""
    lines = _lines(_REVIEW / "modes/post-agent.md")
    owner = next(i for i, line in enumerate(lines) if "[gate] parent evidence:" in line and line.startswith("- "))
    retry = next(i for i, line in enumerate(lines) if line.startswith("- Gate failure, re-spawn unused"))
    assert owner < retry
    recovery = "\n".join(lines[owner:retry])
    assert all(text in recovery for text in ("fixed parent recovery block above", "without consuming"))
    assert "Never write or repair `lineup.md`" in "\n".join(_lines(_REVIEW / "templates/consolidator-prompt.md"))


@pytest.mark.parametrize("mode", ["DOCS_TYPING", "TESTS_CI", "CICD_ONLY", "DOCS_ONLY", "DOCS_CICD"])
def test_specialized_lineup_flows_through_acceptance(mode: str) -> None:
    """Every specialized lineup requires the acceptance gate before dispatch."""
    line = next(line for line in _lines(_REVIEW / "modes/scope-context.md") if f"`{mode}_MODE=true`" in line)
    assert "only after the acceptance gate" in line


def test_terminal_evidence_is_written_as_report_data() -> None:
    """Contributor-derived evidence belongs in the writer contract alone."""
    lines = _lines(_REVIEW / "modes/acceptance-gate.md")
    assert not any("GATE_REASON" in line for line in lines)
    assert any(line.startswith("- `Summary:` stating the specific evidence") for line in lines)


@pytest.mark.parametrize(
    ("path", "required"),
    [
        pytest.param("modes/agent-launch.md", "$RUN_DIR/lineup.launch.md", id="authoritative-launch-lineup"),
        pytest.param("modes/post-agent.md", "one object per assigned candidate", id="per-candidate-return"),
        pytest.param("modes/post-agent.md", "duplicated or unmatched entry", id="reject-ambiguous-verifier-output"),
        pytest.param("templates/agent-prompts.md", "CONVERSATION_SNAPSHOT=", id="reviewer-consumes-conversation"),
        pytest.param("templates/consolidator-prompt.md", "missing binding", id="consolidator-discloses-missing-input"),
        pytest.param("modes/fill-gaps.md", "even with zero units", id="gap-preparation-without-reviewers"),
        pytest.param("modes/fill-gaps.md", "latest record per ID", id="complete-carried-gap-inventory"),
        pytest.param("SKILL.md", "Skip Step 0", id="resume-without-reset"),
        pytest.param("templates/agent-prompts.md", "code-user-choice.txt", id="explicit-approval-input"),
    ],
)
def test_review_handoff_names_its_required_input(path: str, required: str) -> None:
    """The deciding producer or consumer documents the exact input and handling."""
    assert any(required in line for line in _lines(_REVIEW / path))
