"""Guard the bridge quota check: only a harness incident whose fault is quota-exhausted classifies a reply.

The bridge links an incident record through a reply's ``incident`` field; its ``fault`` is the harness-classified cause.
Verdict and blockers text is prose a peer model can forge, so the check never reads it. Every looping Codex consumer in
this plugin (resolve C1, resolve comment dispatch, analyse vitality, review Step 4) relies on this one decision.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from bridge_quota_check import incident_path, main

#: Workspace-relative incident path in the shape the bridge harness writes.
_INCIDENT = ".temp/bridge/incidents/1700000000000000000-0a0b0c0d-quota-exhausted.json"
_skip_no_git = pytest.mark.skipif(shutil.which("git") is None, reason="the repository-root fallback asks git")


def _symlinks_supported() -> bool:
    """Probe whether this host and account may create a symlink (Windows needs a privilege)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(tmp, Path(tmp, "link"))
        except (OSError, NotImplementedError):
            return False
    return True


_skip_no_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="symlink creation unavailable on this host")


def _write_json(path: Path, payload: object) -> Path:
    """Write ``payload`` as JSON at ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    return path


def _reply(incident: object) -> dict:
    """Return a stopped bridge reply whose verdict already carries the quota wording."""
    verdict = "Codex quota exhausted: usage limit reached"
    return {"status": "blocked", "verdict": verdict, "findings": [], "files_touched": [], "remaining": [],
            "blockers": [verdict], "incident": incident}  # fmt: skip


class TestBridgeQuotaCheck:
    """``bridge_quota_check.py REPLY`` prints ``quota-exhausted`` or ``none``."""

    def test_quota_incident_classifies_the_reply(self, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A reply linking a harness incident with fault quota-exhausted is a quota stop."""
        _write_json(workdir / _INCIDENT, {"schema_version": 2, "fault": "quota-exhausted"})
        reply = _write_json(workdir / "reply.json", _reply(_INCIDENT))

        code = main([str(reply)])

        assert (code, capsys.readouterr().out) == (0, "quota-exhausted\n")

    @_skip_no_git
    @pytest.mark.parametrize(
        ("workspace", "cwd"),
        [
            pytest.param(".", "nested", id="workspace-at-git-root"),
            pytest.param("pkg", "pkg/src", id="workspace-below-git-root"),
        ],
    )
    def test_finds_the_incident_from_below_the_workspace(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        workspace: str,
        cwd: str,
    ) -> None:
        """The incident path is relative to the bridge workspace, so a shell that moved deeper still finds it.

        The bridge workspace is the launch directory, which may itself sit below the repository root; the lookup walks
        from the current directory up to the Git root.
        """
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        _write_json(tmp_path / workspace / _INCIDENT, {"fault": "quota-exhausted"})
        reply = _write_json(tmp_path / "reply.json", _reply(_INCIDENT))
        (tmp_path / cwd).mkdir(parents=True)
        monkeypatch.chdir(tmp_path / cwd)

        code = main([str(reply)])

        assert (code, capsys.readouterr().out) == (0, "quota-exhausted\n")

    @pytest.mark.parametrize(
        "incident",
        [
            pytest.param(".temp/bridge/incidents/..\\..\\..\\outside.json", id="windows-parent-traversal"),
            pytest.param(".temp/bridge/incidents/sub\\x.json", id="windows-separator"),
        ],
    )
    def test_backslash_paths_are_not_incidents(self, incident: str) -> None:
        """A backslash would act as a separator on Windows and could climb out of the store, so it is rejected."""
        assert incident_path({"incident": incident}) is None

    @_skip_no_symlink
    @pytest.mark.parametrize(
        ("link", "target"),
        [
            pytest.param(_INCIDENT, "outside/quota.json", id="incident-file-symlink"),
            pytest.param(".temp/bridge/incidents", "outside", id="incident-store-symlink"),
        ],
    )
    def test_symlink_out_of_the_store_is_not_followed(
        self, workdir: Path, capsys: pytest.CaptureFixture[str], link: str, target: str
    ) -> None:
        """A store or record that resolves outside ``.temp/bridge/incidents`` is not a harness incident.

        Only the bridge writes inside its own store; a symlink there pointing elsewhere could plant a quota fault.
        """
        _write_json(workdir / "outside" / Path(_INCIDENT).name, {"fault": "quota-exhausted"})
        _write_json(workdir / "outside/quota.json", {"fault": "quota-exhausted"})
        (workdir / link).parent.mkdir(parents=True, exist_ok=True)
        (workdir / link).symlink_to(workdir / target)
        reply = _write_json(workdir / "reply.json", _reply(_INCIDENT))

        code = main([str(reply)])

        assert (code, capsys.readouterr().out) == (0, "none\n")

    @pytest.mark.parametrize(
        ("incident", "written", "record"),
        [
            pytest.param(None, _INCIDENT, {"fault": "quota-exhausted"}, id="model-authored-null-incident"),
            pytest.param(_INCIDENT, None, None, id="incident-file-missing"),
            pytest.param(_INCIDENT, _INCIDENT, {"fault": "blocked"}, id="old-bridge-generic-fault"),
            pytest.param(_INCIDENT, _INCIDENT, {"fault": "rate-limited"}, id="rate-limited"),
            pytest.param(_INCIDENT, _INCIDENT, {"fault": "capacity"}, id="capacity"),
            pytest.param(_INCIDENT, _INCIDENT, ["quota-exhausted"], id="record-not-an-object"),
            pytest.param("../escape.json", "../escape.json", {"fault": "quota-exhausted"}, id="parent-traversal"),
            pytest.param(
                ".temp/bridge/incidents/sub/x.json",
                ".temp/bridge/incidents/sub/x.json",
                {"fault": "quota-exhausted"},
                id="below-the-incident-store",
            ),
            pytest.param("notes/quota.json", "notes/quota.json", {"fault": "quota-exhausted"}, id="outside-store"),
            pytest.param(7, None, None, id="incident-not-a-string"),
        ],
    )
    def test_anything_else_is_not_classified(
        self,
        workdir: Path,
        capsys: pytest.CaptureFixture[str],
        incident: object,
        written: str | None,
        record: object,
    ) -> None:
        """Only a harness incident in the bridge store with fault quota-exhausted classifies; the rest prints none.

        A peer can write the quota wording but never an incident, even while a real quota incident from another call
        sits on disk. Older bridges, rate limits and capacity name other faults. A path outside the bridge's incident
        store is not an incident, whatever it holds.
        """
        _write_json(workdir / (written or "unrelated.json"), record)
        reply = _write_json(workdir / "reply.json", _reply(incident))

        code = main([str(reply)])

        assert (code, capsys.readouterr().out) == (0, "none\n")

    def test_unreadable_reply_exits_2(self, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A reply file that is missing or not a JSON object is reported, not silently classified."""
        (workdir / "reply.json").write_text("[]", encoding="utf-8")

        code = main([str(workdir / "reply.json")])

        assert code == 2
        assert "cannot read reply" in capsys.readouterr().err


#: Glob a resolve run passes for its persisted Phase 1 challenger replies.
_VERDICTS = "challenge-verdicts-*.json"


def _verdicts(**extra: object) -> dict:
    """Return a persisted Phase 1 challenger reply, plus any extra top-level keys."""
    return {"items": [{"id": 3, "evidence": "VALID", "evidence_rationale": "a.py:4 drops the value"}], **extra}


class TestChallengerQuotaCheck:
    """``bridge_quota_check.py --challenger DIR PATTERN`` reads the challenger's own Codex cross-check outcome."""

    def test_quota_field_in_any_reply_classifies_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One persisted challenger reply carrying ``codex_blocked: "quota"`` is a quota stop for the run.

        A ``foundry:challenger`` runs its own ``bridge:review`` cross-check; when that call stops on a harness quota
        incident the challenger reports it in its envelope, and every later Codex call in the run would fail the same
        way.
        """
        _write_json(tmp_path / "challenge-verdicts-logic.json", _verdicts())
        _write_json(tmp_path / "challenge-verdicts-api-late.json", _verdicts(codex_blocked="quota"))

        code = main(["--challenger", str(tmp_path), _VERDICTS])

        assert (code, capsys.readouterr().out) == (0, "quota-exhausted\n")

    @pytest.mark.parametrize(
        ("name", "content"),
        [
            pytest.param("challenge-verdicts-logic.json", json.dumps(_verdicts()), id="field-absent"),
            pytest.param(
                "challenge-verdicts-logic.json", json.dumps(_verdicts(codex_blocked=True)), id="boolean-value"
            ),
            pytest.param(
                "challenge-verdicts-logic.json", json.dumps(_verdicts(codex_blocked="rate-limited")), id="rate-limited"
            ),
            pytest.param(
                "challenge-verdicts-logic.json", json.dumps(_verdicts(codex_blocked="capacity")), id="capacity"
            ),
            pytest.param(
                "challenge-verdicts-logic.json",
                json.dumps({"items": [{"id": 3, "codex_blocked": "quota"}]}),
                id="nested-not-top-level",
            ),
            pytest.param(
                "challenge-verdicts-logic.json",
                json.dumps({"items": [{"id": 3, "evidence_rationale": "Codex quota exhausted: usage limit reached"}]}),
                id="quota-wording-in-rationale",
            ),
            pytest.param("challenge-verdicts-logic.json", json.dumps(["codex_blocked", "quota"]), id="not-an-object"),
            pytest.param("challenge-verdicts-logic.json", '{"codex_blocked": "quota"', id="truncated-reply"),
            pytest.param("c1-reply-4.json", json.dumps(_verdicts(codex_blocked="quota")), id="file-outside-pattern"),
            pytest.param(
                "sub/challenge-verdicts-logic.json",
                json.dumps(_verdicts(codex_blocked="quota")),
                id="below-the-run-dir",
            ),
        ],
    )
    def test_anything_else_is_not_classified(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, content: str
    ) -> None:
        """Only a top-level ``codex_blocked`` equal to ``"quota"`` in a matching reply counts; the rest prints none.

        Only a quota stop disables Codex for the run: rate limits and capacity stay transient. Rationale prose is never
        read, a reply cut off mid-write or not an object is not a stop, and only replies directly in the run directory
        that match the pattern belong to a challenger, so a bridge reply or a nested file never decides.
        """
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        code = main(["--challenger", str(tmp_path), _VERDICTS])

        assert (code, capsys.readouterr().out) == (0, "none\n")

    @pytest.mark.parametrize(
        "run_dir",
        [pytest.param("missing", id="run-dir-missing"), pytest.param("empty", id="no-reply-persisted-yet")],
    )
    def test_no_reply_prints_none(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], run_dir: str) -> None:
        """A challenger that timed out leaves no reply, which is normal here and never an error."""
        (tmp_path / "empty").mkdir()

        code = main(["--challenger", str(tmp_path / run_dir), _VERDICTS])

        assert (code, capsys.readouterr().out) == (0, "none\n")

    def test_directory_name_is_never_a_pattern(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Glob characters in the run directory's own path are literal, so the reply there is still found."""
        run_dir = tmp_path / "run [1]"
        _write_json(run_dir / "challenger-iter0.json", {"verdict": "pass", "codex_blocked": "quota"})

        code = main(["--challenger", str(run_dir), "challenger-iter0.json"])

        assert (code, capsys.readouterr().out) == (0, "quota-exhausted\n")

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="neither"),
            pytest.param(["reply.json", "--challenger", "run", _VERDICTS], id="both"),
            pytest.param(["--challenger", "run"], id="no-pattern"),
        ],
    )
    def test_exactly_one_mode_is_required(self, argv: list[str]) -> None:
        """A bridge reply and a challenger scan are separate questions, so the call names exactly one."""
        with pytest.raises(SystemExit) as stop:
            main(argv)

        assert stop.value.code == 2


