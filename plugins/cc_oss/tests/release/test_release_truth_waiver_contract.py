"""Check release truth instructions and per-invocation waiver evidence.

Two kinds of test share this file. Static ones read the shipped release instructions as prose, whitespace-normalised so
a reflowed paragraph cannot break them, and assert the rules they state. Shell ones extract the bash blocks those
instructions ship and run them through real Bash, git and the plugin's ``bin/`` scripts, because the block text running
end to end is the contract; none of those boundaries is mocked.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import release_append_marker as ram
from _text_io import bash_blocks, bash_path

_PLUGIN = Path(__file__).resolve().parents[2]
_RELEASE = _PLUGIN / "skills/release"
#: The truth-check probe runs ``${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/symbol_exists_at_ref.py`` from a scratch
#: repo, where the bare ``plugins/cc_oss`` fallback resolves to nothing; point it at this plugin (forward slashes work
#: in Git Bash).
_PROBE_ENV = {**os.environ, "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix()}
_PROBE_MARKER = "symbol_exists_at_ref.py"
_CLASSIFY = "modes/classify-truth-check.md"
_GATHER = "templates/gather-prompt.md"
_SITES = [pytest.param(_CLASSIFY, id="classify"), pytest.param(_GATHER, id="gather")]
_LATIN1_SOURCE = b"# coding: latin-1\nlabel = 'caf\xe9'\ndef Current():\n    pass\n"
_CONDITIONAL_DEF = b"if enabled:\n    def Legacy():\n        pass\n"
_COMPUTED_GLOBAL = b"name = 'Leg' + 'acy'\nglobals()[name] = implementation\n"
_CLICK_OPTION = b"@click.option('--legacy')\ndef command():\n    pass\n"
_DOCSTRING_ONLY = b'"""The old docs showed:\n\ndef Legacy():\n    pass\n"""\n'
_GATHER_BASE = {"status": "done", "file": "gather.md", "breaking": 0}
_BREAKING_LEDGER = "REMOVED: ⚠️ Breaking Changes: old API\n"
_skip_shell_unavailable = pytest.mark.skipif(shutil.which("bash") is None, reason="Release setup uses Bash.")


def _raw(relative: str) -> str:
    """Read one shipped release-skill file exactly as written."""
    return (_RELEASE / relative).read_text(encoding="utf-8")


def _prose(relative: str) -> str:
    """Read one shipped release-skill file with all whitespace runs collapsed to single spaces.

    Phrase assertions then hold however a paragraph is wrapped or indented.
    """
    return " ".join(_raw(relative).split())


def _block(relative: str, marker: str) -> str:
    """Return the first bash block of ``relative`` that contains ``marker``."""
    return next(block for block in bash_blocks(_raw(relative)) if marker in block)


def _probe_block(relative: str) -> str:
    """Pick the truth-probe bash block, with the spawn-time ``<OSS_BIN>`` placeholder bound to this plugin's bin dir.

    The gather template runs in a subagent shell that has no ``CLAUDE_PLUGIN_ROOT``, so the orchestrator substitutes an
    absolute bin path for ``<OSS_BIN>`` before spawning; this performs the same substitution.
    """
    return _block(relative, _PROBE_MARKER).replace("<OSS_BIN>", (_PLUGIN / "bin").as_posix())


def _waiver_ledger_blocks() -> list[str]:
    """Return the two waiver-ledger setup blocks: the delegated procedure's first, then SKILL.md's inline one."""
    blocks = bash_blocks(_raw("modes/delegation.md")) + bash_blocks(_raw("SKILL.md"))
    candidates = [block for block in blocks if "release-waived-${CSID}" in block and "mktemp" in block]
    assert len(candidates) == 2
    return candidates


def _session_env(tmp_path: Path, session: str, **extra: str) -> dict[str, str]:
    """Return the process environment with ``TMPDIR`` and the session id redirected for a shell block."""
    return {**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": session, **extra}


def _bash(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``script`` through Bash in ``cwd`` and capture its output."""
    return subprocess.run(
        [shutil.which("bash"), "-c", script], cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=30
    )


