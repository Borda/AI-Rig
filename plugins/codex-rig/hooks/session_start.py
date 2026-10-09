#!/usr/bin/env python3
"""Emit a bounded read-only Codex Rig shim-health message at session start.

## Purpose

give an installed Codex session an actionable indication of managed role-shim health before normal work begins. It turns
the packaged doctor result into a short startup message so an operator can recognize degraded setup without opening
diagnostic files first. It also warns when the managed global-instructions block in ``CODEX_HOME/AGENTS.md`` names a
different Codex Rig root than this plugin, or no root at all, which happens after a direct plugin update that did not
re-render the block: the block's packaged ``shared/<file>`` pointers then read another package version, a removed one,
or (for a block from a template before the ``PLUGIN_ROOT`` line) no package.

## Scope

parses hook input and invokes the diagnostic surface only; it never installs, repairs, removes, or otherwise changes
shims. The doctor subprocess is bounded by input, output, and time limits, and the hook validates that it is running
from the active installed plugin root. The global-instructions comparison reads a bounded ``AGENTS.md`` through the
packaged installer's read-only parser and never writes. A missing file, or a missing, duplicated or hand-edited block,
produces no warning (``install_global_agents.py --check`` owns those diagnoses). A block naming no root warns without
re-reading this package's template: the template shipped beside this hook always defines ``PLUGIN_ROOT``, so a rootless
block was rendered from an older (or custom) template.

## Usage

the plugin hook runner executes this file with its JSON event on standard input; invoke ``python session_start.py`` only
for local diagnosis. Set ``PLUGIN_ROOT`` to the installed plugin directory when reproducing the hook locally, and
provide a ``SessionStart`` event envelope on standard input.

## Used by

the optional ``SessionStart`` hook declared by Codex Rig and the session-start acceptance tests. It is the presentation
boundary between hook lifecycle events and ``manage_role_agents.py doctor``; callers should not depend on its internal
helper functions.

## Outputs

prints a bounded JSON-compatible hook response that is informative but does not reveal private filesystem or credential
details. Healthy diagnostics produce a continue response without a system warning, while degraded or blocked checks
include a sanitized reason and the safe status command. A stale global-instructions root adds one warning naming only
the last component of each root (normally the plugin version), or that the block names none, and the re-render command.

## Failure

malformed input, unavailable plugin state, or a diagnostic error becomes a concise health warning so session startup
remains non-blocking. The handler returns a successful process status for these expected failures, allowing the host
session to continue while directing the operator to the doctor command.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

# The hook runs from the installed plugin cache; loading the installer parser must not write bytecode there.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

#: Largest hook event payload, in bytes, read from standard input before it is rejected as oversized.
MAX_INPUT_BYTES = 65_536
#: Largest stdout or stderr size, in bytes, accepted from the doctor subprocess.
MAX_OUTPUT_BYTES = 1_048_576
#: Character cap for the failed-check reason shown in the startup health message.
MAX_REASON_CHARS = 240
#: Largest global ``AGENTS.md``, in bytes, read to compare its managed plugin root; a larger file is not compared.
MAX_GLOBAL_AGENTS_BYTES = 1_048_576


def _response(message: str | None = None) -> str:
    """Encode one non-blocking SessionStart response."""
    value: dict[str, object] = {"continue": True, "suppressOutput": False}
    if message:
        value["systemMessage"] = message
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _input() -> dict[str, object]:
    """Read and validate the bounded SessionStart envelope."""
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("hook input is oversized")
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("hook_event_name") != "SessionStart":
        raise ValueError("SessionStart hook input required")
    return value


def _plugin_root() -> Path:
    """Bind the hook to the exact installed plugin root supplied by Codex."""
    configured = os.environ.get("PLUGIN_ROOT")
    if not configured:
        raise ValueError("PLUGIN_ROOT is unavailable")
    root = Path(configured).resolve(strict=True)
    script = Path(__file__).resolve(strict=True)
    if script != root / "hooks" / "session_start.py":
        raise ValueError("hook is outside the active plugin root")
    return root


def _health_message(result: dict[str, object], classification: str) -> str:
    """Render one bounded failed check without suggesting an unsafe repair."""
    checks = result.get("checks")
    reason = "details unavailable"
    if isinstance(checks, dict):
        preferred = "blocked" if classification == "blocked" else "degraded"
        for name in ("python", "platform", "filesystem", "executables", "package", "active_package"):
            value = checks.get(name)
            if isinstance(value, dict) and value.get("status") == preferred:
                detail = value.get("detail")
                if isinstance(detail, str) and detail:
                    reason = f"{name}: {detail}"
                    break
    if reason == "details unavailable":
        # Unknown-host refusal paths can carry only a top-level detail. Surface it
        # instead of the useless generic placeholder.
        top_detail = result.get("detail")
        if isinstance(top_detail, str) and top_detail:
            reason = top_detail
    reason = _bounded_reason(reason.replace("\n", " "))
    return (
        f"Codex Rig shim health: {classification} — {reason}. No files changed. "
        "Run $codex-rig:agent-shims status for all checks and safe next steps."
    )


def _bounded_reason(reason: str) -> str:
    """Keep a bounded diagnostic while retaining its final observed value."""
    if len(reason) <= MAX_REASON_CHARS:
        return reason
    prefix, separator, observed = reason.rpartition(", observed ")
    if not separator:
        return reason[:MAX_REASON_CHARS]
    suffix = f"{separator}{observed}"
    head_length = MAX_REASON_CHARS - len(suffix) - 1
    if head_length <= 0:
        return suffix[-MAX_REASON_CHARS:]
    return f"{prefix[:head_length]}…{suffix}"


def _managed_block_root(root: Path, agents: Path) -> tuple[bool, str | None]:
    """Report whether one authenticated global block exists and the plugin root it names, via this package's parser."""
    installer = root / "scripts" / "install_global_agents.py"
    spec = importlib.util.spec_from_file_location("codex_rig_install_global_agents", installer)
    if spec is None or spec.loader is None:
        raise ImportError("global-instructions installer is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with agents.open("rb") as stream:
        payload = stream.read(MAX_GLOBAL_AGENTS_BYTES + 1)
    body = module.authenticated_managed_body(payload) if len(payload) <= MAX_GLOBAL_AGENTS_BYTES else None
    if body is None:
        return False, None
    return True, module.rendered_plugin_root(body)


def _global_root_notice(root: Path) -> str | None:
    """Warn when the global managed block names another plugin root than the one running this hook, or none.

    Advisory only: an unreadable file or parser is not a shim-health failure, so it yields no warning rather than
    replacing the doctor result. Roots compare after symlink resolution, as the installer renders them.
    """
    home_value = os.environ.get("CODEX_HOME")
    try:
        agents = (Path(home_value) if home_value else Path.home() / ".codex") / "AGENTS.md"
        if not agents.is_file():
            return None
        managed, rendered = _managed_block_root(root, agents)
    except (OSError, ImportError, RuntimeError, SyntaxError):
        return None
    if not managed:
        return None
    if rendered is None:
        stale = "names no PLUGIN_ROOT (rendered from an older template)"
    elif os.path.normcase(os.path.realpath(rendered)) == os.path.normcase(os.path.realpath(root)):
        return None
    else:
        # Only the last component is shown: the full roots are private paths. Split on both separators so a root of
        # the other path flavour cannot leak whole.
        rendered_name = rendered.replace("\\", "/").rstrip("/").rpartition("/")[2]
        stale = _bounded_reason(f"names …/{rendered_name}, not this session's …/{root.name}")
    return (
        f"Codex Rig global instructions: stale PLUGIN_ROOT — the managed CODEX_HOME/AGENTS.md block {stale}, so its "
        "shared/ pointers do not read this package. No files changed. Re-render with $codex-rig:sync or this "
        "package's scripts/install_global_agents.py --source <installed-package>/assets/AGENTS.md --codex-home <home>."
    )


def _joined(*messages: str | None) -> str | None:
    """Combine the present startup warnings into one system message."""
    return " ".join(message for message in messages if message) or None


def main() -> int:
    """Run the packaged doctor and surface only actionable degraded health."""
    root_notice: str | None = None
    try:
        _input()
        root = _plugin_root()
        root_notice = _global_root_notice(root)
        manager = root / "scripts" / "manage_role_agents.py"
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            [sys.executable, str(manager), "doctor"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=25,
        )
        if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
            raise ValueError("doctor output is oversized")
        result = json.loads(completed.stdout)
        classification = result.get("classification") if isinstance(result, dict) else None
        if completed.returncode != 0 or classification not in {"healthy", "degraded", "blocked"}:
            raise ValueError("doctor did not return a valid diagnostic")
        message = None
        if classification != "healthy":
            message = _health_message(result, classification)
        print(_response(_joined(message, root_notice)))
        return 0
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as error:
        unavailable = f"Codex Rig shim health check unavailable: {error}. Run $codex-rig:agent-shims doctor."
        print(_response(_joined(unavailable, root_notice)))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
