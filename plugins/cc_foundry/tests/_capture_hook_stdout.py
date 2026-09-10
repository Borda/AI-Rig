"""Generator for the frozen pre-refactor stdout captures of the two decision hooks.

``fixtures/hook_stdout_captures.json`` records the exact bytes ``hooks/blueprint-allow.js`` and
``hooks/sentinel-read-allow.js`` wrote to stdout, for a corpus of commands, at the revision named in the fixture's
``captured_at_rev``. The dispatcher differential suite replays those bytes: a refactor that changes what either module
prints — or what the dispatcher prints in their place — fails against a record neither implementation can influence.

Comparing two freshly refactored implementations against each other would let shared drift pass, which is why the
record is a committed artifact rather than a value recomputed at test time.

**Regenerating discards the evidence.** Run this only to establish a new baseline deliberately, from a tree whose
decision modules are the ones the new baseline should describe, and say so in the commit message. Routine test runs
never invoke it.

Usage:
    python plugins/cc_foundry/tests/_capture_hook_stdout.py [--out PATH]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_HOOKS_DIR = _PLUGIN_DIR / "hooks"
_BIN_DIR = _PLUGIN_DIR / "bin"

if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))

import build_blueprint_manifest as bbm  # noqa: E402  (path set above)

BLUEPRINT_HOOK = "blueprint-allow.js"
SENTINEL_HOOK = "sentinel-read-allow.js"

#: Blueprint texts seeded into the ``seeded`` manifest, keyed by the ``src`` label reported in the allow reason.
#: Deliberately the same three the blueprint suite uses, so an allow captured here is an allow that suite also covers.
SEEDED_TEXTS = {
    "skills/review/SKILL.md:12": 'RUN_DIR="$(cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}")"',
    "skills/review/SKILL.md:40": 'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\nmkdir -p .reports/review\necho "$CSID"',
    "rules/git.md:7": "git rev-parse --show-toplevel",
}

#: Commands the parametrized suites do not carry but whose captured bytes the differential suite needs anyway —
#: the allow paths of the seeded manifest and the documented normalization-tolerant variants of them.
EXTRA_COMMANDS = (
    *SEEDED_TEXTS.values(),
    f"{list(SEEDED_TEXTS.values())[0]}  # resolve the run dir",
    list(SEEDED_TEXTS.values())[0] + "   ",
    list(SEEDED_TEXTS.values())[1].replace("\n", "\r\n"),
    f"{list(SEEDED_TEXTS.values())[0]}\n{list(SEEDED_TEXTS.values())[2]}",
    "",
    "   ",
    "\n",
    "\t \n ",
)

#: Manifest bodies that are not built from the corpus. ``None`` means "write no manifest file at all".
STATIC_MANIFESTS: dict[str, bytes | None] = {
    "no-manifest": None,
    "malformed": b'{"schema": 1, "entries": {',
    "entries-not-object": b'{"schema": 1, "entries": []}',
}

#: Scenarios applied to a reduced corpus — every command passes through them identically, so the full sweep would
#: record 200 copies of one fact.
REDUCED_SCENARIOS = ("malformed", "entries-not-object")

#: Raw stdin payloads that are not valid hook input. Captured because the modules' behaviour on them is contractual.
RAW_STDIN_CASES = {
    "not-json": "not json",
    "empty-stdin": "",
    "empty-object": "{}",
    "missing-tool-input": '{"tool_name": "Bash"}',
    "null-command": '{"tool_name": "Bash", "tool_input": {"command": null}}',
    "command-not-string": '{"tool_name": "Bash", "tool_input": {"command": 42}}',
}


def _b64(raw: bytes) -> str:
    """Return ``raw`` as ASCII base64 — the only encoding that survives a JSON round trip unchanged."""
    return base64.b64encode(raw).decode("ascii")


def _manifest_bytes(texts: dict[str, str]) -> bytes:
    """Encode a manifest whose entries are the digests of ``texts`` (``src`` label to already-normalized text)."""
    entries = {bbm.sha256_text(text): {"kind": "block", "src": src} for src, text in texts.items()}
    return bbm.encode_manifest({"schema": 1, "plugin": "cc_foundry@0.0.0", "entries": entries})


def _seeded_manifest() -> bytes:
    """Return the manifest holding the three seeded blueprint texts."""
    return _manifest_bytes({src: bbm.normalize(text) for src, text in SEEDED_TEXTS.items()})


def _run_hook(root: Path, hook: str, stdin_text: str) -> dict:
    """Run one hook out of ``root/hooks`` and return its raw stdout, stderr and exit code."""
    proc = subprocess.run(
        ["node", str(root / "hooks" / hook)],
        input=stdin_text.encode("utf-8"),
        capture_output=True,
        env={"PATH": _path_env(), "CLAUDE_PLUGIN_ROOT": str(root)},
        timeout=30,
        check=False,
    )
    return {"stdout_b64": _b64(proc.stdout), "stderr_b64": _b64(proc.stderr), "exit": proc.returncode}


def _path_env() -> str:
    """Return a minimal ``PATH`` — the hooks must not inherit anything else from the capturing shell."""
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


def _payload(command: str, tool_name: str = "Bash") -> str:
    """Return a PreToolUse stdin payload carrying ``command``."""
    return json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})


def _corpus() -> list[str]:
    """Return every command the capture sweeps, deduplicated and ordered for a stable diff."""
    from _hook_command_corpus import collect_commands

    seen = {*collect_commands(), *EXTRA_COMMANDS}
    return sorted(seen)


def _reduced(commands: Iterable[str]) -> list[str]:
    """Return the subset swept under the reduced scenarios: the seeded texts plus a few structural shapes."""
    picks = {*SEEDED_TEXTS.values(), "", "   ", "ls -la", "git status --short"}
    return sorted(pick for pick in picks if pick in set(commands) or pick in SEEDED_TEXTS.values())


def capture(out_path: Path) -> dict:
    """Run both modules over the corpus and return the fixture object."""
    commands = _corpus()
    seeded = _seeded_manifest()
    manifests: dict[str, bytes | None] = {"seeded": seeded, **STATIC_MANIFESTS}

    fixture: dict = {
        "_comment": (
            "Frozen stdout bytes of the two decision modules, recorded before they were given evaluate() wrappers. "
            "Regenerated only to establish a new baseline deliberately; see _capture_hook_stdout.py."
        ),
        "captured_at_rev": _git_rev(),
        "node_version": _node_version(),
        "manifests": {
            name: (None if body is None else {"sha256": hashlib.sha256(body).hexdigest(), "b64": _b64(body)})
            for name, body in manifests.items()
        },
        "blueprint": {},
        "shape": {},
        "raw_stdin": {"blueprint": {}, "shape": {}},
    }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "plugin"
        (root / "hooks").mkdir(parents=True)
        for hook in (BLUEPRINT_HOOK, SENTINEL_HOOK):
            shutil.copy(_HOOKS_DIR / hook, root / "hooks" / hook)
        manifest_path = root / "blueprint-manifest.json"

        for scenario, body in manifests.items():
            targets = _reduced(commands) if scenario in REDUCED_SCENARIOS else commands
            manifest_path.unlink(missing_ok=True)
            if body is not None:
                manifest_path.write_bytes(body)
            fixture["blueprint"][scenario] = {
                command: _run_hook(root, BLUEPRINT_HOOK, _payload(command)) for command in targets
            }

        # ``tampered`` builds a single-entry manifest out of each command itself, which is the only way to reach the
        # digest-hit-then-danger-refusal path. One manifest per command, so it cannot share the loop above.
        tampered: dict[str, dict] = {}
        for command in commands:
            manifest_path.write_bytes(_manifest_bytes({"tampered.md:1": bbm.normalize(command)}))
            tampered[command] = _run_hook(root, BLUEPRINT_HOOK, _payload(command))
        fixture["blueprint"]["tampered-self"] = tampered
        fixture["manifests"]["tampered-self"] = (
            "per-command: one entry, the digest of the command's own normalized text"
        )

        manifest_path.unlink(missing_ok=True)
        manifest_path.write_bytes(seeded)
        fixture["blueprint"]["non-bash-tool"] = {
            command: _run_hook(root, BLUEPRINT_HOOK, _payload(command, tool_name="Read"))
            for command in _reduced(commands)
        }

        # The shape module reads no manifest, so one sweep covers it.
        fixture["shape"]["bash"] = {command: _run_hook(root, SENTINEL_HOOK, _payload(command)) for command in commands}
        fixture["shape"]["non-bash-tool"] = {
            command: _run_hook(root, SENTINEL_HOOK, _payload(command, tool_name="Read"))
            for command in _reduced(commands)
        }

        for name, stdin_text in RAW_STDIN_CASES.items():
            fixture["raw_stdin"]["blueprint"][name] = _run_hook(root, BLUEPRINT_HOOK, stdin_text)
            fixture["raw_stdin"]["shape"][name] = _run_hook(root, SENTINEL_HOOK, stdin_text)

    out_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return fixture


def _git_rev() -> str:
    """Return the current commit, suffixed ``-dirty`` when the decision modules differ from it."""
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(_PLUGIN_DIR), check=False
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", f"hooks/{BLUEPRINT_HOOK}", f"hooks/{SENTINEL_HOOK}"],
        capture_output=True,
        text=True,
        cwd=str(_PLUGIN_DIR),
        check=False,
    ).stdout.strip()
    return f"{rev}-dirty" if dirty else rev


def _node_version() -> str:
    """Return the node that produced the capture — recorded so a later mismatch is visible, not silent."""
    return subprocess.run(["node", "--version"], capture_output=True, text=True, check=False).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    """Write the capture fixture and report what it holds.

    Examples:
        >>> isinstance(main.__doc__, str)
        True
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_TESTS_DIR / "fixtures" / "hook_stdout_captures.json")
    args = parser.parse_args(argv)
    fixture = capture(args.out)
    blueprint = sum(len(cases) for cases in fixture["blueprint"].values())
    shape = sum(len(cases) for cases in fixture["shape"].values())
    print(
        f"wrote {args.out} — {blueprint} blueprint captures, {shape} shape captures, rev {fixture['captured_at_rev']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