def _git(repo: Path, *args: str) -> None:
    """Run one git command in ``repo`` with a fixed identity."""
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run(["git", *identity, *args], cwd=repo, check=True, capture_output=True)


def _commit_files(repo: Path, files: dict[str, bytes]) -> None:
    """Initialise ``repo`` if needed, then commit ``files`` (name to bytes) as one baseline commit."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    for name, data in files.items():
        (repo / name).write_bytes(data)
    _git(repo, "add", *files)
    _git(repo, "commit", "-qm", "baseline")


def _run_probe(repo: Path, relative: str, symbol: str, public_path: str) -> subprocess.CompletedProcess[str]:
    """Run the truth-probe block of ``relative`` for ``symbol`` in ``public_path`` at ``HEAD``."""
    script = f"REF=HEAD; SYMBOL='{symbol}'; PUBLIC_PATH='{public_path}'; " + _probe_block(relative)
    return _bash(script, repo, _PROBE_ENV)


def _init_release_head(root: Path) -> str:
    """Create a real frozen HEAD for append staging contract checks."""
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True, capture_output=True)
    (root / "source.txt").write_text("source\n", encoding="utf-8", newline="\n")
    _git(root, "add", "source.txt")
    _git(root, "commit", "-qm", "source")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _seed_marker(root: Path, sha: str) -> None:
    """Write the branch-bound marker used by append staging checks."""
    path = root / ram.state_relative("main", "marker")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n", encoding="utf-8", newline="\n")


def _prepare_append_repo(root: Path) -> tuple[str, Path]:
    """Freeze a release HEAD in ``root`` with its tag snapshot and marker; return the head SHA and snapshot file."""
    head_sha = _init_release_head(root)
    tag_state = root / "tag-state"
    tag_state.write_bytes(
        subprocess.check_output(
            ["git", "for-each-ref", "--sort=refname", "--format=%(refname) %(objectname)", "refs/tags"], cwd=root
        )
    )
    (root / ".temp").mkdir()
    _seed_marker(root, head_sha)
    return head_sha, tag_state


def _begin_stage(root: Path, head_sha: str, tag_state: Path, changelog: str) -> str:
    """Run ``release_append_publish.py begin`` and record the sentinels the append block reads; return the stage dir."""
    begin_args = ["--branch", "main", "--changelog", changelog, "--head-sha", head_sha, "--start-sha", head_sha]
    range_args = ["--range-start", head_sha, "--branch-ref", "main", "--tag-state-file", str(tag_state)]
    stage = subprocess.run(
        [sys.executable, str(_PLUGIN / "bin/release_append_publish.py"), "begin", *begin_args, *range_args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for sentinel, value in (
        ("release-append-stage-fresh", bash_path(stage)),
        ("release-marker-valid-fresh", "true"),
        ("release-do-append-fresh", "true"),
    ):
        (root / sentinel).write_text(value + "\n", encoding="utf-8", newline="\n")
    return stage


def _gather_text() -> str:
    """Return the gather template's ``Write full findings`` section, up to its ``Return ONLY:`` line."""
    template = _prose(_GATHER)
    return template[template.index("Write full findings") : template.index("Return ONLY:")]


def _final_gate_text() -> str:
    """Return the append template's final cross-artifact truth gate section."""
    append = _prose("modes/release-draft-template.md")
    return append[append.index("**Final cross-artifact truth gate**") : append.index("**Provenance record**")]


