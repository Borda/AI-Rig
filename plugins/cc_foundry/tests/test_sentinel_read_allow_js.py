"""Subprocess tests for ``hooks/sentinel-read-allow.js``.

The hook is a ``PreToolUse`` gate that auto-allows Bash commands whose ONLY
command substitutions are the plugin-blueprint sentinel-read idiom
``$(cat "${TMPDIR:-/tmp}/<name>")`` and whose every segment is read-only.
Its security contract:

* **Allow-original, never rewrite** — no ``updatedInput`` is emitted, so
  settings.json deny rules keep matching the original command string.
* **Sentinel shape only** — any non-sentinel substitution, backtick, process
  substitution, heredoc, or write-redirect passes through unchanged (empty
  stdout, exit 0) to real permission checking.
* **Read-only segments** — a whitelisted first token is required per segment;
  guarded CLIs (git, gh, rm, ...) always passthrough.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "sentinel-read-allow.js"

NODE_UNAVAILABLE = shutil.which("node") is None

SENTINEL = '"${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"'


def _run(command: str) -> dict:
    """Invoke the hook with a Bash tool payload and return parsed stdout (or {})."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run(
        ["node", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def _is_allowed(result: dict) -> bool:
    """Check whether the hook allowed the requested operation.

    Examples:
        >>> (_is_allowed({"hookSpecificOutput": {"permissionDecision": "allow"}}), _is_allowed(None))
        (True, False)
    """
    try:
        return result["hookSpecificOutput"]["permissionDecision"] == "allow"
    except (KeyError, TypeError):
        return False


# ── Blueprint sentinel reads with read-only follow-ups are allowed ────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\n'
            f"RUN_DIR=$(cat {SENTINEL})\n"
            'cat "$RUN_DIR/foundry--solution-architect.md"',
            id="observed-run-dir-cat",
        ),
        pytest.param(f'RUN_DIR="$(cat {SENTINEL})"; ls "$RUN_DIR"/*.md', id="observed-quoted-assign-ls-glob"),
        'FOUNDRY_SHARED=$(cat "${TMPDIR:-/tmp}/foundry-shared-dir-${CSID}"); '
        'cat "$FOUNDRY_SHARED/agent-spawn-protocol.md"',
        'V=$(cat ${TMPDIR:-/tmp}/dev-review-run-dir-123 2>/dev/null || echo "")',
        pytest.param(f'V=$(cat {SENTINEL} 2>/dev/null || echo "$CLEAN_ARGS")', id="default-from-variable"),
        pytest.param(f'V=$(cat {SENTINEL}); grep -c "verdict" "$V/report.md" | head -5', id="pipe-into-whitelisted"),
        pytest.param(f'V=$(cat {SENTINEL}); [ -z "$V" ] && echo missing', id="test-bracket-guard"),
        'TS=$(date -u +%Y-%m-%dT%H-%M-%SZ); echo "$TS"',
        'IFS= read -r TS < "${TMPDIR:-/tmp}/dev-fix-team-ts-${CSID}" 2>/dev/null || TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)',
        # Pure read-form anchor — ZERO substitutions. Prefix allow-rules can never
        # match it (first token = `IFS=` assignment), so the hook must carry it.
        'IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""',
        'IFS= read -r V < "${TMPDIR:-/tmp}/foundry-shared-dir-${CSID}" 2>/dev/null || V=""\n'
        'cat "$V/agent-spawn-protocol.md"',
        "IFS= read -r V < ${TMPDIR:-/tmp}/dev-review-run-dir-123 2>/dev/null || V=x",
    ],
)
def test_blueprint_sentinel_reads_are_allowed(command: str) -> None:
    """Sentinel-read idiom plus read-only segments gets permissionDecision allow."""
    result = _run(command)
    assert _is_allowed(result), f"{command!r} should be allowed, got: {result}"


@_skip_node_unavailable
def test_allow_emits_no_updated_input() -> None:
    """Allow decision must not rewrite the command — deny rules match the original."""
    result = _run(f"V=$(cat {SENTINEL})")
    assert "updatedInput" not in result["hookSpecificOutput"]


# ── Anything not provably the blueprint idiom must passthrough ────────────────


