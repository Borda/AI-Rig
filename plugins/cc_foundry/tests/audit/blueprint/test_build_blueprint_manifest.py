"""Tests for build_blueprint_manifest.py — normalization spec, filters, and CLI.

The normalization pipeline is the contract a companion runtime hook must reproduce byte-identically, so it is tested by
explicit vectors rather than only by doctest.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import build_blueprint_manifest as bbm
import pytest
from _blueprint_vectors import vector_params

PLUGIN_JSON = '{"name": "foundry", "version": "1.2.3"}\n'


def _make_plugin(scan_dir: Path, name: str, skill_md: str = "", claude_md: str = "") -> Path:
    """Create a minimal plugin tree under ``scan_dir`` and return its directory.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     plugin = _make_plugin(Path(directory), "demo")
        ...     (plugin.name, (plugin / ".claude-plugin" / "plugin.json").read_text())
        ('demo', '{"name": "foundry", "version": "1.2.3"}\\n')
    """
    plugin_dir = scan_dir / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(PLUGIN_JSON, encoding="utf-8")
    if skill_md:
        skill_dir = plugin_dir / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if claude_md:
        (plugin_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    return plugin_dir


@pytest.fixture(name="scan_dir")
def _scan_dir(tmp_path: Path) -> Path:
    """Scan directory holding all four target plugins; only cc_foundry has content."""
    root = tmp_path / "plugins"
    root.mkdir()
    _make_plugin(
        root,
        "cc_foundry",
        skill_md="# Demo\n\n```bash\necho alpha\necho beta\n```\n",
        claude_md="```bash\nls -la\n```\n",
    )
    for name in ("cc_oss", "cc_develop", "cc_research"):
        _make_plugin(root, name)
    return root


class TestNormalize:
    """Normalize: the four-step pipeline that the runtime hook must mirror exactly."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("echo a\r\necho b\r\n", "echo a\necho b", id="crlf-to-lf"),
            pytest.param("echo a\recho b", "echo a\necho b", id="lone-cr-to-lf"),
            pytest.param("echo a   \t\necho b  ", "echo a\necho b", id="trailing-whitespace"),
            pytest.param("\n\n\necho a\n\n\n", "echo a", id="leading-trailing-blanks"),
            pytest.param("echo a\n\n\n\n\necho b", "echo a\n\necho b", id="collapse-blank-run"),
            pytest.param("echo a  # note", "echo a", id="trailing-comment"),
            pytest.param("#!/usr/bin/env bash\necho a", "echo a", id="hash-at-line-start-is-comment"),
            pytest.param("echo   a    b", "echo   a    b", id="no-intra-line-collapse"),
            pytest.param('echo "a  b"', 'echo "a  b"', id="no-quote-rewriting"),
            pytest.param("curl http://x#y", "curl http://x#y", id="url-fragment-survives"),
            pytest.param("curl http://x#y  # fetch", "curl http://x#y", id="url-fragment-plus-comment"),
            pytest.param('echo "${V:-#x}"', 'echo "${V:-#x}"', id="parameter-default-hash-survives"),
            pytest.param("echo ${V:-#x}", "echo ${V:-#x}", id="unquoted-parameter-default-survives"),
            pytest.param("echo '# literal'", "echo '# literal'", id="single-quoted-hash-survives"),
            pytest.param('echo "# literal"', 'echo "# literal"', id="double-quoted-hash-survives"),
            pytest.param("echo a\t# tabbed", "echo a", id="tab-before-hash-is-comment"),
            # A ``#`` inside a multi-line quoted string is not treated as a comment.
            pytest.param(
                'echo "line one\n# still inside\nline three"',
                'echo "line one\n# still inside\nline three"',
                id="quote-state-spans-lines",
            ),
            # A lone comment line between commands collapses to exactly one blank line.
            pytest.param("echo a\n# note\necho b", "echo a\n\necho b", id="comment-only-line-leaves-single-blank"),
        ],
    )
    def test_documented_step(self, raw: str, expected: str) -> None:
        """Each documented normalization step, and the quote-aware comment rule, produces the documented result.

        Covers line-ending and whitespace cleanup, blank-run collapsing, and trailing-comment stripping where a ``#`` is
        a comment only when unquoted and at a word start (URL fragments, parameter defaults and quoted hashes survive,
        including a quote spanning several lines), and a lone comment line leaving exactly one blank line.
        """
        assert bbm.normalize(raw) == expected

    def test_spacing_variants_do_not_collide(self) -> None:
        """Step (d): commands differing only in spacing keep distinct digests.

        Collapsing intra-line spacing would let ``echo  a`` and ``echo a`` share one manifest entry, so approving one
        spelling would approve the other.
        """
        assert bbm.sha256_text(bbm.normalize("echo  a")) != bbm.sha256_text(bbm.normalize("echo a"))