class TestTruthCheckInstructions:
    """The shipped instructions keep every truth-check rule that guards Removed and Breaking claims."""

    @pytest.mark.parametrize("relative_path", [_CLASSIFY, _GATHER])
    def test_removed_api_requires_baseline_presence_and_head_absence(self, relative_path: str) -> None:
        """A shipped API removed at HEAD must survive truth checking as Removed.

        Both truth-check sites must demand presence at ``$LAST_TAG`` and absence at ``HEAD``, and keep the claim when
        the baseline cannot prove otherwise; dropping any phrase lets a real removal vanish from the release notes.
        """
        instructions = _prose(relative_path)

        assert all(
            phrase in instructions
            for phrase in (
                "❌ Removed",
                "present at `$LAST_TAG` and absent at `HEAD`",
                "keep the ❌ Removed claim",
                "absent at `$LAST_TAG`",
            )
        )

    def test_gather_retains_qualified_removal_and_breaking_claims(self) -> None:
        """An unresolved baseline must leave qualified claims in the gathered change table.

        The gather file is the only record the later phases read, so a claim dropped here is lost for the release.
        """
        write_contract = _gather_text()

        assert all(
            phrase in write_contract
            for phrase in ("<GATHER_FILE>", "qualified", "⚠️ Breaking Changes", "❌ Removed", "baseline")
        )

    def test_gather_template_receives_absolute_bin_dir(self) -> None:
        """The gather subagent's shell has no ``CLAUDE_PLUGIN_ROOT``, so its bin calls use a spawn-time absolute path.

        With the env fallback ``plugins/cc_oss``, which does not exist in the user's repository, every probe exits as
        inconclusive and the truth check degrades silently. The orchestrator resolves the directory once and substitutes
        it.
        """
        template = _prose(_GATHER)
        delegation = _prose("modes/delegation.md")

        assert (
            "CLAUDE_PLUGIN_ROOT" not in template,
            template.count("<OSS_BIN>/"),
            "`<OSS_BIN>`" in delegation,
            "OSS_BIN=$OSS_BIN" in delegation,
        ) == (True, 2, True, True)

    @pytest.mark.parametrize("relative_path", [_CLASSIFY, _GATHER])
    def test_truth_probe_is_bin_script_not_inline_heredoc(self, relative_path: str) -> None:
        """Each truth-check site calls the one probe script instead of carrying a pasted AST heredoc.

        A heredoc-carrying block is not allow-listed and drops out of blueprint extraction, so it prompted on every
        symbol checked; the two sites also held byte-identical copies that could drift apart.
        """
        instructions = _raw(relative_path)

        assert (instructions.count(_PROBE_MARKER), "<<'PY'" in instructions) == (1, False)

    def test_breaking_waivers_require_item_review_and_post_merge_keeps_removals(self) -> None:
        """Breaking and Removed claims retain their category-specific review rules.

        Unconfirmed breaking items must be put to the user one by one, and the post-merge truth check must still treat a
        Removed claim as a claim to re-verify.
        """
        skill = _prose("SKILL.md")
        append = _prose("modes/release-draft-template.md")
        gate = skill.index("When `unconfirmed > 0`")

        assert "UNCONFIRMED_BREAKING" in skill
        assert "AskUserQuestion" in skill[gate - 900 : gate]
        assert (
            "❌ Removed"
            in append[append.index("**Truth check re-run**") : append.index("**Identify highlights re-rank**")]
        )

    def test_post_merge_removal_reconciles_changelog_and_standalone_migration(self) -> None:
        """An append removal must be removed from every release artifact carrying it.

        The final gate has to reach the changelog and the standalone migration file, and abort rather than publish a
        release where one artifact still carries the removed claim.
        """
        final_gate = _final_gate_text()

        assert all(phrase in final_gate for phrase in ("POST_MERGE_REMOVE", "$CHANGELOG_FILE", "MIGRATION.md"))
        assert "abort" in final_gate.lower()

    def test_notes_stage_before_changelog_and_publish_after_truth_gate(self) -> None:
        """The consumer flow must keep notes edits off live files until truth approval.

        Staging has to begin before any changelog edit, and publishing has to come after the final truth gate, so a
        rejected truth check leaves the live changelog untouched.
        """
        skill = _prose("SKILL.md")
        append = _prose("modes/release-draft-template.md")
        audit = skill[skill.index("## Audit changelog") : skill.index("## Extract contributors")]

        assert audit.index('release_append_publish.py" begin') < audit.index("If `$EDIT_CHANGELOG_FILE` exists")
        assert append.index("$APPEND_STAGE/DRAFT.md` — Read + Edit") < append.index(
            "**Final cross-artifact truth gate**"
        )
        assert append.index("**Final cross-artifact truth gate**") < append.index('release_append_publish.py" publish')
        assert all(
            phrase in audit
            for phrase in (
                "add (same emoji format) to `$EDIT_CHANGELOG_FILE`",
                "create `$EDIT_CHANGELOG_FILE`",
                'if [ "$RELEASE_MODE" = notes ]; then',
            )
        )
        assert all(
            phrase in append
            for phrase in (
                '--marker-dir "$APPEND_STAGE/.temp"',
                'PROVENANCE_FILE="$APPEND_STAGE/$PROVENANCE_FILE"',
                "inspect `$APPEND_STAGE/$CHANGELOG_FILE` for every notes run",
                "write only `$APPEND_STAGE/DRAFT.md`",
            )
        )

    def test_migration_only_claim_is_independently_truth_checked(self) -> None:
        """A standalone migration claim cannot depend on a matching DRAFT removal.

        The final gate must truth-check ``MIGRATION.md`` on its own, including claims with no ``DRAFT.md`` match.
        """
        final_gate = _final_gate_text()

        assert "independently truth-check" in final_gate.lower()
        assert all(phrase in final_gate for phrase in ("MIGRATION.md", "without a DRAFT.md match"))

    def test_codemap_breaking_promotion_has_post_promotion_baseline_gate(self) -> None:
        """An Added claim promoted to Breaking must be checked before artifact writing.

        Promotion changes the claim's category, so the baseline check and the user question must run again before the
        Audit changelog step writes anything.
        """
        instructions = _prose(_CLASSIFY)
        promotion = instructions[instructions.index("**Apply**:") :]

        assert "post-promotion" in promotion.lower()
        assert all(phrase in promotion for phrase in ("$LAST_TAG", "AskUserQuestion", "before Audit changelog"))


