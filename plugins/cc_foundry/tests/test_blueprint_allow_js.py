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
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

import build_blueprint_manifest as bbm

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "blueprint-allow.js"
VECTORS = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "blueprint_normalization_vectors.json").read_text(encoding="utf-8")
)


def _vector_params(group: str) -> list:
    """Return the shared fixture's vectors for ``group`` as one ``pytest.param`` each."""
    return [pytest.param(vector, id=vector["id"]) for vector in VECTORS[group]]


NODE_UNAVAILABLE = shutil.which("node") is None

#: Blueprint texts seeded into every synthetic manifest, keyed by role.
SINGLE = 'RUN_DIR="$(cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}")"'
BLOCK = 'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\nmkdir -p .reports/review\necho "$CSID"'
OTHER = "git rev-parse --show-toplevel"


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


@pytest.fixture(name="run_blueprint")
def _run_blueprint(plugin_root: Path) -> Callable[..., dict]:
    """Return a callable that writes a manifest, runs the hook, and parses its stdout."""

    def _run(command: str, *, manifest: bytes | None = None, tool_name: str = "Bash") -> dict:
        """Run the copied hook with one optional fixture manifest."""
        if manifest is not None:
            (plugin_root / "blueprint-manifest.json").write_bytes(manifest)
        payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
        proc = subprocess.run(
            ["node", str(plugin_root / "hooks" / HOOK.name)],
            input=payload,
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root)},
            timeout=10,
        )
        assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
        out = (proc.stdout or "").strip()
        return json.loads(out) if out else {}

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


def _is_allowed(result: dict) -> bool:
    """Check whether the hook allowed the requested operation.

    Examples:
        >>> (_is_allowed({"hookSpecificOutput": {"permissionDecision": "allow"}}), _is_allowed({}))
        (True, False)
    """
    try:
        return result["hookSpecificOutput"]["permissionDecision"] == "allow"
    except (KeyError, TypeError):
        return False