#: Line the challenger opens its ``## Codex Cross-Check`` section with after a quota stop.
_QUOTA_LINE = (
    "⚠ codex-blocked:quota — Codex out of credits (incident fault `quota-exhausted`); cross-check not retried. "
    "Report above is Claude-only."
)
#: Head of a challenger report, before its Codex Cross-Check section.
_REPORT_HEAD = "## Challenge: PR 7\n\n### Summary\nSolid.\n\n---\n\n"
#: Line the challenger opens its Codex Cross-Check section with when Codex ran; the verbatim Codex output follows it.
_SUCCESS_LINE = "Codex cross-check completed."
#: A real Codex-succeeded section; anything appended to it is verbatim Codex output a peer model writes.
_SUCCESS_SECTION = f"## Codex Cross-Check\n\n{_SUCCESS_LINE}\n\n"
#: A report quoting the quota contract inside a fenced block before its own section.
_FENCED_QUOTE = f"```markdown\n## Codex Cross-Check\n\n{_QUOTA_LINE}\n```\n\n"


class TestChallengerReportLine:
    """A challenger reply without a JSON envelope carries the stop as the opening line of its Cross-Check section."""

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(f"{_REPORT_HEAD}## Codex Cross-Check\n\n{_QUOTA_LINE}\n", id="report-opening-line"),
            pytest.param(f"{_REPORT_HEAD}## Codex Cross-Check\n{_QUOTA_LINE}\n", id="no-blank-after-heading"),
            pytest.param(
                f"{_REPORT_HEAD}{json.dumps(_verdicts(codex_blocked='quota'))}\n", id="report-closed-by-envelope-line"
            ),
            pytest.param(
                f"{_REPORT_HEAD}```python\nx = 1\n```\n\n## Codex Cross-Check\n\n{_QUOTA_LINE}\n",
                id="closed-fence-earlier-in-the-body",
            ),
            pytest.param(
                f"{_REPORT_HEAD}~~~\n{_SUCCESS_SECTION}~~~\n\n## Codex Cross-Check\n\n{_QUOTA_LINE}\n",
                id="fenced-success-quote-before-the-real-section",
            ),
            pytest.param(f"{_REPORT_HEAD}## Codex Cross-Check  \n\n{_QUOTA_LINE}\n", id="heading-with-trailing-spaces"),
            pytest.param(
                f"{_REPORT_HEAD}## Codex Cross-Check\n\n{_QUOTA_LINE}\n".replace("\n", "\r\n"), id="crlf-line-endings"
            ),
            pytest.param(
                f"{_REPORT_HEAD}## Codex Cross-Check notes\n\nSee below.\n\n## Codex Cross-Check\n\n{_QUOTA_LINE}\n",
                id="longer-heading-is-another-section",
            ),
        ],
    )
    def test_either_carrier_classifies(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str) -> None:
        """The anchored opening line, or a closing JSON envelope line with the key, marks the stop.

        Callers that demand no envelope only get the report, and a demanded envelope may close a reply as one JSON line.
        """
        (tmp_path / "challenge-domain-logic.md").write_text(content, encoding="utf-8")

        code = main(["--challenger", str(tmp_path), _VERDICTS, "challenge-domain-*.md"])

        assert (code, capsys.readouterr().out) == (0, "quota-exhausted\n")

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(
                f"{_REPORT_HEAD}## Codex Cross-Check\n\nCodex found two issues.\n{_QUOTA_LINE}\n",
                id="not-the-first-line",
            ),
            pytest.param(f"Note: {_QUOTA_LINE}\n## Codex Cross-Check\n\nCodex agrees.\n", id="outside-the-section"),
            pytest.param(
                f"{_REPORT_HEAD}## Codex Cross-Check\n\n  {_QUOTA_LINE}\n", id="indented-not-the-anchored-line"
            ),
            pytest.param(
                f"{_REPORT_HEAD}## Codex Cross-Check\n\n⚠ **Codex cross-check failed** — Codex quota exhausted: usage\n",
                id="failed-variant-quota-wording",
            ),
            pytest.param(f"{_REPORT_HEAD}{json.dumps(_verdicts())}\n", id="closing-envelope-without-key"),
            pytest.param(
                f"{_REPORT_HEAD}## Codex Cross-Check\n\n{_QUOTA_LINE}\n{json.dumps(_verdicts())}\n",
                id="closing-envelope-outranks-the-line",
            ),
            pytest.param(
                json.dumps({"items": [], "report": f"## Codex Cross-Check\n{_QUOTA_LINE}"}),
                id="envelope-decides-when-present",
            ),
            pytest.param(f"{_REPORT_HEAD}{_SUCCESS_SECTION}{_QUOTA_LINE}\n", id="codex-output-opens-with-the-line"),
            pytest.param(
                f"{_REPORT_HEAD}{_SUCCESS_SECTION}## Codex Cross-Check\n\n{_QUOTA_LINE}\n",
                id="codex-output-carries-its-own-heading",
            ),
            pytest.param(f"{_REPORT_HEAD}{_FENCED_QUOTE}{_SUCCESS_SECTION}", id="fenced-quote-before-the-real-section"),
            pytest.param(f"{_REPORT_HEAD}{_FENCED_QUOTE}", id="fenced-quote-without-a-real-section"),
            pytest.param(f"{_REPORT_HEAD}~~~\n## Codex Cross-Check\n\n{_QUOTA_LINE}\n~~~\n", id="tilde-fenced-quote"),
            pytest.param(
                f"{_REPORT_HEAD}````markdown\n```\n## Codex Cross-Check\n\n{_QUOTA_LINE}\n```\n````\n",
                id="quote-inside-a-nested-fence",
            ),
            pytest.param(
                f"{_REPORT_HEAD}```\n## Codex Cross-Check\n\n{_QUOTA_LINE}\n", id="quote-inside-an-unclosed-fence"
            ),
            pytest.param(
                f"{_REPORT_HEAD}## Codex Cross-Check\n\n```\n{_QUOTA_LINE}\n```\n", id="fence-opens-the-section"
            ),
            pytest.param(
                f"{_REPORT_HEAD}``` a`b\n## Codex Cross-Check\n\n{_QUOTA_LINE}\n", id="any-backtick-run-opens-a-fence"
            ),
        ],
    )
    def test_anything_else_is_not_classified(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
    ) -> None:
        """The token counts only as the first non-blank line under the heading of a reply that is not an envelope.

        Elsewhere in the report it is prose, which never decides, and a JSON envelope decides by its key alone. The
        first heading outside fenced blocks is the challenger's own: verbatim Codex output, a peer-written heading
        inside it and fenced quotes of the contract all come after or inside something else, so none of them ever
        decides.
        """
        (tmp_path / "challenge-domain-logic.md").write_text(content, encoding="utf-8")

        code = main(["--challenger", str(tmp_path), _VERDICTS, "challenge-domain-*.md"])

        assert (code, capsys.readouterr().out) == (0, "none\n")