class TestSharedVectors:
    """The shared fixture is the cross-language contract; Python must satisfy all of it.

    ``test_blueprint_allow_js.py`` runs the identical vectors against the JS hook, so a change landing on one side alone
    fails here or there rather than silently opening the normalization-asymmetry bug class the manifest design depends
    on avoiding.
    """

    @pytest.mark.parametrize("vector", vector_params("normalize"))
    def test_normalize(self, vector: dict) -> None:
        """Produce the fixture's expected text for every shared ``normalize`` vector.

        Each vector is a raw block and the exact text the hook must hash; a mismatch here means the two languages would
        compute different digests for the same command.
        """
        assert bbm.normalize(vector["input"]) == vector["expected"]

    @pytest.mark.parametrize("vector", vector_params("needs_bailout"))
    def test_needs_bailout(self, vector: dict) -> None:
        """Agree with the fixture on heredocs and unterminated quotes.

        A block that needs bail-out gets no per-command entries; if the two sides disagreed, one would manifest commands
        the other refuses to extract.
        """
        assert bbm.needs_bailout(vector["input"]) is vector["expected"]

    @pytest.mark.parametrize("vector", vector_params("split_logical_commands"))
    def test_split_logical_commands(self, vector: dict) -> None:
        """Split exactly as the fixture specifies.

        The hook looks each line up as its own entry, so both sides must cut a block into the same logical commands,
        continuations included.
        """
        assert bbm.split_logical_commands(vector["input"]) == vector["expected"]

    @pytest.mark.parametrize("vector", vector_params("aliases_escaped_blank"))
    def test_aliases_escaped_blank(self, vector: dict) -> None:
        """Refuse raw whitespace whose normalization could change shell behavior.

        A backslash-blank at a line end or before ``#`` makes the shell run a different command than the normalized text
        the digest covers; carriage returns and discarded non-shell whitespace also refuse on both sides.
        """
        assert bbm.aliases_escaped_blank(vector["input"]) is vector["expected"]

    @pytest.mark.parametrize("vector", vector_params("is_dangerous"))
    def test_is_dangerous(self, vector: dict) -> None:
        """Classify every shared vector as the fixture specifies.

        Classification is the most security-critical function of the pair: a command the
        generator judges safe and the hook judges dangerous merely wastes an allow, but
        the reverse silently ships a destructive command into the manifest that the
        hook's own defence-in-depth layer then declines to catch.
        """
        assert bbm.is_dangerous(vector["input"]) is vector["expected"]


class TestSplitLogicalCommands:
    """split_logical_commands: newline-only splitting with verbatim continuations."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("a; b && c", ["a; b && c"], id="never-splits-on-operators"),
            pytest.param(
                "cmd \\\n  --flag\nnext",
                ["cmd \\\n  --flag", "next"],
                id="continuation-kept-verbatim",
            ),
            pytest.param("echo a\\\\\necho b", ["echo a\\\\", "echo b"], id="escaped-backslash-is-not-a-continuation"),
        ],
    )
    def test_splits_only_on_newlines(self, text: str, expected: list[str]) -> None:
        """Commands split on newlines only, never on shell operators, with continuations kept verbatim.

        Backslash-newlines are preserved so the text still matches what is sent, and an even run of trailing backslashes
        ends the command instead of continuing it.
        """
        assert bbm.split_logical_commands(text) == expected


class TestBailout:
    """needs_bailout: heredocs and multi-line quotes disable per-command extraction."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("echo a\necho b", False, id="plain-block"),
            pytest.param("cat <<EOF\nbody\nEOF", True, id="heredoc"),
            pytest.param("cat <<-'EOF'\nbody\nEOF", True, id="dashed-quoted-heredoc"),
            pytest.param('grep <<< "word"', True, id="herestring-over-bails"),
            pytest.param("echo 'one\ntwo'", True, id="multi-line-quote"),
            pytest.param("echo 'one line'", False, id="closed-quote"),
        ],
    )
    def test_bailout_triggers(self, text: str, expected: bool) -> None:
        """Bail-out fires on heredoc markers and quotes left open across lines, and not on balanced input.

        A here-string is treated as a heredoc marker on purpose (over-bailing only costs an allow), while a closed
        single-line quote must not trigger it.
        """
        assert bbm.needs_bailout(text) is expected


class TestDangerFilter:
    """The danger filter's effect on manifest entries.

    Classification itself is asserted by :class:`TestSharedVectors` against the shared cross-language fixture, so the
    same cases bind the JS hook too; what remains here is how a dangerous verdict shapes the entries and the reported
    drop count.
    """

    @pytest.mark.parametrize(
        "unsafe_command",
        [
            pytest.param("rm -rf x", id="denylist-drop"),
            pytest.param("mv a b", id="allowlist-drop"),
        ],
    )
    def test_dropped_count_reported_on_stderr(
        self, scan_dir: Path, capsys: pytest.CaptureFixture[str], unsafe_command: str
    ) -> None:
        """The danger-filter drop count is always printed, never silent, for denylist and allowlist drops alike.

        A block pairing a safe command with an unsafe one loses its block entry and the unsafe line, so two entries are
        dropped. An allowlist drop (``mv``) is reported in the same stderr count as a denylist drop (``rm -rf``).
        """
        skill = scan_dir / "cc_foundry" / "skills" / "demo" / "SKILL.md"
        skill.write_text(f"```bash\necho safe\n{unsafe_command}\n```\n", encoding="utf-8")
        assert bbm.main(["--update", "--scan-dir", str(scan_dir)]) == 0
        assert "cc_foundry: 2 entries, 2 dropped by danger filter" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("text", "dropped"),
        [
            pytest.param('$PYTHON_CMD "$ROOT/bin/x.py" --apply', 1, id="variable-command-word"),
            pytest.param("sed -i s/a/b/ f", 1, id="sed-in-place"),
            pytest.param('FOO="a b" rm -rf x', 1, id="assignment-with-spaces-hides-rm"),
            pytest.param("n=$(( $(wc -c < f) / 3 ))", 0, id="arithmetic-is-not-a-command"),
            pytest.param("[[ -n $A && $B =~ x ]] || echo no", 0, id="conditional-test-stays-one-segment"),
        ],
    )
    def test_command_word_classes_decide_the_dropped_count(self, text: str, dropped: int) -> None:
        """A command word the filter cannot judge is dropped, while arithmetic and ``[[ ]]`` tests are not commands.

        The idioms ``$(( $(wc -c < f) / 3 ))`` and ``[[ a && $B =~ x ]]`` put a ``$`` word first in a split segment;
        they must not be mistaken for a variable command word, or common token-count and flag-validation blocks would
        lose their auto-allow, while ``FOO="a b" rm`` must not hide the ``rm`` behind a multi-word assignment.
        """
        entries, dropped_count = bbm.block_entries(text, "f.md:1")
        assert dropped_count == dropped
        assert bool(entries) is (dropped == 0)