@_skip_shell_unavailable
class TestSymbolProbe:
    """The baseline probe block, run through Bash against a real scratch repository, fails closed."""

    @pytest.mark.parametrize(
        ("relative_path", "source", "symbol", "expected"),
        [
            pytest.param(_CLASSIFY, _LATIN1_SOURCE, "Legacy", 2, id="latin1-source-inconclusive-classify"),
            pytest.param(_GATHER, _LATIN1_SOURCE, "Legacy", 2, id="latin1-source-inconclusive-gather"),
            pytest.param(_CLASSIFY, b"Legacy = implementation\n", "Legacy", 0, id="assignment-reexport-is-declared"),
            pytest.param(_CLASSIFY, b"parser.add_argument('--legacy')\n", "--legacy", 0, id="parser-flag-is-declared"),
            pytest.param(_CLASSIFY, b"import package.impl as Legacy\n", "Legacy", 0, id="aliased-import-is-declared"),
            pytest.param(
                _CLASSIFY, b"globals()['Legacy'] = implementation\n", "Legacy", 2, id="dynamic-global-inconclusive"
            ),
            pytest.param(_CLASSIFY, _CONDITIONAL_DEF, "Legacy", 2, id="conditional-definition-classify"),
            pytest.param(_GATHER, _CONDITIONAL_DEF, "Legacy", 2, id="conditional-definition-gather"),
            pytest.param(_CLASSIFY, _COMPUTED_GLOBAL, "Legacy", 2, id="computed-global-classify"),
            pytest.param(_GATHER, _COMPUTED_GLOBAL, "Legacy", 2, id="computed-global-gather"),
            pytest.param(_CLASSIFY, _CLICK_OPTION, "--legacy", 2, id="click-decorator-classify"),
            pytest.param(_GATHER, _CLICK_OPTION, "--legacy", 2, id="click-decorator-gather"),
            pytest.param(_CLASSIFY, _DOCSTRING_ONLY, "Legacy", 2, id="docstring-only-name-classify"),
            pytest.param(_GATHER, _DOCSTRING_ONLY, "Legacy", 2, id="docstring-only-name-gather"),
            pytest.param(_CLASSIFY, b"def Legacy():\n    return 1\n", "Legacy", 0, id="defined-function-classify"),
            pytest.param(_GATHER, b"def Legacy():\n    return 1\n", "Legacy", 0, id="defined-function-gather"),
        ],
    )
    def test_exit_code_for_source_binding(
        self, tmp_path: Path, relative_path: str, source: bytes, symbol: str, expected: int
    ) -> None:
        """The probe certifies only bindings it can read: 0 for a declared name, 2 when inconclusive.

        Assignment re-exports, parser flags, aliased imports and real definitions count as declarations (exit 0). A
        valid PEP-263 module, a dynamic or computed public binding, a conditional definition, a click decorator and a
        name that only appears in a docstring must not be read as evidence of absence: the probe fails closed (exit 2)
        instead of failing on decoding or certifying the name as absent.
        """
        _commit_files(tmp_path, {"api.py": source})

        completed = _run_probe(tmp_path, relative_path, symbol, "api.py")

        assert completed.returncode == expected, completed.stderr

    @pytest.mark.parametrize("relative_path", _SITES)
    @pytest.mark.parametrize(
        "public_path", [pytest.param("public.py", id="claimed-module"), pytest.param("", id="no-path")]
    )
    def test_rejects_private_method_outside_claimed_public_module(
        self, tmp_path: Path, relative_path: str, public_path: str
    ) -> None:
        """An unrelated private method cannot establish a public binding at the prior tag.

        ``private.py`` defines ``Hidden.Legacy`` while the claimed ``public.py`` carries no binding; both the claimed
        module and an empty ``PUBLIC_PATH`` (module unknown) must stay inconclusive.
        """
        files = {
            "private.py": b"class Hidden:\n    def Legacy(self):\n        pass\n",
            "public.py": b'"""No binding."""\n',
        }
        _commit_files(tmp_path, files)

        completed = _run_probe(tmp_path, relative_path, "Legacy", public_path)

        assert completed.returncode == 2, completed.stderr