@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"rm $(cat {SENTINEL})", id="rm-not-whitelisted"),
        pytest.param(f"V=$(cat {SENTINEL}); git push origin main", id="git-push-chain"),
        'V=$(cat "/etc/passwd")',
        'V=$(cat "$HOME/.ssh/id_rsa")',
        pytest.param(f"V=$(cat {SENTINEL}); W=$(date)", id="second-non-sentinel-subst"),
        "V=`cat ${TMPDIR:-/tmp}/x`",
        pytest.param(f"V=$(cat {SENTINEL}); diff <(echo a) <(echo b)", id="process-subst"),
        pytest.param(f"V=$(cat {SENTINEL}); cat <<EOF\nhi\nEOF", id="heredoc"),
        pytest.param(f'V=$(cat {SENTINEL}); echo hi > "$V/out.txt"', id="write-redirect"),
        'V=$(cat "${TMPDIR:-/tmp}/${X:-$(rm -rf /)}")',
        'echo \\" ; rm -rf / ; echo \\"',
        "ls -la",
        # Read-form anchor must NOT loosen anything else:
        'IFS= read -r V < "/etc/passwd"',
        'IFS= read -r V < "${TMPDIR:-/tmp}/s-1" || V=""; rm -rf "$V"',
        'read -r V < "${TMPDIR:-/tmp}/../../etc/passwd"',
        pytest.param(f"export PATH=/tmp/evil:$PATH; IFS= read -r V < {SENTINEL}", id="read-form-path-hijack"),
        "TS=$(date +%s; rm -rf /)",
        "TS=$(date -u +%Y -d yesterday)",
        pytest.param(f"V=$(cat {SENTINEL}); python -c 'x'", id="python-not-whitelisted"),
        pytest.param(f"V=$(cat {SENTINEL}); curl https://x.example", id="curl-not-whitelisted"),
    ],
)
def test_non_blueprint_commands_passthrough(command: str) -> None:
    """Everything not provably the blueprint idiom falls through to real checks."""
    result = _run(command)
    assert result == {}, f"{command!r} was allowed — gate bypass risk: {result}"


# ── Codex adversarial-review PoCs (2026-07-22) — all MUST passthrough ─────────


@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        # Class 1 — unquoted path/default swallowing shell syntax (injection inside $()).
        "V=$(cat ${TMPDIR:-/tmp}/sentinel;rm${IFS}/tmp/pwned)",
        "V=$(cat ${TMPDIR:-/tmp}/sentinel;curl${IFS}https://x.invalid)",
        "V=$(cat ${TMPDIR:-/tmp}/sentinel>/tmp/pwned)",
        "V=$(cat ${TMPDIR:-/tmp}/sentinel||echo ;>/tmp/pwned)",
        "V=$(cat ${TMPDIR:-/tmp}/sentinel)>(touch${IFS}/tmp/pwned)",
        # Class 2 — find spawns / deletes.
        pytest.param(f"V=$(cat {SENTINEL}); find /tmp -exec sh -c 'touch /tmp/pwned' {{}} \\;", id="poc-find-exec"),
        pytest.param(f"V=$(cat {SENTINEL}); find /tmp -exec curl https://x.invalid \\;", id="poc-find-exec-curl"),
        pytest.param(f"V=$(cat {SENTINEL}); find /tmp -delete", id="poc-find-delete"),
        # Class 3 — writer tokens.
        pytest.param(f"V=$(cat {SENTINEL}); touch /tmp/pwned", id="poc-touch"),
        "TS=$(date +%s); mkdir -p /tmp/pwned-dir",
        "TS=$(date +%s); sort -o /tmp/pwned /etc/hosts",
        "TS=$(date +%s); date --set=@0",
        # Class 4 — path traversal (input-redirect `<` is intentionally allowed:
        # no escalation over what a whitelisted read-only token already reads).
        'V=$(cat ${TMPDIR:-/tmp}/../../etc/passwd); printf %s "$V"',
        # Re-review pass 2 — loader/lookup-path hijack via sensitive assignment.
        pytest.param(f"export PATH=/tmp/attacker:$PATH; V=$(cat {SENTINEL})", id="poc-path-hijack"),
        pytest.param(f"PATH=/tmp/x:$PATH V=$(cat {SENTINEL})", id="poc-path-inline"),
        pytest.param(f"export LD_PRELOAD=/tmp/evil.so; V=$(cat {SENTINEL}); cat x", id="poc-ld-preload"),
        pytest.param(f"IFS=x; V=$(cat {SENTINEL})", id="poc-nonempty-ifs"),
        # Re-review pass 2 — unquoted bare $VAR word-split read (PV dropped from UPATH).
        'export X=" /etc/passwd"; V=$(cat ${TMPDIR:-/tmp}/$X); printf %s "$V"',
    ],
)
def test_codex_poc_bypasses_are_closed(command: str) -> None:
    """Every confirmed Codex bypass PoC must fall through to the real prompt."""
    result = _run(command)
    assert result == {}, f"{command!r} STILL ALLOWED — bypass reopened: {result}"


