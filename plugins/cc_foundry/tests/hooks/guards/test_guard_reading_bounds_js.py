"""Exercise source-reading precision, alias collection and runtime bounds through the real guards."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[3] / "hooks"


def _inspect(hook: str, command: str, cwd: Path, *node_options: str) -> subprocess.CompletedProcess[str]:
    """Inspect shell text in a disposable directory without executing that text."""
    return subprocess.run(
        ["node", *node_options, str(HOOKS / hook)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}),
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        timeout=2,
        check=False,
    )


@pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(("-c&bash -c " * 1700)[:16384], id="shell-option-repetition"),
        pytest.param("&" * 16384, id="ampersands"),
    ],
)
def test_pathological_source_text_finishes_within_two_seconds(hook: str, command: str, tmp_path: Path) -> None:
    """The size-capped worst-case source prefixes cannot stall a hook past its internal time budget."""
    started = time.monotonic()
    result = _inspect(hook, command, tmp_path)
    assert time.monotonic() - started < 2
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    ("hook", "command"),
    [
        pytest.param("commit-guard.js", "git status", id="git"),
        pytest.param("gh-write-guard.js", "gh pr view 1", id="gh"),
    ],
)
def test_expired_inspection_deadline_requests_permission(hook: str, command: str, tmp_path: Path) -> None:
    """An elapsed wall-clock budget always asks when the command names the protected program."""
    clock = tmp_path / "clock.cjs"
    clock.write_text("let now = 0; Date.now = () => (now += 1001);\n", encoding="utf-8")
    result = _inspect(hook, command, tmp_path, "--require", str(clock))
    assert result.returncode == 0, result.stderr
    verdict = json.loads(result.stdout)["hookSpecificOutput"]
    assert verdict["permissionDecision"] == "ask"
    assert "deadline" in verdict["permissionDecisionReason"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("hook", "command", "reason"),
    [
        pytest.param(
            "commit-guard.js",
            "gh pr view \"${|bash -c 'git push --force';}\"",
            "force-push is forbidden",
            id="git-nested-source",
        ),
        pytest.param(
            "gh-write-guard.js",
            "x=\"${|bash -c 'gh repo sync --force';}\"",
            "gh force update blocked",
            id="gh-nested-source",
        ),
    ],
)
def test_shell_source_retains_existing_force_blocks(hook: str, command: str, reason: str, tmp_path: Path) -> None:
    """Literal source after a raw separator retains its force ban beside unsupported enclosing syntax."""
    result = _inspect(hook, command, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("hook", "source", "reason"),
    [
        pytest.param("commit-guard.js", "git push -f origin main", "force-push is forbidden", id="git"),
        pytest.param(
            "gh-write-guard.js",
            "gh api repos/o/r/git/refs/heads/main -X PATCH -F force=true",
            "gh force update blocked",
            id="gh",
        ),
    ],
)
@pytest.mark.parametrize("redirect", [">/dev/null", "&>/dev/null", " 2>&1"])
def test_attached_shell_redirection_keeps_complete_prefix_force_ban(
    hook: str, source: str, reason: str, redirect: str, tmp_path: Path
) -> None:
    """A shell's attached output redirection cannot hide its literal source in a complete oversized prefix."""
    result = _inspect(hook, f"bash{redirect} -c '{source}'; " + "x" * 17000, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        'g"i"t -c alias.setup=\'config alias.p "push --force origin main"\' setup; git p',
        "git -c alias.setup='config alias.p \"push --force origin main\"' setup; git p",
        "git -c alias.setup='!git config alias.p \"push --force origin main\"' setup; git p",
        "git p; git -c alias.setup='config alias.p \"push --force origin main\"' setup",
        "git p; git stage; git -c alias.setup='config alias.stage \"config alias.p push\"' setup; git p --force",
    ],
)
def test_alias_expansions_collect_configured_aliases(command: str, tmp_path: Path) -> None:
    """Aliases that run config contribute their assignments before any force-push invocation is judged."""
    result = _inspect("commit-guard.js", command, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "force-push is forbidden" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "form",
    ["-- alias.p 'push --force origin main'", "-- alias.p 'push --force origin main' --old"],
)
def test_config_option_terminator_keeps_force_alias_visible(form: str, tmp_path: Path) -> None:
    """The config option terminator leaves all remaining arguments as operands."""
    result = _inspect("commit-guard.js", f"git config {form}; git p", tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "force-push is forbidden" in result.stderr


@pytest.mark.parametrize(
    "form",
    [
        "-f .gitmodules --get-regexp path",
        "--blob HEAD:.gitmodules --list",
        "--global -e",
        "-z --list",
        "--remove-section foo",
        "--get-color color.diff.new red",
        "--edit",
        "-e",
    ],
)
def test_config_without_alias_key_has_no_verdict(form: str, tmp_path: Path) -> None:
    """Unmodeled config options cannot introduce an alias assignment without naming an alias key."""
    result = _inspect("commit-guard.js", f"git config {form}; git p", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    ("hook", "command", "padding", "expected_code"),
    [
        pytest.param("commit-guard.js", "git push origin main --follow-tags", 16348, 0, id="git"),
        pytest.param("gh-write-guard.js", "gh repo sync --force", 16360, 2, id="gh"),
    ],
)
def test_cut_oversize_segment_is_never_read_as_complete(
    hook: str, command: str, padding: int, expected_code: int, tmp_path: Path
) -> None:
    """Only complete words before the cap can supply force flags to the coarse reader."""
    result = _inspect(hook, "echo " + "a" * padding + "; " + command, tmp_path)
    assert result.returncode == expected_code, (result.stdout, result.stderr)
    assert "force" not in result.stderr


def test_cut_git_flag_requests_size_permission(tmp_path: Path) -> None:
    """An incomplete follow-tags flag cannot be reinterpreted as a force option or an approved plain push."""
    result = _inspect("commit-guard.js", "echo " + "a" * 16348 + "; git push origin main --follow-tags", tmp_path)
    assert result.returncode == 0, (result.stdout, result.stderr)
    verdict = json.loads(result.stdout)["hookSpecificOutput"]
    assert verdict["permissionDecision"] == "ask"
    assert "size limit" in verdict["permissionDecisionReason"]
    assert result.stderr == ""


def test_cut_gh_force_flag_keeps_non_force_write_gate(tmp_path: Path) -> None:
    """Dropping an incomplete force option keeps the complete repo sync write subject to its token gate."""
    result = _inspect("gh-write-guard.js", "echo " + "a" * 16360 + "; gh repo sync --force", tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "gh write blocked — gh repo sync" in result.stderr
    assert "gh force update blocked" not in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("hook", "tail"),
    [
        pytest.param("commit-guard.js", "git push --force origin main", id="git"),
        pytest.param("gh-write-guard.js", "gh repo sync --force", id="gh-force"),
        pytest.param("gh-write-guard.js", "gh issue create -t x -b y", id="gh-write"),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "echo ${!x}; true & COMMAND",
        "echo ${!x}; (COMMAND)",
        "echo ${!x}; x=$(COMMAND)",
        "echo ${!x}; x=`COMMAND`",
        "echo ${!x}; >f COMMAND",
        'echo ${!x}; eval "COMMAND"',
        "echo ${x:=ok}; env bash -c 'COMMAND'",
        "echo ${!x}; bash -c $'COMMAND'",
        "cat <<EOF\nhello\nEOF\necho ${!x}; COMMAND",
        "echo $((1<<2)); echo ${!x}; COMMAND",
        'echo "a<<b"; echo ${!x}; COMMAND',
    ],
)
def test_unmodeled_source_requests_permission_with_write_risk(
    hook: str, tail: str, template: str, tmp_path: Path
) -> None:
    """Unreadable named commands explain that inspection may have missed a push or GitHub write."""
    result = _inspect(hook, template.replace("COMMAND", tail), tmp_path)
    assert result.returncode == 0, (result.stdout, result.stderr)
    verdict = json.loads(result.stdout)["hookSpecificOutput"]
    assert verdict["permissionDecision"] == "ask"
    assert "may hold a push or GitHub write" in verdict["permissionDecisionReason"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    "command",
    [
        "git config payload.p 'push --force origin main'; git config --rename-section payload alias; git p",
        "git config payload.p 'push --force origin main'; git config --rename-section payload ALIAS; git p",
        "git config --rename-section payload alias",
        "git config --rename-section alias payload",
        "git config rename-section payload alias",
        "git config rename-section alias payload",
        "git config --remove-section alias",
        "git config remove-section alias",
    ],
)
def test_config_alias_section_changes_request_permission(command: str, tmp_path: Path) -> None:
    """Unmodeled section operations naming aliases cannot silently install or change a push alias."""
    result = _inspect("commit-guard.js", command, tmp_path)
    assert result.returncode == 0, (result.stdout, result.stderr)
    verdict = json.loads(result.stdout)["hookSpecificOutput"]
    assert verdict["permissionDecision"] == "ask"
    assert "git config alias form" in verdict["permissionDecisionReason"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("hook", "source", "reason"),
    [
        pytest.param("commit-guard.js", "git push --force origin main", "force-push is forbidden", id="git"),
        pytest.param("gh-write-guard.js", "gh repo sync --force", "gh force update blocked", id="gh"),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "echo ${!x}; bash -c echo # it's a comment\nbash -c 'COMMAND'",
        r"echo ${!x}; bash -c $'\''; bash -c 'COMMAND'",
        "echo ${!x}; bash -c echo # it's a comment\nbash <<< 'COMMAND'",
    ],
)
def test_failed_literal_source_read_keeps_later_force_source(
    hook: str, source: str, reason: str, template: str, tmp_path: Path
) -> None:
    """A quote in unmodeled source cannot skip a later complete literal shell operand."""
    result = _inspect(hook, template.replace("COMMAND", source), tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("hook", "command", "reason"),
    [
        pytest.param("commit-guard.js", "git push --force origin main", "force-push is forbidden", id="git-force"),
        pytest.param("commit-guard.js", "git push origin +main", "force-push is forbidden", id="git-refspec"),
        pytest.param(
            "commit-guard.js", "git status; git push --force origin main", "force-push is forbidden", id="git-last"
        ),
        pytest.param("gh-write-guard.js", "gh repo sync --force", "gh force update blocked", id="gh-force"),
        pytest.param("gh-write-guard.js", "gh issue create -t x -b y", "gh write blocked", id="gh-write"),
    ],
)
def test_complete_write_words_before_oversize_cut_still_block(
    hook: str, command: str, reason: str, tmp_path: Path
) -> None:
    """Truncating a final argument retains complete force flags, refspecs and write subcommands before it."""
    result = _inspect(hook, command + " word" * 9000, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("boundary", [" ", "\t", "\n", ";", "&", "|", "<", ">", "(", ")"])
@pytest.mark.parametrize(
    ("hook", "command", "reason", "partial"),
    [
        pytest.param("commit-guard.js", "git push --force", "force-push is forbidden", False, id="git-force-at-cap"),
        pytest.param("commit-guard.js", "git push --force", "force-push is forbidden", True, id="git-partial-word"),
        pytest.param(
            "gh-write-guard.js", "gh repo sync --force", "gh force update blocked", False, id="gh-force-at-cap"
        ),
        pytest.param("gh-write-guard.js", "gh issue create", "gh write blocked", False, id="gh-write-at-cap"),
    ],
)
def test_complete_write_word_at_size_cap_still_blocks(
    hook: str, command: str, reason: str, boundary: str, partial: bool, tmp_path: Path
) -> None:
    """A shell word boundary at or before the cap preserves complete force flags and write subcommands."""
    padding = 16384 - len(command) - 7 - (5 if partial else 0)
    text = "echo " + "a" * padding + "; " + command + boundary + "word" * 10
    result = _inspect(hook, text, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("hook", "command", "reason"),
    [
        pytest.param(
            "commit-guard.js", 'gh pr view "${|git push -f origin main;}"', "force-push is forbidden", id="funsub"
        ),
        pytest.param(
            "commit-guard.js",
            'echo ${!x}; echo "$(printf \'"\')"; git push --force; echo "ok"',
            "force-push is forbidden",
            id="nested-double-quote",
        ),
        pytest.param(
            "gh-write-guard.js",
            'echo ${!x}; echo "$(printf \'"\')"; gh repo sync --force; echo "ok"',
            "gh force update blocked",
            id="gh-nested-double-quote",
        ),
        pytest.param(
            "commit-guard.js",
            r"echo {fd}>f; echo a\&& git push --force",
            "force-push is forbidden",
            id="git-escaped-ampersand",
        ),
        pytest.param(
            "gh-write-guard.js",
            r"echo {fd}>f; echo a\&& gh repo sync --force",
            "gh force update blocked",
            id="gh-escaped-ampersand",
        ),
    ],
)
def test_dynamic_quoted_source_keeps_force_verdict(hook: str, command: str, reason: str, tmp_path: Path) -> None:
    """Uncertain nested quote boundaries retain raw force detections when the exact reader stops."""
    result = _inspect(hook, command, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert reason in result.stderr
    assert result.stdout == ""