class TestAllowlistGate:
    """The fail-closed allowlist: an entry is manifested only when every command it runs is listed.

    The denylist stays the cross-language contract; this gate is generator-only, so these cases need no JS twin.
    """

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param('V=$(python "${CLAUDE_PLUGIN_ROOT}/bin/x.py" --flag)', id="plugin-script-capture"),
            pytest.param('git status --short && git diff --stat "$BASE"', id="read-only-git"),
            pytest.param(
                'case "$MODE" in\nall|grouped) echo ok ;;\n*) exit 1 ;;\nesac', id="case-labels-and-alternatives"
            ),
            pytest.param('case "$T" in\n  tox*|"make test") echo x ;;\nesac', id="quoted-label-with-space"),
            pytest.param('_q() {\n  codemap-py query "$@"\n}\n_q central --top 5', id="function-definition-and-call"),
            pytest.param("cat <<'EOF' > f.md\nmv a b\nEOF", id="quoted-heredoc-body-is-data"),
            pytest.param("command -v jq >/dev/null 2>&1 && echo ok", id="fd-redirection-does-not-split"),
            pytest.param("find . -name '*.md' -print0 | xargs -0 grep -l x", id="xargs-wraps-listed-command"),
            pytest.param("timeout 5 curl -sS https://example.com", id="timeout-wraps-curl-get"),
            pytest.param("uv run pytest tests/ -q", id="uv-run-listed-command"),
            pytest.param("uv run --frozen --python=3.12 pytest -q", id="uv-run-known-options"),
            pytest.param('ARGS+=(--output "$F")', id="array-append"),
            pytest.param('mv "$_NOW" "$_NOW.done"', id="consume-marker-rename"),
            pytest.param(
                'IFS= read -r CACHE_FILE < "${TMPDIR:-/tmp}/cache-file-${CSID}"\n'
                'jq . f > "$CACHE_FILE.tmp" && mv "$CACHE_FILE.tmp" "$CACHE_FILE"',
                id="mv-atomic-publish-of-state-file",
            ),
            pytest.param(
                'IFS= read -r D < "${TMPDIR:-/tmp}/report-dir-${CSID}"\nmv -f "$D/report.md" "$D/report.refused.md"',
                id="mv-forced-within-state-dir",
            ),
            pytest.param("python -\"$F\" <<'PY'\nimport sys\nprint(sys.argv[1])\nPY", id="python-quoted-heredoc"),
            pytest.param(
                "python -c 'import json,sys; print(json.load(sys.stdin)[\"a\"])' < f", id="python-literal-code"
            ),
            pytest.param('cp -p "$f" "$RUN_DIR/pre/$f"', id="cp-snapshot"),
            pytest.param('echo x | tee -a "$LOG"', id="tee-append"),
            pytest.param(
                "[ -f \"${TMPDIR:-/tmp}/flags-${CSID}\" ] && while IFS='=' read -r _SK _SV; do "
                'case "$_SK" in MODE) MODE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/flags-${CSID}"',
                id="read-sentinel-keys",
            ),
            pytest.param(
                'IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/impl-${CSID}"\nbash -- "$IMPL_DIR/full-suite.sh"',
                id="shell-script-from-own-state",
            ),
            pytest.param('unlink "${TMPDIR:-/tmp}/lock-${CSID}"', id="unlink-sentinel"),
            pytest.param("kaggle competitions list -p 1", id="kaggle-list"),
            pytest.param('{\n  echo a\n  echo b\n} > "$OUT"', id="group-redirection"),
            pytest.param("jq -r '.a' f \\\n  | sort -u", id="continuation-line"),
        ],
    )
    def test_listed_commands_are_admitted(self, text: str) -> None:
        """Shapes the shipped blueprints rely on stay in the manifest.

        Each case is shell syntax a head-only allowlist would misread as an unlisted command — a ``case`` label, a
        function call, a heredoc line, the fd after ``2>&``, a continuation backslash — or a wrapper whose wrapped
        command is listed.
        """
        assert bbm.unlisted_command(text) is None
        assert bbm.is_excluded(text) is False

    @pytest.mark.parametrize(
        ("text", "offender"),
        [
            pytest.param("mv a b", "mv", id="mv"),
            pytest.param(
                'IFS= read -r D < "${TMPDIR:-/tmp}/report-dir-${CSID}"\nmv "$D/report.md" "$HOME/report.md"',
                "mv",
                id="mv-out-of-state-dir",
            ),
            pytest.param('mv -f "$D/a.md" "$D/b.md"', "mv", id="mv-under-untrusted-root"),
            pytest.param("cp -r build /", "cp /", id="cp-onto-root"),
            pytest.param('. "$HOME/.bashrc"', ". $HOME/.bashrc", id="source-non-state-file"),
            pytest.param(
                '[ -f "${TMPDIR:-/tmp}/flags-${CSID}" ] && . "${TMPDIR:-/tmp}/flags-${CSID}"',
                ". ${TMPDIR:-/tmp}/flags-${CSID}",
                id="source-sentinel",
            ),
            pytest.param(
                'IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/impl-${CSID}"\n. "$IMPL_DIR/pr-vars.sh"',
                ". $IMPL_DIR/pr-vars.sh",
                id="source-under-sentinel-root",
            ),
            pytest.param('tmpenv=$(mktemp)\n. "$tmpenv"', ". $tmpenv", id="source-mktemp-file"),
            pytest.param(
                'IFS= read -r METRIC_CMD < "${TMPDIR:-/tmp}/metric-cmd-${CSID}"\nbash -c "$METRIC_CMD"',
                "$METRIC_CMD",
                id="shell-string-from-sentinel",
            ),
            pytest.param(
                'IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/impl-${CSID}"\nbash "$IMPL_DIR/steps.sh"',
                "shell script $IMPL_DIR/steps.sh",
                id="shell-script-from-state-not-named",
            ),
            pytest.param('. "$IMPL_DIR/vars.sh"', ". $IMPL_DIR/vars.sh", id="source-under-untrusted-root"),
            pytest.param("perl -pi -e 's/a/b/' f", "perl", id="unknown-interpreter"),
            pytest.param('python -c "$CODE"', "python -c", id="python-code-from-expansion"),
            pytest.param("python -m pip install x", "python -m pip", id="python-unlisted-module"),
            pytest.param("python - <<EOF\nprint('$X')\nEOF", "python -", id="python-unquoted-heredoc"),
            pytest.param('node -e \'require("fs").rmSync("x")\'', "node -e", id="node-inline-code"),
            pytest.param("awk '{system(\"rm \" $1)}' f", "awk system/pipe/write", id="awk-system"),
            pytest.param("curl -X POST -d x https://example.com", "curl -X POST", id="curl-post"),
            pytest.param("find . -name '*.bak' -exec mv {} /tmp \\;", "mv", id="find-exec-unlisted"),
            pytest.param("ls | xargs -I {} mv {} dst", "mv", id="xargs-unlisted"),
            pytest.param('bash -c "$RUNNER pytest -q"', "$RUNNER", id="shell-string-of-unset-variable"),
            pytest.param('X=$(cat "$F")\nbash -c "$X"', "$X", id="shell-string-of-variable-from-substitution"),
            pytest.param(
                'IFS= read -r X < "${TMPDIR:-/tmp}/x-${CSID}"\nfor X in a b; do bash -c "$X"; done',
                "$X",
                id="shell-string-of-variable-also-set-by-loop",
            ),
            pytest.param('eval "$(python "$ROOT/bin/parse.py" "$ARGS")"', "eval", id="eval-of-plugin-script-output"),
            pytest.param('kaggle competitions download "$SLUG"', "kaggle competitions download", id="kaggle-download"),
            pytest.param('unlink "$dest"', "unlink", id="unlink-non-state-path"),
            pytest.param("case x in a) mv p q;; esac", "mv", id="arm-on-case-line"),
            pytest.param("f() { mv a b; }", "mv", id="function-body"),
            pytest.param("cat <<EOF\nnow: $(mv a b)\nEOF", "mv", id="live-heredoc-substitution"),
            pytest.param("cat <<EOF\nnever closed", "unterminated heredoc", id="unterminated-heredoc"),
            pytest.param("2>/dev/null mv a b", "mv", id="redirection-before-command"),
            pytest.param('bash "$DIR/run.sh"', "shell script $DIR/run.sh", id="shell-script-operand"),
            pytest.param("trap 'mv a b' EXIT", "mv", id="trap-handler"),
            pytest.param("uv add --dev pkg", "uv add", id="uv-add"),
            pytest.param("uv run -f ruff rm -rf build", "uv run -f", id="uv-run-unknown-option"),
            pytest.param("uv run --python 3.12 ln -sf a b", "ln", id="uv-run-known-option-then-unlisted"),
        ],
    )
    def test_unlisted_commands_are_excluded(self, text: str, offender: str) -> None:
        """Any command the allowlist does not name keeps its entry out of the manifest, and is reported by name.

        The denylist misses most of these (``mv``, ``tee``, ``python -c``, ``bash -c "$RUNNER …"`` and friends) or, for
        the ``case``-line arm and the function body, cannot see the command at all. ``eval`` has no allowlist judgement
        any more, so it is unlisted by name as well as denied by the danger filter.
        """
        assert bbm.unlisted_command(text) == offender
        assert bbm.is_excluded(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param('eval "$(python "$ROOT/bin/parse.py" "$ARGS")"', id="eval-of-plugin-script-output"),
            pytest.param(
                'IFS= read -r RUNNER < "${TMPDIR:-/tmp}/qs-runner-${CSID}" 2>/dev/null || RUNNER="python -m"\n'
                'eval "$RUNNER pytest -q" > "$LOG" 2>&1',
                id="runner-eval-from-sentinel",
            ),
            pytest.param('RUNNER="uv run"\neval "$RUNNER ruff --version"', id="runner-eval-from-literal"),
            pytest.param('n=comm; n+=ands\neval "${n}[ruff]=/bin/rm"\nruff -rf x', id="eval-of-assembled-table-name"),
        ],
    )
    def test_eval_shapes_are_dangerous(self, text: str) -> None:
        """Every ``eval`` keeps its entry out of the manifest, including the runner shapes once admitted.

        ``eval`` re-parses text built at run time, so a table name assembled from literal pieces can rebind a command
        name past every name check; no shipped block runs ``eval``, so the danger filter denies it by name.
        """
        assert bbm.is_dangerous(text) is True


