"""Subprocess tests for ``hooks/blueprint-allow.js``.

The hook is a ``PreToolUse`` gate that auto-allows a Bash command whose normalized
text is an exact digest hit in the plugin's ``blueprint-manifest.json`` — i.e. the
command is verbatim text from a reviewed, versioned plugin Markdown file. Its
security contract:

* **Allow-original, never rewrite** — no ``updatedInput`` is emitted, so
  settings.json deny rules keep matching the original command string.
* **Provenance, not shape** — the digest must match; a one-character deviation,
  a spliced pair of halves, or a missing/malformed manifest passes through
  (empty stdout, exit 0) to real permission checking.
* **Composition safety** — a multi-line command is allowed only when EVERY
  logical command in it is independently a manifest entry.
* **Defence in depth** — an independent ``is_dangerous`` re-check refuses even on
  a digest hit, so a corrupted or tampered manifest cannot buy an auto-allow. It
  runs on the NORMALIZED text, i.e. exactly the text that was hashed and looked
  up, so the check judges what the layer above it matched rather than a comment
  the shell would never execute.

Test manifests are injected by mirroring the real plugin layout under ``tmp_path``
(``hooks/blueprint-allow.js`` + ``blueprint-manifest.json`` at the root), which is
what the hook's own ``__dirname/..`` resolution expects. ``CLAUDE_PLUGIN_ROOT`` is
pinned to the same directory in every subprocess so the fallback can never reach
the repository's real manifest and flake a passthrough case into an allow.
Digests are produced by the Python generator, so every allow is itself a
cross-language check that both normalizers agree.

The hook's pure functions (``normalize``, ``isDangerous`` ...) are exercised on hundreds of shared vectors. Those calls
go through ONE long-lived node process per module (the ``hook_js`` fixture) instead of one process per vector: the same
real JavaScript runs on every vector, only the process start-up is shared. Tests whose contract is the stdin/stdout
hook run, a per-test plugin root or the process exit behaviour keep their own real subprocess.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import build_blueprint_manifest as bbm
import pytest
from _blueprint_vectors import VECTORS, vector_params
from _hook_io import is_allowed

HOOK = Path(__file__).resolve().parents[3] / "hooks" / "blueprint-allow.js"


def _literal_data_params() -> list:
    """Return one case per command of the ``literal_data_pairs`` vectors, flagged True when it is a live write."""
    return [
        pytest.param(command, kind == "live", id=f"{vector['id']}-{kind}-{index}")
        for vector in VECTORS["literal_data_pairs"]
        for kind in ("inert", "live")
        for index, command in enumerate(vector[kind])
    ]


NODE_UNAVAILABLE = shutil.which("node") is None

#: Blueprint texts seeded into every synthetic manifest, keyed by role.
SINGLE = 'RUN_DIR="$(cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}")"'
BLOCK = 'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\nmkdir -p .reports/review\necho "$CSID"'
OTHER = "git rev-parse --show-toplevel"

#: Request/reply loop of the long-lived node worker: one JSON line in, one JSON line out, errors reported not fatal.
_WORKER_JS = (
    "const h = require(process.argv[1]);"
    "require('readline').createInterface({input: process.stdin}).on('line', (line) => {"
    "const call = JSON.parse(line);"
    "let reply;"
    "try { reply = {ok: true, value: h[call.fn](call.arg)}; } catch (error) { reply = {ok: false, error: String(error)}; }"
    "process.stdout.write(JSON.stringify(reply) + '\\n');"
    "});"
)


@pytest.fixture(name="plugin_root")
def _plugin_root(tmp_path: Path) -> Path:
    """Mirror of the plugin layout: a copy of the hook under ``hooks/``, root empty."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(HOOK, hooks / HOOK.name)
    return tmp_path