# ── Basic hook hygiene ────────────────────────────────────────────────────────


@_skip_node_unavailable
def test_non_bash_tool_passthrough() -> None:
    """Non-Bash tool payloads are ignored."""
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
    proc = subprocess.run(["node", str(HOOK)], input=payload, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


@_skip_node_unavailable
def test_malformed_json_exits_zero() -> None:
    """Malformed stdin never crashes or blocks."""
    proc = subprocess.run(["node", str(HOOK)], input="not json", capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ── Whole-line comments do not reject an otherwise-clean block ────────────────

READ_FORM = f"IFS= read -r RUN_DIR < {SENTINEL}"


@_skip_node_unavailable
class TestCommentSegments:
    """Segment validation skips whole-line comments.

    Every plugin bash block carries `# timeout: N` annotations on their own lines. Before this was handled, one such
    line put `#` in first-token position and rejected the whole block — measured on 107 of 790 real blueprint blocks, of
    which 28 were otherwise fully allowable.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f'{READ_FORM}\n# timeout: 5000\ncat "$RUN_DIR/r.md"', id="comment-between"),
            pytest.param(f'# resolve run dir first\n{READ_FORM}\ncat "$RUN_DIR/r.md"', id="comment-leading"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/r.md"\n# done', id="comment-trailing-line"),
            pytest.param(f'{READ_FORM}\n#no space after hash\ncat "$RUN_DIR/r.md"', id="comment-no-space"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/r.md"  # timeout: 5000', id="comment-inline"),
        ],
    )
    def test_allows_block_with_comment_lines(self, command: str) -> None:
        """A comment line is inert and must not reject an otherwise read-only block.

        Exercises the shapes real skill files use: a `# timeout:` annotation
        between two read-only commands, a leading explanatory line, a trailing
        one, and the no-space `#foo` form.
        """
        result = _run(command)
        assert _is_allowed(result), f"{command!r} should be allowed, got: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f"{READ_FORM}\n# note && rm -rf /tmp/x", id="separator-after-comment"),
            pytest.param(f"{READ_FORM}\n# note; rm -rf /tmp/x", id="semicolon-after-comment"),
            pytest.param(f"{READ_FORM}\n# note | curl https://x.invalid", id="pipe-after-comment"),
        ],
    )
    def test_comment_skip_cannot_hide_a_live_command(self, command: str) -> None:
        """Text bash would treat as commented-out is still validated as if live.

        Segments split on `;|&` as well as newline, so the skip can only ever cost an allow (false negative), never
        grant one. A shell would ignore `rm` here; the hook must not, because it does not parse comment scope.
        """
        result = _run(command)
        assert result == {}, f"{command!r} must passthrough, got: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f"# {READ_FORM}\ncat /etc/passwd", id="poc-commented-read-form-anchor"),
            "# TS=$(date -u +%Y)\ncat /etc/passwd",
            pytest.param(f"cat /etc/passwd ; # {READ_FORM}", id="poc-commented-anchor-after-separator"),
        ],
    )
    def test_a_comment_cannot_supply_the_blueprint_anchor(self, command: str) -> None:
        """Commented-out text must not satisfy the anchor requirement.

        Skipping comment segments during validation opened this: a `#`-prefixed
        sentinel read still matched READ_FORM against the raw command, so a
        comment could vouch for a live `cat /etc/passwd` that has no blueprint
        idiom in it at all. The anchor is now tested against comment-stripped
        text while every other check still sees the full command.
        """
        result = _run(command)
        assert result == {}, f"{command!r} STILL ALLOWED — comment supplied the anchor: {result}"

    def test_quoted_hash_is_not_treated_as_a_comment(self) -> None:
        """A `#` inside quotes is masked before the comment check and stays data.

        Guards the inverse mistake: treating `grep "#hdr"` as a comment line
        would skip validating a segment that really does run a command.
        """
        result = _run(f'{READ_FORM}\ngrep -n "#hdr" "$RUN_DIR/r.md"')
        assert _is_allowed(result)


# ── Adversarial-review regressions, 2026-08-18 ───────────────────────────────


@_skip_node_unavailable
class TestReviewedBypasses:
    """PoCs from the 2026-08-18 adversarial review (Codex + challenger).

    Each was observed ALLOWED and, for the execution-class ones, confirmed to actually run under `bash -c`. They are the
    reason the comment skip and the traversal rewrite are safe to ship.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            "rm -rf /tmp/pwned",
            "sh -c 'id; touch /tmp/m'",
            "git push --force",
            "python3 -c 'import os'",
            "X=1 touch /tmp/m",
        ],
    )
    def test_escaped_newline_cannot_extend_a_comment_over_a_payload(self, payload: str) -> None:
        """A `\\` ending a comment line must not swallow the next line.

        `maskQuotes` treated backslash-newline as a line continuation and ate the newline, merging the payload into the
        comment segment — which the comment skip then skipped entirely. bash does NOT continue comments that way: it
        ends the comment at the physical newline and runs the next line. Hook allowed, shell executed: arbitrary command
        execution behind a `#`.
        """
        result = _run(f"{READ_FORM}\n# note \\\n{payload}")
        assert result == {}, f"payload {payload!r} STILL ALLOWED behind a comment: {result}"

    def test_chained_comment_continuations_cannot_hide_a_payload(self) -> None:
        """Several stacked `# … \\` lines must not hide the eventual payload either."""
        result = _run(f"{READ_FORM}\n# a \\\n# b \\\ntouch /tmp/m")
        assert result == {}, f"chained continuation STILL ALLOWED: {result}"

    @pytest.mark.parametrize(
        "command",
        ["echo $'\\''; git push --force \\'", "cat $'\\056\\056'/etc/passwd", "cat $'\\x2e\\x2e'/etc/passwd"],
    )
    def test_ansi_c_quoting_is_refused_outright(self, command: str) -> None:
        """Reject shell quoting that desynchronizes the quote parser.

        bash unescapes `\\'` inside `$'…'`, ending the string at a different quote than the masker believes. The toggle
        count drifts, a `;` gets masked away, and the hook sees one `echo` segment where bash runs two commands. No
        blueprint idiom uses ANSI-C quoting, so it fails closed.
        """
        result = _run(f"{READ_FORM}\n{command}")
        assert result == {}, f"{command!r} STILL ALLOWED — ANSI-C desync: {result}"

    @pytest.mark.parametrize("command", ["echo $\\\n'\\''; git push --force \\'", "cat $\\\n'\\056\\056'/etc/passwd"])
    def test_ansi_c_split_across_a_line_continuation_rejects(self, command: str) -> None:
        """ANSI-C quoting assembled across a `\\`+newline must not reach the desync.

        bash joins a line continuation before tokenizing, so `$\\`+newline+`'x'` really does parse as `$'x'` (verified:
        it printed `A` for `\\x41`) — and the raw command never contains the literal `$'` the guard tests for. It
        rejects anyway, but only as a side effect of the escaped-newline fix forcing a segment split at the join point.
        Pinned here because nothing else would catch it if that fix were ever relaxed.
        """
        result = _run(f"{READ_FORM}\n{command}")
        assert result == {}, f"{command!r} STILL ALLOWED — ANSI-C via continuation: {result}"

    @pytest.mark.parametrize("command", ["cat \\.\\./etc/passwd", "cat .\\./etc/passwd"])
    def test_backslash_escaped_dots_are_still_traversal(self, command: str) -> None:
        """Reject escaped parent traversal that the shell resolves after parsing.

        An unquoted `\\.` is a no-op escape: bash drops the backslash and the path becomes `..`. Confirmed against a
        real shell reading a file one directory up. The guard therefore also tests a backslash-collapsed copy.
        """
        result = _run(f"{READ_FORM}\n{command}")
        assert result == {}, f"{command!r} STILL ALLOWED — escaped traversal: {result}"

    @pytest.mark.parametrize("command", ["uniq /etc/hosts /tmp/pwned", "uniq /dev/null /tmp/victim.txt"])
    def test_uniq_is_not_a_read_only_token(self, command: str) -> None:
        """Write OUT — it cannot be a whitelisted segment head.

        Verified against a real shell: `uniq /dev/null victim` truncated a
        two-line file to zero bytes. Same class the list already excludes
        `sort -o` for, but the output file is positional rather than a flag, so
        it survived the original sweep. Segment validation never reads operands.
        """
        result = _run(f"{READ_FORM}\n{command}")
        assert result == {}, f"{command!r} STILL ALLOWED — writes its second operand: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            'export "PATH"=/attacker/bin:$PATH',
            "export \\PATH=/attacker/bin:$PATH",
            "export PATH+=:/attacker/bin",
            'export "GLOBIGNORE"=x',
            'export PA"TH"=/evil',
        ],
    )
    def test_quote_and_append_forms_of_sensitive_assignment_reject(self, command: str) -> None:
        """Quoting or escaping a sensitive variable name must not evade the guard.

        bash strips quotes and backslashes from an assignment word before the builtin sees it, so `export "PATH"=…`
        really does set PATH — and a planted binary on the hijacked PATH then runs as a "read-only" whitelisted token.
        Confirmed end to end: a planted `cat` printed attacker output. The guard now also tests a quote-stripped copy.
        """
        result = _run(f"{READ_FORM}\n{command}\ncat /etc/hosts")
        assert result == {}, f"{command!r} STILL ALLOWED — PATH hijack: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            "cat () ( touch /tmp/pwned ); cat",
            'cat () ( sh -c "echo X > ./f" ); cat',
            "ls () ( touch /tmp/pwned ); ls",
            "( touch /tmp/pwned )",
        ],
    )
    def test_bare_parens_are_rejected(self, command: str) -> None:
        """A function definition with a subshell body must not be allowed.

        `cat () ( touch x ); cat` contains no top-level separator inside the body, so segmentation sees one segment
        whose first token is the safe name `cat` and never vets the body; the following `cat` then runs it. Confirmed
        creating a marker file under a real shell. The `$(`/`<(`/`>(` guards all require a sigil, so bare parens slipped
        past every one.
        """
        result = _run(f"{READ_FORM}\n{command}")
        assert result == {}, f"{command!r} STILL ALLOWED — arbitrary execution: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            'printf -v PATH /attacker/bin:%s "$PATH"',
            "printf -v BASH_ENV /tmp/e",
            "IFS= read -r PATH < /tmp/attacker",
            "IFS= read -r LD_PRELOAD < /tmp/x",
            "X=T\nexport PA${X}H=/attacker/bin:$PATH",
            'export "IFS"=,',
            "export \\IFS=,",
            "export LD_AUDIT=/tmp/e.so",
            "export DYLD_FRAMEWORK_PATH=/tmp/e",
            "export PROMPT_COMMAND=/tmp/e",
            "echo ${DYLD_INSERT_LIBRARIES:=/tmp/evil.dylib}",
            "echo ${LD_PRELOAD:=/tmp/evil.so}",
            "echo ${IFS:=X}",
            "echo ${IFS=X}",
        ],
    )
    def test_non_assignment_routes_to_setting_a_variable_reject(self, command: str) -> None:
        """Gating `NAME=` syntax is not enough — every route that SETS the variable rejects.

        bash offers three ways to set a variable that never produce a literal
        `NAME=` for a pattern to see: `printf -v NAME`, `read NAME`, and `export`
        with an expansion in the name half (expansion happens after the text is
        inspected). Each was confirmed to hijack PATH end to end, with a planted
        binary running as a whitelisted "read-only" token.
        """
        result = _run(f"{READ_FORM}\n{command}\ncat /etc/hosts")
        assert result == {}, f"{command!r} STILL ALLOWED — sets a sensitive variable: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            "printf '%s\\n' \"$RUN_DIR\"",
            'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"',
            'IFS= read -r OTHER < "${TMPDIR:-/tmp}/y-${CSID}"',
        ],
    )
    def test_variable_write_guards_spare_the_blueprint_idioms(self, command: str) -> None:
        """The write guards must not catch the idioms the hook exists to bless.

        `export CSID="${…}"` carries an expansion in the VALUE, not the name; `read` into an ordinary variable is the
        core sentinel form; `printf` without `-v` writes to stdout only.
        """
        result = _run(f"{READ_FORM}\n{command}")
        assert _is_allowed(result), f"{command!r} should be allowed, got: {result}"

    def test_genuine_line_continuation_still_allows(self) -> None:
        """A `\\` continuation outside a comment must keep working.

        The fix only changes what happens to the newline; a real continuation joining two read-only segments is still
        the blueprint idiom it was.
        """
        result = _run(f'{READ_FORM}\ngrep -n "H1" "$RUN_DIR/r.md" \\\n| head -5')
        assert _is_allowed(result), f"legit continuation should allow, got: {result}"