class TestHashing:
    """block_entries: whole-block plus per-command digests."""

    def test_block_and_command_entries(self) -> None:
        """A multi-command block contributes one block digest and one per command.

        The hook accepts either the whole block or any single line of it, so both digests must be manifested.
        """
        entries, _ = bbm.block_entries("echo alpha\necho beta", "f.md:1")
        assert entries[bbm.sha256_text("echo alpha\necho beta")]["kind"] == "block"
        assert entries[bbm.sha256_text("echo alpha")]["kind"] == "command"
        assert entries[bbm.sha256_text("echo beta")]["kind"] == "command"

    @pytest.mark.parametrize(
        ("text", "kinds", "dropped"),
        [
            pytest.param("cat <<EOF\nline one\nline two\nEOF", [], 1, id="data-heredoc-emits-nothing"),
            pytest.param(
                "python - \"$F\" <<'PY'\nimport sys\nprint(sys.argv[1])\nPY",
                ["block"],
                0,
                id="python-code-heredoc-block",
            ),
            pytest.param("echo safe\nrm -rf x", ["command"], 2, id="dangerous-block-keeps-safe-commands"),
        ],
    )
    def test_block_entry_kinds_and_dropped_count(self, text: str, kinds: list[str], dropped: int) -> None:
        """Entry kinds and the danger-filter drop count follow from each entry being judged on its own text.

        A heredoc block yields exactly one ``block`` entry; a dangerous block is dropped while its safe lines survive as
        ``command`` entries, and the number of dropped entries is reported.
        """
        entries, dropped_count = bbm.block_entries(text, "f.md:1")
        assert [record["kind"] for record in entries.values()] == kinds
        assert dropped_count == dropped

    def test_single_command_block_emitted_once_as_block(self) -> None:
        """When block and command text coincide, only the block entry is recorded.

        A one-line block would otherwise register the same digest twice, once per kind, and the second record would
        overwrite the first.
        """
        entries, _ = bbm.block_entries("echo alpha", "f.md:1")
        assert entries == {bbm.sha256_text("echo alpha"): {"kind": "block", "src": "f.md:1"}}

    def test_src_points_at_opening_fence(self, scan_dir: Path) -> None:
        """Every entry from a block carries that block's opening-fence provenance.

        The ``src`` value (file and opening-fence line) is what a reviewer follows from a manifest hit back to the
        Markdown that justified it.
        """
        manifest, _ = bbm.build_plugin(scan_dir / "cc_foundry")
        entries = manifest["entries"]
        assert isinstance(entries, dict)
        assert entries[bbm.sha256_text("echo beta")]["src"] == "skills/demo/SKILL.md:3"

    def test_non_bash_blocks_ignored(self, tmp_path: Path) -> None:
        """Only blocks detected as bash are hashed.

        A fenced Python block holds code the hook never sees as a Bash command, so it must contribute no entry.
        """
        plugin_dir = _make_plugin(tmp_path, "cc_foundry", skill_md="```python\nprint(1)\n```\n")
        manifest, _ = bbm.build_plugin(plugin_dir)
        assert manifest["entries"] == {}

    @pytest.mark.parametrize("vector", vector_params("aliases_escaped_blank"))
    def test_raw_normalization_refusals_drop_whole_block(self, tmp_path: Path, vector: dict) -> None:
        """Manifest extraction preserves raw whitespace refusals and counts each rejected block once."""
        plugin_dir = _make_plugin(tmp_path, "cc_foundry", skill_md="```bash\necho safe\n```\n")
        skill = plugin_dir / "skills" / "demo" / "SKILL.md"
        skill.write_bytes(("```bash\n" + vector["input"] + "\n```\n").encode("utf-8"))

        manifest, dropped = bbm.build_plugin(plugin_dir)

        assert dropped == int(vector["expected"])
        assert bool(manifest["entries"]) is (not vector["expected"])