# ── Verbatim blueprint text is allowed ────────────────────────────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
class TestVerbatimAllow:
    """A command whose normalized text is a manifest entry gets permissionDecision allow."""

    def test_single_command_entry(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """A single-line blueprint command matching one entry is allowed.

        This is the dominant blueprint class the prefix matcher cannot express at all:
        the command opens with an assignment and contains ``$(...)``, so no
        settings.json allow rule can ever match it.
        """
        result = run_blueprint(SINGLE, manifest=seeded)
        assert _is_allowed(result), f"{SINGLE!r} should be allowed, got: {result}"

    def test_whole_block_entry(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """A multi-line block sent verbatim matches its whole-block entry.

        The block's own lines are NOT in the manifest here, so an allow proves the whole-block digest path fired rather
        than the per-command fallback.
        """
        result = run_blueprint(BLOCK, manifest=seeded)
        assert _is_allowed(result), f"block should be allowed, got: {result}"

    def test_trailing_comment_variant(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """A blueprint command sent with an extra trailing comment still hash-matches.

        Comment stripping runs before hashing, so the model appending an explanatory ``# ...`` to blueprint text does
        not cost the allow. The comment is benign — it carries no separator, which would split a new segment for the
        danger check.
        """
        result = run_blueprint(f"{SINGLE}  # resolve the run dir", manifest=seeded)
        assert _is_allowed(result), f"comment variant should be allowed, got: {result}"

    def test_trailing_whitespace_variant(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """Trailing whitespace on a line does not cost the allow — the generator rstrips too.

        Leading whitespace is a different matter and must still miss: the pipeline
        right-strips only, so a hook trimming both ends would be normalizing more
        aggressively than the generator.
        """
        result = run_blueprint(SINGLE + "   ", manifest=seeded)
        assert _is_allowed(result), f"trailing-whitespace variant should be allowed, got: {result}"

    def test_crlf_variant(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """A block arriving with CRLF line endings normalizes to the committed digest.

        The manifest is generated on whichever host ran pre-commit; a Windows-side command must still match, which is
        what the CRLF step of the pipeline buys.
        """
        result = run_blueprint(BLOCK.replace("\n", "\r\n"), manifest=seeded)
        assert _is_allowed(result), f"CRLF variant should be allowed, got: {result}"

    def test_every_logical_command_hits(self, run_blueprint: Callable[..., dict]) -> None:
        """A multi-line command is allowed when each of its logical commands is an entry.

        Nothing matches the combined text, so only the composition path can allow it — and it may only do so with full,
        not partial, coverage.
        """
        manifest = _manifest_bytes({"a.md:1": bbm.normalize(SINGLE), "b.md:2": bbm.normalize(OTHER)})
        result = run_blueprint(f"{SINGLE}\n{OTHER}", manifest=manifest)
        assert _is_allowed(result), f"fully covered pair should be allowed, got: {result}"

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
            "",
            "   ",
            "ls -la",
        ],
    )
    def test_deviations_passthrough(self, run_blueprint: Callable[..., dict], seeded: bytes, command: str) -> None:
        """Text that is not verbatim blueprint content is never auto-allowed."""
        result = run_blueprint(command, manifest=seeded)
        assert result == {}, f"{command!r} was allowed — gate bypass risk: {result}"

    def test_recombined_halves_of_two_entries(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """Splicing the first half of one entry onto the second half of another must not allow.

        This is the composition attack the manifest design exists to refuse: fragments
        are never trusted, only whole reviewed commands. Neither the spliced whole nor
        either fragment is a manifest digest.
        """
        spliced = SINGLE.split("=", 1)[0] + "=" + OTHER.split(" ", 1)[1]
        result = run_blueprint(spliced, manifest=seeded)
        assert result == {}, f"{spliced!r} was allowed — composition bypass: {result}"

    def test_missing_manifest(self, run_blueprint: Callable[..., dict]) -> None:
        """With no manifest on disk the hook allows nothing and still exits 0."""
        result = run_blueprint(SINGLE)
        assert result == {}

    def test_malformed_manifest_json(self, run_blueprint: Callable[..., dict]) -> None:
        """A truncated or corrupt manifest is treated as absent, never as a crash."""
        result = run_blueprint(SINGLE, manifest=b'{"schema": 1, "entries": {')
        assert result == {}

    def test_manifest_entries_wrong_type(self, run_blueprint: Callable[..., dict]) -> None:
        """A manifest whose ``entries`` is not an object is rejected rather than indexed."""
        result = run_blueprint(SINGLE, manifest=b'{"schema": 1, "entries": []}')
        assert result == {}

    def test_non_bash_tool(self, run_blueprint: Callable[..., dict], seeded: bytes) -> None:
        """Non-Bash tool payloads are ignored even when the text would match."""
        result = run_blueprint(SINGLE, manifest=seeded, tool_name="Read")
        assert result == {}

    def test_malformed_stdin(self, plugin_root: Path) -> None:
        """Malformed stdin never crashes or blocks."""
        proc = subprocess.run(
            ["node", str(plugin_root / "hooks" / HOOK.name)],
            input="not json",
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
        assert proc.returncode == 0
        assert (proc.stdout or "").strip() == ""


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

    def test_generator_would_never_emit_the_tampered_entry(self) -> None:
        """The hand-crafted tamper case is genuinely one the generator rejects.

        Without this the defence-in-depth test could be passing for the wrong reason — a case the generator drops anyway
        is only interesting if the manifest COULD hold it, so the test proves the entry is synthetic, not reachable.
        """
        entries, dropped = bbm.block_entries(bbm.normalize('BUILD=$(rm -rf "$HOME/build")'), "f.md:1")
        assert entries == {}
        assert dropped == 1


# ── Shared cross-language vectors ─────────────────────────────────────────────


def _node_eval(expression: str) -> object:
    """Require the hook as a module and return the JSON-encoded value of ``expression``."""
    script = f"const h = require({json.dumps(str(HOOK))}); process.stdout.write(JSON.stringify({expression}));"
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


@_skip_node_unavailable
class TestSharedVectors:
    """The JS port must reproduce the Python pipeline on every shared vector."""

    @pytest.mark.parametrize("vector", _vector_params("normalize"))
    def test_normalize(self, vector: dict) -> None:
        """Match JavaScript normalization with the shared fixture and Python implementation.

        Asymmetry here is the one real bug class of this design: a hook that
        normalizes more aggressively than the generator lets a crafted command
        collide with a manifest digest.
        """
        assert _node_eval(f"h.normalize({json.dumps(vector['input'])})") == vector["expected"]

    @pytest.mark.parametrize("vector", _vector_params("needs_bailout"))
    def test_needs_bailout(self, vector: dict) -> None:
        """Match JavaScript bailout decisions with the generator."""
        assert _node_eval(f"h.needsBailout({json.dumps(vector['input'])})") is vector["expected"]

    @pytest.mark.parametrize("vector", _vector_params("split_logical_commands"))
    def test_split_logical_commands(self, vector: dict) -> None:
        """Match JavaScript command splitting with the generator."""
        assert _node_eval(f"h.splitLogicalCommands({json.dumps(vector['input'])})") == vector["expected"]

    @pytest.mark.parametrize("vector", _vector_params("is_dangerous"))
    def test_is_dangerous(self, vector: dict) -> None:
        """Match JavaScript risk classification with the generator.

        This is the asymmetry that matters most. The generator's filter decides what reaches the manifest and the hook's
        decides what a digest hit is still allowed to do; a command only one side calls dangerous means the secondary
        defense is no longer independent of the layer it exists to backstop.
        """
        assert _node_eval(f"h.isDangerous({json.dumps(vector['input'])})") is vector["expected"]

    def test_digests_agree_across_languages(self) -> None:
        """Python and JS produce the same digest for the same normalized text.

        The hash is the actual join between generator and hook; equal normalization with a different encoding assumption
        would still miss every entry.
        """
        text = bbm.normalize(BLOCK)
        assert _node_eval(f"h.sha256Text({json.dumps(text)})") == bbm.sha256_text(text)


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

        def _hook(cmd: str) -> dict:
            """Run the committed hook against one Bash command."""
            proc = subprocess.run(
                ["node", str(HOOK)],
                input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
                capture_output=True,
                encoding="utf-8",
                timeout=10,
            )
            assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
            out = (proc.stdout or "").strip()
            return json.loads(out) if out else {}

        mutated = normalized[:-1] + ("y" if normalized[-1] == "x" else "x")
        assert _is_allowed(_hook(raw)), f"committed block should be allowed: {raw!r}"
        assert _hook(mutated) == {}, f"mutated block was allowed: {mutated!r}"


# ── Library entry point added for the dispatcher ──────────────────────────────


@_skip_node_unavailable
class TestEvaluateClassification:
    """``evaluate`` labels what the module did, running the same checks in the same order as ``decide``.

    The labels are what the audit record carries, so a mislabelled decline is a mis-recorded history. Three distinctions
    matter and each is asserted separately:

    * ``passthrough`` versus ``none`` — examined and declined, versus never examined at all;
    * which check declined it — a danger refusal, a missing manifest and a plain miss are different facts;
    * digest presence — the digest exists exactly when the command was normalized, and is never invented.
    """

    @staticmethod
    def _evaluate(plugin_root: Path, command: str, *, tool_name: str = "Bash", raw: str | None = None) -> dict:
        """Require the copied hook as a module and return ``evaluate``'s result for one payload."""
        stdin = raw if raw is not None else json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
        script = (
            f"const h = require({json.dumps(str(plugin_root / 'hooks' / HOOK.name))});"
            f"process.stdout.write(JSON.stringify(h.evaluate({json.dumps(stdin)})));"
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root)},
            timeout=10,
        )
        assert proc.returncode == 0, f"node failed: {proc.stderr}"
        return json.loads(proc.stdout)

    def test_allow_carries_lane_rank_src_and_digest(self, plugin_root: Path, seeded: bytes) -> None:
        """A digest hit is rank 1 and reports the plugin file the text came from."""
        (plugin_root / "blueprint-manifest.json").write_bytes(seeded)
        result = self._evaluate(plugin_root, SINGLE)
        assert (result["decision"], result["lane"], result["rank"]) == ("allow", "blueprint", 1)
        assert result["src"] == "skills/review/SKILL.md:12"
        assert result["digest"] and "digest" in result

    def test_danger_refusal_is_labelled_as_such(self, plugin_root: Path) -> None:
        """The independent danger re-check is a decision, and the record must say which check refused.

        A refusal on a digest hit means the manifest held something it should not — a generator bug, a stale manifest or
        an edited one. Recording it as a plain miss would hide the one case worth investigating.
        """
        command = 'BUILD=$(rm -rf "$HOME/build")'
        (plugin_root / "blueprint-manifest.json").write_bytes(_manifest_bytes({"t.md:1": bbm.normalize(command)}))
        result = self._evaluate(plugin_root, command)
        assert (result["decision"], result["why"], result["payload"]) == ("passthrough", "danger", None)

    def test_missing_manifest_is_distinguished_from_a_miss(self, plugin_root: Path) -> None:
        """An unreadable manifest is a different fact from a command that simply is not in one."""
        result = self._evaluate(plugin_root, SINGLE)
        assert (result["decision"], result["why"]) == ("passthrough", "manifest-unavailable")

    def test_plain_miss_is_a_passthrough_with_a_digest(self, plugin_root: Path, seeded: bytes) -> None:
        """The command was normalized and looked up, so the digest belongs on the record."""
        (plugin_root / "blueprint-manifest.json").write_bytes(seeded)
        result = self._evaluate(plugin_root, "ls -la")
        assert (result["decision"], result["why"]) == ("passthrough", "no-match")
        assert result["digest"] == bbm.sha256_text(bbm.normalize("ls -la"))

    @pytest.mark.parametrize("vector", [v for v in VECTORS["needs_bailout"] if v["expected"]])
    def test_bailout_is_labelled_separately_from_a_miss(self, plugin_root: Path, seeded: bytes, vector: dict) -> None:
        """Text the per-command splitter refuses to reason about is a bailout, not a miss.

        The bailout check is consulted only to LABEL a decline that already happened — a digest hit returns before it —
        so labelling can never turn an allow into a passthrough.
        """
        (plugin_root / "blueprint-manifest.json").write_bytes(seeded)
        result = self._evaluate(plugin_root, vector["input"])
        assert result["decision"] == "passthrough"
        assert result["why"] in ("bailout", "no-match")

    @pytest.mark.parametrize(
        ("kwargs", "case"),
        [
            pytest.param({"raw": "not json"}, "unparsable stdin", id="parse-failure"),
            pytest.param({"command": SINGLE, "tool_name": "Read"}, "not a Bash call", id="non-bash-tool"),
            pytest.param({"command": ""}, "no command at all", id="empty-command"),
        ],
    )
    def test_returning_before_deciding_is_none_with_no_digest(self, plugin_root: Path, kwargs: dict, case: str) -> None:
        """Nothing normalized the command, so there is no digest to record — and no opinion either."""
        result = self._evaluate(plugin_root, kwargs.pop("command", ""), **kwargs)
        assert (result["decision"], result["why"]) == ("none", "not-applicable"), case
        assert "digest" not in result

    def test_whitespace_only_reaches_a_decision_here(self, plugin_root: Path, seeded: bytes) -> None:
        """The two decision modules genuinely diverge on whitespace, and the record keeps both answers.

        This module normalizes first and reaches ``no-match``; the shape module trims and returns before deciding. One
        is an abstention and the other is the absence of an opinion.
        """
        (plugin_root / "blueprint-manifest.json").write_bytes(seeded)
        assert self._evaluate(plugin_root, "   ")["decision"] == "passthrough"

    def test_evaluate_returns_the_same_payload_decide_produces(self, plugin_root: Path, seeded: bytes) -> None:
        """One payload builder, so the two entry points can never disagree on a byte of stdout."""
        (plugin_root / "blueprint-manifest.json").write_bytes(seeded)
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": SINGLE}})
        script = (
            f"const h = require({json.dumps(str(plugin_root / 'hooks' / HOOK.name))});"
            f"process.stdout.write(JSON.stringify([h.evaluate({json.dumps(stdin)}).payload,"
            f" h.decide({json.dumps(stdin)})]));"
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root)},
            timeout=10,
        )
        from_evaluate, from_decide = json.loads(proc.stdout)
        assert from_evaluate == from_decide

    def test_evaluate_never_exits_the_process(self, plugin_root: Path) -> None:
        """A library that calls ``process.exit`` takes its caller's other lane down with it."""
        script = (
            f"const h = require({json.dumps(str(plugin_root / 'hooks' / HOOK.name))});"
            'h.evaluate("not json"); h.evaluate(JSON.stringify({tool_name: "Bash", tool_input: {command: "ls"}}));'
            "process.stdout.write('survived');"
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root)},
            timeout=10,
        )
        assert proc.stdout == "survived"
