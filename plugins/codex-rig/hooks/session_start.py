#!/usr/bin/env python3
"""Provide reply guidance for ordinary Codex Rig sessions and report startup shim health.

## Purpose

Give an installed Codex session concise reply guidance even without a managed global instructions file or a skill
invocation. On startup and resume, also report managed role-shim health before normal work begins. The hook turns the
packaged doctor result into a short startup message so an operator can recognize degraded setup without opening
diagnostic files first. It also warns when the managed global-instructions block in ``CODEX_HOME/AGENTS.md`` names a
different Codex Rig root than this plugin, or no root at all, which happens after a direct plugin update that did not
re-render the block: the block's packaged ``shared/<file>`` pointers then read another package version, a removed one,
or (for a block from a template before the ``PLUGIN_ROOT`` line) no package.

## Scope

Parses hook input; clear, compact, and fork events only receive the static, package-owned model context. The startup and
resume diagnostic never installs, repairs, removes, or otherwise changes shims. The doctor subprocess is bounded by
input, output, and time limits, and the hook validates that it is running from the active installed plugin root. The
global-instructions comparison reads a bounded ``AGENTS.md`` through the packaged installer's read-only parser and never
writes. A missing file, or a missing, duplicated or hand-edited block, produces no warning (``install_global_agents.py
--check`` owns those diagnoses). A block naming no root warns without re-reading this package's template: the template
shipped beside this hook always defines ``PLUGIN_ROOT``, so a rootless block was rendered from an older (or custom)
template.

## Usage

the plugin hook runner executes this file with its JSON event on standard input; invoke ``python session_start.py`` only
for local diagnosis. Set ``PLUGIN_ROOT`` to the installed plugin directory when reproducing the hook locally, and
provide a ``SessionStart`` event envelope on standard input.

## Used by

the optional ``SessionStart`` hook declared by Codex Rig and the session-start acceptance tests. It is the presentation
boundary between hook lifecycle events and ``manage_role_agents.py doctor``; callers should not depend on its internal
helper functions.

## Outputs

Prints model guidance in the SessionStart-specific additional context. Only static package text enters that field.
Healthy diagnostics produce a continue response without a system warning, while degraded or blocked checks include a
sanitized reason and the status command. A stale global-instructions warning names only the last component of each root
(normally the plugin version), or reports that the block names none. Ordinary files receive the scoped installer and
verification steps. A symbolic link requires the linked instructions' owner to review the source.

## Failure

Invalid events and unverifiable plugin roots continue the session without adding model guidance. Event warnings describe
the observed input problem and how to recognize guidance on a supported event. Root warnings identify the package
binding to check. Once the hook validates the event and root, doctor failures retain the static guidance. An oversized
stream stops the diagnostic and reports its per-stream limit and the expected result on a later run. These handled
failures produce a user-visible warning and a successful process exit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

# A source file cannot contain a bytecode cache directory.
sys.pycache_prefix = str(Path(__file__).resolve())

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
REPLY_GUIDANCE = (
    "Start user-facing replies, including explanations and refusals, with the outcome, situation or requested action. "
    "Use bullets for parallel facts, "
    "numbered steps for ordered actions, and compact tables when comparisons are easier to scan. "
    "Recommend the next action when feasible. If blocked, explain why, who can unblock it, how, and the resume condition. "
    "For a safe user-run step, give the copy-ready command, working directory, effect and success check when applicable. "
    "If you can execute it after a prerequisite, offer to run it then. For user-only steps, offer to verify the result. "
    "If no safe path to the goal exists, explain why the goal remains unmet without inventing an owner or alternative. "
    "When the goal is complete and nothing remains, say no further action is needed. "
    "Use long paragraphs of prose only when requested. Preserve exact output formats and let native question controls present "
    "their own questions and choices."
)


def _response(message: str | None = None, *, include_guidance: bool = False) -> str:
    """Encode static model guidance separately from any user-visible startup warning."""
    value: dict[str, object] = {"continue": True, "suppressOutput": False}
    if include_guidance:
        value["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": REPLY_GUIDANCE}
    if message:
        value["systemMessage"] = message
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


class _InvalidEvent(ValueError):
    """Describe a rejected envelope without exposing any caller-supplied content."""


def _start_source() -> str:
    """Read the bounded SessionStart envelope and return its supported source."""
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise _InvalidEvent(f"The event exceeded the {MAX_INPUT_BYTES:,}-byte input limit.")
    try:
        value = json.loads(payload)
    except (ValueError, RecursionError) as error:
        raise _InvalidEvent("Codex Rig could not decode the event as JSON.") from error
    if not isinstance(value, dict) or value.get("hook_event_name") != "SessionStart":
        raise _InvalidEvent("The input did not identify a SessionStart event.")
    source = value.get("source", "startup")
    if not isinstance(source, str) or source not in ("startup", "resume", "clear", "compact", "fork"):
        raise _InvalidEvent("The SessionStart event contained an unsupported start source.")
    return source


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


def _package_module(path: Path) -> ModuleType:
    """Load a helper by its exact path inside the already verified hook package."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ImportError("packaged helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _managed_block_root(root: Path, agents: Path) -> tuple[bool, str | None]:
    """Report whether one authenticated global block exists and the plugin root it names, via this package's parser."""
    module = _package_module(root / "scripts" / "install_global_agents.py")
    # Follow regular-file symlinks, but a changed target must neither stall startup nor be read as instructions.
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    with open(agents, "rb", opener=lambda path, flags: os.open(path, flags | nonblocking)) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return False, None
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
    except Exception:
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
    notice = (
        f"Codex Rig global instructions: stale PLUGIN_ROOT — the managed CODEX_HOME/AGENTS.md block {stale}, so its "
        "shared/ pointers do not read this package. No files changed."
    )
    if agents.is_symlink():
        return (
            f"{notice} AGENTS.md is a symbolic link. The installer cannot update a linked file. "
            "Ask the owner of the linked instructions to check which installation they serve. "
            "For this session, the managed block needs to name the active Codex Rig package."
        )
    # Run from the selected package so the warning never embeds a private or shell-interpreted pathname.
    if sys.platform == "win32":
        launcher = r".\bin\python.cmd"
        home_arg = "$(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' })"
    else:
        launcher = "./bin/python"
        home_arg = '"${CODEX_HOME:-$HOME/.codex}"'
    return (
        f"{notice} In a shell using this session's Codex home, "
        "run `codex plugin list --marketplace borda-ai-rig --json`. For the enabled Codex Rig version, open its "
        "directory under that home's plugins/cache/borda-ai-rig/codex-rig. To update the managed block, run "
        f"`{launcher} scripts/install_global_agents.py --source assets/AGENTS.md --codex-home {home_arg}`. "
        "After the installer succeeds, confirm on the next startup or resume that this stale PLUGIN_ROOT warning is gone."
    )