@_skip_shell_unavailable
class TestWaiverLedger:
    """The waiver-ledger setup blocks give every invocation its own evidence file, keyed by the live session."""

    @pytest.mark.parametrize("index", [pytest.param(0, id="delegated"), pytest.param(1, id="inline")])
    def test_ledger_is_unique_per_same_day_invocation(self, tmp_path: Path, index: int) -> None:
        """Two invocations on one branch and date preserve separate waiver evidence.

        The second run must create a fresh empty ledger instead of reusing or truncating the first run's file, so
        waivers recorded earlier in the day are never erased.
        """
        script = "BRANCH=main; DATE=2026-09-25; CSID=same-session; " + _waiver_ledger_blocks()[index]
        env = _session_env(tmp_path, "same-session")
        _bash(script, tmp_path, env).check_returncode()
        first = tmp_path / (tmp_path / "release-waived-same-session").read_text(encoding="utf-8").strip()
        first.write_text("REMOVED: earlier evidence\n", encoding="utf-8", newline="\n")

        completed = _bash(script, tmp_path, env)

        second = tmp_path / (tmp_path / "release-waived-same-session").read_text(encoding="utf-8").strip()
        assert completed.returncode == 0, completed.stderr
        assert (second != first, first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8")) == (
            True,
            "REMOVED: earlier evidence\n",
            "",
        )

    @pytest.mark.parametrize("index", [pytest.param(0, id="delegated"), pytest.param(1, id="inline")])
    def test_ledger_uses_current_session_in_fresh_shell(self, tmp_path: Path, index: int) -> None:
        """A release ledger sentinel is never written under an empty session ID.

        A fresh shell has no ``CSID``; the block must derive it from ``CLAUDE_CODE_SESSION_ID`` before naming the
        sentinel.
        """
        script = "BRANCH=main; DATE=2026-09-25; unset CSID; " + _waiver_ledger_blocks()[index]

        completed = _bash(script, tmp_path, _session_env(tmp_path, "fresh-session"))

        assert completed.returncode == 0, completed.stderr
        assert (tmp_path / "release-waived-fresh-session").is_file()


