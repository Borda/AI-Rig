"""Keep review confidence tied to named coverage gaps instead of constants and host trivia.

A fixed Codex confidence fed every run's min and median, challenger's own skipped Codex pass and the reviewer host's
operating system were recorded as gaps, and nothing tied the aggregate to Readiness, so scores sat flat. These tests
hold the consolidator, agent and report-template wording to the replacement rules, and run the publication gate on real
publications to show the Readiness ceiling is enforced for fresh reports while resolve intake still reads saved ones.
"""

from __future__ import annotations

from pathlib import Path

import find_review_report as frr
import pytest
from _text_io import flat
from _verdict_publication import Publication, candidate, judged

_PLUGIN = Path(__file__).resolve().parents[2]
_TEMPLATES = _PLUGIN / "skills/review/templates"


def _lines(name: str) -> list[str]:
    """Return the lines of one review template."""
    return (_TEMPLATES / name).read_text(encoding="utf-8").splitlines()


def _line_starting(name: str, prefix: str) -> str:
    """Return the one line of a template that starts with ``prefix``."""
    matches = [line for line in _lines(name) if line.startswith(prefix)]
    assert len(matches) == 1, f"{name}: expected one line starting {prefix!r}, found {len(matches)}"
    return matches[0]


class TestCodexLane:
    """The Codex co-review is a static lane whose confidence comes from its envelope or not at all."""

    def test_no_fixed_codex_confidence(self) -> None:
        """No template assigns the Codex lane a constant score that would feed min and median on every run."""
        for name in ("consolidator-prompt.md", "review-report.md", "agent-prompts.md"):
            text = flat((_TEMPLATES / name).read_text(encoding="utf-8"))
            assert "fixed confidence" not in text
            assert "0.75" not in text

    @pytest.mark.parametrize(
        "statement",
        [
            pytest.param("The Codex lane is a static, read-only co-review", id="labelled-static"),
            pytest.param("whose `status` is `complete`", id="envelope-confidence-only-when-complete"),
            pytest.param("stays out of min, median and the aggregate", id="no-number-excluded"),
            pytest.param(
                "A null, failed, timed-out, quota-blocked or blocked Codex reply never contributes a number",
                id="failed-excluded",
            ),
            pytest.param("name it once in the `## Confidence` **Gaps**", id="failed-named-as-gap"),
        ],
    )
    def test_consolidator_parses_codex_from_envelope(self, statement: str) -> None:
        """The confidence-parsing rule takes Codex confidence from the reply envelope and names a failed reply."""
        assert statement in _line_starting("consolidator-prompt.md", "**Confidence parsing:**")

    def test_report_template_shows_codex_without_a_number(self) -> None:
        """The report's confidence table gives the Codex row a dash, never a score, unless the envelope carries one."""
        rows = [line for line in _lines("review-report.md") if line.startswith("| Codex co-reviewer |")]
        assert len(rows) == 1
        cells = [cell.strip() for cell in rows[0].strip("|").split("|")]
        assert cells[1] == "—"

    def test_bridge_review_asks_for_a_rating_line(self) -> None:
        """The bridge review request ends with a source-5 rating line, so a completed Codex reply can carry a judgment.

        Without it a completed Codex reply was a verbal verdict only, recorded as an undecidable reviewer that blocked
        Readiness on its own.
        """
        launch = (_PLUGIN / "skills/review/modes/agent-launch.md").read_text(encoding="utf-8").splitlines()
        request = next(line for line in launch if 'Skill(skill="bridge:review"' in line)
        assert "End the verdict string with one line: source-5-v2 rating: <1-5>" in request
        consolidator = _line_starting("consolidator-prompt.md", "**Confidence parsing:**")
        assert "`source-5-v2 rating: <1-5> — <rationale>`" in consolidator

    def test_rating_is_read_from_the_field_the_peer_can_write(self) -> None:
        """The request and the consolidator agree on where the rating lives: the bridge reply's ``verdict`` string.

        The peer answers under a fixed output schema, so its free text lands only in ``verdict`` or ``details``; a
        transcript never ends with a rating line, and a consolidator reading the transcript finds no Codex rating.
        """
        launch = (_PLUGIN / "skills/review/modes/agent-launch.md").read_text(encoding="utf-8").splitlines()
        request = next(line for line in launch if 'Skill(skill="bridge:review"' in line)
        assert "the consolidator reads the rating line from `verdict` in `codex-review-reply.json`" in request
        parsing = _line_starting("consolidator-prompt.md", "**Confidence parsing:**")
        assert "whose `verdict` string in `codex-review-reply.json` ends with" in parsing
        assert "never from the transcript" in parsing
        assert "transcript ends with" not in parsing