def _manifest_bytes(texts: dict[str, str]) -> bytes:
    """Encode a manifest whose entries are the digests of ``texts`` (src label -> text).

    Examples:
        >>> b'"plugin": "cc_foundry@0.0.0"' in _manifest_bytes({"skill.md:1": "echo hi"})
        True
    """
    entries = {bbm.sha256_text(text): {"kind": "block", "src": src} for src, text in texts.items()}
    return bbm.encode_manifest({"schema": 1, "plugin": "cc_foundry@0.0.0", "entries": entries})


def _run_node(
    args: list[str], *, stdin: str | None = None, plugin_root: Path | None = None
) -> subprocess.CompletedProcess:
    """Run ``node`` once, pinning ``CLAUDE_PLUGIN_ROOT`` to ``plugin_root`` when one is given."""
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root)} if plugin_root else None
    return subprocess.run(
        ["node", *args], input=stdin, capture_output=True, encoding="utf-8", env=env, timeout=10, check=False
    )


def _hook_result(proc: subprocess.CompletedProcess) -> dict:
    """Parse one hook run's stdout (empty means passthrough), asserting the hook exited 0."""
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    out = (proc.stdout or "").strip()
    return json.loads(out) if out else {}


def _bash_stdin(command: str, tool_name: str = "Bash") -> str:
    """Return the PreToolUse stdin payload for one command."""
    return json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})


@pytest.fixture(name="run_blueprint")
def _run_blueprint(plugin_root: Path) -> Callable[..., dict]:
    """Return a callable that writes a manifest, runs the hook, and parses its stdout."""

    def _run(command: str, *, manifest: bytes | None = None, tool_name: str = "Bash") -> dict:
        """Run the copied hook with one optional fixture manifest."""
        if manifest is not None:
            (plugin_root / "blueprint-manifest.json").write_bytes(manifest)
        proc = _run_node(
            [str(plugin_root / "hooks" / HOOK.name)], stdin=_bash_stdin(command, tool_name), plugin_root=plugin_root
        )
        return _hook_result(proc)

    return _run


@pytest.fixture(name="seeded")
def _seeded() -> bytes:
    """Manifest holding the single command, the multi-line block, and one unrelated entry."""
    return _manifest_bytes(
        {
            "skills/review/SKILL.md:12": bbm.normalize(SINGLE),
            "skills/review/SKILL.md:40": bbm.normalize(BLOCK),
            "rules/git.md:7": bbm.normalize(OTHER),
        }
    )


@pytest.fixture(name="seeded_root")
def _seeded_root(plugin_root: Path, seeded: bytes) -> Path:
    """Plugin root whose ``blueprint-manifest.json`` is the seeded manifest."""
    (plugin_root / "blueprint-manifest.json").write_bytes(seeded)
    return plugin_root