@_skip_shell_unavailable
class TestPrepareWaiverGate:
    """Prepare and the delegated gather step stop when waiver evidence is missing or disagrees with the envelope."""

    def test_missing_ledger_stops_before_no_waivers_claim(self, tmp_path: Path) -> None:
        """Missing waiver evidence must fail before producing a zero-waiver artifact.

        With no ledger sentinel for the session, writing ``waived-changes.md`` would certify "no waivers" without
        evidence.
        """
        script = _block("modes/prepare.md", "waived-changes.md")

        completed = _bash(script, tmp_path, _session_env(tmp_path, "missing"))

        assert completed.returncode != 0
        assert not (tmp_path / "releases/waived-changes.md").exists()

    def test_empty_ledger_with_reported_waiver_stops_before_output(self, tmp_path: Path) -> None:
        """A present but empty ledger cannot certify an envelope reporting a waiver.

        The envelope says one item is unconfirmed while the ledger holds nothing, so the artifact must not be written.
        """
        script = _block("modes/prepare.md", "waived-changes.md")
        (tmp_path / "release-waived-mismatch").write_text("ledger\n", encoding="utf-8", newline="\n")
        (tmp_path / "ledger").write_text("", encoding="utf-8", newline="\n")
        (tmp_path / "release-prepare-version-mismatch").write_text("v1\n", encoding="utf-8", newline="\n")
        (tmp_path / "release-unconfirmed-mismatch").write_text("1\n", encoding="utf-8", newline="\n")
        (tmp_path / "releases/v1").mkdir(parents=True)

        completed = _bash(script, tmp_path, _session_env(tmp_path, "mismatch"))

        assert completed.returncode != 0
        assert not (tmp_path / "releases/v1/waived-changes.md").exists()

    def test_delegated_count_disagreement_stops_before_artifact_phase(self, tmp_path: Path) -> None:
        """A gather envelope cannot advance with fewer ledger entries than waivers.

        ``UNCONFIRMED=1`` against an empty ledger must fail the reconcile block and leave no unconfirmed-count sentinel
        for the artifact phase to trust.
        """
        script = _block("modes/delegation.md", "gather waiver count disagrees")
        (tmp_path / "release-waived-mismatch").write_text("ledger\n", encoding="utf-8", newline="\n")
        (tmp_path / "ledger").write_text("", encoding="utf-8", newline="\n")

        completed = _bash("UNCONFIRMED=1\n" + script, tmp_path, _session_env(tmp_path, "mismatch"))

        assert completed.returncode != 0
        assert not (tmp_path / "release-unconfirmed-mismatch").exists()

    @pytest.mark.parametrize(
        ("envelope", "ledger_text", "expected_success"),
        [
            pytest.param({**_GATHER_BASE, "unconfirmed": 1}, _BREAKING_LEDGER, False, id="breaking-field-missing"),
            pytest.param(
                {**_GATHER_BASE, "unconfirmed": 1, "unconfirmed_breaking": "1"},
                _BREAKING_LEDGER,
                False,
                id="breaking-field-string",
            ),
            pytest.param(
                {**_GATHER_BASE, "unconfirmed": 1, "unconfirmed_breaking": -1},
                _BREAKING_LEDGER,
                False,
                id="breaking-field-negative",
            ),
            pytest.param(
                {**_GATHER_BASE, "unconfirmed": 1, "unconfirmed_breaking": 0},
                _BREAKING_LEDGER,
                False,
                id="breaking-ledger-mismatch",
            ),
            pytest.param(
                {**_GATHER_BASE, "unconfirmed": 1, "unconfirmed_breaking": 1},
                _BREAKING_LEDGER,
                True,
                id="breaking-matching-count",
            ),
            pytest.param({**_GATHER_BASE, "unconfirmed_breaking": 0}, "", False, id="total-field-missing"),
            pytest.param({**_GATHER_BASE, "unconfirmed": "0", "unconfirmed_breaking": 0}, "", False, id="total-string"),
            pytest.param(
                {**_GATHER_BASE, "unconfirmed": -1, "unconfirmed_breaking": 0}, "", False, id="total-negative"
            ),
        ],
    )
    def test_gather_envelope_counts_must_match_ledger(
        self, tmp_path: Path, envelope: dict[str, object], ledger_text: str, expected_success: bool
    ) -> None:
        """Delegated approval counts come from typed, ledger-backed evidence or the run stops.

        A missing, string or negative ``unconfirmed``/``unconfirmed_breaking`` count and a count the waiver ledger does
        not back must fail closed; only a typed count equal to the ledger lets the artifact phase start.
        """
        blocks = bash_blocks(_raw("modes/delegation.md"))
        validate = next(block for block in blocks if "delegation validation failed" in block)
        reconcile = next(block for block in blocks if "gather waiver count disagrees" in block)
        (tmp_path / "gather.md").write_text("gathered\n", encoding="utf-8", newline="\n")
        (tmp_path / "ledger").write_text(ledger_text, encoding="utf-8", newline="\n")
        (tmp_path / "release-waived-mismatch").write_text("ledger\n", encoding="utf-8", newline="\n")
        env = _session_env(tmp_path, "mismatch", ENVELOPE=json.dumps(envelope))

        completed = _bash(validate + reconcile, tmp_path, env)

        assert (completed.returncode == 0, (tmp_path / "release-unconfirmed-mismatch").exists()) == (
            expected_success,
            expected_success,
        ), completed.stderr


