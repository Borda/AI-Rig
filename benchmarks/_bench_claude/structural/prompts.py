"""System-prompt assembly and per-arm tool exposure."""

from __future__ import annotations

from pathlib import Path

from _bench_common.codemap_discovery import resolve_index_path

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

# Shared neutral wrapper used by BOTH arms. Everything here is identical across arms —
# repo framing, cwd note, the single efficiency instruction, and the output-format
# requirements per task type — so the token metric is not confounded by prompt asymmetry
# (only one arm being told to stop early). The arm-specific `{tools_section}` differs solely
# in tool availability and syntax; it carries no answering strategy.
_SHARED_SYSTEM_TEMPLATE = """You are a developer investigating the {repo_name} codebase.
Your current working directory IS the repository root ({repo_path}) — use relative paths (e.g. `find . -name "*.py"`) or absolute paths starting with {repo_path}.

{tools_section}

Answer in as few tool calls as possible; do not re-verify results you already have.

Output format requirements:
For symbol location tasks: report exactly in this format:
  file_path: <path>  start_line: <N>  end_line: <M>
For caller count tasks: report the integer count of unique production callers.
For caller list tasks: report all callers as a list of qualified names (module::function).
For file-identification tasks (debugging, feature scaffolding, issue triage): put the relevant files under a `## Files` heading, one repository-relative path per line (e.g. `pkg/sub/module.py`), not a bare filename.
For symbol-review tasks (undocumented / uncovered symbols): put the symbols under a `## Symbols` heading, one qualified name per line.

Be concise and precise. State the exact values you found (counts, line numbers, module names)."""

# Plain arm — tool availability only; scan-query prohibition preserved verbatim.
_PLAIN_TOOLS = """Answer the question using Grep, Bash, Glob, and Read. Do NOT use the Skill tool.
Do NOT use scan-query or any codemap binary — not via bare command, not via python/python3 path.
Rely on standard filesystem and grep operations only."""

# Codemap arm — tool availability plus scan-query invocation syntax and subcommand
# reference. This is tool documentation only; it prescribes no answering strategy
# (no "call scan-query first", "stop after one call", "trust as authoritative", etc.).
_CODEMAP_TOOLS = """You have the scan-query structural index tool available, in addition to Grep, Bash, Glob, and Read.

scan-query is a Python script on your PATH. Invoke it via Bash:
  scan-query --index {index_path} <subcommand> [args]

Subcommands:
  symbol <name> [--with-imports]         — get source + line range of a symbol by name
  find-symbol <pattern>                  — regex search across all symbol qualified names
  symbols <module>                       — list all symbols in a module
  fn-rdeps <qname> [--exclude-tests]    — callers of a function (`count` = unique callers)
  rdeps <module>                         — modules that import a module
  undocumented [module] [--all]          — symbols lacking docstrings
  uncovered [module] [--top N]           — symbols lacking test coverage
  coupled [--top N]                      — most-coupled modules
  xrefs <module> [--broken]             — Sphinx cross-references
  central [--top N] [--exclude-tests]   — most-imported modules, ranked by importer count
  path <source> <target>                 — a shortest import path between two modules
  fn-blast <qname>                       — transitive caller closure of a function
  diff-impact [--base REF]               — structural blast radius of the current git change set
  batch [FILE|-]                         — run many queries in one process (reads a JSON array of {{cmd, args}})"""

_C_STRICT_USE = "\n\nYou must use Codemap at least once for structural investigation; other tools remain allowed."


def _transport_arm(arm: str) -> str:
    """Return the legacy transport setup that implements one arm label."""
    return {"A_plain": "plain", "B_auto": "codemap", "C_strict": "codemap"}.get(arm, arm)


def _build_system_prompt(arm: str, repo_name: str, repo_path: str, index_path: str) -> str:
    """Assemble the system prompt for one arm from the shared neutral wrapper.

    Both arms receive identical repo framing, the single efficiency instruction, and the
    output-format requirements; only the tool-availability section differs. The plain arm's
    section forbids scan-query; the codemap arm's section documents scan-query syntax and
    subcommands. Keeping every non-tool sentence identical prevents prompt asymmetry from
    confounding the token-ratio headline.

    Args:
        arm: Legacy ``plain``/``codemap`` or canonical A/B/C arm label.
        repo_name: Human-readable repository name for framing.
        repo_path: Absolute path to the repository root (the agent cwd).
        index_path: Path to the pre-built codemap index (codemap arm only; ignored for plain).

    Returns:
        The fully formatted ``--system-prompt`` string for the given arm.

    Examples:
        >>> p = _build_system_prompt("plain", "demo", "/repo", "/x.json")
        >>> "do not re-verify results you already have" in p
        True
        >>> "scan-query" in _build_system_prompt("codemap", "demo", "/repo", "/x.json")
        True
    """
    transport_arm = _transport_arm(arm)
    tools_section = _PLAIN_TOOLS if transport_arm == "plain" else _CODEMAP_TOOLS.format(index_path=index_path)
    if arm == "C_strict":
        tools_section += _C_STRICT_USE
    return _SHARED_SYSTEM_TEMPLATE.format(repo_name=repo_name, repo_path=repo_path, tools_section=tools_section)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_index(repo_path: Path, explicit: Path | None = None) -> Path:
    """Resolve the codemap index path (raises on miss; validates an explicit path).

    Thin adapter over :func:`_bench_common.codemap_discovery.resolve_index_path`: ``-master``/``-main`` stems,
    ``.cache/codemap/`` before ``.cache/scan/``, resolved paths, ``FileNotFoundError`` on a
    miss, and an explicit path must be an existing file.

    Args:
        repo_path: Root of the cloned repository.
        explicit: Explicit ``--index-path`` argument; validated and returned when provided.

    Returns:
        Path to the index JSON file.

    Raises:
        FileNotFoundError: When no index can be found, or an explicit path is not a file.
    """
    return resolve_index_path(repo_path, explicit, strip_suffixes=True, missing="raise", require_explicit_file=True)