@pytest.fixture(scope="module", name="hook_js")
def _hook_js(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[[str, object], object]]:
    """Return ``call(fn, arg)`` evaluating one export of the shipped hook in a single shared node process.

    The worker requires the real hook once and answers one JSON request per line, so hundreds of vectors cost one
    process start. A watchdog kills a hung worker after 10 s (the former per-vector timeout); its stderr goes to a file
    that the failure message quotes.
    """
    log = tmp_path_factory.mktemp("hook-js") / "stderr.log"
    with (
        log.open("wb") as stderr,
        subprocess.Popen(
            ["node", "-e", _WORKER_JS, str(HOOK)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            encoding="utf-8",
        ) as proc,
    ):
        assert proc.stdin is not None
        assert proc.stdout is not None

        def call(fn: str, arg: object) -> object:
            """Evaluate ``h[fn](arg)`` in the worker and return its JSON-decoded result."""
            watchdog = threading.Timer(10, proc.kill)
            watchdog.start()
            try:
                proc.stdin.write(json.dumps({"fn": fn, "arg": arg}) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            finally:
                watchdog.cancel()
            assert line, f"node worker died or hung on {fn}: {log.read_text(encoding='utf-8', errors='replace')}"
            reply = json.loads(line)
            assert reply["ok"], f"hook.{fn} threw: {reply['error']}"
            return reply["value"]

        yield call
        proc.stdin.close()


@pytest.fixture(name="is_dangerous")
def _is_dangerous(hook_js: Callable[[str, object], object]) -> Callable[[str], object]:
    """Return ``is_dangerous(command)``, the shipped hook's independent risk classification."""
    return lambda command: hook_js("isDangerous", command)


# ── Verbatim blueprint text is allowed ────────────────────────────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
class TestVerbatimAllow:
    """A command whose normalized text is a manifest entry gets permissionDecision allow."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(SINGLE, id="single-command"),
            pytest.param(BLOCK, id="whole-block"),
            pytest.param(f"{SINGLE}  # resolve the run dir", id="trailing-comment"),
            pytest.param(SINGLE + "   ", id="trailing-whitespace"),
        ],
    )
    def test_manifest_entry_text_allowed(self, run_blueprint: Callable[..., dict], seeded: bytes, command: str) -> None:
        """Blueprint text sent verbatim, or varying only in what normalization strips, matches its entry.

        The single command is the dominant blueprint class the prefix matcher cannot express at all: it opens with an
        assignment and contains ``$(...)``, so no settings.json allow rule can ever match it. The block's own lines are
        NOT in the manifest, so its allow proves the whole-block digest path fired rather than the per-command
        fallback. Comment stripping runs before hashing, so an explanatory trailing ``# ...`` (no separator) does not
        cost the allow. Trailing whitespace is right-stripped by the generator too (leading whitespace must still
        miss, see ``TestPassthrough``). Only shell space and tab can be discarded safely at line endings.
        """
        result = run_blueprint(command, manifest=seeded)

        assert is_allowed(result), f"{command!r} should be allowed, got: {result}"

    def test_every_logical_command_hits(self, run_blueprint: Callable[..., dict]) -> None:
        """A multi-line command is allowed when each of its logical commands is an entry.

        Nothing matches the combined text, so only the composition path can allow it — and it may only do so with full,
        not partial, coverage.
        """
        manifest = _manifest_bytes({"a.md:1": bbm.normalize(SINGLE), "b.md:2": bbm.normalize(OTHER)})

        result = run_blueprint(f"{SINGLE}\n{OTHER}", manifest=manifest)

        assert is_allowed(result), f"fully covered pair should be allowed, got: {result}"

    def test_allow_reports_src_provenance(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """The allow reason names the plugin file the text came from."""
        result = run_blueprint(SINGLE, manifest=seeded)

        assert result["hookSpecificOutput"]["permissionDecisionReason"] == (
            "plugin blueprint — verbatim block from skills/review/SKILL.md:12"
        )

    def test_allow_emits_no_updated_input(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """Allow must not rewrite the command — deny rules keep matching the original."""
        result = run_blueprint(SINGLE, manifest=seeded)

        assert "updatedInput" not in result["hookSpecificOutput"]


# ── Anything not provably verbatim blueprint text passes through ──────────────


@_skip_node_unavailable
class TestPassthrough:
    """Every miss, deviation, or ambiguity falls through to the real permission prompt."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(SINGLE.replace("oss-review", "oss-reviex"), id="one-character-deviation"),
            pytest.param(" " + SINGLE, id="leading-space-not-stripped"),
            pytest.param(SINGLE.replace('"$(cat', "$(cat"), id="quoting-deviation"),
            pytest.param(f"{SINGLE}\ncat /etc/passwd", id="partial-coverage-appended-line"),
            pytest.param(f"cat /etc/passwd\n{SINGLE}", id="partial-coverage-prepended-line"),
            pytest.param(SINGLE.split("=", 1)[0] + "=" + OTHER.split(" ", 1)[1], id="recombined-halves-of-two-entries"),
            pytest.param("", id="empty-command"),
            pytest.param("   ", id="whitespace-only"),
            pytest.param("ls -la", id="unrelated-command"),
        ],
    )
    def test_deviations_passthrough(self, run_blueprint: Callable[..., dict], seeded: bytes, command: str) -> None:
        """Text that is not verbatim blueprint content is never auto-allowed.

        Splicing the first half of one entry onto the second half of another is the composition attack the manifest
        design exists to refuse: fragments are never trusted, only whole reviewed commands, and neither the spliced
        whole nor either fragment is a manifest digest. A leading space must miss because the pipeline right-strips
        only; a hook trimming both ends would normalize more aggressively than the generator.
        """
        result = run_blueprint(command, manifest=seeded)

        assert result == {}, f"{command!r} was allowed — gate bypass risk: {result}"

    @pytest.mark.parametrize(
        "manifest",
        [
            pytest.param(None, id="missing-manifest"),
            pytest.param(b'{"schema": 1, "entries": {', id="malformed-manifest-json"),
            pytest.param(b'{"schema": 1, "entries": []}', id="entries-wrong-type"),
        ],
    )
    def test_unusable_manifest_allows_nothing(self, run_blueprint: Callable[..., dict], manifest: bytes | None) -> None:
        """An absent, truncated or corrupt manifest allows nothing and still exits 0.

        Scenario: no manifest on disk; a truncated manifest; a manifest whose ``entries`` is not an object is rejected
        rather than indexed. Each is treated as absent, never as a crash.
        """
        result = run_blueprint(SINGLE, manifest=manifest)

        assert result == {}

    def test_non_bash_tool(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """Non-Bash tool payloads are ignored even when the text would match."""
        result = run_blueprint(SINGLE, manifest=seeded, tool_name="Read")

        assert result == {}

    def test_malformed_stdin(self, plugin_root: Path) -> None:
        """Malformed stdin never crashes or blocks.

        The hook is fed text that is not JSON; it must exit 0 with empty stdout so the real permission check proceeds.
        """
        proc = _run_node([str(plugin_root / "hooks" / HOOK.name)], stdin="not json")

        assert (proc.returncode, (proc.stdout or "").strip()) == (0, "")


# ── Defence in depth: a tampered manifest still cannot buy an allow ───────────


@_skip_node_unavailable
class TestTamperedManifestRefused:
    """The independent danger re-check overrides a digest hit."""

    @pytest.mark.parametrize(
        "command",
        [
            'BUILD=$(rm -rf "$HOME/build")',
            "echo done && git push origin main",
            "find .cache -type f -mtime +30 -delete",
            "printf x | xargs rm -f",
        ],
    )
    def test_dangerous_entry_still_refused(self, run_blueprint: Callable[..., dict], command: str) -> None:
        """A dangerous command injected into the manifest is refused despite matching.

        The generator's danger filter should make this state unreachable, so this is a
        test of the SECOND layer: a generator bug, a stale manifest, or an edited
        ``blueprint-manifest.json`` must not be enough on its own to auto-allow.
        """
        manifest = _manifest_bytes({"tampered.md:1": bbm.normalize(command)})

        result = run_blueprint(command, manifest=manifest)

        assert result == {}, f"{command!r} allowed from a tampered manifest: {result}"

    def test_generator_never_emits_the_tampered_entry(self) -> None:
        """The hand-crafted tamper case is genuinely one the generator rejects.

        Without this the defence-in-depth test could be passing for the wrong reason — a case the generator drops anyway
        is only interesting if the manifest COULD hold it, so the test proves the entry is synthetic, not reachable.
        """
        entries, dropped = bbm.block_entries(bbm.normalize('BUILD=$(rm -rf "$HOME/build")'), "f.md:1")

        assert (entries, dropped) == ({}, 1)


# ── Shared cross-language vectors ─────────────────────────────────────────────


@_skip_node_unavailable
class TestSharedVectors:
    """The JS port must reproduce the Python pipeline on every shared vector."""

    @pytest.mark.parametrize("vector", vector_params("normalize"))
    def test_normalize(self, hook_js: Callable[[str, object], object], vector: dict) -> None:
        """Match JavaScript normalization with the shared fixture and Python implementation.

        Asymmetry here is the one real bug class of this design: a hook that
        normalizes more aggressively than the generator lets a crafted command
        collide with a manifest digest.
        """
        assert hook_js("normalize", vector["input"]) == vector["expected"]

    @pytest.mark.parametrize("vector", vector_params("split_logical_commands"))
    def test_split_logical_commands(self, hook_js: Callable[[str, object], object], vector: dict) -> None:
        """Match JavaScript command splitting with the generator."""
        assert hook_js("splitLogicalCommands", vector["input"]) == vector["expected"]

    @pytest.mark.parametrize(
        ("js_function", "vector"),
        [
            pytest.param(js_function, vector, id=f"{group}-{vector['id']}")
            for group, js_function in (
                ("needs_bailout", "needsBailout"),
                ("aliases_escaped_blank", "aliasesEscapedBlank"),
                ("is_dangerous", "isDangerous"),
            )
            for vector in VECTORS[group]
        ],
    )
    def test_boolean_decision(self, hook_js: Callable[[str, object], object], js_function: str, vector: dict) -> None:
        """Match the generator's bailout, escaped-blank and risk decisions on every shared vector.

        Bailout is the per-command splitter's refusal to reason about a text. The escaped-blank check runs on the raw
        command before normalization; a shape only one side refuses either drops a shipped entry or lets the shell's
        split reading through on a digest hit. The risk classification is the asymmetry that matters most: the
        generator's filter decides what reaches the manifest and the hook's decides what a digest hit is still allowed
        to do, so a command only one side calls dangerous means the secondary defense is no longer independent of the
        layer it exists to backstop.
        """
        assert hook_js(js_function, vector["input"]) is vector["expected"]

    def test_digests_agree_across_languages(self, hook_js: Callable[[str, object], object]) -> None:
        """Python and JS produce the same digest for the same normalized text.

        The hash is the actual join between generator and hook; equal normalization with a different encoding assumption
        would still miss every entry.
        """
        text = bbm.normalize(BLOCK)

        assert hook_js("sha256Text", text) == bbm.sha256_text(text)


PLUGIN_DIR = HOOK.parent.parent


def _committed_block() -> tuple[str, str]:
    """Return (raw block text, normalized text) for a real committed manifest entry.

    Picks the first shipped bash block whose RAW text differs from its normalized form — i.e. one carrying comments or
    blank lines — so exercising it proves the whole comment-strip pipeline, not just a digest lookup of already-clean
    text.
    """
    entries = json.loads((PLUGIN_DIR / bbm.MANIFEST_NAME).read_text(encoding="utf-8"))["entries"]
    for filepath in bbm.collect_md_files(PLUGIN_DIR):
        text = Path(filepath).read_text(encoding="utf-8", errors="replace")
        for block in bbm.parse_blocks(text, filepath):
            if block.lang_detected != "bash":
                continue
            normalized = bbm.normalize(block.content)
            if not normalized or normalized == block.content:
                continue
            if bbm.sha256_text(normalized) in entries:
                return block.content, normalized
    return "", ""


@_skip_node_unavailable
@pytest.mark.skipif(
    not (PLUGIN_DIR / bbm.MANIFEST_NAME).is_file(),
    reason="cc_foundry blueprint-manifest.json has not been generated in this checkout",
)
class TestRealManifest:
    """The committed cc_foundry manifest works end to end with the shipped hook."""

    def test_committed_block_allowed_and_mutation_refused(self) -> None:
        """Raw shipped block text is allowed; a one-character mutation of it is not.

        This is the only case running the SHIPPED hook against the SHIPPED manifest via
        ``__dirname`` resolution — the path a real session takes. The raw text is fed in
        exactly as it appears in the Markdown file, comments and all, so an allow proves
        normalization, hashing and lookup line up end to end rather than in a fixture.
        """
        raw, normalized = _committed_block()
        assert raw, "no committed bash block with comments/blank lines found to exercise"
        mutated = normalized[:-1] + ("y" if normalized[-1] == "x" else "x")

        verdicts = [_hook_result(_run_node([str(HOOK)], stdin=_bash_stdin(command))) for command in (raw, mutated)]

        assert is_allowed(verdicts[0]), f"committed block should be allowed: {raw!r}"
        assert verdicts[1] == {}, f"mutated block was allowed: {mutated!r}"


# ── Library entry point added for the dispatcher ──────────────────────────────


def _evaluate(plugin_root: Path, stdin: str) -> dict:
    """Require the copied hook as a module and return ``evaluate``'s result for one raw stdin text."""
    script = (
        f"const h = require({json.dumps(str(plugin_root / 'hooks' / HOOK.name))});"
        f"process.stdout.write(JSON.stringify(h.evaluate({json.dumps(stdin)})));"
    )
    proc = _run_node(["-e", script], plugin_root=plugin_root)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        pytest.param(SINGLE + "\r# ; echo hidden", id="cr-before-comment-command"),
        pytest.param(SINGLE + "\r\n# comment", id="crlf-before-comment"),
        pytest.param(SINGLE + "\r" + OTHER, id="cr-composition"),
        pytest.param(BLOCK.replace("\n", "\r\n"), id="crlf-block"),
        *[
            pytest.param(SINGLE + char, id=f"trailing-u{ord(char):04x}")
            for char in "\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2028\u2029\u202f\u205f\u3000"
        ],
        pytest.param(OTHER + "\xa0 # comment", id="non-shell-blank-before-comment"),
    ],
)
def test_normalization_alias_passthrough(command: str, run_blueprint: Callable[..., dict], seeded_root: Path) -> None:
    """Shell word characters discarded by normalization never acquire a manifest allow in either entry point."""
    assert run_blueprint(command) == {}
    result = _evaluate(seeded_root, _bash_stdin(command))
    assert result["decision"] == "passthrough"
    assert result["payload"] is None