@_skip_shell_unavailable
class TestAppendInventory:
    """The final artifact inventory of append mode lists every staged artifact (real git, staging script and Bash)."""

    def test_includes_existing_migration_without_migration_flag(self, tmp_path: Path) -> None:
        """An existing standalone migration artifact remains a truth-check consumer.

        Four stale artifacts exist and no migration flag is set; the inventory must still list ``MIGRATION.md`` so its
        claims are truth-checked with the rest.
        """
        script = _block("modes/release-draft-template.md", "release-artifacts")
        for name in ("DRAFT.md", "CHANGELOG.md", "SUMMARY.md", "MIGRATION.md"):
            (tmp_path / name).write_text("stale claim\n", encoding="utf-8", newline="\n")
        head_sha, tag_state = _prepare_append_repo(tmp_path)
        stage = _begin_stage(tmp_path, head_sha, tag_state, "CHANGELOG.md")

        completed = _bash(
            "MARKER_VALID=true; CHANGELOG_FILE=CHANGELOG.md; " + script, tmp_path, _session_env(tmp_path, "fresh")
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines() == [
            f"{bash_path(stage)}/{name}" for name in ("DRAFT.md", "CHANGELOG.md", "SUMMARY.md", "MIGRATION.md")
        ]

    def test_reloads_changelog_path_in_fresh_shell(self, tmp_path: Path) -> None:
        """The final artifact inventory must recover the resolved changelog path.

        The changelog path was persisted by an earlier shell; in a fresh shell with ``CHANGELOG_FILE`` unset the block
        has to read it back, or the nested ``docs/CHANGELOG.md`` would be missing from the truth check.
        """
        script = _block("modes/release-draft-template.md", "release-artifacts")
        persist = _block("SKILL.md", "changelog path unresolved")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/CHANGELOG.md").write_text("stale claim\n", encoding="utf-8", newline="\n")
        (tmp_path / "DRAFT.md").write_text("new claim\n", encoding="utf-8", newline="\n")
        head_sha, tag_state = _prepare_append_repo(tmp_path)
        env = _session_env(tmp_path, "fresh")
        _bash("CHANGELOG_FILE=docs/CHANGELOG.md; " + persist, tmp_path, env).check_returncode()
        stage = _begin_stage(tmp_path, head_sha, tag_state, "docs/CHANGELOG.md")

        completed = _bash("MARKER_VALID=true; unset CHANGELOG_FILE; " + script, tmp_path, env)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines() == [
            f"{bash_path(stage)}/DRAFT.md",
            f"{bash_path(stage)}/docs/CHANGELOG.md",
        ]