# ── Stripped redirect forms must end at a token boundary ─────────────────────


@_skip_node_unavailable
class TestRedirectStripping:
    """Only genuine stderr-silencing / stdout-to-null forms are stripped.

    The stripper removes those forms and then rejects any `>` still standing. A prefix match let `>/dev/nullpwned` lose
    its `>/dev/null` to the stripper, leaving no `>` to reject — so a real file write was allowed. The forms are now
    anchored to a token boundary.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f"{READ_FORM}\necho pwned >/dev/nullpwned", id="poc-suffix-past-dev-null"),
            pytest.param(f"{READ_FORM}\necho hi 1>/dev/nullx", id="poc-fd-prefixed-suffix"),
            pytest.param(f"{READ_FORM}\necho hi >/dev/null2", id="poc-digit-suffix"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/r.md" 2>&1x', id="poc-fd-dup-suffix"),
        ],
    )
    def test_write_through_a_dev_null_prefix_is_rejected(self, command: str) -> None:
        """A write target that merely starts with `/dev/null` must not be stripped.

        `echo pwned >/dev/nullpwned` writes a real file; the hook's own contract forbids allowing any write, so this has
        to passthrough to a prompt.
        """
        result = _run(command)
        assert result == {}, f"{command!r} STILL ALLOWED — write redirect: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/r.md" 2>/dev/null', id="stderr-silence"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/r.md" >/dev/null', id="stdout-to-null"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/r.md" 2>&1 | head -5', id="fd-dup-then-pipe"),
        ],
    )
    def test_genuine_silencing_forms_still_allow(self, command: str) -> None:
        """Anchoring the stripped forms must not break the idioms skills actually use.

        Every blueprint sentinel read carries `2>/dev/null`; if the anchoring were too strict these would start
        prompting again.
        """
        result = _run(command)
        assert _is_allowed(result), f"{command!r} should be allowed, got: {result}"


# ── `..` is matched as a path component, not as any two dots ──────────────────


@_skip_node_unavailable
class TestTraversalMatching:
    """Traversal rejection keys on a real path component.

    The check previously tested `cmd.includes("..")`, which also fired on `...` ellipsis and version ranges like
    `v1.2..v1.3` — neither is traversal.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/a...b.md"', id="ellipsis-in-filename"),
            pytest.param(f'{READ_FORM}\necho "range v1.2..v1.3"', id="version-range"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/file..txt"', id="double-dot-in-filename"),
        ],
    )
    def test_allows_non_traversal_double_dots(self, command: str) -> None:
        """Two dots that are not a path component must not read as traversal."""
        result = _run(command)
        assert _is_allowed(result), f"{command!r} should be allowed, got: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            'IFS= read -r V < "${TMPDIR:-/tmp}/../../etc/passwd"',
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/../secrets.md"', id="traversal-mid-path"),
            pytest.param(f'{READ_FORM}\ncat "../etc/passwd"', id="traversal-leading"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/.."', id="traversal-trailing"),
            pytest.param(f"{READ_FORM}\ncat ..", id="traversal-bare-arg"),
            pytest.param(f"{READ_FORM}\ncat ${{V:-..}}", id="traversal-via-default"),
        ],
    )
    def test_rejects_real_traversal(self, command: str) -> None:
        """Every genuine `..` path component still rejects.

        Covers the forms an escape would actually take: inside the sentinel
        path, mid-path, leading, trailing, as a bare operand, and smuggled
        through a parameter-expansion default.
        """
        result = _run(command)
        assert result == {}, f"{command!r} must passthrough, got: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f"{READ_FORM}\ncat {{a,../etc/passwd}}", id="poc-brace-expansion-comma"),
            pytest.param(f"{READ_FORM}\ncat {{a,..}}", id="poc-brace-comma-trailing"),
            pytest.param(f"{READ_FORM}\ncat a,../etc/passwd", id="poc-bare-comma"),
            pytest.param(f"{READ_FORM}\ncat (..)", id="poc-paren-neighbour"),
            pytest.param(f"{READ_FORM}\ncat [..]/x", id="poc-bracket-neighbour"),
        ],
    )
    def test_traversal_poc_bypasses_are_closed(self, command: str) -> None:
        """Separators absent from an allow-list neighbour class must not admit traversal.

        A first attempt at this check enumerated the characters a traversal may open at, which let every character
        omitted from that class through — `cat {a,../etc/passwd}` brace-expands to read `../etc/passwd` and was allowed.
        The check is now default-reject, exempting only a `..` flanked by word characters, so an unlisted separator
        fails closed instead.
        """
        result = _run(command)
        assert result == {}, f"{command!r} STILL ALLOWED — traversal bypass: {result}"