class TestPosixSrc:
    """posix_src: provenance paths are forward-slashed on every host."""

    @pytest.mark.parametrize(
        ("path", "line", "expected"),
        [
            pytest.param(
                PureWindowsPath(r"skills\demo\SKILL.md"), 7, "skills/demo/SKILL.md:7", id="simulated-windows-path"
            ),
            pytest.param(PurePosixPath("agents/curator.md"), 1, "agents/curator.md:1", id="posix-path-unchanged"),
        ],
    )
    def test_provenance_path_is_forward_slashed(self, path, line: int, expected: str) -> None:
        """A Windows-flavoured path emits forward slashes, and a POSIX path passes through untouched.

        Provenance paths are written into a committed manifest, so they must read the same on every host.
        """
        assert bbm.posix_src(path, line) == expected

    def test_simulated_windows_src_is_forward_slashed_in_emitted_json(self) -> None:
        """The encoded manifest contains no backslash in a Windows-derived src.

        The Windows path is simulated with ``PureWindowsPath`` so the case runs on every host; a backslash in the JSON
        would make the committed manifest differ between operating systems.
        """
        src = bbm.posix_src(PureWindowsPath(r"rules\_full\claude-config.md"), 42)
        payload = bbm.encode_manifest({"schema": 1, "entries": {"deadbeef": {"kind": "block", "src": src}}})
        text = payload.decode("utf-8")
        assert '"src": "rules/_full/claude-config.md:42"' in text
        assert "\\\\" not in text

    def test_generated_manifest_has_no_backslash_src(self, scan_dir: Path) -> None:
        """No src emitted by a real build contains a backslash.

        Builds the scratch plugin end to end, so the check covers the whole path from file discovery to the record.
        """
        manifest, _ = bbm.build_plugin(scan_dir / "cc_foundry")
        entries = manifest["entries"]
        assert isinstance(entries, dict)
        assert all("\\" not in record["src"] for record in entries.values())


class TestManifestShape:
    """Manifest identity and encoding."""

    def test_plugin_label_uses_directory_name(self, scan_dir: Path) -> None:
        """Identity is ``<dir-name>@<version>``, not plugin.json's ``name`` field.

        The scratch plugin.json names itself ``foundry`` while its directory is ``cc_foundry``; the label must follow
        the directory so it matches the manifest file a plugin ships.
        """
        assert bbm.plugin_label(scan_dir / "cc_foundry") == "cc_foundry@1.2.3"

    def test_encoding_is_sorted_ascii_bytes_with_newline(self) -> None:
        """Encoding matches the committed-manifest convention exactly.

        Sorted keys, two-space indent, a trailing newline and no carriage returns keep regenerated manifests byte-
        stable, which the drift gate depends on.
        """
        payload = bbm.encode_manifest({"schema": 1, "plugin": "p@1", "entries": {}})
        assert payload.endswith(b"\n")
        assert payload.startswith(b'{\n  "entries": {}')
        assert b"\r" not in payload