@_skip_node_unavailable
class TestEvaluateClassification:
    """``evaluate`` labels what the module did, running the same checks in the same order as ``decide``.

    The labels are what the audit record carries, so a mislabelled decline is a mis-recorded history. Three distinctions
    matter and each is asserted separately:

    * ``passthrough`` versus ``none`` — examined and declined, versus never examined at all;
    * which check declined it — a danger refusal, a missing manifest and a plain miss are different facts;
    * digest presence — the digest exists exactly when the command was normalized, and is never invented.
    """

    def test_allow_carries_lane_rank_src_and_digest(self, seeded_root: Path) -> None:
        """A digest hit is rank 1 and reports the plugin file the text came from."""
        result = _evaluate(seeded_root, _bash_stdin(SINGLE))

        assert (result["decision"], result["lane"], result["rank"]) == ("allow", "blueprint", 1)
        assert result["src"] == "skills/review/SKILL.md:12"
        assert result["digest"]

    def test_danger_refusal_is_labelled_as_such(self, plugin_root: Path) -> None:
        """The independent danger re-check is a decision, and the record must say which check refused.

        A refusal on a digest hit means the manifest held something it should not — a generator bug, a stale manifest or
        an edited one. Recording it as a plain miss would hide the one case worth investigating.
        """
        command = 'BUILD=$(rm -rf "$HOME/build")'
        (plugin_root / "blueprint-manifest.json").write_bytes(_manifest_bytes({"t.md:1": bbm.normalize(command)}))

        result = _evaluate(plugin_root, _bash_stdin(command))

        assert (result["decision"], result["why"], result["payload"]) == ("passthrough", "danger", None)

    def test_missing_manifest_is_distinguished_from_a_miss(self, plugin_root: Path) -> None:
        """An unreadable manifest is a different fact from a command that simply is not in one."""
        result = _evaluate(plugin_root, _bash_stdin(SINGLE))

        assert (result["decision"], result["why"]) == ("passthrough", "manifest-unavailable")

    def test_plain_miss_is_a_passthrough_with_a_digest(self, seeded_root: Path) -> None:
        """The command was normalized and looked up, so the digest belongs on the record."""
        result = _evaluate(seeded_root, _bash_stdin("ls -la"))

        assert (result["decision"], result["why"]) == ("passthrough", "no-match")
        assert result["digest"] == bbm.sha256_text(bbm.normalize("ls -la"))

    @pytest.mark.parametrize("vector", [v for v in VECTORS["needs_bailout"] if v["expected"]])
    def test_bailout_is_labelled_separately_from_a_miss(self, seeded_root: Path, vector: dict) -> None:
        """Text the per-command splitter refuses to reason about is a bailout, not a miss.

        The bailout check is consulted only to LABEL a decline that already happened — a digest hit returns before it —
        so labelling can never turn an allow into a passthrough.
        """
        result = _evaluate(seeded_root, _bash_stdin(vector["input"]))

        assert result["decision"] == "passthrough"
        assert result["why"] in ("bailout", "no-match")

    @pytest.mark.parametrize(
        "stdin",
        [
            pytest.param("not json", id="parse-failure"),
            pytest.param(_bash_stdin(SINGLE, "Read"), id="non-bash-tool"),
            pytest.param(_bash_stdin(""), id="empty-command"),
        ],
    )
    def test_returning_before_deciding_is_none_with_no_digest(self, plugin_root: Path, stdin: str) -> None:
        """Nothing normalized the command, so there is no digest to record — and no opinion either.

        Unparsable stdin, a call to a tool other than Bash and a command that is empty all return before any decision.
        """
        result = _evaluate(plugin_root, stdin)

        assert (result["decision"], result["why"]) == ("none", "not-applicable")
        assert "digest" not in result

    def test_whitespace_only_reaches_a_decision_here(self, seeded_root: Path) -> None:
        """The two decision modules genuinely diverge on whitespace, and the record keeps both answers.

        This module normalizes first and reaches ``no-match``; the shape module trims and returns before deciding. One
        is an abstention and the other is the absence of an opinion.
        """
        assert _evaluate(seeded_root, _bash_stdin("   "))["decision"] == "passthrough"

    def test_returns_the_same_payload_decide_produces(self, seeded_root: Path) -> None:
        """One payload builder, so the two entry points can never disagree on a byte of stdout."""
        stdin = _bash_stdin(SINGLE)
        script = (
            f"const h = require({json.dumps(str(seeded_root / 'hooks' / HOOK.name))});"
            f"process.stdout.write(JSON.stringify([h.evaluate({json.dumps(stdin)}).payload,"
            f" h.decide({json.dumps(stdin)})]));"
        )
        proc = _run_node(["-e", script], plugin_root=seeded_root)

        from_evaluate, from_decide = json.loads(proc.stdout)

        assert from_evaluate == from_decide

    def test_never_exits_the_process(self, plugin_root: Path) -> None:
        """A library that calls ``process.exit`` takes its caller's other lane down with it."""
        script = (
            f"const h = require({json.dumps(str(plugin_root / 'hooks' / HOOK.name))});"
            'h.evaluate("not json"); h.evaluate(JSON.stringify({tool_name: "Bash", tool_input: {command: "ls"}}));'
            "process.stdout.write('survived');"
        )

        proc = _run_node(["-e", script], plugin_root=plugin_root)

        assert proc.stdout == "survived"