# ── Verdicts are invariant under runtime value substitution ───────────────────


@_skip_node_unavailable
class TestRuntimeInvariance:
    """The same shape gets the same verdict whatever the runtime values are.

    This is the hook's entire justification for existing alongside `blueprint-allow.js`: provenance matching covers far
    more committed text but collapses to zero the moment any value differs between runs, while this hook keys on shape
    and does not.
    """

    @pytest.mark.parametrize(
        "command",
        [
            'IFS= read -r WORK_DIR < "${TMPDIR:-/tmp}/develop-fix-run-dir-${CSID}"\n'
            'grep -n "CRITICAL" "$WORK_DIR/out.md" | head -200',
            'IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-$CSID"\ncat "$RUN_DIR/r.md"',
            'IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID:-shared}"\ncat "$RUN_DIR/r.md"',
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/my report.md"', id="target-with-space"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/foundry--challenger.md"', id="target-with-double-hyphen"),
            'IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH"\necho "$BRANCH"',
        ],
    )
    def test_allows_every_runtime_variant_of_one_shape(self, command: str) -> None:
        """Varying only runtime values must not change an allow verdict.

        Each case is the same read-then-inspect shape with a different sentinel basename, variable name, CSID form, or
        target filename — exactly what differs between two runs of the same skill.
        """
        result = _run(command)
        assert _is_allowed(result), f"{command!r} should be allowed, got: {result}"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/$(whoami).md"', id="substitution-in-varied-part"),
            pytest.param(f'{READ_FORM}\ncat "$RUN_DIR/r.md"; rm -rf /tmp/x', id="separator-in-varied-part"),
            pytest.param(f'{READ_FORM}\necho pwned > "$RUN_DIR/out.txt"', id="redirect-in-varied-part"),
        ],
    )
    def test_rejects_injection_through_the_varied_part(self, command: str) -> None:
        """Invariance must not extend to values carrying shell metacharacters.

        The same slot that legitimately varies is where an injection would be planted, so a varied value containing a
        substitution, separator, or redirect has to flip the verdict back to passthrough.
        """
        result = _run(command)
        assert result == {}, f"{command!r} must passthrough, got: {result}"