class TestCli:
    """Main: ``--check`` / ``--update`` behaviour and determinism."""

    def test_update_writes_all_target_plugins(self, scan_dir: Path) -> None:
        """``--update`` creates a manifest for each target plugin.

        All four target plugins are present in the scratch tree, so each must end with its own manifest file.
        """
        assert bbm.main(["--update", "--scan-dir", str(scan_dir)]) == 0
        for name in bbm.TARGET_PLUGINS:
            assert (scan_dir / name / bbm.MANIFEST_NAME).is_file()

    def test_check_passes_after_update(self, scan_dir: Path) -> None:
        """``--check`` is clean immediately after ``--update``.

        Generate-then-verify is one round trip: a check that disagreed with its own generator would fail every commit.
        """
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        assert bbm.main(["--check", "--scan-dir", str(scan_dir)]) == 0

    def test_check_detects_drift(self, scan_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--check`` fails when a source block changed after the manifest was written.

        The skill's block is rewritten after ``--update``; the stale manifest must be reported as drift on stderr.
        """
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        skill = scan_dir / "cc_foundry" / "skills" / "demo" / "SKILL.md"
        skill.write_text("```bash\necho gamma\n```\n", encoding="utf-8")
        capsys.readouterr()
        assert bbm.main(["--check", "--scan-dir", str(scan_dir)]) == 1
        assert "BLUEPRINT-MANIFEST-DRIFT: cc_foundry" in capsys.readouterr().err

    def test_check_treats_missing_manifest_as_drift(self, scan_dir: Path) -> None:
        """A never-generated manifest is drift, not a pass.

        A plugin that ships blocks but no manifest would otherwise auto-allow nothing yet look verified.
        """
        assert bbm.main(["--check", "--scan-dir", str(scan_dir)]) == 1

    def test_check_writes_nothing(self, scan_dir: Path) -> None:
        """``--check`` never creates or mutates a manifest file.

        The check runs in pre-commit and must stay read-only; writing here would hide the drift it reports.
        """
        bbm.main(["--check", "--scan-dir", str(scan_dir)])
        assert not (scan_dir / "cc_foundry" / bbm.MANIFEST_NAME).exists()

    def test_missing_plugin_directory_fails(self, scan_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing target plugin is reported and exits non-zero.

        ``cc_oss`` is dissolved into the scan root, so one of the four target plugin directories no longer exists.
        """
        for entry in (scan_dir / "cc_oss").iterdir():
            entry.rename(scan_dir / entry.name)
        (scan_dir / "cc_oss").rmdir()
        assert bbm.main(["--update", "--scan-dir", str(scan_dir)]) == 1
        assert "plugin directory not found" in capsys.readouterr().err

    def test_two_runs_are_byte_identical(self, scan_dir: Path) -> None:
        """Regeneration is deterministic — the drift gate depends on it.

        Two consecutive ``--update`` runs over unchanged sources must write identical bytes.
        """
        manifest_path = scan_dir / "cc_foundry" / bbm.MANIFEST_NAME
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        first = manifest_path.read_bytes()
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        assert manifest_path.read_bytes() == first

    def test_manifest_json_shape(self, scan_dir: Path) -> None:
        """The written manifest carries schema, plugin identity, and entry records.

        The scratch plugin's CLAUDE.md holds one ``ls -la`` block, which must appear keyed by its digest with its
        provenance.
        """
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        manifest = json.loads((scan_dir / "cc_foundry" / bbm.MANIFEST_NAME).read_text(encoding="utf-8"))
        assert manifest["schema"] == 1
        assert manifest["plugin"] == "cc_foundry@1.2.3"
        assert manifest["entries"][bbm.sha256_text("ls -la")] == {"kind": "block", "src": "CLAUDE.md:1"}

    def test_mode_flag_is_required(self, scan_dir: Path) -> None:
        """Neither ``--check`` nor ``--update`` given is a usage error.

        argparse exits with status 2 before any file is read or written.
        """
        with pytest.raises(SystemExit) as excinfo:
            bbm.main(["--scan-dir", str(scan_dir)])
        assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("IFS= read -r X < \"${TMPDIR:-/tmp}/x\"; printf '%1$n' PATH", True, id="positional-percent-n"),
        pytest.param("IFS= read -r X < \"${TMPDIR:-/tmp}/x\"; printf %d 'IFS-=1'", True, id="numeric-ifs-subtract"),
        pytest.param("typeset -F n=1,PATH=1", True, id="float-declaration"),
        pytest.param("exit -- '-1,PATH=1'", True, id="exit-negative-count"),
        pytest.param("bye '1,PATH=1'", True, id="bye-count"),
        pytest.param("logout '1,PATH=1'", True, id="logout-count"),
        pytest.param("print -f%d '1,PATH=1'", True, id="print-attached-format"),
        pytest.param("ZLE_RPROMPT_INDENT=1,PATH=1", True, id="integer-special-parameter"),
        pytest.param("echo $(( PATH ||= 1 ))", True, id="logical-or-assignment"),
        pytest.param("echo $(( ++IFS ))", True, id="prefix-ifs-increment"),
        pytest.param('IFS= read -r X < "${TMPDIR:-/tmp}/x"', False, id="sentinel-read-prefix"),
        pytest.param("grep -- --path=x f", False, id="lowercase-path-flag"),
        pytest.param("[[ $a == PATH ]]", False, id="equality-comparison"),
        pytest.param("echo PATH", False, id="bare-lookup-name"),
        pytest.param("echo '-PATH=1'", False, id="quoted-single-dash-flag"),
        pytest.param("echo --PATH=1", False, id="double-dash-flag"),
        pytest.param("echo OTHER_PATH=1", False, id="identifier-suffix"),
        pytest.param("printf '%%n' PATH", False, id="escaped-percent"),
        pytest.param("IFS=',' command read -r X", False, id="wrapped-nonempty-read-prefix"),
        pytest.param('echo "PATH == 1"', False, id="quoted-equality"),
        pytest.param('echo CDP""ATH=0', False, id="dequoted-inert-word"),
        pytest.param("python x.py CD$'P'ATH=0", False, id="ansi-c-inert-word"),
        pytest.param('unknown --CDP""ATH=0', False, id="dequoted-flag-prefix"),
        pytest.param('unknown CDP""ATH=0', True, id="dequoted-unknown-operand"),
        pytest.param('python x.py "$(echo CDP""ATH=0)"', False, id="nested-inert-word-owner"),
        pytest.param('printf %""n IFS', True, id="dequoted-percent-n"),
        pytest.param("printf %s 'PATH=1'", False, id="string-conversion-data"),
        pytest.param("printf '%b %c %q' 'PATH=1' 'IFS=1' 'CDPATH=1'", True, id="text-conversions-data"),
        pytest.param("printf %s '%n'", False, id="percent-n-data-stays-conservative"),
        pytest.param("printf %*s 'PATH=1' x", True, id="numeric-width-keeps-check"),
        pytest.param("printf '%s %d' x 'PATH=1'", True, id="mixed-format-keeps-check"),
        pytest.param("printf '$F' IFS", False, id="single-quoted-dollar-format"),
        pytest.param('print -- "$value"', False, id="print-without-format"),
    ],
)
def test_position_independent_lookup_writes(command: str, expected: bool) -> None:
    """Refuse writes wherever an operand appears while retaining comparisons, flags and read prefixes.

    A write to a shell lookup variable (``PATH``, ``IFS``, ``CDPATH``) can hide in a printf format, a numeric operand or
    an arithmetic expansion; comparisons, ordinary flags and the sentinel read prefix must stay allowed.
    """
    assert bbm.is_dangerous(command) is expected