class TestGapsNotCarried:
    """Self-inflicted and provisionable entries never reach the score or the header."""

    @pytest.fixture(name="carried_rules")
    def _carried_rules(self) -> str:
        """Return the bullet list under the consolidator's never-carried heading, flattened."""
        lines = _lines("consolidator-prompt.md")
        start = lines.index("**Gaps never carried into the score or `Confidence:` header:**")
        bullets = []
        for line in lines[start + 1 :]:
            if line.startswith("- "):
                bullets.append(line)
            elif bullets and line.strip():
                break
        return "\n".join(bullets)

    @pytest.mark.parametrize(
        "statement",
        [
            pytest.param("own skipped Codex cross-check (`--no-codex`)", id="challenger-no-codex"),
            pytest.param(
                "operating system (whatever host class it is, recorded per run): never a gap by itself", id="host-os"
            ),
            pytest.param("A missing Python package or another Python version: provisioned", id="packages-provisioned"),
            pytest.param(
                "only a failed provisioning step with its concrete reason stays a gap", id="provisioning-failure"
            ),
            pytest.param(
                "naming exactly that class and the paths it leaves unexecuted, nothing broader", id="hardware-narrow"
            ),
            pytest.param("never listed as a `(-0.00)` **Gaps** line", id="closures-not-gaps"),
        ],
    )
    def test_consolidator_drops_self_inflicted_gaps(self, carried_rules: str, statement: str) -> None:
        """Each entry the audit traced to the workflow or the host, not the PR, is excluded or narrowed."""
        assert statement in carried_rules

    def test_agents_do_not_record_host_trivia(self) -> None:
        """Every reviewer gets the same rule, so the consolidator is not left filtering host trivia out of each file."""
        rule = _line_starting("agent-prompts.md", "**Confidence gaps — every agent:**")
        assert "never list the reviewer host's operating system" in rule
        assert "never a gap by itself" in rule
        assert "name exactly that class and the paths it leaves unexecuted" in rule

    def test_challenger_skip_is_not_a_gap(self) -> None:
        """The challenger's instructed ``--no-codex`` skip is covered by the review's own Codex lane."""
        rule = next(line for line in _lines("agent-prompts.md") if line.startswith("**Agent 7 — foundry:challenger"))
        assert "That skipped cross-check is never a Confidence gap" in rule


class TestReadinessCeiling:
    """The publication gate caps a fresh report's confidence by its Readiness; intake keeps reading saved reports."""

    @staticmethod
    def _publish(tmp_path: Path, publication: Publication) -> Path:
        """Write the frozen head and launch roles a current producer must record."""
        (tmp_path / "head-sha.txt").write_text("abcdef1234567", encoding="utf-8")
        publication.lineup = "sw-engineer | spawned | roles: Software engineer\n"
        return publication.write(tmp_path)

    @staticmethod
    def _blocked(tmp_path: Path, confidence: str, readiness: str = "blocked") -> Path:
        """Publish a judged review whose one candidate is UNVERIFIED for absent hardware, at ``confidence``."""
        blocker = candidate("V1", "UNVERIFIED", missing_capability_kind="hardware")
        reviewers = [judged("Software engineer", "MINOR_EDITS")]
        publication = Publication(
            "REVIEW_V1_PASS", "MINOR_EDITS", readiness, reviewers, [blocker], confidence=confidence
        )
        return TestReadinessCeiling._publish(tmp_path, publication)

    @pytest.mark.parametrize(
        ("confidence", "returncode"),
        [
            pytest.param("0.86 — mypy not reproduced", 1, id="above-blocked-ceiling"),
            pytest.param("0.80 — no CUDA host", 0, id="at-blocked-ceiling"),
            pytest.param("0.62 — no CUDA host", 0, id="below-blocked-ceiling"),
            pytest.param("unavailable", 0, id="no-leading-score-unchecked"),
        ],
    )
    def test_producer_caps_blocked_readiness(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], confidence: str, returncode: int
    ) -> None:
        """A blocked review stating more than the blocked ceiling is refused with the ceiling named in the reason."""
        report = self._blocked(tmp_path, confidence)
        assert frr.main(["--report", str(report), "--producer"]) == returncode
        output = capsys.readouterr().out
        assert ("exceeds the 0.80 ceiling for readiness=blocked" in output) is bool(returncode)
        assert ("incomplete-review-report" in output) is bool(returncode)

    def test_producer_caps_incomplete_readiness_lower(self, tmp_path: Path) -> None:
        """Incomplete readiness (work still owed) carries the lower ceiling."""
        publication = Publication(
            "REVIEW_V1_PASS",
            "MINOR_EDITS",
            "incomplete",
            [
                judged("Software engineer", "MINOR_EDITS"),
                {
                    **judged("QA", "APPROVE"),
                    "presence": "missing",
                    "source_outcome": None,
                    "source_rating": None,
                    "readiness": "incomplete",
                },
            ],
            confidence="0.75 — QA reviewer missing",
        )
        report = self._publish(tmp_path, publication)
        line, reason = frr.publication_verdict(report, producer=True)
        assert not line
        assert "exceeds the 0.70 ceiling for readiness=incomplete" in reason

    def test_intake_still_reads_a_saved_report_above_the_ceiling(self, tmp_path: Path) -> None:
        """Resolve intake never applies the ceiling, so reports published before it keep their findings usable."""
        report = self._blocked(tmp_path, "0.88 — saved before the ceiling")
        assert frr.main(["--report", str(report)]) == 0
        assert frr.assess_report(report).gaps == []

    def test_complete_readiness_has_no_ceiling(self, tmp_path: Path) -> None:
        """Only non-complete Readiness is capped; a complete review keeps whatever its reviewers support."""
        publication = Publication(
            "REVIEW_V1_PASS", "APPROVE", "complete", [judged("Software engineer", "APPROVE")], confidence="0.95"
        )
        report = self._publish(tmp_path, publication)
        line, reason = frr.publication_verdict(report, producer=True)
        assert line == "Gate: PASS", reason

    def test_consolidator_states_ceilings_without_floors(self) -> None:
        """The consolidator applies the table's ceilings and states that no floor or variance target exists."""
        rule = _line_starting("consolidator-prompt.md", "**Confidence ceilings:**")
        assert "`verdict-states.md` §Confidence ceilings" in rule
        assert "Ceilings only: no floor, no variance target, no stacking." in rule
        assert "The publication gate refuses a `Confidence:` above its Readiness ceiling." in rule