# ── isDangerous: lookup-variable writes and inert data ────────────────────────


@_skip_node_unavailable
class TestIsDangerous:
    """The runtime risk gate refuses writes to shell lookup variables while keeping inert text, flags and reads."""

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
    def test_position_independent_writes(
        self, is_dangerous: Callable[[str], object], command: str, expected: bool
    ) -> None:
        """Refuse writes wherever an operand appears while retaining comparisons, flags and read prefixes.

        Each case names a lookup variable (``PATH``, ``IFS``, ``CDPATH``) in a different position — format operand,
        arithmetic, declaration, dequoted spelling — and the verdict says whether the shell could write it there.
        """
        assert is_dangerous(command) is expected

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
        ],
    )
    def test_shell_read_spellings(self, is_dangerous: Callable[[str], object], command: str) -> None:
        """The runtime gate matches Python on shell-read spellings and arithmetic blanks.

        Quoting tricks, line continuations and control characters inside a word still reach the lookup variable when the
        shell reads the command, so every spelling must be refused.
        """
        assert is_dangerous(command) is True

    @pytest.mark.parametrize(
        "operator", ["=", "+=", "-=", "*=", "/=", "%=", "**=", "<<=", ">>=", "&=", "|=", "^=", "&&=", "||=", "^^="]
    )
    @pytest.mark.parametrize("quoted", ["{}", '"{}"', "'{}'", "$'{}'"])
    def test_operator_matrix(self, is_dangerous: Callable[[str], object], operator: str, quoted: str) -> None:
        """Single-quoted echo operands are data; other quote states retain expression protection.

        Every assignment operator is tried in every quote state, because a quote state decides whether the shell
        evaluates the text as an arithmetic expression or passes it through untouched.
        """
        command = "echo " + quoted.format(f"1,PATH[0] {operator} 1")

        assert is_dangerous(command) is (quoted != "'{}'")

    @pytest.mark.parametrize(
        "command",
        [
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
    def test_percent_n_in_resolved_commands(self, is_dangerous: Callable[[str], object], command: str) -> None:
        """Positional percent-n must refuse through every existing compound and wrapper resolver.

        The same ``printf '%1$n' PATH`` write is wrapped in each compound form and command wrapper the resolver unwraps.
        """
        assert is_dangerous(command) is True

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
    def test_context_exemptions(self, is_dangerous: Callable[[str], object], command: str, expected: bool) -> None:
        """Only inert positions of literal unshadowed commands may exempt raw lookup-write tokens.

        Literal arguments of external programs and echoed text are data; anything the shell can evaluate, or a command
        that was shadowed, aliased, quoted or expanded, keeps the full rule.
        """
        assert is_dangerous(command) is expected

    @pytest.mark.parametrize("vector", vector_params("closed_text_formats") + vector_params("literal_builtin_operands"))
    def test_closed_text_formats(self, is_dangerous: Callable[[str], object], vector: dict) -> None:
        """Closed formats and literal name operands keep inert text separate from live writes.

        Gate A confirmed the closed grammar validates dash-leading formats; HEAD admitted ``printf -- '-%s' x``.
        """
        assert is_dangerous(vector["input"]) is vector["expected"]

    @pytest.mark.parametrize(("command", "live"), _literal_data_params())
    def test_literal_data_positions(self, is_dangerous: Callable[[str], object], command: str, live: bool) -> None:
        """Each inert-data exemption retains refusal in the corresponding live write positions.

        The shared vectors pair every inert command (data, so allowed) with live commands (a write position, so
        refused); each command is its own case so a failure names the exact spelling.
        """
        assert is_dangerous(command) is live

    @pytest.mark.parametrize("builtin", ["printf", "print -f", "print -rf", "env printf"])
    @pytest.mark.parametrize("end_options", [False, True])
    @pytest.mark.parametrize("vector", vector_params("closed_format_matrix"))
    def test_closed_format_grammar_matrix(
        self, is_dangerous: Callable[[str], object], builtin: str, end_options: bool, vector: dict
    ) -> None:
        """Format admission depends on the closed grammar even when a format begins with a dash.

        Every format of the shared matrix runs through each printing builtin, with and without an end-of-options marker,
        and is admitted only when the grammar admits it.
        """
        before = "-- " if end_options and builtin.endswith("printf") else ""
        after = "-- " if end_options and not builtin.endswith("printf") else ""
        command = f"{builtin} {before}'{vector['format']}' {after}PATH x"

        assert is_dangerous(command) is (not vector["admitted"]), command

    def test_dash_leading_print_format(self, is_dangerous: Callable[[str], object]) -> None:
        """A dash-leading print format never escapes the percent-n prohibition."""
        assert is_dangerous("print -f '-%nf%s' PATH x") is True