@pytest.mark.parametrize(
    "command",
    [
        'print -f%d CDP""ATH=0',
        r"print -f%d CD\PATH=0",
        "print -f%d CD$'P'ATH=0",
        "print -f%d $'CD'PATH=0",
        "print -f%d CDPATH$''=0",
        "print -f%d CD\\\nPATH=0",
        'printf -""v PATH %s /tmp',
        r"printf -\v PATH %s /tmp",
        "printf '-v' PATH %s /tmp",
        'F=%n; printf "$F" IFS',
        'printf "${F:-%n}" IFS',
        'print -f "$F" IFS',
        "[[ 0 -eq 'CDPATH\n=0' ]]",
        "[[ 0 -eq 'CDPATH\r=0' ]]",
        "[[ 0 -eq 'CDPATH\v=0' ]]",
        "[[ 0 -eq 'CDPATH\f=0' ]]",
        "[[ 0 -eq CDPATH$'\\n'=0 ]]",
        "[[ 0 -eq CDPATH\\\n=0 ]]",
        "case x in x) printf '%1$n' PATH;; esac",
        "repeat 1 printf '%1$n' PATH",
        "for x (a) printf '%1$n' PATH",
        "if true { printf '%1$n' PATH }",
        "command printf '%1$n' PATH",
        "builtin printf '%1$n' PATH",
        "env printf '%1$n' PATH",
        "timeout 1 printf '%1$n' PATH",
        "xargs printf '%1$n' PATH",
    ],
)
def test_obfuscated_lookup_writes_are_refused(command: str) -> None:
    """Shell quote removal, every arithmetic blank, and every compound or wrapper must keep lookup-write refusals.

    The first group hides a write to ``PATH``, ``IFS`` or ``CDPATH`` behind quoting, line continuations or odd blanks;
    the second places a positional ``%n`` format inside a ``case``, ``for``, ``if`` or wrapper command, each of which
    has its own resolver that could lose the refusal.
    """
    assert bbm.is_dangerous(command) is True