def _joined(*messages: str | None) -> str | None:
    """Combine the present startup warnings into one system message."""
    return " ".join(message for message in messages if message) or None


def main() -> int:
    """Deliver guidance for supported session events and run diagnostics only at startup or resume."""
    root_notice: str | None = None
    include_guidance = False
    unavailable = "Codex Rig could not read the SessionStart input. This event supplied no reply guidance."
    try:
        source = _start_source()
        unavailable = (
            "Codex Rig could not verify the active plugin root. Inspect the enabled Codex Rig SessionStart command "
            "in /hooks. Start a fresh session after PLUGIN_ROOT resolves to the installed package containing that hook."
        )
        root = _plugin_root()
        include_guidance = True
        if source not in ("startup", "resume"):
            print(_response(include_guidance=True))
            return 0
        unavailable = "Codex Rig shim health check unavailable. Run $codex-rig:agent-shims doctor for the diagnostic."
        root_notice = _global_root_notice(root)
        manager = root / "scripts" / "manage_role_agents.py"
        runner = _package_module(root / "shared" / "github_read.py")
        try:
            completed = runner.run_bounded_command(
                [sys.executable, str(manager), "doctor"], timeout=25, max_output_bytes=MAX_OUTPUT_BYTES
            )
        except runner.GitHubReadError:
            print(
                _response(
                    _joined(
                        "Codex Rig stopped the shim doctor at its output limit. On a later startup or resume, stdout "
                        f"and stderr must stay within {MAX_OUTPUT_BYTES:,} bytes each. The hook recognizes a completed "
                        "diagnostic when the doctor exits 0 with a JSON classification of healthy, degraded or blocked.",
                        root_notice,
                    ),
                    include_guidance=True,
                )
            )
            return 0
        result = json.loads(completed.stdout)
        classification = result.get("classification") if isinstance(result, dict) else None
        if completed.returncode != 0 or classification not in {"healthy", "degraded", "blocked"}:
            raise ValueError("doctor did not return a valid diagnostic")
        message = None
        if classification != "healthy":
            message = _health_message(result, classification)
        print(_response(_joined(message, root_notice), include_guidance=True))
        return 0
    except _InvalidEvent as error:
        print(
            _response(
                f"{error} This event supplied no reply guidance. On a supported SessionStart event, "
                "hookSpecificOutput.additionalContext carries the guidance."
            )
        )
        return 0
    except Exception:
        print(_response(_joined(unavailable, root_notice), include_guidance=include_guidance))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
