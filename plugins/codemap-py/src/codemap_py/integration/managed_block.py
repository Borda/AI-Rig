"""Render and replace the sentinel-bounded region this plugin owns in a consumer file."""

from __future__ import annotations
import re
from codemap_py import __version__
from .types import PROVIDER_NAME
from .util import _sha256_bytes, _utc_now_iso


PROTOCOL_VERSION = "codemap-py.integration.v2"


# In-file managed-block scheme for `apply`'s source_write ops (canonical marker shape).
# `apply` never owns a whole file — it replaces only the sentinel-bounded region inside an
# existing, consumer-owned source file; everything outside the sentinels is preserved
# byte-for-byte. BLOCK_SCHEMA_VERSION is embedded in the begin marker so a future body-shape
# change is distinguishable from today's; the full sha256 of the enclosed body is the
# drift/foreign-tamper signal, independent of that version tag.
BLOCK_SCHEMA_VERSION = 1


_MANAGED_BEGIN_RE = re.compile(r"<!-- codemap-py:integration:begin v(\d+) sha256=([0-9a-f]{64}) -->\n")


_MANAGED_END = "<!-- codemap-py:integration:end -->\n"


# Read-only inspection contract for Codex Rig's own authenticated global-instructions block
# (plugins/codex-rig/scripts/install_global_agents.py BEGIN_PREFIX/END_MARKER). Never imported
# from codex-rig — the byte format is treated as a stable, independently
# verifiable contract, not a Python API.
_CODEX_RIG_AGENTS_BEGIN_RE = re.compile(rb"<!-- codex-rig:global-agents begin sha256=([0-9a-f]{64}) -->\n")


_CODEX_RIG_AGENTS_END = b"<!-- codex-rig:global-agents end -->\n"


def _render_managed_block(body: str) -> str:
    """Wrap *body* in an authenticated, version-stamped, sha256-stamped managed block.

    Canonical shape:
    ``<!-- codemap-py:integration:begin v<schema> sha256=<64hex> -->`` ... enclosed body ...
    ``<!-- codemap-py:integration:end -->``.

    Examples:
        >>> block = _render_managed_block("hello\\n")
        >>> _managed_block_status(block)
        'authenticated'
    """
    digest = _sha256_bytes(body.encode("utf-8"))
    return f"<!-- codemap-py:integration:begin v{BLOCK_SCHEMA_VERSION} sha256={digest} -->\n{body}{_MANAGED_END}"


def _managed_block_status(content: str) -> str:
    """Return ``absent`` | ``authenticated`` | ``foreign_or_modified`` for enclosing-file *content*.

    Structural, tamper-evident check on whatever managed block currently exists in *content* —
    independent of any plan's before/after-state bookkeeping (see :func:`_classify_mutation`
    for the drift/idempotency layer built on top of this).

    Examples:
        >>> _managed_block_status("")
        'absent'
        >>> _managed_block_status("not a managed block")
        'absent'
    """
    matches = list(_MANAGED_BEGIN_RE.finditer(content))
    if not matches:
        return "absent"
    if len(matches) > 1 or content.count(_MANAGED_END) != 1:
        return "foreign_or_modified"
    match = matches[0]
    end_index = content.find(_MANAGED_END, match.end())
    if end_index == -1:
        return "foreign_or_modified"
    body = content[match.end() : end_index]
    return "authenticated" if _sha256_bytes(body.encode("utf-8")) == match.group(2) else "foreign_or_modified"


def _managed_block_body(runtime: str, consumer: str, version: str | None) -> str:
    """Return the contract-bound managed body for one provider/consumer wiring."""
    return (
        f"Provider: {PROVIDER_NAME} {__version__}\n"
        f"Runtime: {runtime}\n"
        f"Consumer: {consumer} {version or 'unknown'}\n"
        f"Protocol: {PROTOCOL_VERSION}\n"
        "Contract: shared/integration-contract.md\n"
        f"Updated: {_utc_now_iso()}\n"
    )


def _replace_managed_region(content: str, new_block: str) -> str:
    """Swap only the sentinel-bounded region in *content* for *new_block*; preserve the rest.

    Examples:
        >>> old = "before\\n" + _render_managed_block("ALPHA\\n") + "after\\n"
        >>> new = _replace_managed_region(old, _render_managed_block("BETA\\n"))
        >>> new.startswith("before\\n") and new.endswith("after\\n") and "BETA" in new and "ALPHA" not in new
        True
    """
    match = _MANAGED_BEGIN_RE.search(content)
    if match is None:
        return content  # caller already gated "replace" on sentinel presence; unreachable in practice
    end_index = content.find(_MANAGED_END, match.end())
    if end_index == -1:
        return content
    region_end = end_index + len(_MANAGED_END)
    return content[: match.start()] + new_block + content[region_end:]


def _mutate_content(original_text: str, new_block: str, action: str) -> str:
    """Return *original_text* with *new_block* either inserted (new file/EOF-appended) or swapped in.

    ``"insert"`` appends at EOF preceded by one blank line (or writes just the block for an
    absent/empty file); ``"replace"`` swaps only the existing sentinel-bounded region, leaving
    everything outside it byte-for-byte unchanged.

    Examples:
        >>> _mutate_content("", "BLOCK\\n", "insert")
        'BLOCK\\n'
        >>> _mutate_content("existing\\n", "BLOCK\\n", "insert")
        'existing\\n\\nBLOCK\\n'
    """
    if action == "insert":
        if not original_text:
            return new_block
        body = original_text if original_text.endswith("\n") else original_text + "\n"
        return f"{body}\n{new_block}"
    return _replace_managed_region(original_text, new_block)


def _managed_protocol(content: str) -> str | None:
    """Return the declared protocol inside an authenticated managed block, if present."""
    match = _MANAGED_BEGIN_RE.search(content)
    if match is None:
        return None
    end_index = content.find(_MANAGED_END, match.end())
    body = content[match.end() : end_index] if end_index != -1 else ""
    for line in body.splitlines():
        if line.startswith("Protocol: "):
            return line.removeprefix("Protocol: ")
    return None