@pytest.mark.parametrize(
    "operator", ["=", "+=", "-=", "*=", "/=", "%=", "**=", "<<=", ">>=", "&=", "|=", "^=", "&&=", "||=", "^^="]
)
@pytest.mark.parametrize("quoted", ["{}", '"{}"', "'{}'", "$'{}'"])
def test_raw_lookup_write_operator_matrix(operator: str, quoted: str) -> None:
    """Single-quoted echo operands are data; other quote states retain expression protection.

    Every assignment operator is tried against bare, double-quoted, single-quoted and ANSI-C quoted operands; only the
    single-quoted form is inert text the shell never evaluates.
    """
    command = "echo " + quoted.format(f"1,PATH[0] {operator} 1")
    assert bbm.is_dangerous(command) is (quoted != "'{}'")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("exit test-PATH=1", True, id="exit-hyphenated-lvalue"),
        pytest.param("git log; (( a-PATH=1 ))", True, id="arithmetic-after-external-command"),
        pytest.param("echo $(( 1-PATH=1 ))", True, id="echo-arithmetic-expansion"),
        pytest.param("python x.py $(( PATH=1 ))", True, id="external-arithmetic-expansion"),
        pytest.param("git() { (( $1 )); }; git 'a-PATH=1'", True, id="shadowed-external-command"),
        pytest.param("python x.py --path-out f", False, id="external-long-flag"),
        pytest.param('printf "export PATH=%s\\n" "$value"', False, id="printed-export-format"),
        pytest.param('echo "set PATH=x"', False, id="echo-assignment-message"),
        pytest.param("exit '--PATH-a'", True, id="leading-dashes-can-decrement"),
        pytest.param("printf %d '1-PATH=1'", True, id="numeric-subtraction-lvalue"),
        pytest.param("python x.py 'PATH=1'", False, id="external-assignment-shaped-argument"),
        pytest.param("python x.py 'test-PATH=1'", False, id="external-hyphenated-text"),
        pytest.param("python x.py '$(( 1-PATH=1 ))'", False, id="quoted-arithmetic-text"),
        pytest.param("echo '$[test-PATH=1]'", False, id="quoted-legacy-arithmetic"),
        pytest.param("echo '[[ test-PATH=1 ]]'", False, id="quoted-conditional-text"),
        pytest.param("python x.py 'n[PATH=1]'", False, id="external-subscript-text"),
        pytest.param("echo '${x:-test-PATH=1}'", False, id="quoted-parameter-operator"),
        pytest.param("printf 'export PATH=%1$n' PATH", True, id="format-percent-n-is-never-exempt"),
        pytest.param("print -f 'export PATH=%s' value", False, id="print-format-only"),
        pytest.param("print -f%d '1,PATH=1'", True, id="attached-format-keeps-numeric-operand"),
        pytest.param("unknown 'test-PATH=1'", True, id="unknown-command-keeps-full-rule"),
        pytest.param("'python' x.py --path-out f", True, id="quoted-command-has-no-exemption"),
        pytest.param('"$RUNNER" x.py --path-out f', True, id="expanded-command-has-no-exemption"),
        pytest.param("./inspect.py 'test-PATH=1'", False, id="literal-direct-python-script"),
        pytest.param("node inspect.js 'test-PATH=1'", False, id="literal-node-script"),
        pytest.param("git 'a-PATH=1'; git() { (( $1 )); }", True, id="later-definition-disables-exemption"),
        pytest.param("alias git='printf %d'; git 'a-PATH=1'", True, id="alias-disables-exemption"),
        pytest.param("LABEL='test-PATH=1'", True, id="ordinary-assignment-has-no-exemption"),
        pytest.param('python x.py "$(exit test-PATH=1)"', True, id="nested-command-owns-operand"),
        pytest.param("python x.py \"$(echo 'test-PATH=1')\"", False, id="nested-echo-owns-inert-operand"),
        pytest.param(
            "python x.py --path-out f; exit 'python x.py --path-out f'",
            True,
            id="literal-lookalike-cannot-borrow-context",
        ),
        pytest.param("python x.py '[^\"]'; IFS= read -r X < f", False, id="regex-context-stays-in-word"),
        pytest.param("while IFS='=' read -r k v; do echo \"$k\"; done", False, id="while-read-prefix-is-preserved"),
        pytest.param('python x.py "program-path=${OUT}"', False, id="label-outside-parameter-expansion"),
    ],
)
def test_lookup_write_context_exemptions(command: str, expected: bool) -> None:
    """Only inert positions of literal unshadowed commands may exempt raw lookup-write tokens.

    An assignment-shaped word is harmless as an argument of an external command or inside a quoted message, but a
    shadowing function, an alias, a quoted command name or a nested command that owns the operand removes the exemption.
    """
    assert bbm.is_dangerous(command) is expected


@pytest.mark.parametrize("vector", vector_params("closed_text_formats") + vector_params("literal_builtin_operands"))
def test_closed_text_formats(vector: dict) -> None:
    """Closed formats and literal name operands keep inert text separate from live writes.

    The vectors come from the shared fixture, so the hook is held to the same closed format grammar as the generator.
    """
    # Gate A confirmed the closed grammar validates dash-leading formats; HEAD admitted printf -- '-%s' x.
    assert bbm.is_dangerous(vector["input"]) is vector["expected"]


@pytest.mark.parametrize("vector", vector_params("literal_data_pairs"))
def test_literal_data_positions_preserve_live_refusals(vector: dict) -> None:
    """Each inert-data exemption retains refusal in the corresponding live write positions.

    Every vector pairs commands where the lookup name is only text with the same shapes where it is written; widening
    the exemption to the live shapes would let a real write through.
    """
    assert [command for command in vector["inert"] if bbm.is_dangerous(command) is not False] == []
    assert [command for command in vector["live"] if bbm.is_dangerous(command) is not True] == []


@pytest.mark.parametrize(
    ("builtin", "before", "after"),
    [
        pytest.param("printf", "", "", id="printf"),
        pytest.param("printf", "-- ", "", id="printf-end-options"),
        pytest.param("print -f", "", "", id="print-f"),
        pytest.param("print -f", "", "-- ", id="print-f-end-options"),
        pytest.param("print -rf", "", "", id="print-rf"),
        pytest.param("print -rf", "", "-- ", id="print-rf-end-options"),
        pytest.param("env printf", "", "", id="env-printf"),
        pytest.param("env printf", "-- ", "", id="env-printf-end-options"),
    ],
)
@pytest.mark.parametrize("vector", vector_params("closed_format_matrix"))
def test_closed_format_grammar_matrix(builtin: str, before: str, after: str, vector: dict) -> None:
    """Format admission depends on the closed grammar even when a format begins with a dash.

    The same format is tried under ``printf``, ``print -f``, ``print -rf`` and ``env printf``, with and without an end-
    of-options marker (placed before the format for printf, after it for print), so a dash-leading format cannot be
    mistaken for an option and slip past the grammar.
    """
    command = f"{builtin} {before}'{vector['format']}' {after}PATH x"
    assert bbm.is_dangerous(command) is (not vector["admitted"]), command


def test_dash_leading_print_format_cannot_write_lookup_name() -> None:
    """A dash-leading print format never escapes the percent-n prohibition.

    ``%n`` stores a character count into the named variable; a format beginning with a dash must not be read as an
    option that hides the conversion.
    """
    assert bbm.is_dangerous("print -f '-%nf%s' PATH x") is True
