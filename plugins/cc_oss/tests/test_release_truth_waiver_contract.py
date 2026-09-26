"""Check release truth instructions and per-invocation waiver evidence."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import release_append_marker as ram


_RELEASE = Path(__file__).resolve().parents[1] / "skills/release"
_skip_shell_unavailable = pytest.mark.skipif(shutil.which("bash") is None, reason="Release setup uses Bash.")


def _bash_path(path: str) -> str:
    """Represent a native Python stage path for Git Bash fixture files."""
    if sys.platform != "win32":
        return path
    return subprocess.check_output(["cygpath", "-u", path], text=True).strip()


def _init_release_head(root: Path) -> str:
    """Create a real frozen HEAD for append staging contract checks."""
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=root, check=True, capture_output=True)
    (root / "source.txt").write_text("source\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _seed_marker(root: Path, sha: str) -> None:
    """Write the branch-bound marker used by append staging checks."""
    path = root / ram.state_relative("main", "marker")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n", encoding="utf-8", newline="\n")


@pytest.mark.parametrize(
    "relative_path",
    ["modes/classify-truth-check.md", "templates/gather-prompt.md"],
)
def test_removed_api_requires_baseline_presence_and_head_absence(relative_path: str) -> None:
    """A shipped API removed at HEAD must survive truth checking as Removed."""
    instructions = (_RELEASE / relative_path).read_text(encoding="utf-8")
    assert "❌ Removed" in instructions
    assert "present at `$LAST_TAG` and absent at `HEAD`" in instructions
    assert "keep the ❌ Removed claim" in instructions
    assert "absent at `$LAST_TAG`" in instructions


def test_gather_retains_qualified_removal_and_breaking_claims() -> None:
    """An unresolved baseline must leave qualified claims in the gathered change table."""
    instructions = (_RELEASE / "templates/gather-prompt.md").read_text(encoding="utf-8")
    write_contract = instructions[instructions.index("Write full findings") : instructions.index("Return ONLY:")]
    assert "<GATHER_FILE>" in write_contract
    assert "qualified" in write_contract
    assert "⚠️ Breaking Changes" in write_contract
    assert "❌ Removed" in write_contract
    assert "baseline" in write_contract


@_skip_shell_unavailable
@pytest.mark.parametrize("relative_path", ["modes/classify-truth-check.md", "templates/gather-prompt.md"])
def test_python_baseline_probe_ignores_docstring_only_name(tmp_path: Path, relative_path: str) -> None:
    """A docstring mention stays inconclusive until absence is independently established."""
    instructions = (_RELEASE / relative_path).read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL)
    probe = next(block for block in blocks if 'python - "$REF" "$SYMBOL"' in block)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    module = tmp_path / "api.py"
    module.write_text('"""The old docs showed:\n\ndef Legacy():\n    pass\n"""\n', encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "tag", "v1"], cwd=tmp_path, check=True, capture_output=True)
    module.write_text('"""No public API here."""\n', encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "remove-mention"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", "REF=v1; SYMBOL=Legacy; PUBLIC_PATH=api.py; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 2, completed.stderr
    module.write_text("def Legacy():\n    return 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "define-api"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    defined = subprocess.run(
        [shutil.which("bash"), "-c", "REF=HEAD; SYMBOL=Legacy; PUBLIC_PATH=api.py; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert defined.returncode == 0, defined.stderr


@_skip_shell_unavailable
@pytest.mark.parametrize("relative_path", ["modes/classify-truth-check.md", "templates/gather-prompt.md"])
def test_python_probe_treats_latin1_source_as_inconclusive(tmp_path: Path, relative_path: str) -> None:
    """A valid PEP-263 module must not turn decoding into an undocumented probe failure."""
    instructions = (_RELEASE / relative_path).read_text(encoding="utf-8")
    probe = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL)
        if 'python - "$REF" "$SYMBOL"' in block
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "api.py").write_bytes(b"# coding: latin-1\nlabel = 'caf\xe9'\ndef Current():\n    pass\n")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "latin1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", "REF=HEAD; SYMBOL=Legacy; PUBLIC_PATH=api.py; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 2, completed.stderr


@_skip_shell_unavailable
@pytest.mark.parametrize("setup", ["delegated", "inline"])
def test_waiver_ledger_is_unique_per_same_day_invocation(tmp_path: Path, setup: str) -> None:
    """Two invocations on one branch and date preserve separate waiver evidence."""
    skill = (_RELEASE / "SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
    candidates = [block for block in blocks if "release-waived-${CSID}" in block and "GATHER_FILE=" in block]
    assert len(candidates) == 2
    script = candidates[0 if setup == "delegated" else 1]
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "same-session"})
    for entry in ("first", "second"):
        completed = subprocess.run(
            [shutil.which("bash"), "-c", "BRANCH=main; DATE=2026-09-25; CSID=same-session; " + script],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        ledger = tmp_path / (tmp_path / "release-waived-same-session").read_text(encoding="utf-8").strip()
        assert ledger.is_file()
        if entry == "first":
            first_ledger = ledger
            ledger.write_text("REMOVED: earlier evidence\n", encoding="utf-8", newline="\n")
        else:
            assert ledger != first_ledger
            assert first_ledger.read_text(encoding="utf-8") == "REMOVED: earlier evidence\n"
            assert ledger.read_text(encoding="utf-8") == ""


@_skip_shell_unavailable
@pytest.mark.parametrize("setup", ["delegated", "inline"])
def test_waiver_ledger_uses_current_session_in_fresh_shell(tmp_path: Path, setup: str) -> None:
    """A release ledger sentinel must never be written under an empty session ID."""
    skill = (_RELEASE / "SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
    script = [block for block in blocks if "release-waived-${CSID}" in block and "GATHER_FILE=" in block][
        0 if setup == "delegated" else 1
    ]
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "fresh-session"})
    completed = subprocess.run(
        [shutil.which("bash"), "-c", "BRANCH=main; DATE=2026-09-25; unset CSID; " + script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "release-waived-fresh-session").is_file()


@_skip_shell_unavailable
@pytest.mark.parametrize("source", ["Legacy = implementation\n", "parser.add_argument('--legacy')\n"])
def test_python_probe_recognizes_public_bindings(tmp_path: Path, source: str) -> None:
    """Assignment reexports and parser flags count as Python surface declarations."""
    instructions = (_RELEASE / "modes/classify-truth-check.md").read_text(encoding="utf-8")
    probe = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL)
        if 'python - "$REF" "$SYMBOL"' in block
    )
    symbol = "Legacy" if source.startswith("Legacy") else "--legacy"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "api.py").write_text(source, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", f"REF=HEAD; SYMBOL='{symbol}'; PUBLIC_PATH=api.py; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


@_skip_shell_unavailable
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import package.impl as Legacy\n", 0, id="aliased-import"),
        pytest.param("globals()['Legacy'] = implementation\n", 2, id="dynamic-global-inconclusive"),
    ],
)
def test_python_probe_handles_alias_and_dynamic_binding(tmp_path: Path, source: str, expected: int) -> None:
    """A dynamic public binding cannot be interpreted as evidence of absence."""
    instructions = (_RELEASE / "modes/classify-truth-check.md").read_text(encoding="utf-8")
    probe = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL)
        if 'python - "$REF" "$SYMBOL"' in block
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "api.py").write_text(source, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", "REF=HEAD; SYMBOL=Legacy; PUBLIC_PATH=api.py; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == expected, completed.stderr


@_skip_shell_unavailable
@pytest.mark.parametrize(
    ("source", "symbol"),
    [
        pytest.param("if enabled:\n    def Legacy():\n        pass\n", "Legacy", id="conditional-definition"),
        pytest.param("name = 'Leg' + 'acy'\nglobals()[name] = implementation\n", "Legacy", id="computed-global"),
        pytest.param("@click.option('--legacy')\ndef command():\n    pass\n", "--legacy", id="click-decorator"),
    ],
)
@pytest.mark.parametrize("relative_path", ["modes/classify-truth-check.md", "templates/gather-prompt.md"])
def test_python_probe_fails_closed_for_unsupported_binding(
    tmp_path: Path, source: str, symbol: str, relative_path: str
) -> None:
    """Partial syntax coverage must not certify a public name as absent."""
    instructions = (_RELEASE / relative_path).read_text(encoding="utf-8")
    probe = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL)
        if 'python - "$REF" "$SYMBOL"' in block
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "api.py").write_text(source, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "api.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", f"REF=HEAD; SYMBOL='{symbol}'; PUBLIC_PATH=api.py; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 2, completed.stderr


@_skip_shell_unavailable
@pytest.mark.parametrize("relative_path", ["modes/classify-truth-check.md", "templates/gather-prompt.md"])
def test_python_probe_rejects_private_method_outside_claimed_public_module(tmp_path: Path, relative_path: str) -> None:
    """An unrelated private method cannot establish a public binding at the prior tag."""
    instructions = (_RELEASE / relative_path).read_text(encoding="utf-8")
    probe = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL)
        if 'python - "$REF" "$SYMBOL"' in block
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "private.py").write_text(
        "class Hidden:\n    def Legacy(self):\n        pass\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "public.py").write_text('"""No Legacy binding here."""\n', encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "private.py", "public.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "tag", "v1"], cwd=tmp_path, check=True, capture_output=True)
    completed = subprocess.run(
        [shutil.which("bash"), "-c", "REF=v1; SYMBOL=Legacy; PUBLIC_PATH=public.py; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 2, completed.stderr
    without_path = subprocess.run(
        [shutil.which("bash"), "-c", "REF=v1; SYMBOL=Legacy; PUBLIC_PATH=''; " + probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert without_path.returncode == 2, without_path.stderr


def test_breaking_waivers_require_item_review_and_post_merge_keeps_removals() -> None:
    """Breaking and Removed claims retain their category-specific review rules."""
    skill = (_RELEASE / "SKILL.md").read_text(encoding="utf-8")
    append = (_RELEASE / "modes/release-draft-template.md").read_text(encoding="utf-8")
    assert "UNCONFIRMED_BREAKING" in skill
    assert (
        "AskUserQuestion" in skill[skill.index("When `unconfirmed > 0`") - 900 : skill.index("When `unconfirmed > 0`")]
    )
    assert (
        "❌ Removed" in append[append.index("**Truth check re-run**") : append.index("**Identify highlights re-rank**")]
    )


def test_post_merge_removal_reconciles_changelog_and_standalone_migration() -> None:
    """An append removal must be removed from every release artifact carrying it."""
    append = (_RELEASE / "modes/release-draft-template.md").read_text(encoding="utf-8")
    final_gate = append[append.index("**Final cross-artifact truth gate**") : append.index("**Provenance record**")]
    assert "POST_MERGE_REMOVE" in final_gate
    assert "$CHANGELOG_FILE" in final_gate
    assert "MIGRATION.md" in final_gate
    assert "abort" in final_gate.lower()


@_skip_shell_unavailable
def test_append_inventories_existing_migration_without_migration_flag(tmp_path: Path) -> None:
    """An existing standalone migration artifact remains a truth-check consumer."""
    append = (_RELEASE / "modes/release-draft-template.md").read_text(encoding="utf-8")
    script = next(
        block for block in re.findall(r"```bash\n(.*?)```", append, re.DOTALL) if "release-artifacts" in block
    )
    for name in ("DRAFT.md", "CHANGELOG.md", "SUMMARY.md", "MIGRATION.md"):
        (tmp_path / name).write_text("stale claim\n", encoding="utf-8", newline="\n")
    head_sha = _init_release_head(tmp_path)
    tag_state = tmp_path / "tag-state"
    tag_state.write_bytes(
        subprocess.check_output(
            ["git", "for-each-ref", "--sort=refname", "--format=%(refname) %(objectname)", "refs/tags"], cwd=tmp_path
        )
    )
    (tmp_path / ".temp").mkdir()
    _seed_marker(tmp_path, head_sha)
    stage = subprocess.run(
        [
            sys.executable,
            str(_RELEASE.parents[1] / "bin/release_append_publish.py"),
            "begin",
            "--branch",
            "main",
            "--changelog",
            "CHANGELOG.md",
            "--head-sha",
            head_sha,
            "--start-sha",
            head_sha,
            "--range-start",
            head_sha,
            "--branch-ref",
            "main",
            "--tag-state-file",
            str(tag_state),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (tmp_path / "release-append-stage-fresh").write_text(_bash_path(stage) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-marker-valid-fresh").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-fresh").write_text("true\n", encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "fresh"})
    completed = subprocess.run(
        [shutil.which("bash"), "-c", "MARKER_VALID=true; CHANGELOG_FILE=CHANGELOG.md; " + script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        f"{_bash_path(stage)}/{name}" for name in ("DRAFT.md", "CHANGELOG.md", "SUMMARY.md", "MIGRATION.md")
    ]


@_skip_shell_unavailable
def test_append_reloads_changelog_path_in_fresh_shell(tmp_path: Path) -> None:
    """The final artifact inventory must recover the resolved changelog path."""
    append = (_RELEASE / "modes/release-draft-template.md").read_text(encoding="utf-8")
    skill = (_RELEASE / "SKILL.md").read_text(encoding="utf-8")
    script = next(
        block for block in re.findall(r"```bash\n(.*?)```", append, re.DOTALL) if "release-artifacts" in block
    )
    persist = next(
        block for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL) if "changelog path unresolved" in block
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/CHANGELOG.md").write_text("stale claim\n", encoding="utf-8", newline="\n")
    (tmp_path / "DRAFT.md").write_text("new claim\n", encoding="utf-8", newline="\n")
    head_sha = _init_release_head(tmp_path)
    tag_state = tmp_path / "tag-state"
    tag_state.write_bytes(
        subprocess.check_output(
            ["git", "for-each-ref", "--sort=refname", "--format=%(refname) %(objectname)", "refs/tags"], cwd=tmp_path
        )
    )
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "fresh"})
    (tmp_path / ".temp").mkdir()
    _seed_marker(tmp_path, head_sha)
    written = subprocess.run(
        [shutil.which("bash"), "-c", "CHANGELOG_FILE=docs/CHANGELOG.md; " + persist],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    stage = subprocess.run(
        [
            sys.executable,
            str(_RELEASE.parents[1] / "bin/release_append_publish.py"),
            "begin",
            "--branch",
            "main",
            "--changelog",
            "docs/CHANGELOG.md",
            "--head-sha",
            head_sha,
            "--start-sha",
            head_sha,
            "--range-start",
            head_sha,
            "--branch-ref",
            "main",
            "--tag-state-file",
            str(tag_state),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (tmp_path / "release-append-stage-fresh").write_text(_bash_path(stage) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-marker-valid-fresh").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-fresh").write_text("true\n", encoding="utf-8", newline="\n")
    completed = subprocess.run(
        [shutil.which("bash"), "-c", "MARKER_VALID=true; unset CHANGELOG_FILE; " + script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [f"{_bash_path(stage)}/DRAFT.md", f"{_bash_path(stage)}/docs/CHANGELOG.md"]


def test_notes_stage_before_changelog_and_publish_after_truth_gate() -> None:
    """The consumer flow must keep notes edits off live files until truth approval."""
    skill = (_RELEASE / "SKILL.md").read_text(encoding="utf-8")
    append = (_RELEASE / "modes/release-draft-template.md").read_text(encoding="utf-8")
    audit = skill[skill.index("## Audit changelog") : skill.index("## Extract contributors")]
    assert audit.index('release_append_publish.py" begin') < audit.index("If `$EDIT_CHANGELOG_FILE` exists")
    assert "add (same emoji format) to `$EDIT_CHANGELOG_FILE`" in audit
    assert "create `$EDIT_CHANGELOG_FILE`" in audit
    assert append.index("$APPEND_STAGE/DRAFT.md` — Read + Edit") < append.index("**Final cross-artifact truth gate**")
    assert append.index("**Final cross-artifact truth gate**") < append.index('release_append_publish.py" publish')
    assert '--marker-dir "$APPEND_STAGE/.temp"' in append
    assert 'PROVENANCE_FILE="$APPEND_STAGE/$PROVENANCE_FILE"' in append
    assert 'if [ "$RELEASE_MODE" = notes ]; then' in audit
    assert "inspect `$APPEND_STAGE/$CHANGELOG_FILE` for every notes run" in append
    assert "write only `$APPEND_STAGE/DRAFT.md`" in append


def test_migration_only_claim_is_independently_truth_checked() -> None:
    """A standalone migration claim cannot depend on a matching DRAFT removal."""
    append = (_RELEASE / "modes/release-draft-template.md").read_text(encoding="utf-8")
    final_gate = append[append.index("**Final cross-artifact truth gate**") : append.index("**Provenance record**")]
    assert "independently truth-check" in final_gate.lower()
    assert "MIGRATION.md" in final_gate
    assert "without a DRAFT.md match" in final_gate


def test_codemap_breaking_promotion_has_post_promotion_baseline_gate() -> None:
    """An Added claim promoted to Breaking must be checked before artifact writing."""
    instructions = (_RELEASE / "modes/classify-truth-check.md").read_text(encoding="utf-8")
    promotion = instructions[instructions.index("**Apply**:") :]
    assert "post-promotion" in promotion.lower()
    assert "$LAST_TAG" in promotion
    assert "AskUserQuestion" in promotion
    assert "before Audit changelog" in promotion


@_skip_shell_unavailable
def test_missing_waiver_ledger_stops_before_no_waivers_claim(tmp_path: Path) -> None:
    """Missing waiver evidence must fail before producing a zero-waiver artifact."""
    instructions = (_RELEASE / "modes/prepare.md").read_text(encoding="utf-8")
    script = next(
        block for block in re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL) if "waived-changes.md" in block
    )
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "missing"})
    completed = subprocess.run(
        [shutil.which("bash"), "-c", script], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False
    )
    assert completed.returncode != 0
    assert not (tmp_path / "releases/waived-changes.md").exists()


@_skip_shell_unavailable
def test_empty_ledger_with_reported_waiver_stops_before_output(tmp_path: Path) -> None:
    """A present but empty ledger cannot certify an envelope reporting a waiver."""
    instructions = (_RELEASE / "modes/prepare.md").read_text(encoding="utf-8")
    script = next(
        block for block in re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL) if "waived-changes.md" in block
    )
    (tmp_path / "release-waived-mismatch").write_text("ledger\n", encoding="utf-8", newline="\n")
    (tmp_path / "ledger").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "release-prepare-version-mismatch").write_text("v1\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-unconfirmed-mismatch").write_text("1\n", encoding="utf-8", newline="\n")
    (tmp_path / "releases/v1").mkdir(parents=True)
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "mismatch"})
    completed = subprocess.run(
        [shutil.which("bash"), "-c", script], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False
    )
    assert completed.returncode != 0
    assert not (tmp_path / "releases/v1/waived-changes.md").exists()


@_skip_shell_unavailable
def test_delegated_count_disagreement_stops_before_artifact_phase(tmp_path: Path) -> None:
    """A gather envelope cannot advance with fewer ledger entries than waivers."""
    instructions = (_RELEASE / "SKILL.md").read_text(encoding="utf-8")
    script = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", instructions, re.DOTALL)
        if "gather waiver count disagrees" in block
    )
    (tmp_path / "release-waived-mismatch").write_text("ledger\n", encoding="utf-8", newline="\n")
    (tmp_path / "ledger").write_text("", encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "mismatch"})
    completed = subprocess.run(
        [shutil.which("bash"), "-c", "UNCONFIRMED=1\n" + script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert not (tmp_path / "release-unconfirmed-mismatch").exists()


@_skip_shell_unavailable
@pytest.mark.parametrize(
    ("breaking_value", "ledger_line", "expected_success"),
    [
        pytest.param(None, "REMOVED: ⚠️ Breaking Changes: old API\n", False, id="missing-field"),
        pytest.param("1", "REMOVED: ⚠️ Breaking Changes: old API\n", False, id="string-field"),
        pytest.param(-1, "REMOVED: ⚠️ Breaking Changes: old API\n", False, id="negative-field"),
        pytest.param(0, "REMOVED: ⚠️ Breaking Changes: old API\n", False, id="ledger-mismatch"),
        pytest.param(1, "REMOVED: ⚠️ Breaking Changes: old API\n", True, id="matching-count"),
    ],
)
def test_gather_envelope_breaking_count_matches_ledger_before_artifacts(
    tmp_path: Path, breaking_value: int | str | None, ledger_line: str, expected_success: bool
) -> None:
    """The delegated breaking approval count must come from typed, item-backed evidence."""
    skill = (_RELEASE / "SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
    validate = next(block for block in blocks if "delegation validation failed" in block)
    reconcile = next(block for block in blocks if "gather waiver count disagrees" in block)
    (tmp_path / "gather.md").write_text("gathered\n", encoding="utf-8", newline="\n")
    (tmp_path / "ledger").write_text(ledger_line, encoding="utf-8", newline="\n")
    (tmp_path / "release-waived-mismatch").write_text("ledger\n", encoding="utf-8", newline="\n")
    envelope: dict[str, object] = {"status": "done", "file": "gather.md", "unconfirmed": 1, "breaking": 0}
    if breaking_value is not None:
        envelope["unconfirmed_breaking"] = breaking_value
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "mismatch", "ENVELOPE": json.dumps(envelope)}
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", validate + reconcile],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (completed.returncode == 0) is expected_success, completed.stderr


@_skip_shell_unavailable
@pytest.mark.parametrize(
    "total_count",
    [
        pytest.param(None, id="missing"),
        pytest.param("0", id="string"),
        pytest.param(-1, id="negative"),
    ],
)
def test_gather_envelope_missing_total_waiver_count_fails_closed(tmp_path: Path, total_count: int | str | None) -> None:
    """A missing or invalid total count cannot match an empty ledger."""
    skill = (_RELEASE / "SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
    validate = next(block for block in blocks if "delegation validation failed" in block)
    reconcile = next(block for block in blocks if "gather waiver count disagrees" in block)
    (tmp_path / "gather.md").write_text("gathered\n", encoding="utf-8", newline="\n")
    (tmp_path / "ledger").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "release-waived-missing").write_text("ledger\n", encoding="utf-8", newline="\n")
    envelope: dict[str, object] = {"status": "done", "file": "gather.md", "breaking": 0, "unconfirmed_breaking": 0}
    if total_count is not None:
        envelope["unconfirmed"] = total_count
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "missing",
            "ENVELOPE": json.dumps(envelope),
        }
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", validate + reconcile],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert not (tmp_path / "release-unconfirmed-missing").exists()