# ── Library entry points added for the dispatcher ─────────────────────────────


def _node_eval_sentinel(expression: str) -> object:
    """Require the hook as a module and return the JSON-encoded value of ``expression``."""
    script = (
        f"const h = require({json.dumps(str(HOOK))});process.stdout.write(JSON.stringify({expression}) ?? 'undefined');"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", timeout=15, check=False)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return None if proc.stdout == "undefined" else json.loads(proc.stdout)


def _stdin(command: str, tool_name: str = "Bash") -> str:
    """Return a PreToolUse stdin payload as a JSON string literal for embedding in a node expression."""
    return json.dumps(json.dumps({"tool_name": tool_name, "tool_input": {"command": command}}))


@pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hook")
class TestImportIsSideEffectFree:
    """Requiring the module must do nothing at all.

    The dispatcher requires this file as a library, while ``bin/audit_hook_coverage.py`` still runs it as a standalone
    hook. Before the ``require.main`` guard the driver was top-level: importing it attached stdin listeners and could
    call ``process.exit``, which is precisely what makes a module unusable from another process's entry point.
    """

    def test_require_attaches_no_stdin_listeners_and_does_not_exit(self) -> None:
        """Count listeners before and after the require; both must be zero, and the process must survive it."""
        script = (
            "const before = process.stdin.listenerCount('data') + process.stdin.listenerCount('end');"
            f"require({json.dumps(str(HOOK))});"
            "const after = process.stdin.listenerCount('data') + process.stdin.listenerCount('end');"
            "process.stdout.write(JSON.stringify([before, after]));"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", timeout=15, check=False)
        assert proc.returncode == 0, f"requiring the module exited or crashed: {proc.stderr}"
        assert json.loads(proc.stdout) == [0, 0]

    def test_module_exports_the_library_surface(self) -> None:
        """``decide`` and ``evaluate`` are the dispatcher's entry points; ``isAllowable`` stays exported for tests."""
        assert sorted(_node_eval_sentinel(f"Object.keys(require({json.dumps(str(HOOK))}))")) == [
            "decide",
            "evaluate",
            "isAllowable",
        ]


@pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hook")
class TestEvaluateClassification:
    """``evaluate`` labels what the module did, without changing what it decides."""

    def test_allow_reports_the_shape_lane_at_rank_two(self) -> None:
        """A shape allow is rank 2: it outranks nothing, and blueprint provenance beats it wherever both fire."""
        result = _node_eval_sentinel(f"h.evaluate({_stdin(f'V=$(cat {SENTINEL})')})")
        assert (result["decision"], result["lane"], result["rank"]) == ("allow", "shape", 2)
        assert result["payload"]["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_examined_and_declined_is_a_passthrough(self) -> None:
        """A command the matcher looked at and refused is an abstention with a reason."""
        result = _node_eval_sentinel(f"h.evaluate({_stdin('ls -la')})")
        assert (result["decision"], result["why"], result["payload"]) == ("passthrough", "shape-mismatch", None)

    @pytest.mark.parametrize(
        ("expression", "case"),
        [
            pytest.param('h.evaluate("not json")', "unparsable stdin", id="parse-failure"),
            pytest.param(f"h.evaluate({_stdin('ls', 'Read')})", "not a Bash call", id="non-bash-tool"),
            pytest.param(f"h.evaluate({_stdin('   ')})", "empty after trimming", id="whitespace-only"),
        ],
    )
    def test_returning_before_deciding_is_none_not_passthrough(self, expression: str, case: str) -> None:
        """``none`` is the absence of an opinion, and must never be read as an abstention.

        The distinction is load-bearing downstream: an abstention is evidence the command was examined and refused, and
        counting a module that never looked at it as one would overstate what the log establishes.
        """
        result = _node_eval_sentinel(expression)
        assert (result["decision"], result["why"]) == ("none", "not-applicable"), case

    def test_evaluate_returns_the_same_payload_object_decide_produces(self) -> None:
        """One payload builder, so the two entry points can never disagree on a byte of stdout."""
        command = _stdin(f"V=$(cat {SENTINEL})")
        assert _node_eval_sentinel(f"h.evaluate({command}).payload") == _node_eval_sentinel(f"h.decide({command})")

    def test_evaluate_never_exits_the_process(self) -> None:
        """A library that calls ``process.exit`` takes its caller's other lane down with it."""
        script = (
            f"const h = require({json.dumps(str(HOOK))});"
            f"h.evaluate({_stdin('ls', 'Read')}); h.evaluate('not json'); h.evaluate({_stdin('ls -la')});"
            "process.stdout.write('survived');"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", timeout=15, check=False)
        assert proc.stdout == "survived"
